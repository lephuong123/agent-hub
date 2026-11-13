"""Tài khoản Claude (accounts/, luồng core): thêm/đổi/xoá/kiểm, migrate token cũ, phát hiện token tự đặt, phiên đăng nhập.

Không gọi `claude` thật: `start_login` là handle GIẢ (monkeypatch — accounts/login.py thuộc luồng LOGIN), `claude auth status` được thay
bằng hàm giả. Token trong test là chuỗi giả, không phải token thật.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from cfharness import auth
from cfharness.accounts.service import AccountService, secret_name, token_hash
from cfharness.errors import HubError
from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI
from m7_fake import close, make_daemon, rpc, wait_status

TOK_A = "sk-ant-oat01-" + "A" * 40
TOK_B = "sk-ant-oat01-" + "B" * 40
TOK_X = "sk-ant-oat01-" + "X" * 40


def _events(d, type_):
    return [json.loads(r[0]) for r in d.hub.store.conn.execute("SELECT payload FROM events WHERE type=? ORDER BY seq", (type_,))]


@pytest.fixture
async def d(hub_home):
    dm = await make_daemon(hub_home)
    yield dm
    await close(dm)


async def test_add_token_list_never_contains_token(d):
    r = await rpc(d, "account.add_token", {"label": "Công việc", "token": TOK_A})
    acc = r["account"]
    assert acc["id"].startswith("a_") and acc["kind"] == "token" and acc["label"] == "Công việc"
    assert acc["active"] is False and acc["status"] == "unknown" and acc["expires_at"]
    assert d.hub.secrets.get(secret_name(acc["id"])) == TOK_A
    lst = await rpc(d, "account.list", {})
    blob = json.dumps(lst)
    assert TOK_A not in blob and "token_hash" not in blob and token_hash(TOK_A) not in blob
    ids = [a["id"] for a in lst["accounts"]]
    assert ids == [acc["id"], "machine"] and lst["active"] is None
    assert _events(d, "account.added") == [{"id": acc["id"], "label": "Công việc"}]
    # audit không giữ token
    rows = [r[0] for r in d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='account.add_token'")]
    assert rows and all(TOK_A not in r for r in rows)
    # trùng token → Conflict
    with pytest.raises(HubError) as e:
        await rpc(d, "account.add_token", {"label": "lại", "token": TOK_A})
    assert e.value.code == -32009
    with pytest.raises(HubError):
        await rpc(d, "account.add_token", {"label": "x", "token": "ngắn"})


async def test_use_copies_secret_clears_auth_block_and_publishes(d):
    a = (await rpc(d, "account.add_token", {"label": "A", "token": TOK_A}))["account"]["id"]
    b = (await rpc(d, "account.add_token", {"label": "B", "token": TOK_B}))["account"]["id"]
    d.hub.store.state_set("auth_block", {"message": "Not logged in", "run_id": "r_x", "at": "2026-11-13T00:00:00Z"})
    r = await rpc(d, "account.use", {"id": a})
    assert r == {"active": a, "previous": None}
    assert d.hub.secrets.get("CLAUDE_CODE_OAUTH_TOKEN") == TOK_A
    assert d.hub.store.state_get("claude_account_active") == a
    assert d.hub.runs.auth_block is None
    assert _events(d, "auth.cleared")[-1]["by"] == "account_switch"
    r = await rpc(d, "account.use", {"id": b})
    assert r == {"active": b, "previous": a}
    assert d.hub.secrets.get("CLAUDE_CODE_OAUTH_TOKEN") == TOK_B
    sw = _events(d, "account.switched")
    assert sw == [{"from": None, "to": a, "by": "user"}, {"from": a, "to": b, "by": "user"}]
    st = await rpc(d, "hub.status", {})
    assert st["account"] == {"id": b, "label": "B"}
    lst = await rpc(d, "account.list", {})
    assert lst["active"] == b and [x["active"] for x in lst["accounts"]] == [False, True, False]
    assert (await rpc(d, "account.list", {}))["accounts"][1]["last_used_at"]
    # tài khoản đang dùng lúc có cờ chặn → status auth_error
    d.hub.store.state_set("auth_block", {"message": "Invalid API key", "at": "x"})
    acc = next(x for x in (await rpc(d, "account.list", {}))["accounts"] if x["id"] == b)
    assert acc["status"] == "auth_error" and "Invalid" in acc["last_error"]


async def test_use_machine_deletes_secret(d):
    a = (await rpc(d, "account.add_token", {"label": "A", "token": TOK_A}))["account"]["id"]
    await rpc(d, "account.use", {"id": a})
    r = await rpc(d, "account.use", {"id": "machine"})
    assert r == {"active": "machine", "previous": a}
    assert d.hub.secrets.get("CLAUDE_CODE_OAUTH_TOKEN") is None
    assert (await rpc(d, "hub.status", {}))["account"] == {"id": "machine", "label": "Đăng nhập của máy"}
    with pytest.raises(HubError):
        await rpc(d, "account.use", {"id": "custom"})
    with pytest.raises(HubError) as e:
        await rpc(d, "account.use", {"id": "a_khongco"})
    assert e.value.code == -32004


async def test_rm_rename(d):
    a = (await rpc(d, "account.add_token", {"label": "A", "token": TOK_A}))["account"]["id"]
    b = (await rpc(d, "account.add_token", {"label": "B", "token": TOK_B}))["account"]["id"]
    await rpc(d, "account.use", {"id": a})
    with pytest.raises(HubError) as e:
        await rpc(d, "account.rm", {"id": a})
    assert e.value.code == -32009  # Conflict
    with pytest.raises(HubError):
        await rpc(d, "account.rm", {"id": "machine"})
    assert (await rpc(d, "account.rename", {"id": b, "label": "  Cá nhân  "}))["account"]["label"] == "Cá nhân"
    assert _events(d, "account.renamed") == [{"id": b, "label": "Cá nhân"}]
    assert await rpc(d, "account.rm", {"id": b}) == {"ok": True}
    assert d.hub.secrets.get(secret_name(b)) is None
    assert _events(d, "account.removed") == [{"id": b}]
    assert [x["id"] for x in (await rpc(d, "account.list", {}))["accounts"]] == [a, "machine"]


async def test_custom_detection(d):
    a = (await rpc(d, "account.add_token", {"label": "A", "token": TOK_A}))["account"]["id"]
    await rpc(d, "account.use", {"id": a})
    d.hub.secrets.set("CLAUDE_CODE_OAUTH_TOKEN", TOK_X)  # người đặt tay `cfharness secret set` ngoài danh sách
    lst = await rpc(d, "account.list", {})
    assert lst["active"] == "custom"
    assert [(x["id"], x["kind"], x["active"]) for x in lst["accounts"]] == [(a, "token", False), ("custom", "custom", True), ("machine", "machine", False)]
    assert TOK_X not in json.dumps(lst)
    assert (await rpc(d, "hub.status", {}))["account"]["id"] == "custom"
    # thêm token đó vào danh sách → nhận diện theo hash, hết `custom`
    x = (await rpc(d, "account.add_token", {"label": "X", "token": TOK_X}))["account"]
    assert x["active"] is True
    assert (await rpc(d, "account.list", {}))["active"] == x["id"]


def test_migrate_legacy_token(hub_home):
    from cfharness.hub import Hub
    from cfharness.runtime import FakeRuntime
    from cfharness.secrets import SecretStore

    SecretStore(hub_home.home / "secrets").set("CLAUDE_CODE_OAUTH_TOKEN", TOK_A)
    h = Hub(hub_home, runtime=FakeRuntime())
    try:
        lst = h.accounts.list()
        acc = lst["accounts"][0]
        assert acc["label"] == "Mặc định" and acc["active"] and lst["active"] == acc["id"]
        assert acc["expires_at"] is None
        assert h.store.state_get("claude_account_active") == acc["id"]
        assert h.secrets.get(secret_name(acc["id"])) == TOK_A
        # chạy một lần: xoá rồi khởi tạo lại không tạo thêm
        assert h.accounts.migrate_legacy() is None
    finally:
        h.store.close()
    h2 = Hub(hub_home, runtime=FakeRuntime())
    try:
        assert len([a for a in h2.accounts.list()["accounts"] if a["kind"] == "token"]) == 1
    finally:
        h2.store.close()


def test_no_legacy_token_no_account(hub_home):
    from cfharness.hub import Hub
    from cfharness.runtime import FakeRuntime

    h = Hub(hub_home, runtime=FakeRuntime())
    try:
        assert h.accounts.list() == {"active": None, "accounts": [h.accounts.get("machine")]}
        assert h.status()["account"] is None
    finally:
        h.store.close()


def test_human_only_classification():
    from cfharness.accounts.rpc import MUTATING_ACCOUNTS

    assert set(MUTATING_ACCOUNTS) <= auth.all_mutating()
    assert set(MUTATING_ACCOUNTS) <= auth.HUMAN_ONLY
    assert not set(MUTATING_ACCOUNTS) & set(auth.MANAGER_ALLOWED)  # đổi tài khoản chỉ do người
    assert "account.list" not in auth.all_mutating()
    assert {"use", "add", "login", "rm"} <= HUB_HUMAN_ONLY_CLI["account"]


async def test_agent_cannot_switch(d):
    a = (await rpc(d, "account.add_token", {"label": "A", "token": TOK_A}))["account"]["id"]
    agent = auth.Caller(via="socket", token_ok=False)
    for m, p in (("account.use", {"id": a}), ("account.add_token", {"label": "x", "token": TOK_B}), ("account.rm", {"id": a})):
        with pytest.raises(HubError):
            await rpc(d, m, p, as_caller=agent)
    assert d.hub.store.state_get("claude_account_active") is None
    assert d.hub.secrets.get("CLAUDE_CODE_OAUTH_TOKEN") is None


async def test_check_updates_status(d, monkeypatch):
    a = (await rpc(d, "account.add_token", {"label": "A", "token": TOK_A}))["account"]["id"]
    seen: list[dict] = []

    async def fake_status(self, env):
        seen.append(env)
        return (env.get("CLAUDE_CODE_OAUTH_TOKEN") == TOK_A), ""

    monkeypatch.setattr(AccountService, "_auth_status", fake_status)
    acc = (await rpc(d, "account.check", {"id": a}))["account"]
    assert acc["status"] == "ok" and acc["last_error"] is None
    assert seen[-1]["CLAUDE_CODE_OAUTH_TOKEN"] == TOK_A
    assert "CLAUDECODE" not in seen[-1]  # env_strip áp dụng
    m = (await rpc(d, "account.check", {"id": "machine"}))["account"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen[-1]
    assert m["status"] == "auth_error" and m["last_error"]
    assert TOK_A not in json.dumps(acc)


async def test_rate_limited_from_gate(d):
    a = (await rpc(d, "account.add_token", {"label": "A", "token": TOK_A}))["account"]["id"]
    d.hub.store.state_set("rate_limit_gates", {a: {"until": "2999-01-01T00:00:00Z", "source": "rejected", "run_id": "r_1", "set_at": "x"}})
    d.hub.store.state_set("rate_limit_by_account", {a: {"utilization": 0.97}})
    acc = (await rpc(d, "account.list", {}))["accounts"][0]
    assert acc["status"] == "rate_limited" and acc["rate_limit"] == {"until": "2999-01-01T00:00:00Z", "utilization": 0.97}
    d.hub.store.state_set("rate_limit_gates", {a: {"until": "2000-01-01T00:00:00Z"}})
    acc = (await rpc(d, "account.list", {}))["accounts"][0]
    assert acc["status"] != "rate_limited" and acc["rate_limit"] is None


async def test_expiring(d):
    a = (await rpc(d, "account.add_token", {"label": "A", "token": TOK_A}))["account"]["id"]
    d.hub.store.conn.execute("UPDATE claude_accounts SET expires_at='2000-01-01T00:00:00Z', status='ok' WHERE id=?", (a,))
    assert (await rpc(d, "account.list", {}))["accounts"][0]["status"] == "expiring"


# ---------------------------------------------------------------------------- phiên đăng nhập (handle giả)
class FakeHandle:
    instances: list["FakeHandle"] = []

    def __init__(self, env):
        self.url = "https://claude.ai/oauth/authorize?code=true&state=fake"
        self.env = env
        self.closed = 0
        self.codes: list[str] = []
        FakeHandle.instances.append(self)

    async def submit(self, code, timeout=60):
        self.codes.append(code)
        if code == "sai":
            raise HubError("code không đúng hoặc đã hết hạn")
        return TOK_B

    async def close(self):
        self.closed += 1


@pytest.fixture
def fake_login(monkeypatch):
    FakeHandle.instances = []
    calls: list[tuple[str, dict]] = []

    async def start_login(binary, env, timeout=45):
        calls.append((binary, env))
        return FakeHandle(env)

    mod = types.ModuleType("cfharness.accounts.login")
    mod.start_login = start_login  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cfharness.accounts.login", mod)
    import cfharness.accounts as pkg

    monkeypatch.setattr(pkg, "login", mod, raising=False)
    return calls


async def test_login_start_finish(d, fake_login, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOK_X)  # env daemon có token → không được truyền cho setup-token
    a = (await rpc(d, "account.add_token", {"label": "A", "token": TOK_A}))["account"]["id"]
    await rpc(d, "account.use", {"id": a})
    st = await rpc(d, "account.login_start", {"label": "Mới"})
    assert st["login_id"].startswith("l_") and st["url"].startswith("https://") and st["expires_at"]
    env = fake_login[-1][1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "CLAUDECODE" not in env
    r = await rpc(d, "account.login_finish", {"login_id": st["login_id"], "code": "abc#xyz-code-123"})
    acc = r["account"]
    assert acc["label"] == "Mới" and d.hub.secrets.get(secret_name(acc["id"])) == TOK_B
    h = FakeHandle.instances[-1]
    assert h.codes == ["abc#xyz-code-123"] and h.closed >= 1
    assert d.hub.accounts._logins == {}
    # audit không giữ code
    rows = [r[0] for r in d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='account.login_finish'")]
    assert rows and all("abc#xyz-code-123" not in x and TOK_B not in x for x in rows)
    # phiên đã đóng → finish lại = NotFound
    with pytest.raises(HubError) as e:
        await rpc(d, "account.login_finish", {"login_id": st["login_id"], "code": "x"})
    assert e.value.code == -32004
    # không tự đổi: tài khoản đang dùng giữ nguyên
    assert (await rpc(d, "account.list", {}))["active"] == a


async def test_login_single_session_cancel_and_bad_code(d, fake_login):
    s1 = await rpc(d, "account.login_start", {"label": "1"})
    s2 = await rpc(d, "account.login_start", {"label": "2"})
    h1, h2 = FakeHandle.instances
    assert h1.closed == 1 and h2.closed == 0  # mở phiên mới đóng phiên cũ
    assert list(d.hub.accounts._logins) == [s2["login_id"]]
    with pytest.raises(HubError):
        await rpc(d, "account.login_finish", {"login_id": s1["login_id"], "code": "x"})
    with pytest.raises(HubError):
        await rpc(d, "account.login_finish", {"login_id": s2["login_id"], "code": "sai"})
    assert h2.closed == 1 and d.hub.accounts._logins == {}
    s3 = await rpc(d, "account.login_start", {"label": "3"})
    assert await rpc(d, "account.login_cancel", {"login_id": s3["login_id"]}) == {"ok": True}
    assert FakeHandle.instances[-1].closed == 1 and d.hub.accounts._logins == {}
    assert await rpc(d, "account.login_cancel", {"login_id": "l_khongco"}) == {"ok": True}
    assert [a for a in (await rpc(d, "account.list", {}))["accounts"] if a["kind"] == "token"] == []


async def test_login_expiry(d, fake_login):
    d.hub.accounts.login_ttl_s = 0.05
    s = await rpc(d, "account.login_start", {"label": "hết hạn"})
    await asyncio.sleep(0.2)
    assert d.hub.accounts._logins == {} and FakeHandle.instances[-1].closed == 1
    with pytest.raises(HubError) as e:
        await rpc(d, "account.login_finish", {"login_id": s["login_id"], "code": "abc"})
    assert e.value.code == -32004


async def test_run_records_account_id(d):
    a = (await rpc(d, "account.add_token", {"label": "A", "token": TOK_A}))["account"]["id"]
    await rpc(d, "account.use", {"id": a})
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku"})
    run = await rpc(d, "run.start", {"agent": "w1", "prompt": "làm việc"})
    r = await wait_status(d, run["run_id"] if "run_id" in run else run["id"], ("completed",))
    assert r["account_id"] == a
    acc = (await rpc(d, "account.list", {}))["accounts"][0]
    assert acc["last_used_at"]
