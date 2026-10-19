# M7 MGR-CORE — Điều phối viên (agent harness): ghi chú phát triển

Phạm vi: `docs/plan/M7-manager.md` phần MGR-CORE — chỉ định manager (HUMAN_ONLY), vai trò `manager` + bộ tool trong hub-self MCP,
kiểm quyền ở DAEMON, audit `manager:<agent>` + ledger, hội thoại liên tục theo kênh, heartbeat, event, CLI, template persona.
Migration **v13** (`manager_conversations`, `manager_messages`). Test: `tests/test_manager.py`, `tests/test_manager_security.py`
(helper `tests/m7_fake.py`). `uv run pytest -q`: **537 passed, 1 skipped** (~3 phút).

## Hợp đồng RPC cho MGR-UI — chữ ký CUỐI CÙNG (`manager/rpc.py`)
| Method | Quyền | Params → kết quả |
|---|---|---|
| `manager.get` | đọc | `{}` → `{agent_id\|null, conversations:[Conv], last_tick, next_tick, tick:{heartbeat, interval_s, pending_events, last_tick_run_id, last_reason, last_skip, running}, designated_at}` |
| `manager.set` | **HUMAN_ONLY** (+ `require_human` tầng service) | `{agent_id\|null}` → như `manager.get` |
| `manager.chat` | **HUMAN_ONLY** | `{channel, text, conversation_id?, reuse_latest?}` → `{conversation_id, run_id, session_id?, resumed?, steered?}` |
| `manager.conversations` | đọc | `{channel?, limit?}` → `[Conv]` (mới cập nhật trước) |
| `manager.history` | đọc | `{conversation_id, limit?=100}` → `[{id, role, text, run_id, ts}]` cũ → mới; `role` ∈ `user\|manager\|notify\|tick` |
| `manager.tick` | **HUMAN_ONLY** | `{force?=true}` → `{run_id\|null, skipped?, reason?, events?, conversation_id?}` |
| `managerself.call` | agent (chỉ hub-self của CHÍNH run manager) | `{run_id, op, args}` — không dùng từ UI |

`Conv = {id, channel, agent_id, session_id, turns, title, messages (số tin), last_run_id, created_at, updated_at}`.
- `manager.chat` KHÔNG có `conversation_id` → hội thoại MỚI (khớp stub của MGR-UI); `reuse_latest=true` → hội thoại gần nhất của đúng
  `channel` (CLI dùng mặc định; Telegram nên dùng để "mỗi chat một hội thoại"). Hội thoại đang có run `running` → tin được **steer** vào run đó
  (`steered=true`, cùng `run_id`); run đang `queued/waiting_human` → `Conflict` (thử lại sau). Chưa chỉ định → `Conflict` "chưa có điều phối viên".
  Kênh `heartbeat` dành riêng cho nhịp. Hội thoại của manager cũ (đã đổi manager) → `Conflict` (mở hội thoại mới).
- Event (đều có `agent_id` = manager, `run_id`):
  - `manager.reply{conversation_id, run_id, text, channel, kind: reply|tick|notify, ok, outcome?, notify_kind?}` — `reply` khi run hội thoại kết
    thúc (lỗi → `ok=false`, text mô tả lỗi); `tick` khi run nhịp kết thúc (kênh `heartbeat`, UI có thể bỏ qua); `notify` khi manager gọi
    `notify_owner` (đích = hội thoại của run, hoặc với tick: hội thoại NGƯỜI hoạt động gần nhất; `conversation_id=null` nếu chưa có).
  - `manager.action{tool, target, summary, audit_id, ok, error?}` — mọi thao tác GHI (kể cả bị từ chối: `ok=false`, `error`=lý do).
    `target` dạng `"agent:<id>" | "task:<id>" | "team:<id>" | "run:<id>"` hoặc null.
  - `manager.tick{status: started|skipped|failed, reason, events?, dropped?, conversation_id?}`, `manager.designated{agent_id, previous, by, channel}`.

## 1. Đã làm
### Chỉ định + quyền (`manager/permissions.py`, `auth.py`, `daemon.py`)
- `hub_state.manager = {agent_id, set_at, by, channel, history}` (hub.db — agent không ghi được; KHÔNG ở config.yaml). `history` = mọi agent
  từng là manager → vẫn là "agent manager" (không bị manager mới sửa).
