"""Hub clone × filehub (thư mục chung "Không gian số"): token THIẾT BỊ không bao giờ vào gói (cả clone lẫn move), nhập không bật đồng bộ
(TẮT, chưa đăng nhập, không tên thiết bị cũ), `local_path` đi qua pathmap (move), `filehub/` + `secrets/filehub/` của máy đích giữ nguyên,
doctor báo filehub, đăng nhập lại xoá nhắc. Không gọi claude, không mạng (binary sync giả `fake_tangthu_sync.py`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
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


# ============================================================================ nhập: TẮT, chưa đăng nhập, không tự đồng bộ
def test_import_clone_filehub_off_folders_removed_and_report(tmp_path, monkeypatch):
    src, _, _ = fh_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])
    T.seal_package(pkg, src.home, PW)
    dst = dest(tmp_path, monkeypatch)
    plan = T.import_plan(dst, pkg, passphrase=PW)
    assert plan["filehub"]["folders"][0]["name"] == "chung" and plan["filehub"]["mode"] == "clone"
    assert any(w.startswith("Thư mục Không gian số cần đăng nhập lại: `cfharness filehub setup --name chung --binary ") for w in plan["warnings"]), plan["warnings"]
    assert not dst.config.exists()  # dry-run không ghi gì
    r = T.import_package(dst, pkg, passphrase=PW)
    fh = _cfg(dst)
    assert fh["enabled"] is False and fh["folders"] == [] and fh["device_name"] is None
    assert fh["server"] == "https://kgs.example.test"  # cấu hình không phải thiết bị giữ lại → setup không cần gõ lại server
    assert not (dst.home / "secrets" / "filehub").exists() and not (dst.home / "filehub").exists()
    st = _state(dst, "import_filehub")
    assert st["mode"] == "clone" and st["needs_login"] and st["agents"] == ["w1"] and st["command"].startswith("cfharness filehub setup --name chung")
    assert _state(dst, "filehub.paused") is None  # tạm dừng là của máy cũ
    assert r["filehub"]["config_changed"] == ["filehub.enabled", "filehub.device_name", "filehub.folders"]
    assert "Thư mục Không gian số cần đăng nhập lại" in T.filehub_note(r["filehub"])
    from cfharness.hub_transfer_web import _brief_report

    br = _brief_report(r)["filehub"]
    assert set(br) == {"mode", "command", "agents", "folders"} and br["folders"] == [{"name": "chung"}]
    # daemon đích: service không coi là bật → không spawn binary sync; workspace filehub: từ chối rõ ràng
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    h = Hub(dst, runtime=FakeRuntime())
    try:
        assert not h.filehub.enabled() and not h.filehub.configured() and h.filehub.status()["relogin_after_import"]
        h.filehub.start()
        assert h.filehub.supervisor._task is None and not (dst.home / "filehub").exists()
        with pytest.raises(HubError, match="chưa bật"):
            h.filehub.resolve_ref("filehub:chung")
    finally:
        h.store.close()


def test_import_move_keeps_folders_with_mapped_local_path(tmp_path, monkeypatch):
    src, _, _ = fh_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core", for_move=True)["path"])
    T.seal_package(pkg, src.home, PW)
    dst = dest(tmp_path, monkeypatch)
    want = str(tmp_path / "userB" / "KhongGianSo" / "chung")
    plan = T.import_plan(dst, pkg, mode="move", passphrase=PW)
    assert plan["filehub"]["folders"] == [{"name": "chung", "local_path": want}]  # dry-run hiện đường dẫn ĐÃ map
    T.import_package(dst, pkg, mode="move", passphrase=PW)
    fh = _cfg(dst)
    assert fh["enabled"] is False and fh["device_name"] is None
    assert fh["folders"] == [{"name": "chung", "root_id": "r_chung", "ws_id": "ws_1", "local_path": want}]
    assert fh["local_parent"] == str(tmp_path / "userB" / "KhongGianSo")
    assert not (dst.home / "secrets" / "filehub").exists()  # move cũng không mang token thiết bị
    assert _state(dst, "import_filehub")["folders_kept"] is True
    assert "đóng băng không dừng filehub" in T.filehub_note(_state(dst, "import_filehub"))


def test_import_replace_keeps_target_filehub_files(tmp_path, monkeypatch):
    src, _, _ = fh_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])
    dst = dest(tmp_path, monkeypatch)
    # máy đích đã có hub + filehub riêng (thiết bị của chính nó)
    dst.home.mkdir(parents=True, exist_ok=True)
    dst.config.write_text("guard:\n  git_probe: false\n")
    (dst.home / "secrets" / "filehub").mkdir(parents=True, mode=0o700)
    (dst.home / "secrets" / "filehub" / "tokens.json").write_text(TARGET_TOKEN)
    (dst.home / "filehub" / "profile").mkdir(parents=True)
    (dst.home / "filehub" / "status.json").write_text('{"state": "idle"}')
    T.import_package(dst, pkg, replace=True, skip_secrets=True)
    assert (dst.home / "secrets" / "filehub" / "tokens.json").read_text() == TARGET_TOKEN
    assert (dst.home / "filehub" / "status.json").read_text() == '{"state": "idle"}'
    assert _cfg(dst)["enabled"] is False  # cấu hình đến từ gói: vẫn TẮT tới khi người setup


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
    assert fh["level"] == "warning" and fh["fix"].startswith("cfharness filehub setup --name chung"), fh
    # binary sync đường dẫn máy cũ (map sang userB) không có ở máy này
    assert by["filehub_binary"]["level"] == "warning" and "--binary" in by["filehub_binary"]["fix"] and "--binary" in fh["fix"]
    assert by["perms"]["ok"], by["perms"]
    # đăng nhập lại xong (nhắc đã xoá) nhưng token chưa có + đang bật → "chưa đăng nhập"
    c = sqlite3.connect(str(dst.db))
    c.execute("UPDATE hub_state SET value='null' WHERE key='import_filehub'")
    c.commit()
    c.close()
    b = tmp_path / "userB" / "bin" / "tang-thu-sync-headless"
    b.parent.mkdir(parents=True)
    b.write_text("#!/bin/sh\n")
    b.chmod(0o755)
    cfg = HubConfig(dst.config)
    cfg.set("filehub.enabled", True)
    by = {c["id"]: c for c in D.run(dst)["checks"]}
    assert by["filehub"]["level"] == "warning" and by["filehub"]["fix"] == "cfharness filehub login" and "filehub_binary" not in by
    (dst.home / "secrets" / "filehub" / "tokens.json").write_text("{}")
    by = {c["id"]: c for c in D.run(dst)["checks"]}
    assert by["filehub"]["ok"] and "đã đăng nhập" in by["filehub"]["detail"]
    # hub chưa từng thiết lập filehub
    cfg.set("filehub.enabled", False)
    cfg.set("filehub.folders", [])
    assert {c["id"]: c for c in D.run(dst)["checks"]}["filehub"]["level"] == "info"


# ============================================================================ đăng nhập lại ở máy đích → hết nhắc
@pytest.mark.skipif(sys.platform == "win32", reason="binary sync giả là shim #!/bin/sh")
async def test_relogin_clears_import_flag(hub_home, tmp_path):
    from fh_fake import fake_bin, until
    from m7_fake import close, make_daemon, rpc

    d = await make_daemon(hub_home)
    try:
        d.hub.store.state_set(T.FILEHUB_STATE, {"mode": "clone", "needs_login": True})
        assert d.hub.filehub.status()["relogin_after_import"]
        d.hub.config.set("filehub.tick_s", 0.05)
        d.hub.config.set("filehub.poll_s", 0.1)
        await rpc(d, "filehub.setup", {"server": "https://kgs.example.test", "binary": str(fake_bin(tmp_path / "bin")), "name": "chung",
                                       "local_parent": str(tmp_path / "KGS"), "no_browser": True})
        await until(lambda: d.hub.filehub.login_state.get("state") == "done")
        assert not d.hub.store.state_get(T.FILEHUB_STATE) and not d.hub.filehub.status()["relogin_after_import"]
    finally:
        await d.hub.filehub.stop()
        await close(d)


def test_import_filehub_state_is_machine_state():
    assert T.FILEHUB_STATE in T.MACHINE_STATE and "filehub.paused" in T.MACHINE_STATE
