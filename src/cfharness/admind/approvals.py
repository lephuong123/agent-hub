"""Four-eyes (AUTH-06, BR-SA-04): thao tác mức Tới hạn tạo yêu cầu chờ, Super Admin THỨ HAI duyệt trong 24 giờ mới chạy.

- Hệ chỉ có 1 Super Admin đang hoạt động → không có người thứ hai: yêu cầu tự được duyệt bởi `timer` sau `SOLO_WAIT_S` (1 giờ),
  kèm cảnh báo; trong thời gian chờ người yêu cầu huỷ được.
- Thao tác khai `wait_after_approve_s` (vd xoá hẳn user: 24 giờ) → sau khi duyệt còn chờ thêm, ai trong hai người cũng huỷ được.
- Tham số GỐC (có thể chứa secret) nằm trong vault (`approval.<id>`), DB chỉ giữ bản đã che.
- Chạy: `run_due()` (vòng nền) gọi lại Dispatcher với `approved_by` + tài khoản người yêu cầu (`run_as`).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .audit import scrub
from .db import dumps, loads, new_id

if TYPE_CHECKING:
    from .dispatch import Ctx, State

TTL_S = 24 * 3600
SOLO_WAIT_S = 3600
STATES = ("pending", "approved", "rejected", "expired", "cancelled", "done", "failed")


class Approvals:
    def __init__(self, state: "State") -> None:
        self.state = state

    @property
    def db(self):  # noqa: ANN201
        return self.state.db

    def request(self, ctx: "Ctx", raw: dict[str, Any]) -> dict[str, Any]:
        now = self.state.clock()
        aid = new_id("apr")
        solo = self.state.auth.count_super() <= 1
        run_after = None
        state = "pending"
        decided_by = None
        if solo:
            state, decided_by = "approved", "timer"
            run_after = now + SOLO_WAIT_S + float(ctx.op.meta.get("wait_after_approve_s", 0))
        self.state.vault.set(f"approval.{aid}", json.dumps(raw, ensure_ascii=False))
        self.db.exec("INSERT INTO approvals(id, op, params, target, requested_by, reason, state, created_at, expires_at, decided_by, decided_at,"
                     " run_after) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (aid, ctx.op.name, dumps(scrub(raw)), ctx.audit_target, ctx.actor_id, str(raw.get("reason") or "")[:500], state, now,
                      now + TTL_S, decided_by, now if solo else None, run_after))
        if solo:
            from .alerts import raise_alert

            raise_alert(self.state, "approval", aid, "approval_solo", "warn")
        return {"approval_id": aid, "state": state, "solo": solo, "run_after": run_after}

    def get(self, aid: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM approvals WHERE id=?", (aid,))
        if r:
            r["params"] = loads(r["params"], {})
        return r

    def list(self, *, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self.expire()
        q = "SELECT * FROM approvals" + (" WHERE state=?" if state else "") + " ORDER BY created_at DESC LIMIT ?"
        rows = self.db.all(q, ((state,) if state else ()) + (limit,))
        for r in rows:
            r["params"] = loads(r["params"], {})
        return rows

    def expire(self) -> int:
        return self.db.exec("UPDATE approvals SET state='expired' WHERE state='pending' AND expires_at<?", (self.state.clock(),))

    def decide(self, ctx: "Ctx", aid: str, approve: bool) -> dict[str, Any]:
        from .ops import OpError, REGISTRY

        self.expire()
        r = self.get(aid)
        if not r:
            raise OpError("not_found", "không có yêu cầu")
        if r["state"] != "pending":
            raise OpError("conflict", f"yêu cầu đang ở trạng thái {r['state']}")
        if r["requested_by"] == ctx.actor_id:
            raise OpError("forbidden", "người yêu cầu không tự duyệt được")
        now = self.state.clock()
        if not approve:
            self.db.exec("UPDATE approvals SET state='rejected', decided_by=?, decided_at=? WHERE id=?", (ctx.actor_id, now, aid))
            self.state.vault.delete(f"approval.{aid}")
            return {"state": "rejected"}
        o = REGISTRY.get(r["op"])
        wait = float(o.meta.get("wait_after_approve_s", 0)) if o else 0.0
        self.db.exec("UPDATE approvals SET state='approved', decided_by=?, decided_at=?, run_after=? WHERE id=?",
                     (ctx.actor_id, now, now + wait, aid))
        out = {"state": "approved", "run_after": now + wait}
        if wait <= 0:
            out["result"] = self._execute(aid)
        return out

    def cancel(self, ctx: "Ctx", aid: str) -> dict[str, Any]:
        from .ops import OpError

        r = self.get(aid)
        if not r:
            raise OpError("not_found", "không có yêu cầu")
        if r["state"] not in ("pending", "approved"):
            raise OpError("conflict", f"yêu cầu đang ở trạng thái {r['state']}")
        if ctx.actor_id not in (r["requested_by"], r["decided_by"]) and ctx.account.get("role") != "super_admin":  # type: ignore[union-attr]
            raise OpError("forbidden", "không huỷ được yêu cầu này")
        self.db.exec("UPDATE approvals SET state='cancelled', decided_at=? WHERE id=?", (self.state.clock(), aid))
        self.state.vault.delete(f"approval.{aid}")
        return {"state": "cancelled"}

    def _execute(self, aid: str) -> Any:
        from .dispatch import Peer

        r = self.get(aid)
        if not r or r["state"] != "approved":
            return None
        disp = self.state.extra.get("dispatcher")
        acct = self.state.auth.account(r["requested_by"]) if r["requested_by"] else None
        raw = loads(self.state.vault.get(f"approval.{aid}") or "{}", {})
        if disp is None or not acct or acct.get("state") != "active":
            self.db.exec("UPDATE approvals SET state='failed', result=? WHERE id=?", (dumps({"error": "requester_inactive"}), aid))
            return None
        res = disp.handle({"op": r["op"], "params": raw, "req_id": aid}, Peer(uid=0, root=True), approved_by=r["decided_by"], run_as=acct)
        self.db.exec("UPDATE approvals SET state=?, result=? WHERE id=?", ("done" if res.get("ok") else "failed", dumps(scrub(res)), aid))
        self.state.vault.delete(f"approval.{aid}")
        return res

    def run_due(self) -> int:
        self.expire()
        n = 0
        for r in self.db.all("SELECT id FROM approvals WHERE state='approved' AND run_after IS NOT NULL AND run_after<=?",
                             (self.state.clock(),)):
            self._execute(r["id"])
            n += 1
        return n

