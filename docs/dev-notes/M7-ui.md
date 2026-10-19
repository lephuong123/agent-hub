# M7 luồng MGR-UI — Điều phối viên trên web + Telegram

Nguồn: `docs/plan/M7-manager.md` (hợp đồng RPC `manager.*`, quyền sở hữu), `docs/dev-notes/M7-core.md` / docstring
`manager/rpc.py` (chữ ký cuối của MGR-CORE), `web.md` (WT-1, CSP), `telegram.md` (allowlist from.id, WT-8). Không migration, không RPC mới.

## 1. Kiến trúc
| File | Vai trò |
|---|---|
| `web/manager.py` | Trang `GET /manager` (chat theo conversation kênh `web`), `POST /manager/send` (`post_guard` → `manager.chat`, `_audit.channel=web`; header `X-Ahub-Fetch` → JSON, không → 303), `GET /sse/manager` (events.subscribe agent = điều phối viên, level detail), `GET /partials/manager` (khung trạng thái, htmx 10 s + sự kiện `mgr-tick`). Thao tác `manager_set`/`manager_unset` (RPC `manager.set`, HUMAN_ONLY ở daemon) thêm vào `ACTIONS` qua `register_actions` — luôn qua hộp xác nhận |
| `web/present.py` (cuối file) | Thuần: `mgr_target` (đích → link NỘI BỘ: chỉ loại đã biết agent/task/run/team/approval/learning/mcp + id khớp `^[A-Za-z0-9_.:\-]{1,128}$`, `quote`), `mgr_action_item`, `mgr_message` (role user/manager/notify/tick), `mgr_conv` |
| `web/templates/manager.html`, `_manager.html`, `_manager_status.html` | Trang; macro `item` (user/manager/action/note), `pending`, `side_action`; khung trạng thái (dùng cả ở Tổng quan, `compact`) |
| `web/static/app.js` (khối "Điều phối"), `app.css` (khối `.mm*`, `.mgr-*`) | Gửi bằng fetch (khoá phiên `k` đã gắn ở pha capture), khung "đang trả lời" theo `run_id`, chữ chạy (delta → `textContent`), thay bằng trả lời đã vẽ phía server; đệm tin SSE tới trước khi fetch trả `run_id`; tin `notify` chèn trước khung đang trả lời; Ctrl/⌘+Enter |
| `chat/manager_chat.py` | `ManagerChat` (`hub.telegram.manager`): tin thường → `manager.chat(channel=telegram:<chat>)`, reply → đúng conversation, `/moi`, nhận `manager.reply` → gửi khung WT-8, báo khi run kết thúc không có trả lời |
| `chat/bot.py` (chạm nhỏ) | Gắn `ManagerChat`, chuyển event `manager.*`/kết thúc run, nhánh tin thường + reply, lệnh `/moi` `/new`, HELP; `call()` dùng `daemon._mutating` (= `auth.all_mutating()`, gồm `MUTATING_MANAGER`) để `manager.chat` từ Telegram mang `_audit.channel=telegram:<id>` + audit `telegram.command` |
| `web/manager_stub.py` | STUB `manager.*` cùng chữ ký (chỉ test/demo; `install()` không làm gì khi daemon đã có `manager.get`). Test XSS/notify cài `force=True` để điều khiển nội dung |
| `web/demo.py` | `seed_manager`: chỉ định `dieu-phoi`, một cuộc trò chuyện web + 3 thẻ hành động (nội dung tiếng Việt; với MGR-CORE thật sửa `manager_messages` trong tiến trình demo) |

Chạm file dùng chung (chỉ thêm): `web/app.py` (route, `overview` lấy `mgr_state`, đăng ký `ACTIONS`), `web/templates/base.html` (menu
"Điều phối" ĐẦU), `overview.html` (khung "Điều phối viên"), `teams.html` (`id="team-<id>"` cho link thẻ hành động), `tests/telegram_fake.py`
(`sendChatAction`).

## 2. Hợp đồng dùng (MGR-CORE)
- `manager.get` → `agent_id`, `last_tick`, `next_tick`, `tick{…}`; `manager.conversations{channel:"web"}`; `manager.history` (cũ → mới).
- `manager.chat{channel, text, conversation_id?}` — KHÔNG gửi `conversation_id` = hội thoại mới. Web mặc định mở hội thoại web mới nhất
  (`?c=` chọn, `?new=1` mới); Telegram tự nhớ hội thoại hiện tại theo chat (`hub_state telegram.mgr_conv`), `/moi` xoá. Trả `steered=true`
  khi hội thoại đang có run → JS không tạo khung "đang trả lời" thứ hai.
