# Kế hoạch làm lại UX — Web + Telegram + "Câu hỏi có lựa chọn"

Ngày: 2026-10-19. Trạng thái: **đã chốt (mục 6), đang thi công** — xem mục 7. Phạm vi: `web/`, `chat/`, `notify/telegram.py`, thêm service `questions`.
Giữ nguyên mọi bất biến an toàn của WEB/TG/M7-ui (CSP `'self'`, không CDN, không inline script/style, nội dung agent = DỮ LIỆU,
WT-1/WT-4/WT-5/WT-8, cổng HUMAN_ONLY, audit theo kênh).

---

## 0. Chẩn đoán (từ ảnh `docs/screenshots/web/*` + đọc code)

### Web
| # | Vấn đề | Chỗ thấy |
|---|---|---|
| W1 | Thanh menu phẳng **12 mục** ngang hàng, không nhóm; trên mobile phải cuộn ngang mới thấy "Cài đặt" | `base.html` |
| W2 | Một yêu cầu duyệt hiện **3 lần** trên trang run (khung đỏ trên + "Xin duyệt" + "Chờ bạn duyệt" trong diễn biến) | `03-run-cho-duyet.png`, `replay_items` |
| W3 | Tổng quan: bảng agent dạng spreadsheet, cột "Công cụ gần nhất" lặp `Bash ls -la .` mọi dòng; nút "Xem live"/"Xem run" lẫn kiểu | `01-tong-quan.png` |
| W4 | "Hoạt động gần đây" là firehose thô: id `t_792439e717`, `running→done`, câu tiếng Anh của agent lặp 3 lần — nhiễu, không cho biết **cần làm gì** | `_items.html`, `activity_item` |
| W5 | Thứ bậc thị giác yếu: mọi thứ là cùng một `.card` trắng; không phân biệt "việc của bạn" vs "thông tin" | `app.css` |
| W6 | Icon lẫn lộn (✓ ✖ 🗑️ ✎ ‖ ▶ ◐ ⚡ ✎) — emoji hiển thị khác nhau theo OS, chỗ có chỗ không | toàn bộ template |
| W7 | Nút không nhất quán: `btn` xanh đặc, viền đỏ nhạt, "Báo nhầm & cho phép" nút xám — không rõ nút chính | approvals, agent |
| W8 | Id kỹ thuật lộ khắp nơi (`r_41bb8ebdd819`, `[cfharness task t_…] You have been assigned…`) thay vì tiêu đề người đọc được | runs, agent |
| W9 | Trang Điều phối: hộp chat bị cắt đầu (thẻ hành động mất nửa), thẻ hành động chen giữa tin nhắn, danh sách cuộc trò chuyện ở cột phải dưới cùng, ô nhập không dính đáy | `16-dieu-phoi.png` |
| W10 | Không có trạng thái tải/gửi (bấm "Giao việc" không có phản hồi tức thì), flash cuối trang dễ bỏ qua, thời gian tuyệt đối `22:58:36` thay vì "2 phút trước" | `app.js`, flash |
| W11 | Form agent dài, gom hết một trang; "Tuỳ chọn" giấu trong `<details>` không nhãn rõ | `agent_form.html` |

### Telegram
| # | Vấn đề | Chỗ thấy |
|---|---|---|
| T1 | Mỗi yêu cầu duyệt = **≥ 4 tin** (nội dung → hệ thống có nút → xác nhận bước 2 → tin "Đã cho phép" mới) + toast. Chat bị trôi rất nhanh | `send_approval`, `_decide`, `_on_callback` |
| T2 | Phải gõ lệnh tay (`/run <agent> <yêu cầu>`, `/stop <run_id>`); không có menu lệnh `/` (chưa `setMyCommands`), không bàn phím nhanh | `_command` |
| T3 | Danh sách `/agents`, `/runs` là văn bản thô `• r_xxx · agent — completed · $0.0038 · 2026-10-19T22:5` — trạng thái tiếng Anh, id không bấm được | `_c_agents`, `_c_runs` |
| T4 | Sau khi bấm nút, bot gửi THÊM tin mới thay vì sửa tin cũ → không biết tin nào còn hiệu lực | `_on_callback` |
| T5 | Kết quả run: tiêu đề dài, tiền tố cảnh báo lặp mỗi tin; không có "bước tiếp theo" (tiếp tục / chạy lại / xem web) | `format_result` |
| T6 | Điều phối viên đang nghĩ: chỉ có "typing" một lần; câu trả lời dài cắt thành nhiều `<pre>` khó đọc trên điện thoại | `manager_chat.py` |
| T7 | **Không có cơ chế "hỏi người"**: agent bị dặn "Do not ask questions" (`prompt.py:16`, `tasks.py:394`); `task_block(reason)` và `notify_owner(kind=need_human)` chỉ là văn bản tự do → người phải tự hiểu agent đang hỏi gì và trả lời bằng cách nào | `hub_self.py`, `ops.py` |