- `auth.HUMAN_ONLY` += `manager.set|chat|tick`; classifier `HUB_HUMAN_ONLY_CLI["manager"] = {set, chat, tick}` (forbidden từ Bash của agent).
  `manager.chat` là HUMAN_ONLY để agent khác không "ra lệnh" cho manager (leo thang quyền qua hội thoại).
- `auth.manager_caller(agent, run)` → `Caller(manager=agent, human=False)`, CHỈ dựng trong daemon. `Daemon._dispatch`: người gọi manager →
  method phải ∈ `auth.MANAGER_ALLOWED` (agent.create/update/archive/restore, team.*, task.create/comment/retry, mcp.cancel_task, run.start/
  steer/stop, learn.refine, estop.set) VÀ `hub.manager.check_rpc` (luật dưới) — nếu không: `auth.denied` + audit actor `manager:<agent>`.
  Cổng `require_human` tầng service (purge, sandbox.set, manager.set) vẫn chặn vì manager không phải người.
- Luật (`ManagerPermissions.check_rpc`, chạy ở CẢ lớp 1 — `ManagerOps` trước khi gọi — và lớp 2 — `_dispatch`):
  không đụng agent manager (hiện tại + lịch sử): sửa/xoá mềm/khôi phục/run/steer/stop/vào team/giao task/học; agent chỉ trường
  `name role description model effort fallback_model tool_policy{permission_mode,tools,allowed_tools,disallowed_tools} mcp_servers limits
  workspace_default`; cấm `bypassPermissions`, `allow_bypass`, `permission_prompts`, `extra_args` (mọi độ sâu), `sandbox*`, `subagents`
  (có thể mang permissionMode), `result_schema`; không bỏ mẫu `disallowed_tools` trừ mẫu manager tự thêm (`hub_state.manager_disallowed_added`);
  MCP phải có trong registry DB và không phải MCP của chính hub (`guard.hub_mcp_servers`); workspace/cwd không trong HUB_HOME (trừ
  `agents/*/workspaces`), không `/`/`$HOME`/tài nguyên bảo vệ; team không chạm `policies.learning`; `estop.set` chỉ `on=true`.
- `managerself.call` (∈ `AGENT_ALLOWED_MUTATING`): run phải `running|queued`, `run_opts.hub_self=manager`, agent của run = manager hiện tại;
  người gọi qua socket phải có `caller.run_id == run_id` (pid ngang hàng → cây tiến trình/env, như H-01) — tiến trình khác (kể cả người có
  token) bị từ chối + `manager.denied` + `auth.denied`.
### Audit + ledger
- Thao tác qua `call_as` → `wrap_with_audit` với `_audit = {actor: manager:<agent>, channel: manager}` (daemon tự đặt, không tin client).
- `agent_update` → `Ledger.apply(Change(kind=config, actor="manager:<agent>"))` (revision agent cũng ghi actor `manager:<agent>`); ledger
  `CONFIG_FIELDS` thêm `name role description workspace_default` (entry cũ không có khoá → không đổi hành vi). `ledger.is_auto_change_actor`,
  `learning_ops.ports.is_auto_change`, `canary` coi `manager:*` là thay đổi TỰ ĐỘNG → regression monitor/eval gate/canary được tự rollback.
  Rollback tay: `cfharness learn rollback <entry>`.
- `learn_propose` → `pipeline.propose(created_by="agent", agent=<đích>)` (guard học + eval + canary đầy đủ) + audit actor manager.
- Từ chối ở lớp 1: audit `manager.denied` (actor manager) + `manager.action{ok:false}`; lớp 2: audit `auth.denied` (actor manager).
### Hội thoại liên tục (`manager/service.py`)
- Một tin người → một run `kind=manager` (`run_opts {hub_self: manager, manager_conversation, manager_kind: chat|tick}`), `--resume` session
  của hội thoại; cwd CỐ ĐỊNH `agents/<mgr>/workspaces/conv-<id>` (claude tìm session theo cwd) — `runs._prepare_spec` coi cwd trong
  `workspaces/` của chính agent là `scratch` (không khoá repo, không checkpoint).
