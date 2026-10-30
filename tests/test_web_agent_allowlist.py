"""Allowlist domain WebFetch theo agent (`tool_policy.web_allow_domains`) — docs/dev-notes/webfetch-agent-allowlist.md.

In-process (Hub + FakeRuntime giữ run), resolver DNS giả — không ra mạng, không gọi claude. Gồm ca vượt rào (subdomain, userinfo,
IDN/punycode, IP literal, cổng, hoa/thường, dấu chấm cuối, `\\`) và ca "chưa đặt = hành vi y như cũ".
"""

from __future__ import annotations

import asyncio

import pytest

from cfharness.errors import HubError
from cfharness.guard.classifier import Verdict
from cfharness.guard.url_safety import agent_domains_widening, normalize_agent_domains
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime

ALLOW = ["docs.python.org", "*.github.io"]


@pytest.fixture
def hub(hub_home):
    rt = FakeRuntime(hold=True)
    h = Hub(hub_home, runtime=rt)
    h.config.set("guard.enabled", True)
    h.config.set("approval.timeout_s", 0.3)
    h.config.set("guard.git_probe", False)
    h.guard.resolver = lambda host: {"rebind.example": ["127.0.0.1"]}.get(host, ["93.184.216.34"])
    yield h
    rt.release()
    h.store.close()


async def _run(hub, agent="w", tool_policy=None):
    if hub.store.get_agent(agent) is None:
        hub.agents.create({"id": agent, "name": agent.upper(), **({"tool_policy": tool_policy} if tool_policy is not None else {})})
    r = hub.runs.submit(agent, "x")
    for _ in range(300):
        cur = hub.store.get_run(r["id"])
        if cur and cur["status"] == "running":
            break
        await asyncio.sleep(0.01)
    return r["id"]


async def _check(hub, rid, url, tool="WebFetch"):
    return await asyncio.wait_for(hub.guard.check({"run_id": rid, "tool_name": tool, "tool_input": {"url": url, "prompt": "x"}, "channel": "hook"}), 20)


def _decisions(hub, rid):
    return [dict(r) for r in hub.store.conn.execute("SELECT * FROM guard_decisions WHERE run_id=? ORDER BY id", (rid,))]


def _audit(hub, rid):
    return [dict(r) for r in hub.store.conn.execute("SELECT * FROM audit_log WHERE run_id=? ORDER BY id", (rid,))]


# ============================================================================ kiểm hợp lệ / chuẩn hoá
def test_normalize_and_validate():
    assert normalize_agent_domains(None) is None
    assert normalize_agent_domains([]) == []
    assert normalize_agent_domains([" Docs.Python.ORG. ", "docs.python.org", "*.GitHub.io"]) == ["docs.python.org", "*.github.io"]
    assert normalize_agent_domains(["bücher.de", "*.münchen.de"]) == ["xn--bcher-kva.de", "*.xn--mnchen-3ya.de"]
    for bad in (["*"], ["https://x.com"], ["x.com/path"], ["user@x.com"], ["x.com:443"], ["a b.com"], [""], "x.com", [1], {"x": 1}):
        with pytest.raises(ValueError):
            normalize_agent_domains(bad)


def test_agent_spec_field(hub):
    hub.agents.create({"id": "a", "name": "a"})
    assert hub.store.get_agent("a")["tool_policy"]["web_allow_domains"] is None  # mặc định: chưa đặt
    hub.agents.update("a", {"tool_policy": {"web_allow_domains": ["Docs.Python.org."]}})
    assert hub.store.get_agent("a")["tool_policy"]["web_allow_domains"] == ["docs.python.org"]
    hub.agents.update("a", {"model": "haiku"})  # sửa trường khác không mất allowlist
    assert hub.store.get_agent("a")["tool_policy"]["web_allow_domains"] == ["docs.python.org"]
    with pytest.raises(HubError):
        hub.agents.update("a", {"tool_policy": {"web_allow_domains": ["*"]}})
    with pytest.raises(HubError):
        hub.agents.create({"id": "b", "name": "b", "tool_policy": {"web_allow_domains": ["http://evil.com/"]}})


def test_widening_rule():
    assert agent_domains_widening(None, ["a.com"]) is None  # đặt mới = thu hẹp
    assert agent_domains_widening(None, None) is None
    assert agent_domains_widening(["a.com", "b.com"], ["a.com"]) is None
    assert agent_domains_widening(["a.com"], []) is None
    assert agent_domains_widening(["a.com"], ["A.com."]) is None
    assert agent_domains_widening(["a.com"], None)  # bỏ allowlist
    assert agent_domains_widening(["a.com"], ["a.com", "evil.com"])
    assert agent_domains_widening(["a.com"], ["*.a.com"])  # mẫu mới (kể cả "hẹp hơn" về chữ) → coi là nới, chỉ người
    assert agent_domains_widening([], ["a.com"])
    assert agent_domains_widening(["a.com"], ["*"])  # giá trị sai → không nhận


