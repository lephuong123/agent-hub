# Báo cáo spike M0 — kiểm chứng `claude` CLI làm runtime cho agent-hub

| Mục | Giá trị |
|---|---|
| Ngày chạy | 2026-10-18 |
| CLI | `claude` 2.1.302, đăng nhập OAuth `claude.ai` (subscription team), `apiKeySource: "none"` |
| Model | `--model haiku` (→ `claude-haiku-5-5`), `--max-budget-usd 0.3` mỗi lần |
| Thư mục chạy | `docs/spikes/m0/work/` (scratch; chỉ có `sandbox-dummy/keep.txt` làm mục tiêu cho lệnh "nguy hiểm") |
| Script + fixture | `docs/spikes/m0/` (xem mục 8) |
| **Tổng chi phí thực** | **≈ $0.19** (38 session, `python3 m0/cost_total.py`). Riêng 1 lần chạy baseline KHÔNG cô lập đã tốn $0.17 — xem phát hiện F-1 |

Không sửa `~/.claude/*`, không chạy `claude auth login/logout`, không xoá gì ngoài `work/`. Thao tác "phá huỷ" duy nhất được thử là `rm -rf sandbox-dummy` và nó **luôn bị chặn** (thư mục vẫn còn sau mọi lần thử).

## 0. Kết luận nhanh

| Giả định | Kết luận | Một dòng |
|---|---|---|
| A-01 phê duyệt động trong `-p` | **Xác nhận** (cả 2 cách) | Hook `PreToolUse` (allow/deny/ask) và `--permission-prompt-tool` (cờ ẩn nhưng chạy được) đều chặn thật; nên dùng **hook làm bộ phân loại + permission tool làm kênh "ask"** |
| A-02 steer qua stdin | **Xác nhận** | Tin gửi khi tool đang chạy được chèn vào **ngay sau tool_result của lượt hiện tại** (cùng 1 `result`), không phải xếp sau lượt |
| A-03 ngắt rồi `--resume` | **Xác nhận** | SIGINT → `result` `error_during_execution`/`aborted_tools`, exit 0; SIGTERM → không có `result`, exit 143; cả hai resume tốt. Có thêm **interrupt mềm qua stdin** giữ tiến trình sống |
| A-04 cô lập hooks/settings | **Xác nhận** | `--setting-sources ""` (chuỗi rỗng) + `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` → không còn SessionStart Serena, 0 MCP người dùng (kể cả `claude.ai *`), OAuth vẫn chạy |
| A-05 không nạp CLAUDE.md/auto-memory | **Xác nhận (một phần cho CLAUDE.md cấp user)** | CLAUDE.md thư mục cha: không nạp khi `--setting-sources ""`; auto-memory: tắt bằng `{"autoMemoryEnabled": false}`. Máy hiện **không có** `~/.claude/CLAUDE.md` nên không kiểm trực tiếp được nhánh user |
| A-13 giữ chờ hook/permission tool | **Xác nhận cho 150 s; có timeout, phải cấu hình** | Hook giữ 150 s không sao. Timeout hook = trường `timeout` (giây); **hết giờ → fail-OPEN** (rơi về luật tĩnh). Permission tool chịu `MCP_TOOL_TIMEOUT` (ms); hết giờ → tool lỗi (không chạy) |

## 1. A-01 — Phê duyệt động trong `-p`

### 1.1 Cách (b): hook `PreToolUse` trong `--settings`

File settings (`m0/settings/hub-hooks.json`):

```json
{
  "autoMemoryEnabled": false,
  "hooks": {
    "PreToolUse": [
      {"matcher": "*", "hooks": [{"type": "command",
        "command": "python3 /Users/phuonglq/Workspace/agent-hub/docs/spikes/m0/hooks/pretool.py"}]}
    ]
  }
}
```

Lệnh (qua `m0/run_iso.sh`, mọi lần chạy đều có bộ cờ cô lập ở mục 4):