---

## 1. Tính năng mới cốt lõi: **Câu hỏi (Question)** — dùng chung cho Telegram, Web, CLI, TUI

Đây là phần người dùng yêu cầu rõ nhất: *"nếu là question thì cho user chọn luôn options, hoặc nhập câu trả lời"*. Làm ở tầng
DAEMON (một nguồn sự thật), mọi kênh chỉ hiển thị + gửi câu trả lời.

### 1.1 Mô hình dữ liệu — migration **v14** (thêm cuối `MIGRATIONS`)
```sql
CREATE TABLE questions (
  id TEXT PRIMARY KEY,                -- q_<12 hex>
  agent_id TEXT NOT NULL, run_id TEXT, session_id TEXT, task_id TEXT, conversation_id TEXT,
  source TEXT NOT NULL,               -- ask_tool | task_block | manager | ask_user_question
  text TEXT NOT NULL,                 -- câu hỏi (đã che secret)
  options TEXT NOT NULL DEFAULT '[]', -- JSON [{key:"1",label,description}] tối đa 6, label ≤ 60 ký tự
  multi INTEGER NOT NULL DEFAULT 0,   -- chọn nhiều
  allow_free_text INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,               -- open | answered | cancelled
  answer TEXT,                        -- JSON {keys:[…], text:"…"}
  answered_by TEXT, answered_channel TEXT, created_at TEXT, answered_at TEXT
);
CREATE INDEX questions_open ON questions(status, created_at);
```
Không có `expired` (cùng tinh thần approval: không tự quyết thay người). Câu hỏi của run bị huỷ/ESTOP → `cancelled`.

### 1.2 Service `questions.py` (`hub.questions`)
- `open(agent_id, run_id, text, options, multi, allow_free_text, source, …)` → kiểm hợp lệ (≤ 6 lựa chọn, nhãn ≤ 60, bỏ ký tự
  điều khiển/bidi như `visible()`), che secret, ghi DB, phát event `question.open`.
- `answer(id, keys=[], text="", channel, by)` → chỉ khi `open`; kiểm `keys ⊆ options`, `multi`, `allow_free_text`; ghi, phát
  `question.answered`, rồi **nối tiếp công việc** (1.4). Idempotent: trả lời lần 2 → `Conflict` (như approval).
- `cancel(id, reason)`; `list(status, agent, run)`; `get(id)`.
- RPC: `question.list|get` (đọc), `question.answer|cancel` (**HUMAN_ONLY**, ∈ `MUTATING`, audit). Agent KHÔNG được tự trả lời câu
  hỏi của mình: thêm `cfharness question answer` vào `HUB_HUMAN_ONLY_CLI` + `mcp__*__hub_question_answer` forbidden (cùng kiểu
  `hub_approve`). Manager: KHÔNG có trong `MANAGER_ALLOWED` (manager hỏi người, không trả lời thay người) — ca đỏ→xanh trong
  `tests/test_manager_security.py` cả 2 lớp.
- Event mới: `question.open` (level warn), `question.answered`, `question.cancelled` → `EVENT_LEVELS`, docstring adapter, sinh lại
  `docs/schema/event.v1.json`.

### 1.3 Agent đặt câu hỏi bằng cách nào (3 đường vào, cùng một service)
1. **Tool hub-self `ask_human`** (mới, mọi vai trò `worker|lead|manager|knowledge`):
   `{question, options?: [{label, description?}], multi?: bool, allow_free_text?: bool (mặc định true)}`.
   Trả về `{question_id, note: "The human was asked. End your turn now; you will be resumed with the answer."}`.
2. **`task_block` mở rộng**: thêm `options?` — nếu có, tạo luôn question `source=task_block` gắn `task_id`.
   `notify_owner(kind=need_human)` của manager: thêm `options?` → question `source=manager` gắn `conversation_id`.
3. **Tool gốc `AskUserQuestion` của Claude** (đang nằm trong `READ_ONLY_TOOLS` và chạy được nhưng vô nghĩa ở `-p`): hook PreToolUse
   (`guard/hook.py`, đã chạy mọi tool) bắt `AskUserQuestion` → gọi RPC `question.open` với `questions[0].question/options/multiSelect`
   (đúng schema gốc) → **deny** kèm lý do "Asked the human via cfharness (question q_…). End your turn; you will be resumed with
   the answer." Ưu điểm: model đã quen tool này, không cần dạy. Fail-closed vẫn giữ (lỗi → deny như cũ, chỉ khác là không tạo câu hỏi).
   Cần spike nhỏ với `claude` thật để xác nhận input schema của phiên bản trong `tested_versions` + ghi fixture.