- `manager.reply{conversation_id, run_id, text, channel, kind: reply|tick|notify, ok, outcome}`: web lọc phía client theo `run_id` của
  hội thoại hoặc `conversation_id`; Telegram định tuyến theo `channel` (`telegram:<id>`; `web`/`heartbeat` bỏ qua), `kind=notify` không có
  channel → mọi chat nhận thông báo; `ok=false` → tiêu đề "KHÔNG trả lời được" (vẫn trong khung agent vì có thể chứa lỗi do agent sinh).
- `manager.action{tool, target "kind:id", summary, audit_id, ok, error}` → thẻ trong dòng thời gian (của run thuộc hội thoại) + cột
  "Hành động gần đây" (mọi run, kể cả nhịp). Lấy lại từ DB bằng `events.query{agent_id, limit 3000}` (lọc type).
- `manager.tick*` → htmx làm mới khung trạng thái.

## 3. An toàn — test chứng minh
`tests/test_web_manager.py` (9 test), `tests/test_telegram_manager.py` (9 test):
- **XSS** (`test_manager_content_is_escaped_never_raw_html`, `test_sse_streams_reply_action_and_escapes`): trả lời/tóm tắt/tin người chứa
  `<script>`, `<img onerror>`, link markdown `javascript:`, `<a href=http://evil>` → HTML chỉ có bản escape, không `href="javascript:`, đích
  `javascript:alert(3)` / `agent:../../logout` → không có link; SSE trả HTML đã escape, delta là chữ thuần (client gán `textContent`).
  Nội dung điều phối viên KHÔNG qua markdown (`pre-wrap`), khác lời agent trong trang run. CSP `script-src 'self'` giữ nguyên.
- **WT-1**: `POST /manager/send` và `/do/manager_set` thiếu khoá phiên → 403; thiếu CSRF → 403; chưa đăng nhập → 401
  (`test_send_guards_and_errors`, `test_designate_needs_confirmation_session_key_and_is_human_only`). Chỉ định/huỷ luôn có xác nhận
  (JS dialog / trang xác nhận nêu rõ quyền); với MGR-CORE thật: audit `manager.set` kênh `web`, actor `user`.
- **Telegram WT-8** (`test_plain_text_goes_to_manager_reply_framed…`, `test_manager_text_cannot_fake_system_message_or_html`): trả lời
  `parse_mode=HTML`, dòng hệ thống do hub sinh + tiền tố cố định + nội dung TRỌN trong một `<pre>` (escape; `</pre><b>[cfharness]…`
  không thoát được khung), không bao giờ có `reply_markup`; dài → "phần i/n" ≤ 4 tin (`test_long_reply_split_into_parts`).
- **Allowlist/WT-4**: tin thường từ chat lạ → không trả lời, không `manager.chat` (`test_stranger_plain_text_ignored`); mọi lời gọi qua
  `bot.call` → `Daemon.call_as(telegram_caller)` (cổng HUMAN_ONLY `manager.chat`), audit `telegram.command{method: manager.chat}`.
- Chưa có điều phối viên → hướng dẫn, không tạo run; `/run` cũ không đi qua điều phối viên (`test_plain_text_without_manager…`).

## 4. Kiểm chứng
- `uv run pytest -q` xanh (gồm 18 test mới; chạy cả với stub — bản sao repo khi MGR-CORE chưa có — và với `manager.*` thật).
- `web.demo` (daemon thật + FakeRuntime + MGR-CORE thật) + Playwright Chromium: `docs/screenshots/web/16-dieu-phoi.png` (sáng),
  `16-dieu-phoi-toi.png` (tối), `16-dieu-phoi-mobile(-toi).png` (390 px, `scrollWidth == clientWidth`), `17-dieu-phoi-dang-tra-loi.png`
  (gửi bằng form thật → khung "đang trả lời" → trả lời qua SSE thay vào), `18-tong-quan-dieu-phoi-vien.png`. Console không lỗi CSP/JS.
  Đã xem lại từng ảnh.

