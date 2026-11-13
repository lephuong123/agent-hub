"""evals/style.py — chấm phong cách câu trả lời theo RESPONSE_STYLE (hàm thuần) + checker `response_style` + `eval.style` (chỉ đọc DB)."""

from __future__ import annotations

import argparse

import pytest

from cfharness.errors import HubError
from cfharness.evals import style as S
from cfharness.evals.checkers import EvalOutput, grade_deterministic, validate_checker

Q_SHORT = "Bản build hôm nay chạy xong chưa?"

GOOD = [
    (Q_SHORT, "Xong rồi. Bản build lúc 9:40 đã đạt hết test, mình đã đẩy lên nhánh `release/1.4`."),
    ("Lệnh nào để chạy test của dự án?",
     "Chạy lệnh sau trong thư mục gốc của dự án:\n\n```bash\nuv run pytest -q\n```\n\nToàn bộ mất khoảng 25 phút trên Windows."),
    ("Tóm tắt giúp tôi tình hình ba việc đang làm, mỗi việc một dòng, kèm việc còn mở và việc cần tôi quyết định trong tuần này nhé, "
     "càng chi tiết càng tốt vì tôi cần gửi cho sếp trước buổi họp chiều nay.",
     "## Đã xong\n\nAgent **coder** đã sửa lỗi đăng nhập và thêm test hồi quy.\n\n## Còn mở\n\n- Trang báo cáo chậm khi có trên 10 nghìn dòng.\n"
     "- Bản dịch tiếng Anh còn thiếu ở màn Cài đặt.\n- Chưa có ảnh chụp màn hình mới cho tài liệu.\n\n## Cần anh quyết\n\nCó gộp PR "
     "vào nhánh chính trước buổi họp không?"),
    ("Đọc giúp tôi file cấu hình", "File `config.yaml` đặt `notify.telegram.agent_render: markdown`, phần còn lại để mặc định."),
    ("What is the build status?", "The build passed all tests at 9:40 and was pushed to `release/1.4`."),  # tiếng Anh → không ép tiếng Việt
    ("Run id của lần chạy vừa rồi là gì?", "Lần chạy vừa rồi là `r_0123456789ab`, kết thúc thành công."),  # người hỏi chính id
]

BAD = [
    ("internal_id", Q_SHORT, "Xong rồi, task t_0a1b2c3d4e đã hoàn tất (run r_0123456789ab)."),
    ("internal_id", "Có gì mới không?", "Có một câu hỏi đang chờ anh trong hội thoại mc_1a2b3c4d5e và luồng ws_deadbeef."),
    ("hub_header", Q_SHORT, "[cfharness] run xong\nBản build đã đạt hết test và được đẩy lên nhánh phát hành."),
    ("no_action", Q_SHORT, "Không có gì mới cần báo cáo trong lúc này.\nNO_ACTION"),
    ("fence_lang", "Lệnh nào để chạy test?", "Chạy lệnh sau trong thư mục gốc:\n\n```\nuv run pytest -q\n```"),
    ("hr", "Kết quả thế nào?", "Đã xong phần sửa lỗi.\n\n---\n\nPhần tài liệu vẫn còn dở, mai mình làm tiếp."),
    ("language", "Tình hình bản build hôm nay thế nào rồi?",
     "The build finished successfully and all tests passed. I pushed the artifacts to the release branch."),
    ("length", "Có lỗi gì không?", "Không có lỗi nghiêm trọng. " + "Mình đã kiểm tra lần lượt từng phần của hệ thống và ghi chú lại. " * 15),
]


@pytest.mark.parametrize("request_text,reply", GOOD)
def test_good_replies_pass(request_text, reply):
    r = S.check_style(reply, request_text)
    assert r.passed, r.violations
    assert r.score == 1.0


@pytest.mark.parametrize("rule,request_text,reply", BAD)
def test_bad_replies_flag_exact_rule(rule, request_text, reply):
    r = S.check_style(reply, request_text)
    assert not r.passed
    assert {v.rule for v in r.violations} == {rule}, r.violations
    assert 0 <= r.score < 1


