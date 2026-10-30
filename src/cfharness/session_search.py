"""Tìm kiếm hội thoại (session search) — SQLite FTS5 trên TOÀN BỘ nội dung hội thoại của run + hội thoại điều phối viên.

Giống `session_search` của Hermes Agent: thuần SQLite, tất định, KHÔNG gọi LLM. `group` gộp hit theo luồng (top session). Ghi chú thiết kế: `docs/dev-notes/session-search.md`.

Dữ liệu (migration v17, `store.py`):
- `session_messages`: mỗi hàng = một "tin" đã chuẩn hoá: prompt của run (`runs.prompt`, role `user`), tin steer (`user.message`,
  role `steer`), câu trả lời hoàn chỉnh của assistant (`message.complete` — CLI đã gộp các `message.delta` thành tin hoàn chỉnh,
  role `assistant`), lời gọi tool (`tool.start`: tên + tóm tắt input, role `tool_call`), kết quả tool (`tool.complete`: tóm tắt
  đã cắt, role `tool_result`), tin hội thoại điều phối viên (`manager_messages`, source `manager`, role giữ nguyên user/manager/…).
  Cột `text` = bản ĐÃ CHE secret (redact.redact_str: mẫu khoá + giá trị secret đã đăng ký) — secret không bao giờ vào FTS.
  Cột `folded` = `text` gấp dấu (bỏ dấu thanh/mũ, `đ`→`d`, chữ thường) theo TỪNG KÝ TỰ (cùng độ dài với `text`) → câu hỏi
  có dấu hay không dấu đều khớp, và vị trí khớp trên `folded` dùng thẳng để cắt đoạn trích từ `text`.
- `session_fts`: FTS5 external-content trên `session_messages.folded`, đồng bộ bằng trigger INSERT/DELETE. Tokenizer chọn lúc
  migration: `trigram` (khớp chuỗi con, mọi ngôn ngữ) nếu bản SQLite hỗ trợ, nếu không `unicode61 remove_diacritics 2`.
  Không có FTS5 → vẫn tạo `session_messages`, tìm bằng LIKE trên `folded`.

Retention: `session_messages` KHÔNG phụ thuộc bảng `events` — job retention (recovery.purge_retention) xoá event > 30 ngày nhưng
văn bản hội thoại đã chỉ mục được giữ CÙNG VÒNG ĐỜI với `runs` (giữ vĩnh viễn). Purge cứng agent (`recovery.purge_agent`) xoá
cả tin của agent đó.

Bốn chế độ (`search`): `search` (truy vấn → hit xếp hạng bm25 + đoạn trích), `around` (id tin → N tin trước/sau cùng luồng),
`read` (run/session/hội thoại → transcript phân trang), `browse` (danh sách luồng gần đây). "Luồng" (thread) = hội thoại điều
phối viên (`conversation_id`) hoặc session `claude` (gồm mọi run resume cùng session) hoặc run.

Phạm vi nhìn (`Visibility`): người/nội bộ thấy tất cả; điều phối viên thấy tất cả; agent khác chỉ thấy tin của CHÍNH nó và
của các team nó là thành viên (không thấy hội thoại điều phối viên). Daemon tính phạm vi từ người gọi (auth.Caller) — xem
`mcp_server/surface_rpc.py`.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .errors import HubError, NotFound
from .redact import redact_str

TECHNICAL_KINDS = ("review", "eval", "eval_judge", "curator", "learning")
MODES = ("search", "around", "read", "browse")
MAX_TEXT = 8000  # = claude_adapter.MESSAGE_COMPLETE_MAX
TOOL_INPUT_MAX = 200
TOOL_RESULT_MAX = 2000  # như Hermes (2.000); = claude_adapter.RESULT_PREVIEW_MAX — hàng chỉ mục trước đó giữ 300
GROUP_SCAN_MAX = 500  # group: số tin xếp hạng tối đa quét để gộp luồng
GROUP_HITS_PER_THREAD = 3
INDEXED_EVENTS = ("user.message", "message.complete", "tool.start", "tool.complete")
_WORD = re.compile(r"\w+", re.U)

COLS = ("id", "source", "run_id", "agent_id", "team_id", "task_id", "session_id", "conversation_id", "subagent_id", "kind", "role", "tool", "ts")


# ============================================================================ chuẩn hoá
def _fold_char(ch: str) -> str:
    if ch in "đĐ":
        return "d"
    base = "".join(c for c in unicodedata.normalize("NFD", ch) if not unicodedata.combining(c))
    low = base.lower() if base else ch.lower()
    return low if len(low) == 1 else (base[:1] or ch)


class _FoldTable(dict):
    """Bảng `str.translate` tự điền (cache theo code point) — nhanh hơn vòng lặp Python từng ký tự ~10 lần."""

    def __missing__(self, cp: int) -> str:
        v = _fold_char(chr(cp))
        self[cp] = v
        return v


_FOLD = _FoldTable()


def fold(s: str) -> str:
    """Gấp dấu theo từng ký tự, GIỮ độ dài (vị trí trên bản gấp = vị trí trên bản gốc)."""
    return s.translate(_FOLD)


def clean(text: Any, limit: int = MAX_TEXT) -> str:
    s = redact_str(str(text or "")).strip()
    return s[:limit]


# ============================================================================ schema (migration v17)
def pick_tokenizer(conn: sqlite3.Connection) -> str | None:
    """Tokenizer FTS5 tốt nhất bản SQLite hiện tại hỗ trợ; None = không có FTS5."""
    for tok in ("trigram remove_diacritics 1", "trigram", "unicode61 remove_diacritics 2"):
        try:
            conn.execute(f"CREATE VIRTUAL TABLE temp._fts_probe USING fts5(x, tokenize='{tok}')")
            conn.execute("DROP TABLE temp._fts_probe")
            return tok
        except sqlite3.OperationalError:
            continue
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    run_id TEXT,
    agent_id TEXT,
    team_id TEXT,
    task_id TEXT,
    session_id TEXT,
    conversation_id TEXT,
    subagent_id TEXT,
    kind TEXT,
    role TEXT NOT NULL,
    tool TEXT,
    text TEXT NOT NULL,
    folded TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_messages_run ON session_messages(run_id, ts, id);
CREATE INDEX IF NOT EXISTS session_messages_session ON session_messages(session_id, ts, id);
CREATE INDEX IF NOT EXISTS session_messages_conv ON session_messages(conversation_id, ts, id);
CREATE INDEX IF NOT EXISTS session_messages_agent ON session_messages(agent_id, ts);
CREATE INDEX IF NOT EXISTS session_messages_ts ON session_messages(ts);
"""


