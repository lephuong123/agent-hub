""""Nhận thông báo gì" theo nhóm + nhãn (notify_events.py, web/notify_pages.py — plan-chat-setup-web PR4).

Kiểm: mọi event trong DEFAULTS của hai kênh + mọi event notifier vẽ được đều có nhãn và nhóm (thêm event quên nhãn → đỏ); preset
"Khuyên dùng" = đúng mặc định; `preset_of` khứ hồi; `config.set` từ chối event lạ; web: chọn preset ghi CẢ hai khoá (audit kênh
web), tuỳ chỉnh theo nhóm/mục, event lạ bị từ chối, trang hiện nhãn thay mã thô, thiếu CSRF/khoá phiên → 403.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cfharness import notify_events as NE
from cfharness.config import DEFAULTS, HubConfig
from cfharness.runtime import FakeRuntime
from test_web import ISOLATED, _start, _stop, audit_rows  # noqa: F401 — dùng lại khung test web

SRC = Path(__file__).resolve().parent.parent / "src" / "cfharness"


def _rendered_events() -> set[str]:
    """Loại event mà notifier Slack/Telegram có nhánh vẽ riêng (quét mã nguồn: `ev.type == "…"`, `etype == "…"`, bộ `in (…)`)."""
    out: set[str] = set()
    for rel in ("notify/slack.py", "notify/telegram.py"):
        text = (SRC / rel).read_text(encoding="utf-8")
        out |= set(re.findall(r'(?:ev\.type|etype) == "([a-z_.]+)"', text))
        for grp in re.findall(r"ev\.type in \(([^)]*)\)", text):
            out |= set(re.findall(r'"([a-z_.]+)"', grp))
    from cfharness.notify.telegram import APPROVAL_EVENTS, GOLDEN_SUGGESTION_EVENT
    from cfharness.web_events import KINDS

    return out | set(APPROVAL_EVENTS) | {GOLDEN_SUGGESTION_EVENT} | set(KINDS)


@pytest.mark.parametrize("ch", NE.CHANNELS)
def test_every_default_event_has_label_and_group(ch):
    for e in DEFAULTS["notify"][ch]["events"]:
        assert NE.EVENT_LABELS.get(e), f"{e} ({ch}) thiếu nhãn — thêm vào notify_events._GROUPS"
        assert NE.GROUP_OF.get(e) in NE.EVENT_GROUPS, e


def test_every_renderable_event_has_label():
    ev = _rendered_events()
    assert "approval.open" in ev and "filehub.lane.roots" in ev and "web.login" in ev  # bộ quét còn chạy
    missing = sorted(e for e in ev if e not in NE.EVENT_LABELS)
    assert not missing, f"notifier gửi được nhưng chưa có nhãn: {missing}"


def test_labels_vietnamese_and_groups_cover_exactly():
    assert len(set(NE.KNOWN_EVENTS)) == len(NE.KNOWN_EVENTS)
    assert set(NE.GROUP_OF) == set(NE.EVENT_LABELS)
    assert [g for g in NE.EVENT_GROUPS] == ["needs_you", "errors", "learning", "security", "filehub"]
    for e, label in NE.EVENT_LABELS.items():
        assert label and label != e and "." not in label, e


@pytest.mark.parametrize("ch", NE.CHANNELS)
def test_recommended_is_exactly_default(ch):
    assert NE.events_for("recommended", ch) == DEFAULTS["notify"][ch]["events"]
    assert NE.preset_of(DEFAULTS["notify"][ch]["events"], ch) == "recommended"


@pytest.mark.parametrize("ch", NE.CHANNELS)
@pytest.mark.parametrize("preset", ["needs_me", "recommended", "all"])
def test_preset_round_trip(ch, preset):
    evs = NE.events_for(preset, ch)
    got = NE.preset_of(evs, ch)
    assert set(NE.events_for(got, ch)) == set(evs)  # Telegram: "Tất cả" = mặc định → cùng tập
    assert NE.preset_of(list(reversed(evs)) + evs[:1], ch) == got  # thứ tự/trùng không đổi preset
    both = {c: NE.events_for(preset, c) for c in NE.CHANNELS}
    assert NE.shared_preset(both) == preset  # lựa chọn chung khứ hồi đúng


def test_preset_of_custom_and_events_for_custom_raises():
    assert NE.preset_of(["approval.open", "budget.warning"]) == "custom"
    assert NE.preset_of([]) == "custom"
    with pytest.raises(ValueError):
        NE.events_for("custom")
    with pytest.raises(ValueError):
        NE.events_for("all", "email")


def test_shared_preset_mixed_is_custom():
    assert NE.shared_preset({"slack": NE.events_for("needs_me", "slack"), "telegram": NE.events_for("all", "telegram")}) == "custom"
    assert NE.shared_preset({c: list(DEFAULTS["notify"][c]["events"]) for c in NE.CHANNELS}) == "recommended"


def test_needs_me_and_all():
    assert set(NE.events_for("needs_me")) == {"approval.open", "approval.waiting_human", "approval.reminder", "approval.escalated", "team.inbox_new"}
    assert NE.events_for("all") == list(NE.KNOWN_EVENTS)


def test_from_selection_group_plus_items_and_rejects_unknown():
    out = NE.from_selection(["errors"], ["guard.blocked", "budget.warning"])
    assert out == ["run.repeated_failure", "budget.warning", "budget.exceeded", "guard.blocked"]  # thứ tự chuẩn, không trùng
    with pytest.raises(ValueError):
        NE.from_selection(["khong_co"])
    with pytest.raises(ValueError):
        NE.from_selection([], ["run.completed"])


def test_grouped_keeps_unknown_selected_in_other():
    gs = NE.grouped(["approval.open", "tu.dat.tay"])
    assert gs[0]["n"] == 1 and not gs[0]["all"]
    assert gs[-1]["id"] == "other" and gs[-1]["entries"][0]["event"] == "tu.dat.tay"


def test_config_set_rejects_unknown_event(tmp_path):
    c = HubConfig(tmp_path / "config.yaml")
    c.set("notify.slack.events", ["approval.open"])
    assert c.get("notify.slack.events") == ["approval.open"]
    for key, value in (("notify.slack.events", ["approval.open", "run.completed"]), ("notify.telegram.events", "approval.open"),
                       ("notify.telegram", {"events": ["x.y"]}), ("notify", {"slack": {"events": ["x.y"]}})):
        with pytest.raises(ValueError):
            c.set(key, value)
    c.set("notify.telegram", {"events": NE.events_for("all", "telegram")})  # nhánh cha hợp lệ vẫn ghi được


# ------------------------------------------------------------------------------------------------------------------ web
@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    await e.login()
    yield e
    await _stop(e)


def _ev(env, ch):
    return env.hub.config.get(f"notify.{ch}.events")


async def test_page_renders_labels_not_raw_codes(env):
    r = await env.client.get("/settings/notifications")
    assert r.status_code == 200
    html = r.text
    assert 'id="notify-events"' in html and 'action="/do/notify_events"' in html
    for label in ("Chỉ việc cần tôi", "Khuyên dùng", "Tất cả", "Tuỳ chỉnh", "Cần bạn", "Lỗi &amp; ngân sách", "Bảo mật", "Thư mục chung",
                  NE.EVENT_LABELS["approval.open"], NE.EVENT_LABELS["filehub.conflict"]):
        assert label in html, label
    assert re.search(r'name="preset" value="recommended" checked', html)  # mặc định = Khuyên dùng
    assert "<code>approval.open</code>" not in html  # multi Nâng cao cũng hiện nhãn, không mã thô
    assert "Slack và Telegram đang nhận khác nhau" not in html  # mặc định hai kênh cùng preset


async def test_preset_sets_both_channels_with_audit(env):
    r = await env.post("/do/notify_events", {"preset": "needs_me", "next": "/settings/notifications"})
    assert r.status_code == 303, r.text
    want = NE.events_for("needs_me")
    assert _ev(env, "slack") == want and _ev(env, "telegram") == want
    rows = [x for x in audit_rows(env, "config.set") if x["channel"] == "web"]
    assert len(rows) >= 2
    await env.post("/do/notify_events", {"preset": "recommended"})
    assert _ev(env, "slack") == DEFAULTS["notify"]["slack"]["events"]
    assert _ev(env, "telegram") == DEFAULTS["notify"]["telegram"]["events"]
    await env.post("/do/notify_events", {"preset": "all"})
    assert _ev(env, "slack") == list(NE.KNOWN_EVENTS) == _ev(env, "telegram")
    html = (await env.client.get("/settings/notifications")).text
    assert re.search(r'name="preset" value="all" checked', html)


async def test_custom_groups_and_items(env):
    r = await env.post("/do/notify_events", {"preset": "custom", "grp": "errors", "ev": ["approval.open", "guard.blocked"]})
    assert r.status_code == 303
    want = ["approval.open", "run.repeated_failure", "budget.warning", "budget.exceeded", "guard.blocked"]
    assert _ev(env, "slack") == want == _ev(env, "telegram")
    html = (await env.client.get("/settings/notifications")).text
    assert re.search(r'name="preset" value="custom" checked', html)
    assert re.search(r'name="grp" value="errors" data-nev-grp="errors" checked', html)


async def test_unknown_event_or_preset_rejected(env):
    before = (_ev(env, "slack"), _ev(env, "telegram"))
    for data in ({"preset": "custom", "ev": "run.completed"}, {"preset": "custom", "grp": "khong_co"}, {"preset": "moi_thu"}, {}):
        r = await env.post("/do/notify_events", {"next": "/settings/notifications", **data})
        assert r.status_code == 303
        assert (_ev(env, "slack"), _ev(env, "telegram")) == before, data
    # đường config_set chung (Nâng cao) cũng bị daemon từ chối event lạ
    await env.post("/do/config_set", {"key": "notify.slack.events", "value": ["approval.open", "x.lạ"], "confirm": "yes"})
    assert _ev(env, "slack") == before[0]


async def test_per_channel_advanced_still_works(env):
    r = await env.post("/do/config_set", {"key": "notify.slack.events", "value": ["approval.open"], "confirm": "yes"})
    assert r.status_code == 303
    assert _ev(env, "slack") == ["approval.open"]
    assert _ev(env, "telegram") == DEFAULTS["notify"]["telegram"]["events"]
    html = (await env.client.get("/settings/notifications")).text
    assert "Slack và Telegram đang nhận khác nhau" in html
    assert re.search(r'name="preset" value="custom" checked', html)


async def test_csrf_and_session_key_required(env):
    before = _ev(env, "slack")
    r = await env.client.post("/do/notify_events", data={"preset": "all"})
    assert r.status_code == 403
    r = await env.client.post("/do/notify_events", data={"preset": "all", "csrf": "sai", "k": env.key})
    assert r.status_code == 403
    r = await env.client.post("/do/notify_events", data={"preset": "all", "csrf": env.csrf})  # thiếu khoá phiên (WT-1)
    assert r.status_code == 403
    assert _ev(env, "slack") == before
