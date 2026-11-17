# Release notes — cfharness

Mỗi phiên bản là một tag `vX.Y.Z` (danh sách: [Releases](https://github.com/lephuong123/agent-hub/releases)).
Cập nhật hub đang chạy: `cfharness update` hoặc nút "Cập nhật hệ thống" ở trang Cài đặt web.

Quy tắc: **viết release note ở file này TRƯỚC khi phát hành** (xem mục "Phát hành" trong `AGENTS.md`).
Mục mới nhất ở trên cùng. Thay đổi đã gộp mà chưa có tag nằm ở mục "Chưa phát hành".

---

## Chưa phát hành

### Bảo mật
- **`cfharness selftest --live` — tự kiểm bảo mật với `claude` thật trước khi phát hành**: tạo hub TẠM (daemon riêng, không đụng hub thật),
  cho một agent model rẻ (mặc định haiku) thử 12 thao tác bị cấm — ghi `policy/` (shell, python, tool Write), đọc secret / token người,
  gọi thẳng socket hub, `cfharness approve`, `cfharness config set`, curl dịch vụ loopback, ghi/đọc ngoài workspace khi giam — rồi đo
  hậu quả thật và báo ĐẠT / TRƯỢT / KHÔNG KẾT LUẬN từng ca. Trần chi phí cứng (`--budget-usd`, mặc định $0.50; mỗi run `--per-run-usd`
  $0.10), đạt trần thì dừng ngay và bỏ qua các ca còn lại; xong thì dọn sạch hub tạm. `cfharness selftest --dry-run` chỉ liệt kê ca,
  không tốn tiền. Lệnh chỉ người (agent gõ bị chặn). Hướng dẫn: `docs/dev-notes/selftest.md`.

---

## v0.2.15 — 2026-11-17

Bản sửa lỗi: daemon trên Windows không còn bị "điếc" sau khi một client ngắt kết nối giữa chừng (#124).

### Sửa lỗi
- **Daemon sống mà "điếc" trên Windows — `cfharness chat`/web/CLI không gửi được gì, báo "daemon không chạy"**: khi một client đóng kết
  nối giữa chừng, `accept` lỗi (WinError 64) và asyncio đóng hẳn socket nghe của daemon; tiến trình vẫn chạy nhưng không nhận kết nối
  mới cho tới khi bị giết. Daemon giờ kiểm socket nghe mỗi 2 giây và tự mở lại (ghi cổng mới vào `hub.sock`), client nối lại bình thường (#124).

### Lưu ý khi cập nhật
- Trên Windows, dừng daemon và đóng mọi cửa sổ `cfharness` (TUI, `chat`, `web`) trước khi `uv tool install --force`; nếu còn
  `cfharness.exe` đang chạy, uv báo `Access is denied` khi xoá thư mục `Scripts` và cập nhật thất bại.

---

## v0.2.14 — 2026-11-16

Thư mục dùng chung với Không gian số giờ là **cả workspace** của domain, và đăng nhập được ngay trên web hub kể cả qua link public (#122).

### Thư mục dùng chung với "Không gian số": cả workspace + cài đặt trên web
- Thư mục chung giờ có thể là **toàn bộ một workspace** Không gian số (mặc định khi `cfharness filehub setup`; tài khoản đăng nhập phải là
  Workspace Admin): đường dẫn trên máy = đường dẫn bạn thấy trên web tính từ workspace, thư mục cấp 1 tạo sau tự về máy. Chế độ một thư mục cũ
  vẫn dùng được (`--scope folder`).
- Đổi tên workspace/thư mục trên web: đồng bộ chạy tiếp, trên máy giữ tên và đường dẫn cũ; giao việc bằng tên mới (`filehub:<tên mới>/…`)
  vẫn trúng đúng thư mục. Thẻ Cài đặt hiện "Toàn bộ workspace «tên mới»" và tài khoản đã đăng nhập.
- Tệp đặt thẳng ở gốc workspace không lên Không gian số (Không gian số không có tệp ở gốc) — thẻ Cài đặt liệt kê để bạn chuyển vào một thư
  mục cấp 1; agent được dặn tạo tệp trong thư mục cấp 1.
- Tài khoản bị gỡ quyền admin của workspace → thẻ Cài đặt báo đỏ kèm tên tài khoản, nút "Đăng nhập lại".
- Luồng tự thiết lập trên trang Cài đặt web (có từ v0.2.13, #121) thêm **chọn chế độ** (toàn bộ workspace — mặc định — hoặc một thư mục),
  nút **Chuyển sang toàn bộ workspace**; nút "Đăng nhập lại" gộp với "Thêm thư mục khác…" (chế độ một thư mục). Thiết lập, đăng nhập lại,
  chuyển chế độ và gỡ thư mục giờ có xác nhận 2 bước; nút Gỡ ẩn khi có run đang ghi vào thư mục đó.
- Qua link public, đăng nhập theo kiểu chuyển tiếp: Không gian số đưa trình duyệt về `https://<hub>/filehub/oauth/cb` (cần
  `web.public_origin`) — trước đây link đồng ý mở qua link public không đăng nhập xong được. Qua link public không đổi được máy chủ/binary đã
  đặt; binary tự tìm lần đầu phải là bản quản trị cài (người dùng hub không ghi được). Web trên chính máy chạy hub đăng nhập trực tiếp như terminal.
- Lệnh mới `cfharness filehub switch-to-workspace`: chuyển thư mục chung đang dùng sang toàn bộ workspace (giữ nguyên tệp cũ).
- Bị gỡ quyền admin rồi được cấp lại → đồng bộ tự chạy tiếp, không phải đăng nhập lại; workspace bị xoá trên web → báo "workspace đã bị xoá".
- Thư mục đã gỡ khỏi hub (hoặc bản cũ sau khi chuyển chế độ) không còn âm thầm đồng bộ tiếp.
- `scripts/setup-hub-user.sh` bước Không gian số mặc định đồng bộ toàn bộ workspace (`KGS_WS=` chọn sẵn tên; một thư mục: `KGS_SCOPE=folder`).

### Lưu ý khi cập nhật
- Hub đang đồng bộ MỘT thư mục vẫn chạy như cũ. Muốn chuyển sang cả workspace: `cfharness filehub switch-to-workspace` (hoặc nút "Chuyển sang
  toàn bộ workspace" ở Cài đặt web) — đăng nhập bằng tài khoản admin của workspace; xong thì kiểm bản sao mới, tự xoá bản sao cũ và thu hồi
  thiết bị cũ ở Không gian số › Hồ sơ › Thiết bị.
- Chế độ workspace và đăng nhập qua link public cần `tang-thu-sync-headless` bản mới (có `--root-mode`/`--redirect-uri`) và Không gian số
  bản có gốc workspace. Binary cũ: thiết lập chế độ workspace báo "binary sync chưa hỗ trợ…" — dùng `--scope folder` hoặc cập nhật binary.
  Bản sync mới vẫn báo `--version` 0.1.1 (chưa phát hành app desktop mới) nên `filehub.sync_min_version` giữ nguyên — hub nhận diện bản mới
  theo tính năng. Máy chủ Không gian số (thumucai.trungtamdhs.com, trungtamai.cchchn.com) đã chạy bản có gốc workspace từ 2026-11-16.
- Đăng nhập Không gian số qua link public cần đặt `web.public_origin` (https) ở terminal.

---

## v0.2.13 — 2026-11-16

Tự thiết lập thư mục chung Không gian số ngay trên web, và sửa lỗi trang Cài đặt báo lỗi sau khi cập nhật lên v0.2.12: giao diện web giờ tự chạy lại sang bản mới sau mỗi lần cập nhật.

### Tính năng mới — thư mục chung Không gian số
- Trang **Cài đặt** web có luồng tự thiết lập Không gian số, dùng được cả qua link public: nhập địa chỉ Không gian số và tên thư mục, bấm
  **Đăng nhập & nối thư mục**, rồi đồng ý thiết bị trên trang Không gian số (link mở ngay trên thẻ, thẻ tự cập nhật khi xong). Trên web cũng
  đăng nhập lại khi hết phiên, thêm thư mục khác, gỡ thư mục, tạm dừng/chạy lại đồng bộ. Không còn phải nhờ admin chạy lệnh ở máy chủ (#121).
- Binary sync chỉ do admin cài: web không nhận đường dẫn binary; hub dùng cấu hình hoặc tự tìm ở `/usr/local/bin`, `/opt/homebrew/bin`,
  `~/.local/bin`. Chỗ đặt bản sao trên máy chỉ đổi được khi dùng web trên chính máy chạy hub.

### Sửa lỗi
- **Trang Cài đặt lỗi 500 sau khi cập nhật lên v0.2.12 (macOS/Linux)**: bước cập nhật chỉ khởi động lại daemon, còn tiến trình web
  (`cfharness web`, gồm cả web chạy bằng LaunchDaemon) vẫn chạy code cũ nhưng đọc giao diện mới → trang nào dùng thông tin mới (thẻ
  "Ứng dụng máy tính" ở Cài đặt) báo lỗi. Giờ web tự chạy lại sang bản mới khi bản cài đổi (khoảng 30 giây), và daemon bản mới khi lên
  sẽ dừng web còn chạy code cũ để launchd bật lại (không có launchd thì daemon tự chạy lại đúng lệnh cũ). Sau khi web chạy lại cần đăng
  nhập lại (#120).

### Lưu ý khi cập nhật
- Không có migration, không có cấu hình phải sửa.
- Hub đang lỗi trang Cài đặt (đã lên v0.2.12): mở thẳng trang **`/update`** của web (ví dụ `https://<hub>/update`) và bấm cập nhật
  lên bản này — không cần vào Cài đặt. Khoảng 1 phút sau khi daemon lên lại, web tự chạy lại bằng code mới; đăng nhập lại là xong.

---

## v0.2.12 — 2026-11-16

Tải app máy tính ngay trên web, task xong luôn có báo cáo về người giao việc, và sửa cập nhật trên Windows khi web UI đang mở.

### Tính năng mới
- **Tải app máy tính ngay trên web**: trang Cài đặt có thẻ "Ứng dụng máy tính (Cfharness Desktop)" với nút tải macOS (.dmg) / Windows (.exe)
  bản mới nhất + hướng dẫn mở app chưa ký; chân thanh bên có link "Tải app máy tính" (#117).

### Thay đổi
- **Task xong luôn có báo cáo gửi về người giao việc**: trước đây chỉ điều phối viên báo (khi nó chạy nền và tự quyết gọi
  `notify_owner`), và nó hay kết thúc bằng `NO_ACTION` nên người không nghe gì. Giờ khi một task người giao xong, hub tự gửi báo cáo về
  đúng hội thoại/thread đã giao việc, theo khung cố định **Đã làm / Còn mở / Cần bạn** (tắt bằng `manager.auto_report_tasks: false`).
  Task không gắn hội thoại nào (tạo từ CLI/web) thì điều phối viên bắt buộc phải báo, không được bỏ qua (#118).
- Agent làm task kết thúc bằng báo cáo theo khung trên. Kết quả lưu vào task là bản **đầy đủ hơn** giữa tin nhắn cuối và `summary` của
  `task_complete`; trước đây `summary` ngắn luôn thắng nên báo cáo bị mất chi tiết.
- Task gốc tách thành task con: kết quả được gộp lại theo cùng khung (mỗi task con một dòng, gom "Còn mở"/"Cần bạn" của các task con),
  không còn hiện id nội bộ.
- Sự kiện "task xong" gửi điều phối viên mang câu kết quả + "Còn mở" + "Cần bạn" (trước đây 200 ký tự đầu).

### Sửa lỗi
- **Cập nhật trên Windows làm hub chết hẳn khi web UI đang mở**: `cfharness web` (và tiến trình khác chạy từ bản cài) giữ file trong venv
  → `uv tool install` lỗi "Access is denied" giữa chừng, venv bị xoá dở, bước quay về bản cũ cũng lỗi theo. Tiến trình cập nhật giờ dừng
  mọi tiến trình chạy từ bản cài trước khi cài, thử cài lại một lần nếu file vẫn bị khoá, và bật lại web UI sau khi xong (không mở tab
  trình duyệt mới). macOS/Linux không đổi. Có hiệu lực từ lần cập nhật **sau** khi hub đã chạy bản có sửa lỗi này (#116).

### Lưu ý khi cập nhật
- Không có migration. Báo cáo task tự động bật sẵn; tắt bằng `cfharness config set manager.auto_report_tasks false`.
- Windows: bản sửa cập nhật (#116) chỉ có tác dụng từ lần cập nhật **sau** v0.2.12 — lần này nếu web UI đang mở, đóng nó trước khi bấm cập nhật.

---

## v0.2.11 — 2026-11-16

### Sửa lỗi
- **Điều phối viên không trả lời được trên Windows** (`không spawn được claude.EXE: [WinError 206] The filename or extension is too long`):
  system prompt của điều phối viên lên ~36.600 ký tự sau khi thêm danh sách lệnh CLI (v0.2.10), vượt giới hạn dòng lệnh Windows 32.767.
  Prompt dài hơn 8.000 ký tự giờ truyền qua file (`--append-system-prompt-file`) thay vì dòng lệnh (#114).
- Danh sách lệnh CLI trong prompt điều phối viên gọn lại (mỗi nhóm lệnh một dòng, cờ của lệnh con xem `--help`): ~14.700 → ~6.800 ký tự,
  bớt token mỗi lượt (#114).
- Run không khởi chạy được vẫn ghi lại system prompt đã dùng, để điều tra (#114).

### Giám sát cỡ system prompt (#114)
- Daemon ghi log cảnh báo khi system prompt của một agent vượt `runtime.claude.system_prompt_warn_chars` (mặc định 30.000 ký tự),
  kèm các khối lớn nhất.
- `cfharness doctor` báo vàng agent có prompt vượt ngưỡng; bình thường in agent có prompt lớn nhất.

### Lưu ý khi cập nhật
- Không có migration. Cấu hình mới có giá trị mặc định: `runtime.claude.system_prompt_file_min_chars` (8000),
  `runtime.claude.system_prompt_warn_chars` (30000).

---

## v0.2.10 — 2026-11-15

### Cài đặt hub cho user
- `scripts/setup-hub-user.sh` làm trọn trong một lệnh: thêm ingress tunnel + DNS cho tên miền con (biến `DOMAIN`, #108), nối thư mục chung Không gian số khi đặt `KGS=<URL>` (#109).

### Điều phối viên
- Biết danh sách lệnh CLI thật của `cfharness`; link trần trong câu trả lời bấm được; file được nhắc tới có chip **Xem/Mở** (#103).
- **Nhập bằng giọng nói** trên web (nút micro ở ô chat Điều phối, ô "Nói tiếp" của run, ô giao việc): chế độ đọc chép và chế độ hội thoại.
  Tin nói bằng giọng không bao giờ duyệt approval hay trả lời câu hỏi xin phép (có thể nghe nhầm) — điều phối viên phải hỏi lại bằng nút (#105).
- Đổi người làm task mà vẫn giữ task trong task gốc: tool `task_reassign` của điều phối viên / RPC `task.reassign` (#104).

### Thư mục dùng chung với "Không gian số" (#106)
- Thư mục đồng bộ 2 chiều giữa máy chạy hub và Không gian số: bạn thêm/sửa/xoá tệp trên web hoặc điện thoại, agent làm việc trên bản sao cục bộ
  (`~/KhongGianSo/<tên>`). Thiết lập: `cfharness filehub setup --server https://… --binary /đường/dẫn/tang-thu-sync-headless` (đồng ý thiết bị,
  thư mục rỗng tạo ngay ở trang đồng ý); trạng thái: `cfharness filehub status` hoặc thẻ "Không gian số" ở Cài đặt web (qua link public chỉ xem).
- Giao việc với workspace `filehub:<tên>` hoặc `filehub:<tên>/<thư mục con>`; mọi run được đọc thư mục chung. Agent xoá/di chuyển tệp ở đó
  không cần duyệt — sau run hub báo (Telegram/Slack + Cài đặt), khôi phục ở thùng rác Không gian số. Không có "Hoàn tác" ở hub cho thư mục này.
- Bạn sửa tệp trên Không gian số trong lúc agent đang làm → agent được nhắc đọc lại; Không gian số hiện dải "cfharness đang làm việc".
  Bản xung đột, tệp có thể chứa bí mật và chiều đẩy lên bị dừng đều được báo.
- Chip tệp của run trên thư mục chung có thêm "Không gian số" (mở thẳng tệp trên web Không gian số).
- **Lưu ý khi cập nhật**: cần binary `tang-thu-sync-headless` (≥ `filehub.sync_min_version`); tắt mặc định, không có migration.

### App desktop (hỗ trợ phía hub) (#110)
- `cfharness web --json [--ensure] [--port N]`: in một dòng JSON (địa chỉ, link đăng nhập một lần, mã hub, phiên bản, tên); `--ensure` tự
  khởi động web chạy nền (cổng riêng theo hub, log ở `HUB_HOME/logs/web.log`). Cấu hình mới `web.port`, `web.display_name`.
- Endpoint `GET /.well-known/cfharness.json` (công khai, chỉ cho biết đây là hub cfharness) và `GET /app/badge` (cần đăng nhập, chỉ số
  duyệt/câu hỏi/run đang chạy/ESTOP — không gia hạn phiên) cho app desktop.
- Nhập bằng giọng nói dùng được trong app desktop: `voice.js` dùng bộ nhận dạng giọng nói của máy (cầu nối `window.cfhNative.speech`) khi có,
  vẫn giữ quy tắc tin giọng nói không bao giờ duyệt/chốt.
- **Directory hub** cho app desktop (hub của người quản trị máy): `cfharness admin directory token [--rotate]` in một lần URL
  `/.well-known/cfharness-directory?t=…` — app nhập một lần danh sách hub có địa chỉ công khai (tên, user, origin; không cấp quyền đăng nhập).
  `cfharness admin directory export [--out]`, `… status`; cấu hình mới `admin_directory.name`. Token sai → 404, thử sai liên tục → 429.
- Bản tóm tắt gửi người quản trị (`admin_export`) thêm `web.public_origin` (đã chuẩn hoá) và `web.display_name` (schema 3); hub chưa đặt hai
  khoá này vẫn ghi schema 2 (bộ đọc nhận schema 1/2/3).
- Agent không đọc/ghi được thư mục dữ liệu của app desktop (`io.cfharness.desktop`, và `~/Library/{WebKit,HTTPStorages,Caches}/cfharness-desktop`
  của bản dev chưa đóng gói — cookie phiên, token directory): chặn bằng sandbox,
  luật quyền tĩnh và bộ phân loại — agent trust full cũng bị chặn, kể cả qua Bash và Glob dùng ký tự đại diện.
- `cfharness web --json --ensure` gọi cùng lúc (app mở + gõ lệnh) chỉ bật một server web; `started` báo đúng trên Windows.
- **Lưu ý khi cập nhật**: máy nhiều user — nâng cấp **hub của người quản trị trước**. Hub đã đặt `web.public_origin`/`web.display_name` ghi bản tóm tắt schema 3 — hub
  admin bản cũ sẽ báo `unsupported_schema` cho hub đó cho tới khi được nâng cấp.

### Chi phí
- Lượt chạy tiếp sau khi **người** duyệt/từ chối approval không bị trần ngân sách ngày chặn (trước đây báo `resume_failed`, quyết định của người
  thành vô ích). Vẫn áp trần mỗi run và chính sách root của admin. Điều phối viên/hub tự quyết thì không được miễn.
  Khi miễn trừ thực sự được dùng → audit `budget.exempt_approved` (#105).

### Chuyển hub sang máy khác (#111)
- `cfharness hub export` đóng gói toàn bộ hub (agent, team, task, tri thức/ledger, lịch, lịch sử run) thành MỘT file `.cfhx`;
  `cfharness hub import <file> [--dry-run] [--map-path CŨ=MỚI]` nhập vào máy khác, tự đổi đường dẫn HUB_HOME/`$HOME`. Mặc định chế độ
  **nhân bản** (hai máy cùng chạy): Telegram/Slack tắt, lịch tạm dừng, task đang dở bị chặn, yêu cầu duyệt/câu hỏi đang chờ của máy nguồn
  được huỷ ("[hub] nhân bản — chưa chạy lại") ở máy mới.
- Gói **mang secret đã mã hoá** bằng passphrase (≥ 12 ký tự, hỏi 2 lần; không TTY: `--passphrase-file` quyền 0600): token MCP, tài khoản Claude,
  bot…; `--with-totp`, `--with-mcp-tokens` để mang thêm TOTP web / token MCP HTTP; `--no-secrets` để không mang. Khi nhập: hỏi passphrase và
  thử ngay — sai thì dừng trước khi ghi gì; `--skip-secrets` nhập phần còn lại (quên passphrase). Chế độ nhân bản KHÔNG nhập bot Telegram/Slack
  và đăng nhập OAuth MCP của máy nguồn (cần bot riêng, đăng nhập lại MCP); token MCP tĩnh và tài khoản Claude được nhập.
- `hub import --replace` lỗi giữa chừng → tự khôi phục hiện trạng trước khi nhập (bản `pre-restore`).
- Thư mục **Không gian số** (filehub): token thiết bị không bao giờ đi theo gói; sau khi nhập đồng bộ ở máy mới TẮT tới khi bạn chạy
  `cfharness filehub setup --name <tên>` (đăng nhập thiết bị mới — báo cáo nhập, `--dry-run` và `hub doctor` đều nhắc); chuyển nhà giữ thư
  mục chung (đường dẫn đổi theo máy mới), nhân bản gỡ thư mục chung để hai hub không tự đồng bộ chung; máy cũ đóng băng vẫn đồng bộ tệp.
- **Làm được trên web**: Cài đặt → **Chuyển hub sang máy khác** (`/hub-transfer`). Xuất: chọn gọn/đầy đủ, nhân bản/chuyển nhà, có mang secret
  không → xác nhận 2 bước (passphrase gõ ở bước 2) → chạy nền, xong bấm **Tải về** (link dùng một lần, hết hạn sau 10 phút). Nhập: tải file
  `.cfhx` lên (trần `transfer.max_upload_mb`, mặc định 4096 MB) → xem toàn bộ kế hoạch như `--dry-run` (sửa đường dẫn, chế độ, thử passphrase)
  → xác nhận 2 bước (gõ lại id gói) → hub tạm dừng vài phút để nhập rồi tự chạy lại, trang hiện báo cáo + kết quả `hub doctor`; vẫn giữ đăng
  nhập web. Máy mới tinh có thẻ **"Nhập hub từ máy khác"** ở trang chủ. Hub đã chuyển nhà có nút **Mở lại hub này**.
- Gói mang **session Claude Code** của hub để hội thoại điều phối viên, task chưa xong và run dở **chạy tiếp (`--resume`) ở máy mới**
  (profile `core`: chỉ phần đang dở; `full` — mặc định: mọi session của hub; session Claude Code bạn tự chạy ngoài hub không bao giờ bị lấy).
  Khi nhập, session được đặt vào đúng thư mục `~/.claude/projects/…` theo đường dẫn mới và đường dẫn trong transcript được đổi theo;
  session đã có ở máy đích (trùng id) giữ nguyên. Transcript trong gói đã **che secret theo giá trị** (`***`). Áp cả ở chế độ nhân bản.
- Nhập được **khác hệ điều hành, hai chiều** (macOS/Linux ↔ Windows): đường dẫn trong DB, cấu hình, chính sách và transcript session
  được đổi kiểu (`/Users/a/x` ↔ `C:\Users\a\x`, cả dạng `C:/…` và dạng escape trong JSON); `--map-path` nhận cả đường dẫn Windows.
  Bản kiểm tra `--dry-run` báo thêm: tên file không tạo được ở máy đích (ký tự `<>:"|?*`, tên `CON`/`NUL`…, hai tên chỉ khác hoa/thường
  trên ổ không phân biệt hoa/thường — kể cả macOS mặc định) — file skill/tri thức được **đổi tên an toàn** (vd skill `con` → `con-skill`) và
  ghi lại, file khác thì dừng nhập kèm tên file cần đổi ở máy nguồn; sandbox OS không có trên Windows; workspace còn kiểu đường dẫn máy cũ.
- Mọi lần nhập: MCP server chạy bằng lệnh không có ở máy mới (vd `/opt/homebrew/bin/node` khi sang Windows) được **đánh dấu lỗi** kèm gợi ý
  `cfharness mcp edit <tên>`. Tự khởi động daemon không được chuyển — cài lại bằng `cfharness daemon install-service`.
- **Chuyển nhà** `cfharness hub export --for move`: sau khi gói qua kiểm tra, máy cũ bị **đóng băng** (bền qua khởi động lại daemon) — không
  chạy run nào (kể cả điều phối viên, lịch, task), không chạy lịch/nhịp/học, Telegram/Slack ngừng nhận và gửi (máy mới giữ bot); sao lưu tự động
  vẫn chạy. `daemon status`, `top` và mọi trang web hiện banner "đã chuyển sang máy khác lúc …". Dùng lại máy cũ: `cfharness hub unfreeze`
  (có xác nhận). Nhập ở máy mới với `--mode move` để giữ nguyên bot/lịch.
- Gói ghi danh sách **repo workspace** (remote đã bỏ thông tin đăng nhập, nhánh, HEAD, có thay đổi chưa commit không — repo KHÔNG nằm trong gói).
  Khi nhập: repo chưa có ở máy mới → báo kèm lệnh `git clone …`; `--clone-repos` tự clone (không bao giờ vào thư mục đã có); agent có
  workspace chưa có → nhập ở trạng thái **paused** (tạo/clone repo rồi `cfharness agent resume <id>`). Repo có thay đổi chưa commit ở máy cũ → cảnh báo.
- Đường dẫn máy cũ trong **memory, skill, SOUL và graph memory** được tự viết lại khi daemon máy mới lên lần đầu — qua ledger (actor `import`,
  rollback được; không bao giờ bị tự rollback). Lịch sử (episode, fact đã hết hiệu lực, skill đã lưu trữ) giữ nguyên.
- `cfharness hub doctor [--probe] [--json]`: kiểm hub sau khi nhập (claude + phiên bản + đăng nhập, secret được tham chiếu, MCP, Telegram/Slack,
  repo + HEAD, agent/lịch đang tạm dừng, việc viết lại tri thức, tự khởi động, `cfharness[vector]`, quyền file) và gợi ý lệnh sửa; tự chạy sau
  `hub import`. Chỉ đọc, chạy được khi daemon dừng; `--probe` thử thật `claude auth status` + kết nối MCP (không gọi model).
### An toàn (guard) (#107)
- Lệnh "chỉ đọc" có cờ ghi file hoặc chạy lệnh tuỳ ý không còn được cho qua như an toàn: `sort -o`, `awk` (`system()`, `print > file`,
  pipe tới lệnh, `-i inplace`), `git --output`/`-c core.pager=…`/`--paginate`/`--ext-diff`, `diff --output`, `rg --pre`, `man -P`,
  `env -S`/`--split-string=`, `yq -i` — giờ là ghi/cần duyệt và vẫn kiểm đường dẫn được bảo vệ.
- Vé duyệt một lần và luật "luôn cho phép" (`exact`) phân biệt lệnh chạy ngoài sandbox (`dangerouslyDisableSandbox`), khoảng trắng trong
  dấu nháy và thư mục chạy (cwd) của Bash — duyệt lệnh ở thư mục này không còn áp cho thư mục khác.
- Luật "luôn cho phép" `exact` bắt buộc gắn agent; luật cũ thiếu agent (sửa tay) không còn áp cho mọi agent (trang Quyền ghi "không áp dụng").

### Web (#107)
- Sau khi gửi tin đọc bằng giọng nói, tin gõ tay kế tiếp không còn bị gắn nhãn giọng nói.

### An toàn (quyền agent / điều phối viên / Slack) (#107)
- Agent không còn tự ghi được dòng audit giả danh người (`actor=user`, kênh web…): RPC `audit.record` giờ chỉ người gọi được.
- Điều phối viên/agent không còn giao task cho điều phối viên hoặc team đang ESTOP bằng tên khoá phụ `assignee_agent_id`/`team_id`.
- Điều phối viên không còn yêu cầu học/tinh chỉnh agent điều phối viên bằng cách gửi kèm `id` của agent khác (`learn.refine`).
- Bot Slack chưa ghim workspace (token đặt tay) chỉ nhận tin từ workspace của chính token, không còn nhận từ mọi workspace.

### Run, task và lịch (sửa lỗi) (#104, #107)
- Task gốc không còn kẹt "đang chạy" khi task con cuối bị huỷ: còn task con xong → task gốc xong; không còn task con nào → task gốc `blocked` kèm lý do (hiện ở "Cần bạn") (#104).
- Task không còn kẹt vĩnh viễn ở trạng thái "đang nhận" (`__claiming__`) khi khởi chạy run gặp lỗi bất ngờ hay daemon dừng giữa chừng.
- Tự resume: chuỗi có run gốc đã bị dọn vẫn bị giới hạn ngân sách (trước đây thành không giới hạn); `recovery.auto_resume_max_attempts` > 50 nay có hiệu lực.
- Chi phí luỹ kế của session không còn bị tụt khi một lượt resume thiếu `total_cost_usd` — chi phí lượt sau không bị cộng hai lần.
- Lịch cron không bao giờ chạy được (vd `0 0 30 2 *`) bị từ chối ngay lúc tạo/xem trước, kèm lỗi tiếng Việt.
- Khởi động lại daemon: run bị ngắt không còn mất lượt tự resume khi khởi động không có event loop, và không còn bị resume hai lần.
- Approval đã quyết nhưng lượt chạy tiếp bị chặn (ESTOP, ngân sách…): run chuyển `failed` thay vì kẹt "chờ duyệt"; chạy tiếp bằng `cfharness run continue`.
- Gửi run lỗi giữa chừng không còn để lại run "đang xếp hàng" ma giữ chỗ ngân sách ngày.

### Học và tri thức (sửa lỗi) (#107)
- Thay đổi học chờ eval gate (L3) được kiểm guard lại trước khi áp dụng: ghim/hạn mức/đọc-trước-khi-ghi người vừa đặt trong lúc chấm không còn bị bỏ qua.
- Quét an toàn nội dung tri thức quét toàn bộ nội dung (trước đây chỉ 64K đầu, SKILL.md được tới 100K — injection đặt sau mốc 64K lọt qua).
- Sửa file hỗ trợ của skill (`references/`…) bị giới hạn cùng trần `skill_chars` như SKILL.md (trước đây không giới hạn).
- Memory/skill chứa token bí mật dạng thô (`sk-ant-…`, `ghp_…`, `github_pat_…`, `AKIA…`, `xoxb-/xoxp-…`…) bị chặn, không còn được ghi vào tri thức.
- Nhiều tiến trình cùng mở một `hub.db` cũ không còn lỗi migration chạy hai lần ("table … already exists").
- Rollback memory không gỡ được mục (mục đã bị sửa tiếp sau entry) giờ báo `partial` kèm số mục bỏ qua thay vì báo thành công im lặng.
- Tên skill có ký tự xuống dòng ở cuối (`"my-skill\n"`) bị từ chối.
- Rollback không còn khôi phục từ blob ledger hỏng/bị sửa: blob sai sha256 bị từ chối kèm lỗi.
- Nhập/nhân bản agent (`agent import`, `agent clone`) giờ đi qua quét an toàn tri thức (threat scan) như mọi thay đổi memory/skill:
  nội dung trúng mẫu nguy hiểm thì bị từ chối và không ghi gì vào hub.
- Áp dụng thay đổi học (memory/skill/cấu hình/graph) an toàn khi daemon chết giữa chừng: hub ghi ý định trước, khởi động lại thì tự hoàn tất
  hoặc bỏ thay đổi dở dang; trường hợp không xác định được thì cảnh báo (`learning.ledger_recovery`) và giữ bản trước để khôi phục tay.
  Thêm migration v27 (bảng `ledger_pending`) — tự chạy khi cập nhật.

### Lưu ý khi cập nhật
- Không có migration mới (vẫn v26).
- Máy nhiều user: nâng cấp **hub của người quản trị trước** — hub đã đặt `web.public_origin`/`web.display_name` ghi bản tóm tắt schema 3,
  hub admin bản cũ sẽ báo `unsupported_schema`.
- Thư mục chung Không gian số tắt mặc định; muốn dùng cần binary `tang-thu-sync-headless` (≥ `filehub.sync_min_version`) rồi `cfharness filehub setup`.
- Sau khi nhập hub từ máy khác: tự khởi động daemon không đi theo gói (`cfharness daemon install-service`), Không gian số phải `filehub setup` lại;
  chạy `cfharness hub doctor` để xem việc còn phải làm.
- Luật "luôn cho phép" `exact` sửa tay mà thiếu agent sẽ không còn áp dụng — tạo lại luật gắn agent (trang Quyền).

---

## v0.2.9 — 2026-11-14

### Tin cậy thiết bị (web public)
- Khi nhập mã TOTP, tick **"Tin cậy thiết bị này"** → 30 ngày không phải nhập lại. Chỉ bật khi vào qua địa chỉ public https.
- Đăng xuất vẫn giữ thiết bị; nút riêng "Đăng xuất và quên thiết bị này".
- Mỗi lượt vào/ra web, khoá do sai mã, thiết bị mới hay bị thu hồi đều báo lên Telegram.
- Xem/thu hồi: `cfharness web devices list|revoke|revoke-all` hoặc Cài đặt.

### Quản trị admin (nhiều hub trên một máy)
- Admin xem danh sách user, ESTOP từ admin (user không tự gỡ được), cảnh báo `admin.alert`.
- Chính sách root ép xuống từng user: trần ngân sách, số run đồng thời, bắt buộc sandbox.
- Nhật ký kiểm toán chỉ có metadata; trang "Quản trị máy" cho user thấy admin thấy gì về mình; `cfharness admin setup check`.

### Chi phí
- Trần ngày riêng cho điều phối viên; giới hạn số lần một task bị gián đoạn được xếp lại hàng.
- Run hỏng giữa chừng vẫn tính chi phí ước lượng; `budget.warning`/`budget.exceeded` báo một lần mỗi ngày.
- Bỏ mọi hạn mức dùng mặc định (`null` = không giới hạn) (#99).

### Sao lưu và cập nhật an toàn
- `cfharness backup create|verify|list|restore`: tự sao lưu 03:30 hằng ngày, giữ 7 bản; không chứa secret và dữ liệu đăng nhập web.
- Trước mỗi lần cập nhật đều chụp một bản; daemon không lên thì tự quay về bản cũ; `cfharness update --rollback`.

### Tài khoản Claude
- Danh sách tài khoản Claude, người bấm đổi để chạy tiếp khi một tài khoản hết hạn mức (web, CLI, `/taikhoan` trên Telegram/Slack) (#102).

### Điều phối viên & luồng việc
- **Luồng việc (workstream)**: mỗi epic một luồng, có STATE.md + task mở; tab luồng trên khung chat, đổi tab không tải lại trang, đủ CRUD (#83, #84, #89, #90).
- Giao diện chat kiểu Claude: markdown, nút Dừng, chữ chạy, thao tác dưới tin, SSE có `seq` tự nối lại (#95, #98).
- Khối "Response style" + ghi chú định dạng theo kênh; golden eval phong cách trả lời (#91, #97).
- Nút "Chạy nhịp ngay".

### Web
- Trang **Quyền đã cấp** (`/permissions`); sửa lịch chạy + xem lịch sử lượt.
- Trang Công việc realtime (#85).
- Transcript trang run kiểu Claude: markdown, gom tool, subagent lồng, diff, bố cục một cột; lưu suy nghĩ của agent để phát lại (#92, #93, #96).
- Bấm xem / mở file agent đã thao tác (cục bộ + cloud), cấu hình ở Cài đặt `files.*` (#87, #88).
- Văn phòng 3D: bảng quản lý agent, tạm dừng/tiếp tục/dừng run qua xác nhận 3D (#86).

### Telegram / Slack
- Nút tạm dừng/bật lịch; tin hiện markdown (Telegram bỏ `<pre>`, Slack dùng mdconv, chia tin theo khối) (#94).

### Agent
- Agent nạp plugin Claude Code (`agent.plugins` → `--plugin-dir`) (#82); agent thường dùng connector claude.ai (`tool_policy.connectors`) (#70).

### Lưu ý khi cập nhật
- Hub nào đã tự đặt `notify.*.events` thì phải thêm tay `web.*`, `budget.*`, `admin.alert`.
- Migration tới v26.

---

## v0.2.8 — 2026-11-10

### Sửa lỗi tạo điều phối viên
- `agent create --template manager` lỗi `KeyError` trên mọi hub (SOUL có ngoặc nhọn bị `str.format` hiểu nhầm). Nay chỉ thay `{name}`/`{role}`.
- `scripts/setup-hub-user.sh` bước 6/9 tạo sẵn agent `dieu-phoi` (mẫu manager) và chỉ định làm điều phối viên.
- Hướng dẫn tạo hub: thêm bước 6b điều phối viên.

## v0.2.7 — 2026-11-10

### Sửa lỗi vận hành nhiều hub
- Cập nhật khi hub chạy bằng LaunchDaemon/LaunchAgent: launchd chạy lại daemon bằng bản mới (trước đây daemon mới chạy ngoài launchd, chết thì không tự lên lại).
- `scripts/setup-hub-user.sh`: ghép token Claude bị xuống dòng khi dán, gọi thử Claude trước khi lưu; token hỏng thì hỏi lại (hết lỗi 401).

## v0.2.6 — 2026-11-10

### Đăng ký đăng nhập bằng mã QR
- Trang `/enroll` → "Hiện mã bí mật" hiện **mã QR** cho Google/Microsoft Authenticator, Authy, 1Password; vẫn có link `otpauth://` và khoá nhập tay.
- QR sinh ở server (`segno`, không CDN), trang `Cache-Control: no-store`.

## v0.2.5 — 2026-11-10

### Sửa lỗi đa instance
- **Một daemon cho mỗi HUB_HOME** (khoá `daemon.lock`): LaunchDaemon và CLI tự khởi động cùng lúc không còn bật 2 daemon cùng ghi `hub.db`.
- `cfharness web totp enroll` không in bí mật (chỉ in link `/enroll`; `--show-uri` nếu cần).
- `scripts/setup-hub-user.sh`: tạo hub cho một user macOS bằng một lệnh.

## v0.2.4 — 2026-11-08

- **Nhiều hub trên một Mac mini**: giam agent trong workspace (`workspace.confine`), web public qua tunnel TLS + TOTP, LaunchDaemon (`cfharness admin install-daemon`), tổng quan admin (#75).
- Slack: tag bot trong thread đã tham gia → đọc được tin người khác; `thread_context` mặc định bật (#73).
- Slack: đọc tin được chuyển tiếp (Forward) (#74).

## v0.2.3 — 2026-11-07

- Slack: điều phối viên tag được người dùng (#69); báo cáo lịch đăng thẳng vào kênh chung (`notify.slack.channel_root_posts`) (#71).
- Slack mặc định `allow_channels` + reaction (#72).
- Chat: gỡ nút khi approval/câu hỏi đã được quyết trong lúc đang gửi (#59).

## v0.2.2 — 2026-11-05

- Index run đang sống (migration v22) — hết đứng mỗi chu kỳ `manager.check_s` (#67).

## v0.2.1 — 2026-11-05

### Học / memory
- **Graph memory**: kho thực thể + fact có thời gian trên SQLite, truy hồi FTS5 + bge-m3, tầng agent/team/global (#49); mirror MEMORY.md vào graph (#51).
- Web `/memory`: quản lý + xem memory hệ thống và graph memory (#50).
- Tự đề xuất golden task từ task done, người duyệt một chạm (#40).

### Điều phối viên
- Việc cho thành viên team luôn đi qua team (#53); kết quả heartbeat báo về đúng hội thoại đã giao việc (#56).
- `task_cancel` archive task chờ (#62).

### Guard & bảo mật
- Bịt ca lách lập lịch hoãn / chạy bền (#37); allowlist domain WebFetch theo agent (`tool_policy.web_allow_domains`) (#44).
- MCP `hub_approve` grant theo luật (#45); `agent.revert/archive`, `trash.restore/purge` chỉ người ở mọi đường vào (#39).

### Tin cậy
- Tự resume có giới hạn run dừng vì lỗi tạm thời + thống kê run treo ở thẻ Sức khoẻ (#42).
- Xoay vòng log daemon an toàn trên Windows (#43); đóng `eval_runs` kẹt khi daemon khởi động lại (#64); sửa đường dẫn autostart (#65).

### Văn phòng 3D
- Khu Cafe điện tử cho agent rảnh, chế độ "Vào văn phòng" (đi bộ), điều phối viên là nhân vật chính, làm lại hình ảnh (PBR, bóng), mượt 60 FPS (#52, #54, #55, #57, #58, #60, #61, #63).

### Telegram / Slack
- Tin chủ động chỉ gửi về chat đã giao việc (#48); Slack trả lời điều phối viên dạng trích dẫn có link (#58).

## v0.2.0 — 2026-10-31

### Slack như Claude Tag
- Tag bot ở mọi kênh không cần ghép nối (#24); đang xử lý + tiến độ + nút Dừng, nói tiếp trong thread, ngữ cảnh thread (#25, #26).
- Không đăng lên gốc kênh — trả lời vào thread của cuộc, không có thì DM (#27); chế độ gọn, icon ⏳ → ✅/❌ (#28, #29, #30).

### Điều phối viên & duyệt
- Nhận đính kèm PDF/Excel/Word/PowerPoint (#31).
- **Cổng duyệt** `gates.mode` human|manager|off + không hỏi lại theo agent + luật (#32).
- Skill manager trên web, guard trust all (#23).

### Web
- **Chế độ 3D "Văn phòng"**: agent là nhân vật chibi, duyệt + chat ngay trong 3D (#33).
- Tài nguyên tĩnh có phiên bản (hết kẹt cache `app.js`) (#22); Cài đặt hiện cấu hình theo nhóm, dropdown cho mục có danh sách giá trị.

### Học / memory (ngang Hermes)
- Tool `memory` cho agent ghi memory ngay trong session (#35), không giới hạn số lần, ghi được tầng team/global (#41).
- Thu hẹp khoảng cách memory với Hermes (#36); `session_search` giữ kết quả tool 2.000 ký tự + gộp theo luồng (#38).

### Khác
- Tin Telegram/Slack gọn: ai — cần gì — bấm gì (#21); README hướng dẫn cài cho người dùng (#20).
- Daemon không ghi đè token người khi đã có daemon sống; launchd/systemd mang PATH lúc cài (#46); sửa 7 lỗi từ rà log (#34).

## v0.1.0 — 2026-10-27

Bản phát hành đầu tiên cho người dùng. Cài: `uv tool install --python 3.13 git+https://github.com/lephuong123/agent-hub@v0.1.0`.

- **Lõi**: daemon điều phối agent chạy trên `claude` CLI (mỗi run một tiến trình `claude -p … stream-json`), cô lập cấu hình, hàng đợi FIFO,
  ngân sách, ESTOP, khôi phục sau restart, tự resume; CLI `cfharness`, JSON-RPC qua Unix socket; SQLite WAL.
- **Bề mặt**: TUI `cfharness top` (kiểu htop, attach, steer, takeover), `cfharness chat` fullscreen kiểu Claude Code, web cục bộ, MCP server `hub_*`
  (stdio + HTTP có token), Telegram, Slack 2 chiều (Socket Mode: duyệt bằng nút, `/cfh`, thread = reply, modal, ảnh/tệp).
- **An toàn**: hook guard phân loại thao tác (safe/write/destructive), phê duyệt của người (không bao giờ tự từ chối do hết giờ), "luôn cho phép",
  hồ sơ phê duyệt strict|balanced|autonomous, sandbox OS, kiểm mạng WebFetch/curl, trust full theo agent, xác thực RPC, toàn vẹn policy, che secret.
- **Team & điều phối**: team (lead/worker/reviewer), task DAG, dispatcher, workspace repo thật (checkpoint/diff/revert, worktree cho task song song),
  **điều phối viên** (manager) giao việc + heartbeat, câu hỏi có lựa chọn, xin phép người bằng nút, lịch chạy tự động, hệ thống ngoài hub (launchd).
- **Học**: Signal, review fork, proposal tự áp dụng qua ledger (rollback được), eval gate với golden task, regression monitor, curator, digest,
  tầng tri thức team/global + canary, tìm kiếm hội thoại FTS5.
- **Tích hợp**: registry MCP + OAuth 2.1, danh mục ~64 MCP server, connector claude.ai, Claude in Chrome, secret store, audit.
- **Nền tảng**: macOS, Linux, Windows native; tự khởi động daemon (launchd / systemd / Task Scheduler); đổi tên agent-hub → cfharness (`home migrate`).
