"""Màn xem agent kiểu Claude Code (`tui/agent_view.py`) — Textual Pilot + daemon IN-PROCESS (socket thật, FakeRuntime). Không gọi `claude`.

Kiểm: replay DB → live (prompt, lời agent, dòng tool `⎿`), click tool mở rộng, tool đang chạy → cập nhật tại chỗ, tool của subagent thụt dưới
`● Agent(…)`, hộp duyệt + `a`, `x` + y dừng, esc quay lại, chữ agent là DỮ LIỆU (ANSI/markup không được diễn giải), kéo chọn → copy
(clipboard thật được thay bằng giả). Ảnh SVG: `CFHARNESS_SCREENSHOTS=<thư mục>`.
"""

from __future__ import annotations


from cfharness.events import make_event
from cfharness.manager import attachments as A
from cfharness.tui.agent_view import AgentViewScreen, AvItem, open_agent_view
from cfharness.tui.app import ConfirmScreen, PromptScreen, TopApp
from test_manager_repl import EVIL
from test_tui import SIZE, _open_approval, daemon, held_daemon, mk_agents, shot, until  # noqa: F401  (fixture)


def items(scr: AgentViewScreen) -> list[AvItem]:
    return [w for w in scr.tlog.children if isinstance(w, AvItem)]


def plain(w) -> str:
    r = w.render()
    return str(getattr(r, "plain", r))


def view_text(scr: AgentViewScreen) -> str:
    return "\n".join(plain(w) for w in items(scr))


def item_with(scr: AgentViewScreen, needle: str) -> AvItem | None:
    return next((w for w in items(scr) if needle in plain(w)), None)


def no_native_clipboard(monkeypatch) -> list[str]:
    got: list[str] = []
    monkeypatch.setattr(A, "clipboard_set", lambda text: got.append(text) or True)
    return got


async def open_view(app, pilot, ref: str) -> AgentViewScreen:
    await open_agent_view(app, ref)
    assert await until(lambda: isinstance(app.screen, AgentViewScreen) and app.screen.loaded, pilot=pilot)
    return app.screen


async def held_run(ctl, prompt: str = "dọn dẹp thư mục build"):
    hub = ctl.hub
    mk_agents(hub, 1)
    run = hub.runs.submit("ag0", prompt)
    assert await until(lambda: (ctl.runs(run["id"]) or {}).get("status") == "running")
    assert await until(lambda: "tool.complete" in ctl.types(run["id"]))
    return run


# =========================================================================== replay + live
async def test_replay_then_live_renders_cc_look_and_click_expands(daemon, hub_home):
    hub = daemon.hub
    mk_agents(hub, 1)
    app = TopApp(hub_home.socket, refresh_s=0.3)
    async with app.run_test(size=SIZE) as pilot:
        run = hub.runs.submit("ag0", "explain the repo layout")
        await until(lambda: "tool.start" in daemon.types(run["id"]), pilot=pilot)
        await until(lambda: (app.model.rows.get("ag0") or {"current_run_id": None})["current_run_id"] == run["id"], pilot=pilot)
        scr = await open_view(app, pilot, "ag0")  # agent id → run đang chạy
        assert scr.run_id == run["id"] and scr.replayed > 0
        await hub.runs.wait(run["id"], 20)
        assert await until(lambda: "⎿  Xong" in view_text(scr), pilot=pilot), view_text(scr)
        text = view_text(scr)
        assert "> explain the repo layout" in text
        assert "● Bash(" in text and "\n  ⎿  " in text  # dòng tool + gutter kết quả
        assert any(plain(w).startswith("● ") and w.meta.get("kind") == "text" for w in items(scr))  # lời agent
        assert "đang chạy…" not in text.lower().replace("đang chạy… (", "")
        persisted = {e.seq for e in hub.store.query_events(run_id=run["id"])}
        assert persisted <= {k[1] for k in scr.seen}  # không mất event, không trùng
        head = plain(scr.query_one("#av-title"))
        assert "Ag 0" in head and run["id"][:10] in head and "xong" in head
        assert "tool" in plain(scr.query_one("#av-stats")) and not scr.query_one("#av-stop").display
        # click dòng tool → mở rộng (input JSON), click lần nữa → thu lại
        w = next(w for w in items(scr) if w.meta.get("kind") == "tool")
        before = plain(w)
        scr.tlog.scroll_to_widget(w, animate=False)
        await pilot.pause(0.1)
        await pilot.click(w, offset=(2, 0))
        assert await until(lambda: w.meta.get("expanded") and "input:" in plain(w), pilot=pilot)
        shot(app, "av-01-xong.svg")
        await pilot.click(w, offset=(2, 0))
        assert await until(lambda: plain(w) == before, pilot=pilot)
        # ctrl+o: mở rộng mọi tool
        await pilot.press("ctrl+o")
        assert all("input:" in plain(x) for x in items(scr) if x.meta.get("kind") == "tool" and x.meta.get("input"))
        await pilot.press("escape")
        assert await until(lambda: app.screen is app.htop, pilot=pilot)


