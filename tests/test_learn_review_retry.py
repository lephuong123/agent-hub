"""Review fork: kết thúc không trọn (rate_limited / budget_exceeded) không đánh rơi Signal; trần ngân sách theo cỡ ngữ cảnh session
gốc (lỗi dữ liệu thật: 44/126 review chết sau 1–3 lượt vì $0.20 không đủ nạp lại ~38k token). Không gọi claude: Hub in-process,
gọi `tick()`/`finalize()` trực tiếp với run giả."""

import pytest

from cfharness.hub import Hub
from cfharness.store import now_iso


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home)
    h.agents.create({"id": "r1", "name": "R1", "model": "sonnet"})
    h.config.set("learning.review_debounce_s", 0)
    yield h
    h.store.close()


def src_run(h, rid="r_src", **kw):
    row = {"id": rid, "agent_id": "r1", "kind": "adhoc", "prompt": "x", "status": "completed", "outcome": "completed", "session_id": "s_src",
           "queued_at": now_iso(), "started_at": now_iso(), "ended_at": now_iso(), **kw}
    h.store.insert_run(row)
    return h.store.get_run(rid)


def signal(h, sid):
    h.store.conn.execute("INSERT INTO signals (id, agent_id, kind, source, created_at) VALUES (?,?,?,?,?)", (sid, "r1", "correction", "test", now_iso()))


def start_job(h, signals=("sg_1",)):
    for s in signals:
        signal(h, s)
    src_run(h)
    job = h.learning.reviews.enqueue("r1", "signal", signal_ids=list(signals), source_run_id="r_src", delay_s=0)
    started = h.learning.reviews.tick()
    assert len(started) == 1, h.learning.reviews.get(job["id"])
    return h.learning.reviews.get(job["id"]), h.store.get_run(started[0])


def end(h, run, outcome, cost=0.2):
    """Như RunManager._finalize: ghi DB, bỏ khỏi run đang chạy, rồi listener bus gọi finalize của review."""
    h.store.conn.execute("UPDATE runs SET status=?, outcome=?, ended_at=? WHERE id=?", (outcome, outcome, now_iso(), run["id"]))
    h.runs.active.pop(run["id"], None)
    h.learning.reviews.finalize({**run, "status": outcome, "outcome": outcome, "cost_usd": cost})


def unprocessed(h):
    return {s["id"] for s in h.learning.signals.unprocessed("r1")}


# ---------------------------------------------------------------- bug 2: rate_limited / budget_exceeded
async def test_rate_limited_review_is_requeued_with_backoff_and_keeps_signals(hub):
    hub.config.set("learning.review_retry_backoff_s", 120)
    job, run = start_job(hub)
    end(hub, run, "rate_limited")
    j = hub.learning.reviews.get(job["id"])
    assert j["status"] == "queued" and j["run_id"] is None and j["not_before"] > now_iso()
    assert "rate_limited" in j["reason"] and j["result"]["attempts"][0]["outcome"] == "rate_limited"
    assert unprocessed(hub) == {"sg_1"}  # Signal KHÔNG bị tiêu
    assert hub.learning.reviews.tick() == []  # còn trong backoff
    hub.store.conn.execute("UPDATE review_jobs SET not_before='2000-01-01T00:00:00.000Z' WHERE id=?", (job["id"],))
    started = hub.learning.reviews.tick()
    assert len(started) == 1 and started[0] != run["id"]
    end(hub, hub.store.get_run(started[0]), "completed", cost=0.1)
    j = hub.learning.reviews.get(job["id"])
    assert j["status"] == "done" and len(j["result"]["attempts"]) == 2 and j["cost_usd"] == pytest.approx(0.3)
    assert unprocessed(hub) == set()


async def test_rate_limited_retries_are_capped_but_signals_survive(hub):
    hub.config.set("learning.review_rate_limit_retries", 1)
    job, run = start_job(hub)
    end(hub, run, "rate_limited")
    hub.store.conn.execute("UPDATE review_jobs SET not_before='2000-01-01T00:00:00.000Z' WHERE id=?", (job["id"],))
    run2 = hub.store.get_run(hub.learning.reviews.tick()[0])
    end(hub, run2, "rate_limited")
    j = hub.learning.reviews.get(job["id"])
    assert j["status"] == "failed" and "rate_limited sau 2 lần thử" in j["reason"]
    assert unprocessed(hub) == {"sg_1"}  # lỗi tạm thời của nhà cung cấp: review sau nhặt lại


