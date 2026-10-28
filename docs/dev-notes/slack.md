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

Bổ sung khi tích hợp P2–P5: `slack.status` trả thêm `team` (tên workspace); `slack.allow` nhớ DM ↔ người (`slack.dms`) và
`slack.revoke` gỡ luôn người sở hữu DM khỏi `allowed_user_ids`; tin xác nhận ghép nối nhắc cả "terminal hoặc trang Cài đặt";
`notify.slack.modal_ttl_s` có trong config mặc định.

## P6 — tag bot trong kênh (`app_mention`)
| File | Vai trò |
|---|---|
| `chat/slack/bot.py` | `strip_mention` (bỏ `<@bot>`, giữ tag người khác); `_on_mention`: `app_mention` → chuẩn hoá thành tin `message` rồi đi `_on_message` (cùng cổng workspace + allowlist kênh VÀ người); `broadcast_chats()` = chỉ DM |
| `chat/base.py` | `ChatBot.broadcast_chats()` — mặc định = `notify_chats()` (Telegram không đổi) |
| `chat/questions_tg.py`, `chat/manager_chat.py`, `SlackChatNotifier.target` | tin chủ động (approval, câu hỏi, `notify_owner` khi chưa có hội thoại) dùng `broadcast_chats()` |
| `web/app.py` | manifest 2 chiều thêm event `app_mention` + scope `app_mentions:read` |

- **Bật**: `cfharness config set notify.slack.allow_channels true` → mời bot vào kênh (`/invite @cfharness`) → tag. KHÔNG ghép nối kênh:
  khi `allow_channels` bật, `auth.slack_caller` nhận MỌI kênh (không phải DM) nếu người tag ∈ `allowed_user_ids` (workspace đã ghim);
  kênh được nhớ ở `hub_state slack.channels` lần đầu người đó tag để giao trả lời (`notify_chats`). DM vẫn phải ghép nối.
  App đã cài trước P6: dán lại manifest (hoặc thêm event + scope bằng tay) rồi **Reinstall** để Slack gửi `app_mention`.
- **Luồng**: tag ở gốc kênh → cuộc trò chuyện điều phối viên MỚI (mỗi lần tag một việc), trả lời vào thread của tin đã tag. Tag lại trong thread
  đó → tiếp tục cuộc đó (`_on_thread_reply`). Tag trong thread của người khác → `_remember(ts, thread_ts)` để trả lời vào đúng thread.
- **Không đọc cả kênh**: không xin `message.channels` / `channels:history` → tin không tag bot (kể cả trả lời trong thread) bot không thấy.
  `app_mention` trong DM bị bỏ (DM đã đến qua `message.im`).
- **Kênh chỉ nhận trả lời của việc được tag ở đó**: approval có nút, câu hỏi của agent, thông báo hub → chỉ DM. Kết quả trong kênh mọi thành viên
  đều thấy. Lịch `deliver_to: slack:C…` vẫn giao vào kênh (đi theo `notify_chats()`).
- Test: `tests/test_slack_mentions.py` (thread + cuộc mới/cũ, người lạ, `allow_channels` tắt, workspace khác, approval chỉ vào DM, manifest);
  Slack giả: `FakeSlack.mention()`.

## P7 — "đang xử lý…" + tin tiến độ + nút Dừng (khi điều phối viên làm việc)
| File | Vai trò |
|---|---|
| `chat/slack/progress.py` | `SlackProgress`: trạng thái thread (`assistant.threads.setStatus`, lỗi → reaction ⏳), MỘT tin tiến độ sửa tại chỗ theo `tool.start`/`tool.complete` của run điều phối viên, nút "Dừng" (`ms`), thu về một dòng khi xong; `tool_label` (tên tool lọc ký tự + nhãn hub theo loại) |
| `chat/slack/bot.py` | `typing(…, reply_to=)` → `progress.status`; `TYPING_EVERY_S = 90` (Slack tự xoá trạng thái sau 2 phút / khi bot đăng tin trong thread), `TYPING_MAX_S = 1800`; hook `manager_run_started|finished`; `handle_action` nhận nút `ms` (còn lại → lõi) |
| `chat/base.py` | `typing(api, chat, *, reply_to=None)`; hook rỗng `manager_run_started(api, chat, run_id, reply_to=)` / `manager_run_finished(run_id, chat=, reply_to=, outcome=)` — Telegram không đổi |
| `chat/manager_chat.py` | gọi `manager_run_started` sau `manager.chat` (TRƯỚC vòng "đang gõ"), `manager_run_finished` khi giao trả lời / run kết thúc không trả lời; nhịp "đang gõ" lấy `bot.TYPING_EVERY_S|TYPING_MAX_S` nếu có (Telegram vẫn 4 s / 180 s) |
| `chat/bot.py` | `TelegramBot.typing` nhận (và bỏ qua) `reply_to` — lời gọi Bot API không đổi |
| `config.py` | `notify.slack.manager_progress` (bật, mặc định `true`), `notify.slack.manager_progress_edit_s` (3) |

