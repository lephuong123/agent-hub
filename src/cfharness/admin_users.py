"""Quản lý user của máy nhiều người dùng (plan-admin-users §3; chi tiết `docs/dev-notes/admin-users.md`).

- `list_users`: ghép thành viên nhóm `cfharness-users`, plist LaunchDaemon `com.cfharness.daemon.<u>.plist`, thư mục đã cấp
  `status/<u>/`, file trạng thái → trạng thái mỗi user (`USER_STATES`).
- `setup_check`: kiểm nhóm + thư mục chung (chủ/nhóm/quyền) → danh sách {id, level ok|warn|fail, detail, fix}.
- `AddPlan`/`RevokePlan`: in lệnh sudo (mặc định); `--write` (root) chạy qua `runner` (test thay được — không gọi sudo/dseditgroup thật).
Mọi hàm nhận hook (`getgrnam`, `lstat`, `owner_of`, `runner`) để test không phụ thuộc máy.
"""

from __future__ import annotations

import os
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .admin_control import (ADMIN_GROUP, USER_RE, USERS_GROUP, admin_log_append, admin_log_dir, control_dir, flag_path, group_gid,
                            group_members, policy_dir, shared_root)

PLIST_PREFIX = "com.cfharness.daemon."
LAUNCH_DAEMONS_DIR = Path("/Library/LaunchDaemons")
# trạng thái user (enum — hiển thị tiếng Việt ở CLI/web)
USER_STATES = {
    "not_installed": "chưa cài",
    "installed_no_export": "đã cài, chưa xuất",
    "ok": "ổn",
    "stale": "quá hạn",
    "stopped": "đã dừng",
    "bad_file": "file lỗi / sai chủ",
    "policy_mismatch": "sai chính sách",
    "revoked_running": "bị thu quyền còn chạy",
}
Runner = Callable[[list[str]], tuple[int, str, str]]


def _owner_name(uid: int) -> str | None:
    from .admin_overview import _owner_name as f

    return f(uid)


def installed_plists(directory: Path = LAUNCH_DAEMONS_DIR) -> set[str]:
    try:
        names = os.listdir(directory)
    except OSError:
        return set()
    out = set()
    for n in names:
        if n.startswith(PLIST_PREFIX) and n.endswith(".plist"):
            u = n[len(PLIST_PREFIX):-len(".plist")]
            if USER_RE.match(u):
                out.add(u)
    return out


def user_state(hubs: list[dict[str, Any]], *, member: bool, plist: bool, expected_rev: Any = None) -> str:
    """Gộp trạng thái các hub của một user thành một mã `USER_STATES`."""
    live = [h for h in hubs if h.get("state") == "ok"]
    if not member and live and hubs:
        return "revoked_running"
    if not hubs:
        return "installed_no_export" if plist else "not_installed"
    if any(h.get("state") in ("malformed", "owner_mismatch") for h in hubs):
        return "bad_file"
    if any(h.get("state") == "stale" for h in hubs):
        return "stale"
    for h in live:
        pol = (h.get("data") or {}).get("policy") or {}
        if pol.get("valid") is False or (expected_rev is not None and pol.get("rev_applied") != expected_rev):
            return "policy_mismatch"
    if not live:
        return "stopped"
    return "ok"


def list_users(ov: dict[str, Any], *, plist_dir: Path = LAUNCH_DAEMONS_DIR, members: list[str] | None = None,
               expected_rev: Callable[[str], Any] | None = None) -> list[dict[str, Any]]:
    """`ov` = `admin_overview.read_dir(...)`. Trả mỗi user một dòng (đã sắp theo tên)."""
    mem = set(group_members(USERS_GROUP) if members is None else members)
    plists = installed_plists(plist_dir)
    by: dict[str, list[dict[str, Any]]] = {}
    for h in ov.get("hubs") or []:
        by.setdefault(str(h.get("user")), []).append(h)
    granted = set(ov.get("user_dirs") or [])
    names = sorted(mem | plists | granted | set(by))
    out = []
    for u in names:
        hubs = by.get(u, [])
        exp = expected_rev(u) if expected_rev else None
        st = user_state(hubs, member=u in mem, plist=u in plists, expected_rev=exp)
        cost_today = sum(float(((h.get("data") or {}).get("cost_usd") or {}).get("today") or 0) for h in hubs)
        cost_month = sum(float(((h.get("data") or {}).get("cost_usd") or {}).get("month") or 0) for h in hubs)
        running = sum(int(((h.get("data") or {}).get("runs") or {}).get("running") or 0) for h in hubs if h.get("state") == "ok")
        out.append({"user": u, "state": st, "state_vi": USER_STATES[st], "member": u in mem, "plist": u in plists, "granted": u in granted,
                    "hubs": len(hubs), "cost_today_usd": round(cost_today, 6), "cost_month_usd": round(cost_month, 6), "running": running,
                    "version": next((str((h.get("data") or {}).get("version")) for h in hubs if (h.get("data") or {}).get("version")), None)})
    return out


