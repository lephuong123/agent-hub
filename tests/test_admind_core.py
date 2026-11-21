"""admind lõi: pipeline thao tác, xác thực, ma trận quyền, step-up, four-eyes, audit chuỗi băm, job (plan-superadmin SA0-2, SA1-1, SA2-1)."""

from __future__ import annotations

import json

import pytest

from cfharness.admind import rbac
from cfharness.admind.dispatch import Peer
from cfharness.admind.jobs import KINDS, Step, StepError, kind
from cfharness.admind.ops import REGISTRY, load_all

from admind_helpers import PW, SAC_UID, make_env


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    yield e
    e.state.db.close()


# ============================================================================ nguồn gọi / mã thao tác
def test_unknown_uid_rejected(env):
    r = env.disp.handle({"op": "sys.ping"}, Peer(uid=999))
    assert r["error"]["code"] == "forbidden"


def test_unknown_op_and_bad_request(env):
    assert env.err("rm -rf /") == "invalid"
    assert env.disp.handle("chuỗi", Peer(uid=SAC_UID))["error"]["code"] == "invalid"
    assert env.ok("sys.ping")["pong"] is True


def test_no_op_takes_shell_string():
    """Không mã thao tác nào có tham số kiểu 'command/cmd/argv/shell'."""
    for o in load_all().values():
        for f in o.params.model_fields:
            assert f not in ("cmd", "command", "argv", "shell", "script"), o.name


def test_every_op_group_in_matrix():
    for o in load_all().values():
        assert o.group in rbac.MATRIX, o.name


def test_root_only_requires_root(env):
    assert env.err("sys.bootstrap", {"email": "a@ex.com", "name": "A"}) == "forbidden"
    r = env.ok("sys.bootstrap", {"email": "a@ex.com", "name": "A"}, root=True)
    assert len(r["enroll_code"]) >= 20 and r["account"]["state"] == "pending"


def test_params_validated(env):
    env.make_admin("boss")
    assert env.err("admin.create", {"email": "khong-phai-email", "name": "x", "role": "operator"}, as_="boss") == "invalid"
    assert env.err("admin.create", {"email": "x@ex.com", "name": "x", "role": "operator", "la": 1}, as_="boss") == "invalid"


# ============================================================================ đăng nhập
def test_login_flow_and_lockout(env):
    a = env.make_admin("boss")
    assert env.ok("auth.me", as_="boss")["account"]["id"] == a["id"]
    # sai TOTP → unauthenticated; 5 lần sai cùng IP → locked
    for _ in range(5):
        assert env.err("auth.login", {"email": "boss@ex.com", "password": PW, "totp": "000000"}, ip="9.9.9.9") == "unauthenticated"
    assert env.err("auth.login", {"email": "boss@ex.com", "password": PW, "totp": env.code_for(a["id"])}, ip="9.9.9.9") == "locked"
    # audit không chứa mật khẩu
    rows = env.state.audit.search(op_prefix="auth.login")
    assert rows and all(PW not in json.dumps(r) for r in rows)


def test_totp_replay_rejected(env):
    a = env.make_admin("boss")
    code = env.code_for(a["id"])
    env.ok("auth.login", {"email": "boss@ex.com", "password": PW, "totp": code})
    assert env.err("auth.login", {"email": "boss@ex.com", "password": PW, "totp": code}) == "unauthenticated"


def test_session_idle_and_max(env):
    env.make_admin("boss")
    env.clock.tick(15 * 60 + 1)
    assert env.err("auth.me", as_="boss") == "unauthenticated"
    env.make_admin("b2")
    for _ in range(40):
        env.clock.tick(14 * 60)
        if not env.call("auth.me", as_="b2")["ok"]:
            break
    assert env.err("auth.me", as_="b2") == "unauthenticated"  # quá 8 giờ


def test_logout_and_revoke(env):
    env.make_admin("boss")
    env.ok("auth.logout", as_="boss")
    assert env.err("auth.me", as_="boss") == "unauthenticated"


# ============================================================================ ma trận vai trò
def test_role_matrix_enforced(env):
    env.make_admin("boss")
    env.make_admin("aud", role="auditor")
    env.make_admin("opr", role="operator")
    assert env.err("admin.list", as_="aud") == "forbidden"
    assert env.err("admin.list", as_="opr") == "forbidden"
    assert isinstance(env.ok("admin.list", as_="boss"), list)
    assert isinstance(env.ok("audit.search", as_="aud"), list)
    assert env.err("sys.config_get", as_="opr") == "forbidden"


def test_operator_sees_only_own_audit(env):
    env.make_admin("boss")
    o = env.make_admin("opr", role="operator")
    rows = env.ok("audit.search", as_="opr")
    assert rows and all(r["actor_id"] == o["id"] for r in rows)


def test_matrix_generated_cases(env):
    """Mọi (vai trò × thao tác có phiên): không có quyền → forbidden, không bao giờ chạy handler."""
    env.make_admin("boss")
    for role in rbac.ROLES:
        if role == rbac.SUPER:
            continue
        env.make_admin(role, role=role, scope={"teams": ["t1"]} if role == rbac.TEAM_LEAD else {})
    for o in REGISTRY.values():
        if o.public or o.root_only:
            continue
        for role in rbac.ROLES:
            if role == rbac.SUPER:
                continue
            if not rbac.allowed(role, o.group, o.need):
                assert env.err(o.name, {}, as_=role) == "forbidden", (o.name, role)


# ============================================================================ step-up / four-eyes
def test_step_up_required_for_high(env):
    env.make_admin("boss")
    env.clock.tick(6 * 60)
    env.ok("auth.me", as_="boss")
    assert env.err("admin.create", {"email": "o@ex.com", "name": "O", "role": "operator"}, as_="boss") == "step_up_required"
    env.step_up("boss")
    r = env.ok("admin.create", {"email": "o@ex.com", "name": "O", "role": "operator"}, as_="boss")
    assert r["enroll_code"]


