# Luồng WEB — giao diện trình duyệt (`cfharness web`)

Nguồn: `docs/plan/web-telegram.md` (quyền sở hữu, nguyên tắc), BRD FR-UI-01..10, FR-SG-04/11, Q-11. Ảnh: `docs/screenshots/web/`.

## 1. Kiến trúc
| File | Vai trò |
|---|---|
| `web/security.py` | Chỉ loopback (`require_loopback`), link đăng nhập một lần (HMAC khoá RAM + `HUB_HOME/auth/web.json` 0600, dir 0700), phiên RAM (cookie `ahub_web` HttpOnly/SameSite=Strict/Path=/, idle 12 h, tối đa 7 ngày), CSRF theo phiên, `allowed_hosts` |
| `web/app.py` | Starlette: `GuardMiddleware` ASGI thuần (Host → 421, chưa đăng nhập → 303 /login hoặc 401, header CSP/XFO/no-referrer/no-store; không đệm nên SSE chạy); trang; `POST /do/<action>` bảng `ACTIONS`; SSE |
| `web/present.py` | Thuần: nhãn tiếng Việt (agent/run/task/lý do chờ), `transcript_item(ev)` (event → mục), `activity_item` (dùng lại `tui.model.activity_line`), `md()` an toàn (markdown-it, HTML thô bị escape, link chỉ http/https/mailto) |
| `web/cli.py` | `cfharness web [--host] [--port 8765] [--open] [--url]` — đã có server chạy thì chỉ cấp link mới |
| `web/demo.py` | `python -m cfharness.web.demo --home <tmp> --port 8799`: daemon thật + FakeRuntime + dữ liệu mẫu (run giữ, approval waiting_human) — dùng chụp ảnh |
| `web/templates/`, `web/static/` | Jinja2; `htmx-2.0.4.min.js` vendored (sha384 `HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+`, 0BSD), `app.css`, `app.js` (hộp xác nhận, EventSource) |

Web là CLIENT thuần của daemon (`tui.client.AsyncHubClient(channel="web")`), không đọc/ghi `hub.db`. Không thêm RPC, không migration.
RPC dùng: `tui.snapshot`, `approval.*`, `run.*` (start/get/list/stop/steer/continue/resume), `agent.*`, `template.list`, `mcp.*`,
`task.*`, `team.*`, `learn.feed|ledger|show|rollback|policy_get|policy_set`, `knowledge.pin`, `golden.*`, `digest.*`, `report.cost|get`,
`audit.list`, `estop.*`, `config.get|set`, `secret.*`, `notify.test`, `guard.integrity.status`, `hub.status`, `events.subscribe`.

Thao tác ghi: form POST (không cần JS). `ACTIONS[name]` = method RPC + dựng params từ form + thông điệp + `confirm`. Thao tác nhạy cảm
(approve/deny, stop, xoá/tạm dừng/revert agent, ESTOP, config, secret rm, rollback, MCP rm/import, archive…) cần `confirm=yes`: có JS
→ `<dialog>` (kèm ô ghi chú khi cần); không JS → server trả trang xác nhận. Live: `GET /sse/run/<id>?since=&epoch=` (level detail,
`until_run_end`) gửi JSON `{t: item|delta|stats|refresh|end}`; mục đã vẽ sẵn HTML phía server (cùng macro với phần phát lại từ DB);
heartbeat 15 s; client đóng tab → subscription đóng (run không ảnh hưởng). Tổng quan + thanh run làm mới bằng htmx `every 2s`.

## 2. Bảo mật — đã làm + test (`tests/test_web.py`, 26 test)
- Bind chỉ loopback (`test_bind_loopback_only`, CLI trả 1). Host header phải là `cfharness.localhost:port` → chống DNS rebinding; 127.0.0.1/localhost → 303 sang host riêng, không đặt cookie (`test_host_header_must_be_web_host`).
- Không phiên → 303 /login (trang) / 401 (partials, SSE, POST); cookie giả vô hiệu (`test_unauthenticated_requests_are_blocked`).
- Link một lần: dùng lại / chữ ký sai / hết hạn → 401; cookie HttpOnly + SameSite=Strict; đăng xuất huỷ phiên (`test_one_time_login_link…`).
- CSRF + Origin/Referer/Sec-Fetch-Site cho mọi POST (`test_csrf_and_origin_required_for_writes`); CSP `script-src 'self'` (không inline,
  không eval — htmx `allowEval:false`), không header CORS, không CDN (`test_security_headers_and_no_cors`).
