# Hồ sơ phê duyệt (`approval.profile`) + giao điều phối viên duyệt (2026-10-26)

Mục tiêu: agent ít phải dừng xin phép người, nhưng không nới bất kỳ thứ gì trong danh sách "không bao giờ tự động".
Migration **v18** (`approval_requests.routed_to|delegated_at|escalated_at|escalation_note|verdict_targets|timeout_action`).
Test: `tests/test_approval_profiles.py`.

## Dữ liệu từ hub thật (7 yêu cầu)
| Số | Luật | Thực chất | Cách xử lý |
|---|---|---|---|
| 4 | `mcp_destructive_hint` | playwright `browser_navigate`/`browser_close` (điều phối viên `dieu-phoi`) — server tự gắn `destructiveHint` | Ghi đè annotations do NGƯỜI đặt: `cfharness mcp annotate playwright 'browser_*' --not-destructive`, hoặc "Luôn cho phép tool này cho agent X" (luật `mcp_tool`) khi duyệt |
| 1 | `write_outside_workspace` | web-coder Edit trong `C:\WorkSpace\_wt\lh-54831-workflows\…`; task có `workspace = C:\WorkSpace\workflowswebapp` (dir), nội dung task do điều phối viên viết bảo agent TỰ tạo worktree ở `_wt\…` | Xem "Nguyên nhân gốc" dưới |
| 1 | `code_write_dynamic` (uncertain) | `python - <<EOF` ghi file test, do `guard.hold_uncertain: true` | Profile `balanced`: uncertain → cho chạy + audit |
| 1 | `git_reset_hard` | `git reset -q --hard origin/dev` trong worktree vừa tạo, ngay sau fetch | Probe git: worktree sạch + commit hiện tại còn trên ref khác → `write` |

Xem lại số liệu bất kỳ lúc nào: `cfharness approvals stats [--days 30] [--json]` (theo luật / agent / tool / kết quả / ai quyết /
`routed_to` + phân bố thời gian chờ + số lần hub tự cho phép không mở approval).

### Nguyên nhân gốc `write_outside_workspace`
- Run của task lấy cwd = `workspace.path` của task. Worktree được agent tạo GIỮA run (run đầu `r_8c7f…` bị huỷ đã tạo nó — thư mục có
  trước session của run sửa file), nằm ngoài cwd → mọi Edit/Write trong đó là "ngoài workspace". cwd không đổi được giữa chừng (`--resume`
  của claude gắn với cwd).
- Thêm lỗi phụ trong dispatcher: `workspace.kind = worktree` bỏ mất `path` (`_workspace` chỉ trả path cho `dir`) → run chạy ở scratch,
  `--worktree` không bao giờ được truyền. Đã sửa: `worktree` + `path` (repo) → cwd = repo + `claude --worktree <tên>`.
- Sửa:
  1. `workspace.extra_write_roots` của task (CLI `task create --extra-write-root`, tool `task_create{extra_write_roots}` của điều phối
     viên): ≤ 5 thư mục tuyệt đối, PHẢI nằm trong `workspace.allowed_roots`, không phải/không chứa chính một gốc, không phải/không chứa
     vùng bảo vệ (`validate_workspace`), được phép chưa tồn tại. Guard và sandbox (`allowWrite`) của run dùng nó; guard kiểm LẠI theo
     allowed_roots mỗi lần.
  2. `guard.own_worktree_writes` (balanced/autonomous): Edit/Write vào git worktree LIÊN KẾT (`.git` là file) cùng repo
     (`git-common-dir` trùng) với cwd của run, nằm trong `workspace.allowed_roots`, và thư mục worktree được tạo SAU lúc run đầu tiên
     của TASK vào hàng đợi → `write` (`own_worktree_write`). Worktree của người/ticket khác (tạo trước) vẫn hỏi.
  3. Persona điều phối viên: khi bảo agent làm trong worktree riêng → đặt `extra_write_roots`.

## Profile
`approval.profile` (mặc định **balanced**). Profile chỉ đặt sẵn khoá; khoá đặt rõ trong `config.yaml` luôn thắng (`HubConfig.explicit`).

