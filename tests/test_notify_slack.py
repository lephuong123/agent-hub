"""FR-SG-09 — thông báo Slack, test bằng HTTP server giả cục bộ (không gọi Slack thật)."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cfharness.events import make_event
from cfharness.hub import Hub
from cfharness.notify import format_message
from cfharness.runtime import FakeRuntime


class FakeSlack:
    def __init__(self, fail_first: int = 0, mode: str = "webhook") -> None:
        self.received: list[dict] = []
        self.headers: list[dict] = []
        self.fail_first = fail_first
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if outer.fail_first > 0:
                    outer.fail_first -= 1
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"boom")
                    return
                outer.received.append(body)
                outer.headers.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode() if mode == "bot" else b"ok")

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def make(hub_home):
    hubs, slacks = [], []

    def _m(**slack_cfg):
        h = Hub(hub_home, runtime=FakeRuntime())
        h.config.set("notify.slack", {**h.config.get("notify.slack"), "backoff_base_s": 0.05, **slack_cfg})
        h.start()
        hubs.append(h)
        return h

    yield _m, slacks
    for h in hubs:
        h.store.close()


def approval_event(**kw):
    p = {"request_id": "ap_1", "preview": "rm -rf docs  # token sk-ant-api03-SECRETSECRETSECRET", "reason": "rm -r không phải file run tự tạo", "matched_rule": "delete_not_run_created"}
    p.update(kw)
    return make_event("approval.open", "coder", p, run_id="r_1")


async def test_disabled_quietly_without_url(make):
    m, _ = make
    hub = m()
    assert hub.notifier.target() is None
    hub.publish(approval_event())
    await asyncio.sleep(0.1)
    assert hub.notifier.sent == 0 and hub.notifier.failed == 0
    assert (await hub.notifier.send_now("x"))["sent"] is False


async def test_webhook_via_secret_message_redacted(make):
    m, _ = make
    slack = FakeSlack()
    hub = m()
    hub.secrets.set("SLACK_WEBHOOK_URL", slack.url + "/hook")
    hub.publish(approval_event())
    await hub.notifier.drain(10)
    assert len(slack.received) == 1
    text = slack.received[0]["text"]
    assert "coder" in text and "cfharness approve ap_1" in text and "rm -rf docs" in text
    assert "r_1" not in text and "[cfharness]" not in text  # tin gọn: không id run, không tiền tố
    assert "SECRETSECRET" not in text  # che secret
    types = [e.type for e in hub.store.query_events(types=["notify.sent"])]
    assert types == ["notify.sent"]
    slack.close()


async def test_failure_logs_event_and_retries_with_backoff(make):
    m, _ = make
    slack = FakeSlack(fail_first=2)
    hub = m(webhook_url=slack.url)
    hub.publish(approval_event())
    await hub.notifier.drain(10)
    assert len(slack.received) == 1
    fails = hub.store.query_events(types=["notify.failed"])
    assert len(fails) == 2 and fails[0].payload["retry_in_s"] == pytest.approx(0.05) and fails[1].payload["retry_in_s"] == pytest.approx(0.1)
    slack.close()


async def test_bot_token_and_channel(make):
    m, _ = make
    slack = FakeSlack(mode="bot")
    hub = m(api_base=slack.url + "/api", channel="#ops")
    hub.secrets.set("SLACK_BOT_TOKEN", "xoxb-1234567890-abcdefghij")
    hub.publish(make_event("guard.blocked", "coder", {"detail": "tự duyệt", "preview": "cfharness approve ap_1"}, run_id="r_2"))
    await hub.notifier.drain(10)
    assert slack.received[0]["channel"] == "#ops" and "Đã chặn" in slack.received[0]["text"]
    assert slack.headers[0]["Authorization"] == "Bearer xoxb-1234567890-abcdefghij"
    slack.close()


async def test_reminders_escalate_and_notify(make):
    m, _ = make
    slack = FakeSlack()
    hub = m(webhook_url=slack.url)
    hub.config.set("notify.reminder_interval_s", 0.2)
    hub.agents.create({"id": "coder", "name": "C"})
    run = hub.runs.submit("coder", "x")
    row = hub.approvals.open(hub.store.get_run(run["id"]), tool_name="mcp__crm__delete_contact", tool_input={"id": 42, "email": "a@b.c"}, tool_use_id="t", fingerprint="f", verdict={"verdict": "destructive", "rule": "mcp_delete_name"}, channel="hook")
    await asyncio.sleep(0.25)
    assert hub.approvals.remind_due() == [row["id"]]
    assert hub.approvals.remind_due() == []  # giãn: lần 2 cần 0.4 s
    await asyncio.sleep(0.45)
    assert hub.approvals.remind_due() == [row["id"]]
    await hub.notifier.drain(10)
    texts = [r["text"] for r in slack.received]
    assert any("Nhắc lần 1" in t for t in texts) and any("Nhắc lần 2" in t for t in texts)
    assert all("a@b.c" not in t for t in texts)  # không đưa dữ liệu MCP ra Slack
    hub.approvals.resolve(row["id"], "deny", note="x")
    assert hub.approvals.remind_due() == []
    slack.close()


def test_format_messages():
    assert "tạm dừng" in format_message(make_event("approval.waiting_human", "a", {"request_id": "ap_9", "preview": "x"}, run_id="r"))
    assert "lỗi 3 lần" in format_message(make_event("run.repeated_failure", "a", {"count": 3, "error": "e"}, run_id="r"))
    assert format_message(make_event("tool.start", "a", {})) is None
