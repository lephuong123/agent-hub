"""Lịch chạy tự động (`hub.schedules`): biểu thức (cron.py), service + vòng nền (service.py), RPC `schedule.*` (rpc.py), CLI `schedule` (cli.py).

Xem docs/dev-notes/schedules.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .service import ScheduleService

if TYPE_CHECKING:
    from ..hub import Hub

__all__ = ["ScheduleService", "attach"]


def attach(hub: "Hub") -> None:
    hub.schedules = ScheduleService(hub)  # type: ignore[attr-defined]
