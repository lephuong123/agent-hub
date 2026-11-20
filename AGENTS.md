# AGENTS.md — hướng dẫn cho agent code tiếp trên repo này

## Bối cảnh
- Sản phẩm: hub điều phối đội agent chạy trên `claude` CLI. KHÔNG tự viết agent loop, KHÔNG gọi Anthropic API trực tiếp.
  Mỗi run = một tiến trình `claude -p … --output-format stream-json` do daemon spawn.
- Đọc trước: `research/BRIEF.md` (quyết định đã chốt), `docs/BRD-chi-tiet.md` (FR/NFR, data model §8, event §9, lộ trình §13),
  `docs/spikes/M0-spike-report.md` (cờ CLI đã kiểm chứng).
- Mốc hiện tại: M0 xong phần lõi. Tiếp theo là M1 (MCP server hub-self, phê duyệt thao tác phá huỷ, resume, khôi phục restart).
- Không sửa `research/`, `docs/` (trừ ghi chú nhỏ trong `docs/dev-notes/`), `demo/`.

## Kiến trúc (`src/cfharness/`)
| Module | Vai trò |
|---|---|
| `paths.py` | Bố cục `HUB_HOME` (env `CFHARNESS_HOME`), đường dẫn socket (rút về `/tmp/cfharness-<uid>-<hash>.sock` nếu quá dài cho AF_UNIX) |
| `config.py` | `HUB_HOME/config.yaml` + `DEFAULTS`. Mọi giới hạn là setting; đọc lại theo mtime nên `config set` có hiệu lực ngay. Cờ cô lập `claude` ở `runtime.claude.isolation.*` |
| `events.py` | Envelope `HubEvent` (v, epoch, seq, ts, type, level, source, agent/run/session/subagent id, payload), bảng level, loại không ghi DB |
| `claude_adapter.py` | stream-json → event hub (FR-RT-04). Có trạng thái theo run. Kiểm cô lập hậu nghiệm (hook/MCP/plugin/auto-memory lạ → `isolation.violation`) |
| `bus.py` | `EventBus`: seq toàn hub theo epoch, ring buffer theo agent (biết `truncated`), subscriber hàng đợi giới hạn (`slow_consumer`). `Aggregator` usage/status |
| `store.py` | SQLite WAL, migration theo `PRAGMA user_version` (danh sách `MIGRATIONS` — chỉ THÊM phần tử mới, không sửa cái cũ). `events` append-only bằng trigger |
| `agents.py` | CRUD agent, revision (`agent_revisions`), xoá mềm vào `trash/`, pause |
| `runs.py` | `RunManager`: submit (ESTOP/auth/budget), dispatcher FIFO (giới hạn toàn hub + theo agent), spawn, ghi kết quả, stop, ESTOP |
| `runtime/` | `AgentRuntime` (interface), `ClaudeCliRuntime` + `build_argv` (hàm thuần), `FakeRuntime` (phát lại JSONL) |
| `prompt.py` | Khối system prompt: quy ước hub + SOUL (tiếng Anh), hash lưu vào run |
| `hub.py` | Nối Store + Bus + services; dùng chung cho daemon và test in-process |
| `daemon.py` / `rpc.py` | JSON-RPC 2.0 newline-delimited qua Unix socket (0600). `events.subscribe` biến kết nối thành luồng notification |
| `cli.py` | `cfharness` (argparse). Client thuần của daemon; tự khởi động daemon nếu chưa chạy |

Nguyên tắc: daemon là writer DUY NHẤT của `hub.db`; CLI/TUI/MCP chỉ gọi RPC. Mọi tương tác runtime qua `AgentRuntime`.
Trạng thái agent do `RunManager` tính (bỏ `agent.status` từ adapter). Event kết thúc run chính thức là event `source="hub"`
phát trong `RunManager._finalize` sau khi ghi DB (event `run.completed` của adapter bị nuốt).

## Sự thật về `claude` CLI cần nhớ (spike M0)
- Cô lập: `--setting-sources ""` + `--settings <runtime/settings.json>` (có `autoMemoryEnabled:false`) + `--strict-mcp-config --mcp-config <runtime/mcp.json>`. Không `--bare`, không đặt `CLAUDE_CONFIG_DIR` (mất OAuth).
- Input `--input-format stream-json --replay-user-messages`: prompt gửi qua stdin; daemon đóng stdin sau `result` để tiến trình thoát (M0 = 1 lượt/tiến trình). Interrupt mềm = `control_request interrupt` qua stdin.
- SIGINT → `result` `error_during_execution`/`aborted_tools`; SIGTERM → không có `result`. Run có `stop_requested` → `cancelled`.
- `total_cost_usd` sau `--resume` là LUỸ KẾ theo session → `runs.cost_usd` = chênh lệch so với `sessions.cumulative_cost_usd`.
- Hook lỗi/timeout là fail-OPEN — hook guard của M1 phải tự fail-closed.
- Biến môi trường phiên Claude Code bao ngoài (`CLAUDECODE`, `CLAUDE_CODE_*`…) bị loại khỏi tiến trình con (`runtime.claude.env_strip`).

## Phát hành — viết release note TRƯỚC
Release note theo từng phiên bản ở `CHANGELOG.md` (gốc repo, tiếng Việt, mục mới nhất trên cùng). Quy trình bắt buộc khi phát hành `vX.Y.Z`:
1. **Trước tiên** viết release note: đổi mục "Chưa phát hành" thành `## vX.Y.Z — <YYYY-MM-DD>`, bổ sung đủ mọi PR/thay đổi kể từ tag trước
   (`git log --first-parent <tag trước>..HEAD`), nhóm theo chức năng, viết theo góc nhìn người dùng, ghi số PR, có mục "Lưu ý khi cập nhật"
   nếu có migration / cấu hình phải sửa tay. Chưa có release note thì KHÔNG nâng phiên bản, KHÔNG tạo tag/GitHub Release.
2. Sau đó mới nâng `version` (`pyproject.toml`, `src/cfharness/__init__.py`, `uv.lock`), tag ở README và `scripts/setup-hub-user.sh`.
3. Thân GitHub Release = chép nguyên mục của phiên bản đó trong `CHANGELOG.md` (không dùng "Generate release notes" thay thế).
Trong lúc làm việc: PR có thay đổi người dùng thấy được thì thêm một dòng vào mục "Chưa phát hành".

## Lệnh
```bash
uv sync
uv run pytest -q                      # toàn bộ test, ~4 s, KHÔNG gọi claude thật
uv run pytest tests/test_argv.py -q   # bộ dựng argv
uv run cfharness --help
```
Test KHÔNG được gọi `claude` thật hay tốn tiền: dùng `FakeRuntime` (fixture JSONL) hoặc `tests/fake_claude.py` (binary giả cho `ClaudeCliRuntime`).
Fixture stream-json thật ở `src/cfharness/fixtures/` (mặc định của FakeRuntime) và `tests/fixtures/` (chép từ `docs/spikes/m0/fixtures/` + lần chạy thật M0).
Khi nâng phiên bản CLI: ghi fixture mới, thêm contract test, rồi thêm phiên bản vào `runtime.claude.tested_versions`.

## Quy ước
- Thông báo người dùng, docstring, README, BRD: **tiếng Việt**. Định danh (tên hàm, field, cờ, loại event): tiếng Anh.
- Persona/SOUL/skill/memory/prompt gửi cho agent: **tiếng Anh**.
- Lỗi nghiệp vụ ném `HubError`/`NotFound`/`Conflict`/`Blocked` (`errors.py`) → mã JSON-RPC → mã thoát CLI (0/1/2/3).
- Loại event mới: thêm vào `EVENT_LEVELS` trong `events.py`, cập nhật ánh xạ trong docstring `claude_adapter.py`, và test.
- Không thêm `--dangerously-skip-permissions`; `bypassPermissions` chỉ khi `tool_policy.allow_bypass` + workspace scratch/worktree (NFR-15, có test),
  hoặc agent được NGƯỜI đặt trust full (`guard.trust.set`, `docs/dev-notes/trust-and-chat-settings.md`).
- Thay đổi schema DB: thêm migration mới vào cuối `MIGRATIONS`.
- Không commit secret; argv lưu DB đã che (`redact.py`), file runtime 0600 và bị xoá sau run.

## M1 luồng A — Tích hợp (MCP server, MCP client registry, secret, audit)
Chi tiết, lệch BRD, việc dở: `docs/dev-notes/M1-A.md`.

| Module | Vai trò |
|---|---|
| `mcp_server/server.py` | MCP server `hub_*` (SDK `mcp` 2.x, `MCPServer`). Client MỎNG của daemon (RPC qua `DaemonCaller`, tự khởi động daemon). Output = model Pydantic; annotations readOnly/destructive. Quyền: stdio=`control`, HTTP theo token. `hub_approve(always=true, confirm_rule=<always_offer.fingerprint|domain>)` = cùng `approval.approve{always, always_confirm}` (bề mặt người; lý do + giới hạn: LLM-2 trong `docs/dev-notes/hardening-SEC.md`); `hub_approve(grant_rule=true, confirm_rule=<matched_rule>, grant_for)` = `approval.approve{grant_rule, grant_for_s, grant_confirm}`; `hub_open_requests` có `always_offer` |
| `mcp_server/http.py` | Streamable HTTP CHỈ loopback + `BearerAuth` (401). Token `read`/`control` ở `HUB_HOME/mcp_server/tokens.json` (0600) |
| `mcp_server/rpc.py` | RPC `mcp.*`/`secret.*`/`audit.*`/`feedback.*`; `register_rpc(daemon)` ở CUỐI `Daemon.__init__` còn **bọc audit mọi method thay đổi trạng thái** (`MUTATING`; `channel` lấy từ `params._audit`, mặc định `cli`). `mcp.events` = long-poll ≤ 25 s theo run |
| `mcp_server/cli.py` | `cfharness mcp serve|snippet|token|add|edit|list|show|rm|restore|probe|import|assign|unassign|agent-config`, `secret`, `audit list`, `feedback` |
| `mcp_client/` | Registry MCP (DB `mcp_servers` là nguồn sự thật; `mcp/registry.yaml` là BẢN SAO chỉ đọc). Hợp đồng cho B: `build_agent_mcp_servers(agent)`, `tool_annotations(server, tool)`, thêm `agent_mcp_tool_rules(agent)` |
| `mcp_client/wiring.py` | `attach(hub)` (gọi trong `Hub.__init__`): `hub.secrets`, `hub.audit`, `hub.mcp`, listener `ToolWriteWatcher` → audit + event `tool.write` |
| `secrets.py` | Secret store file 0600 `HUB_HOME/secrets/<NAME>`; `${NAME}` trong cấu hình, `resolve()` fail-closed |
| `mcp_client/oauth.py` | OAuth 2.1 cho MCP http/sse (`auth: {type: oauth}`): discovery PRM/AS, DCR, PKCE, state RAM ≤10 phút, token ở secret store; RPC `mcp.oauth_start|callback|status|disconnect|refresh`; chèn Bearer vào mcp.json runtime. Chi tiết: `docs/dev-notes/mcp-oauth.md` |
| `mcp_client/oauth_proxy.py` | `hub.mcp_proxy`: proxy loopback (CHỈ 127.0.0.1, lười, `mcp.oauth_proxy.enabled`, BẬT mặc định từ v0.2.17) — mcp.json của run trỏ `/r/<id>…` + header `X-Cfharness-Proxy-Key` theo run, KHÔNG token; proxy làm mới + chèn Bearer, stream SSE, thu hồi trong `finally` của `_run`. Không mở ngoại lệ sandbox (client MCP chạy trong tiến trình claude). Test `tests/test_mcp_oauth_proxy.py`; `docs/dev-notes/mcp-oauth.md` §8.1 |
| `audit.py` | `audit_log` append-only (trigger). `audit.record(kind, agent_id, run_id, data)` cho luồng B; tiến trình ngoài daemon dùng RPC `audit.record` |

Quy tắc thêm: method RPC mới thay đổi trạng thái → thêm tên vào `MUTATING` (rpc.py) và khai báo TRƯỚC dòng `register_rpc(self)`.
Tool `hub_*` mới: kiểm `hub.require_control(ctx, …)` nếu không readOnly; mô tả tiếng Việt; output là model Pydantic.
Test MCP: `tests/test_mcp_server.py` (in-process `Client(srv)`, stdio thật, HTTP uvicorn cục bộ), không gọi `claude`.
Kết nối Claude Code: `cfharness mcp snippet` (CHỈ in lệnh/JSON, không sửa `~/.claude.json`).

## M1 luồng B — An toàn & vòng đời (guard, approval, Slack, resume, khôi phục)
Chi tiết, lệch BRD, việc dở, kết quả kiểm chứng `claude` thật: `docs/dev-notes/M1-B.md`.

| Module | Vai trò |
|---|---|
| `guard/shell.py` | Lexer shell: NFKC + chữ đồng hình, `${IFS}`, bỏ quote, `$'\x..'`, biến đã gán, `~`, heredoc/here-string, tách `;&&||\|&`, thu `$(..)`/`` `..` ``/`<(..)` |
| `guard/classifier.py` | `classify(tool, input, GuardContext) -> Verdict` (safe/write/destructive/uncertain + `forbidden`), THUẦN. "File run tự tạo" = birthtime ≥ lúc run đầu của session vào hàng đợi. `action_fingerprint` cho vé một lần |
| `guard/policy.py` | `HUB_HOME/policy/{protected,catalog}.yaml` + `history/`; tài nguyên bảo vệ mặc định (BRD FR-SG-02 + mã nguồn `cfharness`) |
| `guard/service.py` | `GuardService.check` (RPC `guard.check`): phân loại → allow / mở approval + GIỮ ≤ `approval.timeout_s` → allow/deny/waiting_human |
| `guard/hook.py`, `guard/mcp_guard.py` | Hook PreToolUse + MCP `hub-guard` (permission-prompt-tool). Chỉ stdlib, FAIL-CLOSED (`|| exit 2`, alarm, mọi lỗi → deny) |
| `guard/runtime.py` | Sinh phần guard của settings.json/mcp.json; mẫu `--disallowedTools` tĩnh và bỏ đúng mẫu cho run resume có vé |
| `guard/locks.py` | Khoá thư mục `dir:` theo SESSION (bản tối thiểu; M3 thay bằng khoá repo + checkpoint) |
| `guard/rpc.py`, `guard/cli.py` | RPC `approval.*`/`guard.*`/`notify.test`/`run.continue|resume`/`agent.purge`; CLI `approvals`, `approve`, `deny`, `guard test|policy|protect|decisions`, `notify test`, `run continue`, `agent purge` |
| `approvals.py` | `ApprovalService`: open → waiting_human → allowed/denied/cancelled (không `expired`), vé một lần, nhắc lại giãn dần, Signal `reject` |
| `notify/slack.py` | Listener bus → Slack webhook/bot (secret), tin đã che, backoff, `notify.failed`; tắt êm khi chưa cấu hình |
| `recovery.py` | Đối soát `(pid, start_time)` khi daemon khởi động, dừng mồ côi, `interrupted` → tự resume (trừ waiting_human); purge retention |
| `auto_resume.py` | Tự resume CÓ TRẦN run ad-hoc dừng vì lỗi tạm thời (crashed/timed_out/rate_limited/mạng; trần chung với restart), backoff, event `run.auto_resume` + audit; `hang_stats` cho thẻ Sức khoẻ (`report.hangs`). `docs/dev-notes/run-auto-resume.md` |
| `accounts/` | Danh sách tài khoản Claude, NGƯỜI bấm đổi (không tự đổi). `service.py` `AccountService` (`hub.accounts`; token ở secret `CLAUDE_ACCT_<ID>`, đổi = chép vào `CLAUDE_CODE_OAUTH_TOKEN`), `rpc.py` `account.*` (ghi ∈ HUMAN_ONLY, `MUTATING_ACCOUNTS`, gỡ `token`/`code` trước audit), `login.py` `claude setup-token` qua PTY (pywinpty trên Windows), `cli.py` `cfharness account`. Cổng rate limit theo tài khoản ở `dispatch/dispatcher.py`; web: `web/account_pages.py`. `docs/dev-notes/plan-claude-accounts.md`. `claude_config.py`: mỗi tài khoản token một phiên claude.ai (connector) riêng ở `HUB_HOME/claude-config/<id>/` (CLAUDE_CONFIG_DIR, symlink `projects`/`settings.json`/`CLAUDE.md` sang claude home mặc định; agent không đọc/ghi, không sao lưu/xuất) — run connector theo tài khoản ĐANG DÙNG, không mượn phiên tài khoản khác (`docs/dev-notes/manager-connectors.md`) |

Bất biến: thao tác phá huỷ KHÔNG BAO GIỜ tự từ chối do hết giờ; hook/hub-guard lỗi = deny; agent không ghi được `policy/`
(settings `permissions.deny` + classifier) và không tự duyệt (`cfharness approve`, socket hub, `mcp__*__hub_approve` = forbidden).
Thêm luật phân loại: thêm ca vào `tests/test_guard_classifier.py` (recall 100% trên `DESTRUCTIVE_*`, báo nhầm ≤ 5% trên `SAFE_*`).
Test e2e guard dùng `tests/fake_claude_guard.py` (chạy hook thật từ `--settings`, tôn trọng `--disallowedTools`, cost luỹ kế).

## M3 — Team & điều phối (team, task/DAG, dispatcher, workspace repo thật, hub-self MCP)
Chi tiết, lệch BRD, nghiệm thu thật, việc dở: `docs/dev-notes/M3.md` (đầu file: chữ ký RPC `task.list|get` cho tab Tasks).

| Module | Vai trò |
|---|---|
| `teams.py` | `TeamService`: CRUD, thành viên `lead|worker|reviewer|verifier` (≤1 lead), `TeamPolicies`, ESTOP team, archive/restore (xoá mềm), tổng quan + chi phí ngày/tuần |
| `tasks.py` | `TaskService`: task (idempotency_key), DAG `task_links` (chống chu trình), comment, chuyển trạng thái + event `task.status`, prompt worker/lead/review (tiếng Anh) |
| `dispatch/dispatcher.py` | `TaskDispatcher` (`hub.dispatcher`): tick + `kick()`; claim nguyên tử; lease/heartbeat; retry `--resume`; circuit breaker; rate limit; ngân sách ngày; lead decompose; review; tổng hợp task gốc; `self_call` cho hub-self |
| `dispatch/routing.py`, `dispatch/plan.py` | Chọn worker theo role/description + tải; `PLAN_SCHEMA`/`REVIEW_SCHEMA` + kiểm hợp lệ |
| `dispatch/rpc.py`, `dispatch/cli.py` | RPC `team.*`, `task.*`, `taskself.call`, `run.revert|diff`, `report.*`, `agent.clone|export|import`, `mcp.team_run|task_events`, `estop.set{team}`; CLI `team`, `task`, `run revert|diff`, `report`, `cost`, `agent clone|export|import` |
| `workspace.py` | `WorkspaceLocks` (khoá theo gốc repo, thay `guard/locks.RepoLocks`), `take_checkpoint`/`compute_diff`/`revert`/`resume_conflicts` |
| `reports.py` | `report(since)` (FR-OB-06), `cost_breakdown(by)` |
| `mcp_server/hub_self.py` | MCP stdio `hub-self` (stdlib) cho agent trong run có task: `task_show|comment|complete|request_review|block` (+ lead `task_create|link|team_roster`) |

