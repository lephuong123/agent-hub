"""Slack 2 chiều — Socket Mode + duyệt bằng nút + lệnh /cfh + trả lời trong thread. Slack GIẢ cục bộ (tests/slack_fake.py),
daemon IN-PROCESS (`Daemon` + FakeRuntime), không gọi Slack/claude thật."""

from __future__ import annotations

import asyncio
import time

import pytest
import yaml

from cfharness import auth
from cfharness.chat.slack import render
from cfharness.daemon import Daemon
from cfharness.errors import BLOCKED
from cfharness.guard.classifier import GuardContext, classify
from cfharness.runtime import FakeRuntime

from slack_fake import APP_TOKEN, BOT_TOKEN, TEAM, FakeSlack

BOSS, OTHER, STRANGER = "U0BOSS001", "U0OTHER01", "U0STRANGE"
DM_BOSS, DM_OTHER, DM_STRANGER = "D0BOSS001", "D0OTHER01", "D0STRANGE"


async def until(fn, timeout: float = 10.0, step: float = 0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = fn()
        if v:
            return v
        await asyncio.sleep(step)
    raise AssertionError("hết giờ chờ điều kiện")


@pytest.fixture
def sk():
    f = FakeSlack()
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
async def make(hub_home, sk):
    daemons: list[Daemon] = []

    async def _m(*, users=(BOSS,), chats=(DM_BOSS,), token=True, interactive=True, runtime=None, sl_cfg=None, cfg=None, connect=True):
        c = {"guard": {"git_probe": False},
             "notify": {"slack": {"api_base": sk.url, "interactive": interactive, "idle_check_s": 0.05, "backoff_base_s": 0.05,
                                  "team_id": TEAM, "allowed_chat_ids": list(chats), "allowed_user_ids": list(users), **(sl_cfg or {})}}}
        c.update(cfg or {})
        hub_home.config.write_text(yaml.safe_dump(c))
        rt = runtime or FakeRuntime()
        d = Daemon(hub_home, runtime=rt)
        d._rt = rt  # type: ignore[attr-defined]
        if token:
            d.hub.secrets.set("SLACK_BOT_TOKEN", BOT_TOKEN)
            d.hub.secrets.set("SLACK_APP_TOKEN", APP_TOKEN)
        d.hub.start()
        daemons.append(d)
        _BOTS.append(d.hub.slack)
        sk.hubs.append(d.hub)
        if token and interactive and connect:
            await until(lambda: d.hub.slack.info.get("polling") and sk.connected())
        return d

    yield _m
    _BOTS.clear()
    for d in daemons:
        try:
            await stop(d)
        except Exception:
            pass


async def rpc(d: Daemon, method: str, params: dict | None = None):
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    if "error" in resp:
        raise AssertionError(resp["error"])
    return resp["result"]


_BOTS: list = []  # bot Slack của các daemon test đang chạy (deliver chờ hàng đợi của chúng xử lý xong)


def _idle() -> bool:
    return all(b._inbox is None or b._inbox._unfinished_tasks == 0 for b in _BOTS)  # type: ignore[attr-defined]


async def deliver(sk: FakeSlack, eid: str) -> None:
    """ACK tới TRƯỚC khi xử lý → chờ worker tiêu hết hàng đợi (không ngủ cố định: máy tải nặng thì chậm hơn), rồi chờ thêm chút
    cho tác vụ nền do handler sinh (gửi tin…)."""
    await until(lambda: sk.acked(eid))
    await asyncio.sleep(0.05)
    await until(_idle, timeout=20)
    await asyncio.sleep(0.1)


def texts(sk: FakeSlack, ch: str) -> list[str]:
    return [sk.text_of(m) for m in sk.sent(ch)]


def open_approval(d: Daemon, run_id: str, *, klass: str = "file", cmd: str = "rm -rf dummy", fp: str = "fp1"):
    run = d.hub.store.get_run(run_id)
    return d.hub.approvals.open(run, tool_name="Bash", tool_input={"command": cmd}, tool_use_id="t1", fingerprint=fp,
                                verdict={"klass": klass, "rule": "delete_not_run_created", "verdict": "destructive", "reason": "rm -r thư mục có sẵn",
                                         "protected_id": None}, channel="hook")


async def held_run(d: Daemon, agent: str = "coder") -> str:
    d.hub.agents.create({"id": agent, "name": agent})
    r = d.hub.runs.submit(agent, "làm việc")
    await until(lambda: d.hub.store.get_run(r["id"])["status"] == "running")
    return r["id"]


def approval_msg(sk: FakeSlack, ch: str, aid: str) -> dict:
    m = sk.msg_for(ch, aid)  # tin gọn không in id → tra qua nút
    assert m is not None
    return m


# ============================================================================ render (mrkdwn injection)
def test_render_escapes_and_keeps_agent_inside_code():
    md = render.html_to_mrkdwn("<b>[cfharness] Yêu cầu ap_1</b>\n<pre>" + render.escape("<!channel> <@U1> <http://x|bấm> ```thoát``` *đậm*") + "</pre>")
    assert "<!channel>" not in md and "<@U1>" not in md and "<http" not in md
    assert "&lt;!channel&gt;" in md
    body = md.split("```\n", 1)[1].rsplit("\n```", 1)[0]
    assert "```" not in body  # agent không đóng được khối mã
    plain = render.message("a <!here> & b")
    assert "<!here>" not in plain["blocks"][0]["text"]["text"] and plain["blocks"][0]["text"]["verbatim"] is True


def test_render_splits_long_code_block():
    md = "<pre>" + "\n".join(f"dòng {i} " + "x" * 80 for i in range(120)) + "</pre>"
    parts = render.split_sections(render.html_to_mrkdwn(md))
    assert len(parts) > 1 and all(len(p) <= 3000 for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)  # mỗi đoạn tự đóng khối mã


# ============================================================================ cổng người
async def test_silent_when_not_configured(make, sk):
    d = await make(token=False)
    await asyncio.sleep(0.3)
    assert not d.hub.slack.info["polling"] and sk.calls == []


async def test_silent_when_interactive_off(make, sk):
    d = await make(interactive=False)  # có token nhưng chưa bật 2 chiều (chế độ webhook một chiều cũ)
    await asyncio.sleep(0.3)
    assert not d.hub.slack.info["polling"] and sk.calls_of("apps.connections.open") == []


async def test_slack_caller_gate(make):
    d = await make(users=(BOSS,), chats=(DM_BOSS, "C0TEAM001"))
    assert auth.slack_caller(d.hub, DM_BOSS, BOSS).human
    assert not auth.slack_caller(d.hub, DM_BOSS, STRANGER).human  # người lạ trong DM (không thể với DM thật) vẫn không phải người
    assert not auth.slack_caller(d.hub, DM_STRANGER, BOSS).human
    assert not auth.slack_caller(d.hub, "C0TEAM001", BOSS).human  # kênh chung chưa bật allow_channels
    assert not auth.slack_caller(d.hub, "C0OTHER01", BOSS).human
    resp = await d.call_as(auth.slack_caller(d.hub, DM_STRANGER, STRANGER), "estop.set", {"on": True})
    assert resp["error"]["code"] == BLOCKED


async def test_stranger_ignored_and_audited(make, sk):
    d = await make()
    await deliver(sk, sk.user_message(DM_STRANGER, STRANGER, "chào"))
    await deliver(sk, sk.slash(STRANGER, "trangthai"))
    assert sk.sent(DM_STRANGER) == []
    kinds = [a["kind"] for a in d.hub.audit.query(limit=50)]
    assert "slack.ignored" in kinds


async def test_other_workspace_ignored(make, sk):
    await make()
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "/cfh trangthai", team="T0EVIL001"))
    await deliver(sk, sk.slash(BOSS, "trangthai", team="T0EVIL001"))
    assert sk.sent(DM_BOSS) == []


