# Chuyển hub sang máy khác — xuất / nhập toàn bộ hub (`cfharness hub export|import`, web `/hub-transfer`)

Tài liệu tổng hợp PR1–PR6 của `docs/dev-notes/plan-hub-clone.md` (plan + quyết định của chủ repo + lệch plan từng PR ở mục "Tiến độ" của plan).
Phần 1 cho người dùng, phần 2 cho người sửa code.

## 1. Cho người dùng

### Hai bước
1. Ở máy cũ: **xuất** → ra MỘT file `.cfhx` (mặc định `HUB_HOME/exports/<máy>-<ngày>-<giờ>.cfhx`, quyền 0600).
2. Chép file sang máy mới bằng cách nào cũng được (USB, AirDrop, scp…) → **nhập**. Không có kênh truyền riêng.

| | Terminal | Web (`cfharness web --open` → Cài đặt → "Chuyển hub sang máy khác", hoặc `/hub-transfer`) |
|---|---|---|
| Xuất | `cfharness hub export [file] [--profile core\|full] [--for clone\|move] [--stop] [--no-secrets] [--with-totp] [--with-mcp-tokens]` — hỏi passphrase 2 lần | Form chọn gồm gì / mục đích / secret → bước 1 (cảnh báo nội dung gói) → bước 2 (gõ passphrase 2 ô; chuyển nhà: nhắc đóng băng) → chạy nền, trang tự cập nhật → nút **Tải về** (link một lần, 10 phút) |
| Nhập | `cfharness hub import <file> --dry-run` rồi bỏ `--dry-run` (`--map-path CŨ=MỚI`, `--mode clone\|move`, `--replace --stop`, `--skip-secrets`, `--clone-repos`) | Tải file lên (≤ `transfer.max_upload_mb`, mặc định 4096 MB) → trang **kế hoạch** đầy đủ (như `--dry-run`, sửa được chế độ/đường dẫn/thử passphrase) → **Nhập…** → bước 2 gõ lại id gói + passphrase → daemon dừng vài phút rồi tự chạy lại → báo cáo + kết quả `hub doctor` |
| Sau khi nhập | `cfharness hub doctor [--probe]` | Nút "Chạy kiểm tra" ở `/hub-transfer` (tự chạy sau khi nhập) |
| Mở lại máy cũ (chuyển nhà) | `cfharness hub unfreeze` | Nút **Mở lại hub này** ở `/hub-transfer` (xác nhận) |

Máy mới tinh: cài cfharness, `cfharness web --open` → trang chủ có thẻ **"Nhập hub từ máy khác"** (hub trống = chưa có agent, team, task,
run, lịch, secret nào). Hub trống thì không cần tick "thay thế"; hub đã có dữ liệu → bắt buộc tick "thay thế hub hiện có" (hiện trạng được
chụp thành bản sao lưu `pre-restore` trước, lỗi giữa chừng tự khôi phục). Gói lớn hơn trần tải lên → nhập bằng terminal.

### Hai chế độ
- **Nhân bản** (`clone`, mặc định — hai máy cùng chạy): ở máy mới Telegram/Slack TẮT (cần bot riêng: `cfharness notify telegram setup`),
  không nhập bot/OAuth MCP của máy cũ (đăng nhập lại), lịch tạm dừng, task đang dở bị chặn ("[hub] nhân bản…"), yêu cầu duyệt/câu hỏi
  đang chờ của máy cũ bị huỷ. Máy cũ chạy bình thường. Hai hub từ đó độc lập (không đồng bộ ngược).
- **Chuyển nhà** (`--for move` khi xuất + `--mode move` khi nhập): máy cũ **đóng băng** ngay khi gói qua kiểm tra (không run/lịch/
  Telegram/Slack — tránh hai daemon tranh một bot/OAuth), máy mới giữ nguyên kênh/lịch. Gỡ đóng băng chỉ người.