Nguyên tắc: trạng thái task CHỈ đổi qua `TaskService`/`TaskDispatcher` (daemon). Kết quả run → dispatcher qua listener bus
(`call_soon`), chỉ xử lý khi `task.current_run_id == run.id` (idempotent). Run của task mang `task_id`/`team_id`/`run_opts`
(`json_schema`, `read_only`, `hub_self`, `disallowed_tools`); chuỗi resume/continue kế thừa từ `parent_run_id`.
Outcome run thêm `crashed|rate_limited|blocked|protocol_violation` (thêm vào mọi nơi liệt kê trạng thái kết thúc).
Workspace `dir:` = repo thật: KHÔNG commit/stash; checkpoint + diff ở `HUB_HOME/checkpoints/<run>/`; revert chỉ khi file
không bị sửa tiếp. Test M3: `tests/test_team_task.py`, `test_workspace.py` (repo git tạm), `test_dispatch_crash.py`
(`tests/fake_claude_team.py`, giết tiến trình thật), `test_team_mcp.py`, `test_team_cli.py`; helper `tests/m3_fake.py`
(`ScriptedRuntime`: kịch bản theo run — structured_output, crash, hold, ghi file, rate limit).

## M2 — htop (TUI `top`), steer, template, takeover
Chi tiết, số đo NFR/KPI, lệch BRD, việc dở: `docs/dev-notes/M2.md`. Ảnh màn: `docs/screenshots/m2/`.

| Module | Vai trò |
|---|---|
| `tui/app.py` | Textual `TopApp`: `HtopScreen` (bảng agent + firehose; `A` = luôn cho phép khi approval có `always_offer`, có hộp xác nhận), `AttachScreen` (transcript replay DB + ring → live), `TasksScreen` (RPC `task.*` của M3), `LearningsScreen` + `GoldenScreen` (M4-B, `tui/learnings.py`), form agent, hộp xác nhận/nhập. CLIENT THUẦN của daemon |
| `tui/model.py` | Trạng thái thuần (không Textual): `TopModel` nạp `tui.snapshot` + áp event firehose; đo độ trễ event→TUI; định dạng ô/firehose |
| `tui/client.py` | `AsyncHubClient` (asyncio, cùng loop Textual) + `subscribe()`; KHÔNG dùng `RpcClient` đồng bộ trong TUI/test in-process (chặn loop) |
| `tui/rpc.py` | RPC daemon: `tui.snapshot`, `run.steer`, `template.list|get`, `agent.create_from_template`, `run.takeover|takeover_record` |
| `tui/takeover.py` | `claude --resume <session>` tương tác cùng cờ cô lập/mcp/SOUL của agent (không hook guard); ghi Run `kind=human_takeover` |
| `tui/cli.py` | `top`, `attach`, `steer` / `run steer` (viết lại `run-steer` qua `guard.cli.HIDDEN`), `takeover [--print|--record]`, `template list|show` |
| `tui/bench.py` | `python -m cfharness.tui.bench --home <tmp>`: đo NFR-01..05/08 bằng daemon thật + FakeRuntime + TUI headless |
| `steer.py` | `steer(hub, ref, text)`: ghi stdin stream-json của tiến trình đang chạy, phát `user.message`; `Conflict` khi run đã qua `result` |
| `templates/` | `researcher|coder|reviewer|ops.yaml` (config + SOUL tiếng Anh); `agent create <id> --template <tên>` |