```bash
claude --model haiku --max-budget-usd 0.3 --output-format stream-json --verbose --include-hook-events \
  --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --settings m0/settings/hub-hooks.json --tools Bash \
  -p 'Use the Bash tool to run each of these commands as a SEPARATE Bash call ... (1) echo hello-spike (2) touch hooked.txt (3) rm -rf sandbox-dummy (4) ls ...' < /dev/null
```

**JSON hook nhận trên stdin** (`fixtures/hook-inputs.jsonl`):

```json
{"session_id":"5d076484-…","transcript_path":"/Users/phuonglq/.claude/projects/-Users-…-m0-work/5d076484-….jsonl",
 "cwd":".../m0/work","prompt_id":"3f99fcdb-…","permission_mode":"default","effort":{"level":"medium"},
 "hook_event_name":"PreToolUse","tool_name":"Bash",
 "tool_input":{"command":"rm -rf sandbox-dummy","description":"Remove sandbox-dummy directory recursively"},
 "tool_use_id":"toolu_014zXnYva4JiNv4N2Yj9zAoQ"}
```

Với tool MCP có thêm `"tool_name":"mcp__hubperm__approve"` và `"mcp_server":{"name":"hubperm","source":"dynamic"}` (hook `matcher: "*"` bắt cả tool MCP — `fixtures/x-hook-on-mcp-tool.jsonl`).

**JSON output được chấp nhận** (stdout, exit 0):

```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",
  "permissionDecisionReason":"hub guard: destructive operation (rm -rf) is blocked pending human approval"}}
```

`permissionDecision` ∈ `allow | deny | ask`. Quan sát (`fixtures/a01b-hook-allow-deny.jsonl`):

```
ASSISTANT tool_use: Bash {"command": "rm -rf sandbox-dummy", ...}
HOOK hook_response PreToolUse:Bash {... "permissionDecision": "deny" ...}
USER tool_result is_error=True: PreToolUse:Bash hook error: hub guard: destructive operation (rm -rf) is blocked pending human approval
ASSISTANT tool_use: Bash {"command": "ls"}            ← agent đi tiếp, không thử lách
RESULT subtype=success ... permission_denials=[{tool_name: Bash, tool_use_id: toolu_014z…, tool_input: {command: "rm -rf sandbox-dummy"}}]
```

- `deny` **chặn thật** (`sandbox-dummy/` còn nguyên). Lý do deny đến tay model dưới dạng `tool_result is_error=true`; agent báo lại cho người và làm tiếp phần khác. `result.permission_denials[]` liệt kê tool bị chặn → hub dùng cho Signal `reject`.
- `allow` từ hook **vượt qua hỏi quyền mặc định** (lệnh ghi `touch hooked.txt` chạy được trong `permissionMode: default` mà không cần `--allowedTools`).
- `allow` từ hook **KHÔNG vượt được luật deny tĩnh**: hook trả allow nhưng `--disallowedTools "Bash(rm -rf:*)"` vẫn chặn (`fixtures/x-hookallow-vs-disallowed.jsonl`). ⇒ khi người đã duyệt một thao tác phá huỷ, hub phải resume với argv **không** chứa mẫu deny trùng thao tác đó.
- `ask` (`fixtures/a01-ask-*.jsonl`):
  - không có permission tool (`--permission-prompts host` mặc định) → bị từ chối: event mới `{"type":"system","subtype":"permission_denied","decision_reason_type":"hook","message":"hub: needs confirmation"}`.
  - `--permission-prompts none` → từ chối với thông điệp dài "…this session has no approval surface… do not retry it: … anything else that requires approval, will be denied the same way for the rest of this session" (`decision_reason_type: "asyncAgent"`).
  - **có `--permission-prompt-tool` → `ask` được chuyển cho permission tool** và tool trả allow thì lệnh chạy (`a01-ask-permtool.jsonl`).
- Mã thoát / lỗi hook (`fixtures/x-hook-crash.jsonl`, `x-hook-exit2.jsonl`, với `--allowedTools "Bash(touch:*)"`):
  - hook **exit 2** + stderr → chặn (`tool_result`: `PreToolUse:Bash hook error: [python3 …]: hub guard (exit2): blocked`), có trong `permission_denials`.
  - hook **exit 1 / crash** → **fail-OPEN**: lệnh vẫn chạy (`MODE-crash.txt` được tạo). ⇒ hub phải tự bọc fail-closed (mục 7).

