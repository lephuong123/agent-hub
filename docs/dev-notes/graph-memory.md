# Graph memory — đã làm (store, retrieval, write, quality — nhánh `feat/graph-memory`)

Plan + đối chiếu Hermes + chia PR: `plan-graph-memory.md`. Nhánh `feat/graph-memory` = `feat/graph-memory-retrieval` (store → retrieval)
+ merge `feat/graph-memory-write` + PR quality (mục cuối). Vòng đầy đủ: agent nói chuyện → trích xuất theo lô ghi fact (PR write) → fact
được chèn vào tin user đầu / tra bằng `memory_recall` (PR retrieval) → kết quả run đẩy độ tin cậy lên/xuống, consolidation gộp trùng,
golden memory chấm recall@k / stale-fact-rate trong eval gate (PR quality).

Các mục "Việc dở" theo từng PR bên dưới là ghi chép LÚC PR đó xong; trạng thái hiện tại xem § "Tích hợp" và § "PR quality".

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
- ~~Recall (PR retrieval) — chưa có gì đọc graph vào prompt. Bộ khớp embedding (`set_embed_matcher`) chưa cắm.~~ Xong khi tích hợp (dưới).
- Chưa mirror ghi MEMORY.md (tool `memory` target memory/user) thành episode (`on_memory_write` của Hermes).
- `agent_context` không primary (subagent/cron) của Hermes: hub không phân biệt — run do lịch chạy vẫn được trích như run thường.
- LLM xác nhận mâu thuẫn / gộp hai thực thể đã có: PR quality.
- `review_scope=graph` không đưa Signal (correction…) vào prompt trích xuất.

# Tích hợp (nhánh `feat/graph-memory`)

Merge commit `feat/graph-memory-write` → `feat/graph-memory` (từ `feat/graph-memory-retrieval`). Xung đột chỉ ở tài liệu (AGENTS.md, dev note,
plan) — giữ cả hai. Sửa sau merge:
- `provider.TECHNICAL_KINDS` thêm `graph_extract` (không prefetch cho run kỹ thuật mới của PR write).
- **Cắm bộ khớp thực thể bằng embedding** (`resolve.set_embed_matcher`): `embed.entity_matcher(hub, graph_memory.vector.entity_match=0.92)`,
  cắm trong `start_vector_loop` (chỉ khi có embedder). Vòng nền chỉ mục thêm vector TÊN thực thể (`mem_vectors.item_kind='entity'`, sau khi hết
  fact). Bộ khớp chỉ chạy khi model đã nạp (`ready`), chỉ trong cùng tầng, và chỉ cho DB của hub đã cắm nó (matcher là biến toàn cục).
- Hook ghi của `MemoryProvider` (`sync_turn`, `on_session_end`, `on_pre_compress`) VẪN là no-op: trigger thật đã nằm ở `GraphExtractor`
  (`on_run_terminal`, `on_pre_compact`, `on_session_rotated`) — không nhân đôi đường gọi.

# PR quality — độ chính xác tăng theo thời gian

