"""Văn phòng 3D — chế độ "Vào văn phòng" (đi bộ) + kiểm bố cục không trình duyệt (docs/dev-notes/office-walk.md).

- Kiểm bố cục: chạy `tests/office_layout_check.mjs` bằng Node (dựng World thật với canvas giả): số chỗ, chồng lấn, A* tới được,
  avatar từ cửa vào tới được mọi phòng, đi thẳng qua cửa được, không xuyên tường. Không có Node → skip.
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
    assert "import { Walker } from './office/walk.js';" in office_js
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
