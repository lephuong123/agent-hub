# Slack 2 chiều — bot Socket Mode (thông báo, duyệt bằng nút, lệnh `/cfh`, hội thoại)

Nguồn: `docs/plan/slack-2way.md` (P0 + P1), luồng TG (`docs/dev-notes/telegram.md`), `hardening-SEC.md`.

## P0 — tách lõi chat khỏi Telegram
| File | Vai trò |
|---|---|
| `chat/base.py` | `ChatBot`: logic KHÔNG phụ thuộc nền tảng — `call` (→ `Daemon.call_as`, audit `<kênh>:<chat>`), lệnh (`command_handlers`, `run_command`), tin duyệt + quyết 2 bước (`send_approval`, `handle_action`, `_decide`), theo dõi run → kết quả, `run.continue`, trạng thái `hub_state` khoá `<kênh>.*`, ghép nối. Hằng số dùng chung (`AGENT_FRAME`, `OUTCOME_VI`…) |
| `chat/bot.py` | `TelegramBot(ChatBot)`: chỉ còn vận chuyển — long polling, parse update, ghép nối `/start`, `_send/_edit` (HTML + inline_keyboard), `send_document`, `typing`. Re-export hằng số cũ |
| `chat/flows.py`, `questions_tg.py`, `manager_chat.py`, `media.py` | nhận `ChatBot` (không còn `TelegramApi`); khoá trạng thái `f"{bot.NAME}.…"`; gửi tệp/"đang gõ"/sửa tin qua phương thức của bot. `media.py` phần NHẬN ảnh/tệp vẫn là của Telegram (Slack: P4) |
| `notify/telegram.py` | `CallbackStore(prefix=…)`: kho nút + khoá ký riêng mỗi kênh |

"Ngôn ngữ chung" giữa lõi và lớp vận chuyển: chữ thuần hoặc HTML tối giản (`<b> <i> <pre> <code>`, giá trị động đã `esc`), nút =
`{"inline_keyboard": …}` do `CallbackStore` phát, `{"force_reply": …}` = mời trả lời. Lớp con tự dịch. Không đổi khoá `hub_state`,
chuỗi hiển thị hay lời gọi Bot API của Telegram — 5 file `tests/test_telegram*.py` xanh không sửa.

## P1 — Slack
| File | Vai trò |
|---|---|
| `chat/slack/api.py` | `SlackApi` (httpx2, thân form, gỡ token khỏi lỗi, 429 → `retry_after`), `upload` (files v2) |
| `chat/slack/render.py` | HTML hub → mrkdwn; escape `& < >`; khối chữ `verbatim`; ` trong khối mã → ˋ; chia section ≤ 2900 (đóng/mở lại ```); nút → `actions` |
| `chat/slack/bot.py` | `SlackBot(ChatBot)` + `SlackChatNotifier`: Socket Mode (`websockets`), ACK trước xử lý, hàng đợi tuần tự, chống trùng `slack.seen`, thread ≈ reply, `/cfh`, nút, ghép nối `ghepnoi <mã>` |
| `chat/slack/rpc.py`, `cli.py` | `slack.status|setup|pending|allow|revoke|disable|test`, `cfharness notify slack …` |

File dùng chung (chỉ thêm): `auth.py` (`slack_caller`, `slack.*` ∈ HUMAN_ONLY, `MUTATING_SLACK`), `config.py` (`notify.slack.interactive`
và các khoá mới), `chat/__init__.py` (`hub.slack`), `daemon.py` (đăng ký RPC), `guard/classifier.py` (`notify slack` từ agent → forbidden),
`guard/cli.py` (subparser), `notify/slack.py` (nhường bot khi 2 chiều hoạt động + escape mrkdwn ở kênh webhook). Phụ thuộc mới: `websockets`.

## Luồng
- **Tắt êm**: `interactive: false`, hoặc thiếu `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` trong secret store → vòng ngủ `idle_check_s`, không gọi mạng.
- **Socket Mode**: `auth.test` (một lần mỗi cặp token) → `apps.connections.open` (app token) → WebSocket. Mỗi envelope: ACK ngay →
  kiểm/ghi khoá chống trùng (`event_id` | `trigger_id` | `envelope_id`) → hàng đợi → worker tuần tự. `disconnect` → mở kết nối mới;
  lỗi → backoff (tôn trọng `Retry-After`). Đóng kết nối: xử lý nốt envelope đã ACK (≤ 10 s).
- **Cổng người**: `auth.slack_caller` — bật + `interactive` + chat ∈ `allowed_chat_ids` + người ∈ `allowed_user_ids`; kênh không phải DM chỉ khi
  `allow_channels`. Workspace: bot bỏ envelope có `team_id` khác `notify.slack.team_id` (ghim lúc setup). Người lạ: không trả lời, audit
  `slack.ignored`; nút của người lạ không bị tiêu.
- **Lệnh**: `/cfh <lệnh> …` (client Slack chặn tin bắt đầu bằng `/`), kết quả vào DM người gọi (`conversations.open`, nhớ ở `slack.dms`).
  Tin chữ thường trong DM → điều phối viên.
