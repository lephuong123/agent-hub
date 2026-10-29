# Bớt hỏi duyệt, trust full theo agent, lệnh cài đặt quyền trong chat (2026-10-27)

Mục tiêu: người ít phải duyệt tay. Bỏ những lần hỏi báo nhầm, cho phép người tắt hẳn rào chắn của từng agent khi muốn, và đổi các
setting quyền ngay trong `cfharness chat` thay vì thoát ra CLI.
Test: `tests/test_trust_and_noise.py`.

## Dữ liệu từ hub thật (7 ngày tới 2026-10-27)
24 yêu cầu duyệt, cho phép 100%, người quyết 18. Cả 24 đều đến từ hook PreToolUse của guard (`channel=hook`). Không yêu cầu nào đến
từ prompt quyền của CLI, nên chỉ đổi `--permission-mode` thì không bớt được lần hỏi nào.

| Luật | Người duyệt | Xử lý |
|---|---|---|
| `ssrf_private_resolved` (+ `chrome_sensitive`) | 7 | host dev tin cậy theo agent: `/trust-host`, `guard sandbox dev-host` |
| `write_outside_workspace`, `git_reset_hard`, `code_write_dynamic` | 5 | đã có từ profile balanced (2026-10-26) |
| `mcp_destructive_hint` (playwright của điều phối viên) | 3 | `/mcp-safe playwright 'browser_*'` |
| `code_delete_api` (`node scripts/i18n-guard.mjs`) | 2 | `guard.trusted_repo_scripts` |
| `delete_not_run_created` (thư mục tạm) | 2 | `guard.task_created_deletes` |

## Bớt báo nhầm
- **Host dev tin cậy** (`GuardService.sandbox_dev_host`, RPC `guard.sandbox.dev_host`, HUMAN_ONLY). Lưu `dev_hosts` trong
  `hub_state.sandbox_overrides[agent]`, cùng chỗ với ngoại lệ loopback/domain. `net_check` bỏ qua `ssrf_private_resolved` cho ĐÚNG
  host đó của ĐÚNG agent đó; run của điều phối viên vẫn bị cấm như cũ. Host cũng được thêm vào `allow_domains` để Bash ra mạng tới nó.
  `chrome_sensitive` (javascript_tool trên Chrome thật) vẫn hỏi, vì hook không biết tab đang ở host nào.
- **Script đã commit của repo** (`guard.trusted_repo_scripts`, balanced/autonomous bật). Lệnh `node|python <file>` có file được git
  track, `git status` sạch, và commit cuối chạm nó có thời điểm TRƯỚC run đầu của task → API xoá trong mã là `write`
  (`repo_script_run`). Script agent vừa viết, vừa sửa, hoặc vừa commit trong task vẫn là `code_delete_api`. Các luật khác của
  `scan_code` (socket/module/HUB_HOME, keychain, lệnh shell lồng, SQL) vẫn áp.
- **Xoá thứ do task tạo** (`guard.task_created_deletes`, balanced/autonomous bật). Mốc "file run tự tạo" lấy sớm hơn giữa session
  hiện tại và run đầu của task. Thư mục nằm trong thư mục tạm của hệ thống (`tempfile.gettempdir()`, `%TEMP%`, `%TMP%`, `$TMPDIR`), được
  tạo sau mốc đó và không chứa cwd của run → không duyệt từng file con (thư mục kiểu node_modules thường vượt `max_walk`).
- **Nhắc lại** (`notify.reminder_max_count`): null = không giới hạn (như cũ), 0 = không nhắc, n = tối đa n lần mỗi yêu cầu. Yêu cầu
  vẫn chờ người (Q-04), chỉ thôi làm phiền.

## Trust full theo agent
Quyết định của chủ hub: được phép tắt hẳn rào chắn cho một agent, kể cả vùng dữ liệu/chính sách của hub.

- Lưu ở `hub_state.agent_trust = {agent_id: {mode: "full", until, by, channel, at}}`, NGOÀI cấu hình agent. Vòng học, ledger,
  export/clone và điều phối viên không chạm được. Chỉ RPC `guard.trust.set` (HUMAN_ONLY + `require_human` ở service) đổi được.
  Có audit `guard.trust` và event `guard.trust` (mức summary).
- **Guard** (`GuardService.check`): run của agent đang trust (trừ run review/learning/eval của hub) → `allow` mọi tool call, kể cả
  verdict `forbidden`, vùng hub/policy, push, credential, mạng nội bộ. Vẫn phân loại để ghi `guard_decisions` (`classifier=trust`) +
  audit `guard.allow` đúng mức. ESTOP vẫn chặn (kiểm trước trust). Khoá Claude in Chrome vẫn áp vì đó là điều phối tài nguyên dùng
  chung, không phải quyền.
- **Run mới** (`RunManager._prepare_spec`): `--permission-mode bypassPermissions` trên MỌI workspace, kể cả `dir` (ngoại lệ NFR-15 do
  người chọn; `RunSpec.trust_full` cho `build_argv` bỏ kiểm `allow_bypass`/workspace). settings.json không có `permissions.deny` của hub,
  `sandbox.enabled=false`, bỏ `static_disallowed_tools` + mẫu chặn theo repo. Vẫn GIỮ hook PreToolUse để ghi quyết định và chặn khi
  ESTOP. Run đang chạy giữ sandbox/cờ cũ cho tới khi kết thúc; phần guard có hiệu lực ngay.