BACKFILL_KEY = "session_index_backfill"
PHASES = ("runs", "events", "manager", "done")


def migrate_v17(conn: sqlite3.Connection) -> None:
    """Tạo bảng + FTS5 + trigger và ĐẶT LỊCH backfill (không backfill trong migration — xem `backfill_step`).

    Vì sao lười: đo trên Windows, 100k event (≈74k tin cần chỉ mục) mất ~20–30 s ghi FTS — chạy trong migration sẽ chặn daemon
    khởi động quá thời gian `spawn_background` chờ socket. Backfill chạy nền theo lô nhỏ trong daemon (`run_backfill`), tiếp
    tục được sau restart (con trỏ ở `hub_state`); chỉ mục lúc ghi chạy ngay từ đầu nên dữ liệu MỚI luôn tìm được."""
    from .store import _split_sql

    for stmt in _split_sql(SCHEMA):
        conn.execute(stmt)
    tok = pick_tokenizer(conn)
    if tok is not None:
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5(folded, content='session_messages', content_rowid='id', tokenize='{tok}')")
        conn.execute("CREATE TRIGGER IF NOT EXISTS session_messages_ai AFTER INSERT ON session_messages BEGIN "
                     "INSERT INTO session_fts(rowid, folded) VALUES (new.id, new.folded); END")
        conn.execute("CREATE TRIGGER IF NOT EXISTS session_messages_ad AFTER DELETE ON session_messages BEGIN "
                     "INSERT INTO session_fts(session_fts, rowid, folded) VALUES ('delete', old.id, old.folded); END")
    # mốc trên: hàng ghi SAU migration đã được chỉ mục lúc ghi → backfill chỉ cần tới đây
    hi = {"runs": conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM runs").fetchone()[0],
          "events": conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0],
          "manager": conn.execute("SELECT COALESCE(MAX(id), 0) FROM manager_messages").fetchone()[0]}
    _set_state(conn, {"phase": "runs", "cursor": 0, "hi": hi, "done": 0})


def _get_state(conn: sqlite3.Connection) -> dict[str, Any] | None:
    import json

    try:
        r = conn.execute("SELECT value FROM hub_state WHERE key=?", (BACKFILL_KEY,)).fetchone()
    except sqlite3.OperationalError:  # CSDL tối giản (không có hub_state)
        return None
    return json.loads(r[0]) if r else None


def _set_state(conn: sqlite3.Connection, st: dict[str, Any]) -> None:
    import json

    from .events import iso

    conn.execute("INSERT INTO hub_state(key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                 "updated_at=excluded.updated_at", (BACKFILL_KEY, json.dumps(st), iso()))


def backfill_pending(conn: sqlite3.Connection) -> bool:
    st = _get_state(conn)
    return bool(st) and st.get("phase") != "done"


def backfill_step(conn: sqlite3.Connection, batch: int = 400, idx: "Indexer | None" = None) -> bool:
    """Chỉ mục một lô (idempotent nhờ `ref` UNIQUE). Trả True khi còn việc. Gọi trong giao dịch của người gọi hoặc tự commit
    (autocommit). Thứ tự: runs (prompt; summary nếu run không còn `message.complete` — event đã bị retention xoá) → events → manager."""
    import json

    st = _get_state(conn)
    if not st or st.get("phase") == "done":
        return False
    idx = idx or Indexer(conn)
    phase, cur, hi = st["phase"], int(st.get("cursor") or 0), st.get("hi") or {}
    n = 0
    if phase == "runs":
        rows = conn.execute("SELECT rowid AS _rid, * FROM runs WHERE rowid > ? AND rowid <= ? ORDER BY rowid LIMIT ?",
                            (cur, int(hi.get("runs") or 0), batch)).fetchall()
        for r in rows:
            d = dict(r)
            idx.index_run_prompt(d)
            if d.get("summary") and conn.execute("SELECT 1 FROM events WHERE run_id=? AND +type='message.complete' LIMIT 1", (d["id"],)).fetchone() is None:
                idx.add(f"run:{d['id']}:summary", "run", idx.run_meta(d["id"]), role="assistant", text=d["summary"], ts=d.get("ended_at") or d["queued_at"])
            cur = d["_rid"]
        n = len(rows)
    elif phase == "events":
        # `+type`: buộc quét theo rowid (không dùng index events(type, ts) — sẽ sắp xếp lại toàn bảng mỗi lô)
        rows = conn.execute(f"SELECT rowid AS _rid, * FROM events WHERE rowid > ? AND rowid <= ? AND +type IN ({','.join('?' * len(INDEXED_EVENTS))}) "
                            "ORDER BY rowid LIMIT ?", (cur, int(hi.get("events") or 0), *INDEXED_EVENTS, batch)).fetchall()
        for e in rows:
            d = dict(e)
            cur = d["_rid"]
            try:
                d["payload"] = json.loads(d["payload"])
            except (TypeError, ValueError):
                continue
            idx.index_event_row(d)
        n = len(rows)
    elif phase == "manager":
        rows = conn.execute("SELECT m.*, c.agent_id FROM manager_messages m LEFT JOIN manager_conversations c ON c.id = m.conversation_id "
                            "WHERE m.id > ? AND m.id <= ? ORDER BY m.id LIMIT ?", (cur, int(hi.get("manager") or 0), batch)).fetchall()
        for m in rows:
            idx.index_manager_message(dict(m))
            cur = m["id"]
        n = len(rows)
    st["done"] = int(st.get("done") or 0) + n
    if n < batch:  # hết pha này
        st["phase"], st["cursor"] = PHASES[PHASES.index(phase) + 1], 0
    else:
        st["cursor"] = cur
    _set_state(conn, st)
    return st["phase"] != "done"


def backfill(conn: sqlite3.Connection) -> int:
    """Chạy hết backfill đồng bộ (test, công cụ). Trả số tin mới được chỉ mục."""
    before = conn.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0]
    idx = Indexer(conn)
    while backfill_step(conn, batch=2000, idx=idx):
        pass
    return conn.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0] - before


