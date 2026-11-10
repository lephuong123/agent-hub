# Cập nhật cfharness bằng một nút (web + CLI)

## Người dùng thấy gì
- **Web → Cài đặt**: thẻ "Cập nhật hệ thống" ở đầu trang (nạp bằng htmx, không chặn trang): bản đang chạy, bản mới nhất
  (link Release), nút **Cập nhật lên vX.Y.Z** (hộp xác nhận). Có run chạy/chờ → nút đổi thành "Cập nhật dù vậy" (force: run bị
  ngắt, tự tiếp tục khi daemon chạy lại — recovery.py). Sau khi bấm → trang `/update` tự poll 3 s, hiện bước + nhật ký.
- **CLI**: `cfharness update --check`, `cfharness update [-y] [--force] [--tag vX.Y.Z]` (theo dõi tới khi xong).
- Bản cài editable/từ mã nguồn: thẻ hiện lý do, không có nút (cập nhật bằng `git pull`).

## Luồng
1. RPC `update.start` (HUMAN_ONLY, audit kênh gọi) → `Updater.start`: kiểm kiểu cài (`uv-receipt.toml` có `git`), tag `vX.Y.Z` mới
   hơn bản đang chạy, không có cập nhật đang dở, không có run (trừ `force`).
2. Chép `update_helper.py` → `HUB_HOME/update/helper.py`, chạy `"<python gốc>" -I helper.py apply …` tách session.
   Lý do python gốc + `-I` + chỉ stdlib: `uv tool install --force` xoá/tạo lại venv của tool trong lúc helper chạy.
3. Daemon tự dừng sau 0,5 s (exit 0 → LaunchAgent/systemd không chạy lại vì `SuccessfulExit=false`/`on-failure`).
4. Helper: chờ pid daemon thoát (≤ 60 s, rồi SIGTERM) → `uv tool install --force --python X.Y git+https://github.com/lephuong123/agent-hub@<tag>`
   → LUÔN chạy `<venv>/bin/python -m cfharness daemon start` (kể cả khi cài lỗi) → `--version` → `state.json` `done|failed`.

## Giới hạn / việc dở
- Process `cfharness web` / `cfharness chat` đang mở vẫn chạy code cũ: thẻ báo "giao diện web đang chạy bản cũ" khi phiên bản
  web ≠ daemon. Chưa tự khởi động lại web (khoá phiên trong RAM — mở lại = đăng nhập lại).
- Windows: chưa kiểm thật (uv có thể không ghi đè được `cfharness.exe` nếu `cfharness web` còn chạy).
- ~~Không có rollback tự động~~ → có từ lưới an toàn (dưới).

## Lưới an toàn (chi tiết: `docs/dev-notes/backup.md` § "Cập nhật có lưới an toàn")
- Trước khi chạy helper, `update.start` tạo bản sao lưu `pre-update` (`backup.before_update`, mặc định bật; lỗi → huỷ cập nhật). `state.json.backup`.
- Helper thêm pha `health` (`hub.ping` qua socket, ≤ 60 s + kiểm lại sau 3 s). Không lên → `rolling_back`: dừng daemon còn sót, khôi phục hub.db
  từ bản pre-update NẾU `user_version` đã tăng (DB đã migrate giữ ở `backups/hub.db.replaced-*`), cài lại bản đang chạy, khởi động lại, kiểm lại.
  `state.json`: `rolled_back`, `rollback{ok, db_restored, …}`; thẻ web hiện "đã tự quay về".
- `cfharness update --rollback [-y] [--force]` → RPC `update.rollback` (HUMAN_ONLY, audit): quay về `v<version_before>` + DB trước (nếu schema tăng).
  Daemon không trả lời → CLI chạy helper tại chỗ. Không quay tiếp sau một lần quay về.

## Cập nhật khi daemon chạy dưới launchd (LaunchAgent `daemon install-service` / LaunchDaemon `admin install-daemon`)

Sự cố thật (hub `lamlinh`, 2026-11-10, v0.2.4 → 0.2.5): daemon dừng để cập nhật với mã 0 → launchd (`KeepAlive.SuccessfulExit=false`)
không chạy lại; helper tự `daemon start` → daemon mới chạy NGOÀI launchd (chết thì không ai bật lại; LaunchDaemon không còn quản lý).

Cách sửa (v0.2.7):
- `daemon.supervisor()`: launchd đặt `XPC_SERVICE_NAME` = Label của job; chỉ nhận nhãn `com.cfharness.daemon.*` (shell Terminal cũng có
  biến này, giá trị khác). Đã kiểm bằng LaunchAgent thật.
- `update.start` dưới launchd: daemon thoát mã **75** (`SUPERVISED_RESTART_CODE`) → launchd chạy lại; plan restart của helper =
  `daemon start --wait-supervisor 75` (chỉ CHỜ launchd, hết giờ mới tự bật). `XPC_SERVICE_NAME` bị gỡ khỏi env của helper.
- launchd chạy lại daemon NGAY khi bản cũ thoát, lúc `uv` còn cài đè venv: `run_foreground` → `_hold_while_updating` chờ tới khi helper
  qua pha `installing`, rồi thoát 75 để launchd chạy lại bằng mã mới (tối đa 20 phút; state cũ hơn 20 phút = helper treo → bỏ qua).
- Test: `tests/test_update.py::test_supervisor_*`, `test_plan_supervised_*`, `test_rpc_update_under_launchd_*`, `test_daemon_holds_*`,
  `test_wait_supervised_*`.
