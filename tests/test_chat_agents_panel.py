"""`cfharness chat`: bảng agent đang chạy + thẻ duyệt/câu hỏi của MỌI agent (manager/agentsview.py + repl.py).

Phần thuần: `AgentMonitor` với snapshot/event giả nhiều agent, vẽ bảng ở nhiều độ rộng, thẻ, định tuyến `routed_to`.
Phần daemon: IN-PROCESS trên socket thật + ScriptedRuntime (giữ run worker ở `running`), guard thật mở approval, lệnh
`/approve` `/deny` `/always` `/answer` `/stop` `/agents` với xác nhận y/N giả. Không gọi `claude`.
`CFHARNESS_SCREENSHOTS=docs/screenshots/m2` → ghi lại ảnh SVG của bảng + thẻ.
"""

from __future__ import annotations

import asyncio
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text

from cfharness.daemon import Daemon
from cfharness.events import make_event
from cfharness.manager import agentsview as G
from cfharness.manager import chatview as V
from cfharness.manager import repl
from cfharness.tui.client import AsyncHubClient
from m3_fake import ScriptedRuntime
from m7_fake import MgrScript, wait_status

EVIL = "\x1b]8;;http://evil.example\x07bấm\x1b]8;;\x07 \x1b[31mĐỎ\x1b[0m \x1b]0;pwned\x07 ‮gnp.exe [bold]B[/bold] \x9b2J\rghi đè\x07"
BAD = ("\x1b]", "http://evil", "pwned", "‮", "\x9b", "\x07", "\x1b[31mĐỎ", "\r")


def iso(sec_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=sec_ago)).isoformat().replace("+00:00", "Z")


def agent(aid: str, status: str = "working", **kw) -> dict:
    d = {"id": aid, "name": kw.pop("name", aid), "status": status, "task": kw.pop("task", f"việc của {aid}"), "run_status": "running" if status == "working" else None,
         "current_run_id": f"r_{aid}" if status in ("working", "waiting", "waiting_human") else None, "run_started_at": iso(75),
         "last_tool": "Bash", "last_tool_at": iso(3), "run_tokens": 12345, "run_cost_usd": 0.0421, "tokens_today": 0, "cost_today_usd": 0.0,
         "waiting_human": None, "last_run_id": None}
    d.update(kw)
    return d


def snap(agents: list[dict], approvals: list[dict] | None = None, seq: int = 10) -> dict:
    return {"hub": {}, "agents": agents, "approvals": approvals or [], "epoch": "e1", "last_seq": seq}


def ap(aid: str, agent_id: str, **kw) -> dict:
    d = {"id": aid, "agent_id": agent_id, "run_id": f"r_{agent_id}", "tool_name": "Bash", "tool_input": {"command": "rm -rf /srv/data/tmp"},
         "preview": "rm -rf /srv/data/tmp", "status": "open", "opened_at": iso(12), "reason": "xoá đệ quy ngoài phạm vi run",
         "risk_level": "destructive", "destructive_class": "fs_delete", "matched_rule": "rm_recursive_outside_run", "always_offer": None}
    d.update(kw)
    return d


def render(r, width: int = 120, color: bool = False) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=color, color_system="truecolor" if color else None, width=width, markup=False, highlight=False,
            emoji=False).print(r)
    return buf.getvalue()


def team() -> G.AgentMonitor:
    m = G.AgentMonitor("boss")
    m.load_snapshot(snap([
        agent("boss", task="điều phối"),
        agent("nghien-cuu", name="Nghiên cứu", task="Tóm tắt README của repo X và các issue mở trong tuần"),
        agent("lap-trinh", task="Chạy test và sửa các test đỏ trong tests/"),
        agent("van-hanh", "waiting", task="Dọn dữ liệu tạm cũ trên máy chủ"),
        agent("cho-slot", "working", run_status="queued", task="xếp hàng"),
        agent("ranh", "idle", task=None),
    ], [ap("ap_1", "van-hanh")]))
    return m


