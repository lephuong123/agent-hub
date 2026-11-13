# Luồng TG — bot Telegram (thông báo, duyệt bằng nút, lệnh chat)

Nguồn: `docs/plan/web-telegram.md` (luồng TG), BRD FR-SG-04/09/11, FR-LN-13; `docs/dev-notes/M1-B.md` (Slack), `hardening-SEC.md`.

## Kiến trúc
| File | Vai trò |
|---|---|
| `notify/base.py` | `QueuedNotifier`: khung chung listener bus → lọc `notify.<kênh>.events` → hàng đợi → gửi có backoff → `notify.sent`/`notify.failed{channel}`. Slack refactor nhẹ thành lớp con (API cũ giữ nguyên, test Slack xanh) |
| `notify/telegram.py` | `TelegramApi` (Bot API qua `httpx2.AsyncClient`, gỡ token khỏi mọi thông báo lỗi), định dạng văn bản thuần (không parse_mode), `CallbackStore` (nonce + HMAC), `TelegramNotifier` |
| `chat/bot.py` | `TelegramBot` (`hub.telegram`): vòng long polling, ghép nối, lệnh chat, nút inline, theo dõi run → gửi kết quả, reply → continue |
| `chat/rpc.py` | RPC `telegram.status|setup|pending|allow|revoke|disable|test` (đăng ký SAU bước bọc audit → tự audit) |
| `chat/cli.py` | `cfharness notify telegram setup|status|test|disable|allow|revoke` (gắn vào parser `notify` của `guard/cli.py`) |

File dùng chung đã chạm (chỉ thêm): `hub.py` (attach), `daemon.py` (`call_as` + tham số `as_caller` của `_dispatch`, đăng ký RPC),
`auth.py` (`telegram_caller`, `telegram.*` ∈ HUMAN_ONLY, `MUTATING_TELEGRAM` trong `all_mutating`), `config.py` (`notify.telegram`).
Ngoài danh sách dùng chung: `guard/cli.py` (gắn subparser), `guard/classifier.py` (`HUB_HUMAN_ONLY_CLI["notify"] = {"telegram"}`),
`learning_ops/digest.py` (`on_notify` chỉ tính kênh Slack khi cập nhật `slack_status`). Không có migration (không dùng v15):
trạng thái nhỏ nằm ở `hub_state` (`telegram.offset|callbacks|approval_msgs|watch|msgmap`).

## Thư viện HTTP
`httpx2` (fork httpx của pydantic) — đã có trong môi trường vì `mcp` 2.x phụ thuộc nó, nên KHÔNG thêm phụ thuộc/không đổi `uv.lock`.
Chọn async client thay vì `urllib` trong thread (như Slack) vì long poll 25 s phải **huỷ được** khi daemon dừng: một luồng
`to_thread` treo sẽ chặn `asyncio.run` đóng executor tới 25 s. Fallback `import httpx` nếu sau này `mcp` bỏ `httpx2`.
Logger `httpx*`/`httpcore*` hạ xuống WARNING (log INFO của thư viện có URL chứa token); cộng thêm che theo giá trị của SEC.

## Luồng
- **Tắt êm**: chưa có secret `TELEGRAM_BOT_TOKEN` (CHỈ đọc secret store, không lấy biến môi trường) hoặc `enabled: false` → vòng
  ngủ `idle_check_s`, không gọi mạng, không event. `setup`/`disable` gọi `kick()` (huỷ long poll đang treo, chạy lại ngay).
- **setup**: CLI hỏi token (getpass / `--token-stdin`) → RPC `telegram.setup` (HUMAN_ONLY) kiểm `getMe`, lưu secret (0600, đăng ký che),
  sinh `TELEGRAM_CALLBACK_KEY`, sinh mã ghép nối 8 hex (TTL 10 phút). Người gửi `/start <mã>` (deep link `t.me/<bot>?start=<mã>`)
  → bot chỉ ghi nhận "chờ xác nhận" (chat riêng; nhóm cần `allow_groups`) → CLI poll `telegram.pending`, hiện tên/username/chat_id, hỏi
  y/N → `telegram.allow`. Mã dùng một lần. `--chat-id` thêm thẳng, `--keep-token` ghép thêm chat.
