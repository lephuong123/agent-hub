# App desktop cfharness — hỗ trợ phía hub

Ngày: 2026-11-15. Nhánh `feat/desktop-app-hub`. Không migration. Plan của app (repo `cfharness-desktop`): `docs/PLAN.md`
mục 3, 5, 6 (H1–H6). Test: `tests/test_desktop_hub.py` (+ harness node `tests/js/voice_native_harness.js`), `tests/test_desktop_directory.py` (C5, C6).

App desktop (Tauri) là vỏ native hiển thị CHÍNH giao diện web của từng hub trong WebView — không viết lại UI. Phía hub chỉ thêm vài
điểm nối nhỏ, mỗi điểm theo đúng hợp đồng chung dưới đây (cả hai repo phải làm khớp từng chữ).

## Hợp đồng chung hub ↔ app

| # | Điểm nối | Trạng thái |
|---|---|---|
| C1 | `cfharness web --json [--ensure] [--port N]` | **xong (PR phần 1)** |
| C2 | `GET /.well-known/cfharness.json` công khai | **xong (PR phần 1)** |
| C3 | `GET /app/badge` cần phiên | **xong (PR phần 1)** |
| C4 | `voice.js` dùng `window.cfhNative.speech` trước Web Speech API | **xong (PR phần 1)** |
| C5 | Directory `/.well-known/cfharness-directory?t=` + `cfharness admin directory …` + SPEC admin_overview schema 3 | **xong (PR phần 2)** |
| C6 | Sandbox/permissions/classifier chặn thư mục data của app | **xong (PR phần 2)** |
| C7 | User-Agent ` CfharnessDesktop/<ver>` → chỉ log | **xong (PR phần 1)** |

### C1 — `cfharness web --json [--ensure] [--port N]`
`CFHARNESS_HOME` chọn hub. In ĐÚNG MỘT dòng JSON ra stdout:

```json
{"ok":true,"origin":"http://cfharness.localhost:8711","login_url":"http://cfharness.localhost:8711/login?t=<một lần>",
 "hub_id":"<16 hex = admin_overview.hub_id(home)>","version":"0.2.10","name":"<web.display_name | tên thư mục HUB_HOME>","started":false}
```

Lỗi: `{"ok":false,"error":"<mã>","message":"<tiếng Việt>"}` + mã thoát 1. Mã lỗi: `web_not_running` (không `--ensure`, chưa có server),
`web_not_responding` (pid còn sống mà không trả lời), `port_busy` (cổng chỉ định bận), `no_free_port`, `web_start_failed`, `bad_port`,
`bad_host`, `bad_host_name`, `web_busy` (một lần `--ensure` khác giữ khoá quá `--wait` mà vẫn chưa có server trả lời).

- "Server đang chạy" = `HUB_HOME/auth/web.json` hợp lệ (pid sống, file của mình) VÀ server trả lời `/.well-known/cfharness.json` với Host
  đúng tên riêng (`desktop.probe`).