### 1.2 Cách (a): `--permission-prompt-tool <mcp tool>`

Cờ không có trong `--help` nhưng **được chấp nhận** ở 2.1.302. Server mẫu: `m0/mcp/hubperm.py` (FastMCP, `uv run --no-project --with 'mcp<2'`), cấu hình `m0/mcp/hubperm.mcp.json`.

```bash
MCP_CFG=m0/mcp/hubperm.mcp.json ./run_iso.sh a01a-permtool-allow-deny \
  --settings m0/settings/hub-base.json --tools Bash --permission-prompt-tool mcp__hubperm__approve -p '<4 lệnh như trên>'
```

**Đối số tool nhận** (`fixtures/permtool-inputs.jsonl`): `{"tool_name":"Bash","input":{"command":"rm -rf sandbox-dummy","description":"…"},"tool_use_id":"toolu_015M…"}`.
**Kết quả được chấp nhận**: đúng **một** text block chứa JSON
`{"behavior":"allow","updatedInput":{…}}` hoặc `{"behavior":"deny","message":"…"}`.

Quan sát:
- deny chặn thật; `tool_result is_error=true` = `message` của hub; có trong `permission_denials`.
- Permission tool **chỉ được gọi khi CLI định hỏi quyền**: `echo`, `ls` (CLI tự coi là chỉ đọc) chạy thẳng, không qua tool; `touch`/`rm -rf` mới qua tool. Cũng không được gọi nếu `--allowedTools`/`acceptEdits` đã cho phép. ⇒ **không dùng được làm bộ phân loại "mọi tool call"** (FR-SG-03 đòi điều này) — hook làm được.
- Tool được **ẩn khỏi model** tự động: `system/init.tools` chỉ còn `["Bash"]` khi có `--permission-prompt-tool` (so với `["Bash","mcp__hubperm__approve"]` khi không có — `x-tools-filter-mcp*.jsonl`).
- Bẫy gặp phải (fixture giữ lại để test):
  - FastMCP v1 mặc định trả kèm `structuredContent` → CLI báo `Permission prompt tool returned an invalid result. Expected a single text block param with type="text" and a string text value.` và **không chạy** tool (fail-closed). Sửa: `@mcp.tool(structured_output=False)` (`a01a-permtool-invalid-result.jsonl`).
  - Server MCP không lên (mcp 2.x đổi API) → `Error: MCP tool mcp__hubperm__approve (passed via --permission-prompt-tool) not found`, **tiến trình thoát exit 1 và không phát `result`** (`a01a-permtool-mcp-failed.jsonl`). Hub phải coi "EOF không có result" là lỗi runtime.

### 1.3 Không có hook/tool (đối chứng)

`touch plain-2.txt` trong `permissionMode: default` bị từ chối: `permission_denied` — "touch in '…/work/plain-2.txt' needs approval. The path is inside the working directories…" (`a01-nohook-write-hostdefault.jsonl`). `--disallowedTools "Bash(rm -rf:*)"` chặn tĩnh kể cả với `--permission-mode acceptEdits --allowedTools Bash` (`decision_reason_type: "subcommandResults"` → CLI tách lệnh ghép) (`x-disallowed-static.jsonl`). Managed settings của tổ chức (deny `Bash(aws:*)`, ask cho `CF_DB_PROD`) **vẫn áp dụng** dù `--setting-sources ""` (thấy trong debug log).

### 1.4 Khuyến nghị cho hub (FR-RT-09, FR-SG-03/04)

