"""L5 — cảnh báo theo mẫu audit (docs/dev-notes/plan-logging.md §4 A8, audit_watch.py): mỗi luật đỏ→xanh, chống trùng (một `on` mỗi
(luật, principal)), `resolved` khi qua trọn cửa sổ, không vòng lặp (dòng `audit.alert` bị bỏ qua), nạp tập principal lúc nâng cấp im lặng,
tap lỗi không làm hỏng ghi audit, định tuyến Telegram vào nhóm bảo mật. Không gọi `claude`, không mạng thật."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
import yaml

from cfharness import audit_watch as AW
from cfharness.daemon import Daemon
from cfharness.hub import Hub
from cfharness.runtime.fake import FakeRuntime


@pytest.fixture(autouse=True)
def _watch_on(monkeypatch):
    monkeypatch.delenv("CFHARNESS_AUDIT_WATCH", raising=False)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home)
    clock = Clock()
    h.audit_watch.clock = clock
    seen: list = []
    h.bus.add_listener(lambda ev: seen.append(ev) if ev.type == "audit.alert" else None)
    h.alerts = seen  # type: ignore[attr-defined]
    return h


def rec(h: Hub, kind: str, principal: str | None = "os:boss", **kw):
    return h.audit.record(kind, None, None, kw.pop("data", {}), actor=kw.pop("actor", "user"), channel=kw.pop("channel", "cli"),
                          principal=principal, **kw)


def payloads(h: Hub, rule: str | None = None) -> list[dict]:
    return [e.payload for e in h.alerts if rule is None or e.payload["rule"] == rule]  # type: ignore[attr-defined]


def tick(h: Hub, s: float) -> None:
    h.audit_watch.clock.t += s  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------- luật
def test_denied_burst_red_green_dedup_resolved(hub):
    for _ in range(4):
        rec(hub, "agent.update", "agent:coder", outcome="denied")
        tick(hub, 10)
    assert payloads(hub, "denied_burst") == []  # 4 < 5
    rec(hub, "agent.update", "agent:coder", outcome="denied")
    [on] = payloads(hub, "denied_burst")
    assert on == {"rule": "denied_burst", "principal": "agent:coder", "count": 5, "window_s": 600, "state": "on"}
    for _ in range(3):  # còn bật → không báo lại
        tick(hub, 30)
        rec(hub, "agent.update", "agent:coder", outcome="denied")
    rec(hub, "agent.update", "os:boss", outcome="ok")
    assert len(payloads(hub, "denied_burst")) == 1
    # principal khác có cửa sổ riêng
    for _ in range(5):
        rec(hub, "run.start", "telegram:42", outcome="denied")
    assert [p["principal"] for p in payloads(hub, "denied_burst")] == ["agent:coder", "telegram:42"]
    tick(hub, 599)
    assert hub.audit_watch.sweep() == []
    tick(hub, 2)
    done = hub.audit_watch.sweep()
    assert {d["principal"] for d in done} == {"agent:coder", "telegram:42"}
    res = [p for p in payloads(hub, "denied_burst") if p["state"] == "resolved"]
    assert {(p["principal"], p["count"]) for p in res} == {("agent:coder", 8), ("telegram:42", 5)}
    # cửa sổ trượt: 5 lượt rải trong > 10 phút → không báo
    for _ in range(5):
        rec(hub, "run.start", "slack:U1", outcome="denied")
        tick(hub, 200)
    assert not [p for p in payloads(hub, "denied_burst") if p["principal"] == "slack:U1"]
    # dòng audit cùng tên cho mỗi cảnh báo, kiểu dữ liệu chỉ metadata
    rows = hub.audit.query(kind="audit.alert", limit=50)
    assert len(rows) == len(payloads(hub)) and rows[0]["principal"] == "hub:audit_watch"


def test_login_fail_burst_by_ip_and_global(hub):
    for _ in range(5):
        rec(hub, "web.login_failed", "web:anon", data={"ip": "203.0.113.9", "reason": "bad_code"}, target="203.0.113.9")
    got = payloads(hub, "login_fail_burst")
    assert [p["principal"] for p in got] == ["ip:203.0.113.9"]  # một IP → không cảnh báo toàn cục (đã có cảnh báo theo IP)
    assert payloads(hub, "denied_burst") == []  # web.login_* không vào denied_burst
    for i in range(4):
        rec(hub, "web.login_failed", "web:anon", data={"ip": f"198.51.100.{i}"}, target=f"198.51.100.{i}")
    assert [p["principal"] for p in payloads(hub, "login_fail_burst")] == ["ip:203.0.113.9", "*"]
    rec(hub, "web.login_failed", "web:anon", data={"ip": "<script>"}, target="<script>")  # IP rác → "?"
    assert all("<" not in p["principal"] for p in payloads(hub))


def test_security_change_rules(hub):
    rec(hub, "config.set", target="hub.max_concurrent_runs", outcome="ok")
    rec(hub, "config.set", target="gates.mode", outcome="error")  # thất bại → không phải thay đổi
    assert payloads(hub, "security_change") == []
    rec(hub, "config.set", target="gates.mode", outcome="ok")
    [p] = payloads(hub, "security_change")
    assert p["subject"] == "gates.mode" and p["count"] == 1 and p["principal"] == "os:boss"
    rec(hub, "config.set", target="approval.profile", outcome="ok")  # cùng principal, đang bật → không báo lại
    assert len(payloads(hub, "security_change")) == 1
    for who, kind, data, target, subj in (
            ("web:dev_aaaa1111", "config.set", {}, "guard.sandbox.enabled", "guard.sandbox"),
            ("web:dev_bbbb2222", "config.set", {}, "guard", "guard.enabled"),  # đặt cả nhánh cha
            ("telegram:1", "config.set", {}, "workspace.confine.root", "workspace.confine"),
            ("telegram:2", "config.set", {}, "audit_watch.denied_burst.threshold", "audit_watch"),
            ("telegram:3", "config.set", {}, "audit.archive_after_days", "audit"),
            ("slack:U1", "guard.trust", {"agent_id": "a", "after": "full"}, None, "guard.trust"),
            ("slack:U2", "guard.rule_grant", {"agent_id": "a", "action": "grant", "rule": "git_push"}, None, "guard.rule_grant"),
            ("slack:U3", "guard.sandbox_override", {"agent_id": "a"}, None, "guard.sandbox_override")):
        rec(hub, kind, who, data=data, target=target)
        assert payloads(hub, "security_change")[-1]["principal"] == who and payloads(hub, "security_change")[-1]["subject"] == subj, who
    n = len(payloads(hub, "security_change"))
    rec(hub, "guard.trust", "slack:U9", data={"after": "off"})
    rec(hub, "guard.rule_grant", "slack:U9", data={"action": "remove"})
    assert len(payloads(hub, "security_change")) == n  # thu hẹp quyền không cảnh báo


async def test_security_change_through_real_config_set(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime())
    seen: list = []
    d.hub.bus.add_listener(lambda ev: seen.append(ev.payload) if ev.type == "audit.alert" else None)
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "config.set", "params": {"key": "gates.mode", "value": "human"}})
    assert "result" in resp, resp
    assert any(p["rule"] == "security_change" and p["subject"] == "gates.mode" for p in seen), seen


def test_secret_access_only_outside_hours(hub):
    hub.audit_watch.now_local = lambda: datetime(2026, 11, 18, 23, 30).astimezone()
    rec(hub, "secret.set", target="X")
    assert payloads(hub, "secret_access") == []  # hours = null → tắt
    hub.config.set("audit_watch.secret_access.hours", "08:00-19:00")
    hub.audit_watch.invalidate()
    rec(hub, "read:secret.list", data={"count": 1})
    [p] = payloads(hub, "secret_access")
    assert p["principal"] == "os:boss"
    hub.audit_watch.now_local = lambda: datetime(2026, 11, 18, 10, 0).astimezone()
    rec(hub, "secret.set", "web:dev_x1234567", target="Y")
    assert len(payloads(hub, "secret_access")) == 1  # trong giờ → không báo
    assert AW.in_hours(AW.parse_hours("22:00-06:00"), datetime(2026, 1, 1, 2, 0))
    with pytest.raises(ValueError):
        hub.config.set("audit_watch.secret_access.hours", "8h-19h")


def test_mass_read_weighted_by_count(hub):
    for i in range(29):
        rec(hub, "read:run.get", "web:dev_abcd1234", data={"count": 1}, target=f"r{i}")
    assert payloads(hub, "mass_read") == []
    rec(hub, "read:session.search", "web:dev_abcd1234", data={"count": 1, "coalesced": True})
    [p] = payloads(hub, "mass_read")
    assert p["count"] == 30
    rec(hub, "read:audit.list", "os:other", data={"count": 40, "coalesced": True})  # dòng gộp mang count lớn
    assert [x["principal"] for x in payloads(hub, "mass_read")] == ["web:dev_abcd1234", "os:other"]


def test_tamper_forwarded(hub):
    rec(hub, "audit.tamper_detected", None, data={"checked": 3, "break_id": 2, "reason": "hash_mismatch"})
    rec(hub, "guard.tamper_detected", None, data={"target": "policy"})
    got = payloads(hub, "tamper")
    assert len(got) == 1 and got[0]["principal"] == "hub" and got[0]["subject"] == "audit.tamper_detected"
    tick(hub, 3601)
    hub.audit_watch.sweep()
    assert payloads(hub, "tamper")[-1]["state"] == "resolved" and payloads(hub, "tamper")[-1]["count"] == 2


# ---------------------------------------------------------------------------- principal mới
def test_new_principal_seeded_quietly_on_upgrade(hub_home, monkeypatch):
    monkeypatch.setenv("CFHARNESS_AUDIT_WATCH", "off")  # "trước nâng cấp": audit đã có principal
    h = Hub(hub_home)
    for p in ("os:boss", "telegram:7", "web:dev_11112222"):
        rec(h, "agent.update", p, outcome="ok")
    monkeypatch.delenv("CFHARNESS_AUDIT_WATCH")
    h2 = Hub(hub_home)
    seen: list = []
    h2.bus.add_listener(lambda ev: seen.append(ev.payload) if ev.type == "audit.alert" else None)
    h2.audit_watch.start()  # nạp tập (không có loop → chỉ nạp)
    for p in ("os:boss", "telegram:7", "web:dev_11112222", "hub:schedule", "agent:x", "manager:m", "web:sess_abcd1234", "web:anon", "anon:socket"):
        rec(h2, "agent.update", p, outcome="ok")
    assert seen == []
    rec(h2, "agent.update", "telegram:99", outcome="ok")
    rec(h2, "agent.update", "telegram:99", outcome="ok")
    assert [(p["rule"], p["principal"]) for p in seen] == [("new_principal", "telegram:99")]
    st = h2.store.state_get(AW.SEEN_KEY)
    assert "telegram:99" in st["list"] and "hub:schedule" not in st["list"]


def test_new_principal_fresh_hub_learns_owner_then_alerts(hub):
    rec(hub, "agent.update", "os:boss")
    assert payloads(hub, "new_principal") == []  # cài mới: chủ máy qua CLI học im lặng
    rec(hub, "agent.update", "os:intruder")
    rec(hub, "agent.update", "mcp:http:read")
    assert [p["principal"] for p in payloads(hub, "new_principal")] == ["os:intruder", "mcp:http:read"]


def test_new_principal_seen_set_is_capped(hub):
    hub.config.set("audit_watch.new_principal.max_seen", 3)
    hub.audit_watch.invalidate()
    for i in range(6):
        rec(hub, "agent.update", f"telegram:{i}")
    assert len(hub.store.state_get(AW.SEEN_KEY)["list"]) == 3


# ---------------------------------------------------------------------------- an toàn
def test_no_loop_on_alert_rows(hub):
    for _ in range(5):
        rec(hub, "x.y", "agent:a", outcome="denied")
    n = len(payloads(hub))
    rows = hub.audit.query(kind="audit.alert", limit=50)
    assert n >= 1 and len(rows) == n  # dòng audit.alert (principal hub:audit_watch) không sinh cảnh báo mới
    # kể cả dòng audit.alert giả có outcome=denied
    for _ in range(10):
        rec(hub, "audit.alert", "agent:a", outcome="denied")
    assert len(payloads(hub)) == n


def test_tap_failure_never_breaks_audit_write(hub, monkeypatch):
    def boom(row):
        raise RuntimeError("tap hỏng")

    hub.audit.row_taps.insert(0, boom)
    monkeypatch.setattr(hub.audit_watch, "_matches", lambda row, kind: (_ for _ in ()).throw(RuntimeError("luật hỏng")))
    rid = rec(hub, "agent.update", outcome="denied")
    assert rid and hub.audit.query(kind="agent.update", limit=1)[0]["id"] == rid


def test_env_off_disables(hub, monkeypatch):
    monkeypatch.setenv("CFHARNESS_AUDIT_WATCH", "off")
    for _ in range(10):
        rec(hub, "x", "agent:a", outcome="denied")
    assert payloads(hub) == []


def test_active_alert_survives_restart_and_resolves(hub_home):
    h = Hub(hub_home)
    c = Clock()
    h.audit_watch.clock = c
    for _ in range(5):
        rec(h, "x", "agent:a", outcome="denied")
    assert h.store.state_get(AW.ACTIVE_KEY)
    h2 = Hub(hub_home)
    seen: list = []
    h2.bus.add_listener(lambda ev: seen.append(ev.payload) if ev.type == "audit.alert" else None)
    h2.audit_watch.clock = lambda: c.t + 601
    h2.audit_watch.sweep()
    assert [(p["rule"], p["state"], p["count"]) for p in seen] == [("denied_burst", "resolved", 5)]
    assert h2.store.state_get(AW.ACTIVE_KEY) == {}


def test_config_validation_and_security_key(hub_home):
    from cfharness.guard.integrity import SECURITY_KEYS

    assert "audit_watch" in SECURITY_KEYS
    h = Hub(hub_home)
    for key, val in (("audit_watch.denied_burst.threshold", 0), ("audit_watch.nope", {}), ("audit_watch.enabled", "yes"),
                     ("audit_watch.new_principal.ignore_prefixes", "hub:")):
        with pytest.raises(ValueError):
            h.config.set(key, val)
    assert h.config.set("audit_watch.mass_read.threshold", 50) == 50


# ---------------------------------------------------------------------------- thông báo
def test_alert_event_registered_and_formatted():
    from cfharness.config import DEFAULTS
    from cfharness.events import EVENT_LEVELS, make_event
    from cfharness.notify.slack import format_message
    from cfharness.notify.telegram import format_event

    assert EVENT_LEVELS["audit.alert"] == "summary"
    for ch in ("slack", "telegram"):
        assert "audit.alert" in DEFAULTS["notify"][ch]["events"]
    on = make_event("audit.alert", None, {"rule": "denied_burst", "principal": "agent:coder", "count": 5, "window_s": 600, "state": "on"})
    t = format_message(on)
    assert "từ chối" in t and "agent:coder" in t and "10 phút" in t and "audit list" in t
    assert format_event(on)
    off = make_event("audit.alert", None, {"rule": "security_change", "principal": "os:x", "count": 2, "window_s": 600, "state": "resolved",
                                           "subject": "gates.mode"})
    assert "Hết cảnh báo" in format_message(off) and "gates.mode" in format_message(off)
    bad = make_event("audit.alert", None, {"rule": "<b>x</b>", "principal": "a", "count": 1, "window_s": 1, "state": "on"})
    assert format_message(bad) is None
    evil = make_event("audit.alert", None, {"rule": "mass_read", "principal": "<script>`x", "count": 1, "window_s": 600, "state": "on",
                                            "subject": "free text"})
    t2 = format_message(evil)
    assert "<script>" not in t2 and "free text" not in t2


async def test_alert_goes_to_telegram_security_group(hub_home):
    from telegram_fake import TOKEN, FakeTelegram

    tg = FakeTelegram()
    group, boss = -1001234, 111
    try:
        hub_home.config.write_text(yaml.safe_dump({"guard": {"git_probe": False}, "notify": {"telegram": {
            "api_base": tg.url, "poll_timeout_s": 1, "idle_check_s": 0.05, "backoff_base_s": 0.05, "allow_groups": True,
            "allowed_user_ids": [str(boss)], "allowed_chat_ids": [str(boss), str(group)]}}}))
        d = Daemon(hub_home, runtime=FakeRuntime())
        d.hub.secrets.set("TELEGRAM_BOT_TOKEN", TOKEN)
        d.hub.start()
        tg.hubs.append(d.hub)
        try:
            for _ in range(5):
                rec(d.hub, "agent.update", "agent:coder", outcome="denied")
            for _ in range(200):
                msgs = [x["text"] for x in tg.sent(group)]
                if any("Cảnh báo kiểm toán" in m for m in msgs):
                    break
                await asyncio.sleep(0.05)
            assert any("Cảnh báo kiểm toán" in m and "agent:coder" in m for m in msgs), msgs
            assert not any("Cảnh báo kiểm toán" in x["text"] for x in tg.sent(boss))
        finally:
            await asyncio.wait_for(d.hub.stop(), 15)
    finally:
        tg.close()
