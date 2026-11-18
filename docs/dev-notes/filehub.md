# Thư mục dùng chung với "Không gian số" (`filehub/`)

Ngày: 2026-11-15 (gốc workspace + cài đặt trên web: 2026-11-16, PR H7 — `plan-filehub-workspace-root.md`). Plan + quyết định + hợp đồng 3 repo:
`plan-cloud-files.md` (§3, §7, §8). Không có migration.

> **Chế độ `lane` (cổng cfharness, 2026-11-17):** thư mục có `folders[].mode = lane` (hoặc `filehub.mode = lane`) KHÔNG đồng bộ về máy —
> cây làm việc theo run qua cổng loopback của Không gian số. Xem `plan-filehub-lane.md` (§C hợp đồng, §10 đã làm). Mọi mục dưới đây là chế độ `sync`.
> Người dùng: tạo khoá ở Không gian số › Hồ sơ › Cổng cfharness (chọn thư mục/workspace cấp cho hub) → dán ở Cài đặt › Cổng cfharness (hoặc
> `cfharness filehub lane setup`, bỏ trống địa chỉ) → mọi thư mục được cấp thành `filehub:<tên>` dùng được ngay; cấp thêm/thu hồi trên Không
> gian số → hub tự cập nhật trong ≤ 5 phút (bấm "Thử kết nối" để ngay). Thư mục đang đồng bộ kiểu cũ cùng gốc giữ nguyên tới khi bấm
> "Chuyển sang cổng".

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
- Web (#121 + W7/H7, xem § Web): mọi thao tác (thiết lập, đăng nhập lại, chuyển sang workspace, thêm/gỡ thư mục, tạm dừng/chạy lại/cho đẩy
  tiếp) làm được cả qua link public — vẫn đăng nhập web + TOTP + CSRF + khoá phiên; thiết lập/đăng nhập/chuyển/gỡ = `Action.step2`. Đường dẫn
  binary không bao giờ từ web; đường dẫn bản sao trên máy (`local_parent`, `local_path`) chỉ khi `is_local_request`; qua link public
  (`remote`) không ĐỔI được server/binary đã đặt.

## Lệch plan / hợp đồng

- Plan §3.3 cũ "xoá = destructive" thay bằng §7.3 (xoá = `write`, chỉ báo). `filehub.max_delete_batch` mặc định 0 (tắt), là công tắc tuỳ chọn.
- Plan §3.1 "đọc base SQLite của sync-core" → dùng `index/<rootId>.json` theo hợp đồng §8.3.
- `filehub.resync` (plan §3.1) chưa làm — hợp đồng headless (§8.3 + `tang-thu-sync/docs/headless-cfharness.md`) không có cơ chế (control.json
  chỉ `presence` + `resume_push`), hub không bịa giao thức. `filehub.resume {root_id}` = gửi `resume_push` qua control.json (không có cờ `--resume-push` riêng — theo §8.3).
- Báo sau run (deleted/changed/secret) cho run có WORKSPACE `filehub:` VÀ (2026-11-17) cho mỗi `extra_write_roots` nằm trong thư mục chung:
  `RunManager._extra_checkpoints` chụp danh sách tệp (`take_checkpoint(force_walk=True)`, ≤ 5 gốc, bỏ gốc đã nằm trong workspace `filehub:` chính
  và gốc lồng nhau, bỏ run chỉ đọc / `workspace.checkpoint` tắt) → `_extra_diff` → `FilehubService.report_extra` → cùng event, payload thêm
  `extra_root` (đường dẫn tương đối trong thư mục chung). Diff này không lưu vào `runs.diff` (không revert, chỉ để báo).
- Steer chỉ cho run GIỮ KHOÁ gốc (workspace `filehub:`), không cho run chỉ đọc hay run dùng `extra_write_roots`.
- Kiểm sha256 binary (S3) chưa làm: `filehub.sync_binary` là đường dẫn người đặt; hub chỉ kiểm `--version`.

## Web — tự thiết lập + quản lý trên web, cả qua link public (#121 + H7/W7, 2026-11-16)

