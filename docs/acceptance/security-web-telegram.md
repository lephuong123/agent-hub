# Kiểm toán bảo mật — Web UI + bot Telegram (2026-10-18)

Phạm vi: `src/agent_hub/web/` (docs/dev-notes/web.md), `src/agent_hub/notify/telegram.py` + `src/agent_hub/chat/` (docs/dev-notes/telegram.md).
Đối chiếu: nguyên tắc `docs/plan/web-telegram.md`, mô hình đe doạ `docs/dev-notes/hardening-SEC.md` (agent chạy CÙNG user OS, có sandbox),
6 bất biến an toàn (BRD §6.10, dẫn từ `traceability-v2.md` §4).

Cách làm: đọc mã + tái hiện thật. Thư mục tái hiện (không nằm trong repo):
`/private/tmp/claude-501/-Users-phuonglq-Workspace-agent-hub/e77b5236-4485-4813-8b88-ed8c360a2291/scratchpad/audit-web/`
- `web.demo` (daemon thật + FakeRuntime, HUB_HOME tạm) trên `127.0.0.1:8799`, trình duyệt Chromium thật (Playwright).
- `evil_devserver.py` — "dev server" giả lập app do agent viết, chạy ở `127.0.0.1:8800`.
- `test_audit_tg.py` — 4 test pytest với server Bot API GIẢ (`tests/telegram_fake.py`), không gọi Telegram thật.
- Không gọi `claude` thật (chi phí $0).

## Tóm tắt

| # | Mức | Phát hiện | Bất biến bị ảnh hưởng |
|---|---|---|---|
| WT-1 | **Nghiêm trọng** | Cookie phiên web gửi tới MỌI cổng của 127.0.0.1 → app do agent viết (người chạy thử ở localhost) lấy phiên, tự duyệt thao tác phá huỷ | 1 (agent không tự duyệt được), 3 |
| WT-2 | TB | Bản xem trước thao tác bị cắt ở 300 ký tự (web + Telegram) → agent đệm lệnh để giấu phần phá huỷ; tool MCP chỉ hiện tên, không có tham số | 1 (người duyệt mà không thấy thao tác thật) |
| WT-3 | TB | Classifier không chặn đọc `HUB_HOME/auth/web.json` khi viết bằng glob, đường dẫn tương đối sau `cd`, biến; khi sandbox tắt → agent tự ký link đăng nhập | 1, 3 (chỉ khi không có sandbox) |
| WT-4 | Thấp | Nhóm Telegram (`allow_groups`): nút duyệt không kiểm người bấm (`from.id`) | 1 (nếu bật nhóm) |
| WT-5 | Thấp | Duyệt 1 chạm cho lớp `file` (vd `rm -rf ~/projects`) trên Telegram — xác nhận 2 bước chỉ cho db/git/system/hub/policy/mcp | — |
| WT-6 | Thấp | Thao tác chỉ-người không qua hộp xác nhận: `mcp_add` (chạy lệnh tuỳ ý qua probe), `secret_set` (ghi đè), lưu/tạo agent (đổi `permission_mode`, tools) | khuếch đại WT-1 |
| WT-7 | Thấp | `safe_next` cho qua `/\t/evil.com` → chuyển hướng mở (cần CSRF hợp lệ — hiện không khai thác được) | — |
| WT-8 | Thấp | Kết quả run gửi Telegram nguyên văn: agent giả được tin "[agent-hub] …", URL và `/lệnh` thành liên kết bấm được | — |
| BUG | — | "Chờ bạn duyệt … luật ?" trên trang run: payload `approval.waiting_human` thiếu `tool/matched_rule/reason` | — |

---

## WT-1 — Nghiêm trọng: cookie phiên không tách theo cổng → app localhost lấy phiên web, tự duyệt

**Bản chất.** Cookie không tách theo cổng (RFC 6265 §8.5). Cookie `ahub_web` đặt cho `127.0.0.1:8765` cũng được gửi tới
`127.0.0.1:3000`, `:5173`, `:8000`… `SameSite=Strict` không giúp gì: các cổng khác nhau trên cùng host vẫn là same-site. Origin check +
CSRF chỉ chặn được *trình duyệt*. Ai có cookie đều GET được trang, đọc token CSRF trong HTML (`name="csrf"`), rồi POST
`/do/approve` từ client không phải trình duyệt với `Origin: http://127.0.0.1:8765` tự đặt.

