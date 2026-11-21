"""Hàng đợi job (FR-SA-JOB, plan-superadmin §2.4).

Job = chuỗi bước. Mỗi bước có `check()` (đã xong trên máy thật chưa — đọc trạng thái thật), `apply()`, `undo()` (cho "Huỷ và dọn").
Chạy lại = bỏ qua bước `check()` đúng (idempotent, JOB-02). admind khởi động lại → job `running` thành `failed` + `resume_from`.
Tối đa `MAX_PARALLEL` job ghi song song; một đối tượng (`target`) chỉ một job chạy một lúc (JOB-03).
Secret của job (token Claude…) chỉ ở RAM (`_secrets`) — mất khi admind khởi động lại; thử lại phải nhập lại. Log bước đi qua bộ che.
"""

from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..redact import redact_str
from .db import dumps, loads, new_id

if TYPE_CHECKING:
    from .dispatch import State

log = logging.getLogger("cfharness.admind.jobs")

MAX_PARALLEL = 3
JOB_STATES = ("queued", "awaiting_approval", "running", "succeeded", "failed", "cancelled")
STEP_STATES = ("pending", "running", "done", "skipped", "failed", "undone")
LOG_MAX = 20_000


class StepError(Exception):
    """Lỗi có chủ đích của một bước — thông điệp hiện cho người (kèm cách sửa)."""


class Step:
    name = "step"
    title = "Bước"

    def check(self, jc: "JobCtx") -> bool:
        return False

    def apply(self, jc: "JobCtx") -> None:
        raise NotImplementedError

    def undo(self, jc: "JobCtx") -> None:
        return None


@dataclass
class JobCtx:
    state: "State"
    job_id: str
    kind: str
    target: str | None
    params: dict[str, Any]
    secrets: dict[str, str]
    data: dict[str, Any] = field(default_factory=dict)   # kết quả chia sẻ giữa các bước (lưu DB — không để secret ở đây)
    _lines: list[str] = field(default_factory=list)

    @property
    def runner(self) -> Any:
        return self.state.runner

    def log(self, msg: str) -> None:
        self._lines.append(self.mask(str(msg)))

    def mask(self, s: str) -> str:
        for v in self.secrets.values():
            if v and len(v) >= 6:
                s = s.replace(v, "***")
        return redact_str(s)


KINDS: dict[str, Callable[[dict[str, Any]], list[Step]]] = {}


def kind(name: str) -> Callable[[Callable[[dict[str, Any]], list[Step]]], Callable[[dict[str, Any]], list[Step]]]:
    def deco(fn: Callable[[dict[str, Any]], list[Step]]) -> Callable[[dict[str, Any]], list[Step]]:
        KINDS[name] = fn
        return fn

    return deco


