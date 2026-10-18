"""Xuất OpenTelemetry (FR-OB-07) — OPT-IN, tắt mặc định (`telemetry.otel.enabled=false`).

Không phụ thuộc SDK `opentelemetry`: dựng span theo mô hình dữ liệu OTLP và gửi OTLP/HTTP **JSON** tới
`<endpoint>/v1/traces` (collector chuẩn nhận được), hoặc ghi JSONL (`exporter: file`). Listener bus CHỈ đưa span vào hàng
đợi giới hạn; luồng nền gom lô + gửi (lỗi mạng không ảnh hưởng hub, span bị bỏ khi đầy — đếm `dropped`).

Span (chỉ METADATA — allowlist thuộc tính, không bao giờ prompt/output/kết quả tool/nội dung MCP/secret):
- `run <kind>`: một span mỗi run (trace_id suy từ run_id) — agent, kind, model, outcome, cost, token, số turn, thời lượng.
- `tool <name>`: con của span run — tên tool, thời lượng, is_error, `tool.mcp_server` nếu là `mcp__<server>__…`.
  `include_payloads=true` chỉ thêm input ĐÃ CHE (redact) + cắt 512 ký tự của tool BUILT-IN; tool `mcp__*` không bao giờ.
- `learning <event>`: span tức thời cho `learning.*`, `review.*`, `curation.*`, `guard.blocked`, `eval.completed` — chỉ id,
  loại, trạng thái, target/op, actor, chi phí; không diff/nội dung.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..events import TERMINAL_RUN_EVENTS, HubEvent

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("agent_hub.telemetry")

LEARNING_PREFIXES = ("learning.", "review.", "curation.", "curator.", "eval.completed", "guard.blocked")
LEARNING_KEYS = ("proposal_id", "ledger_entry_id", "review_id", "curation_id", "eval_id", "status", "outcome", "target", "op", "risk_class", "scope",
                 "actor", "created_by", "reason", "rule", "kind", "cost_usd", "trigger", "team_id")
MAX_OPEN_TOOLS = 10_000


def _hex_id(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[: n * 2]


def _ns(ev: HubEvent) -> int:
    return int(ev.ts.timestamp() * 1e9)


def _attr(k: str, v: Any) -> dict[str, Any] | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float):
        return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": str(v)[:256]}}


def _attrs(d: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in (_attr(k, v) for k, v in d.items()) if a is not None]


class OtelExporter:
    """Listener bus → span OTLP. `hub.telemetry`."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.q: queue.Queue = queue.Queue(maxsize=10_000)
        self.open_runs: dict[str, int] = {}
        self.open_tools: dict[tuple[str, str], tuple[int, str]] = {}
        self._payloads: dict[tuple[str, str], Any] = {}
        self.stats = {"spans": 0, "exported": 0, "dropped": 0, "errors": 0, "last_error": None}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.sent_batches: list[dict[str, Any]] = []  # exporter "memory" (test)
        hub.bus.add_listener(self.on_event)

    def cfg(self) -> dict[str, Any]:
        return dict(self.hub.config.get("telemetry.otel") or {})

    @property
    def enabled(self) -> bool:
        return bool(self.cfg().get("enabled"))

    # ------------------------------------------------------------------ listener (không chặn)
    def on_event(self, ev: HubEvent) -> None:
        try:
            if not self.enabled:
                return
            self._ensure_thread()
            t = ev.type
            if t == "run.started" and ev.run_id:
                self.open_runs[ev.run_id] = _ns(ev)
            elif t == "tool.start" and ev.run_id:
                if len(self.open_tools) < MAX_OPEN_TOOLS:
                    self.open_tools[(ev.run_id, str(ev.payload.get("tool_id")))] = (_ns(ev), str(ev.payload.get("name") or "?"))
                    if self.cfg().get("include_payloads"):
                        self._payloads[(ev.run_id, str(ev.payload.get("tool_id")))] = ev.payload.get("args_preview") or ev.payload.get("input")
            elif t == "tool.complete" and ev.run_id:
                self._tool_span(ev)
            elif t in TERMINAL_RUN_EVENTS and ev.source == "hub" and ev.run_id and t != "run.waiting_human":
                self._run_span(ev)
            elif t.startswith(LEARNING_PREFIXES):
                self._learning_span(ev)
        except Exception:  # pragma: no cover - telemetry không được làm hỏng bus
            log.debug("otel listener lỗi", exc_info=True)

    def _put(self, span: dict[str, Any]) -> None:
        self.stats["spans"] += 1
        try:
            self.q.put_nowait(span)
        except queue.Full:
            self.stats["dropped"] += 1

    def _run_span(self, ev: HubEvent) -> None:
        run = self.hub.store.get_run(ev.run_id or "") or {}
        start = self.open_runs.pop(ev.run_id or "", None)
        if start is None:
            st = run.get("started_at") or run.get("queued_at")
            start = int(_ts(st) * 1e9) if st else _ns(ev)
        usage = run.get("usage") or {}
        outcome = run.get("outcome") or ev.payload.get("outcome") or ev.type.split(".")[1]
        a = {"agent_hub.agent_id": ev.agent_id, "agent_hub.run_id": ev.run_id, "agent_hub.run.kind": run.get("kind"), "agent_hub.team_id": run.get("team_id"),
             "agent_hub.task_id": run.get("task_id"), "gen_ai.system": "anthropic", "gen_ai.request.model": run.get("model"), "agent_hub.run.outcome": outcome,
             "agent_hub.cost_usd": float(run["cost_usd"]) if run.get("cost_usd") is not None else None,
             "gen_ai.usage.input_tokens": _int(usage.get("input_tokens")), "gen_ai.usage.output_tokens": _int(usage.get("output_tokens")),
             "agent_hub.usage.cache_read_tokens": _int(usage.get("cache_read_input_tokens")), "agent_hub.num_turns": _int(run.get("num_turns")),
             "agent_hub.resumed": bool(run.get("resumed_from_session")) if run else None}
        ok = outcome in ("completed",)
        self._put(self._span(_hex_id("run:" + str(ev.run_id), 16), _hex_id("span:run:" + str(ev.run_id), 8), None, f"run {run.get('kind') or 'adhoc'}",
                             start, _ns(ev), a, ok, None if ok else str(outcome)))

    def _tool_span(self, ev: HubEvent) -> None:
        tid = str(ev.payload.get("tool_id"))
        start, name = self.open_tools.pop((str(ev.run_id), tid), (None, str(ev.payload.get("name") or "?")))
        end = _ns(ev)
        if start is None:
            start = end - int(float(ev.payload.get("duration_s") or 0) * 1e9)
        a: dict[str, Any] = {"agent_hub.agent_id": ev.agent_id, "agent_hub.run_id": ev.run_id, "tool.name": name, "tool.is_error": bool(ev.payload.get("is_error")),
                             "agent_hub.subagent_id": ev.subagent_id}
        if name.startswith("mcp__"):
            parts = name.split("__")
            a["tool.mcp_server"] = parts[1] if len(parts) > 2 else None
        pl = self._payloads.pop((str(ev.run_id), tid), None)
        if pl is not None and self.cfg().get("include_payloads") and not name.startswith("mcp__"):
            from ..redact import redact

            a["tool.input"] = json.dumps(redact(pl), ensure_ascii=False, default=str)[:512]
        err = bool(ev.payload.get("is_error"))
        self._put(self._span(_hex_id("run:" + str(ev.run_id), 16), _hex_id(f"tool:{ev.run_id}:{tid}", 8), _hex_id("span:run:" + str(ev.run_id), 8),
                             f"tool {name}", start, end, a, not err, "tool error" if err else None))

    def _learning_span(self, ev: HubEvent) -> None:
        p = ev.payload or {}
        a = {"agent_hub.event": ev.type, "agent_hub.agent_id": ev.agent_id, "agent_hub.run_id": ev.run_id, "agent_hub.team_id": ev.team_id}
        for k in LEARNING_KEYS:
            v = p.get(k)
            if isinstance(v, (str, int, float, bool)):
                a["agent_hub.learning." + k] = v
        seed = str(p.get("proposal_id") or p.get("curation_id") or p.get("review_id") or ev.run_id or f"{ev.type}:{ev.seq}")
        t = _ns(ev)
        bad = ev.type in ("guard.blocked", "learning.eval_failed")
        self._put(self._span(_hex_id("learning:" + seed, 16), _hex_id(f"ev:{ev.epoch}:{ev.seq}:{ev.type}", 8), None, f"learning {ev.type}", t, t, a, not bad,
                             ev.type if bad else None))

    @staticmethod
    def _span(trace: str, span: str, parent: str | None, name: str, start: int, end: int, attrs: dict[str, Any], ok: bool, msg: str | None) -> dict[str, Any]:
        s: dict[str, Any] = {"traceId": trace, "spanId": span, "name": name, "kind": 1, "startTimeUnixNano": str(start), "endTimeUnixNano": str(max(start, end)),
                             "attributes": _attrs(attrs), "status": {"code": 1 if ok else 2}}
        if msg:
            s["status"]["message"] = msg[:200]
        if parent:
            s["parentSpanId"] = parent
        return s

    # ------------------------------------------------------------------ xuất (luồng nền)
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="agent-hub-otel", daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        while not self._stop.is_set():
            c = self.cfg()
            self.flush(max_wait=float(c.get("flush_s") or 2.0))

    def flush(self, max_wait: float = 0.0) -> int:
        c = self.cfg()
        size = int(c.get("batch_size") or 64)
        batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + max_wait
        while len(batch) < size:
            remaining = deadline - time.monotonic()
            try:
                batch.append(self.q.get(timeout=remaining) if remaining > 0 else self.q.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._export(batch, c)
        return len(batch)

    def payload(self, spans: list[dict[str, Any]], c: dict[str, Any] | None = None) -> dict[str, Any]:
        from .. import __version__

        c = c or self.cfg()
        return {"resourceSpans": [{
            "resource": {"attributes": _attrs({"service.name": c.get("service_name") or "agent-hub", "service.version": __version__,
                                               "agent_hub.home_hash": _hex_id(str(self.hub.paths.home), 4)})},
            "scopeSpans": [{"scope": {"name": "agent_hub", "version": __version__}, "spans": spans}],
        }]}

    def _export(self, spans: list[dict[str, Any]], c: dict[str, Any]) -> None:
        from ..redact import redact

        body = redact(self.payload(spans, c))  # H-12: che theo GIÁ TRỊ secret + mẫu token trên MỌI chuỗi của span trước khi xuất
        kind = c.get("exporter") or "otlp_http"
        try:
            if kind == "memory":
                self.sent_batches.append(body)
            elif kind == "file":
                p = Path(c.get("file") or (self.hub.paths.home / "logs" / "otel.jsonl"))
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(body, ensure_ascii=False) + "\n")
            else:
                url = str(c.get("endpoint") or "http://127.0.0.1:4318").rstrip("/")
                if not url.endswith("/v1/traces"):
                    url += "/v1/traces"
                headers = {"Content-Type": "application/json"}
                for k, v in (c.get("headers") or {}).items():
                    headers[str(k)] = self._resolve(str(v))
                req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=float(c.get("timeout_s") or 5)) as r:
                    r.read()
            self.stats["exported"] += len(spans)
        except Exception as e:
            self.stats["errors"] += 1
            self.stats["dropped"] += len(spans)
            self.stats["last_error"] = repr(e)[:200]

    def _resolve(self, v: str) -> str:
        if "${" not in v:
            return v
        sec = getattr(self.hub, "secrets", None)
        try:
            return sec.resolve(v) if sec is not None else v
        except Exception:
            return ""

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=float(self.cfg().get("flush_s") or 2.0) + float(self.cfg().get("timeout_s") or 5.0) + 1)
        while self.flush() > 0:
            pass

    def status(self) -> dict[str, Any]:
        c = self.cfg()
        return {"enabled": bool(c.get("enabled")), "exporter": c.get("exporter"), "endpoint": c.get("endpoint") if c.get("exporter", "otlp_http") == "otlp_http" else c.get("file"),
                "include_payloads": bool(c.get("include_payloads")), "queued": self.q.qsize(), **self.stats}


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ts(s: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def attach(hub: "Hub") -> None:
    hub.telemetry = OtelExporter(hub)  # type: ignore[attr-defined]
