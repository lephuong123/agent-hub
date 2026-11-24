"""Schema cấu hình một MCP server trong registry (FR-MC-01).

MCP bên thứ ba do `claude` quản lý theo tài khoản (cấu hình + token trong `<config_dir>/.claude.json` / Keychain —
docs/dev-notes/plan-mcp-claude-native.md). Registry của hub chỉ giữ metadata an toàn:

```yaml
<tên>:
  transport: stdio | http | sse
  url: "https://host[:port]"       # CHỈ scheme://host[:port] (host cho sandbox) — http/sse
  tools: {include: ["*_tim*"], exclude: [], require_approval: [], annotations: {}}  # glob
  trust: trusted | untrusted      # mặc định untrusted; giá trị lạ = untrusted (fail-closed, NFR-11)
  timeout_s: 60
  description: "..."
  source: claude
```

Hub KHÔNG còn tự giữ cách chạy/kết nối server bên thứ ba (command/args/env/url đầy đủ/headers/OAuth — gỡ ở v0.2.32; migration
v34 đổi mọi dòng cũ sang nguồn claude). Dòng cũ đọc lại (bản sao lưu/gói hub cũ) được chấp nhận: các trường kiểu hub bị bỏ khi đọc.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
TRUST_VALUES = ("trusted", "untrusted")
TRUST_ALIASES = {"full": "trusted", "trust": "trusted", "trusted": "trusted", "untrusted": "untrusted"}
# Server nội bộ của hub — không nằm trong registry, không được đặt trùng tên.
RESERVED_NAMES = frozenset({"hub-guard", "hub-self", "cfharness", "hub-learning"})
ANNOTATION_OVERRIDE_KEYS = ("destructiveHint", "readOnlyHint")
SOURCE_CLAUDE = "claude"
# Trường của dòng nguồn hub kiểu cũ (≤ v0.2.30) — bỏ khi đọc (`McpServerConfig._drop_legacy`); migration v34 đã gỡ khỏi DB.
LEGACY_HUB_KEYS = ("command", "args", "env", "headers", "auth", "lazy", "supports_parallel_tool_calls")


def normalize_trust(value: Any) -> tuple[str, str | None]:
    """(trust, cảnh báo). Giá trị lạ → untrusted + cảnh báo (FR-MC-01)."""
    if value is None:
        return "untrusted", None
    v = str(value).strip().lower()
    if v in TRUST_ALIASES:
        return TRUST_ALIASES[v], None
    return "untrusted", f"trust '{value}' không hợp lệ — lưu thành 'untrusted'"


class ToolFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include: list[str] = Field(default_factory=list, description="glob tool được dùng; rỗng = mọi tool")
    exclude: list[str] = Field(default_factory=list, description="glob tool bị chặn")
    require_approval: list[str] = Field(
        default_factory=list, description="glob tool luôn qua phê duyệt động dù server trusted (FR-MC-03)"
    )
    # Ghi đè annotations do NGƯỜI đặt (`cfharness mcp annotate`; mcp.update ∈ HUMAN_ONLY): {glob tool: {destructiveHint?, readOnlyHint?}}.
    # Sửa báo nhầm của server (vd playwright đánh dấu browser_navigate/close là destructive). Không gỡ được luật tên-xoá, tài nguyên
    # bảo vệ hay require_approval.
    annotations: dict[str, dict[str, bool]] = Field(default_factory=dict, description="ghi đè annotations theo glob tool (người đặt)")

    @field_validator("annotations")
    @classmethod
    def _annotations(cls, v: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
        for pat, ov in v.items():
            if not isinstance(pat, str) or not pat or len(pat) > 200:
                raise ValueError("annotations: khoá phải là glob tên tool")
            if not isinstance(ov, dict) or not ov:
                raise ValueError(f"annotations[{pat}]: cần {{destructiveHint|readOnlyHint: true|false}}")
            bad = sorted(set(ov) - set(ANNOTATION_OVERRIDE_KEYS))
            if bad:
                raise ValueError(f"annotations[{pat}]: khoá lạ {bad} (được: {', '.join(ANNOTATION_OVERRIDE_KEYS)})")
            if not all(isinstance(x, bool) for x in ov.values()):
                raise ValueError(f"annotations[{pat}]: giá trị phải là true/false")
        return v


class McpServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Literal["stdio", "http", "sse"] = "stdio"
    url: str | None = None
    tools: ToolFilter = Field(default_factory=ToolFilter)
    trust: str = "untrusted"
    timeout_s: float = Field(default=60, gt=0, le=3600)
    description: str = ""
    source: Literal["claude"] = SOURCE_CLAUDE

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy(cls, data: Any) -> Any:
        """Dòng nguồn hub kiểu cũ (manual / claude-import, có command/url đầy đủ/headers/auth): bỏ trường kiểu hub, giữ chính sách.
        url còn `${…}` (secret của hub) → bỏ (không suy được host)."""
        if not isinstance(data, dict):
            return data
        d = {k: v for k, v in data.items() if k not in LEGACY_HUB_KEYS}
        d["source"] = SOURCE_CLAUDE
        if "${" in str(d.get("url") or ""):
            d.pop("url", None)
        return d

    @property
    def managed_by_claude(self) -> bool:
        return True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("tên server: chữ, số, '-', '_', bắt đầu bằng chữ/số, ≤ 63 ký tự")
        if v in RESERVED_NAMES:
            raise ValueError(f"'{v}' là tên dành riêng của hub")
        return v

    @field_validator("trust", mode="before")
    @classmethod
    def _trust(cls, v: Any) -> str:
        return normalize_trust(v)[0]

    @model_validator(mode="after")
    def _url_host_only(self) -> "McpServerConfig":
        """Hub không giữ cách kết nối hay secret — chỉ host (sandbox). url bị rút về `scheme://host[:port]`."""
        if self.url:
            from urllib.parse import urlsplit

            try:
                u = urlsplit(self.url)
                host, port = (u.hostname or ""), u.port
            except ValueError:
                raise ValueError("url không hợp lệ") from None
            if u.scheme not in ("http", "https") or not host:
                raise ValueError("url phải bắt đầu bằng http:// hoặc https:// và có host")
            if self.transport == "stdio":
                raise ValueError("transport stdio không dùng 'url'")
            h = f"[{host}]" if ":" in host else host
            self.url = f"{u.scheme}://{h}" + (f":{port}" if port else "")
        return self

    # -- tiện ích
    def stored(self) -> dict[str, Any]:
        """Dạng lưu registry (bỏ name, bỏ url rỗng)."""
        d = self.model_dump(exclude={"name"})
        if not self.url:
            d.pop("url", None)
        return d


def match_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def annotation_override(tool: str, filt: ToolFilter | dict[str, Any] | None) -> dict[str, bool]:
    """Ghi đè annotations người đặt cho `tool` (gộp mọi glob khớp; glob cụ thể hơn = dài hơn thắng)."""
    ann = (filt.annotations if isinstance(filt, ToolFilter) else (filt or {}).get("annotations")) or {}
    out: dict[str, bool] = {}
    for pat in sorted(ann, key=len):
        if fnmatch.fnmatchcase(tool, pat) and isinstance(ann[pat], dict):
            out.update({k: bool(v) for k, v in ann[pat].items() if k in ANNOTATION_OVERRIDE_KEYS})
    return out


def tool_selected(tool: str, filt: ToolFilter) -> bool:
    """Tool có được dùng theo include/exclude không (include rỗng = tất cả)."""
    if filt.include and not match_any(tool, filt.include):
        return False
    return not match_any(tool, filt.exclude)