- Sửa prompt (tiếng Anh): `prompt.py:16`, `tasks.py:394` từ "Do not ask questions" thành "Prefer reasonable assumptions. If a
  decision genuinely needs the human, call `ask_human` with 2–5 concrete options, then end your turn."

### 1.4 Trả lời xong thì sao (nối tiếp công việc)
| source | Hành động sau `answer` |
|---|---|
| `ask_tool` / `ask_user_question` (run thường) | `run.continue(run_id, prompt=<khung trả lời>)` — `--resume` cùng session (cơ chế có sẵn của Telegram reply) |
| `task_block` | ghi `task_comment` (tác giả = người) + `task.unblock` → dispatcher retry `--resume` với khung trả lời |
| `manager` | `manager.chat(conversation_id, text=<khung trả lời>)` |

Khung trả lời gửi cho agent (tiếng Anh, do HUB sinh):
`[cfharness] The human answered question q_…: selected "2. Xoá cả thư mục cache" ; note: "<free text>"`.
Phần chữ tự do của người được đánh dấu rõ là của người.

Run hỏi xong và kết thúc → outcome giữ `completed` nhưng run gắn cờ `awaiting_answer` (trường trong `runs.result_json`, không thêm
outcome mới để khỏi đụng mọi nơi liệt kê trạng thái). Agent hiển thị trạng thái **"Chờ bạn trả lời"** (tính trong `RunManager`
status như `waiting_human`). Tự resume sau restart: KHÔNG (chờ người, giống waiting_human).

### 1.5 Hiển thị trên Telegram (yêu cầu chính)
Ràng buộc WT-8: chữ do agent viết KHÔNG được nằm trong tin có nút. Nhãn lựa chọn là chữ của agent → giải pháp:

```
Tin 1 (NỘI DUNG, không nút):
  ❓ <b>Agent van-hanh hỏi bạn</b> · run r_f9b2…           ← chữ hub
  ▼ Nội dung do AGENT viết:
  <pre>Thư mục /srv/data/tmp-2025 có 3 GB. Xoá kiểu nào?
  1. Chỉ xoá file > 30 ngày
  2. Xoá cả thư mục cache
  3. Không xoá, chỉ báo cáo</pre>

Tin 2 (HỆ THỐNG, trả lời vào tin 1, có nút — chỉ chữ hub sinh):
  Chọn một phương án hoặc trả lời tin này bằng chữ.
  [ 1 ] [ 2 ] [ 3 ]
  [ ✍️ Trả lời bằng chữ ]   [ ✖ Bỏ qua câu hỏi ]
```
- Nút mang SỐ thứ tự (chữ hub), không mang nhãn của agent → giữ nguyên bất biến WT-8, vẫn chọn được bằng một chạm.
  (Phương án thay thế nếu muốn nhãn trên nút: xem Câu hỏi mở Q1.)
- `multi=true`: nút bật/tắt ☐/☑ (sửa tại chỗ bằng `editMessageReplyMarkup`) + nút "Gửi lựa chọn".
- "✍️ Trả lời bằng chữ" → bot gửi tin `force_reply` ("Nhập câu trả lời cho q_…"); reply tin đó, hoặc reply thẳng tin 1/2 → `question.answer(text=…)`.
  Bảng `hub_state telegram.q_msgmap` (`chat:message_id → question_id`), kiểm TRƯỚC `run.continue`/manager trong `handle_update`.
- Callback dùng `CallbackStore` có sẵn (`a="qa"`, ref=`q_id:key`), TIÊU cả nhóm khi gửi; trả lời không cần bước 2 (không phá huỷ).
  Ngoại lệ: nếu câu hỏi gắn approval/destructive thì vẫn đi đường duyệt cũ, không qua câu hỏi.
- Sau khi trả lời: SỬA tin 2 thành "✅ Bạn đã chọn 2 — đã gửi cho agent (run tiếp tục r_…)" và gỡ nút; trả lời ở kênh khác
  (web/CLI) → `question.answered` → sửa tin thành "Đã trả lời qua web".
- Lệnh `/hoi` (alias `/questions`): liệt kê câu hỏi đang mở, mỗi câu gửi lại cặp tin trên.
- Bot gửi câu hỏi tới `notify_chats` (như approval) + chat đang theo dõi run (`telegram.watch`) / chat của conversation manager.

### 1.6 Hiển thị trên Web
- Thẻ câu hỏi (`_question.html`): khung "❓ Agent X hỏi bạn", nội dung agent autoescape `pre-wrap`, lựa chọn = **radio/checkbox
  có nhãn** (trên web nhãn agent nằm trong `<label>` autoescape, không phải link → an toàn; vẫn tách nút "Gửi" do hub vẽ),
  ô "Câu trả lời khác…" (textarea), nút **Gửi câu trả lời**. POST `/do/question_answer` (thêm vào `ACTIONS`, `audit=True`,
  `post_guard`), không cần hộp xác nhận.
