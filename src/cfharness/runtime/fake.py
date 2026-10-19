"""`FakeRuntime`: phát lại file JSONL đã ghi từ `claude` thật (FR-RT-01). Không gọi model, không tốn tiền.

- Mặc định dùng fixture đóng gói `cfharness/fixtures/claude-stream-sample.jsonl` (35 dòng, CLI 2.1.302).
- `session_id` trong fixture được thay bằng session hub cấp để chuỗi event giống run thật.
- `line_delay_s` tạo độ trễ giữa các dòng; `hold=True` giữ run trước dòng `result` tới khi `release()`.
- Steer (M2, FR-RT-06): `send()` xếp tin vào hộp thư; trước dòng kế tiếp (hoặc ngay khi đang `hold`) runtime chèn
  dòng `{"type":"user",…,"isReplay":true}` như CLI thật (`--replay-user-messages`) → adapter phát `steer.ack`.
  Sau dòng `result` "stdin" coi như đã đóng → `send()` báo lỗi (giữ hành vi M0).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from importlib import resources
from pathlib import Path
from typing import Any, AsyncIterator

from ..events import HubEvent
from .base import AgentRuntime, RawLog, RunHandle, RunSpec, RuntimeError_, make_adapter

_pid_counter = 900_000


def default_fixture() -> Path:
    return Path(str(resources.files("cfharness") / "fixtures" / "claude-stream-sample.jsonl"))


class FakeRuntime(AgentRuntime):
    name = "fake"

    def __init__(
        self,
        fixture: str | os.PathLike[str] | None = None,
        *,
        line_delay_s: float = 0.0,
        hold: bool = False,
        rewrite_session: bool = True,
        cli_version: str = "2.1.302",
        fixture_seq: list[str | os.PathLike[str]] | None = None,
    ) -> None:
        self.fixture = Path(fixture) if fixture else default_fixture()
        # M1-B: fixture cho từng lần start theo thứ tự (vd run đầu + run resume có total_cost_usd luỹ kế)
        self.fixture_seq = [Path(f) for f in fixture_seq or []]
        self.line_delay_s = line_delay_s
        self.hold = hold
        self.rewrite_session = rewrite_session
        self.cli_version = cli_version
        self.gates: dict[str, asyncio.Event] = {}
        self.started: list[RunSpec] = []

    def capabilities(self) -> dict[str, bool]:
        return {"steer": True, "approval": False, "fork": True, "resume": True, "json_schema": False, "budget": True}

    def version(self) -> str | None:
        return self.cli_version

    # hardening H-20: oneshot giả cho judge/curator — test đặt `oneshot_results` (danh sách dict hoặc callable(req) → dict)
    oneshot_results: list[Any] | Any = None

    async def oneshot(self, req: Any) -> Any:
        from .base import OneShotResult

        self.oneshot_calls = getattr(self, "oneshot_calls", []) + [req]
        src = self.oneshot_results
        out = src(req) if callable(src) else (src.pop(0) if isinstance(src, list) and src else None)
        if not isinstance(out, dict):
            return OneShotResult(False, None, 0.0, 0, "FakeRuntime: chưa cấu hình oneshot_results")
        return OneShotResult(True, out, float(out.pop("_cost_usd", 0.0)) if "_cost_usd" in out else 0.0, 0, "", {"structured_output": out})

    def release(self, run_id: str | None = None) -> None:
        for rid, g in self.gates.items():
            if run_id is None or rid == run_id:
                g.set()

    def _lines(self, spec: RunSpec, fixture: Path | None = None) -> list[str]:
        lines = [ln for ln in (fixture or self.fixture).read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not self.rewrite_session:
            return lines
        out = []
        sid = spec.session_id
        for ln in lines:
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                out.append(ln)
                continue
            if isinstance(obj, dict) and "session_id" in obj:
                obj["session_id"] = sid
            if isinstance(obj, dict) and obj.get("type") == "system" and obj.get("subtype") == "init":
                obj["cwd"] = str(spec.cwd)
                if spec.model and "-" in spec.model:
                    obj["model"] = spec.model
            out.append(json.dumps(obj, ensure_ascii=False))
        return out

    async def start(self, spec: RunSpec) -> RunHandle:
        global _pid_counter
        _pid_counter += 1
        spec.cwd.mkdir(parents=True, exist_ok=True)
        self.started.append(spec)
        h = RunHandle(
            run_id=spec.run_id,
            spec=spec,
            adapter=make_adapter(spec),
            pid=_pid_counter,
            argv_redacted=["fake-claude", "--replay", str(self.fixture)],
            cli_version=self.cli_version,
        )
        h.internal["stop"] = asyncio.Event()
        h.internal["inbox"] = []
        h.internal["inbox_evt"] = asyncio.Event()
        h.internal["accepting"] = True
        if self.fixture_seq:
            h.internal["fixture"] = self.fixture_seq.pop(0)
        if self.hold:
            self.gates[spec.run_id] = asyncio.Event()
        return h

    async def events(self, handle: RunHandle) -> AsyncIterator[HubEvent]:
        stop: asyncio.Event = handle.internal["stop"]
        raw = RawLog(handle.spec.raw_log_path)
        handle.spec.stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        handle.spec.stderr_log_path.write_text("", encoding="utf-8")
        try:
            for ln in self._lines(handle.spec, handle.internal.get("fixture")):
                if stop.is_set():
                    break
                for ev in self._flush_inbox(handle, raw):
                    yield ev
                is_result = ln.startswith('{"') and '"type":"result"' in ln.replace(" ", "")
                gate = self.gates.get(handle.run_id)
                if is_result and gate is not None:
                    inbox_evt: asyncio.Event = handle.internal["inbox_evt"]
                    while not gate.is_set() and not stop.is_set():
                        await _wait_any(gate, stop, inbox_evt)
                        for ev in self._flush_inbox(handle, raw):
                            yield ev
                    if stop.is_set():
                        break
                if is_result:
                    handle.internal["accepting"] = False  # như CLI thật: hub đóng stdin sau `result`
                if self.line_delay_s:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=self.line_delay_s)
                        break
                    except asyncio.TimeoutError:
                        pass
                raw.write(ln.encode("utf-8"))
                for ev in handle.adapter.feed(ln):
                    yield ev
            handle.exit_code = -2 if stop.is_set() else 0
        finally:
            handle.raw_log_final = raw.close()
            self.gates.pop(handle.run_id, None)

    async def send(self, handle: RunHandle, user_message: str) -> None:
        if not handle.internal.get("accepting") or handle.internal["stop"].is_set():
            raise RuntimeError_("run không nhận thêm tin (stdin đã đóng sau result)")
        handle.internal["inbox"].append(user_message)
        handle.internal["inbox_evt"].set()

    def _flush_inbox(self, handle: RunHandle, raw: RawLog) -> list[HubEvent]:
        inbox: list[str] = handle.internal["inbox"]
        handle.internal["inbox_evt"].clear()
        out: list[HubEvent] = []
        while inbox:
            text = inbox.pop(0)
            lines = []
            if handle.adapter.replay_count == 0:  # fixture ghi bằng input text: giả lập phát lại prompt trước
                lines.append(_replay_line(handle.spec.prompt, handle.spec.session_id))
            lines.append(_replay_line(text, handle.spec.session_id))
            for ln in lines:
                raw.write(ln.encode("utf-8"))
                out.extend(handle.adapter.feed(ln))
        return out

    async def interrupt(self, handle: RunHandle) -> None:
        handle.internal["stop"].set()

    async def stop(self, handle: RunHandle, grace_s: float = 10.0) -> None:
        handle.stop_requested = True
        handle.internal["stop"].set()


async def _wait_either(a: asyncio.Event, b: asyncio.Event) -> None:
    ta, tb = asyncio.ensure_future(a.wait()), asyncio.ensure_future(b.wait())
    try:
        await asyncio.wait({ta, tb}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (ta, tb):
            t.cancel()


async def _wait_any(*evs: asyncio.Event) -> None:
    tasks = [asyncio.ensure_future(e.wait()) for e in evs]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


def _replay_line(text: str, session_id: str) -> str:
    return json.dumps(
        {"type": "user", "message": {"role": "user", "content": text}, "session_id": session_id, "parent_tool_use_id": None,
         "uuid": str(uuid.uuid4()), "isReplay": True},
        ensure_ascii=False,
    )
