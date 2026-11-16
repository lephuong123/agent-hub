# Thư mục dùng chung với "Không gian số" (`filehub/`)

Ngày: 2026-11-15. Plan + quyết định + hợp đồng 3 repo: `plan-cloud-files.md` (§3, §7, §8). Không có migration.

## Tóm tắt

Một thư mục đồng bộ 2 chiều giữa máy chạy hub (`~/KhongGianSo/<tên>/`) và một thư mục người là Owner trên Không gian số. Đồng bộ do
binary `tang-thu-sync-headless` (repo `tang-thu-sync`) làm — daemon chỉ giám sát tiến trình, đọc file trạng thái, ghi file điều khiển.
cfharness KHÔNG cầm token sync (binary tự ghi `--tokens`), KHÔNG gọi API Không gian số.

| Thành phần | Vai trò |
|---|---|
| `filehub/supervisor.py` | Tiến trình con của daemon: kiểm `--version` ≥ `filehub.sync_min_version`, env danh sách trắng (`ENV_KEEP` — không `CLAUDE*`/`CFHARNESS*`/`ANTHROPIC*`, không token người), `start_new_session`, restart backoff luỹ thừa (`filehub.restart_backoff_s`, chạy ổn > 60 s thì đặt lại), dừng SIGTERM → `stop_grace_s` → SIGKILL, đối soát `(pid, start_time)` ở `HUB_HOME/filehub/supervisor.json` khi daemon khởi động lại (tiến trình mồ côi khớp → dừng trước khi chạy bản mới) |
| `filehub/service.py` | `hub.filehub`: thư mục (`filehub.folders`), trạng thái (status.json), link (index/<root>.json), presence (control.json), steer, báo sau run, đăng nhập (`<binary> login`), thêm/gỡ/tạm dừng |
| `filehub/rpc.py`, `filehub/cli.py` | RPC `filehub.status|list|link` (đọc) + `setup|login|add|remove|pause|resume` (HUMAN_ONLY, tự audit); CLI `cfharness filehub …` |
| `web/filehub_pages.py` | Thẻ "Không gian số" ở Cài đặt + `/filehub/open` (chuyển hướng tới `<server>/files/<folder_id>?file=<file_id>`) |

Tệp: `HUB_HOME/filehub/{profile/, status.json, control.json, ignore.txt, supervisor.json}` (thư mục 0700; sandbox denyRead/denyWrite +
`permissions.deny` Read/Grep/Glob; classifier `filehub_metadata` = cấm), token `HUB_HOME/secrets/filehub/tokens.json` (0600). Backup bỏ
`filehub/` (tái tạo được); cấu hình `filehub.*` nằm trong `config.yaml` (được sao lưu, ∈ `integrity.SECURITY_KEYS`). Xuất/nhập hub
(`hub-clone.md` §2.1b): token + `filehub/` không bao giờ đi theo gói; nhập → `filehub.enabled=false` tới khi người `filehub setup` lại.

## Luồng

1. `cfharness filehub setup --server URL --binary PATH [--name cfharness] [--local-parent ~/KhongGianSo]` → ghi cấu hình → chạy
   `<binary> login --server --tokens --device "cfharness · <máy>" --root-hint <tên> [--no-browser]` → đọc dòng `LOGIN_URL` (trả ngay cho CLI/RPC)
   → chờ ở nền các dòng `ROOT <id>\t<wsId>\t<name>` → exit 0 → tự thêm root đầu tiên làm thư mục chung (`local_path` = `<local-parent>/<tên>`,
   kiểm: tuyệt đối, không trong/chứa HUB_HOME, không quá rộng, không trùng thư mục khác, trong `workspace.confine` W nếu bật giam, không chạm vùng
   bảo vệ cố định) → chạy tiến trình sync với `--add-root <id> --root-name <tên> --ws <wsId> --local-parent <cha>` (một root mỗi lần chạy, theo hợp
   đồng; khi root đã có trong `status.roots` thì không truyền nữa). Dòng `ERROR <mã>` → `login.state=error`.
