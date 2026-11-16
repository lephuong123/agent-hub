"""Ngân sách cỡ system prompt: prompt điều phối viên từng nhảy 21,9k → 36,6k ký tự trong một commit (khối CLI) và vượt giới hạn
dòng lệnh Windows (WinError 206). Test này bắt phình ở CI; `doctor` + log cảnh báo bắt ở máy người dùng."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from cfharness import templates
from cfharness.browser import prompt_block
from cfharness.hub import Hub
from cfharness.learning.runtime import system_prompt_for
from cfharness.manager.cli_ref import cli_reference
from cfharness.prompt import SYSTEM_PROMPT_WARN_CHARS, build_system_prompt, latest_prompts, prompt_sections, size_summary
from cfharness.runtime import FakeRuntime
from cfharness.runtime.claude_cli import WINDOWS_CMDLINE_MAX

# Phần TĨNH của prompt điều phối viên (quy ước + Browser + SOUL template + Response style + CLI). Đo 2026-11-16: 24.431 ký tự.
# Muốn nâng: cắt chỗ khác trước, hoặc nâng có lý do trong commit — phần động (memory, user profile, skills) còn cộng thêm ~5–10k.
MANAGER_STATIC_BUDGET = 26000


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    yield h
    h.store.close()


def _manager_static_prompt() -> str:
    _, soul = templates.render("manager", "dieu-phoi", {"name": "Điều phối viên"})
    browser = max(("chrome", "auto", "playwright", "none"), key=lambda b: len(prompt_block(b) or ""))
    return build_system_prompt("dieu-phoi", soul, [], browser=browser, cli_ref=cli_reference())


def test_manager_static_prompt_within_budget():
    text = _manager_static_prompt()
    assert len(text) <= MANAGER_STATIC_BUDGET, f"prompt tĩnh điều phối viên phình: {size_summary(text, top=5)}"
    assert len(cli_reference()) < 9000


def test_warn_threshold_below_windows_cmdline_limit():
    assert MANAGER_STATIC_BUDGET < SYSTEM_PROMPT_WARN_CHARS < WINDOWS_CMDLINE_MAX


def test_prompt_sections_split_headers_and_banners():
    sep = "═" * 10
    text = f"# A\naaaa\n\n# B\nbb\n\n{sep}\nMEMORY (your notes) [95% — 1/2 chars]\n{sep}\nmmm\n"
    secs = dict(prompt_sections(text))
    assert set(secs) == {"A", "B", "MEMORY (your notes)"}
    assert sum(secs.values()) == len(text)
    assert size_summary(text, top=1).startswith(f"{len(text)} ký tự; lớn nhất: ")


def test_large_prompt_logs_warning(hub, caplog):
    hub.agents.create({"id": "big", "name": "Big"})
    agent = hub.agents.get_row("big")
    (Path(agent["home_path"]) / "SOUL.md").write_text("# Persona\n" + "x" * 500, encoding="utf-8")
    hub.config.set("runtime.claude.system_prompt_warn_chars", 100)
    with caplog.at_level(logging.WARNING, logger="cfharness.learning"):
        system_prompt_for(hub, {}, agent)
    assert any("system prompt của big vượt ngưỡng 100" in r.getMessage() for r in caplog.records)
    hub.config.set("runtime.claude.system_prompt_warn_chars", 10**6)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="cfharness.learning"):
        system_prompt_for(hub, {}, agent)
    assert not any("vượt ngưỡng" in r.getMessage() for r in caplog.records)


def test_latest_prompts_reads_newest_per_active_agent(tmp_path):
    (tmp_path / "prompts").mkdir()
    for h, body in (("old", "o" * 10), ("new", "n" * 40), ("gone", "g")):
        (tmp_path / "prompts" / f"{h}.md").write_text(body, encoding="utf-8")
    c = sqlite3.connect(tmp_path / "hub.db")
    c.execute("CREATE TABLE agents (id TEXT, deleted_at TEXT, archived_at TEXT)")
    c.execute("CREATE TABLE runs (agent_id TEXT, system_prompt_hash TEXT, queued_at TEXT)")
    c.executemany("INSERT INTO agents VALUES (?,?,?)", [("m", None, None), ("d", "2026-01-01", None)])
    c.executemany("INSERT INTO runs VALUES (?,?,?)", [("m", "old", "2026-11-01"), ("m", "new", "2026-11-02"), ("m", None, "2026-11-03"),
                                                      ("d", "gone", "2026-11-02")])
    c.commit()
    c.close()
    assert latest_prompts(tmp_path) == [("m", "n" * 40)]
    assert latest_prompts(tmp_path / "missing") == []
