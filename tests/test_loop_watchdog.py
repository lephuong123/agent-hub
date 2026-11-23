"""LoopWatchdog: loop đứng quá `stall_s` → dump stack (giới hạn số lần), nhịp trở lại → lên dây lại."""

import time

from cfharness.loop_watchdog import LoopWatchdog


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _wd(tmp_path, clock, stall=15.0):
    f = open(tmp_path / "faults.log", "a+", encoding="utf-8")
    return LoopWatchdog(lambda: f, stall_s=lambda: stall, clock=clock, max_dumps=2), f


def test_no_dump_while_beating(tmp_path):
    c = Clock()
    wd, f = _wd(tmp_path, c)
    for _ in range(30):
        c.t += 1
        wd.beat()
        assert wd.check() is False
    f.close()
    assert (tmp_path / "faults.log").read_text(encoding="utf-8") == ""


def test_stall_dumps_stack_limited_then_rearms(tmp_path):
    c = Clock()
    wd, f = _wd(tmp_path, c)
    wd.beat()
    c.t += 16
    assert wd.check() is True  # lần 1
    c.t += 1
    assert wd.check() is False  # chưa tới mốc dump kế (stall*4)
    c.t += 60
    assert wd.check() is True  # lần 2
    c.t += 600
    assert wd.check() is False  # đã đủ max_dumps
    wd.beat()  # loop chạy lại
    assert wd.stall_started is None and wd.dumps == 0
    c.t += 16
    assert wd.check() is True  # lần đứng mới → dump lại
    f.close()
    text = (tmp_path / "faults.log").read_text(encoding="utf-8")
    assert text.count("event loop daemon đứng") == 3
    assert "test_stall_dumps_stack_limited_then_rearms" in text  # stack thread hiện tại có trong dump


def test_disabled_when_zero(tmp_path):
    c = Clock()
    wd, f = _wd(tmp_path, c, stall=0)
    c.t += 1000
    assert wd.check() is False
    f.close()


def test_thread_detects_real_blocked_loop(tmp_path):
    """Thread thật: không ai gọi beat() (như loop bị code đồng bộ chặn) → faults.log có stack."""
    f = open(tmp_path / "faults.log", "a+", encoding="utf-8")
    wd = LoopWatchdog(lambda: f, stall_s=lambda: 0.2, check_s=0.05)
    wd.start()
    try:
        time.sleep(0.6)  # "loop" bị chặn
    finally:
        wd.stop()
        f.close()
    assert "event loop daemon đứng" in (tmp_path / "faults.log").read_text(encoding="utf-8")