- **Allowlist** `notify.telegram.allowed_chat_ids` nằm trong nhánh `notify` của config.yaml → được `guard/integrity` kiểm (ghi ngoài RPC bị khôi
  phục); sửa chỉ qua `telegram.allow|revoke` (HUMAN_ONLY). Classifier: `cfharness notify telegram …` từ agent → forbidden.
- **Cổng người**: mọi lệnh chat → `Daemon.call_as(auth.telegram_caller(hub, chat_id), method, params)` → `_dispatch` như socket: HUMAN_ONLY theo
  `caller.human` (= bot bật + chat trong allowlist), `CURRENT_CALLER` (cổng tầng service), bọc audit với `_audit={channel:"telegram:<id>",
  actor:"user"}`; approve/deny truyền `channel=telegram:<id>`. Chat ngoài allowlist không bao giờ tới dispatch; nếu tới, HUMAN_ONLY từ chối.
- **Chat lạ**: không trả lời, audit `telegram.ignored` (1 dòng/phút/chat), kể cả nút bấm.
- **Nút inline**: `callback_data = v1.<nonce 96 bit>.<HMAC-SHA256[:16]>` (≤ 64 byte). Bản ghi phía hub: hành động, approval, chat_id, nhóm,
  hạn (`callback_ttl_s` 7 ngày). Kiểm: tồn tại → đúng chat → đúng chữ ký → còn hạn; hợp lệ thì TIÊU cả nhóm (Cho phép/Từ chối cùng tin) trước khi
  gọi RPC. Sai chat/chữ ký không tiêu (kẻ lạ không làm hỏng được nút thật). Mọi lần bị từ chối → audit `telegram.callback_rejected{reason}`.
  Lớp dưới vẫn còn: `approvals.resolve` báo `Conflict` nếu đã quyết; vé một lần theo `action_fingerprint`.
- **Chống chạy lại**: offset `getUpdates` lưu `hub_state` TRƯỚC khi xử lý → update không xử lý hai lần (kể cả restart giữa chừng).
- **Duyệt 2 bước** (đã đổi: xem mục kiểm toán — nay MỌI cho phép 2 bước): `is_heavy` = có `protected_resource_id` hoặc `destructive_class ∈ notify.telegram.confirm_classes`
  (mặc định db, git, system, hub, policy, mcp). "Cho phép" → tin xác nhận (nút mới, TTL ≤ 1 h) → "Xác nhận CHO PHÉP" mới gọi approve;
  "Huỷ" → gửi lại tin duyệt với nút mới. `/estop off` cũng cần xác nhận.
