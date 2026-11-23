# Điều phối — hợp đồng v2 (GĐ1 của phương án B: island)

Mã: `src/cfharness/web/manager_feed.py` (route + `Feed` + `Converter`), `web/manager.py` (`page_ctx`, `workstreams_data` dùng chung với trang cũ),
`bus.py` / `daemon.py` (`detail_agent_id`), `web/demo_stream.py` (kịch bản `[demo]`). Test: `tests/test_web_manager_v2.py`.

Trang cũ (`GET /manager`, `/partials/manager*`, `/sse/manager`, `/manager/send|stop|read`) giữ nguyên tới GĐ3. Island (GĐ2, `?ui=next`) chỉ dùng
hợp đồng dưới đây + `POST /manager/send|stop|read` sẵn có.

## Kiến trúc

```
daemon ──events.subscribe{level:summary, detail_agent_id:<điều phối viên>, since_seq, epoch}──▶ Feed (MỘT mỗi tiến trình web)
                                                                                               │ Converter: event → message v2 (một lần)
                                                                                               │ ring RING_MAX=5000 / 8 MB, id = <boot>.<n>
                                                    trình duyệt ◀── /sse/manager/v2 (lọc theo phạm vi của tab) ◀─┘
```

- `Feed` bật lười ở request đầu (`/api/manager/snapshot` hoặc SSE), tự đóng subscription sau `IDLE_STOP_S=120` s không ai xem; bật lại thì
  đăng ký tiếp từ `hub_seq` cuối (ring/DB của daemon phát lại — không mất event).
- Subscription daemon: event MỌI agent ở mức `summary` (đủ cho trạng thái luồng, câu hỏi, duyệt) + event của điều phối viên tới mức `detail`
  (chữ chạy, suy nghĩ). Điều phối viên đổi (`manager.designated`) → đăng ký lại với `detail_agent_id` mới + `resync`.
- `msg.done` gộp đúng toàn bộ chữ đã chạy của lượt (Converter giữ bộ đệm theo run trong `Feed`, không theo kết nối) — trình duyệt nối lại giữa
  chừng vẫn nhận câu trả lời đầy đủ.

## `GET /api/manager/snapshot?c=&w=&new=&run=`

Cùng tham số và cùng dữ liệu với `GET /manager` (`manager.page_ctx`), RPC song song. `Cache-Control: no-store`, gzip.

```jsonc
{
  "v": 1,
  "cursor": "a1b2c3d4.120",          // id SSE lấy TRƯỚC khi đọc dữ liệu → mở SSE với ?since=<cursor>; null nếu không nối được daemon
  "boot": "a1b2c3d4", "epoch": "…",
  "manager": {"available": true, "agent_id": "dieu-phoi", "last_tick": "…", "next_tick": "…"},
  // chưa có điều phối viên: thêm "setup": {"agents": [{id, name}], "templates": ["manager", …]} và KHÔNG có các khoá dưới
  "conv": "mc_…" | null, "ws": "ws_…" | null, "scope": "ws_…" | "_",   // "_" = Sảnh
  "title": "…",
  "conversations": [{"id", "title", "updated_at", "count", "unread", "running"}],   // hội thoại web của Sảnh / của luồng đang mở
  "workstreams": {"enabled": true, "lobby": Tab, "active": [Tab], "closed": [Tab]},
  "items": [Item],                    // dòng thời gian, cũ → mới, đã có vạch ngày
  "pending": {"run": "r_…", "tool": "Đang dùng …", "todo": TodoView} | null,   // lượt đang trả lời (tải lại giữa chừng)
  "actions": [Action],                // việc chờ NGƯỜI của hội thoại: câu hỏi mở + thao tác phá huỷ chờ duyệt của các lượt thuộc hội thoại
  "side_actions": [ActionItem],       // "Hành động gần đây" (12, mới → cũ)
  "conv_runs": ["r_…"],
  "workstream": {"id", "key", "title", "label", "status", "sprint", "state", "tasks", "needs_human", "open_tasks", "recent_done"} | null,
  "ws_convs": [{"id", "title", "channel", "running", "updated_at"}]
}
```

`Tab` = `{"ws", "key", "title", "label", "status": "active|closed", "sprint", "dot": "danger|run|idle|done|none", "dot_label", "unread",
"needs_human", "tasks"}` (Sảnh: `ws = "_"`). `unread` là số toàn hub — client tự coi hội thoại đang xem là đã đọc và gọi `POST /manager/read`.

