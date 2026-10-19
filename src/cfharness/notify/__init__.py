"""Thông báo ra ngoài hub (FR-SG-09). M1: Slack (Incoming Webhook hoặc bot token + channel)."""

from .slack import SlackNotifier, format_message

__all__ = ["SlackNotifier", "format_message"]
