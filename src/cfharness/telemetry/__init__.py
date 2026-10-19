"""Quan sát (M5-B): xuất OpenTelemetry tuỳ chọn (FR-OB-07) — `otel.OtelExporter` (`hub.telemetry`)."""

from .otel import OtelExporter, attach

__all__ = ["OtelExporter", "attach"]
