"""L1 — nền tảng log (docs/dev-notes/plan-logging.md): JSONL mọi tiến trình, không ghi lặp, che secret, level đổi lúc chạy,
lỗi không ai bắt được log, listener bus lỗi không phá publish. Không gọi `claude`."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
import threading
import time

import pytest

from cfharness import auth, logs
from cfharness.logs import LevelController, refresh_levels, setup_logging, setup_process_logging


@pytest.fixture(autouse=True)
def _restore_logging(monkeypatch):
    """setup_logging đổi trạng thái logging toàn cục → trả lại nguyên trạng sau mỗi test."""
    root, lg = logging.getLogger(), logging.getLogger("cfharness")
    saved = (list(root.handlers), root.level, list(lg.handlers), lg.level, getattr(lg, logs._FILE_MARK, None), logs._PROC)
    yield
    for h in list(root.handlers):
        if h not in saved[0]:
            root.removeHandler(h)
            h.close()
    for h in list(lg.handlers):
        if h not in saved[2]:
            lg.removeHandler(h)
            h.close()
    for h in saved[2]:
        if h not in lg.handlers:
            lg.addHandler(h)
    root.setLevel(saved[1])
    lg.setLevel(saved[3])
    if saved[4] is None and hasattr(lg, logs._FILE_MARK):
        delattr(lg, logs._FILE_MARK)
    logs._PROC = saved[5]
    for name in ("cfharness.guard", "asyncio", "cfharness.l1"):
        logging.getLogger(name).setLevel(logging.NOTSET)


def _lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_json_fields_library_records_and_no_duplicate_stderr(tmp_path, monkeypatch):
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    from cfharness.redact import install_stderr_masking

    for h in [h for h in logging.getLogger().handlers if getattr(h, "_cfharness_masked", False)]:
        logging.getLogger().removeHandler(h)
    setup_logging(tmp_path, proc="daemon")
    install_stderr_masking()
    logging.getLogger("cfharness.l1").warning("cảnh báo %s", "x")
    logging.getLogger("asyncio").warning("thư viện cảnh báo")
    logging.getLogger("asyncio").info("thư viện info — không ghi (root WARNING)")
    rows = _lines(tmp_path / "hub.log")
    msgs = [r["msg"] for r in rows]
    assert msgs == ["cảnh báo x", "thư viện cảnh báo"]  # mỗi bản ghi đúng một lần
    r = rows[0]
    assert r["proc"] == "daemon" and isinstance(r["pid"], int) and r["src"].startswith("test_logging_l1:") and r["ts"].endswith("Z")
    assert err.getvalue() == ""  # daemon.out (stderr) không ghi lặp khi đã có hub.log
    for h in [h for h in logging.getLogger().handlers if getattr(h, "_cfharness_masked", False)]:
        logging.getLogger().removeHandler(h)


def test_process_logging_masks_secret_and_tolerates_unwritable(tmp_path):
    from cfharness.redact import register_secret_values

    register_secret_values("L1SecretValue-998877")
    assert setup_process_logging(tmp_path, "web", "web.jsonl", {"level": "info"})
    logging.getLogger("cfharness.l1").error("token=%s", "L1SecretValue-998877")
    logging.getLogger("asyncio").error("lib %s", "L1SecretValue-998877")
    txt = (tmp_path / "web.jsonl").read_text()
    assert "L1SecretValue-998877" not in txt and txt.count('"proc": "web"') == 2
    ro = tmp_path / "ro"
    ro.write_text("không phải thư mục")  # mkdir/ghi lỗi → bỏ qua, không ném
    assert setup_process_logging(ro / "x", "mcp", "mcp.jsonl", shared=True) is False


def test_shared_mode_appends_and_purges_jsonl_rotations(tmp_path):
    (tmp_path / "mcp.jsonl").write_text("x" * 50)
    setup_logging(tmp_path, proc="mcp", filename="mcp.jsonl", shared=True, max_bytes=10, backup_count=2)
    logging.getLogger("cfharness.l1").warning("sau xoay")
    assert (tmp_path / "mcp.jsonl.1").read_text() == "x" * 50
    assert _lines(tmp_path / "mcp.jsonl")[0]["proc"] == "mcp"
    old = tmp_path / "mcp.jsonl.2"
    old.write_text("cũ")
    import os

    os.utime(old, (time.time() - 40 * 86400,) * 2)
    assert logs.purge_old_logs(tmp_path, 30) == ["mcp.jsonl.2"]


def test_level_controller_effective_and_validation():
    now = 1000.0
    cfg = {"level": "warning", "levels": {"cfharness.guard": "debug", "httpx": "debug", "": "debug", "cfharness.x": "bogus"}}
    temp = {"cfharness.runs": {"level": "debug", "until": 2000}, "cfharness.old": {"level": "debug", "until": 10},
            "urllib3": {"level": "debug", "until": 9999}}
    levels, expired = LevelController.effective(cfg, temp, now)
    assert levels == {"cfharness": logging.WARNING, "cfharness.guard": logging.DEBUG, "cfharness.runs": logging.DEBUG}
    assert set(expired) == {"cfharness.old", "urllib3"}
    assert logs.valid_logger_name("asyncio") and logs.valid_logger_name("cfharness.chat.slack")
    for bad in ("", "httpx", "urllib3.connectionpool", "root", "cfharness..x", "a b"):
        assert not logs.valid_logger_name(bad), bad


class _Store:
    def __init__(self):
        self.d = {}

    def state_get(self, k, default=None):
        return self.d.get(k, default)

    def state_set(self, k, v):
        self.d[k] = v


class _Cfg:
    def __init__(self, data):
        self.data = data

    def get(self, k):
        return self.data.get(k)


class _Hub:
    def __init__(self, logs_cfg):
        self.store, self.config = _Store(), _Cfg({"logs": logs_cfg})


def test_refresh_levels_applies_only_with_file_logging_and_expires_temp(tmp_path):
    hub = _Hub({"level": "info", "levels": {"cfharness.guard": "debug"}})
    ctl = LevelController()
    refresh_levels(hub, ctl, now=0)
    assert logging.getLogger("cfharness.guard").level == logging.NOTSET  # chưa setup_logging (test in-process) → không đụng
    setup_logging(tmp_path)
    hub.store.d[logs.TEMP_KEY] = {"asyncio": {"level": "debug", "until": 100}}
    refresh_levels(hub, ctl, now=50)
    assert logging.getLogger("cfharness.guard").level == logging.DEBUG and logging.getLogger("asyncio").level == logging.DEBUG
    logging.getLogger("asyncio").debug("asyncio debug vào file")
    assert any(r["msg"] == "asyncio debug vào file" for r in _lines(tmp_path / "hub.log"))
    refresh_levels(hub, ctl, now=150)  # hết hạn → gỡ khỏi hub_state, logger về NOTSET
    assert hub.store.d[logs.TEMP_KEY] == {} and logging.getLogger("asyncio").level == logging.NOTSET
    hub.config.data["logs"] = {"level": "info"}
    refresh_levels(hub, ctl, now=150)
    assert logging.getLogger("cfharness.guard").level == logging.NOTSET


def test_bus_listener_error_does_not_break_publish(caplog):
    from cfharness.bus import EventBus
    from cfharness.events import make_event

    bus = EventBus()
    got = []

    def boom(ev):
        raise RuntimeError("listener hỏng")

    bus.add_listener(boom)
    bus.add_listener(got.append)
    with caplog.at_level(logging.ERROR, logger="cfharness.bus"):
        ev = bus.publish(make_event("hub.started", None, {}))
    assert got == [ev] and bus.listener_errors == 1
    assert any("listener" in r.getMessage() and r.exc_info for r in caplog.records)


def test_tracked_task_exception_is_logged(caplog):
    from cfharness.aiotasks import spawn, track

    async def bad():
        raise ValueError("task hỏng")

    async def main():
        keep: set = set()
        t = track(asyncio.get_running_loop().create_task(bad(), name="l1-bad"), keep)
        await asyncio.sleep(0.01)
        assert t.done() and not keep
        c = spawn(asyncio.sleep(10), name="l1-cancel")
        c.cancel()
        await asyncio.sleep(0.01)

    with caplog.at_level(logging.ERROR, logger="cfharness.tasks"):
        asyncio.run(main())
    recs = [r for r in caplog.records if r.name == "cfharness.tasks"]
    assert len(recs) == 1 and "l1-bad" in recs[0].getMessage() and recs[0].exc_info  # huỷ không bị coi là lỗi


def test_asyncio_and_thread_exceptions_logged(caplog, tmp_path, monkeypatch):
    import faulthandler

    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    monkeypatch.setattr(threading, "excepthook", lambda a: None)
    monkeypatch.setattr(logs, "_FAULT_FILE", None)
    was = faulthandler.is_enabled()
    try:
        logs.install_crash_handlers(tmp_path)
        assert (tmp_path / "faults.log").exists()
        with caplog.at_level(logging.ERROR, logger="cfharness.crash"):
            async def main():
                loop = asyncio.get_running_loop()
                loop.set_exception_handler(logs.asyncio_exception_handler)
                loop.call_exception_handler({"message": "Task exception was never retrieved", "exception": KeyError("k")})
            asyncio.run(main())
            th = threading.Thread(target=lambda: 1 / 0, name="l1-thread")
            th.start()
            th.join()
        msgs = [r.getMessage() for r in caplog.records if r.name == "cfharness.crash"]
        assert any("never retrieved" in m for m in msgs) and any("l1-thread" in m for m in msgs)
    finally:
        if hasattr(faulthandler, "unregister"):
            import signal

            if hasattr(signal, "SIGUSR1"):
                faulthandler.unregister(signal.SIGUSR1)
        faulthandler.disable()
        if was:
            faulthandler.enable()
        if logs._FAULT_FILE:
            logs._FAULT_FILE.close()


def test_logs_rpcs_human_only_and_audited():
    from cfharness import logs_rpc

    for m in ("logs.levels", "logs.level"):
        assert m in auth.HUMAN_ONLY and m not in auth.MANAGER_ALLOWED, m
    assert set(logs_rpc.MUTATING_LOGS) <= auth.all_mutating()
    assert logs_rpc.parse_duration("30m") == 1800 and logs_rpc.parse_duration("2h") == 7200 and logs_rpc.parse_duration("90s") == 90
    with pytest.raises(ValueError):
        logs_rpc.parse_duration("abc")


async def test_logs_level_rpc_sets_temp_and_audits(tmp_path):
    from cfharness.daemon import Daemon
    from cfharness.errors import HubError
    from cfharness.paths import HubPaths
    from cfharness.runtime.fake import FakeRuntime

    p = HubPaths.resolve(tmp_path / "hub")
    p.ensure()
    p.config.write_text("guard:\n  git_probe: false\n")
    d = Daemon(p, runtime=FakeRuntime())
    try:
        r = await d.methods["logs.level"]({"logger": "cfharness.guard", "level": "DEBUG", "for_s": 600})
        assert r["temp"]["cfharness.guard"]["level"] == "debug" and r["effective"]["cfharness.guard"] == "debug"
        assert d.hub.store.state_get(logs.TEMP_KEY)["cfharness.guard"]["until"] > time.time() + 500
        rows = d.hub.audit.query(kind="logs.level")
        assert rows and rows[0]["channel"] == "cli"
        for bad in ({"logger": "httpx", "level": "debug"}, {"logger": "cfharness", "level": "loud"},
                    {"logger": "cfharness", "level": "debug", "for_s": 10}, {"logger": "cfharness", "level": "debug", "for_s": 999999}):
            with pytest.raises(HubError):
                await d.methods["logs.level"](dict(bad))
        r = await d.methods["logs.level"]({"logger": "cfharness.guard", "level": "clear"})
        assert "cfharness.guard" not in r["temp"]
        snap = await d.methods["logs.levels"]({})
        assert snap["effective"]["cfharness"] == "info" and "bus_listener" in snap["errors"]
    finally:
        d.hub.store.close()
