"""Windows: đường dẫn luật quyền / hook / sandbox trong settings của run (guard/runtime.py) — chạy được trên mọi nền tảng
bằng cách bật/tắt `runtime.IS_WINDOWS` (kết quả POSIX phải giữ nguyên byte-by-byte)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cfharness.guard import runtime as rt


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(rt, "IS_WINDOWS", True)


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(rt, "IS_WINDOWS", False)


def test_rule_path_posix_identity(posix):
    assert rt.rule_path("/Users/a/.cfharness/policy/") == "/Users/a/.cfharness/policy"
    assert "/" + rt.rule_path("/x/y") == "//x/y"
    assert rt.sb_path("/x/y") == "/x/y"


def test_rule_path_windows(win):
    # kiểm thật claude 2.1.303: `Read(//c/Users/…)` chặn Read `C:\Users\…`
    assert rt.rule_path(r"C:\Users\A\.cfharness\policy" + "\\") == "/c/Users/A/.cfharness/policy"
    assert rt._abs_rule(r"D:\hub\secrets/**") == "//d/hub/secrets/**"
    assert rt.sb_path(r"C:\Users\A\x") == "C:/Users/A/x"


def test_pattern_matches_windows_target(win):
    assert rt.pattern_matches("Edit(//c/hub/policy/**)", "Edit", {"file_path": r"C:\hub\policy\x.yaml"})
    assert rt.pattern_matches("Edit(//c/hub/policy)", "Edit", {"file_path": r"C:\hub\policy"})
    assert not rt.pattern_matches("Edit(//c/hub/policy/**)", "Edit", {"file_path": r"C:\hub\policyx\a"})


def test_hook_command_windows_forward_slashes(win):
    cmd = rt.hook_command("r1", Path(r"C:\Users\A\.cfharness\hub.sock"), 60, {"python": r"C:\Py\python.exe"})
    assert "\\" not in cmd  # Git Bash: `\` là ký tự thoát
    assert cmd.startswith("C:/Py/python.exe -I -m cfharness.guard.hook") and cmd.endswith("|| exit 2")
    assert "--socket C:/Users/A/.cfharness/hub.sock" in cmd


def test_sandbox_not_fail_closed_on_windows(win, tmp_path):
    (tmp_path / "policy").mkdir()
    s = rt.build_settings({}, run_id="r1", socket=tmp_path / "hub.sock", policy_dir=tmp_path / "policy", guard_cfg={}, approval_timeout_s=300)
    assert s["sandbox"]["failIfUnavailable"] is False  # true → CLI từ chối mọi run ("Windows sandbox is not active")
    assert not any("\\" in r for r in s["permissions"]["deny"])
    assert not any("\\" in p for p in s["sandbox"]["filesystem"]["denyWrite"] + s["sandbox"]["filesystem"]["denyRead"])
    s2 = rt.build_settings({}, run_id="r1", socket=tmp_path / "hub.sock", policy_dir=tmp_path / "policy",
                           guard_cfg={"sandbox": {"fail_if_unavailable_windows": True}}, approval_timeout_s=300)
    assert s2["sandbox"]["failIfUnavailable"] is True


def test_sandbox_fail_closed_default_posix(posix, tmp_path):
    (tmp_path / "policy").mkdir()
    s = rt.build_settings({}, run_id="r1", socket=tmp_path / "hub.sock", policy_dir=tmp_path / "policy", guard_cfg={}, approval_timeout_s=300)
    assert s["sandbox"]["failIfUnavailable"] is True


def test_sandbox_supported_matches_platform():
    assert rt.sandbox_supported() is (sys.platform != "win32")


def test_windows_autorun_dirs_only_on_windows(posix):
    assert rt.windows_autorun_dirs() == []


def test_windows_autorun_dirs(win):
    d = rt.windows_autorun_dirs()
    assert any(x.endswith("Startup") for x in d) and any(x.endswith("WindowsPowerShell") for x in d)
    zones = rt.exec_zone_deny_rules()
    assert any("Startup" in r for r in zones)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_eval_command_checker_uses_git_bash():
    from cfharness.evals.checkers import _shell_argv

    argv = _shell_argv("echo hi")
    assert "system32" not in argv[0].lower()
