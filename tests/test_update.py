"""Cập nhật cfharness (updater.py, update_helper.py, update_cli.py, web/update_pages.py).

Không gọi mạng/GitHub, không chạy `uv` thật: `Updater.fetch/info/spawn/_uv` được thay; tiến trình phụ chạy với lệnh python giả.
"""

from __future__ import annotations

import json
import sys
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
    assert argv[:3] == ["/base/python3", "-I", str(up.dir / "helper.py")] and (up.dir / "helper.py").read_text() == Path(helper.__file__).read_text()
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