- Hiện ở: hộp "Cần bạn" trên Tổng quan (2.3), trang run (thay chỗ khung duyệt), trang task (khi blocked có câu hỏi), trong luồng
  chat Điều phối (thẻ inline ngay dưới tin của điều phối viên).
- Badge menu "Cần bạn" = approvals + questions đang mở.

### 1.7 CLI/TUI (tối thiểu, để đủ kênh)
`cfharness questions [--all]`, `cfharness answer <q> [--pick 2[,3]] [--text "…"]`; TUI: thêm cột/biểu tượng ❓ ở `HtopScreen`
và phím `a` mở hộp trả lời (dùng `ConfirmScreen`/`InputScreen` có sẵn). Có thể để pha sau.

---

## 2. Làm lại Web

Nguyên tắc: **"Việc của bạn trước, thông tin sau"**; ít chữ kỹ thuật; mỗi màn có MỘT hành động chính. Không đổi kiến trúc
(Starlette + Jinja2 + htmx + SSE, client thuần RPC), không framework JS, không CDN.

### 2.1 Hệ thiết kế (pha W-A, nền cho mọi thứ khác)
- `app.css` tách thành `tokens.css` (màu, khoảng cách 4/8/12/16/24/32, bán kính, bóng, cỡ chữ 12/13/15/17/20/24) + `components.css`.
  Số liệu `font-variant-numeric: tabular-nums`. Font hệ thống giữ nguyên (không tải font ngoài).
- **Bộ icon SVG** `web/static/icons.svg` (sprite `<symbol>`, vẽ tay ~25 icon nét 1.5px: check, x, pause, play, stop, trash, edit,
  bot, team, task, run, approval, question, cost, log, settings, learn, plug(MCP), chevron, external, copy, clock, alert, spark).
  Macro Jinja `icon(name)` → `<svg><use href="/static/icons.svg#name"></svg>` (CSP `'self'` cho phép). Bỏ toàn bộ emoji trong UI
  (giữ emoji trong Telegram).
- Nút: 4 kiểu duy nhất `primary | secondary | danger | ghost` × 2 cỡ; trạng thái `aria-busy` + spinner CSS khi htmx đang gửi
  (`htmx-request`). Pill trạng thái thống nhất một macro `status_pill(status)` với màu + icon: Đang làm (xanh lá, chấm nhấp nháy),
  Chờ duyệt (đỏ), Chờ trả lời (cam), Rảnh (xám), Lỗi (đỏ đậm), Tạm dừng (xám sọc).
- Toast thay flash: vùng `#toasts` góc dưới phải (mobile: trên cùng), tự ẩn 5 s, lỗi giữ tới khi đóng. Server vẫn đặt flash trong
  cookie/session như cũ; JS chỉ chuyển cách hiển thị (không JS → vẫn thấy flash).
- Thời gian tương đối (`<time datetime=… data-rel>` → "2 phút trước", tooltip giờ đầy đủ) trong `app.js`.
- Chế độ tối: rà lại tương phản (WCAG AA) cho mọi token; kiểm bằng ảnh `*-toi.png`.

### 2.2 Bố cục khung (pha W-B)
- Desktop ≥ 1024 px: **sidebar trái** 220 px có nhóm:
  - *Làm việc*: Điều phối · Cần bạn (badge) · Tổng quan
  - *Đội ngũ*: Agent · Team · Công việc · Lượt chạy
  - *Học & chất lượng*: Tự học
  - *Hệ thống*: MCP · Chi phí · Nhật ký · Cài đặt
  - đáy sidebar: trạng thái daemon (chấm xanh/đỏ), ESTOP (nút đỏ luôn thấy), Thoát.
- Tablet: sidebar thu gọn chỉ icon (tooltip). Mobile < 700 px: **thanh tab đáy** 5 mục (Điều phối · Cần bạn · Agent · Lượt chạy · Thêm)
  — "Thêm" mở sheet các mục còn lại.
- Header trang thống nhất: breadcrumb · tiêu đề · pill trạng thái · nhóm hành động bên phải (chính + menu "⋯" cho phụ/nguy hiểm).
- Gộp trang "Duyệt" thành **"Cần bạn"** (`/inbox`, giữ `/approvals` redirect): approvals + questions + task blocked + run lỗi
  chưa xem, sắp theo độ khẩn.

