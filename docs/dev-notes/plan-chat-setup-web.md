# Plan — Kết nối Telegram & Slack qua web nhanh nhất (tối ưu cài đặt + UI/UX)

Trạng thái: **ĐÃ CHỐT 2026-11-28** — chủ repo chọn mọi phương án "Đề xuất" ở mục 7 (Q1 Slack mặc định 2 chiều; Q2 bỏ trang xác nhận
trung gian cho người vừa ghép nối chat riêng/DM; Q3 QR bằng `segno` server-side SVG; Q4 mở rộng RPC `notify.test{channels}`; Q5 preset sự
kiện chung hai kênh, riêng trong Nâng cao).

## 1. Mục tiêu

Người dùng mở web → **Cài đặt → Thông báo & kênh** → kết nối xong Telegram hoặc Slack **không cần terminal**, ít bước nhất:

| Kênh | Hiện nay | Mục tiêu |
|---|---|---|
| Telegram | Chỉ CLI (`cfharness notify telegram setup`), web không làm được | ≤ 2 phút, 3 bước, 0 lệnh |
| Slack 2 chiều | Web làm được nhưng 4 bước trên api.slack.com, dán 2 token cùng lúc, tự tìm trang trong app | ≤ 5 phút, có link thẳng tới đúng trang của app vừa tạo |
| Gửi thử / chẩn đoán | Nút "Gửi thông báo thử" chỉ đi Slack webhook; lỗi hiện chuỗi thô | Thử theo từng kênh; lỗi kèm cách sửa |

Không đổi bất biến an toàn (AGENTS.md mục Telegram/Slack): token chỉ ở secret store, ghép nối bằng mã một lần + người xác nhận
trên phiên web, mọi ghi qua `ACTIONS` (CSRF + khoá phiên, audit `web`), RPC ghi ∈ `HUMAN_ONLY`.

## 2. Hiện trạng — vấn đề tìm thấy

1. **Telegram không có wizard web.** RPC đã đủ (`telegram.setup|pending|allow|revoke|disable|test`, `chat/rpc.py`), nhưng
   `settings/notifications.html:13` chỉ ghi "chạy `cfharness notify telegram setup`". Đây là khoảng trống lớn nhất.
2. **Trạng thái Telegram sai nghĩa.** Pill "đã cấu hình" dựa vào việc secret `TELEGRAM_BOT_TOKEN` tồn tại (`app._tab_notifications`), không
   đọc `telegram.status` → token bị thu hồi / long polling chết vẫn hiện xanh.
3. **Slack có 2 thẻ cạnh tranh nhau.** "Kết nối Slack" (webhook 1 chiều) và "Slack 2 chiều" đứng ngang hàng; người dùng phải tự hiểu khác biệt.
   Khi bot 2 chiều chạy, webhook nhường (không gửi) → thẻ webhook thừa và gây rối.
4. **Slack 2 chiều: bước khó nằm ngoài hub.** Sau khi tạo app từ manifest, người dùng phải tự tìm "OAuth & Permissions" (xoxb) và "Basic
   Information → App-Level Tokens → thêm scope `connections:write`" (xapp); form đòi dán CẢ HAI token một lần — sai một cái là làm lại cả hai.
5. **Nút "Gửi thông báo thử" gây hiểu nhầm.** `notify_test` → RPC `notify.test` = `hub.notifier.send_now` = chỉ Slack webhook
   (`guard/rpc.py:218`). Người chỉ dùng Telegram bấm → không nhận gì.
6. **Cấu hình thông báo là một danh sách phẳng ~20 khoá** trộn chung/Slack/Telegram (`settings_schema.py:65-88`); `events` là multi-select
   **28 mã event thô** (`approval.waiting_human`, `audit.anchored`, `filehub.lane.roots`…) — người dùng không biết chọn gì.