1. **Bộ phân loại = hook `PreToolUse` `matcher:"*"`**, lệnh `agent-hub hook pre-tool` nói chuyện với daemon qua socket. Trả `allow` cho safe/write (kèm audit), `deny` cho thao tác bị cấm hẳn, `ask` cho destructive/uncertain.
2. **Kênh phê duyệt người = `--permission-prompt-tool mcp__hub__approve`** (tool trong hub-self MCP, `structured_output=False`): nhận các `ask` từ hook, mở `ApprovalRequest`, chờ người, trả `allow`/`deny`. Lợi: dùng chung đường ống của CLI (`permission_denials`, `system/permission_denied`), tool ẩn khỏi model.
3. Lớp tĩnh `--disallowedTools` cho mẫu luôn-phá-huỷ vẫn giữ (fail-closed khi hook không chạy), nhưng nhớ: thao tác đã được người duyệt **phải được resume với argv bỏ mẫu deny tương ứng** (hook allow không thắng deny tĩnh).
4. Không bao giờ để `--permission-prompts none` cho run làm việc (nó từ chối và dặn model đừng thử lại suốt phiên). Chỉ dùng cho review/eval như BRD.

## 2. A-13 — Giữ chờ quyết định lâu

Script: `m0/a13_hold.sh 150` (3 biến thể chạy song song, lệnh `touch SLOWAPPROVE-<v>.txt`).

| Biến thể | Cấu hình | Giữ | Kết quả |
|---|---|---|---|
| hook, không khai `timeout` | `hub-hooks.json` | 150 s | allow sau 150 s, lệnh chạy, `elapsed=154s`, exit 0 |
| hook `"timeout": 900` | `hub-hooks-timeout900.json` | 150 s | như trên |
| permission tool | `hubperm`, `MCP_TOOL_TIMEOUT` mặc định | 150 s | allow sau 150 s, lệnh chạy |
| hook `"timeout": 5` | `hub-hooks-timeout5.json`, `HOOK_SLEEP=20` | 5 s | hook bị **giết** (không kịp ghi log), `hook_response` rỗng, **quyết định rơi về luật thường**: lệnh ghi bị từ chối do không có người hỏi (`a13-hook-timeout5.jsonl`) |
| hook `"timeout": 5` + `--allowedTools "Bash(touch:*)"` | | 5 s | **lệnh CHẠY** — hết giờ hook = fail-OPEN (`a13-hook-timeout5-allowedtools.jsonl`) |
| permission tool + `MCP_TOOL_TIMEOUT=5000` | | 5 s | `tool_result`: `MCP server "hubperm" tool "approve" timed out after 5s`, lệnh **không** chạy (fail-closed) |

Kết luận: **Xác nhận cho 2–3 phút**; CLI có timeout và nó cấu hình được: hook dùng trường `timeout` (giây) trên từng hook; permission tool dùng biến môi trường `MCP_TOOL_TIMEOUT` (ms). Chưa đo mặc định chính xác của hai giá trị này (150 s chưa chạm), cũng chưa thử giữ 10 phút–giờ (để M1).

Khuyến nghị:
- Không dựa vào việc giữ hook nhiều giờ. Đặt `timeout` hook lớn (vd. 900 s) **và** cho chính hook tự trả `deny` (fail-closed) ở `approval.timeout_s` = 300 s với lý do "đang chờ người duyệt (waiting_human)", rồi hub dừng tiến trình (SIGINT), giữ session + lease, chuyển `waiting_human`; khi người cho phép → `--resume <sid>` + vé một lần (đúng phương án dự phòng A-13 trong BRD).
- Nếu dùng permission tool làm kênh chờ: đặt `MCP_TOOL_TIMEOUT` > 300 000 trong env tiến trình; quá thời gian → tool tự trả deny có thông điệp chờ.

## 3. A-02 — Steer qua stdin stream-json

Script: `m0/a02_steer.py midtool|twoturns`. argv:

```bash
claude -p --model haiku --max-budget-usd 0.3 --input-format stream-json --output-format stream-json --verbose \
  --replay-user-messages --include-hook-events --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --settings m0/settings/hub-hooks.json --tools Bash --session-id <uuid>
```

Định dạng dòng input đúng (mỗi dòng một JSON, stdin giữ mở):

