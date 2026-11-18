"""L7 — gói chẩn đoán (docs/dev-notes/plan-logging.md B7) + phần L1 để lại (stderr sync filehub ra file xoay vòng, dòng JSON của
update_helper / import-job). Không gọi `claude`, không mạng."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import stat
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cfharness import auth
from cfharness import support_bundle as SB
from cfharness.daemon import Daemon
from cfharness.errors import HubError
from cfharness.hub import Hub
from cfharness.paths import HubPaths
from cfharness.redact import register_secret_values
from cfharness.runtime.fake import FakeRuntime

SECRET = "SupportBundleSecret-7f3a9c11"  # giá trị secret đã đăng ký (che theo GIÁ TRỊ)
GH_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # che theo MẪU
FORBID = "FORBIDDEN-MARKER-0b5e"
RID = "r_00000000beef"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _line(dt: datetime, msg: str, **kw) -> str:
    return json.dumps({"ts": _iso(dt), "level": "info", "logger": "cfharness.x", "msg": msg, "proc": "daemon", "pid": 1, **kw}) + "\n"


def _members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as tf:
        return {m.name: tf.extractfile(m).read() for m in tf.getmembers() if m.isfile()}  # type: ignore[union-attr]


@pytest.fixture
def hub(hub_home: HubPaths):
    register_secret_values(SECRET)
    h = Hub(hub_home)
    c = h.store.conn
    now = _iso(datetime.now(timezone.utc))
    c.execute("INSERT INTO agents (id, name, home_path, created_at, updated_at) VALUES ('a1', 'A1', '/x', ?, ?)", (now, now))
    c.execute("INSERT INTO runs (id, agent_id, kind, prompt, summary, status, outcome, queued_at, started_at, ended_at, req_id)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (RID, "a1", "adhoc", "PROMPT-TEXT-MARK", "SUMMARY-TEXT-MARK", "completed", "success",
                                                now, now, now, None))
    c.execute("INSERT INTO events (epoch, seq, ts, type, level, source, agent_id, run_id, payload) VALUES (?,?,?,?,?,?,?,?,?)",
              ("e-1", 1, now, "tool.start", "summary", "adapter", "a1", RID, json.dumps({"name": "Bash", "input": {"command": "EVENT-CMD-MARK"}})))
    h.audit.record("run.start", "a1", RID, {"prompt": "AUDIT-DATA-MARK"}, actor="user", channel="cli", summary="run.start")
    yield h
    h.store.close()


def _plant_logs(paths: HubPaths) -> None:
    now = datetime.now(timezone.utc)
    logs = paths.logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "hub.log").write_text(_line(now - timedelta(days=3), "OLD-LINE-MARK") + _line(now, f"new line token={SECRET}")
                                  + "not json at all\n" + _line(now, "NEW-LINE-MARK"))
    (logs / "daemon.out").write_text(f"Traceback… Authorization: {GH_TOKEN}\n")
    (logs / "faults.log").write_text("Fatal Python error: hello\n")


def _plant_forbidden(paths: HubPaths) -> None:
    home = paths.home
    for rel in ("secrets/API_KEY", "auth/web.json", "auth/totp.json", "mcp_server/tokens.json", "checkpoints/r_1/x.txt",
                "backups/20260101T000000-manual.tar.gz", "exports/old.cfhx", "imports/up-1/job.json", "audit-archive/2026-01.jsonl.gz",
                "agents/a1/MEMORY.md", "agents/a1/skills/s/SKILL.md", "global/USER.md", "logs/curation/hub.log", "claude/projects/x/s.jsonl"):
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_line(datetime.now(timezone.utc), FORBID))
    for t in auth.all_token_paths(paths.socket):
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(FORBID)
    if sys.platform != "win32":  # symlink trong logs/ trỏ ra file cấm → không theo
        os.symlink(home / "secrets" / "API_KEY", paths.logs_dir / "evil.jsonl")
        os.symlink(home / "auth" / "web.json", paths.logs_dir / "web.log")


# ============================================================================ nội dung
def test_forbidden_files_absent_even_when_present(hub, hub_home):
    _plant_logs(hub_home)
    _plant_forbidden(hub_home)
    res = SB.create(hub_home, since="24h", run_ids=[RID])
    mem = _members(Path(res["path"]))
    assert set(mem) >= {"MANIFEST.json", "meta/versions.json", "meta/doctor.json", "meta/status.json", "meta/config.json", "logs/hub.log",
                        f"runs/{RID}/run.json", f"runs/{RID}/trace.json"}
    for name, data in mem.items():
        assert name == "MANIFEST.json" or name.split("/", 1)[0] in ("meta", "logs", "runs"), name
        assert FORBID.encode() not in data, name
        assert not name.endswith(("hub.db", "tokens.json", ".auth", "MEMORY.md", "SKILL.md")), name
    assert "logs/evil.jsonl" not in mem and "logs/web.log" not in mem and "logs/curation/hub.log" not in mem
    # run: metadata + tóm tắt — không prompt/summary/payload/data audit; raw không có khi không chọn
    run_blob = mem[f"runs/{RID}/run.json"] + mem[f"runs/{RID}/trace.json"]
    for mark in (b"PROMPT-TEXT-MARK", b"SUMMARY-TEXT-MARK", b"EVENT-CMD-MARK", b"AUDIT-DATA-MARK"):
        assert mark not in run_blob
    tr = json.loads(mem[f"runs/{RID}/trace.json"])
    assert {i["source"] for i in tr["items"]} >= {"audit", "event", "run"}
    assert not any(n.startswith(f"runs/{RID}/raw") for n in mem)
    man = json.loads(mem["MANIFEST.json"])
    assert "hub.db" in man["never_included"] and man["cfharness_version"] and man["include_raw"] is False


def test_secret_in_log_and_config_masked_and_counted(hub, hub_home):
    _plant_logs(hub_home)
    hub_home.config.write_text(
        "notify:\n  slack:\n    channel: '#x " + SECRET + "'\n  telegram:\n    bot_token: plain-bot-token-value\n    allowed_chat_ids: [12345, 678]\n"
        "web:\n  public_origin: https://hub.example.test\n  port: 8765\n  totp_required: true\nlearning:\n  review_digest_over_tokens: 9000\n")
    res = SB.create(hub_home, since="24h")
    mem = _members(Path(res["path"]))
    for name, data in mem.items():
        assert SECRET.encode() not in data and GH_TOKEN.encode() not in data, name
        assert b"plain-bot-token-value" not in data and b"hub.example.test" not in data and b"12345" not in data or name.startswith("logs/"), name
    rep = json.loads(mem["MANIFEST.json"])["redaction_report"]
    assert rep["files"].get("logs/hub.log", 0) >= 1 and rep["files"].get("logs/daemon.out", 0) >= 1
    assert rep["files"].get("meta/config.json", 0) >= 1 and rep["total"] == sum(rep["files"].values())
    assert SECRET not in json.dumps(rep)  # chỉ số đếm
    cfg = json.loads(mem["meta/config.json"])
    assert cfg["web"] == {"public_origin": "***", "port": "***", "totp_required": True}  # web: chỉ bool
    assert cfg["notify"]["telegram"]["bot_token"] == "***" and cfg["notify"]["telegram"]["allowed_chat_ids"] == ["***", "***"]
    assert cfg["learning"]["review_digest_over_tokens"] == 9000  # số không phải id → giữ
    assert "***" in cfg["notify"]["slack"]["channel"]
    assert b"***" in mem["logs/hub.log"]
    # kế hoạch (dry_run) cho cùng số đếm, không ghi file
    plan = SB.create(hub_home, since="24h", dry_run=True)
    assert plan["redaction_report"]["files"].get("logs/hub.log") == rep["files"]["logs/hub.log"]
    assert len(SB.list_bundles(hub_home)) == 1


def test_time_window_filter(hub, hub_home):
    _plant_logs(hub_home)
    old = time.time() - 5 * 86400
    gz = hub_home.logs_dir / "hub.log.2.gz"
    gz.write_bytes(gzip.compress(_line(datetime.now(timezone.utc) - timedelta(days=5), "GZ-OLD-MARK").encode()))
    os.utime(gz, (old, old))
    recent_gz = hub_home.logs_dir / "web.jsonl.1.gz"
    now = datetime.now(timezone.utc)
    recent_gz.write_bytes(gzip.compress((_line(now - timedelta(days=2), "WEB-OLD-MARK") + _line(now, "WEB-NEW-MARK")).encode()))
    os.utime(hub_home.logs_dir / "faults.log", (old, old))
    mem = _members(Path(SB.create(hub_home, since="24h")["path"]))
    hl = mem["logs/hub.log"]
    assert b"NEW-LINE-MARK" in hl and b"OLD-LINE-MARK" not in hl and b"not json" not in hl
    assert "logs/hub.log.2" not in mem and "logs/faults.log" not in mem  # mtime trước since → bỏ cả file
    assert b"WEB-NEW-MARK" in mem["logs/web.jsonl.1"] and b"WEB-OLD-MARK" not in mem["logs/web.jsonl.1"]
    files = {f["path"]: f for f in json.loads(mem["MANIFEST.json"])["files"]}
    assert files["logs/hub.log"]["lines_before_since"] == 1 and files["logs/hub.log"]["lines_not_json"] == 1
    mem7 = _members(Path(SB.create(hub_home, since="7d")["path"]))
    assert b"OLD-LINE-MARK" in mem7["logs/hub.log"] and b"GZ-OLD-MARK" in mem7["logs/hub.log.2"]
    with pytest.raises(HubError):
        SB.create(hub_home, since="hôm qua", dry_run=True)


def test_size_caps(hub, hub_home):
    now = datetime.now(timezone.utc)
    for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
        (hub_home.logs_dir / name).write_text("".join(_line(now, f"{name} {i} " + "x" * 200) for i in range(200)))
    cfg = {"support": {"max_mb": 40 / 1024, "max_file_mb": 16 / 1024}}
    res = SB.create(hub_home, since="24h", cfg=cfg)
    assert res["max_bytes"] == 40 * 1024 and res["max_file_bytes"] == 16 * 1024
    assert all(f["size"] <= 16 * 1024 for f in res["files"]) and res["total"] <= 40 * 1024
    assert any(f.get("truncated") for f in res["files"] if f["path"].startswith("logs/"))
    assert res["skipped"] and all(s["reason"] == "total_cap" for s in res["skipped"])
    mem = _members(Path(res["path"]))
    assert sum(len(v) for k, v in mem.items() if k != "MANIFEST.json") == res["total"]
    for f in res["files"]:  # đuôi = dòng MỚI nhất, dòng nguyên vẹn
        if f["path"].startswith("logs/") and f.get("truncated"):
            lines = mem[f["path"]].decode().splitlines()
            assert lines and all(json.loads(x) for x in lines) and " 199 " in lines[-1]


def test_manifest_sha256_and_mode_0600(hub, hub_home):
    _plant_logs(hub_home)
    res = SB.create(hub_home, since="24h", run_ids=[RID])
    p = Path(res["path"])
    assert SB.NAME_RE.match(p.name) and p.parent == hub_home.home / "exports"
    if sys.platform != "win32":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    mem = _members(p)
    man = json.loads(mem.pop("MANIFEST.json"))
    assert {f["path"] for f in man["files"]} == set(mem)
    for f in man["files"]:
        assert hashlib.sha256(mem[f["path"]]).hexdigest() == f["sha256"] and len(mem[f["path"]]) == f["size"], f["path"]
    assert res["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert not list(p.parent.glob(".*.part"))


def test_raw_only_when_chosen(hub, hub_home):
    hub_home.runs_dir.mkdir(parents=True, exist_ok=True)
    (hub_home.runs_dir / f"{RID}.jsonl.gz").write_bytes(gzip.compress(f'{{"type":"assistant","text":"RAW-MARK {SECRET}"}}\n'.encode()))
    (hub_home.runs_dir / f"{RID}.stderr.log").write_text("STDERR-MARK\n")
    mem = _members(Path(SB.create(hub_home, run_ids=[RID])["path"]))
    assert not any(b"RAW-MARK" in v for v in mem.values())
    res = SB.create(hub_home, run_ids=[RID], include_raw=True)
    mem = _members(Path(res["path"]))
    raw = mem[f"runs/{RID}/raw.jsonl"]
    assert b"RAW-MARK ***" in raw and SECRET.encode() not in raw and b"STDERR-MARK" in mem[f"runs/{RID}/stderr.log"]
    assert json.loads(mem["MANIFEST.json"])["redaction_report"]["files"][f"runs/{RID}/raw.jsonl"] == 1
    with pytest.raises(HubError):
        SB.create(hub_home, run_ids=["r_doesnotexist"], dry_run=True)
    with pytest.raises(HubError):
        SB.create(hub_home, run_ids=["../../etc/passwd"], dry_run=True)


# ============================================================================ RPC / quyền
async def test_rpc_plan_create_raw_needs_confirm_and_audited(hub_home):
    from m7_fake import close, make_daemon, rpc

    d = await make_daemon(hub_home)
    try:
        d.hub.agents.create({"id": "a1", "name": "a1"})
        now = _iso(datetime.now(timezone.utc))
        d.hub.store.conn.execute("INSERT INTO runs (id, agent_id, kind, status, queued_at) VALUES (?,?,?,?,?)", (RID, "a1", "adhoc", "completed", now))
        _plant_logs(hub_home)
        plan = await rpc(d, "support.plan", {"since": "24h", "runs": [RID]})
        assert plan["dry_run"] and any(f["path"] == "meta/status.json" for f in plan["files"]) and not SB.list_bundles(hub_home)
        with pytest.raises(HubError) as ei:
            await rpc(d, "support.create", {"runs": [RID], "include_raw": True})
        assert ei.value.code == -32602 and not SB.list_bundles(hub_home)
        r = await rpc(d, "support.create", {"runs": [RID], "include_raw": True, "confirm_raw": True, "_audit": {"channel": "cli"}})
        assert r["include_raw"] and r["name"] and Path(r["path"]).exists()
        mem = _members(Path(r["path"]))
        st = json.loads(mem["meta/status.json"])
        assert st.get("version") and "running" in st  # hub.status thật của daemon
        rows = d.hub.store.conn.execute("SELECT outcome FROM audit_log WHERE kind='support.create' ORDER BY id").fetchall()
        assert [x[0] for x in rows] == ["error", "ok"]
        assert (await rpc(d, "support.list"))["bundles"][0]["name"] == r["name"]
    finally:
        await close(d)


async def test_agent_forbidden_rpc_cli_and_sandbox(hub_home):
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore
    from cfharness.guard.runtime import sandbox_settings, secret_read_deny_rules

    for m in ("support.plan", "support.create", "support.list"):
        assert m in auth.HUMAN_ONLY and m not in auth.MANAGER_ALLOWED, m
    assert set(SB.MUTATING_SUPPORT) <= auth.all_mutating()
    d = Daemon(hub_home, runtime=FakeRuntime())
    agent = auth.Caller(via="socket", token_ok=True, run_id="r_x", agent_id="coder", evidence=["env"])
    for m in ("support.plan", "support.create", "support.list"):
        resp = await d.call_as(agent, m, {})
        assert resp.get("error", {}).get("code") == -32003, (m, resp)
    assert not SB.list_bundles(hub_home)
    home = hub_home.home
    ps = PolicyStore(home)
    ps.ensure()
    ctx = GuardContext(home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
    bundle = f"{home}/exports/support-20261118T101010.tar.gz"
    for tool, inp in (("Bash", {"command": "cfharness support bundle --yes"}), ("Bash", {"command": "cfharness support list"}),
                      ("Bash", {"command": f"cat {bundle}"}), ("Bash", {"command": f"cp {bundle} /tmp/x.tgz"}),
                      ("Bash", {"command": f"tar xzf {bundle}"}), ("Bash", {"command": f"cat {home}/backups/x.tar.gz"}),
                      ("Read", {"file_path": bundle}), ("Bash", {"command": f"cat {home}/logs/filehub-sync.log"})):
        assert classify(tool, inp, ctx).forbidden, inp
    fs = sandbox_settings(home, hub_home.socket, {"enabled": True})["filesystem"]
    assert any(str(x).rstrip("/*").endswith("/exports") for x in fs["denyRead"]), fs["denyRead"]
    assert any("exports" in r and r.startswith("Read(") for r in secret_read_deny_rules(home, hub_home.socket))


def test_cli_offline_bundle_confirms_and_raw_step2(hub, hub_home, monkeypatch, capsys):
    from cfharness.cli import main

    _plant_logs(hub_home)
    hub_home.runs_dir.mkdir(parents=True, exist_ok=True)
    (hub_home.runs_dir / f"{RID}.jsonl.gz").write_bytes(gzip.compress(b'{"t":"RAW-CLI-MARK"}\n'))
    (hub_home.home / "secrets").mkdir(exist_ok=True)
    (hub_home.home / "secrets" / "OFFLINE_X").write_text("OfflineStoreSecret-4411aa")  # chỉ có trong secret store, chưa đăng ký
    with (hub_home.logs_dir / "hub.log").open("a") as f:
        f.write(_line(datetime.now(timezone.utc), "leak OfflineStoreSecret-4411aa"))
    h = ["--home", str(hub_home.home)]
    assert main(["support", "bundle", *h]) == 1  # không TTY, không --yes → không tạo
    assert not SB.list_bundles(hub_home)
    assert main(["support", "bundle", "--yes", "--run", RID, "--include-raw", *h]) == 1  # raw: --yes KHÔNG bỏ qua bước 2
    assert not SB.list_bundles(hub_home)
    assert main(["support", "bundle", "--yes", "--since", "2h", *h]) == 0
    out = capsys.readouterr().out
    assert "logs/hub.log" in out and "đã tạo gói chẩn đoán" in out
    time.sleep(1.05)  # tên gói theo giây
    assert main(["support", "bundle", "--yes", "--run", RID, "--include-raw", "--confirm-raw", *h]) == 0
    b = SB.list_bundles(hub_home)
    assert len(b) == 2 and b"RAW-CLI-MARK" in _members(SB.bundle_path(hub_home, b[0]["name"]))[f"runs/{RID}/raw.jsonl"]
    for x in b:  # ngoại tuyến vẫn che theo GIÁ TRỊ secret store
        assert all(b"OfflineStoreSecret-4411aa" not in v for v in _members(SB.bundle_path(hub_home, x["name"])).values())
    assert main(["support", "list", *h]) == 0
    monkeypatch.setenv(auth.ENV_RUN_ID, "r_agent")  # trong run của agent: làm tại chỗ bị từ chối
    assert main(["support", "bundle", "--yes", *h]) == 3


# ============================================================================ web
async def test_web_card_plan_create_and_single_use_download(hub_home):
    from test_web import ISOLATED, _start, _stop

    from cfharness.web.transfer_pages import mint_download

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        c = env.client
        assert (await c.get("/partials/support")).status_code == 401
        await env.login()
        _plant_logs(hub_home)
        assert 'hx-get="/partials/support"' in (await c.get("/settings")).text
        card = await c.get("/partials/support")
        assert card.status_code == 200 and "Gói chẩn đoán" in card.text
        page = await c.get("/support?since=24h")
        assert page.status_code == 200 and "logs/hub.log" in page.text and "/do/support_create" in page.text
        assert SECRET not in page.text
        r = await env.post("/do/support_create", {"since": "24h", "runs": "", "next": "/settings#support"})
        assert r.status_code == 200 and "Tạo gói chẩn đoán?" in r.text and not SB.list_bundles(hub_home)  # bước xác nhận
        r = await env.post("/do/support_create", {"since": "24h", "runs": "", "confirm": "yes", "next": "/settings#support"})
        assert r.status_code == 303, r.text
        b = SB.list_bundles(hub_home)
        assert len(b) == 1
        row = env.hub.store.conn.execute("SELECT channel, outcome FROM audit_log WHERE kind='support.create'").fetchone()
        assert tuple(row) == ("web", "ok")
        # raw: confirm=yes chưa đủ — bước 2 bắt buộc
        r = await env.post("/do/support_create_raw", {"since": "24h", "runs": RID, "confirm": "yes", "next": "/settings#support"})
        assert r.status_code == 200 and "RAW TRANSCRIPT" in r.text and len(SB.list_bundles(hub_home)) == 1
        # tải về: link MỘT lần, gắn phiên, kho token riêng
        name = b[0]["name"]
        assert name in (await c.get("/partials/support")).text
        r = await env.post("/support/download", {"name": name})
        assert r.status_code == 303
        url = r.headers["location"]
        assert url.startswith("/support/download/")
        got = await c.get(url)
        assert got.status_code == 200 and got.content == SB.bundle_path(hub_home, name).read_bytes()
        assert got.headers["cache-control"] == "no-store" and name in got.headers["content-disposition"]
        assert (await c.get(url)).status_code == 404  # dùng lại
        r = await env.post("/support/download", {"name": "../hub.db"})
        assert r.status_code == 303 and not r.headers["location"].startswith("/support/download/")
        assert (await c.post("/support/download", data={"name": name})).status_code == 403  # thiếu CSRF/khoá

        class _Req:  # token của kho "transfer" không mở được gói chẩn đoán
            app = env.app

        sess = next(iter(env.auth.sessions.values()))
        tok = mint_download(_Req(), sess, name)  # type: ignore[arg-type]
        assert (await c.get(f"/support/download/{tok}")).status_code == 404
    finally:
        await _stop(env)


# ============================================================================ phần L1 để lại
# (stderr của binary sync filehub ra file — bỏ: main v0.2.21 đã gỡ đồng bộ 2 chiều cũ, không còn tiến trình sync)


def test_update_helper_writes_json_lines(tmp_path):
    from cfharness import update_helper as H

    p = tmp_path / "update.log"
    with H.open_log(p) as log:
        log.write("\n=== cập nhật cfharness 0.1 → v0.2 ===\n")
        log.write("$ uv tool install\nInstalled 1 executable\n[mã thoát 0]\n")
        log.write("daemon KHÔNG trả lời hub.ping sau 60 s\n")
        log.write("dòng cuối không xuống dòng")
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert [r["msg"] for r in rows] == ["=== cập nhật cfharness 0.1 → v0.2 ===", "$ uv tool install", "Installed 1 executable", "[mã thoát 0]",
                                        "daemon KHÔNG trả lời hub.ping sau 60 s", "dòng cuối không xuống dòng"]
    for r in rows:
        assert set(r) == {"ts", "level", "logger", "msg", "proc", "pid"} and r["proc"] == "helper" and r["pid"] == os.getpid()
        assert r["logger"] == "cfharness.update_helper" and r["ts"].endswith("Z")
        datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
    assert rows[4]["level"] == "error" and rows[2]["level"] == "info"
    if sys.platform != "win32":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    # log_query đọc được (cùng schema) + đuôi trên web/CLI hiện chữ người đọc
    from cfharness.updater import Updater

    up = Updater.__new__(Updater)
    up.paths = HubPaths(tmp_path)  # type: ignore[attr-defined]
    (tmp_path / "logs").mkdir()
    os.replace(p, tmp_path / "logs" / "update.log")
    assert up.log_tail(2).splitlines() == ["daemon KHÔNG trả lời hub.ping sau 60 s", "dòng cuối không xuống dòng"]
    import re

    assert not re.search(r"^\s*(import|from)\s+(cfharness|\.)", Path(H.__file__).read_text(), re.M)  # helper chỉ stdlib


def test_import_job_stream_json():
    from cfharness.logs import setup_stream_json

    buf = io.StringIO()
    register_secret_values("ImportJobSecret-55aa66")
    h = setup_stream_json("import-job", stream=buf)
    try:
        logging.getLogger("cfharness.hub_transfer_web").info("nhập hub (web): done — ImportJobSecret-55aa66")
    finally:
        logging.getLogger("cfharness").removeHandler(h)
    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert rec["proc"] == "import-job" and rec["level"] == "info" and rec["logger"] == "cfharness.hub_transfer_web"
    assert "ImportJobSecret" not in rec["msg"] and rec["pid"] == os.getpid() and {"ts", "msg", "src"} <= set(rec)


def test_import_job_cli_logs_json(hub_home, monkeypatch, capfd):
    from cfharness import hub_transfer_web as W
    from cfharness.cli import main

    monkeypatch.setattr(W, "run_import_job", lambda paths, uid, supervised=False: {"state": "failed", "error": "gói hỏng"})
    try:
        assert main(["hub", "import-job", "up-0000000000000000", "--home", str(hub_home.home)]) == 1
    finally:
        lg = logging.getLogger("cfharness")
        for x in [x for x in lg.handlers if getattr(x, "_cfharness_stream_json", False)]:
            lg.removeHandler(x)
    err = capfd.readouterr().err
    rows = [json.loads(x) for x in err.splitlines() if x.startswith("{")]
    assert [r["level"] for r in rows] == ["info", "error"] and all(r["proc"] == "import-job" for r in rows)
    assert "gói hỏng" in rows[-1]["msg"]


def test_plan_text_lists_files():
    from cfharness.support_cli import plan_text

    t = plan_text({"since": "x", "files": [{"path": "logs/hub.log", "size": 10, "truncated": True}], "skipped": [{"path": "logs/a.jsonl", "size": 5}],
                   "redaction_report": {"total": 2, "files": {"logs/hub.log": 2}}, "total": 10, "max_bytes": 100, "never_included": ["hub.db"]})
    assert "logs/hub.log" in t and "che 2 chỗ" in t and "BỎ logs/a.jsonl" in t and "hub.db" in t