- Token người (`<socket>.auth`) và khoá ký link không bao giờ có trong HTML/header/cookie/log/audit (`test_human_token_never_in_html_or_logs`);
  giá trị secret đặt qua web không hiện lại, không vào audit.
- HUMAN_ONLY qua web ghi `audit_log` `channel=web, actor=user`: approval.resolved, estop.set, config.set, secret.set, agent.*,
  trash.restore, learn.policy_set, run.*, team/task (`test_human_only_admin_actions_audited_as_web`, `test_approve_deny…`). Method chỉ-người
  mà daemon không tự audit theo `_audit` (guard.protect.*, integrity.accept, run.takeover) → web tự gọi `audit.record` (hiện chưa có trang nào dùng).
- `config.set` từ web chỉ cho danh sách khoá an toàn `SAFE_CONFIG` (guard/sandbox… phải dùng CLI).

## 3. Agent trong run có vào được web không? — đã kiểm THẬT
Spike `claude` 2.1.302 + haiku (~$0.02), settings = `guard.runtime.sandbox_settings` thật, server demo trên 127.0.0.1:8799:

| Trong Bash của agent | Kết quả |
|---|---|
| `curl http://127.0.0.1:8799/login` (qua proxy sandbox) | `000` — không tới được |
| `curl --noproxy '*' …` (thẳng TCP) | `Failed to connect … 0 ms` |
| python `socket.create_connection(('127.0.0.1', 8799))` | `Operation not permitted` |
| `cat HUB_HOME/auth/web.json` | bị chặn (`denyRead` `HUB_HOME/auth`) |
| `curl https://example.com` | 200 (mạng ngoài vẫn chạy) |

Kết luận: sandbox Seatbelt chặn mọi kết nối localhost từ Bash/hậu duệ. Lớp tối thiểu khi không có sandbox (tắt, Linux thiếu bwrap
mà `fail_if_unavailable: false`) hoặc tool không sandbox (WebFetch chạy trong tiến trình `claude`, MCP server): không có cookie/link →
mọi request bị 303/401; link chỉ ký bằng khoá trong `HUB_HOME/auth` (sandbox denyRead + `permissions.deny` Read/Grep/Glob `auth/**`);
lệnh `cfharness web …` từ agent bị classifier `forbidden` (`HUB_HUMAN_ONLY_CLI["web"]`) (`test_agent_in_run_cannot_reach_web_secrets`).
Giới hạn còn lại: không sandbox + agent đọc được `auth/web.json` (phải lách classifier) → tự ký link → có phiên web = quyền người;
tương đương rủi ro token người đã ghi ở `hardening-SEC.md` §3.

## 3b. Sửa theo kiểm toán `docs/acceptance/security-web-telegram.md` (WT-1, WT-2/6/7 phần web, BUG waiting_human)
**WT-1 (cookie không tách theo cổng → app localhost duyệt thay người).** Nhiều lớp:
1. Host riêng `cfharness.localhost:<port>` (mặc định; `--host-name` đổi). Cookie host-only → KHÔNG gửi tới `127.0.0.1:*`/`localhost:*`.
   Chrome/Firefox/Safari tự phân giải `*.localhost`; resolver macOS cũng vậy (`getaddrinfo` → ::1, 127.0.0.1 — đã kiểm). Server vẫn bind 127.0.0.1.