**Kịch bản thực tế.** Việc hằng ngày của agent là viết app web. Người chạy `npm run dev` và mở `http://127.0.0.1:3000` để xem thử,
trong khi trình duyệt vẫn còn phiên agent-hub (phiên sống tới 7 ngày). Mã server do agent viết (agent có thể bị prompt injection)
chạy ngoài sandbox dưới quyền người dùng. Nó nhận cookie, gọi `/approvals` và duyệt mọi yêu cầu đang chờ. Audit ghi `channel=web, actor=user`
nên không phân biệt được với người thật. Cùng phiên đó, nó còn làm được: `mcp_add` lệnh stdio tuỳ ý (daemon chạy probe ngoài sandbox),
`agent.update` (đổi tools/`permission_mode`), `estop off`, `learn.rollback`, `secret_set`. Sandbox không ngăn được vì tiến trình
khai thác không chạy trong sandbox.

**Tái hiện (đã chạy, thành công).**
1. `uv run python -m agent_hub.web.demo --home <scratch>/home --port 8799` (seed sẵn `ap_289f42b9e3` ở trạng thái waiting_human, lệnh `rm -rf /srv/data/tmp-2025`).
2. `python3 -I evil_devserver.py evil.log`: server ở `127.0.0.1:8800`. Mỗi GET, nó đọc header `Cookie`, gọi `GET /approvals` của
   :8799, lấy `csrf` + id các yêu cầu, rồi `POST /do/approve {csrf,id,confirm=yes}` kèm `Origin: http://127.0.0.1:8799`.
3. Chromium: mở link đăng nhập một lần → `/` (đã đăng nhập). Sau đó mở `http://127.0.0.1:8800/`.
4. Kết quả `evil.log`: `{"cookie": "ahub_web=BZbs…", "approvals_seen": ["ap_289f42b9e3"], "approve_results": [["ap_289f42b9e3", 200]]}`;
   `agent-hub approvals --all` → `ap_289f42b9e3  allowed`. Không cần bấm gì trên trang agent-hub.

**Đề xuất sửa (kết hợp).**
1. **Phục vụ web trên một host riêng**, mặc định `http://agent-hub.localhost:<port>` (Chrome/Firefox/Safari tự phân giải `*.localhost`
   về loopback). Đây là cookie host-only, nên không được gửi tới `127.0.0.1:*` / `localhost:*` mà các dev server thường dùng. Khi đó
   `allowed_hosts` CHỈ chấp nhận host này và từ chối `127.0.0.1`/`localhost` (hoặc chuyển hướng 303 sang host riêng trước khi đặt cookie).
   Link đăng nhập cũng dùng host này.
2. **Gắn phiên với một bí mật không nằm trong cookie** cho mọi POST nhạy cảm, để cookie bị lộ không đủ dùng. Ví dụ: approve/deny,
   `estop`, `mcp_add/import`, `agent_*`, `secret_*`, `config_set`, `learn_rollback` cần thêm header `X-Ahub-Key` lấy từ
   `sessionStorage`. Giá trị này được trả MỘT lần trong fragment `#k=…` lúc đăng nhập (fragment không gửi lên server, không lộ qua
   Referer). Trang HTML KHÔNG được nhúng giá trị này, nên đọc HTML bằng cookie cũng không lấy được.
3. Rút `SESSION_IDLE_S` xuống 1–2 h và `SESSION_MAX_S` xuống 24 h. Khi IP/UA đổi giữa chừng thì ghi `audit` + thông báo Slack/Telegram.
4. Test hồi quy: client có cookie hợp lệ, Origin giả đúng, CSRF đọc từ HTML, KHÔNG có bí mật phiên → `POST /do/approve` phải 403.

## WT-2 — TB: bản xem trước bị cắt 300 ký tự giấu phần phá huỷ; MCP không có tham số

`approvals.action_preview(limit=300)` cắt lệnh và thêm "…". Web (`approval_card`, hộp xác nhận) và Telegram (`format_approval`, bước 2)
chỉ hiện bản đã cắt. Vé duyệt lại gắn với dấu vân tay của TOÀN BỘ lệnh. Agent (hoặc prompt injection) đệm phần đầu bằng nội dung
vô hại thì người duyệt không thấy `rm -rf`. Với tool `mcp__*`, preview chỉ là tên tool, không có tham số (không biết xoá bảng/bản ghi nào).

