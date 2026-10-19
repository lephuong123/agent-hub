"""Bản sao tạm của agent home cho eval (FR-LN-10) — KHÔNG đụng bản thật.

- `make_home(hub, agent, dest)`: chép `HUB_HOME/agents/<id>/` (SOUL, MEMORY, USER, skills, config.yaml…) sang `dest`,
  bỏ `workspaces/`, `runtime/`.
- `materialize(hub, home, cfg, proposal)`: áp proposal ỨNG VIÊN lên bản sao (config → dict cấu hình overlay;
  memory/skill/soul → file trong bản sao). Ưu tiên hàm của M4-A nếu có (`hub.learning.materialize(proposal, home, config)`).
- `materialize_before(hub, home, cfg, entry)`: dựng trạng thái TRƯỚC một ledger entry (cho post-apply).
- `apply_overlay(agent, overlay)`: gọi trong `RunManager._prepare_spec` cho run `kind=eval`: thay `home_path` bằng bản
  sao, ép workspace scratch trong bản sao, ghi đè trường cấu hình ứng viên (model, effort…).
- `fingerprint(...)`: băm cấu hình + tri thức → khoá cache điểm baseline.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import HubError

if TYPE_CHECKING:
    from ..hub import Hub

CONFIG_FIELDS = ("model", "effort", "fallback_model", "tool_policy", "mcp_servers", "limits", "subagents", "result_schema")
KNOWLEDGE_FILES = ("SOUL.md", "MEMORY.md", "USER.md")
_IGNORE = shutil.ignore_patterns("workspaces", "runtime", "__pycache__", "*.lock")


def make_home(src: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copytree(src, dest, ignore=_IGNORE, symlinks=True)
    else:
        dest.mkdir(parents=True)
    (dest / "workspaces").mkdir(exist_ok=True)
    return dest


def base_config(agent: dict[str, Any]) -> dict[str, Any]:
    return {k: agent.get(k) for k in CONFIG_FIELDS}


def _candidate_value(p: dict[str, Any]) -> Any:
    for k in ("candidate", "after", "new_value", "value", "content", "payload"):
        if k in p and p[k] is not None:
            return p[k]
    return None


def _config_change(p: dict[str, Any]) -> dict[str, Any]:
    v = _candidate_value(p)
    ref = p.get("target_ref") or p.get("field")
    if isinstance(v, dict):
        for k in ("changes", "config", "fields"):
            if isinstance(v.get(k), dict):
                return dict(v[k])
        known = {k: x for k, x in v.items() if k in CONFIG_FIELDS or k.lower() == "soul"}
        if known:
            return known
        for k in ("value", "new_value", "after", "content", "new"):
            if k in v and ref:
                return {str(ref): v[k]}
    elif v is not None and ref:
        return {str(ref): v}
    if isinstance(p.get("changes"), dict):
        return dict(p["changes"])
    raise HubError("proposal config không có giá trị ứng viên (candidate/after/new_value/payload + target_ref)")


def _file_for(home: Path, target: str, ref: str | None, p: dict[str, Any]) -> Path:
    if p.get("path"):
        rel = Path(str(p["path"]))
        if rel.is_absolute() or ".." in rel.parts:
            raise HubError("proposal.path phải tương đối trong agent home")
        return home / rel
    if target == "memory":
        return home / (ref if ref and ref.endswith(".md") and "/" not in ref else "MEMORY.md")
    if target == "user_profile":
        return home / "USER.md"
    if target == "soul":
        return home / "SOUL.md"
    if target == "skill":
        if not ref or "/" in ref or ref.startswith("."):
            raise HubError(f"tên skill không hợp lệ: {ref!r}")
        return home / "skills" / ref / "SKILL.md"
    raise HubError(f"target không hỗ trợ khi dựng bản sao eval: {target}")


def apply_unified_diff(original: str, diff: str) -> str:
    """Áp unified diff (một file) lên `original`. Lệch ngữ cảnh → HubError."""
    src = original.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    lines = diff.splitlines(keepends=True)
    k = 0
    while k < len(lines):
        ln = lines[k]
        if ln.startswith("@@"):
            try:
                parts = ln.split()[1][1:].split(",")  # -a,b
                a, b = int(parts[0]), (int(parts[1]) if len(parts) > 1 else 1)
            except (IndexError, ValueError):
                raise HubError(f"hunk header lỗi: {ln.strip()}") from None
            start = a if b == 0 else max(a - 1, 0)
            if start < i:
                raise HubError("hunk chồng lấn")
            out.extend(src[i:start])
            i = start
            k += 1
            while k < len(lines) and not lines[k].startswith("@@"):
                h = lines[k]
                if h.startswith("\\"):
                    k += 1
                    continue
                tag, body = h[:1], h[1:]
                if tag in (" ", "-"):
                    if i >= len(src) or src[i].rstrip("\r\n") != body.rstrip("\r\n"):
                        raise HubError(f"diff không khớp nội dung ở dòng {i + 1}")
                    if tag == " ":
                        out.append(src[i])
                    i += 1
                elif tag == "+":
                    out.append(body if body.endswith("\n") else body + "\n")
                elif h.strip() == "":
                    if i < len(src) and src[i].strip() == "":
                        out.append(src[i])
                        i += 1
                k += 1
            continue
        k += 1
    out.extend(src[i:])
    return "".join(out)


def materialize(hub: "Hub", home: Path, cfg: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    """Áp proposal ứng viên lên bản sao `home` + dict cấu hình `cfg` (đổi tại chỗ). Trả `cfg`."""
    learning = getattr(hub, "learning", None)
    fn = getattr(learning, "materialize", None)
    if callable(fn):  # hợp đồng với M4-A: A biết cách áp mọi loại proposal của chính nó (đọc proposal theo id)
        from ..errors import NotFound

        try:
            r = fn(proposal, home, cfg)
            return r if isinstance(r, dict) else cfg
        except NotFound:
            pass  # proposal không nằm trong bảng của A (vd `eval check --set` thủ công) → dựng theo dict bên dưới
    target = str(proposal.get("target") or "")
    op = str(proposal.get("op") or "patch")
    ref = proposal.get("target_ref")
    if target == "config":
        for k, v in _config_change(proposal).items():
            if k.lower() == "soul":
                (home / "SOUL.md").write_text(str(v), encoding="utf-8")
            elif k in CONFIG_FIELDS:
                cfg[k] = v
            else:
                raise HubError(f"trường cấu hình không hỗ trợ trong eval: {k}")
        return cfg
    path = _file_for(home, target, ref, proposal)
    if op in ("archive", "delete"):
        if target == "skill" and path.parent.exists():
            arch = home / ".archive" / "skills"
            arch.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path.parent), str(arch / path.parent.name))
        elif path.exists():
            path.unlink()
        return cfg
    v = _candidate_value(proposal)
    if isinstance(v, dict) and isinstance(v.get("content"), str):
        v = v["content"]
    if isinstance(v, str):
        new = v
    elif proposal.get("diff"):
        new = apply_unified_diff(path.read_text(encoding="utf-8") if path.exists() else "", str(proposal["diff"]))
    else:
        raise HubError("proposal không có nội dung ứng viên (after/content hoặc diff)")
    if op == "add" and target == "memory" and path.exists() and not proposal.get("replace") and isinstance(v, str) and not proposal.get("diff"):
        old = path.read_text(encoding="utf-8")
        new = old.rstrip("\n") + "\n" + new.strip("\n") + "\n" if new.strip() not in old else old
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return cfg


def _blob(hub: "Hub", sha: str | None) -> str | None:
    if not sha:
        return None
    p = hub.paths.home / "ledger" / "blobs" / str(sha)
    return p.read_text(encoding="utf-8") if p.exists() else None


def materialize_before(hub: "Hub", home: Path, cfg: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Dựng trạng thái TRƯỚC `entry` trên bản sao. Trả False nếu không dựng được (thiếu dữ liệu before)."""
    learning = getattr(hub, "learning", None)
    ledger = getattr(learning, "ledger", None)
    fn = getattr(ledger, "materialize_before", None) or getattr(learning, "materialize_before", None)
    if callable(fn):
        try:
            r = fn(entry, home, cfg)
            return r is not False
        except Exception:
            return False
    target = str(entry.get("target") or "")
    if target == "config":
        before = entry.get("before") or entry.get("before_config")
        if isinstance(before, str):
            try:
                before = json.loads(before)
            except json.JSONDecodeError:
                before = None
        if not isinstance(before, dict):
            return False
        for k, v in before.items():
            if k.lower() == "soul":
                (home / "SOUL.md").write_text(str(v), encoding="utf-8")
            elif k in CONFIG_FIELDS:
                cfg[k] = v
        return True
    try:
        path = _file_for(home, target, entry.get("target_ref"), entry)
    except HubError:
        return False
    content = entry.get("before_content")
    if content is None:
        content = _blob(hub, entry.get("before_blob"))
    op = str(entry.get("op") or "")
    if content is None:
        if op == "add" and not entry.get("before_blob"):  # file mới tạo → trước đó không có
            if target == "skill" and path.parent.exists():
                shutil.rmtree(path.parent)
            elif path.exists():
                path.unlink()
            return True
        if op in ("archive", "delete") and target == "skill":
            arch = home / ".archive" / "skills" / path.parent.name
            if arch.exists():
                shutil.move(str(arch), str(path.parent))
                return True
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def fingerprint(home: Path, cfg: dict[str, Any], *, hub: Any = None, agent_id: str | None = None) -> str:
    """Khoá cache điểm baseline = MỌI THỨ đi vào prompt/hành vi của run eval:
    cấu hình ứng viên + file tri thức tầng agent (SOUL/MEMORY/USER/skills của bản sao) + (khi có `hub`) system prompt
    THỰC dựng như `RunManager` (khối tri thức agent + team + global + USER theo PHIÊN BẢN gán cho agent — M5-A) và
    bản đồ phiên bản tri thức (`hub.knowledge.versions.version_map`). Đổi tri thức team/global → baseline tính lại."""
    h = hashlib.sha256()
    h.update(json.dumps(cfg, sort_keys=True, default=str, ensure_ascii=False).encode())
    for name in KNOWLEDGE_FILES:
        p = home / name
        h.update(name.encode() + b"\0" + (p.read_bytes() if p.exists() else b"") + b"\0")
    sk = home / "skills"
    if sk.is_dir():
        for p in sorted(sk.rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(sk)).encode() + b"\0" + p.read_bytes() + b"\0")
    if hub is not None and agent_id:
        h.update(b"\0prompt\0" + prompt_fingerprint(hub, agent_id, home).encode())
    return h.hexdigest()[:20]


