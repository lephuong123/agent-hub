"""Telegram — bot long polling + thông báo + duyệt bằng nút + lệnh chat. Server Bot API GIẢ cục bộ (tests/telegram_fake.py),
daemon IN-PROCESS (`Daemon` + FakeRuntime), không gọi Telegram/claude thật."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest
import yaml

from cfharness import auth
from cfharness.daemon import Daemon
from cfharness.errors import BLOCKED
from cfharness.events import make_event
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.runtime import build_settings, rule_path, sb_path
from cfharness.notify.telegram import TelegramApi, TelegramError, format_event
from cfharness.runtime import FakeRuntime

from telegram_fake import TOKEN, FakeTelegram

BOSS, OTHER, STRANGER = 111, 222, 999


async def until(fn, timeout: float = 10.0, step: float = 0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = fn()
        if v:
            return v
        await asyncio.sleep(step)
    raise AssertionError("hết giờ chờ điều kiện")


@pytest.fixture
def tg():
    f = FakeTelegram()
    yield f
    f.close()


async def stop(d: Daemon) -> None:
    if not getattr(d, "_stopped", False):
        d._stopped = True  # type: ignore[attr-defined]
        rt = getattr(d, "_rt", None)
        if rt is not None and hasattr(rt, "release"):
            rt.release()
        await asyncio.wait_for(d.hub.stop(), 15)


@pytest.fixture
async def make(hub_home, tg):
    daemons: list[Daemon] = []

    async def _m(*, allowed=(BOSS,), token=True, runtime=None, tg_cfg=None, cfg=None):
        c = {"guard": {"git_probe": False},
             "notify": {"telegram": {"api_base": tg.url, "poll_timeout_s": 1, "idle_check_s": 0.05, "backoff_base_s": 0.05,
                                     "allowed_chat_ids": [str(x) for x in allowed], **(tg_cfg or {})}}}
        c.update(cfg or {})
        hub_home.config.write_text(yaml.safe_dump(c))
        rt = runtime or FakeRuntime()
        d = Daemon(hub_home, runtime=rt)
        d._rt = rt  # type: ignore[attr-defined]
        if token:
            d.hub.secrets.set("TELEGRAM_BOT_TOKEN", TOKEN)
        d.hub.start()
        daemons.append(d)
        return d

    yield _m
    for d in daemons:  # test lỗi giữa chừng vẫn dừng hub (không treo vòng poll / run đang giữ)
        try:
            await stop(d)
        except Exception:
            pass


async def rpc(d: Daemon, method: str, params: dict | None = None):
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    if "error" in resp:
        raise AssertionError(resp["error"])
    return resp["result"]


async def say(tg: FakeTelegram, chat: int, text: str, **kw) -> None:
    uid = tg.user_message(chat, text, **kw)
    await until(lambda: tg.processed(uid))
    await asyncio.sleep(0.05)


async def press(tg: FakeTelegram, chat: int, data: str, **kw) -> None:
    uid = tg.press(chat, data, **kw)
    await until(lambda: tg.processed(uid))
    await asyncio.sleep(0.05)


def texts(tg: FakeTelegram, chat: int) -> list[str]:
    return [m["text"] for m in tg.sent(chat)]


def gctx(hub_home, cwd=None):
    from cfharness.guard.policy import PolicyStore

    return GuardContext(cwd or hub_home.home, PolicyStore(hub_home.home).load(), hub_socket=str(hub_home.socket), git_probe=False)


def open_approval(d: Daemon, run_id: str, *, klass: str = "file", cmd: str = "rm -rf dummy", fp: str = "fp1", prot: str | None = None):
    run = d.hub.store.get_run(run_id)
    return d.hub.approvals.open(run, tool_name="Bash", tool_input={"command": cmd}, tool_use_id="t1", fingerprint=fp,
                                verdict={"klass": klass, "rule": "delete_not_run_created", "verdict": "destructive", "reason": "rm -r thư mục có sẵn",
                                         "protected_id": prot}, channel="hook")


async def held_run(d: Daemon, agent: str = "coder") -> str:
    d.hub.agents.create({"id": agent, "name": agent})
    r = d.hub.runs.submit(agent, "làm việc")
    await until(lambda: d.hub.store.get_run(r["id"])["status"] == "running")
    return r["id"]


# ============================================================================ cấu hình / tắt êm
async def test_disabled_quietly_without_token(make, tg):
    d = await make(token=False)
    d.hub.publish(make_event("approval.open", "coder", {"request_id": "ap_x", "preview": "rm -rf x"}, run_id="r1"))
    await asyncio.sleep(0.4)
    assert tg.calls == []  # không gọi Bot API
    st = await rpc(d, "telegram.status")
    assert st["configured"] is False and st["polling"] is False
    assert not d.hub.store.query_events(agent_id="coder")  or all(e.type != "notify.failed" for e in d.hub.store.query_events(agent_id="coder"))
    await stop(d)


async def test_setup_pairing_allowlist_and_stranger_ignored(make, tg):
    d = await make(token=False, allowed=())
    with pytest.raises(AssertionError, match="đúng dạng"):
        await rpc(d, "telegram.setup", {"token": "abc"})
    r = await rpc(d, "telegram.setup", {"token": TOKEN})
    assert r["bot_username"] == "hub_test_bot" and r["pair_code"] and r["deep_link"].endswith(r["pair_code"])
    assert TOKEN not in str(r)
    assert d.hub.secrets.get("TELEGRAM_BOT_TOKEN") == TOKEN and d.hub.secrets.get("TELEGRAM_CALLBACK_KEY")
    await until(lambda: tg.calls_of("getUpdates"))
    # người lạ: sai mã → bỏ qua + audit, không trả lời
    await say(tg, STRANGER, "/start WRONGCODE")
    await say(tg, STRANGER, "/status")
    assert tg.sent(STRANGER) == []
    assert await rpc(d, "telegram.pending", {"code": r["pair_code"]}) == []
    # chủ: đúng mã → chờ xác nhận ở terminal (chưa vào allowlist)
    await say(tg, BOSS, f"/start {r['pair_code'].lower()}")
    pend = await rpc(d, "telegram.pending", {"code": r["pair_code"]})
    assert [p["chat_id"] for p in pend] == [str(BOSS)] and pend[0]["username"] == "boss"
    assert "Quay lại terminal" in texts(tg, BOSS)[-1]
    assert str(BOSS) not in d.hub.config.get("notify.telegram.allowed_chat_ids")
    await say(tg, BOSS, "/status")
    assert len(tg.sent(BOSS)) == 1  # chưa được phép → lệnh bị bỏ qua
    out = await rpc(d, "telegram.allow", {"chat_id": str(BOSS)})
    assert out["allowed_chat_ids"] == [str(BOSS)] and out["welcome_sent"] is True
    assert d.hub.telegram.pairing is None  # mã dùng một lần
    await say(tg, BOSS, "/status")
    assert "ESTOP" in texts(tg, BOSS)[-1]
    au = d.hub.audit.query(kind="telegram.*")
    kinds = {a["kind"] for a in au}
    assert {"telegram.setup", "telegram.allow", "telegram.ignored", "telegram.pair_request"} <= kinds
    assert all(TOKEN not in str(a) for a in au)
    ign = [a for a in au if a["kind"] == "telegram.ignored"]
    assert {a["channel"] for a in ign} == {f"telegram:{STRANGER}", f"telegram:{BOSS}"}  # BOSS trước khi được phép
    await stop(d)


async def test_human_only_gate_for_telegram_channel(make, tg, hub_home):
    d = await make()
    for m in ("telegram.setup", "telegram.allow", "telegram.revoke", "telegram.disable", "telegram.test", "telegram.pending"):
        assert m in auth.HUMAN_ONLY
    assert auth.telegram_caller(d.hub, BOSS, BOSS).human is True
    assert auth.telegram_caller(d.hub, BOSS, None).human is False  # thiếu from.id
    assert auth.telegram_caller(d.hub, BOSS, 777).human is False  # chat riêng: người gửi phải là chủ chat
    assert auth.telegram_caller(d.hub, STRANGER, STRANGER).human is False
    # chat lạ (dù bằng cách nào tới được dispatch) không qua cổng HUMAN_ONLY
    resp = await d.call_as(auth.telegram_caller(d.hub, STRANGER, STRANGER), "estop.set", {"on": True})
    assert resp["error"]["code"] == BLOCKED and d.hub.runs.estop.get("on") is not True
    # agent gõ lệnh CLI telegram → forbidden (không tự ghép chat của nó vào allowlist)
    for cmd in ("cfharness notify telegram allow 5", "cfharness notify telegram setup --token-stdin"):
        v = classify("Bash", {"command": cmd}, gctx(hub_home))
        assert v.forbidden, cmd
    await stop(d)


# ============================================================================ lệnh chat
async def test_commands_status_agents_runs_help_estop(make, tg):
    d = await make()
    d.hub.agents.create({"id": "coder", "name": "Coder"})
    await until(lambda: tg.calls_of("getUpdates"))
    await say(tg, BOSS, "/help")
    assert "/run <agent> <yêu cầu>" in texts(tg, BOSS)[-1]
    await say(tg, BOSS, "/agents")
    assert "coder" in texts(tg, BOSS)[-1]
    await say(tg, BOSS, "/runs")
    assert "Chưa có run" in texts(tg, BOSS)[-1]
    await say(tg, BOSS, "/approvals")
    assert "Không có yêu cầu" in texts(tg, BOSS)[-1]
    await say(tg, BOSS, "/estop on bảo trì")
    assert d.hub.runs.estop["on"] is True
    au = d.hub.audit.query(kind="estop.set")
    assert au and au[0]["channel"] == f"telegram:{BOSS}" and au[0]["actor"] == "user"
    # tắt ESTOP: 2 bước
    await say(tg, BOSS, "/estop off")
    confirm = tg.sent(BOSS)[-1]
    btns = FakeTelegram.buttons(confirm)
    assert d.hub.runs.estop["on"] is True and [b["text"] for b in btns] == ["Xác nhận TẮT ESTOP", "Huỷ"]
    await press(tg, BOSS, btns[0]["callback_data"])
    assert d.hub.runs.estop["on"] is False
    await say(tg, BOSS, "/digest")
    assert "Digest 24 giờ qua" in texts(tg, BOSS)[-1]
    await say(tg, BOSS, "/khonglenh")
    assert "Không có lệnh" in texts(tg, BOSS)[-1]
    await stop(d)


async def test_run_command_returns_result_with_cost_then_reply_continues(make, tg):
    d = await make()
    d.hub.agents.create({"id": "coder", "name": "Coder"})
    await until(lambda: tg.calls_of("getUpdates"))
    await say(tg, BOSS, "/run coder viết hàm cộng hai số")
    await until(lambda: any("Đã giao run" in t for t in texts(tg, BOSS)))
    res_msg = await until(lambda: next((m for m in tg.sent(BOSS) if m["text"].startswith("<b>[cfharness] Run ")), None))
    runs = d.hub.store.list_runs(agent_id="coder")
    assert len(runs) == 1 and runs[0]["prompt"] == "viết hàm cộng hai số"
    run = runs[0]
    assert run["id"] in res_msg["text"] and "hoàn thành" in res_msg["text"] and "Chi phí: $" in res_msg["text"]
    assert (run["summary"] or "")[:40] in res_msg["text"]
    au = d.hub.audit.query(kind="run.start")
    assert au[0]["channel"] == f"telegram:{BOSS}"
    # reply vào tin kết quả → continue cùng session
    n0 = len(tg.sent(BOSS))
    sent_ids = _message_ids(tg, BOSS)
    res_id = sent_ids[[m["text"] for m in tg.sent(BOSS)].index(res_msg["text"])]
    await say(tg, BOSS, "thêm test cho hàm đó", reply_to=res_id)
    await until(lambda: any("Đã tiếp tục hội thoại" in t for t in texts(tg, BOSS)[n0:]))
    await until(lambda: len(d.hub.store.list_runs(agent_id="coder")) == 2)
    child = next(r for r in d.hub.store.list_runs(agent_id="coder") if r["id"] != run["id"])
    assert child["parent_run_id"] == run["id"] and child["prompt"] == "thêm test cho hàm đó"
    await until(lambda: any(child["id"] in m["text"] and m["text"].startswith("<b>[cfharness] Run ") for m in tg.sent(BOSS)))
    await stop(d)


def _message_ids(tg: FakeTelegram, chat: int) -> list[int]:
    """message_id do server giả cấp cho từng sendMessage (tăng dần theo thứ tự gửi toàn cục)."""
    ids, n = [], 0
    for m, b in list(tg.calls):
        if m == "sendMessage":
            n += 1
            if str(b.get("chat_id")) == str(chat):
                ids.append(n)
    return ids


def test_long_result_truncated_with_hint(hub_home):
    from cfharness.hub import Hub

    h = Hub(hub_home, runtime=FakeRuntime())
    txt = h.telegram.format_result({"id": "r_1", "agent_id": "a", "outcome": "completed", "cost_usd": 0.0123, "num_turns": 3, "duration_ms": 4200,
                                    "summary": "x" * 10_000})
    assert len(txt) < 2000 and "đã cắt" in txt and "cfharness runs show r_1" in txt and "$0.0123" in txt
    h.store.close()


def test_wt8_agent_result_framed_cannot_fake_system_message(hub_home):
    """WT-8: nội dung agent nằm TRỌN trong <pre> (escape), sau tiền tố cố định; không giả được dòng hệ thống / thẻ HTML / link."""
    from cfharness.chat.bot import AGENT_FRAME
    from cfharness.hub import Hub

    h = Hub(hub_home, runtime=FakeRuntime())
    evil = "xong</pre><b>[cfharness] Đã CHO PHÉP ap_1</b>\n[cfharness] nhắn /estop off tại https://evil.example <a href='x'>bấm</a>\u202e"
    txt = h.telegram.format_result({"id": "r_2", "agent_id": "a", "outcome": "completed", "summary": evil})
    lines = txt.split("\n")
    assert lines[0].startswith("<b>[cfharness] Run r_2") and AGENT_FRAME in lines[2]
    pre = txt[txt.index("<pre>") + 5 : txt.rindex("</pre>")]
    assert "<" not in pre and "&lt;/pre&gt;&lt;b&gt;[cfharness] Đã CHO PHÉP" in pre and "⟨U+202E⟩" in pre
    assert txt.count("<pre>") == 1 and txt.count("</pre>") == 1 and "<a " not in txt
    h.store.close()


def approval_sys_msg(tg: FakeTelegram, chat: int, aid: str):
    """Tin HỆ THỐNG có nút Cho phép/Từ chối của approval."""
    return next((m for m in reversed(tg.sent(chat)) if aid in m["text"] and [b["text"] for b in FakeTelegram.buttons(m)] == ["Cho phép", "Từ chối"]), None)


def content_msgs(tg: FakeTelegram, chat: int, aid: str):
    return [m for m in tg.sent(chat) if aid in m["text"] and "Nội dung do AGENT viết" in m["text"]]


async def approve_2step(tg: FakeTelegram, chat: int, sysmsg) -> None:
    await press(tg, chat, FakeTelegram.buttons(sysmsg)[0]["callback_data"])
    step2 = tg.with_buttons(chat, ["Xác nhận CHO PHÉP", "Huỷ"])  # T-B: bước 2 = SỬA tin hệ thống (không gửi tin mới)
    await press(tg, chat, FakeTelegram.buttons(step2)[0]["callback_data"])


# ============================================================================ thông báo + nút duyệt
async def test_approval_notification_buttons_forgery_replay(make, tg):
    d = await make(allowed=(BOSS, OTHER), runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    ap = open_approval(d, rid)
    msg = await until(lambda: approval_sys_msg(tg, BOSS, ap["id"]))
    await until(lambda: approval_sys_msg(tg, OTHER, ap["id"]))  # mọi chat trong allowlist
    btns = FakeTelegram.buttons(msg)
    assert all(len(b["callback_data"]) <= 64 for b in btns)
    approve, deny = btns[0]["callback_data"], btns[1]["callback_data"]
    await until(lambda: any(e.type == "notify.sent" and e.payload.get("channel") == "telegram" for e in d.hub.store.query_events(run_id=rid)))
    # giả mạo: dữ liệu bịa, chữ ký sai (nonce thật), nút của BOSS bấm từ chat OTHER, chat lạ
    v, nonce, sig = approve.split(".")
    for data, chat in (("v1.AAAAAAAAAAAAAAAA.0123456789abcdef", BOSS), (f"v1.{nonce}.{'0' * 16}", BOSS), (approve, OTHER), ("ap:" + ap["id"], BOSS)):
        await press(tg, chat, data)
    await press(tg, STRANGER, approve)
    assert d.hub.approvals.get(ap["id"])["status"] == "open"
    rej = d.hub.audit.query(kind="telegram.callback_rejected")
    assert {a["data"]["reason"] for a in rej} >= {"unknown_or_used", "bad_signature", "wrong_chat", "malformed"}
    # bấm thật (2 bước) → cho phép, audit kênh telegram:<chat> + from_id
    await approve_2step(tg, BOSS, msg)
    got = d.hub.approvals.get(ap["id"])
    assert got["status"] == "allowed" and got["resolved_channel"] == f"telegram:{BOSS}"
    # bấm lại (replay) + nút từ chối cùng nhóm → bị từ chối, không quyết lần 2
    await press(tg, BOSS, approve)
    await press(tg, BOSS, deny)
    assert d.hub.approvals.get(ap["id"])["status"] == "allowed"
    resolved = d.hub.audit.query(kind="approval.resolved")
    assert len(resolved) == 1 and resolved[0]["channel"] == f"telegram:{BOSS}"
    cmd = [a for a in d.hub.audit.query(kind="telegram.command") if a["data"]["method"] == "approval.approve"]
    assert cmd and cmd[0]["data"]["from_id"] == str(BOSS)
    ans = [b["text"] for b in tg.calls_of("answerCallbackQuery")]
    assert any("đã dùng" in t for t in ans) and any("Đã CHO PHÉP" in t for t in ans)
    # nút trên chat OTHER bị gỡ sau khi quyết (approval.resolved)
    edited = await until(lambda: next((b for b in tg.edits(OTHER) if ap["id"] in b["text"] and "CHO PHÉP" in b["text"]), None))
    assert not FakeTelegram.buttons(edited) and "bởi bạn qua Telegram" in edited["text"]  # T-B: sửa tại chỗ thành kết quả, gỡ nút
    await stop(d)


async def test_heavy_destructive_needs_two_step_confirm_and_cancel(make, tg):
    d = await make(runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    ap = open_approval(d, rid, klass="git", cmd="git push --force origin main")
    msg = await until(lambda: approval_sys_msg(tg, BOSS, ap["id"]))
    assert "NẶNG" in msg["text"] and any("git push --force" in m["text"] for m in content_msgs(tg, BOSS, ap["id"]))
    await press(tg, BOSS, FakeTelegram.buttons(msg)[0]["callback_data"])
    assert d.hub.approvals.get(ap["id"])["status"] == "open"  # bước 1 chưa cho phép
    n_sent = len(tg.sent(BOSS))
    step2 = tg.with_buttons(BOSS, ["Xác nhận CHO PHÉP", "Huỷ"])
    b2 = FakeTelegram.buttons(step2)
    assert step2 in tg.edits(BOSS) and "Xác nhận bước 2" in step2["text"] and "NẶNG" in step2["text"]  # T-B: sửa tại chỗ
    # Huỷ → vẫn chờ, tin hệ thống trở lại nút duyệt (nút MỚI), không gửi thêm tin
    await press(tg, BOSS, b2[1]["callback_data"])
    assert d.hub.approvals.get(ap["id"])["status"] == "open"
    again = tg.with_buttons(BOSS, ["Cho phép", "Từ chối"])
    assert again is not msg and again in tg.edits(BOSS) and len(tg.sent(BOSS)) == n_sent
    assert FakeTelegram.buttons(again)[0]["callback_data"] != FakeTelegram.buttons(msg)[0]["callback_data"]
    await approve_2step(tg, BOSS, again)
    assert d.hub.approvals.get(ap["id"])["status"] == "allowed"
    await stop(d)


async def test_wt5_file_class_rm_rf_needs_two_steps(make, tg):
    """WT-5 (tái hiện audit: trước đây 1 chạm → allowed). Nay mọi cho phép cần bước 2, như hộp xác nhận của web."""
    d = await make(runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    ap = open_approval(d, rid, klass="file", cmd="rm -rf ~/projects", fp="fp-f")
    msg = await until(lambda: approval_sys_msg(tg, BOSS, ap["id"]))
    await press(tg, BOSS, FakeTelegram.buttons(msg)[0]["callback_data"])
    assert d.hub.approvals.get(ap["id"])["status"] == "open"
    step2 = tg.with_buttons(BOSS, ["Xác nhận CHO PHÉP", "Huỷ"])
    await press(tg, BOSS, FakeTelegram.buttons(step2)[0]["callback_data"])
    assert d.hub.approvals.get(ap["id"])["status"] == "allowed"
    await stop(d)


async def test_wt2_full_command_not_truncated_and_targets_shown(make, tg):
    """WT-2 (tái hiện audit: lệnh đệm ~340 ký tự giấu `rm -rf` ở cuối). Nay tin nội dung chứa lệnh ĐẦY ĐỦ + đích khớp luật."""
    d = await make(runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    cmd = "echo '" + "chuan bi don dep thu muc tam an toan " * 9 + "' >/dev/null; rm -rf ~/Documents"
    run = d.hub.store.get_run(rid)
    ap = d.hub.approvals.open(run, tool_name="Bash", tool_input={"command": cmd}, tool_use_id="t", fingerprint="fp-trunc",
                              verdict={"klass": "file", "rule": "delete_not_run_created", "verdict": "destructive", "reason": "rm -r",
                                       "targets": ["~/Documents"]}, channel="hook")
    await until(lambda: approval_sys_msg(tg, BOSS, ap["id"]))
    body = "\n".join(m["text"] for m in content_msgs(tg, BOSS, ap["id"]))
    assert "rm -rf ~/Documents" in body and "Phần khớp luật (đích): ~/Documents" in body
    sysm = approval_sys_msg(tg, BOSS, ap["id"])
    assert "rm -rf" not in sysm["text"] and sysm.get("reply_to_message_id")  # nút KHÔNG nằm cùng nội dung agent (WT-8)
    for m in content_msgs(tg, BOSS, ap["id"]):
        assert not FakeTelegram.buttons(m) and m.get("parse_mode") == "HTML"
    # rất dài → nhiều tin / tệp đính kèm, vẫn đủ phần cuối
    long_cmd = "echo " + "A" * 9000 + " ; rm -rf ~/x"
    ap2 = d.hub.approvals.open(run, tool_name="Bash", tool_input={"command": long_cmd}, tool_use_id="t2", fingerprint="fp-l",
                               verdict={"klass": "file", "rule": "r", "verdict": "destructive", "reason": "x"}, channel="hook")
    await until(lambda: approval_sys_msg(tg, BOSS, ap2["id"]))
    parts = content_msgs(tg, BOSS, ap2["id"])
    assert len(parts) == 3 and "rm -rf ~/x" in parts[-1]["text"] and all(len(m["text"]) < 4096 for m in parts)
    huge = "echo " + "B" * 30000 + " ; rm -rf ~/y"
    ap3 = d.hub.approvals.open(run, tool_name="Bash", tool_input={"command": huge}, tool_use_id="t3", fingerprint="fp-h",
                               verdict={"klass": "file", "rule": "r", "verdict": "destructive", "reason": "x"}, channel="hook")
    await until(lambda: approval_sys_msg(tg, BOSS, ap3["id"]))
    docs = tg.calls_of("sendDocument")
    assert docs and b"rm -rf ~/y" in docs[-1]["_raw"] and "rm -rf ~/y" in content_msgs(tg, BOSS, ap3["id"])[-1]["text"]
    # MCP: đủ tham số
    ap4 = d.hub.approvals.open(run, tool_name="mcp__db__delete_rows", tool_input={"table": "customers", "where": "id > 0"}, tool_use_id="t4",
                               fingerprint="fp-m", verdict={"klass": "mcp", "rule": "mcp_destructive", "verdict": "destructive", "reason": "x"}, channel="hook")
    await until(lambda: approval_sys_msg(tg, BOSS, ap4["id"]))
    mtxt = "\n".join(m["text"] for m in content_msgs(tg, BOSS, ap4["id"]))
    assert "mcp__db__delete_rows" in mtxt and "customers" in mtxt and "id &gt; 0" in mtxt
    await stop(d)


async def test_wt4_group_requires_allowed_user_and_explicit_opt_in(make, tg):
    """WT-4 (tái hiện audit: người lạ trong nhóm bấm Cho phép → allowed). Nay chỉ from.id thuộc allowed_user_ids."""
    GROUP = -1001234
    d = await make(allowed=(GROUP,), runtime=FakeRuntime(hold=True), tg_cfg={"allow_groups": True, "allowed_user_ids": [str(BOSS)]})
    rid = await held_run(d)
    ap = open_approval(d, rid)
    msg = await until(lambda: approval_sys_msg(tg, GROUP, ap["id"]))
    approve = FakeTelegram.buttons(msg)[0]["callback_data"]
    await press(tg, GROUP, approve, from_id=777777)  # người lạ trong nhóm
    assert d.hub.approvals.get(ap["id"])["status"] == "open"
    ign = d.hub.audit.query(kind="telegram.ignored")
    assert ign and ign[0]["data"]["from_id"] == "777777"
    n = len(tg.sent(GROUP))
    tg.user_message(GROUP, "/estop on", chat_type="supergroup")  # from.id = GROUP (không phải người được phép)
    await say(tg, GROUP, "/status", chat_type="supergroup")
    assert len(tg.sent(GROUP)) == n and d.hub.runs.estop.get("on") is not True
    # chủ (BOSS) bấm trong nhóm → được (nút chưa bị người lạ làm tiêu)
    await approve_2step_from(tg, GROUP, msg, BOSS)
    assert d.hub.approvals.get(ap["id"])["status"] == "allowed"
    assert auth.telegram_caller(d.hub, GROUP, BOSS).human and not auth.telegram_caller(d.hub, GROUP, 777777).human
    await stop(d)


async def test_wt4_groups_off_by_default(make, tg):
    """Mặc định allow_groups=false: nhóm trong allowlist vẫn không phải người, không nhận thông báo, không thêm được."""
    GROUP = -1001234
    d2 = await make(allowed=(GROUP,), tg_cfg={"allowed_user_ids": [str(BOSS)]})
    assert not auth.telegram_caller(d2.hub, GROUP, BOSS).human and d2.hub.telegram.notify_chats() == set()
    with pytest.raises(AssertionError, match="allow_groups"):
        await rpc(d2, "telegram.allow", {"chat_id": str(GROUP), "user_id": str(BOSS)})
    await stop(d2)


async def approve_2step_from(tg: FakeTelegram, chat: int, sysmsg, from_id: int) -> None:
    await press(tg, chat, FakeTelegram.buttons(sysmsg)[0]["callback_data"], from_id=from_id)
    step2 = tg.with_buttons(chat, ["Xác nhận CHO PHÉP", "Huỷ"])
    await press(tg, chat, FakeTelegram.buttons(step2)[0]["callback_data"], from_id=from_id)


async def test_approvals_command_and_deny_button(make, tg):
    d = await make(runtime=FakeRuntime(hold=True), tg_cfg={"events": []})  # tắt thông báo đẩy: chỉ /approvals
    rid = await held_run(d)
    ap = open_approval(d, rid)
    await until(lambda: tg.calls_of("getUpdates"))
    await say(tg, BOSS, "/approvals")
    msg = approval_sys_msg(tg, BOSS, ap["id"])
    await press(tg, BOSS, FakeTelegram.buttons(msg)[1]["callback_data"])
    got = d.hub.approvals.get(ap["id"])
    assert got["status"] == "denied" and got["resolved_channel"] == f"telegram:{BOSS}" and "Telegram" in (got["note"] or "")
    await stop(d)


async def test_notify_failure_retries_with_backoff(make, tg):
    tg.fail_send = 1
    d = await make()
    d.hub.publish(make_event("guard.blocked", "coder", {"detail": "chặn ghi policy/", "preview": "echo > policy/x"}, run_id="r9"))
    await until(lambda: any("guard chặn" in m["text"] for m in tg.sent(BOSS)))
    await until(lambda: any(e.type == "notify.sent" for e in d.hub.store.query_events(run_id="r9")))
    evs = [e for e in d.hub.store.query_events(run_id="r9") if e.type in ("notify.failed", "notify.sent")]
    assert [e.type for e in evs] == ["notify.failed", "notify.sent"] and all(e.payload["channel"] == "telegram" for e in evs)
    await stop(d)


# ============================================================================ secret
async def test_secrets_never_sent_or_stored(make, tg, hub_home):
    d = await make(runtime=FakeRuntime(hold=True))
    d.hub.secrets.set("DB_PASSWORD", "hunter2-SUPER-secret-value")
    rid = await held_run(d)
    ap = open_approval(d, rid, cmd="psql postgres://u:hunter2-SUPER-secret-value@db/x -c 'drop table t'")
    await until(lambda: approval_sys_msg(tg, BOSS, ap["id"]))
    txt = "\n".join(m["text"] for m in content_msgs(tg, BOSS, ap["id"]))
    assert "hunter2-SUPER-secret-value" not in txt and "***" in txt
    # format_event cắt dữ liệu dài (vd kết quả MCP thô trong preview)
    long = format_event(make_event("approval.open", "a", {"request_id": "ap_l", "preview": "mcp__vb__dump " + "d" * 5000}, run_id="r"))
    assert len(long) < 1000
    await say(tg, BOSS, "/status")
    st = await rpc(d, "telegram.status")
    assert TOKEN not in str(st)
    # token không nằm trong bất kỳ tin nào gửi đi, cũng không trong hub.db / log (ngoài secrets/)
    for _, body in tg.calls:
        assert TOKEN not in str(body)
    await stop(d)
    hits = []
    for p in hub_home.home.rglob("*"):
        if p.is_file() and "secrets" not in p.parts:
            if TOKEN.encode() in p.read_bytes():
                hits.append(str(p))
    assert not hits, hits
    # lỗi mạng có URL chứa token → gỡ token khỏi thông báo lỗi
    api = TelegramApi("http://127.0.0.1:1", TOKEN, timeout=0.5)
    with pytest.raises(TelegramError) as ei:
        await api.call("getMe")
    assert TOKEN not in str(ei.value)


async def test_agent_in_run_cannot_read_bot_token(make, tg, hub_home):
    rt = FakeRuntime()
    d = await make(runtime=rt)
    d.hub.agents.create({"id": "coder", "name": "Coder"})
    r = d.hub.runs.submit("coder", "đọc token bot")
    await d.hub.runs.wait(r["id"], 10)
    spec = rt.started[-1]
    assert TOKEN not in str(spec.env_extra) and TOKEN not in str(spec)
    tok_file = hub_home.home / "secrets" / "TELEGRAM_BOT_TOKEN"
    assert tok_file.exists() and (sys.platform == "win32" or oct(tok_file.stat().st_mode & 0o777) == "0o600")  # Windows: ACL hồ sơ người dùng
    # sandbox Bash: denyRead secrets/ ; tool Read: permissions.deny
    s = build_settings({}, run_id="r1", socket=hub_home.socket, policy_dir=hub_home.home / "policy", guard_cfg={"hook_timeout_s": 900},
                       approval_timeout_s=300, cwd=hub_home.home / "agents" / "coder" / "workspaces" / "r1")
    assert sb_path(hub_home.home / "secrets") in s["sandbox"]["filesystem"]["denyRead"]
    assert f"Read(/{rule_path(hub_home.home)}/secrets/**)" in s["permissions"]["deny"]
    # bộ phân loại: đọc file token → forbidden
    v = classify("Bash", {"command": f"cat {tok_file}"}, gctx(hub_home, hub_home.home / "agents" / "coder"))
    assert v.forbidden and v.rule == "hub_secret_read", v
    await stop(d)