### Cái gì đi theo / không
- **Đi theo**: hub.db (agent, team, task, hội thoại điều phối viên, lịch, ledger, graph memory, audit, lịch sử run), cấu hình, chính sách,
  tri thức agent (MEMORY/USER/skill/SOUL), prompt đóng băng; profile `full` thêm stdout thô của run; **session Claude Code** của hub
  (`core`: phần đang dở; `full`: mọi session hub biết) để `--resume` chạy tiếp; **secret mã hoá** bằng passphrase (mặc định); tuỳ chọn TOTP web,
  token MCP HTTP; danh sách repo workspace (`repos.json`: remote đã bỏ thông tin đăng nhập, nhánh, HEAD, dirty).
- **Không bao giờ**: `checkpoints/` (`run revert` của run cũ không chạy ở máy mới), repo (clone lại — `--clone-repos` / ô tick trên web),
  thay đổi chưa commit, khoá ký phiên web `auth/web.json`, thiết bị tin cậy, token người `<socket>.auth`, `logs/`, `backups/`, session Claude
  Code bạn tự chạy ngoài hub, job launchd/LaunchDaemon (cài lại trên máy mới — doctor gợi ý).
- **Thư mục Không gian số** (filehub): cấu hình `filehub.*` đi theo (đường dẫn bản sao cục bộ đổi theo máy mới), token THIẾT BỊ thì
  không bao giờ. Sau khi nhập, đồng bộ ở máy mới **TẮT** — đăng nhập thiết bị mới: `cfharness filehub setup --name <tên>` (thêm
  `--binary …` nếu binary sync ở chỗ khác). Nhân bản: thư mục chung bị gỡ khỏi hub mới (setup cùng tên để workspace `filehub:<tên>` chạy lại);
  chuyển nhà: giữ thư mục chung, đăng nhập xong là đồng bộ tiếp. Máy cũ đóng băng vẫn đồng bộ tệp (`cfharness filehub pause` nếu thôi dùng).
- Gói ngoài phần secret **KHÔNG mã hoá**: chứa memory, transcript, nhật ký kiểm toán — giữ riêng tư.
- Quên passphrase = mất phần secret; phần còn lại vẫn nhập được (`--skip-secrets` / ô "bỏ qua secret"), rồi `cfharness secret set <TÊN>`.

### Khác hệ điều hành
macOS/Linux ↔ Windows, hai chiều: đường dẫn trong DB, cấu hình, chính sách, transcript đổi kiểu (`/Users/a/x` ↔ `C:\Users\a\x`); tên file
không tạo được ở máy đích → tri thức đổi tên an toàn, file khác → từ chối trước khi ghi; MCP stdio thiếu lệnh → đánh dấu lỗi, sửa bằng
`cfharness mcp edit`. **Chưa kiểm trên Windows thật** (xem mục 3).

## 2. Kỹ thuật

| Module | Vai trò |
|---|---|
| `hub_transfer.py` | Gói `.cfhx` (tar không nén ngoài, từng phần gzip riêng, `MANIFEST.json` cuối, sha256 nội dung gốc, `sections` có `prefix`/`optional`); `export_package`/`export_in_daemon`/`export_offline`, `seal_package`, `verify_package`, `import_plan` (dry-run, không ghi), `import_package` (chỉ ngoại tuyến, giữ `daemon.lock`, stage → pathmap → dọn trạng thái máy → áp chế độ → `_apply_stage` → session → secret → `rebaseline_offline`; lỗi → `_rollback_import`); RPC `hub.export` |
| `hub_transfer_cli.py` | `cfharness hub export\|import\|unfreeze\|doctor\|import-job` (`import-job` = nội bộ, do web chạy) |
| `pathmap.py` | Viết lại đường dẫn nhiều cặp một lượt, hai kiểu POSIX/Windows (dùng chung với `home migrate`) |
| `transfer_secrets.py` | `secrets.enc`: scrypt → AES-256-GCM, AAD = id gói + sha256 manifest; danh sách trắng bản rõ; phân loại theo chế độ |
| `transfer_sessions.py` | Phần `claude/`: chọn session theo profile, che secret theo giá trị, `encode_cwd` theo quy tắc Claude Code |
| `transfer_os.py` | Tên file/MCP/cảnh báo theo OS đích (tham số, test mô phỏng được) |
| `transfer_repos.py` | `repos.json`, `pause_missing`, `--clone-repos` |
| `hub_freeze.py` | Đóng băng chuyển nhà (`hub_state.migrated_to`), RPC `hub.frozen\|freeze\|unfreeze` |
| `import_followups.py` | Lần daemon lên đầu: viết lại đường dẫn trong tri thức qua `Ledger.apply` actor `import` (rollback được) |
| `hub_doctor.py` | Kiểm sau nhập, chỉ đọc; RPC `hub.doctor` |
| `hub_transfer_web.py` (PR6) | RPC cho web + tiến trình `hub import-job` + chờ/dọn lúc daemon khởi động (mục 2.1) |
| `web/transfer_pages.py` (PR6) | `/hub-transfer`, `/partials/hub-transfer[/doctor]`, tải lên/tải về, trang kế hoạch, ACTIONS `hub_export`/`hub_import`/`hub_unfreeze`; JS `static/transfer.js` |

