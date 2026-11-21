"""Chính sách qua admind (FR-SA-POL; plan-superadmin SA4-1) — thay bước `sudo install` tay của plan-admin-users.

Phạm vi: `machine`, `default`, `group:<id>`, `team:<id>`, `user:<id>`. Mỗi lần lưu tăng `rev` của phạm vi đó + lưu người sửa + diff
(`policy_versions`); rollback = lưu lại nội dung bản trước thành bản mới.

Hub KHÔNG đổi: nó vẫn đọc `policy/_machine.json` ∧ (`<u>.json` | `_default.json`) và lấy giá trị chặt hơn. admind ghi
- `_machine.json` = phạm vi machine; `_default.json` = phạm vi default;
- `<u>.json` = CHẶT HƠN của (user nếu có, không thì default) ∧ nhóm của user ∧ mọi team của user — `rev` = bộ đếm toàn cục tăng dần
  (lưu `policy.user_rev.<u>` để collector so với `policy.rev_applied` hub báo → cảnh báo `policy_mismatch`).
File root:wheel 0644, ghi nguyên tử.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from .. import admin_control as AC
from ..admin_policy import POLICY_SPEC, _sanitize, _stricter, _vtuple
from .db import dumps, loads
from .ops import OpError, op
from .ptypes import Params
from .shared import write_json

SCOPE_RE = r"^(machine|default|(group|team|user):[a-z]{1,4}_[0-9a-f]{8,32})$"
FLAT_KEYS = (("budget", "usd_per_day"), ("budget", "usd_per_run_max"), ("runs", "max_concurrent"), ("require", "sandbox"),
             ("require", "confine"), ("require", "export"), ("models", "allow"), ("min_version", None), ("policy_required", None))


def current(db: Any, scope: str) -> tuple[int, dict[str, Any]]:
    r = db.one("SELECT rev, content FROM policy_versions WHERE scope=? ORDER BY rev DESC LIMIT 1", (scope,))
    return (int(r["rev"]), loads(r["content"], {}) or {}) if r else (0, {})


def _get(d: dict[str, Any], sec: str, key: str | None) -> Any:
    return d.get(sec) if key is None else (d.get(sec) or {}).get(key)


def diff(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    out = []
    for sec, key in FLAT_KEYS:
        x, y = _get(a, sec, key), _get(b, sec, key)
        if x != y:
            out.append(f"{sec}{'.' + key if key else ''}: {json.dumps(x, ensure_ascii=False)} → {json.dumps(y, ensure_ascii=False)}")
    return out


def user_scopes(db: Any, user_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Các phạm vi áp cho user (trừ machine — hub tự gộp): user (hoặc default) + nhóm + các team."""
    out: list[tuple[str, dict[str, Any]]] = []
    rev, own = current(db, f"user:{user_id}")
    out.append((f"user:{user_id}", own) if rev else ("default", current(db, "default")[1]))
    u = db.one("SELECT group_id FROM hub_users WHERE id=?", (user_id,))
    if u and u["group_id"]:
        out.append((f"group:{u['group_id']}", current(db, f"group:{u['group_id']}")[1]))
    for t in db.all("SELECT team_id FROM team_members WHERE user_id=?", (user_id,)):
        out.append((f"team:{t['team_id']}", current(db, f"team:{t['team_id']}")[1]))
    return out


