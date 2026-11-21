"""Chạy lệnh hệ thống cho admind — CHỈ argv (không shell), chương trình phải thuộc danh sách trắng, có timeout, stdin tuỳ chọn.

Test tiêm `FakeRunner` (ghi lại lệnh, trả kết quả theo mẫu) — không bao giờ gọi sudo/launchctl/sysadminctl/cloudflared thật.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

ALLOWED = {
    "sysadminctl", "dscl", "dseditgroup", "launchctl", "sudo", "cloudflared", "chown", "chmod", "mkdir", "install", "id", "createhomedir",
    "tar", "openssl", "df", "du", "ps", "pgrep", "curl", "security", "/bin/rm",
}
ALLOWED_ABS = re.compile(r"^(/opt/cfharness/bin/cfharness|/Users/[A-Za-z0-9_.-]+/\.local/bin/(cfharness|claude|uv)|/usr/bin/[a-z]+|/usr/sbin/[a-z]+|"
                         r"/bin/[a-z]+|/opt/homebrew/bin/[a-z]+|/usr/local/bin/[a-z]+)$")


class RunnerError(RuntimeError):
    pass


@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _allowed(argv: list[str]) -> bool:
    if not argv or any(not isinstance(a, str) or "\x00" in a for a in argv):
        return False
    prog = argv[0]
    return prog in ALLOWED or bool(ALLOWED_ABS.match(prog))


class Runner:
    def __init__(self, *, timeout: float = 600.0) -> None:
        self.timeout = timeout

    def run(self, argv: list[str], *, stdin: str | None = None, timeout: float | None = None, env: dict[str, str] | None = None,
            cwd: str | None = None) -> Result:
        if not _allowed(argv):
            raise RunnerError(f"lệnh không thuộc danh sách trắng: {argv[:1]}")
        e = {"PATH": "/opt/cfharness/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin", "LANG": "en_US.UTF-8"}
        if env:
            e.update(env)
        try:
            from ..oscompat import no_window_kwargs

            p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout or self.timeout, env=e, cwd=cwd,
                               check=False, **no_window_kwargs())
        except FileNotFoundError:
            return Result(127, "", f"không có chương trình {argv[0]}")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"quá thời gian {timeout or self.timeout}s")
        return Result(p.returncode, p.stdout or "", p.stderr or "")

    def as_user(self, user: str, argv: list[str], **kw) -> Result:  # noqa: ANN003
        """Chạy dưới uid user hub (giống `cfh()` của setup-hub-user.sh): HOME/USER/LOGNAME đúng, PATH có ~/.local/bin."""
        h = f"/Users/{user}"
        path = f"{h}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        return self.run(["sudo", "-u", user, "-H", "env", f"HOME={h}", f"USER={user}", f"LOGNAME={user}", f"PATH={path}", *argv], **kw)

    def cfh(self, user: str, *args: str, **kw) -> Result:  # noqa: ANN003
        return self.as_user(user, [f"/Users/{user}/.local/bin/cfharness", *args], **kw)


@dataclass
class FakeRunner(Runner):
    """Runner giả cho test: `rules` = [(hàm khớp argv, Result | hàm argv→Result)]; mặc định trả rc=0."""

    rules: list[tuple[Callable[[list[str]], bool], object]] = field(default_factory=list)
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)
    timeout: float = 1.0

    def on(self, pred: Callable[[list[str]], bool] | str, result: object) -> "FakeRunner":
        if isinstance(pred, str):
            needle = pred
            self.rules.insert(0, (lambda a: needle in " ".join(a), result))
        else:
            self.rules.insert(0, (pred, result))
        return self

    def run(self, argv: list[str], *, stdin: str | None = None, timeout: float | None = None, env: dict[str, str] | None = None,
            cwd: str | None = None) -> Result:
        if not _allowed(argv):
            raise RunnerError(f"lệnh không thuộc danh sách trắng: {argv[:1]}")
        self.calls.append((list(argv), stdin))
        for pred, res in self.rules:
            if pred(argv):
                return res(argv) if callable(res) else res  # type: ignore[return-value]
        return Result(0, "", "")

    def ran(self, needle: str) -> bool:
        return any(needle in " ".join(a) for a, _ in self.calls)


def os_name() -> str:
    return os.uname().sysname if hasattr(os, "uname") else "Windows"  # type: ignore[attr-defined]