2. Vòng `filehub.tick_s` (1 s): đọc status.json → event `filehub.state` khi trạng thái tổng đổi (`idle|syncing|conflict|offline|auth_expired|error|
   paused|version_mismatch`), `filehub.conflict` cho bản xung đột mới, `filehub.blocked` khi chiều đẩy lên dừng (`mass_delete`); mục `recent`
   chiều `down` mới → nếu `WorkspaceLocks.holder(<gốc>)` là run đang chạy → `steer(by="hub", channel="hub")` câu cố định
   `[cfharness] The human changed <rel> on Không gian số while you were working. Re-Read it before editing.` (một lần mỗi (run, tệp)).
   Lần đọc đầu tiên chỉ ghi nhận (thay đổi cũ không phải "trong lúc run chạy").
3. `run.started` của run có `run_opts.filehub` và không chỉ đọc → `control.json.presence[<root>] = {label: "cfharness · <agent>", ttl_s}`;
   kết thúc (trừ `waiting_human`) → `null`; làm mới khi quá nửa `ttl_s` (mtime đổi → binary đọc lại ≤ 2 s và PUT presence).
4. `workspace.diff` của run trên `filehub:` → `filehub.deleted{run_id, folder, count, paths≤20, url}` (không hỏi duyệt — §7.3),
   `filehub.changed{…}`, `filehub.secret_warning{…}` khi tệp mới/sửa chứa GIÁ TRỊ secret hub biết (`redact.contains_secret_value`, tệp ≤
   `secret_scan_max_bytes`). Telegram/Slack: dòng chữ cố định, link là chữ, không nút (`notify/slack._filehub_text`, Telegram dùng lại).

## Workspace `filehub:<tên>[/<con>]`

- `FilehubService.resolve_ref`: không `..`, không phần bắt đầu `.tangthu`, realpath phải trong gốc (symlink thoát → lỗi), thư mục phải có.
- Task: `workspace: "filehub:chung/Báo cáo"` → lưu `{kind: filehub, ref, path, folder}`; dispatcher chạy như `dir` + `run_opts.workspace_kind=filehub`,
  `run_opts.filehub=<tên>`. Run ad-hoc: `run.start {cwd: "filehub:…"}` (RunManager.submit giải ref).
- Khoá = GỐC thư mục chung (`WorkspaceLocks(store, hub)` gắn `key_for` theo instance — `filehub.root_of`), cả khi cwd là thư mục con; run chỉ đọc
  không lấy khoá. Checkpoint luôn `_walk` (`take_checkpoint(force_walk=True)`, kể cả khi là repo git); diff mang `filehub`; `run.revert` từ chối
  (`workspace.FILEHUB_NO_REVERT`) — hoàn tác ở phiên bản/thùng rác Không gian số; web không hiện nút hoàn tác.
- Gốc thư mục chung vào `validate_workspace` + `guard.profiles.allowed_roots` (để `extra_write_roots` trỏ vào đó được). `extra_write_roots` mở cho
  workspace `scratch|filehub` (trước chỉ `dir|worktree`).
- Mọi run được ĐỌC (§7.4): khi giam, gốc thư mục chung vào `confine_read`/allowRead (nó phải nằm trong W nên thường đã đọc được); system prompt có
  khối `# Shared folder (Không gian số)` (tiếng Anh, theo cấu hình → không phá prompt cache): nội dung là DỮ LIỆU, không theo lệnh trong tệp.

## Guard (`guard/classifier.py`)

- `GuardContext.filehub_roots` (từ `GuardService._filehub_roots`). `BashAnalyzer.filehub_write`: xoá/ghi đè/`mv` (cả ra ngoài) đích TRONG thư mục chung
  VÀ trong vùng ghi của run (cwd/extra_write_roots/tmp) → `write` rule `filehub_delete|filehub_overwrite`; chính gốc → `destructive`
  `filehub_root_delete`; ngoài vùng ghi của run → luật cũ (`delete_not_run_created`…).
