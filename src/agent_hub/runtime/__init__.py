from .base import AgentRuntime, OneShotRequest, OneShotResult, RawLog, RunHandle, RunSpec, RuntimeError_
from .claude_cli import ArgvError, ClaudeCliRuntime, build_argv, claude_version
from .fake import FakeRuntime, default_fixture

__all__ = [
    "AgentRuntime",
    "ArgvError",
    "ClaudeCliRuntime",
    "FakeRuntime",
    "OneShotRequest",
    "OneShotResult",
    "RawLog",
    "RunHandle",
    "RunSpec",
    "RuntimeError_",
    "build_argv",
    "claude_version",
    "default_fixture",
    "make_runtime",
]


def make_runtime(cfg: dict) -> AgentRuntime:
    """Tạo runtime từ cấu hình hub (`runtime.kind`)."""
    rt = cfg.get("runtime") or {}
    kind = rt.get("kind", "claude")
    if kind == "fake":
        f = rt.get("fake") or {}
        return FakeRuntime(f.get("fixture"), line_delay_s=float(f.get("line_delay_s") or 0.0))
    return ClaudeCliRuntime(rt.get("claude") or {})
