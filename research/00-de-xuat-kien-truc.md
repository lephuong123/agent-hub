# agent-hub — Đề xuất kiến trúc (tổng hợp từ nghiên cứu hermes-agent)

> Nguồn: 5 báo cáo trong `research/notes/` (01 lõi harness · 02 tự cải thiện · 03 đa agent ·
> 04 tích hợp · 05 UI/quan sát). Mọi khẳng định về Hermes có `path:line` trong báo cáo gốc.
> Phần "đề xuất" là thiết kế cho agent-hub, không phải thứ Hermes đã có.

## 1. Kết luận chính

1. **Không fork Hermes.** Repo ~20k file, ~1M dòng Python, nhiều file 2–8k dòng, constructor ~60 tham số,
   và docs lệch code ở nhiều chỗ (max_iterations, delegation depth/concurrency, tên tool MCP).
   Hermes là **kho pattern đã trả học phí**, không phải nền để xây lên.
2. **Hermes mạnh ở 1 agent tự học, yếu ở quản lý team.** Không có thực thể Team, không có registry
   trạng thái tập trung, MCP server chỉ là cầu nối nhắn tin, chưa có màn "htop" cho nhiều agent,
   và vòng tự học **không có eval/rollback theo kết quả**. Đây chính là phần giá trị riêng của agent-hub.
3. **Kiến trúc đề xuất: agent-hub = control plane mỏng** (registry + điều phối + vòng học + event bus
   + MCP), còn vòng lặp agent dùng runtime có sẵn (Claude Agent SDK) thay vì tự viết lại 2–4k dòng loop.
   Runtime được bọc sau một interface `AgentRuntime` để sau này đổi được.

## 2. Bức tranh tổng thể

```
         Claude Code / Serena-style client / hệ khác
                │ MCP (stdio + Streamable HTTP)      │ HTTP Runs API + SSE
                ▼                                     ▼
 ┌──────────────────────────── HUB DAEMON (Python, asyncio) ─────────────────────────────┐
 │  MCP Server        Runs API        Control API (JSON-RPC/WS)  ◄── CLI/TUI "htop"       │
 │      └───────────────┴──────────────────┬────────────────────────────────┘             │
 │                                         ▼                                              │
 │  Registry: Agent · Team · TeamMember     Orchestrator: Task(DAG) · Run · lease/heartbeat│
 │                                         │                                              │
 │  Policy hooks (pre/post tool, pre/post llm) ──► EVENT BUS (seq+epoch, ring, fanout)     │
 │                                         │              │                               │
 │  Learning loop: signals → review fork → proposals → ledger → eval gate → apply/rollback │
 │                                         │                                              │
 │  Store: SQLite WAL (agents, teams, tasks, runs, events, sessions, messages, FTS5, ledger)│
 └─────────────────────────────────────────┬──────────────────────────────────────────────┘
                                           ▼ spawn worker (process/agent)
                      AgentRuntime (Claude Agent SDK) ── MCP client ──► MCP nội bộ
                      home: agents/<id>/{SOUL.md, MEMORY.md, USER.md, skills/, config.yaml}
```

## 3. Bốn trụ cột (ứng với 4 mục tiêu)

### 3.1 Quản lý team agent (thêm/sửa/xoá)
Mượn từ Hermes: **agent = thư mục home cô lập** (profile), CRUD/clone/export; **Kanban SQLite**
(Task tách khỏi Run, DAG, comment làm kênh agent↔agent↔người, lease + heartbeat bằng `(pid, start_time)`,
circuit breaker, giới hạn đồng thời theo agent, định tuyến theo `description`).
Khác Hermes: **một DB chung có cột `agent_id`/`team_id`** (Hermes để state.db riêng từng profile → không xem được cả hạm đội).

Data model (rút gọn):
```
Agent      id, name, role/description, home_path, model, toolsets[], mcp_servers[], tool_policy,
           limits{concurrency, max_iterations, budget_usd_day}, status, current_run_id,
           config_revision, archived_at
Team       id, name, charter, policies{review_required, budget, failure_limit}, estop
TeamMember team_id, agent_id, role(lead|worker|reviewer|verifier), max_in_progress
Task       id, team_id, title, body, status, assignee_agent_id, parents[], created_by, idempotency_key
Run        id, task_id?, agent_id, agent_config_revision, parent_run_id, status/outcome,
           usage{tokens, cost_usd}, summary, result_json, started/ended/heartbeat_at
Event      agent_id, run_id, seq, epoch, ts, type, payload
Signal     agent_id, run_id, kind(correction|undo|reject|edit_output|repeat_pattern|failure), evidence
Proposal   agent_id|team_id, scope(agent|team|global), target(memory|skill|config), diff, status
LedgerEntry proposal_id, before_blob, after_blob, applied_at, eval_score_before/after, rolled_back
```

