"""L5 — truy vấn/xuất audit + giao diện + lưu trữ (docs/dev-notes/plan-logging.md §4 A7, A9): lọc + phân trang, xuất jsonl/csv (0600, có cột
băm, không chứa giá trị đã che), web `/audit` (bộ lọc, cột kết quả, chống XSS ở summary/target, thẻ "Hoạt động theo người"), lưu trữ ra
`audit-archive/` mà `audit verify` vẫn qua, trigger v31 chặn xoá trên mốc, `verify --archive` bắt file bị sửa, HUMAN_ONLY. Không gọi `claude`."""

from __future__ import annotations

import csv
import gzip
import json
import os
import sqlite3
import stat
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import pytest

from cfharness import audit_archive as AA
from cfharness import audit_chain as AC
from cfharness import auth
from cfharness import store as store_mod
from cfharness.daemon import Daemon
from cfharness.hub import Hub
from cfharness.mcp_server.rpc import MUTATING, SENSITIVE_READS, is_audited
from cfharness.runtime.fake import FakeRuntime


def rec(h: Hub, kind: str, **kw):
    return h.audit.record(kind, kw.pop("agent_id", None), kw.pop("run_id", None), kw.pop("data", {}), actor=kw.pop("actor", "user"),
                          channel=kw.pop("channel", "cli"), **kw)


_REAL_NOW = store_mod.now_iso


def at(monkeypatch, iso: str | None) -> None:
    monkeypatch.setattr(store_mod, "now_iso", (lambda: iso) if iso else _REAL_NOW)


# ---------------------------------------------------------------------------- A7 lọc
def test_query_filters_and_pagination(hub_home, monkeypatch):
    h = Hub(hub_home)
    at(monkeypatch, "2026-11-01T10:00:00.000Z")
    rec(h, "agent.update", principal="os:boss", outcome="ok", target="coder", summary="agent.update")
    at(monkeypatch, "2026-11-10T10:00:00.000Z")
    rec(h, "run.start", principal="telegram:7", actor="user", outcome="denied", error_code="blocked", target="r_1", summary="run.start")
    rec(h, "approval.resolved", principal="web:dev_aaaa1111", actor="hub", outcome="ok", target="ap_1", summary="duyệt 50%_x",
        data={"note": "needle-in-data"})
    at(monkeypatch, "2026-11-17T10:00:00.000Z")
    from cfharness import trace

    with trace.bind():
        rid = rec(h, "approval.open", principal="os:boss", outcome="ok", target="needle-target", summary="s")
    q = h.audit.query
    assert [r["kind"] for r in q(principal="os:boss")] == ["approval.open", "agent.update"]
    assert [r["kind"] for r in q(actor="hub")] == ["approval.resolved"]
    assert [r["kind"] for r in q(outcome="denied")] == ["run.start"]
    assert [r["kind"] for r in q(kind_prefix="approval.")] == ["approval.open", "approval.resolved"]
    assert [r["kind"] for r in q(kind="approval.*")] == ["approval.open", "approval.resolved"]
    assert [r["kind"] for r in q(since="2026-11-05T00:00:00.000Z", until="2026-11-15T00:00:00.000Z")] == ["approval.resolved", "run.start"]
    assert [r["kind"] for r in q(since="2026-11-16T00:00:00.000Z")] == ["approval.open"]
    assert q(since="2027-01-01T00:00:00.000Z") == []
    # q tìm trong summary/target, KHÔNG BAO GIỜ trong data; ký tự đại diện LIKE được escape
    assert [r["kind"] for r in q(q="needle")] == ["approval.open"]
    assert q(q="needle-in-data") == []
    assert [r["kind"] for r in q(q="50%_x")] == ["approval.resolved"]
    assert q(q="5_%") == []
    # req_id (index) + phân trang before_id
    req = h.store.conn.execute("SELECT req_id FROM audit_log WHERE id=?", (rid,)).fetchone()[0]
    assert req and [r["id"] for r in q(req_id=req)] == [rid]
    page1 = q(limit=2)
    page2 = q(limit=2, before_id=min(r["id"] for r in page1))
    assert len(page1) == 2 and len(page2) == 2 and max(r["id"] for r in page2) < min(r["id"] for r in page1)
    assert q(limit=10**6) and len(q(limit=10**6)) <= 1000
    # kế hoạch truy vấn dùng index cho principal / outcome / req_id
    for where, arg in (("principal=?", "os:boss"), ("outcome=?", "denied"), ("req_id=?", req)):
        plan = " ".join(str(r[3]) for r in h.store.conn.execute(f"EXPLAIN QUERY PLAN SELECT * FROM audit_log WHERE {where} ORDER BY id DESC", (arg,)))
        assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan


