"""Kịch bản "[demo]" của hub demo (`web/demo.py`): câu trả lời CHẠY CHỮ thật của điều phối viên để thử luồng v2 trang Điều phối trên
trình duyệt (`docs/dev-notes/dieu-phoi-island.md`). Không gọi `claude`: sinh dòng stream-json giống CLI thật rồi cho adapter thật đọc.

Kịch bản: lời dẫn → TodoWrite (checklist) → Read → Bash → (thẻ `manager.action` ×2, một thao tác phá huỷ chờ người duyệt, một câu hỏi
trong hội thoại) → câu trả lời markdown dài chạy chữ ~100 token/s (`TOKEN_S`).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

TAG = "[demo]"  # tin gửi cho điều phối viên có chữ này → kịch bản chạy chữ
TOKEN_S = 0.01  # 100 token/s
ANSWER = (
    "Mình đã xem nhanh tình trạng hub. Tóm tắt:\n\n"
    "## Việc đang chạy\n\n"
    "- **lap-trinh** đang sửa test đỏ trong `tests/` — còn 3 test (`test_api.py`).\n"
    "- **van-hanh** muốn dọn dữ liệu tạm cũ, nhưng lệnh `rm -rf /srv/data/tmp-demo` là thao tác **phá huỷ** — cần anh duyệt.\n\n"
    "## Đề xuất\n\n"
    "1. Duyệt (hoặc từ chối) thao tác dọn dữ liệu ở thẻ bên dưới.\n"
    "2. Chọn hướng sửa định dạng ngày cho `test_api.py`.\n"
    "3. Sau khi xong, mình sẽ giao task review cho **review**.\n\n"
    "| Agent | Trạng thái | Chi phí hôm nay |\n|---|---|---|\n| lap-trinh | đang chạy | $0.21 |\n| van-hanh | chờ duyệt | $0.04 |\n\n"
    "```bash\ncfharness approvals\n```\n\n"
    "Tệp liên quan: /srv/agent-hub/docs/install/README.md\n\n"
    "Có gì cần đổi hướng thì nhắn mình nhé."
)


def tokens(text: str) -> list[str]:
    """Cắt chữ thành mẩu ~1 token (≤ 4–6 ký tự, giữ khoảng trắng đầu) như `text_delta` của CLI thật. Ghép lại = đúng chữ gốc."""
    out: list[str] = []
    for w in re.findall(r"\s*\S+|\s+", text):
        while len(w) > 6:
            out.append(w[:4])
            w = w[4:]
        out.append(w)
    return out


def lines(spec: Any, fixture: Path, *, answer: str = ANSWER, token_s: float = TOKEN_S) -> list[tuple[str, float]]:
    """[(dòng stream-json, chờ trước dòng — giây)]. Dòng `system/init` và `result` lấy từ fixture thật (session của run)."""
    raw = [ln for ln in fixture.read_text(encoding="utf-8").splitlines() if ln.strip()]
    sid = spec.session_id
    init = next(json.loads(ln) for ln in raw if '"subtype":"init"' in ln)
    init.update({"session_id": sid, "cwd": str(spec.cwd)})
    result = json.loads(raw[-1])
    out: list[tuple[str, float]] = [(json.dumps(init, ensure_ascii=False), 0.0)]

    def ev(obj: dict[str, Any], mid: str) -> str:
        return json.dumps({"type": "stream_event", "event": obj, "session_id": sid, "parent_tool_use_id": None, "uuid": str(uuid.uuid4()),
                           "api_message_id": mid}, ensure_ascii=False)

    def message(blocks: list[dict[str, Any]], stop: str) -> None:
        mid = "msg_demo_" + uuid.uuid4().hex[:12]
        out.append((ev({"type": "message_start", "message": {"model": "claude-haiku-5-5", "id": mid, "type": "message", "role": "assistant",
                                                             "content": [], "usage": {"input_tokens": 2, "output_tokens": 1}}}, mid), 0.05))
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                out.append((ev({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}, mid), 0.0))
                for tok in tokens(b["text"]):
                    out.append((ev({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": tok}}, mid), token_s))
            else:
                out.append((ev({"type": "content_block_start", "index": i,
                                "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}, mid), 0.0))
                out.append((ev({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}}, mid),
                            0.05))
        out.append((json.dumps({"type": "assistant", "message": {"model": "claude-haiku-5-5", "id": mid, "type": "message", "role": "assistant",
                                                                  "content": blocks, "stop_reason": None, "usage": {"input_tokens": 2, "output_tokens": 50}},
                                "parent_tool_use_id": None, "session_id": sid, "uuid": str(uuid.uuid4())}, ensure_ascii=False), 0.0))
        for i, _ in enumerate(blocks):
            out.append((ev({"type": "content_block_stop", "index": i}, mid), 0.0))
        out.append((ev({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"input_tokens": 2, "output_tokens": 50}}, mid), 0.0))
        out.append((ev({"type": "message_stop"}, mid), 0.0))

    def tool_result(tid: str, content: str, wait: float) -> None:
        out.append((json.dumps({"type": "user", "message": {"role": "user", "content": [{"tool_use_id": tid, "type": "tool_result", "content": content,
                                                                                        "is_error": False}]},
                                "parent_tool_use_id": None, "session_id": sid, "uuid": str(uuid.uuid4())}, ensure_ascii=False), wait))

    todos = [{"content": "Xem run đang chạy", "status": "completed", "activeForm": "Đang xem run"},
             {"content": "Kiểm tra thao tác chờ duyệt", "status": "in_progress", "activeForm": "Đang kiểm tra thao tác chờ duyệt"},
             {"content": "Tóm tắt cho anh", "status": "pending", "activeForm": "Đang tóm tắt"}]
    message([{"type": "text", "text": "Để mình xem các run và thao tác đang chờ trước đã."},
             {"type": "tool_use", "id": "toolu_demo_todo", "name": "TodoWrite", "input": {"todos": todos}}], "tool_use")
    tool_result("toolu_demo_todo", "Todos have been modified successfully.", 0.3)
    message([{"type": "tool_use", "id": "toolu_demo_read", "name": "Read", "input": {"file_path": "/srv/agent-hub/docs/install/README.md"}}], "tool_use")
    tool_result("toolu_demo_read", "# Cài đặt\n…", 0.6)
    message([{"type": "tool_use", "id": "toolu_demo_bash", "name": "Bash",
              "input": {"command": "cfharness approvals --json", "description": "Liệt kê thao tác chờ duyệt"}}], "tool_use")
    tool_result("toolu_demo_bash", '[{"id": "ap_demo", "status": "waiting_human"}]', 0.8)
    message([{"type": "text", "text": answer}], "end_turn")
    result.update({"session_id": sid, "result": answer})
    out.append((json.dumps(result, ensure_ascii=False), 0.05))
    return out


async def events(rt: Any, handle: Any, fixture: Path, *, token_s: float | None = None) -> AsyncIterator[Any]:
    """Phát `lines` theo nhịp qua adapter thật của run; sau kết quả tool Bash → `side_effects` (nếu `rt.hub` đã gắn)."""
    from ..runtime.base import RawLog

    stop: asyncio.Event = handle.internal["stop"]
    raw = RawLog(handle.spec.raw_log_path)
    handle.spec.stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
    handle.spec.stderr_log_path.write_text("", encoding="utf-8")
    fired = False
    try:
        for ln, wait in lines(handle.spec, fixture, token_s=TOKEN_S if token_s is None else token_s):
            if stop.is_set():
                break
            if wait:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=wait)
                    break
                except asyncio.TimeoutError:
                    pass
            raw.write(ln.encode("utf-8"))
            for ev in handle.adapter.feed(ln):
                yield ev
            if not fired and '"toolu_demo_bash"' in ln and '"tool_result"' in ln and getattr(rt, "hub", None) is not None:
                fired = True
                try:
                    side_effects(rt.hub, handle.run_id)
                except Exception as e:  # pragma: no cover — demo: không làm hỏng run
                    print(f"DEMO side effects lỗi: {e}", flush=True)
        handle.exit_code = -2 if stop.is_set() else 0
    finally:
        handle.raw_log_final = raw.close()


def side_effects(hub: Any, run_id: str) -> dict[str, Any]:
    """Hai thẻ "Đã làm N việc", một thao tác phá huỷ CHỜ NGƯỜI duyệt của lượt này và (nếu biết hội thoại) một câu hỏi có lựa chọn."""
    from ..events import make_event

    run = hub.store.get_run(run_id) or {}
    opts = run.get("run_opts") if isinstance(run.get("run_opts"), dict) else {}
    conv = opts.get("manager_conversation")
    aid = run.get("agent_id")
    vh = next(iter(hub.store.list_runs(agent_id="van-hanh", limit=1)), None)  # đích run → thẻ việc sống trong hội thoại (web/flow.py)
    for tool, target, summary in (("task_list", "agent:lap-trinh", "Đã xem 3 task đang mở của lap-trinh"),
                                  ("run_list", "run:" + vh["id"] if vh else "agent:van-hanh", "Đã kiểm tra run của van-hanh (đang chờ duyệt)")):
        hub.publish(make_event("manager.action", aid, {"tool": tool, "target": target, "summary": summary, "audit_id": 0, "conversation_id": conv},
                               run_id=run_id))
    fp = "fp-demo-" + secrets.token_hex(4)
    ap = hub.approvals.open(run, tool_name="Bash", tool_input={"command": "rm -rf /srv/data/tmp-demo"}, tool_use_id="toolu_" + fp, fingerprint=fp,
                            verdict={"klass": "fs_delete", "rule": "rm_recursive_outside_run", "verdict": "destructive",
                                     "reason": "xoá đệ quy ngoài phạm vi run"}, channel="hook")
    hub.approvals.to_waiting_human(ap["id"])
    q = None
    if conv:
        q = hub.questions.open(run, "Sửa test_api.py theo hướng nào?", [{"label": "Sửa test theo ISO 8601", "description": "nhanh, khớp chuẩn"},
                                                                       {"label": "Giữ định dạng cũ, sửa API"}], source="manager", conversation_id=conv)
    return {"approval": ap["id"], "question": (q or {}).get("id")}
