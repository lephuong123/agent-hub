# Plan: spike `GeminiCliRuntime` — cho agent chọn runtime `claude` hoặc `gemini` (hướng 2)

Trạng thái: PLAN spike (2026-11-25, cập nhật cùng ngày: thêm mục 0 chọn CLI). Chưa chạy. Đây là thay đổi phạm vi: BRD ghi O3 (đa nhà cung cấp model) và C-05 (không đa provider)
là ngoài phạm vi; NFR-22 chỉ chừa interface `AgentRuntime`. Nếu spike đạt (mục 5) thì cập nhật BRD (bỏ O3 khỏi "ngoài phạm vi",
sửa C-05) trước khi mở PR code đầu tiên.

> **Kết quả spike `agy` + tài khoản Google (2026-11-25): `docs/spikes/agy-spike-report.md`.** Kỹ thuật đạt qua `agy acp`
> (phê duyệt từng tool, cô lập theo run, MCP theo session, steer, cancel, load); `agy -p` không đạt. Chốt chặn là điều khoản:
> FAQ của Antigravity nói bọc `agy` trong ứng dụng bên thứ ba "not supported under consumer plans" → không đưa vào hub với gói cá nhân.

## 0. Chọn CLI nào: `gemini` hay `agy` (bổ sung 2026-11-25)

Chính sách dữ liệu: **đã chốt được phép** đưa nội dung vào Gemini (phuonglq, 2026-11-25). BRD sẽ được sửa O3/C-05 nếu spike đạt.

