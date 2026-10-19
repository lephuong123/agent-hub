"""Hub demo cho giao diện web (chụp ảnh màn, thử tay) — daemon THẬT trên socket + FakeRuntime (không gọi `claude`, không tốn tiền).

    uv run python -m cfharness.web.demo --home /tmp/ahub-demo --port 8799

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


async def seed_manager(d) -> dict:
    """Trang Điều phối (M7): chỉ định điều phối viên + một cuộc trò chuyện web có thẻ hành động. Daemon chưa có `manager.*` thật → stub."""
    from datetime import datetime, timedelta, timezone

    from .. import templates
    from ..events import iso, make_event
    from . import manager_stub

    hub = d.hub
    stub = manager_stub.install(d)
    tpl = "manager" if "manager" in templates.names() else "researcher"
    cfg, soul = templates.render(tpl, "dieu-phoi", {"name": "Điều phối viên"})
    hub.agents.create(cfg, soul)

    async def call(method: str, params: dict) -> dict:
        resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp["result"]

    await call("manager.set", {"agent_id": "dieu-phoi"})
    r1 = await call("manager.chat", {"channel": "web", "text": "Tạo một agent nghiên cứu để tóm tắt README của repo cfharness, rồi giao việc đó cho nó. Xong thì báo tôi."})
    await hub.runs.wait(r1["run_id"], 30)
    now = datetime.now(timezone.utc)
    for k, (tool, target, summary) in enumerate((("agent_create", "agent:nghien-cuu", "Đã tạo agent nghien-cuu (mẫu researcher, model haiku, MCP tai-lieu)"),
                                  ("task_create", "task:" + (hub.tasks.list()[0]["id"] if hub.tasks.list() else "t_1"),
                                   "Đã giao task “Tóm tắt README” cho team docs"),
                                  ("run_start", "run:" + r1["run_id"], "Đã chạy thử agent nghien-cuu với README.md"))):
        hub.publish(make_event("manager.action", "dieu-phoi", {"tool": tool, "target": target, "summary": summary, "audit_id": 1}, run_id=r1["run_id"],
                               ts=now - timedelta(minutes=11, seconds=-10 * k)))
    r2 = await call("manager.chat", {"channel": "web", "conversation_id": r1["conversation_id"], "text": "Có việc gì đang kẹt cần tôi không?"})
    await hub.runs.wait(r2["run_id"], 30)
    # nội dung mẫu dễ đọc (FakeRuntime chỉ phát lại fixture tiếng Anh) + mốc thời gian quanh các thẻ hành động
    texts = {1: ("Xong. Tôi đã tạo agent nghien-cuu (mẫu researcher) và giao task “Tóm tắt README” cho team docs.\n"
                 "Bản tóm tắt 5 ý đã có trong task — tôi sẽ báo lại khi reviewer duyệt xong."),
             3: ("Có 1 việc cần anh: run của agent van-hanh đang chờ duyệt lệnh\n  rm -rf /srv/data/tmp-2025\n"
                 "Tôi không được phép duyệt thao tác phá huỷ — anh xem ở trang Duyệt. Ngoài ra 2 task đang chạy bình thường, chi phí hôm nay $0.42.")}
    offs = (12, 10, 4, 3)
    if stub is not None:
        msgs = stub.convs[r1["conversation_id"]]["messages"]
        for i, m in enumerate(msgs[:4]):
            m["ts"] = iso(now - timedelta(minutes=offs[i]))
            m["text"] = texts.get(i, m["text"])
        stub.last_tick = iso(now - timedelta(minutes=7))
        stub.next_tick = iso(now + timedelta(minutes=23))
    else:  # MGR-CORE thật: bảng manager_messages (chỉ trong demo — daemon demo chạy cùng tiến trình)
        conn = hub.store.conn
        ids = [r[0] for r in conn.execute("SELECT id FROM manager_messages WHERE conversation_id=? ORDER BY id", (r1["conversation_id"],))]
        for i, mid in enumerate(ids[:4]):
            conn.execute("UPDATE manager_messages SET ts=? WHERE id=?", (iso(now - timedelta(minutes=offs[i])), mid))
            if i in texts:
                conn.execute("UPDATE manager_messages SET text=? WHERE id=?", (texts[i], mid))
    return {"conversation": r1["conversation_id"], "manager_run": r2["run_id"]}


def seed_questions(hub, info: dict) -> dict:
    """Câu hỏi có lựa chọn (UX): một câu chờ bạn, một câu đang ở điều phối viên, một câu của điều phối viên trong chat, một câu đã trả lời."""
    get = hub.store.get_run
    q1 = hub.questions.open(get(info["live"]), "Test tests/test_api.py đỏ vì API giờ trả ngày dạng ISO 8601 thay vì dd/mm/yyyy. Sửa theo hướng nào?",
                            [{"label": "Sửa test theo định dạng mới (ISO 8601)", "description": "nhanh nhất, khớp chuẩn"},
                             {"label": "Giữ định dạng cũ, sửa lại API", "description": "client cũ không phải đổi"},
                             {"label": "Hỗ trợ cả hai, thêm cờ cấu hình"}])
    tasks = {t["title"]: t["id"] for t in hub.tasks.list()}
    tid = tasks.get("Kiểm tra link hỏng trong docs")
    r = dict(get(info["done"][0]))
    q2 = hub.questions.open(r, "Có 12 link hỏng trỏ tới wiki cũ (wiki.old.local). Thay bằng link wiki mới hay xoá hẳn?",
                            ["Thay bằng link wiki mới", "Xoá các link hỏng"], task_id=tid)
    mrun = dict(get(info["manager"]["manager_run"]))
    q3 = hub.questions.open(mrun, "Team docs đã dùng $4.6/$5 ngân sách tuần. Anh muốn xử lý thế nào?",
                            [{"label": "Tăng ngân sách lên $10/tuần"}, {"label": "Tạm dừng các task không gấp"}, {"label": "Giữ nguyên, báo lại khi hết"}],
                            source="manager", conversation_id=info["manager"]["conversation"])
    q4 = hub.questions.open(get(info["done"][1]), "Review theo chuẩn PEP 8 đầy đủ hay chỉ lỗi nghiêm trọng?", ["Đầy đủ", "Chỉ lỗi nghiêm trọng"])
    hub.questions.answer(q4["id"], keys=["2"], text="lần này chỉ cần lỗi chặn merge", by="user", channel="web")
    return {"open": q1["id"], "with_manager": q2["id"], "owner": q3["id"]}


async def main_async(a: argparse.Namespace) -> None:
    import uvicorn

    os.environ["CFHARNESS_HOME"] = str(Path(a.home).resolve())
    os.environ.pop("CFHARNESS_SOCKET", None)
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
    info["manager"] = await seed_manager(d)
    info["questions"] = seed_questions(d.hub, info)
    auth = WebAuth()
    app = create_app(paths, auth=auth, port=a.port)
    write_state(paths, host="127.0.0.1", port=a.port, key=auth.key)  # name mặc định cfharness.localhost
    print(f"SEED {info}", flush=True)
    from .security import WEB_HOST

    print(f"LOGIN {login_url(WEB_HOST, a.port, auth.mint())}", flush=True)
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
