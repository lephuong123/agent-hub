# Sao lưu / khôi phục HUB_HOME (`cfharness backup`) + lưới an toàn cập nhật

Đề xuất #1 + #2 trong `plan-feature-proposals.md`. Mã: `backup.py`, `backup_cli.py`, `web/backup_pages.py` (+ `_backup.html`),
`updater.py`, `update_helper.py`, `update_cli.py`. Test: `tests/test_backup.py`, `tests/test_sec_auth.py::test_backup_restore_and_update_rollback_human_only`.
Không migration DB.

## Người dùng thấy gì
- `cfharness backup create [--label x]` · `backup list` · `backup verify <id>` · `backup restore <id> [--stop] [--yes] [--confirm-id <id>]`.
- Web → Cài đặt: thẻ "Sao lưu" (dưới "Cập nhật hệ thống"): danh sách, lịch tự động + lần tới, nút **Tạo bản sao lưu ngay** (hộp xác nhận).
  Khôi phục KHÔNG có nút — mỗi dòng chỉ hiện lệnh `cfharness backup restore <id> --stop`.
- `cfharness update` giờ tự tạo bản `pre-update` trước khi cài; daemon không lên → tự quay về. `cfharness update --rollback` quay về tay.

## Bản sao lưu chứa gì
Một file `HUB_HOME/backups/<id>.tar.gz` (0600; thư mục `backups/` 0700) + file kèm `<id>.json` (manifest + sha256/cỡ của archive).
Id: `YYYYmmddTHHMMSS-<loại>[-nhãn]`, loại ∈ `manual|auto|pre-update|pre-restore`.

| Gồm | Bỏ (mặc định, không có tuỳ chọn gộp) |
|---|---|
| `hub.db` — SQLite online backup API, kết nối chỉ đọc riêng, một bước (ảnh nhất quán; WAL không chặn daemon ghi) | `secrets/`, `auth/`, `mcp_server/` (token MCP HTTP) |
| `config.yaml`, `policy/`, `agents/` (trừ `*/runtime`, `*/workspaces`, `*/.tier_overlay`), `global/`, `teams/` | `runs/`, `checkpoints/`, `logs/`, `workspaces/`, `worktrees/`, `evals/`, `models/` (tạm/lớn/tái tạo được) |
| `ledger/` (blob rollback), `snapshots/`, `mcp/`, `trash/`, `prompts/` (prompt đóng băng cho resume) | `update/`, `backups/`, socket/pid/lock, `cli_history` |

Manifest (`MANIFEST.json`, phần tử cuối của tar): `format`, `id`, `kind`, `created_at`, `cfharness_version`, `user_version`, `db{sha256,size}`,
`files[{path,size,sha256}]`, `skipped` (file đổi cỡ/biến mất giữa chừng), danh sách bỏ. Tên phần tử: `hub.db`, `home/<đường dẫn tương đối>`;
uid/gid/uname bị xoá, quyền 0600. Symlink và file đặc biệt bị bỏ (không bao giờ theo symlink).

**Không mã hoá**: stdlib không có mã hoá đối xứng; `cryptography` chỉ là dependency bắc cầu (không khai báo trực tiếp) → chọn BỎ hẳn secret thay
vì chép ra rồi mã hoá. Hệ quả: khôi phục sang máy khác phải `cfharness secret set …` lại, đăng nhập web lại, TOTP enroll lại, token MCP HTTP sinh lại.
Khôi phục trên cùng máy không đụng các thư mục này nên không mất gì.

## Kiểm (`verify`)
sha256 của archive (theo file kèm; thiếu file kèm → cảnh báo, vẫn kiểm tiếp) → từng phần tử: tên an toàn (không tuyệt đối/`..`, chỉ thuộc tập
sao lưu), là file thường, sha256 khớp manifest, không thừa/thiếu → `hub.db` giải nén ra `backups/.verify-*` → `PRAGMA integrity_check` = ok +
`user_version` khớp manifest. Thư mục tạm luôn bị xoá.

## Khôi phục (`restore`) — CHỈ người, CHỈ ngoại tuyến
- Không có RPC khôi phục. RPC `backup.restore_plan` (HUMAN_ONLY, chỉ đọc) trả kế hoạch; CLI `backup restore` làm phần ghi TẠI CHỖ sau khi daemon dừng.
- Lớp chặn agent: `backup` ∈ `HUB_HUMAN_ONLY_CLI` (mọi lệnh con forbidden với agent, kể cả `list`); `backup.create|verify|restore_plan` ∈ HUMAN_ONLY;
  `backup.restore` từ chối khi có `CFHARNESS_RUN_ID`; sandbox denyRead + denyWrite `HUB_HOME/backups` (+ `Read/Grep/Glob(backups/**)`,
  `classifier.sensitive_read`).
- Các bước: kế hoạch (+ kiểm) → chặn nếu không qua kiểm hoặc schema của bản sao lưu MỚI hơn mã đang chạy → xác nhận bước 1 (y/N) → bước 2 (gõ lại
  id; không TTY: `--yes` + `--confirm-id`) → daemon đang chạy: cần `--stop` (lệnh tự dừng + chạy lại sau), không thì mã thoát 2 →
  `backup.restore`: giữ `daemon.lock` suốt quá trình (daemon không thể khởi động chen), kiểm + giải nén vào `backups/.restore-*`, chụp hiện trạng
  thành bản **pre-restore**, ĐỒNG BỘ tập sao lưu về đúng bản (gỡ file thuộc tập mà bản sao lưu không có, ghi file của bản), giữ nguyên phần ngoài tập
  (secrets, auth, `agents/*/workspaces`, runs…), thay `hub.db` (gỡ `-wal/-shm`), rồi ghi lại `integrity_baseline` khớp `policy/` + `config.yaml`
  trên đĩa (`rebaseline_offline`, cùng cách quét với `IntegrityMonitor`) — daemon khởi động không coi đó là sửa ngoài RPC.
