# 05 — UI & Quan sát (Observability) trong hermes-agent

> Phạm vi: CLI cổ điển, TUI (Ink), dashboard web, Desktop (Electron), lớp dữ liệu quan sát, khả năng "attach" vào session đang chạy.
> Quy ước: mọi tham chiếu `path:line` tính từ root repo `research/hermes-agent/`. Chỗ nào là **[suy luận]** thì đã đánh dấu; còn lại là điều đọc thấy trực tiếp trong code/AGENTS.md.

---

## 1. Tổng quan: 4 "mặt" UI, 1 giao thức lõi

| Mặt UI | Stack | Nói chuyện với core bằng | Nguồn |
|---|---|---|---|
| CLI cổ điển (`hermes`) | Python: **Rich** (banner/panel) + **prompt_toolkit** (input, autocomplete) + `KawaiiSpinner`; menu chọn dùng **curses** | In-process (gọi callback của `AIAgent` trực tiếp) | `hermes_cli/AGENTS.md:7-20`, `agent/display.py:847` |
| TUI (`hermes --tui`) | **React 19 + Ink** (fork riêng `@hermes/ink`), state bằng **nanostores** | **JSON-RPC 2.0, newline-delimited** — qua stdio (cũ) hoặc **WebSocket có xác thực** tới gateway (hiện tại, "canonical") | `tui_gateway/AGENTS.md:9-21`, `ui-tui/README.md`, `ui-tui/CANONICAL_GATEWAY.md:1-10`, `ui-tui/package.json:24-35` |
| Dashboard web (`hermes dashboard`) | Vite + React SPA; trang Chat **nhúng chính TUI thật** qua **xterm.js** + PTY WebSocket | `/api/pty` (byte PTY thô), `/api/ws` (JSON-RPC), `/api/events` (sidecar feed), REST `/api/sessions*`, `/api/analytics/*` | `web/AGENTS.md:8-30`, `hermes_cli/web_routers/chat_ws.py:299,538,702,755,767` |
| Desktop | **Electron 40 + React 19 + nanostores + @tanstack/react-query/virtual**, `@assistant-ui/react` | Cùng JSON-RPC qua WebSocket, client dùng chung `apps/shared` (`JsonRpcGatewayClient`) | `apps/desktop/src/AGENTS.md:6-30`, `apps/desktop/package.json` |

Nguyên tắc kiến trúc được lặp lại ở mọi AGENTS.md: **"TypeScript owns the screen. Python owns sessions, tools, model calls"** (`tui_gateway/AGENTS.md:17-18`). Dashboard **không được viết lại chat bằng React** — phải mở rộng Ink (`web/AGENTS.md:19-23`). Desktop thì ngược lại: có renderer chat riêng, nhưng mọi hành vi agent nằm sau gateway (`apps/desktop/AGENTS.md` mục "What this app is").

---

## 2. Sơ đồ kiến trúc UI ↔ core

### 2.1 Đường cũ (stdio, một process sở hữu agent)

```
hermes --tui
  └─ Node (Ink, ui-tui/src/entry.tsx → GatewayClient)
        │  stdin/stdout: JSON-RPC requests / responses / events (NDJSON)
        │  stderr: gom vào log ring trong RAM (→ event gateway.stderr)
        ▼
     python -m tui_gateway.entry  (tui_gateway/server.py = facade, methods_*.py = handlers)
        └─ AIAgent (run_agent.py) + tools + SessionDB (state.db SQLite)
```
(`ui-tui/README.md` mục "What runs"; `tui_gateway/AGENTS.md:9-16`)

### 2.2 Đường hiện tại ("canonical gateway" — một daemon, nhiều viewer)

