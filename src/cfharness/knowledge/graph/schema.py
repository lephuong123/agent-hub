"""Migration v19 — graph memory (docs/dev-notes/plan-graph-memory.md).

Bảng (mọi hàng mang `scope` agent|team|global + `owner_id` — agent id, team id, hoặc `global`):
- `mem_episodes`: văn bản nguồn (đã che secret) mà một lần trích xuất đọc — KHÔNG BAO GIỜ xoá (trừ purge agent), là provenance.
- `mem_entities`: thực thể (tên NFC, `canon` = khoá gấp dấu, alias, loại). `invalidated_at` = gộp/không còn dùng (không xoá).
- `mem_facts`: cạnh (src, relation, dst) + câu fact; thời gian KÉP như Graphiti: `valid_from/valid_to` (thời gian thế giới) và
  `ingested_at/invalidated_at` (thời gian hệ thống). Mâu thuẫn đóng hiệu lực fact cũ, không xoá.
- `retracted_by` (mọi bảng): ledger entry rollback đã GỠ hàng này (đọc luôn bỏ qua). Rollback không xoá cứng — lịch sử còn nguyên.
- `ledger_entry_id` / `invalidated_entry_id`: entry tạo / entry đóng hiệu lực — rollback theo hàng + ghim canary dựa vào đây.
- `mem_vectors`: embedding tuỳ chọn (extra `cfharness[vector]`, BAAI/bge-m3 1024-d), ghi `model` + `model_version` mỗi vector —
  đổi model → chỉ mục lại. Lưu BLOB float32; sqlite-vec (nếu cài) dựng bảng ảo từ đây ở PR retrieval. Không có → FTS5 thuần.
FTS5 `mem_facts_fts`/`mem_entities_fts`/`mem_episodes_fts` external-content trên cột `folded`, tokenizer chọn LÚC CHẠY như v17.
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS mem_episodes (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    agent_id TEXT,
    run_id TEXT,
    session_id TEXT,
    source_kind TEXT NOT NULL,
    source_ref TEXT,
    text TEXT NOT NULL,
    folded TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL,
    ledger_entry_id TEXT,
    retracted_by TEXT
);
CREATE INDEX IF NOT EXISTS mem_episodes_owner ON mem_episodes(scope, owner_id, created_at);
CREATE TABLE IF NOT EXISTS mem_entities (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    canon TEXT NOT NULL,
    type TEXT,
    aliases TEXT,
    alias_keys TEXT,
    summary TEXT,
    folded TEXT NOT NULL,
    created_by TEXT,
    agent_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidated_entry_id TEXT,
    ledger_entry_id TEXT,
    retracted_by TEXT
);
CREATE INDEX IF NOT EXISTS mem_entities_canon ON mem_entities(scope, owner_id, canon);
CREATE INDEX IF NOT EXISTS mem_entities_entry ON mem_entities(ledger_entry_id);
CREATE TABLE IF NOT EXISTS mem_facts (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    src_entity_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    dst_entity_id TEXT,
    fact TEXT NOT NULL,
    folded TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    ingested_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidated_by TEXT,
    invalidated_entry_id TEXT,
    source_episode_id TEXT,
    created_by TEXT,
    agent_id TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    hits INTEGER NOT NULL DEFAULT 0,
    last_used TEXT,
    ledger_entry_id TEXT,
    retracted_by TEXT
);
CREATE INDEX IF NOT EXISTS mem_facts_edge ON mem_facts(scope, owner_id, src_entity_id, relation);
CREATE INDEX IF NOT EXISTS mem_facts_dst ON mem_facts(dst_entity_id);
CREATE INDEX IF NOT EXISTS mem_facts_entry ON mem_facts(ledger_entry_id);
CREATE INDEX IF NOT EXISTS mem_facts_agent ON mem_facts(agent_id, ingested_at);
CREATE TABLE IF NOT EXISTS mem_vectors (
    item_kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_kind, item_id, model)
);
"""

FTS_TABLES = (("mem_facts_fts", "mem_facts"), ("mem_entities_fts", "mem_entities"), ("mem_episodes_fts", "mem_episodes"))


def migrate_v19(conn: sqlite3.Connection) -> None:
    from ...session_search import pick_tokenizer
    from ...store import _split_sql

    for stmt in _split_sql(SCHEMA):
        conn.execute(stmt)
    tok = pick_tokenizer(conn)
    if tok is None:  # không có FTS5 → đọc bằng LIKE trên `folded`
        return
    for fts, base in FTS_TABLES:
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5(folded, content='{base}', content_rowid='rowid', tokenize='{tok}')")
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {base}_ai AFTER INSERT ON {base} BEGIN INSERT INTO {fts}(rowid, folded) VALUES (new.rowid, new.folded); END")
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {base}_ad AFTER DELETE ON {base} BEGIN "
                     f"INSERT INTO {fts}({fts}, rowid, folded) VALUES ('delete', old.rowid, old.folded); END")
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {base}_au AFTER UPDATE OF folded ON {base} BEGIN "
                     f"INSERT INTO {fts}({fts}, rowid, folded) VALUES ('delete', old.rowid, old.folded); "
                     f"INSERT INTO {fts}(rowid, folded) VALUES (new.rowid, new.folded); END")


def has_fts(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mem_facts_fts'").fetchone() is not None