7. **Danh sách người được phép chỉ là id** (`D0123…`, `123456789`) — không biết ai là ai, khó gỡ đúng người.
8. **Ghép nối chưa tận dụng deep link.** Telegram có `t.me/<bot>?start=<mã>` (RPC đã trả `deep_link`) nhưng web không dùng; không có QR
   cho trường hợp mở web trên máy tính mà Telegram trên điện thoại. Slack không có link mở DM với bot.
9. **Bước xác nhận dư.** Bấm "Xác nhận" người chờ ghép nối → sang trang xác nhận riêng (`Action.confirm`) → bấm lần nữa. Mã đã một lần +
   đang trong phiên web có khoá phiên — trang trung gian không thêm an toàn đáng kể nhưng thêm một lần chuyển trang (xem Q2).
10. **Không có lối vào từ trang chủ.** Hub mới không gợi ý "nhận duyệt trên điện thoại" — người dùng phải biết vào Cài đặt.

## 3. Thiết kế UX đích

### 3.1 Trang "Thông báo & kênh" (`/settings/notifications`)

```
┌ Kênh của bạn ───────────────────────────────────────────────┐
│ [Telegram]  ● Hoạt động · @my_cfh_bot · 1 người   [Gửi thử] [Quản lý ▸] │
│ [Slack]     ○ Chưa kết nối  — duyệt bằng nút ngay trong Slack  [Kết nối ▸] │
└─────────────────────────────────────────────────────────────┘
┌ Nhận thông báo gì ──────────────────────────────────────────┐
│ ( ) Chỉ việc cần tôi   (•) Khuyên dùng   ( ) Tất cả   ( ) Tuỳ chỉnh… │
└─────────────────────────────────────────────────────────────┘
▸ Nâng cao (nhắc lại, lớp duyệt 2 bước, webhook Slack 1 chiều, …)
```

- Mỗi kênh MỘT trạng thái, tính ở hàm thuần `present.channel_state(kind, status)` → `off | setup | pairing | ok | error` + câu tiếng Việt
  + hành động gợi ý. Dùng chung cho thẻ kênh, trang chủ, `/settings` (danh mục).
- Bấm "Kết nối"/"Quản lý" → trang riêng `/settings/notifications/telegram` và `/settings/notifications/slack` (stepper), giữ trang chính gọn.

### 3.2 Wizard Telegram — 3 bước (`/settings/notifications/telegram`)

1. **Tạo bot** — nút "Mở @BotFather" (`https://t.me/BotFather`), ô chữ có nút copy `/newbot`, gợi ý tên bot (`cfh_<tên máy>_bot`).
   Ghi chú 1 dòng: "BotFather trả về token dạng `123456:ABC…` — copy nó".
2. **Dán token** → `telegram.setup{token}` (đã có: `getMe`, lưu secret, sinh mã). Hiện "✓ Bot @xxx" rồi tự sang bước 3 (không trang xác nhận
   ghi đè nếu CHƯA có token; còn token cũ thì vẫn cảnh báo ghi đè).
3. **Ghép nối điện thoại** — nút lớn "Mở Telegram và bấm Start" (deep link `t.me/<bot>?start=<mã>`) + **mã QR** của cùng link (mở web trên
   máy tính → quét bằng điện thoại) + dự phòng "hoặc nhắn `/start <mã>`". Poll `telegram.pending` 3 s (htmx, như Slack). Người nhắn hiện thành thẻ
   "Nguyễn A (@a) · chat riêng — [Đây là tôi — cho phép]". Xác nhận → `telegram.allow` → bot gửi tin chào → màn "Xong" có nút "Gửi tin thử".

Nhóm Telegram (`allow_groups`) để trong "Nâng cao" của trang Quản lý, không nằm trên đường chính.

### 3.3 Wizard Slack — từng token một (`/settings/notifications/slack`)

