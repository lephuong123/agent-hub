# Xoay vòng `logs/` của daemon + `run_logger` cho dispatcher/recovery (2026-10-30)

Đóng hai việc còn treo ở `hardening-LRN.md` ("`logs/` daemon chưa xoay vòng", "`run_logger` cho log dispatcher/recovery") và
mục 2 của `hardening-CLI.md`. NFR-21 (log có cấu trúc, có `run_id`/`agent_id`, xoay vòng).

## 1. Xoay vòng an toàn trên Windows (`logs.py`)
- Trước: `hub.log` dùng `RotatingFileHandler`. Trên Windows, file đang mở ở tiến trình khác (`cfharness daemon logs`, trình
  xem log, antivirus) không đổi tên được → `os.rename` ném `PermissionError` → `handleError` in traceback ra stderr
  (`daemon.out`), file KHÔNG xoay và lỗi lặp lại ở mọi dòng log sau đó. `daemon.out` (stdout/stderr thô) không xoay bao giờ.
- `SafeRotatingFileHandler`: xoay bằng `_move` = `os.replace`, lỗi thì chép sang `.N` rồi cắt file gốc (copytruncate).
  Thất bại hẳn (chép cũng lỗi) → không ném, nghỉ 60 s rồi mới thử lại (không thử ở mỗi dòng), không mất dòng.
- Theo tuổi: `purge_old_logs` xoá bản xoay vòng `*.log.N`/`*.out.N` có mtime cũ hơn `logs.max_age_days`, chạy lúc mở handler
  và sau mỗi lần xoay. File đang ghi (`hub.log`, `daemon.out`) và file khác trong `logs/` (`hook-timeouts.jsonl`) không bị đụng.
  Không xoay `hub.log` theo thời gian: file nhỏ thì để đó, tuổi chỉ giới hạn các bản cũ.
- `daemon.out`: xoay lúc khởi động nếu ≥ `max_bytes` (`spawn_background` trước khi mở; `run_foreground` nhánh pythonw tự mở
  file). KHÔNG xoay khi stdout do launchd/systemd mở (đổi tên dưới fd của nó sẽ làm nó ghi tiếp vào `.1`).
- Config mới (chỉ thêm): `logs.max_bytes` (10 MiB), `logs.backup_count` (5), `logs.max_age_days` (30; 0/null = không dọn theo
  tuổi). Đọc lúc daemon khởi động (`daemon._log_settings`, config hỏng → mặc định).

## 2. `run_logger` cho dispatcher/recovery
- Dispatcher chạy NGOÀI task asyncio của run nên contextvar `set_run_context` không có. `TaskDispatcher._rlog(run_id)` tra run →
  `run_logger(log, run_id, agent_id, task_id=…)`; dùng cho lỗi `_safe_terminal` và cảnh báo "tiến trình chết, không heartbeat".
- `recovery.reconcile`/`resume_interrupted`: mỗi quyết định theo run có một dòng gắn run/agent/task: dừng tiến trình mồ côi,
  `interrupted`, `waiting_human`, huỷ run eval, không resume vì file bị sửa, resume lỗi, "tiếp tục ở run …". Dòng tổng
  `khôi phục: {...}` giữ nguyên. `purge_agent` lỗi chỉ mục → gắn `agent_id`.
- `cfharness daemon logs --run R` / `--agent A` nay thấy cả các dòng này.

## Test
`tests/test_log_rotation.py` (8 test, đều đỏ trên code cũ): chặn rename (giả lập WinError 32) → vẫn xoay, không traceback;
người đọc giữ file mở (Windows thật); thất bại hẳn → nghỉ, không mất dòng; dọn theo tuổi; `daemon.out` xoay lúc khởi động;
log recovery/dispatcher có `run_id`/`agent_id`/`task_id`.
