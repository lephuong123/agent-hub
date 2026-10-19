"""Điều phối viên (M7 MGR-CORE) — `hub.manager`: chỉ định, hội thoại liên tục theo kênh, nhịp heartbeat, tool hub-self.

- **Chỉ định**: `hub_state.manager = {agent_id, set_at, by, channel, history:[…]}` — chỉ người (`manager.set` ∈ HUMAN_ONLY +
  `auth.require_human` ở tầng service). Lưu trong hub.db (agent không ghi được) chứ không ở config.yaml.
- **Hội thoại** (`manager_conversations`, `manager_messages`, migration v13): mỗi tin người → một run `kind=manager` của agent
  manager, `--resume` cùng session của conversation (claude tự autocompact khi ngữ cảnh gần đầy); sau `manager.rotate_after_turns`
  lượt (hoặc run lỗi) → session mới, mang theo vài tin gần nhất. cwd cố định `agents/<id>/workspaces/conv-<id>` (resume cần cùng cwd).
- **Nhịp**: listener bus gom sự kiện đáng xử lý (task blocked/done, circuit open, run lỗi lặp, run do manager khởi chạy xong,
  digest) + vòng nền (`manager.check_s`) thêm approval chờ người > N phút; tick khi có sự kiện đã chờ `event_debounce_s`, hoặc
  định kỳ `tick_interval_s` CHỈ khi có việc (task do manager giao chưa xong, approval chờ, sự kiện). Không tick khi ESTOP, agent
  pause/archive, hoặc đang có run manager (khoá chống chồng — sự kiện giữ lại cho tick sau).
- **Tool hub-self `manager`** → `self_call` (RPC `managerself.call`): ràng run_id ↔ người gọi, op ∈ MANAGER_TOOLS, kiểm quyền
  (`permissions.py`), thực hiện qua `Daemon.call_as(manager_caller)` (daemon kiểm lại MANAGER_ALLOWED + trường cấm, bọc audit
  actor `manager:<agent>`), sửa cấu hình agent qua `Ledger.apply` (actor `manager:<agent>` = thay đổi tự động, rollback được).
  Mọi thao tác ghi → event `manager.action{tool, target, summary, audit_id, ok, error?}`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import auth as _auth
from ..errors import Blocked, Conflict, HubError, NotFound
from ..events import TERMINAL_RUN_EVENTS, HubEvent, iso, make_event
from ..store import now_iso
from . import prompts
from .permissions import ManagerPermissions

if TYPE_CHECKING:
    from ..daemon import Daemon
    from ..hub import Hub

log = logging.getLogger("cfharness.manager")

HEARTBEAT_CHANNEL = "heartbeat"
LIVE = ("queued", "running", "waiting_human")
TEXT_MAX = 4000


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _trunc(v: Any, n: int = 600) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"… (+{len(v) - n} chars)"
    if isinstance(v, dict):
        return {k: _trunc(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_trunc(x, n) for x in v[:60]]
    return v


class ManagerService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.perms = ManagerPermissions(hub)
        self.daemon: "Daemon | None" = None  # gắn ở manager/rpc.register_rpc (call_as)
        self.pending: list[dict[str, Any]] = []
        self._pending_keys: set[str] = set()
        self._first_pending: float | None = None
        self._reported_approvals: set[str] = set()
        self._fail_times: dict[str, list[float]] = {}
        self._tick_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._run_keys: dict[str, str] = {}
        hub.bus.add_listener(self._on_event)

    # ------------------------------------------------------------------ cấu hình
    def cfg(self, key: str, default: Any = None) -> Any:
        try:
            v = self.hub.config.get(f"manager.{key}")
        except KeyError:
            return default
        return default if v is None else v

    @property
    def conn(self):
        return self.hub.store.conn

    # ------------------------------------------------------------------ chỉ định (HUMAN_ONLY)
    def state(self) -> dict[str, Any]:
        return dict(self.hub.store.state_get("manager") or {})

    def agent_id(self) -> str | None:
        return self.state().get("agent_id") or None

    def is_manager(self, agent_id: str | None) -> bool:
        return bool(agent_id) and agent_id == self.agent_id()

    # ---- MGR-3: run nào được vai trò manager (do hội thoại/tick tạo, hoặc tiếp nối nội bộ của chúng)
    ISSUED_KEY = "manager_issued_runs"

    def mark_issued(self, run_id: str, conversation_id: str | None) -> None:
        st = dict(self.hub.store.state_get(self.ISSUED_KEY) or {})
        st[run_id] = conversation_id
        if len(st) > 2000:
            st = dict(list(st.items())[-2000:])
        self.hub.store.state_set(self.ISSUED_KEY, st)

    def is_issued(self, run_id: str | None) -> bool:
        return bool(run_id) and run_id in (self.hub.store.state_get(self.ISSUED_KEY) or {})

    def run_key(self, run_id: str, *, create: bool = True) -> str | None:
        """R2-3: khoá của tiêu đề tin thật cho MỘT run manager — ngẫu nhiên 128 bit, chỉ ở RAM, đăng ký che theo giá trị (DB/event/
        raw log/RPC/web/MCP chỉ thấy `***`). Prompt lưu DB chỉ có `prompts.KEY_PLACEHOLDER`; `_prepare_spec` thay lúc spawn."""
        k = self._run_keys.get(run_id)
        if k is None and create:
            from ..redact import register_secret_values

            k = secrets.token_hex(16)
            register_secret_values(k)
            self._run_keys[run_id] = k
            if len(self._run_keys) > 500:
                self._run_keys.pop(next(iter(self._run_keys)))
        return k

    def render_prompt(self, run_id: str, prompt: str) -> str:
        return prompt.replace(prompts.KEY_PLACEHOLDER, self.run_key(run_id) or "") if prompts.KEY_PLACEHOLDER in prompt else prompt

    def agent_target_block(self, method: str, p: dict[str, Any]) -> str | None:
        """MGR-3: người gọi KHÔNG phải người (agent/thiếu token) không được điều khiển run/agent điều phối viên (continue/resume/steer/
        stop/start, giao task cho manager). Chỉ người hoặc chính cơ chế hội thoại/tick của hub."""
        mids = self.perms.manager_ids()
        if not mids:
            return None
        if method in ("run.continue", "run.resume", "run.steer", "run.stop", "run.takeover", "mcp.cancel_task"):
            ref = p.get("id") or p.get("run_id") or p.get("ref")
            run = self.hub.store.find_run(str(ref)) if ref else None
            if run is None and ref:
                r = self.conn.execute("SELECT agent_id FROM runs WHERE session_id=? LIMIT 1", (str(ref),)).fetchone()
                aid = r[0] if r else (str(ref) if str(ref) in mids else None)
            else:
                aid = (run or {}).get("agent_id")
            if aid in mids:
                return "run của điều phối viên chỉ người (hoặc hội thoại/nhịp của hub) được tiếp tục/điều khiển"
        if method in ("run.start", "mcp.run", "mcp.agent_run") and str(p.get("agent") or p.get("agent_id") or "") in mids:
            return "chỉ người được khởi chạy run cho điều phối viên (dùng manager.chat)"
        if method in ("task.create", "mcp.team_run"):
            t = p.get("task") if isinstance(p.get("task"), dict) else p
            if str(t.get("assignee") or "") in mids:
                return "không giao task cho điều phối viên"
        return None

    def set(self, agent_id: str | None, *, by: str = "user", channel: str = "cli") -> dict[str, Any]:
        _auth.require_human("chỉ định điều phối viên")  # cổng tầng service (ngoài HUMAN_ONLY ở RPC)
        st = self.state()
        prev = st.get("agent_id")
        if agent_id:
            row = self.hub.agents.get_row(agent_id)  # NotFound nếu không có / đã archive
            if row.get("archived_at"):
                raise HubError(f"agent '{agent_id}' đã bị xoá mềm")
            why = self.perms.manager_config_problem(row)  # MGR-8: chỉ agent có tool/quyền trong khung điều phối viên
            if why:
                raise Blocked(f"không chỉ định được '{agent_id}' làm điều phối viên: {why}. Sửa cấu hình agent trước "
                              f"(vd `cfharness agent edit {agent_id} --tools Read,Grep,Glob`).", data={"reason": "manager_config"})
        hist = [x for x in (st.get("history") or []) if x]
        if agent_id and agent_id not in hist:
            hist.append(agent_id)
        st.pop("key", None)
        st.update({"agent_id": agent_id or None, "set_at": now_iso(), "by": by, "channel": channel, "history": hist[-20:]})
        self.hub.store.state_set("manager", st)
        self.hub.publish(make_event("manager.designated", agent_id or prev, {"agent_id": agent_id or None, "previous": prev, "by": by, "channel": channel}))
        return self.get()

    def get(self) -> dict[str, Any]:
        tk = self.hub.store.state_get("manager_tick") or {}
        aid = self.agent_id()
        active = self._active_runs(aid) if aid else []
        return {
            "agent_id": aid,
            "conversations": self.conversations(None, limit=50),
            "last_tick": tk.get("last_tick"),
            "next_tick": self._next_tick(tk) if aid and self.cfg("heartbeat", True) else None,
            "tick": {"heartbeat": bool(self.cfg("heartbeat", True)), "interval_s": float(self.cfg("tick_interval_s", 1800)),
                     "pending_events": len(self.pending), "last_tick_run_id": tk.get("last_run_id"), "last_reason": tk.get("last_reason"),
                     "last_skip": tk.get("last_skip"), "running": any((r.get("run_opts") or {}).get("manager_kind") == "tick" for r in active)},
            "designated_at": self.state().get("set_at"),
        }

    # ------------------------------------------------------------------ hội thoại
    def _conv_row(self, cid: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM manager_conversations WHERE id=?", (cid,)).fetchone()
        if r is None:
            raise NotFound(f"không có hội thoại '{cid}'")
        return dict(r)

    def _conv_out(self, c: dict[str, Any]) -> dict[str, Any]:
        n = self.conn.execute("SELECT COUNT(*) FROM manager_messages WHERE conversation_id=?", (c["id"],)).fetchone()[0]
        return {"id": c["id"], "channel": c["channel"], "agent_id": c["agent_id"], "session_id": c.get("session_id"), "turns": c.get("turns"),
                "title": c.get("title"), "messages": int(n), "last_run_id": c.get("last_run_id"), "created_at": c["created_at"], "updated_at": c["updated_at"]}

    def conversations(self, channel: str | None = None, *, limit: int = 100, all_managers: bool = False) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM manager_conversations WHERE archived_at IS NULL", []
        if not all_managers:  # MGR-11: mặc định chỉ hội thoại của điều phối viên HIỆN TẠI (web/Telegram không mở nhầm hội thoại cũ)
            sql += " AND agent_id=?"
            args.append(self.agent_id() or "")
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        return [self._conv_out(dict(r)) for r in self.conn.execute(sql, args)]

    def history(self, cid: str, limit: int = 100) -> list[dict[str, Any]]:
        self._conv_row(cid)
        rows = self.conn.execute("SELECT * FROM (SELECT * FROM manager_messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                                 (cid, int(limit))).fetchall()
        return [{"id": r["id"], "role": r["role"], "text": r["text"], "run_id": r["run_id"], "ts": r["ts"]} for r in rows]

    def _new_conv(self, channel: str, agent_id: str, title: str | None) -> dict[str, Any]:
        cid = "mc_" + secrets.token_hex(5)
        now = now_iso()
        self.conn.execute("INSERT INTO manager_conversations (id, channel, agent_id, title, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                          (cid, channel, agent_id, (title or "")[:80] or None, now, now))
        return self._conv_row(cid)

    def _add_msg(self, cid: str, role: str, text: str, run_id: str | None) -> None:
        self.conn.execute("INSERT INTO manager_messages (conversation_id, role, text, run_id, ts) VALUES (?,?,?,?,?)",
                          (cid, role, text[:20000], run_id, now_iso()))
        self.conn.execute("UPDATE manager_conversations SET updated_at=? WHERE id=?", (now_iso(), cid))

    def _conv_cwd(self, agent_id: str, cid: str) -> str:
        home = Path(self.hub.agents.get_row(agent_id)["home_path"])
        d = home / "workspaces" / f"conv-{cid}"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def _active_runs(self, agent_id: str | None, cid: str | None = None) -> list[dict[str, Any]]:
        if not agent_id:
            return []
        rows = self.conn.execute(f"SELECT id FROM runs WHERE agent_id=? AND kind='manager' AND status IN ({','.join('?' * len(LIVE))})",
                                 (agent_id, *LIVE)).fetchall()
        out = [self.hub.store.get_run(r[0]) or {} for r in rows]
        if cid:
            out = [r for r in out if (r.get("run_opts") or {}).get("manager_conversation") == cid]
        return out

    def _submit(self, conv: dict[str, Any], prompt: str, *, kind: str, budget: float | None) -> dict[str, Any]:
        aid = conv["agent_id"]
        why = self.perms.manager_config_problem(self.hub.agents.get_row(aid))  # MGR-8: cấu hình bị nới sau khi chỉ định → không chạy
        if why:
            raise Blocked(f"điều phối viên '{aid}' có cấu hình vượt khung: {why}", data={"reason": "manager_config"})
        resume = conv.get("session_id")
        carry = ""
        if resume and int(conv.get("session_turns") or 0) >= int(self.cfg("rotate_after_turns", 80)):
            resume = None  # xoay session: claude autocompact lo ngữ cảnh dài trong session; ở đây giới hạn số lượt/session
        if resume and not self.hub.store.get_session(resume):
            resume = None
        if not resume and int(conv.get("turns") or 0) > 0:
            carry = prompts.carry_over(self.history(conv["id"], limit=6))
        opts = {"hub_self": "manager", "manager_conversation": conv["id"], "manager_kind": kind}
        run = self.hub.runs.submit(aid, carry + prompt, cwd=self._conv_cwd(aid, conv["id"]), budget_usd=budget, kind="manager",
                                   resume_session_id=resume, resume_reason="manager_conversation" if resume else None, run_opts=opts,
                                   manager_issue=True)
        st = 0 if not resume else int(conv.get("session_turns") or 0)
        self.conn.execute("UPDATE manager_conversations SET session_id=?, last_run_id=?, session_turns=?, updated_at=? WHERE id=?",
                          (run["session_id"], run["id"], st, now_iso(), conv["id"]))
        return run

    async def chat(self, channel: str, text: str, *, conversation_id: str | None = None, reuse_latest: bool = False) -> dict[str, Any]:
        aid = self.agent_id()
        if not aid:
            raise Conflict("chưa có điều phối viên — chỉ định bằng `cfharness manager set <agent>`")
        text = str(text or "").strip()
        channel = str(channel or "cli")
        if not text:
            raise HubError("thiếu 'text'")
        if channel == HEARTBEAT_CHANNEL:
            raise HubError("kênh 'heartbeat' dành cho nhịp tự động")
        conv = None
        switched_from = None
        if conversation_id:
            conv = self._conv_row(conversation_id)
            if conv["channel"] != channel:
                raise Conflict(f"hội thoại {conversation_id} thuộc kênh '{conv['channel']}', không phải '{channel}'")
            if conv["agent_id"] != aid:
                # MGR-11: đã đổi điều phối viên → chuyển sang hội thoại của manager mới trên cùng kênh (UI dùng conversation_id trả về)
                switched_from, conv, reuse_latest = conversation_id, None, True
        if conv is None and reuse_latest:
            r = self.conn.execute("SELECT id FROM manager_conversations WHERE channel=? AND agent_id=? AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 1",
                                  (channel, aid)).fetchone()
            conv = self._conv_row(r[0]) if r else None
        if conv is None:
            conv = self._new_conv(channel, aid, text.splitlines()[0])
        busy = self._active_runs(aid, conv["id"])
        if busy:
            # run của hội thoại đang chạy → gửi tin vào run đó (steer) thay vì chồng run thứ hai cùng session
            from ..steer import steer

            r = busy[0]
            if r["status"] != "running":
                raise Conflict(f"điều phối viên đang xử lý tin trước của hội thoại này (run {r['id']}, {r['status']}) — thử lại sau")
            try:
                res = await steer(self.hub, r["id"], self.render_prompt(r["id"], prompts.chat_prompt(channel, conv["id"], text)), by="user", channel=channel)
            except Conflict:
                raise Conflict(f"điều phối viên đang kết thúc lượt trả lời trước (run {r['id']}) — gửi lại sau giây lát") from None
            self._add_msg(conv["id"], "user", text, r["id"])
            return {"conversation_id": conv["id"], "run_id": r["id"], "steered": True, "seq": (res or {}).get("seq")}
        run = self._submit(conv, prompts.chat_prompt(channel, conv["id"], text), kind="chat", budget=self.cfg("chat_budget_usd"))
        self._add_msg(conv["id"], "user", text, run["id"])
        self.conn.execute("UPDATE manager_conversations SET turns=turns+1 WHERE id=?", (conv["id"],))
        out = {"conversation_id": conv["id"], "run_id": run["id"], "session_id": run["session_id"], "resumed": bool(run.get("resumed_from_session"))}
        if switched_from:
            out["switched_from"] = switched_from
        return out

    def _home_conversation(self) -> dict[str, Any] | None:
        """Hội thoại của NGƯỜI hoạt động gần nhất (đích của notify_owner trong tick)."""
        aid = self.agent_id()
        r = self.conn.execute("SELECT id FROM manager_conversations WHERE agent_id=? AND channel != ? AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 1",
                              (aid, HEARTBEAT_CHANNEL)).fetchone()
        return self._conv_row(r[0]) if r else None

    # ------------------------------------------------------------------ kết thúc run → manager.reply
    def _on_event(self, ev: HubEvent) -> None:
        try:
            if ev.type in TERMINAL_RUN_EVENTS and ev.source == "hub" and ev.run_id:
                run = self.hub.store.get_run(ev.run_id) or {}
                if (run.get("run_opts") or {}).get("manager_conversation") and ev.type != "run.waiting_human" and self.is_issued(run["id"]):
                    self._finish_run(run)
                    return
            if ev.type == "agent.purged" and ev.agent_id:  # MGR-12: dọn trạng thái manager gắn theo id
                self.perms.forget_agent(ev.agent_id)
            self._collect(ev)
        except Exception:  # pragma: no cover — listener không được làm hỏng bus
            log.exception("manager listener lỗi với %s", ev.type)

    def _finish_run(self, run: dict[str, Any]) -> None:
        opts = run.get("run_opts") or {}
        cid = opts["manager_conversation"]
        try:
            conv = self._conv_row(cid)
        except NotFound:
            return
        outcome = run.get("outcome") or run.get("status")
        ok = outcome == "completed"
        if ok:
            text = str(run.get("summary") or "").strip() or "(không có nội dung)"
        else:
            text = f"(Điều phối viên không trả lời được: run {run['id']} kết thúc '{outcome}'" + (f" — {str(run.get('error'))[:300]}" if run.get("error") else "") + ")"
        self._add_msg(cid, "manager", text, run["id"])
        sess = run.get("session_id") if ok or outcome == "cancelled" else None  # lỗi → session mới lần sau (mang theo tóm tắt)
        self.conn.execute("UPDATE manager_conversations SET session_id=?, session_turns=session_turns+1, last_run_id=?, updated_at=? WHERE id=?",
                          (sess, run["id"], now_iso(), cid))
        kind = opts.get("manager_kind") or "chat"
        if ok:
            # manager đã nhìn trạng thái khi soạn câu trả lời → bỏ sự kiện "xong" xảy ra trước đó (không tốn một tick chỉ để NO_ACTION)
            end = str(run.get("ended_at") or now_iso())
            keep = [e for e in self.pending if not (e["kind"] in ("task_done", "run_done") and str(e["ts"]) <= end)]
            if len(keep) != len(self.pending):
                self.pending = keep
                if not keep:
                    self._first_pending = None
        self.hub.publish(make_event("manager.reply", run["agent_id"], {"conversation_id": cid, "run_id": run["id"], "text": text[:TEXT_MAX],
                                                                       "channel": conv["channel"], "kind": "tick" if kind == "tick" else "reply", "ok": ok,
                                                                       "outcome": outcome}, run_id=run["id"]))
        if kind == "tick":
            tk = dict(self.hub.store.state_get("manager_tick") or {})
            tk.update({"last_result": text[:300], "last_outcome": outcome})
            self.hub.store.state_set("manager_tick", tk)

    # ------------------------------------------------------------------ nhịp: gom sự kiện
    def _push(self, kind: str, key: str, summary: str, **data: Any) -> None:
        if key in self._pending_keys:
            return
        self._pending_keys.add(key)
        if self._first_pending is None:
            self._first_pending = time.monotonic()
        self.pending.append({"kind": kind, "summary": summary[:300], "ts": now_iso(), **data})

    def _collect(self, ev: HubEvent) -> None:
        aid = self.agent_id()
        if not aid or ev.agent_id == aid:
            return
        p = ev.payload or {}
        if ev.type == "task.status":
            to = p.get("to")
            t = self.hub.tasks.maybe(ev.task_id) if ev.task_id else None
            if t is None:
                return
            if to == "blocked":
                qs = getattr(self.hub, "questions", None)
                if qs is not None and qs.pending_for_task(t["id"]):
                    return  # UX: task chờ câu trả lời — đã có sự kiện `question` (với manager) hoặc đã hỏi người
                self._push("task_blocked", f"tb:{t['id']}:{t.get('updated_at')}", f"task {t['id']} '{t['title'][:80]}' BLOCKED: {p.get('reason') or t.get('block_reason') or ''}",
                           task_id=t["id"], team_id=t.get("team_id"))
            elif to == "done" and (not t.get("origin_task_id") or str(t.get("created_by") or "").startswith("manager:")):
                self._push("task_done", f"td:{t['id']}", f"task {t['id']} '{t['title'][:80]}' DONE: {str(t.get('result_summary') or '')[:200]}",
                           task_id=t["id"], team_id=t.get("team_id"))
        elif ev.type == "task.circuit_open":
            self._push("task_circuit_open", f"tc:{ev.task_id}:{ev.seq}", f"task {ev.task_id}: circuit breaker opened (repeated failures)", task_id=ev.task_id)
        elif ev.type in ("run.failed",) and ev.source == "hub":
            oc = p.get("outcome")
            if oc in ("crashed", "failed", "timed_out", "protocol_violation") and ev.agent_id:
                now = time.monotonic()
                ts = [x for x in self._fail_times.get(ev.agent_id, []) if now - x < 3600] + [now]
                self._fail_times[ev.agent_id] = ts
                if len(ts) >= int(self.cfg("crash_repeat", 2)):
                    self._push("run_failing", f"rf:{ev.agent_id}:{len(ts) // int(self.cfg('crash_repeat', 2))}",
                               f"agent {ev.agent_id}: {len(ts)} failed runs in the last hour (last {ev.run_id}: {oc})", agent_id=ev.agent_id, run_id=ev.run_id)
        elif ev.type == "run.completed" and ev.source == "hub" and ev.run_id:
            tracked = set((self.hub.store.state_get("manager_tracked") or {}).get("runs") or [])
            if ev.run_id in tracked:
                self._push("run_done", f"rd:{ev.run_id}", f"run {ev.run_id} of {ev.agent_id} finished: {str(p.get('summary') or '')[:200]}", run_id=ev.run_id)
        elif ev.type == "learning.digest":
            self._push("digest", f"dg:{p.get('id') or ev.seq}", "a new learning digest is available (digest_get)")

    def _scan_approvals(self) -> None:
        mins = float(self.cfg("approval_wait_min", 15))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=mins)
        for a in self.hub.approvals.list("pending", limit=50):
            if a["id"] in self._reported_approvals:
                continue
            t = _parse(a.get("opened_at"))
            if t is not None and t <= cutoff:
                self._reported_approvals.add(a["id"])
                self._push("approval_waiting", f"ap:{a['id']}", f"approval {a['id']} ({a.get('agent_id')}: {a.get('tool_name')} `{str(a.get('preview') or '')[:120]}`) "
                           f"waiting for the human > {int(mins)} min", approval_id=a["id"])

    # ------------------------------------------------------------------ nhịp: vòng nền + tick
    def start(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, float(self.cfg("check_s", 10))))
            try:
                await self.maybe_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("manager tick lỗi")

    def _next_tick(self, tk: dict[str, Any]) -> str | None:
        last = _parse(tk.get("last_check") or tk.get("last_tick"))
        if last is None:
            return iso()
        return iso(last + timedelta(seconds=float(self.cfg("tick_interval_s", 1800))))

    def _open_work(self) -> int:
        """Việc manager đang theo dõi: task do manager giao chưa xong (không tính blocked — đã báo bằng sự kiện)."""
        r = self.conn.execute("SELECT COUNT(*) FROM tasks WHERE created_by LIKE 'manager:%' AND status NOT IN ('done','archived','blocked') AND deleted_at IS NULL").fetchone()
        return int(r[0])

    def _skip(self, reason: str) -> dict[str, Any]:
        tk = dict(self.hub.store.state_get("manager_tick") or {})
        if tk.get("last_skip") != reason:
            tk["last_skip"] = reason
            self.hub.store.state_set("manager_tick", tk)
        return {"run_id": None, "skipped": reason, "pending_events": len(self.pending)}

    async def maybe_tick(self, *, force: bool = False) -> dict[str, Any]:
        aid = self.agent_id()
        if not aid:
            return {"run_id": None, "skipped": "no_manager"}
        if not force and not self.cfg("heartbeat", True):
            return self._skip("heartbeat_off")
        self._scan_approvals()
        if self.hub.runs.estop.get("on"):
            return self._skip("estop")
        row = self.hub.store.get_agent(aid)
        if row is None or row.get("archived_at"):
            return self._skip("manager_missing")
        if row.get("status") == "paused":
            return self._skip("manager_paused")
        async with self._tick_lock:
            if self._active_runs(aid):
                return self._skip("manager_busy")  # khoá chống chồng: sự kiện giữ lại cho tick sau
            tk = dict(self.hub.store.state_get("manager_tick") or {})
            now = datetime.now(timezone.utc)
            due_events = bool(self.pending) and (force or (time.monotonic() - (self._first_pending or 0)) >= float(self.cfg("event_debounce_s", 45)))
            nxt = _parse(self._next_tick(tk))
            periodic = nxt is not None and now >= nxt
            if not (due_events or periodic or force):
                return {"run_id": None, "skipped": "not_due", "pending_events": len(self.pending)}
            work = self._open_work()
            if not self.pending and not work and not force:
                tk.update({"last_check": iso(now), "last_skip": "nothing_to_do"})
                self.hub.store.state_set("manager_tick", tk)
                self.hub.publish(make_event("manager.tick", aid, {"status": "skipped", "reason": "nothing_to_do"}))
                return {"run_id": None, "skipped": "nothing_to_do"}
            return self._tick(aid, tk, reason="events" if self.pending else ("forced" if force else "periodic"), open_work=work)

    def _tick(self, aid: str, tk: dict[str, Any], *, reason: str, open_work: int) -> dict[str, Any]:
        cap = int(self.cfg("max_pending_events", 40))
        events, dropped = self.pending[:cap], max(0, len(self.pending) - cap)
        r = self.conn.execute("SELECT id FROM manager_conversations WHERE channel=? AND agent_id=? AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 1",
                              (HEARTBEAT_CHANNEL, aid)).fetchone()
        conv = self._conv_row(r[0]) if r else self._new_conv(HEARTBEAT_CHANNEL, aid, "heartbeat")
        prompt = prompts.tick_prompt(events, dropped=dropped, reason=reason, open_work=open_work)
        try:
            run = self._submit(conv, prompt, kind="tick", budget=self.cfg("tick_budget_usd"))
        except HubError as e:
            self.hub.publish(make_event("manager.tick", aid, {"status": "failed", "reason": e.message[:200]}))
            return self._skip("submit_failed: " + e.message[:80])
        self.pending, self._pending_keys, self._first_pending = [], set(), None
        self._add_msg(conv["id"], "tick", prompts.tick_digest(events, dropped, reason), run["id"])
        self.conn.execute("UPDATE manager_conversations SET turns=turns+1 WHERE id=?", (conv["id"],))
        now = iso()
        tk.update({"last_tick": now, "last_check": now, "last_run_id": run["id"], "last_reason": reason, "last_skip": None, "events": len(events)})
        self.hub.store.state_set("manager_tick", tk)
        self.hub.publish(make_event("manager.tick", aid, {"status": "started", "reason": reason, "events": len(events), "dropped": dropped,
                                                         "conversation_id": conv["id"]}, run_id=run["id"]))
        return {"run_id": run["id"], "conversation_id": conv["id"], "reason": reason, "events": len(events)}

    # ------------------------------------------------------------------ kiểm quyền (daemon, lớp 2)
    def check_rpc(self, caller: "_auth.Caller", method: str, params: dict[str, Any]) -> str | None:
        if not self.is_manager(caller.manager):
            return f"'{caller.manager}' không còn là điều phối viên được chỉ định"
        return self.perms.check_rpc(caller, method, params)

    # ------------------------------------------------------------------ tool hub-self (RPC managerself.call)
    async def self_call(self, run_id: str, op: str, args: dict[str, Any]) -> Any:
        from .ops import ManagerOps

        run = self.hub.store.get_run(str(run_id or ""))
        if run is None or run["status"] not in ("running", "queued"):
            raise HubError("run không đang chạy — tool điều phối viên chỉ dùng trong run đang chạy")
        if (run.get("run_opts") or {}).get("hub_self") != "manager" or not self.is_issued(run["id"]):
            raise Blocked("run này không phải run điều phối viên (chỉ run do hội thoại/nhịp của hub tạo)")
        mid = run["agent_id"]
        caller = _auth.current_caller()
        if caller is not None and caller.via == "socket" and caller.run_id != run["id"]:
            # chỉ tiến trình hub-self con của CHÍNH run manager được gọi (pid ngang hàng → cây tiến trình/env)
            info = {"op": op, "claimed_run": run["id"], **caller.to_dict()}
            self.hub.audit.record("manager.denied", caller.agent_id, caller.run_id, info, actor=f"agent:{caller.agent_id}" if caller.agent_id else "unknown",
                                  channel="socket", target=f"run:{run['id']}", summary="managerself.call từ tiến trình không thuộc run manager")
            self.hub.publish(make_event("auth.denied", caller.agent_id, {"method": "managerself.call", **info}, run_id=caller.run_id, source="hub"))
            raise Blocked("managerself.call chỉ dành cho hub-self của chính run điều phối viên")
        if not self.is_manager(mid):
            raise Blocked(f"agent '{mid}' không còn là điều phối viên được chỉ định")
        return await ManagerOps(self, run).call(op, dict(args or {}))