def test_agent_cannot_run_slack_cli(hub_home):
    from cfharness.guard.policy import PolicyStore

    ctx = GuardContext(hub_home.home, PolicyStore(hub_home.home).load(), hub_socket=str(hub_home.socket), git_probe=False)
    v = classify("Bash", {"command": "cfharness notify slack allow D0EVIL001 --user-id U0EVIL001"}, ctx)
    assert v.forbidden, v


# ============================================================================ lệnh
async def test_slash_status_goes_to_dm(make, sk):
    await make()
    eid = sk.slash(BOSS, "trangthai")
    await deliver(sk, eid)
    out = texts(sk, DM_BOSS)
    assert any("ESTOP" in t for t in out)
    await deliver(sk, sk.slash(BOSS, "khonglenh"))
    assert any("Không có lệnh /cfh khonglenh" in t for t in texts(sk, DM_BOSS))


async def test_estop_on_off_two_step(make, sk):
    d = await make()
    await deliver(sk, sk.slash(BOSS, "estop on bảo trì"))
    assert d.hub.runs.estop.get("on")
    audit = [a for a in d.hub.audit.query(kind="estop.set", limit=5)]
    assert audit and audit[0]["channel"] == f"slack:{DM_BOSS}"
    await deliver(sk, sk.slash(BOSS, "estop off"))
    m = sk.sent(DM_BOSS)[-1]
    btn = next(b for b in sk.buttons(m) if "TẮT" in b["text"]["text"])
    await deliver(sk, sk.press(DM_BOSS, BOSS, btn["value"], m["_ts"]))
    assert d.hub.runs.estop.get("on") is not True