```
                ┌──────────── gateway daemon (1 owner / HERMES_HOME, multiplex nhiều profile) ───────────┐
                │  gateway/session_authority.py  ── LiveSession{subscribers, event_stream: SessionEvents} │
                │        │  attach(actor, ref) → SubscriptionSnapshot(epoch, seq, history, pending, prompts)│
                │        ▼                                                                               │
                │  SessionEvents.publish() → event_replay ring (seq) → FanoutTransport (mailbox/peer)     │
                │        ▲                                                                               │
                │  AIAgent callbacks ──(gateway/local_viewer_events.py: wire())──► execution events      │
                └────────┬───────────────────┬───────────────────────┬─────────────────────────────────────┘
                         │ WS + ticket        │ WS + ticket            │ WS (dashboard token)
                    Ink TUI (viewer)     Desktop (viewer)        Dashboard /api/ws
                         ▲
        "Closing Ink detaches its socket without stopping the gateway"
```
- Ink/Desktop gọi `hermes gateway ensure --json` → lấy endpoint + **ticket điều khiển dùng một lần**, truyền qua WS subprotocol, không nằm trong URL (`ui-tui/CANONICAL_GATEWAY.md:3-10`, `apps/desktop/src/AGENTS.md:8-18`).
- `session_authority.attach()` thêm transport vào fanout, trả snapshot gồm watermark `(epoch, sequence)`, lịch sử local, receipt đang chờ và prompt (approval/clarify) đang mở (`gateway/session_authority.py:261-288`).
- Bắt buộc capability `session:read` để attach; các capability khác: `session:create|submit|control|approve|respond` (`hermes_cli/web_routers/chat_ws.py:716-722`).

### 2.3 Dashboard web (3 tầng process)

```
Browser ─ xterm.js ──/api/pty (raw PTY bytes, resize "\x1b[RESIZE:c;r]")──► web_server ─ptyprocess─► hermes --tui ─► tui_gateway
Browser ─ React sidebar ◄──/api/events (channel)── web_server ◄──/api/pub── tui_gateway (WsPublisherTransport: mirror mọi emit)
```
- `tui_gateway/event_publisher.py:1-8`: gateway phía PTY mở back-WS tới `/api/pub`, gương **mọi emit** dạng JSON; dashboard broadcast lại cho subscriber `/api/events` cùng channel. **Best-effort, drop khi đầy** (queue 256, `event_publisher.py:29`) — agent loop không bao giờ bị chặn.
- Route: `hermes_cli/web_routers/chat_ws.py:737-785`.

### 2.4 CLI cổ điển (in-process)
`HermesCLI` (`cli.py`) + mixins (`hermes_cli/cli_stream_mixin.py`, `cli_status_bar_mixin.py`, `cli_tui_mixin.py`…) gắn callback trực tiếp vào `AIAgent`. `agent/display.py` là hàm hiển thị thuần (không phụ thuộc AIAgent): preview tool (`build_tool_preview` :495), nhãn trạng thái (`build_status_phrase` :601), diff inline (`extract_edit_diff` :736, `render_edit_diff_with_delta` :829), `KawaiiSpinner` (:847) in "feed hoạt động `┊`", câu tóm tắt tool xong (`get_cute_tool_message` :1264). Lưu ý kỹ thuật: không dùng `\033[K` vì vỡ dưới `patch_stdout` của prompt_toolkit (`hermes_cli/AGENTS.md:37-40`).

Khi session chạy trên gateway, các callback CLI kiểu cũ (tool-generating, thinking, reasoning, MoA, `_print_fn`) được **chiếu thành execution event** cho viewer local (`gateway/local_viewer_events.py:1-11`).

---

## 3. Nguồn sự kiện gốc: callback của AIAgent

`AIAgent.__init__` nhận một loạt callback (`run_agent.py:251-280`, `agent/agent_init.py:2260-2287`):
`tool_progress_callback, tool_start_callback, tool_complete_callback, thinking_callback, reasoning_callback, clarify_callback, step_callback, stream_delta_callback, interim_assistant_callback, tool_gen_callback, status_callback, notice_callback, notice_clear_callback, event_callback(str, dict), reaction_callback, tool_result_metadata_callback`, cộng các callback desktop (`read_terminal`, `read_preview`, `connection`, `theme`…).

Gateway nối callback → event: `tui_gateway/agent_callbacks.py:141 (_agent_cbs)`, `tui_gateway/tool_progress.py:276 (_on_tool_start)`, `:324 (_on_tool_complete)`, `:377 (tool.output_risk)`; usage ticker 1 s: `tui_gateway/server.py:2021 (_start_usage_ticker)`.