```json
{"type":"user","message":{"role":"user","content":"STEER from operator: ..."}}
```

Dòng thời gian `midtool` (`fixtures/a02-steer-midtool.timeline.txt`):

```
  0.00s >> sent MSG1
  1.57s user replay/text: Use the Bash tool to run exactly: sleep 15 ...   isReplay=True
  1.57s assistant tool_use: Bash "sleep 15 && echo first-done"
  2.57s >> sent MSG2 (steer)                                   ← lúc tool đang chạy
  4.67s system/task_started ...
 16.69s user tool_result: first-done
 16.70s user replay/text: STEER from operator: ...            isReplay=True  ← ack ĐÚNG lúc được tiêu thụ
 17.93s assistant tool_use: Bash "echo steer-received"
 18.76s assistant text: ... PINEAPPLE
 18.78s result/success turns=3   (một result duy nhất)
```

Kết luận: **Xác nhận**. Tin steer được chèn vào ở ranh giới gọi model kế tiếp (sau `tool_result` đang chờ), **trong cùng một lượt** → một `result`. Bản phát lại `isReplay: true` xuất hiện ở thời điểm tin được đưa vào ngữ cảnh, dùng làm `steer.ack` rất chuẩn. Đối chứng `twoturns`: gửi sau `result` → CLI phát lại `system/init` rồi chạy lượt mới, ra `result` thứ hai; tiến trình chỉ thoát khi đóng stdin.

Khuyến nghị: mọi run spawn với `--input-format stream-json --replay-user-messages`, stdin giữ mở; prompt đầu cũng gửi qua stdin; đóng stdin khi muốn kết thúc sau `result`. Hub phải xử lý **nhiều `result`** trên một tiến trình.

## 4. A-04 / A-05 — Cô lập cấu hình

Script: `m0/a04_a05_isolation.sh` (`SKIP_BASELINE=1` để khỏi tốn $0.17).