def test_four_eyes_two_supers(env):
    a = env.make_admin("boss")
    env.make_admin("boss2")
    o = env.make_admin("opr", role="operator")
    r = env.call("admin.delete", {"id": o["id"], "reason": "nghỉ việc"}, as_="boss")
    assert r["error"]["code"] == "approval_required"
    aid = r["error"]["data"]["approval_id"]
    assert env.state.auth.account(o["id"]) is not None
    # người yêu cầu không tự duyệt
    assert env.err("approval.decide", {"id": aid, "approve": True}, as_="boss") == "forbidden"
    out = env.ok("approval.decide", {"id": aid, "approve": True}, as_="boss2")
    assert out["result"]["ok"] and env.state.auth.account(o["id"]) is None
    rows = env.state.audit.search(op_prefix="admin.delete")
    assert any(r["approver_id"] for r in rows) and any(r["actor_id"] == a["id"] for r in rows)


def test_four_eyes_solo_waits(env):
    env.make_admin("boss")
    o = env.make_admin("opr", role="operator")
    r = env.call("admin.delete", {"id": o["id"]}, as_="boss")
    assert r["error"]["data"]["solo"] is True
    assert env.state.approvals.run_due() == 0
    env.clock.tick(3601)
    assert env.state.approvals.run_due() == 1
    assert env.state.auth.account(o["id"]) is None


def test_four_eyes_expiry(env):
    env.make_admin("boss")
    env.make_admin("boss2")
    o = env.make_admin("opr", role="operator")
    aid = env.call("admin.delete", {"id": o["id"]}, as_="boss")["error"]["data"]["approval_id"]
    env.clock.tick(24 * 3600 + 1)
    env.make_admin("boss3")
    assert env.err("approval.decide", {"id": aid, "approve": True}, as_="boss3") == "conflict"


def test_last_super_admin_guard(env):
    a = env.make_admin("boss")
    env.make_admin("opr", role="operator")
    env.step_up("boss")
    assert env.err("admin.lock", {"id": a["id"]}, as_="boss") == "forbidden"  # không tự khoá


# ============================================================================ audit chuỗi băm
def test_audit_chain_detects_edit(env):
    env.make_admin("boss")
    env.ok("auth.logout", as_="boss")
    assert env.state.audit.verify()["ok"]
    env.state.db.exec("UPDATE audit SET op='sửa' WHERE seq=1")
    v = env.state.audit.verify()
    assert not v["ok"] and v["reason"] == "hash_mismatch"


def test_audit_chain_detects_delete(env):
    env.make_admin("boss")
    env.ok("auth.logout", as_="boss")
    env.state.db.exec("DELETE FROM audit WHERE seq=1")
    assert env.state.audit.verify()["reason"] in ("seq_gap", "prev_mismatch")


def test_denied_calls_audited(env):
    env.make_admin("aud", role="auditor")
    env.err("admin.list", as_="aud")
    assert any(r["outcome"] == "denied" for r in env.state.audit.search(op_prefix="admin.list"))


# ============================================================================ job
class _S(Step):
    def __init__(self, name, box, fail=False):
        self.name, self.title, self.box, self.fail = name, name, box, fail

    def check(self, jc):
        return self.name in self.box["done"]

    def apply(self, jc):
        if self.fail and not self.box.get("fixed"):
            raise StepError("hỏng giả lập " + jc.secrets.get("claude_token", ""))
        self.box["done"].add(self.name)
        jc.log("đã làm " + self.name)

    def undo(self, jc):
        self.box["done"].discard(self.name)
        self.box.setdefault("undone", []).append(self.name)


BOX: dict = {}


@kind("test.three")
def _three(params):
    return [_S("a", BOX), _S("b", BOX, fail=True), _S("c", BOX)]


def test_job_idempotent_retry_and_cleanup(env):
    BOX.clear()
    BOX["done"] = set()
    jid = env.state.jobs.submit("test.three", target="u1", params={}, created_by="x", secrets={"claude_token": "sk-ant-bimat123456"})
    j = env.state.jobs.get(jid)
    assert j["state"] == "failed" and j["resume_from"] == "b"
    assert "sk-ant-bimat123456" not in json.dumps(j)  # log đã che secret
    BOX["fixed"] = True
    env.state.jobs.retry(jid)
    j = env.state.jobs.get(jid)
    assert j["state"] == "succeeded" and [s["state"] for s in j["steps"]] == ["done", "done", "done"]
    # chạy lại job mới: mọi bước 'check' đúng → bỏ qua
    jid2 = env.state.jobs.submit("test.three", target="u1", params={}, created_by="x")
    assert [s["state"] for s in env.state.jobs.get(jid2)["steps"]] == ["skipped"] * 3


def test_job_cancel_cleanup(env):
    BOX.clear()
    BOX["done"] = set()
    jid = env.state.jobs.submit("test.three", target="u2", params={}, created_by="x")
    out = env.state.jobs.cancel(jid, cleanup=True)
    assert "a" in out["undone"] and "a" not in BOX["done"]


def test_job_recover_after_restart(env):
    jid = env.state.jobs.submit("test.three", target="u3", params={}, created_by="x") if (BOX.update(done=set()) or True) else None
    env.state.db.exec("UPDATE jobs SET state='running' WHERE id=?", (jid,))
    from cfharness.admind.jobs import JobEngine

    JobEngine(env.state)
    assert env.state.jobs.get(jid)["error"] == "admind_restarted"


def test_kinds_registry():
    assert "test.three" in KINDS