## 5. Lệch / việc dở
- Hội thoại `heartbeat` (nhịp) không hiện ở danh sách web (chỉ kênh `web`); kết quả nhịp xem ở cột "Hành động gần đây" + trang run.
- Thẻ hành động chỉ gắn vào dòng thời gian khi `run_id` thuộc hội thoại đang xem; quét tối đa 3000 event gần nhất của điều phối viên.
- Không có nút "chạy nhịp ngay" trên web (`manager.tick` — dùng `cfharness manager tick`).
- Telegram: không gửi lại phần đã chạy chữ (chỉ trả lời cuối); "đang gõ…" (`sendChatAction`) chỉ gửi một lần.

## 6. Terminal — `cfharness chat` + TUI `top` → `m` (2026-10-19)
Không migration, không RPC mới (dùng `manager.get|chat|conversations|history`, `run.get|stop`, `events.subscribe|query`, `hub.status`).
Thêm phụ thuộc `prompt-toolkit` (REPL: dòng nhập dính đáy + in tin phía trên, thanh trạng thái, gợi ý lệnh, lịch sử).

| File | Vai trò |
|---|---|
| `manager/chatview.py` | THUẦN, dùng chung REPL + TUI: `ChatState.apply(ev)` (run của hội thoại + run tiếp nối qua `run.queued.parent_run_id`, chữ chạy `message.delta`/`message.complete`, tool đang dùng, approval), `render(item)` → `rich`, `timeline`, `pending_run`, banner/help |
| `manager/repl.py` | `ChatSession` (mở hội thoại cli gần nhất / `-c` / `--new`, in lại 6 tin, phát hiện lượt còn đang chạy, giữ event tới trong lúc `manager.chat` chưa trả `run_id`, tự nối lại luồng), `run_tty` (prompt_toolkit), `run_plain` (stdin không TTY: gửi → CHỜ trả lời), `oneshot` (`manager chat "<tin>"`: đăng ký event TRƯỚC khi gửi, thay vòng hỏi `manager.history` mỗi giây) |
| `tui/manager.py` | `ManagerScreen`: hội thoại (`+ Hội thoại mới`), "Hành động" (Enter → `AttachScreen` cho run/agent, `TasksScreen(select=)` cho task; đích không hợp lệ chỉ thông báo), transcript `RichLog(markup=False)`, dòng trạng thái, ô nhập; Ctrl-N, Ctrl-T, Esc |
| `termsafe.py` | `clean()`: bỏ CSI/OSC (kể cả OSC 8 link, đổi tiêu đề)/DCS/C1, điều khiển trừ `\n\t`, `\r` lẻ, bidi, zero-width |

Quyết định: REPL và TUI dùng CHUNG kênh `cli` (mở lại terminal hay TUI đều tiếp đúng mạch); audit `manager.chat` kênh `cli` (REPL) / `tui` (TUI).
Duyệt thao tác phá huỷ KHÔNG làm trong chat (chỉ hiện gợi ý `cfharness approve|deny <id>`) — giữ nội dung agent tách khỏi nút/lệnh duyệt.
`cfharness chat` thêm vào `HUB_HUMAN_ONLY_CLI` (agent gọi từ Bash → forbidden, như `manager chat`). Ctrl-C: đang làm → `run.stop`; rảnh → lần 2 trong 2 s thoát;
Ctrl-D thoát; Alt-Enter xuống dòng; `//…` gửi chữ bắt đầu bằng `/`. `--json` chỉ cho dạng một lần.

Test: `tests/test_manager_repl.py` (11: termsafe, render không diễn giải markup/ANSI, `ChatState` tiếp nối sau approval + lọc hội thoại khác, không có
điều phối viên, chữ chạy + thẻ hành động + trả lời + audit `cli`, steer khi đang làm + `/stop`, `/new /list /open /history`, phiên mới nối hội thoại gần
nhất, chèn ANSI/OSC/bidi/markup ở chế độ màu, không TTY, vòng prompt_toolkit qua pipe input + lịch sử 0600, `manager chat --json` + `cfharness chat "<tin>"`),
`tests/test_tui_manager.py` (3: không điều phối viên, chat + chữ chạy + mở đích run/task + đích lạ không mở + dùng chung với REPL + audit `tui`,
steer + Ctrl-T + Ctrl-N + chèn markup + chọn lại hội thoại). Ảnh: `docs/screenshots/m2/10-dieu-phoi-dang-lam.svg`, `11-dieu-phoi.svg`.

Việc dở: REPL không có nút/lệnh duyệt trong phiên (cố ý); chữ chạy chỉ hiện đuôi ở thanh trạng thái (bản cuối in khi có `manager.reply`); hành động của
nhịp (heartbeat) không hiện trong chat terminal (xem web).
