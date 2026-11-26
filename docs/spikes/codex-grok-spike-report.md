# Báo cáo spike PR0 — Codex (ChatGPT) và Grok Build làm runtime cho cfharness

Ngày chạy: 2026-11-25. Plan: `docs/dev-notes/plan-codex-grok-runtime.md`. Spike trước: `docs/spikes/agy-spike-report.md`.
Chi tiết, bảng ID và fixture: `docs/spikes/codex-grok/codex/REPORT-codex.md` (X-01..X-08), `docs/spikes/codex-grok/grok/REPORT-grok.md` (K-01..K-08).

**Lưu ý tài khoản**: cả hai lần đo đều chạy trên **gói Free** (ChatGPT `planType: free`; Grok hết quota free 600K token/24 h ở lượt thứ 16). Kết quả kỹ thuật vẫn đúng, nhưng quota thật của Plus/Pro/SuperGrok chưa đo.

## 1. Kết luận

| | Codex 0.172.0 | Grok 1.0.92 |
|---|---|---|
| Đường tích hợp nên dùng | **`codex app-server`** (JSON-RPC riêng của OpenAI) | **`grok agent stdio`** (ACP — cùng giao thức `agy`) |
| Duyệt từng tool giữa chừng | **Có**: `requestApproval` cho lệnh/patch, `elicitation` cho tool MCP; decline → không chạy; giữ 60 s OK | **Có**: `session/request_permission` cho sửa file/shell/MCP; reject → không chạy; giữ 60 s OK |
| Steer / stop / resume | `turn/steer` ✓ · `turn/interrupt` ✓ · `thread/resume` chưa đo (`exec resume` ✓) | chưa đo (429) · `session/cancel` ✓ · `session/load` ✓ |
| MCP của hub | `config.mcp_servers.*` + `default_tools_approval_mode` | `session/new.mcpServers` (không cần trust thư mục) |
| Sandbox OS trên Windows | **Có — chỉ khi `windows.sandbox="mxc"`** (chặn ghi ngoài cwd + mạng). `"unelevated"` của javis-os **hỏng** | **Không** (`--sandbox`: "no sandbox enforcement for this platform") → chỉ dựa vào duyệt quyền |
| Cô lập cấu hình người dùng | Một phần: `--ignore-user-config` (chỉ `exec`), `project_doc_max_bytes=0`, `features.apps=false`; **`~/.codex/AGENTS.md` vẫn lọt** | **Kém**: mặc định nạp MCP/skill/hook/rules của **Claude Code** trên máy; `GROK_CLAUDE_*_ENABLED=0` tắt phần lớn nhưng plugin `vercel`, `~/.grok/config.toml`, `~/.grok/rules` vẫn lọt; chỉ `GROK_HOME` riêng (cần đăng nhập/API key riêng) |
| Oneshot (curation, judge) | `exec --json -a never -s read-only … --output-schema` | `--prompt-file --output-format json --permission-mode dontAsk --json-schema` |
| Prompt tiếng Việt dài | 81 KB ✓ | 105 KB ✓ |
| Điều khoản | Có chương trình chính thức SIWC cho app cục bộ/mã nguồn mở; mỗi người dùng tài khoản của mình | Docs khuyên API key cho tự động hoá; không câu nào cấm/cho phép rõ; ToS hiện hành chưa đọc được |

**Điều plan cũ sai, cần sửa**:
1. Mục 3 của plan ("guard quyết tĩnh trước khi chạy, fail-closed") **không cần nữa** cho run chính: cả hai đều có kênh duyệt động. Giữ fail-closed tĩnh chỉ cho oneshot và khi chạy nền không có người.
2. Không dùng `codex exec` cho run chính (thiếu item cho lệnh bị chặn → lỗ hổng audit; không steer) và không làm `GrokCliRuntime` kiểu `-p`.
3. `-c windows.sandbox="unelevated"` của javis-os → thay bằng `"mxc"`.

## 2. Thiết kế đề xuất (thay mục 5 của plan)