# =========================================================================== thuần: bảng
def test_summary_line_counts_and_hidden_when_nothing():
    m = team()
    s = G.summary_line(m, 200).plain
    assert "⚠ 1 chờ bạn duyệt" in s and "● 3 agent đang chạy" in s and "◌ 1 chờ slot" in s and "(↓ xem)" in s
    assert "boss" not in s
    assert "(↓/Esc đóng)" in G.summary_line(m, 200, expanded=True).plain
    idle = G.AgentMonitor("boss")
    idle.load_snapshot(snap([agent("boss"), agent("ranh", "idle")]))
    assert G.summary_line(idle, 120) is None and G.panel_lines(idle, 120, 40, expanded=True) == []


@pytest.mark.parametrize("width", [20, 34, 48, 64, 80, 120, 200])
def test_panel_width_safe_every_width(width):
    m = team()
    m.model.rows["lap-trinh"].data["name"] = "Lập trình 🚀 viên rất rất dài tên"
    m.model.rows["nghien-cuu"].data["task"] = "日本語のタスク説明がとても長い " * 5
    lines = G.panel_lines(m, width, 40, expanded=True)
    assert len(lines) == 1 + 4  # dòng gọn + 4 agent (không boss, không agent rảnh)
    for ln in lines:
        assert ln.cell_len <= width, (width, ln.plain, ln.cell_len)
    joined = "\n".join(x.plain for x in lines)
    assert "Nghi" in joined
    if width >= 64:
        assert "⚙ Bash" in joined and "$0.042" in joined and "12k" in joined and "1m1" in joined
    if width < 48:
        assert "$0.042" not in joined


def test_panel_states_and_order():
    m = team()
    rows = G.panel_lines(m, 140, 40, expanded=True)[1:]
    # chờ duyệt trước, rồi đang chạy, rồi chờ slot
    assert rows[0].plain.lstrip().startswith(V.AGENT_STATE["approval"][0]) and "van-hanh" in rows[0].plain
    assert "duyệt: rm -rf /srv/data/tmp" in rows[0].plain
    assert rows[-1].plain.lstrip().startswith("◌") and "cho-slot" in rows[-1].plain
    # waiting_human → ⏸ đỏ
    m.model.rows["van-hanh"].data["status"] = "waiting_human"
    assert G.panel_lines(m, 140, 40, expanded=True)[1].plain.lstrip().startswith("⏸")


def test_panel_caps_rows_to_height():
    m = G.AgentMonitor("boss")
    m.load_snapshot(snap([agent(f"w{i:02d}") for i in range(20)]))
    lines = G.panel_lines(m, 100, 14, expanded=True)  # 14 dòng terminal → tối đa 6 dòng agent
    assert len(lines) == 1 + 6 and "+15 nữa" in lines[-1].plain and "/agents" in lines[-1].plain
    big = G.panel_lines(m, 100, 200, expanded=True)
    assert len(big) == 1 + G.MAX_PANEL_ROWS and "+9 nữa" in big[-1].plain
    assert len(G.panel_lines(m, 100, 14, expanded=False)) == 1


def test_error_rows_only_for_errors_seen_this_session():
    m = G.AgentMonitor("boss")
    m.load_snapshot(snap([agent("cu", "error"), agent("moi", "working")]))
    assert [r.id for _, r in m.active()] == ["moi"]  # lỗi cũ từ trước phiên: không hiện mãi
    m.apply({"type": "run.failed", "agent_id": "moi", "seq": 11, "epoch": "e1", "payload": {"error": "hết ngân sách"}})
    m.model.rows["moi"].data["status"] = "error"
    act = m.active()
    assert act[0][0] == "error" and "hết ngân sách" in G.agent_row(m, "error", act[0][1], 100).plain
    m.apply({"type": "run.started", "agent_id": "moi", "seq": 12, "epoch": "e1", "payload": {}})
    assert m.active()[0][0] == "running"


def test_agent_text_is_data_in_panel_and_table():
    m = G.AgentMonitor("boss")
    m.load_snapshot(snap([agent("x", name=EVIL, task=EVIL, last_tool=EVIL)], [ap("ap_e", "x", preview=EVIL)]))
    m.apply({"type": "tool.start", "agent_id": "x", "seq": 11, "epoch": "e1", "payload": {"name": EVIL, "args_preview": EVIL}})
    for r in (*G.panel_lines(m, 120, 40, expanded=True), G.agents_table(m)):
        s = render(r, color=True)
        for bad in BAD:
            assert bad not in s, (bad, s)
    assert "[bold]B[/bold]" in render(G.agents_table(m, width=400), 400)  # markup hiện nguyên văn


