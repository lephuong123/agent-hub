"""M5-B — curation & quan sát.

- `service.CurationService` (`hub.curation`): curator liên agent (FR-LN-12) + curator pha 2 LLM (FR-LN-09), chạy nền khi rảnh, có ngân sách.
- `similarity`: so khớp skill tất định (hàm thuần). `prompts`: prompt Hermes nguyên văn (Q-13) + phần hub. `llm`: lời gọi `claude -p --json-schema`.
- `kpi`: báo cáo KPI G-4 + overhead học NFR-19. `overhead`: bench FakeRuntime.
- `rpc`/`cli`: `curation.*`, `report.kpi|overhead`, `telemetry.*`; CLI `agent-hub curation …`, `report kpi|overhead`, `telemetry status`.
"""

from .service import CurationService


def attach(hub) -> None:  # type: ignore[no-untyped-def]
    """Gắn vào hub (CUỐI `Hub.__init__`, sau `hub.learning`): `hub.curation`, `hub.telemetry` (OTel tắt mặc định)."""
    from ..telemetry import attach as _attach_otel

    hub.curation = CurationService(hub)
    _attach_otel(hub)


__all__ = ["CurationService", "attach"]