async def test_rpc_list_relative_time_and_bad_params(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime())
    rec(d.hub, "agent.update", principal="os:boss", outcome="ok")
    rows = await d.methods["audit.list"]({"since": "1h", "principal": "os:boss"})  # chạy trên luồng đọc (daemon.OFFLOAD_READS)
    assert [r["kind"] for r in rows] == ["agent.update"]
    for bad in ({"outcome": "maybe"}, {"since": "hôm qua"}, {"before_id": "x"}):
        resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "audit.list", "params": bad})
        assert resp["error"]["code"] == -32602, bad


# ---------------------------------------------------------------------------- A7 xuất
@pytest.mark.parametrize("fmt", ["jsonl", "csv"])
async def test_export_file_mode_hash_columns_and_masked(hub_home, fmt):
    d = Daemon(hub_home, runtime=FakeRuntime())
    d.hub.secrets.set("MY_TOKEN", "SUPERSECRETVALUE-123456")
    rec(d.hub, "agent.update", principal="os:boss", outcome="ok", data={"leak": "SUPERSECRETVALUE-123456"}, target="coder")
    rec(d.hub, "run.start", principal="telegram:1", outcome="denied")
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "audit.export", "params": {"format": fmt, "since": "1d"}})
    r = resp["result"]
    p = Path(r["path"])
    assert p.parent == hub_home.home / "exports" and p.name.startswith("audit-") and p.suffix == f".{fmt}"
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    raw = p.read_text(encoding="utf-8")
    assert "SUPERSECRETVALUE" not in raw  # đã che lúc ghi, xuất nguyên chuỗi đã che
    if fmt == "jsonl":
        rows = [json.loads(x) for x in raw.splitlines()]
    else:
        rows = [{k: (v if v != "" else None) for k, v in x.items()} for x in csv.DictReader(raw.splitlines())]
    assert r["rows"] == len(rows) >= 2
    assert {"prev_hash", "row_hash", "principal", "outcome", "req_id"} <= set(rows[0])
    for x in rows:  # băm lại được từ file xuất
        assert AC.compute_hash(x["prev_hash"], x) == x["row_hash"]
    # lượt xuất được audit như thao tác ghi (sau khi xuất)
    assert d.hub.audit.query(kind="audit.export", limit=1)[0]["outcome"] == "ok"
    assert "audit.export" in MUTATING and is_audited(d.methods["audit.export"])


