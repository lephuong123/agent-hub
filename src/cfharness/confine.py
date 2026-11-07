"""Giam agent trong MỘT workspace W (`workspace.confine`, plan đa instance P2 — `docs/dev-notes/confine.md`).

Khi `workspace.confine.root` được đặt (CHỈ người: `config.set` ∈ HUMAN_ONLY, khoá `workspace.confine` ∈ integrity.SECURITY_KEYS):
- `workspace.validate_workspace` / `manager.permissions.check_path`: mọi `dir:`/worktree/`extra_write_roots` phải nằm trong `root`
  (vùng scratch/worktree/eval/checkpoint của chính hub vẫn được).
- Sandbox OS (`guard/runtime.sandbox_settings`, phương án A — spike claude 2.1.319, `hardening-SEC.md` §1.1): `denyRead: [$HOME]`
  + `allowRead: [root, read_allow, workspace của hub, cwd, bin trên PATH]`. Áp CẢ khi agent `trust_full` (Q4) và khi
  `guard.sandbox.enabled=false` (khi đó chỉ phần giam filesystem, mạng mở).
- Classifier: Read/Grep/Glob/LS ngoài vùng đọc → `forbidden/read_outside_workspace`; tool ghi file ra ngoài `root` →
  `forbidden/write_outside_workspace` (trong `root` nhưng ngoài cwd vẫn là destructive, người duyệt được).
- `permissions.deny` (lớp tĩnh, không phụ thuộc hook): Read/Grep/Glob/Edit/Write vào mọi mục anh em của `root` và của từng thư mục
  cha tới `$HOME` (allow-trong-deny KHÔNG có ở luật quyền nên phải liệt kê — tính lúc dựng settings).

Registry hub (`~/.config/cfharness/hubs.json`, env `CFHARNESS_HUBS_REGISTRY` cho test): mỗi daemon ghi HUB_HOME của nó khi khởi động
(ghi nguyên tử, 0600) → sandbox/classifier chặn đọc + ghi mọi hub home KHÁC (trường hợp nhiều hub chung một user OS).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# toolchain thường cần ĐỌC ngoài W (cache gói, trình quản lý phiên bản, cấu hình git — KHÔNG gồm credential: vẫn denyRead riêng)
DEFAULT_READ_ALLOW = ["~/.npm", "~/.cache", "~/.local/share/uv", "~/.cargo", "~/.rustup", "~/.nvm", "~/Library/Caches",
                      "~/.pyenv", "~/.bun", "~/.gitconfig", "~/.config/git/config", "~/.config/git/ignore"]
# vùng của CHÍNH hub mà run được đọc/ghi (workspace scratch, worktree, bản sao eval) — như validate_workspace
HUB_WORK_DIRS = ("agents", "worktrees", "evals", "checkpoints")
CONFINE_RULES = ("read_outside_workspace", "write_outside_workspace")
READ_TOOL_KEYS = ("file_path", "path", "notebook_path")


@dataclass(frozen=True)
class Confine:
    root: str  # realpath
    read_allow: tuple[str, ...]  # realpath (expanded), có thể chưa tồn tại


def _real(p: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(str(p)))
    except (OSError, ValueError):
        return os.path.abspath(os.path.expanduser(str(p)))


def _under(p: str, base: str) -> bool:
    base = base.rstrip("/") or "/"
    return p == base or p.startswith(base + "/") or base == "/"


def from_config(config: Any) -> Confine | None:
    """`workspace.confine` → Confine; root null/rỗng → None (hành vi cũ). Giá trị lạ (sửa tay) → dùng mặc định read_allow."""
    try:
        c = config.get("workspace.confine") or {}
    except KeyError:
        return None
    if not isinstance(c, dict) or not c.get("root"):
        return None
    ra = c.get("read_allow")
    if not isinstance(ra, list):
        ra = DEFAULT_READ_ALLOW
    return Confine(root=_real(str(c["root"])), read_allow=tuple(dict.fromkeys(_real(str(x)) for x in ra if x)))


def of_hub(hub: Any) -> Confine | None:
    try:
        return from_config(hub.config)
    except Exception:  # pragma: no cover
        return None


def user_home() -> str:
    return _real("~")


def validate_root(value: Any, hub_home: Path | None = None) -> None:
    """Kiểm `config set workspace.confine[.root]` — sai → ValueError (không ghi)."""
    if value is None or value == "":
        return
    if not isinstance(value, str):
        raise ValueError("workspace.confine.root phải là đường dẫn (chuỗi) hoặc null")
    p = os.path.expanduser(value)
    if not os.path.isabs(p):
        raise ValueError("workspace.confine.root phải là đường dẫn tuyệt đối")
    rp = _real(p)
    uh = user_home()
    if rp == "/" or rp == uh or _under(uh, rp):
        raise ValueError(f"workspace.confine.root không được là '/', home của người hoặc thư mục cha của home ({rp})")
    if hub_home is not None:
        hh = _real(str(hub_home))
        if _under(hh, rp) or _under(rp, hh):
            raise ValueError(f"workspace.confine.root không được chứa/nằm trong HUB_HOME ({hh})")


def validate_setting(key: str, value: Any, hub_home: Path | None = None) -> None:
    if key == "workspace":
        if isinstance(value, dict) and "confine" in value:
            validate_setting("workspace.confine", value["confine"], hub_home)
        return
    if key == "workspace.confine":
        if value is None:
            return
        if not isinstance(value, dict):
            raise ValueError("workspace.confine phải là mapping {root, read_allow} hoặc null")
        unknown = set(value) - {"root", "read_allow"}
        if unknown:
            raise ValueError(f"workspace.confine: khoá lạ {sorted(unknown)}")
        validate_root(value.get("root"), hub_home)
        if "read_allow" in value:
            validate_setting("workspace.confine.read_allow", value["read_allow"], hub_home)
        return
    if key == "workspace.confine.root":
        validate_root(value, hub_home)
    elif key == "workspace.confine.read_allow":
        if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
            raise ValueError("workspace.confine.read_allow phải là danh sách đường dẫn")
        uh = user_home()
        for x in value:
            p = os.path.expanduser(x)
            if not os.path.isabs(p):
                raise ValueError(f"workspace.confine.read_allow: '{x}' phải là đường dẫn tuyệt đối (hoặc ~/…)")
            rp = _real(p)
            if rp == "/" or rp == uh or _under(uh, rp):
                raise ValueError(f"workspace.confine.read_allow: '{x}' quá rộng (home hoặc cha của home)")


def hub_work_dirs(hub_home: Path | str) -> list[str]:
    """Vùng làm việc của CHÍNH hub (đọc được khi giam): workspaces của từng agent (liệt kê — sandbox không mở lại glob), worktrees, evals."""
    out: list[str] = []
    for h in dict.fromkeys([str(hub_home), _real(str(hub_home))]):
        hp = Path(h)
        out += [str(hp / "worktrees"), str(hp / "evals")]
        try:
            for a in (hp / "agents").iterdir():
                if a.is_dir():
                    out.append(str(a / "workspaces"))
        except OSError:
            pass
    return list(dict.fromkeys(out))


def allowed_workspace_roots(hub: Any, conf: Confine) -> list[Path]:
    hh = Path(hub.paths.home).resolve()
    return [Path(conf.root)] + [hh / n for n in HUB_WORK_DIRS]


def read_roots(conf: Confine, hub_home: Path | str, *, cwd: str | Path | None = None, extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Mọi gốc được ĐỌC khi giam (dùng chung cho sandbox allowRead và classifier)."""
    from .guard.runtime import path_bin_dirs

    out = [conf.root, *conf.read_allow, *hub_work_dirs(hub_home)]
    if cwd is not None:
        out += [str(cwd), _real(str(cwd))]
    out += [str(x) for x in extra]
    out += path_bin_dirs()  # script có shebang trong ~/.local/bin… cần đọc để chạy
    return list(dict.fromkeys(x.rstrip("/") for x in out if x))


