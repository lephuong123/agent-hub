"""Hook `PreToolUse` của hub: `python -I -m cfharness.guard.hook --run <id> --socket <sock> --timeout <s>`.

FAIL-CLOSED (spike M0: hook lỗi/timeout = fail-OPEN nếu để mặc định):
- mọi lỗi (JSON vào hỏng, không nối được daemon, daemon trả lỗi, hết giờ) → in quyết định `deny`, exit 0;
- tự đặt `signal.alarm` trước timeout của CLI; nếu vẫn vỡ thì lệnh trong settings có `|| exit 2` (exit 2 = chặn).
Chỉ dùng stdlib, không import gói nặng — chạy ở MỖI tool call.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time

_done = False


def _emit(decision: str, reason: str) -> None:
    global _done
    if _done:
        return
    _done = True
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}}
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _deny_and_exit(reason: str) -> None:
    try:
        _emit("deny", reason)
    except BaseException:
        os._exit(2)
    os._exit(0)


def rpc_call(sock_path: str, method: str, params: dict, timeout: float) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf:
        raise RuntimeError("daemon đóng kết nối không trả lời")
    msg = json.loads(buf)
    if "error" in msg:
        raise RuntimeError(f"daemon lỗi: {msg['error'].get('message')}")
    return msg["result"]


def _args(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--") and i + 1 < len(argv):
            out[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


def main(argv: list[str] | None = None) -> int:
    t0 = time.monotonic()
    try:
        a = _args(sys.argv[1:] if argv is None else argv)
        run_id = a.get("run") or os.environ.get("CFHARNESS_RUN_ID") or ""
        sock = a.get("socket") or os.environ.get("CFHARNESS_SOCKET") or ""
        timeout = float(a.get("timeout") or 300)
        # tự kết thúc TRƯỚC timeout của CLI (CLI hết giờ = fail-open)
        signal.signal(signal.SIGALRM, lambda *_: _deny_and_exit("hub guard: hết thời gian chờ quyết định (fail-closed)"))
        signal.alarm(max(1, int(timeout) + 5))
        raw = sys.stdin.read()
        data = json.loads(raw)
        if not isinstance(data, dict) or not data.get("tool_name"):
            raise ValueError("input hook không có tool_name")
        if not run_id or not sock:
            raise ValueError("thiếu --run/--socket")
        res = rpc_call(
            sock,
            "guard.check",
            {
                "run_id": run_id,
                "channel": "hook",
                "tool_name": data.get("tool_name"),
                "tool_input": data.get("tool_input") or {},
                "tool_use_id": data.get("tool_use_id"),
                "session_id": data.get("session_id"),
                "cwd": data.get("cwd"),
                "max_wait_s": max(1.0, timeout - (time.monotonic() - t0) - 10),
            },
            timeout,
        )
        decision = res.get("decision")
        if decision not in ("allow", "deny"):
            raise ValueError(f"quyết định lạ: {decision!r}")
        _emit(decision, str(res.get("reason") or f"hub guard: {decision}"))
        return 0
    except SystemExit:
        raise
    except BaseException as e:  # fail-closed
        _deny_and_exit(f"hub guard unavailable — blocked (fail-closed): {type(e).__name__}: {str(e)[:200]}")
        return 0


if __name__ == "__main__":
    try:
        rc = main()
    except SystemExit as e:
        rc = int(e.code or 0)
    except BaseException:
        _deny_and_exit("hub guard crashed — blocked (fail-closed)")
        rc = 0
    sys.exit(rc)
