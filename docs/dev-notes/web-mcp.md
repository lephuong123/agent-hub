# Web — quản lý MCP kiểu "connectors" (wave 1, nhánh `feat/web-mcp-manage`)

Mục tiêu: trang MCP trên web làm được những gì CLI `cfharness mcp edit|annotate|agent-config` làm, cộng duyệt danh mục và cài.
Code: `web/mcp_pages.py` (route + action), `mcp_client/catalog.py` (danh mục), RPC `mcp.catalog` (`mcp_server/rpc.py`).
Test: `tests/test_web_mcp.py`, `tests/test_mcp_catalog.py` (không mạng thật, không chạy server MCP, không gọi `claude`).

## Trang / route
| Route | Việc |
|---|---|
| `GET /mcp` | như cũ (bảng server, thêm tay, nhập từ Claude Code) + nút **Duyệt danh mục** |
| `GET /mcp/{name}` | chi tiết; cảnh báo tool đáng ngờ; bảng tool có điều khiển; form **Sửa cấu hình** |
| `POST /do/mcp_edit` | action mới trong `ACTIONS` → `mcp.update{name, patch, probe}` (luôn hộp xác nhận; `_audit.channel=web`) |
| `POST /mcp/{name}/tool` | `op` = `block|unblock|allow|unallow|approval_on|approval_off|annotate` → `mcp.update{patch.tools}` |
| `GET /mcp/agent/{id}` | xem `mcp.agent_config` (server + allow/deny + vấn đề). Link từ trang agent và từ "Agent dùng" ở `/mcp/{name}` |
| `GET /mcp/browse?q=&cursor=` | `mcp.catalog{search, limit:20, cursor}` → thẻ; phân trang theo `nextCursor` |
| `GET /mcp/browse/install?name=&opt=` | `mcp.catalog{name, check_auth}` → form cài dựng sẵn |
| `POST /mcp/browse/install` | `mcp.add{probe:false}` → `secret.set` (từng trường bí mật) → `mcp.probe` |

Route MCP được chèn TRƯỚC `/mcp/{name}` cũ (`app._mcp_routes()`), nên `/mcp/browse` không rơi vào `/mcp/{name}`.
Hệ quả: server tên `browse` không mở được trang chi tiết (vẫn quản lý được bằng CLI) — chấp nhận.

## Quy tắc an toàn đã giữ
- Sửa server: form hiện env/header như registry lưu (`${TÊN}`); giá trị rõ ở khoá trông như secret bị che `••••••` và nếu gửi lại
  nguyên chuỗi che thì GIỮ giá trị cũ (không bao giờ ghi chuỗi che). Giá trị mới trông như secret → registry tự tách vào secret store
  (hành vi `_extract_literal_secrets` có sẵn). Xoá dòng = xoá khoá (`None` trong patch, nhờ `env_keys_old`/`header_keys_old`).
- Bảng tool tính trạng thái giống `McpRegistry.tool_rules_for_servers`: Bị chặn (exclude / ngoài include / đáng ngờ chưa cho phép rõ) →
  Luôn hỏi duyệt (destructive hoặc require_approval) → Tự chạy (chỉ đọc hoặc server tin cậy) → Qua guard.
- Cho phép tool đáng ngờ = thêm đúng tên vào `include`; nếu `include` đang rỗng thì thành `["*", tool]` để không vô tình chặn mọi tool khác.
- Thao tác NỚI an toàn (cho phép tool đáng ngờ, bỏ chặn, bỏ luôn hỏi duyệt, ghi đè `destructiveHint=false`/`readOnlyHint=true`) cần
  xác nhận: có JS → `<dialog>`; không JS → trang xác nhận phía server. Ghi đè annotations không gỡ luật tên-xoá / tài nguyên bảo vệ / require_approval.
- Cài từ danh mục: trust mặc định `untrusted`. Trường `isSecret` = `<input type=password>`; giá trị vào secret store tên
  `MCP_<SERVER>_<KHOÁ>` (cùng quy ước bộ tách secret của import), cấu hình giữ `${TÊN}`. Trùng tên secret đã có → từ chối trừ khi tích
  "Ghi đè secret đã có". Khi lỗi / thiếu xác nhận, form được vẽ lại KHÔNG kèm giá trị secret. Không vào audit (audit chỉ có tên).
- Dữ liệu registry là của bên thứ ba: chỉ là chữ (autoescape); link repo/website chỉ nhận `http(s)://`; khoá `_meta.cfharness/curated`
  từ registry bị bỏ qua (chỉ danh sách tuyển chọn trong gói mới gắn được nhãn của hub); `get(name)` trả mục khác tên → không tin.

## Danh mục (`mcp_client/catalog.py`)
- API (kiểm bằng curl 2026-10-26): `GET https://registry.modelcontextprotocol.io/v0/servers?search=&limit=&cursor=&version=latest`,
  một mục `GET /v0/servers/<tên url-encode>/versions/latest`. Remote `type` = `streamable-http` (→ transport `http`) hoặc `sse`; remote
  có thể có `variables` (`{region}` trong URL) → ô nhập riêng, chỉ nhận `[A-Za-z0-9._~-]`.
- Cài tự động: remote http/sse, npm (`npx -y <id>@<ver>`), pypi (`uvx <id>==<ver>`) chạy stdio. `packageArguments` có giá trị cố định →
  nối vào lệnh; có biến/bí mật → ô nhập. oci/nuget/mcpb và gói tự chạy http → "chưa cài tự động được" (thêm tay).
- Cache RAM + đĩa `HUB_HOME/cache/mcp_catalog/` TTL 10 phút, timeout 8 s. Lỗi mạng → cache cũ (hết hạn) → danh sách tuyển chọn `CURATED`
  (playwright, github remote, filesystem, fetch, atlassian remote, figma remote — xem chú thích trong code về cách đã kiểm).
- OAuth: remote không có header bắt buộc mà POST `initialize` không token trả 401 → nhãn **Cần đăng nhập (OAuth)** (chỉ kiểm https công
  khai — không gõ localhost/IP nội bộ; cache 10 phút). Trang cài có khung giữ chỗ "Đăng nhập (sắp có)". Đăng nhập OAuth thật: nhánh
  `feat/mcp-oauth` (không làm ở đây). Hiện vẫn cho cài server OAuth; probe sẽ báo 401.
- `mcp.catalog` CHỈ ĐỌC (không trong `MUTATING`); mạng chạy trong thread (`asyncio.to_thread`) để không chặn daemon.

## File dùng chung đã chạm (tối thiểu)
- `web/app.py`: bỏ handler `mcp_show` cũ (chuyển sang `mcp_pages`), `*_mcp_routes()` trong danh sách route, `register_actions` cuối file.
- `mcp_server/rpc.py`: method `mcp.catalog`.
- Template: `mcp.html` (nút/link danh mục), `agent.html` (link "xem cấu hình MCP"), `mcp_show.html` (viết lại); mới: `mcp_browse.html`,
  `mcp_install.html`, `mcp_agent.html`. `app.css`: thêm khối `.mcp-*` ở cuối.

## Việc dở / chưa chắc
- Chưa có ảnh chụp màn hình mới cho các trang này (chỉ kiểm bằng test HTML).
- Cài từ danh mục chưa hỗ trợ oci (docker) — github chỉ cài được bản remote.
- `secret.set` chạy SAU `mcp.add`; nếu `secret.set` lỗi giữa chừng, server đã có trong registry nhưng thiếu secret (trang chi tiết báo "Thiếu secret").
- Chưa có CLI tương ứng cho danh mục (`cfharness mcp catalog`) — RPC đã sẵn.