Trước đây thẻ Cài đặt qua link public chỉ xem, và `setup-hub-user.sh` bắt admin mở link đồng ý TRÊN máy chủ. Nay người dùng hub tự làm trên
web (gộp #121 "tự thiết lập" với W7 "cài đặt qua link public + relay OAuth" — một thẻ, một luồng đăng nhập, một bộ `ACTIONS`):

1. Thẻ "Không gian số" (chưa có thư mục) hiện form: địa chỉ Không gian số (`https://`, kiểm ở web), **chế độ** (mặc định toàn bộ workspace —
   tài khoản Workspace Admin; hoặc một thư mục), tên thư mục (chế độ một thư mục, mặc định `cfharness`); web cục bộ thêm ô thư mục cha.
   KHÔNG có ô binary. Thiếu binary (`detect_binary` không thấy) → thẻ báo nhờ admin cài, không hiện form.
2. `filehub_setup` (xác nhận + bước 2 nêu hệ quả của chế độ) → RPC `filehub.setup{server, scope, name?, no_browser: true, relay, remote}` →
   chuyển tới `/filehub/login` (từng bước theo chế độ). Thẻ ở Cài đặt cũng hiện nút "Mở trang đồng ý" (URL `LOGIN_URL` của binary, chỉ
   `http(s)://`, `rel=noopener noreferrer`) và tự poll `/partials/filehub` 3 s/lần khi `login.state ∈ starting|waiting` — cùng MỘT lượt
   đăng nhập (`login_state` của daemon), hai chỗ xem.
3. Kiểu đăng nhập: web trên chính máy hub (`is_local_request`) → listener loopback của binary như CLI; qua link public → relay OAuth (cần
   `web.public_origin`; § "Gốc workspace + cài đặt trên web"). Luôn `no_browser`.
4. Người đồng ý → `_finish_login` tự thêm root đầu tiên (như CLI). Root khác cấp lúc đăng nhập → form "Thêm" (`filehub_add`, xác nhận).
   Một nút `filehub_login` (bước 2): "Đăng nhập lại" (chế độ workspace) / "Đăng nhập lại / thêm thư mục khác…" (chế độ một thư mục — trang
   đồng ý cho chọn/tạo thêm thư mục), nổi bật khi hết phiên / mất quyền / vừa chuyển máy. "Chuyển sang toàn bộ workspace" (`filehub_switch`,
   bước 2) khi còn thư mục chế độ folder. "Gỡ khỏi hub" (`filehub_remove`, bước 2, giữ tệp) — ẩn khi run đang giữ khoá thư mục.

Ranh giới (giữ luật CHẶT hơn của cả hai bên):
- `filehub.detect_binary`: `binary` (chỉ CLI) → cấu hình `filehub.sync_binary` → `BINARY_CANDIDATES` (chỗ cài cố định), KHÔNG dò PATH (bỏ
  `binary_hint` dò PATH của H7). Web không bao giờ gửi `binary` (bộ dựng tham số bỏ trường này) → web không chạy được tệp tuỳ ý.
- Qua link public (`remote`): không đổi server đã đặt; không đổi binary đã đặt (kể cả khi `detect_binary` tìm ra chỗ khác); chưa đặt binary
  thì binary tự dò phải tên `tang-thu-sync-headless` và người dùng của hub không ghi được (tệp + thư mục chứa — `_remote_binary_ok`).
- Đường dẫn trên máy (`local_parent`, `local_path`) từ phiên public → từ chối trước khi gọi RPC (`_guard_local`). Qua cloud, bản sao đặt ở
  `<local_parent>/<tên>`, vẫn qua `_validate_local` (không trong/chứa HUB_HOME, trong W khi giam…).
- Mọi thao tác qua `ACTIONS` (CSRF + Origin + khoá phiên, có bước xác nhận; thiết lập/đăng nhập/chuyển/gỡ có bước 2) → RPC HUMAN_ONLY, audit
  kênh `web`.
- Lệch plan-cloud-files §3.6 "qua cloud chỉ xem": chủ repo quyết 2026-11-16 cho người dùng hub tự nối Không gian số từ cloud. Quyết định 1
  (web cfharness không ghi TỆP qua cloud) giữ nguyên.

Test: `tests/test_web_filehub.py::test_setup_login_and_add_folder_over_public_link`, `::test_pause_remove_over_public_link`,
`::test_setup_via_public_link_relay_oauth_callback`, `::test_login_on_hub_machine_uses_loopback_listener_and_switch_remove_step2`;
`tests/test_filehub_supervisor.py::test_remote_setup_cannot_change_server_or_pick_writable_binary`.

## Test (không mạng, không claude)

`tests/fake_tangthu_sync.py` (binary giả đúng §8.3: `--version`, `login`, vòng chính ghi status/index, đọc control, `inject.jsonl` mô phỏng cloud,
`--max-delete-batch`, SIGTERM dừng êm), `tests/fh_fake.py`; `test_filehub_supervisor.py`, `test_filehub_workspace.py`, `test_filehub_concurrency.py`,
`test_web_filehub.py`, ca `SAFE_FILEHUB`/`DESTRUCTIVE_FILEHUB` trong `test_guard_classifier.py`, `test_sec_auth.py::test_filehub_writes_human_only`,
`test_manager_security.py::test_manager_filehub_read_and_assign_but_no_writes`.

## Chạy thử với binary thật

```bash
cfharness filehub setup --server https://thumucai.trungtamdhs.com --binary /đường/dẫn/tang-thu-sync-headless   # --scope folder --name cfharness: một thư mục
# (tuỳ chọn) cfharness config set filehub.sync_min_version 0.1.0 ; filehub.max_delete_batch 50 ; filehub.local_parent ~/KhongGianSo
cfharness filehub status
cfharness task create --assignee <agent> --workspace "filehub:cfharness/Báo cáo" "đọc yêu cầu, viết báo cáo .docx"
```
Nếu bật `workspace.confine`, `filehub.local_parent` phải nằm trong W trước khi `setup`.

## Đổi tên cfharness (`filehub.rename`, 2026-11-17)

`cfharness filehub rename <tên cũ> <tên mới>` / RPC `filehub.rename {name, new_name}` (HUMAN_ONLY, `MUTATING_FILEHUB`, `HUB_HUMAN_ONLY_CLI`) / thẻ
Cài đặt "Đổi tên trong cfharness" (`ACTIONS.filehub_rename`, có xác nhận, một bước — không nới quyền):
- Chỉ đổi `folders[].name`; `local_path`, `root_id`, tên trên Không gian số giữ nguyên; không khởi động lại binary (tên chỉ vào argv qua
  `--root-name` của lần thêm root).
- Tên cũ vào `folders[].aliases` (≤ `ALIASES_MAX`=5, đổi lại tên cũ thì bỏ khỏi bí danh): task (`workspace.folder`), `run_opts.filehub`,
  `workspace_default` của agent đã ghi tên cũ vẫn khớp. Thứ tự khớp `match_ref`: tên cfharness → bí danh cũ → tên sống trên web. `add`/`rename`
  từ chối tên trùng tên hoặc bí danh của thư mục khác.
- Từ chối khi có run giữ khoá gốc thư mục, hoặc root chưa đồng bộ lần đầu (`pending_add` — binary còn nhận `--root-name`, đổi tên lúc đó sẽ lệch
  `local_path`).
Test: `test_filehub_workspace.py::test_rename_*`, `test_web_filehub.py::test_rename_from_web`, `test_sec_auth.py::test_filehub_writes_human_only`,
`test_manager_security.py::test_manager_filehub_read_and_assign_but_no_writes`, `test_guard_classifier.py::test_filehub_cli_human_only`.

## Việc dở

- Nghiệm thu thật (§5 plan) khi có binary S1–S3 + T1/T3 phía Không gian số.
- **Kiểm sha256/tải binary theo tag** — cần phía tang-thu-sync: CI (`.github/workflows/ci.yml`) và `docs/PHAT-HANH.md` chỉ phát bản app desktop
  (`install.json` có `sha256` cho `.dmg`/`-setup.exe`), CHƯA phát `tang-thu-sync-headless` kèm checksum. Khi có (vd `headless.json`
  `{version, files[{platform, arch, url, sha256}]}` hoặc `SHA256SUMS` theo tag), hub thêm `filehub.sync_sha256` (người đặt, ∈ SECURITY_KEYS) +
  kiểm trước mỗi lần spawn trong `SyncSupervisor` (+ `cfharness filehub install --tag`). Không tự bịa nguồn checksum.
- **`filehub.resync`** (yêu cầu đồng bộ đầy đủ lại) — cần phía tang-thu-sync: hợp đồng headless chưa có cơ chế (control.json chỉ `presence`,
  `resume_push`; không có cờ). Đề xuất cho S-tiếp: khoá control `"resync": ["<rootId>"]` áp khi mtime đổi (như `resume_push`), binary quét lại toàn
  bộ root + đối chiếu base. Khi có: hub thêm RPC `filehub.resync {root_id}` (HUMAN_ONLY, MUTATING) ghi khoá đó + sửa CẢ `tests/fake_tangthu_sync.py`.
- Chặn đẩy tệp có secret TRƯỚC khi lên cloud — cần hook trong sync-core (repo tang-thu-sync); hub hiện chỉ cảnh báo SAU run (`filehub.secret_warning`).

## Sửa sau nghiệm thu thật (2026-11-15)

Nghiệm thu với binary thật + Không gian số dev + claude haiku thật đạt (setup, 2 chiều, `.env` bỏ qua, xoá không hỏi + `filehub.deleted`,
thùng rác cloud, presence, steer by=hub). Bốn lỗi đã sửa:
1. `workspace_default` của agent nhận `filehub:<tên>[/<con>]` (`validate_workspace` giải ref; `RunManager.submit` dùng ref khi run không có cwd →
   run có `run_opts.filehub` như `--cwd filehub:…`; `WorkspaceLocks.key_for`/`fileview` giải ref).
2. `ignore.txt` mặc định thêm `.claude/`, `.claude.json`, `.cc-writes` (Claude Code ghi `.claude/.cc-writes` trong cwd của run).
3. `filehub.state` không phát/ghi mỗi vòng syncing↔idle (syncing gộp vào idle cho event); trạng thái thật vẫn ở `filehub.status`.
4. Tên thiết bị: macOS `scutil --get ComputerName`, không có → hostname (IP giữ nguyên, không cắt ở dấu chấm), ≤ 40 ký tự.

## Gốc workspace + cài đặt trên web (PR H7, 2026-11-16)

Plan + quyết định W1–W7: `plan-filehub-workspace-root.md`. Hợp đồng chung: `tang-thu-sync/protocol/openapi.yaml` (6a9eaf9).

- Cấu hình: `filehub.folders[].kind` = `folder` | `workspace` (thiếu = folder) + `account` (username, chỉ hiển thị); `filehub.scope` (mặc định
  `workspace`) cho `setup`; `filehub.relay_timeout_s` (600). Chế độ workspace: `root_id = ws_id` (gốc ảo), bản sao `<local_parent>/<tên ws>`.
- Đăng nhập: `login --root-mode workspace` CHỈ khi chế độ workspace (single là mặc định của binary → không truyền cờ, binary cũ vẫn chạy chế độ
  folder; binary cũ + workspace → `ERROR usage` → báo "chưa hỗ trợ chế độ workspace"). Đăng nhập lại giữ chế độ của thư mục đang có.
  Dòng `ROOT <id>\t<wsId>\t<name>[\t<kind>\t<account>]` — 3 cột cũ vẫn đọc (kind=folder, account rỗng).
- Relay OAuth (web public): `filehub.login{relay:true}` → binary `--redirect-uri <web.public_origin>/filehub/oauth/cb --no-browser --timeout
  <relay_timeout_s>`, stdin PIPE; daemon lấy `state` từ query của LOGIN_URL (thiếu/ngắn → từ chối). Route web CÔNG KHAI `GET /filehub/oauth/cb`
  (`GuardMiddleware.PUBLIC`; `WebState.fh_limiter` khoá theo IP + toàn cục sau 10 lần sai) → RPC `filehub.login_callback{code,state,error?}`
  (HUMAN_ONLY, MUTATING, code/state bị `pop` trước audit): `compare_digest`; sai/hết hạn/không có lượt chờ → lỗi CHUNG và lượt chờ GIỮ
  NGUYÊN; khớp → xoá state (một lần) → `CODE <code>\t<state>\n` vào stdin (code/state chỉ ký tự URL-safe, ≤ 2048 — không chèn được dòng
  thứ hai). `?error=` của trang đồng ý: sau khi state khớp → `ERROR <mã>\t<state>` vào stdin (mã chỉ `[a-z0-9_]` ≤ 64, khác → `other`), binary
  in `ERROR <mã>` và thoát. Trang kết quả chỉ chữ cố định hoặc chữ daemon dựng (`message`), không phản chiếu query.
  Web trên chính máy hub (`is_local_request`) dùng listener loopback như CLI (`no_browser`). Thẻ + trang `/filehub/login`: § Web.
- Hợp đồng headless: `tang-thu-sync/docs/headless-cfharness.md` (S5, commit 9869ede). Chạy đồng bộ với `--add-root <id> … --root-kind workspace`
  cho gốc workspace (thiếu = folder; hub chỉ truyền khi workspace). Lỗi `login`: `ERROR <mã>` exit 1 (`usage` exit 2); chữ tiếng Việt dựng ở
  daemon (`service.login_error_text` → `status.login.message`, nêu tên tài khoản cho `super_not_member`/`not_ws_admin`/`device_limit`).
- Trạng thái (status.json của binary): `roots[].kind`, `roots[].server_name` (tên SỐNG, `null` tới lần đồng bộ đầu; đổi tên workspace không có
  sự kiện feed → cập nhật theo lần làm mới token ~15 phút — bí danh chịu được tên cũ/null, tên cfharness luôn khớp), `roots[].unpushable[{path,
  kind}]` (kind `root_files` → `folders[].root_files` "tệp ở gốc workspace không đồng bộ"; kind khác → `folders[].unpushable`). Tài khoản: cột 5
  của dòng `ROOT` (rỗng khi server cũ) → `folders[].account` (status không có account). `last_error ∈ {root_revoked, disconnected}`
  hoặc `roots[].state == disconnected` → `folders[].revoked` + trạng thái tổng `revoked` (event `filehub.state`).
- Đổi tên: tên cfharness + `local_path` giữ; `resolve_ref` (`match_ref`) nhận tên sống làm bí danh (tên cfharness khớp trước, tên dài nhất
  thắng, bí danh trùng nhiều thư mục → không khớp). `filehub.list` thêm `kind`, `server_name`, `note`, `layout`. Khối prompt "Shared folder"
  ghi gốc = cả workspace, tên mới, "tạo tệp trong thư mục cấp 1" (tên sống đọc từ status.json — chỉ đổi khi người đổi tên → prompt cache ổn định).
- `filehub.switch_to_workspace` / `cfharness filehub switch-to-workspace` (HUMAN_ONLY, MUTATING, ∈ `HUB_HUMAN_ONLY_CLI`): có run giữ khoá →
  từ chối; đăng nhập chế độ workspace TRƯỚC, chỉ khi thành công mới gỡ thư mục chế độ folder khỏi cấu hình (tệp giữ) và thêm gốc workspace ở
  bản sao mới (không trùng/chứa bản cũ — trùng tên thì thêm hậu tố ` workspace`); server không cấp gốc workspace → cấu hình cũ giữ nguyên.
  `login.switched_from` (RAM) → thẻ/CLI nhắc xoá bản cũ + thu hồi thiết bị cũ.
- Link gốc workspace: `<server>/files` (trang Tệp của workspace hiện tại — route riêng theo ws chưa có ở T6); tệp ở gốc workspace: không có link.

Lệch plan/hợp đồng (H7):
- Plan §4 "Chuyển chế độ" gỡ root cũ TRƯỚC khi đăng nhập; làm SAU khi đăng nhập thành công (lỗi đăng nhập không làm mất thư mục chung đang chạy).
- `cfharness filehub rename` (đổi tên cục bộ) đã làm 2026-11-17 — § "Đổi tên cfharness".
- `filehub.sync_min_version` giữ nguyên: headless S5 vẫn báo `--version` 0.1.1 (phiên bản workspace = phiên bản app desktop, chưa phát hành) — hub nhận diện bản mới theo tính năng (binary cũ + chế độ workspace → "chưa hỗ trợ").

Test: `test_filehub_supervisor.py` (workspace 5 cột + `--root-kind`, 4 cột / account rỗng, binary cũ 3 cột, relay state một lần/hết hạn/lỗi/code chèn dòng, switch, remote setup),
`test_filehub_workspace.py` (`filehub:<ws>/<cấp 1>/…` trong W, bí danh tên mới, prompt, revoked), `test_web_filehub.py` (thao tác qua link public,
bước 2, relay end-to-end với binary giả, callback sai/dùng lại/XSS/giới hạn tốc độ, loopback trên máy hub), `test_sec_auth.py`,
`test_manager_security.py`, `test_guard_classifier.py::test_filehub_cli_human_only`.

## Nghiệm thu thật gốc workspace (e2e 3 repo, 2026-11-16)

Server T6 (worktree `feat/sync-ws-root`, Postgres/MinIO/clamd dev, DB riêng) + binary S5 thật + cfharness H7 (HUB_HOME tạm, `workspace.confine`
bật). Đạt: đăng nhập workspace qua loopback (CLI) và relay (`filehub.login{relay}` → `/filehub/oauth/cb` của web, state sai/lặp → 400), thư mục
cấp 1 hai chiều < 1 s, đổi tên workspace (đường dẫn giữ, `server_name` + bí danh `filehub:<tên mới>`; không có dòng feed → tên mới tới khi
làm mới token, ≤ 15 phút), tệp ở gốc → `root_files`, `not_ws_admin`/`super_not_member`, token sang ws khác → 403 `root_not_granted`, admin 2 ws
chọn một, chế độ một thư mục cũ. Hai lỗi đã sửa:
1. **Gỡ admin rồi cấp lại không chạy tiếp** (S5): engine ngắt gốc workspace như root thư mục (đổi tên `<tên> (đã ngắt đồng bộ)`) → `local_path`
   biến mất, cấp lại admin không có tác dụng. Đã chốt (T6): cấp lại admin → chạy tiếp KHÔNG cần đăng nhập lại. Sửa ở tang-thu-sync: gốc workspace
   mất admin = tạm thời (`last_error=root_revoked`, `roots[].state=error`, không đổi gì trên máy, hỏi lại mỗi 30 s). Hub: `folders[].revoked_temporary`
   → thẻ/CLI nói "cấp lại quyền admin là tự chạy tiếp, không cần đăng nhập lại" (root đã ngắt — rời/xoá workspace, binary cũ — vẫn "đăng nhập lại").
2. **Lỗi trang đồng ý đổ cho tài khoản hub đang dùng**: `not_ws_admin`/`super_not_member`/`device_limit` chỉ có mã, không biết tài khoản nào vừa
   đồng ý — chữ cũ "Tài khoản «<tài khoản hub>» không phải Workspace Admin" sai đúng lúc người đăng nhập nhầm tài khoản. Nay: "Tài khoản bạn vừa
   dùng ở trang đồng ý …" + "Hub hiện đăng nhập bằng «x»" làm ngữ cảnh.

Vòng 2 (cùng ngày): `switch-to-workspace` đạt — có run giữ khoá gốc (run claude GIẢ `tests/fake_claude.py` "hang") → từ chối, exit 1; đồng ý lỗi
(`not_ws_admin`) / huỷ (`access_denied`) → cấu hình + đồng bộ thư mục cũ giữ nguyên; thành công → thư mục cũ gỡ khỏi cấu hình, tệp giữ, gốc workspace
đồng bộ. Hai lỗi đã sửa:
3. **Bản sao cũ vẫn đồng bộ sau khi gỡ** (cả `filehub remove`): hub chỉ sửa cấu hình, hồ sơ của binary còn root → binary đồng bộ hai chiều bản cũ
   song song với gốc workspace (thư mục cũ nằm trong workspace → hai bản trên máy). Nay supervisor truyền `--remove-root <id,…>` cho root có trong
   `status.roots` mà không còn trong `filehub.folders` (`FilehubService.stale_roots`, binary S5 mới).
4. **Workspace bị xoá báo như mất quyền**: binary S5 nay báo `last_error=workspace_deleted` (khi server nói rõ — DELETE phát WebSocket
   `root_revoked workspace_deleted`); hub: `folders[].workspace_deleted` + `disconnected_path` (bản trên máy đổi tên `<tên> (đã ngắt đồng bộ)`) →
   thẻ/CLI "workspace đã bị xoá — gỡ khỏi hub" (không bảo đăng nhập lại). Đặt `status='deleting'` thẳng trong DB (không qua DELETE) không phát
   WebSocket → binary chỉ biết ở lượt feed kế tiếp (feed 404 → `disconnected` chung).
