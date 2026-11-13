# Nhiều hub trên một máy — ghi chú thi công

Kế hoạch gốc + quyết định chủ repo: `docs/dev-notes/plan-multi-instance.md` (§2.1, §4/P1, §6b — mỗi người một user macOS, mỗi
user một hub, dùng chung một tài khoản `claude`). File này ghi phần ĐÃ LÀM. P2 (giam agent trong workspace), P3 (web public,
LaunchDaemon, token OAuth claude), Q6 (tổng quan admin) làm ở nhánh khác.

## P1 — đã làm

Test: `tests/test_multi_instance.py` (22 ca, không gọi `claude`, không cần daemon). Không migration, không RPC mới.

### 1. Quyền file của HUB_HOME
- `HubPaths.ensure()` đặt HUB_HOME và `secrets/` 0700 (chmod lỗi → log cảnh báo, không dừng).
- Daemon (`daemon.run_foreground`) gọi `oscompat.set_private_umask()` → umask 0o077: `hub.db`, `logs/`, `runs/`, `prompts/`… daemon
  tạo ra chỉ user này đọc được. **Tiến trình con** spawn qua `oscompat.new_group_kwargs` (claude của run) nhận lại umask GỐC
  (`Popen(umask=…)`) — file agent tạo trong workspace có quyền như người tự tạo (không bất ngờ thành 0600 trong repo).
- Cài đặt cũ (HUB_HOME 0755, hub.db 0644): `HubPaths.tighten_permissions()` lúc daemon khởi động bỏ quyền group/other của
  HUB_HOME + các mục cấp 1 trong `paths.PRIVATE_ENTRIES` (không đệ quy — HUB_HOME 0700 đã chặn đi vào). Bỏ qua symlink/mục của user
  khác; lỗi chmod chỉ cảnh báo; daemon log danh sách mục đã siết.
- In-process (test, `Daemon(...)` trực tiếp) KHÔNG đổi umask của tiến trình gọi — chỉ `run_foreground`.

### 1b. Một daemon cho mỗi HUB_HOME (khoá `daemon.lock`)
- Sự cố thật khi tạo hub `lamlinh` (2026-11-10): LaunchDaemon vừa bật daemon, chưa kịp mở socket thì `cfharness doctor` thấy socket
  chưa trả lời → tự khởi động bản thứ hai. `serve()` chỉ kiểm socket SỐNG nên cả hai cùng chạy, cùng ghi `hub.db`.
- `daemon.run_foreground` lấy khoá độc quyền `HUB_HOME/daemon.lock` (0600; `flock` / `msvcrt.locking`) TRƯỚC khi mở hub, giữ suốt
  đời tiến trình (hệ điều hành tự nhả khi chết, kể cả SIGKILL). Không lấy được sau 3 s → thoát mã 1 (launchd `KeepAlive
  SuccessfulExit=false` thử lại sau `ThrottleInterval`). Chờ 3 s để một lần dò trùng lúc không làm daemon thật bỏ cuộc.
- `spawn_background` (tự khởi động của CLI/MCP/web, `daemon start`): khoá đang bị giữ → CHỜ bản đang khởi động trả lời `hub.ping`,
  không bật thêm; bản mình bật thua khoá → cũng chờ bản thắng. Trả pid của daemon ĐANG phục vụ.
- Test: `tests/test_multi_instance.py::test_daemon_lock_*`.

### 2. Socket dự phòng + kiểm chủ socket
- Khi `HUB_HOME/hub.sock` quá dài cho AF_UNIX: trước `/tmp/cfharness-<uid>-<hash>.sock` (đoán được, nằm thẳng trong /tmp dùng
  chung) → nay `/tmp/cfharness-<uid>/cfharness-<hash>.sock`. Thư mục tạo 0700, kiểm chủ = user hiện tại, không symlink
  (`paths.ensure_socket_dir`, daemon gọi trước khi sinh token; không an toàn → `UnsafeSocketDir`, daemon không khởi động, thông báo
  bảo xoá hoặc đặt `CFHARNESS_SOCKET`). Quyền rộng hơn 0700 → tự siết.
- **Lệch đề xuất "ưu tiên `$TMPDIR` trên macOS"**: không dùng. `$TMPDIR` khác nhau giữa phiên GUI / SSH / LaunchDaemon (P3 chạy
  daemon bằng LaunchDaemon, người dùng vào bằng SSH) nên client và daemon có thể tính ra hai đường dẫn khác nhau; thư mục
  `/var/folders/…/T` còn bị macOS dọn định kỳ. Đường dẫn `/tmp/cfharness-<uid>/` xác định theo uid; kẻ chiếm trước chỉ gây từ
  chối dịch vụ (bị phát hiện), không lấy được token.