# ============================================================================ duyệt bằng nút
async def test_approval_buttons_two_step(make, sk):
    d = await make(runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    ap = open_approval(d, rid)
    await until(lambda: any(sk.buttons(m) for m in sk.sent(DM_BOSS)))
    msgs = sk.sent(DM_BOSS)
    content = next(m for m in msgs if not sk.buttons(m))
    assert "rm -rf dummy" in sk.text_of(content) and "```" in sk.text_of(content)
    sysmsg = approval_msg(sk, DM_BOSS, ap["id"])
    assert sysmsg.get("thread_ts")  # tin có nút nằm trong thread của tin nội dung (WT-8)
    assert "rm -rf dummy" not in sk.text_of(sysmsg)
    allow = next(b for b in sk.buttons(sysmsg) if b["text"]["text"] == "Cho phép")
    ts = sysmsg["_ts"]
    await deliver(sk, sk.press(DM_BOSS, BOSS, allow["value"], ts))
    upd = sk.calls_of("chat.update")[-1]
    assert upd["ts"] == ts and "Chắc chắn cho phép" in sk.text_of(upd)
    confirm = next(b for b in sk.buttons(upd) if b["text"]["text"] == "Xác nhận CHO PHÉP")
    await deliver(sk, sk.press(DM_BOSS, BOSS, confirm["value"], ts))
    row = d.hub.approvals.get(ap["id"])
    assert row["status"] == "allowed" and row.get("resolved_channel") == f"slack:{DM_BOSS}"


async def test_buttons_reject_forged_reused_and_stranger(make, sk):
    d = await make(users=(BOSS, OTHER), chats=(DM_BOSS, DM_OTHER), runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    ap = open_approval(d, rid)
    await until(lambda: any(sk.buttons(m) for m in sk.sent(DM_BOSS)))
    deny = next(b for b in sk.buttons(approval_msg(sk, DM_BOSS, ap["id"])) if b["text"]["text"] == "Từ chối")
    # bịa / sai chữ ký / nút của DM người khác
    await deliver(sk, sk.press(DM_BOSS, BOSS, "v1.bịa.0000000000000000", "1"))
    v1, nonce, sig = deny["value"].split(".")
    await deliver(sk, sk.press(DM_BOSS, BOSS, f"v1.{nonce}.{'0' * 16}", "1"))
    await deliver(sk, sk.press(DM_OTHER, OTHER, deny["value"], "1"))
    await deliver(sk, sk.press(DM_BOSS, STRANGER, deny["value"], "1"))  # người lạ bấm: không tiêu nút
    assert d.hub.approvals.get(ap["id"])["status"] in ("open", "waiting_human")
    reasons = [a["data"].get("reason") for a in d.hub.audit.query(kind="slack.callback_rejected", limit=20)]
    assert {"malformed", "unknown_or_used", "bad_signature", "wrong_chat"} & set(reasons)
    await deliver(sk, sk.press(DM_BOSS, BOSS, deny["value"], "1"))
    assert d.hub.approvals.get(ap["id"])["status"] == "denied"
    await deliver(sk, sk.press(DM_BOSS, BOSS, deny["value"], "1"))  # bấm lại
    assert any("đã dùng" in str(p.get("text")) for p in sk.calls_of("chat.postEphemeral"))


async def test_resolved_elsewhere_clears_buttons(make, sk):
    d = await make(runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    ap = open_approval(d, rid)
    await until(lambda: any(sk.buttons(m) for m in sk.sent(DM_BOSS)))
    await rpc(d, "approval.deny", {"id": ap["id"], "channel": "cli", "by": "user"})
    upd = await until(lambda: [u for u in sk.calls_of("chat.update") if "Đã từ chối" in sk.text_of(u)])
    assert not sk.buttons(upd[-1])


async def test_webhook_notifier_yields_to_bot(make, sk):
    d = await make()
    d.hub.secrets.set("SLACK_WEBHOOK_URL", f"{sk.url}/webhook")
    assert d.hub.slack.active() and d.hub.notifier.target() is None


# ============================================================================ giao việc + trả lời trong thread
async def test_run_result_and_thread_continue(make, sk):
    d = await make()
    d.hub.agents.create({"id": "coder", "name": "coder"})
    await deliver(sk, sk.slash(BOSS, "giaoviec coder viết hàm cộng"))
    res = await until(lambda: [m for m in sk.sent(DM_BOSS) if "— xong" in sk.text_of(m)])
    assert "Trả lời trong thread" in sk.text_of(res[-1])
    first = d.hub.store.list_runs(agent_id="coder")[0]
    n = len(res)
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "thêm test nữa", thread_ts=res[-1]["_ts"]))
    await until(lambda: len([m for m in sk.sent(DM_BOSS) if "— xong" in sk.text_of(m)]) > n)
    child = next(r for r in d.hub.store.list_runs(agent_id="coder") if r["id"] != first["id"])
    assert child["parent_run_id"] == first["id"] and child["prompt"] == "thêm test nữa"


# ============================================================================ Socket Mode
async def test_duplicate_envelope_processed_once(make, sk):
    await make()
    await deliver(sk, sk.slash(BOSS, "estop on một"))
    n = len(sk.sent(DM_BOSS))
    e1 = sk.user_message(DM_BOSS, BOSS, "trùng", event_id="EvSAME")
    e2 = sk.user_message(DM_BOSS, BOSS, "trùng", event_id="EvSAME")
    await deliver(sk, e1)
    await deliver(sk, e2)
    later = sk.sent(DM_BOSS)[n:]
    assert len([m for m in later if "điều phối viên" in sk.text_of(m).lower()]) <= 1


async def test_reconnects_after_disconnect(make, sk):
    await make()
    before = sk.connections
    sk.disconnect()
    await until(lambda: sk.connections > before)
    await deliver(sk, sk.slash(BOSS, "trangthai"))
    assert any("ESTOP" in t for t in texts(sk, DM_BOSS))


async def test_tokens_never_in_status_or_audit(make, sk):
    d = await make()
    st = await rpc(d, "slack.status")
    blob = str(st) + str(d.hub.audit.query(limit=200))
    assert BOT_TOKEN not in blob and APP_TOKEN not in blob


# ============================================================================ setup + ghép nối
async def test_setup_pairing_allow(make, sk):
    d = await make(users=(), chats=(), token=False, sl_cfg={"team_id": None}, connect=False)
    r = await rpc(d, "slack.setup", {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN})
    assert r["team_id"] == TEAM and r["pair_code"]
    assert BOT_TOKEN not in str(r)
    await until(lambda: d.hub.slack.info.get("polling") and sk.connected())
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "ghepnoi SAIMA123"))
    assert (await rpc(d, "slack.pending", {"code": r["pair_code"]})) == []
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, f"ghepnoi {r['pair_code']}"))
    pend = await rpc(d, "slack.pending", {"code": r["pair_code"]})
    assert pend and pend[0]["user_id"] == BOSS and pend[0]["chat_id"] == DM_BOSS
    out = await rpc(d, "slack.allow", {"chat_id": DM_BOSS})
    assert DM_BOSS in out["allowed_chat_ids"] and BOSS in out["allowed_user_ids"]
    await deliver(sk, sk.slash(BOSS, "trangthai"))
    assert any("ESTOP" in t for t in texts(sk, DM_BOSS))


