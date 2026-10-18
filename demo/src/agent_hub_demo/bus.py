"""Event bus in-memory của hub + aggregator trạng thái.

- Ring buffer THEO AGENT (giới hạn cả số event lẫn số byte) → viewer attach muộn replay được.
- `seq` tăng dần theo agent, `epoch` đổi mỗi lần bus khởi tạo.
- Fanout tới subscriber bằng hàng đợi CÓ GIỚI HẠN: viewer chậm bị cắt (đánh dấu `dropped`),
  agent không bao giờ phải chờ UI.
- Kênh firehose (agent_id=None, include_deltas=False) cho màn tổng quan: không gửi delta token.
- `Aggregator` gom event thành snapshot bảng "htop" (status, task, last tool, tokens, $…)
  và danh sách learning proposal.

Phần này được viết để TÁI DÙNG cho bản thật (đổi ring/log sang SQLite là đủ).
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .events import HubEvent


# ============================================================================ bus
class Subscription:
    def __init__(self, bus: "EventBus", agent_id: str | None, include_deltas: bool, maxsize: int) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.bus = bus
        self.agent_id = agent_id
        self.include_deltas = include_deltas
        self.queue: asyncio.Queue[HubEvent] = asyncio.Queue(maxsize=maxsize)
        self.dropped = False
        self.truncated = False  # replay không đủ (ring đã đẩy event cũ ra) → client đọc log bền

    def wants(self, ev: HubEvent) -> bool:
        if self.agent_id is not None and ev.agent_id != self.agent_id:
            return False
        if not self.include_deltas and ev.is_delta:
            return False
        return True

    def offer(self, ev: HubEvent) -> None:
        if self.dropped:
            return
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            # viewer chậm: cắt, không chặn agent
            self.dropped = True
            self.bus.unsubscribe(self)

    def drain(self) -> list[HubEvent]:
        out = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                return out

    def close(self) -> None:
        self.bus.unsubscribe(self)


class _Ring:
    def __init__(self, max_events: int, max_bytes: int) -> None:
        self.events: deque[tuple[HubEvent, int]] = deque()
        self.bytes = 0
        self.max_events = max_events
        self.max_bytes = max_bytes

    def append(self, ev: HubEvent) -> None:
        size = len(ev.model_dump_json())
        self.events.append((ev, size))
        self.bytes += size
        while self.events and (len(self.events) > self.max_events or self.bytes > self.max_bytes):
            _, s = self.events.popleft()
            self.bytes -= s

    @property
    def first_seq(self) -> int | None:
        return self.events[0][0].seq if self.events else None

    def since(self, seq: int) -> list[HubEvent]:
        return [e for e, _ in self.events if e.seq > seq]


class EventBus:
    def __init__(self, ring_events: int = 5000, ring_bytes: int = 2_000_000, epoch: str | None = None) -> None:
        self.epoch = epoch or uuid.uuid4().hex[:8]
        self._seq: dict[str, int] = {}
        self._rings: dict[str, _Ring] = {}
        self._subs: dict[str, Subscription] = {}
        self._listeners: list[Callable[[HubEvent], None]] = []
        self.ring_events = ring_events
        self.ring_bytes = ring_bytes

    # -- publish
    def publish(self, ev: HubEvent) -> HubEvent:
        seq = self._seq.get(ev.agent_id, 0) + 1
        self._seq[ev.agent_id] = seq
        ev = ev.model_copy(update={"seq": seq, "epoch": self.epoch})
        ring = self._rings.setdefault(ev.agent_id, _Ring(self.ring_events, self.ring_bytes))
        ring.append(ev)
        for listener in list(self._listeners):
            listener(ev)  # listener đồng bộ (aggregator) — phải nhanh
        for sub in list(self._subs.values()):
            if sub.wants(ev):
                sub.offer(ev)
        return ev

    def publish_many(self, evs: Iterable[HubEvent]) -> list[HubEvent]:
        return [self.publish(e) for e in evs]

    # -- replay / subscribe
    def last_seq(self, agent_id: str) -> int:
        return self._seq.get(agent_id, 0)

    def replay(self, agent_id: str, since_seq: int = 0) -> tuple[list[HubEvent], bool]:
        """Trả (events có seq > since_seq, truncated)."""
        ring = self._rings.get(agent_id)
        if ring is None:
            return [], False
        first = ring.first_seq
        truncated = first is not None and since_seq < first - 1
        return ring.since(since_seq), truncated

    def subscribe(
        self,
        agent_id: str | None = None,
        *,
        since_seq: int | None = None,
        include_deltas: bool = True,
        maxsize: int = 20_000,
    ) -> Subscription:
        """Đăng ký nhận event. Nếu có `since_seq` (và agent_id), hàng đợi được nạp sẵn phần replay
        từ ring rồi mới nối live — không hở, không trùng (bus chạy đơn luồng asyncio)."""
        sub = Subscription(self, agent_id, include_deltas, maxsize)
        if agent_id is not None and since_seq is not None:
            evs, sub.truncated = self.replay(agent_id, since_seq)
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

    def forget(self, agent_id: str) -> None:
        self._rings.pop(agent_id, None)


# ============================================================================ aggregator
STATUS_ORDER = {"waiting": 0, "error": 1, "working": 2, "idle": 3, "stopped": 4}


@dataclass
class AgentState:
    id: str
    name: str
    team: str
    role: str = ""
    model: str = "sonnet"
    mcp_servers: list[str] = field(default_factory=list)
    status: str = "idle"
    status_detail: str | None = None
    task: str | None = None
    session_id: str | None = None
    last_tool: str | None = None
    last_tool_detail: str | None = None
    last_tool_dur: float | None = None
    tool_running: bool = False
    turn_started: datetime | None = None
    base_tokens: int = 0
    base_cost: float = 0.0
    run_tokens: int = 0
    run_cost: float = 0.0
    context_percent: float = 0.0
    tps: float = 0.0
    running_model: str | None = None
    open_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_error: str | None = None
    subagents: int = 0

    @property
    def tokens(self) -> int:
        return self.base_tokens + self.run_tokens

    @property
    def cost(self) -> float:
        return self.base_cost + self.run_cost

    @property
    def pending_request(self) -> dict[str, Any] | None:
        return next(iter(self.open_requests.values()), None)


@dataclass
class Proposal:
    proposal_id: str
    agent_id: str
    scope: str  # agent | team
    kind: str  # memory | skill
    title: str
    diff: str
    auto: bool
    status: str  # pending | applied | rejected | rolled_back
    created: datetime
    applied_by: str | None = None


class Aggregator:
    def __init__(self) -> None:
        self.agents: dict[str, AgentState] = {}
        self.proposals: dict[str, Proposal] = {}
        self.removed_cost = 0.0
        self.removed_tokens = 0

    # -- registry
    def register(self, st: AgentState) -> None:
        self.agents[st.id] = st

    def unregister(self, agent_id: str) -> None:
        st = self.agents.pop(agent_id, None)
        if st:  # chi phí đã tiêu vẫn tính vào "hôm nay"
            self.removed_cost += st.cost
            self.removed_tokens += st.tokens

    # -- totals
    @property
    def total_cost(self) -> float:
        return self.removed_cost + sum(a.cost for a in self.agents.values())

    @property
    def total_tokens(self) -> int:
        return self.removed_tokens + sum(a.tokens for a in self.agents.values())

    def count_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.agents.values():
            out[a.status] = out.get(a.status, 0) + 1
        return out

    @property
    def pending_proposals(self) -> int:
        return sum(1 for p in self.proposals.values() if p.status == "pending")

    # -- apply
    def apply(self, ev: HubEvent) -> None:
        st = self.agents.get(ev.agent_id)
        p = ev.payload
        if ev.type == "learning.proposal":
            self.proposals[p["proposal_id"]] = Proposal(
                proposal_id=p["proposal_id"],
                agent_id=ev.agent_id,
                scope=p.get("scope", "agent"),
                kind=p.get("kind", "memory"),
                title=p.get("title", ""),
                diff=p.get("diff", ""),
                auto=bool(p.get("auto")),
                status="pending",
                created=ev.ts,
            )
            return
        if ev.type == "learning.applied":
            prop = self.proposals.get(p["proposal_id"])
            if prop:
                prop.status = {"applied": "applied", "rejected": "rejected", "rolled_back": "rolled_back"}[
                    p["action"]
                ]
                prop.applied_by = p.get("by")
            return
        if st is None:
            return
        if ev.session_id:
            st.session_id = ev.session_id
        if ev.type == "agent.status":
            st.status = p.get("status", st.status)
            st.status_detail = p.get("detail")
            if st.status in ("idle", "stopped"):
                st.tool_running = False
                st.tps = 0.0
        elif ev.type == "turn.start":
            st.turn_started = ev.ts
            st.task = p.get("task") or st.task
            st.running_model = p.get("model")
            st.run_tokens, st.run_cost = 0, 0.0
            st.last_error = None
        elif ev.type == "tool.start":
            st.last_tool = p.get("name")
            st.last_tool_detail = p.get("summary")
            st.last_tool_dur = None
            st.tool_running = True
        elif ev.type == "tool.complete":
            st.last_tool_dur = p.get("duration_s")
            st.tool_running = False
        elif ev.type == "usage.tick":
            st.run_tokens = p.get("total_tokens", st.run_tokens)
            st.run_cost = p.get("cost_usd", st.run_cost)
            st.context_percent = p.get("context_percent", st.context_percent)
            st.tps = p.get("tps", st.tps)
        elif ev.type == "turn.complete":
            st.base_tokens += p.get("total_tokens", st.run_tokens)
            st.base_cost += p.get("cost_usd", st.run_cost)
            st.run_tokens, st.run_cost = 0, 0.0
            st.tps = 0.0
            st.tool_running = False
            st.open_requests.clear()
        elif ev.type == "request.open":
            st.open_requests[p["request_id"]] = dict(p)
        elif ev.type == "request.cancel":
            st.open_requests.pop(p.get("request_id"), None)
        elif ev.type == "subagent.spawn":
            st.subagents += 1
        elif ev.type == "error":
            st.last_error = p.get("message")


def utc(dt: datetime | None = None) -> datetime:
    return dt or datetime.now(timezone.utc)