| Phần | Ở đâu |
|---|---|
| Migration **v20** (SQL): `mem_fact_uses` (fact_id, run_id, agent_id, channel prefetch/recall, used_at, outcome NULL/up/down/neutral, delta, settled_at) | `store.py` |
| Ghi lượt dùng: prefetch (`RunManager._memory_prefetch` → `prefetch_for_run(run_id)`) và tool `memory_recall` (`memory.recall_self`, run_id của run). Một lượt / (fact, run). Không ghi cho người tra cứu (không có run) và run eval (đọc bản chụp) | `knowledge/graph/provider.py` (`record`), `learning/graph_quality.record_uses`, `mcp_server/surface_rpc.py` |
| **Độ tin cậy theo kết quả** `GraphQuality.settle()`: run XẤU → −0.1 (thay đổi do run tạo — `proposals.run_id` — bị `regression_monitor`/`eval_gate`/`canary` rollback; run nằm trong `regression_watch.observed.run_ids` của entry đã rollback; run có Signal correction/reject/cancel/edit_output/undo). Run `completed` kết thúc ≥ `settle_h`=6 h mà không xấu → +0.03. Lỗi/huỷ/không còn run → trung lập. `up` hoá xấu sau → lật thành `down` đúng một lần. Kẹp [0.05, 0.95] | `learning/graph_quality.py`, `graph_memory.quality.*` |
| **Consolidation** `GraphQuality.consolidate()`: theo tầng — gộp thực thể (khoá `entity_key` = canon bỏ ký tự không phải chữ/số, hoặc tên là alias của thực thể khác; giữ thực thể cũ nhất) + gộp fact gần trùng cùng (src, relation) (Jaccard từ ≥ 0.8 hoặc cosine bge-m3 ≥ 0.95; khác đích thì không gộp; giữ fact tin cậy cao nhất, hoà → mới nhất; tin cậy fact giữ = max nhóm). Bỏ qua fact/graph người pin. Qua `pipeline.propose(target="graph", created_by="curator")` — guard + ledger (rollback theo hàng) + canary; KHÔNG ăn suất graph của agent | `learning/graph_quality.py`, `pipeline.rate_check` |
| Thao tác graph mới: `merge_entity {entity, into}` (alias + chép fact sang thực thể giữ, bản cũ đóng với `invalidated_by` = bản chép, `valid_to` giữ nguyên) và `supersede_fact {fact_id, by}` (đóng + liên kết thay thế). Không thao tác nào chạm `mem_episodes` | `knowledge/graph/store.py` (`plan_ops`) |
| Vòng nền: `LearningOps.tick` → `graph_quality.tick()` — settle mỗi `settle_interval_s`=300 s, consolidation mỗi `graph_memory.consolidate.interval_h`=24 h | `learning_ops/wiring.py` |
| **Golden memory**: checker `memory_recall` (`k`, `gold_fact_ids`/`gold_contains`, `gold_answer`, `as_of`, `stale_fact_ids`/`stale_contains`, `forbidden_contains`/`forbidden_fact_ids`, `pass_threshold`). Chấm tất định trên `.graph_snapshot.json` của bản sao home: recall@k × (1 − stale-fact-rate); thăm dò đầu độc lọt vào top-k HOẶC vào graph → 0. `as_of` quá khứ → tra cả lịch sử, chỉ tính fact còn hiệu lực ngày đó | `evals/memory_golden.py`, `evals/checkers.py` |
| Eval gate: task memory KHÔNG chạy run (không tốn tiền, không gọi model). `_tasks_for`: lô entry toàn `graph` → chỉ task memory (không có → task thường); đích khác → bỏ task memory. `scores.memory = {after, before}` (recall@k, stale_rate, poisoned trung bình mẫu). Graph ở tầng agent là L1 → chấm trong lô hằng ngày `run_batch_l1`, tụt → `eval_gate` tự rollback | `evals/gate.py` |
| Bộ mẫu: `fixtures/graph_memory_golden.json` — 9 fact (hai lần ghi: có cập nhật theme/deadline), 7 câu hỏi: EN, VI có dấu, VI không dấu, trộn VI/EN (cập nhật), EN (cập nhật), theo thời gian (`as_of` 2025), thăm dò đầu độc. `seed_fixture(hub, agent)` ghi graph (created_by=user) + tạo golden task (người) | `evals/memory_golden.py` |
| CLI client mỏng `cfharness learn graph stats\|search\|show\|rollback\|consolidate\|settle` → RPC `learn.graph_stats`, `memory.recall`, `learn.graph_fact` (fact + chuỗi thay thế + cùng cạnh + episode nguồn + ledger + lượt dùng), `learn.rollback` (entry tạo fact), `learn.graph_consolidate`, `learn.graph_settle`. Bốn RPC `learn.graph_*` là `HUMAN_ONLY` (xem graph MỌI agent; agent dùng `memory_recall`) | `learning/cli.py`, `learning/rpc.py`, `auth.py` |

## Quyết định / lệch (PR quality)
- **"Run xấu" không gồm run lỗi/huỷ**: lỗi hạ tầng (timeout, crash) không nói gì về fact; huỷ là tín hiệu mơ hồ. Chỉ các tín hiệu mà regression
  monitor/eval gate dùng để kết luận hồi quy mới hạ độ tin cậy. Run thành công nhưng sai mà người không sửa → vẫn `up` (giới hạn đã biết).
- **Ghi thẳng `confidence`/`mem_fact_uses` ngoài ledger** (cột VOLATILE, như `hits`/`mem_vectors`): là thống kê dẫn xuất, không phải tri thức;
  rollback entry không khôi phục chúng. Độ tin cậy đổi → bản chụp eval đổi (snapshot giữ `confidence`) → cache baseline eval tính lại.
