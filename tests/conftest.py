from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.paths import HubPaths
from agent_hub.runtime import default_fixture

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def hub_home(tmp_path, monkeypatch) -> HubPaths:
    home = tmp_path / "hub"
    monkeypatch.setenv("AGENT_HUB_HOME", str(home))
    monkeypatch.delenv("AGENT_HUB_SOCKET", raising=False)
    paths = HubPaths.resolve()
    paths.ensure()
    return paths


@pytest.fixture
def sample_lines() -> list[str]:
    return [ln for ln in default_fixture().read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture
def budget_fixture() -> Path:
    return FIXTURES / "claude-budget-exceeded.jsonl"
