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

## P3 — câu hỏi của agent, điều phối viên, lịch
- **Câu hỏi** (`questions_tg.py`, không đổi luồng): `question.open{routed_to=human}` → mỗi DM trong `notify_chats()` (+ chat đang theo dõi
  run, chat của hội thoại điều phối viên) nhận tin NỘI DUNG (chữ agent trong khối mã) + tin HỆ THỐNG trong thread của nó với nút số;
  chọn nhiều = nút bật/tắt `☐/☑ n` + `[Gửi lựa chọn]` (mỗi lần bấm: tiêu cả nhóm, `chat.update` cấp nút mới — giữ mô hình Telegram,
  KHÔNG dùng checkbox Block Kit: checkbox giữ trạng thái phía client, không ký từng lựa chọn được). "Trả lời bằng chữ" → tin trong thread
  (không có `force_reply` trên Slack — render thêm dòng "↩️ Trả lời trong thread"); trả lời trong thread của tin nội dung/hệ thống/ô nhập
  → `question.answer{text}`. `question.answered|cancelled` từ BẤT KỲ kênh → `chat.update` tin hệ thống thành dòng kết quả, gỡ nút.
  Đã chạy được nguyên trạng sau P0/P1 — P3 chỉ thêm test end-to-end với Slack giả (`tests/test_slack_questions.py`) + sửa lời dẫn.
- **Lời dẫn theo nền tảng**: lõi không còn viết cứng "trả lời (reply) tin này", `/approvals`, `/moi`, `/run …`, `/help`. `ChatBot` có
  `cmd(name)`, `reply_hint(what)`, `reply_here()`, `reply_into(target)`, `plain_hint()`, `answer_box_hint()` — mặc định = chữ Telegram
  (từng byte như cũ), `SlackBot` ghi đè ("trả lời trong thread của …", `/cfh <lệnh>`). `manager_chat.GUIDE`/`UNAVAILABLE` giữ nguyên là chữ
  Telegram (test import); bot gọi `guide_text(bot)` / `unavailable_text(bot)`.
- **Lịch**: `deliver_to.channel` nhận `slack:<chat_id>` (`[CDG][A-Z0-9]{2,20}` — DM `D…`, kênh `C…`/`G…`). Giao kết quả giống `telegram:`:
  `manager.reply{channel: "slack:<chat>", kind: notify}` → `SlackBot.manager` (chỉ gửi nếu chat ∈ `notify_chats()`). Mô tả công cụ
  `schedule_create` (hub_self) và `cfharness schedule add --to` nhắc `slack:<chat_id>`.

## Chưa làm (theo plan)
P2 modal giao việc, P4 ảnh/tệp vào (+ ghi chú tệp trong prompt điều phối viên cho kênh `slack:`), P5 wizard web 2 chiều (manifest
Socket Mode). P3 còn: reaction ⏳ thay "đang gõ…" khi chờ điều phối viên (cần scope `reactions:write` trong manifest — làm cùng P5); bot
Slack hiện bỏ qua "đang gõ" (`typing` ném lỗi → vòng nhắc dừng êm). Nghiệm thu trên workspace Slack thật: chưa chạy.
