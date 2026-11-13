"""Tài khoản Claude — đầu-cuối trong tiến trình (core + login + ratelimit, docs/dev-notes/plan-claude-accounts.md).

Thêm 2 tài khoản → dùng A → run của A bị rate limit `rejected` (event tổng hợp từ ScriptedRuntime) đóng cổng CỦA A, task chờ reset →
NGƯỜI `account.use` B → task chạy ngay bằng B, `hub.status.account` = B; cổng A vẫn còn nhưng không chặn B. Không có đổi tự động.
Đăng nhập: `start_login` giả (thay hàm trong accounts/login.py thật) → `login_start` / `login_finish` thêm tài khoản thứ ba.
Không gọi `claude` thật; token là chuỗi giả.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from m7_fake import close, make_daemon, rpc

TOK_A = "sk-ant-oat01-" + "A" * 40
TOK_B = "sk-ant-oat01-" + "B" * 40
TOK_C = "sk-ant-oat01-" + "C" * 40
RL_ERR = {"subtype": "error_during_execution", "error_text": "API Error: rate limit exceeded", "api_error_status": 429}


async def settle(cond, timeout=8.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("hết giờ chờ điều kiện")


class Script:
    """Run nào spawn khi tài khoản A đang dùng → rate limit rejected (reset sau 1 giờ); còn lại xong bình thường."""

    def __init__(self, d_ref):
        self.d_ref = d_ref
        self.reset = int(time.time()) + 3600
        self.accounts: list[str | None] = []

    def __call__(self, spec):
        hub = self.d_ref["d"].hub
        acct = hub.store.state_get("claude_account_active")
        self.accounts.append(acct)
        if acct == self.d_ref.get("a"):
            return {**RL_ERR, "rate_limit": {"status": "rejected", "resetsAt": self.reset, "rateLimitType": "five_hour"}}
        return {"text": "xong", "cost": 0.001}


@pytest.fixture
async def env(hub_home):
    ref: dict = {}
    script = Script(ref)
    d = await make_daemon(hub_home, script)  # type: ignore[arg-type]
    ref["d"] = d
    yield d, script, ref
    await close(d)


async def test_rate_limit_then_human_switch_resumes_queue(env):
    d, script, ref = env
    hub = d.hub
    a = (await rpc(d, "account.add_token", {"label": "Công ty", "token": TOK_A}))["account"]["id"]
    b = (await rpc(d, "account.add_token", {"label": "Cá nhân", "token": TOK_B}))["account"]["id"]
    ref["a"] = a
    assert (await rpc(d, "account.use", {"id": a})) == {"active": a, "previous": None}
    assert (await rpc(d, "hub.status", {}))["account"] == {"id": a, "label": "Công ty"}

    hub.agents.create({"id": "writer", "name": "writer", "role": "writer", "description": "w"})
    hub.teams.create({"id": "docs", "name": "Docs", "charter": "c", "policies": {}, "members": [{"agent_id": "writer", "role": "worker"}]})
    t = hub.tasks.create({"team": "docs", "assignee": "writer", "title": "viết", "body": "x"})

    # run của A bị rejected → cổng của A đóng, task chờ reset; không đổi tài khoản tự động
    await settle(lambda: hub.tasks.row(t["id"])["wait_reason"] == "rate_limit_reset")
    gates = hub.store.state_get("rate_limit_gates") or {}
    assert set(gates) == {a}
    assert hub.dispatcher.gate_until() is not None
    st = await rpc(d, "hub.status", {})
    assert st["account"]["id"] == a and st["rate_limit_gate"]["account"] == a
    acc_a = next(x for x in (await rpc(d, "account.list", {}))["accounts"] if x["id"] == a)
    assert acc_a["status"] == "rate_limited" and acc_a["rate_limit"]["until"]
    await asyncio.sleep(0.3)
    assert hub.store.state_get("claude_account_active") == a and len(script.accounts) == 1
    runs_a = [r[0] for r in hub.store.conn.execute("SELECT account_id FROM runs")]
    assert runs_a == [a]

    # NGƯỜI đổi sang B → task chạy ngay bằng B
    r = await rpc(d, "account.use", {"id": b})
    assert r == {"active": b, "previous": a}
    assert hub.secrets.get("CLAUDE_CODE_OAUTH_TOKEN") == TOK_B
    await settle(lambda: hub.tasks.row(t["id"])["status"] == "done")
    assert script.accounts[-1] == b
    assert [r[0] for r in hub.store.conn.execute("SELECT account_id FROM runs ORDER BY started_at")][-1] == b
    st = await rpc(d, "hub.status", {})
    assert st["account"] == {"id": b, "label": "Cá nhân"}
    assert st["rate_limit_gate"] is None  # khoá cũ = bản của B
    assert a in (hub.store.state_get("rate_limit_gates") or {})  # cổng A còn, chỉ không chặn B
    lst = await rpc(d, "account.list", {})
    by_id = {x["id"]: x for x in lst["accounts"]}
    assert lst["active"] == b and by_id[b]["active"] and by_id[a]["status"] == "rate_limited"
    sw = [json.loads(x[0]) for x in hub.store.conn.execute("SELECT payload FROM events WHERE type='account.switched' ORDER BY seq")]
    assert [(e["from"], e["to"]) for e in sw] == [(None, a), (a, b)]  # chỉ 2 lần đổi, đều do người
    blob = json.dumps(lst) + json.dumps(st)
    assert TOK_A not in blob and TOK_B not in blob


async def test_login_start_finish_with_fake_pty(env, monkeypatch):
    d, _, _ = env
    from cfharness.accounts import login as login_mod

    seen: dict = {}

    class FakeHandle:
        url = "https://claude.com/cai/oauth/authorize?code=true&state=fake"
        closed = 0

        async def submit(self, code, timeout=60):
            seen["code"] = code
            return TOK_C

        async def close(self):
            FakeHandle.closed += 1

    async def fake_start(binary, env, timeout=45):
        seen["env"] = env
        return FakeHandle()

    monkeypatch.setattr(login_mod, "start_login", fake_start)
    s = await rpc(d, "account.login_start", {"label": "Mới"})
    assert s["url"].startswith("https://") and s["login_id"].startswith("l_") and s["expires_at"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]
    r = await rpc(d, "account.login_finish", {"login_id": s["login_id"], "code": "abc#def-123"})
    acc = r["account"]
    assert acc["label"] == "Mới" and acc["kind"] == "token" and not acc["active"]
    assert seen["code"] == "abc#def-123" and FakeHandle.closed == 1
    assert d.hub.accounts._logins == {}
    audit = " ".join(x[0] for x in d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind LIKE 'account.login%'"))
    assert "abc#def-123" not in audit and TOK_C not in audit
    assert (await rpc(d, "account.use", {"id": acc["id"]}))["active"] == acc["id"]
    assert (await rpc(d, "hub.status", {}))["account"] == {"id": acc["id"], "label": "Mới"}
