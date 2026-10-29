"""Bản chuẩn .pth/sitecustomize theo interpreter (`sys.prefix`): đổi venv/uv tool không còn báo `guard.env_tamper` mãi mãi,
nhưng trong CÙNG prefix, .pth mới/sửa vẫn bị bắt."""

from __future__ import annotations

import pytest

from cfharness.guard import integrity
from cfharness.hub import Hub
from cfharness.paths import HubPaths
from cfharness.runtime.fake import FakeRuntime


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Hai interpreter giả: prefix A (uv tool cũ) và B (.venv của repo); `active` chọn site-packages đang chạy."""
    sp = {"A": tmp_path / "A" / "lib" / "site-packages", "B": tmp_path / "B" / "lib" / "site-packages"}
    for d in sp.values():
        d.mkdir(parents=True)
    (sp["A"] / "_cfharness.pth").write_text("/uv/tools/cfharness\n")
    (sp["B"] / "_editable_impl_cfharness.pth").write_text("import _editable_impl_cfharness\n")
    (sp["B"] / "_virtualenv.pth").write_text("import _virtualenv\n")
    active = {"name": "A"}

    def files():
        d = sp[active["name"]]
        return {str(p): integrity._sha(p.read_bytes()) for p in d.iterdir()}

    monkeypatch.setattr(integrity, "python_env_files", files)
    monkeypatch.setattr(integrity, "env_prefix", lambda: str(tmp_path / active["name"]))
    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    yield h, sp, active
    h.store.close()


def _tamper_events(h):
    return [e for e in h.store.query_events() if e.type == "guard.env_tamper"]


def test_switching_interpreter_is_not_tamper(env):
    h, sp, active = env
    assert integrity.check_python_env(h) is None  # A: bản chuẩn đầu
    active["name"] = "B"
    assert integrity.check_python_env(h) is None  # B lần đầu: bản chuẩn mới cho B, không phải tamper
    assert integrity.check_python_env(h) is None  # khởi động lại vẫn êm
    active["name"] = "A"
    assert integrity.check_python_env(h) is None  # quay lại A: bản chuẩn A còn nguyên
    assert not _tamper_events(h)


def test_new_pth_in_same_prefix_still_detected(env):
    h, sp, active = env
    active["name"] = "B"
    assert integrity.check_python_env(h) is None
    (sp["B"] / "zz_evil.pth").write_text("import os; os.system('x')\n")
    info = integrity.check_python_env(h)
    assert info and any("zz_evil.pth" in x for x in info["added"])
    (sp["B"] / "_virtualenv.pth").write_text("import evil\n")
    info = integrity.check_python_env(h)
    assert info and any("_virtualenv.pth" in x for x in info["changed"])
    assert len(_tamper_events(h)) == 2


def test_accept_adopts_current_prefix_only(env):
    h, sp, active = env
    assert integrity.check_python_env(h) is None  # A
    active["name"] = "B"
    assert integrity.check_python_env(h) is None
    (sp["B"] / "new_pkg.pth").write_text("/x\n")
    assert integrity.check_python_env(h)  # cảnh báo
    assert integrity.check_python_env(h, adopt=True) is None  # `guard integrity accept`
    assert integrity.check_python_env(h) is None
    active["name"] = "A"
    (sp["A"] / "zz_evil.pth").write_text("import os\n")
    info = integrity.check_python_env(h)  # bản chuẩn A không bị accept của B xoá
    assert info and any("zz_evil.pth" in x for x in info["added"])


def test_legacy_flat_baseline_migrated(env):
    h, sp, active = env
    # bản chuẩn dạng cũ: dict phẳng chứa .pth của interpreter A (uv tool) — daemon nay chạy B
    h.store.state_set("pyenv_baseline", {str(p): integrity._sha(p.read_bytes()) for p in sp["A"].iterdir()})
    active["name"] = "B"
    assert integrity.check_python_env(h) is None  # trước đây: tamper mãi (added = .pth của B, removed = của A)
    assert not _tamper_events(h)
    st = h.store.state_get("pyenv_baseline")
    assert st["v"] == 2 and set(st["by_prefix"]) == {integrity.env_prefix()}


def test_legacy_baseline_same_prefix_keeps_detection(env):
    h, sp, active = env
    h.store.state_set("pyenv_baseline", {str(p): integrity._sha(p.read_bytes()) for p in sp["A"].iterdir()})
    (sp["A"] / "zz_evil.pth").write_text("import os\n")  # thêm trước lần kiểm đầu sau nâng cấp
    info = integrity.check_python_env(h)
    assert info and any("zz_evil.pth" in x for x in info["added"])
