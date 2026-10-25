# cfharness

Hub điều phối đội agent AI chạy trên `claude` CLI (Claude Code) đã đăng nhập trên máy.
Bản hiện tại: **mốc M0 — lõi chạy 1 agent** (daemon + SQLite + event bus, CRUD agent, run ad-hoc,
giới hạn đồng thời toàn cục, ESTOP, `ClaudeCliRuntime` + `FakeRuntime`).

Tài liệu nghiệp vụ: `docs/BRD-chi-tiet.md`. Quyết định đã chốt: `research/BRIEF.md`.
Kết quả spike cô lập/phê duyệt/steer/ngắt: `docs/spikes/M0-spike-report.md`.

## Cài đặt

```bash
uv sync                 # Python ≥ 3.12 (ghim 3.13 trong .python-version)
uv run cfharness --help
```

Yêu cầu: `claude` trong PATH và đã `claude auth login` (đã kiểm với 2.1.302). `cfharness doctor` kiểm các điều này mà không tốn tiền.

**Windows** (native, không cần WSL): cần Git for Windows (Claude Code chạy Bash/hook qua Git Bash). Daemon nghe TCP
`127.0.0.1` (file endpoint `hub.sock`), run chạy trong Job Object, hệ thống ngoài hub dùng Task Scheduler thay launchd.
Gốc workspace mặc định là `~/Workspace` — thư mục khác thì thêm: `cfharness config set workspace.allowed_roots '["C:/WorkSpace"]'`.
Claude Code chưa có sandbox OS trên Windows (`doctor` cảnh báo) — khác biệt bảo mật và lỗ còn hở: `docs/dev-notes/windows.md`.

## Dùng nhanh

```bash
export CFHARNESS_HOME=~/.cfharness          # mặc định; đổi sang thư mục khác để thử nghiệm
uv run cfharness daemon start               # CLI cũng tự khởi động daemon khi cần
uv run cfharness agent create researcher --role "Nghiên cứu" --model haiku --budget-per-run 0.2
uv run cfharness run researcher "Tóm tắt README trong thư mục này" --cwd . --follow
uv run cfharness runs list
uv run cfharness runs show <run_id> --events
uv run cfharness agent edit researcher --model sonnet --set limits.budget_usd_per_day=2
uv run cfharness agent rm researcher && uv run cfharness agent restore researcher   # xoá mềm / khôi phục
uv run cfharness config set hub.max_concurrent_runs 2
uv run cfharness estop on --reason "bảo trì" --stop-running ; uv run cfharness estop off
uv run cfharness daemon stop
```

Mọi lệnh có `--json`. Mã thoát: 0 ok · 1 lỗi người dùng · 2 lỗi hệ thống · 3 bị chặn (ESTOP/ngân sách/chưa đăng nhập).

Chạy thử không tốn tiền: `cfharness config set runtime.kind fake` (phát lại stdout `claude` đã ghi).

## Dùng qua web

Không quen terminal? Mở giao diện web trên chính máy này:

```bash
uv run cfharness web --open        # khởi động web (http://cfharness.localhost:8765), mở trình duyệt và tự đăng nhập
```

- **Tổng quan**: agent nào đang làm gì (cập nhật mỗi 2 giây), chi phí hôm nay, khung đỏ **Cần bạn duyệt** khi agent định làm
  thao tác phá huỷ — bấm *Cho phép một lần* hoặc *Từ chối* (có hộp xác nhận), và ô **Giao việc nhanh**.
- **Xem live**: bấm *Xem live* ở một agent → diễn biến chạy trực tiếp (lời agent, tool đang chạy, diff), ô **Chỉ đạo** để nói thêm
  với agent đang chạy, nút **Dừng run**. Khi xong: kết quả, chi phí, nút 👍/👎 để agent tự học, ô **Nói tiếp** để tiếp hội thoại.
- **Agent → + Tạo agent**: chọn mẫu (researcher/coder/reviewer/ops), tích các **MCP** được dùng, đặt ngân sách → *Tạo agent*.
  Trang agent có ô **Giao việc**, lịch sử cấu hình (quay về bản cũ), tạm dừng, xoá mềm (khôi phục ở *Thùng rác*).
