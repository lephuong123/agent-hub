"""Hardening SEC — H-12 (NFR-10, FR-MC-05): secret không lộ ra DB/WAL/raw stdout/log/OTel/runtime.

Tái hiện kịch bản QA (`scratchpad/audit/scripts/secret_e2e.py`, `otel_leak.py`): run có secret trong secret store, agent in
secret ra stdout (tool result + câu trả lời), OTel bật `include_payloads` — rồi grep TOÀN BỘ `HUB_HOME` (trừ `secrets/`).
(Hub không còn tự chạy/probe MCP bên thứ ba từ v0.2.32 — kịch bản mcp.json mang secret + probe in token đã gỡ cùng tính năng.)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
import yaml

from cfharness.hub import Hub
from cfharness.paths import HubPaths
from cfharness.redact import mask_bytes, mask_values, redact, redact_str, register_secret_values
from cfharness.runtime.fake import FakeRuntime, default_fixture

STORE_SECRET = "Zq9xINTERNALmcpTOKENvalue7781"
ENV_SECRET = "EnvFallbackTOKEN55aa77bb99"


def _grep_home(home: Path, *needles: str) -> list[str]:
    hits = []
    for f in home.rglob("*"):
        if not f.is_file() or "secrets" in f.relative_to(home).parts:
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if f.suffix == ".gz":
            import gzip

            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        for n in needles:
            if n.encode() in data:
                hits.append(f"{f.relative_to(home)}:{n[:6]}")
    return hits


def _leaky_fixture(tmp: Path) -> Path:
    """Fixture stream-json thật nhưng agent in secret ra: lệnh Bash, tool result, câu trả lời cuối."""
    txt = default_fixture().read_text(encoding="utf-8")
    txt = txt.replace('"a.txt"', f'"token={STORE_SECRET} env={ENV_SECRET}"').replace("xong", f"done {STORE_SECRET}")
    txt = txt.replace('{\\"command\\": \\"ls', f'{{\\"command\\": \\"echo {STORE_SECRET}')
    p = tmp / "leaky.jsonl"
    p.write_text(txt, encoding="utf-8")
    return p


def test_value_masker_unit():
    register_secret_values("ValueMaskUnitSecret123", None, "short")
    assert mask_values("x ValueMaskUnitSecret123 y") == "x *** y"
    assert mask_bytes(b"ValueMaskUnitSecret123") == b"***"
    assert redact({"a": ["ValueMaskUnitSecret123"]}) == {"a": ["***"]}
    assert mask_values("short") == "short"  # < 8 ký tự không đăng ký
    # mẫu token QA (otel_leak.py) dù không có trong store
    assert "SUPERSECRET" not in redact_str("echo sk-live-SUPERSECRET-123456")


def test_e2e_run_with_secret_never_leaks(tmp_path, monkeypatch):
    """QA secret_e2e + H-12: run thật (FakeRuntime phát lại stream-json có secret) → không file nào trong HUB_HOME chứa secret."""
    monkeypatch.setenv("CFHARNESS_HOME", str(tmp_path / "hub"))
    p = HubPaths.resolve(tmp_path / "hub")
    p.ensure()
    p.config.write_text(yaml.safe_dump({"guard": {"git_probe": False, "integrity": {"tick_s": 0.2}},
                                        "telemetry": {"otel": {"enabled": True, "exporter": "file", "include_payloads": True}}}))
    fixture = _leaky_fixture(tmp_path)

    async def main() -> None:
        rt = FakeRuntime(fixture, hold=True)
        hub = Hub(p, runtime=rt)
        hub.start()
        hub.secrets.set("INTERNAL_MCP_TOKEN", STORE_SECRET)
        hub.secrets.set("OTHER_TOKEN", ENV_SECRET)
        hub.agents.create({"id": "a1", "name": "A1"})
        r = hub.runs.submit("a1", f"hello, use {ENV_SECRET} carefully")
        rid = r["id"]
        for _ in range(500):  # chờ gate (spawn chậm trên Windows) rồi mới thả
            if rid in rt.gates:
                break
            await asyncio.sleep(0.02)
        rt.release(rid)
        await hub.runs.wait(rid, 10)
        assert not list(p.home.rglob(f"runtime/{rid}"))  # thư mục runtime của run được dọn
        hub.telemetry.flush(2.0)
        logging.getLogger("cfharness.test").warning("log có secret %s và %s", STORE_SECRET, ENV_SECRET)
        await hub.stop()

    from cfharness.redact import install_log_filter

    from cfharness.logs import setup_logging

    setup_logging(p.logs_dir)
    install_log_filter(logging.getLogger("cfharness"))
    asyncio.run(main())
    hits = _grep_home(p.home, STORE_SECRET, ENV_SECRET)
    assert not hits, f"secret lộ ở: {hits}"
    # có thật sự ghi raw log + event (không phải "không lộ vì không ghi gì")
    raws = list(p.runs_dir.glob("*.jsonl.gz"))
    assert raws
    import gzip

    assert b"***" in gzip.decompress(raws[0].read_bytes())


def test_startup_removes_leftover_runtime_dirs(tmp_path):
    """H-12: daemon chết giữa run → `agents/*/runtime/<run>/mcp.json` (secret đã resolve) còn sót → khởi động lại phải xoá."""
    p = HubPaths.resolve(tmp_path / "hub")
    p.ensure()
    left = p.agents_dir / "a1" / "runtime" / "r_dead"
    left.mkdir(parents=True)
    (left / "mcp.json").write_text(json.dumps({"mcpServers": {"x": {"env": {"T": STORE_SECRET}}}}))

    async def main() -> None:
        hub = Hub(p, runtime=FakeRuntime())
        hub.start()
        await asyncio.sleep(0)
        await hub.stop()

    asyncio.run(main())
    assert not left.exists()
    assert not _grep_home(p.home, STORE_SECRET)


def test_otel_include_payloads_masks_secret(tmp_path):
    """QA otel_leak.py: `include_payloads=true` → span không chứa secret (store) hay token dạng sk-live-…"""
    p = HubPaths.resolve(tmp_path / "hub")
    p.ensure()
    S = "sk-live-SUPERSECRET-123456"
    p.config.write_text(yaml.safe_dump({"guard": {"git_probe": False}, "telemetry": {"otel": {"enabled": True, "exporter": "memory", "include_payloads": True}}}))
    from cfharness.events import make_event

    h = Hub(p, runtime=FakeRuntime())
    h.secrets.set("OTEL_T", "StoredOtelSecret998877")
    h.agents.create({"id": "a", "name": "A"})
    for txt in (S, "StoredOtelSecret998877"):
        h.publish(make_event("tool.start", "a", {"tool_id": "t" + txt[:3], "name": "Bash", "input": {"command": f"echo {txt}"}, "args_preview": f"echo {txt}"}, run_id="r_1"))
        h.publish(make_event("tool.complete", "a", {"tool_id": "t" + txt[:3], "name": "Bash", "duration_s": 0.1}, run_id="r_1"))
        h.publish(make_event("guard.blocked", "a", {"reason": f"x {txt}", "rule": "r"}))
    h.telemetry.close()
    d = json.dumps(h.telemetry.sent_batches)
    assert d.count('"tool.input"') >= 1  # payload có xuất (đã che)
    assert S not in d and "SUPERSECRET" not in d and "StoredOtelSecret998877" not in d
    h.store.close()
