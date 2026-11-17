"""`LaneService` (`hub.filehub.lane`) — cổng cfharness: thư mục chung Không gian số KHÔNG qua đồng bộ (docs/dev-notes/plan-filehub-lane.md).

- Cấu hình `filehub.lane.servers.<tên> = {url, hub_label, user, key_prefix, scope, expires_at, set_at}` (∈ `integrity.SECURITY_KEYS` như
  cả nhánh `filehub`); KHOÁ ở secret store `FILEHUB_LANE_<TÊN>` (0600, đăng ký bộ che theo giá trị). Khoá không bao giờ vào env/argv của
  run hay tiến trình nào, DB, config.yaml, audit, log, event, mcp.json, Telegram/Slack, gói `clone`.
- Thư mục chế độ `lane`: `folders[].mode = "lane"` (hoặc `filehub.mode = lane`) + `folders[].server` (thiếu → server duy nhất đã thiết lập).
  `target(body)` giải `<tên>[/<con>]` → `{folder, server, root, sub, key}`; khoá ghi = `filehub-lane:<server>:<root>` (gốc thư mục chung).
- Trạng thái theo server (`ok|offline|auth_failed|version_mismatch|disabled|not_configured|key_expiring`) → event `filehub.lane.state`.
- Agent KHÔNG BAO GIỜ nói chuyện với cổng: sandbox chặn TCP localhost, classifier `filehub_lane` (forbidden); mọi truy cập qua daemon.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ...errors import Conflict, HubError, NotFound
from ...events import make_event
from .client import PROTOCOL, SERVER_RE, LaneClient, LaneError, check_url, key_prefix, port_of, valid_key

if TYPE_CHECKING:
    from ..service import FilehubService

log = logging.getLogger("cfharness.filehub.lane")

SECRET_PREFIX = "FILEHUB_LANE_"
STATES = ("ok", "offline", "auth_failed", "version_mismatch", "disabled", "not_configured", "key_expiring")
LOCK_PREFIX = "filehub-lane:"


def secret_name(server: str) -> str:
    return SECRET_PREFIX + str(server).upper()


def lock_key(server: str, root: str) -> str:
    return f"{LOCK_PREFIX}{server}:{root}"


def validate_setting(key: str, value: Any) -> None:
    """`config.set` nhánh filehub: `mode` ∈ sync|lane; URL server lane chỉ loopback; không ai cất khoá vào config."""
    def _mode(v: Any) -> None:
        if v is not None and v not in ("sync", "lane"):
            raise ValueError("filehub.mode phải là sync|lane")

    def _servers(v: Any) -> None:
        if v is None:
            return
        if not isinstance(v, dict):
            raise ValueError("filehub.lane.servers phải là mapping {tên: {url, …}}")
        for name, s in v.items():
            if not isinstance(name, str) or not SERVER_RE.match(name):
                raise ValueError(f"tên server lane không hợp lệ: {name!r} ([a-z0-9_]{{1,32}})")
            if not isinstance(s, dict):
                raise ValueError(f"filehub.lane.servers.{name} phải là mapping")
            try:
                check_url(s.get("url"))
            except LaneError as e:
                raise ValueError(e.message) from None
            if any(k in s for k in ("key", "token", "secret")) or any(isinstance(x, str) and x.startswith("tt_lane_") and len(x) > 12 for x in s.values()):
                raise ValueError("khoá cổng cfharness chỉ ở secret store (`cfharness filehub lane setup`), không ở config")

    def _folders(v: Any) -> None:
        for f in v or []:
            if isinstance(f, dict):
                _mode(f.get("mode"))
                if f.get("server") is not None and not (isinstance(f["server"], str) and SERVER_RE.match(f["server"])):
                    raise ValueError(f"folders[].server không hợp lệ: {f.get('server')!r}")

    if key == "filehub":
        if isinstance(value, dict):
            _mode(value.get("mode"))
            _servers((value.get("lane") or {}).get("servers") if isinstance(value.get("lane"), dict) else None)
            _folders(value.get("folders"))
    elif key == "filehub.mode":
        _mode(value)
    elif key == "filehub.lane":
        if isinstance(value, dict):
            _servers(value.get("servers"))
    elif key == "filehub.lane.servers":
        _servers(value)
    elif key.startswith("filehub.lane.servers."):
        name = key[len("filehub.lane.servers."):].split(".", 1)[0]
        _servers({name: value} if "." not in key[len("filehub.lane.servers."):] else None)
    elif key == "filehub.folders":
        _folders(value)


def _parse_ts(v: Any) -> float | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class LaneService:
    def __init__(self, fh: "FilehubService") -> None:
        self.fh = fh
        self.hub = fh.hub
        self._clients: dict[str, tuple[str, LaneClient]] = {}
        self._hello: dict[str, tuple[float, dict[str, Any]]] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.transport: Any = None  # test: transport httpx thay thế (mặc định TCP thật tới 127.0.0.1)

    # ------------------------------------------------------------------ cấu hình
    def cfg(self, key: str) -> Any:
        return self.fh.cfg(f"lane.{key}")

    def servers(self) -> dict[str, dict[str, Any]]:
        v = self.cfg("servers") or {}
        return {k: dict(s) for k, s in v.items() if isinstance(s, dict) and SERVER_RE.match(str(k))} if isinstance(v, dict) else {}

    def server_cfg(self, server: str) -> dict[str, Any]:
        s = self.servers().get(str(server or ""))
        if s is None:
            raise NotFound(f"chưa thiết lập cổng cfharness '{server}' — `cfharness filehub lane setup --server {server or '<tên>'} --url http://127.0.0.1:<cổng>`")
        return s

    def ports(self) -> tuple[int, ...]:
        """Cổng loopback của mọi server lane (classifier `filehub_lane`: agent nhắm cổng này = cấm)."""
        out = []
        for s in self.servers().values():
            p = port_of(str(s.get("url") or ""))
            if p:
                out.append(p)
        return tuple(sorted(set(out)))

    def _key(self, server: str) -> str | None:
        """Khoá CHỈ từ secret store (không đọc env — khác `SecretStore.lookup`)."""
        try:
            return self.hub.secrets.get(secret_name(server))
        except HubError:
            return None

    def client(self, server: str) -> LaneClient:
        s = self.server_cfg(server)
        key = self._key(server)
        if not key:
            raise LaneError("unauthorized", message=f"cổng cfharness '{server}': chưa có khoá — `cfharness filehub lane setup` lại")
        fp = hashlib.sha256(f"{s.get('url')}|{key}".encode()).hexdigest()
        cur = self._clients.get(server)
        if cur is not None and cur[0] == fp:
            return cur[1]
        c = LaneClient(str(s.get("url")), key, timeout=float(self.cfg("request_timeout_s") or 60), transport=self.transport)
        if cur is not None:
            self._spawn(cur[1].aclose())
        self._clients[server] = (fp, c)
        return c

    def default_server(self) -> str | None:
        s = list(self.servers())
        return s[0] if len(s) == 1 else None

    def folder_server(self, f: dict[str, Any]) -> str | None:
        srv = f.get("server")
        return str(srv) if srv else self.default_server()

    # ------------------------------------------------------------------ giải `filehub:<tên>[/<con>]` cho thư mục lane
    def target(self, body: str) -> dict[str, Any] | None:
        from ..service import aliases_of
        from .client import norm_rel

        folders = self.fh.lane_folders()
        if not folders:
            return None
        hit = None
        for f in sorted(folders, key=lambda x: -len(x["name"])):
            for n in [f["name"], *aliases_of(f)]:
                if body == n or body.startswith(n + "/"):
                    hit = (f, body[len(n):].lstrip("/"))
                    break
            if hit:
                break
        if hit is None:
            return None
        f, sub = hit
        srv = self.folder_server(f)
        if not srv:
            raise HubError(f"thư mục chung '{f['name']}' ở chế độ lane nhưng chưa chọn cổng (folders[].server) — `cfharness filehub lane setup`")
        try:
            sub = norm_rel(sub)
        except LaneError:
            raise HubError(f"đường dẫn con không hợp lệ: {sub!r}") from None
        return {"folder": f["name"], "server": srv, "root": str(f["root_id"]), "sub": sub, "kind": str(f.get("kind") or "folder"),
                "key": lock_key(srv, str(f["root_id"]))}

    # ------------------------------------------------------------------ trạng thái
    def _set_state(self, server: str, state: str, **extra: Any) -> None:
        prev = (self.states.get(server) or {}).get("state")
        self.states[server] = {"state": state, "at": time.time(), **{k: v for k, v in extra.items() if v is not None}}
        if prev != state:
            self.hub.publish(make_event("filehub.lane.state", None, {"server": server, "state": state, "prev": prev,
                                                                     **{k: v for k, v in extra.items() if k in ("code", "days_left") and v is not None}},
                                        source="hub"))

    def _on_error(self, server: str, e: LaneError) -> None:
        if e.lane_code in ("unauthorized",):
            self._set_state(server, "auth_failed", code=e.lane_code)
        elif e.lane_code == "lane_disabled":
            self._set_state(server, "disabled", code=e.lane_code)
        elif e.lane_code == "offline":
            self._set_state(server, "offline", code=e.lane_code)

    async def hello(self, server: str, *, force: bool = False) -> dict[str, Any]:
        """`GET /hello` (bộ nhớ đệm 60 s). protocol ≠ 1 → `version_mismatch`; lỗi C.7 → trạng thái tương ứng."""
        cached = self._hello.get(server)
        if cached and not force and time.time() - cached[0] < 60:
            return cached[1]
        try:
            h = await self.client(server).hello()
        except LaneError as e:
            self._on_error(server, e)
            raise
        if h.get("protocol") != PROTOCOL:
            self._set_state(server, "version_mismatch")
            raise LaneError("version_mismatch", message=f"cổng cfharness '{server}': giao thức {h.get('protocol')!r} ≠ {PROTOCOL} — cập nhật cfharness/Không gian số")
        self._hello[server] = (time.time(), h)
        exp = self.key_expiry(server, h)
        self._set_state(server, "key_expiring" if exp.get("warn") else "ok", days_left=exp.get("days_left") if exp.get("warn") else None)
        return h

    def key_expiry(self, server: str, hello: dict[str, Any] | None = None) -> dict[str, Any]:
        """Hạn khoá: `expires_at` của server (hello/cấu hình) — không có thì `set_at + key_max_days` (chủ repo chốt 180 ngày)."""
        s = self.servers().get(server) or {}
        exp = _parse_ts(((hello or {}).get("key") or {}).get("expires_at") or s.get("expires_at"))
        if exp is None:
            set_at = s.get("set_at") or self.hub.secrets.set_at(secret_name(server))
            if set_at:
                exp = float(set_at) + float(self.cfg("key_max_days") or 180) * 86400
        if exp is None:
            return {"expires_at": None, "days_left": None, "warn": False}
        days = (exp - time.time()) / 86400
        return {"expires_at": datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "days_left": round(days, 1),
                "warn": days <= float(self.cfg("key_warn_days") or 14), "expired": days <= 0}

    def status(self) -> dict[str, Any]:
        out = []
        for name, s in self.servers().items():
            st = self.states.get(name) or {}
            out.append({"name": name, "url": s.get("url"), "user": s.get("user"), "hub_label": s.get("hub_label"), "scope": s.get("scope"),
                        "key_prefix": s.get("key_prefix"), "key": self.hub.secrets.exists(secret_name(name)), "state": st.get("state") or "unknown",
                        "error": st.get("code"), "expiry": self.key_expiry(name)})
        folders = [{"name": f["name"], "root_id": f.get("root_id"), "server": self.folder_server(f), "kind": f.get("kind") or "folder",
                    "old_copy": f.get("local_path") or None, "workspace": f"filehub:{f['name']}"} for f in self.fh.lane_folders()]
        return {"mode": str(self.fh.cfg("mode") or "sync"), "servers": out, "folders": folders, **self._run_status()}

    def _run_status(self) -> dict[str, Any]:
        return {}

    # ------------------------------------------------------------------ thiết lập (CHỈ người — RPC HUMAN_ONLY)
    async def setup(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.lane_setup {server, url, key}`: kiểm URL loopback + dạng khoá → `hello` bằng khoá mới → lưu khoá (secret store)
        + cấu hình server. Khoá bị gỡ khỏi `p` NGAY (audit chỉ thấy tham số còn lại)."""
        key = p.pop("key", None)
        server = str(p.get("server") or "").strip().lower()
        if not SERVER_RE.match(server):
            raise HubError("tên cổng không hợp lệ (vd qltm, cchc — [a-z0-9_]{1,32})", code=-32602)
        url = check_url(p.get("url"))
        if not valid_key(key):
            raise HubError("khoá cổng cfharness không đúng dạng (tt_lane_<43 ký tự>) — tạo ở Không gian số › Hồ sơ › Cổng cfharness", code=-32602)
        c = LaneClient(url, str(key), timeout=float(self.cfg("request_timeout_s") or 60), transport=self.transport)
        try:
            h = await c.hello()
        finally:
            await c.aclose()
        if h.get("protocol") != PROTOCOL:
            raise LaneError("version_mismatch", message=f"cổng cfharness: giao thức {h.get('protocol')!r} ≠ {PROTOCOL} — cập nhật cfharness/Không gian số")
        k = h.get("key") or {}
        self.hub.secrets.set(secret_name(server), str(key))
        servers = self.servers()
        servers[server] = {"url": url, "hub_label": str(k.get("hub_label") or "")[:32], "user": str((h.get("user") or {}).get("username") or "")[:80],
                           "key_prefix": key_prefix(str(key)), "scope": "ro" if k.get("scope") == "ro" else "rw",
                           "expires_at": k.get("expires_at") or None, "set_at": round(time.time(), 3)}
        self.hub.config.set("filehub.lane.servers", servers)
        if not self.fh.enabled():
            self.hub.config.set("filehub.enabled", True)
        self._clients.pop(server, None)
        self._hello[server] = (time.time(), h)
        self._set_state(server, "ok")
        return {"server": server, **{k2: v for k2, v in servers[server].items() if k2 != "set_at"}, "roots": self._roots_view(h),
                "expiry": self.key_expiry(server, h)}

    def _roots_view(self, h: dict[str, Any]) -> list[dict[str, Any]]:
        from ...termsafe import one_line

        mine = {str(f.get("root_id")): f for f in self.fh.all_folders() if f.get("root_id")}
        out = []
        for r in (h.get("roots") or [])[:200]:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            f = mine.get(str(r["id"]))
            from ..service import folder_mode

            out.append({"id": str(r["id"])[:128], "kind": "workspace" if r.get("kind") == "workspace" else "folder", "ws_id": str(r.get("ws_id") or "")[:128],
                        "name": one_line(str(r.get("name") or ""), 120), "role": one_line(str(r.get("role") or ""), 20),
                        "folder": f["name"] if f else None, "mode": folder_mode(f, self.hub.config) if f else None})
        return out

    async def test(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.lane_test {server}`: hello + cursor bằng khoá đã lưu (không in khoá)."""
        server = str(p.get("server") or self.default_server() or "")
        h = await self.hello(server, force=True)
        cur = await self.client(server).cursor()
        return {"server": server, "ok": True, "protocol": h.get("protocol"), "user": (h.get("user") or {}).get("username"),
                "scope": (h.get("key") or {}).get("scope"), "roots": self._roots_view(h), "cursor": bool(cur), "expiry": self.key_expiry(server, h)}

    def disable(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.lane_disable {server}`: xoá khoá + cấu hình server. Thư mục lane đang dùng server này → từ chối (chuyển về sync /
        gỡ trước) trừ khi `force`."""
        server = str(p.get("server") or "")
        self.server_cfg(server)
        using = [f["name"] for f in self.fh.lane_folders() if self.folder_server(f) == server]
        if using and not p.get("force"):
            raise Conflict(f"thư mục {', '.join(using)} đang dùng cổng '{server}' — `cfharness filehub switch <tên> --to sync` hoặc gỡ trước (hoặc --force)")
        if self._busy(server):
            raise Conflict(f"có run đang làm việc qua cổng '{server}' — dừng run trước")
        servers = self.servers()
        servers.pop(server, None)
        self.hub.config.set("filehub.lane.servers", servers)
        try:
            self.hub.secrets.delete(secret_name(server))
        except HubError:
            pass
        old = self._clients.pop(server, None)
        if old is not None:
            self._spawn(old[1].aclose())
        self._hello.pop(server, None)
        self._set_state(server, "not_configured")
        return {"server": server, "removed": True, "folders": using}

    def _busy(self, server: str) -> bool:
        return False

    # ------------------------------------------------------------------ tiện ích
    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self.fh._bg.add(t)
        t.add_done_callback(self.fh._bg.discard)

    async def stop(self) -> None:
        for _fp, c in list(self._clients.values()):
            await c.aclose()
        self._clients.clear()


__all__ = ["LaneService", "SECRET_PREFIX", "secret_name", "lock_key", "validate_setting", "LOCK_PREFIX"]
