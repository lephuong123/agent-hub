"""Khối "cfharness CLI reference" trong system prompt của điều phối viên (manager/cli_ref.py): sinh từ parser thật, chỉ điều phối viên."""

from __future__ import annotations

import shlex

import pytest

from cfharness.cli import build_parser
from cfharness.hub import Hub
from cfharness.learning.runtime import system_prompt_for
from cfharness.manager.cli_ref import cli_reference
from cfharness.prompt import build_system_prompt
from cfharness.runtime import FakeRuntime


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    yield h
    h.store.close()


def _commands() -> list[str]:
    body = cli_reference().split("```text\n", 1)[1].rsplit("```", 1)[0]
    return [ln for ln in body.splitlines() if ln.strip()]


def test_reference_lists_real_commands_and_rules():
    ref = cli_reference()
    lines = _commands()
    assert "cfharness runs list --agent X --status X --limit X" in lines  # lệnh đúng (trước đây điều phối viên bịa `run list`)
    assert not any(ln.startswith("cfharness run list") for ln in lines)
    assert "cfharness account use <id>" in lines and "cfharness schedule pause <id> --reason X" in lines
    assert not any(ln.startswith("cfharness hook") for ln in lines)  # lệnh nội bộ
    for must in ("You cannot run them yourself", "fetch it with", "never guess a command or flag", "--help"):
        assert must in ref, must
    assert len(ref) < 16000  # giữ khối gọn (prompt mỗi lượt)


def test_every_listed_command_path_parses():
    """Mọi đường lệnh trong khối (bỏ cờ/tham số) phải là lệnh con có thật của parser — khối không bao giờ lệch CLI."""
    p = build_parser()
    for ln in _commands():
        words = []
        for w in shlex.split(ln)[1:]:
            if w.startswith(("-", "<", "[", "…")):
                break
            words.append(w)
        sub = p
        for w in words:
            sp = next(a for a in sub._actions if a.__class__.__name__ == "_SubParsersAction")
            assert w in sp.choices, ln
            sub = sp.choices[w]


def test_block_after_response_style_and_hash_stable():
    a = build_system_prompt("m", "soul", ["# Memory\nx"], cli_ref=cli_reference())
    assert a.index("# Response style") < a.index("# cfharness CLI reference") < a.index("# Memory")
    assert a == build_system_prompt("m", "soul", ["# Memory\nx"], cli_ref=cli_reference())
    assert "# cfharness CLI reference" not in build_system_prompt("m", "soul", [])


def test_only_manager_gets_block(hub, monkeypatch):
    hub.agents.create({"id": "boss", "name": "Boss"})
    hub.agents.create({"id": "coder", "name": "Coder"})
    monkeypatch.setattr(hub.manager, "is_manager", lambda aid: aid == "boss")
    boss = hub.agents.get_row("boss")
    coder = hub.agents.get_row("coder")
    assert "# cfharness CLI reference" in system_prompt_for(hub, {}, boss)[0]
    assert "# cfharness CLI reference" not in system_prompt_for(hub, {}, coder)[0]