- `until` (tuỳ chọn): hết hạn thì trust tự hết hiệu lực ở cả guard lẫn run mới.
- Hiển thị: `⚠TRUST` ở `top` và bảng `/agents`; `/perm` và `cfharness guard trust list` liệt kê.
- **Mặc định cho agent mới** (`guard.trust_new_agents: off|full`, mặc định `off`; đặt bằng `config.set` nên chỉ người đổi được).
  `full` → listener `agent.created` (`GuardService._trust_new_agent`) ghi trust full không hết hạn, `by=default, channel=config`, kèm
  audit/event `guard.trust`. Áp cho MỌI đường tạo agent: người, điều phối viên, clone, import. Agent có sẵn giữ nguyên; tắt từng agent
  bằng `guard trust <agent> off`. Rủi ro đã chấp nhận: điều phối viên (có thể dính prompt injection) tạo agent mới là có agent không rào chắn.
- Rủi ro đã chấp nhận: agent trust có thể sửa `config.yaml`/`hub.db`/audit/policy, đọc secret của hub, push, gọi mạng nội bộ. Dấu vết
  trong `guard_decisions`/audit chỉ đáng tin tới khi agent tự sửa chúng.

## Lệnh chat (`manager/settings_cmds.py`)
Do client xử lý, không gửi tới model, nên điều phối viên không gõ được. Mỗi lệnh gọi đúng RPC của CLI (cổng HUMAN_ONLY + audit ở daemon),
in giá trị cũ → mới và hỏi y/N. `/trust … full` và `/profile autonomous` xác nhận 2 bước. Dùng chung cho REPL và màn chat fullscreen.

| Lệnh | RPC |
|---|---|
| `/perm` | `approval.profile`, `guard.trust.list`, `guard.sandbox.list`, `approval.stats{days:7}`, `config.get` — kèm gợi ý theo luật |
| `/profile [strict\|balanced\|autonomous]` | `config.set approval.profile` |
| `/trust [<agent> full\|off [4h]]` | `guard.trust.set` / `guard.trust.list` |
| `/trust-host <agent> <host> [--remove]` | `guard.sandbox.dev_host` |
| `/sandbox <agent> localhost on\|off` · `/sandbox <agent> domain <d> [--remove]` | `guard.sandbox.set` / `guard.sandbox.allow_domain` |
| `/mcp-safe <server> <glob> [--clear]` | `mcp.update` (annotations `destructiveHint=false`); Claude in Chrome → `config.set runtime.claude.chrome.annotations` |
| `/delegate on\|off [file,git,mcp]` | `config.set approval.delegate_to_manager` |
| `/reminders on\|off\|once\|<n>` | `config.set notify.reminder_max_count` |

CLI tương ứng: `cfharness guard sandbox dev-host <agent> <host> [--remove]`, `cfharness guard trust <agent> full|off [--for 4h]`,
`cfharness guard trust list`.

## Chưa làm
- Telegram/Slack chưa có các lệnh này (kênh từ xa cần nút xác nhận riêng).
- `docs/schema/event.v1.json` chưa sinh lại cho event `guard.trust`: `python -m cfharness.event_schema --write docs/schema/event.v1.json`.

## Không hỏi lại theo agent + luật (2026-10-29)
Giữa "luôn cho phép" (chỉ đúng một thao tác, chỉ uncertain) và trust full (bỏ mọi rào chắn): người duyệt một lần và chọn
"Không hỏi lại luật này" → từ nay agent đó không bị hỏi duyệt lại với `matched_rule` đó, KỂ CẢ destructive, ở mọi task/run.
Lý do: hub thật hỏi lặp cùng luật (vd `code_delete_api` 6 lần cho dev-du-lieu, `schedule_persistence` cho `npm run deploy` mỗi lần deploy).

- Lưu `hub_state.agent_rule_grants = {agent: {rule: {until, by, channel, at, approval_id}}}` (ngoài cấu hình agent như trust).
  `until` None = không hạn. Chỉ người: `approval.approve{grant_rule: true, grant_for_s?}`, `guard.grant.set|rm` (HUMAN_ONLY +
  `require_human`); `guard.grant.list` đọc tự do. Audit + event `guard.rule_grant`.
- `GuardService.check`: sau forbidden/khoá Chrome/`needs_human`, TRƯỚC `auto_allow` → allow, `guard_decisions.classifier=rule_grant`.
  forbidden, ESTOP, khoá Chrome vẫn áp; run review/eval/learning không hưởng (cùng loại trừ với trust).
- Khi đặt grant: mọi approval CHƯA quyết cùng agent + luật được cho phép luôn (`released`).
- Bề mặt: web nút "Luôn cho phép luật này" → trang bước 2 bắt buộc (chọn không hạn/8h/24h/7d/30d); Telegram/Slack nút
  "Không hỏi lại luật này…" → bước 2 "không hạn" | "8 giờ"; CLI `cfharness approve <id> --rule [--for 8h]`,
  `cfharness guard grant list|set|rm` (`guard grant` ∈ `HUB_HUMAN_ONLY_CLI` → agent gõ = forbidden).
- Rủi ro đã chấp nhận: grant theo LUẬT, không theo đích — `delete_not_run_created` cho một agent nghĩa là agent đó xoá được mọi file
  không do run tạo mà không hỏi (trong giới hạn sandbox/workspace). Người chọn thời hạn để thu hẹp.
- Chưa làm: TUI `top`/`cfharness chat` chưa có phím/lệnh riêng (dùng CLI hoặc web); MCP `hub_approve` chưa có tham số grant.
  Test: `tests/test_rule_grant.py`, `tests/test_web.py::test_rule_grant_button_two_steps_and_duration`.
