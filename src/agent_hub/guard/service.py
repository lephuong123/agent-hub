"""`GuardService` (trong daemon): điểm quyết định cho hook PreToolUse và permission tool (FR-SG-03/04, FR-RT-09).

`check()` — RPC `guard.check`:
1. tìm run (không thấy/không chạy → deny, fail-closed);
2. phân loại (`classifier.classify`) với ngữ cảnh run (cwd, mốc "file run tự tạo" = lúc chuỗi session bắt đầu,
   chính sách ở `HUB_HOME/policy/`, annotations MCP từ registry của luồng A);
3. safe/write → allow (+ audit với write); forbidden (tự duyệt, đụng socket hub) → deny + `guard.blocked`;
4. destructive/uncertain → vé một lần còn hạn? → allow; không thì mở ApprovalRequest và GIỮ tới
   `approval.timeout_s`; người cho phép → allow (hoặc dừng & resume nếu luật deny tĩnh chặn thao tác); người từ chối
   → deny kèm ghi chú; hết giờ → `waiting_human`, deny với thông điệp chờ, hub dừng tiến trình gọn.
Mỗi quyết định ghi `guard_decisions` (append-only) + event `guard.decision` (debug).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..approvals import action_preview, audit_record
from ..errors import HubError
from ..events import make_event
from ..redact import redact
from ..store import now_iso
from .classifier import GuardContext, Verdict, action_fingerprint, classify
from .policy import PolicyStore
from .runtime import pattern_matches

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("agent_hub.guard")

WAITING_MSG = (
    "[agent-hub] This action is destructive and is held for human approval (request {aid}). "
    "Nobody answered within {timeout}s, so the hub is pausing this session now and will resume it after a human decides. "
    "Do NOT retry this action and do NOT try to achieve it another way. End your turn now with a one-line status."
)
DENIED_MSG = "[agent-hub] A human DENIED this action (request {aid}).{note} Do not retry it or work around it; continue another way or report what remains."
FORBIDDEN_MSG = "[agent-hub] Blocked by hub safety policy: {reason}. Agents may not approve their own requests or change safety policy."
RESTART_MSG = (
    "[agent-hub] A human APPROVED this action (request {aid}), but a static safety rule blocks it in this process. "
    "The hub will restart this session with a one-time permission. End your turn now."
)


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_hub_mcp_config(cfg: dict[str, Any], port: int = 8765) -> bool:
    import re as _re
    from urllib.parse import urlparse

    cmd = " ".join([str(cfg.get("command") or "")] + [str(a) for a in (cfg.get("args") or [])])
    if _re.search(r"agent[-_]hub(\.exe)?\b.*\bmcp\b.*\bserve\b", cmd) or "agent_hub.mcp_server" in cmd and "hub_self" not in cmd:
        return True
    url = str(cfg.get("url") or "")
    if url:
        try:
            u = urlparse(url)
            host = (u.hostname or "").lower()
            if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0") and (u.port or (443 if u.scheme == "https" else 80)) == port:
                return True
        except ValueError:
            return False
    return False


class GuardService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.policy_store = PolicyStore(hub.paths.home)
        try:
            self.policy_store.ensure()
        except OSError:  # pragma: no cover
            log.exception("không tạo được policy/")

    # ------------------------------------------------------------------ context
    def enabled(self) -> bool:
        return bool(self.hub.config.get("guard.enabled"))

    def _annotations(self, server: str, tool: str) -> dict[str, Any] | None:
        reg = getattr(self.hub, "mcp", None)
        if reg is not None and hasattr(reg, "tool_annotations"):
            return reg.tool_annotations(server, tool)
        try:
            from ..mcp_client import tool_annotations

            return tool_annotations(server, tool, home=self.hub.paths.home)
        except Exception:
            return None

    def session_since(self, run: dict[str, Any]) -> float | None:
        """Mốc "file run tự tạo": lúc run ĐẦU TIÊN của session vào hàng đợi (chuỗi resume dùng chung mốc)."""
        sid = run.get("session_id")
        if sid:
            r = self.hub.store.conn.execute("SELECT MIN(queued_at) FROM runs WHERE session_id=?", (sid,)).fetchone()
            if r and r[0]:
                return _ts(r[0])
        return _ts(run.get("queued_at"))

    def context_for(self, run: dict[str, Any] | None, cwd: str | None = None) -> GuardContext:
        c = cwd or (run or {}).get("cwd") or os.getcwd()
        return GuardContext(
            cwd=Path(c),
            policy=self.policy_store.load(),
            run_since=self.session_since(run) if run else None,
            mcp_annotations=self._annotations,
            hub_socket=str(self.hub.paths.socket),
            git_probe=bool(self.hub.config.get("guard.git_probe")),
            hub_mcp_servers=self.hub_mcp_servers(),
            checkpoint_root=((run or {}).get("checkpoint") or {}).get("root") if ((run or {}).get("checkpoint") or {}).get("kind") == "git" else None,
            manager_run=str(((run or {}).get("run_opts") or {}).get("hub_self") or "") == "manager",
            repo_deny=tuple(self.repo_deny_patterns(c)),
            repo_deny_lookup=self.repo_deny_patterns,
            extra_write_roots=tuple(os.path.realpath(os.path.expanduser(str(x))) for x in ((self.hub.config.get("guard.sandbox") or {}).get("extra_allow_write") or [])),
        )

    def repo_deny_patterns(self, cwd: str | None) -> list[str]:
        """MGR-2: mẫu chặn theo repo do NGƯỜI đặt (policy/repo_deny.yaml) áp cho mọi run có cwd trong repo."""
        if not cwd:
            return []
        try:
            from .policy import repo_deny_for

            return repo_deny_for(self.policy_store, cwd)
        except Exception:  # pragma: no cover
            return []

    # ------------------------------------------------------------------ ngoại lệ sandbox THEO AGENT (chỉ người đặt)
    SANDBOX_OVERRIDE_KEYS = ("allow_localhost", "allow_domains")

    def sandbox_overrides(self, agent_id: str | None = None) -> dict[str, Any]:
        """`hub_state.sandbox_overrides` = {agent_id: {allow_localhost: bool, by, at}}. Lưu NGOÀI cấu hình agent nên vòng học/ledger,
        export/clone/revert không chạm được; chỉ RPC `guard.sandbox.set` (HUMAN_ONLY) đổi."""
        allv = self.hub.store.state_get("sandbox_overrides") or {}
        if agent_id is None:
            return {k: v for k, v in allv.items() if any(v.get(x) for x in self.SANDBOX_OVERRIDE_KEYS)}
        return dict(allv.get(agent_id) or {})

    def run_sandbox_overrides(self, agent_id: str) -> dict[str, Any]:
        """Ngoại lệ dùng khi dựng settings của run: ngoại lệ người đặt + host của MCP server gán cho agent (R3-1)."""
        out = self.sandbox_overrides(agent_id)
        out["mcp_domains"] = self.agent_mcp_domains(agent_id)
        return out

    def agent_mcp_domains(self, agent_id: str) -> list[str]:
        from urllib.parse import urlparse

        try:
            agent = self.hub.store.get_agent(agent_id) or {}
            names = list(agent.get("mcp_servers") or [])
        except Exception:  # pragma: no cover
            return []
        out: list[str] = []
        reg = getattr(self.hub, "mcp", None)
        for n in names:
            try:
                row = reg.get_row(n) if reg is not None else None
            except Exception:
                row = None
            url = str(((row or {}).get("config") or {}).get("url") or "")
            if url:
                h = (urlparse(url).hostname or "").lower()
                if h:
                    out.append(h)
        return list(dict.fromkeys(out))

    def sandbox_allow_domain(self, agent_id: str, domain: str, *, remove: bool = False, by: str = "user", channel: str = "cli") -> dict[str, Any]:
        """CHỈ người: mở/gỡ một domain mạng cho Bash của agent (R3-1). Audit + event, như ngoại lệ localhost."""
        import re as _re

        from ..auth import require_human

        require_human("mở domain mạng cho agent")
        self.hub.agents.get_row(agent_id)
        d = domain.strip().lower()
        if not _re.fullmatch(r"(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)*(:\d+)?|\*", d):
            raise HubError(f"domain không hợp lệ: {domain!r}")
        allv = dict(self.hub.store.state_get("sandbox_overrides") or {})
        cur = dict(allv.get(agent_id) or {})
        doms = [x for x in (cur.get("allow_domains") or []) if x != d]
        if not remove:
            doms.append(d)
        cur.update({"allow_domains": doms, "by": by, "channel": channel, "at": now_iso()})
        allv[agent_id] = cur
        self.hub.store.state_set("sandbox_overrides", allv)
        info = {"agent_id": agent_id, "domain": d, "action": "remove" if remove else "allow", "allow_domains": doms, "by": by, "channel": channel}
        audit_record("guard.sandbox_override", agent_id=agent_id, run_id=None, data=info)
        self.hub.publish(make_event("guard.sandbox_override", agent_id, info, source="hub"))
        return {"agent_id": agent_id, **cur}

    def sandbox_set(self, agent_id: str, *, allow_localhost: bool, by: str = "user", channel: str = "cli") -> dict[str, Any]:
        from ..auth import require_human

        require_human("đặt ngoại lệ sandbox")  # cổng tầng service (ngoài cổng RPC)
        self.hub.agents.get_row(agent_id)  # NotFound nếu không có
        allv = dict(self.hub.store.state_get("sandbox_overrides") or {})
        before = dict(allv.get(agent_id) or {})
        cur = {**before, "allow_localhost": bool(allow_localhost), "by": by, "channel": channel, "at": now_iso()}
        allv[agent_id] = cur
        self.hub.store.state_set("sandbox_overrides", allv)
        info = {"agent_id": agent_id, "before": {k: before.get(k) for k in self.SANDBOX_OVERRIDE_KEYS}, "after": {"allow_localhost": cur["allow_localhost"]},
                "by": by, "channel": channel}
        audit_record("guard.sandbox_override", agent_id=agent_id, run_id=None, data=info)
        self.hub.publish(make_event("guard.sandbox_override", agent_id, info, source="hub"))
        return {"agent_id": agent_id, **cur}

    def hub_mcp_servers(self) -> frozenset[str]:
        """H-18: tên đăng ký của MCP server trỏ về CHÍNH hub — nhận theo lệnh (`agent-hub mcp serve`, `-m agent_hub.mcp_server`)
        hoặc URL loopback cổng `mcp_server.http_port`, không theo tên."""
        reg = getattr(self.hub, "mcp", None)
        if reg is None or not hasattr(reg, "list"):
            return frozenset()
        try:
            port = int(self.hub.config.get("mcp_server.http_port") or 8765)
        except (KeyError, TypeError, ValueError):
            port = 8765
        out: set[str] = set()
        try:
            rows = reg.list(include_deleted=False)
        except Exception:
            return frozenset()
        for r in rows:
            if is_hub_mcp_config(r.get("config") or {}, port):
                out.add(str(r.get("name")))
        return frozenset(out)

    # ------------------------------------------------------------------ record
    def _record(self, run: dict[str, Any] | None, channel: str, tool_name: str, tool_input: dict[str, Any], v: Verdict, action: str, approval_id: str | None = None) -> None:
        inp = json.dumps(redact(tool_input), ensure_ascii=False, default=str)[:4000]
        self.hub.store.conn.execute(
            "INSERT INTO guard_decisions (ts, run_id, agent_id, channel, tool_name, input_redacted, matched_rule, classifier, verdict, action, reason, approval_request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), (run or {}).get("id"), (run or {}).get("agent_id"), channel, tool_name, inp, v.rule, "static", v.verdict, action, v.reason[:500], approval_id),
        )
        self.hub.publish(
            make_event(
                "guard.decision",
                (run or {}).get("agent_id"),
                {"tool": tool_name, "verdict": v.verdict, "matched_rule": v.rule, "classifier": "static", "action": action, "channel": channel, "approval_id": approval_id},
                run_id=(run or {}).get("id"),
                source="hook",
            )
        )
        if v.verdict != "safe" or action != "allow":
            audit_record(
                f"guard.{action}",
                (run or {}).get("agent_id"),
                (run or {}).get("id"),
                {"tool": tool_name, "input": tool_input, "verdict": v.to_dict(), "channel": channel, "approval_id": approval_id, "actor": f"agent:{(run or {}).get('agent_id')}"},
            )

    # ------------------------------------------------------------------ check
    async def check(self, p: dict[str, Any]) -> dict[str, Any]:
        run_id = str(p.get("run_id") or "")
        channel = str(p.get("channel") or "hook")
        tool_name = str(p.get("tool_name") or "")
        tool_input = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        run = self.hub.store.get_run(run_id) if run_id else None
        if run is None or run["status"] not in ("running", "queued"):
            v = Verdict("uncertain", "unknown_run", None, f"run '{run_id}' không đang chạy")
            self._record(run, channel, tool_name, tool_input, v, "deny")
            return {"decision": "deny", "reason": "[agent-hub] guard: run is not active — blocked (fail-closed)"}
        if self.hub.runs.estop.get("on"):
            v = Verdict("uncertain", "estop", None, "ESTOP đang bật")
            self._record(run, channel, tool_name, tool_input, v, "deny")
            return {"decision": "deny", "reason": "[agent-hub] Emergency stop is on — all tool calls are blocked. End your turn."}
        ctx = self.context_for(run, p.get("cwd") if channel == "hook" and p.get("cwd") else None)
        v = classify(tool_name, tool_input, ctx)
        if v.forbidden:
            self._record(run, channel, tool_name, tool_input, v, "deny")
            self.hub.publish(
                make_event("guard.blocked", run["agent_id"], {"source": "runtime", "reason": "guard_rule", "rule": v.rule, "detail": v.reason, "tool": tool_name, "preview": action_preview(tool_name, tool_input)}, run_id=run_id)
            )
            return {"decision": "deny", "reason": FORBIDDEN_MSG.format(reason=v.reason)}
        if not v.hold:
            self._record(run, channel, tool_name, tool_input, v, "allow")
            return {"decision": "allow", "reason": f"hub guard: {v.verdict}", "verdict": v.verdict}
        fp = action_fingerprint(tool_name, tool_input)
        ap = self.hub.approvals
        ticket = ap.consume_ticket(run.get("session_id"), fp, run_id)
        if ticket is not None:
            self._record(run, channel, tool_name, tool_input, v, "allow", ticket["id"])
            audit_record("approval.ticket_used", run["agent_id"], run_id, {"approval_id": ticket["id"], "action_fingerprint": fp, "tool": tool_name})
            return {"decision": "allow", "reason": f"[agent-hub] approved once by a human (request {ticket['id']})", "approval_id": ticket["id"]}
        row = ap.find_pending(run_id, fp) or ap.open(run, tool_name=tool_name, tool_input=tool_input, tool_use_id=p.get("tool_use_id"), fingerprint=fp, verdict=v.to_dict(), channel=channel)
        aid = row["id"]
        self._record(run, channel, tool_name, tool_input, v, "hold", aid)
        timeout = float(self.hub.config.get("approval.timeout_s"))
        max_wait = p.get("max_wait_s")
        if isinstance(max_wait, (int, float)) and max_wait > 0:
            timeout = min(timeout, float(max_wait))
        if row["status"] == "waiting_human":
            timeout = 0.0
        outcome = await ap.hold(aid, timeout)
        if outcome == "allowed":
            cur = self.hub.store.get_run(run_id) or run
            blockers = [pat for pat in (self.hub.runs.static_deny_for(run_id)) if pattern_matches(pat, tool_name, tool_input)]
            if blockers:
                self.hub.runs.pause_for_human(run_id, aid, restart=True)
                return {"decision": "deny", "reason": RESTART_MSG.format(aid=aid), "approval_id": aid}
            ap.consume_ticket(cur.get("session_id"), fp, run_id)
            self._record(run, channel, tool_name, tool_input, v, "allow", aid)
            return {"decision": "allow", "reason": f"[agent-hub] approved by a human (request {aid})", "approval_id": aid}
        if outcome == "denied":
            note = ap.get(aid).get("note")
            self._record(run, channel, tool_name, tool_input, v, "deny", aid)
            return {"decision": "deny", "reason": DENIED_MSG.format(aid=aid, note=f" Note from the human: {note}." if note else ""), "approval_id": aid}
        if outcome == "cancelled":
            self._record(run, channel, tool_name, tool_input, v, "deny", aid)
            return {"decision": "deny", "reason": f"[agent-hub] Request {aid} was cancelled. Do not retry; end your turn.", "approval_id": aid}
        # hết giờ giữ → waiting_human (KHÔNG tự từ chối: request vẫn chờ người)
        ap.to_waiting_human(aid)
        self.hub.runs.pause_for_human(run_id, aid)
        return {"decision": "deny", "reason": WAITING_MSG.format(aid=aid, timeout=int(timeout)), "approval_id": aid, "waiting_human": True}

    # ------------------------------------------------------------------ helpers cho CLI/RPC
    def test(self, p: dict[str, Any]) -> dict[str, Any]:
        tool = str(p.get("tool") or "Bash")
        ti = p.get("input") if isinstance(p.get("input"), dict) else {"command": str(p.get("command") or "")}
        run = self.hub.store.find_run(p["run_id"]) if p.get("run_id") else None
        ctx = self.context_for(run, p.get("cwd"))
        v = classify(tool, ti, ctx)
        return {**v.to_dict(), "hold": v.hold, "fingerprint": action_fingerprint(tool, ti), "cwd": str(ctx.cwd)}

    def policy(self) -> dict[str, Any]:
        return self.policy_store.load().to_dict()

    def protect_add(self, p: dict[str, Any]) -> dict[str, Any]:
        ops = p.get("ops")
        if isinstance(ops, str):
            ops = [o.strip() for o in ops.split(",") if o.strip()]
        r = self.policy_store.add(str(p.get("kind") or ""), str(p.get("selector") or ""), ops, str(p.get("note") or ""))
        self._policy_changed("protect.add", r.to_dict(), p)
        return r.to_dict()

    def protect_rm(self, p: dict[str, Any]) -> dict[str, Any]:
        r = self.policy_store.remove(str(p.get("id") or ""))
        self._policy_changed("protect.rm", r.to_dict(), p)
        return r.to_dict()

    def _policy_changed(self, what: str, data: dict[str, Any], p: dict[str, Any]) -> None:
        ver = self.policy_store.load().version
        by, channel = p.get("by") or "user", p.get("channel") or "cli"
        self.hub.publish(make_event("policy.changed", None, {"version": ver, "by": by, "change": what, "resource": data}))
        audit_record("policy.changed", None, None, {"change": what, "resource": data, "version": ver, "actor": by, "channel": channel})
