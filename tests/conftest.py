from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.paths import HubPaths
from agent_hub.runtime import default_fixture

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _allow_pytest_tmp_workspaces(tmp_path_factory):
    """MGR-1: test đặt workspace dưới thư mục tạm của pytest (ngoài ~/Workspace mặc định sản phẩm).
    Cho phép gốc tạm này CHỈ trong test; không nới mặc định sản phẩm. Dùng cả biến môi trường để daemon tiến trình con
    (spawn_background) kế thừa — sản phẩm không bao giờ đặt biến này."""
    import os

    import agent_hub.workspace as _ws

    base = str(tmp_path_factory.getbasetemp())
    _ws._EXTRA_ALLOWED_ROOTS.append(base)
    prev = os.environ.get("AGENT_HUB_EXTRA_WORKSPACE_ROOTS")
    os.environ["AGENT_HUB_EXTRA_WORKSPACE_ROOTS"] = base if not prev else prev + os.pathsep + base
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("AGENT_HUB_EXTRA_WORKSPACE_ROOTS", None)
        else:
            os.environ["AGENT_HUB_EXTRA_WORKSPACE_ROOTS"] = prev
        try:
            _ws._EXTRA_ALLOWED_ROOTS.remove(base)
        except ValueError:
            pass


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