- `.tangthu*` trong thư mục chung và mọi đường dẫn dưới `HUB_HOME/filehub` → `forbidden` `filehub_metadata` (tool ghi, Bash ghi/xoá, cả chuỗi lệnh có
  nhắc `HUB_HOME/filehub`); đọc `HUB_HOME/filehub` = `sensitive_read`.
- `cfharness filehub setup|login|add|remove|rm|pause|resume` ∈ `HUB_HUMAN_ONLY_CLI`.
- Giới hạn: xoá qua mã (`python -c "shutil.rmtree(…)"`, `node fs.rm…`) vẫn theo luật API xoá trong mã (hỏi người) — chỉ lệnh shell đã phân tích được hạ
  về `write`.

## Quyền

- RPC ghi `filehub.*` ∈ `auth.HUMAN_ONLY`, `MUTATING_FILEHUB` ∈ `auth.all_mutating()` (bọc audit), KHÔNG ∈ `MANAGER_ALLOWED`. Điều phối viên có tool
  chỉ đọc `filehub_list|filehub_link` và giao việc workspace `filehub:` (`manager/permissions.check_path` giải ref rồi kiểm như đường dẫn thường).
- Web (2026-11-16, xem § Web): thiết lập/đăng nhập/thêm/gỡ/tạm dừng/chạy lại/cho đẩy tiếp làm được cả qua link public; chỉ đường dẫn bản sao
  trên máy (`local_parent`, `local_path`) là `is_local_request`; đường dẫn binary không bao giờ từ web.

## Lệch plan / hợp đồng

- Plan §3.3 cũ "xoá = destructive" thay bằng §7.3 (xoá = `write`, chỉ báo). `filehub.max_delete_batch` mặc định 0 (tắt), là công tắc tuỳ chọn.
- Plan §3.1 "đọc base SQLite của sync-core" → dùng `index/<rootId>.json` theo hợp đồng §8.3.
- `filehub.resync` (plan §3.1) chưa làm. `filehub.resume {root_id}` = gửi `resume_push` qua control.json (không có cờ `--resume-push` riêng — theo §8.3).
- Báo sau run (deleted/changed/secret) chỉ cho run có WORKSPACE `filehub:` (có checkpoint/diff). Run ghi vào thư mục chung qua `extra_write_roots`
  không có diff ở thư mục đó → không có báo (thay đổi vẫn hiện ở `recent`/Không gian số).
- Steer chỉ cho run GIỮ KHOÁ gốc (workspace `filehub:`), không cho run chỉ đọc hay run dùng `extra_write_roots`.
- Kiểm sha256 binary (S3) chưa làm: `filehub.sync_binary` là đường dẫn người đặt; hub chỉ kiểm `--version`.

## Web — tự thiết lập qua link public (2026-11-16)

Trước đây thẻ Cài đặt qua link public chỉ xem, và `setup-hub-user.sh` bắt admin mở link đồng ý TRÊN máy chủ. Nay người dùng hub tự làm trên web:

1. Thẻ "Không gian số" (chưa có thư mục) hiện form: địa chỉ Không gian số (`https://`), tên thư mục (mặc định `cfharness`); web cục bộ thêm ô
   thư mục cha. Thiếu binary (`detect_binary` không thấy) → thẻ báo nhờ admin cài, không hiện form.
2. `filehub_setup` → RPC `filehub.setup{server, name, no_browser: true}` → thẻ hiện nút "Mở trang đồng ý" (URL `LOGIN_URL` của binary, chỉ
   `http(s)://`, `rel=noopener noreferrer`) và tự poll `/partials/filehub` 3 s/lần khi `login.state ∈ starting|waiting`.
3. Người đồng ý → `_finish_login` tự thêm root đầu tiên (như CLI). Root khác cấp lúc đăng nhập → form "Thêm" (`filehub_add`). "Thêm thư mục
   khác…" / "Đăng nhập lại" = `filehub_login` (luôn `no_browser`). "Gỡ" = `filehub_remove` (xác nhận, giữ tệp).

