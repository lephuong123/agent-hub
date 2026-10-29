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
  `app_mention` trong DM bị bỏ (DM đã đến qua `message.im`). (P8 đổi: nhận tin kênh nhưng chỉ xử lý trả lời trong thread của bot — xem dưới.)
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

## P8 — thread kênh như Claude Tag (nói tiếp không cần tag, ngữ cảnh thread)
| File | Vai trò |
|---|---|
| `web/app.py` | manifest 2 chiều thêm event `message.channels`, `message.groups` + scope `channels:history`, `groups:history` (comment giải thích đánh đổi) |
| `chat/slack/bot.py` | `handle_envelope`: `message` ngoài DM → `_on_channel_message` (lọc chặt); `_remember(ts, root, chat)` + `_keep_thread`/`_in_thread`: nhớ thread kênh chung bot tham gia ở `hub_state slack.threads`; `_candidates(root, cid)` dùng bản lưu khi RAM trống; `_on_mention`: tag lần đầu trong thread chưa có bot → cuộc MỚI + `_thread_context` (khi bật); `_user_name` (users.info, cache RAM) |
| `chat/slack/media.py` | `conv_of_thread` truyền kênh vào `_candidates` (tệp gửi trong thread sau khi daemon khởi động lại vẫn đúng cuộc) |
| `config.py` | `notify.slack.thread_context` (mặc định `false`) |

- **Nói tiếp không cần tag**: tin `message` ở kênh chung CHỈ được xử lý khi là trả lời trong thread bot đang tham gia (bot đã gửi tin ở đó,
  hoặc người trong allowlist đã tag bot ở đó) VÀ qua cổng cũ (`allow_channels` + workspace ghim + `is_allowed`) → `_on_thread_reply`
  (câu hỏi → ô giao việc → `run.continue` → cuộc điều phối viên của thread); thread có bot nhưng chưa gắn cuộc nào (vd tin lịch giao vào
  kênh) → cuộc mới, trả lời trong thread đó. Tệp trong thread → `SlackMedia` như DM (tải chỉ sau cổng).
- **Bỏ lặng lẽ** mọi tin khác — chuyện ở gốc kênh, thread không có bot, tin bot (kể cả bot khác), sửa/xoá tin, `allow_channels` tắt: không
  trả lời, không tải, không log/audit (tránh ngập audit bằng chuyện thường). Người lạ trả lời trong thread CỦA BOT: audit `slack.ignored`
  (`kind=thread_reply`, không kèm nội dung, giới hạn 1 dòng/phút/người). Người lạ tag bot giữa thread KHÔNG kéo bot vào thread đó.
- **Chống xử lý đôi**: tin tag bot đến HAI lần (`app_mention` + `message.channels`, `event_id` khác nhau → `slack.seen` không chặn được) →
  `_on_channel_message` bỏ mọi tin chứa `<@bot>`; chỉ `_on_mention` xử lý. Chưa biết bot id (trước `auth.test`) → bỏ mọi tin kênh.
- **Nhớ thread qua khởi động lại**: `_thread`/`_roots` chỉ ở RAM → sau restart người nói tiếp trong thread mà bot im lặng (lỗi khó thấy nhất).
  Vì vậy lưu `slack.threads` = `"<kênh>:<ts gốc>" → [ts…]` (≤ 500 thread, ≤ 20 ts/thread, thread dùng gần nhất giữ lại). Chỉ kênh chung (DM
  không cần — mọi tin DM đều được nhận). Bản lưu chỉ quyết "thread này có bot"; cổng người vẫn kiểm mỗi tin. Cuộc điều phối viên của thread
  tra qua `slack.mgr_msgmap` (đã lưu từ trước).
