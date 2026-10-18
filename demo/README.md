# agent-hub — bản demo TUI "htop cho agent"

Bản demo **chạy được** của giao diện terminal theo `research/00-de-xuat-kien-truc.md` §3.4 và
`research/notes/05-ui-observability.md` §7. Dữ liệu **giả lập**: không gọi `claude` thật, không tốn tiền.
Nhưng kịch bản được sinh dưới dạng **các dòng stream-json giống hệt `claude -p --output-format stream-json`**
rồi đi qua adapter thật → event bus → TUI, nên đường ống là đường ống của bản thật.

## Chạy

```bash
cd demo
uv run agent-hub-demo            # hoặc: uv run python -m agent_hub_demo
uv run agent-hub-demo --fast     # kịch bản chạy nhanh hơn ~3 lần
uv run pytest                    # 22 test: adapter, bus, simulator, giao diện (Pilot) + chụp SVG
```

Python 3.12–3.13 (ghim bằng `.python-version` = 3.13, vì máy mặc định là 3.14). Phụ thuộc: `textual`, `pydantic`.

## Màn hình & phím tắt

| Màn | Phím | Việc |
|---|---|---|
| **Bảng htop** | `↑↓` | chọn agent |
| | `Enter` | attach — xem transcript live |
| | `a` / `d` | duyệt / từ chối yêu cầu đang chờ của agent được chọn |
| | `s` | steer (mở attach kèm ô nhập) |
| | `x` | dừng agent |
| | `t` | lọc theo team (Tất cả → Nghiên cứu → Vận hành) |
| | `/` | lọc theo chữ (tên/task/team); `Esc` xoá lọc |
| | `l` | tab Learnings |
| | `n` / `e` / `Delete` | tạo / sửa / xoá agent (form: tên, vai trò, team, model, MCP servers) |
| | `q` | thoát |
| **Attach** | `Esc` | về bảng (agent vẫn chạy) |
| | `a` / `d` | duyệt / từ chối |
| | `s` | gửi chỉ đạo (hiện trong transcript `› Bạn (steer): …`) |
| | `x` | dừng |
| | `r` | mở/thu gọn khối suy nghĩ |
| **Learnings** | `a` / `d` / `r` | duyệt / từ chối / rollback đề xuất; `Esc` về bảng |

Màn chính: header tổng (số agent theo trạng thái, tokens, $ hôm nay, số đề xuất chờ duyệt) · bảng
`ID TÊN TEAM TRẠNG THÁI TASK TOOL-CUỐI T TOK $` với `● làm việc / ◐ chờ duyệt / ○ rảnh / ✖ lỗi / ■ đã dừng` ·
dòng chi tiết agent được chọn (yêu cầu đang chờ, lỗi) · panel "Hoạt động gần đây" lấy từ kênh **firehose**
(chỉ event tóm tắt, không có delta token).

Attach: khi mở, màn hình **replay toàn bộ ring buffer** của agent (dòng đầu ghi `phát lại N event, seq 1→k, epoch`)
rồi nối live — không hở, không trùng. Suy nghĩ hiển thị thu gọn/mờ; tool dạng `┊ ⚡ Bash ls (0.4s)` + dòng kết quả
rút gọn `↳ …`; lệnh đang chờ duyệt hiện khung vàng + banner; thanh dưới: `model · ctx% · tok/s · tok · $ · seq · phiên`.

Kịch bản 5 agent / 2 team:

| ID | Tên | Team | Lúc mở app | Kịch bản |
|---|---|---|---|---|
| a1 | researcher | Nghiên cứu | ● đang chạy tool | tra cứu văn bản CĐS qua MCP `hthong-tlkc-don-vi` (vb_tu_dien_truong → vb_tim → subagent → vb_doc) |
| a2 | writer | Nghiên cứu | ● đang stream chữ | soạn báo cáo tuần đợt 100 ngày CĐS (MCP `cds-100-ngay`) |
| a3 | coder | Vận hành | ◐ chờ duyệt | sửa lỗi lọc nhiệm vụ quá hạn; xin chạy `rm -rf build/ dist/ .pytest_cache` — duyệt/từ chối dẫn tới 2 nhánh khác nhau |
| a4 | reviewer | Vận hành | ○ rảnh | rà soát PR, lặp lại định kỳ |
| a5 | ops | Vận hành | ✖ lỗi | kiểm tra MCP, `theo-doi-cchc` ECONNREFUSED → `result` lỗi; lần sau thành công |

Định kỳ (mặc định 30 s) hub giả phát thêm `learning.proposal`. Chính sách theo BRIEF quyết định 2: đề xuất tầng
**agent** (không phải xoá skill) → tự áp dụng (`learning.applied by auto (eval …)`); tầng **team**, xoá skill → chờ duyệt.