### 2.1 Web (PR6) — thiết kế
- **RPC** (đều `auth.HUMAN_ONLY`, không ở `MANAGER_ALLOWED`): `hub.transfer_status` (đọc), `hub.export_start` (ghi, tự audit), `hub.export_status`,
  `hub.import_plan` (đọc), `hub.import_start` (ghi, tự audit), `hub.import_status`. `MUTATING_TRANSFER_WEB` ∈ `auth.all_mutating()`.
- **Passphrase trên web**: form đầu KHÔNG có ô passphrase. Nó chỉ được gõ ở BƯỚC 2 (`Action.reenter` → ô password trống trên trang "Xác nhận lần 2")
  và đi đúng một lần trong POST cuối → RPC. Không qua `carry()` (`Action.secret_fields`, áp cả trang bước 1 lẫn bước 2), không vào HTML/log.
  Lý do không làm "2 ô ở form đầu + nhập lại ở bước 2": hai lần gõ độc lập không so được với nhau (server không giữ lần đầu) — người gõ lệch
  ở bước 2 sẽ mã hoá bằng passphrase khác với cái họ tưởng; gõ 2 ô ngay ở POST cuối vừa xác nhận được, vừa để passphrase qua mạng/RAM web
  ít lần nhất. Daemon: `p.pop("passphrase")` TRƯỚC mọi việc + `wrap_with_audit` gỡ khoá `passphrase` (lưới PR2); passphrase sống trong hộp
  một phần tử của tác vụ, `clear()` ngay sau `seal_package` (chuỗi Python không xoá được khỏi bộ nhớ — giới hạn đã biết).
- **Xuất nền**: `ExportJobs` (`hub.transfer_web`) chạy ĐÚNG đường CLI (`export_in_daemon` → `seal_package` trong daemon → `freeze` với sha256 đã gắn;
  gắn hỏng + `move` → `unfreeze`). Trạng thái chỉ RAM (phase/progress/error/kết quả gọn; `written` = cỡ file `.part`).
- **Tải về**: web đọc file gói TRỰC TIẾP (gói hàng GB không qua JSON-RPC được; web cùng user OS với daemon, không phải agent — sandbox chặn
  agent đọc `exports/`). Id lấy từ `hub.export_status`, khớp `hub_transfer.ID_RE`, mở `O_NOFOLLOW` + kiểm file thường. Token `secrets.token_urlsafe(24)`
  trong RAM tiến trình web (`app.state.transfer_tokens`), gắn `Session.sid`, hết hạn 10 phút, tiêu khi dùng (phiên khác → 404 không tiêu).
- **Tải lên**: XHR thân thô (không multipart — `request.form()` sẽ đệm vào RAM/thư mục tạm hệ thống), CSRF + khoá phiên qua header, cùng kiểm
  nguồn với `post_guard` (`origin_guard` tách ra). `Content-Length` bắt buộc (411) và ≤ trần (413) TRƯỚC khi đọc thân; vượt khi stream → 413;
  thiếu byte/ngắt → 400; mọi lỗi xoá `.part`. File `imports/up-<16 hex>.cfhx` 0600 (thư mục 0700). Daemon khởi động xoá bản tải lên > 24 giờ, `.part` > 1 giờ.
  `transfer.max_upload_mb` KHÔNG có trong danh mục Cài đặt web (nâng bằng `cfharness config set`).
