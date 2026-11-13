# Kế hoạch: câu trả lời của AI + giao diện hiển thị giống Claude chat

Ngày: 2026-11-13. Trạng thái: **đề xuất, chờ chốt 4 quyết định ở mục 1**. Ba agent đã đọc code để viết kế hoạch này, chưa sửa file nào:
(A) trang Điều phối `/manager`, (B) transcript trang run, (C) nội dung câu trả lời và các kênh TUI/Telegram/Slack.

Mọi ràng buộc an toàn hiện có vẫn giữ nguyên:
- CSP `'self'`, không CDN, không script/style inline.
- Nội dung agent là DỮ LIỆU.
- Client chỉ chèn HTML do server vẽ hoặc dùng `textContent`.
- WT-8, cổng HUMAN_ONLY.

---

## 0. Chẩn đoán ngắn

| # | Vấn đề | Bằng chứng |
|---|---|---|
| D1 | Chữ của điều phối viên là văn bản thô `pre-wrap`, không có markdown, khối code nằm lẫn trong đoạn văn | `_manager.html:1,7`, `app.css:674`, ảnh `16-dieu-phoi.png` |
| D2 | `md()` dùng preset `commonmark`: không có bảng hay gạch ngang, `![](…)` vẫn ra `<img>`, link không có `rel`, code không tô màu và không có nút copy | `present.py:128-143` |
| D3 | Live run: khi tool xong thì mất args/diff; khi run kết thúc thì `location.reload()` xoá thinking và các khối đang mở | `app.py:1449-1467`, `app.js:267-271` |
| D4 | Thinking chỉ thấy lúc live; khối `thinking` trong `assistant` không được ánh xạ nên `reasoning.complete` không bao giờ phát | `claude_adapter.py:396-435` |
| D5 | SSE Điều phối: nối lại thì chữ bị nhân đôi (không có `id`/`seq`); nhận `end` là chết luồng; lời dẫn biến mất khi `reply` tới; nhiều lượt dính chữ vào nhau | `app.py:1399`, `app.js:503-567`, `manager.py:375-414` |
| D6 | Không có nút Dừng ở Điều phối; không có Copy/Retry; không có nút "về tin mới nhất" | `manager.html:103-131` |
| D7 | Tool hiển thị dạng hộp to, tên kỹ thuật; không xem được input; output bị cắt ở 600 ký tự dù adapter giữ 2000; không gom tool liên tiếp; subagent không lồng | `_items.html:11-18`, `present.py:204` |
| D8 | Kết quả lặp hai lần (Diễn biến + thẻ Kết quả); nhiều dòng hệ thống gây nhiễu; ô steer đè lên transcript | `run.html:31-42`, ảnh `11-run-xong.png`, `04-run-live.png` |
| D9 | Prompt yêu cầu *"plain-text summary"*; SOUL điều phối viên **yêu cầu** nêu task id; không có quy tắc ngôn ngữ hay cách trình bày | `prompt.py:21`, `templates/manager.yaml`, `tasks.py:518` |
| D10 | Telegram đặt cả câu trả lời trong `<pre>`, nên markdown hiện thô; chia tin cắt cứng 3500 ký tự làm vỡ code fence (cả Slack); `TEXT_MAX=4000` cắt mà không báo | `manager_chat.py:200-218`, `service.py:158,810` |

---

## 1. Quyết định cần chủ repo chốt trước

| # | Câu hỏi | Đề xuất |
|---|---|---|
| Q1 | Bỏ bất biến "Điều phối: không markdown" (AGENTS.md M7, M7-ui §3, test `test_manager_content_is_escaped_never_raw_html`)? | **Bỏ.** Chỉ cho phép đầu ra của `md()` đã cứng hoá được `\|safe`. Tin người dùng vẫn để thô. Đã có tiền lệ TUI `cc_render.markdown_text` (2026-10-26). |
| Q2 | Link ngoài do agent viết hiển thị thế nào? | Web: link thật, có `rel="nofollow noopener noreferrer"` `target=_blank` và hiện tên miền thật bên cạnh. Telegram: dạng chữ `nhãn (URL)`. Link nội bộ vẫn chỉ đến từ `mgr_target`. |
| Q3 | Telegram bỏ `<pre>` và chuyển markdown sang HTML (nới WT-8 giống Slack)? | **Có**, kèm cờ `notify.telegram.agent_render: markdown\|pre` để quay lại khung cũ nhanh. Dòng hub vẫn đứng trước `AGENT_FRAME`. |
| Q4 | Lưu thinking vào DB (`reasoning.complete`)? | **Có.** Qua `redact`, cắt ở 8000 ký tự, cờ `runtime.claude.persist_thinking` (mặc định bật), dùng retention có sẵn. |

---

## 2. Kiến trúc: một bộ render dùng chung