def deny_read_roots(conf: Confine) -> list[str]:
    """Gốc bị chặn đọc: $HOME; root nằm ngoài $HOME → thêm thư mục cha của root (anh em của W)."""
    uh = user_home()
    out = [uh, os.path.expanduser("~").rstrip("/")]
    if not _under(conf.root, uh):
        parent = os.path.dirname(conf.root.rstrip("/"))
        if parent and parent != "/":
            out.append(parent)
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------- permissions.deny (lớp tĩnh)
def static_deny_targets(conf: Confine, allow: list[str], max_entries: int = 400) -> list[str]:
    """Mục anh em của `root` và của mỗi thư mục cha tới $HOME (và mọi mục ở $HOME) KHÔNG phải tổ tiên/con của một gốc được phép.
    Phương án B dùng cho luật quyền (không có allow-trong-deny). Thư mục tạo SAU lúc dựng settings không có trong danh sách
    (classifier + sandbox vẫn chặn)."""
    uh = user_home()
    allow_r = [a.rstrip("/") for a in allow if a]

    def keep(p: str) -> bool:
        return any(_under(a, p) or _under(p, a) for a in allow_r)

    chain: list[str] = []
    d = conf.root
    base = uh if _under(conf.root, uh) else os.path.dirname(conf.root.rstrip("/"))
    while d and d != "/" and _under(d, base):
        parent = os.path.dirname(d)
        if parent == d:
            break
        chain.append(parent)
        if parent == base:
            break
        d = parent
    if uh not in chain:
        chain.append(uh)
    out: list[str] = []
    for parent in dict.fromkeys(chain):
        try:
            names = sorted(os.listdir(parent))
        except OSError:
            continue
        for n in names:
            p = os.path.join(parent, n)
            if not keep(p):
                out.append(p)
            if len(out) >= max_entries:
                return out
    return out


