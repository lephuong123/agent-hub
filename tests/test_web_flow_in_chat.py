"""Luồng việc ngay trong chat (ux-chat-flow mục 1, 2, 4, 9, 10): khay "Cần bạn", thẻ việc sống, "Đội đang làm gì" (`web/flow.py`,
`GET /api/manager/flow`), panel diễn biến (`/runs/<id>?panel=1`), tìm kiếm mở đúng hội thoại, bộ lọc /runs "Chờ duyệt".

Không gọi `claude` thật: hàm thuần + daemon thật với FakeRuntime giữ run ở `running`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cfharness.runtime import FakeRuntime
from cfharness.web import extra_pages as X
from cfharness.web import flow as FL
from cfharness.web import manager_feed as F
from cfharness.web import present as P

from test_web import ISOLATED, _start, _stop, open_approval, running_run, until  # noqa: F401

COORD = Path(__file__).resolve().parents[1] / "src" / "cfharness" / "web" / "static" / "coord"

AP_H = {"id": "ap2", "run_id": "r_2", "agent_id": "dev", "status": "waiting_human", "opened_at": "2026-11-25T02:00:00Z", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /x"}}
AP_O = {"id": "ap1", "run_id": "r_1", "agent_id": "van-hanh", "status": "open", "opened_at": "2026-11-25T01:00:00Z", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /y"}}
Q_O = {"id": "q_1", "agent_id": "lap-trinh", "run_id": "r_3", "status": "open", "text": "Sửa theo hướng nào?", "options": ["A", "B"],
       "created_at": "2026-11-25T01:30:00Z"}
Q_M = {"id": "q_2", "agent_id": "nghien-cuu", "run_id": "r_4", "status": "with_manager", "text": "x"}


# ---------------------------------------------------------------------------- khay "Cần bạn" (thuần)
def test_tray_count_equals_badge_and_approvals_first():
    n = FL.need_json([AP_O, AP_H, {"id": "x", "status": "allowed"}], [Q_O, Q_M])
    assert n["count"] == P.need_count([AP_O, AP_H], [Q_O, Q_M]) == 3  # cùng định nghĩa với huy hiệu menu
    assert (n["approvals"], n["questions"], n["with_manager"]) == (2, 1, 1)
    kinds = [(it["kind"], it["id"]) for it in n["items"]]
    # duyệt trước (chờ quá hạn — waiting_human — trước), câu hỏi chờ bạn sau; câu hỏi ở điều phối viên không thành mục
    assert kinds == [("approval", "ap2"), ("approval", "ap1"), ("question", "q_1")]
    ap = n["items"][0]
    assert ap["text"] == "rm -rf /x" and ap["href"] == "/approvals"  # toàn văn thao tác, cùng dạng `actions` của ảnh chụp v2
    assert n["items"][2]["options"][1]["label"] == "B"


# ---------------------------------------------------------------------------- "Đội đang làm gì" (thuần)
def test_team_lists_busy_agents_attention_first_without_manager():
    rows = [{"id": "dieu-phoi", "status": "working"}, {"id": "a", "status": "working", "task": "Viết docs", "last_tool": "Read"},
            {"id": "b", "status": "idle"}, {"id": "van-hanh", "status": "working", "current_run_id": "r_1",
                                            "task": "[cfharness] Answer to your question q_1 from the human owner."}]
    idx = P.need_index([AP_O], [])
    team = FL.team_json(rows, idx, "dieu-phoi")
    assert [a["id"] for a in team] == ["van-hanh", "a"]  # chờ duyệt lên trước; rảnh và điều phối viên không có
    assert team[0]["label"] == "Chờ duyệt" and team[0]["run_id"] == "r_1"
    assert team[0]["task"] == "Tiếp tục sau khi bạn trả lời"  # không lộ prompt nội bộ tiếng Anh
    assert team[1]["tool"] == "Read"


# ---------------------------------------------------------------------------- thẻ việc sống (thuần)
def test_work_card_task_states_and_result():
    idx = P.need_index([{"run_id": "r_9", "agent_id": "a", "status": "waiting_human"}], [])
    rows = [{"id": "a", "current_run_id": "r_9", "last_tool": "Bash"}]
    t = {"id": "t_1", "title": "Dọn dữ liệu", "status": "running", "assignee_agent_id": "a",
         "runs": [{"id": "r_8", "agent_id": "a", "status": "failed"}, {"id": "r_9", "agent_id": "a", "status": "running"}]}
    w = FL.work_task(t, idx, rows)
    assert (w["label"], w["live"], w["run_id"], w["step"], w["href"]) == ("Chờ duyệt", True, "r_9", "Đang dùng Bash", "/tasks/t_1")
    done = FL.work_task({**t, "status": "done", "result_summary": "Đã xong 5 ý", "runs": [{"id": "r_9", "agent_id": "a", "status": "completed"}]}, {}, [])
    assert (done["label"], done["live"], done["end"], done["result"]) == ("Xong", False, True, "Đã xong 5 ý")


def test_work_card_run_and_manager_run_skipped():
    r = FL.work_run({"id": "r_1", "agent_id": "x", "status": "running", "prompt": "Tóm tắt README"}, {}, [])
    assert (r["title"], r["label"], r["live"], r["href"]) == ("Tóm tắt README", "Đang chạy", True, "/runs/r_1")
    assert FL.work_run({"id": "r_2", "kind": "manager", "status": "completed"}, {}, [])["skip"] is True


def test_work_ids_whitelist_and_cap():
    assert FL.work_ids("t_1, r_2,../x,agent:a,t_1,r_<b>") == ["t_1", "r_2"]
    assert len(FL.work_ids(",".join(f"t_{i}" for i in range(40)))) == FL.WORK_MAX


def test_action_target_carries_kind_id_only_for_internal_link():
    a = F.action_json(P.mgr_action_item({"payload": {"tool": "task_create", "target": "task:t_abc", "summary": "giao"}, "run_id": "r_m"}))
    assert a["target"] == {"href": "/tasks/t_abc", "label": a["target"]["label"], "kind": "task", "id": "t_abc"}
    b = F.action_json(P.mgr_action_item({"payload": {"tool": "x", "target": "javascript:alert(1)"}}))
    assert b["target"]["kind"] is None and b["target"]["id"] is None and b["target"]["href"] is None


def test_island_has_tray_cards_team_panel():
    v = (COORD / "views.js").read_text(encoding="utf-8")
    for s in ("function Tray()", "function WorkCards(", "function TeamSide()", "Đội đang làm gì", "/api/manager/flow", "data-xpanel"):
        assert s in v, s
    # khay dùng CÙNG thẻ duyệt/câu hỏi (đi qua ui.decide/ui.answer → /api/manager/act) — không có đường duyệt riêng
    assert "<${Approval} key=${a.id} a=${a} p=${tray} />" in v and "/do/approve" not in v
    # "Kiểm tra ngay" / "Huỷ chỉ định" chỉ nằm trong mục thu gọn "Điều phối viên"
    i = v.index('<details class="mgr-side-sec side-fold"><summary><h3>Điều phối viên</h3>')
    assert v.index('name="manager_tick"') > i and v.index('name="manager_unset"') > i


# ---------------------------------------------------------------------------- tìm kiếm (thuần)
def test_search_link_opens_conversation_with_message_anchor():
    assert X.hit_link({"run_id": "r_m", "conversation_id": "mc_1", "ref": "mm:42"}) == "/manager?c=mc_1#m42"
    assert X.hit_link({"run_id": "r_m"}, "mc_1") == "/manager?c=mc_1"  # lượt chạy của điều phối viên → hội thoại của nó
    assert X.hit_link({"run_id": "r_x", "ref": "ev:1:2"}) == "/runs/r_x"  # run thường vẫn mở run
    assert X._run_conv({"run_opts": '{"manager_conversation": "mc_9"}'}) == "mc_9" and X._run_conv({"run_opts": None}) is None


def test_search_groups_by_conversation_keeping_rank_order():
    hits = [{"id": 1, "conversation_id": "mc_1", "run_id": "r_a"}, {"id": 2, "run_id": "r_x", "agent_id": "dev"},
            {"id": 3, "run_id": "r_b"}, {"id": 4, "conversation_id": "mc_2"}]
    gs = X.group_hits(hits, {"r_b": "mc_1"}, {"mc_1": "Kế hoạch tuần"})
    assert [(g["conv"], g["run_id"], len(g["hits"])) for g in gs] == [("mc_1", None, 2), (None, "r_x", 1), ("mc_2", None, 1)]
    assert gs[0]["title"] == "Kế hoạch tuần" and gs[0]["link"] == "/manager?c=mc_1" and gs[2]["title"] == "Hội thoại với điều phối viên"


# ---------------------------------------------------------------------------- qua daemon thật + HTTP
@pytest.fixture
async def held(hub_home):
    rt = FakeRuntime(ISOLATED, line_delay_s=0.01, hold=True)
    e = await _start(hub_home, rt)
    yield e
    rt.release()
    await _stop(e)


async def test_flow_endpoint_tray_approve_same_gate_and_runs_filter(held):
    e = held
    assert (await e.client.get("/api/manager/flow")).status_code == 401  # hết phiên → 401 JSON như /api/manager/*
    await e.login()
    run = await running_run(e)
    ap = open_approval(e.hub, run["id"])
    r = await e.client.get(f"/api/manager/flow?work={run['id']},bad id")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    j = r.json()
    assert j["need"]["count"] == 1 and j["need"]["items"][0]["id"] == ap["id"] and "rm -rf /srv/data" in j["need"]["items"][0]["text"]
    w = j["work"][run["id"]]
    assert w["state"] == "waiting_human" and w["label"] == "Chờ duyệt" and w["live"] is True and w["agent"] == "ag0"
    assert [a["id"] for a in j["team"]] == ["ag0"] and j["team"][0]["label"] == "Chờ duyệt"

    # /runs "Chờ duyệt" gồm run đang chạy có phê duyệt mở (khớp pill)
    html = (await e.client.get("/runs?status=waiting_human")).text
    assert f"/runs/{run['id']}" in html and "Chờ duyệt" in html
    assert f"/runs/{run['id']}" not in (await e.client.get("/runs?status=failed")).text

    # panel diễn biến cho panel trượt: chỉ phần thân (dấu hiệu fview-card), không khung trang
    p = await e.client.get(f"/runs/{run['id']}?panel=1")
    assert p.status_code == 200 and "fview-card run-pcard" in p.text and 'id="fpanel"' not in p.text and "<html" not in p.text

    # duyệt từ khay = CÙNG cổng với trang Duyệt: thiếu xác nhận → hỏi lại, không gọi RPC
    r0 = await e.post("/api/manager/act/approve", {"id": ap["id"], "idem": "t1"}, headers={"X-Ahub-Fetch": "1"})
    assert r0.json().get("need") in ("confirm", "confirm2")
    assert e.hub.approvals.get(ap["id"])["status"] in ("open", "waiting_human")
    r1 = await e.post("/api/manager/act/approve", {"id": ap["id"], "idem": "t2", "confirm": "yes"}, headers={"X-Ahub-Fetch": "1"})
    body = r1.json()
    if body.get("need") == "confirm2":  # lớp nặng: bước 2 ở trang Duyệt — khay không có lối tắt
        assert e.hub.approvals.get(ap["id"])["status"] in ("open", "waiting_human")
    else:
        assert body.get("ok") is True, body
        assert await until(lambda: e.hub.approvals.get(ap["id"])["status"] not in ("open", "waiting_human"))
        assert (await e.client.get("/api/manager/flow")).json()["need"]["count"] == 0