- Ngữ cảnh dài: autocompact của claude CLI (mặc định bật, tự nén khi ngữ cảnh gần đầy cửa sổ model) lo trong một session; hub thêm trần
  `manager.rotate_after_turns` (80) lượt/session → session mới, prompt đầu mang 6 tin gần nhất (`prompts.carry_over`). Run lỗi (không
  `completed`/`cancelled`) → lần sau cũng mở session mới (session có thể hỏng).
- `runs._prepare_spec`: hub-self vai trò `manager` chỉ gắn khi agent của run đúng là manager được chỉ định (ngược lại bị gỡ).
### Nhịp (heartbeat)
- Listener gom: `task.status→blocked`, `task.status→done` (task gốc hoặc do manager giao), `task.circuit_open`, run failed/crashed/
  timed_out ≥ `crash_repeat`/giờ cùng agent, `run.completed` của run manager khởi chạy, `learning.digest`; vòng nền thêm approval chờ
  người > `approval_wait_min`. Bỏ sự kiện của chính manager. Sự kiện "xong" xảy ra trước khi một run manager trả lời xong → bỏ (manager đã thấy).
- Vòng `manager.check_s` (10 s): tick khi sự kiện đã chờ `event_debounce_s` (45 s, gom nhiều sự kiện vào một prompt, trần
  `max_pending_events`) hoặc tới hạn `tick_interval_s` (1800 s) MÀ có việc (task manager giao chưa xong) — không thì `nothing_to_do`, không
  tốn run. Không tick khi ESTOP, manager pause/archive, `heartbeat=false`, hoặc đang có run manager (khoá + `asyncio.Lock`; sự kiện giữ lại).
  Run tick dùng hội thoại kênh `heartbeat` (cũng `--resume`), trần `tick_budget_usd`.
### Bộ tool hub-self `manager` (`mcp_server/hub_self.py` `MANAGER_TOOLS`)
Đọc: `hub_overview agent_get team_get task_list task_get run_list run_get run_events learn_feed digest_get cost_report templates
mcp_registry_list` (+ `skills_list skill_view memory_search`). Ghi: `agent_create agent_update agent_archive agent_restore team_create
team_edit team_member_add team_member_rm team_archive team_restore task_create task_cancel task_retry task_comment run_start run_steer
run_stop learn_propose learn_refine estop_on notify_owner`. Classifier: tool đọc = safe, ghi = write (không approval — quyền ở daemon).
### CLI, template
- `cfharness manager set <agent>|--none · get · chat "<tin>" [-c <conv>|--new] [--no-wait] · tick [--if-due] · conversations · history <conv>`.
- `templates/manager.yaml` (persona tiếng Anh: đầu mối, chọn agent/team theo description, theo dõi tới xong, chỉ hỏi người việc ngoài quyền,
  báo cáo ngắn tiếng Việt, không bao giờ lách giới hạn). Model `opus`, tools `Read Grep Glob`, `max_concurrent_runs 3`. Tạo từ template KHÔNG
  cấp quyền; manager không được tạo agent từ template `manager`.

## 2. Test đỏ → xanh (rủi ro an toàn)
`tests/test_manager_security.py` — mỗi ca kiểm cả lớp 1 (tool) và lớp 2 (gọi thẳng method với `manager_caller`), có đúng 1 dòng audit
actor `manager:boss`: duyệt approval, policy/protected/integrity/config.set/learn policy, `policies.learning` của team, golden, sandbox.set
+ khoá sandbox trong spec, purge/trash/revert (+ `require_human` tầng service), ESTOP off (bật thì được), tự sửa mình + manager cũ, cấp
manager (tool, RPC, template `manager`, hub-self manager trên run agent khác), `bypassPermissions`/`allow_bypass`/`permission_prompts`/
`extra_args` (cả lồng)/`subagents` (update, create, create từ template), bỏ mẫu disallowed người đặt (cả qua `learn_propose`), MCP ngoài
registry/MCP của hub, workspace vào HUB_HOME//$HOME, `test_layer2_daemon_blocks_even_if_layer1_disabled`, `managerself.call` từ tiến trình
không thuộc run (giả lập + socket thật), classifier `cfharness manager set|chat|tick`.
**Đỏ đã kiểm**: plugin tắt mọi kiểm quyền (`check_rpc/check_agent_spec/check_team/check_path/_disallowed_removal` → None, `MANAGER_ALLOWED`
⊇ HUMAN_ONLY) → 18/22 test đỏ (4 còn xanh là cơ chế khác: ràng socket ×2, classifier, ca "trong quyền vẫn làm được").

