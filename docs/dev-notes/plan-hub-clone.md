# Plan — Xuất / nhập toàn bộ hub sang máy khác (`cfharness hub export|import`)

Trạng thái: **XONG — PR1–PR6** (tài liệu tổng hợp: `docs/dev-notes/hub-clone.md`) (xem "Tiến độ" cuối file; plan 2026-11-15; quyết định của chủ repo đã chốt ở mục 10). Mục tiêu: đóng gói MỌI thứ của một hub
cfharness thành MỘT file, mang sang máy khác (macOS ↔ Linux ↔ Windows), nhập vào và chạy tiếp — agent, team, task, hội thoại điều
phối viên, tri thức/học, lịch, quyền đã cấp, kết nối MCP/Telegram/Slack, tài khoản Claude, lịch sử run/audit.
Trải nghiệm người dùng chỉ có 2 bước: `cfharness hub export` ra một file → chép file sang máy kia bằng cách nào cũng được →
`cfharness hub import <file>`. Không có kênh truyền riêng (SSH…).

## 1. Hiện có gì và còn thiếu gì

| Đã có | Dùng lại được | Thiếu cho "clone" |
|---|---|---|
| `backup.py` — tar.gz + manifest sha256, chụp `hub.db` bằng online backup API, `verify`, `restore` ngoại tuyến giữ `daemon.lock`, `rebaseline_offline` | Toàn bộ khung archive/verify/apply/rebaseline | Cố ý BỎ `secrets/`, `auth/`, `mcp_server/`, `runs/`, `checkpoints/`; chỉ khôi phục **cùng đường dẫn HUB_HOME** |
| `home_migrate.py` — viết lại đường dẫn tuyệt đối trong DB (bảng không append-only) + file cấu hình, cập nhật `integrity_baseline` | `rewrite_text`, `_db_rewrite`, `REWRITE_FILES` | Chỉ đổi HUB_HOME; không đổi `$HOME`/thư mục repo; không đụng session Claude Code |
| `agents.export_agent/import_agent` | Mẫu "scrub secret theo giá trị" | Chỉ một agent, không có team/task/ledger/lịch sử |

Đo trên hub thật (máy này, 2026-11-15): `hub.db` 460 MB (+WAL), `checkpoints/` 921 MB, `agents/` 221 MB, `runs/` 127 MB,
`ledger/` 16 MB, `prompts/` 2.7 MB, 20 secret, 1.678 session; `~/.claude/projects` 2.9 GB (lẫn session KHÔNG thuộc hub).

### Những thứ gắn với MÁY (lý do không thể chỉ `tar` HUB_HOME)
1. **Đường dẫn tuyệt đối** khắp nơi: `agents.home_path`, `runs.cwd`, `workspace_default` (`/Users/phuonglq/Workspace/toa-soan-ia`),
   `mcp_servers.config` (command stdio), `skills.path`, `hub_state`, config.yaml, policy, checkpoint.json. Máy mới khác username/thư mục → hỏng.
2. **Session Claude Code** (`--resume`) nằm NGOÀI hub: `~/.claude/projects/<cwd mã hoá>/<session>.jsonl`. Tên thư mục mã hoá từ cwd →
   đổi đường dẫn là đổi tên thư mục + `cwd` bên trong jsonl. Thiếu file này: hội thoại điều phối viên / task retry `--resume` gãy.
3. **Secret** (`secrets/` 0600): token MCP, OAuth MCP (refresh token XOAY VÒNG), Telegram/Slack bot + callback key, `CLAUDE_ACCT_*`,
   `CLAUDE_CODE_OAUTH_TOKEN`. Không có stdlib để mã hoá.
4. **Danh tính kênh**: Telegram long-poll (2 máy cùng token → `409 Conflict`), Slack Socket Mode (2 kết nối → sự kiện chia đôi ngẫu nhiên),
   OAuth refresh xoay vòng (máy nào refresh trước thì máy kia chết token), lịch chạy 2 lần, ngân sách tính 2 nơi.
5. **Trạng thái tạm của máy** trong `hub_state`: `repo_locks`, `browser_locks`, `pending_resume`, `rate_limit*`, `pyenv_baseline`,
   `cli_compat`, `auth_block`, `admin_watch.active`, `systems.registry|paused` (job launchd của máy cũ), `telegram.offset`, `slack.seen`…
6. **Ngoài HUB_HOME**: plist LaunchDaemon (`admin install-daemon`), registry `~/.config/cfharness/hubs.json`, `/Users/Shared/cfharness`
   (admin), repo thật mà agent `dir:` làm việc, binary MCP stdio, `claude` CLI + đăng nhập.
7. **Toàn vẹn**: `integrity_baseline` + phát hiện ghi ngoài vào `hub.db` — nhập không qua daemon thì phải rebaseline như `restore`.

## 2. Hai chế độ — mặc định `clone` (2 máy cùng chạy)

Chủ repo chốt: trường hợp chính là **hai máy cùng chạy**. Vì vậy mặc định khi nhập là `clone` (an toàn khi máy nguồn vẫn sống);
`move` là tuỳ chọn cho người thực sự bỏ máy cũ và muốn giữ nguyên bot/OAuth không phải cài lại.

