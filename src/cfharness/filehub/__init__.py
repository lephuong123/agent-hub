"""Thư mục dùng chung người ↔ agent trên "Không gian số" (`hub.filehub`) — docs/dev-notes/filehub.md.

- `service.py`: thư mục chung (cổng cfharness là chế độ duy nhất), thư mục đồng bộ cũ cần chuyển, dừng tiến trình sync còn sót, link
- `lane/`: cổng cfharness — client, luồng sự kiện, cây làm việc theo run, tool `fs_*`
- `rpc.py` (`filehub.*`), `cli.py` (`cfharness filehub …`)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .service import REF_PREFIX, FilehubService

if TYPE_CHECKING:
    from ..hub import Hub

__all__ = ["FilehubService", "REF_PREFIX", "attach"]


def attach(hub: "Hub") -> None:
    hub.filehub = FilehubService(hub)  # type: ignore[attr-defined]
