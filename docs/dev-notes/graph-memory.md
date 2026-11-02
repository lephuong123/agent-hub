# Graph memory — đã làm (PR 1: store, PR 3: write)

Plan + đối chiếu Hermes + chia PR: `plan-graph-memory.md`. PR store là nền móng; PR write (mục cuối) tự GHI graph (trích xuất theo lô +
tool `memory` target=graph). CHƯA có gì đọc graph vào prompt (recall = PR retrieval) — graph được ghi nhưng agent chưa thấy nó.

## Đã làm
| Phần | Ở đâu |
|---|---|
| Migration **v19** (Python, `knowledge.graph.schema.migrate_v19`): `mem_episodes`, `mem_entities`, `mem_facts`, `mem_vectors`, FTS5 `mem_*_fts` (cột `folded`, trigger ai/ad/au) | `knowledge/graph/schema.py`, `store.py` (`PY_MIGRATION_PREFIXES`, `is_py_migration` — tổng quát hoá luật "chỉ `session_search.`") |
| NFC + gấp dấu: `nfc` (dạng lưu), `canon` (khoá thực thể/alias), `folded` (FTS), `norm_relation` (snake_case) | `knowledge/graph/text.py` |
| `GraphStore`: `find_entity`, `get_*`, `facts`, `neighbors` (1–2 hop), `search` (FTS5 bm25, LIKE dự phòng), `plan_ops` (THUẦN), `rows_state`/`write_rows`/`invert_rows` (cho Ledger), `snapshot`/`apply_to_snapshot`/`revert_on_snapshot` (eval), `purge_owner`/`export_owner`/`import_rows`, `put_vector`/`get_vector`/`needs_embedding` | `knowledge/graph/store.py` |
| Ledger `Change.kind="graph"` (`graph_rows`, `graph_key`), `change_key`, `state_bytes`, `is_entry_graph`; rollback theo hàng; `materialize_before` trên bản chụp | `learning/ledger.py` |
| Pipeline target `graph`: `_plan_graph`, L1 ở tầng agent / L3 tầng team-global (canary/watch có sẵn), suất riêng, `_replaces` chỉ lô giống hệt, `materialize` lên bản chụp | `learning/pipeline.py` |
| Guard: threat scan văn bản mới (fact, tên, alias, summary), `fact_too_long` (`learning.limits.graph_fact_chars`=500), `duplicate_fact`, pin cả graph (`graph`, ref `graph://…`) / từng fact (`graph_fact`, ref fact id). Không trần tổng, không luật văn phong memory (not_english/imperative…) | `learning/guard.py` |
| Suất `learning.rate.graph_writes_per_agent_per_day` = 20 (mọi tầng, mọi bên ghi trừ người); graph KHÔNG tính vào `max_changes_per_agent_per_day`, `max_team_global_changes_per_day`, `agent_tool_max_per_day` | `learning/policy.py`, `pipeline.rate_check` |
| `LearningService.graph_write(...)`: quyền tầng = tool `memory` (`memory_scopes`, `_shared_team`); từ chối run eval/curator | `learning/service.py` |
| Canary/ghim: đích `graph://<scope>/<owner>` (`tier_canaries.path`); `TierVersions.graph_hidden(agent)` = entry canary agent bị ghim; `GraphStore.visible` ẩn hàng entry đó tạo + coi phép đóng hiệu lực của nó như chưa có; `graph_tiers`, `graph_snapshot` | `learning/canary.py`, `knowledge/tiers.py` |
| Eval: `make_home(..., hub=, agent_id=)` ghi `<copy>/.graph_snapshot.json` (graph agent thấy, theo ghim; bỏ `hits/last_used`, không văn bản episode); `fingerprint` băm file này | `evals/overlay.py`, `evals/gate.py` |
| Purge agent xoá cứng graph tầng agent (+ vector); clone `--with-memory` chép hàng (id mới); export `with_memory` thêm `graph.json` (qua `scrub`), import nạp lại | `recovery.py`, `agents.py` |

## Quyết định / lệch
- **Không xoá cứng khi rollback**: hàng entry tạo được đánh `retracted_by=<entry rollback>`; đọc luôn bỏ qua. Hàng entry đóng hiệu lực được
  khôi phục về trạng thái trước. Hàng đã bị đổi SAU entry → bỏ qua (cờ `partial` trên entry rollback). Thực thể còn fact sống ngoài lô tham
  chiếu → không gỡ.
- **Cột VOLATILE** (`hits`, `last_used`, `confidence`) không thuộc trạng thái ledger: cập nhật theo sử dụng/kết quả (PR retrieval/quality)
  không làm rollback "thấy bị sửa sau", rollback không khôi phục chúng. Bản chụp eval vẫn chứa `confidence` (ảnh hưởng xếp hạng).
