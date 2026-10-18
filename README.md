# agent-hub

Hub điều phối đội agent AI chạy trên `claude` CLI (Claude Code) đã đăng nhập trên máy.
Bản hiện tại: **mốc M0 — lõi chạy 1 agent** (daemon + SQLite + event bus, CRUD agent, run ad-hoc,
giới hạn đồng thời toàn cục, ESTOP, `ClaudeCliRuntime` + `FakeRuntime`).

Tài liệu nghiệp vụ: `docs/BRD-chi-tiet.md`. Quyết định đã chốt: `research/BRIEF.md`.
Kết quả spike cô lập/phê duyệt/steer/ngắt: `docs/spikes/M0-spike-report.md`.

## Cài đặt

```bash
uv sync                 # Python ≥ 3.12 (ghim 3.13 trong .python-version)
uv run agent-hub --help
```

Yêu cầu: `claude` trong PATH và đã `claude auth login` (đã kiểm với 2.1.302). `agent-hub doctor` kiểm các điều này mà không tốn tiền.

## Dùng nhanh

```bash
export AGENT_HUB_HOME=~/.agent-hub          # mặc định; đổi sang thư mục khác để thử nghiệm
uv run agent-hub daemon start               # CLI cũng tự khởi động daemon khi cần
uv run agent-hub agent create researcher --role "Nghiên cứu" --model haiku --budget-per-run 0.2
uv run agent-hub run researcher "Tóm tắt README trong thư mục này" --cwd . --follow
uv run agent-hub runs list
uv run agent-hub runs show <run_id> --events
uv run agent-hub agent edit researcher --model sonnet --set limits.budget_usd_per_day=2
uv run agent-hub agent rm researcher && uv run agent-hub agent restore researcher   # xoá mềm / khôi phục
uv run agent-hub config set hub.max_concurrent_runs 2
uv run agent-hub estop on --reason "bảo trì" --stop-running ; uv run agent-hub estop off
uv run agent-hub daemon stop
```

Mọi lệnh có `--json`. Mã thoát: 0 ok · 1 lỗi người dùng · 2 lỗi hệ thống · 3 bị chặn (ESTOP/ngân sách/chưa đăng nhập).

Chạy thử không tốn tiền: `agent-hub config set runtime.kind fake` (phát lại stdout `claude` đã ghi).

## Dùng qua web

Không quen terminal? Mở giao diện web trên chính máy này:

```bash
uv run agent-hub web --open        # khởi động web (127.0.0.1:8765), mở trình duyệt và tự đăng nhập
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

Đăng nhập & an toàn: link in ra/mở bởi lệnh trên chỉ dùng **một lần** (hết hạn sau 5 phút) rồi đổi thành cookie phiên. Mất phiên
hoặc mở trình duyệt khác → chạy lại `agent-hub web --open` (nếu web đang chạy, lệnh chỉ cấp link mới); `agent-hub web --url`
chỉ in link. Web **chỉ** nghe trên loopback (`--host` khác 127.0.0.1/::1/localhost bị từ chối) — xem từ máy khác thì dùng
`ssh -L 8765:127.0.0.1:8765 <máy>`. Mọi thao tác ghi có chống CSRF, ghi nhật ký kênh `web`; agent trong run không truy cập được web.
Đổi cổng: `agent-hub web --port 8800`. Dừng: Ctrl+C ở terminal chạy web.

## Dùng qua Telegram

Nhận thông báo (yêu cầu duyệt, lỗi lặp, guard chặn, rollback, digest 08:00), **duyệt bằng nút** và giao việc ngay trên điện thoại.
Bot chạy trong daemon bằng long polling — không cần mở cổng hay endpoint công khai.

1. **Tạo bot**: trên Telegram nhắn [@BotFather](https://t.me/BotFather) → `/newbot` → đặt tên → chép token (dạng `123456789:AA…`).
2. **Kết nối**:
   ```bash
   uv run agent-hub notify telegram setup      # nhập token (ẩn khi gõ) → lưu secret store, không in ra
   ```
   Lệnh in một **mã ghép nối** dùng một lần: mở chat với bot và gửi `/start <mã>` (hoặc mở link lệnh in ra rồi bấm Start).
   Terminal hiện chat vừa nhắn và hỏi xác nhận — chỉ chat bạn xác nhận mới vào allowlist.
3. **Kiểm tra**: `agent-hub notify telegram test` · `agent-hub notify telegram status` · tắt: `agent-hub notify telegram disable [--forget-token]`.

Lệnh trong chat: `/status`, `/agents`, `/runs [agent]`, `/approvals`, `/run <agent> <yêu cầu>` (bot gửi kết quả tóm tắt + chi phí
khi xong; **trả lời (reply)** vào tin kết quả để tiếp tục hội thoại), `/stop <run>`, `/estop on|off`, `/digest`, `/help`.

An toàn: chỉ chat trong allowlist được nhận tin và điều khiển (tin từ chat khác bị bỏ qua và ghi audit); nút duyệt dùng một lần,
có chữ ký, chống bấm lại; thao tác phá huỷ nặng (git/db/hệ thống/tài nguyên bảo vệ…) cần xác nhận 2 bước; mọi thao tác ghi audit
kênh `telegram:<chat_id>`. Bot không gửi secret và cắt dữ liệu dài (xem đầy đủ trên TUI/web). Agent trong run không đọc được token bot.

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
