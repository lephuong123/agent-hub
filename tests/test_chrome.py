"""Claude in Chrome cho agent (docs/dev-notes/chrome.md): `tool_policy.browser`, cờ `--chrome`/`--no-chrome` (flag matrix H-10),
bỏ Playwright khi `chrome`, khối prompt `auto`, phân loại guard từng lớp tool chrome (gồm navigate tới loopback/metadata), ghi đè của
người, khoá trình duyệt giữa hai run, kill switch, quyền điều phối viên.

Không gọi claude thật, không đụng trình duyệt: Hub in-process + FakeRuntime(hold) / daemon m7_fake (ScriptedRuntime).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cfharness.browser import CHROME_SERVER, effective_mode, is_playwright_server, prompt_block
from cfharness.config import DEFAULTS
from cfharness.errors import HubError
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore
from cfharness.hub import Hub
from cfharness.runtime import ArgvError, FakeRuntime, RunSpec, build_argv

C = f"mcp__{CHROME_SERVER}__"
PUBLIC_IP = "93.184.216.34"


def spec(**kw) -> RunSpec:
    base = dict(run_id="r_1", agent_id="a", prompt="p", cwd=Path("/tmp/w"), session_id="11111111-2222-3333-4444-555555555555",
                raw_log_path=Path("/tmp/r.jsonl"), stderr_log_path=Path("/tmp/r.err"))
    base.update(kw)
    return RunSpec(**base)


def ccfg(**chrome) -> dict:
    c = json.loads(json.dumps(DEFAULTS["runtime"]["claude"]))
    c["chrome"] = {**c["chrome"], **chrome}
    return c


# ============================================================================ argv
@pytest.mark.parametrize("browser,version,expect", [
    ("chrome", None, "--chrome"),
    ("auto", None, "--chrome"),
    ("chrome", "2.1.310", "--chrome"),
    ("auto", "2.1.310", "--chrome"),
    ("none", None, None),  # phiên bản không rõ → không đoán cờ --no-chrome
    ("playwright", None, None),
    ("none", "2.1.310", "--no-chrome"),
    ("playwright", "2.1.310", "--no-chrome"),
    (None, "2.1.310", "--no-chrome"),  # agent cũ không có trường browser
    ("auto", "2.1.302", None),  # 2.1.302 chưa có --chrome trong flag_matrix → auto bỏ cờ (còn Playwright)
    ("none", "2.1.302", None),
])
def test_argv_chrome_flags(browser, version, expect):
    tp = {} if browser is None else {"browser": browser}
    argv, _ = build_argv(spec(tool_policy=tp), ccfg(), version)  # H-10: check_flag_matrix chạy bên trong — không được ném
    got = [f for f in ("--chrome", "--no-chrome") if f in argv]
    assert got == ([expect] if expect else []), argv


def test_argv_chrome_on_version_without_flag_is_refused():
    with pytest.raises(ArgvError, match="--chrome"):
        build_argv(spec(tool_policy={"browser": "chrome"}), ccfg(), "2.1.302")


def test_argv_pass_no_chrome_off():
    argv, _ = build_argv(spec(tool_policy={"browser": "none"}), ccfg(pass_no_chrome=False), "2.1.310")
    assert "--no-chrome" not in argv and "--chrome" not in argv


def test_flag_matrix_has_chrome_only_for_2_1_310():
    m = DEFAULTS["runtime"]["claude"]["flag_matrix"]
    assert {"--chrome", "--no-chrome"} <= set(m["2.1.310"])
    assert "--chrome" not in m["2.1.302"] and "--chrome" not in m["2.1.303"]


def test_is_playwright_server():
    assert is_playwright_server("playwright", {})
    assert is_playwright_server("browser", {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]})
    assert not is_playwright_server("github", {"command": "npx", "args": ["@modelcontextprotocol/server-github"]})


# ============================================================================ hub in-process
@pytest.fixture
def hub(hub_home):
    rt = FakeRuntime(hold=True)
    h = Hub(hub_home, runtime=rt)
    h.config.set("approval.timeout_s", 0.3)
    h.guard.resolver = lambda host: [PUBLIC_IP] if host != "intranet.example.com" else ["10.1.2.3"]
    yield h
    rt.release()


def _agent(hub, aid, browser=None, mcp=None):
    cfg = {"id": aid, "name": aid}
    if browser:
        cfg["tool_policy"] = {"browser": browser}
    if mcp:
        cfg["mcp_servers"] = mcp
    hub.agents.create(cfg)


async def _running(hub, aid):
    r = hub.runs.submit(aid, "x")
    for _ in range(500):
        cur = hub.store.get_run(r["id"])
        if cur and cur["status"] == "running":
            return cur
        await asyncio.sleep(0.01)
    raise AssertionError("run không chạy")


def _spec_for(hub, aid):
    run = hub.store.get_run(hub.runs.submit(aid, "hi")["id"])
    return hub.runs._prepare_spec(run, hub.store.get_agent(aid))


def _add_playwright(hub):
    hub.mcp.add({"name": "playwright", "transport": "stdio", "command": "npx", "args": ["@playwright/mcp@latest"], "trust": "trusted"})


async def test_prepare_spec_chrome_drops_playwright_auto_keeps(hub):
    _add_playwright(hub)
    _agent(hub, "c", "chrome", ["playwright"])
    _agent(hub, "u", "auto", ["playwright"])
    _agent(hub, "n", None, ["playwright"])
    sc, su, sn = _spec_for(hub, "c"), _spec_for(hub, "u"), _spec_for(hub, "n")
    servers = lambda s: set(json.loads(Path(s.mcp_config_path).read_text(encoding="utf-8"))["mcpServers"])  # noqa: E731
    assert "playwright" not in servers(sc) and "playwright" not in sc.allowed_mcp_servers
    assert CHROME_SERVER in sc.allowed_mcp_servers  # server dynamic của --chrome không phải isolation.violation
    assert "playwright" in servers(su) and CHROME_SERVER in su.allowed_mcp_servers
    assert "playwright" in servers(sn) and CHROME_SERVER not in sn.allowed_mcp_servers
    v = "2.1.310"
    assert "--chrome" in build_argv(sc, hub.config.get("runtime.claude"), v)[0]
    assert "--chrome" in build_argv(su, hub.config.get("runtime.claude"), v)[0]
    argv_n = build_argv(sn, hub.config.get("runtime.claude"), v)[0]
    assert "--chrome" not in argv_n and "--no-chrome" in argv_n


async def test_prompt_block_auto_and_chrome(hub):
    _agent(hub, "u", "auto")
    _agent(hub, "c", "chrome")
    _agent(hub, "n")
    pu, pc, pn = _spec_for(hub, "u").system_prompt, _spec_for(hub, "c").system_prompt, _spec_for(hub, "n").system_prompt
    assert "prefer Claude in Chrome, fall back to Playwright" in pu and "list_connected_browsers" in pu and "mcp__claude-in-chrome__" in pu
    assert "Browser: Claude in Chrome" in pc and "Playwright" not in pc.split("# Browser")[1].split("\n# ")[0].replace("no other browser", "")
    assert "claude-in-chrome" not in pn and "# Browser" not in pn
    assert "untrusted data" in prompt_block("auto") and prompt_block("none") == prompt_block("playwright") == ""


async def test_kill_switch(hub):
    _add_playwright(hub)
    _agent(hub, "c", "chrome", ["playwright"])
    with pytest.raises(ValueError):
        hub.config.set("runtime.claude.chrome.enabled", "no")
    with pytest.raises(ValueError):
        hub.config.set("runtime.claude.chrome.max_concurrent_runs", 0)
    hub.config.set("runtime.claude.chrome.enabled", False)
    assert effective_mode(hub.store.get_agent("c"), hub.config) == "playwright"
    s = _spec_for(hub, "c")
    argv = build_argv(s, hub.config.get("runtime.claude"), "2.1.310")[0]
    assert "--chrome" not in argv and "--no-chrome" in argv
    assert "claude-in-chrome" not in (s.system_prompt or "")
    assert "playwright" in json.loads(Path(s.mcp_config_path).read_text(encoding="utf-8"))["mcpServers"]  # chrome tắt → Playwright quay lại


async def test_kill_switch_and_not_enabled_agent_block_chrome_tools(hub):
    _agent(hub, "n")
    _agent(hub, "c", "chrome")
    rn = await _running(hub, "n")
    res = await hub.guard.check({"run_id": rn["id"], "tool_name": C + "tabs_context_mcp", "tool_input": {}, "channel": "hook"})
    assert res["decision"] == "deny" and "không bật" in res["reason"]
    rc = await _running(hub, "c")
    assert (await hub.guard.check({"run_id": rc["id"], "tool_name": C + "read_page", "tool_input": {}, "channel": "hook"}))["decision"] == "allow"
    hub.config.set("runtime.claude.chrome.enabled", False)
    res = await hub.guard.check({"run_id": rc["id"], "tool_name": C + "read_page", "tool_input": {}, "channel": "hook"})
    assert res["decision"] == "deny"


# ============================================================================ phân loại (thuần)
def ctx(tmp_path, **kw) -> GuardContext:
    return GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "hubhome").load(), git_probe=False, chrome_allowed=True, **kw)


READ = ["tabs_context_mcp", "list_connected_browsers", "read_page", "get_page_text", "find", "read_console_messages",
        "read_network_requests", "shortcuts_list"]
ACTION = ["form_input", "shortcuts_execute", "select_browser", "switch_browser", "resize_window", "tabs_close_mcp"]
SENSITIVE = ["javascript_tool", "file_upload", "upload_image"]


def test_classification_table(tmp_path):
    c = ctx(tmp_path)
    for t in READ:
        v = classify(C + t, {}, c)
        assert v.verdict == "safe" and v.rule == "chrome_read", (t, v)
    for t in ACTION:
        v = classify(C + t, {}, c)
        assert (v.verdict, v.rule, v.klass) == ("uncertain", "chrome_action", "mcp"), (t, v)
    for t in SENSITIVE:
        v = classify(C + t, {}, c)
        assert (v.verdict, v.rule) == ("uncertain", "chrome_sensitive") and v.reason, (t, v)
    assert classify(C + "computer", {"action": "left_click", "coordinate": [1, 2], "tabId": 1}, c).rule == "chrome_action"
    assert classify(C + "computer", {"action": "type", "text": "x", "tabId": 1}, c).rule == "chrome_action"
    for a in ("screenshot", "zoom", "scroll", "hover", "wait"):
        assert classify(C + "computer", {"action": a, "tabId": 1}, c).verdict == "safe", a
    assert classify(C + "gif_creator", {"action": "start_recording", "tabId": 1}, c).verdict == "safe"
    assert classify(C + "gif_creator", {"action": "export", "download": True, "tabId": 1}, c).rule == "chrome_action"
    v = classify(C + "brand_new_tool", {}, c)
    assert v.rule == "mcp_unknown_tool"


def test_navigate_url_rules_like_webfetch(tmp_path):
    c = ctx(tmp_path)
    v = classify(C + "navigate", {"url": "https://example.com/a"}, c)
    assert (v.verdict, v.rule) == ("write", "chrome_navigate") and v.net_urls == ["https://example.com/a"]
    assert classify(C + "navigate", {"url": "example.com"}, c).net_urls == ["https://example.com"]  # thiếu scheme → https
    assert classify(C + "navigate", {"url": "back", "tabId": 3}, c).rule == "chrome_navigate"
    for url in ("http://127.0.0.1:3000/", "localhost:5173", "http://192.168.1.10/admin", "http://[::1]/"):
        v = classify(C + "navigate", {"url": url}, c)
        assert v.klass == "network" and v.verdict == "uncertain" and not v.forbidden, (url, v)
        assert v.rule == classify("WebFetch", {"url": url if "://" in url else "https://" + url}, c).rule  # cùng luật WebFetch
    v = classify(C + "navigate", {"url": "http://169.254.169.254/latest/meta-data/"}, c)
    assert v.forbidden and v.rule == "ssrf_metadata"
    assert classify(C + "navigate", {"url": "javascript:alert(1)"}, c).rule == "url_scheme"
    assert classify(C + "navigate", {"url": "file:///C:/Users/x/.ssh/id_rsa"}, c).rule == "url_scheme"
    assert classify(C + "tabs_create_mcp", {}, c).verdict == "write"


def test_browser_batch_strictest_inner(tmp_path):
    c = ctx(tmp_path)
    b = lambda *acts: classify(C + "browser_batch", {"actions": [{"name": n, "input": i} for n, i in acts]}, c)  # noqa: E731
    assert b(("find", {"query": "x", "tabId": 1}), ("computer", {"action": "screenshot", "tabId": 1})).verdict == "safe"
    assert b(("navigate", {"url": "https://a.com", "tabId": 1}), ("computer", {"action": "left_click", "tabId": 1})).rule == "chrome_action"
    assert b(("computer", {"action": "screenshot", "tabId": 1}), ("javascript_tool", {"text": "1", "tabId": 1})).rule == "chrome_sensitive"
    v = b(("navigate", {"url": "http://10.0.0.5/", "tabId": 1}),)
    assert v.klass == "network" and v.rule == "webfetch_local" and v.hold
    v = b(("computer", {"action": "screenshot", "tabId": 1}), ("navigate", {"url": "http://169.254.169.254/", "tabId": 1}))
    assert v.forbidden and v.rule == "ssrf_metadata"
    v = b(("navigate", {"url": "https://ok.com", "tabId": 1}), ("mcp__claude-in-chrome__navigate", {"url": "https://two.com", "tabId": 1}))
    assert set(v.net_urls) == {"https://ok.com", "https://two.com"}  # mọi URL con đều qua DNS/allowlist ở daemon
    assert b(("browser_batch", {"actions": []}),).rule == "chrome_action"  # lồng nhau
    assert classify(C + "browser_batch", {"actions": "nope"}, c).rule == "chrome_action"
    assert classify(C + "browser_batch", {}, c).rule == "chrome_action"


def test_not_enabled_is_forbidden(tmp_path):
    c = GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "h").load(), git_probe=False, chrome_allowed=False)
    v = classify(C + "read_page", {}, c)
    assert v.forbidden and v.rule == "chrome_not_enabled"


# ============================================================================ guard trong daemon: profile, DNS/domain, ghi đè
async def test_profiles_hold_matrix(hub):
    _agent(hub, "c", "chrome")
    run = await _running(hub, "c")

    async def t(tool, ti=None):
        return await hub.guard.test({"run_id": run["id"], "tool": C + tool, "input": ti or {}})

    # balanced (mặc định): đọc safe, hành động cho chạy + audit, nhạy cảm giữ
    assert (await t("tabs_context_mcp"))["hold"] is False
    r = await t("computer", {"action": "left_click", "tabId": 1})
    assert r["verdict"] == "uncertain" and r["hold"] is False
    r = await t("javascript_tool", {"text": "document.cookie", "tabId": 1})
    assert r["hold"] is True and r["never_automatic"]  # chỉ người quyết (không giao điều phối viên)
    assert (await t("navigate", {"url": "https://example.com"}))["hold"] is False
    # strict: hành động cũng giữ
    hub.config.set("approval.profile", "strict")
    assert (await t("computer", {"action": "left_click", "tabId": 1}))["hold"] is True
    assert (await t("tabs_close_mcp", {"tabId": 1}))["hold"] is True
    assert (await t("read_page", {"tabId": 1}))["hold"] is False
    assert (await t("navigate", {"url": "https://example.com"}))["hold"] is False  # write: audit, không giữ
    # autonomous: nhạy cảm vẫn giữ
    hub.config.set("approval.profile", "autonomous")
    assert (await t("file_upload", {"paths": ["C:/x"], "tabId": 1}))["hold"] is True


async def test_navigate_network_rules_in_daemon(hub):
    _agent(hub, "c", "chrome")
    run = await _running(hub, "c")

    async def chk(url):
        return await hub.guard.check({"run_id": run["id"], "tool_name": C + "navigate", "tool_input": {"url": url, "tabId": 1}, "channel": "hook",
                                      "max_wait_s": 0.05})

    r = await chk("https://example.com")
    assert r["decision"] == "allow"
    row = hub.store.conn.execute("SELECT verdict, matched_rule, action FROM guard_decisions WHERE tool_name=? ORDER BY id DESC LIMIT 1",
                                 (C + "navigate",)).fetchone()
    assert tuple(row) == ("write", "chrome_navigate", "allow")
    r = await chk("http://169.254.169.254/latest/meta-data/")
    assert r["decision"] == "deny" and "metadata" in r["reason"]
    t = await hub.guard.test({"run_id": run["id"], "tool": C + "navigate", "input": {"url": "http://127.0.0.1:8080/"}})
    assert t["klass"] == "network" and t["hold"] is True  # localhost dev server: cùng luật WebFetch (giữ chờ người)
    t = await hub.guard.test({"run_id": run["id"], "tool": C + "navigate", "input": {"url": "https://intranet.example.com/"}})
    assert t["rule"] == "ssrf_private_resolved" and t["hold"] is True  # DNS → IP nội bộ
    hub.config.set("guard.web", {"deny_domains": ["*.evil.example"]})
    t = await hub.guard.test({"run_id": run["id"], "tool": C + "navigate", "input": {"url": "https://a.evil.example/"}})
    assert t["forbidden"] and t["rule"] == "web_domain_denied"
    hub.config.set("guard.web", {"allow_domains": ["example.com"]})
    t = await hub.guard.test({"run_id": run["id"], "tool": C + "browser_batch",
                              "input": {"actions": [{"name": "navigate", "input": {"url": "https://other.org", "tabId": 1}}]}})
    assert t["rule"] == "web_domain_not_allowed" and t["hold"] is True


async def test_human_override_wins_but_not_over_url_rules(hub):
    _agent(hub, "c", "chrome")
    run = await _running(hub, "c")
    t = lambda tool, ti=None: hub.guard.test({"run_id": run["id"], "tool": C + tool, "input": ti or {}})  # noqa: E731
    hub.config.set("runtime.claude.chrome.annotations", {"javascript_tool": {"destructiveHint": False}, "tabs_*": {"destructiveHint": True},
                                                         "navigate": {"readOnlyHint": True}})
    r = await t("javascript_tool", {"text": "1", "tabId": 1})
    assert (r["verdict"], r["rule"], r["hold"]) == ("write", "chrome_human_override", False)
    r = await t("tabs_context_mcp")
    assert r["verdict"] == "destructive" and r["hold"] is True
    assert (await t("navigate", {"url": "https://example.com"}))["verdict"] == "safe"
    r = await t("navigate", {"url": "http://169.254.169.254/"})
    assert r["forbidden"]  # ghi đè không gỡ luật SSRF
    with pytest.raises(ValueError):
        hub.config.set("runtime.claude.chrome.annotations", {"x": {"trust": "trusted"}})


async def test_always_allow_mcp_tool_rule_for_chrome_action(hub):
    hub.config.set("approval.profile", "strict")
    _agent(hub, "c", "chrome")
    run = await _running(hub, "c")
    task = asyncio.create_task(hub.guard.check({"run_id": run["id"], "tool_name": C + "computer",
                                                "tool_input": {"action": "left_click", "tabId": 1}, "channel": "hook"}))
    for _ in range(100):
        aps = hub.approvals.list("pending", run_id=run["id"])
        if aps:
            break
        await asyncio.sleep(0.01)
    offer = hub.guard.always_offer(aps[0])
    assert offer and offer["kind"] == "mcp_tool" and offer["confirm"] == C + "computer"
    spec_ = hub.guard.always_prepare(aps[0]["id"])
    hub.approvals.resolve(aps[0]["id"], "allowed")
    hub.guard.always_commit(spec_)
    assert (await task)["decision"] == "allow"
    res = await hub.guard.check({"run_id": run["id"], "tool_name": C + "computer", "tool_input": {"action": "type", "text": "x", "tabId": 1},
                                 "channel": "hook"})
    assert res["decision"] == "allow"


# ============================================================================ khoá trình duyệt
async def test_browser_lock_two_runs(hub):
    _agent(hub, "a1", "chrome")
    _agent(hub, "a2", "auto")
    hub.agents.update("a1", {"limits": {"max_concurrent_runs": 2}})
    r1, r2 = await _running(hub, "a1"), await _running(hub, "a2")

    async def chk(run, tool, ti=None):
        return await hub.guard.check({"run_id": run["id"], "tool_name": C + tool, "tool_input": ti or {}, "channel": "hook"})

    assert (await chk(r1, "tabs_context_mcp"))["decision"] == "allow"
    assert (await chk(r1, "read_page", {"tabId": 1}))["decision"] == "allow"  # cùng run → giữ khoá
    res = await chk(r2, "tabs_context_mcp")
    assert res["decision"] == "deny" and res.get("browser_busy") and "Playwright" in res["reason"] and "a1" in res["reason"]
    assert (await chk(r2, "list_connected_browsers"))["decision"] == "allow"  # không cần khoá
    assert set(hub.guard.browser.list()) == {r1["id"]}
    # run chrome (không auto) bận → hướng dẫn chờ, không gợi ý Playwright
    r3 = await _running(hub, "a1")
    res3 = await chk(r3, "read_page")
    assert res3["decision"] == "deny" and "Playwright" not in res3["reason"] and "retry" in res3["reason"]
    # max_concurrent_runs=2 → run thứ hai được
    hub.config.set("runtime.claude.chrome.max_concurrent_runs", 2)
    assert (await chk(r2, "tabs_context_mcp"))["decision"] == "allow"
    hub.config.set("runtime.claude.chrome.max_concurrent_runs", 1)
    # kết thúc run → nhả khoá
    await hub.runs.stop(r1["id"])
    await hub.runs.stop(r2["id"])
    for _ in range(300):
        if not hub.guard.browser.list():
            break
        await asyncio.sleep(0.01)
    assert hub.guard.browser.list() == {}
    assert (await chk(r3, "read_page"))["decision"] == "allow"


def test_browser_lock_stale_holder_cleaned(hub):
    lock = hub.guard.browser
    assert lock.acquire("r_dead", "x", 1) is None
    assert lock.acquire("r_new", "y", 1) == {"r_dead": lock.list()["r_dead"]}
    assert lock.acquire("r_new", "y", 1, lambda rid: rid != "r_dead") is None
    assert set(lock.list()) == {"r_new"}


# ============================================================================ điều phối viên / học / CLI
@pytest.fixture
async def mgr(hub_home):
    from m7_fake import close, make_daemon, manager_run

    d = await make_daemon(hub_home)
    res = await manager_run(d)
    d.hub.agents.create({"id": "w1", "name": "w1"})
    yield d, res["run_id"]
    await close(d)


async def test_manager_sets_browser_on_others_not_itself(mgr):
    from m7_fake import tool

    d, rid = mgr
    await tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"browser": "chrome"}}})
    assert d.hub.store.get_agent("w1")["tool_policy"]["browser"] == "chrome"
    with pytest.raises(HubError, match="điều phối viên"):
        await tool(d, rid, "agent_update", {"id": "boss", "changes": {"tool_policy": {"browser": "chrome"}}})
    with pytest.raises(HubError):
        await tool(d, rid, "agent_update", {"id": "w1", "changes": {"tool_policy": {"browser": "firefox"}}})
    assert d.hub.store.get_agent("boss")["tool_policy"].get("browser", "none") == "none"
    # chủ hub đặt cho điều phối viên → được (bề mặt người)
    d.hub.agents.update("boss", {"tool_policy": {"browser": "auto"}})
    assert d.hub.store.get_agent("boss")["tool_policy"]["browser"] == "auto"


def test_learning_cannot_enable_chrome(hub):
    from cfharness.learning.guard import LearningGuard

    lg = LearningGuard(hub)
    blk = lg._config({"target": "config", "payload": {"changes": {"tool_policy": {"browser": "chrome"}}}},
                     {"current_config": {"tool_policy": {"browser": "none"}}})
    assert blk is not None and blk.rule == "browser" and blk.category == "self_escalation"
    assert lg._config({"target": "config", "payload": {"changes": {"tool_policy": {"browser": "none"}}}},
                      {"current_config": {"tool_policy": {"browser": "chrome"}}}) is None  # tắt bớt thì được


def test_agent_validation_and_cli_flag(hub):
    from cfharness.cli import _agent_payload, build_parser

    with pytest.raises(HubError):
        hub.agents.create({"id": "bad", "name": "bad", "tool_policy": {"browser": "firefox"}})
    _agent(hub, "w")
    assert hub.store.get_agent("w")["tool_policy"]["browser"] == "none"  # mặc định = như cũ
    a = build_parser().parse_args(["agent", "edit", "w", "--browser", "chrome"])
    assert _agent_payload(a, creating=False) == {"tool_policy": {"browser": "chrome"}}
    hub.agents.update("w", {"tool_policy": {"browser": "chrome"}})
    hub.agents.update("w", {"model": "haiku"})
    assert hub.store.get_agent("w")["tool_policy"]["browser"] == "chrome"  # sửa trường khác không mất browser


def test_templates_default_auto():
    from cfharness.templates import load

    for name in ("researcher", "coder", "reviewer"):
        assert load(name)["config"]["tool_policy"]["browser"] == "auto", name
    assert load("ops")["config"].get("tool_policy", {}).get("browser") is None  # mẫu khác: không đổi


def test_web_form_browser_field():
    from starlette.datastructures import FormData

    from cfharness.web.app import agent_changes

    assert agent_changes(FormData([("name", "A"), ("browser", "chrome")]), creating=False, from_template=False)["tool_policy"]["browser"] == "chrome"
    assert agent_changes(FormData([("name", "A"), ("browser", "")]), creating=False, from_template=False)["tool_policy"]["browser"] == "none"
    assert "browser" not in agent_changes(FormData([("name", "A")]), creating=False, from_template=False)["tool_policy"]  # form cũ: giữ nguyên
    with pytest.raises(ValueError):
        agent_changes(FormData([("browser", "firefox")]), creating=False, from_template=False)
