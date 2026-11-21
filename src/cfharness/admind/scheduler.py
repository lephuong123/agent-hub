"""Việc định kỳ của admind (chạy trong thread, mỗi việc một vòng riêng — `Server.every`)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .dispatch import State
    from .server import Server

log = logging.getLogger("cfharness.admind.scheduler")


def run_approvals(state: "State") -> None:
    state.approvals.run_due()


def verify_audit(state: "State") -> None:
    from .alerts import raise_alert

    r = state.audit.verify()
    if not r["ok"]:
        raise_alert(state, "system", "audit", "tamper", "crit")


TASKS: list[tuple[float, Any]] = [
    (60.0, run_approvals),
    (86400.0, verify_audit),
    (86400.0, lambda st: __import__("cfharness.admind.ops_claude", fromlist=["check_ages"]).check_ages(st)),
]


def _optional(name: str, attr: str) -> Any:
    import importlib

    try:
        return getattr(importlib.import_module(f"cfharness.admind.{name}"), attr)
    except (ModuleNotFoundError, AttributeError):
        return None


def register(server: "Server") -> None:
    for s, fn in TASKS:
        server.every(s, fn)
    for mod, attr, every in (("collector", "collect", 60.0), ("health", "check_domains", 600.0), ("notify", "flush", 30.0),
                             ("reports", "tick", 60.0), ("ops_teams", "sync", 30.0)):
        fn = _optional(mod, attr)
        if fn is not None:
            server.every(every, fn)
