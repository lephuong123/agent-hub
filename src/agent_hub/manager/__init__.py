"""M7 MGR-CORE — điều phối viên (agent harness quản lý + giao việc cho các agent khác). Xem `service.py`, `permissions.py`."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..hub import Hub


def attach(hub: "Hub") -> None:
    from .service import ManagerService

    hub.manager = ManagerService(hub)  # type: ignore[attr-defined]
