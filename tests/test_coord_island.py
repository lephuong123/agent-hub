"""GĐ2 (phần 1) trang Điều phối — island Preact `/manager?ui=next` (web/coord.py, static/coord/*.js, docs/dev-notes/dieu-phoi-island.md):

- thư viện vendored đúng phiên bản ghim (sha256 khớp README, import map trỏ đúng thư mục, wheel có đủ tệp);
- trang island: import map nội tuyến được phép bằng băm sha256 trong CSP của RIÊNG trang này (trang khác vẫn `script-src 'self'`),
  ảnh chụp đầu nhúng bằng `|tojson` (chữ agent không thoát khỏi <script>), `.mjs` phục vụ đúng MIME, `?v=` → immutable;
- chưa có điều phối viên → trang cũ (phần chỉ định); `/api/manager/status`; `/api/manager/*` hết phiên → 401 JSON;
- POST /manager/read → `ws.status` mới (số chưa đọc của tab) cho island;
- store/reducer + client SSE (Node: `tests/coord_store_check.mjs`). Không gọi `claude`."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from cfharness.web import coord as C
from cfharness.web import manager_feed as F

from test_web import ISOLATED, _start, _stop, until  # noqa: F401
from test_web_manager import designate, send
from test_web_manager_v2 import menv  # noqa: F401 — fixture daemon thật + manager stub

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "cfharness" / "web" / "static"
NODE = shutil.which("node")


# =========================================================================== vendored
def test_vendor_pinned_versions_and_hashes_match_readme():
    readme = (STATIC / "vendor" / "README.md").read_text(encoding="utf-8")
    want = {"preact": "11.1.1", "htm": "3.1.1", "@preact/signals-core": "1.14.5", "@preact/signals": "2.11.4",
            "streaming-markdown": "0.2.15", "dompurify": "3.4.18"}
    for spec, path in C.VENDOR.items():
        f = STATIC / path
        assert f.is_file(), path
        pkg = spec.split("/")[0] if not spec.startswith("@") else "/".join(spec.split("/")[:2])
        assert want[pkg] in path, (spec, path)  # phiên bản nằm trong tên thư mục
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        assert digest in readme, f"sha256 của {path} không khớp README (tệp bị sửa?)"
        assert (f.parent / "LICENSE").is_file(), f"thiếu LICENSE cạnh {path}"
    # signals 2.11.4 import đúng các specifier có trong import map
    sig = (STATIC / C.VENDOR["@preact/signals"]).read_text(encoding="utf-8")
    for spec in re.findall(r'from"([^"]+)"', sig):
        assert spec in C.VENDOR, spec


def test_import_map_versioned_and_covers_island_modules():
    imap = json.loads(C.import_map())["imports"]
    for spec, path in C.VENDOR.items():
        assert imap[spec].startswith("/static/" + path + "?v="), imap[spec]
    mods = sorted(p.name for p in (STATIC / "coord").glob("*.js"))
    assert {"main.js", "store.js", "sse.js", "views.js", "composer.js", "util.js", "api.js"} <= set(mods)
    for m in mods:
        assert imap[f"/static/coord/{m}"].startswith(f"/static/coord/{m}?v=")
    csp = C.csp_with(C.import_map())
    h = base64.b64encode(hashlib.sha256(C.import_map().encode()).digest()).decode()
    assert f"script-src 'self' 'sha256-{h}'" in csp and "'unsafe-inline'" not in csp


def test_island_sources_have_no_unsafe_dom_sinks():
    """Chữ agent chỉ qua children của htm (escape); HTML duy nhất = `it.html` (present.md của server)."""
    for f in (STATIC / "coord").glob("*.js"):
        src = f.read_text(encoding="utf-8")
        assert "innerHTML" not in src and "insertAdjacentHTML" not in src and "eval(" not in src, f.name
        if f.name == "views.js":
            assert src.count("dangerouslySetInnerHTML") == 1 and "__html: it.html" in src
        else:
            assert "dangerouslySetInnerHTML" not in src, f.name


def test_island_js_size_budget():
    """JS của island (không tính markdown/sanitizer), gzip từng tệp như khi phục vụ: preact + hooks + signals + htm + coord/*.js.
    Ngân sách kế hoạch là < 30 KB; GĐ2 phần 1 đo ~40 KB (vendor ~11 KB, coord ~29 KB), phần 2 (dòng thời gian, chạy chữ markdown, cuộn,
    thẻ duyệt, vẽ dần) đo ~50 KB (coord ~38 KB, trong đó ~8 KB là chú thích tiếng Việt — không có bước build/minify). Chặn hồi quy ở 56 KB;
    thu về 30 KB là việc của GĐ3. Đây là cỡ NGUỒN; cỡ PHỤC VỤ (đã thu gọn, web/jsmin.py) kiểm ở test_coord_verify_r1."""
    import gzip

    files = [STATIC / C.VENDOR[k] for k in ("preact", "preact/hooks", "@preact/signals-core", "@preact/signals", "htm")]
    files += list((STATIC / "coord").glob("*.js"))
    total = sum(len(gzip.compress(f.read_bytes(), 9)) for f in files)
    assert total < 57_000, total  # nguồn có chú thích (verify r2 thêm ~0.5 KB: canh im lặng SSE, neo cuộn, thử lại tải nền; r3 ~0.3 KB: vẽ mục gần ngay khi cuộn, gỡ khoảng dư); cỡ phục vụ kiểm riêng


@pytest.mark.skipif(not NODE, reason="cần Node để chạy store/SSE của island")
def test_store_and_sse_reducer_headless():
    r = subprocess.run([NODE, str(ROOT / "tests" / "coord_store_check.mjs"), str(ROOT)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout and "ALL OK" in r.stdout and r.stdout.count("\nok ") >= 40, r.stdout


@pytest.mark.skipif(not NODE, reason="cần Node để chạy logic dòng thời gian của island")
def test_timeline_scroll_stream_logic_headless():
    """GĐ2 phần 2: cuộn dính đáy, gom delta theo khung hình, bỏ trùng theo id, thẻ duyệt theo giờ, lọc URL, duyệt/trả lời lạc quan + hoàn tác."""
    r = subprocess.run([NODE, str(ROOT / "tests" / "coord_timeline_check.mjs"), str(ROOT)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout and "ALL OK" in r.stdout and r.stdout.count("\nok ") >= 35, r.stdout


def test_no_infinite_box_shadow_animation():
    """GĐ3: không còn animation box-shadow vô hạn ở đâu (app.css + coord.css): chấm đang chạy / mic dùng ::after transform + opacity."""
    css = (STATIC / "app.css").read_text(encoding="utf-8") + (STATIC / "coord.css").read_text(encoding="utf-8")
    infinite = {m.group(1) for m in re.finditer(r"animation:\s*([\w-]+)[^;]*infinite", css)}
    assert {"ring-ping"} <= infinite
    for name in infinite:
        kf = re.search(r"@keyframes " + re.escape(name) + r" \{(.*)$", css, re.M)  # khung hình viết trên một dòng
        assert kf, name
        assert "box-shadow" not in kf.group(1), name
    assert ".ws-dot.run::after" in css and ".voice-mic.on::after" in css
    coord = (STATIC / "coord.css").read_text(encoding="utf-8")
    assert "content-visibility: auto" in coord and "contain-intrinsic-size: auto 400px" in coord


def test_streaming_markdown_filters_urls_via_dompurify():
    src = (STATIC / "coord" / "md.js").read_text(encoding="utf-8")
    assert "DOMPurify.isValidAttribute" in src and "safeUrl(value)" in src and "innerHTML" not in src
    assert 'from "streaming-markdown"' in src and 'from "dompurify"' in src


@pytest.mark.skipif(not shutil.which("uv"), reason="cần uv để dựng wheel")
def test_wheel_contains_vendor_and_island(tmp_path):
    r = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    whl = next(tmp_path.glob("*.whl"))
    names = set(zipfile.ZipFile(whl).namelist())
    want = [f"cfharness/web/static/{x}" for x in [*C.VENDOR.values(), "coord/main.js", "coord/store.js", "coord.css", "vendor/README.md"]]
    want.append("cfharness/web/templates/manager_next.html")
    for n in want:
        assert n in names, n


# =========================================================================== trang + API qua daemon thật
async def test_island_page_csp_boot_and_static(menv):
    e = menv
    await e.login()
    r = await e.client.get("/manager?ui=next")
    assert r.status_code == 200 and 'action="/do/manager_set"' in r.text  # chưa có điều phối viên → phần chỉ định của trang cũ
    await designate(e)
    j = await send(e, '</script><script>alert(1)</script> chào')
    cid = j["conversation_id"]
    assert await until(lambda: (e.hub.store.get_run(j["run_id"]) or {}).get("status") == "completed")
    r = await e.client.get(f"/manager?ui=next&c={cid}")
    assert r.status_code == 200 and 'id="coord"' in r.text
    m = re.search(r'<script type="importmap">(.*?)</script>', r.text, re.S)
    assert m, "thiếu import map"
    h = base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode()
    csp = r.headers["content-security-policy"]
    assert f"'sha256-{h}'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["cache-control"] == "no-store"
    boot_raw = re.search(r'<script type="application/json" id="coord-boot">(.*?)</script>', r.text, re.S).group(1)
    assert "</script" not in boot_raw.lower() and "<script" not in boot_raw.lower()  # |tojson escape `<`
    boot = json.loads(boot_raw)
    assert boot["snap"]["conv"] == cid and boot["csrf"] and boot["icons"].startswith("/static/icons.svg")
    assert boot["hub"] == {"estop": False, "admin_estop": False}
    assert any(it.get("text", "").startswith("</script>") for it in boot["snap"]["items"] if it["kind"] == "user")
    # trang khác (kể cả trang Điều phối cũ ?ui=old) vẫn CSP gốc
    assert "sha256-" not in (await e.client.get("/manager?ui=old")).headers["content-security-policy"]
    assert "sha256-" not in (await e.client.get("/runs")).headers["content-security-policy"]
    e.client.cookies.clear()
    # .mjs đúng MIME + ?v= → immutable
    u = json.loads(m.group(1))["imports"]["preact"]
    s = await e.client.get(u)
    assert s.status_code == 200 and s.headers["content-type"].startswith("text/javascript")
    assert "immutable" in s.headers.get("cache-control", "")


async def test_status_api_and_auth(menv):
    e = menv
    r = await e.client.get("/api/manager/status")
    assert r.status_code == 401  # API JSON: hết phiên → 401, không chuyển hướng trang đăng nhập
    await e.login()
    await designate(e)
    r = await e.client.get("/api/manager/status")
    assert r.status_code == 200
    m = r.json()["manager"]
    assert m["agent_id"] == "boss" and m["name"] == "Điều phối" and m["status_label"] and m["cost_today"].startswith("$")
    assert r.json()["hub"] == {"estop": False, "admin_estop": False}  # dải ESTOP của island
    s = (await e.client.get("/api/manager/snapshot")).json()
    assert s["manager"]["name"] == "Điều phối" and "status_tone" in s["manager"]


async def test_read_kicks_ws_status(menv):
    e = menv
    await e.login()
    await designate(e)
    j = await send(e, "xin chào")
    cid = j["conversation_id"]
    assert await until(lambda: (e.hub.store.get_run(j["run_id"]) or {}).get("status") == "completed")
    await e.client.get("/api/manager/snapshot")  # bật Feed
    feed = e.app.state.mgr_feed
    assert await until(lambda: feed.tabs is not None)
    called = []
    orig = feed.kick_tabs
    feed.kick_tabs = lambda: (called.append(1), orig())[1]  # type: ignore[method-assign]
    r = await e.post("/manager/read", {"c": cid}, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 200 and called, "đọc xong phải tính lại số chưa đọc của tab (ws.status)"
    assert isinstance(F.tabs_from({"enabled": True}), dict)
