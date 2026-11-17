# Hệ thống ngoài hub (`systems/`) — điều phối viên quản lý job launchd của các app trong ~/Workspace

## Vì sao
Người nhắn `cfharness chat` "tạm tắt thông báo Telegram của viec-nhom-2" → điều phối viên không làm được: app đó KHÔNG phải agent/task của
hub mà là app riêng chạy bằng launchd (`com.viecnhom2.bantin`, `…nhacviec`), còn agent trong run thì bị sandbox + classifier chặn `launchctl`
(`schedule_persistence`). Giờ hub biết các hệ thống đó và daemon tự gọi `launchctl` (ngoài sandbox) sau khi kiểm quyền.

## Mô hình
- Hệ thống = thư mục con trực tiếp của gốc (`systems.roots`, mặc định `workspace.allowed_roots` = `~/Workspace`) có dấu hiệu dự án
  (.git, package.json, pyproject.toml, README/AGENTS/CLAUDE.md…) hoặc có job launchd trỏ vào. Bỏ `_*`, `.*`, thư mục của chính hub.
- Job = plist trong `~/Library/LaunchAgents`, gán theo `WorkingDirectory` rồi tới đường dẫn tuyệt đối trong `ProgramArguments`. Plist là nguồn
  sự thật (đọc lại mỗi lần). Job có lệnh `cfharness`/tên cũ bị loại. Plist mà `plistlib` không đọc được → `plutil -convert xml1` (task-force).
- Registry: `hub_state["systems.registry"]`, job hub tạm dừng: `hub_state["systems.paused"]`. Không migration.
- pause = `bootout` + `disable` (không tự nạp lại khi đăng nhập/khởi động lại máy); resume = `enable` + `bootstrap gui/<uid> <plist>`;
  restart = `kickstart -k` (từ chối khi đang tạm dừng).

## Quyền
| Ai | Được |
|---|---|
| Người (CLI/socket có token) | `system scan [--apply]`, `add`, `rm`, `lock/unlock`, `pause/resume/restart` mọi job đã đăng ký |
| Điều phối viên | tool `system_list`, `system_get{log}` (đọc), `system_job{label, action, reason}` — CHỈ job thuộc hệ thống đã đăng ký, không bị khoá; trần `manager.max_system_ops_per_hour` (10) |
| Agent trong run | không gì cả: `system.*` ghi ∈ HUMAN_ONLY, `cfharness system <ghi>` ∈ `HUB_HUMAN_ONLY_CLI`, `launchctl` = destructive + sandbox |

Kiểm 2 lớp như M7: `ManagerOps._rpc` → `permissions.check_rpc("system.job")` → `SystemService.job_check`; daemon kiểm lại `MANAGER_ALLOWED` +
`check_rpc`; service kiểm lần 3 (khoá + người gọi). Event `system.job`, audit kind `system.job` (actor `manager:<id>` hoặc `user`).
Persona template `manager.yaml` đổi "never touch production" → cho phép `system_job` với job đã đăng ký; agent `dieu-phoi` trên home thật đã
được người cập nhật SOUL (revision 3). Hội thoại cũ giữ system prompt cũ (frozen theo session) → mở hội thoại mới.

## Nghiệm thu thật (2026-10-19)
`cfharness system scan --apply` → 33 hệ thống, 91 job. `cfharness manager chat --new "viec-nhom-2 đang gửi thông báo hằng ngày đến Telegram -
tạm tắt…"` → điều phối viên gọi `system_get` rồi `system_job pause com.viecnhom2.bantin`; `launchctl print-disabled` thấy
`com.viecnhom2.bantin => disabled`, job không còn trong `launchctl list`; audit #377 `system.job` actor `manager:dieu-phoi`.

## Test
`tests/test_systems.py` (launchctl GIẢ — không bao giờ gọi launchctl thật): nhận diện/đăng ký, pause/resume/restart, đuôi log, manager trong quyền,
ngoài phạm vi (lớp 1 + lớp 2), job khoá, không đăng ký/khoá được, trần tần suất, agent trong run bị chặn, classifier.

## Web + chat (2026-11-17)
- Web `/systems` (`web/systems_pages.py`, `systems.html`, `system.html`, macro hàng job `_systems.html`; menu "Hệ thống ngoài" nhóm "Hệ thống"):
  danh sách `system.list` + tóm tắt; `?scan=1` = `system.scan {}` (quét thử, KHÔNG đăng ký) → nút "Đăng ký N hệ thống mới" (`system.scan{apply}`);
  form thêm theo đường dẫn (`system.add`); `/systems/{id}` = `system.get{log_bytes: 4000}` (đuôi log đã che, autoescape) + "Gỡ khỏi danh sách".
  Thao tác qua `ACTIONS`: `system_job{label, action}` (pause|resume|restart), `system_lock{label, on}`, `system_rm`, `system_scan_apply`,
  `system_add` — mọi cái có `confirm` (gỡ = danger), `audit=True` → `wrap_with_audit` ghi kênh `web`. Web là người (HUMAN_ONLY qua token người).
- Chat `/hethong` (alias `/systems`; Slack `/cfh hethong`) ở `ChatBot._c_systems`: tin danh sách văn bản thuần (tên app từ README, label/lịch
  từ plist → `visible()`), nút ở tin RIÊNG, nhãn chỉ mang label job (bỏ label có ký tự vô hình/quá 200). Mã nút `yp|yr|yk` (bước 1) →
  sửa tại chỗ thành "Xác nhận …"/"Huỷ" (`ypc|yrc|ykc`/`ycx`) → `self.call("system.job", {reason: "người … qua <kênh>"})` (cổng người +
  allowlist from.id + audit `<kênh>:<chat>`). Mọi thao tác job 2 bước (đụng app đang chạy thật). Chat KHÔNG có nút đăng ký/gỡ/khoá.
- Test: `tests/test_web_systems.py`, `tests/test_telegram.py::test_systems_command_job_buttons_two_steps`,
  `tests/test_slack.py::test_systems_job_buttons_two_steps`; thế giới giả dùng chung `test_systems.make_world` + `attach_world`.

## Việc dở
- Trang web chưa tự làm mới trạng thái job (tải lại trang); chưa có nút trong cảnh 3D `/office`.
- Chỉ launchd (macOS) miền `gui/<uid>`; chưa có systemd/cron/docker.
- `job_paths` tách đối số theo khoảng trắng — đường dẫn có dấu cách trong `ProgramArguments` có thể không gán được (WorkingDirectory vẫn đúng).