- **Mâu thuẫn tất định**: cùng (src, relation) còn hiệu lực → đóng (trừ `multi: true`). `valid_to` = `valid_from` fact mới hoặc bây giờ.
  LLM xác nhận mâu thuẫn để PR quality.
- **Trùng lặp**: fact trùng (src, relation, dst, `canon(fact)`) bị bỏ khỏi lô (`dropped`, hiện trong diff); cả lô không còn gì mới →
  `duplicate_fact`.
- **Episode không threat-scan khi ghi**: là văn bản nguồn thô (có thể trích lời người dùng), chỉ che secret; KHÔNG BAO GIỜ được chèn
  nguyên văn vào ngữ cảnh — fact mới được chèn, và fact bị scan. PR retrieval phải giữ bất biến này (+ `scan_load` lúc đọc).
- Global: `owner_id = "global"` trong bảng graph (giống `skills`), `owner_id = NULL` trong `Change`/ledger (giống memory).
- Alias tìm bằng `alias_keys LIKE '%|canon|%'` (không index) — đủ cho quy mô hiện tại; PR quality có thể tách bảng alias.
- Clone/import không qua ledger (như chép MEMORY.md): agent đích mới tạo. Purge không xoá fact tầng team/global agent đã ghi (tri thức chung).

## Việc dở (cho PR sau)
- Chưa có recall/chèn tin đầu (PR retrieval). (Ghi: xong ở PR write — xem dưới.)
- `mem_vectors` mới chỉ là bảng + helper; chưa có embedder (bge-m3), chưa có sqlite-vec, chưa có backfill.
- Bản chụp eval chứa TOÀN BỘ graph agent thấy — graph rất lớn thì cần cắt theo golden task (PR retrieval).
- `version_map` (`knowledge versions`) chưa liệt kê graph; web/TUI chưa hiển thị graph.

## Test
`tests/test_graph_memory.py` (20 ca, không gọi claude, vector số giả): migration + luật migration Python, NFC/gấp dấu, kế hoạch thuần,
ghi qua pipeline/ledger + tìm không dấu + láng giềng, che secret, mâu thuẫn/`multi`, đóng hiệu lực theo id/cạnh, guard (threat scan, trần
fact, trùng, không trần tổng, tiếng Việt), pin, suất riêng, quyền tầng, chặn run eval, rollback theo hàng (độc lập, mở lại fact cũ, bỏ qua hàng
đổi sau, rollback của rollback), canary `graph://` + ghim (ẩn hàng và ẩn phép đóng hiệu lực), eval overlay/fingerprint/materialize(_before),
purge/clone/export/import, vector theo model/phiên bản.

# PR write (nhánh `feat/graph-memory-write`)

