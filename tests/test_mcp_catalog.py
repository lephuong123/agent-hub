"""Danh mục MCP Registry (`mcp_client/catalog.py`) + RPC `mcp.catalog`. KHÔNG gọi mạng thật: thay `_http_get_json`/`_http_status`."""

from __future__ import annotations

import json
import urllib.error

import pytest

from cfharness.mcp_client import catalog as C
from cfharness.mcp_server.rpc import MUTATING

FIGMA_PKG = {"server": {"name": "io.github.GLips/Figma-Context-MCP", "description": "Figma data", "version": "0.13.2",
                        "repository": {"url": "https://github.com/GLips/Figma-Context-MCP", "source": "github"},
                        "packages": [{"registryType": "npm", "identifier": "figma-developer-mcp", "version": "0.13.2", "transport": {"type": "stdio"},
                                      "packageArguments": [{"value": "--stdio", "type": "positional"}],
                                      "environmentVariables": [{"name": "FIGMA_API_KEY", "description": "PAT", "isRequired": True, "isSecret": True}]}]},
             "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}}}
REMOTE = {"server": {"name": "com.example/remote", "title": "Ví dụ", "description": "remote", "version": "1.0.0",
                     "websiteUrl": "javascript:alert(1)",
                     "remotes": [{"type": "streamable-http", "url": "https://mcp.example.com/mcp"},
                                 {"type": "sse", "url": "https://{region}.example.com/sse", "variables": {"region": {"isRequired": True}},
                                  "headers": [{"name": "X-Api-Key", "isRequired": True, "isSecret": True}]}]},
          "_meta": {"cfharness/curated": {"oauth": True}}}  # khoá curated trong dữ liệu registry phải bị BỎ QUA
PYPI_OCI = {"server": {"name": "com.x/fetch", "description": "d", "version": "2.0",
                       "packages": [{"registryType": "pypi", "identifier": "mcp-x-fetch", "version": "2.0", "transport": {"type": "stdio"}},
                                    {"registryType": "oci", "identifier": "ghcr.io/x/fetch:2.0", "transport": {"type": "stdio"}},
                                    {"registryType": "npm", "identifier": "x-http", "version": "1", "transport": {"type": "streamable-http"}}]}}


@pytest.fixture(autouse=True)
def _no_net(monkeypatch):
    C.clear_cache()

    def boom(*a, **k):  # mặc định: mọi lời gọi mạng thật bị cấm
        raise AssertionError("test không được gọi mạng thật")

    monkeypatch.setattr(C, "_http_get_json", boom)
    monkeypatch.setattr(C, "_http_status", boom)
    yield
    C.clear_cache()


def test_normalize_and_install_options():
    e = C.normalize(FIGMA_PKG)
    assert e["name"] == "io.github.GLips/Figma-Context-MCP" and e["kinds"] == ["npm"] and e["installable"] and e["status"] == "active"
    [o] = C.install_options(e)
    assert o["transport"] == "stdio" and o["command"] == "npx" and o["args"] == ["-y", "figma-developer-mcp@0.13.2", "--stdio"]
    assert o["env"][0]["name"] == "FIGMA_API_KEY" and o["env"][0]["secret"] and o["env"][0]["required"]

    r = C.normalize(REMOTE)
    assert r["needs_oauth"] is None and not r["curated"]  # registry không tự gắn được nhãn của hub
    assert r["website"] == ""  # link không phải http(s) bị bỏ
    o1, o2 = C.install_options(r)
    assert (o1["transport"], o1["url"]) == ("http", "https://mcp.example.com/mcp")
    assert o2["transport"] == "sse" and o2["variables"][0]["name"] == "region" and o2["headers"][0]["secret"]

    p = C.normalize(PYPI_OCI)
    opts = C.install_options(p)
    assert [(o["kind"], o["command"], o["args"]) for o in opts] == [("pypi", "uvx", ["mcp-x-fetch==2.0"])]  # oci + npm-http: chưa hỗ trợ
    assert C.normalize({"server": {"description": "không tên"}}) is None


def test_suggest_and_secret_names():
    assert C.suggest_name("io.github.microsoft/playwright-mcp") == "playwright-mcp"
    assert C.suggest_name("com.figma.mcp/mcp") == "figma" and C.suggest_name("io.github.miroapp/mcp-server") == "miroapp"
    assert C.suggest_name("x/__weird name!!") == "weird-name"
    assert C.secret_name("figma", "FIGMA_API_KEY") == "MCP_FIGMA_FIGMA_API_KEY"


def test_curated_list_is_valid():
    names = set()
    for it in C.CURATED:
        e = C.normalize(it, trusted_meta=True)
        assert e and e["installable"] and e["curated"], it
        names.add(e["name"])
    assert {"io.github.microsoft/playwright-mcp", "com.atlassian/atlassian-mcp-server", "com.figma.mcp/mcp"} <= names
    figma = C.normalize(next(x for x in C.CURATED if x["server"]["name"] == "com.figma.mcp/mcp"), trusted_meta=True)
    assert figma["needs_oauth"] is True


def test_search_calls_registry_caches_and_paginates(monkeypatch, tmp_path):
    calls = []

    def fake(url, timeout):
        calls.append(url)
        return {"servers": [FIGMA_PKG, REMOTE], "metadata": {"nextCursor": "c2", "count": 2}}

    monkeypatch.setattr(C, "_http_get_json", fake)
    r = C.search("figma", limit=5, cache_dir=tmp_path)
    assert r["source"] == "registry" and r["next_cursor"] == "c2" and len(r["servers"]) == 3
    # trang đầu: mục tuyển chọn khớp (Figma) đứng trước kết quả registry
    assert [s["name"] for s in r["servers"]] == ["com.figma.mcp/mcp", "io.github.GLips/Figma-Context-MCP", "com.example/remote"]
    assert r["servers"][0]["curated"] and not r["servers"][1]["curated"]
    assert "search=figma" in calls[0] and "limit=5" in calls[0] and "version=latest" in calls[0] and calls[0].startswith(C.REGISTRY_URL + "/v0/servers?")
    assert C.search("figma", limit=5, cache_dir=tmp_path)["source"] == "cache" and len(calls) == 1
    C.search("figma", limit=5, cursor="c2", cache_dir=tmp_path)
    assert "cursor=c2" in calls[-1]
    assert list(tmp_path.glob("*.json"))  # cache đĩa


def test_search_falls_back_to_stale_cache_then_curated(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "_http_get_json", lambda url, timeout: {"servers": [FIGMA_PKG], "metadata": {}})
    C.search("figma", cache_dir=tmp_path)
    C.clear_cache()
    # cache đĩa hết hạn + mạng lỗi → dùng bản cũ
    for f in tmp_path.glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        d["ts"] = 0
        f.write_text(json.dumps(d), encoding="utf-8")

    def down(url, timeout):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(C, "_http_get_json", down)
    r = C.search("figma", cache_dir=tmp_path)
    assert r["source"] == "stale-cache" and r["servers"][1]["name"] == "io.github.GLips/Figma-Context-MCP" and "offline" in r["error"]
    # không có cache → danh sách tuyển chọn
    r = C.search("playwright", cache_dir=tmp_path / "empty")
    assert r["source"] == "offline" and [s["name"] for s in r["servers"]] == ["io.github.microsoft/playwright-mcp"] and r["error"]
    # dữ liệu sai dạng cũng là lỗi
    monkeypatch.setattr(C, "_http_get_json", lambda url, timeout: {"nope": 1})
    assert C.search("zzz", cache_dir=tmp_path / "x")["source"] == "offline"
    assert C.search("", offline=True)["servers"]


def test_get_one_and_404_fallback(monkeypatch, tmp_path):
    seen = []

    def fake(url, timeout):
        seen.append(url)
        if "figma.mcp" in url:
            raise urllib.error.HTTPError(url, 404, "nf", None, None)  # type: ignore[arg-type]
        return FIGMA_PKG

    monkeypatch.setattr(C, "_http_get_json", fake)
    e = C.get("io.github.GLips/Figma-Context-MCP", cache_dir=tmp_path)
    assert e["name"] == "io.github.GLips/Figma-Context-MCP" and seen[0].endswith("/v0/servers/io.github.GLips%2FFigma-Context-MCP/versions/latest")
    f = C.get("com.figma.mcp/mcp", cache_dir=tmp_path)  # 404 → mục tuyển chọn
    assert f["curated"] and f["needs_oauth"] is True
    assert C.get("không/có", cache_dir=tmp_path) is None
    assert C.get("com.figma.mcp/mcp", offline=True)["name"] == "com.figma.mcp/mcp"


def test_check_oauth(monkeypatch):
    hits = []

    def status(url, timeout):
        hits.append(url)
        return 401 if "auth" in url else 200

    monkeypatch.setattr(C, "_http_status", status)
    base = {"kind": "remote", "headers": [], "needs_oauth": None}
    assert C.check_oauth({**base, "url": "https://auth.example.com/mcp"}) is True
    assert C.check_oauth({**base, "url": "https://auth.example.com/mcp"}) is True and len(hits) == 1  # cache
    assert C.check_oauth({**base, "url": "https://open.example.com/mcp"}) is False
    assert C.check_oauth({**base, "url": "https://auth.example.com/x", "headers": [{"name": "K", "required": True}]}) is False
    for bad in ("http://auth.example.com/mcp", "https://127.0.0.1/mcp", "https://localhost/mcp", "https://10.0.0.5/mcp",
                "https://{region}.example.com/mcp"):
        assert C.check_oauth({**base, "url": bad}) is None, bad
    assert C.check_oauth({"kind": "npm"}) is None and C.check_oauth({**base, "url": "https://x", "needs_oauth": True}) is True
    assert len(hits) == 2

    def err(url, timeout):
        raise OSError("down")

    monkeypatch.setattr(C, "_http_status", err)
    assert C.check_oauth({**base, "url": "https://down.example.com/mcp"}) is None


def test_mcp_catalog_rpc_is_read_only():
    assert "mcp.catalog" not in MUTATING


def test_curated_list_is_consistent():
    """Danh sách tuyển chọn: tên duy nhất, mỗi mục cài được, có loại; mục OAuth là remote; mục cần client_id tay có chỗ tạo app."""
    entries = [C.normalize(x, trusted_meta=True) for x in C.CURATED]
    assert all(entries) and len(entries) >= 50
    names = [e["name"] for e in entries]
    assert len(names) == len(set(names))
    assert len({C.suggest_name(n) for n in names}) == len(names)  # tên trong hub không đụng nhau
    for e in entries:
        assert e["curated"] and e["category"] in C.CATEGORIES, e["name"]
        opts = C.install_options(e)
        assert opts and e["installable"], e["name"]
        if e["needs_oauth"]:
            assert opts[0]["kind"] == "remote" and opts[0]["needs_oauth"] and opts[0]["url"].startswith("https://"), e["name"]
        if e["oauth_client"] == "manual":
            assert e["needs_oauth"] and e["setup_url"].startswith("https://") and opts[0]["oauth_client"] == "manual", e["name"]
    assert {e["name"] for e in entries if e["oauth_client"] == "manual"} >= {"com.slack/mcp", "com.hubspot/mcp"}


def test_get_prefers_curated_without_network(monkeypatch, tmp_path):
    def boom(url, timeout):
        raise AssertionError("không được gọi mạng cho mục tuyển chọn")

    monkeypatch.setattr(C, "_http_get_json", boom)
    e = C.get("com.notion/mcp", cache_dir=tmp_path)
    assert e["title"] == "Notion" and e["needs_oauth"] and e["oauth_client"] is None and e["remotes"][0]["url"] == "https://mcp.notion.com/mcp"
    sq = C.get("com.squareup/mcp", cache_dir=tmp_path)
    assert C.install_options(sq)[0]["transport"] == "sse"