`Item` (mọi chữ là chữ THÔ — island gán textContent / htm tự escape; HTML DUY NHẤT là `html` của mục `manager`):

| kind | khoá |
|---|---|
| `day` | `id: "d<YYYY-MM-DD>"`, `key`, `text` |
| `user` | `id: "m<id>"`, `text`, `ts`, `run_id` |
| `manager` | `id: "m<id>"` (đã lưu) hoặc `"e<seq>"` (tới qua SSE), `text` (chữ gốc cho Sao chép/Sửa), `html` (`present.md` đã cứng hoá, cache theo id), `notify`, `tick`, `cut`, `agent`, `files: [{path, name}]` (chip Xem/Mở), `ts`, `run_id` |
| `actions` | `id: "g<audit_id|run>"`, `ok`, `items: [ActionItem]`, `ts`, `run_id` |
| `question` | `id: "q-<id>"`, `q: Question` |
| `note` | `text`, `tone`, `run_id` |

`ActionItem` = `{kind:"action", summary, tool, ok, error, ts, run_id, audit_id, conversation_id, target:{href, label}}` (`href` chỉ là link nội
bộ do `present.mgr_target` dựng). `Question` = `{id, agent_id, run_id, task_id, conversation_id, text, options:[{key,label,description}], multi,
allow_free_text, status, routed_to, created_at, escalation_note, answer, answered_by}`.
`Action` = `{"kind":"question", …Question}` hoặc `{"kind":"approval", id, run_id, agent_id, status, tool, text (toàn văn thao tác), reason,
created_at, href:"/approvals"}`.

## `GET /sse/manager/v2?tab=&c=&w=&since=`

- `tab`: khoá ngẫu nhiên của tab trình duyệt (sinh một lần mỗi trang). Phạm vi gắn với (phiên, tab), nhớ `WATCH_TTL_S=600` s sau khi ngắt →
  EventSource tự nối lại cùng URL không mất phạm vi đã thêm bằng `/api/manager/watch`.
- `c`, `w`: phạm vi ban đầu (lặp tham số hoặc phân tách bằng dấu phẩy; `w=_` = Sảnh). Tối đa 64 mỗi loại.
- Vị trí bắt đầu: header `Last-Event-ID` (EventSource tự gửi khi nối lại) hoặc `?since=<cursor>` (lần mở đầu, lấy từ snapshot). Không có → chỉ live.

Khung luồng: `retry: 3000`, `hello` (không id), rồi (nếu cần) `resync`, phần đã lỡ từ ring, rồi live. MỌI message (trừ `hello`) có
`id: <boot>.<n>`; `n` tăng đơn điệu trong một `boot`. Message ngoài phạm vi không gửi; ở nhịp ping (15 s) nếu đã bỏ qua message thì gửi khối
`id: <boot>.<n>\n: ping` (không data — theo WHATWG vẫn cập nhật last event id) để lần nối lại không quét lại phần đã bỏ.

Mỗi message là `data: {"t": "<loại>", …}` (không dùng trường `event:` — client chỉ cần `onmessage`). Khoá chung của message theo lượt:
`run`, `conv`, `ws`.

