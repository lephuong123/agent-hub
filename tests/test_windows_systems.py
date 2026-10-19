"""Hệ thống ngoài hub trên Windows: backend Task Scheduler (systems/taskscheduler.py) — đọc task, trạng thái, tạm dừng/bật lại/khởi động lại.

PowerShell và schtasks là bản GIẢ (`FakeTaskScheduler`) — test không bao giờ gọi Task Scheduler thật. Chạy được trên mọi hệ điều hành
(đường dẫn trong task dựng từ tmp_path nên khớp `os.sep` của máy chạy test).
"""

from __future__ import annotations

import base64
import json

import pytest

from cfharness.errors import HubError
from cfharness.systems import service as svc
from cfharness.systems.launchd import Launchctl, job_paths
from cfharness.systems.taskscheduler import TaskScheduler, _schedule, parse_task, split_args
from m7_fake import close, make_daemon, rpc


class FakeTaskScheduler:
    """`powershell -EncodedCommand …` → JSON danh sách task; `schtasks /End|/Change|/Run /TN …` → đổi trạng thái."""

    def __init__(self, tasks: list[dict]) -> None:
        self.tasks = {t["path"]: t for t in tasks}
        self.calls: list[list[str]] = []
        self.fail_run: set[str] = set()

    def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[0] == "powershell":
            script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
            assert "Get-ScheduledTask" in script and "\\Microsoft\\*" in script
            return 0, "\ufeff" + json.dumps(list(self.tasks.values())), ""
        assert argv[0] == "schtasks"
        op, tn = argv[1], argv[argv.index("/TN") + 1]
        t = self.tasks.get(tn)
        if t is None:
            return 1, "", "ERROR: The system cannot find the file specified."
        if op == "/End":
            if t["state"] != "Running":
                return 1, "", "ERROR: task is not running"
            t["state"] = "Ready"
            return 0, "SUCCESS", ""
        if op == "/Change":
            t["state"] = "Disabled" if argv[-1] == "/DISABLE" else "Ready"
            return 0, "SUCCESS", ""
        if op == "/Run":
            if tn in self.fail_run or t["state"] == "Disabled":
                return 1, "", "ERROR: cannot run"
            t["state"] = "Running"
            return 0, "SUCCESS", ""
        return 64, "", "unknown"


def _task(path: str, exe: str, args: str = "", cwd: str = "", triggers: list | None = None, state: str = "Ready", mine: bool = True,
          last: int | None = 0) -> dict:
    return {"path": path, "mine": mine, "state": state, "last": last, "actions": [{"exe": exe, "args": args, "cwd": cwd}],
            "triggers": triggers or []}


@pytest.fixture
def world(tmp_path):
    ws = tmp_path / "WorkSpace"
    ws.mkdir()
    a = ws / "workflowswebapp"
    (a / "scripts").mkdir(parents=True)
    (a / "package.json").write_text("{}")
    (a / "README.md").write_text("# Workflows web app\n")
    b = ws / "bot-khac"
    b.mkdir()
    tasks = [
        _task("\\CF\\workflows web", "node.exe", "server.js", cwd=str(a), triggers=[{"kind": "MSFT_TaskLogonTrigger", "enabled": True}],
              state="Running", last=0x41301),
        _task("\\CF\\workflows-sync", "C:\\Program Files\\nodejs\\node.exe", f'"{a / "scripts" / "sync.js"}" --full',
              triggers=[{"kind": "MSFT_TaskTimeTrigger", "start": "2026-01-01T08:00:00", "every": "PT15M", "enabled": True}], last=1),
        _task("\\bot-khac daily", str(b / "run.cmd"), triggers=[{"kind": "MSFT_TaskDailyTrigger", "start": "2026-01-01T07:30:00+07:00"}]),
        _task("\\CF\\hub", "cfharness.exe", "daemon start", cwd=str(a)),  # job của chính hub
        _task("\\Other user task", "node.exe", cwd=str(a), mine=False),  # task của user khác
        _task("\\Outside", "C:\\Tools\\x.exe", triggers=[{"kind": "MSFT_TaskBootTrigger"}]),  # ngoài gốc
    ]
    return {"ws": ws, "a": a, "fake": FakeTaskScheduler(tasks)}


