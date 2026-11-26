"""Tài khoản runtime codex/grok — NHIỀU tài khoản mỗi runtime (runtime/accounts.py) + thẻ ở Cài đặt → Tài khoản AI.
Codex: `codex app-server` GIẢ (account/read, rateLimits, login/start). Grok: script giả in đúng dạng `grok models` / `grok login`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cfharness.runtime.accounts import MACHINE, RuntimeAccounts, account_env, active_account, candidates

HERE = Path(__file__).parent

GROK_FAKE = '''import os, sys, time
a = sys.argv[1:]
open(os.environ["FAKE_GROK_LOG"], "a").write(" ".join(a) + " HOME=" + os.environ.get("GROK_HOME", "") + "\\n")
if a == ["--version"]:
    print("grok 1.0.92 (fake)")
elif a == ["models"]:
    print("You are logged in with grok.com.\\n\\nDefault model: grok-4.7\\n\\nAvailable models:\\n  * grok-4.7 (default)")
elif a[:1] == ["login"]:
    print("To sign in, open this URL in your browser:\\n\\n  https://accounts.x.ai/oauth2/device?user_code=F65V-2Z9J\\n", flush=True)
    print("Confirm this code in your browser:\\n\\n  F65V-2Z9J\\n\\nWaiting for authorization...", flush=True)
    time.sleep(0.5)
    print("Signed in as someone")
elif a == ["logout"]:
    print("Signed out")
'''


class _Cfg:
    def __init__(self, d):
        self.d = d

    def get(self, k):
        return self.d.get(k)


def _hub(tmp_path: Path, cfg: dict) -> SimpleNamespace:
    return SimpleNamespace(config=_Cfg(cfg), paths=SimpleNamespace(home=tmp_path / "hub"))


def _bin(tmp_path: Path, script: Path, name: str) -> str:
    from conftest import fake_cli

    return str(fake_cli(tmp_path, script, name))


async def _until(pred, n: int = 80) -> None:
    for _ in range(n):
        if pred():
            return
        await asyncio.sleep(0.1)


def _codex(tmp_path: Path) -> RuntimeAccounts:
    return RuntimeAccounts(_hub(tmp_path, {"runtime.codex": {"binary": _bin(tmp_path, HERE / "fake_codex_appserver.py", "codex")}}))


def _grok(tmp_path: Path, monkeypatch) -> RuntimeAccounts:
    monkeypatch.setenv("FAKE_GROK_LOG", str(tmp_path / "g.log"))
    script = tmp_path / "grok_fake.py"
    script.write_text(GROK_FAKE, encoding="utf-8")
    return RuntimeAccounts(_hub(tmp_path, {"runtime.grok": {"binary": _bin(tmp_path, script, "grok")}}))


@pytest.mark.asyncio
async def test_list_returns_cache_immediately_and_checks_in_background(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "c.log"))
    acc = _codex(tmp_path)
    first = acc.list("codex")["codex"]
    a = first["accounts"][0]
    assert a["id"] == MACHINE and a["active"] and a["checking"] and a["logged_in"] is None  # chưa có cache: hiện ngay, kiểm ngầm
    await acc.wait_idle()
    a = acc.list("codex")["codex"]["accounts"][0]
    assert a["logged_in"] is True and a["email"] == "dev@example.com" and a["plan"] == "team" and a["workspace"] == "Acme"
    assert a["rate_limits"]["primary"]["used_percent"] == 12 and not a["checking"]
    # cache xuống đĩa: daemon mới đọc lại ngay, không phải kiểm
    acc2 = _codex(tmp_path)
    a2 = acc2.list("codex")["codex"]["accounts"][0]
    assert a2["email"] == "dev@example.com" and not a2["checking"]
    assert acc2.list("codex")["codex"]["version"] == "0.172.0"


@pytest.mark.asyncio
async def test_add_codex_account_uses_own_codex_home_and_use(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "c.log"))
    monkeypatch.setenv("FAKE_CODEX_LOGIN_OK", "1")
    acc = _codex(tmp_path)
    lg = await acc.login_start("codex", "device", label="Công ty")
    assert lg["code"] == "ABCD-EFGHI" and lg["new"] and lg["label"] == "Công ty"
    await _until(lambda: acc.logins["codex"]["state"] != "pending")
    assert acc.logins["codex"]["state"] == "done"
    ids = [a["id"] for a in acc.list("codex")["codex"]["accounts"]]
    assert ids[0] == MACHINE and len(ids) == 2 and ids[1].startswith("a_")
    new_id = ids[1]
    home = tmp_path / "hub" / "runtime-accounts" / "codex" / new_id
    assert home.is_dir()
    assert active_account(tmp_path / "hub", "codex") == MACHINE  # hub KHÔNG tự đổi
    acc.use("codex", new_id)
    assert active_account(tmp_path / "hub", "codex") == new_id
    assert account_env(tmp_path / "hub", "codex") == {"CODEX_HOME": str(home)}
    acc.rm("codex", new_id)
    assert not home.exists() and active_account(tmp_path / "hub", "codex") == MACHINE
    with pytest.raises(Exception):
        acc.rm("codex", MACHINE)


@pytest.mark.asyncio
async def test_cancelled_new_account_leaves_no_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "c.log"))
    acc = _codex(tmp_path)
    lg = await acc.login_start("codex", "browser", label="Tạm")
    assert lg["state"] == "pending" and lg["url"].startswith("https://auth.openai.com/oauth") and lg["code"] is None
    home = tmp_path / "hub" / "runtime-accounts" / "codex" / lg["account_id"]
    assert home.is_dir()
    r = await acc.login_cancel("codex")
    assert r["cancelled"] and not home.exists() and len(acc.list("codex")["codex"]["accounts"]) == 1
    assert any('"account/login/cancel"' in x for x in (tmp_path / "c.log").read_text(encoding="utf-8").splitlines())


@pytest.mark.asyncio
async def test_codex_logged_out(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOGGED_OUT", "1")
    acc = _codex(tmp_path)
    acc.refresh("codex")
    await acc.wait_idle()
    a = acc.list("codex")["codex"]["accounts"][0]
    assert a["logged_in"] is False and a["email"] is None


@pytest.mark.asyncio
async def test_grok_add_account_logs_into_own_home(tmp_path, monkeypatch):
    acc = _grok(tmp_path, monkeypatch)
    acc.refresh("grok")
    await acc.wait_idle()
    m = acc.list("grok")["grok"]["accounts"][0]
    assert m["logged_in"] is True and m["plan"] == "grok.com" and m["models"] == ["grok-4.7"]
    lg = await acc.login_start("grok", label="Cá nhân")
    assert lg["code"] == "F65V-2Z9J" and "user_code=F65V-2Z9J" in lg["url"]
    await _until(lambda: acc.logins["grok"]["state"] != "pending")
    assert acc.logins["grok"]["state"] == "done"
    new_id = acc.list("grok")["grok"]["accounts"][1]["id"]
    home = tmp_path / "hub" / "runtime-accounts" / "grok" / new_id
    log = (tmp_path / "g.log").read_text(encoding="utf-8")
    assert f"login --device-auth HOME={home}" in log  # grok login ghi auth.json vào thư mục RIÊNG của tài khoản
    acc.use("grok", new_id)
    assert account_env(tmp_path / "hub", "grok") == {"GROK_AUTH_PATH": str(home / "auth.json")}
    with pytest.raises(Exception):
        await acc.login_start("grok", "browser", label="x")  # grok chỉ có mã thiết bị
    await acc.logout("grok", new_id)
    await acc.wait_idle()


@pytest.mark.asyncio
async def test_missing_cli_and_bad_kind(tmp_path, monkeypatch):
    monkeypatch.setattr("cfharness.runtime.accounts.candidates", lambda k: [])
    acc = RuntimeAccounts(_hub(tmp_path, {"runtime.codex": {"binary": str(tmp_path / "nope.exe")}}))
    c = acc.list("codex")["codex"]
    assert c["binary"] is None and not c["accounts"][0]["checking"]  # không có CLI → không kiểm
    with pytest.raises(Exception):
        acc.list("gemini")
    with pytest.raises(Exception):
        await acc.login_start("codex", label="x")


def test_runtime_env_uses_active_account(tmp_path):
    """Run codex dùng CODEX_HOME của tài khoản Đang dùng; `machine` = không đặt (CLI tự dùng ~/.codex)."""
    from cfharness.config import DEFAULTS
    from cfharness.runtime import CodexRuntime, RunSpec

    hub = tmp_path / "hub"
    root = hub / "runtime-accounts"
    root.mkdir(parents=True)
    (root / "accounts.json").write_text(json.dumps({"codex": {"active": "a_12345678", "accounts": [{"id": "a_12345678", "label": "X"}]}}))
    rt = CodexRuntime({**DEFAULTS["runtime"]["codex"], "hub_home": str(hub)})
    spec = RunSpec(run_id="r", agent_id="a", prompt="p", cwd=tmp_path, session_id="s", raw_log_path=tmp_path / "r.jsonl",
                   stderr_log_path=tmp_path / "r.err")
    assert rt._env(spec)["CODEX_HOME"] == str(root / "codex" / "a_12345678")
    (root / "accounts.json").write_text(json.dumps({"codex": {"active": "machine", "accounts": []}}))
    assert rt._env(spec).get("CODEX_HOME") in (None, __import__("os").environ.get("CODEX_HOME"))


def test_candidates_are_existing_paths():
    for k in ("codex", "grok"):
        assert all(Path(p).exists() for p in candidates(k))


def test_runtime_account_methods_are_human_only():
    from cfharness import auth

    for m in ("runtime_account.list", "runtime_account.refresh", "runtime_account.detect", "runtime_account.login_start",
              "runtime_account.login_cancel", "runtime_account.logout", "runtime_account.use", "runtime_account.rm"):
        assert m in auth.HUMAN_ONLY


def test_template_login_field_is_not_named_method():
    """Ô form tên `method` che `form.method` → JS gắn khoá phiên lỗi → POST bị từ chối (lỗi đã gặp khi chạy thử)."""
    t = (Path(__file__).parents[1] / "src" / "cfharness" / "web" / "templates" / "_runtime_accounts.html").read_text(encoding="utf-8")
    assert '"method":' not in t and 'name="method"' not in t


@pytest.mark.asyncio
async def test_orphan_account_dirs_are_collected(tmp_path, monkeypatch):
    """Thư mục tài khoản dở dang còn sót (Windows: CLI con giữ file lúc huỷ) → `list` dọn; tài khoản thật và lượt đang mở thì giữ."""
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "c.log"))
    acc = _codex(tmp_path)
    root = tmp_path / "hub" / "runtime-accounts" / "codex"
    (root / "a_deadbeef" / ".tmp").mkdir(parents=True)
    (root / "not-an-account").mkdir()
    lg = await acc.login_start("codex", "browser", label="Đang mở")
    acc.list("codex")
    assert not (root / "a_deadbeef").exists() and (root / "not-an-account").exists() and (root / lg["account_id"]).exists()
    await acc.login_cancel("codex")
    acc.list("codex")
    assert not (root / lg["account_id"]).exists()
    await acc.wait_idle()


def test_rmtree_handles_readonly_files(tmp_path):
    """Pack git chỉ đọc (Codex clone plugin vào CODEX_HOME mới) không được làm sót thư mục tài khoản (đã gặp khi chạy thử Windows)."""
    import stat

    from cfharness.runtime.accounts import _rmtree

    d = tmp_path / "a_00000001" / ".tmp" / "plugins" / ".git" / "objects"
    d.mkdir(parents=True)
    f = d / "pack.idx"
    f.write_text("x")
    os_chmod = __import__("os").chmod
    os_chmod(f, stat.S_IREAD)
    _rmtree(tmp_path / "a_00000001")
    assert not (tmp_path / "a_00000001").exists()
