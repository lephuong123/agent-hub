# App desktop cfharness — hỗ trợ phía hub

Ngày: 2026-11-15. Nhánh `feat/desktop-app-hub`. Không migration, không RPC mới. Plan của app (repo `cfharness-desktop`): `docs/PLAN.md`
mục 3, 5, 6 (H1–H6). Test: `tests/test_desktop_hub.py` (+ harness node `tests/js/voice_native_harness.js`).

App desktop (Tauri) là vỏ native hiển thị CHÍNH giao diện web của từng hub trong WebView — không viết lại UI. Phía hub chỉ thêm vài
điểm nối nhỏ, mỗi điểm theo đúng hợp đồng chung dưới đây (cả hai repo phải làm khớp từng chữ).

## Hợp đồng chung hub ↔ app

| # | Điểm nối | Trạng thái |
|---|---|---|
| C1 | `cfharness web --json [--ensure] [--port N]` | **xong (PR phần 1)** |
| C2 | `GET /.well-known/cfharness.json` công khai | **xong (PR phần 1)** |
| C3 | `GET /app/badge` cần phiên | **xong (PR phần 1)** |
| C4 | `voice.js` dùng `window.cfhNative.speech` trước Web Speech API | **xong (PR phần 1)** |
| C5 | Directory `/.well-known/cfharness-directory?t=` + `cfharness admin directory …` + SPEC admin_overview schema 3 | phần 2 |
| C6 | Sandbox/permissions/classifier chặn thư mục data của app | phần 2 |
| C7 | User-Agent ` CfharnessDesktop/<ver>` → chỉ log | **xong (PR phần 1)** |

### C1 — `cfharness web --json [--ensure] [--port N]`
`CFHARNESS_HOME` chọn hub. In ĐÚNG MỘT dòng JSON ra stdout:

```json
{"ok":true,"origin":"http://cfharness.localhost:8711","login_url":"http://cfharness.localhost:8711/login?t=<một lần>",
 "hub_id":"<16 hex = admin_overview.hub_id(home)>","version":"0.2.10","name":"<web.display_name | tên thư mục HUB_HOME>","started":false}
```

Lỗi: `{"ok":false,"error":"<mã>","message":"<tiếng Việt>"}` + mã thoát 1. Mã lỗi: `web_not_running` (không `--ensure`, chưa có server),
`web_not_responding` (pid còn sống mà không trả lời), `port_busy` (cổng chỉ định bận), `no_free_port`, `web_start_failed`, `bad_port`,
`bad_host`, `bad_host_name`.

- "Server đang chạy" = `HUB_HOME/auth/web.json` hợp lệ (pid sống, file của mình) VÀ server trả lời `/.well-known/cfharness.json` với Host
  đúng tên riêng (`desktop.probe`).
- `--ensure`: chưa có server → spawn `python -m cfharness web --no-link --host … --port … --host-name …` TÁCH tiến trình (POSIX
  `start_new_session`; Windows không fork: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`), stdin = DEVNULL, stdout/stderr nối vào
  `HUB_HOME/logs/web.log` (0600). Chờ tối đa `--wait` (mặc định 30 s) tới khi server trả lời rồi in JSON (`started: true` khi chính lần
  gọi này khởi động server). Tiến trình con tự khởi động daemon nếu chưa chạy (hành vi sẵn có của `cfharness web`).
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

## Việc còn lại (PR phần 2)
- C5 directory (route token + `cfharness admin directory export|token|status`, SPEC admin_overview schema 3 với `public_origin` + `display_name`).
- C6 chặn thư mục data của app (`~/Library/Application Support/io.cfharness.desktop`, `%APPDATA%\io.cfharness.desktop`) ở sandbox denyRead/denyWrite,
  `permissions.deny` và classifier.
- Nhánh Windows của `--ensure` chưa chạy thật trên máy Windows (chỉ có code + cờ `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`).