async def test_revoke_dm_removes_its_user(make, sk):
    d = await make(users=(BOSS, OTHER), chats=(DM_BOSS, DM_OTHER))
    d.hub.slack.pairing = None
    await rpc(d, "slack.allow", {"chat_id": DM_BOSS, "user_id": BOSS})  # ghi DM ↔ người
    out = await rpc(d, "slack.revoke", {"chat_id": DM_BOSS})
    assert DM_BOSS not in out["allowed_chat_ids"] and BOSS not in out["allowed_user_ids"] and OTHER in out["allowed_user_ids"]
    assert not auth.slack_caller(d.hub, DM_BOSS, BOSS).human


def test_pair_code_tolerates_pasted_formatting():
    from cfharness.chat.slack.bot import pair_code_of

    for t in ("ghepnoi ab12cd34", "`ghepnoi AB12CD34`", "*ghepnoi* <AB12CD34>", "ghép nối AB12CD34", "ghép nối AB12CD34",
              "  ghepnoi​   AB12CD34 "):
        assert pair_code_of(t) == "AB12CD34", t
    assert pair_code_of("chào bot") is None and pair_code_of("ghepnoi") is None


async def test_pairing_with_backticks_from_web(make, sk):
    d = await make(users=(), chats=())
    code = d.hub.slack.start_pairing()["code"]
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, f"`ghepnoi {code.lower()}`"))
    pend = d.hub.slack.pending(code)
    assert pend and pend[0]["user_id"] == BOSS


async def test_kick_does_not_leave_polling_false(make, sk):
    d = await make()
    before = sk.connections
    d.hub.slack.kick()  # như bấm lưu token lần 2: kết nối cũ đóng muộn không được ghi đè trạng thái kết nối mới
    await until(lambda: sk.connections > before)
    await asyncio.sleep(1.0)
    assert d.hub.slack.info["polling"] is True
    await deliver(sk, sk.slash(BOSS, "trangthai"))
    assert any("ESTOP" in t for t in texts(sk, DM_BOSS))