| | `move` — **chuyển nhà** (`--mode move`) | `clone` — **nhân bản**, 2 hub cùng chạy (MẶC ĐỊNH) |
|---|---|---|
| Máy nguồn sau khi xuất | Bị **đóng băng**: daemon từ chối submit run/tick/lịch/kênh, banner "đã chuyển sang <máy> lúc …"; `hub unfreeze` để gỡ | Chạy bình thường |
| Telegram/Slack | Giữ token + allowlist + map hội thoại → người dùng không phải ghép nối lại | **Tắt** ở máy đích (giữ cấu hình, không giữ token) — người tự `notify … setup` bot KHÁC |
| MCP OAuth | Chuyển refresh token | Không chuyển — `mcp.oauth_start` lại (tránh xoay vòng làm chết bên kia) |
| Lịch (`schedules`) | Giữ nguyên, bật | Nhập ở trạng thái **pause** hết, người bật lại cái cần |
| Điều phối viên, task đang mở | Giữ, chạy tiếp | Giữ dữ liệu; task `running/queued` → `blocked` kèm bình luận "[hub] nhân bản — chưa chạy lại" |
| `hub_id` / admin export | Theo home mới (tự tính) | Như trên + `cloned_from_hub` trong manifest |
| Tài khoản Claude | Chuyển | Chuyển (cảnh báo: 2 hub dùng chung hạn mức) |

Lý do có `move` đóng băng nguồn: không có cách nào để hai daemon cùng giữ một bot Telegram / một OAuth refresh token mà không hỏng một bên.

Hệ quả của `clone` (ghi rõ trong báo cáo nhập + `hub doctor`):
- Hai hub từ đó là **hai hub độc lập**: dữ liệu không đồng bộ ngược, tri thức học ở máy này không sang máy kia (muốn mang lại thì
  export/import tiếp, hoặc `agent export` từng agent).
- Mỗi hub cần **bot Telegram/Slack riêng** (`notify telegram|slack setup` trên máy mới) và đăng nhập lại MCP OAuth.
- Token MCP thường (header `Authorization` tĩnh) và tài khoản Claude ĐƯỢC chuyển — dùng chung được, nhưng hạn mức/rate limit tính chung.
- Lịch nhập ở trạng thái pause → người bật những lịch muốn chạy ở máy mới (tránh việc định kỳ chạy 2 lần).
- Gói đã nhập theo `clone` có thể nhập tiếp vào máy thứ 3 (không giới hạn số bản).

## 3. Định dạng gói xuất

Một file `<tên>.cfhx` = tar (không nén ngoài; từng phần nén riêng để mở được phần lẻ), quyền 0600, gồm:

```
MANIFEST.json          # cuối tar (như backup): format=cfharness-export, schema=1, id, created_at, mode_hint,
                       # source{home, user_home, os, arch, cfharness_version, claude_version, user_version, hub_id},
                       # profile, path_roots{hub_home, user_home, workspaces[]}, sections{}, files[{path,size,sha256}]
hub.db                 # online backup API (như backup.snapshot_db)
home/…                 # tập INCLUDE của backup.py
extra/runs/…           # profile full
claude/<session>.jsonl + claude/index.json # session Claude Code của hub (chỉ session_id có trong bảng sessions/runs)
secrets.enc            # tuỳ chọn: secrets/ + auth/totp*.json + mcp_server/tokens.json, mã hoá (mục 5)
repos.json             # danh sách repo workspace: đường dẫn, git remote, branch, HEAD, có thay đổi chưa commit không
```

Profile (`--profile`):
- `core` — như backup + `prompts/` + session Claude Code của **hội thoại điều phối viên + task chưa xong + run `pending_resume`**. Đủ để chạy tiếp.
- `full` — `core` + `runs/` (stdout thô) + mọi session Claude của hub.
- Luôn bỏ: **`checkpoints/`** (chủ repo chốt: không cần — checkpoint chỉ phục vụ `run revert`/`run diff` trên repo ĐÚNG trạng thái lúc
  run kết thúc, sang máy khác repo được clone lại nên không hoàn tác được nữa; tự hết hạn sau `retention.checkpoints_days`=30),
  `logs/`, `evals/`, `models/` (tải lại), `update/`, `backups/`, `worktrees/` (tái tạo từ repo), socket/pid/lock, `cli_history`, `trash/tamper`.
  Ở máy đích, run cũ: `run revert` báo "checkpoint không có (run nhập từ máy khác)"; `run diff` lấy cột `runs.diff` nếu có.
  Ở máy đích, `checkpoint_revertible` (tự cho phép theo checkpoint) chỉ áp cho run MỚI — run nhập không có checkpoint → không tự cho phép.

Ước lượng máy này: core ≈ 150–200 MB nén, full ≈ 250–300 MB nén.

## 4. Luồng XUẤT — `cfharness hub export [<file>] [--profile core|full] [--no-secrets] [--for move] [--stop]`

Mặc định gọn nhất: `cfharness hub export` → hỏi passphrase → ghi `HUB_HOME/exports/<tên-máy>-<ngày>.cfhx` (profile `full`, CÓ secret
đã mã hoá) và in đường dẫn + cỡ + câu "chép file này sang máy kia rồi chạy `cfharness hub import <file>`". Web Cài đặt có nút
"Xuất toàn bộ hub" và cả luồng nhập (mục 7b).

1. **Chặn**: chỉ người (`hub.export_*` ∈ HUMAN_ONLY, `hub` export/import ∈ `HUB_HUMAN_ONLY_CLI`), từ chối khi có `CFHARNESS_RUN_ID`;
   sandbox denyRead/denyWrite thư mục xuất mặc định `HUB_HOME/exports/` (giống `backups/`).
2. **Làm lặng hub** (`quiesce`, RPC `hub.export_prepare`): dừng dispatcher/lịch/tick điều phối viên/vòng học như ESTOP nhưng KHÔNG ghi
   cờ estop vào dữ liệu xuất; có run `running` → liệt kê và yêu cầu `--stop` (dừng mềm → resume được ở máy mới, đánh dấu `pending_resume`)
   hoặc chờ. Approval/câu hỏi đang mở: giữ nguyên (máy mới hỏi tiếp — bất biến "không tự từ chối do hết giờ").