Ý chính của kế hoạch: trang run và trang Điều phối **dùng chung một bộ render**, thay vì mỗi trang tự làm một kiểu như hiện nay.

- **`present.py`** (hàm thuần):
  - `md()` đã cứng hoá: preset commonmark cộng `table`, `strikethrough`, `tasklists`; `html=False`; ảnh thành link chữ; `link_open` có `rel` và tên miền; fence có header (ngôn ngữ do pygments xác định, nút Sao chép); pygments chỉ dùng **class**, không dùng `style=`; LRU cache.
  - `tool_view()`, `merge_tool()`, `group_items()`.
- **`templates/_transcript.html`** (mới): các macro `tx_msg`, `tx_tool`, `tx_group`, `tx_thinking`. `_items.html` và `_manager.html` đều import file này.
- **CSS `.tx-*`**: hợp nhất `.ti.*` (run) với `.mm*`/`.mm-acts` (Điều phối).
- **JS `txAppend(container, msg)`**: gộp các phần delta→`textContent`, thay theo `dom`, gom nhóm, giữ vị trí cuộn, copy. Cả `initManager` và live run đều gọi hàm này.
- **SSE**: các loại `delta` / `part` (HTML server vẽ, có điều tiết) / `item` / `seg` / `think` / `tool`, có kèm `seq` để chống trùng.
- **`chat/mdconv.py`** (mới): markdown → khối trung gian → `to_telegram_html()` / `to_slack_mrkdwn()` + `split_md()` chia tin theo khối.
- Khai báo rõ trong `pyproject.toml`: `markdown-it-py`, `mdit-py-plugins`, `pygments`. Hiện chúng chỉ có mặt gián tiếp qua rich/textual.

---

## 3. Lộ trình

### Đợt 1 — P0: sửa lỗi + nền tảng (1 PR mỗi gạch đầu dòng)

| ID | Việc | File chính | Công |
|---|---|---|---|
| P0-1 | **Prompt "Response style"**: bỏ "plain-text"; thêm khối `RESPONSE_STYLE` (tiếng Anh) sau SOUL/charter, trước khối tri thức; ghi chú theo kênh `format_note(channel)` trong tin user (không đổi hash); định dạng kết quả task cho worker/resume | `prompt.py`, `manager/prompts.py`, `tasks.py:518,526`, `runs.py:1281` | S |
| P0-2 | **Cứng hoá + nâng cấp `md()`**, khối code có header, nút copy, pygments dùng class | `present.py`, `app.css`, `app.js`, `pyproject.toml` | M |
| P0-3 | **Sửa live run**: converter nhớ đủ mục `tool.start` (`merge_tool` dùng chung với `replay_items`); bỏ `location.reload()`, thay bằng partial `/partials/run/<id>/result` | `app.py`, `present.py`, `app.js` | S |
| P0-4 | **Sửa SSE Điều phối**: kèm `seq` và bỏ trùng, tự nối lại có backoff (cả khi nhận `end`), `seg` ở `message.complete`, bỏ `scroll-behavior: smooth` trên log | `manager.py`, `app.js`, `app.css` | M |
| P0-5 | **Markdown cho Điều phối** (sau khi Q1 được chốt) qua `tx_msg` dùng chung | `_manager.html`, `manager.py`, `_transcript.html` | S |
| P0-6 | **Nút Dừng** ở Điều phối: `POST /manager/stop`, nút gửi biến thành nút dừng khi đang chạy và ô nhập trống, không hộp xác nhận, có audit kênh `web` | `manager.py`, `manager.html`, `app.js` | S/M |
| P0-7 | **Tool một dòng kiểu Claude**: "Đọc a.py", "Chạy `npm test`", "Sửa x +3 −1"…; mở ra xem Input/Output đầy đủ (2000 ký tự); lỗi màu đỏ và mở sẵn | `present.py` (`tool_view`), `_transcript.html`, `app.css` | M |
| P0-8 | **Bỏ kết quả lặp + giảm nhiễu**: thẻ Kết quả chỉ còn phản hồi khi trùng tin cuối; dòng start, steer.ack, subagent và end thu gọn | `run.html`, `present.py`, `replay_items` | S |
| P0-9 | **Lưu thinking** (sau khi Q4 được chốt): nhánh `thinking`/`redacted_thinking` trong adapter, `EVENT_LEVELS`, fixture + contract test | `claude_adapter.py`, `events.py`, `config.py` | M |
| P0-10 | **Telegram markdown** (sau khi Q3 được chốt): dùng `mdconv`; bảng hẹp thành `<pre>`, bảng rộng thành list; `split_md` theo khối; `TEXT_MAX` lên ~14000 và có cờ `truncated`; tin quá dài gửi kèm tệp `.md` | `chat/mdconv.py`, `manager_chat.py`, `chat/base.py`, `manager/service.py` | M |