## Cấu trúc & phần nào tái dùng cho bản thật

```
src/agent_hub_demo/
  events.py          envelope Pydantic {v, agent_id, session_id, run_id, seq, epoch, ts, type, payload}  → TÁI DÙNG nguyên
  claude_adapter.py  1 dòng stream-json của `claude` → 0..n HubEvent (có trạng thái theo lượt)          → TÁI DÙNG nguyên
  bus.py             EventBus (ring theo agent: giới hạn số event + byte, seq/epoch, fanout queue        → TÁI DÙNG; thêm
                     có giới hạn — viewer chậm bị cắt, firehose không delta) + Aggregator (bảng htop,      log bền SQLite/JSONL
                     learnings)
  simulator.py       FakeHub: sinh dòng stream-json giả + điều khiển (approve/deny/steer/stop, CRUD,       → THAY bằng hub thật
                     learnings). API của FakeHub = bản nháp API control của hub thật                        (spawn `claude -p`)
  app.py             TUI Textual: HtopScreen, AttachScreen(Transcript), LearningsScreen, form/xác nhận       → TÁI DÙNG phần lớn; đổi
                                                                                                            `app.hub` sang client WS
tests/               adapter trên file mẫu THẬT research/notes/claude-stream-sample.jsonl, bus replay,
                     simulator, Pilot (Enter/Esc/l/duyệt/form) + lưu ảnh vào screenshots/
```

Để chuyển sang bản thật: thay `FakeHub` bằng một worker spawn
`claude -p "<task>" --output-format stream-json --verbose --include-partial-messages --session-id <uuid> …`,
đọc stdout từng dòng → `ClaudeStreamAdapter.feed()` → `EventBus.publish()`. UI chỉ đọc `bus` + `agg` và gọi
các hàm điều khiển — giữ nguyên chữ ký `approve/deny/steer/stop/create_agent/...`.

## Ánh xạ event `claude` stream-json → event hub

| Dòng stream-json của `claude` | Event hub |
|---|---|
| `system/init` (session_id, model, tools, mcp_servers, cwd) | `turn.start` + `agent.status{working}` |
| `system/status`, `system/hook_*`, `system/thinking_tokens`, `rate_limit_event` | bỏ qua (hook của máy — hub nên cô lập cấu hình) |
| `stream_event` `message_start` (usage) | `usage.tick` (ước tính: tokens, context %, tok/s, $) |
| `stream_event` `content_block_delta` `text_delta` | `message.delta{text}` |
| `stream_event` `content_block_delta` `thinking_delta` | `reasoning.delta{text, estimated_tokens}` |
| `stream_event` `message_delta` (output_tokens) | `usage.tick` |
| `assistant` có `tool_use` | `tool.start{tool_use_id, name, input, summary}` (+ `subagent.spawn` nếu tool là `Task`/`Agent`) |
| `assistant` có `text`/`thinking` mà **không** có partial delta trước đó | `message.delta` / `reasoning.delta` (không nhân đôi khi đã stream) |
| `user` có `tool_result` | `tool.complete{tool_use_id, name, duration_s, is_error, summary}` |
| `result` (`total_cost_usd`, `usage`, `modelUsage.contextWindow`, `duration_ms`, `num_turns`) | `usage.tick` (chuẩn) + `turn.complete` + `agent.status{idle|error}` (+ `error` nếu `is_error`) |
| — (hook PreToolUse/policy của hub) | `request.open` / `request.cancel` |
| — (hub: steer qua `--input-format stream-json`) | `user.message` *(loại mở rộng ngoài danh sách tối thiểu)* |
| — (hub: harness tự cải thiện) | `learning.proposal` / `learning.applied` |
| — (hub: dừng tiến trình) | `turn.complete{interrupted}` + `agent.status{stopped}` |

## Hạn chế của bản demo

- Không có daemon/WebSocket: TUI và hub giả chạy chung một process (bản thật: hub là daemon, UI attach qua wire).
- `request.open` (duyệt lệnh) do kịch bản phát; bản thật cần hook PreToolUse hoặc `--permission-prompt-tool` để chặn và chờ.
- Steer chỉ phản hồi một câu mẫu; dừng (`x`) là huỷ task giả, không có SIGINT thật.
- Chi phí/tok/s giữa lượt là ước tính theo bảng giá thô; số chuẩn lấy ở `result`.
- Không có log bền (SQLite/JSONL): ring buffer đầy thì báo `truncated`, chưa có nơi đọc tiếp.
- Sửa agent chỉ đổi metadata hiển thị; không sinh lại persona/cấu hình MCP.