| Khoá | strict (= trước đây) | balanced (mặc định) | autonomous |
|---|---|---|---|
| `guard.hold_uncertain` | true | false — uncertain cho chạy + audit, TRỪ vùng hub/policy/mạng, MCP `require_approval`, MCP chưa probe/untrusted, mọi thứ "không bao giờ" | false |
| `approval.checkpoint_auto_allow` | false | true — phá huỷ mà mọi đích nằm trong repo có checkpoint của CHÍNH run và `run revert` khôi phục được (file track ở HEAD lúc checkpoint / có bản lưu / do run tạo sau checkpoint; reset/checkout chỉ khi HEAD chưa đổi) | true |
| `guard.safe_reset_probe` | false | true — `git reset [-q] --hard <ref>` trong worktree liên kết hoặc scratch: cây sạch (kể cả untracked), không ghi đè file ignore, commit hiện tại còn trên ref khác | true |
| `guard.own_worktree_writes` | false | true | true |
| `guard.trusted_repo_scripts` | false | true — `node\|python <script>` đã commit trước task, chưa sửa → API xoá trong mã không tính (trust-and-chat-settings.md) | true |
| `guard.task_created_deletes` | false | true — xoá thứ do task tạo; thư mục tạm hệ thống do task tạo (trust-and-chat-settings.md) | true |
| `approval.delegate_to_manager.enabled` | false | true | true |
| `approval.scratch_auto_allow` | false | false | true — run scratch/worktree của agent có `tool_policy.allow_bypass` (người đặt): phá huỷ lớp file/git mà mọi đích trong cwd → cho phép (NFR-15 không đổi) |

`cfharness approvals profile` in bảng hiệu lực (giá trị + nguồn `profile`/`explicit`). Mọi lần hub tự cho phép ghi `guard_decisions`
(`classifier = checkpoint|autonomous_scratch`) + audit `guard.allow` (`decided_by`, `auto_reason`).

Chỉ NGƯỜI đặt: `config.set` ∈ `auth.HUMAN_ONLY`; nhánh `approval`/`guard` ∈ `integrity.SECURITY_KEYS` (ghi thẳng config.yaml → khôi
phục); agent gõ `cfharness config set …` = `hub_human_only` (cấm thẳng); vòng học chặn khoá `approval.*`/`guard.*`; web `SAFE_CONFIG`
không có. Giá trị lạ sửa tay: profile → `strict`, `hold_uncertain` → true, khoá khác → false.

## KHÔNG BAO GIỜ tự động (mọi profile) — `guard/profiles.never_automatic`
Một hàm duy nhất, dùng cho MỌI đường tự động (checkpoint, scratch, uncertain không giữ, giao điều phối viên, và kiểm lại khi điều
phối viên quyết):
- verdict `forbidden`; tài nguyên được bảo vệ (`protected_id`, luật `protected_*`);
- lớp `hub`/`policy`/`guard`/`network` (dữ liệu/secret/chính sách của hub, SSRF, domain người chặn/ngoài allowlist);
- `git push` mọi dạng, force push, refspec, nhánh bảo vệ, đổi remote;
- thông tin xác thực: `git_credential`, keychain, token forge, secret hub; vùng thực thi ngoài sandbox, cơ chế chạy trễ;
- đường dẫn (hoặc cwd của lệnh git) ngoài `workspace.allowed_roots`, hoặc không xác định được đích;
- ESTOP (không tự cho phép, không giao); MCP `require_approval`, MCP ngoài run; lệnh xin `dangerouslyDisableSandbox`.
Test: `test_never_protected_resource`, `test_never_hub_policy_secrets_zone`, `test_never_forbidden_verdict`,
`test_never_git_push_force_push_remote_change`, `test_never_paths_outside_allowed_roots`, `test_never_credential_access`,
`test_never_sandbox_exception`, `test_never_mcp_require_approval_or_network`, `test_never_while_estop`,
`test_never_auto_end_to_end_push_in_checkpointed_repo`.

## Giao điều phối viên duyệt — ranh giới MỚI
Trước: "điều phối viên KHÔNG được duyệt thao tác phá huỷ". Nay (quyết định của chủ hub): điều phối viên được quyết một tập approval
NGƯỜI đã giao, không hơn.
- `approval.delegate_to_manager: {enabled, classes: [file, git, mcp], timeout_s: 600}` (lớp giao được tối đa: file, git, mcp, db, system).
- Approval mở cho agent KHÁC (không phải run/agent điều phối viên hiện tại hay trước đây), lớp trong `classes`, ngoài danh sách "không
  bao giờ", có điều phối viên sẵn sàng (chỉ định, không tạm dừng/xoá, không ESTOP, nhịp bật) → `routed_to="manager"`, event
  `approval.delegated`, sự kiện `[approval_delegated]` cho nhịp + đánh thức sớm (`wake_soon`). Không thông báo người (Telegram/Slack lọc
  approval.open/waiting_human/reminder khi `routed_to=manager`; nhắc lại bỏ qua).
- Điều phối viên: `approval_list` (đọc), `approval_decide{approval_id, decision: allow|deny, reason}`, `approval_escalate{approval_id,
  note}` → RPC `approval.manager_decide` (HUMAN_ONLY + `MANAGER_ALLOWED`; luật ở `manager/permissions.check_rpc` lớp 1+2 và
  `ApprovalService.manager_decide` lớp 3): chỉ `routed_to=manager` + còn chờ, không approval của run điều phối viên, không bao giờ
  `always`, cần lý do, kiểm lại lớp + "không bao giờ" từ verdict đã lưu (`verdict_targets`). Quyết định audit actor `manager:<agent>`,
  ghi chú `[điều phối viên] <lý do>`; agent bị từ chối nhận "The coordinator (manager agent) DENIED …".