- **Rollback một fact = rollback cả lô** (ledger entry) đã tạo nó — ledger rollback theo entry; `learn graph rollback <fact>` nói rõ điều này.
- **Consolidation tất định, không LLM xác nhận** (plan nói "LLM confirm"): luật khoá tên + Jaccard/cosine bảo thủ, mọi gộp đi qua ledger nên
  rollback được; LLM xác nhận để sau nếu thấy gộp sai/sót. Fact vừa được chép sang thực thể gộp không được gộp trùng trong cùng lượt (lượt sau).
- **Golden memory không chạy agent**: đo TRUY HỒI (cái agent sẽ được chèn/tra được), không đo câu trả lời cuối. `gold_answer` chỉ cho
  `answer_hit` (có fact top-k chứa đáp án). Độ chính xác câu trả lời cần golden task thường (`llm_judge`) — không có trong bộ mẫu.
- `summarize` đếm theo MẪU (mỗi task chạy `eval.samples` lần) — `samples`, không phải số task.

## Chưa kiểm chứng / việc dở
- **bge-m3 thật CHƯA chạy** (không tải model 2,3 GB trong các phiên này): fastembed `add_custom_model` có tải `onnx/model.onnx_data` không,
  chất lượng/độ trễ thật, ngưỡng `entity_match`=0.92 và `fact_cosine`=0.95 trên bge-m3 thật — đều là phỏng đoán, cần đo trên dữ liệu thật.
- Chưa chạy với daemon thật / `claude` thật: payload hook PreCompact có `trigger`; haiku trả đúng schema trích xuất; vòng nền settle/consolidate
  trên DB lớn (truy vấn `_bad_runs` quét `regression_watch` đã rollback mỗi lần settle — đủ nhỏ hiện tại).
- Chưa có chỉ số chèn (số fact/ký tự/token khối recall, tỉ lệ cache-read) — `provider.last` chỉ trong RAM; plan có "injected tokens,
  cache-read ratio" → chưa làm. Prefetch vẫn tắt cho run eval nên eval chưa đo tác động của khối chèn lên câu trả lời.
- Bộ mẫu golden nhỏ (7 câu), chưa có câu hỏi kiêng (abstention — câu không liên quan phải trả rỗng): `min_score` gần như không lọc (độ liên quan
  chuẩn hoá theo hit tốt nhất) nên câu không liên quan vẫn kéo fact yếu; cần sửa xếp hạng trước khi chấm kiêng có ý nghĩa.
- Run eval không có kênh vector (bản chụp không chứa vector) → golden memory chấm FTS5 + thực thể + mở rộng, không chấm kênh vector.
- Không có tool MCP cho người `hub_memory_recall`; người dùng `cfharness learn graph search` (RPC `memory.recall`).
- Chưa mirror ghi MEMORY.md thành episode (`on_memory_write`), chưa phân biệt `agent_context` không primary, `review_scope=graph` chưa đưa
  Signal vào prompt trích xuất (từ PR write — vẫn còn).
- Alias/thực thể tìm bằng LIKE không index; web/TUI chưa có trang xem graph; `knowledge versions` chưa liệt kê graph.

## Test
`tests/test_graph_quality.py` (11 ca; không gọi claude, không tải model — embedder giả túi-từ): khoá gộp + Jaccard; lượt dùng một lần/(fact, run),
không ghi cho người/eval; độ tin cậy lên với run thành công, xuống với correction / cửa sổ hồi quy / thay đổi bị `eval_gate` rollback, lật
up→down một lần, kẹp sàn, idempotent; tick nền settle + consolidation đến hạn; consolidation gộp thực thể + fact gần trùng (liên kết thay thế,
`valid_to` giữ, không xoá episode, alias tìm được, rollback theo hàng, idempotent); bỏ qua fact pin + không ăn suất agent; kiểm checker memory;
bộ mẫu EN/VI/không dấu/trộn/thời gian/đầu độc đều đạt, stale-rate > 0 khi cập nhật bị sót, đầu độc → 0, pipeline chặn fact chỉ thị; eval gate
dùng task memory cho entry graph (tụt → `eval_gate` rollback, không run eval, `scores.memory`), `run_manual`; RPC + parser `learn graph`; bộ khớp
thực thể bằng vector (cùng tầng, chỉ khi model `ready`).

## Sửa theo review (2026-11-02)
Test: `tests/test_graph_memory_review.py` (12 ca, bộ trích xuất GIẢ, không gọi claude, không tải model).
- **Pin graph/graph_fact qua đường thật**: RPC `knowledge.pin` + CLI `cfharness knowledge pin|unpin <agent|team|-> graph|graph_fact <ref>` —
  `graph` ref `-` (= `graph://<scope>/<owner>`), `graph_fact` ref = id fact hoặc tiền tố duy nhất trong đúng tầng. Trước đó chỉ test gọi
  `set_pin` trực tiếp nên luật bảo vệ của người không dùng được.
