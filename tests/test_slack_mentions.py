"""Tag bot trong kênh Slack (`app_mention`) → điều phối viên, trả lời trong thread. Cùng cổng người như DM (workspace + allowlist kênh VÀ
người, kênh chỉ khi `allow_channels`); approval / câu hỏi chủ động chỉ vào DM. Slack giả (tests/slack_fake.py), daemon in-process."""

from __future__ import annotations

from cfharness.chat.slack.bot import strip_mention
from cfharness.runtime import FakeRuntime
from cfharness.web import app as webapp

from slack_fake import BOT_USER, FakeSlack
from test_slack import (BOSS, DM_BOSS, STRANGER, approval_msg, deliver, held_run, make, open_approval, rpc, sk,  # noqa: F401
                        until)

CH = "C0TEAM0001"


async def mk(make, sk: FakeSlack, *, channels: bool = True, runtime=None):
    from cfharness.web import manager_stub

    d = await make(chats=(DM_BOSS, CH), sl_cfg={"allow_channels": channels}, runtime=runtime)
    manager_stub.install(d)
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    return d


def boss_runs(d) -> list[dict]:
    return d.hub.store.list_runs(agent_id="boss")


def conv_of(d) -> str | None:
    return (d.hub.store.state_get("slack.mgr_conv") or {}).get(CH)


def ids(d) -> set[str]:
    return {r["id"] for r in boss_runs(d)}


def replies(sk: FakeSlack, root: str) -> list[dict]:
    return [m for m in sk.sent(CH) if m.get("thread_ts") == root and "Điều phối viên" in sk.text_of(m)]


def test_strip_mention():
    assert strip_mention(f"<@{BOT_USER}> làm báo cáo", BOT_USER) == "làm báo cáo"
    assert strip_mention(f"<@{BOT_USER}|cfharness>  hỏi <@U0OTHER01> giúp", BOT_USER) == "hỏi <@U0OTHER01> giúp"
    assert strip_mention("<@U0XYZ> việc", None) == "việc"
    assert strip_mention("dòng 1\n<@U0BOT0001> dòng 2", BOT_USER) == "dòng 1\n dòng 2"


def test_manifest_subscribes_app_mention_only():
    m = webapp.slack_2way_manifest()
    assert "app_mention" in m["settings"]["event_subscriptions"]["bot_events"]
    assert "app_mentions:read" in m["oauth_config"]["scopes"]["bot"]
    assert "message.channels" not in m["settings"]["event_subscriptions"]["bot_events"]  # không đọc cả kênh
    assert "channels:history" not in m["oauth_config"]["scopes"]["bot"]


async def test_mention_in_channel_goes_to_manager_and_replies_in_thread(make, sk):
    d = await mk(make, sk)
    eid = sk.mention(CH, BOSS, f"<@{BOT_USER}> làm báo cáo tuần")
    await deliver(sk, eid)
    run = (await until(lambda: boss_runs(d)))[0]
    assert "làm báo cáo tuần" in run["prompt"] and f"<@{BOT_USER}>" not in run["prompt"]
    reply = (await until(lambda: [m for m in sk.sent(CH) if "Điều phối viên" in sk.text_of(m)], timeout=15))[0]
    assert reply.get("thread_ts")  # trả lời trong thread của tin đã tag, không đăng lên gốc kênh
    conv1 = conv_of(d)
    assert conv1

    # tag lần nữa trong thread đó → tiếp tục đúng cuộc
    before = ids(d)
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> thêm phần chi phí", thread_ts=reply["thread_ts"]))
    await until(lambda: ids(d) - before)
    assert conv_of(d) == conv1
    await until(lambda: len(replies(sk, reply["thread_ts"])) >= 2, timeout=15)

    # tag mới ở gốc kênh → cuộc trò chuyện MỚI
    before = ids(d)
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> việc khác"))
    await until(lambda: ids(d) - before)
    assert conv_of(d) != conv1


async def test_mention_from_stranger_ignored(make, sk):
    d = await mk(make, sk)
    await deliver(sk, sk.mention(CH, STRANGER, f"<@{BOT_USER}> xoá hết"))
    assert not boss_runs(d) and not sk.sent(CH)
    rows = d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='slack.ignored'").fetchall()
    assert rows


async def test_mention_ignored_when_channels_off(make, sk):
    d = await mk(make, sk, channels=False)
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> làm gì đó"))
    assert not boss_runs(d) and not sk.sent(CH)


async def test_mention_other_workspace_ignored(make, sk):
    d = await mk(make, sk)
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> làm", team="T0OTHER"))
    assert not boss_runs(d) and not sk.sent(CH)


async def test_approvals_only_go_to_dm_not_channel(make, sk):
    d = await mk(make, sk, runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    ap = open_approval(d, rid)
    await until(lambda: approval_msg(sk, DM_BOSS, ap["id"]) if any(sk.buttons(m) for m in sk.sent(DM_BOSS)) else None)
    assert not any(sk.buttons(m) for m in sk.sent(CH))
    assert d.hub.slack.notify_chats() == {DM_BOSS, CH} and d.hub.slack.broadcast_chats() == {DM_BOSS}


async def test_any_channel_works_without_pairing(make, sk):
    from cfharness import auth
    from cfharness.web import manager_stub

    d = await make(chats=(DM_BOSS,), sl_cfg={"allow_channels": True})  # kênh KHÔNG nằm trong allowlist
    manager_stub.install(d)
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    assert auth.slack_caller(d.hub, CH, BOSS).human and not auth.slack_caller(d.hub, CH, STRANGER).human
    assert not auth.slack_caller(d.hub, "D0NOTPAIRD", BOSS).human  # DM vẫn phải ghép nối
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> làm báo cáo"))
    await until(lambda: boss_runs(d))
    reply = (await until(lambda: [m for m in sk.sent(CH) if "Điều phối viên" in sk.text_of(m)], timeout=15))[0]
    assert reply.get("thread_ts")
    assert CH in d.hub.slack.notify_chats() and d.hub.slack.broadcast_chats() == {DM_BOSS}
    await deliver(sk, sk.mention(CH, STRANGER, f"<@{BOT_USER}> xoá hết"))
    assert len(boss_runs(d)) == 1