def permission_deny_rules(conf: Confine, allow: list[str]) -> list[str]:
    from .guard.runtime import rule_path

    out: list[str] = []
    for p in static_deny_targets(conf, allow):
        r = rule_path(p)
        for t in ("Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit"):
            out += [f"{t}(/{r}/**)", f"{t}(/{r})"]
    return out


# ---------------------------------------------------------------------------- sandbox
def apply_sandbox(sb: dict[str, Any], conf: Confine, hub_home: Path, *, cwd: Path | None, extra: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """Thêm phần giam vào khoá `sandbox` (đã có hoặc tối thiểu). allowRead KHÔNG mở lại được mục denyRead cụ thể hơn (spike) —
    secret/token của hub vẫn bị chặn kể cả khi nằm dưới một gốc được đọc."""
    fs = dict(sb.get("filesystem") or {})
    fs["denyRead"] = list(dict.fromkeys(list(fs.get("denyRead") or []) + deny_read_roots(conf)))
    fs["allowRead"] = list(dict.fromkeys(list(fs.get("allowRead") or []) + read_roots(conf, hub_home, cwd=cwd, extra=extra)))
    return {**sb, "filesystem": fs}


def confine_only_sandbox(conf: Confine, hub_home: Path, socket: Path, sb_cfg: dict[str, Any], *, cwd: Path | None) -> dict[str, Any]:
    """Sandbox CHỈ để giam filesystem (trust_full hoặc guard.sandbox.enabled=false): ghi được root + cwd + toolchain, mạng mở
    (`*` + loopback), Unix socket chỉ theo cấu hình (hub.sock luôn bị chặn). Vẫn chặn đọc secret/token của hub."""
    from .auth import token_path

    tok = str(token_path(socket))
    deny_r = [tok]
    for h in dict.fromkeys([str(hub_home), _real(str(hub_home))]):
        deny_r += [f"{h}/secrets", f"{h}/mcp_server/tokens.json", f"{h}/auth"]
        try:
            for a in (Path(h) / "agents").iterdir():
                deny_r.append(str(a / "runtime"))
        except OSError:
            pass
    deny_r += other_hub_homes(hub_home)
    deny_w = [str(socket), tok, *other_hub_homes(hub_home)]
    aw = [conf.root, *conf.read_allow] + [str(x) for x in sb_cfg.get("extra_allow_write") or []]
    socks = [str(x) for x in sb_cfg.get("allow_unix_sockets") or [] if _real(str(x)) != _real(str(socket))]
    sb: dict[str, Any] = {
        "enabled": True,
        "failIfUnavailable": bool(sb_cfg.get("fail_if_unavailable", True)),
        "autoAllowBashIfSandboxed": False,
        "allowUnsandboxedCommands": False,
        "filesystem": {"denyRead": deny_r, "denyWrite": deny_w, "allowWrite": list(dict.fromkeys(aw))},
        "network": {"allowedDomains": ["*"], "allowLocalBinding": True},
    }
    if socks:
        sb["network"]["allowUnixSockets"] = socks
    return apply_sandbox(sb, conf, hub_home, cwd=cwd)


# ---------------------------------------------------------------------------- classifier helper
def outside(p: str, conf_root: str | None, read_roots_: tuple[str, ...], deny_roots: tuple[str, ...]) -> bool:
    """`p` (tuyệt đối) bị chặn ĐỌC khi giam: nằm trong một gốc bị chặn ($HOME…) mà không nằm trong gốc được đọc nào.
    Xét cả đường dẫn literal lẫn realpath (symlink trong W trỏ ra ngoài → chặn)."""
    if not conf_root:
        return False
    cands = {os.path.normpath(p), _real(p)}
    for c in cands:
        if any(_under(c, r) for r in read_roots_):
            continue
        if any(_under(c, d) for d in deny_roots):
            return True
    return False


# ---------------------------------------------------------------------------- registry các hub (cùng user OS)
def registry_path() -> Path:
    env = os.environ.get("CFHARNESS_HUBS_REGISTRY")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".config" / "cfharness" / "hubs.json"


_REG_CACHE: dict[str, Any] = {"key": None, "homes": []}


def registered_homes() -> list[str]:
    p = registry_path()
    try:
        st = p.stat()
    except OSError:
        return []
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _REG_CACHE["key"] == key:
        return list(_REG_CACHE["homes"])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        homes = [str(h.get("home") if isinstance(h, dict) else h) for h in (data.get("hubs") if isinstance(data, dict) else data) or []]
    except (OSError, ValueError, AttributeError, TypeError):
        homes = []
    homes = [h for h in homes if h and os.path.isabs(h)]
    _REG_CACHE.update(key=key, homes=homes)
    return homes


def register_home(hub_home: Path | str) -> None:
    """Ghi HUB_HOME vào registry (nguyên tử, 0600, thư mục 0700). Bỏ mục không còn tồn tại. Lỗi → bỏ qua (không chặn khởi động)."""
    p = registry_path()
    me = _real(str(hub_home))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p.parent, 0o700)
        except OSError:
            pass
        homes = [h for h in registered_homes() if os.path.isdir(h) and _real(h) != me]
        homes.append(me)
        fd, tmp = tempfile.mkstemp(prefix=".hubs.", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"hubs": [{"home": h} for h in dict.fromkeys(homes)]}, f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return


def other_hub_homes(hub_home: Path | str) -> list[str]:
    """Hub home đã đăng ký KHÁC home đang dùng (không chứa/nằm trong nó)."""
    cur = {str(hub_home).rstrip("/"), _real(str(hub_home))}
    out: list[str] = []
    for h in registered_homes():
        r = _real(h)
        if r in cur or h.rstrip("/") in cur or any(_under(c, r) or _under(r, c) for c in cur):
            continue
        out.append(h.rstrip("/"))
    return list(dict.fromkeys(out))
