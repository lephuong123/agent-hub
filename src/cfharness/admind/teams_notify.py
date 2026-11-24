"""Kênh thông báo của team (TEAM-14): nhóm Telegram / kênh Slack gắn với team, gửi bằng bot của quản trị (token trong vault)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .dispatch import State


def send_team(state: "State", channel: dict[str, Any], text: str) -> list[str]:
    from .notify import _post

    sink = state.extra.get("notify_sink")
    if sink is not None:
        sink.append(text)
        return ["sink"]
    done = []
    tok = state.vault.get("notify.telegram")
    if tok and channel.get("telegram_chat"):
        if _post(f"https://api.telegram.org/bot{tok}/sendMessage", {"chat_id": channel["telegram_chat"], "text": text[:4000]}, form=True):
            done.append("telegram")
    tok = state.vault.get("notify.slack")
    if tok and channel.get("slack_channel"):
        if _post("https://slack.com/api/chat.postMessage", {"channel": channel["slack_channel"], "text": text[:3900]},
                 {"Authorization": f"Bearer {tok}"}):
            done.append("slack")
    return done