- **Thread = reply**: bot nhớ ts gốc của mọi tin nó gửi trong thread; trả lời trong thread → thử lần lượt (tin mới nhất trước) câu hỏi →
  ô giao việc → `run.continue` → hội thoại điều phối viên.
- **Nút**: `value` = `v1.<nonce>.<HMAC>` (`SLACK_CALLBACK_KEY`, kho `slack.callbacks`); toast = `chat.postEphemeral`; sửa tại chỗ = `chat.update`.
  Quyết ở kênh khác → `approval.resolved` → gỡ nút ở mọi kênh (Telegram và Slack).
- **Thông báo**: khi bot hoạt động (`SlackBot.active()`), `SlackNotifier` (webhook/kênh) nhường hết cho `SlackChatNotifier` (DM, approval có nút).

## P5 — wizard web 2 chiều (trang Cài đặt, không cần CLI)
| File | Vai trò |
|---|---|
| `web/app.py` | `SLACK_MANIFEST_2WAY` (Socket Mode, `interactivity`, `app_home.messages_tab_enabled`, bot event `message.im`, scope `chat:write im:history im:read im:write commands users:read files:read files:write`), `slack_slash()` (lệnh hợp lệ, sai → `/cfh`), `slack_2way_create_url(slash)` — lệnh lấy từ `notify.slack.slash_command` |
| `web/slack_pages.py` | ngữ cảnh khối Cài đặt (`slack.status`), partial `/partials/slack/pending`, thao tác `slack2_setup|pair|allow|revoke|test|disable` vào `ACTIONS` |
| `templates/settings.html` (`#slack2`), `_slack_pending.html` | trạng thái, DM được phép (nút Gỡ), hướng dẫn tạo app + form 2 token, mã ghép nối + người chờ xác nhận (poll 3 s) |

Luồng: "Tạo app Slack 2 chiều" (manifest điền sẵn) → Install → copy `xoxb-` (OAuth & Permissions) + tạo App-Level Token `xapp-`
(`connections:write`) → dán → `slack.setup` (daemon kiểm `auth.test` + `apps.connections.open`, lưu secret, ghim workspace, bật
`interactive`) → trang hiện `ghepnoi <mã>` → người nhắn mã trong DM (hoặc `/cfh ghepnoi <mã>`) → hiện tên + `user_id` ở danh sách chờ →
"Xác nhận" (hộp xác nhận nêu quyền sẽ cấp) = `slack.allow`. "Tạo mã ghép nối mới" / "Bật lại" = `slack.setup {}` (dùng token đã lưu).
"Tắt" = `slack.disable` (giữ token); "Tắt và xoá token" = `slack.disable {forget_token}`.

Bảo mật: mọi POST qua `post_guard` (CSRF + Origin + khoá phiên WT-1), `slack.*` HUMAN_ONLY và tự audit `channel=web`; poll
`slack.pending` gọi KHÔNG audit (đọc). Token: ô `type=password autocomplete=off` + `pattern` phía trình duyệt, kiểm lại phía server bằng
`BOT_TOKEN_RE|APP_TOKEN_RE` của `chat/slack/rpc.py` (thông điệp lỗi không lặp giá trị), không đặt `data-confirm` lên form token (hộp xác nhận
JS chép mọi input vào DOM) — dùng ô tích `confirm=yes`; trang xác nhận phía server bỏ hai trường token (`secret_fields`). Mã ghép nối giữ
trong `Session.extra` (RAM, theo phiên), không phải bí mật lâu dài (một lần, TTL `pair_ttl_s`, vẫn cần người xác nhận).
Test `tests/test_web_slack.py` (4): manifest + lệnh slash, khối hiện cạnh wizard webhook cũ, 403 khi thiếu CSRF/khoá phiên (không RPC nào
chạy), luồng đủ với Slack giả (setup → ghép nối → allow → test → mã mới → revoke → disable → xoá token), audit `web`, token không có
trong thân/header response, audit, log. Ảnh: `docs/screenshots/web/v2/23-slack-2-chieu-cai-dat.png`, `24-slack-2-chieu-ghep-noi.png`
(Chromium thật + Slack giả, đã bấm Xác nhận qua hộp xác nhận JS → allowlist cập nhật, tin chào gửi).

Giới hạn (RPC trong `chat/`, chưa sửa ở P5): `slack.status` không trả tên workspace (chỉ `team_id`) và không ghép `chat_id` ↔ `user_id`
→ trang hiện hai danh sách riêng; `slack.revoke` chỉ gỡ `chat_id` (người vẫn ở `allowed_user_ids`, vô hại vì cần cả hai); tin bot trả khi
ghép bằng slash vẫn nói "Quay lại terminal để xác nhận" (nên đổi thành "terminal hoặc trang Cài đặt").

## Chưa làm (theo plan)
P2 modal giao việc, P3 câu hỏi bằng checkbox + `schedules` kênh `slack:<chat>` + ghi chú tệp trong prompt điều phối viên, P4 ảnh/tệp vào.
Nghiệm thu trên workspace Slack thật: chưa chạy.
