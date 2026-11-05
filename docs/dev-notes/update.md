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
- Không có rollback tự động; cài lỗi → daemon chạy lại bằng venv còn lại, `state.json` ghi lỗi; cài tay theo README.
