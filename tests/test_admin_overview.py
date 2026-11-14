"""Tổng quan máy (Q6): xuất trạng thái CHỈ SỐ LIỆU ra thư mục chung + đọc lại (admin_overview.py, admin_cli.py, web/admin_pages.py).

Không gọi `claude` thật (FakeRuntime), không gọi dseditgroup/chgrp thật ngoài nhóm hiện có của user chạy test.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

grp = pytest.importorskip("grp", reason="tổng quan máy dùng nhóm POSIX (grp) — không có trên Windows")

from cfharness import admin_overview as ao
from cfharness import auth
from cfharness.errors import BLOCKED, HubError
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore

SECRET = "TOPSECRET-sk-ant-zzz-Phuong-du-an-B"
ME = ao.os_user()


def _walk_strings(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(k)
            _walk_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_strings(v, out)
    elif isinstance(obj, str):
        out.append(obj)
    return out


def _sample(**over) -> dict:
    d = {"schema": 1, "user": ME, "hub_id": "0123456789abcdef", "version": "0.2.3", "generated_at": "2026-11-07T08:00:00Z",
         "generated_ts": time.time(), "interval_s": 60, "daemon": {"up": True}, "estop": {"on": False, "teams": 0},
         "cost_usd": {"today": 1.5, "week": 7.25}, "runs": {"running": 2, "queued": 1, "today_failed": 1},
         "approvals": {"open": 3}, "questions": {"open": 1}, "agents": {"total": 4}}
    d.update(over)
    return d


def _put(d: Path, name: str, content, mtime: float | None = None) -> Path:
    p = d / name
    p.write_text(content if isinstance(content, str) else json.dumps(content))
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# ------------------------------------------------------------------ thuần
def test_sanitize_drops_free_text_and_unknown_keys():
    raw = _sample(summary=SECRET, version="0.2.3; rm -rf /", hub_id="/Users/an/.cfharness",
                  runs={"running": 1, "title": SECRET, "queued": -1, "today_failed": True}, agents={"total": 2, "ids": ["a", "b"]},
                  cost_usd={"today": float("nan"), "week": 2.0})
    s = ao.sanitize(raw)
    strings = _walk_strings(s, [])
    assert SECRET not in json.dumps(s) and "summary" not in s and "/Users" not in json.dumps(s)
    assert "version" not in s and "hub_id" not in s
    assert s["runs"] == {"running": 1} and s["agents"] == {"total": 2} and s["cost_usd"] == {"week": 2.0}
    # mọi chuỗi còn lại là khoá SPEC hoặc giá trị khớp regex enum/id
    allowed_keys = set()

    def keys(spec):
        for k, v in spec.items():
            allowed_keys.add(k)
            if isinstance(v, dict):
                keys(v)

    keys(ao.SPEC)
    for x in strings:
        assert x in allowed_keys or any(rx.match(x) for rx in ao._RE.values()), x


# ------------------------------------------------------------------ xuất
async def test_export_content_allowlist_atomic_mode_and_no_secret(hub_home, tmp_path):
    from test_web import ISOLATED, _start, _stop, until

    from cfharness.runtime import FakeRuntime

    out_dir = tmp_path / "status"
    out_dir.mkdir()
    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        hub = env.hub
        hub.agents.create({"id": "an-secret-agent", "name": SECRET, "model": "haiku", "description": SECRET})
        run = hub.runs.submit("an-secret-agent", f"deploy with password {SECRET} to /Users/an/Workspace/du-an-B")
        assert await until(lambda: (hub.store.get_run(run["id"]) or {}).get("status") not in ("queued", "running"), 15)
        # tắt → không ghi
        hub.config.set("admin_export.dir", str(out_dir))
        assert hub.admin_export.export_once() is None and not list(out_dir.iterdir())
        hub.config.set("admin_export.enabled", True)
        hub.config.set("admin_export.group", "cfharness-admin-khong-co-nhom-nay")
        p = hub.admin_export.export_once()
        assert p == out_dir / f"{ME}.json"
        assert hub.admin_export.last_warnings == ["group_missing"]
        st = p.stat()
        assert stat.S_IMODE(st.st_mode) == 0o640
        assert [x.name for x in out_dir.iterdir()] == [p.name]  # không sót file tạm
        text = p.read_text()
        data = json.loads(text)
        assert SECRET not in text and "an-secret-agent" not in text and "du-an-B" not in text
        assert str(hub_home.home) not in text and os.uname().nodename not in text
        assert ao.sanitize(data) == data  # chỉ khoá SPEC
        assert data["schema"] == ao.SCHEMA and data["user"] == ME and data["hub_id"] == ao.hub_id(hub_home.home)
        assert data["agents"]["total"] == 1 and data["runs"]["today_total"] >= 1 and data["daemon"]["up"] is True
        assert data["interval_s"] == 60 and "cost_usd" in data and "approvals" in data
        # nhóm có thật (nhóm chính của user test) → không cảnh báo; ghi đè nguyên tử giữ 0640
        hub.config.set("admin_export.group", grp.getgrgid(os.getgid()).gr_name)
        hub.admin_export.export_once()
        assert hub.admin_export.last_warnings == [] and stat.S_IMODE(p.stat().st_mode) == 0o640
        # symlink cài sẵn ở tên file tạm không bị theo: mkstemp O_EXCL tạo tên ngẫu nhiên
        assert [x.name for x in out_dir.iterdir()] == [p.name]
    finally:
        await _stop(env)
    # hub dừng → lần ghi cuối daemon.up=false → người đọc thấy "stopped"
    r = ao.read_dir(out_dir)
    assert r["hubs"][0]["state"] == "stopped"


def test_export_missing_dir_is_skipped_with_error(hub_home, tmp_path):
    from cfharness.hub import Hub

    hub = Hub(hub_home)
    try:
        hub.config.set("admin_export.enabled", True)
        hub.config.set("admin_export.dir", str(tmp_path / "khong-co"))
        assert hub.admin_export.export_once() is None and hub.admin_export.last_error == "dir_missing"
        assert not (tmp_path / "khong-co").exists()  # bộ xuất không tự tạo thư mục chung
    finally:
        hub.store.close()


def test_config_validation():
    from cfharness.config import validate_setting

    for k, v in (("admin_export.interval_s", 5), ("admin_export.interval_s", "60"), ("admin_export.dir", "rel/path"),
                 ("admin_export.group", "x; rm"), ("admin_export.enabled", "yes")):
        with pytest.raises(ValueError):
            validate_setting(k, v)
    validate_setting("admin_export.interval_s", 120)
    validate_setting("admin_export.dir", "/Users/Shared/cfharness/status")


# ------------------------------------------------------------------ đọc
def test_read_dir_stale_malformed_owner_and_totals(tmp_path):
    d = tmp_path / "status"
    d.mkdir()
    now = time.time()
    _put(d, f"{ME}.json", _sample(), mtime=now - 30)
    assert ao.read_dir(d, now=now)["hubs"][0]["state"] == "ok"
    # quá 3× chu kỳ → stale; run đang chạy không cộng vào tổng
    _put(d, f"{ME}.json", _sample(), mtime=now - 181)
    r = ao.read_dir(d, now=now)
    h = r["hubs"][0]
    assert h["state"] == "stale" and h["stale"] and r["totals"]["running"] == 0 and r["totals"]["cost_today_usd"] == 1.5
    # chu kỳ khai trong file được tôn trọng
    _put(d, f"{ME}.json", _sample(interval_s=600), mtime=now - 181)
    assert ao.read_dir(d, now=now)["hubs"][0]["state"] == "ok"
    # file hỏng các kiểu: không ném
    _put(d, "bad1.json", "{not json")
    _put(d, "bad2.json", "[1, 2]")
    _put(d, "bad3.json", {"schema": 99, "user": "bad3"})
    _put(d, "bad4.json", "x" * (ao.MAX_FILE + 10))
    (d / "bad5.json").mkdir()
    os.symlink(d / f"{ME}.json", d / "bad6.json")
    _put(d, ".hidden.json", _sample())
    _put(d, "notes.txt", "hello")
    # tên file khác user trong file / khác chủ file → owner_mismatch, không hiện số liệu
    _put(d, "someoneelse.json", _sample(user="someoneelse"))
    _put(d, "spoof.json", _sample())
    r = ao.read_dir(d, now=now)
    by = {h["user"]: h for h in r["hubs"]}
    assert set(by) == {ME, "bad1", "bad2", "bad3", "bad4", "bad5", "bad6", "someoneelse", "spoof"}
    assert by["bad1"]["error"] == "bad_json" and by["bad2"]["error"] == "not_object" and by["bad3"]["error"] == "unsupported_schema"
    assert by["bad4"]["error"] == "too_large" and by["bad5"]["error"] == "not_regular" and by["bad6"]["error"] == "symlink"
    assert all(by[k]["state"] == "malformed" and by[k]["data"] is None for k in ("bad1", "bad2", "bad3", "bad4", "bad5", "bad6"))
    assert by["someoneelse"]["state"] == "owner_mismatch" and by["someoneelse"]["data"] is None  # uid chủ file = ME ≠ someoneelse
    assert by["spoof"]["state"] == "owner_mismatch"  # trường user = ME ≠ tên file
    assert r["totals"]["bad"] == 8
    # chữ tự do trong file của người khác không lọt ra
    _put(d, f"{ME}.json", {**_sample(), "note": SECRET, "runs": {"running": 1, "x": SECRET}})
    assert SECRET not in json.dumps(ao.read_dir(d, now=now))


def test_read_dir_missing(tmp_path):
    r = ao.read_dir(tmp_path / "khong-co")
    assert r["exists"] is False and r["hubs"] == []


# ------------------------------------------------------------------ RPC + quyền + CLI
async def test_rpc_human_only(hub_home, tmp_path, monkeypatch):
    from m7_fake import close, make_daemon, rpc

    import cfharness.admin_control as ac

    d = await make_daemon(hub_home)
    try:
        status = tmp_path / "status"
        status.mkdir()
        _put(status, f"{ME}.json", _sample())
        d.hub.config.set("admin_export.dir", str(status))
        # người nhưng KHÔNG thuộc nhóm cfharness-admin → từ chối (plan-admin-users: is_admin ở RPC)
        with pytest.raises(HubError) as ei:
            await rpc(d, "admin.overview", {})
        assert ei.value.code == BLOCKED and "cfharness-admin" in str(ei.value)
        monkeypatch.setattr(ac, "_GETGRNAM", lambda n: type("G", (), {"gr_gid": 4242, "gr_mem": []})())
        monkeypatch.setattr(ac, "_GETGROUPS", lambda: [4242])
        r = await rpc(d, "admin.overview", {})
        assert r["exists"] and r["hubs"][0]["user"] == ME and r["self"]["enabled"] is False
        assert "admin.overview" in auth.HUMAN_ONLY
        agent = auth.Caller(via="socket", pid=99, token_ok=True, run_id="r_x", agent_id="w1", evidence=["env_run_id"])
        with pytest.raises(HubError) as ei:
            await rpc(d, "admin.overview", {}, as_caller=agent)
        assert ei.value.code == BLOCKED
        # agent sửa config.yaml nhánh admin_export ngoài RPC → bị khôi phục (nhánh an toàn)
        from cfharness.guard.integrity import SECURITY_KEYS

        assert "admin_export" in SECURITY_KEYS
    finally:
        await close(d)


def test_cli_render_and_classifier(tmp_path):
    from cfharness.admin_cli import render_text

    d = tmp_path / "status"
    d.mkdir()
    _put(d, f"{ME}.json", _sample())
    _put(d, "bad1.json", "{")
    txt = render_text({**ao.read_dir(d), "self": {"enabled": False}})
    assert ME in txt and "file hỏng" in txt and "Tổng: $1.5000 hôm nay" in txt and "admin_export.enabled" in txt
    assert "chưa có" in render_text(ao.read_dir(tmp_path / "x"))
    ctx = GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "h").load())
    for cmd in ("cfharness admin overview", "cfharness admin overview --json"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd


def test_sandbox_denies_shared_status_dir(tmp_path):
    import sys

    if sys.platform == "win32":
        pytest.skip("macOS/Linux")
    from cfharness.guard.runtime import sandbox_settings

    s = sandbox_settings(tmp_path / "hub", tmp_path / "hub.sock", {})
    assert ao.SHARED_ROOT in s["filesystem"]["denyRead"] and ao.SHARED_ROOT in s["filesystem"]["denyWrite"]


# ------------------------------------------------------------------ web
async def test_web_page_renders_and_menu_only_for_admin(hub_home, tmp_path, as_admin):
    from test_web import ISOLATED, _start, _stop

    import cfharness.admin_control as ac
    from cfharness.runtime import FakeRuntime

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        status = tmp_path / "status"
        env.hub.config.set("admin_export.dir", str(status))
        await env.login()
        grnam, groups = ac._GETGRNAM, ac._GETGROUPS
        ac._GETGROUPS = lambda: []  # không thuộc nhóm admin → không có mục menu, trang 403
        try:
            html = (await env.client.get("/costs")).text
            assert "/admin/overview" not in html and "/admin/users" not in html
            assert (await env.client.get("/admin/overview")).status_code == 403
        finally:
            ac._GETGRNAM, ac._GETGROUPS = grnam, groups
            env.app.state.web._admin_nav = None
        page = (await env.client.get("/admin/overview")).text
        assert "Chưa thiết lập" in page
        status.mkdir()
        _put(status, f"{ME}.json", _sample())
        _put(status, "evil.json", {**_sample(user="evil"), "note": "<script>alert(1)</script>"})
        page = (await env.client.get("/admin/overview")).text
        assert "Tổng quan máy" in page and f"<code>{ME}</code>" in page and "Sai chủ file" in page
        assert "<script>alert" not in page and "$1.5000" in page
        html = (await env.client.get("/costs")).text
        assert 'href="/admin/overview"' in html
    finally:
        await _stop(env)
