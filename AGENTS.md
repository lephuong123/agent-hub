# AGENTS.md — hướng dẫn cho agent code tiếp trên repo này

## Bối cảnh
- Sản phẩm: hub điều phối đội agent chạy trên `claude` CLI. KHÔNG tự viết agent loop, KHÔNG gọi Anthropic API trực tiếp.
  Mỗi run = một tiến trình `claude -p … --output-format stream-json` do daemon spawn.
- Đọc trước: `research/BRIEF.md` (quyết định đã chốt), `docs/BRD-chi-tiet.md` (FR/NFR, data model §8, event §9, lộ trình §13),
  `docs/spikes/M0-spike-report.md` (cờ CLI đã kiểm chứng).
- Mốc hiện tại: M0 xong phần lõi. Tiếp theo là M1 (MCP server hub-self, phê duyệt thao tác phá huỷ, resume, khôi phục restart).
- Không sửa `research/`, `docs/` (trừ ghi chú nhỏ trong `docs/dev-notes/`), `demo/`.

## Kiến trúc (`src/agent_hub/`)
| Module | Vai trò |
|---|---|
| `paths.py` | Bố cục `HUB_HOME` (env `AGENT_HUB_HOME`), đường dẫn socket (rút về `/tmp/agent-hub-<uid>-<hash>.sock` nếu quá dài cho AF_UNIX) |
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
| `cli.py` | `agent-hub` (argparse). Client thuần của daemon; tự khởi động daemon nếu chưa chạy |

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

## Lệnh
```bash
uv sync
uv run pytest -q                      # toàn bộ test, ~4 s, KHÔNG gọi claude thật
uv run pytest tests/test_argv.py -q   # bộ dựng argv
uv run agent-hub --help
```
Test KHÔNG được gọi `claude` thật hay tốn tiền: dùng `FakeRuntime` (fixture JSONL) hoặc `tests/fake_claude.py` (binary giả cho `ClaudeCliRuntime`).
Fixture stream-json thật ở `src/agent_hub/fixtures/` (mặc định của FakeRuntime) và `tests/fixtures/` (chép từ `docs/spikes/m0/fixtures/` + lần chạy thật M0).
Khi nâng phiên bản CLI: ghi fixture mới, thêm contract test, rồi thêm phiên bản vào `runtime.claude.tested_versions`.

## Quy ước
- Thông báo người dùng, docstring, README, BRD: **tiếng Việt**. Định danh (tên hàm, field, cờ, loại event): tiếng Anh.
- Persona/SOUL/skill/memory/prompt gửi cho agent: **tiếng Anh**.
- Lỗi nghiệp vụ ném `HubError`/`NotFound`/`Conflict`/`Blocked` (`errors.py`) → mã JSON-RPC → mã thoát CLI (0/1/2/3).
- Loại event mới: thêm vào `EVENT_LEVELS` trong `events.py`, cập nhật ánh xạ trong docstring `claude_adapter.py`, và test.
- Không thêm `--dangerously-skip-permissions`; `bypassPermissions` chỉ khi `tool_policy.allow_bypass` + workspace scratch/worktree (NFR-15, có test).
- Thay đổi schema DB: thêm migration mới vào cuối `MIGRATIONS`.
- Không commit secret; argv lưu DB đã che (`redact.py`), file runtime 0600 và bị xoá sau run.

## M1 luồng A — Tích hợp (MCP server, MCP client registry, secret, audit)
Chi tiết, lệch BRD, việc dở: `docs/dev-notes/M1-A.md`.

| Module | Vai trò |
|---|---|
| `mcp_server/server.py` | MCP server `hub_*` (SDK `mcp` 2.x, `MCPServer`). Client MỎNG của daemon (RPC qua `DaemonCaller`, tự khởi động daemon). Output = model Pydantic; annotations readOnly/destructive. Quyền: stdio=`control`, HTTP theo token |
| `mcp_server/http.py` | Streamable HTTP CHỈ loopback + `BearerAuth` (401). Token `read`/`control` ở `HUB_HOME/mcp_server/tokens.json` (0600) |
| `mcp_server/rpc.py` | RPC `mcp.*`/`secret.*`/`audit.*`/`feedback.*`; `register_rpc(daemon)` ở CUỐI `Daemon.__init__` còn **bọc audit mọi method thay đổi trạng thái** (`MUTATING`; `channel` lấy từ `params._audit`, mặc định `cli`). `mcp.events` = long-poll ≤ 25 s theo run |
| `mcp_server/cli.py` | `agent-hub mcp serve|snippet|token|add|edit|list|show|rm|restore|probe|import|assign|unassign|agent-config`, `secret`, `audit list`, `feedback` |
| `mcp_client/` | Registry MCP (DB `mcp_servers` là nguồn sự thật; `mcp/registry.yaml` là BẢN SAO chỉ đọc). Hợp đồng cho B: `build_agent_mcp_servers(agent)`, `tool_annotations(server, tool)`, thêm `agent_mcp_tool_rules(agent)` |
| `mcp_client/wiring.py` | `attach(hub)` (gọi trong `Hub.__init__`): `hub.secrets`, `hub.audit`, `hub.mcp`, listener `ToolWriteWatcher` → audit + event `tool.write` |
| `secrets.py` | Secret store file 0600 `HUB_HOME/secrets/<NAME>`; `${NAME}` trong cấu hình, `resolve()` fail-closed |
| `audit.py` | `audit_log` append-only (trigger). `audit.record(kind, agent_id, run_id, data)` cho luồng B; tiến trình ngoài daemon dùng RPC `audit.record` |