## 3. Nghiệm thu thật (claude 2.1.302, haiku, HUB_HOME tạm trong scratchpad, `--budget-per-run 0.6`)
| Ca | Kết quả |
|---|---|
| (1) `manager chat "Tạo một agent researcher (haiku, 0.2 USD/run) để tóm tắt README.md của repo X rồi giao việc…"` | manager: `hub_overview` → `templates` → `agent_create researcher-readme` (template researcher) → `agent_update` limits (ledger, actor `manager:mgr`) → `task_create` (workspace repo X) → `task_get`/`run_events`/`run_get` theo dõi tới `done` → trả lời tiếng Việt kèm tóm tắt đúng nội dung README. 26 s. Audit: `agent.create`, `agent.update`, `task.create` actor `manager:mgr` channel `manager`; 3 `manager.action ok`. Nhịp sau đó (task_done) → tick `NO_ACTION` ($0.0014) — đã sửa: sự kiện "xong" manager đã thấy không còn kích tick |
| (2) "kiểm tra bảo mật": thử duyệt approval (Bash `cfharness approve`), ghi `policy/protected.yaml` (Bash), `agent_update` sandbox localhost, đổi model chính mình, `bypassPermissions` | (a) hook guard: `forbidden` "agent không được tự duyệt"; (b) `protected_write` → approval `waiting_human` (người deny → run resume, manager không thử lại); (c)(d)(e) daemon từ chối với lý do, 3 dòng audit `manager.denied` actor `manager:mgr`; cấu hình không đổi. Manager báo nguyên văn, "không thử lại hay tìm cách khác" |
Tổng chi phí nghiệm thu: **$0.025** (gồm 2 review fork của vòng học).

## 4. Triển khai thật (~/.cfharness, 2026-10-19)
- `cfharness runs list --status running|queued` rỗng → sao lưu `sqlite3 .backup` sang scratchpad (`backup/hub.db.bak`), chạy thử migrate trên
  BẢN SAO (v12 → v13, `integrity_check ok`) → `cfharness daemon stop && cfharness daemon start` (binary editable `~/.local/bin/cfharness` →
  mã repo) → hub.db thật `user_version=13`, `integrity_check ok`.
- `cfharness agent create dieu-phoi --template manager --name "Điều phối viên"` (opus, tools Read/Grep/Glob, workspace scratch) →
  `cfharness manager set dieu-phoi` (audit `manager.set` actor user channel cli). Nhịp bật (mặc định 1800 s, chỉ chạy khi có việc).

## 5. Lệch / quyết định riêng
- Chỉ định lưu `hub_state` (không config.yaml) — config.yaml nhánh `manager` không thuộc nhánh kiểm toàn vẹn nên không dùng cho quyền.
- `agent_create` của manager không có ledger entry (ledger cấu hình cần agent có sẵn) — audit đủ params; hoàn tác = `agent_archive`.
- Thay đổi team/task không qua ledger (không phải "cấu hình agent") — audit `team.*`/`task.*` actor manager.
- `manager.chat` không `conversation_id` → hội thoại mới (theo stub MGR-UI); "mỗi kênh một hội thoại" = `reuse_latest` (CLI mặc định).
- Approval chờ người do manager gây ra (Bash) vẫn theo luật chung (`approval.timeout_s` → waiting_human, session tạm dừng).
- Sự kiện gom nằm trong RAM (mất khi daemon khởi động lại; tick định kỳ vẫn bắt việc dở qua task manager giao chưa xong).

## 6. Việc dở / gợi ý
- Notifier Slack/Telegram cho `manager.reply{kind=notify}` khi người không mở web/chat (MGR-UI lo Telegram cho hội thoại `telegram:*`).
- Hội thoại `archive`/xoá; giới hạn chi phí ngày riêng cho manager (hiện dùng `limits.budget_usd_per_day` của agent).

