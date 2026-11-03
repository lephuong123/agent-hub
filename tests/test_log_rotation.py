"""NFR-21 — xoay vòng `logs/` của daemon (an toàn trên Windows) + log dispatcher/recovery gắn run_id/agent_id/task_id."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime
from cfharness.store import now_iso

LINE = "x" * 200


def _logger(name: str, handler: logging.Handler) -> logging.Logger:
    lg = logging.getLogger(f"cfharness.test_rot.{name}")
    lg.handlers.clear()
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg


def _block_rename(monkeypatch):
    """Giả lập Windows: file đang mở ở tiến trình khác → đổi tên ném PermissionError (WinError 32)."""

    def deny(*a, **k):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(os, "replace", deny)
    monkeypatch.setattr(os, "rename", deny)


def test_setup_logging_rotates_when_rename_blocked(tmp_path, monkeypatch, capsys):
    from cfharness.logs import setup_logging

    root = logging.getLogger("cfharness")
    old = list(root.handlers)
    try:
        setup_logging(tmp_path, max_bytes=2000, backup_count=2, max_age_days=30)
        h = root.handlers[0]
        _block_rename(monkeypatch)
        lg = logging.getLogger("cfharness.test_rot.setup")
        for i in range(60):
            lg.warning("%d %s", i, LINE)
        h.flush()
        log = tmp_path / "hub.log"
        assert log.stat().st_size <= 2000 + 400  # đã xoay bằng copytruncate, không phình mãi
        assert (tmp_path / "hub.log.1").exists() and (tmp_path / "hub.log.2").exists()
        assert not (tmp_path / "hub.log.3").exists()
        assert "PermissionError" not in capsys.readouterr().err  # không rơi vào handleError
        last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        assert last["msg"].startswith("59 ")
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in old:
            root.addHandler(h)


def test_rotation_with_reader_holding_file_open(tmp_path):
    """Windows thật: `daemon logs`/trình xem log giữ hub.log mở → os.rename lỗi; handler vẫn xoay (copytruncate)."""
    from cfharness.logs import SafeRotatingFileHandler

    h = SafeRotatingFileHandler(tmp_path / "hub.log", maxBytes=1500, backupCount=3)
    lg = _logger("reader", h)
    try:
        lg.info("first %s", LINE)
        with open(tmp_path / "hub.log", encoding="utf-8") as reader:
            for i in range(40):
                lg.info("%d %s", i, LINE)
            reader.read()  # người đọc không bị hỏng (Windows: thấy file đã bị cắt)
        assert (tmp_path / "hub.log").stat().st_size <= 1500 + 400
        backups = [tmp_path / f"hub.log.{i}" for i in (1, 2, 3)]
        assert all(b.exists() for b in backups)
        assert "39 " in (tmp_path / "hub.log").read_text(encoding="utf-8")  # không mất dòng mới nhất
    finally:
        h.close()


def test_rotation_gives_up_quietly_and_backs_off(tmp_path, monkeypatch, capsys):
    from cfharness import logs as logs_mod
    from cfharness.logs import SafeRotatingFileHandler

    h = SafeRotatingFileHandler(tmp_path / "hub.log", maxBytes=500, backupCount=2)
    lg = _logger("giveup", h)
    calls = {"n": 0}

    def fail_copy(*a, **k):
        calls["n"] += 1
        raise PermissionError(32, "locked")

    try:
        _block_rename(monkeypatch)
        monkeypatch.setattr(logs_mod.shutil, "copyfile", fail_copy)
        for i in range(30):
            lg.info("%d %s", i, LINE)
        assert calls["n"] == 1  # thất bại hẳn → nghỉ, không thử lại ở mỗi dòng
        assert h._retry_at > 0
        assert "Traceback" not in capsys.readouterr().err
        assert (tmp_path / "hub.log").read_text(encoding="utf-8").count("\n") == 30  # không mất dòng
    finally:
        h.close()


def test_purge_old_logs_by_age(tmp_path):
    from cfharness.logs import SafeRotatingFileHandler, purge_old_logs

    old = time.time() - 40 * 86400
    for name in ("hub.log.4", "daemon.out.2", "hook-timeouts.jsonl", "hub.log"):
        (tmp_path / name).write_text("x", encoding="utf-8")
        os.utime(tmp_path / name, (old, old))
    (tmp_path / "hub.log.1").write_text("new", encoding="utf-8")
    h = SafeRotatingFileHandler(tmp_path / "hub.log", maxBytes=1000, backupCount=5, max_age_days=30)  # dọn lúc khởi động
    h.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["hook-timeouts.jsonl", "hub.log", "hub.log.1"]
    (tmp_path / "hub.log.2").write_text("x", encoding="utf-8")
    os.utime(tmp_path / "hub.log.2", (old, old))
    assert purge_old_logs(tmp_path, 0) == []  # 0 = không dọn theo tuổi
    assert purge_old_logs(tmp_path, 30) == ["hub.log.2"]


def test_daemon_out_rotated_on_start(hub_home):
    from cfharness.daemon import _log_settings, _rotate_daemon_out

    hub_home.ensure()
    hub_home.config.write_text("logs:\n  max_bytes: 100\n  backup_count: 2\n", encoding="utf-8")
    ls = _log_settings(hub_home)
    assert ls["max_bytes"] == 100 and ls["backup_count"] == 2 and ls["max_age_days"] == 30
    out = hub_home.logs_dir / "daemon.out"
    for i in range(4):
        out.write_text(f"run {i} " + LINE, encoding="utf-8")
        _rotate_daemon_out(hub_home, ls)
    assert not out.exists()
    assert (hub_home.logs_dir / "daemon.out.1").read_text(encoding="utf-8").startswith("run 3")
    assert (hub_home.logs_dir / "daemon.out.2").read_text(encoding="utf-8").startswith("run 2")
    assert not (hub_home.logs_dir / "daemon.out.3").exists()
    out.write_text("nhỏ", encoding="utf-8")
    _rotate_daemon_out(hub_home, ls)  # dưới max_bytes → giữ nguyên
    assert out.read_text(encoding="utf-8") == "nhỏ"


# ---------------------------------------------------------------- (b) run_logger cho dispatcher/recovery


def _recs(caplog, logger: str, run_id: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == logger and getattr(r, "run_id", None) == run_id]


async def test_recovery_logs_carry_run_context(hub_home, caplog):
    h1 = Hub(hub_home, runtime=FakeRuntime())
    h1.agents.create({"id": "a", "name": "A"})
    t = h1.tasks.create({"title": "việc", "assignee": "a"})
    rid = "r_" + uuid.uuid4().hex[:12]
    h1.store.insert_run({"id": rid, "agent_id": "a", "kind": "adhoc", "prompt": "do it", "session_id": str(uuid.uuid4()),
                         "status": "running", "queued_at": now_iso(), "started_at": now_iso(), "task_id": t["id"]})
    h1.store.close()
    caplog.set_level(logging.INFO, logger="cfharness.recovery")
    h2 = Hub(hub_home, runtime=FakeRuntime())
    h2.start()
    try:
        recs = _recs(caplog, "cfharness.recovery", rid)
        assert recs, [r.getMessage() for r in caplog.records]
        assert all(r.agent_id == "a" and r.task_id == t["id"] for r in recs)
        assert any("interrupted" in r.getMessage() for r in recs)
        assert any("tiếp tục ở run" in r.getMessage() for r in recs)
    finally:
        await h2.stop()


async def test_dispatcher_terminal_error_log_carries_run_context(hub_home, caplog, monkeypatch):
    hub = Hub(hub_home, runtime=FakeRuntime())
    hub.agents.create({"id": "a", "name": "A"})
    t = hub.tasks.create({"title": "việc", "assignee": "a"})
    rid = "r_" + uuid.uuid4().hex[:12]
    hub.store.insert_run({"id": rid, "agent_id": "a", "kind": "adhoc", "prompt": "do it", "session_id": str(uuid.uuid4()),
                          "status": "completed", "queued_at": now_iso(), "task_id": t["id"]})

    def boom(run_id):
        raise RuntimeError("hỏng")

    monkeypatch.setattr(hub.dispatcher, "on_run_terminal", boom)
    monkeypatch.setattr(hub.dispatcher, "kick", lambda: None)
    caplog.set_level(logging.INFO, logger="cfharness.dispatch")
    try:
        hub.dispatcher._safe_terminal(rid)
        recs = [r for r in caplog.records if r.name.startswith("cfharness.dispatch") and getattr(r, "run_id", None) == rid]
        assert recs and recs[0].agent_id == "a" and recs[0].task_id == t["id"] and recs[0].exc_info
    finally:
        hub.store.close()


def test_dispatcher_json_line_has_ids(tmp_path):
    """Dòng JSON ở hub.log có run_id/agent_id/task_id khi đi qua run_logger (không cần contextvar)."""
    from cfharness.logs import JsonFormatter, RunContextFilter, SafeRotatingFileHandler, run_logger

    h = SafeRotatingFileHandler(tmp_path / "hub.log")
    h.setFormatter(JsonFormatter())
    h.addFilter(RunContextFilter())
    lg = _logger("json", h)
    try:
        run_logger(lg, "r_9", "a9", task_id="t_9").warning("run %s: tiến trình chết", "r_9")
        h.flush()
        d = json.loads((tmp_path / "hub.log").read_text(encoding="utf-8").splitlines()[-1])
        assert (d["run_id"], d["agent_id"], d["task_id"]) == ("r_9", "a9", "t_9")
    finally:
        h.close()
