# Báo cáo spike — Codex CLI (gói ChatGPT) làm runtime cho cfharness

| Mục | Giá trị |
|---|---|
| Ngày chạy | 2026-11-25 |
| CLI | `codex-cli` **0.172.0** (npm `@openai/codex` qua Volta, binary `codex-win32-x64`), Windows 11 Pro 26300 |
| Đăng nhập | ChatGPT, device code (`codex login --device-auth`) — app-server báo `authMode: chatgpt`, **`planType: free`** |
| Model | `gpt-5.6-luna` + `model_reasoning_effort="low"` mọi lượt (`codex debug models` còn có `gpt-6-luna`, `gpt-5.6-terra`) |
| Lượt model | 17, mỗi lượt ≈ 11–64K token vào (phần lớn cache) |
| Script | `scripts/run_exec.sh`, `scripts/summ.py`, `scripts/appserver_probe.py` (client JSON-RPC cho `codex app-server`) |
| Fixture | `fixtures/x0*.jsonl` + `*.stderr.txt` (hostname/installationId đã thay bằng `<host>`, `<installation-id>`) |
| Run dir | `%LOCALAPPDATA%\cfh-spike-cx\ws*` — ngoài git và ngoài `%TEMP%` (lý do: mục 2, 4); đã xoá sau spike |

Bẫy X-04: trước bẫy `~/.codex/config.toml` và `~/.codex/AGENTS.md` **không tồn tại**; tạo tạm rồi xoá, đã kiểm không còn. Không đọc/chép/di chuyển `~/.codex/auth.json`.

## 0. Kết luận nhanh

**Về kỹ thuật: ĐẠT — tốt hơn plan dự kiến.** Codex có **kênh duyệt động** qua `codex app-server` (ngang permission tool của Claude) → guard của hub **không buộc phải fail-closed tĩnh** như mục 3 của `plan-codex-grok-runtime.md`.

| ID | Giả định | Kết quả | Fixture |
|---|---|---|---|
| X-01 | Event `exec --json` chuẩn hoá được | **Xác nhận**: `thread.started{thread_id}` · `turn.started` · `item.started/completed` (`agent_message`, `command_execution`, `file_change`, `mcp_tool_call`, `error`) · `turn.completed{usage}`. **Lỗ hổng**: lệnh bị *policy* chặn và lệnh treo **không sinh item nào** | `x02e`, `x03a`, `x02a` |
| X-02 | Sandbox Windows chặn ghi/xoá | **Xác nhận với `windows.sandbox="mxc"`**: read-only chặn xoá dummy/ghi/apply_patch; workspace-write cho ghi trong cwd, **chặn ghi ra ngoài**, **chặn mạng** (`000`). **Bác bỏ cách javis-os (`"unelevated"`)**: lệnh treo → bị kill sau 40–120 s, và ghi được ra ngoài cwd. Không đặt gì → sandbox `disabled`, mọi shell "blocked by policy" | `x02a`, `x02e`, `x02b`, `x02d`, `x02c` |
| X-03 | MCP hub qua `-c mcp_servers.*` | **Xác nhận**. Núm `default_tools_approval_mode` / `tools.<t>.approval_mode` = `auto\|prompt\|writes\|approve`; `writes` + `-a never`: tool `readOnlyHint` chạy, tool ghi trả "requires approval, but approval policy is never" và **server không bị gọi** | `x03a` |
| X-04 | Cô lập cấu hình người dùng | **Một phần**: `exec --ignore-user-config` bỏ được `config.toml`; **không bỏ được `~/.codex/AGENTS.md`** (chỉ tránh bằng `CODEX_HOME` khác = mất đăng nhập). AGENTS.md dự án tắt bằng `project_doc_max_bytes=0` | `x04a`, `x04b` |
| X-05 | Resume | **Xác nhận**: `codex <cờ global> exec resume --json <thread_id> "<prompt>"` nhớ đúng mã; `exec resume` không có `-s` | `x05a`, `x05b` |
| X-06 | Prompt dài tiếng Việt qua stdin | **Xác nhận**: 81 KB UTF-8, dòng đầu/cuối đúng dấu | `x06` |
| X-07 | Kênh duyệt động giữa chừng | **Xác nhận** qua `codex app-server`: duyệt lệnh, patch, tool MCP (elicitation); decline → không chạy; giữ 60 s không timeout; `turn/steer`, `turn/interrupt` chạy | `x07a`–`x07e` |
| X-08 | `--approve-for-me` / `--remote` | `--approve-for-me`: chuyển yêu cầu duyệt cho reviewer tự động (`codex-auto-review`); lần thử không thấy reviewer được gọi. `--remote <ADDR>`: nối `exec` vào app-server — chưa thử | `x08` |

## 1. Event `codex exec --json`

