"""Lỗi mạng tạm thời của vòng nhận tin chat: DEBUG lúc đầu, WARNING một lần khi kéo dài, INFO khi hồi (chat/netlog.py)."""

from __future__ import annotations

import logging

from cfharness.chat.netlog import TransientLog, is_transient
from cfharness.chat.slack.api import SlackError
from cfharness.notify.telegram import TelegramError

LOG = "cfharness.test.netlog"


def _levels(caplog):
    return [r.levelno for r in caplog.records if r.name == LOG]


def test_is_transient_classification():
    # đúng định dạng TelegramApi/SlackApi bọc lỗi mạng httpx
    assert is_transient(TelegramError("getUpdates: ReadTimeout: "))
    assert is_transient(TelegramError("getUpdates: ConnectError: [Errno 8] nodename nor servname provided"))
    assert is_transient(TelegramError("getUpdates: ConnectTimeout: timed out"))
    assert is_transient(TelegramError("getUpdates: Bad Gateway", code=502))
    assert is_transient(SlackError("apps.connections.open: bị giới hạn tốc độ (429)", code="ratelimited"))
    assert is_transient(ConnectionResetError("reset"))
    assert not is_transient(TelegramError("getUpdates: Unauthorized", code=401))
    assert not is_transient(TelegramError("getUpdates: Conflict: terminated by other getUpdates request", code=409))
    assert not is_transient(SlackError("auth.test: invalid_auth", code="invalid_auth"))
    assert not is_transient(ValueError("bad json"))


def test_transient_errors_debug_then_one_warning_then_info(caplog):
    caplog.set_level(logging.DEBUG, logger=LOG)
    t = [0.0]
    g = TransientLog(logging.getLogger(LOG), "Telegram", warn_after=3, warn_after_s=300, clock=lambda: t[0])
    e = TelegramError("getUpdates: ReadTimeout: ")
    g.error(e, "x")
    g.error(e, "x")
    assert _levels(caplog) == [logging.DEBUG, logging.DEBUG]
    g.error(e, "x")  # lần thứ 3 liên tiếp → WARNING
    g.error(e, "x")
    g.error(e, "x")  # chỉ cảnh báo một lần mỗi đợt mất kết nối
    assert _levels(caplog).count(logging.WARNING) == 1
    g.ok()
    assert _levels(caplog)[-1] == logging.INFO
    caplog.clear()
    g.ok()  # thành công bình thường: không log gì
    g.error(e, "x")  # đợt mới bắt đầu lại từ DEBUG
    assert _levels(caplog) == [logging.DEBUG]


def test_long_outage_by_time_warns(caplog):
    caplog.set_level(logging.DEBUG, logger=LOG)
    t = [0.0]
    g = TransientLog(logging.getLogger(LOG), "Slack", warn_after=100, warn_after_s=300, clock=lambda: t[0])
    e = TelegramError("getUpdates: ConnectError: x")
    g.error(e, "x")
    t[0] = 301
    g.error(e, "x")
    assert _levels(caplog) == [logging.DEBUG, logging.WARNING]


def test_non_transient_always_warns(caplog):
    caplog.set_level(logging.DEBUG, logger=LOG)
    g = TransientLog(logging.getLogger(LOG), "Telegram")
    g.error(TelegramError("getUpdates: Unauthorized", code=401), "x")
    g.error(TelegramError("getUpdates: Unauthorized", code=401), "x")
    assert _levels(caplog) == [logging.WARNING, logging.WARNING]