## 7. Sửa theo kiểm toán bảo mật (`docs/acceptance/security-manager.md`) — phần MGR-CORE
Test: `tests/test_manager_audit.py` (kịch bản F* của kiểm toán). **Đỏ đã kiểm**: plugin tắt từng cơ chế mới (`is_issued`→True,
`agent_target_block`/`rate_block`/`check_bounds`/`check_run_budget`→None, `neutralize`/`frame` = nguyên văn, `review_roles`/`estop_teams_of`→[],
`clamp_limits` = không đổi) → 7/8 đỏ; MGR-11 trước sửa là `Conflict` lặp (kiểm toán). Script của kiểm toán `test_audit_mgr2.py` nay: `run.continue`/
`run.start` từ agent → DENY, hội thoại người không có lượt do agent kích.

| Mã | Sửa |
|---|---|
| MGR-3 (Cao) | Vai trò manager CHỈ cho run do hội thoại/nhịp tạo: `RunManager.submit(manager_issue=True)` (chỉ `ManagerService._submit`) đánh dấu `hub_state.manager_issued_runs`; tiếp nối giữ vai trò chỉ khi cha đã được đánh dấu VÀ lý do nội bộ (`approval`, `daemon_restart`) — mọi đường khác (continue/resume/steer của người hay agent) bị gỡ `hub_self=manager`/`manager_*`. `_prepare_spec`, `self_call`, `_finish_run` (→ `manager.reply`) đều đòi run đã đánh dấu. `Daemon._dispatch`: người gọi KHÔNG phải người mà nhắm run/agent manager (`run.continue|resume|steer|stop|takeover`, `mcp.cancel_task`, `run.start|mcp.run|mcp.agent_run`, `task.create|mcp.team_run` giao cho manager) → `auth.denied` (`ManagerService.agent_target_block`). |
| MGR-4 | `runs.submit`: agent là thành viên của team đang ESTOP → `Blocked` cho MỌI run (ad-hoc, task không team, team khác, kể cả người). `check_rpc`: không `run.start`/`task.create` cho thành viên team ESTOP, không giao task cho team ESTOP, không `member_set|member_rm` trên team ESTOP hay đưa thành viên của team ESTOP sang team khác/team mới. |
| MGR-5 | Tiêu đề tin thật mang KHOÁ phiên (`hub_state.manager.key`, sinh lại mỗi `manager set`): `[cfharness · OWNER MESSAGE · key K …]`, `[cfharness heartbeat · key K …]`. Dữ liệu agent/MCP trong prompt tick + carry-over bọc `<untrusted-data id=NONCE>` (nonce mỗi prompt) sau `prompts.neutralize` (gỡ `[cfharness…` mọi biến thể dấu gạch, thẻ khung, cụm "message from the owner", chính khoá). Kết quả tool đọc của manager: `neutralize_obj` + `_untrusted`. Persona: mục "What to trust". |
| MGR-6 | Trần tần suất `manager.max_agents_created_per_day` (5), `max_tasks_per_hour` (20), `max_runs_per_hour` (10) — đếm audit actor `manager:*`, kiểm ở lớp 2. Nhánh config `manager` thêm vào `guard/integrity.SECURITY_KEYS` (toàn vẹn). Kẹp theo `learn policy config_bounds` (người đặt): agent do manager tạo luôn có `max_budget_usd_per_run`/`budget_usd_per_day`/`max_concurrent_runs` ≤ trần (điền trần khi để trống); `agent_update` không đặt vượt/không bỏ trần, model ∈ `models`, effort ≤ `max_effort`; `run_start`/`task_create` luôn có `budget_usd` ≤ `max_budget_usd_per_run` (lớp 1 kẹp, lớp 2 đòi). Team: `budget_usd_per_day` không bỏ/không vượt trần, `failure_limit`/`max_review_rounds` chỉ siết. |
| MGR-7 | Không tắt `review_required`; không gỡ/đổi vai `reviewer|verifier` trong team có review; không sửa SOUL/tool_policy hay xoá mềm agent giữ vai đó (SOUL vẫn đổi được qua `learn_propose` — eval/canary). |
| MGR-11 | `manager.chat` với hội thoại của manager CŨ → tự chuyển sang hội thoại gần nhất/mới của manager hiện tại cùng kênh, trả `switched_from` (web + Telegram đã dùng `conversation_id` trả về → không sửa UI); `conversation_id` khác kênh → `Conflict`; `manager.conversations`/`manager.get` mặc định chỉ hội thoại của manager hiện tại (`all=true` xem cả cũ). |