def test_ids_in_code_paths_or_request_and_lookalikes():
    # id nằm trong yêu cầu → được nhắc lại
    assert S.check_style("Task t_0a1b2c3d4e đã xong, kết quả đã gửi kèm.", "task t_0a1b2c3d4e xong chưa?").passed
    # từ tiếng Anh/định danh trông giống id nhưng không phải hex của hub → không phạt
    r = S.check_style("Biến `t_total` và hàm `r_parse_value` đã được đổi tên cho dễ đọc hơn.", "Sửa gì vậy?")
    assert r.passed, r.violations
    # id trong khối mã vẫn là lộ id
    r = S.check_style("Xem log:\n\n```text\nrun r_0123456789ab failed\n```", "Sao lỗi vậy?")
    assert [v.rule for v in r.violations] == ["internal_id"] and r.violations[0].samples == ["r_0123456789ab"]


def test_language_heuristic_ignores_code_urls_and_unaccented_requests():
    reply = ("Mình đã sửa xong lỗi đăng nhập.\n\n```python\ndef login(user, password):\n    return check(user, password)\n```\n\n"
             "Chi tiết ở https://example.com/docs/login-flow và `src/auth/login.py`.")
    assert S.check_style(reply, "Sửa lỗi đăng nhập giúp tôi").passed
    # yêu cầu tiếng Việt không dấu → không kết luận được là tiếng Việt → không phạt câu trả lời tiếng Anh
    assert "language" not in S.check_style("The fix is merged and deployed to staging already.", "sua loi dang nhap giup toi").checked
    assert S.detect_lang("ok") == "unknown" and S.detect_lang("Bạn làm xong chưa?") == "vi" and S.detect_lang("are you done yet?") == "other"
    # tin chủ động (không yêu cầu) → mặc định tiếng Việt
    r = S.check_style("Heads up: the nightly job failed twice and needs your attention today.")
    assert [v.rule for v in r.violations] == ["language"]


def test_options_skip_and_budget():
    long = "Đã xong. " + "Mình ghi lại chi tiết từng bước đã làm để anh tiện xem lại. " * 15
    assert not S.check_style(long, "Xong chưa?").passed
    assert S.check_style(long, "Xong chưa?", S.StyleOptions(short_budget=5000)).passed
    assert S.check_style(long, "Xong chưa?", S.StyleOptions(skip=("length",))).passed
    # khối mã không tính vào độ dài
    code = "Đây là log:\n\n```text\n" + "dòng log rất dài\n" * 200 + "```"
    assert S.check_style(code, "Cho xem log").passed


def test_checker_kind_response_style_validates_and_grades():
    spec = validate_checker({"kind": "response_style", "short_budget": 300, "skip": ["hr"]})
    g = grade_deterministic(spec, EvalOutput(text="Xong rồi, đã chạy hết test.\n\n---\n\nKhông có lỗi.", prompt=Q_SHORT))
    assert g.passed and g.score == 1.0 and "hr" not in g.detail["checked"]
    g = grade_deterministic(validate_checker({"kind": "response_style"}), EvalOutput(text="Xong: task t_0a1b2c3d4e đã hoàn tất hết các bước rồi.\nNO_ACTION", prompt=Q_SHORT))
    assert not g.passed and {v["rule"] for v in g.detail["violations"]} == {"internal_id", "no_action"}
    # lồng trong all
    validate_checker({"kind": "all", "checks": [{"kind": "contains", "text": "xong"}, {"kind": "response_style"}]})
    for bad in ({"kind": "response_style", "skip": ["nope"]}, {"kind": "response_style", "min_vi_ratio": 2},
                {"kind": "response_style", "short_budget": "dài"}):
        with pytest.raises(HubError):
            validate_checker(bad)


def test_cli_style_flag_builds_checker():
    from cfharness.learning_ops.cli import checker_from_args

    a = argparse.Namespace(checker=None, regex=None, not_regex=None, contains=["xong"], json_schema=None, cmd=None, style=True,
                           judge=None, judge_file=None, flags="")
    assert checker_from_args(a) == {"kind": "all", "checks": [{"kind": "contains", "all": ["xong"], "ignore_case": True}, {"kind": "response_style"}]}


