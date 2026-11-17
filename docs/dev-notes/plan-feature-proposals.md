# Đề xuất chức năng tiếp theo (2026-11-10)

Rút từ mục "việc dở / lệch BRD" của dev-notes + đối chiếu mã. Không gồm trusted device (`plan-trusted-device.md`) và
quản trị user (`plan-admin-users.md`).

| # | Đề xuất | Bằng chứng | Phạm vi | Công |
|---|---|---|---|---|
| 1 | **Sao lưu/khôi phục HUB_HOME** (`cfharness backup create\|verify\|restore` + lịch mẫu) | không có mã backup; ledger/audit giữ vĩnh viễn trên 1 file SQLite; `hardening-SEC.md` §3 "hub.db chỉ phát hiện" | `backup.py` (SQLite online backup trong daemon), RPC, CLI, Cài đặt web | M |
| 2 | **Cập nhật có lưới an toàn** (chụp trước, tự quay về tag cũ nếu daemon không lên; `update --rollback`) | `update.md`: "Không có rollback tự động" | `update_helper.py`, `update/state.json` | S–M |
| 3 | **Trang "Quyền đã cấp"** trên web (grant theo luật, trust full, luật luôn cho phép, tài nguyên bảo vệ) | `web.md` §6; `chat/base.py:810` bảo gỡ bằng CLI | `web/` + RPC có sẵn `guard.grant.*`, `guard.trust.*` | M |
| 4 | **Rào chắn chi phí còn hở**: trần ngày riêng cho điều phối viên, trần requeue `interrupted`, tính cost run `crashed`, `budget.warning` vào notify mặc định, ước tính lịch theo chi phí thật | `M7-core.md` §6, `run-auto-resume.md`, `config.py:202,220`, `schedules.md` | `config.py`, `runs.py`, dispatcher, manager, schedules | S–M |
| 5 | **Hoàn thiện Lịch chạy**: sửa lịch + lịch sử lượt trên web, tạm dừng/bật từ chat | `web-extra-pages.md`, `schedules.md` | `web/extra_pages.py`, `chat/base.py` | S |
| 6 | **Proxy loopback chèn token MCP OAuth** (run > 1 h không 401, token ra khỏi file runtime) | `mcp-oauth.md` §8 | `mcp_client/oauth.py`, proxy mới, ngoại lệ sandbox 1 cổng | L |
| 7 | **Quản lý hội thoại điều phối viên**: archive/xoá mềm, nút "chạy nhịp ngay", xem hội thoại heartbeat | `M7-core.md` §6 (`archived_at` chưa RPC nào đặt), `M7-ui.md` §5 | `manager/service.py`, `manager/rpc.py`, `web/manager.py` | S |
| 8 | **Sức khoẻ/doctor mở rộng**: dung lượng HUB_HOME, dọn `workspaces/conv-*`/worktree mồ côi (mặc định chỉ báo), cảnh báo cache | `recovery.py` không dọn workspaces; `M4-A.md`, `M5-A.md` | `recovery.py`, `/partials/health`, `doctor` | S–M |
| 9 | **Trang "Hệ thống"** trên web + nút Telegram/Slack | `systems.md` việc dở | `web/`, `chat/base.py` | S–M |
| 10 | **Selftest bảo mật chạy thật** trước phát hành (`cfharness selftest --live`, home tạm, trần $) | `hardening-SEC.md` §3, `telegram.md`, `slack.md`, `web-public.md` chưa nghiệm thu thật | `scripts/` hoặc module mới (ngoài pytest) | M |
| 11 | **Tìm hội thoại trong `cfharness chat`/TUI** (`/search`) + `session reindex` | `session-search.md` | `tui/chat.py` | S |

Bất biến cần giữ (tóm tắt): restore/rollback/grant ∈ HUMAN_ONLY + `HUB_HUMAN_ONLY_CLI`; bản sao lưu 0600, bỏ/mã hoá `secrets/`,
`auth/`, thư mục sao lưu vào denyRead/denyWrite; migration chỉ tiến (rollback mã khi `user_version` đã tăng phải khôi phục DB từ
bản chụp); ghi `policy/` qua `integrity.attach`; nới quyền luôn 2 bước; hết ngân sách chặn run mới nhưng không giết approval
đang chờ (Q-04); không xoá `runs`/ledger/audit/`guard_decisions`; chữ agent qua `termsafe.clean`.

## Top 5 (giá trị / công) và thứ tự
1. **#4 Rào chắn chi phí** — nhanh, chặn rò tiền ngay.
2. **#1 Sao lưu/khôi phục** → rồi **#2 Cập nhật có lưới an toàn** (dùng lại bản chụp của #1).
3. **#5 + #7** gộp một PR web nhỏ.
4. **#3 Quyền đã cấp.**
5. Sau đó #8, #9, #11; #6 và #10 trước một bản phát hành lớn.