1. **Tạo app** — nút "Tạo app Slack" (manifest 2 chiều sẵn có). Ô nhỏ đổi tên lệnh slash TRƯỚC khi bấm (nhiều người chung workspace) — ghi
   `notify.slack.slash_command` rồi dựng lại URL (đã có `slack_2way_create_url`).
2. **Cài vào workspace + dán Bot token** — hướng dẫn "Install to Workspace → Allow → copy Bot User OAuth Token". Dán `xoxb-…` →
   RPC MỚI `slack.verify_bot{bot_token}`: `auth.test` + `bots.info` (manifest đã có scope `users:read`) → lưu `SLACK_BOT_TOKEN`, trả
   `{team, team_id, bot_user_id, app_id}`; hub nhớ `app_id` (`hub_state slack.app`).
3. **App token** — vì đã biết `app_id`, hiện **link thẳng** `https://api.slack.com/apps/<app_id>/general` ("cuộn tới App-Level Tokens →
   Generate → thêm scope `connections:write`"). Dán `xapp-…` → `slack.setup{app_token}` (đã hỗ trợ: thiếu `bot_token` thì dùng token đã lưu).
4. **Ghép nối** — nút "Mở DM với bot" (`https://slack.com/app_redirect?app=<app_id>&team=<team_id>`) + mã `ghepnoi <mã>` có nút copy; poll
   `slack.pending`; xác nhận như Telegram.

Webhook 1 chiều chuyển xuống "Nâng cao → Chỉ nhận thông báo vào một kênh (webhook)"; khi 2 chiều đang chạy ghi rõ "webhook đang nhường".

### 3.4 Chung cho hai kênh

- **Người được phép có tên**: lúc `allow` lưu `{chat_id, user_id, name, username, at}` vào `hub_state telegram.peers` / `slack.peers`;
  bảng hiện "Nguyễn A (@a) · từ 3 ngày trước — [Gỡ]". Id vẫn hiện nhỏ. Tên là DỮ LIỆU người ngoài → autoescape, một dòng, ≤ 60 ký tự.
- **Gửi thử theo kênh** (`telegram.test`, `slack.test`, webhook) + nút chung ở đầu trang gửi tới MỌI kênh đang bật (sửa vấn đề 5):
  RPC `notify.test` nhận thêm `{channels:"all"}` hoặc web gọi lần lượt — xem Q4.
- **Chẩn đoán** (`chat/diagnose.py`, hàm thuần): ánh xạ `last_error` → câu tiếng Việt + nút sửa:
  Telegram 401 (token bị thu hồi → "Dán token mới"), 409 Conflict (bot đang chạy ở máy/hub khác hoặc có webhook → nút "Gỡ webhook"
  = `deleteWebhook`, RPC `telegram.repair`), mạng; Slack `invalid_auth`, `not_allowed_token_type` (dán nhầm xoxb/xapp), Socket Mode chưa bật,
  workspace khác workspace đã ghim. Hiện "liên lạc lần cuối: 2 phút trước" (`last_poll_at`/`connected_at`).
- **Nhận thông báo gì**: nhóm event có nhãn tiếng Việt (`notify_events.py`):
  *Cần bạn* (approval.*, question, team.inbox_new) · *Lỗi & ngân sách* (run.repeated_failure, budget.*) · *Học & digest* (learning.*,
  golden.*, regression.detected) · *Bảo mật* (guard.*, auth.denied, audit.*, admin.*) · *Thư mục chung* (filehub.*).
  Preset: "Chỉ việc cần tôi" / "Khuyên dùng" (= mặc định hiện tại) / "Tất cả" / "Tuỳ chỉnh" (checkbox theo nhóm, mở rộng ra từng event có nhãn).
  Ghi vẫn là `config.set notify.<kênh>.events` (danh sách đầy đủ). Mặc định áp cho cả hai kênh; "Tuỳ chỉnh riêng từng kênh" trong Nâng cao.
- **Trang chủ**: khi chưa kênh nào `ok` → thẻ "Nhận yêu cầu duyệt trên điện thoại — Kết nối Telegram (2 phút) · Slack", ẩn được
  (localStorage, try/catch). Danh mục `/settings` mô tả dòng "Thông báo & kênh" theo trạng thái thật.

## 4. Bảo mật — giữ nguyên / bổ sung

- Token: `type=password`, `Action.secret_fields`, RPC pop token trước audit/log, không hiện lại, không vào `Session.extra`. Wizard nhiều bước
  KHÔNG giữ token giữa các bước ở web — mỗi token lưu vào secret store ngay ở bước của nó (bước 2 Slack lưu bot token; bước 3 chỉ gửi app token).
- `slack.verify_bot`, `telegram.repair` → `MUTATING_SLACK`/`MUTATING_TELEGRAM` + `auth.HUMAN_ONLY` + ca trong `tests/test_sec_auth.py`;
  không ∈ `MANAGER_ALLOWED`.
- Ghép nối: vẫn mã một lần (TTL `pair_ttl_s`) + người bấm xác nhận trên phiên web đã đăng nhập (CSRF + khoá phiên). QR/deep link chứa mã ghép
  nối — rủi ro như hiện chữ mã trên màn hình (lộ khi chia sẻ màn hình); ghi chú dưới QR "đừng chia sẻ màn hình lúc này".
- QR dựng ở SERVER thành SVG (không CDN, không script inline, CSP giữ `'self'`) — xem Q3.
- `telegram.peers`/`slack.peers` trong `hub_state`: thêm vào `hub_transfer.CLONE_STATE` (clone bỏ token Telegram/Slack → peers cũng bỏ), giữ khi
  `move`; ca trong `tests/test_hub_transfer.py`.
- Nội dung tên/username từ Telegram/Slack là dữ liệu không tin cậy: `termsafe.one_line`, autoescape, không `|safe`.

## 5. Chia PR (theo thứ tự giá trị)

### PR1 — Wizard Telegram trên web (lấp khoảng trống lớn nhất)
- `web/telegram_pages.py` (song sinh `slack_pages.py`): `telegram_ctx`, `partial_pending` (`/partials/telegram/pending`, poll 3 s,
  không gia hạn phiên), actions `tg_setup` (secret_fields `token`), `tg_pair`, `tg_allow`, `tg_revoke`, `tg_test`, `tg_disable`.
- Template `settings/telegram.html` (stepper 3 bước) + `_telegram_pending.html`; deep link + nút copy (đã có helper clipboard trong `app.js`).
- Trạng thái đọc `telegram.status` (sửa vấn đề 2).
- Test `tests/test_web_telegram.py` (daemon in-process + `tests/telegram_fake.py`): token sai không lộ trong HTML/audit, ghép nối →
  pending hiện → allow → allowlist, chưa đăng nhập 401/redirect, thiếu CSRF bị chặn, tên người có `<script>` bị escape.
- CHANGELOG "Chưa phát hành" + ảnh `docs/screenshots/web/`.

### PR2 — Tái cấu trúc trang kênh + tên người + gửi thử đúng
- Thẻ kênh + `present.channel_state` (thuần, test bảng trạng thái); trang `/settings/notifications/slack` (chuyển khối Slack 2 chiều hiện
  có sang, webhook vào Nâng cao); `telegram.peers`/`slack.peers` (ghi ở `allow`, xoá ở `revoke`); gửi thử theo kênh + nút chung.
- Link cũ `/settings/notifications#slack|#slack2` vẫn tới đúng chỗ (redirect/neo trong `_nav.html`).

### PR3 — Slack từng bước + link thẳng tới app
- RPC `slack.verify_bot` (+ `hub_state slack.app`), stepper 4 bước, link `api.slack.com/apps/<app_id>/general`, `app_redirect`.
- Đổi lệnh slash ngay trong bước 1. Test trên `tests/slack_fake.py` (thêm `bots.info`).

### PR4 — "Nhận thông báo gì" theo nhóm + nhãn
- `notify_events.py` (`EVENT_GROUPS`, `EVENT_LABELS`, `PRESETS`); test: mọi event trong `DEFAULTS.notify.*.events` + mọi event có
  thể gửi đều có nhãn (thêm event mới quên nhãn → test đỏ). Widget preset + checkbox nhóm thay multi-select thô; cấu hình nâng cao tách theo kênh.

### PR5 — Chẩn đoán + lối vào từ trang chủ
- `chat/diagnose.py` (thuần) + RPC `telegram.repair` (deleteWebhook); thẻ gợi ý ở trang chủ; mô tả động ở danh mục `/settings`.

### PR6 (tuỳ chọn) — Mã QR
- SVG server-side cho deep link Telegram (và link DM Slack). Tách riêng để không chặn PR1.

Ước lượng: PR1 ~1 ngày, PR2 ~1 ngày, PR3 ~0,5–1 ngày, PR4 ~0,5 ngày, PR5 ~0,5 ngày, PR6 ~0,5 ngày.

## 6. Nghiệm thu

- Test tự động mỗi PR (không gọi Telegram/Slack thật — server giả sẵn có).
- Thử tay trên Telegram + Slack thật, bấm giờ từ "mở trang Cài đặt" tới "nhận tin thử": Telegram ≤ 2 phút, Slack ≤ 5 phút; cả trên điện thoại
  (web public) lẫn máy tính. Ghi kết quả vào `docs/dev-notes/telegram.md` / `slack.md` (mục "Chưa làm" của slack.md: nghiệm thu thật).
- `uv run pytest -q` xanh; `scripts/check_broad_except.py` không tăng baseline.

## 7. Câu hỏi cần chốt

- **Q1 — Slack mặc định 2 chiều?** Đề xuất: đường chính chỉ là 2 chiều, webhook 1 chiều thành tuỳ chọn nâng cao. (Phương án khác: hỏi
  "chỉ nhận thông báo" vs "nhận + điều khiển" ở đầu wizard.)
- **Q2 — Bỏ trang xác nhận trung gian khi cho phép người vừa ghép nối?** Đề xuất: bấm "Đây là tôi — cho phép" trên thẻ là đủ (mã một lần +
  phiên web + cảnh báo ngay trên thẻ). Giữ trang xác nhận cho nhóm Telegram / kênh Slack chung và cho thêm chat bằng id thủ công.
- **Q3 — QR code:** thêm dependency Python `segno` (MIT, thuần Python) hay tự viết bộ mã QR nhỏ / vendor JS `qrcode-generator` vào
  `static/vendor/`? Đề xuất: `segno` (server-side SVG, không đụng CSP), hoặc bỏ QR nếu không muốn thêm dependency (deep link vẫn đủ trên điện thoại).
- **Q4 — "Gửi thử tất cả":** mở rộng RPC `notify.test{channels}` (một chỗ, CLI dùng được) hay để web gọi lần lượt các RPC sẵn có? Đề xuất: mở rộng RPC.
- **Q5 — Preset sự kiện áp chung hai kênh** (một lựa chọn) hay riêng từng kênh ngay trên đường chính? Đề xuất: chung, riêng trong Nâng cao.

## 8. Đã làm (2026-11-28)

| PR | Nhánh | Nội dung |
|---|---|---|
| #240 (PR1 + PR6) | `feat/web-telegram-wizard` | Wizard `/settings/notifications/telegram` (BotFather → dán token → deep link + **QR `segno`** SVG server-side → "Đây là tôi — cho phép" không trang trung gian, Q2); dòng Telegram ở trang Thông báo đọc `telegram.status`. |
| #241 (PR4) | `feat/notify-event-groups` | `notify_events.py` (nhóm + nhãn + preset), khối "Nhận thông báo gì" (preset chung hai kênh, Q5), `config set notify.<kênh>.events` từ chối event lạ. |
| #242 (PR2/PR3/PR5 phần nền) | `feat/chat-setup-backend` | RPC `slack.verify_bot`, `telegram.repair`, `notify.test{channels}` (Q4, nay HUMAN_ONLY), `status.peers`/`frozen`/`diagnosis`, `chat/diagnose.py` (thuần), `slack.status.links`. |
| PR này (PR2/PR3/PR5 giao diện) | `feat/chat-setup-ui` (gộp 3 nhánh trên) | Thẻ kênh + `present.channel_state` (thuần, dựa trên `diagnosis`) dùng chung cho thẻ / trang Hoạt động / danh mục `/settings`; "Gửi thử tất cả" + "Gửi thử" từng kênh (`notify_test{channel}` → `notify.test{channels}`, không kênh nào gửi được → báo lỗi kèm lý do từng kênh); "Nâng cao" theo kênh trong thẻ (`cfg-notify-telegram`/`cfg-notify-slack`), cấu hình chung giữ `cfg-notify`; trang `/settings/notifications/slack` stepper 4 bước (`slack2_slash`, `slack2_verify`, `slack2_app`, `slack2_allow` không xác nhận cho DM đang chờ, `slack2_allow_confirm`), webhook một chiều vào "Nâng cao" (+ nhãn "đang nhường"); người được phép có tên ở cả hai trang (`present.chat_peers`, `settings/_chat.html`); chẩn đoán + `tg_repair` trên trang Telegram và thẻ kênh; thẻ "Nhận yêu cầu duyệt trên điện thoại" ở `/activity` (ẩn bằng `data-dismiss` + localStorage trong `app.js`, không JS vẫn hiện); neo cũ `#slack`/`#slack2` (early.js chuyển sang trang Slack, không JS vẫn có neo trên thẻ Slack). |

Lệch so với plan:
- `slack.status` thêm hai cờ `webhook` / `webhook_yields` (chỉ bool, không URL) để thẻ Slack và trang Hoạt động biết webhook một chiều đang có
  mà không phải đọc `secret.list` (đọc nhạy cảm, có audit). Slack chỉ có webhook → thẻ "chỉ thông báo" (= `ok`, không hiện gợi ý ở trang Hoạt động).
- `slack2_allow` (DM đang chờ) lấy `user_id` từ `slack.pending` của daemon, không tin form. `slack2_setup` (dán hai token một lần) vẫn nhận
  (có trang xác nhận GHI ĐÈ) nhưng không còn form nào trên trang.
- Đã có token mà dán lại (bot token bước 2 / app token bước 3) → phải tích "Ghi đè"; web hỏi lại `slack.status` ngay trước khi gửi (như Telegram).
- "Trang chủ" của plan = trang Hoạt động (`/activity`, `overview.html`) — `/` hiện chuyển sang Điều phối.
- Mô tả động ở `/settings` chỉ áp cho trang danh mục (thêm 2 RPC trạng thái trong RAM); cột nav con giữ mô tả tĩnh.

Còn lại:
- Nghiệm thu thật (§6) CHƯA chạy: bấm giờ Telegram ≤ 2 phút / Slack ≤ 5 phút trên Telegram + Slack thật, điện thoại (web public) và máy tính;
  link `api.slack.com/apps/<id>/general` + `app_redirect` mới chỉ kiểm trên Slack giả.
- Ảnh (hub demo, chưa kênh nào kết nối — trạng thái "đang chạy / lỗi" chưa có ảnh): `docs/screenshots/web/34-thong-bao-kenh.png` (+ `-mobile`, 390 px
  không tràn ngang), `35-slack-tung-buoc.png`, `36-telegram-ket-noi.png`, `37-hoat-dong-goi-y-dien-thoai.png` — `web.demo` + Playwright Chromium headless.
