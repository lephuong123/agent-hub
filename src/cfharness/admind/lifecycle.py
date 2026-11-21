"""Vòng đời user hệ thống bằng job (FR-SA-USR-02..12; plan-superadmin §2.4 — port từ `scripts/setup-hub-user.sh`).

Mọi lệnh chạy qua `state.runner` (argv, danh sách trắng). Lệnh dưới uid user: `runner.cfh(u, ...)` / `runner.as_user(u, ...)`.
LaunchDaemon + thư mục chung dùng lại `systems.autostart.LaunchDaemonPlan` và `admin_users.AddPlan|RevokePlan` (in-process, root) —
admind KHÔNG chạy binary do user ghi được bằng quyền root.
Secret (token Claude, link TOTP) chỉ sống trong RAM của job / `state.extra["reveal"]`; không vào DB, log đã che.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .. import admin_control as AC
from .jobs import JobCtx, Step, StepError, kind

TOTP_LINK_RE = re.compile(r"(https?://\S+/enroll\?t=[A-Za-z0-9_\-]+)")
UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh"
CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"
REPO = "git+https://github.com/lephuong123/agent-hub@"


def home(u: str) -> str:
    return f"/Users/{u}"


def _ok(r: Any, what: str) -> Any:
    if not r.ok:
        raise StepError(f"{what} lỗi ({r.rc}): {(r.err or r.out).strip()[:300]}")
    return r


def _user_row(jc: JobCtx) -> dict[str, Any]:
    u = jc.state.db.one("SELECT * FROM hub_users WHERE id=?", (jc.params["user_id"],))
    if not u:
        raise StepError("không có bản ghi user")
    return u


def _u(jc: JobCtx) -> str:
    return str(jc.params["os_user"])


def _adapter2(runner: Any):  # LaunchDaemonPlan: runner(argv, timeout) -> (rc, out, err)
    def run(argv: list[str], timeout: float = 60) -> tuple[int, str, str]:
        r = runner.run(argv, timeout=timeout)
        return r.rc, r.out, r.err
    return run


def _adapter1(runner: Any):  # AddPlan/RevokePlan: runner(argv) -> (rc, out, err)
    def run(argv: list[str]) -> tuple[int, str, str]:
        r = runner.run(argv, timeout=60)
        return r.rc, r.out, r.err
    return run


def _launchd_dir(jc: JobCtx) -> Path:
    from ..systems.autostart import LAUNCH_DAEMONS_DIR

    return Path(jc.state.extra.get("launchd_dir") or LAUNCH_DAEMONS_DIR)


def _machine(jc: JobCtx) -> Any:
    from .machine import machine

    return machine(jc.state)


# ============================================================================ bước tạo user
class OsUserStep(Step):
    name, title = "os_user", "Tạo user macOS (ẩn khỏi màn hình đăng nhập), home 0700, thư mục Workspace"

    def check(self, jc: JobCtx) -> bool:
        return _machine(jc).os_user_exists(_u(jc)) and jc.data.get("home_ok") is True

    def apply(self, jc: JobCtx) -> None:
        u, r = _u(jc), jc.runner
        if not _machine(jc).os_user_exists(u):
            pw = secrets.token_urlsafe(24)  # không lưu: user hub không bao giờ đăng nhập màn hình
            jc.secrets["_os_pw"] = pw
            _ok(r.run(["sysadminctl", "-addUser", u, "-fullName", jc.params.get("full_name") or u, "-password", pw, "-home", home(u)],
                      timeout=120), "sysadminctl -addUser")
            jc.secrets.pop("_os_pw", None)
            r.run(["createhomedir", "-c", "-u", u], timeout=120)
            r.run(["dscl", ".", "create", f"/Users/{u}", "IsHidden", "1"], timeout=30)
            jc.data["created_os_user"] = True
            jc.log(f"đã tạo user {u}")
        _ok(r.run(["chmod", "700", home(u)], timeout=30), "chmod home")
        _ok(r.as_user(u, ["mkdir", "-p", f"{home(u)}/{jc.params.get('workspace', 'Workspace')}"], timeout=30), "mkdir Workspace")
        jc.data["home_ok"] = True

    def undo(self, jc: JobCtx) -> None:
        if jc.data.get("created_os_user"):
            jc.runner.run(["sysadminctl", "-deleteUser", _u(jc), "-secure"], timeout=300)
            jc.log(f"đã xoá user {_u(jc)} do job tạo")


class InstallStep(Step):
    name, title = "install", "Cài uv, claude, cfharness (bản cài riêng của user)"

    def check(self, jc: JobCtx) -> bool:
        u = _u(jc)
        if not all(os.path.exists(f"{home(u)}/.local/bin/{b}") for b in ("uv", "claude", "cfharness")) and not jc.state.extra.get("fake_fs"):
            return False
        r = jc.runner.cfh(u, "--version", timeout=60)
        return r.ok and jc.params["version"].lstrip("v") in r.out

    def apply(self, jc: JobCtx) -> None:
        u, r = _u(jc), jc.runner
        if not os.path.exists(f"{home(u)}/.local/bin/uv"):
            _ok(r.as_user(u, ["/bin/sh", "-c", UV_INSTALL], timeout=600), "cài uv")
        if not os.path.exists(f"{home(u)}/.local/bin/claude"):
            _ok(r.as_user(u, ["/bin/bash", "-c", CLAUDE_INSTALL], timeout=600), "cài claude")
        _ok(r.as_user(u, [f"{home(u)}/.local/bin/uv", "tool", "install", "--force", "--python", "3.13", REPO + jc.params["version"]],
                      timeout=900), "cài cfharness")
        jc.log(f"cfharness {jc.params['version']} đã cài cho {u}")


class ClaudeTokenStep(Step):
    name, title = "claude_token", "Nạp token Claude vào secret store của hub (qua stdin) và gọi thử"

    def apply(self, jc: JobCtx) -> None:
        u = _u(jc)
        tok = jc.secrets.get("claude_token")
        if not tok:
            raise StepError("thiếu token Claude (admind đã khởi động lại?) — bấm Thử lại và dán lại token")
        if not tok.startswith("sk-ant-"):
            raise StepError("token không đúng dạng sk-ant-…")
        _ok(jc.runner.cfh(u, "secret", "set", "CLAUDE_CODE_OAUTH_TOKEN", "--value-stdin", stdin=tok, timeout=60), "lưu token")
        # gọi thử: token đi qua stdin vào biến môi trường của tiến trình con — không nằm trong argv (`ps`)
        script = ('IFS= read -r T; cd "$HOME/' + jc.params.get("workspace", "Workspace") + '" && CLAUDE_CODE_OAUTH_TOKEN="$T" exec '
                  '"$HOME/.local/bin/claude" -p --max-turns 1 --setting-sources "" "Reply with just: OK"')
        r = jc.runner.as_user(u, ["/bin/sh", "-c", script], stdin=tok + "\n", timeout=120)
        if "OK" not in (r.out or ""):
            raise StepError("Claude từ chối token (dán thiếu? hết hạn?): " + jc.mask((r.err or r.out).strip()[-160:]))
        jc.log("đã lưu token — claude trả lời OK")


class ConfigStep(Step):
    name, title = "config", "Cấu hình hub: cổng MCP, giam workspace, địa chỉ public, bật xuất trạng thái"

    def apply(self, jc: JobCtx) -> None:
        u, p = _u(jc), jc.params
        ws = f"{home(u)}/{p.get('workspace', 'Workspace')}"
        sets = [("mcp_server.http_port", str(p["mcp_port"])), ("workspace.allowed_roots", f'["{ws}"]'), ("workspace.confine.root", ws),
                ("systems.roots", f'["{ws}"]'), ("web.client_ip_header", "cf-connecting-ip"), ("admin_export.enabled", "true")]
        if p.get("fqdn"):
            sets.append(("web.public_origin", f"https://{p['fqdn']}"))
        for k, v in sets:
            _ok(jc.runner.cfh(u, "config", "set", k, v, timeout=60), f"config set {k}")
        jc.log(f"cổng MCP {p['mcp_port']} · agent chỉ làm trong {ws}")


class LaunchdStep(Step):
    name, title = "launchd", "LaunchDaemon (daemon + web) — tự chạy khi máy khởi động"

    def check(self, jc: JobCtx) -> bool:
        d = _launchd_dir(jc)
        u = _u(jc)
        return (d / f"com.cfharness.daemon.{u}.plist").exists() and (d / f"com.cfharness.web.{u}.plist").exists() and \
            bool(jc.data.get("launchd_ok"))

    def apply(self, jc: JobCtx) -> None:
        from ..systems.autostart import LaunchDaemonPlan

        u = _u(jc)
        jc.runner.cfh(u, "daemon", "stop", timeout=60)
        plan = LaunchDaemonPlan(u, user_home=Path(home(u)), web_port=int(jc.params["web_port"]), runner=_adapter2(jc.runner),
                                directory=_launchd_dir(jc))
        try:
            for line in plan.apply(euid=0):
                jc.log(line)
        except Exception as e:  # noqa: BLE001
            raise StepError(f"cài LaunchDaemon lỗi: {e}") from None
        jc.data["launchd_ok"] = True

    def undo(self, jc: JobCtx) -> None:
        from ..systems.autostart import LaunchDaemonPlan

        u = _u(jc)
        try:
            LaunchDaemonPlan(u, user_home=Path(home(u)), web_port=int(jc.params["web_port"]), runner=_adapter2(jc.runner),
                             directory=_launchd_dir(jc)).remove(euid=0)
        except Exception as e:  # noqa: BLE001
            jc.log(f"gỡ LaunchDaemon lỗi: {e}")


class ManagerStep(Step):
    name, title = "manager", "Điều phối viên (agent dieu-phoi, mẫu manager)"

    def check(self, jc: JobCtx) -> bool:
        if not jc.params.get("create_manager", True):
            return True
        r = jc.runner.cfh(_u(jc), "manager", "get", "--json", timeout=60)
        return r.ok and '"agent_id": "' in r.out.replace('":"', '": "')

    def apply(self, jc: JobCtx) -> None:
        u = _u(jc)
        if not jc.runner.cfh(u, "agent", "show", "dieu-phoi", timeout=60).ok:
            _ok(jc.runner.cfh(u, "agent", "create", "dieu-phoi", "--template", "manager", "--name", "Điều phối viên", timeout=120), "tạo agent")
        _ok(jc.runner.cfh(u, "manager", "set", "dieu-phoi", timeout=60), "chỉ định điều phối viên")
        jc.log("điều phối viên: dieu-phoi")


class SharedDirsStep(Step):
    name, title = "shared_dirs", "Nhóm cfharness-users + thư mục chung status/audit/report của user"

    def apply(self, jc: JobCtx) -> None:
        from ..admin_users import AddPlan

        u = _u(jc)
        plan = AddPlan(user=u, status_dir=AC.shared_root() / "status", runner=_adapter1(jc.runner)).build(install_daemon=False)
        rep = AC.shared_root() / "report" / u
        plan.steps += [(f"mkdir {rep}", ["/bin/mkdir", "-p", str(rep)]), (f"chown {rep}", ["/usr/sbin/chown", f"{u}:{AC.ADMIN_GROUP}", str(rep)]),
                       (f"chmod {rep}", ["/bin/chmod", "2750", str(rep)])]
        try:
            for line in plan.apply(euid=0):
                jc.log(line)
        except Exception as e:  # noqa: BLE001
            raise StepError(str(e)) from None


class DomainStep(Step):
    name, title = "domain", "Tên miền: DNS (đúng zone) + ingress tunnel"

    def check(self, jc: JobCtx) -> bool:
        if not jc.params.get("domain_id"):
            return True
        from .domain_steps import DnsCreate, IngressAdd

        return DnsCreate().check(jc) and IngressAdd().check(jc)

    def apply(self, jc: JobCtx) -> None:
        from .domain_steps import DnsCreate, IngressAdd

        for s in (DnsCreate(), IngressAdd()):
            if not s.check(jc):
                s.apply(jc)

    def undo(self, jc: JobCtx) -> None:
        if not jc.params.get("domain_id"):
            return
        from .domain_steps import DnsDelete, IngressRemove

        for s in (IngressRemove(), DnsDelete()):
            try:
                s.apply(jc)
            except Exception as e:  # noqa: BLE001
                jc.log(f"dọn {s.name} lỗi: {e}")


class KgsStep(Step):
    name, title = "kgs", "Không gian số (tuỳ chọn)"

    def check(self, jc: JobCtx) -> bool:
        return not jc.params.get("kgs_url")

    def apply(self, jc: JobCtx) -> None:
        jc.log("Nối Không gian số cần khoá cổng của chính user: user đăng nhập hub → Cài đặt → Thư mục chung (hoặc "
               f"`sudo -u {_u(jc)} -i cfharness filehub setup`). Bỏ qua bước này trong job.")


class DoctorStep(Step):
    name, title = "doctor", "Chẩn đoán hub + kiểm web cục bộ"

    def apply(self, jc: JobCtx) -> None:
        u = _u(jc)
        r = jc.runner.cfh(u, "doctor", timeout=180)
        jc.log((r.out or "").strip()[-1500:])
        if not r.ok:
            jc.log("doctor có mục chưa xanh (xem log trên) — không chặn")
        probe = jc.state.extra.get("local_probe")
        if probe is not None and not probe(int(jc.params["web_port"]), jc.params.get("fqdn") or "localhost"):
            raise StepError(f"web :{jc.params['web_port']} chưa lên — xem {home(u)}/.cfharness/logs/web.out")
        if jc.params.get("domain_id"):
            from .domain_steps import HttpsCheck

            try:
                HttpsCheck().apply(jc)
            except StepError as e:
                jc.log(f"chưa truy cập được từ ngoài: {e} (kiểm lại ở trang Tên miền sau vài phút)")


class TotpStep(Step):
    name, title = "totp", "Tạo link đăng ký TOTP cho user (hiện MỘT lần)"

    def apply(self, jc: JobCtx) -> None:
        r = _ok(jc.runner.cfh(_u(jc), "web", "totp", "enroll", timeout=60), "totp enroll")
        m = TOTP_LINK_RE.search(r.out or "")
        if not m:
            raise StepError("không đọc được link đăng ký TOTP")
        put_reveal(jc.state, jc.job_id, m.group(1))
        jc.log("đã tạo link đăng ký TOTP (xem ở trang job — hiện một lần)")


def put_reveal(state: Any, key: str, value: str, ttl_s: float = 24 * 3600) -> None:
    store = state.extra.setdefault("reveal", {})
    store[key] = (value, state.clock() + ttl_s)


def take_reveal(state: Any, key: str) -> str | None:
    v = state.extra.setdefault("reveal", {}).pop(key, None)
    if not v or v[1] < state.clock():
        return None
    return v[0]


class FinishCreate(Step):
    name, title = "finish", "Kích hoạt user trong SAC"

    def apply(self, jc: JobCtx) -> None:
        jc.state.db.exec("UPDATE hub_users SET state='active' WHERE id=?", (jc.params["user_id"],))
        for t in jc.params.get("teams") or []:
            jc.state.db.exec("INSERT OR IGNORE INTO team_members(team_id, user_id, role, joined_at) VALUES (?,?, 'member', ?)",
                             (t, jc.params["user_id"], jc.state.clock()))
        AC.admin_log_append(_u(jc), "user_add", by="sac")


def _on_create_fail(jc: JobCtx) -> None:
    jc.state.db.exec("UPDATE hub_users SET state='create_failed' WHERE id=? AND state='creating'", (jc.params["user_id"],))


@kind("user.create")
def k_create(params: dict[str, Any]) -> list[Step]:
    return [OsUserStep(), InstallStep(), ClaudeTokenStep(), ConfigStep(), LaunchdStep(), ManagerStep(), SharedDirsStep(), DomainStep(),
            KgsStep(), DoctorStep(), TotpStep(), FinishCreate()]


k_create.on_failure = _on_create_fail  # type: ignore[attr-defined]


def _on_create_cancel(jc: JobCtx) -> None:
    jc.state.db.exec("UPDATE hub_users SET state='deleted' WHERE id=? AND state IN ('creating','create_failed')", (jc.params["user_id"],))
    if jc.params.get("domain_id"):
        jc.state.db.exec("UPDATE domains SET state='revoked' WHERE id=?", (jc.params["domain_id"],))


k_create.on_cancel = _on_create_cancel  # type: ignore[attr-defined]


# ============================================================================ tạm khoá / mở khoá (USR-07, 08)
class EstopStep(Step):
    def __init__(self, on: bool) -> None:
        self.on = on
        self.name, self.title = ("estop_on", "Bật ESTOP quản trị (dừng run)") if on else ("estop_off", "Gỡ ESTOP quản trị")

    def apply(self, jc: JobCtx) -> None:
        AC.set_flag(self.on, _u(jc), reason_code=jc.params.get("reason_code") or "other", by="sac")


class WebStop(Step):
    """Huỷ mọi phiên web + tắt web (ingress trả lỗi → Cloudflare báo hub không truy cập được)."""

    def __init__(self, stop: bool) -> None:
        self.stop = stop
        self.name, self.title = ("web_stop", "Huỷ phiên web + dừng web của hub") if stop else ("web_start", "Chạy lại web của hub")

    def apply(self, jc: JobCtx) -> None:
        u = _u(jc)
        label = f"system/com.cfharness.web.{u}"
        if self.stop:
            jc.runner.cfh(u, "web", "devices", "revoke-all", timeout=60)
            jc.runner.run(["launchctl", "disable", label], timeout=30)
            jc.runner.run(["launchctl", "bootout", label], timeout=30)
        else:
            jc.runner.run(["launchctl", "enable", label], timeout=30)
            plist = _launchd_dir(jc) / f"com.cfharness.web.{u}.plist"
            jc.runner.run(["launchctl", "bootstrap", "system", str(plist)], timeout=30)
            jc.runner.run(["launchctl", "kickstart", "-k", label], timeout=30)


class SetUserState(Step):
    def __init__(self, st: str) -> None:
        self.st = st
        self.name, self.title = f"state_{st}", f"Trạng thái SAC: {st}"

    def apply(self, jc: JobCtx) -> None:
        extra = ", revoked_at=?" if self.st == "revoked" else (", revoked_at=NULL" if self.st == "active" else "")
        args: tuple = (self.st, jc.state.clock(), jc.params["user_id"]) if self.st == "revoked" else (self.st, jc.params["user_id"])
        jc.state.db.exec(f"UPDATE hub_users SET state=?{extra} WHERE id=?", args)


@kind("user.suspend")
def k_suspend(params: dict[str, Any]) -> list[Step]:
    return [EstopStep(True), WebStop(True), SetUserState("suspended")]


@kind("user.resume")
def k_resume(params: dict[str, Any]) -> list[Step]:
    return [WebStop(False), EstopStep(False), SetUserState("active")]


# ============================================================================ thu hồi / khôi phục (USR-09, 11)
class RevokeOs(Step):
    name, title = "revoke_os", "Cờ ESTOP 'revoked' → bootout LaunchDaemon → gỡ nhóm cfharness-users (HUB_HOME giữ nguyên)"

    def apply(self, jc: JobCtx) -> None:
        from ..admin_users import RevokePlan

        try:
            for line in RevokePlan(user=_u(jc), status_dir=AC.shared_root() / "status", runner=_adapter1(jc.runner)).build().apply(euid=0):
                jc.log(line)
        except Exception as e:  # noqa: BLE001
            raise StepError(str(e)) from None


class RevokeDomains(Step):
    name, title = "revoke_domains", "Thu hồi tên miền của hub"

    def apply(self, jc: JobCtx) -> None:
        from .domain_steps import AccessRemove, DnsDelete, IngressRemove

        for d in jc.state.db.all("SELECT id FROM domains WHERE hub_user_id=? AND state NOT IN ('revoked')", (jc.params["user_id"],)):
            jc.params["domain_id"] = d["id"]
            for s in (IngressRemove(), DnsDelete(), AccessRemove()):
                if not s.check(jc):
                    s.apply(jc)
            jc.state.db.exec("UPDATE domains SET state='revoked' WHERE id=?", (d["id"],))
        jc.params.pop("domain_id", None)


class TotpDisable(Step):
    name, title = "totp_disable", "Vô hiệu TOTP + thiết bị tin cậy"

    def apply(self, jc: JobCtx) -> None:
        jc.runner.cfh(_u(jc), "web", "totp", "disable", timeout=60)
        jc.runner.cfh(_u(jc), "web", "devices", "revoke-all", timeout=60)
        jc.log("Nhắc: thu hồi bot Telegram/Slack của user (nếu có) trên BotFather / Slack.")


@kind("user.revoke")
def k_revoke(params: dict[str, Any]) -> list[Step]:
    return [RevokeOs(), RevokeDomains(), TotpDisable(), SetUserState("revoked")]


class RestoreOs(Step):
    name, title = "restore_os", "Vào lại nhóm, LaunchDaemon, gỡ cờ revoked"

    def apply(self, jc: JobCtx) -> None:
        u = _u(jc)
        _ok(jc.runner.run(["/usr/sbin/dseditgroup", "-o", "edit", "-a", u, "-t", "user", AC.USERS_GROUP], timeout=60), "dseditgroup")
        h = jc.state.db.one("SELECT web_port FROM hubs WHERE user_id=?", (jc.params["user_id"],))
        jc.params["web_port"] = (h or {}).get("web_port") or jc.params.get("web_port")
        if not jc.params.get("web_port"):
            raise StepError("không biết cổng web của hub")
        LaunchdStep().apply(jc)
        AC.set_flag(False, u, by="sac")


@kind("user.restore")
def k_restore(params: dict[str, Any]) -> list[Step]:
    steps: list[Step] = [RestoreOs()]
    if params.get("domain_id"):
        from .domain_steps import create_steps

        steps += create_steps()
    return [*steps, TotpStep(), SetUserState("active")]


# ============================================================================ sao lưu + xoá hẳn (USR-10, 12)
class BackupHome(Step):
    name, title = "backup", "Sao lưu /Users/<u> (nén + mã hoá) vào kho sao lưu quản trị, giữ 90 ngày"

    def check(self, jc: JobCtx) -> bool:
        p = jc.data.get("backup_path")
        return bool(p) and os.path.exists(p)

    def apply(self, jc: JobCtx) -> None:
        u = _u(jc)
        d = jc.state.paths.backups
        d.mkdir(parents=True, exist_ok=True)
        tar = d / f"{u}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        _ok(jc.runner.run(["tar", "-czf", str(tar), "-C", "/Users", u], timeout=7200), "nén home")
        key = secrets.token_urlsafe(32)
        jc.state.vault.set(f"backup.{tar.stem.replace('.tar', '')[:60]}", key)
        enc = tar.with_suffix(".gz.enc")
        _ok(jc.runner.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-in", str(tar), "-out", str(enc), "-pass", "stdin"],
                          stdin=key + "\n", timeout=7200), "mã hoá")
        jc.runner.run(["/bin/rm", "-f", str(tar)], timeout=60)
        jc.data["backup_path"] = str(enc)
        jc.log(f"sao lưu: {enc}")


class VerifyBackup(Step):
    name, title = "verify_backup", "Xác minh bản sao lưu đọc được"

    def apply(self, jc: JobCtx) -> None:
        p = jc.data.get("backup_path")
        if not p or (not os.path.exists(p) and not jc.state.extra.get("fake_fs")):
            raise StepError("không có bản sao lưu — không xoá")


class DeleteOsUser(Step):
    name, title = "delete_os_user", "Xoá user macOS + dọn thư mục chung"

    def apply(self, jc: JobCtx) -> None:
        u = _u(jc)
        _ok(jc.runner.run(["sysadminctl", "-deleteUser", u, "-secure"], timeout=900), "sysadminctl -deleteUser")
        for sub in ("status", "audit", "report"):
            jc.runner.run(["/bin/rm", "-rf", str(AC.shared_root() / sub / u)], timeout=120)
        for t in jc.state.db.all("SELECT slug FROM teams"):
            jc.runner.run(["/bin/rm", "-rf", str(AC.shared_root() / "teams" / t["slug"] / "inbox" / u)], timeout=60)
        jc.state.db.exec("DELETE FROM team_members WHERE user_id=?", (jc.params["user_id"],))


@kind("user.delete")
def k_delete(params: dict[str, Any]) -> list[Step]:
    return [BackupHome(), VerifyBackup(), DeleteOsUser(), SetUserState("deleted")]


@kind("user.backup")
def k_backup(params: dict[str, Any]) -> list[Step]:
    return [BackupHome(), VerifyBackup()]