# ============================================================================ setup check
@dataclass
class DirSpec:
    rel: str
    owner: str  # "root" | "<user>"
    group: str  # "wheel" | ADMIN_GROUP
    mode: int
    why: str
    legacy_modes: tuple[int, ...] = ()


def dir_specs() -> list[DirSpec]:
    return [
        DirSpec("", "root", "wheel", 0o755, "gốc thư mục chung"),
        DirSpec("status", "root", ADMIN_GROUP, 0o751, "trạng thái hub: admin liệt kê, user chỉ đi vào thư mục riêng", (0o2770, 0o3773)),
        DirSpec("audit", "root", ADMIN_GROUP, 0o751, "audit metadata (P2)"),
        DirSpec("control", "root", ADMIN_GROUP, 0o2775, "cờ ESTOP admin: nhóm admin ghi, mọi user đọc"),
        DirSpec("policy", "root", "wheel", 0o755, "chính sách: chỉ root ghi"),
        DirSpec("admin-log", "root", ADMIN_GROUP, 0o2775, "nhật ký thao tác admin: user đọc được"),
    ]


def _mkdir_cmds(path: str, owner: str, group: str, mode: int) -> list[str]:
    q = shlex.quote(path)
    return [f"sudo mkdir -p {q}", f"sudo chown {owner}:{group} {q}", f"sudo chmod {mode:o} {q}"]


def setup_check(*, me: str | None = None, lstat: Callable[[str], os.stat_result] = os.lstat,
                gid_name: Callable[[int], str | None] | None = None, admin: bool | None = None) -> dict[str, Any]:
    """Kiểm nhóm + thư mục chung. Không cần quyền đặc biệt; không sửa gì — chỉ đề xuất lệnh sửa (`fix`)."""
    from .admin_control import is_admin
    from .admin_overview import os_user

    me = me or os_user()
    root = str(shared_root())
    checks: list[dict[str, Any]] = []

    def add(cid: str, level: str, detail: str, fix: list[str] | None = None) -> None:
        checks.append({"id": cid, "level": level, "detail": detail, "fix": fix or []})

    def _gname(gid: int) -> str | None:
        if gid_name is not None:
            return gid_name(gid)
        try:
            import grp

            return grp.getgrgid(gid).gr_name
        except (KeyError, ImportError):
            return None

    for g, why in ((ADMIN_GROUP, "người quản trị máy"), (USERS_GROUP, "người được dùng hub")):
        if group_gid(g) is None:
            add(f"group:{g}", "fail", f"chưa có nhóm {g} ({why})", [f'sudo dseditgroup -o create -r "cfharness {why}" {g}'])
        else:
            add(f"group:{g}", "ok", f"nhóm {g} có ({len(group_members(g))} thành viên khai báo)")
    both = sorted(set(group_members(ADMIN_GROUP)) & set(group_members(USERS_GROUP)))
    if both:
        add("group:overlap", "warn", f"user vừa ở {USERS_GROUP} vừa ở {ADMIN_GROUP}: {', '.join(both)} — người dùng thường không nên ở nhóm admin",
            [f"sudo dseditgroup -o edit -d {shlex.quote(u)} -t user {ADMIN_GROUP}" for u in both])
    adm = is_admin() if admin is None else admin
    add("me:admin", "ok" if adm else "warn",
        f"tiến trình này ({me}) {'thuộc' if adm else 'KHÔNG thuộc'} nhóm {ADMIN_GROUP}" + ("" if adm else " — đăng xuất/khởi động lại daemon sau khi thêm vào nhóm (InitGroups)"))
    for spec in dir_specs():
        p = os.path.join(root, spec.rel) if spec.rel else root
        try:
            st = lstat(p)
        except FileNotFoundError:
            add(f"dir:{spec.rel or '/'}", "fail", f"{p} chưa có ({spec.why})", _mkdir_cmds(p, spec.owner, spec.group, spec.mode))
            continue
        except OSError as e:
            add(f"dir:{spec.rel or '/'}", "fail", f"{p}: không stat được ({e})")
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            add(f"dir:{spec.rel or '/'}", "fail", f"{p} không phải thư mục thật (symlink/file)", [f"sudo rm -f {shlex.quote(p)}"] + _mkdir_cmds(p, spec.owner, spec.group, spec.mode))
            continue
        mode = stat.S_IMODE(st.st_mode)
        owner = "root" if st.st_uid == 0 else (_owner_name(st.st_uid) or str(st.st_uid))
        group = _gname(st.st_gid) or str(st.st_gid)
        bad = []
        if owner != spec.owner:
            bad.append(f"chủ {owner} (cần {spec.owner})")
        if group != spec.group:
            bad.append(f"nhóm {group} (cần {spec.group})")
        if mode != spec.mode:
            bad.append(f"quyền {mode:o} (cần {spec.mode:o})")
        if not bad:
            add(f"dir:{spec.rel or '/'}", "ok", f"{p} {owner}:{group} {mode:o}")
        else:
            level = "warn" if mode in spec.legacy_modes else "fail"
            hint = " — bố cục Q6 cũ, xem runbook chuyển đổi" if mode in spec.legacy_modes else ""
            add(f"dir:{spec.rel or '/'}", level, f"{p}: " + ", ".join(bad) + hint,
                [f"sudo chown {spec.owner}:{spec.group} {shlex.quote(p)}", f"sudo chmod {spec.mode:o} {shlex.quote(p)}"])
    # thư mục riêng từng user trong status/ (chủ = user, nhóm admin, 2750)
    sd = os.path.join(root, "status")
    try:
        subs = sorted(n for n in os.listdir(sd) if USER_RE.match(n))
    except OSError:
        subs = []
    for u in subs:
        p = os.path.join(sd, u)
        try:
            st = lstat(p)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        owner = _owner_name(st.st_uid) or str(st.st_uid)
        mode = stat.S_IMODE(st.st_mode)
        group = _gname(st.st_gid) or str(st.st_gid)
        ok = owner == u and mode == 0o2750 and group == ADMIN_GROUP
        add(f"user_dir:{u}", "ok" if ok else "fail", f"{p} {owner}:{group} {mode:o}" + ("" if ok else f" (cần {u}:{ADMIN_GROUP} 2750)"),
            [] if ok else [f"sudo chown {shlex.quote(u)}:{ADMIN_GROUP} {shlex.quote(p)}", f"sudo chmod 2750 {shlex.quote(p)}"])
    worst = "fail" if any(c["level"] == "fail" for c in checks) else ("warn" if any(c["level"] == "warn" for c in checks) else "ok")
    return {"root": root, "ok": worst == "ok", "level": worst, "checks": checks}


