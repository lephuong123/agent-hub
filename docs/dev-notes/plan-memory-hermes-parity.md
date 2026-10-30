# Plan: memory ngang Hermes (2026-10-30)

Nối tiếp `memory-hermes-gaps.md`. Giữ khác biệt cố ý: KHÔNG có `write_approval` (hub "báo chứ không hỏi"). Đã chốt: tầng chung qua
tool chỉ cho `lead`/`manager`/agent bật cờ; tìm theo nghĩa dùng embedding LOCAL. Mục "đối chiếu Hermes" phải mở `hermes-agent @ 903230b`
kiểm trước khi code.

## PR 1 — `session_search` (xong)
- `claude_adapter.RESULT_PREVIEW_MAX` 500 → 2.000; `session_search.TOOL_RESULT_MAX` 300 → 2.000. Hàng cũ giữ 300.
- `group`: gộp hit theo luồng (top session, ≤ 3 đoạn trích/luồng). Mặc định bật cho hub-self + MCP, tắt cho RPC/CLI (`--group`).
- Chưa làm: tóm tắt luồng bằng LLM (`summarize`, haiku, trần chi phí, cache theo `(thread, query)`) — chỉ làm nếu Hermes có.

## PR 2 — tool `memory`: bỏ rate limit + ghi tầng team/global
- `pipeline.rate_check`: miễn `created_by == "agent"`; `agent_quota_full` + đếm team/global loại `agent` khỏi `acts` (tool và review
  không ăn suất của nhau). Cầu dao `learning.rate.agent_tool_max_per_day` (null = không giới hạn). Hãm tự nhiên: trần ký tự kho + 600/mục.
- `hub_self` tool `memory` thêm `scope: agent|team|global` — chỉ hiện cho `lead`, `manager`, agent có `learning.shared_memory_write: true`.
- `_continue_tier`: `created_by == "agent"` áp dụng ngay như người (không eval trước), canary `watch` NHƯNG regression monitor được tự
  rollback (người: không bao giờ).
- Rollback THEO MỤC cho memory tầng team/global (mở rộng `ledger.is_entry_memory`), để gỡ một mục agent ghi không kéo mục khác theo.
- MEMORY_GUIDANCE: khi nào ghi tầng chung (đổi prompt hash một lần).

## PR 3 — lưu memory trước khi nén context
- Thêm `PreCompact` vào `observe_hook.EVENTS`, `cli_surface` hook choices, `surface_rpc.HOOK_EVENTS`; metadata `trigger` (auto/manual).
- `hook_observe` + `PreCompact` → `reviews.enqueue(agent, "pre_compress", delay_s=0, focus="memory")`, ép review digest (`fork=false`):
  nội dung trước nén đã nằm trong chỉ mục `session_messages` nên hook không cần chặn CLI (vẫn fail-open, 2 s).
- Cùng cơ chế khi điều phối viên xoay session (`ManagerService.rotate_reason`); đối chiếu Hermes: cả lúc kết thúc session.
- `pre_compress` miễn `review_max_per_agent_per_day`, debounce theo session. Cờ `learning.flush_on_compact: true`.
- Kiểm thủ công: bản `claude` đang ghim có bắn `PreCompact` khi autocompact ở `-p --resume` không.

## PR 4 — `MemoryProvider` + tìm theo nghĩa
- 4a `memory_providers/base.py`: `prefetch`, `sync_turn`, `on_pre_compress`, `on_session_end`, `tools()` (đối chiếu chữ ký Hermes).
  `prefetch` vào TIN ĐẦU (không vào system prompt — giữ cache); mọi kết quả qua `threats.scan_load` + redact.
- 4b provider `local-vector`: `sqlite-vec` + `fastembed` (multilingual-e5-small), extra `cfharness[vector]`, không cài → FTS5. Backfill lô
  nền như `run_backfill`. Tìm lai bm25 + cosine (RRF): `mode: semantic|hybrid`. Phạm vi nhìn tái dùng `_caller_vis`.
- 4c adapter Mem0/Honcho: tắt mặc định, secret qua secret store, bật thì ghi audit.
