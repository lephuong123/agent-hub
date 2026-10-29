# Chế độ 3D "Văn phòng" (`/office`) — hợp đồng & quyết định

Chủ sản phẩm (2026-10-29): thêm chế độ 3D thể hiện toàn bộ hệ thống; mỗi agent là một nhân vật **chibi** trong văn phòng
(làm việc, đi lại, nghỉ, ngủ, chờ duyệt…). Có **nút chuyển 2D/3D**, lựa chọn lưu ở `localStorage` **cho tới khi người dùng đăng nhập lại**.

## Quyết định
- Three.js **0.169.0** đóng gói sẵn ở `web/static/vendor/three-0.169.0/` (`three.module.min.js` một file, `OrbitControls.js` đã đổi
  `from 'three'` → `from './three.module.min.js'`, `LICENSE` MIT). Không CDN, không importmap (inline bị CSP chặn), không bundler.
- Nhân vật chibi dựng **thủ tục** bằng hình khối Three.js (đầu to, thân nhỏ) + animation thủ tục → không cần asset GLB,
  không vướng giấy phép, **không nới CSP** (không `blob:`, không WASM, không worker). Chữ trên cảnh vẽ bằng `CanvasTexture`.
- Không RPC mới, không migration. Web vẫn là client thuần (RPC `tui.snapshot`, `question.list`, `team.list`, `task.list`, `manager.get`, `hub.status`).
- **Chế độ 3D = TOÀN TRANG 3D** (chủ sản phẩm, 2026-10-29, bổ sung): khi bật 3D thì không còn gì 2D. `/office` là trang ĐỘC LẬP
  (không kế thừa `base.html`: không sidebar, thanh trên, tab đáy, bảng bên, chip HUD, dòng gợi ý). Thấy được chỉ có canvas + **một nút
  "2D" nổi ở góc** (`#office-to-2d`). Mọi thông tin vẽ TRONG cảnh (CanvasTexture + `fillText`): màn hình HUD trên tường bắc, thẻ thông tin
  nhân vật (billboard cạnh nhân vật, camera bay tới), bảng kanban (≤ 5 task/cột), màn hình phòng server (run đang chạy), bảng "Cần bạn" ở
  lối vào (approval + câu hỏi) + thẻ đỏ trên đầu agent có approval, biển "hết phiên"/"mất kết nối". Danh sách chữ chỉ cho trình đọc màn hình
  (`sr-only`). Ngoại lệ DUY NHẤT dùng chữ DOM: không có WebGL / quá chậm (`#office-fallback`, tối thiểu).
- Cảnh 3D **không có link sang trang 2D** (bấm vật thể chỉ để camera tập trung/giải thích, không bao giờ điều hướng tới `/agents`, `/runs`…).
  Thao tác ghi (duyệt/dừng) chưa làm; khi làm thì PHẢI qua `ACTIONS` + `post_guard` + xác nhận như phần còn lại.

## Nút chuyển chế độ (localStorage)
- Khoá `cfh_view`: `"3d"` = chế độ 3D; không có = 2D.
- `early.js` (chạy trong `<head>`):
  - Nếu `location.hash` bắt đầu bằng `#k=` (vừa đăng nhập) → `removeItem("cfh_view")` và KHÔNG chuyển trang (giữ fragment cho app.js lấy khoá).
  - Ngược lại nếu `cfh_view === "3d"` và trang KHÁC `/office` và `/login` → `location.replace("/office")` (mọi trang, không chỉ `/`).
  - Gắn class `view-3d` lên `<html>` khi đang ở chế độ 3D.
- Đăng xuất (`/logout`) cũng xoá `cfh_view` (app.js, cùng chỗ xoá `ahub_k`).
- Nút `#view-toggle` ở thanh bên (và header màn nhỏ) của trang 2D: bật 3D → set `cfh_view` + đi `/office`.
  `aria-pressed` phản ánh trạng thái. Không JS → nút là link thường tới `/office`.
