"""Xác thực NGƯỜI GỌI RPC của daemon (H-01, H-05) — lớp phòng thủ không phụ thuộc cách agent viết lệnh.

Vì sao cần: agent chạy CÙNG user OS với hub; quyền file socket (0600) không phân biệt được "người" với
"tiến trình con của run". Bộ phân loại lệnh chỉ là một lớp (lách được bằng cách viết khác). Ở đây daemon tự
xác định ai đang gọi, theo hai bằng chứng độc lập:

1. **Token người** — file 0600 cạnh socket (`<socket>.auth`, ví dụ `HUB_HOME/hub.sock.auth`), sinh lại mỗi lần
   daemon khởi động. CLI/TUI/MCP của người đọc TỰ ĐỘNG (không thêm bước tay) và gửi trong `params._auth`; daemon bỏ
   khoá này trước khi xử lý (không vào audit/log). Token KHÔNG nằm trong env của run; sandbox của run chặn đọc file
   (`guard/runtime.py`), bộ phân loại coi mọi chuỗi chứa đường dẫn socket là `forbidden`.
2. **Tiến trình ngang hàng** — pid của đầu kia socket do KERNEL báo (`LOCAL_PEERPID` trên macOS, `SO_PEERCRED`
   trên Linux), rồi:
   - đi ngược cây tiến trình (ppid): gặp pid tiến trình `claude` của một run đang chạy → là agent;
   - session id (`getsid`): run được spawn với `start_new_session` nên mọi hậu duệ (kể cả `nohup`, `&`, double-fork
     đã mồ côi về launchd/init) giữ session = pid của run, trừ khi tự gọi `setsid()`;
   - biến môi trường `AGENT_HUB_RUN_ID` của tiến trình gọi (đọc qua `KERN_PROCARGS2` / `/proc/<pid>/environ`).

Phương thức NHẠY CẢM (`HUMAN_ONLY`) chỉ chạy khi: có token đúng VÀ không có dấu hiệu nào là agent. Agent gọi
`learn.propose` không bị từ chối mà bị hạ xuống `created_by="agent"` (đi qua guard học đầy đủ — H-05). Lời gọi
không qua socket (in-process: test, MCP in-process) là nội bộ → tin cậy.

Giới hạn (ghi ở `docs/dev-notes/hardening-SEC.md`): một tiến trình con của run gọi `setsid()` rồi double-fork, xoá
env, VÀ đọc được token thì qua được lớp 2 — vì vậy lớp sandbox OS (chặn đọc token + chặn kết nối Unix socket, kế thừa
cho mọi hậu duệ, không thoát được bằng setsid/launchctl — đã kiểm chứng) là lớp chính khi bật.
"""

from __future__ import annotations

import contextvars
import ctypes
import ctypes.util
import hmac
import os
import secrets
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hub import Hub

AUTH_PARAM = "_auth"
ENV_RUN_ID = "AGENT_HUB_RUN_ID"

# Phương thức chỉ NGƯỜI được gọi (token + không phải hậu duệ run). Thêm method nhạy cảm mới → thêm vào đây + test.
HUMAN_ONLY: frozenset[str] = frozenset(
    {
        # phê duyệt / chính sách an toàn
        "approval.approve", "approval.deny",
        "guard.protect.add", "guard.protect.rm", "guard.integrity.accept",
        "config.set", "auth.reset", "hub.shutdown", "estop.set", "team.estop",
        # vòng đời / cấu hình agent (tool_policy, permission_mode, mcp_servers… là bề mặt nâng quyền)
        "agent.create", "agent.update", "agent.delete", "agent.restore", "agent.purge", "agent.clone", "agent.import",
        "agent.create_from_template", "mcp.agent_create",
        # học (H-05): propose bị HẠ xuống created_by=agent thay vì từ chối — xem `DOWNGRADE`
        "learn.propose", "learn.rollback", "learn.retry", "learn.refine", "learn.policy_set", "learn.review_cancel",
        "knowledge.pin", "curation.rollback", "eval.post_apply",
        # golden task = chính sách an toàn (Q-07)
        "golden.add", "golden.rm", "golden.update", "golden.restore", "golden.dismiss", "golden.from_run",
        # secret + registry MCP (server MCP chạy NGOÀI sandbox → thêm server = thoát sandbox)
        "secret.set", "secret.rm", "mcp.add", "mcp.update", "mcp.remove", "mcp.restore", "mcp.import", "mcp.assign",
        # thao tác phá huỷ workspace / tiếp quản / team
        "run.revert", "run.takeover", "run.takeover_record",
        "team.create", "team.update", "team.archive", "team.restore", "team.member_set", "team.member_rm",
        # V2-N1: bề mặt quản trị luồng CLI thêm sau (revert về revision cũ = nâng quyền; purge = xoá vĩnh viễn; archive/pause = DoS)
        "agent.archive", "agent.revert", "agent.pause", "agent.resume", "trash.restore", "trash.purge", "task.archive",
        # Telegram (chat/rpc.py): token bot, allowlist chat, gửi tin — agent không được tự ghép chat của nó vào allowlist
        "telegram.setup", "telegram.pending", "telegram.allow", "telegram.revoke", "telegram.disable", "telegram.test",
    }
)
DOWNGRADE = frozenset({"learn.propose"})