- **`AcpRuntime`** dùng chung cho `grok agent stdio` (và `agy acp` nếu sau này có gói doanh nghiệp/API). Hub là ACP client: guard trả `request_permission` bằng `allow_once`/`reject_once` (**không bao giờ** `*_always` — Grok lưu theo project); MCP hub qua `session/new`; stop = `session/cancel` → kill cây; resume = `session/load`.
  - Grok env: `GROK_DISABLE_AUTOUPDATER=1`, `GROK_MEMORY=0`, mọi `GROK_CLAUDE_*_ENABLED=0`, `GROK_CURSOR_*_ENABLED=0`; `grok agent --reasoning-effort low stdio` (mặc định `high`).
- **`CodexAppServerRuntime`**: một `codex app-server` mỗi run; `thread/start` với `approvalPolicy="untrusted"`, sandbox theo profile, `developerInstructions`, `config` = `windows.sandbox="mxc"`, `features.apps=false`, `project_doc_max_bytes=0`, `mcp_servers.<hub>`; guard trả `requestApproval`/`elicitation`; `turn/steer`, `turn/interrupt`.
- Oneshot: `exec` (Codex) / `--prompt-file` + `dontAsk` (Grok), fail-closed.
- Run dir: ngoài git và ngoài `%TEMP%`. Luôn kill **cả cây tiến trình** (Windows để `codex.exe` mồ côi).
- `doctor`: trạng thái đăng nhập; kiểm MXC (`codex -c windows.sandbox="mxc" sandbox --permission-profile :workspace -- cmd.exe /d /c echo MXC_OK`); cảnh báo `~/.codex/AGENTS.md`, `~/.grok/config.toml [mcp_servers]`, `~/.grok/rules/`, plugin Grok đã cài.

Thứ tự PR đề xuất: PR1 registry runtime theo agent → PR2 `AcpRuntime` + Grok (giao thức đã đo kỹ 2 lần) → PR3 `CodexAppServerRuntime` → PR4 oneshot cho cả hai → PR5 UI/doctor → PR6 docs/BRD.

**Đã cài (nhánh `feat/multi-runtime`)**: PR1 + PR2 + PR3 + PR5 gộp (`runtime/rpcbase.py`, `runtime/acp.py`, `runtime/codex.py`,
`runtime/doctor.py`, `agents.runtime`, migration v37, CLI `--runtime`, form web). Cô lập theo mục 3 (đo bổ sung) thay cho các dòng
"cô lập" ở bảng trên. PR4 (oneshot qua codex/grok) chưa làm — judge/curator vẫn dùng runtime Claude của hub.

## 3. Đo bổ sung (2026-11-25, sau báo cáo đầu) — kết quả