- **Nhập = tiến trình tách rời** (mẫu `updater`): `hub.import_start` kiểm nhanh TRONG daemon (id gõ lại khớp manifest, passphrase đúng bằng
  `check_passphrase`, hub trống hoặc `replace`, run đang chạy → từ chối trừ `stop`) — sai thì daemon vẫn chạy, chưa ghi gì → ghi `job.json`
  (không passphrase) + `pass` (0600, O_EXCL) + `state.json` → spawn `[sys.executable, -m, cfharness, hub, import-job, <id>]` (`close_fds`,
  env không `CFHARNESS_RUN_ID`/`XPC_SERVICE_NAME`; token người ở file socket `.auth`, không trong env) → daemon dừng sau 0,5 s (launchd: mã 75).
  Job: đọc + xoá `pass` NGAY → chờ `daemon.lock` → `import_package` (hàm của CLI, tự hoàn tác khi lỗi) → xoá gói tải lên khi thành công →
  khởi động lại daemon (`spawn_background`; launchd: `wait_supervised`) kể cả khi lỗi → `state.json` (danh sách trắng `STATE_KEYS`/`REPORT_KEYS`).
- **Daemon chờ job**: `daemon.run_foreground` gọi `hub_transfer_web.hold_while_importing` (sau `_hold_while_updating`): có job bận (phase
  starting/waiting_daemon/importing, pid job còn sống, < 2 giờ) → chờ, không lấy `daemon.lock` chen ngang (launchd KeepAlive chạy lại ngay).
- **Web khi daemon dừng**: như `update_pages` — web KHÔNG đọc `state.json` trực tiếp; partial bắt `DaemonUnavailable` → "daemon đang dừng để
  nhập…", poll 2 s; daemon lên → `hub.import_status` + `hub.doctor`. Phiên web sống sót (RAM tiến trình web; gói không chứa `auth/web.json`).
- `/hub-transfer` là trang 2D thường (chế độ 3D chuyển `/office` như mọi trang). Không script/style inline; `transfer.js` chỉ gán `textContent`.

### 2.1b filehub (thư mục chung Không gian số — tích hợp sau khi gộp `main`, 2026-11-15)
Kiểm trên code thật sau merge (`docs/dev-notes/filehub.md`):
- **Token thiết bị** `HUB_HOME/secrets/filehub/tokens.json` (thư mục CON của `secrets/`; binary sync tự ghi, `--device "cfharness · <máy>"`):
  `transfer_secrets.collect` chỉ lấy file thường tên `NAME_RE` ngay trong `secrets/` → thư mục con bị bỏ qua SẠCH (không lỗi), `missing_secrets`
  (`_secret_names`) cũng chỉ đếm tên `NAME_RE` → không báo nhầm thiếu secret. Đã đúng từ trước; nay thành quyết định có ghi + test (cả clone
  lẫn move): mang token sang = hai máy đồng bộ dưới CÙNG một thiết bị. Gói sửa tay chứa `secrets/filehub/…` → `allowed_payload_path` từ chối
  (`transfer_secrets.NEVER`). `filehub/` (profile/base/status) ∉ `backup.INCLUDE_DIRS` (+ ∈ `EXCLUDED`) → không vào gói; nhập chỉ đồng bộ tập
  INCLUDE (`backup._apply_stage`) → `filehub/` + `secrets/filehub/` của máy đích giữ nguyên (cả `--replace`, cả hoàn tác).