- Các trang khác: *Công việc* (kanban + phụ thuộc), *Team*, *Tự học* (thay đổi đã áp dụng, diff, rollback, golden task),
  *MCP* (thêm/kiểm tra/nhập từ Claude Code), *Chi phí*, *Nhật ký* (audit), *Cài đặt* (ESTOP, cấu hình thường dùng, secret).

Dùng được trên điện thoại/màn nhỏ; sáng/tối theo hệ thống. Ảnh màn: `docs/screenshots/web/`.

Đăng nhập & an toàn: web dùng tên riêng `cfharness.localhost` (trình duyệt tự trỏ về máy này) để cookie không lọt sang app khác
bạn chạy ở `127.0.0.1`/`localhost`; cần bật JavaScript cho thao tác ghi. Link in ra/mở bởi lệnh trên chỉ dùng **một lần** (hết hạn sau
5 phút) rồi đổi thành phiên (tự hết sau 1 giờ không thao tác). Mất phiên
hoặc mở trình duyệt khác → chạy lại `cfharness web --open` (nếu web đang chạy, lệnh chỉ cấp link mới); `cfharness web --url`
chỉ in link. Web **chỉ** nghe trên loopback (`--host` khác 127.0.0.1/::1/localhost bị từ chối) — xem từ máy khác thì dùng
`ssh -L 8765:127.0.0.1:8765 <máy>`. Mọi thao tác ghi có chống CSRF, ghi nhật ký kênh `web`; agent trong run không truy cập được web.
Đổi cổng: `cfharness web --port 8800`. Dừng: Ctrl+C ở terminal chạy web.

## Dùng qua Telegram

Nhận thông báo (yêu cầu duyệt, lỗi lặp, guard chặn, rollback, digest 08:00), **duyệt bằng nút** và giao việc ngay trên điện thoại.
Bot chạy trong daemon bằng long polling — không cần mở cổng hay endpoint công khai.