Quy tắc thêm: method RPC mới thay đổi trạng thái → thêm tên vào `MUTATING` (rpc.py) và khai báo TRƯỚC dòng `register_rpc(self)`.
Tool `hub_*` mới: kiểm `hub.require_control(ctx, …)` nếu không readOnly; mô tả tiếng Việt; output là model Pydantic.
Test MCP: `tests/test_mcp_server.py` (in-process `Client(srv)`, stdio thật, HTTP uvicorn cục bộ), không gọi `claude`.
Kết nối Claude Code: `agent-hub mcp snippet` (CHỈ in lệnh/JSON, không sửa `~/.claude.json`).

## M1 luồng B — An toàn & vòng đời (guard, approval, Slack, resume, khôi phục)
Chi tiết, lệch BRD, việc dở, kết quả kiểm chứng `claude` thật: `docs/dev-notes/M1-B.md`.

| Module | Vai trò |
|---|---|
| `guard/shell.py` | Lexer shell: NFKC + chữ đồng hình, `${IFS}`, bỏ quote, `$'\x..'`, biến đã gán, `~`, heredoc/here-string, tách `;&&||\|&`, thu `$(..)`/`` `..` ``/`<(..)` |
| `guard/classifier.py` | `classify(tool, input, GuardContext) -> Verdict` (safe/write/destructive/uncertain + `forbidden`), THUẦN. "File run tự tạo" = birthtime ≥ lúc run đầu của session vào hàng đợi. `action_fingerprint` cho vé một lần |
| `guard/policy.py` | `HUB_HOME/policy/{protected,catalog}.yaml` + `history/`; tài nguyên bảo vệ mặc định (BRD FR-SG-02 + mã nguồn `agent_hub`) |
| `guard/service.py` | `GuardService.check` (RPC `guard.check`): phân loại → allow / mở approval + GIỮ ≤ `approval.timeout_s` → allow/deny/waiting_human |
| `guard/hook.py`, `guard/mcp_guard.py` | Hook PreToolUse + MCP `hub-guard` (permission-prompt-tool). Chỉ stdlib, FAIL-CLOSED (`|| exit 2`, alarm, mọi lỗi → deny) |
| `guard/runtime.py` | Sinh phần guard của settings.json/mcp.json; mẫu `--disallowedTools` tĩnh và bỏ đúng mẫu cho run resume có vé |
| `guard/locks.py` | Khoá thư mục `dir:` theo SESSION (bản tối thiểu; M3 thay bằng khoá repo + checkpoint) |
| `guard/rpc.py`, `guard/cli.py` | RPC `approval.*`/`guard.*`/`notify.test`/`run.continue|resume`/`agent.purge`; CLI `approvals`, `approve`, `deny`, `guard test|policy|protect|decisions`, `notify test`, `run continue`, `agent purge` |
| `approvals.py` | `ApprovalService`: open → waiting_human → allowed/denied/cancelled (không `expired`), vé một lần, nhắc lại giãn dần, Signal `reject` |
| `notify/slack.py` | Listener bus → Slack webhook/bot (secret), tin đã che, backoff, `notify.failed`; tắt êm khi chưa cấu hình |
| `recovery.py` | Đối soát `(pid, start_time)` khi daemon khởi động, dừng mồ côi, `interrupted` → tự resume (trừ waiting_human); purge retention |

