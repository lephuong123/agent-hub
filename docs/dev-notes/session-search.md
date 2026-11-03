# Tìm kiếm hội thoại (session search) — FTS5, migration v17

Tương đương `session_search` của Hermes Agent: tìm trên TOÀN BỘ nội dung hội thoại bằng SQLite FTS5, tất định, KHÔNG gọi LLM.
Trước v17, `memory.search` chỉ tìm LIKE trên `runs.prompt`/`runs.summary` (BRD §FR-MS-02 ghi FTS5 là "mục tiêu") — nay đã có thật.

Mã: `src/cfharness/session_search.py` (schema, chỉ mục, 4 chế độ, CLI). RPC: `mcp_server/surface_rpc.py`. Test: `tests/test_session_search.py`.

## Dữ liệu được chỉ mục

| Nguồn | role | Ghi chú |
|---|---|---|
| `runs.prompt` (lúc `Store.insert_run`) | `user` | prompt ĐẦY ĐỦ (không phải `prompt_preview` 200 ký tự của `run.queued`). Bỏ qua run điều phối viên (prompt là khung; tin người nằm ở `manager_messages`) |
| event `user.message` (steer) | `steer` | bỏ qua run điều phối viên (như trên) |
| event `message.complete` | `assistant` | tin assistant HOÀN CHỈNH — CLI đã gộp các `message.delta` (không ghi DB) thành một tin; tối đa 8000 ký tự như adapter. Có `subagent_id` nếu là subagent |
| event `tool.start` | `tool_call` | `"<tên tool> <args_preview>"` (≤ 200 ký tự input), cột `tool` |
| event `tool.complete` | `tool_result` | `result_preview` cắt còn 2.000 ký tự (trước 2026-10-30: 300 — hàng cũ giữ nguyên); bỏ nếu rỗng |
| `manager_messages` (lúc `ManagerService._add_msg`) | giữ nguyên (`user`/`manager`/`notify`/`tick`/`schedule`) | `source='manager'`, `kind='manager_chat'`, `conversation_id` |
| `runs.summary` (chỉ khi backfill, run không còn `message.complete` vì event đã bị retention xoá) | `assistant` | để run cũ vẫn tìm được |

Không chỉ mục: `message.delta`/`reasoning.delta`, `steer.ack` (trùng `user.message`), event hệ thống.

Mỗi hàng `session_messages` mang `run_id, agent_id, team_id, task_id, session_id, conversation_id, subagent_id, kind (loại run), role, tool, ts`
(team/task/session/kind lấy từ hàng `runs`, cache trong `Indexer`). `ref` UNIQUE (`run:<id>:prompt`, `ev:<epoch>:<seq>`, `mm:<id>`, …) → chỉ mục idempotent.

## Thiết kế

- **Che secret TRƯỚC khi chỉ mục**: `redact.redact_str` (mẫu khoá kiểu `sk-ant-…`, `Bearer …` + giá trị secret hub đã đăng ký). Cả `text` lẫn `folded`
  và FTS chỉ chứa bản đã che → secret không bao giờ vào FTS (test `test_redaction_before_indexing`).
- **Tiếng Việt**: cột `folded` = gấp dấu theo TỪNG ký tự (NFD bỏ dấu, `đ`→`d`, chữ thường; giữ nguyên độ dài) — câu hỏi cũng được gấp, nên
  "cau hinh" khớp "cấu hình" và ngược lại. Lý do không dựa riêng vào tokenizer: `remove_diacritics` của SQLite KHÔNG đổi `đ`→`d`, và `trigram
  remove_diacritics` chỉ có từ 3.45. Vì độ dài giữ nguyên, vị trí khớp trên `folded` cắt thẳng đoạn trích từ `text` gốc (giữ dấu, đánh dấu «…»).
