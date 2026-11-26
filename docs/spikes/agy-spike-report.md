# Báo cáo spike — `agy` (Antigravity CLI) đăng nhập tài khoản Google làm runtime cho cfharness

| Mục | Giá trị |
|---|---|
| Ngày chạy | 2026-11-25 |
| CLI | `agy` **1.5.16** (Windows 11, cài bằng `install.ps1` vào `%LOCALAPPDATA%\agy\bin`) |
| Đăng nhập | Tài khoản Google cá nhân (`oauth-personal`), token trong keyring OS |
| Model | `-p`: `gemini-3.8-flash-low`. ACP: mặc định `gemini-4-neon-high` (chưa đặt model) cho tới G-10, G-10 dùng `session/set_model` → flash-low |
| Chi phí | Không trả tiền theo lượt: trừ vào quota gói Google. Tổng ≈ 0,3 triệu token qua 12 lượt |
| Script + fixture | `docs/spikes/agy/scripts/` (`acp_probe.py`, `acp_steer_resume.py`, `mini_mcp.py`), `docs/spikes/agy/fixtures/` |
| Plan liên quan | `docs/dev-notes/plan-gemini-runtime.md` |

Cấu hình HOME (`~/.gemini/GEMINI.md`, `~/.gemini/config/mcp_config.json`, `config.json`) được sao lưu trước bẫy G-06 và đã trả lại nguyên trạng. Mục tiêu phá huỷ duy nhất là `work/sandbox-dummy/` — vẫn còn sau mọi lần thử.

## 0. Kết luận nhanh

**Về kỹ thuật: ĐẠT nếu đi qua `agy acp` (Agent Client Protocol), KHÔNG đạt nếu đi qua `agy -p`.**
**Về điều khoản: CHƯA ĐƯỢC PHÉP RÕ RÀNG với gói cá nhân — đây là chốt chặn, không phải kỹ thuật.**

| ID | Giả định | `agy -p` | `agy acp` |
|---|---|---|---|
| G-01 | Event chuẩn hoá được | **Xác nhận**: `init`(conversation_id, tools, permission_mode) · `step_update`(text_delta / tool + args + output, usage) · `result` | **Xác nhận**: JSON-RPC `session/update` (`agent_message_chunk`, `tool_call`, `tool_call_update` status/content, `usage_update`), `session/prompt` trả `stopReason` + usage |
| G-02 | Chặn/giữ chờ phê duyệt tool | **Bác bỏ**: mặc định `request-review` nhưng **không hỏi ai**; shell bị sandbox chặn ghi, còn `write_file` **ghi luôn, kể cả ngoài workspace** | **Xác nhận**: `session/request_permission` cho `run_command`, `write_file`, mọi tool MCP; Deny → tool `failed`, không chạy; **giữ chờ 150 s không timeout** |
| G-03 | Kênh "ask" | Không có | Chính là `request_permission` (Allow/Deny once) — hub làm client |
| G-04 | Steer | chưa đo (`--input-format stream-json` có trong `--help`) | **Xác nhận**: prompt thứ 2 gửi vào cùng session khi lượt 1 đang chạy được gộp vào lượt đang làm |
| G-05 | Ngắt + resume | chưa đo | **Xác nhận**: `session/cancel` → `stopReason: cancelled` tức thì; kill tiến trình → tiến trình mới `session/load` nhớ đúng ngữ cảnh; không thấy lỗi phát lại 2 lần |
| G-06 | Cô lập cấu hình người dùng | **Bác bỏ**: nạp `~/.gemini/GEMINI.md` (lộ mã bẫy) và đọc MCP HOME (treo ~120 s chờ server bẫy) | **Xác nhận**: `--gemini-dir <dir riêng>` + `--app-data-dir <tên tương đối>` → không nạp GEMINI.md/MCP HOME, đăng nhập vẫn chạy (token ở keyring) |
| G-07 | MCP do hub cấp | — | **Xác nhận**: `session/new.mcpServers` (stdio) theo session; tên tool `mcp_<server>_<tool>`; deny → server MCP không bị gọi |
| G-08 | Xác thực headless | Chưa đăng nhập: `agy models` in `Please sign in` (exit 0) | `initialize.authMethods`: `oauth-personal`, `oauth-business` (Gemini Enterprise), … |
| G-09 | Chi phí/budget | usage token theo bước + `result` | usage theo lượt; **không có budget flag** → hub tự cắt bằng `session/cancel` |
| G-10 | Prompt dài, tiếng Việt, Windows | chưa đo (argv) | **Xác nhận**: 79 KB UTF-8 tiếng Việt qua stdin JSON, chép lại đúng dấu |
| G-11 | Không ghi ra ngoài workspace | **Bác bỏ** (`write_file` ghi `../outside-cwd.txt`) | Mọi lần ghi đều hỏi quyền → guard quyết |