Bất biến: thao tác phá huỷ KHÔNG BAO GIỜ tự từ chối do hết giờ; hook/hub-guard lỗi = deny; agent không ghi được `policy/`
(settings `permissions.deny` + classifier) và không tự duyệt (`agent-hub approve`, socket hub, `mcp__*__hub_approve` = forbidden).
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
| `tui/app.py` | Textual `TopApp`: `HtopScreen` (bảng agent + firehose), `AttachScreen` (transcript replay DB + ring → live), `TasksScreen` (RPC `task.*` của M3), `LearningsScreen` + `GoldenScreen` (M4-B, `tui/learnings.py`), form agent, hộp xác nhận/nhập. CLIENT THUẦN của daemon |
| `tui/model.py` | Trạng thái thuần (không Textual): `TopModel` nạp `tui.snapshot` + áp event firehose; đo độ trễ event→TUI; định dạng ô/firehose |
| `tui/client.py` | `AsyncHubClient` (asyncio, cùng loop Textual) + `subscribe()`; KHÔNG dùng `RpcClient` đồng bộ trong TUI/test in-process (chặn loop) |
| `tui/rpc.py` | RPC daemon: `tui.snapshot`, `run.steer`, `template.list|get`, `agent.create_from_template`, `run.takeover|takeover_record` |
| `tui/takeover.py` | `claude --resume <session>` tương tác cùng cờ cô lập/mcp/SOUL của agent (không hook guard); ghi Run `kind=human_takeover` |
| `tui/cli.py` | `top`, `attach`, `steer` / `run steer` (viết lại `run-steer` qua `guard.cli.HIDDEN`), `takeover [--print|--record]`, `template list|show` |
| `tui/bench.py` | `python -m agent_hub.tui.bench --home <tmp>`: đo NFR-01..05/08 bằng daemon thật + FakeRuntime + TUI headless |
| `steer.py` | `steer(hub, ref, text)`: ghi stdin stream-json của tiến trình đang chạy, phát `user.message`; `Conflict` khi run đã qua `result` |
| `templates/` | `researcher|coder|reviewer|ops.yaml` (config + SOUL tiếng Anh); `agent create <id> --template <tên>` |