- **Tokenizer** chọn LÚC CHẠY trong migration (`pick_tokenizer`): `trigram remove_diacritics 1` → `trigram` → `unicode61 remove_diacritics 2`.
  Trigram = khớp chuỗi con (đường dẫn, tên file, mã lỗi, mọi ngôn ngữ); từ < 3 ký tự không khớp được bằng trigram → lọc thêm `LIKE` trên
  `folded`. Không có FTS5 → vẫn có `session_messages`, tìm bằng `LIKE` (`engine: "like"`). Bản build hiện tại: SQLite 3.50.4 → trigram.
- **FTS5 external-content** (`content='session_messages'`) + trigger AFTER INSERT/DELETE — một bản văn, xoá hàng thì FTS tự đồng bộ.
- **Điểm chỉ mục lúc ghi**: `Store.insert_event` (mọi event persisted đi qua `Hub._persist` → đây; daemon là writer duy nhất) và
  `Store.insert_run`; tin điều phối viên ở `ManagerService._add_msg`. Lỗi chỉ mục chỉ ghi log, không làm mất event.
- **Backfill LƯỜI, theo lô nền** (không trong migration): migration v17 chỉ tạo schema + ghi con trỏ `hub_state.session_index_backfill`
  (mốc trên rowid của runs/events/manager_messages lúc migration). `Hub.start` chạy `run_backfill`: mỗi lô 150 hàng một giao dịch ngắn,
  nhả event loop giữa các lô, tiếp tục sau restart. Lý do (đo trên Windows, máy dev): 100k event (≈74k tin cần chỉ mục, trigram) mất
  ~20 s — làm trong migration sẽ chặn daemon khởi động quá thời gian `spawn_background` chờ socket; theo lô thì lô chậm nhất ~0.4 s/300 hàng.
  Khi còn backfill, kết quả có `backfill_pending: true`. Truy vấn backfill dùng `+type` để buộc SQLite đi theo rowid / `events_run`
  thay vì index `events(type, ts)` (đo: chọn sai index làm một lô mất 15–22 s).
- **Retention**: job retention (`recovery.purge_retention`) xoá `events` > 30 ngày nhưng KHÔNG đụng `session_messages` — văn bản hội thoại
  đã chỉ mục được giữ CÙNG VÒNG ĐỜI với `runs` (giữ vĩnh viễn). Đây là chủ ý: transcript đầy đủ chỉ còn ở dạng đã che + cắt ngắn
  (tool result 2.000 ký tự), đủ để tìm lại "đã làm gì ở đâu". Ngoại lệ: `recovery.purge_agent` (xoá cứng agent) xoá luôn tin của agent đó.

## Bốn chế độ (`session.search {mode, …}`)

| mode | Tham số | Kết quả |
|---|---|---|
| `search` (mặc định) | `query` (từ / `"cụm từ"`), lọc `agent_id, team_id, task_id, run_id, session_id, conversation_id, kind, role, source, since, until` (ISO / `YYYY-MM-DD` / `7d` `12h` `30m`), `include_technical`, `group`, `limit` ≤ 100, `offset` | `group=true` → `threads[]` (xem dưới); không thì `hits[]`: `id` (message id), `run_id`, `session_id`/`conversation_id`, `thread`, `agent_id`, `role`, `tool`, `ts`, `snippet`, `score` (−bm25), `next_offset`, `engine` |
| `around` | `message_id`, `window` (mặc định 5, ≤ 50) | `messages[]` cùng luồng, tin gốc có `anchor: true` |
| `read` | `run_id` (cả tiền tố) \| `session_id` \| `conversation_id`, `role?`, `limit` ≤ 500, `offset` | transcript theo thời gian + `total`, `next_offset` |
| `browse` | các bộ lọc như `search` | `threads[]`: `thread`, `first_ts`, `last_ts`, `messages`, `runs`, `agent_id`, `title` (tin user đầu) |

