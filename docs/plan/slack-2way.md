# Slack 2 chiều — điều khiển hub qua Slack ngang Telegram

Yêu cầu (2026-10-26): "thao tác và kiểm soát công việc 2 chiều qua Slack không được như với Tele à?" → đưa Slack từ kênh
thông báo một chiều lên kênh người điều khiển hub, cùng mức an toàn với luồng TG (`docs/dev-notes/telegram.md`).

## Hiện trạng
| | Slack | Telegram |
|---|---|---|
| Mã | `notify/slack.py` (170 dòng): webhook / `chat.postMessage` | `notify/telegram.py` + `chat/` (~2.700 dòng) |
| Nhận tin | không | long polling `getUpdates` |
| Duyệt | in lệnh `cfharness approve <id>` | nút inline, nonce + HMAC, 2 bước, gỡ nút khi quyết ở kênh khác |
| Lệnh | không | `/canban /hoi /giaoviec /agent /luotchay /approvals /stop /estop /digest /lich /moi` |
| Hội thoại | không | reply → `run.continue`; tin thường → điều phối viên; câu hỏi agent; ảnh/tệp |
| Danh tính | không cần | `auth.telegram_caller` → `Daemon.call_as` (HUMAN_ONLY + audit `telegram:<chat>`) |

Lý do một chiều: web wizard ghi rõ "hub chỉ nghe loopback, Slack không gọi ngược được" (`web/app.py:62`), và M1-B đã hoãn
"Duyệt bằng nút Slack (sau M5)". **Socket Mode** gỡ đúng ràng buộc này: hub mở WebSocket RA ngoài tới Slack, không cần URL công khai —
tương đương long polling của Telegram.

## Nguyên tắc (không đổi so với luồng TG)
- Slack là CLIENT của daemon: mọi thao tác qua `Daemon.call_as(auth.slack_caller(...))` → cổng HUMAN_ONLY, `CURRENT_CALLER`, audit kênh `slack:<user_id>`.
- Không nới lỏng `auth.py`; chỉ thêm kênh. Allowlist chỉ người sửa (RPC `slack.*` ∈ HUMAN_ONLY; nhánh `notify` của config.yaml đã được `guard/integrity` kiểm).
- Agent không đọc được token Slack (secret store + sandbox); classifier: `cfharness notify slack …` từ agent → forbidden.
- Nội dung agent viết không bao giờ cùng tin với nút (WT-8); mọi chuỗi qua `redact_str`; tool MCP chỉ tên.
- Tắt êm khi chưa cấu hình. Chế độ webhook một chiều hiện có vẫn chạy y nguyên cho ai không bật 2 chiều.
- Giao diện tiếng Việt. Test không gọi Slack thật (server giả cục bộ).

