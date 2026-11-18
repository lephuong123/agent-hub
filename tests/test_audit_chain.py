"""L4 — chuỗi băm audit (docs/dev-notes/plan-logging.md §4 A6, audit_chain.py): migration v30, genesis sau migration, sửa/xoá bằng sqlite
thô → verify báo đúng id, tính lại cả chuỗi → bản neo bắt được, neo theo chu kỳ + khi dừng, event audit.tamper_detected, HUMAN_ONLY,
sao lưu/khôi phục không báo nhầm, hiệu năng. Không gọi `claude`, không mạng."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from cfharness import audit_chain as AC
from cfharness import auth
from cfharness.audit import AuditLog
from cfharness.store import MIGRATIONS, Store, _split_sql, is_py_migration


def _raw(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db), isolation_level=None)
    c.execute("DROP TRIGGER IF EXISTS audit_no_update")
    c.execute("DROP TRIGGER IF EXISTS audit_no_delete")
    return c


def _fill(store: Store, n: int, start: int = 0) -> list[int]:
    a = AuditLog(store)
    return [a.record("test.row", None, None, {"i": i}, actor="user", channel="cli", target=f"t{i}", duration_ms=i * 1.5)
            for i in range(start, start + n)]


def _recompute_from(c: sqlite3.Connection, start_id: int) -> None:
    """Kẻ tấn công có quyền ghi hub.db: sửa xong tính lại prev_hash/row_hash cho mọi dòng từ start_id → chuỗi tự nhất quán."""
    cols = ", ".join(AC.CHAIN_COLUMNS)
    rows = c.execute(f"SELECT {cols}, prev_hash, row_hash FROM audit_log WHERE id >= ? ORDER BY id", (start_id,)).fetchall()
    prev = c.execute("SELECT row_hash FROM audit_log WHERE id < ? ORDER BY id DESC LIMIT 1", (start_id,)).fetchone()
    ph = prev[0] if prev else AC.GENESIS
    for r in rows:
        d = dict(zip([*AC.CHAIN_COLUMNS, "prev_hash", "row_hash"], r))
        h = AC.compute_hash(ph, d)
        c.execute("UPDATE audit_log SET prev_hash=?, row_hash=? WHERE id=?", (ph, h, d["id"]))
        ph = h


# ------------------------------------------------------------------ băm + migration
def test_v30_columns_and_hash_columns_not_updatable(hub_home):
    s = Store(hub_home.db)
    assert s.schema_version == len(MIGRATIONS) >= 30
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(audit_log)")}
    assert {"prev_hash", "row_hash"} <= cols
    (rid,) = _fill(s, 1)
    for col in ("row_hash", "prev_hash"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            s.conn.execute(f"UPDATE audit_log SET {col}='x' WHERE id=?", (rid,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s.conn.execute("DELETE FROM audit_log WHERE id=?", (rid,))
    s.close()


def test_canonical_json_contract():
    row = {"id": 3, "ts": "2026-11-18T00:00:00.000Z", "kind": "k", "actor": "user", "channel": "cli", "summary": "Tiếng Việt", "duration_ms": 2}
    raw = AC.canonical_json(row)
    d = json.loads(raw)
    assert list(d) == sorted(AC.CHAIN_COLUMNS) and b" " not in raw.replace("Tiếng Việt".encode(), b"")
    assert "Tiếng Việt".encode("utf-8") in raw and d["duration_ms"] == 2.0 and d["data"] is None
    assert AC.compute_hash("genesis", row) == AC.compute_hash("genesis", {**row, "id": "3", "duration_ms": 2.0})  # cùng kiểu như SQLite trả lại
    assert AC.compute_hash("genesis", row) != AC.compute_hash("genesis", {**row, "summary": "x"})


def test_chain_continuity_across_restarts(hub_home):
    s = Store(hub_home.db)
    _fill(s, 5)
    s.close()
    for k in range(3):  # "khởi động lại": Store + AuditLog mới mỗi lần
        s = Store(hub_home.db)
        _fill(s, 4, start=10 * (k + 1))
        s.close()
    s = Store(hub_home.db)
    rows = s.conn.execute("SELECT id, prev_hash, row_hash FROM audit_log ORDER BY id").fetchall()
    assert rows[0]["prev_hash"] == AC.GENESIS and all(rows[i]["prev_hash"] == rows[i - 1]["row_hash"] for i in range(1, len(rows)))
    assert [r["id"] for r in rows] == list(range(1, 18))
    res = AC.verify_chain(s.conn)
    assert res["ok"] and res["checked"] == 17 and res["chain_start_id"] == 1
    s.close()


def _v29_db(db: Path) -> None:
    c = sqlite3.connect(str(db), isolation_level=None)
    c.row_factory = sqlite3.Row
    for i, sql in enumerate(MIGRATIONS[:29], start=1):
        c.execute("BEGIN")
        if is_py_migration(sql):
            mod, fn = sql.rsplit(".", 1)
            getattr(importlib.import_module(f"cfharness.{mod}"), fn)(c)
        else:
            for st in _split_sql(sql):
                c.execute(st)
        c.execute(f"PRAGMA user_version={i}")
        c.execute("COMMIT")
    for i in range(3):
        c.execute("INSERT INTO audit_log (ts, kind, actor, channel, data) VALUES (?,?,?,?,?)", (f"2026-01-0{i + 1}T00:00:00Z", "old.row", "user", "cli", "{}"))
    c.close()


def test_genesis_after_migration_with_pre_v30_rows(hub_home):
    _v29_db(hub_home.db)
    s = Store(hub_home.db)
    assert s.schema_version == len(MIGRATIONS)
    last_old = AC.last_row(s.conn)
    assert last_old["id"] == 3 and last_old["row_hash"] is None
    _fill(s, 3)
    r4 = AC._as_dict(c := s.conn.execute(f"SELECT {', '.join(AC.CHAIN_COLUMNS)}, prev_hash, row_hash FROM audit_log WHERE id=4"), c.fetchone())
    assert r4["prev_hash"] == "genesis:" + __import__("hashlib").sha256(AC.canonical_json(last_old)).hexdigest()
    res = AC.verify_chain(s.conn)
    assert res["ok"] and res["unchained"] == 3 and res["chain_start_id"] == 4 and res["checked"] == 6
    s.close()
    # sửa dòng CUỐI trước v30 → genesis không khớp nữa
    c = _raw(hub_home.db)
    c.execute("UPDATE audit_log SET kind='forged' WHERE id=3")
    c.close()
    s = Store(hub_home.db)
    res = AC.verify_chain(s.conn)
    assert not res["ok"] and res["break"]["id"] == 4 and res["break"]["reason"] == "prev_mismatch"
    s.close()


def test_offline_writers_chain_too(hub_home):
    """hub_freeze.freeze_offline (daemon dừng) đi qua insert_chained — không còn chỗ nào INSERT audit_log ngoài chuỗi."""
    import re

    from cfharness import hub_freeze

    s = Store(hub_home.db)
    _fill(s, 2)
    s.close()
    hub_freeze.freeze_offline(hub_home, {"export_id": "x-20261118-000000", "sha256": "0" * 64})
    s = Store(hub_home.db)
    assert AC.verify_chain(s.conn)["ok"] and AC.last_row(s.conn)["kind"] == "hub.freeze"
    s.close()
    src = Path(AC.__file__).parent
    hits = [str(p.relative_to(src)) for p in src.rglob("*.py") if re.search(r"INSERT\s+INTO\s+audit_log", p.read_text(encoding="utf-8"))]
    assert hits == ["audit_chain.py"], hits


# ------------------------------------------------------------------ phát hiện sửa/xoá
def test_update_row_reports_exact_id(hub_home):
    s = Store(hub_home.db)
    _fill(s, 10)
    s.close()
    c = _raw(hub_home.db)
    c.execute("UPDATE audit_log SET actor='someone-else' WHERE id=6")
    c.close()
    s = Store(hub_home.db)
    res = AC.verify_chain(s.conn)
    assert not res["ok"] and res["break"]["id"] == 6 and res["break"]["reason"] == "hash_mismatch"
    assert res["checked"] == 6
    assert AC.verify_chain(s.conn, since_id=7)["ok"]  # từ sau chỗ sửa: dòng trước được tin
    s.close()


def test_delete_row_detected(hub_home):
    s = Store(hub_home.db)
    _fill(s, 10)
    s.close()
    c = _raw(hub_home.db)
    c.execute("DELETE FROM audit_log WHERE id=4")
    c.close()
    s = Store(hub_home.db)
    res = AC.verify_chain(s.conn)
    assert not res["ok"] and res["break"]["id"] == 5 and res["break"]["reason"] == "id_gap" and res["break"]["expected"] == 4
    s.close()


def test_unhashed_row_inside_chain_detected(hub_home):
    s = Store(hub_home.db)
    _fill(s, 3)
    s.close()
    c = _raw(hub_home.db)
    c.execute("INSERT INTO audit_log (ts, kind, actor, channel) VALUES ('2026-11-18T00:00:00Z','forged','user','cli')")
    c.close()
    s = Store(hub_home.db)
    res = AC.verify_chain(s.conn)
    assert res["break"] == {"id": 4, "reason": "unhashed_row", "expected": "row_hash", "actual": None}
    s.close()


def test_recomputed_chain_caught_by_anchor_and_deleted_tail_by_anchor(hub_home):
    from cfharness.hub import Hub

    hub = Hub(hub_home)
    _fill(hub.store, 8)
    a1 = hub.audit_chain.anchor("interval")
    assert a1 and a1["last_id"] == 8
    assert hub.audit_chain.anchor("interval") is None  # không có dòng mới → không lặp bản neo
    _fill(hub.store, 4, start=100)
    a2 = hub.audit_chain.anchor("interval")
    assert a2["last_id"] == 12
    assert hub.audit_chain.verify()["ok"]
    hub.store.close()
    # sửa dòng 5 rồi TÍNH LẠI cả chuỗi → chuỗi tự nhất quán nhưng không khớp bản neo
    c = _raw(hub_home.db)
    c.execute("UPDATE audit_log SET summary='che dấu' WHERE id=5")
    _recompute_from(c, 5)
    c.close()
    conn = sqlite3.connect(str(hub_home.db))
    assert AC.verify_chain(conn)["ok"]
    conn.close()
    res = AC.verify_db(hub_home.db, hub_home.home)
    assert not res["ok"] and res["break"] is None
    assert {(p["id"], p["reason"]) for p in res["anchors"]["problems"]} == {(8, "anchor_mismatch"), (12, "anchor_mismatch")}
    # xoá phần đuôi (không cần tính lại) → chỉ bản neo phát hiện được
    c = _raw(hub_home.db)
    c.execute("DELETE FROM audit_log WHERE id >= 9")
    c.execute("UPDATE sqlite_sequence SET seq=8 WHERE name='audit_log'")
    c.close()
    res = AC.verify_db(hub_home.db, hub_home.home)
    assert any(p == {"id": 12, "reason": "anchor_missing", "expected": a2["row_hash"], "actual": None} for p in res["anchors"]["problems"])
    assert AC.tamper_payload(res)["anchors"][-1] == {"id": 12, "reason": "anchor_missing"}


def test_anchor_file_format_and_bad_lines(hub_home):
    from cfharness.hub import Hub

    hub = Hub(hub_home)
    _fill(hub.store, 2)
    hub.audit_chain.anchor("start")
    p = AC.anchors_path(hub_home.home)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    rec = json.loads(p.read_text().splitlines()[0])
    assert set(rec) == {"v", "last_id", "row_hash", "ts", "reason"} and AC.is_hash(rec["row_hash"]) and rec["reason"] == "start"
    with open(p, "a") as f:
        f.write("không phải json\n" + json.dumps({"last_id": "x", "row_hash": "zz"}) + "\n")
    af = AC.read_anchors(p)
    assert af["bad_lines"] == 2 and len(af["anchors"]) == 1
    assert hub.audit_chain.verify()["ok"]
    hub.store.close()


async def test_anchor_on_interval_and_on_stop(hub_home):
    from cfharness.daemon import Daemon
    from cfharness.runtime.fake import FakeRuntime

    hub_home.config.write_text("audit:\n  anchor_interval_s: 1\n")
    d = Daemon(hub_home, runtime=FakeRuntime())
    hub = d.hub
    hub.start()
    p = AC.anchors_path(hub_home.home)
    try:
        n0 = len(AC.read_anchors(p)["anchors"])
        hub.audit.record("test.interval", None, None, {}, actor="user", channel="cli")
        for _ in range(40):
            await asyncio.sleep(0.1)
            got = AC.read_anchors(p)["anchors"]
            if len(got) > n0 and got[-1]["reason"] == "interval":
                break
        got = AC.read_anchors(p)["anchors"]
        assert got[-1]["reason"] == "interval" and got[-1]["last_id"] == AC.last_row(hub.store.conn)["id"]
        hub.audit.record("test.before_stop", None, None, {}, actor="user", channel="cli")
        last_id = AC.last_row(hub.store.conn)["id"]
    finally:
        await hub.stop()
    got = AC.read_anchors(p)["anchors"]
    assert got[-1]["reason"] == "stop" and got[-1]["last_id"] >= last_id
    assert AC.verify_db(hub_home.db, hub_home.home)["ok"]


def test_no_anchor_while_backup_running(hub_home):
    from cfharness import backup
    from cfharness.hub import Hub

    hub = Hub(hub_home)
    _fill(hub.store, 2)
    with backup._LOCK:
        assert hub.audit_chain.anchor("interval") is None
    assert hub.audit_chain.anchor("interval")["last_id"] == 2
    hub.store.close()


async def test_tamper_event_published_on_start_and_verify_rpc(hub_home):
    """Sửa trong lúc daemon dừng → daemon lên kiểm đuôi, phát audit.tamper_detected (payload chỉ id + mã) + dòng audit; không lặp lại khi
    khởi động lần nữa với cùng chỗ gãy; RPC audit.verify (người) phát lại."""
    from cfharness.daemon import Daemon
    from cfharness.runtime.fake import FakeRuntime

    s = Store(hub_home.db)
    _fill(s, 6)
    s.close()
    c = _raw(hub_home.db)
    c.execute("UPDATE audit_log SET data='{\"i\": 99}' WHERE id=3")
    c.close()
    seen: list = []
    d = Daemon(hub_home, runtime=FakeRuntime())
    d.hub.bus.add_listener(lambda ev: seen.append(ev) if ev.type == "audit.tamper_detected" else None)
    d.hub.start()
    try:
        assert len(seen) == 1
        assert seen[0].payload == {"checked": 3, "break_id": 3, "reason": "hash_mismatch"}  # không băm, không nội dung
        assert seen[0].level == "summary"
        row = d.hub.audit.query(kind="audit.tamper_detected", limit=1)[0]
        assert row["data"]["break_id"] == 3
        res = await d.methods["audit.verify"]({})
        assert not res["ok"] and res["break"]["id"] == 3 and len(seen) == 2
    finally:
        await d.hub.stop()
    seen.clear()
    d = Daemon(hub_home, runtime=FakeRuntime())
    d.hub.bus.add_listener(lambda ev: seen.append(ev) if ev.type == "audit.tamper_detected" else None)
    d.hub.start()
    try:
        assert seen == []  # cùng chỗ gãy đã báo → không báo lại mỗi lần khởi động
    finally:
        await d.hub.stop()


def test_tamper_event_forwarded_by_notifiers():
    from cfharness.config import DEFAULTS
    from cfharness.events import EVENT_LEVELS, make_event
    from cfharness.notify.slack import format_message

    for ch in ("slack", "telegram"):
        assert "audit.tamper_detected" in DEFAULTS["notify"][ch]["events"] and "audit.anchored" in DEFAULTS["notify"][ch]["events"]
    assert EVENT_LEVELS["audit.tamper_detected"] == "summary"
    t = format_message(make_event("audit.tamper_detected", None, {"checked": 9, "break_id": 7, "reason": "hash_mismatch",
                                                                  "anchors": [{"id": 12, "reason": "anchor_missing"}]}))
    assert "#7" in t and "#12" in t and "audit verify" in t
    assert format_message(make_event("audit.anchored", None, {"last_id": 5, "row_hash": "a" * 64, "reason": "interval"})).count("a" * 64) == 1
    assert DEFAULTS["audit"]["anchor_notify"] is False  # Q4: tắt mặc định


def test_anchored_event_only_when_notify_enabled(hub_home):
    from cfharness.hub import Hub

    hub = Hub(hub_home)
    seen: list = []
    hub.bus.add_listener(lambda ev: seen.append(ev) if ev.type == "audit.anchored" else None)
    _fill(hub.store, 1)
    hub.audit_chain.anchor("interval")
    assert seen == []
    hub.config.set("audit.anchor_notify", True)
    _fill(hub.store, 1, start=5)
    hub.audit_chain.anchor("interval")
    assert len(seen) == 1 and set(seen[0].payload) == {"last_id", "row_hash", "reason"}
    hub.store.close()


def test_anchor_to_admin_metadata(hub_home, tmp_path):
    from cfharness import admin_audit as aa
    from cfharness import admin_overview as ao
    from cfharness.hub import Hub

    assert aa.line_for("audit.anchor", "hub", "internal", {"anchor_id": 4, "anchor_hash": "zz"}) is None  # băm sai → không ghi
    r = aa.line_for("audit.anchor", "hub", "internal", {"anchor_id": 4, "anchor_hash": "b" * 64, "summary": "x"})
    assert set(r) == {"ts", "kind", "actor", "channel", "outcome", "anchor_id", "anchor_hash"}
    hub = Hub(hub_home)
    status = tmp_path / "status"
    status.mkdir()
    hub.config.set("admin_export.dir", str(status))
    _fill(hub.store, 2)
    assert hub.audit_chain.anchor("interval") is not None  # xuất admin tắt → chỉ file cục bộ
    assert not (tmp_path / "audit").exists()
    hub.config.set("admin_export.enabled", True)
    ud = tmp_path / "audit" / ao.os_user()
    ud.mkdir(parents=True)
    _fill(hub.store, 1, start=9)
    rec = hub.audit_chain.anchor("interval")
    lines = [json.loads(x) for x in (ud / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl").read_text().splitlines()]
    anc = [x for x in lines if x["kind"] == "audit.anchor"]
    assert anc and anc[-1]["anchor_id"] == rec["last_id"] and anc[-1]["anchor_hash"] == rec["row_hash"]
    rows = aa.read_audit(status, user=ao.os_user())["rows"]
    assert any(x.get("anchor_id") == rec["last_id"] for x in rows)  # bộ đọc admin giữ hai trường (AUDIT_SPEC)
    hub.store.close()


# ------------------------------------------------------------------ quyền
def test_verify_human_only_and_anchor_file_protected(hub_home):
    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI, GuardContext, classify
    from cfharness.guard.integrity import SECURITY_KEYS
    from cfharness.guard.policy import PolicyStore
    from cfharness.guard.runtime import sandbox_settings

    assert "audit.verify" in auth.HUMAN_ONLY and "audit.verify" not in auth.MANAGER_ALLOWED
    assert HUB_HUMAN_ONLY_CLI.get("audit", "missing") is None and "audit" in SECURITY_KEYS
    ps = PolicyStore(hub_home.home)
    ps.ensure()
    ctx = GuardContext(hub_home.home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
    for cmd in ("cfharness audit verify", "cfharness audit verify --since-id 3 --json"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd
    af = str(AC.anchors_path(hub_home.home))
    v = classify("Write", {"file_path": af, "content": "{}"}, ctx)
    assert v.verdict == "destructive" or v.forbidden, v
    sb = sandbox_settings(hub_home.home, hub_home.socket, {"enabled": True})
    assert af in sb["filesystem"]["denyWrite"]


async def test_verify_rpc_denied_for_non_human(hub_home):
    from cfharness import oscompat, transport
    from cfharness.daemon import Daemon
    from cfharness.runtime.fake import FakeRuntime

    d = Daemon(hub_home, runtime=FakeRuntime())
    d.hub.start()
    srv = await transport.start_server(d._handle_conn, hub_home.socket, limit=1 << 20)
    try:
        async def call(params: dict) -> dict:
            r, w = await transport.open_connection(str(hub_home.socket), limit=1 << 20)
            try:
                w.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "audit.verify", "params": params}) + "\n").encode())
                await w.drain()
                return json.loads(await r.readline())
            finally:
                w.close()

        tok = auth.token_path(hub_home.socket, "human").read_text().strip()
        ok = await call({"_auth": tok})
        assert ok["result"]["ok"] is True
        bad = await call({})
        assert bad["error"]["code"] == -32003
    finally:
        srv.close()
        oscompat.JOBS.close_all()
        await d.hub.stop()


def test_cli_verify_exit_codes(capsys):
    from types import SimpleNamespace

    from cfharness.mcp_server.cli import cmd_audit_verify

    outs = []

    def ctx_for(res):
        return SimpleNamespace(args=SimpleNamespace(since_id=None), call=lambda m, p: res, out=lambda r, t: outs.append(t))

    good = {"ok": True, "checked": 3, "first_id": 1, "last_id": 3, "chain_start_id": 1, "break": None,
            "anchors": {"file": True, "checked": 1, "total": 1, "latest": {"last_id": 3, "ts": "t"}, "problems": []}}
    assert cmd_audit_verify(ctx_for(good)) == 0 and "toàn vẹn" in outs[-1]
    bad = dict(good, ok=False, **{"break": {"id": 2, "reason": "hash_mismatch", "expected": "a", "actual": "b"}})
    bad["anchors"] = dict(good["anchors"], problems=[{"id": 3, "reason": "anchor_missing"}])
    assert cmd_audit_verify(ctx_for(bad)) == 1 and "#2" in outs[-1] and "BẢN NEO #3" in outs[-1]


# ------------------------------------------------------------------ sao lưu / chuyển hub
def test_backup_restore_no_false_alarm(hub_home):
    from cfharness import backup as B
    from cfharness.hub import Hub

    assert AC.ANCHORS_FILE in B.INCLUDE_FILES
    hub = Hub(hub_home)
    _fill(hub.store, 5)
    hub.audit_chain.anchor("interval")
    hub.store.close()
    r = B.create(hub_home)
    hub = Hub(hub_home)
    _fill(hub.store, 5, start=50)
    hub.audit_chain.anchor("interval")  # neo dòng 10+ — bản sao lưu không có
    hub.store.close()
    B.restore(hub_home, r["id"])
    res = AC.verify_db(hub_home.db, hub_home.home)
    assert res["ok"], res
    assert res["anchors"]["latest"]["last_id"] <= 6


def test_update_helper_restores_matching_anchors(hub_home, tmp_path):
    from cfharness import backup as B
    from cfharness import update_helper as U
    from cfharness.hub import Hub

    hub = Hub(hub_home)
    _fill(hub.store, 3)
    hub.audit_chain.anchor("interval")
    hub.store.close()
    r = B.create(hub_home, kind="pre-update")
    hub = Hub(hub_home)
    _fill(hub.store, 3, start=20)
    hub.audit_chain.anchor("interval")
    hub.store.close()
    log = open(tmp_path / "u.log", "w")
    U.restore_db(hub_home.home, B.backup_dir(hub_home) / f"{r['id']}.tar.gz", log)
    log.close()
    assert AC.verify_db(hub_home.db, hub_home.home)["ok"]


# ------------------------------------------------------------------ hiệu năng
def test_performance_1000_records(hub_home):
    s = Store(hub_home.db)
    t0 = time.monotonic()
    _fill(s, 1000)
    dt_write = time.monotonic() - t0
    t0 = time.monotonic()
    res = AC.verify_chain(s.conn)
    dt_verify = time.monotonic() - t0
    assert res["ok"] and res["checked"] == 1000
    assert dt_write < 5.0, dt_write
    assert dt_verify < 1.0, dt_verify
    s.close()