# Method GHI mà agent (trong run) ĐƯỢC gọi — mỗi method phải có lý do. Method ghi mới phải nằm ở HUMAN_ONLY hoặc ở đây
# (test `test_every_mutating_method_is_classified` fail nếu chưa phân loại — tránh lặp lỗi phối hợp kiểu V2-N1).
AGENT_ALLOWED_MUTATING: dict[str, str] = {
    "run.start": "lead/agent điều phối khởi chạy run khác — vẫn chịu guard/budget/ESTOP",
    "run.continue": "tiếp hội thoại — run mới vẫn chịu guard",
    "run.resume": "tiếp run gián đoạn — run mới vẫn chịu guard",
    "run.steer": "gửi tin vào run — không đổi quyền",
    "run.stop": "dừng run — an toàn (không phá dữ liệu)",
    "mcp.run": "tool MCP hub_run — như run.start",
    "mcp.team_run": "chạy task team — như task.create",
    "mcp.cancel_task": "huỷ task — trạng thái task, không phá dữ liệu",
    "task.create": "lead tạo task con",
    "task.comment": "bình luận task",
    "task.block": "worker báo bị chặn",
    "task.unblock": "gỡ chặn task",
    "task.link": "nối DAG (chống chu trình ở service)",
    "task.retry": "thử lại task — chịu circuit breaker/budget",
    "feedback.record": "Signal — audit ghi actor=agent",
    "learn.signal_add": "Signal — audit ghi actor=agent",
    "learn.canary_check": "chỉ đánh giá canary theo luật, không áp thay đổi của người",
    "regression.check": "chỉ rollback entry tự động (không bao giờ entry của người — H-04)",
    "eval.run": "chạy golden trên bản sao home (không đổi golden)",
    "eval.batch": "như eval.run",
    "eval.check": "eval gate trên bản sao",
    "digest.run": "tạo digest/Slack — không đổi chính sách",
    "curator.run": "curator pha 1 — qua Ledger, chịu eval gate",
    "curation.run": "curator liên agent — qua pipeline (created_by=curator)",
    "curation.phase2": "như curation.run",
    "curation.tick": "như curation.run",
    "agent.export": "xuất cấu hình (secret chỉ là tham chiếu ${NAME})",
}


def all_mutating() -> frozenset[str]:
    """Hợp mọi danh sách method GHI của các luồng (bọc audit)."""
    out: set[str] = set()
    for mod, name in (("agent_hub.mcp_server.rpc", "MUTATING"), ("agent_hub.mcp_server.surface_rpc", "MUTATING_SURFACE"),
                      ("agent_hub.learning.rpc", "MUTATING_M4A"), ("agent_hub.learning_ops.rpc", "MUTATING_B"),
                      ("agent_hub.curation.rpc", "MUTATING_CURATION"), ("agent_hub.chat.rpc", "MUTATING_TELEGRAM")):
        try:
            import importlib

            out |= set(getattr(importlib.import_module(mod), name))
        except (ImportError, AttributeError):  # pragma: no cover
            continue
    return frozenset(out)


def require_human(what: str) -> None:
    """Cổng ở TẦNG SERVICE (V2-N1): gọi trong ngữ cảnh RPC của người gọi không phải người → Blocked.
    Ngoài ngữ cảnh RPC (vòng nền của daemon, vd retention) thì cho qua."""
    c = CURRENT_CALLER.get()
    if c is not None and not c.human:
        from .errors import Blocked

        raise Blocked(f"{what}: chỉ người được làm (người gọi: {'agent' if c.is_agent else 'thiếu token'})", data={"caller": c.to_dict()})


# ============================================================================ token
def token_path(sock_path: str | os.PathLike[str]) -> Path:
    p = Path(sock_path)
    return p.with_name(p.name + ".auth")


def issue_token(sock_path: str | os.PathLike[str]) -> str:
    """Sinh token mới (mỗi lần daemon khởi tạo), ghi 0600 không theo symlink. Trả token."""
    path = token_path(sock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok)
    os.replace(tmp, path)
    return tok


