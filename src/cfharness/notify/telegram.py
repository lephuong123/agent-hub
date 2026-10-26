"""Thông báo Telegram (FR-SG-09 mở rộng — docs/plan/web-telegram.md, luồng TG).

- `TelegramApi`: gọi Bot API (`<api_base>/bot<token>/<method>`) bằng `httpx2` (AsyncClient, huỷ được giữa chừng —
  long polling không giữ luồng khi daemon dừng; `httpx2` đã có sẵn qua phụ thuộc `mcp`, không thêm thư viện).
  Mọi thông báo lỗi được gỡ token trước khi ra log/event.
- `TelegramNotifier`: cùng luồng sự kiện với Slack (`QueuedNotifier`, lọc `notify.telegram.events`), gửi tới MỌI chat
  trong `notify.telegram.allowed_chat_ids`. Tin `approval.open|waiting_human|reminder` kèm nút inline Cho phép / Từ chối.
- `CallbackStore`: dữ liệu nút = `v1.<nonce>.<chữ ký>` — nonce ngẫu nhiên 96 bit lưu phía hub (`hub_state`), gắn với
  hành động + đối tượng + chat_id; chữ ký HMAC-SHA256 (khoá `TELEGRAM_CALLBACK_KEY` trong secret store). Dùng một lần:
  bấm là tiêu (kèm các nút cùng nhóm); bấm lại / dữ liệu lạ / sai chat / sai chữ ký / quá hạn → từ chối + audit.
- Nội dung: văn bản thuần (không parse_mode), đã che secret (`redact_str` + theo giá trị), cắt ngắn; tool MCP chỉ tên.
"""

from __future__ import annotations

import hashlib
import hmac
import html as _html
import json
import logging
import re
import secrets as _secrets
import time
from typing import TYPE_CHECKING, Any

from ..events import HubEvent
from ..redact import redact_str
from .base import QueuedNotifier, clip
from .slack import format_message as _slack_format

if TYPE_CHECKING:
    from ..hub import Hub

try:  # httpx2 (pydantic) là phụ thuộc của mcp; httpx gốc nếu có
    import httpx2 as _httpx
except ImportError:  # pragma: no cover
    import httpx as _httpx  # type: ignore[no-redef]

log = logging.getLogger("cfharness.notify.telegram")
for _n in ("httpx", "httpx2", "httpcore", "httpcore2"):  # log thư viện có URL chứa token → không để mức INFO
    logging.getLogger(_n).setLevel(logging.WARNING)

MAX_TEXT = 3900  # Telegram giới hạn 4096 ký tự/tin
APPROVAL_EVENTS = ("approval.open", "approval.waiting_human", "approval.reminder", "approval.escalated")


class TelegramError(Exception):
    def __init__(self, message: str, *, code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class TelegramApi:
    def __init__(self, base: str, token: str, *, timeout: float = 15.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _scrub(self, s: Any) -> str:
        t = str(s)
        if self.token:
            t = t.replace(self.token, "***")
        return redact_str(t)

    async def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None,
                   files: dict[str, tuple[str, bytes, str]] | None = None) -> Any:
        """`files` (vd sendDocument) → multipart: các tham số khác gửi dạng form (reply_markup… phải là chuỗi JSON)."""
        url = f"{self.base}/bot{self.token}/{method}"
        try:
            async with _httpx.AsyncClient(timeout=timeout or self.timeout) as c:
                if files:
                    form = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in (params or {}).items()}
                    r = await c.post(url, data=form, files=files)
                else:
                    r = await c.post(url, json=params or {})
            try:
                data = r.json()
            except ValueError:
                raise TelegramError(f"{method}: HTTP {r.status_code} (không phải JSON)", code=r.status_code) from None
        except TelegramError:
            raise
        except Exception as e:  # lỗi mạng: thông báo có thể chứa URL (token) → gỡ
            raise TelegramError(f"{method}: {type(e).__name__}: {self._scrub(e)}") from None
        if not isinstance(data, dict) or not data.get("ok"):
            d = data if isinstance(data, dict) else {}
            ra = (d.get("parameters") or {}).get("retry_after")
            raise TelegramError(f"{method}: {self._scrub(d.get('description') or d)}", code=d.get("error_code"), retry_after=ra)
        return data.get("result")

    async def download(self, file_id: str, max_bytes: int, *, timeout: float | None = None) -> bytes:
        """Tải tệp người gửi: `getFile` → GET `<base>/file/bot<token>/<file_path>` (stream, dừng ngay khi vượt `max_bytes`).
        Lỗi (gồm quá cỡ) → TelegramError đã gỡ token."""
        info = await self.call("getFile", {"file_id": str(file_id)}, timeout=timeout)
        fp = (info or {}).get("file_path") if isinstance(info, dict) else None
        size = (info or {}).get("file_size") if isinstance(info, dict) else None
        if not fp or not isinstance(fp, str) or ".." in fp.split("/") or fp.startswith("/"):
            raise TelegramError("getFile: không có file_path hợp lệ")
        if isinstance(size, int) and size > max_bytes:
            raise TelegramError(f"tệp quá lớn ({size // 1024} KB)", code=413)
        url = f"{self.base}/file/bot{self.token}/{fp}"
        try:
            async with _httpx.AsyncClient(timeout=timeout or max(self.timeout, 30.0)) as c:
                async with c.stream("GET", url) as r:
                    if r.status_code != 200:
                        raise TelegramError(f"tải tệp: HTTP {r.status_code}", code=r.status_code)
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise TelegramError(f"tệp quá lớn (> {max_bytes // 1024} KB)", code=413)
                    return bytes(buf)
        except TelegramError:
            raise
        except Exception as e:  # lỗi mạng: URL chứa token → gỡ
            raise TelegramError(f"tải tệp: {type(e).__name__}: {self._scrub(e)}") from None