- **Event tới bot**: `ChatBot` nghe bus bằng listener ĐỒNG BỘ (`hub.bus.add_listener`) — nhận MỌI event, không qua bộ lọc level của
  `Subscription`; `tool.start`/`tool.complete` (level summary, `claude_adapter`) tới đủ, kể cả của agent phụ (`subagent_id` → "↳").
  Run chạy ngay sau `manager.chat` nên event có thể tới trước khi có bản ghi → nạp lại từ store (`query_events(run_id, types)`), khử trùng
  theo `tool_id`. Run con (resume sau duyệt, `run.queued{parent_run_id}`) nối vào cùng tin; nút Dừng dừng run con. `run.waiting_human` →
  tiêu đề "⏸ Chờ bạn duyệt một thao tác".
- **Trạng thái**: `setStatus{channel_id, thread_ts = gốc thread trả lời sẽ vào, status: "đang xử lý…"}`, nhắc mỗi 90 s, xoá bằng `status: ""`
  khi xong. Tin tiến độ được đăng TRƯỚC khi đặt trạng thái (bot đăng tin trong thread làm Slack xoá trạng thái). Lỗi (vd
  `method_not_supported_for_channel_type`, `missing_scope`, `not_allowed_token_type`) → `reactions.add hourglass_flowing_sand` trên tin của
  người (một lần), `reactions.remove` khi có trả lời / run kết thúc; reaction cũng lỗi (thiếu `reactions:write`) → bỏ êm, log debug, vòng
  nhắc dừng. Thread đã xong được nhớ (`_closed`) → vòng nhắc muộn không đặt lại trạng thái / gắn lại ⏳.
- **Tin tiến độ**: reply vào tin của người (cùng thread với trả lời). Tiêu đề + "N bước · thời gian" + tối đa 8 bước gần nhất
  (`▸` đang chạy, `✓` xong, `✗` lỗi) dạng `` `Tên` — nhãn``. Tên = tên tool chỉ giữ `[A-Za-z0-9_.:-]`, ≤ 40, qua `redact_str`
  (`mcp__srv__tool` → `srv.tool`); nhãn do hub đặt theo loại tool (`TOOL_VI`, tool lạ → "dùng công cụ"). KHÔNG BAO GIỜ `input`,
  `args_preview` hay `result_preview`. Không ghi "Điều phối viên" trong tin tiến độ (tin đó không được trông như khung trả lời WT-8).
- **Nhịp sửa**: `chat.update` ≤ 1 lần / `manager_progress_edit_s` (gộp mọi event trong khoảng), không có event → vẫn sửa mỗi 30 s (thời gian);
  429 → chờ `retry_after` (≤ 60 s) rồi sửa lại với trạng thái mới nhất. Xong → một dòng `✓ Xong · N bước · 42 giây` /
  `⏹ Đã dừng · …` / `⚠️ Chưa xong · …`, gỡ nút; việc này chạy nền — không chặn tin trả lời. Theo dõi tối đa 6 giờ / 100 run.
- **Nút Dừng**: `CallbackStore.issue([("ms", run_gốc, "⏹ Dừng")])` (nonce một lần + HMAC, ràng chat). `_on_actions` kiểm workspace +
  allowlist người TRƯỚC khi tiêu nonce (người lạ trong kênh: `slack.ignored`, nút còn nguyên). Bấm → `bot.call(cid, "run.stop", {id})`
  (cổng người, audit `slack.command`) → tin sửa ngay thành "⏹ Đang dừng…" không nút; lỗi → toast + cấp nút mới. Xong/hết hạn →
  `invalidate_ref`. Trạng thái tiến độ chỉ trong RAM: daemon khởi động lại → tin cũ đứng yên, nút cũ vẫn dừng đúng run (ref = run id).
- **Giới hạn**: trả lời trong thread (`_on_thread_reply`) gửi `message_id = gốc thread` → ⏳ (khi dùng reaction) gắn vào tin gốc chứ không
  phải tin trả lời mới nhất; hai việc song song trong cùng thread dùng chung trạng thái/⏳ của thread. `setStatus` vốn dành cho app
  "AI assistant" — workspace/kênh không hỗ trợ thì luôn rơi về reaction.
- **Scope**: `chat:write` (setStatus, chat.update) đã có; reaction cần thêm `reactions:write` — CHƯA thêm vào manifest (`web/app.py`).
- Test: `tests/test_slack_progress.py` (trạng thái đúng thread + xoá; tin tiến độ đăng/sửa/thu gọn, không lộ input; reaction thay thế được
  gắn + gỡ; cả hai lỗi → bỏ êm; nút Dừng dừng run + người lạ không bấm được + bấm lại bị từ chối; gộp sửa + 429; Telegram không đổi).
  Slack giả: `assistant.threads.setStatus` (`fail_status`), `reactions.add|remove` (`fail_reactions`), `ratelimit[method]` → HTTP 429.

## Chưa làm
- Thêm `reactions:write` vào manifest 2 chiều (đường dự phòng ⏳ của P7) — app đã cài cần Reinstall.
- Nghiệm thu trên workspace Slack thật: chưa chạy (cả tag trong kênh — P6, `setStatus`/tin tiến độ — P7) (modal `views.open`, tải `url_private` với `files:read`, tệp từ máy tính + điện thoại).
