"""RPC `events.query` (GĐ0 trang Điều phối): lấy N event GẦN NHẤT khi có nhiều hơn `limit`, lọc `types` ở daemon, nhận `since`/`epoch`.
Khung "đang trả lời" đọc event gần nhất: `tests/test_web_manager_gd0.py`. Không gọi `claude`."""

from __future__ import annotations

from cfharness.daemon import Daemon
from cfharness.events import make_event
from cfharness.runtime.fake import FakeRuntime


async def _rpc(d: Daemon, method: str, params: dict | None = None):
    r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params or {})})
    assert "error" not in r, r
    return r["result"]


def _seed(d: Daemon, n: int = 30) -> None:
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku"})
    for i in range(n):
        d.hub.publish(make_event("tool.start" if i % 3 == 0 else "learning.applied", "w1", {"i": i, "tool": "Read"}, run_id="r_1"))


async def test_desc_returns_newest_when_more_than_limit(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime())
    try:
        _seed(d, 30)
        newest = await _rpc(d, "events.query", {"run_id": "r_1", "limit": 5, "order": "desc"})
        assert [e["payload"]["i"] for e in newest] == [29, 28, 27, 26, 25]  # 5 event MỚI nhất, mới → cũ
        oldest = await _rpc(d, "events.query", {"run_id": "r_1", "limit": 5})
        assert [e["payload"]["i"] for e in oldest] == [0, 1, 2, 3, 4]  # mặc định vẫn cũ → mới (tương thích)
    finally:
        d.hub.store.close()


async def test_types_filter_is_honored_with_order_and_limit(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime())
    try:
        _seed(d, 30)
        evs = await _rpc(d, "events.query", {"run_id": "r_1", "types": ["tool.start"]})
        assert {e["type"] for e in evs} == {"tool.start"} and len(evs) == 10
        top = await _rpc(d, "events.query", {"run_id": "r_1", "types": ["tool.start"], "order": "desc", "limit": 2})
        assert [e["payload"]["i"] for e in top] == [27, 24]
        assert await _rpc(d, "events.query", {"run_id": "r_1", "types": ["no.such"]}) == []
    finally:
        d.hub.store.close()


async def test_since_and_epoch(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime())
    try:
        _seed(d, 10)
        allv = await _rpc(d, "events.query", {"run_id": "r_1"})
        cut = allv[6]["seq"]
        after = await _rpc(d, "events.query", {"run_id": "r_1", "since": cut})
        assert [e["payload"]["i"] for e in after] == [7, 8, 9]
        assert after == await _rpc(d, "events.query", {"run_id": "r_1", "since_seq": cut})
        ep = allv[0]["epoch"]
        assert len(await _rpc(d, "events.query", {"run_id": "r_1", "since": cut, "epoch": ep})) == 3
        assert await _rpc(d, "events.query", {"run_id": "r_1", "epoch": "khac"}) == []
    finally:
        d.hub.store.close()
