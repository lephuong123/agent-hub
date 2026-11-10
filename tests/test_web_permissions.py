"""Trang "Quyền đã cấp" (web/perm_pages.py, đề xuất #3): xem + gỡ grant theo luật, trust full, luật luôn cho phép, ngoại lệ sandbox,
tài nguyên bảo vệ. Gỡ quyền = 1 bước xác nhận; gỡ tài nguyên bảo vệ (NỚI quyền) = 2 bước. Audit kênh web. Không gọi `claude`.

Cùng khung với tests/test_web.py: daemon IN-PROCESS trên socket thật + FakeRuntime, app Starlette qua ASGITransport.
"""

from __future__ import annotations

import json

import pytest

from cfharness.runtime import FakeRuntime
from cfharness.web.perm_pages import always_view, grant_rows, sandbox_rows
from test_web import ISOLATED, _start, _stop, audit_rows  # noqa: F401


@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    yield e
    await _stop(e)


def _always(hub, agent: str = "ag0") -> dict:
    return hub.guard.policy_store.always_add({"kind": "web_domain", "agent_id": agent, "tool": "WebFetch", "domain": "docs.example.com",
                                             "approval_id": "ap_x", "by": "user", "channel": "cli"})


# =========================================================================== hàm thuần
def test_pure_helpers():
    rows = grant_rows({"b": {"r2": {"until": None}}, "a": {"r1": {"by": "user"}, "r0": {}}})
    assert [(r["agent_id"], r["rule"]) for r in rows] == [("a", "r0"), ("a", "r1"), ("b", "r2")]
    sb = sandbox_rows({"ag0": {"allow_localhost": True, "allow_domains": ["api.x.com", "dev.local"], "dev_hosts": ["dev.local"], "by": "user"}})
    assert [(r["kind"], r["value"]) for r in sb] == [("localhost", "127.0.0.1 / ::1"), ("dev_host", "dev.local"), ("domain", "api.x.com")]
    assert "docs.example.com" in always_view({"kind": "web_domain", "agent_id": "ag0", "domain": "docs.example.com"})
    assert "mọi agent" in always_view({"kind": "exact", "tool": "Bash", "fingerprint": "abc"})


# =========================================================================== xác thực / menu
async def test_requires_login_csrf_and_listed_in_menu(env):
    c = env.client
    assert (await c.get("/permissions")).status_code == 303
    assert (await c.post("/do/perm_grant_rm", data={"agent": "ag0", "rule": "x", "confirm": "yes"})).status_code == 401
    await env.login()
    env.hub.guard.rule_grant_set("ag0", "delete_not_run_created")
    r = await c.post("/do/perm_grant_rm", data={"agent": "ag0", "rule": "delete_not_run_created", "confirm": "yes", "csrf": env.csrf})
    assert r.status_code == 403 and env.hub.guard.rule_grants().get("ag0")  # thiếu khoá phiên → không gỡ
    r = await c.get("/permissions")
    assert r.status_code == 200 and "Quyền đã cấp" in r.text and 'href="/permissions"' in r.text and 'aria-current="page"' in r.text


# =========================================================================== xem + gỡ
async def test_lists_everything_and_revokes_with_one_confirm(env):
    await env.login()
    hub = env.hub
    hub.guard.rule_grant_set("ag0", "delete_not_run_created", for_s=3600)
    hub.guard.trust_set("ag0", "full")
    rule = _always(hub)
    hub.guard.sandbox_set("ag0", allow_localhost=True)
    hub.guard.sandbox_allow_domain("ag0", "api.example.com")
    hub.guard.sandbox_dev_host("ag0", "dev.local")
    r = await env.client.get("/permissions")
    assert r.status_code == 200
    t = r.text
    assert "delete_not_run_created" in t and 'id="trust-ag0"' in t and rule["id"] in t and "docs.example.com" in t
    assert "api.example.com" in t and "dev.local" in t and "Localhost" in t
    assert "/do/perm_grant_rm" in t and "/do/perm_trust_off" in t and "/do/perm_always_rm" in t

    # gỡ grant: chưa xác nhận → trang xác nhận, không đổi; xác nhận → gỡ + audit kênh web
    form = {"agent": "ag0", "rule": "delete_not_run_created", "next": "/permissions"}
    r = await env.post("/do/perm_grant_rm", form)
    assert r.status_code == 200 and "lại chờ duyệt" in r.text and hub.guard.rule_grants().get("ag0")
    r = await env.post("/do/perm_grant_rm", {**form, "confirm": "yes"})
    assert r.status_code == 303 and not hub.guard.rule_grants().get("ag0")
    au = audit_rows(env, "guard.rule_grant")[-1]
    assert json.loads(au["data"])["action"] == "remove" and au["channel"] == "web"

    # tắt trust
    r = await env.post("/do/perm_trust_off", {"agent": "ag0", "next": "/permissions", "confirm": "yes"})
    assert r.status_code == 303 and hub.guard.trust_of("ag0") is None
    assert audit_rows(env, "guard.trust")[-1]["channel"] == "web"

    # gỡ luật luôn cho phép = vô hiệu hoá (giữ lịch sử); id không phải aa_ → từ chối (không gỡ nhầm tài nguyên bảo vệ)
    r = await env.post("/do/perm_always_rm", {"id": "pr_khac", "next": "/permissions", "confirm": "yes"})
    assert r.status_code == 303 and "Chưa đủ thông tin" in (await env.client.get("/permissions")).text
    r = await env.post("/do/perm_always_rm", {"id": rule["id"], "next": "/permissions", "confirm": "yes"})
    assert r.status_code == 303 and hub.guard.always_rules() == []
    assert any(x["id"] == rule["id"] and x.get("disabled") for x in hub.guard.policy()["always_allow"])
    pc = audit_rows(env, "policy.changed")
    assert pc and json.loads(pc[-1]["data"])["change"] == "always.rm" and pc[-1]["channel"] == "web"

    # ngoại lệ sandbox
    await env.post("/do/perm_domain_rm", {"agent": "ag0", "value": "api.example.com", "next": "/permissions", "confirm": "yes"})
    await env.post("/do/perm_dev_host_rm", {"agent": "ag0", "value": "dev.local", "next": "/permissions", "confirm": "yes"})
    await env.post("/do/perm_localhost_off", {"agent": "ag0", "next": "/permissions", "confirm": "yes"})
    o = hub.guard.sandbox_overrides("ag0")
    assert not o.get("allow_localhost") and not o.get("allow_domains") and not o.get("dev_hosts")
    r = await env.client.get("/permissions")
    assert "Không agent nào có ngoại lệ sandbox" in r.text and "Không agent nào đang trust full" in r.text