def test_agents_table_lists_everyone():
    m = team()
    s = render(G.agents_table(m), 160)
    for aid in ("Nghiên cứu", "lap-trinh", "van-hanh", "cho-slot", "ranh", "boss"):
        assert aid in s
    assert "rảnh" in s and "chờ duyệt" in s and "/attach" in s


@pytest.mark.parametrize("width", [60, 80, 110, 160])
def test_agents_table_fits_width(width):
    m = team()
    s = render(G.agents_table(m, width=width), width)
    for ln in s.splitlines():
        assert cell_len(ln) <= width, (width, ln)
    assert "$0.042" in s and "1m15" in s  # cột số không bị cắt
    assert ("TOOL CUỐI" in s) == (width >= 80)


# =========================================================================== thuần: định tuyến + thẻ
def test_routed_to_manager_no_bell_and_no_badge():
    m = G.AgentMonitor("boss")
    m.load_snapshot(snap([agent("w", "waiting")], [ap("ap_m", "w", routed_to="manager")]))
    assert m.counts() == {"approvals_human": 0, "questions_human": 0, "manager": 1, "tasks_waiting": 0}
    assert m.state(m.model.rows["w"]) == "manager"
    s = G.summary_line(m, 200).plain
    assert "chờ bạn duyệt" not in s and "◐ 1 điều phối viên đang xử lý" in s
    # event cũ (không có routed_to) → cần người; event mới có routed_to manager → không chuông
    n, _ = m.apply({"type": "approval.open", "agent_id": "w", "run_id": "r_w", "payload": {"request_id": "ap_m", "routed_to": "manager", "preview": "x"}})
    assert n and n[0]["routed"] == "manager" and n[0]["bell"] is False
    # escalated → người: thẻ đầy đủ + chuông, dù đã in dòng "đang xử lý"
    n, _ = m.apply({"type": "approval.escalated", "agent_id": "w", "run_id": "r_w", "payload": {"request_id": "ap_m"}})
    assert n[0]["kind"] == "approval" and n[0]["routed"] == "human" and n[0]["bell"] is True
    assert m.counts()["approvals_human"] == 1  # override event thắng snapshot cũ
    # delegated → lại về điều phối viên
    m.apply({"type": "approval.delegated", "agent_id": "w", "payload": {"request_id": "ap_m"}})
    assert m.counts()["approvals_human"] == 0


def test_approval_without_routed_field_needs_human_and_waiting_human_rings_again():
    m = G.AgentMonitor("boss")
    m.load_snapshot(snap([agent("w", "waiting")], [ap("ap_h", "w")]))
    assert m.counts()["approvals_human"] == 1
    n, need = m.apply({"type": "approval.open", "agent_id": "w", "payload": {"request_id": "ap_h", "preview": "rm"}})
    assert need and n[0]["bell"] and n[0]["routed"] == "human"
    n, _ = m.apply({"type": "approval.open", "agent_id": "w", "payload": {"request_id": "ap_h"}})
    assert n == []  # không in thẻ hai lần
    n, _ = m.apply({"type": "approval.waiting_human", "agent_id": "w", "payload": {"request_id": "ap_h"}})
    assert n == [{"kind": "note", "id": "ap_h", "waiting_human": True, "bell": True}]
    n, _ = m.apply({"type": "approval.resolved", "agent_id": "w", "payload": {"request_id": "ap_h", "decision": "allowed", "by": "user", "channel": "web"}})
    assert n[0]["resolved"]["channel"] == "web" and m.counts()["approvals_human"] == 0
    assert "đã cho phép bởi user qua web" in G.resolved_note("ap_h", n[0]["resolved"])["text"]


def test_question_routing():
    m = G.AgentMonitor("boss")
    m.load_questions([{"id": "q_m", "agent_id": "w", "status": "with_manager", "routed_to": "manager", "text": "?"},
                      {"id": "q_h", "agent_id": "w", "status": "open", "routed_to": "human", "text": "?"},
                      {"id": "q_x", "status": "answered"}])
    assert m.counts() == {"approvals_human": 0, "questions_human": 1, "manager": 1, "tasks_waiting": 0}
    n, _ = m.apply({"type": "question.open", "agent_id": "w", "payload": {"question_id": "q_n", "status": "with_manager", "routed_to": "manager", "text": "a?"}})
    assert n[0]["kind"] == "question" and n[0]["bell"] is False
    n, _ = m.apply({"type": "question.escalated", "agent_id": "w", "payload": {"question_id": "q_n", "status": "open", "routed_to": "human", "text": "a?"}})
    assert n[0]["bell"] is True and m.counts()["questions_human"] == 2
    n, _ = m.apply({"type": "question.answered", "agent_id": "w", "payload": {"question_id": "q_n", "by": "user", "channel": "web"}})
    assert n[0]["question_done"] == "question.answered" and "q_n" not in m.questions


