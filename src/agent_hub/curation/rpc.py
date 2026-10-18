"""RPC của M5-B: `curation.*`, `report.kpi|overhead`, `telemetry.*`.

`register_rpc(daemon)` gọi TRƯỚC `mcp_server.rpc.register_rpc`; method ghi được bọc audit ngay tại đây (`wrap_with_audit`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_CURATION = ("curation.run", "curation.phase2", "curation.rollback", "curation.tick")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) is None:
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


def _since(v: Any) -> str | None:
    if not v:
        return None
    from ..reports import _iso, parse_since

    return _iso(parse_since(str(v)))


def methods(daemon: "Daemon") -> dict[str, Callable[[dict[str, Any]], Any]]:
    hub = daemon.hub
    C = lambda: hub.curation  # noqa: E731

    def kpi(p: dict[str, Any]) -> dict[str, Any]:
        from .kpi import kpi_report

        return kpi_report(hub, since=_since(p.get("since")), weeks=p.get("weeks"), agent_id=p.get("agent"))

    def overhead(p: dict[str, Any]) -> dict[str, Any]:
        from .kpi import overhead_report

        return overhead_report(hub, since=_since(p.get("since")), agent_id=p.get("agent"), limit_pct=float(p.get("limit_pct") or 15))

    return {
        "curation.run": lambda p: C().cross_agent(dry_run=bool(p.get("dry_run")), team_id=p.get("team"), llm=p.get("llm")),
        "curation.detect": lambda p: C().detect(p.get("team")),
        "curation.phase2": lambda p: C().phase2(_req(p, "agent"), dry_run=bool(p.get("dry_run")), focus=p.get("focus"), force=bool(p.get("force"))),
        "curation.list": lambda p: C().list(p.get("kind"), int(p.get("limit") or 30)),
        "curation.show": lambda p: C().get(_req(p, "id")),
        "curation.rollback": lambda p: C().rollback(_req(p, "id")),
        "curation.status": lambda p: C().status(),
        "curation.tick": lambda p: C().tick(),
        "report.kpi": kpi,
        "report.overhead": overhead,
        "telemetry.status": lambda p: hub.telemetry.status(),
        "telemetry.flush": lambda p: {"flushed": hub.telemetry.flush()},
    }


def register_rpc(daemon: "Daemon") -> None:
    from ..mcp_server.rpc import wrap_with_audit

    for name, fn in methods(daemon).items():
        daemon.methods[name] = wrap_with_audit(daemon.hub, name, fn) if name in MUTATING_CURATION else fn