- Bản sao lưu schema cũ hơn: cho phép (daemon chạy migration tiến khi khởi động), có cảnh báo.

## Lịch tự động — BẬT mặc định `daily 03:30`
Lý do: ledger/audit/tri thức giữ vĩnh viễn trên MỘT file SQLite (`hardening-SEC.md` §3: hub.db chỉ được phát hiện, không tự khôi phục); một bản
nén/ngày, giữ 7, bỏ runs/checkpoints/logs — tốn ít đĩa so với rủi ro mất hết. Vòng nền riêng trong daemon (`BackupService`, start ở `hub.started`,
nhịp `backup.tick_s`), KHÔNG dùng `schedules/` (lịch ở đó bắn điều phối viên/agent/task tốn tiền, có trần của manager — không hợp với việc nội bộ),
chỉ dùng lại parser `schedules/cron.py`. Mốc kế tiếp = `next_after(spec, bản auto gần nhất | lúc daemon khởi động)` → máy ngủ qua mốc thì chạy bù
đúng một lần ở nhịp sau. Bỏ lượt khi đang cập nhật/đang tạo bản khác. Event `backup.created {id, kind, size, pruned}` / `backup.failed {kind, error}`.

## Cấu hình `backup.*`
`schedule` ("daily 03:30"; "off" = tắt), `keep` (7, bản manual+auto), `keep_safety` (3, bản pre-update+pre-restore), `before_update` (true),
`compresslevel` (5), `tick_s` (60). `config set` kiểm `schedule` đọc được và `keep*` ≥ 1. Bản pre-update đang được `update/state.json` tham chiếu
(cần cho `--rollback`) không bao giờ bị retention xoá. Nhánh `backup` không nằm trong `integrity.SECURITY_KEYS` (không phải bề mặt nâng quyền;
`config.set` vẫn HUMAN_ONLY, agent không ghi được config.yaml).

## Cập nhật có lưới an toàn
1. `update.start`: sau mọi kiểm tra, TRƯỚC khi chép/chạy helper → `hub.backups.create(kind="pre-update")` (lỗi → huỷ cập nhật, thông báo cách tắt).
   `state.json.backup = {id, path, user_version}`.
2. Helper (chỉ stdlib, không import cfharness) sau `restarting` thêm pha `health`: `hub.ping` qua Unix socket (`--socket`, method công khai không cần
   token) tới khi trả lời, kiểm lại sau `STABLE_S` (3 s) — tổng ≤ `--health-timeout` (60 s). Windows: dựa vào mã thoát lệnh khởi động lại.
3. Không lên + có `--rollback-install` (= `uv tool install --force … @v<bản đang chạy>`): pha `rolling_back` — dừng daemon còn sót theo `hub.pid`
   (SIGTERM → 15 s → SIGKILL; không bao giờ chính mình/cha), nếu `user_version` hub.db > `--db-version` → lấy `daemon.lock` (flock như daemon) rồi
   thay hub.db bằng bản trong archive (kiểm `integrity_check` trước; DB đang có + `-wal/-shm` chuyển sang `backups/hub.db.replaced-<ts>`, không xoá)
   → cài lại tag cũ → `rollback_restarting` → kiểm lại. `state = failed`, `rolled_back = true`, `rollback{ok, db_restored, user_version_*, error}`.
   `daemon._UPDATE_BUSY_PHASES` có `rolling_back` → daemon do launchd bật lại trong lúc này chờ (không mở hub.db giữa chừng).
4. `cfharness update --rollback` → RPC `update.rollback` (HUMAN_ONLY, tự audit) → `Updater.rollback`: đích = `v<version_before>` trong state.json
   (tag `vX.Y.Z`, repo cố định), từ chối khi lần gần nhất đã là quay về / đã tự quay về / đang chạy đúng bản đó; helper `rollback` = chờ daemon thoát
   → khôi phục DB nếu schema đã tăng → cài tag cũ → khởi động lại → kiểm. Daemon không trả lời (bản mới hỏng) → CLI chạy `Updater.rollback` tại chỗ.

**Mất dữ liệu có chủ đích**: quay về DB = mất những gì ghi vào hub.db SAU lúc chụp pre-update (event/run/ledger của bản mới). DB đó được giữ ở
`backups/hub.db.replaced-*` để lấy lại tay. Khi schema KHÔNG tăng, hub.db giữ nguyên (chỉ cài lại mã).

## Giới hạn / việc dở
- Không mã hoá, không có tuỳ chọn gộp secret (xem trên). Không có `backup rm` (xoá tay file trong `backups/` hoặc chờ retention).
- `runs/` (stdout thô) và `checkpoints/` không được sao lưu → `run revert`/xem log thô của run cũ không còn sau khi khôi phục sang máy mới.
- Bản sao lưu nằm cùng đĩa với HUB_HOME — chép `backups/` ra nơi khác (Time Machine/ổ ngoài) là việc của người dùng.
- Khôi phục không tự xoá `agents/<id>/runtime|workspaces` của agent không còn trong bản sao lưu (mồ côi, vô hại).
- Windows: helper chưa dò sức khoẻ daemon (không có kênh stdlib đơn giản cho endpoint TCP) và không lấy `daemon.lock` khi thay hub.db.