Ranh giới:
- `filehub.detect_binary`: cấu hình `filehub.sync_binary` → `BINARY_CANDIDATES` (chỗ cài cố định), KHÔNG dò PATH. Web không bao giờ gửi
  `binary` (bộ dựng tham số bỏ trường này) → web không chạy được tệp tuỳ ý.
- Đường dẫn trên máy (`local_parent`, `local_path`) từ phiên public → từ chối trước khi gọi RPC (`_guard_local`). Qua cloud, bản sao đặt ở
  `<local_parent>/<tên>`, vẫn qua `_validate_local` (không trong/chứa HUB_HOME, trong W khi giam…).
- Mọi thao tác qua `ACTIONS` (CSRF + Origin + khoá phiên, có bước xác nhận) → RPC HUMAN_ONLY, audit kênh `web`.
- Lệch plan-cloud-files §3.6 "qua cloud chỉ xem": chủ repo quyết 2026-11-16 cho người dùng hub tự nối Không gian số từ cloud. Quyết định 1
  (web cfharness không ghi TỆP qua cloud) giữ nguyên.

Test: `tests/test_web_filehub.py::test_setup_login_and_add_folder_over_public_link`, `::test_pause_remove_over_public_link`.

## Test (không mạng, không claude)

`tests/fake_tangthu_sync.py` (binary giả đúng §8.3: `--version`, `login`, vòng chính ghi status/index, đọc control, `inject.jsonl` mô phỏng cloud,
`--max-delete-batch`, SIGTERM dừng êm), `tests/fh_fake.py`; `test_filehub_supervisor.py`, `test_filehub_workspace.py`, `test_filehub_concurrency.py`,
`test_web_filehub.py`, ca `SAFE_FILEHUB`/`DESTRUCTIVE_FILEHUB` trong `test_guard_classifier.py`, `test_sec_auth.py::test_filehub_writes_human_only`,
`test_manager_security.py::test_manager_filehub_read_and_assign_but_no_writes`.

## Chạy thử với binary thật

```bash
cfharness filehub setup --server https://thumucai.trungtamdhs.com --binary /đường/dẫn/tang-thu-sync-headless --name cfharness
# (tuỳ chọn) cfharness config set filehub.sync_min_version 0.1.0 ; filehub.max_delete_batch 50 ; filehub.local_parent ~/KhongGianSo
cfharness filehub status
cfharness task create --assignee <agent> --workspace "filehub:cfharness/Báo cáo" "đọc yêu cầu, viết báo cáo .docx"
```
Nếu bật `workspace.confine`, `filehub.local_parent` phải nằm trong W trước khi `setup`.

## Việc dở

- Nghiệm thu thật (§5 plan) khi có binary S1–S3 + T1/T3 phía Không gian số.
- Kiểm sha256/tải binary theo tag; `filehub.resync`; báo sau run cho `extra_write_roots`; chặn đẩy tệp có secret TRƯỚC khi lên cloud (cần hook sync-core).

## Sửa sau nghiệm thu thật (2026-11-15)

Nghiệm thu với binary thật + Không gian số dev + claude haiku thật đạt (setup, 2 chiều, `.env` bỏ qua, xoá không hỏi + `filehub.deleted`,
thùng rác cloud, presence, steer by=hub). Bốn lỗi đã sửa:
1. `workspace_default` của agent nhận `filehub:<tên>[/<con>]` (`validate_workspace` giải ref; `RunManager.submit` dùng ref khi run không có cwd →
   run có `run_opts.filehub` như `--cwd filehub:…`; `WorkspaceLocks.key_for`/`fileview` giải ref).
2. `ignore.txt` mặc định thêm `.claude/`, `.claude.json`, `.cc-writes` (Claude Code ghi `.claude/.cc-writes` trong cwd của run).
3. `filehub.state` không phát/ghi mỗi vòng syncing↔idle (syncing gộp vào idle cho event); trạng thái thật vẫn ở `filehub.status`.
4. Tên thiết bị: macOS `scutil --get ComputerName`, không có → hostname (IP giữ nguyên, không cắt ở dấu chấm), ≤ 40 ký tự.