- Trên `/office`: nút "2D" nổi (`#office-to-2d`, link thường tới `/`) → `removeItem("cfh_view")` rồi về `/`.

## Server (`web/office.py`)
### `GET /office`
Trang `office.html` — tài liệu ĐỘC LẬP (`<html>/<head>` riêng: charset, viewport, color-scheme, favicon, `office.css`, script module
`office.js`; không script/style inline). Chưa đăng nhập → 303 `/login` (GuardMiddleware có sẵn).

### `GET /office/state` → JSON (401 nếu chưa đăng nhập; `Cache-Control: no-store`)
```json
{
  "epoch": "e-…", "last_seq": 123,
  "hub": {"estop": false, "running": 1, "queued": 0, "rate_limited": false, "approvals_waiting_human": 0, "cost_today_usd": 0.42},
  "manager_id": "dieu-phoi",
  "teams": [{"id": "toa-soan-ia", "name": "toa-soan-ia"}],
  "agents": [{
    "id": "lap-trinh", "name": "Lập trình viên", "role": "Software engineer",
    "team": "toa-soan-ia", "team_role": "worker",
    "status": "working",          // working|waiting|waiting_human|asking|idle|error|paused
    "tool": "Bash",               // TÊN tool cuối (không input/preview), null nếu không có
    "task": "Sửa lỗi đăng nhập",  // dòng đầu, ≤ 80 ký tự — DỮ LIỆU agent, client chỉ textContent/fillText
    "run_id": "r_…",              // run đang chạy/xếp hàng hoặc null
    "approvals": 0,
    "is_manager": false
  }],
  "task_counts": {"ready": 2, "running": 1, "review": 0, "blocked": 0},
  "tasks": {"ready": [{"id": "tk_…", "title": "≤ 60 ký tự", "assignee": "lap-trinh|null"}], "running": […], "review": […], "blocked": […]},
  "approvals": [{"id": "ap_…", "agent": "lap-trinh", "tool": "Bash", "klass": "fs_delete|null", "status": "open|waiting_human",
                 "wait_s": 312, "preview": "≤ 120 ký tự, một dòng"}],
  "questions": [{"id": "q_…", "agent": "kiem-thu", "status": "open|with_manager", "text": "≤ 120 ký tự, một dòng"}],
  "runs": [{"id": "r_…", "agent": "lap-trinh", "started_at": "ISO", "cost_usd": 0.12}]
}
```
Agent thuộc nhiều team → lấy team đầu tiên (theo `team.list`). `asking` = có câu hỏi `question.list{pending}` (như `overview_data`).
- `tasks`: `task.list{status:[ready,running,review,blocked], limit:200}` (sắp `priority DESC, created_at ASC`) → tối đa 5 task mỗi cột
  (`TASK_TOP`); lỗi/daemon cũ → cột rỗng. `task_counts` vẫn là tổng theo `team.list[].counts`.
- `approvals`: `tui.snapshot.approvals` (đang chờ), ≤ 20. `preview` là `action_preview` ĐÃ CHE của approval, cắt còn ≤ 120 (`PREVIEW_MAX`).
- `questions`: `question.list{pending}` (open + with_manager), ≤ 20, `text` ≤ 120. Không gửi lựa chọn/câu trả lời.
- `runs`: các hàng `tui.snapshot.agents` có `current_run_id` + `run_started_at` (`cost_usd` = `run_cost_usd` ước tính).
- **Mọi chữ tự do bị cắt Ở SERVER** (`one_line`: một dòng, gộp khoảng trắng, bỏ ký tự điều khiển/định dạng/bidi, thêm "…") và client chỉ vẽ
  bằng `fillText`. id/tool/klass chỉ `[A-Za-z0-9_\-:.]`; `status` chỉ từ khoá `[a-z_]`.