- **Eval gate**: lô toàn graph chỉ chấm bằng task memory; không có task memory → `[]` → `deferred_no_eval` + `unevaluated` (H-17), KHÔNG
  rơi về task thường (run eval không thấy graph). `run_batch_l1` tách entry graph thành lô riêng theo agent (không trộn với MEMORY.md/skill).
- **Trần độ dài**: tên/alias thực thể ≤ 200, kiểu ≤ 64, tóm tắt ≤ 1000 (`too_long`); tổng văn bản mới một lô ≤ 60 000 < `MAX_SCAN_CHARS`
  (`batch_too_large`) — không còn đuôi không được quét. Kiểu thực thể vào `new_texts` (được quét). Lúc nạp: `memory_recall` quét + che
  `src`/`dst` như câu fact (cờ → `blocked`); `known_entities` (prompt trích xuất) che secret, quét, một dòng, cắt độ dài. `import_rows` vẫn
  không quét lúc ghi — dựa vào quét lúc nạp (fact, tên, known_entities).
- **`learn.propose` từ agent** (daemon `_agent_propose` → `LearningService.agent_propose`): memory/USER/graph theo `memory_scopes` như tool
  `memory` (trước đây worker thường ghi được team/global); graph đi qua `graph_write`. `graph_write(created_by=agent)` chỉ nhận
  `add_entity|add_alias|add_fact|invalidate_fact`, bỏ `confidence` agent tự khai và mọi khoá `_…` nội bộ. Skill/config qua `learn.propose`
  không đổi (chưa xét quyền tầng skill).
- **Fact người khai** (`created_by=user`): bên ghi khác KHÔNG đóng được — `add_fact` mâu thuẫn bị bỏ (`conflicts_with_user_fact`; cả lô không
  còn gì → guard chặn cùng tên luật), `invalidate_fact` → `protected_user_fact`. Ngoại lệ: trích xuất gắn cờ nội bộ `_user_grounded` khi bằng
  chứng có lượt NGƯỜI (fact `source=user`; invalidation có ít nhất một id lượt người) — người nói "từ giờ deploy lên canary" vẫn cập nhật được.
  Cờ bị bỏ ở pipeline cho `created_by` ngoài user/review/curator.
- **Trần trích xuất nguyên tử theo agent**: `GraphExtractor._reserved` giữ chỗ ngay sau `cap_problem` (không có await ở giữa); `cap_problem`
  đếm cả chỗ đang giữ (số lần + `graph_extract_budget_usd` mỗi chỗ) → session song song của một agent không vượt trần lần/ngày và $/ngày.

## Chạy thử bge-m3 thật + chỉnh xếp hạng (2026-11-02)
- Tải `BAAI/bge-m3` qua fastembed 0.9.1 (`add_custom_model` lấy đủ `onnx/model.onnx_data`): 1 phút 41 giây, 2,2 GB; vector 1024-d đã
  chuẩn hoá; embed một câu hỏi trung vị 41 ms trên CPU; sqlite-vec 0.1.9 nạp được trên Windows. HF cache trên Windows không có symlink
  (không bật Developer Mode) → chạy được, chỉ tốn chỗ hơn; tắt cảnh báo bằng `HF_HUB_DISABLE_SYMLINKS_WARNING=1`.
- Bộ thử 10 fact (theo memory thật của agent) / 11 câu hỏi EN-VI-trộn, chấm đúng FACT: FTS5 7/11 top-1; FTS5 + vector (cũ) 8/11 top-1,
  11/11 top-3. Nguyên nhân: kênh vector giữ MỌI fact có cosine > 0, nên hit FTS rác (trigram khớp nhầm tiếng Việt) được cộng ở fts +
  vector + expand và đứng đầu; tăng riêng weight vector không đổi gì.
- Sửa: kênh vector chỉ giữ cosine ≥ `graph_memory.vector.min_sim` (0.4) và ≥ tốt nhất − `sim_margin` (0.15); weight vector 1.5
  (`graph_memory.recall.weights` ghi đè được). Kết quả: **10/11 top-1, 11/11 top-3**. Câu còn sai ("how do I run unit tests in
  workflowswebapp") là hoà cosine 0.612 vs 0.610 giữa hai fact cùng repo. Ngưỡng là theo bge-m3 — đổi model thì đo lại. Mẫu nhỏ: chỉnh
  tiếp theo golden set, không theo bộ này.