Plugin hooks (dùng cho tracing kiểu Langfuse — nay là plugin catalog, `website/docs/user-guide/features/built-in-plugins.md:141-154`) — `hermes_cli/plugins.py:107-140`: `pre/post_tool_call`, `pre/post_llm_call`, `on_stream_start|delta|end`, `on_interim_message`, `pre/post_api_request`, `api_request_error`, `pre/post_auxiliary_call`, `on_session_start|end|finalize|reset`, `subagent_start|stop`, `agent_loop_stopped`, `pre_gateway_dispatch`… Ngoài ra có tích hợp NeMo Relay cho OpenTelemetry (`built-in-plugins.md:160`).

---

## 4. Giao thức wire & schema event

### 4.1 Envelope
```json
{"jsonrpc":"2.0","method":"event",
 "params":{"type":"tool.start","session_id":"<sid>","payload":{...},"seq":42}}
```
- Tạo ở `tui_gateway/server.py:718-720 (_event_frame)`; gửi qua `write_json` (:697) — route theo `session_id` tới transport của session. Payload được kiểm tra với contract (`_contracts.check_payload`).
- `seq`: đơn điệu theo session, gắn bởi `event_replay._stamp_event` (`tui_gateway/event_replay.py:66`). Event canonical còn có `replay_epoch` (`gateway/session_events.py:37-45`).
- Wire **khai báo bằng Pydantic ở Python, sinh ra TypeScript**: `tui_gateway/contracts/*.py` → `apps/shared/src/gateway-contract.generated.ts` + `gateway-contract.openrpc.json` qua `scripts/gen_gateway_contracts.py`; test fail nếu file sinh bị cũ (`tui_gateway/AGENTS.md:33-45`).

### 4.2 Danh mục event chính (`tui_gateway/contracts/events.py`)

**Luồng turn (thứ cần cho "xem agent như CLI")**
| Event | Payload chính | Dòng |
|---|---|---|
| `message.start` | `display_kind?, display_text?, text?` | :147 |
| `message.delta` | `text: str\|null` (null = ranh giới segment), `rendered?` | :169 |
| `reasoning.delta` / `reasoning.available` / `thinking.delta` | `text, rendered?, verbose?` | :170-172 |
| `message.interim` | `text, already_streamed` | :182 |
| `message.complete` | `text, usage: Usage, status: complete\|error\|interrupted, reasoning?, error?, error_surface{layer,code,retryable,…}, billing?, persisted_turn{row_ids,…}` | :261 |
| `status.update` | `kind` (status, lifecycle, compacting, goal, loop, heartbeat, process…), `text` | :271 |
| `session.usage` | `usage: Usage` + bộ đếm cũ (input/output/cache/reasoning tokens, api_calls, context_*, avg_latency_s, tokens_per_s, active_subagents, model, provider…) | :309 |
| `agent.print` | `text` (dòng trạng thái agent in ra) | :348 |

**Tool**
| Event | Payload | Dòng |
|---|---|---|
| `tool.generating` | `name` (model đang sinh args) | :396 |
| `tool.start` | `tool_id, name, args?, args_text?, preview?, context?, labels?` | :368 |
| `tool.complete` | `tool_id, name, duration_s?, result, summary?, result_text?, inline_diff?, todos?, revision?` | :387 |
| `tool.output_risk` | `tool_id, name, risk, findings[], redacted` | :409 |
| `todo.updated` | `todos[], revision` (snapshot đầy đủ) | :419 |

**Model `Usage`** (`tui_gateway/contracts/common.py:19-44`): `model, input, output, reasoning, prompt, completion, total, calls, compressions, context_used/max/percent, cache_hit_pct, cache_read/write, avg_latency_s, avg_tps, active_subagents, cost_usd, cost_status, dev_credits_spent_micros`.

**Khác**: `gateway.ready{skin, change_events, replay_epoch, session_authority?}` (:63), `error` (:120), `notice` (:129), `session.info` (:465), `session.title` (:319), `session.reclaimed` (:495), `session.control.update` (:522), `background.complete`/`btw.complete` (:547-548), `moa.*` (:644-647), `agent.terminal.output`/`terminal.close` (:701-702), `browser.progress` (:712), `voice.*` (:775-778), tín hiệu thay đổi `sessions.changed`/`cron.changed`/`platforms.changed`/`projects.changed`/`skills.changed` (:825-830 — sinh bởi watcher theo mtime của `state.db`, `cron/jobs.json`…), `request.cancel` (`contracts/server_requests.py:378`).

