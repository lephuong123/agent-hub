"""End-to-end: daemon thật (tiến trình con) + CLI + FakeRuntime. Không gọi `claude`."""

import json
import sqlite3

import pytest
import yaml

from agent_hub.cli import main
from agent_hub.daemon import spawn_background
from agent_hub.rpc import RpcClient


@pytest.fixture
def daemon(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake", "fake": {"line_delay_s": 0.0}}}))
    spawn_background(hub_home)
    yield hub_home
    main(["daemon", "stop"])


def cli(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_e2e_daemon_run_follow(daemon, capsys):
    paths = daemon
    code, out, _ = cli(capsys, "daemon", "status", "--json")
    st = json.loads(out)
    assert code == 0 and st["daemon_running"] and st["runtime"] == "fake" and st["max_concurrent_runs"] == 4

    code, out, _ = cli(capsys, "agent", "create", "researcher", "--role", "Nghiên cứu", "--model", "haiku", "--budget-per-run", "0.2")
    assert code == 0, out
    code, _, err = cli(capsys, "agent", "create", "researcher")
    assert code == 1 and "agent đã tồn tại" in err

    code, out, err = cli(capsys, "run", "researcher", "tóm tắt README", "--follow")
    assert code == 0, err
    assert "┊ Bash(ls)" in out and "xong" in out and "■ completed · $0.0047" in out

    code, out, _ = cli(capsys, "runs", "list", "--json")
    runs = json.loads(out)
    assert len(runs) == 1 and runs[0]["status"] == "completed"
    run = runs[0]
    assert run["cost_usd"] == 0.00474188 and run["max_budget_usd"] == 0.2

    # DB: event đã ghi, cost khớp result.total_cost_usd
    db = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True)
    rows = db.execute("SELECT type, payload FROM events WHERE run_id=? ORDER BY seq", (run["id"],)).fetchall()
    types = [r[0] for r in rows]
    assert types[0] == "run.queued" and "run.started" in types and "tool.start" in types and "tool.complete" in types
    done = [json.loads(p) for t, p in rows if t == "run.completed"]
    assert len(done) == 1 and done[0]["cost_usd"] == 0.00474188
    assert db.execute("SELECT cost_usd FROM runs WHERE id=?", (run["id"],)).fetchone()[0] == 0.00474188
    db.close()

    code, out, _ = cli(capsys, "agent", "list", "--json")
    ag = json.loads(out)[0]
    assert ag["status"] == "idle" and ag["cost_today_usd"] == pytest.approx(0.00474188)


def test_e2e_estop_config_and_soft_delete(daemon, capsys):
    cli(capsys, "agent", "create", "a1")
    code, out, _ = cli(capsys, "config", "set", "hub.max_concurrent_runs", "2")
    assert code == 0 and "hub.max_concurrent_runs = 2" in out
    code, out, _ = cli(capsys, "config", "get", "hub.max_concurrent_runs")
    assert out.strip() == "2"
    code, _, err = cli(capsys, "config", "set", "hub.max_concurrent_runs", "0")
    assert code == 1 and "≥ 1" in err

    code, out, _ = cli(capsys, "estop", "on", "--reason", "bảo trì")
    assert code == 0 and "BẬT" in out
    code, _, err = cli(capsys, "run", "a1", "hi")
    assert code == 3 and "ESTOP" in err
    cli(capsys, "estop", "off")
    code, out, _ = cli(capsys, "run", "a1", "hi")
    assert code == 0 and out.strip().startswith("r_")
    RpcClient(daemon.socket).call("run.wait", {"id": out.strip(), "timeout": 10})

    code, out, _ = cli(capsys, "agent", "edit", "a1", "--model", "opus", "--set", "limits.max_runtime_s=600")
    assert code == 0 and "revision 2" in out
    code, out, _ = cli(capsys, "agent", "rm", "a1")
    assert code == 0 and "xoá mềm" in out
    code, out, _ = cli(capsys, "agent", "list", "--json")
    assert json.loads(out) == []
    code, out, _ = cli(capsys, "agent", "restore", "a1")
    assert code == 0
    code, out, _ = cli(capsys, "agent", "show", "a1", "--json")
    ag = json.loads(out)
    assert ag["model"] == "opus" and ag["limits"]["max_runtime_s"] == 600 and ag["status"] == "idle"


def test_e2e_subscribe_replay_after_run(daemon, capsys):
    cli(capsys, "agent", "create", "a2")
    code, out, _ = cli(capsys, "run", "a2", "x")
    rid = out.strip()
    c = RpcClient(daemon.socket)
    c.call("run.wait", {"id": rid, "timeout": 10})
    # attach muộn: replay từ đầu (seq 0) rồi đóng khi gặp event kết thúc của hub
    msgs = list(c.subscribe({"run_id": rid, "since_seq": 0, "until_run_end": True}))
    evs = [m for m in msgs if "type" in m]
    assert evs[0]["type"] == "run.queued" and evs[-1]["type"] == "run.completed"
    seqs = [e["seq"] for e in evs]
    assert seqs == sorted(seqs)
    assert msgs[-1].get("end", {}).get("reason") == "run_finished"
