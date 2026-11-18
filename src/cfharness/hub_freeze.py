"""Đóng băng hub NGUỒN sau khi `cfharness hub export --for move` (chế độ "chuyển nhà" — PR5 của docs/dev-notes/plan-hub-clone.md §2, §4.7).

Lý do: hai daemon không thể cùng giữ một bot Telegram (long poll → 409), một kết nối Slack Socket Mode (sự kiện chia đôi), một OAuth
refresh token xoay vòng, cùng chạy lịch/tính ngân sách. Khi đã xuất gói để chuyển nhà, máy nguồn phải im.

Trạng thái = `hub_state.migrated_to {at, export_id, sha256, profile, sealed, by, channel}` — nằm trong hub.db nên BỀN qua restart daemon.
Khi có mặt (`frozen(hub)` khác None):
- `RunManager.submit` từ chối MỌI run (ad-hoc, điều phối viên, lịch, task, review, resume…) bằng `Blocked`; `RunManager.tick` không khởi
  chạy run còn trong hàng đợi; khôi phục lúc khởi động không tự resume run dở (run đó chạy tiếp ở máy mới).
- Vòng nền ghi tri thức/DB (dispatcher, lịch, nhịp điều phối viên, học, learning_ops, curation, vector graph) bỏ lượt — dùng chung cờ
  `runs.export_quiesced(hub)`. RIÊNG sao lưu (`backup.py`) VẪN chạy: hub đóng băng vẫn là dữ liệu của người (có thể `unfreeze` dùng
  lại), bản sao lưu không tạo run, không chạm kênh ngoài, không đổi dữ liệu — tắt nó chỉ làm mất lưới an toàn.
- Telegram/Slack: `token()` của bot trả None (không long poll / không Socket Mode / không gửi) và `QueuedNotifier` bỏ mọi thông báo
  (kể cả webhook Slack một chiều) — tránh 409 và chia sự kiện với máy mới. Lệnh `notify … test|setup` của người (ignore_enabled) vẫn chạy.
- Thư mục chung Không gian số (`hub.filehub`, cổng cfharness): không có run thì không có cây làm việc nào; vòng sức khoẻ/bảo trì của cổng
  vẫn chạy (chỉ `hello`, đẩy nốt cây mồ côi đã có trước khi đóng băng bỏ lượt vì `export_quiesced`). Event `filehub.*` vẫn ghi DB nhưng không
  gửi ra kênh (QueuedNotifier bỏ).
- `hub.status`, `tui.snapshot`, web (mọi trang) hiện banner "đã chuyển sang máy khác lúc …".
Gỡ: CHỈ người — `cfharness hub unfreeze` (xác nhận y/N) → RPC `hub.unfreeze` (HUMAN_ONLY, tự audit; agent gõ lệnh `cfharness hub …` bị
classifier chặn thẳng — `HUB_HUMAN_ONLY_CLI["hub"] = None`). Event `hub.frozen` / `hub.unfrozen`.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import Blocked, Conflict, HubError
from .events import make_event

if TYPE_CHECKING:
    from .daemon import Daemon
    from .hub import Hub
    from .paths import HubPaths

log = logging.getLogger("cfharness.hub_freeze")

KEY = "migrated_to"
MUTATING_FREEZE = ("hub.freeze", "hub.unfreeze")
INFO_KEYS = ("at", "export_id", "sha256", "profile", "sealed", "by", "channel")


def frozen(hub: Any) -> dict[str, Any] | None:
    """`hub_state.migrated_to` (dict) khi hub đã chuyển nhà sang máy khác — None khi bình thường. Không cache: một truy vấn khoá chính."""
    store = getattr(hub, "store", None)
    if store is None:
        return None
    try:
        v = store.state_get(KEY)
    except Exception:  # pragma: no cover — DB đóng giữa chừng (tắt daemon)
        return None
    return v if isinstance(v, dict) and v else None


def banner(info: dict[str, Any] | None) -> str:
    """Một dòng tiếng Việt (CLI/TUI/web) — chỉ dữ liệu của hub (id gói, thời điểm), không chữ tự do."""
    if not info:
        return ""
    return (f"Hub này ĐÃ CHUYỂN sang máy khác lúc {info.get('at') or '?'} (gói {info.get('export_id') or '?'}) — đang đóng băng: "
            "không chạy run/lịch/kênh chat. Dùng lại máy này: `cfharness hub unfreeze`")


def blocked_error(info: dict[str, Any]) -> Blocked:
    return Blocked(banner(info), data={"migrated_to": {k: info.get(k) for k in INFO_KEYS}})


def _clean(info: dict[str, Any]) -> dict[str, Any]:
    from .store import now_iso

    out = {k: info.get(k) for k in INFO_KEYS if info.get(k) is not None}
    out.setdefault("at", now_iso())
    return out


def _kick_channels(hub: Any) -> None:
    """Huỷ long poll / kết nối Socket Mode đang treo để vòng nhận thấy trạng thái mới ngay (đóng băng → im; gỡ → chạy lại)."""
    for name in ("telegram", "slack"):
        bot = getattr(hub, name, None)
        if bot is not None and hasattr(bot, "kick"):
            try:
                bot.kick()
            except Exception:  # pragma: no cover
                log.exception("không khởi động lại được vòng %s", name)


def freeze(hub: "Hub", info: dict[str, Any]) -> dict[str, Any]:
    """Ghi `migrated_to` (trong daemon) + phát `hub.frozen` + làm im kênh chat. Gọi lại với CÙNG export_id = cập nhật (vd sha256 sau khi
    CLI gắn `secrets.enc`); export_id khác khi đang đóng băng → `Conflict` (gỡ trước)."""
    cur = frozen(hub)
    rec = _clean(info)
    if not rec.get("export_id"):
        raise HubError("thiếu export_id", code=-32602)
    if cur and cur.get("export_id") != rec["export_id"]:
        raise Conflict(f"hub đã đóng băng theo gói {cur.get('export_id')} — `cfharness hub unfreeze` trước", data={"migrated_to": cur})
    if cur:
        rec = {**cur, **{k: v for k, v in rec.items() if k != "at"}}
    hub.store.state_set(KEY, rec)
    if not cur:
        hub.publish(make_event("hub.frozen", None, {k: rec.get(k) for k in ("at", "export_id", "profile")}))
        _kick_channels(hub)
        log.warning("hub đã đóng băng (chuyển nhà theo gói %s)", rec["export_id"])
    return rec


def unfreeze(hub: "Hub", *, export_id: str | None = None) -> dict[str, Any]:
    cur = frozen(hub)
    if not cur:
        return {"unfrozen": False, "migrated_to": None}
    if export_id and export_id != cur.get("export_id"):
        raise Conflict(f"hub đóng băng theo gói {cur.get('export_id')}, không phải {export_id}", data={"migrated_to": cur})
    hub.store.conn.execute("DELETE FROM hub_state WHERE key=?", (KEY,))
    hub.publish(make_event("hub.unfrozen", None, {"export_id": cur.get("export_id"), "frozen_at": cur.get("at")}))
    _kick_channels(hub)
    try:
        hub.runs.tick()
    except Exception:  # pragma: no cover
        log.exception("tick sau khi gỡ đóng băng lỗi")
    try:
        hub.dispatcher.kick()
    except Exception:  # pragma: no cover
        pass
    log.warning("đã gỡ đóng băng hub (gói %s)", cur.get("export_id"))
    return {"unfrozen": True, "migrated_to": cur}


def freeze_offline(paths: "HubPaths", info: dict[str, Any]) -> dict[str, Any]:
    """Ghi `migrated_to` khi daemon DỪNG (xuất ngoại tuyến). Người gọi giữ `daemon.lock`. Không qua daemon nên không có event; daemon
    lên sau đó đọc trạng thái này ngay từ đầu (bền qua restart)."""
    import json

    from .store import now_iso

    rec = _clean(info)
    c = sqlite3.connect(str(Path(paths.db)), isolation_level=None)
    try:
        cur = c.execute("SELECT value FROM hub_state WHERE key=?", (KEY,)).fetchone()
        old = json.loads(cur[0]) if cur else None
        if isinstance(old, dict) and old.get("export_id") and old.get("export_id") != rec.get("export_id"):
            raise Conflict(f"hub đã đóng băng theo gói {old.get('export_id')} — `cfharness hub unfreeze` trước")
        if isinstance(old, dict):
            rec = {**old, **{k: v for k, v in rec.items() if k != "at"}}
        c.execute("INSERT INTO hub_state(key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                  "updated_at=excluded.updated_at", (KEY, json.dumps(rec, ensure_ascii=False), now_iso()))
        c.execute("INSERT INTO audit_log (ts, kind, actor, channel, data) VALUES (?,?,?,?,?)",
                  (now_iso(), "hub.freeze", "user", "cli", json.dumps({"export_id": rec.get("export_id"), "offline": True}, ensure_ascii=False)))
    finally:
        c.close()
    return rec


def register_rpc(daemon: "Daemon") -> None:
    """`hub.frozen` (đọc), `hub.freeze {export_id, sha256?, sealed?}` + `hub.unfreeze {export_id?}` (HUMAN_ONLY, tự audit)."""
    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub

    def _freeze(p: dict[str, Any]) -> dict[str, Any]:
        cur = frozen(hub)
        if not cur or cur.get("export_id") != p.get("export_id"):
            # chỉ CẬP NHẬT bản ghi do chính `hub.export {for: move}` tạo — đóng băng mới luôn đi qua lần xuất (gói đã kiểm)
            raise Conflict("hub.freeze chỉ cập nhật đóng băng của lần xuất `--for move` vừa rồi (export_id phải khớp)")
        return freeze(hub, {"export_id": p.get("export_id"), "sha256": p.get("sha256"), "sealed": bool(p.get("sealed"))})

    def _unfreeze(p: dict[str, Any]) -> dict[str, Any]:
        return unfreeze(hub, export_id=p.get("export_id"))

    daemon.methods.update({
        "hub.frozen": lambda p: {"migrated_to": frozen(hub), "banner": banner(frozen(hub))},
        "hub.freeze": wrap_with_audit(hub, "hub.freeze", _freeze),
        "hub.unfreeze": wrap_with_audit(hub, "hub.unfreeze", _unfreeze),
    })


__all__ = ["KEY", "frozen", "banner", "blocked_error", "freeze", "unfreeze", "freeze_offline", "register_rpc", "MUTATING_FREEZE"]