def test_approval_card_full_command_and_actions():
    long_cmd = "find /srv/data -name '*.tmp' -mtime +30 \\\n  -exec rm -f {} + && echo " + "x" * 400 + " && echo ĐUÔI-CUỐI"
    a = ap("ap_9", "van-hanh", tool_input={"command": long_cmd, "timeout": 60}, preview=long_cmd[:300] + "…", status="waiting_human")
    s = render(G.approval_card(a, agent_name="Vận hành"), 100)
    assert "ĐUÔI-CUỐI" in s and "x" * 80 in s  # KHÔNG cắt (preview chỉ 300 ký tự)
    assert "tham số khác" in s and "timeout" in s
    for want in ("Cần bạn duyệt", "Vận hành", "Bash", "lý do: xoá đệ quy", "destructive · fs_delete · rm_recursive_outside_run", "id ap_9",
                 "/approve ap_9", "/deny ap_9", "run đã tạm dừng chờ bạn"):
        assert want in s, want
    assert "/always" not in s
    a["always_offer"] = {"kind": "exact", "agent_id": "van-hanh", "tool": "Bash", "fingerprint": "abc123", "summary": "luật exact"}
    assert "/always ap_9" in render(G.approval_card(a), 100)
    # điều phối viên xử lý → một dòng, vẫn chỉ cách tự quyết
    s = render(G.approval_card(a, routed="manager"), 100)
    assert "điều phối viên đang xử lý" in s and "/approve ap_9" in s and "ĐUÔI-CUỐI" not in s


def test_cards_render_agent_text_as_data():
    a = ap("ap_e", "x", tool_input={"command": EVIL}, preview=EVIL, reason=EVIL, matched_rule=EVIL)
    q = {"id": "q_e", "agent_id": "x", "text": EVIL, "options": [{"key": "1", "label": EVIL, "description": EVIL}], "status": "open"}
    for r in (G.approval_card(a, agent_name=EVIL), G.question_card(q, agent_name=EVIL), G.always_rule_text({"kind": "web_domain", "domain": EVIL}),
              G.pending_text(G.AgentMonitor(), [a], [q])):
        s = render(r, color=True)
        for bad in BAD:
            assert bad not in s, (bad, s)


def test_question_card():
    q = {"id": "q_1", "agent_id": "w", "status": "open", "text": "Giữ hay xoá thư mục cache?\nDung lượng 4 GB.", "multi": True, "allow_free_text": True,
         "options": [{"key": "1", "label": "Giữ"}, {"key": "2", "label": "Xoá", "description": "không khôi phục được"}]}
    s = render(G.question_card(q, agent_name="Nghiên cứu"))
    for want in ("Agent Nghiên cứu hỏi bạn", "Dung lượng 4 GB.", "1. Giữ", "2. Xoá — không khôi phục được", "/answer q_1 <số> (chọn nhiều: 1,2)"):
        assert want in s, want


# =========================================================================== daemon thật
class Script(MgrScript):
    def __call__(self, spec):
        if spec.prompt.lstrip().startswith("[hold]"):
            self.specs.append(spec)
            return {"text": "worker done", "cost": 0.002, "hold": True}
        return super().__call__(spec)