## 1. `agy -p` không dùng được cho hub

`permission_mode: request-review` trong `init` gây hiểu nhầm: trong `-p` không có ai để review nên:
- `run_command` chạy ngay trong một sandbox OS **chặn ghi**: `Remove-Item -Recurse -Force sandbox-dummy` và `Set-Content` đều `Access to the path is denied` (`fixtures/g02b-*`, `g02c-*`). Đọc file ngoài workspace và gọi mạng vẫn được (`curl https://example.com` → 200, `g02d-*`).
- Tool file riêng (`write_to_file` → event `write_file`) **ghi thẳng, không qua sandbox**, kể cả `C:\…\spikes\agy\outside-cwd.txt` (`g11a-*`).
- `--mode plan`: không ghi gì ngoài file kế hoạch trong `~/.gemini/antigravity-cli/brain/…` (`g02e-plan.jsonl`).
- Không có hook chặn tool trong `--help` 1.5.16; javis-os cũng ghi nhận "không có nấc tương đương".

## 2. `agy acp` — đường tích hợp được

Giao thức: JSON-RPC 2.0, mỗi message một dòng trên stdio. Trình tự hub cần làm (xem `scripts/acp_probe.py`):

```
→ initialize {protocolVersion:1, clientCapabilities:{fs:{readTextFile:false,writeTextFile:false}, terminal:false}}
← agentCapabilities {loadSession, sessionCapabilities{resume,close,load,list,additionalDirectories}, _meta.antigravity.systemPrompt.append}
→ session/new {cwd, mcpServers:[{name,command,args,env:[{name,value}]}]}
← {sessionId, models{currentModelId, availableModels[27]}, modes{default|accept-edits|auto|plan}, configOptions}
→ session/set_model {sessionId, modelId}
→ session/prompt {sessionId, prompt:[{type:"text", text}]}
← session/update … tool_call {toolCallId, name, args, kind: execute|other|…, status}
← session/request_permission {toolCall{name, rawInput}, options:[{optionId:"allow",kind:"allow_once"},{optionId:"deny",kind:"reject_once"}]}
→ {outcome:{outcome:"selected", optionId:"deny"}}
← tool_call_update status:"failed", content: "user denied permission … Do not attempt to circumvent"
← (response session/prompt) {stopReason: end_turn|cancelled, usage{inputTokens,outputTokens,thoughtTokens,…}}
```

Quan sát thêm:
- Sau một lần Deny, agent **kết thúc lượt** (không làm tiếp các bước sau) — `g03a`, `g03b`.
- Mọi tool MCP đều hỏi quyền, **kể cả tool `readOnlyHint: true`** → guard của hub tự quyết theo registry (trust/annotation) như hiện nay.
- `_meta.antigravity.systemPrompt.append` → có đường nối system prompt của hub (chưa đo).
- `--app-data-dir` **phải là đường dẫn tương đối** (tính từ `--gemini-dir`); tuyệt đối → `language server failed to start: must not be absolute`.
- Model mặc định của ACP là model nặng (`gemini-4-neon-high`) → runtime **luôn** gọi `session/set_model`.
- Không thấy tiến trình `agy`/language server sót lại sau khi đóng stdin hoặc kill.

