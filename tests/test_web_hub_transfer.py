"""Trang web "Chuyển hub sang máy khác" (`/hub-transfer`, web/transfer_pages.py + hub_transfer_web.py) — PR6 của plan-hub-clone.md §7b.

Daemon IN-PROCESS trên socket thật + app Starlette qua ASGITransport (mẫu tests/test_web.py). Không gọi claude, không mạng. Tiến trình
`hub import-job` KHÔNG spawn thật: `hub_transfer_web._spawn` bị thay (ghi lại argv/env), rồi test gọi `run_import_job` trực tiếp sau khi
daemon đã dừng (khởi động lại daemon được mock) — rồi dựng daemon mới trên cùng HUB_HOME để thấy trang của phiên web CŨ hiện báo cáo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import pytest

from cfharness import auth
from cfharness import hub_transfer as T
from cfharness import hub_transfer_web as W
from cfharness import transfer_secrets as S
from cfharness.daemon import Daemon
from cfharness.runtime import FakeRuntime

from test_hub_transfer import make_source
from test_web import BASE, ISOLATED, _start, _stop, until

WIN = sys.platform == "win32"
PW = "web-transfer-passphrase-9000"
SECRET = "sk-WEB-TRANSFER-SECRET-1234"


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(S, "SCRYPT_N", 2**14)


@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


def _hdr(env, **extra) -> dict:
    return {"x-csrf-token": env.csrf, "x-ahub-key": env.key, "content-type": "application/octet-stream", **extra}


def _imports(env) -> list[str]:
    d = env.paths.home / "imports"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _all_text(env) -> str:
    c = env.hub.store.conn
    return "\n".join(c.iterdump())


# ============================================================================ chặn / bảo mật bề mặt
async def test_requires_login_csrf_key_and_same_origin(env):
    c = env.client
    r = await c.get("/hub-transfer")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await c.get("/partials/hub-transfer")).status_code == 401
    assert (await c.post("/hub-transfer/upload", content=b"x")).status_code == 401
    assert (await c.get("/hub-transfer/download/abc")).status_code == 303  # chưa đăng nhập → /login, không lộ gì
    await env.login()
    assert (await c.get("/hub-transfer")).status_code == 200
    body = b"x" * 10
    assert (await c.post("/hub-transfer/upload", content=body, headers={"x-ahub-key": env.key})).status_code == 403  # thiếu CSRF
    assert (await c.post("/hub-transfer/upload", content=body, headers={"x-csrf-token": env.csrf})).status_code == 403  # thiếu khoá phiên
    r = await c.post("/hub-transfer/upload", content=body, headers=_hdr(env, origin="http://evil.example"))
    assert r.status_code == 403
    r = await c.post("/hub-transfer/upload", content=body, headers=_hdr(env, **{"sec-fetch-site": "cross-site"}))
    assert r.status_code == 403
    # thao tác ghi qua /do/ cũng cần CSRF + khoá
    assert (await c.post("/do/hub_export", data={"profile": "core", "confirm": "yes", "confirm2": "yes"})).status_code == 403
    assert (await c.post("/do/hub_export", data={"csrf": env.csrf, "profile": "core", "confirm": "yes", "confirm2": "yes"})).status_code == 403
    assert not _imports(env) and env.hub.transfer_web.job is None


async def test_upload_limits_413_411_and_no_partial_file(env):
    await env.login()
    env.hub.config.set("transfer.max_upload_mb", 1)
    c = env.client
    big = 1024 * 1024 + 1
    r = await c.post("/hub-transfer/upload", content=b"x" * big, headers=_hdr(env))
    assert r.status_code == 413 and "transfer.max_upload_mb" in r.json()["error"]
    assert not [n for n in _imports(env) if n.endswith((".cfhx", ".part"))]

    async def gen():
        yield b"abc"

    r = await c.post("/hub-transfer/upload", content=gen(), headers=_hdr(env))  # chunked, không Content-Length → 411
    assert r.status_code == 411
    assert not [n for n in _imports(env) if n.endswith((".cfhx", ".part"))]
    r = await c.post("/hub-transfer/upload", content=b"hello", headers=_hdr(env))
    assert r.status_code == 200
    uid = r.json()["upload"]
    p = env.paths.home / "imports" / f"{uid}.cfhx"
    assert W.UPLOAD_RE.match(uid) and p.read_bytes() == b"hello"
    if not WIN:
        assert p.stat().st_mode & 0o777 == 0o600 and (env.paths.home / "imports").stat().st_mode & 0o777 == 0o700
    # gói rác → trang kế hoạch báo lỗi (không 500), nút xoá gói tải lên chạy
    r = await c.get(f"/hub-transfer/import/{uid}")
    assert r.status_code in (200, 303)
    r = await env.post(f"/hub-transfer/import/{uid}/discard", {})
    assert r.status_code == 303 and not p.exists()
    assert (await c.get("/hub-transfer/import/up-zz")).status_code == 404


# ============================================================================ xuất
async def test_export_two_steps_passphrase_never_leaks_one_time_download(env, caplog):
    caplog.set_level(logging.DEBUG)
    await env.login()
    env.hub.secrets.set("API_KEY", SECRET)
    c = env.client
    html_all: list[str] = []
    page = (await c.get("/hub-transfer")).text
    html_all.append(page)
    assert 'name="has_secrets" value="1"' in page and 'name="passphrase"' not in page  # passphrase KHÔNG ở form đầu
    form = {"profile": "core", "for": "clone", "secrets": "1", "has_secrets": "1", "next": "/hub-transfer"}
    r = await env.post("/do/hub_export", form)  # bước 1: cảnh báo nội dung gói
    assert r.status_code == 200 and "memory, transcript" in r.text and 'name="passphrase"' not in r.text
    html_all.append(r.text)
    assert env.hub.transfer_web.job is None
    r = await env.post("/do/hub_export", {**form, "confirm": "yes", "passphrase": PW, "passphrase2": PW})  # bước 2: chưa chạy
    assert r.status_code == 200 and "Xác nhận lần 2" in r.text
    assert re.search(r'<input type="password" name="passphrase" required', r.text) and PW not in r.text  # ô trống, không mang qua carry()
    html_all.append(r.text)
    assert env.hub.transfer_web.job is None
    r = await env.post("/do/hub_export", {**form, "confirm": "yes", "confirm2": "yes", "passphrase": PW, "passphrase2": PW + "x"})
    assert r.status_code == 303 and env.hub.transfer_web.job is None  # không khớp → không chạy
    r = await env.post("/do/hub_export", {**form, "confirm": "yes", "confirm2": "yes", "passphrase": "short", "passphrase2": "short"})
    assert r.status_code == 303 and env.hub.transfer_web.job is None
    r = await env.post("/do/hub_export", {**form, "confirm": "yes", "confirm2": "yes", "passphrase": PW, "passphrase2": PW})
    assert r.status_code == 303 and r.headers["location"] == "/hub-transfer"
    assert await until(lambda: (env.hub.transfer_web.job or {}).get("state") in ("done", "failed"), 30)
    job = env.hub.transfer_web.job
    assert job["state"] == "done", job
    res = job["result"]
    assert res["sealed"] and res["secret_names"] == 1 and "passphrase" not in json.dumps(job)
    pkg = env.paths.home / "exports" / res["file"]
    assert T.check_passphrase(pkg, PW) == ["API_KEY"]
    html_all.append((await c.get("/hub-transfer")).text)
    html_all.append((await c.get("/partials/hub-transfer")).text)
    assert "Tải về" in html_all[-1]
    # passphrase không ở đâu: HTML, log, audit/events/DB
    for h in html_all:
        assert PW not in h
    assert PW not in caplog.text
    assert PW not in _all_text(env)
    rows = [r["data"] for r in env.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='hub.export_start'").fetchall()]
    assert rows and all("passphrase" not in x for x in rows)
    assert env.hub.store.conn.execute("SELECT channel FROM audit_log WHERE kind='hub.export_start'").fetchone()[0] == "web"

    # tải về: link MỘT lần, gắn phiên, hết hạn
    r = await env.post("/hub-transfer/download", {})
    assert r.status_code == 303
    url = r.headers["location"]
    assert url.startswith("/hub-transfer/download/")
    from test_web import Env

    other = Env(env.d, env.task, env.rt, env.paths)
    other.app, other.auth = env.app, env.auth  # cùng tiến trình web, PHIÊN KHÁC
    import httpx2 as httpx

    other.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=BASE)
    try:
        await other.login()
        assert (await other.client.get(url)).status_code == 404  # phiên khác
    finally:
        await other.client.aclose()
    r = await c.get(url)
    assert r.status_code == 200 and r.content == pkg.read_bytes()
    assert r.headers["content-disposition"] == f'attachment; filename="{res["file"]}"'
    assert r.headers["cache-control"] == "no-store" and r.headers["content-type"] == "application/octet-stream"
    assert (await c.get(url)).status_code == 404  # dùng lại
    r = await env.post("/hub-transfer/download", {})
    url2 = r.headers["location"]
    tok = url2.rsplit("/", 1)[1]
    sid, eid, _exp = env.app.state.transfer_tokens[tok]
    env.app.state.transfer_tokens[tok] = (sid, eid, time.time() - 1)
    assert (await c.get(url2)).status_code == 404  # hết hạn
    assert (await c.get(f"/static/../exports/{res['file']}")).status_code in (400, 404)  # không bao giờ qua static


async def test_export_move_freezes_and_unfreeze_action_two_steps(env):
    await env.login()
    form = {"profile": "core", "for": "move", "has_secrets": "0", "next": "/hub-transfer"}
    r = await env.post("/do/hub_export", {**form, "confirm": "yes"})
    assert r.status_code == 200 and "ĐÓNG BĂNG" in r.text
    r = await env.post("/do/hub_export", {**form, "confirm": "yes", "confirm2": "yes"})
    assert r.status_code == 303
    assert await until(lambda: (env.hub.transfer_web.job or {}).get("state") in ("done", "failed"), 30)
    assert env.hub.transfer_web.job["state"] == "done" and env.hub.runs.frozen
    page = (await env.client.get("/hub-transfer")).text
    assert 'id="frozen"' in page and "Mở lại hub này" in page
    r = await env.post("/do/hub_unfreeze", {"export_id": env.hub.runs.frozen["export_id"], "next": "/hub-transfer"})
    assert r.status_code == 200 and env.hub.runs.frozen  # chưa xác nhận → chưa gỡ
    r = await env.post("/do/hub_unfreeze", {"export_id": env.hub.runs.frozen["export_id"], "confirm": "yes"})
    assert r.status_code == 303 and not env.hub.runs.frozen
    assert env.hub.store.conn.execute("SELECT channel FROM audit_log WHERE kind='hub.unfreeze'").fetchone()[0] == "web"


# ============================================================================ nhập
def _source_pkg(tmp_path, monkeypatch) -> Path:
    src, _proj = make_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])
    assert T.seal_package(pkg, src.home, PW)["sealed"]
    return pkg


async def _upload(env, pkg: Path) -> str:
    r = await env.client.post("/hub-transfer/upload", content=pkg.read_bytes(), headers=_hdr(env))
    assert r.status_code == 200, r.text
    assert r.json()["next"] == f"/hub-transfer/import/{r.json()['upload']}"
    return r.json()["upload"]


async def test_import_end_to_end_plan_step2_job_restart_session_survives(env, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    pkg = _source_pkg(tmp_path, monkeypatch)
    pkg_id = T.read_manifest(pkg)["id"]
    monkeypatch.setenv("HOME", str(tmp_path / "userB"))
    (tmp_path / "userB").mkdir()
    await env.login()
    c = env.client
    html: list[str] = []
    page = (await c.get("/manager")).text
    assert 'id="fresh-import"' in page  # hub trống → thẻ nổi bật ở trang Trò chuyện (chưa có điều phối viên)
    uid = await _upload(env, pkg)
    r = await c.get(f"/hub-transfer/import/{uid}")
    assert r.status_code == 200 and pkg_id in r.text and "cần passphrase" in r.text and "Bản đồ đường dẫn" in r.text
    assert 'name="replace"' not in r.text  # hub trống: không phải tick "thay thế"
    html.append(r.text)
    r = await env.post(f"/hub-transfer/import/{uid}", {"mode": "clone", "passphrase": PW})
    assert "passphrase đúng" in r.text
    html.append(r.text)
    r = await env.post(f"/hub-transfer/import/{uid}", {"mode": "clone", "passphrase": "sai-sai-sai-sai"})
    assert "passphrase SAI" in r.text and "Không nhập được" in r.text
    html.append(r.text)

    spawned: list = []
    monkeypatch.setattr(W, "_spawn", lambda argv, envv, log: spawned.append((argv, envv)) or 4242)
    form = {"upload": uid, "pkg_id": pkg_id, "mode": "clone", "has_secrets": "1", "next": f"/hub-transfer/import/{uid}"}
    r = await env.post("/do/hub_import", form)  # bước 2 bắt buộc: gõ lại id + passphrase
    assert r.status_code == 200 and 'name="confirm_id"' in r.text and 'type="password" name="passphrase"' in r.text
    html.append(r.text)
    jd = env.paths.home / "imports" / uid
    assert not (jd / "job.json").exists() and not spawned
    r = await env.post("/do/hub_import", {**form, "confirm": "yes", "confirm2": "yes", "confirm_id": "sai-id", "passphrase": PW})
    assert r.status_code == 303 and not (jd / "job.json").exists() and not spawned  # id không khớp → không làm gì
    r = await env.post("/do/hub_import", {**form, "confirm": "yes", "confirm2": "yes", "confirm_id": pkg_id, "passphrase": "sai-sai-sai-sai"})
    assert r.status_code == 303 and not (jd / "job.json").exists() and not spawned  # sai passphrase → daemon vẫn chạy
    r = await env.post("/do/hub_import", {**form, "confirm": "yes", "confirm2": "yes", "confirm_id": pkg_id, "passphrase": PW})
    assert r.status_code == 303 and r.headers["location"] == f"/hub-transfer?import={uid}"
    assert len(spawned) == 1
    argv, envv = spawned[0]
    assert argv[1:5] == ["-m", "cfharness", "hub", "import-job"] and argv[5] == uid and argv[0] == sys.executable
    job = json.loads((jd / "job.json").read_text())
    assert (jd / "pass").read_text() == PW
    if not WIN:
        assert (jd / "pass").stat().st_mode & 0o777 == 0o600 and (jd / "job.json").stat().st_mode & 0o777 == 0o600
    assert PW not in json.dumps(job) and PW not in " ".join(argv) and PW not in json.dumps(envv)
    assert auth.ENV_RUN_ID not in envv and envv["CFHARNESS_HOME"] == str(env.paths.home)
    # daemon tự dừng sau khi trả lời
    await asyncio.wait_for(env.task, 20)
    r = await c.get(f"/partials/hub-transfer?import={uid}")
    assert r.status_code == 200 and "daemon đang dừng" in r.text and "every 2s" in r.text

    restarted: list = []
    st = await asyncio.to_thread(W.run_import_job, env.paths, uid, restart=lambda p, s: restarted.append(s) or 5151)
    assert st["state"] == "done", st
    assert restarted == [False] and st["restarted"] == 5151
    assert not (jd / "pass").exists() and not (env.paths.home / "imports" / f"{uid}.cfhx").exists()
    raw_state = (jd / "state.json").read_text()
    assert PW not in raw_state and st["report"]["imported"] == pkg_id and st["report"]["mode"] == "clone"
    assert st["report"]["secrets"]["imported"]

    # daemon mới trên cùng HUB_HOME — phiên web CŨ vẫn dùng được (auth/ không bị gói ghi đè)
    d2 = Daemon(env.paths, runtime=FakeRuntime(ISOLATED))
    t2 = asyncio.create_task(d2.serve())
    try:
        for _ in range(400):  # chờ daemon mới trả lời (trang vẫn 200 khi daemon dừng — đọc nội dung partial)
            if "daemon đang dừng" not in (await c.get(f"/partials/hub-transfer?import={uid}")).text:
                break
            await asyncio.sleep(0.05)
        assert {a["id"] for a in d2.hub.agents.list()} >= {"a1", "w1"}
        r = await c.get(f"/partials/hub-transfer?import={uid}")
        assert r.status_code == 200 and "đã nhập" in r.text and pkg_id in r.text and "every 2s" not in r.text
        html.append(r.text)
        r = await c.get("/partials/hub-transfer/doctor")
        assert r.status_code == 200 and "Kiểm tra hub" in r.text
        page = (await c.get(f"/hub-transfer?import={uid}")).text
        assert 'id="imported-from"' in page and pkg_id in page
        html.append(page)
        assert 'id="fresh-import"' not in (await c.get("/manager")).text
        audit = [dict(x) for x in d2.hub.store.conn.execute("SELECT kind, channel, data FROM audit_log WHERE kind LIKE 'hub.import%'").fetchall()]
        assert any(a["kind"] == "hub.imported" for a in audit)
        assert PW not in "\n".join(d2.hub.store.conn.iterdump())
    finally:
        d2.stopping.set()
        await asyncio.wait_for(t2, 20)
    for h in html:
        assert PW not in h
    assert PW not in caplog.text
    assert PW not in (env.paths.home / "logs").joinpath("cfharness.log").read_text() if (env.paths.home / "logs" / "cfharness.log").exists() else True


async def test_import_start_refusals_replace_and_running_runs(env, tmp_path, monkeypatch):
    from m7_fake import rpc

    pkg = _source_pkg(tmp_path, monkeypatch)
    pkg_id = T.read_manifest(pkg)["id"]
    monkeypatch.setenv("HOME", str(tmp_path / "userB"))
    await env.login()
    uid = await _upload(env, pkg)
    monkeypatch.setattr(W, "_spawn", lambda *a: pytest.fail("không được spawn"))
    env.hub.agents.create({"id": "mine", "name": "Mine", "model": "haiku"})  # hub KHÔNG còn trống
    base = {"upload": uid, "confirm_id": pkg_id, "passphrase": PW, "mode": "clone"}
    with pytest.raises(Exception, match="thay thế"):
        await rpc(env.d, "hub.import_start", base)
    plan = (await env.client.get(f"/hub-transfer/import/{uid}")).text
    assert 'name="replace" value="1" required' in plan  # hub có dữ liệu → bắt buộc tick thay thế
    with pytest.raises(Exception, match="passphrase"):
        await rpc(env.d, "hub.import_start", {**base, "replace": True, "passphrase": ""})
    with pytest.raises(Exception, match="không khớp"):
        await rpc(env.d, "hub.import_start", {**base, "replace": True, "confirm_id": "x"})
    env.hub.runs.active["r_fake"] = object()  # type: ignore[index]
    try:
        with pytest.raises(Exception, match="run đang chạy"):
            await rpc(env.d, "hub.import_start", {**base, "replace": True})
    finally:
        env.hub.runs.active.pop("r_fake", None)
    assert not (env.paths.home / "imports" / uid / "job.json").exists() and not env.d.stopping.is_set()


async def test_new_rpcs_human_only_not_manager_and_mutating_audited(env):
    from m7_fake import rpc

    for m in W.HUMAN_ONLY_TRANSFER_WEB:
        assert m in auth.HUMAN_ONLY, m
        assert m not in auth.MANAGER_ALLOWED, m
    assert set(W.MUTATING_TRANSFER_WEB) <= auth.all_mutating()
    agent = auth.Caller(via="socket", pid=99, token_ok=True, run_id="r_x", agent_id="w1", evidence=["env_run_id"])
    for m, p in (("hub.export_start", {"profile": "core", "passphrase": PW}), ("hub.import_start", {"upload": "up-0000000000000000"}),
                 ("hub.import_plan", {"upload": "up-0000000000000000"}), ("hub.transfer_status", {}), ("hub.export_status", {}),
                 ("hub.import_status", {"upload": "up-0000000000000000"})):
        with pytest.raises(Exception) as ei:
            await rpc(env.d, m, p, as_caller=agent)
        assert getattr(ei.value, "code", None) == -32003, (m, ei.value)
    assert env.hub.transfer_web.job is None


# ============================================================================ tiến trình job + daemon chờ
def _write_job(paths, uid: str, *, pw: str | None = PW) -> Path:
    d = W.job_dir(paths, uid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(json.dumps({"upload": uid, "mode": "clone", "maps": []}))
    if pw:
        (d / "pass").write_text(pw)
    return d


def test_run_import_job_waits_for_daemon_lock_fails_safely_and_still_restarts(hub_home):
    import threading

    from cfharness.daemon import acquire_instance_lock, release_instance_lock

    uid = "up-00000000000000aa"
    (hub_home.home / "imports").mkdir(exist_ok=True)
    W.upload_path(hub_home, uid).write_bytes(b"not a package")
    d = _write_job(hub_home, uid)
    lock = acquire_instance_lock(hub_home)
    seen: list = []
    threading.Timer(0.6, lambda: (seen.append(W.read_state(hub_home, uid)), release_instance_lock(lock))).start()
    st = W.run_import_job(hub_home, uid, restart=lambda p, s: 777, wait_s=10)
    assert seen and seen[0]["phase"] == "waiting_daemon" and seen[0]["job_pid"] == os.getpid()
    assert not (d / "pass").exists()  # đọc rồi xoá NGAY
    assert st["state"] == "failed" and st["restarted"] == 777 and st["error"] and PW not in json.dumps(st)
    assert W.upload_path(hub_home, uid).exists()  # lỗi → giữ gói để thử lại


def test_run_import_job_restart_failure_recorded(hub_home):
    uid = "up-00000000000000ab"
    (hub_home.home / "imports").mkdir(exist_ok=True)
    _write_job(hub_home, uid, pw=None)

    def boom(p, s):
        raise RuntimeError("x")

    st = W.run_import_job(hub_home, uid, restart=boom, wait_s=1)
    assert st["state"] == "failed" and "cfharness daemon start" in st["error"] and st["restarted"] is None


def test_import_busy_hold_and_cleanup(hub_home, monkeypatch):
    uid, dead, old = "up-0000000000000001", "up-0000000000000002", "up-0000000000000003"
    (hub_home.home / "imports").mkdir(exist_ok=True)
    _write_job(hub_home, uid)
    W.write_state(hub_home, uid, state="running", phase="importing", job_pid=os.getpid())
    assert W.import_busy(hub_home)
    _write_job(hub_home, dead)
    W.write_state(hub_home, dead, state="running", phase="importing", job_pid=99999999)
    W.upload_path(hub_home, old).write_bytes(b"x")
    os.utime(W.upload_path(hub_home, old), (time.time() - 2 * 86400,) * 2)
    part = hub_home.home / "imports" / ".up-0000000000000004.cfhx.part"
    part.write_bytes(b"x")
    os.utime(part, (time.time() - 7200,) * 2)
    n = W.cleanup(hub_home)
    assert (W.job_dir(hub_home, uid) / "pass").exists()  # job đang sống: không đụng
    assert not (W.job_dir(hub_home, dead) / "pass").exists() and W.read_state(hub_home, dead)["state"] == "failed"
    assert not W.upload_path(hub_home, old).exists() and not part.exists()
    assert n["pass"] == 1 and n["dead"] == 1 and n["uploads"] == 2
    # daemon khởi động trong lúc job còn bận → chờ (không lấy daemon.lock chen ngang)
    calls = {"n": 0}

    def fake_busy(p):
        calls["n"] += 1
        return calls["n"] < 3

    monkeypatch.setattr(W, "import_busy", fake_busy)
    assert W.hold_while_importing(hub_home, poll_s=0.01, max_s=5) is True and calls["n"] == 3
    W.write_state(hub_home, uid, state="done", phase="finished")
    monkeypatch.undo()
    assert not W.import_busy(hub_home)


async def test_settings_link_and_3d_free_plain_page(env):
    await env.login()
    s = (await env.client.get("/settings/backup")).text
    assert 'href="/hub-transfer"' in s
    page = (await env.client.get("/hub-transfer")).text
    assert 'id="hub-upload"' in page and "transfer.js" in page and "<script>" not in page  # không script inline (CSP)
