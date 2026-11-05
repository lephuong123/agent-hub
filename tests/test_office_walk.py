"""Văn phòng 3D — chế độ "Vào văn phòng" (đi bộ) + kiểm bố cục không trình duyệt (docs/dev-notes/office-walk.md).

- Kiểm bố cục: chạy `tests/office_layout_check.mjs` bằng Node (dựng World thật với canvas giả): số chỗ, chồng lấn, A* tới được,
  điểm xuất hiện của avatar ở giữa khu làm việc (xa cửa ra), tới được mọi phòng, đi thẳng qua cửa được, không xuyên tường.
- Chính sách giao diện (`tests/office_walk_ui_check.mjs`, hàm thuần của walk.js/util.js): gợi ý ẩn khi modal/chat/bảng mở và nằm
  sát mép dưới; không tự chọn agent khi đang mở các thứ đó; nhãn tên tránh chồng. Không có Node → skip.
- Tĩnh: nút chuyển chế độ trong office.html, walk.js không gọi mạng / không dựng HTML (đi bộ chỉ là cách XEM).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from cfharness.web import office as O
from test_web import _start, _stop  # noqa: F401 — dùng lại khung test web
from test_web_office import env  # noqa: F401 — fixture đăng nhập web

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(O.__file__).parent
STATIC = WEB / "static"
NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="cần Node để dựng World (ES module) không trình duyệt")
def test_office_layout_and_walk_reachability():
    r = subprocess.run([NODE, str(ROOT / "tests" / "office_layout_check.mjs"), str(ROOT)], capture_output=True, text=True,
                       encoding="utf-8", timeout=240)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("ok n=") == 10, r.stdout


@pytest.mark.skipif(not NODE, reason="cần Node để import module walk.js/util.js")
def test_walk_ui_policy_headless():
    r = subprocess.run([NODE, str(ROOT / "tests" / "office_walk_ui_check.mjs"), str(ROOT)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout and r.stdout.count("ok ") >= 14, r.stdout


def _fn(src: str, head: str) -> str:
    body = src[src.index(head):]
    return body[:body.index("\n  }\n")]


def test_walk_exit_door_only_on_e():
    """Chạm/bấm cửa ra trong chế độ đi bộ KHÔNG mở hộp thoại thoát; chỉ [E] (walkInteract) mở. Điểm xuất hiện không ở cửa."""
    office_js = (STATIC / "office.js").read_text(encoding="utf-8")
    pick = _fn(office_js, "function pick(ev)")
    assert "...(walk.active ? [] : world.exitMeshes || [])" in pick, "cửa ra vẫn bấm được trong chế độ đi bộ"
    assert pick.count("exitMeshes") == 1
    assert "flows.openLogout()" in _fn(office_js, "function walkInteract()")
    walk = (STATIC / "office" / "walk.js").read_text(encoding="utf-8")
    assert "this.pos = spawnPoint(world)" in walk and "deskSpawn(world)" in walk and "this.pos = { x: world.W - 1.4, z: world.D - 1.2 }" not in walk


def test_walk_hint_and_autoselect_respect_open_panels():
    """Gợi ý ẩn khi modal/chat/bảng mở (finding office.js:232); walkProximity không tự chọn agent khi đó (finding office.js:302)."""
    office_js = (STATIC / "office.js").read_text(encoding="utf-8")
    assert "const ui = () => ({ walk: walk.active, modal: modals.open, chat: chat.isOpen || talk.isOpen, board: !!focusBoard });" in office_js
    prox = _fn(office_js, "function walkProximity(now)")
    assert "if (id !== near && !walkBusy(ui())) {" in prox
    # mọi chỗ select() trong walkProximity nằm sau cổng walkBusy
    assert prox.index("walkBusy(ui())") < prox.index("select(")
    # hiện/ẩn gợi ý chỉ qua hintShown (mỗi khung + lúc bật/tắt), vị trí qua hintLayout
    assert re.findall(r"hint\.sprite\.visible = ([^;]+);", office_js) == ["false", "hintShown(ui())", "hintShown(ui())"]
    assert "hintLayout(camera.fov, camera.aspect)" in _fn(office_js, "function fitHint()")


@pytest.mark.skipif(not NODE, reason="cần Node cho node --check")
def test_office_js_syntax():
    files = [STATIC / "office.js", *sorted((STATIC / "office").glob("*.js"))]
    for f in files:
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{f.name}: {r.stderr}"


def test_walk_module_is_view_only():
    """Đi bộ chỉ là cách XEM: walk.js không gọi mạng, không dựng HTML, không đụng khoá phiên/CSRF."""
    walk = (STATIC / "office" / "walk.js").read_text(encoding="utf-8")
    for bad in ("fetch(", "XMLHttpRequest", "EventSource", "innerHTML", "outerHTML", "insertAdjacentHTML", "localStorage",
                "csrf", "ahub_k", "import("):
        assert bad not in walk, bad
    # chỉ vendor three 0.169.0 + module nội bộ, không thư viện ngoài
    imports = re.findall(r"^import .* from '([^']+)';", walk, re.M)
    assert imports and all(i.startswith("./") or i == "../vendor/three-0.169.0/three.module.min.js" for i in imports), imports
    office_js = (STATIC / "office.js").read_text(encoding="utf-8")
    assert "import { Walker, walkBusy, hintShown, hintLayout, coordinatorOf, nearDesk } from './office/walk.js';" in office_js
    # thao tác từ chế độ đi bộ đi qua đúng luồng sẵn có (chat / bảng / flows), không có đường ghi riêng
    fn = office_js[office_js.index("function walkInteract()"):]
    fn = fn[:fn.index("\n  }\n")]
    assert "chat.open()" in fn and "showBoard(" in fn and "flows.openLogout()" in fn and "doAct(" in fn and "fetch" not in fn
    # reduced-motion: bấm sàn dịch chuyển tức thời
    assert "goTo(h.point.x, h.point.z, reduced)" in office_js and "if (reduced) {" in walk


async def test_office_page_has_walk_toggle(env):  # noqa: F811
    await env.login()
    html = (await env.client.get("/office")).text
    m = re.search(r'<button [^>]*id="office-walk"[^>]*>', html)
    assert m and 'type="button"' in m.group(0) and 'aria-pressed="false"' in m.group(0) and " hidden" in m.group(0)
    p = re.search(r'<button [^>]*id="office-pov"[^>]*>', html)
    assert p and " hidden" in p.group(0)
    # không handler inline (CSP 'self'), nút 2D vẫn còn
    assert not re.search(r"<button [^>]*\son\w+=", html)
    assert 'id="office-to-2d"' in html


def test_office_render_loop_perf_contract():
    """Nhịp vẽ + đo hiệu năng (docs/dev-notes/office-3d-perf.md): chuyển động → theo màn hình, đứng yên → 30 FPS (frameGate);
    watchdog đếm nhịp rAF (không phụ thuộc giới hạn khung); shader biên dịch trước sau khi dựng World; LOD chibi mỗi khung;
    perf.js chỉ chạy khi có #perf3d (không global, không log, không mạng)."""
    office_js = (STATIC / "office.js").read_text(encoding="utf-8")
    assert "1000 / 30" not in office_js  # bỏ giới hạn cứng 30 FPS
    # cổng nhịp vẽ nằm trong office/pacer.js (vsync p20, khoá 30 khi quá tải); pacer vẫn gọi frameGate thuần của util.js
    assert "pacer.shouldDraw(ts, " in office_js and "gapEst" not in office_js
    assert "import { createPacer } from './office/pacer.js';" in office_js
    frame = office_js[office_js.index("function frame(ts)"):]
    # watchdog (hạ tier < 24, fallback < 10) đo TRƯỚC cổng nhịp vẽ → khung bỏ qua vì đứng yên vẫn được đếm
    # vị trí của LỜI GỌI (không phải chú thích): mốc 24 / 10 truyền vào watchdogStep, đứng trước cổng nhịp vẽ
    call = "watchdogStep(wd, ts, gap, document.hasFocus(), 24, 10, "
    assert call in frame and frame.index(call) < frame.index("pacer.shouldDraw(")
    assert "if (rebuilt) warmShaders();" in office_js and "renderer.compile(scene, camera)" in office_js
    assert "compileAsync(" not in office_js  # thăm dò isReady() ném lỗi khi vật liệu bị dispose giữa chừng
    assert "updateChibiLod(camera, Math.max(1, host.clientHeight));" in office_js
    # thả chuột ngoài canvas không được để `down` kẹt (moving() đúng mãi → vẽ 60 FPS mãi)
    assert "window.addEventListener('pointerup', release);" in office_js and "window.addEventListener('pointercancel', release);" in office_js
    assert "controls.dampingFactor = 1 - Math.pow(1 - DAMP_60, dt * 60);" in office_js
    perf = (STATIC / "office" / "perf.js").read_text(encoding="utf-8")
    assert "if (!perfFlags().perf || !renderer) return null;" in perf
    for bad in ("console.", "fetch(", "localStorage", "innerHTML", "Worker", "blob:"):
        assert bad not in perf, bad
    pacer = (STATIC / "office" / "pacer.js").read_text(encoding="utf-8")
    assert "frameGate(" in pacer and "from './util.js'" in pacer
    for bad in ("console.", "fetch(", "localStorage", "innerHTML", "import * as THREE"):
        assert bad not in pacer, bad


@pytest.mark.skipif(not NODE, reason="cần Node để dựng World (ES module) không trình duyệt")
def test_walk_motion_headless():
    """Động học đi bộ (walk.js): va chạm hình tròn trượt dọc tường liền mạch, qua mọi cửa, không lẹm; bộ điều khiển có vận tốc;
    camera spring-arm (docs/dev-notes/office-walk.md)."""
    r = subprocess.run([NODE, str(ROOT / "tests" / "office_walk_motion_check.mjs"), str(ROOT)], capture_output=True, text=True,
                       encoding="utf-8", timeout=240)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout and r.stdout.count("ok ") >= 5, r.stdout


@pytest.mark.skipif(not NODE, reason="cần Node để import office/pacer.js")
def test_office_pacer_headless():
    """Bộ điều nhịp (pacer.js): 60/30 đúng nhịp màn hình, rAF chậm vẽ mọi rAF, quá tải → khoá đều 30, gai/hitch không khoá,
    khoá lại nhân đôi thời gian, dt lượng tử theo vsync, việc phụ chạy ở rAF bị bỏ."""
    r = subprocess.run([NODE, str(ROOT / "tests" / "office_pacer_check.mjs"), str(ROOT)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout and r.stdout.count("ok ") >= 10, r.stdout
