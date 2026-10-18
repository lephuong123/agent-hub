"""Secret store (FR-MC-05, NFR-10): file 0600, tham chiếu ${NAME}, không lộ giá trị."""

import os
import stat

import pytest

from agent_hub.errors import HubError
from agent_hub.secrets import SecretMissing, SecretStore, normalize_name, refs_in


def test_set_get_permissions(tmp_path):
    s = SecretStore(tmp_path / "secrets", env_fallback=False)
    name = s.set("internal-mcp token", "sk-very-secret-value-123")
    assert name == "INTERNAL_MCP_TOKEN"
    p = tmp_path / "secrets" / name
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "secrets").stat().st_mode) == 0o700
    assert s.get("internal_mcp_token") == "sk-very-secret-value-123"
    listed = s.list()
    assert [x["name"] for x in listed] == [name]
    assert "sk-very-secret" not in repr(listed)
    assert s.delete(name) and not s.exists(name)


def test_resolve_and_missing(tmp_path, monkeypatch):
    s = SecretStore(tmp_path / "secrets", env_fallback=False)
    s.set("TOK", "abc123")
    cfg = {"headers": {"Authorization": "Bearer ${TOK}"}, "args": ["--x", "${TOK}"], "n": 1}
    assert s.resolve(cfg) == {"headers": {"Authorization": "Bearer abc123"}, "args": ["--x", "abc123"], "n": 1}
    assert refs_in(cfg) == {"TOK"}
    with pytest.raises(SecretMissing) as ei:
        s.resolve({"env": {"A": "${NOPE}"}})
    assert "NOPE" in ei.value.message and "secret set" in ei.value.message
    # env fallback (tương thích ${VAR} sẵn có trong ~/.claude.json)
    monkeypatch.setenv("FROM_ENV_VAR", "envval")
    assert SecretStore(tmp_path / "secrets", env_fallback=True).resolve("${FROM_ENV_VAR}") == "envval"
    assert s.missing({"a": "${FROM_ENV_VAR}"}) == ["FROM_ENV_VAR"]


def test_mask_values_and_bad_names(tmp_path):
    s = SecretStore(tmp_path / "secrets")
    s.set("API_KEY", "zzzz-secret-zzzz")
    assert s.mask_values("lỗi: zzzz-secret-zzzz bị từ chối") == "lỗi: ${API_KEY} bị từ chối"
    with pytest.raises(HubError):
        normalize_name("///")
    with pytest.raises(HubError):
        s.set("X", "")