### `GET /sse/office?since=<seq>&epoch=<epoch>` → `text/event-stream`, mỗi `data:` là JSON
- Dùng lại `_stream()` của `app.py` (`level=summary`, toàn hub). Thiếu `since` → `hub.status.last_seq`.
- Khung: `{"t":"hello"}`, `{"t":"end","reason":…}`, comment `: ping` mỗi 15 s, và
  `{"t":"ev","seq":n,"type":"tool.start","agent":"id|null","team":"id|null","tool":"Bash|null","status":"…|null","outcome":"…|null"}`
  `{"t":"refresh"}` — client nên tải lại `/office/state` (gộp/debounce).
- Danh sách trắng loại event (khác → bỏ): `agent.status`(status=payload.status), `run.queued|started` , `run.completed|failed|cancelled|interrupted|waiting_human`
  (CHỈ `source=="hub"`; outcome=payload.outcome), `tool.start|tool.complete` (tool=payload.name; complete+is_error → status `"error"`),
  `subagent.spawn`, `approval.open|waiting_human|resolved|delegated|escalated`, `question.open|answered|cancelled|escalated`,
  `task.status` (status=payload.to), `team.estop`, `hub.estop` (status `"on"`/`"off"`), `schedule.fired`, `manager.action` (tool=payload.tool),
  `ratelimit.throttle`, `learning.applied|learning.rolled_back`, `guard.blocked`, `steer.ack`.
- `refresh` sau: `agent.status`, mọi `run.*` kết thúc/started, `approval.*`, `question.*`, `hub.estop`, `team.estop`, `task.status`.
- **KHÔNG BAO GIỜ** đưa chữ tự do vào khung: không `text`, `input`, `args_preview`, `result_preview`, `preview`, `prompt_preview`, `summary`, `reason`.
  Chỉ id/tên tool/trạng thái theo danh sách trên (tên tool cắt ≤ 64 ký tự, chỉ `[A-Za-z0-9_\-:.]`).

## Client (`office.html` + `static/office.js` + `static/office/*.js` + `static/office.css`)
DOM do template cung cấp (thấy được: chỉ canvas + nút "2D"):
- `#office` (data-state="/office/state", data-sse="/sse/office") chứa `#office-canvas` (vùng vẽ, toàn khung nhìn).
- `#office-to-2d` — nút tròn "2D" nổi góc phải trên (link `/`; JS xoá `cfh_view` trước khi đi).
- `#office-fallback` (hidden) — chữ DOM tối thiểu khi không có WebGL/FPS quá thấp (nút "2D" vẫn ở góc).
- Khối `.sr-only` (KHÔNG hiển thị, cho trình đọc màn hình): `#office-hud` (role=status, dòng trạng thái hub), `#office-expired` (hidden
  trừ khi 401), `#office-panel` (aria-live: mô tả nhân vật/bảng đang xem), `#office-list` (aria-live; mỗi agent một `<button>` — Enter →
  camera bay tới + thẻ 3D) + các nút "Toàn cảnh / Xem màn hình trạng thái / bảng công việc / màn hình phòng server / bảng Cần bạn".

Trong cảnh (module `static/office/boards.js`):
- Màn hình HUD lớn trên tường bắc: đang chạy, xếp hàng, chờ bạn duyệt, chi phí hôm nay, ESTOP, giới hạn tốc độ, kết nối, đếm task.
- Bấm nhân vật → thẻ thông tin (sprite cạnh nhân vật; màn dọc: phía trên): tên, id, trạng thái tiếng Việt, vị trí, vai trò, team,
  task, tool, run id, approval đang chờ. Camera bay mượt tới và bám theo khi nhân vật đi. Esc / bấm sàn trống → về toàn cảnh.
- Bảng kanban (tường tây), màn hình phòng server, bảng "Cần bạn" (lối vào), màn hình HUD: bấm → camera bay tới nhìn thẳng (biển phòng/
  team/nhãn tên tạm ẩn để không che chữ).
- Thẻ đỏ (cam nếu chưa tới người) trên đầu agent có approval: tool + [klass] + preview.
- 401 → biển đỏ "Phiên đăng nhập đã hết — chạy cfharness web --open" trong cảnh (chưa có văn phòng thì dựng trên một khoảnh sàn),
  ngừng SSE. Mất kết nối > 3 s → biển cam "Mất kết nối" (và HUD/`#office-hud` báo), tự kết nối lại.