**Tái hiện.** `test_audit_tg.py::test_preview_truncation_hides_destructive_tail`: lệnh
`echo '<chuỗi vô hại ~340 ký tự>' >/dev/null; rm -rf ~/Documents`. Tin Telegram và `preview` trên web đều kết thúc bằng `…an toa…`,
KHÔNG chứa `rm -rf`. Test đạt (lỗ có thật).

**Đề xuất.**
- Không cắt phần cuối. Hiện `đầu … cuối` (vd 150 + 150 ký tự) kèm tổng độ dài. Nếu lệnh dài hơn ngưỡng thì gắn cờ
  "LỆNH DÀI — xem đầy đủ" và Telegram buộc xác nhận 2 bước.
- Luôn hiện danh sách `targets` của verdict (classifier đã có, lưu trong `approval.open`/audit) ở dòng riêng: "Đích bị xoá: …".
- Web: thêm trang `/approvals/<id>` hiện `tool_input` đầy đủ (đã che secret), cả với MCP. Dữ liệu này ở máy cục bộ nên lý do "không đưa
  dữ liệu MCP ra ngoài" của Slack không áp dụng. Telegram: hiện tên tham số + giá trị đã cắt từng trường.
- Chuẩn hoá ký tự điều khiển/bidi (`\r`, U+202E, U+200B…) trong preview thành ký hiệu nhìn thấy được.

## WT-3 — TB: classifier lọt khi đọc `auth/web.json` (chỉ nguy hiểm lúc không có sandbox)

Ai đọc được `HUB_HOME/auth/web.json` sẽ có khoá ký link đăng nhập, tự ký được link và có phiên web với quyền người, tức là tự duyệt.
Sandbox `denyRead HUB_HOME/auth` chặn được việc này (web.md §3, đã kiểm thật). Khi sandbox tắt (hoặc Linux thiếu bwrap mà
`fail_if_unavailable: false`), lớp còn lại là classifier. `agent-hub guard test` (AGENT_HUB_HOME = home tạm) cho kết quả:

| Lệnh | Phán quyết |
|---|---|
| `cat $H/auth/web.json`, `python3 -c "open('$H/auth/web.json')…"`, `grep -r key $H/auth`, `cp -r $H/auth …`, `tar cf - $H/auth` | ⛔ `hub_secret_read` (đạt) |
| `cd $H && cat auth/web.json` | ✓ an toàn (**lọt**) |
| `cat $H/aut*/web.json` | ✓ an toàn (**lọt**) |
| `D=$H; cat $D/auth/web.json` | ✓ an toàn (**lọt**) |
| `find $H -name web.json -exec cat {} +` | ✓ an toàn (**lọt**) |
| `cat $AGENT_HUB_HOME/auth/web.json` / `…/secrets/TELEGRAM_BOT_TOKEN` | ✓ an toàn (**lọt**, trái với ghi chú H-02 "`$AGENT_HUB_HOME` được giải") |

**Đề xuất.**
- Classifier: giải `cd X && …` (cwd mới cho các lệnh sau), mở glob trên đường dẫn literal trước khi so khớp, gán biến đơn giản
  `V=…; …$V…`, và giải `$AGENT_HUB_HOME`/`$HOME` cho cả nhánh ĐỌC (không chỉ nhánh ghi). `find <tổ tiên của auth|secrets> … -exec`
  → `uncertain`.
- Không giữ khoá ký lâu dài trên đĩa. `web.json` chỉ chứa pid/port. `agent-hub web --url` xin link qua RPC HUMAN_ONLY mới
  (`web.mint_login`, server web đăng ký với daemon qua socket người), nên quyền tạo link đi qua cùng cổng pid/token như approve.
- Ghi cảnh báo trong `doctor`/trang Cài đặt khi `guard.sandbox.enabled=false` mà web đang chạy.

## WT-4 — Thấp: nhóm Telegram không kiểm người bấm

