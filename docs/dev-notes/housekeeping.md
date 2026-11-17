# Dung lượng HUB_HOME + dọn thư mục làm việc (đề xuất #8)

Module `src/cfharness/housekeeping.py`. Không migration, không loại event mới. Test: `tests/test_housekeeping.py`,
`tests/test_web_pages_extra.py::test_health_card_shows_disk_and_cleanup`.

## Báo (chỉ đọc)
- `disk_usage(home)`: byte + số file theo mục cấp 1 của HUB_HOME (không theo symlink), gộp riêng `agents/*/workspaces`, chỗ trống ổ đĩa.
- `scan(home, conn, older_than_days=None)` — loại thư mục:
  | kind | điều kiện | dọn được |
  |---|---|---|
  | `conv_missing` | `agents/<a>/workspaces/conv-<id>` mà `manager_conversations` không có `<id>` | có |
  | `conv_archived` | hội thoại có `archived_at` | có (khôi phục hội thoại sau đó: `_conv_cwd` tạo lại thư mục rỗng, session vẫn resume được; tệp đính kèm cũ mất) |
  | `scratch_orphan` | `agents/<a>/workspaces/<x>` không run nào có id `<x>` (cùng agent), không cwd run nào nằm trong, không task mở/agent `workspace_default`/team `policies.workspace` trỏ vào | có |
  | `worktree_orphan` | `HUB_HOME/worktrees/<x>` không ai tham chiếu | có |
  | `scratch_stale` | chỉ run đã kết thúc tham chiếu, lần cuối + mtime thư mục cũ hơn N ngày (doctor đếm ở 30) | chỉ khi `--older-than N` |
  | `task_worktree_done` | git worktree NGOÀI HUB_HOME (`_wt/…`) của task done/archived còn trên đĩa | KHÔNG — repo của người, chỉ gợi ý `git worktree remove` |
  Run "sống" = `queued|running|waiting_human|interrupted` (interrupted có thể tự resume). Thư mục chứa cwd của run sống / task mở → `busy`.
  Không đọc được DB → không đoán mồ côi (danh sách rỗng).
- `hub doctor` thêm kiểm `disk` (info), `disk_free` (warning < 2 GB), `cleanup` (warning khi có thư mục dọn được), `cleanup_stale`,
  `task_worktree:<path>` (info) + khoá `housekeeping` trong JSON. Chạy được khi daemon dừng (DB chỉ đọc).
- RPC `hub.housekeeping {older_than_days?, refresh?}` (HUMAN_ONLY, chỉ đọc, cache 10 phút, quét trong luồng phụ với kết nối DB chỉ đọc riêng)
  → thẻ Sức khoẻ web (2 dòng: "Dung lượng HUB_HOME", "Thư mục làm việc dọn được"). Web KHÔNG có nút dọn.

## Dọn
- `cfharness hub doctor --cleanup [--older-than NGÀY] [--yes]` → `hub.housekeeping{refresh}` → in danh sách → y/N → `hub.cleanup{paths, older_than_days}`.
- `hub.cleanup` (HUMAN_ONLY, tự audit qua `wrap_with_audit`, không ở MANAGER_ALLOWED): tính LẠI danh sách lúc gọi, chỉ xoá giao của danh sách
  người đã xem với ứng viên hiện tại; kiểm lại cwd của run sống; `_safe_target` chỉ nhận đúng `agents/<a>/workspaces/<x>` hoặc
  `worktrees/<x>`, không symlink ở cấp nào. Chỉ `shutil.rmtree` thư mục — không bao giờ đụng `runs`/ledger/audit/DB.
- Agent: `cfharness hub …` đã forbidden (`HUB_HUMAN_ONLY_CLI["hub"] = None`); RPC chặn bằng HUMAN_ONLY.

## Chưa làm
- Nút dọn trên web (cần `Action` có bước xác nhận liệt kê thư mục) — hiện chỉ gợi ý lệnh terminal.
- Không tự dọn theo lịch; không dọn `checkpoints/` (đã có `retention.checkpoints_days` trong `recovery.purge_retention`), `logs/`, `backups/`, `exports/`
  — doctor chỉ báo dung lượng của chúng.
- `agents/*/runtime/<run>` sót đã được `integrity.cleanup_runtime_dirs` dọn lúc daemon khởi động, không báo lại ở đây.
