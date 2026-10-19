"""Khôi phục sau khi daemon khởi động lại (FR-RT-19, Q-09) + dọn thùng rác hết hạn (FR-SG-06).

`reconcile(hub)` chạy trong `Hub.start()` (epoch mới):
1. Run `running` còn trong DB (daemon chết/kill): đối soát tiến trình bằng `(pid, start_time)` — chỉ khi pid còn sống
   VÀ thời điểm khởi động của nó khớp `runs.pid_start_time` (±`recovery.pid_start_tolerance_s`) mới coi là tiến trình
   mồ côi của run → dừng cả process group (SIGINT → SIGTERM → SIGKILL). pid đã bị tái sử dụng → không đụng.
   Run → `interrupted`. Run có approval đang chờ → `waiting_human` (KHÔNG resume cho tới khi có người).
2. Run trong danh sách `pending_resume` (daemon dừng có kiểm soát khi run đang chạy) → như trên.
3. Tự resume (`recovery.auto_resume`, trừ ESTOP/agent paused/agent đã xoá): run mới `--resume <session>` cùng cwd với
   lời nhắc tiếp tục chuẩn; run chưa kịp có session (chưa start) → chạy lại prompt gốc.
4. Run `queued` → xếp lại hàng đợi theo thứ tự cũ.

Retention (hardening H-09, NFR-28/Q-10/Q-17) — `purge_retention(hub, now)`, gọi từ `purge_expired` (vòng nền ~30 s của
approvals) tối đa 1 lần / `retention.interval_s`:
- `events` cũ hơn `retention.events_days` (30; 0/null = vĩnh viễn) — gồm transcript (event text/tool của run). Trigger
  append-only chỉ cho xoá hàng cũ hơn mốc `retention_cutoff('events')` do job này đặt (migration v12).
- stdout thô `runs/<id>.jsonl(.gz)` + `runs/<id>.stderr.log` cũ hơn `retention.raw_days` (30), trừ run đang chạy.
- `checkpoints/<run>/` (bản sao file trước/sau run để diff/revert) cũ hơn `retention.checkpoints_days` (30) tính từ lúc run
  kết thúc. GIỮ checkpoint của run chưa kết thúc hẳn (`queued|running|waiting_human|interrupted` — còn resume/đối soát file).
  Xoá xong → `runs.diff.revertible=false` + `checkpoint_purged_at`: cửa sổ `run revert` = 30 ngày (danh sách file/stat vẫn giữ).
- Xoá mềm quá `purge_after` (Q-17, `retention.auto_purge`): thư mục `.archive` của skill (state → `deleted`; nội dung vẫn
  khôi phục được từ blob ledger) và thùng rác team. Agent: `purge_expired` như M1-B.
- GIỮ vĩnh viễn: `runs` (cost tổng hợp), `ledger_entries`/`snapshots`/blob, `audit_log`, `guard_decisions`, `signals`.
Mỗi lần có xoá → audit `retention.purged`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import HubError
from .events import make_event
from .oscompat import IS_WINDOWS, kill_tree, pid_alive, win_start_time
from .store import now_iso

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("cfharness.recovery")


def pid_start_time(pid: int) -> float | None:
    """Thời điểm tiến trình bắt đầu (epoch giây, độ phân giải 1 s) theo `ps -o lstart=`; None nếu không còn.
    Windows: `GetProcessTimes`."""
    if IS_WINDOWS:
        return win_start_time(pid)
    if not pid_alive(pid):
        return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5, env={**os.environ, "LC_ALL": "C", "LANG": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    s = out.stdout.strip()
    if out.returncode != 0 or not s:
        return None
    try:
        return time.mktime(time.strptime(" ".join(s.split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def process_matches(pid: int | None, recorded_start: float | None, tolerance_s: float = 5.0) -> bool:
    if not pid or recorded_start is None:
        return False
    st = pid_start_time(int(pid))
    return st is not None and abs(st - float(recorded_start)) <= tolerance_s


def kill_group(pid: int, grace_s: float = 3.0) -> bool:
    """Dừng cả process group của tiến trình mồ côi. Trả True nếu đã gửi tín hiệu.
    Windows: Job Object của run đã mất cùng daemon cũ → giết cả cây theo ppid. KHÔNG gửi CTRL_BREAK: với pid không
    chắc là gốc process group, GenerateConsoleCtrlEvent phát cho MỌI tiến trình chung console (kể cả chính hub)."""
    if IS_WINDOWS:
        if not pid_alive(pid):
            return False
        kill_tree(pid)
        return True
    sent = False
    for sig, wait in ((signal.SIGINT, grace_s), (signal.SIGTERM, grace_s), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(pid, sig)
            sent = True
        except ProcessLookupError:
            return sent
        except PermissionError:
            try:
                os.kill(pid, sig)
                sent = True
            except OSError:
                return sent
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return sent
            time.sleep(0.05)
    return sent


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def reconcile(hub: "Hub") -> dict[str, Any]:
    store, runs = hub.store, hub.runs
    tol = float(hub.config.get("recovery.pid_start_tolerance_s"))
    to_resume: list[dict[str, Any]] = []
    interrupted = killed = waiting = 0
    for run in store.list_runs(status="running", limit=10_000):
        pid = run.get("pid")
        if pid and process_matches(pid, run.get("pid_start_time"), tol):
            if kill_group(int(pid)):
                killed += 1
        if hub.approvals.pending_for_run(run["id"]):
            for a in hub.approvals.pending_for_run(run["id"]):
                hub.approvals.to_waiting_human(a["id"])
            store.update_run(run["id"], {"status": "waiting_human", "ended_at": now_iso(), "error": "daemon khởi động lại khi đang chờ duyệt"})
            waiting += 1
            continue
        store.update_run(run["id"], {"status": "interrupted", "outcome": "interrupted", "ended_at": now_iso(), "error": "daemon khởi động lại (tiến trình mồ côi đã dừng)" if pid else "daemon khởi động lại"})
        hub.publish(make_event("run.interrupted", run["agent_id"], {"outcome": "interrupted", "error": "daemon restart", "orphan_killed": bool(pid)}, run_id=run["id"]))
        interrupted += 1
        to_resume.append(store.get_run(run["id"]))  # type: ignore[arg-type]
    for rid in store.state_get("pending_resume", []) or []:
        r = store.get_run(rid)
        if r and r["status"] == "interrupted" and not r.get("continued_in_run_id"):
            to_resume.append(r)
    store.state_set("pending_resume", [])
    # agent 'working' cũ không còn đúng
    for a in store.list_agents(include_archived=True):
        if a["status"] == "working":
            store.update_agent(a["id"], {"status": "idle", "current_run_id": None})
    resumed: list[str] = []
    requeued: list[str] = []
    if _loop_running():
        queued = sorted(store.list_runs(status="queued", limit=10_000), key=lambda r: r["queued_at"])
        for r in queued:
            if r.get("kind") == "eval":  # M4-B: bản sao home tạm của eval đã mất khi daemon dừng → huỷ, gate chạy lại sau
                store.update_run(r["id"], {"status": "cancelled", "outcome": "cancelled", "ended_at": now_iso(), "error": "eval bị huỷ do daemon khởi động lại"})
                continue
            runs.queue.append(r["id"])
            runs.waiters[r["id"]] = asyncio.Event()
            requeued.append(r["id"])
        if hub.config.get("recovery.auto_resume"):
            for r in to_resume:
                nr = resume_interrupted(hub, r)
                if nr:
                    resumed.append(nr["id"])
        runs.tick()
    out = {"interrupted": interrupted, "orphans_killed": killed, "waiting_human": waiting, "resumed": resumed, "requeued": requeued}
    if interrupted or resumed or requeued or waiting:
        hub.publish(make_event("recovery.reconciled", None, out))
        log.info("khôi phục: %s", out)
    return out


def resume_interrupted(hub: "Hub", run: dict[str, Any]) -> dict[str, Any] | None:
    from .runs import RESUME_INTERRUPTED_PROMPT

    if hub.runs.estop.get("on"):
        return None
    if run.get("kind") == "eval":  # M4-B: run eval --no-session-persistence, home tạm đã mất → không resume
        return None
    ag = hub.store.get_agent(run["agent_id"])
    if ag is None or ag.get("archived_at") or ag["status"] == "paused":
        return None
    from .workspace import resume_conflicts  # M3: người đã sửa file của run trong lúc gián đoạn → không resume

    changed = resume_conflicts(run)
    if changed:
        hub.publish(make_event("error", run["agent_id"], {"code": "workspace_changed", "message": f"không tự resume: {len(changed)} file của run đã bị sửa sau khi gián đoạn", "files": changed[:20]}, run_id=run["id"]))
        return None
    try:
        if run.get("started_at") and run.get("session_id"):
            nr = hub.runs.submit(
                run["agent_id"], RESUME_INTERRUPTED_PROMPT, cwd=run.get("cwd"), budget_usd=run.get("max_budget_usd"), model=run.get("model"),
                kind=run.get("kind") or "adhoc", resume_session_id=run["session_id"], parent_run_id=run["id"], resume_reason="daemon_restart",
            )
        else:
            nr = hub.runs.submit(run["agent_id"], run.get("prompt") or "", cwd=run.get("cwd"), budget_usd=run.get("max_budget_usd"), model=run.get("model"), kind=run.get("kind") or "adhoc", parent_run_id=run["id"], resume_reason="daemon_restart")
    except HubError as e:
        hub.publish(make_event("error", run["agent_id"], {"code": "auto_resume_failed", "message": e.message}, run_id=run["id"]))
        return None
    hub.store.update_run(run["id"], {"continued_in_run_id": nr["id"]})
    hub.publish(make_event("run.resumed", run["agent_id"], {"from_run_id": run["id"], "run_id": nr["id"], "reason": "daemon_restart"}, run_id=run["id"], session_id=run.get("session_id")))
    return nr


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _due(ts: str | None, now: datetime) -> bool:
    if not ts:
        return False
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) <= now
    except ValueError:
        return False


def purge_retention(hub: "Hub", now: datetime | None = None) -> dict[str, Any]:
    """Retention 30 ngày (H-09): event/transcript, stdout thô, xoá mềm skill/team quá hạn. Trả số mục đã xoá."""
    import shutil

    from .approvals import audit_record

    now = now or datetime.now(timezone.utc)
    cfg = hub.config.get
    conn = hub.store.conn
    out: dict[str, Any] = {"events": 0, "raw_files": 0, "checkpoints": 0, "skills": 0, "teams": 0}
    days = cfg("retention.events_days")
    if days:
        cutoff = _iso(now - timedelta(days=float(days)))
        with hub.store.tx():
            conn.execute("INSERT INTO retention_cutoff (name, cutoff, updated_at) VALUES ('events', ?, ?) "
                         "ON CONFLICT(name) DO UPDATE SET cutoff=excluded.cutoff, updated_at=excluded.updated_at", (cutoff, now_iso()))
            out["events"] = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
        out["events_cutoff"] = cutoff
    raw_days = cfg("retention.raw_days")
    runs_dir = hub.paths.runs_dir
    if raw_days and runs_dir.is_dir():
        limit = (now - timedelta(days=float(raw_days))).timestamp()
        active = set(getattr(hub.runs, "active", {}) or {})
        for p in runs_dir.iterdir():
            if not p.is_file() or not p.name.endswith((".jsonl", ".jsonl.gz", ".stderr.log")):
                continue
            if p.name.split(".", 1)[0] in active:
                continue
            try:
                if p.stat().st_mtime < limit:
                    p.unlink()
                    out["raw_files"] += 1
            except OSError:
                continue
    cp_days = cfg("retention.checkpoints_days")
    cp_dir = hub.paths.home / "checkpoints"
    if cp_days and cp_dir.is_dir():
        limit_dt = now - timedelta(days=float(cp_days))
        keep_status = ("queued", "running", "waiting_human", "interrupted")
        active = set(getattr(hub.runs, "active", {}) or {})
        for d in cp_dir.iterdir():
            if not d.is_dir() or d.name in active:
                continue
            run = hub.store.get_run(d.name)
            if run is not None and run.get("status") in keep_status:
                continue  # run còn có thể resume — resume_conflicts cần checkpoint
            ended = (run or {}).get("ended_at") or (run or {}).get("queued_at")
            try:
                old = datetime.fromisoformat(ended.replace("Z", "+00:00")) < limit_dt if ended else d.stat().st_mtime < limit_dt.timestamp()
            except (ValueError, OSError):
                continue
            if not old:
                continue
            shutil.rmtree(d, ignore_errors=True)
            out["checkpoints"] += 1
            if run is not None and isinstance(run.get("diff"), dict):
                hub.store.update_run(run["id"], {"diff": {**run["diff"], "revertible": False, "checkpoint_purged_at": now_iso()}})
    if cfg("retention.auto_purge"):
        for r in conn.execute("SELECT scope, owner_id, name, archived_path, purge_after FROM skills WHERE state='archived' AND pinned=0 "
                              "AND purge_after IS NOT NULL").fetchall():
            if not _due(r["purge_after"], now):
                continue
            if r["archived_path"]:
                shutil.rmtree(r["archived_path"], ignore_errors=True)
            conn.execute("UPDATE skills SET state='deleted', archived_path=NULL, purge_after=NULL, updated_at=? WHERE scope=? AND owner_id=? AND name=?",
                         (now_iso(), r["scope"], r["owner_id"], r["name"]))
            out["skills"] += 1
        for r in conn.execute("SELECT id, trash_path, purge_after FROM teams WHERE deleted_at IS NOT NULL AND trash_path IS NOT NULL").fetchall():
            if not _due(r["purge_after"], now):
                continue
            shutil.rmtree(r["trash_path"], ignore_errors=True)
            conn.execute("UPDATE teams SET trash_path=NULL, purge_after=NULL, updated_at=? WHERE id=?", (now_iso(), r["id"]))
            out["teams"] += 1
    if any(out[k] for k in ("events", "raw_files", "checkpoints", "skills", "teams")):
        audit_record("retention.purged", None, None, {**out, "actor": "retention", "channel": "hub"})
        log.info("retention: %s", out)
    return out


def purge_expired(hub: "Hub", now: datetime | None = None) -> list[str]:
    """Purge tự động agent đã xoá mềm quá `retention.soft_delete_days` (chính sách do NGƯỜI đặt; có audit).
    Kèm retention 30 ngày (H-09) tối đa 1 lần / `retention.interval_s`."""
    try:
        last = float(hub.store.state_get("retention_last_run", 0) or 0)
        if time.time() - last >= float(hub.config.get("retention.interval_s") or 3600):
            hub.store.state_set("retention_last_run", time.time())
            purge_retention(hub)
    except Exception:  # pragma: no cover - retention lỗi không chặn purge agent
        log.exception("retention lỗi")
    if not hub.config.get("retention.auto_purge"):
        return []
    now = now or datetime.now(timezone.utc)
    out = []
    for a in hub.store.list_agents(include_archived=True):
        pa = a.get("purge_after")
        if not a.get("deleted_at") or not pa or a.get("status") == "purged":
            continue
        try:
            due = datetime.fromisoformat(pa.replace("Z", "+00:00")) <= now
        except ValueError:
            continue
        if due:
            purge_agent(hub, a["id"], by="retention", channel="hub")
            out.append(a["id"])
    return out


def purge_agent(hub: "Hub", agent_id: str, *, by: str, channel: str) -> dict[str, Any]:
    """Xoá cứng thùng rác của agent đã xoá mềm (FR-SG-06). Là thao tác PHÁ HUỶ: chỉ người (CLI gõ lại id) hoặc
    chính sách retention; agent khởi xướng thì đi qua ApprovalRequest (guard)."""
    import shutil

    from .approvals import audit_record
    from .errors import NotFound

    from .auth import require_human

    require_human("purge agent")  # V2-N1 (SEC): cổng ở tầng service — lời gọi nội bộ (vd trash.purge) không lách được
    a = hub.store.get_agent(agent_id)
    if a is None:
        raise NotFound(f"không tìm thấy agent '{agent_id}'")
    if not a.get("deleted_at"):
        raise HubError(f"agent '{agent_id}' chưa xoá mềm — `cfharness agent rm {agent_id}` trước")
    tp = a.get("trash_path")
    if tp:
        shutil.rmtree(tp, ignore_errors=True)
    hub.store.update_agent(agent_id, {"status": "purged", "trash_path": None, "purge_after": None, "updated_at": now_iso()})
    hub.publish(make_event("agent.purged", agent_id, {"agent_id": agent_id, "by": by, "trash_path": tp}))
    audit_record("agent.purged", agent_id, None, {"trash_path": tp, "actor": by, "channel": channel})
    return hub.store.get_agent(agent_id)  # type: ignore[return-value]