**Server → client request** (agent hỏi người dùng, chặn thread agent tới khi có trả lời cùng id `srq-<n>`): `approval`, `clarify`, `sudo`, `secret`, `vault.*`, `connection` (`tui_gateway/AGENTS.md:22-31`).

**Subagent** — không phải event mà là RPC poll: `subagent.list` → `{subagents, delegations}`; `subagent.tail` → 16 KiB cuối transcript con đang sống; `subagent.steer`, `subagent.interrupt` (`tui_gateway/AGENTS.md:96-124`, `tui_gateway/methods_subagents.py:53,105`). Trạng thái con: `queued|running|completed|failed|error|timeout|interrupted` (`contracts/common.py:179-189`). TUI có "dock" Live agents: Ctrl+T mở roster, Enter xem chi tiết, `t` xem tail, `e` steer, `x/X` dừng (`ui-tui/README.md` mục "Live agents").

### 4.3 Phía client TUI
`ui-tui/src/app/createGatewayEventHandler.ts` (event → state), `turnController.ts` (buffer delta, trạng thái tool/reasoning), `turnStore.ts` (nanostore: streaming text, tools, reasoning, subagents, todos, activity trail), `useLongRunToolCharms.ts` (thông báo tool chạy > 8 s), `gatewayRecovery.ts` (respawn/resume, ngân sách 3 lần/60 s) — theo `ui-tui/README.md` mục "App model".

---

## 5. "Attach" xem một session đang chạy từ process khác — CÓ

Ba cơ chế đọc thấy:

1. **Canonical gateway attach (chính thức)** — `session_authority.attach()` (`gateway/session_authority.py:261`): nhiều viewer (TUI, Desktop, dashboard) cùng subscribe một session; `FanoutTransport` (`tui_gateway/transport.py:143-168`) giữ mailbox có giới hạn cho từng peer (16384 frame / 4 MB); viewer chậm bị **rút subscription** (không làm chậm agent) và phải reconnect + replay. `session.detach` là canonical method (`contracts/canonical.py:301`). Đóng Ink chỉ detach, không dừng gateway (`ui-tui/CANONICAL_GATEWAY.md:12`).
2. **Replay theo seq** — `session.events.since{session_id,last_seen}` → `{events, latest_seq, truncated, epoch, open_requests}` (`tui_gateway/methods_session.py:2327-2341`). Ring mỗi session: 512 event / 4 MB, tối đa 64 session / 64 MB/process (`tui_gateway/event_replay.py:24-33`). `epoch` đổi khi restart → client reset watermark; `truncated` → client phải refetch transcript từ DB.
3. **Sidecar dashboard** — `/api/pub` → `/api/events` (mục 2.3), read-only, best-effort.

Liệt kê session đang sống: `session.active_list` → mỗi row `{id, title, model, started_at, last_active, message_count, status: working|waiting|starting|idle}` (`gateway/session_active_list.py:16-52`; `waiting` = đang có prompt approval/clarify mở). REST cho lịch sử: `/api/sessions`, `/api/sessions/{id}/messages`, `/api/sessions/{id}/timeline`, `/api/sessions/stats` (`hermes_cli/web_routers/sessions.py:182-801`).

Dashboard nhiều session? Dashboard có `SessionsPage`, `AnalyticsPage`, `LogsPage`, `SystemPage` (`web/src/pages/`) nhưng chat chỉ là một PTY TUI. Desktop có nhiều tile/pane chat, "background profiles keep streaming" (`apps/desktop/AGENTS.md` mục "Switching context") và sidebar poll `session.active_list` (`gateway/session_active_list.py:3-5`). **[suy luận]** Không thấy một màn "htop" tổng hợp tất cả agent kiểu bảng live; gần nhất là sidebar Desktop + roster subagent của TUI.

---

## 6. Dữ liệu bền vững cho quan sát (SQLite `state.db`)

DDL ở `hermes_state_common.py:399-680`.