Bố cục văn phòng & hành vi: xem phần "Ánh xạ" bên dưới. Hiệu năng: dừng vẽ khi `document.hidden`, ≤ 30 FPS, `pixelRatio ≤ 1.5`,
`prefers-reduced-motion` → dịch chuyển tức thời, không lắc lư.

## Ánh xạ trạng thái → nhân vật
| Trạng thái / event | Nhân vật |
|---|---|
| `working` | ngồi bàn của mình (cụm bàn theo team), gõ phím; màn hình sáng |
| `tool.start` Bash | đi sang **phòng server**, đứng gõ ở tủ rack; `tool.complete` → về bàn |
| Read/Grep/Glob/WebFetch/WebSearch | đi sang **thư viện** (giá sách) |
| Edit/Write/NotebookEdit | ở bàn, gõ nhanh |
| `mcp__*` | ở bàn, bong bóng 🔌 |
| `subagent.spawn` | bong bóng 👥 |
| `waiting` | đứng ở bàn, bong bóng 🛡 |
| `waiting_human` / `approval.waiting_human` | đứng **giơ tay**, vòng đỏ dưới chân, bong bóng ✋ |
| `approval.delegated` | đi tới **phòng điều phối viên** |
| `asking` / `question.open` | bong bóng ❓ |
| `idle` | đi lang thang giữa **pantry** (uống cà phê ☕) và **sofa** (ngồi nghỉ) |
| `paused` | ngủ trên sofa/bàn, 💤 |
| `error` / outcome lỗi | ngồi ôm đầu, ⚠ |
| `rate_limited` / `ratelimit.throttle` | ngồi **hàng ghế chờ** ⏳ |
| `queued` | đi về bàn |
| `task.status` review | **phòng họp** |
| `learning.*` | bong bóng 💡 ngắn |
| `schedule.fired` | đồng hồ treo tường nháy + bong bóng ⏰ ở agent đích |
| `manager.action` | điều phối viên ở phòng riêng, bong bóng 📋 |
| `hub.estop` on | đèn đỏ toàn văn phòng, mọi người đứng im |
| `team.estop` | đèn đỏ ở cụm bàn team đó |
| `guard.blocked` | bong bóng ⛔ ngắn |

## Giai đoạn 2 — duyệt + chat NGAY TRONG 3D (chủ sản phẩm 2026-10-29)
Mọi thao tác ghi từ cảnh 3D đi qua CÙNG cổng với web 2D: `post_guard` (Origin/Sec-Fetch-Site, CSRF, khoá phiên `k`), audit kênh `web`,
xác nhận + bước 2. Không có đường ghi nào "nhẹ" hơn 2D.

### Xác thực phía client
- `office.html` gắn `data-csrf="{{ csrf }}"` trên `#office`. Khoá `k` đọc từ `localStorage["ahub_k"]` (KHÔNG in, không vẽ, không log).
- Mọi POST: `fetch(url, {method:"POST", credentials:"same-origin", headers:{"X-Ahub-Fetch":"1"}, body: URLSearchParams{csrf, k, …}})`.

### Thao tác: `POST /office/do/{name}` → JSON
- `name` ∈ danh sách trắng `OFFICE_ACTIONS` = `approve`, `deny`, `approve_always`, `approve_rule`, `answer` (dùng đúng `Action` trong
  `app.ACTIONS`). Tên khác → 404.