# ============================================================================ cấp / thu quyền
def _default_runner(argv: list[str]) -> tuple[int, str, str]:  # pragma: no cover — test dùng runner giả
    import subprocess

    from .oscompat import no_window_kwargs

    p = subprocess.run(argv, capture_output=True, text=True, timeout=60, **no_window_kwargs())
    return p.returncode, p.stdout, p.stderr


@dataclass
class _Plan:
    user: str
    status_dir: Path
    runner: Runner | None = None
    steps: list[tuple[str, list[str] | None]] = field(default_factory=list)  # (lệnh shell để in, argv để chạy | None = bước Python)

    def __post_init__(self) -> None:
        if not USER_RE.match(self.user or "") or self.user == "root":
            raise ValueError(f"tên user không hợp lệ: {self.user!r}")

    def commands(self) -> list[str]:
        return [s for s, _ in self.steps]

    @staticmethod
    def _need_root(euid: int | None) -> None:
        if (os.geteuid() if euid is None else euid) != 0:
            raise PermissionError("--write cần chạy bằng root: sudo cfharness admin users … --write")


class AddPlan(_Plan):
    """Cấp quyền: vào nhóm → thư mục riêng status/<u>/ + audit/<u>/ → LaunchDaemon (in lệnh) → admin-log → bước tiếp."""

    def build(self, *, install_daemon: bool = True, web_port: int | None = None, system_install: bool = False) -> "AddPlan":
        u = shlex.quote(self.user)
        audit = self.status_dir.parent / "audit"
        self.steps = [(f"sudo dseditgroup -o edit -a {u} -t user {USERS_GROUP}", ["/usr/sbin/dseditgroup", "-o", "edit", "-a", self.user, "-t", "user", USERS_GROUP])]
        for d in (self.status_dir / self.user, audit / self.user):
            q = shlex.quote(str(d))
            self.steps += [(f"sudo mkdir -p {q}", ["/bin/mkdir", "-p", str(d)]),
                           (f"sudo chown {u}:{ADMIN_GROUP} {q}", ["/usr/sbin/chown", f"{self.user}:{ADMIN_GROUP}", str(d)]),
                           (f"sudo chmod 2750 {q}", ["/bin/chmod", "2750", str(d)])]
        if install_daemon:
            extra = (f" --web-port {int(web_port)}" if web_port else "") + (" --system-install" if system_install else "")
            self.steps.append((f"sudo cfharness admin install-daemon --user {u}{extra} --write", None))
        self.steps.append((f"# admin-log: user_add {self.user}", None))
        return self

    def next_steps(self) -> list[str]:
        u = shlex.quote(self.user)
        return [f"sudo -u {u} cfharness secret set CLAUDE_CODE_OAUTH_TOKEN   # token Claude (claude setup-token)",
                f"sudo -u {u} cfharness config set admin_export.enabled true   # hoặc đặt require.export trong policy",
                f"sudo -u {u} cfharness web totp enroll   # nếu user dùng web public"]

    def apply(self, *, euid: int | None = None) -> list[str]:
        self._need_root(euid)
        run = self.runner or _default_runner
        done = []
        for shown, argv in self.steps:
            if argv is None:
                continue
            rc, out, err = run(argv)
            if rc != 0:
                raise RuntimeError(f"`{shown}` lỗi ({rc}): {(err or out).strip()[:300]}")
            done.append(shown)
        admin_log_append(self.user, "user_add", by=os.environ.get("SUDO_USER") or "root")
        done.append(f"admin-log: user_add {self.user}")
        return done