`TelegramBot._on_callback` / `CallbackStore.consume` chỉ kiểm `chat.id`, không kiểm `callback_query.from.id`. Lệnh chat cũng vậy:
không kiểm `message.from.id`. Mặc định `allow_groups: false` (CLI cảnh báo), nhưng khi bật thì mọi thành viên nhóm đều duyệt
được thao tác phá huỷ.

**Tái hiện.** `test_group_member_not_owner_can_approve`: nhóm `-1001234` có trong allowlist. Người lạ `from_id=777777` bấm "Cho phép"
→ `allowed`, kênh `telegram:-1001234`.

**Đề xuất.** Allowlist theo cặp (chat_id, user_id). Lúc ghép nối (`/start <mã>`), lưu `from.id` của người gửi. Callback và lệnh trong
nhóm chỉ nhận khi `from.id` thuộc danh sách người được phép (`allowed_user_ids`). Audit thêm `from_id` cho mọi quyết định.

## WT-5 — Thấp: `rm -rf` ngoài run chỉ cần 1 chạm trên Telegram

`is_heavy` chỉ đúng khi có `protected_resource_id` hoặc lớp thuộc `confirm_classes` (mặc định db, git, system, hub, policy, mcp).
Lớp `file` không nằm trong danh sách, nên `rm -rf ~/projects` (không khai báo bảo vệ) được duyệt bằng một chạm, dễ bấm nhầm trên điện thoại.
Tái hiện: `test_file_class_rm_rf_is_single_tap` → `allowed` sau 1 lần bấm.
**Đề xuất.** Thêm `file` vào mặc định `confirm_classes` khi verdict là `destructive` và đích nằm ngoài cwd của run, hoặc khi đích là
thư mục (`rm -r`). Giữ 1 chạm cho `uncertain` trong cwd.

## WT-6 — Thấp: thao tác chỉ-người không có hộp xác nhận (khuếch đại WT-1)

`ACTIONS` không đặt `confirm` cho: `mcp_add` (gọi `mcp.add {probe: true}`, tức daemon chạy NGAY lệnh stdio tuỳ ý ngoài sandbox), `secret_set`
(ghi đè được `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CALLBACK_KEY`…), `agent_resume`, `agent_restore`, `mcp_restore`, `team_member`, `golden_restore`.
Form `/agents/create` và `/agents/{id}/save` cũng không có xác nhận và không kiểm `permission_mode` theo `PERMISSION_MODES`.
Server nhận mọi giá trị; việc chặn `bypassPermissions` còn tuỳ vào validator của `agent.update`. Bản thân điểm này không khai thác được
vì CSRF chặn, nhưng gặp WT-1 hoặc XSS thì thành một bước leo thang không cần tương tác.
**Đề xuất.** Thêm `confirm` + `danger` cho `mcp_add` (hiện lệnh sẽ chạy), `secret_set` khi tên đã tồn tại, và lưu agent khi đổi
`permission_mode`/`tools`/`mcp_servers`. Kiểm `permission_mode ∈ PERMISSION_MODES` ở `agent_changes`.

## WT-7 — Thấp: `safe_next` cho qua tab