- Logic cổng (confirm → bước 2 heavy/always/grant) TÁCH khỏi `do_action` thành hàm dùng chung `plan_action(request, name, form)`
  trả "quyết định"; `do_action` (2D) vẽ `confirm.html`/`approve2.html`/redirect như cũ, `/office/do` trả JSON:
  - `{"need":"confirm","message":str,"danger":bool,"note_label":str|null}` — khi chưa có `confirm=yes`
  - `{"need":"confirm2","kind":"heavy"|"always"|"grant","view":{tool,text,lines,hidden_chars},"always":<always_offer>|null,
     "grant_for":["","8h","24h","7d","30d"]|null}` — khi cần bước 2 và chưa có `confirm2=yes`
  - `{"ok":true,"message":str}` | `{"ok":false,"error":str}` (HTTP 400/403/404/503)
- Client gửi lại cùng trường + `confirm=yes`, rồi `confirm2=yes` (+ `note`, `grant_for`). Không bao giờ tự gửi `confirm*=yes` khi người chưa bấm.

### Đọc chi tiết
- `GET /office/approval/{id}` → `{id, agent, status, routed_to, tool, klass, wait_s, view:{tool,text (ĐẦY ĐỦ — present.action_view),lines,
  hidden_chars}, always_offer: bool, can_rule: bool (matched_rule), heavy: bool}`. Panel duyệt PHẢI hiện toàn bộ `view.text` (cuộn được)
  trước nút "Cho phép"; `hidden_chars` → cảnh báo đỏ.
- `GET /office/question/{id}` → `{id, agent, status, text (đầy đủ), options:[{key,label}], allow_text: bool}`.

### Chat với điều phối viên
- `GET /office/chat?c=<conv>` → `{"available":bool,"manager_id":str|null,"conversation_id":str|null,
  "conversations":[{id,title,updated_at}] (kênh web), "messages":[{kind:"user"|"manager"|"action"|"note"|"question", text, ts, run_id, ok?}] (≤ 50 gần nhất)}`.
- Gửi: `POST /manager/send` sẵn có với `X-Ahub-Fetch: 1` (trả `conversation_id`, `run_id`, `steered`; bỏ qua các trường `html_*`).
- Live: `GET /sse/office/chat?c=<conv>` → JSON: `{"t":"delta","run","text"}`, `{"t":"reply","run","conv","text","notify"}`,
  `{"t":"action","run","tool","summary","ok"}`, `{"t":"question","conv","id"}`, `{"t":"run_end","run","outcome"}`, `{"t":"tick"}`
  (mirror `web/manager._converter` nhưng KHÔNG HTML). Chữ ở đây là DỮ LIỆU agent → client chỉ `fillText`.
- Đăng xuất trong 3D: POST `/logout` (csrf) rồi xoá `ahub_k` + `cfh_view` → `/login`.

### Giao diện 3D
- Bấm nhãn đỏ trên đầu agent / mục trong bảng "Cần bạn" / nút "Duyệt" trên thẻ nhân vật → **bảng duyệt 3D** (billboard lớn trước camera):
  toàn bộ thao tác (cuộn), nút vẽ trên canvas, raycast + bàn phím (Tab/Enter/Esc): "Cho phép một lần", "Từ chối" (ô ghi chú),
  "Luôn cho phép" (chỉ khi `always_offer`), "Luôn cho phép theo luật" (khi `can_rule`, chọn thời hạn). Bước xác nhận + bước 2 cũng là bảng 3D,
  bước 2 nêu rõ luật sẽ tạo. Kết quả → nhân vật hạ tay / pop ✅❌.
- Câu hỏi → bảng 3D: chữ câu hỏi + nút phương án + ô trả lời tự do.
- Chat → **màn hình lớn ở phòng điều phối viên**: camera bay tới, lịch sử (cuộn), dòng nhập vẽ 3D; gõ qua một `<textarea>` ẩn (sr-only, nhận
  focus, IME tiếng Việt chạy được); Enter gửi, Shift+Enter xuống dòng; chữ chạy theo delta; nhân vật điều phối viên có bong bóng nói.
  "Hội thoại mới" + chọn hội thoại cũ trên màn hình.
- Cửa ra vào "Thoát" = đăng xuất (xác nhận 3D).
- Mọi nút 3D có bản sao `<button>` sr-only trong danh sách ẩn (trình đọc màn hình).
