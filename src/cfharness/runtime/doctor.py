"""`cfharness doctor`: kiểm runtime codex / grok (chỉ khi CLI đã cài — không cài = không dùng, không báo lỗi).

Mỗi kiểm trả (tên, ok, chi tiết, level). Đo thật: docs/spikes/codex-grok-spike-report.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..oscompat import IS_WINDOWS, exec_argv, no_window_kwargs


def _run(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(exec_argv(argv), capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL, **no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def runtime_checks(cfg: dict[str, Any]) -> list[tuple[str, bool, str, str]]:
    from .rpcbase import binary_version

    out: list[tuple[str, bool, str, str]] = []
    rt = cfg.get("runtime") or {}
    # ---- codex
    ccfg = rt.get("codex") or {}
    cbin = str(ccfg.get("binary") or "codex")
    if shutil.which(cbin):
        out.append(("phiên bản codex", bool(binary_version(cbin, 0)), str(binary_version(cbin) or "không đọc được `--version`"), "warning"))
        code, text = _run([cbin, "login", "status"])
        out.append(("đăng nhập codex", code == 0 and "logged in" in text.lower(),
                    text.strip().splitlines()[0][:200] if text.strip() else f"exit {code} — chạy `codex login`", "warning"))
        if IS_WINDOWS and ccfg.get("windows_sandbox", "mxc") == "mxc":
            code, text = _run([cbin, "-c", 'windows.sandbox="mxc"', "sandbox", "--permission-profile", ":workspace", "--",
                               "cmd.exe", "/d", "/c", "echo MXC_OK"], 60)
            out.append(("sandbox MXC của codex", "MXC_OK" in text,
                        "OK" if "MXC_OK" in text else f"không chạy được (cần Windows 11 24H2+): {text.strip()[:200]}", "warning"))
        agents_md = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "AGENTS.md"
        out.append(("~/.codex/AGENTS.md", not agents_md.exists(),
                    "không có" if not agents_md.exists() else f"{agents_md} được nạp vào MỌI run codex (không có cờ tắt) — xoá hoặc chấp nhận",
                    "warning"))
    # ---- grok
    gcfg = rt.get("grok") or {}
    gbin = str(gcfg.get("binary") or "grok")
    if shutil.which(gbin) or (Path.home() / ".grok" / "bin" / ("grok.exe" if IS_WINDOWS else "grok")).exists():
        exe = shutil.which(gbin) or str(Path.home() / ".grok" / "bin" / ("grok.exe" if IS_WINDOWS else "grok"))
        out.append(("phiên bản grok", bool(binary_version(exe, 0)), str(binary_version(exe) or "không đọc được `--version`"), "warning"))
        if not shutil.which(gbin):
            out.append(("grok trong PATH", False, f"đặt runtime.grok.binary = {exe}", "warning"))
        auth = Path(os.environ.get("GROK_AUTH_PATH") or (Path(os.environ.get("GROK_HOME") or (Path.home() / ".grok")) / "auth.json"))
        out.append(("đăng nhập grok", auth.exists(), str(auth) if auth.exists() else "chưa có auth.json — chạy `grok login`", "warning"))
    return out