### 3.2 Harness tự cải thiện (lõi giá trị)
Mượn gần nguyên văn từ Hermes (`agent/background_review.py`, `agent/curator.py`):
- **2 kho**: memory khai báo, nhỏ, luôn trong prompt, có hạn mức cứng; skills thủ tục, nạp theo nhu cầu, chỉ index ≤60 ký tự.
- **Review fork sau turn**: đếm ~10 turn / 10 tool-iteration → fork bản sao, whitelist chỉ tool memory/skill
  (+ đọc read-only), ngân sách iteration/token, persistence tách tuyệt đối khỏi session thật.
- **Curator 2 pha**: pha deterministic (stale 14 ngày → archive 30 ngày) luôn chạy; pha LLM gộp là opt-in.
- **An toàn**: provenance (`created_by`), archive thay vì xoá, pin, ledger + rollback từng entry, snapshot trước thao tác hàng loạt, staging + duyệt.
- **Bài học "đừng lặp lại"** (từ comment trong code Hermes): không học negative claims/lỗi môi trường;
  một fact – một kho; memory viết dạng khai báo, không mệnh lệnh; xoá skill phải có đích hấp thụ.

Thêm mới (Hermes chưa có):
- **Tín hiệu có cấu trúc** từ hook/UI: approve/reject, undo, user sửa output (diff), hủy run, chuỗi tool lặp ≥N lần, lỗi lặp lại → đưa vào prompt review làm *evidence*.
- **3 tầng tri thức**: agent → team → global. Kho chung chỉ ghi qua Proposal + duyệt.
- **Eval gate**: mỗi agent có bộ golden task (lấy từ run thật được user đánh dấu tốt); patch skill/config → replay → tụt điểm thì tự rollback.
- **Curator liên agent**: phát hiện skill trùng giữa các agent → đề xuất promote lên tầng team.

### 3.3 Cắm vào hệ thống đang chạy (kiểu Serena)
- **agent-hub là MCP server** (stdio cho Claude Code spawn trực tiếp + Streamable HTTP cho dùng chung).
  Tool: `hub_list_agents`, `hub_agent_create/update/archive`, `hub_team_*`, `hub_run(agent|team, task) → run_id`,
  `hub_events(run_id, after_seq)` (long-poll, tránh timeout MCP), `hub_approve`, `hub_cancel`, `hub_steer`,
  `hub_feedback(run_id, …)` (nguồn tín hiệu học), `hub_memory_search`, `hub_skill_*`. Kèm `instructions`
  + resource hướng dẫn (`hub://huong-dan`) như các MCP nội bộ đang làm.
  Phê duyệt phải nối thật vào runtime (Hermes có `permissions_*` nhưng không có nguồn sự kiện).
- **agent-hub là MCP client**: mượn schema `mcp_servers` của Hermes — `command/args/env | url/headers`,
  `tools.include/exclude` (glob), `trust: untrusted` (tool không có `readOnlyHint` → phải duyệt), `lazy`,
  `supports_parallel_tool_calls` (bật cho các MCP nội bộ read-only), env whitelist. Có lệnh import `mcpServers` từ `~/.claude.json` (lọc secret).
- Thứ tự giao thức: **MCP → HTTP Runs/SSE → A2A → ACP**.

### 3.4 Giao diện "htop cho agent"
Mượn từ Hermes (`tui_gateway/`): core sở hữu công việc, UI chỉ attach/detach; một wire JSON-RPC có schema Pydantic;
event có `seq + epoch` + ring buffer để replay; fanout có hàng đợi giới hạn (viewer chậm bị cắt, agent không chờ);
câu hỏi ngược (approval/clarify) là request có id, replay khi attach.