Bối cảnh mới phát hiện khi tham khảo javis-os (https://github.com/blogminhquy/javis-os, `docs/10-models-va-engine.md`):
- Google **ngừng phục vụ Gemini CLI cho tài khoản cá nhân từ 18/06/2026** (Google AI Pro/Ultra/free). Gemini CLI vẫn chạy với
  **Gemini API key trả phí** và giấy phép **Gemini Code Assist Standard/Enterprise**. Bản thay thế Google chỉ định là
  **Antigravity CLI (`agy`)** dùng gói Google cá nhân. javis-os đã gỡ engine Gemini CLI ở bản 0.50.0.
- Vì vậy spike phải chọn đường xác thực trước, rồi mới chọn CLI:

| Đường | CLI | Ưu | Nhược (theo javis-os + docs) |
|---|---|---|---|
| A. API key trả phí (AI Studio) hoặc Vertex/Code Assist của công ty | `gemini` | Key để ở secrets hub, mỗi run một env → nhiều "tài khoản" dễ; có hook `BeforeTool`, `--resume`, stream-json | Trả theo token; Google đang dồn sức sang `agy`, Gemini CLI có thể bị bỏ dần |
| B. Gói Google (đăng nhập OAuth) | `agy` | Dùng gói subscription, có cả model không phải của Google | Đăng nhập lưu ở **keyring OS** (một tài khoản/HOME); **không có nấc chặn tool tương đương** (javis-os phải dựa `--sandbox` + system prompt + chặn ở MCP hub); MCP chỉ nạp từ **HOME** `~/.gemini/config/mcp_config.json` (tầng workspace bị bỏ qua — issue #60 antigravity-cli) → **không cô lập theo run**; nhận prompt qua stdin thất thường (`Error: empty prompt`), vỡ UTF-8 tiếng Việt khi bơm stdin; `--sandbox` + `--dangerously-skip-permissions` làm tool shell bị huỷ sau 5 s (agy 1.2.7); nối mạch `--conversation` chưa đo |

**Đề xuất: spike đường A (`gemini` + API key/Vertex của công ty) là chính**, và chỉ chạy G-02 + G-06 cho `agy` để có số liệu so sánh.
Đường B vướng đúng hai thứ là lõi của hub (guard chặn tool, cô lập theo run). Hướng của javis-os (đặt rào ở MCP hub, tin CLI chạy
`auto`) không hợp hub này: guard của cfharness chặn cả Bash/Write chứ không chỉ tool MCP.

Bài học kỹ thuật lấy từ javis-os, áp vào runtime Gemini nào cũng được:
- **Đo cú pháp CLI trên binary thật, không suy từ docs** (javis-os sai 2 lần): runtime dò `--help` một lần, ghi vào `compat.py`.
- Prompt dài đi qua file/stdin, không argv (Windows 32767 ký tự; system prompt hub cũng dài).
- Model của Google có tên kèm mức nghĩ (`…-flash-medium`) → không gửi thêm `effort`, nếu không CLI thoát mã 1.
- Danh sách model hỏi thẳng CLI (`agy models` / API), không chép tay bảng model.
- Lấy session id từ sự kiện `init` trong stream, không tự sinh.

## 1. Mục tiêu

Một agent có thể chạy trên `gemini` CLI (headless `gemini -p`) thay cho `claude`, **cùng lớp an toàn của hub**:
guard phê duyệt tool, ESTOP/stop, audit, cô lập cấu hình người dùng, che secret trong log thô.
Hub vẫn là một: cùng daemon, DB, web, TUI; chỉ khác runtime của từng run.

Không làm (kể cả khi spike đạt):
- Không tự viết agent loop hay gọi Gemini API trực tiếp (giữ nguyên tinh thần O1: runtime là một CLI agent có sẵn).
- Không chuyển điều phối viên (manager), judge/eval, curator, second-opinion sang Gemini ở vòng đầu — chúng giữ `claude`.
- Không làm tương đương cho connector claude.ai, plugin Claude Code, `--agents` (subagent), `accounts/` nhiều tài khoản claude.

## 2. Hiện trạng code (điểm phải đụng)

| Chỗ | Hiện tại | Ghi chú cho Gemini |
|---|---|---|
| `runtime/base.py` `AgentRuntime` | interface start/events/send/interrupt/stop/resume/fork/oneshot/auth_status | Đủ để cắm runtime mới; `RunHandle.adapter` đang gõ cứng kiểu `ClaudeStreamAdapter` |
| `runtime/base.py` `RunSpec` | ~40 trường, nhiều trường chỉ Claude hiểu (`plugin_dirs`, `agents_json`, `connectors`, `claude_config_dir`, `permission_prompt_tool`…) | Runtime Gemini phải **từ chối rõ ràng** trường không hỗ trợ, không lờ đi |
| `runtime/__init__.py` `make_runtime` | một runtime toàn cục theo `runtime.kind` | Cần chọn runtime **theo run** (theo agent) |
| `runs.py:145` `RunManager.runtime()` | một instance dùng cho mọi run | Đổi thành registry `{kind: runtime}` |
| `claude_adapter.py` (614 dòng) | stream-json Claude → `HubEvent` | Viết `gemini_adapter.py` ra cùng bộ `HubEvent` |
| `guard/` + `hub-guard` MCP | `--permission-prompt-tool` + hook `PreToolUse` + `control_request can_use_tool` | Phải tìm kênh tương đương — **điểm quyết định của spike** |
| `ClaudeCliRuntime.capabilities()` | steer/approval/fork/resume/json_schema/budget | Web/TUI/RPC phải đọc capabilities để ẩn/tắt nút (steer, fork…) |

## 3. Câu hỏi spike (giả định cần kiểm)

Mỗi câu ghi kết quả: **Xác nhận / Bác bỏ / Một phần**, kèm lệnh đã chạy và fixture JSONL (như `docs/spikes/M0-spike-report.md`).

| ID | Giả định | Vì sao quan trọng | Cách kiểm |
|---|---|---|---|
| G-01 | `gemini -p --output-format stream-json` cho JSONL ổn định: `init` (có session id) · `message` · `tool_use` · `tool_result` · `error` · `result` (có token/chi phí) | Nền của adapter, FR-RT-04 | Chạy 3 prompt (không tool / 1 tool / nhiều tool, có lỗi tool), lưu fixture `g01-*.jsonl`, đối chiếu với `docs/schema/event.v1.json` |
| G-02 | **Phê duyệt động trong headless**: hook `BeforeTool` chặn được (allow/deny), và có cách "giữ chờ" người duyệt ≥ 150 s | Không có thì không có guard → **no-go** cho mọi agent có tool ghi | Hook script giữ 150 s rồi deny `rm -rf sandbox-dummy`; đo timeout và hành vi khi hết giờ (fail-open hay fail-closed?) |
| G-03 | Có kênh "ask" thứ hai (tương đương `--permission-prompt-tool`), hoặc hook đủ để hub tự hỏi người qua socket | Quyết định thiết kế guard cho Gemini | Đọc docs + thử `--approval-mode default` trong `-p`: tool cần duyệt bị deny, treo, hay tự chạy? |
| G-04 | Steer: gửi tin vào tiến trình đang chạy qua stdin | FR-RT-06; không có thì `capabilities.steer=False` (chấp nhận được) | Thử stdin stream; nếu không có, ghi rõ và dừng ở đó |
| G-05 | Ngắt + resume: SIGINT/SIGTERM rồi `--resume <uuid>` tiếp tục đúng | ESTOP, auto-resume | Ngắt giữa tool dài; resume; kiểm lỗi đã biết **tool result bị phát lại 2 lần khi resume headless** (issue google-gemini/gemini-cli#29365, báo ở 0.60.0) |
| G-06 | Cô lập: không nạp `~/.gemini/settings.json`, `GEMINI.md` thư mục cha, extension, MCP của người dùng; chỉ nạp cấu hình hub đưa vào | NFR-15, A-04/A-05 tương đương | Đặt "bẫy" trong `~/.gemini` (MCP giả, GEMINI.md có chữ ký), chạy với home/settings riêng của hub (thử biến môi trường đổi thư mục cấu hình, settings hệ thống), kiểm `init` |
| G-07 | MCP do hub cấp (`hub-self`, proxy) chạy được qua settings của run, tên tool dự đoán được để guard/audit nhận diện | hub-self là đường duy nhất agent gọi task/knowledge | Cấu hình 1 server stdio + 1 http; xem tên tool trong stream; gọi thử `fs_*` |
| G-08 | Xác thực headless không cần trình duyệt: API key (AI Studio) hoặc Vertex; token để ở secrets của hub, không lộ trong argv/log | NFR bảo mật, H-12 | Chạy với key qua env; grep raw log + stderr tìm key; thử key sai → mã lỗi/thông điệp để làm `auth_status()` |
| G-09 | Giới hạn chi phí/vòng: có `max turns`/budget hoặc ít nhất số token theo lượt để `cost_guard` tự cắt | Cost guardrails | Đọc `result` lấy token; nếu không có budget flag thì hub cắt bằng stop khi vượt |
| G-10 | Windows: spawn qua `oscompat.exec_argv`, prompt dài (> 32 KB) truyền qua file/stdin, không WinError 206 | Máy chính chạy Windows | System prompt 60 KB qua file |
| G-11 | Sandbox/workspace: ghi được trong `cwd` + `add_dirs`, không ghi ra ngoài khi không cho phép | `confine.py`, workspace scratch/dir/worktree | Thử ghi `../outside.txt` ở approval-mode mặc định và khi hook deny |

Ghi lại phiên bản `gemini --version`, model dùng, và tổng chi phí thực.

## 4. Cách chạy spike

- Thư mục: `docs/spikes/gemini/` (script + `fixtures/` + `work/` scratch), báo cáo `docs/spikes/gemini-spike-report.md`.
- Model rẻ nhất có tool calling (flash), trần chi phí cả spike ≈ $2; ghi chi phí từng lần.
- Không sửa `~/.gemini/*` thật ngoài file "bẫy" của G-06 (sao lưu và trả lại sau khi xong); mục tiêu phá huỷ duy nhất là `work/sandbox-dummy/`.
- Chưa đụng `src/`. Script spike gọi `gemini` trực tiếp.
- Chính sách dữ liệu đã chốt (mục 0). Còn cần: **một API key trả phí hoặc project Vertex của công ty** cho đường A. Spike vẫn chỉ dùng prompt giả.
- Đo `agy` (so sánh, mục 0) chỉ khi máy có sẵn `agy` đăng nhập gói Google; không thì bỏ qua, ghi "chưa đo".

## 5. Tiêu chí go / no-go

| Kết quả | Quyết định |
|---|---|
| G-01, G-02, G-05, G-06, G-08 đều Xác nhận | **Go** đầy đủ: agent Gemini được dùng tool ghi, qua guard như Claude |
| G-02 Bác bỏ (không chặn/giữ chờ được tool trong headless) | **Go hạn chế**: agent Gemini chỉ được tool đọc (allowlist tĩnh), workspace scratch, không MCP ghi. Ghi rõ trong UI |
| G-01 hoặc G-06 Bác bỏ | **No-go**: không chuẩn hoá được event hoặc không cô lập được cấu hình người dùng → dừng, ghi lý do vào BRD |
| G-04 Bác bỏ | Không ảnh hưởng go; `steer=False`, UI ẩn ô steer |
| G-05 có lỗi phát lại 2 lần | Go nhưng tắt auto-resume cho Gemini tới khi bản CLI sửa; ghi phiên bản tối thiểu vào `runtime/compat.py` |

## 6. Phác thảo PR nếu go (để ước lượng, sẽ chỉnh theo kết quả spike)

1. **PR1 — Runtime theo run**: `make_runtime` → registry `{claude, gemini, fake}`; `agents.runtime_kind` (mặc định `claude`, migration DB);
   `RunManager` chọn runtime theo agent; `RunHandle.adapter` thành interface chung. Chưa có Gemini thật, test bằng `FakeRuntime`.
2. **PR2 — `GeminiStreamAdapter`**: JSONL Gemini → `HubEvent` (fixture từ spike); test bằng fixture, không gọi `gemini` thật.
3. **PR3 — `GeminiCliRuntime`**: dựng argv, env cô lập, secrets, raw log + che secret, stop/interrupt/resume, `auth_status`, `version`;
   **từ chối** run có trường Claude-only trong `RunSpec`; `capabilities()` đúng thực tế.
4. **PR4 — Guard cho Gemini**: nối hook `BeforeTool` vào guard (theo kết quả G-02/G-03); fail-closed khi hết giờ; audit `tool.write`.
5. **PR5 — UI/CLI**: chọn runtime ở form tạo agent; ẩn tính năng không hỗ trợ theo `capabilities()`; `cfharness doctor` kiểm `gemini` + key.
6. **PR6 — Tài liệu + release**: BRD, README (cài Gemini CLI), CHANGELOG.

Mỗi PR có test không gọi CLI thật.

## 7. Rủi ro

- **Gemini CLI thay đổi nhanh**: định dạng stream-json và hook có thể đổi giữa các bản → ghim phiên bản tối thiểu/tối đa trong `compat.py`, doctor cảnh báo.
- **Lớp an toàn yếu hơn Claude**: nếu G-02 chỉ "một phần", agent Gemini chỉ có quyền thấp. Không được hạ guard chung để vừa với Gemini.
- **Hai đường code**: mọi tính năng runtime mới về sau phải trả lời "Gemini thì sao?". Giảm bằng `capabilities()` và từ chối rõ ràng thay vì nhánh `if kind == ...` rải rác.
- **Google đổi chiến lược CLI**: Gemini CLI đã bị cắt với tài khoản cá nhân (18/06/2026); nếu Google bỏ luôn đường API key thì phải chuyển sang `agy`, vốn chưa cô lập được theo run. Theo dõi trong doctor (phiên bản + cảnh báo ngừng hỗ trợ).

## 8. Nguồn

- Hooks Gemini CLI (`BeforeTool` chặn/sửa lời gọi tool): https://geminicli.com/docs/hooks/
- Cấu hình, `--resume`: https://geminicli.com/docs/reference/configuration/
- Lỗi resume headless phát lại tool result 2 lần: https://github.com/google-gemini/gemini-cli/issues/29365
- javis-os (tham khảo cách một hệ khác chạy nhiều engine, gồm `agy` và Gemini API): https://github.com/blogminhquy/javis-os — `docs/10-models-va-engine.md`, `docs/dev/2026-11-antigravity-noi-mach-va-model-spec.txt`
- Gemini CLI ngừng phục vụ tài khoản cá nhân, chuyển sang Antigravity CLI: https://www.aibuilderclub.com/blog/google-kills-gemini-cli-june-18-2026 , https://avinashsangle.com/blog/gemini-cli-to-antigravity-cli-guide
- Spike tương ứng cho Claude: `docs/spikes/M0-spike-report.md`