class RevokePlan(_Plan):
    """Thu quyền: cờ ESTOP riêng user (reason revoked) → bootout LaunchDaemon → gỡ khỏi nhóm → admin-log. KHÔNG xoá HUB_HOME."""

    def build(self) -> "RevokePlan":
        u = shlex.quote(self.user)
        fp = shlex.quote(str(flag_path(self.user)))
        self.steps = [
            (f"echo '{{\"reason_code\":\"revoked\"}}' | sudo tee {fp} >/dev/null && sudo chmod 0644 {fp}", None),
            (f"sudo launchctl bootout system/{PLIST_PREFIX}{self.user} 2>/dev/null || true", ["/bin/launchctl", "bootout", f"system/{PLIST_PREFIX}{self.user}"]),
            (f"sudo launchctl bootout system/com.cfharness.web.{self.user} 2>/dev/null || true", ["/bin/launchctl", "bootout", f"system/com.cfharness.web.{self.user}"]),
            (f"sudo dseditgroup -o edit -d {u} -t user {USERS_GROUP}", ["/usr/sbin/dseditgroup", "-o", "edit", "-d", self.user, "-t", "user", USERS_GROUP]),
            (f"# admin-log: user_revoke {self.user} (HUB_HOME của user GIỮ NGUYÊN)", None),
        ]
        return self

    def apply(self, *, euid: int | None = None) -> list[str]:
        from .admin_control import set_flag

        self._need_root(euid)
        run = self.runner or _default_runner
        done = []
        set_flag(True, self.user, reason_code="revoked", by=os.environ.get("SUDO_USER") or "root")
        done.append(f"cờ ESTOP {flag_path(self.user)}")
        for shown, argv in self.steps:
            if argv is None:
                continue
            rc, out, err = run(argv)
            if rc != 0 and "bootout" not in shown:
                raise RuntimeError(f"`{shown}` lỗi ({rc}): {(err or out).strip()[:300]}")
            done.append(shown)
        admin_log_append(self.user, "user_revoke", by=os.environ.get("SUDO_USER") or "root", reason_code="revoked")
        done.append(f"admin-log: user_revoke {self.user}")
        return done


def runbook(admin_user: str) -> list[str]:
    """Lệnh thiết lập một lần (in ở `admin setup check --runbook` và docs)."""
    root = str(shared_root())
    a = shlex.quote(admin_user)
    out = [f'sudo dseditgroup -o create -r "cfharness quản trị máy" {ADMIN_GROUP}',
           f'sudo dseditgroup -o create -r "cfharness người dùng" {USERS_GROUP}',
           f"sudo dseditgroup -o edit -a {a} -t user {ADMIN_GROUP}"]
    for spec in dir_specs():
        p = os.path.join(root, spec.rel) if spec.rel else root
        out += _mkdir_cmds(p, spec.owner, spec.group, spec.mode)
    out.append("# rồi với mỗi user: sudo cfharness admin users add <user> --write ; khởi động lại daemon admin (InitGroups)")
    return out


__all__ = ["AddPlan", "RevokePlan", "USER_STATES", "control_dir", "admin_log_dir", "policy_dir", "installed_plists", "list_users",
           "runbook", "setup_check", "user_state"]