def test_activity_counts_per_principal_day(hub_home, monkeypatch):
    h = Hub(hub_home)
    now = datetime.now(timezone.utc)
    at(monkeypatch, now.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    for _ in range(3):
        rec(h, "agent.update", principal="os:boss", outcome="ok")
    rec(h, "run.start", principal="agent:coder", outcome="denied")
    rec(h, "x", principal=None)
    at(monkeypatch, "2020-01-01T00:00:00.000Z")
    rec(h, "old", principal="os:boss")
    a = h.audit.activity(days=7)
    assert len(a["days"]) == 7
    rows = {r["principal"]: r for r in a["rows"]}
    assert rows["os:boss"]["total"] == 3 and rows["os:boss"]["counts"][-1] == 3
    assert rows["agent:coder"]["denied"] == 1 and set(rows) == {"os:boss", "agent:coder"}


# ---------------------------------------------------------------------------- web
async def test_web_audit_filters_outcome_xss_and_activity(hub_home):
    from test_web import ISOLATED, _start, _stop

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        rec(env.hub, "agent.update", principal="os:boss", outcome="denied", error_code="blocked",
            target="<img src=x onerror=alert(1)>", summary="<script>alert('s')</script>")
        rec(env.hub, "run.start", principal="telegram:9", outcome="ok", target="r_ok")
        await env.login()
        r = await env.client.get("/audit")
        assert r.status_code == 200
        assert "<img src=x" not in r.text and "<script>alert" not in r.text and "&lt;img src=x" in r.text
        assert "outcome-denied" in r.text and "outcome-ok" in r.text and "bị từ chối" in r.text
        assert "Hoạt động theo người" in r.text and "telegram:9" in r.text
        f = await env.client.get("/audit?outcome=denied&principal=os:boss&since=1h")
        assert f.status_code == 200 and "agent.update" in f.text and "r_ok" not in f.text
        assert 'value="os:boss"' in f.text
        s = await env.client.get("/audit?q=r_ok")
        assert "r_ok" in s.text and "&lt;img" not in s.text
        bad = await env.client.get("/audit?since=<script>")
        assert bad.status_code == 200 and "<script>" not in bad.text.split("</form>")[1]
        css = (Path(__file__).parents[1] / "src" / "cfharness" / "web" / "static" / "app.css").read_text()
        assert ".pill.outcome-denied" in css
    finally:
        await _stop(env)


# ---------------------------------------------------------------------------- A9 lưu trữ
def _hub_with_months(hub_home, monkeypatch) -> Hub:
    h = Hub(hub_home)
    for month, n in (("2026-01", 4), ("2026-02", 3), ("2026-11", 3)):
        for i in range(n):
            at(monkeypatch, f"{month}-1{i}T08:00:00.000Z")
            rec(h, "agent.update", principal="os:boss", outcome="ok", target=f"{month}-{i}")
    at(monkeypatch, None)
    return h


def test_archive_off_by_default(hub_home, monkeypatch):
    h = _hub_with_months(hub_home, monkeypatch)
    assert h.audit_archive.run_once() == {"skipped": "off"}
    assert not AA.archive_dir(hub_home.home).exists()


def test_archive_moves_whole_months_and_verify_still_passes(hub_home, monkeypatch):
    h = _hub_with_months(hub_home, monkeypatch)
    h.audit_chain.anchor("interval")  # bản neo trỏ dòng #10 (vẫn trong DB)
    # bản neo trỏ vào dòng sẽ bị lưu trữ (#2) — ghi tay đúng định dạng
    r2 = h.store.conn.execute("SELECT row_hash FROM audit_log WHERE id=2").fetchone()[0]
    with open(AC.anchors_path(hub_home.home), "a") as f:
        f.write(json.dumps({"v": 1, "last_id": 2, "row_hash": r2, "ts": "x", "reason": "interval"}) + "\n")
    h.config.set("audit.archive_after_days", 30)
    res = h.audit_archive.run_once(now=datetime(2026, 11, 18, tzinfo=timezone.utc))
    assert [a["file"] for a in res["archived"]] == ["2026-01.jsonl.gz", "2026-02.jsonl.gz"], res
    d = AA.archive_dir(hub_home.home)
    if os.name == "posix":
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in d.iterdir())
    man = AA.read_manifest(hub_home.home)
    assert [(e["first_id"], e["last_id"], e["rows"]) for e in man["archives"]] == [(1, 4, 4), (5, 7, 3)]
    assert man["archives"][0]["anchors"] == [{"id": 2, "row_hash": r2}]
    ids = [r[0] for r in h.store.conn.execute("SELECT id FROM audit_log ORDER BY id")]
    assert ids[0] == 8 and h.store.state_get("audit.archive_cutoff") == 7
    assert h.audit.query(kind="audit.archived", limit=5)  # dòng audit của lượt lưu trữ
    # file lưu trữ mang đủ cột + cột băm, băm lại được
    lines = [json.loads(x) for x in gzip.open(d / "2026-01.jsonl.gz", "rt", encoding="utf-8")]
    assert [x["id"] for x in lines] == [1, 2, 3, 4] and all(AC.compute_hash(x["prev_hash"], x) == x["row_hash"] for x in lines)
    # verify vẫn qua: dòng đầu DB nối với next_prev của manifest; neo #2 đối chiếu với manifest
    v = AC.verify_db(hub_home.db, hub_home.home, archive=True)
    assert v["ok"], v
    assert v["archive_link_id"] == 7 and v["archive"]["files"] == 2 and v["archive"]["rows"] == 7
    assert v["anchors"]["archived"] == 1 and v["anchors"]["ok"]
    # dòng mới vẫn nối chuỗi; chạy lại không làm gì thêm (tháng hiện tại chưa trọn)
    rec(h, "after.archive", principal="os:boss")
    assert h.audit_archive.run_once(now=datetime(2026, 11, 18, tzinfo=timezone.utc))["archived"] == []
    assert AC.verify_db(hub_home.db, hub_home.home, archive=True)["ok"]
    # trigger v31: không xoá được dòng trên mốc (kể cả bằng sqlite thô)
    raw = sqlite3.connect(str(hub_home.db), isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("DELETE FROM audit_log WHERE id=8")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("UPDATE audit_log SET summary='x' WHERE id=9")
    raw.close()


def test_archive_tamper_detected(hub_home, monkeypatch):
    h = _hub_with_months(hub_home, monkeypatch)
    h.config.set("audit.archive_after_days", 30)
    h.audit_archive.run_once(now=datetime(2026, 11, 18, tzinfo=timezone.utc))
    d = AA.archive_dir(hub_home.home)
    # sửa một dòng trong file lưu trữ → sha256 lệch
    lines = gzip.open(d / "2026-02.jsonl.gz", "rt", encoding="utf-8").read().replace("2026-02-1", "2026-02-9")
    with gzip.open(d / "2026-02.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write(lines)
    v = AC.verify_db(hub_home.db, hub_home.home, archive=True)
    assert not v["ok"] and v["archive"]["problems"][0] == {"file": "2026-02.jsonl.gz", "reason": "sha_mismatch"}
    assert AC.tamper_payload(v)["archive"] == [{"file": "2026-02.jsonl.gz", "reason": "sha_mismatch"}]
    assert AC.verify_db(hub_home.db, hub_home.home)["ok"]  # không --archive: chuỗi DB + neo vẫn đúng
    # mất manifest → dòng đầu DB không nối được → archive_missing
    (d / AA.MANIFEST).unlink()
    v2 = AC.verify_db(hub_home.db, hub_home.home)
    assert not v2["ok"] and v2["break"]["reason"] == "archive_missing" and v2["break"]["id"] == 8


def test_archive_refuses_broken_chain_and_resumes_after_crash(hub_home, monkeypatch):
    h = _hub_with_months(hub_home, monkeypatch)
    h.config.set("audit.archive_after_days", 30)
    raw = sqlite3.connect(str(hub_home.db), isolation_level=None)
    raw.execute("DROP TRIGGER audit_no_update")
    raw.execute("UPDATE audit_log SET summary='sửa' WHERE id=3")
    res = h.audit_archive.run_once(now=datetime(2026, 11, 18, tzinfo=timezone.utc))
    assert res["archived"] == [] and res["error"] == "hash_mismatch" and res["error_id"] == 3
    assert not AA.archive_dir(hub_home.home).exists() or not list(AA.archive_dir(hub_home.home).glob("*.gz"))
    assert h.store.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] >= 10  # không xoá gì
    raw.close()


def test_archive_resume_after_crash_between_manifest_and_delete(hub_home, monkeypatch):
    h = _hub_with_months(hub_home, monkeypatch)
    h.config.set("audit.archive_after_days", 30)
    real = AA.AuditArchiver._delete
    monkeypatch.setattr(AA.AuditArchiver, "_delete", lambda self, a, b: (_ for _ in ()).throw(OSError("daemon chết")))
    res = h.audit_archive.run_once(now=datetime(2026, 11, 18, tzinfo=timezone.utc))
    assert res["error"] == "OSError" and len(AA.read_manifest(hub_home.home)["archives"]) == 1
    assert h.store.conn.execute("SELECT MIN(id) FROM audit_log").fetchone()[0] == 1
    monkeypatch.setattr(AA.AuditArchiver, "_delete", real)
    res2 = h.audit_archive.run_once(now=datetime(2026, 11, 18, tzinfo=timezone.utc))
    assert res2["archived"][0].get("resumed") and len(res2["archived"]) == 2
    assert AC.verify_db(hub_home.db, hub_home.home, archive=True)["ok"]


def test_archive_skips_while_quiesced(hub_home, monkeypatch):
    h = _hub_with_months(hub_home, monkeypatch)
    h.config.set("audit.archive_after_days", 30)
    h.runs.quiesced = True
    try:
        assert h.audit_archive.run_once(now=datetime(2026, 11, 18, tzinfo=timezone.utc)) == {"skipped": "busy"}
    finally:
        h.runs.quiesced = None


def test_migration_v31_trigger_conditional(hub_home):
    from cfharness.store import MIGRATIONS, Store

    s = Store(hub_home.db)
    assert s.schema_version == len(MIGRATIONS) >= 31
    a = Hub(hub_home).audit
    ids = [a.record("t", None, None, {}) for _ in range(3)]
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s.conn.execute("DELETE FROM audit_log WHERE id=?", (ids[0],))  # chưa có mốc → không xoá được gì
    s.state_set("audit.archive_cutoff", ids[0])
    s.conn.execute("DELETE FROM audit_log WHERE id=?", (ids[0],))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s.conn.execute("DELETE FROM audit_log WHERE id=?", (ids[1],))


def test_archive_dir_backup_and_agent_protection(hub_home):
    from cfharness import backup
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore
    from cfharness.guard.runtime import HUB_WRITE_DENY_NAMES, sandbox_settings, secret_read_deny_rules
    from cfharness.pathmap import SCAN_SKIP_DIRS

    assert "audit-archive" in backup.INCLUDE_DIRS and backup.included(PurePosixPath("audit-archive/2026-01.jsonl.gz"))
    assert "audit-archive" in SCAN_SKIP_DIRS and "audit-archive" in HUB_WRITE_DENY_NAMES
    from cfharness.hub_transfer import MACHINE_STATE

    assert "audit_watch.active" in MACHINE_STATE and "audit.archive_cutoff" not in MACHINE_STATE  # mốc đi cùng DB + audit-archive/
    home = hub_home.home
    rules = secret_read_deny_rules(home, hub_home.socket)
    assert any("audit-archive" in r and r.startswith("Read(") for r in rules)
    sb = sandbox_settings(home, hub_home.socket, {"enabled": True})
    fs = sb["filesystem"]
    assert any(str(x).endswith("/audit-archive") for x in fs["denyRead"])
    assert any("audit-archive" in str(x) for x in fs["denyWrite"])
    ps = PolicyStore(home)
    ps.ensure()
    ctx = GuardContext(home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
    for tool, inp in (("Bash", {"command": f"zcat {home}/audit-archive/2026-01.jsonl.gz"}), ("Read", {"file_path": f"{home}/audit-archive/MANIFEST.json"}),
                      ("Bash", {"command": f"rm -rf {home}/audit-archive"}), ("Bash", {"command": "cfharness audit export --since 7d"})):
        assert classify(tool, inp, ctx).forbidden, inp


# ---------------------------------------------------------------------------- quyền
async def test_new_rpcs_human_only(hub_home):
    for m in ("audit.export", "audit.activity", "audit.list", "audit.verify"):
        assert m in auth.HUMAN_ONLY and m not in auth.MANAGER_ALLOWED, m
    assert "audit.activity" in SENSITIVE_READS and "audit.export" in MUTATING
    d = Daemon(hub_home, runtime=FakeRuntime())
    agent = auth.Caller(via="socket", token_ok=True, run_id="r_x", agent_id="coder", evidence=["env"])
    for m in ("audit.export", "audit.activity", "audit.list"):
        resp = await d.call_as(agent, m, {"format": "jsonl"} if m == "audit.export" else {})
        assert "error" in resp and resp["error"]["code"] == -32003, (m, resp)
    assert not (hub_home.home / "exports").exists() or not list((hub_home.home / "exports").glob("audit-*"))


def test_backup_restore_carries_archive_and_verify_passes(hub_home, monkeypatch):
    from cfharness import backup as B

    h = _hub_with_months(hub_home, monkeypatch)
    h.config.set("audit.archive_after_days", 30)
    h.audit_archive.run_once(now=datetime(2026, 11, 18, tzinfo=timezone.utc))
    h.store.close()
    r = B.create(hub_home)
    assert B.verify(hub_home, r["id"])["ok"]
    # sau sao lưu: xoá mất thư mục lưu trữ → khôi phục mang nó về, verify (cả file) lại qua
    import shutil

    shutil.rmtree(AA.archive_dir(hub_home.home))
    assert not AC.verify_db(hub_home.db, hub_home.home)["ok"]
    B.restore(hub_home, r["id"])
    v = AC.verify_db(hub_home.db, hub_home.home, archive=True)
    assert v["ok"] and v["archive"]["files"] == 2, v