class JobEngine:
    def __init__(self, state: "State", *, synchronous: bool = False) -> None:
        self.state = state
        self.synchronous = synchronous  # test: chạy ngay trong thread gọi
        self._secrets: dict[str, dict[str, str]] = {}
        self._sem = threading.BoundedSemaphore(MAX_PARALLEL)
        self._busy: set[str] = set()
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._recover()

    @property
    def db(self):  # noqa: ANN201
        return self.state.db

    def _recover(self) -> None:
        for j in self.db.all("SELECT id FROM jobs WHERE state='running'"):
            nxt = self.db.one("SELECT name FROM job_steps WHERE job_id=? AND state NOT IN ('done','skipped') ORDER BY idx LIMIT 1", (j["id"],))
            self.db.exec("UPDATE jobs SET state='failed', error='admind_restarted', resume_from=? WHERE id=?",
                         (nxt["name"] if nxt else None, j["id"]))
            self.db.exec("UPDATE job_steps SET state='failed' WHERE job_id=? AND state='running'", (j["id"],))

    # ------------------------------------------------------------------ tạo / đọc
    def submit(self, kind_name: str, *, target: str | None, params: dict[str, Any], created_by: str | None,
               secrets: dict[str, str] | None = None, approved_by: str | None = None) -> str:
        if kind_name not in KINDS:
            raise ValueError(f"loại job không có: {kind_name}")
        steps = KINDS[kind_name](params)
        jid = new_id("job")
        now = self.state.clock()
        with self.db.tx() as c:
            c.execute("INSERT INTO jobs(id, host_id, kind, target, params, state, created_by, approved_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (jid, self.db.host_id, kind_name, target, dumps({"params": params, "data": {}}), "queued", created_by, approved_by, now))
            for i, s in enumerate(steps):
                c.execute("INSERT INTO job_steps(job_id, idx, name, state) VALUES (?,?,?,'pending')", (jid, i, s.name))
        if secrets:
            self._secrets[jid] = dict(secrets)
        self._start(jid)
        return jid

    def get(self, jid: str) -> dict[str, Any] | None:
        j = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not j:
            return None
        blob = loads(j.pop("params"), {}) or {}
        j["params"] = blob.get("params", {})
        j["data"] = blob.get("data", {})
        j["steps"] = self.db.all("SELECT idx, name, state, started_at, finished_at, log FROM job_steps WHERE job_id=? ORDER BY idx", (jid,))
        titles = {s.name: s.title for s in KINDS.get(j["kind"], lambda p: [])(j["params"])} if j["kind"] in KINDS else {}
        for s in j["steps"]:
            s["title"] = titles.get(s["name"], s["name"])
        j["needs_secrets"] = bool(j["state"] == "failed" and j.get("params", {}).get("_needs_secrets") and jid not in self._secrets)
        return j

    def list(self, *, target: str | None = None, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q, p = ["1=1"], []
        if target:
            q.append("target=?"), p.append(target)
        if state:
            q.append("state=?"), p.append(state)
        return self.db.all(f"SELECT id, kind, target, state, created_by, created_at, started_at, finished_at, error, resume_from FROM jobs "
                           f"WHERE {' AND '.join(q)} ORDER BY created_at DESC LIMIT ?", (*p, limit))

    def busy(self, target: str) -> bool:
        return self.db.one("SELECT id FROM jobs WHERE target=? AND state IN ('queued','running')", (target,)) is not None

    # ------------------------------------------------------------------ chạy
    def _start(self, jid: str) -> None:
        if self.synchronous:
            self._run(jid)
            return
        t = threading.Thread(target=self._run, args=(jid,), name=f"job-{jid}", daemon=True)
        self._threads[jid] = t
        t.start()

    def wait(self, jid: str, timeout: float | None = None) -> None:
        t = self._threads.get(jid)
        if t:
            t.join(timeout)

    def _run(self, jid: str) -> None:
        j = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not j:
            return
        target = j["target"] or jid
        with self._sem:
            with self._lock:
                if target in self._busy:
                    self.db.exec("UPDATE jobs SET state='failed', error='target_busy' WHERE id=?", (jid,))
                    return
                self._busy.add(target)
            try:
                self._run_steps(j)
            finally:
                with self._lock:
                    self._busy.discard(target)
                self._secrets.pop(jid, None) if self._job_state(jid) == "succeeded" else None

    def _job_state(self, jid: str) -> str:
        r = self.db.one("SELECT state FROM jobs WHERE id=?", (jid,))
        return r["state"] if r else ""

    def _ctx(self, j: dict[str, Any]) -> JobCtx:
        blob = loads(j["params"], {}) or {}
        return JobCtx(state=self.state, job_id=j["id"], kind=j["kind"], target=j["target"], params=blob.get("params", {}),
                      secrets=self._secrets.get(j["id"], {}), data=blob.get("data", {}))

    def _save_data(self, jc: JobCtx) -> None:
        self.db.exec("UPDATE jobs SET params=? WHERE id=?", (dumps({"params": jc.params, "data": jc.data}), jc.job_id))

    def _step_log(self, jc: JobCtx, idx: int) -> None:
        text = "\n".join(jc._lines)[-LOG_MAX:]
        jc._lines.clear()
        self.db.exec("UPDATE job_steps SET log=substr(log || ? , -?) WHERE job_id=? AND idx=?", (text + ("\n" if text else ""), LOG_MAX, jc.job_id, idx))

    def _run_steps(self, j: dict[str, Any]) -> None:
        jc = self._ctx(j)
        steps = KINDS[j["kind"]](jc.params)
        now = self.state.clock
        self.db.exec("UPDATE jobs SET state='running', started_at=COALESCE(started_at, ?), error=NULL, resume_from=NULL WHERE id=?", (now(), j["id"]))
        self._event(jc, "job.started")
        rows = {r["idx"]: r for r in self.db.all("SELECT idx, state FROM job_steps WHERE job_id=?", (j["id"],))}
        for i, s in enumerate(steps):
            if rows.get(i, {}).get("state") in ("done", "skipped"):
                continue
            if self._job_state(j["id"]) == "cancelled":
                return
            self.db.exec("UPDATE job_steps SET state='running', started_at=? WHERE job_id=? AND idx=?", (now(), j["id"], i))
            try:
                if s.check(jc):
                    jc.log("đã xong từ trước — bỏ qua")
                    st = "skipped"
                else:
                    s.apply(jc)
                    st = "done"
            except Exception as e:  # noqa: BLE001
                msg = str(e) if isinstance(e, StepError) else f"{type(e).__name__}: {e}"
                jc.log("LỖI: " + msg)
                if not isinstance(e, StepError):
                    log.warning("job %s bước %s lỗi:\n%s", j["id"], s.name, jc.mask(traceback.format_exc()))
                self._step_log(jc, i)
                self._save_data(jc)
                self.db.exec("UPDATE job_steps SET state='failed', finished_at=? WHERE job_id=? AND idx=?", (now(), j["id"], i))
                self.db.exec("UPDATE jobs SET state='failed', error=?, resume_from=?, finished_at=? WHERE id=?",
                             (jc.mask(msg)[:500], s.name, now(), j["id"]))
                self._event(jc, "job.failed", step=s.name)
                return
            self._step_log(jc, i)
            self._save_data(jc)
            self.db.exec("UPDATE job_steps SET state=?, finished_at=? WHERE job_id=? AND idx=?", (st, now(), j["id"], i))
        self.db.exec("UPDATE jobs SET state='succeeded', finished_at=? WHERE id=?", (now(), j["id"]))
        self._event(jc, "job.succeeded")
        hook = getattr(KINDS[j["kind"]], "on_success", None)
        if hook:
            try:
                hook(jc)
            except Exception:  # pragma: no cover
                log.exception("on_success của job %s lỗi", j["id"])

    def _event(self, jc: JobCtx, name: str, **kw: Any) -> None:
        if name == "job.failed":
            from .alerts import raise_alert

            raise_alert(self.state, "job", jc.job_id, "job_failed", "warn")

    # ------------------------------------------------------------------ thử lại / huỷ
    def retry(self, jid: str, *, secrets: dict[str, str] | None = None) -> None:
        j = self.db.one("SELECT state FROM jobs WHERE id=?", (jid,))
        if not j or j["state"] not in ("failed",):
            raise ValueError("chỉ thử lại được job đang lỗi")
        if secrets:
            self._secrets[jid] = dict(secrets)
        self.db.exec("UPDATE job_steps SET state='pending' WHERE job_id=? AND state='failed'", (jid,))
        self.db.exec("UPDATE jobs SET state='queued' WHERE id=?", (jid,))
        self._start(jid)

    def cancel(self, jid: str, *, cleanup: bool) -> dict[str, Any]:
        j = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not j or j["state"] in ("succeeded", "cancelled"):
            raise ValueError("job không huỷ được")
        if j["state"] == "running":
            raise ValueError("job đang chạy — đợi bước hiện tại xong (hoặc lỗi) rồi huỷ")
        self.db.exec("UPDATE jobs SET state='cancelled', finished_at=? WHERE id=?", (self.state.clock(), jid))
        undone: list[str] = []
        if cleanup:
            jc = self._ctx(j)
            steps = KINDS[j["kind"]](jc.params)
            done = {r["idx"] for r in self.db.all("SELECT idx FROM job_steps WHERE job_id=? AND state IN ('done','failed')", (jid,))}
            for i in sorted(done, reverse=True):
                try:
                    steps[i].undo(jc)
                    undone.append(steps[i].name)
                    self.db.exec("UPDATE job_steps SET state='undone' WHERE job_id=? AND idx=?", (jid, i))
                except Exception as e:  # noqa: BLE001
                    jc.log(f"dọn bước {steps[i].name} lỗi: {e}")
                self._step_log(jc, i)
        self._secrets.pop(jid, None)
        return {"undone": undone}
