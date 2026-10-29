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

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..approvals import action_preview, audit_record
from ..errors import HubError
from ..events import make_event
from ..redact import redact
from ..store import now_iso
from .classifier import GuardContext, Verdict, action_fingerprint, always_eligible, classify
from .policy import PolicyStore
from .runtime import pattern_matches

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.guard")

WAITING_MSG = (
    "[cfharness] This action is destructive and is held for human approval (request {aid}). "
    "Nobody answered within {timeout}s, so the hub is pausing this session now and will resume it after a human decides. "
    "Do NOT retry this action and do NOT try to achieve it another way. End your turn now with a one-line status."
)
DENIED_MSG = "[cfharness] {who} DENIED this action (request {aid}).{note} Do not retry it or work around it; continue another way or report what remains."
CONTINUE_MSG = (
    "[cfharness] Nobody approved this destructive action (request {aid}) within {timeout}s, so it is DENIED for now and your run continues. "
    "Do it another way that is not destructive, or skip it and clearly report in your final summary what was skipped and why. "
    "Do NOT retry the same action now. The request stays open: if a human approves it later, the hub resumes this session so you can run it once."
)
NET_CONTINUE_MSG = (
    "[cfharness] Nobody approved this network connection (request {aid}) within {timeout}s, so it is DENIED for now and your run continues. "
    "Do not retry the same host now; work without it, or clearly report in your final summary what could not be fetched and why."
)
FORBIDDEN_MSG = "[cfharness] Blocked by hub safety policy: {reason}. Agents may not approve their own requests or change safety policy."
RESTART_MSG = (
    "[cfharness] A human APPROVED this action (request {aid}), but a static safety rule blocks it in this process. "
    "The hub will restart this session with a one-time permission. End your turn now."
)


def _same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    from .policy import fold_path

    return fold_path(os.path.realpath(a)).rstrip("/") == fold_path(os.path.realpath(b)).rstrip("/")


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_MCP_TOOL_RE = re.compile(r"mcp__[A-Za-z0-9_.-]{1,80}__[A-Za-z0-9_.-]{1,120}")
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")


def always_summary(offer: dict[str, Any]) -> str:
    """Câu mô tả luật "luôn cho phép" sẽ tạo (chữ hub; host/agent là dữ liệu — mỗi bề mặt tự escape khi vẽ)."""
    if offer.get("kind") == "web_domain":
        return (f"luật web_domain: agent {offer.get('agent_id')} được WebFetch tới ĐÚNG host {offer.get('domain')} "
                "(không gồm tên miền con, không wildcard) mà không hỏi lại")
    if offer.get("kind") == "mcp_tool":
        return (f"luật mcp_tool: agent {offer.get('agent_id')} được gọi ĐÚNG tool {offer.get('tool')} (mọi tham số) mà không hỏi lại — "
                "chỉ bỏ cờ destructiveHint/chưa probe/untrusted do server tự khai; tool tên xoá, tài nguyên bảo vệ, require_approval vẫn hỏi")
    return (f"luật exact: agent {offer.get('agent_id')} + tool {offer.get('tool')} + ĐÚNG dấu vân tay thao tác "
            f"{offer.get('fingerprint')} (lệnh khác dù một ký tự vẫn hỏi lại)")


def always_confirm_value(spec: dict[str, Any]) -> str:
    """Giá trị client phải nhắc lại khi duyệt `always` (fingerprint | domain | tên tool MCP)."""
    if spec.get("kind") == "mcp_tool":
        return str(spec.get("tool") or "")
    return str(spec.get("fingerprint") or spec.get("domain") or "")


