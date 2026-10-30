# Tool `memory` của agent — ghi memory ngay trong session (kiểu Hermes)

## Vì sao
Trước đây chỉ review fork (sau ≥ 10 lượt/vòng tool hoặc Signal mạnh) mới đề xuất được memory, nên agent không tự ghi lại được điều
người vừa sửa/dặn. Hermes cho agent chính gọi tool `memory` trực tiếp; review nền chỉ là lưới an toàn. Hub làm giống vậy nhưng giữ
nguyên pipeline an toàn.

## Hành vi
- Tool `memory` trên hub-self cho vai trò `worker`, `lead`, `reviewer`, `knowledge` (run thường), `manager`. KHÔNG có trong `learning`
  (review fork dùng `propose_memory`), run `eval`/`eval_judge`/`curator` hay run có `eval_overlay` (daemon từ chối).
- Tham số: `target` (`memory` = MEMORY.md, `user` = USER.md), một thao tác `action/content/old_text` hoặc lô `operations` (≤ 10).
  Thao tác như Hermes: `add`, `patch` (old_text là chuỗi con duy nhất của một mục → thay bằng content), `remove`. Không có `replace`.
- Daemon: `LearningService._agent_memory` → `pipeline.propose(target="memory", scope="agent", created_by="agent", payload.operations)`.
  Guard (threat scan, mệnh lệnh, không phải tiếng Anh, khẳng định tiêu cực, một fact – một kho, pin từng dòng), hạn mức ký tự,
  rate (`learning.rate.max_changes_per_agent_per_day` — dùng CHUNG suất với review), ledger + rollback như mọi thay đổi do học.
- Lô: áp tuần tự, sai một thao tác → cả lô bị chặn (báo `thao tác #n`). Hạn mức xét trên kết quả cuối, nên khi đầy agent gửi một lô
  vừa gộp/xoá vừa thêm. Một lô = một proposal = một ledger entry = một suất.
- Kết quả: `status` (applied/blocked/deferred_rate_limited/suggested) + `reason`/`rule` + `entries` hiện có + `usage`. Ghi xuống đĩa
  ngay; system prompt của session hiện tại không đổi (snapshot đóng băng, giữ prompt cache) — thấy từ session sau.
- `learning.agent_memory_tool` (mặc định `true`): khi bật, `learning/runtime.apply_mcp` gắn hub-self vai trò `knowledge` cho mọi run thường
  (trước đây chỉ khi agent có skill). Tắt → tool biến mất và daemon từ chối op `memory`.
- `propose_memory` (review) cũng nhận `operations` — gỡ chỗ kẹt "đầy mà mỗi proposal chỉ một thao tác".
- `apply_memory_op` chặn `§` cả khi `patch` (trước chỉ chặn khi `add` → patch có thể tách một mục thành hai).

## Lệch Hermes
- Không có `write_approval`/hàng chờ duyệt (hub là "báo chứ không hỏi"); thay bằng guard + ledger + rollback + regression monitor.
- Không có tầng team/global qua tool này — ghi tầng chung vẫn chỉ qua review (L3: eval + canary).
- Hermes không giới hạn số lần ghi; hub tính vào `rate.max_changes_per_agent_per_day`.

## Việc dở
- Hướng dẫn "lưu chủ động" hiện chỉ nằm trong mô tả tool (không thêm khối vào system prompt để không đổi prompt hash của mọi agent).
- Rollback memory vẫn theo cả file (xem báo cáo audit memory 2026-10-30) — rollback một lô agent ghi cũng gỡ các thay đổi sau nó.