### 2.3 Từng màn (pha W-C)
| Màn | Thay đổi |
|---|---|
| **Tổng quan** | (1) Dải "Cần bạn" trên cùng: tối đa 3 thẻ gọn (duyệt/câu hỏi) với nút hành động ngay + "xem tất cả". Trống → dòng xanh "Không có gì cần bạn". (2) 4 số liệu (đang chạy, chờ bạn, chi phí hôm nay so với ngân sách có thanh %, thay đổi tự học). (3) **Lưới thẻ agent** thay bảng: tên + vai trò, pill trạng thái, "đang làm" 1 dòng (tiêu đề task, không phải prompt thô), chi phí hôm nay, nút "Xem trực tiếp" khi đang chạy. Bỏ cột "Công cụ gần nhất". (4) "Hoạt động" chỉ sự kiện có nghĩa (bắt đầu/xong/lỗi/duyệt/hỏi/học), gộp các dòng liên tiếp cùng agent, câu chữ tiếng Việt do hub sinh ("Trợ lý nghiên cứu đã xong *Tóm tắt README* · $0.004"), id ẩn vào link. Giao việc nhanh chuyển thành nút "+ Giao việc" mở drawer |
| **Lượt chạy (chi tiết)** | Header dính: agent, pill, chi phí/token/thời gian dạng chip, nút Dừng. Khung duyệt/câu hỏi **một chỗ duy nhất** ngay dưới header; trong diễn biến chỉ còn 1 dòng mốc "⏸ Xin duyệt — xem phía trên" (sửa `replay_items` gộp `approval.request`+`waiting_human`). Diễn biến dạng **timeline**: tin của người (phải), lời agent (trái, `md()` an toàn có sẵn), nhóm tool liên tiếp gộp thành một dòng "5 công cụ · Bash ×3, Read ×2" bấm để mở. Ô chỉ đạo (steer) dính đáy như chat |
| **Danh sách lượt chạy** | Cột "Yêu cầu" hiển thị tiêu đề task nếu có, nếu không thì 80 ký tự đầu prompt; bỏ tiền tố `[cfharness task …] You have been assigned…` (lấy `task.title`). Lọc nhanh dạng chip (Đang chạy · Chờ bạn · Lỗi · Xong) |
| **Agent** | Header: tên, vai trò, pill; hành động chính "Giao việc"; Sửa/Tạm dừng/Xoá vào menu "⋯". Tab: Tổng quan · Lượt chạy · Cấu hình · Tri thức · Lịch sử. Thẻ cấu hình thành danh sách định nghĩa gọn có icon |
| **Form agent** | Chia 3 bước (Cơ bản → Công cụ & quyền → Ngân sách & nâng cao) trên cùng một form (chỉ ẩn/hiện bằng CSS `:target`/JS nhẹ, không JS vẫn hiện hết); chọn template dạng thẻ có mô tả; giải thích ngắn dưới từng trường |
| **Điều phối** | Bố cục kiểu app chat: cột trái danh sách cuộc trò chuyện (mobile: drawer), giữa là luồng tin, phải là panel điều phối viên (mobile: ẩn sau nút ⓘ). Luồng tin cuộn tới cuối khi mở, không cắt thẻ. Thẻ hành động gom thành MỘT khối "Đã làm 3 việc ▸" dưới tin trả lời tương ứng (mở ra từng việc + link). Thẻ câu hỏi (1.6) inline. Trạng thái "đang trả lời…" có chấm động + delta stream. Ô nhập dính đáy, Enter gửi / Shift+Enter xuống dòng, gợi ý nhanh (chip: "Có gì cần tôi?", "Chi phí tuần này", "Giao việc mới") |
| **Công việc** | Kanban giữ, thẻ gọn hơn (tiêu đề, người làm, pill, thời gian); task blocked có câu hỏi → hiện nút "Trả lời" |
| **Cài đặt** | Nhóm: Thông báo (Slack/Telegram trạng thái + hướng dẫn ghép nối), An toàn (ESTOP, chính sách — chỉ xem), Giới hạn (`SAFE_CONFIG`), Truy cập web |
| Trống/lỗi | Mỗi danh sách có empty state có hướng dẫn 1 câu + nút hành động; trang lỗi daemon tắt có lệnh khởi động |

### 2.4 Kỹ thuật & kiểm thử web
- Giữ nguyên route/`ACTIONS`/`post_guard`; thêm `question_answer`. Sửa `present.py` (thuần) để sinh câu chữ hoạt động tiếng Việt +
  gộp dòng — có unit test thuần.
- `tests/test_web.py` (27 test) đang kiểm chuỗi/HTML cụ thể → cập nhật theo markup mới NHƯNG giữ mọi ca an toàn (CSP header, không
  inline script/style — thêm test quét toàn bộ template: không có `style=`/`<script>` inline/`on*=`; XSS ở `test_web_manager.py`).
- Ảnh màn: chạy lại `web/demo.py` + Playwright chụp sáng/tối/mobile cho mọi màn → `docs/screenshots/web/v2/`. So trước/sau trong PR.
- Kiểm a11y: điều hướng bàn phím, focus ring, `aria-live` cho toast/SSE, tương phản.

---

## 3. Làm lại Telegram

Nguyên tắc: **ít tin hơn, sửa tại chỗ, bấm thay vì gõ**. Giữ WT-2/4/5/8, nonce dùng một lần, audit.