async def until(pred, timeout: float = 10.0) -> bool:
    for _ in range(int(timeout / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.fixture
async def denv(hub_home, monkeypatch):
    monkeypatch.setattr(repl, "BELL_GAP_S", 0.0)  # đếm từng tiếng chuông (thật: gộp trong 1 s)
    # approval-profiles: thẻ duyệt cho NGƯỜI dùng lệnh uncertain → profile strict (balanced cho uncertain chạy + audit, không mở approval)
    hub_home.config.write_text(yaml.safe_dump({"guard": {"enabled": True, "git_probe": False}, "approval": {"timeout_s": 30, "profile": "strict"}}))
    s = Script()
    d = Daemon(hub_home, runtime=ScriptedRuntime(s))
    d.hub.config.set("tasks.tick_s", 0.05)
    d.hub.config.set("manager.check_s", 3600)
    task = asyncio.create_task(d.serve())
    assert await until(lambda: hub_home.socket.exists())
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "role": "hub boss", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "manager.set", "params": {"agent_id": "boss"}})
    assert "error" not in resp, resp
    for aid, name in (("nghien-cuu", "Nghiên cứu"), ("lap-trinh", "Lập trình"), ("van-hanh", "Vận hành")):
        d.hub.agents.create({"id": aid, "name": name, "model": "haiku"})
    yield d
    d.hub.runs.runtime().release()  # type: ignore[attr-defined]
    d.stopping.set()
    try:
        await asyncio.wait_for(task, 20)
    except Exception:
        pass


class Fake:
    """Xác nhận y/N + chuông giả cho ChatSession."""

    def __init__(self, s: repl.ChatSession) -> None:
        self.answers: list[str] = []
        self.prompts: list[str] = []
        self.bells = 0
        self.launched: list[list[str]] = []
        s.ask, s.bell, s.launcher = self.ask, self.bell, self.launch

    async def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def bell(self) -> None:
        self.bells += 1

    async def launch(self, argv: list[str]) -> int:
        self.launched.append(argv)
        return 0


def chat(hub_home, width: int = 120) -> tuple[repl.ChatSession, io.StringIO, Fake]:
    buf = io.StringIO()
    s = repl.ChatSession(AsyncHubClient(hub_home.socket, channel="cli"), repl.Out(color=False, file=buf, width=width), new=True, home=hub_home.home)
    return s, buf, Fake(s)


async def hold_run(d, agent_id: str, text: str) -> str:
    rid = d.hub.runs.submit(agent_id, f"[hold] {text}")["id"]
    await wait_status(d, rid)
    return rid


async def guard_check(d, rid: str, cmd: str):
    task = asyncio.ensure_future(d.hub.guard.check({"run_id": rid, "tool_name": "Bash", "tool_input": {"command": cmd}, "channel": "hook"}))
    for _ in range(500):
        reqs = d.hub.approvals.list("pending", run_id=rid)
        if reqs:
            return task, reqs[0]
        await asyncio.sleep(0.01)
    raise AssertionError("không mở approval")


UNCERTAIN = 'eval "$CMD"  # ' + "dài " * 120 + "HẾT-LỆNH"