# ============================================================================ eval.style (chỉ đọc DB)
def _conv(store, cid: str, agent: str = "mgr") -> None:
    store.conn.execute("INSERT INTO manager_conversations (id, channel, agent_id, title, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                       (cid, "web", agent, "t", "2026-11-13T00:00:00.000Z", "2026-11-13T00:00:00.000Z"))


def _msg(store, cid: str, role: str, text: str, run_id: str | None) -> None:
    from cfharness.store import now_iso

    store.conn.execute("INSERT INTO manager_messages (conversation_id, role, text, run_id, ts) VALUES (?,?,?,?,?)", (cid, role, text, run_id, now_iso()))


def test_score_replies_reads_manager_history(tmp_path):
    from cfharness.evals.style_report import score_replies
    from cfharness.store import Store

    store = Store(tmp_path / "hub.db")
    _conv(store, "mc_aaaaaaaaaa")
    _msg(store, "mc_aaaaaaaaaa", "user", Q_SHORT, "r_000000000001")
    _msg(store, "mc_aaaaaaaaaa", "manager", "Xong rồi, bản build đã đạt hết test.", "r_000000000001")
    _msg(store, "mc_aaaaaaaaaa", "user", "Còn task nào chưa xong?", "r_000000000002")
    _msg(store, "mc_aaaaaaaaaa", "manager", "Còn task t_0a1b2c3d4e đang chạy.", "r_000000000002")
    _msg(store, "mc_aaaaaaaaaa", "tick", "(sự kiện)", "r_000000000003")
    _msg(store, "mc_aaaaaaaaaa", "manager", "NO_ACTION", "r_000000000003")
    _msg(store, "mc_aaaaaaaaaa", "manager", "(Điều phối viên không trả lời được: lỗi)", "r_000000000004")
    _msg(store, "mc_aaaaaaaaaa", "notify", "The nightly job failed twice and needs your attention today.", "r_000000000005")
    before = store.conn.total_changes
    r = score_replies(store, since="1d")
    assert store.conn.total_changes == before  # chỉ đọc
    s = r["summary"]
    assert s["n"] == 3 and s["passed"] == 1
    assert s["by_rule"] == {"internal_id": 1, "language": 1}
    assert s["skipped"] == {"tick_no_action": 1, "hub_failure": 1}
    one = score_replies(store, run_id="r_000000000002")
    assert one["summary"]["n"] == 1 and one["items"][0]["violations"][0]["samples"] == ["t_0a1b2c3d4e"]
    assert score_replies(store, since="1d", agent_id="khac")["summary"]["n"] == 0


def test_rpc_and_cli_surface(tmp_path):
    from types import SimpleNamespace

    from cfharness.cli import build_parser
    from cfharness.learning_ops.cli import _style_text
    from cfharness.learning_ops.rpc import MUTATING_B, LearningOpsRpc
    from cfharness.store import Store

    a = build_parser().parse_args(["eval", "style", "r_1", "--since", "2d", "--skip", "length,hr", "--short-budget", "400", "-v"])
    assert (a.action, a.run, a.since, a.skip, a.short_budget, a.verbose) == ("style", "r_1", "2d", ["length,hr"], 400, True)
    assert "eval.style" not in MUTATING_B  # chỉ đọc → không bọc audit
    store = Store(tmp_path / "hub.db")
    _conv(store, "mc_bbbbbbbbbb")
    _msg(store, "mc_bbbbbbbbbb", "user", Q_SHORT, "r_000000000009")
    _msg(store, "mc_bbbbbbbbbb", "manager", "Xong, run r_000000000009 đã đạt hết các bài test.", "r_000000000009")
    rpc = LearningOpsRpc(SimpleNamespace(hub=SimpleNamespace(store=store)))
    r = rpc.methods()["eval.style"]({"since": "1d", "options": {"skip": ["hr"]}})
    assert r["summary"]["by_rule"] == {"internal_id": 1} and r["options"]["skip"] == ["hr"]
    txt = _style_text(r)
    assert "0/1 câu trả lời đạt" in txt and "internal_id" in txt and "r_000000000009" in txt
    with pytest.raises(HubError):
        rpc.methods()["eval.style"]({"options": {"skip": ["khong_co"]}})
    assert "không có câu trả lời" in _style_text(rpc.methods()["eval.style"]({"since": "1d", "agent_id": "khac"}))