2. Khoá phiên NGOÀI cookie (`Session.key`, 256 bit): trao MỘT lần qua fragment `/#k=…` của redirect đăng nhập (fragment không lên server,
   không vào log/Referer), `app.js` cất vào `localStorage` của origin web (origin khác — kể cả `cfharness.localhost:<cổng khác>` — không
   đọc được) và gắn `k` vào MỌI form POST; server từ chối 403 nếu thiếu/sai (`post_guard`). HTML không bao giờ chứa khoá (trang xác nhận
   mang trường form qua `carry()` đã loại `k`/`csrf`). Hệ quả: thao tác ghi cần JavaScript; tab/trình duyệt khác chưa có khoá → đăng nhập lại.
3. Phiên ngắn: idle 1 h, tối đa 12 h; polling `/partials/*` và SSE KHÔNG gia hạn (`test_wt1_short_session_polling_does_not_extend`).
4. `__Host-` prefix: không dùng — bắt buộc `Secure` (chỉ HTTPS; trình duyệt không nhất quán với http://*.localhost).
5. Tái hiện QA chạy lại (Chromium thật + `evil_devserver.py` nguyên bản ở :8800, và biến thể mạnh nhất ở `cfharness.localhost:8801`):
   bản gốc nhận cookie RỖNG (host-only); biến thể cùng tên host (trình duyệt gửi cookie vì không tách cổng) đọc được CSRF nhưng
   `POST /do/approve` → **403**, approval vẫn `waiting_human`; người bấm trong trình duyệt → `allowed`. Test hồi quy: `test_wt1_stolen_cookie_and_csrf_cannot_write`.
6. Sửa kèm: `Referrer-Policy: same-origin` (với `no-referrer` Chrome gửi `Origin: null` cho form POST → 403 với người thật — phát hiện khi chạy trình duyệt thật).

**Mô hình đe doạ còn lại.** (a) Tiến trình ngoài sandbox cùng user đọc được profile trình duyệt (cookie + localStorage trên đĩa) hoặc
gắn debugger vào trình duyệt → có phiên; cũng đọc được token người — ngang mức đã chấp nhận ở hardening-SEC §3. (b) XSS trên origin web
lộ khoá trong localStorage — giảm bằng CSP `script-src 'self'`, autoescape, markdown không HTML. (c) Trang độc gửi người tới
`cfharness.localhost:<cổng khác>` để thu cookie: cookie đó vô dụng khi thiếu khoá; còn GET đọc được (xem trạng thái, prompt, kết quả run).
(d) `--host-name 127.0.0.1` (dự phòng cho trình duyệt không phân giải `*.localhost`) mất lớp 1, còn lớp 2.

**WT-2.** `present.action_view`: hiện TOÀN BỘ thao tác (Bash: cả lệnh + tham số khác; tool khác/MCP: mọi tham số JSON), không cắt; khung
cuộn, nhãn "lệnh dài N ký tự", tô `<mark>` phần nguy hiểm (rm/-rf/dd/git push --force/DROP/>…), ký tự ẩn/bidi/điều khiển hiện `⟦U+202E⟧`
+ cảnh báo; hộp xác nhận chứa toàn bộ lệnh (`test_wt2_full_action_shown_and_dangerous_part_marked`).
**Bước 2 cho thao tác nặng** (nhất quán Telegram): `notify.telegram.is_heavy` với `confirm_classes` + `file`, hoặc tài nguyên bảo vệ →
sau hộp xác nhận còn trang `approve2.html` (toàn bộ lệnh + ô tích "đã đọc") (`test_heavy_approval_needs_second_step`).
**WT-6.** Xác nhận cho `mcp_add` (hiện lệnh sẽ CHẠY ngay ngoài sandbox), `mcp_restore`, `secret_set` (ô tích "ghi đè"; trang xác nhận không
mang giá trị secret), `agent_resume`, `agent_restore`, `team_member`, `golden_restore`; tạo agent luôn xác nhận; lưu agent xác nhận khi đổi
permission_mode/tools/MCP; `permission_mode` ∉ `PERMISSION_MODES` (vd bypassPermissions) → 400 (`test_wt6_sensitive_forms_need_confirmation`).
**WT-7.** `safe_next`: chỉ `^/` + bảng ký tự an toàn, cấm `//`, `/\`, ký tự điều khiển/khoảng trắng (`test_wt7_safe_next_rejects_open_redirects`).
**BUG "luật ?".** `approvals.to_waiting_human` thêm `tool, matched_rule, destructive_class, reason, protected_resource_id` vào payload; template bỏ ":" khi không có tool
(`test_waiting_human_event_carries_rule_and_page_has_no_dangling_colon`). Ảnh chụp lại: `03-run-cho-duyet.png`, `07-duyet.png`, `15-hop-xac-nhan.png`.

## 3c. Ngoại lệ sandbox localhost theo agent (SEC §8)
Nhãn đỏ `⚠ localhost` (tooltip: agent được mở cổng và gọi dịch vụ trên máy này; chỉ người tắt được) cạnh tên agent ở Tổng quan
(`tui.snapshot` → `sandbox_localhost`), danh sách Agent (`agent.list` → `sandbox_overrides`) và trang chi tiết (`guard.sandbox.get`).
Trang chi tiết có khung "Sandbox: truy cập localhost" + nút bật/tắt → action `sandbox_localhost` = RPC `guard.sandbox.set`
(HUMAN_ONLY, hộp xác nhận nêu rủi ro, cần khoá phiên WT-1; service tự audit `guard.sandbox_override` với `channel=web`).
Test: `test_sandbox_localhost_badge_and_toggle`.

## 4. File dùng chung đã chạm (chỉ THÊM)
- `pyproject.toml`/`uv.lock`: `jinja2>=3.1`, khai báo rõ `starlette>=0.40`, `uvicorn>=0.30` (đã có gián tiếp qua `mcp`).
- `cli.py`: đăng ký `web.cli.add_parsers` cuối `build_parser`.
- `guard/runtime.py` (`secret_read_deny_rules`): thêm `HUB_HOME/auth/**`. `guard/classifier.py`: `HUB_HUMAN_ONLY_CLI["web"] = None`.
- `approvals.py` (`to_waiting_human`): thêm khoá vào payload `approval.waiting_human` (BUG kiểm toán).
- Không đổi `auth.py`/`daemon.py`: tiến trình web là tiến trình của NGƯỜI, đọc token như CLI — kênh `web` đi qua `_audit.channel`.

## 5. Kiểm chứng
- `uv run pytest -q`: xanh (gồm 27 test web, ~4 s).
- Server thật (`web.demo`, daemon trên socket + FakeRuntime) + Playwright Chromium headless: 22 ảnh desktop 1280 (sáng + tối) và
  mobile 390 px; đã xem lại từng ảnh; trên 390 px `scrollWidth == clientWidth` (không tràn ngang); console không lỗi CSP/JS.
  Tạo lại: chạy `web.demo`, rồi script Playwright (đăng nhập bằng link ký từ `auth/web.json`, chụp các trang).

## 6. Lệch / việc dở
- Chưa có: trang guard policy/protect (dùng CLI `cfharness guard`), purge vĩnh viễn, sửa golden checker, journey học, canary chi tiết,
  takeover chạy từ web (chỉ hiện lệnh `cfharness takeover <run>` — cần terminal tương tác), nhập MCP có bước xem trước (hiện nhập thẳng sau xác nhận).
- Phiên lưu RAM: khởi động lại web → đăng nhập lại. Một server web/HUB_HOME (`auth/web.json`).
- Reasoning chỉ hiện khi xem live (delta không lưu DB) — giống TUI.


## UX v2 (2026-10-19) — kế hoạch `docs/dev-notes/ux-plan.md` (pha W-Q, W-A, W-B, W-C)
Ảnh: `docs/screenshots/web/v2/` (sáng, `-toi`, `-mobile` 390 px, `-tablet` 900 px). Test: `tests/test_web_questions.py` (+ cập nhật `test_web.py`).

**W-Q — câu hỏi của agent trên web.** Macro `question_card(q, next, compact)` (`_macros.html`): chữ agent (câu hỏi, nhãn, mô tả, ghi chú
của điều phối viên) autoescape + `pre-wrap`; lựa chọn = radio/checkbox có `<label>`; ô "câu trả lời khác" nếu `allow_free_text`.
`compact` (dải Cần bạn ở Tổng quan — khung tự làm mới 3 s) KHÔNG có ô nhập: mỗi lựa chọn là một nút một-chạm, "Trả lời bằng chữ…" dẫn sang
`/inbox`. Câu `with_manager`: chỉ hiện lựa chọn + "Tự trả lời" (`question_take` → `question.escalate`). `ACTIONS`: `question_answer`
(không hộp xác nhận), `question_take`, `question_cancel` (xác nhận) — đều qua `post_guard` (CSRF + khoá phiên WT-1), `channel=web`; daemon tự audit.
Hiện ở: `/inbox`, Tổng quan, trang run (MỘT chỗ, ngoài khung tự làm mới), trang task, luồng chat Điều phối (cả qua SSE: `manager.py` dựng
thẻ từ payload `question.*`, module macro dựng bằng `make_module({"csrf"})`). Badge menu = approval chờ + câu hỏi `open`. Agent có câu hỏi chờ
→ trạng thái "Chờ bạn trả lời" (`asking`).

**W-A — hệ thiết kế.** `app.css` viết lại: token sáng/tối (thêm `--ask` cho câu hỏi, `--on-ok`), nút `primary|ok|danger|ghost` + cỡ `sm|lg|icon`,
`pill` có icon, `xcard` (thẻ duyệt/câu hỏi), lưới thẻ agent, timeline run, chat. Sprite icon tĩnh `static/icons.svg` (vẽ tay, `<use href>`,
macro `icon()`); UI web không còn emoji. Toast góc dưới: tự ẩn bằng CSS animation (không cần JS), nút đóng bằng JS. `<time data-rel>` →
"x phút trước" (app.js, 30 s). Ô chat/steer tự giãn; Enter gửi (Shift+Enter xuống dòng, bỏ qua khi đang gõ dấu IME).

**W-B — bố cục.** Sidebar có nhóm (Làm việc · Đội ngũ · Học & chất lượng · Hệ thống; đáy: daemon, ESTOP, Thoát); 761–1100 px chỉ icon;
≤ 760 px header gọn + tab đáy 5 mục (Điều phối · Cần bạn · Agent · Lượt chạy · Thêm — sheet `<details>`). Trang mới `/inbox` "Cần bạn"
(câu hỏi chờ bạn, thao tác chờ duyệt, câu hỏi đang ở điều phối viên, task bị chặn không có câu hỏi, câu đã trả lời gần đây); `/approvals` giữ
nguyên (lịch sử), thuộc mục "Cần bạn" trên menu.

**W-C — từng màn.** Tổng quan: 4 thẻ số liệu, dải Cần bạn (≤ 3 thẻ), lưới thẻ agent (bỏ cột "công cụ gần nhất"), "Hoạt động" chỉ còn sự kiện có
nghĩa với câu tiếng Việt do hub sinh (`present.activity_text` — bỏ tool/lời agent từng bước/run.started). Run: header chip, approval MỘT mục trong
diễn biến (dom `ev-ap-<id>`: open → waiting_human → resolved thay tại chỗ, cả replay lẫn SSE), mục câu hỏi `ev-q-<id>`, ô chỉ đạo dính đáy.
Điều phối: 3 cột (danh sách cuộc trò chuyện · chat · trạng thái), thẻ hành động liên tiếp của một lượt gom thành "Đã làm N việc" (`_timeline`; SSE
gộp vào khối cuối cùng lượt), chip gợi ý. Danh sách run/agent: tiêu đề task thay prompt thô (`present.run_title`), lọc nhanh dạng chip.
Agent: header + menu "⋯" (Tạm dừng/Xoá). Empty state có icon + hướng dẫn.

Đổi hành vi/kỳ vọng test có chủ đích: nhãn localhost là `lh-badge` (không còn ký tự ⚠); "Hoạt động" không còn dòng `tool.start`.
Chưa làm: gom nhóm nhiều lời gọi tool liên tiếp trên trang run; form agent nhiều bước; tab trên trang agent; trang Tự học/MCP/Chi phí/Nhật ký
chỉ được tô lại bằng CSS mới (bố cục cũ).