- **`sessions`** (:408): `id, source, model, parent_session_id, started_at, ended_at, end_reason, message_count, tool_call_count, input/output/cache_read/cache_write/reasoning_tokens, api_call_count, estimated_cost_usd, actual_cost_usd, cost_status, cost_source, pricing_version, billing_provider/base_url/mode, cwd, git_branch, title, last_activity_at, last_activity_description, last_activity_provenance, profile_name, archived/pinned/hidden, tool_names`.
- **`messages`** (:477): `session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp, token_count, finish_reason, reasoning*, display_kind, display_metadata, display_order, message_uid, active, compacted`.
- **`session_model_usage`** (:510): khóa `(session_id, model, billing_provider, base_url, mode, task)` → token, `api_call_count`, `estimated/actual_cost_usd`, `first_seen/last_seen`.
- Khác: `session_admissions` (hàng đợi input bền), `worker_executions`, `gateway_heartbeats`, `session_turn_leases`, `async_delegations`.

Module liên quan:
- `hermes_state_usage.py:82 SessionUsageMixin` — **writer token chạy nền gộp batch** (`queue_token_counts`), CLI cộng delta / gateway ghi giá trị tích lũy (`_token_update_sql`, :20-47).
- `agent/activity_tracking.py:41 _touch_activity` — nhịp liveness: cập nhật `last_activity_ts/desc/provenance` trong RAM, ghi bền **rate-limited** vào `sessions.last_activity_*` (:91-127); watchdog dùng nó để phát hiện stall; bridge heartbeat cho worker kanban.
- `hermes_state_timeline.py` — index prompt + nhảy transcript có giới hạn (chỉ đọc, không hydrate toàn bộ), phục vụ `/api/sessions/{id}/timeline`.
- `agent/insights.py:93 InsightsEngine` — phân tích lịch sử từ `sessions`/`messages` (cost, model, tool, giờ trong ngày, bar chart text); lệnh `hermes insights` (`hermes_cli/subcommands/insights.py`).
- `hermes_logging.py:1-6` — file log xoay vòng: `agent.log` (INFO+), `errors.log` (WARNING+), `gateway.log`, `gui.log`; qua một queue async, `RedactingFormatter` che secret.
- `session.events.stats` — telemetry của ring replay (`tui_gateway/methods_session.py:2344`).

---

## 7. Áp dụng cho agent-hub

### 7.1 Bài học rút ra (từ những gì đã đọc)
1. **Tách tuyệt đối "core sở hữu công việc" và "UI chỉ là viewer"** — agent chạy trong daemon; UI nào cũng chỉ attach/detach. Đóng UI không giết agent.
2. **Một wire duy nhất, khai báo có schema, sinh type cho client** (Pydantic → TS/OpenRPC). Cùng một luồng event phục vụ CLI, TUI, web, desktop.
3. **Event có `seq` + `epoch` + ring replay** → viewer attach muộn/reconnect vẫn bắt kịp; `truncated` → fallback đọc DB.
4. **Fanout có backpressure theo từng viewer** — viewer chậm bị cắt, agent không bao giờ chờ UI.
5. **Event stream (nóng) ≠ store bền (lạnh)**: stream cho live view, SQLite cho lịch sử/chi phí/insights; usage ghi theo batch nền.
6. **Câu hỏi ngược (approval/clarify) là request có id**, được replay khi attach (`open_requests`) để viewer mới thấy agent đang chờ gì.

### 7.2 Đề xuất event bus

```
 agent worker (mỗi agent 1 process/task)
   └─ adapter callback → AgentEvent ──► Hub daemon (event bus)
                                         ├─ ring buffer theo agent_id (seq, epoch)  → live attach/replay
                                         ├─ append-only log (SQLite/JSONL)          → lịch sử, audit
                                         ├─ aggregator (usage, cost, status)        → bảng "htop"
                                         └─ fanout per-subscriber (bounded queue)   → CLI/TUI/Web
```
Envelope đề xuất (rút gọn từ hermes — **[đề xuất]**):
```json
{"v":1,"agent_id":"a1","session_id":"s9","turn_id":"t3","seq":1042,"epoch":"e7f…",
 "ts":"2026-10-18T09:00:00Z","type":"tool.start","payload":{…}}
```
Bộ `type` tối thiểu (map 1-1 với hermes): `agent.status` (idle/working/waiting/error — như `session.active_list`), `turn.start`, `message.delta`, `reasoning.delta`, `tool.generating`, `tool.start`, `tool.complete` (kèm `duration_s`, `summary`), `todo.updated`, `usage.tick` (tokens, cost_usd, context_percent, tps), `turn.complete` (status, usage cuối), `request.open`/`request.cancel` (approval/clarify), `subagent.spawn|status`, `log` (agent.print), `error`.