Steer: adapter phát `steer.ack` cho tin user `isReplay` thứ 2 trở đi (tin #1 = prompt). `ClaudeCliRuntime` vẫn đóng stdin
sau `result` như M0, TRỪ khi còn tin steer chưa được phát lại (`handle.internal["steer_pending"]`; phòng hờ
`runtime.claude.steer_ack_grace_s`, mặc định 30 s). `FakeRuntime` hỗ trợ steer (chèn dòng replay; sau `result` từ chối).
Migration v5 (M2): `runs.steer_count`, `runs.takeover_of_run_id`, index `events(type, ts)`.
Test M2: `tests/test_tui.py` (Pilot + daemon IN-PROCESS trên socket thật; `CFHARNESS_SCREENSHOTS=docs/screenshots/m2`
để ghi lại SVG), `tests/test_steer.py` (+ `tests/fake_claude_steer.py`: claude giả mô phỏng spike A-02), `tests/test_templates.py`.
Event mới: `user.message`, `steer.ack`, `run.takeover`. Mọi thao tác thay đổi từ TUI gửi `_audit.channel=tui`.

## M4 luồng B — Đánh giá & vận hành học (golden task, eval gate, hồi quy, curator, digest)
Chi tiết, hợp đồng với A, lệch BRD, nghiệm thu thật: `docs/dev-notes/M4-B.md`.

| Module | Vai trò |
|---|---|
| `evals/golden.py` | Golden task do NGƯỜI quản lý (Q-07): DB + bản sao `policy/golden/`; agent/vòng học chỉ gợi ý (`golden.suggested`) |
| `evals/style.py`, `evals/style_report.py` | Chấm phong cách trả lời theo `prompt.RESPONSE_STYLE` (hàm thuần; checker `response_style`) + chấm offline tin điều phối viên thật: RPC chỉ đọc `eval.style`, CLI `cfharness eval style` |
| `evals/checkers.py`, `evals/judge.py` | Chấm tất định (regex/contains/json_schema/command/response_style/all) + LLM judge `claude -p --json-schema --no-session-persistence` (run `eval_judge`) |
| `evals/gate.py`, `evals/overlay.py` | `hub.evals.gate.check(proposal)` / `post_apply(entry)` (async) — chạy golden task trên BẢN SAO home (`run_opts.eval_overlay`, run kind=`eval`), N lần, ngưỡng tụt, ngân sách; tụt sau áp dụng → B tự rollback (`eval_gate`) |
| `learning_ops/regression.py` | FR-LN-17: so correction/lỗi/cost trước–sau → `ledger.rollback(actor="regression_monitor")` |
| `learning_ops/curator.py` | FR-LN-08 pha 1 tất định (14/30 ngày, pin), archive qua `Ledger.apply` của A (`actor=curator`) |
| `learning_ops/digest.py` | FR-LN-13: digest 08:00 giờ máy → Slack (event `learning.digest`) + `digest.tui_open` |
| `learning_ops/wiring.py`, `rpc.py`, `cli.py`, `ports.py` | `attach(hub)`, vòng nền (start ở `hub.started`), RPC `golden.* eval.* digest.* curator.* regression.*` (tự bọc audit), CLI cùng tên |
| `tui/learnings.py` | Tab Learnings (FR-UI-06: feed, eval trước/sau, rollback `r`, pin `p`, digest `D`, journey `j`, canary placeholder M5) + `GoldenScreen` (`G`); digest khi mở TUI lần đầu trong ngày |

Bất biến: regression monitor/eval gate CHỈ tự rollback entry actor ∈ {learning, review, agent, curator}; thay đổi của NGƯỜI chỉ
cảnh báo (`regression.detected{human_change}`). Golden task/rubric thuộc chính sách an toàn — guard coi `cfharness golden <ghi>` và `mcp__*__hub_golden_(add|rm|…)` từ agent
là `policy_change` (forbidden). Eval KHÔNG đụng home thật; run eval không resume sau restart. Test: `tests/test_eval*.py`,
`test_regress*.py`, `test_curator*.py`, `test_digest*.py`, `test_tui_learnings.py`, helper `tests/m4b_fake.py`. Migration v8, v9.
Ảnh: `docs/screenshots/m4/`. Tool MCP `hub_steer` (→ `run.steer`) ở `mcp_server/server.py`.

## M4 luồng A — Lõi học (Signal, review fork, proposal tự áp dụng, ledger, guard học, tri thức tầng agent)
Chi tiết, lệch BRD, nghiệm thu thật: `docs/dev-notes/M4-A.md`.

| Module | Vai trò |
|---|---|
| `knowledge/` | `hub.knowledge`: MEMORY.md/USER.md (mục `§`, hạn mức), skills/<name>/SKILL.md, chỉ mục DB `skills` (created_by, state, use_count), pin, xoá mềm `.archive/`; `prompt_blocks.py` dựng memory snapshot + skill index (≤60 ký tự) |
| `learning/service.py` | `hub.learning`: listener bus, vòng nền (`learning.tick_s`), `learnself_call` (MCP hub-self vai trò `knowledge`/`learning`) |
| `learning/signals.py` | Signal FR-LN-01 + bộ phát hiện (steer phủ định, stop, revert, edit_output, failure, repeat_pattern); `signals.record(...)` cho B |
| `learning/review.py` | Trigger + hàng đợi gộp newest-wins + review fork `--resume --fork-session --session-id`, chỉ Read/Grep/Glob + hub-self |
| `learning/pipeline.py` | Proposal → guard → L1/L2/L3 → rate → eval gate (`hub.evals.gate`, B) → `Ledger.apply`. KHÔNG có hàng chờ duyệt |
| `learning/ledger.py` | HÀM ÁP DỤNG DUY NHẤT (snapshot blob sha256 → ghi → `ledger_entries` append-only), rollback entry/chuỗi |
| `learning/guard.py`, `threats.py` | Guard học: policy_touch / self_escalation / bounds / guard_rule → `guard.blocked` |
| `learning/policy.py` | `policy/learning.yaml` (công tắc, config_bounds, rate) — chỉ người (`learn policy set`) |
| `learning/runtime.py` | Lắp system prompt từ `agent["home_path"]` (frozen theo session: resume/fork dùng lại `HUB_HOME/prompts/<hash>.md`) + MCP cho review |
| `learning/prompts/` | Prompt Hermes NGUYÊN VĂN (MIT, `NOTICE` ở gốc) + `render.py` chỉ đổi tên tool |
| `learning/rpc.py`, `cli.py`, `mcp_server/learning_tools.py` | RPC `learn.*`/`knowledge.*`/`learnself.call`, CLI `learn …`/`knowledge …`, MCP `hub_learn_*`, `hub_knowledge_get` |

Quy tắc: mọi thay đổi memory/skill/cấu hình do học PHẢI qua `hub.learning.pipeline.propose(...)` hoặc `Ledger.apply(Change)` — không
ghi file trực tiếp. Không thêm trạng thái chờ duyệt. Đọc tri thức luôn từ `agent["home_path"]` (eval M4-B chạy trên bản sao home).
Thêm luật guard học: thêm ca vào `tests/test_learn_pipeline.py`. Test review fork dùng `tests/fake_claude_learn.py` (không gọi claude).

## M5 luồng B — Curator liên agent & quan sát (FR-LN-12, FR-LN-09, FR-OB-07, NFR-19, KPI G-4)
Chi tiết, hợp đồng với A, lệch BRD, nghiệm thu thật: `docs/dev-notes/M5-B.md`.

| Module | Vai trò |
|---|---|
| `curation/similarity.py` | So khớp skill tất định (thuần): điểm tên+nội dung, cụm giữa agent KHÁC nhau, dấu vân tay cụm, gộp tất định |
| `curation/service.py` | `hub.curation`: liên agent → proposal L3 `scope=team created_by=curator` qua pipeline của A (không tự ghi tầng team); sau `learning.promoted` archive bản agent (`absorbed_into` = skill team). Pha 2: snapshot `bulk_tree` → run `kind=curator` (prompt Hermes nguyên văn), chặn tool hub-self (chỉ skill tầng agent, read-before-write, archive cần `absorbed_into`) → pipeline; `rollback(id)`; vòng nền khi rảnh + ngân sách `curation.*` |
| `curation/prompts.py`, `llm.py` | Prompt curator Hermes (Q-13) + phần hub; gộp liên agent bằng `claude -p --json-schema --tools ""` (run `kind=curator`) |
| `curation/kpi.py`, `overhead.py` | `report kpi` (G-4: correction lặp theo tuần/agent+loại việc, rollback, chờ duyệt), `report overhead` (NFR-19), bench FakeRuntime |
| `curation/rpc.py`, `cli.py` | RPC `curation.* report.kpi|overhead telemetry.*` (tự bọc audit); CLI `curation run|detect|phase2|list|show|rollback|status|tick`, `report kpi|overhead`, `telemetry status|flush` |
| `telemetry/otel.py` | `hub.telemetry`: OTel opt-in (`telemetry.otel.enabled`, tắt mặc định), OTLP/HTTP JSON hoặc file; span run/tool/learning, CHỈ metadata theo allowlist |

Bất biến: curation không ghi tri thức trực tiếp — mọi thay đổi qua `pipeline.propose(created_by="curator")` (ledger `actor=curator`, vẫn chịu eval
gate/regression tự rollback); pha 2 không bao giờ xoá cứng, không đụng skill pin/tầng team/memory/cấu hình. OTel không bao giờ xuất prompt, text, input/
output tool, diff hay nội dung tool `mcp__*`; thêm thuộc tính span = thêm vào allowlist + ca trong `tests/test_otel.py`. Không có migration (bản ghi ở
`logs/curation/`, `hub_state`). Test: `tests/test_curation.py`, `test_otel.py`, `test_overhead.py`.

## M5 luồng A — Tầng tri thức team/global, USER dùng chung, canary/shadow
Chi tiết, lệch BRD, hợp đồng với M5-B, nghiệm thu thật: `docs/dev-notes/M5-A.md`. Migration v10 (`tier_canaries`).

| Module | Vai trò |
|---|---|
| `knowledge/tiers.py` | `hub.knowledge.versions`: phiên bản tầng team/global theo agent (ghim bản trước khi có canary, ứng viên eval ở `<home>/.tier_overlay/`), `version_map`, `tier_overview`, `user_profile` |
| `learning/canary.py` | `hub.learning.canary`: L3 team/global/USER — eval trên golden task của các agent bị ảnh hưởng (mẫu ≥ 3) → canary (agent khởi nguồn) / shadow (USER) trong 5 run hoặc 24 h → `promoted` hoặc `Ledger.rollback(actor="canary")` |
| `learning/pipeline.py` | `_continue_tier` (L3 tầng), `_resolve_team`, `materialize` tầng team/global vào bản sao eval |

Bất biến: KHÔNG ghi file tầng team/global ngoài `Ledger.apply`; đọc tầng team/global cho prompt/skill_view LUÔN qua
`ks.versions`/`ks.effective_skills(agent_id, home=…)` (không đọc file sống trực tiếp) để agent không canary giữ bản cũ và prompt
cache. Một đích chỉ một canary. Không có bước hỏi người. CLI: `knowledge tier|versions|user`, `learn canary list|show|check`,
`learn propose --scope team --team`. Test: `tests/test_canary.py`, `test_tier_*.py`, `test_user_profile.py`, helper `tests/m5a_fake.py`.

## Hardening luồng LRN — học & điều phối (H-04, H-09, H-15, H-16, H-17, H-19, H-21)
Chi tiết, test chứng minh, việc còn lại: `docs/dev-notes/hardening-LRN.md`. Migration **v12** (`retention_cutoff` + trigger
`events_no_delete` có điều kiện). Test: `tests/test_hardening_lrn.py`, `tests/test_tui_learnings.py::test_h16_*`.

Bất biến (thay thế ghi chú M5-A "canary rollback cả thay đổi của người"):
- **Tự rollback không chạm thay đổi của người** được ép ở `Ledger.rollback` (hàm áp dụng duy nhất): bên gọi tự động
  (`AUTO_ROLLBACK_CALLERS` = regression_monitor/eval_gate/canary) gặp entry actor ngoài `AUTO_CHANGE_ACTORS` (kể cả trong
  chuỗi) hoặc đích bị sửa ngoài ledger → `Conflict(data.human_change)`, không ghi, phát `regression.detected{human_change}`.
  Thêm bên gọi tự động mới → thêm vào `AUTO_ROLLBACK_CALLERS` + ca trong `tests/test_hardening_lrn.py`.
- Rollback cấu hình chỉ khôi phục khoá entry đã đổi (`ledger.config_keys`); "cùng đích" cấu hình = trùng khoá.
- Proposal tầng team/global/USER `created_by=user` → áp dụng ngay, canary mode `watch` (không ghim, chỉ cảnh báo).
  `created_by=agent` (tool `memory` có `scope`) → cũng `watch` nhưng hồi quy thì canary TỰ rollback theo mục.
- Khoá repo giữ ĐỒNG BỘ trong `RunManager.tick` trước `create_task`; dispatcher coi run ghi cùng repo đang xếp hàng là khoá
  (`RunManager.lock_pending`). Không nới `settle` trong test để che race.
- Ngân sách ngày trừ phần giữ chỗ của run `queued|running` (`RunManager.reserved_budget`).
- Charter team = khối (3) của system prompt (`learning/runtime.team_charter`), không nằm trong tin user; `system_prompt_snapshot`
  mặc định `on`.
- Retention (`recovery.purge_retention`, `retention.events_days|raw_days|interval_s`): chỉ job này xoá event (đặt mốc rồi xoá);
  không bao giờ xoá `runs`/ledger/snapshot/audit/guard_decisions.

## Hardening luồng CLI — bề mặt & runtime (H-06, H-07, H-08, H-10, H-11, H-13, H-14, H-20)
Chi tiết, việc cần luồng khác, **danh sách lệch BRD cần cập nhật**: `docs/dev-notes/hardening-CLI.md`. Test: `tests/test_hardening_cli.py`, `tests/test_event_schema.py`.

| Module | Vai trò |
|---|---|
| `cli_surface.py` | `rewrite_argv` chạy TRƯỚC mọi bộ viết lại: `run list\|show\|logs\|stop\|follow` → `runs …` (không bao giờ thành run mới), alias `requests`, `approve --deny`, `learn pin\|unpin\|enable\|disable`, `eval golden\|results`, `feedback --good`; lệnh `stop`, `trash`, `hook` |
| `mcp_server/surface_rpc.py` | RPC `agent.delete{detach_teams}\|archive\|revert`, `trash.*`, `memory.search(_self)`, `hook.observe\|timeout`, `mcp.agent_run\|cancel_task`, `hub.compat` — đăng ký trong `mcp_server/rpc.register_rpc` trước bước bọc audit |
| `runtime/compat.py` | `hub.compat`: phiên bản CLI lúc `Hub.start` → `hub_state.cli_compat`; `raw.unknown` > 5 %/giờ → `cli.unknown_rate_alert`; nạp spool hook quá giờ |
| `observe_hook.py` | Hook quan sát PostToolUse/UserPromptSubmit/Stop, FAIL-OPEN (2 s, luôn exit 0, chỉ metadata); `add_observe_hooks()` gọi trong `_prepare_spec` sau phần guard |
| `event_schema.py` | Sinh `docs/schema/event.v1.json` (`python -m cfharness.event_schema --write docs/schema/event.v1.json`) |

Quy tắc: id agent trùng `agents.RESERVED_IDS` bị từ chối khi tạo. Lệnh con mới của `run` → thêm vào `RUN_SUBCOMMANDS` + `RESERVED_IDS`.
Cờ `claude` mới trong `build_argv`/`extra_args` → thêm vào `runtime.claude.flag_matrix[<phiên bản>]` (phiên bản trong bảng mà thiếu cờ → `ArgvError`,
run `failed`, không spawn). Lời gọi model một lượt (judge/curator) đi qua `AgentRuntime.oneshot`, không `create_subprocess_exec` trực tiếp.
Log trong code run: `logs.run_logger(log, run_id, agent_id)`; task của run đã có contextvar (`set_run_context`). Loại event mới: thêm vào
`EVENT_LEVELS` rồi sinh lại `docs/schema/event.v1.json` (test chỉ cho THÊM loại, không xoá/đổi level). Agent revision lưu cả SOUL (`agent revert`).

## Hardening SEC — an toàn trước agent cùng user OS (sandbox, xác thực RPC, toàn vẹn, che secret)
Chi tiết, spike sandbox thật, giới hạn: `docs/dev-notes/hardening-SEC.md`.

| Module | Vai trò |
|---|---|
| `auth.py` | Token người `<socket>.auth` (0600, sinh mỗi lần daemon khởi tạo, client người tự gửi `params._auth`), pid ngang hàng (`LOCAL_PEERPID`/`SO_PEERCRED`) → cây ppid/session/env của run. `HUMAN_ONLY` = method chỉ người; `learn.propose` của agent bị hạ `created_by=agent` |
| `guard/runtime.py` | `sandbox_settings` (khoá `sandbox` của settings.json: chặn ghi HUB_HOME trừ workspace, chặn đọc token/secret/mcp.json, chặn Unix socket), `secret_read_deny_rules`, `check_extra_args` (H-18) |
| `guard/integrity.py` | `hub.integrity`: bản chuẩn policy/ + config.yaml (migration v11) → khôi phục + `guard.tamper_detected`; phát hiện ghi ngoài vào hub.db; `cleanup_runtime_dirs`; RPC `guard.integrity.status|check|accept` |
| `redact.py` | Che theo GIÁ TRỊ secret (SecretStore + env-fallback) dùng chung cho DB, bus, raw stdout, log, OTel, Slack, probe |

Quy tắc: method RPC nhạy cảm mới → thêm vào `auth.HUMAN_ONLY` + lệnh CLI tương ứng vào `HUB_HUMAN_ONLY_CLI` (classifier) + test trong
`tests/test_sec_auth.py`. Writer hợp lệ mới ghi vào `policy/` → bọc trong `guard/integrity.attach` (không thì bị khôi phục). Không đưa token
người vào env của run. Test: `tests/test_sec_*.py`.
Trước phát hành: `cfharness selftest --live` (`selftest.py`, `docs/dev-notes/selftest.md`) — hub tạm + `claude` thật chạy ca đỏ, trần chi phí cứng.
Thêm lớp chặn mới → thêm `Case` (hậu quả đo ở phía người) + cập nhật `tests/test_selftest.py` (claude giả, không tốn tiền).

## Telegram — bot thông báo, duyệt bằng nút, lệnh chat (luồng TG)
Chi tiết, lý do chọn `httpx2`, test chứng minh, việc dở: `docs/dev-notes/telegram.md`. Không có migration (trạng thái ở `hub_state`).

| Module | Vai trò |
|---|---|
| `notify/base.py` | `QueuedNotifier`: khung chung các kênh (lọc `notify.<kênh>.events`, hàng đợi, backoff, `notify.sent|failed{channel}`); `SlackNotifier` là lớp con |
| `notify/telegram.py` | `TelegramApi` (httpx2 async, gỡ token khỏi lỗi), định dạng văn bản thuần đã che, `CallbackStore` (nonce dùng một lần + HMAC, theo chat), `TelegramNotifier` |
| `chat/bot.py` | `hub.telegram`: long polling trong daemon (start ở `hub.started`, tắt êm khi chưa có token), ghép nối `/start <mã>`, lệnh chat, nút duyệt (2 bước cho thao tác nặng), theo dõi run → gửi kết quả, reply → `run.continue` |
| `chat/rpc.py`, `chat/cli.py` | RPC `telegram.*` (trừ `status` đều HUMAN_ONLY, tự audit); CLI `notify telegram setup|status|test|disable|allow|revoke` |
| `chat/media.py` | `bot.media` (`MediaBase` dùng chung mọi kênh; `MediaChat` = Telegram, Slack: `chat/slack/media.py`): ảnh/tệp văn bản người gửi (tải SAU allowlist, kiểm cỡ trước `getFile`, album gom một tin) → `manager.chat{images, files}`; thoại → báo chưa hỗ trợ; dòng `[[file: <tương đối>]]` trong trả lời điều phối viên → `sendDocument` (chỉ tệp thường trong cwd hội thoại, không symlink, ≤ `deliver_max_bytes`, không chứa secret). Test: `tests/test_telegram_media.py` |

Bất biến: lệnh/nút từ chat CHỈ đi qua `Daemon.call_as(auth.telegram_caller(hub, chat_id), …)` (cổng HUMAN_ONLY + audit kênh `telegram:<chat_id>`);
`telegram_caller(hub, chat_id, from_id)` là người chỉ khi bot bật VÀ chat ∈ `allowed_chat_ids` VÀ người gửi/bấm được phép (chat riêng: from.id = chat; nhóm: `allow_groups` + from.id ∈ `allowed_user_ids`). Nội dung agent chỉ đứng SAU tiền tố cố định, KHÔNG BAO GIỜ cùng tin với nút: lệnh (tin duyệt) trong `<pre>`; trả lời/kết quả/câu hỏi là markdown → HTML whitelist qua `chat/mdconv.py` (không `<a>`, link thành chữ `nhãn (URL)`, `/lệnh` trong `<code>`; cờ `notify.telegram.agent_render: pre` quay lại `<pre>` — docs/dev-notes/telegram.md mục 2026-11-13); mọi "Cho phép" 2 bước. Chat
ngoài allowlist: không trả lời, audit `telegram.ignored`. Nút "Luôn cho phép…" chỉ khi approval có `always_offer` (daemon tính,
cùng kiểm tra với `always_prepare` — LLM-2 trong `docs/dev-notes/hardening-SEC.md`), 2 bước, bước 2 nêu luật sẽ tạo. Nonce nút bị tiêu (cả nhóm) TRƯỚC khi gọi RPC; offset `getUpdates` lưu TRƯỚC khi xử lý.
Token bot chỉ ở secret store (không đọc env, không in, không trả qua RPC). Thêm lệnh chat ghi trạng thái → gọi qua `self.call(...)` (không gọi
service trực tiếp). Test: `tests/test_telegram.py` (daemon in-process), `tests/test_telegram_e2e.py` (daemon thật + hook guard thật),
server Bot API giả `tests/telegram_fake.py` — không gọi Telegram thật.

## Slack 2 chiều — Socket Mode (duyệt bằng nút, `/cfh`, thread = reply)
Chi tiết, việc dở: `docs/dev-notes/slack.md`; plan: `docs/plan/slack-2way.md`. Lõi chat dùng chung: `chat/base.py` (`ChatBot`) —
Telegram (`chat/bot.py`) và Slack (`chat/slack/bot.py`) chỉ khác lớp vận chuyển. Thêm logic chat mới → viết ở `ChatBot` (hoặc
flows/questions/manager_chat nhận `ChatBot`), không gọi API nền tảng trực tiếp; khoá `hub_state` luôn `f"{bot.NAME}.…"`. Chữ hiển thị
trong lõi không viết cứng `/lệnh` hay "reply": dùng `bot.cmd(name)`, `bot.reply_hint/reply_here/reply_into/plain_hint/answer_box_hint`
(mặc định = chữ Telegram, giữ nguyên từng byte; Slack ghi đè).

| Module | Vai trò |
|---|---|
| `chat/slack/bot.py` | `hub.slack`: Socket Mode (`websockets`) trong daemon, ACK trước xử lý + hàng đợi tuần tự + chống trùng `slack.seen`; `/cfh <lệnh>` → DM; nút; thread → câu hỏi / giao việc / `run.continue` / điều phối viên; `SlackChatNotifier` (khi hoạt động, `SlackNotifier` webhook nhường) |
| `chat/slack/media.py` | `SlackMedia`: tin `file_share` (DM, sau allowlist) → tải `url_private` bằng bot token (chỉ host `files.slack.com`, stream + trần, không redirect) → `manager.chat{images, files}`; `[[file: …]]` → files v2. Test: `tests/test_slack_media.py` |
| `chat/slack/render.py` | HTML hub → mrkdwn + Block Kit; escape `& < >`, `verbatim`, ` trong khối mã → ˋ |
| `chat/slack/api.py`, `rpc.py`, `cli.py` | `SlackApi` (gỡ token khỏi lỗi); RPC `slack.*` (trừ `status` đều HUMAN_ONLY); CLI `notify slack setup|status|test|disable|allow|revoke` |
| `web/slack_pages.py` (+ `app.SLACK_MANIFEST_2WAY`, `_slack_pending.html`) | Wizard web "Slack 2 chiều" ở Cài đặt: link tạo app (manifest Socket Mode), dán 2 token → `slack.setup`, mã ghép nối trong `Session.extra`, poll `slack.pending` (htmx), `slack.allow|revoke|test|disable` qua `ACTIONS` (CSRF + khoá phiên, audit `web`) |

Bất biến: như Telegram — mọi lệnh/nút qua `Daemon.call_as(auth.slack_caller(hub, chat_id, user_id), …)`; người = `interactive` bật + chat ∈
`allowed_chat_ids` + user ∈ `allowed_user_ids` (+ `allow_channels` cho kênh không phải DM); envelope khác `team_id` ghim → bỏ. Token bot/app chỉ
ở secret store. Test: `tests/test_slack.py`, `tests/test_slack_questions.py`, `tests/test_slack_modals.py`, `tests/test_slack_media.py`,
`tests/test_web_slack.py` (wizard web), Slack giả `tests/slack_fake.py` (HTTP + WebSocket cục bộ).

## WEB — giao diện trình duyệt (`cfharness web`)
Chi tiết, kiểm chứng sandbox thật, việc dở: `docs/dev-notes/web.md`. Ảnh: `docs/screenshots/web/`. Test: `tests/test_web.py`.

| Module | Vai trò |
|---|---|
| `web/security.py` | Chỉ loopback; host riêng `cfharness.localhost` (WT-1); link đăng nhập MỘT LẦN (HMAC, khoá ở RAM + `HUB_HOME/auth/web.json` 0600) → cookie phiên HttpOnly/SameSite=Strict + khoá phiên NGOÀI cookie (`k`, localStorage) bắt buộc cho mọi POST; CSRF; Host hợp lệ |
| `web/app.py` | Starlette + Jinja2 + htmx vendored + SSE. `GuardMiddleware` ASGI thuần (không dùng BaseHTTPMiddleware — làm hỏng SSE). Ghi = `POST /do/<action>` (bảng `ACTIONS`) hoặc form agent |
| `web/present.py` | Thuần: nhãn tiếng Việt, `transcript_item`, `activity_item`, `md()` an toàn |
| `web/cli.py`, `web/demo.py` | `cfharness web [--port] [--open] [--url]`; hub demo FakeRuntime để chụp ảnh |
| `web/perm_pages.py` | `/permissions` "Quyền đã cấp": xem + gỡ trust/grant theo luật/luật luôn cho phép/ngoại lệ sandbox, thêm/gỡ tài nguyên bảo vệ (RPC `guard.*` có sẵn, service tự audit kênh web). Thu hẹp quyền = 1 bước; NỚI quyền = `Action.step2` (bước 2 bắt buộc, `plan_action`). Cùng nhánh: sửa lịch + lịch sử lượt `/schedules/{id}`, nút `/lich` Telegram/Slack, `manager.archive\|unarchive` + "Chạy nhịp ngay"/xem nhịp ở `/manager` — `docs/dev-notes/web-permissions.md` |
| `web/memory_pages.py`, `static/memory_graph.js` | `/memory…`: memory mọi tầng (thêm/sửa/xoá/ghim mục qua `learn.propose`/`knowledge.pin`), graph memory (tìm, fact/thực thể, rollback, ghim, consolidation, đồ thị SVG tự viết). RPC chỉ đọc `learn.graph_view` (`learning/graph_view.py`, HUMAN_ONLY). Chi tiết: `docs/dev-notes/web-memory.md`, test `tests/test_web_memory.py` |

Quy tắc: web là CLIENT thuần (RPC qua `AsyncHubClient(channel="web")`), không đụng `hub.db`. Thao tác ghi mới → thêm vào `ACTIONS`
(nhạy cảm: đặt `confirm`), gửi `audit=True` (approval: `channel="web"`). Không script/style inline (CSP `'self'`), không CDN; tài nguyên
tĩnh mới đặt trong `web/static/`. Không bao giờ đưa token người/khoá `auth/`/`Session.key` vào template, cookie, log (trang xác nhận mang trường form qua `carry()` — đã loại `k`).
Duyệt: hiện TOÀN BỘ thao tác (`present.action_view`, không cắt); lớp nặng (`notify.telegram.confirm_classes` + `file`, tài nguyên bảo vệ) cần bước 2. "Luôn cho phép" (`approve_always`) chỉ hiện khi `a.always_offer` và LUÔN cần bước 2 nêu luật. `config.set` chỉ khoá trong `SAFE_CONFIG`.
Agent trong run: sandbox chặn TCP localhost (đã kiểm thật) + denyRead `HUB_HOME/auth` + `cfharness web` forbidden trong classifier.

### Web public (tunnel TLS) + TOTP + LaunchDaemon + Claude dùng chung — `docs/dev-notes/web-public.md`
`web/public.py` (`web.public_origin` chỉ https, `client_ip` chỉ tin header proxy khi có public_origin + peer loopback, `LoginLimiter` theo IP/toàn cục/ngày),
`web/totp.py` (RFC 6238 stdlib, `HUB_HOME/auth/totp*.json` 0600), `/enroll` + ô mã ở `/login`, `cfharness web totp enroll|status|disable`, `web --no-link`.
Bind VẪN loopback; nhánh config `web` ∈ `integrity.SECURITY_KEYS`, không có trong danh mục Cài đặt web; MCP HTTP KHÔNG public.
`systems/autostart.LaunchDaemonPlan` + `cfharness admin install-daemon --user U [--web-port N] [--write]` (∈ `HUB_HUMAN_ONLY_CLI`).
Secret `CLAUDE_CODE_OAUTH_TOKEN` → env của mọi tiến trình `claude` (`ClaudeCliRuntime(secrets=)`, `child_env(oauth_token=)` — tạo runtime mới phải truyền
`secrets=hub.secrets`); không bao giờ vào argv/DB/plist. Test: `tests/test_web_public.py`.
Thiết bị tin cậy (`web/devices.py`, `auth/trusted_devices.json` chỉ hash, cookie `__Host-…_dev`): CHỈ host public https (không bao giờ `cfharness.localhost`),
hạn tuyệt đối, xoay + phát hiện dùng lại, gắn `totp_fp`; thu hồi = `cfharness web devices …`/Cài đặt (phiên của thiết bị chết ngay qua `WebAuth.device_ok`).
Vào/ra web + thiết bị → RPC `web.notify` (HUMAN_ONLY, `web_events.py` lọc metadata theo danh sách trắng) → event `web.*` → Telegram nhóm riêng (`security_chats`). Test: `tests/test_web_devices.py`.

## M7 luồng MGR-UI — Điều phối viên trên web + Telegram + terminal
Chi tiết, hợp đồng dùng, test chứng minh, việc dở: `docs/dev-notes/M7-ui.md`. Ảnh: `docs/screenshots/web/16-…18-*.png`, `docs/screenshots/m2/10-11-dieu-phoi*.svg`. Không migration, không RPC mới.

| Module | Vai trò |
|---|---|
| `web/manager.py` | Trang "Điều phối" (`/manager`, đầu menu): chat theo conversation kênh `web` (`manager.chat/history/conversations`), SSE `/sse/manager` (agent = điều phối viên: delta/draft/seg/think/tool/reply/action/run_end/tick, mọi message mang `seq` → client bỏ trùng + tự nối lại `since=<seq cuối>`), `/partials/manager`, `POST /manager/send`, `POST /manager/stop` (nút Dừng: chỉ run của điều phối viên thuộc hội thoại điều phối, `run.stop` audit `web`); `ACTIONS` `manager_set`/`manager_unset` (xác nhận) |
| `web/present.py` (`mgr_*`) | Thuần: đích `manager.action` → link NỘI BỘ (loại đã biết + id an toàn), chuẩn hoá history/conversation |
| `chat/manager_chat.py` | `hub.telegram.manager`: tin không `/` → `manager.chat(channel=telegram:<chat>)`, reply → đúng conversation (`hub_state telegram.mgr_*`), `/moi`, `manager.reply` (theo `channel`) → khung WT-8 |
| `web/manager_stub.py` | Stub `manager.*` cùng chữ ký — CHỈ test/demo (`install(daemon, force=)`) |
| `manager/repl.py`, `manager/chatview.py` | `cfharness chat` (= `manager chat` không nội dung): REPL `prompt_toolkit` (nhập dính đáy, thanh trạng thái spinner/tool/chữ chạy, `/new /list /open /history /stop`, steer khi đang làm, Ctrl-C dừng lượt, lịch sử `HUB_HOME/cli_history` 0600); không TTY → từng dòng stdin. `ChatState`/`render` thuần dùng chung với TUI. `manager chat "<tin>"` chờ qua `events.subscribe` (`repl.oneshot`) |
| `manager/agentsview.py` | `cfharness chat` thấy cả đội: `AgentMonitor` (bọc `tui.model.TopModel`: `tui.snapshot` ~1 s + firehose summary, đăng ký lại khi `slow_consumer`; `question.list`), bảng agent trong toolbar (↓/Esc), thẻ duyệt/câu hỏi của MỌI agent + chuông, `/agents /stop <agent> /attach /approvals /approve /deny /always /answer` (RPC có sẵn, y/N; `/always` 2 bước + `always_confirm`). Hợp đồng định tuyến: `routed_to` ∈ manager/human/None + event `approval.delegated|escalated` — manager = không chuông. Style ở hằng `chatview.C_*`/`AGENT_STATE`. Test: `tests/test_chat_agents_panel.py` |
| `tui/chat.py` | `cfharness chat` trên TTY (mặc định; `--classic` = REPL cũ): `ChatApp(TopApp)` kênh `cli` + `ChatScreen` fullscreen kiểu Claude Code. Lõi = `ChatSession` (lớp con: `ScreenOut`, y/N → `ConfirmScreen`, `/attach` → `AttachScreen` trong app); `TopApp.model` = model của `AgentMonitor` (một firehose). Mỗi mục một widget: cuộn chuột/PgUp/PgDn, chữ đang chạy, dòng tool của điều phối viên click/Ctrl-O mở rộng (`LiveChatState`), thẻ hành động click → `tui.manager.open_target`, thẻ duyệt/câu hỏi click → ĐIỀN SẴN `/approve`/`/answer` (duyệt vẫn qua Enter + y), bảng agent đang chạy dưới ô nhập (click/↓ → Attach), kéo chọn → copy (OSC 52 + `attachments.clipboard_set`), Ctrl-G bảng agent. Hình giống Claude Code CLI, vẽ bằng `tui/cc_render.py`: không Header/Footer, hộp chào viền cam (hook `ChatSession.banner`), `> tin người`, `● trả lời`, `● Tool(args)` + `⎿`, thẻ duyệt trong hộp viền tím, spinner trên ô nhập chỉ khi bận, ô nhập viền tròn `> `, dòng `? phím tắt` / menu `/` (`?` ở ô trống = bảng phím tắt), bảng agent `❯` khi chọn. Nền màn đặt ở `ChatApp.CSS` (CSS app thắng `DEFAULT_CSS`). Textual gọi `on_mount` của mọi lớp trong MRO → `ChatApp.on_mount` phải `prevent_default()`. Mục phải là MỘT `Text` (`flatten`) thì mới kéo chọn được. Test: `tests/test_chat_app.py` |
| `tui/manager.py` | `ManagerScreen` (phím `m` ở `top`): hội thoại + "Hành động" (Enter → Attach/Tasks, chỉ đích hợp lệ) + transcript `RichLog(markup=False)` + ô nhập; Ctrl-N mới, Ctrl-T dừng lượt. Hội thoại kênh `cli` DÙNG CHUNG với `cfharness chat`; audit kênh `tui` |
| `termsafe.py` | `clean()`/`one_line()`: bỏ ANSI/OSC/C1, điều khiển, bidi, zero-width trước khi in chữ agent ra terminal |

Bất biến: chữ của điều phối viên (trả lời, tóm tắt/đích hành động) là DỮ LIỆU. Web: câu trả lời/báo vẽ bằng `present.md()` đã cứng hoá
(chủ repo chốt Q1 2026-11-13 — `docs/dev-notes/plan-chat-ui-claude.md`: `html=False`, không `<img>`, link ngoài `rel=nofollow noopener noreferrer`
+ tên miền, `javascript:`/`data:` không thành link, không `style=`) — đầu ra của `md()` là thứ DUY NHẤT được `|safe`/innerHTML (`draft`/`reply` qua
SSE); tóm tắt/đích hành động, suy nghĩ, tên tool, tin của NGƯỜI vẫn là autoescape/`textContent` (`pre-wrap`); link nội bộ chỉ từ `mgr_target`;
delta gán `textContent`; Telegram chỉ sau tiền tố cố định (markdown → HTML whitelist, `chat/mdconv.py`), không bao giờ kèm nút.
Mọi ghi từ web qua `post_guard` (CSRF + khoá phiên WT-1) với `_audit.channel=web`; từ Telegram qua `bot.call` (cổng người + allowlist from.id, audit
`telegram:<id>`). Terminal (REPL/TUI): chữ agent qua `termsafe.clean` + `rich.Text` (không markup, không OSC 8 link); `cfharness chat` ∈ `HUB_HUMAN_ONLY_CLI`.
Ngoại lệ (chủ repo quyết 2026-10-26, giống Claude Code): `cfharness chat` fullscreen + màn xem agent vẽ lời agent bằng MARKDOWN qua `tui/cc_render.markdown_text`
— vẫn `termsafe.clean` TRƯỚC khi parse, `hyperlinks=False` + gỡ mọi style `link`/meta (không OSC 8, link chỉ là chữ), markup Rich không được đọc. Test: `tests/test_cc_render.py`.
Test: `tests/test_web_manager.py`, `tests/test_telegram_manager.py`, `tests/test_manager_repl.py`, `tests/test_tui_manager.py` (thêm ca XSS/chèn ANSI mới vào đây).

## M7 MGR-CORE — Điều phối viên (manager: agent harness quản lý + giao việc)
Chi tiết, **chữ ký RPC cho MGR-UI**, nghiệm thu thật: `docs/dev-notes/M7-core.md`. Migration **v13** (`manager_conversations`, `manager_messages`).

| Module | Vai trò |
|---|---|
| `manager/service.py` | `hub.manager`: chỉ định (`hub_state.manager`, `require_human`), hội thoại theo kênh (run `kind=manager`, `--resume`, cwd cố định `workspaces/conv-<id>`), `manager.reply`, nhịp heartbeat (gom sự kiện + định kỳ, khoá chống chồng, không chạy khi ESTOP), `self_call` |
| `manager/permissions.py` | Luật "toàn quyền trừ an toàn" — `check_rpc` chạy ở CẢ `ManagerOps` (lớp 1) và `Daemon._dispatch` cho `Caller.manager` (lớp 2) |
| `manager/ops.py` | Tool hub-self `manager` (đọc toàn hub; ghi qua `Daemon.call_as(auth.manager_caller)` hoặc `Ledger.apply` actor `manager:<agent>`) → event `manager.action` |
| `manager/rpc.py`, `cli.py`, `prompts.py` | RPC `manager.get|set|chat|conversations|history|tick`, `managerself.call`; CLI `cfharness manager …`; prompt chat/tick (tiếng Anh) |
| `manager/doc_extract.py`, `manager/pdf_worker.py`, `pdf_support.py` | Tệp đính kèm cho điều phối viên: nhận diện theo nội dung + bản đọc được (Office → CSV/chữ bằng stdlib; PDF → `.txt` theo trang (dòng đầu nêu phạm vi trang) + phần `.p001-050.pdf` bằng `pypdf` trong TIẾN TRÌNH CON có hạn giờ, ghi từng sự kiện ngay (bị giết vẫn giữ phần đã xong); các PDF của một tin song song trong thread — không chặn loop, dưới hạn 60 s của web; `chat` khoá theo hội thoại giữ thứ tự tin). `pdf_support`: dò + chạy thử `pdftoppm -v` (cache), `tools.poppler_path` (∈ SECURITY_KEYS, thêm CUỐI PATH của run), ghi chú system prompt/`files_note` khi thiếu, mục `hub doctor`. `docs/dev-notes/pdf-attachments.md`; test `tests/test_doc_extract.py`, `tests/test_pdf_support.py` |
| `templates/manager.yaml` | Persona điều phối viên (tiếng Anh, báo cáo tiếng Việt). Tạo từ template KHÔNG cấp quyền — chỉ `manager set` |

Bất biến: chỉ NGƯỜI chỉ định manager (`manager.set|chat|tick` ∈ HUMAN_ONLY). Quyền của manager kiểm ở DAEMON, không tin tool list:
method ngoài `auth.MANAGER_ALLOWED` hoặc vi phạm `check_rpc` → từ chối + audit actor `manager:<agent>`. Thêm tool ghi cho manager → thêm method
vào `MANAGER_ALLOWED` (kèm lý do), luật vào `permissions.check_rpc`, ca đỏ→xanh vào `tests/test_manager_security.py` (cả lớp 1 lẫn lớp 2).
Thay đổi cấu hình agent do manager = actor `manager:<agent>` = thay đổi TỰ ĐỘNG (`ledger.is_auto_change_actor`) → được tự rollback.
Khung quyền (§9 `docs/dev-notes/M7-core.md`): `manager.capabilities{profile strict|hermes, tools, mcp, web}` CHỈ người đặt (`cfharness manager
capabilities set …` = `config.set`, nhánh `manager` kiểm toàn vẹn); strict = Read/Grep/Glob (mặc định), hermes = Bash/Write/Edit/Web + MCP trusted
(chỉ đọc) — luôn dưới hook guard + sandbox OS + approval (khung rộng đòi `guard.enabled` + `guard.sandbox.enabled`), không bao giờ Task/subagents/
bypass. `manager_config_problem` kiểm theo khung ở `manager.set` và mỗi run. Run manager: classifier cấm thẳng ghi `hub-*`, loopback/LAN
(curl/WebFetch), `claude` lồng, CLAUDE.md/.claude; tiến trình con gọi socket chỉ được `managerself.call` (`ManagerService.run_socket_block`).
Thêm tool vào khung → `CAP_PROFILES`/`CAP_TOOLS_MAX` (permissions.py) + ca trong `tests/test_manager_capabilities.py`.
Không bao giờ: manager duyệt approval CHƯA được người giao (chỉ `routed_to=manager` qua `approval.manager_decide` — `docs/dev-notes/approval-profiles.md`), sửa policy/guard/golden/sandbox, purge, tắt ESTOP, sửa agent manager (kể cả chính mình/manager cũ),
`bypassPermissions`/`allow_bypass`/`extra_args`/`subagents`, bỏ mẫu disallowed người đặt, MCP ngoài registry. Test: `tests/test_manager*.py`
(helper `tests/m7_fake.py`, ScriptedRuntime giữ run manager ở `running`).

## UX — câu hỏi có lựa chọn (agent → điều phối viên → người); làm lại web/Telegram
Kế hoạch, quyết định, tiến độ: `docs/dev-notes/ux-plan.md`. Migration **v14** (`questions`). Test: `tests/test_questions.py`.

| Module | Vai trò |
|---|---|
| `questions/service.py` | `hub.questions`: `open` (định tuyến `with_manager` nếu agent trong task + có manager sẵn sàng, ngược lại `open` cho người), `answer`, `escalate`, `cancel`, nối tiếp (task → bình luận `answer` + `unblock`; hội thoại manager → `manager.chat`; run → `run.continue`), vòng nền quá hạn → người + nhắc lại giãn dần |
| `questions/rpc.py`, `cli.py` | RPC `question.*` (ghi = HUMAN_ONLY, tự audit; `by`/`channel` lấy từ người gọi), CLI `questions`, `answer` |

Bất biến: không bao giờ tự trả lời thay người (không `expired`). Agent không trả lời/chuyển/huỷ câu hỏi (HUMAN_ONLY + classifier). Điều phối viên chỉ
`question.answer|escalate` khi câu hỏi `with_manager` và không phải của chính nó — luật ở `manager/permissions.check_rpc` (cả 2 lớp). Nội dung câu hỏi
và nhãn lựa chọn là DỮ LIỆU agent (đã che secret, bỏ ký tự điều khiển/vô hình): Telegram đặt sau tiền tố cố định (markdown đã escape, `bot.agent_body`), nút chỉ mang số (WT-8).

## Đổi tên cfharness — tên mới + tên cũ, `home migrate`
Chi tiết, cách áp, việc người dùng phải làm, lệch BRD: `docs/dev-notes/rename-cfharness.md`. Test: `tests/test_rename_cfharness.py`.

| Module | Vai trò |
|---|---|
| `names.py` | Nguồn DUY NHẤT của tên lệnh/module/env/thư mục home/socket/MCP server — tên mới VÀ tên cũ. Guard (classifier, `is_hub_mcp_config`, threats, `neutralize`, auth, sandbox) dựng mẫu từ đây |
| `home_migrate.py` | `home status|migrate [--dry-run] [--stop]`: chuyển home tên cũ → home mới, viết lại đường dẫn ở bảng không append-only + file cấu hình, cập nhật bản chuẩn toàn vẹn |
| `scripts/rename_cfharness.py` | Đổi tên máy móc toàn repo (idempotent, `--dry-run`); bỏ qua dòng `rename:keep` |

Bất biến: guard nhận diện tên cũ VĨNH VIỄN (binary/module cũ có thể còn trong venv). Thêm tên/biến thể → sửa `names.py` + ca hai chiều trong
`tests/test_rename_cfharness.py`. Literal tên cũ chỉ được nằm trên dòng có `rename:keep`. `home migrate` là lệnh chỉ người (`HUB_HUMAN_ONLY_CLI`).

## Hệ thống ngoài hub — app trong ~/Workspace + job launchd (`systems/`)
Chi tiết, quyền, nghiệm thu thật, việc dở: `docs/dev-notes/systems.md`. Không migration (`hub_state systems.registry|systems.paused`). Test: `tests/test_systems.py`, `tests/test_web_systems.py`, ca `systems` trong `test_telegram.py`/`test_slack.py`.

| Module | Vai trò |
|---|---|
| `systems/launchd.py` | Đọc plist (thuần, `plutil` dự phòng) + `Launchctl` (runner thay được — test KHÔNG gọi launchctl thật): state/disabled/pause/resume/restart miền `gui/<uid>` |
| `systems/service.py` | `hub.systems`: nhận diện (gốc `systems.roots` → thư mục con + job theo cwd/đường dẫn), registry, `job(label, action)` + `job_check` dùng chung với quyền manager |
| `systems/rpc.py`, `cli.py` | RPC `system.list|get|scan|add|rm|lock|job` (ghi = HUMAN_ONLY, tự audit); CLI `cfharness system scan|list|show|add|rm|lock|unlock|pause|resume|restart` |
| `web/systems_pages.py` (+ `systems.html`, `system.html`, `_systems.html`) | Trang `/systems` "Hệ thống ngoài": danh sách + quét thử/đăng ký + thêm + gỡ + khoá + pause/resume/restart job qua `ACTIONS` (xác nhận, audit `web`); `/systems/{id}` đuôi log. Chat: `ChatBot._c_systems` (`/hethong`, nút job 2 bước `yp|yr|yk`) |

Bất biến: đăng ký/khoá = CHỈ người. Điều phối viên chỉ `system.job` (MANAGER_ALLOWED + `check_rpc` → `job_check`) trên job thuộc hệ thống đã đăng ký,
không khoá, trong trần `manager.max_system_ops_per_hour`. Không bao giờ nhận diện thư mục/job của chính hub. `launchctl` chỉ chạy trong daemon.

## Xin phép người bằng nút + giảm cổng chặn (`ask_owner_permission`)
Chi tiết: `docs/dev-notes/owner-permission.md`. Migration **v15** (`questions.action`). Test: `tests/test_owner_permission.py`.
- `manager/owner_actions.py`: điều phối viên ĐỀ XUẤT (approve_action, sandbox_localhost, sandbox_domain, agent_update) → câu hỏi do HUB soạn,
  thao tác chốt sẵn → NGƯỜI chọn "Cho phép" → hub thực hiện bằng `call_as(internal)` + audit kênh trả lời → kết quả về điều phối viên.
  Thêm kind mới → `KINDS` + `build()` (kiểm hợp lệ, cấm đích là agent manager) + ca trong test.
- `ManagerService._owner_shortcut`: tin người trong chat ("1", "ok", "duyệt", `approve ap_…`, `answer q_… 1`) → làm ngay, gắn ghi chú `[hub]`.
- `guard.hold_uncertain=false` → chỉ `destructive` hỏi người (giữ uncertain vùng hub/policy + `mcp_require_approval`). `guard.enabled=false`
  → cho phép sẵn tool của agent (`GUARD_OFF_ALLOWED_TOOLS`), giữ mẫu chặn tĩnh + sandbox.


## Lịch chạy tự động — `schedules/` (kiểu `cronjob_manage` của Hermes)
Chi tiết, quyền, đỏ đã kiểm, lệch Hermes, việc dở: `docs/dev-notes/schedules.md`. Migration **v16** (`schedules`, `schedule_fires` — gộp nhánh thì
đánh số lại). Test: `tests/test_schedules.py` (đồng hồ giả `hub.schedules.clock`, gọi `tick()` trực tiếp).

| Module | Vai trò |
|---|---|
| `schedules/cron.py` | Parser THUẦN stdlib: cron 5 trường, `@daily…`, `every 30m`, `daily 08:00`, `weekdays 08:30`, `weekly …`, `monthly …`, `at …`/`in 30m`, alias tiếng Việt; `next_after`, `min_interval_s`, `runs_per_day` (giờ máy, zoneinfo) |
| `schedules/service.py` | `hub.schedules`: CRUD (xoá mềm), vòng nền (start ở `hub.started`), bỏ lượt (ESTOP/overlap/agent pause/team ESTOP), chạy bù ≤ 1 lần trong `catchup_grace_s`, bắn manager (`ManagerService._submit` kind `schedule` trong hội thoại đích) / agent (`runs.submit kind=schedule`) / task (`created_by=schedule:<id>`), giao kết quả (`manager.reply{kind:notify}`), tự dừng khi lỗi lặp, `manager_check` |
| `schedules/rpc.py`, `cli.py` | RPC `schedule.list\|get\|preview\|create\|update\|pause\|resume\|rm\|run_now` (ghi ∈ HUMAN_ONLY, tự audit); CLI `cfharness schedule …`; Telegram `/lich` (đọc) |

Bất biến: `created_by` lấy từ người gọi. Điều phối viên chỉ ghi lịch `created_by=manager:*` (lịch người = chỉ đọc), trong trần nhánh config
`schedules.*` (kiểm toàn vẹn), không từ lượt của lịch; luật ở `ScheduleService.manager_check`, gọi từ `manager/permissions.check_rpc` (2 lớp) và
`_gate` (lần 3). Nội dung lịch do điều phối viên viết vào prompt trong khung `<untrusted-data>`, tiêu đề `[cfharness · SCHEDULED JOB · key …]`
(không bao giờ thành "tin người"). Thêm luật/ trần → ca đỏ→xanh trong `tests/test_schedules.py`.

## Luồng việc (workstream) — mỗi epic một luồng cho điều phối viên
Chi tiết: `docs/dev-notes/workstreams.md`. Bảng `workstreams` (v24) + `workstream_id` trên `manager_conversations`/`tasks`; service
`manager/workstreams.py` (`hub.manager.ws`). Session mới của hội thoại trong luồng nạp STATE.md + task mở (`context_block`); Sảnh nạp
danh sách luồng. Tick xử lý MỘT luồng mỗi lượt trong hội thoại nhịp riêng của luồng. `notify_owner` của task thuộc luồng → hội thoại
của luồng. Tool `workstream_*`, `conversation_set_workstream`, `task_create(workstream)`. RPC người `workstream.create|update`,
`manager.conversation_update|read` (HUMAN_ONLY). Web `/manager?w=` + thanh bên Sảnh/Luồng; REPL `/ws`.
Test: `tests/test_manager_workstreams.py`, `tests/test_web_workstreams.py`.

## Hồ sơ phê duyệt — `approval.profile` strict|balanced|autonomous, giao điều phối viên duyệt
Chi tiết, dữ liệu hub thật, bảng profile, ranh giới, lưu ý cài đặt cũ: `docs/dev-notes/approval-profiles.md`. Migration **v18**
(`approval_requests.routed_to…timeout_action`). Test: `tests/test_approval_profiles.py`.

| Module | Vai trò |
|---|---|
| `guard/profiles.py` | `profile()`, `knob()` (khoá đặt rõ trong config.yaml thắng profile — đọc khoá profile qua đây, không `config.get`), `never_automatic()` (ranh giới DUY NHẤT cho mọi đường tự động), `delegable()`, `on_timeout()`, kiểm `config.set approval.*` |
| `guard/service.py` | `auto_allow` (checkpoint / scratch autonomous), `route_for` (giao điều phối viên), on_timeout `deny_and_continue`, `always_offer` kind `mcp_tool` |
| `approvals.py` | `routed_to`, `approval.delegated/escalated`, `manager_decide`, `check_delegations`, phê duyệt muộn của deny_and_continue, `approval_stats` |
| `guard/classifier.py` | `own_worktree_write`, `git_reset_hard_safe`, luật `mcp_tool`, ghi đè annotations (`human_override`) |

Bất biến: mọi đường tự động gọi `never_automatic` trước; thêm luật/lớp nhạy cảm → thêm vào `NEVER_RULES`/`NEVER_KLASS` + một test `test_never_*`.
Điều phối viên chỉ quyết approval `routed_to=manager` (luật ở `permissions.check_rpc` lớp 1+2 và `ApprovalService.manager_decide` lớp 3), không bao giờ
approval của run điều phối viên, không bao giờ `always`. Q-04 giữ: mặc định hết giờ = `wait`.

## Cổng duyệt — công tắc chung `gates.mode` + không hỏi lại theo luật
Chi tiết: `docs/dev-notes/gates.md`, `docs/dev-notes/trust-and-chat-settings.md` (mục "Không hỏi lại theo agent + luật"). Không migration.
- `gates.mode` human|manager|off (chỉ người; đọc qua `guard/profiles.gates_mode`). manager = `delegable()` mở mọi lớp kể cả danh sách
  "không bao giờ" — approval của chính run/agent điều phối viên vẫn chỉ người. off = guard cho phép mọi thứ lẽ ra phải hỏi (forbidden/ESTOP giữ).
  Thêm cổng chờ người mới → đọc `gates_mode` và có ca trong `tests/test_gates.py`.
- Grant theo agent + luật: `hub_state.agent_rule_grants`, `approval.approve{grant_rule}`, `guard.grant.set|rm|list` (HUMAN_ONLY), áp ở
  `GuardService.check` (classifier `rule_grant`). Test: `tests/test_rule_grant.py`.

## Chế độ 3D văn phòng (`/office`)
Hợp đồng, quyết định, ánh xạ trạng thái → nhân vật: `docs/dev-notes/office-3d.md`. Không RPC mới, không migration. Test: `tests/test_web_office.py`. Hiệu năng (nhịp vẽ động 60 / yên 30, đo `#perf3d`): `docs/dev-notes/office-3d-perf.md`.
**Chế độ 3D = toàn trang 3D**: `/office` là trang độc lập (không `base.html`), thấy được chỉ canvas + nút "2D" nổi; HUD/thẻ nhân vật/kanban/
"Cần bạn"/run đang chạy/hết phiên đều vẽ TRONG cảnh; danh sách chữ chỉ `sr-only`. Đang 3D thì MỌI trang (trừ `/office`, `/login`) → `/office`.

| Module | Vai trò |
|---|---|
| `web/office.py` | `GET /office` (trang độc lập), `GET /office/state` (JSON gọn, `no-store`; + `tasks` ≤ 5/cột, `approvals`, `questions`, `runs`, `hub.cost_today_usd` — chữ tự do cắt ở server bằng `one_line`; chưa đăng nhập → 401 qua `GuardMiddleware.API`, polling không gia hạn phiên), `GET /sse/office` (`_stream` + `office_frames`: danh sách trắng loại event → khung `{t:"ev", seq, type, agent, team, tool, status, outcome}` + `{t:"refresh"}`). Chỉ RPC đọc `tui.snapshot`, `question.list`, `team.list`, `task.list`, `manager.get`, `hub.status` |
| `web/static/office.js` (+ `static/office/{world,chibi,actors,path,boards,util,ui,flows,chat}.js` — `ui` = bảng canvas + nút DOM ẩn song sinh, `flows` = duyệt/xác nhận/bước 2/câu hỏi/thoát, `chat` = màn chat điều phối viên, `office.css`, `templates/office.html`) | Cảnh Three.js: nhân vật chibi dựng thủ tục, A* trên lưới sàn, bảng/thẻ/biển trong cảnh (`boards.js`); chữ chỉ `fillText` (DOM: `textContent` trong khối sr-only) |
| `web/static/vendor/three-0.169.0/` | Three.js đóng gói sẵn (MIT, `LICENSE`), `OrbitControls.js` import tương đối — không CDN/importmap |
| `early.js` / `app.js` / `base.html` | Nút `#view-toggle` (`data-view-toggle`, link thường tới `/office` khi không JS), khoá `cfh_view` (xoá khi đăng nhập `#k=` và khi đăng xuất), `cfh_view=3d` + trang khác `/office`/`/login` → `/office` |

Bất biến: `/sse/office` và `/office/state` KHÔNG BAO GIỜ mang chữ tự do (text, input, args_preview, result_preview, summary, reason…) — chỉ id/tên tool
(`[A-Za-z0-9_\-:.]`, ≤ 64)/từ khoá trạng thái; `/office/state` chỉ thêm chữ đã cắt: `task` ≤ 80, tiêu đề task ≤ 60, preview approval/chữ câu hỏi ≤ 120
(một dòng, bỏ ký tự điều khiển/bidi) — dữ liệu agent, client chỉ `fillText`. Cảnh 3D không link sang trang 2D; không thêm phần tử 2D nhìn thấy vào `/office`.
Event kết thúc run chỉ nhận `source=="hub"`. Thêm loại event vào cảnh → thêm vào `office.ALLOWED` + ca trong test. Không nới CSP (không `blob:`, WASM, worker,
script inline).

Giai đoạn 2 (duyệt + câu hỏi + chat trong 3D, mục "Giai đoạn 2" của hợp đồng): ghi từ cảnh 3D CHỈ qua `POST /office/do/{name}` — danh sách trắng
`office.OFFICE_ACTIONS` (approve, deny, approve_always, approve_rule, answer→`question_answer`), dùng ĐÚNG `Action` của `app.ACTIONS`, sau `post_guard`
(Origin, CSRF, khoá phiên `k`) và CÙNG cổng `app.plan_action` với `do_action` 2D (xác nhận → bước 2 heavy/always/grant). Không gọi RPC ghi khi chưa có
`confirm=yes` (3D hỏi cả thao tác 2D không hỏi: `OFFICE_CONFIRM`) và `confirm2=yes` khi có bước 2. Thêm điều kiện cổng → sửa `plan_action` (áp cho cả 2D/3D),
không viết cổng riêng cho 3D. Thêm thao tác 3D → `OFFICE_ACTIONS` + ca đỏ→xanh trong `tests/test_web_office.py`. Chữ đầy đủ (thao tác chờ duyệt — `present.action_view`,
câu hỏi, chat) CHỈ ở `GET /office/approval|question/{id}`, `/office/chat`, `/sse/office/chat` (JSON, không HTML; client chỉ `fillText`/`textContent`),
không bao giờ trong `/sse/office`. Đường JSON của 3D nằm trong `GuardMiddleware.API` (tiền tố): chưa đăng nhập → 401; GET không gia hạn phiên.

## Tool `memory` của agent (kiểu Hermes) — ghi memory ngay trong session
Chi tiết, lệch Hermes: `docs/dev-notes/agent-memory-tool.md`. Agent đang chạy (vai trò hub-self worker/lead/reviewer/knowledge/manager) có tool
`memory(target memory|user, action add|patch|remove | operations[≤10])` → RPC `learnself.call` op `memory` → `LearningService._agent_memory` →
`pipeline.propose(created_by="agent", scope="agent")`: VẪN qua guard/threat scan/hạn mức/rate/ledger (rollback được), chỉ tầng agent (team/global vẫn
chỉ review đề xuất L3). Lô `operations` = một proposal = một ledger entry, hạn mức ký tự xét trên kết quả cuối (`knowledge.store.apply_memory_ops`);
`propose_memory` của review cũng nhận `operations`. Kết quả trả mục hiện có + usage; system prompt của session KHÔNG đổi (snapshot đóng băng).
Không có trong run review/eval/curator. Tắt: `config set learning.agent_memory_tool false` (khi bật, mọi run thường có hub-self kể cả agent chưa có skill).
Test: `tests/test_agent_memory_tool.py`.
Thu hẹp khoảng cách với Hermes (`docs/dev-notes/memory-hermes-gaps.md`): review chạy trên DIGEST khi `review_model` khác model nguồn
hoặc session > `learning.review_digest_over_tokens` (`ReviewScheduler.digest_for`); quét memory lúc nạp (`prompt_blocks._safe`,
`threats.scan_load`); guard `format_drift` + `entry_too_long` (`learning.limits.memory_entry_chars`); `MEMORY_GUIDANCE` trong prompt;
rollback memory tầng agent THEO MỤC (`ledger.invert_memory`, không chuỗi). Test: `tests/test_memory_hermes_gaps.py`.
Plan ngang Hermes còn lại: `docs/dev-notes/plan-memory-hermes-parity.md`. Tool `memory` không giới hạn số lần
(`rate.agent_tool_max_per_day`, 0 = không giới hạn, không ăn suất review) và ghi được tầng team/global theo `LearningService.memory_scopes`
(lead → team mình lead; điều phối viên / policy `agents.<id>.shared_memory_write` → mọi tầng) — test `tests/test_memory_tool_parity.py`. `session_search` giữ kết quả tool 2.000 ký tự
(`claude_adapter.RESULT_PREVIEW_MAX` = `session_search.TOOL_RESULT_MAX`) và `group` gộp hit theo luồng — mặc định bật cho hub-self + MCP,
tắt cho RPC `session.search`/CLI (`--group`). Test: `tests/test_session_search.py`.

## Graph memory — kho thực thể + fact có thời gian (kiểu Graphiti, SQLite)
Plan (đối chiếu Hermes, chia PR store → retrieval ∥ write → quality, thay PR 3/4 của `plan-memory-hermes-parity.md`): `docs/dev-notes/plan-graph-memory.md`.
Đã làm (store + retrieval + write + quality, nhánh `feat/graph-memory`): `docs/dev-notes/graph-memory.md`. Migration v20 (SQL) = `mem_fact_uses`. Migration **v19** (Python `knowledge.graph.schema.migrate_v19` — migration Python mới: thêm
tiền tố vào `store.PY_MIGRATION_PREFIXES`). Test: `tests/test_graph_memory.py`, `tests/test_graph_memory_write.py` (bộ trích xuất GIẢ), `tests/test_graph_retrieval.py`, `tests/test_graph_quality.py`, `tests/test_graph_memory_mirror.py`, `tests/test_graph_memory_review.py` (sửa theo review: pin graph qua RPC/CLI, quyền tầng
`learn.propose` của agent, fact người khai không bị bên khác đóng, trần độ dài tên thực thể, trần trích xuất nguyên tử).

| Module | Vai trò |
|---|---|
| `knowledge/graph/` | `GraphStore` (đọc, `plan_ops` THUẦN, hàng cho ledger, bản chụp eval, purge/clone/export, vector), `text.py` (NFC + gấp dấu), `schema.py` (bảng `mem_*`, FTS5) |
| `learning/ledger.py` | `Change.kind="graph"` (`graph_rows`, `graph_key=graph://<scope>/<owner>`), rollback THEO HÀNG (`retracted_by`, không xoá cứng) |
| `learning/pipeline.py`, `guard.py` | target `graph`: threat scan, `fact_too_long`, `duplicate_fact`, pin `graph`/`graph_fact`, suất `rate.graph_writes_per_agent_per_day` (riêng, không trần tổng) |
| `knowledge/graph/retrieval.py` | (PR retrieval) `Retriever.search`: FTS5 + vector + thực thể + mở rộng 1–2 bước → RRF → liên quan × độ tin cậy × độ mới; `format_recall` (khối `<recalled-memory>` dạng DỮ LIỆU, có ngày hiệu lực, qua `scan_load` + redact); `snapshot_conn` cho run eval |
| `knowledge/graph/embed.py` | Embedder bge-m3 (fastembed/ONNX, extra `cfharness[vector]`, import lười), sqlite-vec `load_vec`, backfill nền `run_vector_loop` (`Hub.start`). Không cài → FTS5 thuần |
| `knowledge/graph/provider.py` | `MemoryProvider` kiểu Hermes + `GraphMemoryProvider` (`provider_for(hub)`): prefetch vào TIN USER ĐẦU (`RunManager._memory_prefetch` → `_prepare_spec(recall=)`), tool hub-self `memory_recall` (RPC `memory.recall[_self]`) |
| `learning/service.py` | `graph_write(...)`: quyền tầng = tool `memory` (`memory_scopes`); tool `memory` `target='graph'` (`_agent_graph`: `facts`/`forget`, provenance = lượt người gần nhất) |
| `learning/graph_extract.py` | `GraphExtractor` (`hub.learning.graph_extract`): MỘT oneshot JSON-schema / kết thúc run làm việc, hook `PreCompact`, xoay session điều phối viên, review job khi `review_scope=graph`; con trỏ theo session (`hub_state learning.graph_cursor`); chỉ `user`/`steer`/`assistant` (+ `user`/`manager` của điều phối viên), bằng chứng bắt buộc; tầng LLM chọn hạ về `agent` khi không có quyền; trần `graph_extract_*`; dòng `runs` kind=`graph_extract` |
| `knowledge/graph/resolve.py` | gộp thực thể khi ghi: canon → alias → `set_embed_matcher` (cắm `embed.entity_matcher` trong `start_vector_loop`: vector TÊN thực thể, cùng tầng, chỉ khi model `ready`) |
| `learning/graph_mirror.py` | `MemoryMirror` (`hub.learning.graph_mirror`, `on_memory_write`): mục MEMORY.md/USER.md/team memory.md mới → fact graph cùng tầng (một oneshot, chung trần `graph_extract_*`, `created_by=agent`); mục mất → `invalidate_fact` (tất định); trigger `Pipeline._apply` + `LearningOps.tick`; prefetch bỏ fact mirror (`recall.prefetch_mirrored`) |
| `learning/graph_quality.py` | `GraphQuality` (`hub.learning.graph_quality`): `record_uses` (bảng `mem_fact_uses`), `settle` (độ tin cậy theo KẾT QUẢ run: run xấu = thay đổi do run tạo bị tự rollback / trong cửa sổ hồi quy đã rollback / có correction → giảm; run completed sau `settle_h` → tăng), `consolidate` (gộp thực thể + fact gần trùng qua `merge_entity`/`supersede_fact`, `created_by=curator`), `stats`/`fact_history`; `tick` từ `LearningOps.tick` |
| `evals/memory_golden.py` | checker `memory_recall` (golden memory: recall@k × (1 − stale-fact-rate), thăm dò đầu độc → 0) chấm trên bản chụp graph, KHÔNG chạy run; eval gate `_tasks_for` (entry toàn graph → task memory); bộ mẫu `fixtures/graph_memory_golden.json` + `seed_fixture` |
| `learning/cli.py` `learn graph` | `stats\|search\|show\|rollback\|consolidate\|settle` — client mỏng; RPC `learn.graph_*` là `HUMAN_ONLY` |

Bất biến: graph CHỈ ghi qua `pipeline.propose(target="graph")` / `LearningService.graph_write` → `Ledger.apply` (không INSERT trực tiếp vào `mem_*`
ngoài `GraphStore.write_rows` do Ledger gọi; ngoại lệ: clone/import agent mới, purge). Mọi hàng có `scope` + `owner_id`; đọc LUÔN qua
`GraphStore.visible`/`hidden=versions.graph_hidden(agent)` (ghim canary). Output tool/web không bao giờ ghi thẳng vào graph; văn bản episode
không bao giờ chèn nguyên văn vào ngữ cảnh. Eval: graph vào bản sao home qua `.graph_snapshot.json` (băm trong fingerprint), không đọc DB sống.
Thêm cột vào `mem_*` → migration mới; cột thay đổi ngoài ledger → thêm vào `graph.store.VOLATILE`.
Đọc (PR retrieval, `docs/dev-notes/graph-memory.md` § PR 2): recall KHÔNG gọi LLM, KHÔNG vào system prompt (chỉ tin user đầu của session mới,
không cho run review/eval/curator/resume/fork); không bao giờ chờ nạp model trên đường chạy run (`Embedder.ready`); `mem_vectors` là chỉ mục
dẫn xuất (vòng nền ghi thẳng, ngoài ledger). Test KHÔNG tải model: `tests/conftest.py` đặt `CFHARNESS_GRAPH_VECTOR=off`, test cần vector gán
`hub.graph_embedder = <giả>`. `memory_search` đọc memory/skill THEO PHIÊN BẢN agent (ghim canary), không đọc file sống. Test: `tests/test_graph_retrieval.py`.
Thêm cột vào `mem_*` → migration mới; cột thay đổi ngoài ledger → thêm vào `graph.store.VOLATILE`. Trích xuất KHÔNG BAO GIỜ đọc
`tool_call`/`tool_result`/tin subagent/`tick`/`notify` (lọc ở SQL `GraphExtractor.collect`) — thêm vai trò mới vào chỉ mục session thì quyết định rõ
có cho trích hay không. Run kind mới không phải "làm việc" → thêm vào `review.NON_WORK_KINDS`, `session_search.TECHNICAL_KINDS`, `kpi.LEARNING_KINDS`
(và `graph.provider.TECHNICAL_KINDS` để không prefetch).
Quality (PR quality): độ tin cậy CHỈ đổi theo KẾT QUẢ run (`graph_quality.settle`), không theo số lần dùng (`hits` chỉ để quan sát); suy giảm theo
tuổi chỉ giảm HẠNG lúc đọc, không bao giờ xoá. `confidence` + `mem_fact_uses` ghi ngoài ledger (dẫn xuất, như `mem_vectors`). Consolidation chỉ
ĐÓNG hiệu lực + liên kết thay thế (`invalidated_by`) qua pipeline (`created_by=curator`, không ăn suất agent), không bao giờ xoá episode, bỏ qua fact
người pin. Golden memory (`checker.kind=memory_recall`) không chạy run/không gọi model; chỉ dùng cho lô entry graph.

## Cập nhật cfharness — nút "Cập nhật hệ thống" (web Cài đặt) + `cfharness update`
Chi tiết: `docs/dev-notes/update.md`. Không migration. Test: `tests/test_update.py` (không gọi mạng/uv thật).

| Module | Vai trò |
|---|---|
| `updater.py` | `hub.updater`: bản mới nhất (GitHub Releases, cache 10 phút), kiểu cài (`uv-receipt.toml`: chỉ `uv_git` cập nhật được), `start` → chép helper, spawn, daemon tự dừng; RPC `update.check|status|start` |
| `update_helper.py` | Tiến trình phụ CHỈ stdlib, chạy bằng python gốc ngoài venv (`-I`): chờ daemon thoát → `uv tool install --force … @<tag>` → `python -m cfharness daemon start` → `HUB_HOME/update/state.json`, `logs/update.log` |
| `update_cli.py`, `web/update_pages.py` | `cfharness update [--check] [--yes] [--force] [--tag]`; thẻ đầu Cài đặt (`/partials/update`, poll khi đang cập nhật) + trang `/update`, thao tác `update_start` (xác nhận) |

Bất biến: `update.start` ∈ HUMAN_ONLY (không ở MANAGER_ALLOWED), `cfharness update` ∈ `HUB_HUMAN_ONLY_CLI`; tag chỉ dạng `vX.Y.Z`, repo cố định
(`updater.REPO`); có run chạy/chờ → từ chối trừ `force`. Helper không được import `cfharness` (venv bị cài đè trong lúc chạy).

## Sao lưu / khôi phục HUB_HOME + cập nhật có lưới an toàn — `backup.py`
Chi tiết, danh sách gồm/bỏ, giới hạn: `docs/dev-notes/backup.md`. Không migration. Test: `tests/test_backup.py`, `tests/test_sec_auth.py::test_backup_*`.
- `backup.py`: archive `HUB_HOME/backups/<id>.tar.gz` (0600, thư mục 0700) + `<id>.json`; hub.db bằng SQLite online backup API (kết nối chỉ đọc
  riêng), KHÔNG chép file thô; bỏ `secrets/`, `auth/`, `mcp_server/` (không mã hoá). `hub.backups` (vòng nền `backup.schedule`, mặc định
  `daily 03:30`, giữ `backup.keep`), RPC `backup.list|create|verify|restore_plan`, CLI `backup create|list|verify|restore` (`backup_cli.py`),
  thẻ "Sao lưu" ở Cài đặt (`web/backup_pages.py`, ACTIONS `backup_create`).
- Bất biến: khôi phục KHÔNG có RPC — chỉ CLI của người, daemon dừng (giữ `daemon.lock`), xác nhận 2 bước, chụp `pre-restore` trước, ghi lại
  `integrity_baseline` sau (`rebaseline_offline`). `backup.create|verify|restore_plan`, `update.rollback` ∈ HUMAN_ONLY; `backup` ∈ HUB_HUMAN_ONLY_CLI;
  sandbox denyRead/denyWrite `backups/`. Thêm thư mục cấp 1 mới vào HUB_HOME → quyết định rõ thuộc `INCLUDE_DIRS` hay `EXCLUDED`.
- Cập nhật: `update.start` tạo bản `pre-update` trước khi chạy helper; helper (chỉ stdlib) dò `hub.ping`, không lên → cài lại bản cũ + khôi phục hub.db
  khi `user_version` đã tăng; `cfharness update --rollback` (RPC `update.rollback`). Pha mới của helper mà daemon phải chờ → `daemon._UPDATE_BUSY_PHASES`.

## Xuất / nhập toàn bộ hub sang máy khác — `hub_transfer.py` (PR1–PR6 của `docs/dev-notes/plan-hub-clone.md`)
Tài liệu tổng hợp (người dùng + kỹ thuật + giới hạn): `docs/dev-notes/hub-clone.md`. Không migration. Test: `tests/test_hub_transfer.py`,
`tests/test_hub_transfer_secrets.py`, `tests/test_hub_transfer_sessions.py`, `tests/test_hub_transfer_cross_os.py`, `tests/test_hub_transfer_move.py`,
`tests/test_web_hub_transfer.py`, `tests/test_sec_auth.py::test_hub_transfer_web_rpcs_human_only`.
- `hub_transfer.py`: gói `.cfhx` (tar không nén ngoài, từng phần tử gzip riêng, `MANIFEST.json` cuối, sha256 nội dung gốc, `sections` có `prefix`
  — phần mới thêm sau là mục `sections` mới, bản cũ bỏ qua phần `optional`). Xuất = RPC `hub.export` (HUMAN_ONLY, tự audit; làm lặng bằng
  `RunManager.quiesced` rồi thả trong `finally`) hoặc ngoại tuyến (giữ `daemon.lock`); chỉ ghi vào `HUB_HOME/exports/`. Nhập CHỈ ngoại tuyến từ CLI
  (`hub import`, không RPC): kiểm gói → stage `HUB_HOME/.import-*` → `pathmap` trên stage → dọn `MACHINE_STATE` → áp chế độ (`clone` mặc định)
  → chuyển vào chỗ (`backup._apply_stage`) → `rebaseline_offline`. `hub_transfer_cli.py`: `cfharness hub export|import` (∈ `HUB_HUMAN_ONLY_CLI`).
- `pathmap.py`: viết lại đường dẫn nhiều cặp trong MỘT lượt (dùng chung với `home_migrate`). Bảng append-only/`HISTORY_COLS` giữ nguyên; tri thức
  thuộc ledger (file MEMORY/SKILL/config agent, bảng `mem_*`) KHÔNG viết lại tay — chỉ ghi `hub_state.import_followups`.
- PR2 `transfer_secrets.py`: phần `secrets.enc` = scrypt(n=2^17,r=8,p=1) → AES-256-GCM, AAD = id gói + sha256 manifest (bỏ đúng mục của chính
  `secrets.enc`) → tráo bất kỳ phần nào khác = giải mã thất bại. Bản rõ chỉ theo danh sách trắng `allowed_payload_path` (`secrets/<NAME>`,
  `.meta.json`, `auth/totp.json`, `mcp_server/tokens.json`) — KHÔNG BAO GIỜ `auth/web.json`, thiết bị tin cậy, `<socket>.auth`. Passphrase CHỈ
  trong tiến trình CLI của người (getpass / `--passphrase-file` 0600; parser `allow_abbrev=False`): daemon đóng gói phần không mật, CLI gắn
  `secrets.enc` (`seal_package`) — passphrase không qua RPC/audit/log/DB (`wrap_with_audit` vẫn gỡ khoá `passphrase` phòng hờ). Nhập: giải mã
  ngay sau kiểm gói (sai → từ chối, chưa ghi gì), ghi secret SAU CÙNG qua `SecretStore.set_bytes` (0600, đăng ký bộ che) có hoàn tác. `clone`
  bỏ `TELEGRAM_*`/`SLACK_*`/`MCP_OAUTH_*` (`transfer_secrets.CLONE_SKIP_PREFIXES`), giữ token MCP tĩnh + `CLAUDE_ACCT_*`/`CLAUDE_CODE_OAUTH_TOKEN`;
  thêm loại secret mới của hub → phân loại ở đó + ca trong test. `clone` còn huỷ approval/câu hỏi đang chờ (`cancelled`, KHÔNG `expired`) +
  run `waiting_human` → `interrupted`. Nhập lỗi sau khi đã ghi → `_rollback_import` (bản `pre-restore` / home trống). Vòng nền ghi tri thức/DB
  kiểm `runs.export_quiesced(hub)` đầu mỗi lượt — thêm vòng nền ghi mới → thêm dòng kiểm + vào danh sách trong test.
- PR3 `transfer_sessions.py`: phần `claude/` (optional) = session Claude Code của hub (`core`: hội thoại điều phối viên chưa lưu trữ + task chưa
  xong + run dở/`pending_resume`; `full`: mọi session_id trong `sessions`/`runs`/`manager_conversations`) — chỉ tìm đúng `<sid>.jsonl` hub biết
  dưới `<claude home>/projects/*/` (+ cây `<sid>/`), không theo symlink, trần cỡ, che secret THEO GIÁ TRỊ lúc xuất. Claude home =
  `CLAUDE_CONFIG_DIR` hoặc `~/.claude` (chỉ đọc/ghi file — không bao giờ đặt biến này cho tiến trình `claude`); test dùng
  `CFHARNESS_TEST_CLAUDE_HOME` (conftest trỏ thư mục tạm — test KHÔNG đụng `~/.claude` thật). Nhập: `encode_cwd(pathmap(cwd))` (quy tắc
  Claude Code: đơn vị UTF-16 ngoài `[A-Za-z0-9]` → `-`, > 200 → cắt + `-`base36(hash Java); đổi quy tắc → sửa `encode_cwd` + fixture tên
  thật trong test), viết lại từng dòng JSON, `O_EXCL` 0600 / thư mục mới 0700, sid trùng → bỏ; ghi SAU hub.db, TRƯỚC secret, có hoàn tác.
- PR4 khác OS (§5b): `pathmap.PathMap` biết CẢ HAI kiểu bất kể OS đang chạy (kiểu suy từ chuỗi: `path_style`; KHÔNG dùng `IS_WINDOWS`);
  cặp Windows khớp dạng thô/JSON-escape/`C:/…`, ổ đĩa không phân biệt hoa/thường; khác kiểu → sau tiền tố đổi dấu phân cách CHỈ ở phần đuôi
  của token đường dẫn (`_tail_end`: dừng ở khoảng trắng — trừ đoạn tương đối kiểu `Application Support/x` —, nháy, `<>|,;:&`, `\` lạ,
  ngoặc đóng lẻ); POSIX → Windows trong JSON (giá trị DB là JSON hợp lệ, file `.json`) / nháy kép YAML → dạng `\\`. POSIX → POSIX giữ
  đúng hành vi cũ (`home migrate`). Kiểm tuyệt đối đa kiểu: `pathmap.is_abs` (không `os.path.isabs`). `transfer_os.py`: mọi hàm nhận
  `target_os` làm THAM SỐ (test mô phỏng Windows trên macOS); `plan_names` kiểm tên phần tử TRƯỚC khi giải nén (Windows: `<>:"|?*`, điều
  khiển, CON/NUL/COM1…, đuôi `.`/khoảng trắng; ổ không phân biệt hoa/thường — `fs_case_insensitive` dò không ghi): tri thức (bên trong
  `skills/`, `.archive/`, mục `trash/`) → đổi tên tất định trong STAGE (thư mục skill giữ `NAME_RE`: `con` → `con-skill`; `skills.name|path`,
  frontmatter; KHÔNG qua ledger) + `import_followups.renamed`; `claude/` → bỏ file; còn lại → từ chối cả gói trước khi ghi. MCP stdio thiếu
  lệnh (mọi lần nhập) → `probe_error` "[hub] …" (không có cột bật/tắt). Thêm vùng tri thức đổi tên được → `transfer_os._kind` + ca test.
- PR5 chuyển nhà `hub_freeze.py`: `hub export --for move` → sau khi gói qua kiểm tra, `hub_state.migrated_to {at, export_id, sha256, sealed…}`
  (trong daemon: NGAY trong cửa sổ làm lặng; ngoại tuyến: trước khi nhả `daemon.lock`; CLI gắn `secrets.enc` xong → `hub.freeze` cập nhật sha256
  cùng export_id, gắn hỏng → tự gỡ). Đóng băng = `RunManager.frozen`: `submit` từ chối MỌI kind (`Blocked` + `data.migrated_to`), `tick` không khởi
  chạy hàng đợi, `recovery.resume_interrupted` không resume, `runs.export_quiesced` = đang xuất HOẶC đóng băng (mọi vòng nền ghi bỏ lượt) — RIÊNG
  sao lưu dùng `runs.exporting` (vẫn chạy khi đóng băng). Telegram/Slack: `token()` trả None khi đóng băng (trừ `ignore_enabled` của người),
  `QueuedNotifier` bỏ mọi tin; freeze/unfreeze `kick()` vòng nhận. Banner: `hub.status.migrated_to`, `tui.snapshot.hub.migrated_to`, web
  `base.html` (RPC đọc `hub.frozen`). Gỡ CHỈ người: `hub.unfreeze` (HUMAN_ONLY, tự audit) ← `cfharness hub unfreeze` (y/N). Event `hub.frozen|unfrozen`.
  Thêm đường tạo run/kênh gửi ra ngoài mới → phải đi qua `RunManager.submit` / `token()` / `QueuedNotifier` (hoặc tự kiểm `hub_freeze.frozen`).
- PR5 `transfer_repos.py`: phần `repos.json` (optional) — repo workspace NGOÀI HUB_HOME (agent `workspace_default`, team `policies.workspace`, task
  mở, `cwd` run chưa xong): gốc git, remote ĐÃ CHE (`mask_remote`: bỏ userinfo URL, token query, che theo giá trị secret), nhánh, HEAD, dirty;
  git qua `GIT_SAFE_ARGS`/`GIT_ENV`. KHÔNG BAO GIỜ đóng gói repo. Nhập: `plan` (thiếu → lệnh `git clone`, dirty → cảnh báo), `pause_missing` trên
  stage DB (agent thiếu workspace → `paused` + `hub_state.import_paused`; `agents.set_paused(False)` gỡ lý do), `--clone-repos` SAU khi hub.db vào
  chỗ (không bao giờ vào thư mục đã có, lỗi không hoàn tác lần nhập) → `unpause_present`. `hub_state.import_repos` cho doctor.
- PR5 `import_followups.py` (gọi ở `Hub.start`): `hub_state.import_followups` → viết lại đường dẫn bằng `map` của lần nhập qua `Ledger.apply` actor
  `ledger.IMPORT_ACTOR="import"` — memory (file, rollback theo mục), skill (tree), SOUL (config), graph fact/thực thể SỐNG (graph, sửa hàng tại
  chỗ, rollback theo hàng); config.yaml agent dựng lại từ DB; episode/fact đã đóng/`.archive/` = lịch sử, giữ nguyên. `import` KHÔNG thuộc
  `AUTO_CHANGE_ACTORS` (do người khởi xướng → không bao giờ bị tự rollback). Idempotent, lỗi từng mục → `status=partial` + `retry` (thử lại lần
  daemon lên sau), audit `hub.import_followups`.
- PR5 `hub_doctor.py`: `cfharness hub doctor [--probe] [--json]` + RPC `hub.doctor` (HUMAN_ONLY, chỉ đọc, không probe mạng) — đọc DB chỉ đọc,
  config, TÊN secret (không bao giờ giá trị); không gọi `claude` ngoài `--version` (và `auth status` khi `--probe`). Tự chạy sau `hub import`.
  `HUB_HUMAN_ONLY_CLI["hub"] = None` (mọi lệnh con `hub …` chặn với agent).
- PR6 web — `hub_transfer_web.py` (daemon) + `web/transfer_pages.py` (`/hub-transfer`, thẻ ở Cài đặt, thẻ "Nhập hub từ máy khác" ở trang chủ khi
  `hub_fresh`) + `static/transfer.js`. RPC `hub.transfer_status|export_start|export_status|import_plan|import_start|import_status` (∈ HUMAN_ONLY,
  `HUMAN_ONLY_TRANSFER_WEB`; ghi = `MUTATING_TRANSFER_WEB`, tự audit, `passphrase` bị pop trước khi chạy). Xuất NỀN trong daemon (`ExportJobs`:
  `export_in_daemon` → `seal_package` TRONG daemon → freeze/unfreeze như CLI). Passphrase web CHỈ ở bước 2 (`Action.reenter` — ô password trống;
  `Action.secret_fields` lọc cả trang bước 1 và bước 2 khỏi `carry()`). Tải lên = XHR thân thô (`origin_guard` + CSRF/khoá qua header, Content-Length
  ≤ `transfer.max_upload_mb` trước khi đọc, `.part` 0600 → rename, lỗi → xoá); tải về = token một lần trong RAM web (gắn `Session.sid`, 10 phút)
  → stream `exports/<id>.cfhx` (`ID_RE`, `O_NOFOLLOW`) — hai chỗ DUY NHẤT web đụng file của hub (gói, không bao giờ hub.db). Nhập: `hub.import_start`
  kiểm nhanh (id gõ lại, passphrase, hub trống|replace, run|stop) → `imports/<id>/job.json` (không passphrase) + `pass` 0600 → spawn
  `python -m cfharness hub import-job <id>` → daemon tự dừng; job xoá `pass` ngay khi đọc, chờ `daemon.lock`, gọi `import_package`, LUÔN khởi
  động lại daemon, ghi `state.json` (danh sách trắng `STATE_KEYS`/`REPORT_KEYS`). `daemon.run_foreground` → `hold_while_importing` (job sống →
  daemon mới chờ); `register_rpc` → `cleanup` (`pass` sót, job chết → failed, gói tải lên > 24 h). Web không đọc `state.json` trực tiếp (như
  `update_pages`: daemon dừng → "đang dừng", poll). Thêm RPC web → `HUMAN_ONLY_TRANSFER_WEB` (+ `MUTATING_…`) + ca trong 2 file test trên; khoá
  mới của state/kết quả → danh sách trắng; pha job mà daemon phải chờ → `IMPORT_BUSY_PHASES`.
- filehub (Không gian số): `secrets/filehub/` + `filehub/` KHÔNG BAO GIỜ vào gói (cả move); khoá cổng `FILEHUB_LANE_*`: clone bỏ, move giữ. Nhập
  (`_adjust_filehub`) → bỏ khoá cấu hình của đồng bộ cũ (`SYNC_ONLY_KEYS`), clone gỡ `folders`, move giữ (thư mục đồng bộ cũ = `mode: sync` "cần chuyển",
  đã pathmap); `hub_state.import_filehub` (∈ MACHINE_STATE, xoá khi `lane_setup` xong) → báo cáo/doctor nhắc `cfharness filehub lane setup`.
- Bất biến: gói không bao giờ chứa `checkpoints/`, `auth/web.json`/thiết bị, `secrets/` dạng rõ, session Claude không thuộc hub; passphrase không bao giờ vào
  argv/env/audit/log/DB/`job.json`/`state.json`/HTML; thêm trạng thái gắn với máy vào `hub_state` →
  thêm khoá vào `hub_transfer.MACHINE_STATE` (hoặc `CLONE_STATE`) + ca trong test. `exports/`/`imports/` ∈ `backup.EXCLUDED`, sandbox
  denyRead/denyWrite, `sensitive_read`.

## Tổng quan máy (Q6) + quản trị user trên cùng máy — mỗi hub xuất số liệu, admin xem/cảnh báo/ESTOP/chính sách
Chi tiết: `docs/dev-notes/admin-overview.md` (Q6 + RUNBOOK nhóm/thư mục), `docs/dev-notes/admin-users.md` (plan-admin-users P1+P2: bất biến,
lệch kế hoạch, việc dở). Migration **v26** (`admin_samples`). Test: `tests/test_admin_overview.py`, `tests/test_admin_{control,policy,watch,users,audit}.py`,
`tests/test_web_admin.py`.

| Module | Vai trò |
|---|---|
| `admin_overview.py` | `hub.admin_export` (tắt mặc định, `admin_export.*` ∈ SECURITY_KEYS; `require.export` của chính sách ép bật) ghi nguyên tử `status/<u>/<hub_id>.json` (thư mục root cấp; chưa có → file phẳng cũ `<u>.json`) 0640; SPEC **schema 2** (bộ đọc nhận 1 và 2, cả hai bố cục); RPC `admin.overview` |
| `admin_control.py` | Nhóm `cfharness-admin` (quản trị) / `cfharness-users` (người dùng), `is_admin()`; ESTOP admin = file cờ `control/estop[.<u>]` (có mặt = bật kể cả hỏng; chỉ `reason_code` enum), `hub.admin_control` kiểm 5 s + trước submit, `RunManager.estop` gộp (user `estop off` không gỡ được), event `hub.admin_estop`; admin-log |
| `admin_policy.py` | `policy/{_machine,_default,<u>}.json` root-owned → lấy giá trị CHẶT HƠN (scope ngân sách `admin`, trần mỗi run, đồng thời, `require.*`, `min_version`, `models.allow`); hỏng = fail-open + cảnh báo, fail-closed khi `_machine.json` có `policy_required` |
| `admin_watch.py` | `hub.admin_watch` (chỉ daemon thuộc `cfharness-admin`): event `admin.alert{user, hub_id, code, state on|resolved}` (chống trùng, báo hồi phục, qua notifier Telegram/Slack sẵn có) + lịch sử `admin_samples` (90 ngày) |
| `admin_users.py`, `admin_audit.py`, `admin_rpc.py`, `admin_cli.py` | `admin users list|show|add|revoke` (add/revoke mặc định in lệnh, `--write` cần root), `setup check`; audit METADATA `audit/<u>/<ngày>.jsonl` (30 ngày); RPC `admin.*`; CLI `cfharness admin …` |
| `web/admin_pages.py` | `/admin/overview|users|users/<u>|alerts|audit` (admin; biểu đồ SVG trong template) + `/machine` "Quản trị máy" (phía user, chỉ đọc); ghi duy nhất = ESTOP admin (`admin_estop_on|off`, xác nhận) |

Bất biến: nội dung file/dòng/cờ/cảnh báo CHỈ theo danh sách trắng (`SPEC`, `AUDIT_SPEC`, `ADMIN_LOG_SPEC`, `POLICY_SPEC`, `REASON_CODES`,
`ALERT_CODES`) — không bao giờ chữ tự do (hai chiều); thêm trường → spec + ca trong test. Người đọc coi file là dữ liệu không tin cậy
(`O_NOFOLLOW`, trần cỡ, lọc lại, kiểm chủ file; chính sách phải chủ uid 0 + không ghi được bởi nhóm/khác). Đường dẫn `control/`, `policy/`,
`admin-log/` là HẰNG SỐ (`admin_control.SHARED_ROOT`; chỉ test đổi qua `CFHARNESS_TEST_SHARED_ROOT`). Mọi RPC `admin.*` ∈ HUMAN_ONLY, không ở
MANAGER_ALLOWED (phía admin còn `require_admin`); `cfharness admin` ∈ `HUB_HUMAN_ONLY_CLI`; `admin_watch.*` ∈ SECURITY_KEYS. Agent không đọc/ghi
`/Users/Shared/cfharness`: sandbox + `permissions.deny` Read/Grep/Glob/Edit/Write (mọi hub, kể cả trust_full) + classifier `admin_shared` (forbidden).
Thay đổi `runs.py` gói trong `_admin_gate`/`_admin_policy()` (một dòng gọi ở mỗi chỗ). Test giả nhóm bằng fixture `as_admin` (mặc định không thuộc nhóm).

## Giam agent trong workspace W — `workspace.confine` (P2 đa instance)
Chi tiết, spike (claude 2.1.319: `allowRead` trong `denyRead` hoạt động, glob không mở lại), giới hạn: `docs/dev-notes/confine.md`,
`docs/dev-notes/hardening-SEC.md` §1.1. Không migration. Test: `tests/test_confine.py`, ca `DESTRUCTIVE_CONFINE` trong `tests/test_guard_classifier.py`.
- `confine.py`: đọc cấu hình, `read_roots`/`deny_read_roots`, `apply_sandbox` (denyRead `$HOME` + allowRead W/toolchain/workspace hub),
  `confine_only_sandbox` (trust_full / sandbox tắt), `permission_deny_rules` (anh em của W), registry hub `~/.config/cfharness/hubs.json`
  (env `CFHARNESS_HUBS_REGISTRY`; conftest đặt về thư mục tạm).
- Bất biến: `workspace.confine` chỉ người (`config.set` HUMAN_ONLY, khoá ∈ `integrity.SECURITY_KEYS` — khoá con có dấu chấm được hỗ trợ);
  khi bật, `trust_full` KHÔNG gỡ giam (Q4) và `guard.sandbox.enabled=false` vẫn để lại sandbox chỉ-giam; mọi workspace/cwd/worktree/
  `extra_write_roots` phải trong W (`validate_workspace`, `check_path`). Không dùng glob trong `sandbox.filesystem.allowRead` (không mở lại).

## Cổng cfharness — thư mục chung Không gian số không qua đồng bộ (`filehub.mode` sync|lane, `filehub/lane/`)
Plan + hợp đồng §C (chép NGUYÊN VĂN ở `quan-ly-thu-muc-ai/docs/PLAN-cong-cfharness.md`), đã làm, lệch, việc dở: `docs/dev-notes/plan-filehub-lane.md`.
Không migration (trạng thái ở `hub_state filehub.lane` ∈ `hub_transfer.MACHINE_STATE`, cây/trạng thái ở `HUB_HOME/filehub/lane*`). Mặc định `sync`.

| Module | Vai trò |
|---|---|
| `filehub/lane/client.py` | `LaneClient` §C (HTTP+SSE, `LaneError` theo C.7, `norm_rel` C.4 phía hub, `check_url` chỉ loopback) |
| `filehub/lane/service.py` | `hub.filehub.lane`: khoá (secret `FILEHUB_LANE_<TÊN>`), trạng thái, luồng SSE + cursor, `prepare_run`/`flush_run`/`end_run`, presence, `switch`, mồ côi, bảo trì |
| `filehub/lane/workdir.py` | Cây theo run `<agent home>/workspaces/lane-<tree>`: dựng, kéo + steer, quét/đẩy, xung đột, chặn secret, flush |
| `filehub/lane/tools.py` | `fsself.call` (hub-self `fs_*`, cờ `--fs ro|rw`) — ràng run + phạm vi + chỉ đọc |

Bất biến: khoá cổng KHÔNG BAO GIỜ trong env/argv/mcp.json của run, DB, config.yaml, audit (`wrap_with_audit` bỏ `key`), log, event, gói
`clone` (`transfer_secrets.CLONE_SKIP_PREFIXES`). Agent không bao giờ nói chuyện với cổng (sandbox chặn TCP localhost + classifier `filehub_lane`
∈ `TRUST_NEVER_RULES`); mọi truy cập qua daemon. Quét giá trị secret TRƯỚC khi đẩy (không một byte nào rời máy). Không bao giờ ghi đè bản của
người (`If-Match`; 409 → bản xung đột riêng). Hub không xoá bản sao `sync` cũ khi chuyển. Ghi chế độ/khoá/mồ côi = HUMAN_ONLY (`HUMAN_ONLY_LANE`),
không ∈ MANAGER_ALLOWED; CLI `filehub lane|switch` ∈ `HUB_HUMAN_ONLY_CLI`. Đổi hợp đồng → sửa §C ở CẢ HAI repo + `tests/fake_kgs_lane.py`.
Steer/nhắn agent = câu cố định tiếng Anh, đường dẫn đã `one_line`. Test: `tests/test_lane_{client,service,workdir,switch}.py` (server giả, ScriptedRuntime).

## Giọng nói trên web — đọc chép + hội thoại giọng nói với điều phối viên
Chi tiết, an toàn, việc dở: `docs/dev-notes/voice.md`. Không migration. Test: `tests/test_voice.py`.
- `web/static/voice.js` (Web Speech API, `vi-VN` mặc định): nút micro cho mọi `textarea[data-voice]` + `#mgr-mic`; nút sóng âm `#mgr-voice` = chế độ
  hội thoại (tự gửi khi im, đọc to trả lời). Chỉ nghe sự kiện `mgr:*` do `app.js initManager` phát; chữ agent chỉ qua `textContent`/`speechSynthesis`.
- Bất biến: tin giọng nói mang `voice=dictation|conversation` → `manager.chat{voice}` → `_owner_shortcut` KHÔNG BAO GIỜ duyệt approval/chốt câu hỏi
  xin phép từ giọng nói (`_voice_shortcut`). Header `microphone=(self)` — không nới thêm (camera/geolocation vẫn `()`).

## Desktop app — hỗ trợ phía hub
Hợp đồng chung hub ↔ app (C1–C7), việc đã làm/còn lại, test: `docs/dev-notes/desktop-app.md`. Không migration. Test: `tests/test_desktop_hub.py`
(+ `tests/js/voice_native_harness.js` chạy `voice.js` bằng node với DOM giả), `tests/test_desktop_directory.py` (C5 + C6).

| Module | Vai trò |
|---|---|
| `web/desktop.py` | `GET /.well-known/cfharness.json` (công khai, CHỈ `{kind, schema, app_api}`), `GET /app/badge` (cần phiên, `badge_from` thuần: chỉ int/bool/phiên bản), `note_app_ua` (UA `CfharnessDesktop/<ver>` → chỉ log), chọn cổng theo hub, spawn web tách tiến trình, khoá `auth/web.ensure.lock` + nonce `CFHARNESS_WEB_NONCE` (nhận diện server vừa bật, không theo pid), `probe`/`wait_ready`/`info` cho C1 |
| `web/cli.py` `cmd_web_app` | `cfharness web --json [--ensure] [--port N] [--wait S]`: một dòng JSON `{ok, origin, login_url, hub_id, version, name, started}` / `{ok:false, error, message}` mã 1; `--ensure` khởi động web nền (`--no-link`, log `HUB_HOME/logs/web.log` 0600). Cấu hình `web.port`, `web.display_name` |
| `web/static/voice.js` | Dò `window.cfhNative.speech` (đủ `available/start/stop/on`, `available()` = true) TRƯỚC Web Speech API; `NativeListener` cùng giao diện `Listener` |
| `admin_directory.py` | C5 directory hub (hub ADMIN): mục `{id, name, user, origin}` lọc từ file trạng thái `admin_overview` (SPEC schema 3: `public_origin` qua `parse_public_origin`, `display_name` một dòng ≤ 60), ≤ 200 mục; token băm SHA-256 ở `HUB_HOME/auth/directory.json` (0600), so `compare_digest`; RPC `admin.directory\|directory_status\|directory_token{rotate}\|directory_serve{token}`; CLI `cfharness admin directory export [--out]\|token [--rotate]\|status`; cấu hình `admin_directory.name` |
| `web/desktop.py` `directory` | `GET /.well-known/cfharness-directory?t=` công khai có token: sai/thiếu/chưa tạo/không phải admin → 404 JSON `{"error":"not_found"}`; sai liên tục → 429 (`directory_limiter`, theo IP + toàn cục); trần lần trả (`SuccessWindow`); lọc lại bằng `admin_directory.document` |
| `desktop_app.py` | C6 thư mục dữ liệu app (`io.cfharness.desktop`: macOS Application Support + WebKit/HTTPStorages/Caches, Windows `%APPDATA%`/`%LOCALAPPDATA%`, Linux XDG) → sandbox denyRead/denyWrite + vùng cố định (`fixed_protected_dirs`), `permissions.deny` (`guard/runtime.app_data_deny_rules`, mọi hub kể cả trust_full), classifier `desktop_app_data` (forbidden, ∈ `NEVER_DOWNGRADE_RULES`) |

Bất biến: JSON của C1 không bao giờ chứa token người hay khoá ký (`login_url` = link một lần 300 s, như `--url`); well-known không thêm trường (không
phiên bản/tên); `/app/` ∈ `GuardMiddleware.API` (GET không gia hạn phiên, chưa đăng nhập → 401 JSON) và không bao giờ trả chữ tự do — thêm trường
badge = số/bool + ca trong test. UA của app không đổi giao diện. Cầu nối giọng nói dùng CHUNG luồng `mgr:*` + `voice=dictation|conversation`; không
nới CSP/Permissions-Policy, không `innerHTML`/`eval`. `cfharness web …` (mọi biến thể) vẫn forbidden với agent. Luồng cũ `cfharness web` (không `--json`/`--ensure`) giữ nguyên.
Directory (C5): mọi RPC `admin.directory*` ∈ HUMAN_ONLY (không ở MANAGER_ALLOWED) + `require_admin`; chỉ `admin.directory_token` là ghi (bọc audit; token/URL
không vào audit — `_result_brief`); token chỉ hiện MỘT lần, file chỉ giữ băm. Danh mục KHÔNG cấp quyền đăng nhập và không bao giờ có trường ngoài `{id, name,
user, origin}` (không chi phí/trạng thái/đường dẫn) — thêm trường = sửa hợp đồng ở cả hai repo + ca trong test. Nhánh `admin_directory` ∈ `integrity.SECURITY_KEYS`;
`cfharness admin …` forbidden với agent. Route trả 404 cho MỌI lỗi (không phân biệt lý do). Thư mục app (C6): thêm biến thể đường dẫn → `desktop_app.app_data_dirs`
+ ca trong `tests/test_desktop_directory.py`/`tests/test_guard_classifier.py`; KHÔNG chặn chuỗi identifier đứng một mình (agent làm việc trên repo app).
Trust full KHÔNG gỡ `desktop_app_data`/`admin_shared`/`other_hub_write` (`guard/service.TRUST_NEVER_RULES` — thêm rule "không phải quyền của hub để trao"
→ thêm vào đây VÀ `classifier.STICKY_FORBIDDEN_RULES` + ca trong `tests/test_trust_and_noise.py`). Cấu hình/đăng nhập Claude Code (`claude_files.py`:
`~/.claude.json*`, `.credentials.json`, `<config>/backups`, `$CLAUDE_CONFIG_DIR`, `HUB_HOME/claude-config/`) = rule `claude_credentials` cùng nhóm: sandbox denyRead,
`runtime.claude_files_deny_rules` (cả trust_full), classifier forbidden; KHÔNG mở rộng sang `~/.claude/projects|settings.json`. Glob kiểm ký tự đại diện ở mọi vị trí (`desktop_app.glob_may_touch`). Bản xuất `admin_overview`
chỉ ghi schema mới khi có trường mới (tương thích admin cũ). `--ensure` luôn giữ khoá ensure khi spawn; `started` theo nonce.

## Thư mục dùng chung với "Không gian số" — `filehub/` (chỉ qua cổng cfharness)
Chi tiết + đường nâng cấp: `docs/dev-notes/filehub.md`; hợp đồng §C + phần đã làm: `docs/dev-notes/plan-filehub-lane.md`. Không migration (cấu hình
`filehub.*` ∈ `integrity.SECURITY_KEYS`, trạng thái ở `HUB_HOME/filehub/` + `hub_state filehub.lane`). Đồng bộ 2 chiều cũ (`tang-thu-sync-headless`,
đăng nhập thiết bị/relay OAuth, bản sao `~/KhongGianSo`) đã gỡ ở H5 — KHÔNG thêm lại.

| Module | Vai trò |
|---|---|
| `filehub/service.py` | `hub.filehub`: `lane_target("filehub:<tên>[/<con>]")`; `resolve_ref` LUÔN ném lỗi có giải thích (qua cổng / đồng bộ cũ cần chuyển / tên sai); `legacy_folders` + `LEGACY_TEXT`; `link`; `remove|rename|resume`; `stop_orphan_sync` (tiến trình sync còn sót, đối soát `(pid, start_time)`) |
| `filehub/lane/` | Cổng: `client.py` (§C, chỉ loopback), `service.py` (`LaneService`: khoá ở secret store, `hello` + `reconcile` — thêm gốc mới, tự chuyển thư mục đồng bộ cũ cùng gốc, đánh dấu thu hồi; cây theo run, presence, mồ côi, lô xoá đang giữ, hoàn tác), `workdir.py`, `tools.py` (`fs_*`) |
| `filehub/rpc.py`, `filehub/cli.py` | RPC `filehub.status|list|link` (đọc) + `remove|rename|resume` + `lane_setup|test|disable|add|orphan` (HUMAN_ONLY, `MUTATING_FILEHUB` tự audit); CLI `cfharness filehub status|remove|rename|resume --root|lane …` (lệnh cũ chỉ in hướng dẫn) |
| `web/filehub_pages.py` | Thẻ "Không gian số — cổng cfharness" (khoá chỉ ở bước 2, thư mục cần chuyển, đổi tên/gỡ, lô xoá đang giữ, cây mồ côi) — mọi thao tác qua `ACTIONS` → RPC `filehub.*`; `/filehub/open` → link theo id do cổng trả |

Bất biến: khoá cổng chỉ ở secret store (không env/argv/DB/audit/event/gói `clone`); agent không bao giờ nói chuyện với cổng (`filehub_lane`, sandbox chặn
TCP localhost); `.tangthu*` trong bản sao cũ (`GuardContext.filehub_old_copies`) + `HUB_HOME/filehub` = cấm (`filehub_metadata`). Hub KHÔNG BAO GIỜ xoá/
di chuyển bản sao cũ của đồng bộ; thư mục còn cấu hình đồng bộ = "cần chuyển": run/task bị từ chối với `LEGACY_TEXT`, không bao giờ chạy trong bản sao.
Xoá trong thư mục chung = `write` (chỉ báo sau run). Event filehub chỉ mang tên/đường dẫn tương đối/id. Điều phối viên chỉ đọc (`filehub_list|filehub_link`)
+ giao việc `filehub:`; thêm thao tác ghi filehub → HUMAN_ONLY + ca trong `tests/test_sec_auth.py`/`test_manager_security.py`. Test: server GIẢ
`tests/fake_kgs_lane.py` + `lane_fake.py` (`test_lane_*.py`, `test_filehub_h5.py`, `test_web_filehub.py`) — đổi §C → sửa fake trong CÙNG PR.

## Log hệ thống — `docs/dev-notes/plan-logging.md` (L1 nền tảng, L2 req_id, L3 audit đầy đủ, L4 chuỗi băm audit, L5 truy vấn + cảnh báo + lưu trữ audit, L6 công cụ debug, L7 gói chẩn đoán, L8 dọn nuốt lỗi)
| Module | Vai trò |
|---|---|
| `logs.py` | `setup_logging(proc, filename, shared)` JSONL mọi tiến trình (daemon `hub.log`, web `web.jsonl`, MCP `mcp.jsonl`), handler root cho thư viện (không lặp), che secret; `LevelController`/`level_loop` (config `logs.level|levels` + level tạm `hub_state logs.temp_levels`); `install_crash_handlers` (excepthook, faulthandler `logs/faults.log`, SIGUSR1), `asyncio_exception_handler`, `ERROR_COUNTS` |
| `logs_rpc.py` | RPC `logs.levels|level` (HUMAN_ONLY, `logs.level` tự audit), CLI `cfharness logs level` (∈ HUB_HUMAN_ONLY_CLI); L6: RPC `logs.query` + `trace.get` (HUMAN_ONLY, ∈ `SENSITIVE_READS`, chạy trong thread — trace mở SQLite CHỈ ĐỌC riêng), CLI `cfharness logs [--since --level --logger --req --run --agent --proc -n -f --local]` (daemon dừng → đọc file trực tiếp) + `cfharness trace <req_id|run_id|task_id>` (∈ HUB_HUMAN_ONLY_CLI) |
| `log_query.py` | L6: đọc `*.jsonl` + `hub.log` (cả `.N`, `.gz`) lọc THEO TRƯỜNG (chuỗi con chỉ là lọc thô trước parse), gộp theo ts, trần dòng/byte (file mới trước, file lớn đọc đuôi, bỏ file có mtime trước `since`); `follow` (`-f`); `ErrorCounter` (handler WARNING, thùng 1 phút/24 h, gắn trong `setup_logging` → `hub.status.log_errors`); `recent_errors` (doctor "Lỗi gần đây": gom logger + `msg_template`) |
| `trace_view.py` | L6: dòng thời gian audit (req_id/run_id) + log + event TÓM TẮT (`EVENT_KEYS`, giá trị chỉ từ khoá/id ngắn; bỏ level detail/debug) + guard (không input) + approval (không input/lý do) + run con (`parent_run_id`, `continued_in_run_id`, lệnh con qua `parent_req_id` trong log); event theo req không gắn run = cửa sổ rowid tìm nhị phân theo ts (không index `events.req_id`) |
| `web/debug_pages.py` | L6: `/logs` "Nhật ký hệ thống" + `/trace/{id}` (chỉ đọc, autoescape; `/audit` link req_id, trang run link "Dòng thời gian") |
| `audit_chain.py` | L4: chuỗi băm `audit_log` (v30 `prev_hash`/`row_hash`, `CHAIN_COLUMNS` = HỢP ĐỒNG), `insert_chained` (writer DUY NHẤT của `audit_log`: `AuditLog.record`, `hub_freeze` ngoại tuyến, `hub_transfer` trên stage), `verify_chain`/`verify_db` (kết nối chỉ đọc), `AuditChain` (`hub.audit_chain`: neo `HUB_HOME/audit-anchors.jsonl` + metadata admin `audit.anchor` + event `audit.anchored` khi `audit.anchor_notify`; kiểm đuôi lúc khởi động; event `audit.tamper_detected`). RPC `audit.verify` (HUMAN_ONLY, luồng phụ), CLI `cfharness audit verify [--since-id N] [--json]` (mã thoát 1 khi gãy) |
| `audit.py` (L5) | `AuditLog.query` lọc principal/actor/outcome/req_id/kind tiền tố/since-until (id tìm nhị phân theo ts — `first_id_at`/`last_id_at`)/`q` (CHỈ summary+target)/`before_id`; `activity(days)`; `export_conn` (jsonl/csv, 0600 O_EXCL, mọi cột kể cả băm, `data` nguyên chuỗi đã che); `row_taps` (dòng đầy đủ + `data_obj` sau mỗi lần ghi, lỗi chỉ log). RPC `audit.list` (tham số mới, `since` nhận `24h`), `audit.export` (∈ MUTATING, ghi `exports/audit-<ts>.<ext>`, kết nối chỉ đọc trong thread), `audit.activity` (∈ SENSITIVE_READS) — đều HUMAN_ONLY; CLI `audit list --principal --actor --outcome --req --q --since --until --before-id`, `audit export`, `audit verify --archive`; web `/audit` (bộ lọc, cột kết quả `.pill.outcome-*`, "Cũ hơn", thẻ "Hoạt động theo người") |
| `audit_watch.py` (L5) | `hub.audit_watch` (row_tap): luật `denied_burst`/`login_fail_burst`/`security_change`/`secret_access`/`new_principal`/`mass_read`/`tamper` — cửa sổ trượt trong RAM, event `audit.alert{rule, principal, count, window_s, state on|resolved, subject?}` + dòng audit cùng tên; chống trùng theo (luật, principal), `resolved` sau trọn cửa sổ (vòng 30 s + mỗi lần ghi), trạng thái `hub_state audit_watch.active`, tập principal `audit_watch.seen` (nạp từ audit sẵn có lần đầu). Cấu hình `audit_watch.*` (∈ SECURITY_KEYS, `validate_setting`) |
| `audit_archive.py` (L5) | `hub.audit_archive` (tắt mặc định, `audit.archive_after_days`): tháng UTC đã trọn trước mốc → kiểm chuỗi + neo → `audit-archive/<yyyy-mm>.jsonl.gz` (0600/0700) → `MANIFEST.json` → transaction đặt `hub_state audit.archive_cutoff` + DELETE (trigger v31). Không lưu trữ dòng cuối; tiếp tục được sau khi chết giữa chừng; `verify_archives` (sha256 + chuỗi + nối liền); bỏ lượt khi `export_quiesced`/sao lưu |
| `support_bundle.py`, `support_cli.py`, `web/support_pages.py` (L7) | Gói chẩn đoán: `create(paths, since, run_ids, include_raw, dry_run)` → kế hoạch hoặc `exports/support-<ts>.tar.gz` (0600, `.part` O_EXCL → rename). Nguồn theo DANH SÁCH TRẮNG: `meta/` (versions từ `hub_state.cli_compat` — không spawn claude, `hub_doctor.run`, `hub.status`, config đã lọc `filter_config`), `logs/` (chỉ tên khớp `LOG_JSONL_RE`/`LOG_TEXT_RE`, không thư mục con/symlink; JSONL lọc theo `ts`), `runs/<id>/run.json` (`RUN_COLUMNS`) + `trace.json` (`trace_view.build`), raw chỉ khi `include_raw`. Mọi byte qua `redact.redact_count` → `redaction_report` (chỉ số đếm). RPC `support.plan|list` + `support.create` (∈ MUTATING_SUPPORT, tự audit; `include_raw` cần `confirm_raw`) — HUMAN_ONLY; CLI `cfharness support bundle|list` (daemon dừng → tại chỗ giữ `daemon.lock`); web thẻ Cài đặt + `/support` (xem trước) + ACTIONS `support_create`/`support_create_raw` (bước 2) + tải về một lần (`transfer_pages.mint_download/take_download/stream_file`, kho token riêng) |
| `aiotasks.py` | `track(task, keep)` / `spawn(coro)`: tác vụ nền "bắn rồi quên" có log lỗi — dùng thay cho `keep.add(t); t.add_done_callback(keep.discard)` |
| `trace.py` | L2: mã yêu cầu `req_id` (`r_<ULID>`), contextvar `REQ_CTX` {req_id, parent_req_id, channel, principal}, `bind`/`child`/`traced` (điểm vào nội bộ: chat, nhịp điều phối viên, lịch, dispatcher, auto-resume), `outgoing` (client gắn `params._trace`), `accept` (daemon: chỉ nhận đúng định dạng), `TraceFilter` (log). Daemon trả `_meta.req_id` trong MỌI phản hồi (`RpcError.req_id`, CLI in "mã yêu cầu"); access log `cfharness.rpc` (đọc = DEBUG, ghi/lỗi = INFO, denied/`rpc.slow` = WARNING; `logs.slow_rpc_ms`) |

Bất biến: log là METADATA — không bao giờ ghi prompt, trả lời, input/output tool, diff, giá trị secret (kể cả DEBUG). Chỉ đổi level được cho
`cfharness[.x]` + `logs.LIB_LOGGERS` (không httpx/urllib3). Listener bus lỗi không được làm hỏng `publish`. Tiến trình mới chạy lâu → `setup_process_logging`
(lỗi IO bỏ qua). Test: `tests/test_logging_l1.py` (fixture trả lại trạng thái logging toàn cục — test gọi `setup_logging` phải dùng nó).
req_id (L2): CHỈ là mã đối chiếu — client/agent tự khai được, nên không bao giờ dùng để quyết định quyền/danh tính/chống trùng; daemon kiểm định dạng
chặt (dài đúng 28, Crockford HOA) và pop `_trace` trước handler. Migration v28: `runs.req_id`, `audit_log.req_id`, `events.req_id`. Run luôn có req_id
(lệnh tạo, hoặc mã riêng khi tạo ngoài mọi lượt); task `_run` bind req_id của run → event/log/audit của run cùng mã; env `CFHARNESS_REQ_ID` cho `claude`
→ hook guard gửi kèm `guard.check`. `HubEvent.req_id` tuỳ chọn (`Hub.publish` điền). Access log chỉ metadata (`logs.EXTRA_FIELDS`) — không params/kết quả;
method chờ lâu theo thiết kế → `daemon.SLOW_EXEMPT`. Điểm vào nội bộ mới tạo run → `@trace.traced("<kênh>")`. OTel: trace_id = băm req_id của run,
thuộc tính span run theo `otel.RUN_SPAN_KEYS`. Test: `tests/test_trace_id.py`.
Audit đầy đủ (L3, migration **v29**: `audit_log.outcome|error_code|duration_ms|principal|declared_channel|session_ref|ip|caller_run_id`):
`principal` do DAEMON chứng thực (`auth.principal_of`) — token theo loại client `auth.TOKEN_KINDS` (`<socket>.auth` CLI/TUI, `<socket>.web.auth` web,
`<socket>.mcp.auth` MCP server; sinh lại cùng nhau; client web/mcp thiếu token riêng → token người). Kênh của người qua socket = `auth.attested_channel`
(token, không phải `_audit.channel`; kênh khai → `declared_channel`). `params._client` (web: `session_ref` băm + `ip` chỉ khi qua public_origin — `web_client_meta`;
MCP: `transport`/`level`) chỉ nhận theo loại token (`clean_client_meta`), daemon pop trước handler. Ngữ cảnh lượt (`trace.REQ_CTX`: principal, audit_channel,
declared_channel, session_ref, ip, caller_run_id) là mặc định của `AuditLog.record`. `wrap_with_audit` ghi MỌI lời gọi (ok/denied/error + `duration_ms`, không
traceback, ném lại lỗi gốc; dòng lỗi bỏ khoá kiểu thông tin đăng nhập `FAILURE_DROP_KEYS` — thêm tham số bí mật mới → thêm vào đó) và đặt dấu `AUDITED_ATTR` — lớp bọc ngoài phải giữ `__wrapped__`; method ghi mới không bọc → thêm lý do vào `SERVICE_AUDITED` của
`tests/test_audit_full.py`. Đọc nhạy cảm `mcp_server.rpc.SENSITIVE_READS` → daemon ghi `read:<method>` (đích + số bản ghi, chuỗi tìm kiếm chỉ độ dài), gộp
(principal, method, target) / 10 phút trong RAM (dòng đầu ngay, dòng `coalesced` khi cửa sổ đóng — audit append-only). Agent/điều phối viên trong run: req_id
client gửi bị bỏ, mã mới với cha = req_id của run. Token web/mcp cùng lớp bảo vệ với token người (sandbox, `permissions.deny`, classifier, fileview, backup).
`audit.list` ∈ HUMAN_ONLY, `cfharness audit` ∈ HUB_HUMAN_ONLY_CLI. Không bao giờ đưa sid/khoá phiên/token vào audit. Test: `tests/test_audit_full.py`.
Công cụ debug (L6): agent KHÔNG đọc được `HUB_HOME/logs/` — sandbox denyRead + `permissions.deny` Read/Grep/Glob (`secret_read_deny_rules`) + classifier
(`sensitive_read`, `sensitive_regions` → `hub_secret_read` forbidden); `cfharness logs|trace|daemon logs` forbidden. `trace.get`/`/trace` không bao giờ trả payload
event, input tool/guard/approval, `data` của audit — thêm trường event vào tóm tắt = thêm vào `trace_view.EVENT_KEYS` (giá trị vẫn qua `_SAFE`). Log trả về
qua RPC che lại theo giá trị secret (`mask_values`); terminal qua `termsafe.one_line`, web autoescape (không `|safe`). Log có thêm trường `parent_req_id`.
Test: `tests/test_debug_tools.py`.
Chuỗi băm (L4, migration **v30**): MỌI INSERT vào `audit_log` qua `audit_chain.insert_chained` (đọc dòng cuối + ghi trong CÙNG transaction, id cấp tường minh
→ id liên tiếp trong vùng chuỗi; test quét mã nguồn đòi `INSERT INTO audit_log` chỉ có ở `audit_chain.py`). Đổi `CHAIN_COLUMNS` = thay đổi phá vỡ; cột audit
mới KHÔNG tự vào chuỗi. Dòng trước v30 = NULL (không kiểm), dòng chuỗi đầu = `genesis:<sha256 dòng cuối trước v30>`. Bản neo: `audit-anchors.jsonl` ∈
`backup.INCLUDE_FILES` (đi cùng bản sao lưu + gói xuất clone/move → khôi phục/nhập ra neo khớp DB; `update_helper.restore_anchors` khi quay về DB trước cập
nhật), ∈ `HUB_WRITE_DENY_NAMES` (sandbox); không neo khi đang sao lưu/xuất gói (`AuditChain._busy` — đọc đuôi TRƯỚC khi kiểm). Không neo vào
`integrity_baseline` (cùng hub.db — không thêm bảo vệ). Payload `audit.tamper_detected` CHỈ id + mã lý do; báo lúc khởi động chống lặp theo chữ ký
(`hub_state audit.tamper_reported`). Nhánh `audit` ∈ `integrity.SECURITY_KEYS`. Test: `tests/test_audit_chain.py`.
Truy vấn + cảnh báo + lưu trữ (L5, migration **v31**: `audit_no_delete` chỉ cho xoá `id <= hub_state['audit.archive_cutoff']`): `q` không bao giờ tìm
trong `data`; tap audit (`taps`/`row_taps`) không bao giờ làm hỏng lượt ghi; payload `audit.alert` CHỈ metadata (`subject` thuộc từ vựng cố định
`audit_watch.SUBJECTS`, principal qua `clean_principal`) — thêm luật = thêm vào `RULES`/`RULE_VI`/`DEFAULTS` (+ config) + ca đỏ→xanh trong
`tests/test_audit_watch.py`; luật phải rẻ (RAM, không quét DB mỗi lần ghi). Dòng `audit.alert` bị bỏ qua (không vòng lặp). `new_principal` không báo
`hub:`/`agent:`/`manager:`/`anon:`/`web:sess_`/`web:anon`; cài mới học im lặng principal `os:` đầu tiên. Env `CFHARNESS_AUDIT_WATCH=off` (conftest đặt cho
bộ test chung). Telegram gửi `audit.*` vào `security_chats`. Lưu trữ: không bao giờ xoá trước khi file + manifest đã ghi xong; chuỗi gãy/neo lệch → không
lưu trữ; `verify_chain` nối dòng đầu DB với `next_prev` của mục manifest (`archive_missing` nếu có mốc mà không nối được), bản neo trỏ vào dòng đã lưu
trữ đối chiếu với `anchors` trong manifest. `audit-archive/` ∈ `backup.INCLUDE_DIRS` (bản sao lưu + gói `.cfhx`), `pathmap.SCAN_SKIP_DIRS`, sandbox
denyRead/denyWrite + `permissions.deny` + `HUB_WRITE_DENY_NAMES`, classifier `sensitive_read`/`sensitive_regions`. Test: `tests/test_audit_query.py`,
`tests/test_audit_watch.py`.
Gói chẩn đoán (L7): KHÔNG BAO GIỜ thêm nguồn bằng cách quét thư mục rộng — nguồn mới = hàm `_*_items` riêng + ca "file cấm vắng mặt" trong
`tests/test_support_bundle.py`; không bao giờ hub.db/secrets/auth/token socket/session Claude/checkpoints/backups/exports/imports/audit-archive/tri thức
agent. Mọi byte vào gói qua `support_bundle.scrub` NGAY trước khi ghi (sha256 manifest = nội dung đã che). Cột `runs` mới chỉ vào gói khi thêm vào
`RUN_COLUMNS` (metadata, không chữ tự do). Raw transcript luôn cần xác nhận bước 2 ở MỌI bề mặt (`confirm_raw` RPC, `--confirm-raw`/gõ `raw` CLI,
`Action.step2` web). `exports/`, `imports/`, `backups/` ∈ classifier `sensitive_regions` (Bash) ngoài sandbox + `sensitive_read`. `update_helper.JsonLog` (chỉ stdlib) và `logs.setup_stream_json` (import-job) ghi dòng JSON
cùng trường lõi `hub.log`. Test: `tests/test_support_bundle.py`.
Nuốt lỗi (L8): handler `except Exception`/`except BaseException`/`except:` MỚI phải (a) log kèm `exc_info` (`log.debug(..., exc_info=True)` trở lên,
hoặc `log.exception`), (b) ném lại, hoặc (c) có chú thích `# fail-closed: <lý do>` / `# best-effort: <lý do>` (`# pragma: no cover — <lý do>` cũng
được; pragma trần thì không) trên dòng `except` hoặc dòng lệnh đầu của thân — kiểm bằng `scripts/check_broad_except.py` (AST) trong
`tests/test_broad_except.py`; baseline theo file `scripts/broad_except_baseline.txt` chỉ được GIẢM (`--update` sau khi dọn). Log trong handler chỉ
metadata (id, tên lớp lỗi) — không payload; lỗi của client Telegram/Slack/webhook/HTTP không `exc_info` (traceback có thể mang URL chứa token) → dùng
chú thích. Module chỉ stdlib (hook, `mcp_guard`, `observe_hook`, `hub_self`, `update_helper`, `transport`, `oscompat`, `cron`) chỉ thêm chú thích.
Tác vụ "bắn rồi quên" dùng `aiotasks.spawn(coro, name=…)`; task giữ handle (huỷ/await theo vòng đời) đặt `name=`.
