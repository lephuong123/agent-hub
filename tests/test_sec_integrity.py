"""Hardening SEC — H-02 lớp 3 (bất biến §4.3): policy/ + golden + config.yaml (nhánh an toàn) + hub.db bị ghi NGOÀI RPC.

Kịch bản QA (sg2.txt, cls.py): `python3 -c "open('<HUB>/policy/protected.yaml','a')…"`, `ln -s <HUB>/policy pol; echo x > pol/…`,
`echo 'enabled: false' > <HUB>/policy/learning.yaml`, `sqlite3 <HUB>/hub.db "UPDATE golden_tasks …"` — lớp này không cần
biết lệnh viết thế nào: so đĩa với bản chuẩn trong hub.db → khôi phục + `guard.tamper_detected` (+ Slack).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys

import pytest
import yaml

from cfharness.hub import Hub
from cfharness.paths import HubPaths
from cfharness.runtime.fake import FakeRuntime


@pytest.fixture
def hub(tmp_path):
    p = HubPaths.resolve(tmp_path / "hub")
    p.ensure()
    p.config.write_text(yaml.safe_dump({"guard": {"git_probe": False, "integrity": {"tick_s": 0.05}}, "hub": {"max_concurrent_runs": 3}}))
    h = Hub(p, runtime=FakeRuntime())
    yield h
    try:
        h.store.close()
    except Exception:
        pass


def tamper_events(h):
    return [e for e in h.store.query_events() if e.type == "guard.tamper_detected"]


async def _tick(h, n=6):
    for _ in range(n):
        await asyncio.sleep(0.05)


async def test_policy_tamper_via_python_and_symlink_is_restored(hub, tmp_path):
    h = hub
    h.start()
    pol = h.paths.home / "policy"
    orig = (pol / "protected.yaml").read_bytes()
    # QA: python3 -c "open('<HUB>/policy/protected.yaml','a').write('x')" — chạy thật như agent cùng user OS
    subprocess.run([sys.executable, "-c", f"open({str(pol / 'protected.yaml')!r},'a').write('\\nresources: []\\n')"], check=True)
    # QA: ln -s <HUB>/policy pol; echo x > pol/catalog.yaml (ghi xuyên symlink)
    link = tmp_path / "pol"
    os.symlink(pol, link)
    (link / "catalog.yaml").write_text("bash_patterns: []\n")
    # thêm file lạ
    (pol / "evil.yaml").write_text("x")
    await _tick(h)
    assert (pol / "protected.yaml").read_bytes() == orig
    assert "bash_patterns: []\n" != (pol / "catalog.yaml").read_text()
    assert not (pol / "evil.yaml").exists()
    evs = tamper_events(h)
    assert evs and evs[-1].payload["target"] == "policy"
    # bản bị sửa không mất: nằm trong trash/tamper/
    assert list((h.paths.home / "trash" / "tamper").rglob("evil.yaml"))
    await h.stop()


async def test_policy_dir_replaced_by_symlink_and_learning_yaml(hub, tmp_path):
    h = hub
    h.learning.policy.set("enabled", True)  # ghi hợp lệ (daemon) → bản chuẩn có learning.yaml
    h.start()
    pol = h.paths.home / "policy"
    learn_orig = (pol / "learning.yaml").read_text()
    # QA cls.py: echo 'enabled: false' > <HUB>/policy/learning.yaml
    (pol / "learning.yaml").write_text("enabled: false\n")
    await _tick(h)
    assert (pol / "learning.yaml").read_text() == learn_orig
    # thay cả thư mục policy bằng symlink tới bản giả
    fake = tmp_path / "fakepol"
    fake.mkdir()
    (fake / "protected.yaml").write_text("resources: []\n")
    os.rename(pol, tmp_path / "moved_policy")
    os.symlink(fake, pol)
    await _tick(h, 10)
    assert pol.is_dir() and not pol.is_symlink()
    assert (pol / "learning.yaml").read_text() == learn_orig and (pol / "protected.yaml").exists()
    await h.stop()


async def test_legit_rpc_writes_are_not_reverted(hub):
    """Người sửa qua RPC/CLI (protect add, learn policy set, golden add, config set) → bản chuẩn cập nhật, không bị khôi phục."""
    h = hub
    h.start()
    r = h.guard.protect_add({"kind": "path", "selector": "/data/prod/**", "ops": ["write"]})
    h.learning.policy.set("auto_apply", False)
    h.agents.create({"id": "a", "name": "A"})
    h.evals.golden.add({"agent_id": "a", "title": "t", "prompt": "p", "checker": {"kind": "contains", "text": "x"}}, created_by="user")
    h.config.set("approval.timeout_s", 120)
    await _tick(h)
    assert "/data/prod/**" in (h.paths.home / "policy" / "protected.yaml").read_text()
    assert (h.paths.home / "policy" / "golden" / "a.yaml").exists()
    assert h.config.get("approval.timeout_s") == 120
    assert not tamper_events(h), [e.payload for e in tamper_events(h)]
    # golden mirror bị agent sửa tay → khôi phục
    g = h.paths.home / "policy" / "golden" / "a.yaml"
    good = g.read_bytes()
    g.write_text("tasks: []\n")
    await _tick(h)
    assert g.read_bytes() == good
    await h.stop()


async def test_config_security_keys_restored_other_keys_adopted(hub):
    h = hub
    h.start()
    cfg = h.paths.config
    data = yaml.safe_load(cfg.read_text()) or {}
    data.setdefault("guard", {})["enabled"] = False  # agent tắt guard bằng cách ghi thẳng config.yaml
    data.setdefault("hub", {})["max_concurrent_runs"] = 7  # khoá thường do người sửa tay → được nhận
    cfg.write_text(yaml.safe_dump(data))
    await _tick(h)
    after = yaml.safe_load(cfg.read_text())
    assert after["guard"].get("enabled", True) is not False
    assert after["hub"]["max_concurrent_runs"] == 7
    ev = tamper_events(h)
    assert ev and ev[-1].payload["target"] == "config" and "guard" in ev[-1].payload["keys"]
    await h.stop()


async def test_hub_db_external_write_detected(hub):
    """QA: sqlite3 <HUB>/hub.db "UPDATE golden_tasks …" — kết nối khác ghi DB → phát hiện (event + Slack), không im lặng."""
    h = hub
    h.start()
    await _tick(h, 2)
    subprocess.run([sys.executable, "-c", f"import sqlite3;c=sqlite3.connect({str(h.paths.db)!r});c.execute(\"UPDATE hub_state SET value=value\");"
                    f"c.execute(\"INSERT INTO hub_state(key,value,updated_at) VALUES('x','1','t')\");c.commit()"], check=True)
    await _tick(h)
    evs = [e for e in tamper_events(h) if e.payload["target"] == "hub.db"]
    assert evs and evs[-1].payload.get("external_write")
    await h.stop()


async def test_tamper_while_daemon_down_restored_on_start(tmp_path):
    p = HubPaths.resolve(tmp_path / "hub")
    p.ensure()
    p.config.write_text(yaml.safe_dump({"guard": {"git_probe": False}}))
    h = Hub(p, runtime=FakeRuntime())
    h.start()
    good = (p.home / "policy" / "protected.yaml").read_bytes()
    await h.stop()
    (p.home / "policy" / "protected.yaml").unlink()  # agent xoá lúc daemon tắt
    h2 = Hub(p, runtime=FakeRuntime())  # PolicyStore.ensure() tạo lại bản rỗng mặc định…
    h2.start()  # …nhưng bản chuẩn trong DB thắng
    assert (p.home / "policy" / "protected.yaml").read_bytes() == good
    await h2.stop()


async def test_human_accept_installs_new_policy(hub, tmp_path):
    from cfharness.daemon import Daemon

    d = Daemon(hub.paths, runtime=FakeRuntime())
    d.hub.start()
    newtxt = "version: 9\nresources: []\n"
    r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "guard.integrity.accept", "params": {"path": "policy/protected.yaml", "content": newtxt}})
    assert r["result"]["ok"]
    await asyncio.sleep(0.2)
    assert (hub.paths.home / "policy" / "protected.yaml").read_text() == newtxt
    r = await d._dispatch({"jsonrpc": "2.0", "id": 2, "method": "guard.integrity.accept", "params": {"path": "../x", "content": "x"}})
    assert "error" in r
    await d.hub.stop()