## 3. Điều khoản — chốt chặn

Nguồn chính thức (không ghi ngày hiệu lực trên trang):
- **Antigravity Additional Terms §6** (https://antigravity.google/terms): *"…using the Service in connection with products not provided by us."* và *"Using third party software, tools, or services to access the Service … is a breach of this Agreement"*, có thể dẫn tới *"suspension or termination of your Antigravity and/or Gemini CLI accounts."*
- **FAQ** (https://antigravity.google/docs/faq), mục headless trong công cụ bên thứ ba: *"Wrapping agy inside third-party coding agents or applications, or automating account rotation, is not supported under consumer plans."*

Diễn đàn (không phải văn bản chính sách):
- Nhân viên Google (`chunduriv`, staff/moderator, 2026-09-30, thread 185992): chạy **binary `agy` nguyên bản** làm **tiến trình con cục bộ cho một người dùng**, không trích/chuyển token OAuth, không làm server dùng chung, không vòng lặp tần suất cao không người giám sát → *"a supported use"*; Google còn có adapter `antigravity-acp` hỗ trợ `oauth-personal`. Câu hỏi đối chiếu với FAQ sau đó **chưa được trả lời**.
- Các trả lời khác (thread 196472, 201688, 2026-10/11) từ người **không phải nhân viên** nói ngược lại cho gói consumer.

Đánh giá: cfharness là **ứng dụng điều phối nhiều agent**, có lịch chạy, chạy nền, Telegram/Slack, chạy khi người không ngồi máy — tức đúng loại FAQ nói *"not supported under consumer plans"*, và vượt ra ngoài điều kiện "single-user, không unattended" của câu trả lời staff. Rủi ro là **khoá tài khoản Google** của người dùng.

## 4. Quyết định đề xuất

| Lựa chọn | Đánh giá |
|---|---|
| Đưa `agy` + **tài khoản Google cá nhân** vào hub cho người dùng | **Không làm** tới khi Google có văn bản chính thức cho phép (rủi ro khoá tài khoản; mâu thuẫn FAQ) |
| `agy acp` + **Gemini Enterprise (`oauth-business`)** hoặc **API key / ADC của công ty** | Kỹ thuật đã chứng minh ở trên áp dụng nguyên; Terms §6 vẫn cần đọc lại cho gói doanh nghiệp/API (FAQ chỉ nói "consumer plans") — **hướng nên đi tiếp** |
| `gemini` CLI + API key (đường A của plan) | Vẫn mở; chưa spike |

Thiết kế runtime nếu đi tiếp (thay cho phác thảo `-p` trong plan): `AcpRuntime` dùng chung cho mọi agent nói ACP — một tiến trình `agy acp --gemini-dir <run>/gem --app-data-dir run-app` mỗi run; guard trả lời `session/request_permission`; MCP của hub qua `session/new.mcpServers`; steer = `session/prompt` thứ hai; stop = `session/cancel` rồi kill; resume = `session/load`.
Lợi phụ: ACP là giao thức mở (Zed), nên cùng `AcpRuntime` có thể chạy agent khác hỗ trợ ACP.

## 5. Chưa đo

- `-p --input-format stream-json` (steer ở `-p`), `--print-timeout`, `--json-schema` trong ACP/oneshot.
- `session/resume` (khác `session/load`), `session/close`, `session/list`.
- `systemPrompt.append` qua `_meta`; `session/set_mode` (`auto`/`accept-edits`) có còn gửi `request_permission` không.
- Sandbox shell của ACP (chạy với quyền ghi hay chặn ghi sau khi Allow) — ở `g03b` không tới bước shell vì lượt dừng sau Deny.
- Thời gian giữ chờ tối đa (> 150 s), hành vi khi client không bao giờ trả lời.
- Đăng nhập `oauth-business` / API key / ADC với ACP; nhiều tài khoản song song.
- macOS/Linux.