### Đợt 2 — P1: cảm giác Claude

| ID | Việc | Công |
|---|---|---|
| P1-1 | Hiện markdown ngay khi đang chạy chữ: server render có điều tiết (~150–250 ms), gửi `{t:"part", html}`, ngưỡng 8–24 KB | M |
| P1-2 | Gom các tool liên tiếp: "Đã dùng 5 công cụ · Đọc ×3, Chạy ×2"; mở sẵn khi lỗi hoặc đang chạy; dùng chung cho chip "Đã làm N việc" của Điều phối | M |
| P1-3 | Subagent lồng trong mục tool `Agent(mô tả)` | M |
| P1-4 | Thinking gập lại + dòng "Đang dùng …" trong khung chờ của Điều phối (SSE `think`/`tool`, chỉ dùng `textContent`) | M |
| P1-5 | Hàng thao tác dưới tin: Copy (văn bản gốc nằm trong `<template>`), Thử lại, Sửa tin người; giờ hiện khi hover | S/M |
| P1-6 | Bố cục chat một cột cho trang run (dùng lại `.mgr-log` / `.composer-wrap`), ô steer không đè nội dung, nút "↓ tin mới nhất" ở cả hai trang | M |
| P1-7 | Diff Edit/Write dùng `difflib` có ngữ cảnh, dùng lại `.udiff` | S/M |
| P1-8 | Ô nhập + trợ năng: Enter xuống dòng trên màn cảm ứng, Esc để dừng, bản nháp lưu `sessionStorage`, bỏ `aria-live` trên log và thay bằng vùng `role=status` | S |
| P1-9 | Slack dùng `mdconv` (bảng, `*nghiêng*`, `hr`, sửa fence vắt qua tin) | M |
| P1-10 | SOUL `manager.yaml` bỏ "(agent, task id)"; `sanitize_reply` gỡ `[cfharness…`/`NO_ACTION`; chữ hub tại `service.py:796` không ghép id | S |
| P1-11 | Duyệt và câu hỏi chỉ hiện đầy đủ một chỗ (trong transcript chỉ còn một dòng link tới thẻ) | S |

### Đợt 3 — P2: hoàn thiện
- TodoWrite thành checklist cập nhật tại chỗ.
- Báo rõ khi tin bị cắt.
- Ẩn bớt khi run trên 300 mục.
- Spinner "✳ Đang nghĩ… 12s · 3.4k token".
- Vạch ngăn theo ngày.
- Mobile: vùng an toàn và `visualViewport`.
- `<blockquote expandable>` cho Telegram.
- Golden eval chấm phong cách trả lời: không có id `t_…`, fence có ngôn ngữ, độ dài, tiếng Việt.

**Thứ tự gợi ý:** P0-1 → P0-2 → P0-3 → P0-4 → (tách `_transcript.html`) → P0-5 → P0-6 → P0-7 → P0-8 → P0-9 → P0-10 → P1.

---

## 4. Bản nháp khối `RESPONSE_STYLE` (gửi agent, tiếng Anh)

```
# Response style
These rules apply to free-text replies a person reads. When you are asked for structured output (a JSON schema), follow the schema exactly instead. Per-message channel notes take precedence over the length guidance here.
- Language: write in the language of the request (Vietnamese unless the owner wrote in another one). Keep code, commands, paths, identifiers and quoted errors unchanged.
- Lead with the answer or outcome in the first sentence. No preamble, no restating the request, no closing offers.
- Match length to the question: a simple question gets 1-3 sentences; a report gets a short structure.
- Light Markdown: short paragraphs; bullets only for 3+ parallel items; **bold** sparingly; `##` headings only with 3+ sections; a table only to compare items across 2+ attributes (max 4 short columns); code, commands and logs in fenced blocks with a language tag; inline `code` for names and paths; no horizontal rules, no decorative emoji.
- After multi-step work, close with: what was done, what is still open, what you need from the person.
- Do not expose internals: no hub headers, no task/run/conversation/workstream ids, no tool names, no raw JSON, no NO_ACTION — unless the person asks for them.
- Never invent results. Say plainly what you have not verified.
```

Ảnh hưởng tới hash:
- Mọi agent đổi `system_prompt_hash` một lần, nên prompt cache cũng mất một lần.
- Session đang resume giữ prompt cũ (`learning/runtime.py:51-57`). Ghi chú theo kênh trong tin user (P0-1) giúp áp dụng ngay cho các hội thoại đang chạy.

---

## 5. Test và kiểm chứng
- **XSS markdown** (`tests/test_web_markdown.py` mới):
  - fence info `"><script>`;
  - `javascript:` viết bằng entity;
  - autolink `<javascript:…>`;
  - ref-link trỏ tới `javascript:`;
  - `data:` URL;
  - HTML nằm trong ô bảng;
  - ảnh không được ra `<img`;
  - kết quả không chứa `style=`.