- Tên file giữ tiền tố `cfharness-` (regex guard `HUB_SOCKET_RE`, `names.HUB_SOCKET_PREFIXES` — tên cũ vẫn nhận). Thêm vào regex:
  `/tmp/cfharness-<số>` (thư mục) → agent nhắc tới là forbidden; sandbox cấm ghi cả thư mục (`paths.socket_private_dirs`, dùng ở
  `guard/runtime.py` hai chỗ cấm ghi).
- Client: `rpc.with_auth` (dùng chung `RpcClient` + `tui.client.AsyncHubClient` → CLI, TUI, web, MCP) khi có token người thì
  `rpc.check_socket_owner`: `lstat` (không theo symlink), đòi `S_ISSOCK` và `st_uid == getuid()`; sai → `UntrustedSocket`
  (lớp con `RpcError`, mã hệ thống) với thông báo tiếng Việt, KHÔNG ghi gì lên kết nối. Windows bỏ qua (file endpoint TCP — daemon
  kiểm cùng user phía nó).

### 3. Cổng MCP HTTP
- Mặc định `mcp_server.http_port` = **8766** (`config.MCP_HTTP_DEFAULT_PORT`), web giữ 8765. `mcp serve --http`: cổng bận → lỗi
  tiếng Việt kèm lệnh đổi cổng, mã thoát 1 (dùng lại `web.cli._port_busy`). `config set` kiểm cổng là số nguyên 1–65535.
- Guard (`is_hub_mcp_config`) nhận URL loopback cổng cấu hình **và** cổng cũ 8765 là MCP của chính hub (cài đặt cũ có thể đã
  đăng ký URL đó vào registry) — nhận rộng hơn = an toàn hơn.
- Cài đặt cũ có `mcp_server.http_port: 8765` đặt RÕ trong config.yaml vẫn giữ 8765 → `doctor multi` báo trùng cổng web.

### 4. Cookie web theo hub
- Tên cookie = `ahub_web_<6 hex sha256(HUB_HOME)>` (`web.security.cookie_name`, `WebState.cookie`). Hai hub trên
  `cfharness.localhost` khác cổng không còn đá phiên nhau. Khoá phiên ngoài cookie (`k`, localStorage theo origin có cổng) giữ
  nguyên. Đổi tên cookie = mọi phiên web đang mở phải đăng nhập lại một lần sau khi cập nhật.

### 5. Callback OAuth của `mcp login`
- `mcp_client.oauth_callback_port` (mặc định 33418, 0 = OS chọn); `--port` thắng config. Giữ mặc định CỐ ĐỊNH vì `redirect_uri`
  ổn định → bản đăng ký DCR dùng lại được (`docs/dev-notes/mcp-oauth.md` §6); cổng bận vẫn tự rơi về cổng ngẫu nhiên như trước.
  Hai hub cùng user OS chạy `mcp login` đồng thời: đặt cổng khác cho hub thứ hai.

### 6. `CFHARNESS_EXTRA_WORKSPACE_ROOTS`
- Chỉ có hiệu lực khi `CFHARNESS_TESTING=1` (`workspace.env_extra_roots`, dùng ở `validate_workspace` và
  `guard/profiles.allowed_roots`). `tests/conftest.py` đặt cờ ở mức module → daemon tiến trình con của test kế thừa.

### 7. `cfharness doctor multi`
- `doctor_multi.py`, chỉ đọc, không cần daemon: phiên bản, chủ + quyền HUB_HOME và mục nhạy cảm, socket (vị trí, chủ, là socket
  thật, thư mục dự phòng), web (đang chạy/cổng bị tiến trình khác chiếm), cookie theo hub, cổng MCP (khác web, bận), cổng callback
  OAuth, bot Telegram/Slack (chỉ "đã cấu hình" + vân tay sha256 8 ký tự — hai hub cùng vân tay = dùng chung bot, C7; KHÔNG in token),
  `workspace.allowed_roots` (cảnh báo gốc rộng `~`/`~/Workspace`), biến `CFHARNESS_EXTRA_WORKSPACE_ROOTS` bị bỏ qua. Mã thoát 0/1,
  `--json` có. `multi` thêm vào `agents.RESERVED_IDS` (`cfharness doctor <agent>` vẫn như cũ).

### Việc còn lại / giới hạn
- Bot trùng giữa hai hub của HAI user OS không tự phát hiện được (không đọc chéo home) — so vân tay bằng mắt hoặc qua tổng quan
  admin (Q6).
- `tighten_permissions` không đệ quy: file 0644 cũ bên trong `runs/`… vẫn 0644 nhưng không ai khác vào được vì HUB_HOME 0700.
- C4 (update ghi đè venv chung user) và C5 (đăng nhập claude theo user) không thuộc P1 — đã giải bằng mô hình mỗi người một user.