## Khác biệt nền tảng phải xử lý (Slack ≠ Telegram)
| Vấn đề | Telegram | Slack — cách làm |
|---|---|---|
| Nhận sự kiện | `getUpdates` + offset | Socket Mode: `apps.connections.open` (app token `xapp-`, scope `connections:write`) → `wss://…`; envelope `hello` / `events_api` / `interactive` / `slash_commands` / `disconnect`. **ACK `{"envelope_id"}` trong 3 s** rồi mới xử lý (task nền). Slack tự xoay kết nối vài giờ một lần (`disconnect` có `reason=warning/refresh_requested`) → mở kết nối mới trước, đóng cũ sau. |
| Chống xử lý lại | offset lưu trước khi xử lý | Slack gửi lại nếu không ACK (`retry_attempt`) → khoá chống trùng theo `envelope_id` / `event_id` / `action_ts` trong `hub_state` (`slack.seen`, TTL 1 h), ghi TRƯỚC khi xử lý. |
| Lệnh `/…` | gõ `/run` trong chat | Client Slack **chặn mọi tin bắt đầu bằng `/`** ("không phải lệnh hợp lệ") → đăng ký MỘT slash command `/cfh <lệnh> …` (`/cfh canban`, `/cfh giaoviec dev sửa lỗi X`); thêm alias không dấu `/cfh run …`. Kết quả gửi vào DM của người với bot (không phụ thuộc kênh gọi lệnh); gọi trong kênh bot không là thành viên → trả ephemeral qua `response_url`. |
| Reply để tiếp tục | `reply_to_message` | **Thread**: `thread_ts` của tin cha ↔ `msgmap["<channel>:<ts>"]`. Tin trả lời trong thread của tin kết quả → `run.continue`; thread của tin điều phối viên → đúng hội thoại; thread của câu hỏi → câu trả lời. |
| Ô nhập (`force_reply`) | có | Không có → **modal** (`views.open` với `trigger_id` từ nút/slash): ô "Yêu cầu" cho giao việc, ô trả lời chữ cho câu hỏi. Submit = envelope `interactive` `view_submission`. |
| Nút | `callback_data` ≤ 64 byte | Block Kit `button`: `action_id` + `value` (≤ 2000). Vẫn đưa `v1.<nonce>.<hmac>` vào `value` — dùng lại `CallbackStore` (một lần, tiêu cả nhóm, TTL). Sửa/gỡ nút: `chat.update` với `blocks` mới. Nút "Từ chối"/"Xác nhận" dùng `style: danger/primary` + `confirm` dialog của Slack cho bước 2 nặng (ngoài bước 2 của hub, không thay thế). |
| Định dạng | HTML `<pre>` | mrkdwn: escape `& < >` cho MỌI giá trị động (chống `<!channel>`, `<@U…>`, link giả `<http://x|nhãn>`); nội dung agent trong khối ``` sau tiền tố cố định; section text ≤ 3000 ký tự/khối, ≤ 50 khối/tin → `split_chunks` theo 2900; quá dài → `files.uploadV2` (.txt). |
| Danh tính | `chat.id` + `from.id` | Payload Socket Mode đến từ Slack qua kết nối đã xác thực bằng app token → tin được `user.id`, `team.id`. Allowlist = `allowed_user_ids` (U…) **và** `team_id` ghim lúc setup. DM: kênh `D…` của người đó. Kênh chung: tắt mặc định (`allow_channels`), bật rồi vẫn chỉ người trong allowlist được tính là người. |
| Giới hạn tốc độ | 429 + `retry_after` | `chat.postMessage` ~1 tin/s/kênh, `chat.update` Tier 3 → tôn trọng header `Retry-After`; tiến độ sửa tại chỗ ≥ `progress_edit_s` (mặc định 10 s, như TG). |
| Tệp | `getFile` | `files:read` (tải `url_private` với bot token, chỉ SAU khi qua allowlist), gửi ra `files.getUploadURLExternal` + `files.completeUploadExternal`. |

## Kiến trúc
Không chép 2.300 dòng `chat/` thành bản Slack. Tách lớp vận chuyển khỏi logic chat, làm dần theo pha (mỗi pha test Telegram vẫn xanh):

```
chat/
  core/                ← logic dùng chung, không biết Telegram/Slack
    model.py           Msg(text_parts, buttons=[[Btn(label, action, ref, style)]], agent_frame?, reply_to/thread)
    commands.py        bảng lệnh + HELP (đang ở bot.py) — tên lệnh, alias, handler(ctx, rest)
    approvals.py       approval_sys_text / send_approval / _decide / always_offer (đang ở bot.py)
    flows.py           ← chat/flows.py hiện tại, đổi `api: TelegramApi` → `t: Transport`
    questions.py       ← chat/questions_tg.py
    manager_chat.py, media.py (phần không phụ thuộc nền tảng)
  transport.py         Protocol Transport: send(chat, Msg) → MsgRef · edit(ref, Msg|None) · ack_action(id, toast)
                       · open_input(trigger, kind, ref) · send_file(chat, name, bytes) · typing(chat) · label ("Telegram"/"Slack")
  telegram/            bot.py (vòng getUpdates, ghép nối, render Msg → HTML + inline_keyboard) — hành vi giữ nguyên
  slack/
    socket.py          vòng Socket Mode (kết nối, ACK, xoay kết nối, backoff, chống trùng)
    api.py             SlackApi (Web API qua httpx2, gỡ token khỏi lỗi — như TelegramApi._scrub)
    render.py          Msg → blocks/mrkdwn (escape), chia khối
    bot.py             SlackBot: map envelope → core (lệnh /cfh, nút, modal, thread reply, DM thường → điều phối viên)
    rpc.py, cli.py     slack.setup|status|pending|allow|revoke|disable|test
