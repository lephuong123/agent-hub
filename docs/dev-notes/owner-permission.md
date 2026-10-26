# Điều phối viên xin phép người bằng nút + giảm cổng chặn (2026-10-19)

Mục tiêu: người chỉ nhắn yêu cầu và trả lời câu hỏi. Không gõ `cfharness approve …`, `guard sandbox …`. Migration **v15**
(`questions.action`). Test: `tests/test_owner_permission.py`.

## Vấn đề gặp ở hub thật
- `guard.enabled=false` làm mất kênh xin quyền (hook + `--permission-prompt-tool`). Khi đó `claude -p --permission-mode default`
  **tự từ chối** mọi Bash/Edit/Write cần hỏi (log: "Claude requested permissions to write to …AGENTS.md, but you haven't granted it
  yet", "This command requires approval"). Hậu quả: agent chỉ còn đọc, B1a kẹt, kiem-tra-he-thong không chạy được `curl` tới NocoDB.
- Điều phối viên không có cách xin quyền người, nên chỉ chuyển lời "agent không làm được" kèm lệnh cho người tự gõ. Người gõ
  `!cfharness approve ap_…` vào chat cũng không có tác dụng.

## Đã làm
| Phần | Nội dung |
|---|---|
| `manager/owner_actions.py` | Tool `ask_owner_permission{kind, …, reason}` với các kind: `approve_action{approval_id}`, `sandbox_localhost{agent}`, `sandbox_domain{agent, domain}`, `agent_update{agent, changes}`. HUB tự soạn chữ câu hỏi từ dữ liệu thật (toàn bộ lệnh của approval, diff cấu hình cũ → mới). Thao tác được CHỐT vào `questions.action` ngay lúc hỏi. Người chọn "1. Cho phép" thì hub thực hiện qua `Daemon.call_as(<internal = người>)` (cổng + audit theo kênh trả lời), ghi `answer.action_result`, phát event `question.action`, rồi gửi kết quả về điều phối viên (`[hub] Đã thực hiện … / KHÔNG cho phép / LỖI`) |
| `questions/service.py` | `open(action=)` luôn `open`/người (không bao giờ `with_manager`); `answer()` từ chối mọi người trả lời không phải người với câu hỏi có action; `_act_then_continue`; `answer_in_chat`; `continuation_text` |
| `manager/service.py` `_owner_shortcut` | Tin của NGƯỜI trong hội thoại (`manager.chat` ∈ HUMAN_ONLY). (a) `approve\|duyệt\|cho phép\|deny\|từ chối ap_<id> [ghi chú]`, chấp nhận tiền tố `!cfharness`. (b) `answer q_<id> 1`. (c) Nếu hội thoại có đúng 1 câu hỏi `open`: chấp nhận "1"/"1,3", nhãn phương án, "ok/đồng ý/duyệt…" (= phương án 1), "không/từ chối…" (= phương án 2), chỉ khi tin ≤ 40 ký tự. (d) Nếu không có câu hỏi nào và cả tin chỉ là một câu duyệt ngắn ("duyệt", "approve tiếp đi"…) và có đúng 1 approval đang chờ, thì duyệt approval đó. Hub thực hiện ngay rồi gắn ghi chú `[hub] …` vào CHÍNH lượt chat đó (không sinh thêm lượt). Tin bình thường không bị đụng |
| Guard | `guard.hold_uncertain` (mặc định `true`). Đặt `false` thì chỉ `destructive` mới hỏi người. `uncertain` vẫn giữ ở vùng `hub`/`policy` và với `mcp_require_approval` (`GuardService.needs_human`) |
| Guard tắt | `runs._prepare_spec`: khi `guard.enabled=false` thì agent không `plan`/`read_only` được cho phép sẵn tool của mình (`--allowedTools`; khi `tools` là null thì dùng `GUARD_OFF_ALLOWED_TOOLS`). Vẫn giữ `static_disallowed_tools` + mẫu chặn theo repo + sandbox OS |
| Trần điều phối viên | Tạo agent 5 → 20/ngày, task 20 → 60/giờ, run 10 → 30/giờ. `approval_wait_min` 15 → 2 (điều phối viên biết sớm để xin phép bằng nút) |
| Persona/prompt | Tự gỡ kẹt: thiếu tool thì `agent_update` hoặc giao lại, rồi `task_retry`. Chỉ hỏi người khi cần quyền người, và hỏi bằng nút. Không bao giờ bảo người gõ lệnh. Tự chọn mặc định hợp lý. "Chỉ đọc" vẫn cần Bash. Thông điệp lỗi `_DENIED` trỏ tới `ask_owner_permission` |

## Bất biến giữ nguyên
> Cập nhật 2026-10-26 (`approval-profiles.md`): điều phối viên ĐƯỢC quyết approval mà người đã giao cho nó (`routed_to=manager`,
> `approval.delegate_to_manager`, lớp người cho phép, ngoài danh sách "không bao giờ tự động"). Mọi approval khác vẫn như dưới đây.
> `guard.hold_uncertain` mặc định nay theo `approval.profile` (balanced → false).

- Điều phối viên không tự duyệt approval chưa được giao, không tự cấp sandbox: với những thứ đó nó chỉ tạo câu hỏi. Câu hỏi có action thì `question.answer` chỉ nhận người
  (`by == "user"`). Lời gọi của điều phối viên vẫn bị chặn ở daemon (`question.answer` của manager chỉ dành cho câu hỏi `with_manager`).
- Không kind nào đụng agent điều phối viên (hiện tại + lịch sử), `bypassPermissions`/`allow_bypass`/`extra_args`/`subagents`/`sandbox*`,
  policy, golden, purge.
- Chữ mô tả thao tác là chữ của HUB. Lý do của điều phối viên chỉ là ghi chú, đã che secret và bỏ ký tự điều khiển.

## Lệch / lưu ý
- Duyệt bằng chat bỏ bước xác nhận 2 lần mà web/Telegram dùng cho lớp nặng (người đã chủ động gõ, hoặc đã đọc toàn bộ lệnh trong
  câu hỏi xin phép). Câu duyệt không kèm mã chỉ được nhận khi cả tin là câu duyệt ngắn VÀ có đúng 1 approval đang chờ.
- Ghi đè `.claude/**` trong repo: CLI vẫn hỏi quyền. Guard bật thì hub-guard trả lời. Guard tắt thì CLI từ chối (cần bật guard).
