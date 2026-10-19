"""Kênh chat của người với hub — bot Telegram (docs/dev-notes/telegram.md).

- `attach(hub)` (gọi trong `Hub.__init__`): tạo `hub.telegram` (`TelegramBot`) — kênh thông báo trên luồng sự kiện chung
  + vòng long polling (tự chạy ở `hub.started`, dừng ở `hub.stopping`; tắt êm khi chưa có token).
- `rpc.register_rpc(daemon)` (gọi trong `Daemon.__init__`): RPC `telegram.*` + gắn daemon cho bot để lệnh chat đi qua
  `Daemon.call_as` (cổng HUMAN_ONLY + audit kênh `telegram:<chat_id>`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..hub import Hub


def attach(hub: "Hub"):
    from .bot import TelegramBot

    bot = TelegramBot(hub)
    hub.telegram = bot  # type: ignore[attr-defined]
    return bot
