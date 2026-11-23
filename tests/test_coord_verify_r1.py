"""Island Điều phối — các lỗi verify vòng 1 (docs/dev-notes/dieu-phoi-island.md):

- `/manager?ui=next` có phạm vi luồng (`?w=`, `?c=` hội thoại thuộc luồng, `?new=1&w=`) trả 500 (`|first` của Jinja bị ghi đè);
- ảnh chụp `?tab=` đăng ký phạm vi TRƯỚC khi lấy cursor (message của đích vừa mở không bị luồng SSE bỏ qua);
- `Watch`: dọn theo TTL không được bỏ phạm vi của luồng SSE đang chạy (hub bận liên tục), `op=set` đặt lại tại chỗ, quá MAX_WATCH bỏ cái cũ
  nhất; subscription daemon tự tắt khi không ai xem dù hub im lặng;
- `/api/manager/act`: kiểm CSRF trước cache idem, không cache lỗi 4xx;
- ghi chú lượt lỗi trong ảnh chụp mang id `n<run>` (khớp client);
- JS island phục vụ ĐÃ THU GỌN (`web/jsmin.py`): đúng cú pháp (`node --check`), store/SSE chạy đúng trên bản thu gọn, ngân sách gzip."""

from __future__ import annotations

import gzip
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from cfharness.web import coord as C
from cfharness.web import jsmin
from cfharness.web import manager_feed as F

from test_web import ISOLATED, _start, _stop, until  # noqa: F401
from test_web_manager import designate, mgr_rpc, send
from test_web_manager_v2 import FakeSource, menv, settle, take  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "cfharness" / "web" / "static"
NODE = shutil.which("node")