async def test_protected_add_one_step_remove_two_steps(env):
    await env.login()
    hub = env.hub
    form = {"kind": "path", "selector": "/srv/du-lieu-quan-trong", "ops": "delete", "note": "dữ liệu khách", "next": "/permissions"}
    r = await env.post("/do/perm_protect_add", form)
    assert r.status_code == 200 and "Thêm tài nguyên bảo vệ" in r.text  # xác nhận 1 bước
    r = await env.post("/do/perm_protect_add", {**form, "confirm": "yes"})
    assert r.status_code == 303
    res = [x for x in hub.guard.policy()["resources"] if x["selector"] == "/srv/du-lieu-quan-trong"]
    assert res and res[0]["ops_protected"] == ["delete"] and not res[0]["builtin"]
    rid = res[0]["id"]
    # integrity: ghi hợp lệ của daemon → không bị coi là tamper (không khôi phục mất mục vừa thêm)
    hub.integrity.check()
    assert any(x["id"] == rid for x in hub.guard.policy()["resources"])
    r = await env.client.get("/permissions")
    assert f'id="prot-{rid}"' in r.text and "/do/perm_protect_rm" in r.text

    rm = {"id": rid, "selector": "/srv/du-lieu-quan-trong", "kind": "path", "next": "/permissions"}
    r = await env.post("/do/perm_protect_rm", rm)  # bước 1
    assert r.status_code == 200 and "Gỡ tài nguyên bảo vệ" in r.text
    r = await env.post("/do/perm_protect_rm", {**rm, "confirm": "yes"})  # bước 2: nêu hệ quả, chưa gỡ
    assert r.status_code == 200 and "Xác nhận lần 2" in r.text and "NỚI QUYỀN" in r.text and 'name="confirm2" value="yes"' in r.text
    assert any(x["id"] == rid for x in hub.guard.policy()["resources"])
    r = await env.post("/do/perm_protect_rm", {**rm, "confirm": "yes", "confirm2": "yes"})
    assert r.status_code == 303 and not any(x["id"] == rid for x in hub.guard.policy()["resources"])
    pc = audit_rows(env, "policy.changed")
    assert [json.loads(p["data"])["change"] for p in pc][-2:] == ["protect.add", "protect.rm"] and pc[-1]["channel"] == "web"
    # mục mặc định: không có nút gỡ; ép gọi → daemon từ chối, không đổi
    builtin = next(x for x in hub.guard.policy()["resources"] if x["builtin"])
    page = (await env.client.get("/permissions")).text
    row = page.split(f'id="prot-{builtin["id"]}"', 1)[1].split("</tr>", 1)[0]
    assert "perm_protect_rm" not in row
    r = await env.post("/do/perm_protect_rm", {"id": builtin["id"], "next": "/permissions", "confirm": "yes", "confirm2": "yes"})
    assert r.status_code == 303 and any(x["id"] == builtin["id"] for x in hub.guard.policy()["resources"])
    # gỡ tài nguyên qua thao tác luật luôn cho phép → từ chối
    r = await env.post("/do/perm_protect_rm", {"id": "aa_1234", "next": "/permissions", "confirm": "yes", "confirm2": "yes"})
    assert r.status_code == 303


async def test_agent_text_escaped(env):
    """Ghi chú tài nguyên / preview luật là dữ liệu — luôn autoescape."""
    await env.login()
    env.hub.guard.protect_add({"kind": "path", "selector": "/tmp/x", "note": "<script>alert(1)</script>"})
    r = await env.client.get("/permissions")
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
