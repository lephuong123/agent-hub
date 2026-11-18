"""Hub clone × filehub (thư mục chung "Không gian số"). Gói CŨ (hub còn đồng bộ 2 chiều — đã bỏ ở H5): token THIẾT BỊ không bao giờ vào gói
(cả clone lẫn move); nhập bỏ khoá cấu hình của đồng bộ, thư mục đồng bộ thành "cần chuyển" (move: `mode: sync`, `local_path` qua pathmap;
clone: gỡ), `filehub/` + `secrets/filehub/` của máy đích giữ nguyên, doctor báo, dán khoá cổng xoá nhắc. Không gọi claude, không mạng.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from cfharness import hub_doctor as D
from cfharness import hub_transfer as T
from cfharness import transfer_secrets as S
from cfharness.config import HubConfig
from cfharness.errors import HubError
from cfharness.paths import HubPaths

from test_hub_transfer import make_source

PW = "correct horse battery staple 42"
TOKEN = "kgs-device-refresh-token-NEVER-0001"
TARGET_TOKEN = "kgs-target-own-device-token-0002"


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(S, "SCRYPT_N", 2**14)


def fh_source(tmp_path: Path, monkeypatch) -> tuple[HubPaths, Path, Path]:
    """Hub máy A đã thiết lập filehub: bật, 1 thư mục chung, tên thiết bị đặt tay, token thiết bị + profile/status của binary sync,
    agent dùng workspace `filehub:chung`, run dở có cwd trong thư mục chung, đang tạm dừng đồng bộ."""
    src, proj = make_source(tmp_path, monkeypatch)
    user = tmp_path / "userA"
    local = user / "KhongGianSo" / "chung"
    local.mkdir(parents=True)
    b = user / "bin" / "tang-thu-sync-headless"
    b.parent.mkdir()
    b.write_text("#!/bin/sh\nexit 0\n")
    b.chmod(0o755)
    cfg = HubConfig(src.config)
    cfg.set("filehub.server", "https://kgs.example.test")
    cfg.set("filehub.sync_binary", str(b))
    cfg.set("filehub.device_name", "cfharness · Máy cũ")
    cfg.set("filehub.local_parent", str(user / "KhongGianSo"))
    cfg.set("filehub.folders", [{"name": "chung", "root_id": "r_chung", "ws_id": "ws_1", "local_path": str(local)}])
    cfg.set("filehub.enabled", True)
    td = src.home / "secrets" / "filehub"
    td.mkdir(mode=0o700)
    (td / "tokens.json").write_text(json.dumps({"refresh_token": TOKEN}))
    os.chmod(td / "tokens.json", 0o600)
    fd = src.home / "filehub" / "profile"
    fd.mkdir(parents=True)
    (fd / "base.sqlite").write_text(f"profile-of-old-device {TOKEN}")
    (src.home / "filehub" / "status.json").write_text(json.dumps({"state": "idle", "roots": [{"id": "r_chung"}]}))
    c = sqlite3.connect(str(src.db))
    try:
        c.execute("UPDATE agents SET workspace_default='filehub:chung' WHERE id='w1'")
        c.execute("UPDATE runs SET cwd=? WHERE id='r_q1'", (str(local),))
        c.execute("INSERT INTO hub_state(key, value, updated_at) VALUES ('filehub.paused','true','t')")
        c.commit()
    finally:
        c.close()
    return src, proj, local


def dest(tmp_path: Path, monkeypatch, name: str = "userB") -> HubPaths:
    monkeypatch.setenv("HOME", str(tmp_path / name))
    (tmp_path / name).mkdir(exist_ok=True)
    return HubPaths.resolve(tmp_path / name / ".cfharness")


def _cfg(p: HubPaths) -> dict:
    return HubConfig(p.config).get("filehub")


def _state(p: HubPaths, key: str):
    c = sqlite3.connect(str(p.db))
    try:
        r = c.execute("SELECT value FROM hub_state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None
    finally:
        c.close()


# ============================================================================ xuất: token thiết bị không bao giờ vào gói
@pytest.mark.parametrize("for_move", [False, True])
def test_device_token_never_in_package_clone_or_move(tmp_path, monkeypatch, for_move):
    src, _, local = fh_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="full", for_move=for_move)["path"])
    r = T.seal_package(pkg, src.home, PW)
    assert r["sealed"]
    raw = pkg.read_bytes()
    assert TOKEN.encode() not in raw and b"profile-of-old-device" not in raw
    with tarfile.open(pkg, "r:") as tf:
        names = tf.getnames()
    assert not any("filehub" in n for n in names), names  # cả home/filehub/ lẫn secrets/filehub/
    m = T.read_manifest(pkg)
    assert m["secrets"]["names"] == ["API_KEY"] and m["missing_secrets"] == []  # thư mục con không tính là secret (thiếu)
    payload = S.decrypt(m, T.verify_package(pkg)["secrets_blob"], PW)
    assert not any("filehub" in k for k in payload)
    # thư mục chung KHÔNG phải repo workspace → không vào repos.json
    v = T.verify_package(pkg, extract_to=tmp_path / "x")
    repos = json.loads((tmp_path / "x" / "repos.json").read_text())["repos"] if (tmp_path / "x" / "repos.json").exists() else []
    assert v["ok"] and not any(str(local) in json.dumps(rp) for rp in repos)
    # gói sửa tay để nhét token thiết bị → bộ ghi/đọc payload từ chối (ngoài danh sách trắng)
    assert not S.allowed_payload_path("secrets/filehub/tokens.json") and "secrets/filehub/tokens.json" in S.NEVER
    with pytest.raises(HubError, match="danh sách trắng"):
        S.encrypt(m, [("secrets/filehub/tokens.json", TOKEN.encode())], PW)


# ============================================================================ nhập gói cũ: thư mục đồng bộ → cần chuyển
def test_import_clone_old_package_drops_sync_and_report(tmp_path, monkeypatch):
    src, _, _ = fh_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])
    T.seal_package(pkg, src.home, PW)
    dst = dest(tmp_path, monkeypatch)
    plan = T.import_plan(dst, pkg, passphrase=PW)
    assert plan["filehub"]["folders"][0]["name"] == "chung" and plan["filehub"]["mode"] == "clone"
    assert any(w.startswith("Thư mục Không gian số: dán khoá cổng cfharness") for w in plan["warnings"]), plan["warnings"]
    assert not dst.config.exists()  # dry-run không ghi gì
    r = T.import_package(dst, pkg, passphrase=PW)
    fh = _cfg(dst)
    assert fh["folders"] == [] and fh["enabled"] is True and fh["server"] == "https://kgs.example.test"
    raw = HubConfig(dst.config)._overrides["filehub"]
    assert not {"sync_binary", "device_name", "local_parent"} & set(raw), raw  # khoá cấu hình của đồng bộ cũ bị bỏ
    assert not (dst.home / "secrets" / "filehub").exists() and not (dst.home / "filehub").exists()
    st = _state(dst, "import_filehub")
    assert st["mode"] == "clone" and st["needs_login"] and st["agents"] == ["w1"] and st["command"] == "cfharness filehub lane setup"
    assert _state(dst, "filehub.paused") is None  # tạm dừng là của máy cũ
    assert set(r["filehub"]["config_changed"]) == {"filehub.sync_binary", "filehub.device_name", "filehub.local_parent", "filehub.folders"}
    from cfharness.hub_transfer_web import _brief_report

    br = _brief_report(r)["filehub"]
    assert set(br) == {"mode", "command", "agents", "folders", "lane_folders"} and br["folders"] == [{"name": "chung"}]
    # daemon đích: workspace filehub: từ chối rõ ràng, không có gì chạy
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    h = Hub(dst, runtime=FakeRuntime())
    try:
        assert h.filehub.status()["relogin_after_import"]
        h.filehub.start()
        assert not (dst.home / "filehub").exists()
        with pytest.raises(HubError, match="không có thư mục chung"):
            h.filehub.resolve_ref("filehub:chung")
    finally:
        h.store.close()


def test_import_move_old_package_marks_needs_migration(tmp_path, monkeypatch):
    src, _, local = fh_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core", for_move=True)["path"])
    T.seal_package(pkg, src.home, PW)
    dst = dest(tmp_path, monkeypatch)
    want = str(tmp_path / "userB" / "KhongGianSo" / "chung")
    plan = T.import_plan(dst, pkg, mode="move", passphrase=PW)
    assert plan["filehub"]["folders"] == [{"name": "chung", "local_path": want}]  # dry-run hiện đường dẫn ĐÃ map
    T.import_package(dst, pkg, mode="move", passphrase=PW)
    fh = _cfg(dst)
    assert fh["folders"] == [{"name": "chung", "root_id": "r_chung", "ws_id": "ws_1", "local_path": want, "mode": "sync"}]
    assert "local_parent" not in HubConfig(dst.config)._overrides["filehub"]
    assert not (dst.home / "secrets" / "filehub").exists()  # move cũng không mang token thiết bị
    st = _state(dst, "import_filehub")
    assert st["folders_kept"] is True and st["needs_migration"] == ["chung"]
    assert "dùng đồng bộ cũ (đã bỏ)" in T.filehub_note(st)
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    h = Hub(dst, runtime=FakeRuntime())
    try:
        assert h.filehub.status()["state"] == "needs_migration"
        with pytest.raises(HubError, match="đồng bộ cũ"):
            h.filehub.resolve_ref("filehub:chung")
    finally:
        h.store.close()


def test_import_replace_keeps_target_filehub_files(tmp_path, monkeypatch):
    src, _, _ = fh_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])
    dst = dest(tmp_path, monkeypatch)
    # máy đích đã có hub + tàn dư đồng bộ cũ của chính nó
    dst.home.mkdir(parents=True, exist_ok=True)
    dst.config.write_text("guard:\n  git_probe: false\n")
    (dst.home / "secrets" / "filehub").mkdir(parents=True, mode=0o700)
    (dst.home / "secrets" / "filehub" / "tokens.json").write_text(TARGET_TOKEN)
    (dst.home / "filehub" / "profile").mkdir(parents=True)
    (dst.home / "filehub" / "status.json").write_text('{"state": "idle"}')
    T.import_package(dst, pkg, replace=True, skip_secrets=True)
    assert (dst.home / "secrets" / "filehub" / "tokens.json").read_text() == TARGET_TOKEN
    assert (dst.home / "filehub" / "status.json").read_text() == '{"state": "idle"}'


# ============================================================================ doctor
def test_doctor_filehub_checks(tmp_path, monkeypatch):
    src, _, _ = fh_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core", for_move=True)["path"])
    dst = dest(tmp_path, monkeypatch)
    T.import_package(dst, pkg, mode="move", skip_secrets=True)
    (dst.home / "secrets" / "filehub").mkdir(parents=True, mode=0o700)  # thư mục con hợp lệ 0700 không bị báo "quyền rộng"
    os.chmod(dst.home / "secrets", 0o700)
    by = {c["id"]: c for c in D.run(dst)["checks"]}
    fh = by["filehub"]
    assert fh["level"] == "warning" and fh["fix"] == "cfharness filehub lane setup", fh
    assert "filehub_binary" not in by and by["perms"]["ok"], by["perms"]
    # nhắc sau nhập đã xoá → còn thư mục đồng bộ cũ → báo cần chuyển
    c = sqlite3.connect(str(dst.db))
    c.execute("UPDATE hub_state SET value='null' WHERE key='import_filehub'")
    c.commit()
    c.close()
    by = {c["id"]: c for c in D.run(dst)["checks"]}
    assert by["filehub"]["level"] == "warning" and "đồng bộ cũ (đã bỏ)" in by["filehub"]["detail"]
    # đã chuyển sang cổng nhưng máy này chưa có khoá
    cfg = HubConfig(dst.config)
    cfg.set("filehub.folders", [{"name": "chung", "root_id": "r_chung", "mode": "lane", "server": "qltm"}])
    cfg.set("filehub.lane.servers", {"qltm": {"url": "http://127.0.0.1:8223"}})
    by = {c["id"]: c for c in D.run(dst)["checks"]}
    assert by["filehub"]["level"] == "warning" and "qltm" in by["filehub"]["detail"] and "chưa có khoá" in by["filehub"]["detail"]
    (dst.home / "secrets" / "FILEHUB_LANE_QLTM").write_text("x")
    by = {c["id"]: c for c in D.run(dst)["checks"]}
    assert by["filehub"]["ok"] and "qua cổng" in by["filehub"]["detail"]
    # hub chưa từng thiết lập filehub
    cfg.set("filehub.enabled", False)
    cfg.set("filehub.folders", [])
    cfg.set("filehub.lane.servers", {})
    assert {c["id"]: c for c in D.run(dst)["checks"]}["filehub"]["level"] == "info"


# ============================================================================ dán khoá cổng ở máy đích → hết nhắc
async def test_lane_setup_clears_import_flag(hub_home):
    from fake_kgs_lane import KEY, FakeLane
    from m7_fake import close, make_daemon, rpc

    lane = FakeLane()
    d = await make_daemon(hub_home)
    try:
        d.hub.store.state_set(T.FILEHUB_STATE, {"mode": "clone", "needs_login": True})
        assert d.hub.filehub.status()["relogin_after_import"]
        await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
        assert not d.hub.store.state_get(T.FILEHUB_STATE) and not d.hub.filehub.status()["relogin_after_import"]
    finally:
        await d.hub.filehub.stop()
        await close(d)
        lane.close()


def test_import_filehub_state_is_machine_state():
    assert T.FILEHUB_STATE in T.MACHINE_STATE and "filehub.paused" in T.MACHINE_STATE
