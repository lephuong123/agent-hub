"""Cổng cfharness H1 — `LaneClient` theo hợp đồng §C (plan-filehub-lane.md) trên server GIẢ `fake_kgs_lane.py` (TCP loopback thật) + thiết lập
khoá qua daemon (`filehub.lane_setup|test|disable|status`). Không gọi Không gian số thật, không gọi claude.

Đỏ→xanh an toàn: khoá không lộ trong lỗi/repr/config.yaml/DB/audit/event; URL không loopback bị từ chối; RPC lane là HUMAN_ONLY, không ∈
MANAGER_ALLOWED; CLI `filehub lane` bị classifier chặn với agent; khoá bị gỡ khỏi gói `clone`.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from cfharness import auth
from cfharness.daemon import Daemon
from cfharness.errors import HubError
from cfharness.filehub.lane.client import LaneClient, LaneError, check_url, norm_rel
from fake_kgs_lane import KEY, FakeLane, dec_cursor
from m7_fake import rpc


@pytest.fixture
def lane():
    f = FakeLane(ping_s=0.3)
    yield f
    f.close()


# ============================================================================ thuần
def test_check_url_only_loopback():
    assert check_url("http://127.0.0.1:8223") == "http://127.0.0.1:8223"
    assert check_url("http://127.0.0.1:8223/") == "http://127.0.0.1:8223"
    assert check_url("http://[::1]:8233") == "http://[::1]:8233"
    for bad in ("https://127.0.0.1:8223", "http://localhost:8223", "http://10.0.0.5:8223", "http://kgs.example.com:8223", "http://127.0.0.1",
                "http://u:p@127.0.0.1:8223", "http://127.0.0.1:8223/lane", "http://127.0.0.1:8223?x=1", "http://127.0.0.2:8223", "", None):
        with pytest.raises(LaneError):
            check_url(bad)


def test_norm_rel_c4():
    assert norm_rel("Báo cáo/Q3.docx") == "Báo cáo/Q3.docx"
    assert norm_rel("") == ""
    import unicodedata

    assert norm_rel(unicodedata.normalize("NFD", "Báo cáo")) == unicodedata.normalize("NFC", "Báo cáo")
    for bad in ("/abs", "a/", "a//b", "./a", "a/../b", "..", ".tangthu", "x/.TangThu-db", "a\\b", "a\x00b", "a\nb", "x" * 1100, "/".join(["a"] * 65)):
        with pytest.raises(LaneError) as ei:
            norm_rel(bad)
        assert ei.value.lane_code == "invalid_name"


# ============================================================================ client
async def test_client_read_write_errors_and_no_key_leak(lane):
    c = LaneClient(lane.url, KEY)
    try:
        assert KEY not in repr(c) and c.prefix == KEY[:12]
        h = await c.hello()
        assert h["protocol"] == 1 and h["roots"][0]["id"] == "r_chung"
        lane.human_write("Báo cáo/yeu-cau.md", "viết báo cáo\n")
        items, cur, cut = await c.list_all("r_chung", "", recursive=True)
        assert {i["path"] for i in items} == {"Báo cáo", "Báo cáo/yeu-cau.md"} and dec_cursor(cur) == lane.seq and not cut
        data, hdr = await c.read("r_chung", "Báo cáo/yeu-cau.md")
        assert data == b"viet bao cao\n".replace(b"viet bao cao", "viết báo cáo".encode()) and hdr["etag"].strip('"')
        st = await c.stat("r_chung", "Báo cáo/yeu-cau.md")
        it = await c.write("r_chung", "Báo cáo/yeu-cau.md", b"v2", agent="w1", run="r_1", if_match=st["version_id"])
        assert it["rev"] == st["rev"] + 1 and lane.file("Báo cáo/yeu-cau.md") == b"v2"
        last = lane.writes()[-1]
        assert last["headers"]["X-Lane-Agent"] == "w1" and last["headers"]["X-Lane-Run"] == "r_1" and last["headers"].get("Idempotency-Key")
        assert last["headers"]["If-Match"] == f'"{st["version_id"]}"' and last["headers"]["X-Lane-Sha256"]
        # 409 conflict (base cũ) — có `current`
        with pytest.raises(LaneError) as ei:
            await c.write("r_chung", "Báo cáo/yeu-cau.md", b"v3", agent="w1", run="r_1", if_match=st["version_id"])
        assert ei.value.lane_code == "conflict" and ei.value.status == 409 and ei.value.extra["current"]["path"] == "Báo cáo/yeu-cau.md"
        # 409 exists khi tạo mới đè
        with pytest.raises(LaneError) as ei:
            await c.write("r_chung", "Báo cáo/yeu-cau.md", b"x", agent="w1", run="r_1", create=True)
        assert ei.value.lane_code == "exists"
        # 423 khoá tệp
        lane.lock("Báo cáo/yeu-cau.md")
        cur_v = (await c.stat("r_chung", "Báo cáo/yeu-cau.md"))["version_id"]
        with pytest.raises(LaneError) as ei:
            await c.write("r_chung", "Báo cáo/yeu-cau.md", b"x", agent="w1", run="r_1", if_match=cur_v)
        assert ei.value.lane_code == "file_locked" and ei.value.extra.get("until")
        # 422 infected (EICAR) — không tạo phiên bản
        with pytest.raises(LaneError) as ei:
            await c.write("r_chung", "Báo cáo/virus.txt", b"X5O EICAR test", agent="w1", run="r_1", create=True)
        assert ei.value.lane_code == "infected" and ei.value.extra.get("quarantined") is True and lane.file("Báo cáo/virus.txt") is None
        # 403 root_not_granted
        with pytest.raises(LaneError) as ei:
            await c.stat("r_khac", "x")
        assert ei.value.lane_code == "root_not_granted"
        # 429 / 503 / 507 có retry_after / quota
        lane.fail_next("PUT content", 429, "too_many_writes", retry_after=3)
        with pytest.raises(LaneError) as ei:
            await c.write("r_chung", "Báo cáo/a.txt", b"a", agent="w1", run="r_1", create=True)
        assert ei.value.lane_code == "too_many_writes" and ei.value.retry_after == 3
        lane.quota_bytes = 1
        with pytest.raises(LaneError) as ei:
            await c.write("r_chung", "Báo cáo/b.txt", b"bbbb", agent="w1", run="r_1", create=True)
        assert ei.value.lane_code == "storage_full" and ei.value.status == 507
        lane.quota_bytes = None
        # 404
        with pytest.raises(LaneError) as ei:
            await c.stat("r_chung", "khong-co")
        assert ei.value.lane_code == "not_found"
        # ro key → 403 read_only_key
        lane.scope = "ro"
        with pytest.raises(LaneError) as ei:
            await c.mkdir("r_chung", "X", agent="w1", run="r_1")
        assert ei.value.lane_code == "read_only_key"
        lane.scope = "rw"
        # idempotency: cùng khoá → cùng phản hồi, không tạo phiên bản thứ hai
        a = await c.write("r_chung", "Báo cáo/c.txt", b"c", agent="w1", run="r_1", create=True, idem="11111111-1111-1111-1111-111111111111")
        b = await c.write("r_chung", "Báo cáo/c.txt", b"c", agent="w1", run="r_1", create=True, idem="11111111-1111-1111-1111-111111111111")
        assert a["version_id"] == b["version_id"]
        # ghi đè thiếu If-Match → lỗi lập trình phía client, không gửi
        with pytest.raises(LaneError) as ei:
            await c.write("r_chung", "Báo cáo/c.txt", b"d", agent="w1", run="r_1")
        assert ei.value.lane_code == "precondition_required"
        # khoá sai → 401 giống nhau, lỗi không chứa khoá
        bad = LaneClient(lane.url, "tt_lane_" + "Z" * 43)
        with pytest.raises(LaneError) as ei:
            await bad.hello()
        assert ei.value.lane_code == "unauthorized" and "tt_lane_" not in str(ei.value) and "tt_lane_" not in repr(ei.value)
        await bad.aclose()
        # header proxy → 403 lane_not_local (server từ chối) — client không bao giờ gửi các header này
        assert not any(k.lower().startswith("x-forwarded") for r in lane.requests for k in (r.get("headers") or {}))
        # mạng chết → offline
        dead = LaneClient("http://127.0.0.1:1", KEY, timeout=2)
        with pytest.raises(LaneError) as ei:
            await dead.hello()
        assert ei.value.lane_code == "offline" and KEY not in str(ei.value)
        await dead.aclose()
    finally:
        await c.aclose()


async def test_client_sse_frames_cursor_and_last_event_id(lane):
    c = LaneClient(lane.url, KEY)
    try:
        cur0 = await c.cursor()
        got: list = []

        async def reader(cursor):
            async for ev in c.events(cursor, read_timeout=5):
                got.append(ev)
                if ev.type == "change" and ev.data.get("path") == "b.txt":
                    return

        t = asyncio.create_task(reader(cur0))
        await asyncio.sleep(0.2)
        lane.human_write("a.txt", "a")
        lane.human_write("b.txt", "b")
        await asyncio.wait_for(t, 5)
        ch = [e for e in got if e.type == "change"]
        assert [e.data["path"] for e in ch] == ["a.txt", "b.txt"] and ch[0].data["by_kind"] == "human" and ch[0].data["self"] is False
        assert all(e.id and e.id.startswith("c1.") for e in ch)
        # nối lại từ id của khung đầu (Last-Event-ID) → nhận lại đúng khung sau đó
        got2: list = []

        async def reader2(cursor):
            async for ev in c.events(cursor, read_timeout=5):
                got2.append(ev)
                if ev.type == "change":
                    return

        await asyncio.wait_for(reader2(ch[0].id), 5)
        assert got2[-1].data["path"] == "b.txt"
        assert any(r["headers"].get("Last-Event-ID") == ch[0].id for r in lane.requests if r["path"].endswith("/events"))
        # khoá bị thu hồi giữa luồng → khung key_revoked rồi đóng; lần nối sau 401
        got3: list = []

        async def reader3():
            async for ev in c.events(await c.cursor(), read_timeout=5):
                got3.append(ev)

        t3 = asyncio.create_task(reader3())
        await asyncio.sleep(0.2)
        lane.revoke()
        await asyncio.wait_for(t3, 5)
        assert got3[-1].type == "key_revoked"
        with pytest.raises(LaneError) as ei:
            async for _ in c.events(None):
                pass
        assert ei.value.lane_code == "unauthorized"
    finally:
        await c.aclose()


# ============================================================================ daemon: thiết lập khoá (chỉ người)
@pytest.fixture
async def daemon(hub_home):
    d = Daemon(hub_home)
    d.hub.start()
    yield d
    d.hub.runs.shutting_down = True
    await d.hub.filehub.stop()
    d.hub.store.close()


async def test_setup_stores_key_only_in_secret_store(daemon, lane, hub_home):
    d = daemon
    r = await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    assert r["server"] == "qltm" and r["key_prefix"] == KEY[:12] and r["user"] == "lamlinh" and r["roots"][0]["id"] == "r_chung"
    assert d.hub.secrets.get("FILEHUB_LANE_QLTM") == KEY
    assert (hub_home.home / "secrets" / "FILEHUB_LANE_QLTM").stat().st_mode & 0o777 == 0o600
    # khoá không ở config.yaml, DB (audit/event/state), kết quả RPC
    assert KEY not in (hub_home.home / "config.yaml").read_text()
    assert KEY not in json.dumps(r)
    dump = "\n".join(str(tuple(row)) for t in ("audit_log", "events", "hub_state") for row in d.hub.store.conn.execute(f"SELECT * FROM {t}").fetchall())
    assert KEY not in dump and "filehub.lane_setup" in dump  # audit có, khoá không
    st = await rpc(d, "filehub.lane_status", {})
    assert st["servers"][0]["state"] == "ok" and st["servers"][0]["key"] is True and KEY not in json.dumps(st)
    assert st["servers"][0]["expiry"]["days_left"] > 170  # khoá không có expires_at → 180 ngày từ lúc đặt
    t = await rpc(d, "filehub.lane_test", {"server": "qltm"})
    assert t["ok"] and t["protocol"] == 1
    # khoá sai dạng / URL không loopback / khoá hỏng → từ chối, không lưu
    for prm, msg in (({"server": "cchc", "url": lane.url, "key": "abc"}, "không đúng dạng"),
                     ({"server": "cchc", "url": "http://10.1.2.3:8223", "key": KEY}, "loopback|127.0.0.1"),
                     ({"server": "cchc", "url": lane.url, "key": "tt_lane_" + "Q" * 43}, "khoá không hợp lệ")):
        with pytest.raises(HubError, match=msg):
            await rpc(d, "filehub.lane_setup", prm)
    assert not d.hub.secrets.exists("FILEHUB_LANE_CCHC")
    # config.set không cất được khoá / URL ngoài máy
    with pytest.raises(Exception):
        d.hub.config.set("filehub.lane.servers", {"x": {"url": "http://127.0.0.1:1", "key": KEY}})
    with pytest.raises(Exception):
        d.hub.config.set("filehub.lane.servers", {"x": {"url": "https://kgs.example.com"}})
    with pytest.raises(Exception):
        d.hub.config.set("filehub.mode", "cloud")
    # disable: xoá khoá + cấu hình
    r = await rpc(d, "filehub.lane_disable", {"server": "qltm"})
    assert r["removed"] and not d.hub.secrets.exists("FILEHUB_LANE_QLTM") and not d.hub.filehub.lane.servers()
    states = [e.payload.get("state") for e in d.hub.store.query_events(types=["filehub.lane.state"])]
    assert "ok" in states and "not_configured" in states


async def test_version_mismatch_and_auth_failed_states(daemon, lane):
    d = daemon
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    lane.protocol = 2
    with pytest.raises(HubError, match="giao thức"):
        await rpc(d, "filehub.lane_test", {"server": "qltm"})
    assert d.hub.filehub.lane.states["qltm"]["state"] == "version_mismatch"
    lane.protocol = 1
    lane.revoke()
    with pytest.raises(HubError, match="khoá"):
        await rpc(d, "filehub.lane_test", {"server": "qltm"})
    assert d.hub.filehub.lane.states["qltm"]["state"] == "auth_failed"


def test_lane_rpcs_human_only_and_cli_blocked_for_agents():
    from cfharness.filehub.rpc import HUMAN_ONLY_LANE, MUTATING_FILEHUB
    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI

    for m in HUMAN_ONLY_LANE:
        assert m in auth.HUMAN_ONLY and m not in auth.MANAGER_ALLOWED, m
    assert {"filehub.lane_setup", "filehub.lane_disable"} <= set(MUTATING_FILEHUB) <= auth.all_mutating()
    assert "filehub.lane_status" not in auth.HUMAN_ONLY
    assert {"lane", "switch"} <= HUB_HUMAN_ONLY_CLI["filehub"]


async def test_agent_cannot_setup_lane(daemon, lane):
    """Người gọi là agent (Caller không phải người) → HUMAN_ONLY từ chối trước khi chạm khoá."""
    d = daemon
    c = auth.Caller(via="socket", pid=1, token_ok=True, run_id="r_x", agent_id="w1", evidence=["env_run_id"])
    with pytest.raises(HubError) as ei:
        await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY}, as_caller=c)
    assert ei.value.code == -32003 and not d.hub.secrets.exists("FILEHUB_LANE_QLTM")
    assert KEY not in "\n".join(str(tuple(r)) for r in d.hub.store.conn.execute("SELECT * FROM audit_log").fetchall())


def test_clone_package_drops_lane_key():
    from cfharness.transfer_secrets import CLONE_SKIP_PREFIXES

    assert any(p == "FILEHUB_LANE_" for p, _ in CLONE_SKIP_PREFIXES)
    from cfharness.hub_transfer import MACHINE_STATE

    assert "filehub.lane" in MACHINE_STATE
