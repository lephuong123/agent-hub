# Allowlist domain WebFetch theo agent (`tool_policy.web_allow_domains`)

## Vì sao
"Rủi ro còn lại" của M7-core.md §9 và hardening-SEC.md §WEB-1: WebFetch là kênh RA dữ liệu (gửi qua query URL khi bị prompt injection).
WEB-1 đã có `guard.web.allow_domains` cho cả hub (ngoài danh sách → giữ chờ người) và `manager.web_domains`, nhưng chưa khoá được
từng agent vào vài domain nó thật sự cần.

## Hành vi
- Trường mới trên spec agent: `tool_policy.web_allow_domains: list[str] | null`.
  - `null` (mặc định, cũng là mọi agent cũ) = **y như trước**: không có kiểm mới nào chạy (test `test_not_set_*`, cả bản ghi cũ không có khoá).
  - danh sách glob (`docs.python.org`, `*.github.io`) = WebFetch (và Claude in Chrome `navigate`/`tabs_create`, cùng luật WebFetch)
    tới host khác bị **TỪ CHỐI thẳng** (`forbidden`, rule `web_agent_domain_not_allowed`) — không mở approval, không cho LLM thứ hai
    hạ. Ghi `guard_decisions` + audit `guard.deny` + event `guard.blocked` như mọi forbidden.
  - `[]` = chặn mọi WebFetch.
- Kiểm hợp lệ (`url_safety.normalize_agent_domains`, validator của `ToolPolicy`): chữ thường, bỏ dấu chấm cuối, bỏ trùng, IDN →
  punycode; từ chối `*` (không giới hạn = `null`), scheme/đường dẫn/cổng/`user@`, khoảng trắng, sai kiểu.
- So khớp (`GuardService.agent_web_check`, gọi đầu `net_check` và trong nhánh trust full): host lấy từ `static_check` (chữ thường,
  IDN → punycode, bỏ cổng và dấu chấm cuối) rồi `domain_matches`. `*.x.com` không gồm `x.com`; hậu tố giả (`x.com.evil.com`,
  `evilx.com`), `%2e`, chữ đồng hình Cyrillic, IP literal mọi dạng (thập phân/hex/số nguyên/IPv6-mapped) không khớp tên miền.
  URL không tách chắc chắn được host (userinfo `a@b`, `\`, scheme khác http/https, thiếu scheme, rỗng) → từ chối.
- Chỉ NÂNG mức: `guard.web.deny_domains`, metadata, DNS về mạng nội bộ, allowlist toàn hub vẫn áp như WEB-1 với host trong danh sách agent.
- Agent trust full: allowlist theo agent VẪN áp (giới hạn người đặt riêng cho agent đó, cụ thể hơn "tin hết").
- `guard.enabled=false` (không có hook lọc domain): agent có allowlist → `WebFetch` vào `--disallowedTools` và bị bỏ khỏi
  `allowed_tools` (fail-closed); agent không đặt → như cũ.
- Redirect: hook chỉ thấy URL ban đầu; theo hiểu biết hiện tại `claude` báo redirect khác host cho model và cần lời gọi WebFetch
  mới (qua hook lại → bị từ chối). Redirect cùng host thì không vấn đề. Chưa kiểm chứng trên claude thật (như WEB-1 rủi ro 2).

## Quyền đổi (như các thay đổi `tool_policy` khác)
- Người (CLI `agent edit --web-domains a.com,*.b.io|any|none`, web form, RPC `agent.update` từ bề mặt người): tự do.
- Điều phối viên (`manager/permissions.py`, `TOOL_POLICY_FIELDS`): đặt khi tạo agent; sửa agent khác chỉ được THU HẸP — bỏ allowlist
  hoặc thêm mẫu chưa có (kể cả `*.a.com` khi đang có `a.com`) → từ chối ("chỉ người được nới"), giống luật không bỏ `disallowed_tools`.
- Vòng học / `propose_config_change` (`learning/guard.py`): nới = `self_escalation`; thu hẹp được.
- Agent xin người qua nút duyệt (`owner_actions`, `TOOL_POLICY_FIELDS`): được — người quyết.
- Hiển thị: `cfharness agent show` (`web_allow_domains: …`), view Rich (`domain WebFetch`), trang agent web (chip domain /
  "không giới hạn" / "chặn mọi WebFetch"), form sửa có ô + hộp xác nhận "Đổi quyền" khi giá trị đổi. Ô trống trên web = không giới hạn;
  agent đang `[]` mà ô trống → giữ nguyên (bỏ chặn qua `--web-domains any`).

## Test
`tests/test_web_agent_allowlist.py` (46 ca): chuẩn hoá/kiểm hợp lệ, luật nới/hẹp, 4 ca + bản ghi cũ + guard tắt "chưa đặt = như cũ",
8 URL được qua, 22 URL vượt rào bị từ chối + audit, IDN, `[]`, tool khác không ảnh hưởng, deny/metadata của hub vẫn áp, trust full,
Chrome, `guard test`, guard tắt, giá trị sửa tay sai kiểu (→ hẹp nhất), quyền manager/học/nút duyệt, CLI + form web.
38 ca đỏ trên code cũ; 8 ca hồi quy "chưa đặt" xanh trên cả hai.

## Rủi ro còn lại
- DNS rebinding của tên miền NẰM TRONG danh sách vẫn như WEB-1 (hook không pin IP).
- WebSearch không có URL: allowlist không áp (tắt bằng `tools`/`disallowed_tools` nếu cần).
- Bash `curl` không dùng danh sách này (đã có `sandbox.allowed_domains` theo agent; trên Windows không có sandbox).
