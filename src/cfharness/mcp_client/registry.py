"""Registry MCP server cho agent (FR-MC-01..06).

Nguồn sự thật: bảng `mcp_servers` trong `hub.db` (daemon là writer duy nhất). `HUB_HOME/mcp/registry.yaml`
là BẢN SAO chỉ để đọc (sinh lại sau mỗi thay đổi; dạng `{servers: {tên: cấu hình}}`) — giữ tương thích
với `agents.load_mcp_registry` của M0 và để người xem nhanh. Sửa tay file này không có tác dụng.

Xoá = xoá mềm: `deleted_at` + bản sao `trash/mcp/<tên>__<ts>.yaml`; `mcp restore` khôi phục.

Mọi server đều nguồn `claude` (cấu hình + token do claude giữ theo tài khoản): registry chỉ giữ chính sách — trust, tools
(include/exclude/require_approval/annotations), mô tả, host. Dòng mới chỉ tạo qua `claude_source.register`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

import yaml
from pydantic import ValidationError

from ..errors import Conflict, HubError, NotFound
from ..events import make_event
from ..paths import HubPaths
from ..secrets import SecretStore
from .config import SOURCE_CLAUDE, McpServerConfig, annotation_override, match_any, normalize_trust, tool_selected

log = logging.getLogger("cfharness.mcp_client")

JSON_FIELDS = ("config", "tools_include", "tools_exclude", "annotations_cache")


def _now() -> str:
    from ..events import iso

    return iso()


def _validation_message(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'cấu hình'}: {err['msg']}" for err in e.errors())


UPDATABLE = ("trust", "tools", "description", "timeout_s")


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
        row["agents"] = self.agents_using(name)
        return row

    # ------------------------------------------------------------------ ghi
    def _check_writable(self) -> None:
        if not self.writable:  # pragma: no cover
            raise HubError("registry mở chế độ chỉ đọc (chỉ daemon được ghi)")

    def _write_mirror(self) -> None:
        servers = {r["name"]: r["config"] for r in self.list()}
        p = self.paths.mcp_registry
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".yaml.tmp")
        header = "# BẢN SAO do cfharness sinh từ hub.db — KHÔNG sửa tay; dùng `cfharness mcp claude add` / `cfharness mcp edit|rm`.\n"
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

    def _emit(self, op: str, name: str, extra: dict[str, Any] | None = None) -> None:
        try:
            self.publish(make_event("mcp.changed", None, {"op": op, "name": name, **(extra or {})}))
        except Exception:  # pragma: no cover
            log.exception("không phát được mcp.changed")

    def add(self, data: dict[str, Any]) -> dict[str, Any]:
        """Tạo dòng (chỉ `claude_source.register` gọi — đã kiểm server có trong cấu hình claude của tài khoản)."""
        self._check_writable()
        cfg, warnings = self._validate(data)
        existing = self.get_row(cfg.name, include_deleted=True)
        if existing is not None:
            hint = " (đang xoá mềm — dùng `cfharness mcp restore`)" if existing.get("deleted_at") else ""
            raise Conflict(f"MCP server '{cfg.name}' đã tồn tại{hint}")
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
        return out

    def update(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Sửa chính sách: trust, tools (gộp), description, timeout_s. Cách kết nối do claude giữ — sửa bằng `cfharness mcp claude`."""
        self._check_writable()
        row = self.require(name)
        if "name" in patch and patch["name"] != name:
            raise HubError("không đổi được tên server (xoá rồi thêm lại)")
        bad = sorted(k for k in patch if k not in UPDATABLE and k != "name")
        if bad:
            raise HubError(f"không sửa được {', '.join(bad)} — cấu hình kết nối do claude giữ theo tài khoản "
                           "(`cfharness mcp claude rm` rồi `cfharness mcp claude add`); hub chỉ giữ " + ", ".join(UPDATABLE), code=-32602)
        cur = dict(row["config"])
        for k, v in patch.items():
            if k == "name":
                continue
            if k == "tools" and isinstance(v, dict):
                cur["tools"] = {**(cur.get("tools") or {}), **{kk: vv for kk, vv in v.items() if vv is not None}}
            else:
                cur[k] = v
        cfg, warnings = self._validate({"name": name, **cur})
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

    # ------------------------------------------------------------------ hợp đồng cho runtime / guard
    def tool_annotations(self, server: str, tool: str) -> dict[str, Any] | None:
        row = self.get_row(server)
        if row is None:
            return None
        cache = row.get("annotations_cache") or {}
        rec = cache.get(tool)
        cfg = row["config"]
        tools = cfg.get("tools") or {}
        ov = annotation_override(tool, tools)
        # hub không probe server (cấu hình + token do claude giữ) → guard dùng ghi đè của người, annotations còn lại từ lần probe của
        # bản cũ (≤ v0.2.30, cột annotations_cache), nếu không thì suy theo TÊN tool (guard/classifier.classify_mcp — `mcp_claude_*`)
        extra: dict[str, Any] = {"source": SOURCE_CLAUDE, "probed": rec is not None}
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

    def claude_source_servers(self, server_names: list[str]) -> list[str]:
        """Server trong `server_names`, giữ thứ tự — mọi server đều nguồn claude. Server gán mà không có / đã xoá mềm → HubError
        (fail-closed: run không chạy thiếu server)."""
        out: list[str] = []
        for name in server_names:
            row = self.get_row(name, include_deleted=True)
            if row is None:
                raise HubError(f"MCP server '{name}' của agent không có trong registry", data={"server": name})
            if row.get("deleted_at"):
                raise HubError(f"MCP server '{name}' của agent đã bị xoá mềm — `cfharness mcp restore {name}` hoặc gỡ khỏi agent", data={"server": name})
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
                # đạt được bằng cờ CLI → hook guard áp theo `selected`.
                for pat in cfg.tools.exclude:
                    if not _is_glob(pat):
                        disallowed.append(f"mcp__{name}__{pat}")
                for pat in cfg.tools.include:
                    if _is_glob(pat):
                        continue
                    if cfg.trust == "trusted" and pat not in cfg.tools.exclude and not match_any(pat, cfg.tools.require_approval):
                        allowed.append(f"mcp__{name}__{pat}")
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
