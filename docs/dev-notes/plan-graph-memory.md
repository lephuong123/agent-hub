# Plan: graph memory (2026-11-02)

Chủ repo đã chốt: memory của agent thành **graph** — dung lượng gần như không giới hạn, không mất ngữ cảnh, càng dùng càng đúng.
Embedding **BAAI/bge-m3** (dense 1024-d, tiếng Việt + tiếng Anh), extra tuỳ chọn `cfharness[vector]`; không cài → chỉ FTS5. Có
**tầng agent, team, global** ngay từ đầu (USER/hồ sơ người dùng giữ dạng file). MEMORY.md / USER.md / team `memory.md` giữ làm
"lõi" nhỏ luôn có trong system prompt (hạn mức không đổi). Plan này THAY PR 3 và PR 4 của `plan-memory-hermes-parity.md`.

## 1. Đối chiếu Hermes (`hermes-agent @ 903230b`, `agent/memory_provider.py`, đã mở kiểm 2026-11-02)
Brief đúng về các hàm chính: `prefetch(query, *, session_id)`, `queue_prefetch`, `sync_turn(user, assistant, *, session_id, messages,
turn_author)` (không chặn), `on_session_end(messages)`, `on_pre_compress(messages) -> str`, `on_memory_write(action, target, content,
metadata)`, `get_tool_schemas()`, `handle_tool_call(name, args, **kw) -> str`. Lệch / bổ sung so với brief:
- Còn có `name`, `is_available`, `initialize(session_id, **kw)` (kw: `hermes_home`, `platform`, `agent_context` primary|subagent|cron|flush
  — **không ghi khi không phải primary**), `system_prompt_block()` (TĨNH), `recall_status()`, `on_turn_start`, `on_session_switch`
  (resume/branch/reset/compression), `on_delegation`, `identity_signature`, `backup_paths`, `get_config_schema/save_config`, `shutdown`.
- `on_pre_compress` có phiên bản checkpoint (`PRE_COMPRESS_CHECKPOINT_API_VERSION = 2`: fail-closed, bằng chứng chuẩn hoá).
- `on_memory_write` mang `metadata` (provenance, `previous_content` cho replace/remove); patch đến dưới dạng replace cả mục.
- Hermes chỉ cho MỘT provider ngoài, mỗi profile một instance; job nền phải chạy dưới contextvars của profile.
- `TRIVIAL_PROMPT_RE`: bỏ recall cho câu chào/"ok"/lệnh `/` — ta dùng lại ý này ở PR retrieval.

## 2. Thiết kế đích (kiểu Graphiti, tự làm trên SQLite — không Neo4j/Kuzu/FalkorDB)
- **Lưu** (hub.db, migration v19 `knowledge.graph.schema.migrate_v19`): `mem_episodes` (văn bản nguồn đã che secret, không xoá — provenance),
  `mem_entities` (tên NFC, `canon` = gấp dấu + chữ thường, alias, loại), `mem_facts` (src, relation, dst, câu fact, `valid_from/valid_to`
  thời gian thế giới, `ingested_at/invalidated_at` thời gian hệ thống, `source_episode_id`, `created_by`, `confidence`, `hits`,
  `last_used`, `ledger_entry_id`), mọi hàng có `scope` + `owner_id`. FTS5 trên cột `folded` (tokenizer chọn lúc chạy như v17:
  trigram → unicode61). `mem_vectors` (BLOB float32 + `model` + `model_version` mỗi vector; sqlite-vec khi có).
- **Ghi**: một lần trích xuất LLM theo lô mỗi lần kết thúc session / PreCompact / điều phối viên xoay session (không theo lượt), bằng
  `claude -p --json-schema --no-session-persistence` như `evals/judge.py` (qua `AgentRuntime.oneshot`); trần chi phí + suất ghi
  agent/ngày. Mâu thuẫn trên cùng (thực thể, quan hệ) → đóng hiệu lực fact cũ (không xoá). Gộp thực thể: alias/embedding + LLM xác
  nhận trong consolidation. Output tool/web KHÔNG BAO GIỜ ghi thẳng vào graph (chống đầu độc MINJA/AgentPoison) — chỉ qua trích xuất
  có guard.
- **Đọc**: không gọi LLM. FTS5 + vector → RRF → mở rộng 1–2 bước → xếp hạng liên quan × độ tin cậy × độ mới (giảm hạng, không xoá).
  Chèn top-k fact (kèm ngày hiệu lực, dạng DỮ LIỆU) vào TIN USER ĐẦU (`runs.py` ~556), không vào system prompt (giữ prompt cache);
  tool hub-self để kéo thêm. Mọi thứ qua `threats.scan_load` + redact. Tôn trọng phạm vi nhìn (`session_search._caller_vis`) và ghim
  canary.
- **Đúng dần**: provenance mọi fact; vô hiệu theo thời gian; độ tin cậy cập nhật theo KẾT QUẢ (fact xuất hiện trong run thành công ↑,
  run hồi quy/bị rollback ↓ — không theo độ phổ biến); chỉ lưu fact qua guard/eval; golden set câu hỏi memory (EN/VI/trộn, cập nhật,
  thời gian, từ chối trả lời, thăm dò đầu độc) nối vào eval gate; chỉ số recall@k, độ đúng, tỉ lệ fact cũ, token chèn, tỉ lệ cache-read.

