# Web — "Quyền đã cấp", sửa lịch + lịch sử lượt, quản lý hội thoại điều phối viên (2026-11-10)

Đề xuất #3, #5, #7 (`plan-feature-proposals.md`). Nhánh `feat/web-permissions-schedules-manager`. Không migration.

## #3 Trang "Quyền đã cấp" — `/permissions` (`web/perm_pages.py`, `templates/permissions.html`)
Một chỗ xem + gỡ mọi quyền NGƯỜI đã nới cho agent. Menu: nhóm "Hệ thống" (cả sheet "Thêm" trên điện thoại). Chỉ RPC có sẵn:

| Khối | Đọc | Gỡ / thêm (`ACTIONS`) |
|---|---|---|
| Trust full theo agent | `guard.trust.list`, `config.get guard.trust_new_agents` (chỉ xem) | `perm_trust_off` → `guard.trust.set{mode: off}` |
| Không hỏi lại theo luật | `guard.grant.list` | `perm_grant_rm` → `guard.grant.rm{id, rule}` |
| Luật luôn cho phép | `guard.policy` → `always_allow` (cả mục đã gỡ, 20 gần nhất) | `perm_always_rm` → `guard.protect.rm{id: aa_…}` (→ `always_rm`, vô hiệu hoá có lịch sử). Builder từ chối id không bắt đầu bằng `aa_` |
| Ngoại lệ sandbox | `guard.sandbox.list` (host dev hiện một lần dù cũng nằm trong `allow_domains`) | `perm_localhost_off`, `perm_domain_rm`, `perm_dev_host_rm` |
| Tài nguyên bảo vệ | `guard.policy` → `resources` (mục `builtin` không có nút gỡ) | `perm_protect_add` → `guard.protect.add`; `perm_protect_rm` → `guard.protect.rm` (builder từ chối `aa_…`) |

Bậc xác nhận: THU HẸP quyền (gỡ grant/trust/luật/ngoại lệ, thêm tài nguyên bảo vệ) = 1 bước. NỚI quyền (gỡ tài nguyên bảo vệ) = 2 bước:
`Action.step2` mới trong `web/app.py` — `plan_action` trả `Plan("step2", kind="generic", message=…)` khi chưa `confirm2=yes`, `do_action` vẽ
`confirm.html` (tiêu đề "Xác nhận lần 2", mang `confirm2=yes`). Cổng chung cho cả 2D/3D (3D không dùng các thao tác này).
Không thêm/nới grant hay trust từ trang này (đã có ở nút duyệt, CLI, `/trust` trong `cfharness chat`).

An toàn: mọi method ∈ `auth.HUMAN_ONLY`, ngoài `MANAGER_ALLOWED` (ca đỏ lớp 2 trong `tests/test_manager_security.py`). Service tự ghi audit theo
`channel=web` (`guard.rule_grant`, `guard.trust`, `guard.sandbox_override`, `policy.changed`) → web gọi `audit=False`. Ghi `policy/` qua
`PolicyStore` của daemon (writer `add/remove` đã bọc `guard/integrity.attach`; `always_rm` tự `_rebaseline`) — test gọi `hub.integrity.check()`
sau khi thêm để chứng minh không bị khôi phục như tamper. Ghi chú tài nguyên/preview luật autoescape.
Chat: bước 2 "Luôn cho phép…"/"Không hỏi lại luật này…" (Telegram/Slack, `chat/base.py`) và trang `approve2.html` chỉ thêm tới trang web ngoài lệnh CLI.

## #5 Hoàn thiện Lịch chạy
- **Web** `GET /schedules/{id}` (`extra_pages.schedule_page`, `templates/schedule_edit.html`): form sửa điền sẵn (không đổi `kind`), tình trạng,
  lịch sử ≤ 50 lượt (`schedule.get{fires: 50}`: bắt đầu, mốc hẹn, kết quả + lý do, run/task, chi phí, kết thúc, "chạy ngay"). `POST
  /schedules/{id}/save`: lần 1 `schedule.preview` + trang xác nhận nêu từng thay đổi "cũ → mới" (`edit_changes`, thuần) + mốc chạy tới; lần 2
  `schedule.update` (audit kênh web). Ô ngân sách trống = mặc định hub; ô nơi nhận trống = xoá nơi nhận. Danh sách có link + nút "Sửa · lịch sử".
- **Telegram/Slack** `/lich`: tin danh sách (văn bản thuần) + tin RIÊNG có nút (WT-8; nhãn chỉ id lịch `sc_…` do hub sinh, ≤ 10 lịch).
  `sp` tạm dừng = một chạm; `sr` bật lại → bước 2 sửa tại chỗ ("Xác nhận BẬT LẠI"/"Huỷ") → `src`. Ghi qua `bot.call` (cổng người + allowlist
  from.id + audit `<kênh>:<chat>`), nonce tiêu trước RPC (khung `CallbackStore` có sẵn).
- **CLI** `cfharness schedule edit <id> --clear-to` (alias `--clear`) → `schedule.update{deliver_to: null}`; dùng cùng `--to` → lỗi.
- **Hội thoại điều phối viên**: tin role `schedule` → `present.mgr_message` trả mục `note` (web: dòng `mm-note info`; `cfharness chat`/TUI:
  `chatview.history_items` → note; 3D: `chat_messages` đã nhận `note`).

## #7 Quản lý hội thoại điều phối viên
- RPC `manager.archive|unarchive {conversation_id}` (`ManagerService.archive`): chỉ đặt/xoá `archived_at` (cột có từ v13). Tin, run, audit giữ
  nguyên. Hội thoại đang có run chạy → Conflict. `manager.chat{conversation_id}` vào hội thoại đã lưu trữ → tự khôi phục (không mất tin người).
  `manager.conversations{archived: true}` → CHỈ hội thoại đã lưu trữ; Conv có thêm `archived_at`. ∈ HUMAN_ONLY + `MUTATING_MANAGER` (tự audit),
  ngoài `MANAGER_ALLOWED`; cổng service `require_human`. CLI `cfharness manager archive|unarchive <conv>`, `manager conversations --archived`
  (∈ `HUB_HUMAN_ONLY_CLI`).
- **Web `/manager`** (sau khi gộp main có "luồng việc", 2026-11-14): giao diện lưu trữ dùng bản của main (`conv_archive` →
  `manager.conversation_update{archived}`, menu ⋯ của hội thoại); nhánh này chỉ còn thêm nút "Chạy nhịp ngay" (`manager_tick`, xác nhận)
  ở thanh thông tin. RPC `manager.archive|unarchive` giữ làm lối gọi CLI/RPC (Conflict khi hội thoại đang chạy). Mục "Nhịp tự động" và
  banner chỉ xem của bản nhánh đã bỏ.

## Test
`tests/test_web_permissions.py`, `tests/test_manager_archive.py`, ca mới trong `test_web_pages_extra.py`, `test_web_manager.py`,
`test_telegram.py`, `test_slack.py`, `test_schedules.py`, `test_manager_security.py`, `test_sec_auth.py`.

## Chưa làm
- Thêm grant/trust/ngoại lệ sandbox mới từ trang Quyền đã cấp (chỉ gỡ); mẫu chặn theo repo (`guard.repo_deny`) chưa hiện.
- Telegram/Slack chưa có nút "Chạy ngay"/sửa lịch; `/lich` chỉ nút cho ≤ 10 lịch đầu.
- Xoá cứng hội thoại (chỉ lưu trữ).
