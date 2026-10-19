"""Hook QUAN SÁT của hub (FR-RT-17, hardening H-11): `PostToolUse`, `UserPromptSubmit`, `Stop`.

`python -I -m cfharness.observe_hook <Event> --run <id> --socket <sock> [--timeout 2] [--home <HUB_HOME>]`
(cũng gọi được bằng `cfharness hook <Event> …`).

FAIL-OPEN — ngược với hook guard (`guard/hook.py`, fail-closed):
- luôn `exit 0`, KHÔNG in gì ra stdout (stdout của hook có thể bị CLI hiểu là quyết định);
- gửi RPC `hook.observe` (chỉ metadata: tên event, tool, session) với timeout mặc định 2 s;
- quá giờ / daemon không trả lời → ghi `hook.timeout`: thử gửi notification JSON-RPC (không chờ trả lời) tới daemon,
  nếu cả việc đó cũng hỏng thì ghi một dòng vào `HUB_HOME/logs/hook-timeouts.jsonl` (daemon nạp lúc khởi động).
Chỉ stdlib — chạy ở MỖI tool call.
"""

from __future__ import annotations

import json
import os
import sys
import time

from cfharness import transport
from cfharness.oscompat import utf8_stdio

EVENTS = ("PostToolUse", "UserPromptSubmit", "Stop")
MAX_STDIN = 4 * 1024 * 1024


def _args(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    pos: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and i + 1 < len(argv):
            out[a[2:]] = argv[i + 1]
            i += 2
            continue
        pos.append(a)
        i += 1
    if pos:
        out["event"] = pos[0]
    return out


def _send(sock_path: str, msg: dict, timeout: float, *, wait_reply: bool) -> dict | None:
    s = transport.connect(sock_path, timeout)
    try:
        s.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        if not wait_reply:
            return None
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf:
        raise ConnectionError("daemon đóng kết nối không trả lời")
    return json.loads(buf)


def _spool(home: str | None, rec: dict) -> None:
    if not home:
        return
    try:
        d = os.path.join(home, "logs")
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, "hook-timeouts.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def observe(argv: list[str], stdin_text: str | None = None) -> dict:
    """Thân hook — trả kết quả (cho test); KHÔNG bao giờ ném lỗi."""
    a = _args(argv)
    event = a.get("event", "")
    run_id = a.get("run", "") or os.environ.get("CFHARNESS_RUN_ID", "")
    sock = a.get("socket", "")
    home = a.get("home") or os.environ.get("CFHARNESS_HOME")
    try:
        timeout = float(a.get("timeout") or 2.0)
    except ValueError:
        timeout = 2.0
    try:
        raw = stdin_text if stdin_text is not None else sys.stdin.read(MAX_STDIN)
        data = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    meta = {k: data.get(k) for k in ("session_id", "tool_name", "stop_hook_active") if data.get(k) is not None}
    if event == "UserPromptSubmit":
        meta["prompt_chars"] = len(str(data.get("prompt") or ""))  # chỉ độ dài, không gửi nội dung
    if event not in EVENTS or not sock:
        return {"ok": False, "reason": "bad_args"}
    t0 = time.monotonic()
    try:
        msg = _send(sock, {"jsonrpc": "2.0", "id": 1, "method": "hook.observe", "params": {"run_id": run_id, "event": event, "data": meta}}, timeout, wait_reply=True)
        return {"ok": True, "reply": msg}
    except TimeoutError:
        reason = "timeout"
    except Exception:
        reason = "unreachable"
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    rec = {"run_id": run_id, "event": event, "timeout_s": timeout, "elapsed_ms": elapsed_ms, "reason": reason, "ts": time.time()}
    try:  # notification (không id) — daemon ghi event khi rảnh, hook không chờ
        _send(sock, {"jsonrpc": "2.0", "method": "hook.timeout", "params": rec}, 0.3, wait_reply=False)
        delivered = True
    except Exception:
        delivered = False
    if not delivered:
        _spool(home, rec)
    return {"ok": False, "reason": reason, "elapsed_ms": elapsed_ms, "notified": delivered}


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    try:
        observe(sys.argv[1:] if argv is None else argv)
    except BaseException:  # pragma: no cover - fail-open tuyệt đối
        pass
    return 0


# ---------------------------------------------------------------------------- settings.json (gọi từ RunManager)
def add_observe_hooks(settings: dict, *, run_id: str, socket_path: str, hub_home: str, python: str, cfg: dict | None) -> tuple[dict, list[str]]:
    """Thêm entry hook quan sát vào settings của run. Trả (settings mới, tên event đã thêm — để đưa vào allowed_hooks).

    Hàm RIÊNG (không sửa `guard/runtime.build_settings` của luồng SEC); entry PreToolUse của guard giữ nguyên."""
    import shlex

    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return settings, []
    events = [e for e in (cfg.get("events") or EVENTS) if e in EVENTS]
    t = float(cfg.get("timeout_s") or 2.0)
    cli_t = int(cfg.get("cli_timeout_s") or max(5, int(t) + 3))
    s = dict(settings)
    hooks = dict(s.get("hooks") or {})
    for ev in events:
        cmd = (f"{shlex.quote(python)} -I -m cfharness.observe_hook {ev} --run {shlex.quote(run_id)} --socket {shlex.quote(socket_path)} "
               f"--timeout {t:g} --home {shlex.quote(hub_home)} >/dev/null 2>&1 || true")
        entry: dict = {"hooks": [{"type": "command", "command": cmd, "timeout": cli_t}]}
        if ev == "PostToolUse":
            entry["matcher"] = "*"
        hooks[ev] = list(hooks.get(ev) or []) + [entry]
    s["hooks"] = hooks
    return s, events


if __name__ == "__main__":
    sys.exit(main())