async def run_backfill(hub: Any, *, batch: int = 150, pause_s: float = 0.02) -> None:
    """Vòng nền trong daemon (`Hub.start`): mỗi lô một giao dịch ngắn, nhả event loop giữa các lô."""
    import asyncio
    import logging

    log = logging.getLogger("cfharness.session_search")
    try:
        if not backfill_pending(hub.store.conn):
            return
        log.info("bắt đầu backfill chỉ mục hội thoại (v17)")
        idx = indexer_for(hub.store)
        while True:
            with hub.store.tx():
                more = backfill_step(hub.store.conn, batch=batch, idx=idx)
            if not more:
                break
            await asyncio.sleep(pause_s)
        log.info("xong backfill chỉ mục hội thoại: %s tin", (_get_state(hub.store.conn) or {}).get("done"))
    except asyncio.CancelledError:  # daemon dừng — con trỏ đã lưu, lần sau chạy tiếp
        raise
    except Exception:  # pragma: no cover
        log.exception("backfill chỉ mục hội thoại lỗi (sẽ thử lại lần khởi động sau)")


def start_backfill(hub: Any) -> None:
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if backfill_pending(hub.store.conn):
        hub._bg.append(loop.create_task(run_backfill(hub)))


# ============================================================================ chỉ mục lúc ghi
def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


