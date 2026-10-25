"""Tìm kiếm hội thoại (session_search.py, migration v17): migration trên DB v16, chỉ mục lúc ghi, tiếng Việt có/không dấu,
che secret, phạm vi nhìn theo người gọi, 4 chế độ search|around|read|browse, CLI + MCP + hub-self. Không gọi `claude`."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
import yaml

from cfharness import auth as _auth
from cfharness import session_search as ss
from cfharness.errors import Blocked, HubError, NotFound
from cfharness.events import make_event
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime
from cfharness.store import MIGRATIONS, Store

VI_TEXT = "Đã cấu hình xong máy chủ; đường dẫn log nằm ở /var/log/app"


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    yield h
    h.store.close()


def _run(hub, rid, agent, *, prompt="", kind="adhoc", team=None, session=None, queued="2026-10-01T00:00:00Z", summary=None):
    hub.store.insert_run({"id": rid, "agent_id": agent, "kind": kind, "prompt": prompt, "status": "completed", "queued_at": queued,
                          "team_id": team, "session_id": session or f"s_{rid}", "summary": summary})


def _ev(hub, type_, agent, run_id, payload, session=None):
    hub.publish(make_event(type_, agent, payload, run_id=run_id, session_id=session or f"s_{run_id}"))


def _search(hub, vis=None, **p):
    return ss.search(hub.store.conn, p, vis)


# ============================================================================ migration v17 trên DB v16 có dữ liệu
def test_migration_v17_backfills_existing_v16_db(tmp_path, monkeypatch):
    import cfharness.store as store_mod

    db = tmp_path / "hub.db"
    monkeypatch.setattr(store_mod, "MIGRATIONS", MIGRATIONS[:16])
    s = Store(db)
    assert s.schema_version == 16
    now = "2026-10-01T00:00:00Z"
    s.conn.execute("INSERT INTO agents (id, name, home_path, created_at, updated_at) VALUES ('a1','A1','/x',?,?)", (now, now))
    s.conn.execute("INSERT INTO runs (id, agent_id, kind, prompt, status, queued_at, session_id, summary) VALUES "
                   "('r1','a1','adhoc','Hãy cấu hình máy chủ','completed',?, 'sess1', 'ok')", (now,))
    # run cũ đã mất event do retention → summary vẫn được chỉ mục
    s.conn.execute("INSERT INTO runs (id, agent_id, kind, prompt, status, queued_at, session_id, summary) VALUES "
                   "('r0','a1','adhoc','việc cũ','completed','2026-01-01T00:00:00Z', 'sess0', 'kết quả lưu trữ zebra')")
    rows = [
        ("message.complete", {"text": VI_TEXT, "message_id": "m1"}),
        ("message.delta", {"text": "không được chỉ mục"}),
        ("tool.start", {"tool_id": "t1", "name": "Bash", "args_preview": "systemctl restart nginx"}),
        ("tool.complete", {"tool_id": "t1", "name": "Bash", "result_preview": "Job for nginx.service done"}),
    ]
    for i, (t, p) in enumerate(rows, 1):
        s.conn.execute("INSERT INTO events (epoch, seq, ts, type, level, source, agent_id, run_id, session_id, payload) VALUES "
                       "('e0', ?, ?, ?, 'summary', 'cli', 'a1', 'r1', 'sess1', ?)", (i, f"2026-10-01T00:00:0{i}Z", t, json.dumps(p, ensure_ascii=False)))
    s.conn.execute("INSERT INTO manager_conversations (id, channel, agent_id, created_at, updated_at) VALUES ('mc_1','web','mgr',?,?)", (now, now))
    s.conn.execute("INSERT INTO manager_messages (conversation_id, role, text, ts) VALUES ('mc_1','user','lên lịch báo cáo hằng tuần',?)", (now,))
    s.close()

    monkeypatch.setattr(store_mod, "MIGRATIONS", MIGRATIONS)
    s2 = Store(db)
    assert s2.schema_version == len(MIGRATIONS) >= 17
    # migration KHÔNG backfill (chặn khởi động daemon với DB lớn) — đặt lịch; tin mới ghi sau migration được chỉ mục ngay
    assert ss.backfill_pending(s2.conn) and s2.conn.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0] == 0
    assert ss.search(s2.conn, {"query": "cau hinh"})["backfill_pending"] is True
    while ss.backfill_step(s2.conn, batch=2):  # lô nhỏ: con trỏ đi qua nhiều bước
        pass
    assert not ss.backfill_pending(s2.conn)
    roles = {r["role"] for r in s2.conn.execute("SELECT role FROM session_messages")}
    assert {"user", "assistant", "tool_call", "tool_result"} <= roles
    texts = [r[0] for r in s2.conn.execute("SELECT text FROM session_messages")]
    assert not any("không được chỉ mục" in t for t in texts)  # delta không vào chỉ mục
    hits = ss.search(s2.conn, {"query": "cau hinh may chu"})["hits"]
    assert {h["run_id"] for h in hits} == {"r1"} and {h["role"] for h in hits} == {"user", "assistant"}
    assert ss.search(s2.conn, {"query": "nginx"})["hits"][0]["role"] in ("tool_call", "tool_result")
    assert ss.search(s2.conn, {"query": "zebra"})["hits"][0]["run_id"] == "r0"
    mh = ss.search(s2.conn, {"query": "báo cáo hằng tuần"})["hits"]
    assert mh and mh[0]["conversation_id"] == "mc_1" and mh[0]["kind"] == "manager_chat"
    # migration chạy lại không nhân đôi (ref UNIQUE)
    n = s2.conn.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0]
    assert ss.backfill(s2.conn) == 0 and s2.conn.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0] == n
    s2.close()


async def test_backfill_runs_in_background_after_start(hub_home):
    import asyncio

    h = Hub(hub_home, runtime=FakeRuntime())
    try:
        h.agents.create({"id": "a", "name": "A"})
        c = h.store.conn
        c.execute("INSERT INTO runs (id, agent_id, kind, prompt, status, queued_at, session_id) VALUES ('rx','a','adhoc','tìm con kangaroo','completed','t','sx')")
        ss._set_state(c, {"phase": "runs", "cursor": 0, "hi": {"runs": c.execute("SELECT MAX(rowid) FROM runs").fetchone()[0], "events": 0, "manager": 0}})
        assert _search(h, query="kangaroo")["hits"] == []
        await ss.run_backfill(h, batch=1, pause_s=0)
        assert _search(h, query="kangaroo")["hits"][0]["run_id"] == "rx" and not ss.backfill_pending(c)
        h.start()
        await asyncio.sleep(0)
    finally:
        await h.stop()
        h.store.close()


def test_tokenizer_choice_and_fallback():
    c = sqlite3.connect(":memory:")
    tok = ss.pick_tokenizer(c)
    assert tok is not None and (tok.startswith("trigram") or tok.startswith("unicode61"))
    # fallback unicode61 vẫn tìm được tiếng Việt (bản gấp dấu)
    from cfharness.store import _split_sql

    for stmt in _split_sql(ss.SCHEMA):
        c.execute(stmt)
    c.execute("CREATE TABLE runs (id TEXT, agent_id TEXT, team_id TEXT, task_id TEXT, session_id TEXT, kind TEXT, run_opts TEXT)")
    c.execute("CREATE VIRTUAL TABLE session_fts USING fts5(folded, content='session_messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2')")
    c.execute("CREATE TRIGGER session_messages_ai AFTER INSERT ON session_messages BEGIN INSERT INTO session_fts(rowid, folded) VALUES (new.id, new.folded); END")
    c.row_factory = sqlite3.Row
    ss.Indexer(c).add("x:1", "run", {"agent_id": "a"}, role="assistant", text=VI_TEXT, ts="2026-10-01T00:00:00Z")
    r = ss.search(c, {"query": "duong dan"})
    assert r["engine"] == "unicode61" and len(r["hits"]) == 1


def test_fold_keeps_length_and_strips_vietnamese_marks():
    s = "Đường Dẫn Cấu Hình ỹ"
    f = ss.fold(s)
    assert len(f) == len(s) and f == "duong dan cau hinh y"


# ============================================================================ chỉ mục lúc ghi + tiếng Việt + che secret
def test_live_indexing_vietnamese_and_snippet(hub):
    hub.agents.create({"id": "a", "name": "A"})
    _run(hub, "r1", "a", prompt="Kiểm tra cấu hình")
    _ev(hub, "message.complete", "a", "r1", {"text": VI_TEXT})
    _ev(hub, "message.delta", "a", "r1", {"text": "delta-zzz"})
    _ev(hub, "user.message", "a", "r1", {"text": "thêm: kiểm tra quyền truy cập", "steer": True})
    _ev(hub, "tool.start", "a", "r1", {"name": "Read", "args_preview": "/etc/nginx/nginx.conf", "input": {"file_path": "/etc/nginx/nginx.conf"}})
    _ev(hub, "tool.complete", "a", "r1", {"name": "Read", "result_preview": "x" * 2000})
    for q in ("cấu hình", "cau hinh", "CẤU HÌNH", "đường dẫn", "duong dan log"):
        hits = _search(hub, query=q)["hits"]
        assert hits, q
    h = _search(hub, query="duong dan")["hits"][0]
    assert h["role"] == "assistant" and "«đường» «dẫn»" in h["snippet"]  # đoạn trích giữ dấu gốc, đánh dấu chỗ khớp
    assert _search(hub, query="delta-zzz")["hits"] == []
    assert _search(hub, query="quyen truy cap")["hits"][0]["role"] == "steer"
    assert _search(hub, query="nginx.conf")["hits"][0]["role"] == "tool_call"
    tr = hub.store.conn.execute("SELECT text FROM session_messages WHERE role='tool_result'").fetchone()[0]
    assert len(tr) <= ss.TOOL_RESULT_MAX
    # câu hỏi 2 ký tự (trigram không khớp) → lọc LIKE
    assert _search(hub, query='"ở /var"')["hits"]


def test_redaction_before_indexing(hub):
    from cfharness.redact import register_secret_values

    secret = "Zq9-very-private-value-77"
    register_secret_values(secret)
    hub.agents.create({"id": "a", "name": "A"})
    _run(hub, "r1", "a", prompt=f"dùng token {secret} để gọi API")
    _ev(hub, "message.complete", "a", "r1", {"text": "khoá là sk-ant-api03-abcdefghijklmnop và xong"})
    _ev(hub, "tool.start", "a", "r1", {"name": "Bash", "args_preview": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuv' x"})
    all_text = " ".join(r[0] + r[1] for r in hub.store.conn.execute("SELECT text, folded FROM session_messages"))
    assert secret not in all_text and secret.lower() not in all_text
    assert "sk-ant-api03" not in all_text and "abcdefghijklmnopqrstuv" not in all_text
    assert "***" in all_text
    assert _search(hub, query="very-private-value")["hits"] == []
    assert _search(hub, query="abcdefghijklmnop")["hits"] == []
    fts = " ".join(r[0] for r in hub.store.conn.execute("SELECT folded FROM session_fts"))
    assert "private" not in fts


def test_manager_messages_indexed_and_retention_keeps_text(hub):
    hub.agents.create({"id": "a", "name": "A"})
    hub.store.conn.execute("INSERT INTO manager_conversations (id, channel, agent_id, created_at, updated_at) VALUES ('mc_x','web','a','t','t')")
    hub.manager._add_msg("mc_x", "user", "nhắc tôi gia hạn tên miền", None)
    assert _search(hub, query="gia han ten mien")["hits"][0]["conversation_id"] == "mc_x"
    # retention xoá event cũ → văn bản đã chỉ mục vẫn còn (giữ theo vòng đời runs)
    _run(hub, "r_old", "a", prompt="p")
    hub.store.conn.execute("INSERT INTO events (epoch, seq, ts, type, level, source, agent_id, run_id, payload) VALUES "
                           "('old', 1, '2020-01-01T00:00:00Z', 'message.complete', 'summary', 'cli', 'a', 'r_old', ?)", (json.dumps({"text": "x"}),))
    ss.indexer_for(hub.store).index_event_row({"type": "message.complete", "ts": "2020-01-01T00:00:00Z", "epoch": "old", "seq": 1, "run_id": "r_old",
                                               "payload": {"text": "kỷ niệm năm 2020 quokka"}})
    from cfharness.recovery import purge_retention

    hub.config.set("retention.events_days", 30)
    assert purge_retention(hub)["events"] >= 1
    assert _search(hub, query="quokka")["hits"]


def test_purge_agent_removes_its_index(hub):
    hub.agents.create({"id": "gone", "name": "G"})
    _run(hub, "rg", "gone", prompt="bí mật kinh doanh platypus")
    assert _search(hub, query="platypus")["hits"]
    hub.agents.remove("gone")
    from cfharness.recovery import purge_agent

    purge_agent(hub, "gone", by="user", channel="cli")
    assert _search(hub, query="platypus")["hits"] == []


# ============================================================================ 4 chế độ
@pytest.fixture
def convo(hub):
    hub.agents.create({"id": "a", "name": "A"})
    hub.agents.create({"id": "b", "name": "B"})
    _run(hub, "r1", "a", prompt="bước 0 khởi động", session="S1", queued="2026-10-01T00:00:00Z")
    for i in range(1, 12):
        hub.publish(make_event("message.complete", "a", {"text": f"bước {i} của quy trình triển khai"}, run_id="r1", session_id="S1"))
    _run(hub, "r2", "a", prompt="tiếp tục bước 12", session="S1", queued="2099-01-01T00:00:00Z")  # resume cùng session
    _run(hub, "r3", "b", prompt="việc khác hoàn toàn của b", session="S3", queued="2026-10-02T00:00:00Z")
    _run(hub, "rv", "a", prompt="review quy trình triển khai", kind="review", session="SV")
    return hub


def test_mode_search_filters_and_paging(convo):
    r = _search(convo, query="quy trinh trien khai", limit=5)
    assert r["mode"] == "search" and len(r["hits"]) == 5 and r["next_offset"] == 5
    assert all(h["run_id"] != "rv" for h in r["hits"])  # ẩn phiên kỹ thuật mặc định
    assert any(h["run_id"] == "rv" for h in _search(convo, query="review quy trinh", include_technical=True)["hits"])
    assert _search(convo, query="viec khac", agent_id="a")["hits"] == []
    assert _search(convo, query="viec khac", agent_id="b")["hits"][0]["run_id"] == "r3"
    assert _search(convo, query="buoc", role="user", since="2098-01-01")["hits"][0]["run_id"] == "r2"
    with pytest.raises(HubError):
        _search(convo, query="  ")
    with pytest.raises(HubError):
        _search(convo, query="x", since="hôm qua")


def test_mode_around(convo):
    mid = next(h["id"] for h in _search(convo, query="buoc 6 cua")["hits"])
    r = _search(convo, mode="around", message_id=mid, window=2)
    texts = [m["text"] for m in r["messages"]]
    assert r["thread"] == {"type": "session", "id": "S1"}
    assert len(texts) == 5 and texts[2].startswith("bước 6") and r["messages"][2]["anchor"] is True
    assert texts[0].startswith("bước 4") and texts[4].startswith("bước 8")
    with pytest.raises(NotFound):
        _search(convo, mode="around", message_id=999999)


def test_mode_read_paged_session_and_run(convo):
    p1 = _search(convo, mode="read", session_id="S1", limit=5)
    assert p1["total"] == 13 and len(p1["messages"]) == 5 and p1["next_offset"] == 5
    assert p1["messages"][0]["text"].startswith("bước 0")
    last = _search(convo, mode="read", session_id="S1", limit=5, offset=10)
    assert last["next_offset"] is None and last["messages"][-1]["text"].startswith("tiếp tục bước 12")
    assert _search(convo, mode="read", run_id="r3")["total"] == 1
    with pytest.raises(NotFound):
        _search(convo, mode="read", run_id="khong-co")
    with pytest.raises(HubError):
        _search(convo, mode="read")


def test_mode_browse(convo):
    r = _search(convo, mode="browse")
    ids = [t["thread"]["id"] for t in r["threads"]]
    assert ids[0] == "S1" and "S3" in ids and "SV" not in ids
    s1 = r["threads"][0]
    assert s1["runs"] == 2 and s1["messages"] == 13 and s1["title"].startswith("bước 0")
    assert [t["thread"]["id"] for t in _search(convo, mode="browse", agent_id="b")["threads"]] == ["S3"]
    assert "bước 0" in ss.render_text(r)


# ============================================================================ phạm vi nhìn (daemon/RPC)
class _D:
    def __init__(self, hub):
        self.hub = hub
        self.methods = {}


@pytest.fixture
def scoped(hub):
    for aid in ("w1", "w2", "w3", "boss"):
        hub.agents.create({"id": aid, "name": aid})
    hub.teams.create({"id": "t1", "name": "T1", "members": [{"agent_id": "w1", "role": "worker"}, {"agent_id": "w2", "role": "worker"}]})
    _run(hub, "rw1", "w1", prompt="alpha của w1")
    _run(hub, "rw2", "w2", prompt="alpha của w2 trong team", team="t1")
    _run(hub, "rw2solo", "w2", prompt="alpha riêng của w2")
    _run(hub, "rw3", "w3", prompt="alpha của w3")
    hub.store.conn.execute("INSERT INTO manager_conversations (id, channel, agent_id, created_at, updated_at) VALUES ('mc_s','web','boss','t','t')")
    hub.manager._add_msg("mc_s", "user", "alpha kế hoạch của người", None)
    hub.store.update_run("rw1", {"status": "running"})
    from cfharness.mcp_server.surface_rpc import SurfaceRpc

    return hub, SurfaceRpc(_D(hub))


def _as(caller):
    return _auth.CURRENT_CALLER.set(caller)


def test_scope_worker_sees_own_and_team_only(scoped):
    hub, rpc = scoped
    tok = _as(_auth.Caller(via="socket", pid=1, run_id="rw1", agent_id="w1", evidence=["descendant_of_run_pid:1"]))
    try:
        runs = {h.get("run_id") for h in rpc.session_search({"query": "alpha"})["hits"]}
        assert runs == {"rw1", "rw2"}  # của mình + run team t1; không có w2 riêng, w3, hội thoại điều phối viên
        assert rpc.session_search({"query": "alpha"})["visibility"]["agents"] == ["w1"]
        with pytest.raises(NotFound):
            rpc.session_search({"mode": "read", "run_id": "rw3"})
        with pytest.raises(NotFound):
            rpc.session_search({"mode": "read", "conversation_id": "mc_s"})
        assert {t["thread"]["id"] for t in rpc.session_search({"mode": "browse"})["threads"]} == {"s_rw1", "s_rw2"}
        mid = hub.store.conn.execute("SELECT id FROM session_messages WHERE run_id='rw3'").fetchone()[0]
        with pytest.raises(NotFound):
            rpc.session_search({"mode": "around", "message_id": mid})
        # memory.search: không được hỏi agent khác; transcript cũng bị giới hạn
        with pytest.raises(Blocked):
            rpc.memory_search({"query": "alpha", "agent_id": "w3"})
        res = rpc.memory_search({"query": "alpha", "scope": "transcripts"})
        assert {r["run_id"] for r in res["results"]} <= {"rw1", "rw2"}
        # hub-self: run_id phải là run của chính tiến trình gọi
        assert rpc.session_search_self({"run_id": "rw1", "args": {"query": "alpha"}})["hits"]
        hub.store.update_run("rw3", {"status": "running"})
        with pytest.raises(Blocked):
            rpc.session_search_self({"run_id": "rw3", "args": {"query": "alpha"}})
        with pytest.raises(Blocked):
            rpc.memory_search_self({"run_id": "rw3", "args": {"query": "alpha"}})
    finally:
        _auth.CURRENT_CALLER.reset(tok)


def test_scope_manager_and_human_see_all_unknown_blocked(scoped):
    hub, rpc = scoped
    hub.store.state_set("manager", {"agent_id": "boss"})
    assert hub.manager.is_manager("boss")
    tok = _as(_auth.Caller(via="socket", pid=1, run_id="rb", agent_id="boss", evidence=["env_run_id"]))
    try:
        res = rpc.session_search({"query": "alpha"})
        assert {h.get("run_id") for h in res["hits"]} >= {"rw1", "rw2", "rw2solo", "rw3"}
        assert any(h.get("conversation_id") == "mc_s" for h in res["hits"]) and "visibility" not in res
    finally:
        _auth.CURRENT_CALLER.reset(tok)
    tok = _as(_auth.Caller(via="socket", pid=1, token_ok=True))  # người (token đúng, không dấu hiệu agent)
    try:
        assert len(rpc.session_search({"query": "alpha"})["hits"]) == 5
    finally:
        _auth.CURRENT_CALLER.reset(tok)
    tok = _as(_auth.Caller(via="socket", pid=1, token_ok=False))  # thiếu token, không thuộc run nào
    try:
        with pytest.raises(Blocked):
            rpc.session_search({"query": "alpha"})
        with pytest.raises(Blocked):
            rpc.memory_search({"query": "alpha"})
    finally:
        _auth.CURRENT_CALLER.reset(tok)
    # nội bộ (không có CURRENT_CALLER) → tất cả
    assert len(rpc.session_search({"query": "alpha"})["hits"]) == 5


def test_hub_self_memory_search_modes(scoped):
    hub, rpc = scoped
    from cfharness.mcp_server.hub_self import ROLE_TOOLS, TOOLS

    props = TOOLS["memory_search"]["inputSchema"]["properties"]
    assert {"mode", "message_id", "window", "run_id", "session_id", "since"} <= set(props)
    assert "memory_search" in ROLE_TOOLS["worker"]
    r = rpc.memory_search_self({"run_id": "rw1", "args": {"mode": "browse"}})
    assert r["mode"] == "browse" and {t["thread"]["id"] for t in r["threads"]} == {"s_rw1", "s_rw2"}
    r = rpc.memory_search_self({"run_id": "rw1", "args": {"query": "alpha", "scope": "sessions"}})
    assert {h["run_id"] for h in r["hits"]} == {"rw1", "rw2"}
    assert "memory.search_self" in _auth.SCOPED_READ and "session.search" in _auth.SCOPED_READ


# ============================================================================ daemon thật: CLI + MCP
@pytest.fixture
def daemon(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake", "fake": {"line_delay_s": 0.0}}, "runs": {"stop_grace_s": 1}}))
    from cfharness.daemon import spawn_background

    spawn_background(hub_home)
    yield hub_home
    from cfharness.cli import main

    main(["daemon", "stop"])


def _cli(capsys, *args):
    from cfharness.cli import main

    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


async def test_daemon_cli_and_mcp_session_search(daemon, capsys):
    from mcp import Client

    from cfharness.mcp_server.server import READ_ONLY_TOOLS, build_server
    from cfharness.rpc import RpcClient

    rpc = RpcClient(daemon.socket, timeout=30)
    rpc.call("agent.create", {"config": {"id": "ev", "name": "EV"}})
    run = rpc.call("run.start", {"agent": "ev", "prompt": "Kiểm tra máy chủ đường dẫn wombat"})
    for _ in range(100):
        if rpc.call("run.get", {"id": run["id"]})["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    res = rpc.call("session.search", {"query": "duong dan wombat"})
    assert res["hits"] and res["hits"][0]["run_id"] == run["id"]
    rd = rpc.call("session.search", {"mode": "read", "run_id": run["id"]})
    roles = [m["role"] for m in rd["messages"]]
    assert roles[0] == "user" and "tool_call" in roles and "assistant" in roles  # fixture: Bash ls → "xong"
    code, out, _ = _cli(capsys, "search", "đường", "dẫn")
    assert code == 0 and "«đường» «dẫn»" in out
    code, out, _ = _cli(capsys, "search", "--read", run["id"][:10])
    assert code == 0 and "assistant: xong" in out
    code, out, _ = _cli(capsys, "search", "--browse")
    assert code == 0 and "wombat" in out
    code, out, _ = _cli(capsys, "--json", "search", "--around", str(res["hits"][0]["id"]), "--window", "1")
    assert code == 0 and json.loads(out)["mode"] == "around"
    srv = build_server(lambda m, p: RpcClient(daemon.socket, timeout=60).call(m, p))
    async with Client(srv) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert "hub_session_search" in READ_ONLY_TOOLS and tools["hub_session_search"].annotations.read_only_hint is True
        r = (await c.call_tool("hub_session_search", {"query": "wombat"})).structured_content
        assert r["mode"] == "search" and r["hits"][0]["run_id"] == run["id"]
        r = (await c.call_tool("hub_session_search", {"mode": "browse"})).structured_content
        assert r["threads"]
        ms = (await c.call_tool("hub_memory_search", {"query": "wombat", "scope": "transcripts"})).structured_content
        assert ms["results"][0]["kind"] == "transcript" and ms["results"][0]["run_id"] == run["id"]