# ============================================================================ CHƯA đặt → hành vi y như cũ
NOT_SET_CASES = [
    ("https://example.com/docs", "allow", None),
    ("https://evil.example/?q=secret", "allow", None),
    ("https://rebind.example/x", "deny", "ssrf_private_resolved"),  # vẫn giữ chờ người như WEB-1
    ("http://169.254.169.254/latest/meta-data/", "deny", "ssrf_metadata"),
]


@pytest.mark.parametrize("url,decision,rule", NOT_SET_CASES)
async def test_not_set_unchanged(hub, url, decision, rule):
    rid = await _run(hub)
    assert hub.store.get_agent("w")["tool_policy"].get("web_allow_domains") is None
    res = await _check(hub, rid, url)
    assert res["decision"] == decision
    rules = [d["matched_rule"] for d in _decisions(hub, rid)]
    assert "web_agent_domain_not_allowed" not in rules
    if rule:
        assert rule in rules


async def test_not_set_legacy_row_and_hub_allowlist_unchanged(hub):
    # bản ghi agent cũ (tool_policy không có khoá) + allowlist toàn hub: vẫn giữ chờ người (approval), không bị cấm thẳng
    rid = await _run(hub)
    hub.store.update_agent("w", {"tool_policy": {"permission_mode": None}})
    v = Verdict("safe", "x")
    run = hub.store.get_run(rid)
    assert hub.guard.agent_web_check(run, "WebFetch", {"url": "https://evil.com/"}, v) is None
    hub.config.set("guard.web.allow_domains", ["docs.python.org"])
    res = await _check(hub, rid, "https://evil.com/")
    assert res["decision"] == "deny"
    assert [d["matched_rule"] for d in _decisions(hub, rid)][-1] == "web_domain_not_allowed"
    assert any(d["action"] == "hold" for d in _decisions(hub, rid))


async def test_not_set_guard_off_keeps_webfetch(hub):
    hub.config.set("guard.enabled", False)
    await _run(hub)
    spec = hub.runs.runtime().started[-1]
    assert "WebFetch" not in (spec.tool_policy.get("disallowed_tools") or [])
    assert "WebFetch" in (spec.tool_policy.get("allowed_tools") or [])


# ============================================================================ ĐÃ đặt → ngoài danh sách bị từ chối + audit
ALLOWED = [
    "https://docs.python.org/3/",
    "https://DOCS.Python.ORG/3/library",
    "https://docs.python.org./x",  # dấu chấm cuối
    "https://docs.python.org:443/x",  # cổng không đổi host
    "http://docs.python.org:8080/x",
    "https://foo.github.io/",
    "https://a.b.github.io/x",
    "https://docs.python.org/redirect?to=https://evil.com/",  # host-based: chuyển hướng khác host = lời gọi WebFetch mới, qua hook lại
]

REFUSED = [
    "https://evil.com/?q=secret",
    "https://docs.python.org.evil.com/",  # hậu tố giả
    "https://evildocs.python.org/",
    "https://sub.docs.python.org/",  # subdomain không có trong danh sách
    "https://github.io/",  # `*.github.io` không gồm github.io
    "https://docs.python.org@evil.com/",  # userinfo
    "https://user:pw@docs.python.org/",  # userinfo dù host đúng → không tách chắc chắn được, từ chối
    "https://evil.com#@docs.python.org/",
    "https://evil.com/?@docs.python.org",
    "https://evil.com\\@docs.python.org/",  # `\` mập mờ giữa trình phân tích
    "https://docs.python.org%2eevil.com/",
    "https://dоcs.python.org/",  # chữ о Cyrillic (homograph) → punycode khác
    "http://93.184.216.34/",  # IP literal công khai
    "http://1572395042/",  # IP dạng số nguyên
    "http://0x5db8d822/",
    "http://[::ffff:5db8:d822]/",
    "http://127.0.0.1:8765/",  # nội bộ → giờ là CẤM thẳng (không chỉ giữ chờ)
    "https://rebind.example/",
    "ftp://docs.python.org/",
    "file:///etc/passwd",
    "docs.python.org/3/",  # thiếu scheme
    "",
]


