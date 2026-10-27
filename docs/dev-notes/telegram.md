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
- `AGENT_FRAME` rút còn "▼ Agent viết:" (ux-plan §3.1). WT-8 giữ nguyên: chữ agent vẫn TRỌN trong `<pre>` sau tiền tố cố định, không chung tin với nút.
- Duyệt: tin nội dung `⏸ <agent> muốn chạy:` + `<pre>lệnh</pre>` + `<pre>Vì sao hỏi / Ảnh hưởng tới</pre>`; tin nút `❓ Cho phép <agent> chạy thao tác ở tin trên?`
  (+ "⚠️ Thao tác nặng" khi cần). Bước 2: "Chắc chắn cho phép?". Kết quả sửa tại chỗ: "✅ Đã cho phép — bởi bạn qua Telegram."
- Kết quả run: `✅ <agent> — xong · 12 giây · $0.004` + khung agent + "↩ Trả lời tin này để giao thêm cho agent này."
- Test không còn dò id trong chữ: `FakeTelegram.msg_for/thread`, `FakeSlack.msg_for/root_of` tra nút → id qua `CallbackStore` của hub (fixture gắn `tg.hubs`/`sk.hubs`).
