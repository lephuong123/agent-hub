"""Hạn mức theo TÀI KHOẢN Claude + phản ứng khi NGƯỜI đổi tài khoản (docs/dev-notes/plan-claude-accounts.md, luồng ratelimit).

- Cổng/trạng thái rate limit ghi theo tài khoản (`rate_limit_gates`, `rate_limit_by_account`); khoá cũ `rate_limit` /
  `rate_limit_gate` = bản của tài khoản đang dùng; dispatcher chỉ xét cổng của tài khoản đang dùng.
- Event `account.switched` (do người bấm): task chờ hạn mức reset chạy lại ngay (giữ `rate_limit_hits`), task `blocked` vì
  "claude chưa đăng nhập" về `ready`, auto_resume hẹn vì `rate_limit` chạy ngay. KHÔNG có đổi tự động.

Không gọi claude: `ScriptedRuntime`. Event tổng hợp phát qua bus của hub như core sẽ phát."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from cfharness.dispatch.dispatcher import RL_GATE_KEY
from cfharness.events import make_event
from cfharness.hub import Hub
from m3_fake import ScriptedRuntime

RL_ERR = {"subtype": "error_during_execution", "error_text": "API Error: rate limit exceeded", "api_error_status": 429}
AUTH_REASON = "lỗi không thể retry: claude chưa đăng nhập"


async def settle(cond, timeout=6.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await asyncio.sleep(0.02)
        if cond():
            return True
    raise AssertionError("hết giờ chờ điều kiện")


@pytest.fixture
def make_hub(hub_home):
    hubs = []

    def _make(script, active="a_one", **cfg):
        rt = ScriptedRuntime(script)
        h = Hub(hub_home, runtime=rt)
        h.config.set("tasks.tick_s", 0.05)
        h.config.set("tasks.rate_limit_backoff_s", 0.05)
        h.config.set("tasks.rate_limit_backoff_max_s", 0.1)
        h.config.set("recovery.auto_resume_backoff_s", 0)
        for k, v in cfg.items():
            h.config.set(k, v)
        if active:
            h.store.state_set("claude_account_active", active)
        h.start()
        hubs.append(h)
        return h, rt

    yield _make
    for h in hubs:
        h.runs.shutting_down = True
        h.auto_resume.stop()
        h.store.close()


def mk_team(hub):
    for aid in ("writer", "researcher"):
        hub.agents.create({"id": aid, "name": aid, "role": aid, "description": f"{aid} work"})
    return hub.teams.create({"id": "docs", "name": "Docs", "charter": "c", "policies": {},
                             "members": [{"agent_id": "writer", "role": "worker"}, {"agent_id": "researcher", "role": "worker"}]})


def switch(hub, to, frm=None):
    """Như core: đổi con trỏ rồi phát `account.switched {from, to, by}`."""
    frm = frm if frm is not None else hub.store.state_get("claude_account_active")
    hub.store.state_set("claude_account_active", to)
    hub.publish(make_event("account.switched", None, {"from": frm, "to": to, "by": "human"}))


def rl_update(hub, run_id, info):
    hub.publish(make_event("ratelimit.update", "writer", {"rate_limit_info": info}, run_id=run_id, source="claude"))


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


# ============================================================================ trạng thái theo tài khoản + khoá cũ
async def test_event_of_other_account_does_not_gate_active(make_hub):
    """Run của a_one bị rejected nhưng người đang dùng a_two → cổng ghi cho a_one, dispatcher vẫn claim; khoá cũ = bản a_two."""
    hub, _ = make_hub(lambda spec: {"text": "ok"}, active="a_two")
    mk_team(hub)
    hub.agents.create({"id": "solo", "name": "solo"})
    r = hub.runs.submit("solo", "adhoc")
    await hub.runs.wait(r["id"], 5)
    hub.store.update_run(r["id"], {"account_id": "a_one"})
    reset = int(time.time()) + 3600
    rl_update(hub, r["id"], {"status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour"})

    gates = hub.store.state_get("rate_limit_gates")
    assert set(gates) == {"a_one"} and datetime.fromisoformat(gates["a_one"]["until"].replace("Z", "+00:00")) == datetime.fromtimestamp(reset, tz=timezone.utc)
    assert set(gates["a_one"]) == {"until", "source", "run_id", "set_at"}
    assert hub.store.state_get("rate_limit_by_account")["a_one"]["status"] == "rejected"
    assert hub.store.state_get(RL_GATE_KEY) is None and hub.store.state_get("rate_limit") is None
    assert hub.dispatcher.gate_until() is None
    assert hub.dispatcher.gate_until("a_one") is not None

    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "x", "body": "x"})
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")


async def test_active_account_state_mirrors_legacy_keys(make_hub):
    """Không có cột/giá trị `account_id` → dùng tài khoản đang dùng; khoá cũ (hub.status) phản ánh đúng tài khoản đó."""
    hub, _ = make_hub(lambda spec: {"text": "ok"}, active="a_one")
    hub.agents.create({"id": "solo", "name": "solo"})
    r = hub.runs.submit("solo", "adhoc")
    await hub.runs.wait(r["id"], 5)
    reset = int(time.time()) + 600
    rl_update(hub, r["id"], {"status": "rejected", "resetsAt": reset})

    st = hub.status()
    assert st["rate_limit"]["status"] == "rejected" and st["rate_limit"]["throttled"] is True
    assert st["rate_limit_gate"]["account"] == "a_one"
    assert hub.store.state_get("rate_limit_gates")["a_one"]["until"] == st["rate_limit_gate"]["until"]
    gate_evs = [e.payload for e in hub.store.query_events() if e.type == "ratelimit.gate"]
    assert gate_evs and gate_evs[-1]["account"] == "a_one" and gate_evs[-1]["open"] is False

    # người đổi sang a_two (chưa có hạn mức) → khoá cũ trống, bản của a_one còn nguyên
    switch(hub, "a_two")
    await settle(lambda: hub.store.state_get(RL_GATE_KEY) is None)
    assert hub.store.state_get("rate_limit") is None
    assert hub.dispatcher.gate_until() is None
    assert "a_one" in hub.store.state_get("rate_limit_gates")
    assert hub.store.state_get("rate_limit_by_account")["a_one"]["status"] == "rejected"

    # đổi lại a_one → khoá cũ là bản của a_one
    switch(hub, "a_one")
    await settle(lambda: (hub.store.state_get(RL_GATE_KEY) or {}).get("account") == "a_one")
    assert hub.store.state_get("rate_limit")["status"] == "rejected"


async def test_default_key_when_no_account_configured(make_hub):
    hub, _ = make_hub(lambda spec: {"text": "ok"}, active=None)
    hub.agents.create({"id": "solo", "name": "solo"})
    r = hub.runs.submit("solo", "adhoc")
    await hub.runs.wait(r["id"], 5)
    rl_update(hub, r["id"], {"status": "rejected", "resetsAt": int(time.time()) + 600})
    assert set(hub.store.state_get("rate_limit_gates")) == {"_default"}
    assert hub.status()["rate_limit_gate"]["account"] == "_default"


async def test_legacy_gate_before_upgrade_belongs_to_previous_account(make_hub):
    """DB cũ chỉ có `rate_limit_gate`: lần đổi đầu tiên gán nó cho tài khoản TRƯỚC (from), không cho tài khoản mới."""
    hub, _ = make_hub(lambda spec: {"text": "ok"}, active=None)
    until = iso(datetime.now(timezone.utc) + timedelta(hours=1))
    hub.store.state_set(RL_GATE_KEY, {"until": until, "source": "event", "run_id": None, "set_at": until})
    hub.store.state_set("rate_limit", {"status": "rejected", "throttled": True})
    switch(hub, "a_new", frm=None)
    await settle(lambda: hub.store.state_get(RL_GATE_KEY) is None)
    assert hub.store.state_get("rate_limit_gates") == {"_default": {"until": until, "source": "event", "run_id": None, "set_at": until}}
    assert hub.store.state_get("rate_limit_by_account")["_default"]["status"] == "rejected"
    assert hub.store.state_get("rate_limit") is None


# ============================================================================ người đổi tài khoản → chạy tiếp
async def test_switch_wakes_tasks_waiting_for_reset_and_keeps_hits(make_hub):
    reset = int(time.time()) + 3600
    n = {"i": 0}

    def script(spec):
        n["i"] += 1
        if n["i"] == 1:
            return {**RL_ERR, "rate_limit": {"status": "rejected", "resetsAt": reset}}
        if n["i"] == 2:
            return RL_ERR  # lần 2 vẫn rate limit (không biết reset) → hits phải thành 2 (giữ 1 cũ)
        return {"text": "ok"}

    hub, _ = make_hub(script, active="a_one")
    mk_team(hub)
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "rl", "body": "x"})
    await settle(lambda: hub.tasks.row(t["id"])["rate_limit_hits"] == 1)
    assert hub.tasks.row(t["id"])["wait_reason"] == "rate_limit_reset"
    t2 = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "other", "body": "y"})
    await settle(lambda: hub.tasks.row(t2["id"])["wait_reason"] == "rate_limit_reset")
    await asyncio.sleep(0.2)
    assert n["i"] == 1  # cổng của a_one đang đóng: không spawn gì

    switch(hub, "a_two")
    await settle(lambda: hub.tasks.row(t2["id"])["status"] == "done")
    await settle(lambda: int(hub.tasks.row(t["id"])["rate_limit_hits"] or 0) == 2 or hub.tasks.row(t["id"])["status"] == "done")
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")
    assert n["i"] >= 3
    assert "a_one" in hub.store.state_get("rate_limit_gates")  # cổng a_one vẫn còn — chỉ không chặn a_two
    assert hub.store.state_get(RL_GATE_KEY) is None


async def test_on_account_switched_direct_keeps_hits(make_hub):
    """Gọi trực tiếp (không tick): `not_before` = bây giờ, `rate_limit_hits` giữ nguyên, wait_reason chưa đổi."""
    hub, _ = make_hub(lambda spec: {"text": "ok"}, active="a_one")
    mk_team(hub)
    hub.runs.shutting_down = True  # chặn tick để quan sát trạng thái ngay sau khi xử lý
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "rl", "body": "x"})
    far = iso(datetime.now(timezone.utc) + timedelta(hours=2))
    hub.tasks.update(t["id"], {"status": "ready", "wait_reason": "rate_limit_reset", "not_before": far, "rate_limit_hits": 3})
    hub.store.state_set("claude_account_active", "a_two")
    out = hub.dispatcher.on_account_switched({"from": "a_one", "to": "a_two", "by": "human"})
    assert out == {"woken": 1, "unblocked": 0}
    row = hub.tasks.row(t["id"])
    assert row["rate_limit_hits"] == 3
    assert datetime.fromisoformat(row["not_before"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)


async def test_switch_unblocks_auth_blocked_tasks_only(make_hub):
    hub, _ = make_hub(lambda spec: {"text": "ok"}, active="machine")
    mk_team(hub)
    hub.runs.shutting_down = True
    a = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "auth", "body": "x"})
    b = hub.tasks.create({"team": "docs", "assignee": "researcher", "title": "other", "body": "y"})
    hub.tasks.set_status(a["id"], "blocked", reason=AUTH_REASON, current_run_id=None)
    hub.tasks.set_status(b["id"], "blocked", reason="circuit open", current_run_id=None)
    hub.runs.shutting_down = False

    switch(hub, "a_one")
    await settle(lambda: hub.tasks.row(a["id"])["status"] == "done")
    rb = hub.tasks.row(b["id"])
    assert rb["status"] == "blocked" and rb["block_reason"] == "circuit open"
    evs = [e.payload for e in hub.store.query_events() if e.type == "task.status" and e.payload.get("from") == "blocked"]
    assert evs and "đổi tài khoản Claude" in evs[0]["reason"]


async def test_no_automatic_switch_on_rate_limit(make_hub):
    """Bị rate limit KHÔNG làm hub tự đổi tài khoản (con trỏ giữ nguyên, không có event account.switched)."""
    reset = int(time.time()) + 3600
    hub, _ = make_hub(lambda spec: {**RL_ERR, "rate_limit": {"status": "rejected", "resetsAt": reset}}, active="a_one")
    mk_team(hub)
    hub.store.state_set("rate_limit_gates", {})
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "rl", "body": "x"})
    await settle(lambda: hub.tasks.row(t["id"])["rate_limit_hits"] == 1)
    await asyncio.sleep(0.2)
    assert hub.store.state_get("claude_account_active") == "a_one"
    assert not any(e.type == "account.switched" for e in hub.store.query_events())


# ============================================================================ auto_resume
async def test_switch_brings_rate_limit_auto_resume_forward(make_hub):
    reset = int(time.time()) + 3600

    def script(spec):
        if spec.prompt == "rl":
            return {**RL_ERR, "rate_limit": {"status": "rejected", "resetsAt": reset}}
        if spec.prompt == "crash":
            return {"crash": True}
        return {"text": "ok"}

    hub, _ = make_hub(script, active="a_one", **{"recovery.auto_resume_backoff_s": 300, "recovery.auto_resume_backoff_max_s": 600})
    hub.agents.create({"id": "a", "name": "A"})
    hub.agents.create({"id": "b", "name": "B"})
    r1 = hub.runs.submit("a", "rl")
    r2 = hub.runs.submit("b", "crash")
    await hub.runs.wait(r1["id"], 5)
    await hub.runs.wait(r2["id"], 5)
    await settle(lambda: {r1["id"], r2["id"]} <= set(hub.store.state_get("auto_resume_pending", {}) or {}))
    pend = hub.store.state_get("auto_resume_pending")
    assert pend[r1["id"]] - time.time() > 3000  # chờ tới lúc reset của a_one

    switch(hub, "a_two")
    await settle(lambda: hub.store.get_run(r1["id"]).get("continued_in_run_id"))
    nr = hub.store.get_run(hub.store.get_run(r1["id"])["continued_in_run_id"])
    assert nr["resume_reason"] == "auto_resume:rate_limit"
    # lượt hẹn vì nguyên nhân khác giữ lịch cũ
    assert r2["id"] in (hub.store.state_get("auto_resume_pending") or {})
    assert not hub.store.get_run(r2["id"]).get("continued_in_run_id")
