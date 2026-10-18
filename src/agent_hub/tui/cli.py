"""Lệnh CLI của M2: `top`, `attach <agent|run>`, `steer <run|agent> "<tin>"` (= `run steer`), `takeover <run|agent>`,
`template list|show`. `agent create <id> --template <tên>` nằm ở `cli.cmd_agent`.

`run steer` đụng parser cũ (`run <agent> <prompt>`) nên được viết lại thành lệnh ẩn `run-steer` (thêm vào
bảng `guard.cli.HIDDEN` dùng chung với `run continue`).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from typing import Any


def cmd_top(ctx: Any) -> int:
    from .app import TopApp

    ctx.client()  # tự khởi động daemon nếu chưa chạy
    attach = getattr(ctx.args, "target", None)
    TopApp(ctx.paths.socket, attach=attach, refresh_s=float(getattr(ctx.args, "refresh", 1.0) or 1.0)).run()
    return 0


def cmd_steer(ctx: Any) -> int:
    a = ctx.args
    r = ctx.call("run.steer", {"id": a.target, "text": a.text, "channel": "cli", "by": os.environ.get("USER") or "user"})
    ctx.out(r, f"đã gửi steer vào run {r['run_id']} (lần {r['steer_count']}, seq {r['seq']}) — theo dõi steer.ack: agent-hub runs follow {r['run_id']}")
    if getattr(a, "wait_ack", False):
        deadline = time.monotonic() + float(a.wait_ack)
        for msg in ctx.client().subscribe({"run_id": r["run_id"], "since_seq": r["seq"], "epoch": r["epoch"], "level": "summary"}):
            if msg.get("type") == "steer.ack":
                ctx.out(msg, f"steer.ack: {msg['payload'].get('text', '')[:80]}")
                return 0
            if "end" in msg or time.monotonic() > deadline or (msg.get("type") or "").startswith("run.") and msg.get("source") == "hub" and msg["type"] != "run.queued":
                break
        ctx.out({"ack": False}, "chưa thấy steer.ack (run có thể đã kết thúc trước khi tin được đưa vào)")
        return 1
    return 0


def cmd_takeover(ctx: Any) -> int:
    a = ctx.args
    if a.record:
        r = ctx.call("run.takeover_record", {"id": a.target, "exit_code": None})
        ctx.out(r, f"đã ghi run takeover {r.get('id')}")
        return 0
    info = ctx.call("run.takeover", {"id": a.target})
    if a.print_only or ctx.json:
        ctx.out(info, info["command"] + f"\n# khi xong: agent-hub takeover {info['run_id']} --record")
        return 0
    env = {k: v for k, v in os.environ.items() if k not in set(info.get("env_strip") or [])}
    print(f"[agent-hub] takeover run {info['run_id']} (session {info['session_id']}) trong {info['cwd']}", flush=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
    try:
        rc = subprocess.call(info["argv"], cwd=info["cwd"] if info.get("cwd_exists") else None, env=env)
    except OSError as e:
        print(f"lỗi: không chạy được claude: {e}")
        ctx.call("run.takeover_record", {"id": info["run_id"], "launched": False})
        return 2
    r = ctx.call("run.takeover_record", {"id": info["run_id"], "exit_code": rc, "started_at": started})
    ctx.out(r, f"đã ghi run takeover {r['id']} (kind=human_takeover, exit {rc})")
    return 0


def cmd_template(ctx: Any) -> int:
    from ..cli import _table

    a = ctx.args
    if a.action == "list":
        rows = ctx.call("template.list")
        ctx.out(rows, _table([[t["name"], t["model"], t["permission_mode"], ",".join(t["tools"] or []) or "(mặc định)", t["title"]] for t in rows], ["TEMPLATE", "MODEL", "PERMISSION", "TOOLS", "MÔ TẢ"]))
    else:
        t = ctx.call("template.get", {"name": a.name})
        ctx.out(t, f"# {t['name']} — {t.get('title')}\n\n" + json.dumps(t.get("config"), ensure_ascii=False, indent=2) + "\n\n" + str(t.get("soul") or ""))
    return 0


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    from ..guard.cli import HIDDEN

    HIDDEN[("run", "steer")] = "run-steer"
    HIDDEN[("run", "takeover")] = "takeover"

    t = sub.add_parser("top", help="màn htop: xem/điều khiển live mọi agent (TUI)", parents=[common])
    t.add_argument("--refresh", type=float, default=1.0, help="chu kỳ nạp ảnh chụp (giây, mặc định 1)")
    t.set_defaults(fn=cmd_top, target=None)
    at = sub.add_parser("attach", help="mở TUI attach thẳng vào run (hoặc run hiện tại của agent)", parents=[common])
    at.add_argument("target", help="id run hoặc id agent")
    at.set_defaults(fn=cmd_top, refresh=1.0)

    for name in ("steer", "run-steer"):
        s = sub.add_parser(name, help='gửi tin vào run đang chạy (FR-RT-06): agent-hub steer <run|agent> "<tin>"' if name == "steer" else "(= run steer) gửi tin vào run đang chạy", parents=[common])
        s.add_argument("target", help="id run (tiền tố được) hoặc id agent")
        s.add_argument("text")
        s.add_argument("--wait-ack", type=float, nargs="?", const=120.0, default=None, metavar="GIÂY", help="chờ steer.ack (mặc định 120 s)")
        s.set_defaults(fn=cmd_steer)

    tk = sub.add_parser("takeover", help="tiếp quản run đã dừng bằng `claude --resume` tương tác (FR-UI-10)", parents=[common])
    tk.add_argument("target", help="id run hoặc id agent (→ run gần nhất)")
    tk.add_argument("--print", dest="print_only", action="store_true", help="chỉ in lệnh, không chạy")
    tk.add_argument("--record", action="store_true", help="ghi nhận đã takeover thủ công (sau --print) và dọn file runtime")
    tk.set_defaults(fn=cmd_takeover)

    tp = sub.add_parser("template", help="template agent có sẵn (FR-AG-07)", parents=[common])
    ts = tp.add_subparsers(dest="action", required=True)
    ts.add_parser("list", parents=[common])
    sh = ts.add_parser("show", parents=[common])
    sh.add_argument("name")
    tp.set_defaults(fn=cmd_template)