### 3.1 Khung (pha T-A)
- `setMyCommands` khi bot khởi động (chữ hub, tiếng Việt): `/trangthai` (`/status`), `/canban` — "Cần bạn" (`/approvals`+câu hỏi),
  `/giaoviec` (`/run`), `/agent`, `/luotchay` (`/runs`), `/moi`, `/digest`, `/estop`, `/help`. Giữ alias tiếng Anh cũ.
- **Bàn phím nhanh** (ReplyKeyboardMarkup, `is_persistent`, `resize_keyboard`) ở chat riêng: `📋 Cần bạn` · `📊 Trạng thái` /
  `➕ Giao việc` · `🤖 Agent`. Bấm = gửi đúng lệnh tương ứng (xử lý như lệnh, không chuyển cho điều phối viên).
- Bộ định dạng chung `notify/telegram_fmt.py`: tiêu đề `<b>` + emoji trạng thái (✅ xong, ❌ lỗi, ⏸ chờ duyệt, ❓ hỏi, ▶️ đang chạy,
  🛑 ESTOP), dòng phụ `<i>`, số tiền `$0.004`, thời gian tương đối, trạng thái **tiếng Việt** (dùng chung bảng nhãn với `web/present.py`
  — chuyển bảng nhãn sang module dùng chung `labels.py`).
- Rút gọn tiền tố WT-8: giữ tiền tố cố định nhưng ngắn "▼ Agent viết:" (vẫn là chữ hub, vẫn trước `<pre>`) — cập nhật test WT-8.

### 3.2 Duyệt gọn (pha T-B) — từ ≥ 4 tin xuống **2 tin**
1. Tin nội dung (không nút) — như hiện nay nhưng gọn: `⏸ <b>van-hanh muốn chạy</b>` + `<pre>lệnh</pre>` + 1 dòng lý do.
2. Tin hệ thống (có nút): `[✅ Cho phép] [⛔ Từ chối]` + dòng nhỏ luật/lớp.
   - Bấm "Cho phép" → **sửa chính tin 2** thành bước xác nhận: "Xác nhận cho phép MỘT lần `rm -rf …`? (đọc lại tin trên)"
     `[✅ Xác nhận] [↩ Quay lại]` — vẫn 2 bước (WT-5), vẫn chỉ chữ hub, nhưng không sinh tin mới. Nonce mới cho bước 2 như cũ.
   - Kết quả → sửa tin 2 thành "✅ Đã cho phép lúc 23:21 — run chạy tiếp" (gỡ nút). Không gửi thêm tin "Đã CHO PHÉP…" (chỉ toast
     `answerCallbackQuery`).
   - "Báo nhầm & cho phép" (có trên web) thêm vào Telegram ở bước xác nhận (nút thứ 3) nếu RPC hỗ trợ.
3. Nhắc lại (`approval.reminder`): sửa tin 2 thêm dòng "nhắc lần 2" + gửi tin ngắn 1 dòng reply vào tin 1 (để có thông báo đẩy),
   không gửi lại toàn bộ.
- Lưu `message_id` tin 2 vào `CallbackStore.add_message` (đã có) để `editMessageText` khi quyết ở kênh khác.

### 3.3 Lệnh tương tác (pha T-C)
- `/giaoviec` không tham số → tin "Giao việc cho ai?" + nút mỗi agent (id agent là chữ hub, charset an toàn; tối đa 8 nút + "Khác…")
  → bấm → `force_reply` "Mô tả việc cần làm cho <agent>:" → tin trả lời = prompt → `run.start`. Vẫn giữ `/run <agent> <yêu cầu>`.
- `/agent` → mỗi agent 1 dòng `🟢 nghien-cuu — Rảnh · $0.008 hôm nay` + nút `[Giao việc] [Lượt gần nhất]`; agent đang chạy thêm `[Dừng]`.
- `/luotchay` → 5 run gần nhất có trạng thái tiếng Việt, tiêu đề task/prompt ngắn (chữ agent/người → trong `<pre>`? Prompt là chữ
  của NGƯỜI, nhưng prompt task có thể do lead/agent tạo → coi là dữ liệu: tách tin danh sách (không nút, `<pre>`) và tin nút
  `[1] [2] [3] [4] [5]` → chi tiết run. Cùng mẫu số-thay-nhãn như câu hỏi.)
- `/stop` không tham số → nút chọn run đang chạy.

### 3.4 Kết quả run & điều phối viên (pha T-D)
- Kết quả run: dòng hệ thống 1 dòng `✅ <b>nghien-cuu xong</b> · $0.004 · 12 s` + `▼ Agent viết:` + `<pre>`; cắt ở `result_max_chars`
  kèm "xem đầy đủ trên web: /runs/<id>" (link web chỉ in dạng chữ, `cfharness.localhost` không mở được trên điện thoại → nói rõ).
  Thêm **một** tin hệ thống nhỏ có nút chỉ khi có bước tiếp hợp lý: lỗi/vượt ngân sách → `[🔁 Chạy lại]`; xong → không nút (reply
  để tiếp tục như nay — ghi rõ "↩ Trả lời tin này để giao tiếp").
