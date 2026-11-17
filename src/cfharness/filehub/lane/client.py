"""`LaneClient` — client HTTP+SSE của cổng cfharness (`lane/v1`, hợp đồng §C trong docs/dev-notes/plan-filehub-lane.md).

- Chỉ nói chuyện với `http://127.0.0.1:<cổng>` / `http://[::1]:<cổng>` (`check_url` — kiểm khi setup VÀ mỗi lần tạo client): khoá hub
  không bao giờ rời máy.
- Khoá (`tt_lane_…`) chỉ nằm trong header `Authorization` của request; KHÔNG BAO GIỜ trong lỗi/repr/log (`LaneError` dựng chữ từ mã C.7,
  không chép thân phản hồi; `_scrub` gỡ khoá khỏi mọi chuỗi phòng hờ).
- Mã lỗi C.7 → `LaneError(code, status, extra)` (lớp con `HubError`). Lỗi mạng → `code="offline"`, `status=0`.
- `events(cursor)` đọc MỘT kết nối SSE (C.8) và trả từng khung; nối lại + lùi do `LaneService` lo (cần biết cursor đã xử lý xong).
- Đường dẫn chuẩn hoá PHÍA HUB theo C.4 (`norm_rel`) trước khi gửi — agent không chèn được `..`/`.tangthu`/ký tự điều khiển.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from ...errors import HubError

try:  # httpx2 (pydantic) là phụ thuộc của mcp; httpx gốc nếu có
    import httpx2 as _httpx
except ImportError:  # pragma: no cover
    import httpx as _httpx  # type: ignore[no-redef]

PROTOCOL = 1
KEY_PREFIX = "tt_lane_"
KEY_RE = re.compile(r"^tt_lane_[A-Za-z0-9_-]{43}$")
HDR_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SERVER_RE = re.compile(r"^[a-z0-9_]{1,32}$")
PATH_MAX_BYTES = 1024
PATH_MAX_DEPTH = 64
META_PREFIX = ".tangthu"
UPLOAD_MAX_BYTES = 256 * 1024 * 1024  # đẩy từ bộ nhớ (httpx async không gửi tệp đồng bộ có Content-Length) — tệp lớn hơn: báo agent
USER_AGENT = "cfharness-lane/1"

# chữ tiếng Việt cho mã lỗi C.7 (không chép thân phản hồi của server vào lỗi)
ERROR_TEXT = {
    "invalid_input": "yêu cầu không hợp lệ", "hash_mismatch": "sha256 không khớp", "unauthorized": "khoá không hợp lệ / hết hạn / bị thu hồi",
    "lane_disabled": "Không gian số đang tắt cổng cfharness", "lane_not_local": "cổng chỉ nhận kết nối từ chính máy",
    "root_not_granted": "gốc không được cấp cho khoá này", "forbidden": "không có quyền", "read_only_key": "khoá chỉ đọc",
    "not_found": "không có", "conflict": "người đã lưu phiên bản mới hơn", "exists": "đã có mục cùng tên",
    "name_conflict": "đích đã có mục cùng tên", "parent_missing": "thư mục cha chưa có", "length_required": "thiếu Content-Length",
    "precondition_required": "thiếu If-Match/If-None-Match", "too_large": "tệp quá lớn", "invalid_name": "tên/đường dẫn không hợp lệ",
    "root_files_not_allowed": "không được đặt tệp ngay ở gốc workspace", "infected": "tệp bị phát hiện mã độc (đã cách ly)",
    "file_locked": "tệp đang bị khoá trên Không gian số", "rate_limited": "quá nhiều yêu cầu", "too_many_writes": "quá nhiều lệnh ghi đồng thời",
    "scan_unavailable": "máy quét virus tạm thời không chạy", "storage_full": "hết dung lượng", "offline": "không kết nối được cổng",
    "bad_response": "phản hồi không hợp lệ", "version_mismatch": "phiên bản giao thức khác", "not_local_url": "URL cổng phải là loopback",
}
RETRYABLE = frozenset({"file_locked", "rate_limited", "too_many_writes", "scan_unavailable", "offline"})


class LaneError(HubError):
    """Lỗi cổng cfharness theo C.7. `lane_code` = `code` của server (hoặc `offline`/`bad_response` do hub đặt)."""

    def __init__(self, lane_code: str, status: int = 0, extra: dict[str, Any] | None = None, message: str | None = None) -> None:
        self.lane_code = str(lane_code or "bad_response")[:64]
        self.status = int(status or 0)
        self.extra = dict(extra or {})
        text = message or f"cổng cfharness: {ERROR_TEXT.get(self.lane_code, self.lane_code)} ({self.lane_code}{', HTTP ' + str(self.status) if self.status else ''})"
        super().__init__(text, data={"lane_code": self.lane_code, "status": self.status})

    @property
    def retry_after(self) -> float | None:
        v = self.extra.get("retry_after")
        try:
            return max(0.0, float(v)) if v is not None else None
        except (TypeError, ValueError):
            return None

    def __repr__(self) -> str:  # không bao giờ có khoá/thân phản hồi
        return f"LaneError({self.lane_code!r}, {self.status})"


def check_url(url: Any) -> str:
    """URL cổng → dạng chuẩn `http://127.0.0.1:<cổng>` / `http://[::1]:<cổng>`. Ngoài loopback / có đường dẫn, userinfo, query → lỗi."""
    if not isinstance(url, str) or not url:
        raise LaneError("not_local_url", message="cổng cfharness: thiếu URL (vd http://127.0.0.1:8223)")
    try:
        u = urlsplit(url.strip())
        port = u.port
    except ValueError:
        raise LaneError("not_local_url", message=f"cổng cfharness: URL không hợp lệ: {url[:100]!r}") from None
    host = (u.hostname or "").lower()
    bad = (u.scheme != "http" or u.username or u.password or u.query or u.fragment or u.path not in ("", "/") or port is None
           or not (0 < port < 65536))
    try:
        loop = host not in ("", "localhost") and ipaddress.ip_address(host) in (ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1"))
    except ValueError:
        loop = False
    if bad or not loop:
        raise LaneError("not_local_url", message="cổng cfharness: URL phải đúng dạng http://127.0.0.1:<cổng> hoặc http://[::1]:<cổng> "
                                                 "(khoá hub không bao giờ gửi ra ngoài máy)")
    return f"http://[::1]:{port}" if ":" in host else f"http://127.0.0.1:{port}"


def port_of(url: str) -> int | None:
    try:
        return urlsplit(url).port
    except ValueError:
        return None


def valid_key(key: Any) -> bool:
    return isinstance(key, str) and bool(KEY_RE.match(key))


def key_prefix(key: str) -> str:
    return key[:12] if isinstance(key, str) else ""


def norm_rel(path: Any, *, allow_root: bool = True) -> str:
    """Đường dẫn tương đối theo C.4 (chuẩn hoá PHÍA HUB): NFC, `/`, không `/` đầu/cuối, không phần rỗng/`.`/`..`, không `.tangthu*`,
    không ký tự điều khiển, ≤ 1024 byte, ≤ 64 cấp. `""` = chính gốc."""
    if path is None:
        path = ""
    if not isinstance(path, str):
        raise LaneError("invalid_name", 422, message="đường dẫn phải là chuỗi")
    p = unicodedata.normalize("NFC", path).replace("\\", "/") if "\\" not in path else None
    if p is None:
        raise LaneError("invalid_name", 422, message=f"đường dẫn không được chứa '\\': {path[:200]!r}")
    p = p.strip("/") if p in ("/",) else p
    if p == "":
        if not allow_root:
            raise LaneError("invalid_name", 422, message="cần đường dẫn tới một mục (không phải gốc)")
        return ""
    if p.startswith("/") or p.endswith("/"):
        raise LaneError("invalid_name", 422, message=f"đường dẫn phải tương đối, không '/' đầu/cuối: {p[:200]!r}")
    parts = p.split("/")
    for x in parts:
        if x in ("", ".", "..") or x.lower().startswith(META_PREFIX) or any(ord(c) < 32 or ord(c) == 127 or 0x80 <= ord(c) < 0xA0 for c in x):
            raise LaneError("invalid_name", 422, message=f"đường dẫn không hợp lệ: {p[:200]!r}")
    if len(p.encode("utf-8")) > PATH_MAX_BYTES or len(parts) > PATH_MAX_DEPTH:
        raise LaneError("invalid_name", 422, message="đường dẫn quá dài")
    return p


def join_rel(*parts: str) -> str:
    return "/".join(x.strip("/") for x in parts if x and x.strip("/"))


@dataclass
class LaneEvent:
    type: str
    id: str | None
    data: dict[str, Any] = field(default_factory=dict)


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "tt_lane_***") if key else text


class LaneClient:
    """Một client cho một server lane. Khoá chỉ ở thuộc tính riêng `_key`; `repr` không có khoá."""

    def __init__(self, url: str, key: str, *, timeout: float = 60.0, transport: Any = None) -> None:
        self.url = check_url(url)
        if not valid_key(key):
            raise LaneError("unauthorized", message="khoá cổng cfharness không đúng dạng tt_lane_<43 ký tự>")
        self._key = key
        self.timeout = float(timeout)
        self._transport = transport
        self._client: Any = None

    def __repr__(self) -> str:
        return f"LaneClient({self.url}, key={key_prefix(self._key)}…)"

    @property
    def prefix(self) -> str:
        return key_prefix(self._key)

    def _http(self) -> Any:
        if self._client is None:
            kw: dict[str, Any] = {"timeout": self.timeout, "follow_redirects": False, "trust_env": False}
            if self._transport is not None:
                kw["transport"] = self._transport
            self._client = _httpx.AsyncClient(**kw)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover
                pass
            self._client = None

    def _headers(self, agent: str | None = None, run: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT}
        if agent is not None:
            if not HDR_RE.match(agent):
                raise LaneError("invalid_input", message="X-Lane-Agent không hợp lệ")
            h["X-Lane-Agent"] = agent
        if run is not None:
            if not HDR_RE.match(run):
                raise LaneError("invalid_input", message="X-Lane-Run không hợp lệ")
            h["X-Lane-Run"] = run
        h.update(extra or {})
        return h

    def _error(self, resp: Any) -> LaneError:
        body: dict[str, Any] = {}
        try:
            d = resp.json()
            body = d if isinstance(d, dict) else {}
        except Exception:
            pass
        code = str(body.get("code") or "") or {401: "unauthorized", 404: "not_found", 411: "length_required", 413: "too_large",
                                                 423: "file_locked", 428: "precondition_required", 429: "rate_limited",
                                                 503: "scan_unavailable", 507: "storage_full"}.get(resp.status_code, "bad_response")
        if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
            code = "bad_response"
        extra = {k: v for k, v in body.items() if k != "code" and k in ("current", "until", "retry_after", "max_bytes", "quarantined", "scope",
                                                                          "used_bytes", "quota_bytes")}
        if "retry_after" not in extra and resp.headers.get("retry-after"):
            extra["retry_after"] = resp.headers.get("retry-after")
        return LaneError(code, resp.status_code, extra)

    async def _req(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None, content: bytes | None = None,
                   agent: str | None = None, run: str | None = None, headers: dict[str, str] | None = None, raw: bool = False,
                   timeout: float | None = None) -> Any:
        url = f"{self.url}/lane/v1{path}"
        hdr = self._headers(agent, run, headers)
        kw: dict[str, Any] = {"timeout": float(timeout)} if timeout else {}
        try:
            resp = await self._http().request(method, url, params=params, json=json_body, content=content, headers=hdr, **kw)
        except (_httpx.TimeoutException, _httpx.TransportError, OSError) as e:
            raise LaneError("offline", 0, message=f"cổng cfharness: không kết nối được {self.url} ({type(e).__name__})") from None
        if resp.status_code >= 400:
            raise self._error(resp)
        if raw:
            return resp
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            d = resp.json()
        except ValueError:
            raise LaneError("bad_response", resp.status_code) from None
        if not isinstance(d, dict):
            raise LaneError("bad_response", resp.status_code)
        return d

    # ------------------------------------------------------------------ C.3 / C.5 đọc
    async def hello(self) -> dict[str, Any]:
        return await self._req("GET", "/hello")

    async def cursor(self) -> str:
        return str((await self._req("GET", "/cursor")).get("cursor") or "")

    @staticmethod
    def _root(root: str) -> str:
        if not isinstance(root, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", root):
            raise LaneError("root_not_granted", 403, message="id gốc không hợp lệ")
        return root

    async def stat(self, root: str, path: str) -> dict[str, Any]:
        return (await self._req("GET", f"/roots/{self._root(root)}/stat", params={"path": norm_rel(path)})).get("item") or {}

    async def list(self, root: str, path: str = "", *, recursive: bool = False, page: str | None = None, limit: int = 2000) -> dict[str, Any]:
        params: dict[str, Any] = {"path": norm_rel(path), "recursive": 1 if recursive else 0, "limit": max(1, min(2000, int(limit)))}
        if page:
            params["page"] = page
        return await self._req("GET", f"/roots/{self._root(root)}/list", params=params)

    async def list_all(self, root: str, path: str = "", *, recursive: bool = True, max_items: int | None = None) -> tuple[list[dict[str, Any]], str, bool]:
        """Mọi trang của MỘT lượt list (cùng ảnh chụp) → (items, cursor của trang đầu, cắt vì quá `max_items`)."""
        items: list[dict[str, Any]] = []
        first = await self.list(root, path, recursive=recursive)
        cursor = str(first.get("cursor") or "")
        page: dict[str, Any] | None = first
        while page is not None:
            items += [x for x in page.get("items") or [] if isinstance(x, dict)]
            if max_items is not None and len(items) > max_items:
                return items, cursor, True
            nxt = page.get("next_page")
            page = await self.list(root, path, recursive=recursive, page=str(nxt)) if nxt else None
        return items, cursor, False

    async def read(self, root: str, path: str, *, version: str | None = None, max_bytes: int | None = None) -> tuple[bytes, dict[str, str]]:
        """Nội dung tệp (byte thô) + header (ETag, X-Lane-Sha256). Kiểm sha256 khi server gửi. `max_bytes` → `too_large`."""
        params: dict[str, Any] = {"path": norm_rel(path, allow_root=False)}
        if version:
            params["version"] = version
        url = f"{self.url}/lane/v1/roots/{self._root(root)}/content"
        h = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._http().stream("GET", url, params=params, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise self._error(resp)
                async for c in resp.aiter_bytes():
                    total += len(c)
                    if max_bytes is not None and total > max_bytes:
                        raise LaneError("too_large", 413, {"max_bytes": max_bytes})
                    h.update(c)
                    chunks.append(c)
                hdr = {k.lower(): v for k, v in resp.headers.items()}
        except (_httpx.TimeoutException, _httpx.TransportError, OSError) as e:
            raise LaneError("offline", 0, message=f"cổng cfharness: không kết nối được {self.url} ({type(e).__name__})") from None
        want = hdr.get("x-lane-sha256")
        if want and want.lower() != h.hexdigest():
            raise LaneError("hash_mismatch", 0, message="cổng cfharness: nội dung tải về sai sha256")
        return b"".join(chunks), hdr

    async def read_to_file(self, root: str, path: str, dest: str, *, version: str | None = None) -> dict[str, str]:
        """Tải tệp vào `dest` (tạo mới O_EXCL, 0644) — luồng, kiểm sha256. Lỗi → xoá `dest`."""
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        ok = False
        try:
            hdr = await self.read_to_fd(root, path, fd, version=version)
            ok = True
            return hdr
        finally:
            if not ok:
                try:
                    os.unlink(dest)
                except OSError:
                    pass

    async def read_to_fd(self, root: str, path: str, fd: int, *, version: str | None = None) -> dict[str, str]:
        """Tải tệp vào `fd` đã mở để ghi (hàm ĐÓNG fd) — luồng, kiểm sha256. Trả header (chữ thường) + `sha256` đã tính + `version_id` lấy
        từ `ETag` CỦA CHÍNH lượt tải này (bên gọi dựng base từ đây, không `stat` sau — tránh lệch nội dung ↔ phiên bản khi người vừa lưu tiếp).
        Lỗi → ném; bên gọi tự xoá tệp tạm."""
        params: dict[str, Any] = {"path": norm_rel(path, allow_root=False)}
        if version:
            params["version"] = version
        url = f"{self.url}/lane/v1/roots/{self._root(root)}/content"
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as f:
            try:
                async with self._http().stream("GET", url, params=params, headers=self._headers()) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise self._error(resp)
                    async for c in resp.aiter_bytes():
                        h.update(c)
                        f.write(c)
                    hdr = {k.lower(): v for k, v in resp.headers.items()}
            except (_httpx.TimeoutException, _httpx.TransportError) as e:
                raise LaneError("offline", 0, message=f"cổng cfharness: không kết nối được {self.url} ({type(e).__name__})") from None
        want = hdr.get("x-lane-sha256")
        if want and want.lower() != h.hexdigest():
            raise LaneError("hash_mismatch", 0, message="cổng cfharness: nội dung tải về sai sha256")
        hdr["sha256"] = h.hexdigest()
        et = (hdr.get("etag") or "").strip()
        if et.startswith("W/"):
            et = et[2:]
        hdr["version_id"] = et.strip('"')
        return hdr

    async def versions(self, root: str, path: str) -> list[dict[str, Any]]:
        return list((await self._req("GET", f"/roots/{self._root(root)}/versions", params={"path": norm_rel(path, allow_root=False)})).get("versions") or [])

    # ------------------------------------------------------------------ C.6 ghi (mọi lệnh: X-Lane-Agent + X-Lane-Run + Idempotency-Key)
    async def write(self, root: str, path: str, data: bytes, *, agent: str, run: str, if_match: str | None = None, create: bool = False,
                    parents: bool = True, idem: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        """PUT nội dung. Server quét virus ĐỒNG BỘ (chờ tới ~300 s) → bên gọi truyền `timeout` dài hơn (`filehub.lane.write_timeout_s`) và
        GIỮ `idem` qua mọi lần thử lại (C.6: cùng khoá → đúng phản hồi lần đầu, không 409 với chính bản mình vừa chốt)."""
        if not isinstance(data, (bytes, bytearray)):
            raise LaneError("invalid_input", message="nội dung phải là bytes")
        if len(data) > UPLOAD_MAX_BYTES:
            raise LaneError("too_large", 413, {"max_bytes": UPLOAD_MAX_BYTES})
        if not create and not if_match:
            raise LaneError("precondition_required", 428, message="ghi đè cần If-Match (phiên bản gốc)")
        hdr = {"X-Lane-Sha256": hashlib.sha256(data).hexdigest(), "Idempotency-Key": idem or str(uuid.uuid4()),
               "Content-Type": "application/octet-stream"}
        if create:
            hdr["If-None-Match"] = "*"
        else:
            hdr["If-Match"] = f'"{if_match}"'
        d = await self._req("PUT", f"/roots/{self._root(root)}/content", params={"path": norm_rel(path, allow_root=False), "parents": 1 if parents else 0},
                            content=bytes(data), agent=agent, run=run, headers=hdr, timeout=timeout)
        return d.get("item") or {}

    async def mkdir(self, root: str, path: str, *, agent: str, run: str, parents: bool = True, idem: str | None = None) -> dict[str, Any]:
        d = await self._req("POST", f"/roots/{self._root(root)}/mkdir", json_body={"path": norm_rel(path, allow_root=False), "parents": bool(parents)},
                            agent=agent, run=run, headers={"Idempotency-Key": idem or str(uuid.uuid4())})
        return d.get("item") or {}

    async def move(self, root: str, src: str, dst: str, *, if_rev: int | None, agent: str, run: str, idem: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"from": norm_rel(src, allow_root=False), "to": norm_rel(dst, allow_root=False)}
        if if_rev is not None:
            body["if_rev"] = int(if_rev)
        d = await self._req("POST", f"/roots/{self._root(root)}/move", json_body=body, agent=agent, run=run,
                            headers={"Idempotency-Key": idem or str(uuid.uuid4())})
        return d.get("item") or {}

    async def delete(self, root: str, path: str, *, if_rev: int | None, agent: str, run: str, idem: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"path": norm_rel(path, allow_root=False)}
        if if_rev is not None:
            params["if_rev"] = int(if_rev)
        return await self._req("DELETE", f"/roots/{self._root(root)}/item", params=params, agent=agent, run=run,
                               headers={"Idempotency-Key": idem or str(uuid.uuid4())})

    async def restore(self, root: str, *, agent: str, run: str, trash_batch: str | None = None, path: str | None = None,
                      version_id: str | None = None, if_match: str | None = None, idem: str | None = None) -> dict[str, Any]:
        if trash_batch:
            body: dict[str, Any] = {"trash_batch": str(trash_batch)}
        else:
            body = {"path": norm_rel(path, allow_root=False), "version_id": str(version_id or ""), "if_match": str(if_match or "")}
        d = await self._req("POST", f"/roots/{self._root(root)}/restore", json_body=body, agent=agent, run=run,
                            headers={"Idempotency-Key": idem or str(uuid.uuid4())})
        return d.get("item") or {}

    async def presence_put(self, root: str, path: str | None, ttl_s: int, *, agent: str, run: str) -> dict[str, Any]:
        return await self._req("PUT", "/presence", json_body={"root": self._root(root), "path": norm_rel(path) if path else None,
                                                               "ttl_s": int(max(30, min(3600, ttl_s)))}, agent=agent, run=run)

    async def presence_delete(self, root: str, *, agent: str, run: str) -> None:
        await self._req("DELETE", "/presence", params={"root": self._root(root)}, agent=agent, run=run)

    # ------------------------------------------------------------------ C.8 luồng sự kiện (MỘT kết nối)
    async def events(self, cursor: str | None = None, *, read_timeout: float = 45.0) -> AsyncIterator[LaneEvent]:
        params = {"cursor": cursor} if cursor else None
        hdr = self._headers(extra={"Accept": "text/event-stream", **({"Last-Event-ID": cursor} if cursor else {})})
        timeout = _httpx.Timeout(self.timeout, read=read_timeout)
        try:
            async with self._http().stream("GET", f"{self.url}/lane/v1/events", params=params, headers=hdr, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise self._error(resp)
                ev_type, ev_id, data_lines = "message", None, []
                async for line in resp.aiter_lines():
                    line = line.rstrip("\r")
                    if line == "":
                        if data_lines or ev_type != "message":
                            raw = "\n".join(data_lines)
                            try:
                                data = json.loads(raw) if raw else {}
                            except ValueError:
                                data = {}
                            yield LaneEvent(ev_type, ev_id, data if isinstance(data, dict) else {})
                        ev_type, ev_id, data_lines = "message", None, []
                        continue
                    if line.startswith(":"):
                        continue
                    name, _, val = line.partition(":")
                    val = val[1:] if val.startswith(" ") else val
                    if name == "event":
                        ev_type = val.strip() or "message"
                    elif name == "id":
                        ev_id = val.strip() or None
                    elif name == "data":
                        data_lines.append(val)
        except (_httpx.TimeoutException, _httpx.TransportError, OSError) as e:
            raise LaneError("offline", 0, message=f"cổng cfharness: luồng sự kiện mất kết nối ({type(e).__name__})") from None


__all__ = ["LaneClient", "LaneError", "LaneEvent", "check_url", "norm_rel", "join_rel", "valid_key", "key_prefix", "port_of", "PROTOCOL",
           "KEY_RE", "SERVER_RE", "RETRYABLE", "_scrub"]