def compose(parts: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in parts:
        out = _stricter(out, p) if out else _stricter(p, p)
    return out


def write_files(state: Any, user_ids: list[str] | None = None) -> list[str]:
    """Ghi lại file chính sách cho các user (None = mọi user đang có) + _machine/_default."""
    d = AC.policy_dir()
    d.mkdir(parents=True, exist_ok=True)
    counter = int(state.db.get_setting("policy.rev_counter", 0) or 0)
    written = []
    for scope, name in (("machine", "_machine.json"), ("default", "_default.json")):
        rev, content = current(state.db, scope)
        if rev:
            counter += 1
            write_json(d / name, {**content, "rev": counter})
            state.db.set_setting(f"policy.file_rev.{name}", counter)
            written.append(name)
    q = "SELECT id, os_user FROM hub_users WHERE state NOT IN ('deleted')"
    users = [u for u in state.db.all(q) if user_ids is None or u["id"] in user_ids]
    for u in users:
        parts = user_scopes(state.db, u["id"])
        if not any(c for _s, c in parts):
            continue
        counter += 1
        write_json(d / f"{u['os_user']}.json", {**compose([c for _s, c in parts]), "rev": counter})
        state.db.set_setting(f"policy.user_rev.{u['os_user']}", counter)
        written.append(f"{u['os_user']}.json")
    state.db.set_setting("policy.rev_counter", counter)
    return written


def affected_users(db: Any, scope: str) -> list[str] | None:
    if scope in ("machine", "default"):
        return None
    kind, _, oid = scope.partition(":")
    if kind == "user":
        return [oid]
    if kind == "group":
        return [u["id"] for u in db.all("SELECT id FROM hub_users WHERE group_id=?", (oid,))]
    return [m["user_id"] for m in db.all("SELECT user_id FROM team_members WHERE team_id=?", (oid,))]


def validate(content: dict[str, Any], scope: str) -> dict[str, Any]:
    clean = _sanitize(content)
    clean.pop("rev", None)
    if scope != "machine":
        clean.pop("policy_required", None)
    junk = set(content) - set(POLICY_SPEC) - {"rev"}
    if junk:
        raise OpError("invalid", f"khoá không hợp lệ: {', '.join(sorted(junk))}")
    for sec, key in (("budget", "usd_per_day"), ("budget", "usd_per_run_max")):
        v = _get(clean, sec, key)
        if v is not None and not 0 <= float(v) <= 100000:
            raise OpError("invalid", f"{sec}.{key} ngoài khoảng")
    mc = _get(clean, "runs", "max_concurrent")
    if mc is not None and not 1 <= int(mc) <= 50:
        raise OpError("invalid", "runs.max_concurrent trong 1..50")
    return clean


def save(ctx, scope: str, content: dict[str, Any]) -> dict[str, Any]:
    clean = validate(content, scope)
    rev, old = current(ctx.db, scope)
    ctx.db.exec("INSERT INTO policy_versions(scope, rev, content, author, created_at, diff) VALUES (?,?,?,?,?,?)",
                (scope, rev + 1, dumps(clean), ctx.actor_id, ctx.state.clock(), "\n".join(diff(old, clean))))
    files = write_files(ctx.state, affected_users(ctx.db, scope))
    return {"scope": scope, "rev": rev + 1, "diff": diff(old, clean), "files": files}


def save_user_budget(ctx, os_user: str, usd: float) -> None:
    u = ctx.db.one("SELECT id FROM hub_users WHERE os_user=?", (os_user,))
    if u:
        _rev, cur = current(ctx.db, f"user:{u['id']}")
        save(ctx, f"user:{u['id']}", {**cur, "budget": {**(cur.get("budget") or {}), "usd_per_day": usd}})


# ============================================================================ thao tác
@op("policy.list", group="policy", risk="read")
def policy_list(ctx, p) -> list[dict[str, Any]]:
    """Mọi phạm vi có chính sách + rev hiện tại + người sửa cuối."""
    rows = ctx.db.all("SELECT scope, MAX(rev) AS rev FROM policy_versions GROUP BY scope ORDER BY scope")
    out = []
    for r in rows:
        last = ctx.db.one("SELECT author, created_at, content FROM policy_versions WHERE scope=? AND rev=?", (r["scope"], r["rev"]))
        out.append({"scope": r["scope"], "rev": r["rev"], "author": last["author"], "at": last["created_at"], "content": loads(last["content"], {}),
                    "label": scope_label(ctx.db, r["scope"])})
    return out


def scope_label(db: Any, scope: str) -> str:
    if scope == "machine":
        return "Toàn máy"
    if scope == "default":
        return "Mặc định"
    kind, _, oid = scope.partition(":")
    if kind == "user":
        u = db.one("SELECT os_user FROM hub_users WHERE id=?", (oid,))
        return f"User {u['os_user'] if u else oid}"
    if kind == "group":
        g = db.one("SELECT name FROM groups_ WHERE id=?", (oid,))
        return f"Nhóm {g['name'] if g else oid}"
    t = db.one("SELECT name FROM teams WHERE id=?", (oid,))
    return f"Team {t['name'] if t else oid}"


class ScopeP(Params):
    scope: str = Field(pattern=SCOPE_RE)


@op("policy.get", group="policy", risk="read", params=ScopeP)
def policy_get(ctx, p: ScopeP) -> dict[str, Any]:
    """Một phạm vi: bản hiện tại + lịch sử phiên bản."""
    rev, content = current(ctx.db, p.scope)
    hist = ctx.db.all("SELECT rev, author, created_at, diff FROM policy_versions WHERE scope=? ORDER BY rev DESC LIMIT 50", (p.scope,))
    return {"scope": p.scope, "label": scope_label(ctx.db, p.scope), "rev": rev, "content": content, "history": hist}


class SaveP(ScopeP):
    content: dict[str, Any]


@op("policy.save", group="policy", risk="high", params=SaveP, target=lambda p: p.scope)
def policy_save(ctx, p: SaveP) -> dict[str, Any]:
    """Lưu chính sách (rev+1, diff) → ghi lại file cho các user bị ảnh hưởng."""
    return save(ctx, p.scope, p.content)


@op("policy.rollback", group="policy", risk="high", params=ScopeP, target=lambda p: p.scope)
def policy_rollback(ctx, p: ScopeP) -> dict[str, Any]:
    """Quay về bản trước một bước (lưu thành bản mới)."""
    rev, _cur = current(ctx.db, p.scope)
    prev = ctx.db.one("SELECT content FROM policy_versions WHERE scope=? AND rev=?", (p.scope, rev - 1))
    if not prev:
        raise OpError("conflict", "không có bản trước")
    return save(ctx, p.scope, loads(prev["content"], {}))


class UserP(Params):
    user: str = Field(min_length=1, max_length=64)


@op("policy.effective", group="policy", risk="read", params=UserP)
def policy_effective(ctx, p: UserP) -> dict[str, Any]:
    """Với một user: từng khoá, giá trị cuối và nguồn (máy / mặc định / nhóm / team / user) — POL-03."""
    u = ctx.db.one("SELECT id, os_user FROM hub_users WHERE id=? OR os_user=?", (p.user, p.user))
    if not u:
        raise OpError("not_found", "không có user")
    parts = [("machine", current(ctx.db, "machine")[1]), *user_scopes(ctx.db, u["id"])]
    final = compose([c for _s, c in parts])
    rows = []
    for sec, key in FLAT_KEYS:
        v = _get(final, sec, key) if not (sec == "policy_required") else _get(parts[0][1], sec, key)
        if v is None:
            continue
        src = [scope_label(ctx.db, s) for s, c in parts if _get(c, sec, key) == v]
        rows.append({"key": f"{sec}{'.' + key if key else ''}", "value": v, "source": ", ".join(src) or "gộp"})
    return {"user": u["os_user"], "policy": final, "rows": rows, "rev_written": ctx.db.get_setting(f"policy.user_rev.{u['os_user']}")}


@op("policy.simulate", group="policy", risk="read", params=SaveP)
def policy_simulate(ctx, p: SaveP) -> dict[str, Any]:
    """Trước khi lưu: user bị ảnh hưởng + hub sẽ bị chặn run (vd phiên bản < min_version, chưa bật sandbox mà require.sandbox)."""
    from .collector import snapshot

    clean = validate(p.content, p.scope)
    ids = affected_users(ctx.db, p.scope)
    users = ctx.db.all("SELECT id, os_user FROM hub_users WHERE state IN ('active','suspended')")
    out = []
    for u in users:
        if ids is not None and u["id"] not in ids:
            continue
        parts = [("machine", clean if p.scope == "machine" else current(ctx.db, "machine")[1])]
        for s, c in user_scopes(ctx.db, u["id"]):
            parts.append((s, clean if s == p.scope or (p.scope == "default" and s == "default") else c))
        if p.scope.startswith(("group:", "team:", "user:")) and p.scope not in [s for s, _c in parts]:
            parts.append((p.scope, clean))
        final = compose([c for _s, c in parts])
        h = ctx.db.one("SELECT version, snapshot FROM hubs WHERE user_id=?", (u["id"],)) or {}
        snap = snapshot(h)
        block = []
        if final.get("min_version") and h.get("version") and _vtuple(h["version"]) < _vtuple(final["min_version"]):
            block.append(f"phiên bản {h['version']} < {final['min_version']}")
        if (final.get("require") or {}).get("sandbox") and (snap.get("guard") or {}).get("sandbox") is False:
            block.append("chưa bật sandbox")
        if (final.get("require") or {}).get("confine") and (snap.get("guard") or {}).get("confine") is False:
            block.append("chưa giam workspace")
        out.append({"os_user": u["os_user"], "policy": final, "blocked": block})
    return {"affected": len(out), "users": out, "would_block": [x for x in out if x["blocked"]]}
