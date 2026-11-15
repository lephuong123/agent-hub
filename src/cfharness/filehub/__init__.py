"""Thư mục dùng chung người ↔ agent trên "Không gian số" (`hub.filehub`) — docs/dev-notes/filehub.md.

- `supervisor.py`: tiến trình con `tang-thu-sync-headless` (backoff, đối soát pid, SIGTERM, env tối thiểu)
- `service.py`: trạng thái, link, presence, steer khi người sửa trong lúc agent chạy, báo sau run
- `rpc.py` (`filehub.*`), `cli.py` (`cfharness filehub …`)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .service import REF_PREFIX, FilehubService, local_roots, root_of

if TYPE_CHECKING:
    from ..hub import Hub

__all__ = ["FilehubService", "REF_PREFIX", "attach", "local_roots", "root_of"]


def attach(hub: "Hub") -> None:
    hub.filehub = FilehubService(hub)  # type: ignore[attr-defined]