async def test_running_tool_updates_in_place_subagent_nested(held_daemon, hub_home):
    ctl = held_daemon
    run = await held_run(ctl)
    rid, hub = run["id"], ctl.hub
    app = TopApp(hub_home.socket, refresh_s=0.3)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_view(app, pilot, rid)
        assert plain(scr.query_one("#av-title")).endswith("đang chạy") and scr.query_one("#av-stop").display
        assert scr.query_one("#av-spinner").display
        hub.publish(make_event("reasoning.delta", "ag0", {"text": "Cần xem Makefile.\nRồi tìm cấu hình.\nSau đó dọn.\nDòng 4.\nDòng 5."}, run_id=rid))
        assert await until(lambda: item_with(scr, "✻ Đang nghĩ…") is not None, pilot=pilot)
        th = item_with(scr, "✻ Đang nghĩ…")
        assert "Dòng 5." not in plain(th) and "+2 dòng" in plain(th)
        scr.item_clicked(th)  # click → mở hết suy nghĩ
        assert "Dòng 5." in plain(th)
        hub.publish(make_event("message.delta", "ag0", {"text": "Mình sẽ đọc "}, run_id=rid))
        hub.publish(make_event("message.delta", "ag0", {"text": "Makefile trước."}, run_id=rid))
        assert await until(lambda: item_with(scr, "Mình sẽ đọc Makefile trước.▌") is not None, pilot=pilot)
        live = item_with(scr, "Mình sẽ đọc Makefile")
        hub.publish(make_event("message.complete", "ag0", {"text": "Mình sẽ đọc Makefile trước."}, run_id=rid))
        assert await until(lambda: plain(live) == "● Mình sẽ đọc Makefile trước.", pilot=pilot)
        hub.publish(make_event("tool.start", "ag0", {"tool_id": "tA", "name": "Read", "input": {"file_path": "Makefile"},
                                                     "args_preview": "Makefile"}, run_id=rid))
        assert await until(lambda: item_with(scr, "Read(Makefile)") is not None, pilot=pilot)
        tw = item_with(scr, "Read(Makefile)")
        assert "Đang chạy…" in plain(tw)
        n = len(items(scr))
        hub.publish(make_event("tool.complete", "ag0", {"tool_id": "tA", "name": "Read", "duration_s": 0.42, "is_error": False,
                                                        "result_preview": "all: build\nbuild:\n\tgo build\ntest:\n\tgo test"}, run_id=rid))
        assert await until(lambda: "Đang chạy…" not in plain(tw), pilot=pilot)
        assert "● Read(Makefile)  0.4s" in plain(tw) and "⎿  all: build" in plain(tw) and "+2 dòng (click để mở)" in plain(tw)
        assert len(items(scr)) == n  # cập nhật tại chỗ, không thêm mục
        # subagent: Task → `● Agent(mô tả)`, tool của nó thụt vào ngay dưới
        hub.publish(make_event("tool.start", "ag0", {"tool_id": "tT", "name": "Task", "input": {"description": "Tìm file cấu hình"},
                                                     "args_preview": "Tìm file cấu hình"}, run_id=rid))
        hub.publish(make_event("subagent.spawn", "ag0", {"parent_tool_use_id": "tT", "type": "Explore", "description": "Tìm file cấu hình"},
                               run_id=rid))
        hub.publish(make_event("tool.start", "ag0", {"tool_id": "tS", "name": "Grep", "args_preview": "config"}, run_id=rid, subagent_id="tT"))
        hub.publish(make_event("message.delta", "ag0", {"text": "chữ phụ không hiện"}, run_id=rid, subagent_id="tT"))
        hub.publish(make_event("tool.complete", "ag0", {"tool_id": "tS", "name": "Grep", "duration_s": 0.1, "result_preview": "cfg.yaml"},
                               run_id=rid, subagent_id="tT"))
        assert await until(lambda: item_with(scr, "Grep(config)") is not None and plain(item_with(scr, "Grep(config)")).startswith("    ") and "cfg.yaml" in plain(item_with(scr, "Grep(config)")), pilot=pilot)
        kids = items(scr)
        hdr = item_with(scr, "Agent(Tìm file cấu hình)")
        sub = item_with(scr, "Grep(config)")
        assert hdr is not None and "Đang chạy… (1 tool)" in plain(hdr)
        assert kids.index(sub) == kids.index(hdr) + 1  # thụt ngay dưới dòng Agent
        shot(app, "av-02-dang-chay.svg")
        hub.publish(make_event("tool.complete", "ag0", {"tool_id": "tT", "name": "Task", "duration_s": 3.2, "result_preview": "Thấy cfg.yaml"},
                               run_id=rid))
        assert await until(lambda: "Xong (1 tool · 3.2s)" in plain(hdr), pilot=pilot)
        assert "chữ phụ không hiện" not in view_text(scr)  # delta của subagent không hiện
        ctl.rt.release()


