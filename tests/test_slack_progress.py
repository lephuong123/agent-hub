"""P7 — điều phối viên đang làm việc cho một tin Slack: trạng thái "đang xử lý…" (setStatus, lỗi → reaction ⏳), MỘT tin tiến độ sửa tại
chỗ (tên tool + nhãn hub, không input tool) có nút "Dừng". Slack giả (tests/slack_fake.py), daemon in-process + FakeRuntime + manager stub."""

from __future__ import annotations

from cfharness.chat.base import ChatBot
from cfharness.chat.slack.progress import REACTION, STATUS_TEXT, tool_label
from cfharness.events import make_event
from cfharness.runtime import FakeRuntime

from slack_fake import BOT_USER, FakeSlack
from test_slack import BOSS, DM_BOSS, STRANGER, deliver, make, rpc, sk, until  # noqa: F401

CH = "C0TEAM0001"


async def mk(make, sk: FakeSlack, *, hold: bool = True, edit_s: float = 0.2):
    from cfharness.web import manager_stub

    d = await make(chats=(DM_BOSS,), sl_cfg={"allow_channels": True, "manager_progress_edit_s": edit_s},
                   runtime=FakeRuntime(hold=hold))
    manager_stub.install(d)
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    return d


def last_ts(sk: FakeSlack) -> str:
    with sk.cond:
        return f"{sk.next_ts}.000100"  # ts của tin người vừa bơm (FakeSlack._ts)


async def tag(sk: FakeSlack, text: str, *, user: str = BOSS) -> str:
    eid = sk.mention(CH, user, f"<@{BOT_USER}> {text}")
    ts = last_ts(sk)
    await deliver(sk, eid)
    return ts


def progress_msgs(sk: FakeSlack, ch: str) -> list[dict]:
    return [m for m in sk.sent(ch) if "Đang xử lý" in sk.text_of(m)]


def updates(sk: FakeSlack, ts: str) -> list[dict]:
    return [p for p in sk.calls_of("chat.update") if p.get("ts") == ts]


def boss_run(d) -> dict:
    return d.hub.store.list_runs(agent_id="boss")[0]


def test_tool_label_never_uses_input():
    assert tool_label("Bash") == ("Bash", "chạy lệnh")
    assert tool_label("mcp__github__create_issue") == ("github.create_issue", "công cụ MCP")
    assert tool_label("<!channel> `x`") == ("channelx", "dùng công cụ")  # ký tự lạ bị lọc, nhãn hub cho tool lạ
    assert tool_label(None)[0] == "?"


def test_telegram_untouched():
    from cfharness.chat import manager_chat
    from cfharness.chat.bot import TelegramBot
    from cfharness.chat.slack.bot import SlackBot

    assert manager_chat.TYPING_EVERY_S == 4.0 and manager_chat.TYPING_MAX_S == 180.0
    assert getattr(TelegramBot, "TYPING_EVERY_S", None) is None  # Telegram vẫn nhắc "đang gõ" mỗi 4 s
    assert TelegramBot.manager_run_started is ChatBot.manager_run_started  # hook rỗng
    assert TelegramBot.manager_run_finished is ChatBot.manager_run_finished
    assert SlackBot.TYPING_EVERY_S == 90.0


async def test_status_progress_updated_and_collapsed(make, sk):
    d = await mk(make, sk)
    ts = await tag(sk, "làm báo cáo")
    prog = (await until(lambda: progress_msgs(sk, CH)))[0]
    assert prog["thread_ts"] == ts  # trong thread của tin đã tag
    btns = sk.buttons(prog)
    assert len(btns) == 1 and "Dừng" in btns[0]["text"]["text"]

    # trạng thái "đang xử lý…" đúng thread
    st = await until(lambda: sk.calls_of("assistant.threads.setStatus"))
    assert st[0] == {"channel_id": CH, "thread_ts": ts, "status": STATUS_TEXT}

    # bước tool hiện tên + nhãn hub, KHÔNG có input/args_preview
    rid = boss_run(d)["id"]
    await until(lambda: "Bash" in sk.text_of(prog) or any("Bash" in sk.text_of(u) for u in updates(sk, prog["_ts"])))
    d.hub.publish(make_event("tool.start", "boss", {"tool_id": "t_x9", "name": "mcp__github__create_issue",
                                                    "input": {"title": "BI-MAT-INPUT"}, "args_preview": "BI-MAT-INPUT"}, run_id=rid))
    up = (await until(lambda: [u for u in updates(sk, prog["_ts"]) if "github.create_issue" in sk.text_of(u)]))[-1]
    body = sk.text_of(up)
    assert "BI-MAT-INPUT" not in body and "công cụ MCP" in body and "chạy lệnh" in body
    assert sk.buttons(up)  # còn nút Dừng khi đang chạy
    assert not [m for m in sk.sent(CH) if "BI-MAT-INPUT" in str(m)]

    # xong → trả lời trong thread, tin tiến độ thu về một dòng, gỡ nút, xoá trạng thái
    d._rt.release()
    await until(lambda: [m for m in sk.sent(CH) if "Điều phối viên" in sk.text_of(m) and m.get("thread_ts") == ts], timeout=15)
    fin = (await until(lambda: [u for u in updates(sk, prog["_ts"]) if "Xong" in sk.text_of(u)]))[-1]
    assert not sk.buttons(fin) and "\n" not in sk.text_of(fin).strip() and "bước" in sk.text_of(fin)
    await until(lambda: [c for c in sk.calls_of("assistant.threads.setStatus") if c.get("status") == ""])
    assert not sk.calls_of("reactions.add")
    assert not d.hub.slack.progress.recs


