"""`hub.filehub` — thư mục dùng chung người ↔ agent trên "Không gian số" (docs/dev-notes/filehub.md, plan-cloud-files.md §3, §8.4).

- Cấu hình ở `config.yaml` nhánh `filehub.*` (∈ `integrity.SECURITY_KEYS`; chỉ daemon ghi theo lệnh người). Danh sách thư mục chung
  `filehub.folders = [{name, root_id, ws_id, local_path, kind, account?}]` (v1 dùng 1, code sẵn cho N). `kind` = `folder` (MỘT thư mục,
  cây con) | `workspace` (TOÀN BỘ một workspace — gốc ảo `root_id = ws_id`, docs/dev-notes/plan-filehub-workspace-root.md); thiếu = folder.
- Đổi tên trên web không đổi id: `local_path` + tên cfharness GIỮ; tên sống (`status.roots[].server_name`) là BÍ DANH trong `resolve_ref`.
- Đổi tên cfharness (`filehub.rename`, chỉ người): `name` mới, `local_path` GIỮ; tên cũ vào `aliases` (≤ `ALIASES_MAX`) để task/agent/run đã
  ghi `filehub:<tên cũ>` vẫn khớp. Thứ tự khớp: tên cfharness → bí danh cũ → tên sống trên web.
- Đăng nhập: listener loopback của binary (CLI / web mở trên máy hub) hoặc RELAY OAuth (web mở qua `web.public_origin`): binary chạy với
  `--redirect-uri <public_origin>/filehub/oauth/cb`, hub giữ `state` của LOGIN_URL (≤ `relay_timeout_s`, MỘT lần) → route công khai của web
  gọi `filehub.login_callback{code, state}` → so `compare_digest` → ghi `CODE <code>\t<state>` vào stdin của binary (PKCE verifier không rời binary).
- Trạng thái đồng bộ đọc từ `HUB_HOME/filehub/status.json` (binary sync ghi nguyên tử) → `idle|syncing|conflict|offline|auth_expired|error`
  (+ `paused`, `not_configured`, `version_mismatch` do hub đặt). Đổi trạng thái / bản xung đột mới / chiều đẩy bị chặn → event.
- Link "Mở trên Không gian số": ánh xạ đường dẫn → id từ `<profile>/index/<rootId>.json` (chỉ đọc) → `<server>/files/<folder_id>?file=<file_id>`.
- Presence: run GHI vào thư mục chung bắt đầu/kết thúc → `control.json` `{"presence": {rootId: {label, ttl_s} | null}}` (binary đẩy lên
  Không gian số — cfharness không cầm token). Làm mới trước khi hết `ttl_s`.
- Steer: `recent` có thay đổi chiều `down` trong khi một run đang giữ khoá gốc thư mục → `steer(by="hub")` câu cố định (tiếng Anh).
  `signals` bỏ qua steer `by=="hub"` (không phải correction của người).
- Sau run trên workspace `filehub:`: xoá → `filehub.deleted` (không hỏi duyệt — §7.3), đổi → `filehub.changed`, tệp mới/sửa chứa GIÁ TRỊ
  secret hub biết (`redact`) → `filehub.secret_warning` (chỉ cảnh báo, tệp đã lên cloud). Cũng cho mỗi `extra_write_roots` của run nằm
  trong thư mục chung (`RunManager` chụp danh sách tệp riêng — `extra_checkpoints` — rồi gọi `report_extra`; payload có `extra_root`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..aiotasks import track
from ..errors import Conflict, HubError, NotFound
from ..events import TERMINAL_RUN_EVENTS, HubEvent, make_event
from ..oscompat import no_window_kwargs
from .supervisor import SyncSupervisor, child_env

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.filehub")

REF_PREFIX = "filehub:"
STATUS_MAX_BYTES = 4_000_000
STATES = ("idle", "syncing", "conflict", "offline", "auth_expired", "error", "paused", "not_configured", "version_mismatch", "starting", "revoked")
STEER_TEXT = "[cfharness] The human changed {rel} on Không gian số while you were working. Re-Read it before editing."
IGNORE_BASE = (".git/", "node_modules/", ".DS_Store", "Thumbs.db", "*.tmp", "*.swp", "~$*", ".~lock.*", ".tangthu*",
               # tệp nội bộ của claude CLI trong cwd của run (vd `.claude/.cc-writes`, settings.local.json) — không bao giờ lên cloud
               ".claude/", ".claude.json", ".cc-writes")
DEVICE_NAME_MAX = 40
META_PREFIX = ".tangthu"
KINDS = ("folder", "workspace")
ALIASES_MAX = 5  # tên cfharness cũ giữ làm bí danh sau `filehub.rename`
RELAY_PATH = "/filehub/oauth/cb"
# code/state của callback OAuth đi vào MỘT dòng stdin của binary (`CODE <code>\t<state>`) — chỉ ký tự an toàn cho URL, không tab/xuống dòng
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~+/=")
TOKEN_MAX = 2048
# lỗi trang đồng ý trả về redirect_uri (openapi.yaml authorize 302) — ngoài danh sách → "other" (không phản chiếu chữ của bên ngoài)
CALLBACK_ERRORS = ("access_denied", "device_limit", "super_not_member", "not_ws_admin")
_ERR_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
# `ERROR <mã>` của `login` (headless S5) → chữ cho thẻ Cài đặt / CLI / app desktop (dựng ở daemon, không trong template)
LOGIN_ERROR_TEXT = {
    "access_denied": "Bạn đã từ chối cấp quyền ở trang đồng ý.",
    # Trang đồng ý không cho biết tài khoản nào vừa đồng ý (lỗi chỉ có mã) → không gán lỗi cho tài khoản hub đang dùng: đúng lúc người
    # đăng nhập NHẦM tài khoản thì gán sẽ đổ lỗi cho tài khoản đúng. Tài khoản đang dùng chỉ nêu làm ngữ cảnh ({acc}).
    "device_limit": "Tài khoản bạn vừa dùng ở trang đồng ý đã đủ số thiết bị — thu hồi thiết bị cũ ở Không gian số › Hồ sơ › Thiết bị rồi "
                    "thử lại.{acc}",
    "super_not_member": "Tài khoản bạn vừa dùng ở trang đồng ý là Super Admin, không phải thành viên của workspace — chế độ workspace cần đăng "
                        "nhập bằng tài khoản Workspace Admin là thành viên của workspace đó.{acc}",
    "not_ws_admin": "Tài khoản bạn vừa dùng ở trang đồng ý không phải Workspace Admin của workspace đã chọn — đăng nhập lại bằng tài khoản "
                    "admin của workspace (hoặc dùng chế độ một thư mục).{acc}",
    "timeout": "Hết thời gian chờ đồng ý — bắt đầu lại.", "state_mismatch": "Liên kết đăng nhập không khớp — bắt đầu lại.",
    "stdin_closed": "Lượt đăng nhập bị huỷ trước khi hoàn tất — bắt đầu lại.", "network": "Không kết nối được Không gian số — kiểm mạng rồi thử lại.",
    "invalid_grant": "Mã đăng nhập không còn hiệu lực — bắt đầu lại.", "store": "Không ghi được tệp token của tiến trình đồng bộ.",
    "usage": "Binary đồng bộ không nhận tham số (bản cũ?) — cập nhật tang-thu-sync-headless.",
}


def login_error_text(code: Any, account: str = "") -> str | None:
    if not code:
        return None
    c = str(code)
    t = LOGIN_ERROR_TEXT.get(c)
    if t is None:
        return f"Đăng nhập Không gian số lỗi: {c}"
    return t.format(acc=f" Hub hiện đăng nhập bằng «{account}»." if account else "")


def error_code(v: Any) -> str:
    """Mã lỗi từ query `?error=` của trang đồng ý → chuyển cho binary (`ERROR <mã>\t<state>`): chỉ [a-z0-9_], ≤ 64, khác → other."""
    s = str(v or "")
    return s if 0 < len(s) <= 64 and all(c in _ERR_CODE_CHARS for c in s) else "other"
REVOKED_ERRORS = ("root_revoked", "disconnected", "workspace_deleted")
BINARY_NAME = "tang-thu-sync-headless"
# Chỗ admin cài binary sync (scripts/setup-hub-user.sh cài /usr/local/bin) — web KHÔNG BAO GIỜ nhận đường dẫn binary từ client (chạy được tệp
# tuỳ ý); thiếu cấu hình thì daemon tự dò ở các chỗ cố định này.
BINARY_CANDIDATES = ("/usr/local/bin/" + BINARY_NAME, "/opt/homebrew/bin/" + BINARY_NAME, "~/.local/bin/" + BINARY_NAME)


DEFAULT_PARENT = "~/KhongGianSo"  # = DEFAULTS filehub.local_parent
PARENT_DIRNAME = "KhongGianSo"  # tên thư mục cha trong vùng giam khi local_parent nằm ngoài W


def _real(p: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _under(p: str, base: str) -> bool:
    p, base = p.rstrip("/"), base.rstrip("/")
    return p == base or p.startswith(base + "/")


def all_folders(config: Any) -> list[dict[str, Any]]:
    """Mọi thư mục chung đã cấu hình (cả chế độ `sync` lẫn `lane`)."""
    try:
        v = config.get("filehub.folders") or []
    except KeyError:
        return []
    return [dict(f) for f in v if isinstance(f, dict) and f.get("name")]


def folder_mode(f: dict[str, Any] | None, config: Any) -> str:
    """`sync` (đồng bộ 2 chiều cũ) | `lane` (cổng cfharness). `folders[].mode` thắng `filehub.mode` (mặc định `sync`)."""
    m = (f or {}).get("mode")
    if m not in ("sync", "lane"):
        try:
            m = config.get("filehub.mode")
        except (KeyError, AttributeError):
            m = None
    return "lane" if m == "lane" else "sync"


def config_folders(config: Any) -> list[dict[str, Any]]:
    """Thư mục chung chế độ `sync` (có bản sao cục bộ) — mọi chỗ dùng `local_path` (guard, sandbox, khoá, binary sync)."""
    return [f for f in all_folders(config) if f.get("local_path") and folder_mode(f, config) == "sync"]


def lane_folders(config: Any) -> list[dict[str, Any]]:
    """Thư mục chung chế độ `lane` (không bản sao lâu dài — cây làm việc theo run, filehub/lane/)."""
    return [f for f in all_folders(config) if f.get("root_id") and folder_mode(f, config) == "lane"]


def local_roots(hub: Any) -> list[str]:
    """Gốc bản sao cục bộ (realpath) của mọi thư mục chung đã cấu hình — dùng cho guard/sandbox/khoá/validate_workspace.
    Đọc thẳng cấu hình (không cần `hub.filehub`) để mọi nơi gọi được, kể cả khi service chưa gắn."""
    try:
        if not hub.config.get("filehub.enabled"):
            return []
    except (KeyError, AttributeError):
        return []
    return list(dict.fromkeys(_real(f["local_path"]) for f in config_folders(hub.config)))


def root_of(hub: Any, path: str) -> str | None:
    rp = _real(path)
    for r in local_roots(hub):
        if _under(rp, r):
            return r
    return None


def _is_ip(s: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def friendly_host(host: str) -> str:
    """Tên máy từ hostname: IP giữ nguyên (không cắt ở dấu chấm), `may.local`/FQDN → phần đầu; giới hạn độ dài."""
    h = (host or "").strip()
    if not h:
        return "máy"
    if not _is_ip(h):
        h = h.split(".")[0] or h
    return h[:DEVICE_NAME_MAX]


_MACHINE: str | None = None


def machine_name() -> str:
    """Tên máy thân thiện cho thiết bị sync: macOS `scutil --get ComputerName` (vd "MacBook của An"), không có → hostname."""
    global _MACHINE
    if _MACHINE is not None:
        return _MACHINE
    name = ""
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3, **no_window_kwargs())
            if r.returncode == 0:
                name = " ".join(r.stdout.split())[:DEVICE_NAME_MAX]
        except (OSError, subprocess.TimeoutExpired):
            name = ""
    _MACHINE = name or friendly_host(socket.gethostname())
    return _MACHINE


def _runnable(p: str) -> bool:
    return bool(p) and os.path.isabs(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def detect_binary(configured: str | None = None) -> str | None:
    """Binary sync dùng được: cấu hình (người đặt) → các chỗ cài cố định (`BINARY_CANDIDATES`). Không dò PATH (PATH của daemon do người
    dùng/agent ảnh hưởng được)."""
    if configured:
        p = os.path.expanduser(str(configured))
        if _runnable(p):
            return p
    for c in BINARY_CANDIDATES:
        p = os.path.expanduser(c)
        if _runnable(p):
            return p
    return None


def valid_name(name: str) -> bool:
    return (bool(name) and len(name) <= 80 and all(c.isalnum() or c in " -_." for c in name) and name.strip() == name
            and name not in (".", "..") and not name.startswith("."))


def safe_name(name: str, fallback: str = "workspace") -> str:
    """Tên thư mục chung từ tên trên Không gian số (vd tên workspace có `/`, `:`) → ký tự hợp lệ của `valid_name`."""
    out = "".join(c if (c.isalnum() or c in " -_.") else "-" for c in str(name or ""))[:80]
    out = " ".join(out.split()).lstrip(".").strip()
    return out if valid_name(out) else fallback


def kind_of(f: dict[str, Any] | None) -> str:
    k = str((f or {}).get("kind") or "folder")
    return k if k in KINDS else "folder"


def aliases_of(f: dict[str, Any] | None) -> list[str]:
    v = (f or {}).get("aliases") or []
    return [str(x) for x in v if isinstance(x, str) and valid_name(x)][:ALIASES_MAX] if isinstance(v, list) else []


def valid_token(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v) <= TOKEN_MAX and all(c in _TOKEN_CHARS for c in v)


def read_status_file(hub: Any) -> dict[str, Any] | None:
    """status.json của binary sync (đọc thẳng — dùng cả khi `hub.filehub` chưa gắn, vd dựng system prompt)."""
    p = Path(hub.paths.home) / "filehub" / "status.json"
    try:
        if p.stat().st_size > STATUS_MAX_BYTES:
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def live_names(hub: Any) -> dict[str, str]:
    """root_id → tên SỐNG trên Không gian số (`roots[].server_name`, đã làm sạch một dòng). Đổi tên trên web → tên mới ở đây."""
    from ..termsafe import one_line

    out: dict[str, str] = {}
    for r in (read_status_file(hub) or {}).get("roots") or []:
        if isinstance(r, dict) and r.get("id") and r.get("server_name"):
            n = one_line(str(r["server_name"]), 120)
            if n:
                out[str(r["id"])] = n
    return out


def shared_folders(hub: Any) -> list[dict[str, str]]:
    """Thư mục chung cho khối prompt "Shared folder": tên cfharness, đường dẫn, kind, tên sống (nếu khác)."""
    try:
        if not hub.config.get("filehub.enabled"):
            return []
    except (KeyError, AttributeError):
        return []
    names = live_names(hub)
    out = []
    for f in config_folders(hub.config):
        live = names.get(str(f.get("root_id") or ""), "")
        out.append({"name": str(f["name"]), "path": str(f["local_path"]), "kind": kind_of(f), "server_name": live if live and live != f["name"] else ""})
    for f in lane_folders(hub.config):  # cổng cfharness: không bản sao — cây làm việc dựng cho run có workspace `filehub:<tên>`
        out.append({"name": str(f["name"]), "path": "", "kind": kind_of(f), "server_name": "", "lane": True})
    return out


def root_revoked(r: dict[str, Any]) -> bool:
    return str(r.get("last_error") or "") in REVOKED_ERRORS or str(r.get("state") or "") == "disconnected"


class FilehubService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.supervisor = SyncSupervisor(self)
        self._task: asyncio.Task[None] | None = None
        self._last_state: str | None = None
        self._seen_recent: set[str] = set()
        self._recent_primed = False
        self._seen_conflicts: set[str] = set()
        self._blocked: dict[str, str] = {}
        self._steered: set[tuple[str, str]] = set()
        self._presence: dict[str, dict[str, str]] = {}  # root_id → {run_id: label}
        self._presence_written = 0.0
        self._resume_push: list[str] = []
        self.login_state: dict[str, Any] = {"state": "none"}
        self._login_proc: asyncio.subprocess.Process | None = None
        self._relay: dict[str, Any] | None = None  # {state, expires} — state của LOGIN_URL relay đang chờ callback (một lần)
        self._bg: set[asyncio.Task[Any]] = set()
        hub.bus.add_listener(self._on_event)
        from .lane.service import LaneService  # cổng cfharness (chế độ lane) — filehub/lane/

        self.lane = LaneService(self)

    # ------------------------------------------------------------------ cấu hình / đường dẫn
    def cfg(self, key: str) -> Any:
        try:
            return self.hub.config.get(f"filehub.{key}")
        except KeyError:
            return None

    def local_parent(self) -> str:
        """Thư mục cha THỰC DÙNG của bản sao cục bộ (đường dẫn tuyệt đối).
        Đang giam (`workspace.confine`) mà `filehub.local_parent` (kể cả mặc định `~/KhongGianSo`) nằm ngoài vùng giam W → bản sao ở đó
        không bao giờ hợp lệ (`_validate_local`) → dùng `<W>/KhongGianSo` thay vì báo lỗi bắt người tự sửa cấu hình."""
        parent = os.path.expanduser(str(self.cfg("local_parent") or DEFAULT_PARENT))
        from .. import confine as _confine

        conf = _confine.of_hub(self.hub)
        if conf is not None and not _under(_real(parent), conf.root):
            return os.path.join(conf.root, PARENT_DIRNAME)
        return parent

    @property
    def dir(self) -> Path:
        return self.hub.paths.home / "filehub"

    @property
    def profile_dir(self) -> Path:
        return self.dir / "profile"

    @property
    def status_path(self) -> Path:
        return self.dir / "status.json"

    @property
    def control_path(self) -> Path:
        return self.dir / "control.json"

    @property
    def ignore_path(self) -> Path:
        return self.dir / "ignore.txt"

    @property
    def tokens_path(self) -> Path:
        return self.hub.paths.home / "secrets" / "filehub" / "tokens.json"

    def device_name(self) -> str:
        return str(self.cfg("device_name") or f"cfharness · {machine_name()}")[:64]

    def folders(self) -> list[dict[str, Any]]:
        """Thư mục chế độ `sync` (có bản sao cục bộ). Thư mục `lane`: `lane_folders()`."""
        return config_folders(self.hub.config)

    def all_folders(self) -> list[dict[str, Any]]:
        return all_folders(self.hub.config)

    def lane_folders(self) -> list[dict[str, Any]]:
        return lane_folders(self.hub.config)

    def _set_folders(self, rows: list[dict[str, Any]]) -> None:
        """Ghi `filehub.folders` từ danh sách thư mục `sync` — GIỮ thư mục `lane` (code đồng bộ cũ chỉ thấy thư mục sync)."""
        keep = [f for f in self.lane_folders() if not any(r.get("name") == f["name"] or (f.get("root_id") and r.get("root_id") == f["root_id"]) for r in rows)]
        self.hub.config.set("filehub.folders", list(rows) + keep)

    def lane_target(self, ref: Any) -> dict[str, Any] | None:
        """`filehub:<tên>[/<con>]` trỏ thư mục chế độ `lane` → `{folder, server, root, sub, key}`; thư mục `sync`/không khớp → None."""
        if not isinstance(ref, str) or not ref.startswith(REF_PREFIX) or not self.enabled():
            return None
        return self.lane.target(ref[len(REF_PREFIX):].strip().strip("/"))

    def enabled(self) -> bool:
        return bool(self.cfg("enabled"))

    def configured(self) -> bool:
        return self.enabled() and bool(self.cfg("sync_binary")) and bool(self.cfg("server")) and bool(self.folders()) and not self.paused()

    def paused(self) -> bool:
        return bool(self.hub.store.state_get("filehub.paused"))

    def folder(self, name: str, *, aliases: bool = False) -> dict[str, Any]:
        """Thư mục theo tên cfharness; `aliases=True` → cả tên cũ sau `filehub.rename` (run/task đã ghi tên cũ)."""
        for f in self.folders():
            if f["name"] == name:
                return f
        if aliases:
            for f in self.folders():
                if name in aliases_of(f):
                    return f
        raise NotFound(f"không có thư mục chung '{name}' (cfharness filehub status)")

    def _known_account(self) -> str:
        acc = self.login_state.get("account") or next((f.get("account") for f in self.folders() if f.get("account")), "")
        return str(acc or "")

    def scope(self) -> str:
        v = str(self.cfg("scope") or "workspace")
        return v if v in KINDS else "workspace"

    def match_ref(self, body: str) -> tuple[dict[str, Any], str]:
        """`<tên>[/<con>]` → (thư mục, phần con). Tên cfharness khớp trước; không có → tên SỐNG trên web (bí danh sau khi đổi tên,
        chỉ khi khớp đúng một thư mục). Tên dài nhất thắng (tên workspace có thể chứa `/`)."""
        folders = self.folders()
        for f in sorted(folders, key=lambda x: -len(x["name"])):
            n = f["name"]
            if body == n or body.startswith(n + "/"):
                return f, body[len(n):].lstrip("/")
        old = sorted(((a, f) for f in folders for a in aliases_of(f)), key=lambda x: -len(x[0]))  # tên cũ sau `filehub.rename`
        for a, f in old:
            if body == a or body.startswith(a + "/"):
                return f, body[len(a):].lstrip("/")
        names = live_names(self.hub)
        hits = []
        for f in folders:
            live = names.get(str(f.get("root_id") or ""))
            if live and (body == live or body.startswith(live + "/")):
                hits.append((len(live), f, body[len(live):].lstrip("/")))
        if hits:
            hits.sort(key=lambda x: -x[0])
            if len(hits) == 1 or hits[0][0] != hits[1][0]:
                return hits[0][1], hits[0][2]
        name = body.partition("/")[0]
        raise NotFound(f"không có thư mục chung '{name}' (cfharness filehub status)")

    def folder_by_root(self, root_id: str | None) -> dict[str, Any] | None:
        return next((f for f in self.folders() if f.get("root_id") == root_id), None)

    def folder_of_path(self, path: str) -> dict[str, Any] | None:
        rp = _real(path)
        for f in self.folders():
            if _under(rp, _real(f["local_path"])):
                return f
        return None

    def pending_add(self) -> dict[str, Any] | None:
        st = self.read_status() or {}
        have = {r.get("id") for r in st.get("roots") or [] if isinstance(r, dict)}
        return next((f for f in self.folders() if f.get("root_id") and f["root_id"] not in have), None)

    def stale_roots(self) -> list[str]:
        """Root binary còn đồng bộ (status.json) mà KHÔNG còn trong `filehub.folders` — `filehub remove` / chuyển sang workspace chỉ sửa cấu
        hình; hồ sơ của binary vẫn giữ root nên nó tiếp tục đồng bộ bản sao cũ (chuyển sang workspace: thư mục cũ nằm TRONG workspace mới →
        hai bản trên máy của cùng một thư mục server, người xoá bản cũ như được dặn là xoá trên server). Truyền `--remove-root` (e2e 2026-11-16)."""
        st = self.read_status() or {}
        mine = {str(f.get("root_id")) for f in self.folders() if f.get("root_id")}
        out = []
        for r in st.get("roots") or []:
            rid = str((r or {}).get("id") or "") if isinstance(r, dict) else ""
            if rid and rid not in mine and all(c.isalnum() or c in "-_" for c in rid):
                out.append(rid)
        return list(dict.fromkeys(out))

    # ------------------------------------------------------------------ tệp phụ
    def prepare_files(self) -> None:
        for d in (self.dir, self.profile_dir, self.tokens_path.parent):
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:  # pragma: no cover
                pass
        self.write_ignore()
        if not self.control_path.exists():
            self._write_control()

    def ignore_lines(self) -> list[str]:
        """Mẫu bỏ qua chiều đẩy lên (kiểu .gitignore): rác/tạm/.git + tên bí mật của `files.secret_patterns` (trừ `secret_allow`)."""
        from ..fileview import settings as _file_settings

        try:
            st = _file_settings(self.hub.config.get("files"))
        except Exception:  # noqa: BLE001
            st = _file_settings(None)
        out = list(IGNORE_BASE) + [str(p) for p in st["secret_patterns"]] + [f"!{p}" for p in st["secret_allow"]]
        return list(dict.fromkeys(out + [str(x) for x in self.cfg("extra_ignore") or []]))

    def write_ignore(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.ignore_path.with_suffix(".tmp")
        tmp.write_text("# sinh tự động bởi cfharness (filehub/service.py) — sửa qua filehub.extra_ignore\n" + "\n".join(self.ignore_lines()) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.ignore_path)

    def _write_control(self) -> None:
        ttl = int(self.cfg("presence_ttl_s") or 600)
        pres: dict[str, Any] = {}
        for f in self.folders():
            rid = f.get("root_id")
            if not rid:
                continue
            labels = list((self._presence.get(rid) or {}).values())
            pres[rid] = {"label": (", ".join(dict.fromkeys(labels)))[:80], "ttl_s": ttl} if labels else None
        data = {"presence": pres, "resume_push": list(self._resume_push)}
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.control_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.control_path)
        self._presence_written = time.time()

    # ------------------------------------------------------------------ đọc status / index
    def read_status(self) -> dict[str, Any] | None:
        try:
            if self.status_path.stat().st_size > STATUS_MAX_BYTES:
                return None
            d = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) else None

    def read_index(self, root_id: str) -> dict[str, Any]:
        if not root_id or "/" in root_id or root_id.startswith("."):
            return {}
        p = self.profile_dir / "index" / f"{root_id}.json"
        try:
            if p.stat().st_size > 50_000_000:
                return {}
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def overall_state(self, st: dict[str, Any] | None) -> str:
        if not self.enabled() or not self.folders():
            return "not_configured"
        if self.paused():
            return "paused"
        sv = self.supervisor.state
        if sv in ("version_mismatch",):
            return "version_mismatch"
        if sv in ("error",) or (sv == "backoff" and self.supervisor.restarts >= 3):
            return "error"
        if st is None:
            return "starting" if sv in ("starting", "running", "backoff", "stopped") else "offline"
        s = str(st.get("state") or "idle")
        if s not in STATES:
            s = "error"
        mine = {f.get("root_id") for f in self.folders()}
        if s != "auth_expired" and any(root_revoked(r) for r in st.get("roots") or [] if isinstance(r, dict) and r.get("id") in mine):
            return "revoked"  # tài khoản không còn quyền với gốc (vd bị gỡ Workspace Admin) — đăng nhập lại bằng tài khoản có quyền
        if s in ("idle", "syncing") and any((r or {}).get("conflicts") for r in st.get("roots") or [] if isinstance(r, dict)):
            return "conflict"
        return s

    # ------------------------------------------------------------------ link
    def server(self) -> str:
        return str(self.cfg("server") or "").rstrip("/")

    def link_for(self, path: str) -> dict[str, Any] | None:
        f = self.folder_of_path(path)
        if f is None or not f.get("root_id"):
            return None
        base = _real(f["local_path"])
        rp = _real(path)
        rel = os.path.relpath(rp, base).replace(os.sep, "/")
        idx = self.read_index(f["root_id"])
        items = idx.get("items") if isinstance(idx.get("items"), dict) else {}
        root_folder = idx.get("root_folder_id")
        srv = self.server()
        if rel in (".", ""):
            if kind_of(f) == "workspace":  # gốc ảo (wsId) không phải thư mục trên web — trang Tệp của workspace hiện tại
                return {"folder": f["name"], "rel": "", "url": f"{srv}/files" if srv else None}
            return {"folder": f["name"], "rel": "", "url": f"{srv}/files/{root_folder}" if root_folder else None}
        it = items.get(rel) if isinstance(items, dict) else None
        if not isinstance(it, dict):
            return {"folder": f["name"], "rel": rel, "url": None}
        if it.get("file_id"):
            parent = it.get("parent_folder_id") or root_folder
            if kind_of(f) == "workspace" and parent == f.get("root_id"):  # tệp ở gốc workspace: không đồng bộ (W3) → không có trang
                return {"folder": f["name"], "rel": rel, "url": None}
            url = f"{srv}/files/{parent}?file={it['file_id']}"
        else:
            url = f"{srv}/files/{it.get('folder_id')}"
        return {"folder": f["name"], "rel": rel, "url": url if srv else None}

    def link(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.link {path, run_id?|agent_id?}` — đường dẫn tương đối được hiểu theo cwd/diff của run (như `file.read`)."""
        path = str(p.get("path") or "")
        if not path:
            raise HubError("thiếu tham số 'path'", code=-32602)
        cands = [path] if os.path.isabs(os.path.expanduser(path)) else []
        rid = p.get("run_id")
        if rid and not cands:
            run = self.hub.store.find_run(str(rid))
            if run is None:
                raise NotFound(f"không tìm thấy run '{rid}'")
            for base in ((run.get("diff") or {}).get("root"), run.get("cwd")):
                if base:
                    cands.append(os.path.join(str(base), path))
        for c in cands:
            res = self.link_for(c)
            if res is not None:
                if not res.get("url"):
                    raise NotFound("chưa có ánh xạ id cho tệp này (đồng bộ chưa xong?)")
                return res
        raise NotFound("tệp không nằm trong thư mục chung")

    # ------------------------------------------------------------------ workspace `filehub:<tên>[/<con>]`
    def resolve_ref(self, ref: str, *, must_exist: bool = True) -> tuple[str, dict[str, Any], str]:
        """`filehub:<tên>[/<đường dẫn con>]` → (đường dẫn cục bộ tuyệt đối, thư mục, đường dẫn con). Không `..`, không thoát gốc (symlink)."""
        if not isinstance(ref, str) or not ref.startswith(REF_PREFIX):
            raise HubError(f"workspace phải dạng filehub:<tên>[/<thư mục con>]: {ref!r}")
        body = ref[len(REF_PREFIX):].strip().strip("/")
        if not self.enabled():
            raise HubError("thư mục chung (filehub) chưa bật — `cfharness filehub setup`")
        if self.lane.target(body) is not None:  # chế độ lane: không có bản sao trên máy — bên gọi phải xử lý `lane_target` trước
            raise HubError(f"'{ref}' là thư mục chung chế độ lane (cổng cfharness) — không có bản sao trên máy; cây làm việc dựng khi run chạy",
                           data={"lane": True})
        f, sub = self.match_ref(body)
        parts = [x for x in sub.split("/") if x not in ("", ".")]
        if any(x == ".." for x in parts) or any(x.startswith(META_PREFIX) for x in parts):
            raise HubError(f"đường dẫn con không hợp lệ: {sub!r}")
        base = _real(f["local_path"])
        target = os.path.join(base, *parts) if parts else base
        rt = _real(target)
        if not _under(rt, base):
            raise HubError(f"'{ref}' thoát ra ngoài thư mục chung (symlink?)")
        if must_exist and not os.path.isdir(rt):
            raise HubError(f"thư mục {rt} chưa có trên máy (đồng bộ chưa xong, hoặc chưa tạo trên Không gian số)")
        return rt, f, "/".join(parts)

    # ------------------------------------------------------------------ trạng thái
    def status(self) -> dict[str, Any]:
        from ..termsafe import one_line

        st = self.read_status()
        roots = {r.get("id"): r for r in (st or {}).get("roots") or [] if isinstance(r, dict)}
        names = live_names(self.hub)
        out_f = []
        for f in self.folders():
            r = roots.get(f.get("root_id")) or {}
            idx = self.read_index(f.get("root_id") or "") if f.get("root_id") else {}
            kind = kind_of(f)
            live = names.get(str(f.get("root_id") or "")) or ""
            account = one_line(str(f.get("account") or ""), 80)  # từ cột 5 của dòng ROOT lúc đăng nhập (status S5 không có account)
            # S5: `roots[].unpushable[{path, kind}]` — kind root_files = tệp nằm thẳng ở gốc workspace (server 422 root_files_not_allowed)
            unp = [x for x in (r.get("unpushable") or [])[:200] if isinstance(x, dict) and x.get("path")]
            root_files = [one_line(str(x["path"]), 300) for x in unp if x.get("kind") == "root_files"][:50]
            other_unpushable = [{"path": one_line(str(x["path"]), 300), "kind": one_line(str(x.get("kind") or ""), 30)}
                                for x in unp if x.get("kind") != "root_files"][:50]
            if kind == "workspace":
                url = f"{self.server()}/files" if self.server() else None
            else:
                url = f"{self.server()}/files/{idx['root_folder_id']}" if idx.get("root_folder_id") and self.server() else None
            out_f.append({
                "name": f["name"], "root_id": f.get("root_id"), "local_path": f["local_path"], "state": r.get("state") or ("pending" if f.get("root_id") else "no_root"),
                "kind": kind, "server_name": live, "renamed": bool(live and live != f["name"]), "account": account or None,
                "aliases": aliases_of(f),
                "display": (f"Toàn bộ workspace «{live or f['name']}»" if kind == "workspace" else f"Thư mục «{live or f['name']}»"),
                "root_files": root_files, "unpushable": other_unpushable, "revoked": root_revoked(r),
                # gốc workspace mất admin (binary S5 `last_error=root_revoked`, không ngắt): cấp lại quyền → tự chạy tiếp, không đăng nhập lại
                "revoked_temporary": root_revoked(r) and str(r.get("last_error") or "") == "root_revoked" and str(r.get("state") or "") != "disconnected",
                # workspace bị xoá trên server (binary S5 `last_error=workspace_deleted`): ngắt hẳn, binary đổi tên bản trên máy
                # `<tên> (đã ngắt đồng bộ)` — đăng nhập lại không giúp gì; `disconnected_path` = chỗ tệp nằm bây giờ
                "workspace_deleted": str(r.get("last_error") or "") == "workspace_deleted",
                "disconnected_path": (str(r["local_path"]) if root_revoked(r) and r.get("local_path")
                                      and _real(str(r["local_path"])) != _real(f["local_path"]) else None),
                "pending_up": r.get("pending_up"), "pending_down": r.get("pending_down"), "blocked": r.get("blocked"),
                "conflicts": [{"path": str(c.get("path"))[:300], "created_at": c.get("created_at")} for c in (r.get("conflicts") or [])[:50] if isinstance(c, dict)],
                "last_error": r.get("last_error"),
                "url": url,
                "lock": self.hub.runs.locks.holder(_real(f["local_path"])),
            })
        recent = [{k: e.get(k) for k in ("ts", "root_id", "dir", "op", "path")} for e in ((st or {}).get("recent") or [])[-20:] if isinstance(e, dict)]
        return {
            "enabled": self.enabled(), "server": self.server() or None, "device": self.device_name(), "state": self.overall_state(st),
            "paused": self.paused(), "supervisor": self.supervisor.info(), "sync_version": (st or {}).get("version") or self.supervisor.version,
            "updated_at": (st or {}).get("updated_at"), "folders": out_f, "recent": recent,
            "login": {**{k: v for k, v in self.login_state.items()
                         if k in ("state", "url", "roots", "error", "started_at", "finished_at", "mode", "relay", "account", "switch", "switched_from")},
                      "message": login_error_text(self.login_state.get("error"), self._known_account())
                      if self.login_state.get("state") == "error" else None},
            "tokens": self.tokens_path.exists(), "local_parent": self.local_parent(),
            "scope": self.scope(), "binary": detect_binary(self.cfg("sync_binary")),
            "relay_available": self.public_origin() is not None,
            "can_switch": any(kind_of(f) != "workspace" for f in self.folders()),
            "relogin_after_import": bool(self.hub.store.state_get("import_filehub")),
            # thư mục chế độ lane (cổng cfharness — không bản sao trên máy): `filehub status` liệt kê cùng thư mục sync
            "lane_folders": [{**f, "mode": "lane"} for f in self.lane.status().get("folders") or []],
        }

    def list(self) -> list[dict[str, Any]]:
        """RPC `filehub.list` (đọc — điều phối viên dùng để giao việc): tên thư mục chung + workspace + đường dẫn cục bộ."""
        names = live_names(self.hub)
        out = []
        for f in self.folders():
            live = names.get(str(f.get("root_id") or "")) or ""
            row = {"name": f["name"], "workspace": f"{REF_PREFIX}{f['name']}", "local_path": f["local_path"], "root_id": f.get("root_id"),
                   "kind": kind_of(f), "server_name": live or f["name"]}
            if live and live != f["name"]:  # đổi tên trên web: tên cfharness giữ, tên mới là bí danh (`filehub:<tên mới>` cũng khớp)
                row["note"] = f"{f['name']} — trên web hiện là «{live}»"
            if aliases_of(f):
                row["aliases"] = aliases_of(f)
            if kind_of(f) == "workspace":
                row["layout"] = "root = whole workspace; new files must be inside a top-level folder"
            out.append(row)
        return out

    # ------------------------------------------------------------------ vòng nền
    def start(self) -> None:
        if not self.enabled():
            return
        try:
            self.prepare_files()
            self.supervisor.reconcile()
        except Exception:  # pragma: no cover
            log.exception("filehub: không chuẩn bị được thư mục")
        if self.configured():
            self.supervisor.start()
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._loop())
            except RuntimeError:
                self._task = None

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._login_proc is not None and self._login_proc.returncode is None:
            try:
                self._login_proc.kill()
            except ProcessLookupError:  # pragma: no cover
                pass
        await self.supervisor.stop()
        await self.lane.stop()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(0.05, float(self.cfg("tick_s") or 1.0)))
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("filehub tick lỗi")

    def on_supervisor_state(self) -> None:
        try:
            self.tick()
        except Exception:  # pragma: no cover
            log.exception("filehub tick lỗi")

    def tick(self) -> None:
        st = self.read_status()
        state = self.overall_state(st)
        # syncing↔idle là nhịp thường của đồng bộ — không phát/ghi event mỗi vòng; chỉ khi vào/ra trạng thái có ý nghĩa
        # (offline, auth_expired, error, conflict, paused, version_mismatch…). Trạng thái thật vẫn có ở filehub.status.
        state = "idle" if state == "syncing" else state
        if state != self._last_state:
            prev, self._last_state = self._last_state, state
            if prev is not None or state not in ("not_configured", "starting"):
                self.hub.publish(make_event("filehub.state", None, {"state": state, "prev": prev, "error": self.supervisor.last_error if state in ("error", "version_mismatch") else None},
                                            source="hub"))
        if st is None:
            return
        for r in st.get("roots") or []:
            if not isinstance(r, dict):
                continue
            f = self.folder_by_root(r.get("id"))
            fname = f["name"] if f else str(r.get("name") or r.get("id"))[:80]
            for c in r.get("conflicts") or []:
                if not isinstance(c, dict):
                    continue
                key = f"{r.get('id')}|{c.get('path')}|{c.get('created_at')}"
                if key in self._seen_conflicts:
                    continue
                self._seen_conflicts.add(key)
                self.hub.publish(make_event("filehub.conflict", None, {"folder": fname, "root_id": r.get("id"), "path": str(c.get("path"))[:300]}, source="hub"))
            b = r.get("blocked")
            if b and self._blocked.get(str(r.get("id"))) != b:
                self.hub.publish(make_event("filehub.blocked", None, {"folder": fname, "root_id": r.get("id"), "reason": str(b)[:40]}, source="hub"))
            if b:
                self._blocked[str(r.get("id"))] = str(b)
            else:
                self._blocked.pop(str(r.get("id")), None)
                if r.get("id") in self._resume_push:
                    self._resume_push.remove(r.get("id"))
                    self._write_control()
        self._handle_recent(st.get("recent") or [])
        ttl = float(self.cfg("presence_ttl_s") or 600)
        if any(self._presence.values()) and time.time() - self._presence_written > ttl / 2:
            self._write_control()

    def _handle_recent(self, recent: list[Any]) -> None:
        keys = []
        for e in recent:
            if not isinstance(e, dict):
                continue
            k = f"{e.get('ts')}|{e.get('root_id')}|{e.get('dir')}|{e.get('op')}|{e.get('path')}"
            keys.append((k, e))
        if not self._recent_primed:  # lần đầu: thay đổi cũ không phải "trong lúc run chạy"
            self._seen_recent = {k for k, _ in keys}
            self._recent_primed = True
            return
        for k, e in keys:
            if k in self._seen_recent:
                continue
            self._seen_recent.add(k)
            if e.get("dir") == "down":
                self._cloud_change(e)
        if len(self._seen_recent) > 2000:
            self._seen_recent = {k for k, _ in keys}

    def _cloud_change(self, e: dict[str, Any]) -> None:
        if not self.cfg("steer_on_cloud_change"):
            return
        f = self.folder_by_root(e.get("root_id"))
        if f is None:
            return
        holder = self.hub.runs.locks.holder(_real(f["local_path"]))
        rid = (holder or {}).get("run_id")
        if not rid:
            return
        run = self.hub.store.get_run(rid)
        if run is None or run.get("status") != "running":
            return
        from ..termsafe import one_line

        rel = one_line(str(e.get("path") or ""), 200)
        if (rid, rel) in self._steered:
            return
        self._steered.add((rid, rel))
        text = STEER_TEXT.format(rel=rel)
        self._spawn(self._steer(rid, text))

    async def _steer(self, run_id: str, text: str) -> None:
        from ..steer import steer

        try:
            await steer(self.hub, run_id, text, by="hub", channel="hub")
        except HubError as ex:
            log.info("filehub: không steer được run %s: %s", run_id, ex)

    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        track(t, self._bg)  # L1: lỗi task → log

    # ------------------------------------------------------------------ listener bus: presence + báo sau run
    def _run_folder(self, run: dict[str, Any]) -> dict[str, Any] | None:
        name = (run.get("run_opts") or {}).get("filehub")
        if not name:
            return None
        try:
            return self.folder(str(name), aliases=True)
        except NotFound:
            return None

    def _on_event(self, ev: HubEvent) -> None:
        try:
            if ev.type == "hub.started":
                self.start()
                self.lane.start()  # cổng cfharness: hạn khoá, cây mồ côi, cây của run đã xong (daemon khởi động lại)
            elif ev.type == "run.started" and ev.run_id:
                run = self.hub.store.get_run(ev.run_id) or {}
                f = self._run_folder(run)
                if f is not None and f.get("root_id") and not (run.get("run_opts") or {}).get("read_only"):
                    self._presence.setdefault(f["root_id"], {})[ev.run_id] = f"cfharness · {run.get('agent_id')}"
                    self._write_control()
            elif ev.type in TERMINAL_RUN_EVENTS and ev.source == "hub" and ev.run_id and ev.type != "run.waiting_human":
                changed = False
                for rid, runs in self._presence.items():
                    if runs.pop(ev.run_id, None) is not None:
                        changed = True
                if changed:
                    self._write_control()
                self._steered = {x for x in self._steered if x[0] != ev.run_id}
            elif ev.type == "workspace.diff" and ev.run_id:
                self.after_run(ev.run_id)
        except Exception:  # pragma: no cover — listener không được làm hỏng bus
            log.exception("filehub listener lỗi với %s", ev.type)

    def after_run(self, run_id: str) -> dict[str, Any] | None:
        """Sau diff của run trên workspace `filehub:` → báo xoá / thay đổi / tệp có thể chứa bí mật (chỉ cảnh báo)."""
        run = self.hub.store.get_run(run_id) or {}
        f = self._run_folder(run)
        diff = run.get("diff")
        if f is None or not isinstance(diff, dict) or not diff.get("root"):
            return None
        return self._report(run_id, run, f, diff)

    def report_extra(self, run_id: str, diffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Báo sau run cho `extra_write_roots` nằm trong thư mục chung (RunManager chụp danh sách tệp trước run, diff sau run — không có
        revert). Mỗi gốc thêm một bộ event, payload có `extra_root` (đường dẫn tương đối trong thư mục chung)."""
        run = self.hub.store.get_run(run_id) or {}
        out = []
        for d in diffs:
            if not isinstance(d, dict) or not d.get("root"):
                continue
            f = self.folder_of_path(str(d["root"]))
            if f is None:
                continue
            r = self._report(run_id, run, f, d, extra=True)
            if r is not None:
                out.append(r)
        return out

    def _report(self, run_id: str, run: dict[str, Any], f: dict[str, Any], diff: dict[str, Any], *, extra: bool = False) -> dict[str, Any] | None:
        base = _real(f["local_path"])
        droot = str(diff["root"])
        deleted, changed, secret = [], [], []
        max_b = int(self.cfg("secret_scan_max_bytes") or 5_000_000)
        from ..redact import contains_secret_value

        for item in diff.get("files") or []:
            ap = os.path.join(droot, str(item.get("path") or ""))
            rel = os.path.relpath(_real(ap) if os.path.exists(ap) else os.path.abspath(ap), base).replace(os.sep, "/")
            if rel.startswith(".."):
                continue
            if item.get("status") == "deleted":
                deleted.append(rel)
                continue
            changed.append(rel)
            try:
                if os.path.isfile(ap) and not os.path.islink(ap) and os.path.getsize(ap) <= max_b:
                    with open(ap, "rb") as fh:
                        if contains_secret_value(fh.read()):
                            secret.append(rel)
            except OSError:
                continue
        link = self.link_for(droot) or {}
        url = link.get("url")
        common: dict[str, Any] = {"folder": f["name"], "run_id": run_id, "url": url}
        if extra:
            common["extra_root"] = os.path.relpath(_real(droot), base).replace(os.sep, "/")
        if changed:
            ev = make_event("filehub.changed", run.get("agent_id"), {**common, "count": len(changed), "paths": changed[:20]}, run_id=run_id, source="hub")
            self.hub.publish(ev)
        if deleted:
            self.hub.publish(make_event("filehub.deleted", run.get("agent_id"), {**common, "count": len(deleted), "paths": deleted[:20]}, run_id=run_id, source="hub"))
        if secret:
            self.hub.publish(make_event("filehub.secret_warning", run.get("agent_id"), {**common, "count": len(secret), "paths": secret[:20]}, run_id=run_id, source="hub"))
        return {"changed": changed, "deleted": deleted, "secret": secret}

    # ------------------------------------------------------------------ thao tác của người (RPC ghi, HUMAN_ONLY)
    def _validate_local(self, local: str, *, exclude: str | None = None, forbid: tuple[str, ...] | list[str] = ()) -> str:
        p = Path(os.path.expanduser(local))
        if not p.is_absolute():
            raise HubError(f"đường dẫn bản sao cục bộ phải tuyệt đối: {local!r}")
        rp = _real(p)
        hh = _real(self.hub.paths.home)
        if _under(rp, hh) or _under(hh, rp):
            raise HubError(f"bản sao cục bộ '{rp}' không được nằm trong/chứa HUB_HOME ({hh})", code=-32003)
        uh = _real(os.path.expanduser("~"))
        if rp in (uh, "/") or rp == os.path.dirname(uh):
            raise HubError(f"bản sao cục bộ '{rp}' quá rộng — chọn một thư mục riêng (vd ~/KhongGianSo/<tên>)")
        for f in self.folders():
            if f["name"] == exclude:
                continue
            o = _real(f["local_path"])
            if _under(rp, o) or _under(o, rp):
                raise HubError(f"bản sao cục bộ '{rp}' trùng/chứa thư mục chung '{f['name']}' ({o})")
        for old in forbid:  # chuyển chế độ: bản sao mới không được trùng/chứa bản sao cũ (người tự xoá bản cũ sau khi kiểm)
            o = _real(old)
            if _under(rp, o) or _under(o, rp):
                raise HubError(f"bản sao cục bộ '{rp}' trùng/chứa bản sao cũ ({o}) — chọn đường dẫn khác (--local-path)")
        from .. import confine as _confine

        conf = _confine.of_hub(self.hub)
        if conf is not None:  # P2: phải trong W
            if not _under(rp, conf.root):
                raise HubError(f"bản sao cục bộ '{rp}' nằm ngoài workspace được giam ({conf.root}) — workspace.confine (chỉ người đổi)", code=-32003)
        from ..guard.runtime import fixed_protected_dirs

        sb = (self.hub.config.get("guard") or {}).get("sandbox") or {}
        for region in fixed_protected_dirs(self.hub.paths.home, self.hub.paths.socket, sb):
            if "*" in region:
                continue
            r = _real(region)
            if _under(rp, r) or _under(r, rp):
                raise HubError(f"bản sao cục bộ '{rp}' trùng/chứa vùng bảo vệ ({region})", code=-32003)
        return rp

    def add(self, root_id: str, name: str | None = None, *, ws_id: str | None = None, local_path: str | None = None, kind: str | None = None,
            account: str | None = None, forbid: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
        root_id = str(root_id or "").strip()
        if not root_id or not all(c.isalnum() or c in "-_" for c in root_id):
            raise HubError(f"root_id không hợp lệ: {root_id!r}")
        granted = next((r for r in self.login_state.get("roots") or [] if r.get("id") == root_id), {})
        name = str(name or "").strip() or safe_name(str(granted.get("name") or ""), root_id) or root_id
        if not valid_name(name):
            raise HubError(f"tên thư mục chung không hợp lệ: {name!r} (chữ, số, khoảng trắng, - _ ., ≤ 80 ký tự)")
        if any(f["name"] == name or f.get("root_id") == root_id or name in aliases_of(f) for f in self.all_folders()):
            raise Conflict(f"thư mục chung '{name}' (root {root_id}) đã có (hoặc là tên cũ của một thư mục chung)")
        ws_id = ws_id or granted.get("ws_id") or None
        kind = str(kind or granted.get("kind") or "folder")
        if kind not in KINDS:
            raise HubError(f"kind không hợp lệ: {kind!r} (folder|workspace)")
        account = account if account is not None else granted.get("account")
        parent = self.local_parent()
        local = self._validate_local(local_path or os.path.join(parent, name), forbid=forbid)
        if not os.path.isdir(local):
            os.makedirs(local, exist_ok=True)
        row: dict[str, Any] = {"name": name, "root_id": root_id, "ws_id": ws_id, "local_path": local, "kind": kind}
        if account:
            row["account"] = str(account)[:80]
        folders = self.folders() + [row]
        self._set_folders(folders)
        if not self.enabled():
            self.hub.config.set("filehub.enabled", True)
        self._apply_change()
        return {"name": name, "root_id": root_id, "local_path": local, "workspace": f"{REF_PREFIX}{name}", "kind": kind}

    def remove(self, name: str) -> dict[str, Any]:
        f = self.folder(name)
        holder = self.hub.runs.locks.holder(_real(f["local_path"]))
        if holder:
            raise Conflict(f"thư mục chung '{name}' đang có run {holder.get('run_id')} ghi — dừng run trước")
        self._set_folders([x for x in self.folders() if x["name"] != name])
        self._apply_change()
        return {"removed": name, "local_path": f["local_path"], "note": "tệp trên máy và trên Không gian số được giữ nguyên"}

    def rename(self, name: str, new_name: str) -> dict[str, Any]:
        """Đổi tên cfharness (`filehub:<tên>`) — chỉ cục bộ: không đổi `local_path`, không đổi tên trên Không gian số. Tên cũ giữ làm bí
        danh (task/agent/run đã ghi `filehub:<tên cũ>` vẫn chạy). Có run giữ khoá → từ chối; root chưa đồng bộ lần đầu → từ chối (binary
        còn nhận `--root-name` của lần thêm)."""
        f = self.folder(str(name or ""))
        old = f["name"]
        new_name = str(new_name or "").strip()
        if not valid_name(new_name):
            raise HubError(f"tên thư mục chung không hợp lệ: {new_name!r} (chữ, số, khoảng trắng, - _ ., ≤ 80 ký tự)")
        if new_name == old:
            raise HubError(f"tên mới trùng tên hiện tại «{old}»")
        for x in self.all_folders():
            if x["name"] != old and (x["name"] == new_name or new_name in aliases_of(x)):
                raise Conflict(f"tên «{new_name}» đã dùng cho thư mục chung «{x['name']}»")
        holder = self.hub.runs.locks.holder(_real(f["local_path"]))
        if holder:
            raise Conflict(f"thư mục chung '{old}' đang có run {holder.get('run_id')} ghi — dừng run trước khi đổi tên")
        pend = self.pending_add()
        if pend is not None and pend.get("root_id") == f.get("root_id"):
            raise Conflict(f"thư mục chung '{old}' chưa đồng bộ lần đầu — chờ đồng bộ xong rồi đổi tên")
        aliases = [a for a in aliases_of(f) if a != new_name] + [old]
        rows = []
        for x in self.folders():
            if x["name"] == old:
                x = {**x, "name": new_name, "aliases": aliases[-ALIASES_MAX:]}
            rows.append(x)
        self._set_folders(rows)
        return {"name": new_name, "previous": old, "aliases": aliases[-ALIASES_MAX:], "local_path": f["local_path"],
                "workspace": f"{REF_PREFIX}{new_name}", "note": "chỉ đổi tên trong cfharness — đường dẫn trên máy và tên trên Không gian số giữ nguyên"}

    def pause(self) -> dict[str, Any]:
        self.hub.store.state_set("filehub.paused", True)
        self._spawn(self.supervisor.stop())
        self.tick()
        return {"paused": True}

    def resume(self, root_id: str | None = None, *, discard: bool = False) -> Any:
        if root_id and any(str(f.get("root_id")) == str(root_id) for f in self.lane_folders()):
            # thư mục chế độ lane: lô xoá hàng loạt đang giữ của các run trên gốc này → NGƯỜI quyết đẩy tiếp / bỏ (discard)
            return self.lane.release_deletes(str(root_id), discard=discard)
        if discard:
            raise HubError("--discard chỉ dùng cho thư mục chế độ lane (cổng cfharness)")
        if root_id:  # chiều đẩy lên bị chặn (xoá hàng loạt) → báo binary đẩy tiếp
            if self.folder_by_root(root_id) is None:
                raise NotFound(f"không có thư mục chung với root {root_id}")
            if root_id not in self._resume_push:
                self._resume_push.append(root_id)
            self._write_control()
            return {"resume_push": root_id}
        self.hub.store.state_set("filehub.paused", False)
        self._apply_change()
        return {"paused": False, "running": self.configured()}

    def _apply_change(self) -> None:
        if not self.configured():
            self._spawn(self.supervisor.stop())
            self.tick()
            return
        self.prepare_files()
        self._write_control()
        if self.supervisor._task is None or self.supervisor._task.done():
            self.supervisor.start()
        else:
            self.supervisor.restart()
        if self._task is None or self._task.done():
            self.start()

    # ------------------------------------------------------------------ thiết lập / đăng nhập
    def public_origin(self) -> str | None:
        """`web.public_origin` (https, đã kiểm) — đích relay OAuth khi đăng nhập từ web mở qua link public. None = không relay được."""
        from ..web.public import parse_public_origin

        try:
            return parse_public_origin(self.hub.config.get("web.public_origin"))
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _remote_binary_ok(binary: str) -> bool:
        """Thiết lập qua web PUBLIC (phiên có thể bị lấy cắp): chỉ nhận binary đúng tên và người dùng của hub KHÔNG ghi được (tệp lẫn
        thư mục chứa) — vd /usr/local/bin do quản trị cài. Không thì phải đặt ở terminal / web trên chính máy hub."""
        base = os.path.basename(binary)
        if base not in ("tang-thu-sync-headless", "tang-thu-sync-headless.exe"):
            return False
        real = _real(binary)
        return not os.access(real, os.W_OK) and not os.access(os.path.dirname(real), os.W_OK)

    def setup(self, p: dict[str, Any]) -> dict[str, Any]:
        """Cấu hình + đăng nhập (đồng ý thiết bị) — xong thì tự thêm thư mục chung đầu tiên. `scope` workspace (mặc định) | folder.
        Binary: `binary` (chỉ CLI — web không bao giờ gửi) → cấu hình → chỗ cài cố định (`detect_binary`, không dò PATH).
        `remote` = gọi từ web qua link public: không được ĐỔI server/binary đã đặt; binary lần đầu phải qua `_remote_binary_ok`."""
        remote = bool(p.get("remote"))
        cur_server, cur_binary = self.server(), str(self.cfg("sync_binary") or "")
        server = str(p.get("server") or cur_server or "").strip().rstrip("/")
        if not (server.startswith("https://") or server.startswith("http://127.0.0.1") or server.startswith("http://localhost")):
            raise HubError("server phải là URL https:// của Không gian số (vd https://thumucai.trungtamdhs.com)")
        given = os.path.expanduser(str(p.get("binary") or "").strip())
        binary = given if given else detect_binary(cur_binary or None)
        if not binary or not _runnable(binary):
            if given:
                raise HubError(f"không tìm thấy binary sync chạy được: {given!r} (--binary /đường/dẫn/{BINARY_NAME})")
            raise HubError(f"máy chạy hub chưa có binary sync {BINARY_NAME} (admin cài vào /usr/local/bin, hoặc "
                           f"`cfharness filehub setup --binary /đường/dẫn/{BINARY_NAME}` trong terminal)")
        if remote:
            if cur_server and server != cur_server:
                raise HubError("qua link public không đổi được máy chủ Không gian số đã đặt — đổi ở terminal (`cfharness filehub setup`) "
                               "hoặc web trên chính máy chạy hub", code=-32003)
            cur = os.path.expanduser(cur_binary) if cur_binary else ""
            if cur and binary != cur:
                raise HubError("qua link public không đổi được binary sync đã đặt — đổi ở terminal (`cfharness filehub setup --binary …`) "
                               "hoặc web trên chính máy chạy hub", code=-32003)
            if not cur and not self._remote_binary_ok(binary):
                raise HubError("qua link public chỉ dùng được binary `tang-thu-sync-headless` do quản trị cài (vd /usr/local/bin, bạn không ghi "
                               "được) — đường dẫn khác: đặt ở terminal hoặc web trên chính máy chạy hub", code=-32003)
        scope = str(p.get("scope") or self.scope())
        if scope not in KINDS:
            raise HubError(f"scope không hợp lệ: {scope!r} (workspace|folder)")
        name = str(p.get("name") or "").strip()
        if name and not valid_name(name):
            raise HubError(f"tên thư mục không hợp lệ: {name!r}")
        if p.get("local_parent"):  # kiểm TRƯỚC khi ghi cấu hình nào
            lp = os.path.expanduser(str(p["local_parent"]).strip())
            if not os.path.isabs(lp):
                raise HubError(f"thư mục cha của bản sao cục bộ phải là đường dẫn tuyệt đối: {p['local_parent']!r}")
            from .. import confine as _confine

            conf = _confine.of_hub(self.hub)
            if conf is not None and not _under(_real(lp), conf.root):
                raise HubError(f"thư mục cha '{_real(lp)}' nằm ngoài workspace được giam ({conf.root}) — chọn một thư mục trong đó "
                               f"(vd {os.path.join(conf.root, PARENT_DIRNAME)}) hoặc bỏ trống để hub tự chọn", code=-32003)
        self.hub.config.set("filehub.server", server)
        self.hub.config.set("filehub.sync_binary", binary)
        if p.get("local_parent"):
            self.hub.config.set("filehub.local_parent", str(p["local_parent"]))
        if scope != self.scope():
            self.hub.config.set("filehub.scope", scope)
        self.hub.config.set("filehub.enabled", True)
        return {"server": server, "binary": binary, "name": name or ("cfharness" if scope == "folder" else ""), "scope": scope}

    def relogin_mode(self) -> str:
        """Đăng nhập lại giữ chế độ của thư mục chung đang có; chưa có → `filehub.scope`."""
        fs = self.folders()
        return kind_of(fs[0]) if fs else self.scope()

    async def login(self, *, root_hint: str | None = None, no_browser: bool = False, auto_add: bool = False, local_path: str | None = None,
                    mode: str | None = None, relay: bool = False, switch: bool = False) -> dict[str, Any]:
        """Chạy `<binary> login …`, chờ dòng `LOGIN_URL` rồi trả ngay; tiến trình chạy tiếp tới khi người đồng ý (ROOT …) hoặc lỗi.
        `mode` folder|workspace (`--root-mode single|workspace`; single = mặc định của binary nên không truyền cờ — chạy được với binary cũ).
        `relay` = web mở qua link public: `--redirect-uri <web.public_origin>/filehub/oauth/cb`, stdin PIPE, giữ `state` chờ callback."""
        mode = mode or self.relogin_mode()
        if mode not in KINDS:
            raise HubError(f"chế độ không hợp lệ: {mode!r} (workspace|folder)")
        old_proc = self._login_proc
        if old_proc is not None and old_proc.returncode is None:
            same = (self.login_state.get("mode"), bool(self.login_state.get("relay")), bool(self.login_state.get("switch"))) == (mode, relay, switch)
            if (same and self.login_state.get("state") == "waiting" and self.login_state.get("url")
                    and (not relay or self._relay is not None)):
                return {"login_url": self.login_state["url"], "state": "waiting", "mode": mode, "relay": relay}
            self._kill_login()  # lượt cũ khác kiểu (vd loopback → relay) hoặc đã huỷ: bỏ, bắt đầu lại
            try:
                await asyncio.wait_for(old_proc.wait(), timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover
                pass
        binary = self.supervisor.binary()
        if not binary or not self.server():
            raise HubError("chưa cấu hình server/binary — chạy `cfharness filehub setup` trước")
        origin = None
        if relay:
            origin = self.public_origin()
            if origin is None:
                raise HubError("đăng nhập từ xa cần `web.public_origin` (https://…, qua tunnel TLS) — đặt ở terminal "
                               "(`cfharness config set web.public_origin https://…`) hoặc đăng nhập trên chính máy chạy hub")
        self.prepare_files()
        argv = [binary, "login", "--server", self.server(), "--tokens", str(self.tokens_path), "--device", self.device_name()]
        if mode == "workspace":
            argv += ["--root-mode", "workspace"]
        if root_hint:
            argv += ["--root-hint", root_hint]
        relay_timeout = int(self.cfg("relay_timeout_s") or 600)
        if relay:
            argv += ["--redirect-uri", f"{origin}{RELAY_PATH}", "--no-browser", "--timeout", str(relay_timeout)]
        elif no_browser:
            argv.append("--no-browser")
        self._relay = None
        self.login_state = {"state": "starting", "started_at": time.time(), "roots": [], "mode": mode, "relay": relay, "switch": switch}
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE if relay else asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=child_env(),
                                                    cwd=str(self.dir), **no_window_kwargs())
        self._login_proc = proc
        timeout = float(self.cfg("login_url_timeout_s") or 30)
        url = None
        try:
            while url is None:
                assert proc.stdout is not None
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not line:
                    break
                s = line.decode("utf-8", "replace").strip()
                if s.startswith("LOGIN_URL "):
                    url = s[len("LOGIN_URL "):].strip()
                elif s.startswith("ERROR"):
                    code = s[5:].strip()[:120] or "unknown"
                    self.login_state.update(state="error", error=code, finished_at=time.time())
                    if code == "usage" and (mode == "workspace" or relay):
                        raise HubError("binary sync chưa hỗ trợ " + ("đăng nhập từ xa (relay)" if relay else "chế độ workspace")
                                       + " — cập nhật tang-thu-sync-headless" + ("" if relay else ", hoặc thiết lập với --scope folder"))
                    raise HubError(login_error_text(code, self._known_account()) or f"đăng nhập Không gian số lỗi: {code}")
        except asyncio.TimeoutError:
            proc.kill()
            self.login_state.update(state="error", error="timeout", finished_at=time.time())
            raise HubError("binary sync không trả LOGIN_URL kịp") from None
        if url is None:
            rc = await proc.wait()
            self.login_state.update(state="error", error=f"exit {rc}", finished_at=time.time())
            raise HubError(f"binary sync thoát (mã {rc}) trước khi in LOGIN_URL")
        if not (url.startswith("http://") or url.startswith("https://")):
            proc.kill()
            raise HubError("LOGIN_URL không hợp lệ")
        if relay:
            from urllib.parse import parse_qs, urlsplit

            st = (parse_qs(urlsplit(url).query).get("state") or [""])[0]
            if not valid_token(st) or len(st) < 16:
                proc.kill()
                self.login_state.update(state="error", error="no_state", finished_at=time.time())
                raise HubError("LOGIN_URL của binary sync không có state hợp lệ — không đăng nhập từ xa được")
            self._relay = {"state": st, "expires": time.time() + relay_timeout}
        self.login_state.update(state="waiting", url=url)
        self._spawn(self._finish_login(proc, auto_add=auto_add, local_path=local_path, switch=switch))
        return {"login_url": url, "state": "waiting", "mode": mode, "relay": relay}

    def _kill_login(self) -> None:
        p = self._login_proc
        self._relay = None
        if p is not None and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:  # pragma: no cover
                pass

    async def login_callback(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.login_callback {code, state, error?}` — web nhận ở route công khai `/filehub/oauth/cb` (relay OAuth).
        `state` so `compare_digest` với state đang chờ (≤ relay_timeout_s, MỘT lần). Sai/hết hạn/không có lượt chờ → lỗi CHUNG, lượt chờ
        giữ nguyên (kẻ dò không làm hỏng lượt đăng nhập thật). Khớp → ghi `CODE <code>\t<state>` vào stdin binary, chờ kết quả ≤ 30 s.
        code/state bị gỡ khỏi `p` trước khi audit (thông tin đăng nhập một lần)."""
        import hmac

        code, state, error = p.pop("code", None), p.pop("state", None), p.pop("error", None)
        generic = HubError("liên kết đăng nhập không hợp lệ hoặc đã hết hạn — bắt đầu lại ở Cài đặt › Không gian số", code=-32602)
        rel, proc = self._relay, self._login_proc
        if rel is None or proc is None or proc.returncode is not None or proc.stdin is None:
            raise generic
        if time.time() > float(rel["expires"]):
            self._kill_login()
            self.login_state.update(state="error", error="timeout", finished_at=time.time())
            raise generic
        if not valid_token(state) or not hmac.compare_digest(str(state).encode(), str(rel["state"]).encode()):
            raise generic
        self._relay = None  # dùng một lần
        if error is not None:  # trang đồng ý trả `?error=` → chuyển cho binary (`ERROR <mã>\t<state>`), binary in `ERROR <mã>` rồi thoát
            line = f"ERROR {error_code(error)}\t{state}\n"
        elif valid_token(code):
            line = f"CODE {code}\t{state}\n"
        else:
            self.login_state.update(state="error", error="invalid_code", finished_at=time.time())
            self._kill_login()
            return {"ok": False, "state": "error", "error": "invalid_code", "message": "Mã đăng nhập không hợp lệ — bắt đầu lại."}
        try:
            proc.stdin.write(line.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            self.login_state.update(state="error", error="sync_exited", finished_at=time.time())
            return {"ok": False, "state": "error", "error": "sync_exited"}
        t0 = time.monotonic()
        while self.login_state.get("state") not in ("done", "error") and time.monotonic() - t0 < 30:
            await asyncio.sleep(0.05)
        st = str(self.login_state.get("state"))
        err = self.login_state.get("error") if st == "error" else None
        return {"ok": st == "done", "state": st, "error": err, "message": login_error_text(err, self._known_account()) if err else None}

    async def switch_to_workspace(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.switch_to_workspace {local_path?, relay?, no_browser?, root_hint?}` (chỉ người): đăng nhập `--root-mode workspace`;
        xong → gỡ thư mục chung chế độ một thư mục khỏi cấu hình (GIỮ tệp trên máy và trên Không gian số) → thêm gốc workspace (bản sao
        mới không trùng/chứa bản cũ). Đăng nhập lỗi → cấu hình cũ giữ nguyên. Người tự xoá bản cũ + thu hồi thiết bị cũ ở Hồ sơ › Thiết bị."""
        folders = self.folders()
        if not self.server() or not self.supervisor.binary():
            raise HubError("chưa thiết lập Không gian số — `cfharness filehub setup` (mặc định đã là chế độ workspace)")
        if folders and all(kind_of(f) == "workspace" for f in folders):
            raise Conflict("thư mục chung đã ở chế độ toàn bộ workspace")
        for f in folders:
            holder = self.hub.runs.locks.holder(_real(f["local_path"]))
            if holder:
                raise Conflict(f"thư mục chung '{f['name']}' đang có run {holder.get('run_id')} ghi — dừng run trước khi chuyển chế độ")
        old = [f["local_path"] for f in folders if kind_of(f) != "workspace"]
        res = await self.login(mode="workspace", relay=bool(p.get("relay")), no_browser=bool(p.get("no_browser")), auto_add=True,
                               local_path=p.get("local_path"), switch=True, root_hint=p.get("root_hint"))
        self.login_state["switched_from"] = old
        return {**res, "switch_from": old,
                "note": "sau khi đồng ý: thư mục cũ được gỡ khỏi hub (tệp giữ nguyên) — kiểm bản mới rồi tự xoá bản cũ; thu hồi thiết bị cũ ở "
                        "Không gian số › Hồ sơ › Thiết bị (tối đa 5 thiết bị mỗi tài khoản)"}

    async def _finish_login(self, proc: asyncio.subprocess.Process, *, auto_add: bool, local_path: str | None, switch: bool = False) -> None:
        from ..termsafe import one_line

        roots: list[dict[str, str]] = []
        err = None
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").rstrip("\n")
            if s.startswith("ROOT "):
                # `ROOT <id>\t<wsId>\t<name>[\t<kind>\t<account>]` — cột 4–5 mới (S5); thiếu = folder / không rõ tài khoản
                parts = s[5:].split("\t")
                if len(parts) >= 3:
                    kind = parts[3].strip() if len(parts) >= 4 else "folder"
                    roots.append({"id": parts[0].strip(), "ws_id": parts[1].strip(), "name": one_line(parts[2], 80),
                                  "kind": kind if kind in KINDS else "folder", "account": one_line(parts[4], 80) if len(parts) >= 5 else ""})
            elif s.startswith("ERROR"):
                err = s[5:].strip()[:120] or "unknown"
        rc = await proc.wait()
        if self._login_proc is not proc:  # lượt cũ đã bị thay bằng lượt mới — không đụng trạng thái của lượt mới
            return
        self._relay = None
        if rc != 0 or err:
            if self.login_state.get("state") != "error":  # lỗi đã đặt (callback từ chối / huỷ) thì giữ
                self.login_state.update(state="error", error=err or f"exit {rc}", finished_at=time.time())
            self.hub.publish(make_event("filehub.state", None, {"state": "auth_error", "prev": self._last_state, "error": self.login_state.get("error")}, source="hub"))
            return
        try:
            os.chmod(self.tokens_path, 0o600)
        except OSError:
            pass
        account = next((r["account"] for r in roots if r.get("account")), "")
        self.login_state.update(roots=roots, account=account or None)
        if self.hub.store.state_get("import_filehub"):  # hub nhập từ máy khác (hub_transfer): thiết bị máy này đã đăng nhập → hết nhắc
            self.hub.store.state_set("import_filehub", None)
        added = None
        mode = self.login_state.get("mode") or "folder"
        if switch:
            added = self._switch_apply(roots, local_path)
        elif auto_add and roots and not self.folders():
            r = next((x for x in roots if x["kind"] == mode), roots[0])
            try:
                added = self.add(r["id"], safe_name(r["name"]), ws_id=r.get("ws_id"), local_path=local_path, kind=r["kind"], account=r.get("account") or None)
            except HubError as ex:
                self.login_state.update(error=f"không thêm được thư mục: {ex}"[:200])
        elif self.folders():
            self._update_accounts(roots)
            self._apply_change()  # đăng nhập lại (auth_expired / mất quyền) → chạy lại tiến trình sync với token mới
        if self.login_state.get("state") != "error":
            self.login_state.update(state="done", finished_at=time.time())
        try:
            from ..approvals import audit_record

            audit_record("filehub.login_done", agent_id=None, run_id=None,
                         data={"roots": [r["id"] for r in roots], "kinds": [r["kind"] for r in roots], "added": (added or {}).get("name"), "switch": switch})
        except Exception:  # pragma: no cover
            pass

    def _update_accounts(self, roots: list[dict[str, str]]) -> None:
        """Đăng nhập lại: cập nhật tài khoản hiển thị của thư mục đang có (có thể đăng nhập bằng tài khoản admin khác)."""
        by_id = {r["id"]: r for r in roots}
        fs, changed = self.folders(), False
        for f in fs:
            acc = (by_id.get(str(f.get("root_id"))) or {}).get("account")
            if acc and f.get("account") != acc:
                f["account"], changed = acc, True
        if changed:
            self._set_folders(fs)

    def _switch_apply(self, roots: list[dict[str, str]], local_path: str | None) -> dict[str, Any] | None:
        r = next((x for x in roots if x["kind"] == "workspace"), None)
        if r is None:  # server cũ bỏ qua root_mode → không đổi gì
            self.login_state.update(state="error", error="server chưa cấp gốc workspace (Không gian số chưa hỗ trợ, hoặc tài khoản không phải "
                                                          "Workspace Admin) — thư mục cũ giữ nguyên; đăng nhập lại bằng `cfharness filehub login`",
                                    finished_at=time.time())
            return None
        before = self.folders()
        old = [f for f in before if kind_of(f) != "workspace"]
        old_paths = [f["local_path"] for f in old]
        self._set_folders([f for f in before if kind_of(f) == "workspace"])
        name = safe_name(r["name"])
        if not local_path:
            parent = self.local_parent()
            if any(_under(_real(os.path.join(parent, name)), _real(o)) or _under(_real(o), _real(os.path.join(parent, name))) for o in old_paths):
                name = safe_name(f"{name} workspace")
        try:
            added = self.add(r["id"], name, ws_id=r.get("ws_id"), local_path=local_path, kind="workspace", account=r.get("account") or None,
                             forbid=old_paths)
        except HubError as ex:
            self._set_folders(before)
            self._apply_change()
            self.login_state.update(state="error", error=f"không thêm được gốc workspace: {ex}"[:200], finished_at=time.time())
            return None
        self.login_state["switched_from"] = old_paths
        return added


__all__ = ["FilehubService", "REF_PREFIX", "RELAY_PATH", "STEER_TEXT", "local_roots", "root_of", "config_folders", "valid_name", "safe_name", "kind_of",
           "live_names", "shared_folders", "all_folders", "lane_folders", "folder_mode", "read_status_file", "login_error_text", "BINARY_NAME", "detect_binary"]
