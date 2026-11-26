"""Poppler cho tool Read (`pdf_support.py`): dò pdftoppm có cache, kiểm `tools.poppler_path`, ghi chú system prompt khi thiếu,
thêm thư mục poppler vào CUỐI PATH của run, mục `hub doctor`, kiểm cấu hình `attachments.pdf.*`. Không gọi claude."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cfharness import pdf_support as ps
from cfharness.config import HubConfig, validate_setting
from cfharness.prompt import build_system_prompt

EXE = "pdftoppm.exe" if sys.platform == "win32" else "pdftoppm"


@pytest.fixture(autouse=True)
def _fresh_cache(request, monkeypatch):
    ps.reset_cache()
    if request.node.name != "test_probe_runs_binary_like_claude_code":  # pdftoppm giả trong test là file rác: coi `pdftoppm -v` chạy được nếu file còn
        monkeypatch.setattr(ps, "probe", lambda exe: Path(exe).is_file() or exe.startswith("/usr/"))
    yield
    ps.reset_cache()


@pytest.fixture
def poppler(tmp_path: Path) -> Path:
    d = tmp_path / "poppler" / "bin"
    d.mkdir(parents=True)
    (d / EXE).write_bytes(b"fake")
    (d / "git.exe").write_bytes(b"not used")
    return d


@pytest.fixture
def no_system_pdftoppm(monkeypatch):
    """Máy test có thể đã cài poppler — ép `shutil.which` không thấy pdftoppm trên PATH của daemon."""
    real = ps.shutil.which
    monkeypatch.setattr(ps.shutil, "which", lambda name, *a, **k: None if name == ps.PDFTOPPM else real(name, *a, **k))


def test_validate_poppler_path(poppler: Path, tmp_path: Path):
    ps.validate_poppler_path(None)
    ps.validate_poppler_path(str(poppler))
    empty = tmp_path / "empty"
    empty.mkdir()
    bad = [("", "đường dẫn"), (123, "đường dẫn"), ("relative/bin", "tuyệt đối"), (str(tmp_path / "missing"), "không có thư mục"),
           (str(empty), "không có pdftoppm"), (str(poppler) + os.pathsep + str(empty), "MỘT thư mục"),
           (str(poppler) + "\x00", "không hợp lệ"), (str(poppler / EXE), "không có thư mục")]
    for value, msg in bad:
        with pytest.raises(ValueError, match=msg):
            ps.validate_poppler_path(value)


def test_config_validation_tools_and_attachments(poppler: Path, tmp_path: Path):
    validate_setting("tools.poppler_path", str(poppler))
    validate_setting("tools.poppler_path", None)
    validate_setting("tools", {"poppler_path": str(poppler)})
    for key, value in [("tools.poppler_path", "rel"), ("tools", {"other": 1}), ("tools.other", "x"), ("tools", "x")]:
        with pytest.raises(ValueError):
            validate_setting(key, value)
    validate_setting("attachments.pdf.split_pages", 30)
    validate_setting("attachments.pdf.split_pages", 100)
    validate_setting("attachments.pdf.timeout_s", 50)
    validate_setting("attachments.pdf", {"split": False, "max_parts": 5, "timeout_s": 12.5})
    validate_setting("attachments", {"pdf": {"split_pages": 1}})
    for key, value in [("attachments.pdf.split_pages", 0), ("attachments.pdf.split_pages", 2.5), ("attachments.pdf.split_pages", True),
                       ("attachments.pdf.max_parts", 201), ("attachments.pdf.timeout_s", 1),
                       ("attachments.pdf.split_pages", 101),  # phần > 100 trang: Read đọc cả phần một lần — quá nặng / quá 600 trang
                       ("attachments.pdf.timeout_s", 51),  # quá hạn RPC web 60 s → người thấy lỗi gửi mà hub vẫn nhận tin ("attachments.pdf.split", "yes"),
                       ("attachments.pdf.nope", 1), ("attachments.other", {}), ("attachments", []), ("attachments.pdf", 3)]:
        with pytest.raises(ValueError):
            validate_setting(key, value)
    cfg = HubConfig(tmp_path / "config.yaml")
    assert cfg.get("tools.poppler_path") is None and cfg.get("attachments.pdf.split_pages") == 50
    cfg.set("tools.poppler_path", str(poppler))
    assert cfg.get("tools.poppler_path") == str(poppler)
    with pytest.raises(ValueError):
        cfg.set("tools.poppler_path", str(tmp_path))


def test_tools_branch_is_integrity_protected_and_web_readonly():
    from cfharness.guard.integrity import SECURITY_KEYS
    from cfharness.web.settings_schema import BY_KEY, READONLY

    assert "tools" in SECURITY_KEYS
    assert BY_KEY["tools.poppler_path"].protected and READONLY == ()  # khoá bảo vệ: web chỉ qua config_set_protected
    assert {"attachments.pdf.split", "attachments.pdf.split_pages", "attachments.pdf.max_parts"} <= set(BY_KEY)


def test_status_uses_config_and_cache_refreshes_on_change(poppler: Path, no_system_pdftoppm, monkeypatch):
    calls = []
    real_find = ps._find_in_dir
    monkeypatch.setattr(ps, "_find_in_dir", lambda d: calls.append(d) or real_find(d))
    off = ps.pdftoppm_status({})
    assert off == {"usable": False, "path": None, "source": None, "configured": None, "broken": None}
    assert ps.system_note({}) == ps.PDF_SYSTEM_NOTE
    cfg = {"tools": {"poppler_path": str(poppler)}}
    on = ps.pdftoppm_status(cfg)
    assert on["usable"] and on["source"] == "config" and on["path"] == str(poppler / EXE)
    n = len(calls)
    assert ps.pdftoppm_status(cfg)["usable"] and len(calls) == n  # cache: không dò lại
    assert ps.system_note(cfg) is None
    assert not ps.pdftoppm_status({})["usable"]  # đổi cấu hình → khoá cache khác
    (poppler / EXE).unlink()
    assert ps.pdftoppm_status(cfg)["usable"]  # còn trong TTL
    assert not ps.pdftoppm_status(cfg, ttl_s=0)["usable"]  # hết TTL → dò lại


def test_status_prefers_path(monkeypatch):
    monkeypatch.setattr(ps.shutil, "which", lambda name, *a, **k: "/usr/bin/pdftoppm" if name == ps.PDFTOPPM else None)
    st = ps.pdftoppm_status({"tools": {"poppler_path": None}})
    assert st["usable"] and st["source"] == "path" and ps.system_note({}) is None
    # pdftoppm thấy trên PATH daemon → thư mục đó cũng vào path_extra của run (env_extra của run có thể thay PATH)
    assert ps.run_path_extra({}) == [str(Path("/usr/bin/pdftoppm").parent)]


def test_probe_runs_binary_like_claude_code(tmp_path: Path, monkeypatch):
    # thật: không có file / file rác (không chạy được) → False
    assert not ps.probe(str(tmp_path / "missing" / EXE))
    junk = tmp_path / EXE
    junk.write_bytes(b"not a program")
    assert not ps.probe(str(junk))
    # quy tắc của Claude Code: mã 0, hoặc mã ≠ 127 VÀ có stderr
    import subprocess as sp

    seen = []
    for rc, err, ok in ((0, b"", True), (99, b"pdftoppm version 24.02.0", True), (1, b"", False), (127, b"not found", False)):
        monkeypatch.setattr(ps.subprocess, "run", lambda argv, **k: seen.append(argv) or sp.CompletedProcess(argv, rc, b"", err))
        assert ps.probe("/x/pdftoppm") is ok
    assert seen[0] == ["/x/pdftoppm", "-v"]


def test_broken_binary_is_not_usable(poppler: Path, no_system_pdftoppm, monkeypatch, hub_home, tmp_path: Path):
    # pdftoppm có file nhưng `-v` hỏng → KHÔNG coi là dùng được (giữ lời nhắc "never pass pages"), doctor cảnh báo
    monkeypatch.setattr(ps, "probe", lambda exe: False)
    cfg = {"tools": {"poppler_path": str(poppler)}}
    st = ps.pdftoppm_status(cfg)
    assert not st["usable"] and st["broken"] == str(poppler / EXE) and ps.system_note(cfg) == ps.PDF_SYSTEM_NOTE
    from cfharness import hub_doctor as D

    HubConfig(hub_home.config).set("tools.poppler_path", str(poppler))
    c = {x["id"]: x for x in D.run(hub_home, claude_binary=str(tmp_path / "no-claude"))["checks"]}["pdftoppm"]
    assert not c["ok"] and c["level"] == "warning" and "-v" in c["detail"] and "scoop install poppler" in c["fix"]


def test_system_prompt_block_only_when_missing():
    with_note = build_system_prompt("a1", "soul", pdf_note=ps.PDF_SYSTEM_NOTE)
    assert "# Reading PDF files" in with_note and "never pass `pages`" in with_note
    assert with_note.index("# Reading PDF files") < with_note.index("# Persona (SOUL)")
    assert "# Reading PDF files" not in build_system_prompt("a1", "soul")
    assert "# Reading PDF files" not in build_system_prompt("a1", "soul", pdf_note=None)


def test_append_path_appends_once_and_keeps_variable_name(poppler: Path):
    sep = os.pathsep
    env = {"PATH": sep.join(["/a", "/b"])}
    ps.append_path(env, [str(poppler)])
    assert env["PATH"].split(sep) == ["/a", "/b", str(poppler)]  # CUỐI PATH: không che lệnh sẵn có
    ps.append_path(env, [str(poppler)])
    assert env["PATH"].split(sep).count(str(poppler)) == 1
    assert ps.append_path({"X": "1"}, [str(poppler)])["PATH"] == str(poppler)
    assert ps.append_path({"PATH": "/a"}, ["/x" + sep + "/evil"])["PATH"] == "/a"  # giá trị nhiều mục → bỏ
    if sys.platform == "win32":
        env = {"Path": "C:\\a"}
        ps.append_path(env, [str(poppler)])
        assert list(env) == ["Path"] and env["Path"].endswith(str(poppler))


def test_run_env_appends_poppler_dir(poppler: Path):
    from cfharness.runtime.claude_cli import ClaudeCliRuntime

    rt = ClaudeCliRuntime({"env_extra": {"PATH": "/only/this"}})
    env = rt._run_env(SimpleNamespace(env_extra={}, path_extra=[str(poppler)]))
    key = next(k for k in env if k.upper() == "PATH")
    assert env[key].split(os.pathsep) == ["/only/this", str(poppler)]
    env = rt._run_env(SimpleNamespace(env_extra={}))  # spec cũ không có path_extra
    assert env[next(k for k in env if k.upper() == "PATH")] == "/only/this"


async def test_run_spec_gets_path_extra_and_system_note(hub_home, poppler: Path, no_system_pdftoppm, tmp_path: Path):
    from cfharness.hub import Hub
    from cfharness.runtime import FakeRuntime

    h = Hub(hub_home, runtime=FakeRuntime())
    h.start()
    try:
        h.agents.create({"id": "a1", "name": "A1"})

        async def spec_of(prompt: str):
            n = len(h.runs._runtime.started)
            h.runs.submit("a1", prompt)
            for _ in range(500):
                if len(h.runs._runtime.started) > n:
                    return h.runs._runtime.started[-1]
                await asyncio.sleep(0.01)
            raise AssertionError("run không khởi động")

        spec = await spec_of("x")
        assert spec.path_extra == [] and "# Reading PDF files" in (spec.system_prompt or "")
        h.config.set("tools.poppler_path", str(poppler))
        spec = await spec_of("y")
        assert spec.path_extra == [str(poppler)] and "# Reading PDF files" not in (spec.system_prompt or "")
        (poppler / EXE).unlink()  # thư mục hết hợp lệ sau khi đặt → bỏ qua lúc spawn (không chặn run)
        ps.reset_cache()
        spec = await spec_of("z")
        assert spec.path_extra == [] and "# Reading PDF files" in (spec.system_prompt or "")
    finally:
        await asyncio.sleep(0.2)
        h.store.close()


def test_doctor_pdftoppm_check(hub_home, poppler: Path, no_system_pdftoppm, tmp_path: Path):
    from cfharness import hub_doctor as D

    def check(rep):
        return {x["id"]: x for x in rep["checks"]}["pdftoppm"]

    c = check(D.run(hub_home, claude_binary=str(tmp_path / "no-claude")))
    assert not c["ok"] and c["level"] == "warning" and "scoop install poppler" in c["fix"] and "winget install oschwartz10612.Poppler" in c["fix"]
    assert "tools.poppler_path" in c["fix"]
    HubConfig(hub_home.config).set("tools.poppler_path", str(poppler))
    c = check(D.run(hub_home, claude_binary=str(tmp_path / "no-claude")))
    assert c["ok"] and c["level"] == "ok" and str(poppler / EXE) in c["detail"]
    (poppler / EXE).unlink()  # cấu hình trỏ thư mục không còn pdftoppm → cảnh báo + cách sửa
    c = check(D.run(hub_home, claude_binary=str(tmp_path / "no-claude")))
    assert not c["ok"] and c["level"] == "warning" and "tools.poppler_path" in c["fix"]


def test_eval_overlay_prompt_matches_production_pdf_note(tmp_path: Path, monkeypatch):
    # prompt eval phải giống prompt run thật: máy không có pdftoppm → cùng khối "# Reading PDF files"
    from cfharness.evals import overlay

    seen: list = []
    import cfharness.prompt as P

    real = P.build_system_prompt
    monkeypatch.setattr(P, "build_system_prompt", lambda *a, **k: seen.append(k.get("pdf_note")) or real(*a, **k))
    hub = SimpleNamespace(config={}, knowledge=None)
    monkeypatch.setattr(ps, "pdf_render_available", lambda cfg=None: False)
    a = overlay.prompt_fingerprint(hub, "a1", tmp_path)
    monkeypatch.setattr(ps, "pdf_render_available", lambda cfg=None: True)
    b = overlay.prompt_fingerprint(hub, "a1", tmp_path)
    assert seen == [ps.PDF_SYSTEM_NOTE, None] and a != b


async def test_status_never_probes_on_event_loop(monkeypatch, no_system_pdftoppm, poppler: Path):
    # trong event loop: không chạy `pdftoppm -v` tại chỗ — chưa có cache → `pending` (an toàn: còn lời nhắc), dò trong thread nền;
    # cache hết hạn → trả giá trị cũ ngay, dò lại nền
    import threading
    import time as _t

    gate = threading.Event()
    loop_thread = threading.get_ident()
    probes: list[int] = []

    def slow_probe(exe):
        probes.append(threading.get_ident())
        gate.wait(5)
        return True

    monkeypatch.setattr(ps, "probe", slow_probe)
    cfg = {"tools": {"poppler_path": str(poppler)}}
    t0 = _t.monotonic()
    st = ps.pdftoppm_status(cfg)
    assert _t.monotonic() - t0 < 0.5 and st["pending"] and not st["usable"]
    assert ps.system_note(cfg) == ps.PDF_SYSTEM_NOTE and ps.run_path_extra(cfg) == [str(poppler)]
    gate.set()
    for _ in range(200):
        if ps.pdftoppm_status(cfg).get("usable"):
            break
        await asyncio.sleep(0.01)
    assert ps.pdftoppm_status(cfg)["usable"] and probes and loop_thread not in probes
    gate.clear()
    t0 = _t.monotonic()
    st = ps.pdftoppm_status(cfg, ttl_s=0)  # hết hạn: giá trị cũ, không chờ
    assert _t.monotonic() - t0 < 0.5 and st["usable"] and "pending" not in st
    gate.set()
    # code async muốn kết quả chắc chắn: to_thread (block mặc định True ngoài event loop)
    assert (await asyncio.to_thread(ps.pdftoppm_status, cfg, ttl_s=0))["usable"]


def test_doctor_no_restart_note_when_pdftoppm_works_from_path(hub_home, monkeypatch, tmp_path: Path):
    from cfharness import hub_doctor as D

    monkeypatch.setattr(ps.shutil, "which", lambda name, *a, **k: "/usr/bin/pdftoppm" if name == ps.PDFTOPPM else None)
    c = {x["id"]: x for x in D.run(hub_home, claude_binary=str(tmp_path / "no-claude"))["checks"]}["pdftoppm"]
    assert c["ok"] and c["detail"] == "/usr/bin/pdftoppm" and "khởi động lại" not in c["detail"]