- Run đang chạy lâu (> 30 s, run do chat này giao): một tin tiến độ **sửa tại chỗ** mỗi ≥ 10 s ("▶️ đang làm · 7 công cụ · $0.002"),
  xoá/sửa thành kết quả khi xong. Giới hạn tần suất `editMessageText` (≤ 1/10 s/tin, tránh 429).
- Điều phối viên: `sendChatAction typing` lặp mỗi 4 s tới khi có trả lời (hiện chỉ 1 lần); trả lời dài → một tin `<pre>` đầu + "…
  (còn N ký tự — xem trên web)" thay vì tới 4 tin; hành động của manager gửi gọn 1 dòng hệ thống "Đã làm: tạo agent X, giao task Y".
- Câu hỏi của manager (`notify_owner kind=need_human` có options) → mẫu 1.5.

### 3.5 Kiểm thử Telegram
- `tests/telegram_fake.py`: thêm `setMyCommands`, `editMessageText`, `ReplyKeyboardMarkup`, `force_reply` (ghi lại để assert).
- Giữ toàn bộ ca an toàn hiện có (`test_wt2_*`, `test_wt4_*`, `test_wt5_*`, `test_wt8_*`) — chỉ đổi kỳ vọng "tin mới" → "tin được sửa".
  Ca mới: câu hỏi chọn 1/nhiều/chữ; nút bấm lại; người ngoài allowlist bấm; trả lời qua web → tin Telegram được sửa; nhãn lựa chọn chứa
  HTML/`/lệnh`/link → chỉ xuất hiện trong `<pre>`, không bao giờ trên nút; câu hỏi bị huỷ khi ESTOP/stop run.
- e2e (`test_telegram_e2e.py`): agent giả gọi `AskUserQuestion` → hook → câu hỏi → bấm "2" → run tiếp tục với khung trả lời đúng.
- Thử tay với Telegram thật (ghi kết quả vào `docs/dev-notes/telegram.md`): bot thật + 1 run thật hỏi câu có 3 lựa chọn.

---

## 4. Thứ tự thi công & ước lượng

| Pha | Nội dung | Phụ thuộc | Ước lượng |
|---|---|---|---|
| **Q-1** | Migration v14, `questions.py`, RPC + HUMAN_ONLY + audit + event + schema, CLI `questions/answer`, test service + bảo mật (agent/manager không tự trả lời) | — | 1.5 ngày |
| **Q-2** | Đường vào: `ask_human` hub-self, `task_block/notify_owner` có options, hook bắt `AskUserQuestion` (+ spike claude thật, fixture), sửa prompt, nối tiếp sau trả lời (continue/unblock/manager.chat), trạng thái "Chờ bạn trả lời" | Q-1 | 2 ngày |
| **T-A** | Menu lệnh, bàn phím nhanh, bộ định dạng + nhãn dùng chung | — (song song Q) | 0.5 ngày |
| **T-Q** | Câu hỏi trên Telegram (1.5) | Q-1, T-A | 1 ngày |
| **T-B** | Duyệt 2 tin + sửa tại chỗ | T-A | 1 ngày |
| **T-C, T-D** | Lệnh tương tác, kết quả/tiến độ, điều phối viên | T-A | 1.5 ngày |
| **W-A** | Token, icon sprite, nút/pill/toast/thời gian tương đối | — (song song) | 1 ngày |
| **W-B** | Sidebar/tab đáy, header trang, trang "Cần bạn" | W-A | 1 ngày |
| **W-Q** | Thẻ câu hỏi web + action `question_answer` | Q-1, W-A | 0.5 ngày |
| **W-C** | Làm lại từng màn (Tổng quan, Run, Điều phối trước; còn lại sau) | W-B | 3 ngày |
| **Z** | Cập nhật test, ảnh màn v2 sáng/tối/mobile, dev-notes (`web.md`, `telegram.md`, `M7-ui.md`), AGENTS.md mục mới | tất cả | 1 ngày |

Tổng ≈ 14 ngày công. Có thể chia 3 luồng song song: **Q** (lõi câu hỏi) · **T** (Telegram) · **W** (web), hội tụ ở T-Q và W-Q.
Ưu tiên giá trị sớm: Q-1 → Q-2 → T-Q (người dùng có ngay "chọn option trên Tele"), song song W-A → W-B → Tổng quan/Run/Điều phối.

