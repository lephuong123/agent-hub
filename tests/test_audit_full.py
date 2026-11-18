"""L3 — audit đầy đủ (docs/dev-notes/plan-logging.md §4 A1–A5): migration v29, principal do daemon chứng thực (token web/MCP riêng),
ghi cả thất bại (denied/error + duration), lượt đọc nhạy cảm gộp 10 phút, mọi method ghi đều được bọc audit, agent không tự chọn req_id.

Daemon THẬT trên Unix socket THẬT (như tests/test_trace_id.py) — không gọi `claude`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from cfharness import auth, oscompat, trace, transport
from cfharness.daemon import Daemon
from cfharness.errors import HubError
from cfharness.mcp_server.rpc import SENSITIVE_READS, is_audited, wrap_with_audit
from cfharness.oscompat import IS_WINDOWS
from cfharness.paths import HubPaths
from cfharness.rpc import RpcError
from cfharness.runtime.fake import FakeRuntime
from cfharness.tui.client import AsyncHubClient


@pytest.fixture
def home(monkeypatch):
    base = Path(tempfile.gettempdir()) if IS_WINDOWS else Path(os.path.realpath("/tmp"))
    h = base / f"ahau-{os.getpid()}-{time.time_ns() % 10**6}"
    monkeypatch.setenv("CFHARNESS_HOME", str(h))
    monkeypatch.delenv("CFHARNESS_SOCKET", raising=False)
    monkeypatch.delenv(trace.ENV_REQ_ID, raising=False)
    p = HubPaths.resolve(h)
    p.ensure()
    p.config.write_text("guard:\n  git_probe: false\nhub:\n  max_concurrent_runs: 20\n")
    yield p
    shutil.rmtree(h, ignore_errors=True)


async def _start(p: HubPaths) -> tuple[Daemon, asyncio.base_events.Server]:
    d = Daemon(p, runtime=FakeRuntime(hold=True))
    d.hub.start()
    srv = await transport.start_server(d._handle_conn, p.socket, limit=16 * 1024 * 1024)
    os.chmod(p.socket, 0o600)
    return d, srv


async def _stop(d: Daemon, srv) -> None:
    srv.close()
    oscompat.JOBS.close_all()
    d.hub.runs._runtime.release()
    await d.hub.stop()


async def _raw(p: HubPaths, method: str, params: dict) -> dict:
    reader, writer = await transport.open_connection(str(p.socket), limit=1 << 20)
    try:
        writer.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n").encode())
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()


def _rows(d: Daemon, kind: str) -> list[dict]:
    return d.hub.audit.query(kind=kind, limit=50)


def _tok(p: HubPaths, kind: str = "human") -> str:
    return auth.token_path(p.socket, kind).read_text().strip()


# ---------------------------------------------------------------------------- A1 schema
def test_v29_columns_and_indexes(home):
    d = Daemon(home, runtime=FakeRuntime())
    cols = {r[1] for r in d.hub.store.conn.execute("PRAGMA table_info(audit_log)")}
    for c in ("outcome", "error_code", "duration_ms", "principal", "declared_channel", "session_ref", "ip", "caller_run_id", "req_id"):
        assert c in cols, c
    assert "row_hash" not in cols and "prev_hash" not in cols  # L4
    idx = {r[1] for r in d.hub.store.conn.execute("PRAGMA index_list(audit_log)")}
    assert {"audit_log_principal_ts", "audit_log_outcome_ts"} <= idx
    rid = d.hub.audit.record("x.test", None, None, {}, principal="os:me", outcome="ok")
    with pytest.raises(Exception):  # trigger append-only giữ nguyên
        d.hub.store.conn.execute("UPDATE audit_log SET outcome='error' WHERE id=?", (rid,))
    row = d.hub.audit.query(kind="x.test")[0]
    assert row["principal"] == "os:me" and row["outcome"] == "ok"
    assert d.hub.audit.query(principal="os:me")[0]["id"] == rid and d.hub.audit.query(outcome="denied") == []
    for k in auth.TOKEN_KINDS:  # ba token, 0600
        assert oct(auth.token_path(home.socket, k).stat().st_mode & 0o777) == "0o600"
    assert len({_tok(home, k) for k in auth.TOKEN_KINDS}) == 3


# ---------------------------------------------------------------------------- A3 thất bại
async def test_wrap_records_error_denied_and_unexpected(home):
    d = Daemon(home, runtime=FakeRuntime())
    # lỗi nghiệp vụ → error + mã
    r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "agent.update", "params": {"id": "khong-co", "changes": {}}})
    assert "error" in r
    row = _rows(d, "agent.update")[0]
    assert row["outcome"] == "error" and row["error_code"] == "not_found" and row["duration_ms"] is not None
    assert "Traceback" not in json.dumps(row)
    # ESTOP → Blocked → denied
    d.hub.agents.create({"id": "a1", "name": "a1"})
    await d.hub.runs.set_estop(True, reason="test")
    r = await d._dispatch({"jsonrpc": "2.0", "id": 2, "method": "run.start", "params": {"agent": "a1", "prompt": "hi"}})
    assert r["error"]["code"] == -32003
    row = _rows(d, "run.start")[0]
    assert row["outcome"] == "denied" and row["error_code"] == "blocked" and row["data"]["params"]["prompt"] == "hi"
    # thành công → ok
    await d.hub.runs.set_estop(False)
    await d._dispatch({"jsonrpc": "2.0", "id": 3, "method": "agent.pause", "params": {"id": "a1", "name": "a1"}})
    assert _rows(d, "agent.pause")[0]["outcome"] == "ok"

    # lỗi bất ngờ → error/system, lỗi gốc được ném lại
    def boom(p):
        raise RuntimeError("bí mật trong traceback")

    w = wrap_with_audit(d.hub, "x.boom", boom)
    with pytest.raises(RuntimeError):
        await w({"secret": 1})
    row = _rows(d, "x.boom")[0]
    assert row["outcome"] == "error" and row["error_code"] == "system" and "bí mật" not in json.dumps(row, ensure_ascii=False)

    def hub_err(p):
        raise HubError("xung đột", code=-32009)

    with pytest.raises(HubError):
        await wrap_with_audit(d.hub, "x.conf", hub_err)({"value": "s3cr3t"})
    row = _rows(d, "x.conf")[0]
    assert row["error_code"] == "conflict" and "s3cr3t" not in json.dumps(row)  # params che như cũ


def test_service_rows_default_outcome(home):
    d = Daemon(home, runtime=FakeRuntime())
    d.hub.audit.record("auth.denied", None, None, {})
    d.hub.audit.record("guard.trust", "a", None, {"by": "user", "channel": "web"})
    d.hub.audit.record("guard.tamper_detected", None, None, {})
    assert _rows(d, "auth.denied")[0]["outcome"] == "denied"
    t = _rows(d, "guard.trust")[0]
    assert t["outcome"] == "ok" and t["actor"] == "user" and t["channel"] == "web"  # A5: actor rơi về `by`
    assert _rows(d, "guard.tamper_detected")[0]["outcome"] is None


# ---------------------------------------------------------------------------- A4 lượt đọc nhạy cảm
async def test_sensitive_read_coalesced(home):
    d = Daemon(home, runtime=FakeRuntime())
    for _ in range(3):
        r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "secret.list", "params": {}})
        assert "result" in r
    rows = _rows(d, "read:secret.list")
    assert len(rows) == 1 and rows[0]["data"]["count"] == 1 and rows[0]["outcome"] == "ok" and rows[0]["principal"] == "hub:internal"
    # run.get không events → không ghi; có events → ghi
    d.hub.agents.create({"id": "a1", "name": "a1"})
    run = d.hub.runs.submit("a1", "x")
    await d._dispatch({"jsonrpc": "2.0", "id": 2, "method": "run.get", "params": {"id": run["id"]}})
    assert _rows(d, "read:run.get") == []
    await d._dispatch({"jsonrpc": "2.0", "id": 3, "method": "run.get", "params": {"id": run["id"], "events": True}})
    got = _rows(d, "read:run.get")
    assert len(got) == 1 and got[0]["target"] == run["id"] and isinstance(got[0]["data"].get("result_count"), int)
    # cửa sổ đóng → MỘT dòng gộp cho 2 lượt còn lại
    d._flush_reads(time.monotonic() + 601)
    rows = _rows(d, "read:secret.list")
    assert len(rows) == 2 and rows[0]["data"] == {"count": 2, "window_s": 600, "coalesced": True, **({"result_count": rows[0]["data"]["result_count"]} if "result_count" in rows[0]["data"] else {})}
    # chuỗi tìm kiếm chỉ ghi độ dài
    from cfharness.daemon import _read_target

    assert _read_target({"query": "mật khẩu ngân hàng"}) == "q:18ch"


def test_sensitive_reads_exist(home):
    d = Daemon(home, runtime=FakeRuntime())
    assert SENSITIVE_READS <= set(d.methods)
    assert not (SENSITIVE_READS & auth.all_mutating())


# ---------------------------------------------------------------------------- A2 principal theo kênh
def test_principal_each_channel():
    C = auth.Caller
    assert auth.principal_of(C(via="socket", token_ok=True, cred="human")) == f"os:{auth.os_user()}"
    assert auth.principal_of(C(via="socket", token_ok=True, cred="web"), {"session_ref": "dev_0123abcd"}) == "web:dev_0123abcd"
    assert auth.principal_of(C(via="socket", token_ok=True, cred="web")) == "web:anon"
    assert auth.principal_of(C(via="socket", token_ok=True, cred="mcp"), {"transport": "stdio"}) == "mcp:stdio"
    assert auth.principal_of(C(via="socket", token_ok=True, cred="mcp"), {"transport": "http", "level": "read"}) == "mcp:http:read"
    assert auth.principal_of(C(via="telegram:-100", token_ok=True, user="42")) == "telegram:42"
    assert auth.principal_of(C(via="slack:D1", token_ok=True, user="U9")) == "slack:U9"
    assert auth.principal_of(auth.manager_caller("mgr", "r1")) == "manager:mgr"
    assert auth.principal_of(C(via="socket", run_id="r1", agent_id="a1", evidence=["env_run_id"])) == "agent:a1"
    assert auth.principal_of(C(via="internal"), internal_channel="schedule") == "hub:schedule"
    assert auth.principal_of(C(via="socket", token_ok=False)) == "anon:socket"
    # metadata client chỉ nhận theo loại token
    human = C(via="socket", token_ok=True, cred="human")
    assert auth.clean_client_meta(human, {"session_ref": "dev_0123abcd", "ip": "1.2.3.4"}) == {}
    web = C(via="socket", token_ok=True, cred="web")
    assert auth.clean_client_meta(web, {"session_ref": "raw-sid-value", "ip": "not-ip", "transport": "http"}) == {}
    assert auth.clean_client_meta(web, {"session_ref": "sess_deadbeef", "ip": "203.0.113.5"}) == {"session_ref": "sess_deadbeef", "ip": "203.0.113.5"}


def test_web_client_meta_ip_only_public():
    from cfharness.web.app import web_client_meta

    class St:
        def __init__(self, pub):
            self.pub, self.ip_header = pub, "x-forwarded-for"

        def is_public(self, host):
            return self.pub

    class S:
        device_id = ""

    scope = {"client": ("127.0.0.1", 5555)}
    hdr = {"x-forwarded-for": "203.0.113.9"}
    local = web_client_meta(St(False), scope, hdr, "cfharness.localhost", "SID-RAW", S())
    assert set(local) == {"session_ref"} and local["session_ref"].startswith("sess_") and "SID-RAW" not in json.dumps(local)
    pub = web_client_meta(St(True), scope, hdr, "hub.example.com", "SID-RAW", S())
    assert pub["ip"] == "203.0.113.9"
    s2 = S()
    s2.device_id = "devid"
    assert web_client_meta(St(False), scope, hdr, "h", "SID", s2)["session_ref"].startswith("dev_")


async def test_socket_principals_and_spoofed_channel(home):
    d, srv = await _start(home)
    try:
        # CLI (token người) — khai channel=web → kênh cli, declared_channel=web, principal os:<user>
        cli = AsyncHubClient(home.socket, channel="web")
        await cli.call("agent.create", {"config": {"id": "s1", "name": "s1"}}, audit=True)
        row = _rows(d, "agent.create")[0]
        assert row["channel"] == "cli" and row["declared_channel"] == "web" and row["principal"] == f"os:{auth.os_user()}"
        # TUI giữ kênh tui
        await AsyncHubClient(home.socket, channel="tui").call("agent.pause", {"id": "s1", "name": "s1"}, audit=True)
        assert _rows(d, "agent.pause")[0]["channel"] == "tui"
        # web (token riêng) — khai channel=cli vẫn thành web; session_ref + ip (ip chỉ nhận từ token web)
        meta = {"session_ref": "sess_0011aabb", "ip": "198.51.100.7"}
        web = AsyncHubClient(home.socket, channel="cli", cred="web", client_meta=lambda: meta)
        await web.call("agent.resume", {"id": "s1", "name": "s1"}, audit=True)
        row = _rows(d, "agent.resume")[0]
        assert (row["channel"], row["declared_channel"], row["principal"], row["session_ref"], row["ip"]) == (
            "web", "cli", "web:sess_0011aabb", "sess_0011aabb", "198.51.100.7")
        # CLI gửi `_client` giả → bị bỏ
        spoof = AsyncHubClient(home.socket, channel="cli", client_meta=lambda: meta)
        await spoof.call("agent.pause", {"id": "s1", "name": "s1"}, audit=True)
        row = _rows(d, "agent.pause")[0]
        assert row["session_ref"] is None and row["ip"] is None and row["principal"].startswith("os:")
        # MCP (token riêng)
        r = await _raw(home, "agent.resume", {"id": "s1", "_auth": _tok(home, "mcp"), "_audit": {"channel": "mcp:claude-code", "actor": "user"},
                                             "_client": {"transport": "http", "level": "control"}})
        assert "result" in r
        row = _rows(d, "agent.resume")[0]
        assert row["principal"] == "mcp:http:control" and row["channel"] == "mcp:claude-code"
        # web cũ (token người, daemon mới): os:<user> + declared_channel=web
        r = await _raw(home, "agent.pause", {"id": "s1", "_auth": _tok(home), "_audit": {"channel": "web", "actor": "user"}})
        row = _rows(d, "agent.pause")[0]
        assert row["principal"].startswith("os:") and row["channel"] == "cli" and row["declared_channel"] == "web"
        # token web vẫn là người với method HUMAN_ONLY
        r = await _raw(home, "audit.list", {"_auth": _tok(home, "web"), "limit": 1})
        assert "result" in r
        # lượt đọc nhạy cảm theo principal web
        r = await _raw(home, "secret.list", {"_auth": _tok(home, "web"), "_client": {"session_ref": "dev_00000001"}})
        assert _rows(d, "read:secret.list")[0]["principal"] == "web:dev_00000001"
        # không token → audit.list bị từ chối (HUMAN_ONLY) + dòng auth.denied outcome=denied
        r = await _raw(home, "audit.list", {"limit": 1})
        assert r["error"]["code"] == -32003
        assert _rows(d, "auth.denied")[0]["outcome"] == "denied"
    finally:
        await _stop(d, srv)


# ---------------------------------------------------------------------------- A5 mọi method ghi đều có audit
# Method ghi được audit BỞI SERVICE (không qua wrap_with_audit) — lý do:
SERVICE_AUDITED = {
    "approval.manager_decide": "ApprovalService.manager_decide ghi approval.* (tiền tố approval. không bọc lần hai)",
    "managerself.call": "mỗi thao tác ghi bên trong đi qua call_as(manager_caller) → method đích đã bọc; thao tác đọc không ghi",
    "manager.conversation_read": "chỉ đánh dấu đã xem (UI), không đổi trạng thái nghiệp vụ — cố ý không audit",
    "file.open": "fileview.register_rpc tự ghi audit (không bao giờ nội dung file)",
    "web.notify": "web_events tự ghi audit theo loại sự kiện (danh sách trắng)",
    **{f"telegram.{m}": "chat/rpc._audit tự ghi (gỡ token)" for m in ("setup", "allow", "revoke", "disable", "test")},
    **{f"slack.{m}": "chat/slack/rpc tự ghi (gỡ token)" for m in ("setup", "allow", "revoke", "disable", "test")},
}


def test_every_mutating_method_is_audited(home):
    d = Daemon(home, runtime=FakeRuntime())
    registered = [m for m in sorted(auth.all_mutating()) if m in d.methods]
    assert len(registered) > 100
    missing = [m for m in registered if not is_audited(d.methods[m]) and m not in SERVICE_AUDITED]
    assert missing == [], f"method ghi chưa bọc wrap_with_audit (thêm vào hoặc ghi lý do trong SERVICE_AUDITED): {missing}"
    stale = [m for m in SERVICE_AUDITED if m in d.methods and is_audited(d.methods[m])]
    assert stale == [], f"đã bọc rồi — bỏ khỏi SERVICE_AUDITED: {stale}"
    for m in ("eval.run", "eval.batch", "eval.check", "digest.run", "curator.run", "regression.check"):
        assert is_audited(d.methods[m]), m


async def test_guard_rows_take_actor_channel_from_audit_meta_and_repo_deny_rm(home, tmp_path):
    d, srv = await _start(home)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        web = AsyncHubClient(home.socket, channel="web", cred="web", client_meta=lambda: {"session_ref": "sess_12345678"})
        await web.call("guard.repo_deny.add", {"repo": str(repo), "pattern": "rm -rf *"}, audit=True)
        await web.call("guard.repo_deny.rm", {"repo": str(repo), "pattern": "rm -rf *"}, audit=True)
        for k in ("guard.repo_deny_add", "guard.repo_deny_rm"):
            row = _rows(d, k)[0]
            assert (row["actor"], row["channel"], row["principal"], row["outcome"]) == ("user", "web", "web:sess_12345678", "ok"), (k, row)
        d.hub.agents.create({"id": "t1", "name": "t1"})
        # CLI cũ gửi `by`/`channel` trong params (không `_audit`) → vẫn chạy; kênh = kênh daemon chứng thực
        await AsyncHubClient(home.socket, channel="cli").call("guard.trust.set", {"id": "t1", "mode": "off", "by": "user", "channel": "web"})
        row = _rows(d, "guard.trust")[0]
        assert row["actor"] == "user" and row["channel"] == "cli" and row["principal"].startswith("os:")
    finally:
        await _stop(d, srv)


# ---------------------------------------------------------------------------- req_id của agent
async def test_agent_supplied_req_id_ignored(home):
    d = Daemon(home, runtime=FakeRuntime(hold=True))
    d.hub.agents.create({"id": "ag", "name": "ag"})
    run = d.hub.runs.submit("ag", "x")
    run_req = d.hub.store.get_run(run["id"])["req_id"]
    assert trace.valid_req_id(run_req)
    agent = auth.Caller(via="socket", pid=1, run_id=run["id"], agent_id="ag", evidence=["env_run_id"])
    d._caller_for = lambda *a, **k: agent  # type: ignore[method-assign]
    human_req = trace.new_req_id()
    r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "run.stop", "params": {"id": run["id"], "_trace": {"req_id": human_req}}},
                          via_socket=True)
    got = r["_meta"]["req_id"]
    assert got != human_req and trace.valid_req_id(got)
    row = _rows(d, "run.stop")[0]
    assert row["req_id"] == got and row["principal"] == "agent:ag" and row["caller_run_id"] == run["id"] and row["actor"] == "agent"
    # người: req_id client gửi được giữ
    d._caller_for = lambda *a, **k: auth.Caller(via="socket", pid=1, token_ok=True, cred="human")  # type: ignore[method-assign]
    r = await d._dispatch({"jsonrpc": "2.0", "id": 2, "method": "hub.ping", "params": {"_trace": {"req_id": human_req}}}, via_socket=True)
    assert r["_meta"]["req_id"] == human_req
    d.hub.runs._runtime.release()


async def test_agent_req_id_parent_is_run(home):
    d = Daemon(home, runtime=FakeRuntime(hold=True))
    d.hub.agents.create({"id": "ag", "name": "ag"})
    run = d.hub.runs.submit("ag", "x")
    run_req = d.hub.store.get_run(run["id"])["req_id"]
    agent = auth.Caller(via="socket", pid=1, run_id=run["id"], agent_id="ag", evidence=["env_run_id"])
    seen = {}

    def spy(p):
        seen.update(trace.current() or {})
        return {"ok": True}

    d.methods["x.spy"] = spy
    d._caller_for = lambda *a, **k: agent  # type: ignore[method-assign]
    await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "x.spy", "params": {"_trace": {"req_id": trace.new_req_id()}}}, via_socket=True)
    assert seen["parent_req_id"] == run_req and seen["principal"] == "agent:ag" and seen["caller_run_id"] == run["id"]
    d.hub.runs._runtime.release()


async def test_rpc_error_still_raised_to_client(home):
    d, srv = await _start(home)
    try:
        with pytest.raises(RpcError):
            await AsyncHubClient(home.socket, channel="cli").call("agent.update", {"id": "nope", "changes": {}}, audit=True)
        row = _rows(d, "agent.update")[0]
        assert row["outcome"] == "error" and row["principal"].startswith("os:") and row["channel"] == "cli"
    finally:
        await _stop(d, srv)


async def test_failure_row_never_carries_credentials(home):
    """Handler lỗi TRƯỚC khi kịp gỡ thông tin đăng nhập khỏi params → dòng audit lỗi vẫn không mang chúng."""
    d = Daemon(home, runtime=FakeRuntime())

    def early_fail(p):
        raise HubError("không hợp lệ")

    w = wrap_with_audit(d.hub, "x.login", early_fail)
    with pytest.raises(HubError):
        await w({"code": "C0DE-1", "state": "ST4TE", "token": "T0K", "name": "srv"})
    row = _rows(d, "x.login")[0]
    dump = json.dumps(row)
    assert row["outcome"] == "error" and row["data"]["params"] == {"name": "srv"}
    assert "C0DE-1" not in dump and "ST4TE" not in dump and "T0K" not in dump