@pytest.fixture
async def env(hub_home, world):
    d = await make_daemon(hub_home)
    d.hub.config.set("systems.roots", [str(world["ws"])])
    d.hub.systems._lc = TaskScheduler(runner=world["fake"], cache_s=0)
    yield d, world
    await close(d)


# ------------------------------------------------------------------ thuần
def test_schedule_and_parse():
    assert _schedule([{"kind": "MSFT_TaskTimeTrigger", "every": "PT15M"}]) == ("scheduled", "mỗi 15 phút")
    assert _schedule([{"kind": "MSFT_TaskDailyTrigger", "start": "2026-01-01T08:05:00"}]) == ("scheduled", "lúc 08:05")
    assert _schedule([{"kind": "MSFT_TaskWeeklyTrigger", "start": "2026-01-01T09:00:00", "days": 0b0000011}]) == ("scheduled", "lúc 09:00 thứ CN,2")
    assert _schedule([{"kind": "MSFT_TaskLogonTrigger"}])[0] == "service"
    assert _schedule([{"kind": "MSFT_TaskBootTrigger"}])[0] == "service"
    assert _schedule([]) == ("on_demand", "chỉ khi được gọi")
    assert _schedule([{"kind": "MSFT_TaskTimeTrigger", "every": "PT1H", "enabled": False}]) == ("on_demand", "chỉ khi được gọi")
    assert split_args('"C:\\a b\\x.js" --full') == ["C:\\a b\\x.js", "--full"]
    j = parse_task(_task("\\CF\\x", "C:\\n\\node.exe", '"C:\\WorkSpace\\app\\s.js" -v', cwd='"C:\\WorkSpace\\app"'))
    assert j["label"] == "CF\\x" and j["plist"] == "\\CF\\x" and j["cwd"] == "C:\\WorkSpace\\app"
    assert j["program"] == ["C:\\n\\node.exe", "C:\\WorkSpace\\app\\s.js", "-v"]
    assert "C:\\WorkSpace\\app\\s.js" in job_paths(j)
    assert parse_task(_task("\\CF\\x", "a.exe", mine=False)) is None
    assert parse_task(_task("\\..\\x", "a.exe")) is None


def test_label_validation():
    ok = TaskScheduler.label_ok
    assert ok("CF\\workflows web") and ok("{4abf-dc69}") and ok("OneDrive Startup Task-S-1-12-1")
    for bad in ("", "..\\x", "a/b", 'a"b', "a\nb", "\\leading", "C:x"):
        assert not ok(bad), bad
    assert Launchctl.label_ok("com.vn2.web") and not Launchctl.label_ok("CF\\x")


def test_state_and_disabled(world):
    f = world["fake"]
    f.tasks["\\bot-khac daily"]["state"] = "Disabled"
    ts = TaskScheduler(runner=f, cache_s=0)
    st = ts.state()
    assert st["CF\\workflows web"] == {"loaded": True, "pid": None, "last_exit": None, "running": True}  # 0x41301 = đang chạy, không lỗi
    assert st["CF\\workflows-sync"]["last_exit"] == 1 and not st["CF\\workflows-sync"]["running"]
    assert "bot-khac daily" not in st and ts.disabled() == {"bot-khac daily"}
    assert "Other user task" not in {j["label"] for j in ts.jobs()}
    assert all(not k.startswith("_") for j in ts.jobs() for k in j)


def test_cache_and_invalidation(world):
    f = world["fake"]
    ts = TaskScheduler(runner=f, cache_s=60)
    ts.state(), ts.disabled(), ts.jobs()
    assert sum(c[0] == "powershell" for c in f.calls) == 1
    ts.pause("CF\\workflows-sync")
    assert ts.disabled() == {"CF\\workflows-sync"}  # ghi xoá cache → đọc lại
    assert sum(c[0] == "powershell" for c in f.calls) == 2


