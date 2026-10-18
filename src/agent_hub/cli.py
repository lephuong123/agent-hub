"""CLI `agent-hub` (FR-UI-08, bộ lệnh M0).

Mọi lệnh có `--json`. Mã thoát: 0 ok · 1 lỗi người dùng · 2 lỗi hệ thống · 3 bị chặn (ESTOP/ngân sách/auth).
CLI là client của daemon (writer duy nhất của DB); nếu daemon chưa chạy, CLI tự khởi động nó.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import parse_value
from .errors import exit_code_for
from .paths import HubPaths
from .rpc import DaemonUnavailable, RpcClient, RpcError

EXIT_OK, EXIT_USER, EXIT_SYS, EXIT_BLOCKED = 0, 1, 2, 3


# ============================================================================ helpers
class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.paths = HubPaths.resolve(getattr(args, "home", None))
        self.json = bool(getattr(args, "json", False))
        self._client: RpcClient | None = None

    def client(self, autostart: bool = True) -> RpcClient:
        if self._client is None:
            c = RpcClient(self.paths.socket, timeout=120)
            try:
                c.call("hub.ping")
            except DaemonUnavailable:
                if not autostart:
                    raise
                from .daemon import spawn_background

                pid = spawn_background(self.paths)
                eprint(f"(đã khởi động daemon nền, pid {pid})")
            self._client = c
        return self._client

    def call(self, method: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        return self.client().call(method, params or {}, **kw)

    def out(self, data: Any, text: str | None = None) -> None:
        if self.json:
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        elif text is not None:
            print(text)


def eprint(*a: Any) -> None:
    print(*a, file=sys.stderr)


def _split_list(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    out: list[str] = []
    for v in values:
        out.extend(x.strip() for x in v.split(",") if x.strip())
    return out


def _fmt_usd(v: Any) -> str:
    return "-" if v is None else f"${float(v):.4f}"


def _table(rows: list[list[Any]], headers: list[str]) -> str:
    cols = [headers] + [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cols) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cols]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


# ============================================================================ daemon
def cmd_daemon(ctx: Ctx) -> int:
    a = ctx.args
    if a.action == "run":
        from .daemon import run_foreground

        run_foreground(ctx.paths)
        return EXIT_OK
    client = RpcClient(ctx.paths.socket, timeout=10)
    if a.action == "start":
        try:
            st = client.call("hub.ping")
            ctx.out({"running": True, "pid": st.get("pid")}, f"daemon đã chạy (pid {st.get('pid')})")
            return EXIT_OK
        except DaemonUnavailable:
            pass
        if a.foreground:
            from .daemon import run_foreground

            run_foreground(ctx.paths)
            return EXIT_OK
        from .daemon import spawn_background

        pid = spawn_background(ctx.paths)
        ctx.out({"running": True, "pid": pid, "socket": str(ctx.paths.socket)}, f"daemon đã khởi động (pid {pid}, socket {ctx.paths.socket})")
        return EXIT_OK
    if a.action == "stop":
        try:
            client.call("hub.shutdown")
        except DaemonUnavailable:
            ctx.out({"running": False}, "daemon không chạy")
            return EXIT_OK
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and ctx.paths.socket.exists():
            time.sleep(0.1)
        if ctx.paths.socket.exists() and ctx.paths.pidfile.exists():
            try:
                os.kill(int(ctx.paths.pidfile.read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        ctx.out({"running": False}, "daemon đã dừng")
        return EXIT_OK
    if a.action == "logs":
        return _daemon_logs(ctx)
    if a.action == "status":
        try:
            st = client.call("hub.status")
        except DaemonUnavailable:
            ctx.out({"daemon_running": False, "home": str(ctx.paths.home)}, f"daemon không chạy (HUB_HOME={ctx.paths.home})")
            return EXIT_USER
        es = st["estop"]
        text = (
            f"daemon đang chạy · v{st['version']} · HUB_HOME={st['home']}\n"
            f"epoch {st['epoch']} · seq {st['last_seq']} · runtime={st['runtime']}\n"
            f"run: {st['running']} đang chạy / {st['queued']} chờ (giới hạn {st['max_concurrent_runs']}) · agent: {st['agents']}\n"
            f"chi phí hôm nay: {_fmt_usd(st['cost_today_usd'])}\n"
            f"ESTOP: {'BẬT — ' + str(es.get('reason') or '') if es.get('on') else 'tắt'}"
        )
        if st.get("auth_block"):
            text += "\nCẢNH BÁO: claude chưa đăng nhập — spawn bị chặn (chạy `claude auth login` rồi `agent-hub doctor`)"
        try:  # H-10: kiểm phiên bản lúc daemon khởi động + cảnh báo raw.unknown
            compat = client.call("hub.compat")
        except Exception:
            compat = None
        if compat:
            st["compat"] = compat
            text += _compat_text(compat)
        ctx.out({**st, "daemon_running": True}, text)
        return EXIT_OK
    return EXIT_USER


def _compat_text(compat: dict[str, Any]) -> str:
    out = ""
    cli = compat.get("cli") or {}
    if cli.get("status") == "untested":
        out += f"\nCẢNH BÁO (vàng): claude CLI {cli.get('version')} chưa có trong ma trận đã kiểm ({', '.join(cli.get('tested') or [])})"
    elif cli.get("status") == "missing":
        out += "\nCẢNH BÁO: không chạy được `claude --version` (agent-hub doctor)"
    ur = compat.get("unknown_rate") or {}
    if ur.get("alert"):
        out += f"\nCẢNH BÁO (ĐỎ): {ur.get('unknown')}/{ur.get('total')} event không nhận dạng được trong 1 giờ ({ur.get('rate', 0) * 100:.1f}%) — cân nhắc `agent-hub estop on`"
    return out


def _daemon_logs(ctx: Ctx) -> int:
    """`daemon logs` (FR-UI-08): N dòng cuối của log có cấu trúc `logs/hub.log` (+ `daemon.out`); lọc --run/--agent."""
    a = ctx.args
    n = int(getattr(a, "lines", None) or 50)
    files = [ctx.paths.logs_dir / "hub.log", ctx.paths.logs_dir / "daemon.out"]
    lines: list[str] = []
    for f in files:
        if not f.exists():
            continue
        try:
            data = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for ln in data:
            if a.run and a.run not in ln:
                continue
            if a.agent and a.agent not in ln:
                continue
            lines.append(ln if f.name == "hub.log" else f"[daemon.out] {ln}")
    lines = lines[-n:]
    if ctx.json:
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                out.append({"raw": ln})
        ctx.out(out)
    else:
        print("\n".join(lines) if lines else f"(chưa có log trong {ctx.paths.logs_dir})")
    return EXIT_OK


# ============================================================================ doctor
def cmd_doctor(ctx: Ctx) -> int:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, level: str = "error") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "level": "ok" if ok else level})

    from .config import HubConfig

    cfg = HubConfig(ctx.paths.config).data
    ccfg = cfg["runtime"]["claude"]
    binary = ccfg.get("binary") or "claude"
    path = shutil.which(binary)
    add("claude trong PATH", bool(path), path or f"không tìm thấy `{binary}`")
    if path:
        from .runtime.claude_cli import claude_version

        v = claude_version(binary, 0)
        tested = ccfg.get("tested_versions") or []
        add("phiên bản claude", bool(v) and v in tested, f"{v} (đã kiểm: {', '.join(tested)})" + ("" if v in tested else " — phiên bản chưa kiểm"), "warning")
        try:
            r = subprocess.run([binary, "auth", "status"], capture_output=True, text=True, timeout=30)
            try:
                info = json.loads(r.stdout)
                logged = bool(info.get("loggedIn", r.returncode == 0))
                detail = ", ".join(f"{k}={info[k]}" for k in ("loggedIn", "authMethod", "subscriptionType") if k in info)
            except (json.JSONDecodeError, AttributeError):
                logged = r.returncode == 0
                detail = (r.stdout or r.stderr).strip()[:200] or f"exit {r.returncode}"
            add("đăng nhập claude", logged, detail)
            auth_ok = logged
        except (OSError, subprocess.TimeoutExpired) as e:
            add("đăng nhập claude", False, str(e))
            auth_ok = False
    else:
        auth_ok = False
    add("HUB_HOME ghi được", os.access(ctx.paths.home, os.W_OK) if ctx.paths.home.exists() else os.access(ctx.paths.home.parent, os.W_OK), str(ctx.paths.home))
    client = RpcClient(ctx.paths.socket, timeout=10)
    daemon_up = True
    try:
        client.call("hub.ping")
        add("daemon", True, f"đang chạy ({ctx.paths.socket})")
    except DaemonUnavailable:
        daemon_up = False
        add("daemon", False, "không chạy (`agent-hub daemon start`)", "warning")
    if daemon_up and auth_ok:
        try:
            client.call("auth.reset")
        except Exception:
            pass
    if ctx.args.agent:
        if not daemon_up:
            add(f"agent {ctx.args.agent}", False, "cần daemon chạy để kiểm agent", "warning")
        else:
            try:
                ag = client.call("agent.get", {"id": ctx.args.agent})
                from .agents import AgentConfig, load_mcp_registry

                AgentConfig(id=ag["id"], **{k: ag[k] for k in ("name", "role", "description", "model", "effort", "fallback_model", "tool_policy", "mcp_servers", "limits", "workspace_default") if ag.get(k) is not None})
                add(f"cấu hình agent {ag['id']}", True, f"model={ag['model']} rev={ag['config_revision']}")
                reg = load_mcp_registry(ctx.paths)
                for s in ag.get("mcp_servers") or []:
                    if s not in reg:
                        add(f"MCP {s}", False, "không có trong registry")
                        continue
                    # FR-AG-08: thử kết nối thật (probe: initialize + tools/list, không gọi model)
                    try:
                        pr = client.call("mcp.probe", {"name": s, "timeout_s": getattr(ctx.args, "probe_timeout", None) or 20})
                        if pr.get("ok"):
                            add(f"MCP {s}", True, f"kết nối được, {len(pr.get('tools') or {})} tool ({pr.get('elapsed_ms')} ms)")
                        else:
                            add(f"MCP {s}", False, f"không kết nối được: {pr.get('error')}" + (f" — {pr['hint']}" if pr.get("hint") else ""))
                    except RpcError as e:
                        add(f"MCP {s}", False, f"không thử được: {e.message}")
                json.dumps(cfg["runtime"]["claude"].get("settings") or {})
                add("settings.json hub sinh", isinstance(cfg["runtime"]["claude"].get("settings") or {}, dict), "hợp lệ")
            except RpcError as e:
                add(f"agent {ctx.args.agent}", False, e.message)
    if daemon_up:
        try:
            compat = client.call("hub.compat") or {}
            ur = compat.get("unknown_rate") or {}
            if ur:
                add("tỉ lệ event lạ (raw.unknown, 1 giờ)", not ur.get("alert"), f"{ur.get('unknown', 0)}/{ur.get('total', 0)} ({(ur.get('rate') or 0) * 100:.1f}%)", "error")
        except Exception:
            pass
    bad = [c for c in checks if c["level"] == "error"]
    text = "\n".join(f"{'✓' if c['ok'] else ('!' if c['level'] == 'warning' else '✗')} {c['check']}: {c['detail']}" for c in checks)
    ctx.out({"ok": not bad, "checks": checks}, text)
    return EXIT_OK if not bad else EXIT_SYS


# ============================================================================ agent
def _agent_payload(a: argparse.Namespace, *, creating: bool) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for src, dst in (("name", "name"), ("role", "role"), ("description", "description"), ("model", "model"), ("effort", "effort"), ("fallback_model", "fallback_model"), ("workspace", "workspace_default")):
        v = getattr(a, src, None)
        if v is not None:
            d[dst] = v
    tp: dict[str, Any] = {}
    if a.permission_mode is not None:
        tp["permission_mode"] = a.permission_mode
    for src, dst in (("allowed_tools", "allowed_tools"), ("disallowed_tools", "disallowed_tools"), ("tools", "tools")):
        v = _split_list(getattr(a, src, None))
        if v is not None:
            tp[dst] = v
    if tp:
        d["tool_policy"] = tp
    if a.mcp is not None:
        d["mcp_servers"] = _split_list(a.mcp)
    lim: dict[str, Any] = {}
    for src, dst in (("budget_per_run", "max_budget_usd_per_run"), ("budget_per_day", "budget_usd_per_day"), ("max_concurrent", "max_concurrent_runs"), ("max_runtime", "max_runtime_s")):
        v = getattr(a, src, None)
        if v is not None:
            lim[dst] = v
    if lim:
        d["limits"] = lim
    for kv in getattr(a, "set", None) or []:
        if "=" not in kv:
            raise ValueError(f"--set cần dạng khoá=giá_trị: {kv}")
        k, v = kv.split("=", 1)
        parts = k.split(".")
        cur = d
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = parse_value(v)
    return d


def _read_soul(a: argparse.Namespace) -> str | None:
    if getattr(a, "soul_file", None):
        return Path(a.soul_file).expanduser().read_text(encoding="utf-8")
    return None


def _agent_text(ag: dict[str, Any]) -> str:
    tp = ag.get("tool_policy") or {}
    lim = ag.get("limits") or {}
    lines = [
        f"{ag['id']} — {ag['name']}" + (f"  [ĐÃ XOÁ MỀM, thùng rác: {ag.get('trash_path')}]" if ag.get("deleted_at") else ""),
        f"  role: {ag.get('role') or '-'}",
        f"  description: {ag.get('description') or '-'}",
        f"  status: {ag['status']}  current_run: {ag.get('current_run_id') or '-'}  revision: {ag['config_revision']}",
        f"  model: {ag.get('model')}  effort: {ag.get('effort') or '-'}  fallback: {ag.get('fallback_model') or '-'}",
        f"  permission_mode: {tp.get('permission_mode') or '(mặc định)'}  allowed: {','.join(tp.get('allowed_tools') or []) or '-'}  disallowed: {','.join(tp.get('disallowed_tools') or []) or '-'}",
        f"  mcp: {','.join(ag.get('mcp_servers') or []) or '-'}",
        f"  limits: {json.dumps(lim, ensure_ascii=False)}",
        f"  workspace: {ag.get('workspace_default') or '(scratch theo run)'}",
        f"  home: {ag['home_path']}",
        f"  skills: {', '.join(ag.get('skills') or []) or '-'}  memory: {ag.get('memory_chars', 0)} ký tự",
        f"  chi phí hôm nay: {_fmt_usd(ag.get('cost_today_usd'))}",
    ]
    if ag.get("recent_runs"):
        lines.append("  run gần nhất:")
        for r in ag["recent_runs"]:
            lines.append(f"    {r['id']}  {r['status']:<15} {_fmt_usd(r.get('cost_usd'))}  {r['queued_at']}  rev {r.get('agent_config_revision')}")
    return "\n".join(lines)


def cmd_agent(ctx: Ctx) -> int:
    a = ctx.args
    act = a.action
    if act == "create":
        payload = _agent_payload(a, creating=True)
        payload["id"] = a.id
        payload.setdefault("name", a.id)
        if getattr(a, "template", None):  # M2 (FR-AG-07): cờ của lệnh ghi đè giá trị template
            payload.pop("id")
            ag = ctx.call("agent.create_from_template", {"id": a.id, "template": a.template, "overrides": payload, "soul": _read_soul(a)})
            ctx.out(ag, f"đã tạo agent {ag['id']} từ template {a.template} (home {ag['home_path']})")
            return EXIT_OK
        ag = ctx.call("agent.create", {"config": payload, "soul": _read_soul(a)})
        ctx.out(ag, f"đã tạo agent {ag['id']} (home {ag['home_path']})")
    elif act == "list":
        rows = ctx.call("agent.list", {"archived": a.archived})
        if getattr(a, "status", None):  # FR-AG-02: lọc theo trạng thái
            rows = [r for r in rows if r["status"] == a.status or (a.status == "archived" and r.get("archived_at"))]
        if getattr(a, "team", None):  # FR-AG-02: lọc theo team
            members = {m["agent_id"] for m in (ctx.call("team.get", {"id": a.team}).get("members") or [])}
            rows = [r for r in rows if r["id"] in members]
        ctx.out(
            rows,
            _table(
                [[r["id"], r["name"], r.get("role"), r["status"] + (" (xoá)" if r.get("deleted_at") else ""), r.get("model"), r.get("current_run_id") or "-", r.get("last_tool") or "-", _fmt_usd(r.get("cost_today_usd")), r["config_revision"]] for r in rows],
                ["ID", "NAME", "ROLE", "STATUS", "MODEL", "RUN", "LAST TOOL", "$ HÔM NAY", "REV"],
            )
            if rows
            else "(chưa có agent)",
        )
    elif act == "show":
        ag = ctx.call("agent.get", {"id": a.id})
        if a.soul:
            ctx.out(ag, ag.get("soul") or "")
        else:
            ctx.out(ag, _agent_text(ag))
    elif act == "edit":
        changes = _agent_payload(a, creating=False)
        soul = _read_soul(a)
        if not changes and soul is None:
            eprint("không có thay đổi nào (dùng --model, --role, --set khoá=giá_trị, --soul-file …)")
            return EXIT_USER
        ag = ctx.call("agent.update", {"id": a.id, "changes": changes, "soul": soul})
        ctx.out(ag, f"đã sửa agent {ag['id']} → revision {ag['config_revision']}")
    elif act in ("rm", "archive"):
        ag = ctx.call("agent.delete" if act == "rm" else "agent.archive", {"id": a.id, "detach_teams": bool(a.detach_teams)})
        det = ag.get("detached_teams") or []
        extra = f"; đã gỡ khỏi team: {', '.join(t['team_id'] for t in det)}" if det else ""
        ctx.out(ag, f"đã xoá mềm agent {a.id} → {ag.get('trash_path')}{extra} (khôi phục: agent-hub agent restore {a.id})")
    elif act == "revert":
        ag = ctx.call("agent.revert", {"id": a.id, "to": a.to})
        note = " (revision đích không lưu SOUL — SOUL giữ nguyên)" if ag.get("soul_in_snapshot") is False else ""
        ctx.out(ag, f"agent {a.id}: revert về nội dung revision {a.to} → revision mới {ag['config_revision']}{note}")
    elif act == "restore":
        ag = ctx.call("agent.restore", {"id": a.id})
        ctx.out(ag, f"đã khôi phục agent {a.id}")
    elif act in ("pause", "resume"):
        ag = ctx.call(f"agent.{act}", {"id": a.id})
        ctx.out(ag, f"agent {a.id}: {ag['status']}")
    elif act == "revisions":
        revs = ctx.call("agent.revisions", {"id": a.id})
        ctx.out(revs, "\n".join(f"rev {r['revision']}  {r['actor']}  {r['created_at']}  model={r['config_snapshot'].get('model')}" for r in revs))
    return EXIT_OK


# ============================================================================ run
def render_event(ev: dict[str, Any], state: dict[str, Any]) -> str | None:
    t = ev["type"]
    p = ev.get("payload") or {}
    indent = "    " if ev.get("subagent_id") else ""
    if t == "run.queued":
        return f"⧗ run {ev['run_id']} đã vào hàng đợi (agent {ev['agent_id']}, ngân sách {_fmt_usd(p.get('max_budget_usd'))})"
    if t == "run.started":
        mcp = ",".join(s["name"] for s in p.get("mcp_servers") or []) or "-"
        return f"▶ {ev['agent_id']} · {p.get('model')} · session {str(p.get('session_id'))[:8]} · cwd {p.get('cwd')} · mcp {mcp} · CLI {p.get('claude_code_version')}"
    if t == "message.complete":
        return indent + (p.get("text") or "").rstrip()
    if t == "tool.start":
        state[p.get("tool_id")] = p.get("name")
        return f"{indent}┊ {p.get('name')}({p.get('args_preview') or ''})"
    if t == "tool.complete":
        mark = "✗" if p.get("is_error") else "✓"
        prev = (p.get("result_preview") or "").splitlines()
        first = prev[0][:100] if prev else ""
        more = f" (+{len(prev) - 1} dòng)" if len(prev) > 1 else ""
        return f"{indent}  {mark} {p.get('name')} {p.get('duration_s')}s  {first}{more}"
    if t == "approval.open":  # M1-B
        return f"◐ GIỮ CHỜ DUYỆT {p.get('request_id')}: {p.get('preview')} — {p.get('reason')}\n  duyệt: agent-hub approve {p.get('request_id')} · từ chối: agent-hub deny {p.get('request_id')}"
    if t == "approval.resolved":
        return f"◑ {p.get('request_id')}: {p.get('decision')} bởi {p.get('by')} ({p.get('channel')}, chờ {p.get('wait_s')}s)"
    if t == "run.waiting_human":
        return f"⏸ waiting_human — chờ người duyệt {p.get('approval_id')} (tiến trình đã dừng, giữ khoá thư mục). Khi duyệt, hub tự resume ở run mới."
    if t == "guard.blocked":
        return f"⛔ guard chặn: {p.get('detail')}"
    if t == "isolation.violation":
        return f"⚠ vi phạm cô lập: {p.get('kind')} {p.get('name')}"
    if t in ("run.completed", "run.failed", "run.cancelled", "run.interrupted") and ev.get("source") == "hub":
        u = p.get("usage") or {}
        toks = sum(int(u.get(k, 0) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
        dur = f"{(p.get('duration_ms') or 0) / 1000:.1f}s" if p.get("duration_ms") else "-"
        line = f"■ {p.get('outcome')} · {_fmt_usd(p.get('cost_usd'))} · {dur} · {toks} token · turns {p.get('num_turns') or '-'}"
        if p.get("error") and p.get("outcome") != "completed":
            line += f"\n  lỗi: {str(p['error'])[:400]}"
        return line
    if t == "error":
        return f"! {p.get('message')}"
    if t == "cli.version_warning":
        return f"! claude CLI {p.get('version')} chưa có trong ma trận đã kiểm ({', '.join(p.get('matrix') or [])})"
    return None


def follow_run(ctx: Ctx, run_id: str, since_seq: int | None, epoch: str | None) -> dict[str, Any] | None:
    state: dict[str, Any] = {}
    last: dict[str, Any] | None = None
    params: dict[str, Any] = {"run_id": run_id, "until_run_end": True, "level": "summary"}
    if since_seq is not None:
        params["since_seq"] = since_seq
        params["epoch"] = epoch
    else:
        params["since_seq"] = 0
    for msg in ctx.client().subscribe(params):
        if "subscribed" in msg or "end" in msg:
            continue
        if ctx.json:
            print(json.dumps(msg, ensure_ascii=False), flush=True)
        else:
            line = render_event(msg, state)
            if line:
                print(line, flush=True)
        if msg["type"] in ("run.completed", "run.failed", "run.cancelled", "run.interrupted") and msg.get("source") == "hub":
            last = msg
    return last


def cmd_run(ctx: Ctx) -> int:
    a = ctx.args
    if getattr(a, "continue_from", None):
        # FR-UI-08: `run <agent> "<prompt>" --continue <run>` = `run continue <run> "<prompt>"` (tiếp tục session của run đó)
        prev = ctx.call("run.get", {"id": a.continue_from})
        if prev.get("agent_id") != a.agent:
            raise ValueError(f"run {a.continue_from} thuộc agent '{prev.get('agent_id')}', không phải '{a.agent}'")
        run = ctx.call("run.continue", {"id": a.continue_from, "prompt": a.prompt, "budget_usd": a.budget})
    else:
        run = None
    params: dict[str, Any] = {"agent": a.agent, "prompt": a.prompt}
    if a.cwd:
        params["cwd"] = str(Path(a.cwd).expanduser().resolve())
    if a.budget is not None:
        params["budget_usd"] = a.budget
    if a.model:
        params["model"] = a.model
    if run is None:
        run = ctx.call("run.start", params)
    if not a.follow:
        ctx.out(run, run["id"])
        return EXIT_OK
    if not ctx.json:
        eprint(f"run {run['id']} (session {run['session_id']})")
    follow_run(ctx, run["id"], run.get("seq_before"), run.get("epoch"))
    final = ctx.call("run.get", {"id": run["id"]})
    if ctx.json:
        print(json.dumps({"run": final}, ensure_ascii=False, default=str))
    oc = final.get("outcome")
    return EXIT_OK if oc == "completed" else (EXIT_BLOCKED if oc == "budget_exceeded" else EXIT_USER)


def cmd_runs(ctx: Ctx) -> int:
    a = ctx.args
    if a.action == "list":
        rows = ctx.call("run.list", {"agent": a.agent, "status": a.status, "limit": a.limit})
        ctx.out(
            rows,
            _table(
                [[r["id"], r["agent_id"], r["status"], r.get("model") or "-", _fmt_usd(r.get("cost_usd")), f"{(r.get('duration_ms') or 0) / 1000:.1f}s" if r.get("duration_ms") else "-", r["queued_at"], (r.get("prompt") or "")[:40].replace("\n", " ")] for r in rows],
                ["RUN", "AGENT", "STATUS", "MODEL", "COST", "DUR", "QUEUED", "PROMPT"],
            )
            if rows
            else "(chưa có run)",
        )
    elif a.action == "show":
        r = ctx.call("run.get", {"id": a.id, "events": a.events})
        if ctx.json:
            ctx.out(r)
        else:
            keys = ["id", "agent_id", "status", "outcome", "session_id", "model", "cli_version", "agent_config_revision", "cwd", "max_budget_usd", "cost_usd", "cost_basis", "duration_ms", "num_turns", "queued_at", "started_at", "ended_at", "pid", "system_prompt_hash", "raw_log_path", "stderr_log_path", "isolation_violations", "error", "summary"]
            print("\n".join(f"{k:>22}: {r.get(k)}" for k in keys))
            print(f"{'argv':>22}: {' '.join(r.get('argv_redacted') or [])}")
            if a.events:
                for e in r.get("events") or []:
                    print(f"  #{e['seq']:<5} {e['ts']}  {e['type']:<20} {json.dumps(e['payload'], ensure_ascii=False)[:120]}")
    elif a.action == "stop":
        r = ctx.call("run.stop", {"id": a.id})
        ctx.out(r, f"đã gửi lệnh dừng run {r['id']}")
    elif a.action == "logs":
        r = ctx.call("run.logs", {"id": a.id})
        ctx.out(r, (r.get("stderr") or "(stderr rỗng)") + (f"\nlỗi: {r['error']}" if r.get("error") else "") + f"\nstdout thô: {r.get('raw_log_path')}")
    elif a.action == "follow":
        r = ctx.call("run.get", {"id": a.id})
        follow_run(ctx, r["id"], 0, None)
    return EXIT_OK


# ============================================================================ estop / config
def cmd_estop(ctx: Ctx) -> int:
    a = ctx.args
    if a.action == "status":
        st = ctx.call("estop.get")
    else:
        st = ctx.call("estop.set", {"on": a.action == "on", "reason": getattr(a, "reason", None), "stop_running": getattr(a, "stop_running", False), "team": getattr(a, "team", None)})
    ctx.out(st, f"ESTOP: {'BẬT' if st.get('on') else 'tắt'}" + (f" — {st.get('reason')}" if st.get("reason") else "") + (" (đã gửi dừng mọi run)" if st.get("stop_running") else ""))
    return EXIT_OK


def cmd_config(ctx: Ctx) -> int:
    a = ctx.args
    if a.action == "get":
        v = ctx.call("config.get", {"key": a.key})
        if ctx.json or isinstance(v, (dict, list)):
            print(json.dumps(v, ensure_ascii=False, indent=2))
        else:
            print(v)
    else:
        r = ctx.call("config.set", {"key": a.key, "value": parse_value(a.value)})
        ctx.out(r, f"{a.key} = {json.dumps(r[a.key], ensure_ascii=False)}")
    return EXIT_OK


# ============================================================================ parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="xuất JSON")
    common.add_argument("--home", default=argparse.SUPPRESS, help="HUB_HOME (mặc định $AGENT_HUB_HOME hoặc ~/.agent-hub)")

    p = argparse.ArgumentParser(prog="agent-hub", description="agent-hub — điều phối đội agent chạy trên `claude` CLI", parents=[common])
    p.add_argument("--version", action="version", version=f"agent-hub {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="quản lý daemon", parents=[common])
    d.add_argument("action", choices=["start", "stop", "status", "run", "logs"])
    d.add_argument("--foreground", action="store_true", help="chạy daemon ở tiền cảnh")
    d.add_argument("--lines", "-n", type=int, default=50, help="logs: số dòng cuối")
    d.add_argument("--run", help="logs: chỉ dòng có run_id này")
    d.add_argument("--agent", help="logs: chỉ dòng có agent_id này")
    d.set_defaults(fn=cmd_daemon)

    doc = sub.add_parser("doctor", help="kiểm sức khoẻ (không gọi model)", parents=[common])
    doc.add_argument("agent", nargs="?")
    doc.add_argument("--probe-timeout", type=float, default=20, help="giây chờ mỗi MCP server khi thử kết nối")
    doc.set_defaults(fn=cmd_doctor)

    ag = sub.add_parser("agent", help="quản lý agent", parents=[common])
    asub = ag.add_subparsers(dest="action", required=True)

    def agent_fields(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--name")
        sp.add_argument("--role")
        sp.add_argument("--description")
        sp.add_argument("--model", help="opus|sonnet|haiku|fable hoặc id đầy đủ")
        sp.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
        sp.add_argument("--fallback-model")
        sp.add_argument("--permission-mode", choices=["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan", "default"])
        sp.add_argument("--allowed-tools", action="append", help="lặp lại hoặc phân tách bằng dấu phẩy")
        sp.add_argument("--disallowed-tools", action="append")
        sp.add_argument("--tools", action="append", help="danh sách tool built-in được dùng (--tools '' = không tool nào)")
        sp.add_argument("--mcp", action="append", help="tên MCP server trong registry")
        sp.add_argument("--budget-per-run", type=float)
        sp.add_argument("--budget-per-day", type=float)
        sp.add_argument("--max-concurrent", type=int)
        sp.add_argument("--max-runtime", type=int, help="giây")
        sp.add_argument("--workspace", help="thư mục làm việc mặc định (mặc định: scratch theo run)")
        sp.add_argument("--soul-file", help="file SOUL.md (persona, tiếng Anh)")
        sp.add_argument("--set", action="append", metavar="KHOÁ=GIÁ_TRỊ", help="đặt trường bất kỳ, vd limits.max_runtime_s=600")

    c = asub.add_parser("create", help="tạo agent", parents=[common])
    c.add_argument("id")
    c.add_argument("--template", "--from-template", dest="template", help="tạo từ template: researcher|coder|reviewer|ops (agent-hub template list)")
    agent_fields(c)
    lst = asub.add_parser("list", help="liệt kê agent", parents=[common])
    lst.add_argument("--archived", action="store_true", help="kể cả agent đã xoá mềm")
    lst.add_argument("--team", help="chỉ agent là thành viên team này")
    lst.add_argument("--status", choices=["idle", "working", "waiting", "paused", "error", "disabled", "archived"], help="lọc theo trạng thái")
    sh = asub.add_parser("show", help="xem agent", parents=[common])
    sh.add_argument("id")
    sh.add_argument("--soul", action="store_true", help="chỉ in SOUL.md")
    e = asub.add_parser("edit", help="sửa agent (tăng revision)", parents=[common])
    e.add_argument("id")
    agent_fields(e)
    for name, hlp in (("rm", "xoá mềm (chuyển vào thùng rác)"), ("archive", "= rm: archive + chuyển vào thùng rác (khôi phục bằng restore)"),
                      ("restore", "khôi phục agent đã xoá mềm"), ("pause", "tạm dừng nhận việc mới"), ("resume", "tiếp tục nhận việc"), ("revisions", "lịch sử revision")):
        x = asub.add_parser(name, help=hlp, parents=[common])
        x.add_argument("id")
        if name in ("rm", "archive"):
            x.add_argument("--detach-teams", action="store_true", help="gỡ agent khỏi mọi team trước khi xoá (mặc định: từ chối nếu còn là thành viên)")
    rv = asub.add_parser("revert", help="tạo revision mới có nội dung bằng revision N (FR-AG-03)", parents=[common])
    rv.add_argument("id")
    rv.add_argument("--to", type=int, required=True, metavar="N", help="số revision đích (xem `agent revisions <id>`)")
    ag.set_defaults(fn=cmd_agent)

    r = sub.add_parser("run", help='chạy ad-hoc: agent-hub run <agent> "<prompt>"', parents=[common])
    r.add_argument("agent")
    r.add_argument("prompt")
    rmode = r.add_mutually_exclusive_group()
    rmode.add_argument("--follow", "-f", "--wait", action="store_true", dest="follow", help="theo dõi stream tới khi xong")
    rmode.add_argument("--detach", "-d", action="store_false", dest="follow", help="trả run id ngay (mặc định)")
    r.add_argument("--continue", dest="continue_from", metavar="RUN", help="tiếp tục session của run này (= run continue <run> \"<prompt>\")")
    r.add_argument("--cwd", help="thư mục làm việc (mặc định: workspace của agent hoặc scratch)")
    r.add_argument("--budget", type=float, help="ngân sách USD cho run (--max-budget-usd)")
    r.add_argument("--model", help="ghi đè model cho run này")
    r.set_defaults(fn=cmd_run)

    rs = sub.add_parser("runs", help="xem/điều khiển run", parents=[common])
    rsub = rs.add_subparsers(dest="action", required=True)
    rl = rsub.add_parser("list", parents=[common])
    rl.add_argument("--agent")
    rl.add_argument("--status")
    rl.add_argument("--limit", type=int, default=20)
    rsh = rsub.add_parser("show", parents=[common])
    rsh.add_argument("id")
    rsh.add_argument("--events", action="store_true")
    for name in ("stop", "logs", "follow"):
        x = rsub.add_parser(name, parents=[common])
        x.add_argument("id")
    rs.set_defaults(fn=cmd_runs)

    es = sub.add_parser("estop", help="dừng khẩn cấp toàn hub", parents=[common])
    esub = es.add_subparsers(dest="action", required=True)
    on = esub.add_parser("on", parents=[common])
    on.add_argument("--reason")
    on.add_argument("--stop-running", action="store_true", help="gửi dừng mọi run đang chạy")
    on.add_argument("--team", help="M3: chỉ ESTOP team này (FR-TK-10)")
    off = esub.add_parser("off", parents=[common])
    off.add_argument("--team", help="M3: tắt ESTOP của team này")
    esub.add_parser("status", parents=[common])
    es.set_defaults(fn=cmd_estop)

    cf = sub.add_parser("config", help="đọc/ghi cấu hình hub", parents=[common])
    csub = cf.add_subparsers(dest="action", required=True)
    g = csub.add_parser("get", parents=[common])
    g.add_argument("key", nargs="?")
    s = csub.add_parser("set", parents=[common])
    s.add_argument("key")
    s.add_argument("value", help="giá trị đọc như YAML: 4, true, null, [a, b]")
    cf.set_defaults(fn=cmd_config)

    # M1 luồng A: mcp (serve/snippet/token/registry), secret, audit, feedback
    from .mcp_server.cli import add_parsers as _add_integration_parsers

    _add_integration_parsers(sub, common)

    # M1 luồng B: approvals/approve/deny, guard, notify, run continue, agent purge
    from .guard.cli import add_parsers as _add_safety_parsers

    _add_safety_parsers(sub, common)

    # M2: top, attach, steer/run steer, takeover, template
    from .tui.cli import add_parsers as _add_tui_parsers

    _add_tui_parsers(sub, common)

    # M3: team, task, run revert|diff, report, cost, agent clone|export|import
    from .dispatch.cli import add_parsers as _add_m3_parsers

    _add_m3_parsers(sub, common)

    # M4-B: golden, eval, digest (+ `learn digest`), curator, regression
    from .learning_ops.cli import add_parsers as _add_m4b_parsers

    _add_m4b_parsers(sub, common)

    # M4-A: learn (signal/review/proposal/ledger/rollback/policy/cache), knowledge (memory/skill/pin)
    from .learning.cli import add_parsers as _add_m4a_parsers

    _add_m4a_parsers(sub, common)

    # M5-B: curation, report kpi|overhead, telemetry
    from .curation.cli import add_parsers as _add_m5b_parsers

    _add_m5b_parsers(sub, common)

    # hardening CLI (H-08, H-11): stop, trash, hook + alias (`cli_surface.rewrite_argv`)
    from .cli_surface import add_parsers as _add_surface_parsers

    _add_surface_parsers(sub, common)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    from .dispatch.cli import rewrite_argv as _rewrite_m3
    from .guard.cli import rewrite_argv

    from .learning_ops.cli import rewrite_argv as _rewrite_m4b

    from .curation.cli import rewrite_argv as _rewrite_m5b

    from .cli_surface import rewrite_argv as _rewrite_surface

    raw = _rewrite_surface(sys.argv[1:] if argv is None else list(argv), parser)
    args = parser.parse_args(_rewrite_m5b(_rewrite_m4b(_rewrite_m3(rewrite_argv(raw)))))
    ctx = Ctx(args)
    try:
        return int(args.fn(ctx) or 0)
    except RpcError as e:
        if ctx.json:
            print(json.dumps({"error": {"code": e.code, "message": e.message, "data": e.data}}, ensure_ascii=False, default=str))
        else:
            eprint(f"lỗi: {e.message}")
            fields = (e.data or {}).get("fields") if isinstance(e.data, dict) else None
            for f in fields or []:
                eprint(f"  - {f['field']}: {f['error']}")
        return exit_code_for(e.code)
    except DaemonUnavailable as e:
        eprint(f"lỗi: {e}")
        return EXIT_SYS
    except (ValueError, FileNotFoundError) as e:
        eprint(f"lỗi: {e}")
        return EXIT_USER
    except KeyboardInterrupt:
        return 130
    except RuntimeError as e:
        eprint(f"lỗi hệ thống: {e}")
        return EXIT_SYS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
