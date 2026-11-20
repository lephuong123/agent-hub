# Release notes — cfharness

Mỗi phiên bản là một tag `vX.Y.Z` (danh sách: [Releases](https://github.com/lephuong123/agent-hub/releases)).
Cập nhật hub đang chạy: `cfharness update` hoặc nút "Cập nhật hệ thống" ở trang Cài đặt web.

Quy tắc: **viết release note ở file này TRƯỚC khi phát hành** (xem mục "Phát hành" trong `AGENTS.md`).
Mục mới nhất ở trên cùng. Thay đổi đã gộp mà chưa có tag nằm ở mục "Chưa phát hành".

---

## Chưa phát hành

### Người dùng tự đăng nhập claude.ai cho connector ngay trên web hub
- Cài đặt › Tài khoản Claude có khối **Connector claude.ai**: bấm "Đăng nhập claude.ai", mở link, đăng nhập tài khoản đã Connect Google Drive/Gmail… trên claude.ai, dán code — xong là agent dùng được connector, không cần admin vào máy chạy lệnh. Khối hiện tài khoản đang đăng nhập, nút đăng xuất và công tắc "Tự dùng connector cho mọi agent" (`connectors.auto`, bật cần xác nhận 2 bước).

---

## v0.2.24 — 2026-11-20

### Lịch — gửi kết quả tới nhiều chat riêng cùng lúc (#165)
- Một lịch gửi được kết quả tới tối đa 4 chat riêng (Telegram chat riêng, Slack DM) ngoài nơi nhận chính — vd báo cáo sáng về cả Telegram
  lẫn Slack: `cfharness schedule edit <id> --also telegram:<chat_id> --also slack:D…` (`--no-also` để bỏ; `schedule add` cũng nhận `--also`).
- Chỉ người đặt được (điều phối viên không thể tự thêm chat nhận); chat không còn trong allowlist của bot thì không nhận. Trang lịch trên web
  hiện các kênh gửi thêm (chỉ xem).

### Daemon — không còn lặp "thoát 1" sau `cfharness update` trên macOS (#166)
- Đã cài tự khởi động (LaunchAgent) thì `cfharness update` và lệnh tự khởi động daemon nhờ launchd chạy daemon (`launchctl kickstart`, giữ
  đúng `--home`) thay vì bật một bản ngoài launchd. Trước đây bản ngoài launchd giữ `daemon.lock`, job launchd thua khoá, thoát 1 và bị
  launchd chạy lại vô hạn (hàng nghìn lần/ngày). Job launchd lỡ thua khoá giờ chờ khoá (thử lại mỗi 30 giây) và tiếp quản ngay khi bản kia dừng.
- Phát hiện và sửa bởi team agent giám sát của hub.

#### Lưu ý khi cập nhật
- Hub đang chạy daemon NGOÀI launchd (kiểm: `launchctl print gui/$(id -u)/com.cfharness.daemon.<…>` thấy `state = spawn scheduled`, `runs`
  rất lớn): lần cập nhật này vẫn đi qua đường cũ. Sau khi cập nhật xong, đưa daemon về launchd một lần:
  `cfharness daemon stop && launchctl kickstart -k gui/$(id -u)/com.cfharness.daemon.<…>` (khi không có run đang chạy).

---

## v0.2.23 — 2026-11-20

### Agent tự dùng connector đã Connect trên tài khoản Claude (#164)
- `cfharness config set connectors.auto true`: mọi agent chưa đặt connector riêng tự dùng MỌI connector (Google Drive, Gmail, Docs…) mà tài khoản claude.ai đăng nhập `claude /login` của hub đã Connect — không phải khai từng agent. Chỉ bật khi hub có phiên `/login` (token setup-token không nạp connector). Không áp cho điều phối viên và run kỹ thuật (review/eval/curator). Guard vẫn áp: đọc cho qua, ghi được ghi nhật ký, xoá phải duyệt; MCP lạ của `~/.claude` vẫn bị chặn.
- `--connectors all` cho một agent (hoặc điều phối viên: `manager capabilities set --connectors all`) = mọi connector đã Connect.
- `scripts/setup-hub-user.sh`: `CONNECTORS=auto` (đăng nhập `/login` + bật `connectors.auto`); `CONNECTOR_AGENTS=all` bật connector cho mọi agent; connector được GỘP với connector agent đang có thay vì ghi đè. (#163)

#### Lưu ý khi cập nhật
- `connectors.auto` tắt mặc định — không đổi gì cho đến khi người bật. Khi bật, agent ghi bằng tài khoản thật (Gmail, Slack…) không hỏi duyệt trừ thao tác xoá: chỉ Connect trên claude.ai những connector muốn agent dùng.

---

## v0.2.22 — 2026-11-20

### Connector claude.ai trên hub dùng token chung (#162)
- Agent/điều phối viên bật connector claude.ai (`--connectors "Google Drive"`…) giờ dùng được connector trên hub đăng nhập bằng token `claude setup-token`: token này không nạp connector nào, nên run có connector tự chạy bằng phiên `claude /login` của user OS khi có (`runtime.claude.connectors_auth`: `auto` mặc định, `login`, `token`). Chưa đăng nhập `/login` → run vẫn chạy bằng token như cũ và có event `connector.auth_fallback`; connector chưa nối trên claude.ai (vd Google Drive `needs-auth`) → event `connector.unavailable` thay vì im lặng. `cfharness hub doctor --probe` có thêm mục "connector claude.ai". Hướng dẫn: `docs/dev-notes/manager-connectors.md`.
- `scripts/setup-hub-user.sh` thêm bước nối connector: `CONNECTORS="Google Drive" CONNECTOR_AGENTS="<agent…>"`; hub đã có thì `sudo ONLY_CONNECTORS=1 CONNECTORS="Google Drive" CONNECTOR_AGENTS="<agent>" bash scripts/setup-hub-user.sh <user>` chỉ nâng cfharness, đăng nhập `claude auth login` cho user hub và bật connector (không đụng token, tunnel, TOTP).

#### Lưu ý khi cập nhật (connector)
- Hub đã bật connector và user OS của hub CÓ phiên `claude /login`: run có connector giờ chạy bằng tài khoản của phiên đó (hạn mức/chi phí tính vào tài khoản đó, không theo `cfharness account use`). Muốn giữ token chung như cũ: `cfharness config set runtime.claude.connectors_auth token`.

### Web — bật connector claude.ai cho từng agent (#161)
- Form sửa/tạo agent có phần **Connector claude.ai**: tick connector (gợi ý từ danh sách của điều phối viên, connector đang bật và connector
  mà `allowed_tools` cần) hoặc gõ tên khác. Đổi connector phải xác nhận (agent ghi bằng tài khoản thật). Trước đây chỉ đặt được bằng
  `cfharness agent edit <id> --connectors`.
- Trang agent và form báo đỏ khi `allowed_tools` cho gọi `mcp__claude_ai_X__*` mà connector X chưa bật. Đây là nguyên nhân jira-ops và
  jira-analyst không thấy Atlassian MCP (run khởi động với `--setting-sources ""` + `--strict-mcp-config` nên không nạp connector).

### Log hệ thống và nhật ký kiểm toán — `docs/dev-notes/plan-logging.md` (#150, #153, #155, #156, #157, #158, #159, #160)
- Web và `cfharness mcp serve` giờ ghi log JSON có che secret như daemon (`logs/web.jsonl`, `logs/mcp.jsonl`); mỗi dòng log có thêm tiến trình, pid và vị trí mã. Log của thư viện (asyncio, mcp, uvicorn…) vào cùng file thay vì chỉ ra stderr; cảnh báo không còn bị ghi hai lần (`hub.log` + `daemon.out`).
- `cfharness logs level` xem level log đang áp dụng; `cfharness logs level cfharness.guard debug --for 30m` bật DEBUG tạm cho một phần hub, tự về mức cũ khi hết giờ — không cần khởi động lại daemon. Đổi lâu dài: `cfharness config set logs.level …` (có hiệu lực ngay).
- Lỗi trước đây chết im lặng giờ được ghi lại: tác vụ nền lỗi, lỗi trong thread, lỗi không ai bắt, listener event lỗi (không còn làm hỏng việc phát event). Daemon treo: `cfharness daemon dump-stacks` ghi stack mọi thread vào `logs/faults.log`.
- `web.log`, `update.out`, `update.log`, `import-job.out` được xoay vòng khi quá `logs.max_bytes`.
- Mỗi lệnh (CLI, web, TUI, MCP, Telegram/Slack, lịch, điều phối viên) có một mã yêu cầu `r_…` đi theo suốt: dòng audit, run, event, quyết định guard và log đều mang cùng mã. Lỗi từ CLI in kèm `(mã yêu cầu r_…)` để tra trong `logs/hub.log`. `hub.log` có thêm một dòng cho mỗi lời gọi RPC (method, thời lượng, kết quả — không có nội dung); lời gọi chậm hơn `logs.slow_rpc_ms` (mặc định 2000 ms) được cảnh báo `rpc.slow`. Daemon cập nhật lên DB phiên bản 28 (chỉ thêm cột) (L2).
- Nhật ký kiểm toán đầy đủ hơn (L3): mỗi dòng ghi **ai** làm do daemon xác định (`principal`: `os:<user>` cho CLI/TUI, `web:dev_…`/`web:sess_…` cho từng thiết bị/phiên web — chỉ là mã băm, `telegram:<id>`, `slack:<id>`, `mcp:stdio`/`mcp:http:<mức token>`, `manager:<agent>`, `agent:<id>`), **kết quả** (`ok`/`error`/`denied` + mã lỗi) và **thời lượng**. Thao tác bị từ chối hoặc lỗi giờ cũng để lại dòng (trước đây chỉ ghi khi thành công). Web dùng token riêng nên kênh `web` không còn giả được từ client khác (kênh client tự khai lưu riêng ở `declared_channel`); qua link public có thêm ip. Lượt đọc dữ liệu nhạy cảm (danh sách secret, transcript run, tìm hội thoại/memory, graph memory, quyết định guard, đọc audit, `hub doctor`, level log) được ghi `read:<method>` (chỉ đích + số bản ghi, không nội dung; gộp mỗi 10 phút). Gỡ mẫu chặn repo (`guard repo-deny rm`) có dòng audit; dòng trust/grant/sandbox ghi đúng người và kênh. `cfharness audit list` thêm cột kết quả + principal; đọc audit giờ chỉ người (agent bị chặn).
- Công cụ debug (L6): `cfharness logs --since 2h --level warning --logger cfharness.guard --req r_… --run … --proc web` lọc log theo trường (mọi tiến trình, kể cả file đã xoay vòng và `.gz`), `-f` theo dõi dòng mới; daemon dừng vẫn đọc được file. `cfharness trace <mã yêu cầu|run|task>` ghép một dòng thời gian: audit, log, event (chỉ tóm tắt), quyết định guard, phê duyệt và run con. Web có trang "Nhật ký hệ thống" (`/logs`) và trang dòng thời gian (`/trace/<mã>`); bấm mã yêu cầu ở Nhật ký kiểm toán hoặc "Dòng thời gian" ở trang run. Thẻ Sức khoẻ hiện số lỗi/cảnh báo trong log 1 giờ/24 giờ theo logger, số RPC chậm và lỗi không ai bắt; `hub doctor` thêm mục "Lỗi gần đây" (top 5 lỗi 24 giờ, kèm mã yêu cầu mẫu). Agent không còn đọc được `HUB_HOME/logs/`.
- Nhật ký kiểm toán chống sửa (L4): mỗi dòng audit mới được nối vào một chuỗi băm (sha256); hub định kỳ (mỗi giờ, `audit.anchor_interval_s`) và khi daemon dừng ghi "bản neo" của dòng cuối vào `audit-anchors.jsonl` (và vào file metadata của quản trị máy khi bật xuất trạng thái admin). `cfharness audit verify [--since-id N] [--json]` kiểm toàn bộ chuỗi + bản neo, báo đúng dòng bị sửa/xoá; ai sửa DB rồi tính lại cả chuỗi vẫn bị bản neo bắt. Phát hiện sửa/xoá (kể cả trong lúc daemon dừng — daemon kiểm phần đuôi khi khởi động) → cảnh báo `audit.tamper_detected` qua Telegram/Slack. Gửi bản neo ra Telegram/Slack để giữ bằng chứng ngoài máy: `cfharness config set audit.anchor_notify true` (tắt mặc định). Bản neo đi cùng bản sao lưu và gói xuất hub nên khôi phục/nhập không báo nhầm.

- Truy vấn audit + cảnh báo (L5): `cfharness audit list` lọc thêm `--principal --actor --outcome denied --req r_… --since 24h --until … --q <chữ>` (`--q` chỉ tìm trong tóm tắt/đích, không bao giờ trong dữ liệu) và phân trang `--before-id N`; `cfharness audit export --since 7d [--format jsonl|csv]` ghi `HUB_HOME/exports/audit-<giờ>.<đuôi>` (0600, kèm cột băm để kiểm lại). Trang web "Nhật ký kiểm toán" có các bộ lọc này, cột kết quả có màu, nút "Cũ hơn" và thẻ "Hoạt động theo người" (số dòng theo danh tính × ngày, 7 ngày, kèm số lần bị từ chối). Hub tự cảnh báo `audit.alert` qua Telegram (nhóm bảo mật) / Slack khi thấy mẫu đáng ngờ: ≥ 5 thao tác bị từ chối / 10 phút cùng một danh tính, ≥ 5 lần đăng nhập web sai / 15 phút (theo IP; toàn cục khi từ nhiều IP), đổi cấu hình an toàn (`gates.mode`, `approval.profile`, `guard.enabled`, `guard.sandbox.*`, `workspace.confine`, `audit.*`, `audit_watch.*`, bật trust full, cấp quyền theo luật, ngoại lệ sandbox), danh tính mới lần đầu xuất hiện (thiết bị web tin cậy, user Telegram/Slack, token MCP…), ≥ 30 lượt đọc dữ liệu nhạy cảm / 10 phút, nhật ký/chính sách bị sửa ngoài hub; truy cập secret ngoài giờ khi đặt `audit_watch.secret_access.hours` (vd `"08:00-19:00"`). Mỗi cảnh báo chỉ báo một lần cho tới khi hết, và có tin "Hết cảnh báo" khi qua trọn cửa sổ không còn lặp lại. Ngưỡng ở `audit_watch.*` (chỉ người đổi, được kiểm toàn vẹn). Tuỳ chọn lưu trữ audit cũ (tắt mặc định — audit vẫn giữ vĩnh viễn trong DB): `cfharness config set audit.archive_after_days 365` dồn các tháng đã trọn ra `HUB_HOME/audit-archive/<yyyy-mm>.jsonl.gz` (giữ chuỗi băm, có `MANIFEST.json`) rồi mới xoá khỏi DB; `audit verify` vẫn kiểm được chuỗi, `audit verify --archive` còn kiểm từng file lưu trữ. Agent không đọc/ghi được `audit-archive/`.
- Gói chẩn đoán (L7): `cfharness support bundle [--since 24h] [--run <id> …]` in danh sách file + cỡ, hỏi xác nhận rồi ghi `HUB_HOME/exports/support-<giờ>.tar.gz` (0600) để gửi người hỗ trợ khi gỡ lỗi: log hệ thống trong khoảng thời gian chọn (cả file đã xoay vòng), `hub doctor`, trạng thái hub, phiên bản (cfharness, claude, python, OS), cấu hình đã lọc (khoá nhạy cảm bị che, nhánh `web` chỉ giữ bật/tắt), dòng thời gian (chỉ metadata) của các run chọn, và `MANIFEST.json` (sha256 từng file + số chỗ đã che theo file). Mọi byte được quét che secret (theo giá trị + mẫu token) ngay trước khi ghi; không bao giờ có hub.db, secret, đăng nhập web, token, session Claude, checkpoint, bản sao lưu, memory/skill. Raw transcript chỉ khi thêm `--include-raw` và xác nhận bước 2 (gõ `raw`; `--yes` không bỏ qua bước này). Trần `support.max_mb` (mặc định 200) và `support.max_file_mb` (50). Daemon dừng vẫn tạo được. Web: thẻ "Gói chẩn đoán" ở Cài đặt — xem trước, tạo (raw cần bước 2), tải về bằng link dùng một lần. `update.log` và `import-job.out` ghi dòng JSON cùng dạng `hub.log` (trang cập nhật vẫn hiện chữ như cũ). Agent không đọc được `exports/`, `imports/`, `backups/` kể cả qua lệnh shell.
- Ít lỗi chết im lặng hơn (L8): khoảng 300 chỗ hub bắt lỗi rồi bỏ qua giờ hoặc ghi log kèm traceback (DEBUG cho việc phụ, WARNING cho lỗi đáng ngờ như kiểm tài nguyên bảo vệ của workspace, eval gate lỗi hạ tầng, đẩy cây lane lỗi), hoặc được đánh dấu rõ là cố ý (`fail-closed` của guard: lỗi = chặn; `best-effort`). Bật `cfharness logs level cfharness debug --for 30m` để thấy cả lỗi phụ khi gỡ rối. Lệnh dừng run (timeout, ESTOP, dừng tay, chờ người) và vài tác vụ nền khác giờ ghi ERROR có tên tác vụ nếu lỗi thay vì mất dấu. Không đổi hành vi chặn/cho phép của guard.

#### Lưu ý khi cập nhật (L5)
- Daemon nâng DB lên phiên bản 31 (chỉ thay trigger `audit_no_delete`: vẫn chặn xoá mọi dòng audit, trừ dòng đã chuyển ra `audit-archive/` khi bạn bật `audit.archive_after_days`). Lần đầu chạy sau cập nhật, hub nạp danh sách danh tính đã thấy từ audit sẵn có nên không cảnh báo "danh tính mới" cho những danh tính cũ. Danh sách `notify.<kênh>.events` đã sửa tay sẽ không tự có `audit.alert` — thêm vào nếu muốn nhận cảnh báo.

#### Lưu ý khi cập nhật (L4)
- Daemon nâng DB lên phiên bản 30 (thêm `prev_hash`/`row_hash` vào `audit_log`). Dòng audit cũ để trống và không được kiểm; chuỗi bắt đầu từ dòng đầu tiên sau khi cập nhật. Danh sách `notify.<kênh>.events` đã sửa tay sẽ không tự có `audit.tamper_detected` — thêm vào nếu muốn nhận cảnh báo.

#### Lưu ý khi cập nhật (L3)
- Daemon nâng DB lên phiên bản 29 (chỉ thêm cột vào `audit_log`, dòng cũ để trống).
- **Khởi động lại web (`cfharness web`) và `cfharness mcp serve` sau khi cập nhật daemon** để dùng token riêng mới (`<socket>.web.auth`, `<socket>.mcp.auth`). Web/MCP chạy từ trước vẫn hoạt động nhưng được ghi là `os:<user>` với kênh `cli` (kênh tự khai nằm ở `declared_channel`).

---

## v0.2.21 — 2026-11-18

### Lưu ý khi cập nhật
- **Đồng bộ 2 chiều cũ với Không gian số đã bỏ** (tiến trình `tang-thu-sync-headless`, đăng nhập thiết bị, bản sao `~/KhongGianSo/…`):
  thư mục chung giờ CHỈ đi qua cổng cfharness. Hub còn thư mục đồng bộ cũ thì sau khi cập nhật thấy "Thư mục X đang dùng đồng bộ cũ
  (đã bỏ) — dán khoá cổng cfharness ở Cài đặt để dùng tiếp; bản sao trên máy để nguyên" (Cài đặt › Không gian số, `cfharness filehub
  status`); run/task giao vào thư mục đó bị từ chối với cùng câu. Dán khoá cổng (tạo ở Không gian số › Hồ sơ › Cổng cfharness, cấp đúng
  thư mục/workspace đó) là hub tự chuyển thư mục sang cổng — giữ tên `filehub:<tên>`. Hub KHÔNG xoá gì: bản sao trên máy, `HUB_HOME/filehub/`
  để nguyên (kiểm rồi tự xoá); thu hồi thiết bị đồng bộ cũ ở Không gian số › Hồ sơ › Thiết bị. Tiến trình đồng bộ còn chạy từ bản cũ được
  hub dừng khi khởi động. Khoá cấu hình cũ (`filehub.sync_binary`, `local_parent`, `device_name`, `mode`…) không gây lỗi, được bỏ qua.
- Lệnh `cfharness filehub setup|login|add|pause|switch|switch-to-workspace` không còn (chỉ in hướng dẫn); `filehub resume` cần `--root`
  (lô xoá đang giữ của cổng). `scripts/setup-hub-user.sh`: `KGS=…` luôn đi qua cổng (biến `KGS_MODE`, `KGS_SCOPE`… vẫn nhận, bỏ qua).
- Không có migration DB.

### Không gian số — chỉ còn cổng cfharness (H5, #154)
- Gỡ đồng bộ 2 chiều cũ: tiến trình giám sát binary sync, đăng nhập thiết bị + relay OAuth qua web public (`/filehub/oauth/cb`, trang
  `/filehub/login`), bản sao cục bộ, chặn xoá hàng loạt của binary, chuyển chế độ sync ↔ cổng. Thẻ Cài đặt gộp thành một thẻ "Không gian số —
  cổng cfharness" (thêm đổi tên / gỡ thư mục, báo thư mục cần chuyển).
- Thư mục đồng bộ cũ tự chuyển sang cổng khi khoá được cấp đúng gốc (khi dán khoá, "Thử kết nối" hoặc kiểm tra nền mỗi phút).
- Link "Mở trên Không gian số" (chip tệp ở diff của run, tool `filehub_link` của điều phối viên) dùng id do cổng trả.
- Nhập gói hub cũ (`hub import`) còn cấu hình đồng bộ: bỏ khoá của đồng bộ, thư mục thành "cần chuyển" (`move`) / gỡ (`clone`);
  `hub doctor` báo thư mục cần chuyển và cổng chưa có khoá.
- Hub chỉ dán khoá cổng (chưa đặt `filehub.server`) giờ có link "Mở trên Không gian số": cổng báo địa chỉ web công khai trong `hello`
  (`server.public_url`, cần Không gian số bản mới — bản cũ không gửi thì như trước). Hub kiểm chặt (chỉ https, không đường dẫn/userinfo), tự
  đặt `filehub.server` khi còn trống và cập nhật nếu cổng đổi địa chỉ; giá trị người đặt không bao giờ bị ghi đè; nhiều cổng → link mỗi cổng
  về web của chính nó.

---

## v0.2.20 — 2026-11-18

Hoàn thiện cổng cfharness: hoàn tác run trên Không gian số, nút duyệt lô xoá trên Telegram/Slack, mọi run đọc được thư mục chung qua cổng,
chặn run đọc/ghi cây làm việc của run khác; sửa test chập chờn của nhịp điều phối viên (#151).

### Lưu ý khi cập nhật
- Không có migration DB của hub. Cổng cfharness giờ có trên cả hai hệ Không gian số (bản chính cổng 8223, bản CCHC cổng 8233) — hub nối
  CCHC dán khoá cổng là chuyển được (hub tự thử 8223 rồi 8233).
- Mặc định mọi run đọc được thư mục chung chế độ cổng; muốn tắt: `cfharness config set filehub.lane.read_all_runs false`.

### Không gian số — cổng cfharness (#151)
- Cổng cfharness: hoàn tác run làm việc trên thư mục chung (`cfharness run revert <run>`, nút "Hoàn tác trên Không gian số" ở trang run) —
  khôi phục phiên bản trước run, lấy lại tệp run xoá từ thùng rác, chuyển tệp run tạo vào thùng rác, đổi tên lại; từ chối khi tệp đã bị
  sửa tiếp sau run. Run cũ (trước bản này) vẫn chỉ khôi phục được trên Không gian số.
- Telegram/Slack: nút "Cho xoá"/"Không xoá" cho lô xoá hàng loạt đang giữ của cổng cfharness (2 bước, bước 2 liệt kê danh sách hiện tại).
- Cổng cfharness: mọi run (kể cả run không làm trong thư mục chung) đọc được thư mục chung chế độ cổng qua tool đọc `fs_list`/`fs_read`…
  (tắt: `filehub.lane.read_all_runs: false`); run không đọc/ghi được cây làm việc của run khác; dải "cfharness đang làm việc" trên Không gian
  số vẫn giữ khi run chờ bạn duyệt.

### Điều phối viên
- Sửa lỗi nhịp đầu tiên của điều phối viên đôi khi bị bỏ lượt (mốc thời gian lệch mili-giây) (#151).

---

## v0.2.19 — 2026-11-18

Cổng cfharness: agent làm việc thẳng trên Không gian số cùng máy, realtime hai chiều, thay đồng bộ 2 chiều thường trực — dán khoá là dùng được
ngay (#147, #148). Kèm sửa bản sao ngoài vùng giam (#141), `cfharness update` tự chạy lại web (#144), gỡ MCP đã xoá (#145), `RESET=1` (#146).

### Lưu ý khi cập nhật
- Không có migration DB của hub. Cấu hình mới đều có mặc định; hub đang dùng đồng bộ cũ (`filehub.mode: sync`) **không đổi gì** cho tới khi
  người dán khoá cổng.
- Muốn dùng cổng: Không gian số phải là bản có cổng cfharness (đã deploy 2026-11-18, cổng `127.0.0.1:8223`) và Super Admin bật "Cho phép
  cổng cfharness" ở Hệ thống › Chính sách. Sau đó Workspace Admin tạo khoá ở Hồ sơ › Cổng cfharness → dán vào cfharness › Cài đặt › Cổng
  cfharness. Bản sao `~/KhongGianSo/<tên>` của đồng bộ cũ được để nguyên — kiểm rồi tự xoá khi đã chuyển.
- Hệ Không gian số CCHC (cổng 8233) chưa có cổng cfharness — hub nối CCHC tiếp tục dùng đồng bộ cũ.

### Cài đặt
- `scripts/setup-hub-user.sh` thêm `RESET=1`: làm lại hub của một user từ đầu — dừng dịch vụ, CẤT (không xoá) dữ liệu hub, Workspace, session Claude cũ sang `~/.reset-<giờ>`, hỏi gõ "reset" để xác nhận (#146).

### Không gian số (thư mục chung)
- Hub đang giam agent trong một workspace (`workspace.confine`) mà thư mục cha của bản sao (`filehub.local_parent`, mặc định `~/KhongGianSo`) nằm ngoài vùng giam → bản sao tự đặt ở `<vùng giam>/KhongGianSo/<tên>` thay vì báo "nằm ngoài workspace được giam" (nối thư mục qua web public không sửa được đường dẫn nên bị kẹt). `filehub setup --local-parent` ngoài vùng giam bị từ chối ngay, không ghi cấu hình (#141).

### Không gian số — cổng cfharness (chế độ `lane`, chưa bật mặc định) (#147, #148)
- `cfharness filehub lane setup --url http://127.0.0.1:8223` (khoá hỏi bằng getpass, chỉ lưu trong secret store), `lane status|test|disable`:
  nối hub với cổng cfharness của Không gian số trên cùng máy — chỉ nhận URL loopback, khoá không bao giờ vào cấu hình/nhật ký/gói nhân bản.
  Hub đang dùng đồng bộ cũ không đổi gì cho tới khi người chuyển thư mục sang `lane`.
- Run trên thư mục chế độ `lane` (`filehub:<tên>[/<con>]`) có cây làm việc riêng dựng từ Không gian số lúc bắt đầu (đúng phạm vi ref) và
  xoá khi run xong; người sửa/xoá/di chuyển tệp trong phạm vi → hub kéo về và nhắn agent ngay (thường < 1 s). Run chỉ đọc hoặc phạm vi quá
  lớn: cây rỗng + tool `fs_list|fs_read|fs_stat|fs_versions|fs_link|fs_pull`. Agent không bao giờ gọi được cổng trực tiếp (bị chặn).
- Agent lưu tệp trong cây → lên Không gian số trong vài giây thành phiên bản mới (không bao giờ ghi đè bản người vừa lưu: giữ bản của agent
  thành tệp `<tên> (cfharness <agent> <giờ>)`); tệp đang bị người khoá → xếp hàng, mở khoá là lưu. **Tệp chứa giá trị secret của hub bị
  chặn TRƯỚC khi đẩy** (không một byte nào rời máy, agent được báo). Hết run còn thay đổi chưa đẩy được → giữ ở cây mồ côi
  (`cfharness filehub lane orphan push|drop <run>`, tự dọn sau 7 ngày). Diff của run lấy từ nhật ký thao tác; hoàn tác ở phiên bản/thùng rác
  Không gian số.
- Chuyển từng thư mục: `cfharness filehub switch <tên> --to lane|sync` (từ chối khi đồng bộ còn dở / gốc chưa được cấp cho khoá / có run
  đang ghi) — bản sao `~/KhongGianSo/<tên>` cũ **để nguyên** (hub không xoá), được báo ở `filehub lane status` và thẻ web. Thư mục mới:
  tự thêm khi dán khoá (xem dưới); ca tay `cfharness filehub lane add <id gốc>`. Thẻ "Cổng cfharness" ở Cài đặt web (khoá chỉ nhập ở bước 2). Trên Không gian số thấy dải
  "cfharness · <agent> · đang sửa <tệp>" trong lúc run ghi. `setup-hub-user.sh`: `KGS_MODE=lane`.
- An toàn hơn khi agent làm việc trên thư mục chế độ `lane`: hub không bao giờ đi xuyên symlink agent đặt trong cây (xoá/di chuyển/đọc để
  đẩy đều từ chối); tệp tạm/khoá Office/`.git` của người trên Không gian số không bao giờ bị xoá; agent xoá tệp mà người vừa lưu bản mới →
  không xoá, kéo bản của người về; tệp lớn chưa tải không bị ghi đè bằng nội dung cụt (bản của agent thành tệp riêng, nhắc `fs_pull`); hai
  bản xung đột cùng phút không đè nhau; người xoá thư mục thì tệp agent chưa lưu bên trong được giữ thành bản riêng. **Xoá hàng loạt**
  (> `filehub.lane.max_delete_batch`, mặc định 20, mỗi run) bị GIỮ, báo người — `cfharness filehub resume --root <id>` để xoá tiếp, thêm
  `--discard` để không xoá và kéo tệp về lại — hoặc nút "Cho xoá"/"Không xoá" ở thẻ "Cổng cfharness" (Cài đặt web). Tool `fs_delete` của
  agent đi cùng đường xoá đó (cùng trần, không xoá tệp tạm/khoá/`.git` của người và giữ thư mục còn chứa chúng). Xoá thư mục: tệp trước, thư mục sau (Không gian số chỉ xoá thư mục rỗng). Ghi lên Không gian số chờ quét virus tới `filehub.lane.write_timeout_s` (mặc định 360 s) và
  gửi lại an toàn (cùng Idempotency-Key). `cfharness filehub status` liệt kê cả thư mục chế độ lane. Run chỉ đọc không bao giờ đẩy (kể cả
  sau khi daemon khởi động lại); người di chuyển tệp lên đúng tên agent vừa tạo → bản của agent thành tệp riêng; người sửa tệp lớn chưa tải
  → không tải cả tệp mỗi lần.
- **Dán khoá là chạy được luôn**: `filehub lane setup` / thẻ "Cổng cfharness" tự thêm MỌI thư mục/workspace khoá được cấp thành thư mục
  chung chế độ lane (tên theo Không gian số, tự thêm số nếu trùng; workspace → cả workspace) — giao việc `filehub:<tên>` được ngay, không
  cần `lane test`/`lane add` hay id gốc. Hub tự đối soát mỗi lần nói chuyện với cổng: cấp thêm thư mục → tự thêm; thư mục bị thu hồi →
  báo "mất quyền" (không xoá gì); thư mục đang đồng bộ kiểu cũ cùng gốc → giữ nguyên, có nút "Chuyển sang cổng". Báo qua Telegram/Slack
  (event `filehub.lane.roots`). Bỏ trống địa chỉ → hub thử 8223 rồi 8233, tên cổng theo Không gian số; trên web chỉ cần dán khoá.
  `setup-hub-user.sh KGS_MODE=lane` kết thúc với thư mục dùng được ngay. `lane add` vẫn còn cho ca tay; "Xoá khoá" gỡ luôn các thư mục chỉ
  có qua cổng đó (tệp trên Không gian số giữ nguyên, dán lại khoá là về).
- Điều phối viên và `filehub.list`/`filehub status` thấy thư mục chế độ lane (chế độ, loại, trạng thái `ok|revoked|offline…`); thẻ
  "Không gian số" không còn báo "chưa thiết lập" khi hub dùng cổng; tên thư mục gõ sai → lỗi liệt kê các tên có thật.
- Thẻ "Cổng cfharness": trạng thái bằng chữ + việc cần làm (vd khoá bị thu hồi → "tạo khoá mới … rồi dán lại", không hiện "còn N ngày"),
  hạn khoá theo ngày giờ máy + số ngày nguyên, nút "Thử kết nối", mọi thao tác quay về đúng thẻ. Hub tự kiểm tra cổng định kỳ (cả lúc không
  có run): "không kết nối được" tự về "đang hoạt động" khi Không gian số chạy lại. Lô xoá hàng loạt đang giữ liệt kê đường dẫn (50 mục đầu
  + "và N mục nữa") trên thẻ, trang xác nhận và tin Telegram/Slack.
- Lô xoá hàng loạt đang giữ vẫn hiện "N mục xoá đang chờ duyệt" + "Cho xoá"/"Không xoá" (cùng nghĩa) sau khi run đã kết thúc, và có ở
  trang "Cần bạn" (task bị chặn vì lô xoá có nút tới lô đó). Thay đổi còn kẹt sau run ghi rõ thao tác từng mục (xoá / ghi / di chuyển) + đường
  dẫn; trang xác nhận nói thẳng "Xoá N tệp trên Không gian số …" / "Không xoá — giữ N tệp, bỏ thay đổi của agent"; "Làm lại trên Không gian
  số" không bao giờ ngầm xoá lô đang giữ. Ô địa chỉ "Nâng cao" để trống (tự thử 8223 rồi 8233). Cổng được kiểm lại mỗi phút (khoá bị thu hồi
  / Không gian số tắt thấy trong ≤ 1 phút), thẻ ghi "kiểm tra lúc …". Tool đọc của điều phối viên (`filehub_list`…) không còn ghi nhật ký như
  thao tác ghi; điều phối viên thấy lý do task đang chờ bằng chữ rõ (vd chờ slot chạy) thay vì mã thô.


### Cập nhật
- `cfharness update` không còn bảo "tắt và mở lại giao diện web" — người dùng hub chạy trên máy chủ (web qua LaunchDaemon, vào bằng link
  public) không làm được việc đó. Giờ lệnh chờ giao diện web tự chạy lại bằng bản mới (tối đa 2 phút), quá giờ thì tự chạy lại web, rồi báo
  kết quả; chỉ nhắc mở lại `cfharness chat`/`top` khi thật sự đang có cửa sổ như vậy. Trang Cài đặt web cũng thôi bảo bấm Ctrl+C (#144).

### MCP
- **Gỡ được MCP đã xoá khỏi agent/điều phối viên**: `cfharness mcp unassign <server> <agent>` không còn đòi server phải còn trong registry,
  và sửa agent không còn bị chặn vì một MCP đã gán từ trước nay không còn. Lỗi "điều phối viên … MCP 'x' không có trong registry" giờ
  chỉ thẳng lệnh sửa (#145).

---

## v0.2.18 — 2026-11-17

Điều phối viên dùng được MCP trusted (chỉ đọc) ngay với khung mặc định, và sửa cờ dòng lệnh `cfharness mcp add|edit` (#140, #142).

### Điều phối viên
- Khung quyền mặc định `strict` nay cho dùng MCP **trusted ở chế độ chỉ đọc** (`mcp: read_only_trusted`): điều phối viên gắn MCP
  đã đánh dấu trusted không còn bị chặn "vượt khung" mà không phải chạy `cfharness manager capabilities set --mcp read_only_trusted`.
  Tool MCP ghi vẫn bị chặn tĩnh, MCP untrusted vẫn bị từ chối; muốn tắt hẳn: `cfharness manager capabilities set --mcp none` (#142).

### MCP server
- `cfharness mcp add|edit <tên> --url … --oauth …`: cờ đặt SAU tên được đọc đúng — trước đây bị lưu lặng lẽ thành lệnh stdio
  (`--transport …`). Lệnh stdio vẫn viết sau `--` (#140).
- `mcp edit --transport http` (và sửa trên web) từ server stdio tự bỏ `command`/`args`/`env` cũ — không còn lỗi "transport http không dùng 'command'" (#140).
- Trang MCP server liệt kê cả redirect URI qua tên miền public (khi đang vào web qua đó) để khai cho app OAuth tạo tay ở nhà cung cấp (#140).

### Lưu ý khi cập nhật
- Không có migration. Hub đã đặt `manager.capabilities.mcp: none` rõ ràng thì giữ nguyên (không tự mở MCP). Điều phối viên chỉ dùng
  được MCP đã đánh dấu trusted — MCP untrusted vẫn báo lỗi kèm lệnh `cfharness mcp edit <tên> --trust trusted`.

---

## v0.2.17 — 2026-11-17

Trang "Hệ thống ngoài" trên web + chat, `hub doctor` báo dung lượng và dọn thư mục mồ côi, tìm hội thoại cũ trong `cfharness chat`,
đổi tên thư mục chung Không gian số, bộ tự kiểm bảo mật `cfharness selftest --live`, và proxy loopback cho MCP OAuth (bật mặc định —
run dài không còn 401, token không còn nằm trong file của run) (#129–#137).

### Hệ thống ngoài + Sức khoẻ hub
- **Trang "Hệ thống ngoài" trên web** (`/systems`, nhóm "Hệ thống" ở thanh bên): xem các app trong thư mục làm việc và job launchd của chúng
  (trạng thái, pid, ai tạm dừng, lịch), quét tìm + đăng ký hệ thống mới, thêm theo đường dẫn, gỡ, khoá/mở khoá job, tạm dừng / bật lại /
  khởi động lại job; trang chi tiết có đuôi log (đã che secret). Mọi thao tác có hộp xác nhận và ghi nhật ký kênh `web`.
- **Lệnh chat `/hethong`** (Telegram; Slack: `/cfh hethong`): xem hệ thống ngoài + job, nút Dừng / Bật lại / Khởi động lại từng job —
  luôn 2 bước. Đăng ký, gỡ, khoá chỉ làm ở web/CLI.
- **Sức khoẻ: dung lượng HUB_HOME + dọn thư mục làm việc mồ côi**: `cfharness hub doctor` và thẻ Sức khoẻ (Cài đặt web) báo dung lượng
  theo thư mục (logs, checkpoints, backups, exports, workspaces của agent…), chỗ trống ổ đĩa, và các thư mục làm việc không còn dùng —
  thư mục của hội thoại điều phối viên đã lưu trữ/không còn, workspace/worktree không run/task nào tham chiếu, workspace của run đã xong
  quá 30 ngày, git worktree của task đã xong. Mặc định CHỈ BÁO; dọn bằng `cfharness hub doctor --cleanup [--older-than NGÀY]` (liệt kê
  rồi hỏi y/N, ghi nhật ký kiểm toán). Không bao giờ xoá run/ledger/audit hay thư mục đang có run chạy; git worktree ngoài HUB_HOME chỉ
  được gợi ý lệnh `git worktree remove`.

### Không gian số (thư mục dùng chung)
- **Báo sau run cho thư mục ghi thêm**: run có `extra_write_roots` nằm trong thư mục chung (vd task làm ở scratch nhưng được ghi vào
  `~/KhongGianSo/<tên>/Báo cáo`) giờ cũng báo tệp đã xoá / đã đổi / có thể chứa bí mật sau run — như run có workspace `filehub:`.
- **Đổi tên thư mục chung trong cfharness**: `cfharness filehub rename <tên cũ> <tên mới>` hoặc "Đổi tên trong cfharness" ở thẻ Không gian số
  (Cài đặt). Chỉ đổi tên dùng để giao việc (`filehub:<tên>`); đường dẫn trên máy và tên trên Không gian số giữ nguyên, tên cũ vẫn khớp
  (task/agent đã ghi `filehub:<tên cũ>` chạy tiếp). Từ chối khi có run đang ghi vào thư mục hoặc thư mục chưa đồng bộ lần đầu.

### Tìm hội thoại cũ
- **`/search <từ khoá>` trong `cfharness chat`** (cả màn hình đầy đủ lẫn `--classic`): tìm trong mọi hội thoại với điều phối viên và
  session của các agent (có dấu/không dấu đều khớp, `"cụm từ"` trong ngoặc kép), kết quả gộp theo luồng, đánh số, kèm đoạn trích.
  `/open <số>` (hoặc click kết quả ở màn hình đầy đủ) mở hội thoại cli tìm được; luồng khác thì hiện đoạn hội thoại quanh chỗ khớp
  (chỉ xem) và gợi ý `/attach <run>`. Có trong menu `/` và bảng phím tắt `?`.
- **`cfharness session reindex`**: dựng lại chỉ mục tìm kiếm hội thoại khi kết quả tìm thiếu/lệch — nạp lại FTS5 và quét lại toàn bộ
  run/event/hội thoại để bù tin bị sót (`--fts-only` chỉ nạp lại FTS, `--wait` chờ xong). Chạy nền trong daemon, không xoá văn bản đã
  chỉ mục; chỉ người làm được (agent bị chặn), có ghi nhật ký kiểm toán. `cfharness session status` xem số tin đã chỉ mục và tiến độ.

### Bảo mật
- **`cfharness selftest --live` — tự kiểm bảo mật với `claude` thật trước khi phát hành**: tạo hub TẠM (daemon riêng, không đụng hub thật),
  cho một agent model rẻ (mặc định haiku) thử 12 thao tác bị cấm — ghi `policy/` (shell, python, tool Write), đọc secret / token người,
  gọi thẳng socket hub, `cfharness approve`, `cfharness config set`, curl dịch vụ loopback, ghi/đọc ngoài workspace khi giam — rồi đo
  hậu quả thật và báo ĐẠT / TRƯỢT / KHÔNG KẾT LUẬN từng ca. Trần chi phí cứng (`--budget-usd`, mặc định $0.50; mỗi run `--per-run-usd`
  $0.10), đạt trần thì dừng ngay và bỏ qua các ca còn lại; xong thì dọn sạch hub tạm. `cfharness selftest --dry-run` chỉ liệt kê ca,
  không tốn tiền. Lệnh chỉ người (agent gõ bị chặn). Hướng dẫn: `docs/dev-notes/selftest.md`.

### Tích hợp MCP
- **Proxy loopback cho MCP đăng nhập OAuth (bật mặc định)**: run không còn nhận token OAuth trong file mcp.json — `claude` nói
  chuyện với một proxy của daemon trên `127.0.0.1` (URL + khoá riêng từng run, hết hiệu lực khi run kết thúc), proxy chèn token mới nhất và tự làm mới khi sắp hết hạn hoặc khi server trả 401. Run
  dài hơn hạn access token (thường 1 giờ) không còn bị 401 giữa chừng. Hỗ trợ Streamable HTTP và SSE. Proxy không khởi động được →
  tự dùng lại cách cũ (Bearer trong mcp.json). Đã nghiệm thu với Claude Code 2.1.326 thật (guard + sandbox bật). Tắt nếu cần:
  `cfharness config set mcp.oauth_proxy.enabled false`.
- **Máy có `HTTP_PROXY`/`HTTPS_PROXY`**: tiến trình `claude` của run luôn có `127.0.0.1,localhost,::1` trong `NO_PROXY`, và daemon gọi
  máy chủ OAuth/MCP chạy trên máy này đi thẳng — trước đây request tới địa chỉ cục bộ bị đẩy qua proxy công ty và hỏng.
- **`cfharness mcp edit --url` giữ nguyên transport `sse`** (trước đây tự đổi sang `http`, làm hỏng server SSE cũ).

### Sửa lỗi
- **Web không lên lại sau khi khởi động lại máy (lỗi 502 qua tunnel)**: số tiến trình ghi trong `auth/web.json` bị tiến trình khác của
  hệ thống dùng lại sau khi máy khởi động → `cfharness web` tưởng đã có server đang chạy, in link đăng nhập rồi thoát, launchd bật lại
  mãi mà không ai nghe cổng. Giờ web so thời điểm tiến trình bắt đầu với lúc ghi trạng thái, nhận ra file cũ và tự chạy bình thường.
  Hub đang bị kẹt: xoá `~/.cfharness/auth/web.json` của user đó (launchd bật lại web trong khoảng 30 giây) (#136).
- **Kết nối MCP OAuth qua link public lỗi sau khi bấm Chấp nhận**: web luôn bảo nhà cung cấp quay về `http://127.0.0.1:<cổng>/…`,
  địa chỉ chỉ đúng khi trình duyệt chạy trên chính máy hub — người vào qua `https://<tên>.…` (tunnel) bị lỗi không kết nối được. Giờ vào
  qua link public thì quay về `https://<link public>/mcp/oauth/callback`; vào từ chính máy vẫn như cũ. MCP server đăng ký client tự động
  (DCR) tự nhận địa chỉ mới; server dùng `client_id` nhập tay cần khai thêm địa chỉ này ở phía nhà cung cấp (#137).

### Lưu ý khi cập nhật
- Không có migration.
- Proxy MCP OAuth bật sẵn: MCP server đăng nhập OAuth sẽ đi qua proxy của daemon trên `127.0.0.1`. Nếu gặp lỗi kết nối MCP, tắt bằng
  `cfharness config set mcp.oauth_proxy.enabled false` (quay về cách cũ) và báo lại. Máy có `HTTP_PROXY`/`HTTPS_PROXY` không cần làm gì.
- Sau khi cập nhật có thể chạy `cfharness selftest --live` (vài cent với haiku) để kiểm lớp bảo vệ trên máy mình.

---

## v0.2.16 — 2026-11-17

Thêm lệnh nhận bản chuẩn toàn vẹn sau khi cài lại cfharness (#127); test listener của daemon chạy được trên Linux/macOS (#126).

### Lệnh mới
- **`cfharness guard integrity status|accept`**: cảnh báo "môi trường Python của hub bị thay đổi" (vd sau khi cài lại cfharness) bảo chạy
  `cfharness guard integrity accept` nhưng CLI chưa có lệnh này. Giờ `status` xem lần kiểm gần nhất và các lệch gần đây, `accept` (chỉ
  người) nhận trạng thái hiện tại làm bản chuẩn, gồm cả `.pth`/`sitecustomize`.

### Lưu ý khi cập nhật
- Trên Windows vẫn phải dừng daemon và đóng mọi cửa sổ `cfharness` trước khi `uv tool install --force` (xem v0.2.15).
- Nếu sau khi cài lại có cảnh báo "môi trường Python của hub bị thay đổi", xem `cfharness guard integrity status` rồi chạy
  `cfharness guard integrity accept`.

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