1. **Tạo bot**: trên Telegram nhắn [@BotFather](https://t.me/BotFather) → `/newbot` → đặt tên → chép token (dạng `123456789:AA…`).
2. **Kết nối**:
   ```bash
   uv run cfharness notify telegram setup      # nhập token (ẩn khi gõ) → lưu secret store, không in ra
   ```
   Lệnh in một **mã ghép nối** dùng một lần: mở chat với bot và gửi `/start <mã>` (hoặc mở link lệnh in ra rồi bấm Start).
   Terminal hiện chat vừa nhắn và hỏi xác nhận — chỉ chat bạn xác nhận mới vào allowlist.
3. **Kiểm tra**: `cfharness notify telegram test` · `cfharness notify telegram status` · tắt: `cfharness notify telegram disable [--forget-token]`.

Lệnh trong chat: `/status`, `/agents`, `/runs [agent]`, `/approvals`, `/run <agent> <yêu cầu>` (bot gửi kết quả tóm tắt + chi phí
khi xong; **trả lời (reply)** vào tin kết quả để tiếp tục hội thoại), `/stop <run>`, `/estop on|off`, `/digest`, `/help`.

An toàn: chỉ chat trong allowlist được nhận tin và điều khiển (tin từ chat khác bị bỏ qua và ghi audit); nút duyệt dùng một lần,
có chữ ký, chống bấm lại, chỉ người đã ghép nối bấm được; mọi "Cho phép" cần xác nhận 2 bước; tin duyệt hiện lệnh ĐẦY ĐỦ; mọi thao tác ghi audit
kênh `telegram:<chat_id>`. Bot không gửi secret và cắt dữ liệu dài (xem đầy đủ trên TUI/web). Agent trong run không đọc được token bot.

## Điều phối viên

Điều phối viên là **một agent bạn chỉ định** để quản lý cả đội: bạn nói mục tiêu bằng lời ("tạo agent nghiên cứu tóm tắt README
của repo X rồi giao việc đó"), nó tự tạo/sửa agent và team, giao task, theo dõi tới khi xong và báo lại. Nó KHÔNG được duyệt thao tác
phá huỷ, sửa chính sách/guard/golden task, mở ngoại lệ sandbox, xoá vĩnh viễn, tắt ESTOP hay sửa cấu hình của chính nó; mọi thao tác
ghi nhật ký `manager:<agent>` và rollback được.

- **Chỉ định** (chỉ người): `cfharness manager set <agent>` (huỷ: `--none`), hoặc web → trang **Điều phối** → *Chỉ định…* (có hộp xác nhận).
- **Web — trang Điều phối** (đầu menu): khung chat theo cuộc trò chuyện (*+ Cuộc trò chuyện mới* để mở ngữ cảnh mới), trả lời chạy
  chữ trực tiếp, **thẻ hành động** của điều phối viên ("Đã tạo agent X", "Đã giao task Y cho team Z") có link tới đối tượng, cột bên:
  trạng thái + nhịp (lần gần nhất / kế tiếp), hành động gần đây, danh sách cuộc trò chuyện. Trang **Tổng quan** có khung nhỏ
  *Điều phối viên* với ô gửi nhanh. Chữ của điều phối viên luôn hiện như văn bản thuần (không HTML, không link do agent đặt).
- **Telegram**: mọi tin nhắn **không** bắt đầu bằng `/` được gửi cho điều phối viên; trả lời về trong khung "nội dung do AGENT viết"
  (không có nút). Nhắn tiếp là cùng cuộc trò chuyện; **reply** vào một tin cũ của điều phối viên để quay lại đúng cuộc đó; `/moi` (hoặc
  `/moi <nội dung>`) mở cuộc trò chuyện mới. Tin điều phối viên chủ động báo (`notify_owner`) cũng đến đây. Chưa có điều phối viên → bot
  trả hướng dẫn. Các lệnh cũ (`/run`, reply vào tin kết quả run…) giữ nguyên.
- **Terminal**: `cfharness manager chat "<nội dung>"`, `manager get|conversations|history|tick`.
- **Terminal — `cfharness chat`** (chat qua lại, giống Claude CLI): ngoài hội thoại với điều phối viên còn thấy **cả đội**.
  - *Bảng agent* ngay trên thanh trạng thái: gọn một dòng `● 3 agent đang chạy · ⚠ 1 chờ bạn duyệt  (↓ xem)` (ẩn khi không có gì
    chạy/chờ); **↓** khi dòng nhập trống mở bảng — mỗi agent một dòng: trạng thái (● chạy, ◌ chờ slot, ◐ chờ duyệt, ⏸ chờ bạn, ✖ lỗi),
    tên, task, thời gian, tool cuối, token, $; **↓**/**Esc** đóng (đang gõ thì ↑↓ vẫn là lịch sử). Terminal thấp → `+k nữa`.
  - *Thẻ duyệt / câu hỏi* của **mọi** agent hiện ngay trong chat kèm chuông: agent, tool, lệnh **đầy đủ**, lý do, rủi ro, id, cách xử
    lý. Yêu cầu đang do điều phối viên xử lý chỉ hiện một dòng "điều phối viên đang xử lý" (không chuông); chuyển lên bạn → thẻ đầy đủ.
  - Lệnh: `/agents` (`/ps`) bảng mọi agent · `/stop <agent|run>` (hỏi y/N) · `/attach <agent>` mở màn attach của `top`, thoát là quay
    lại chat · `/approvals` · `/approve <id> [ghi chú]` (in lại lệnh đầy đủ rồi hỏi y/N) · `/deny <id> [lý do]` · `/always <id>`
    (chỉ khi hub cho phép, xác nhận 2 bước) · `/answer <id> 1,2 [chữ]`. Mọi quyết định ghi audit kênh `cli`, cùng cổng chỉ-người
    như TUI/web/Telegram. Không phải TTY (script): thẻ in chữ thường, không bảng, y/N đọc từ dòng stdin kế tiếp.
  - Ảnh: `docs/screenshots/m2/15-chat-bang-agent.svg`, `16-chat-the-duyet.svg`.

Ảnh: `docs/screenshots/web/16-dieu-phoi*.png`, `17-…`, `18-…`.

## Bố cục `HUB_HOME`

```
hub.db (SQLite WAL)  hub.sock  hub.pid  config.yaml
agents/<id>/{config.yaml, SOUL.md, MEMORY.md, USER.md, skills/, .archive/, runtime/, workspaces/}
runs/<run_id>.jsonl.gz (stdout thô)  runs/<run_id>.stderr.log
trash/agents/<id>__<ts>/   (xoá mềm, giữ 30 ngày)
logs/hub.log (JSON lines, xoay vòng)  mcp/registry.yaml
```

## Kiểm thử

```bash
uv run pytest           # chỉ dùng FakeRuntime / `claude` giả — không gọi model thật
```
