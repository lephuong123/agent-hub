"""F18: suite phải chạy được bên trong một lượt agent cfharness — biến môi trường của run không rò vào test."""
from __future__ import annotations

import os
import subprocess
import sys

from conftest import RUN_ENV_VARS


def test_run_env_vars_stripped():
    for k in ("CFHARNESS_HOME", "CFHARNESS_RUN_ID", "CFHARNESS_SOCKET"):
        assert k in RUN_ENV_VARS
    leaked = [k for k in RUN_ENV_VARS if k in os.environ]
    assert leaked == [], f"biến môi trường của lượt chạy rò vào test: {leaked}"


def test_child_process_does_not_inherit_run_env():
    code = "import os,sys; sys.stdout.write(','.join(k for k in os.environ if k in ('CFHARNESS_RUN_ID','CFHARNESS_HOME')))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out == ""


def test_own_process_environ_has_no_run_id():
    """Daemon in-process đọc env của pytest qua auth.proc_environ — không được thấy RUN_ID của lượt agent bao ngoài."""
    import cfharness.auth as auth

    env = auth.proc_environ(os.getpid()) or {}
    assert not any(k in env for k in RUN_ENV_VARS)
