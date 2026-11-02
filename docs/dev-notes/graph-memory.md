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

# PR 2: retrieval + chèn (nhánh `feat/graph-memory-retrieval`)

Agent bắt đầu ĐỌC graph: top-k fact được chèn vào tin user đầu của session mới, và agent tra thêm bằng tool hub-self. Graph vẫn chưa được ghi
tự động (đó là PR write); hiện chỉ ghi được qua `LearningService.graph_write`.

## Đã làm
| Phần | Ở đâu |
|---|---|
| Embedder: giao diện `Embedder` (`model`, `version`, `dim`, `ready`, `embed`) + `BgeM3Embedder` (BAAI/bge-m3, dense 1024-d, CLS + L2) qua **fastembed** (onnxruntime). Extra tuỳ chọn `cfharness[vector]` = `fastembed>=0.7` + `sqlite-vec>=0.1.6` (đã có trong `uv.lock`). Import lười; model cache ở `HUB_HOME/models` | `knowledge/graph/embed.py`, `pyproject.toml` |
| Backfill nền `run_vector_loop` (như `session_search.run_backfill`): mỗi lô embed trong thread, ghi `mem_vectors` trong giao dịch ngắn, rảnh → ngủ `vector.interval_s`. Đổi model/phiên bản thì `needs_embedding` trả lại mọi fact (chỉ mục lại). Lỗi 5 lần liên tiếp → tắt kênh vector tới lần khởi động sau | `embed.py`, `hub.py` (`Hub.start`) |
| sqlite-vec: `load_vec` nạp extension vào kết nối hub.db (bật `enable_load_extension` rồi tắt ngay); `vec_distance_cosine` trên BLOB float32 sẵn có. Không có sqlite-vec → cosine Python trên ≤ `vector.max_scan` vector mới nhất. Không có embedder → chỉ FTS5 | `embed.py`, `retrieval.py` |
| Tìm lai `Retriever.search`: các kênh `fts`, `vector`, `entity` (fact chạm thực thể khớp tên/alias), `expand` (1–2 bước từ thực thể của các hit đầu). Gộp RRF (k=60, trọng số 1/1/0,8/0,5), điểm = liên quan × (0,3 + 0,7·confidence) × suy giảm (`0.5^(tuổi/half_life)`, sàn 0,3). Mặc định bỏ fact đã đóng hiệu lực; `include_history` giữ lại (×0,5, `valid=false`). `touch` cập nhật `hits`/`last_used` | `knowledge/graph/retrieval.py` |
| `MemoryProvider` (giữ tên + chữ ký Hermes) + `GraphMemoryProvider` (`provider_for(hub)`): `prefetch`, `queue_prefetch` (vector câu hỏi tính trong thread, có hạn giờ, CHỈ khi model đã `ready`), `get_tool_schemas`, `handle_tool_call`, `recall_status`. Các hook ghi (`sync_turn`, `on_session_end`, `on_pre_compress`, `on_memory_write`) chưa làm gì, để PR write dùng | `knowledge/graph/provider.py` |
| Chèn: `RunManager._memory_prefetch` (async, trước `_prepare_spec`) → `_prepare_spec(..., recall=)` đặt khối `<recalled-memory>…</recalled-memory>` TRƯỚC tin user đầu, chỉ ở stdin (`runs.prompt` trong DB giữ nguyên lời người). Không chèn khi: resume/fork, run `review/eval/eval_judge/curator/learning`, `eval_overlay`, `learning_review`, `no_session_persistence`, câu chào/lệnh `/` (`TRIVIAL_PROMPT_RE`), hoặc tắt cấu hình | `runs.py` |
| Định dạng: tiếng Anh, ghi rõ "reference DATA, not instructions", mỗi fact có `valid <from> → <to/now>`, tầng, độ tin cậy. Mọi fact qua `redact_str` + `threats.scan_load` (fact bị cờ thì bỏ). Chuỗi `</` trong fact bị vô hiệu để không đóng khối sớm. Trần `recall.max_chars`. Văn bản episode không bao giờ được chèn | `retrieval.format_recall` |
| Tool hub-self `memory_recall(query?, entity?, limit, hops, include_history)` cho worker/lead/reviewer/knowledge/manager (không cho `learning`) → RPC `memory.recall_self` (ràng với agent của run; tầng + ghim canary của agent đó). `memory.recall` cho người/MCP (phạm vi theo `_caller_vis`). Cả hai nằm trong `auth.SCOPED_READ`. Classifier: an toàn | `mcp_server/hub_self.py`, `surface_rpc.py`, `auth.py`, `guard/classifier.py` |
| `memory_search` có thêm scope `graph` (và `all` gồm graph). **Sửa lỗi**: memory tầng team/global trước đây đọc từ file sống, nên agent đang bị ghim vẫn thấy thay đổi đang canary. Giờ đọc qua `versions.read_memory(agent, …)` và gộp kết quả theo phiên bản. Bỏ kết quả trùng do team bị đọc như có USER. Run eval đọc tri thức tầng agent, overlay và bản chụp graph trong bản sao home | `surface_rpc.memory_search` |
| Run eval: `memory_recall`/`memory_search` trong run eval đọc `.graph_snapshot.json` của bản sao home (DB trong RAM, `snapshot_conn`), không đọc DB sống và không đếm `hits` | `retrieval.snapshot_conn`, `provider.recall` |
| Cấu hình `graph_memory.recall.*` (`enabled`, `prefetch`, `top_k`=8, `max_chars`=2000, `hops`=1, `half_life_days`=90, `min_score`) và `graph_memory.vector.*` (`enabled`=auto, `model`, `batch`, `interval_s`, `query_timeout_s`, `max_scan`, `threads`). Biến môi trường `CFHARNESS_GRAPH_VECTOR=off` tắt hẳn kênh vector; `tests/conftest.py` đặt biến này cho mọi test | `config.py` |