3. **Chụp**: `hub.db` online backup → file tạm; quét tập file theo profile (dùng `backup.iter_included` + phần thêm); session Claude:
   lấy `session_id` từ `sessions`/`runs`/`manager_conversations`, tìm `~/.claude/projects/*/<sid>.jsonl` (+ thư mục `<sid>/` subagent nếu có),
   ghi `claude/index.json {sid → cwd gốc}`.
4. **repos.json**: với mỗi `workspace_default`/`workspace dir:` + `cwd` của task mở: `git remote get-url`, branch, HEAD, `dirty`. Repo
   KHÔNG được đóng gói (là dữ liệu của người, có thể rất lớn) — chỉ ghi để máy mới biết phải clone gì. Có cờ `--include-dirty-patch`
   (ghi `git diff` của repo dirty) — mặc định tắt.
5. **Secret** (mặc định CÓ): hỏi passphrase 2 lần (TTY; không TTY: `--passphrase-file`), mã hoá (mục 5). `--no-secrets` → manifest
   ghi danh sách TÊN secret thiếu để máy mới nhắc đặt lại.
6. **Ghi gói** nguyên tử (`.part` → rename), sha256, `verify` ngay sau khi ghi (dùng lại `backup.verify` mở rộng).
7. `--for move`: sau khi verify OK → ghi `hub_state.migrated_to {at, export_id, sha256}` → daemon nguồn chuyển sang **đóng băng** (mục 2).
   `--for clone` hoặc không ghi: chỉ thả `quiesce`.
8. Audit `hub.exported {id, profile, size, secrets: bool, mode}` (metadata, không đường dẫn tuyệt đối của người khác).

## 5. Mã hoá secret

Chốt: thêm `cryptography` làm dependency TRỰC TIẾP (hiện chỉ là bắc cầu — lý do `backup.md` chọn bỏ secret; có wheel cho macOS,
Linux, Windows).
`secrets.enc` = scrypt(passphrase, salt 16B, n=2^17, r=8, p=1) → khoá 32B → AES-256-GCM, AAD = id gói + sha256 phần còn lại của manifest
(gói bị tráo phần khác → giải mã thất bại). Nội dung = tar nhỏ: `secrets/*`, `auth/totp*.json` (tuỳ chọn `--with-totp`),
`mcp_server/tokens.json` (tuỳ chọn). KHÔNG BAO GIỜ chuyển: `auth/web.json` (khoá ký phiên), `trusted_devices.json`, `hub.sock.auth`.
Passphrase không vào argv/log/DB; `--passphrase-file` phải 0600.

Quên passphrase = mất phần secret (phần còn lại vẫn nhập được với `--skip-secrets`, rồi đặt lại secret tay).

## 5b. Khác hệ điều hành (macOS ↔ Linux ↔ Windows) — hỗ trợ cả hai chiều