```
- `CallbackStore` chuyển sang `chat/core/callbacks.py`, nhận `prefix` (`telegram.` / `slack.`) cho khoá `hub_state` và khoá HMAC riêng
  (`SLACK_CALLBACK_KEY`). Trạng thái Slack: `slack.callbacks|approval_msgs|watch|msgmap|flow_map|seen` — không cần migration.
- `ChatError`, `channel_label`, `who_label`, `decided_text` lên `core`; `channel_label("slack:U…") = "Slack"` (cả TG hiển thị "đã duyệt qua Slack").
- `approval.resolved` từ BẤT KỲ kênh nào → cả hai bot vô hiệu nonce + gỡ nút tin của mình (đã có cho TG, Slack làm y hệt).

## Thay đổi file dùng chung (chỉ THÊM)
- `auth.py`: `slack_caller(hub, team_id, channel_id, user_id) -> Caller(via=f"slack:{user_id}")` — người khi: bot bật, `team_id` khớp,
  `user_id ∈ allowed_user_ids`, và (DM của chính người đó **hoặc** `allow_channels` bật + `channel_id ∈ allowed_channel_ids`).
  `slack.*` vào `HUMAN_ONLY`; `MUTATING_SLACK` vào `all_mutating`.
- `daemon.py`: đăng ký `chat.slack.rpc`; comment `call_as` nhắc thêm Slack (hàm đã chung, không đổi logic).
- `hub.py`: `hub.slack` (attach như Telegram).
- `config.py` `notify.slack`: thêm `interactive: false`, `app_token_secret: SLACK_APP_TOKEN`, `callback_key_secret: SLACK_CALLBACK_KEY`,
  `team_id`, `allowed_user_ids: []`, `allow_channels: false`, `allowed_channel_ids: []`, `slash_command: "/cfh"`, `callback_ttl_s`,
  `pair_ttl_s`, `progress_after_s`, `progress_edit_s`, `result_max_chars`, `confirm_classes`, `document_max_bytes`, `deliver_*`.
- `notify/slack.py`: khi `interactive` bật và bot đang chạy → `SlackNotifier` NHƯỜNG các event có nút (approval.*, question.*) cho `SlackBot`
  (gửi DM có nút tới từng người allowlist); event còn lại vẫn đi webhook/kênh như cũ. Không gửi trùng.
- `guard/classifier.py`: `HUB_HUMAN_ONLY_CLI["notify"] = {"telegram", "slack"}`.
- `web/app.py` + `settings.html`: wizard "Slack 2 chiều" — manifest mới (dưới), ô dán `xoxb-` + `xapp-`, mã ghép nối, danh sách người đã ghép.
- `pyproject.toml` / `uv.lock`: thư viện WebSocket (xem Quyết định D1).

Manifest app (wizard điền sẵn):
```json
{"display_information": {"name": "cfharness"},
 "features": {"bot_user": {"display_name": "cfharness", "always_online": true},
              "app_home": {"messages_tab_enabled": true, "messages_tab_read_only_enabled": false},
              "slash_commands": [{"command": "/cfh", "description": "Điều khiển cfharness", "usage_hint": "canban | giaoviec <agent> <yêu cầu> | help"}]},
 "oauth_config": {"scopes": {"bot": ["chat:write", "im:history", "im:read", "im:write", "commands", "users:read", "files:read", "files:write"]}},
 "settings": {"socket_mode_enabled": true, "interactivity": {"is_enabled": true},
              "event_subscriptions": {"bot_events": ["message.im"]}, "org_deploy_enabled": false, "token_rotation_enabled": false}}
