"""Registry MCP server cho agent (FR-MC-01..06).

Nguồn sự thật: bảng `mcp_servers` trong `hub.db` (daemon là writer duy nhất). `HUB_HOME/mcp/registry.yaml`
là BẢN SAO chỉ để đọc (sinh lại sau mỗi thay đổi; dạng `{servers: {tên: cấu hình}}`) — giữ tương thích
với `agents.load_mcp_registry` của M0 và để người xem nhanh. Sửa tay file này không có tác dụng.

Xoá = xoá mềm: `deleted_at` + bản sao `trash/mcp/<tên>__<ts>.yaml`; `mcp restore` khôi phục.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import ValidationError

from ..errors import Conflict, HubError, NotFound
from ..events import make_event
from ..paths import HubPaths
from ..secrets import SecretStore, is_ref, normalize_name
from .config import SOURCE_CLAUDE, McpServerConfig, annotation_override, match_any, normalize_trust, tool_selected
from .importer import _Extractor, convert_entry, read_claude_servers

log = logging.getLogger("cfharness.mcp_client")

JSON_FIELDS = ("config", "tools_include", "tools_exclude", "annotations_cache")


def _now() -> str:
    from ..events import iso

    return iso()


def _validation_message(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'cấu hình'}: {err['msg']}" for err in e.errors())


class McpRegistry:
    def __init__(
        self,
        conn_provider: Callable[[], sqlite3.Connection],
        paths: HubPaths,
        secrets: SecretStore,
        *,
        publish: Callable[[Any], Any] | None = None,
        agents_using: Callable[[str], list[str]] | None = None,
        writable: bool = True,
    ) -> None:
        self._conn_provider = conn_provider
        self.paths = paths
        self.secrets = secrets
        self.publish = publish or (lambda ev: None)
        self.agents_using = agents_using or (lambda name: [])
        self.writable = writable

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn_provider()

    # ------------------------------------------------------------------ đọc
    def _row(self, r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        for k in JSON_FIELDS:
            if d.get(k) is not None:
                d[k] = json.loads(d[k])
        return d

    def get_row(self, name: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
        r = self._row(self.conn.execute("SELECT * FROM mcp_servers WHERE name=?", (name,)).fetchone())
        if r is None or (r.get("deleted_at") and not include_deleted):
            return None
        return r

    def require(self, name: str, *, include_deleted: bool = False) -> dict[str, Any]:
        r = self.get_row(name, include_deleted=include_deleted)
        if r is None:
            raise NotFound(f"không có MCP server '{name}' trong registry (`cfharness mcp list`)")
        return r

    def config_of(self, row: dict[str, Any]) -> McpServerConfig:
        return McpServerConfig(name=row["name"], **row["config"])

    def list(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM mcp_servers" + ("" if include_deleted else " WHERE deleted_at IS NULL") + " ORDER BY name"
        return [self._row(r) for r in self.conn.execute(sql)]  # type: ignore[misc]

    def show(self, name: str) -> dict[str, Any]:
        row = self.require(name, include_deleted=True)
        cfg = row["config"]
        refs_missing = self.secrets.missing({"env": cfg.get("env"), "headers": cfg.get("headers"), "args": cfg.get("args"), "url": cfg.get("url"),
                                             "auth": (cfg.get("auth") or {}).get("client_secret_ref")})
        row["missing_secrets"] = refs_missing
        row["agents"] = self.agents_using(name)
        self._add_oauth(row)
        return row

    def _add_oauth(self, row: dict[str, Any]) -> dict[str, Any]:
        """Dòng OAuth kiểu cũ (nguồn hub, đã gỡ từ v0.2.28 — `legacy_oauth.py`) → `oauth_status = needs_migration` +
        `migrate_cmd`. Không còn trạng thái token (hub không giữ OAuth nữa)."""
        from .legacy_oauth import STATUS, migrate_cmd, needs_migration

        if needs_migration(row.get("config") or {}):
            row["oauth_status"] = STATUS
            row["migrate_cmd"] = migrate_cmd(row["name"])
        return row

    def with_oauth(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._add_oauth(r) for r in rows]

    # ------------------------------------------------------------------ ghi
    def _check_writable(self) -> None:
        if not self.writable:  # pragma: no cover
            raise HubError("registry mở chế độ chỉ đọc (chỉ daemon được ghi)")

    def _write_mirror(self) -> None:
        servers = {r["name"]: r["config"] for r in self.list()}
        p = self.paths.mcp_registry
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".yaml.tmp")
        header = "# BẢN SAO do cfharness sinh từ hub.db — KHÔNG sửa tay; dùng `cfharness mcp add|edit|rm`.\n"
        tmp.write_text(header + yaml.safe_dump({"servers": servers}, allow_unicode=True, sort_keys=True), encoding="utf-8")
        tmp.replace(p)

    def _validate(self, data: dict[str, Any]) -> tuple[McpServerConfig, list[str]]:
        warnings: list[str] = []
        d = dict(data)
        if "trust" in d:
            d["trust"], w = normalize_trust(d["trust"])
            if w:
                warnings.append(w)
        if isinstance(d.get("tools"), dict):
            d["tools"] = {k: v for k, v in d["tools"].items() if v is not None}
        try:
            cfg = McpServerConfig(**{k: v for k, v in d.items() if v is not None})
        except ValidationError as e:
            raise HubError(f"cấu hình MCP không hợp lệ: {_validation_message(e)}") from None
        return cfg, warnings

    def _extract_literal_secrets(self, cfg: McpServerConfig) -> tuple[McpServerConfig, list[str]]:
        """Giá trị secret dạng rõ trong env/header/args/url → secret store, cấu hình giữ `${…}` (FR-MC-05)."""
        ex = _Extractor(cfg.name)
        d = cfg.model_dump()
        d["env"] = {k: ex.kv(k, v) for k, v in d["env"].items()}
        d["headers"] = {k: ex.kv(k, v) for k, v in d["headers"].items()}
        d["args"] = ex.args(d["args"])
        if d.get("url"):
            d["url"] = ex.url(d["url"])
        auth = d.get("auth")
        if auth and auth.get("client_secret_ref") and not is_ref(auth["client_secret_ref"]):
            # client_secret nhập dạng rõ (OAuth client đăng ký tay) → secret store, cấu hình chỉ giữ ${…}
            n = normalize_name(f"MCP_{cfg.name}_OAUTH_CLIENT_SECRET")
            ex.secrets[n] = str(auth["client_secret_ref"])
            auth["client_secret_ref"] = f"${{{n}}}"
        for n, v in ex.secrets.items():
            self.secrets.set(n, v)
        return McpServerConfig(**d), sorted(ex.secrets)

    @staticmethod
    def _refuse_legacy_oauth(cfg: McpServerConfig) -> None:
        from .legacy_oauth import ADD_REFUSED, needs_migration

        if needs_migration(cfg):
            raise HubError(ADD_REFUSED, data={"oauth": "removed"})

    def _emit(self, op: str, name: str, extra: dict[str, Any] | None = None) -> None:
        try:
            self.publish(make_event("mcp.changed", None, {"op": op, "name": name, **(extra or {})}))
        except Exception:  # pragma: no cover
            log.exception("không phát được mcp.changed")

    def add(self, data: dict[str, Any], *, allow_legacy_oauth: bool = False) -> dict[str, Any]:
        """`allow_legacy_oauth` CHỈ cho test dựng dòng OAuth kiểu cũ (hub cũ còn sót) — đường thường từ chối (`legacy_oauth.ADD_REFUSED`)."""
        self._check_writable()
        cfg, warnings = self._validate(data)
        if not allow_legacy_oauth:
            self._refuse_legacy_oauth(cfg)
        existing = self.get_row(cfg.name, include_deleted=True)
        if existing is not None:
            hint = " (đang xoá mềm — dùng `cfharness mcp restore`)" if existing.get("deleted_at") else ""
            raise Conflict(f"MCP server '{cfg.name}' đã tồn tại{hint}")
        cfg, moved = self._extract_literal_secrets(cfg)
        now = _now()
        with _tx(self.conn):
            self.conn.execute(
                "INSERT INTO mcp_servers (name, transport, config, trust, tools_include, tools_exclude, source, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    cfg.name,
                    cfg.transport,
                    json.dumps(cfg.stored(), ensure_ascii=False),
                    cfg.trust,
                    json.dumps(cfg.tools.include),
                    json.dumps(cfg.tools.exclude),
                    cfg.source,
                    now,
                    now,
                ),
            )
        self._write_mirror()
        self._emit("added", cfg.name, {"trust": cfg.trust, "transport": cfg.transport})
        out = self.show(cfg.name)
        out["warnings"] = warnings
        out["secrets_moved"] = moved
        return out

    def update(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        self._check_writable()
        row = self.require(name)
        if "name" in patch and patch["name"] != name:
            raise HubError("không đổi được tên server (xoá rồi thêm lại)")
        cur = dict(row["config"])
        if "source" in patch and (str(patch["source"] or "") == SOURCE_CLAUDE) != (cur.get("source") == SOURCE_CLAUDE):
            raise HubError("không đổi được nguồn server hub ↔ claude (xoá rồi thêm lại)")
        new_t = patch.get("transport")
        if new_t and new_t != cur.get("transport", "stdio"):
            # Đổi loại transport: bỏ trường riêng của loại cũ (patch là phép gộp — không bỏ thì stdio→http vướng 'command' cũ).
            drop = ("command", "args", "env") if new_t in ("http", "sse") else (("url", "headers", "auth") if new_t == "stdio" else ())
            for k in drop:
                if k not in patch:
                    cur.pop(k, None)
        for k, v in patch.items():
            if k == "name":
                continue
            if k == "tools" and isinstance(v, dict):
                cur["tools"] = {**(cur.get("tools") or {}), **{kk: vv for kk, vv in v.items() if vv is not None}}
            elif k in ("env", "headers") and isinstance(v, dict):
                merged = {**(cur.get(k) or {}), **v}
                cur[k] = {kk: vv for kk, vv in merged.items() if vv is not None}  # None = xoá khoá
            else:
                cur[k] = v
        cfg, warnings = self._validate({"name": name, **cur})
        if "auth" in patch:  # sửa dòng OAuth kiểu cũ (mô tả, trust…) vẫn được; THÊM/đổi auth OAuth nguồn hub thì không
            self._refuse_legacy_oauth(cfg)
        cfg, moved = self._extract_literal_secrets(cfg)
        with _tx(self.conn):
            self.conn.execute(
                "UPDATE mcp_servers SET transport=?, config=?, trust=?, tools_include=?, tools_exclude=?, updated_at=? WHERE name=?",
                (
                    cfg.transport,
                    json.dumps(cfg.stored(), ensure_ascii=False),
                    cfg.trust,
                    json.dumps(cfg.tools.include),
                    json.dumps(cfg.tools.exclude),
                    _now(),
                    name,
                ),
            )
        self._write_mirror()
        self._emit("updated", name, {"changed": sorted(patch)})
        out = self.show(name)
        out["warnings"] = warnings
        out["secrets_moved"] = moved
        return out

    def convert_to_claude(self, name: str, info: dict[str, Any]) -> dict[str, Any]:
        """Đổi một dòng nguồn hub sang nguồn claude TẠI CHỖ (`mcp.migrate_to_claude`, plan-mcp-claude-native PR4): giữ tên (nên mọi
        agent đã gán giữ nguyên), trust, `tools` (include/exclude/require_approval/annotations ghi đè của người), mô tả, timeout và
        `annotations_cache` (cột riêng); bỏ cách kết nối + `auth` (claude giữ). `info` = mục `claude_source.read_servers`."""
        self._check_writable()
        row = self.require(name)
        cur = dict(row["config"])
        if cur.get("source") == SOURCE_CLAUDE:
            raise Conflict(f"MCP '{name}' đã là nguồn claude")
        keep = {k: cur[k] for k in ("tools", "trust", "description", "timeout_s") if k in cur}
        data: dict[str, Any] = {"name": name, **keep, "source": SOURCE_CLAUDE, "transport": info["transport"]}
        if info.get("host"):
            data["url"] = f"https://{info['host']}"
            raw = str(cur.get("url") or "")
            if "${" not in raw and raw.lower().startswith(("http://", "https://")):
                data["url"] = raw  # validator rút về scheme://host[:port] (giữ http loopback / cổng)
        cfg, warnings = self._validate(data)
        with _tx(self.conn):
            self.conn.execute(
                "UPDATE mcp_servers SET transport=?, config=?, trust=?, tools_include=?, tools_exclude=?, source=?, updated_at=? WHERE name=?",
                (cfg.transport, json.dumps(cfg.stored(), ensure_ascii=False), cfg.trust, json.dumps(cfg.tools.include),
                 json.dumps(cfg.tools.exclude), cfg.source, _now(), name),
            )
        self._write_mirror()
        self._emit("migrated", name, {"source": SOURCE_CLAUDE})
        out = self.show(name)
        out["warnings"] = warnings
        return out

    def remove(self, name: str, *, force: bool = False) -> dict[str, Any]:
        """Xoá mềm. Đang được agent dùng → từ chối trừ khi `force` (run của agent đó sẽ báo lỗi rõ ràng)."""
        self._check_writable()
        row = self.require(name)
        users = self.agents_using(name)
        if users and not force:
            raise Conflict(
                f"MCP server '{name}' đang được agent dùng: {', '.join(users)} — gỡ khỏi agent trước hoặc dùng --force",
                data={"agents": users},
            )
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        trash = self.paths.trash_dir / "mcp" / f"{name}__{ts}.yaml"
        trash.parent.mkdir(parents=True, exist_ok=True)
        trash.write_text(yaml.safe_dump({"name": name, **row}, allow_unicode=True, sort_keys=True), encoding="utf-8")
        now = _now()
        with _tx(self.conn):
            self.conn.execute("UPDATE mcp_servers SET deleted_at=?, trash_path=?, updated_at=? WHERE name=?", (now, str(trash), now, name))
        self._write_mirror()
        self._emit("removed", name, {"soft": True, "agents": users})
        return self.show(name)

    def restore(self, name: str) -> dict[str, Any]:
        self._check_writable()
        row = self.require(name, include_deleted=True)
        if not row.get("deleted_at"):
            raise HubError(f"MCP server '{name}' không ở trạng thái đã xoá")
        with _tx(self.conn):
            self.conn.execute("UPDATE mcp_servers SET deleted_at=NULL, trash_path=NULL, updated_at=? WHERE name=?", (_now(), name))
        self._write_mirror()
        self._emit("restored", name)
        return self.show(name)

    # ------------------------------------------------------------------ probe
    def runtime_entry(self, cfg: McpServerConfig) -> dict[str, Any]:
        """Entry đã thay secret. CHỈ trong bộ nhớ / file runtime 0600 — không bao giờ trả qua RPC, ghi DB/log.
        Dòng OAuth kiểu cũ (nguồn hub, đã gỡ từ v0.2.28) → HubError `oauth=needs_migration` (fail-closed: run không chạy thiếu server)."""
        from .legacy_oauth import needs_migration, needs_migration_error

        if needs_migration(cfg):
            raise needs_migration_error(cfg.name)
        return self.secrets.resolve(cfg.claude_entry())

    def resolved_entry(self, name: str) -> dict[str, Any]:
        """Entry `mcpServers` đã thay secret (chỉ dùng trong bộ nhớ / file runtime 0600)."""
        cfg = self.config_of(self.require(name))
        return self.runtime_entry(cfg)

    async def probe(self, name: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        from .probe import probe_server

        row = self.require(name)
        cfg = self.config_of(row)
        if cfg.managed_by_claude:  # token do claude giữ → hub không kết nối được; gắn nhãn tay: `cfharness mcp annotate`
            return {"ok": False, "tools": {}, "elapsed_ms": 0, "name": name, "trust": cfg.trust, "warnings": [],
                    "error": "server nguồn claude: hub không probe (cấu hình + token do claude giữ)",
                    "hint": f"gắn nhãn tool bằng `cfharness mcp annotate {name} <tool> --read-only|--destructive`"}
        try:
            resolved = self.runtime_entry(cfg)
        except HubError as e:
            from .legacy_oauth import migrate_cmd

            oauth_err = isinstance(e.data, dict) and e.data.get("oauth")
            res = {"ok": False, "tools": {}, "elapsed_ms": 0, "error": e.message,
                   "hint": (migrate_cmd(name) if oauth_err else "đặt secret còn thiếu rồi probe lại")}
        else:
            res = await probe_server(cfg, resolved, timeout_s=timeout_s)
            if not res["ok"] and cfg.auth is None and cfg.transport != "stdio" and "401" in str(res.get("error") or ""):
                res["hint"] = (f"server đòi xác thực (401) — nếu là OAuth: gỡ (`cfharness mcp rm {name}`) rồi thêm vào claude theo tài khoản "
                               f"`cfharness mcp claude add {name} --account <id> --url <url>` + `cfharness mcp claude login {name} --account <id>`")
        now = _now()
        old = row.get("annotations_cache") or {}
        if res["ok"]:
            res["new_tools"] = sorted(set(res["tools"]) - set(old)) if old else []
            res["removed_tools"] = sorted(set(old) - set(res["tools"])) if old else []
            if self.writable:
                with _tx(self.conn):
                    self.conn.execute(
                        "UPDATE mcp_servers SET annotations_cache=?, last_probe_at=?, probe_error=NULL, updated_at=? WHERE name=?",
                        (json.dumps(res["tools"], ensure_ascii=False), now, now, name),
                    )
        elif self.writable:
            with _tx(self.conn):
                self.conn.execute("UPDATE mcp_servers SET probe_error=?, updated_at=? WHERE name=?", (res.get("error"), now, name))
        warnings = []
        for t, rec in res["tools"].items():
            if rec.get("suspicious"):
                warnings.append(f"tool '{t}' có mô tả đáng ngờ ({'; '.join(rec['suspicious'])}) — mặc định bị chặn")
        res["warnings"] = warnings
        res["name"] = name
        res["trust"] = cfg.trust
        self._emit("probed", name, {"ok": res["ok"], "tools": len(res["tools"]), "elapsed_ms": res.get("elapsed_ms")})
        return res

    # ------------------------------------------------------------------ import
    def import_claude(
        self,
        path: str | os.PathLike[str],
        *,
        project: str | None = None,
        select: list[str] | None = None,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        src = Path(path).expanduser()
        servers = read_claude_servers(src, project)
        names = sorted(servers)
        if select is not None:
            unknown = sorted(set(select) - set(names))
            if unknown:
                raise HubError(f"không có trong {src}: {', '.join(unknown)}", data={"available": names})
            names = [n for n in names if n in set(select)]
        items: list[dict[str, Any]] = []
        for n in names:
            item: dict[str, Any] = {"name": n}
            try:
                cfg_dict, secrets, notes = convert_entry(n, servers[n])
                cfg, warnings = self._validate(cfg_dict)
            except HubError as e:
                item.update(status="error", error=e.message)
                items.append(item)
                continue
            item.update(transport=cfg.transport, trust=cfg.trust, secrets=sorted(secrets), notes=notes + warnings)
            existing = self.get_row(n, include_deleted=True)
            if existing is not None and not overwrite:
                item["status"] = "skipped_exists"
                items.append(item)
                continue
            if dry_run:
                item["status"] = "would_import"
                items.append(item)
                continue
            if not self.writable:  # pragma: no cover
                raise HubError("registry chỉ đọc")
            for sn, sv in secrets.items():
                self.secrets.set(sn, sv)
            now = _now()
            with _tx(self.conn):
                if existing is not None:
                    self.conn.execute("DELETE FROM mcp_servers WHERE name=?", (n,))
                self.conn.execute(
                    "INSERT INTO mcp_servers (name, transport, config, trust, tools_include, tools_exclude, source, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (n, cfg.transport, json.dumps(cfg.stored(), ensure_ascii=False), cfg.trust, json.dumps(cfg.tools.include), json.dumps(cfg.tools.exclude), cfg.source, now, now),
                )
            item["status"] = "imported" if existing is None else "overwritten"
            items.append(item)
        if not dry_run and any(i.get("status") in ("imported", "overwritten") for i in items):
            self._write_mirror()
            self._emit("imported", str(src), {"count": sum(1 for i in items if i.get("status") in ("imported", "overwritten"))})
        return {"source": str(src), "project": project, "dry_run": dry_run, "available": sorted(servers), "items": items}

    # ------------------------------------------------------------------ hợp đồng cho runtime / guard
    def tool_annotations(self, server: str, tool: str) -> dict[str, Any] | None:
        row = self.get_row(server)
        if row is None:
            return None
        cache = row.get("annotations_cache") or {}
        rec = cache.get(tool)
        cfg = row["config"]
        claude_src = cfg.get("source") == SOURCE_CLAUDE
        if rec is None and not claude_src:
            return None
        tools = cfg.get("tools") or {}
        ov = annotation_override(tool, tools)
        extra: dict[str, Any] = {}
        if claude_src:
            # nguồn claude: hub không probe được → không có annotations của server; guard dùng ghi đè của người, nếu không thì
            # suy theo TÊN tool (guard/classifier.classify_mcp — `mcp_claude_*`). `probed` = có bản ghi annotations thật
            extra = {"source": SOURCE_CLAUDE, "probed": rec is not None}
        return {
            **(rec or {}),
            **extra,
            **ov,  # ghi đè của NGƯỜI (tools.annotations) thắng annotations server tự khai
            "human_override": ov or None,
            "server": server,
            "trust": row.get("trust") or "untrusted",
            "require_approval": match_any(tool, tools.get("require_approval") or []),
            "selected": tool_selected(tool, self.config_of(row).tools),
            "last_probe_at": row.get("last_probe_at"),
        }

    def build_for_agent_servers(self, server_names: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name in server_names:
            row = self.get_row(name, include_deleted=True)
            if row is None:
                raise HubError(f"MCP server '{name}' của agent không có trong registry", data={"server": name})
            if row.get("deleted_at"):
                raise HubError(f"MCP server '{name}' của agent đã bị xoá mềm — `cfharness mcp restore {name}` hoặc gỡ khỏi agent", data={"server": name})
            if (row.get("config") or {}).get("source") == SOURCE_CLAUDE:
                continue  # claude tự nạp từ cấu hình của tài khoản (runs.py: đường connector + kiểm tồn tại)
            out[name] = self.runtime_entry(self.config_of(row))
        return out

    def claude_source_servers(self, server_names: list[str]) -> list[str]:
        """Server nguồn claude (chưa xoá mềm) trong `server_names`, giữ thứ tự."""
        out: list[str] = []
        for name in server_names:
            row = self.get_row(name)
            if row is not None and (row.get("config") or {}).get("source") == SOURCE_CLAUDE:
                out.append(name)
        return out

    def tool_rules_for_servers(self, server_names: list[str]) -> dict[str, list[str]]:
        allowed: list[str] = []
        disallowed: list[str] = []
        warnings: list[str] = []
        for name in server_names:
            row = self.get_row(name)
            if row is None:
                continue
            cfg = self.config_of(row)
            cache = row.get("annotations_cache") or {}
            if not cache:
                if cfg.trust == "trusted" and not cfg.tools.include and not cfg.tools.exclude and not cfg.tools.require_approval:
                    allowed.append(f"mcp__{name}")
                    continue
                # hardening H-13 (FR-MC-02/03): chưa probe → vẫn sinh luật cho mẫu KHÔNG glob (tên tool cụ thể):
                # exclude → --disallowedTools; include (trusted, không require_approval) → --allowedTools. Mẫu glob không diễn
                # đạt được bằng cờ CLI → cảnh báo; tool ngoài luật đi qua hook guard (chưa probe = `mcp_unknown_tool` → duyệt).
                globbed = False
                for pat in cfg.tools.exclude:
                    if _is_glob(pat):
                        globbed = True
                    else:
                        disallowed.append(f"mcp__{name}__{pat}")
                for pat in cfg.tools.include:
                    if _is_glob(pat):
                        globbed = True
                    elif cfg.trust == "trusted" and pat not in cfg.tools.exclude and not match_any(pat, cfg.tools.require_approval):
                        allowed.append(f"mcp__{name}__{pat}")
                if (globbed or not (cfg.tools.include or cfg.tools.exclude)) and not cfg.managed_by_claude:
                    # (nguồn claude: không probe được; mẫu glob include/exclude do hook guard áp theo `selected`)
                    warnings.append(f"server '{name}' chưa probe — mẫu glob/không có include không suy được allow/deny theo tool (`cfharness mcp probe {name}`)")
                continue
            for tool, rec in sorted(cache.items()):
                rec = {**rec, **annotation_override(tool, cfg.tools)}
                full = f"mcp__{name}__{tool}"
                explicit = tool in cfg.tools.include
                if not tool_selected(tool, cfg.tools) or (rec.get("suspicious") and not explicit):
                    disallowed.append(full)
                    continue
                if rec.get("destructiveHint") is True or match_any(tool, cfg.tools.require_approval):
                    continue  # để guard/approval của luồng B quyết (FR-SG-01)
                if cfg.trust == "trusted" or rec.get("readOnlyHint") is True:
                    allowed.append(full)
        return {"allowed": allowed, "disallowed": disallowed, "warnings": warnings}


def _is_glob(pat: str) -> bool:
    return any(c in pat for c in "*?[")


class _tx:
    """BEGIN IMMEDIATE … COMMIT trên một connection autocommit (isolation_level=None)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, et, ev, tb) -> None:  # type: ignore[no-untyped-def]
        self.conn.execute("ROLLBACK" if et else "COMMIT")
