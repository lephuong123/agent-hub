#!/usr/bin/env python3
"""Đổi tên máy móc agent-hub → cfharness trên toàn repo (chạy lại được, có --dry-run).

    python3 scripts/rename_cfharness.py --dry-run     # thống kê, không ghi
    python3 scripts/rename_cfharness.py               # áp dụng
    python3 scripts/rename_cfharness.py --no-schema   # không sinh lại docs/schema/event.v1.json

Làm gì:
1. `git mv src/agent_hub src/cfharness` (bỏ qua nếu đã chuyển).
2. Thay trong file chữ của PHẠM VI (`INCLUDE`): `AGENT_HUB_`→`CFHARNESS_`, `agent_hub`→`cfharness`, `agent-hub`→`cfharness`
   (gồm `.agent-hub`, `agent-hub.localhost`, `/tmp/agent-hub-…`, `[agent-hub …]`, tên project/script trong pyproject + uv.lock).
   KHÔNG đổi: dòng có `rename:keep`; đường dẫn thư mục repo (`Workspace/agent-hub`, `-Workspace-agent-hub`); fixture stream-json
   ghi thật (`*.jsonl`); chuỗi Hermes nguyên văn (`learning/prompts/hermes_*.py` — chỉ đổi dòng chú thích `#`);
   docs/ ngoài dev-notes + schema, research/, demo/, scripts/, .git, .venv.
3. `uv lock` + sinh lại `docs/schema/event.v1.json` bằng `python -m cfharness.event_schema --write`.
4. In chỗ còn sót (ngoài dòng rename:keep) để người xem.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD_PKG, NEW_PKG = "src/agent_hub", "src/cfharness"
INCLUDE = ["src", "tests", "AGENTS.md", "README.md", "NOTICE", "pyproject.toml", "uv.lock", "docs/dev-notes", ".gitignore"]
SKIP_PARTS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache"}
SKIP_SUFFIX = {".jsonl", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".pyc", ".db", ".sqlite"}
COMMENT_ONLY = re.compile(r"(^|/)learning/prompts/hermes_[^/]*\.py$")
KEEP = "rename:keep"
SKIP_FILES = {"docs/dev-notes/rename-cfharness.md"}  # ghi chú về chính việc đổi tên — giữ nguyên tên cũ

# thứ tự quan trọng: tên biến môi trường trước, rồi module, rồi tên lệnh/thư mục
RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"AGENT_HUB"), "CFHARNESS"),
    (re.compile(r"agent_hub(?!_demo)"), "cfharness"),  # demo/ (agent_hub_demo) không đổi
    # đường dẫn thư mục repo (`~/Workspace/agent-hub`, `-Users-x-Workspace-agent-hub`) giữ nguyên — repo không đổi tên thư mục
    (re.compile(r"(?<!Workspace/)(?<!Workspace-)agent-hub"), "cfharness"),
]
LEFTOVER = re.compile(r"(?i:agent[-_]hub)(?!_demo)|AGENT_HUB")


def tracked_files() -> list[Path]:
    out: list[Path] = []
    for inc in INCLUDE:
        p = ROOT / inc
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                rel = str(f.relative_to(ROOT))
                if f.is_file() and not (set(f.relative_to(ROOT).parts) & SKIP_PARTS) and f.suffix.lower() not in SKIP_SUFFIX and rel not in SKIP_FILES:
                    out.append(f)
    return out


def rewrite_line(line: str) -> tuple[str, int]:
    if KEEP in line:
        return line, 0
    n = 0
    for pat, rep in RULES:
        line, k = pat.subn(rep, line)
        n += k
    return line, n


def rewrite_text(text: str, rel: str) -> tuple[str, int]:
    comment_only = bool(COMMENT_ONLY.search(rel))
    out: list[str] = []
    total = 0
    for line in text.splitlines(keepends=True):
        if comment_only and not line.lstrip().startswith("#"):
            out.append(line)
            continue
        nl, n = rewrite_line(line)
        out.append(nl)
        total += n
    return "".join(out), total


def move_package(dry: bool) -> str:
    old, new = ROOT / OLD_PKG, ROOT / NEW_PKG
    if new.exists() and not old.exists():
        return "đã chuyển trước đó"
    if new.exists() and old.exists():
        raise SystemExit(f"lỗi: cả {OLD_PKG} lẫn {NEW_PKG} cùng tồn tại — gộp tay trước")
    if not old.exists():
        raise SystemExit(f"lỗi: không thấy {OLD_PKG}")
    if dry:
        return f"sẽ git mv {OLD_PKG} → {NEW_PKG}"
    shutil.rmtree(old / "__pycache__", ignore_errors=True)
    r = subprocess.run(["git", "mv", OLD_PKG, NEW_PKG], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:  # thư mục có file chưa track (vd tính năng đang làm) → git mv từ chối; chuyển thường rồi git add
        shutil.move(str(old), str(new))
        subprocess.run(["git", "add", "-A", OLD_PKG, NEW_PKG], cwd=ROOT, check=False, capture_output=True)
    return f"git mv {OLD_PKG} → {NEW_PKG}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-schema", action="store_true", help="không chạy uv lock / sinh lại schema")
    a = ap.parse_args()

    print(f"[1] package: {move_package(a.dry_run)}")
    per_file: Counter[str] = Counter()
    for f in tracked_files():
        rel = str(f.relative_to(ROOT))
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new, n = rewrite_text(text, rel)
        if n:
            per_file[rel] = n
            if not a.dry_run:
                f.write_text(new, encoding="utf-8")
    print(f"[2] {'sẽ thay' if a.dry_run else 'đã thay'} {sum(per_file.values())} chỗ trong {len(per_file)} file")
    by_top = Counter()
    for rel, n in per_file.items():
        by_top["/".join(rel.split("/")[:2])] += n
    for k, v in by_top.most_common():
        print(f"    {v:>5}  {k}")

    if not a.dry_run and not a.no_schema:
        r = subprocess.run(["uv", "lock"], cwd=ROOT, capture_output=True, text=True)
        print(f"[3] uv lock: {'ok' if r.returncode == 0 else 'LỖI ' + r.stderr.strip()[-300:]}")
        r = subprocess.run(["uv", "run", "python", "-m", "cfharness.event_schema", "--write", "docs/schema/event.v1.json"],
                           cwd=ROOT, capture_output=True, text=True)
        print(f"    schema: {'ok' if r.returncode == 0 else 'LỖI ' + (r.stderr or r.stdout).strip()[-300:]}")

    # [4] chỗ còn sót trong phạm vi (ngoài rename:keep và đường dẫn repo)
    left: list[str] = []
    for f in tracked_files():
        rel = str(f.relative_to(ROOT))
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(lines, 1):
            if KEEP in line or not LEFTOVER.search(line):
                continue
            if a.dry_run:
                line = rewrite_text(line, rel)[0]
                if not LEFTOVER.search(line):
                    continue
            rest = re.sub(r"Workspace[/-]agent-hub", "", line)
            if COMMENT_ONLY.search(rel) and not line.lstrip().startswith("#"):
                continue
            if LEFTOVER.search(rest):
                left.append(f"{rel}:{i}: {line.strip()[:160]}")
    print(f"[4] còn sót cần người xem: {len(left)}")
    for x in left[:200]:
        print("    " + x)
    if not a.dry_run:
        print("Tiếp: `uv sync`, `uv run cfharness --help`, `uv run pytest -q`; người dùng chạy `cfharness home migrate` và `cfharness mcp snippet`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