API của hub:
- `agents.list` → snapshot bảng (id, tên, status, task hiện tại, last tool, elapsed, tokens, $).
- `agents.subscribe(agent_id, since_seq?)` → snapshot + stream; `agents.detach`.
- `agents.events.since(agent_id, seq)` → replay; `truncated` → client đọc log.
- `agents.steer|interrupt|approve` (điều khiển, tách quyền read/control như capability của hermes).
- Kênh "firehose" `/events?all` cho màn tổng quan (chỉ gửi event tóm tắt: status, tool.start/complete, usage.tick — **không** gửi delta token để nhẹ).

### 7.3 Giao diện "htop cho agent"

```
┌ agent-hub ─ 5 agents ─ $3.42 hôm nay ─ 1.2M tok ────────────────────────────────┐
│ ID  NAME        STATUS    TASK                     LAST TOOL         T   TOK   $  │
│ a1  researcher  ● working  "tóm tắt repo hermes"    read_file (2.1s)  4m  210k .61│
│ a2  coder       ◐ waiting  approval: rm -rf build   terminal          9m  88k  .20│
│ a3  reviewer    ○ idle     —                        —                 —   12k  .03│
└ ↑↓ chọn · Enter attach · a approve · s steer · x stop · / lọc · q thoát ─────────┘
```
Enter → **chế độ attach** giống Claude Code/hermes CLI: transcript streaming, khối reasoning thu gọn, dòng tool `┊ ⚡ read_file src/x.py (2.1s)`, diff inline, thanh trạng thái dưới (model · context % · tok/s · $). Esc quay lại bảng — agent vẫn chạy (attach/detach như hermes).

### 7.4 Stack khuyến nghị **[đề xuất, không phải từ repo]**
- **Hub daemon**: Python (asyncio + FastAPI/Starlette WebSocket) nếu agent viết bằng Python — dùng Pydantic định nghĩa event, sinh JSON Schema/TS như hermes. Lưu trữ: SQLite (WAL) cho sessions/usage + JSONL/bảng `events` append-only; ring buffer in-memory cho live.
- **Wire**: JSON-RPC 2.0 qua WebSocket (local: Unix socket) — cùng mô hình hermes, đã chứng minh chạy được cho cả TUI và web.
- **TUI "htop"**: 2 lựa chọn
  - **Ink (React) + nanostores** — giống hermes, tái dùng type TS sinh ra, và **có thể nhúng lại vào web qua xterm.js + PTY** như dashboard hermes (một UI, hai nơi hiển thị).
  - **Textual (Python)** — nếu muốn một ngôn ngữ duy nhất; Textual có DataTable + Log widget hợp cho bảng htop + stream; chạy web được qua `textual serve`.
- **Web dashboard** (giai đoạn 2): React + Vite + react-query, nhận cùng event stream; tận dụng pattern "embed TUI qua xterm.js" để không phải viết chat UI hai lần.
- **Tracing ngoài** (tuỳ chọn): xuất OpenTelemetry/Langfuse từ hook `pre/post_tool_call`, `pre/post_llm_call` giống hệ plugin của hermes.

### 7.5 Lưu ý rủi ro (rút từ comment trong code hermes)
- Không để UI chậm chặn agent: queue có giới hạn + drop/cắt subscriber (`event_publisher.py`, `FanoutTransport`).
- `seq` reset khi restart → luôn kèm `epoch`.
- Delta token rất nhiều (comment: "a long turn emits ~hundreds of token events", `event_replay.py:21`) → ring theo cả số event lẫn byte.
- Usage giữa turn chỉ là "tick"; số chuẩn lấy ở `turn.complete` (giống `session.usage` vs `message.complete`).
