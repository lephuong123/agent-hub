"""Pipeline một lời gọi admind (plan-superadmin §2.2, BR-SA-01).

giải mã → tra `REGISTRY` → kiểm nguồn (root_only/public) → phiên → validate tham số → vai trò (`rbac.MATRIX`) → phạm vi →
step-up (risk=high) → four-eyes (risk=critical) → chạy → audit (kể cả khi bị từ chối) → trả kết quả.

`Dispatcher.handle(req, peer)` THUẦN (không socket) → test gọi trực tiếp trên mọi nền tảng; `server.py` chỉ bọc socket.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from . import rbac
from .audit import AdminAudit
from .auth import AuthError, AuthService, Ticket
from .db import SacDB
from .ops import REGISTRY, Op, OpError, load_all
from .paths import AdmindPaths
from .vault import Vault

log = logging.getLogger("cfharness.admind")

MAX_REQ_BYTES = 256 * 1024


@dataclass
class Peer:
    uid: int | None
    root: bool = False


@dataclass
class State:
    """Mọi thứ admind giữ. Tạo bằng `State.open(paths)`; test truyền runner/cf/clock giả."""

    paths: AdmindPaths
    db: SacDB
    vault: Vault
    auth: AuthService
    audit: AdminAudit
    clock: Callable[[], float] = time.time
    runner: Any = None          # admind.runner.Runner
    cloudflare: Any = None      # admind.cloudflare.CloudflareAPI | giả
    jobs: Any = None            # admind.jobs.JobEngine
    approvals: Any = None       # admind.approvals.Approvals
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def open(cls, paths: AdmindPaths | None = None, *, clock: Callable[[], float] = time.time, runner: Any = None,
             cloudflare: Any = None, scrypt_n: int | None = None) -> "State":
        p = paths or AdmindPaths.default()
        p.ensure()
        db = SacDB(p.db)
        vault = Vault(p.vault)
        auth = AuthService(db, vault, clock=clock, **({"scrypt_n": scrypt_n} if scrypt_n else {}))
        st = cls(paths=p, db=db, vault=vault, auth=auth, audit=AdminAudit(db), clock=clock, runner=runner, cloudflare=cloudflare)
        from .approvals import Approvals
        from .jobs import JobEngine

        st.approvals = Approvals(st)
        st.jobs = JobEngine(st)
        return st


@dataclass
class Ctx:
    state: State
    op: Op
    peer: Peer
    account: dict[str, Any] | None = None
    ticket: Ticket | None = None
    ip: str = ""
    ua: str = ""
    req_id: str = ""
    approved_by: str | None = None   # đang chạy một yêu cầu four-eyes đã duyệt
    job_id: str | None = None
    audit_target: str | None = None  # handler đặt lại khi target chỉ biết sau khi chạy

    @property
    def db(self) -> SacDB:
        return self.state.db

    @property
    def actor_id(self) -> str | None:
        return self.account["id"] if self.account else ("root" if self.peer.root else None)

    def require(self, cond: bool, code: str, message: str, data: Any = None) -> None:
        if not cond:
            raise OpError(code, message, data)

    def user_subject(self, user_id_or_os: str) -> dict[str, Any] | None:
        """Thông tin phạm vi của một user hệ thống (theo id hoặc tên user OS) — None nếu không có."""
        u = self.db.one("SELECT id, group_id FROM hub_users WHERE id=? OR (os_user=? AND host_id=?)",
                        (user_id_or_os, user_id_or_os, self.db.host_id))
        if not u:
            return None
        zones = [r["name"] for r in self.db.all("SELECT z.name FROM domains d JOIN zones z ON z.id=d.zone_id WHERE d.hub_user_id=?", (u["id"],))]
        teams = [r["team_id"] for r in self.db.all("SELECT team_id FROM team_members WHERE user_id=?", (u["id"],))]
        return {"id": u["id"], "group_id": u["group_id"], "zones": zones, "teams": teams}

    def can(self, group: str, need: str = "read") -> bool:
        return bool(self.account) and rbac.allowed(self.account["role"], group, need)

    def scoped_user_ids(self) -> set[str] | None:
        """Tập user_id tài khoản này được thấy; None = không giới hạn."""
        if not self.account or rbac.unrestricted(self.account):
            return None
        out = set()
        for u in self.db.all("SELECT id FROM hub_users"):
            if rbac.in_scope(self.account, self.user_subject(u["id"])):
                out.add(u["id"])
        return out


class Dispatcher:
    def __init__(self, state: State, *, allowed_uids: set[int] | None = None) -> None:
        self.state = state
        self.allowed_uids = allowed_uids
        self.ops = load_all()
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ tiện ích
    @staticmethod
    def _err(code: str, message: str, data: Any = None, req_id: str = "") -> dict[str, Any]:
        e: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            e["data"] = data
        return {"ok": False, "error": e, "req_id": req_id}

    def _audit(self, ctx: Ctx | None, op_name: str, outcome: str, *, params: Any = None, code: str | None = None, ip: str = "",
               actor: dict[str, Any] | None = None, target: str | None = None) -> None:
        try:
            self.state.audit.record(op=op_name, outcome=outcome, actor=actor or (ctx.account if ctx else None) or
                                    ({"id": "root", "role": "root"} if ctx and ctx.peer.root else None),
                                    ip=ip or (ctx.ip if ctx else ""), target=target or (ctx.audit_target if ctx else None), params=params,
                                    error_code=code, job_id=ctx.job_id if ctx else None, approver_id=ctx.approved_by if ctx else None)
        except Exception:  # pragma: no cover — audit hỏng không được che lỗi gốc; vẫn log
            log.exception("ghi audit lỗi (%s)", op_name)

    # ------------------------------------------------------------------ pipeline
    def handle(self, req: Any, peer: Peer, *, approved_by: str | None = None, run_as: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(req, dict):
            return self._err("invalid", "yêu cầu không hợp lệ")
        rid = str(req.get("req_id") or "")[:64]
        if self.allowed_uids is not None and not peer.root and peer.uid not in self.allowed_uids:
            return self._err("forbidden", "người gọi không được phép", req_id=rid)
        name = req.get("op")
        o = self.ops.get(name) if isinstance(name, str) else None
        meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
        ip, ua = str(meta.get("ip") or "")[:64], str(meta.get("ua") or "")[:200]
        if o is None:
            return self._err("invalid", "thao tác không tồn tại", req_id=rid)
        raw = req.get("params") if req.get("params") is not None else {}
        ctx = Ctx(state=self.state, op=o, peer=peer, ip=ip, ua=ua, req_id=rid, approved_by=approved_by)
        audited = o.need == "write" or o.audit_read
        try:
            if o.root_only:
                ctx.require(peer.root, "forbidden", "thao tác chỉ chạy từ root trên máy")
            elif run_as is not None:
                ctx.account = run_as
            elif not o.public:
                try:
                    ctx.ticket, ctx.account = self.state.auth.resolve(req.get("ticket"))
                except AuthError as e:
                    raise OpError(e.code, e.message, e.data) from None
            if ctx.account and not o.public and not o.root_only and not rbac.allowed(ctx.account["role"], o.group, o.need):
                raise OpError("forbidden", "không có quyền")  # trước khi kiểm tham số — không cho dò mã/tham số
            try:
                params = o.params.model_validate(raw)
            except ValidationError as e:
                raise OpError("invalid", "tham số không hợp lệ",
                              [{"field": ".".join(str(x) for x in er["loc"]), "error": er["msg"]} for er in e.errors()[:10]]) from None
            ctx.audit_target = o.target(params) if o.target else None
            if ctx.account and not o.public and not o.root_only:
                if o.scope == "machine" and not rbac.unrestricted(ctx.account):
                    raise OpError("forbidden", "ngoài phạm vi được giao")
                if o.scope == "user" and ctx.audit_target and not rbac.unrestricted(ctx.account):
                    if not rbac.in_scope(ctx.account, ctx.user_subject(ctx.audit_target)):
                        raise OpError("forbidden", "ngoài phạm vi được giao")
                if o.scope == "team" and ctx.audit_target and not rbac.unrestricted(ctx.account):
                    if ctx.audit_target not in rbac.scope_of(ctx.account).get("teams", []):
                        raise OpError("forbidden", "ngoài phạm vi team được giao")
                if o.risk in ("high", "critical") and ctx.ticket is not None and approved_by is None:
                    if not self.state.auth.strong(ctx.ticket):
                        raise OpError("step_up_required", "cần nhập lại mã TOTP")
                if o.risk == "critical" and approved_by is None:
                    ap = self.state.approvals.request(ctx, raw if isinstance(raw, dict) else {})
                    self._audit(ctx, o.name, "pending", params=raw)
                    return {"ok": False, "error": {"code": "approval_required", "message": "đã tạo yêu cầu chờ Super Admin khác duyệt",
                                                   "data": ap}, "req_id": rid}
            result = o.handler(ctx, params)
        except OpError as e:
            if audited or e.code in ("forbidden", "step_up_required"):
                self._audit(ctx, o.name, "denied" if e.code in ("forbidden", "unauthenticated", "step_up_required", "locked") else "error",
                            params=raw, code=e.code)
            return self._err(e.code, e.message, e.data, rid)
        except Exception as e:  # noqa: BLE001 — lỗi lạ: không lộ chi tiết, log đầy đủ
            log.exception("thao tác %s lỗi", o.name)
            if audited:
                self._audit(ctx, o.name, "error", params=raw, code="internal")
            return self._err("internal", f"lỗi nội bộ ({type(e).__name__})", req_id=rid)
        if audited:
            self._audit(ctx, o.name, "ok", params=raw)
        return {"ok": True, "result": result, "req_id": rid}