## 5. Rủi ro
- **WT-8 vs nhãn trên nút** — đã chọn "số trên nút, nhãn trong `<pre>`". Nếu đổi sang nhãn trên nút phải có biện pháp bù (Q1).
- `AskUserQuestion` ở chế độ `-p` có thể đổi hành vi theo phiên bản CLI → bọc qua `flag_matrix`/`tested_versions`, có fixture; nếu
  phiên bản không hỗ trợ, vẫn còn `ask_human` (hub-self) làm đường chính.
- Sửa tin Telegram bị giới hạn tốc độ (429) → hàng đợi `QueuedNotifier` đã có backoff; gộp sửa tiến độ.
- Đổi markup web làm đỏ nhiều test chuỗi → làm theo pha, mỗi pha xanh toàn bộ `uv run pytest -q` trước khi qua pha sau.
- Run "hỏi rồi kết thúc" giữ khoá repo? → KHÔNG giữ (khác waiting_human): run đã kết thúc sạch, lần tiếp tục lấy khoá lại như resume.

## 6. Quyết định đã chốt (2026-10-19)
- **Q1** — nút Telegram mang SỐ `[1][2][3]`, nhãn đầy đủ trong tin nội dung phía trên (giữ WT-8).
- **Q2** — web dùng sidebar có nhóm (2.2).
- **Q3** — câu hỏi chờ người được nhắc lại giãn dần (`notify.reminder_interval_s` → `reminder_max_interval_s`), không bao giờ tự trả lời.
- **Q4** — agent trong task hỏi **điều phối viên trước**; điều phối viên không chốt được (`question_escalate`) hoặc quá
  `questions.manager_timeout_s` (900 s), hoặc không có/đang tạm dừng/ESTOP → hỏi người. Câu hỏi của chính điều phối viên
  (`ask_owner`) và agent ngoài task → người ngay. Điều phối viên KHÔNG trả lời được câu hỏi đã lên người hay câu hỏi của chính nó
  (kiểm ở cả lớp 1 `ManagerOps` và lớp 2 `Daemon._dispatch` qua `permissions.check_rpc`).

## 7. Tiến độ thi công
| Pha | Trạng thái |
|---|---|
| Q-1 lõi câu hỏi | ✅ migration v14 `questions`; `questions/service.py` (`hub.questions`); RPC `question.list|get|answer|escalate|cancel` (`questions/rpc.py`, tự audit); HUMAN_ONLY + MANAGER_ALLOWED (`answer`/`escalate`); classifier chặn `cfharness answer`/`questions answer|escalate|cancel`; event `question.open|escalated|answered|cancelled|reminder` + schema; CLI `cfharness questions`, `cfharness answer <id> --pick 2 --text …`; test `tests/test_questions.py` (15 ca) |
| Q-2 đường vào | ✅ hub-self `ask_human` (worker/lead); manager `question_list|question_answer|question_escalate|ask_owner`; prompt worker + prompt nhịp manager; nối tiếp: task → bình luận `answer` + gỡ chặn; hội thoại manager → `manager.chat`; run thường → `run.continue`. ⏳ còn: hook bắt `AskUserQuestion` (cần spike `claude` thật), `task_block`/`notify_owner` có `options` (đã có `ask_human`/`ask_owner` thay thế) |
| T-Q, T-A … T-D | ✅ `chat/questions_tg.py` (câu hỏi: tin nội dung `<pre>` + tin nút số, ☐/☑ chọn nhiều, ✍️ trả lời chữ qua force_reply/reply, sửa tin khi trả lời ở mọi kênh, `/hoi`, `/canban`), `chat/flows.py` (`/giaoviec` chọn agent bằng nút, `/agent`, `/luotchay`, `/stop` chọn run, tin tiến độ sửa tại chỗ), duyệt còn 2 tin (bước 2 sửa tại chỗ), menu lệnh tiếng Việt + bàn phím nhanh. Test `tests/test_telegram_questions.py`. Chi tiết: `docs/dev-notes/telegram.md` mục "UX v2" |
| W-Q, W-A, W-B, W-C | ✅ thẻ câu hỏi + `ACTIONS` `question_answer|question_take|question_cancel`, trang `/inbox` "Cần bạn", sidebar nhóm + tab đáy mobile, icon SVG, toast, thời gian tương đối, làm lại Tổng quan/Run/Điều phối/danh sách. Ảnh `docs/screenshots/web/v2/`. Test `tests/test_web_questions.py`. Chi tiết: `docs/dev-notes/web.md` mục "UX v2". ⏳ còn: gom tool liên tiếp trên trang run, form agent nhiều bước, tab trang agent, bố cục riêng cho Tự học/MCP/Chi phí/Nhật ký |

Lệch so với plan ở mục 1: nối tiếp task KHÔNG dùng `--resume` mà dùng bình luận `answer` + gỡ chặn (dispatcher chạy lại, prompt
worker đã có khối Comments) — đơn giản, dùng lại đường retry sẵn có. Run hỏi xong kết thúc với outcome `blocked` (task) như `task_block`.
