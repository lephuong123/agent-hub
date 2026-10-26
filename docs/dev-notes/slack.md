# Slack 2 chiều — bot Socket Mode (thông báo, duyệt bằng nút, lệnh `/cfh`, hội thoại)

Nguồn: `docs/plan/slack-2way.md` (P0 + P1), luồng TG (`docs/dev-notes/telegram.md`), `hardening-SEC.md`.

## P0 — tách lõi chat khỏi Telegram
| File | Vai trò |
|---|---|
| `chat/base.py` | `ChatBot`: logic KHÔNG phụ thuộc nền tảng — `call` (→ `Daemon.call_as`, audit `<kênh>:<chat>`), lệnh (`command_handlers`, `run_command`), tin duyệt + quyết 2 bước (`send_approval`, `handle_action`, `_decide`), theo dõi run → kết quả, `run.continue`, trạng thái `hub_state` khoá `<kênh>.*`, ghép nối. Hằng số dùng chung (`AGENT_FRAME`, `OUTCOME_VI`…) |
| `chat/bot.py` | `TelegramBot(ChatBot)`: chỉ còn vận chuyển — long polling, parse update, ghép nối `/start`, `_send/_edit` (HTML + inline_keyboard), `send_document`, `typing`. Re-export hằng số cũ |
| `chat/flows.py`, `questions_tg.py`, `manager_chat.py`, `media.py` | nhận `ChatBot` (không còn `TelegramApi`); khoá trạng thái `f"{bot.NAME}.…"`; gửi tệp/"đang gõ"/sửa tin qua phương thức của bot. `media.py`: `MediaBase` dùng chung (từ P4), `MediaChat` = phần nhận riêng Telegram |
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

## P4 — ảnh/tệp
| File | Vai trò |
|---|---|
| `chat/media.py` | Tách `MediaBase` (mọi kênh): `classify` (ảnh/văn bản/chưa hỗ trợ), `collect` (giới hạn số lượng + cỡ khai báo TRƯỚC khi tải, tải qua `MediaItem.fetch`, đếm `downloads`, kiểm UTF-8/NUL), `submit` (báo lỗi chữ hub + `manager.handle_text`), chiều RA `deliver_files`. `MediaChat` (Telegram) chỉ còn dựng `MediaItem` từ `photo`/`document`/album — chữ hiển thị và lời gọi Bot API không đổi |
| `chat/slack/media.py` | `SlackMedia`: tin `file_share` → `MediaItem` cho mỗi `files[]`; `allowed_url` (chỉ `https://files.slack.com/…`, không userinfo/cổng lạ/`..`; test thay gốc bằng `notify.slack.files_base`); `download` (bot token ở header, stream + trần cứng, KHÔNG theo redirect, trang HTML 200 → lỗi "thiếu `files:read`?", lỗi gỡ token) |
| `chat/base.py` | `_make_media()` — Telegram mặc định, Slack ghi đè (`chat/slack/bot.py`) |
| `manager/prompts.py` | `deliverables_note(channel)`: quy ước `[[file: …]]` cho cả `telegram:` và `slack:` (`TELEGRAM_FILES_NOTE` giữ nguyên chữ) |
| `config.py` | `notify.slack.document_max_bytes` (1 MB, trần cứng 10 MB), `notify.slack.files_base` (chỉ test) |

- **Vào**: `_on_message` kiểm workspace + allowlist chat/người TRƯỚC (người lạ: không trả lời, không tải — test đếm `downloads == 0` và
  Slack giả không nhận GET nào); `SlackMedia.handle` kiểm lại `is_allowed` trước khi tải. Ảnh PNG/JPEG/GIF/WebP ≤ 5 MB, ≤ 4/tin; tệp văn bản
  ≤ `document_max_bytes`, ≤ 4/tin; PDF/Office/nhị phân, tệp đã xoá/ẩn (`mode: tombstone|hidden_by_limit`), tệp kênh ngoài
  (`file_access: check_file_info`) → từ chối kèm lý do (một tin hub, tên tệp trong khối mã, escape). Chú thích = chữ của tin.
- **Thread**: tệp gửi trong thread → dò tin của bot trong thread (`_candidates`, như `_on_thread_reply`) → đúng cuộc trò chuyện điều phối
  viên, kể cả khi đã `/cfh moi`. Ngoài thread → cuộc hiện tại.
- **Ra**: `[[file: …]]` → `MediaBase.deliver_files` → `SlackBot.send_document` → `SlackApi.upload` (files v2:
  `files.getUploadURLExternal` → POST → `files.completeUploadExternal`). Cùng quy tắc Telegram (chỉ tệp thường trong cwd hội thoại,
  không symlink, ≤ `deliver_max_bytes`, ≤ `deliver_max_files`, không chứa secret); audit `slack.file_sent`. Slack không trả ts của tin
  chứa tệp → reply vào tin tệp không map về hội thoại (trả lời trong thread của tin chữ đi trước).
- Scope: `files:read` (tải), `files:write` (gửi) — đã có trong manifest. Test: `tests/test_slack_media.py`; Slack giả phục vụ tệp ở
  `/files-pri/<id>/<tên>` (`user_files()`, `downloaded()`; thiếu token → trang HTML 200 như Slack thật).

## Chưa làm (theo plan)
P2 modal giao việc, P3 câu hỏi bằng checkbox + `schedules` kênh `slack:<chat>` (ghi chú tệp trong prompt điều phối viên: đã làm ở P4),
P5 wizard web 2 chiều (manifest Socket Mode). Nghiệm thu trên workspace Slack thật: chưa chạy (P4: kiểm tải `url_private` với app có
`files:read`, tệp từ máy tính + điện thoại, tệp > 5 MB).