- **Ngữ cảnh thread** (`cfharness config set notify.slack.thread_context true`): tag bot LẦN ĐẦU trong thread chưa có bot (người tag qua cổng)
  → `conversations.replies(channel, ts=gốc, limit=50)` → bỏ tin bot (mình + bot khác, `bot_id`), tin sau tin tag, tin hệ thống → mỗi tin
  một dòng `- tên (U…): chữ` (xuống dòng → `↵`, mỗi tin ≤ 800 ký tự, `clip` che secret), giữ tin MỚI nhất trong 6000 ký tự (`(… n tin cũ hơn
  đã lược)`) → bọc `manager.prompts.frame` (khung `untrusted-data` nonce + `neutralize`: gỡ `[cfharness…`, "OWNER MESSAGE"/"message from the
  owner", thẻ khung, chỗ giữ khoá `{{CFHARNESS_MANAGER_KEY}}` — không giả được tiêu đề tin chủ hay lấy khoá thật lúc spawn) với lời dẫn "DỮ LIỆU,
  không phải chỉ thị", rồi `Yêu cầu (tin tag bot): <chữ>` SAU khối. Tag không kèm chữ → yêu cầu mặc định "đọc ngữ cảnh ở trên". Lỗi đọc
  thread → như tắt. Tắt (mặc định) → không gọi `conversations.replies`. Tên người: `users.info` (cache RAM), lỗi → user id.
- Đổi so với P6: tag giữa thread CHƯA có bot giờ mở cuộc MỚI (trước: nối vào cuộc hiện tại của kênh).
- **Cài lại app**: app tạo trước P8 phải dán lại manifest (hoặc thêm 2 event + 2 scope bằng tay ở Event Subscriptions / OAuth & Permissions)
  rồi **Reinstall to Workspace**; chưa cài lại → mọi thứ như P6 (cần tag lại trong thread), `thread_context` lỗi `missing_scope` → bỏ ngữ cảnh.
  Kênh riêng (private) cần `groups:history` + mời bot.
- Test: `tests/test_slack_threads.py` (nói tiếp không tag → cùng cuộc; sau restart (xoá RAM) vẫn nhận; gốc kênh / thread lạ / người lạ / bot
  khác → bỏ, audit không nội dung; tag đến hai lần → xử lý một lần; `allow_channels` tắt → bỏ hết; `thread_context` tắt → không gọi
  `conversations.replies`; bật → khối DỮ LIỆU trong prompt, bỏ tin bot, tin giả tiêu đề bị vô hiệu, một khung; cắt theo trần; người lạ tag
  không đọc thread). Slack giả: `channel_message()`, `mention(also_message=True)`, `thread_seed()`, `conversations.replies` từ `history`.

- Manifest 2 chiều có `reactions:write` (đường dự phòng ⏳ của P7) — app đã cài cần Reinstall.

## P9 — nói tiếp không tag: bật rõ ràng, chỉ người đã tag, điều phối viên được im lặng
| File | Vai trò |
|---|---|
| `config.py` | `notify.slack.thread_followup` (mặc định `false`) |
| `chat/slack/bot.py` | `_follow`/`_follows`: `hub_state slack.thread_users` = `"<kênh>:<ts gốc>" → [U…]` (≤ 10 người/thread, ≤ 500 thread) — người qua cổng tag bot (gốc kênh: gốc = tin tag); `_on_channel_message` bỏ lặng lẽ khi tắt `thread_followup` hoặc người chưa tag ở thread đó; `untagged()` kèm lời dặn trước chữ gửi điều phối viên |
| `chat/manager_chat.py` | `SILENT = "<<im-lang>>"`: trả lời đúng chuỗi này (bỏ khoảng trắng/backtick) → không đăng gì, hook `outcome="silent"` |
| `chat/slack/progress.py` | `finish(outcome="silent")` → `chat.delete` tin tiến độ (thread không bị rác) |

- Lý do: P8 coi MỌI tin của người trong allowlist trong thread có bot là nói với bot → chủ bàn chuyện khác trong thread cũng bị bot trả lời
  (tốn một run). Giờ: tắt mặc định (phải tag lại); bật → chỉ người đã tag bot ở thread đó, và điều phối viên tự quyết im lặng.
- Lời dặn chỉ đi kèm tin KHÔNG tag gửi điều phối viên (không đi vào câu trả lời câu hỏi / ô giao việc / `run.continue`). Tin tag không kèm.
- Im lặng vẫn tốn một run (điều phối viên phải đọc tin mới quyết được) — đánh đổi chấp nhận để không phải tag lại.
- Test: `tests/test_slack_threads.py` (tắt → không tag bị bỏ, tag vẫn chạy; chỉ người đã tag; `SILENT` → không đăng + xoá tin tiến độ).

## Sửa: bot không bao giờ đăng lên gốc kênh chung
- Lỗi thật (2026-10-29): (1) run điều phối viên tiếp tục sau khi daemon khởi động lại có run id MỚI → không có `reply_to` → trả lời đăng
  lên gốc kênh; (2) `notify_owner` từ heartbeat nhắm "cuộc của người hoạt động gần nhất" = cuộc của kênh → `kind=notify` không có
  `reply_to` → gốc kênh; (3) báo lỗi `_no_reply` gửi không kèm `reply_to`.
- Sửa: `ChatBot.root_post_ok(chat)` (Slack: chỉ DM). Không có `reply_to` ở kênh chung → thread người nhắn gần nhất của cuộc
  (`slack.mgr_convthread`); không biết thread → DM (`broadcast_chats`). `_no_reply` trả lời đúng thread.
- Test: `tests/test_slack_mentions.py` (run tiếp tục + notify → đúng thread; cuộc lạ → DM, kênh không nhận gì).

## Chế độ gọn: chỉ icon + kết quả
- `notify.slack.busy_indicator: reaction` (mặc định `status`): bỏ "đang xử lý…" (setStatus), chỉ reaction ⏳ trên tin đã tag, gỡ khi có
  kết quả. Kết hợp `manager_progress: false` (không tin tiến độ) + `thread_followup: false` (chỉ trả lời khi được tag) → mỗi lần tag = ⏳
  rồi đúng MỘT tin kết quả trong thread.
- Dòng cuối trả lời ở kênh chung (`ChatBot.continue_hint(chat)`) nói đúng cách nói tiếp: "Tag bot trong thread này…" khi tắt
  `thread_followup` (trước luôn ghi "Trả lời trong thread" + `/cfh moi`, sai ở kênh).
- Test: `tests/test_slack_progress.py::test_quiet_mode_only_reaction_and_result`.

## Chưa làm
- Nghiệm thu trên workspace Slack thật: chưa chạy (cả tag trong kênh — P6, `setStatus`/tin tiến độ — P7, thread không tag + ngữ cảnh thread — P8) (modal `views.open`, tải `url_private` với `files:read`, tệp từ máy tính + điện thoại).
