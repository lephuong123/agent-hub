"""Interface `AgentRuntime` (FR-RT-01, NFR-22).

Hub chỉ nói chuyện với runtime qua interface này. Hai cài đặt ở M0:
- `ClaudeCliRuntime`: spawn `claude -p … --output-format stream-json --verbose`.
- `FakeRuntime`: phát lại file JSONL đã ghi (test/demo không tốn tiền).
Cả hai cùng đi qua `ClaudeStreamAdapter` nên chuỗi event chuẩn hoá giống nhau.
"""

from __future__ import annotations

import abc
import gzip
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from ..claude_adapter import ClaudeStreamAdapter
from ..events import HubEvent


@dataclass
class RunSpec:
    run_id: str
    agent_id: str
    prompt: str
    cwd: Path
    session_id: str
    raw_log_path: Path
    stderr_log_path: Path
    resume_session_id: str | None = None
    fork: bool = False
    model: str | None = None
    effort: str | None = None
    fallback_model: str | None = None
    system_prompt: str | None = None
    tool_policy: dict[str, Any] = field(default_factory=dict)
    mcp_config_path: Path | None = None
    settings_path: Path | None = None
    max_budget_usd: float | None = None
    display_name: str | None = None
    workspace_kind: Literal["scratch", "dir", "worktree"] = "scratch"
    add_dirs: list[Path] = field(default_factory=list)
    allowed_hooks: list[str] | None = None
    allowed_mcp_servers: list[str] | None = None
    allowed_plugin_prefixes: list[str] | None = None
    require_no_auto_memory: bool = False
    # M1-B: --permission-prompt-tool (kênh phê duyệt thứ hai sau hook, FR-RT-09); env bổ sung cho tiến trình
    permission_prompt_tool: str | None = None
    env_extra: dict[str, str] = field(default_factory=dict)
    # M3: --json-schema (FR-RT-14), --agents (FR-RT-13), -w/--worktree (FR-RT-12 tuỳ chọn)
    json_schema: dict[str, Any] | None = None
    agents_json: dict[str, Any] | None = None
    worktree: str | None = None
    # M4-B: run eval (FR-LN-10) — `--no-session-persistence`, không ghi session ra đĩa
    no_session_persistence: bool = False
    # run điều phối viên có connector claude.ai: ghi đè `runtime.claude.isolation` (setting_sources/strict_mcp_config) cho riêng run
    isolation_overrides: dict[str, Any] | None = None
    allowed_mcp_prefixes: list[str] | None = None
    # agent được người đặt trust full (guard/service.trust_set): cho phép `bypassPermissions` trên mọi workspace (ngoại lệ NFR-15 do người chọn)
    trust_full: bool = False
    # CLI 2.1.303+: bộ quyết định cho `control_request can_use_tool` (vd SandboxNetworkAccess) — async (request, request_id) →
    # {behavior: allow|deny, message?, updatedInput?}. RunManager gắn (đi qua guard); None → runtime từ chối (fail-closed)
    control_handler: Any = None


@dataclass
class RunHandle:
    run_id: str
    spec: RunSpec
    adapter: ClaudeStreamAdapter
    pid: int | None = None
    started_at: float = field(default_factory=time.time)
    argv_redacted: list[str] = field(default_factory=list)
    cli_version: str | None = None
    exit_code: int | None = None
    stop_requested: bool = False
    stderr_tail: str = ""
    raw_log_final: Path | None = None
    internal: dict[str, Any] = field(default_factory=dict)


class RuntimeError_(Exception):
    """Lỗi dựng lệnh / spawn (spawn bị từ chối trước khi chạy)."""


class AgentRuntime(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def capabilities(self) -> dict[str, bool]: ...

    @abc.abstractmethod
    async def start(self, spec: RunSpec) -> RunHandle: ...

    @abc.abstractmethod
    def events(self, handle: RunHandle) -> AsyncIterator[HubEvent]:
        """Event chuẩn hoá của run cho tới khi tiến trình kết thúc. Sau khi cạn: `handle.exit_code` có giá trị."""

    async def send(self, handle: RunHandle, user_message: str) -> None:
        raise NotImplementedError("steer (FR-RT-06) thuộc M2, phụ thuộc spike A-02")

    @abc.abstractmethod
    async def interrupt(self, handle: RunHandle) -> None: ...

    @abc.abstractmethod
    async def stop(self, handle: RunHandle, grace_s: float = 10.0) -> None: ...

    async def resume(self, session_id: str, spec: RunSpec) -> RunHandle:
        return await self.start(replace(spec, resume_session_id=session_id, fork=False))

    async def fork(self, session_id: str, spec: RunSpec) -> RunHandle:
        return await self.start(replace(spec, resume_session_id=session_id, fork=True))

    def version(self) -> str | None:
        return None

    async def oneshot(self, req: "OneShotRequest") -> "OneShotResult":
        """Gọi một lượt KHÔNG session, không tool, output có cấu trúc (`--json-schema`) — cho LLM judge (FR-LN-10) và
        curator gộp skill (FR-LN-12). Hardening H-20 (NFR-22): mọi lời gọi model đi qua AgentRuntime, không spawn `claude` trực tiếp."""
        raise RuntimeError_(f"runtime '{self.name}' không hỗ trợ oneshot")


@dataclass
class OneShotRequest:
    prompt: str
    schema: dict[str, Any]
    system: str
    model: str
    budget_usd: float | None = None
    timeout_s: float = 600.0
    label: str = ""


@dataclass
class OneShotResult:
    ok: bool
    output: dict[str, Any] | None
    cost_usd: float
    duration_ms: int
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class RawLog:
    """Lưu NGUYÊN VĂN stdout của run (FR-RT-04): ghi `runs/<id>.jsonl`, nén `.jsonl.gz` khi xong."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "ab")

    def write(self, line: bytes) -> None:
        from ..redact import mask_bytes  # H-12 (SEC): stdout thô cũng che theo GIÁ TRỊ secret

        line = mask_bytes(line)
        self._f.write(line if line.endswith(b"\n") else line + b"\n")
        self._f.flush()

    def close(self, compress: bool = True) -> Path:
        if self._f.closed:
            return self.path
        self._f.close()
        from ..redact import mask_file_inplace  # H-12 (SEC): stderr của run (`<id>.stderr.log`) che sau khi chạy xong

        mask_file_inplace(self.path.with_name(self.path.name.replace(".jsonl", ".stderr.log")))
        if not compress:
            return self.path
        gz = self.path.with_name(self.path.name + ".gz")
        with open(self.path, "rb") as src, gzip.open(gz, "wb") as dst:
            shutil.copyfileobj(src, dst)
        self.path.unlink()
        return gz


def make_adapter(spec: RunSpec) -> ClaudeStreamAdapter:
    return ClaudeStreamAdapter(
        spec.agent_id,
        spec.run_id,
        allowed_hooks=spec.allowed_hooks,
        allowed_mcp_servers=spec.allowed_mcp_servers,
        allowed_plugin_prefixes=spec.allowed_plugin_prefixes,
        require_no_auto_memory=spec.require_no_auto_memory,
        allowed_mcp_prefixes=spec.allowed_mcp_prefixes,
    )
