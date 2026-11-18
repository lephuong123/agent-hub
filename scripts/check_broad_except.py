#!/usr/bin/env python3
"""Kiểm "nuốt lỗi" trong `src/cfharness` (L8, `docs/dev-notes/plan-logging.md` B4) — chỉ stdlib, dựa trên AST.

    python3 scripts/check_broad_except.py            # in mọi handler chưa đạt (path:line) + tổng
    python3 scripts/check_broad_except.py --check    # mã thoát 1 nếu file nào TĂNG so với baseline (hoặc file mới có vi phạm)
    python3 scripts/check_broad_except.py --update   # ghi lại scripts/broad_except_baseline.txt

Handler bị xét: `except Exception`, `except BaseException`, `except:` trần, và tuple có chứa một trong hai.
Handler ĐẠT khi có ít nhất một điều:
  (a) thân handler có lời gọi log kèm thông tin exception: `<x>.exception(...)` (log.exception, logging.exception…), hoặc
      `<x>.debug|info|warning|warn|error|critical|log(..., exc_info=<khác False/None>)`;
  (b) thân handler có `raise` (ở bất kỳ nhánh nào, không tính trong hàm/lambda lồng);
  (c) dòng `except` hoặc các dòng tới hết dòng lệnh đầu của thân có chú thích:
      `# fail-closed: <lý do>`, `# best-effort: <lý do>`, hoặc `# pragma: no cover — <lý do>` / `# noqa: BLE001 — <lý do>`
      (gạch `—`, `–` hoặc `-`, sau đó phải có chữ). `# pragma: no cover` trần (không lý do) KHÔNG đạt.

Baseline = số handler chưa đạt THEO FILE (`path:count`), vì số dòng hay xê dịch. `--check` chỉ cho phép GIẢM.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "cfharness"
BASELINE = ROOT / "scripts" / "broad_except_baseline.txt"

BROAD = {"Exception", "BaseException"}
LOG_LEVELS = {"debug", "info", "warning", "warn", "error", "critical", "log"}
_REASON = r"\s*\S"
COMMENT_RES = (
    re.compile(r"#\s*fail-closed\s*:" + _REASON),
    re.compile(r"#\s*best-effort\s*:" + _REASON),
    re.compile(r"#\s*pragma:\s*no cover\s*[—–-]" + _REASON),
    re.compile(r"#\s*noqa:\s*BLE001\s*[—–-]" + _REASON),
)


def _is_broad(node: ast.expr | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in BROAD
    if isinstance(node, ast.Attribute):  # builtins.Exception
        return node.attr in BROAD
    if isinstance(node, ast.Tuple):
        return any(_is_broad(e) for e in node.elts)
    return False


def _walk_local(nodes: list[ast.stmt]):
    """Duyệt thân handler, không đi vào hàm/lớp/lambda lồng."""
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    stack: list[ast.AST] = [n for n in nodes if not isinstance(n, nested)]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(c for c in ast.iter_child_nodes(n) if not isinstance(c, nested))


def _logs_exc(call: ast.Call) -> bool:
    f = call.func
    name = f.attr if isinstance(f, ast.Attribute) else None
    if name == "exception":
        return True
    if name in LOG_LEVELS:
        for kw in call.keywords:
            if kw.arg == "exc_info":
                v = kw.value
                return not (isinstance(v, ast.Constant) and v.value in (False, None))
    return False


def _comments(source: str) -> dict[int, str]:
    out: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string
    except (tokenize.TokenError, IndentationError, SyntaxError):  # best-effort: file lỗi cú pháp đã bị ast.parse báo
        pass
    return out


def handler_ok(h: ast.ExceptHandler, comments: dict[int, str]) -> bool:
    for n in _walk_local(h.body):
        if isinstance(n, ast.Raise):
            return True
        if isinstance(n, ast.Call) and _logs_exc(n):
            return True
    first = h.body[0]
    last_line = getattr(first, "end_lineno", None) or first.lineno
    # dòng lệnh đầu có thể là khối nhiều dòng (if/with) — chỉ xét tới dòng mở của nó
    if isinstance(first, (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFor, ast.AsyncWith)):
        last_line = first.lineno
    for ln in range(h.lineno, last_line + 1):
        c = comments.get(ln)
        if c and any(r.search(c) for r in COMMENT_RES):
            return True
    return False


def scan_source(source: str) -> list[int]:
    """Số dòng các handler rộng CHƯA đạt trong một đoạn mã."""
    tree = ast.parse(source)
    comments = _comments(source)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and _is_broad(node.type) and not handler_ok(node, comments):
            bad.append(node.lineno)
    return sorted(bad)


def scan(root: Path = SRC) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else p.as_posix()
        lines = scan_source(p.read_text(encoding="utf-8"))
        if lines:
            out[rel] = lines
    return out


def read_baseline(path: Path = BASELINE) -> dict[str, int]:
    out: dict[str, int] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, n = line.rpartition(":")
        out[name] = int(n)
    return out


def write_baseline(found: dict[str, list[int]], path: Path = BASELINE) -> None:
    lines = [
        "# Số handler `except Exception` chưa đạt quy tắc nuốt lỗi, theo file (scripts/check_broad_except.py --update).",
        "# Chỉ được GIẢM: thêm handler mới phải log kèm exc_info hoặc có `# fail-closed: …` / `# best-effort: …`.",
    ]
    lines += [f"{k}:{len(v)}" for k, v in sorted(found.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def regressions(found: dict[str, list[int]], baseline: dict[str, int]) -> list[str]:
    out = []
    for k, v in sorted(found.items()):
        allowed = baseline.get(k, 0)
        if len(v) > allowed:
            out.append(f"{k}: {len(v)} > baseline {allowed} (dòng {', '.join(map(str, v))})")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="thất bại nếu số vi phạm của file nào tăng so với baseline")
    g.add_argument("--update", action="store_true", help="ghi lại baseline")
    a = ap.parse_args(argv)
    found = scan()
    total = sum(len(v) for v in found.values())
    if a.update:
        write_baseline(found)
        print(f"đã ghi {BASELINE.relative_to(ROOT)}: {total} handler ở {len(found)} file")
        return 0
    if a.check:
        bad = regressions(found, read_baseline())
        for b in bad:
            print(b)
        print(f"tổng {total} handler chưa đạt; {'TĂNG ở ' + str(len(bad)) + ' file' if bad else 'không tăng'}")
        return 1 if bad else 0
    for k, v in found.items():
        for ln in v:
            print(f"{k}:{ln}")
    print(f"tổng {total} handler chưa đạt ở {len(found)} file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
