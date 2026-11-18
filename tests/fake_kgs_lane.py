"""Server cổng cfharness GIẢ (`lane/v1`, hợp đồng §C — docs/dev-notes/plan-filehub-lane.md) chạy trong tiến trình test trên 127.0.0.1:<cổng
ngẫu nhiên>. ThreadingHTTPServer (stdlib) — TCP thật nên test được SSE đứt/nối lại. Không gọi Không gian số thật.

Cây trong RAM theo gốc: phiên bản, `If-Match`/`If-None-Match`, khoá tệp (423), hạn mức (507), `infected` khi nội dung chứa `EICAR`,
idempotency 24 h, presence, luồng SSE có cursor (`c1.` + base64url {ws: seq}), `reset`, `root_revoked`, `key_revoked`, `replaced`, ping.
API cho test: `human_write`, `human_delete`, `human_move`, `lock`, `unlock`, `revoke`, `drop_streams`, `reset_root`, `revoke_root`,
`fail_next(op, status, code, **extra)`, `requests` (nhật ký, header Authorization đã che), `presence`, `on_content` (hook gọi trong lúc
phục vụ `GET content` — mô phỏng người lưu bản mới giữa tải nội dung và `stat`), `put_delay[path] = giây` (MỘT lần: PUT content chốt
phiên bản + lưu Idempotency-Key rồi mới trả sau `giây` — mô phỏng quét virus lâu hơn timeout của client).
§C (bản cập nhật 2026-11-17): xoá thư mục CHỈ khi rỗng (`409 not_empty`, kể cả mục vô hình); di chuyển thư mục có mục vô hình với agent
(`hidden`) → `403 hidden_content`, có tệp đang khoá → `423`; cùng Idempotency-Key khi lần đầu CHƯA xong → `409 in_progress {retry_after}`
(`put_delay` = thời gian quét TRƯỚC khi chốt). `hidden` = tập đường dẫn agent không thấy (list/stat bỏ qua, không phát khung).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

KEY = "tt_lane_" + "A" * 11 + "b3dFakeKeyForTestsOnly0123456789"  # 8 + 43 ký tự
assert len(KEY) == 51


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def enc_cursor(seq: int, ws: str = "w") -> str:
    return "c1." + base64.urlsafe_b64encode(json.dumps({ws: seq}).encode()).decode().rstrip("=")


def dec_cursor(c: str | None) -> int | None:
    if not c or not c.startswith("c1."):
        return None
    s = c[3:]
    try:
        d = json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
        return int(next(iter(d.values())))
    except Exception:
        return None


def valid_path(p: str) -> bool:
    if p == "":
        return True
    if unicodedata.normalize("NFC", p) != p or p.startswith("/") or p.endswith("/") or len(p.encode()) > 1024:
        return False
    parts = p.split("/")
    return len(parts) <= 64 and all(x not in ("", ".", "..") and not x.lower().startswith(".tangthu") and all(ord(c) >= 32 for c in x) for x in parts)


class Item:
    def __init__(self, path: str, type_: str, content: bytes | None = None) -> None:
        self.path = path
        self.type = type_
        self.id = uuid.uuid4().hex
        self.rev = 1
        self.versions: list[dict[str, Any]] = []
        self.content = b""
        self.locked_until: float | None = None
        self.mtime = now_iso()
        if type_ == "file":
            self.set_content(content or b"", "human")

    def set_content(self, data: bytes, by: str) -> None:
        self.content = data
        self.versions.insert(0, {"version_id": uuid.uuid4().hex, "no": len(self.versions) + 1, "size": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest(), "at": now_iso(), "by_kind": by, "data": data})
        self.mtime = now_iso()

    def to_dict(self, parent_id: str | None) -> dict[str, Any]:
        d: dict[str, Any] = {"path": self.path, "type": self.type, "id": self.id, "parent_id": parent_id, "rev": self.rev, "mtime": self.mtime,
                             "locked": bool(self.locked_until and self.locked_until > time.time()), "label": None}
        if self.type == "file":
            v = self.versions[0]
            d.update({"version_id": v["version_id"], "version_no": v["no"], "size": v["size"], "sha256": v["sha256"], "mime": "application/octet-stream"})
        return d


class Root:
    def __init__(self, rid: str, kind: str = "folder", ws: str = "w", name: str = "cfharness") -> None:
        self.id, self.kind, self.ws, self.name = rid, kind, ws, name
        self.items: dict[str, Item] = {}
        self.root_id_item = uuid.uuid4().hex


class FakeLane:
    def __init__(self, *, key: str = KEY, roots: tuple[str, ...] = ("r_chung",), kind: str = "folder", scope: str = "rw",
                 expires_at: str | None = None, protocol: int = 1, ping_s: float = 15.0, public_url: str | None = None) -> None:
        self.key = key
        self.public_url = public_url  # hello.server.public_url (§C.3, tuỳ chọn) — None = không gửi trường
        self.scope = scope
        self.expires_at = expires_at
        self.protocol = protocol
        self.ping_s = ping_s
        self.revoked = False
        self.disabled = False
        self.quota_bytes: int | None = None
        self.roots: dict[str, Root] = {r: Root(r, kind=kind, name=r.replace("r_", "")) for r in roots}
        self.journal: list[tuple[int, str, dict[str, Any]]] = []  # (seq, event, data)
        self.seq = 0
        self.cond = threading.Condition()
        self.idem: dict[str, tuple[int, dict[str, Any]]] = {}
        self.presence: dict[tuple[str, str], dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.failures: list[tuple[str, int, str, dict[str, Any]]] = []
        self.streams: list[dict[str, Any]] = []
        self.stream_opens = 0
        self.list_pages: dict[str, tuple[list[dict[str, Any]], str]] = {}
        self.page_limit = 2000
        self.on_content: Any = None  # callable(path) — chạy trong lúc phục vụ GET content (sau khi đã chọn phiên bản trả về)
        self.put_delay: dict[str, float] = {}
        self.hidden: set[str] = set()
        self.inflight: set[str] = set()
        self.trash: dict[str, tuple[str, list[Item]]] = {}  # trash_batch → (root, mục đã xoá) — C.6 `restore {trash_batch}`
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:  # noqa: D401
                pass

            def _send(self, code: int, body: Any = None, headers: dict[str, str] | None = None, raw: bytes | None = None) -> None:
                data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
                self.send_response(code)
                if raw is None and body is not None:
                    self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _handle(self, method: str) -> None:
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
                body = b""
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    outer.requests.append({"method": method, "path": u.path, "chunked": True})
                    return self._send(411, {"code": "length_required"})
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    body = self.rfile.read(n)
                hdr = {k: v for k, v in self.headers.items()}
                rec = {"method": method, "path": u.path, "query": q, "headers": {k: ("<redacted>" if k.lower() == "authorization" else v) for k, v in hdr.items()},
                       "auth": hdr.get("Authorization"), "body_len": len(body)}
                outer.requests.append(rec)
                if u.path == "/lane/v1/events" and method == "GET":
                    return outer._events(self, q, hdr)
                code, res, extra_h, raw = outer.dispatch(method, u.path, q, hdr, body)
                rec["status"], rec["code"] = code, (res or {}).get("code") if isinstance(res, dict) else None
                self._send(code, res, extra_h, raw)

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_PUT(self) -> None:  # noqa: N802
                self._handle("PUT")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.drop_streams()
        self.srv.shutdown()
        self.srv.server_close()

    # ------------------------------------------------------------------ API cho test
    def root(self, rid: str = "r_chung") -> Root:
        return self.roots[rid]

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        with self.cond:
            self.seq += 1
            self.journal.append((self.seq, event, data))
            self.cond.notify_all()

    def _change(self, root: Root, op: str, it: Item, *, by: str, from_path: str | None = None, agent: str | None = None,
                request_id: str | None = None) -> None:
        d = {"root": root.id, "op": op, "type": it.type, "id": it.id, "path": it.path, "from_path": from_path, "rev": it.rev,
             "version_id": it.versions[0]["version_id"] if it.type == "file" and it.versions else None,
             "size": len(it.content) if it.type == "file" else None, "sha256": it.versions[0]["sha256"] if it.type == "file" and it.versions else None,
             "by_kind": by, "self": by == "cfharness", "agent": agent if by == "cfharness" else None,
             "request_id": request_id if by == "cfharness" else None, "at": now_iso()}
        self._emit("change", d)

    def _ensure_parents(self, root: Root, path: str, by: str) -> None:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            p = "/".join(parts[:i])
            if p not in root.items:
                it = Item(p, "folder")
                root.items[p] = it
                self._change(root, "create", it, by=by)

    def human_write(self, path: str, data: bytes | str, root: str = "r_chung") -> Item:
        r = self.roots[root]
        data = data.encode() if isinstance(data, str) else data
        self._ensure_parents(r, path, "human")
        it = r.items.get(path)
        if it is None:
            it = Item(path, "file", data)
            r.items[path] = it
            self._change(r, "create", it, by="human")
        else:
            it.set_content(data, "human")
            it.rev += 1
            self._change(r, "update", it, by="human")
        return it

    def human_mkdir(self, path: str, root: str = "r_chung") -> None:
        r = self.roots[root]
        self._ensure_parents(r, path + "/x", "human")

    def human_delete(self, path: str, root: str = "r_chung") -> None:
        r = self.roots[root]
        it = r.items.pop(path)
        for k in [k for k in r.items if k.startswith(path + "/")]:  # xoá thư mục = cả cây con (vào thùng rác cùng lô)
            r.items.pop(k)
        it.rev += 1
        self._change(r, "delete", it, by="human")

    def human_move(self, src: str, dst: str, root: str = "r_chung") -> None:
        r = self.roots[root]
        it = r.items.pop(src)
        it.path = dst
        it.rev += 1
        r.items[dst] = it
        self._change(r, "move", it, by="human", from_path=src)

    def lock(self, path: str, seconds: float = 3600, root: str = "r_chung") -> None:
        self.roots[root].items[path].locked_until = time.time() + seconds

    def unlock(self, path: str, root: str = "r_chung") -> None:
        self.roots[root].items[path].locked_until = None

    def revoke(self) -> None:
        self.revoked = True
        self._emit("key_revoked", {})

    def revoke_root(self, root: str = "r_chung") -> None:
        self.roots.pop(root, None)
        self._emit("root_revoked", {"root": root})

    def reset_root(self, root: str = "r_chung", reason: str = "compacted") -> None:
        self._emit("reset", {"root": root, "reason": reason})

    def drop_streams(self) -> None:
        with self.cond:
            for s in self.streams:
                s["drop"] = True
            self.cond.notify_all()

    def fail_next(self, op: str, status: int, code: str, **extra: Any) -> None:
        """`op` = "<METHOD> <đuôi đường dẫn>" vd "PUT content", "GET hello" — lần gọi kế tiếp khớp trả lỗi này."""
        self.failures.append((op, status, code, extra))

    def file(self, path: str, root: str = "r_chung") -> bytes | None:
        it = self.roots[root].items.get(path)
        return it.content if it is not None and it.type == "file" else None

    def writes(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] in ("PUT", "POST", "DELETE") and "/presence" not in r["path"]]

    # ------------------------------------------------------------------ xử lý
    def _auth(self, hdr: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
        for h in hdr:
            if h.lower() in ("forwarded", "cf-connecting-ip", "cf-ray") or h.lower().startswith("x-forwarded-"):
                return 403, {"code": "lane_not_local"}
        a = hdr.get("Authorization") or ""
        if self.revoked or a != f"Bearer {self.key}":
            return 401, {"code": "unauthorized"}
        if self.disabled:
            return 403, {"code": "lane_disabled"}
        return None

    def _check_hdrs(self, hdr: dict[str, str], write: bool) -> tuple[int, dict[str, Any]] | None:
        import re

        for k in ("X-Lane-Agent", "X-Lane-Run"):
            v = hdr.get(k)
            if (v is None and write) or (v is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", v)):
                return 400, {"code": "invalid_input"}
        return None

    def _used(self) -> int:
        return sum(len(i.content) for r in self.roots.values() for i in r.items.values() if i.type == "file")

    def _parent_id(self, root: Root, path: str) -> str:
        par = path.rsplit("/", 1)[0] if "/" in path else ""
        return root.items[par].id if par and par in root.items else root.root_id_item

    def dispatch(self, method: str, path: str, q: dict[str, str], hdr: dict[str, str], body: bytes) -> tuple[int, Any, dict[str, str] | None, bytes | None]:
        if not path.startswith("/lane/v1/"):
            return 404, {"code": "not_found"}, None, None
        tail = path[len("/lane/v1/"):]
        op_name = f"{method} {tail.rsplit('/', 1)[-1]}"
        for i, (op, st, code, extra) in enumerate(self.failures):
            if op == op_name:
                self.failures.pop(i)
                return st, {"code": code, **extra}, None, None
        bad = self._auth(hdr)
        if bad:
            return bad[0], bad[1], None, None
        if tail == "hello" and method == "GET":
            srv = {"name": "Không gian số", "instance": "qltm", "version": "2026.11.20", **({"public_url": self.public_url} if self.public_url is not None else {})}
            return 200, {"protocol": self.protocol, "server": srv,
                         "user": {"username": "lamlinh"},
                         "key": {"prefix": self.key[:12], "hub_label": "lamlinh", "scope": self.scope, "expires_at": self.expires_at},
                         "roots": [{"id": r.id, "kind": r.kind, "ws_id": r.ws, "name": r.name, "role": "owner"} for r in self.roots.values()],
                         "limits": {"max_file_bytes": 2147483648, "list_page_max": 2000, "presence_ttl_s": [30, 3600],
                                    "writes_in_flight_per_key": 8, "event_streams_per_key": 2, "path_max_bytes": 1024}}, None, None
        if tail == "cursor" and method == "GET":
            return 200, {"cursor": enc_cursor(self.seq)}, None, None
        write = method in ("PUT", "POST", "DELETE")
        bad = self._check_hdrs(hdr, write)
        if bad:
            return bad[0], bad[1], None, None
        if write and self.scope == "ro":
            return 403, {"code": "read_only_key"}, None, None
        idem = hdr.get("Idempotency-Key")
        if write and idem and idem in self.idem:
            st, res = self.idem[idem]
            return st, res, None, None
        if write and idem and idem in self.inflight:
            return 409, {"code": "in_progress", "retry_after": 0.3}, None, None
        if tail == "presence":
            return self._presence(method, q, hdr, body)
        if not tail.startswith("roots/"):
            return 404, {"code": "not_found"}, None, None
        _, rid, cmd = tail.split("/", 2)
        root = self.roots.get(rid)
        if root is None:
            return 403, {"code": "root_not_granted"}, None, None
        if write and idem:
            self.inflight.add(idem)
        try:
            if method == "PUT" and cmd == "content" and q.get("path") in self.put_delay:
                time.sleep(self.put_delay.pop(q["path"]))  # "quét virus" TRƯỚC khi chốt — lần gửi lại cùng khoá lúc này → in_progress
            with self.cond:
                st, res, h, raw = self._root_cmd(method, cmd, root, q, hdr, body)
            if write and idem and st < 500 and st not in (423, 429):
                self.idem[idem] = (st, res)
        finally:
            self.inflight.discard(idem or "")
        return st, res, h, raw

    def _root_cmd(self, method: str, cmd: str, root: Root, q: dict[str, str], hdr: dict[str, str], body: bytes) -> tuple[int, Any, dict[str, str] | None, bytes | None]:
        agent = hdr.get("X-Lane-Agent")
        rid = hdr.get("Idempotency-Key")
        p = q.get("path", "")
        if cmd in ("stat", "list", "content", "versions", "item") and not valid_path(p):
            return 422, {"code": "invalid_name"}, None, None
        if method == "GET" and cmd == "stat":
            it = root.items.get(p)
            if it is None or p in self.hidden:
                return 404, {"code": "not_found"}, None, None
            return 200, {"item": it.to_dict(self._parent_id(root, p))}, None, None
        if method == "GET" and cmd == "list":
            page = q.get("page")
            if page:
                rest, cur = self.list_pages.pop(page, ([], ""))
            else:
                if p and (p not in root.items or root.items[p].type != "folder"):
                    return 404, {"code": "not_found"}, None, None
                pre = p + "/" if p else ""
                rec = q.get("recursive") == "1"
                rest = [it.to_dict(self._parent_id(root, k)) for k, it in sorted(root.items.items())
                        if k.startswith(pre) and k != p and (rec or "/" not in k[len(pre):]) and k not in self.hidden]
                cur = enc_cursor(self.seq)
            lim = min(int(q.get("limit") or 2000), self.page_limit)
            items, rest = rest[:lim], rest[lim:]
            nxt = None
            if rest:
                nxt = uuid.uuid4().hex
                self.list_pages[nxt] = (rest, cur)
            return 200, {"items": items, "next_page": nxt, "cursor": cur}, None, None
        if method == "GET" and cmd == "content":
            it = root.items.get(p)
            if it is None or it.type != "file":
                return 404, {"code": "not_found"}, None, None
            v = it.versions[0]
            if q.get("version"):
                v = next((x for x in it.versions if x["version_id"] == q["version"]), None)
                if v is None:
                    return 404, {"code": "not_found"}, None, None
            if self.on_content is not None:
                self.on_content(p)
            if hdr.get("If-None-Match") == f'"{v["version_id"]}"':
                return 304, None, {"ETag": f'"{v["version_id"]}"'}, b""
            return 200, None, {"ETag": f'"{v["version_id"]}"', "X-Lane-Sha256": v["sha256"], "Content-Type": "application/octet-stream"}, v["data"]
        if method == "GET" and cmd == "versions":
            it = root.items.get(p)
            if it is None or it.type != "file":
                return 404, {"code": "not_found"}, None, None
            return 200, {"versions": [{k: v for k, v in x.items() if k != "data"} for x in it.versions[:50]]}, None, None
        if method == "PUT" and cmd == "content":
            if not p:
                return 422, {"code": "invalid_name"}, None, None
            want = hdr.get("X-Lane-Sha256")
            if not want:
                return 400, {"code": "invalid_input"}, None, None
            if hashlib.sha256(body).hexdigest() != want:
                return 400, {"code": "hash_mismatch"}, None, None
            im, inm = hdr.get("If-Match"), hdr.get("If-None-Match")
            if not im and inm != "*":
                return 428, {"code": "precondition_required"}, None, None
            if root.kind == "workspace" and "/" not in p:
                return 422, {"code": "root_files_not_allowed"}, None, None
            cur = root.items.get(p)
            if cur is not None and cur.locked_until and cur.locked_until > time.time():
                return 423, {"code": "file_locked", "until": datetime.fromtimestamp(cur.locked_until, timezone.utc).isoformat()}, None, None
            if b"EICAR" in body:
                return 422, {"code": "infected", "quarantined": True}, None, None
            if self.quota_bytes is not None and self._used() + len(body) - (len(cur.content) if cur else 0) > self.quota_bytes:
                return 507, {"code": "storage_full", "scope": "workspace", "used_bytes": self._used(), "quota_bytes": self.quota_bytes}, None, None
            par = p.rsplit("/", 1)[0] if "/" in p else ""
            if par and par not in root.items:
                if q.get("parents") != "1":
                    return 409, {"code": "parent_missing"}, None, None
                self._ensure_parents(root, p, "cfharness")
            if inm == "*":
                if cur is not None:
                    return 409, {"code": "exists", "current": cur.to_dict(self._parent_id(root, p))}, None, None
                it = Item(p, "file")
                it.versions = []
                it.set_content(body, "cfharness")
                root.items[p] = it
                self._change(root, "create", it, by="cfharness", agent=agent, request_id=rid)
                return 201, {"item": it.to_dict(self._parent_id(root, p))}, None, None
            if cur is None:
                return 404, {"code": "not_found"}, None, None
            if cur.type != "file" or im != f'"{cur.versions[0]["version_id"]}"':
                return 409, {"code": "conflict", "current": cur.to_dict(self._parent_id(root, p))}, None, None
            cur.set_content(body, "cfharness")
            cur.rev += 1
            self._change(root, "update", cur, by="cfharness", agent=agent, request_id=rid)
            return 200, {"item": cur.to_dict(self._parent_id(root, p))}, None, None
        if method == "POST" and cmd in ("mkdir", "move", "restore"):
            try:
                b = json.loads(body or b"{}")
            except ValueError:
                return 400, {"code": "invalid_input"}, None, None
            if cmd == "mkdir":
                path = str(b.get("path") or "")
                if not path or not valid_path(path):
                    return 422, {"code": "invalid_name"}, None, None
                if path in root.items:
                    return 200, {"item": root.items[path].to_dict(self._parent_id(root, path))}, None, None
                par = path.rsplit("/", 1)[0] if "/" in path else ""
                if par and par not in root.items and not b.get("parents"):
                    return 409, {"code": "parent_missing"}, None, None
                self._ensure_parents(root, path + "/x", "cfharness")
                return 201, {"item": root.items[path].to_dict(self._parent_id(root, path))}, None, None
            if cmd == "move":
                src, dst = str(b.get("from") or ""), str(b.get("to") or "")
                if not src or not dst or not valid_path(src) or not valid_path(dst):
                    return 422, {"code": "invalid_name"}, None, None
                it = root.items.get(src)
                if it is None:
                    return 404, {"code": "not_found"}, None, None
                if b.get("if_rev") is not None and int(b["if_rev"]) != it.rev:
                    return 409, {"code": "conflict", "current": it.to_dict(self._parent_id(root, src))}, None, None
                if dst in root.items:
                    return 409, {"code": "name_conflict", "current": root.items[dst].to_dict(self._parent_id(root, dst))}, None, None
                if it.type == "folder":
                    sub = [k for k in root.items if k.startswith(src + "/")]
                    if any(k in self.hidden for k in sub):
                        return 403, {"code": "hidden_content"}, None, None
                    lk = [root.items[k] for k in sub if root.items[k].locked_until and root.items[k].locked_until > time.time()]
                    if lk:
                        return 423, {"code": "file_locked", "until": datetime.fromtimestamp(lk[0].locked_until, timezone.utc).isoformat()}, None, None
                self._ensure_parents(root, dst, "cfharness")
                for k in [k for k in list(root.items) if k == src or k.startswith(src + "/")]:
                    x = root.items.pop(k)
                    x.path = dst + k[len(src):]
                    root.items[x.path] = x
                it.rev += 1
                self._change(root, "move", it, by="cfharness", from_path=src, agent=agent, request_id=rid)
                return 200, {"item": it.to_dict(self._parent_id(root, dst))}, None, None
            if b.get("trash_batch"):  # khôi phục từ thùng rác: chỗ cũ đã có mục → name_conflict (không ghi đè)
                tb = self.trash.get(str(b["trash_batch"]))
                if tb is None or tb[0] != root.id:
                    return 404, {"code": "not_found"}, None, None
                items = tb[1]
                clash = next((x for x in items if x.path in root.items), None)
                if clash is not None:
                    return 409, {"code": "name_conflict", "current": root.items[clash.path].to_dict(self._parent_id(root, clash.path))}, None, None
                self.trash.pop(str(b["trash_batch"]))
                self._ensure_parents(root, items[0].path, "cfharness")
                for x in items:
                    x.rev += 1
                    root.items[x.path] = x
                self._change(root, "restore", items[0], by="cfharness", agent=agent, request_id=rid)
                return 200, {"item": items[0].to_dict(self._parent_id(root, items[0].path))}, None, None
            # restore phiên bản
            path = str(b.get("path") or "")
            it = root.items.get(path)
            if it is None:
                return 404, {"code": "not_found"}, None, None
            if b.get("if_match") != it.versions[0]["version_id"]:
                return 409, {"code": "conflict", "current": it.to_dict(self._parent_id(root, path))}, None, None
            v = next((x for x in it.versions if x["version_id"] == b.get("version_id")), None)
            if v is None:
                return 404, {"code": "not_found"}, None, None
            it.set_content(v["data"], "cfharness")
            it.rev += 1
            self._change(root, "restore", it, by="cfharness", agent=agent, request_id=rid)
            return 200, {"item": it.to_dict(self._parent_id(root, path))}, None, None
        if method == "DELETE" and cmd == "item":
            it = root.items.get(p)
            if not p:
                return 403, {"code": "root_not_granted"}, None, None
            if it is None:
                return 404, {"code": "not_found"}, None, None
            if q.get("if_rev") and int(q["if_rev"]) != it.rev:
                return 409, {"code": "conflict", "current": it.to_dict(self._parent_id(root, p))}, None, None
            if it.locked_until and it.locked_until > time.time():
                return 423, {"code": "file_locked", "until": datetime.fromtimestamp(it.locked_until, timezone.utc).isoformat()}, None, None
            if it.type == "folder" and any(k.startswith(p + "/") for k in root.items):
                return 409, {"code": "not_empty"}, None, None  # thư mục chỉ xoá khi rỗng (kể cả mục vô hình với agent)
            d = it.to_dict(self._parent_id(root, p))
            gone = [root.items.pop(k) for k in sorted(k for k in list(root.items) if k == p or k.startswith(p + "/"))]
            it.rev += 1
            self._change(root, "delete", it, by="cfharness", agent=agent, request_id=rid)
            batch = uuid.uuid4().hex
            self.trash[batch] = (root.id, gone)
            return 200, {"trash_batch": batch, "item": d}, None, None
        return 404, {"code": "not_found"}, None, None

    def _presence(self, method: str, q: dict[str, str], hdr: dict[str, str], body: bytes) -> tuple[int, Any, dict[str, str] | None, bytes | None]:
        run = hdr.get("X-Lane-Run") or ""
        if method == "PUT":
            b = json.loads(body or b"{}")
            if b.get("root") not in self.roots:
                return 403, {"code": "root_not_granted"}, None, None
            ttl = int(b.get("ttl_s") or 0)
            if not 30 <= ttl <= 3600:
                return 400, {"code": "invalid_input"}, None, None
            label = f"cfharness · {hdr.get('X-Lane-Agent')}" + (f" · đang sửa {str(b['path']).rsplit('/', 1)[-1]}" if b.get("path") else "")
            self.presence[(b["root"], run)] = {"label": label, "path": b.get("path"), "until": time.time() + ttl}
            return 200, {"since": now_iso(), "until": now_iso()}, None, None
        if method == "DELETE":
            self.presence.pop((q.get("root", ""), run), None)
            return 204, None, None, None
        return 404, {"code": "not_found"}, None, None

    def _events(self, h: BaseHTTPRequestHandler, q: dict[str, str], hdr: dict[str, str]) -> None:
        bad = self._auth(hdr)
        if bad:
            data = json.dumps(bad[1]).encode()
            h.send_response(bad[0])
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            return
        cur = q.get("cursor") or hdr.get("Last-Event-ID")
        pos = dec_cursor(cur) if cur else None
        if pos is None:
            pos = 0 if not cur else 0
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Connection", "close")
        h.end_headers()
        st = {"drop": False}
        with self.cond:
            self.stream_opens += 1
            self.streams.append(st)
            if len([s for s in self.streams if not s.get("closed")]) > 2:  # luồng thứ 3 → đóng luồng cũ nhất
                old = next(s for s in self.streams if not s.get("closed") and s is not st)
                old["replaced"] = True
        last_ping = time.time()
        try:
            while True:
                with self.cond:
                    while not st["drop"] and not st.get("replaced") and not any(seq > pos for seq, _, _ in self.journal) \
                            and time.time() - last_ping < self.ping_s:
                        self.cond.wait(0.05)
                    if st["drop"]:
                        return
                    if st.get("replaced"):
                        h.wfile.write(b"event: replaced\ndata: {}\n\n")
                        h.wfile.flush()
                        return
                    frames = [(s, e, d) for s, e, d in self.journal if s > pos]
                for s, e, d in frames:
                    if e == "change" and d.get("root") not in self.roots and d.get("op") != "acl":
                        pos = s
                        continue
                    h.wfile.write(f"id: {enc_cursor(s)}\nevent: {e}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n".encode())
                    h.wfile.flush()
                    pos = s
                    if e == "key_revoked":
                        return
                if not frames and time.time() - last_ping >= self.ping_s:
                    h.wfile.write(b"event: ping\ndata: {}\n\n")
                    h.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            st["closed"] = True


def new_key() -> str:
    return "tt_lane_" + secrets.token_urlsafe(32)[:43].ljust(43, "A")
