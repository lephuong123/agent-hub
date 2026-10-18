# Luồng TG — bot Telegram (thông báo, duyệt bằng nút, lệnh chat)

Nguồn: `docs/plan/web-telegram.md` (luồng TG), BRD FR-SG-04/09/11, FR-LN-13; `docs/dev-notes/M1-B.md` (Slack), `hardening-SEC.md`.

## Kiến trúc
| File | Vai trò |
|---|---|
| `notify/base.py` | `QueuedNotifier`: khung chung listener bus → lọc `notify.<kênh>.events` → hàng đợi → gửi có backoff → `notify.sent`/`notify.failed{channel}`. Slack refactor nhẹ thành lớp con (API cũ giữ nguyên, test Slack xanh) |
| `notify/telegram.py` | `TelegramApi` (Bot API qua `httpx2.AsyncClient`, gỡ token khỏi mọi thông báo lỗi), định dạng văn bản thuần (không parse_mode), `CallbackStore` (nonce + HMAC), `TelegramNotifier` |
| `chat/bot.py` | `TelegramBot` (`hub.telegram`): vòng long polling, ghép nối, lệnh chat, nút inline, theo dõi run → gửi kết quả, reply → continue |
| `chat/rpc.py` | RPC `telegram.status|setup|pending|allow|revoke|disable|test` (đăng ký SAU bước bọc audit → tự audit) |
| `chat/cli.py` | `agent-hub notify telegram setup|status|test|disable|allow|revoke` (gắn vào parser `notify` của `guard/cli.py`) |

File dùng chung đã chạm (chỉ thêm): `hub.py` (attach), `daemon.py` (`call_as` + tham số `as_caller` của `_dispatch`, đăng ký RPC),
`auth.py` (`telegram_caller`, `telegram.*` ∈ HUMAN_ONLY, `MUTATING_TELEGRAM` trong `all_mutating`), `config.py` (`notify.telegram`).
Ngoài danh sách dùng chung: `guard/cli.py` (gắn subparser), `guard/classifier.py` (`HUB_HUMAN_ONLY_CLI["notify"] = {"telegram"}`),
`learning_ops/digest.py` (`on_notify` chỉ tính kênh Slack khi cập nhật `slack_status`). Không có migration (không dùng v15):
trạng thái nhỏ nằm ở `hub_state` (`telegram.offset|callbacks|approval_msgs|watch|msgmap`).

## Thư viện HTTP
`httpx2` (fork httpx của pydantic) — đã có trong môi trường vì `mcp` 2.x phụ thuộc nó, nên KHÔNG thêm phụ thuộc/không đổi `uv.lock`.
Chọn async client thay vì `urllib` trong thread (như Slack) vì long poll 25 s phải **huỷ được** khi daemon dừng: một luồng
`to_thread` treo sẽ chặn `asyncio.run` đóng executor tới 25 s. Fallback `import httpx` nếu sau này `mcp` bỏ `httpx2`.
Logger `httpx*`/`httpcore*` hạ xuống WARNING (log INFO của thư viện có URL chứa token); cộng thêm che theo giá trị của SEC.

## Luồng
- **Tắt êm**: chưa có secret `TELEGRAM_BOT_TOKEN` (CHỈ đọc secret store, không lấy biến môi trường) hoặc `enabled: false` → vòng
  ngủ `idle_check_s`, không gọi mạng, không event. `setup`/`disable` gọi `kick()` (huỷ long poll đang treo, chạy lại ngay).
- **setup**: CLI hỏi token (getpass / `--token-stdin`) → RPC `telegram.setup` (HUMAN_ONLY) kiểm `getMe`, lưu secret (0600, đăng ký che),
  sinh `TELEGRAM_CALLBACK_KEY`, sinh mã ghép nối 8 hex (TTL 10 phút). Người gửi `/start <mã>` (deep link `t.me/<bot>?start=<mã>`)
  → bot chỉ ghi nhận "chờ xác nhận" (chat riêng; nhóm cần `allow_groups`) → CLI poll `telegram.pending`, hiện tên/username/chat_id, hỏi
  y/N → `telegram.allow`. Mã dùng một lần. `--chat-id` thêm thẳng, `--keep-token` ghép thêm chat.
- **Allowlist** `notify.telegram.allowed_chat_ids` nằm trong nhánh `notify` của config.yaml → được `guard/integrity` kiểm (ghi ngoài RPC bị khôi
  phục); sửa chỉ qua `telegram.allow|revoke` (HUMAN_ONLY). Classifier: `agent-hub notify telegram …` từ agent → forbidden.
- **Cổng người**: mọi lệnh chat → `Daemon.call_as(auth.telegram_caller(hub, chat_id), method, params)` → `_dispatch` như socket: HUMAN_ONLY theo
  `caller.human` (= bot bật + chat trong allowlist), `CURRENT_CALLER` (cổng tầng service), bọc audit với `_audit={channel:"telegram:<id>",
  actor:"user"}`; approve/deny truyền `channel=telegram:<id>`. Chat ngoài allowlist không bao giờ tới dispatch; nếu tới, HUMAN_ONLY từ chối.