```
{"type":"thread.started","thread_id":"01a2…"}                 ← id để resume
{"type":"turn.started"}
{"type":"item.completed","item":{"type":"error","message":"Loaded only 32768 of 129191 bytes from …AGENTS.md…"}}   (cảnh báo)
{"type":"item.completed","item":{"type":"agent_message","text":"…"}}
{"type":"item.started","item":{"type":"command_execution","command":"\"…powershell.exe\" -Command '…'","status":"in_progress"}}
{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"…","exit_code":0,"status":"completed|failed"}}
{"type":"item.started|completed","item":{"type":"mcp_tool_call","server","tool","arguments","result":{"content":[…]},"error":null|{"message"},"status"}}
{"type":"item.started|completed","item":{"type":"file_change","changes":[{"path","kind":"add"}],"status":"completed"}}
{"type":"turn.completed","usage":{"input_tokens","cached_input_tokens","cache_write_input_tokens","output_tokens","reasoning_output_tokens"}}
```
- `effort=low`: không có item `reasoning`.
- **Lỗ hổng audit**: lệnh bị execpolicy/sandbox `disabled` chặn ("blocked by policy", vd `Remove-Item -Recurse -Force` ở `x02a`) và lệnh treo không sinh item. App-server thì có item `status: declined`.
- Prompt ở argv + stdin là pipe → Codex in `Reading additional input from stdin...` và nối stdin vào prompt → runtime dùng `-` và đưa prompt qua stdin.
- Cờ global (`-a`, `-m`, `-c`) phải **trước** `exec` (`codex exec -a never` → `unexpected argument '-a'`).

## 2. Sandbox Windows (X-02)

| Cấu hình | Ghi trong cwd | Ghi `../` | Xoá dummy | Mạng | Thoát |
|---|---|---|---|---|---|
| `-s workspace-write`, không đặt `windows.sandbox` | blocked by policy | blocked | — | blocked | 31 s; log `selection: disabled` |
| `-c windows.sandbox="unelevated"` (javis-os) | **treo → bị kill** | **GHI ĐƯỢC** (run dir dưới `%TEMP%`) | — | lỗi alias `curl` | `x02b` không thoát ~6 phút sau `turn.completed`; `x02d` hết timeout 300 s |
| `-s workspace-write -c windows.sandbox="mxc"` | **OK** | **Access denied** | — | **000** | 33 s |
| `-s read-only -c windows.sandbox="mxc"` | Access denied | — | **blocked** (dummy còn nguyên) | — | 21 s |
| `codex -c windows.sandbox="mxc" sandbox --permission-profile :workspace -- cmd …` (không gọi model) | OK | denied | — | 000 | 1–3 s |

- Docs chính thức `/codex/windows`: `mxc` (Windows 11 24H2/25H2) là *"recommended"*, `elevated`/`unelevated` là *"legacy fallback"*.
- Lệnh kiểm rẻ cho `doctor`: `codex -c windows.sandbox="mxc" sandbox --permission-profile :workspace -- cmd.exe /d /c echo MXC_OK`.
- Run dir **không đặt dưới `%TEMP%`** (`x02d` ghi được ra ngoài cwd; chưa tách được do TMP được phép ghi hay do `unelevated`).
- Log sandbox: `~/.codex/.sandbox/sandbox.<ngày>.log`.
- `timeout` của bash chỉ giết `cmd.exe`, `codex.exe` mồ côi → runtime phải **kill cả cây tiến trình**.

## 3. MCP do hub cấp (X-03)

```
-c "mcp_servers.spike.command='C:\…\python.exe'"  -c "mcp_servers.spike.args=['-I','C:\…\mini_mcp.py']"
-c "mcp_servers.spike.env={MINI_MCP_LOG='…'}"     -c 'mcp_servers.spike.default_tools_approval_mode="writes"'
(thêm: tools.<tool>.approval_mode, enabled_tools, disabled_tools, tool_timeout_sec)
```
- `exec` (`-a never`): tool cần duyệt → fail-closed, server không bị gọi.
- App-server: tool cần duyệt → `mcpServer/elicitation/request` với `_meta.codex_approval_kind:"mcp_tool_call"`, `tool_name`, `tool_annotations`, `tool_params`; `{"action":"decline"}` → server không bị gọi (`x07e`); `accept` → chạy (`x07b`).
- Server `codex_apps` (connector ChatGPT) tự khởi động mặc định và *"not controlled by the sandboxed-command network proxy"* → hub phải tắt: `-c features.apps=false`.

## 4. Cô lập cấu hình (X-04)

| Nguồn | Mặc định | Cách tắt đã đo |
|---|---|---|
| `~/.codex/config.toml` | nạp (MCP bẫy khởi động) | `exec --ignore-user-config` (auth vẫn dùng `CODEX_HOME`) |
| `~/.codex/AGENTS.md` | nạp | **Không tắt được** bằng `--ignore-user-config` hay `project_doc_max_bytes=0`; chỉ `CODEX_HOME` khác (→ `Not logged in`) |
| AGENTS.md dự án (gốc git → cwd) | nạp; trong agent-hub 32 KB/129 KB ≈ 265K token mỗi lượt | `-c project_doc_max_bytes=0`, hoặc run dir ngoài git |
| Connector `codex_apps` | bật | `-c features.apps=false` |