- **Đồng nhất replay/live**: cùng một chuỗi event đi qua `replay_items` và `_run_converter` phải ra cùng HTML.
- **SSE**: các message có `seq`; `message.complete` sinh ra `seg`; `draft`/`part.html` đã được sanitize.
- **Prompt** (`tests/test_prompt_style.py`):
  - `RESPONSE_STYLE` nằm đúng vị trí và hash ổn định;
  - lead/review không có dòng định dạng kết quả;
  - ghi chú kênh có cho Telegram/Slack, không có cho web.
- **`tests/test_mdconv.py`**:
  - chỉ ra thẻ thuộc whitelist Telegram và thẻ cân;
  - không có `pre`/`code` nằm trong `blockquote`;
  - `split_md` không vượt ngân sách và mở lại fence đúng ngôn ngữ.
- **Cập nhật test**: `test_web_manager.py`, `test_web.py:557-569`, `test_web_files.py:177-191`, `test_telegram*.py` (WT-8 với bất biến mới), `test_slack.py`.
- **Thủ công**:
  - `uv run pytest -q`;
  - `web.demo` + Playwright (sáng, tối, 390 px; `scrollWidth == clientWidth`; không có lỗi CSP);
  - chụp lại ảnh `16`, `17`, `v2/04`, `v2/11`.

## 6. Rủi ro
- XSS qua markdown hoặc tên ngôn ngữ của khối code: giảm bằng bộ test ở mục 5, và chỉ `md()` được `|safe`.
- CSP chặn `style=` từ pygments: chỉ dùng class.
- Tốn CPU khi render lại markdown lúc chữ đang chạy: điều tiết + LRU + ngưỡng kích thước.
- Lưu thinking làm DB lớn hơn và có thể lộ dữ liệu: `redact`, cắt bớt, có cờ cấu hình.
- Đổi id DOM làm hỏng test hoặc JS: giữ nguyên `runbar`, `transcript`, `live-state`, `steer-form`, `st-cost`, `st-tok`, `.mm mgr`, `.mm-acts`, `data-run`.

---

## 7. Nhật ký thi công

- **2026-11-13 — chốt Q1–Q4: đồng ý cả 4 đề xuất** (markdown cho Điều phối; link ngoài có `rel` + tên miền / Telegram `nhãn (URL)`;
  Telegram bỏ `<pre>` có cờ quay lại; lưu thinking có redact + cắt + cờ).
- **P0-1 (PR `feat/response-style`)**: `prompt.RESPONSE_STYLE` ghép sau SOUL/charter, trước khối tri thức; bỏ "plain-text" khỏi
  `HUB_CONVENTIONS`; `manager/prompts.format_note(channel)` cho Telegram/Slack (trong tin user → không đổi hash); `tick_prompt`
  nhắc notify_owner theo Response style (NO_ACTION vẫn là tín hiệu nhịp, không bị khối style cấm); worker/resume task trả kết quả
  dạng markdown; SOUL mẫu `manager.yaml` bỏ "(agent, task id)" (chỉ ảnh hưởng điều phối viên tạo mới — điều phối viên cũ đã có
  khối style đứng sau SOUL). Hệ quả: mọi agent đổi `system_prompt_hash` một lần; session đang resume giữ prompt cũ cho tới khi xoay,
  ghi chú kênh vẫn áp ngay. Test: `tests/test_prompt_style.py`.
- **P0-9 (PR `feat/persist-thinking`)**: `_on_assistant` phát `reasoning.complete` (level `detail`, CÓ ghi DB) cho khối `thinking`
  (text qua `redact`, cắt ở `runtime.claude.thinking_max_chars` = 8000 + cờ `truncated`, giữ `subagent_id`) và `redacted_thinking`
  (`redacted: true`, text rỗng — bản mã hoá không lưu). Khối thinking rỗng (CLI 2.1.310+ thường chỉ gửi chữ ký) bị bỏ qua.
  `runtime.claude.persist_thinking=false` → không phát/không ghi (vẫn có `reasoning.delta` trực tiếp). Web: `present.transcript_item`
  ẩn `reasoning.complete` của subagent như `message.complete`, `redacted` hiện "(suy nghĩ bị ẩn)"; `app.js` thay khối "Suy nghĩ…"
  đang chạy bằng mục `reasoning` (như `msg` thay khối chữ) để không hiện đôi. Lưu ý: fixture thật 2.1.310 có `thinking` rỗng → với
  CLI hiện tại có thể ít khi có nội dung để lưu. Test: fixture `tests/fixtures/claude-thinking.jsonl` (tổng hợp theo dạng thật),
  `tests/test_adapter.py`, `tests/test_persist_thinking.py` (FakeRuntime → DB → `replay_items`), snapshot `docs/schema/event.v1.json`.