# ============================================================================ định dạng
_EMOJI = re.compile(r":[a-z0-9_+\-]+:\s*")
_BOLD = re.compile(r"\*([^*\n]+)\*")


def to_plain(text: str) -> str:
    """Tin kiểu Slack (mrkdwn) → văn bản thuần cho Telegram."""
    t = _EMOJI.sub("", text)
    t = _BOLD.sub(r"\1", t)
    return t.replace("`", "")


def fit(text: str, limit: int = MAX_TEXT, hint: str = "") -> str:
    t = redact_str(text)
    if len(t) <= limit:
        return t
    tail = f"\n…(đã cắt{' — ' + hint if hint else ''})"
    return t[: max(0, limit - len(tail))] + tail


def format_approval(p: dict[str, Any], agent: str | None, run: str | None, *, kind: str = "approval.open", heavy: bool = False) -> str:
    rid = p.get("request_id") or p.get("id")
    head = {
        "approval.open": f"[cfharness] Agent {agent or '-'} muốn chạy thao tác PHÁ HUỶ — đang giữ chờ duyệt",
        "approval.waiting_human": f"[cfharness] Yêu cầu {rid} của agent {agent or '-'} chưa có người duyệt → run {run or '-'} chuyển waiting_human (đã dừng tiến trình, giữ khoá repo, KHÔNG tự từ chối)",
        "approval.reminder": f"[cfharness] Nhắc lần {p.get('reminder_count')}: yêu cầu {rid} (agent {agent or '-'}) vẫn chờ người duyệt sau {int(float(p.get('wait_s') or 0) // 60)} phút",
        "approvals.list": f"[cfharness] Yêu cầu {rid} — agent {agent or '-'} đang chờ duyệt ({p.get('status')})",
        "approval.escalated": f"[cfharness] Điều phối viên chuyển yêu cầu {rid} của agent {agent or '-'} lên NGƯỜI duyệt ({clip(p.get('note'), 200)})",
    }.get(kind, f"[cfharness] Yêu cầu {rid}")
    lines = [head, f"• Run: {run or '-'} · Yêu cầu: {rid}", f"• Thao tác: {clip(p.get('preview'), 300)}"]
    if p.get("reason"):
        lines.append(f"• Lý do: {clip(p.get('reason'), 200)} ({p.get('matched_rule')})")
    if heavy:
        lines.append("• Thao tác NẶNG — cho phép cần xác nhận 2 bước.")
    lines.append(f"• Hoặc trên máy: cfharness approve {rid} · cfharness deny {rid}")
    return "\n".join(lines)


def is_heavy(row: dict[str, Any], confirm_classes: list[str] | tuple[str, ...]) -> bool:
    """Thao tác phá huỷ "nặng" (duyệt 2 bước): chạm tài nguyên được bảo vệ hoặc thuộc lớp trong `confirm_classes`."""
    klass = row.get("destructive_class")
    return bool(row.get("protected_resource_id")) or (klass is not None and klass in set(confirm_classes or ()))


# ---------------------------------------------------------------- WT-2 / WT-8: thao tác đầy đủ, khung nội dung agent
_INVISIBLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
CHUNK = 3500  # ký tự nội dung/tin (Telegram ≤ 4096 sau khi bỏ thẻ HTML)
MAX_CHUNKS = 4  # dài hơn → gửi kèm tệp .txt (đầy đủ) + đầu/cuối trong tin