- `codex debug prompt-input "<p>"` in toàn bộ input model thấy, không tốn lượt → dùng cho test và `doctor`.
- `~/.codex/AGENTS.md`: `doctor` cảnh báo, hoặc `CODEX_HOME` riêng theo tài khoản do hub quản lý và người dùng `codex login` vào đúng đó. **Không chép `auth.json`.**

## 5. `codex app-server` — kênh duyệt động (X-07)

Schema: `codex app-server generate-json-schema --out <dir>` (39 file).
```
→ initialize {clientInfo}   → (notif) initialized
→ thread/start {cwd, model, approvalPolicy:"untrusted"|"on-request"|"never"|{granular}, sandbox, developerInstructions, config:{…, mcp_servers}, ephemeral} ← {thread:{id}}
→ turn/start {threadId, input:[{type:"text",text}]}
← (req) item/commandExecution/requestApproval {itemId, command, cwd, commandActions, proposedExecpolicyAmendment}
→ {decision:"accept"|"acceptForSession"|"decline"|"cancel"|{acceptWithExecpolicyAmendment}}
← (req) item/fileChange/requestApproval {itemId, reason, grantRoot}      (diff nằm trong item/started của fileChange)
← (req) mcpServer/elicitation/request {serverName, _meta:{codex_approval_kind:"mcp_tool_call", tool_name, tool_params}} → {action}
← item/started|completed, item/agentMessage/delta, thread/status/changed{activeFlags:["waitingOnApproval"]}, turn/completed{status}
→ turn/steer {threadId, expectedTurnId, input}  → turn/interrupt {threadId, turnId}
```
- `x07a` (`untrusted` + `read-only`): lệnh bị decline → `status: declined`, file không tạo, model **làm tiếp** bước sau (khác `agy`, vốn kết thúc lượt). Lệnh được accept **chạy dù sandbox read-only** — có lẽ thiếu `windows.sandbox` thì "accept" = chạy ngoài sandbox. **Đo lại với `config.windows.sandbox="mxc"` trước PR2.**
- `x07b`: giữ duyệt 60 s không timeout.
- `x07c`: steer giữa lượt được áp dụng (trả lời có "STEER-OK").
- `x07d`: interrupt → `status: interrupted` ngay, nhưng lệnh đang chạy **không có `item/completed`** → hub tự đóng item treo.
- App-server đánh dấu **[experimental]**; chưa có tương đương `--ignore-user-config`.

## 6. Đề xuất `CodexRuntime`

- **Đường chính: `codex app-server`**, một tiến trình mỗi run (khởi động ~1,5 s).
  - `thread/start`: `cwd` (ngoài `%TEMP%`, ngoài git), `approvalPolicy="untrusted"` (hoặc `granular`), `sandbox` theo profile, `developerInstructions` = system prompt của hub.
  - `config`: `windows.sandbox="mxc"`, `features.apps=false`, `project_doc_max_bytes=0`, `mcp_servers.<hub>` với `default_tools_approval_mode="prompt"|"writes"`.
- Guard trả `requestApproval` / `elicitation`; không có người (chạy nền, theo lịch) → decline.
- Steer = `turn/steer`; stop = `turn/interrupt` → đóng stdin → kill cả cây; resume = `thread/resume` (chưa đo).
- Oneshot (`curation/llm.py`, `evals/judge.py`…) qua `exec`:
  `codex -m … -c model_reasoning_effort=… -a never -s read-only -c windows.sandbox="mxc" -c features.apps=false -c project_doc_max_bytes=0 exec --json --skip-git-repo-check --ignore-user-config [--output-schema f] -` + timeout + kill cả cây.
- `doctor`: `codex login status`, kiểm MXC qua `codex sandbox`, cảnh báo `~/.codex/AGENTS.md`.
- `capabilities()`: `approval=True`, `steer=True`, `resume=True`, `budget=False`.

## 7. Chưa đo

- App-server + `config.windows.sandbox="mxc"`: lệnh đã duyệt còn bị sandbox giữ không, mạng có chặn không (**trước PR2**); `thread/resume`; thay `--ignore-user-config` cho app-server.
- `granular`, `acceptForSession`, `acceptWithExecpolicyAmendment`, `cancel`.
- `sandbox_workspace_write.exclude_tmpdir_env_var`; nguyên nhân ghi ra ngoài ở `x02d`.
- `--approve-for-me` khi model thật sự xin vượt quyền; `exec --remote ws://`.
- `danger-full-access` (cố ý không chạy); hạn mức gói Free khi chạy song song; nhiều `CODEX_HOME` cùng lúc; macOS/Linux.

## 8. Đo bổ sung

X-09..X-12: xem `docs/spikes/codex-grok-spike-report.md` mục 3 (fixture cùng thư mục `fixtures/`).
