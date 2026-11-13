"""Khối "Response style" (câu trả lời kiểu Claude chat) + ghi chú định dạng theo kênh — plan-chat-ui-claude.md P0-1."""

from __future__ import annotations

from cfharness.manager import prompts as mp
from cfharness.prompt import HUB_CONVENTIONS, RESPONSE_STYLE, build_system_prompt, prompt_hash


def test_response_style_block_placed_after_soul_and_charter_before_knowledge():
    text = build_system_prompt("a1", "I am the persona.", ["# Memory\nRepo uses pnpm."], charter="# Team charter\nBe precise.")
    i_conv, i_soul = text.index("cfharness conventions"), text.index("I am the persona.")
    i_ch, i_style, i_mem = text.index("Be precise."), text.index("# Response style"), text.index("Repo uses pnpm.")
    assert i_conv < i_soul < i_ch < i_style < i_mem
    assert text.count("# Response style") == 1


def test_response_style_without_soul_or_knowledge_still_present_and_stable():
    a = build_system_prompt("a1", "", [])
    assert "# Response style" in a
    assert prompt_hash(a) == prompt_hash(build_system_prompt("a1", "", []))  # ổn định → giữ prompt cache


def test_no_plain_text_rule_and_key_style_rules_present():
    assert "plain-text" not in HUB_CONVENTIONS
    for must in ("Lead with the answer", "fenced blocks with a language tag", "no internal ids", "Never invent results",
                 "structured output", "Vietnamese"):
        assert must in RESPONSE_STYLE, must
    # NO_ACTION là tín hiệu điều khiển của nhịp, không được bị khối style cấm
    assert "NO_ACTION" not in RESPONSE_STYLE


def test_chat_prompt_format_note_per_channel():
    tg = mp.chat_prompt("telegram:123", "c1", "hi")
    sl = mp.chat_prompt("slack:D123", "c1", "hi")
    web = mp.chat_prompt("web:x", "c1", "hi")
    assert mp.TELEGRAM_FORMAT_NOTE in tg and mp.SLACK_FORMAT_NOTE not in tg
    assert mp.SLACK_FORMAT_NOTE in sl
    assert mp.TELEGRAM_FORMAT_NOTE not in web and mp.SLACK_FORMAT_NOTE not in web
    for p in (tg, sl, web):
        assert "Response style" in p and "not ids" in p
    assert "Vietnamese, short." not in web


def test_result_only_still_applies_and_tick_mentions_style():
    p = mp.chat_prompt("slack:C123", "c1", "hi", result_only=True)
    assert mp.RESULT_ONLY_NOTE in p
    t = mp.tick_prompt([], dropped=0, reason="interval", open_work=0)
    assert "Response style" in t and "NO_ACTION" in t
