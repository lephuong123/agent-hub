"""Cảnh báo theo mẫu nhật ký kiểm toán (L5, docs/dev-notes/plan-logging.md §4 A8).

`AuditWatch` là một `row_tap` của `hub.audit` (`AuditLog.record` gọi SAU khi ghi xong mỗi dòng — lỗi của tap bị log, không bao giờ làm hỏng
lượt ghi). Mỗi luật là cửa sổ trượt TRONG RAM (deque theo khoá) — không quét DB mỗi lần ghi. Khi ngưỡng đạt → event `audit.alert`
`{rule, principal, count, window_s, state: "on"}` (+ `subject` thuộc từ vựng cố định cho `security_change`/`tamper`) + dòng audit cùng tên;
chống trùng: một `on` cho mỗi (luật, principal) tới khi hết; hết = qua trọn `window_s` không có lượt khớp nào → `state: "resolved"` (tổng số
lượt). Trạng thái cảnh báo đang bật lưu `hub_state['audit_watch.active']` (khởi động lại vẫn báo hết được). Payload CHỈ metadata.

Luật (ngưỡng ở `audit_watch.*` — nhánh ∈ `integrity.SECURITY_KEYS`, chỉ người đổi):

| luật | mặc định |
|---|---|
| `denied_burst` | ≥ 5 dòng `outcome=denied` / 10 phút / principal (trừ `web.login_*` — luật riêng) |
| `login_fail_burst` | ≥ 5 `web.login_failed` / 15 phút theo IP (`ip:<ip>`); toàn cục (`*`) khi ≥ 5 lượt từ ≥ 2 IP khác nhau |
| `security_change` | ngay lập tức: `config.set` khoá `gates.mode`, `approval.profile`, `guard.enabled`, `guard.sandbox.*`, `workspace.confine*`, `audit.*`, `audit_watch.*` (kể cả đặt nhánh cha); bật trust full (`guard.trust` after=full); cấp grant luật (`guard.rule_grant` action=grant); ngoại lệ sandbox (`guard.sandbox_override`) |
| `secret_access` | `secret.set`/`secret.rm`/`read:secret.list` ngoài `audit_watch.secret_access.hours` (vd `"08:00-19:00"`, giờ máy; null = TẮT) |
| `new_principal` | principal lần đầu thấy (tập đã thấy ở `hub_state['audit_watch.seen']`, trần `max_seen`); không báo `hub:`/`agent:`/`manager:`/`anon:` và phiên web không thiết bị (`web:sess_`, `web:anon` — đổi mỗi lần đăng nhập). Lần đầu sau nâng cấp: nạp tập từ audit sẵn có, không báo |
| `mass_read` | ≥ 30 lượt `read:*` (cộng `data.count`) / 10 phút / principal |
| `tamper` | chuyển tiếp `audit.tamper_detected` / `guard.tamper_detected` |

Dòng do chính luồng cảnh báo sinh ra (`audit.alert`) bị bỏ qua — không vòng lặp. Env `CFHARNESS_AUDIT_WATCH=off` tắt hẳn (bộ test chung đặt
trong conftest; `tests/test_audit_watch.py` bật lại).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("cfharness.audit_watch")

RULES = ("denied_burst", "login_fail_burst", "security_change", "secret_access", "new_principal", "mass_read", "tamper")
RULE_VI = {
    "denied_burst": "nhiều thao tác bị từ chối",
    "login_fail_burst": "nhiều lần đăng nhập web thất bại",
    "security_change": "thay đổi cấu hình an toàn",
    "secret_access": "truy cập secret ngoài giờ",
    "new_principal": "danh tính mới lần đầu xuất hiện",
    "mass_read": "đọc dữ liệu nhạy cảm hàng loạt",
    "tamper": "nhật ký/chính sách bị sửa ngoài hub",
}
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "denied_burst": {"enabled": True, "threshold": 5, "window_s": 600},
    "login_fail_burst": {"enabled": True, "threshold": 5, "window_s": 900},
    "security_change": {"enabled": True, "window_s": 600},
    "secret_access": {"enabled": True, "hours": None, "window_s": 600},
    "new_principal": {"enabled": True, "window_s": 600, "max_seen": 1000,
                      "ignore_prefixes": ["hub:", "agent:", "manager:", "anon:", "web:sess_", "web:anon"]},
    "mass_read": {"enabled": True, "threshold": 30, "window_s": 600},
    "tamper": {"enabled": True, "window_s": 3600},
}
# từ vựng cố định của `subject` (không bao giờ chữ tự do)
SECURITY_CONFIG_KEYS = ("gates.mode", "approval.profile", "guard.enabled", "guard.sandbox", "workspace.confine", "audit_watch", "audit")
SUBJECTS = frozenset({*SECURITY_CONFIG_KEYS, "guard.trust", "guard.rule_grant", "guard.sandbox_override", "audit.tamper_detected",
                      "guard.tamper_detected"})
SELF_KINDS = frozenset({"audit.alert"})
SECRET_KINDS = frozenset({"secret.set", "secret.rm", "read:secret.list"})
TAMPER_KINDS = frozenset({"audit.tamper_detected", "guard.tamper_detected"})
ACTIVE_KEY = "audit_watch.active"
SEEN_KEY = "audit_watch.seen"
MAX_WINDOWS = 5000
_PRINCIPAL_RE = re.compile(r"[^A-Za-z0-9_:.@*\-]")
_IP_RE = re.compile(r"^[0-9A-Fa-f.:]{2,45}$")
_HOURS_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
CFG_TTL_S = 5.0
SWEEP_MIN_S = 5.0


def clean_principal(p: Any) -> str:
    return _PRINCIPAL_RE.sub("?", str(p))[:64] or "?"


def parse_hours(v: Any) -> tuple[int, int] | None:
    """`"08:00-19:00"` → (phút bắt đầu, phút kết thúc) trong ngày; qua đêm được (`"22:00-06:00"`). Sai → ValueError; rỗng → None."""
    if v in (None, "", False):
        return None
    m = _HOURS_RE.match(str(v))
    if not m:
        raise ValueError("audit_watch.secret_access.hours phải dạng HH:MM-HH:MM (vd 08:00-19:00) hoặc null")
    a, b = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
    if not (0 <= a < 1440 and 0 <= b <= 1440) or a == b:
        raise ValueError("audit_watch.secret_access.hours không hợp lệ")
    return a, b


def in_hours(hours: tuple[int, int], dt: datetime) -> bool:
    m = dt.hour * 60 + dt.minute
    a, b = hours
    return a <= m < b if a < b else (m >= a or m < b)


def validate_setting(key: str, value: Any) -> None:
    """Kiểm khi `config.set audit_watch…` (gọi từ config.validate_setting)."""
    vals: dict[str, Any] = {}
    if key == "audit_watch":
        if not isinstance(value, dict):
            raise ValueError("audit_watch phải là object")
        vals = value
    else:
        rest = key.split(".", 1)[1]
        parts = rest.split(".")
        cur: dict[str, Any] = vals
        for p_ in parts[:-1]:
            cur = cur.setdefault(p_, {})
        cur[parts[-1]] = value
    for rule, sub in vals.items():
        if rule == "enabled":
            if not isinstance(sub, bool):
                raise ValueError("audit_watch.enabled phải là true/false")
            continue
        if rule not in RULES:
            raise ValueError(f"luật không có: {rule} (có: {', '.join(RULES)})")
        if not isinstance(sub, dict):
            raise ValueError(f"audit_watch.{rule} phải là object")
        for k, v in sub.items():
            if k in ("threshold", "window_s", "max_seen"):
                if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                    raise ValueError(f"audit_watch.{rule}.{k} phải là số nguyên ≥ 1")
            elif k == "enabled" and not isinstance(v, bool):
                raise ValueError(f"audit_watch.{rule}.enabled phải là true/false")
            elif k == "hours":
                parse_hours(v)
            elif k == "ignore_prefixes" and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise ValueError("audit_watch.new_principal.ignore_prefixes phải là danh sách chuỗi")


class AuditWatch:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.clock: Callable[[], float] = time.time
        self.now_local: Callable[[], datetime] = lambda: datetime.now().astimezone()
        self._lock = threading.RLock()
        self._windows: dict[tuple[str, str], deque[tuple[float, float, str]]] = {}
        self._active: dict[str, dict[str, Any]] | None = None
        self._seen: dict[str, Any] | None = None
        self._cfg: dict[str, Any] | None = None
        self._cfg_at = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._last_sweep = 0.0
        self.emitted: list[dict[str, Any]] = []  # chỉ để quan sát/test (20 mục cuối)
        a = getattr(hub, "audit", None)
        if a is not None and hasattr(a, "row_taps"):
            a.row_taps.append(self.on_row)

    # ------------------------------------------------------------------ cấu hình
    def enabled(self) -> bool:
        if os.environ.get("CFHARNESS_AUDIT_WATCH", "").lower() in ("off", "0", "false"):
            return False
        return bool(self.cfg().get("enabled", True))

    def cfg(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._cfg is None or now - self._cfg_at > CFG_TTL_S:
            try:
                over = self.hub.config.get("audit_watch") or {}
            except Exception:  # best-effort: cấu hình đọc lỗi → mặc định
                log.debug("không đọc được audit_watch", exc_info=True)
                over = {}
            merged: dict[str, Any] = {"enabled": over.get("enabled", True) if isinstance(over, dict) else True}
            for r in RULES:
                sub = over.get(r) if isinstance(over, dict) else None
                merged[r] = {**DEFAULTS[r], **(sub if isinstance(sub, dict) else {})}
            self._cfg, self._cfg_at = merged, now
        return self._cfg

    def invalidate(self) -> None:
        self._cfg = None

    def rule(self, name: str) -> dict[str, Any] | None:
        r = self.cfg().get(name) or {}
        return r if r.get("enabled", True) else None

    # ------------------------------------------------------------------ tap
    def on_row(self, row: dict[str, Any]) -> None:
        if not self.enabled():
            return
        kind = str(row.get("kind") or "")
        if kind in SELF_KINDS:
            return
        with self._lock:
            now = self.clock()
            for rule, principal, weight, subject, extra in self._matches(row, kind):
                self._hit(rule, principal, weight, subject, now, extra)
            if now - self._last_sweep >= SWEEP_MIN_S:  # dọn/báo hết tối đa mỗi vài giây trên đường ghi (vòng nền 30 s lo phần còn lại)
                self.sweep(now)

    def _matches(self, row: dict[str, Any], kind: str) -> list[tuple[str, str, float, str | None, str]]:
        out: list[tuple[str, str, float, str | None, str]] = []
        principal = row.get("principal")
        p = clean_principal(principal) if principal else "unknown"
        data = row.get("data_obj") if isinstance(row.get("data_obj"), dict) else {}
        outcome = row.get("outcome")
        if outcome == "denied" and not kind.startswith("web.login") and self.rule("denied_burst"):
            out.append(("denied_burst", p, 1.0, None, ""))
        if kind == "web.login_failed" and self.rule("login_fail_burst"):
            ip = str(data.get("ip") or row.get("target") or row.get("ip") or "")
            ip = ip if _IP_RE.match(ip) else "?"
            out.append(("login_fail_burst", f"ip:{ip}", 1.0, None, ip))
            out.append(("login_fail_burst", "*", 1.0, None, ip))
        if outcome not in ("error", "denied") and self.rule("security_change"):
            subj = self._security_subject(kind, row, data)
            if subj:
                out.append(("security_change", p, 1.0, subj, ""))
        if kind in SECRET_KINDS and self.rule("secret_access"):
            try:
                hours = parse_hours(self.rule("secret_access").get("hours"))  # type: ignore[union-attr]
            except ValueError:
                hours = None
            if hours is not None and not in_hours(hours, self.now_local()):
                out.append(("secret_access", p, 1.0, None, ""))
        if kind.startswith("read:") and self.rule("mass_read"):
            try:
                w = max(1.0, float(data.get("count") or 1))
            except (TypeError, ValueError):
                w = 1.0
            out.append(("mass_read", p, w, None, ""))
        if kind in TAMPER_KINDS and self.rule("tamper"):
            out.append(("tamper", "hub", 1.0, kind, ""))
        if principal and self.rule("new_principal") and self._is_new(p, int(row.get("id") or 0)):
            out.append(("new_principal", p, 1.0, None, ""))
        return out

    @staticmethod
    def _security_subject(kind: str, row: dict[str, Any], data: dict[str, Any]) -> str | None:
        if kind == "config.set":
            key = str(row.get("target") or "")
            for k in SECURITY_CONFIG_KEYS:
                if key == k or key.startswith(k + ".") or k.startswith(key + "."):
                    return k
            return None
        if kind == "guard.trust" and data.get("after") == "full":
            return "guard.trust"
        if kind == "guard.rule_grant" and data.get("action") == "grant":
            return "guard.rule_grant"
        if kind == "guard.sandbox_override":
            return "guard.sandbox_override"
        return None

    # ------------------------------------------------------------------ cửa sổ + chống trùng
    def _threshold(self, rule: str) -> int:
        if rule in ("security_change", "secret_access", "new_principal", "tamper"):
            return 1
        return int((self.rule(rule) or {}).get("threshold") or DEFAULTS[rule]["threshold"])

    def _window(self, rule: str) -> float:
        return float((self.rule(rule) or {}).get("window_s") or DEFAULTS[rule]["window_s"])

    def _hit(self, rule: str, principal: str, weight: float, subject: str | None, now: float, extra: str) -> None:
        win = self._window(rule)
        key = (rule, principal)
        dq = self._windows.get(key)
        if dq is None:
            if len(self._windows) >= MAX_WINDOWS:
                self._evict(now)
            dq = self._windows[key] = deque()
        dq.append((now, weight, extra))
        while dq and now - dq[0][0] > win:
            dq.popleft()
        total = sum(w for _, w, _ in dq)
        active = self._active_all()
        akey = f"{rule}|{principal}"
        cur = active.get(akey)
        if cur is not None:
            cur["last"] = now
            cur["count"] = int(cur.get("count") or 0) + int(weight)
            return
        if total < self._threshold(rule):
            return
        if rule == "login_fail_burst" and principal == "*" and len({x for _, _, x in dq}) < 2:
            return  # toàn cục = tấn công phân tán; một IP đã có cảnh báo `ip:<ip>` riêng
        active[akey] = {"rule": rule, "principal": principal, "since": now, "last": now, "count": int(total), "window_s": int(win),
                        **({"subject": subject} if subject in SUBJECTS else {})}
        self._save_active()
        self._emit(active[akey], "on")

    def _evict(self, now: float) -> None:
        stale = sorted(self._windows, key=lambda k: self._windows[k][-1][0] if self._windows[k] else 0.0)
        for k in stale[: max(1, len(stale) // 2)]:
            self._windows.pop(k, None)

    def sweep(self, now: float | None = None) -> list[dict[str, Any]]:
        """Báo `resolved` cho cảnh báo đã qua trọn cửa sổ không có lượt khớp; dọn cửa sổ rỗng."""
        with self._lock:
            now = self.clock() if now is None else now
            self._last_sweep = now
            done = []
            active = self._active_all()
            for akey, a in list(active.items()):
                if now - float(a.get("last") or 0) >= float(a.get("window_s") or 600):
                    done.append(active.pop(akey))
            if done:
                self._save_active()
                for a in done:
                    self._emit(a, "resolved")
            for k in [k for k, dq in self._windows.items() if not dq or now - dq[-1][0] > self._window(k[0])]:
                self._windows.pop(k, None)
            return done

    def _active_all(self) -> dict[str, dict[str, Any]]:
        if self._active is None:
            try:
                v = self.hub.store.state_get(ACTIVE_KEY) or {}
            except Exception:  # best-effort: hub_state đọc lỗi → bắt đầu rỗng
                log.debug("không đọc được %s", ACTIVE_KEY, exc_info=True)
                v = {}
            self._active = {k: x for k, x in v.items() if isinstance(x, dict) and x.get("rule") in RULES} if isinstance(v, dict) else {}
        return self._active

    def _save_active(self) -> None:
        try:
            self.hub.store.state_set(ACTIVE_KEY, self._active_all())
        except Exception:  # best-effort: mất trạng thái thì chỉ không báo "hết" sau khởi động lại
            log.warning("không lưu được %s", ACTIVE_KEY, exc_info=True)

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(a) for a in self._active_all().values()]

    # ------------------------------------------------------------------ principal mới
    def seed(self, before_id: int | None = None) -> dict[str, Any]:
        """Nạp tập principal đã thấy (một lần): từ audit sẵn có (`GROUP BY principal` — dùng index `(principal, ts)`). Hub chưa có
        principal nào (cài mới) → principal `os:` đầu tiên (chính chủ máy qua CLI) được học im lặng."""
        if self._seen is None:
            try:
                v = self.hub.store.state_get(SEEN_KEY)
            except Exception:  # best-effort
                log.debug("không đọc được %s", SEEN_KEY, exc_info=True)
                v = None
            if isinstance(v, dict) and isinstance(v.get("list"), list):
                self._seen = {"list": [str(x) for x in v["list"]], "quiet_os": bool(v.get("quiet_os"))}
            else:
                cap = int((self.cfg().get("new_principal") or {}).get("max_seen") or 1000)
                sql = "SELECT principal FROM audit_log WHERE principal IS NOT NULL" + (" AND id < ?" if before_id else "") + \
                      " GROUP BY principal LIMIT ?"
                rows = self.hub.store.conn.execute(sql, ((before_id, cap) if before_id else (cap,))).fetchall()
                ign = tuple(str(x) for x in ((self.cfg().get("new_principal") or {}).get("ignore_prefixes") or ()))
                lst = [x for x in (clean_principal(r[0]) for r in rows) if not x.startswith(ign)]
                self._seen = {"list": lst, "quiet_os": not lst}
                self._save_seen()
                log.info("audit_watch: nạp %d principal đã thấy từ audit sẵn có", len(lst))
        return self._seen

    def _save_seen(self) -> None:
        try:
            self.hub.store.state_set(SEEN_KEY, {"v": 1, **(self._seen or {})})
        except Exception:  # best-effort
            log.warning("không lưu được %s", SEEN_KEY, exc_info=True)

    def _is_new(self, p: str, row_id: int) -> bool:
        conf = self.rule("new_principal") or {}
        if p.startswith(tuple(str(x) for x in (conf.get("ignore_prefixes") or ()))):
            return False
        seen = self.seed(before_id=row_id or None)
        if p in seen["list"]:
            return False
        seen["list"].append(p)
        cap = int(conf.get("max_seen") or 1000)
        if len(seen["list"]) > cap:
            del seen["list"][: len(seen["list"]) - cap]
        quiet = bool(seen.get("quiet_os") and p.startswith("os:"))
        if quiet:
            seen["quiet_os"] = False
        self._save_seen()
        return not quiet

    # ------------------------------------------------------------------ phát
    def _emit(self, a: dict[str, Any], state: str) -> None:
        payload: dict[str, Any] = {"rule": a["rule"], "principal": clean_principal(a["principal"]), "count": int(a.get("count") or 0),
                                   "window_s": int(a.get("window_s") or 0), "state": state}
        if a.get("subject") in SUBJECTS:
            payload["subject"] = a["subject"]
        self.emitted = [*self.emitted[-19:], payload]
        (log.warning if state == "on" else log.info)("audit.alert %s", payload)
        try:
            self.hub.audit.record("audit.alert", None, None, dict(payload), actor="hub", channel="runtime",  # type: ignore[attr-defined]
                                  target=payload["principal"], summary=f"audit.alert:{payload['rule']}:{state}", principal="hub:audit_watch")
        except Exception:  # best-effort: không ghi được vẫn phát event
            log.exception("không ghi được audit.alert")
        from .events import make_event

        ev = make_event("audit.alert", None, payload, source="hub")
        loop = self._loop
        try:
            on_loop = loop is not None and asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        try:
            if loop is not None and not on_loop and loop.is_running():
                loop.call_soon_threadsafe(self.hub.publish, ev)
            else:
                self.hub.publish(ev)
        except Exception:  # best-effort
            log.exception("không phát được audit.alert")

    # ------------------------------------------------------------------ vòng đời
    def start(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        if self.enabled():
            try:
                self.seed()
            except Exception:  # best-effort
                log.exception("không nạp được tập principal đã thấy")
        if self._loop is not None:
            self._task = self._loop.create_task(self._sweep_loop(), name="audit-watch")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                if self.enabled():
                    self.sweep()
            except Exception:  # pragma: no cover
                log.exception("lượt kiểm cảnh báo audit lỗi")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


def attach(hub: "Hub") -> AuditWatch:
    hub.audit_watch = AuditWatch(hub)  # type: ignore[attr-defined]
    return hub.audit_watch  # type: ignore[attr-defined]


__all__ = ["DEFAULTS", "RULES", "RULE_VI", "AuditWatch", "attach", "clean_principal", "in_hours", "parse_hours", "validate_setting"]
