"""Lớp khác biệt hệ điều hành (`oscompat.py`) + kênh điều khiển (`transport.py`) — chạy được trên mọi nền tảng;
phần Job Object / bảng TCP chỉ chạy trên Windows."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from cfharness import oscompat, transport

win_only = pytest.mark.skipif(not oscompat.IS_WINDOWS, reason="chỉ Windows")


def test_pid_alive_self_and_missing():
    assert oscompat.pid_alive(os.getpid())
    assert not oscompat.pid_alive(None)
    assert not oscompat.pid_alive(0)


def test_pid_alive_does_not_kill(tmp_path):
    # Windows: os.kill(pid, 0) = TerminateProcess — pid_alive không được giết tiến trình nó kiểm
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert oscompat.pid_alive(p.pid)
        time.sleep(0.2)
        assert p.poll() is None
    finally:
        p.kill()
        p.wait()
    assert not oscompat.pid_alive(p.pid)


def test_transport_roundtrip_and_peer_pid(tmp_path):
    sock = tmp_path / "hub.sock"
    seen: dict = {}

    async def handle(reader, writer):
        seen["peer"] = transport.peer_pid(writer.get_extra_info("socket"))
        line = await reader.readline()
        writer.write(json.dumps({"echo": json.loads(line)}).encode() + b"\n")
        await writer.drain()
        writer.close()

    async def main():
        server = await transport.start_server(handle, sock, limit=1 << 16)
        try:
            def client():
                s = transport.connect(sock, 5)
                try:
                    s.sendall(b'{"x": 1}\n')
                    return s.makefile("rb").readline()
                finally:
                    s.close()

            line = await asyncio.get_running_loop().run_in_executor(None, client)
            r, w = await transport.open_connection(sock, limit=1 << 16)
            w.write(b'{"y": 2}\n')
            await w.drain()
            line2 = await r.readline()
            w.close()
            return line, line2
        finally:
            server.close()

    line, line2 = asyncio.run(main())
    assert json.loads(line) == {"echo": {"x": 1}}
    assert json.loads(line2) == {"echo": {"y": 2}}
    if sys.platform in ("darwin", "linux", "win32"):
        assert seen["peer"] == os.getpid()


@win_only
def test_endpoint_file_loopback_only(tmp_path):
    p = tmp_path / "hub.sock"
    p.write_text(json.dumps({"host": "0.0.0.0", "port": 1}), encoding="utf-8")
    with pytest.raises(OSError):
        transport.connect(p, 1)
    p.write_text("không phải json", encoding="utf-8")
    with pytest.raises(OSError):
        transport.connect(p, 1)


@win_only
def test_same_user():
    assert oscompat.same_user(os.getpid())
    assert not oscompat.same_user(4)  # System


@win_only
def test_job_contains_descendants_and_terminates():
    code = ("import subprocess, sys, time; "
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); print(c.pid, flush=True); time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, **oscompat.new_group_kwargs(suspended=True))
    reg = oscompat.JobRegistry()
    job = reg.create("r_test")
    job.adopt(p.pid, resume=True)
    child = int(p.stdout.readline())
    try:
        assert job.contains(p.pid) and job.contains(child)
        assert reg.run_of(child) == "r_test"
        assert not job.contains(os.getpid())
        assert job.terminate()
        p.wait(10)
        deadline = time.monotonic() + 5
        while oscompat.pid_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not oscompat.pid_alive(child)
    finally:
        reg.close_all()
        p.stdout.close()


@win_only
def test_ancestors_and_start_time():
    from cfharness.auth import ancestors

    chain = ancestors(os.getpid())
    assert chain[0] == os.getpid() and os.getppid() in chain
    st = oscompat.win_start_time(os.getpid())
    assert st is not None and abs(time.time() - st) < 24 * 3600