- Điều phối viên chuyển lên, quá `timeout_s`, hoặc không còn sẵn sàng (vòng `approval.reminder_check_s`) → `routed_to="human"`,
  `approval.escalated` (Telegram có nút như approval.open; Slack; web). Người quyết được BẤT KỲ lúc nào (kể cả khi đang giao).
- Hợp đồng với chat UI: trường `routed_to` ("manager"|"human"|null = approval cũ), event `approval.delegated`/`approval.escalated` mức
  `summary` (đến `events.subscribe` mức summary), `routed_to` có trong `approval.list|get`, `tui.snapshot`, payload approval.open/
  waiting_human/reminder.
- Approval của chính điều phối viên (vd playwright của `dieu-phoi`) KHÔNG bao giờ giao — dùng ghi đè annotations hoặc luật `mcp_tool`.

## Hết giờ giữ: `approval.on_timeout`
`wait` (mặc định, Q-04 giữ nguyên: run tạm dừng `waiting_human`, không bao giờ tự từ chối) | `deny_and_continue` (opt-in, theo agent:
`approval.on_timeout_by_agent: {<agent>: deny_and_continue}`): agent nhận từ chối kèm hướng dẫn ("làm cách khác hoặc bỏ qua và báo
cáo"), run CHẠY TIẾP, approval vẫn `waiting_human` (`timeout_action=deny_and_continue`, người vẫn được nhắc). Người cho phép sau: run
còn chạy → nhắn vào run (vé một lần dùng khi agent làm lại đúng thao tác); run đã kết thúc → resume session với vé (như waiting_human).
Từ chối sau → không làm gì thêm.

## MCP: ghi đè annotations + "luôn cho phép tool này"
- `tools.annotations: {glob: {destructiveHint?, readOnlyHint?}}` trong registry (DB), đặt bằng `cfharness mcp annotate <server> <glob>
  --not-destructive|--destructive|--read-only|--not-read-only|--clear` (= `mcp.update`, HUMAN_ONLY; `mcp annotate` từ agent = cấm).
  Ghi đè `destructiveHint=false` bỏ cả khớp mô tả xoá; KHÔNG gỡ luật tên-xoá, tài nguyên bảo vệ, `require_approval`. Không ship mặc định
  nào (kể cả playwright: `browser_navigate` là đường ra mạng ngoài sandbox — để người quyết).
- Luật `mcp_tool` (policy/always_allow.yaml): agent (bắt buộc) + ĐÚNG tên `mcp__<server>__<tool>`, chỉ hạ verdict do annotations của
  server (`mcp_destructive_hint`, `mcp_unknown_tool`, `mcp_untrusted_write`). Hiện qua `always_offer` (kind `mcp_tool`, `confirm` = tên
  tool) ở CLI `approve --always`, TUI, web, Telegram, MCP `hub_approve(confirm_rule=always_offer.confirm)`.

## Lưu ý cài đặt cũ (migration)
- Cài đặt KHÔNG có `guard.hold_uncertain` trong config.yaml → chuyển sang balanced: uncertain thường cho chạy + audit. Muốn giữ như
  cũ: `cfharness config set approval.profile strict` (hoặc đặt rõ `guard.hold_uncertain: true`).
- Cài đặt có `guard.hold_uncertain: true|false` → giữ nguyên giá trị đó.
- `notify.*.events` người tự đặt mà có `approval.open` → tự nhận `approval.escalated`.
- Persona điều phối viên (`templates/manager.yaml`) chỉ áp cho agent tạo mới; agent điều phối viên đã có vẫn được mô tả tool mới +
  prompt nhịp (`[approval_delegated]`). Muốn cập nhật SOUL: sửa tay hoặc tạo lại từ template.
- `docs/schema/event.v1.json` chưa sinh lại (ngoài phạm vi sửa docs) — test chỉ cho THÊM loại nên vẫn xanh; chạy
  `python -m cfharness.event_schema --write docs/schema/event.v1.json` khi được phép.

## Rủi ro còn lại
- Checkpoint chỉ chụp trạng thái LÚC run bắt đầu: thao tác tự cho phép làm mất chỉnh sửa do chính run làm sau đó; người sửa cùng repo
  trong lúc run đang chạy có thể mất phần sửa đó (revert khôi phục về trạng thái trước run, không phải trạng thái người).
- `extra_write_roots` không lấy khoá repo cho thư mục thêm.
- Điều phối viên là LLM: dữ liệu approval (lệnh, đích) là dữ liệu agent; giới hạn cứng là lớp + danh sách "không bao giờ", không phải
  phán đoán của nó. Thu hẹp bằng `approval.delegate_to_manager.classes` hoặc tắt `enabled`.