| Chạy | Hooks | `mcp_servers` | plugins / skills | CLAUDE.md cha (`ZEBRA-7781`) | `memory_paths` | Chi phí "Reply OK" |
|---|---|---|---|---|---|---|
| Mặc định | `SessionStart:startup` (Serena) | 14 user + ~48 `claude.ai *` (295 tool) | `cf-hubspot` + 4 builtin; 35 skill (gồm `anthropic-skills:*`) | **nạp** | `~/.claude/projects/<slug>/memory/` | **$0.1735** |
| `--setting-sources ""` + `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` | **không có** | **[]** (cả `claude.ai *` cũng mất) | 4 builtin; 19 skill builtin | **không nạp** (`NONE`) | vẫn còn | $0.0006 |
| `--setting-sources project` | không có | [] | | **nạp** | vẫn còn | |
| + `--settings '{"autoMemoryEnabled":false}'` hoặc env `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | | | | | **`None`** | |

Ghi chú:
- `--setting-sources ""` (chuỗi rỗng) được chấp nhận. OAuth vẫn dùng (`apiKeySource: "none"`, chạy thành công), **không cần `--bare`**.
- `--setting-sources project` vẫn nạp CLAUDE.md của mọi thư mục cha → nếu `HUB_HOME` nằm dưới thư mục có CLAUDE.md thì bị nhiễm. Dùng chuỗi rỗng an toàn hơn; hook/permission của hub đưa qua `--settings <file>` (vẫn có hiệu lực khi sources rỗng — đã thấy hook PreToolUse của hub chạy).
- Debug log xác nhận: ở chế độ cô lập không đọc `userSettings` (mất 4 rule allow `claude-in-chrome`), không có sự kiện `InstructionsLoaded`; **managed/policy settings của tổ chức vẫn áp dụng** (không tắt được, cũng không nên).
- Plugin builtin (`cc-plugin-sec-default`, `agents-md`, `telemetry`, `plugin-authoring`) và 19 skill builtin vẫn còn — đây là phần của CLI, không phải cấu hình người dùng. Nếu muốn tắt skill: `--disable-slash-commands` (chưa thử).
- `~/.claude/CLAUDE.md` **không tồn tại** trên máy nên không có chuỗi đánh dấu cấp user để hỏi; theo cơ chế quan sát được (CLAUDE.md gắn với setting source, `""` loại cả user lẫn project) khả năng cao cũng bị loại → kết luận "Một phần" cho nhánh này; kiểm lại khi người dùng có file đó.
- Transcript của mọi phiên vẫn ghi vào `~/.claude/projects/<slug-cwd>/<session>.jsonl` (đường dẫn có trong input hook `transcript_path`).

**Bộ cờ cô lập khuyến nghị**: `--setting-sources "" --settings <HUB>/agents/<id>/runtime/settings.json` (chứa `autoMemoryEnabled:false` + hooks của hub) `--strict-mcp-config --mcp-config <HUB>/agents/<id>/runtime/mcp.json`, cwd = workspace agent, stdin luôn mở/`</dev/null`. Kiểm hậu nghiệm từ `system/init`: `mcp_servers` ⊆ khai báo, `memory_paths == null`, `plugins` ⊆ builtin, không có `hook_started` ngoài hook của hub.

## 5. A-03 — Ngắt giữa lượt rồi `--resume`

Script: `m0/a03_interrupt.py sigint|sigterm|control`. Lệnh dài chạy foreground: `python3 -c "import time; time.sleep(30); print(1)" && echo late-done`; tín hiệu gửi 3 s sau `tool_use`. Pha 2: `claude -p '<hỏi lại lệnh trước>' --resume <sid>` + bộ cờ cô lập.

| Cách ngắt | Thoát sau | Exit | Dòng cuối stream | Tiến trình con | Resume |
|---|---|---|---|---|---|
| SIGINT tới pid `claude` | 1.06 s | 0 | `user` "[Request interrupted by user for tool use]" + `result {"subtype":"error_during_execution","is_error":true,"terminal_reason":"aborted_tools","stop_reason":"tool_use","errors":["[ede_diagnostic] …"]}` | đã chết | OK — agent nhớ đúng lệnh, biết bị ngắt |
| SIGTERM | 1.17 s | 143 | `tool_result "Exit code 137"`, **không có `result`** | đã chết | OK — agent nói không nhận được output |
| `{"type":"control_request","request_id":"int-1","request":{"subtype":"interrupt"}}` qua stdin | 0 s | (tiến trình sống tới khi đóng stdin) | `control_response {"subtype":"success","request_id":"int-1","response":{"still_queued":[]}}` + `result` giống SIGINT | đã chết | OK |

Kết luận: **Xác nhận** — transcript không hỏng, `--resume <sid>` giữ nguyên `session_id`. Ngắt mềm qua `control_request interrupt` là cách tốt nhất cho run stream-json: lượt dừng, tiến trình vẫn sống để nhận steer/tin tiếp theo (không phải spawn lại, giữ cache).

Lưu ý thêm:
- Lần đầu dùng `sleep 30 && echo …`, CLI **tự chặn** lệnh sleep foreground ("Blocked: sleep 30 followed by … use Monitor with an until-loop") và model chuyển sang chạy nền (`run_in_background`); lượt kết thúc trước khi tín hiệu tới, task nền bị dừng khi tiến trình thoát (`task_notification status: "stopped"`). Fixture giữ ở `fixtures/a03-sleep-blocked/`. Hub cần biết: **task nền của Bash chết theo tiến trình `-p`**.
- Khi resume một phiên có task nền dở dang, CLI phát thêm một cặp `system/init` + `result` rỗng (`num_turns: 0`) trước lượt thật.
- `total_cost_usd` và `modelUsage` trong `result` sau `--resume` là **luỹ kế theo session** (pha 2 lần đầu báo đúng $0.00072257 của pha 1), còn `usage` là của lượt. Hub phải lấy **chênh lệch** khi ghi chi phí run.

Khuyến nghị `interrupt/stop` (FR-RT-07): `control_request interrupt` → chờ `result` → nếu cần dừng hẳn: đóng stdin → SIGINT → `grace_s` → SIGTERM → SIGKILL cả process group (spawn với `start_new_session=True`). Run thiếu `result` (SIGTERM/crash) → `outcome=cancelled|crashed`, vẫn resume được.

## 6. Phát hiện khác có ích cho hub

- **F-1 Chi phí nhiễm cấu hình**: chạy không cô lập, "Reply OK" tốn **$0.17** (nạp ~295 tool MCP vào cache) so với **$0.0006** khi cô lập — gần 300×. Cô lập là bắt buộc, không chỉ vì an toàn.
- **`CLAUDE_CONFIG_DIR`**: chỉ thử `claude auth status` (chỉ đọc). Với thư mục cấu hình mới → `"loggedIn": false` (khoá keychain gắn theo thư mục cấu hình) → **làm mất đăng nhập OAuth** cho tiến trình đó. Không dùng để cô lập; nếu cần tách hẳn (A-09) phải kèm token dài hạn (`claude setup-token` + `CLAUDE_CODE_OAUTH_TOKEN`) — chưa thử vì thay đổi trạng thái đăng nhập.
- **`--max-budget-usd` khi vượt** (`fixtures/x-budget-exceeded.jsonl`, budget 0.0005): kiểm sau mỗi lần gọi API (vượt nhẹ $0.00051), `result {"subtype":"error_max_budget_usd","is_error":true,"terminal_reason":"budget_exhausted","errors":["Reached maximum budget ($0.0005)"],"result":null}`, **exit 1**. Ngân sách tính theo **chi tiêu của tiến trình hiện tại**, không theo `total_cost_usd` luỹ kế: resume phiên đã tốn $0.00041 với `--max-budget-usd 0.0003` vẫn chạy bình thường.
- **Event mới cần thêm vào bảng ánh xạ FR-RT-04**: `system/permission_denied` (`tool_name, tool_use_id, decision_reason_type: hook|asyncAgent|subcommandResults, message`), `system/task_started|task_notification|task_updated|background_tasks_changed` (Bash chạy như task), `system/commands_changed`, `control_response`, `user` có `isReplay: true`. `result.terminal_reason` có các giá trị đã thấy: `completed`, `aborted_tools`, `budget_exhausted`.
- **`result` khi bị chặn**: vẫn `subtype: success`, `is_error: false`; thông tin chặn nằm ở `permission_denials[]` (tool_name, tool_use_id, tool_input).
- **`system/init`** có `memory_paths`, `plugins`, `skills`, `capabilities` (vd. `interrupt_receipt_v1`) — dùng cho kiểm cô lập và ma trận tương thích (FR-RT-15).
- `-p` không có stdin sẽ chờ 3 s rồi in cảnh báo "no stdin data received in 3s" → luôn `< /dev/null` hoặc dùng stream-json input.
- `--debug-file <path>` tạo thêm symlink `latest` cạnh file log.

## 7. argv khuyến nghị cho `ClaudeCliRuntime`

```bash
cd <HUB_HOME>/agents/<id>/workspace          # cwd do hub kiểm soát
claude -p \
  --input-format stream-json --output-format stream-json --verbose \
  --replay-user-messages --include-partial-messages --include-hook-events \
  --session-id <uuid>                        # hoặc --resume <sid> [--fork-session]
  --model <model> [--effort ...] [--fallback-model ...] \
  --setting-sources "" \
  --settings <HUB_HOME>/agents/<id>/runtime/settings.json \
  --strict-mcp-config --mcp-config <HUB_HOME>/agents/<id>/runtime/mcp.json \
  --permission-prompt-tool mcp__hub__approve \
  --permission-mode acceptEdits --allowedTools ... --disallowedTools "Bash(rm -rf:*)" "Bash(git push --force:*)" ... \
  --append-system-prompt "<persona+memory+skill index>" --system-prompt-snapshot on \
  --max-budget-usd <n>