async def test_allowed_domains_pass(hub):
    rid = await _run(hub, tool_policy={"web_allow_domains": ALLOW})
    for url in ALLOWED:
        res = await _check(hub, rid, url)
        assert res["decision"] == "allow", (url, res)


@pytest.mark.parametrize("url", REFUSED)
async def test_refused_and_audited(hub, url):
    rid = await _run(hub, tool_policy={"web_allow_domains": ALLOW})
    res = await _check(hub, rid, url)
    assert res["decision"] == "deny" and "Blocked by hub safety policy" in res["reason"], (url, res)
    assert not hub.approvals.list("pending", run_id=rid)  # từ chối thẳng, không mở approval
    dec = _decisions(hub, rid)[-1]
    assert dec["matched_rule"] == "web_agent_domain_not_allowed" and dec["action"] == "deny", (url, dec)
    assert any(a["kind"] == "guard.deny" for a in _audit(hub, rid))


async def test_idn_allowlist_matches_unicode_and_punycode(hub):
    rid = await _run(hub, tool_policy={"web_allow_domains": ["bücher.de"]})
    for url in ("https://bücher.de/", "https://BÜCHER.de/x", "https://xn--bcher-kva.de/"):
        assert (await _check(hub, rid, url))["decision"] == "allow", url
    assert (await _check(hub, rid, "https://bucher.de/"))["decision"] == "deny"


async def test_empty_list_blocks_all(hub):
    rid = await _run(hub, tool_policy={"web_allow_domains": []})
    assert (await _check(hub, rid, "https://docs.python.org/"))["decision"] == "deny"
    assert _decisions(hub, rid)[-1]["matched_rule"] == "web_agent_domain_not_allowed"


async def test_other_tools_untouched(hub):
    rid = await _run(hub, tool_policy={"web_allow_domains": ALLOW})
    res = await asyncio.wait_for(hub.guard.check({"run_id": rid, "tool_name": "Read", "tool_input": {"file_path": "x.txt"}, "channel": "hook"}), 20)
    assert res["decision"] == "allow"


async def test_hub_deny_and_metadata_still_apply(hub):
    hub.config.set("guard.web.deny_domains", ["docs.python.org"])
    rid = await _run(hub, tool_policy={"web_allow_domains": ["docs.python.org", "169.254.169.254"]})
    await _check(hub, rid, "https://docs.python.org/")
    assert _decisions(hub, rid)[-1]["matched_rule"] == "web_domain_denied"  # danh sách agent không gỡ chặn của hub
    await _check(hub, rid, "http://169.254.169.254/latest/")
    assert _decisions(hub, rid)[-1]["matched_rule"] == "ssrf_metadata"


async def test_trust_full_does_not_lift_allowlist(hub):
    rid = await _run(hub, tool_policy={"web_allow_domains": ALLOW})
    hub.guard.trust_set("w", "full")
    assert (await _check(hub, rid, "https://docs.python.org/"))["decision"] == "allow"
    res = await _check(hub, rid, "https://evil.com/")
    assert res["decision"] == "deny"
    assert _decisions(hub, rid)[-1]["matched_rule"] == "web_agent_domain_not_allowed"


async def test_chrome_navigate_same_rule(hub):
    rid = await _run(hub, tool_policy={"web_allow_domains": ALLOW})
    run = hub.store.get_run(rid)
    nav = "mcp__claude-in-chrome__navigate"
    v = Verdict("write", "chrome_navigate", "mcp", "x", net_urls=["https://evil.com/"])
    out = hub.guard.agent_web_check(run, nav, {"url": "https://evil.com/"}, v)
    assert out is not None and out.forbidden and out.rule == "web_agent_domain_not_allowed"
    v = Verdict("write", "chrome_navigate", "mcp", "x", net_urls=["https://docs.python.org/"])
    assert hub.guard.agent_web_check(run, nav, {"url": "https://docs.python.org/"}, v) is None


async def test_guard_test_rpc_reports_rule(hub):
    rid = await _run(hub, tool_policy={"web_allow_domains": ALLOW})
    v = await hub.guard.test({"tool": "WebFetch", "input": {"url": "https://evil.com/"}, "run_id": rid})
    assert v["forbidden"] and v["rule"] == "web_agent_domain_not_allowed"


async def test_guard_off_disables_webfetch_when_set(hub):
    hub.config.set("guard.enabled", False)
    await _run(hub, tool_policy={"web_allow_domains": ALLOW})
    spec = hub.runs.runtime().started[-1]
    assert "WebFetch" in spec.tool_policy["disallowed_tools"]  # không có hook lọc domain → fail-closed
    assert "WebFetch" not in (spec.tool_policy.get("allowed_tools") or [])