class Indexer:
    """Ghi `session_messages` (trigger đẩy sang FTS). Daemon là writer duy nhất; gọi trên CÙNG kết nối của Store."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._runs: dict[str, dict[str, Any]] = {}
        self._ready: bool | None = None

    @property
    def ready(self) -> bool:
        if self._ready is None:
            self._ready = _has_table(self.conn, "session_messages")
        return self._ready

    def run_meta(self, run_id: str | None) -> dict[str, Any]:
        if not run_id:
            return {}
        m = self._runs.get(run_id)
        if m is None:
            r = self.conn.execute("SELECT id, agent_id, team_id, task_id, session_id, kind, run_opts FROM runs WHERE id=?", (run_id,)).fetchone()
            if r is None:
                return {"run_id": run_id}
            import json

            try:
                opts = json.loads(r["run_opts"]) if r["run_opts"] else {}
            except (TypeError, ValueError):
                opts = {}
            m = {"run_id": r["id"], "agent_id": r["agent_id"], "team_id": r["team_id"], "task_id": r["task_id"], "session_id": r["session_id"],
                 "kind": r["kind"], "manager_conv": (opts or {}).get("manager_conversation")}
            if len(self._runs) > 2000:
                self._runs.clear()
            self._runs[run_id] = m
        return m

    def add(self, ref: str, source: str, meta: dict[str, Any], *, role: str, text: Any, ts: str, tool: str | None = None,
            subagent_id: str | None = None, conversation_id: str | None = None, limit: int = MAX_TEXT) -> bool:
        t = clean(text, limit)
        if not t:
            return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO session_messages (ref, source, run_id, agent_id, team_id, task_id, session_id, conversation_id, subagent_id, "
            "kind, role, tool, text, folded, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ref, source, meta.get("run_id"), meta.get("agent_id"), meta.get("team_id"), meta.get("task_id"), meta.get("session_id"),
             conversation_id, subagent_id, meta.get("kind"), role, tool, t, fold(t), ts),
        )
        return cur.rowcount > 0

    def index_run_prompt(self, run: dict[str, Any]) -> None:
        meta = self.run_meta(run["id"])
        if meta.get("manager_conv"):
            return  # prompt run điều phối viên = khung prompt; tin của người đã nằm trong manager_messages
        self.add(f"run:{run['id']}:prompt", "run", meta, role="user", text=run.get("prompt"), ts=run["queued_at"])

    def index_event_row(self, e: dict[str, Any]) -> None:
        """`e`: dict có type, ts, epoch, seq, run_id, agent_id, subagent_id, payload (dict)."""
        t, p = e["type"], e.get("payload") or {}
        meta = dict(self.run_meta(e.get("run_id"))) or {}
        if not meta.get("agent_id"):
            meta["agent_id"] = e.get("agent_id")
        for k in ("team_id", "task_id", "session_id"):
            meta[k] = meta.get(k) or e.get(k)
        ref = f"ev:{e['epoch']}:{e['seq']}"
        sub = e.get("subagent_id")
        if t == "message.complete":
            self.add(ref, "run", meta, role="assistant", text=p.get("text"), ts=e["ts"], subagent_id=sub)
        elif t == "user.message":
            if meta.get("manager_conv"):
                return  # steer của điều phối viên = khung prompt; tin người nằm trong manager_messages
            self.add(ref, "run", meta, role="steer", text=p.get("text"), ts=e["ts"], subagent_id=sub)
        elif t == "tool.start":
            name = str(p.get("name") or "?")
            self.add(ref, "run", meta, role="tool_call", text=f"{name} {str(p.get('args_preview') or '')[:TOOL_INPUT_MAX]}", ts=e["ts"], tool=name,
                     subagent_id=sub)
        elif t == "tool.complete":
            prev = str(p.get("result_preview") or "")[:TOOL_RESULT_MAX]
            if prev.strip():
                name = str(p.get("name") or "?")
                self.add(ref, "run", meta, role="tool_result", text=prev, ts=e["ts"], tool=name, subagent_id=sub,
                         limit=TOOL_RESULT_MAX)

    def index_event(self, ev: Any) -> None:
        """Gọi từ `Store.insert_event` (event đã persisted). Prompt của run chỉ mục ở `Store.insert_run` (prompt đầy đủ, không phải preview)."""
        if not self.ready or ev.type not in INDEXED_EVENTS:
            return
        from .events import iso

        self.index_event_row({"type": ev.type, "ts": iso(ev.ts), "epoch": ev.epoch, "seq": ev.seq, "run_id": ev.run_id, "agent_id": ev.agent_id,
                              "team_id": ev.team_id, "task_id": ev.task_id, "session_id": ev.session_id, "subagent_id": ev.subagent_id,
                              "payload": ev.payload})

    def index_manager_message(self, m: dict[str, Any]) -> None:
        if not self.ready:
            return
        meta = {"run_id": m.get("run_id"), "agent_id": m.get("agent_id"), "kind": "manager_chat"}
        self.add(f"mm:{m['id']}", "manager", meta, role=str(m.get("role") or "user"), text=m.get("text"), ts=m["ts"],
                 conversation_id=m.get("conversation_id"))

    def index_manager_row(self, msg_id: int) -> None:
        r = self.conn.execute("SELECT m.*, c.agent_id FROM manager_messages m LEFT JOIN manager_conversations c ON c.id = m.conversation_id "
                              "WHERE m.id=?", (msg_id,)).fetchone()
        if r is not None:
            self.index_manager_message(dict(r))

    def purge_agent(self, agent_id: str) -> int:
        if not self.ready:
            return 0
        return self.conn.execute("DELETE FROM session_messages WHERE agent_id=?", (agent_id,)).rowcount


def indexer_for(store: Any) -> Indexer:
    idx = getattr(store, "_session_indexer", None)
    if idx is None:
        idx = Indexer(store.conn)
        store._session_indexer = idx
    return idx


# ============================================================================ phạm vi nhìn
@dataclass
class Visibility:
    """`all=True` → không lọc. Ngược lại chỉ tin `source='run'` của `agents` hoặc của run thuộc `teams`."""

    all: bool = True
    agents: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    who: str = "human"

    def clause(self, alias: str = "m") -> tuple[str, list[Any]]:
        if self.all:
            return "", []
        parts, args = [], []
        if self.agents:
            parts.append(f"{alias}.agent_id IN ({','.join('?' * len(self.agents))})")
            args += self.agents
        if self.teams:
            parts.append(f"{alias}.team_id IN ({','.join('?' * len(self.teams))})")
            args += self.teams
        if not parts:
            return " AND 0", []
        return f" AND {alias}.source='run' AND ({' OR '.join(parts)})", args

    def to_dict(self) -> dict[str, Any]:
        return {"all": self.all, "agents": self.agents, "teams": self.teams, "who": self.who}


def agent_visibility(conn: sqlite3.Connection, agent_id: str) -> Visibility:
    teams = [r[0] for r in conn.execute("SELECT team_id FROM team_members WHERE agent_id=? ORDER BY team_id", (agent_id,))]
    return Visibility(all=False, agents=[agent_id], teams=teams, who=f"agent:{agent_id}")


# ============================================================================ truy vấn
def _since(v: Any) -> str | None:
    """ISO / 'YYYY-MM-DD' / tương đối '7d' '12h' '30m'."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    m = re.fullmatch(r"(\d+)\s*([dhm])", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[u]
        from .events import iso

        return iso(datetime.now(timezone.utc) - delta)
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HubError(f"mốc thời gian không hợp lệ: '{s}' (ISO, YYYY-MM-DD hoặc 7d/12h/30m)") from None
    return s