Bộ viết lại đường dẫn hiện tại (`home_migrate`) chỉ biết dạng đường dẫn của OS ĐANG chạy. Nhập khác OS cần `pathmap.py` biết cả hai phía:
- Manifest ghi `source.os` + `source.sep`; mỗi cặp `cũ→mới` được dịch theo cả hai phía: POSIX `/Users/a/x` ↔ Windows `C:\Users\a\x`
  (phần đuôi đổi `/`↔`\`), kèm biến thể JSON-escape (`\\`) và dạng `C:/Users/…` — áp cho mọi OS, không phụ thuộc `IS_WINDOWS` như hiện nay.
- Đường dẫn TƯƠNG ĐỐI trong tar luôn `/` (đã đúng như `backup`/`agent export`); giải nén trên Windows tự đổi.
- Tên file hợp lệ trên POSIX nhưng không hợp lệ trên Windows (`:`, `?`, `*`, tên `CON`/`NUL`…, phân biệt hoa/thường trùng nhau): kiểm ở
  `--dry-run`, báo danh sách; skill/memory có tên như vậy → đổi tên qua ledger (rollback được) và ghi vào báo cáo.
- Session Claude Code: thư mục `~/.claude/projects` trên Windows là `%USERPROFILE%\.claude\projects`; tên thư mục mã hoá cwd = thay mọi
  ký tự không phải chữ/số bằng `-` (quan sát: `/Users/phuonglq/.cfharness/…` → `-Users-phuonglq--cfharness-…`; Windows dự kiến
  `C:\Users\x\…` → `C--Users-x-…`) — xác nhận bằng spike `claude` thật trên Windows + fixture contract test trước khi bật.
- Lệnh MCP stdio: `command` kiểu `/opt/homebrew/bin/node` không có trên Windows → dry-run liệt kê, server đó nhập ở trạng thái tắt + gợi ý
  `cfharness mcp edit`; lệnh trần (`npx`, `uvx`, `node`) giữ nguyên.
- Quyền file: Windows không có 0600 → dùng cơ chế sẵn có của `oscompat` (ACL chỉ chủ) cho `secrets/`, `auth/`, gói `.cfhx`.
- Thứ chỉ có trên một OS: LaunchDaemon (macOS) / dịch vụ Windows / systemd không chuyển — `hub doctor` gợi ý lệnh cài autostart đúng OS;
  `systems.registry` (job launchd) luôn bị xoá khi nhập khác máy; cấu hình sandbox/confine theo đường dẫn được viết lại, nhánh nào
  không áp được trên OS đích (vd. sandbox macOS) → báo trong dry-run.
- Test: chạy cùng một bộ test nhập với gói tạo giả từ "máy POSIX" và "máy Windows" (manifest + đường dẫn giả), trên CI cả hai OS.

## 6. Luồng NHẬP — `cfharness hub import <file> [--home <đích>] [--map-path CŨ=MỚI]… [--mode clone|move] [--dry-run] [--replace --stop]`

Mặc định gọn nhất: `cfharness hub import <file>` → in tóm tắt kế hoạch (như `--dry-run`) → hỏi y/N → hỏi passphrase → nhập → in báo
cáo + chạy `hub doctor`. Máy đích chưa từng cài/chạy hub là trường hợp chính (home trống).

CHỈ ngoại tuyến, như `backup restore`: daemon đích không chạy (giữ `daemon.lock` suốt quá trình), không có RPC nhập.

1. **Kiểm gói**: sha256 từng phần, tên phần tử an toàn (không tuyệt đối/`..`/symlink), `MANIFEST` hợp lệ, `user_version` ≤ mã đích
   (mới hơn → từ chối: "cập nhật cfharness trên máy này lên ≥ vX.Y.Z trước"), cũ hơn → cho, daemon tự migrate khi lên.
2. **Đích**: HUB_HOME đích phải TRỐNG (hoặc chỉ có thứ `cfharness` vừa tạo). Không trống → cần `--replace --stop` → chụp `pre-restore`
   như `backup restore` rồi mới ghi.
3. **Bản đồ đường dẫn** (tự tính + `--map-path` ghi đè):
   - `source.hub_home` → HUB_HOME đích; `source.user_home` → `$HOME` đích;
   - mỗi repo trong `repos.json` → đường dẫn đích (mặc định thay tiền tố `$HOME`; không tồn tại → báo, xem bước 7).
   Áp bằng bộ viết lại của `home_migrate` (tách phần lõi `rewrite_text`/`_db_rewrite`/file rewrite thành hàm dùng chung nhận
   NHIỀU cặp `cũ→mới`, sắp theo độ dài giảm dần). Bảng append-only + `HISTORY_COLS` giữ chữ cũ (là lịch sử).
   File tri thức (MEMORY/SKILL/config agent — thuộc ledger) có đường dẫn cũ: **viết lại qua `Ledger.apply` actor `import`**
   (rollback được), không ghi file trực tiếp — sau khi daemon lên lần đầu (hàng đợi `hub_state.import_followups`).
4. **Session Claude Code**: mỗi `claude/<sid>.jsonl` → cwd mới = map(cwd gốc) → thư mục `~/.claude/projects/<mã hoá(cwd mới)>/`;
   viết lại trường `cwd` (và đường dẫn tuyệt đối trong `tool_use.input` theo cùng bản đồ — chỉ chuỗi khớp tiền tố); không ghi đè file
   đã có (sid trùng → bỏ + cảnh báo). Hàm mã hoá cwd lấy từ quan sát spike (`/` và `.` → `-`); thêm contract test với fixture thật.
5. **Làm sạch trạng thái máy** trong `hub_state` (danh sách `MACHINE_STATE` khai báo rõ, có test): xoá `repo_locks`, `browser_locks`,
   `rate_limit*`, `pyenv_baseline`, `cli_compat`, `cli_unknown_rate`, `auth_block`, `admin_watch.active`, `systems.registry|paused`
   (job launchd máy cũ — liệt kê lại trong báo cáo để `system scan` trên máy mới); giữ `pending_resume` (run bị dừng lúc xuất resume tiếp).
   Run `running|queued` trong DB → `interrupted` (recovery sẵn có lo phần còn lại).
6. **Áp chế độ**: `clone` → tắt Telegram/Slack (`notify.*.enabled=false`), pause mọi lịch, task đang chạy → `blocked`, bỏ secret OAuth MCP
   + bot. `move` → giữ hết.
7. **Repo**: repo đích không tồn tại → báo cáo kèm lệnh gợi ý `git clone <remote> <đích> && git checkout <branch>`; tuỳ chọn
   `--clone-repos` tự chạy `git clone` (không bao giờ ghi đè thư mục đã có). Agent có workspace thiếu → `paused` + lý do cho tới khi repo có.
8. **Secret**: `secrets.enc` có mặt → hỏi passphrase → giải mã → ghi `secrets/` 0600 (theo chế độ, mục 2).
9. **Ghi** theo thứ tự an toàn: giải nén vào `HUB_HOME/.import-<id>/` (stage) → viết lại đường dẫn trên STAGE (DB + file) → kiểm
   `PRAGMA integrity_check` → chuyển vào chỗ (`backup._apply_stage` mở rộng) → session Claude → `rebaseline_offline` → ghi
   `hub_state.imported_from {export_id, source_hub_id, at, mode}` → audit `hub.imported` (ghi khi daemon lên lần đầu, từ hàng đợi).
10. **`--dry-run`**: in toàn bộ kế hoạch không ghi gì: bản đồ đường dẫn + số hàng/file sẽ viết lại, repo thiếu, MCP stdio có `command`
    không tìm thấy trên PATH, secret thiếu, phiên bản `claude` đích so với `tested_versions`, dung lượng cần.

## 7. Sau khi nhập — `cfharness hub doctor`
Chạy được nhiều lần, chỉ đọc + gợi ý lệnh: `claude` có + đã đăng nhập (hoặc secret `CLAUDE_CODE_OAUTH_TOKEN`), `secret.resolve` mọi
`${NAME}`, `mcp probe` từng server, Telegram/Slack `test`, repo workspace tồn tại + đúng HEAD, lịch nào đang pause, LaunchDaemon
(`admin install-daemon` nếu máy cũ có), đăng ký `hubs.json`, `web totp enroll` nếu không chuyển TOTP, model vector (`cfharness[vector]`),
`home status`. Web: kết quả doctor + `imported_from` hiện ở `/hub-transfer` (mục 7b).

## 7b. Web — làm được CẢ xuất LẪN nhập trên trình duyệt (chủ repo yêu cầu 2026-11-15)

Trang `/hub-transfer` "Chuyển hub sang máy khác" (link từ thẻ ở Cài đặt; client thuần qua RPC, thao tác qua `ACTIONS` + `post_guard`).
- **Xuất**: form chọn profile core/full, có mang secret không, passphrase (2 ô) → `ACTIONS` `hub_export` (xác nhận 2 bước, cảnh báo gói
  chứa memory/transcript/audit) → RPC `hub.export_start` (HUMAN_ONLY, tự audit kênh `web`) chạy NỀN trong daemon (quiesce → ghi gói),
  tiến độ poll htmx `/partials/hub-transfer` (RPC `hub.export_status`) → xong: nút "Tải về" = link MỘT LẦN (token ngẫu nhiên trong RAM,
  hết hạn 10 phút, gắn phiên; stream file `Content-Disposition: attachment`, không bao giờ qua static). Passphrase chỉ đi trong body POST
  tới RPC, không vào log/audit/DB/state file; trang xác nhận không mang passphrase qua `carry()` — form nhập lại passphrase ở bước 2.
- **Nhập**: form tải file lên (`POST /hub-transfer/upload`, cùng CSRF + khoá phiên; stream xuống `HUB_HOME/imports/<id>.cfhx` 0600,
  trần cỡ `transfer.max_upload_mb`, kiểm `Content-Length` trước, không đọc cả file vào RAM) → RPC `hub.import_plan` (HUMAN_ONLY, chỉ đọc:
  verify + dry-run, nhận passphrase để thử giải mã) → trang hiện TOÀN BỘ kế hoạch như CLI `--dry-run` (bản đồ đường dẫn có ô sửa
  `--map-path`, repo thiếu, MCP thiếu lệnh, secret, chế độ clone|move, cảnh báo hub đích không trống → bắt buộc tick "thay thế, đã có bản
  pre-restore") → `ACTIONS` `hub_import` (bước 2 bắt buộc, gõ lại id gói) → RPC `hub.import_start`.
- **Thực thi nhập khi daemon phải dừng** (theo mẫu `updater`): daemon ghi `imports/<id>/job.json` (tham số, KHÔNG có passphrase) +
  `imports/<id>/pass` (0600, xoá ngay khi đọc) → spawn tách rời `python -m cfharness hub import-job <id>` (dùng mã đang cài, không
  phải helper stdlib vì không cài đè venv) → daemon tự dừng → job lấy `daemon.lock`, chạy đúng hàm nhập của CLI, ghi
  `imports/<id>/state.json` (phase/progress/error, không chữ tự do của người khác) → khởi động lại daemon → web đang poll thấy daemon lên
  lại, hiện báo cáo + kết quả `hub doctor`. Lỗi giữa chừng → khôi phục bản pre-restore (như `update` rollback), state = failed.
  Daemon đang có run → từ chối trừ khi tick "dừng run đang chạy".
- **Máy mới tinh**: `cfharness web --open` trên hub vừa cài (home trống) → trang chủ hiện thẻ "Nhập hub từ máy khác" nổi bật.
- Phiên web sống sót qua nhập: `auth/` của máy đích KHÔNG bị gói ghi đè (gói không bao giờ chứa `auth/web.json`, devices).
- `/hub-transfer` và `/partials/hub-transfer` là trang 2D thường (chế độ 3D chuyển hướng `/office` như mọi trang).
- Bất biến web: chỉ người (HUMAN_ONLY + `post_guard`), manager không có trong `MANAGER_ALLOWED`, upload/download chỉ loopback/public
  theo `web/security` sẵn có; sandbox denyRead/denyWrite `HUB_HOME/imports` + `exports`. Test: `tests/test_web_hub_transfer.py`
  (CSRF thiếu → 403, upload quá cỡ → 413, link tải dùng lại → 404, passphrase không xuất hiện ở log/audit/state).

## 8. Bất biến an toàn (thêm vào AGENTS.md khi làm)
- Xuất/nhập chỉ người; agent không đọc được gói (`exports/` deny + classifier `sensitive_read`), không gọi được lệnh (`HUB_HUMAN_ONLY_CLI`).
- Gói không mã hoá phần ngoài `secrets.enc` → chứa memory, transcript, audit: 0600, cảnh báo rõ khi xuất, không bao giờ tự đẩy lên mạng.
- Nhập không bao giờ ghi đè hub có dữ liệu mà không có `pre-restore`; không bao giờ ghi đè repo/thư mục người dùng.
- Tri thức bị viết lại đường dẫn đi qua ledger (rollback được); chính sách/guard/golden giữ nguyên ngữ nghĩa, chỉ đổi tiền tố đường dẫn,
  rồi rebaseline toàn vẹn.
- `move`: nguồn đóng băng cho tới khi người `unfreeze` — tránh 2 daemon cùng giữ một bot/OAuth.

## 9. Chia PR

| PR | Nội dung | Test chính |
|---|---|---|
| **1. Lõi gói + clone** | `hub_export.py`: format `.cfhx`, profile core/full (không checkpoints), quiesce (`hub.export_prepare|release`), verify; tách bộ viết lại đường dẫn của `home_migrate` thành `pathmap.py` (nhiều cặp, POSIX trước); `hub import` vào home trống với `--map-path` + dry-run; `MACHINE_STATE`; chế độ `clone` mặc định (tắt kênh, pause lịch, task chạy dở → blocked) | xuất → nhập sang `tmp/HUB_HOME` khác + `$HOME` giả: daemon lên, agent/task/ledger/graph/lịch còn nguyên, đường dẫn đã đổi, append-only không đổi, integrity sạch; nguồn vẫn chạy được sau export |
| **2. Secret** | `cryptography` trực tiếp, `secrets.enc` (mặc định có), passphrase, `--skip-secrets`; clone bỏ bot/OAuth, giữ token MCP tĩnh + tài khoản Claude | sai passphrase/gói tráo → từ chối; clone không mang token bot/OAuth |
| **3. Session Claude** | đóng gói + mã hoá lại thư mục `~/.claude/projects`, viết lại `cwd` | `fake_claude` kiểm `--resume <sid>` tìm thấy file ở thư mục mới; fixture jsonl thật |
| **4. Khác OS** | `pathmap` hai phía POSIX↔Windows, tên file không hợp lệ, MCP stdio thiếu, quyền file Windows, spike mã hoá cwd của Claude Code trên Windows | gói giả "từ Windows" nhập trên macOS/Linux và ngược lại, CI cả hai OS |
| **5. Move + repo + doctor** | `--mode move` + đóng băng nguồn + `hub unfreeze`; `repos.json`, `--clone-repos`, agent thiếu workspace → paused; `hub doctor` | nguồn đóng băng từ chối `run.submit`; repo git tạm: thiếu → gợi ý; dirty → cảnh báo |
| **6. Web** | mục 7b: `/hub-transfer` xuất (nền + link tải một lần) và nhập (upload → kế hoạch → `import-job` tách rời → daemon lên lại), thẻ máy mới tinh; CHANGELOG, `docs/dev-notes/hub-clone.md`, AGENTS.md | `tests/test_web_hub_transfer.py` |

Không migration DB (trạng thái ở `hub_state`). Ước lượng: PR1 lớn nhất (~2 ngày), PR4 ~1.5 ngày, các PR khác ~1 ngày.

## 10. Quyết định đã chốt (chủ repo, 2026-11-15)
1. Thêm `cryptography` làm dependency trực tiếp; gói mặc định mang secret đã mã hoá bằng passphrase.
2. Trường hợp chính: **hai máy cùng chạy** → mặc định nhập là `clone`; `move` là tuỳ chọn.
3. Hỗ trợ **cả** macOS/Linux **và** Windows, hai chiều (mục 5b).
4. **Bỏ `checkpoints/`** khỏi gói (mọi profile).
5. **Không** có kênh truyền (SSH…): người dùng chỉ export ra một file và import file đó.
6. **Web làm được cả xuất lẫn nhập** (mục 7b).

## Tiến độ
- **PR1** (447dfad): lõi gói `.cfhx` + nhập `clone`/`move` cùng OS — `hub_transfer.py`, `pathmap.py`.
- **PR2** (2026-11-15): secret mã hoá — `transfer_secrets.py` + `hub_transfer.seal_package|check_passphrase`, `cryptography>=42` trực tiếp.
  Lệch/chốt so với mục 5: (1) passphrase CHỈ ở tiến trình CLI — daemon đóng gói phần không mật (RPC `hub.export` không nhận passphrase), CLI
  gắn `secrets.enc` bằng cách cắt gói tại MANIFEST rồi ghi `secrets.enc` + manifest mới (không chép lại gói); PR6 (web) sẽ phải gọi
  `seal_package` trong daemon — `wrap_with_audit` đã gỡ `passphrase`. (2) Chỉ mang `auth/totp.json` (không `totp_enroll.json`). (3) TOTP/token
  MCP ở đích đã có và khác gói → giữ của đích, báo. (4) `clone` bỏ mọi `SLACK_*` (kể cả webhook một chiều) — đúng yêu cầu PR2.
  Sửa chỗ hở PR1: `clone` huỷ approval/câu hỏi đang chờ + run `waiting_human` → `interrupted`; nhập lỗi giữa chừng → tự hoàn tác (pre-restore
  hoặc home trống); làm lặng vòng nền ghi tri thức/DB (`runs.export_quiesced`) — lượt đang `await` dở không bị cắt (ghi rõ ở docstring
  `hub_transfer`), thứ tự chụp giữ DB trước rồi file (blob ledger luôn có đủ cho entry trong DB). PR1 tính nhầm `secrets/.meta.json` là
  secret thiếu — đã sửa (dùng `secrets.NAME_RE`).
- **PR3** (2026-11-15): session Claude Code — `transfer_sessions.py` + phần `claude/` (optional) của gói; nhập ghi vào
  `<claude home>/projects/<encode_cwd(cwd mới)>/`, viết lại đường dẫn từng dòng JSON, hoàn tác cùng lần nhập.
  Quy tắc mã hoá cwd XÁC NHẬN: đọc hàm `WH`/`k` trong binary Claude Code 2.1.325 (`replace(/[^a-zA-Z0-9]/g,"-")`, > 200 ký tự → 200 đầu +
  `-` + `Math.abs(hashJava).toString(36)`; tra thư mục dài theo tiền tố 200 ký tự + kiểm cwd) và đối chiếu 3.655/3.655 thư mục thật
  `~/.claude/projects` trên máy dev (có `.`, `_`, khoảng trắng, ngoặc, chữ Việt NFC); giá trị thư mục dài kiểm bằng chính hàm JS trên node.
  Lệch/chốt so với plan: (1) mục 4.3 nói lấy cả `manager_conversations` cho mọi profile — `core` chỉ lấy hội thoại CHƯA lưu trữ;
  run dở = `running|queued|waiting_human|interrupted` chưa có `continued_in_run_id` + `pending_resume`/`auto_resume_pending`.
  (2) Che secret theo giá trị trong transcript lúc xuất (mặc định, không tắt được) — resume vẫn chạy, model thấy `***`; giá trị nhiều dòng
  dạng thô trong file không phải JSON lines không bị che. (3) File ngoài `.jsonl` trong cây `<sid>/` (tool-results) chép nguyên, không đổi
  đường dẫn. (4) Không có cờ `--no-sessions`. (5) Không kiểm `claude --resume` thật (fake_claude không mô phỏng tra file) — có hàm hợp đồng
  `find_session` + test; nghiệm thu thật với claude chưa làm. Windows (`C:\…` → `C--…`) mới suy từ mã, chờ spike PR4.
- **PR4** (2026-11-15): khác hệ điều hành, hai chiều — `pathmap.py` (hai phía), `transfer_os.py` (tên file, MCP, cảnh báo theo OS).
  Bộ viết lại: mỗi cặp `cũ → mới` sinh các DẠNG của đường dẫn cũ (POSIX: một dạng; Windows: thô `C:\a`, JSON-escape `C:\\a`, `C:/a`, ổ đĩa
  không phân biệt hoa/thường), một regex hợp mọi dạng (dài trước, ranh giới như cũ); khớp dạng KHÁC kiểu với đích → quét tiếp phần đuôi của
  token đường dẫn (`_tail_end`) và chỉ đổi dấu phân cách trong đó. POSIX → Windows cần biết ngữ cảnh: chuỗi là JSON hợp lệ / file `.json` →
  dạng `\\`; YAML → `\\` khi trong nháy kép; còn lại dạng thô. DB/file cấu hình/transcript dùng chung bộ này. Không đổi format gói.
  Lệch/chốt so với plan: (1) đổi tên skill/tri thức KHÔNG qua ledger — đổi trong stage (thư mục, `skills.name|path`, `name:` frontmatter)
  + `import_followups.renamed|skills_renamed`: rename qua ledger = xoá + tạo lại skill (mất use_count/pin, hàng ledger cũ trỏ tên cũ) — quá
  lớn cho trường hợp hiếm này; ledger/lịch sử giữ tên cũ, tham chiếu theo tên cũ trong nội dung SKILL.md/memory người tự sửa.
  (2) "nhập ở trạng thái tắt" cho MCP: registry không có trạng thái bật/tắt (thêm = migration) → ĐÁNH DẤU `probe_error` + followups +
  gợi ý `mcp edit`; agent dùng server đó vẫn chạy (Claude Code báo MCP không kết nối). (3) Tên không hợp lệ trong `claude/` → bỏ file
  (phần optional), không chặn. (4) Quyền file Windows: không có ACL riêng — dùng mô hình sẵn có của `oscompat` (hồ sơ người dùng NTFS),
  `paths.tighten_permissions()` sau nhập (POSIX siết, Windows bỏ qua) + cảnh báo khi HUB_HOME Windows nằm ngoài hồ sơ. (5) Không có tham số
  CLI `--target-os` (luôn là OS đang chạy); `import_plan|import_package(target_os=, case_insensitive=)` chỉ cho test/web sau này.
  (6) Không chạy CI Windows ở PR này.
  **CHƯA kiểm trên Windows thật**: quy tắc thư mục session `C:\Users\x\…` → `C--Users-x-…` (suy từ mã Claude Code, không spike `claude`
  Windows); `claude --resume` thật sau khi nhập sang Windows; giải nén/đổi tên trên NTFS (test chỉ mô phỏng: đích Windows = tham số +
  `--map-path` sang `C:\Users\b\…`, file vẫn ghi vào thư mục tạm macOS; ca `skipif` `test_invalid_reason_agrees_with_real_windows_fs` chỉ
  chạy trên Windows); `shutil.which` của lệnh MCP trần trên Windows (PATHEXT `.cmd`); đường dẫn UNC `\\máy\share` mới có test đơn vị; giới
  hạn MAX_PATH 260 ký tự không kiểm; hub đích Windows đọc cấu hình YAML có đường dẫn `C:\…` dạng thô (đã kiểm `yaml.safe_load` trên macOS).
- **PR5** (2026-11-15): chuyển nhà + repo + tri thức + doctor — `hub_freeze.py`, `transfer_repos.py`, `import_followups.py`, `hub_doctor.py`;
  test `tests/test_hub_transfer_move.py`.
  Quyết định: (1) **Sao lưu VẪN chạy khi đóng băng** (`runs.exporting` thay `export_quiesced` ở `backup.py`): không tạo run, không chạm kênh
  ngoài, không đổi dữ liệu; hub đóng băng vẫn là dữ liệu của người (có thể `unfreeze`) — tắt sao lưu chỉ làm mất lưới an toàn. Mọi vòng nền
  ghi khác bỏ lượt qua `export_quiesced` (= đang xuất HOẶC đóng băng). (2) Đóng băng ghi NGAY trong cửa sổ làm lặng của `hub.export {for:move}`
  (sha256 gói chưa gắn secret, `sealed:false`) → không có khe để run/lịch chạy chen; CLI gắn `secrets.enc` xong gọi `hub.freeze` (CHỈ cập nhật
  cùng export_id: sha256 + `sealed:true`), gắn hỏng (gói bị xoá) → CLI tự `hub.unfreeze`. Ngoại tuyến: `freeze_offline` trước khi nhả lock.
  Xuất `--for move` khi đã đóng băng → từ chối; xuất clone từ hub đóng băng được; `migrated_to` ∈ `MACHINE_STATE` (gói không mang đóng băng
  sang máy mới). `--for move` cần xác nhận y/N (không TTY: `--yes`). (3) Telegram/Slack: chặn ở `token()` (không long poll/Socket Mode/gửi;
  `ignore_enabled` của người — `notify … test|setup` — vẫn chạy) + `QueuedNotifier` (cả webhook một chiều); freeze/unfreeze `kick()` vòng nhận.
  (4) Lý do tạm dừng agent thiếu workspace ở `hub_state.import_paused` (bảng agents không có cột lý do → không migration), hiện trong lỗi
  `submit` + doctor; `agent resume` gỡ. Áp cho MỌI agent có `workspace_default` không tồn tại sau khi nhập (không chỉ repo trong repos.json),
  cả `clone` lẫn `move`. (5) Actor `import` KHÔNG thuộc `AUTO_CHANGE_ACTORS` (do người khởi xướng) → không cần sửa danh sách actor nào khác;
  revision agent ghi actor `import`. (6) Graph: chỉ sửa fact/thực thể SỐNG tại chỗ (một entry graph mỗi tầng); `mem_episodes` (bằng chứng gốc),
  fact đã đóng hiệu lực, `.archive/` = lịch sử → giữ chữ cũ. `config.yaml` của agent = bản sao của hàng DB (đã viết lại lúc nhập) → dựng lại từ
  DB, không qua ledger. (7) `hub.doctor` HUMAN_ONLY (lộ đường dẫn/tên secret/repo), chỉ đọc, không audit, KHÔNG probe mạng trong RPC; CLI chạy
  tại chỗ (DB chỉ đọc) kể cả khi daemon dừng; `claude --version` luôn chạy, `claude auth status` + `mcp.probe` chỉ với `--probe`.
  `HUB_HUMAN_ONLY_CLI["hub"] = None` (mọi lệnh con).
  Lệch plan: không có `--include-dirty-patch` (§4.4) — chỉ cảnh báo dirty; doctor chưa kiểm `hubs.json`, `home status`, `web totp enroll`,
  Telegram/Slack `test` thật (chỉ bật/tắt + có token); `--clone-repos` dùng remote ĐÃ CHE (repo https riêng tư cần đăng nhập git ở máy mới —
  `GIT_TERMINAL_PROMPT=0` → báo lỗi, người clone tay); không kiểm `claude --resume` thật (như PR3).
  **Chữ ký RPC cho PR6 (web)**: `hub.export {profile, stop, for: clone|move}` → `{id, path, size, sha256, …, repos[{path, git, remote,
  remote_masked, branch, dirty, uncommitted}], migrated_to?}`; `hub.frozen {}` → `{migrated_to|null, banner}` (đọc, mọi người gọi);
  `hub.freeze {export_id, sha256, sealed}` (HUMAN_ONLY, chỉ cập nhật đóng băng cùng export_id); `hub.unfreeze {export_id?}` (HUMAN_ONLY) →
  `{unfrozen, migrated_to}`; `hub.doctor {}` (HUMAN_ONLY) → `{ok, home, checks[{id, check, ok, level ok|info|warning|error, detail, fix}],
  summary{errors, warnings, total}}`. `import_plan` trả thêm `repos[{from, to, exists, git, remote, branch, head, dirty, uncommitted, used_by,
  hint, head_match?}]`, `agents_paused{id: {reason, workspace}}`; `import_package(clone_repos=)` → `repos_cloned[{to, ok, error?, warning?}]`,
  `agents_unpaused[]`. Web PR6 dùng `hub.doctor` cho trang sau nhập; banner đóng băng đã có ở `base.html`.
- **PR6** (2026-11-15): web — `hub_transfer_web.py` (RPC + tiến trình `hub import-job` + chờ/dọn lúc daemon khởi động), `web/transfer_pages.py`
  (`/hub-transfer`, kế hoạch nhập, tải lên/tải về), `static/transfer.js`, thẻ Cài đặt + thẻ trang chủ khi hub trống; test
  `tests/test_web_hub_transfer.py` + `tests/test_sec_auth.py::test_hub_transfer_web_rpcs_human_only`. Tài liệu: `docs/dev-notes/hub-clone.md`.
  Lệch/chốt so với §7b: (1) passphrase KHÔNG có ở form đầu — gõ 2 ô ở bước 2 (`Action.reenter`), vì "form 2 ô + nhập lại ở bước 2" là hai lần gõ
  không so được với nhau (server không giữ lần đầu); POST cuối là chỗ duy nhất passphrase rời trình duyệt. (2) RPC đặt tên `hub.export_start` /
  `hub.import_start` (+ `hub.transfer_status`, `hub.export_status`, `hub.import_status`) — `hub.import_plan` nhận `upload` (id file tải lên), không
  nhận đường dẫn tuỳ ý. (3) Web đụng FILE GÓI trực tiếp ở hai chỗ (tải lên ghi `imports/`, tải về đọc `exports/<ID_RE>.cfhx`) — gói hàng GB
  không qua JSON-RPC được; web không bao giờ đụng hub.db; web KHÔNG đọc `state.json` khi daemon dừng (như `update_pages`: báo "đang dừng", poll).
  (4) "Hub trống" cho web = `hub_fresh` (không agent/team/task/run/lịch/secret) vì daemon đang chạy luôn có hub.db (`home_blank` luôn sai) — nhập
  vẫn chụp `pre-restore` (job tự tính `replace = not home_blank`). (5) Gói tải lên bị xoá sau khi nhập thành công (giữ khi lỗi để thử lại; daemon
  dọn sau 24 giờ). (6) `transfer.max_upload_mb` không vào danh mục Cài đặt web. (7) Tiến độ chỉ theo pha (+ số byte đã ghi khi đóng gói).
  Chưa kiểm: job + `spawn_background` trên Windows thật; dưới launchd thật (daemon thoát 75, daemon mới chờ `hold_while_importing`) mới có test
  đơn vị.
