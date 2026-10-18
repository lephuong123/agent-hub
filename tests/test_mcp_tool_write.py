"""Event `tool.write` + audit cho tool MCP không readOnlyHint (FR-MC-03), suy từ tool.start + annotations."""

import asyncio
import json

from agent_hub.events import make_event
from agent_hub.hub import Hub


async def test_tool_write_audit_and_event(hub_home):
    hub = Hub(hub_home)
    try:
        hub.mcp.add({"name": "viec-nhom-2", "transport": "http", "url": "https://viec.example/mcp", "trust": "trusted"})
        cache = {
            "tim_viec": {"name": "tim_viec", "readOnlyHint": True, "destructiveHint": None},
            "tao_viec": {"name": "tao_viec", "readOnlyHint": None, "destructiveHint": None},
        }
        hub.store.conn.execute("UPDATE mcp_servers SET annotations_cache=? WHERE name='viec-nhom-2'", (json.dumps(cache),))
        sub = hub.bus.subscribe(None, level="summary")

        def start(tid, name, inp):
            hub.publish(make_event("tool.start", "a1", {"tool_id": tid, "name": name, "input": inp, "args_preview": str(inp)[:40]}, run_id="r_1"))

        start("t1", "mcp__viec-nhom-2__tim_viec", {"q": "x"})  # readOnly → không audit
        start("t2", "mcp__viec-nhom-2__tao_viec", {"title": "Việc mới", "api_token": "abc"})  # ghi → audit + event
        start("t3", "mcp__la__tool_la", {})  # server lạ/chưa probe → coi là ghi
        start("t4", "Bash", {"command": "ls"})  # không phải MCP → bỏ qua
        hub.publish(make_event("tool.complete", "a1", {"tool_id": "t2", "name": "mcp__viec-nhom-2__tao_viec", "is_error": False, "result_preview": "đã tạo #12"}, run_id="r_1"))
        await asyncio.sleep(0.05)  # event tool.write phát qua call_soon
        evs = sub.drain()
        types = [e.type for e in evs]
        tw = [e for e in evs if e.type == "tool.write"]
        assert [e.payload["tool"] for e in tw] == ["tao_viec", "tool_la"]
        assert tw[0].run_id == "r_1" and tw[0].agent_id == "a1" and tw[0].payload["server"] == "viec-nhom-2"
        assert tw[0].payload["annotations_known"] is True and tw[1].payload["annotations_known"] is False
        # tool.write đến SAU tool.start tương ứng (không chen giữa publish)
        assert types.index("tool.write") > types.index("tool.start")
        rows = hub.audit.query(kind="tool.write*")
        kinds = sorted(r["kind"] for r in rows)
        assert kinds == ["tool.write", "tool.write", "tool.write.result"]
        w = [r for r in rows if r["kind"] == "tool.write" and r["target"] == "viec-nhom-2/tao_viec"][0]
        assert w["run_id"] == "r_1" and w["agent_id"] == "a1" and w["channel"] == "runtime" and w["actor"] == "agent:a1"
        assert w["data"]["input"]["title"] == "Việc mới" and w["data"]["input"]["api_token"] == "***"
        res = [r for r in rows if r["kind"] == "tool.write.result"][0]
        assert res["data"]["result_preview"] == "đã tạo #12" and res["data"]["is_error"] is False
        # event được ghi DB
        assert hub.store.query_events(run_id="r_1", types=["tool.write"])
        # server bị bỏ qua theo cấu hình
        hub.config.set("mcp.audit_ignore_servers", ["hub-guard", "viec-nhom-2"])
        start("t5", "mcp__viec-nhom-2__tao_viec", {})
        await asyncio.sleep(0.01)
        assert len(hub.audit.query(kind="tool.write")) == 2
        sub.close()
    finally:
        hub.store.close()
