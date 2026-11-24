"""Hub clone PR2 — secret mã hoá trong gói (`transfer_secrets.py`, `hub_transfer.seal_package`), huỷ approval/câu hỏi khi clone, hoàn tác
khi nhập lỗi giữa chừng, làm lặng vòng nền khi xuất. Không gọi claude, không mạng. scrypt hạ n=2^14 cho nhanh (bộ đọc vẫn kiểm khoảng).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import sqlite3
import stat
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cfharness import backup as B
from cfharness import hub_transfer as T
from cfharness import redact
from cfharness import transfer_secrets as S
from cfharness.errors import HubError
from cfharness.paths import HubPaths

from test_hub_transfer import _rewrite_pkg, make_source

PW = "correct horse battery staple 42"
OAUTH = "MCP_OAUTH_SRV_A1B2C3"  # bản ghi OAuth MCP kiểu cũ (tiền tố MCP_OAUTH_ — OAuth của hub gỡ từ v0.2.28)
VALUES = {
    "API_KEY": "sk-TRANSFER-SECRET-XYZ",  # make_source đặt sẵn
    "TELEGRAM_BOT_TOKEN": "123456:tg-bot-token-AAAA",
    "TELEGRAM_CALLBACK_KEY": "tg-callback-hmac-key-0001",
    "SLACK_BOT_TOKEN": "xoxb-slack-bot-token-0001",
    "SLACK_APP_TOKEN": "xapp-slack-app-token-0001",
    OAUTH: json.dumps({"server_url": "https://mcp.example/x", "tokens": {"access_token": "oauth-access-0001", "refresh_token": "oauth-refresh-0001"}}),
    "MCP_SRV_AUTHORIZATION": "Bearer static-mcp-token-0001",
    "CLAUDE_ACCT_WORK": "sk-ant-oat-account-token-0001",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-current-token-0001",
    "MULTI_LINE": "dòng 1\nline 2 \t é\n",
}
CLONE_DROPPED = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CALLBACK_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", OAUTH}
WEB_KEY = b'{"key": "web-session-signing-key-NEVER"}'
DEVICES = b'{"devices": "trusted-NEVER"}'
TOTP = b'{"secret": "JBSWY3DPEHPK3PXP", "last_step": 0}'
MCP_TOKENS = b'{"read": "mcp-http-read-token-0001"}'


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(S, "SCRYPT_N", 2**14)


def source(tmp_path: Path, monkeypatch) -> tuple[HubPaths, Path]:
    src, proj = make_source(tmp_path, monkeypatch)
    sd = src.home / "secrets"
    for n, v in VALUES.items():
        (sd / n).write_bytes(v.encode("utf-8"))
        os.chmod(sd / n, 0o600)
    (sd / ".meta.json").write_text(json.dumps({"CLAUDE_ACCT_WORK": {"set_at": 1700000000.0}, "GONE": {"set_at": 1.0}}))
    (src.home / "auth").mkdir(exist_ok=True)
    (src.home / "auth" / "web.json").write_bytes(WEB_KEY)
    (src.home / "auth" / "trusted_devices.json").write_bytes(DEVICES)
    (src.home / "auth" / "totp.json").write_bytes(TOTP)
    (src.home / "mcp_server").mkdir(exist_ok=True)
    (src.home / "mcp_server" / "tokens.json").write_bytes(MCP_TOKENS)
    Path(f"{src.socket}.auth").write_text("human-socket-token-NEVER")
    return src, proj


def sealed_pkg(src: HubPaths, **kw) -> Path:
    pkg = Path(T.export_offline(src, profile="core")["path"])
    r = T.seal_package(pkg, src.home, PW, **kw)
    assert r["sealed"], r
    return pkg


def dest(tmp_path: Path, monkeypatch, name: str = "userB") -> HubPaths:
    monkeypatch.setenv("HOME", str(tmp_path / name))
    (tmp_path / name).mkdir(exist_ok=True)
    return HubPaths.resolve(tmp_path / name / ".cfharness")


# ============================================================================ phân loại
def test_classify_secret_kinds_per_mode():
    kinds = {n: S.classify(n) for n in VALUES}
    assert kinds["TELEGRAM_BOT_TOKEN"] == kinds["SLACK_APP_TOKEN"] == "channel" and kinds[OAUTH] == "oauth"
    assert kinds["MCP_SRV_AUTHORIZATION"] == "mcp_static" and kinds["CLAUDE_ACCT_WORK"] == kinds["CLAUDE_CODE_OAUTH_TOKEN"] == "claude_account"
    assert S.classify("MCP_SRV_OAUTH_CLIENT_SECRET") == "mcp_static"  # client secret người nhập — tĩnh, giữ
    assert kinds["API_KEY"] == "other"
    assert {n for n in VALUES if S.skipped_in(n, "clone")} == CLONE_DROPPED
    assert not any(S.skipped_in(n, "move") for n in VALUES)


# ============================================================================ round-trip
def test_seal_roundtrip_move_exact_bytes_private_and_never_auth(tmp_path, monkeypatch):
    src, _ = source(tmp_path, monkeypatch)
    pkg = sealed_pkg(src, with_totp=True, with_mcp_tokens=True)
    raw = pkg.read_bytes()
    for v in [*VALUES.values(), WEB_KEY.decode(), DEVICES.decode(), TOTP.decode(), MCP_TOKENS.decode(), "human-socket-token-NEVER", PW]:
        assert v.encode() not in raw, v[:12]
    m = T.read_manifest(pkg)
    assert m["secrets"]["names"] == sorted(VALUES) and m["secrets"]["totp"] and m["secrets"]["mcp_tokens"] and m["missing_secrets"] == []
    assert m["sections"]["secrets"]["prefix"] == "secrets.enc" and stat.S_IMODE(os.stat(pkg).st_mode) == 0o600
    with tarfile.open(pkg, "r:") as tf:
        names = tf.getnames()
    assert names[-1] == "MANIFEST.json" and names[-2] == "secrets.enc"
    v = T.verify_package(pkg)
    assert v["ok"] and v["secrets_blob"], v["errors"]
    payload = S.decrypt(v["manifest"], v["secrets_blob"], PW)
    assert "auth/web.json" not in payload and "auth/trusted_devices.json" not in payload and not any("sock" in k for k in payload)
    assert json.loads(payload["secrets/.meta.json"]) == {"CLAUDE_ACCT_WORK": {"set_at": 1700000000.0}}
    assert T.check_passphrase(pkg, PW) == sorted(VALUES)
    with pytest.raises(HubError, match="đã có"):
        T.seal_package(pkg, src.home, PW)

    dst = dest(tmp_path, monkeypatch)
    plan = T.import_plan(dst, pkg, mode="move", passphrase=PW)
    assert not plan["blockers"] and plan["secrets"]["unlocked"] is True and plan["secrets"]["import"] == sorted(VALUES), plan["secrets"]
    r = T.import_package(dst, pkg, mode="move", passphrase=PW)
    assert r["secrets"]["imported"] == sorted(VALUES) and not r["secrets"]["skipped_mode"] and r["secrets"]["totp"] and r["secrets"]["mcp_tokens"]
    sd = dst.home / "secrets"
    assert stat.S_IMODE(os.stat(sd).st_mode) == 0o700
    for n, val in VALUES.items():
        assert (sd / n).read_bytes() == val.encode("utf-8") and stat.S_IMODE(os.stat(sd / n).st_mode) == 0o600, n
    assert (dst.home / "auth" / "totp.json").read_bytes() == TOTP and stat.S_IMODE(os.stat(dst.home / "auth" / "totp.json").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(dst.home / "auth").st_mode) == 0o700
    assert (dst.home / "mcp_server" / "tokens.json").read_bytes() == MCP_TOKENS
    assert not (dst.home / "auth" / "web.json").exists() and not (dst.home / "auth" / "trusted_devices.json").exists()
    from cfharness.secrets import SecretStore

    assert SecretStore(sd).set_at("CLAUDE_ACCT_WORK") == 1700000000.0  # giữ tuổi token
    c = sqlite3.connect(str(dst.db))
    imp = json.loads(c.execute("SELECT value FROM hub_state WHERE key='imported_from'").fetchone()[0])
    assert imp["secrets_imported"] == len(VALUES) and imp["secrets_missing"] == []
    c.close()


def test_import_clone_drops_bots_and_oauth_keeps_static_and_accounts(tmp_path, monkeypatch):
    src, _ = source(tmp_path, monkeypatch)
    pkg = sealed_pkg(src)  # không --with-totp/--with-mcp-tokens
    assert not T.read_manifest(pkg)["secrets"]["totp"]
    dst = dest(tmp_path, monkeypatch)
    plan = T.import_plan(dst, pkg)
    assert plan["secrets"]["unlocked"] is None and set(plan["secrets"]["skip_mode"]) == CLONE_DROPPED
    assert any("bỏ 5 secret" in w for w in plan["warnings"]), plan["warnings"]
    r = T.import_package(dst, pkg, passphrase=PW)
    assert set(r["secrets"]["skipped_mode"]) == CLONE_DROPPED and set(r["secrets"]["imported"]) == set(VALUES) - CLONE_DROPPED
    sd = dst.home / "secrets"
    for n in VALUES:
        assert (sd / n).exists() == (n not in CLONE_DROPPED), n
    assert not (dst.home / "auth" / "totp.json").exists() and not (dst.home / "mcp_server" / "tokens.json").exists()
    assert not (sd / OAUTH).exists()  # bản ghi OAuth hub kiểu cũ (gỡ từ v0.2.28) — clone không mang


def test_redaction_after_import_comes_from_secret_store(tmp_path, monkeypatch):
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    src, _ = source(tmp_path, monkeypatch)
    pkg = sealed_pkg(src)
    dst = dest(tmp_path, monkeypatch)
    T.import_package(dst, pkg, passphrase=PW)
    monkeypatch.setattr(redact, "_values", set())  # tiến trình mới: bộ che rỗng
    monkeypatch.setattr(redact, "_rx", None)
    monkeypatch.setattr(redact, "_rx_bytes", None)
    assert VALUES["CLAUDE_ACCT_WORK"] in redact.mask_values(f"x {VALUES['CLAUDE_ACCT_WORK']} y")
    h = Hub(dst, runtime=FakeRuntime())  # SecretStore.preload
    try:
        for n in set(VALUES) - CLONE_DROPPED:
            v = VALUES[n].strip()
            assert v not in redact.mask_values(f"lộ: {VALUES[n]} ."), n
    finally:
        h.store.close()


# ============================================================================ từ chối
def test_wrong_passphrase_refused_before_any_write(tmp_path, monkeypatch):
    src, _ = source(tmp_path, monkeypatch)
    pkg = sealed_pkg(src)
    dst = dest(tmp_path, monkeypatch)
    with pytest.raises(HubError, match="passphrase sai"):
        T.check_passphrase(pkg, PW + "x")
    plan = T.import_plan(dst, pkg, passphrase="sai passphrase rồi")
    assert any("passphrase sai" in b for b in plan["blockers"]) and plan["secrets"]["unlocked"] is False
    with pytest.raises(HubError, match="passphrase sai"):
        T.import_package(dst, pkg, passphrase="sai passphrase rồi")
    with pytest.raises(HubError, match="cần passphrase"):
        T.import_package(dst, pkg)
    assert not dst.db.exists() and T.home_blank(dst)
    assert not list((dst.home / "secrets").glob("*")) if (dst.home / "secrets").exists() else True
    assert not any(p.name.startswith(".import-") for p in dst.home.iterdir())


def test_tampered_package_fails_decryption(tmp_path, monkeypatch):
    src, _ = source(tmp_path, monkeypatch)
    base = sealed_pkg(src)
    saved = base.read_bytes()

    def fresh() -> Path:
        base.write_bytes(saved)
        return base

    def resha(m: dict, path: str, data: bytes) -> dict:
        for f in m["files"]:
            if f["path"] == path:
                f["sha256"], f["size"] = hashlib.sha256(data).hexdigest(), len(data)
        return m

    # (1) manifest sửa một trường bất kỳ (sha mọi phần tử vẫn khớp) → AAD khác
    pkg = fresh()
    _rewrite_pkg(pkg, manifest_fn=lambda m: {**m, "by": "attacker"})
    assert T.verify_package(pkg)["ok"]
    with pytest.raises(HubError, match="passphrase sai hoặc gói đã bị sửa"):
        T.check_passphrase(pkg, PW)
    # (2) tráo nội dung phần khác (MEMORY.md) và sửa sha256 trong manifest cho khớp → kiểm gói qua, giải mã thất bại
    pkg = fresh()
    import gzip

    new = b"# tri thuc bi trao\n"
    _rewrite_pkg(pkg, member_fn=lambda n, d: gzip.compress(new) if n == "home/agents/a1/MEMORY.md" else d,
                 manifest_fn=lambda m: resha(m, "home/agents/a1/MEMORY.md", new))
    assert T.verify_package(pkg)["ok"]
    with pytest.raises(HubError, match="bị sửa"):
        T.check_passphrase(pkg, PW)
    dst = dest(tmp_path, monkeypatch)
    with pytest.raises(HubError, match="bị sửa"):
        T.import_package(dst, pkg, passphrase=PW)
    assert not dst.db.exists()
    # (3) danh sách tên secret trong manifest bị bớt (giấu một secret) → AAD khác
    pkg = fresh()
    _rewrite_pkg(pkg, manifest_fn=lambda m: {**m, "secrets": {**m["secrets"], "names": m["secrets"]["names"][1:]}})
    with pytest.raises(HubError):
        T.check_passphrase(pkg, PW)
    # (4) lật một byte của bản mã (sha cập nhật) → thẻ GCM sai
    pkg = fresh()
    holder = {}

    def flip(n, d):
        if n == "secrets.enc":
            d = d[:5] + bytes([d[5] ^ 1]) + d[6:]
            holder["d"] = d
        return d

    _rewrite_pkg(pkg, member_fn=flip)
    _rewrite_pkg(pkg, manifest_fn=lambda m: resha(m, "secrets.enc", holder["d"]))
    with pytest.raises(HubError, match="bị sửa"):
        T.check_passphrase(pkg, PW)
    # (5) tham số KDF yếu → từ chối khi kiểm gói (không cần passphrase)
    pkg = fresh()
    _rewrite_pkg(pkg, manifest_fn=lambda m: {**m, "secrets": {**m["secrets"], "kdf": {**m["secrets"]["kdf"], "n": 2}}})
    assert any("scrypt" in e for e in T.verify_package(pkg)["errors"])


def test_payload_whitelist_never_carries_web_auth(tmp_path, monkeypatch):
    m = {"id": "x-20261115-000000", "files": [], "sections": {}}
    with pytest.raises(HubError, match="danh sách trắng"):
        S.encrypt(m, [("auth/web.json", WEB_KEY)], PW)
    for bad in ("auth/web.json", "auth/trusted_devices.json", "auth/totp_enroll.json", "secrets/../x", "secrets/lower", "hub.sock.auth"):
        assert not S.allowed_payload_path(bad), bad
    # gói bị dựng tay chứa auth/web.json (mã hoá đúng khoá) → bộ đọc từ chối
    monkeypatch.setattr(S, "allowed_payload_path", lambda n: True)
    m2, ct = S.encrypt(m, [("secrets/API_KEY", b"v" * 10), ("auth/web.json", WEB_KEY)], PW)
    monkeypatch.undo()
    monkeypatch.setattr(S, "SCRYPT_N", 2**14)
    with pytest.raises(HubError, match="không hợp lệ"):
        S.decrypt(m2, ct, PW)


def test_passphrase_rules_file_mode_and_length(tmp_path):
    f = tmp_path / "pw"
    f.write_text(PW + "\n")
    os.chmod(f, 0o644)
    with pytest.raises(HubError, match="0600"):
        S.read_passphrase_file(f)
    os.chmod(f, 0o600)
    assert S.read_passphrase_file(f) == PW
    link = tmp_path / "pw-link"
    link.symlink_to(f)
    with pytest.raises(HubError):
        S.read_passphrase_file(link)
    with pytest.raises(HubError, match="ít nhất 12"):
        S.check_passphrase("short", for_export=True)
    with pytest.raises(HubError, match="ít nhất 12"):
        S.encrypt({"id": "x"}, [("secrets/A_B", b"x" * 9)], "short")


def test_skip_secrets_imports_rest_and_lists_missing(tmp_path, monkeypatch):
    src, _ = source(tmp_path, monkeypatch)
    pkg = sealed_pkg(src)
    dst = dest(tmp_path, monkeypatch)
    plan = T.import_plan(dst, pkg, skip_secrets=True)
    assert not plan["blockers"] and plan["secrets"]["skipped"] and set(plan["missing_secrets"]) == set(VALUES) - CLONE_DROPPED
    r = T.import_package(dst, pkg, skip_secrets=True)
    assert dst.db.exists() and r["secrets"]["skipped"] and not r["secrets"]["imported"]
    assert set(r["missing_secrets"]) == set(VALUES) - CLONE_DROPPED
    assert not (dst.home / "secrets").exists() or not [p for p in (dst.home / "secrets").iterdir() if not p.name.startswith(".")]


# ============================================================================ passphrase không lộ
def test_cli_passphrase_never_leaks(tmp_path, monkeypatch, capsys, caplog):
    from cfharness.cli import main

    caplog.set_level(logging.DEBUG)
    src, _ = source(tmp_path, monkeypatch)
    pwf = tmp_path / "pass.txt"
    pwf.write_text(PW + "\n")
    os.chmod(pwf, 0o600)
    out = tmp_path / "out" / "hub.cfhx"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit):  # không có cờ nhận passphrase trong argv
        main(["hub", "export", str(out), "--passphrase", PW, "--home", str(src.home)])
    capsys.readouterr()
    bad = tmp_path / "bad.txt"
    bad.write_text(PW)
    os.chmod(bad, 0o644)
    assert main(["hub", "export", str(out), "--passphrase-file", str(bad), "--home", str(src.home)]) != 0 and not out.exists()
    assert main(["hub", "export", str(out), "--profile", "core", "--with-totp", "--passphrase-file", str(pwf), "--home", str(src.home)]) == 0
    text = capsys.readouterr().out
    assert "đã mã hoá" in text and "ngoài phần secret" in text
    dst_home = tmp_path / "userB" / ".cfharness"
    monkeypatch.setenv("HOME", str(tmp_path / "userB"))
    assert main(["hub", "import", str(out), "--home", str(dst_home), "--yes"]) == 1  # không TTY, không passphrase-file
    assert "--passphrase-file" in capsys.readouterr().err and not (dst_home / "hub.db").exists()
    wrong = tmp_path / "wrong.txt"
    wrong.write_text("một passphrase khác hẳn")
    os.chmod(wrong, 0o600)
    assert main(["hub", "import", str(out), "--home", str(dst_home), "--yes", "--passphrase-file", str(wrong)]) != 0
    assert not (dst_home / "hub.db").exists()
    assert main(["hub", "import", str(out), "--home", str(dst_home), "--dry-run", "--passphrase-file", str(pwf)]) == 0
    assert "passphrase đúng" in capsys.readouterr().out
    assert main(["hub", "import", str(out), "--home", str(dst_home), "--yes", "--passphrase-file", str(pwf)]) == 0
    text = capsys.readouterr().out
    assert "Secret đã nhập" in text and "bỏ theo chế độ clone" in text
    assert (dst_home / "secrets" / "CLAUDE_ACCT_WORK").read_text() == VALUES["CLAUDE_ACCT_WORK"]
    # passphrase không ở đâu: gói, mọi file của hai home, DB (audit/events/hub_state), log, đầu ra
    needle = PW.encode()
    assert needle not in out.read_bytes()
    for root in (src.home, dst_home):
        for p in root.rglob("*"):
            if p.is_file() and p != pwf:
                assert needle not in p.read_bytes(), p
        c = sqlite3.connect(str(root / "hub.db"))
        try:
            assert PW not in "\n".join(c.iterdump())
        finally:
            c.close()
    assert PW not in caplog.text


async def test_rpc_export_audit_drops_passphrase_param(hub_home):
    from m7_fake import MgrScript, close, make_daemon, rpc

    d = await make_daemon(hub_home, MgrScript())
    try:
        r = await rpc(d, "hub.export", {"profile": "core", "secrets": True, "passphrase": PW, "_audit": {"channel": "cli"}})
        assert Path(r["path"]).is_file()
        rows = [x["data"] for x in d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='hub.export'").fetchall()]
        assert rows and all(PW not in x for x in rows) and '"secrets"' in rows[0] and "passphrase" not in rows[0]
        assert PW not in "\n".join(d.hub.store.conn.iterdump())
    finally:
        await close(d)


# ============================================================================ clone: approval/câu hỏi đang chờ → cancelled
def _pending_source(tmp_path, monkeypatch) -> HubPaths:
    src, proj = make_source(tmp_path, monkeypatch)
    c = sqlite3.connect(str(src.db))
    with c:
        c.execute("INSERT INTO runs(id, agent_id, kind, prompt, status, queued_at, cwd) VALUES ('r_wait','a1','adhoc','x','waiting_human',"
                  "'2026-11-01T00:00:00+00:00',?)", (str(proj),))
        for aid, st in (("ap_open", "open"), ("ap_wait", "waiting_human"), ("ap_done", "allowed")):
            c.execute("INSERT INTO approval_requests(id, run_id, agent_id, tool_name, action_fingerprint, status, opened_at) "
                      "VALUES (?,?,?,?,?,?,?)", (aid, "r_wait", "a1", "Bash", "fp", st, "2026-11-01T00:00:00+00:00"))
        for qid, st in (("q_open", "open"), ("q_mgr", "with_manager"), ("q_done", "answered")):
            c.execute("INSERT INTO questions(id, agent_id, run_id, source, text, status, routed_to, created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (qid, "a1", "r_wait", "agent", "hỏi?", st, "human", "2026-11-01T00:00:00+00:00"))
    c.close()
    return src


def test_clone_cancels_pending_approvals_and_questions_move_keeps(tmp_path, monkeypatch):
    src = _pending_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])
    dst = dest(tmp_path, monkeypatch)
    plan = T.import_plan(dst, pkg, skip_secrets=True)
    assert plan["approvals_cancelled"] == 2 and plan["questions_cancelled"] == 2 and plan["runs_interrupted"] == 3
    r = T.import_package(dst, pkg, skip_secrets=True)
    assert r["approvals_cancelled"] == 2 and r["questions_cancelled"] == 2
    c = sqlite3.connect(str(dst.db))
    c.row_factory = sqlite3.Row
    ap = {x["id"]: x for x in c.execute("SELECT * FROM approval_requests")}
    assert ap["ap_open"]["status"] == ap["ap_wait"]["status"] == "cancelled" and ap["ap_done"]["status"] == "allowed"
    assert ap["ap_open"]["note"] == T.CLONE_PENDING_NOTE and ap["ap_open"]["resolved_by"] == "hub"
    qs = {x["id"]: x for x in c.execute("SELECT * FROM questions")}
    assert qs["q_open"]["status"] == qs["q_mgr"]["status"] == "cancelled" and qs["q_done"]["status"] == "answered"
    assert qs["q_open"]["escalation_note"] == T.CLONE_PENDING_NOTE
    assert c.execute("SELECT status FROM runs WHERE id='r_wait'").fetchone()[0] == "interrupted"
    assert c.execute("SELECT value FROM hub_state WHERE key='pending_resume'").fetchone() is None  # không tự resume
    assert "expired" not in {x["status"] for x in c.execute("SELECT status FROM approval_requests UNION SELECT status FROM questions")}
    aud = json.loads(c.execute("SELECT data FROM audit_log WHERE kind='hub.imported'").fetchone()[0])
    assert aud["approvals_cancelled"] == 2 and aud["questions_cancelled"] == 2
    c.close()
    dst2 = dest(tmp_path, monkeypatch, "userC")
    T.import_package(dst2, pkg, mode="move", skip_secrets=True)
    c = sqlite3.connect(str(dst2.db))
    assert {x[0] for x in c.execute("SELECT status FROM approval_requests WHERE id IN ('ap_open','ap_wait')")} == {"open", "waiting_human"}
    assert c.execute("SELECT status FROM runs WHERE id='r_wait'").fetchone()[0] == "waiting_human"
    c.close()


# ============================================================================ hoàn tác khi nhập lỗi giữa chừng
def _snapshot(home: Path) -> dict[str, bytes]:
    out = {}
    for rel, full in B.iter_included(home):
        out[str(rel)] = full.read_bytes()
    return out


def test_replace_failure_midway_restores_pre_restore(tmp_path, monkeypatch):
    src, _ = source(tmp_path, monkeypatch)
    pkg = sealed_pkg(src)
    dst = dest(tmp_path, monkeypatch)
    T.import_package(dst, pkg, passphrase=PW)
    (dst.home / "agents" / "a1" / "MEMORY.md").write_text("đổi ở máy B\n")
    (dst.home / "secrets" / "API_KEY").write_text("giá trị của máy B")
    c = sqlite3.connect(str(dst.db))
    with c:
        c.execute("INSERT INTO hub_state(key, value, updated_at) VALUES ('marker_b', '1', 't')")
    c.close()
    before_files = _snapshot(dst.home)
    before_secrets = {p.name: p.read_bytes() for p in (dst.home / "secrets").iterdir()}
    real = T._restore_dirs

    def boom(*a, **k):  # sau khi đã gỡ/ghi file + chuyển runs → giữa chừng
        real(*a, **k)
        raise OSError("đĩa đầy (giả)")

    monkeypatch.setattr(T, "_restore_dirs", boom)
    with pytest.raises(HubError, match="đã khôi phục hiện trạng") as ei:
        T.import_package(dst, pkg, replace=True, passphrase=PW)
    assert ei.value.data["rolled_back"] is True and ei.value.data["pre_restore"]
    assert _snapshot(dst.home) == before_files
    assert {p.name: p.read_bytes() for p in (dst.home / "secrets").iterdir()} == before_secrets
    c = sqlite3.connect(str(dst.db))
    assert c.execute("SELECT value FROM hub_state WHERE key='marker_b'").fetchone()[0] == "1"
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()
    assert not any(p.name.startswith((".import-", ".rollback-")) for p in dst.home.iterdir())
    monkeypatch.setattr(T, "_restore_dirs", real)

    # lỗi ở bước ghi secret (cuối cùng): secret đã ghi được hoàn tác + home về pre-restore
    from cfharness.secrets import SecretStore

    calls = {"n": 0}
    orig = SecretStore.set_bytes

    def flaky(self, name, data, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("ghi secret lỗi (giả)")
        return orig(self, name, data, **kw)

    monkeypatch.setattr(SecretStore, "set_bytes", flaky)
    with pytest.raises(HubError, match="đã khôi phục"):
        T.import_package(dst, pkg, replace=True, passphrase=PW)
    assert {p.name: p.read_bytes() for p in (dst.home / "secrets").iterdir()} == before_secrets
    assert _snapshot(dst.home) == before_files


def test_blank_home_failure_midway_leaves_home_blank(tmp_path, monkeypatch):
    src, _ = make_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="full")["path"])
    dst = dest(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise OSError("lỗi giả")

    monkeypatch.setattr(B, "rebaseline_offline", boom)
    with pytest.raises(HubError, match="trở lại trống") as ei:
        T.import_package(dst, pkg)
    assert ei.value.data["rolled_back"] is True and ei.value.data["pre_restore"] is None
    assert T.home_blank(dst) and not dst.db.exists() and not (dst.home / "runs" / "r_old.jsonl").exists()


# ============================================================================ làm lặng vòng nền khi xuất
def test_background_loops_skip_while_export_quiesced():
    from cfharness import backup, session_search
    from cfharness.curation.service import CurationService
    from cfharness.dispatch.dispatcher import TaskDispatcher
    from cfharness.knowledge.graph import embed
    from cfharness.learning.service import LearningService
    from cfharness.learning_ops.wiring import LearningOps
    from cfharness.manager.service import ManagerService
    from cfharness.runs import export_quiesced
    from cfharness.schedules.service import ScheduleService
    from cfharness.filehub.lane.service import LaneService

    assert export_quiesced(SimpleNamespace(runs=SimpleNamespace(quiesced="export")))
    assert not export_quiesced(SimpleNamespace(runs=SimpleNamespace(quiesced=None))) and not export_quiesced(SimpleNamespace())
    for fn in (LearningService.loop, LearningOps.loop, CurationService.loop, ScheduleService._loop, ManagerService._loop,
               TaskDispatcher._loop, embed.run_vector_loop, session_search.run_backfill, LaneService._maintenance_loop):
        assert "export_quiesced(" in inspect.getsource(fn), fn.__qualname__
    # PR5: sao lưu chỉ bỏ lượt khi ĐANG XUẤT (`exporting`), vẫn chạy khi hub đóng băng vì chuyển nhà (hub_freeze)
    assert "exporting(" in inspect.getsource(backup.BackupService._loop)


async def test_schedule_loop_does_not_tick_while_quiesced(monkeypatch):
    import asyncio

    from cfharness.schedules import service as sched

    ticks = []
    fake = SimpleNamespace(runs=SimpleNamespace(quiesced="export"))
    obj = SimpleNamespace(hub=fake, cfg=lambda k: 0, tick=lambda: ticks.append(1))
    real_sleep = asyncio.sleep
    n = {"i": 0}

    async def fast(_s):
        n["i"] += 1
        if n["i"] == 5:
            fake.runs.quiesced = None
        if n["i"] > 8:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(sched.asyncio, "sleep", fast)
    with pytest.raises(asyncio.CancelledError):
        await sched.ScheduleService._loop(obj)  # type: ignore[arg-type]
    assert len(ticks) == 4  # lượt 1–4 bỏ (đang xuất), 5–8 chạy
