"""Event bus in-memory của daemon + aggregator trạng thái (FR-OB-01/02).

Cải tiến so với demo:
- `seq` đơn điệu TOÀN HUB trong một `epoch` (khớp PK (epoch, seq) của bảng events).
- Ring buffer THEO AGENT, giới hạn số event + byte; ghi nhớ `seq` lớn nhất đã bị đẩy ra
  để biết replay có bị `truncated` không (khi đó client nạp lại từ DB).
- Subscriber lọc theo agent / run / level; hàng đợi CÓ GIỚI HẠN — subscriber chậm bị ngắt
  với lý do `slow_consumer`, agent không bao giờ chờ UI.
- Listener đồng bộ (ghi DB, aggregator) chạy trước fanout, theo thứ tự đăng ký.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from .events import HubEvent, new_epoch

LEVEL_RANK = {"summary": 0, "detail": 1, "debug": 2}
HUB_KEY = "_hub"  # ring cho event không gắn agent


class Subscription:
    def __init__(
        self,
        bus: "EventBus",
        *,
        agent_id: str | None,
        run_id: str | None,
        level: str,
        maxsize: int,
    ) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.bus = bus
        self.agent_id = agent_id
        self.run_id = run_id
        self.max_rank = LEVEL_RANK.get(level, 0)
        self.queue: asyncio.Queue[HubEvent | None] = asyncio.Queue(maxsize=maxsize)
        self.closed_reason: str | None = None
        self.truncated = False

    def wants(self, ev: HubEvent) -> bool:
        if self.agent_id is not None and ev.agent_id != self.agent_id:
            return False
        if self.run_id is not None and ev.run_id != self.run_id:
            return False
        return LEVEL_RANK.get(ev.level, 0) <= self.max_rank

    def offer(self, ev: HubEvent) -> None:
        if self.closed_reason:
            return
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            self.close("slow_consumer")

    def close(self, reason: str = "closed") -> None:
        if self.closed_reason:
            return
        self.closed_reason = reason
        self.bus.unsubscribe(self)
        # đánh thức người đang chờ: bỏ bớt nếu đầy để chắc chắn đặt được sentinel
        while True:
            try:
                self.queue.put_nowait(None)
                break
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    break

    async def get(self) -> HubEvent | None:
        """Event kế tiếp; None khi subscription bị đóng."""
        if self.closed_reason and self.queue.empty():
            return None
        return await self.queue.get()

    def drain(self) -> list[HubEvent]:
        out: list[HubEvent] = []
        while True:
            try:
                e = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return out
            if e is not None:
                out.append(e)


class _Ring:
    def __init__(self, max_events: int, max_bytes: int) -> None:
        self.events: deque[tuple[HubEvent, int]] = deque()
        self.bytes = 0
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.evicted_upto = 0  # seq lớn nhất đã bị đẩy khỏi ring

    def append(self, ev: HubEvent) -> None:
        size = len(ev.model_dump_json())
        self.events.append((ev, size))
        self.bytes += size
        while self.events and (len(self.events) > self.max_events or self.bytes > self.max_bytes):
            old, s = self.events.popleft()
            self.bytes -= s
            self.evicted_upto = old.seq

    def since(self, seq: int) -> list[HubEvent]:
        return [e for e, _ in self.events if e.seq > seq]


class EventBus:
    def __init__(
        self,
        *,
        ring_events: int = 2000,
        ring_bytes: int = 4_000_000,
        subscriber_queue: int = 10_000,
        epoch: str | None = None,
        start_seq: int = 0,
    ) -> None:
        self.epoch = epoch or new_epoch()
        self._seq = start_seq
        self._rings: dict[str, _Ring] = {}
        self._subs: dict[str, Subscription] = {}
        self._listeners: list[Callable[[HubEvent], None]] = []
        self.ring_events = ring_events
        self.ring_bytes = ring_bytes
        self.subscriber_queue = subscriber_queue

    @property
    def last_seq(self) -> int:
        return self._seq

    # -- publish
    def publish(self, ev: HubEvent) -> HubEvent:
        self._seq += 1
        ev = ev.model_copy(update={"seq": self._seq, "epoch": self.epoch})
        key = ev.agent_id or HUB_KEY
        ring = self._rings.get(key)
        if ring is None:
            ring = self._rings[key] = _Ring(self.ring_events, self.ring_bytes)
        ring.append(ev)
        for listener in list(self._listeners):
            listener(ev)
        for sub in list(self._subs.values()):
            if sub.wants(ev):
                sub.offer(ev)
        return ev

    def publish_many(self, evs: Iterable[HubEvent]) -> list[HubEvent]:
        return [self.publish(e) for e in evs]

    # -- replay / subscribe
    def replay(self, agent_id: str | None, since_seq: int = 0) -> tuple[list[HubEvent], bool]:
        """(event có seq > since_seq trong ring của agent, truncated)."""
        ring = self._rings.get(agent_id or HUB_KEY)
        if ring is None:
            return [], False
        return ring.since(since_seq), since_seq < ring.evicted_upto

    def subscribe(
        self,
        agent_id: str | None = None,
        *,
        run_id: str | None = None,
        since_seq: int | None = None,
        level: str = "summary",
        maxsize: int | None = None,
        backfill: Callable[[int], list[HubEvent]] | None = None,
    ) -> Subscription:
        """Đăng ký nhận event. Có `since_seq` → nạp sẵn phần replay rồi nối live (không hở, không trùng:
        bus chạy đơn luồng asyncio, không có `await` giữa replay và đăng ký).

        Nếu ring không đủ (truncated) và có `backfill(since_seq)` (đọc DB) thì dùng backfill thay ring.
        """
        sub = Subscription(self, agent_id=agent_id, run_id=run_id, level=level, maxsize=maxsize or self.subscriber_queue)
        if since_seq is not None:
            if agent_id is not None:
                evs, truncated = self.replay(agent_id, since_seq)
            else:
                evs, truncated = [], any(since_seq < r.evicted_upto for r in self._rings.values())
                merged = [e for r in self._rings.values() for e in r.since(since_seq)]
                evs = sorted(merged, key=lambda e: e.seq)
            if truncated and backfill is not None:
                evs, truncated = backfill(since_seq), False
            sub.truncated = truncated
            for e in evs:
                if sub.wants(e):
                    sub.offer(e)
        self._subs[sub.id] = sub
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.pop(sub.id, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def add_listener(self, fn: Callable[[HubEvent], None]) -> None:
        self._listeners.append(fn)

    def close_all(self, reason: str = "hub_stopping") -> None:
        for sub in list(self._subs.values()):
            sub.close(reason)


# ============================================================================ aggregator
@dataclass
class AgentLive:
    id: str
    status: str = "idle"
    current_run_id: str | None = None
    last_tool: str | None = None
    last_tool_preview: str | None = None
    last_tool_at: datetime | None = None
    run_tokens: int = 0
    run_cost_est: float = 0.0
    session_id: str | None = None


@dataclass
class Aggregator:
    """Gom event thành trạng thái live theo agent + usage toàn hub trong epoch (FR-OB-02)."""

    agents: dict[str, AgentLive] = field(default_factory=dict)
    cost_usd: float = 0.0
    tokens: int = 0
    runs_completed: int = 0
    runs_failed: int = 0
    rate_limit: dict[str, Any] | None = None

    def get(self, agent_id: str) -> AgentLive:
        st = self.agents.get(agent_id)
        if st is None:
            st = self.agents[agent_id] = AgentLive(agent_id)
        return st

    def apply(self, ev: HubEvent) -> None:
        p = ev.payload
        if ev.type == "ratelimit.update":
            self.rate_limit = p.get("rate_limit_info")
            return
        if ev.agent_id is None:
            return
        st = self.get(ev.agent_id)
        if ev.session_id:
            st.session_id = ev.session_id
        if ev.type == "agent.status":
            st.status = p.get("status", st.status)
            st.current_run_id = p.get("current_run_id")
        elif ev.type == "run.started":
            st.run_tokens, st.run_cost_est = 0, 0.0
        elif ev.type == "tool.start":
            st.last_tool = p.get("name")
            st.last_tool_preview = p.get("args_preview")
            st.last_tool_at = ev.ts
        elif ev.type == "usage.tick":
            st.run_tokens = p.get("total_tokens", st.run_tokens)
            st.run_cost_est = p.get("cost_usd", st.run_cost_est)
        elif ev.type in ("run.completed", "run.failed"):
            self.cost_usd += float(p.get("cost_usd") or 0.0)
            u = p.get("usage") or {}
            self.tokens += sum(int(u.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if ev.type == "run.completed":
                self.runs_completed += 1
            else:
                self.runs_failed += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "cost_usd": round(self.cost_usd, 8),
            "tokens": self.tokens,
            "runs_completed": self.runs_completed,
            "runs_failed": self.runs_failed,
            "rate_limit": self.rate_limit,
        }