```
(`app_mentions:read` + `channels:history` chỉ thêm khi bật `allow_channels`.)

## Setup & ghép nối
1. `cfharness notify slack setup` (hoặc web wizard): hỏi bot token `xoxb-` và app token `xapp-` (getpass / `--token-stdin`) → RPC `slack.setup`
   kiểm `auth.test` (lấy `team_id`, `bot_user_id`) + `apps.connections.open` → lưu secret (0600, đăng ký che), ghim `team_id`, sinh
   `SLACK_CALLBACK_KEY`, sinh mã ghép nối 8 hex (TTL 10 phút).
2. Người DM bot `ghepnoi <mã>` (hoặc `/cfh ghepnoi <mã>`) → bot ghi "chờ xác nhận" kèm `user_id`, tên (`users.info`), `team_id`.
3. CLI/web poll `slack.pending`, hiện tên + `user_id`, hỏi y/N → `slack.allow`. Mã dùng một lần. `--user-id` thêm thẳng.
- Người ngoài allowlist: không trả lời, audit `slack.ignored` (1 dòng/phút/người), kể cả nút và slash command (slash: trả ephemeral
  "không có quyền" là đủ — Slack bắt phản hồi trong 3 s; KHÔNG kèm thông tin hub).

## Tiến độ
- 2026-10-26: **P0 xong** (lõi `chat/base.py`, test Telegram xanh không sửa). **P1 xong phần mã + test với Slack giả**
  (`tests/test_slack.py`); chưa nghiệm thu trên workspace Slack thật. Chi tiết: `docs/dev-notes/slack.md`.
- 2026-10-26: **P2 xong** (mã + test Slack giả, `tests/test_slack_modals.py`): modal giao việc (nút agent, `/cfh giaoviec <agent>`) và
  "Trả lời bằng chữ" cho câu hỏi; nonce phía hub `slack.modals`; không có trigger → trả lời trong thread. Lệnh `/cfh …`, theo dõi run,
  thread → `run.continue` đã có từ P1. Chưa nghiệm thu `views.open` trên workspace thật.
- 2026-10-26: **P3 xong phần mã + test với Slack giả** (`tests/test_slack_questions.py`): câu hỏi (nút số, chọn nhiều bằng nút bật/tắt
  thay checkbox, trả lời trong thread), lời dẫn của lõi theo nền tảng (`bot.cmd`, `reply_*`), lịch giao kết quả về `slack:<chat>`.
  Còn: reaction ⏳ thay "đang gõ…" (cần scope `reactions:write` — cùng P5).
- 2026-10-26: **P4 xong phần mã + test với Slack giả** — ảnh/tệp văn bản trong DM → điều phối viên (tải `url_private` CHỈ sau allowlist,
  chỉ host `files.slack.com`, cùng giới hạn Telegram), `[[file: …]]` → files v2, ghi chú tệp trong prompt kênh `slack:`. Phần dùng chung
  tách ở `chat/media.py` (`MediaBase`). Scope không đổi. Test `tests/test_slack_media.py`; chưa nghiệm thu trên workspace thật.
- 2026-10-26: **P5 xong phần web** — wizard "Slack 2 chiều" ở Cài đặt (`web/slack_pages.py`, `tests/test_web_slack.py`, ảnh
  `docs/screenshots/web/v2/23-…`, `24-…`); chạy thử với Slack giả trong Chromium thật. Chưa nghiệm thu trên workspace Slack thật.

## Pha thực hiện
Mỗi pha chạy được độc lập, `uv run pytest -q` xanh, có ghi chú trong `docs/dev-notes/slack.md`.

### P0 — Tách lõi chat (không đổi hành vi) · ~1–1,5 ngày
- Tạo `chat/transport.py`, `chat/core/model.py`, chuyển `CallbackStore` sang core (giữ re-export ở `notify/telegram.py` để import cũ chạy).
- `TelegramTransport` bọc `TelegramApi` + render `Msg` → HTML/inline_keyboard. Chuyển dần `flows`, `questions_tg`, `manager_chat`, `media`,
  phần approval/lệnh của `bot.py` sang nhận `Transport` thay `TelegramApi`.
- Tiêu chí: 5 file `tests/test_telegram*.py` (~1.360 dòng) xanh KHÔNG sửa assert (chỉ sửa import nếu cần). Đây là lưới an toàn của pha.

### P1 — Socket Mode + duyệt bằng nút (giá trị chính) · ~2 ngày
- `slack/socket.py`, `slack/api.py`, `slack/render.py`, `slack/rpc.py`, `slack/cli.py`, `auth.slack_caller`, config, classifier.
- Setup + ghép nối + allowlist; `cfharness notify slack status|test|disable|allow|revoke`.
- Approval: tin NỘI DUNG (thao tác đầy đủ, khối code, chia phần / tệp .txt) + tin HỆ THỐNG trong thread với nút
  [Cho phép] [Luôn cho phép…] [Từ chối] → bước 2 [Xác nhận CHO PHÉP] [Huỷ] (sửa tại chỗ bằng `chat.update`).
  Nhắc lại / waiting_human / escalated như TG. Quyết ở kênh khác → gỡ nút, sửa thành dòng kết quả.
- `/cfh estop on|off` (off 2 bước), `/cfh approvals`, `/cfh help`.
- `SlackNotifier` nhường event có nút cho bot khi `interactive` bật.

### P2 — Lệnh + giao việc + tiếp tục hội thoại · ~1,5 ngày
- `/cfh canban|trangthai|agent|luotchay|giaoviec|stop|digest|lich|moi` (+ alias tiếng Anh) dùng `core/commands.py`.
- Giao việc: `/cfh giaoviec <agent> <yêu cầu>` chạy ngay; thiếu yêu cầu → nút chọn agent → modal nhập yêu cầu.
- Theo dõi run do Slack giao (`slack.watch`, kế thừa run con), tin tiến độ sửa tại chỗ, tin kết quả (outcome, chi phí, lượt, thời gian).
- Trả lời trong thread của tin kết quả → `run.continue`.

### P3 — Câu hỏi của agent + điều phối viên · ~1,5 ngày
- Câu hỏi (`question.*`): tin có nút lựa chọn (chọn nhiều = checkbox Block Kit), "Trả lời bằng chữ" → modal hoặc trả lời trong thread.
- DM chữ thường → điều phối viên (`manager.*`); trả lời trong thread → đúng hội thoại; `/cfh moi` mở hội thoại mới.
  "Đang gõ…": Slack bot không có typing indicator → reaction ⏳ lên tin của người, gỡ khi có trả lời.

### P4 — Ảnh/tệp · ~1 ngày
- Ảnh/tệp văn bản trong DM → điều phối viên (cùng giới hạn TG: ảnh ≤ 5 MB, ≤ 4/tin, tệp văn bản ≤ `document_max_bytes`); tải `url_private`
  CHỈ sau allowlist. `[[file: …]]` từ điều phối viên → upload (chỉ tệp thường trong cwd hội thoại, quét secret như `media._has_secret`).

### P5 — Web + tài liệu · ~0,5–1 ngày
- Wizard web 2 chiều (manifest mới, dán 2 token, ghép nối, danh sách người, tắt/bật `interactive`), trang Cài đặt hiện trạng thái kết nối.
- `docs/dev-notes/slack.md`, mục `AGENTS.md`, cập nhật `docs/dev-notes/M1-B.md` (gỡ dòng "sau M5").

Tổng: ~8–9 ngày công nếu làm đủ; P0+P1 (~3–3,5 ngày) đã giải quyết phần đau nhất (duyệt không cần mở máy).

## Test
Server giả `tests/slack_fake.py`: Web API HTTP cục bộ (`auth.test`, `apps.connections.open`, `chat.postMessage|update`, `views.open`,
`conversations.open`, `users.info`, `files.*`) + WebSocket cục bộ đẩy envelope và ghi nhận ACK.
- **An toàn** (`tests/test_slack.py`): tắt êm không token; `team_id` khác → bỏ qua; người ngoài allowlist (DM, nút, slash, modal) → không tới
  dispatch + audit; `slack_caller` người lạ → BLOCKED cho `estop.set`; `cfharness notify slack …` từ agent → forbidden; token không có trong
  kết quả/audit/log (`_scrub`); escape mrkdwn (`<!channel>`, `<@U…>`, `<http://x|y>` trong lệnh agent hiển thị nguyên văn, không ping/không link).
