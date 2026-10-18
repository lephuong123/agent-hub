"""Test giao diện bằng Pilot + lưu ảnh chụp SVG vào demo/screenshots/."""

from pathlib import Path

from textual.widgets import DataTable, Input

from agent_hub_demo.app import AgentFormScreen, AgentHubApp, AttachScreen, HtopScreen, LearningsScreen, Transcript
from agent_hub_demo.simulator import FakeHub

SHOTS = Path(__file__).resolve().parents[1] / "screenshots"
SIZE = (150, 42)


def shot(app, name: str) -> None:
    SHOTS.mkdir(exist_ok=True)
    app.save_screenshot(name, path=str(SHOTS))


async def test_luong_chinh():
    app = AgentHubApp(FakeHub(live=False))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, HtopScreen)
        table = app.screen.query_one("#agents", DataTable)
        assert table.row_count == 5
        shot(app, "01-htop.svg")

        # Enter → attach researcher (hàng đầu), có replay
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, AttachScreen) and app.screen.agent_id == "a1"
        assert len(app.screen.query_one(Transcript).children) > 5
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HtopScreen)

        # attach coder đang chờ duyệt, steer rồi duyệt
        await pilot.press("down", "down", "enter")
        await pilot.pause(0.3)
        assert app.screen.agent_id == "a3"
        await pilot.press("s")
        await pilot.press(*"chi xoa build, giu dist")
        await pilot.press("enter")
        await pilot.pause(0.3)
        shot(app, "02-attach.svg")
        await pilot.press("a")
        await pilot.pause(0.3)
        assert app.hub.agg.agents["a3"].pending_request is None
        await pilot.press("escape")
        await pilot.pause()

        # learnings: duyệt 1 proposal đang chờ
        await pilot.press("l")
        await pilot.pause(0.3)
        assert isinstance(app.screen, LearningsScreen)
        pending_before = app.hub.agg.pending_proposals
        assert pending_before >= 1
        shot(app, "03-learnings.svg")
        await pilot.press("a")
        await pilot.pause(0.3)
        assert app.hub.agg.pending_proposals == pending_before - 1
        await pilot.press("escape")
        await pilot.pause()

        # tạo agent mới qua form
        await pilot.press("n")
        await pilot.pause(0.3)
        assert isinstance(app.screen, AgentFormScreen)
        app.screen.query_one("#f-name", Input).value = "thu-ky"
        app.screen.query_one("#f-role", Input).value = "Tổng hợp công văn đến hằng ngày"
        shot(app, "04-form-tao-agent.svg")
        await pilot.click("#save")
        await pilot.pause(0.3)
        assert isinstance(app.screen, HtopScreen)
        assert any(a.name == "thu-ky" for a in app.hub.agg.agents.values())

        # lọc team bằng t
        await pilot.press("t")
        await pilot.pause(0.2)
        assert app.screen.query_one("#agents", DataTable).row_count == 3  # team Nghiên cứu (2 cũ + thu-ky mới)


async def test_attach_writer_va_xoa():
    app = AgentHubApp(FakeHub(live=False))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("down", "enter")
        await pilot.pause(0.3)
        assert app.screen.agent_id == "a2"
        shot(app, "05-attach-writer.svg")
        await pilot.press("escape")
        await pilot.press("delete")
        await pilot.pause(0.2)
        await pilot.press("y")
        await pilot.pause(0.2)
        assert "a2" not in app.hub.agg.agents