def _tokenizer(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("SELECT sql FROM sqlite_master WHERE name='session_fts'").fetchone()
    if r is None:
        return None
    return "trigram" if "trigram" in (r[0] or "") else "unicode61"


def _terms(query: str) -> tuple[list[str], list[str]]:
    """(cụm có ngoặc kép, từ đơn) — đã gấp dấu."""
    q = fold(query or "")
    phrases = [p.strip() for p in re.findall(r'"([^"]+)"', q) if p.strip()]
    rest = re.sub(r'"[^"]*"', " ", q)
    words = [w for w in _WORD.findall(rest) if w]
    return phrases, words


def _fts_query(tok: str, phrases: list[str], words: list[str]) -> tuple[str, list[str]]:
    """Biểu thức MATCH + danh sách chuỗi phải lọc thêm bằng LIKE (trigram không khớp được chuỗi < 3 ký tự)."""
    parts, like = [], []
    for p in phrases + words:
        esc = p.replace('"', '""')
        if tok == "trigram":
            if len(p) >= 3:
                parts.append(f'"{esc}"')
            else:
                like.append(p)
        else:
            parts.append(f'"{esc}"' + ("*" if p in words else ""))
    return " AND ".join(parts), like


def _filters(p: dict[str, Any], alias: str = "m") -> tuple[str, list[Any]]:
    sql, args = "", []
    for key, col in (("agent_id", "agent_id"), ("team_id", "team_id"), ("task_id", "task_id"), ("run_id", "run_id"), ("session_id", "session_id"),
                     ("conversation_id", "conversation_id"), ("role", "role"), ("source", "source")):
        v = p.get(key)
        if v:
            sql += f" AND {alias}.{col}=?"
            args.append(v)
    kind = p.get("kind")
    if kind:
        kinds = [kind] if isinstance(kind, str) else list(kind)
        sql += f" AND {alias}.kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    elif not p.get("include_technical"):
        sql += f" AND ({alias}.kind IS NULL OR {alias}.kind NOT IN ({','.join('?' * len(TECHNICAL_KINDS))}))"
        args += list(TECHNICAL_KINDS)
    since, until = _since(p.get("since")), _since(p.get("until"))
    if since:
        sql += f" AND {alias}.ts >= ?"
        args.append(since)
    if until:
        sql += f" AND {alias}.ts <= ?"
        args.append(until)
    return sql, args


def _snippet(text: str, needles: Iterable[str], width: int = 220) -> str:
    f = fold(text)
    pos = [(f.find(n), len(n)) for n in needles if n and f.find(n) >= 0]
    if not pos:
        s = text[:width].replace("\n", " ")
        return s + ("…" if len(text) > width else "")
    first = min(p for p, _ in pos)
    start = max(0, first - width // 3)
    end = min(len(text), start + width)
    seg = text[start:end]
    fseg = f[start:end]
    marks: list[tuple[int, int]] = []
    for n in sorted({n for n in needles if n}, key=len, reverse=True):
        i = fseg.find(n)
        while i >= 0:
            if not any(a < i + len(n) and i < b for a, b in marks):
                marks.append((i, i + len(n)))
            i = fseg.find(n, i + len(n))
    out, last = [], 0
    for a, b in sorted(marks):
        out += [seg[last:a], "«", seg[a:b], "»"]
        last = b
    out.append(seg[last:])
    s = "".join(out).replace("\n", " ").strip()
    return ("…" if start else "") + s + ("…" if end < len(text) else "")


def _thread_of(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("conversation_id"):
        return {"type": "conversation", "id": row["conversation_id"]}
    if row.get("session_id"):
        return {"type": "session", "id": row["session_id"]}
    return {"type": "run", "id": row.get("run_id")}


def _msg(row: sqlite3.Row | dict[str, Any], *, full: bool = True) -> dict[str, Any]:
    d = dict(row)
    out = {k: d.get(k) for k in COLS}
    out["thread"] = _thread_of(d)
    if full:
        out["text"] = d.get("text")
    return {k: v for k, v in out.items() if v is not None}


def _limit(v: Any, default: int, hi: int) -> int:
    try:
        n = int(v if v is not None else default)
    except (TypeError, ValueError):
        raise HubError("limit phải là số") from None
    return max(1, min(n, hi))


def search(conn: sqlite3.Connection, p: dict[str, Any], vis: Visibility | None = None) -> dict[str, Any]:
    """Một trong 4 chế độ (p['mode'], mặc định 'search'). Thuần đọc."""
    if not _has_table(conn, "session_messages"):
        raise HubError("CSDL chưa có chỉ mục hội thoại (cần migration v17) — khởi động lại daemon")
    vis = vis or Visibility()
    mode = str(p.get("mode") or "search")
    if mode not in MODES:
        raise HubError(f"mode phải là {'|'.join(MODES)}")
    fn = {"search": _mode_search, "around": _mode_around, "read": _mode_read, "browse": _mode_browse}[mode]
    out = fn(conn, p, vis)
    out["mode"] = mode
    if backfill_pending(conn):
        out["backfill_pending"] = True  # dữ liệu cũ (trước v17) đang được chỉ mục nền — kết quả có thể thiếu
    if not vis.all:
        out["visibility"] = vis.to_dict()
    return out


def _mode_search(conn: sqlite3.Connection, p: dict[str, Any], vis: Visibility) -> dict[str, Any]:
    query = str(p.get("query") or "").strip()
    phrases, words = _terms(query)
    if not phrases and not words:
        raise HubError("query rỗng")
    limit = _limit(p.get("limit"), 10, 100)
    offset = max(0, int(p.get("offset") or 0))
    group = bool(p.get("group"))
    # group: xếp hạng tin rồi gộp theo luồng (như Hermes trả top session) — lấy cửa sổ tin đủ rộng, offset/limit tính theo LUỒNG
    take, skip = (min(GROUP_SCAN_MAX, (offset + limit) * 10 + 1), 0) if group else (limit + 1, offset)
    fsql, fargs = _filters(p)
    vsql, vargs = vis.clause()
    tok = _tokenizer(conn)
    match, like = _fts_query(tok, phrases, words) if tok else ("", phrases + words)
    like_sql = "".join(" AND m.folded LIKE ? ESCAPE '\\'" for _ in like)
    like_args = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in like]
    if match:
        sql = ("SELECT m.*, bm25(session_fts) AS rank FROM session_fts JOIN session_messages m ON m.id = session_fts.rowid "
               f"WHERE session_fts MATCH ?{fsql}{vsql}{like_sql} ORDER BY rank, m.ts DESC LIMIT ? OFFSET ?")
        args = [match, *fargs, *vargs, *like_args, take, skip]
    else:
        sql = f"SELECT m.*, 0.0 AS rank FROM session_messages m WHERE 1{fsql}{vsql}{like_sql} ORDER BY m.ts DESC LIMIT ? OFFSET ?"
        args = [*fargs, *vargs, *like_args, take, skip]
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        raise HubError(f"truy vấn tìm kiếm không hợp lệ: {e}") from None
    needles = phrases + words

    def hit(r: sqlite3.Row) -> dict[str, Any]:
        h = _msg(r, full=False)
        h["score"] = round(-float(r["rank"] or 0.0), 4)
        h["snippet"] = _snippet(r["text"], needles)
        return h

    if group:
        threads = _group_hits(conn, [hit(r) for r in rows], vis)
        page = threads[offset:offset + limit]
        more = len(threads) > offset + limit
        return {"query": query, "threads": page, "count": len(page), "offset": offset, "next_offset": offset + limit if more else None,
                "engine": tok or "like", "grouped": True, "scanned": len(rows), "scan_truncated": len(rows) >= take}
    more = len(rows) > limit
    hits = [hit(r) for r in rows[:limit]]
    return {"query": query, "hits": hits, "count": len(hits), "offset": offset, "next_offset": offset + limit if more else None,
            "engine": tok or "like"}


def _group_hits(conn: sqlite3.Connection, hits: list[dict[str, Any]], vis: Visibility) -> list[dict[str, Any]]:
    """Gộp hit đã xếp hạng theo luồng, GIỮ thứ tự hạng của hit tốt nhất mỗi luồng; mỗi luồng ≤ GROUP_HITS_PER_THREAD đoạn trích."""
    order: list[str] = []
    by: dict[str, dict[str, Any]] = {}
    for h in hits:
        th = h["thread"]
        key = f"{th['type']}:{th['id']}"
        g = by.get(key)
        if g is None:
            g = by[key] = {"thread": th, "agent_id": h.get("agent_id"), "best_score": h["score"], "matches": 0, "first_ts": h.get("ts"),
                           "last_ts": h.get("ts"), "hits": []}
            order.append(key)
        g["matches"] += 1
        ts = h.get("ts")
        if ts:
            g["first_ts"] = min(g["first_ts"] or ts, ts)
            g["last_ts"] = max(g["last_ts"] or ts, ts)
        if len(g["hits"]) < GROUP_HITS_PER_THREAD:
            g["hits"].append({k: h[k] for k in ("id", "run_id", "role", "tool", "ts", "snippet", "score") if h.get(k) is not None})
    vsql, vargs = vis.clause()
    out = []
    for key in order:
        g = by[key]
        tsql, targs = _thread_clause(g["thread"])
        first = conn.execute(f"SELECT text FROM session_messages m WHERE {tsql}{vsql} AND m.role IN ('user','steer') ORDER BY m.ts, m.id LIMIT 1",
                             (*targs, *vargs)).fetchone()
        title = (first[0] if first else "").replace("\n", " ").strip()
        g["title"] = title[:120] + ("…" if len(title) > 120 else "")
        out.append({k: v for k, v in g.items() if v is not None})
    return out


def _get_msg(conn: sqlite3.Connection, mid: Any, vis: Visibility) -> dict[str, Any]:
    vsql, vargs = vis.clause()
    try:
        mid = int(mid)
    except (TypeError, ValueError):
        raise HubError("message_id phải là số") from None
    r = conn.execute(f"SELECT m.* FROM session_messages m WHERE m.id=?{vsql}", (mid, *vargs)).fetchone()
    if r is None:
        raise NotFound(f"không có tin #{mid} (hoặc ngoài phạm vi được xem)")
    return dict(r)


def _thread_clause(t: dict[str, Any], alias: str = "m") -> tuple[str, list[Any]]:
    if t["type"] == "conversation":
        return f"{alias}.conversation_id=?", [t["id"]]
    if t["type"] == "session":
        return f"{alias}.session_id=?", [t["id"]]
    return f"{alias}.run_id=?", [t["id"]]


def _mode_around(conn: sqlite3.Connection, p: dict[str, Any], vis: Visibility) -> dict[str, Any]:
    mid = p.get("message_id") if p.get("message_id") is not None else p.get("id")
    if mid is None:
        raise HubError("thiếu tham số 'message_id'", code=-32602)
    m = _get_msg(conn, mid, vis)
    n = _limit(p.get("window") if p.get("window") is not None else p.get("limit"), 5, 50)
    th = _thread_of(m)
    tsql, targs = _thread_clause(th)
    vsql, vargs = vis.clause()
    before = conn.execute(f"SELECT m.* FROM session_messages m WHERE {tsql}{vsql} AND (m.ts < ? OR (m.ts = ? AND m.id < ?)) "
                          "ORDER BY m.ts DESC, m.id DESC LIMIT ?", (*targs, *vargs, m["ts"], m["ts"], m["id"], n)).fetchall()
    after = conn.execute(f"SELECT m.* FROM session_messages m WHERE {tsql}{vsql} AND (m.ts > ? OR (m.ts = ? AND m.id > ?)) "
                         "ORDER BY m.ts, m.id LIMIT ?", (*targs, *vargs, m["ts"], m["ts"], m["id"], n)).fetchall()
    msgs = [_msg(r) for r in reversed(before)] + [{**_msg(m), "anchor": True}] + [_msg(r) for r in after]
    return {"message_id": m["id"], "thread": th, "messages": msgs}


def _mode_read(conn: sqlite3.Connection, p: dict[str, Any], vis: Visibility) -> dict[str, Any]:
    if p.get("conversation_id"):
        th = {"type": "conversation", "id": str(p["conversation_id"])}
    elif p.get("session_id"):
        th = {"type": "session", "id": str(p["session_id"])}
    elif p.get("run_id"):
        rid = str(p["run_id"])
        r = conn.execute("SELECT id FROM runs WHERE id=?", (rid,)).fetchone() or (
            (lambda rows: rows[0] if len(rows) == 1 else None)(conn.execute("SELECT id FROM runs WHERE id LIKE ? LIMIT 2", (rid + "%",)).fetchall()))
        th = {"type": "run", "id": r[0] if r else rid}
    else:
        raise HubError("read cần run_id, session_id hoặc conversation_id", code=-32602)
    limit = _limit(p.get("limit"), 50, 500)
    offset = max(0, int(p.get("offset") or 0))
    tsql, targs = _thread_clause(th)
    vsql, vargs = vis.clause()
    role = p.get("role")
    rsql, rargs = (" AND m.role=?", [role]) if role else ("", [])
    total = conn.execute(f"SELECT COUNT(*) FROM session_messages m WHERE {tsql}{vsql}{rsql}", (*targs, *vargs, *rargs)).fetchone()[0]
    if total == 0:
        raise NotFound(f"không có transcript cho {th['type']} '{th['id']}' (hoặc ngoài phạm vi được xem)")
    rows = conn.execute(f"SELECT m.* FROM session_messages m WHERE {tsql}{vsql}{rsql} ORDER BY m.ts, m.id LIMIT ? OFFSET ?",
                        (*targs, *vargs, *rargs, limit, offset)).fetchall()
    nxt = offset + len(rows)
    return {"thread": th, "total": total, "offset": offset, "next_offset": nxt if nxt < total else None, "messages": [_msg(r) for r in rows]}


def _mode_browse(conn: sqlite3.Connection, p: dict[str, Any], vis: Visibility) -> dict[str, Any]:
    limit = _limit(p.get("limit"), 20, 200)
    offset = max(0, int(p.get("offset") or 0))
    fsql, fargs = _filters(p)
    vsql, vargs = vis.clause()
    key = "COALESCE(m.conversation_id, m.session_id, m.run_id)"
    rows = conn.execute(
        f"SELECT {key} AS thread_id, CASE WHEN m.conversation_id IS NOT NULL THEN 'conversation' WHEN m.session_id IS NOT NULL THEN 'session' ELSE 'run' END AS thread_type, "
        "MIN(m.ts) AS first_ts, MAX(m.ts) AS last_ts, COUNT(*) AS messages, COUNT(DISTINCT m.run_id) AS runs, "
        "MAX(m.agent_id) AS agent_id, MAX(m.team_id) AS team_id, MAX(m.kind) AS kind, MIN(m.id) AS first_id "
        f"FROM session_messages m WHERE 1{fsql}{vsql} GROUP BY {key} ORDER BY last_ts DESC LIMIT ? OFFSET ?",
        (*fargs, *vargs, limit + 1, offset),
    ).fetchall()
    more = len(rows) > limit
    out = []
    for r in rows[:limit]:
        d = dict(r)
        th = {"type": d.pop("thread_type"), "id": d.pop("thread_id")}
        tsql, targs = _thread_clause(th)
        first = conn.execute(f"SELECT text FROM session_messages m WHERE {tsql}{vsql} AND m.role IN ('user','steer') ORDER BY m.ts, m.id LIMIT 1",
                             (*targs, *vargs)).fetchone()
        title = (first[0] if first else "").replace("\n", " ").strip()
        d.pop("first_id", None)
        out.append({"thread": th, **{k: v for k, v in d.items() if v is not None}, "title": title[:120] + ("…" if len(title) > 120 else "")})
    return {"threads": out, "count": len(out), "offset": offset, "next_offset": offset + limit if more else None}


# ============================================================================ hiển thị CLI
def render_text(res: dict[str, Any]) -> str:
    mode = res.get("mode")
    lines: list[str] = []
    if mode == "search" and res.get("grouped"):
        if not res.get("threads"):
            return "(không có kết quả)"
        for t in res["threads"]:
            th = t["thread"]
            lines.append(f"{(t.get('last_ts') or '')[:19]}  {th['type']}:{str(th['id'])[:14]}  {t.get('agent_id') or '-'}  {t.get('matches')} khớp  "
                         f"điểm {t.get('best_score')}  {t.get('title', '')}")
            for h in t.get("hits", []):
                lines.append(f"    #{h['id']} {h.get('role')}: {h.get('snippet', '')}")
        if res.get("next_offset") is not None:
            lines.append(f"(còn nữa — --offset {res['next_offset']})")
    elif mode == "search":
        if not res.get("hits"):
            return "(không có kết quả)"
        for h in res["hits"]:
            th = h.get("thread") or {}
            lines.append(f"#{h['id']}  {h.get('ts', '')[:19]}  {h.get('agent_id') or '-'}  {h.get('role')}  {th.get('type')}:{str(th.get('id'))[:12]}"
                         + (f"  run {h['run_id'][:12]}" if h.get("run_id") else ""))
            lines.append(f"    {h.get('snippet', '')}")
        if res.get("next_offset") is not None:
            lines.append(f"(còn nữa — --offset {res['next_offset']})")
    elif mode in ("around", "read"):
        th = res.get("thread") or {}
        lines.append(f"{th.get('type')}: {th.get('id')}" + (f"  ({res.get('total')} tin)" if res.get("total") is not None else ""))
        for m in res.get("messages", []):
            mark = "▶ " if m.get("anchor") else "  "
            who = m.get("role") + (f"[{m['tool']}]" if m.get("tool") and m.get("role") == "tool_result" else "")
            text = (m.get("text") or "").replace("\n", "\n      ")
            lines.append(f"{mark}#{m['id']} {m.get('ts', '')[:19]} {who}: {text}")
        if res.get("next_offset") is not None:
            lines.append(f"(còn nữa — --offset {res['next_offset']})")
    elif mode == "browse":
        if not res.get("threads"):
            return "(chưa có hội thoại nào)"
        for t in res["threads"]:
            th = t["thread"]
            lines.append(f"{t.get('last_ts', '')[:19]}  {th['type']}:{str(th['id'])[:14]}  {t.get('agent_id') or '-'}  {t.get('messages')} tin/{t.get('runs')} run  {t.get('title', '')}")
    return "\n".join(lines)


# ============================================================================ CLI: cfharness search
def cmd_search(ctx: Any) -> int:
    from .errors import NotFound as _NF

    a = ctx.args
    p: dict[str, Any] = {k: v for k, v in (("agent_id", a.agent), ("team_id", a.team), ("kind", a.kind), ("role", a.role), ("since", a.since),
                                            ("until", a.until), ("limit", a.limit), ("offset", a.offset or None)) if v}
    if a.technical:
        p["include_technical"] = True
    if a.group:
        p["group"] = True
    if a.around is not None:
        p.update(mode="around", message_id=a.around, window=a.window)
    elif a.read:
        ref = a.read
        p["mode"] = "read"
        if ref.startswith("mc_"):
            p["conversation_id"] = ref
        else:
            try:
                res = ctx.call("session.search", {**p, "run_id": ref})
                ctx.out(res, render_text(res))
                return 0
            except Exception as e:  # RpcError (qua daemon) hoặc NotFound (in-process): không có run → thử session id
                if getattr(e, "code", None) != _NF.code:
                    raise
                p["session_id"] = ref
    elif a.browse:
        p["mode"] = "browse"
    else:
        q = " ".join(a.query or []).strip()
        if not q:
            raise HubError("cần từ khoá — hoặc dùng --around <id> | --read <run|session|mc_…> | --browse")
        p.update(mode="search", query=q)
    res = ctx.call("session.search", p)
    ctx.out(res, render_text(res))
    return 0


def add_parsers(sub: Any, common: Any) -> None:
    s = sub.add_parser("search", help='tìm kiếm hội thoại (FTS5, không gọi LLM): cfharness search "từ khoá" | --around ID | --read RUN | --browse',
                       parents=[common])
    s.add_argument("query", nargs="*", help='từ khoá (có dấu/không dấu đều khớp; "cụm từ" trong ngoặc kép)')
    g = s.add_mutually_exclusive_group()
    g.add_argument("--around", type=int, metavar="ID", help="xem các tin quanh tin #ID")
    g.add_argument("--read", metavar="REF", help="đọc transcript: id run (hoặc tiền tố) | session id | hội thoại mc_…")
    g.add_argument("--browse", action="store_true", help="liệt kê luồng hội thoại gần đây")
    s.add_argument("--window", type=int, default=5, help="số tin trước/sau cho --around")
    s.add_argument("--agent", help="lọc theo agent")
    s.add_argument("--team", help="lọc theo team")
    s.add_argument("--kind", help="lọc theo loại run (adhoc, task, chat, …; manager_chat)")
    s.add_argument("--role", help="user|steer|assistant|tool_call|tool_result|manager|…")
    s.add_argument("--since", help="từ thời điểm (ISO, YYYY-MM-DD, 7d/12h/30m)")
    s.add_argument("--until", help="đến thời điểm")
    s.add_argument("--technical", action="store_true", help="kể cả phiên kỹ thuật review/eval/curator")
    s.add_argument("--group", action="store_true", help="gộp kết quả theo luồng hội thoại (top session, ≤ 3 đoạn trích mỗi luồng)")
    s.add_argument("--limit", type=int)
    s.add_argument("--offset", type=int, default=0)
    s.set_defaults(fn=cmd_search)
