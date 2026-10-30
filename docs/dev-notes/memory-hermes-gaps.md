# Thu hẹp khoảng cách memory với Hermes (2026-10-30)

Theo báo cáo so sánh memory agent-hub ↔ Hermes (hermes-agent @ 903230b). Phần "agent ghi memory trong session" ở
`agent-memory-tool.md`.

## 1. Review trên digest (như `auxiliary.background_review`)
- `learning.review_model` khác model run nguồn, HOẶC ngữ cảnh session nguồn > `learning.review_digest_over_tokens` (mặc định
  150.000) → review KHÔNG `--resume --fork-session` mà chạy session mới; tin nhắn đầu = digest các tin gần nhất của session
  (`session_messages`, đã che secret) ≤ `learning.review_digest_chars` (mặc định 40.000, mỗi tin ≤ 1.500 ký tự), cũ trước, rồi
  prompt review. `run_opts.review_digest=true`, `fork=false`. Ngân sách: base + (digest/4 + 20k token) × giá nạp lại.
- Session chưa được chỉ mục / digest rỗng → fork như cũ. `review_digest_over_tokens: null` → luôn fork (trừ khi đổi model).
- Đánh đổi: review thấy tối đa ~40k ký tự gần nhất, không thấy tool output đầy đủ (chỉ mục giữ ≤ 2.000 ký tự/kết quả tool — trước 2026-10-30: 300).

## 2. Quét lúc nạp
`knowledge/prompt_blocks._safe`: mỗi mục memory (mọi tầng) quét `threats.scan_load` (injection, Unicode ẩn — KHÔNG gồm mẫu
policy_touch/self_escalation, vốn là luật cho thay đổi do học) trước khi vào system prompt; khớp → `[BLOCKED: …]`. Bắt cả mục
sửa tay ngoài pipeline.

## 3. Lệch định dạng + trần độ dài mục
- `pipeline._memory_drift`: file đọc rồi viết lại không ra đúng nội dung (bỏ qua CRLF/khoảng trắng cuối dòng; file chỉ có tiêu đề
  không tính) → guard chặn `format_drift` với mọi tác nhân trừ `user` (như Hermes từ chối ghi đè file lệch). Không tạo `.bak`:
  ledger đã giữ snapshot.
- `learning.limits.memory_entry_chars` (mặc định 600): mục do học ghi dài hơn → `entry_too_long` (một mục = một fact). Mục cũ dài
  sẵn không bị đụng; người (`created_by=user`) không bị giới hạn.
- `parse_memory` chuẩn hoá CRLF: trước đây file sửa bằng editor Windows bị dồn mọi mục thành một.

## 4. Hướng dẫn memory trong system prompt
`prompt_blocks.MEMORY_GUIDANCE` (sau các khối memory) khi `learning.agent_memory_tool` và `learning.knowledge_tools` bật. Thêm khối
làm đổi prompt hash của mọi agent MỘT lần (mất prompt cache một lần ở session mới).

## 5. Rollback memory theo mục
`ledger.is_entry_memory` (file memory tầng agent, kể cả entry rollback): rollback = `invert_memory(before, after, current)` —
gỡ mục entry đã thêm, trả bản cũ của mục entry đã sửa (đúng chỗ), thêm lại mục entry đã xoá; mục entry thêm/sửa mà đã bị thay
đổi sau đè lên thì bỏ qua. `later_entries` = [] → không cần `--chain`, regression monitor gỡ một fact xấu không còn gỡ các
fact thêm sau, và không còn Conflict vì entry sau của người. Tầng team/global giữ rollback cả file (canary ghim theo file).

## Còn lệch Hermes
- Không có cơ chế cắm memory ngoài (MemoryProvider: Honcho, Mem0…) / tìm theo ngữ nghĩa.
- Không có hook trước nén context (claude CLI tự nén).
- ~~`session_search`: kết quả tool giữ 300 ký tự, chưa gộp theo session~~ — xong 2026-10-30 (2.000 ký tự, `group`).