def visible(text: str) -> str:
    """Ký tự điều khiển / bidi / zero-width → ký hiệu nhìn thấy được (không giấu được phần lệnh)."""
    return _INVISIBLE.sub(lambda m: "\n" if m.group(0) == "\n" else f"⟨U+{ord(m.group(0)):04X}⟩", text)


def esc(v: Any) -> str:
    return _html.escape(str(v if v is not None else ""), quote=False)


def full_action(tool_name: str, tool_input: Any) -> str:
    """Thao tác ĐẦY ĐỦ (đã che secret) — không cắt: Bash = toàn bộ lệnh; MCP/tool khác = tên + MỌI tham số."""
    from ..redact import redact

    ti = tool_input if isinstance(tool_input, dict) else {"input": tool_input}
    ti = redact(ti)
    if tool_name == "Bash":
        body = str(ti.get("command") or "")
        extra = {k: v for k, v in ti.items() if k not in ("command", "description")}
        if extra:
            body += "\n# tham số khác: " + json.dumps(extra, ensure_ascii=False, default=str)
    else:
        body = f"{tool_name}\n" + json.dumps(ti, ensure_ascii=False, indent=2, default=str)
    return visible(redact_str(body))


def split_chunks(text: str, n: int = CHUNK) -> list[str]:
    return [text[i : i + n] for i in range(0, len(text), n)] or [""]


def format_event(ev: HubEvent, *, heavy: bool = False) -> str | None:
    p = ev.payload or {}
    if ev.type in APPROVAL_EVENTS:
        return format_approval(p, ev.agent_id, ev.run_id, kind=ev.type, heavy=heavy)
    if ev.type == "learning.digest":
        if not p.get("send") or not p.get("text"):
            return None
        return fit("[cfharness] Digest\n" + to_plain(str(p["text"])), hint="xem đầy đủ: cfharness digest show, TUI hoặc web")
    t = _slack_format(ev)
    return fit(to_plain(t)) if t else None


# ============================================================================ nút inline
class CallbackStore:
    """Nonce dùng một lần cho nút inline. Lưu ở `hub_state` (sống qua restart daemon), chỉ daemon ghi."""

    STATE = "telegram.callbacks"
    MSGS = "telegram.approval_msgs"

    def __init__(self, hub: "Hub", key_fn: Any) -> None:
        self.hub = hub
        self._key_fn = key_fn
        self._recs: dict[str, dict[str, Any]] | None = None

    def _all(self) -> dict[str, dict[str, Any]]:
        if self._recs is None:
            v = self.hub.store.state_get(self.STATE) or {}
            self._recs = v if isinstance(v, dict) else {}
        return self._recs

    def _save(self) -> None:
        self.hub.store.state_set(self.STATE, self._all())

    def _sig(self, nonce: str, rec: dict[str, Any]) -> str:
        key = self._key_fn()
        if not key:
            raise TelegramError("chưa có khoá ký callback")
        msg = f"{nonce}|{rec['a']}|{rec['r']}|{rec['c']}|{rec['g']}".encode()
        return hmac.new(key.encode() if isinstance(key, str) else key, msg, hashlib.sha256).hexdigest()[:16]

    def prune(self) -> None:
        now = time.time()
        recs = self._all()
        dead = [n for n, r in recs.items() if float(r.get("exp") or 0) < now]
        for n in dead:
            recs.pop(n, None)
        if dead:
            self._save()

    def issue(self, buttons: list[tuple[str, str, str]], chat_id: Any, *, ttl: float) -> tuple[dict[str, Any], str]:
        """buttons = [(action, ref, nhãn)] → (reply_markup, group). Các nút cùng nhóm: bấm một là tiêu cả nhóm."""
        return self.issue_rows([buttons], chat_id, ttl=ttl)

    def issue_rows(self, rows: list[list[tuple[str, str, str]]], chat_id: Any, *, ttl: float) -> tuple[dict[str, Any], str]:
        """Như `issue` nhưng nhiều hàng nút (bàn phím câu hỏi). Mọi nút của các hàng cùng MỘT nhóm."""
        self.prune()
        group = "g" + _secrets.token_hex(6)
        kb = []
        for buttons in rows:
            row = []
            for action, ref, label in buttons:
                nonce = _secrets.token_urlsafe(12)
                rec = {"a": action, "r": ref, "c": str(chat_id), "g": group, "exp": time.time() + ttl}
                self._all()[nonce] = rec
                row.append({"text": label, "callback_data": f"v1.{nonce}.{self._sig(nonce, rec)}"})
            if row:
                kb.append(row)
        self._save()
        return {"inline_keyboard": kb}, group

    def consume(self, data: Any, chat_id: Any) -> tuple[dict[str, Any] | None, str]:
        """Kiểm + TIÊU nonce. Trả (rec, "ok") hoặc (None, lý do). Sai chat/chữ ký KHÔNG tiêu (không cho kẻ lạ huỷ nút thật)."""
        if not isinstance(data, str):
            return None, "malformed"
        parts = data.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return None, "malformed"
        _, nonce, sig = parts
        recs = self._all()
        rec = recs.get(nonce)
        if rec is None:
            return None, "unknown_or_used"
        if str(chat_id) != rec.get("c"):
            return None, "wrong_chat"
        try:
            good = hmac.compare_digest(sig, self._sig(nonce, rec))
        except TelegramError:
            good = False
        if not good:
            return None, "bad_signature"
        group = rec.get("g")
        for n in [n for n, r in recs.items() if r.get("g") == group]:
            recs.pop(n, None)
        self._save()
        if float(rec.get("exp") or 0) < time.time():
            return None, "expired"
        return rec, "ok"

    def invalidate_ref(self, ref: str) -> int:
        recs = self._all()
        dead = [n for n, r in recs.items() if r.get("r") == ref]
        for n in dead:
            recs.pop(n, None)
        if dead:
            self._save()
        return len(dead)

    # tin chứa nút của một approval → gỡ nút khi approval được quyết ở kênh khác
    def add_message(self, ref: str, chat_id: Any, message_id: Any) -> None:
        m = self.hub.store.state_get(self.MSGS) or {}
        lst = m.setdefault(ref, [])
        lst.append([str(chat_id), message_id])
        if len(m) > 500:
            for k in list(m)[: len(m) - 500]:
                m.pop(k, None)
        self.hub.store.state_set(self.MSGS, m)

    def pop_messages(self, ref: str) -> list[list[Any]]:
        m = self.hub.store.state_get(self.MSGS) or {}
        out = m.pop(ref, None) or []
        if out:
            self.hub.store.state_set(self.MSGS, m)
        return out

    def count(self) -> int:
        return len(self._all())


