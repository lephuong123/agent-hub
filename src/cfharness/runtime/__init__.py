from .acp import AcpRuntime
from .base import AgentRuntime, OneShotRequest, OneShotResult, RawLog, RunHandle, RunSpec, RuntimeError_
from .claude_cli import ArgvError, ClaudeCliRuntime, build_argv, claude_version
from .codex import CodexRuntime
from .fake import FakeRuntime, default_fixture

__all__ = [
    "AcpRuntime",
    "AgentRuntime",
    "ArgvError",
    "ClaudeCliRuntime",
    "CodexRuntime",
    "FakeRuntime",
    "OneShotRequest",
    "OneShotResult",
    "RUNTIME_KINDS",
    "RawLog",
    "RunHandle",
    "RunSpec",
    "RuntimeError_",
    "build_argv",
    "claude_version",
    "default_fixture",
    "make_runtime",
]

# runtime chọn được THEO AGENT (`agents.runtime`, plan-codex-grok-runtime). `fake` chỉ đặt toàn hub (`runtime.kind`) cho test/demo.
RUNTIME_KINDS = ("claude", "codex", "grok")


def make_runtime(cfg: dict, secrets: object = None, kind: str | None = None, hub_home: object = None) -> AgentRuntime:
    """Tạo runtime. `kind` = runtime của agent (None/"claude" → theo `runtime.kind` của hub). `runtime.kind: fake` thắng mọi agent
    (test/demo không bao giờ gọi model thật)."""
    rt = cfg.get("runtime") or {}
    hub_kind = rt.get("kind", "claude")
    if hub_kind == "fake":
        f = rt.get("fake") or {}
        return FakeRuntime(f.get("fixture"), line_delay_s=float(f.get("line_delay_s") or 0.0))
    if kind == "codex":
        return CodexRuntime({**(rt.get("codex") or {}), **({"hub_home": str(hub_home)} if hub_home else {})})
    if kind == "grok":
        return AcpRuntime({**(rt.get("grok") or {}), **({"hub_home": str(hub_home)} if hub_home else {})})
    return ClaudeCliRuntime(rt.get("claude") or {}, secrets=secrets)
