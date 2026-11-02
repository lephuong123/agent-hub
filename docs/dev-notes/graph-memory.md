# Graph memory — đã làm (PR 1: store)

Plan + đối chiếu Hermes + chia PR: `plan-graph-memory.md`. PR này là nền móng: CHƯA có gì đọc graph vào prompt và CHƯA có gì tự ghi
graph (trích xuất cuối session = PR write, recall = PR retrieval). Hành vi agent hiện tại không đổi.

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
- Chưa có tool hub-self `graph` / trích xuất cuối session (PR write) và chưa có recall/chèn tin đầu (PR retrieval).
- `mem_vectors` mới chỉ là bảng + helper; chưa có embedder (bge-m3), chưa có sqlite-vec, chưa có backfill.
- Bản chụp eval chứa TOÀN BỘ graph agent thấy — graph rất lớn thì cần cắt theo golden task (PR retrieval).
- `version_map` (`knowledge versions`) chưa liệt kê graph; web/TUI chưa hiển thị graph.

## Test
`tests/test_graph_memory.py` (20 ca, không gọi claude, vector số giả): migration + luật migration Python, NFC/gấp dấu, kế hoạch thuần,
ghi qua pipeline/ledger + tìm không dấu + láng giềng, che secret, mâu thuẫn/`multi`, đóng hiệu lực theo id/cạnh, guard (threat scan, trần
fact, trùng, không trần tổng, tiếng Việt), pin, suất riêng, quyền tầng, chặn run eval, rollback theo hàng (độc lập, mở lại fact cũ, bỏ qua hàng
đổi sau, rollback của rollback), canary `graph://` + ghim (ẩn hàng và ẩn phép đóng hiệu lực), eval overlay/fingerprint/materialize(_before),
purge/clone/export/import, vector theo model/phiên bản.
