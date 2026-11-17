# OAuth cho MCP server từ xa (`feat/mcp-oauth`)

Mục tiêu: người dùng nối một MCP server từ xa đòi OAuth bằng MỘT cú bấm "Kết nối" (như connector của Claude), thay vì phải
tự lấy token dán vào `headers`. Trước nhánh này MCP client chỉ có header tĩnh.

## 1. Luồng (MCP Authorization spec, OAuth 2.1)
Daemon điều khiển, chia 2 bước (trình duyệt đi vòng qua web UI hoặc listener loopback của CLI — KHÔNG có server callback chặn
trong daemon):

1. `mcp.oauth_start {name, redirect_uri}` → `{authorize_url, expires_at, client, authorization_host}`
   - `POST initialize` không token tới URL server → 401 + `WWW-Authenticate` (`resource_metadata`, `scope`).
   - Protected Resource Metadata (RFC 9728): URL từ header → `/.well-known/oauth-protected-resource/<path>` → gốc. `resource`
     phải khớp URL server (`check_resource_allowed`) — không thì từ chối (chống PRM trỏ token sang dịch vụ khác).
   - Authorization Server Metadata (RFC 8414 + OIDC discovery, thứ tự URL của SDK `mcp`). `issuer` phải khớp AS mà PRM chỉ tới
     (chống mix-up). AS khai `code_challenge_methods_supported` mà thiếu `S256` → từ chối.
   - Không có PRM → dự phòng spec 2025-03-26: AS metadata ở origin server, thiếu nữa thì `/authorize`, `/token`, `/register`.
   - Client: `auth.client_id` (+ `client_secret_ref`) người nhập; không có → Dynamic Client Registration (RFC 7591), client công
     khai (`token_endpoint_auth_method=none`), mỗi `redirect_uri` một bản đăng ký (giữ ≤ 6, dùng lại nếu cùng issuer).
     AS không có `registration_endpoint` → lỗi nêu rõ lệnh nhập client_id + redirect URI cần khai với nhà cung cấp.
   - PKCE S256 (verifier 96 ký tự), `state` 256 bit, `resource` (RFC 8707), `scope` = `auth.scopes` → `WWW-Authenticate` →
     `scopes_supported` của PRM (KHÔNG lấy toàn bộ `scopes_supported` của AS — tránh xin quá quyền).
   - Trạng thái chờ `{state → server, redirect_uri, verifier, client_key, issuer, hạn}` CHỈ ở RAM daemon, hạn 10 phút, ≤ 32 mục.
2. `mcp.oauth_callback {state, code | error, iss?}` → `{name, ok, oauth_status, oauth, error?}`
   - `state` bị TIÊU ngay (một lần, cả khi lỗi); lạ/đã dùng → lỗi; quá hạn → `expired_state`.
   - `iss` (RFC 9207): có mà khác issuer → từ chối; thiếu mà AS khai `authorization_response_iss_parameter_supported` → từ chối.
   - Đổi code lấy token (`code_verifier`, `redirect_uri`, `resource`; client_secret_basic/post theo client) → lưu.
3. Làm mới: `refresh_token` + `resource`; AS không xoay vòng → giữ refresh_token cũ. `invalid_grant|invalid_client` → đánh dấu
   `error` ("đăng nhập lại"). Khoá theo server (không refresh song song).
4. Ngắt kết nối `mcp.oauth_disconnect {name}`: thu hồi (RFC 7009, refresh rồi access, cố gắng — lỗi bỏ qua) → xoá bản ghi + huỷ
   state đang chờ của server.

RPC: `mcp.oauth_start|callback|disconnect|refresh` (ghi — `MUTATING` + `auth.HUMAN_ONLY`, audit tự bọc), `mcp.oauth_status {name}`
(đọc). `mcp.show`/`mcp.list` thêm `oauth_status` (`not_connected|connected|expired|error`), `oauth_expires_at`, `oauth`
(`status, expires_at, scope, issuer, refreshable, client: dcr|manual, error, connected_at`) — không bao giờ có token.
`mcp.agent_config.problems` có `<server>:oauth=<trạng thái>` khi chưa kết nối. Event: dùng lại `mcp.changed` với `op` =
`oauth_started|oauth_connected|oauth_refreshed|oauth_error|oauth_disconnected` (payload không có token) — không thêm loại event.