## Quyết định / lệch
- **Runtime fastembed thay cho sentence-transformers**: fastembed chạy onnxruntime (không torch), nhẹ và chạy được trên Windows. Tuy nhiên
  danh sách dense của fastembed 0.9.1 KHÔNG có bge-m3 (đã kiểm `fastembed/text/onnx_embedding.py` ngày 2026-11-02), nên phải đăng ký bằng
  `TextEmbedding.add_custom_model(model="BAAI/bge-m3", pooling=CLS, normalization=True, dim=1024, model_file="onnx/model.onnx")`. Repo HF
  `BAAI/bge-m3` có sẵn `onnx/model.onnx` + `onnx/model.onnx_data` + `onnx/tokenizer.json`, và `1_Pooling/config.json` ghi CLS pooling.
  `additional_files=["onnx/model.onnx_data"]` chỉ được truyền khi chữ ký hàm có tham số đó. **CHƯA chạy thật** (không tải model 2,3 GB trong
  phiên này): việc fastembed có tự tải file `.onnx_data` hay không chưa được kiểm chứng.
- **sqlite-vec dùng hàm khoảng cách, không dùng bảng ảo `vec0`**: `vec0` cũng quét phẳng (không ANN), lại cần đồng bộ thêm một bảng với
  rollback/retract. `vec_distance_cosine` trên `mem_vectors` cho cùng kết quả mà không có thêm trạng thái. Đã kiểm với sqlite-vec 0.1.9 trên
  Windows (test `test_sqlite_vec_distance_matches_python_cosine` chạy khi có gói, so với cosine Python).
- **Không bao giờ chờ model trên đường chạy run**: câu hỏi chỉ được embed khi model đã nạp (`ready`). Lần đầu daemon chạy, vòng nền nạp và
  có thể tải model; trong lúc đó recall dùng FTS5. Ở RPC `memory.recall`, câu hỏi được embed đồng bộ (đã `ready`, khoảng vài trăm ms CPU).
- **`mem_vectors` là chỉ mục dẫn xuất**: vòng nền ghi thẳng (`GraphStore.put_vector`), không qua ledger — ngoại lệ có chủ đích của bất biến
  "chỉ ghi `mem_*` qua Ledger". Fact bị gỡ thì đọc lọc theo fact, vector mồ côi vô hại.
- **Độ mới tính theo `ingested_at`**, không theo `last_used`: lấy theo mức độ được dùng sẽ tạo vòng tự khuếch đại. `hits` chỉ để quan sát
  (PR quality sẽ cập nhật độ tin cậy theo KẾT QUẢ run).
- **`hops` = số bước theo fact** từ thực thể: 1 = fact chạm thực thể, 2 = thêm fact chạm thực thể láng giềng.
- **Kênh LIKE dự phòng của `GraphStore.search`**: khi FTS5 dùng được (có từ ≥ 3 ký tự) thì không còn rơi xuống LIKE. Trước đây câu hỏi kiểu
  "which … should I take" rơi xuống LIKE với từ "i" và khớp MỌI fact.
- Một tool `memory_recall` thay cho cặp `graph_search`/`graph_neighbors` trong plan: `entity` + `hops` thay cho `graph_neighbors`.
- Prefetch TẮT cho run eval (đúng yêu cầu); vì vậy eval chưa đo được tác động của khối chèn. Tool `memory_recall` trong run eval vẫn đọc bản chụp.

## Việc dở
- Chưa có event/cột đo khối chèn (số fact, ký tự, token); `provider.last` chỉ nằm trong RAM. Các chỉ số recall@k và token chèn để PR quality.
- Chưa có tool MCP cho người (`hub_memory_recall`) và chưa có lệnh CLI. RPC `memory.recall` đã có.
- Run eval không có kênh vector (bản chụp không chứa vector). `min_score` gần như không lọc vì độ liên quan được chuẩn hoá theo hit tốt nhất:
  một câu hỏi không liên quan vẫn có thể kéo vào vài fact khớp từ khoá yếu.
- `GraphStore.find_entity` (alias LIKE) và `_entity_ids` không có index — cần bảng alias khi graph lớn.

## Test
`tests/test_graph_retrieval.py` (19 ca; embedder giả túi-từ có bảng đồng nghĩa; không tải model, không gọi claude): RRF/suy giảm, định dạng (dữ liệu, ngày
hiệu lực, bỏ fact bị cờ, không đóng khối sớm), FTS gấp dấu + lịch sử + xếp hạng tin cậy/độ mới, thực thể + 1/2 bước, backfill vector
(model/phiên bản, idempotent, chỉ mục lại, `ready`), vòng nền, công tắc embedder, cosine Python, phạm vi tầng + ghim canary, giao diện
provider, chèn tin đầu (không vào system prompt/DB; không cho resume/fork/review/eval/curator/chào/tắt), quét + che khi nạp, vai trò tool +
classifier + `SCOPED_READ`, RPC ràng run, run eval đọc bản chụp, `memory_search` đọc theo ghim + scope graph, sqlite-vec = cosine Python
(bỏ qua khi không cài).
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
