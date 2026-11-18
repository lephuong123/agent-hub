"""`hello.server.public_url` (§C.3, tuỳ chọn) → link "Mở trên Không gian số" cho hub chỉ dán khoá cổng (chưa đặt `filehub.server`).

- Kiểm nghiêm (`clean_public_url`): chỉ https, không userinfo / đường dẫn / truy vấn, trần độ dài.
- `filehub.server` trống → hub tự đặt + đánh dấu `server_auto`; hello sau đổi public_url → cập nhật bản tự đặt; người đã đặt → KHÔNG ghi đè.
- Nhiều cổng: link của mỗi cổng về web của chính nó (khi `filehub.server` không do người đặt).
Server lane GIẢ (`fake_kgs_lane.py`), không gọi Không gian số thật.
"""

from __future__ import annotations

import pytest

from cfharness.filehub.lane.service import clean_public_url
from fake_kgs_lane import KEY, FakeLane
from lane_fake import close, make
from m7_fake import rpc


@pytest.mark.parametrize("raw,want", [
    ("https://tangthu.congty.vn", "https://tangthu.congty.vn"),
    ("https://TangThu.vn/", "https://tangthu.vn"),
    ("https://x.vn:443", "https://x.vn"),
    ("https://x.vn:8443", "https://x.vn:8443"),
    ("http://x.vn", None),
    ("https://u:p@x.vn", None),
    ("https://u@x.vn", None),
    ("https://x.vn/files", None),
    ("https://x.vn?a=1", None),
    ("https://x.vn#f", None),
    ("https://", None),
    ("https://x.vn:99999", None),
    ("https://x .vn", None),
    ("https://x.vn\n", None),
    ("https://x.vn\\@y.vn", None),
    ("javascript:alert(1)", None),
    ("https://bé.vn", None),
    ("https://" + "a" * 250 + ".vn", None),
    (None, None), (5, None), ("", None),
])
def test_clean_public_url(raw, want):
    assert clean_public_url(raw) == want


@pytest.fixture
async def env(hub_home):
    lane = FakeLane(public_url="https://kgs.congty.vn")
    lane.human_write("Báo cáo/yeu-cau.md", "viết\n")
    d, _s = await make(hub_home)
    yield d, lane
    await close(d)
    lane.close()


def _server(d):
    return d.hub.config.get("filehub.server")


async def _link(d, path):
    return (await rpc(d, "filehub.link", {"path": path}))["url"]


async def test_setup_sets_server_from_hello_and_links_use_it(env):
    d, lane = env
    assert not _server(d)
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    assert _server(d) == "https://kgs.congty.vn"
    it = lane.roots["r_chung"].items["Báo cáo/yeu-cau.md"]
    url = await _link(d, "chung/Báo cáo/yeu-cau.md")
    assert url == f"https://kgs.congty.vn/files/{lane.roots['r_chung'].items['Báo cáo'].id}?file={it.id}"
    # hello mới đổi public_url → bản TỰ đặt được cập nhật
    lane.public_url = "https://moi.congty.vn/"
    await rpc(d, "filehub.lane_test", {"server": "qltm"})
    assert _server(d) == "https://moi.congty.vn"
    assert (await _link(d, "chung/Báo cáo/yeu-cau.md")).startswith("https://moi.congty.vn/files/")
    # trường không hợp lệ (http) → bỏ qua, không đổi gì
    lane.public_url = "http://moi.congty.vn"
    await rpc(d, "filehub.lane_test", {"server": "qltm"})
    assert _server(d) == "https://moi.congty.vn"


async def test_human_set_server_never_overwritten(env):
    d, lane = env
    d.hub.config.set("filehub.server", "https://nguoi.dat.vn")
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    assert _server(d) == "https://nguoi.dat.vn"
    assert (await _link(d, "chung/Báo cáo/yeu-cau.md")).startswith("https://nguoi.dat.vn/files/")
    lane.public_url = "https://khac.vn"
    await rpc(d, "filehub.lane_test", {"server": "qltm"})
    assert _server(d) == "https://nguoi.dat.vn"


async def test_human_overrides_auto_value_then_kept(env):
    d, lane = env
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    assert _server(d) == "https://kgs.congty.vn"
    d.hub.config.set("filehub.server", "https://nguoi.dat.vn")  # người đặt lại sau khi hub tự đặt
    lane.public_url = "https://khac.vn"
    await rpc(d, "filehub.lane_test", {"server": "qltm"})
    assert _server(d) == "https://nguoi.dat.vn"
    assert (await _link(d, "chung/Báo cáo/yeu-cau.md")).startswith("https://nguoi.dat.vn/files/")


async def test_no_field_keeps_old_behaviour(hub_home):
    lane = FakeLane()
    lane.human_write("a.md", "x\n")
    d, _s = await make(hub_home)
    try:
        await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
        assert not _server(d)
        assert await _link(d, "chung/a.md") is None
    finally:
        await close(d)
        lane.close()


async def test_two_servers_each_link_to_its_own_web(hub_home):
    a = FakeLane(public_url="https://a.vn", roots=("r_a",))
    b = FakeLane(public_url="https://b.vn", roots=("r_b",))
    ia = a.human_write("x.md", "1\n", root="r_a")
    ib = b.human_write("y.md", "2\n", root="r_b")
    d, _s = await make(hub_home)
    try:
        await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": a.url, "key": KEY})
        await rpc(d, "filehub.lane_setup", {"server": "cchc", "url": b.url, "key": KEY})
        assert _server(d) == "https://a.vn"  # cổng đầu tiên tự đặt; cổng sau không giành
        assert await _link(d, "a/x.md") == f"https://a.vn/files/{a.roots['r_a'].root_id_item}?file={ia.id}"
        assert await _link(d, "b/y.md") == f"https://b.vn/files/{b.roots['r_b'].root_id_item}?file={ib.id}"
        await rpc(d, "filehub.lane_test", {"server": "cchc"})
        assert _server(d) == "https://a.vn"
    finally:
        await close(d)
        a.close()
        b.close()
