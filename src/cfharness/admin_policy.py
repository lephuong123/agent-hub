"""Chính sách quản trị máy do root ghi (plan-admin-users P2; chi tiết `docs/dev-notes/admin-users.md`).

File `policy/{_machine,_default,<user>}.json` trong thư mục chung (đường dẫn HẰNG SỐ — `admin_control.policy_dir()`), root:wheel 0644.
Đọc như file trạng thái của người khác: không theo symlink, file thường ≤ 16 KB, CHỦ uid 0 (`TRUSTED_UIDS`), không ghi được bởi
nhóm/khác, JSON object, lọc lại qua `POLICY_SPEC` (khoá lạ bị bỏ).

Hiệu lực cho user U = `_machine.json` ∧ (`U.json` nếu có, ngược lại `_default.json`), mỗi khoá lấy giá trị CHẶT HƠN:
số (ngân sách/ngày, trần mỗi run, đồng thời) → nhỏ nhất; `require.*` → OR; `models.allow` → giao; `min_version` → lớn nhất.
Áp dụng cùng cấu hình của hub theo kiểu chặt hơn: scope ngân sách `admin` (RunManager.budget_scopes), `cap_per_run`, `cap_concurrency`,
`submit_gate` (require.sandbox/confine, min_version, models.allow), `require_export` (ép bật AdminExporter).

Hỏng / sai chủ / ghi được bởi người khác → bỏ file đó, cảnh báo (fail-OPEN, `valid=false` vào bản xuất → admin thấy `policy_mismatch`).
Ngoại lệ: `_machine.json` hợp lệ có `policy_required: true` mà file của user/mặc định hỏng hoặc thiếu → CHẶN mọi run (fail-closed).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import Blocked

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("cfharness.admin_policy")

MAX_FILE = 16 * 1024
TTL_S = 10.0
TRUSTED_UIDS: tuple[int, ...] = (0,)  # test đổi thành uid của user chạy test
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,63}$")

POLICY_SPEC: dict[str, Any] = {
    "rev": "int",
    "policy_required": "bool",
    "budget": {"usd_per_day": "num", "usd_per_run_max": "num"},
    "runs": {"max_concurrent": "int"},
    "require": {"sandbox": "bool", "confine": "bool", "export": "bool"},
    "models": {"allow": "list:model"},
    "min_version": "version",
}


def _sanitize(obj: Any) -> dict[str, Any]:
    from .admin_overview import sanitize

    return sanitize(obj, POLICY_SPEC)


def read_policy_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(dữ liệu đã lọc | None, mã lỗi | None). `missing` không phải lỗi."""
    from .admin_control import read_small

    raw, st, err = read_small(path, MAX_FILE)
    if err == "missing":
        return None, "missing"
    if raw is None or st is None:
        return None, err or "unreadable"
    if st.st_uid not in TRUSTED_UIDS:
        return None, "bad_owner"
    if st.st_mode & 0o022:
        return None, "writable_by_others"
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "not_object"
    return _sanitize(obj), None


