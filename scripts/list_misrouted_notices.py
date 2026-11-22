#!/usr/bin/env python3
"""Liệt kê tin báo chủ động / câu hỏi của điều phối viên đã bị ĐOÁN sang hội thoại của luồng khác (lỗi rò tin giữa các luồng, trước bản sửa
`ManagerService.fallback_conversation`). CHỈ ĐỌC: mở `hub.db` bằng `?mode=ro`, không ghi gì.

    uv run python scripts/list_misrouted_notices.py                 # HUB_HOME mặc định (CFHARNESS_HOME hoặc ~/.cfharness)
    uv run python scripts/list_misrouted_notices.py --home <dir>    # HUB_HOME khác
    uv run python scripts/list_misrouted_notices.py --db <hub.db>   # chỉ định thẳng tệp DB

Tiêu chí:
- notify: event `manager.reply` (kind=notify) có `conversation_guessed=1`, hội thoại nhận thuộc một luồng mà luồng của run khác nó
  (hoặc run không thuộc luồng nào). Dòng `manager_messages` tương ứng (role=notify, cùng run + hội thoại) cho id tin.
- question: câu hỏi `source=manager` của lượt nhịp mà đích là ĐOÁN (run không thuộc luồng, không có đúng một hội thoại gốc
  `tick_convs`) và hội thoại của câu hỏi thuộc một luồng (cờ đoán không lưu cho câu hỏi — suy lại theo cùng luật `_target_conv`).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    from cfharness.paths import resolve_home

    return resolve_home(args.home) / "hub.db"


def _opts(raw: str | None) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def collect(conn: sqlite3.Connection, *, all_guessed: bool = False) -> list[dict]:
    """`all_guessed` → kèm cả tin đoán KHÔNG chéo luồng (rơi vào cuộc Sảnh bất kỳ) — cột `cross` cho biết có chéo luồng không."""
    conn.row_factory = sqlite3.Row
    convs = {r["id"]: dict(r) for r in conn.execute("SELECT id, channel, title, workstream_id FROM manager_conversations")}
    ws = {r["id"]: r["key"] or r["title"] for r in conn.execute("SELECT id, key, title FROM workstreams")}

    def run_opts(run_id: str | None) -> dict:
        if not run_id:
            return {}
        r = conn.execute("SELECT run_opts FROM runs WHERE id=?", (run_id,)).fetchone()
        return _opts(r[0]) if r else {}

    out: list[dict] = []
    for e in conn.execute("SELECT ts, run_id, payload FROM events WHERE type='manager.reply' "
                          "AND json_extract(payload, '$.conversation_guessed') = 1 ORDER BY ts"):
        p = _opts(e["payload"])
        cid = p.get("conversation_id")
        c = convs.get(cid) or {}
        cws = c.get("workstream_id")
        rws = run_opts(e["run_id"]).get("manager_workstream")
        cross = bool(cws) and cws != rws
        if not cross and not all_guessed:
            continue
        m = conn.execute("SELECT id FROM manager_messages WHERE conversation_id=? AND run_id=? AND role='notify' ORDER BY ABS(julianday(ts) - julianday(?)) LIMIT 1",
                         (cid, e["run_id"], e["ts"])).fetchone()
        out.append({"kind": "notify", "id": f"msg:{m[0]}" if m else "msg:?", "ts": e["ts"], "run": e["run_id"], "conv": cid,
                    "conv_ws": f"{cws} ({ws.get(cws, '?')})" if cws else None, "run_ws": rws, "cross": cross,
                    "text": str(p.get("text") or "")[:80]})
    for q in conn.execute("SELECT id, created_at, run_id, conversation_id, text FROM questions WHERE source='manager' AND conversation_id IS NOT NULL "
                          "ORDER BY created_at"):
        c = convs.get(q["conversation_id"]) or {}
        cws = c.get("workstream_id")
        if not cws and not all_guessed:
            continue
        o = run_opts(q["run_id"])
        own = convs.get(o.get("manager_conversation")) or {}
        if own.get("channel") != "heartbeat" or o.get("manager_workstream"):
            continue  # run chat / nhịp của một luồng → không phải đoán
        if len([x for x in (o.get("tick_convs") or []) if x]) == 1:
            continue
        out.append({"kind": "question", "id": q["id"], "ts": q["created_at"], "run": q["run_id"], "conv": q["conversation_id"],
                    "conv_ws": f"{cws} ({ws.get(cws, '?')})" if cws else None, "run_ws": None, "cross": bool(cws),
                    "text": str(q["text"] or "")[:80]})
    return sorted(out, key=lambda r: str(r["ts"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Liệt kê (chỉ đọc) tin notify/câu hỏi bị đoán sang hội thoại của luồng khác.")
    ap.add_argument("--home", help="HUB_HOME (mặc định: CFHARNESS_HOME hoặc ~/.cfharness)")
    ap.add_argument("--db", help="đường dẫn hub.db (thay cho --home)")
    ap.add_argument("--all", action="store_true", help="kèm cả tin bị đoán nhưng không chéo luồng (rơi vào cuộc ở Sảnh)")
    ap.add_argument("--json", action="store_true", help="in JSON thay cho bảng")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # console Windows (cp1252) không in được tiếng Việt
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    db = _db_path(args)
    if not db.exists():
        print(f"Không thấy {db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = collect(conn, all_guessed=args.all)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print(f"{r['kind']:8} {r['id']:<14} {r['ts']}  run={r['run']}  conv={r['conv']}  luồng hội thoại={r['conv_ws'] or 'Sảnh'}  "
                  f"luồng run={r['run_ws'] or '-'}  {r['text']!r}")
        cross = [r for r in rows if r["cross"]]
        print(f"Tổng: {len(cross)} tin bị đoán sang luồng khác ({sum(r['kind'] == 'notify' for r in cross)} notify, "
              f"{sum(r['kind'] == 'question' for r in cross)} câu hỏi)" + (f"; {len(rows)} tin bị đoán tất cả" if args.all else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
