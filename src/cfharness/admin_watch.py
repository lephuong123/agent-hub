"""Cảnh báo + lịch sử cho người quản trị máy (plan-admin-users P1/P2; chi tiết `docs/dev-notes/admin-users.md`).

`AdminWatcher` (`hub.admin_watch`) chạy trong MỌI hub nhưng chỉ làm việc khi tiến trình thuộc nhóm `cfharness-admin`
(`admin_control.is_admin`), `admin_watch.enabled` và thư mục trạng thái chung tồn tại. Mỗi `admin_watch.interval_s`:
- đọc tổng quan (`admin_overview.read_dir`) + danh sách user (`admin_users.list_users`) → tập mã cảnh báo theo (user, hub);
- mã mới → event `admin.alert{user, hub_id, code, state:"on"}`; mã hết → `{…, state:"resolved"}` (báo hồi phục). Chống trùng bằng
  `hub_state.admin_watch.active` (sống qua restart). Event đi qua notifier Telegram/Slack SẴN CÓ của hub admin (`notify.*.events`).
  Payload chỉ id/enum — không chữ tự do.
- (P2) ghi mẫu số liệu vào `admin_samples` mỗi `admin_watch.sample_interval_s`, xoá mẫu cũ hơn `admin_watch.history_days`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .events import HubEvent
    from .hub import Hub

log = logging.getLogger("cfharness.admin_watch")

ALERT_CODES = ("stale", "stopped", "owner_mismatch", "malformed", "missing", "budget_80", "budget_100", "tamper", "auth_block",
               "approval_waiting_long", "version_behind", "policy_mismatch", "admin_estop_on", "revoked_running")
ALERT_VI = {
    "stale": "hub quá hạn (daemon có thể đã chết)", "stopped": "hub đã dừng", "owner_mismatch": "file trạng thái SAI CHỦ",
    "malformed": "file trạng thái hỏng", "missing": "đã cấp quyền nhưng chưa có file trạng thái", "budget_80": "đã dùng ≥ 80% ngân sách ngày",
    "budget_100": "đã hết ngân sách ngày", "tamper": "phát hiện sửa ngoài hub (toàn vẹn)", "auth_block": "claude chưa đăng nhập",
    "approval_waiting_long": "có thao tác chờ duyệt quá lâu", "version_behind": "phiên bản cũ hơn hub admin",
    "policy_mismatch": "chính sách quản trị chưa được áp / không hợp lệ", "admin_estop_on": "ESTOP quản trị máy đang bật",
    "revoked_running": "đã thu quyền nhưng hub vẫn chạy",
}
STATE_KEY = "admin_watch.active"


def _vt(v: Any) -> tuple[int, ...]:
    from .admin_policy import _vtuple

    return _vtuple(str(v or ""))


def hub_codes(h: dict[str, Any], *, my_version: str, approval_wait_s: float, budget_warn_pct: float, expected_rev: Any = None) -> set[str]:
    """Mã cảnh báo của MỘT hub (thuần)."""
    codes: set[str] = set()
    st = h.get("state")
    if st in ("stale", "stopped", "owner_mismatch", "malformed"):
        codes.add(str(st))
    d = h.get("data") or {}
    if not d or st in ("owner_mismatch", "malformed"):
        return codes
    pct = (d.get("budget") or {}).get("day_pct")
    if isinstance(pct, (int, float)):
        if pct >= 100:
            codes.add("budget_100")
        elif pct >= budget_warn_pct:
            codes.add("budget_80")
    g = d.get("guard") or {}
    if int(g.get("tamper_today") or 0) > 0:
        codes.add("tamper")
    if (d.get("flags") or {}).get("auth_block") or (d.get("claude") or {}).get("auth_ok") is False:
        codes.add("auth_block")
    if float((d.get("approvals") or {}).get("oldest_wait_s") or 0) >= approval_wait_s > 0:
        codes.add("approval_waiting_long")
    if d.get("version") and _vt(d["version"]) < _vt(my_version):
        codes.add("version_behind")
    pol = d.get("policy")
    if st == "ok" and isinstance(pol, dict) and (pol.get("valid") is False or (expected_rev is not None and pol.get("rev_applied") != expected_rev)):
        codes.add("policy_mismatch")
    return codes


class AdminWatcher:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._task: asyncio.Task[Any] | None = None
        self.last_tick: str | None = None
        self.last_sample: float = 0.0
        self.last_prune: float = 0.0
        hub.bus.add_listener(self._on_event)

    def cfg(self, key: str, default: Any) -> Any:
        try:
            v = (self.hub.config.get("admin_watch") or {}).get(key)
        except KeyError:
            v = None
        return default if v is None else v

    def active_for(self) -> bool:
        from .admin_control import is_admin

        return bool(self.cfg("enabled", True)) and is_admin()

    @property
    def active(self) -> dict[str, dict[str, Any]]:
        v = self.hub.store.state_get(STATE_KEY) or {}
        return v if isinstance(v, dict) else {}

    # ------------------------------------------------------------------ đánh giá
    def compute(self, ov: dict[str, Any], users: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """{khoá: {user, hub_id, code}} của mọi cảnh báo đang đúng."""
        from . import __version__
        from .admin_control import list_flags
        from .admin_policy import effective

        out: dict[str, dict[str, Any]] = {}

        def add(user: str, hub_id: str | None, code: str) -> None:
            out[f"{user}|{hub_id or '-'}|{code}"] = {"user": user, "hub_id": hub_id, "code": code}

        revs: dict[str, Any] = {}

        def exp_rev(u: str) -> Any:
            if u not in revs:
                e = effective(u)
                revs[u] = e.get("rev") if e.get("exists") else None
            return revs[u]

        wait_s = float(self.cfg("approval_wait_s", 1800))
        warn = float(self.cfg("budget_warn_pct", 80))
        for h in ov.get("hubs") or []:
            for c in hub_codes(h, my_version=__version__, approval_wait_s=wait_s, budget_warn_pct=warn, expected_rev=exp_rev(str(h.get("user")))):
                add(str(h["user"]), h.get("hub_id"), c)
        for u in users:
            if u.get("granted") and u.get("member") and not u.get("hubs"):
                add(u["user"], None, "missing")
            if u.get("state") == "revoked_running":
                add(u["user"], None, "revoked_running")
        flags = list_flags()
        on_all = any(f["scope"] == "all" for f in flags)
        for u in users:
            if on_all or any(f.get("user") == u["user"] for f in flags):
                add(u["user"], None, "admin_estop_on")
        return out

    def evaluate(self, ov: dict[str, Any] | None = None, users: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Một lượt: phát `admin.alert` cho mã mới / mã đã hết; lưu trạng thái. Trả {new, resolved, active}."""
        from .admin_overview import overview
        from .admin_users import list_users
        from .events import make_event

        ov = overview(self.hub) if ov is None else ov
        users = list_users(ov) if users is None else users
        cur = self.compute(ov, users) if ov.get("exists") else {}
        prev = self.active
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        new = [k for k in cur if k not in prev]
        gone = [k for k in prev if k not in cur]
        for k in new:
            a = cur[k]
            self.hub.publish(make_event("admin.alert", None, {**a, "state": "on"}))
        for k in gone:
            a = prev[k]
            self.hub.publish(make_event("admin.alert", None, {"user": a.get("user"), "hub_id": a.get("hub_id"), "code": a.get("code"), "state": "resolved"}))
        if new or gone:
            nxt = {k: {**cur[k], "since": prev.get(k, {}).get("since") or now} for k in cur}
            self.hub.store.state_set(STATE_KEY, nxt)
        self.last_tick = now
        self._sample(ov)
        return {"new": [cur[k] for k in new], "resolved": [prev[k] for k in gone], "active": len(cur)}

    # ------------------------------------------------------------------ lịch sử (P2)
    def _sample(self, ov: dict[str, Any], *, force: bool = False) -> int:
        t = time.time()
        every = float(self.cfg("sample_interval_s", 300))
        if not force and t - self.last_sample < every:
            return 0
        self.last_sample = t
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        rows = []
        for h in ov.get("hubs") or []:
            d = h.get("data") or {}
            c, r = d.get("cost_usd") or {}, d.get("runs") or {}
            rows.append((ts, t, str(h.get("user")), h.get("hub_id"), str(h.get("state")), c.get("today"), c.get("month"),
                         r.get("running"), r.get("queued"), r.get("today_failed"), (d.get("approvals") or {}).get("open"),
                         (d.get("budget") or {}).get("day_pct")))
        conn = self.hub.store.conn
        if rows:
            conn.executemany("INSERT INTO admin_samples (ts, ts_epoch, user, hub_id, state, cost_today, cost_month, running, queued, failed_today, "
                             "approvals_open, day_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        if t - self.last_prune > 3600:
            self.last_prune = t
            self.prune()
        return len(rows)

    def prune(self, *, now: float | None = None) -> int:
        days = max(1, int(self.cfg("history_days", 90)))
        cut = (now or time.time()) - days * 86400
        cur = self.hub.store.conn.execute("DELETE FROM admin_samples WHERE ts_epoch < ?", (cut,))
        return int(cur.rowcount or 0)

    def history(self, user: str, *, days: int = 30) -> dict[str, Any]:
        """Chuỗi số liệu cho biểu đồ: chi phí theo ngày (max cost_today mỗi ngày, cộng các hub) + run đang chạy theo giờ (24 h)."""
        conn = self.hub.store.conn
        days = max(1, min(int(days), int(self.cfg("history_days", 90))))
        since = time.time() - days * 86400
        rows = conn.execute("SELECT ts_epoch, hub_id, cost_today, running, state FROM admin_samples WHERE user=? AND ts_epoch >= ? ORDER BY ts_epoch",
                            (user, since)).fetchall()
        per_day: dict[str, dict[str, float]] = {}
        per_hour: dict[str, dict[str, int]] = {}
        h24 = time.time() - 86400
        for ts, hid, cost, running, _st in rows:
            dt = datetime.fromtimestamp(ts)
            day = dt.strftime("%Y-%m-%d")
            hb = per_day.setdefault(day, {})
            hb[hid or "-"] = max(hb.get(hid or "-", 0.0), float(cost or 0))
            if ts >= h24:
                hr = dt.strftime("%Y-%m-%d %H:00")
                hh = per_hour.setdefault(hr, {})
                hh[hid or "-"] = max(hh.get(hid or "-", 0), int(running or 0))
        start = datetime.now() - timedelta(days=days - 1)
        cost = [{"label": (start + timedelta(days=i)).strftime("%Y-%m-%d"), "value": round(sum(per_day.get((start + timedelta(days=i)).strftime("%Y-%m-%d"), {}).values()), 4)}
                for i in range(days)]
        running = [{"label": k[11:16], "value": sum(v.values())} for k, v in sorted(per_hour.items())]
        return {"user": user, "days": days, "cost_by_day": cost, "running_by_hour": running, "samples": len(rows)}

    # ------------------------------------------------------------------ vòng nền
    def _on_event(self, ev: "HubEvent") -> None:
        try:
            if ev.type == "hub.started":
                self.start()
            elif ev.type == "hub.stopping":
                self.stop()
        except Exception:  # pragma: no cover
            log.exception("admin_watch listener lỗi với %s", ev.type)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:
            self._task = None

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                if self.active_for():
                    from .admin_overview import overview
                    from .admin_users import list_users

                    ov = await asyncio.to_thread(overview, self.hub)
                    users = await asyncio.to_thread(list_users, ov)
                    self.evaluate(ov, users)  # ghi DB/bus trên loop (một writer)
                await asyncio.sleep(max(15.0, float(self.cfg("interval_s", 60))))
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("admin_watch lỗi")
                await asyncio.sleep(60)


def attach(hub: "Hub") -> None:
    hub.admin_watch = AdminWatcher(hub)  # type: ignore[attr-defined]


__all__ = ["ALERT_CODES", "ALERT_VI", "AdminWatcher", "attach", "hub_codes"]