def _stricter(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sec, key in (("budget", "usd_per_day"), ("budget", "usd_per_run_max"), ("runs", "max_concurrent")):
        vals = [x[sec][key] for x in (a, b) if isinstance(x.get(sec), dict) and x[sec].get(key) is not None]
        if vals:
            out.setdefault(sec, {})[key] = min(vals)
    for key in ("sandbox", "confine", "export"):
        if any((x.get("require") or {}).get(key) for x in (a, b)):
            out.setdefault("require", {})[key] = True
    allows = [list((x.get("models") or {}).get("allow") or []) for x in (a, b) if (x.get("models") or {}).get("allow")]
    if allows:
        s = allows[0] if len(allows) == 1 else [m for m in allows[0] if m in allows[1]]
        out["models"] = {"allow": s}
    vers = [x["min_version"] for x in (a, b) if x.get("min_version")]
    if vers:
        out["min_version"] = max(vers, key=_vtuple)
    return out


def _vtuple(v: str) -> tuple[int, ...]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def effective(user: str, directory: Path | None = None) -> dict[str, Any]:
    """Chính sách có hiệu lực cho `user` + nguồn/lỗi. Không ném."""
    from .admin_control import USER_RE, policy_dir

    d = Path(directory) if directory else policy_dir()
    out: dict[str, Any] = {"dir": str(d), "exists": d.is_dir(), "policy": {}, "rev": None, "machine_rev": None, "sources": [], "errors": [],
                           "valid": True, "required": False, "blocked": False}
    if not out["exists"]:
        return out
    machine, merr = read_policy_file(d / "_machine.json")
    if merr and merr != "missing":
        out["errors"].append(f"_machine:{merr}")
    own_name = f"{user}.json" if USER_RE.match(user or "") else None
    own, oerr = read_policy_file(d / own_name) if own_name else (None, "missing")
    src_user = "user"
    if oerr == "missing":
        own, oerr = read_policy_file(d / "_default.json")
        src_user = "default"
    if oerr and oerr != "missing":
        out["errors"].append(f"{src_user}:{oerr}")
    if machine is not None:
        out["sources"].append("machine")
        out["machine_rev"] = machine.get("rev")
        out["required"] = bool(machine.get("policy_required"))
    if own is not None:
        out["sources"].append(src_user)
        out["rev"] = own.get("rev")
    m2 = {k: v for k, v in (machine or {}).items() if k not in ("rev", "policy_required")}
    o2 = {k: v for k, v in (own or {}).items() if k not in ("rev", "policy_required")}
    out["policy"] = _stricter(m2, o2)
    out["valid"] = not out["errors"]
    # fail-closed CHỈ khi máy bắt buộc chính sách và phần của user (riêng hoặc mặc định) hỏng/thiếu
    out["blocked"] = bool(out["required"] and (own is None or bool(oerr and oerr != "missing")))
    return out


class PolicyCache:
    """Đọc lại tối đa mỗi TTL_S giây (đường nóng submit/tick)."""

    def __init__(self) -> None:
        self._v: dict[str, Any] | None = None
        self._at = 0.0
        self._key: tuple[str, str] | None = None
        self._warned: set[str] = set()

    def get(self, user: str | None = None) -> dict[str, Any]:
        from .admin_control import os_user, policy_dir

        user = user or os_user()
        key = (user, str(policy_dir()))
        now = time.monotonic()
        if self._v is None or self._key != key or now - self._at > TTL_S:
            self._v = effective(user)
            self._at, self._key = now, key
            for e in self._v["errors"]:
                if e not in self._warned:
                    self._warned.add(e)
                    log.warning("chính sách quản trị máy: bỏ qua file lỗi (%s) — %s", e,
                                "CHẶN run vì _machine.json có policy_required" if self._v["blocked"] else "fail-open, báo admin")
        return self._v

    def invalidate(self) -> None:
        self._v = None


_CACHE = PolicyCache()


def current(hub: "Hub | None" = None) -> dict[str, Any]:
    c = getattr(hub, "admin_policy", None) if hub is not None else None
    return (c if isinstance(c, PolicyCache) else _CACHE).get()


def attach(hub: "Hub") -> None:
    hub.admin_policy = PolicyCache()  # type: ignore[attr-defined]


# ============================================================================ áp dụng (RunManager)
def _pol(hub: "Hub") -> dict[str, Any]:
    try:
        return current(hub).get("policy") or {}
    except Exception:  # pragma: no cover — đọc chính sách không bao giờ làm hỏng đường chạy run
        log.exception("đọc chính sách quản trị máy lỗi")
        return {}


def budget_scope(hub: "Hub") -> list[tuple[str, str, float, float | None]]:
    lim = (_pol(hub).get("budget") or {}).get("usd_per_day")
    if not lim:
        return []
    return [("admin", "admin", hub.store.cost_today(), float(lim))]


def cap_per_run(hub: "Hub", per_run: Any) -> Any:
    cap = (_pol(hub).get("budget") or {}).get("usd_per_run_max")
    if not cap:
        return per_run
    return min(float(per_run), float(cap)) if per_run else float(cap)


def cap_concurrency(hub: "Hub", limit: int) -> int:
    cap = (_pol(hub).get("runs") or {}).get("max_concurrent")
    return max(1, min(int(limit), int(cap))) if cap else limit


def require_export(hub: "Hub") -> bool:
    return bool((_pol(hub).get("require") or {}).get("export"))


def _model_allowed(model: str | None, allow: list[str]) -> bool:
    if not allow or not model:
        return True  # model mặc định của CLI (không đặt) — không biết tên, cho qua (docs: giới hạn)
    return any(model == a or model.startswith(a + "-") for a in allow)  # "claude-sonnet-4" cho cả "claude-sonnet-4-5"


def submit_gate(hub: "Hub", agent: dict[str, Any], model: str | None) -> None:
    from . import __version__

    try:
        eff = current(hub)
    except Exception:  # pragma: no cover
        log.exception("đọc chính sách quản trị máy lỗi")
        return
    if eff.get("blocked"):
        raise Blocked("chính sách quản trị máy hỏng/thiếu trong khi máy BẮT BUỘC có chính sách (policy_required) — báo người quản trị máy",
                      data={"reason": "admin_policy_invalid", "errors": eff.get("errors")})
    pol = eff.get("policy") or {}
    req = pol.get("require") or {}
    if req.get("sandbox") and not bool((hub.config.get("guard.sandbox") or {}).get("enabled", True)):
        raise Blocked("chính sách quản trị máy bắt buộc sandbox (guard.sandbox.enabled=true)", data={"reason": "admin_require_sandbox"})
    if req.get("confine"):
        from .confine import from_config

        if from_config(hub.config) is None:
            raise Blocked("chính sách quản trị máy bắt buộc giam workspace (workspace.confine.root)", data={"reason": "admin_require_confine"})
    mv = pol.get("min_version")
    if mv and _vtuple(__version__) < _vtuple(mv):
        raise Blocked(f"chính sách quản trị máy yêu cầu cfharness ≥ {mv} (đang {__version__}) — `cfharness update`",
                      data={"reason": "admin_min_version", "min_version": mv})
    allow = list((pol.get("models") or {}).get("allow") or [])
    m = model or agent.get("model")
    if not _model_allowed(m, allow):
        raise Blocked(f"chính sách quản trị máy không cho model '{m}' (cho phép: {', '.join(allow)})", data={"reason": "admin_model_not_allowed"})


__all__ = ["POLICY_SPEC", "PolicyCache", "attach", "budget_scope", "cap_concurrency", "cap_per_run", "current", "effective",
           "read_policy_file", "require_export", "submit_gate"]
