"""Trạng thái hiển thị thống nhất + từ ngữ thân thiện (ux-chat-flow P1-5..P1-8) — hàm thuần `web/present.py` và bảng Công việc.

Không gọi `claude`, không cần daemon: chỉ hàm thuần + render template Jinja.
"""

from __future__ import annotations

from cfharness.web import office
from cfharness.web import present as P
from cfharness.web.app import make_templates

AP_OPEN = {"id": "ap1", "run_id": "r_1", "agent_id": "van-hanh", "status": "open"}
AP_HUMAN = {"id": "ap2", "run_id": "r_2", "agent_id": "dev", "status": "waiting_human"}
Q_OPEN = {"id": "q_1", "agent_id": "dieu-phoi", "run_id": "r_3", "status": "open"}
Q_MGR = {"id": "q_2", "agent_id": "nghien-cuu", "run_id": "r_4", "status": "with_manager"}


# ---------------------------------------------------------------------------- display_state
def test_run_waiting_approval_shows_cho_duyet_not_dang_chay():
    idx = P.need_index([AP_OPEN], [])
    key = P.run_state({"id": "r_1", "status": "running"}, idx)
    assert key == "waiting_human" and P.run_status(key)[0] == "Chờ duyệt"
    # agent của run đó cũng "Chờ duyệt" (cùng chữ ở mọi bề mặt)
    assert P.agent_status(P.agent_state({"id": "van-hanh", "status": "working"}, idx))[1] == "Chờ duyệt"


def test_priority_approval_over_question_over_working():
    idx = P.need_index([AP_OPEN], [{**Q_OPEN, "agent_id": "van-hanh", "run_id": "r_1"}])
    assert P.run_state({"id": "r_1", "status": "running"}, idx) == "waiting_human"
    idx = P.need_index([], [Q_OPEN])
    assert P.run_state({"id": "r_3", "status": "running"}, idx) == "asking"
    assert P.agent_state({"id": "dieu-phoi", "status": "idle"}, idx) == "asking"
    assert P.agent_state({"id": "dieu-phoi", "status": "working"}, idx) == "asking"


def test_with_manager_only_replaces_idle():
    idx = P.need_index([], [Q_MGR])
    assert P.agent_state({"id": "nghien-cuu", "status": "idle"}, idx) == "with_manager"
    assert P.agent_state({"id": "nghien-cuu", "status": "working"}, idx) == "working"
    assert P.agent_status("with_manager")[1] == "Chờ điều phối viên"


def test_finished_run_keeps_outcome_and_paused_agent_stays_paused():
    idx = P.need_index([AP_OPEN], [Q_OPEN])
    assert P.run_state({"id": "r_1", "status": "completed"}, idx) == "completed"
    assert P.agent_state({"id": "van-hanh", "status": "paused"}, idx) == "paused"


def test_open_question_beats_with_manager_for_same_agent():
    idx = P.need_index([], [{**Q_MGR, "agent_id": "a"}, {**Q_OPEN, "agent_id": "a"}])
    assert idx["q_agent"]["a"] == "open"


# ---------------------------------------------------------------------------- một nguồn đếm "Cần bạn"
def test_need_count_excludes_with_manager_questions():
    assert P.need_count([AP_OPEN, AP_HUMAN, {"status": "allowed"}], [Q_OPEN, Q_MGR]) == 3


def test_office_state_need_equals_badge_count_and_statuses_match_2d():
    snap = {"hub": {}, "agents": [{"id": "van-hanh", "status": "waiting_human"}, {"id": "dieu-phoi", "status": "idle"},
                                  {"id": "nghien-cuu", "status": "idle"}],
            "approvals": [{**AP_HUMAN, "agent_id": "van-hanh"}]}
    st = office.build_state(snap, [Q_OPEN, {**Q_OPEN, "id": "q_9", "agent_id": "nghien-cuu"}, Q_MGR], [], "dieu-phoi")
    # huy hiệu menu 2D: approvals (1) + câu hỏi chờ BẠN (2) = 3 — câu hỏi ở điều phối viên không tính
    assert st["hub"]["need"] == 3 == P.need_count(snap["approvals"], [Q_OPEN, {**Q_OPEN, "id": "q_9"}, Q_MGR])
    by = {a["id"]: a for a in st["agents"]}
    assert by["dieu-phoi"]["status"] == "asking" and by["van-hanh"]["status"] == "waiting_human"
    assert by["nghien-cuu"]["questions"] == 1  # chỉ đếm câu hỏi chờ bạn


