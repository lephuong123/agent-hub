"""L6 — công cụ debug (docs/dev-notes/plan-logging.md B5/B6): `cfharness logs` (lọc theo trường JSON, cả file xoay `.N`/`.gz`),
`cfharness trace` / RPC `trace.get` (dòng thời gian audit + log + event tóm tắt + guard + approval + run con), trang web `/logs` + `/trace/<id>`,
bộ đếm lỗi trong `hub.status` + thẻ Sức khoẻ, mục "Lỗi gần đây" của `hub doctor`, agent không đọc được `HUB_HOME/logs/`.

Không gọi `claude`, không mạng.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cfharness import auth
from cfharness.log_query import ErrorCounter, Filters, msg_template, parse_time, query, recent_errors
from cfharness.logs_rpc import run_trace
from cfharness.mcp_server.rpc import SENSITIVE_READS
from cfharness.paths import HubPaths
from cfharness.store import Store
from cfharness.trace import new_req_id
from cfharness.trace_view import event_summary, text

T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)  # quá khứ: mtime file (bây giờ) luôn sau mốc


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def at(ms: int) -> str:
    return iso(T0 + timedelta(milliseconds=ms))


def line(ms: int, **kw) -> str:
    d = {"ts": at(ms), "level": "info", "logger": "cfharness.x", "msg": "m", "proc": "daemon", **kw}
    return json.dumps(d, ensure_ascii=False)


# ============================================================================ logs: lọc theo trường
@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    rq = "r_01JABCDEFGHJKMNPQRSTVWXYZ0"
    (d / "hub.log").write_text("\n".join([
        line(5000, level="warning", logger="cfharness.guard.service", msg="guard chậm", req_id=rq),
        line(6000, level="error", logger="cfharness.guardian", msg="không phải guard"),
        "đây không phải JSON {",
        line(7000, level="info", logger="cfharness.rpc", msg=f"nhắc tới {rq} trong chữ"),  # chỉ chuỗi con — KHÔNG khớp --req
        json.dumps(["mảng", "không phải object"]),
        line(8000, level="debug", logger="cfharness.runs", msg="debug", run_id="r_aaaaaaaaaaaa"),
    ]) + "\n")
    (d / "hub.log.1").write_text(line(1000, level="error", logger="cfharness.guard", msg="cũ trong .1", req_id=rq) + "\n")
    with gzip.open(d / "web.jsonl.2.gz", "wt", encoding="utf-8") as f:
        f.write(line(500, level="warning", logger="cfharness.web", msg="nén gz", proc="web", req_id=rq) + "\n")
    (d / "mcp.jsonl").write_text(line(9000, level="info", logger="cfharness.mcp", msg="mcp", proc="mcp") + "\n")
    (d / "daemon.out").write_text("text thô không đọc\n")
    old = time.time() - 3 * 86400
    os.utime(d / "hub.log.1", (old, old))
    os.utime(d / "web.jsonl.2.gz", (old, old))
    return d


def test_field_filters_rotated_and_gz(logs_dir: Path):
    rq = "r_01JABCDEFGHJKMNPQRSTVWXYZ0"
    res = query(logs_dir, Filters(req=rq))
    assert [r["msg"] for r in res["rows"]] == ["nén gz", "cũ trong .1", "guard chậm"]  # gộp theo ts, cả .1 và .gz; chuỗi con bị loại
    assert {"hub.log", "hub.log.1", "web.jsonl.2.gz"} <= set(res["files"]) and "daemon.out" not in res["files"]
    # logger khớp tiền tố theo dấu chấm (cfharness.guardian KHÔNG thuộc cfharness.guard)
    assert [r["msg"] for r in query(logs_dir, Filters(logger="cfharness.guard"))["rows"]] == ["cũ trong .1", "guard chậm"]
    # level = tối thiểu
    assert [r["level"] for r in query(logs_dir, Filters(level="error"))["rows"]] == ["error", "error"]
    # proc theo trường
    assert [r["msg"] for r in query(logs_dir, Filters(proc="web"))["rows"]] == ["nén gz"]
    assert [r["msg"] for r in query(logs_dir, Filters(run="r_aaaaaaaaaaaa"))["rows"]] == ["debug"]
    # thời gian
    rows = query(logs_dir, Filters(since=at(5500), until=at(8500)))["rows"]
    assert [r["msg"] for r in rows] == ["không phải guard", f"nhắc tới {rq} trong chữ", "debug"]
    # trần dòng: N dòng MỚI NHẤT + cờ more
    r2 = query(logs_dir, Filters(), limit=2)
    assert [r["msg"] for r in r2["rows"]] == ["debug", "mcp"] and r2["more"]
    with pytest.raises(ValueError):
        Filters(level="loud")
    with pytest.raises(ValueError):
        Filters(req="a b; rm")


def test_since_skips_old_files_and_byte_cap(logs_dir: Path, tmp_path: Path):
    # --since gần đây: file có mtime cũ (hub.log.1, .gz) không bị đọc
    res = query(logs_dir, Filters(since=parse_time("1h")))
    assert "hub.log.1" not in res["files"] and "web.jsonl.2.gz" not in res["files"]
    # trần byte: file lớn chỉ đọc phần đuôi, có cờ truncated, dòng mới nhất vẫn có
    big = tmp_path / "big"
    big.mkdir()
    (big / "hub.log").write_text("\n".join(line(i, msg=f"dòng {i}") for i in range(20000)) + "\n")
    r = query(big, Filters(), limit=5, max_bytes=50_000)
    assert r["truncated"] and r["scanned_bytes"] <= 50_000
    assert r["rows"][-1]["msg"] == "dòng 19999"


def test_parse_time():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    assert parse_time("2h", now) == "2026-11-18T10:00:00.000Z"
    assert parse_time("2026-11-18T08:00:00Z", now) == "2026-11-18T08:00:00.000Z"
    assert parse_time("", now) is None
    with pytest.raises(ValueError):
        parse_time("hôm qua", now)


def test_cli_logs_local_fallback_and_level_still_works(hub_home: HubPaths, capsys):
    """Daemon dừng → `cfharness logs` đọc file trực tiếp; `logs level` vẫn là lệnh con; `daemon logs` vẫn chạy."""
    from cfharness.cli import build_parser, main

    hub_home.logs_dir.mkdir(parents=True, exist_ok=True)
    (hub_home.logs_dir / "hub.log").write_text(line(0, level="error", logger="cfharness.guard", msg="lỗi \x1b[31mđỏ\x1b[0m", req_id=new_req_id()) + "\n")
    rc = main(["logs", "--since", "100000d", "--level", "error", "--home", str(hub_home.home)])
    out = capsys.readouterr()
    assert rc == 0 and "cfharness.guard" in out.out and "\x1b[" not in out.out  # termsafe: bỏ mã ANSI
    assert "daemon không chạy" in out.err
    rc = main(["logs", "--json", "--local", "--logger", "cfharness.guard", "--home", str(hub_home.home)])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and len(data["rows"]) == 1
    ns = build_parser().parse_args(["logs", "level", "cfharness", "debug"])
    assert ns.fn.__name__ == "cmd_level"
    assert build_parser().parse_args(["logs", "-n", "5"]).fn.__name__ == "cmd_query"
    assert main(["daemon", "logs", "-n", "5", "--home", str(hub_home.home)]) == 0


# ============================================================================ trace: ghép thứ tự từ hub giả
def _db(paths: HubPaths) -> Store:
    paths.ensure()
    st = Store(paths.db)
    st.conn.execute("INSERT INTO agents (id, name, home_path, created_at, updated_at) VALUES ('a1', 'A1', '/x', ?, ?)", (at(0), at(0)))
    return st


def _run(st: Store, rid: str, q: int, s: int, e: int | None, req: str | None, parent: str | None = None, task: str | None = None) -> None:
    st.conn.execute("INSERT INTO runs (id, agent_id, kind, status, outcome, queued_at, started_at, ended_at, req_id, parent_run_id, task_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, "a1", "adhoc", "completed" if e else "running", "success" if e else None, at(q), at(s), at(e) if e else None, req, parent, task))


_SEQ = [0]


def _event(st: Store, ms: int, typ: str, run: str | None, req: str | None, payload: dict, level: str = "summary", task: str | None = None) -> None:
    _SEQ[0] += 1
    st.conn.execute("INSERT INTO events (epoch, seq, ts, type, level, source, agent_id, run_id, task_id, payload, req_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("e-1", _SEQ[0], at(ms), typ, level, "adapter", "a1", run, task, json.dumps(payload), req))


def _audit(st: Store, ms: int, kind: str, run: str | None, req: str | None, summary: str = "") -> None:
    st.conn.execute("INSERT INTO audit_log (ts, kind, actor, channel, run_id, req_id, outcome, summary, data, principal) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (at(ms), kind, "user", "web", run, req, "ok", summary, json.dumps({"value": "SECRETDATA"}), "web:sess_1"))


def _fixture_hub(paths: HubPaths) -> tuple[str, str]:
    st = _db(paths)
    t0ms = int(T0.timestamp() * 1000)
    req, kid_req = new_req_id(t0ms), new_req_id(t0ms + 11)
    _audit(st, 1, "run.start", None, req, "start <b>")
    _event(st, 2, "task.status", None, req, {"status": "running", "text": "TASK-TEXT"})
    _run(st, "r_000000000001", 3, 4, 20, req)
    _event(st, 5, "tool.start", "r_000000000001", req, {"name": "Bash", "input": {"command": "SECRETCMD"}, "text": "<script>alert(1)</script>"})
    _event(st, 5, "message.delta", "r_000000000001", req, {"text": "DELTA"}, level="detail")
    st.conn.execute("INSERT INTO guard_decisions (ts, run_id, agent_id, tool_name, input_redacted, matched_rule, verdict, action, reason)"
                    " VALUES (?,?,?,?,?,?,?,?,?)", (at(6), "r_000000000001", "a1", "Bash", "SECRETINPUT", "rm_recursive", "destructive", "ask", "lý do RAW"))
    st.conn.execute("INSERT INTO approval_requests (id, run_id, agent_id, tool_name, tool_input, action_fingerprint, status, opened_at, resolved_at,"
                    " resolved_by, resolved_channel, reason, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("ap_1", "r_000000000001", "a1", "Bash", "SECRETAPPROVAL", "fp", "allowed", at(7), at(9), "user", "web", "RAWREASON", "RAWNOTE"))
    _run(st, "r_000000000002", 12, 13, 15, new_req_id(t0ms + 12), parent="r_000000000001")  # tiếp nối
    _run(st, "r_000000000003", 16, 17, 18, kid_req)  # lệnh con (agent gọi trong run) — chỉ biết qua parent_req_id trong log
    _event(st, 30, "run.completed", "r_000000000099", None, {})  # run khác — không được vào
    st.conn.commit()
    st.close()
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    (paths.logs_dir / "hub.log").write_text("\n".join([
        line(8, logger="cfharness.rpc", msg="rpc approval.approve ok", req_id=req, event="rpc", method="approval.approve"),
        line(11, logger="cfharness.rpc", msg="rpc task.comment ok", req_id=kid_req, parent_req_id=req),
        line(19, logger="cfharness.runs", msg="run xong", run_id="r_000000000001"),
        line(25, logger="cfharness.rpc", msg="lệnh khác", req_id=new_req_id(t0ms + 25)),
    ]) + "\n")
    return req, kid_req


def test_trace_merge_order_from_fixture_hub(tmp_path: Path):
    paths = HubPaths.resolve(tmp_path / "h")
    req, kid = _fixture_hub(paths)
    res = run_trace(paths.db, paths.logs_dir, req)
    seq = [(it["source"], it["label"]) for it in res["items"]]
    assert seq == [
        ("audit", "run.start"),
        ("event", "task.status"),
        ("run", "xếp hàng r_000000000001"),
        ("run", "bắt đầu r_000000000001"),
        ("event", "tool.start"),
        ("guard", "destructive → ask"),
        ("approval", "mở ap_1"),
        ("log", "info cfharness.rpc"),
        ("approval", "allowed ap_1"),
        ("log", "info cfharness.rpc"),  # lệnh con (parent_req_id)
        ("run", "xếp hàng r_000000000002"),
        ("run", "bắt đầu r_000000000002"),
        ("run", "kết thúc r_000000000002"),
        ("run", "xếp hàng r_000000000003"),
        ("run", "bắt đầu r_000000000003"),
        ("run", "kết thúc r_000000000003"),
        ("log", "info cfharness.runs"),
        ("run", "kết thúc r_000000000001"),
    ], seq
    rel = {r["id"]: r["relation"] for r in res["runs"]}
    assert rel == {"r_000000000001": "root", "r_000000000002": "child", "r_000000000003": "child_req"}
    assert kid in res["req_ids"]
    tool = next(it for it in res["items"] if it["label"] == "tool.start")
    assert tool["detail"] == {"src": "adapter", "name": "Bash"}  # event: chỉ trường trạng thái theo danh sách trắng
    blob = json.dumps(res, ensure_ascii=False)
    for bad in ("SECRETCMD", "<script>", "DELTA", "TASK-TEXT", "SECRETINPUT", "lý do RAW", "SECRETAPPROVAL", "RAWREASON", "RAWNOTE", "SECRETDATA",
                "r_000000000099", "lệnh khác"):
        assert bad not in blob, bad
    # theo run: cùng run + audit/log theo req của run; con vẫn có
    r2 = run_trace(paths.db, paths.logs_dir, "r_000000000001")
    assert r2["kind"] == "run" and {"r_000000000001", "r_000000000002"} <= {r["id"] for r in r2["runs"]}
    assert any(it["source"] == "guard" for it in r2["items"])
    assert "Dòng thời gian" in text(res) and "\x1b" not in text(res)
    nf = run_trace(paths.db, paths.logs_dir, "r_ffffffffffff")
    assert not nf["found"] and "không tìm thấy" in text(nf)
    with pytest.raises(ValueError):
        run_trace(paths.db, paths.logs_dir, "../../etc")


def test_event_summary_whitelist():
    assert event_summary({"name": "Bash", "status": "ok", "input": {"a": 1}, "text": "x"}) == {"name": "Bash", "status": "ok"}
    assert event_summary({"name": "có khoảng trắng và chữ dài", "status": "<b>"}) == {}


def test_trace_timing_month_of_data(tmp_path: Path):
    """< 2 s trên hub cỡ một tháng: 3000 run, 60k event, 20k audit, 10k guard, log 20 MB."""
    paths = HubPaths.resolve(tmp_path / "h")
    st = _db(paths)
    base = int(T0.timestamp() * 1000)
    runs, evs, auds, gds = [], [], [], []
    target_req = None
    for i in range(3000):
        ms = i * 800_000  # ~ 28 ngày
        rq = new_req_id(base + ms)
        if i == 1500:
            target_req = rq
        rid = f"r_{i:012x}"
        runs.append((rid, "a1", "adhoc", "completed", "success", at(ms), at(ms + 10), at(ms + 60_000), rq, None, None))
        for j in range(20):
            evs.append(("e-1", i * 20 + j, at(ms + 100 + j), "tool.start" if j % 2 else "message.complete", "summary", "adapter", "a1", rid, None,
                        json.dumps({"name": "Read"}), rq))
        for j in range(7):
            auds.append((at(ms + j), "run.start", "user", "cli", rid, rq, "ok", "", "{}", "os:x"))
        for j in range(3):
            gds.append((at(ms + 200 + j), rid, "a1", "Read", "{}", None, "safe", "allow", None))
    st.conn.executemany("INSERT INTO runs (id, agent_id, kind, status, outcome, queued_at, started_at, ended_at, req_id, parent_run_id, task_id)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", runs)
    st.conn.executemany("INSERT INTO events (epoch, seq, ts, type, level, source, agent_id, run_id, task_id, payload, req_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)", evs)
    st.conn.executemany("INSERT INTO audit_log (ts, kind, actor, channel, run_id, req_id, outcome, summary, data, principal) VALUES (?,?,?,?,?,?,?,?,?,?)", auds)
    st.conn.executemany("INSERT INTO guard_decisions (ts, run_id, agent_id, tool_name, input_redacted, matched_rule, verdict, action, reason)"
                        " VALUES (?,?,?,?,?,?,?,?,?)", gds)
    st.conn.commit()
    st.close()
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    with open(paths.logs_dir / "hub.log", "w") as f:
        for i in range(110_000):
            f.write(line(i * 20_000, logger="cfharness.rpc", msg="rpc run.get ok", req_id=new_req_id(base + i * 20_000)) + "\n")
    assert (paths.logs_dir / "hub.log").stat().st_size > 15_000_000
    for ident in (target_req, "r_0000000005dc"):
        t = time.perf_counter()
        res = run_trace(paths.db, paths.logs_dir, ident)
        dt = time.perf_counter() - t
        assert res["found"] and dt < 2.0, (ident, dt)
        assert any(it["source"] == "event" for it in res["items"]) and any(it["source"] == "audit" for it in res["items"])


# ============================================================================ quyền
def test_human_only_and_sensitive_reads_and_cli_blocked(tmp_path: Path):
    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI, GuardContext, classify
    from cfharness.guard.policy import PolicyStore

    for m in ("logs.query", "trace.get"):
        assert m in auth.HUMAN_ONLY and m not in auth.MANAGER_ALLOWED and m in SENSITIVE_READS, m
    assert HUB_HUMAN_ONLY_CLI.get("trace", "missing") is None and HUB_HUMAN_ONLY_CLI.get("logs", "missing") is None
    home = tmp_path / "hub"
    home.mkdir()
    ps = PolicyStore(home)
    ps.ensure()
    ctx = GuardContext(home / "agents", ps.load(), run_since=time.time(), git_probe=False)
    for cmd in ("cfharness trace r_abcdef123456", "cfharness logs --since 2h", "cfharness logs -f", "cfharness daemon logs -n 5"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd


def test_agent_cannot_read_hub_logs(tmp_path: Path):
    """Sandbox denyRead + permissions.deny (Read/Grep/Glob) + classifier sensitive_read cho HUB_HOME/logs/."""
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore
    from cfharness.guard.runtime import sandbox_settings, secret_read_deny_rules

    home = tmp_path / "hub"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "hub.log").write_text("{}\n")
    sock = home / "hub.sock"
    sb = sandbox_settings(home, sock, {})
    assert str(home / "logs") in sb["filesystem"]["denyRead"]
    rules = secret_read_deny_rules(home, sock)
    assert any(r.startswith("Read(") and f"{home}/logs/**" in r for r in rules)
    ps = PolicyStore(home)
    ps.ensure()
    ctx = GuardContext(home / "agents", ps.load(), run_since=time.time(), git_probe=False)
    for tool, inp in (("Read", {"file_path": str(home / "logs" / "hub.log")}), ("Bash", {"command": f"cat {home}/logs/hub.log"}),
                      ("Bash", {"command": f"tail -n 50 {home}/logs/web.jsonl.1"}), ("Grep", {"pattern": "x", "path": str(home / "logs")})):
        assert classify(tool, inp, ctx).forbidden, (tool, inp)


async def test_rpc_human_only_end_to_end(tmp_path: Path, monkeypatch):
    """Qua socket thật: không token → bị chặn; token người → kết quả + dòng audit `read:logs.query` / `read:trace.get`."""
    import shutil
    import tempfile

    from cfharness import oscompat, transport
    from cfharness.daemon import Daemon
    from cfharness.runtime.fake import FakeRuntime

    h = Path(os.path.realpath(tempfile.gettempdir() if os.name == "nt" else "/tmp")) / f"ahdbg-{os.getpid()}-{time.time_ns() % 10**6}"
    monkeypatch.setenv("CFHARNESS_HOME", str(h))
    monkeypatch.delenv("CFHARNESS_SOCKET", raising=False)
    p = HubPaths.resolve(h)
    p.ensure()
    p.config.write_text("guard:\n  git_probe: false\n")
    d = Daemon(p, runtime=FakeRuntime())
    d.hub.start()
    srv = await transport.start_server(d._handle_conn, p.socket, limit=16 * 1024 * 1024)
    try:
        p.logs_dir.mkdir(parents=True, exist_ok=True)
        (p.logs_dir / "hub.log").write_text(line(0, level="error", msg="lỗi mẫu") + "\n")

        async def call(method: str, params: dict) -> dict:
            reader, writer = await transport.open_connection(str(p.socket), limit=1 << 22)
            try:
                writer.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n").encode())
                await writer.drain()
                return json.loads(await reader.readline())
            finally:
                writer.close()

        tok = auth.token_path(p.socket, "human").read_text().strip()
        for m, prm in (("logs.query", {"since": "100000d"}), ("trace.get", {"id": "r_abcdef123456"})):
            r = await call(m, prm)
            assert "error" in r and r["error"]["code"] == -32003, (m, r)
            ok = await call(m, {**prm, "_auth": tok})
            assert "result" in ok, (m, ok)
        assert (await call("logs.query", {"since": "100000d", "_auth": tok}))["result"]["rows"][0]["msg"] == "lỗi mẫu"
        bad = await call("logs.query", {"level": "loud", "_auth": tok})
        assert bad["error"]["code"] == -32602
        kinds = {r["kind"] for r in d.hub.audit.query(kind="read:*", limit=50)}
        assert {"read:logs.query", "read:trace.get"} <= kinds
    finally:
        srv.close()
        oscompat.JOBS.close_all()
        await d.hub.stop()
        shutil.rmtree(h, ignore_errors=True)


# ============================================================================ sức khoẻ + doctor
def test_error_counter_buckets():
    c = ErrorCounter()
    lg = logging.getLogger("cfharness.test_l6_counter")
    lg.propagate = False
    lg.addHandler(c)
    try:
        lg.warning("w1")
        lg.error("e1")
        lg.error("e2")
        lg.info("không đếm")
        lg.warning("chậm", extra={"event": "rpc.slow"})
        rec = logging.LogRecord("cfharness.old", logging.ERROR, __file__, 1, "cũ", None, None)
        rec.created = time.time() - 2 * 3600
        c.handle(rec)
        snap = c.snapshot()
    finally:
        lg.removeHandler(c)
    assert snap["1h"]["error"] == 2 and snap["1h"]["warning"] == 2 and snap["1h"]["rpc_slow"] == 1
    assert snap["24h"]["error"] == 3
    top = {x["logger"]: x for x in snap["24h"]["by_logger"]}
    assert top["cfharness.test_l6_counter"] == {"logger": "cfharness.test_l6_counter", "error": 2, "warning": 2}
    rec2 = logging.LogRecord("cfharness.ancient", logging.ERROR, __file__, 1, "x", None, None)
    rec2.created = time.time() - 30 * 3600
    c.handle(rec2)
    assert c.snapshot()["24h"]["error"] == 3  # ngoài 24 h


def test_setup_logging_installs_counter_and_status_field(hub_home: HubPaths):
    from cfharness import log_query
    from cfharness.daemon import Daemon
    from cfharness.runtime.fake import FakeRuntime

    lg = logging.getLogger("cfharness")
    root = logging.getLogger()
    before_lg, before_root = list(lg.handlers), list(root.handlers)
    try:
        log_query.ensure_error_counter()
        log_query.ensure_error_counter()  # idempotent
        assert sum(1 for h in lg.handlers if h is log_query.COUNTER) == 1
        n0 = log_query.COUNTER.snapshot()["1h"]["error"]
        logging.getLogger("cfharness.l6probe").error("đếm tôi")
        d = Daemon(hub_home, runtime=FakeRuntime())
        st = d.hub.status()
        le = st["log_errors"]
        assert le["1h"]["error"] == n0 + 1 and "uncaught" in le and "bus_listener_errors" in le
        assert any(x["logger"] == "cfharness.l6probe" for x in le["24h"]["by_logger"])
        d.hub.store.close()
    finally:
        for h in [x for x in lg.handlers if x not in before_lg]:
            lg.removeHandler(h)
        for h in [x for x in root.handlers if x not in before_root]:
            root.removeHandler(h)


def test_doctor_recent_errors(hub_home: HubPaths):
    from cfharness import hub_doctor

    now = datetime.now(timezone.utc)
    hub_home.logs_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(6):
        rows.append(json.dumps({"ts": iso(now - timedelta(minutes=10 + i)), "level": "error", "logger": "cfharness.runs",
                                "msg": f"run r_{i:012x} lỗi sau {i * 7} giây", "req_id": new_req_id()}))
    rows.append(json.dumps({"ts": iso(now - timedelta(minutes=5)), "level": "critical", "logger": "cfharness.crash", "msg": "sập 1"}))
    rows.append(json.dumps({"ts": iso(now - timedelta(days=3)), "level": "error", "logger": "cfharness.old", "msg": "quá cũ"}))
    rows.append(json.dumps({"ts": iso(now - timedelta(minutes=1)), "level": "warning", "logger": "cfharness.w", "msg": "chỉ cảnh báo"}))
    (hub_home.logs_dir / "hub.log").write_text("\n".join(rows) + "\n")
    assert msg_template("run r_0123456789ab lỗi sau 21 giây") == "run <id> lỗi sau <n> giây"
    rep = recent_errors(hub_home.logs_dir)
    assert rep["groups"][0] == {"logger": "cfharness.runs", "template": "run <id> lỗi sau <n> giây", "count": 6,
                                "last_ts": rep["groups"][0]["last_ts"], "req_id": rep["groups"][0]["req_id"]}
    assert rep["groups"][0]["req_id"].startswith("r_") and len(rep["groups"]) == 2
    doc = hub_doctor.run(hub_home)
    chk = next(c for c in doc["checks"] if c["id"] == "recent_errors")
    assert chk["level"] == "warning" and "7 dòng ERROR" in chk["detail"]
    out = hub_doctor.text(doc)
    assert "Lỗi gần đây" in out and "6× cfharness.runs: run <id> lỗi sau <n> giây" in out and "quá cũ" not in out


# ============================================================================ web
async def test_web_logs_trace_pages_escape_and_health(hub_home: HubPaths, tmp_path: Path):
    from test_web import ISOLATED, _start, _stop

    from cfharness.runtime.fake import FakeRuntime

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        rq = new_req_id()
        hub_home.logs_dir.mkdir(parents=True, exist_ok=True)
        now = iso(datetime.now(timezone.utc))
        (hub_home.logs_dir / "hub.log").write_text(json.dumps({"ts": now, "level": "error", "logger": "cfharness.x", "proc": "daemon",
                                                               "msg": "<script>alert('xss')</script>", "req_id": rq,
                                                               "exc": "Traceback <img src=x onerror=alert(1)>"}) + "\n")
        await env.login()
        r = await env.client.get("/logs?level=error")
        assert r.status_code == 200
        assert "<script>alert" not in r.text and "&lt;script&gt;alert(" in r.text
        assert "<img src=x" not in r.text
        assert f'href="/trace/{rq}"' in r.text and 'href="/logs"' in r.text  # menu + link mã yêu cầu
        bad = await env.client.get("/logs?level=loud")
        assert bad.status_code == 200 and "Bộ lọc không hợp lệ" in bad.text
        # dòng thời gian của một run thật (FakeRuntime) — trang run link tới /trace/<run>
        env.hub.store.conn.execute("INSERT INTO audit_log (ts, kind, actor, channel, req_id, summary, data) VALUES (?,?,?,?,?,?,?)",
                                   (now, "agent.update", "user", "web", rq, "<b>tóm tắt</b>", "{}"))
        t = await env.client.get(f"/trace/{rq}")
        assert t.status_code == 200 and "agent.update" in t.text
        assert "<b>tóm tắt</b>" not in t.text and "&lt;b&gt;tóm tắt&lt;/b&gt;" in t.text
        assert "<script>alert" not in t.text
        assert (await env.client.get("/trace/..%2F..%2Fetc")).status_code == 404
        a = await env.client.get("/audit")
        assert f'href="/trace/{rq}"' in a.text
        h = await env.client.get("/partials/health")
        assert h.status_code == 200 and "Lỗi trong log (24 giờ)" in h.text and "Gọi RPC chậm" in h.text
        # chưa đăng nhập → không xem được
        env.client.cookies.clear()
        r2 = await env.client.get("/logs")
        assert r2.status_code in (302, 303, 401) and "alert" not in r2.text
    finally:
        await _stop(env)


async def test_web_run_page_links_trace(hub_home: HubPaths):
    from test_web import ISOLATED, _start, _stop, until

    from cfharness.runtime.fake import FakeRuntime

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        await env.login()
        env.hub.agents.create({"id": "dbg", "name": "dbg", "model": "haiku"})
        rid = env.hub.runs.submit("dbg", "xin chào")["id"]
        assert await until(lambda: (env.hub.store.get_run(rid) or {}).get("status") in ("completed", "failed"))
        page = await env.client.get(f"/runs/{rid}")
        assert f'href="/trace/{rid}"' in page.text
        t = await env.client.get(f"/trace/{rid}")
        assert t.status_code == 200 and rid in t.text and "xếp hàng" in t.text
    finally:
        await _stop(env)