def prompt_fingerprint(hub: Any, agent_id: str, home: Path) -> str:
    """Băm system prompt mà run eval trên bản sao `home` sẽ nhận + bản đồ phiên bản tri thức team/global/USER."""
    from ..prompt import build_system_prompt, prompt_hash

    parts: list[str] = []
    ks = getattr(hub, "knowledge", None)
    blocks: list[str] = []
    if ks is not None:
        try:
            from ..knowledge.prompt_blocks import build_knowledge_blocks

            blocks, _ = build_knowledge_blocks(ks, agent_id, home=home)
        except Exception as e:  # tri thức hỏng → khác mọi lần khác, không dùng cache cũ
            parts.append(f"blocks_error:{e!r}")
        vers = getattr(ks, "versions", None)
        if vers is not None and hasattr(vers, "version_map"):
            try:
                parts.append(json.dumps(vers.version_map(agent_id), sort_keys=True, default=str, ensure_ascii=False))
            except Exception as e:
                parts.append(f"versions_error:{e!r}")
    soul_p = home / "SOUL.md"
    text = build_system_prompt(agent_id, soul_p.read_text(encoding="utf-8") if soul_p.exists() else "", blocks)
    parts.insert(0, prompt_hash(text))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:20]


# ------------------------------------------------------------------ hook trong RunManager
def apply_overlay(agent: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Agent hiệu lực của run eval: home = bản sao tạm, workspace scratch trong bản sao, cấu hình ứng viên."""
    a = dict(agent)
    home = overlay.get("home")
    if not home:
        raise HubError("eval_overlay thiếu 'home'")
    a["home_path"] = str(home)
    a["workspace_default"] = None
    for k, v in (overlay.get("config") or {}).items():
        if k in CONFIG_FIELDS:
            a[k] = v
    return a


def seed_workspace(cwd: Path, overlay: dict[str, Any]) -> None:
    files = overlay.get("files") or {}
    if not files:
        return
    cwd.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        r = Path(rel)
        if r.is_absolute() or ".." in r.parts:
            continue
        p = cwd / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(content), encoding="utf-8")