- `--ensure`: chưa có server → spawn `python -m cfharness web --no-link --host … --port … --host-name …` TÁCH tiến trình (POSIX
  `start_new_session`; Windows không fork: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`), stdin = DEVNULL, stdout/stderr nối vào
  `HUB_HOME/logs/web.log` (0600). Chờ tối đa `--wait` (mặc định 30 s) tới khi server trả lời rồi in JSON (`started: true` khi chính lần
  gọi này khởi động server). Tiến trình con tự khởi động daemon nếu chưa chạy (hành vi sẵn có của `cfharness web`).
- Hai `--ensure` cùng lúc (app mở + người gõ lệnh): khoá độc quyền `HUB_HOME/auth/web.ensure.lock` (flock / msvcrt, 0600, OS tự nhả khi tiến
  trình chết) từ lúc đọc lại state tới khi server sẵn sàng → chỉ MỘT server được bật, lần kia chờ khoá rồi dùng server đó (`started:false`).
- Nhận diện "server do chính lần này bật" bằng NONCE (env `CFHARNESS_WEB_NONCE` → tiến trình web ghi vào `auth/web.json`, gỡ khỏi env của nó),
  KHÔNG bằng pid: trên Windows `sys.executable` của venv/uv tool là launcher, python thật là tiến trình con khác pid. `wait_ready(nonce=)` chỉ
  tin state đúng nonce (state của server khác mang khoá ký khác → link đăng nhập hỏng) — state lạ chỉ được nhận sau khi tiến trình của mình đã
  thoát (thua cuộc bind cổng) và server kia trả lời. Nonce không phải bí mật, không bao giờ in ra JSON.
- Cổng: `--port` → `web.port` → cổng TẤT ĐỊNH theo hub: home mặc định (`~/.cfharness`) = 8765; hub khác = `8770 + hub_id mod 1000`, bận thì
  dò tiếp trong dải (bỏ 8766 = MCP HTTP). Cổng chỉ định (`--port`/`web.port`) mà bận → `port_busy` (không tự đổi).
- `login_url` = link đăng nhập MỘT LẦN (HMAC, hạn 300 s), giống `cfharness web --url`. KHÔNG BAO GIỜ in token người (`<socket>.auth`) hay
  khoá ký (`auth/web.json`). `--no-link` ở tiến trình con → link không vào `web.log`.
- Không `--json` mà có `--ensure` → cùng luồng, in chữ cho người (`--open` mở trình duyệt).
- Không `--json`, không `--ensure` → hành vi cũ giữ nguyên (foreground, `--port` mặc định 8765).
- Cấu hình mới (nhánh `web` ∈ `integrity.SECURITY_KEYS`, kiểm ở `web/public.validate_setting`):
  `web.port` (null | số nguyên 1024..65535), `web.display_name` (null | chuỗi một dòng 1..60, không ký tự điều khiển/bidi/vô hình).
  CLI đọc nhánh `web` thẳng từ `config.yaml` (chỉ đọc, không cần daemon) để lỗi `web_not_running` không phải bật daemon.
- Agent không chạy được: `cfharness web …` (mọi biến thể, kể cả `--json --ensure`) đã forbidden trong classifier (`HUB_HUMAN_ONLY_CLI`).

### C2 — `GET /.well-known/cfharness.json`
Công khai (trong `GuardMiddleware.PUBLIC`), `Cache-Control: no-store`, đúng `{"kind":"cfharness.hub","schema":1,"app_api":1}` — không phiên bản,
không tên, không gì khác. Kiểm Host vẫn áp dụng (Host lạ → 421). App dùng nó để xác nhận một origin là hub cfharness; `desktop.probe` của
CLI cũng dùng nó để biết server đã sẵn sàng.

### C3 — `GET /app/badge`
Cần phiên. Tiền tố `/app/` nằm trong `GuardMiddleware.API` → GET không gia hạn phiên (như `/office/state`); chưa đăng nhập → 401 JSON
`{"error":"unauthenticated"}` (riêng `/app/`, đường API khác giữ nguyên chữ thuần). Trả:

```json
{"approvals":0,"questions":0,"running":0,"estop":false,"version":"0.2.10"}
```

- `approvals` = approval chưa quyết (`approval.list{status:pending}`) KHÔNG giao điều phối viên (`routed_to != "manager"`).
- `questions` = câu hỏi đang chờ người (`question.list{status:open}`).
- `running` = `hub.status.running`; `estop` = ESTOP hub hoặc ESTOP admin (`estop.get`).
- `desktop.badge_from` là hàm THUẦN danh sách trắng — không bao giờ chữ tự do. Daemon không trả lời → 503 `{"error":"daemon_unavailable"}`.

### C4 — cầu nối giọng nói `window.cfhNative.speech`
`voice.js` dò cầu nối TRƯỚC Web Speech API. Cầu nối hợp lệ khi có đủ `available/start/stop/on`; `available()` (Promise) quyết định trước khi gắn
nút (`boot` chờ). `true` → dùng `NativeListener`, ngược lại dùng `SpeechRecognition` như cũ (không có cả hai → ẩn nút như cũ).

- `start({lang, interim:true})`, `stop()`, `on("result" {text, final}, "end" {}, "error" {code})`. Cầu nối không có `off` → đăng ký MỘT lần,
  chuyển sự kiện cho phiên đang chạy (`nativeCur`); start/stop nối tiếp (`nativeChain`). Sự kiện muộn sau khi dừng bị bỏ.
- `NativeListener` cùng giao diện với `Listener` (onText(finals, interim), onEnd("stopped"|"error", code), onSpeech) → đọc chép và hội thoại
  dùng CHUNG luồng: cùng sự kiện `mgr:*`, cùng `voice=dictation|conversation` (daemon không bao giờ duyệt/chốt từ tin giọng nói).
- "end" khi chưa dừng → tự `start` lại (như Chrome tự kết thúc), trần 20 lần. "error" `no-speech`/`aborted` bị bỏ qua như Web Speech.
- Lỗi quyền (`not-allowed`) trong app → hướng dẫn mở Cài đặt hệ thống (thay cho câu "ổ khoá trên thanh địa chỉ").
- CSP/Permissions-Policy không đổi; không `innerHTML`/`eval` (test tĩnh giữ nguyên). App chỉ tiêm `window.cfhNative` vào WebView của origin đã đăng ký.

### C7 — nhận diện app
`GuardMiddleware`: User-Agent chứa `CfharnessDesktop/<ver>` (`[0-9A-Za-z.+-]{1,32}`) → `log.info` MỘT lần mỗi phiên bản (tối đa 64 phiên bản/tiến
trình). Không đổi giao diện, không lưu, không đưa vào template.

### C5 — directory hub (hub của người quản trị máy)
Mục đích: app nhập MỘT lần danh sách hub có origin công khai trên máy (thay vì người dùng gõ từng URL). Danh mục KHÔNG cấp quyền gì — mỗi hub
vẫn đòi đăng nhập riêng.

```json
{"kind":"cfharness.directory","schema":1,"name":"<một dòng ≤60>","generated_at":"2026-11-15T08:00:00Z",
 "hubs":[{"id":"<hex ≤16>","name":"<≤60>","user":"<≤32>","origin":"https://host[:port]"}]}
```

- **Nguồn**: file trạng thái `admin_overview` (thư mục chung, bộ đọc `read_dir` — mở `O_NOFOLLOW`, kiểm chủ file, lọc qua `SPEC`). SPEC lên
  **schema 3**: thêm `public_origin` (kiểu `origin`: phải đúng dạng chuẩn của `web/public.parse_public_origin` — https, tên miền, không IP/path/
  userinfo; bản "gần đúng" như chữ hoa bị BỎ, bộ đọc không sửa hộ file lạ) và `display_name` (kiểu `name`: `termsafe.one_line` ≤ 60 — bỏ ANSI,
  điều khiển, bidi, zero-width). Bộ xuất lấy từ `web.public_origin` (CHUẨN HOÁ bằng `parse_public_origin` trước khi xuất — config lưu chuỗi
  người gõ, vd `https://An.Example.com/` → `https://an.example.com`) / `web.display_name` (người đặt, nhánh `web` ∈ SECURITY_KEYS). Bộ đọc nhận
  schema 1, 2, 3. Bộ xuất ghi `schema: 3` CHỈ khi có ít nhất một trong hai trường mới; không có → `schema: 2` để hub admin chưa nâng cấp
  (`SCHEMAS_READ = (1, 2)`) vẫn đọc được. Hub đặt origin/tên mà admin chưa nâng cấp → admin thấy `unsupported_schema` (Lưu ý khi cập nhật).
- **Lọc mục** (`admin_directory.entry`, thuần): chỉ hub đọc được (ok/stale/stopped) có `public_origin`; id hex ≤ 16, user khớp `USER_RE` và
  ≤ 32, name một dòng ≤ 60 (thiếu → user), origin kiểm lại; trùng origin → bỏ mục sau; ≤ 200 mục. Đúng 4 khoá mỗi mục, không gì khác. Route
  web lọc lại lần nữa bằng `document()` (phòng daemon khác phiên bản).
- **Token**: `secrets.token_urlsafe(32)`; `HUB_HOME/auth/directory.json` (0600, ghi nguyên tử) chỉ giữ `{sha256, created_at, rev}`. So khớp
  `hmac.compare_digest` trên băm; token sai dạng vẫn tốn cùng lượng việc. Token chỉ hiện MỘT lần (`token` khi chưa có; có rồi → `Conflict
  token_exists`, phải `--rotate` — token cũ mất hiệu lực ngay). URL = `web.public_origin` của hub admin + đường dẫn; chưa có public_origin →
  `http://cfharness.localhost:<cổng>` kèm cảnh báo "chỉ dùng trên máy này".
- **RPC** (`admin_directory.register_rpc`, gọi cuối `admin_rpc.register_rpc`): `admin.directory` (xuất), `admin.directory_status`,
  `admin.directory_token{rotate}` (GHI — `MUTATING_DIRECTORY`, bọc audit; audit chỉ ghi `rotate` + `_result_brief` nên URL/token không vào
  audit), `admin.directory_serve{token}` (web gọi; token sai/chưa tạo/hub không thuộc nhóm admin → `NotFound`). Tất cả ∈ HUMAN_ONLY, không ở
  MANAGER_ALLOWED, đều `require_admin` (serve: `is_admin` → NotFound). Đọc không vào audit (token trong params của serve).
- **Route** `GET /.well-known/cfharness-directory?t=<token>` (`web/desktop.directory`): trong `GuardMiddleware.PUBLIC` (không cần phiên; Host vẫn
  kiểm — Host lạ → 421). MỌI lỗi → 404 `{"error":"not_found"}` (không phân biệt token sai / chưa tạo / không phải admin / daemon tắt).
  Giới hạn tốc độ (RAM tiến trình web): `directory_limiter` = `LoginLimiter(10 lần sai/IP, 30 toàn cục / 10 phút → khoá 10 phút; 200/ngày →
  khoá 1 h)` → 429 `{"error":"rate_limited"}` + `Retry-After` (đang khoá thì token đúng cũng chờ); `SuccessWindow` = trần 30 lần trả/IP, 120 toàn
  cục mỗi phút. `Cache-Control: no-store`. Uvicorn chạy `access_log=False` nên `?t=` không vào log.
- **CLI** (`admin_cli.py`): `cfharness admin directory export [--out F]` (file 0600, `O_NOFOLLOW`), `token [--rotate]`, `status`. Cả nhánh
  `cfharness admin` đã forbidden với agent (`HUB_HUMAN_ONLY_CLI["admin"] = None`).
- **Cấu hình**: `admin_directory.name` (null | một dòng 1..60, không điều khiển/vô hình) — nhánh `admin_directory` ∈ `integrity.SECURITY_KEYS`,
  kiểm ở `admin_directory.validate_setting`.

### C6 — chặn thư mục dữ liệu của app
App chạy cùng user OS với agent của hub local; thư mục data giữ cookie phiên WebView của MỌI hub + token directory. `desktop_app.app_data_dirs()`
(thuần theo home/env, liệt kê mọi nền tảng — thừa vô hại):
- macOS: `~/Library/Application Support/io.cfharness.desktop` (Tauri app_data_dir) + `~/Library/WebKit/…`, `~/Library/HTTPStorages/…`,
  `~/Library/Caches/…` (nơi WKWebView giữ cookie/bộ nhớ theo bundle id — rộng hơn hợp đồng, cần vì cookie phiên nằm ở đây).
  Bản dev KHÔNG đóng gói (`npm run tauri dev`) không có bundle id → WebKit lấy tên binary: thêm `~/Library/WebKit/cfharness-desktop`,
  `~/Library/HTTPStorages/cfharness-desktop`, `~/Library/Caches/cfharness-desktop` (`desktop_app.DEV_NAME`; giữ data store theo hub = cookie
  phiên hub thật). Chỉ đúng các đường dẫn Library đó — chuỗi `cfharness-desktop` đứng một mình (repo `~/Workspace/cfharness-desktop`) KHÔNG bị chặn;
  Bash tách đôi chỉ tính khi có cả tên dev và gốc `Library/{WebKit,HTTPStorages,Caches}`.
- Windows: `%APPDATA%\io.cfharness.desktop` + `%LOCALAPPDATA%\io.cfharness.desktop` (WebView2 `EBWebView`); thiếu biến → `~/AppData/{Roaming,Local}`.
- Linux: `~/.local/share/…` (+ `$XDG_DATA_HOME`), `~/.cache/…`, `~/.config/…`.

Ba lớp: (1) sandbox `denyRead` + `denyWrite` (`guard/runtime.sandbox_settings`) và `fixed_protected_dirs` (cwd trong đó không gỡ chặn ghi,
workspace không được nằm trong/chứa); (2) `permissions.deny` Read/Grep/Glob/Edit/Write/MultiEdit/NotebookEdit (`app_data_deny_rules`) cho MỌI hub
kể cả trust_full; (3) classifier rule `desktop_app_data` (forbidden, klass `hub`, ∈ `NEVER_DOWNGRADE_RULES`): tool đọc (path/file_path, Glob pattern
tuyệt đối), tool ghi, Bash (`desktop_app.shell_mentions`: dạng đường dẫn `Application Support/…`, `%APPDATA%\…`, `$env:LOCALAPPDATA\…`,
`Library/WebKit/…`, `.local/share/…`, hoặc identifier + gốc thư mục app cùng xuất hiện như `cd ~/Library/Application\ Support && cat io.cfharness.desktop/…`).
So sánh không phân biệt hoa/thường (APFS/NTFS) + realpath. KHÔNG chặn identifier đứng một mình (agent làm việc trên repo app grep `tauri.conf.json`).
Glob (không bị sandbox OS bọc; `permissions.deny` Glob chỉ khớp `path`): mẫu nối vào `path`/cwd nếu tương đối, cắt ở ký tự đại diện ĐẦU TIÊN bất kỳ
(`* ? [ {`), rồi `desktop_app.glob_may_touch` so từng thành phần với mỗi thư mục app bằng fnmatch (`{…}` coi là `*`, `**` trước khi hết đường dẫn
thư mục app = khớp) → chặn cả `…/io.cfharness.deskto?/**`, `…/[i]o.cfharness.desktop/*`, `~/Library/*/io.cfharness.desktop/*`; liệt kê tên ở thư mục
cấp trên (`~/Library/Application Support/*`) không bị chặn.
**Trust full không gỡ** rule `desktop_app_data` (cùng `admin_shared`, `other_hub_write` — `guard/service.TRUST_NEVER_RULES`): agent trust full chạy
Bash không sandbox OS, và thư mục app cùng chủ OS với agent nên không có quyền file nào chặn hộ — classifier là lớp chặn Bash duy nhất.

## Sửa theo review (phần 3)
- **trust_full + Bash** đọc được thư mục app (classifier ghi nhận rồi `_allow_trusted` vẫn cho) → `TRUST_NEVER_RULES` = `other_hub_write`,
  `admin_shared`, `desktop_app_data`: trust full vẫn deny + `guard.blocked`. Không bật lại sandbox tối thiểu cho trust_full (sandbox Claude bật là
  chặn ghi ngoài cwd/mạng — trái nghĩa trust full); classifier đủ cho đường dẫn viết trong lệnh.
- **origin chưa chuẩn** (`https://An.Example.com/`) bị bỏ im lặng khỏi danh mục → `collect` xuất dạng chuẩn.
- **schema 3 cho mọi hub** làm admin cũ báo `unsupported_schema` → chỉ ghi 3 khi có trường mới, kèm "Lưu ý khi cập nhật" trong CHANGELOG.
- **`--ensure` đồng thời** ghi đè `auth/web.json` của nhau / link ký bằng khoá server khác → khoá `auth/web.ensure.lock` + nonce.
- **`started` sai trên Windows** (pid launcher ≠ pid python thật) → so nonce.
- **Glob ký tự đại diện trong tên thư mục app** lọt classifier → cắt ở `* ? [ {` + `glob_may_touch`.
- Test: `tests/test_trust_and_noise.py::test_trust_full_never_grants_desktop_app_data_or_admin_shared` (đỏ trước khi sửa);
  `tests/test_desktop_directory.py` (collect schema 2/3 + origin chưa chuẩn; 5 ca Glob forbidden + 3 ca không chặn nhầm; `glob_may_touch` thuần
  mac/Windows); `tests/test_desktop_hub.py::test_ensure_concurrent_calls_spawn_once` (2 luồng `main([...--ensure])`, spawn giả chậm → spawn 1 lần,
  started true/false, cả hai link ký bằng khoá server đang phục vụ, khoá 0600), `test_ensure_started_by_nonce_not_pid`,
  `test_wait_ready_ignores_foreign_state_while_own_proc_alive`, `test_take_nonce_env`; `tests/test_admin_overview.py` (bản xuất không origin = schema 2).

## Đã làm (PR phần 2)
- `src/cfharness/admin_directory.py` (mới), `src/cfharness/desktop_app.py` (mới).
- `admin_overview.py`: SCHEMA 3, kiểu `origin`/`name`, `collect` thêm `public_origin`/`display_name`.
- `admin_rpc.py` gọi `admin_directory.register_rpc`; `auth.py` HUMAN_ONLY + `all_mutating`; `config.py` DEFAULTS + kiểm; `integrity.SECURITY_KEYS`.
- `admin_cli.py`: `admin directory export|token|status`.
- `web/desktop.py`: route directory, `directory_limiter`, `SuccessWindow`; `web/app.py`: PUBLIC + `WebState.dir_limiter|dir_window`.
- `guard/runtime.py`: `app_data_deny_rules`, sandbox deny, `fixed_protected_dirs`; `guard/classifier.py`: rule `desktop_app_data`.

## Test phần 2 (`tests/test_desktop_directory.py` + ca thêm)
- SPEC schema 3: origin sai dạng (http, IP, path, userinfo, query, `*.localhost`, chưa chuẩn hoá) bị bỏ; tên bỏ ANSI/bidi/zero-width, cắt 60;
  `collect` xuất từ config; schema 1/2 vẫn đọc được.
- Lọc mục thuần: id > 16 / sai hex, user > 32 / nhiều dòng, origin sai → bỏ; khoá lạ không lọt; ≤ 200; tên mặc định.
- Thư mục trạng thái thật (tmp): JSON hỏng, sai chủ (tên file ≠ hub_id), schema lạ, trùng origin, id dài, hub cũ không origin → lọc đúng,
  không chữ tự do (`summary`, chi phí) trong kết quả.
- Token: file 0600 chỉ băm, sai/cắt/thêm ký tự → False, xoay làm token cũ mất hiệu lực; mã nguồn dùng `compare_digest`.
- RPC: không phải admin → bị chặn (cả serve với token đúng → route 404); admin: export/status, token một lần (`token_exists`), `--rotate` +
  URL theo `web.public_origin`, token không có trong `audit_log`.
- Route: chưa có token / thiếu / sai / cắt / sai tên tham số → 404 JSON no-store; token đúng → đúng 5 khoá + 4 khoá mỗi mục, không lộ chữ tự
  do/đường dẫn; Host lạ → 421; 10 lần sai → 429 (token đúng cũng 429 khi đang khoá); `SuccessWindow` trần theo IP/toàn cục.
- Cấu hình `admin_directory.name` hợp lệ/sai; `admin_directory` ∈ SECURITY_KEYS; RPC ∈ HUMAN_ONLY, ∉ MANAGER_ALLOWED; classifier cấm
  `cfharness admin directory …`.
- C6: `app_data_dirs` mac/Windows/Linux (+ tên dev `cfharness-desktop` dưới Library — `test_app_data_dirs_cover_unbundled_dev_run`, ca
  dev trong `DESTRUCTIVE_APP_DATA`/`DESTRUCTIVE_DESKTOP_APP`, ca âm `~/Workspace/cfharness-desktop/src/x.rs`), so không phân biệt hoa/thường; 13 ca forbidden (Read/Grep/Glob/Write/Edit/Bash mac/Windows/Linux, lệnh
  tách `cd … &&`); không chặn nhầm grep identifier / thư mục tên gần giống; `permissions.deny` cả trust_full; sandbox denyRead/denyWrite + vùng cố
  định. Ca thêm: `tests/test_guard_classifier.py::test_desktop_app_data_forbidden`, `tests/test_sec_auth.py::test_desktop_app_data_denied_every_hub`;
  `tests/test_trust_and_noise.py` cập nhật (deny của trust_full = admin_shared + app_data).

## Đã làm (PR phần 1)
- `src/cfharness/web/desktop.py` (mới): route C2/C3, `badge_from`, `note_app_ua`, chọn cổng, spawn tách tiến trình, `probe`, `wait_ready`, `info`.
- `web/app.py`: PUBLIC + API + 401 JSON cho `/app/`, log UA, gắn `desktop.routes()`.
- `web/cli.py`: `--json` (không lệnh con) / `--ensure` → `cmd_web_app`; `--wait`; `--port` mặc định None (luồng cũ đặt lại 8765).
- `config.py` `web.port`, `web.display_name` + kiểm ở `web/public.validate_setting`.
- `web/static/voice.js`: `NATIVE`/`NativeListener`/`makeListener`, `boot` chờ `available()`, `cfhVoice.engine()` cho test.

## Test (`tests/test_desktop_hub.py`)
- C2: đúng 3 khoá, no-store, công khai, Host lạ → 421, đường `.well-known` khác vẫn cần phiên.
- C3: 401 JSON khi chưa đăng nhập; đúng 5 khoá + kiểu; đếm approval/run/ESTOP thật (daemon in-process + FakeRuntime giữ run); không gia hạn phiên
  (đối chứng: trang thường gia hạn); `badge_from` bỏ chữ tự do, không đếm approval giao điều phối viên.
- C7: UA app được log, nội dung trả về không đổi.
- C1: `web_not_running` (một dòng JSON, mã 1, không bật daemon); JSON đúng 7 khoá, `login_url` ký đúng khoá, không lộ khoá ký/token người,
  `display_name`; `port_busy`; cổng tất định theo hub; kiểm hợp lệ `web.port`/`web.display_name`; classifier cấm `cfharness web --json --ensure`.
- C1 e2e: HUB_HOME tạm (runtime fake) → `python -m cfharness web --json --ensure --port <trống>` thật: server tách session (`getsid`), sống sau
  khi CLI thoát, `web.log` 0600 không chứa link; lần 2 `started:false` + link mới; dọn: giết server web + `daemon stop`.
- C4 (node + DOM giả, bỏ qua nếu thiếu node): native → `start({lang:"vi-VN",interim:true})`, chữ tạm/chữ chốt vào ô, `voice=dictation`, dừng gọi
  `stop`, sự kiện muộn bị bỏ; lỗi `not-allowed` → thông báo; `available()=false` / không cầu nối → hành vi cũ (ẩn nút / Web Speech).

## Việc còn lại
- Chưa kiểm sandbox thật (`claude` thật) với đường dẫn có dấu cách `Application Support` trong `denyRead` — chỉ kiểm settings sinh ra.
- Directory chỉ liệt kê hub có `web.public_origin`; hub chỉ chạy cục bộ không có trong danh mục (đúng hợp đồng: origin phải https).
- Giới hạn tốc độ directory chỉ ở RAM tiến trình web (khởi động lại web = xoá khoá), như giới hạn đăng nhập.
- Nhánh Windows của `--ensure` chưa chạy thật trên máy Windows (chỉ có code + cờ `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`).

## Link tải app trên web của hub (2026-11-16)
Thẻ `#desktop-app` ở Cài đặt (`templates/_desktop_app.html`) + link `Tải app máy tính` ở chân thanh bên (`base.html`). Link là HẰNG SỐ
`web/desktop.DOWNLOADS` → `releases/latest/download/Cfharness-Desktop-{macOS.dmg,Windows-setup.exe}` của repo app (release của app
đăng kèm hai tệp tên cố định) + trang hướng dẫn GitHub Pages. Không lấy từ cấu hình. Hiện cả khi mở trong app (C7: UA không đổi giao diện).
Test: `tests/test_desktop_hub.py::test_settings_has_desktop_download_links`.
