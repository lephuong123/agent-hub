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
  ledger + rollback như mọi thay đổi do học. Rate: KHÔNG giới hạn (như Hermes, 2026-10-30) — suất riêng `learning.rate.agent_tool_max_per_day`
  (policy, mặc định 0 = không giới hạn), không ăn suất review (`max_changes_per_agent_per_day`/`max_team_global_changes_per_day` chỉ đếm
  `pipeline.QUOTA_ACTORS`). Thứ hãm là trần ký tự kho + `memory_entry_chars`.
- Lô: áp tuần tự, sai một thao tác → cả lô bị chặn (báo `thao tác #n`). Hạn mức xét trên kết quả cuối, nên khi đầy agent gửi một lô
  vừa gộp/xoá vừa thêm. Một lô = một proposal = một ledger entry.
- `scope` (2026-10-30): `agent` (mặc định) | `team` | `global`. Ai được (`LearningService.memory_scopes`, daemon là ranh giới):
  lead → `team` của team mình LEAD (`team_id` khi lead nhiều team); điều phối viên hoặc policy `agents.<id>.shared_memory_write: true`
  → mọi tầng. `target=user` + `scope=global` = USER dùng chung (`user_profile`). Schema hub-self chỉ hiện `scope` khi được
  (`server_entry(memory_scopes=…)` → `--memory-scopes`; `hub_self.memory_tool_def`).
  Tầng chung: `_continue_tier` nhánh `created_by=agent` → áp dụng NGAY cho mọi agent của tầng, không eval, không ghim, canary mode
  `watch`; khác người: hồi quy → canary TỰ rollback THEO MỤC (`ledger.is_entry_memory` nay gồm memory tầng team/global). Nhiều watch
  cùng đích chạy song song; chỉ hoãn khi đích đang canary/shadow của review (`canary.busy(exclude_mode="watch")`). USER dùng chung vẫn
  rollback cả file.
- Kết quả: `status` (applied/blocked/deferred_rate_limited/suggested) + `reason`/`rule` + `entries` hiện có + `usage`. Ghi xuống đĩa
  ngay; system prompt của session hiện tại không đổi (snapshot đóng băng, giữ prompt cache) — thấy từ session sau.
- `learning.agent_memory_tool` (mặc định `true`): khi bật, `learning/runtime.apply_mcp` gắn hub-self vai trò `knowledge` cho mọi run thường
  (trước đây chỉ khi agent có skill). Tắt → tool biến mất và daemon từ chối op `memory`.
- `propose_memory` (review) cũng nhận `operations` — gỡ chỗ kẹt "đầy mà mỗi proposal chỉ một thao tác".
- `apply_memory_op` chặn `§` cả khi `patch` (trước chỉ chặn khi `add` → patch có thể tách một mục thành hai).

## Lệch Hermes
- Không có `write_approval`/hàng chờ duyệt (hub là "báo chứ không hỏi"); thay bằng guard + ledger + rollback + regression monitor.
- Hermes không có tầng team/global; hub cho tool ghi tầng chung nhưng giới hạn người được ghi (lead/điều phối viên/policy) và theo dõi
  + tự rollback theo mục.

## Việc dở
- Đã làm ở `memory-hermes-gaps.md`: hướng dẫn memory trong system prompt, rollback theo mục.
