"""Cách chấm golden task (FR-LN-10, Q-07) — do NGƯỜI DÙNG chọn cho từng task.

Tất định (không gọi model):
- `regex`       : `{"kind":"regex","pattern":"...","flags":"im","must_match":true}`
- `contains`    : `{"kind":"contains","text":"...","ignore_case":true}` (hoặc `"all":[...]` / `"any":[...]`)
- `json_schema` : `{"kind":"json_schema","schema":{...}}` — lấy `structured_output` của run, không có thì tìm khối JSON
                  trong text (khối ```json``` hoặc `{…}` đầu tiên)
- `command`     : `{"kind":"command","cmd":"pytest -q","timeout_s":120}` — chạy trong thư mục làm việc của run eval,
                  stdin = output; env `CFHARNESS_OUTPUT_FILE`/`CFHARNESS_OUTPUT_JSON`; exit 0 = đạt.
                  `"score_from_stdout": true` → dòng số cuối cùng (0..1) của stdout là điểm.
LLM judge (gọi `claude -p --json-schema --no-session-persistence`, xem `judge.py`):
- `llm_judge`   : `{"kind":"llm_judge","rubric":"<rubric người viết>","model":"haiku","pass_threshold":0.7}`
Tổ hợp:
- `all`         : `{"kind":"all","checks":[...]}` — điểm = trung bình, đạt khi mọi check đạt.

Điểm luôn trong [0, 1]. Run eval không `completed` → 0 điểm (không chấm).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import HubError

KINDS = ("regex", "contains", "json_schema", "command", "llm_judge", "all")
_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


@dataclass
class EvalOutput:
    """Kết quả một run eval đưa vào bộ chấm."""

    text: str
    structured: Any = None
    outcome: str = "completed"
    run_id: str | None = None
    cwd: Path | None = None
    prompt: str = ""


@dataclass
class Grade:
    score: float
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    judge_run_ids: list[str] = field(default_factory=list)


def validate_checker(spec: Any, *, _depth: int = 0) -> dict[str, Any]:
    """Kiểm cấu trúc `checker` khi người tạo/sửa golden task. Trả spec đã chuẩn hoá; sai → HubError."""
    if not isinstance(spec, dict):
        raise HubError("checker phải là object JSON/YAML có khoá 'kind'")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise HubError(f"checker.kind không hợp lệ: {kind!r} — chọn một trong {', '.join(KINDS)}")
    s = dict(spec)
    if kind == "regex":
        if not isinstance(s.get("pattern"), str) or not s["pattern"]:
            raise HubError("checker regex cần 'pattern'")
        flags = str(s.get("flags") or "")
        bad = [c for c in flags if c not in _FLAG_MAP]
        if bad:
            raise HubError(f"checker regex: cờ không hỗ trợ {bad} (dùng i m s x)")
        try:
            re.compile(s["pattern"], _flags(flags))
        except re.error as e:
            raise HubError(f"checker regex: pattern lỗi — {e}") from None
        s.setdefault("must_match", True)
    elif kind == "contains":
        if not any(isinstance(s.get(k), (str, list)) and s.get(k) for k in ("text", "all", "any")):
            raise HubError("checker contains cần 'text' hoặc 'all'/'any' (danh sách chuỗi)")
    elif kind == "json_schema":
        if not isinstance(s.get("schema"), dict):
            raise HubError("checker json_schema cần 'schema' (object JSON Schema)")
        try:
            import jsonschema

            jsonschema.validators.validator_for(s["schema"]).check_schema(s["schema"])
        except ImportError:  # pragma: no cover
            pass
        except Exception as e:
            raise HubError(f"checker json_schema: schema không hợp lệ — {str(e)[:200]}") from None
    elif kind == "command":
        if not isinstance(s.get("cmd"), str) or not s["cmd"].strip():
            raise HubError("checker command cần 'cmd' (lệnh shell)")
        s.setdefault("timeout_s", 120)
    elif kind == "llm_judge":
        if not isinstance(s.get("rubric"), str) or len(s["rubric"].strip()) < 10:
            raise HubError("checker llm_judge cần 'rubric' do người viết (≥ 10 ký tự)")
        thr = s.get("pass_threshold", 0.7)
        if not isinstance(thr, (int, float)) or not 0 <= float(thr) <= 1:
            raise HubError("checker llm_judge: pass_threshold phải trong [0, 1]")
    elif kind == "all":
        checks = s.get("checks")
        if not isinstance(checks, list) or not checks or _depth > 2:
            raise HubError("checker all cần 'checks' (danh sách checker, lồng tối đa 2 mức)")
        s["checks"] = [validate_checker(c, _depth=_depth + 1) for c in checks]
    return s


def uses_llm(spec: dict[str, Any]) -> bool:
    if spec.get("kind") == "llm_judge":
        return True
    return spec.get("kind") == "all" and any(uses_llm(c) for c in spec.get("checks") or [])


def _flags(s: str) -> int:
    f = 0
    for c in s:
        f |= _FLAG_MAP.get(c, 0)
    return f


def extract_json(out: EvalOutput) -> Any:
    if out.structured is not None:
        return out.structured
    text = out.text or ""
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    cands = [m.group(1)] if m else []
    cands.append(text.strip())
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        i, j = text.find(start_ch), text.rfind(end_ch)
        if 0 <= i < j:
            cands.append(text[i : j + 1])
    for c in cands:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
    raise ValueError("không tìm thấy JSON trong output")


def grade_deterministic(spec: dict[str, Any], out: EvalOutput) -> Grade:
    """Chấm các checker tất định. `llm_judge` không xử lý ở đây (xem `judge.py`)."""
    kind = spec["kind"]
    text = out.text or ""
    if kind == "regex":
        hit = re.search(spec["pattern"], text, _flags(str(spec.get("flags") or ""))) is not None
        ok = hit if spec.get("must_match", True) else not hit
        return Grade(1.0 if ok else 0.0, ok, {"kind": kind, "matched": hit})
    if kind == "contains":
        ic = bool(spec.get("ignore_case", True))
        hay = text.lower() if ic else text
        norm = (lambda s: s.lower()) if ic else (lambda s: s)
        if spec.get("text"):
            needles, mode = [spec["text"]] if isinstance(spec["text"], str) else list(spec["text"]), "all"
        elif spec.get("all"):
            needles, mode = list(spec["all"]), "all"
        else:
            needles, mode = list(spec["any"]), "any"
        found = [n for n in needles if norm(str(n)) in hay]
        if mode == "all":
            score = len(found) / len(needles)
            ok = len(found) == len(needles)
        else:
            ok = bool(found)
            score = 1.0 if ok else 0.0
        return Grade(score, ok, {"kind": kind, "found": found, "missing": [n for n in needles if n not in found]})
    if kind == "json_schema":
        try:
            data = extract_json(out)
        except ValueError as e:
            return Grade(0.0, False, {"kind": kind, "error": str(e)})
        try:
            import jsonschema

            errs = sorted(jsonschema.validators.validator_for(spec["schema"])(spec["schema"]).iter_errors(data), key=lambda e: list(e.path))
        except ImportError:  # pragma: no cover
            errs = []
        ok = not errs
        return Grade(1.0 if ok else 0.0, ok, {"kind": kind, "errors": [e.message[:200] for e in errs[:5]]})
    if kind == "command":
        return _run_command(spec, out)
    raise HubError(f"checker {kind} không phải tất định")


def _run_command(spec: dict[str, Any], out: EvalOutput) -> Grade:
    cwd = out.cwd if out.cwd and Path(out.cwd).is_dir() else Path(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix="cfharness-eval-") as td:
        of = Path(td) / "output.txt"
        of.write_text(out.text or "", encoding="utf-8")
        jf = Path(td) / "output.json"
        jf.write_text(json.dumps(out.structured, ensure_ascii=False) if out.structured is not None else "null", encoding="utf-8")
        env = {**os.environ, "CFHARNESS_OUTPUT_FILE": str(of), "CFHARNESS_OUTPUT_JSON": str(jf), "CFHARNESS_EVAL_RUN_ID": out.run_id or ""}
        try:
            p = subprocess.run(
                ["/bin/sh", "-c", spec["cmd"]], cwd=str(cwd), input=(out.text or "").encode("utf-8"), capture_output=True,
                timeout=float(spec.get("timeout_s") or 120), env=env,
            )
        except subprocess.TimeoutExpired:
            return Grade(0.0, False, {"kind": "command", "error": "timeout"})
    stdout = p.stdout.decode("utf-8", "replace")
    detail = {"kind": "command", "exit_code": p.returncode, "stdout_tail": stdout[-300:], "stderr_tail": p.stderr.decode("utf-8", "replace")[-300:]}
    if spec.get("score_from_stdout"):
        nums = re.findall(r"(?m)^\s*([01](?:\.\d+)?|0?\.\d+)\s*$", stdout)
        score = max(0.0, min(1.0, float(nums[-1]))) if nums else 0.0
        thr = float(spec.get("pass_threshold", 0.5))
        return Grade(score, p.returncode == 0 and score >= thr, detail)
    ok = p.returncode == 0
    return Grade(1.0 if ok else 0.0, ok, detail)
