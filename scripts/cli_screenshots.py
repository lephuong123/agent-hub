"""Ghi ảnh SVG các màn CLI chính (docs/dev-notes/cli-design.md → docs/screenshots/cli/).

    uv run python scripts/cli_screenshots.py [--out docs/screenshots/cli]

Dùng HUB tạm (thư mục tạm, `runtime.kind fake`) + daemon thật; KHÔNG đụng ~/.cfharness, KHÔNG gọi `claude`.
Màn approvals dùng một yêu cầu mẫu (FakeRuntime không sinh thao tác phá huỷ); các màn còn lại là dữ liệu thật của hub tạm.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile
import time
from pathlib import Path

WIDTH = 104


def mask(obj):
    """Che thư mục home (tên người dùng) trong dữ liệu trước khi vẽ ảnh."""
    home = str(Path.home())
    if isinstance(obj, str):
        s = obj.replace(home, "~").replace(home.replace("\\", "/"), "~")
        s = re.sub(r"-Users-[^-]+-", "-Users-USER-", s)  # đường dẫn đã mã hoá kiểu ~/.claude/projects/-Users-<tên>-…
        return re.sub(r"([\\/](?:Users|home)[\\/])[^\\/\s\"]+", r"\1USER", s)
    if isinstance(obj, dict):
        return {k: mask(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(v) for v in obj]
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/screenshots/cli")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="cfh-cli-shots-"))
    os.environ["CFHARNESS_HOME"] = str(tmp / "hub")
    os.environ.pop("CFHARNESS_SOCKET", None)

    import yaml

    from cfharness import __version__
    from cfharness.daemon import spawn_background
    from cfharness.paths import HubPaths
    from cfharness.rpc import RpcClient, RpcError
    from cfharness.ui import theme as th
    from cfharness.ui import views as V

    paths = HubPaths.resolve()
    paths.ensure()
    paths.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake", "fake": {"line_delay_s": 0.0}}}), encoding="utf-8")
    spawn_background(paths)
    c = RpcClient(paths.socket, timeout=60)
    mode = th.Mode(styled=True, color=True, unicode=True, width=WIDTH)

    def shot(name: str, title: str, renderable) -> None:
        con = th.console(io.StringIO(), mode, record=True, width=WIDTH)
        con.print(renderable, overflow="fold")
        con.save_svg(str(out / f"{name}.svg"), title=title)
        print(out / f"{name}.svg")

    try:
        for aid, role, model in (("coder", "Viết và sửa code", "sonnet"), ("researcher", "Tìm hiểu, tóm tắt tài liệu", "haiku"),
                                 ("reviewer", "Review PR", "opus")):
            c.call("agent.create", {"config": {"id": aid, "name": aid, "role": role, "model": model}})
        prompts = [("coder", "Sửa lỗi phân trang ở API /runs và thêm test"), ("researcher", "Tóm tắt README và docs/BRD")]
        ids = [c.call("run.start", {"agent": ag, "prompt": p})["id"] for ag, p in prompts]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(c.call("run.get", {"id": i})["status"] in ("queued", "running") for i in ids):
            time.sleep(0.2)
        c.call("agent.pause", {"id": "reviewer"})

        shot("01-tong-quan", "cfharness", V.overview(__version__, mode, width=WIDTH))
        st = c.call("hub.status")
        try:
            st["compat"] = c.call("hub.compat")
        except RpcError:
            pass
        shot("02-daemon-status", "cfharness daemon status", V.daemon_status(mask(st), mode, width=WIDTH))
        shot("03-agent-list", "cfharness agent list", V.agent_list(mask(c.call("agent.list", {})), mode, width=WIDTH))
        shot("04-runs-list", "cfharness runs list", V.runs_list(mask(c.call("run.list", {"limit": 20})), mode, width=WIDTH))

        run = mask(c.call("run.get", {"id": ids[0], "events": True}))
        lines = [V.run_started(run, mode)]
        state: dict = {}
        for ev in run.get("events") or []:
            ev = {**ev, "run_id": run["id"], "agent_id": run["agent_id"]}
            if ev["type"] in V.TERMINAL_EVENTS:
                ev["source"] = "hub"
            row = V.event_line(ev, state, mode)
            if row is not None:
                lines.append(row)
        lines.append(th.hint(f"chi tiết: `cfharness runs show {run['id']}`", mode))
        from rich.console import Group

        shot("05-run-follow", 'cfharness run coder "…" -f', Group(*lines))

        sample = [{"id": "ap_7f3a21", "status": "waiting_human", "agent_id": "coder", "run_id": ids[0], "opened_at": None,
                   "matched_rule": "git.push_force", "preview": "git push --force origin main"},
                  {"id": "ap_19bc04", "status": "open", "agent_id": "researcher", "run_id": ids[1], "opened_at": None,
                   "matched_rule": "fs.rm_recursive", "preview": "rm -rf ./build ./dist"}]
        waits = {"ap_7f3a21": "12m05s", "ap_19bc04": "0m41s"}
        try:
            c.call("agent.get", {"id": "khong-co"})
            err = None
        except RpcError as e:
            err = th.error(e.message, mode, fix=th.fix_for(e.code, e.message, e.data))
        estop_on = V.estop({"on": True, "reason": "deploy production đang chạy", "stop_running": True}, mode)
        shot("06-approvals-estop-loi", "cfharness approvals · estop · lỗi",
             Group(V.approvals(sample, mode, width=WIDTH, ago=lambda r: waits[r["id"]]), V.BLANK, estop_on, V.BLANK, err or V.BLANK))
    finally:
        try:
            c.call("hub.shutdown")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
