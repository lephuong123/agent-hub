# Luồng WEB — giao diện trình duyệt (`agent-hub web`)

Nguồn: `docs/plan/web-telegram.md` (quyền sở hữu, nguyên tắc), BRD FR-UI-01..10, FR-SG-04/11, Q-11. Ảnh: `docs/screenshots/web/`.

## 1. Kiến trúc
| File | Vai trò |
|---|---|
| `web/security.py` | Chỉ loopback (`require_loopback`), link đăng nhập một lần (HMAC khoá RAM + `HUB_HOME/auth/web.json` 0600, dir 0700), phiên RAM (cookie `ahub_web` HttpOnly/SameSite=Strict/Path=/, idle 12 h, tối đa 7 ngày), CSRF theo phiên, `allowed_hosts` |
| `web/app.py` | Starlette: `GuardMiddleware` ASGI thuần (Host → 421, chưa đăng nhập → 303 /login hoặc 401, header CSP/XFO/no-referrer/no-store; không đệm nên SSE chạy); trang; `POST /do/<action>` bảng `ACTIONS`; SSE |
| `web/present.py` | Thuần: nhãn tiếng Việt (agent/run/task/lý do chờ), `transcript_item(ev)` (event → mục), `activity_item` (dùng lại `tui.model.activity_line`), `md()` an toàn (markdown-it, HTML thô bị escape, link chỉ http/https/mailto) |
| `web/cli.py` | `agent-hub web [--host] [--port 8765] [--open] [--url]` — đã có server chạy thì chỉ cấp link mới |
| `web/demo.py` | `python -m agent_hub.web.demo --home <tmp> --port 8799`: daemon thật + FakeRuntime + dữ liệu mẫu (run giữ, approval waiting_human) — dùng chụp ảnh |
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

## 2. Bảo mật — đã làm + test (`tests/test_web.py`, 19 test)
- Bind chỉ loopback (`test_bind_loopback_only`, CLI trả 1). Host header phải là loopback:port → chống DNS rebinding (`test_host_header_must_be_loopback`).
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
lệnh `agent-hub web …` từ agent bị classifier `forbidden` (`HUB_HUMAN_ONLY_CLI["web"]`) (`test_agent_in_run_cannot_reach_web_secrets`).
Giới hạn còn lại: không sandbox + agent đọc được `auth/web.json` (phải lách classifier) → tự ký link → có phiên web = quyền người;
tương đương rủi ro token người đã ghi ở `hardening-SEC.md` §3.

## 4. File dùng chung đã chạm (chỉ THÊM)
- `pyproject.toml`/`uv.lock`: `jinja2>=3.1`, khai báo rõ `starlette>=0.40`, `uvicorn>=0.30` (đã có gián tiếp qua `mcp`).
- `cli.py`: đăng ký `web.cli.add_parsers` cuối `build_parser`.
- `guard/runtime.py` (`secret_read_deny_rules`): thêm `HUB_HOME/auth/**`. `guard/classifier.py`: `HUB_HUMAN_ONLY_CLI["web"] = None`.
- Không đổi `auth.py`/`daemon.py`: tiến trình web là tiến trình của NGƯỜI, đọc token như CLI — kênh `web` đi qua `_audit.channel`.

## 5. Kiểm chứng
- `uv run pytest -q`: 460 passed (gồm 19 test web, ~3 s).
- Server thật (`web.demo`, daemon trên socket + FakeRuntime) + Playwright Chromium headless: 22 ảnh desktop 1280 (sáng + tối) và
  mobile 390 px; đã xem lại từng ảnh; trên 390 px `scrollWidth == clientWidth` (không tràn ngang); console không lỗi CSP/JS.
  Tạo lại: chạy `web.demo`, rồi script Playwright (đăng nhập bằng link ký từ `auth/web.json`, chụp các trang).

## 6. Lệch / việc dở
- Chưa có: trang guard policy/protect (dùng CLI `agent-hub guard`), purge vĩnh viễn, sửa golden checker, journey học, canary chi tiết,
  takeover chạy từ web (chỉ hiện lệnh `agent-hub takeover <run>` — cần terminal tương tác), nhập MCP có bước xem trước (hiện nhập thẳng sau xác nhận).
- Phiên lưu RAM: khởi động lại web → đăng nhập lại. Một server web/HUB_HOME (`auth/web.json`).
- Reasoning chỉ hiện khi xem live (delta không lưu DB) — giống TUI.
