"""P7 — "đang xử lý…" + tin tiến độ có nút Dừng khi điều phối viên làm việc cho một tin Slack (kiểu Claude trong Slack).

- Trạng thái: `assistant.threads.setStatus{channel_id, thread_ts, status}` (scope `chat:write`) ở thread mà trả lời sẽ vào. Slack tự
  xoá khi bot đăng tin trong thread hoặc sau 2 phút → nhắc lại mỗi `SlackBot.TYPING_EVERY_S` (90 s). Lỗi (kênh không hỗ trợ, thiếu
  scope…) → reaction ⏳ (`reactions.add`, cần `reactions:write`) trên tin của người, gỡ khi có trả lời / run kết thúc; reaction cũng lỗi
  → bỏ êm (log debug), vòng nhắc dừng.
- `notify.slack.busy_indicator: reaction` → bỏ setStatus, chỉ reaction ⏳ trên tin của người; có kết quả → ⏳ đổi thành ✅ (xong)
  hoặc ❌ (lỗi/dừng); điều phối viên im lặng → chỉ gỡ ⏳.
- Tin tiến độ: MỘT tin trong thread (reply vào tin của người), sửa tại chỗ (`chat.update`) theo `tool.start`/`tool.complete` của run
  điều phối viên. Mỗi bước = tên tool (lọc ký tự, che secret) + nhãn do HUB đặt theo loại tool — KHÔNG BAO GIỜ input/kết quả tool.
  Sửa ≤ 1 lần / `manager_progress_edit_s` (3 s, gộp event), không có event mới thì vẫn cập nhật thời gian mỗi `TICK_S`; 429 → chờ
  `retry_after`. Xong → thu về một dòng ("✓ Xong · N bước · 42 giây" / "⏹ Đã dừng…"), gỡ nút.
- Nút "Dừng": `CallbackStore` (nonce một lần + HMAC, ràng chat); người bấm phải thuộc allowlist (`SlackBot._on_actions` kiểm TRƯỚC khi
  tiêu nonce) → `run.stop` qua `bot.call` (cổng người, audit `slack:<chat>`).
- Event: listener ĐỒNG BỘ trên bus (`hub.bus.add_listener`) nhận MỌI event, không lọc level → `tool.start|complete` tới đủ. Event tới
  trước khi kịp tạo bản ghi → nạp lại từ store (`query_events`). Run con (resume sau duyệt: `run.queued{parent_run_id}`) nối vào cùng tin.
- Trạng thái chỉ trong RAM (daemon khởi động lại → tin tiến độ cũ đứng yên; nút Dừng vẫn ký hợp lệ và dừng đúng run).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from ...events import HubEvent
from ...notify.base import clip
from ...notify.telegram import esc
from ...redact import redact_str
from .api import SlackError

if TYPE_CHECKING:
    from .api import SlackApi
    from .bot import SlackBot

log = logging.getLogger("cfharness.chat.slack.progress")

EDIT_MIN_S = 3.0  # mặc định `notify.slack.manager_progress_edit_s` — chat.update tier 3 (~50/phút) chia cho nhiều run
TICK_S = 30.0  # không có event mới → vẫn sửa để cập nhật thời gian
MAX_AGE_S = 6 * 3600.0  # quá lâu mà chưa kết thúc → thôi theo dõi (tin đứng yên)
STEPS_SHOWN = 8
STEPS_KEPT = 50
MAX_RECS = 100
STATUS_TEXT = "đang xử lý…"
REACTION = "hourglass_flowing_sand"
DONE_REACTION = {"completed": "white_check_mark"}  # ⏳ → ✅ khi xong; outcome khác (lỗi/dừng) → ❌; "silent" → không icon
FAIL_REACTION = "x"
STOP_ACTION = "ms"  # mã nút Dừng trong CallbackStore (không trùng `q*`/`f*`/`ap`… của lõi)
STOP_LABEL = "⏹ Dừng"

# Nhãn do hub đặt theo LOẠI tool (không đọc input) — tool lạ → "dùng công cụ"
TOOL_VI = {
    "Bash": "chạy lệnh", "BashOutput": "đọc kết quả lệnh", "KillShell": "dừng lệnh", "Read": "đọc tệp", "Write": "ghi tệp",
    "Edit": "sửa tệp", "MultiEdit": "sửa tệp", "NotebookEdit": "sửa notebook", "Grep": "tìm trong tệp", "Glob": "tìm tệp",
    "LS": "xem thư mục", "WebFetch": "mở trang web", "WebSearch": "tìm trên web", "Task": "giao agent phụ", "Agent": "giao agent phụ",
    "TodoWrite": "cập nhật danh sách việc", "Skill": "dùng skill", "ToolSearch": "tìm công cụ",
}
_NAME_JUNK = re.compile(r"[^A-Za-z0-9_.:-]")


def tool_label(name: Any) -> tuple[str, str]:
    """→ (tên hiển thị, nhãn hub). `mcp__<server>__<tool>` → `server.tool` + "công cụ MCP". Tên chỉ giữ `[A-Za-z0-9_.:-]`, ≤ 40, che secret."""
    raw = str(name or "")
    if raw.startswith("mcp__"):
        parts = raw.split("__")
        shown = ".".join(x for x in parts[1:3] if x) or "mcp"
        what = "công cụ MCP"
    else:
        shown, what = raw, TOOL_VI.get(raw, "dùng công cụ")
    return redact_str(_NAME_JUNK.sub("", shown)[:40]) or "?", what


def _dur(secs: float) -> str:
    from ..base import _dur_vi

    return _dur_vi(max(0.0, secs))


class SlackProgress:
    def __init__(self, bot: "SlackBot") -> None:
        self.bot = bot
        self.recs: dict[str, dict[str, Any]] = {}  # run gốc (run_id manager.chat trả) → bản ghi tin tiến độ
        self.alias: dict[str, str] = {}  # run con (resume) → run gốc
        self.marks: dict[tuple[str, str], str] = {}  # (chat, ts tin người) → "status" | "reaction" | "none"
        self._closed: dict[tuple[str, str], str] = {}  # thread đã xong → outcome — vòng nhắc muộn không đặt lại trạng thái
        bot.hub.bus.add_listener(self.on_event)

    # ------------------------------------------------------------------ cấu hình
    def enabled(self) -> bool:
        return bool(self.bot.cfg().get("manager_progress", True))

    def edit_min(self) -> float:
        return max(0.05, float(self.bot.cfg().get("manager_progress_edit_s") or EDIT_MIN_S))

    def key_of(self, run_id: Any) -> str | None:
        r = str(run_id or "")
        if r in self.recs:
            return r
        return self.alias.get(r)

    # ------------------------------------------------------------------ "đang xử lý…" (SlackBot.typing)
    async def status(self, api: "SlackApi", chat: str, reply_to: Any) -> None:
        """Một lần nhắc: setStatus ở thread; lỗi → reaction ⏳ (một lần, không cần nhắc lại); cả hai lỗi → ném để vòng nhắc dừng."""
        k = (str(chat), str(reply_to))
        mode = self.marks.get(k)
        if k in self._closed or mode == "reaction":
            return
        if mode == "none":
            raise SlackError("chat này không báo được 'đang xử lý'")
        if self.bot.cfg().get("busy_indicator") != "reaction":  # "reaction" → chỉ icon trên tin người, không "đang xử lý…"
            try:
                await api.call("assistant.threads.setStatus", {"channel_id": k[0], "thread_ts": self.bot._root_of(reply_to) or k[1],
                                                                "status": STATUS_TEXT})
                self._mark(k, "status")
                return
            except SlackError as e:
                if e.retry_after or mode == "status":
                    return  # giới hạn tốc độ / lỗi thoáng qua sau khi đã chạy được — lần nhắc sau thử lại
                log.debug("assistant.threads.setStatus lỗi (%s) — dùng reaction", clip(e, 200))
        try:
            await api.call("reactions.add", {"channel": k[0], "timestamp": k[1], "name": REACTION})
        except SlackError as e:
            if e.code != "already_reacted":
                log.debug("reactions.add lỗi (%s) — bỏ báo 'đang xử lý' (cần scope reactions:write)", clip(e, 200))
                self._mark(k, "none")
                raise SlackError("không báo được 'đang xử lý'") from None
        self._mark(k, "reaction")
        if k in self._closed:  # run đã xong trong lúc đang gắn → gỡ ngay, không để ⏳ treo
            await self._unmark(api, k, self._closed[k])

    def _mark(self, k: tuple[str, str], mode: str) -> None:
        self.marks[k] = mode
        if len(self.marks) > MAX_RECS * 4:
            for old in list(self.marks)[: len(self.marks) - MAX_RECS * 4]:
                self.marks.pop(old, None)

    async def _unmark(self, api: "SlackApi", k: tuple[str, str], outcome: str | None = None) -> None:
        mode = self.marks.pop(k, None)
        try:
            if mode == "status":
                await api.call("assistant.threads.setStatus", {"channel_id": k[0], "thread_ts": self.bot._root_of(k[1]) or k[1], "status": ""})
            elif mode == "reaction":
                await api.call("reactions.remove", {"channel": k[0], "timestamp": k[1], "name": REACTION})
        except Exception as e:
            log.debug("không xoá được trạng thái 'đang xử lý': %s", clip(e, 200))
        if mode == "reaction" and outcome and outcome != "silent":  # chế độ icon: thay ⏳ bằng kết quả trên tin của người
            try:
                await api.call("reactions.add", {"channel": k[0], "timestamp": k[1], "name": DONE_REACTION.get(outcome, FAIL_REACTION)})
            except SlackError as e:
                if e.code != "already_reacted":
                    log.debug("không gắn được icon kết quả: %s", clip(e, 200))

    # ------------------------------------------------------------------ vòng đời (hook từ ManagerChat)
    async def start(self, api: "SlackApi", chat: str, run_id: str, reply_to: Any) -> None:
        if reply_to is None:
            return
        k = (str(chat), str(reply_to))
        self._closed.pop(k, None)
        if not self.enabled():
            return
        rec: dict[str, Any] = {"key": run_id, "run": run_id, "chat": str(chat), "reply_to": str(reply_to), "mid": None, "steps": [], "n": 0,
                               "t0": time.monotonic(), "t_end": None, "last": 0.0, "dirty": asyncio.Event(), "task": None,
                               "done": None, "stopping": False, "waiting": False, "markup": None}
        self.recs[run_id] = rec
        if len(self.recs) > MAX_RECS:
            for old in list(self.recs)[: len(self.recs) - MAX_RECS]:
                self._drop(old)
        for ev in self._backlog(run_id):  # event tới trước khi có bản ghi (run chạy ngay sau manager.chat)
            self._apply(rec, ev)
        rec["markup"] = self._issue(rec)
        try:
            res = await self.bot._send(api, rec["chat"], self.text(rec), rec["markup"], html=True, reply_to=reply_to)
        except Exception as e:
            log.debug("không gửi được tin tiến độ: %s", clip(e, 200))
            self._drop(run_id)
            return
        rec["mid"] = res.get("message_id")
        rec["last"] = time.monotonic()
        if rec["done"]:  # run kết thúc trong lúc đang gửi
            await self._push(rec, final=True)
            return
        task = asyncio.get_running_loop().create_task(self._pump(rec))
        rec["task"] = task
        self.bot._bg.add(task)  # bot.stop() huỷ cùng tác vụ nền khác
        task.add_done_callback(self.bot._bg.discard)

    async def finish(self, run_id: str, *, chat: Any = None, reply_to: Any = None, outcome: str = "completed") -> None:
        """Có trả lời / run kết thúc không trả lời → tin tiến độ thu về một dòng, gỡ nút, xoá trạng thái/reaction (`silent` → xoá tin)."""
        api = self.bot.api()
        key = self.key_of(run_id)
        rec = self.recs.get(key) if key else None
        if rec is not None:
            self._drop(key)  # type: ignore[arg-type]
            rec["done"] = "stopped" if rec["stopping"] or outcome == "cancelled" else ("ok" if outcome == "completed" else "failed")
            rec["t_end"] = time.monotonic()
            if rec["task"] is not None:
                rec["task"].cancel()
            if api is not None and rec["mid"] is not None:
                if outcome == "silent":  # điều phối viên chọn im lặng → gỡ hẳn tin tiến độ (thread không bị rác)
                    try:
                        await api.call("chat.delete", {"channel": rec["chat"], "ts": rec["mid"]})
                    except Exception as e:
                        log.debug("không xoá được tin tiến độ: %s", clip(e, 200))
                else:
                    await self._push(rec, final=True)
            chat, reply_to = rec["chat"], rec["reply_to"]
        if chat is None or reply_to is None:
            return
        k = (str(chat), str(reply_to))
        self._closed[k] = outcome
        if len(self._closed) > MAX_RECS * 4:
            for old in list(self._closed)[: len(self._closed) - MAX_RECS * 4]:
                self._closed.pop(old, None)
        if api is not None:
            await self._unmark(api, k, outcome)

    def _drop(self, key: str) -> None:
        rec = self.recs.pop(key, None)
        for a in [a for a, k in self.alias.items() if k == key]:
            self.alias.pop(a, None)
        if rec is not None:
            self.bot.callbacks.invalidate_ref(key)  # nút Dừng cũ hết hiệu lực dù tin không sửa được
            if rec["task"] is not None and not rec["done"]:
                rec["task"].cancel()

    # ------------------------------------------------------------------ event (listener đồng bộ của bus)
    def on_event(self, ev: HubEvent) -> None:
        t = ev.type
        if t not in ("tool.start", "tool.complete", "run.queued", "run.started", "run.waiting_human") or not ev.run_id:
            return
        if t == "run.queued":
            parent = self.key_of((ev.payload or {}).get("parent_run_id"))
            if parent and parent in self.recs:  # resume/continue sau duyệt → cùng tin tiến độ, nút Dừng dừng run con
                self.alias[ev.run_id] = parent
                self.recs[parent]["run"] = ev.run_id
            return
        key = self.key_of(ev.run_id)
        rec = self.recs.get(key) if key else None
        if rec is None or rec["done"]:
            return
        if t == "run.waiting_human":
            rec["waiting"] = True
        elif t == "run.started":
            rec["waiting"] = False
        else:
            rec["waiting"] = False
            self._apply(rec, ev)
        rec["dirty"].set()

    def _backlog(self, run_id: str) -> list[HubEvent]:
        try:
            return list(self.bot.hub.store.query_events(run_id=run_id, types=["tool.start", "tool.complete"], limit=STEPS_KEPT * 4))
        except Exception:
            return []

    @staticmethod
    def _apply(rec: dict[str, Any], ev: HubEvent) -> None:
        p = ev.payload or {}
        tid = str(p.get("tool_id") or "")
        steps: list[dict[str, Any]] = rec["steps"]
        if ev.type == "tool.start":
            if tid and any(s["id"] == tid for s in steps):
                return  # đã có (nạp lại từ store + listener)
            shown, what = tool_label(p.get("name"))
            steps.append({"id": tid, "name": shown, "what": what, "state": "run", "sub": bool(ev.subagent_id)})
            rec["n"] += 1
            if len(steps) > STEPS_KEPT:
                del steps[: len(steps) - STEPS_KEPT]
        elif ev.type == "tool.complete":
            for s in reversed(steps):
                if s["id"] == tid:
                    s["state"] = "err" if p.get("is_error") else "ok"
                    break

    # ------------------------------------------------------------------ hiển thị (HTML tối giản của hub, mọi giá trị động qua `esc`)
    def text(self, rec: dict[str, Any]) -> str:
        secs = (rec["t_end"] or time.monotonic()) - rec["t0"]
        meta = f"{rec['n']} bước · {_dur(secs)}"
        if rec["done"]:
            icon, word = {"ok": ("✓", "Xong"), "stopped": ("⏹", "Đã dừng"), "failed": ("⚠️", "Chưa xong")}[rec["done"]]
            return f"{icon} <b>{word}</b> · {esc(meta)}"
        # không viết "Điều phối viên" ở đây: tin tiến độ không được trông như tin trả lời (khung WT-8 bắt đầu bằng chữ đó)
        head = ("⏹ <b>Đang dừng…</b>" if rec["stopping"] else "⏸ <b>Chờ bạn duyệt một thao tác</b>" if rec["waiting"]
                else "⏳ <b>Đang xử lý…</b>")
        lines = [f"{head} · {esc(meta)}"]
        shown = rec["steps"][-STEPS_SHOWN:]
        if rec["n"] > len(shown):
            lines.append(f"<i>… {rec['n'] - len(shown)} bước trước</i>")
        for s in shown:
            mark = {"run": "▸", "ok": "✓", "err": "✗"}.get(s["state"], "•")
            lines.append(f"{mark} {'↳ ' if s['sub'] else ''}<code>{esc(s['name'])}</code> — {esc(s['what'])}")
        return "\n".join(lines)

    def _issue(self, rec: dict[str, Any]) -> dict[str, Any]:
        markup, _ = self.bot.callbacks.issue([(STOP_ACTION, rec["key"], STOP_LABEL)], rec["chat"], ttl=min(self.bot._ttl(), MAX_AGE_S))
        return markup

    def _markup(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        return None if rec["done"] or rec["stopping"] else rec["markup"]

    # ------------------------------------------------------------------ sửa tại chỗ (giới hạn tốc độ)
    async def _pump(self, rec: dict[str, Any]) -> None:
        while not rec["done"]:
            try:
                await asyncio.wait_for(rec["dirty"].wait(), timeout=TICK_S)
            except asyncio.TimeoutError:
                pass
            if rec["done"]:
                return
            if time.monotonic() - rec["t0"] > MAX_AGE_S:
                self._drop(rec["key"])
                return
            wait = rec["last"] + self.edit_min() - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)  # gộp mọi event tới trong khoảng này vào MỘT lần sửa
            if rec["done"]:
                return
            rec["dirty"].clear()
            await self._push(rec)

    async def _push(self, rec: dict[str, Any], *, final: bool = False) -> None:
        api = self.bot.api()
        if api is None or rec["mid"] is None:
            return
        for _ in range(3 if final else 1):
            try:
                await self.bot._edit(api, rec["chat"], rec["mid"], self.text(rec), self._markup(rec), fallback=False)
                rec["last"] = time.monotonic()
                return
            except SlackError as e:
                if not e.retry_after:
                    log.debug("không sửa được tin tiến độ: %s", clip(e, 200))
                    return
                await asyncio.sleep(min(float(e.retry_after), 60.0))  # 429: chờ đúng Retry-After
                rec["last"] = time.monotonic()
                if not final:
                    rec["dirty"].set()  # lần sửa sau mang trạng thái mới nhất
                    return
            except Exception as e:
                log.debug("không sửa được tin tiến độ: %s", clip(e, 200))
                return

    # ------------------------------------------------------------------ nút Dừng (nonce đã tiêu, người đã qua allowlist)
    async def on_stop(self, cid: str, key: str, api: "SlackApi") -> str:
        from ..base import ChatError

        rec = self.recs.get(key)
        target = rec["run"] if rec else key
        try:
            await self.bot.call(cid, "run.stop", {"id": target})
        except ChatError as e:
            if rec is not None and not rec["done"]:
                rec["markup"] = self._issue(rec)  # nút cũ đã tiêu → cấp nút mới để thử lại
                await self._push(rec)
            return f"Lỗi: {clip(e, 150)}"
        if rec is not None and not rec["done"]:
            rec["stopping"] = True
            await self._push(rec)  # gỡ nút ngay, không chờ nhịp sửa
        return "Đã gửi yêu cầu dừng."
