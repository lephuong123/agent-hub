"""`hub.filehub` — thư mục dùng chung người ↔ agent trên "Không gian số" qua CỔNG CFHARNESS (docs/dev-notes/filehub.md,
plan-filehub-lane.md). Cổng (chế độ `lane`, filehub/lane/) là chế độ DUY NHẤT: không bản sao lâu dài trên máy, mỗi run có cây làm việc riêng.

- Cấu hình ở `config.yaml` nhánh `filehub.*` (∈ `integrity.SECURITY_KEYS`; chỉ daemon ghi theo lệnh người). `filehub.folders =
  [{name, root_id, ws_id, kind, mode: lane, server, aliases?, local_path?}]`. `kind` = `folder` (MỘT thư mục) | `workspace` (TOÀN BỘ một
  workspace — gốc ảo `root_id = ws_id`).
- Đồng bộ 2 chiều cũ (`tang-thu-sync-headless`, H5 gỡ): thư mục còn `mode: sync` (hoặc thiếu `mode` mà có `local_path`) = CẦN CHUYỂN
  (`legacy_folders`): hub không chạy gì cho chúng, không xoá gì (bản sao `local_path` để nguyên), run nhắm vào → lỗi `LEGACY_TEXT`; khoá cổng
  cấp đúng gốc đó → `LaneService.reconcile` tự chuyển sang `lane` (giữ `local_path` để báo "bản sao cũ"). Khoá cấu hình cũ của đồng bộ
  (`sync_binary`, `local_parent`, `device_name`, …) được bỏ qua — không bao giờ lỗi vì chúng.
- Daemon khởi động: tiến trình sync còn sót của daemon cũ (`HUB_HOME/filehub/supervisor.json`, đối soát `(pid, start_time)`) → SIGTERM,
  chờ, SIGKILL (`stop_orphan_sync`). Không chạy binary nào nữa.
- Đổi tên cfharness (`filehub.rename`, chỉ người): `name` mới; tên cũ vào `aliases` (≤ `ALIASES_MAX`) để task/agent/run đã ghi
  `filehub:<tên cũ>` vẫn khớp.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..aiotasks import track
from ..errors import Conflict, HubError, NotFound
from ..events import HubEvent, make_event

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.filehub")

REF_PREFIX = "filehub:"
IGNORE_BASE = (".git/", "node_modules/", ".DS_Store", "Thumbs.db", "*.tmp", "*.swp", "~$*", ".~lock.*", ".tangthu*",
               # tệp nội bộ của claude CLI trong cwd của run (vd `.claude/.cc-writes`, settings.local.json) — không bao giờ lên cloud
               ".claude/", ".claude.json", ".cc-writes")
META_PREFIX = ".tangthu"  # metadata của đồng bộ cũ — bản sao cũ trên máy có thể còn: agent vẫn KHÔNG được ghi (classifier `filehub_metadata`)
KINDS = ("folder", "workspace")
ALIASES_MAX = 5  # tên cfharness cũ giữ làm bí danh sau `filehub.rename`
ORPHAN_SYNC_GRACE_S = 15.0  # tiến trình sync còn sót: SIGTERM → chờ → SIGKILL
# Thư mục còn cấu hình đồng bộ 2 chiều cũ (đã bỏ ở H5) — chữ CỐ ĐỊNH cho thẻ web, CLI, status, lỗi run
LEGACY_TEXT = ("Thư mục {name} đang dùng đồng bộ cũ (đã bỏ) — dán khoá cổng cfharness ở Cài đặt để dùng tiếp; bản sao trên máy để nguyên")


def legacy_text(name: Any) -> str:
    return LEGACY_TEXT.format(name=f"«{name}»")


def _real(p: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _under(p: str, base: str) -> bool:
    p, base = p.rstrip("/"), base.rstrip("/")
    return p == base or p.startswith(base + "/")


def all_folders(config: Any) -> list[dict[str, Any]]:
    """Mọi thư mục chung đã cấu hình (cổng lẫn đồng bộ cũ cần chuyển)."""
    try:
        v = config.get("filehub.folders") or []
    except KeyError:
        return []
    return [dict(f) for f in v if isinstance(f, dict) and f.get("name")] if isinstance(v, list) else []


def folder_mode(f: dict[str, Any] | None, config: Any) -> str:
    """`lane` (cổng cfharness) | `sync` (đồng bộ 2 chiều CŨ — đã bỏ, thư mục cần chuyển). `folders[].mode` thắng; thiếu thì
    `filehub.mode` cũ = lane → lane; còn lại: có `local_path` (bản sao của đồng bộ cũ) → sync, không → lane."""
    m = (f or {}).get("mode")
    if m in ("sync", "lane"):
        return str(m)
    try:
        g = config.get("filehub.mode")
    except (KeyError, AttributeError):
        g = None
    if g == "lane":
        return "lane"
    return "sync" if (f or {}).get("local_path") else "lane"


def lane_folders(config: Any) -> list[dict[str, Any]]:
    """Thư mục chung qua cổng (không bản sao lâu dài — cây làm việc theo run, filehub/lane/)."""
    return [f for f in all_folders(config) if f.get("root_id") and folder_mode(f, config) == "lane"]


def legacy_folders(config: Any) -> list[dict[str, Any]]:
    """Thư mục còn cấu hình đồng bộ 2 chiều cũ — CẦN CHUYỂN sang cổng (hub không dùng, không xoá gì)."""
    return [f for f in all_folders(config) if folder_mode(f, config) == "sync"]


def old_copies(config: Any) -> list[str]:
    """Bản sao trên máy của đồng bộ cũ (realpath, kể cả thư mục đã tự chuyển sang cổng): hub không bao giờ xoá/đụng; guard vẫn cấm agent
    ghi `.tangthu*` trong đó (metadata cũ); gói xuất hub không coi chúng là repo."""
    out = []
    for f in all_folders(config):
        lp = f.get("local_path")
        if isinstance(lp, str) and lp.strip() and os.path.isabs(os.path.expanduser(lp)):
            out.append(_real(lp))
    return list(dict.fromkeys(out))


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


def shared_folders(hub: Any) -> list[dict[str, Any]]:
    """Thư mục chung cho khối prompt "Shared folder" — chỉ thư mục qua cổng (thư mục đồng bộ cũ chưa chuyển: agent không dùng được)."""
    try:
        if not hub.config.get("filehub.enabled"):
            return []
    except (KeyError, AttributeError):
        return []
    try:
        browse = bool(hub.config.get("filehub.lane.read_all_runs"))
    except (KeyError, AttributeError):
        browse = False
    return [{"name": str(f["name"]), "path": "", "kind": kind_of(f), "server_name": "", "lane": True, "browse": browse}
            for f in lane_folders(hub.config)]


def stop_orphan_sync(home: Path, *, tolerance_s: float = 5.0, grace_s: float = ORPHAN_SYNC_GRACE_S) -> dict[str, Any]:
    """Tiến trình `tang-thu-sync-headless` còn sót từ daemon cũ (ghi ở `HUB_HOME/filehub/supervisor.json`): đúng `(pid, start_time)` →
    SIGTERM (binary dừng êm, không bỏ dở commit), chờ `grace_s`, rồi SIGKILL. pid không khớp (đã thoát / pid bị dùng lại) → không đụng.
    Xong thì xoá tệp ghi pid. Không bao giờ xoá dữ liệu."""
    from ..recovery import kill_group, process_matches

    p = Path(home) / "filehub" / "supervisor.json"
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"found": False}
    pid, start = (rec.get("pid"), rec.get("start_time")) if isinstance(rec, dict) else (None, None)
    out: dict[str, Any] = {"found": False}
    try:
        if pid and process_matches(int(pid), start, tolerance_s):
            killed = kill_group(int(pid), grace_s=grace_s)
            out = {"found": True, "pid": int(pid), "killed": killed}
            log.info("filehub: đã dừng tiến trình đồng bộ cũ còn sót pid=%s", pid)
        elif pid:
            out = {"found": False, "stale": True}
    except (TypeError, ValueError):
        out = {"found": False, "stale": True}
    try:
        p.unlink()
    except OSError:
        pass
    return out


class FilehubService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._bg: set[asyncio.Task[Any]] = set()
        self.orphan_sync: dict[str, Any] | None = None  # kết quả dừng tiến trình sync còn sót lúc khởi động (status/test)
        self._legacy_reported: tuple[str, ...] = ()
        hub.bus.add_listener(self._on_event)
        from .lane.service import LaneService  # cổng cfharness — filehub/lane/

        self.lane = LaneService(self)

    # ------------------------------------------------------------------ cấu hình
    def cfg(self, key: str) -> Any:
        try:
            return self.hub.config.get(f"filehub.{key}")
        except KeyError:
            return None

    @property
    def dir(self) -> Path:
        return self.hub.paths.home / "filehub"

    def enabled(self) -> bool:
        return bool(self.cfg("enabled"))

    def server(self, lane_server: str | None = None) -> str:
        """URL web của Không gian số (link "Mở trên Không gian số" — `fs_link`, `filehub.link`); không phải địa chỉ cổng. `filehub.server`
        người đặt thắng; khi trống hoặc do hub tự đặt từ `hello.server.public_url` (§C.3) → public_url của chính cổng `lane_server` (nhiều
        cổng: mỗi cổng link về web của nó)."""
        cur = str(self.cfg("server") or "").rstrip("/")
        lane = getattr(self, "lane", None)
        if lane is None or (cur and cur != lane.server_auto()):
            return cur
        return lane.public_url(lane_server) or cur

    def all_folders(self) -> list[dict[str, Any]]:
        return all_folders(self.hub.config)

    def folders(self) -> list[dict[str, Any]]:
        return self.all_folders()

    def lane_folders(self) -> list[dict[str, Any]]:
        return lane_folders(self.hub.config)

    def legacy_folders(self) -> list[dict[str, Any]]:
        return legacy_folders(self.hub.config)

    def lane_target(self, ref: Any) -> dict[str, Any] | None:
        """`filehub:<tên>[/<con>]` trỏ thư mục qua cổng → `{folder, server, root, sub, key}`; thư mục cần chuyển/không khớp → None."""
        if not isinstance(ref, str) or not ref.startswith(REF_PREFIX) or not self.enabled():
            return None
        return self.lane.target(ref[len(REF_PREFIX):].strip().strip("/"))

    def folder(self, name: str, *, aliases: bool = False) -> dict[str, Any]:
        """Thư mục theo tên cfharness; `aliases=True` → cả tên cũ sau `filehub.rename`."""
        for f in self.all_folders():
            if f["name"] == name:
                return f
        if aliases:
            for f in self.all_folders():
                if name in aliases_of(f):
                    return f
        raise self.not_found(name)

    def not_found(self, name: str) -> NotFound:
        names = [str(f["name"]) for f in self.all_folders()]
        if names:
            return NotFound(f"không có thư mục chung '{name}' — có: " + ", ".join(f"filehub:{n}" for n in names[:20]))
        return NotFound(f"không có thư mục chung '{name}' — chưa có thư mục chung nào: dán khoá cổng ở Cài đặt › Cổng cfharness "
                        "(hoặc `cfharness filehub lane setup`)")

    def ignore_lines(self) -> list[str]:
        """Mẫu bỏ qua chiều đẩy lên (kiểu .gitignore): rác/tạm/.git + tên bí mật của `files.secret_patterns` (trừ `secret_allow`)."""
        from ..fileview import settings as _file_settings

        try:
            st = _file_settings(self.hub.config.get("files"))
        except Exception:  # noqa: BLE001
            st = _file_settings(None)
        out = list(IGNORE_BASE) + [str(p) for p in st["secret_patterns"]] + [f"!{p}" for p in st["secret_allow"]]
        return list(dict.fromkeys(out + [str(x) for x in self.cfg("extra_ignore") or []]))

    # ------------------------------------------------------------------ workspace `filehub:<tên>[/<con>]`
    def _legacy_match(self, body: str) -> dict[str, Any] | None:
        for f in sorted(self.legacy_folders(), key=lambda x: -len(x["name"])):
            for n in [f["name"], *aliases_of(f)]:
                if body == n or body.startswith(n + "/"):
                    return f
        return None

    def resolve_ref(self, ref: str, *, must_exist: bool = True) -> tuple[str, dict[str, Any], str]:
        """Thư mục chung KHÔNG còn bản sao trên máy để giải (cổng: cây dựng lúc run chạy — bên gọi dùng `lane_target` trước). Luôn ném
        `HubError` có lời giải thích: thư mục qua cổng (`data.lane`), thư mục đồng bộ cũ cần chuyển (`data.needs_migration`), tên sai."""
        if not isinstance(ref, str) or not ref.startswith(REF_PREFIX):
            raise HubError(f"workspace phải dạng filehub:<tên>[/<thư mục con>]: {ref!r}")
        body = ref[len(REF_PREFIX):].strip().strip("/")
        if not self.enabled():
            raise HubError("thư mục chung (filehub) chưa bật — dán khoá cổng ở Cài đặt › Cổng cfharness (`cfharness filehub lane setup`)")
        if self.lane.target(body) is not None:
            raise HubError(f"'{ref}' là thư mục chung qua cổng cfharness — không có bản sao trên máy; cây làm việc dựng khi run chạy",
                           data={"lane": True})
        f = self._legacy_match(body)
        if f is not None:
            raise HubError(legacy_text(f["name"]), data={"needs_migration": True, "folder": f["name"]})
        raise self.not_found(body.partition("/")[0])

    # ------------------------------------------------------------------ link "Mở trên Không gian số"
    async def link(self, p: dict[str, Any]) -> dict[str, Any]:
        """RPC `filehub.link {path, run_id?}` → `{folder, rel, url}`. Có `run_id` (run lane): `path` tương đối trong phạm vi run (diff của
        run). Không có: `path` = `<tên thư mục>/<đường dẫn>` (như `fs_list` chỉ đọc). URL dựng từ `filehub.server` (người đặt) + id do cổng
        trả (`stat`) — không lấy từ client."""
        from .lane.client import LaneError, join_rel, norm_rel

        path = str(p.get("path") or "").strip()
        if not path:
            raise HubError("thiếu tham số 'path'", code=-32602)
        rid = p.get("run_id")
        if rid:
            run = self.hub.store.find_run(str(rid))
            if run is None:
                raise NotFound(f"không tìm thấy run '{rid}'")
            lane = ((run.get("diff") or {}).get("lane") or (run.get("run_opts") or {}).get("filehub_lane") or {})
            if not isinstance(lane, dict) or not lane.get("server") or not lane.get("root"):
                raise NotFound("run này không làm việc trên thư mục chung qua cổng")
            server, root, sub = str(lane["server"]), str(lane["root"]), str(lane.get("sub") or "")
            folder = str((run.get("diff") or {}).get("filehub") or (run.get("run_opts") or {}).get("filehub") or "")
            kind = str(lane.get("kind") or "folder")
        else:
            body = path[len(REF_PREFIX):] if path.startswith(REF_PREFIX) else path
            t = self.lane.target(body.strip().strip("/"))
            if t is None:
                f = self._legacy_match(body.strip().strip("/"))
                if f is not None:
                    raise HubError(legacy_text(f["name"]))
                raise NotFound("đường dẫn phải dạng <tên thư mục chung>/<đường dẫn> (xem filehub_list)")
            server, root, sub, folder, kind = t["server"], t["root"], "", t["folder"], t["kind"]
            path = t["sub"]
        try:
            rel = norm_rel(path)
        except LaneError as e:
            raise HubError(f"đường dẫn không hợp lệ: {e.message}") from None
        srv = self.server(server)
        if not rel:
            return {"folder": folder, "rel": "", "url": f"{srv}/files" if srv and kind == "workspace" and not sub else None}
        try:
            it = await self.lane.client(server).stat(root, join_rel(sub, rel))
        except LaneError as e:
            raise NotFound(f"không lấy được link: {e.message}") from None
        if not srv or not it:
            return {"folder": folder, "rel": rel, "url": None}
        if it.get("type") == "file":
            return {"folder": folder, "rel": rel, "url": f"{srv}/files/{it.get('parent_id')}?file={it.get('id')}"}
        return {"folder": folder, "rel": rel, "url": f"{srv}/files/{it.get('id')}"}

    # ------------------------------------------------------------------ trạng thái
    def legacy_view(self) -> list[dict[str, Any]]:
        out = []
        for f in self.legacy_folders():
            lp = f.get("local_path")
            out.append({"name": f["name"], "root_id": f.get("root_id"), "kind": kind_of(f), "mode": "sync", "status": "needs_migration",
                        "local_path": lp or None,
                        "old_copy": lp if isinstance(lp, str) and lp and os.path.isdir(os.path.expanduser(lp)) else None,
                        "message": legacy_text(f["name"])})
        return out

    def status(self) -> dict[str, Any]:
        lane = self.lane.status()
        lane_rows = [{**f, "mode": "lane"} for f in lane.get("folders") or []]
        legacy = self.legacy_view()
        if not self.enabled() or (not lane_rows and not legacy):
            state = "not_configured"
        elif legacy:
            state = "needs_migration"
        else:
            state = "ok"
        return {
            "enabled": self.enabled(), "server": self.server() or None, "state": state,
            "folders": lane_rows, "lane_folders": lane_rows, "legacy": legacy,
            "lane_configured": bool(self.lane.servers()), "servers": lane.get("servers") or [],
            "relogin_after_import": bool(self.hub.store.state_get("import_filehub")),
            "orphan_sync": self.orphan_sync,
        }

    def list(self) -> list[dict[str, Any]]:
        """RPC `filehub.list` (đọc — điều phối viên dùng để giao việc): thư mục qua cổng + thư mục đồng bộ cũ (KHÔNG giao được — cần chuyển)."""
        out = []
        for f in self.lane.status().get("folders") or []:
            row = {"name": f["name"], "workspace": f["workspace"], "mode": "lane", "kind": f["kind"], "root_id": f.get("root_id"),
                   "server": f.get("server"), "status": f.get("status"), "server_name": f["name"], "local_path": None}
            if f["kind"] == "workspace":
                row["layout"] = "root = whole workspace; new files must be inside a top-level folder"
            out.append(row)
        for f in self.legacy_view():
            out.append({"name": f["name"], "workspace": f"{REF_PREFIX}{f['name']}", "mode": "sync", "kind": f["kind"], "root_id": f["root_id"],
                        "status": "needs_migration", "server_name": f["name"], "local_path": None,
                        "note": "old two-way sync was removed — do NOT assign; the owner must paste a cfharness gateway key in Settings"})
        return out

    # ------------------------------------------------------------------ vòng đời
    def start(self) -> None:
        """`hub.started`: dừng tiến trình đồng bộ cũ còn sót (luồng phụ — có thể chờ tới `ORPHAN_SYNC_GRACE_S`), báo thư mục cần chuyển."""
        if (self.dir / "supervisor.json").exists():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.orphan_sync = stop_orphan_sync(self.hub.paths.home, tolerance_s=self._tolerance())
            else:
                self._spawn(self._stop_orphan_async(loop))
        self.report_legacy()

    def _tolerance(self) -> float:
        try:
            return float(self.hub.config.get("recovery.pid_start_tolerance_s") or 5)
        except KeyError:
            return 5.0

    async def _stop_orphan_async(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            self.orphan_sync = await loop.run_in_executor(None, lambda: stop_orphan_sync(self.hub.paths.home, tolerance_s=self._tolerance()))
        except Exception:  # pragma: no cover
            log.exception("filehub: dừng tiến trình đồng bộ cũ lỗi")

    def report_legacy(self) -> None:
        """Thư mục đồng bộ cũ còn trong cấu hình → event `filehub.state{state: needs_migration, folders}` (một lần cho mỗi tập tên) + log."""
        names = tuple(sorted(f["name"] for f in self.legacy_folders()))
        if names == self._legacy_reported:
            return
        self._legacy_reported = names
        if names:
            log.warning("filehub: thư mục còn dùng đồng bộ cũ (đã bỏ) — cần dán khoá cổng: %s", ", ".join(names))
            self.hub.publish(make_event("filehub.state", None, {"state": "needs_migration", "folders": list(names)[:20]}, source="hub"))

    async def stop(self) -> None:
        await self.lane.stop()

    def _spawn(self, coro: Any) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        track(t, self._bg)  # L1: lỗi task → log

    def _on_event(self, ev: HubEvent) -> None:
        try:
            if ev.type == "hub.started":
                self.start()
                self.lane.start()  # cổng cfharness: hạn khoá, cây mồ côi, cây của run đã xong (daemon khởi động lại)
        except Exception:  # pragma: no cover — listener không được làm hỏng bus
            log.exception("filehub listener lỗi với %s", ev.type)

    # ------------------------------------------------------------------ thao tác của người (RPC ghi, HUMAN_ONLY)
    def _lane_holder(self, f: dict[str, Any]) -> dict[str, Any] | None:
        if folder_mode(f, self.hub.config) != "lane" or not f.get("root_id"):
            return None
        from .lane.service import lock_key

        srv = self.lane.folder_server(f)
        return self.hub.runs.locks.holder(lock_key(srv, str(f["root_id"]))) if srv else None

    def remove(self, name: str) -> dict[str, Any]:
        """Gỡ thư mục chung khỏi cấu hình hub (cổng lẫn đồng bộ cũ). Không xoá tệp nào — trên máy lẫn trên Không gian số."""
        f = self.folder(name)
        holder = self._lane_holder(f)
        if holder:
            raise Conflict(f"thư mục chung '{name}' đang có run {holder.get('run_id')} làm việc — dừng run trước")
        self.hub.config.set("filehub.folders", [x for x in self.all_folders() if x["name"] != name])
        self.report_legacy()
        return {"removed": name, "local_path": f.get("local_path"), "note": "tệp trên máy và trên Không gian số được giữ nguyên"}

    def rename(self, name: str, new_name: str) -> dict[str, Any]:
        """Đổi tên cfharness (`filehub:<tên>`) — chỉ trong hub: không đổi tên trên Không gian số. Tên cũ giữ làm bí danh."""
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
        holder = self._lane_holder(f)
        if holder:
            raise Conflict(f"thư mục chung '{old}' đang có run {holder.get('run_id')} làm việc — dừng run trước khi đổi tên")
        aliases = ([a for a in aliases_of(f) if a != new_name] + [old])[-ALIASES_MAX:]
        rows = [{**x, "name": new_name, "aliases": aliases} if x["name"] == old else x for x in self.all_folders()]
        self.hub.config.set("filehub.folders", rows)
        return {"name": new_name, "previous": old, "aliases": aliases, "workspace": f"{REF_PREFIX}{new_name}",
                "note": "chỉ đổi tên trong cfharness — tên trên Không gian số giữ nguyên"}

    def resume(self, root_id: str | None = None, *, discard: bool = False) -> Any:
        """Lô xoá hàng loạt đang GIỮ của các run trên gốc `root_id` (thư mục qua cổng) → NGƯỜI quyết đẩy tiếp / bỏ (`discard`)."""
        if not root_id:
            raise HubError("thiếu --root <id gốc> — `filehub resume` chỉ còn dùng cho lô xoá đang giữ của thư mục qua cổng", code=-32602)
        if any(str(f.get("root_id")) == str(root_id) for f in self.lane_folders()):
            return self.lane.release_deletes(str(root_id), discard=discard)
        f = next((x for x in self.legacy_folders() if str(x.get("root_id")) == str(root_id)), None)
        if f is not None:
            raise HubError(legacy_text(f["name"]))
        raise NotFound(f"không có thư mục chung với root {root_id}")


__all__ = ["FilehubService", "REF_PREFIX", "LEGACY_TEXT", "legacy_text", "valid_name", "safe_name", "kind_of", "aliases_of", "shared_folders",
           "all_folders", "lane_folders", "legacy_folders", "old_copies", "folder_mode", "stop_orphan_sync"]
