"""Telegram — bot long polling + thông báo + duyệt bằng nút + lệnh chat. Server Bot API GIẢ cục bộ (tests/telegram_fake.py),
daemon IN-PROCESS (`Daemon` + FakeRuntime), không gọi Telegram/claude thật."""

from __future__ import annotations

import asyncio
import time

import pytest
import yaml

from agent_hub import auth
from agent_hub.daemon import Daemon
from agent_hub.errors import BLOCKED
from agent_hub.events import make_event
from agent_hub.guard.classifier import GuardContext, classify
from agent_hub.guard.runtime import build_settings
from agent_hub.notify.telegram import TelegramApi, TelegramError, format_event
from agent_hub.runtime import FakeRuntime

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
    from agent_hub.guard.policy import PolicyStore

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
    assert auth.telegram_caller(d.hub, BOSS).human is True
    assert auth.telegram_caller(d.hub, STRANGER).human is False
    # chat lạ (dù bằng cách nào tới được dispatch) không qua cổng HUMAN_ONLY
    resp = await d.call_as(auth.telegram_caller(d.hub, STRANGER), "estop.set", {"on": True})
    assert resp["error"]["code"] == BLOCKED and d.hub.runs.estop.get("on") is not True
    # agent gõ lệnh CLI telegram → forbidden (không tự ghép chat của nó vào allowlist)
    for cmd in ("agent-hub notify telegram allow 5", "agent-hub notify telegram setup --token-stdin"):
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
    res_msg = await until(lambda: next((m for m in tg.sent(BOSS) if m["text"].startswith("[agent-hub] Run ")), None))
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
    await until(lambda: any(child["id"] in m["text"] and m["text"].startswith("[agent-hub] Run ") for m in tg.sent(BOSS)))
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
    from agent_hub.hub import Hub

    h = Hub(hub_home, runtime=FakeRuntime())
    txt = h.telegram.format_result({"id": "r_1", "agent_id": "a", "outcome": "completed", "cost_usd": 0.0123, "num_turns": 3, "duration_ms": 4200,
                                    "summary": "x" * 10_000})
    assert len(txt) < 2000 and "đã cắt" in txt and "agent-hub runs show r_1" in txt and "$0.0123" in txt
    h.store.close()


# ============================================================================ thông báo + nút duyệt
async def test_approval_notification_buttons_forgery_replay(make, tg):
    d = await make(allowed=(BOSS, OTHER), runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    ap = open_approval(d, rid)
    msg = await until(lambda: next((m for m in tg.sent(BOSS) if ap["id"] in m["text"]), None))
    assert any(ap["id"] in m["text"] for m in tg.sent(OTHER))  # mọi chat trong allowlist
    btns = FakeTelegram.buttons(msg)
    assert [b["text"] for b in btns] == ["Cho phép", "Từ chối"] and all(len(b["callback_data"]) <= 64 for b in btns)
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
    # bấm thật → cho phép, audit kênh telegram:<chat>
    await press(tg, BOSS, approve)
    got = d.hub.approvals.get(ap["id"])
    assert got["status"] == "allowed" and got["resolved_channel"] == f"telegram:{BOSS}"
    # bấm lại (replay) + nút từ chối cùng nhóm → bị từ chối, không quyết lần 2
    await press(tg, BOSS, approve)
    await press(tg, BOSS, deny)
    assert d.hub.approvals.get(ap["id"])["status"] == "allowed"
    resolved = d.hub.audit.query(kind="approval.resolved")
    assert len(resolved) == 1 and resolved[0]["channel"] == f"telegram:{BOSS}"
    ans = [b["text"] for b in tg.calls_of("answerCallbackQuery")]
    assert any("đã dùng" in t for t in ans) and any("Đã CHO PHÉP" in t for t in ans)
    # nút trên chat OTHER bị gỡ sau khi quyết (approval.resolved)
    await until(lambda: any(str(b.get("chat_id")) == str(OTHER) for b in tg.calls_of("editMessageReplyMarkup")))
    await stop(d)


async def test_heavy_destructive_needs_two_step_confirm_and_cancel(make, tg):
    d = await make(runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    ap = open_approval(d, rid, klass="git", cmd="git push --force origin main")
    msg = await until(lambda: next((m for m in tg.sent(BOSS) if ap["id"] in m["text"]), None))
    assert "NẶNG" in msg["text"]
    await press(tg, BOSS, FakeTelegram.buttons(msg)[0]["callback_data"])
    assert d.hub.approvals.get(ap["id"])["status"] == "open"  # bước 1 chưa cho phép
    step2 = tg.sent(BOSS)[-2]  # tin xác nhận (sau đó là tin trả lời bấm nút)
    b2 = FakeTelegram.buttons(step2)
    assert [b["text"] for b in b2] == ["Xác nhận CHO PHÉP", "Huỷ"] and "git push --force" in step2["text"]
    # Huỷ → vẫn chờ, có tin duyệt mới với nút mới
    await press(tg, BOSS, b2[1]["callback_data"])
    assert d.hub.approvals.get(ap["id"])["status"] == "open"
    again = next(m for m in reversed(tg.sent(BOSS)) if FakeTelegram.buttons(m) and FakeTelegram.buttons(m)[0]["text"] == "Cho phép")
    await press(tg, BOSS, FakeTelegram.buttons(again)[0]["callback_data"])
    step2b = next(m for m in reversed(tg.sent(BOSS)) if FakeTelegram.buttons(m) and FakeTelegram.buttons(m)[0]["text"] == "Xác nhận CHO PHÉP")
    await press(tg, BOSS, FakeTelegram.buttons(step2b)[0]["callback_data"])
    assert d.hub.approvals.get(ap["id"])["status"] == "allowed"
    await stop(d)


async def test_approvals_command_and_deny_button(make, tg):
    d = await make(runtime=FakeRuntime(hold=True), tg_cfg={"events": []})  # tắt thông báo đẩy: chỉ /approvals
    rid = await held_run(d)
    ap = open_approval(d, rid)
    await until(lambda: tg.calls_of("getUpdates"))
    await say(tg, BOSS, "/approvals")
    msg = next(m for m in tg.sent(BOSS) if ap["id"] in m["text"])
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
    msg = await until(lambda: next((m for m in tg.sent(BOSS) if ap["id"] in m["text"]), None))
    assert "hunter2-SUPER-secret-value" not in msg["text"] and "***" in msg["text"]
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
    assert tok_file.exists() and oct(tok_file.stat().st_mode & 0o777) == "0o600"
    # sandbox Bash: denyRead secrets/ ; tool Read: permissions.deny
    s = build_settings({}, run_id="r1", socket=hub_home.socket, policy_dir=hub_home.home / "policy", guard_cfg={"hook_timeout_s": 900},
                       approval_timeout_s=300, cwd=hub_home.home / "agents" / "coder" / "workspaces" / "r1")
    assert str(hub_home.home / "secrets") in s["sandbox"]["filesystem"]["denyRead"]
    assert f"Read(/{hub_home.home}/secrets/**)" in s["permissions"]["deny"]
    # bộ phân loại: đọc file token → forbidden
    v = classify("Bash", {"command": f"cat {tok_file}"}, gctx(hub_home, hub_home.home / "agents" / "coder"))
    assert v.forbidden and v.rule == "hub_secret_read", v
    await stop(d)
