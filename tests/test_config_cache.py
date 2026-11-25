"""plan-api-caching PR1: HubConfig gộp DEFAULTS một lần mỗi lần nạp; get() trả bản sao nhánh; revision cho memo đường nóng."""
from __future__ import annotations

import os
import time

from cfharness.config import DEFAULTS, HubConfig


def test_get_returns_copies_cache_not_corrupted(tmp_path):
    c = HubConfig(tmp_path / "config.yaml")
    ev = c.get("notify.slack")
    ev["events"].append("x.bogus")
    ev["new"] = 1
    assert "x.bogus" not in c.get("notify.slack")["events"]
    assert "new" not in c.get("notify.slack")
    full = c.data
    full["notify"] = None
    assert c.get("notify.slack") is not None
    assert c.get() == c.data
    assert DEFAULTS["notify"]["slack"]["events"] == c.get("notify.slack.events")


def test_set_reloads_and_bumps_revision(tmp_path):
    c = HubConfig(tmp_path / "config.yaml")
    r0 = c.revision
    c.set("perf.slow_ms", 123)
    assert c.get("perf.slow_ms") == 123
    assert c.revision > r0


def test_external_edit_picked_up(tmp_path):
    p = tmp_path / "config.yaml"
    c = HubConfig(p)
    assert c.get("perf.slow_ms") == DEFAULTS["perf"]["slow_ms"]
    p.write_text("perf:\n  slow_ms: 7\n", encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert c.get("perf.slow_ms") == 7  # get() luôn stat → thấy ngay
    r = c.revision
    p.write_text("perf:\n  slow_ms: 8\n", encoding="utf-8")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    time.sleep(0.12)  # revision chỉ stat tối đa mỗi 100 ms
    assert c.revision > r
    assert c.get("perf.slow_ms") == 8


def test_file_removed_falls_back_to_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("perf:\n  slow_ms: 9\n", encoding="utf-8")
    c = HubConfig(p)
    assert c.get("perf.slow_ms") == 9
    p.unlink()
    assert c.get("perf.slow_ms") == DEFAULTS["perf"]["slow_ms"]


def test_get_is_fast(tmp_path):
    c = HubConfig(tmp_path / "config.yaml")
    n = 300
    t = time.perf_counter()
    for _ in range(n):
        c.get("runtime.claude.version_cache_s")
    per = (time.perf_counter() - t) / n
    assert per < 0.0005, f"config.get vô hướng {per * 1e6:.0f} µs — cache bản gộp đã hỏng?"
