# Slack 2 chiều — bot Socket Mode (thông báo, duyệt bằng nút, lệnh `/cfh`, hội thoại)

Nguồn: `docs/plan/slack-2way.md` (P0 + P1 + P2), luồng TG (`docs/dev-notes/telegram.md`), `hardening-SEC.md`.

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

## Chưa làm
- Reaction ⏳ thay "đang gõ…" khi chờ điều phối viên (cần scope `reactions:write`); bot Slack hiện bỏ qua "đang gõ".
- Nghiệm thu trên workspace Slack thật: chưa chạy (modal `views.open`, tải `url_private` với `files:read`, tệp từ máy tính + điện thoại).
