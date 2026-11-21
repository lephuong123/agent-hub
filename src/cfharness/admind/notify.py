"""Kênh tin của quản trị (MON-03, RPT-06): Telegram, Slack, email (SMTP). Token trong vault admind; nội dung chỉ tên user + mã/enum + số.

`flush(state)` (mỗi 30 s): gửi cảnh báo mới mở (`notified=0`, không tắt tiếng) và hồi phục. `send(state, text)` dùng cho báo cáo ngày.
Test tiêm `state.extra["notify_sink"]` (list) thay cho gọi mạng.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from .alerts import ALERT_VI

if TYPE_CHECKING:
    from .dispatch import State

log = logging.getLogger("cfharness.admind.notify")
LEVEL_ICON = {"crit": "🔴", "warn": "🟠", "info": "🔵"}


def _post(url: str, data: dict[str, Any], headers: dict[str, str] | None = None, form: bool = False) -> bool:  # pragma: no cover — mạng
    body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    h = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json", **(headers or {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h, method="POST"), timeout=15) as r:  # noqa: S310
            return 200 <= r.status < 300
    except OSError:
        log.warning("gửi tin lỗi tới %s", url.split("/")[2], exc_info=True)
        return False


def send(state: "State", text: str, *, subject: str = "cfharness SAC") -> list[str]:
    """Gửi tới mọi kênh đã cấu hình. Trả danh sách kênh đã gửi được."""
    from .ops_system import setting

    sink = state.extra.get("notify_sink")
    if sink is not None:
        sink.append(text)
        return ["sink"]
    done = []
    tg_tok, tg_chat = state.vault.get("notify.telegram"), setting(state.db, "notify.telegram_chat")
    if tg_tok and tg_chat and _post(f"https://api.telegram.org/bot{tg_tok}/sendMessage", {"chat_id": tg_chat, "text": text[:4000]}, form=True):
        done.append("telegram")
    sl_tok, sl_ch = state.vault.get("notify.slack"), setting(state.db, "notify.slack_channel")
    if sl_tok and sl_ch and _post("https://slack.com/api/chat.postMessage", {"channel": sl_ch, "text": text[:3900]},
                                  {"Authorization": f"Bearer {sl_tok}"}):
        done.append("slack")
    to, host = setting(state.db, "notify.email_to"), setting(state.db, "smtp.host")
    if to and host:
        if send_email(state, to.split(","), subject, text):
            done.append("email")
    return done


def send_email(state: "State", to: list[str], subject: str, text: str) -> bool:  # pragma: no cover — mạng
    from .ops_system import setting

    msg = EmailMessage()
    msg["From"] = setting(state.db, "smtp.from") or setting(state.db, "smtp.user")
    msg["To"] = ", ".join(x.strip() for x in to if x.strip())
    msg["Subject"] = subject
    msg.set_content(text)
    try:
        with smtplib.SMTP(setting(state.db, "smtp.host"), int(setting(state.db, "smtp.port")), timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            if setting(state.db, "smtp.user"):
                s.login(setting(state.db, "smtp.user"), state.vault.get("smtp.password") or "")
            s.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        log.warning("gửi email lỗi", exc_info=True)
        return False


def alert_text(state: "State", a: dict[str, Any]) -> str:
    name = a["subject"]
    if a["subject_kind"] == "user":
        u = state.db.one("SELECT os_user FROM hub_users WHERE id=?", (a["subject"],))
        name = u["os_user"] if u else name
    elif a["subject_kind"] == "domain":
        d = state.db.one("SELECT d.sub, z.name FROM domains d JOIN zones z ON z.id=d.zone_id WHERE d.id=?", (a["subject"],))
        name = f"{d['sub']}.{d['name']}" if d else name
    label = ALERT_VI.get(a["code"], a["code"])
    if a["state"] == "resolved":
        return f"✅ SAC — {name}: đã hết «{label}»"
    return f"{LEVEL_ICON.get(a['level'], '•')} SAC — {name}: {label}"


def flush(state: "State") -> int:
    now = state.clock()
    rows = state.db.all("SELECT * FROM alerts WHERE notified=0 AND (muted_until IS NULL OR muted_until<?) ORDER BY opened_at LIMIT 50", (now,))
    n = 0
    for a in rows:
        if a["state"] == "ack":
            state.db.exec("UPDATE alerts SET notified=1 WHERE id=?", (a["id"],))
            continue
        send(state, alert_text(state, a), subject="Cảnh báo SAC")
        state.db.exec("UPDATE alerts SET notified=1 WHERE id=?", (a["id"],))
        n += 1
    return n