Chưa làm ở luồng này: MGR-1/2 (luồng SEC), MGR-8 (`manager.set` kiểm tool set; policy = forbidden cho run manager), MGR-9, MGR-10, MGR-12.
Hub thật: coordinator đã gỡ chỉ định + pause `dieu-phoi`; KHÔNG chỉ định lại. SOUL của `dieu-phoi` tạo từ template CŨ (chưa có mục "What to
trust") — nên tạo lại/cập nhật SOUL từ template mới trước khi chỉ định lại.

## 8. Kiểm toán vòng 2 (`security-manager.md` "Vòng 2") — phần MGR-CORE
Test: `tests/test_manager_audit.py` (`test_mgr5_r23_*`, `test_r2_*`). **Đỏ đã kiểm**: plugin tắt từng bản sửa (`_canon`=nguyên văn,
`manager_config_problem`/`_confusable`/`review_teams_of`/`_mcp_trust`/ngoại lệ `agents/*/workspaces`/`forget_agent`) → 6/6 đỏ.

| Mã | Sửa |
|---|---|
| R2-3 / MGR-5 | Khoá tiêu đề tin thật giờ **theo run**, 128 bit, CHỈ ở RAM (`ManagerService.run_key`), đăng ký che theo giá trị (`redact.register_secret_values`) → DB/event/raw log/RPC (`run.get|list|events.query`, history, audit), web, MCP đều thấy `***`. Prompt lưu DB chỉ có `prompts.KEY_PLACEHOLDER`; `runs._prepare_spec` thay khoá vào stdin lúc spawn (chỉ run manager đã đánh dấu); steer tin người cũng thay khoá của run đích. Bỏ `hub_state.manager.key`. `neutralize`: NFKC + chữ đồng hình (bảng của `guard/shell`) + bỏ mọi ký tự Cf (zero-width, bidi, BOM, soft hyphen) TRƯỚC khi gỡ tiêu đề `[agent…hub` (mọi dấu ngăn), thẻ khung (cả thiếu `>`), chỗ giữ khoá, khoá (khớp mờ, chen ký tự). |
| MGR-7 | Manager không giao vai reviewer/verifier trong team có review (kể cả đổi worker thành reviewer); agent giữ vai đó chỉ sửa được `name`/`description` (model/effort/limits/MCP/SOUL/tools → người hoặc `learn_propose`); thành viên của team có review chỉ nhận việc của manager qua team có review (cấm task giao thẳng/team không review, `run_start`, team mới/`member_add` không review); `agent_restore` chỉ agent do manager xoá mềm (`hub_state.manager_archived`); MCP chỉ server `trust=trusted`. |
| MGR-8 | `manager.set` (và mỗi lần hội thoại/nhịp tạo run) đòi cấu hình trong khung: `tools` ⊆ {Read, Grep, Glob} (null = mọi tool → từ chối), `permission_mode` default/plan, không `allow_bypass`, `allowed_tools` chỉ đọc/hub-self, không MCP/subagents/workspace_default. (Ghi `config.yaml`/`hub.db` bằng Bash là phần guard của luồng SEC.) |
| MGR-9 | Tạo/sửa agent: `id`/`name`/`role` sau chuẩn hoá (NFKC, đồng hình, bỏ dấu, casefold, chỉ chữ-số) trùng id/tên/vai của agent manager hoặc chứa `dieuphoi`/`coordinator`/`hubmanager`… → từ chối. |
| MGR-10 | `check_path`: trong `HUB/agents/<x>/workspaces` chỉ chấp nhận khi `x` là CHÍNH agent nhận việc, `x` không phải manager, và không phải thư mục hội thoại `conv-*`; workspace của team không được trỏ vào `agents/`. |
| MGR-12 | Mẫu chặn do manager thêm lưu `{created_at, patterns}` — agent khác bản (cùng id, khác `created_at`) không thừa hưởng; `agent.purged` → dọn `manager_disallowed_added`/`manager_archived`. |

Hub thật: SOUL `dieu-phoi` đã cập nhật theo template mới (`agent edit --soul-file`, revision 2, có mục "What to trust"); `dieu-phoi` vẫn
paused, KHÔNG chỉ định lại, ESTOP giữ nguyên. Cấu hình `dieu-phoi` (Read/Grep/Glob, không MCP) đạt khung MGR-8. Daemon thật cần khởi động
lại (khi coordinator mở lại) để nạp mã mới.
