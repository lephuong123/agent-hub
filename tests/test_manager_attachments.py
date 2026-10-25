"""Clipboard Windows (PowerShell) của `manager/attachments.py` — subprocess GIẢ (không đụng clipboard thật)."""

from __future__ import annotations

import base64
import os
import subprocess

import pytest

from cfharness.manager import attachments as A

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FakeRun:
    def __init__(self, *, write: bytes | None = None, rc: int = 0, stdout: bytes = b"", exc: Exception | None = None):
        self.write, self.rc, self.stdout, self.exc = write, rc, stdout, exc
        self.calls: list[dict] = []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": argv, **kw})
        if self.exc:
            raise self.exc
        if self.write is not None:
            with open(kw["env"]["CFHARNESS_CLIP_OUT"], "wb") as f:
                f.write(self.write)
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, b"")


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(A, "IS_WINDOWS", True)
    monkeypatch.setattr(A.shutil, "which", lambda n: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" if n.startswith("powershell") else None)

    def use(fake: FakeRun) -> FakeRun:
        monkeypatch.setattr(A.subprocess, "run", fake)
        return fake

    return use


def _script(call: dict) -> str:
    argv = call["argv"]
    return base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")


def test_windows_image_via_powershell_temp_file(win):
    fake = win(FakeRun(write=PNG))
    assert A.clipboard_image() == PNG
    c = fake.calls[0]
    for flag in ("-NoProfile", "-NonInteractive", "-STA"):
        assert flag in c["argv"]
    assert c["timeout"] == A.CLIP_TIMEOUT_S and c["stdin"] == subprocess.DEVNULL
    assert "Get-Clipboard -Format Image" in _script(c) and "System.Windows.Forms" in _script(c)
    assert not os.path.exists(c["env"]["CFHARNESS_CLIP_OUT"])  # tệp tạm bị xoá


@pytest.mark.parametrize("fake", [
    FakeRun(rc=3),  # không có ảnh
    FakeRun(exc=subprocess.TimeoutExpired("powershell", 5)),
    FakeRun(exc=OSError("không chạy được")),
    FakeRun(write=b"not a png"),  # không phải PNG
    FakeRun(write=b""),
])
def test_windows_image_failures_return_none(win, fake):
    win(fake)
    assert A.clipboard_image() is None
    if fake.calls and "env" in fake.calls[0]:
        assert not os.path.exists(fake.calls[0]["env"]["CFHARNESS_CLIP_OUT"])


def test_windows_image_over_cap_not_read(win, monkeypatch):
    monkeypatch.setattr(A, "CLIP_READ_CAP", 16)
    win(FakeRun(write=PNG))
    assert A.clipboard_image() is None


def test_windows_big_image_returned_so_ui_reports_size(win):
    big = PNG + b"\x00" * (A.MAX_IMAGE_BYTES + 1)
    win(FakeRun(write=big))
    img = A.clipboard_image()
    assert img is not None and len(img) > A.MAX_IMAGE_BYTES  # REPL/TUI tự báo "quá lớn" (giữ giới hạn hiện có)


def test_windows_text(win):
    fake = win(FakeRun(stdout="\ufeffxin chào\r\ndòng 2".encode("utf-8")))
    assert A.clipboard_text() == "xin chào\ndòng 2"
    assert "Get-Clipboard -Raw" in _script(fake.calls[0]) and "-NonInteractive" in fake.calls[0]["argv"]


@pytest.mark.parametrize("fake", [FakeRun(rc=1), FakeRun(exc=subprocess.TimeoutExpired("powershell", 5)), FakeRun(stdout=b"")])
def test_windows_text_failures_empty(win, fake):
    win(fake)
    assert A.clipboard_text() == ""


def test_windows_no_powershell(monkeypatch):
    monkeypatch.setattr(A, "IS_WINDOWS", True)
    monkeypatch.setattr(A.shutil, "which", lambda n: None)
    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: pytest.fail("không được gọi"))
    assert A.clipboard_image() is None and A.clipboard_text() == ""