# =========================================================================== trang island có phạm vi luồng (blocker)
async def test_island_page_scoped_to_workstream_renders(menv):
    e = menv
    await e.login()
    await designate(e)
    w = await mgr_rpc(e, "workstream.create", {"title": "Sửa lỗi <đăng nhập>", "key": "LH-9"})
    r = await e.post("/manager/send", {"text": "chào luồng", "w": w["id"]}, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 200, r.text
    j = r.json()
    cid = j["conversation_id"]
    assert await until(lambda: (e.hub.store.get_run(j["run_id"]) or {}).get("status") not in ("queued", "running"))
    for url in (f"/manager?ui=next&w={w['id']}", f"/manager?ui=next&c={cid}", f"/manager?ui=next&new=1&w={w['id']}", "/manager?ui=next"):
        r = await e.client.get(url)
        assert r.status_code == 200, (url, r.text[:500])
        assert 'id="coord"' in r.text, url
    r = await e.client.get(f"/manager?ui=next&w={w['id']}")
    assert "Hội thoại · Sửa lỗi &lt;đăng nhập&gt;" in r.text  # tiêu đề nhóm vẽ sẵn ở server, chữ đã escape


# =========================================================================== ảnh chụp ?tab= đăng ký phạm vi trước cursor
async def test_snapshot_with_tab_registers_watch_before_cursor(menv):
    e = menv
    await e.login()
    await designate(e)
    j = await send(e, "xin chào")
    cid = j["conversation_id"]
    assert await until(lambda: (e.hub.store.get_run(j["run_id"]) or {}).get("status") not in ("queued", "running"))
    feed = F.feed_of(type("R", (), {"app": e.app})())  # type: ignore[arg-type]
    seen: list[tuple[str, int]] = []
    orig_watch = feed.watch

    def spy(key, convs, wss, **kw):
        feed.append({"t": "probe"}, F.ALL)  # đánh dấu thời điểm: watch sau cursor → id dấu > cursor
        seen.append(("watch", feed.n))
        return orig_watch(key, convs, wss, **kw)

    feed.watch = spy  # type: ignore[method-assign]
    s = (await e.client.get(f"/api/manager/snapshot?c={cid}&tab=tabA")).json()
    assert seen and seen[0][1] <= F.parse_id(s["cursor"])[1], (seen, s["cursor"])  # watch có TRƯỚC khi lấy cursor
    ws_ = [w for w in feed.watches.values() if cid in w.convs]
    assert ws_, "ảnh chụp ?tab= phải đăng ký hội thoại cho luồng SSE của tab"
    # Sảnh (không c): đăng ký phạm vi Sảnh trước, rồi thêm hội thoại ảnh chụp chọn
    s2 = (await e.client.get("/api/manager/snapshot?tab=tabB")).json()
    assert any(F.LOBBY in w.ws and (not s2.get("conv") or s2["conv"] in w.convs) for w in feed.watches.values())
    # không tab → không đăng ký gì thêm
    n = len(feed.watches)
    await e.client.get(f"/api/manager/snapshot?c={cid}")
    assert len(feed.watches) == n


# =========================================================================== Watch: TTL, đặt lại tại chỗ, thứ tự bỏ
def test_watch_add_evicts_oldest_and_readd_moves_to_end():
    w = F.Watch()
    w.add([f"c{i}" for i in range(F.MAX_WATCH)], [])
    w.add(["c0"], [])  # mở lại c0 → thành mới nhất
    w.add(["moi"], [])
    assert "c0" in w.convs and "moi" in w.convs and "c1" not in w.convs and len(w.convs) == F.MAX_WATCH


async def test_live_stream_keeps_watch_scope_while_hub_busy(monkeypatch):
    src = FakeSource()
    src.runs["r1"] = {"id": "r1", "run_opts": {"manager_conversation": "cA"}}
    src.runs["r2"] = {"id": "r2", "run_opts": {"manager_conversation": "cLate"}}
    f = F.Feed(src.call, src.open_sub)
    await f.ensure()
    try:
        w = f.watch("tab1", ["cA"], [])
        gen = F.stream(f, w, F.parse_id(f.cursor), heartbeat_s=30)
        src.ev("run.started", {}, run="r1")
        got = await take(gen, 1, pred=lambda d: d["t"] == "run.start")
        assert got
        # giả lập: hub bận > TTL (không nhịp ping nào) — trước đây `seen` chỉ làm mới ở ping → tab bị dọn, watch mới tách khỏi luồng
        monkeypatch.setattr(F, "WATCH_TTL_S", 0.0)
        w.seen = time.monotonic() - 10
        f.watch("tab-khac", [], [])  # lần gọi bất kỳ chạy dọn TTL
        assert f.watches.get("tab1") is w, "luồng đang chạy giữ Watch → không được dọn"
        w2 = f.watch("tab1", ["cLate"], [])  # tab mở hội thoại mới sau đó
        assert w2 is w
        f.watch("tab1", ["cLate"], [], replace=True)
        assert f.watches["tab1"] is w and list(w.convs) == ["cLate"]  # op=set đặt lại TẠI CHỖ
        src.ev("run.started", {}, run="r2")
        got = await take(gen, 1, pred=lambda d: d["t"] == "run.start" and d.get("conv") == "cLate")
        assert got, "hội thoại mở sau khi hub bận lâu vẫn phải nhận msg.*"
        await gen.aclose()
        assert w.live == 0
    finally:
        await f.close()


async def test_feed_subscription_stops_when_idle_even_if_hub_quiet():
    src = FakeSource()
    f = F.Feed(src.call, src.open_sub, idle_stop_s=0.2)
    try:
        await f.ensure()  # chỉ ảnh chụp, không luồng SSE nào; hub im lặng (không event)
        assert f.task is not None and not f.task.done()
        assert await until(lambda: f.task.done(), timeout=5), "subscription daemon phải tắt khi rảnh dù không có event"
        assert all(s.closed for s in src.subs)
        # mở lại khi có người xem
        await f.ensure()
        assert not f.task.done()
    finally:
        await f.close()


# =========================================================================== act: CSRF trước cache idem, không cache 4xx
async def test_act_guard_before_idem_cache_and_no_4xx_cache(menv):
    e = menv
    await e.login()
    await designate(e)
    j = await send(e, "hỏi tôi đi")
    cid, run = j["conversation_id"], j["run_id"]
    assert await until(lambda: (e.hub.store.get_run(run) or {}).get("status") == "completed")
    q = e.hub.questions.open(e.hub.store.get_run(run), "Chọn?", ["A", "B"], source="manager", conversation_id=cid)
    form = {"id": q["id"], "keys": "1", "idem": "k-r1", "confirm": "yes"}
    bad = await e.post("/api/manager/act/answer", form | {"csrf": "sai"}, headers={"X-Ahub-Fetch": "1"})
    assert bad.status_code == 403
    ok = await e.post("/api/manager/act/answer", form, headers={"X-Ahub-Fetch": "1"})
    assert ok.status_code == 200 and ok.json().get("ok") is True and not ok.json().get("replayed"), ok.text  # 403 không bị cache
    again = await e.post("/api/manager/act/answer", form | {"csrf": "sai"}, headers={"X-Ahub-Fetch": "1"})
    assert again.status_code == 403 and not again.json().get("replayed")  # bản phát lại cũng phải qua CSRF
    rep = await e.post("/api/manager/act/answer", form, headers={"X-Ahub-Fetch": "1"})
    assert rep.status_code == 200 and rep.json().get("replayed") is True


def test_snapshot_note_id_matches_client_run_end_note():
    assert F.item_json({"kind": "note", "text": "Lỗi", "tone": "danger", "run_id": "r5"})["id"] == "nr5"
    assert F.item_json({"kind": "note", "text": "x"})["id"] is None


# =========================================================================== JS thu gọn khi phục vụ
def test_jsmin_preserves_strings_templates_regex():
    src = ('const a = "// không phải chú thích", b = \'/* cũng không */\';\n'
           "/* khối\n chú thích */\n"
           "const r = /a\\/b[/]c/g.test(x) ? 1 : 2; // cuối dòng\n"
           "const t = html`<div class=${a + `x${b}`}>  giữ   khoảng trắng\n  </div>`;\n"
           "const d = x / 2 / y, e = a - -b, f = c + +d;\n"
           "return /re/.source;\n")
    out = jsmin.minify(src)
    assert '"// không phải chú thích"' in out and "'/* cũng không */'" in out
    assert "khối" not in out and "cuối dòng" not in out
    assert "/a\\/b[/]c/g.test(x)" in out
    assert "html`<div class=${a+`x${b}`}>  giữ   khoảng trắng\n  </div>`" in out
    assert "x/2/y" in out and "a- -b" in out and "c+ +d" in out and "return/re/.source" in out


@pytest.mark.skipif(not NODE, reason="cần Node để kiểm cú pháp + chạy store trên bản thu gọn")
def test_minified_island_parses_and_store_checks_pass(tmp_path):
    st = tmp_path / "src" / "cfharness" / "web" / "static"
    (st / "coord").mkdir(parents=True)
    shutil.copytree(STATIC / "vendor", st / "vendor")
    for f in sorted((STATIC / "coord").glob("*.js")):
        m = jsmin.minify(f.read_text(encoding="utf-8"))
        (st / "coord" / f.name).write_text(m, encoding="utf-8")
        chk = tmp_path / (f.stem + ".mjs")
        chk.write_text(m, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(chk)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert r.returncode == 0, (f.name, r.stderr)
    for script in ("coord_store_check.mjs", "coord_timeline_check.mjs"):
        r = subprocess.run([NODE, str(ROOT / "tests" / script), str(tmp_path)], capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert r.returncode == 0 and "ALL OK" in r.stdout, (script, r.stdout[-2000:], r.stderr)


def test_served_island_js_gzip_budget():
    """Ngân sách gzip của island (không markdown/sanitizer) tính trên bản PHỤC VỤ (đã thu gọn): vendor (preact+hooks+signals+htm) + coord.
    Kế hoạch: < 30 KB. Bản thu gọn không đổi tên biến (không bước build) đo ~39 KB — chặn hồi quy ở 42 KB; xuống 30 KB cần minifier đổi tên
    (bước build) hoặc người dùng chốt ngân sách mới (xem dev note)."""
    files = [STATIC / C.VENDOR[k] for k in ("preact", "preact/hooks", "@preact/signals-core", "@preact/signals", "htm")]
    total = sum(len(gzip.compress(f.read_bytes(), 9)) for f in files)
    total += sum(len(gzip.compress(jsmin.minify(f.read_text(encoding="utf-8")).encode(), 9)) for f in (STATIC / "coord").glob("*.js"))
    assert total < 42_000, total


async def test_coord_js_route_serves_minified(menv):
    e = menv
    await e.login()
    src = (STATIC / "coord" / "store.js").read_text(encoding="utf-8")
    r = await e.client.get("/static/coord/store.js?v=1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert "immutable" in r.headers.get("cache-control", "")
    assert len(r.text) < len(src) * 0.8 and "Island Điều phối — store" not in r.text and "export function createStore" in r.text
    assert (await e.client.get("/static/coord/khong-co.js")).status_code == 404
    # tệp tĩnh khác vẫn qua StaticFiles
    assert (await e.client.get("/static/app.js")).status_code == 200
