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

## Việc dở
- Web/Telegram chưa có màn/nút cho hệ thống (chỉ CLI + điều phối viên). Telegram: lệnh chat ghi trạng thái phải qua `bot.call`.
- Chỉ launchd (macOS) miền `gui/<uid>`; chưa có systemd/cron/docker.
- `job_paths` tách đối số theo khoảng trắng — đường dẫn có dấu cách trong `ProgramArguments` có thể không gán được (WorkingDirectory vẫn đúng).
