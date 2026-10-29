"""Điều phối viên (M7 MGR-CORE) — `hub.manager`: chỉ định, hội thoại liên tục theo kênh, nhịp heartbeat, tool hub-self.

- **Chỉ định**: `hub_state.manager = {agent_id, set_at, by, channel, history:[…]}` — chỉ người (`manager.set` ∈ HUMAN_ONLY +
  `auth.require_human` ở tầng service). Lưu trong hub.db (agent không ghi được) chứ không ở config.yaml.
- **Hội thoại** (`manager_conversations`, `manager_messages`, migration v13): mỗi tin người → một run `kind=manager` của agent
  manager, `--resume` cùng session của conversation (claude tự autocompact khi ngữ cảnh gần đầy); sau `manager.rotate_after_turns`
  lượt (hoặc run lỗi) → session mới, mang theo vài tin gần nhất. cwd cố định `agents/<id>/workspaces/conv-<id>` (resume cần cùng cwd).
- **Nhịp**: listener bus gom sự kiện đáng xử lý (task blocked/done, circuit open, run lỗi lặp, run do manager khởi chạy xong,
  digest) + vòng nền (`manager.check_s`) thêm approval chờ người > N phút; tick khi có sự kiện đã chờ `event_debounce_s`, hoặc
  định kỳ `tick_interval_s` CHỈ khi có việc (task do manager giao chưa xong, approval chờ, sự kiện). Không tick khi ESTOP, agent
  pause/archive, hoặc đang có run manager (khoá chống chồng — sự kiện giữ lại cho tick sau).
- **Tool hub-self `manager`** → `self_call` (RPC `managerself.call`): ràng run_id ↔ người gọi, op ∈ MANAGER_TOOLS, kiểm quyền
  (`permissions.py`), thực hiện qua `Daemon.call_as(manager_caller)` (daemon kiểm lại MANAGER_ALLOWED + trường cấm, bọc audit
  actor `manager:<agent>`), sửa cấu hình agent qua `Ledger.apply` (actor `manager:<agent>` = thay đổi tự động, rollback được).
  Mọi thao tác ghi → event `manager.action{tool, target, summary, audit_id, ok, error?}`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import auth as _auth
from ..errors import Blocked, Conflict, HubError, NotFound
from ..events import TERMINAL_RUN_EVENTS, HubEvent, iso, make_event
from ..store import now_iso
from . import prompts
from .permissions import ManagerPermissions

if TYPE_CHECKING:
    from ..daemon import Daemon
    from ..hub import Hub

log = logging.getLogger("cfharness.manager")

HEARTBEAT_CHANNEL = "heartbeat"
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_MAX_COUNT = 4
# Phím tắt của NGƯỜI trong hội thoại (tin `manager.chat` = HUMAN_ONLY): trả lời câu hỏi đang mở / duyệt approval mà không cần lệnh CLI
_APPROVE_RE = re.compile(r"^\s*[!/]?\s*(?:cfharness\s+|agent-hub\s+)?(approve|allow|duyệt|duyet|cho\s*phép|deny|reject|từ\s*chối|tu\s*choi)"  # rename:keep
                         r"\s+(ap_[0-9a-f]{6,})\b\s*(.*)$", re.I | re.S)
_ANSWER_RE = re.compile(r"^\s*[!/]?\s*(?:cfharness\s+)?(?:answer|trả\s*lời)\s+(q_[0-9a-f]{6,})\s+([\d,\s]+)\s*$", re.I)
_KEYS_RE = re.compile(r"^\s*\d(\s*[, ]\s*\d)*\s*\.?\s*$")
_YES = {"ok", "oke", "okay", "okee", "yes", "y", "có", "co", "đồng ý", "dong y", "duyệt", "duyet", "duyệt đi", "approve", "cho phép", "cho phep",
        "được", "duoc", "ừ", "uh", "làm đi", "ok làm đi", "approve tiếp đi", "duyệt tiếp đi", "đồng ý đi", "cho phép đi"}
_NO = {"không", "khong", "no", "n", "deny", "từ chối", "tu choi", "thôi", "thoi", "đừng", "dung", "không cho", "khong cho"}
# duyệt KHÔNG kèm mã: chỉ khi CẢ tin là một câu duyệt ngắn (không bắt "duyệt giúp t kế hoạch này")
_APPROVE_ONLY = {"duyệt", "duyet", "duyệt đi", "duyet di", "duyệt tiếp", "duyệt tiếp đi", "duyệt luôn", "ok duyệt", "ok duyệt đi", "approve",
                 "approve đi", "approve tiếp", "approve tiếp đi", "approve it", "cho phép", "cho phep", "cho phép đi", "cho phép tiếp đi"}
_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))


def _image_ext(data: bytes) -> str | None:
    for magic, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def decode_images(images: Any) -> list[tuple[str, bytes]]:
    """`[{name, data_b64}]` → `[(ext, bytes)]`. Định dạng theo magic bytes (không tin tên/đuôi người gửi); sai → HubError, không ghi gì."""
    if not images:
        return []
    if not isinstance(images, list):
        raise HubError("'images' phải là danh sách")
    if len(images) > IMAGE_MAX_COUNT:
        raise HubError(f"tối đa {IMAGE_MAX_COUNT} ảnh mỗi tin")
    out = []
    for i, im in enumerate(images, 1):
        raw = im.get("data_b64") if isinstance(im, dict) else None
        if not isinstance(raw, str) or len(raw) > IMAGE_MAX_BYTES * 4 // 3 + 8:
            raise HubError(f"ảnh #{i}: thiếu dữ liệu hoặc quá {IMAGE_MAX_BYTES // 1024 // 1024} MB")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise HubError(f"ảnh #{i}: base64 không hợp lệ") from None
        ext = _image_ext(data)
        if ext is None or len(data) > IMAGE_MAX_BYTES:
            raise HubError(f"ảnh #{i}: chỉ nhận PNG/JPEG/GIF/WebP ≤ {IMAGE_MAX_BYTES // 1024 // 1024} MB")
        out.append((ext, data))
    return out
FILE_MAX_BYTES = 10 * 1024 * 1024  # trần CỨNG tệp đính kèm (văn bản/tài liệu) (kênh có thể đặt trần nhỏ hơn, vd notify.telegram.document_max_bytes)
FILE_MAX_COUNT = 4
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_file_name(name: Any, default: str = "file.txt") -> str:
    """Tên tệp do người/kênh gửi → tên an toàn (không thư mục, không `..`, ASCII, ≤ 60 ký tự)."""
    import unicodedata

    base = re.split(r"[\\/]", str(name or ""))[-1].replace("đ", "d").replace("Đ", "D")
    base = "".join(c for c in unicodedata.normalize("NFKD", base) if not unicodedata.combining(c))  # "ghi chú" → "ghi chu"
    s = _SAFE_NAME.sub("_", base).strip("._-")[:60].strip("._-")
    return s or default


def decode_files(files: Any, max_bytes: int = FILE_MAX_BYTES) -> list[tuple[str, bytes]]:
    """`[{name, data_b64}]` → `[(tên an toàn, bytes)]`. Nhận tệp VĂN BẢN (UTF-8, không byte NUL) hoặc TÀI LIỆU PDF/Office nhận diện
    theo nội dung (`doc_extract.doc_ext`; tên an toàn giữ đúng đuôi chuẩn). Sai → HubError, không ghi gì."""
    from .doc_extract import doc_ext

    if not files:
        return []
    if not isinstance(files, list):
        raise HubError("'files' phải là danh sách")
    if len(files) > FILE_MAX_COUNT:
        raise HubError(f"tối đa {FILE_MAX_COUNT} tệp mỗi tin")
    cap = min(int(max_bytes), FILE_MAX_BYTES)
    out = []
    for i, fl in enumerate(files, 1):
        raw = fl.get("data_b64") if isinstance(fl, dict) else None
        if not isinstance(raw, str) or len(raw) > cap * 4 // 3 + 8:
            raise HubError(f"tệp #{i}: thiếu dữ liệu hoặc quá {cap // 1024} KB")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise HubError(f"tệp #{i}: base64 không hợp lệ") from None
        if len(data) > cap:
            raise HubError(f"tệp #{i}: quá {cap // 1024} KB")
        ext = doc_ext(str(fl.get("name") or ""), data)
        if ext:
            safe = safe_file_name(fl.get("name"), "file" + ext)
            if not safe.lower().endswith(ext):
                safe += ext
            out.append((safe, data))
            continue
        if not is_text_bytes(data):
            raise HubError(f"tệp #{i}: chỉ nhận tệp văn bản (UTF-8) hoặc PDF/Excel/Word/PowerPoint")
        out.append((safe_file_name(fl.get("name")), data))
    return out


def is_text_bytes(data: bytes) -> bool:
    """Tệp văn bản = UTF-8 hợp lệ, không byte NUL."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


