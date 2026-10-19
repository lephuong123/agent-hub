"""Schema cấu hình một MCP server trong registry (FR-MC-01) — mượn Hermes `mcp_servers`.

```yaml
<tên>:
  transport: stdio | http | sse
  command: "..."  args: [...]  env: {KEY: "${SECRET}"}       # stdio
  url: "https://..."  headers: {Authorization: "Bearer ${SECRET}"}   # http/sse
  tools: {include: ["*_tim*"], exclude: [], require_approval: []}  # glob
  trust: trusted | untrusted      # mặc định untrusted; giá trị lạ = untrusted (fail-closed, NFR-11)
  lazy: false                      # mượn Hermes — KHÔNG áp dụng (claude CLI quản lý kết nối), chỉ lưu
  supports_parallel_tool_calls: false   # như trên
  timeout_s: 60                    # timeout probe/kết nối
  description: "..."
```
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


class McpServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    tools: ToolFilter = Field(default_factory=ToolFilter)
    trust: str = "untrusted"
    lazy: bool = False
    supports_parallel_tool_calls: bool = False
    timeout_s: float = Field(default=60, gt=0, le=3600)
    description: str = ""
    source: str = "manual"  # manual | claude-import

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
    def _transport_fields(self) -> "McpServerConfig":
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("transport stdio cần 'command'")
            if self.url:
                raise ValueError("transport stdio không dùng 'url'")
        else:
            if not self.url:
                raise ValueError(f"transport {self.transport} cần 'url'")
            if not re.match(r"^https?://", self.url):
                raise ValueError("url phải bắt đầu bằng http:// hoặc https://")
            if self.command:
                raise ValueError(f"transport {self.transport} không dùng 'command'")
        return self

    # -- tiện ích
    def stored(self) -> dict[str, Any]:
        """Dạng lưu registry (bỏ name, bỏ trường rỗng cho gọn)."""
        d = self.model_dump(exclude={"name"})
        if self.transport == "stdio":
            d.pop("url", None)
            d.pop("headers", None)
        else:
            for k in ("command", "args", "env"):
                d.pop(k, None)
        return d

    def claude_entry(self) -> dict[str, Any]:
        """Entry `mcpServers` cho `claude --mcp-config` (CHƯA thay secret)."""
        if self.transport == "stdio":
            e: dict[str, Any] = {"type": "stdio", "command": self.command, "args": list(self.args)}
            if self.env:
                e["env"] = dict(self.env)
            return e
        e = {"type": self.transport, "url": self.url}
        if self.headers:
            e["headers"] = dict(self.headers)
        return e


def match_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def tool_selected(tool: str, filt: ToolFilter) -> bool:
    """Tool có được dùng theo include/exclude không (include rỗng = tất cả)."""
    if filt.include and not match_any(tool, filt.include):
        return False
    return not match_any(tool, filt.exclude)