- Approval quyết ở kênh khác (CLI/TUI/web) → `approval.resolved` → vô hiệu nonce + gỡ nút trên các tin đã gửi.
- **/run**: `run.start` → ghi `telegram.watch[run]=chat`; run con (`run.queued.parent_run_id`, resume sau duyệt/gián đoạn) kế thừa. Event kết thúc
  `source=hub` → gửi kết quả (outcome, chi phí, lượt, thời gian, `summary`/`error` cắt `result_max_chars` + "xem đầy đủ: cfharness runs show …,
  TUI hoặc web"); `waiting_human` chỉ báo ngắn, chờ run con. Tin kết quả → `telegram.msgmap`; reply (không phải lệnh) → `run.continue`.
- **/digest**: dựng tại chỗ 24 h qua bằng `DigestService.collect/render` (CHỈ đọc — không dời mốc digest 08:00). Digest 08:00 đi qua
  thông báo (`learning.digest`), như Slack.
- **Nội dung**: văn bản thuần; mọi chuỗi qua `redact_str` (mẫu + giá trị secret); approval chỉ có preview đã che; tool MCP chỉ tên; tin ≤ 3900 ký tự.

## Test (`tests/test_telegram.py`, `tests/test_telegram_e2e.py`, server giả `tests/telegram_fake.py`)
- Tắt êm không token; setup/ghép nối/allowlist; mã sai + chat lạ bị bỏ qua + audit; token không có trong kết quả/audit.
- Cổng HUMAN_ONLY: `telegram_caller` chat lạ → BLOCKED cho `estop.set`; CLI telegram từ agent → forbidden.
- Lệnh `/help /agents /runs /approvals /estop on|off(2 bước) /digest`, audit `estop.set` kênh `telegram:111`.
- `/run` → kết quả + chi phí; reply → `run.continue` (run con `parent_run_id`) → kết quả lần 2.
- Nút: dữ liệu bịa / chữ ký sai / sai chat / định dạng cũ / chat lạ → từ chối; bấm thật → allowed kênh telegram; bấm lại + nút cùng nhóm → từ chối;
  đúng 1 `approval.resolved`; nút trên chat khác bị gỡ. Duyệt nặng 2 bước + Huỷ. `/approvals` + Từ chối. Gửi lỗi → `notify.failed` → thử lại.
- Secret: secret trong preview bị che; token không có trong tin gửi, `telegram.status`, mọi file HUB_HOME ngoài `secrets/` (gồm hub.db/WAL/log);
  lỗi mạng không lộ token. Agent: token không trong env/spec của run, sandbox denyRead `secrets/`, `Read(/…/secrets/**)` deny, `cat` file token → forbidden.
- e2e (daemon thật + hook guard thật): `rm -rf dummy` bị giữ → tin có nút → bấm → lệnh chạy ĐÚNG 1 lần; bấm lại không chạy nữa; audit kênh
  telegram. `/run` từ chat lạ không tạo run; log daemon không chứa token.

## Lệch / việc dở
- Chỉ văn bản thuần (không Markdown/HTML) — đơn giản, không lỗi escape.
- Bot nhận lệnh từ MỌI người trong chat nhóm được allowlist → mặc định chỉ chat riêng (`allow_groups: false`).
- `/stop` không xác nhận (dừng run không phá dữ liệu). `/run` không có cờ cwd/budget (dùng mặc định agent).
- Chưa kiểm với Telegram thật (chỉ server giả). Nên thử tay: setup → /start → duyệt một `rm -rf` thật trên repo tạm.
- Web (luồng WEB) nên hiển thị `telegram.status` ở trang Cài đặt; RPC sẵn sàng.

## Sửa theo kiểm toán bảo mật (`docs/acceptance/security-web-telegram.md`)
Script tái hiện của kiểm toán (`scratchpad/audit-web/test_audit_tg.py`) nay ĐỎ ở mọi khẳng định lỗ hổng; ca tương ứng đã thành test xanh trong
`tests/test_telegram.py` (`test_wt2_*`, `test_wt4_*`, `test_wt5_*`, `test_wt8_*`).

| Mã | Đã sửa |
|---|---|
| WT-2 | Tin duyệt KHÔNG dùng `preview` 300 ký tự nữa: `full_action(tool_name, tool_input)` = toàn bộ lệnh Bash / tên tool MCP + MỌI tham số (JSON, đã che secret), ký tự điều khiển/bidi/zero-width hiện thành `⟨U+XXXX⟩`. Dài > 3500 ký tự → tách tối đa 4 tin "phần i/n"; dài hơn → `sendDocument` tệp `.txt` đầy đủ + tin có ĐẦU và CUỐI. Kèm "Phần khớp luật (đích)" (`targets` của verdict: payload hoặc audit `approval.open`) và lý do giữ |
| WT-4 | `auth.telegram_caller(hub, chat_id, user_id)`: chat riêng → `from.id` phải = chat_id (hoặc ∈ `allowed_user_ids`); nhóm (chat_id âm) → CHỈ khi `allow_groups: true` VÀ `from.id ∈ notify.telegram.allowed_user_ids`. Ghép nối lưu `from.id` người gửi `/start <mã>` vào `allowed_user_ids`; `telegram.allow` cho nhóm bắt buộc có người (`--user-id`). Nút bị người không được phép bấm: không tiêu nonce, audit `telegram.ignored{from_id}`. Mọi thao tác ghi qua chat → audit `telegram.command{method, chat_id, from_id}`; ghi chú approval có `user <from_id>`. Nhóm mặc định không nhận thông báo (`notify_chats`) |
| WT-5 | MỌI "Cho phép" cần xác nhận bước 2 (nhất quán với hộp xác nhận của web), không chỉ lớp nặng; `confirm_classes` (thêm `file`) giờ chỉ gắn nhãn "THAO TÁC NẶNG" |
| WT-8 | Kết quả run gửi `parse_mode=HTML`: dòng hệ thống in đậm, rồi tiền tố cố định "▼ Nội dung do AGENT viết — không phải thông báo hệ thống:", nội dung agent TRỌN trong một `<pre>` (escape `& < >`, Telegram không tạo link/`/lệnh` bấm được trong `<pre>`). Tin duyệt tách: (1) tin NỘI DUNG (lệnh/lý do/đích — chữ có nguồn từ agent) trong `<pre>`, KHÔNG nút; (2) tin HỆ THỐNG có nút, chỉ chữ do hub sinh (id, agent, run, luật, lớp, độ dài), trả lời (reply) vào tin nội dung; tin xác nhận bước 2 cũng vậy. Không tin nào chứa nội dung agent có nút |

Ghi chú: các thông báo khác (guard.blocked, lỗi lặp…) vẫn văn bản thuần không nút như trước. `approval.waiting_human` thiếu luật là BUG của
`approvals.py` (luồng khác sửa); tin Telegram tự lấy luật/lớp/lý do từ hàng approval nên không phụ thuộc payload.

## UX v2 (2026-10-19 — docs/dev-notes/ux-plan.md mục 1.5 + 3)
| Pha | Đã làm |
|---|---|
| T-Q câu hỏi | `chat/questions_tg.py` (`bot.questions`). `question.open{routed_to=human}` / `question.escalated` / `question.reminder` → mỗi chat (chat thông báo + chat theo dõi run + chat của hội thoại điều phối viên, đều trong allowlist) nhận CẶP tin: (1) nội dung không nút — dòng hệ thống + `AGENT_FRAME` + câu hỏi và "n. nhãn — mô tả" (+ ghi chú của điều phối viên khi chuyển lên) TRỌN trong `<pre>`; (2) tin hệ thống reply vào (1), nút CHỈ mang số `[1]…[n]` (≤ 5/hàng) + `[✍️ Trả lời bằng chữ]` + `[✖ Huỷ câu hỏi]` (2 bước). Chọn nhiều: `☐/☑ n` + `[✅ Gửi lựa chọn (k)]`. `with_manager` KHÔNG gửi. Trả lời bằng chữ: tin `force_reply`; reply vào tin đó hoặc tin (1)/(2) → `question.answer{text}` (map `telegram.q_msgmap`, kiểm TRƯỚC `run.continue`/điều phối viên/luồng giao việc). `question.answered|cancelled` (mọi kênh) → sửa tin (2) ở mọi chat thành dòng kết quả (số đã chọn, ai, kênh — KHÔNG nhãn agent, không chữ tự do) + gỡ nút. Lệnh `/hoi` (`/questions`), `/canban` (duyệt + câu hỏi); gửi lại → tin cũ cùng câu hỏi trong chat bị gỡ nút |
| Nonce câu hỏi | Mọi nút qua `CallbackStore` (`issue_rows` mới: nhiều hàng, một nhóm). Bấm nút KHÔNG kết thúc (bật/tắt, mở ô chữ, bước 1 huỷ, quay lại) → tiêu CẢ nhóm rồi cấp bàn phím MỚI (sửa tại chỗ). Lý do: nút cũ không bao giờ dùng lại được (chống phát lại), trạng thái chọn nằm ở hub (`telegram.q_sel`) chứ không trong dữ liệu nút (không giả được lựa chọn). WT-4 kiểm người bấm TRƯỚC khi tiêu (người lạ không làm hỏng nút) |
| T-B duyệt | Còn 2 tin (nội dung + hệ thống). "Cho phép" → SỬA tin hệ thống thành bước xác nhận (`Xác nhận CHO PHÉP`/`Huỷ`, vẫn 2 bước WT-5, chữ hub); "Huỷ" → sửa lại thành tin duyệt với nút MỚI. Kết quả (`approval.resolved`, mọi kênh) → `editMessageText` mọi tin hệ thống của approval ở mọi chat thành "✅/⛔ Yêu cầu … — đã CHO PHÉP/TỪ CHỐI · bởi … qua …" + gỡ nút; người bấm chỉ nhận toast, KHÔNG có tin mới. ESTOP off cũng sửa tại chỗ. `_clear_buttons` nay sửa chữ (lỗi → fallback gỡ nút) |
| T-A | `setMyCommands` (tiếng Việt: canban, hoi, giaoviec, trangthai, agent, luotchay, approvals, stop, moi, digest, estop, help) mỗi lần token đổi; alias cũ vẫn chạy. `/help` ở chat riêng kèm bàn phím nhanh (`📋 Cần bạn` `📊 Trạng thái` `➕ Giao việc` `🤖 Agent` → lệnh tương ứng, không chuyển cho điều phối viên). Bảng nhãn trạng thái tiếng Việt + emoji trong `chat/bot.py` (`STATUS_VI`, `AGENT_STATUS_VI`, `DECISION_VI`) — riêng của chat, web không phụ thuộc |
| T-C | `chat/flows.py` (`bot.flows`): `/giaoviec` (`/run`) không tham số → danh sách đánh số + nút số → ô `force_reply` → `run.start`; chỉ `<agent>` → ô nhập; đủ tham số → như cũ. `/agent` (`/agents`) nút `➕ n` / `⏹ n`; `/luotchay` (`/runs`) danh sách chữ hub + nút số → khung kết quả (reply = tiếp tục); `/stop` không tham số → chọn run đang chạy |
| T-D | Run do chat giao chạy > `notify.telegram.progress_after_s` (30 s, mặc định trong code) → MỘT tin tiến độ sửa tại chỗ ≤ 1/`progress_edit_s` (10 s) theo `tool.start`; kết thúc → "đã kết thúc — kết quả ở tin bên dưới". Điều phối viên: "đang gõ…" lặp 4 s tới khi trả lời (≤ 180 s). Tiêu đề kết quả có ✅/❌/⏹ |

Lệch/ghi chú: event `question.*` KHÔNG đi qua `notify.telegram.events`/`QueuedNotifier` (gửi trực tiếp từ bot, không backoff — lỗi chỉ log; người dùng `/hoi` để gửi lại). `progress_after_s`/`progress_edit_s` chưa có trong `config.DEFAULTS` (đọc với giá trị mặc định trong code). Nút giao việc/dừng dùng SỐ; tin danh sách có id agent/run (định danh do hub kiểm charset, như tin duyệt cũ). `question.cancel` không gỡ chặn task (hành vi của `questions/` — task vẫn blocked tới khi người xử lý).
Test: `tests/test_telegram_questions.py` (10 ca: cặp tin + nhãn độc chỉ trong `<pre>`, nút chỉ số, người lạ/sai chat/phát lại bị từ chối, sửa tin ở mọi chat, chọn nhiều + nonce cấp lại, `force_reply` + reply vào tin nội dung, trả lời qua web → tin Telegram được sửa, huỷ 2 bước, `with_manager` không gửi tới khi chuyển lên, `/hoi` `/canban`, menu lệnh + bàn phím nhanh, `/giaoviec` `/luotchay` `/agent` `/stop`). Test cũ đổi kỳ vọng "bước 2 = tin mới" → "tin được sửa" (`tg.with_buttons`, `tg.edits`).

## Tin gọn (2026-10-27)
Theo phản hồi "tin khó hiểu, nhiều ký tự đặc biệt": mỗi tin chỉ còn AI — CẦN GÌ — BẤM GÌ.
- Bỏ tiền tố `[cfharness]`, id run/yêu cầu/câu hỏi, tên luật/lớp, lệnh CLI dự phòng (`cfharness approve|deny|answer|runs show`) khỏi tin Telegram/Slack 2 chiều.
  Lệnh CLI chỉ còn ở kênh MỘT chiều (webhook Slack, `notify/slack.format_message`) vì đó là cách duy nhất để xử lý.
- `AGENT_FRAME` rút còn "▼ Agent viết:" (ux-plan §3.1). WT-8 giữ nguyên: chữ agent vẫn TRỌN trong `<pre>` sau tiền tố cố định, không chung tin với nút. (Từ 2026-11-13: markdown thay `<pre>` — mục cuối.)
- Duyệt: tin nội dung `⏸ <agent> muốn chạy:` + `<pre>lệnh</pre>` + `<pre>Vì sao hỏi / Ảnh hưởng tới</pre>`; tin nút `❓ Cho phép <agent> chạy thao tác ở tin trên?`
  (+ "⚠️ Thao tác nặng" khi cần). Bước 2: "Chắc chắn cho phép?". Kết quả sửa tại chỗ: "✅ Đã cho phép — bởi bạn qua Telegram."
- Kết quả run: `✅ <agent> — xong · 12 giây · $0.004` + khung agent + "↩ Trả lời tin này để giao thêm cho agent này."
- Test không còn dò id trong chữ: `FakeTelegram.msg_for/thread`, `FakeSlack.msg_for/root_of` tra nút → id qua `CallbackStore` của hub (fixture gắn `tg.hubs`/`sk.hubs`).

## Gửi về đúng chat gốc (2026-11-01)
Trước đây tin duyệt, câu hỏi và thông báo sự kiện gửi tới MỌI chat trong `notify_chats()` (chat riêng + nhóm khi `allow_groups`).
Nay mỗi tin chủ động gắn với một run/hội thoại đi qua `ChatBot.route_chats(run_id, conversation_id)` (`chat/base.py`):
- `origin_chat` lần chat gốc: `<kênh>.watch` (run giao từ chat) → `mgr_runs` (run điều phối viên gửi từ chat) → `parent_run_id`
  (≤ 8 bậc, resume/continue) → hội thoại điều phối viên (`mgr_convchat`, hoặc kênh `telegram:<chat>` của hội thoại).
- Có chat gốc còn trong allowlist → CHỈ gửi về đó. Không có (run từ web/CLI/lịch/nền) → `broadcast_chats()`: Telegram = chỉ chat
  riêng (nhóm không bao giờ nhận tin không phải việc của nhóm), Slack = chỉ DM (như cũ).
- Áp ở `TelegramNotifier._send`, `SlackChatNotifier._send`, `QuestionChat.chats_for`. `notify test` vẫn gửi mọi chat.
- Lưu ý Slack: trước đây mọi DM trong allowlist đều nhận; nay việc giao từ một DM chỉ về DM đó.
Test: `tests/test_telegram.py::test_route_to_origin_chat_only`.


## Chữ agent dạng markdown — nới WT-8 có kiểm soát (2026-11-13, P0-10 — plan-chat-ui-claude.md Q2/Q3)
Quyết định chủ repo (Q3): bỏ khung `<pre>` cho chữ agent, chuyển markdown → HTML Telegram, có cờ quay lại. Tiền lệ: Slack đã nới
cho trả lời điều phối viên (slack.md — trích dẫn + link). Áp cho trả lời/tin chủ động của điều phối viên (`ManagerChat`), kết quả
`/run` (`ChatBot.format_result`) và câu hỏi của agent (`questions_tg.content_text`). Tin duyệt (`⏸ … muốn chạy:`) GIỮ `<pre>` — đó là
lệnh, phải hiện nguyên văn.
- Bộ chuyển: `chat/mdconv.py` (markdown-it-py `commonmark` + `table`, `strikethrough`, `html=False` → khối trung gian →
  `to_telegram_html`). Mọi chữ agent escape `& < >`; CHỈ bộ chuyển sinh thẻ: `b i s code pre` (+ `<code class="language-x">` với
  tên ngôn ngữ lọc `^[A-Za-z0-9_+.#-]{1,24}$`). Tiêu đề → dòng `<b>`; danh sách `• `/`1. ` thụt bằng NBSP; bảng hẹp (≤ 3 cột,
  ≤ ~34 ký tự/dòng) → `<pre>` căn cột, rộng hơn → mỗi hàng một dòng `• <b>cột1</b>: tên: giá trị · …`; `---` bỏ; trích dẫn → `▎ `
  (không dùng `<blockquote>` — Telegram cấm `pre`/`code`/`a` bên trong).
- Link (Q2): KHÔNG `<a>`. Link http(s) → chữ `nhãn (URL)`; nhãn trùng URL hoặc trông như URL/tên miền → chỉ URL thật (không giả
  đích); link khác (`javascript:`, `mailto:`, đường dẫn) → chỉ nhãn. Ảnh → `alt (URL)`, không nhúng.
- Lệnh: chữ `/lệnh` (không dính chữ đứng trước) nằm trong `<code>` → Telegram không biến thành lệnh bấm được.
- Bất biến còn giữ: dòng hub (tiêu đề in đậm + tiền tố `AGENT_FRAME`) LUÔN đứng trước chữ agent; chữ agent không nằm chung tin với
  nút; đuôi hub (`↩ …`, "đã cắt") cách một dòng trống, in nghiêng. Cái agent làm được: in đậm/nghiêng một dòng trông "giống" chữ hub —
  chấp nhận (luôn nằm dưới tiền tố, không kèm nút, không link/lệnh bấm được), như Slack.
- Chia tin: `split_md` chia markdown TRƯỚC khi render, theo khối (đoạn, mục danh sách, dòng mã), rồi xuống dòng, rồi dấu cách — đo
  độ dài SAU khi render (`telegram_len`, đơn vị UTF-16). Khối mã bị chia → tin sau mở lại cùng ngôn ngữ; bảng bị chia → lặp tiêu đề.
  Quá `MAX_CHUNKS` (4) tin → gửi thêm bản đầy đủ thành tệp `.md` (`send_document`). Điều phối viên trả lời dài: `manager/service.TEXT_MAX`
  nâng 4000 → 20000 (= trần DB); bị cắt → payload `truncated: true`, kênh ghi "hub đã cắt bớt".
- Lọc (P1-10, `manager_chat.sanitize_reply`): gỡ dòng `[cfharness…`, dòng chỉ có `NO_ACTION`, thẻ `<untrusted-data…>` sót lại.
- Cờ: `notify.telegram.agent_render: markdown|pre` (mặc định `markdown`; `pre` = khung cũ y nguyên: TRỌN `<pre>`, cắt cứng 3500).
- Test: `tests/test_mdconv.py` (whitelist thẻ + cân thẻ bằng `html.parser` — `tests/tg_html.py`), `tests/test_telegram.py`
  (WT-8 hai chế độ), `tests/test_telegram_manager.py` (chia khối, `.md`, `truncated`, lọc). Chưa kiểm trên Telegram thật.

## `/lệnh` không bấm được ở MỌI vị trí + `<blockquote expandable>` (2026-11-13, P2 — plan-chat-ui-claude.md)
- Luật Telegram nhận `/lệnh` (thực thể `bot_command`): "/" + `[A-Za-z0-9_]{1,64}` (+ `@bot`). tdlib (`match_bot_commands`): ký tự
  TRƯỚC không phải chữ/số/`_` và không phải `/ < >`. tdesktop (`RegExpBotCommand`): ký tự trước là đầu dòng hoặc dấu ngăn — gồm cả
  `< > /`. Không biết server/ứng dụng nào áp luật nào → `mdconv._TG_CMD` lấy HỢP: mọi `/` + chữ ASCII/số/`_` mà ký tự trước không
  phải chữ/số/`_` (Unicode) đều bị vô hiệu — sau `>`, `<`, ngoặc, `:`, `-`, `.`, `~`, `/`, emoji, dấu nháy, đầu dòng.
  Cách vô hiệu: bọc `<code>` cả cụm tới khoảng trắng (bấm vào = chép; đường dẫn `/usr/bin/x` một khối). Riêng `</thẻ>` (agent viết
  thẻ HTML thô) → chèn WORD JOINER U+2060 sau "/" (không hiện, chữ y nguyên; bọc `<code>` sẽ thành `<`/b`>` khó đọc). Bên trong URL
  (`https?://…`, `www.…`) không đụng: Telegram ưu tiên thực thể URL. Áp cả tiêu đề, ô/tên cột bảng, nhãn link (trước đây chỉ đoạn văn).
- `<blockquote expandable>` (Bot API 7.4) — cờ `notify.telegram.expandable_details` (mặc định **false**, CHƯA kiểm trên bot thật).
  Bật: `mdconv.to_telegram_html(md, expandable=True)` giữ nguyên "màn đầu" (`EXPAND_FIRST_SCREEN` = 500 ký tự thấy được), sau đó
  chuỗi khối liền nhau loại đoạn văn/danh sách/trích dẫn (`▎`) dài ≥ `EXPAND_MIN` (300) được gói trong MỘT `<blockquote expandable>`
  ở cấp ngoài cùng. Khối có `pre`/`code` (khối mã, `mã` nội dòng, `/lệnh` đã bọc) cắt chuỗi và hiện bình thường — Telegram cấm
  `pre`/`code`/`a` trong blockquote và cấm blockquote lồng nhau. Chữ thấy được y hệt bản không gập (chỉ thêm thẻ) → `telegram_len`/
  `split_md` không đổi. Mỗi phần sau `split_md` có màn đầu riêng.
- Test: `tests/test_mdconv.py` (14 vị trí `/lệnh`, tiêu đề/bảng/nhãn link, chữ trông giống lệnh không bị bọc; expandable tắt mặc
  định, bật thì hợp lệ + cùng chữ thấy được + bỏ qua khối có code + nối đúng cờ), `tests/tg_html.py` (thêm: `blockquote` chỉ ở cấp
  ngoài cùng, thuộc tính `expandable` không giá trị; `tappable_commands(html)` tìm `/lệnh` còn bấm được trên chữ thấy được, ngoài
  `pre`/`code`/URL). Chưa kiểm trên Telegram thật: hiển thị expandable, và việc U+2060 chặn được lệnh trên mọi ứng dụng.