async def test_cards_panel_and_decisions_end_to_end(denv, hub_home):
    d = denv
    r1 = await hold_run(d, "nghien-cuu", "Tóm tắt README của repo X")
    r2 = await hold_run(d, "lap-trinh", "Chạy test và sửa test đỏ")
    r3 = await hold_run(d, "van-hanh", "Dọn dữ liệu tạm cũ")
    s, buf, f = chat(hub_home)
    assert await s.open()
    try:
        assert await until(lambda: len(s.mon.active()) == 3)
        assert s.panel_visible() and "● 3 agent đang chạy" in s.panel(120, 40)[0].plain
        # approval của agent KHÁC (không phải điều phối viên) → thẻ + chuông, lệnh đầy đủ
        t1, a1 = await guard_check(d, r3, UNCERTAIN)
        assert await until(lambda: "Cần bạn duyệt" in buf.getvalue() and f.bells == 1)
        out = buf.getvalue()
        assert "HẾT-LỆNH" in out and "agent Vận hành" in out and f"/approve {a1['id']}" in out and f"/always {a1['id']}" in out
        assert await until(lambda: "⚠ 1 chờ bạn duyệt" in s.panel(160, 40)[0].plain)
        s.panel_open = True
        rows = s.panel(160, 40)
        assert any("Vận hành" in x.plain and "duyệt:" in x.plain for x in rows[1:])

        buf.truncate(0), buf.seek(0)
        assert await s.handle("/approvals")
        assert a1["id"] in buf.getvalue()

        # /approve: in lại lệnh đầy đủ, "n" → không làm gì
        buf.truncate(0), buf.seek(0)
        f.answers = ["n"]
        assert await s.handle(f"/approve {a1['id']}")
        assert "HẾT-LỆNH" in buf.getvalue() and "đã huỷ" in buf.getvalue() and "MỘT lần" in f.prompts[-1]
        assert d.hub.approvals.get(a1["id"])["status"] in ("open", "waiting_human")
        # /always: bước 1 có, bước 2 không → không làm gì
        f.answers = ["y", "n"]
        assert await s.handle(f"/always {a1['id']}")
        assert d.hub.approvals.get(a1["id"])["status"] in ("open", "waiting_human") and d.hub.guard.policy()["always_allow"] == []
        assert "luật sẽ tạo: exact" in buf.getvalue()
        # /always: 2 bước đều có → cho phép + luật hẹp, kênh cli
        f.answers = ["y", "có"]
        assert await s.handle(f"/always {a1['id']}")
        row = d.hub.approvals.get(a1["id"])
        assert row["status"] == "allowed" and row["resolved_channel"] == "cli" and row["resolved_by"] == "user"
        assert len(d.hub.guard.policy()["always_allow"]) == 1 and "guard always rm" in buf.getvalue()
        assert (await asyncio.wait_for(t1, 10))["decision"] == "allow"
        assert await until(lambda: "đã cho phép bởi user qua cli" in buf.getvalue())

        # thao tác phá huỷ: không có /always; /approve y → cho phép một lần
        t2, a2 = await guard_check(d, r2, "rm -rf /etc")
        ok = await until(lambda: f"/deny {a2['id']}" in buf.getvalue() and f.bells == 2)
        assert ok, (f.bells, buf.getvalue()[-3000:])
        assert f"/always {a2['id']}" not in buf.getvalue()
        f.answers = ["y"]
        assert await s.handle(f"/always {a2['id']}")
        assert "không đủ điều kiện" in buf.getvalue() and d.hub.approvals.get(a2["id"])["status"] in ("open", "waiting_human")
        assert await s.handle(f"/deny {a2['id']} không được xoá /etc")
        row = d.hub.approvals.get(a2["id"])
        assert row["status"] == "denied" and row["note"] == "không được xoá /etc" and row["resolved_channel"] == "cli"
        assert (await asyncio.wait_for(t2, 10))["decision"] == "deny"
        assert await s.handle(f"/approve {a2['id']}") and "đã denied" in buf.getvalue()

        # /agents (= /ps)
        buf.truncate(0), buf.seek(0)
        assert await s.handle("/ps")
        assert all(n in buf.getvalue() for n in ("Nghiên cứu", "Lập trình", "Vận hành", "Điều phối"))

        # /stop <agent>: hỏi y/N
        f.answers = ["n"]
        assert await s.handle("/stop nghien-cuu")
        assert d.hub.store.get_run(r1)["status"] == "running" and r1 in f.prompts[-1]
        f.answers = ["y"]
        assert await s.handle("/stop nghien-cuu")
        await wait_status(d, r1, ("cancelled",))
        assert await until(lambda: "nghien-cuu" not in {r.id for _, r in s.mon.active()})

        # /attach → `cfharness attach <agent> --home …`
        assert await s.handle("/attach lap-trinh")
        argv = f.launched[-1]
        assert argv[1:5] == ["-m", "cfharness", "attach", "lap-trinh"] and argv[-2:] == ["--home", str(hub_home.home)]
        assert await s.handle("/attach --home=/x") and len(f.launched) == 1

        # audit (ApprovalService ghi `approval.resolved` kèm kênh) = cli cho cả cho phép lẫn từ chối
        import json

        rows = [(json.loads(r["data"]), r["channel"]) for r in d.hub.store.conn.execute("SELECT data, channel FROM audit_log WHERE kind='approval.resolved'")]
        got = {(x.get("approval_id"), x.get("channel") or ch, x.get("decision")) for x, ch in rows}
        assert got >= {(a1["id"], "cli", "allowed"), (a2["id"], "cli", "denied")}, got
    finally:
        await s.close()