# ---------------------------------------------------------------------------- run_title: không lộ prompt nội bộ tiếng Anh
def test_run_title_hides_internal_prompts():
    t = P.run_title({"prompt": "[cfharness] Answer to your question q_bc668cc588 from the human owner. Selected: 1. Yes."})
    assert t == "Tiếp tục sau khi bạn trả lời"
    assert P.run_title({"prompt": "[cfharness] Answer to your question q_1 from the coordinator (manager agent)."}) == "Tiếp tục sau khi điều phối viên trả lời"
    assert P.run_title({"prompt": "[cfharness task t_1] You have been assigned a task by cfharness.\n\n## Task: Viết tài liệu cài đặt\nbody"}) == "Viết tài liệu cài đặt"
    assert P.run_title({"prompt": "[cfharness heartbeat 2026-11-25]"}) == "Điều phối viên · kiểm tra định kỳ"
    assert P.run_title({"prompt": "[cfharness] A human has now APPROVED request ap_1"}) == "Tiếp tục sau khi bạn duyệt"
    assert P.run_title({"prompt": "[cfharness] You must do X"}) == "Việc hệ thống"
    assert P.run_title({"prompt": "Tóm tắt báo cáo tuần"}) == "Tóm tắt báo cáo tuần"
    assert P.run_title({"prompt": "x", "task_id": "t_1"}, {"t_1": "Tiêu đề việc"}) == "Tiêu đề việc"


def test_run_kind_vocabulary():
    assert P.run_kind("adhoc") == "" and P.run_kind(None) == ""
    assert P.run_kind("tick") == "kiểm tra định kỳ" and P.run_kind("task") == "theo công việc"


def test_destructive_why_plain_words_first():
    assert P.destructive_why({"destructive_class": "file"}).startswith("Agent định xoá hoặc ghi đè")
    assert "mất dữ liệu" in P.destructive_why({})


# ---------------------------------------------------------------------------- bảng Công việc
def _t(i: str, st: str) -> dict:
    return {"id": i, "title": "Việc " + i, "status": st}


def test_board_columns_merge_not_started_and_keep_done_visible():
    cols = {c: [] for c in P.TASK_COLUMNS}
    cols["ready"] = [_t("a", "ready")]
    cols["triage"] = [_t("b", "triage")]
    cols["done"] = [_t("c", "done")]
    bc = P.board_columns(cols)
    assert [c["key"] for c in bc] == ["todo", "running", "review", "blocked", "done"]
    assert [t["id"] for t in bc[0]["tasks"]] == ["b", "a"] and bc[0]["label"] == "Chưa bắt đầu"
    assert P.TASK_STATUS["triage"][0] == "Mới"


def test_board_template_collapses_empty_columns_and_empty_state():
    env = make_templates().env
    tpl = env.get_template("_tasks_board.html")
    cols = {c: [] for c in P.TASK_COLUMNS}
    cols["done"] = [_t("c", "done")]
    html = tpl.render(cols=cols, q={}, csrf="x")
    assert 'class="col col-done"' in html and 'col col-running is-empty' in html
    assert html.count('class="col ') == 5
    empty = tpl.render(cols={c: [] for c in P.TASK_COLUMNS}, q={}, csrf="x")
    assert "Chưa có việc" in empty and 'href="/manager"' in empty


def test_heavy_data_vocab_leaks():
    """Dữ liệu thật nhiều: run tự học (prompt Hermes tiếng Anh) và task chờ trưởng nhóm không lộ chuỗi nội bộ."""
    from cfharness.web import present as P

    assert P.run_title({"prompt": "Review the conversation above and update two things:\n\n1. memory"}) == "Tự học · rà soát hội thoại"
    assert P.wait_reason("no_lead") == "team chưa có trưởng nhóm"