async def test_malformed_stored_value_fails_closed(hub):
    rid = await _run(hub)
    hub.store.update_agent("w", {"tool_policy": {"web_allow_domains": "docs.python.org"}})  # sửa tay sai kiểu
    assert (await _check(hub, rid, "https://docs.python.org/"))["decision"] == "deny"


# ============================================================================ quyền đổi allowlist
@pytest.fixture
async def mgr(hub_home):
    from m7_fake import close, make_daemon, manager_run

    d = await make_daemon(hub_home)
    res = await manager_run(d)
    d.hub.agents.create({"id": "w1", "name": "w1"})
    yield d, res["run_id"]
    await close(d)


async def test_manager_can_narrow_not_widen(mgr):
    from m7_fake import tool

    d, rid = mgr
    await tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"web_allow_domains": ["a.com", "b.com"]}}})
    assert d.hub.store.get_agent("w1")["tool_policy"]["web_allow_domains"] == ["a.com", "b.com"]
    await tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"web_allow_domains": ["a.com"]}}})
    for widen in (None, ["a.com", "evil.com"], ["*.a.com"]):
        with pytest.raises(HubError, match="chỉ người"):
            await tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"web_allow_domains": widen}}})
    with pytest.raises(HubError):
        await tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"web_allow_domains": ["*"]}}})
    assert d.hub.store.get_agent("w1")["tool_policy"]["web_allow_domains"] == ["a.com"]
    # người nới được (bề mặt người)
    d.hub.agents.update("w1", {"tool_policy": {"web_allow_domains": None}})
    assert d.hub.store.get_agent("w1")["tool_policy"]["web_allow_domains"] is None


def test_learning_cannot_widen(hub):
    from cfharness.learning.guard import LearningGuard

    lg = LearningGuard(hub)

    def blk(new, cur):
        return lg._config({"target": "config", "payload": {"changes": {"tool_policy": {"web_allow_domains": new}}}},
                          {"current_config": {"tool_policy": {"web_allow_domains": cur}}})

    for new, cur in ((None, ["a.com"]), (["a.com", "evil.com"], ["a.com"]), (["a.com"], [])):
        b = blk(new, cur)
        assert b is not None and b.category == "self_escalation" and b.rule == "web_allow_domains", (new, cur)
    assert blk(["a.com"], ["a.com", "b.com"]) is None  # thu hẹp thì được
    assert blk(["a.com"], None) is None


def test_owner_action_accepts_field(hub):
    from cfharness.manager.owner_actions import TOOL_POLICY_FIELDS

    assert "web_allow_domains" in TOOL_POLICY_FIELDS  # agent XIN người qua nút duyệt (người quyết)


# ============================================================================ hiển thị: CLI / web
def test_cli_flag_and_text(hub):
    from cfharness.cli import _agent_payload, _agent_text, build_parser

    a = build_parser().parse_args(["agent", "edit", "w", "--web-domains", "docs.python.org,*.github.io"])
    assert _agent_payload(a, creating=False) == {"tool_policy": {"web_allow_domains": ["docs.python.org", "*.github.io"]}}
    a = build_parser().parse_args(["agent", "edit", "w", "--web-domains", "any"])
    assert _agent_payload(a, creating=False) == {"tool_policy": {"web_allow_domains": None}}
    a = build_parser().parse_args(["agent", "edit", "w", "--web-domains", "none"])
    assert _agent_payload(a, creating=False) == {"tool_policy": {"web_allow_domains": []}}
    hub.agents.create({"id": "w", "name": "w", "tool_policy": {"web_allow_domains": ["docs.python.org"]}})
    ag = hub.agents.show("w")
    assert "web_allow_domains: docs.python.org" in _agent_text(ag)
    hub.agents.update("w", {"tool_policy": {"web_allow_domains": None}})
    assert "web_allow_domains: (không giới hạn)" in _agent_text(hub.agents.show("w"))


def test_web_form_changes():
    from starlette.datastructures import FormData

    from cfharness.web.app import agent_changes

    ch = agent_changes(FormData([("name", "w"), ("web_allow_domains", "docs.python.org, *.github.io")]), creating=False, from_template=False)
    assert ch["tool_policy"]["web_allow_domains"] == ["docs.python.org", "*.github.io"]
    ch = agent_changes(FormData([("name", "w"), ("web_allow_domains", "")]), creating=False, from_template=False)
    assert ch["tool_policy"]["web_allow_domains"] is None
    ch = agent_changes(FormData([("name", "w")]), creating=False, from_template=False)  # client cũ không có ô → giữ nguyên
    assert "web_allow_domains" not in ch["tool_policy"]