| `t` | phạm vi | dữ liệu |
|---|---|---|
| `hello` | — | `v`, `boot`, `cursor`, `epoch`, `tab` |
| `resync` | mọi tab | `reason`: `gap` (hở quá ring) · `boot` (web khởi động lại / id lạ) · `epoch` (daemon khởi động lại) · `manager` (đổi điều phối viên). Client tải lại snapshot; luồng tiếp tục từ id của chính message `resync` |
| `run.start` | hội thoại/luồng | `state: queued|started` |
| `msg.delta` | hội thoại/luồng | `text` — CHỮ THÔ, không markdown phía server. Island gom theo requestAnimationFrame vào streaming-markdown + DOMPurify |
| `msg.seg` | hội thoại/luồng | hết một đoạn (giữa các lần dùng tool) |
| `think.delta` | hội thoại/luồng | `text` (chữ thô) |
| `tool.start` | hội thoại/luồng | `id`, `name`, `label` (chữ thô) |
| `tool.end` | hội thoại/luồng | `id`, `name`, `error` |
| `todo` | hội thoại/luồng | `todo` (`present.todo_view` — chữ thô) |
| `msg.done` | hội thoại/luồng | `item` (Item `manager`, `html` đã cứng hoá — MỘT lần mỗi câu trả lời; thay khung đang chạy chữ của `run`) |
| `act.item` | hội thoại/luồng | `item` (ActionItem) — gom vào khối "Đã làm N việc" cùng `run` |
| `action.pending` | hội thoại/luồng | `action` (Action: câu hỏi `question.open|escalated` của hội thoại, thao tác phá huỷ chờ duyệt `approval.waiting_human` của lượt điều phối viên) |
| `action.resolved` | hội thoại/luồng | `kind: question|approval`, `id`, `status` (`answered|cancelled` / `allowed|denied`), `answer?`, `answered_by?` |
| `run.end` | hội thoại/luồng | `ok`, `outcome`, `label`, `tone` |
| `ws.status` | mọi tab | một `Tab` đầy đủ (chỉ phát khi đổi; gom `WS_DEBOUNCE_S=0.3` s; nguồn: run/task/câu hỏi/duyệt của MỌI agent, `workstream.updated`, `manager.reply`) — thay vòng hỏi lại 20 s |
| `tabs.patch` | mọi tab | `active: [ws]`, `closed: [ws]` — thứ tự/tập tab đổi (mở, đóng, xoá luồng) |
| `tick` | mọi tab | nhịp điều phối viên (`state`) — làm mới khối trạng thái |

"Hội thoại/luồng": gửi khi `conv` thuộc `c` của tab HOẶC `ws` thuộc `w` của tab. Lượt điều phối viên lấy `conv`/`ws` từ
`run_opts.manager_conversation|manager_workstream` (cache 512 run); hội thoại Sảnh có `ws = "_"`.

Không trùng phía client: id SSE (bỏ message có `n` ≤ `n` cuối đã xử lý), và mục dòng thời gian theo `id` (`msg.done` của một `run` đã có trong
snapshot dạng `m<id>` → bỏ; snapshot luôn thắng khi resync).

## `POST /api/manager/watch` (form: `csrf`, `k`, `tab`, `c`, `w`, `op=add|set`)

`post_guard` (CSRF + Origin + khoá phiên). `add` (mặc định) thêm vào phạm vi; `set` thay. Trả `{"ok": true, "c": [...], "w": [...]}`. Gọi khi mở
tab/hội thoại mới lần đầu (sau khi tải snapshot của nó).

## `POST /api/manager/act/<approve|deny|answer>` (form như `/office/do/<tên>` + `idem`)

Cùng `plan_action` với 2D/3D: thiếu xác nhận → `{"need": "confirm"|"confirm2", …}` (gửi lại với `confirm=yes` / `confirm2=yes`). Kết quả CUỐI
(không phải bước xác nhận) nhớ `IDEM_TTL_S=600` s theo (phiên, tên, `idem`): gửi lại cùng `idem` → cùng thân + `"replayed": true`, không gọi RPC
lần hai. Island: sinh `idem` một lần mỗi lần bấm, hiện lạc quan, đối chiếu bằng `action.resolved`, hoàn tác khi lỗi. Duyệt thao tác phá huỷ có
luật "luôn cho phép" / bước 2 → `href` tới trang Duyệt.

## Hub demo

`uv run python -m cfharness.web.demo --home <thư mục tạm> --port 88xx` rồi gửi cho điều phối viên một tin có `[demo]`: lời dẫn → TodoWrite →
Read → Bash → 2 thẻ hành động + 1 thao tác phá huỷ chờ duyệt + 1 câu hỏi trong hội thoại → câu trả lời markdown (bảng, code, đường dẫn tệp)
chạy chữ ~100 token/s (`demo_stream.TOKEN_S`). Đi qua adapter stream-json thật → event giống run thật.

## Giới hạn / việc của GĐ2–GĐ3

- Đổi tab không mở lại SSE: tab đã mở giữ phạm vi; tab mới → snapshot (nếu chưa có) + `watch add`.
- `ws.status.unread` là số toàn hub (một người dùng); trang cũ `_viewing` → island tự xử lý hội thoại đang xem.
- Câu hỏi/duyệt của agent KHÁC trong luồng (không phải lượt điều phối viên) chưa lên trang này (như trang cũ) — chỉ làm chấm `danger` của tab.
- `/sse/manager` (cũ) và `/partials/manager/tabs` gỡ ở GĐ3 khi island thành mặc định.
