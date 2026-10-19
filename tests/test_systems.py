"""Hệ thống ngoài hub (systems/): nhận diện app + job launchd, đăng ký (chỉ người), tạm dừng/bật lại/khởi động lại job — người và điều phối viên.

`launchctl` là bản GIẢ (`FakeLaunchd`) — test không bao giờ gọi launchctl thật hay đụng ~/Library/LaunchAgents.
Ca an toàn của điều phối viên kiểm cả lớp 1 (tool hub-self) lẫn lớp 2 (gọi thẳng daemon với `Caller.manager`).
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from cfharness import auth
from cfharness.errors import BLOCKED, HubError
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore
from cfharness.systems.launchd import Launchctl, parse_plist
from cfharness.systems.service import slug
from m7_fake import close, make_daemon, manager_run, rpc, tool


class FakeLaunchd:
    """Mô phỏng launchctl miền gui/501: tập job đã nạp + tập disabled."""

    def __init__(self, loaded: dict[str, int | None]) -> None:
        self.loaded = dict(loaded)  # label → pid|None
        self.disabled: set[str] = set()
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        args = argv[1:]
        self.calls.append(args)
        cmd = args[0]
        if cmd == "list":
            rows = ["PID\tStatus\tLabel"] + [f"{pid if pid else '-'}\t0\t{lb}" for lb, pid in self.loaded.items()]
            return 0, "\n".join(rows) + "\n", ""
        if cmd == "print-disabled":
            return 0, "\tdisabled services = {\n" + "".join(f'\t\t"{lb}" => disabled\n' for lb in sorted(self.disabled)) + "\t}\n", ""
        label = args[-1].rsplit("/", 1)[-1]
        if cmd == "bootout":
            if self.loaded.pop(label, "missing") == "missing":
                return 3, "", "Boot-out failed: 3: No such process"
            return 0, "", ""
        if cmd == "disable":
            self.disabled.add(label)
            return 0, "", ""
        if cmd == "enable":
            self.disabled.discard(label)
            return 0, "", ""
        if cmd == "bootstrap":
            lb = plistlib.loads(Path(args[2]).read_bytes())["Label"]
            if lb in self.loaded:
                return 5, "", "Bootstrap failed: 5: Input/output error"
            self.loaded[lb] = 4242
            return 0, "", ""
        if cmd == "kickstart":
            if label not in self.loaded:
                return 113, "", "Could not find service"
            self.loaded[label] = 5151
            return 0, "", ""
        return 64, "", "unknown"


def _plist(d: Path, label: str, **kw) -> None:
    (d / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, **kw}))


@pytest.fixture
def world(tmp_path):
    ws, la = tmp_path / "ws", tmp_path / "LaunchAgents"
    ws.mkdir()
    la.mkdir()
    a = ws / "viec-nhom-2"
    (a / "scripts").mkdir(parents=True)
    (a / "package.json").write_text("{}")
    (a / "README.md").write_text("# Việc nhóm 2\n")
    log = tmp_path / "bantin.log"
    log.write_text("dòng 1\nGửi bản tin sáng OK\n")
    b = ws / "bot-khac"  # không có dấu hiệu dự án — nhận ra nhờ job trỏ vào
    b.mkdir()
    (ws / "_backup").mkdir()
    (ws / "_backup" / "package.json").write_text("{}")
    (ws / "ghi-chu").mkdir()  # không dấu hiệu, không job → bỏ qua
    _plist(la, "com.vn2.web", ProgramArguments=["/usr/bin/node", "next", "start"], WorkingDirectory=str(a), KeepAlive=True)
    _plist(la, "com.vn2.bantin", ProgramArguments=["/usr/bin/node", str(a / "scripts" / "nhac-viec.ts"), "--sang"], StartInterval=900,
           StandardOutPath=str(log))
    _plist(la, "com.vn2.nhacviec", ProgramArguments=["/bin/sh", "-c", f"cd {a} && npm run nhac"], StartCalendarInterval=[{"Hour": 8, "Minute": 0}])
    _plist(la, "com.khac.bot", ProgramArguments=[str(b / "run.sh")], RunAtLoad=True)
    _plist(la, "com.hub.self", ProgramArguments=["/usr/local/bin/cfharness", "daemon", "start"], WorkingDirectory=str(a))  # job của chính hub
    _plist(la, "com.ngoai.root", ProgramArguments=["/opt/x/run"], KeepAlive=True)  # ngoài gốc
    (la / "hong.plist").write_text("không phải plist")
    fake = FakeLaunchd({"com.vn2.web": 811, "com.vn2.bantin": None, "com.vn2.nhacviec": None, "com.khac.bot": 900})
    return {"ws": ws, "la": la, "fake": fake, "a": a}


@pytest.fixture
async def env(hub_home, world):
    d = await make_daemon(hub_home)
    d.hub.config.set("systems.roots", [str(world["ws"])])
    d.hub.config.set("systems.launch_agents_dir", str(world["la"]))
    d.hub.systems._lc = Launchctl(runner=world["fake"], uid=501)
    res = await manager_run(d)
    yield d, res["run_id"], world
    await close(d)


async def _register(d) -> dict:
    return await rpc(d, "system.scan", {"apply": True})


# ------------------------------------------------------------------ thuần
def test_parse_plist_and_schedule(world):
    j = parse_plist(world["la"] / "com.vn2.bantin.plist")
    assert j["kind"] == "scheduled" and j["schedule"] == "mỗi 15 phút"
    assert parse_plist(world["la"] / "com.vn2.nhacviec.plist")["schedule"] == "lúc 08:00"
    assert parse_plist(world["la"] / "com.vn2.web.plist")["kind"] == "service"
    assert parse_plist(world["la"] / "hong.plist") is None
    assert slug("Hệ Thống Mẫu") == "he-thong-mau" and slug("viec-nhom-2") == "viec-nhom-2"


def test_launchctl_parses_state_and_disabled(world):
    f = world["fake"]
    f.disabled.add("com.vn2.bantin")
    lc = Launchctl(runner=f, uid=501)
    st = lc.state()
    assert st["com.vn2.web"]["pid"] == 811 and st["com.vn2.bantin"]["pid"] is None
    assert lc.disabled() == {"com.vn2.bantin"}
    assert ["print-disabled", "gui/501"] in f.calls


# ------------------------------------------------------------------ nhận diện + đăng ký
async def test_scan_discovers_systems_and_jobs(env):
    d, _, w = env
    r = await rpc(d, "system.scan", {})
    found = {s["id"]: s for s in r["found"]}
    assert set(found) == {"viec-nhom-2", "bot-khac"}, found  # _backup, ghi-chu bị bỏ qua
    assert sorted(found["viec-nhom-2"]["jobs"]) == ["com.vn2.bantin", "com.vn2.nhacviec", "com.vn2.web"]  # job của hub bị loại
    assert found["viec-nhom-2"]["title"] == "Việc nhóm 2"
    assert found["bot-khac"]["jobs"] == ["com.khac.bot"]
    assert not r["applied"] and await rpc(d, "system.list") == []
    r2 = await _register(d)
    assert sorted(r2["new"]) == ["bot-khac", "viec-nhom-2"] and r2["registered"] == 2
    assert (await rpc(d, "system.scan", {}))["new"] == []
    rows = {s["id"]: s for s in await rpc(d, "system.list")}
    st = {j["label"]: j["status"] for j in rows["viec-nhom-2"]["jobs"]}
    assert st == {"com.vn2.web": "running", "com.vn2.bantin": "idle", "com.vn2.nhacviec": "idle"}


# ------------------------------------------------------------------ người thao tác job
async def test_human_pause_resume_restart(env):
    d, _, w = env
    f = w["fake"]
    await _register(d)
    r = await rpc(d, "system.job", {"label": "com.vn2.bantin", "action": "pause", "reason": "tạm tắt thông báo"})
    assert r["status"] == "paused" and r["disabled"]
    assert ["bootout", "gui/501/com.vn2.bantin"] in f.calls and ["disable", "gui/501/com.vn2.bantin"] in f.calls
    assert "com.vn2.bantin" not in f.loaded
    job = next(j for j in (await rpc(d, "system.get", {"id": "viec-nhom-2"}))["jobs"] if j["label"] == "com.vn2.bantin")
    assert job["paused_by"]["by"] == "user" and job["paused_by"]["reason"] == "tạm tắt thông báo"
    with pytest.raises(HubError, match="tạm dừng"):
        await rpc(d, "system.job", {"label": "com.vn2.bantin", "action": "restart"})
    r = await rpc(d, "system.job", {"label": "com.vn2.bantin", "action": "resume"})
    assert r["status"] == "running" and not r["disabled"] and "paused_by" not in r
    r = await rpc(d, "system.job", {"label": "com.vn2.web", "action": "restart"})
    assert f.loaded["com.vn2.web"] == 5151
    evs = [e.payload for e in d.hub.store.query_events(types=["system.job"])]
    assert [(e["action"], e["ok"]) for e in evs] == [("pause", True), ("resume", True), ("restart", True)]
    assert [a["kind"] for a in d.hub.audit.query(kind="system.job")]
    # pause job đã dừng sẵn vẫn ổn (bootout trả 3)
    await rpc(d, "system.job", {"label": "com.vn2.nhacviec", "action": "pause"})
    await rpc(d, "system.job", {"label": "com.vn2.nhacviec", "action": "pause"})


async def test_log_tail_and_unknown_job(env):
    d, _, w = env
    await _register(d)
    s = await rpc(d, "system.get", {"id": "viec-nhom-2", "log_bytes": 2000})
    job = next(j for j in s["jobs"] if j["label"] == "com.vn2.bantin")
    assert "bản tin sáng" in job["log"]["stdout"]
    for lb in ("com.ngoai.root", "com.hub.self", "../../etc"):
        with pytest.raises(HubError):
            await rpc(d, "system.job", {"label": lb, "action": "pause"})
    assert not any(c[0] == "bootout" for c in w["fake"].calls)


# ------------------------------------------------------------------ điều phối viên
def _denials(d) -> list[dict]:
    return [r for r in d.hub.audit.query(limit=500) if r["actor"] == "manager:boss" and r["kind"] in ("manager.denied", "auth.denied")]


async def test_manager_pauses_registered_job(env):
    d, rid, w = env
    await _register(d)
    rows = await tool(d, rid, "system_list")
    assert any(s["id"] == "viec-nhom-2" for s in rows)
    res = await tool(d, rid, "system_job", {"label": "com.vn2.bantin", "action": "pause", "reason": "người yêu cầu tắt thông báo"})
    assert res["status"] == "paused"
    au = [a for a in d.hub.audit.query(kind="system.job") if a["actor"] == "manager:boss"]
    assert au, "thao tác của manager phải được audit actor manager:boss"
    acts = [e.payload for e in d.hub.store.query_events(types=["manager.action"])]
    assert acts[-1]["tool"] == "system_job" and acts[-1]["ok"] and acts[-1]["target"] == "system:com.vn2.bantin"
    assert (await tool(d, rid, "system_get", {"id": "viec-nhom-2", "log": True}))["jobs"]
    res = await tool(d, rid, "system_job", {"label": "com.vn2.bantin", "action": "resume"})
    assert res["status"] == "running"


@pytest.mark.parametrize("label,action,match", [
    ("com.ngoai.root", "pause", "không thuộc hệ thống"),  # ngoài gốc / chưa đăng ký
    ("com.hub.self", "pause", "không thuộc hệ thống"),  # job của chính hub
    ("com.vn2.web", "unload", "action phải là"),
])
async def test_manager_denied_outside_scope(env, label, action, match):
    d, rid, w = env
    await _register(d)
    n0 = len(_denials(d))
    with pytest.raises(HubError) as ei:
        await tool(d, rid, "system_job", {"label": label, "action": action})
    assert ei.value.code == BLOCKED and match in ei.value.message, ei.value.message
    assert len(_denials(d)) == n0 + 1
    with pytest.raises(HubError) as ei:  # lớp 2
        await rpc(d, "system.job", {"label": label, "action": action}, as_caller=auth.manager_caller("boss", rid))
    assert ei.value.code == BLOCKED and (ei.value.data or {}).get("reason") == "manager_forbidden"
    assert not any(c[0] in ("bootout", "disable", "kickstart") for c in w["fake"].calls)


async def test_manager_denied_unregistered_system(env):
    d, rid, w = env  # chưa scan --apply → không job nào thuộc hệ thống đã đăng ký
    with pytest.raises(HubError) as ei:
        await tool(d, rid, "system_job", {"label": "com.vn2.bantin", "action": "pause"})
    assert ei.value.code == BLOCKED
    assert "com.vn2.bantin" in w["fake"].loaded


async def test_locked_job_human_only(env):
    d, rid, w = env
    await _register(d)
    await rpc(d, "system.lock", {"label": "com.vn2.web", "on": True})
    for call in (lambda: tool(d, rid, "system_job", {"label": "com.vn2.web", "action": "restart"}),
                 lambda: rpc(d, "system.job", {"label": "com.vn2.web", "action": "restart"}, as_caller=auth.manager_caller("boss", rid))):
        with pytest.raises(HubError) as ei:
            await call()
        assert ei.value.code == BLOCKED and "khoá" in ei.value.message
    assert w["fake"].loaded["com.vn2.web"] == 811
    assert (await rpc(d, "system.job", {"label": "com.vn2.web", "action": "restart"}))["ok"]  # người vẫn làm được
    assert next(j for s in await rpc(d, "system.list") for j in s["jobs"] if j["label"] == "com.vn2.web")["locked"]


async def test_manager_cannot_register_lock_or_scan(env):
    d, rid, w = env
    for method, params in (("system.scan", {"apply": True}), ("system.add", {"path": str(w["a"])}), ("system.rm", {"id": "viec-nhom-2"}),
                           ("system.lock", {"label": "com.vn2.web", "on": False})):
        with pytest.raises(HubError) as ei:
            await rpc(d, method, params, as_caller=auth.manager_caller("boss", rid))
        assert ei.value.code == BLOCKED, method
    assert await rpc(d, "system.list") == []


async def test_manager_rate_limit(env):
    d, rid, w = env
    await _register(d)
    d.hub.config.set("manager.max_system_ops_per_hour", 2)
    await tool(d, rid, "system_job", {"label": "com.vn2.bantin", "action": "pause"})
    await tool(d, rid, "system_job", {"label": "com.vn2.bantin", "action": "resume"})
    with pytest.raises(HubError, match="trần tần suất"):
        await tool(d, rid, "system_job", {"label": "com.vn2.bantin", "action": "pause"})


async def test_agent_in_run_cannot_operate_jobs(env):
    d, rid, w = env
    await _register(d)
    agent = auth.Caller(via="socket", pid=99, token_ok=True, run_id="r_x", agent_id="w1", evidence=["env_run_id"])
    for method, params in (("system.job", {"label": "com.vn2.bantin", "action": "pause"}), ("system.scan", {"apply": True})):
        with pytest.raises(HubError) as ei:
            await rpc(d, method, params, as_caller=agent)
        assert ei.value.code == BLOCKED
    assert "com.vn2.bantin" in w["fake"].loaded


async def test_failed_launchctl_reported(env):
    d, rid, w = env
    await _register(d)
    w["fake"].loaded.pop("com.vn2.web")  # kickstart → 113
    with pytest.raises(HubError, match="kickstart lỗi"):
        await rpc(d, "system.job", {"label": "com.vn2.web", "action": "restart"})
    ev = [e.payload for e in d.hub.store.query_events(types=["system.job"])][-1]
    assert ev["ok"] is False


def test_classifier_system_cli_forbidden_for_agents(tmp_path):
    ctx = GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "h").load())
    for cmd in ("cfharness system pause com.vn2.bantin", "cfharness system scan --apply", "uv run cfharness system lock x",
                "cfharness system restart com.vn2.web"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd
    assert not classify("Bash", {"command": "cfharness system list"}, ctx).forbidden


def test_manager_tools_declared():
    from cfharness.mcp_server.hub_self import MANAGER_READ_TOOLS, MANAGER_TOOLS

    assert {"system_list", "system_get"} <= MANAGER_READ_TOOLS and "system_job" in MANAGER_TOOLS and "system_job" not in MANAGER_READ_TOOLS
    assert "system.job" in auth.MANAGER_ALLOWED and not {"system.scan", "system.add", "system.rm", "system.lock"} & set(auth.MANAGER_ALLOWED)
    assert json.dumps(MANAGER_TOOLS["system_job"])
