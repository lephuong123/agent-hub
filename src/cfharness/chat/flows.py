"""Lệnh Telegram tương tác (UX v2 — T-C) + tiến độ run sửa tại chỗ (T-D). Gắn vào `TelegramBot` (`bot.flows`).

- `/giaoviec` (`/run`): `<agent> <yêu cầu>` → giao ngay như cũ; chỉ `<agent>` → ô nhập (`bot.ask_input`: Telegram `force_reply`, Slack modal); không tham số → danh sách agent
  đánh số + nút số `[➕ 1]…` → ô nhập. Reply vào ô nhập = yêu cầu → `run.start` (cổng người, audit `telegram:<chat>`).
- `/agent`: danh sách đánh số (chữ hub: id, trạng thái, chi phí) + nút `[➕ n]` giao việc, `[⏹ n]` dừng run đang chạy của agent đó.
- `/luotchay`: 10 lượt gần nhất (chữ hub) + nút số → chi tiết (khung kết quả agent WT-8, không nút). `/stop` không tham số → chọn run đang chạy.
- Nút chỉ mang số/biểu tượng + chữ hub; mọi nút qua `CallbackStore` (nonce một lần, tiêu cả nhóm); hành động `f*`.
- Tiến độ: run do chat giao chạy quá `notify.telegram.progress_after_s` (30 s) → MỘT tin tiến độ, sửa tại chỗ ≤ 1 lần/`progress_edit_s`
  (10 s); run kết thúc → sửa thành "đã xong — kết quả bên dưới".
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ..events import HubEvent
from ..notify.base import clip
from ..notify.telegram import esc

if TYPE_CHECKING:
    from .base import ChatBot

log = logging.getLogger("cfharness.chat.telegram.flows")

MAX_PICK = 10
PER_ROW = 5
LIVE = ("queued", "running", "waiting_human")


def _rows(btns: list[tuple[str, str, str]], per: int = PER_ROW) -> list[list[tuple[str, str, str]]]:
    return [btns[i : i + per] for i in range(0, len(btns), per)]


class ChatFlows:
    def __init__(self, bot: "ChatBot") -> None:
        self.bot = bot
        self.MAP = f"{bot.NAME}.flow_map"  # "chat:message_id" (ô nhập force_reply) → {"kind": "run", "agent": id}
        self.hub = bot.hub
        self.progress: dict[str, dict[str, Any]] = {}

    def _put(self, k: str, v: Any, cap: int = 300) -> None:
        m = self.bot._state(self.MAP)
        if v is None:
            m.pop(k, None)
        else:
            m[k] = v
        if len(m) > cap:
            for old in list(m)[: len(m) - cap]:
                m.pop(old, None)
        self.hub.store.state_set(self.MAP, m)

    def _ttl(self) -> float:
        return min(self.bot._ttl(), 86400)

    # ------------------------------------------------------------------ /agent
    async def c_agents(self, cid: str, rest: str, api: Any) -> str | None:
        from .base import AGENT_STATUS_VI

        rows = await self.bot.call(cid, "agent.list")
        if not rows:
            return "Chưa có agent nào (tạo bằng cfharness agent create hoặc web)."
        rows = rows[:30]
        lines = ["🤖 <b>[cfharness] Agent</b>"]
        btns: list[tuple[str, str, str]] = []
        stops: list[tuple[str, str, str]] = []
        for i, r in enumerate(rows, 1):
            st = "⏸ tạm dừng" if r.get("paused") else AGENT_STATUS_VI.get(str(r.get("status")), str(r.get("status")))
            lines.append(f"{i}. {esc(r.get('id'))} — {esc(st)} · hôm nay ${float(r.get('cost_today_usd') or 0):.4f}")
            if i <= MAX_PICK:
                btns.append(("fg", str(r["id"]), f"➕ {i}"))
                if r.get("current_run_id") and r.get("status") == "working":
                    stops.append(("fs", str(r["current_run_id"]), f"⏹ {i}"))
        lines.append("➕ n = giao việc cho agent số n" + (" · ⏹ n = dừng lượt đang chạy" if stops else "") + ".")
        markup, _ = self.bot.callbacks.issue_rows(_rows(btns) + _rows(stops), cid, ttl=self._ttl())
        await self.bot._send(api, cid, "\n".join(lines), markup, html=True)
        return None

    # ------------------------------------------------------------------ /luotchay
    async def c_runs(self, cid: str, rest: str, api: Any) -> str | None:
        from .base import STATUS_VI

        rows = await self.bot.call(cid, "run.list", {"agent": rest.split()[0] if rest else None, "limit": 10})
        if not rows:
            return "Chưa có run nào."
        lines = [f"🗂 <b>[cfharness] {len(rows)} lượt chạy gần nhất</b>"]
        btns = []
        for i, r in enumerate(rows, 1):
            st = r.get("outcome") or r.get("status")
            lines.append(f"{i}. {esc(r.get('id'))} · {esc(r.get('agent_id'))} — {esc(STATUS_VI.get(str(st), str(st)))} · ${float(r.get('cost_usd') or 0):.4f}"
                         f" · {esc(str(r.get('queued_at') or '')[:16].replace('T', ' '))}")
            btns.append(("fr", str(r["id"]), str(i)))
        lines.append("Bấm số để xem kết quả lượt đó.")
        markup, _ = self.bot.callbacks.issue_rows(_rows(btns), cid, ttl=self._ttl())
        await self.bot._send(api, cid, "\n".join(lines), markup, html=True)
        return None

    # ------------------------------------------------------------------ /giaoviec, /run
    async def c_run(self, cid: str, rest: str, api: Any) -> str | None:
        agent, _, prompt = rest.partition(" ")
        if agent and prompt.strip():
            return await self.start_run(cid, agent, prompt.strip())
        if agent:
            await self.ask_prompt(cid, agent, api)
            return None
        rows = await self.bot.call(cid, "agent.list")
        rows = [r for r in rows or [] if not r.get("paused")][:MAX_PICK]
        if not rows:
            return "Chưa có agent nào để giao việc."
        lines = ["➕ <b>[cfharness] Giao việc cho agent nào?</b>"] + [f"{i}. {esc(r['id'])}" for i, r in enumerate(rows, 1)]
        lines.append(f"Hoặc gõ: {esc(self.bot.cmd('giaoviec'))} &lt;agent&gt; &lt;yêu cầu&gt;")
        markup, _ = self.bot.callbacks.issue_rows(_rows([("fg", str(r["id"]), str(i)) for i, r in enumerate(rows, 1)]), cid, ttl=self._ttl())
        await self.bot._send(api, cid, "\n".join(lines), markup, html=True)
        return None

    async def start_run(self, cid: str, agent: str, prompt: str) -> str:
        r = await self.bot.call(cid, "run.start", {"agent": agent, "prompt": prompt})
        self.bot._watch(r["id"], cid)
        return f"Đã giao run {r['id']} cho agent {agent} ({r.get('status')}). Bot sẽ gửi kết quả khi xong."

    async def ask_prompt(self, cid: str, agent: str, api: Any, reply_to: Any = None) -> None:
        res = await self.bot.ask_input(api, cid, "run", agent, f"✍️ Mô tả việc cần làm cho agent {agent} — {self.bot.reply_here()}.",
                                       f"Việc cho {agent}…", reply_to=reply_to)  # Slack: modal (chat/slack/modals.py)
        if res.get("message_id") is not None:
            self._put(f"{cid}:{res['message_id']}", {"kind": "run", "agent": agent})

    async def on_reply(self, cid: str, mid: Any, text: str, api: Any) -> bool:
        from .base import ChatError

        f = self.bot._state(self.MAP).get(f"{cid}:{mid}")
        if not f:
            return False
        self._put(f"{cid}:{mid}", None)
        if f.get("kind") == "run":
            try:
                out = await self.start_run(cid, str(f["agent"]), text)
            except ChatError as e:
                out = f"Lỗi: {clip(e, 400)}"
            await self.bot._send(api, cid, out)
        return True

    # ------------------------------------------------------------------ /stop
    async def c_stop(self, cid: str, rest: str, api: Any) -> str | None:
        if rest:
            r = await self.bot.call(cid, "run.stop", {"id": rest.split()[0]})
            return f"Đã gửi yêu cầu dừng run {(r or {}).get('id') or rest.split()[0]}."
        rows = [r for r in (await self.bot.call(cid, "run.list", {"status": "running", "limit": MAX_PICK}) or []) if r.get("status") in LIVE]
        if not rows:
            return "Không có lượt nào đang chạy."
        lines = ["⏹ <b>[cfharness] Dừng lượt nào?</b>"] + [f"{i}. {esc(r['id'])} · {esc(r.get('agent_id'))}" for i, r in enumerate(rows, 1)]
        markup, _ = self.bot.callbacks.issue_rows(_rows([("fs", str(r["id"]), f"⏹ {i}") for i, r in enumerate(rows, 1)]), cid, ttl=min(self._ttl(), 3600))
        await self.bot._send(api, cid, "\n".join(lines), markup, html=True)
        return None

    # ------------------------------------------------------------------ nút f*
    async def on_callback(self, cid: str, action: str, ref: str, mid: Any, api: Any) -> str:
        from .base import ChatError

        try:
            if action == "fg":
                await self.bot._edit(api, cid, mid, f"➕ <b>[cfharness] Giao việc cho {esc(ref)}</b> — nhập yêu cầu {esc(self.bot.input_where())}.", None)
                await self.ask_prompt(cid, ref, api, reply_to=mid)
                return f"Nhập yêu cầu cho {ref}."
            if action == "fs":
                r = await self.bot.call(cid, "run.stop", {"id": ref})
                await self.bot._edit(api, cid, mid, f"⏹ <b>[cfharness] Đã gửi yêu cầu dừng run {esc((r or {}).get('id') or ref)}</b>.", None)
                return "Đã gửi yêu cầu dừng."
            if action == "fr":
                run = await self.bot.call(cid, "run.get", {"id": ref})
                run = (run or {}).get("run") or run
                if (run or {}).get("status") in LIVE:
                    await self.bot._send(api, cid, f"Run {ref} đang {run.get('status')} — bot gửi kết quả khi xong nếu bạn giao qua {self.bot.LABEL}.")
                else:
                    res = await self.bot._send(api, cid, self.bot.format_result(run), html=True)
                    if res.get("message_id") is not None:  # reply vào chi tiết = tiếp tục hội thoại run đó
                        self.bot._map_message(cid, res["message_id"], ref)
                return f"Run {ref}"
        except ChatError as e:
            return f"Lỗi: {clip(e, 150)}"
        return "Nút không hợp lệ."

    # ------------------------------------------------------------------ T-D: tiến độ run sửa tại chỗ
    def on_event(self, ev: HubEvent) -> None:
        rid = ev.run_id
        if not rid:
            return
        if ev.type == "tool.start":
            w = self.bot._state(self.bot.WATCH)
            chat = w.get(rid)
            if chat is None:
                return
            pg = self.progress.setdefault(rid, {"chat": str(chat), "mid": None, "tools": 0, "last": 0.0, "t0": time.monotonic(), "sending": False})
            pg["tools"] += 1
            c = self.bot.cfg()
            if time.monotonic() - pg["t0"] < float(c.get("progress_after_s") or 30) or pg["sending"]:
                return
            if time.monotonic() - pg["last"] < float(c.get("progress_edit_s") or 10):
                return
            pg["sending"] = True
            self.bot._spawn(self._progress(rid, ev.agent_id))

    def _progress_text(self, rid: str, agent: Any, pg: dict[str, Any], done: bool = False) -> str:
        secs = int(time.monotonic() - pg["t0"])
        if done:
            return f"☑️ <b>[cfharness] Run {esc(rid)} (agent {esc(agent)}) đã kết thúc</b> · {pg['tools']} công cụ · {secs} s — kết quả ở tin bên dưới."
        return f"▶️ <b>[cfharness] Run {esc(rid)} (agent {esc(agent)}) đang chạy</b> · {pg['tools']} công cụ · {secs} s"

    async def _progress(self, rid: str, agent: Any) -> None:
        pg = self.progress.get(rid)
        api = self.bot.api()
        try:
            if pg is None or api is None:
                return
            if pg["mid"] is None:
                res = await self.bot._send(api, pg["chat"], self._progress_text(rid, agent, pg), html=True)
                pg["mid"] = res.get("message_id")
            else:
                await self.bot._edit(api, pg["chat"], pg["mid"], self._progress_text(rid, agent, pg), None)
            pg["last"] = time.monotonic()
        except Exception as e:
            log.debug("không cập nhật được tiến độ run %s: %s", rid, clip(e))
        finally:
            if pg is not None:
                pg["sending"] = False

    async def finish(self, rid: str, agent: Any) -> None:
        pg = self.progress.pop(rid, None)
        api = self.bot.api()
        if pg is None or pg.get("mid") is None or api is None:
            return
        await self.bot._edit(api, pg["chat"], pg["mid"], self._progress_text(rid, agent, pg, done=True), None)