- **Nút**: value bịa / chữ ký sai / sai người / hết hạn / bấm lại / nút cùng nhóm → từ chối; bấm thật → `approval.resolved` kênh `slack:<U>`;
  quyết qua CLI → nút Slack bị gỡ và nonce vô hiệu.
- **Socket**: ACK trước khi xử lý (handler chậm vẫn ACK < 3 s); envelope gửi lại (`retry_attempt`) không chạy hai lần; `disconnect` →
  kết nối lại; restart daemon giữa chừng không xử lý lại.
- **E2E** (`tests/test_slack_e2e.py`): `/cfh giaoviec` → kết quả + chi phí; reply thread → `run.continue` (run con `parent_run_id`) → kết quả lần 2;
  câu hỏi trả lời bằng nút và bằng chữ; DM → điều phối viên.
- P0: toàn bộ `tests/test_telegram*.py` xanh không sửa assert.
- Nghiệm thu thật (cuối P1 và P3): workspace Slack thử, app cài từ manifest wizard, `claude` thật với haiku — ghi vào dev-notes như M4-B.

## Rủi ro
| Rủi ro | Giảm thiểu |
|---|---|
| P0 làm hỏng Telegram | Test TG là lưới; chuyển từng module, mỗi bước chạy test; không đổi chuỗi hiển thị |
| Slack xoay kết nối / mất mạng → lỡ envelope | Slack gửi lại envelope chưa ACK; chống trùng theo id; khi kết nối lại, quét `approval.list` pending và gửi lại tin cho approval chưa có tin Slack |
| Workspace công ty chặn tự cài app / Socket Mode | Wizard nói rõ cần quyền cài app; fallback vẫn là webhook một chiều + Telegram |
| Một token bot nằm trên máy cá nhân | Như TG: secret store 0600, che theo giá trị, sandbox chặn agent; mỗi người một app riêng (giữ mô hình hiện tại của wizard) |
| Slack mrkdwn injection | Escape tập trung trong `slack/render.py`, test riêng |