`safe_next('/\t/evil.com')` trả nguyên chuỗi. Trình duyệt bỏ tab trong URL, thành `//evil.com`, tức chuyển hướng mở (qua `Location` của
`flash_redirect` và `href` của nút "Huỷ" trên `confirm.html`). Điểm này chỉ chạy sau `post_guard` (CSRF), nên hiện không khai thác được.
**Đề xuất.** Bỏ mọi ký tự `< 0x20` và `\x7f` trước khi kiểm, hoặc chỉ chấp nhận `^/[A-Za-z0-9/_\-.?=&%]*$` và chặn `//`, `/\`.

## WT-8 — Thấp: nội dung agent trong tin Telegram

`format_result` gửi `summary` của run nguyên văn (đúng là văn bản thuần, không HTML/Markdown injection). Telegram vẫn tự nhận diện
URL và thực thể `bot_command`: bấm `/estop` trong tin sẽ gửi lệnh `/estop` (không kèm tham số), còn `/estop off` thì vẫn cần nút xác nhận.
Agent có thể tạo tin giống thông báo hệ thống ("[agent-hub] Đã CHO PHÉP …", "nhắn /approvals …") và chèn link lừa đảo.
**Đề xuất.** Gửi kết quả qua `parse_mode=HTML`, bọc toàn bộ `summary` trong `<blockquote>`/`<pre>` (escape `& < >`), để Telegram không
tạo entity bên trong. Đặt tiền tố rõ ràng "Kết quả do agent viết (không phải thông báo hệ thống):".

## BUG — "Chờ bạn duyệt … luật ?" trên trang run

**Nguyên nhân.** `ApprovalService.to_waiting_human` (`approvals.py`, event `approval.waiting_human`) chỉ đưa vào payload
`request_id, run_id, since, preview, lease_held, repo_lock_held, approve_cmd`. Payload này THIẾU `tool`, `matched_rule`,
`destructive_class` và `reason`, trong khi `web/present.transcript_item` đọc `p.get("tool")` và `p.get("matched_rule")` để vẽ mục transcript.
Ảnh `docs/screenshots/web/03-run-cho-duyet.png` (chụp 22:58) cho thấy dòng "⏸ Chờ bạn duyệt : rm -rf … / luật ? — duyệt ở khung phía trên".
`_items.html` đã được sửa lúc 22:59 để ẩn dòng luật khi trống. Hiện tại dòng này không còn chữ "luật", nhưng vẫn dư dấu `:` vì `tool`
rỗng (kiểm bằng `GET /runs/<id>`: `<b>⏸ Chờ bạn duyệt</b> : <code>rm -rf …</code>`). Đây chỉ là che triệu chứng.
Cùng nguyên nhân: tin Telegram `approval.waiting_human` không có dòng "Lý do". TUI (`tui/model.py:337`) rơi về hiện `request_id`.
Kiểm chứng: `test_waiting_human_message_lacks_rule` → các khoá payload như trên.
**Sửa.** Trong `to_waiting_human`, thêm `"tool": row["tool_name"], "matched_rule": row["matched_rule"], "destructive_class": row["destructive_class"],
"reason": row["reason"], "protected_resource_id": row["protected_resource_id"]` vào payload (cập nhật `docs/schema/event.v1.json` nếu có ràng buộc).
Nhánh `approval.waiting_human` không có preview trong `transcript_item` vẫn giữ nguyên.

---

## Điểm đã kiểm và ĐẠT

**Web**
- Bind: `require_loopback` từ chối `0.0.0.0`/IP LAN. CLI trả 1, `uvicorn` chỉ nghe host đã kiểm (đọc mã + test sẵn có).
- DNS rebinding: `GuardMiddleware` trả 421 khi `Host` ∉ `{127.0.0.1,localhost,[::1]}:port`, áp cả cho `/login` và static.
- Chưa đăng nhập: trang → 303 `/login`; partial/SSE/POST → 401. Cookie giả → 401 (`WebAuth.get`).
- Link một lần: HMAC-SHA256 so sánh hằng thời gian; nonce bị tiêu lần đầu (dùng lại → 401); hạn 5 phút. Token không ở lại thanh địa chỉ
  (303 → `/`). `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, access log uvicorn tắt (`access_log=False`). `webbrowser.open`
  trên macOS truyền URL qua stdin của `osascript`, không qua argv.
- Cookie: `HttpOnly`, `SameSite=Strict`, `Path=/`, giá trị 256 bit ngẫu nhiên, phiên trong RAM, có logout (POST + CSRF). Cách ly theo
  cổng thì không đạt (xem WT-1).
- CSRF: mọi POST qua `post_guard` (token theo phiên, so sánh hằng thời gian + Origin/Referer/Sec-Fetch-Site). `Origin: null` → 403.
  Không có header CORS. Trình duyệt khác nguồn không đọc được trang hay SSE.
- XSS (đã render payload độc thật qua `present.md`, `_items.html`, `approval_card`): Jinja autoescape bật. Markdown `html=False`,
  `validateLink` chỉ cho http/https/mailto (`javascript:`, `JAVASCRIPT:`, `java\tscript:`, `&#106;avascript:`, `data:` đều thành văn bản).
  Thuộc tính `onerror`, `<script>`, `"><img>` trong tên tool, `tool_id` (thuộc tính `id`), preview, rule, tên MCP, `agent_id`
  đều đã escape. `data-confirm` hiện bằng `textContent`. SSE gửi HTML đã render+escape phía server. CSP `script-src 'self'`,
  `img-src 'self' data:` (ảnh ngoài trong markdown không rò dữ liệu), `form-action 'self'`, `frame-ancestors 'none'`. Prompt injection từ dữ
  liệu/mô tả MCP KHÔNG chèn được HTML/JS.