## 2. Cấu hình
```yaml
notes:
  transport: http            # hoặc sse; stdio + auth → lỗi
  url: https://mcp.example.com/mcp
  auth:
    type: oauth
    scopes: [read]           # tuỳ chọn
    client_id: my-app        # tuỳ chọn — provider không có DCR
    client_secret_ref: ${MCP_NOTES_OAUTH_CLIENT_SECRET}   # giá trị rõ tự chuyển vào secret store
```
`headers.Authorization` + `auth` → lỗi (hub tự chèn). CLI: `cfharness mcp add --url URL --oauth [--oauth-scope s] [--oauth-client-id id
--oauth-client-secret '${SECRET}'] <tên>`, `mcp edit <tên> --oauth|--no-oauth|--oauth-client-id …` (gộp với `auth` hiện có).
Web: form thêm server có ô "Đăng nhập OAuth" (chỉ http/sse; không probe khi thêm, chuyển về trang server để bấm Kết nối).

## 3. Lưu trữ
- Bản ghi JSON trong secret store (file 0600, thư mục 0700 — `secrets.py`), tên `MCP_OAUTH_<TÊN>_<6 hex băm tên>` (băm để
  `a-b`/`a_b` không đụng nhau): metadata AS (endpoint), `issuer`, `resource`, `server_url`, `clients{redirect_uri → client_id,
  client_secret?, method}`, `tokens{access_token, refresh_token, expires_at, scope, client}`, `error`.
- KHÔNG bao giờ trong hub.db, `registry.yaml`, event, log, audit, argv. Đọc/ghi bản ghi → `redact.register_secret_values` cho
  access/refresh token + client_secret (bộ che theo giá trị); daemon khởi động nạp lại (`wiring.attach`).
- Audit `mcp.oauth_*` chỉ giữ `name`, `redirect_uri`, `error` (bỏ `code`, `state`, `iss`) — `wrap_with_audit`.
- Đổi `url` server → token bị bỏ ngay (`registry.update`), và lúc dùng còn kiểm `server_url` của bản ghi == URL hiện tại: token
  gắn `resource` cũ không bao giờ đi sang URL khác.

## 4. Lúc chạy
- `RunManager._run` → `hub.mcp_oauth.ensure_fresh_for(agent.mcp_servers)` TRƯỚC `_prepare_spec`: token còn < 5 phút
  (`REFRESH_MARGIN_S`) → refresh (timeout 30 s; lỗi mà token còn hạn → dùng tiếp).
- `registry.runtime_entry(cfg)` (dùng bởi `build_for_agent_servers`, `resolved_entry`, `probe`) = resolve `${…}` + chèn
  `headers.Authorization: Bearer <token>`. mcp.json runtime: `_write_json_0600`, `rmtree(runtime/<run>)` trong `finally` của
  `_run` (đã kiểm bằng test: mode 0600 trên POSIX, file biến mất sau run); sandbox chặn agent đọc mcp.json.
