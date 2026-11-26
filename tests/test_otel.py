"""M5-B FR-OB-07: OTel exporter tuỳ chọn — tắt mặc định, chỉ metadata, không nội dung MCP/secret."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from cfharness.events import make_event
from cfharness.hub import Hub
from m3_fake import ScriptedRuntime

SECRET = "sk-live-SUPERSECRET-123456"


def _hub(hub_home, otel: dict | None = None):
    cfg = {"guard": {"git_probe": False}}
    if otel is not None:
        cfg["telemetry"] = {"otel": otel}
    hub_home.config.write_text(yaml.safe_dump(cfg))
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
    h.agents.create({"id": "a", "name": "A"})
    return h


def _emit_run(h):
    h.store.insert_run({"id": "r_1", "agent_id": "a", "kind": "adhoc", "prompt": f"use {SECRET}", "model": "haiku", "status": "completed", "outcome": "completed",
                        "queued_at": "2026-10-18T00:00:00.000Z", "started_at": "2026-10-18T00:00:00.000Z", "cost_usd": 0.0123,
                        "usage": {"input_tokens": 10, "output_tokens": 5}})
    ev = [
        make_event("run.started", "a", {"prompt": SECRET}, run_id="r_1"),
        make_event("tool.start", "a", {"tool_id": "t1", "name": "mcp__crm__get_customer", "input": {"token": SECRET, "q": "acme"}, "args_preview": SECRET}, run_id="r_1"),
        make_event("tool.complete", "a", {"tool_id": "t1", "name": "mcp__crm__get_customer", "duration_s": 0.2, "is_error": False, "result_preview": f"customer {SECRET}"}, run_id="r_1"),
        make_event("tool.start", "a", {"tool_id": "t2", "name": "Bash", "input": {"command": f"echo {SECRET}"}, "args_preview": f"echo {SECRET}"}, run_id="r_1"),
        make_event("tool.complete", "a", {"tool_id": "t2", "name": "Bash", "duration_s": 0.1, "is_error": True, "result_preview": SECRET}, run_id="r_1"),
        make_event("message.complete", "a", {"text": SECRET}, run_id="r_1"),
        make_event("run.completed", "a", {"outcome": "completed"}, run_id="r_1"),
        make_event("learning.applied", "a", {"proposal_id": "pr_1", "target": "skill", "op": "patch", "risk_class": "L1", "diff": f"+{SECRET}", "summary": SECRET}),
    ]
    for e in ev:
        h.publish(e)


def _spans(h):
    h.telemetry.close()
    out = []
    for b in h.telemetry.sent_batches:
        for rs in b["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                out += ss["spans"]
    return out


def test_disabled_by_default(hub_home):
    h = _hub(hub_home)
    assert h.config.get("telemetry.otel.enabled") is False
    _emit_run(h)
    assert h.telemetry.q.qsize() == 0 and h.telemetry.stats["spans"] == 0
    h.store.close()


def test_spans_metadata_only(hub_home):
    h = _hub(hub_home, {"enabled": True, "exporter": "memory", "flush_s": 0.05})
    _emit_run(h)
    spans = _spans(h)
    names = sorted(s["name"] for s in spans)
    assert names == ["learning learning.applied", "run adhoc", "tool Bash", "tool mcp__crm__get_customer"]
    run = next(s for s in spans if s["name"] == "run adhoc")
    tools = [s for s in spans if s["name"].startswith("tool ")]
    assert all(t["traceId"] == run["traceId"] and t["parentSpanId"] == run["spanId"] for t in tools)
    attrs = {a["key"]: a["value"] for a in run["attributes"]}
    assert attrs["cfharness.cost_usd"]["doubleValue"] == pytest.approx(0.0123) and attrs["gen_ai.request.model"]["stringValue"] == "haiku"
    mcp = next(s for s in tools if "mcp" in s["name"])
    assert {a["key"]: a["value"] for a in mcp["attributes"]}["tool.mcp_server"]["stringValue"] == "crm"
    assert next(s for s in tools if s["name"] == "tool Bash")["status"]["code"] == 2
    dump = json.dumps(h.telemetry.sent_batches)
    assert SECRET not in dump and "acme" not in dump  # không prompt/input/output/diff
    h.store.close()


def test_include_payloads_never_for_mcp(hub_home):
    h = _hub(hub_home, {"enabled": True, "exporter": "memory", "include_payloads": True})
    _emit_run(h)
    spans = _spans(h)
    bash = {a["key"]: a["value"] for a in next(s for s in spans if s["name"] == "tool Bash")["attributes"]}
    mcp = {a["key"]: a["value"] for a in next(s for s in spans if "mcp__" in s["name"])["attributes"]}
    assert "tool.input" in bash and "tool.input" not in mcp
    h.store.close()


def test_otlp_http_export(hub_home):
    got: list = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.path, {k.lower(): v for k, v in self.headers.items()}, json.loads(body)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        h = _hub(hub_home, {"enabled": True, "exporter": "otlp_http", "endpoint": f"http://127.0.0.1:{srv.server_port}", "headers": {"x-api-key": "k1"}})
        _emit_run(h)
        h.telemetry.close()
        assert got and got[0][0] == "/v1/traces" and got[0][1].get("x-api-key") == "k1"
        assert got[0][2]["resourceSpans"][0]["resource"]["attributes"][0]["value"]["stringValue"] == "cfharness"
        assert h.telemetry.stats["exported"] >= 4 and h.telemetry.stats["errors"] == 0
        h.store.close()
    finally:
        srv.shutdown()


def test_export_failure_does_not_break_hub(hub_home):
    h = _hub(hub_home, {"enabled": True, "exporter": "otlp_http", "endpoint": "http://127.0.0.1:9", "timeout_s": 0.5})
    _emit_run(h)
    h.telemetry.close()
    assert h.telemetry.stats["errors"] >= 1 and h.telemetry.stats["dropped"] >= 1
    assert h.store.get_run("r_1")["status"] == "completed"
    h.store.close()


def test_req_id_trace_and_allowlist(hub_home):
    """L2: run có req_id → trace_id suy từ req_id (span tool cùng trace), thuộc tính `cfharness.req_id` nằm trong allowlist."""
    from cfharness.telemetry.otel import RUN_SPAN_KEYS, _hex_id
    from cfharness.trace import new_req_id

    h = _hub(hub_home, {"enabled": True, "exporter": "memory", "flush_s": 0.05})
    rq = new_req_id()
    h.store.insert_run({"id": "r_2", "agent_id": "a", "kind": "adhoc", "prompt": "x", "status": "completed", "outcome": "completed",
                        "queued_at": "2026-10-18T00:00:00.000Z", "req_id": rq})
    for e in (make_event("run.started", "a", {}, run_id="r_2"),
              make_event("tool.start", "a", {"tool_id": "t1", "name": "Bash"}, run_id="r_2"),
              make_event("tool.complete", "a", {"tool_id": "t1", "name": "Bash", "duration_s": 0.1}, run_id="r_2"),
              make_event("run.completed", "a", {"outcome": "completed"}, run_id="r_2")):
        h.publish(e)
    spans = _spans(h)
    run = next(s for s in spans if s["name"] == "run adhoc")
    tool = next(s for s in spans if s["name"] == "tool Bash")
    assert run["traceId"] == tool["traceId"] == _hex_id("req:" + rq, 16)
    keys = {a["key"] for a in run["attributes"]}
    assert "cfharness.req_id" in RUN_SPAN_KEYS and keys <= RUN_SPAN_KEYS
    assert {a["key"]: a["value"] for a in run["attributes"]}["cfharness.req_id"]["stringValue"] == rq
    h.store.close()


def test_circuit_breaker_skips_dead_endpoint(hub_home, monkeypatch):
    """Collector chết → lỗi một lần rồi ngắt mạch: các lô sau bỏ ngay (không mở kết nối) cho tới hết `circuit_s`."""
    import urllib.request

    from cfharness.telemetry import otel as O

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    h = _hub(hub_home, {"enabled": True, "exporter": "otlp_http", "endpoint": "http://127.0.0.1:9", "batch_size": 1, "circuit_s": 60})
    tel = h.telemetry
    tel.close()  # dừng luồng nền: gửi tay bằng flush() cho đếm chính xác
    for i in range(5):
        tel._put(tel._span("0" * 32, f"{i:016x}", None, "x", 1, 2, {}, True, None))
    while tel.flush() > 0:
        pass
    assert calls["n"] == 1 and tel.stats["errors"] == 1
    assert tel.stats["dropped"] == 5 and tel.stats["circuit_skipped"] == 4
    assert tel.status()["circuit_open_s"] > 0
    # đổi endpoint → thử lại ngay, không chờ hết thời gian ngắt
    h.config.set("telemetry.otel.endpoint", "http://127.0.0.1:10")
    tel._put(tel._span("0" * 32, "f" * 16, None, "x", 1, 2, {}, True, None))
    tel.flush()
    assert calls["n"] == 2
    # hết thời gian ngắt → thử lại; thành công thì đóng mạch
    monkeypatch.setattr(O.time, "monotonic", lambda: 1e12)

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: R())
    tel._put(tel._span("0" * 32, "e" * 16, None, "x", 1, 2, {}, True, None))
    tel.flush()
    assert tel.stats["exported"] == 1 and tel._circuit is None
    h.store.close()