async def test_question_card_and_answer(denv, hub_home):
    d = denv
    rid = await hold_run(d, "nghien-cuu", "Dọn cache")
    s, buf, f = chat(hub_home)
    assert await s.open()
    try:
        q = d.hub.questions.open(d.hub.store.get_run(rid), "Giữ hay xoá thư mục cache?", ["Giữ", "Xoá"])
        assert await until(lambda: "hỏi bạn" in buf.getvalue() and f.bells == 1)
        assert f"/answer {q['id']}" in buf.getvalue() and "1. Giữ" in buf.getvalue()
        assert await until(lambda: "❓ 1 câu hỏi chờ bạn" in s.panel(160, 40)[0].plain)
        assert await s.handle(f"/answer {q['id']} 2 nhớ sao lưu trước")
        row = d.hub.questions.get(q["id"])
        assert row["status"] == "answered" and row["answer"]["keys"] == ["2"] and row["answer"]["text"] == "nhớ sao lưu trước"
        assert row["answered_channel"] == "cli" and "đã trả lời" in buf.getvalue()
        assert await until(lambda: s.mon.counts()["questions_human"] == 0)
    finally:
        await s.close()


async def test_manager_routed_approval_no_bell_then_escalated(denv, hub_home):
    """Hợp đồng với luồng chính sách duyệt: `routed_to` + `approval.delegated|escalated` (có thể chưa có ở daemon — giả lập bằng event)."""
    d = denv
    rid = await hold_run(d, "van-hanh", "Dọn")
    s, buf, f = chat(hub_home)
    assert await s.open()
    try:
        orig = d.hub.guard.annotate_always

        def with_route(rows):  # daemon "tương lai": approval có routed_to
            out = orig(rows)
            for r in [out] if isinstance(out, dict) else out:
                if isinstance(r, dict) and r.get("id") and r.get("status") in ("open", "waiting_human"):
                    r.setdefault("routed_to", d.routed.get(r["id"]))
            return out

        d.routed = {}  # type: ignore[attr-defined]
        d.hub.guard.annotate_always = with_route
        run = d.hub.store.get_run(rid)
        a = d.hub.approvals.open(run, tool_name="Bash", tool_input={"command": "rm -rf /tmp/x"}, tool_use_id="t1", fingerprint="fp1",
                                 verdict={"klass": "fs_delete", "rule": "rm_recursive", "verdict": "destructive", "reason": "xoá"}, channel="hook")
        d.routed[a["id"]] = "manager"
        d.hub.publish(make_event("approval.delegated", "van-hanh", {"request_id": a["id"], "routed_to": "manager"}, run_id=rid))
        assert await until(lambda: "điều phối viên đang xử lý" in buf.getvalue())
        await asyncio.sleep(0.3)
        assert f.bells == 0 and "Cần bạn duyệt" not in buf.getvalue()
        assert await until(lambda: "◐ 1 điều phối viên đang xử lý" in s.panel(200, 40)[0].plain)
        assert "chờ bạn duyệt" not in s.panel(200, 40)[0].plain
        d.routed[a["id"]] = "human"
        d.hub.publish(make_event("approval.escalated", "van-hanh", {"request_id": a["id"], "routed_to": "human"}, run_id=rid))
        assert await until(lambda: "chuyển lên bạn" in buf.getvalue() and f.bells == 1)
        assert await until(lambda: "⚠ 1 chờ bạn duyệt" in s.panel(200, 40)[0].plain)
        d.hub.approvals.resolve(a["id"], "denied", by="user", channel="web")
    finally:
        await s.close()


async def test_startup_cards_and_plain_mode_confirm(denv, hub_home):
    d = denv
    rid = await hold_run(d, "van-hanh", "Dọn")
    t, a = await guard_check(d, rid, "rm -rf /etc")
    s, buf, f = chat(hub_home)
    assert await s.open()  # mở phiên khi đã có yêu cầu chờ → thẻ ngay
    try:
        assert "Cần bạn duyệt" in buf.getvalue() and "rm -rf /etc" in buf.getvalue() and f.bells == 1
        lines = iter([f"/approve {a['id']}\n", "y\n", ""])
        s.bell = None  # plain: không chuông
        code = await repl.run_plain(s, timeout=5, readline=lambda: next(lines))
        assert code == 0
        assert d.hub.approvals.get(a["id"])["status"] == "allowed"
        out = buf.getvalue()
        assert "Cho phép MỘT lần" in out and "đã CHO PHÉP" in out and "\x1b[" not in out
        assert (await asyncio.wait_for(t, 10))["decision"] == "allow"
    finally:
        await s.close()