## 3. Chia PR: store → (retrieval ∥ write) → quality
1. **store (PR này)** — nền móng, không đổi hành vi agent: migration v19 + FTS5 + bảng vector; `knowledge/graph/` (`GraphStore`: thêm/
   đóng hiệu lực/lấy/láng giềng/tìm từ khoá; NFC + gấp dấu; scope + owner mọi hàng); `Ledger` `Change.kind="graph"` rollback THEO HÀNG
   (như `invert_memory`), đích tổng hợp `graph://<scope>/<owner>` để canary busy/ghim hoạt động; pipeline target `graph` (kế hoạch, guard:
   threat scan văn bản mới, trần từng fact, KHÔNG trần tổng, trùng lặp; suất riêng `rate.graph_writes_per_agent_per_day` mặc định 20);
   quyền tầng giống tool `memory` (`LearningService.graph_write`); eval overlay chụp graph vào bản sao home + băm vào fingerprint;
   purge/clone/export/import gồm hàng graph. Chi tiết đã làm: `graph-memory.md`.
2. **retrieval** (song song với 3) — `MemoryProvider` kiểu Hermes (prefetch/queue_prefetch/recall_status), kênh vector (bge-m3 qua
   `cfharness[vector]`, backfill nền theo lô như `session_search.run_backfill`, đổi model → chỉ mục lại theo `needs_embedding`), RRF,
   1–2 hop, xếp hạng; chèn vào tin user đầu; tool hub-self `graph_search`/`graph_neighbors`; sửa `memory_search` (`surface_rpc.py`) đang đọc
   file SỐNG (lách ghim canary); đọc bản chụp `.graph_snapshot.json` khi run eval. **Đã làm** (`graph-memory.md` § PR 2) — hai tool dự kiến
   gộp thành MỘT `memory_recall(query | entity, hops, include_history)` + `memory_search` scope `graph`; runtime vector = fastembed (ONNX).
3. **write** (song song với 2, ĐÃ LÀM — `graph-memory.md` mục PR write) — hook `PreCompact` + `on_session_end` + xoay session điều phối viên → MỘT lần trích xuất JSON-schema/session
   (debounce, trần chi phí, `agent_context` không primary thì không ghi) → `graph_write(created_by="review")`; mirror tool `memory`
   (`on_memory_write`) thành episode; tool hub-self `graph` cho agent (dùng `graph_write`, quyền tầng đã có ở PR 1).
4. **quality** — consolidation (gộp thực thể alias/embedding + LLM xác nhận), độ tin cậy theo kết quả run, decay hạng, golden set memory +
   chỉ số vào eval gate/`report`, trang web xem graph.

## 4. Khác biệt CỐ Ý so với Hermes / Graphiti
- Không provider ngoài mặc định (Mem0/Honcho để sau, tắt mặc định). Graph là KHO của hub, không phải plugin: ghi chỉ qua
  `pipeline.propose` → `Ledger.apply` (rollback, canary, eval gate, regression monitor dùng được ngay); Hermes/Graphiti ghi thẳng.
- Không Neo4j: SQLite + FTS5 (+ sqlite-vec) trong hub.db — một file, một writer (daemon), sao lưu/purge như mọi bảng khác.
- Không LLM lúc đọc (Graphiti có thể rerank bằng LLM/cross-encoder) — chi phí + độ trễ + prompt cache.
- Recall vào tin user đầu, không vào system prompt (Hermes `prefetch` cũng vậy; `system_prompt_block` tĩnh — ta giữ MEMORY.md làm khối tĩnh).
- Không có hàng chờ duyệt (hub "báo chứ không hỏi"); tầng team/global do review → canary + ghim theo agent; do agent/người → watch.
- Rollback theo hàng, gỡ bằng `retracted_by` — không xoá cứng (Graphiti xoá cạnh/episode được).
- Trần: Hermes giới hạn ký tự kho; graph KHÔNG trần tổng → hãm bằng trần từng fact (`learning.limits.graph_fact_chars`) + suất ghi/ngày.
- Mâu thuẫn: Graphiti hỏi LLM cạnh nào mâu thuẫn; PR 1 dùng luật tất định (cùng src + relation, trừ `multi`) — PR 4 thêm LLM xác nhận.

## 5. Thay PR 3 / PR 4 của `plan-memory-hermes-parity.md`
- PR 3 (PreCompact) → nằm trong **write**: thay vì enqueue review `focus=memory` ghi MEMORY.md, hook kích MỘT lần trích xuất graph.
  MEMORY.md vẫn được review thường cập nhật như cũ.
- PR 4 (MemoryProvider + local vector) → nằm trong **retrieval**: model đổi từ multilingual-e5-small (fastembed) sang bge-m3; provider
  đọc graph thay vì chỉ mục vector riêng trên `session_messages`. PR 4c (Mem0/Honcho) hoãn.
- PR 1, PR 2 của plan cũ giữ nguyên (đã xong).