Steer: adapter phát `steer.ack` cho tin user `isReplay` thứ 2 trở đi (tin #1 = prompt). `ClaudeCliRuntime` vẫn đóng stdin
sau `result` như M0, TRỪ khi còn tin steer chưa được phát lại (`handle.internal["steer_pending"]`; phòng hờ
`runtime.claude.steer_ack_grace_s`, mặc định 30 s). `FakeRuntime` hỗ trợ steer (chèn dòng replay; sau `result` từ chối).
Migration v5 (M2): `runs.steer_count`, `runs.takeover_of_run_id`, index `events(type, ts)`.
Test M2: `tests/test_tui.py` (Pilot + daemon IN-PROCESS trên socket thật; `AGENT_HUB_SCREENSHOTS=docs/screenshots/m2`
để ghi lại SVG), `tests/test_steer.py` (+ `tests/fake_claude_steer.py`: claude giả mô phỏng spike A-02), `tests/test_templates.py`.
Event mới: `user.message`, `steer.ack`, `run.takeover`. Mọi thao tác thay đổi từ TUI gửi `_audit.channel=tui`.

## M4 luồng B — Đánh giá & vận hành học (golden task, eval gate, hồi quy, curator, digest)
Chi tiết, hợp đồng với A, lệch BRD, nghiệm thu thật: `docs/dev-notes/M4-B.md`.

| Module | Vai trò |
|---|---|
| `evals/golden.py` | Golden task do NGƯỜI quản lý (Q-07): DB + bản sao `policy/golden/`; agent/vòng học chỉ gợi ý (`golden.suggested`) |
| `evals/checkers.py`, `evals/judge.py` | Chấm tất định (regex/contains/json_schema/command/all) + LLM judge `claude -p --json-schema --no-session-persistence` (run `eval_judge`) |
| `evals/gate.py`, `evals/overlay.py` | `hub.evals.gate.check(proposal)` / `post_apply(entry)` (async) — chạy golden task trên BẢN SAO home (`run_opts.eval_overlay`, run kind=`eval`), N lần, ngưỡng tụt, ngân sách; tụt sau áp dụng → B tự rollback (`eval_gate`) |
| `learning_ops/regression.py` | FR-LN-17: so correction/lỗi/cost trước–sau → `ledger.rollback(actor="regression_monitor")` |
| `learning_ops/curator.py` | FR-LN-08 pha 1 tất định (14/30 ngày, pin), archive qua `Ledger.apply` của A (`actor=curator`) |
| `learning_ops/digest.py` | FR-LN-13: digest 08:00 giờ máy → Slack (event `learning.digest`) + `digest.tui_open` |
| `learning_ops/wiring.py`, `rpc.py`, `cli.py`, `ports.py` | `attach(hub)`, vòng nền (start ở `hub.started`), RPC `golden.* eval.* digest.* curator.* regression.*` (tự bọc audit), CLI cùng tên |
| `tui/learnings.py` | Tab Learnings (FR-UI-06: feed, eval trước/sau, rollback `r`, pin `p`, digest `D`, journey `j`, canary placeholder M5) + `GoldenScreen` (`G`); digest khi mở TUI lần đầu trong ngày |

Bất biến: regression monitor/eval gate CHỈ tự rollback entry actor ∈ {learning, review, agent, curator}; thay đổi của NGƯỜI chỉ
cảnh báo (`regression.detected{human_change}`). Golden task/rubric thuộc chính sách an toàn — guard coi `agent-hub golden <ghi>` và `mcp__*__hub_golden_(add|rm|…)` từ agent
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
| `event_schema.py` | Sinh `docs/schema/event.v1.json` (`python -m agent_hub.event_schema --write docs/schema/event.v1.json`) |

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

## Telegram — bot thông báo, duyệt bằng nút, lệnh chat (luồng TG)
Chi tiết, lý do chọn `httpx2`, test chứng minh, việc dở: `docs/dev-notes/telegram.md`. Không có migration (trạng thái ở `hub_state`).

| Module | Vai trò |
|---|---|
| `notify/base.py` | `QueuedNotifier`: khung chung các kênh (lọc `notify.<kênh>.events`, hàng đợi, backoff, `notify.sent|failed{channel}`); `SlackNotifier` là lớp con |
| `notify/telegram.py` | `TelegramApi` (httpx2 async, gỡ token khỏi lỗi), định dạng văn bản thuần đã che, `CallbackStore` (nonce dùng một lần + HMAC, theo chat), `TelegramNotifier` |
| `chat/bot.py` | `hub.telegram`: long polling trong daemon (start ở `hub.started`, tắt êm khi chưa có token), ghép nối `/start <mã>`, lệnh chat, nút duyệt (2 bước cho thao tác nặng), theo dõi run → gửi kết quả, reply → `run.continue` |
| `chat/rpc.py`, `chat/cli.py` | RPC `telegram.*` (trừ `status` đều HUMAN_ONLY, tự audit); CLI `notify telegram setup|status|test|disable|allow|revoke` |

Bất biến: lệnh/nút từ chat CHỈ đi qua `Daemon.call_as(auth.telegram_caller(hub, chat_id), …)` (cổng HUMAN_ONLY + audit kênh `telegram:<chat_id>`);
`telegram_caller` là người chỉ khi bot bật VÀ chat ∈ `notify.telegram.allowed_chat_ids` (chỉ người sửa, nhánh config được kiểm toàn vẹn). Chat
ngoài allowlist: không trả lời, audit `telegram.ignored`. Nonce nút bị tiêu (cả nhóm) TRƯỚC khi gọi RPC; offset `getUpdates` lưu TRƯỚC khi xử lý.
Token bot chỉ ở secret store (không đọc env, không in, không trả qua RPC). Thêm lệnh chat ghi trạng thái → gọi qua `self.call(...)` (không gọi
service trực tiếp). Test: `tests/test_telegram.py` (daemon in-process), `tests/test_telegram_e2e.py` (daemon thật + hook guard thật),
server Bot API giả `tests/telegram_fake.py` — không gọi Telegram thật.

## WEB — giao diện trình duyệt (`agent-hub web`)
Chi tiết, kiểm chứng sandbox thật, việc dở: `docs/dev-notes/web.md`. Ảnh: `docs/screenshots/web/`. Test: `tests/test_web.py`.

| Module | Vai trò |
|---|---|
| `web/security.py` | Chỉ loopback; link đăng nhập MỘT LẦN (HMAC, khoá ở RAM + `HUB_HOME/auth/web.json` 0600) → cookie phiên HttpOnly/SameSite=Strict; CSRF; Host hợp lệ |
| `web/app.py` | Starlette + Jinja2 + htmx vendored + SSE. `GuardMiddleware` ASGI thuần (không dùng BaseHTTPMiddleware — làm hỏng SSE). Ghi = `POST /do/<action>` (bảng `ACTIONS`) hoặc form agent |
| `web/present.py` | Thuần: nhãn tiếng Việt, `transcript_item`, `activity_item`, `md()` an toàn |
| `web/cli.py`, `web/demo.py` | `agent-hub web [--port] [--open] [--url]`; hub demo FakeRuntime để chụp ảnh |

Quy tắc: web là CLIENT thuần (RPC qua `AsyncHubClient(channel="web")`), không đụng `hub.db`. Thao tác ghi mới → thêm vào `ACTIONS`
(nhạy cảm: đặt `confirm`), gửi `audit=True` (approval: `channel="web"`). Không script/style inline (CSP `'self'`), không CDN; tài nguyên
tĩnh mới đặt trong `web/static/`. Không bao giờ đưa token người/khoá `auth/` vào template, cookie, log. `config.set` chỉ khoá trong `SAFE_CONFIG`.
Agent trong run: sandbox chặn TCP localhost (đã kiểm thật) + denyRead `HUB_HOME/auth` + `agent-hub web` forbidden trong classifier.