async def test_firehose_resubscribes_after_slow_consumer(denv, hub_home):
    d = denv
    s, buf, f = chat(hub_home)
    assert await s.open()
    try:
        assert await until(lambda: s._fire_sub is not None and s._fire_sub._writer is not None)
        old = s._fire_sub
        old.close()  # như daemon ngắt subscriber chậm: luồng kết thúc → tự đăng ký lại từ seq cuối
        assert await until(lambda: s._fire_sub is not old, 5)
        rid = await hold_run(d, "lap-trinh", "việc sau khi nối lại")
        _t, a = await guard_check(d, rid, "rm -rf /etc")
        assert await until(lambda: f"/approve {a['id']}" in buf.getvalue())
        d.hub.approvals.resolve(a["id"], "denied")
    finally:
        await s.close()


async def test_tty_down_toggles_panel_without_breaking_history(denv, hub_home):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    d = denv
    await hold_run(d, "lap-trinh", "chạy test")
    s, buf, f = chat(hub_home)
    assert await s.open()
    try:
        assert await until(lambda: s.panel_visible())
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            async def drive():
                await asyncio.sleep(0.3)
                inp.send_text("\x1b[B")  # ↓ dòng trống → mở bảng
                assert await until(lambda: s.panel_open, 5)
                inp.send_text("\x1b[B")  # ↓ lần nữa → đóng
                assert await until(lambda: not s.panel_open, 5)
                inp.send_text("\x1b[B")
                assert await until(lambda: s.panel_open, 5)
                inp.send_text("\x1b")  # Esc → đóng
                assert await until(lambda: not s.panel_open, 5)
                inp.send_text("/ps\r")
                assert await until(lambda: "Lập trình" in buf.getvalue(), 5)
                inp.send_text("/quit\r")

            t = asyncio.ensure_future(drive())
            code = await asyncio.wait_for(repl.run_tty(s, hub_home.home), 20)
            await t
        assert code == 0
        # lịch sử ↑↓ vẫn ghi như cũ
        assert "/ps" in (hub_home.home / "cli_history").read_text(encoding="utf-8")
    finally:
        await s.close()


# =========================================================================== ảnh SVG (tuỳ chọn)
def test_screenshots_svg():
    out = os.environ.get("CFHARNESS_SCREENSHOTS")
    if not out:
        pytest.skip("đặt CFHARNESS_SCREENSHOTS=docs/screenshots/m2 để ghi ảnh")
    folder = Path(out)
    folder.mkdir(parents=True, exist_ok=True)
    m = team()
    m.model.rows["van-hanh"].data["status"] = "waiting_human"
    m.model.approvals[0]["status"] = "waiting_human"
    m.load_questions([{"id": "q_7c1e", "agent_id": "nghien-cuu", "status": "with_manager", "routed_to": "manager", "text": "Nên tóm tắt cả issue đã đóng?"}])
    st = V.ChatState("boss", "mc_4f2a", name="Điều phối viên")
    width = 110

    def screen(*body) -> Group:
        return Group(*body)

    mgr = Group(Text(""), Text.assemble(("❯ Bạn", V.C_USER), ("  09:41", V.C_FAINT)), Text("  Giao cho đội dọn dữ liệu tạm và chạy lại test nhé"),
                Text(""), Text.assemble(("◆ Điều phối viên", V.C_MGR), ("  09:41", V.C_FAINT)),
                Text("  Đã giao: lap-trinh chạy test, van-hanh dọn /srv/data/tmp. Mình sẽ báo khi xong."))
    card = G.approval_card({**m.model.approvals[0], "tool_input": {"command": "find /srv/data/tmp -type f -mtime +30 \\\n  -print -delete"},
                            "always_offer": None}, agent_name="Vận hành")
    prompt = Text.assemble(("❯ ", "bold cyan"), ("", ""))
    for name, expanded in (("15-chat-bang-agent.svg", True), ("16-chat-the-duyet.svg", False)):
        con = Console(record=True, width=width, force_terminal=True, color_system="truecolor", markup=False, highlight=False, emoji=False,
                      file=io.StringIO())
        con.print(mgr)
        con.print(card)
        con.print(Text(""))
        con.print(prompt)
        for ln in G.panel_lines(m, width, 40, expanded=expanded):
            con.print(ln)
        con.print(st.status_text(width))
        con.save_svg(str(folder / name), title="cfharness chat")
    assert (folder / "15-chat-bang-agent.svg").exists()