async def test_approval_box_and_approve(held_daemon, hub_home):
    ctl = held_daemon
    run = await held_run(ctl)
    hub = ctl.hub
    app = TopApp(hub_home.socket, refresh_s=0.3)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_view(app, pilot, run["id"])
        ap = await _open_approval(hub, run)
        assert await until(lambda: item_with(scr, "Cần duyệt") is not None, pilot=pilot)
        box = item_with(scr, "Cần duyệt")
        txt = plain(box)
        assert "Bash" in txt and "rm -rf /srv/data" in txt and "xoá đệ quy" in txt and "a cho phép · d từ chối" in txt
        assert box.has_class("av-perm")
        assert await until(lambda: "chờ duyệt" in plain(scr.query_one("#av-title")), pilot=pilot)
        shot(app, "av-03-duyet.svg")
        await pilot.press("a")
        assert await until(lambda: hub.approvals.get(ap["id"])["status"] == "allowed", pilot=pilot)
        assert await until(lambda: "✓ Đã cho phép" in plain(box) and box.has_class("av-perm-done"), pilot=pilot)
        # deny → hỏi ghi chú
        ap2 = await _open_approval(hub, run)
        assert await until(lambda: len([w for w in items(scr) if w.meta.get("kind") == "perm"]) == 2, pilot=pilot)
        await pilot.press("d")
        assert await until(lambda: isinstance(app.screen, PromptScreen), pilot=pilot)
        await pilot.press(*"dung thung rac")
        await pilot.press("enter")
        assert await until(lambda: hub.approvals.get(ap2["id"])["status"] == "denied", pilot=pilot)
        assert hub.approvals.get(ap2["id"])["note"] == "dung thung rac"
        ctl.rt.release()


async def test_stop_with_confirm_then_esc_back(held_daemon, hub_home):
    ctl = held_daemon
    run = await held_run(ctl)
    app = TopApp(hub_home.socket, refresh_s=0.3)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_view(app, pilot, "ag0")
        await pilot.press("x")
        assert await until(lambda: isinstance(app.screen, ConfirmScreen), pilot=pilot)
        await pilot.press("y")
        final = await ctl.hub.runs.wait(run["id"], 20)
        assert final["outcome"] == "cancelled"
        assert await until(lambda: "đã dừng" in plain(scr.query_one("#av-title")) and "⚠ Đã dừng · " in view_text(scr), pilot=pilot)
        assert not scr.query_one("#av-stop").display
        shot(app, "av-04-da-dung.svg")
        await pilot.press("escape")
        assert await until(lambda: app.screen is app.htop, pilot=pilot)