- **Chat lạ**: không trả lời, audit `telegram.ignored` (1 dòng/phút/chat), kể cả nút bấm.
- **Nút inline**: `callback_data = v1.<nonce 96 bit>.<HMAC-SHA256[:16]>` (≤ 64 byte). Bản ghi phía hub: hành động, approval, chat_id, nhóm,
  hạn (`callback_ttl_s` 7 ngày). Kiểm: tồn tại → đúng chat → đúng chữ ký → còn hạn; hợp lệ thì TIÊU cả nhóm (Cho phép/Từ chối cùng tin) trước khi
  gọi RPC. Sai chat/chữ ký không tiêu (kẻ lạ không làm hỏng được nút thật). Mọi lần bị từ chối → audit `telegram.callback_rejected{reason}`.
  Lớp dưới vẫn còn: `approvals.resolve` báo `Conflict` nếu đã quyết; vé một lần theo `action_fingerprint`.
- **Chống chạy lại**: offset `getUpdates` lưu `hub_state` TRƯỚC khi xử lý → update không xử lý hai lần (kể cả restart giữa chừng).
- **Duyệt 2 bước**: `is_heavy` = có `protected_resource_id` hoặc `destructive_class ∈ notify.telegram.confirm_classes`
  (mặc định db, git, system, hub, policy, mcp). "Cho phép" → tin xác nhận (nút mới, TTL ≤ 1 h) → "Xác nhận CHO PHÉP" mới gọi approve;
  "Huỷ" → gửi lại tin duyệt với nút mới. `/estop off` cũng cần xác nhận.
- Approval quyết ở kênh khác (CLI/TUI/web) → `approval.resolved` → vô hiệu nonce + gỡ nút trên các tin đã gửi.
- **/run**: `run.start` → ghi `telegram.watch[run]=chat`; run con (`run.queued.parent_run_id`, resume sau duyệt/gián đoạn) kế thừa. Event kết thúc
  `source=hub` → gửi kết quả (outcome, chi phí, lượt, thời gian, `summary`/`error` cắt `result_max_chars` + "xem đầy đủ: agent-hub runs show …,
  TUI hoặc web"); `waiting_human` chỉ báo ngắn, chờ run con. Tin kết quả → `telegram.msgmap`; reply (không phải lệnh) → `run.continue`.
- **/digest**: dựng tại chỗ 24 h qua bằng `DigestService.collect/render` (CHỈ đọc — không dời mốc digest 08:00). Digest 08:00 đi qua
  thông báo (`learning.digest`), như Slack.
- **Nội dung**: văn bản thuần; mọi chuỗi qua `redact_str` (mẫu + giá trị secret); approval chỉ có preview đã che; tool MCP chỉ tên; tin ≤ 3900 ký tự.

## Test (`tests/test_telegram.py`, `tests/test_telegram_e2e.py`, server giả `tests/telegram_fake.py`)
- Tắt êm không token; setup/ghép nối/allowlist; mã sai + chat lạ bị bỏ qua + audit; token không có trong kết quả/audit.
- Cổng HUMAN_ONLY: `telegram_caller` chat lạ → BLOCKED cho `estop.set`; CLI telegram từ agent → forbidden.
- Lệnh `/help /agents /runs /approvals /estop on|off(2 bước) /digest`, audit `estop.set` kênh `telegram:111`.
- `/run` → kết quả + chi phí; reply → `run.continue` (run con `parent_run_id`) → kết quả lần 2.
- Nút: dữ liệu bịa / chữ ký sai / sai chat / định dạng cũ / chat lạ → từ chối; bấm thật → allowed kênh telegram; bấm lại + nút cùng nhóm → từ chối;
  đúng 1 `approval.resolved`; nút trên chat khác bị gỡ. Duyệt nặng 2 bước + Huỷ. `/approvals` + Từ chối. Gửi lỗi → `notify.failed` → thử lại.
- Secret: secret trong preview bị che; token không có trong tin gửi, `telegram.status`, mọi file HUB_HOME ngoài `secrets/` (gồm hub.db/WAL/log);
  lỗi mạng không lộ token. Agent: token không trong env/spec của run, sandbox denyRead `secrets/`, `Read(/…/secrets/**)` deny, `cat` file token → forbidden.
- e2e (daemon thật + hook guard thật): `rm -rf dummy` bị giữ → tin có nút → bấm → lệnh chạy ĐÚNG 1 lần; bấm lại không chạy nữa; audit kênh
  telegram. `/run` từ chat lạ không tạo run; log daemon không chứa token.

## Lệch / việc dở
- Chỉ văn bản thuần (không Markdown/HTML) — đơn giản, không lỗi escape.
- Bot nhận lệnh từ MỌI người trong chat nhóm được allowlist → mặc định chỉ chat riêng (`allow_groups: false`).
- `/stop` không xác nhận (dừng run không phá dữ liệu). `/run` không có cờ cwd/budget (dùng mặc định agent).
- Chưa kiểm với Telegram thật (chỉ server giả). Nên thử tay: setup → /start → duyệt một `rm -rf` thật trên repo tạm.
- Web (luồng WEB) nên hiển thị `telegram.status` ở trang Cài đặt; RPC sẵn sàng.
