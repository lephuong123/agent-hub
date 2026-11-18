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
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...errors import Blocked, Conflict, HubError, NotFound
from ...events import make_event
from .client import PROTOCOL, SERVER_RE, LaneClient, LaneError, check_url, key_prefix, port_of, valid_key

if TYPE_CHECKING:
    from ..service import FilehubService

log = logging.getLogger("cfharness.filehub.lane")

SECRET_PREFIX = "FILEHUB_LANE_"
STATES = ("ok", "offline", "auth_failed", "version_mismatch", "disabled", "not_configured", "key_expiring")
LOCK_PREFIX = "filehub-lane:"
# trạng thái cổng → (nhãn ngắn, việc người cần làm) — dùng chung cho thẻ web, CLI (không bao giờ in mã thô cho người)
OP_TEXT = {"delete": "xoá", "rmdir": "xoá thư mục", "write": "ghi", "move": "di chuyển", "mkdir": "tạo thư mục"}
ORPHAN_SHOW = 50
STATE_TEXT = {
    "ok": ("đang hoạt động", ""),
    "key_expiring": ("khoá sắp hết hạn", "Tạo khoá mới ở Không gian số › Hồ sơ › Cổng cfharness rồi dán lại (Thay khoá) trước ngày hết hạn."),
    "offline": ("không kết nối được", "Không gian số trên máy này không trả lời ở địa chỉ cổng. Hub tự thử lại và tự về «đang hoạt động» khi "
                "Không gian số chạy lại — bấm «Thử kết nối» để thử ngay; vẫn lỗi thì kiểm tra Không gian số trên máy chạy hub."),
    "auth_failed": ("khoá không còn dùng được", "Khoá bị thu hồi, hết hạn hoặc sai — tạo khoá mới ở Không gian số › Hồ sơ › Cổng cfharness "
                    "rồi dán lại (Thay khoá bên dưới)."),
    "disabled": ("cổng đang tắt", "Quản trị Không gian số đã tắt chính sách «Cho phép cổng cfharness» — nhờ quản trị bật lại."),
    "version_mismatch": ("lệch phiên bản", "cfharness và Không gian số nói hai phiên bản giao thức khác nhau — cập nhật bên cũ hơn."),
    "not_configured": ("chưa thiết lập", "Dán khoá cổng tạo ở Không gian số › Hồ sơ › Cổng cfharness."),
    "unknown": ("đang kiểm tra", "Hub đang kiểm tra kết nối — bấm «Thử kết nối» nếu chờ lâu."),
}
SYNC_HINT = ("«{name}» đang đồng bộ kiểu cũ (bản sao trên máy) — giữ nguyên; chuyển sang cổng bằng nút «Chuyển sang cổng» ở Cài đặt › Cổng "
             "cfharness hoặc `cfharness filehub switch \"{name}\" --to lane`")


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
        self.workdirs: dict[str, Any] = {}  # tree → Workdir (đang chạy hoặc đỗ khi waiting_human)
        self.by_run: dict[str, str] = {}  # run_id → tree
        self._streams: dict[str, asyncio.Task[Any]] = {}  # server → vòng đọc SSE
        self._maint: asyncio.Task[Any] | None = None
        self.stream_reconnects: dict[str, int] = {}
        self._health: asyncio.Task[Any] | None = None
        self._health_last: dict[str, float] = {}
        self._health_fail: dict[str, int] = {}
        self.last_reconcile: dict[str, dict[str, Any]] = {}
        self.hub.bus.add_listener(self._on_bus)

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

    async def hello(self, server: str, *, force: bool = False, reconcile: bool = True) -> dict[str, Any]:
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
        from ...runs import export_quiesced

        if reconcile and not export_quiesced(self.hub):  # đang xuất gói / đóng băng chuyển nhà: không ghi cấu hình
            try:
                self.last_reconcile[server] = self.reconcile(server, h)
            except Exception:  # pragma: no cover — đối soát không được làm hỏng run
                log.exception("lane: đối soát gốc được cấp của %s lỗi", server)
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
                # cho người đọc: ngày theo giờ máy + số ngày nguyên (không "180.0 ngày", không ISO)
                "date": datetime.fromtimestamp(exp).strftime("%d/%m/%Y"), "days": max(0, int(round(days))),
                "warn": days <= float(self.cfg("key_warn_days") or 14), "expired": days <= 0}

    def status(self) -> dict[str, Any]:
        out = []
        for name, s in self.servers().items():
            st = self.states.get(name) or {}
            state = st.get("state") or "unknown"
            label, hint = STATE_TEXT.get(state, (state, ""))
            out.append({"name": name, "url": s.get("url"), "user": s.get("user"), "hub_label": s.get("hub_label"), "scope": s.get("scope"),
                        "key_prefix": s.get("key_prefix"), "key": self.hub.secrets.exists(secret_name(name)), "state": state,
                        "label": label, "hint": hint, "error": st.get("code"), "expiry": self.key_expiry(name),
                        # lần kiểm tra (hello) gần nhất theo giờ máy — thẻ web ghi "kiểm tra lúc …" cạnh trạng thái
                        "checked": datetime.fromtimestamp(float(st["at"])).strftime("%H:%M:%S") if st.get("at") else None,
                        "roots": self._roots_view(self._hello[name][1]) if name in self._hello else None})
        revoked = self._revoked()
        sstate = {x["name"]: x["state"] for x in out}
        folders = []
        for f in self.fh.lane_folders():
            srv = self.folder_server(f)
            rv = str(f.get("root_id")) in revoked.get(str(srv), set())
            sst = sstate.get(str(srv)) or "not_configured"
            folders.append({"name": f["name"], "root_id": f.get("root_id"), "server": srv, "kind": f.get("kind") or "folder",
                            # bản sao của chế độ sync cũ: hub KHÔNG xoá (chủ repo chốt) — chỉ báo còn đó
                            "old_copy": f.get("local_path") if f.get("local_path") and os.path.isdir(os.path.expanduser(str(f["local_path"]))) else None,
                            "revoked": rv, "status": "revoked" if rv else ("ok" if sst in ("ok", "key_expiring") else sst),
                            "workspace": f"filehub:{f['name']}"})
        return {"mode": str(self.fh.cfg("mode") or "sync"), "servers": out, "folders": folders, **self._run_status()}

    def _run_status(self) -> dict[str, Any]:
        lat = sorted(x for wd in self.workdirs.values() for x in wd.latencies)
        p95 = round(lat[min(len(lat) - 1, int(len(lat) * 0.95))] * 1000) if lat else None
        runs = [wd.info() for wd in self.workdirs.values()]
        return {"runs": [{**i, "task_id": self._task_of(i.get("run_id"))} for i in runs], "orphans": self.orphans(),
                "steer_latency_p95_ms": p95,
                "streams": {k: (not t.done()) for k, t in self._streams.items()}}

    # ------------------------------------------------------------------ thiết lập (CHỈ người — RPC HUMAN_ONLY)
    async def setup(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.lane_setup {key, url?, server?}`: kiểm URL loopback + dạng khoá → `hello` bằng khoá mới → lưu khoá (secret store)
        + cấu hình server → THÊM NGAY mọi gốc khoá được cấp thành thư mục chung chế độ lane (`reconcile`). Khoá bị gỡ khỏi `p` NGAY (audit
        chỉ thấy tham số còn lại). Bỏ trống `url` → thử `filehub.lane.default_urls` (8223 rồi 8233); bỏ trống `server` → `hello.server.instance`."""
        key = p.pop("key", None)
        server = str(p.get("server") or "").strip().lower()
        if server and not SERVER_RE.match(server):
            raise HubError("tên cổng không hợp lệ (vd qltm, cchc — [a-z0-9_]{1,32})", code=-32602)
        given = str(p.get("url") or "").strip()
        urls = [check_url(given)] if given else []
        if not given:
            for u in self.cfg("default_urls") or []:
                try:
                    cu = check_url(u)
                except LaneError:
                    continue
                if cu not in urls:
                    urls.append(cu)
            if not urls:
                raise HubError("thiếu địa chỉ cổng (vd http://127.0.0.1:8223)", code=-32602)
        if not valid_key(key):
            raise HubError("khoá cổng cfharness không đúng dạng (tt_lane_<43 ký tự>) — tạo ở Không gian số › Hồ sơ › Cổng cfharness", code=-32602)
        h: dict[str, Any] | None = None
        url = ""
        errs: list[tuple[str, LaneError]] = []
        for u in urls:
            c = LaneClient(u, str(key), timeout=float(self.cfg("request_timeout_s") or 60), transport=self.transport)
            try:
                got = await c.hello()
                if got.get("protocol") != PROTOCOL:
                    raise LaneError("version_mismatch", message=f"cổng cfharness: giao thức {got.get('protocol')!r} ≠ {PROTOCOL} — cập nhật cfharness/Không gian số")
                h, url = got, u
                break
            except LaneError as e:
                errs.append((u, e))
            finally:
                await c.aclose()
        if h is None:
            if given:
                raise errs[-1][1]
            best = next((e for _u, e in errs if e.lane_code != "offline"), errs[-1][1])  # lỗi khoá/chính sách nói nhiều hơn "không ai nghe"
            raise LaneError(best.lane_code, best.status, message=f"cổng cfharness: thử {', '.join(urls)} — {best.message}")
        if not server:
            inst = str((h.get("server") or {}).get("instance") or "").strip().lower()
            server = inst if SERVER_RE.match(inst) else "qltm"
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
        self._health_fail[server] = 0
        rec = self.reconcile(server, h)
        return {"server": server, **{k2: v for k2, v in servers[server].items() if k2 != "set_at"}, "roots": self._roots_view(h),
                "expiry": self.key_expiry(server, h), "added": rec["added"], "sync": rec["sync"], "revoked": rec["revoked"]}

    # ------------------------------------------------------------------ đối soát gốc được cấp ↔ thư mục chung (mỗi `hello` mới)
    def _revoked(self) -> dict[str, set[str]]:
        st = self.hub.store.state_get("filehub.lane") or {}
        return {str(k): {str(x) for x in v} for k, v in (st.get("revoked") or {}).items() if isinstance(v, list)}

    def reconcile(self, server: str, h: dict[str, Any]) -> dict[str, Any]:
        """Gốc khoá được cấp (`hello.roots`) ↔ `filehub.folders`. Gốc chưa có thư mục → THÊM thư mục chế độ lane (tên từ Không gian số, an
        toàn + duy nhất; gốc workspace → kind workspace). Gốc đang đồng bộ kiểu cũ (sync) → GIỮ NGUYÊN, chỉ báo. Thư mục lane của cổng này
        mà gốc không còn được cấp → đánh dấu `revoked` (hub_state; KHÔNG xoá gì ở máy hay trên Không gian số); cấp lại → hết đánh dấu.
        Có thay đổi → event `filehub.lane.roots` (chỉ tên/id)."""
        from ...termsafe import one_line
        from ..service import REF_PREFIX, aliases_of, folder_mode, safe_name

        granted = [r for r in (h.get("roots") or [])[:200] if isinstance(r, dict) and r.get("id")]
        gids = {str(r["id"])[:128] for r in granted}
        rows = self.fh.all_folders()
        by_root = {str(f.get("root_id")): f for f in rows if f.get("root_id")}
        taken = {f["name"] for f in rows} | {a for f in rows for a in aliases_of(f)}
        added: list[dict[str, Any]] = []
        sync: list[dict[str, Any]] = []
        new_rows: list[dict[str, Any]] = []
        for r in granted:
            rid = str(r["id"])[:128]
            f = by_root.get(rid)
            if f is not None:
                if folder_mode(f, self.hub.config) == "sync":
                    sync.append({"name": f["name"], "root_id": rid, "hint": SYNC_HINT.format(name=f["name"])})
                continue
            base = safe_name(one_line(str(r.get("name") or ""), 80), "chung")[:76].rstrip() or "chung"
            name, n = base, 1
            while name in taken:
                n += 1
                name = f"{base} {n}"
            kind = "workspace" if r.get("kind") == "workspace" else "folder"
            row = {"name": name, "root_id": rid, "ws_id": str(r.get("ws_id") or "")[:128] or None, "kind": kind, "mode": "lane", "server": server}
            new_rows.append(row)
            taken.add(name)
            by_root[rid] = row
            added.append({"name": name, "root_id": rid, "kind": kind, "workspace": f"{REF_PREFIX}{name}"})
        if new_rows:
            self.hub.config.set("filehub.folders", rows + new_rows)
            if not self.fh.enabled():
                self.hub.config.set("filehub.enabled", True)
        st = dict(self.hub.store.state_get("filehub.lane") or {})
        rev_all = {str(k): list(v) for k, v in (st.get("revoked") or {}).items() if isinstance(v, list)}
        prev = set(rev_all.get(server) or [])
        mine = [f for f in self.fh.lane_folders() if self.folder_server(f) == server]
        names = {str(f["root_id"]): str(f["name"]) for f in mine}
        now_rev = {rid for rid in names if rid not in gids}
        sync_names = sorted(x["name"] for x in sync)
        prev_sync = list((st.get("sync_reported") or {}).get(server) or [])
        changed = False
        if now_rev != prev:
            rev_all[server] = sorted(now_rev)
            if not rev_all[server]:
                rev_all.pop(server)
            st["revoked"] = rev_all
            changed = True
        if sync_names != prev_sync:
            st["sync_reported"] = {**(st.get("sync_reported") or {}), server: sync_names}
            changed = True
        if changed:
            self.hub.store.state_set("filehub.lane", st)
        newly = sorted(names[x] for x in now_rev - prev if x in names)
        back = sorted(names[x] for x in prev - now_rev if x in names)
        new_sync = [x for x in sync_names if x not in prev_sync]
        if added or newly or back or new_sync:
            self.hub.publish(make_event("filehub.lane.roots", None, {"server": server, "added": [x["name"] for x in added], "revoked": newly,
                                                                     "regranted": back, "sync": new_sync}, source="hub"))
        return {"added": added, "sync": sync, "revoked": sorted(names[x] for x in now_rev)}

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
        self.last_reconcile.pop(server, None)
        h = await self.hello(server, force=True)  # + đối soát gốc được cấp (thêm thư mục mới, đánh dấu gốc mất quyền)
        cur = await self.client(server).cursor()
        rec = self.last_reconcile.get(server) or {}
        return {"server": server, "ok": True, "protocol": h.get("protocol"), "user": (h.get("user") or {}).get("username"),
                "scope": (h.get("key") or {}).get("scope"), "roots": self._roots_view(h), "cursor": bool(cur), "expiry": self.key_expiry(server, h),
                "added": [x["name"] for x in rec.get("added") or []], "revoked": rec.get("revoked") or []}

    def disable(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.lane_disable {server}`: xoá khoá + cấu hình server + thư mục chế độ lane CHỈ có qua cổng này (do thiết lập tự thêm —
        không bản sao, gỡ khỏi cấu hình không đụng tệp nào). Thư mục từng đồng bộ (còn `local_path`) → từ chối (chuyển về sync trước) trừ
        khi `force`."""
        server = str(p.get("server") or "")
        self.server_cfg(server)
        mine = [f for f in self.fh.lane_folders() if self.folder_server(f) == server]
        using = [f["name"] for f in mine if f.get("local_path")]
        if using and not p.get("force"):
            raise Conflict(f"thư mục {', '.join(using)} (từng đồng bộ về máy) đang dùng cổng '{server}' — `cfharness filehub switch <tên> --to sync` "
                           "trước (hoặc --force)")
        if self._busy(server):
            raise Conflict(f"có run đang làm việc qua cổng '{server}' — dừng run trước")
        drop = {f["name"] for f in mine if not f.get("local_path")}
        if drop:
            self.hub.config.set("filehub.folders", [f for f in self.fh.all_folders() if f["name"] not in drop])
        st = dict(self.hub.store.state_get("filehub.lane") or {})
        if server in (st.get("revoked") or {}) or server in (st.get("sync_reported") or {}):
            st["revoked"] = {k: v for k, v in (st.get("revoked") or {}).items() if k != server}
            st["sync_reported"] = {k: v for k, v in (st.get("sync_reported") or {}).items() if k != server}
            self.hub.store.state_set("filehub.lane", st)
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
        return {"server": server, "removed": True, "folders": sorted(drop) + using}

    def _busy(self, server: str) -> bool:
        return any(wd.server == server and not wd.parked for wd in self.workdirs.values())

    # ------------------------------------------------------------------ cây làm việc theo run
    @property
    def state_dir(self) -> Path:
        return Path(self.hub.paths.home) / "filehub" / "lane"

    @property
    def orphan_dir(self) -> Path:
        return Path(self.hub.paths.home) / "filehub" / "lane-orphans"

    def ignore_patterns(self) -> list[str]:
        try:
            return list(self.fh.ignore_lines())
        except Exception:  # pragma: no cover
            from ..service import IGNORE_BASE

            return list(IGNORE_BASE)

    def tree_path(self, agent: dict[str, Any], tree: str) -> Path:
        return Path(agent["home_path"]) / "workspaces" / f"lane-{tree}"

    def is_tree_path(self, path: Any) -> bool:
        """`cwd` là cây lane (chuỗi resume/continue/review của run lane): `<HUB_HOME>/agents/<id>/workspaces/lane-<tree>`."""
        try:
            p = Path(str(path)).expanduser().resolve()
            rel = p.relative_to(Path(self.hub.paths.home).resolve() / "agents")
        except (ValueError, OSError, RuntimeError):
            return False
        parts = rel.parts
        return len(parts) == 3 and parts[1] == "workspaces" and parts[2].startswith("lane-")

    def workdir_of(self, run_id: str) -> Any:
        tree = self.by_run.get(run_id)
        return self.workdirs.get(tree) if tree else None

    def publish(self, etype: str, wd: Any, payload: dict[str, Any]) -> None:
        ev = make_event(etype, wd.agent_id if wd is not None else None,
                        {**({"run_id": wd.run_id, "folder": wd.folder} if wd is not None else {}), **payload}, run_id=wd.run_id if wd is not None else None,
                        source="hub")
        self.hub.publish(ev)

    async def steer(self, run_id: str, text: str) -> bool:
        """Steer câu cố định vào run đang chạy. Run chưa/không chạy (đang dựng cây, đang chờ người) → False (bên gọi giữ lại câu)."""
        from ...steer import steer

        run = self.hub.store.get_run(run_id) or {}
        if run.get("status") != "running":
            return False
        try:
            await steer(self.hub, run_id, text, by="hub", channel="hub")
            return True
        except HubError as e:
            log.info("lane: không steer được run %s: %s", run_id, e.message)
            return False

    async def prepare_run(self, run: dict[str, Any], agent: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        """Trước spawn: dựng/đỗ lại cây của run. Trả (cwd, run_opts đã ghi `tree`). Lỗi → HubError `filehub_unavailable: …` (run failed)."""
        from .workdir import Workdir

        opts = dict(run.get("run_opts") or {})
        lane = dict(opts.get("filehub_lane") or {})
        server, root = str(lane.get("server") or ""), str(lane.get("root") or "")
        tree = str(lane.get("tree") or run["id"])
        try:
            h = await self.hello(server)
            if root not in {str(r.get("id")) for r in h.get("roots") or [] if isinstance(r, dict)}:
                raise LaneError("root_not_granted", 403)
            ro = bool(opts.get("read_only"))
            wd = self.workdirs.get(tree)
            path = self.tree_path(agent, tree)
            if wd is None and (self.state_dir / f"{tree}.json").exists() and path.is_dir():
                wd = Workdir.load(self, self.state_dir / f"{tree}.json", path=path)  # daemon khởi động lại / resume sau waiting_human
            if wd is not None:
                wd.run_id, wd.parked, wd.read_only = run["id"], False, ro
                wd.steered = set()
                await wd.reconcile()
            else:
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
                wd = Workdir(self, tree=tree, run_id=run["id"], agent_id=agent["id"], folder=str(lane.get("folder") or ""), server=server,
                             root=root, sub=str(lane.get("sub") or ""), path=path, read_only=ro, kind=str(lane.get("kind") or "folder"))
                await wd.materialize()
        except LaneError as e:
            raise HubError(f"filehub_unavailable: {e.message}", data={"lane_code": e.lane_code}) from None
        except OSError as e:
            raise HubError(f"filehub_unavailable: không dựng được cây làm việc ({type(e).__name__})") from None
        self.workdirs[tree] = wd
        self.by_run[run["id"]] = tree
        lane["tree"] = tree
        opts["filehub_lane"] = lane
        if run.get("run_opts") != opts:
            self.hub.store.update_run(run["id"], {"run_opts": opts})
        wd.persist()
        self.ensure_stream(server, wd.cursor)
        wd.start()
        return wd.path, opts


    # ------------------------------------------------------------------ luồng sự kiện (một kết nối / server, chỉ khi có cây)
    def ensure_stream(self, server: str, cursor: str) -> None:
        t = self._streams.get(server)
        if t is not None and not t.done():
            return
        if cursor and not self._cursor(server):
            self._save_cursor(server, cursor)
        elif cursor:
            self._save_cursor(server, cursor)  # cây mới: nghe từ ảnh chụp của lượt list (không bỏ sót khung giữa list và nối luồng)
        try:
            self._streams[server] = asyncio.get_running_loop().create_task(self._stream_loop(server))
        except RuntimeError:  # pragma: no cover
            pass

    def _maybe_stop_stream(self, server: str) -> None:
        if any(wd.server == server and not wd.parked for wd in self.workdirs.values()):
            return
        t = self._streams.pop(server, None)
        if t is not None:
            t.cancel()

    def _cursor(self, server: str) -> str:
        st = self.hub.store.state_get("filehub.lane") or {}
        return str(((st.get("cursors") or {}).get(server)) or "")

    def _save_cursor(self, server: str, cursor: str) -> None:
        st = dict(self.hub.store.state_get("filehub.lane") or {})
        cur = dict(st.get("cursors") or {})
        if cur.get(server) == cursor:
            return
        cur[server] = cursor
        st["cursors"] = cur
        self.hub.store.state_set("filehub.lane", st)

    async def _stream_loop(self, server: str) -> None:
        lo, hi = (list(self.cfg("reconnect_s") or [0.5, 10]) + [0.5, 10])[:2]
        delay = float(lo)
        while True:
            try:
                c = self.client(server)
                async for ev in c.events(self._cursor(server) or None):
                    delay = float(lo)
                    if self.states.get(server, {}).get("state") in ("offline", None):
                        self._set_state(server, "ok")
                    stop = await self._on_frame(server, ev)
                    if ev.id and ev.type not in ("ping",):
                        self._save_cursor(server, ev.id)
                    if stop:
                        return
            except asyncio.CancelledError:
                raise
            except LaneError as e:
                self._on_error(server, e)
                if e.lane_code in ("unauthorized", "lane_disabled", "version_mismatch"):
                    await self._stop_pushes(server, e.lane_code)
                    return
            except Exception:  # pragma: no cover — vòng nền không được chết vì một khung hỏng
                log.exception("lane: luồng sự kiện %s lỗi", server)
            self.stream_reconnects[server] = self.stream_reconnects.get(server, 0) + 1
            await asyncio.sleep(delay)
            delay = min(float(hi), delay * 2)

    async def _on_frame(self, server: str, ev: Any) -> bool:
        recv = time.time()
        wds = [wd for wd in self.workdirs.values() if wd.server == server and not wd.parked]
        if ev.type == "change":
            root = str(ev.data.get("root") or "")
            for wd in wds:
                if wd.root == root:
                    await wd.on_change(ev.data, at_recv=recv)
        elif ev.type == "reset":
            root = str(ev.data.get("root") or "")
            for wd in wds:
                if wd.root == root:
                    try:
                        await wd.reconcile()
                    except (LaneError, HubError, OSError) as e:
                        log.warning("lane: đối soát lại sau reset lỗi: %s", e)
            cur = await self.client(server).cursor()
            self._save_cursor(server, cur)
        elif ev.type == "root_revoked":
            root = str(ev.data.get("root") or "")
            self._hello.pop(server, None)
            self._spawn(self._rehello(server))  # đối soát ngay: thư mục của gốc này hiện "mất quyền" (không xoá gì)
            for wd in wds:
                if wd.root == root:
                    wd.revoked = True
                    from .workdir import STEER_REVOKED

                    await wd.steer("revoked", STEER_REVOKED)
        elif ev.type == "key_revoked":
            self._set_state(server, "auth_failed", code="key_revoked")
            await self._stop_pushes(server, "unauthorized")
            return True
        return False

    async def _stop_pushes(self, server: str, code: str) -> None:
        for wd in self.workdirs.values():
            if wd.server == server:
                wd.stopped_push = code

    # ------------------------------------------------------------------ fsself.call (tool hub-self `fs_*`)
    async def fsself_call(self, p: dict[str, Any]) -> Any:
        from ... import auth as _auth
        from .tools import call

        run_id, op = str(p.get("run_id") or ""), str(p.get("op") or "")
        run = self.hub.store.get_run(run_id)
        if run is None or run["status"] not in ("queued", "running"):
            raise HubError("run không còn chạy — fs_* chỉ dùng được trong run đang chạy")
        c = _auth.current_caller()
        if c is not None and c.via == "socket" and not c.human and c.run_id != run["id"]:
            raise Blocked("fs_*: run_id không khớp tiến trình gọi (chỉ hub-self của chính run đó)", data={"caller": c.to_dict()})
        wd = self.workdir_of(run_id)
        if wd is None:
            raise Blocked("run này không có workspace thư mục chung chế độ lane (cổng cfharness)")
        from ...runs import _agent_read_only

        agent = self.hub.store.get_agent(run["agent_id"]) or {}
        ro = bool((run.get("run_opts") or {}).get("read_only")) or _agent_read_only(agent)
        try:
            return await call(self, wd, op, dict(p.get("args") or {}), read_only=ro)
        except LaneError as e:
            if e.lane_code in ("unauthorized", "lane_disabled"):
                self._on_error(wd.server, e)
            raise HubError(e.message, data={"lane_code": e.lane_code}) from None

    async def pull(self, wd: Any, rel: str) -> dict[str, Any]:
        """fs_pull: tải tệp (hoặc thư mục, trong ngưỡng cây) vào cây run; tệp đã sửa cục bộ → giữ bản của agent thành bản xung đột."""
        c = self.client(wd.server)
        it = await c.stat(wd.root, wd.remote(rel))
        async with wd.lock:
            if it.get("type") == "folder":
                items, _cur, cut = await c.list_all(wd.root, wd.remote(rel), recursive=True, max_items=int(self.cfg("tree_max_files") or 20000))
                if cut:
                    raise HubError("thư mục quá nhiều tệp — fs_pull từng thư mục con")
                n = 0
                for x in items:
                    r = wd.rel_of(str(x.get("path") or ""))
                    if not r:
                        continue
                    if wd.ignored_item(r, x.get("type")):
                        wd.hidden.add(r)
                        continue
                    if x.get("type") == "folder":
                        wd.fs.mkdirs(r)
                        wd.base[r] = wd._base_of(x)
                    else:
                        if wd.fs.is_file(r) and wd.modified_locally(r):
                            await wd._conflict_local(r)
                        await wd._download(r, x)
                        n += 1
                wd.fs.mkdirs(rel)
                wd.base[rel] = wd._base_of(it)
                wd.persist()
                return {"path": rel, "type": "folder", "files": n, "local": str(wd.path / rel)}
            if wd.ignored(rel):
                raise HubError(f"'{rel}' khớp mẫu bỏ qua (tệp tạm/khoá/.git) — không kéo vào cây")
            if wd.fs.is_file(rel) and wd.modified_locally(rel):
                await wd._conflict_local(rel)
            b = await wd._download(rel, it)
            wd.read_paths.add(rel)
            wd.persist()
        return {"path": rel, "type": "file", "size": b.get("size"), "version_id": b.get("version_id"), "local": str(wd.path / rel)}

    async def flush_run(self, run_id: str) -> dict[str, Any] | None:
        """Cuối run (TRƯỚC event kết thúc): quét lần cuối + đẩy hết (≤ `final_flush_s`) → `runs.diff` từ nhật ký thao tác + `workspace.diff`."""
        wd = self.workdir_of(run_id)
        if wd is None:
            return None
        await wd.flush(timeout_s=float(self.cfg("final_flush_s") or 60))
        wd.flushed = True
        st = {"added": "added", "modified": "modified", "deleted": "deleted", "renamed": "renamed"}
        files = [{"status": st.get(o.get("op"), "modified"), "path": o.get("path"), **({"from": o["from"]} if o.get("from") else {})}
                 for o in wd.run_ops() if o.get("op") in st]
        diff = {"kind": "lane", "root": str(wd.path), "filehub": wd.folder, "lane": {"server": wd.server, "root": wd.root, "sub": wd.sub},
                "files": files[:500], "stat": {"files": len(files), "unpushed": len(wd.unpushed())}, "revertible": False}
        self.hub.store.update_run(run_id, {"diff": diff})
        ev = make_event("workspace.diff", wd.agent_id, {"kind": "lane", "stat": diff["stat"], "files": [f"{f['status']}:{f['path']}" for f in files[:50]],
                                                        "revertible": False}, run_id=run_id)
        self.hub.publish(ev)
        return diff

    async def end_run(self, run_id: str) -> None:
        """Sau run: `waiting_human`/`interrupted` → ĐỖ cây (giữ base + presence, resume dùng lại); còn lại → báo thay đổi, xoá presence,
        còn thay đổi chưa đẩy → chuyển cây sang `filehub/lane-orphans/<tree>` (event `filehub.push_failed`), không thì xoá cây."""
        tree = self.by_run.pop(run_id, None)
        wd = self.workdirs.get(tree) if tree else None
        if wd is None:
            return
        status = (self.hub.store.get_run(run_id) or {}).get("status")
        if status in ("waiting_human", "interrupted"):
            await wd.stop()
            wd.parked = True
            wd.persist()
            self._maybe_stop_stream(wd.server)
            return
        if not getattr(wd, "flushed", False):
            try:
                await wd.flush(timeout_s=float(self.cfg("final_flush_s") or 60))
            except Exception:  # pragma: no cover
                log.exception("lane: flush cuối run %s lỗi", run_id)
        await self.presence_clear(wd)
        self._report(wd)
        await self._close_tree(wd)

    def _report(self, wd: Any) -> None:
        from ...termsafe import one_line

        ops = wd.run_ops()
        changed = [o["path"] for o in ops if o.get("op") in ("added", "modified", "renamed")]
        deleted = [o["path"] for o in ops if o.get("op") == "deleted"]
        for etype, paths in (("filehub.changed", changed), ("filehub.deleted", deleted)):
            if paths:
                uniq = list(dict.fromkeys(paths))
                self.publish(etype, wd, {"count": len(uniq), "paths": [one_line(x, 300) for x in uniq[:20]], "url": None, "lane": True})

    async def _close_tree(self, wd: Any) -> None:
        self.workdirs.pop(wd.tree, None)
        left = wd.unpushed()
        if left and wd.path.is_dir():
            self._orphan(wd, left)
        else:
            shutil.rmtree(wd.path, ignore_errors=True)
            try:
                wd.state_path.unlink()
            except OSError:
                pass
        self._maybe_stop_stream(wd.server)

    def _orphan(self, wd: Any, left: list[str]) -> None:
        from ...termsafe import one_line

        dst = self.orphan_dir / wd.tree
        self.orphan_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.orphan_dir, 0o700)
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(mode=0o700)
        shutil.move(str(wd.path), str(dst / "tree"))
        wd.path = dst / "tree"
        try:
            wd.state_path.replace(dst / "state.json")
        except OSError:
            pass
        wd.persist_to(dst / "state.json")
        code = (wd.stopped_push or ("mass_delete" if wd.held_deletes() else None) or next(iter(set(wd.failed.values()) - {"hardlink"}), None)
                or ("secret_blocked" if wd.blocked else "pending"))
        self.publish("filehub.push_failed", wd, {"count": len(left), "paths": [one_line(x, 300) for x in left[:20]], "code": code, "tree": wd.tree})

    def _task_of(self, run_id: Any) -> str | None:
        try:
            return (self.hub.store.get_run(str(run_id)) or {}).get("task_id") if run_id else None
        except Exception:  # pragma: no cover
            return None

    @staticmethod
    def change_items(st: dict[str, Any]) -> list[dict[str, Any]]:
        """Thay đổi còn kẹt của một cây (trạng thái đã lưu) → từng mục kèm THAO TÁC sẽ làm trên Không gian số (cho người quyết): xoá / xoá thư
        mục / ghi / di chuyển / tạo thư mục; tệp bị chặn (secret) / lỗi đẩy = ghi kèm lý do. Lô xoá đang giữ đánh dấu `held`."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r, e in sorted((st.get("pending") or {}).items()):
            op = str(e.get("op") or "write")
            if op == "move_src":
                continue
            it = {"path": r, "op": op, "label": OP_TEXT.get(op, op), "held": bool(e.get("held"))}
            if op == "move" and e.get("from"):
                it["from"] = str(e["from"])
            out.append(it)
            seen.add(r)
        for r in sorted(st.get("blocked") or {}):
            if r not in seen:
                out.append({"path": r, "op": "write", "label": "ghi (bị chặn: chứa secret)", "held": False})
                seen.add(r)
        for r, c in sorted((st.get("failed") or {}).items()):
            if r not in seen and c != "hardlink":
                out.append({"path": r, "op": "write", "label": f"ghi (lỗi: {c})", "held": False})
        return out

    def orphans(self) -> list[dict[str, Any]]:
        import json as _json

        from ...termsafe import one_line

        out = []
        if not self.orphan_dir.is_dir():
            return out
        for d in sorted(self.orphan_dir.iterdir()):
            try:
                st = _json.loads((d / "state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items = self.change_items(st)
            held = [x["path"] for x in items if x["held"]]
            cut = [{**x, "path": one_line(x["path"], 300), **({"from": one_line(x["from"], 300)} if x.get("from") else {})} for x in items[:ORPHAN_SHOW]]
            out.append({"tree": d.name, "folder": st.get("folder"), "root_id": st.get("root"), "run_id": st.get("run_id"), "agent_id": st.get("agent_id"),
                        "task_id": self._task_of(st.get("run_id")), "count": len(items), "paths": [x["path"] for x in cut][:20], "items": cut,
                        "held": len(held), "held_paths": [one_line(x, 300) for x in held[:ORPHAN_SHOW]], "since": d.stat().st_mtime})
        return out

    async def orphan(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.lane_orphan {tree, action: push|drop}` (CHỈ người): đẩy lại thay đổi còn kẹt của cây mồ côi, hoặc bỏ hẳn (xoá cây)."""
        from .workdir import Workdir

        tree, action = str(p.get("tree") or ""), str(p.get("action") or "")
        d = self.orphan_dir / tree
        if not tree or "/" in tree or tree.startswith(".") or not (d / "state.json").is_file():
            raise NotFound(f"không có cây mồ côi '{tree}'")
        if action == "drop":
            shutil.rmtree(d, ignore_errors=True)
            return {"tree": tree, "dropped": True}
        if action != "push":
            raise HubError("action phải là push|drop", code=-32602)
        wd = Workdir.load(self, d / "state.json", path=d / "tree")
        wd.parked, wd.stopped_push, wd.read_only = False, None, False
        wd.failed = {k: v for k, v in wd.failed.items() if v == "hardlink"}
        wd.blocked = {}
        # "Đẩy lại" KHÔNG BAO GIỜ ngầm duyệt lô xoá đang giữ: lô đó chỉ đi qua Cho xoá / Không xoá (`filehub.resume`, nêu từng tệp)
        for r in list(wd.pending):
            wd.pending[r] = {**wd.pending[r], "retry_at": 0}
        await wd.flush(timeout_s=float(self.cfg("final_flush_s") or 60))
        left = wd.unpushed()
        held = len(wd.held_deletes())
        if left:
            wd.persist_to(d / "state.json")
            return {"tree": tree, "pushed": False, "left": left[:50], "held": held, "code": wd.stopped_push or ("mass_delete" if held else "pending")}
        self._report(wd)
        shutil.rmtree(d, ignore_errors=True)
        return {"tree": tree, "pushed": True, "held": 0}

    async def _release_orphan(self, d: Path, *, discard: bool) -> int:
        """Lô xoá đang giữ của một cây MỒ CÔI (run đã kết thúc) — cùng nghĩa như khi run còn chạy: Cho xoá = đẩy đúng các lệnh xoá đó;
        Không xoá = bỏ các lệnh xoá (tệp ở lại Không gian số; cây mồ côi không còn agent nên không kéo về). Thay đổi khác giữ nguyên."""
        from .workdir import Workdir

        wd = Workdir.load(self, d / "state.json", path=d / "tree")
        held = wd.held_deletes()
        if not held:
            return 0
        if discard:
            for r in held:
                wd.pending.pop(r, None)
        else:
            wd.parked, wd.stopped_push, wd.read_only = False, None, False
            for r in held:
                wd.pending[r] = {**{k: v for k, v in wd.pending[r].items() if k != "held"}, "approved": True, "retry_at": 0}
            loop = asyncio.get_running_loop()
            deadline = loop.time() + float(self.cfg("final_flush_s") or 60)
            todo = set(held)
            while todo & set(wd.pending) and not wd.stopped_push and loop.time() < deadline:
                await wd.push_pending(final=True, only=todo)
                due = [wd.pending[r] for r in todo & set(wd.pending)]
                if due:
                    nxt = min(float(e.get("retry_at") or 0) for e in due) - time.time()
                    await asyncio.sleep(min(max(0.05, nxt), 1.0))
        if not wd.unpushed():
            if not discard:
                self._report(wd)
            shutil.rmtree(d, ignore_errors=True)
        else:
            wd.persist_to(d / "state.json")
        return len(held)

    async def release_deletes(self, root_id: str, *, discard: bool = False) -> dict[str, Any]:
        """CHỈ người (RPC `filehub.resume {root_id, discard?}`): lô xoá hàng loạt đang giữ của mọi cây trên gốc này → đẩy tiếp, hoặc bỏ
        (`discard`: không xoá trên Không gian số, kéo tệp về lại cây run, agent được báo)."""
        n = 0
        trees = [wd for wd in self.workdirs.values() if wd.root == str(root_id)]
        for wd in trees:
            n += await wd.release_deletes(discard=discard)
        # run đã kết thúc khi lô còn giữ → cây mồ côi: lô xoá vẫn chờ CÙNG quyết định (không đổi nghĩa thành "đẩy lại")
        orphans = [o for o in self.orphans() if o.get("held") and str(o.get("root_id")) == str(root_id)]
        for o in orphans:
            n += await self._release_orphan(self.orphan_dir / str(o["tree"]), discard=discard)
        return {"root_id": root_id, "lane": True, "released" if not discard else "discarded": n, "trees": len(trees) + len(orphans)}

    # ------------------------------------------------------------------ presence (C.6 `PUT /presence`): "cfharness · <agent> · đang sửa <tệp>"
    async def presence_tick(self, wd: Any) -> None:
        """Làm mới presence của run GHI ở nửa ttl, hoặc ngay khi tệp đẩy gần nhất đổi (nhãn do server dựng — hub chỉ gửi đường dẫn)."""
        if wd.read_only or wd.parked or wd.revoked or wd.stopped_push:
            return
        ttl = int(self.cfg("presence_ttl_s") or 300)
        path = wd.remote(wd.last_pushed) if wd.last_pushed else None
        if time.time() - wd._presence_at < ttl / 2 and path == getattr(wd, "_presence_path", None):
            return
        wd._presence_at, wd._presence_path = time.time(), path
        try:
            await self.client(wd.server).presence_put(wd.root, path, ttl, agent=wd.agent_id, run=wd.run_id)
        except LaneError as e:
            log.info("lane: presence lỗi (%s): %s", wd.run_id, e.lane_code)

    async def presence_clear(self, wd: Any) -> None:
        if wd.read_only or not wd._presence_at:
            return
        wd._presence_at = 0.0
        try:
            await self.client(wd.server).presence_delete(wd.root, agent=wd.agent_id, run=wd.run_id)
        except LaneError as e:
            log.info("lane: xoá presence lỗi (%s): %s", wd.run_id, e.lane_code)

    # ------------------------------------------------------------------ chuyển chế độ (CHỈ người) + thêm thư mục lane
    async def add_folder(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.lane_add {root_id, name?, server?}`: thêm thư mục chung chế độ lane từ một gốc khoá được cấp (không bản sao trên máy)."""
        from ..service import REF_PREFIX, aliases_of, safe_name, valid_name

        server = str(p.get("server") or self.default_server() or "")
        self.server_cfg(server)
        rid = str(p.get("root_id") or "")
        h = await self.hello(server, force=True, reconcile=False)  # người chọn tên: không để đối soát tự thêm trước với tên khác
        r = next((x for x in h.get("roots") or [] if isinstance(x, dict) and str(x.get("id")) == rid), None)
        if r is None:
            raise NotFound(f"gốc {rid!r} không được cấp cho khoá cổng '{server}' (xem `cfharness filehub lane test`)")
        name = str(p.get("name") or "").strip() or safe_name(str(r.get("name") or ""), "chung")
        if not valid_name(name):
            raise HubError(f"tên thư mục chung không hợp lệ: {name!r}")
        rows = self.fh.all_folders()
        if any(f["name"] == name or name in aliases_of(f) or str(f.get("root_id")) == rid for f in rows):
            raise Conflict(f"thư mục chung '{name}' (hoặc gốc {rid}) đã có — đổi chế độ bằng `cfharness filehub switch`")
        row = {"name": name, "root_id": rid, "ws_id": str(r.get("ws_id") or "") or None, "kind": "workspace" if r.get("kind") == "workspace" else "folder",
               "mode": "lane", "server": server}
        self.hub.config.set("filehub.folders", rows + [row])
        if not self.fh.enabled():
            self.hub.config.set("filehub.enabled", True)
        return {**row, "workspace": f"{REF_PREFIX}{name}"}

    async def switch(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.switch {name, to: lane|sync, server?, force?}` (CHỈ người).

        → lane: gốc phải được cấp cho khoá; đồng bộ cũ của gốc đó phải yên (status.json: không chờ đẩy/kéo, không bản xung đột, không
        bị chặn) và không có run giữ khoá → `folders[].mode = lane` → binary sync bỏ gốc (`--remove-root`). Bản sao `~/KhongGianSo/<tên>`
        để NGUYÊN (chủ repo chốt: hub không bao giờ xoá/di chuyển) và được báo ở trạng thái.
        → sync: chỉ khi thư mục còn bản sao cục bộ (`local_path`) và không có run lane đang làm việc trên gốc."""
        from ..service import _real

        name, to = str(p.get("name") or ""), str(p.get("to") or "")
        if to not in ("lane", "sync"):
            raise HubError("to phải là lane|sync", code=-32602)
        rows = self.fh.all_folders()
        f = next((x for x in rows if x["name"] == name), None)
        if f is None:
            raise NotFound(f"không có thư mục chung '{name}'")
        from ..service import folder_mode

        cur = folder_mode(f, self.hub.config)
        if cur == to:
            raise HubError(f"thư mục '{name}' đã ở chế độ {to}")
        warn: list[str] = []
        if to == "lane":
            server = str(p.get("server") or f.get("server") or self.default_server() or "")
            self.server_cfg(server)
            h = await self.hello(server, force=True)
            if str(f.get("root_id")) not in {str(r.get("id")) for r in h.get("roots") or [] if isinstance(r, dict)}:
                raise HubError(f"gốc {f.get('root_id')} của '{name}' chưa được cấp cho khoá cổng '{server}' — cấp ở Không gian số › Hồ sơ › Cổng cfharness")
            if f.get("local_path") and self.hub.runs.locks.holder(_real(f["local_path"])):
                raise Conflict(f"thư mục '{name}' đang có run ghi — chờ xong rồi chuyển")
            st = self.fh.read_status() or {}
            r = next((x for x in st.get("roots") or [] if isinstance(x, dict) and x.get("id") == f.get("root_id")), None)
            if r is None:
                if not p.get("force"):
                    raise Conflict(f"không đọc được trạng thái đồng bộ của '{name}' — không biết còn thay đổi chưa đồng bộ không. Chạy đồng bộ tới khi "
                                   "yên rồi chuyển, hoặc --force nếu chắc chắn")
                warn.append("không kiểm được trạng thái đồng bộ (--force)")
            else:
                busy = [k for k in ("pending_up", "pending_down") if r.get(k)] + (["conflicts"] if r.get("conflicts") else []) + (["blocked"] if r.get("blocked") else [])
                if busy or str(r.get("state") or "idle") not in ("idle", "ok", "synced"):
                    raise Conflict(f"đồng bộ của '{name}' chưa yên ({', '.join(busy) or r.get('state')}) — chờ đồng bộ xong / xử lý bản xung đột rồi chuyển")
            nf = {**f, "mode": "lane", "server": server}
        else:
            if not f.get("local_path") or not os.path.isdir(os.path.expanduser(str(f["local_path"]))):
                raise HubError(f"thư mục '{name}' không có bản sao trên máy — thêm lại bằng `cfharness filehub add` (chế độ sync)")
            srv = self.folder_server(f)
            if srv and self.hub.runs.locks.holder(lock_key(srv, str(f.get("root_id")))):
                raise Conflict(f"thư mục '{name}' đang có run làm việc qua cổng — chờ xong rồi chuyển")
            nf = {**f, "mode": "sync"}
            warn.append("bản sao trên máy có thể cũ hơn Không gian số — đồng bộ sẽ đối chiếu lại")
        self.hub.config.set("filehub.folders", [nf if x["name"] == name else x for x in rows])
        try:
            self.fh._apply_change()  # binary sync: thêm lại / bỏ gốc (`--remove-root` cho gốc không còn ở chế độ sync)
        except Exception:  # pragma: no cover
            log.exception("lane: áp đổi chế độ cho binary sync lỗi")
        out = {"name": name, "mode": to, "server": nf.get("server"), "workspace": f"filehub:{name}", "warnings": warn}
        if to == "lane" and f.get("local_path"):
            out["old_copy"] = f["local_path"]
            out["note"] = (f"bản sao cũ {f['local_path']} GIỮ NGUYÊN (hub không xoá) — kiểm rồi tự xoá; agent làm việc qua cây theo run từ nay. "
                           "Khi mọi thư mục đã ở chế độ lane: thu hồi thiết bị đồng bộ ở Không gian số › Hồ sơ › Thiết bị.")
        return out

    # ------------------------------------------------------------------ vòng nền: hạn khoá, mồ côi quá hạn, cây của run đã xong (daemon khởi động lại)
    def start(self) -> None:
        if self._maint is None or self._maint.done():
            try:
                self._maint = asyncio.get_running_loop().create_task(self._maintenance_loop())
            except RuntimeError:  # pragma: no cover
                self._maint = None
        if self._health is None or self._health.done():
            try:
                self._health = asyncio.get_running_loop().create_task(self._health_loop())
            except RuntimeError:  # pragma: no cover
                self._health = None

    async def _rehello(self, server: str) -> None:
        try:
            await self.hello(server, force=True)
        except HubError as e:
            log.info("lane: hello lại %s lỗi: %s", server, e.message)

    async def _health_loop(self) -> None:
        while True:
            try:
                await self.health_check()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("lane: kiểm tra sức khoẻ lỗi")
            await asyncio.sleep(1.0)

    async def health_check(self, now: float | None = None) -> None:
        """Một lượt kiểm tra sức khoẻ (cả khi không có run — luồng sự kiện chỉ mở khi có cây): `hello` mỗi `health_ok_s` khi ổn (đối soát gốc
        mới cấp / mất quyền), lùi dần `health_retry_s` khi lỗi → `offline` tự về `ok` khi Không gian số chạy lại, không cần người."""
        from ...runs import export_quiesced

        if export_quiesced(self.hub):
            return
        now = time.time() if now is None else float(now)
        ok_s = float(self.cfg("health_ok_s") or 60)
        lo, hi = [float(x) for x in (list(self.cfg("health_retry_s") or [5, 300]) + [5, 300])[:2]]
        for name in list(self.servers()):
            if not self._key(name):
                continue
            state = (self.states.get(name) or {}).get("state")
            fail = self._health_fail.get(name, 0)
            wait = ok_s if state in ("ok", "key_expiring") else min(hi, lo * (2 ** max(0, fail - 1)))
            if now < self._health_last.get(name, 0.0) + wait:
                continue
            self._health_last[name] = now
            try:
                await self.hello(name, force=True)
                self._health_fail[name] = 0
            except HubError as e:
                self._health_fail[name] = fail + 1
                log.info("lane: kiểm tra sức khoẻ %s: %s", name, e.message)

    async def _maintenance_loop(self) -> None:
        await asyncio.sleep(1.0)
        from ...runs import export_quiesced

        while True:
            try:
                if not export_quiesced(self.hub):  # đang xuất gói / đóng băng chuyển nhà → bỏ lượt (không đẩy, không ghi trạng thái)
                    await self.maintenance()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("lane: vòng bảo trì lỗi")
            await asyncio.sleep(max(1.0, float(self.cfg("maintenance_s") or 60)))

    async def maintenance(self) -> None:
        from ...runs import export_quiesced

        if export_quiesced(self.hub):  # đang xuất gói / đã đóng băng chuyển nhà: không đẩy, không ghi trạng thái
            return
        day = time.strftime("%Y-%m-%d")
        st = dict(self.hub.store.state_get("filehub.lane") or {})
        warned = dict(st.get("key_warned") or {})
        for name in self.servers():
            exp = self.key_expiry(name)
            if exp.get("warn") and warned.get(name) != day:
                warned[name] = day
                self._set_state(name, "key_expiring", days_left=exp.get("days_left"))
        if warned != (st.get("key_warned") or {}):
            st["key_warned"] = warned
            self.hub.store.state_set("filehub.lane", st)
        keep = float(self.cfg("orphan_keep_days") or 7) * 86400
        if self.orphan_dir.is_dir():
            for d in self.orphan_dir.iterdir():
                try:
                    if time.time() - d.stat().st_mtime > keep:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
        await self.recover()

    def _tree_busy(self, tree: str) -> bool:
        import json as _json

        rows = self.hub.store.conn.execute(
            "SELECT run_opts, status FROM runs WHERE run_opts LIKE ? AND status IN ('queued','running','waiting_human','interrupted')", (f"%{tree}%",)
        ).fetchall()
        for r in rows:
            try:
                o = _json.loads(r[0]) if isinstance(r[0], str) else (r[0] or {})
            except ValueError:
                continue
            if ((o or {}).get("filehub_lane") or {}).get("tree") == tree:
                return True
        return False

    async def recover(self) -> None:
        """Cây có trạng thái trên đĩa mà không thuộc run nào đang chạy/chờ/đỗ (daemon chết giữa run, run đã kết thúc) → đẩy nốt hoặc mồ côi."""
        from .workdir import Workdir

        if not self.state_dir.is_dir():
            return
        for f in sorted(self.state_dir.glob("*.json")):
            tree = f.stem
            if tree in self.workdirs or self._tree_busy(tree):
                continue
            try:
                wd = Workdir.load(self, f)
            except (OSError, ValueError, KeyError):
                f.unlink(missing_ok=True)
                continue
            if not wd.path.is_dir():
                f.unlink(missing_ok=True)
                continue
            if wd.server not in self.servers():
                continue
            wd.parked = False
            if wd.read_only:
                # run CHỈ ĐỌC (trạng thái bền giữ cờ): không bao giờ đẩy — chỉ dọn cây (mọi thứ trong cây là bản đọc)
                wd.pending, wd.blocked, wd.failed = {}, {}, {}
            else:
                try:
                    await wd.flush(timeout_s=float(self.cfg("final_flush_s") or 60))
                except Exception as e:  # pragma: no cover
                    log.warning("lane: đẩy nốt cây %s lỗi: %s", tree, e)
            wd.ops_mark = 0
            self._report(wd)
            await self._close_tree(wd)

    # ------------------------------------------------------------------ fs_* ghi (H3)
    async def write_op(self, wd: Any, op: str, args: dict[str, Any]) -> Any:
        from .tools import rel_arg

        c = self.client(wd.server)
        async with wd.lock:
            if op == "fs_write":
                rel = rel_arg(wd, args.get("path"), allow_root=False)
                content = args.get("content")
                if not isinstance(content, str):
                    raise HubError("content phải là chuỗi", code=-32602)
                if args.get("create_only"):
                    try:
                        await c.stat(wd.root, wd.remote(rel))
                        raise HubError(f"'{rel}' đã có trên Không gian số (create_only)")
                    except LaneError as e:
                        if e.lane_code != "not_found":
                            raise
                from .workdir import TMP_PREFIX

                wd.fs.write_atomic(rel, content.encode("utf-8"), tmp_prefix=TMP_PREFIX)  # không theo symlink ở bất kỳ cấp nào
                return await wd.push_now(rel)
            if op == "fs_mkdir":
                rel = rel_arg(wd, args.get("path"), allow_root=False)
                wd.fs.mkdirs(rel)
                item = await c.mkdir(wd.root, wd.remote(rel), agent=wd.agent_id, run=wd.run_id)
                wd.base[rel] = wd._base_of(item)
                wd._op("added", rel, folder=True)
                wd.persist()
                return {"path": rel, "created": True}
            if op == "fs_move":
                src, dst = rel_arg(wd, args.get("from"), allow_root=False), rel_arg(wd, args.get("to"), allow_root=False)
                wd.fs.check(src)  # TRƯỚC lệnh trên server: cây có symlink ở đường dẫn → từ chối, không đi xuyên (daemon không sandbox)
                wd.fs.check(dst)
                st = await c.stat(wd.root, wd.remote(src))
                item = await c.move(wd.root, wd.remote(src), wd.remote(dst), if_rev=int(st.get("rev") or 0) or None, agent=wd.agent_id, run=wd.run_id)
                wd.fs.rename(src, dst, overwrite=False)
                for k in [k for k in list(wd.base) if k == src or k.startswith(src + "/")]:
                    wd.base[dst + k[len(src):]] = wd.base.pop(k)
                    wd.snapshot.pop(k, None)
                    wd._snap(dst + k[len(src):])
                if dst in wd.base:
                    wd.base[dst]["rev"] = int(item.get("rev") or 0)
                wd._op("renamed", dst, **{"from": src})
                wd.persist()
                return {"from": src, "to": dst, "moved": True}
            if op == "fs_delete":
                rel = rel_arg(wd, args.get("path"), allow_root=False)
                wd.fs.check(rel, final_link_ok=True)  # TRƯỚC lệnh trên server (như fs_move); chính mục là symlink → chỉ gỡ symlink
                st = await c.stat(wd.root, wd.remote(rel))
                # CÙNG đường xoá với quét: chặn xoá hàng loạt + đếm chung, bỏ qua mục bị bỏ qua/vô hình, base cập nhật theo từng mục
                return await wd.tool_delete(rel, st)
            if op == "fs_restore":
                rel = rel_arg(wd, args.get("path"), allow_root=False)
                st = await c.stat(wd.root, wd.remote(rel))
                item = await c.restore(wd.root, agent=wd.agent_id, run=wd.run_id, path=wd.remote(rel), version_id=str(args.get("version_id") or ""),
                                       if_match=str(st.get("version_id") or ""))
                if wd.mode == "tree" or rel in wd.base:
                    await wd._download(rel, item)
                wd._op("modified", rel, restored=True)
                wd.persist()
                return {"path": rel, "restored": True, "version_id": item.get("version_id")}
        raise HubError(f"không có tool '{op}'")

    def _on_bus(self, ev: Any) -> None:
        """Tool của run vừa xong (Write/Edit/Bash…) → kích quét cây ngay (không chờ nhịp `scan_interval_s`)."""
        try:
            if ev.run_id and ev.type in ("tool.complete",) and ev.run_id in self.by_run:
                wd = self.workdir_of(ev.run_id)
                if wd is not None:
                    wd.kick()
        except Exception:  # pragma: no cover — listener không được làm hỏng bus
            pass

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
        for t in list(self._streams.values()) + ([self._maint] if self._maint else []) + ([self._health] if self._health else []):
            t.cancel()
        self._streams.clear()
        for wd in list(self.workdirs.values()):
            await wd.stop()
        for _fp, c in list(self._clients.values()):
            await c.aclose()
        self._clients.clear()


__all__ = ["LaneService", "STATE_TEXT", "SECRET_PREFIX", "secret_name", "lock_key", "validate_setting", "LOCK_PREFIX"]