async def test_steer_line(held_daemon, hub_home):
    ctl = held_daemon
    run = await held_run(ctl)
    app = TopApp(hub_home.socket, refresh_s=0.3)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_view(app, pilot, run["id"])
        await pilot.press("s")
        await pilot.press(*"focus on docs")
        await pilot.press("enter")
        assert await until(lambda: "steer.ack" in ctl.types(run["id"]), pilot=pilot)
        assert await until(lambda: "> focus on docs" in view_text(scr) and "agent đã nhận chỉ đạo" in view_text(scr), pilot=pilot)
        ctl.rt.release()


async def test_agent_text_is_data_and_drag_select_copies(held_daemon, hub_home, monkeypatch):
    native = no_native_clipboard(monkeypatch)
    ctl = held_daemon
    run = await held_run(ctl)
    rid, hub = run["id"], ctl.hub
    app = TopApp(hub_home.socket, refresh_s=0.3)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_view(app, pilot, rid)
        hub.publish(make_event("message.complete", "ag0", {"text": "Kết quả: " + EVIL}, run_id=rid))
        hub.publish(make_event("tool.start", "ag0", {"tool_id": "tE", "name": "Bash", "args_preview": EVIL, "input": {"command": EVIL}}, run_id=rid))
        hub.publish(make_event("tool.complete", "ag0", {"tool_id": "tE", "name": "Bash", "result_preview": EVIL}, run_id=rid))
        assert await until(lambda: item_with(scr, "Kết quả:") is not None and "Đang chạy…" not in plain(item_with(scr, "Bash(A")), pilot=pilot)
        tw = item_with(scr, "Bash(A")
        scr.tlog.scroll_to_widget(tw, animate=False)
        await pilot.pause(0.1)
        await pilot.click(tw, offset=(2, 0))
        assert await until(lambda: tw.meta.get("expanded"), pilot=pilot)
        for w in (item_with(scr, "Kết quả:"), tw):
            r = w.render()
            s = plain(w)
            assert "\x1b" not in s and "\x07" not in s and "\x9b" not in s and "‮" not in s and "\r" not in s
            assert "[link=http://x]y[/link]" in s and "[bold]B[/bold]" in s  # markup hiện nguyên văn
            spans = getattr(r, "spans", None) or []
            assert not any("link" in str(getattr(sp, "style", "")) for sp in spans)  # không có link OSC 8
        # kéo chọn → copy
        w = item_with(scr, "Kết quả:")
        scr.tlog.scroll_to_widget(w, animate=False)
        await pilot.pause(0.1)
        col = plain(w).index("Kết")
        await pilot.mouse_down(w, offset=(col, 0))
        await pilot.hover(w, offset=(col + 8, 0))
        await pilot.mouse_up(w, offset=(col + 8, 0))
        assert await until(lambda: native and native[-1].startswith("Kết quả"), pilot=pilot)
        assert app.clipboard == native[-1]
        assert "Đã copy" in " ".join(str(n.message) for n in app._notifications)
        assert app.screen is scr  # kéo chọn không phải click
        ctl.rt.release()


async def test_open_agent_view_unknown_ref(daemon, hub_home):
    app = TopApp(hub_home.socket)
    async with app.run_test(size=SIZE) as pilot:
        await open_agent_view(app, "khong-co")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, AgentViewScreen)
        assert "không tìm thấy" in " ".join(str(n.message) for n in app._notifications)


def test_end_note_format():
    scr = AgentViewScreen("a", "r_1")
    assert scr._end_render({"outcome": "completed", "duration_ms": 12_000, "turns": 5, "cost": 0.0123}).plain == "  ⎿  Xong · 12s · 5 lượt · $0.012"
    t = scr._end_render({"outcome": "failed", "duration_ms": 3000, "error": "boom"}).plain
    assert t.startswith("✗ Lỗi · 3s") and "boom" in t