| ID | Việc | Kết quả | Fixture |
|---|---|---|---|
| X-09 | Codex app-server + mxc: lệnh hub đã *accept* còn bị sandbox giữ? | **Xác nhận**: read-only + accept → ghi bị từ chối; workspace-write + accept → ghi trong cwd OK, `..\` bị từ chối, mạng `000`. Ba cách đặt đều chạy: `config:{"windows":{"sandbox":"mxc"}}`, `config:{"windows.sandbox":"mxc"}`, `-c windows.sandbox="mxc"` ở gốc. **Thiếu mxc thì accept chạy NGOÀI sandbox kể cả read-only** (`x09n`, log `selection: disabled`) → hub luôn đặt mxc. `windowsSandbox/readiness` báo `notConfigured` dù mxc chạy → không dựa vào nó | `codex/fixtures/x09*` |
| X-10 | Codex `thread/resume` ở tiến trình mới | **Xác nhận** với `thread/start.ephemeral=false`; `thread/resume {threadId, cwd, model, approvalPolicy, sandbox, config, developerInstructions}` nhớ đúng mã | `x10a`, `x10b` |
| X-11 | Cô lập cấu hình cho app-server | MCP của `~/.codex/config.toml` nạp mặc định; `mcp_servers={}` (gốc hay thread) **không** tắt được; tắt **từng server**: `config.mcp_servers.<tên>.enabled=false` (tên lấy từ `config/read {includeLayers:true}`); thêm `features.apps=false` → không server MCP nào khởi động. `~/.codex/AGENTS.md` **vẫn lọt** (không cờ/khóa nào) | `x11a`–`x11f` |
| X-12 | `granular`, `acceptForSession` | `granular` cần `initialize.capabilities.experimentalApi=true`. `acceptForSession` → lệnh giống hệt ở lượt sau **không hỏi lại** → hub chỉ trả `accept`/`decline` để guard theo từng lần | `x12a`, `x12b` |
| K-09 | Cô lập Grok không cần đăng nhập lại | **Xác nhận**: `GROK_HOME` riêng của hub + `GROK_AUTH_PATH=~/.grok/auth.json` → `session/new` xác thực bằng `cached_token`, `auth.json` không đổi. `config.toml` của home đó: `[plugins] disabled=["vercel"]`, `[compat.claude]`/`[compat.cursor]` skills/rules/agents/mcps/hooks = false → tắt plugin + mọi skill/MCP/hook Claude. `GROK_CONFIG` (overlay env) không làm được (chỉ models/features/toolset) | `grok/fixtures/k09*` |
| K-10 | Grok steer | **Chưa đo**: vẫn 429 `free-usage-exhausted` | `k10a` |

Kiểm thật sau khi code (`CodexRuntime` + `codex` 0.172.0 thật, gói Free): guard cho phép lệnh 1 → chạy, tạo file; guard từ chối lệnh 2 →
`decline`, file không tạo; run `completed`; `config/read` + `thread/start.config` (`windows.sandbox=mxc`, `features.apps=false`,
`project_doc_max_bytes=0`) được nhận, không lỗi cấu hình.

## 4. Điều khoản xAI — đọc trong trình duyệt (2026-11-25)

- **Consumer ToS** (https://x.ai/legal/terms-of-service, cập nhật 27/10/2026): không có điều khoản riêng về Grok Build/CLI hay app bên thứ
  ba; §2 cấm chia sẻ thông tin đăng nhập/tài khoản (*"You may not share your account credentials or make your account available to anyone
  else"*) → mỗi người dùng hub dùng tài khoản của chính họ; §4 người dùng chịu trách nhiệm cho "Agentic Actions".
- **Acceptable Use Policy** (https://x.ai/legal/acceptable-use-policy, hiệu lực 14/08/2026): cấm *"Accessing the Services through
  unauthorized automated or non-human means, whether through a bot, script, or otherwise"* và *"circumventing any rate limits"*.
- **Trang Grok Build** (https://x.ai/build) quảng bá *Headless mode — Script Grok Build in CI/CD pipelines*; docs đi kèm CLI nói ACP dành
  cho *"IDEs, SDKs, eval harnesses, and custom apps"* và khuyên **API key cho CI/tự động hoá**.
- Đánh giá: chạy **binary `grok` chính thức** qua giao diện ACP mà xAI công bố, mỗi người dùng tài khoản của mình, có người duyệt tool
  → gần với "authorized" hơn là bot/script tự chế; nhưng chạy nền theo lịch/không người trực trên gói thuê bao là vùng xám → khuyến nghị:
  run theo lịch/nền của agent grok nên dùng **API key (console.x.ai)**; bật grok cho người dùng kèm ghi chú điều khoản (README).

## 5. Còn mở

| Việc | Ảnh hưởng |
|---|---|
| Grok steer (prompt thứ 2 giữa lượt) — code đã cài như `agy` (gửi `session/prompt` thứ 2, chờ mọi prompt xong) | Đo khi có quota |
| Quota thật của gói trả phí (Plus/Pro, SuperGrok) khi nhiều agent chạy song song | Trước khi bật rộng |
| `~/.codex/AGENTS.md` không tắt được | `cfharness doctor` cảnh báo |
| Grok: agent `vuln-*` của người dùng vẫn liệt kê nguồn "user" trong home riêng (chưa rõ từ đâu) | Theo dõi |