def is_hub_mcp_config(cfg: dict[str, Any], port: int = 8765) -> bool:
    import re as _re
    from urllib.parse import urlparse

    cmd = " ".join([str(cfg.get("command") or "")] + [str(a) for a in (cfg.get("args") or [])])
    from .. import names as _names

    cli, mod = _names.alt(_names.HUB_CLI_NAMES), _names.alt(_names.HUB_MODULE_NAMES)  # tên mới + tên cũ
    if _re.search(rf"{cli}(\.exe)?\b.*\bmcp\b.*\bserve\b", cmd) or (_re.search(rf"{mod}\.mcp_server", cmd) and "hub_self" not in cmd):
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
        from .second_opinion import SecondOpinion

        self.second = SecondOpinion(hub)
        hub.bus.add_listener(self._trust_new_agent)
        from .locks import BrowserLock

        self.browser = BrowserLock(hub.store)  # Claude in Chrome: Chrome THẬT của người dùng chung → giới hạn số run cùng lúc

    # ------------------------------------------------------------------ context
    def enabled(self) -> bool:
        return bool(self.hub.config.get("guard.enabled"))

    def _annotations(self, server: str, tool: str) -> dict[str, Any] | None:
        from ..browser import CHROME_SERVER

        if server == CHROME_SERVER:  # server dynamic của CLI (không có trong registry): ghi đè của người ở runtime.claude.chrome.annotations
            return self._chrome_annotations(tool)
        reg = getattr(self.hub, "mcp", None)
        if reg is not None and hasattr(reg, "tool_annotations"):
            return reg.tool_annotations(server, tool)
        try:
            from ..mcp_client import tool_annotations

            return tool_annotations(server, tool, home=self.hub.paths.home)
        except Exception:
            return None

    def _chrome_annotations(self, tool: str) -> dict[str, Any] | None:
        import fnmatch

        from ..browser import chrome_cfg

        ov: dict[str, Any] = {}
        for pat, val in sorted((chrome_cfg(self.hub.config).get("annotations") or {}).items()):
            if isinstance(val, dict) and fnmatch.fnmatchcase(tool, str(pat)):
                ov.update({k: v for k, v in val.items() if k in ("destructiveHint", "readOnlyHint") and isinstance(v, bool)})
        return {"human_override": ov} if ov else None

    def chrome_allowed(self, run: dict[str, Any] | None) -> bool | None:
        """Run có được dùng tool Claude in Chrome không: agent `tool_policy.browser` ∈ chrome|auto và kill switch bật. Không run → None."""
        if not run:
            return None
        from ..browser import CHROME_MODES, effective_mode

        try:
            agent = self.hub.store.get_agent(run.get("agent_id")) if run.get("agent_id") else None
        except Exception:  # lỗi đọc = không cho (fail-closed)
            return False
        return effective_mode(agent, self.hub.config) in CHROME_MODES

    def session_since(self, run: dict[str, Any]) -> float | None:
        """Mốc "file run tự tạo": lúc run ĐẦU TIÊN của session vào hàng đợi (chuỗi resume dùng chung mốc)."""
        sid = run.get("session_id")
        if sid:
            r = self.hub.store.conn.execute("SELECT MIN(queued_at) FROM runs WHERE session_id=?", (sid,)).fetchone()
            if r and r[0]:
                return _ts(r[0])
        return _ts(run.get("queued_at"))

    def task_since(self, run: dict[str, Any]) -> float | None:
        """Mốc "worktree do task tạo": lúc run ĐẦU của task vào hàng đợi (run không có task → mốc session)."""
        tid = run.get("task_id")
        if tid:
            r = self.hub.store.conn.execute("SELECT MIN(queued_at) FROM runs WHERE task_id=?", (tid,)).fetchone()
            if r and r[0]:
                return _ts(r[0])
        return self.session_since(run)

    def run_extra_write_roots(self, run: dict[str, Any] | None, roots: tuple[str, ...]) -> list[str]:
        """Gốc ghi thêm của task (`workspace.extra_write_roots`, đã kiểm lúc tạo task) — kiểm LẠI: chỉ nhận trong workspace.allowed_roots."""
        from .classifier import _in_roots

        out = []
        for x in ((run or {}).get("run_opts") or {}).get("extra_write_roots") or []:
            try:
                rp = os.path.realpath(os.path.expanduser(str(x)))
            except (OSError, ValueError):
                continue
            if _in_roots(rp, roots):
                out.append(rp)
        return out

    def knob(self, key: str) -> Any:
        from . import profiles

        return profiles.knob(self.hub.config, key)

    def context_for(self, run: dict[str, Any] | None, cwd: str | None = None) -> GuardContext:
        from . import profiles

        c = cwd or (run or {}).get("cwd") or os.getcwd()
        roots = tuple(profiles.allowed_roots(self.hub))
        hh = os.path.realpath(str(self.hub.paths.home / "agents"))
        try:
            rc = os.path.realpath(c)
            rel = os.path.relpath(rc, hh).replace("\\", "/").split("/") if (rc + os.sep).lower().startswith((hh + os.sep).lower()) else []
        except (OSError, ValueError):
            rel = []
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
            extra_write_roots=tuple(os.path.realpath(os.path.expanduser(str(x))) for x in ((self.hub.config.get("guard.sandbox") or {}).get("extra_allow_write") or []))
            + tuple(self.run_extra_write_roots(run, roots)),
            agent_id=(run or {}).get("agent_id"),
            always_allow=tuple(r for r in self.always_rules() if r.get("kind") in ("exact", "mcp_tool")) if run else (),
            safe_reset_probe=bool(self.knob("guard.safe_reset_probe")),
            own_worktree_writes=bool(self.knob("guard.own_worktree_writes")),
            allowed_roots=roots,
            worktree_since=self.task_since(run) if run else None,
            scratch_workspace=len(rel) >= 3 and rel[1] == "workspaces" and not rel[2].startswith("conv-"),
            chrome_allowed=self.chrome_allowed(run),
            trusted_repo_scripts=bool(self.knob("guard.trusted_repo_scripts")),
            task_created_deletes=bool(self.knob("guard.task_created_deletes")),
            **self._connector_ctx(run),
        )

    def _connector_ctx(self, run: dict[str, Any] | None) -> dict[str, Any]:
        """Run điều phối viên có connector claude.ai → {connectors, run_mcp_servers} cho classify_mcp; còn lại {}."""
        if not run or str((run.get("run_opts") or {}).get("hub_self") or "") != "manager":
            return {}
        mgr = getattr(self.hub, "manager", None)
        if mgr is None:
            return {}
        try:
            conns = tuple(mgr.perms.capabilities().get("connectors") or ())
            agent = self.hub.store.get_agent(run.get("agent_id")) or {} if run.get("agent_id") else {}
        except Exception:  # lỗi đọc cấu hình = coi như run connector KHÔNG cho connector nào (fail-closed)
            return {"connectors": (), "run_mcp_servers": frozenset()}
        if not conns:
            return {}
        return {"connectors": conns, "run_mcp_servers": frozenset(str(x) for x in agent.get("mcp_servers") or [])}

    # ------------------------------------------------------------------ luật "luôn cho phép" (người tạo khi duyệt `--always`)
    def always_rules(self) -> list[dict[str, Any]]:
        try:
            return self.policy_store.always_list()
        except Exception:  # pragma: no cover — đọc lỗi = không luật nào (giữ chờ người)
            return []

    def always_domain(self, agent_id: str | None, host: str) -> dict[str, Any] | None:
        h = (host or "").lower().rstrip(".")
        for r in self.always_rules():
            if r.get("kind") == "web_domain" and r.get("domain") == h and (not r.get("agent_id") or r.get("agent_id") == agent_id):
                return r
        return None

    def always_prepare(self, approval_id: str) -> dict[str, Any]:
        """Kiểm approval có được "luôn cho phép" không (TRƯỚC khi quyết) → bản luật (chưa ghi). Sai → HubError, không đổi gì.
        Destructive / forbidden / tài nguyên bảo vệ / vùng hub / luật SSRF / MCP người đánh dấu → KHÔNG BAO GIỜ."""
        from ..auth import require_human

        require_human("tạo luật luôn cho phép")
        return self._always_spec(self.hub.approvals.get(approval_id))

    def always_offer(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Approval có được hiện nút "Luôn cho phép" không — CÙNG kiểm tra với `always_prepare` (không lặp logic ở client).
        → `{kind, agent_id, tool, fingerprint|domain, summary}` (summary = câu tiếng Việt mô tả luật sẽ tạo) hoặc None.
        Chỉ đọc (không cần người) — TUI/web/Telegram dùng để quyết hiện nút; khi bấm, `approval.approve{always}` kiểm lại."""
        try:
            spec = self._always_spec(row)
        except (HubError, KeyError, TypeError, ValueError):
            return None
        out = {k: spec[k] for k in ("kind", "agent_id", "tool", "fingerprint", "domain") if k in spec}
        out["confirm"] = always_confirm_value(spec)  # giá trị `always_confirm` (fingerprint | domain | tên tool)
        out["summary"] = always_summary(out)
        return out

    def annotate_always(self, rows: list[dict[str, Any]] | dict[str, Any]) -> Any:
        """Gắn `always_offer` vào (các) approval trả cho client (approval.list|get, tui.snapshot)."""
        for r in [rows] if isinstance(rows, dict) else rows:
            if isinstance(r, dict) and "id" in r:
                r["always_offer"] = self.always_offer(r)
        return rows

    def _always_spec(self, row: dict[str, Any]) -> dict[str, Any]:
        from urllib.parse import urlsplit

        from .classifier import always_eligible

        if row.get("status") not in ("open", "waiting_human"):
            raise HubError(f"approval {row['id']} đã {row.get('status')} — không tạo được luật luôn cho phép")
        if (row.get("kind") or "destructive") != "destructive" or not row.get("tool_name") or not row.get("action_fingerprint"):
            raise HubError("chỉ approval của guard (thao tác tool) mới có lựa chọn luôn cho phép")
        risk = str(row.get("risk_level") or "")
        rule = str(row.get("matched_rule") or "")
        from .classifier import mcp_tool_always_eligible

        mv = Verdict(risk, rule, row.get("destructive_class"), str(row.get("reason") or ""), [], row.get("protected_resource_id"))
        if mcp_tool_always_eligible(str(row.get("tool_name") or ""), mv) and _MCP_TOOL_RE.fullmatch(str(row["tool_name"])):
            # "luôn cho phép tool MCP này cho agent X": chỉ verdict do annotations của server (không phải tên xoá/bảo vệ/require_approval)
            return {"agent_id": row["agent_id"], "tool": row["tool_name"], "approval_id": row["id"], "from_rule": rule,
                    "preview": str(row.get("preview") or "")[:300], "kind": "mcp_tool"}
        if risk != "uncertain":
            raise HubError(f"thao tác mức '{risk or '?'}' (phá huỷ) không thể 'luôn cho phép' — chỉ duyệt MỘT lần (bỏ --always)")
        ti = row.get("tool_input") if isinstance(row.get("tool_input"), dict) else {}
        base = {"agent_id": row["agent_id"], "tool": row["tool_name"], "approval_id": row["id"], "from_rule": rule,
                "preview": str(row.get("preview") or "")[:300]}
        if rule == "web_domain_not_allowed" and row["tool_name"] == "WebFetch":
            try:
                host = (urlsplit(str(ti.get("url") or "")).hostname or "").lower().rstrip(".")
            except ValueError:
                host = ""
            if not host or len(host) > 253 or not _HOST_RE.fullmatch(host):  # chỉ host DNS/IPv4 thuần (chữ hiện ở nút/xác nhận)
                raise HubError("không đọc được host hợp lệ của URL — duyệt một lần")
            return {**base, "kind": "web_domain", "domain": host}
        v = Verdict(risk, rule, row.get("destructive_class"), str(row.get("reason") or ""), [], row.get("protected_resource_id"))
        if not always_eligible(v):
            raise HubError(f"luật '{rule}' (nhóm {row.get('destructive_class') or '-'}) không cho 'luôn cho phép' — chỉ duyệt một lần")
        return {**base, "kind": "exact", "fingerprint": row["action_fingerprint"]}

    def always_commit(self, spec: dict[str, Any], *, by: str = "user", channel: str = "cli") -> dict[str, Any]:
        from ..auth import require_human

        require_human("tạo luật luôn cho phép")
        r = self.policy_store.always_add({**spec, "by": by, "channel": channel})
        self._rebaseline()
        self._policy_changed("always.add", r, {"by": by, "channel": channel})
        return r

    def always_rm(self, rid: str, *, by: str = "user", channel: str = "cli") -> dict[str, Any]:
        from ..auth import require_human

        require_human("gỡ luật luôn cho phép")
        r = self.policy_store.always_rm(rid, by=by)
        self._rebaseline()
        self._policy_changed("always.rm", r, {"by": by, "channel": channel})
        return r

    def _rebaseline(self) -> None:
        """Ghi hợp lệ vào policy/ của daemon → chụp lại bản chuẩn integrity (nếu không sẽ bị khôi phục như tamper)."""
        mon = getattr(self.hub, "integrity", None)
        if mon is not None:
            try:
                mon.rebaseline("daemon_write")
            except Exception:  # pragma: no cover
                log.exception("không chụp lại được bản chuẩn sau khi ghi always_allow")

    # ------------------------------------------------------------------ WEB-1: mạng (SSRF/DNS/domain) — trong daemon
    resolver: Any = None  # test gán hàm giả (host → [ip…]); None = socket.getaddrinfo

    async def net_check(self, run: dict[str, Any] | None, tool_name: str, tool_input: dict[str, Any], v: Verdict, ctx: GuardContext,
                        *, dns: bool = True) -> Verdict:
        """Sau `classify`: WebFetch (và URL curl/wget của Bash) — danh sách domain người đặt, phân giải DNS (mọi IP phải công khai),
        allowlist (cấu hình hoặc `manager.web_domains=allowlist`). Chỉ NÂNG mức (không bao giờ hạ)."""
        from .classifier import SEVERITY
        from .url_safety import domain_matches, resolve_check, static_check

        def _up(a: Verdict, b: Verdict) -> Verdict:  # hoà mức → lấy luật mạng (không để luật hạ-được-bằng-LLM che luật domain)
            return a if a.forbidden or SEVERITY[b.verdict] < SEVERITY[a.verdict] else b

        if v.forbidden:
            return v
        from ..browser import is_chrome_tool

        chrome = is_chrome_tool(tool_name)  # Claude in Chrome navigate/tabs_create (+ trong browser_batch): CÙNG luật WebFetch
        web = tool_name == "WebFetch" or chrome
        urls = ([str(tool_input.get("url") or "")] if tool_name == "WebFetch" else list(v.net_urls or []) if tool_name == "Bash" or chrome else [])
        label = "Chrome" if chrome else "WebFetch"
        if not urls:
            return v
        wcfg = self.hub.config.get("guard.web")
        wcfg = wcfg if isinstance(wcfg, dict) else {}
        deny = [str(x) for x in (wcfg.get("deny_domains") or []) if isinstance(x, str)]
        allow = [str(x) for x in (wcfg.get("allow_domains") or []) if isinstance(x, str)]
        mgr = ctx.manager_run
        mode = str(self.hub.config.get("manager.web_domains") or "any") if mgr else "any"
        if mgr and mode not in ("any", "allowlist"):
            mode = "allowlist"  # giá trị lạ (sửa tay) → hẹp nhất
        try:
            dns_timeout = min(30.0, max(0.5, float(wcfg.get("dns_timeout_s") or 3)))
        except (TypeError, ValueError):
            dns_timeout = 3.0
        agent_id = (run or {}).get("agent_id")
        out = v
        for url in urls[:20]:
            st = static_check(url)
            if not st.ok or not st.host:
                continue  # WebFetch: classify đã xử lý; Bash: sandbox mạng + luật loopback của classifier
            host = st.host
            pat = domain_matches(host, deny)
            if pat:
                return Verdict("destructive", "web_domain_denied", "network", f"{tool_name}: domain {host} bị người chặn (guard.web.deny_domains: {pat})",
                               [url[:200]], forbidden=True, net_urls=v.net_urls)
            if dns and wcfg.get("resolve_dns", True) is not False and st.ips == ():
                rc = await asyncio.to_thread(resolve_check, host, resolver=self.resolver, timeout_s=dns_timeout)
                if not rc.ok:
                    if rc.metadata:
                        return Verdict("destructive", "ssrf_metadata", "network", f"{tool_name}: {rc.reason}", [url[:200]], forbidden=True, net_urls=v.net_urls)
                    if rc.rule == "ssrf_private_resolved" and (web or mgr):
                        if not mgr and self.dev_host_trusted(agent_id, host):
                            continue  # host dev người đã tin cho agent này (vd tên miền dev trỏ về máy local qua hosts)
                        if mgr:
                            return Verdict("destructive", rc.rule, "network", f"điều phối viên không được gọi mạng nội bộ: {rc.reason}", [url[:200]],
                                           forbidden=True, net_urls=v.net_urls)
                        out = _up(out, Verdict("uncertain", rc.rule, "network", f"{label}: {rc.reason} (SSRF)", [url[:200]]))
                        continue
                    if rc.rule == "url_dns_failed" and web:
                        out = _up(out, Verdict("uncertain", rc.rule, "network", f"{label}: {rc.reason}", [url[:200]]))
                        continue
            if web and (allow or (mgr and mode == "allowlist")) and not domain_matches(host, allow) and not self.always_domain(agent_id, host):
                why = "manager.web_domains=allowlist" if mgr and mode == "allowlist" else "guard.web.allow_domains"
                out = _up(out, Verdict("uncertain", "web_domain_not_allowed", "network", f"{label}: {host} ngoài danh sách domain cho phép ({why})",
                                        [url[:200]]))
        return out

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
    SANDBOX_OVERRIDE_KEYS = ("allow_localhost", "allow_domains", "dev_hosts")

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

    # ------------------------------------------------------------------ host dev tin cậy THEO AGENT (chỉ người đặt)
    def dev_host_trusted(self, agent_id: str | None, host: str) -> bool:
        """Host (đúng tên, không wildcard) người đã đánh dấu là máy dev cho agent: phân giải về IP nội bộ vẫn cho WebFetch/Chrome."""
        if not agent_id or not host:
            return False
        h = host.lower().rstrip(".")
        return h in [str(x).lower() for x in (self.sandbox_overrides(agent_id).get("dev_hosts") or [])]

    def sandbox_dev_host(self, agent_id: str, host: str, *, remove: bool = False, by: str = "user", channel: str = "cli") -> dict[str, Any]:
        """CHỈ người: tin một host dev cho agent — guard không giữ chờ `ssrf_private_resolved` cho ĐÚNG host này (WebFetch, Claude in
        Chrome) và Bash được ra mạng tới host đó (thêm vào allow_domains). Audit + event như ngoại lệ sandbox khác."""
        from ..auth import require_human

        require_human("tin host dev cho agent")
        self.hub.agents.get_row(agent_id)
        h = host.strip().lower().rstrip(".")
        if h.startswith(("http://", "https://")):
            from urllib.parse import urlsplit

            try:
                h = (urlsplit(h).hostname or "").lower()
            except ValueError:
                h = ""
        if not h or len(h) > 253 or not _HOST_RE.fullmatch(h):
            raise HubError(f"host không hợp lệ: {host!r} (đúng một tên host, không wildcard, không cổng)")
        allv = dict(self.hub.store.state_get("sandbox_overrides") or {})
        cur = dict(allv.get(agent_id) or {})
        hosts = [x for x in (cur.get("dev_hosts") or []) if x != h]
        doms = [x for x in (cur.get("allow_domains") or []) if x != h]
        if not remove:
            hosts.append(h)
            doms.append(h)
        cur.update({"dev_hosts": hosts, "allow_domains": doms, "by": by, "channel": channel, "at": now_iso()})
        allv[agent_id] = cur
        self.hub.store.state_set("sandbox_overrides", allv)
        info = {"agent_id": agent_id, "dev_host": h, "action": "remove" if remove else "allow", "dev_hosts": hosts, "by": by, "channel": channel}
        audit_record("guard.sandbox_override", agent_id=agent_id, run_id=None, data=info)
        self.hub.publish(make_event("guard.sandbox_override", agent_id, info, source="hub"))
        return {"agent_id": agent_id, **cur}

    # ------------------------------------------------------------------ trust hoàn toàn THEO AGENT (chỉ người đặt)
    TRUST_MODES = ("full", "off")

    def trust_all(self) -> dict[str, Any]:
        """`hub_state.agent_trust` = {agent_id: {mode, until, by, channel, at}} — NGOÀI cấu hình agent (vòng học/ledger/export/clone/
        điều phối viên không chạm được); chỉ RPC `guard.trust.set` (HUMAN_ONLY) đổi. Chỉ trả mục còn hiệu lực."""
        out: dict[str, Any] = {}
        now = datetime.now().astimezone().timestamp()
        for aid, t in (self.hub.store.state_get("agent_trust") or {}).items():
            if not isinstance(t, dict) or t.get("mode") != "full":
                continue
            if t.get("until"):
                until = _ts(t.get("until"))
                if until is None or until <= now:
                    continue
            out[aid] = dict(t)
        return out

    def trust_of(self, agent_id: str | None) -> dict[str, Any] | None:
        if not agent_id:
            return None
        try:
            return self.trust_all().get(agent_id)
        except Exception:  # lỗi đọc = không trust (giữ guard như thường)
            return None

    def run_trusted(self, run: dict[str, Any] | None) -> dict[str, Any] | None:
        """Run được hưởng trust full: agent đang trust và run là run làm việc thường (không phải review/eval/learning của hub)."""
        if not run:
            return None
        if self.run_trusted_kind_excluded(run):
            return None
        return self.trust_of(run.get("agent_id"))

    def trust_set(self, agent_id: str, mode: str, *, for_s: float | None = None, by: str = "user", channel: str = "cli") -> dict[str, Any]:
        """CHỈ người: `full` = hub cho phép MỌI tool call của agent (không giữ chờ, kể cả forbidden/vùng hub; run mới không sandbox OS,
        không permissions.deny tĩnh, `bypassPermissions` kể cả trên repo thật) — vẫn ghi guard_decisions/audit, ESTOP vẫn chặn.
        `off` = về như thường. Phần guard có hiệu lực ngay; sandbox/cờ CLI chốt lúc spawn (run đang chạy giữ như cũ)."""
        from ..auth import require_human

        require_human("đặt trust cho agent")
        if mode not in self.TRUST_MODES:
            raise HubError(f"trust phải là {'|'.join(self.TRUST_MODES)}")
        self.hub.agents.get_row(agent_id)
        until = None
        if for_s is not None and mode == "full":
            try:
                secs = float(for_s)
            except (TypeError, ValueError):
                raise HubError("thời hạn trust không hợp lệ") from None
            if secs <= 0:
                raise HubError("thời hạn trust phải > 0")
            from datetime import timedelta, timezone

            until = (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat(timespec="seconds").replace("+00:00", "Z")
        allv = dict(self.hub.store.state_get("agent_trust") or {})
        before = dict(allv.get(agent_id) or {})
        if mode == "off":
            allv.pop(agent_id, None)
            cur: dict[str, Any] = {"mode": "off"}
        else:
            cur = {"mode": "full", "until": until, "by": by, "channel": channel, "at": now_iso()}
            allv[agent_id] = cur
        self.hub.store.state_set("agent_trust", allv)
        info = {"agent_id": agent_id, "before": before.get("mode") or "off", "after": mode, "until": until, "by": by, "channel": channel}
        audit_record("guard.trust", agent_id=agent_id, run_id=None, data=info)
        self.hub.publish(make_event("guard.trust", agent_id, info, source="hub"))
        return {"agent_id": agent_id, **cur}

    # ------------------------------------------------------------------ cho phép THEO AGENT + LUẬT (chỉ người, xin một lần)
    def rule_grants(self) -> dict[str, dict[str, Any]]:
        """`hub_state.agent_rule_grants` = {agent_id: {rule: {until, by, channel, at, approval_id}}} — hẹp hơn trust full: chỉ bỏ
        hỏi duyệt cho ĐÚNG luật đó của ĐÚNG agent đó (kể cả destructive). Chỉ trả mục còn hiệu lực. Ngoài cấu hình agent như trust."""
        out: dict[str, dict[str, Any]] = {}
        now = datetime.now().astimezone().timestamp()
        for aid, rules in (self.hub.store.state_get("agent_rule_grants") or {}).items():
            if not isinstance(rules, dict):
                continue
            for rule, g in rules.items():
                if not isinstance(g, dict):
                    continue
                if g.get("until"):
                    until = _ts(g.get("until"))
                    if until is None or until <= now:
                        continue
                out.setdefault(aid, {})[rule] = dict(g)
        return out

    def run_rule_grant(self, run: dict[str, Any] | None, v: Verdict) -> dict[str, Any] | None:
        """Grant áp cho run làm việc thường (cùng loại trừ với trust: review/eval/learning của hub); lỗi đọc = không grant."""
        if not run or not v.rule or v.forbidden or self.run_trusted_kind_excluded(run):
            return None
        try:
            return (self.rule_grants().get(str(run.get("agent_id") or "")) or {}).get(v.rule)
        except Exception:
            return None

    @staticmethod
    def run_trusted_kind_excluded(run: dict[str, Any]) -> bool:
        opts = run.get("run_opts") or {}
        return bool(run.get("kind") == "review" or opts.get("learning_review") or opts.get("eval_overlay"))

    def rule_grant_set(self, agent_id: str, rule: str, *, for_s: float | None = None, by: str = "user", channel: str = "cli",
                       approval_id: str | None = None) -> dict[str, Any]:
        """CHỈ người: từ nay agent không bị hỏi duyệt lại cho luật `rule` (không hết hạn nếu `for_s` None). Mọi approval CHƯA quyết
        cùng agent + luật được cho phép luôn. forbidden/ESTOP/khoá Chrome vẫn áp; vẫn ghi guard_decisions + audit."""
        from ..auth import require_human

        require_human("cho phép agent theo luật")
        rule = str(rule or "").strip()
        if not rule:
            raise HubError("thiếu luật")
        self.hub.agents.get_row(agent_id)
        until = None
        if for_s is not None:
            try:
                secs = float(for_s)
            except (TypeError, ValueError):
                raise HubError("thời hạn không hợp lệ") from None
            if secs <= 0:
                raise HubError("thời hạn phải > 0")
            from datetime import timedelta, timezone

            until = (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat(timespec="seconds").replace("+00:00", "Z")
        allv = dict(self.hub.store.state_get("agent_rule_grants") or {})
        cur = {"until": until, "by": by, "channel": channel, "at": now_iso(), "approval_id": approval_id}
        allv[agent_id] = {**dict(allv.get(agent_id) or {}), rule: cur}
        self.hub.store.state_set("agent_rule_grants", allv)
        info = {"agent_id": agent_id, "rule": rule, "action": "grant", "until": until, "by": by, "channel": channel, "approval_id": approval_id}
        audit_record("guard.rule_grant", agent_id=agent_id, run_id=None, data=info)
        self.hub.publish(make_event("guard.rule_grant", agent_id, info, source="hub"))
        released = []
        for row in self.hub.approvals.list("pending", agent_id=agent_id, limit=500):
            if row.get("matched_rule") == rule and row["id"] != approval_id:
                try:
                    self.hub.approvals.resolve(row["id"], "allowed", by=by, channel=channel, note=f"luôn cho phép {agent_id} theo luật {rule}")
                    released.append(row["id"])
                except Exception:  # noqa: BLE001 — approval vừa được quyết ở nơi khác
                    log.warning("không cho phép được %s theo grant", row["id"])
        return {"agent_id": agent_id, "rule": rule, **cur, "released": released}

    def rule_grant_rm(self, agent_id: str, rule: str | None = None, *, by: str = "user", channel: str = "cli") -> dict[str, Any]:
        """CHỈ người: gỡ grant một luật (hoặc mọi luật nếu `rule` None) của agent."""
        from ..auth import require_human

        require_human("gỡ cho phép agent theo luật")
        allv = dict(self.hub.store.state_get("agent_rule_grants") or {})
        cur = dict(allv.get(agent_id) or {})
        removed = list(cur) if rule is None else ([rule] if rule in cur else [])
        if not removed:
            raise HubError(f"agent '{agent_id}' không có cho phép theo luật {rule or ''}".rstrip(), code=-32004)
        for r in removed:
            cur.pop(r, None)
        if cur:
            allv[agent_id] = cur
        else:
            allv.pop(agent_id, None)
        self.hub.store.state_set("agent_rule_grants", allv)
        info = {"agent_id": agent_id, "rules": removed, "action": "remove", "by": by, "channel": channel}
        audit_record("guard.rule_grant", agent_id=agent_id, run_id=None, data=info)
        self.hub.publish(make_event("guard.rule_grant", agent_id, info, source="hub"))
        return {"agent_id": agent_id, "removed": removed}

    def trust_set_all(self, mode: str, *, for_s: float | None = None, by: str = "user", channel: str = "cli") -> dict[str, Any]:
        """CHỈ người: `trust_set` cho MỌI agent đang có (không tính agent đã lưu trữ) — mỗi agent một audit/event `guard.trust`.
        Agent tạo sau lệnh này không được phủ: dùng `guard.trust_new_agents`."""
        from ..auth import require_human

        require_human("đặt trust cho mọi agent")
        if mode not in self.TRUST_MODES:
            raise HubError(f"trust phải là {'|'.join(self.TRUST_MODES)}")
        rows = [self.trust_set(str(a["id"]), mode, for_s=for_s, by=by, channel=channel) for a in self.hub.agents.list()]
        return {"mode": mode, "count": len(rows), "until": rows[0].get("until") if rows else None, "agents": [r["agent_id"] for r in rows]}

    def _trust_new_agent(self, ev: Any) -> None:
        """Listener `agent.created`: `guard.trust_new_agents=full` (người đặt trong config) → agent mới nhận trust full không hết hạn,
        ghi `by=default, channel=config` + audit/event `guard.trust` như `trust_set`. Tắt cho từng agent bằng `guard trust <agent> off`."""
        if ev.type != "agent.created" or not ev.agent_id:
            return
        try:
            if self.hub.config.get("guard.trust_new_agents") != "full":
                return
            allv = dict(self.hub.store.state_get("agent_trust") or {})
            if ev.agent_id in allv:
                return
            allv[ev.agent_id] = {"mode": "full", "until": None, "by": "default", "channel": "config", "at": now_iso()}
            self.hub.store.state_set("agent_trust", allv)
        except Exception:  # lỗi = không trust (giữ guard như thường)
            log.exception("không đặt được trust mặc định cho %s", ev.agent_id)
            return
        info = {"agent_id": ev.agent_id, "before": "off", "after": "full", "until": None, "by": "default", "channel": "config"}
        audit_record("guard.trust", agent_id=ev.agent_id, run_id=None, data=info)
        self.hub.publish(make_event("guard.trust", ev.agent_id, info, source="hub"))

    def hub_mcp_servers(self) -> frozenset[str]:
        """H-18: tên đăng ký của MCP server trỏ về CHÍNH hub — nhận theo lệnh (`cfharness mcp serve`, `-m cfharness.mcp_server`)
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
    def _record(self, run: dict[str, Any] | None, channel: str, tool_name: str, tool_input: dict[str, Any], v: Verdict, action: str, approval_id: str | None = None,
                classifier: str = "static", extra: dict[str, Any] | None = None) -> None:
        inp = json.dumps(redact(tool_input), ensure_ascii=False, default=str)[:4000]
        self.hub.store.conn.execute(
            "INSERT INTO guard_decisions (ts, run_id, agent_id, channel, tool_name, input_redacted, matched_rule, classifier, verdict, action, reason, approval_request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), (run or {}).get("id"), (run or {}).get("agent_id"), channel, tool_name, inp, v.rule, classifier, v.verdict, action, v.reason[:500], approval_id),
        )
        self.hub.publish(
            make_event(
                "guard.decision",
                (run or {}).get("agent_id"),
                {"tool": tool_name, "verdict": v.verdict, "matched_rule": v.rule, "classifier": classifier, "action": action, "channel": channel, "approval_id": approval_id},
                run_id=(run or {}).get("id"),
                source="hook",
            )
        )
        if v.verdict != "safe" or action != "allow":
            audit_record(
                f"guard.{action}",
                (run or {}).get("agent_id"),
                (run or {}).get("id"),
                {"tool": tool_name, "input": tool_input, "verdict": v.to_dict(), "channel": channel, "approval_id": approval_id, "actor": f"agent:{(run or {}).get('agent_id')}",
                 "decided_by": classifier if classifier != "static" else ("human" if approval_id else "static"), **(extra or {})},
            )

    # ------------------------------------------------------------------ Claude in Chrome: khoá trình duyệt
    def chrome_lock_check(self, run: dict[str, Any], tool_name: str) -> dict[str, str] | None:
        """Tool chrome (trừ `list_connected_browsers`): lấy khoá trình duyệt cho run (≤ runtime.claude.chrome.max_concurrent_runs run).
        None = được dùng; dict {reason, message} = đang bận (deny kèm hướng dẫn)."""
        from ..browser import chrome_cfg, effective_mode, is_chrome_tool

        if not is_chrome_tool(tool_name) or tool_name.endswith("__list_connected_browsers"):
            return None
        holders = self.browser.acquire(run["id"], run.get("agent_id"), chrome_cfg(self.hub.config)["max_concurrent_runs"], self._run_active)
        if holders is None:
            return None
        who = ", ".join(f"{h.get('agent_id') or '?'} (run {rid})" for rid, h in holders.items()) or "?"
        agent = self.hub.store.get_agent(run.get("agent_id")) if run.get("agent_id") else None
        fallback = (" Your browser setting is 'auto': use the Playwright MCP tools for this step instead."
                    if effective_mode(agent, self.hub.config) == "auto" else
                    " Wait and retry later (at most a few times), or finish the rest of the task and report that the browser was busy.")
        return {"reason": f"Chrome đang được run khác dùng: {who} (runtime.claude.chrome.max_concurrent_runs)",
                "message": f"[cfharness] Claude in Chrome is busy: another cfharness run is using the owner's Chrome ({who}).{fallback}"}

    def _run_active(self, run_id: str) -> bool:
        r = self.hub.store.get_run(run_id)
        return bool(r and r["status"] in ("running", "queued"))

    # ------------------------------------------------------------------ check
    # uncertain luôn giữ dù `hold_uncertain=false`: vùng hub/policy (sandbox là lớp 2 nhưng đây là tài nguyên của chính hub) và
    # tool MCP người đánh dấu cần duyệt
    # WEB-1: luật mạng (SSRF/DNS/ngoài allowlist) cũng luôn giữ — chính sách mạng của người, không để `hold_uncertain=false` mở
    ALWAYS_HOLD_KLASS = ("hub", "policy", "network")
    # approval-profiles: tool MCP chưa probe / server untrusted cũng luôn giữ — MCP chạy NGOÀI sandbox OS nên "cho chạy + audit" của
    # uncertain (dựa vào sandbox) không áp được (FR-MC-03); người gỡ bằng `mcp probe`, `mcp annotate` hoặc luật mcp_tool
    # Claude in Chrome: `chrome_sensitive` (javascript_tool/file_upload/upload_image trên Chrome THẬT của người) luôn giữ — guard/chrome.py
    ALWAYS_HOLD_RULES = ("mcp_require_approval", "web_domain_not_allowed", "ssrf_private_resolved", "url_dns_failed", "mcp_unknown_tool", "mcp_untrusted_write",
                         "chrome_sensitive")

    def needs_human(self, v: Verdict) -> bool:
        from .profiles import never_automatic

        if not v.hold:
            return False
        if v.verdict != "uncertain" or self.knob("guard.hold_uncertain") is not False:
            return True
        # uncertain + không giữ (balanced/autonomous hoặc người đặt false): vẫn giữ vùng hub/policy/mạng, MCP require_approval và
        # mọi thứ trong danh sách "không bao giờ tự động" (guard/profiles.never_automatic)
        return v.klass in self.ALWAYS_HOLD_KLASS or v.rule in self.ALWAYS_HOLD_RULES or never_automatic(v) is not None

    # ------------------------------------------------------------------ hồ sơ phê duyệt: tự cho phép thao tác phá huỷ revert được
    CHECKPOINT_RULES = ("delete_not_run_created", "overwrite_existing", "git_discard_changes", "git_reset_hard")
    SCRATCH_KLASS = ("file", "git")

    def auto_allow(self, run: dict[str, Any], tool_name: str, tool_input: dict[str, Any], v: Verdict, ctx: GuardContext) -> dict[str, Any] | None:
        """Thao tác `destructive` hub TỰ cho phép (không hỏi người) theo profile — None = theo luồng approval như cũ.
        Luôn qua `never_automatic` trước (đích ngoài workspace.allowed_roots, push, credential, vùng hub, bảo vệ… → None)."""
        from .profiles import never_automatic

        if v.verdict != "destructive" or v.forbidden or self.hub.runs.estop.get("on"):
            return None
        if never_automatic(v, tool_name=tool_name, tool_input=tool_input, roots=list(ctx.allowed_roots), cwd=str(ctx.cwd)) is not None:
            return None
        if self.knob("approval.checkpoint_auto_allow") and v.rule in self.CHECKPOINT_RULES:
            why = self.checkpoint_revertible(run, v)
            if why:
                return {"by": "checkpoint", "reason": why}
        if self.knob("approval.scratch_auto_allow") and (v.klass or "") in self.SCRATCH_KLASS:
            why = self.scratch_confined(run, v, ctx)
            if why:
                return {"by": "autonomous_scratch", "reason": why}
        return None

    def _load_checkpoint(self, run: dict[str, Any]) -> dict[str, Any] | None:
        meta = run.get("checkpoint") or {}
        if meta.get("kind") != "git" or meta.get("truncated"):
            return None
        try:
            cp = json.loads((self.hub.runs.checkpoint_dir(run["id"]) / "checkpoint.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if cp.get("kind") != "git" or cp.get("truncated") or not cp.get("root"):
            return None
        return cp

    def checkpoint_revertible(self, run: dict[str, Any], v: Verdict) -> str | None:
        """Mọi đích của thao tác nằm trong repo có checkpoint của CHÍNH run này và `cfharness run revert` khôi phục được trạng thái
        trước run (file track ở HEAD lúc checkpoint, hoặc có bản lưu trong checkpoint, hoặc do run tạo sau checkpoint). Lỗi → None."""
        from ..workspace import _git, git_root

        cp = self._load_checkpoint(run)
        if cp is None:
            return None
        root = os.path.realpath(cp["root"])
        head = cp.get("head")
        dirty = cp.get("dirty") or {}
        taken = float(cp.get("taken_at") or 0)

        def under(p: str) -> str | None:
            rp = os.path.realpath(p)
            try:
                rel = os.path.relpath(rp, root)
            except ValueError:
                return None
            rel = rel.replace("\\", "/")
            if rel == "." or rel.startswith("../") or rel == ".." or rel.split("/")[0].lower() == ".git" or "/.git/" in f"/{rel}/".lower():
                return None
            return rel

        def file_ok(rel: str, full: str) -> bool:
            if rel in dirty:
                e = dirty[rel] or {}
                return e.get("sha") is None or e.get("blob") is not None
            if head:
                try:
                    if _git(root, "cat-file", "-e", f"{head}:{rel}").returncode == 0:
                        return True
                except (OSError, Exception):  # noqa: BLE001 — probe lỗi = không chứng minh được
                    return False
            try:
                st = os.lstat(full)
            except FileNotFoundError:
                return True  # không tồn tại → không mất gì
            except OSError:
                return False
            from .classifier import _birth

            b = _birth(full, st)
            return b is not None and b >= taken - 1.0  # tạo SAU checkpoint (run tạo) → revert xoá nó

        if v.rule in ("git_reset_hard", "git_discard_changes"):
            tgt = (v.targets or [None])[0]
            if not tgt or git_root(tgt) is None or not _same_path(git_root(tgt), root):
                return None
            try:
                h = _git(root, "rev-parse", "--verify", "-q", "HEAD")
                if h.returncode != 0 or h.stdout.strip() != head:
                    return None  # run đã commit/đổi nhánh → reset có thể mất commit; để người quyết
                st = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no", "--no-renames")
            except Exception:  # noqa: BLE001
                return None
            if st.returncode != 0:
                return None
            for ent in st.stdout.split("\0"):
                if len(ent) > 3 and not file_ok(ent[3:], os.path.join(root, ent[3:])):
                    return None
            return f"mọi thay đổi bị bỏ đều có trong checkpoint của run ({root}) — `cfharness run revert {run['id']}` khôi phục"
        targets = [str(t) for t in v.targets or [] if t]
        if not targets:
            return None
        n = 0
        for t in targets:
            rel = under(t)
            if rel is None:
                return None
            full = os.path.realpath(t)
            if os.path.isdir(full) and not os.path.islink(full):
                for dp, dns, fns in os.walk(full):
                    if any(x.lower() == ".git" for x in dns + fns):
                        return None  # repo/worktree con: không nằm trong checkpoint
                    for fn in fns:
                        n += 1
                        if n > 3000:
                            return None
                        fp = os.path.join(dp, fn)
                        r2 = under(fp)
                        if r2 is None or not file_ok(r2, fp):
                            return None
            elif not file_ok(rel, full):
                return None
        return f"mọi đích nằm trong checkpoint của run ({root}) — `cfharness run revert {run['id']}` khôi phục"

    def scratch_confined(self, run: dict[str, Any], v: Verdict, ctx: GuardContext) -> str | None:
        """autonomous: run scratch/worktree của agent có `tool_policy.allow_bypass` (người đặt — NFR-15 không đổi), mọi đích trong cwd."""
        agent = self.hub.store.get_agent(run["agent_id"]) or {}
        if not ((agent.get("tool_policy") or {}).get("allow_bypass") is True):
            return None
        wk = ((run.get("run_opts") or {}).get("workspace_kind"))
        if not (ctx.scratch_workspace or wk == "worktree"):
            return None
        from .classifier import _under, canon_path

        cw = canon_path(os.path.realpath(str(ctx.cwd)))
        targets = [str(t) for t in v.targets or [] if t]
        if not targets:
            return None
        if any(not (_under(canon_path(os.path.realpath(t)), cw) and _under(canon_path(t), canon_path(str(ctx.cwd)))) for t in targets):
            return None
        return f"run scratch/worktree của agent allow_bypass — mọi đích trong {cw}"

    # ------------------------------------------------------------------ hồ sơ phê duyệt: giao điều phối viên
    def route_for(self, run: dict[str, Any], tool_name: str, tool_input: dict[str, Any], v: Verdict, ctx: GuardContext) -> tuple[str, str | None]:
        """("manager", None) nếu approval giao được cho điều phối viên; ("human", lý do) nếu không."""
        from .profiles import delegable

        why = delegable(v, self.hub.config, tool_name=tool_name, tool_input=tool_input, roots=list(ctx.allowed_roots), cwd=str(ctx.cwd))
        if why:
            return "human", why
        if ctx.manager_run:
            return "human", "approval của chính run điều phối viên"
        mgr = getattr(self.hub, "manager", None)
        mid = mgr.agent_id() if mgr is not None else None
        if not mid:
            return "human", "chưa chỉ định điều phối viên"
        if not self.hub.approvals.manager_ready(run["agent_id"]):
            return "human", "điều phối viên không sẵn sàng (ESTOP/tạm dừng/heartbeat tắt)"
        return "manager", None

    async def sandbox_network(self, run_id: str, host: Any, *, request_id: str | None = None, tool_use_id: str | None = None) -> dict[str, Any]:
        """CLI 2.1.303+: Bash trong sandbox OS mở kết nối tới host ngoài `sandbox.network.allowedDomains` của run → CLI hỏi qua
        `control_request can_use_tool SandboxNetworkAccess {host}` (runtime/claude_cli.py). Quyết định:
        1. host ∈ `guard.web.deny_domains` → deny (forbidden);
        2. host khớp allowlist mạng HIỆN TẠI của agent (cấu hình + domain/localhost người mở + host MCP — người có thể vừa mở sau khi
           run bắt đầu) và không bị luật tĩnh cấm → allow;
        3. còn lại → `check()` như mọi tool (ESTOP, trust, gates, rule grant, approval giữ ≤ `approval.timeout_s`; hết giờ → deny, run
           chạy tiếp). Mọi lỗi → deny (FAIL-CLOSED). Luôn phát `sandbox.network_request`."""
        from .classifier import SANDBOX_NETWORK_TOOL
        from .runtime import network_domains
        from .url_safety import domain_matches, norm_host

        h = norm_host(str(host or ""))[:253]
        tool_input = {"host": h}
        run = self.hub.store.get_run(run_id) if run_id else None
        res: dict[str, Any] | None = None
        try:
            if run is not None and run["status"] in ("running", "queued") and not self.hub.runs.estop.get("on") and h:
                ctx = self.context_for(run)
                wcfg = self.hub.config.get("guard.web")
                wcfg = wcfg if isinstance(wcfg, dict) else {}
                deny = [str(x) for x in (wcfg.get("deny_domains") or []) if isinstance(x, str)]
                pat = domain_matches(h, deny)
                v = classify(SANDBOX_NETWORK_TOOL, tool_input, ctx)
                if pat:
                    v = Verdict("destructive", "web_domain_denied", "network", f"Bash (sandbox): domain {h} bị người chặn (guard.web.deny_domains: {pat})",
                                [h], forbidden=True)
                    self._record(run, "sandbox_network", SANDBOX_NETWORK_TOOL, tool_input, v, "deny")
                    res = {"decision": "deny", "reason": FORBIDDEN_MSG.format(reason=v.reason)}
                elif not v.forbidden:
                    sb_cfg = self.hub.config.get("guard.sandbox")
                    allowed = [str(x).strip("[]") for x in network_domains(sb_cfg if isinstance(sb_cfg, dict) else {}, self.run_sandbox_overrides(run["agent_id"]))]
                    pat = domain_matches(h, allowed)
                    if pat:
                        va = Verdict("safe", "sandbox_network_allowlist", "network", f"host {h} khớp allowlist mạng của agent ({pat})", [h])
                        self._record(run, "sandbox_network", SANDBOX_NETWORK_TOOL, tool_input, va, "allow")
                        res = {"decision": "allow", "reason": f"hub guard: allowlist ({pat})", "decided_by": "allowlist"}
            if res is None:
                res = await self.check({"run_id": run_id, "channel": "sandbox_network", "tool_name": SANDBOX_NETWORK_TOOL, "tool_input": tool_input,
                                        "tool_use_id": tool_use_id})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — FAIL-CLOSED
            log.exception("sandbox_network lỗi")
            res = {"decision": "deny", "reason": f"[cfharness] guard error ({type(e).__name__}) — network access denied (fail-closed)"}
        decision = "allow" if res.get("decision") == "allow" else "deny"
        by = res.get("decided_by") or ("human" if res.get("approval_id") and decision == "allow" else "guard")
        try:
            self.hub.publish(make_event("sandbox.network_request", (run or {}).get("agent_id"), {
                "host": h, "decision": decision, "decided_by": by, "reason": str(res.get("reason") or "")[:300], "approval_id": res.get("approval_id"),
                "request_id": request_id, "continued": bool(res.get("continued")),
            }, run_id=run_id or None, source="hub"))
        except Exception:  # pragma: no cover — sự kiện không được làm hỏng quyết định
            log.exception("không phát được sandbox.network_request")
        return {**res, "decision": decision}

    async def check(self, p: dict[str, Any]) -> dict[str, Any]:
        run_id = str(p.get("run_id") or "")
        channel = str(p.get("channel") or "hook")
        tool_name = str(p.get("tool_name") or "")
        tool_input = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        run = self.hub.store.get_run(run_id) if run_id else None
        if run is None or run["status"] not in ("running", "queued"):
            v = Verdict("uncertain", "unknown_run", None, f"run '{run_id}' không đang chạy")
            self._record(run, channel, tool_name, tool_input, v, "deny")
            return {"decision": "deny", "reason": "[cfharness] guard: run is not active — blocked (fail-closed)"}
        if self.hub.runs.estop.get("on"):
            v = Verdict("uncertain", "estop", None, "ESTOP đang bật")
            self._record(run, channel, tool_name, tool_input, v, "deny")
            return {"decision": "deny", "reason": "[cfharness] Emergency stop is on — all tool calls are blocked. End your turn."}
        ctx = self.context_for(run, p.get("cwd") if channel == "hook" and p.get("cwd") else None)
        trust = self.run_trusted(run)
        if trust is not None:
            return self._allow_trusted(run, channel, tool_name, tool_input, ctx, trust)
        v = classify(tool_name, tool_input, ctx)
        try:
            v = await self.net_check(run, tool_name, tool_input, v, ctx)
        except Exception as e:  # lỗi kiểm mạng → giữ chờ người (fail-closed), không bao giờ cho qua
            log.exception("net_check lỗi")
            if not v.forbidden:
                v = Verdict("uncertain", "url_dns_failed", "network", f"kiểm mạng lỗi: {type(e).__name__}", v.targets)
        if v.forbidden:
            self._record(run, channel, tool_name, tool_input, v, "deny")
            self.hub.publish(
                make_event("guard.blocked", run["agent_id"], {"source": "runtime", "reason": "guard_rule", "rule": v.rule, "detail": v.reason, "tool": tool_name, "preview": action_preview(tool_name, tool_input)}, run_id=run_id)
            )
            return {"decision": "deny", "reason": FORBIDDEN_MSG.format(reason=v.reason)}
        busy = self.chrome_lock_check(run, tool_name)
        if busy is not None:
            vb = Verdict("uncertain", "chrome_busy", "mcp", busy["reason"], [tool_name[:200]])
            self._record(run, channel, tool_name, tool_input, vb, "deny")
            return {"decision": "deny", "reason": busy["message"], "browser_busy": True}
        if not self.needs_human(v):
            self._record(run, channel, tool_name, tool_input, v, "allow")
            return {"decision": "allow", "reason": f"hub guard: {v.verdict}", "verdict": v.verdict}
        from .profiles import gates_mode

        if gates_mode(self.hub.config) == "off":
            # công tắc chung tắt mọi cổng (người đặt): cho phép thứ lẽ ra hỏi; forbidden/ESTOP/khoá Chrome vẫn áp, vẫn ghi quyết định
            self._record(run, channel, tool_name, tool_input, v, "allow", classifier="gates_off", extra={"auto_reason": "gates.mode=off"})
            return {"decision": "allow", "reason": "[cfharness] allowed: the owner turned all approval gates off", "verdict": v.verdict,
                    "decided_by": "gates_off"}
        grant = self.run_rule_grant(run, v)
        if grant is not None:
            # người đã "luôn cho phép agent này theo luật này" — không hỏi lại; vẫn ghi guard_decisions + audit
            why = f"luôn cho phép theo luật {v.rule} (bởi {grant.get('by') or 'user'}" + (f", hết hạn {grant['until']})" if grant.get("until") else ")")
            self._record(run, channel, tool_name, tool_input, v, "allow", classifier="rule_grant", extra={"auto_reason": why})
            return {"decision": "allow", "reason": f"[cfharness] allowed: a human always allows this agent for rule {v.rule}", "verdict": v.verdict,
                    "decided_by": "rule_grant"}
        auto = self.auto_allow(run, tool_name, tool_input, v, ctx)
        if auto is not None:
            # hồ sơ balanced/autonomous: phá huỷ nhưng revert được (checkpoint) / giới hạn trong scratch → cho phép + audit (decided_by)
            self._record(run, channel, tool_name, tool_input, v, "allow", classifier=auto["by"], extra={"auto_reason": auto["reason"]})
            return {"decision": "allow", "reason": f"[cfharness] allowed automatically ({auto['by']}): {auto['reason']}", "verdict": v.verdict,
                    "decided_by": auto["by"]}
        fp = action_fingerprint(tool_name, tool_input)
        ap = self.hub.approvals
        # LLM thứ hai (tắt mặc định): CHỈ hạ uncertain đủ điều kiện → safe/write; mọi kết quả khác → giữ chờ người như cũ
        if always_eligible(v) and self.second.enabled() and ap.find_pending(run_id, fp) is None:
            v, res = await self._second_opinion(run, channel, tool_name, tool_input, v, ctx, p.get("max_wait_s"))
            if res is not None:
                return res
        ticket = ap.consume_ticket(run.get("session_id"), fp, run_id)
        if ticket is not None:
            self._record(run, channel, tool_name, tool_input, v, "allow", ticket["id"])
            audit_record("approval.ticket_used", run["agent_id"], run_id, {"approval_id": ticket["id"], "action_fingerprint": fp, "tool": tool_name})
            return {"decision": "allow", "reason": f"[cfharness] approved once by a human (request {ticket['id']})", "approval_id": ticket["id"]}
        row = ap.find_pending(run_id, fp)
        if row is None:
            route, route_why = self.route_for(run, tool_name, tool_input, v, ctx)
            row = ap.open(run, tool_name=tool_name, tool_input=tool_input, tool_use_id=p.get("tool_use_id"), fingerprint=fp, verdict=v.to_dict(), channel=channel,
                          routed_to=route, route_note=route_why)
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
            return {"decision": "allow", "reason": f"[cfharness] approved by a human (request {aid})", "approval_id": aid}
        if outcome == "denied":
            cur = ap.get(aid)
            note = cur.get("note")
            mgr_dec = str(cur.get("resolved_by") or "").startswith("manager:")
            who = "The coordinator (manager agent)" if mgr_dec else "A human"
            self._record(run, channel, tool_name, tool_input, v, "deny", aid)
            return {"decision": "deny", "reason": DENIED_MSG.format(who=who, aid=aid, note=(f" Note from {'the coordinator' if mgr_dec else 'the human'}: {note}." if note else "")),
                    "approval_id": aid}
        if outcome == "cancelled":
            self._record(run, channel, tool_name, tool_input, v, "deny", aid)
            return {"decision": "deny", "reason": f"[cfharness] Request {aid} was cancelled. Do not retry; end your turn.", "approval_id": aid}
        # hết giờ giữ → waiting_human (KHÔNG tự từ chối: request vẫn chờ người)
        from .profiles import on_timeout

        from .classifier import SANDBOX_NETWORK_TOOL

        net = tool_name == SANDBOX_NETWORK_TOOL  # mạng của Bash trong sandbox: không thuộc bất biến "phá huỷ không tự từ chối" — hết giờ = deny, chạy tiếp
        if net or on_timeout(self.hub.config, run["agent_id"]) == "deny_and_continue":
            # opt-in theo agent: agent nhận từ chối kèm hướng dẫn, run CHẠY TIẾP, approval VẪN mở; người duyệt sau → resume (approvals.py)
            ap.to_waiting_human(aid, continued=True)
            self._record(run, channel, tool_name, tool_input, v, "deny", aid, extra={"timeout_action": "deny_and_continue"})
            msg = NET_CONTINUE_MSG if net else CONTINUE_MSG
            return {"decision": "deny", "reason": msg.format(aid=aid, timeout=int(timeout)), "approval_id": aid, "continued": True}
        ap.to_waiting_human(aid)
        self.hub.runs.pause_for_human(run_id, aid)
        return {"decision": "deny", "reason": WAITING_MSG.format(aid=aid, timeout=int(timeout)), "approval_id": aid, "waiting_human": True}

    def _allow_trusted(self, run: dict[str, Any], channel: str, tool_name: str, tool_input: dict[str, Any], ctx: GuardContext,
                       trust: dict[str, Any]) -> dict[str, Any]:
        """Agent trust full: cho phép mọi thao tác (kể cả forbidden/vùng hub). Vẫn phân loại để ghi guard_decisions + audit đúng mức."""
        try:
            v = classify(tool_name, tool_input, ctx)
        except Exception as e:  # noqa: BLE001 — phân loại lỗi không chặn agent trust, chỉ ghi lại
            v = Verdict("uncertain", "classifier_error", None, f"phân loại lỗi: {type(e).__name__}")
        busy = self.chrome_lock_check(run, tool_name)  # khoá Chrome là điều phối tài nguyên dùng chung, không phải quyền — vẫn áp
        if busy is not None:
            vb = Verdict("uncertain", "chrome_busy", "mcp", busy["reason"], [tool_name[:200]])
            self._record(run, channel, tool_name, tool_input, vb, "deny")
            return {"decision": "deny", "reason": busy["message"], "browser_busy": True}
        why = f"agent trust full (bởi {trust.get('by') or 'user'}" + (f", hết hạn {trust['until']})" if trust.get("until") else ")")
        self._record(run, channel, tool_name, tool_input, v, "allow", classifier="trust", extra={"auto_reason": why})
        return {"decision": "allow", "reason": f"hub guard: {v.verdict} (trust full)", "verdict": v.verdict, "decided_by": "trust"}

    async def _second_opinion(self, run: dict[str, Any], channel: str, tool_name: str, tool_input: dict[str, Any], v: Verdict, ctx: GuardContext,
                              max_wait: Any) -> tuple[Verdict, dict[str, Any] | None]:
        role = ""
        try:
            role = str((self.hub.store.get_agent(run["agent_id"]) or {}).get("role") or "")
        except Exception:  # pragma: no cover
            pass
        if ctx.manager_run:
            role = (role + " (hub coordinator/manager)").strip()
        mw = float(max_wait) - 5 if isinstance(max_wait, (int, float)) and max_wait > 0 else None
        try:
            op = await self.second.ask(tool_name, tool_input, cwd=str(ctx.cwd), role=role, rule=v.rule, why=v.reason, max_wait_s=mw)
        except Exception as e:  # pragma: no cover — ask() đã tự bắt lỗi; phòng hờ
            from .second_opinion import Opinion

            op = Opinion("needs_human", f"second opinion lỗi: {type(e).__name__}", "error")
        info = {"opinion": op.verdict, "opinion_reason": op.reason[:400], "opinion_source": op.source, "cost_usd": op.cost_usd, "model": op.model,
                "static_verdict": v.to_dict()}
        if op.downgrade and always_eligible(v):  # kiểm lại điều kiện ngay trước khi hạ (phòng thủ)
            nv = Verdict(op.verdict, f"llm:{v.rule}", v.klass, f"second opinion ({op.source}): {op.reason}"[:500], v.targets)
            self._record(run, channel, tool_name, tool_input, nv, "allow", classifier="llm", extra=info)
            if nv.verdict == "safe":  # _record chỉ audit khi verdict khác safe — quyết định do LLM luôn phải có audit
                audit_record("guard.allow", run["agent_id"], run["id"], {"tool": tool_name, "input": tool_input, "verdict": nv.to_dict(), "channel": channel,
                                                                          "actor": f"agent:{run['agent_id']}", "decided_by": "llm", **info})
            return nv, {"decision": "allow", "reason": f"hub guard: {op.verdict} (second opinion)", "verdict": op.verdict, "decided_by": "llm"}
        audit_record("guard.second_opinion", run["agent_id"], run["id"], {"tool": tool_name, "input": tool_input, "channel": channel, "decided_by": "human_pending", **info})
        if op.source in ("llm", "cache") and op.reason:
            v = Verdict(v.verdict, v.rule, v.klass, f"{v.reason} | LLM: {op.reason}"[:500], v.targets, v.protected_id, v.forbidden, v.net_urls)
        return v, None

    # ------------------------------------------------------------------ helpers cho CLI/RPC
    async def test(self, p: dict[str, Any]) -> dict[str, Any]:
        tool = str(p.get("tool") or "Bash")
        ti = p.get("input") if isinstance(p.get("input"), dict) else {"command": str(p.get("command") or "")}
        run = self.hub.store.find_run(p["run_id"]) if p.get("run_id") else None
        ctx = self.context_for(run, p.get("cwd"))
        v = classify(tool, ti, ctx)
        try:
            v = await self.net_check(run, tool, ti, v, ctx)
        except Exception as e:  # pragma: no cover
            v = Verdict("uncertain", "url_dns_failed", "network", f"kiểm mạng lỗi: {type(e).__name__}")
        from .profiles import never_automatic, profile

        hold = self.needs_human(v)
        auto = self.auto_allow(run, tool, ti, v, ctx) if (run and hold) else None
        return {**v.to_dict(), "hold": hold and auto is None, "fingerprint": action_fingerprint(tool, ti), "cwd": str(ctx.cwd),
                "profile": profile(self.hub.config), "auto_allow": auto,
                "never_automatic": never_automatic(v, tool_name=tool, tool_input=ti, roots=list(ctx.allowed_roots), cwd=str(ctx.cwd)) if hold else None}

    def policy(self) -> dict[str, Any]:
        d = self.policy_store.load().to_dict()
        d["always_allow"] = self.policy_store.always_list(include_disabled=True)
        return d

    def protect_add(self, p: dict[str, Any]) -> dict[str, Any]:
        ops = p.get("ops")
        if isinstance(ops, str):
            ops = [o.strip() for o in ops.split(",") if o.strip()]
        r = self.policy_store.add(str(p.get("kind") or ""), str(p.get("selector") or ""), ops, str(p.get("note") or ""))
        self._policy_changed("protect.add", r.to_dict(), p)
        return r.to_dict()

    def protect_rm(self, p: dict[str, Any]) -> dict[str, Any]:
        if str(p.get("id") or "").startswith("aa_"):  # luật luôn-cho-phép: gỡ qua cùng RPC chỉ-người (guard.protect.rm)
            return self.always_rm(str(p["id"]), by=str(p.get("by") or "user"), channel=str(p.get("channel") or "cli"))
        r = self.policy_store.remove(str(p.get("id") or ""))
        self._policy_changed("protect.rm", r.to_dict(), p)
        return r.to_dict()

    def _policy_changed(self, what: str, data: dict[str, Any], p: dict[str, Any]) -> None:
        ver = self.policy_store.load().version
        by, channel = p.get("by") or "user", p.get("channel") or "cli"
        self.hub.publish(make_event("policy.changed", None, {"version": ver, "by": by, "change": what, "resource": data}))
        audit_record("policy.changed", None, None, {"change": what, "resource": data, "version": ver, "actor": by, "channel": channel})
