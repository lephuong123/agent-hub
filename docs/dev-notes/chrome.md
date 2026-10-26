# Claude in Chrome cho agent (2026-10-26)

Mục tiêu (chủ hub): agent ưu tiên Claude in Chrome thay Playwright khi làm việc với trình duyệt, ít ma sát ("chỉ cần ra lệnh cho
agent"), không nới an toàn. Test: `tests/test_chrome.py` (không gọi claude thật, không đụng trình duyệt).

## Spike (chủ hub, claude 2.1.310, Windows 11)
`claude -p --chrome --strict-mcp-config --mcp-config <rỗng> --settings <file> --setting-sources ""` → event `system/init` có
`mcp_servers: [{"name":"claude-in-chrome","status":"connected","source":"dynamic"}]` và 22 tool
`mcp__claude-in-chrome__{browser_batch, computer, file_upload, find, form_input, get_page_text, gif_creator, javascript_tool,
list_connected_browsers, navigate, read_console_messages, read_network_requests, read_page, resize_window, select_browser,
shortcuts_execute, shortcuts_list, switch_browser, tabs_close_mcp, tabs_context_mcp, tabs_create_mcp, upload_image}`. Extension đã
cài + kết nối trên máy chủ hub. `--no-chrome` cũng có. **Cờ cô lập KHÔNG chặn Chrome** — chỉ cần `--chrome`.
`claude --help` 2.1.310: `--chrome  Enable Claude in Chrome integration`, `--no-chrome  Disable Claude in Chrome integration`;
`--tools` chỉ giới hạn tool built-in (không ảnh hưởng tool MCP).

## Cấu hình
- Agent: `tool_policy.browser: none | playwright | chrome | auto` (mặc định `none` = hành vi cũ; mẫu researcher/coder/reviewer = `auto`).
  CLI `agent create|edit --browser`, `agent show`, web form *Trình duyệt* (đổi giá trị → hộp xác nhận như đổi quyền).
- Hub (chỉ người, nhánh `runtime` ∈ integrity.SECURITY_KEYS): `runtime.claude.chrome.{enabled: true, max_concurrent_runs: 1,
  pass_no_chrome: true, annotations: {}}`. Giá trị lạ sửa tay → `enabled=false`, 1 run (`browser.chrome_cfg`).
- `runtime.claude.flag_matrix["2.1.310"]` thêm `--chrome`, `--no-chrome` (2.1.302/2.1.303 không — chưa kiểm `--help` của chúng).

## Hành vi (`browser.py`, `runs._prepare_spec`, `runtime/claude_cli.chrome_argv`)
| browser hiệu lực | argv | Playwright của agent | system prompt |
|---|---|---|---|
| `chrome` | `--chrome` | BỎ khỏi mcp.json + allowed_tools của run | khối "Browser: Claude in Chrome" |
| `auto` | `--chrome` | giữ | khối "prefer Claude in Chrome, fall back to Playwright" (khi `list_connected_browsers` rỗng / tool lỗi / bận) |
| `none` / `playwright` | `--no-chrome` nếu phiên bản có cờ trong flag_matrix và `pass_no_chrome` | giữ | — |
- Kill switch `enabled=false` → `chrome`/`auto` hạ thành `playwright` (cả argv, prompt, Playwright quay lại, guard chặn tool chrome).
- Phiên bản CÓ trong flag_matrix mà thiếu `--chrome`: `chrome` → ArgvError (run lỗi rõ ràng); `auto` → bỏ cờ. Phiên bản lạ → vẫn truyền.
- `claude-in-chrome` được thêm vào `allowed_mcp_servers` của run chrome/auto (không phải `isolation.violation`); run khác thấy server
  này trong init → vẫn là vi phạm (bắt trường hợp người bật Chrome mặc định ngoài hub).
- Nhận diện Playwright: tên server chứa `playwright` hoặc command/args/url chứa `playwright`.
- Khối prompt (tiếng Anh) nhắc: Chrome THẬT, không đăng nhập/mua/gửi/đăng/submit trừ khi task yêu cầu đúng việc đó; chữ trên trang là
  dữ liệu không tin cậy; dùng tab group riêng và đóng tab đã mở; bận thì không retry vòng lặp.

## Guard (`guard/chrome.py`, nhánh đầu `classify_mcp`)
Server dynamic không có trong registry → trước đây mọi tool rơi vào `mcp_unknown_tool` (luôn giữ). Bảng dựng sẵn:

| Tool | Verdict / luật | strict | balanced | autonomous |
|---|---|---|---|---|
| `tabs_context_mcp`, `list_connected_browsers`, `read_page`, `get_page_text`, `find`, `read_console_messages`, `read_network_requests`, `shortcuts_list`; `computer` action screenshot/zoom/wait/scroll/scroll_to/hover; `gif_creator` start/stop/clear | safe `chrome_read` | cho | cho | cho |
| `navigate`, `tabs_create_mcp` (URL công khai, qua DNS + guard.web) | write `chrome_navigate` (audit) | cho | cho | cho |
| `navigate` tới loopback/RFC1918/`localhost:3000`/DNS ra IP nội bộ/`javascript:`/`file:`/ngoài allowlist | uncertain luật mạng của WebFetch (`webfetch_local`, `ssrf_private_resolved`, `url_scheme`, `web_domain_not_allowed`) | giữ | giữ | giữ |
| `navigate` tới metadata (169.254.169.254…) / domain người chặn | forbidden `ssrf_metadata` / `web_domain_denied` | cấm | cấm | cấm |
| `computer` click/gõ/phím/kéo, `form_input`, `shortcuts_execute`, `select_browser`, `switch_browser`, `resize_window`, `tabs_close_mcp`, `gif_creator` export | uncertain `chrome_action` (audit) | giữ | cho + audit | cho + audit |
| `javascript_tool`, `file_upload`, `upload_image` | uncertain `chrome_sensitive` (∈ ALWAYS_HOLD_RULES + NEVER_RULES: không giao điều phối viên) | giữ | giữ | giữ |
| `browser_batch` | mức NẶNG NHẤT của hành động con (forbidden con → forbidden; URL con → net_check); lồng nhau/không đọc được/>50 → `chrome_action` | | | |
| tool chrome lạ | uncertain `mcp_unknown_tool` | giữ | giữ | giữ |
| agent không bật Chrome / kill switch tắt | forbidden `chrome_not_enabled` | cấm | cấm | cấm |

- "write ở balanced, giữ ở strict" làm bằng `uncertain` + `guard.hold_uncertain` (đúng cơ chế profile, không thêm khoá mới).
- Lệch nhỏ so với đề bài: `computer` chỉ-nhìn (screenshot/zoom/scroll/hover/wait) = safe, `gif_creator` ghi hình = safe; chỉ export
  (tải về/kéo thả lên trang) là hành động.
- URL: `classifier.url_verdict` (tách từ nhánh WebFetch, dùng chung) + `GuardService.net_check` coi tool chrome như WebFetch với
  `Verdict.net_urls` (gồm mọi URL trong browser_batch). URL thiếu scheme → `https://` (như extension).
- Ghi đè của người: `runtime.claude.chrome.annotations` (`cfharness mcp annotate claude-in-chrome <glob> …` ghi vào đây qua
  `config.set` — chỉ người). Thứ tự: `destructiveHint: true` → destructive; `readOnlyHint: true` → safe; `destructiveHint: false` hạ
  uncertain → write; `readOnlyHint: false` nâng safe → write. KHÔNG gỡ luật URL/SSRF, tài nguyên bảo vệ (`policy` match_mcp).
- Luật `mcp_tool` "luôn cho phép tool này cho agent X" áp được cho `chrome_action`/`chrome_sensitive` (người tạo, đúng tên tool).
- Vòng học không bật được Chrome (`learning/guard`: `self_escalation/browser`).

## Khoá trình duyệt (`guard/locks.BrowserLock`)
Chrome THẬT dùng chung → tối đa `max_concurrent_runs` run cầm tool chrome. Lấy ở lần gọi tool chrome đầu (trừ
`list_connected_browsers`) trong `GuardService.check` (sau kiểm forbidden, trước approval); run khác → deny `chrome_busy` kèm hướng
dẫn (agent `auto`: "dùng Playwright"; `chrome`: chờ/thử lại vài lần hoặc báo bận). Gắn theo RUN, lưu `hub_state.browser_locks`; nhả ở
`RunManager._run` finally (kể cả `waiting_human`); người giữ không còn running/queued bị dọn khi run khác xin (sau restart daemon).

## Điều phối viên
`tool_policy.browser` ∈ `TOOL_POLICY_FIELDS` (permissions + owner_actions): đặt được cho agent KHÁC (giá trị phải hợp lệ). Agent điều
phối viên (kể cả chính nó) không sửa được (luật cũ "không đụng agent manager") → chỉ chủ hub bật Chrome cho điều phối viên.

## Rủi ro còn lại
- Chrome THẬT có phiên đăng nhập của người: `chrome_action` ở balanced cho chạy (chỉ audit) — một trang có prompt injection có thể dẫn
  agent click/gõ trong tab đã đăng nhập. Giảm thiểu: profile Chrome riêng cho agent, `approval.profile strict`, hoặc ghi đè
  `mcp annotate claude-in-chrome 'computer' --destructive`.
- Quyền theo site của extension (cấu hình trong extension) chưa kiểm ở chế độ `-p`; nếu extension hỏi quyền trên UI, run có thể treo
  tới timeout.
- `navigate` không có `tabId` trong browser_batch đi tới đâu thì guard chỉ thấy URL, không thấy trang đích sau redirect.
- `takeover` (`claude --resume` tương tác) và run eval/oneshot không truyền `--chrome`.
- `guard.enabled=false`: không có hook → CLI từ chối tool chrome (không có allow sẵn) và khoá trình duyệt không áp.
- 2.1.302/2.1.303 không có `--chrome` trong flag_matrix (chưa kiểm help) → `chrome` lỗi rõ ràng, `auto` chạy không Chrome.