**Gộp theo luồng (`group`, 2026-10-30 — như Hermes trả top session)**: xếp hạng tin như thường (quét tối đa `GROUP_SCAN_MAX` = 500 tin),
rồi gộp theo luồng giữ thứ tự hit tốt nhất. Mỗi luồng: `thread`, `agent_id`, `best_score`, `matches` (số tin khớp trong cửa sổ quét),
`first_ts`/`last_ts`, `title` (tin user đầu), `hits[]` ≤ 3 đoạn trích. `offset`/`limit` tính theo LUỒNG; `scan_truncated: true` khi cửa sổ
quét đầy (có thể còn luồng khớp yếu hơn). Mặc định BẬT cho agent (hub-self `memory_search`/`session_search`) và MCP `hub_session_search`;
TẮT cho RPC `session.search` và CLI (`--group` để bật) — giữ hình dạng cũ cho người gọi hiện có.

"Luồng" (thread) = hội thoại điều phối viên (`conversation_id`) › session `claude` (gồm mọi run resume cùng session) › run.
Mặc định ẩn phiên kỹ thuật `review|eval|eval_judge|curator|learning` (như `memory.search` cũ), trừ khi lọc `kind` hoặc `include_technical`.

## Phạm vi nhìn — kiểm ở DAEMON

`auth.SCOPED_READ` = `session.search`, `session.search_self`, `memory.search`, `memory.search_self`: daemon xác định đầy đủ người gọi
(cây tiến trình/Job Object/env như method ghi) thay vì đường nóng của method đọc. Rồi `SurfaceRpc._caller_vis`:

- người (token đúng, không dấu hiệu agent) / lời gọi nội bộ → thấy tất cả;
- điều phối viên được chỉ định (`hub.manager.is_manager`) → thấy tất cả, kể cả hội thoại điều phối viên;
- agent khác → chỉ tin `source='run'` của CHÍNH nó hoặc của run thuộc team nó là thành viên; không thấy hội thoại điều phối viên; `memory.search`
  với `agent_id` khác → `Blocked`; `read`/`around` ngoài phạm vi → `NotFound` (không lộ là có tồn tại);
- socket không token, không thuộc run nào → `Blocked`.

hub-self (`memory.search_self`/`session.search_self`): run phải đang chạy VÀ (qua socket) `caller.run_id` phải đúng run đó — không lấy
run_id của agent khác để mở rộng phạm vi. `run_socket_block` của điều phối viên miễn trừ `SCOPED_READ` (đọc thuần, tự giới hạn).

## Bề mặt

- RPC: `session.search`, `session.search_self`; `memory.search` giữ nguyên hình dạng kết quả (kind `transcript` nay từ FTS5, thêm `message_id`, `role`).
- hub-self `memory_search`: thêm `mode`, `message_id`, `window`, `run_id`, `session_id`, `agent_id`, `team_id`, `kind`, `role`, `since`, `until`,
  `offset`; `scope: sessions` hoặc có `mode` → tìm kiếm hội thoại đầy đủ. `query` không còn bắt buộc trong schema (browse/read không cần).
- MCP ngoài: `hub_memory_search` (như cũ) + `hub_session_search` (readOnly, 4 chế độ).
- CLI: `cfharness search "từ khoá" [--agent --team --kind --role --since --until --technical --limit --offset]`,
  `--around ID [--window N]`, `--read <run|tiền tố run|session|mc_…>`, `--browse`; `--json` cho kết quả thô.

## Việc dở / giới hạn

- Tin `tool_result` giữ 2.000 ký tự đầu (như Hermes; adapter `RESULT_PREVIEW_MAX` = 2.000) — output tool dài hơn vẫn không tìm được phần đuôi.
  Hàng chỉ mục trước 2026-10-30 chỉ có 300 ký tự và không khôi phục được (event gốc cũng chỉ giữ 500).
- Chưa tóm tắt luồng bằng LLM (nếu Hermes có — đối chiếu trước khi làm; xem `plan-memory-hermes-parity.md`).
- Chưa có lệnh `reindex`/xây lại FTS thủ công (`INSERT INTO session_fts(session_fts) VALUES('rebuild')` dùng được nếu cần).
- Web/TUI chưa có ô tìm kiếm hội thoại (chỉ RPC/MCP/CLI/hub-self).
- `web/demo.py` sửa `manager_messages.text/ts` trực tiếp (chỉ demo) → bản chỉ mục giữ nội dung cũ.
