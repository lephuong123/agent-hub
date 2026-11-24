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

# F18: biến môi trường của một lượt chạy agent (khi pytest chạy BÊN TRONG một run cfharness) không được rò vào test —
# CFHARNESS_HOME trỏ tới hub thật, CFHARNESS_RUN_ID làm auth/hook coi tiến trình test là agent. Xoá ở mức module để daemon/tiến
# trình con test spawn cũng không kế thừa; fixture `_strip_run_env` xoá lại trước mỗi test (phòng test trước lỡ đặt).
RUN_ENV_VARS = (
    "CFHARNESS_HOME",
    "CFHARNESS_RUN_ID",
    "CFHARNESS_SOCKET",
    "CFHARNESS_MANAGER_KEY",
    "CFHARNESS_EVAL_RUN_ID",
    "CFHARNESS_OUTPUT_FILE",
    "CFHARNESS_OUTPUT_JSON",
    "CFHARNESS_CLIP_OUT",
    "AGENT_HUB_RUN_ID",  # tên cũ (names.LEGACY_ENV_RUN_ID) — auth vẫn đọc
    "AGENT_HUB_HOME",
)
for _k in RUN_ENV_VARS:
    _os.environ.pop(_k, None)


def pytest_configure(config):
    """F18: daemon (kể cả tiến trình con) nhận diện người gọi qua env KHỞI TẠO của pid client do kernel giữ — xoá os.environ không
    đủ. Nếu env khởi tạo của pytest còn biến của lượt agent bao ngoài → chạy lại chính pytest (execve) với env đã xoá, một lần."""
    if _os.environ.get("CFHARNESS_TEST_REEXEC") or not getattr(sys, "orig_argv", None):
        return
    import cfharness.auth as _auth

    try:
        kenv = _auth.proc_environ(_os.getpid()) or {}
    except Exception:  # pragma: no cover
        return
    if not any(k in kenv for k in RUN_ENV_VARS):
        return
    env = {k: v for k, v in _os.environ.items() if k not in RUN_ENV_VARS}
    env["CFHARNESS_TEST_REEXEC"] = "1"
    capman = config.pluginmanager.getplugin("capturemanager")
    if capman is not None:
        capman.stop_global_capturing()
    sys.stdout.flush()
    sys.stderr.flush()
    _os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], env)


@pytest.fixture(autouse=True)
def _strip_run_env(monkeypatch):
    """F18: mỗi test bắt đầu không có biến môi trường của lượt chạy agent (test cần thì tự `monkeypatch.setenv`)."""
    for k in RUN_ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    # Xoá os.environ KHÔNG đổi env khởi tạo mà kernel giữ (macOS KERN_PROCARGS2 / Linux /proc/<pid>/environ): daemon in-process
    # của test đọc env của CHÍNH pytest qua `auth.proc_environ` sẽ thấy CFHARNESS_RUN_ID của lượt agent bao ngoài và coi người gọi
    # là agent. Với pid của chính pytest, trả env hiện tại (đã xoá); tiến trình khác vẫn đọc thật.
    import cfharness.auth as _auth

    real = _auth.proc_environ

    def proc_environ(pid):
        if pid == _os.getpid():
            return dict(_os.environ)
        return real(pid)

    monkeypatch.setattr(_auth, "proc_environ", proc_environ)


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
def _claude_home_for_transfer(tmp_path_factory):
    """Hub clone PR3: `hub export|import` đọc/ghi session Claude Code ở `<claude home>/projects` — test KHÔNG BAO GIỜ đụng ~/.claude thật
    (thư mục tạm rỗng; chỉ có hiệu lực khi CFHARNESS_TESTING=1). Test nào cần thì monkeypatch biến này."""
    import os

    prev = os.environ.get("CFHARNESS_TEST_CLAUDE_HOME")
    os.environ["CFHARNESS_TEST_CLAUDE_HOME"] = str(tmp_path_factory.mktemp("claude-home"))
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CFHARNESS_TEST_CLAUDE_HOME", None)
        else:
            os.environ["CFHARNESS_TEST_CLAUDE_HOME"] = prev


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


@pytest.fixture(autouse=True)
def _no_audit_watch(monkeypatch):
    """L5: cảnh báo theo mẫu audit (audit_watch.py) TẮT cho bộ test chung — không chen event/tin Telegram/dòng audit `audit.alert` vào
    test đếm tin/dòng. `tests/test_audit_watch.py` bật lại (`monkeypatch.delenv`)."""
    monkeypatch.setenv("CFHARNESS_AUDIT_WATCH", "off")


@pytest.fixture(autouse=True)
def _no_connectors_auto(monkeypatch):
    """`connectors.auto` bật mặc định → mỗi run kiểm `claude auth status`; claude giả của bộ test không hiểu lệnh đó.
    TẮT cho bộ test chung; test connector bật lại (`monkeypatch.delenv("CFHARNESS_CONNECTORS_AUTO")`)."""
    monkeypatch.setenv("CFHARNESS_CONNECTORS_AUTO", "off")


@pytest.fixture(autouse=True)
def _fresh_ancestor_cache():
    """auth.cached_ancestors: cache chuỗi tổ tiên (pid + thời điểm khởi động) là RAM toàn tiến trình — mỗi test bắt đầu rỗng."""
    from cfharness import auth as _auth

    _auth._ANCESTOR_CACHE.clear()
    yield
    _auth._ANCESTOR_CACHE.clear()


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