- `mcp.oauth_proxy.enabled=true` → mcp.json của run trỏ tới proxy loopback thay vì chèn Bearer (§8.1); token làm mới khi chuyển tiếp.
- **Chưa kết nối / hết hạn / URL đổi → run FAILED với thông báo rõ** ("cần đăng nhập OAuth — `cfharness mcp login <tên>` hoặc
  nút Kết nối"). Chọn fail-closed thay vì bỏ server + event: cùng hành vi với secret thiếu, agent không chạy âm thầm thiếu tool
  rồi tự xoay xở (có thể bằng cách kém an toàn hơn); người thấy ngay lỗi ở trang run.
- `mcp.probe` cũng `ensure_fresh` rồi dùng token; chưa kết nối → `ok:false` + gợi ý đăng nhập; server không bật OAuth mà trả 401
  → gợi ý `--oauth`.

## 5. Web
- `POST /do/mcp_connect` (`web/mcp_oauth.py`, route đặt TRƯỚC `/do/{name}`): `post_guard` (CSRF + Origin + khoá phiên WT-1) →
  `mcp.oauth_start` với `redirect_uri = http://cfharness.localhost:<port>/mcp/oauth/callback` → trang trung gian
  `mcp_oauth_go.html` (`<meta refresh>` + nút "Tiếp tục tới <host>"). Đây là chỗ DUY NHẤT web đưa trình duyệt ra URL ngoài; URL
  phải https (http chỉ loopback). **Lệch so với yêu cầu "303"**: CSP `form-action 'self'` của trang chặn chuyển hướng khác nguồn
  sau khi gửi form (Chrome/Firefox áp form-action lên redirect) → 303 thẳng sẽ bị trình duyệt chặn; meta refresh là điều hướng
  mới, không thuộc form-action, không cần script inline. htmx → `HX-Redirect`.
- `GET /mcp/oauth/callback`: thêm vào `GuardMiddleware.PUBLIC`. Lý do: cookie phiên `SameSite=Strict` KHÔNG được gửi khi nhà cung
  cấp chuyển hướng về (điều hướng cấp cao khác site) → route này phải chạy không phiên. Bảo vệ: Host vẫn phải là
  `cfharness.localhost:<port>` (421 nếu khác); `state` 256 bit, một lần, ≤ 10 phút do daemon giữ là lớp chống CSRF/login-CSRF
  (kẻ không biết state không thể gắn tài khoản của họ vào hub); trang kết quả không có phiên chỉ hiện tên server + kết quả,
  `<meta refresh>` về `/mcp/<tên>?oauth=connected|error` (điều hướng cùng nguồn → cookie có lại → partial hiện thông báo).
  Có phiên (vd trình duyệt nới SameSite) → flash + 303 về `/mcp/<tên>`. Access log uvicorn đã tắt (`web/cli.py`) nên `code` không
  vào log.
- `mcp_disconnect` ∈ `ACTIONS` (hộp xác nhận, `danger`).
- Giao diện: partial `_mcp_oauth.html` (include cuối `mcp_show.html`), cột "OAuth" trong `mcp.html`, ô "Đăng nhập OAuth" trong form
  thêm server. Chỉnh template MCP tối thiểu (nhánh `feat/web-mcp-manage` đang sửa các file này): mcp_show +1 dòng, mcp +3 dòng.

## 6. CLI
`cfharness mcp login <tên> [--no-browser] [--port N] [--timeout s]`: listener loopback TẠM `http://127.0.0.1:<cổng>/callback`
(ưu tiên 33418 để dùng lại bản DCR; bận → ngẫu nhiên), `mcp.oauth_start`, in + mở URL, xử lý request trên luồng chính tới khi có
MỘT callback mang state hợp lệ (state lạ → 400, chờ tiếp), chuyển cho `mcp.oauth_callback`, đóng listener. Chọn listener thay vì
chờ qua web callback: không phụ thuộc `cfharness web` đang chạy, `127.0.0.1` được nhiều nhà cung cấp chấp nhận cho http hơn
`*.localhost`, ít trạng thái chia sẻ. Handler không log URL. `cfharness mcp logout <tên>`. `mcp show`/`mcp list` hiện trạng thái.
`login`/`logout` ∈ `HUB_HUMAN_ONLY_CLI["mcp"]` (agent gọi → forbidden).

## 7. Mô hình đe doạ / quyết định
- Agent trong run không đọc được token: secret store + mcp.json runtime nằm trong denyRead của sandbox; RPC `mcp.oauth_*` ghi là
  HUMAN_ONLY; `cfharness mcp login|logout` forbidden trong classifier. Agent gọi tool MCP bằng token của người — đó là mục đích;
  tool ghi vẫn qua guard/approval như mọi MCP server (trust/annotations không đổi).
- Token chỉ gửi tới: token endpoint/revocation endpoint của AS đã khám phá (https), và URL MCP server đã cấu hình (khớp
  `resource`). Endpoint http ngoài loopback → từ chối.
- Code bị chặn (log trình duyệt, extension) vô dụng thiếu verifier (PKCE, verifier không rời RAM daemon).
- Daemon khởi động lại giữa chừng → state mất → "state không hợp lệ", bấm Kết nối lại (chấp nhận, không lưu verifier xuống đĩa).
- Còn mở: tiến trình cùng user ngoài sandbox đọc được secret store (ngang mức đã chấp nhận ở hardening-SEC §3).

## 8. Giới hạn đã biết + đề xuất
- **Token hết hạn giữa run rất dài** (đường cũ): token được chèn tĩnh vào mcp.json lúc spawn; `claude` không biết refresh. Run dài hơn
  thời hạn access token (thường 1 h) → MCP trả 401 giữa chừng. Đã có lời giải: proxy loopback §8.1 (bật mặc định từ v0.2.17).

### 8.1 Proxy loopback chèn token (`mcp_client/oauth_proxy.py`, nhánh `feat/mcp-oauth-proxy`)
BẬT mặc định từ v0.2.17 (2026-11-17, sau nghiệm thu với claude thật — kết quả bên dưới); tắt: `cfharness config set mcp.oauth_proxy.enabled false`. Cấu hình
`mcp.oauth_proxy.{enabled, port (0 = OS chọn), max_body_mb}`; nhánh ∈ `integrity.SECURITY_KEYS` (sửa tay config.yaml bị khôi phục).

**Luồng.**
1. `RunManager._run`: agent có server OAuth + proxy bật → `hub.mcp_proxy.ensure_started()` (khởi động lười, một lần mỗi daemon) sau
   `ensure_fresh_for`. Lỗi bind (cổng bận…) → log cảnh báo, **dự phòng đường cũ** (Bearer tĩnh trong mcp.json) — run không hỏng.
2. `_prepare_spec` → `_agent_mcp_servers(agent, run_id)`: vẫn dựng entry đầy đủ trước (kiểm token: chưa kết nối/hết hạn/URL đổi →
   run `failed` rõ như cũ), rồi THAY entry của mỗi server OAuth bằng `proxy.register(run_id, cfg)` =
   `{"type": http|sse, "url": "http://127.0.0.1:<cổng>/r/<id 192 bit><path của server>", "headers": {"X-Cfharness-Proxy-Key": <256 bit>}}`.
   mcp.json KHÔNG còn token, refresh token hay header tĩnh nào của server (proxy tự thêm khi chuyển tiếp).
3. Mỗi request tới proxy, theo thứ tự (mọi từ chối cùng `403 forbidden`, không phân biệt lý do): peer phải loopback; `Host` =
   `127.0.0.1:<cổng>`/`localhost:<cổng>` (chống DNS rebinding); KHÔNG có `Origin` (client MCP của claude không gửi — trình duyệt luôn
   gửi với POST khác nguồn → chặn CSRF từ trang web); route còn sống; khoá đúng (`hmac.compare_digest`); path ∈ danh sách cho phép
   của route (ban đầu chỉ path của URL server — token không đi tới path khác cùng origin, vd token endpoint); server vẫn là OAuth và
   URL cấu hình == URL lúc đăng ký route (URL đổi giữa run → từ chối).
4. Thân request đệm (≤ `max_body_mb`, quá → 413) → `oauth.ensure_fresh(server)` (làm mới nếu còn < 5 phút) → header mới nhất từ
   `registry.runtime_entry` (Bearer + header tĩnh đã thay secret) → gửi tới `origin server + path` (httpx2, không theo redirect,
   `accept-encoding: identity`, bỏ hop-by-hop/`authorization`/khoá proxy/`origin`/`cookie`). Upstream 401 → nếu token chưa bị request
   khác làm mới thì `oauth.refresh` bắt buộc, gửi lại ĐÚNG MỘT lần; vẫn 401/làm mới lỗi → trả 401 **không** kèm `WWW-Authenticate`
   (không để `claude` tự mở luồng OAuth của nó với proxy).
5. Response chuyển tiếp từng khối (`aiter_raw`, không đệm — Streamable HTTP trả `text/event-stream`, GET SSE dài), bỏ hop-by-hop,
   `content-encoding`, `www-authenticate`, `set-cookie`. Transport `sse` cũ: event `endpoint` được viết lại thành
   `/r/<id>/<path>?<query>` (chỉ khi cùng origin với server; path đó vào danh sách cho phép) → POST message cũng đi qua proxy + khoá.
   Client ngắt kết nối → đóng stream upstream.
6. `finally` của `_run` → `proxy.revoke_run(run_id)`: route bị xoá (request sau → 403), stream đang mở bị đóng. Run `waiting_human`
   cũng vậy (tiến trình đã dừng); resume = run mới = route mới. Daemon dừng (`Hub.stop`) → proxy dừng; khởi động lại → route mất
   (RAM), cổng mới (nếu `port: 0`).

**Vì sao KHÔNG mở ngoại lệ sandbox.** Sandbox OS của Claude Code chỉ bọc Bash (hardening-SEC §1: "Hook và server MCP do claude chạy NGOÀI
sandbox"); client MCP HTTP/SSE nằm trong chính tiến trình `claude` → nối được proxy mà KHÔNG cần `allowLocalBinding`. Giữ nguyên chặn
loopback cho Bash là tốt hơn yêu cầu ban đầu: Bash của agent thường không chạm được proxy chút nào. Test khẳng định settings.json của
run không có `allowLocalBinding`.

**Mô hình đe doạ.**
- Agent (Bash/Read) không đọc được khoá: mcp.json ở `agents/*/runtime` (denyRead sandbox + `permissions.deny` Read/Grep/Glob).
- Agent được người mở `allow_localhost` (hoặc trust full / sandbox tắt) chạm được cổng nhưng vẫn cần route id + khoá của đúng run;
  agent khác / tiến trình khác không có. Nếu agent trust full đọc được mcp.json của CHÍNH run mình thì có thể gọi tool MCP qua proxy
  mà không qua hook guard — nhưng đường cũ còn tệ hơn (đọc được token thật, dùng được từ bất kỳ đâu, tới khi hết hạn). Với proxy, thứ
  lộ ra chỉ dùng được trên loopback, cho đúng một server, tới khi run kết thúc.
- Tiến trình cùng user ngoài sandbox: vẫn đọc được secret store (ngang mức §7); với proxy không thêm gì đáng kể.
- Log: proxy không log URL (có route id), header hay body; lỗi đi qua `redact_str`.

**Nghiệm thu với `claude` thật — 2026-11-17, Claude Code 2.1.326, haiku, tổng $0.021 (6 run).** Hub TẠM (`CFHARNESS_HOME` trong
thư mục tạm, daemon từ mã nhánh, `budget.hub_usd_per_day=0.30`), guard bật + sandbox OS bật (settings.json của run: `sandbox.enabled=true`,
`failIfUnavailable=true`, KHÔNG `allowLocalBinding`, hook PreToolUse có mặt). Server = `tests/oauth_fake.py` chạy thành tiến trình riêng trên
127.0.0.1, ghi log từng request (method, path, status, 13 ký tự đầu `Authorization`, tên header, UA). Hai server OAuth gán cho agent:
`fnotes` (Streamable HTTP, đăng nhập qua luồng thật `mcp.oauth_start/callback` với AS giả) và `flegacy` (transport SSE cũ, token gieo sẵn).
Agent gọi `doc_ghi_chu` vài lần.
- **(a) Kết nối 127.0.0.1 khi guard + sandbox bật: ĐẠT.** Tool trả "ghi chu so N: hop OK"; log server thấy request UA `claude-code/2.1.326
  (sdk-cli)` qua proxy. Không cần ngoại lệ sandbox (khẳng định: client MCP chạy ngoài sandbox Bash).
- **(b) Khoá trên mọi loại request: ĐẠT.** Server nhận từ claude (qua proxy): Streamable HTTP `POST /mcp` (initialize, notification 202,
  tools/list, tools/call) + `GET /mcp` (stream server→client, 405 của server giả); SSE cũ `GET /sse` (200, stream) + `POST /messages?session_id=…`
  (202, endpoint đã viết lại qua proxy). Proxy chỉ chuyển tiếp khi khoá đúng (mọi nhánh khác từ chối trước khi chạm upstream) → mọi request
  trên đều mang `X-Cfharness-Proxy-Key`; upstream không bao giờ thấy header khoá (`has_proxy_key_header=false`) và mọi `Bearer` upstream do
  proxy chèn. mcp.json chụp lúc run chạy: chỉ URL `/r/<id>/…` + khoá, không `Authorization`/`Bearer`/access token/seed.
- **Làm mới giữa run: ĐẠT.** Server giả vô hiệu access token ngay sau tools/call đầu → tools/call kế tiếp: upstream 401 → proxy gọi
  `/as/token` (refresh) → gửi lại với token mới → 200; agent không thấy lỗi.
- **(c) HTTP(S)_PROXY trong env daemon: TRƯỚC SỬA — HỎNG.** Daemon chạy với `HTTP_PROXY/HTTPS_PROXY` (+ chữ thường) trỏ proxy chuyển tiếp ghi
  log: claude đẩy `POST/GET http://127.0.0.1:<cổng>/r/<id>/…` QUA proxy ngoài (route id + khoá đi dạng rõ tới proxy; MCP 502
  `CLIENT_HTTP_NOT_IMPLEMENTED`). API Anthropic đi `CONNECT api.anthropic.com:443` (đúng). **Sửa:** `runtime/claude_cli.with_loopback_no_proxy`
  — có biến proxy trong env tiến trình `claude` (cả từ `env_extra` của run) → thêm `127.0.0.1,localhost,::1` vào `NO_PROXY` + `no_proxy`
  (giữ giá trị cũ, `*` giữ nguyên). Phía daemon, httpx cũng không tự bỏ qua loopback: client của proxy + `OAuthManager` gắn
  `LOOPBACK_DIRECT_MOUNTS` (đích 127.0.0.1/localhost/::1 đi thẳng; đích từ xa vẫn theo proxy của môi trường). **SAU SỬA — ĐẠT:** log proxy
  ngoài chỉ còn `CONNECT api.anthropic.com:443` (+ telemetry của CLI), không request loopback nào; cả `fnotes` lẫn `flegacy` trả kết quả.
- Phát hiện thêm khi nghiệm thu (đã sửa): `cfharness mcp edit --url` không kèm `--transport` ép server về `http` → server SSE cũ hỏng
  (claude thử POST lên URL SSE). Giờ giữ transport http/sse hiện có. Log của proxy mang `run_id` của run khởi động nó (contextvar kế thừa)
  → task server chạy trong Context rỗng. Từ chối giờ ghi log INFO kèm mã lý do (`missing_key|bad_key|unknown_route|path|origin|host|…`,
  path chỉ khi ngoài `/r/`) + `proxy.status()["stats"]`.
- Chưa thử với nhà cung cấp OAuth thật (Linear/Notion…) — phần OAuth đã có ở luồng cũ; proxy chỉ đổi chặng claude → server.
- Dọn: daemon tạm dừng, `~/.claude/projects/<cwd tạm>` đã xoá, không đụng hub thật / `~/.claude.json`.

**Giới hạn.** Body request đệm toàn bộ (MCP là JSON-RPC nhỏ; upload lớn qua MCP hiếm). Không hỗ trợ WebSocket. `cfharness takeover`
(phiên tương tác của người, không có vòng đời run) vẫn dùng đường cũ. Probe (`mcp.probe`) gọi thẳng server (trong daemon, không qua file).
- Không có vòng nền refresh — chỉ làm mới khi run/probe/start cần (đủ cho mô hình hiện tại; token "connected" hiển thị có thể
  "expired" tới lần dùng kế tiếp, khi đó tự refresh).
- Không hỗ trợ: Client ID Metadata Document (CIMD), `private_key_jwt`, DPoP, step-up scope (403 `insufficient_scope`).
- Một số nhà cung cấp chỉ chấp nhận redirect `http://localhost`/`127.0.0.1` chính xác → redirect web `cfharness.localhost` có thể
  bị từ chối: dùng `cfharness mcp login` (127.0.0.1) hoặc khai thêm redirect khi đăng ký client tay.
- Ngắt kết nối xoá cả bản đăng ký DCR (lần sau đăng ký lại).

## 9. Nhà cung cấp dự kiến
Có DCR (kỳ vọng chạy một chạm, CHƯA thử thật trong nhánh này — chỉ test với AS giả): các MCP server từ xa theo spec MCP
2025-06+ như Atlassian (`mcp.atlassian.com`), Linear, Notion, Sentry, Asana, Cloudflare, Stripe, Vercel, PayPal — đều khai PRM +
AS metadata + `registration_endpoint` (dựa trên tài liệu công khai; phải kiểm từng cái).
Cần client_id nhập tay: GitHub (OAuth App/GitHub App, không DCR), Google, Slack, Microsoft Entra ID (không DCR mặc định) và AS
doanh nghiệp tự host (Okta/Auth0 khi tắt DCR). Với các provider này: đăng ký app, khai redirect `http://127.0.0.1:33418/callback`
(CLI) và/hoặc `http://cfharness.localhost:8765/mcp/oauth/callback` (web), rồi `cfharness mcp edit <tên> --oauth-client-id …
--oauth-client-secret '${SECRET}'`.

## 10. Test
Proxy (§8.1): `tests/test_mcp_oauth_proxy.py` (10 test, MCP/AS giả `tests/oauth_fake.py` thêm `/mcp` dạng event-stream có khối chờ
`release`, `/sse` + `/messages`): chuyển tiếp với token hiện tại + làm mới khi sắp hết hạn, 401 upstream → làm mới + gửi lại một lần
(lỗi → 401 không `WWW-Authenticate`), stream không đệm, SSE cũ viết lại `endpoint`, từ chối (thiếu/sai khoá, route lạ, path ngoài danh
sách, không qua route, `Origin`, `Host` lạ, khoá run khác, sau thu hồi, URL đổi) mà server thật không nhận request nào, chỉ bind
127.0.0.1, mcp.json của run không chứa token/`Authorization` + settings không `allowLocalBinding` + route thu hồi sau run, chưa kết
nối → run fail rõ, tắt → đường cũ, cổng bận → dự phòng đường cũ.

`tests/test_mcp_oauth.py` (15 test) + AS/MCP giả `tests/oauth_fake.py` (uvicorn 127.0.0.1; kiểm PKCE S256, redirect_uri,
client_id, `resource` thật): discovery 401→PRM→AS, DCR + dùng lại bản đăng ký, state sai/đã dùng/hết hạn, lỗi nhà cung cấp, code
giả, verifier sai, iss (sai/thiếu khi đã khai), client tay (client_secret_basic), không DCR → lỗi rõ, dự phòng không PRM,
refresh trước run + chèn Bearer vào mcp.json (0600, xoá sau run) + refresh_token xoay vòng, run fail rõ khi chưa kết nối,
refresh thất bại → `error`, token hết hạn bị chặn, probe dùng token, đổi URL bỏ token, ngắt kết nối thu hồi + xoá, luồng web
(connect cần CSRF+khoá, callback công khai không cookie, Host lạ 421, có cookie → 303, disconnect cần xác nhận), CLI `mcp login`
qua daemon thật + listener loopback, HUMAN_ONLY/classifier; không lộ token/code/client_secret vào hub.db(+WAL)/registry.yaml/
logs/audit/kết quả RPC/HTML.

File chạm: mới `mcp_client/oauth.py`, `mcp_client/oauth_cli.py`, `web/mcp_oauth.py`, `web/templates/_mcp_oauth.html`,
`mcp_oauth_go.html`, `mcp_oauth_done.html`; sửa nhỏ `mcp_client/config.py` (`auth`), `registry.py` (`runtime_entry`, trạng thái,
bỏ token khi đổi URL, client_secret rõ → secret store), `wiring.py`, `mcp_server/rpc.py` (MUTATING, audit bỏ code/state, `mcp.list`
có trạng thái), `mcp_server/cli.py` (cờ `--oauth*`, login/logout, hiển thị), `auth.py` (HUMAN_ONLY), `guard/classifier.py`
(login/logout), `runs.py` (1 dòng refresh trước `_prepare_spec`), `web/app.py` (PUBLIC, routes, actions, ô OAuth),
`mcp.html`, `mcp_show.html`. Không migration, không loại event mới.