- **Cấu hình** (`filehub.*` ∈ `integrity.SECURITY_KEYS`, đi theo config.yaml): cơ chế bật thật = `filehub.enabled` — `FilehubService.start()`
  (ở `hub.started`) thoát ngay khi tắt, `configured()` cần enabled + server + binary + folders + không `filehub.paused`. Trước khi sửa, gói nhập
  vào để nguyên `enabled=true` + folders + `device_name` của máy cũ → daemon đích spawn binary (không token → `auth_expired`, hoặc binary đường dẫn
  cũ → lỗi) và `filehub login` sẽ đăng ký thiết bị mang TÊN máy cũ. Nay `_adjust_filehub` (mọi chế độ, trên stage, sau pathmap): `enabled=false`,
  `device_name=null`; `clone` gỡ `folders` (hai hub độc lập — không tự đồng bộ chung một root; người muốn dùng chung thì tự chọn root đó lúc đồng ý
  thiết bị — thiết bị KHÁC), `move` giữ `folders` (local_path + `local_parent` + `sync_binary` đã qua pathmap như mọi file cấu hình) → `filehub setup`
  (= bật + `login`; có folders thì không auto_add, `_finish_login` → `_apply_change` chạy tiếp đúng root). `server` giữ (không phải thiết bị).
  `hub_state.import_filehub {mode, server, binary, folders, folders_kept, agents, command, needs_login}` (∈ `MACHINE_STATE`) → báo cáo nhập,
  `--dry-run` (cảnh báo "Thư mục Không gian số cần đăng nhập lại: `cfharness filehub setup …`"), web (`REPORT_KEYS` + `_brief_report` lọc khoá),
  doctor; xoá khi `_finish_login` thành công; `filehub.status.relogin_after_import`. `filehub.paused` ∈ `MACHINE_STATE`.
- **Xuất**: bản sao cục bộ thư mục chung không phải repo → loại khỏi `repos.json` (`_filehub_roots` → `TR.collect` hub_roots), tránh gợi ý clone/
  "thiếu workspace" cho cwd run dở trong `~/KhongGianSo/<tên>`. Agent `workspace_default: filehub:<tên>` không bị `pause_missing` (không phải đường
  dẫn) — báo cáo liệt kê (`agents`); run của chúng bị từ chối rõ ("filehub chưa bật") tới khi setup lại cùng tên.
- **Doctor** (`_filehub`): `import_filehub` còn → warning + lệnh setup; bật mà chưa có `secrets/filehub/tokens.json` → warning `filehub login`;
  `filehub.sync_binary` không có/không chạy được → warning `--binary`. Sửa kèm: kiểm quyền `secrets/` báo nhầm thư mục con `filehub/` (0700) là
  "rộng hơn 0600" — thư mục con nay kiểm 0700 + file bên trong 0600.
- **Đóng băng move**: filehub VẪN chạy (như sao lưu) — đồng bộ tệp của người, không tạo run/tốn tiền, không chung bot/OAuth với máy mới (máy mới
  là thiết bị khác); event `filehub.*` không ra kênh (QueuedNotifier bỏ khi đóng băng), steer/presence không xảy ra (không có run). Ghi ở `hub_freeze`.
- **Sandbox/classifier**: sau merge `HUB_WRITE_DENY_NAMES`, denyRead sandbox, `secret_read_deny_rules`, `sensitive_read` có đủ `filehub` +
  `exports` + `imports` — test `test_hub_transfer.py::…` lặp cả ba. Main không thêm migration/thư mục cấp 1 nào khác; khoá `hub_state` mới duy nhất
  `filehub.paused` (đã xử lý); `recovery` giữ `pending_resume` khi chưa có loop (không ảnh hưởng nhập: move ghi `pending_resume` trên stage).
- Test: `tests/test_hub_transfer_filehub.py`.

### 2.2 Bất biến (toàn tính năng)
- Xuất/nhập chỉ người: RPC ∈ `HUMAN_ONLY` (không `MANAGER_ALLOWED`), `HUB_HUMAN_ONLY_CLI["hub"] = None` (mọi lệnh con, kể cả `import-job`), từ chối khi có
  `CFHARNESS_RUN_ID`; sandbox denyRead/denyWrite `exports/` + `imports/`; `exports/`/`imports/` ∈ `backup.EXCLUDED`.
- Passphrase không bao giờ vào argv, env, RPC audit, log, DB, `job.json`, `state.json`, HTML; file `pass` 0600 sống tới khi job đọc (hoặc daemon dọn).
- Nhập không bao giờ ghi đè hub có dữ liệu mà không có `pre-restore`; lỗi giữa chừng tự hoàn tác; không bao giờ ghi vào repo/thư mục đã có.
- Gói không bao giờ chứa `checkpoints/`, `auth/web.json`/thiết bị, `secrets/` dạng rõ, session Claude không thuộc hub, token thiết bị/profile
  Không gian số (`secrets/filehub/`, `filehub/`); nhập không bao giờ để filehub bật (người `cfharness filehub setup` ở máy mới).
