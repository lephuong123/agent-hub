"""Hệ thống ngoài hub (app trong `~/Workspace` + job launchd): `hub.systems` — service.py, RPC `system.*` (rpc.py), CLI `system` (cli.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .service import ACTIONS, SystemService

if TYPE_CHECKING:
    from ..hub import Hub

__all__ = ["ACTIONS", "SystemService", "attach"]


def attach(hub: "Hub") -> None:
    hub.systems = SystemService(hub)  # type: ignore[attr-defined]