# ============================================================================ notifier
class TelegramNotifier(QueuedNotifier):
    """Kênh Telegram trên luồng sự kiện chung. `bot` (chat/bot.py) cấp API, allowlist và kho nút."""

    channel = "telegram"

    def __init__(self, hub: "Hub", bot: Any) -> None:
        super().__init__(hub)
        self.bot = bot

    def target(self) -> dict[str, Any] | None:
        api = self.bot.api()
        chats = self.bot.notify_chats()
        if api is None or not chats:
            return None
        return {"api": api, "chats": sorted(chats)}

    def render(self, ev: HubEvent) -> dict[str, Any] | None:
        p = ev.payload or {}
        aid = p.get("request_id") if ev.type in APPROVAL_EVENTS else None
        heavy = False
        if aid:
            try:
                row = self.hub.approvals.get(aid)
                if row.get("status") not in ("open", "waiting_human"):
                    return None  # đã quyết trước khi kịp gửi
                heavy = is_heavy(row, self.cfg().get("confirm_classes") or [])
            except Exception:
                heavy = bool(p.get("protected_resource_id")) or p.get("destructive_class") in set(self.cfg().get("confirm_classes") or [])
        if aid:
            return {"approval_id": aid, "kind": ev.type, "payload": dict(p), "done": set()}
        text = format_event(ev, heavy=heavy)
        if not text:
            return None
        return {"text": text, "approval_id": None, "done": set()}

    async def _send(self, target: dict[str, Any], ev: HubEvent, msg: Any) -> dict[str, Any]:
        api: TelegramApi = target["api"]
        n = 0
        for chat in target["chats"]:
            if chat in msg["done"]:
                continue
            aid = msg.get("approval_id")
            if aid:
                row = self.hub.approvals.get(aid)
                if row.get("status") not in ("open", "waiting_human"):
                    return {"chats": n, "skipped": "resolved"}
                await self.bot.send_approval(api, chat, row, kind=msg["kind"], payload=msg.get("payload"))
            else:
                await api.call("sendMessage", {"chat_id": chat, "text": msg["text"], "disable_web_page_preview": True})
            msg["done"].add(chat)
            n += 1
        return {"chats": n}
