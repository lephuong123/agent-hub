# Công tắc chung cổng duyệt — `gates.mode` (2026-10-29)

Chủ repo yêu cầu: "một chỗ để bật/tắt toàn bộ gate của hệ thống" + "cơ chế để điều phối viên approve/reject". Quyết định: chế độ
`manager` giao điều phối viên MỌI thứ, kể cả push/deploy.

## Các cổng chờ người (trước khi có công tắc)
| Cổng | Ai mở trước đây |
|---|---|
| Approval thao tác phá huỷ (hook guard) | người; điều phối viên chỉ lớp `approval.delegate_to_manager.classes`, ngoài `never_automatic` |
| Task bị chặn (gate viết trong prompt task → agent `task_block`) | người "Gỡ chặn"; điều phối viên `task.retry` theo nhịp gom sự kiện |
| Câu hỏi agent (`ask_human`) | trong task: điều phối viên trước; ngoài task: người |
| Điều phối viên xin phép (`ask_owner_permission`) | chỉ người |

Không phải cổng chờ (giữ nguyên ở mọi chế độ): forbidden (tự duyệt, sửa policy/guard, CLI chỉ người), ESTOP, sandbox OS, mẫu chặn tĩnh,
ngân sách, khoá Chrome.

## `gates.mode`
`config.set gates.mode human|manager|off` — chỉ người (config.set ∈ HUMAN_ONLY; `cfharness gates` ∈ `HUB_HUMAN_ONLY_CLI`).
Đọc qua `guard/profiles.gates_mode(config)` (giá trị lạ → human).

- **human** (mặc định): như trước.
- **manager**: `profiles.delegable` trả None → `GuardService.route_for` giao MỌI approval cho điều phối viên (kể cả `NEVER_RULES`/
  `NEVER_KLASS`: push, deploy, credential, mạng nội bộ); `ApprovalService.manager_decide` + `permissions.check_rpc` kiểm cùng hàm nên
  điều phối viên quyết được. Vẫn chỉ người: approval của run/agent điều phối viên (`route_for`, `manager_block`), điều phối viên
  không sẵn sàng, quá `delegation_timeout_s` → người. Câu hỏi ngoài task cũng `with_manager`. `ask_owner_permission` → hub thực hiện
  NGAY thao tác đã chốt (`owner_actions.build` vẫn kiểm hợp lệ, cấm đích là agent điều phối viên; audit actor `manager:<id>`).
  Task bị chặn → `wake_soon()` (không đợi `event_debounce_s`). Prompt nhịp có `GATES_NOTE`: điều phối viên là đại diện chủ cho mọi cổng.
- **off**: `GuardService.check` cho phép mọi thao tác lẽ ra phải hỏi (`guard_decisions.classifier=gates_off`); forbidden/ESTOP/khoá
  Chrome vẫn áp. Task bị chặn/câu hỏi/xin phép xử lý như `manager`.

Rủi ro đã chấp nhận: ở `manager`, điều phối viên bị prompt injection có thể duyệt push/deploy. Mọi quyết định có lý do + audit +
event `approval.resolved{by=manager:<id>}`.

Bề mặt: web Cài đặt → thẻ "Cổng duyệt" (3 nút, xác nhận), `cfharness gates [human|manager|off]`, `/gates` trong `cfharness chat`.
Test: `tests/test_gates.py`. Chưa làm: lệnh Telegram/Slack `/gates`.
