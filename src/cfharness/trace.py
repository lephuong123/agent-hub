"""Mã yêu cầu `req_id` — xương sống nối một thao tác qua RPC → service → run → hook → event → log → audit (L2,
docs/dev-notes/plan-logging.md §3).

- Định dạng `r_<ULID>`: 26 ký tự Crockford base32 (48 bit thời gian ms + 80 bit ngẫu nhiên) → sắp được theo thời gian. Chỉ stdlib.
- Contextvar `REQ_CTX` = `{req_id, parent_req_id, channel, principal}`; `bind(...)` (context manager) / `set_ctx` + `reset`
  đặt cho một lượt xử lý; `current()` / `current_req_id()` đọc.
- Client gắn `params._trace = {"req_id": …}` (`outgoing`); daemon nhận qua `accept` — CHỈ nhận đúng định dạng, sai/thiếu → sinh mới.
- `TraceFilter` (logging) chép `req_id`/`channel` từ contextvar vào bản ghi log.

req_id CHỈ là mã đối chiếu, không mang quyền gì: client (kể cả agent) tự khai được nó, nên KHÔNG BAO GIỜ dùng req_id để quyết định
quyền, danh tính hay chống trùng.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import os
import re
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

TRACE_PARAM = "_trace"
ENV_REQ_ID = "CFHARNESS_REQ_ID"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
REQ_ID_LEN = 28  # "r_" + 26
_REQ_RE = re.compile(r"r_[0-7][0-9A-HJKMNP-TV-Z]{25}")  # ký tự đầu ≤ 7: 128 bit trong 26 ký tự base32
_CHANNEL_RE = re.compile(r"[A-Za-z0-9_.:@ \-]{1,64}")

REQ_CTX: contextvars.ContextVar[dict[str, str | None] | None] = contextvars.ContextVar("cfharness_req_ctx", default=None)

F = TypeVar("F", bound=Callable[..., Any])


def _ulid(ms: int | None = None) -> str:
    t = int(time.time() * 1000) if ms is None else int(ms)
    n = ((t & ((1 << 48) - 1)) << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 31])
        n >>= 5
    return "".join(reversed(out))


def new_req_id(ms: int | None = None) -> str:
    return "r_" + _ulid(ms)


def valid_req_id(v: Any) -> bool:
    """Đúng `r_` + 26 ký tự Crockford (chữ HOA, không I/L/O/U), dài đúng 28 — kiểm độ dài TRƯỚC regex."""
    return isinstance(v, str) and len(v) == REQ_ID_LEN and _REQ_RE.fullmatch(v) is not None


def req_id_ms(v: str) -> int | None:
    """Mốc thời gian (ms epoch) trong req_id; None nếu sai định dạng."""
    if not valid_req_id(v):
        return None
    n = 0
    for ch in v[2:12]:
        n = n * 32 + _CROCKFORD.index(ch)
    return n


def _clean_channel(v: Any) -> str | None:
    return v if isinstance(v, str) and _CHANNEL_RE.fullmatch(v) else None


def current() -> dict[str, str | None] | None:
    return REQ_CTX.get()


def current_req_id() -> str | None:
    c = REQ_CTX.get()
    return (c or {}).get("req_id") or None


def set_ctx(req_id: str | None = None, *, parent_req_id: str | None = None, channel: str | None = None,
            principal: str | None = None) -> contextvars.Token:
    """Đặt ngữ cảnh (req_id sai định dạng → sinh mới). Trả token cho `reset`."""
    rid = req_id if valid_req_id(req_id) else new_req_id()
    parent = parent_req_id if valid_req_id(parent_req_id) and parent_req_id != rid else None
    return REQ_CTX.set({"req_id": rid, "parent_req_id": parent, "channel": _clean_channel(channel), "principal": _clean_channel(principal)})


def reset(token: contextvars.Token) -> None:
    try:
        REQ_CTX.reset(token)
    except ValueError:  # best-effort: token của context khác (task đã copy context) — bỏ qua, context đó tự hết
        pass


@contextmanager
def bind(req_id: str | None = None, *, parent_req_id: str | None = None, channel: str | None = None,
         principal: str | None = None) -> Iterator[dict[str, str | None]]:
    tok = set_ctx(req_id, parent_req_id=parent_req_id, channel=channel, principal=principal)
    try:
        yield REQ_CTX.get() or {}
    finally:
        reset(tok)


@contextmanager
def child(channel: str, parent_req_id: str | None = None) -> Iterator[dict[str, str | None]]:
    """Điểm vào NỘI BỘ của daemon (nhịp điều phối viên, lịch, dispatcher, auto-resume): req_id mới, cha = req_id đang bind (nếu có)."""
    with bind(None, parent_req_id=parent_req_id or current_req_id(), channel=channel) as c:
        yield c


def traced(channel: str) -> Callable[[F], F]:
    """Decorator: mỗi lời gọi chạy trong `child(channel)` (hàm thường hoặc async)."""
    def deco(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def aw(*a: Any, **k: Any) -> Any:
                with child(channel):
                    return await fn(*a, **k)
            return aw  # type: ignore[return-value]

        @functools.wraps(fn)
        def w(*a: Any, **k: Any) -> Any:
            with child(channel):
                return fn(*a, **k)
        return w  # type: ignore[return-value]
    return deco


# ---------------------------------------------------------------------------- client ↔ daemon
def outgoing(params: dict[str, Any] | None, req_id: str | None = None) -> dict[str, Any]:
    """Bản sao params có `_trace.req_id` (req_id truyền vào > đang bind > sinh mới). Đã có `_trace` hợp lệ → giữ."""
    p = dict(params or {})
    t = p.get(TRACE_PARAM)
    if isinstance(t, dict) and valid_req_id(t.get("req_id")):
        return p
    rid = req_id if valid_req_id(req_id) else (current_req_id() or new_req_id())
    p[TRACE_PARAM] = {"req_id": rid}
    return p


def accept(raw: Any) -> str:
    """req_id do client gửi (`params._trace`) nếu đúng định dạng; ngược lại sinh mới. Không bao giờ ném lỗi."""
    rid = raw.get("req_id") if isinstance(raw, dict) else None
    return rid if valid_req_id(rid) else new_req_id()


def from_env() -> str | None:
    v = os.environ.get(ENV_REQ_ID)
    return v if valid_req_id(v) else None


class TraceFilter(logging.Filter):
    """Chép `req_id`/`channel`/`parent_req_id` của contextvar vào bản ghi (không ghi đè giá trị bản ghi đã có)."""

    def filter(self, record: logging.LogRecord) -> bool:
        c = REQ_CTX.get()
        if c:
            if c.get("req_id") and not getattr(record, "req_id", None):
                record.req_id = c["req_id"]
            if c.get("channel") and not getattr(record, "channel", None):
                record.channel = c["channel"]
            if c.get("parent_req_id") and not getattr(record, "parent_req_id", None):
                record.parent_req_id = c["parent_req_id"]  # L6: `trace` tìm lệnh con (điểm vào nội bộ, agent gọi trong run)
        return True


__all__ = ["REQ_CTX", "TRACE_PARAM", "ENV_REQ_ID", "new_req_id", "valid_req_id", "req_id_ms", "current", "current_req_id", "set_ctx",
           "reset", "bind", "child", "traced", "outgoing", "accept", "from_env", "TraceFilter"]
