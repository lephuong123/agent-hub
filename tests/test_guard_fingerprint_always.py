"""A2 + A3: fingerprint của vé/luật `exact` phân biệt thao tác thật khác nhau; luật exact phải gắn agent."""
from __future__ import annotations

import time

import pytest

from cfharness.guard.classifier import GuardContext, action_fingerprint, match_always
from cfharness.guard.policy import PolicyStore


def test_fp_distinguishes_sandbox_escape():
    a = action_fingerprint("Bash", {"command": "make deploy"})
    assert a != action_fingerprint("Bash", {"command": "make deploy", "dangerouslyDisableSandbox": True})
    # false/thiếu = cùng thao tác (giữ fingerprint cũ)
    assert a == action_fingerprint("Bash", {"command": "make deploy", "dangerouslyDisableSandbox": False})


def test_fp_whitespace_collapsed_only_outside_quotes():
    assert action_fingerprint("Bash", {"command": "touch 'a b'"}) != action_fingerprint("Bash", {"command": "touch 'a  b'"})
    assert action_fingerprint("Bash", {"command": 'echo "x\ty"'}) != action_fingerprint("Bash", {"command": 'echo "x y"'})
    assert action_fingerprint("Bash", {"command": "ls   -la  'a b'"}) == action_fingerprint("Bash", {"command": " ls -la 'a b' "})


def test_fp_bash_includes_normalized_cwd(tmp_path):
    a = action_fingerprint("Bash", {"command": "rm -rf build"}, tmp_path / "x")
    assert a != action_fingerprint("Bash", {"command": "rm -rf build"}, tmp_path / "y")
    assert a == action_fingerprint("Bash", {"command": "rm -rf build"}, str(tmp_path / "x" / "z" / ".."))
    # tool khác Bash: cwd không đổi fingerprint
    assert action_fingerprint("Write", {"file_path": "/a"}, tmp_path / "x") == action_fingerprint("Write", {"file_path": "/a"}, tmp_path / "y")


@pytest.fixture
def ps(tmp_path):
    p = PolicyStore(tmp_path / "hub", user_home=tmp_path / "home")
    p.ensure()
    return p


def _ctx(tmp_path, ps, rules, agent, cwd=None):
    return GuardContext(cwd or tmp_path, ps.load(), always_allow=tuple(rules), agent_id=agent, git_probe=False, run_since=time.time())


def test_exact_rule_without_agent_matches_nobody(tmp_path, ps):
    fp = action_fingerprint("Bash", {"command": "make deploy"}, tmp_path)
    rule = {"kind": "exact", "tool": "Bash", "fingerprint": fp}
    assert match_always("Bash", {"command": "make deploy"}, _ctx(tmp_path, ps, [rule], "other-agent")) is None
    owned = {**rule, "agent_id": "w"}
    assert match_always("Bash", {"command": "make deploy"}, _ctx(tmp_path, ps, [owned], "w")) is owned
    assert match_always("Bash", {"command": "make deploy"}, _ctx(tmp_path, ps, [owned], "other-agent")) is None


def test_exact_rule_bound_to_cwd(tmp_path, ps):
    (tmp_path / "other").mkdir()
    rule = {"kind": "exact", "agent_id": "w", "tool": "Bash", "fingerprint": action_fingerprint("Bash", {"command": "make deploy"}, tmp_path)}
    assert match_always("Bash", {"command": "make deploy"}, _ctx(tmp_path, ps, [rule], "w")) is rule
    assert match_always("Bash", {"command": "make deploy"}, _ctx(tmp_path, ps, [rule], "w", tmp_path / "other")) is None


def test_always_add_exact_requires_agent(ps):
    with pytest.raises(ValueError):
        ps.always_add({"kind": "exact", "tool": "Bash", "fingerprint": "abc"})
    assert ps.always_add({"kind": "exact", "agent_id": "w", "tool": "Bash", "fingerprint": "abc"})["agent_id"] == "w"