def load_token(sock_path: str | os.PathLike[str] | None) -> str | None:
    if not sock_path:
        return None
    try:
        p = token_path(sock_path)
        st = os.lstat(p)
        if not os.path.isfile(p) or os.path.islink(p) or st.st_uid != os.getuid():
            return None
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def token_ok(given: Any, expected: str | None) -> bool:
    return bool(expected) and isinstance(given, str) and hmac.compare_digest(given.encode(), expected.encode())  # type: ignore[union-attr]


# ============================================================================ tiến trình
def peer_pid(sock: Any) -> int | None:
    """pid của tiến trình đầu kia Unix socket (do kernel báo, không giả được từ payload)."""
    try:
        if sys.platform == "darwin":
            pid = sock.getsockopt(0, 0x002)  # SOL_LOCAL, LOCAL_PEERPID
            return int(pid) if pid else None
        if hasattr(socket, "SO_PEERCRED"):
            pid, _uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            return int(pid) if pid else None
    except (OSError, AttributeError, struct.error):
        return None
    return None


_libc: Any = None


def _darwin_libc() -> Any:
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    return _libc


@dataclass
class ProcInfo:
    pid: int
    ppid: int
    start: float | None = None


def proc_info(pid: int) -> ProcInfo | None:
    if pid <= 0:
        return None
    if sys.platform == "darwin":
        try:
            buf = ctypes.create_string_buffer(136)
            n = _darwin_libc().proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(3), ctypes.c_uint64(0), buf, ctypes.c_int(136))  # PROC_PIDTBSDINFO
            if n < 136:
                return None
            f = struct.unpack_from("<12I16s32s5IiQQ", buf.raw)
            return ProcInfo(pid=f[3], ppid=f[4], start=float(f[20]) + f[21] / 1e6)
        except (OSError, AttributeError, struct.error):
            return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        rest = raw[raw.rindex(")") + 2 :].split()
        ppid = int(rest[1])
        try:
            ticks = os.sysconf("SC_CLK_TCK")
            boot = next(float(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime"))
            start = boot + int(rest[19]) / ticks
        except (OSError, ValueError, StopIteration):
            start = None
        return ProcInfo(pid=pid, ppid=ppid, start=start)
    except (OSError, ValueError, IndexError):
        return None


def ancestors(pid: int, limit: int = 128) -> list[int]:
    out: list[int] = []
    cur = pid
    seen: set[int] = set()
    while cur > 1 and cur not in seen and len(out) < limit:
        seen.add(cur)
        out.append(cur)
        info = proc_info(cur)
        if info is None:
            break
        cur = info.ppid
    return out


def proc_environ(pid: int) -> dict[str, str] | None:
    """Biến môi trường của tiến trình cùng user (macOS: sysctl KERN_PROCARGS2; Linux: /proc/<pid>/environ)."""
    try:
        if sys.platform == "darwin":
            libc = _darwin_libc()
            argmax = ctypes.c_int(0)
            sz = ctypes.c_size_t(ctypes.sizeof(argmax))
            mib_max = (ctypes.c_int * 2)(1, 8)  # CTL_KERN, KERN_ARGMAX
            if libc.sysctl(mib_max, 2, ctypes.byref(argmax), ctypes.byref(sz), None, 0) != 0 or argmax.value <= 0:
                return None
            size = ctypes.c_size_t(argmax.value)
            mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
            buf = ctypes.create_string_buffer(argmax.value)
            if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
                return None
            raw = buf.raw[: size.value]
            argc = struct.unpack_from("<i", raw)[0]
            parts = raw[4:].split(b"\0")
            i = 1  # bỏ exec path
            while i < len(parts) and parts[i] == b"":
                i += 1
            i += argc
            env_parts = parts[i:]
        else:
            env_parts = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        env: dict[str, str] = {}
        for item in env_parts:
            if b"=" in item:
                k, _, v = item.partition(b"=")
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        return env
    except (OSError, ValueError, struct.error, AttributeError):
        return None


# ============================================================================ người gọi
@dataclass
class Caller:
    via: str = "internal"  # internal | socket
    pid: int | None = None
    token_ok: bool = False
    run_id: str | None = None
    agent_id: str | None = None
    evidence: list[str] = field(default_factory=list)

    @property
    def internal(self) -> bool:
        return self.via == "internal"

    @property
    def is_agent(self) -> bool:
        return bool(self.evidence)

    @property
    def human(self) -> bool:
        return self.internal or (self.token_ok and not self.is_agent)

    def to_dict(self) -> dict[str, Any]:
        return {"via": self.via, "pid": self.pid, "token_ok": self.token_ok, "run_id": self.run_id, "agent_id": self.agent_id,
                "evidence": self.evidence[:5], "human": self.human}


def telegram_caller(hub: "Hub", chat_id: Any) -> Caller:
    """Người gọi qua bot Telegram (chat/bot.py, chạy TRONG daemon — không qua socket nên không có pid ngang hàng).

    Là NGƯỜI chỉ khi bot đang bật và `chat_id` nằm trong allowlist `notify.telegram.allowed_chat_ids` — allowlist chỉ
    người sửa được (`telegram.allow` ∈ HUMAN_ONLY; config.yaml nhánh `notify` được kiểm toàn vẹn). Chat ngoài allowlist
    → không phải người (HUMAN_ONLY bị từ chối như thiếu token). Kênh audit: `telegram:<chat_id>`.
    """
    try:
        cfg = hub.config.get("notify.telegram") or {}
    except Exception:  # pragma: no cover
        cfg = {}
    allowed = {str(x) for x in (cfg.get("allowed_chat_ids") or [])}
    ok = bool(cfg.get("enabled", True)) and str(chat_id) in allowed
    return Caller(via=f"telegram:{chat_id}", token_ok=ok)


CURRENT_CALLER: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("agent_hub_caller", default=None)


def current_caller() -> Caller | None:
    return CURRENT_CALLER.get()


class CallerResolver:
    """Xác định người gọi từ pid ngang hàng. Dùng trạng thái run của hub (pid + session) — xem docstring module."""

    RECENT_S = 24 * 3600

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    def _run_pids(self) -> dict[int, tuple[str, str]]:
        out: dict[int, tuple[str, str]] = {}
        for ar in list(getattr(self.hub.runs, "active", {}).values()):
            h = getattr(ar, "handle", None)
            if h is not None and getattr(h, "pid", None):
                out[int(h.pid)] = (ar.run_id, ar.agent_id)
        return out

    def _recent_runs(self) -> dict[int, tuple[str, str, float | None]]:
        """pid (= session id, vì spawn start_new_session) của run trong 24 h qua — bắt hậu duệ mồ côi sau khi run kết thúc."""
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - self.RECENT_S))
        out: dict[int, tuple[str, str, float | None]] = {}
        try:
            rows = self.hub.store.conn.execute(
                "SELECT id, agent_id, pid, pid_start_time FROM runs WHERE pid IS NOT NULL AND started_at >= ?", (since,)
            ).fetchall()
        except Exception:  # pragma: no cover
            return out
        for r in rows:
            try:
                out[int(r[2])] = (r[0], r[1], float(r[3]) if r[3] is not None else None)
            except (TypeError, ValueError):
                continue
        return out

    def resolve(self, pid: int | None, token: Any, expected_token: str | None) -> Caller:
        c = Caller(via="socket", pid=pid, token_ok=token_ok(token, expected_token))
        if pid is None:
            if sys.platform in ("darwin", "linux"):  # nền tảng hỗ trợ mà không lấy được pid → không tin là người
                c.evidence.append("peer_pid_unknown")
            return c
        if pid == os.getpid():
            return c  # client trong chính tiến trình daemon (test in-process) — không phải hậu duệ run
        live = self._run_pids()
        chain = ancestors(pid)
        for p in chain:
            if p in live:
                c.run_id, c.agent_id = live[p]
                c.evidence.append(f"descendant_of_run_pid:{p}")
                break
        try:
            sid = os.getsid(pid)
        except OSError:
            sid = None
        if sid and sid not in chain:
            if sid in live:
                c.run_id, c.agent_id = c.run_id or live[sid][0], c.agent_id or live[sid][1]
                c.evidence.append(f"session_of_run:{sid}")
            else:
                recent = self._recent_runs().get(sid)
                if recent is not None:
                    leader = proc_info(sid)
                    # leader còn sống nhưng khởi động khác lúc run → pid đã bị tái dùng (vd shell của người) → bỏ qua
                    if leader is None or recent[2] is None or leader.start is None or abs(leader.start - recent[2]) <= 5:
                        c.run_id, c.agent_id = c.run_id or recent[0], c.agent_id or recent[1]
                        c.evidence.append(f"session_of_recent_run:{sid}")
        env = proc_environ(pid)
        if env and env.get(ENV_RUN_ID):
            c.run_id = c.run_id or env[ENV_RUN_ID]
            c.evidence.append("env_run_id")
        if c.run_id and not c.agent_id:
            try:
                row = self.hub.store.get_run(c.run_id)
                c.agent_id = row["agent_id"] if row else None
            except Exception:  # pragma: no cover
                pass
        return c