def test_make_backend_by_platform(monkeypatch):
    from cfharness.config import DEFAULTS

    class Cfg:
        def get(self, key):
            cur = DEFAULTS
            for k in key.split("."):
                cur = cur[k]
            return cur

    monkeypatch.setattr(svc, "IS_WINDOWS", True)
    b = svc.make_backend(Cfg())
    assert isinstance(b, TaskScheduler) and b.schtasks == "schtasks" and b.powershell == "powershell" and b.timeout == 15
    monkeypatch.setattr(svc, "IS_WINDOWS", False)
    b = svc.make_backend(Cfg())
    assert isinstance(b, Launchctl) and b.bin == "/bin/launchctl"


# ------------------------------------------------------------------ qua service + RPC
async def test_scan_and_list(env):
    d, w = env
    r = await rpc(d, "system.scan", {"apply": True})
    found = {s["id"]: s for s in r["found"]}
    assert set(found) == {"workflowswebapp", "bot-khac"}
    assert sorted(found["workflowswebapp"]["jobs"]) == ["CF\\workflows web", "CF\\workflows-sync"]  # job của hub + user khác bị loại
    assert found["bot-khac"]["jobs"] == ["bot-khac daily"]
    rows = {s["id"]: s for s in await rpc(d, "system.list")}
    st = {j["label"]: (j["status"], j["kind"]) for j in rows["workflowswebapp"]["jobs"]}
    assert st == {"CF\\workflows web": ("running", "service"), "CF\\workflows-sync": ("error", "scheduled")}


async def test_pause_resume_restart(env):
    d, w = env
    f = w["fake"]
    await rpc(d, "system.scan", {"apply": True})
    r = await rpc(d, "system.job", {"label": "CF\\workflows web", "action": "pause", "reason": "bảo trì"})
    assert r["status"] == "paused" and r["disabled"] and r["paused_by"]["reason"] == "bảo trì"
    assert ["schtasks", "/End", "/TN", "\\CF\\workflows web"] in f.calls
    assert ["schtasks", "/Change", "/TN", "\\CF\\workflows web", "/DISABLE"] in f.calls
    with pytest.raises(HubError, match="tạm dừng"):
        await rpc(d, "system.job", {"label": "CF\\workflows web", "action": "restart"})
    r = await rpc(d, "system.job", {"label": "CF\\workflows web", "action": "resume"})
    assert r["status"] == "running" and "chạy ngay" in r["detail"]  # task khi đăng nhập = service → /Run ngay như bootstrap
    r = await rpc(d, "system.job", {"label": "CF\\workflows-sync", "action": "pause"})  # không đang chạy: /End lỗi vẫn ổn
    r = await rpc(d, "system.job", {"label": "CF\\workflows-sync", "action": "resume"})
    assert r["detail"] == "đã bật task (/ENABLE)" and r["status"] == "error"  # task hẹn giờ: không chạy ngay, giữ last_exit cũ
    r = await rpc(d, "system.job", {"label": "CF\\workflows-sync", "action": "restart"})
    assert r["status"] == "running"
    f.fail_run.add("\\CF\\workflows-sync")
    with pytest.raises(HubError, match="schtasks /Run lỗi"):
        await rpc(d, "system.job", {"label": "CF\\workflows-sync", "action": "restart"})


async def test_rejects_outside_and_hub_tasks(env):
    d, w = env
    await rpc(d, "system.scan", {"apply": True})
    for lb in ("Outside", "CF\\hub", "Other user task", "..\\..\\x", "a/b"):
        with pytest.raises(HubError):
            await rpc(d, "system.job", {"label": lb, "action": "pause"})
    assert not any(c[0] == "schtasks" for c in w["fake"].calls)


async def test_add_rejects_drive_root_and_matches_case(env, tmp_path):
    d, w = env
    with pytest.raises(HubError):
        await rpc(d, "system.add", {"path": tmp_path.anchor or "/"})
    p = str(w["a"])
    if p[1:2] == ":":  # Windows: đường dẫn khác hoa/thường vẫn gán đúng task
        p = p.upper()
    s = await rpc(d, "system.add", {"path": p})
    assert sorted(j["label"] for j in s["jobs"]) == ["CF\\workflows web", "CF\\workflows-sync"]