async def test_budget_exceeded_without_proposals_retries_once_at_ceiling_then_fails(hub):
    hub.config.set("learning.review_budget_max_usd", 1.5)
    job, run = start_job(hub)
    end(hub, run, "budget_exceeded")
    j = hub.learning.reviews.get(job["id"])
    assert j["status"] == "queued" and unprocessed(hub) == {"sg_1"}
    run2 = hub.store.get_run(hub.learning.reviews.tick()[0])
    assert run2["max_budget_usd"] == pytest.approx(1.5)  # lần thử lại: dùng thẳng trần
    end(hub, run2, "budget_exceeded")
    j = hub.learning.reviews.get(job["id"])
    assert j["status"] == "failed" and "budget_exceeded sau 2 lần thử" in j["reason"]
    assert unprocessed(hub) == set()  # hết lượt → tiêu Signal, không lặp vô hạn


async def test_budget_exceeded_after_proposal_is_done_and_proposal_kept(hub):
    job, run = start_job(hub)
    r = hub.learning.pipeline.propose(target="memory", op="add", agent_id="r1", payload={"action": "add", "content": "The build uses Node 22 LTS."},
                                      created_by="review", review_job_id=job["id"], run_id=run["id"])
    assert r["status"] == "applied"
    end(hub, run, "budget_exceeded")
    j = hub.learning.reviews.get(job["id"])
    assert j["status"] == "done" and j["result"]["proposals"][0]["status"] == "applied"
    assert unprocessed(hub) == set()


async def test_requeue_merges_into_existing_queued_job(hub):
    job, run = start_job(hub)
    signal(hub, "sg_2")
    other = hub.learning.reviews.enqueue("r1", "signal", signal_ids=["sg_2"], source_run_id="r_src", delay_s=30)
    assert other["id"] != job["id"]
    end(hub, run, "rate_limited")
    assert hub.learning.reviews.get(job["id"])["status"] == "cancelled"
    o = hub.learning.reviews.get(other["id"])
    assert o["status"] == "queued" and set(o["signal_ids"]) == {"sg_1", "sg_2"} and o["result"]["attempts"][0]["outcome"] == "rate_limited"
    assert unprocessed(hub) == {"sg_1", "sg_2"}


# ---------------------------------------------------------------- bug 3: trần theo cỡ ngữ cảnh session gốc
async def test_review_budget_scales_with_parent_context(hub):
    R = hub.learning.reviews
    hub.config.set("learning.review_reload_usd_per_mtok", 10.0)
    hub.config.set("learning.review_budget_max_usd", 1.5)
    # runs.usage = 0 nhưng model_usage có số thật (như dữ liệu thật)
    big = src_run(hub, "r_big", usage={"input_tokens": 0}, num_turns=1,
                  model_usage={"claude-opus": {"inputTokens": 12, "cacheReadInputTokens": 8000, "cacheCreationInputTokens": 30000, "costUSD": 0.3}})
    assert R.context_tokens(big) == 38012
    assert R.review_budget(big) == pytest.approx(0.2 + 0.38012, abs=1e-4)  # đủ nạp lại ~38k token
    huge = src_run(hub, "r_huge", session_id="s_huge", model_usage={"m": {"cacheCreationInputTokens": 900_000}})
    assert R.review_budget(huge) == pytest.approx(1.5)  # chặn trên
    assert R.review_budget(big, retry_after_budget=True) == pytest.approx(1.5)
    # run nguồn không có số → lấy run trước của cùng session
    src_run(hub, "r_old", session_id="s_x", queued_at="2000-01-01T00:00:00.000Z", usage={"input_tokens": 5, "cache_read_input_tokens": 20000})
    empty = src_run(hub, "r_new", session_id="s_x")
    assert R.context_tokens(empty) == 20005
    hub.config.set("learning.review_budget_max_usd", None)
    assert R.review_budget(huge) == pytest.approx(9.2)
    hub.config.set("learning.review_budget_usd", None)
    assert R.review_budget(big) is None  # null = không trần riêng (giữ hành vi cũ)
    with pytest.raises(ValueError):
        hub.config.set("learning.review_budget_max_usd", 0)
    with pytest.raises(ValueError):
        hub.config.set("learning.review_rate_limit_retries", -1)


async def test_review_run_gets_scaled_budget(hub):
    signal(hub, "sg_1")
    src_run(hub, model_usage={"claude-opus": {"inputTokens": 10, "cacheCreationInputTokens": 40000}})
    hub.learning.reviews.enqueue("r1", "signal", signal_ids=["sg_1"], source_run_id="r_src", delay_s=0)
    run = hub.store.get_run(hub.learning.reviews.tick()[0])
    assert run["max_budget_usd"] == pytest.approx(0.2 + 0.4001, abs=1e-4)
