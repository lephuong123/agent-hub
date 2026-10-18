"""Hub demo cho giao diện web (chụp ảnh màn, thử tay) — daemon THẬT trên socket + FakeRuntime (không gọi `claude`, không tốn tiền).

    uv run python -m agent_hub.web.demo --home /tmp/ahub-demo --port 8799

Dựng sẵn: agent từ template + MCP giả, team + task (DAG), vài run đã xong, một run đang chạy (giữ), một thao tác phá huỷ
đang chờ người duyệt (waiting_human). In link đăng nhập một lần ra stdout (dòng `LOGIN <url>`), ghi `HUB_HOME/auth/web.json`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "claude-hub-isolated-run.jsonl"


class DemoRuntime:
    """FakeRuntime: run có prompt chứa '[hold]' được giữ (đang chạy) tới khi dừng; còn lại phát lại fixture."""

    def __new__(cls, fixture: Path, delay: float):  # type: ignore[misc]
        from ..runtime import FakeRuntime

        class _RT(FakeRuntime):
            async def start(self, spec):  # type: ignore[override]
                self.hold = "[hold]" in (getattr(spec, "prompt", "") or "")
                return await super().start(spec)

        return _RT(fixture if fixture.exists() else None, line_delay_s=delay)


async def seed(hub) -> dict:
    a = hub.agents
    from .. import templates

    for aid, tpl, name in (("nghien-cuu", "researcher", "Trợ lý nghiên cứu"), ("lap-trinh", "coder", "Lập trình viên"),
                           ("review", "reviewer", "Người review"), ("van-hanh", "ops", "Vận hành")):
        cfg, soul = templates.render(tpl, aid, {"name": name})
        a.create(cfg, soul)
    hub.mcp.add({"name": "github", "transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                 "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}, "trust": "trusted", "description": "Issue, PR, code search"})
    hub.mcp.add({"name": "tai-lieu", "transport": "http", "url": "http://127.0.0.1:9/mcp", "description": "Kho tài liệu nội bộ (chỉ đọc)"})
    a.update("nghien-cuu", {"mcp_servers": ["tai-lieu"]})
    hub.teams.create({"id": "docs", "name": "Nhóm tài liệu", "charter": "Viết và giữ tài liệu cài đặt luôn đúng với mã nguồn."})
    hub.teams.member_set("docs", "lap-trinh", "worker", 1)
    hub.teams.member_set("docs", "review", "reviewer", 1)
    done = []
    for agent, prompt in (("nghien-cuu", "Tóm tắt README.md thành 5 gạch đầu dòng"), ("review", "Review thay đổi gần nhất trong src/"),
                          ("lap-trinh", "Sửa lỗi chính tả trong docs/")):
        r = hub.runs.submit(agent, prompt)
        await hub.runs.wait(r["id"], 30)
        done.append(r["id"])
    t1 = hub.tasks.create({"title": "Viết hướng dẫn cài đặt", "body": "Từ README + docs/, cho người mới.", "assignee": "lap-trinh"})
    hub.tasks.create({"title": "Review hướng dẫn cài đặt", "assignee": "review", "parents": [t1["id"]]})
    hub.tasks.create({"title": "Kiểm tra link hỏng trong docs", "assignee": "nghien-cuu"})
    live = hub.runs.submit("lap-trinh", "[hold] Chạy test và sửa các test đỏ trong tests/")
    risky = hub.runs.submit("van-hanh", "[hold] Dọn dữ liệu tạm cũ trên máy chủ")
    for _ in range(200):
        if all((hub.store.get_run(x["id"]) or {}).get("status") == "running" for x in (live, risky)):
            break
        await asyncio.sleep(0.05)
    ap = hub.approvals.open(hub.store.get_run(risky["id"]), tool_name="Bash", tool_input={"command": "rm -rf /srv/data/tmp-2025"}, tool_use_id="toolu_demo",
                            fingerprint="fp-demo", verdict={"klass": "fs_delete", "rule": "rm_recursive_outside_run", "verdict": "destructive",
                                                            "reason": "xoá đệ quy ngoài phạm vi run"}, channel="hook")
    hub.approvals.to_waiting_human(ap["id"])
    return {"done": done, "live": live["id"], "risky": risky["id"], "approval": ap["id"]}


async def main_async(a: argparse.Namespace) -> None:
    import uvicorn

    os.environ["AGENT_HUB_HOME"] = str(Path(a.home).resolve())
    os.environ.pop("AGENT_HUB_SOCKET", None)
    from ..daemon import Daemon
    from ..paths import HubPaths
    from .app import create_app
    from .security import WebAuth, login_url, write_state

    paths = HubPaths.resolve()
    paths.ensure()
    d = Daemon(paths, runtime=DemoRuntime(FIXTURE, a.delay))
    task = asyncio.create_task(d.serve())
    while not paths.socket.exists():
        await asyncio.sleep(0.05)
    info = await seed(d.hub)
    auth = WebAuth()
    app = create_app(paths, auth=auth, port=a.port)
    write_state(paths, host="127.0.0.1", port=a.port, key=auth.key)
    print(f"SEED {info}", flush=True)
    print(f"LOGIN {login_url('127.0.0.1', a.port, auth.mint())}", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.port, log_level="warning", access_log=False))
    try:
        await server.serve()
    finally:
        d.stopping.set()
        await task


def main() -> None:
    p = argparse.ArgumentParser(description="hub demo cho giao diện web (FakeRuntime)")
    p.add_argument("--home", required=True)
    p.add_argument("--port", type=int, default=8799)
    p.add_argument("--delay", type=float, default=0.05)
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
