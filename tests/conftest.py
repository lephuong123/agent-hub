from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cfharness.oscompat import IS_WINDOWS
from cfharness.paths import HubPaths
from cfharness.runtime import default_fixture

FIXTURES = Path(__file__).parent / "fixtures"

# Đa instance P1: biến môi trường chỉ-cho-test (CFHARNESS_EXTRA_WORKSPACE_ROOTS…) chỉ có hiệu lực khi cờ này bật.
# Đặt ở mức module (os.environ) để daemon/tiến trình con mà test spawn kế thừa.
import os as _os

_os.environ["CFHARNESS_TESTING"] = "1"


@pytest.fixture(scope="session", autouse=True)
def _allow_pytest_tmp_workspaces(tmp_path_factory):
    """MGR-1: test đặt workspace dưới thư mục tạm của pytest (ngoài ~/Workspace mặc định sản phẩm).
    Cho phép gốc tạm này CHỈ trong test; không nới mặc định sản phẩm. Dùng cả biến môi trường để daemon tiến trình con
    (spawn_background) kế thừa — sản phẩm không bao giờ đặt biến này."""
    import os

    import cfharness.workspace as _ws

    base = str(tmp_path_factory.getbasetemp())
    _ws._EXTRA_ALLOWED_ROOTS.append(base)
    prev = os.environ.get("CFHARNESS_EXTRA_WORKSPACE_ROOTS")
    os.environ["CFHARNESS_EXTRA_WORKSPACE_ROOTS"] = base if not prev else prev + os.pathsep + base
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CFHARNESS_EXTRA_WORKSPACE_ROOTS", None)
        else:
            os.environ["CFHARNESS_EXTRA_WORKSPACE_ROOTS"] = prev
        try:
            _ws._EXTRA_ALLOWED_ROOTS.remove(base)
        except ValueError:
            pass


@pytest.fixture(autouse=True)
def _plain_cli_output(monkeypatch):
    """CLI in văn bản thuần khi không phải TTY; FORCE_COLOR của CI/terminal ngoài sẽ bật chế độ rich (ui/theme.py)
    và làm lệch các assert trên chuỗi thuần — test nào cần chế độ có kiểu tự đặt lại (tests/test_cli_design.py)."""
    for k in ("FORCE_COLOR", "CFHARNESS_PLAIN", "CFHARNESS_ASCII"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(scope="session", autouse=True)
def _hubs_registry(tmp_path_factory):
    """P2.4: registry hub (`~/.config/cfharness/hubs.json`) KHÔNG BAO GIỜ ghi vào home thật trong test (cả daemon tiến trình con)."""
    import os

    prev = os.environ.get("CFHARNESS_HUBS_REGISTRY")
    os.environ["CFHARNESS_HUBS_REGISTRY"] = str(tmp_path_factory.mktemp("hubs-registry") / "hubs.json")
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CFHARNESS_HUBS_REGISTRY", None)
        else:
            os.environ["CFHARNESS_HUBS_REGISTRY"] = prev


@pytest.fixture(scope="session", autouse=True)
def _admin_shared_root(tmp_path_factory):
    """Quản trị máy: cờ ESTOP admin / chính sách / admin-log đọc từ thư mục chung HẰNG SỐ (/Users/Shared/cfharness). Test (kể cả daemon
    tiến trình con) dùng thư mục tạm RỖNG — máy dev có cờ ESTOP thật không làm đỏ test (chỉ có hiệu lực khi CFHARNESS_TESTING=1)."""
    import os

    prev = os.environ.get("CFHARNESS_TEST_SHARED_ROOT")
    os.environ["CFHARNESS_TEST_SHARED_ROOT"] = str(tmp_path_factory.mktemp("admin-shared"))
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CFHARNESS_TEST_SHARED_ROOT", None)
        else:
            os.environ["CFHARNESS_TEST_SHARED_ROOT"] = prev


class _Grp:
    def __init__(self, gid: int, mem: list[str]):
        self.gr_gid, self.gr_mem = gid, mem


@pytest.fixture(autouse=True)
def _not_machine_admin(monkeypatch):
    """Mặc định tiến trình test KHÔNG thuộc nhóm cfharness-admin/cfharness-users (máy dev có thể có nhóm thật). Test cần quyền admin
    dùng fixture `as_admin`."""
    import cfharness.admin_control as ac

    monkeypatch.setattr(ac, "_GETGROUPS", lambda: [])
    monkeypatch.setattr(ac, "_GETGRNAM", lambda name: (_ for _ in ()).throw(KeyError(name)))


@pytest.fixture
def as_admin(monkeypatch):
    """Tiến trình test thuộc nhóm cfharness-admin (gid giả 4242). Trả hàm đặt thành viên nhóm: `as_admin(users=[...], admins=[...])`."""
    import cfharness.admin_control as ac

    groups = {ac.ADMIN_GROUP: _Grp(4242, []), ac.USERS_GROUP: _Grp(4243, [])}

    def getgrnam(name):
        if name not in groups:
            raise KeyError(name)
        return groups[name]

    monkeypatch.setattr(ac, "_GETGROUPS", lambda: [20, 4242])
    monkeypatch.setattr(ac, "_GETGRNAM", getgrnam)

    def set_members(users=None, admins=None):
        if users is not None:
            groups[ac.USERS_GROUP].gr_mem = list(users)
        if admins is not None:
            groups[ac.ADMIN_GROUP].gr_mem = list(admins)

    return set_members


@pytest.fixture(autouse=True)
def _no_embedding_model(monkeypatch):
    """Graph memory: test KHÔNG BAO GIỜ nạp/tải model embedding thật (bge-m3 ~2.3 GB) kể cả khi máy dev đã cài extra `cfharness[vector]`.
    Test cần kênh vector gán embedder giả: `hub.graph_embedder = FakeEmbedder()` (get_embedder ưu tiên thuộc tính này)."""
    monkeypatch.setenv("CFHARNESS_GRAPH_VECTOR", "off")


@pytest.fixture
def hub_home(tmp_path, monkeypatch) -> HubPaths:
    home = tmp_path / "hub"
    monkeypatch.setenv("CFHARNESS_HOME", str(home))
    monkeypatch.delenv("CFHARNESS_SOCKET", raising=False)
    paths = HubPaths.resolve()
    paths.ensure()
    return paths


@pytest.fixture
def sample_lines() -> list[str]:
    return [ln for ln in default_fixture().read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture
def budget_fixture() -> Path:
    return FIXTURES / "claude-budget-exceeded.jsonl"


def fake_cli(tmp_path: Path, script: Path, name: str = "claude") -> Path:
    """`claude` giả chạy `script` bằng python hiện tại. POSIX: shim `#!/bin/sh` tên `name`; Windows: chính file `.py`
    (runtime chạy `.py` bằng python — `oscompat.exec_argv`; shell script/`.cmd` làm hỏng argv có dấu nháy/xuống dòng)."""
    if IS_WINDOWS:
        return script
    p = tmp_path / name
    p.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    p.chmod(0o755)
    return p