- Tri thức bị viết lại đường dẫn đi qua ledger (actor `import`, rollback được, không bị tự rollback).
- `move`: nguồn đóng băng tới khi người gỡ.

### 2.3 Quy tắc khi sửa
- Trạng thái gắn với máy mới trong `hub_state` → `hub_transfer.MACHINE_STATE` (hoặc `CLONE_STATE`) + ca test.
- Loại secret mới → phân loại ở `transfer_secrets` + ca trong `tests/test_hub_transfer_secrets.py`.
- Vòng nền ghi mới → kiểm `runs.export_quiesced(hub)` đầu lượt; đường tạo run/kênh gửi ra mới → qua `RunManager.submit`/`token()`/`QueuedNotifier`.
- Thêm RPC web → `HUMAN_ONLY_TRANSFER_WEB` (+ `MUTATING_TRANSFER_WEB` nếu ghi) + ca trong `tests/test_web_hub_transfer.py` và `tests/test_sec_auth.py`.
- Khoá mới trong `state.json`/kết quả xuất → thêm vào `STATE_KEYS`/`REPORT_KEYS`/`EXPORT_KEYS` (danh sách trắng, không chữ tự do của agent).
- Pha mới của job mà daemon phải chờ → `IMPORT_BUSY_PHASES`.

### 2.4 Test
`tests/test_hub_transfer.py` (PR1), `test_hub_transfer_secrets.py` (PR2), `test_hub_transfer_sessions.py` (PR3), `test_hub_transfer_cross_os.py` (PR4),
`test_hub_transfer_move.py` (PR5), `test_hub_transfer_filehub.py` (filehub), `test_web_hub_transfer.py` (PR6: CSRF/khoá/Origin → 403, chưa đăng nhập → /login|401, 413/411 không để file dở,
xuất 2 bước + passphrase không lọt HTML/log/audit/DB, link tải dùng lại/hết hạn/phiên khác → 404, nhập end-to-end: kế hoạch → bước 2 → spawn
bị thay → daemon dừng → `run_import_job` thật → daemon mới → phiên web cũ thấy báo cáo; job chờ lock, lỗi vẫn khởi động lại; daemon chờ job;
dọn `pass`/job chết/gói quá hạn), `test_sec_auth.py::test_hub_transfer_web_rpcs_human_only` (agent đọc trộm token người vẫn bị từ chối).

## 3. Giới hạn / việc dở
- **Windows thật chưa kiểm**: thư mục session `C:\…` → `C--…` (suy từ mã Claude Code), `claude --resume` sau khi nhập, NTFS, `shutil.which` + PATHEXT,
  UNC, MAX_PATH; job `import-job` + `spawn_background` trên Windows chưa chạy thật (dùng `oscompat.detached_kwargs` như updater).
- **`claude --resume` thật — ĐÃ nghiệm thu trên macOS (2026-11-15, claude 2.1.325)**: session tạo ở thư mục A, đưa qua `transfer_sessions.install` sang thư mục B (map A→B, xoá file gốc), `claude -p --resume <sid>` chạy ở B nhớ đúng nội dung lượt trước. Windows thật vẫn chưa kiểm (mục trên).
- Web không có tiến độ % thật khi đóng gói (chỉ pha + số byte đã ghi) và khi nhập (pha); trang nhập chỉ đọc trạng thái qua RPC — daemon không lên
  lại được thì trang chỉ báo "đang dừng" (xem `HUB_HOME/imports/<id>/state.json`, `logs/import-job.out`, rồi `cfharness daemon start`).
- Web không xoá gói đã xuất trong `exports/` (CLI cũng không) — người tự xoá sau khi đã chép sang máy mới.
- Không có `--include-dirty-patch`; `--clone-repos` dùng remote đã che (repo https riêng tư cần đăng nhập git trước).
- Doctor chưa kiểm `hubs.json`, `home status`, `web totp enroll`, Telegram/Slack `test` thật.
- Chưa có ảnh chụp web trong `docs/screenshots/web/`.