LIVE = ("queued", "running", "waiting_human")
TEXT_MAX = 4000


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _trunc(v: Any, n: int = 600) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"… (+{len(v) - n} chars)"
    if isinstance(v, dict):
        return {k: _trunc(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_trunc(x, n) for x in v[:60]]
    return v


class ManagerService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.perms = ManagerPermissions(hub)
        self.daemon: "Daemon | None" = None  # gắn ở manager/rpc.register_rpc (call_as)
        self.pending: list[dict[str, Any]] = []
        self._pending_keys: set[str] = set()
        self._first_pending: float | None = None
        self._reported_approvals: set[str] = set()
        self._fail_times: dict[str, list[float]] = {}
        self._tick_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._run_keys: dict[str, str] = {}
        hub.bus.add_listener(self._on_event)

    # ------------------------------------------------------------------ cấu hình
    def cfg(self, key: str, default: Any = None) -> Any:
        try:
            v = self.hub.config.get(f"manager.{key}")
        except KeyError:
            return default
        return default if v is None else v

    @property
    def conn(self):
        return self.hub.store.conn

    # ------------------------------------------------------------------ chỉ định (HUMAN_ONLY)
    def state(self) -> dict[str, Any]:
        return dict(self.hub.store.state_get("manager") or {})

    def agent_id(self) -> str | None:
        return self.state().get("agent_id") or None

    def is_manager(self, agent_id: str | None) -> bool:
        return bool(agent_id) and agent_id == self.agent_id()

    # ---- MGR-3: run nào được vai trò manager (do hội thoại/tick tạo, hoặc tiếp nối nội bộ của chúng)
    ISSUED_KEY = "manager_issued_runs"

    def mark_issued(self, run_id: str, conversation_id: str | None) -> None:
        st = dict(self.hub.store.state_get(self.ISSUED_KEY) or {})
        st[run_id] = conversation_id
        if len(st) > 2000:
            st = dict(list(st.items())[-2000:])
        self.hub.store.state_set(self.ISSUED_KEY, st)

    def is_issued(self, run_id: str | None) -> bool:
        return bool(run_id) and run_id in (self.hub.store.state_get(self.ISSUED_KEY) or {})

    def run_key(self, run_id: str, *, create: bool = True) -> str | None:
        """R2-3: khoá của tiêu đề tin thật cho MỘT run manager — ngẫu nhiên 128 bit, chỉ ở RAM, đăng ký che theo giá trị (DB/event/
        raw log/RPC/web/MCP chỉ thấy `***`). Prompt lưu DB chỉ có `prompts.KEY_PLACEHOLDER`; `_prepare_spec` thay lúc spawn."""
        k = self._run_keys.get(run_id)
        if k is None and create:
            from ..redact import register_secret_values

            k = secrets.token_hex(16)
            register_secret_values(k)
            self._run_keys[run_id] = k
            if len(self._run_keys) > 500:
                self._run_keys.pop(next(iter(self._run_keys)))
        return k

    def render_prompt(self, run_id: str, prompt: str) -> str:
        return prompt.replace(prompts.KEY_PLACEHOLDER, self.run_key(run_id) or "") if prompts.KEY_PLACEHOLDER in prompt else prompt

    def agent_target_block(self, method: str, p: dict[str, Any]) -> str | None:
        """MGR-3: người gọi KHÔNG phải người (agent/thiếu token) không được điều khiển run/agent điều phối viên (continue/resume/steer/
        stop/start, giao task cho manager). Chỉ người hoặc chính cơ chế hội thoại/tick của hub."""
        mids = self.perms.manager_ids()
        if not mids:
            return None
        if method in ("run.continue", "run.resume", "run.steer", "run.stop", "run.takeover", "mcp.cancel_task"):
            ref = p.get("id") or p.get("run_id") or p.get("ref")
            run = self.hub.store.find_run(str(ref)) if ref else None
            if run is None and ref:
                r = self.conn.execute("SELECT agent_id FROM runs WHERE session_id=? LIMIT 1", (str(ref),)).fetchone()
                aid = r[0] if r else (str(ref) if str(ref) in mids else None)
            else:
                aid = (run or {}).get("agent_id")
            if aid in mids:
                return "run của điều phối viên chỉ người (hoặc hội thoại/nhịp của hub) được tiếp tục/điều khiển"
        if method in ("run.start", "mcp.run", "mcp.agent_run") and str(p.get("agent") or p.get("agent_id") or "") in mids:
            return "chỉ người được khởi chạy run cho điều phối viên (dùng manager.chat)"
        if method in ("task.create", "mcp.team_run"):
            t = p.get("task") if isinstance(p.get("task"), dict) else p
            if str(t.get("assignee") or "") in mids:
                return "không giao task cho điều phối viên"
        return None

    def set(self, agent_id: str | None, *, by: str = "user", channel: str = "cli") -> dict[str, Any]:
        _auth.require_human("chỉ định điều phối viên")  # cổng tầng service (ngoài HUMAN_ONLY ở RPC)
        st = self.state()
        prev = st.get("agent_id")
        if agent_id:
            row = self.hub.agents.get_row(agent_id)  # NotFound nếu không có / đã archive
            if row.get("archived_at"):
                raise HubError(f"agent '{agent_id}' đã bị xoá mềm")
            why = self.perms.manager_config_problem(row)  # MGR-8: chỉ agent có tool/quyền trong khung điều phối viên
            if why:
                raise Blocked(f"không chỉ định được '{agent_id}' làm điều phối viên: {why}", data={"reason": "manager_config"})
        hist = [x for x in (st.get("history") or []) if x]
        if agent_id and agent_id not in hist:
            hist.append(agent_id)
        st.pop("key", None)
        st.update({"agent_id": agent_id or None, "set_at": now_iso(), "by": by, "channel": channel, "history": hist[-20:]})
        self.hub.store.state_set("manager", st)
        self.hub.publish(make_event("manager.designated", agent_id or prev, {"agent_id": agent_id or None, "previous": prev, "by": by, "channel": channel}))
        return self.get()

    def capabilities_info(self) -> dict[str, Any]:
        """§9: khung quyền hiện hành + cấu hình agent manager có vừa khung không (cho `cfharness manager capabilities show`)."""
        from .permissions import CAP_PROFILES, fmt_tools

        caps = self.perms.capabilities()
        try:
            raw = self.hub.config.get("manager.capabilities")
        except KeyError:
            raw = None
        aid = self.agent_id()
        row = self.hub.store.get_agent(aid) if aid else None
        return {"config": raw, "effective": {**caps, "tools": fmt_tools(caps["tools"]).split("/") if caps["tools"] else []},
                "profiles": CAP_PROFILES, "manager": aid,
                "manager_tools": ((row or {}).get("tool_policy") or {}).get("tools") if row else None,
                "problem": self.perms.manager_config_problem(row) if row else None}

    def get(self) -> dict[str, Any]:
        tk = self.hub.store.state_get("manager_tick") or {}
        aid = self.agent_id()
        active = self._active_runs(aid) if aid else []
        return {
            "agent_id": aid,
            "conversations": self.conversations(None, limit=50),
            "last_tick": tk.get("last_tick"),
            "next_tick": self._next_tick(tk) if aid and self.cfg("heartbeat", True) else None,
            "tick": {"heartbeat": bool(self.cfg("heartbeat", True)), "interval_s": float(self.cfg("tick_interval_s", 1800)),
                     "pending_events": len(self.pending), "last_tick_run_id": tk.get("last_run_id"), "last_reason": tk.get("last_reason"),
                     "last_skip": tk.get("last_skip"), "running": any((r.get("run_opts") or {}).get("manager_kind") == "tick" for r in active)},
            "designated_at": self.state().get("set_at"),
        }

    # ------------------------------------------------------------------ hội thoại
    def _conv_row(self, cid: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM manager_conversations WHERE id=?", (cid,)).fetchone()
        if r is None:
            raise NotFound(f"không có hội thoại '{cid}'")
        return dict(r)

    def _conv_out(self, c: dict[str, Any]) -> dict[str, Any]:
        n = self.conn.execute("SELECT COUNT(*) FROM manager_messages WHERE conversation_id=?", (c["id"],)).fetchone()[0]
        return {"id": c["id"], "channel": c["channel"], "agent_id": c["agent_id"], "session_id": c.get("session_id"), "turns": c.get("turns"),
                "title": c.get("title"), "messages": int(n), "last_run_id": c.get("last_run_id"), "created_at": c["created_at"], "updated_at": c["updated_at"]}

    def conversations(self, channel: str | None = None, *, limit: int = 100, all_managers: bool = False) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM manager_conversations WHERE archived_at IS NULL", []
        if not all_managers:  # MGR-11: mặc định chỉ hội thoại của điều phối viên HIỆN TẠI (web/Telegram không mở nhầm hội thoại cũ)
            sql += " AND agent_id=?"
            args.append(self.agent_id() or "")
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        return [self._conv_out(dict(r)) for r in self.conn.execute(sql, args)]

    def history(self, cid: str, limit: int = 100) -> list[dict[str, Any]]:
        self._conv_row(cid)
        rows = self.conn.execute("SELECT * FROM (SELECT * FROM manager_messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                                 (cid, int(limit))).fetchall()
        return [{"id": r["id"], "role": r["role"], "text": r["text"], "run_id": r["run_id"], "ts": r["ts"]} for r in rows]

    def _new_conv(self, channel: str, agent_id: str, title: str | None) -> dict[str, Any]:
        cid = "mc_" + secrets.token_hex(5)
        now = now_iso()
        self.conn.execute("INSERT INTO manager_conversations (id, channel, agent_id, title, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                          (cid, channel, agent_id, (title or "")[:80] or None, now, now))
        return self._conv_row(cid)

    def _add_msg(self, cid: str, role: str, text: str, run_id: str | None) -> None:
        cur = self.conn.execute("INSERT INTO manager_messages (conversation_id, role, text, run_id, ts) VALUES (?,?,?,?,?)",
                                (cid, role, text[:20000], run_id, now_iso()))
        self.conn.execute("UPDATE manager_conversations SET updated_at=? WHERE id=?", (now_iso(), cid))
        try:  # v17: chỉ mục hội thoại (session_search.py) — đã che secret trước khi vào FTS
            from ..session_search import indexer_for

            indexer_for(self.hub.store).index_manager_row(int(cur.lastrowid))
        except Exception:  # pragma: no cover - lỗi chỉ mục không chặn hội thoại
            log.exception("không chỉ mục được tin điều phối viên")

    def _save_images(self, agent_id: str, cid: str, imgs: list[tuple[str, bytes]]) -> list[str]:
        """Ảnh người gửi → `<cwd hội thoại>/attachments/` (0600, tên do hub đặt). Trả đường dẫn tương đối theo cwd."""
        d = Path(self._conv_cwd(agent_id, cid)) / "attachments"
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = []
        for i, (ext, data) in enumerate(imgs, 1):
            name = f"img-{stamp}-{secrets.token_hex(3)}-{i}.{ext}"
            fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            out.append(f"attachments/{name}")
        return out

    def _save_files(self, agent_id: str, cid: str, docs: list[tuple[str, bytes]]) -> tuple[list[str], dict[str, list[str]]]:
        """Tệp người gửi → `<cwd hội thoại>/attachments/doc-<thời điểm>-<ngẫu nhiên>-<tên an toàn>` (0600, O_EXCL).
        Tài liệu Office trích được (xlsx/docx/pptx) → thêm bản đọc được cạnh bản gốc (`<tên>.<n>-<sheet>.csv` / `<tên>.txt`).
        Trả (đường dẫn bản gốc tương đối theo cwd, {bản gốc: [bản trích]})."""
        from .doc_extract import doc_ext, extract

        d = Path(self._conv_cwd(agent_id, cid)) / "attachments"
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out: list[str] = []
        extracted: dict[str, list[str]] = {}

        def write(name: str, data: bytes) -> str:
            fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return f"attachments/{name}"

        for safe, data in docs:
            name = f"doc-{stamp}-{secrets.token_hex(3)}-{safe}"
            rel = write(name, data)
            out.append(rel)
            ext = doc_ext(safe, data)
            if ext and ext != ".pdf":  # PDF: Read đọc thẳng
                extracted[rel] = [write(name + suffix, body) for suffix, body in extract(ext, data)]
        return out, extracted

    def _conv_cwd(self, agent_id: str, cid: str) -> str:
        home = Path(self.hub.agents.get_row(agent_id)["home_path"])
        d = home / "workspaces" / f"conv-{cid}"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def _active_runs(self, agent_id: str | None, cid: str | None = None) -> list[dict[str, Any]]:
        if not agent_id:
            return []
        rows = self.conn.execute(f"SELECT id FROM runs WHERE agent_id=? AND kind='manager' AND status IN ({','.join('?' * len(LIVE))})",
                                 (agent_id, *LIVE)).fetchall()
        out = [self.hub.store.get_run(r[0]) or {} for r in rows]
        if cid:
            out = [r for r in out if (r.get("run_opts") or {}).get("manager_conversation") == cid]
        return out

    def _submit(self, conv: dict[str, Any], prompt: str, *, kind: str, budget: float | None) -> dict[str, Any]:
        aid = conv["agent_id"]
        why = self.perms.manager_config_problem(self.hub.agents.get_row(aid))  # MGR-8: cấu hình bị nới sau khi chỉ định → không chạy
        if why:
            raise Blocked(f"điều phối viên '{aid}' có cấu hình vượt khung (`cfharness manager capabilities show`): {why}", data={"reason": "manager_config"})
        resume = conv.get("session_id")
        carry = ""
        if resume and int(conv.get("session_turns") or 0) >= int(self.cfg("rotate_after_turns", 80)):
            resume = None  # xoay session: claude autocompact lo ngữ cảnh dài trong session; ở đây giới hạn số lượt/session
        if resume and not self.hub.store.get_session(resume):
            resume = None
        if not resume and int(conv.get("turns") or 0) > 0:
            carry = prompts.carry_over(self.history(conv["id"], limit=6))
        opts = {"hub_self": "manager", "manager_conversation": conv["id"], "manager_kind": kind}
        run = self.hub.runs.submit(aid, carry + prompt, cwd=self._conv_cwd(aid, conv["id"]), budget_usd=budget, kind="manager",
                                   resume_session_id=resume, resume_reason="manager_conversation" if resume else None, run_opts=opts,
                                   manager_issue=True)
        st = 0 if not resume else int(conv.get("session_turns") or 0)
        self.conn.execute("UPDATE manager_conversations SET session_id=?, last_run_id=?, session_turns=?, updated_at=? WHERE id=?",
                          (run["session_id"], run["id"], st, now_iso(), conv["id"]))
        return run

    async def chat(self, channel: str, text: str, *, conversation_id: str | None = None, reuse_latest: bool = False,
                   images: list[dict[str, Any]] | None = None, files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        aid = self.agent_id()
        if not aid:
            raise Conflict("chưa có điều phối viên — chỉ định bằng `cfharness manager set <agent>`")
        text = str(text or "").strip()
        channel = str(channel or "cli")
        imgs = decode_images(images)  # kiểm TRƯỚC mọi ghi (định dạng theo magic bytes, kích thước, số lượng)
        docs = decode_files(files)  # tệp văn bản UTF-8 hoặc PDF/Office (nhận theo nội dung), ≤ FILE_MAX_BYTES, tên do hub làm sạch
        if not text and (imgs or docs):
            text = "(xem ảnh đính kèm)" if imgs and not docs else "(xem tệp đính kèm)"
        if not text:
            raise HubError("thiếu 'text'")
        if channel == HEARTBEAT_CHANNEL:
            raise HubError("kênh 'heartbeat' dành cho nhịp tự động")
        conv = None
        switched_from = None
        if conversation_id:
            conv = self._conv_row(conversation_id)
            if conv["channel"] != channel:
                raise Conflict(f"hội thoại {conversation_id} thuộc kênh '{conv['channel']}', không phải '{channel}'")
            if conv["agent_id"] != aid:
                # MGR-11: đã đổi điều phối viên → chuyển sang hội thoại của manager mới trên cùng kênh (UI dùng conversation_id trả về)
                switched_from, conv, reuse_latest = conversation_id, None, True
        if conv is None and reuse_latest:
            r = self.conn.execute("SELECT id FROM manager_conversations WHERE channel=? AND agent_id=? AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 1",
                                  (channel, aid)).fetchone()
            conv = self._conv_row(r[0]) if r else None
        if conv is None:
            conv = self._new_conv(channel, aid, text.splitlines()[0])
        saved_imgs = self._save_images(aid, conv["id"], imgs) if imgs else []
        saved_docs, extracted = self._save_files(aid, conv["id"], docs) if docs else ([], {})
        files = saved_imgs + saved_docs
        prompt_text = text + (prompts.images_note(saved_imgs) if saved_imgs else "") + (prompts.files_note(saved_docs, extracted) if saved_docs else "")
        if saved_imgs:
            text = f"{text}\n📎 {len(saved_imgs)} ảnh: " + ", ".join(f.rsplit("/", 1)[-1] for f in saved_imgs)
        if saved_docs:
            text = f"{text}\n📎 {len(saved_docs)} tệp: " + ", ".join(f.rsplit("/", 1)[-1] for f in saved_docs)
        busy = self._active_runs(aid, conv["id"])
        if busy and busy[0]["status"] != "running":
            r = busy[0]
            raise Conflict(f"điều phối viên đang xử lý tin trước của hội thoại này (run {r['id']}, {r['status']}) — thử lại sau")
        note = await self._owner_shortcut(channel, conv, text) if not files else None
        if note:
            prompt_text = f"{prompt_text}\n\n{note}"
        if busy:
            # run của hội thoại đang chạy → gửi tin vào run đó (steer) thay vì chồng run thứ hai cùng session
            from ..steer import steer

            r = busy[0]
            try:
                res = await steer(self.hub, r["id"], self.render_prompt(r["id"], prompts.chat_prompt(channel, conv["id"], prompt_text)), by="user", channel=channel)
            except Conflict:
                raise Conflict(f"điều phối viên đang kết thúc lượt trả lời trước (run {r['id']}) — gửi lại sau giây lát") from None
            self._add_msg(conv["id"], "user", text, r["id"])
            return {"conversation_id": conv["id"], "run_id": r["id"], "steered": True, "seq": (res or {}).get("seq")}
        run = self._submit(conv, prompts.chat_prompt(channel, conv["id"], prompt_text), kind="chat", budget=self.cfg("chat_budget_usd"))
        self._add_msg(conv["id"], "user", text, run["id"])
        self.conn.execute("UPDATE manager_conversations SET turns=turns+1 WHERE id=?", (conv["id"],))
        out = {"conversation_id": conv["id"], "run_id": run["id"], "session_id": run["session_id"], "resumed": bool(run.get("resumed_from_session"))}
        if switched_from:
            out["switched_from"] = switched_from
        return out

    # ------------------------------------------------------------------ phím tắt của người trong chat
    async def _owner_shortcut(self, channel: str, conv: dict[str, Any], text: str) -> str | None:
        """Tin NGƯỜI là câu trả lời ngắn cho câu hỏi đang mở của hội thoại ("1", "ok", "không", nhãn phương án) hoặc lệnh duyệt
        (`approve ap_…`, `duyệt`) → hub làm NGAY với quyền người, trả ghi chú (chữ của HUB) gắn vào lượt chat. Không khớp → None."""
        low = " ".join(text.lower().split()).rstrip(".!")
        try:
            m = _APPROVE_RE.match(text)
            if m:
                return await self._decide_approval(m.group(2), m.group(1).lower(), m.group(3).strip(), channel)
            m = _ANSWER_RE.match(text)
            if m:
                keys = [k for k in re.split(r"[,\s]+", m.group(2)) if k]
                return await self.hub.questions.answer_in_chat(m.group(1), keys, channel=channel)
            if len(low) > 40:
                return None
            qs = [q for q in self.hub.questions.list("open", limit=50) if q.get("conversation_id") == conv["id"]]
            if len(qs) == 1:
                keys = self._keys_for(qs[0], low)
                if keys:
                    return await self.hub.questions.answer_in_chat(qs[0]["id"], keys, channel=channel)
            if not qs and low in _APPROVE_ONLY:
                pend = self.hub.approvals.list("pending", limit=10)
                if len(pend) == 1:
                    return await self._decide_approval(pend[0]["id"], "approve", "", channel)
                if len(pend) > 1:
                    return ("[hub] Người muốn duyệt nhưng có " + str(len(pend)) + " yêu cầu đang chờ: " + ", ".join(a["id"] for a in pend)
                            + ". Hỏi người cái nào bằng ask_owner_permission (kind approve_action) cho từng yêu cầu.")
        except HubError as e:
            return f"[hub] Không xử lý được lệnh của người: {e.message}"
        return None

    @staticmethod
    def _keys_for(q: dict[str, Any], low: str) -> list[str]:
        opts = q.get("options") or []
        if _KEYS_RE.match(low):
            keys = [k for k in re.split(r"[,\s.]+", low) if k]
            return keys if all(any(o["key"] == k for o in opts) for k in keys) else []
        for o in opts:
            if low == o["label"].lower():
                return [o["key"]]
        if q.get("action") or len(opts) == 2:  # câu hỏi có/không (xin phép: 1 = Cho phép, 2 = Từ chối)
            if low in _YES:
                return [opts[0]["key"]] if opts else []
            if low in _NO and len(opts) >= 2:
                return [opts[1]["key"]]
        return []

    async def _decide_approval(self, approval_id: str, verb: str, note: str, channel: str) -> str:
        ap = self.hub.approvals.get(approval_id)
        allow = not any(verb.startswith(w) for w in ("deny", "reject", "từ", "tu"))
        if ap["status"] not in ("open", "waiting_human"):
            return f"[hub] Approval {ap['id']} đã {ap['status']} trước đó — không đổi."
        if self.daemon is None:
            raise HubError("cần daemon để duyệt")
        params = {"id": ap["id"], "by": "user", "channel": channel, "_audit": {"channel": channel, "actor": "user"}}
        if note:
            params["note"] = note[:300]
        resp = await self.daemon.call_as(_auth.Caller(via="internal"), "approval.approve" if allow else "approval.deny", params)
        if "error" in resp:
            return f"[hub] Duyệt {ap['id']} lỗi: {resp['error'].get('message')}"
        return (f"[hub] Người đã {'CHO PHÉP' if allow else 'TỪ CHỐI'} approval {ap['id']} ({ap['agent_id']}: {ap['tool_name']}). "
                + ("Run của agent tự chạy tiếp; theo dõi tới khi xong." if allow else "Không thử lại thao tác đó."))

    def _home_conversation(self) -> dict[str, Any] | None:
        """Hội thoại của NGƯỜI hoạt động gần nhất (đích của notify_owner trong tick)."""
        aid = self.agent_id()
        r = self.conn.execute("SELECT id FROM manager_conversations WHERE agent_id=? AND channel != ? AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 1",
                              (aid, HEARTBEAT_CHANNEL)).fetchone()
        return self._conv_row(r[0]) if r else None

    # ------------------------------------------------------------------ kết thúc run → manager.reply
    def _on_event(self, ev: HubEvent) -> None:
        try:
            if ev.type in TERMINAL_RUN_EVENTS and ev.source == "hub" and ev.run_id:
                run = self.hub.store.get_run(ev.run_id) or {}
                if (run.get("run_opts") or {}).get("manager_conversation") and ev.type != "run.waiting_human" and self.is_issued(run["id"]):
                    self._finish_run(run)
                    return
            if ev.type == "agent.purged" and ev.agent_id:  # MGR-12: dọn trạng thái manager gắn theo id
                self.perms.forget_agent(ev.agent_id)
            self._collect(ev)
        except Exception:  # pragma: no cover — listener không được làm hỏng bus
            log.exception("manager listener lỗi với %s", ev.type)

    def _finish_run(self, run: dict[str, Any]) -> None:
        opts = run.get("run_opts") or {}
        cid = opts["manager_conversation"]
        try:
            conv = self._conv_row(cid)
        except NotFound:
            return
        outcome = run.get("outcome") or run.get("status")
        ok = outcome == "completed"
        if ok:
            text = str(run.get("summary") or "").strip() or "(không có nội dung)"
        else:
            text = f"(Điều phối viên không trả lời được: run {run['id']} kết thúc '{outcome}'" + (f" — {str(run.get('error'))[:300]}" if run.get("error") else "") + ")"
        self._add_msg(cid, "manager", text, run["id"])
        sess = run.get("session_id") if ok or outcome == "cancelled" else None  # lỗi → session mới lần sau (mang theo tóm tắt)
        self.conn.execute("UPDATE manager_conversations SET session_id=?, session_turns=session_turns+1, last_run_id=?, updated_at=? WHERE id=?",
                          (sess, run["id"], now_iso(), cid))
        kind = opts.get("manager_kind") or "chat"
        if ok:
            # manager đã nhìn trạng thái khi soạn câu trả lời → bỏ sự kiện "xong" xảy ra trước đó (không tốn một tick chỉ để NO_ACTION)
            end = str(run.get("ended_at") or now_iso())
            keep = [e for e in self.pending if not (e["kind"] in ("task_done", "run_done") and str(e["ts"]) <= end)]
            if len(keep) != len(self.pending):
                self.pending = keep
                if not keep:
                    self._first_pending = None
        self.hub.publish(make_event("manager.reply", run["agent_id"], {"conversation_id": cid, "run_id": run["id"], "text": text[:TEXT_MAX],
                                                                       "channel": conv["channel"], "kind": "tick" if kind == "tick" else "reply", "ok": ok,
                                                                       "outcome": outcome}, run_id=run["id"]))
        if kind == "tick":
            tk = dict(self.hub.store.state_get("manager_tick") or {})
            tk.update({"last_result": text[:300], "last_outcome": outcome})
            self.hub.store.state_set("manager_tick", tk)

    # ------------------------------------------------------------------ nhịp: gom sự kiện
    def _push(self, kind: str, key: str, summary: str, **data: Any) -> None:
        if key in self._pending_keys:
            return
        self._pending_keys.add(key)
        if self._first_pending is None:
            self._first_pending = time.monotonic()
        self.pending.append({"kind": kind, "summary": summary[:300], "ts": now_iso(), **data})

    def wake_soon(self) -> None:
        """Sự kiện cần điều phối viên xử lý sớm (approval giao cho nó): bỏ thời gian gom `event_debounce_s` — vòng nền (`check_s`)
        tick ở lần kiểm kế tiếp (vẫn khoá chống chồng, không chạy khi ESTOP/tạm dừng)."""
        if self.pending:
            self._first_pending = time.monotonic() - float(self.cfg("event_debounce_s", 45)) - 1.0

    def _collect(self, ev: HubEvent) -> None:
        aid = self.agent_id()
        if not aid or ev.agent_id == aid:
            return
        p = ev.payload or {}
        if ev.type == "task.status":
            to = p.get("to")
            t = self.hub.tasks.maybe(ev.task_id) if ev.task_id else None
            if t is None:
                return
            if to == "blocked":
                qs = getattr(self.hub, "questions", None)
                if qs is not None and qs.pending_for_task(t["id"]):
                    return  # UX: task chờ câu trả lời — đã có sự kiện `question` (với manager) hoặc đã hỏi người
                self._push("task_blocked", f"tb:{t['id']}:{t.get('updated_at')}", f"task {t['id']} '{t['title'][:80]}' BLOCKED: {p.get('reason') or t.get('block_reason') or ''}",
                           task_id=t["id"], team_id=t.get("team_id"))
                from ..guard.profiles import gates_mode

                if gates_mode(self.hub.config) != "human":
                    self.wake_soon()  # người giao hết cổng cho điều phối viên → quyết ngay, không đợi gom sự kiện
            elif to == "done" and str(t.get("created_by") or "").startswith("schedule:"):
                return  # task do lịch tạo: kết quả đã gửi về kênh của lịch (schedules/) — không tốn một nhịp chỉ để báo xong
            elif to == "done" and (not t.get("origin_task_id") or str(t.get("created_by") or "").startswith("manager:")):
                self._push("task_done", f"td:{t['id']}", f"task {t['id']} '{t['title'][:80]}' DONE: {str(t.get('result_summary') or '')[:200]}",
                           task_id=t["id"], team_id=t.get("team_id"))
        elif ev.type == "task.circuit_open":
            self._push("task_circuit_open", f"tc:{ev.task_id}:{ev.seq}", f"task {ev.task_id}: circuit breaker opened (repeated failures)", task_id=ev.task_id)
        elif ev.type in ("run.failed",) and ev.source == "hub":
            oc = p.get("outcome")
            if oc in ("crashed", "failed", "timed_out", "protocol_violation") and ev.agent_id:
                now = time.monotonic()
                ts = [x for x in self._fail_times.get(ev.agent_id, []) if now - x < 3600] + [now]
                self._fail_times[ev.agent_id] = ts
                if len(ts) >= int(self.cfg("crash_repeat", 2)):
                    self._push("run_failing", f"rf:{ev.agent_id}:{len(ts) // int(self.cfg('crash_repeat', 2))}",
                               f"agent {ev.agent_id}: {len(ts)} failed runs in the last hour (last {ev.run_id}: {oc})", agent_id=ev.agent_id, run_id=ev.run_id)
        elif ev.type == "run.completed" and ev.source == "hub" and ev.run_id:
            tracked = set((self.hub.store.state_get("manager_tracked") or {}).get("runs") or [])
            if ev.run_id in tracked:
                self._push("run_done", f"rd:{ev.run_id}", f"run {ev.run_id} of {ev.agent_id} finished: {str(p.get('summary') or '')[:200]}", run_id=ev.run_id)
        elif ev.type == "learning.digest":
            self._push("digest", f"dg:{p.get('id') or ev.seq}", "a new learning digest is available (digest_get)")

    def _scan_approvals(self) -> None:
        mins = float(self.cfg("approval_wait_min", 15))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=mins)
        for a in self.hub.approvals.list("pending", limit=50):
            if a["id"] in self._reported_approvals or a.get("routed_to") == "manager":
                continue  # giao cho điều phối viên: đã có sự kiện approval_delegated
            t = _parse(a.get("opened_at"))
            if t is not None and t <= cutoff:
                self._reported_approvals.add(a["id"])
                self._push("approval_waiting", f"ap:{a['id']}", f"approval {a['id']} ({a.get('agent_id')}: {a.get('tool_name')} `{str(a.get('preview') or '')[:120]}`) "
                           f"waiting for the human > {int(mins)} min", approval_id=a["id"])

    # ------------------------------------------------------------------ nhịp: vòng nền + tick
    def start(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, float(self.cfg("check_s", 10))))
            try:
                await self.maybe_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("manager tick lỗi")

    def _next_tick(self, tk: dict[str, Any]) -> str | None:
        last = _parse(tk.get("last_check") or tk.get("last_tick"))
        if last is None:
            return iso()
        return iso(last + timedelta(seconds=float(self.cfg("tick_interval_s", 1800))))

    def _open_work(self) -> int:
        """Việc manager đang theo dõi: task do manager giao chưa xong (không tính blocked — đã báo bằng sự kiện)."""
        r = self.conn.execute("SELECT COUNT(*) FROM tasks WHERE created_by LIKE 'manager:%' AND status NOT IN ('done','archived','blocked') AND deleted_at IS NULL").fetchone()
        return int(r[0])

    def _skip(self, reason: str) -> dict[str, Any]:
        tk = dict(self.hub.store.state_get("manager_tick") or {})
        if tk.get("last_skip") != reason:
            tk["last_skip"] = reason
            self.hub.store.state_set("manager_tick", tk)
        return {"run_id": None, "skipped": reason, "pending_events": len(self.pending)}

    async def maybe_tick(self, *, force: bool = False) -> dict[str, Any]:
        aid = self.agent_id()
        if not aid:
            return {"run_id": None, "skipped": "no_manager"}
        if not force and not self.cfg("heartbeat", True):
            return self._skip("heartbeat_off")
        self._scan_approvals()
        if self.hub.runs.estop.get("on"):
            return self._skip("estop")
        row = self.hub.store.get_agent(aid)
        if row is None or row.get("archived_at"):
            return self._skip("manager_missing")
        if row.get("status") == "paused":
            return self._skip("manager_paused")
        async with self._tick_lock:
            if self._active_runs(aid):
                return self._skip("manager_busy")  # khoá chống chồng: sự kiện giữ lại cho tick sau
            tk = dict(self.hub.store.state_get("manager_tick") or {})
            now = datetime.now(timezone.utc)
            due_events = bool(self.pending) and (force or (time.monotonic() - (self._first_pending or 0)) >= float(self.cfg("event_debounce_s", 45)))
            nxt = _parse(self._next_tick(tk))
            periodic = nxt is not None and now >= nxt
            if not (due_events or periodic or force):
                return {"run_id": None, "skipped": "not_due", "pending_events": len(self.pending)}
            work = self._open_work()
            if not self.pending and not work and not force:
                tk.update({"last_check": iso(now), "last_skip": "nothing_to_do"})
                self.hub.store.state_set("manager_tick", tk)
                self.hub.publish(make_event("manager.tick", aid, {"status": "skipped", "reason": "nothing_to_do"}))
                return {"run_id": None, "skipped": "nothing_to_do"}
            return self._tick(aid, tk, reason="events" if self.pending else ("forced" if force else "periodic"), open_work=work)

    def _tick(self, aid: str, tk: dict[str, Any], *, reason: str, open_work: int) -> dict[str, Any]:
        cap = int(self.cfg("max_pending_events", 40))
        events, dropped = self.pending[:cap], max(0, len(self.pending) - cap)
        r = self.conn.execute("SELECT id FROM manager_conversations WHERE channel=? AND agent_id=? AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 1",
                              (HEARTBEAT_CHANNEL, aid)).fetchone()
        conv = self._conv_row(r[0]) if r else self._new_conv(HEARTBEAT_CHANNEL, aid, "heartbeat")
        from ..guard.profiles import gates_mode

        prompt = prompts.tick_prompt(events, dropped=dropped, reason=reason, open_work=open_work, gates=gates_mode(self.hub.config))
        try:
            run = self._submit(conv, prompt, kind="tick", budget=self.cfg("tick_budget_usd"))
        except HubError as e:
            self.hub.publish(make_event("manager.tick", aid, {"status": "failed", "reason": e.message[:200]}))
            return self._skip("submit_failed: " + e.message[:80])
        self.pending, self._pending_keys, self._first_pending = [], set(), None
        self._add_msg(conv["id"], "tick", prompts.tick_digest(events, dropped, reason), run["id"])
        self.conn.execute("UPDATE manager_conversations SET turns=turns+1 WHERE id=?", (conv["id"],))
        now = iso()
        tk.update({"last_tick": now, "last_check": now, "last_run_id": run["id"], "last_reason": reason, "last_skip": None, "events": len(events)})
        self.hub.store.state_set("manager_tick", tk)
        self.hub.publish(make_event("manager.tick", aid, {"status": "started", "reason": reason, "events": len(events), "dropped": dropped,
                                                         "conversation_id": conv["id"]}, run_id=run["id"]))
        return {"run_id": run["id"], "conversation_id": conv["id"], "reason": reason, "events": len(events)}

    def run_socket_block(self, caller: "_auth.Caller", method: str) -> str | None:
        """§9: tiến trình con của run điều phối viên (Bash, script… — khung hermes) gọi THẲNG socket hub (khi sandbox không chặn) →
        chỉ `managerself.call` (cùng luật lớp 1+2 như tool hub-self). Method ghi khác bị từ chối: không dùng quyền "agent thường"
        (run.start/task.create… không trần) để lách trần/luật của manager. Daemon gọi cho mọi method ghi/HUMAN_ONLY từ socket."""
        if not caller.is_agent or not caller.run_id or method == "managerself.call" or method in _auth.SCOPED_READ:
            # SCOPED_READ: đọc thuần (tìm hội thoại/memory) — tự giới hạn phạm vi theo người gọi, không phải thao tác ghi
            return None
        if self.is_issued(caller.run_id):
            return ("tiến trình trong run điều phối viên chỉ thao tác hub qua tool hub-self (managerself.call) — "
                    "không gọi thẳng socket/CLI")
        return None

    # ------------------------------------------------------------------ kiểm quyền (daemon, lớp 2)
    def check_rpc(self, caller: "_auth.Caller", method: str, params: dict[str, Any]) -> str | None:
        if not self.is_manager(caller.manager):
            return f"'{caller.manager}' không còn là điều phối viên được chỉ định"
        return self.perms.check_rpc(caller, method, params)

    # ------------------------------------------------------------------ tool hub-self (RPC managerself.call)
    async def self_call(self, run_id: str, op: str, args: dict[str, Any]) -> Any:
        from .ops import ManagerOps

        run = self.hub.store.get_run(str(run_id or ""))
        if run is None or run["status"] not in ("running", "queued"):
            raise HubError("run không đang chạy — tool điều phối viên chỉ dùng trong run đang chạy")
        if (run.get("run_opts") or {}).get("hub_self") != "manager" or not self.is_issued(run["id"]):
            raise Blocked("run này không phải run điều phối viên (chỉ run do hội thoại/nhịp của hub tạo)")
        mid = run["agent_id"]
        caller = _auth.current_caller()
        if caller is not None and caller.via == "socket" and caller.run_id != run["id"]:
            # chỉ tiến trình hub-self con của CHÍNH run manager được gọi (pid ngang hàng → cây tiến trình/env)
            info = {"op": op, "claimed_run": run["id"], **caller.to_dict()}
            self.hub.audit.record("manager.denied", caller.agent_id, caller.run_id, info, actor=f"agent:{caller.agent_id}" if caller.agent_id else "unknown",
                                  channel="socket", target=f"run:{run['id']}", summary="managerself.call từ tiến trình không thuộc run manager")
            self.hub.publish(make_event("auth.denied", caller.agent_id, {"method": "managerself.call", **info}, run_id=caller.run_id, source="hub"))
            raise Blocked("managerself.call chỉ dành cho hub-self của chính run điều phối viên")
        if not self.is_manager(mid):
            raise Blocked(f"agent '{mid}' không còn là điều phối viên được chỉ định")
        return await ManagerOps(self, run).call(op, dict(args or {}))