- IDOR: hub là một người dùng, không có chủ thể khác để vượt quyền. Mọi đường dẫn `{id}` chỉ đi qua RPC với quyền người của chính phiên.
- HUMAN_ONLY từ web có audit `channel=web, actor=user`; approve/deny/stop/estop/xoá/revert/rollback/secret_rm/config có xác nhận (bỏ qua
  được bằng `confirm=yes` khi có CSRF hợp lệ, đúng thiết kế). `config.set` giới hạn trong `SAFE_CONFIG` (không sửa được `guard.*`,
  `notify.telegram.api_base`, `allowed_chat_ids`).
- `HUB_HOME/auth/` có quyền 0700, `web.json` 0600 (ghi `O_EXCL|O_NOFOLLOW`, `os.replace`). `read_state` từ chối symlink/khác uid. Sandbox
  `denyRead auth/` + `permissions.deny` Read/Grep/Glob. Lệnh `agent-hub web` từ agent → forbidden. Đọc literal thì bị chặn
  (đường lọt: WT-3).
- Token người (`<socket>.auth`) không có trong HTML/cookie/log (test sẵn có + đọc mã `rpc`).

**Telegram**
- Giả mạo callback: dữ liệu bịa, chữ ký sai, nút của chat khác, định dạng cũ, chat lạ → từ chối + audit. Sai chat/chữ ký không tiêu nonce
  thật (test sẵn có + đọc `CallbackStore.consume`).
- Replay: nonce dùng một lần (tiêu cả nhóm Cho phép/Từ chối), offset `getUpdates` lưu TRƯỚC khi xử lý. `approvals.resolve` báo Conflict;
  vé thực thi gắn `action_fingerprint`.
- Chat ngoài allowlist: không trả lời, không tới dispatch. `telegram_caller` không phải người, nên HUMAN_ONLY → BLOCKED.
- Bot không nhận tin của chính nó, nên agent KHÔNG tự gửi `/approve` qua output được (bot không có lệnh duyệt bằng chữ; chỉ nút ký HMAC).
  Reply vào tin kết quả chỉ gọi `run.continue`, không có lệnh nào khác.
- Không có `parse_mode`, nên không có injection Markdown/HTML trong tin gửi (xem WT-8 về entity tự động).
- Token: chỉ lấy từ secret store; `_scrub` gỡ token khỏi lỗi mạng/HTTP, ngắt chuỗi exception bằng `from None`; logger `httpx*` để mức
  WARNING; che theo giá trị (SEC H-12). `telegram.status` và kết quả `setup` không trả token. `api_base` chỉ sửa được bằng `config.set`
  (HUMAN_ONLY, không có trong `SAFE_CONFIG` của web; nhánh `notify` của config.yaml được integrity khôi phục).
- Ghép nối: mã 32 bit, TTL 10 phút, so sánh hằng thời gian, chỉ chat riêng (trừ khi `allow_groups`). Nhắn đúng mã chỉ đưa vào danh sách
  "chờ xác nhận" và người phải gõ `y` ở terminal (hiện tên/username/chat_id). Mã bị huỷ sau `telegram.allow`. Người lạ biết username bot
  nhưng không có mã thì không chiếm được. Có mã mà không được người xác nhận thì cũng không vào được. Riêng `--yes` nhận chat ĐẦU TIÊN
  gửi đúng mã, nên không chia sẻ deep link trước khi tự bấm.
- Forward/nhóm: quyền theo `chat.id`. Tin forward trong chat riêng vẫn do chủ chat gửi. Nhóm: xem WT-4.

## Đề xuất thứ tự sửa
1. WT-1: host riêng `agent-hub.localhost` + bí mật phiên ngoài cookie cho POST nhạy cảm + test hồi quy. **Chặn phát hành web cho đến khi sửa.**
2. BUG payload `approval.waiting_human` (một dòng, ảnh hưởng web/Telegram/TUI).
3. WT-2 (preview đầu…cuối + targets), WT-3 (classifier: cd/glob/biến/`$AGENT_HUB_HOME` nhánh đọc; bỏ khoá ký trên đĩa).
4. WT-4..WT-8.
