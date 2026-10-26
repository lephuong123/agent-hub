# Slack 2 chiều — bot Socket Mode (thông báo, duyệt bằng nút, lệnh `/cfh`, hội thoại)

Nguồn: `docs/plan/slack-2way.md` (P0 + P1 + P2), luồng TG (`docs/dev-notes/telegram.md`), `hardening-SEC.md`.

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

## P2 — modal thay `force_reply` (giao việc, trả lời câu hỏi bằng chữ)
| File | Vai trò |
|---|---|
| `chat/base.py` | Hook `ChatBot.ask_input(api, cid, kind, ref, text, placeholder, reply_to=)` — mặc định = tin `force_reply` như cũ (Telegram không đổi một byte), trả `{"message_id"}` để người gọi map; `input_where()` cho chữ "nhập … ở tin bên dưới" |
| `chat/flows.py`, `questions_tg.py` | `ask_prompt` (nút `fg`, `/giaoviec <agent>`) và nút `qf` gọi `bot.ask_input`; kết quả `{"modal": True}` → không map tin, toast "hộp vừa mở" |
| `chat/slack/modals.py` | `SlackModals`: dựng view (tiêu đề/nhãn cố định của hub, mô tả chỉ id agent/câu hỏi đã escape), `views.open`, nhận `view_submission` |
| `chat/slack/bot.py` | contextvar `_TRIGGER` = `trigger_id` của nút/slash đang xử lý (đặt lại `None` đầu mỗi envelope, dùng một lần); `ask_input` override: có trigger → modal, không/`views.open` lỗi → tin trả lời trong thread (P1); route `interactive` `type=view_submission` |

- **Ràng buộc**: `private_metadata` = nonce ngẫu nhiên; bản ghi `{kind, ref, chat, user, exp}` ở `hub_state slack.modals` (TTL `notify.slack.modal_ttl_s`,
  mặc định 3600 s; tối đa 500). Ghi TRƯỚC `views.open`, gỡ nếu mở lỗi.
- **Submit**: `callback_id` khác `cfh_input` → bỏ. Rồi lần lượt: `team_ok` → nonce có → chưa hết hạn → `user.id` = người đã mở → `is_allowed(chat, user)`
  (thu hồi sau khi mở vẫn chặn) → TIÊU nonce → `flows.start_run` / `questions.answer_text` (đều qua `bot.call` → cổng người, audit `slack:<chat>`).
  Sai → audit `slack.callback_rejected` với `reason = modal:<wrong_team|unknown_or_used|expired|wrong_user|not_allowed>`; người lạ / sai người
  KHÔNG tiêu nonce (không huỷ được hộp nhập của chủ). Workspace lạ thêm `slack.ignored`.
- ACK `view_submission` là ACK rỗng → Slack đóng modal; ô nhập bắt buộc (Slack chặn gửi trống), ≤ 3000 ký tự.
- Trigger sống ~3 s và hàng đợi xử lý tuần tự: envelope đến sau một việc chậm có thể làm `views.open` trả `expired_trigger_id` → lùi về thread.
- Test: `tests/test_slack_modals.py` (nút agent → modal → `run.start` + tin kết quả; `/cfh giaoviec <agent>`; trigger hết hạn → thread;
  metadata bịa / người lạ / người khác trong allowlist / workspace khác / modal lạ / dùng lại / thu hồi allowlist; câu hỏi bằng chữ qua modal,
  chữ agent không vào modal). Slack giả: `views.open` (+ `fail_views`), `views()`, `submit_view()`.

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

## Chưa làm (theo plan)
P3 câu hỏi bằng checkbox + `schedules` kênh `slack:<chat>` + ghi chú tệp trong prompt điều phối viên, P4 ảnh/tệp vào,
P5 wizard web 2 chiều (manifest Socket Mode). Nghiệm thu trên workspace Slack thật: chưa chạy.