# env: MCP_TOOL_TIMEOUT=900000 ; spawn new process group ; prompt đầu và steer gửi qua stdin:
#   {"type":"user","message":{"role":"user","content":"..."}}
# interrupt: {"type":"control_request","request_id":"<id>","request":{"subtype":"interrupt"}}
```

`runtime/settings.json` mẫu (hub sinh):

```json
{
  "autoMemoryEnabled": false,
  "hooks": {
    "PreToolUse": [
      {"matcher": "*", "hooks": [{"type": "command", "command": "agent-hub hook pre-tool", "timeout": 900}]}
    ],
    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "agent-hub hook post-tool", "timeout": 10}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "agent-hub hook stop", "timeout": 10}]}]
  }
}
```

`mcp.json` luôn có `hub` (hub-self, chứa tool `approve` với `structured_output=False`) + server của agent.

Hook `agent-hub hook pre-tool` phải **fail-closed tự thân**: mọi exception/timeout nội bộ → in `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"hub guard unavailable"}}` hoặc `exit 2`; tự hết giờ trước `timeout` của CLI (vì CLI hết giờ hoặc hook exit 1 = fail-OPEN). Quyết định: safe/write → `allow`; luôn-cấm → `deny`; destructive/uncertain → `ask` (chuyển sang `mcp__hub__approve`, chờ ≤ 300 s, sau đó `deny` + "waiting_human", hub tạm dừng & resume khi có người).

Hub đọc stream: coi EOF không có `result` là lỗi runtime; chấp nhận nhiều `result`/tiến trình; chi phí run = `total_cost_usd` − giá trị luỹ kế trước đó của session; kiểm cô lập từ `system/init` (`mcp_servers`, `memory_paths == null`, `plugins`, không `hook_started` lạ).

## 8. Script và fixture (`docs/spikes/m0/`)

| File | Nội dung |
|---|---|
| `run_iso.sh <name> [args]` | Chạy `claude -p` với bộ cờ cô lập, ghi `fixtures/<name>.jsonl`, in tóm tắt. Env: `MCP_CFG`, `BUDGET`, `HOOK_MODE`, `HOOK_SLEEP`, `PERM_SLEEP` |
| `summarize.py <jsonl>` | Tóm tắt stream-json, ghi chi phí vào `costs.tsv` |
| `cost_total.py` | Tổng chi phí thực (max `total_cost_usd` theo session) |
| `a01_approval.sh` | Toàn bộ biến thể A-01 (hook, permission tool, ask, none, crash, exit2, disallowed tĩnh, hook trên tool MCP) |
| `a13_hold.sh [giây]` | A-13 giữ 150 s (3 biến thể song song) |
| `a02_steer.py midtool\|twoturns` | A-02 |
| `a03_interrupt.py sigint\|sigterm\|control` | A-03 |
| `a04_a05_isolation.sh` | A-04/A-05 (`SKIP_BASELINE=1` để tiết kiệm $0.17) |
| `hooks/pretool.py` | Hook PreToolUse mẫu (allow/deny/ask/sleep/crash/exit2) |
| `mcp/hubperm.py`, `mcp/hubperm.mcp.json` | MCP permission-prompt-tool mẫu |
| `settings/hub-hooks*.json`, `hub-base.json` | Settings mẫu (timeout mặc định / 900 / 5) |
| `fixtures/*.jsonl` | Output stream-json thật — dùng cho contract test `FakeRuntime`/FR-RT-04: hook allow/deny (`a01b-*`), permission tool (`a01a-*`, gồm ca lỗi), `permission_denied` (`a01-ask-*`), steer (`a02-*` + `.timeline.txt`), ngắt + resume (`a03-*`), cô lập (`a04-*`, `a05-*`), giữ chờ (`a13-*`), budget (`x-budget-*`) ; `hook-inputs.jsonl`, `permtool-inputs.jsonl` = JSON thật mà hook/tool nhận |

Chưa kiểm (để M1+): giữ hook 10 phút–giờ; mặc định chính xác của hook `timeout` và `MCP_TOOL_TIMEOUT`; `~/.claude/CLAUDE.md` cấp user (máy không có); `--disable-slash-commands`; nhiều tiến trình song song (A-09).