## Quyết định
Đã chốt (2026-10-26): **D1 = thêm `websockets`**, **D2 = làm P0 rồi P1**. D3, D4 theo đề xuất bên dưới cho tới khi có ý kiến khác.

- **D1 — Thư viện WebSocket.** Repo chưa có client WS (`httpx2` không hỗ trợ). Đề xuất: thêm `websockets` (thuần Python, không phụ thuộc
  con, dùng luôn cho server giả trong test). Phương án khác: tự viết client RFC 6455 tối thiểu trên `asyncio` (~200 dòng, tự lo mask/ping/
  phân mảnh) — tránh thêm phụ thuộc nhưng thêm mã bảo trì.
- **D2 — Có làm P0 (tách lõi) không.** Đề xuất có: không có P0 thì P2–P4 là chép lại ~1.500 dòng và mọi sửa sau phải làm hai nơi.
  Nếu chỉ cần duyệt bằng nút gấp: làm P1 trên lõi hiện tại (chỉ dùng chung `CallbackStore`), P0 làm sau.
- **D3 — Kênh chung.** Đề xuất v1 chỉ DM (`allow_channels: false`); kênh team để sau khi có nhu cầu thật.
- **D4 — Tên slash command.** `/cfh` (ngắn). Mỗi người một app riêng → nhiều app cùng `/cfh` trong một workspace; Slack xử lý trùng
  tên thế nào cần kiểm trên workspace thật ở nghiệm thu P1. Để an toàn: tên cấu hình được (`slash_command`), wizard gợi ý `/cfh-<tên>`.