async def test_fallback_reaction_added_and_removed(make, sk):
    d = await mk(make, sk)
    sk.fail_status = True
    eid = sk.user_message(DM_BOSS, BOSS, "tóm tắt giúp")
    ts = last_ts(sk)
    await deliver(sk, eid)
    add = (await until(lambda: sk.calls_of("reactions.add")))[0]
    assert add == {"channel": DM_BOSS, "timestamp": ts, "name": REACTION}
    assert not sk.calls_of("reactions.remove")
    d._rt.release()
    rm = (await until(lambda: sk.calls_of("reactions.remove"), timeout=15))[0]
    assert rm == {"channel": DM_BOSS, "timestamp": ts, "name": REACTION}
    assert len(sk.calls_of("reactions.add")) == 1


async def test_no_status_no_reaction_gives_up_quietly(make, sk):
    d = await mk(make, sk)
    sk.fail_status = sk.fail_reactions = True
    eid = sk.user_message(DM_BOSS, BOSS, "làm đi")
    ts = last_ts(sk)
    await deliver(sk, eid)
    await until(lambda: sk.calls_of("reactions.add"))
    d._rt.release()
    await until(lambda: [m for m in sk.sent(DM_BOSS) if "Điều phối viên" in sk.text_of(m) and m.get("thread_ts") == ts], timeout=15)
    assert not sk.calls_of("reactions.remove")  # chưa gắn được thì không gỡ


async def test_stop_button_stops_run_and_stranger_cannot(make, sk):
    d = await mk(make, sk)
    await tag(sk, "việc dài")
    prog = (await until(lambda: progress_msgs(sk, CH)))[0]
    btn = sk.buttons(prog)[0]
    rid = boss_run(d)["id"]
    await until(lambda: d.hub.store.get_run(rid)["status"] == "running")

    # người lạ trong kênh: bị bỏ qua, nút KHÔNG bị tiêu, run vẫn chạy
    await deliver(sk, sk.press(CH, STRANGER, btn["value"], prog["_ts"]))
    assert d.hub.store.get_run(rid)["status"] == "running"
    assert sk.ref_of(btn) == rid
    assert d.hub.store.conn.execute("SELECT 1 FROM audit_log WHERE kind='slack.ignored'").fetchall()

    await deliver(sk, sk.press(CH, BOSS, btn["value"], prog["_ts"]))
    await until(lambda: d.hub.store.get_run(rid)["status"] == "cancelled", timeout=15)
    stopping = await until(lambda: [u for u in updates(sk, prog["_ts"]) if "Đang dừng" in sk.text_of(u)])
    assert not sk.buttons(stopping[0])
    fin = await until(lambda: [u for u in updates(sk, prog["_ts"]) if "Đã dừng" in sk.text_of(u)], timeout=15)
    assert not sk.buttons(fin[-1])
    rows = d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='slack.command'").fetchall()
    assert any("run.stop" in r[0] for r in rows)
    # bấm lại nút cũ → đã tiêu
    n = len(sk.calls_of("chat.postEphemeral"))
    await deliver(sk, sk.press(CH, BOSS, btn["value"], prog["_ts"]))
    await until(lambda: len(sk.calls_of("chat.postEphemeral")) > n)
    assert "đã dùng" in sk.calls_of("chat.postEphemeral")[-1]["text"]


async def test_updates_coalesced_and_respect_429(make, sk):
    d = await mk(make, sk, edit_s=0.6)
    await tag(sk, "nhiều bước")
    prog = (await until(lambda: progress_msgs(sk, CH)))[0]
    rid = boss_run(d)["id"]
    sk.ratelimit["chat.update"] = 1
    n0 = len(updates(sk, prog["_ts"]))
    for i in range(6):
        d.hub.publish(make_event("tool.start", "boss", {"tool_id": f"t_c{i}", "name": "Read", "input": {"file_path": f"/x/{i}"}}, run_id=rid))
    await until(lambda: sk.calls_of("chat.update#429"))
    last = (await until(lambda: [u for u in updates(sk, prog["_ts"]) if "7 bước" in sk.text_of(u) or "8 bước" in sk.text_of(u)], timeout=10))[-1]
    assert "/x/" not in sk.text_of(last) and "đọc tệp" in sk.text_of(last)
    assert len(updates(sk, prog["_ts"])) - n0 <= 2  # 6 event → 1 lần sửa (sau khi chờ Retry-After)
    d._rt.release()
    await until(lambda: [u for u in updates(sk, prog["_ts"]) if "Xong" in sk.text_of(u)], timeout=15)