| Phần | Ở đâu |
|---|---|
| `GraphExtractor` (`hub.learning.graph_extract`): MỘT `oneshot` JSON-schema (`EXTRACT_SCHEMA`: entities, facts{subject, relation, object?, fact, valid_from?, multi?, scope, source, evidence[]}, invalidations) qua runtime của hub (`hub.runs.runtime()`, hoặc `graph_extract_binary`) — như judge/curation, không tool, không MCP | `learning/graph_extract.py` |
| Trigger: run làm việc kết thúc (debounce `graph_extract_debounce_s`, newest-wins; KHÔNG theo lượt điều phối viên), hook `PreCompact` (ngay), xoay session điều phối viên (`manager.service._submit`, ngay), review job khi `learning.review_scope = graph` (thay fork; job `done` khi trích xong, tiêu Signal) | `graph_extract.on_*`, `review._start_graph`, `manager/service.py` |
| Hook `PreCompact`: `observe_hook.EVENTS`, `cli_surface` (`cfharness hook PreCompact`), `surface_rpc.HOOK_EVENTS` + `hook_observe` (gửi chỉ `session_id` + `trigger` manual/auto; KHÔNG `transcript_path`/`custom_instructions`), mặc định `runtime.claude.observe_hooks.events` | `observe_hook.py`, `cli_surface.py`, `mcp_server/surface_rpc.py`, `config.py` |
| Episode = chỉ mục `session_messages` (đã che secret), con trỏ theo nguồn (`session:<id>` hoặc `conv:<id>` cho điều phối viên) trong `hub_state learning.graph_cursor` (giữ 2000 nguồn mới nhất); cửa sổ = tin MỚI nhất ≤ `graph_extract_max_chars` | `GraphExtractor.collect/window` |
| Chống đầu độc: SQL chỉ lấy `user`/`steer`/`assistant` (run) hoặc `user`/`manager` (điều phối viên); không `tool_call`/`tool_result`, không tin subagent, không `tick`/`notify`, không run kỹ thuật. Mỗi fact PHẢI dẫn `evidence` là id tin trong cửa sổ và có tin đúng `source` khai (user/assistant) — thiếu → bỏ (`no_evidence`). Prompt dặn không trích nội dung agent dẫn từ web/tool/file, không trích chỉ thị. Fact vẫn qua guard (threat scan) | `collect`, `to_batches.grounded`, `EXTRACT_PROMPT` |
| Provenance: mỗi lô kèm episode `source_kind` = trigger, `source_ref` = `<nguồn>#<id đầu>-<id cuối>`, `run_id`/`session_id`; fact trỏ `source_episode_id`; `created_by=review`; độ tin cậy 0.6 (người nói) / 0.45 (agent kết luận) | `_extract` |
| Tầng: LLM chọn `agent`/`team`/`global`; không có quyền (như tool `memory`: `memory_scopes`, `_shared_team` với `run.team_id`) → hạ về `agent`. Lô tầng chung chỉ nhận episode gồm các tin được dẫn | `_tier`, `_extract` |
| Gộp thực thể: tên LLM → thực thể đã có theo canon → alias LLM khai → `set_embed_matcher` (chỗ cắm cho PR retrieval, mặc định không có); tên khác thành alias. Danh sách thực thể đã biết (≤ 80, tầng agent thấy) đưa vào prompt để LLM dùng lại tên | `knowledge/graph/resolve.py`, `known_entities` |
| Mâu thuẫn: giữ luật của PR store (cùng src + relation, không `multi` → đóng fact cũ, `valid_to` = `valid_from` mới). Invalidation LLM chỉ khi thực thể đã có | `GraphStore.plan_ops` |
| Lô bền: chạy thử `plan_ops` (thuần) và bỏ TỪNG thao tác hỏng (alias trùng thực thể khác, không có fact để đóng…) thay vì để cả lô bị chặn | `to_batches` |
| Trần: `--max-budget-usd` = `graph_extract_budget_usd` (0.05); `graph_extract_max_per_agent_per_day` (12); `graph_extract_daily_usd_per_agent` (0.5, tính cả trần lần tới); suất ghi graph của PR store vẫn áp. Lần gọi lỗi không tốn tiền: không dòng `runs`, không tiến con trỏ | `cap_problem` |
| Công tắc: `learning.graph_extract` (mặc định bật) + công tắc học agent/team (FR-LN-15). Run kind mới `graph_extract` (vào `NON_WORK_KINDS`, `TECHNICAL_KINDS`, `kpi.LEARNING_KINDS`, `digest.LEARNING_RUN_KINDS`) | `config.py`, `web/settings_schema.py` |
| Tool `memory` `target='graph'`: `facts=[{subject, relation, object?, fact, valid_from?, multi?}]`, `forget=[{subject, relation, object?}]`, `scope`/`team_id` như MEMORY.md → `graph_write(created_by="agent")`. Episode = 3 lượt NGƯỜI gần nhất của session (`source_kind=agent_tool`) | `learning/service._agent_graph`, `mcp_server/hub_self.py` |

## Quyết định / lệch (PR write)
- Không thêm tool hub-self riêng `graph`: mở rộng tool `memory` (`target='graph'`) — cùng quyền tầng, cùng chỗ bật/tắt (`learning.agent_memory_tool`),
  không thêm tool vào danh sách (prompt cache). PR retrieval có thể thêm `graph_search`.
- `review_scope=graph` KHÔNG fork session (fork thấy output tool → trái bất biến chống đầu độc): review job chạy bộ trích xuất. Khi đó MEMORY.md/skill
  chỉ còn tool `memory` của agent cập nhật — người chọn đánh đổi này.
- Trích theo run làm việc kết thúc (không phải theo lượt), nhưng một session resume nhiều lần → nhiều lần trích (mỗi lần chỉ phần mới, có debounce);
  trần lần/ngày hãm tổng.
- Trợ lý (assistant) vẫn có thể diễn đạt lại nội dung web/tool — chặn bằng prompt + bằng chứng + threat scan, KHÔNG chặn tuyệt đối. Fact do agent
  kết luận có độ tin cậy thấp hơn fact người nói.
- Cửa sổ giữ tin MỚI nhất; phần cũ hơn quá trần bị bỏ (`omitted` trong kết quả) — PreCompact giúp session dài được trích từng đoạn.

## Việc dở sau PR write
- Recall (PR retrieval) — chưa có gì đọc graph vào prompt. Bộ khớp embedding (`set_embed_matcher`) chưa cắm.
- Chưa mirror ghi MEMORY.md (tool `memory` target memory/user) thành episode (`on_memory_write` của Hermes).
- `agent_context` không primary (subagent/cron) của Hermes: hub không phân biệt — run do lịch chạy vẫn được trích như run thường.
- LLM xác nhận mâu thuẫn / gộp hai thực thể đã có: PR quality.
- `review_scope=graph` không đưa Signal (correction…) vào prompt trích xuất.
