"""Cập nhật cfharness (updater.py, update_helper.py, update_cli.py, web/update_pages.py).

Không gọi mạng/GitHub, không chạy `uv` thật: `Updater.fetch/info/spawn/_uv` được thay; tiến trình phụ chạy với lệnh python giả.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

from cfharness import __version__, auth
from cfharness import update_helper as helper
from cfharness.errors import BLOCKED, Conflict, HubError
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore
from cfharness.updater import Updater, install_info, parse_version

CUR = parse_version(__version__)
NEWER = f"v{CUR[0]}.{CUR[1]}.{CUR[2] + 1}"
OLDER = "v0.0.1"


def _uv_git() -> dict:
    return {"kind": "uv_git", "prefix": "/x", "source": "https://github.com/lephuong123/agent-hub@v0.1.0"}


def make_updater(paths, *, tag: str = NEWER, info=_uv_git, uv: str | None = "/usr/bin/uv", fail_fetch: bool = False):
    spawned: list = []

    def fetch(timeout: float) -> dict:
        if fail_fetch:
            raise OSError("mạng hỏng")
        return {"tag": tag, "name": tag, "published_at": "2026-10-30T00:00:00Z", "url": f"https://github.com/x/releases/tag/{tag}"}

    def spawn(argv, opts):
        spawned.append((argv, opts))
        return 4242

    up = Updater(paths, fetch=fetch, spawn=spawn, info=info, uv=uv, python="/venv/bin/python", base_python="/base/python3")
    return up, spawned


def patch_updater(up: Updater, **kw) -> list:
    """Thay ruột `hub.updater` của daemon (closure RPC giữ đúng instance)."""
    new, spawned = make_updater(up.paths, **kw)
    up.fetch, up.spawn, up.info, up._uv, up.python, up.base_python, up._cache = new.fetch, new.spawn, new.info, new._uv, new.python, new.base_python, None
    return spawned


# ------------------------------------------------------------------ thuần
def test_parse_version_and_install_info(tmp_path):
    assert parse_version("v1.2.3") == (1, 2, 3) and parse_version("0.2.0") == (0, 2, 0)
    assert parse_version("v1.2") is None and parse_version("main") is None and parse_version("v1.2.3; rm -rf /") is None
    assert install_info(tmp_path)["kind"] == "source"
    (tmp_path / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "cfharness", editable = "/src/agent-hub" }]\n')
    i = install_info(tmp_path)
    assert i["kind"] == "editable" and "git pull" in i["reason"]
    (tmp_path / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "cfharness", git = "https://github.com/lephuong123/agent-hub?tag=v0.2.0" }]\n')
    assert install_info(tmp_path)["kind"] == "uv_git"


def test_check_reports_available_and_reasons(hub_home):
    up, _ = make_updater(hub_home)
    c = up.check()
    assert c["current"] == __version__ and c["update_available"] and c["can_update"] and c["latest"]["tag"] == NEWER
    up, _ = make_updater(hub_home, tag=OLDER)
    assert not up.check()["update_available"]
    up, _ = make_updater(hub_home, info=lambda: {"kind": "editable", "reason": "cài dạng editable"})
    c = up.check()
    assert not c["can_update"] and "editable" in c["reason"]
    up, _ = make_updater(hub_home, uv=None)
    assert "uv" in up.check()["reason"]
    up, _ = make_updater(hub_home, fail_fetch=True)
    c = up.check()
    assert not c["latest"]["ok"] and "mạng hỏng" in c["latest"]["error"] and not c["update_available"]


def test_start_validates_then_spawns_helper_outside_venv(hub_home):
    up, spawned = make_updater(hub_home, tag=OLDER)
    with pytest.raises(Conflict, match="không có bản mới hơn"):
        up.start()
    up, spawned = make_updater(hub_home)
    with pytest.raises(HubError, match="không hợp lệ"):
        up.start(tag="main; rm -rf ~")
    with pytest.raises(Conflict, match="2 run"):
        up.start(busy=2)
    assert not spawned
    up, _ = make_updater(hub_home, info=lambda: {"kind": "editable", "reason": "cài dạng editable"})
    with pytest.raises(HubError, match="editable"):
        up.start()
    up, spawned = make_updater(hub_home)
    r = up.start(busy=2, force=True, wait_pid=999)
    assert r["tag"] == NEWER and r["helper_pid"] == 4242
    argv, opts = spawned[0]
    assert argv[:3] == ["/base/python3", "-I", str(up.dir / "helper.py")] and (up.dir / "helper.py").read_text(encoding="utf-8") == Path(helper.__file__).read_text(encoding="utf-8")
    install = json.loads(argv[argv.index("--install") + 1])
    assert install[:4] == ["/usr/bin/uv", "tool", "install", "--force"] and install[-1] == f"git+https://github.com/lephuong123/agent-hub@{NEWER}"
    assert json.loads(argv[argv.index("--restart") + 1]) == ["/venv/bin/python", "-m", "cfharness", "daemon", "start"]
    assert argv[argv.index("--wait-pid") + 1] == "999"
    st = up.state()
    assert st["state"] == "starting" and st["tag"] == NEWER and st["version_before"] == __version__ and up.in_progress()
    with pytest.raises(Conflict, match="đang có một lần cập nhật"):
        up.start(force=True)


def _py(code: str) -> str:
    return json.dumps([sys.executable, "-c", code])


def test_helper_apply_done_and_failed(tmp_path):
    state, log = tmp_path / "state.json", tmp_path / "update.log"
    helper.write_state(state, {"state": "starting", "tag": "v9.9.9", "version_before": "0.2.0"})
    marker = tmp_path / "restarted"
    rc = helper.main(["apply", "--state", str(state), "--log", str(log), "--wait-pid", "0",
                      "--install", _py("print('Installed 1 executable: cfharness')"),
                      "--restart", _py(f"open({str(marker)!r}, 'w').write('1')"), "--verify", _py("print('cfharness 9.9.9')")])
    st = json.loads(state.read_text())
    assert rc == 0 and st["state"] == "done" and st["version_after"] == "9.9.9" and marker.exists()
    assert "Installed 1 executable" in log.read_text()
    # cài lỗi → vẫn khởi động lại daemon, trạng thái failed kèm dòng lỗi cuối
    marker.unlink()
    helper.write_state(state, {"state": "starting", "tag": "v9.9.9"})
    rc = helper.main(["apply", "--state", str(state), "--log", str(log), "--install", _py("import sys; print('error: no tag'); sys.exit(2)"),
                      "--restart", _py(f"open({str(marker)!r}, 'w').write('1')")])
    st = json.loads(state.read_text())
    assert rc == 1 and st["state"] == "failed" and "error: no tag" in st["error"] and marker.exists()


# ------------------------------------------------------------------ RPC + quyền
async def test_rpc_human_only_and_daemon_stops_after_start(hub_home):
    from m7_fake import close, make_daemon, rpc

    d = await make_daemon(hub_home)
    try:
        spawned = patch_updater(d.hub.updater)
        c = await rpc(d, "update.check", {})
        assert c["update_available"] and c["can_update"]
        agent = auth.Caller(via="socket", pid=99, token_ok=True, run_id="r_x", agent_id="w1", evidence=["env_run_id"])
        with pytest.raises(HubError) as ei:
            await rpc(d, "update.start", {}, as_caller=agent)
        assert ei.value.code == BLOCKED and not spawned
        r = await rpc(d, "update.start", {"_audit": {"channel": "web"}})
        assert r["started"] and spawned and spawned[0][1]["env"]["CFHARNESS_HOME"] == str(hub_home.home)
        import asyncio

        await asyncio.wait_for(d.stopping.wait(), 3)
        rows = d.hub.store.conn.execute("SELECT channel FROM audit_log WHERE kind='update.start'").fetchall()
        assert [x["channel"] for x in rows] == ["web"]
        s = await rpc(d, "update.status", {})
        assert s["in_progress"] and s["state"]["tag"] == NEWER
    finally:
        await close(d)


def test_classifier_update_cli_forbidden_for_agents(tmp_path):
    ctx = GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "h").load())
    for cmd in ("cfharness update", "cfharness update --yes --force", "uv run cfharness update --check"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd


# ------------------------------------------------------------------ web
async def test_web_update_card_button_confirm_and_audit(hub_home):
    from test_web import ISOLATED, _start, _stop, audit_rows

    from cfharness.runtime import FakeRuntime

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        spawned = patch_updater(env.hub.updater)
        await env.login()
        html = (await env.client.get("/settings")).text
        assert 'hx-get="/partials/update"' in html
        card = (await env.client.get("/partials/update")).text
        assert f"có bản mới {NEWER}" in card and 'action="/do/update_start"' in card and f"Cập nhật lên {NEWER}" in card
        # chưa xác nhận → trang xác nhận, chưa spawn
        r = await env.post("/do/update_start", {"tag": NEWER, "next": "/update"})
        assert r.status_code == 200 and "Daemon sẽ dừng" in r.text and not spawned
        r = await env.post("/do/update_start", {"tag": NEWER, "confirm": "yes", "next": "/update"})
        assert r.status_code == 303 and r.headers["location"].startswith("/update") and spawned
        assert audit_rows(env, "update.start")[-1]["channel"] == "web"
        # daemon đã dừng → thẻ báo đang khởi động lại + vẫn poll, trang /update vẫn mở được
        import asyncio

        await asyncio.wait_for(env.task, 20)
        card = (await env.client.get("/partials/update?full=1")).text
        assert "đang khởi động lại" in card and 'hx-trigger="every 3s"' in card
        assert (await env.client.get("/update")).status_code == 200
    finally:
        await _stop(env)


async def test_web_card_editable_install_has_no_button(hub_home):
    from test_web import ISOLATED, _start, _stop

    from cfharness.runtime import FakeRuntime

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        patch_updater(env.hub.updater, info=lambda: {"kind": "editable", "reason": "cài dạng editable từ mã nguồn /src — cập nhật bằng `git pull`"})
        await env.login()
        card = (await env.client.get("/partials/update")).text
        assert "Không cập nhật tự động được" in card and "/do/update_start" not in card
    finally:
        await _stop(env)


# ------------------------------------------------------------------ chạy dưới launchd (LaunchAgent/LaunchDaemon)
# Sự cố thật (hub lamlinh, LaunchDaemon): cập nhật dừng daemon (mã 0 → launchd không chạy lại) rồi helper tự bật daemon NGOÀI launchd.
def test_supervisor_detects_only_cfharness_launchd_jobs(monkeypatch):
    from cfharness import daemon as D

    monkeypatch.setattr(D, "_IS_MACOS", True)
    for v, want in (("com.cfharness.daemon.lamlinh", "launchd"), ("com.cfharness.daemon.a1b2c3", "launchd"),
                    ("application.com.apple.Terminal.123", None), ("0", None), ("com.cfharness.web.lamlinh", None)):
        monkeypatch.setenv("XPC_SERVICE_NAME", v)
        assert D.supervisor() == want, v
    monkeypatch.delenv("XPC_SERVICE_NAME")
    assert D.supervisor() is None
    monkeypatch.setattr(D, "_IS_MACOS", False)
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.cfharness.daemon.x")
    assert D.supervisor() is None


def test_plan_supervised_waits_for_launchd(hub_home):
    from cfharness.updater import SUPERVISOR_WAIT_S

    up, spawned = make_updater(hub_home)
    up.start(supervised=True)
    argv, _ = spawned[0]
    assert json.loads(argv[argv.index("--restart") + 1]) == ["/venv/bin/python", "-m", "cfharness", "daemon", "start",
                                                            "--wait-supervisor", str(SUPERVISOR_WAIT_S)]
    assert SUPERVISOR_WAIT_S < helper.RESTART_TIMEOUT_S


@pytest.mark.parametrize("label", ["com.cfharness.daemon.lamlinh", None])
async def test_rpc_update_under_launchd_exits_for_restart(hub_home, monkeypatch, label):
    from m7_fake import close, make_daemon, rpc

    from cfharness import daemon as D

    monkeypatch.setattr(D, "_IS_MACOS", True)
    if label:
        monkeypatch.setenv("XPC_SERVICE_NAME", label)
    else:
        monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    d = await make_daemon(hub_home)
    try:
        spawned = patch_updater(d.hub.updater)
        await rpc(d, "update.start", {})
        argv, opts = spawned[0]
        restart = json.loads(argv[argv.index("--restart") + 1])
        assert "XPC_SERVICE_NAME" not in opts["env"]  # daemon helper có thể tự bật không được tưởng mình là job launchd
        if label:
            assert d.exit_code == D.SUPERVISED_RESTART_CODE and "--wait-supervisor" in restart
        else:
            assert d.exit_code == 0 and "--wait-supervisor" not in restart
    finally:
        await close(d)


def test_daemon_holds_while_installing_then_exits_for_restart(tmp_path):
    """launchd chạy lại daemon ngay khi bản cũ thoát — lúc uv còn cài đè venv: bản đó chờ cài xong rồi thoát 75 (chạy lại bằng mã mới)."""
    import threading

    from cfharness import daemon as D
    from cfharness.paths import HubPaths

    hp = HubPaths(tmp_path / "hub")
    hp.ensure()
    D._hold_while_updating(hp)  # không có cập nhật → đi tiếp ngay
    state = hp.home / "update" / "state.json"
    state.parent.mkdir()
    helper.write_state(state, {"state": "running", "phase": "installing", "updated_at": time.time()})

    def done():
        time.sleep(0.4)
        helper.write_state(state, {"state": "running", "phase": "restarting", "updated_at": time.time()})

    threading.Thread(target=done).start()
    t0 = time.monotonic()
    with pytest.raises(SystemExit) as ei:
        D._hold_while_updating(hp, poll_s=0.05)
    assert ei.value.code == D.SUPERVISED_RESTART_CODE and time.monotonic() - t0 >= 0.3
    helper.write_state(state, {"state": "running", "phase": "installing", "updated_at": time.time() - 5000})  # helper treo → bỏ qua
    D._hold_while_updating(hp)


def test_wait_supervised_returns_pid_or_none(hub_home):
    from cfharness import daemon as D

    assert D.wait_supervised(hub_home, 0.3) is None
    D.spawn_background(hub_home)
    try:
        assert D.wait_supervised(hub_home, 5) > 0
    finally:
        from cfharness.rpc import RpcClient

        RpcClient(hub_home.socket, timeout=5).call("hub.shutdown")


def test_wait_supervised_retries_when_daemon_slow_to_answer(hub_home, monkeypatch):
    """Sự cố v0.2.27: daemon đang khởi động nhận kết nối nhưng chưa trả lời trong 2 s → TimeoutError lọt ra, `daemon start
    --wait-supervisor` thoát mã 1, helper ghi cập nhật "failed" dù daemon lên ngay sau đó. Giờ phải thử lại tới khi trả lời."""
    from cfharness import daemon as D
    from cfharness.rpc import RpcClient, RpcError

    errors = [TimeoutError("timed out"), RpcError(-32000, "daemon đóng kết nối không trả lời"), ValueError("json cụt")]

    def fake_call(self, method, params=None, *, timeout=None):
        if errors:
            raise errors.pop(0)
        return {"pid": 4242}

    monkeypatch.setattr(RpcClient, "call", fake_call)
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    assert D.wait_supervised(hub_home, 5) == 4242 and not errors


# Sự cố thật (Windows, lên v0.2.11): `cfharness web --open` chạy từ venv của tool → uv không xoá được Scripts\ ("Access is denied"),
# venv bị xoá dở, quay về cũng lỗi → hub chết hẳn. Helper giờ dừng tiến trình giữ venv trước khi cài và bật lại web sau đó.
VENV = str(Path("/u/tools/cfharness"))


def _table():
    sep = os.sep
    return [
        (10, 1, f"{VENV}{sep}Scripts{sep}python.exe {VENV}{sep}Scripts{sep}python.exe /home/me/.local/bin/cfharness web --open --port 8765"),
        (11, 10, f"{VENV.upper()}{sep}Scripts{sep}python.exe -m cfharness mcp serve"),  # khác hoa/thường (Windows) vẫn khớp
        (12, 1, f"{VENV}-other{sep}python cfharness web"),  # venv khác cùng tiền tố → không khớp
        (os.getpid(), 1, f"{VENV}{sep}python helper"),  # chính helper → bỏ
    ]


def test_venv_holders_match_only_tool_venv():
    pids = [p for p, _ in helper.venv_holders(VENV, _table())]
    assert 10 in pids and os.getpid() not in pids and 12 not in pids
    assert (11 in pids) == (os.path.normcase("A") == os.path.normcase("a"))
    assert helper.venv_holders("", _table()) == []


def test_web_restart_argv_drops_open():
    w = helper.web_restart_argv("/venv/bin/python /home/me/.local/bin/cfharness web --open --port 8765")
    assert w == ["/home/me/.local/bin/cfharness", "web", "--port", "8765"]
    assert helper.web_restart_argv("/venv/bin/python -m cfharness daemon run") is None


def test_stop_venv_holders_windows_only(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(helper, "_process_table", _table)
    monkeypatch.setattr(helper, "_kill", killed.append)
    monkeypatch.setattr(helper, "_wait_exit", lambda pid, t: True)
    log = io.StringIO()
    restarts: list = []
    monkeypatch.setattr(helper, "_windows", lambda: False)
    helper.stop_venv_holders(VENV, log, restarts)
    assert killed == [] and restarts == []  # macOS/Linux: uv thay venv được khi đang chạy → không dừng gì
    monkeypatch.setattr(helper, "_windows", lambda: True)
    helper.stop_venv_holders(VENV, log, restarts)
    helper.stop_venv_holders(VENV, log, restarts)  # gọi lại (bước thử lại) → không thêm web trùng
    assert 10 in killed and os.getpid() not in killed
    assert restarts == [["/home/me/.local/bin/cfharness", "web", "--port", "8765"]]


def test_install_retries_once_when_venv_locked(monkeypatch, tmp_path):
    calls: list = []
    stops: list = []
    outs = iter([(2, "error: failed to remove directory `C:/x/Scripts`: Access is denied. (os error 5)"), (0, "ok")])
    monkeypatch.setattr(helper, "_run", lambda argv, log, t, env: (calls.append(argv), next(outs))[1])
    monkeypatch.setattr(helper, "stop_venv_holders", lambda venv, log, restarts: stops.append(venv))
    a = argparse.Namespace(venv=VENV)
    assert helper.install_with_retry(a, ["uv", "tool", "install"], io.StringIO(), {}, [])[0] == 0
    assert len(calls) == 2 and stops == [VENV]
    # lỗi khác (không phải khoá file) → không thử lại
    calls.clear()
    stops.clear()
    monkeypatch.setattr(helper, "_run", lambda argv, log, t, env: (calls.append(argv), (2, "error: no tag"))[1])
    assert helper.install_with_retry(a, ["uv"], io.StringIO(), {}, [])[0] == 2
    assert len(calls) == 1 and stops == []


def test_updater_passes_tool_venv_to_helper(hub_home):
    up, _ = make_updater(hub_home)
    up.dir.mkdir(parents=True, exist_ok=True)
    argv = up._helper_argv("apply", up.plan(NEWER), install=["uv"], wait_pid=None, backup=None)
    assert argv[argv.index("--venv") + 1] == str(Path("/venv/bin/python").parent.parent)


# ---- sau cập nhật: web tự chạy lại bản mới, CLI không bảo người "tắt và mở lại" (người dùng host không làm được) ----

class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_web_after_update_no_web_running():
    from cfharness.update_cli import web_after_update

    c = _Clock()
    assert web_after_update("p", "0.2.17", read_state=lambda p: None, restart=lambda *a: pytest.fail("không được gọi"),
                            sleep=c.sleep, clock=c) == "none"


def test_web_after_update_waits_for_launchd_respawn():
    from cfharness.update_cli import web_after_update

    c, seen = _Clock(), []
    states = iter([{"version": "0.2.12"}, {"version": "0.2.12"}, {"version": "0.2.17"}])
    res = web_after_update("p", "v0.2.17", read_state=lambda p: next(states), restart=lambda *a: pytest.fail("không được gọi"),
                           sleep=c.sleep, clock=c, progress=seen.append)
    assert res == "ok" and len(seen) == 1  # báo chờ đúng một lần


def test_web_after_update_restarts_web_itself_when_still_old():
    from cfharness.update_cli import web_after_update

    c, calls = _Clock(), []
    res = web_after_update("p", "0.2.17", wait_s=10, read_state=lambda p: {"version": "0.2.10"},
                           restart=lambda p, v: calls.append(v) or "killed", sleep=c.sleep, clock=c)
    assert res == "restarted" and calls == ["0.2.17"]
    res = web_after_update("p", "0.2.17", wait_s=10, read_state=lambda p: {"version": "0.2.10"},
                           restart=lambda p, v: "failed", sleep=c.sleep, clock=c)
    assert res == "failed"

    def boom(p, v):
        raise OSError("x")

    assert web_after_update("p", "0.2.17", wait_s=0, read_state=lambda p: {}, restart=boom, sleep=c.sleep, clock=c) == "none"
    assert web_after_update("p", "0.2.17", wait_s=0, read_state=lambda p: {"pid": 1}, restart=boom, sleep=c.sleep, clock=c) == "failed"


def test_open_terminals_counts_only_interactive_cfharness():
    from cfharness.update_cli import open_terminals

    rows = ["  101 /Users/u/.local/share/uv/tools/cfharness/bin/python /Users/u/.local/bin/cfharness chat",
            "  102 /Users/u/.local/share/uv/tools/cfharness/bin/python -m cfharness top --home /x",
            "  103 /Users/u/.local/share/uv/tools/cfharness/bin/python /Users/u/.local/bin/cfharness web --port 8765",
            "  104 /Users/u/.local/share/uv/tools/cfharness/bin/python -m cfharness daemon run",
            "  105 vim chat.txt",
            f"  {os.getpid()} /x/python /x/cfharness chat"]
    assert open_terminals(rows) == 2


# Sự cố thật (macOS, sau `cfharness update`): daemon được bật lại NGOÀI launchd (không --home) → job LaunchAgent thua
# `daemon.lock`, thoát 1 và bị launchd chạy lại vô hạn (runs=6511). Sửa: đã cài LaunchAgent thì `kickstart -k` thay vì Popen;
# job launchd thua khoá thì chờ khoá, không thoát 1. Mọi lệnh launchctl/Popen đều giả.
def _fake_autostart(monkeypatch, tmp_path, *, installed: bool, rc: int = 0) -> list[list[str]]:
    from cfharness import daemon as D
    from cfharness.systems.autostart import Autostart

    plist = tmp_path / "agent.plist"
    if installed:
        plist.write_text("x", encoding="utf-8")
    calls: list[list[str]] = []

    def run(self, argv):
        calls.append(list(argv))
        return rc, "", "Could not find service" if rc else ""

    monkeypatch.setattr(D, "_IS_MACOS", True)
    monkeypatch.setattr(Autostart, "definition_path", lambda self: plist)
    monkeypatch.setattr(Autostart, "_run", run)
    monkeypatch.setattr(D, "_wait_ready", lambda paths, wait_s, proc: 4242 if proc is None else 777)
    return calls


def test_spawn_background_kickstarts_launchd_when_autostart_installed(hub_home, monkeypatch, tmp_path):
    from cfharness import daemon as D
    from cfharness.systems.autostart import Autostart

    calls = _fake_autostart(monkeypatch, tmp_path, installed=True)
    monkeypatch.setattr(D.subprocess, "Popen", lambda *a, **k: pytest.fail("không được tự bật daemon ngoài launchd"))
    assert D.spawn_background(hub_home) == 4242
    label = Autostart(hub_home).name
    assert calls == [["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]]


@pytest.mark.parametrize("installed,rc", [(False, 0), (True, 113)])  # chưa cài / job chưa nạp → vẫn tự bật như cũ
def test_spawn_background_falls_back_to_popen(hub_home, monkeypatch, tmp_path, installed, rc):
    from cfharness import daemon as D

    calls = _fake_autostart(monkeypatch, tmp_path, installed=installed, rc=rc)
    popened: list[list[str]] = []
    monkeypatch.setattr(D.subprocess, "Popen", lambda argv, **k: popened.append(argv) or object())
    assert D.spawn_background(hub_home) == 777
    assert len(popened) == 1 and popened[0][-2:] == ["daemon", "run"]
    assert len(calls) == (1 if installed else 0)


class _StopAfterLock(Exception):
    pass


@pytest.mark.parametrize("label", ["com.cfharness.daemon.x", None])
def test_run_foreground_lock_lost_under_launchd_waits(hub_home, monkeypatch, label):
    from cfharness import daemon as D
    from cfharness import oscompat

    monkeypatch.setattr(oscompat, "set_private_umask", lambda: None)
    monkeypatch.setattr(D, "_IS_MACOS", True)
    monkeypatch.setattr(D, "_INSTANCE_LOCK", None)
    if label:
        monkeypatch.setenv("XPC_SERVICE_NAME", label)
    else:
        monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    results = [None, None, "handle"]  # thua khoá 2 lần rồi bản ngoài chết → lấy được
    waits: list[float] = []

    def acquire(paths, wait_s=0.0):
        waits.append(wait_s)
        return results.pop(0)

    def stop(paths):
        raise _StopAfterLock

    monkeypatch.setattr(D, "acquire_instance_lock", acquire)
    monkeypatch.setattr(D, "_log_settings", stop)  # bước ngay sau khi giữ khoá: dừng test ở đây
    if label:
        with pytest.raises(_StopAfterLock):
            D.run_foreground(hub_home)
        assert waits == [3.0, D.LOCK_RETRY_S, D.LOCK_RETRY_S] and D._INSTANCE_LOCK == "handle"
    else:
        with pytest.raises(SystemExit) as ei:
            D.run_foreground(hub_home)
        assert ei.value.code == 1 and waits == [3.0]
