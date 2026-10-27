"""Bảng method JSON-RPC của luồng B (namespace `approval.*`, `guard.*`, `notify.*`, `run.continue|resume`, `agent.purge`).

Chữ ký (MCP server của luồng A gọi `approval.list|approve|deny` với các tham số này):
- `approval.list {status?: "open"|"pending"|"waiting_human"|"allowed"|"denied"|"cancelled"|"all", run_id?, agent_id?, limit?}`
  → list. `open` và `pending` đều = mọi request CHƯA quyết (open + waiting_human) — đúng nghĩa `open_requests`.
- `approval.get {id}`
  Mỗi approval trả về (list/get, cả `tui.snapshot`) có `always_offer`: None hoặc `{kind exact|web_domain, agent_id, tool,
  fingerprint|domain, summary}` — tính bằng CÙNG hàm với `always_prepare` (`GuardService.always_offer`); TUI/web/Telegram chỉ hiện
  nút "Luôn cho phép" khi khác None (bấm vẫn kiểm lại ở `approval.approve{always}`).
- `approval.approve|deny {id | request_id, note?, channel? (vd "mcp:<client>"), actor?|by?}` → approval sau khi quyết.
  `approval.approve {…, always: true}` → thêm luật "luôn cho phép" hẹp (chỉ verdict uncertain đủ điều kiện; destructive → lỗi,
  không quyết gì); kết quả có `always_rule`. `always_confirm` (tuỳ chọn; MCP `hub_approve` luôn gửi) = fingerprint|domain của
  `always_offer` — khác luật sẽ tạo → lỗi, không quyết gì. Gỡ: `guard.protect.rm {id: "aa_…"}` (chỉ người); xem: `guard.policy` → `always_allow`.
- `approval.stats {days?}` → thống kê theo luật/agent/tool/kết quả/người quyết + thời gian chờ (`cfharness approvals stats`).
- `approval.profile` → profile + khoá hiệu lực (`guard/profiles.resolved`), chỉ đọc.
- `approval.manager_decide {id, decision: allow|deny|escalate, reason}` — CHỈ điều phối viên (HUMAN_ONLY + MANAGER_ALLOWED; luật ở
  `manager/permissions.check_rpc` + `ApprovalService.manager_decide`): approval `routed_to=manager`, lớp được giao, không phải approval
  của run điều phối viên, không bao giờ `always`. Người dùng `approval.approve|deny` như cũ (quyết được bất kỳ lúc nào).
- `guard.check` (hook/hub-guard), `guard.test {command|tool+input, cwd?, run_id?}`, `guard.policy`,
  `guard.protect.list|add|rm`, `notify.test {text?}`, `run.continue {id, prompt}`, `run.resume {id, prompt?}`,
  `agent.purge {id, confirm}` (confirm phải bằng id — người gõ lại).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon


def _id(p: dict[str, Any]) -> str:
    v = p.get("id") or p.get("request_id")
    if not v:
        raise HubError("thiếu tham số 'id'")
    return str(v)


def _repo_deny_add(hub, p: dict[str, Any]) -> dict[str, Any]:
    repo = p.get("repo")
    pattern = p.get("pattern")
    if not repo or not pattern:
        raise HubError("cần 'repo' và 'pattern'")
    try:
        res = hub.guard.policy_store.repo_deny_add(str(repo), str(pattern))
    except ValueError as e:
        raise HubError(str(e)) from None
    try:
        from ..approvals import audit_record

        audit_record("guard.repo_deny_add", agent_id=None, run_id=None, data={"repo": repo, "pattern": pattern})
    except Exception:  # pragma: no cover
        pass
    return res


def _repo_deny_rm(hub, p: dict[str, Any]) -> dict[str, Any]:
    repo = p.get("repo")
    if not repo:
        raise HubError("cần 'repo'")
    try:
        return hub.guard.policy_store.repo_deny_rm(str(repo), p.get("pattern"))
    except KeyError:
        raise HubError(f"không có mẫu chặn cho repo '{repo}'", code=-32004) from None


def register_rpc(d: "Daemon") -> None:
    hub = d.hub

    def approval_list(p: dict[str, Any]) -> list[dict[str, Any]]:
        st = p.get("status") or "pending"
        if st == "open":
            st = "pending"
        return hub.guard.annotate_always(hub.approvals.list(st, run_id=p.get("run_id"), agent_id=p.get("agent_id"), limit=int(p.get("limit") or 100)))

    def decide(decision: str):
        def _f(p: dict[str, Any]) -> dict[str, Any]:
            by, channel = str(p.get("by") or p.get("actor") or "user"), str(p.get("channel") or "cli")
            # `always` (chỉ approve): kiểm điều kiện TRƯỚC khi quyết (destructive/forbidden/vùng hub/SSRF → lỗi, không quyết gì),
            # quyết như thường, rồi ghi luật hẹp vào policy/always_allow.yaml (có history; gỡ bằng `guard always rm`)
            spec = hub.guard.always_prepare(_id(p)) if decision == "allowed" and p.get("always") is True else None
            if spec is not None and p.get("always_confirm") is not None:
                # MCP hub_approve: client phải nhắc lại đúng fingerprint/host của luật sẽ tạo (từ `always_offer`) — gọi mù → lỗi, không quyết gì
                from .service import always_confirm_value

                want = always_confirm_value(spec)
                if not want or str(p["always_confirm"]).strip() != want:
                    raise HubError("always_confirm không khớp luật sẽ tạo — đọc always_offer (fingerprint hoặc domain) của approval rồi nhắc lại "
                                   "đúng giá trị; approval vẫn đang chờ")  # cố ý KHÔNG trả giá trị đúng trong lỗi (thử lại mù sẽ không tự khớp)
            res = hub.approvals.resolve(_id(p), decision, by=by, channel=channel, note=p.get("note"))
            if spec is not None:
                res = {**res, "always_rule": hub.guard.always_commit(spec, by=by, channel=channel)}
            return res

        return _f

    def manager_decide(p: dict[str, Any]) -> dict[str, Any]:
        from ..auth import current_caller
        from ..errors import Blocked

        c = current_caller()
        if c is None or not c.manager:
            raise Blocked("approval.manager_decide chỉ dành cho điều phối viên — người dùng approval.approve|deny")
        if p.get("always"):
            raise Blocked("điều phối viên không bao giờ tạo luật luôn cho phép")
        return hub.approvals.manager_decide(_id(p), str(p.get("decision") or ""), manager_id=str(c.manager), reason=str(p.get("reason") or ""))

    def approval_stats(p: dict[str, Any]) -> dict[str, Any]:
        from ..approvals import approval_stats as _stats

        try:
            days = float(p.get("days") or 30)
        except (TypeError, ValueError):
            raise HubError("days phải là số") from None
        return _stats(hub, max(0.01, min(days, 3650)))

    def approval_profile(p: dict[str, Any]) -> dict[str, Any]:
        from .profiles import resolved

        return resolved(hub.config)

    def run_continue(p: dict[str, Any]) -> dict[str, Any]:
        prompt = p.get("prompt")
        if not prompt:
            raise HubError("thiếu tham số 'prompt'")
        return hub.runs.continue_run(_id(p), str(prompt), budget_usd=p.get("budget_usd"))

    def run_resume(p: dict[str, Any]) -> dict[str, Any]:
        from ..runs import RESUME_INTERRUPTED_PROMPT

        return hub.runs.continue_run(_id(p), str(p.get("prompt") or RESUME_INTERRUPTED_PROMPT), reason="manual_resume")

    def protect_rm(p: dict[str, Any]) -> dict[str, Any]:
        try:
            return hub.guard.protect_rm(p)
        except KeyError:
            raise HubError(f"không có tài nguyên bảo vệ '{p.get('id')}'", code=-32004) from None
        except ValueError as e:
            raise HubError(str(e)) from None

    def protect_add(p: dict[str, Any]) -> dict[str, Any]:
        try:
            return hub.guard.protect_add(p)
        except ValueError as e:
            raise HubError(str(e)) from None

    def agent_purge(p: dict[str, Any]) -> dict[str, Any]:
        from ..recovery import purge_agent

        aid = _id(p)
        if p.get("confirm") != aid:
            raise HubError(f"purge là thao tác phá huỷ — xác nhận bằng cách gõ lại id: --confirm {aid}")
        return purge_agent(hub, aid, by=str(p.get("by") or "user"), channel=str(p.get("channel") or "cli"))

    d.methods.update(
        {
            "approval.list": approval_list,
            "approval.get": lambda p: hub.guard.annotate_always(hub.approvals.get(_id(p))),
            "approval.approve": decide("allowed"),
            "approval.deny": decide("denied"),
            "approval.manager_decide": manager_decide,
            "approval.stats": approval_stats,
            "approval.profile": approval_profile,
            "guard.check": hub.guard.check,
            "guard.test": hub.guard.test,
            "guard.policy": lambda p: hub.guard.policy(),
            "guard.protect.list": lambda p: hub.guard.policy()["resources"],
            "guard.protect.add": protect_add,
            "guard.protect.rm": protect_rm,
            "guard.decisions": lambda p: [
                dict(r) for r in hub.store.conn.execute(
                    "SELECT * FROM guard_decisions" + (" WHERE run_id=?" if p.get("run_id") else "") + " ORDER BY id DESC LIMIT ?",
                    ((p["run_id"],) if p.get("run_id") else ()) + (int(p.get("limit") or 100),),
                )
            ],
            "notify.test": lambda p: hub.notifier.send_now(str(p.get("text") or ":white_check_mark: cfharness: thử thông báo Slack")),
            "run.continue": run_continue,
            "run.resume": run_resume,
            "agent.purge": agent_purge,
            # SEC: ngoại lệ sandbox theo agent — set chỉ người (auth.HUMAN_ONLY + cổng service), đọc tự do
            "guard.sandbox.list": lambda p: hub.guard.sandbox_overrides(),
            "guard.sandbox.get": lambda p: hub.guard.sandbox_overrides(_id(p)),
            "guard.sandbox.set": lambda p: hub.guard.sandbox_set(
                _id(p), allow_localhost=bool(p.get("allow_localhost")), by=str(p.get("by") or "user"), channel=str(p.get("channel") or "cli")),
            "guard.sandbox.allow_domain": lambda p: hub.guard.sandbox_allow_domain(
                _id(p), str(p.get("domain") or ""), remove=bool(p.get("remove")), by=str(p.get("by") or "user"), channel=str(p.get("channel") or "cli")),
            "guard.sandbox.dev_host": lambda p: hub.guard.sandbox_dev_host(
                _id(p), str(p.get("host") or ""), remove=bool(p.get("remove")), by=str(p.get("by") or "user"), channel=str(p.get("channel") or "cli")),
            # trust hoàn toàn theo agent — set chỉ người (auth.HUMAN_ONLY + cổng service), đọc tự do
            "guard.trust.list": lambda p: hub.guard.trust_all(),
            "guard.trust.set": lambda p: hub.guard.trust_set(
                _id(p), str(p.get("mode") or ""), for_s=p.get("for_s"), by=str(p.get("by") or "user"), channel=str(p.get("channel") or "cli")),
            # MGR-2: mẫu chặn theo repo (người đặt, áp cho mọi agent) — set chỉ người, đọc tự do
            "guard.repo_deny.list": lambda p: hub.guard.policy_store.repo_deny_list(),
            "guard.repo_deny.add": lambda p: _repo_deny_add(hub, p),
            "guard.repo_deny.rm": lambda p: _repo_deny_rm(hub, p),
        }
    )