```
┌ agent-hub ─ 5 agents ─ $3.42 hôm nay ─ 1.2M tok ───────────────────────────────┐
│ ID  NAME        STATUS     TASK                    LAST TOOL         T   TOK  $ │
│ a1  researcher  ● working  tóm tắt repo hermes     read_file (2.1s)  4m  210k .61│
│ a2  coder       ◐ waiting  approval: rm -rf build  terminal          9m  88k  .20│
│ a3  reviewer    ○ idle     —                       —                 —   12k  .03│
└ ↑↓ chọn · Enter attach · a approve · s steer · x stop · l learnings · q thoát ──┘
```
Enter → stream live (reasoning thu gọn, dòng tool, diff, thanh model·context%·tok/s·$); Esc → về bảng, agent vẫn chạy.
Tab `learnings`: xem proposal đang chờ, ledger, diff skill, rollback.

Event tối thiểu: `agent.status`, `turn.start`, `message.delta`, `reasoning.delta`, `tool.start`, `tool.complete`,
`usage.tick`, `turn.complete`, `request.open/cancel`, `subagent.spawn`, `learning.proposal`, `learning.applied`, `error`.
Màn tổng quan chỉ nhận "firehose" tóm tắt (không delta token).

## 4. Stack đề xuất
| Lớp | Lựa chọn | Lý do |
|---|---|---|
| Hub daemon | Python 3.12, asyncio, FastAPI/Starlette WS, Pydantic | Khớp Hermes để mượn pattern/prompt; Pydantic sinh JSON Schema cho client |
| Lưu trữ | SQLite WAL + FTS5; ring buffer in-memory | Một writer duy nhất, đủ cho 1 máy/1 team; nâng Postgres sau |
| Agent runtime | Claude Agent SDK (model mặc định: Claude Opus/Sonnet 5.5) sau interface `AgentRuntime` | Có sẵn loop, tool, MCP client, hooks, subagent — không viết lại 2–4k dòng |
| MCP | SDK `mcp` Python, stdio + Streamable HTTP | Cùng giao thức với Claude Code và các MCP nội bộ |
| TUI | Textual (Python) | Một ngôn ngữ; DataTable + Log hợp "htop"; `textual serve` ra web. (Thay thế: Ink như Hermes nếu muốn nhúng xterm.js) |
| Tracing (tuỳ chọn) | OpenTelemetry/Langfuse từ hooks | Như plugin của Hermes |

## 5. Lộ trình
| Giai đoạn | Nội dung | Kết quả kiểm chứng |
|---|---|---|
| **M0** (1 tuần) | Hub daemon + SQLite schema + Agent CRUD (CLI `hub agent create/list/edit/rm`) + 1 runtime chạy 1 agent + event bus | `hub run researcher "…"` chạy, event vào DB |
| **M1** (1–2 tuần) | MCP server (list/run/events/cancel) + MCP client import từ `~/.claude.json` + trust/approval | Gọi agent-hub từ Claude Code như Serena; agent dùng được MCP nội bộ |
| **M2** (1–2 tuần) | TUI htop + attach/detach + approve/steer | Xem 3 agent chạy song song live |
| **M3** (2 tuần) | Team + Task DAG + Run + dispatcher (lease/heartbeat) + routing theo role | Giao 1 việc cho team, lead tách việc cho worker |
| **M4** (2–3 tuần) | Vòng học: hooks thu Signal → review fork → Proposal → ledger/rollback → curator | Agent tự thêm skill sau khi user sửa lỗi lặp lại; rollback được |
| **M5** | Eval gate (golden tasks) + tri thức team/global + curator liên agent | Patch làm tụt điểm bị tự rollback |

## 6. Rủi ro & quyết định cần chốt
- **Runtime**: Claude Agent SDK (nhanh, phụ thuộc Anthropic) vs tự viết loop theo mẫu Hermes (kiểm soát hoàn toàn, đa provider). Đề xuất: SDK trước, interface mở.
- **Phạm vi tự cải thiện**: tự áp dụng thay đổi nhỏ cho skill riêng agent; mọi thay đổi tầng team/global và USER profile phải qua duyệt.
- **Chi phí**: review fork + eval tốn token → ngân sách theo agent/team, giữ nguyên prompt prefix để tận dụng cache (bất biến số 1 của Hermes).
- **Bảo mật**: các MCP nội bộ hiện read-only; `trust: untrusted` mặc định để tool ghi mới xuất hiện tự động cần duyệt. Không đẩy dữ liệu MCP nội bộ ra ngoài qua tracing khi chưa có opt-in.
