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

## GĐ2 phần 1 — island `/manager?ui=next`

Mã: `web/coord.py` (trang + import map + CSP), `templates/manager_next.html`, `static/coord/*.js`, `static/coord.css`, `static/vendor/README.md`
(phiên bản ghim, sha256, giấy phép). Test: `tests/test_coord_island.py` + `tests/coord_store_check.mjs` (Node, store/reducer + client SSE).

- Trang: `base.html` + `#coord` + ảnh chụp đầu nhúng `<script type="application/json" id="coord-boot">` (`|tojson`). Chưa có điều phối viên →
  trang cũ. Import map nội tuyến → CSP của RIÊNG trang này có `'sha256-…'` (tính trên đúng chuỗi import map); module island import nhau bằng
  đường dẫn tương đối, import map ánh xạ từng `/static/coord/<tệp>.js` sang bản `?v=` (immutable). `.mjs` đăng ký `text/javascript`
  (Windows mặc định text/plain → trình duyệt từ chối module).
- Module: `util.js` (thuần), `store.js` (signal tiêm vào → chạy dưới Node; reducer trả "hiệu ứng" cho main), `sse.js` (EventSource tiêm vào;
  bỏ trùng theo id, lỗi cứng → mở lại với `since=<id cuối>`), `api.js` (csrf + khoá phiên `k`), `views.js` + `composer.js` (Preact + htm),
  `main.js` (điều hướng, watch, read, status, gửi/dừng).
- Khung (pane) = một hội thoại đã mở (hoặc hội thoại mới của một luồng). Khung giữ trong DOM (`hidden`), đổi tab = đổi khung + pushState;
  khung chưa có → khung tạm "Đang tải…" (chỉ hiện sau 400 ms) + `/api/manager/snapshot` + `POST /api/manager/watch` (luồng SSE không mở lại).
  Vị trí cuộn lưu theo khung (Chrome mất scrollTop khi display:none). MỘT ô nhập, gắn khung đang hiện: nháp ở store + sessionStorage (cùng
  khoá `cfh_mgr_draft:…` với trang cũ), tệp đính kèm (File) ở store. Giữ id `#mgr-form/#mgr-text/#mgr-mic/#mgr-voice` + sự kiện `mgr:*` →
  `voice.js` chạy không đổi.
- Gửi lạc quan: tin người + khung chờ hiện ngay; message SSE tới trước phản hồi `/manager/send` (hội thoại mới chưa có id) giữ ở
  `store.early` rồi phát lại khi biết run/hội thoại; lỗi → rút tin, trả chữ + tệp về ô nhập.
- Đọc: island gọi `POST /manager/read` khi hội thoại đang hiện; server sau đó `feed.kick_tabs()` → `ws.status` có số chưa đọc đúng.
- Thêm vào hợp đồng: `GET /api/manager/status` (`manager_json`: tên, trạng thái + nhãn/tông, chi phí hôm nay, nhịp), snapshot `manager` cùng
  dạng, `workstream` thêm `created_at/created_by/closed_at/last_activity/state_updated_at/conversations`; `/api/manager/*` hết phiên → 401 JSON.
- Phần 2: xem mục "GĐ2 phần 2" bên dưới.
- Đo trên hub demo (Chrome, Playwright): mở luồng lần đầu ~41 ms (tới khi vẽ xong), đổi tab đã mở 10–16 ms, gửi → tin hiện 11 ms,
  không long task > 50 ms khi chạy chữ `[demo]`. JS island ~40 KB gzip (vendor ~11 KB) — vượt ngân sách 30 KB (không có bước build;
  ~6 KB là chú thích), thu gọn ở GĐ3.

## GĐ2 phần 2 — dòng thời gian đủ tính năng

Mã mới: `static/coord/scroll.js` (cuộn dính đáy — THUẦN), `static/coord/md.js` (chạy chữ markdown), thêm vào `util.js` (`dayLabel`,
`safeUrl`, `mergeTimeline`, `rafBatch`, `newIdem`), `store.js` (duyệt/trả lời lạc quan, `p.hide`/`p.cv`, tín hiệu dẫn xuất), `views.js`
(Flow/Row/More, Approval, LiveMd). Test: `tests/coord_timeline_check.mjs` (Node) + `tests/test_coord_island.py`.

- **Vẽ lại tối thiểu.** Chữ chạy chỉ đổi `p.pending`; khung/ô nhập/dòng thời gian đọc tín hiệu dẫn xuất (`p.runId`, `p.busy`, `p.empty` —
  `computed`, chỉ báo khi GIÁ TRỊ đổi) → mỗi khung hình chỉ `PendingSlot` vẽ lại. Mục dòng thời gian là `Row` (`shouldComponentUpdate`
  theo object mục) → 2000 mục không bị duyệt lại khi một mục đổi.
- **Chạy chữ.** Delta gom ở store, `rafBatch` đổ MỘT lần mỗi khung hình; `LiveMd` đẩy phần chữ MỚI vào `streaming-markdown` (parser tăng
  dần, chữ luôn là text node). `set_attr` bọc lại: URL qua `safeUrl` (chỉ http(s)/mailto/tương đối; chặn `javascript:`, `data:`, `//host`)
  + `DOMPurify.isValidAttribute`; link mở tab mới `rel=noopener noreferrer nofollow`; lớp code chỉ `language-[\w+-]`. `msg.done` → mục
  `manager` với HTML server (present.md) thay khung đang chạy.
- **Cuộn dính đáy** (`scroll.js`): ở đáy → dính; ResizeObserver trên `.coord-flow` + `pinNow` trong layout effect của Flow/PendingSlot (ghim
  TRƯỚC khi vẽ — nếu chỉ dựa ResizeObserver, Chrome đã tính mục bị đẩy khỏi màn hình là một lần dịch chuyển). Chỉ bỏ dính khi NGƯỜI cuộn
  lên (lăn chuột/vuốt/phím/kéo thanh cuộn trong 400 ms qua) — trình duyệt kẹp scrollTop khi ô nhập co lại thì không. Không dính + nội dung
  lớn thêm → nút "Tin mới nhất" (`p.jump`). Vị trí cuộn nhớ theo khung.
- **Hội thoại dài.** Mở chỉ vẽ `FIRST_SHOW=60` mục cuối (`p.hide` = số mục đầu chưa vẽ); mốc `More` ở đầu + IntersectionObserver (cách 1500 px)
  vẽ thêm 200 mục; `overflow-anchor: auto` (trừ khung đang trả lời) giữ nguyên phần đang xem khi chèn phía trên. `content-visibility: auto`
  chỉ cho mục CŨ của ảnh chụp (trừ 12 mục cuối, `p.cv` cố định theo mục — đổi qua lại visible/auto khi mục ở ngoài màn hình làm mất cỡ đã
  nhớ → scrollHeight nhảy).
- **Duyệt / trả lời lạc quan.** Thẻ thao tác phá huỷ: Cho phép một lần / Từ chối (ghi chú) → bước xác nhận nội tuyến → `POST
  /api/manager/act/approve|deny` với `confirm=yes` + `idem` (sinh mỗi lần bấm). Thẻ hiện kết quả ngay (`busy`); `action.resolved` chốt; lỗi →
  hoàn tác (giữ lựa chọn/chữ đã nhập), mạng lỗi → giữ `idem` để bấm lại cùng khoá (server trả kết quả cũ, không chạy hai lần); bước 2 (thao
  tác nặng) / luôn cho phép / theo luật → trang Duyệt. Câu hỏi: form gửi `POST /api/manager/act/answer` (không tải lại trang). Thẻ duyệt
  xếp theo giờ tạo (`mergeTimeline`), thẻ đã xử lý còn lại dạng thu gọn tới lần tải ảnh chụp sau.
- **Khác.** Nhãn vạch ngày như `present.day_label`; "Đang dùng X (+n)…"; khối "Đã làm N việc" quay khi lượt đang chạy; chỉ tin cuối có
  "Thử lại"; chip tệp thêm "Không gian số" khi có `fh`; dải ESTOP (`/api/manager/status` → `hub: {estop, admin_estop}`, cùng nguồn huy hiệu
  menu); nút mic của ô trả lời câu hỏi do island vẽ (`data-voice-for`, voice.js chỉ bỏ `hidden`) và giữ chỗ; danh sách hội thoại ở thanh
  bên vẽ sẵn ở server rồi island thay trong cùng tác vụ; chấm "đang chạy" và mic dùng `::after` transform/opacity thay animation box-shadow
  vô hạn; announcer `#mgr-status-live` báo khi có trả lời.
- **Đo trên hub demo** (Chrome headless, 1280×860): gửi → tin + khung chờ hiện 15–26 ms; chạy chữ `[demo]` ~100 token/s không long task
  > 50 ms, vẫn dính đáy (cách đáy 0 px); lăn chuột lên → bỏ dính + hiện nút, bấm nút → về đáy; CLS cả lượt 0.0016; trả lời câu hỏi / cho
  phép / từ chối không tải lại trang; đổi tab đã mở ≤ 37 ms tới khi vẽ (2 rAF), mở tab mới 13–17 ms; nháp + vị trí cuộn giữ nguyên;
  2000 mục: mở 45 ms tới khi vẽ (trước khi vẽ dần: 337 ms), cuộn lên tới hết không nhảy; khởi động island (`performance.measure
  ("coord:boot")`) ~70 ms khi đã cache cho hội thoại demo ~120 mục.
- **Còn lại (GĐ3):** JS island ~50 KB gzip (coord ~38 KB, vendor ~11 KB) (ngân sách 30 KB — cần minify/bỏ chú thích khi phục vụ); voice.js MutationObserver vẫn quan sát
  cả `body`.

## Sửa sau verify vòng 1

- **Trang `?ui=next` có phạm vi luồng** (`?w=`, `?c=` hội thoại thuộc luồng, `?new=1&w=`) trả 500: template dùng `|first`, mà Jinja của
  web ghi đè `first` bằng `P.first_line`. Luồng đang xem (`wst`) giờ tính ở `coord.coord_page`.
- **Ảnh chụp ↔ luồng SSE:** `GET /api/manager/snapshot?tab=<khoá tab>` đăng ký phạm vi của đích (`c`, hoặc `w`/Sảnh khi chưa biết hội thoại)
  cho luồng SSE của tab TRƯỚC khi lấy `cursor`, rồi thêm hội thoại ảnh chụp chọn. Client: khung đang tải (`store.beginLoad`) GIỮ message
  của nó; `applySnapshot(snap, p, fx)` phát lại phần có id > `cursor` (phần ≤ cursor ảnh chụp đã gồm) và trả hiệu ứng (đã đọc, báo).
  Tải bị huỷ (đổi tab nhanh, resync) hoặc lỗi → `store.endLoad` đánh dấu khung cũ → mở lại là tải lại (hết kẹt "Đang tải…").
  `POST /api/manager/watch` chỉ còn dùng khi gửi tin đầu tạo hội thoại mới; client ghi nhận phạm vi chỉ khi server nhận (lỗi → thử lại).
- **`Watch` của luồng đang chạy:** `seen` làm mới MỖI vòng của `stream()` (không chỉ ở nhịp ping); `live` = số luồng đang giữ — dọn TTL bỏ
  qua; `op=set` đặt lại tại chỗ; quá `MAX_WATCH` bỏ id cũ nhất (dict theo thứ tự thêm).
- **Subscription daemon tắt khi rảnh dù hub im lặng:** `Feed.reap_later()` hẹn giờ (sau `ensure()` và khi luồng cuối đóng) huỷ task khi
  `idle()`.
- **`/api/manager/act`:** `post_guard` trước cache idem; chỉ cache 2xx/409.
- **Ghi chú lượt lỗi** trong ảnh chụp có id `n<run>` (cùng id client tự thêm khi `run.end` lỗi) → không lặp.
- **"Hành động gần đây" theo khung:** mỗi khung giữ `side` của phạm vi nó (server lọc theo luồng); `act.item` (mang `ws`) chỉ vào khung cùng
  phạm vi; `store.side` = của khung đang hiện.
- **Tin người từ client khác:** v2 không có event tin người → `msg.done` của lượt mà khung không có tin người tương ứng (không phải tick /
  notify, khung không đang gửi) → tải lại nền ảnh chụp của khung đang hiện (khung ẩn: đánh dấu cũ). Đồng thời thay id/chữ tạm bằng bản đã lưu.
- **CLS khi cuộn lên 2000 mục:** mục cũ mang lớp `cvx` (`contain-intrinsic-size: auto …`) nhưng `content-visibility: auto` chỉ bật qua
  `data-cv` HAI khung hình sau khi mục được dựng đầy đủ (`views/cvArm`) → cỡ đã nhớ là cỡ thật. `.coord-more` loại khỏi ứng viên neo cuộn
  (neo vào nó thì mục chèn ngay dưới không được bù). Đo lại (Playwright, lăn chuột thật 60 × 600px lên rồi xuống lại): CLS 0.00001.
- **Nhãn lạc quan thẻ duyệt:** "đang cho phép…" / "đang từ chối…" thay "đang gửi…".
- **Gửi khi khung chưa tải xong** bị chặn (giữ ô nhập, báo "Đang tải hội thoại") — ảnh chụp tới sau sẽ xoá tin lạc quan. Resync khi khung
  tạm `?w=` chưa tải tải lại đúng đích ban đầu (`p.nav`), không thành "hội thoại mới".
- **JS phục vụ đã thu gọn:** `GET /static/coord/<tệp>.js` (route `coord.coord_js`, đứng trước mount `/static`) trả bản `web/jsmin.py` (bỏ
  chú thích/thụt lề/khoảng trắng thừa; chuỗi, template `htm`, regex giữ nguyên; không đổi tên → ngữ nghĩa giữ nguyên; test chạy lại
  store/SSE trên bản thu gọn + `node --check`). gzip: coord 38.6 → 28.3 KB; cộng vendor ~11.2 KB = **~39.5 KB**, vẫn trên ngân sách 30 KB.
  Xuống 30 KB cần minifier đổi tên biến (một bước build) hoặc chốt ngân sách mới — cần người dùng quyết. Test chặn hồi quy ở 42 KB.

### Sửa lỗi verify vòng 2

- **Nối lại SSE sau khi ngắt lâu hơn `WATCH_TTL_S` (ngủ máy) không mất phạm vi:** `coord/sse.js` KHÔNG để EventSource tự nối (nó dùng lại
  URL ban đầu, chỉ có phạm vi lúc mở trang — server đã dọn phạm vi của tab thì tạo lại chỉ với phạm vi đó → mất `msg.*` của mọi khung mở
  sau). Mọi `onerror` (kể cả CONNECTING) → đóng, tự mở lại (giãn cách 1 → 15 s) với URL mang ĐỦ phạm vi hiện tại + `since=<id cuối>`;
  server phát lại từ ring theo phạm vi mới (test `test_coord_verify_r2` + `coord_store_check.mjs`).
- **Luồng chết lặng:** nhịp giữ luồng giờ là message thật `data: {"t":"ping"}` (vẫn kèm `id` khi phần ngoài phạm vi đã tiến); client canh
  im lặng: quá `QUIET` (40 s, > 2 nhịp 15 s) không có gì → mở lại với `since`.
- **Tải lại nền lỗi:** `store.endLoad` phát lại phần SSE đã giữ lên khung đã có dòng thời gian (không mất `msg.done`/`run.end`, khung chờ
  không treo) và trả hiệu ứng; main.js thử lại (1, 2, 4… s, tối đa 5 lần) khi khung vẫn đang hiện. `refresh()` đi qua `load()` (một lượt tải
  mỗi lúc qua `navCtl`) — không còn hai ảnh chụp đua nhau ghi một khung; đường `setup` gọi `endLoad`.
- **"Hành động gần đây" của cả luồng:** `Scope.side` cho `act.item`; `Watch.side` = luồng (Sảnh `_`) của các hội thoại đã mở (lấy từ
  `conv_ws`) → tab chỉ mở một hội thoại vẫn nhận hành động của hội thoại khác cùng luồng.
- **CLS khi cuộn nhanh 2000 mục (kéo thanh cuộn / vuốt mạnh):** đo lại 14 → 0.001. Ba nguyên nhân, ba sửa:
  1. bù `scrollTop` khi chèn mục cũ phía trên (và neo cuộn của trình duyệt — còn bù thêm theo vị trí cũ khi bật lại) làm toạ độ các mục đã
     vẽ đổi lúc chúng ở ngoài màn hình → Chrome tính dịch khi cuộn tới. Nay `.coord-more` là KHOẢNG GIỮ CHỖ cao ≈ số mục chưa vẽ × cỡ trung
     bình (thanh cuộn đúng độ dài thật); chèn một đợt thì khoảng này co đúng bằng phần chèn (đo trước khi vẽ) → mục đã có giữ nguyên toạ
     độ. Neo cuộn của trình duyệt TẮT trên `.mgr-log` của island. Nút "Hiện N mục trước" ở đỉnh khoảng giữ chỗ (đỉnh không dời);
  2. Chrome chỉ tự nhớ cỡ (`contain-intrinsic-size: auto`) của mục đã được vẽ khi có content-visibility → mục vẽ dần ở ngoài màn hình nhận
     400px mặc định rồi co lại khi cuộn tới. Nay cỡ thật đo được ghi thẳng vào `contain-intrinsic-size` của mục;
  3. mục bị bỏ qua (content-visibility) hiện ra trong MỘT khung hình → Chrome tính phần bên trong là dịch (toạ độ cũ đã cũ). Nay
     content-visibility chỉ bật cho mục XA màn hình (> 4000 px, IntersectionObserver `views/cvArm`); gần thì vẽ thật. Chữ chạy khi đã vẽ hết
     2000 mục vẫn ~17 ms/khung hình.
  Thêm: cuộn lên xa hơn một màn hình dù không đo được là người (script, công cụ hỗ trợ) → bỏ dính đáy (trước: ghim đáy kéo về cuối);
  `MORE_STEP` 200 → 60 (đợt vẽ dần ~30 ms thay 60–90 ms); `p.el` gắn bằng ref callback (có trước mọi useLayoutEffect); khung "đang trả
  lời" là anh em của `.coord-flow` trong `.coord-body`.
- **"Hành động gần đây" không đẩy dòng cũ:** dòng theo vị trí (key = chỉ số) và một dòng chữ (đầy đủ ở `title`) → hành động mới chỉ đổi chữ
  của các ô sẵn có (đo: 15 hành động liên tiếp tổng CLS 0.003).
- **Khởi động:** panel phải và danh sách hội thoại bên trái vẽ ở tác vụ sau khung hình đầu (`ui.late`). Long task lúc mở trang còn ~85–95 ms
  (headless) — phần lớn là nạp/chạy module, không phải dựng giao diện (thử dựng 12 mục đầu trước: không đổi).
- **jsmin:** `/` sau `++`/`--` hậu tố là phép chia; docstring ghi giới hạn `/` sau `}`.
- **Còn mở:** ngân sách JS island 30 KB gzip — coord đã thu gọn ~30.2 KB + vendor ~11.2 KB ≈ 41.4 KB (test chặn ở 42 KB); cần người dùng chốt (tính vendor hay
  không, hay thêm bước build đổi tên biến). GĐ3 chưa làm.

## Verify vòng 3 + GĐ3 (chuyển mặc định)
- **Nhịp ping khi hub bận ngoài phạm vi:** `manager_feed.stream` ping theo HẠN (`heartbeat_s` kể từ message có data cuối cùng), không
  theo "wait hết giờ" — trước đây event của luồng khác đánh thức `wait()` liên tục nên không bao giờ ping và watchdog 40 s của client nối
  lại vô ích. Test: `tests/test_coord_verify_r3.py`.
- **Tải lại nền khi đang đọc tin cũ** (không dính đáy): `applySnapshot` giữ số mục đang vẽ + khoảng giữ chỗ (không nhảy chỗ đọc).
- **`retryLater` / resync** dùng chung `navOf(p)` → khung phạm vi (Sảnh / `?w=` chưa có hội thoại) cũng được thử lại.
- **CLS khi End rồi vuốt mạnh lên (0.18):** `views/cvNear` — trong sự kiện scroll (trước khi vẽ khung hình) gỡ `data-cv` của mục trong
  ±1 màn hình (tìm nhị phân theo DOM) thay vì chờ IntersectionObserver trễ một khung. Đo (Chrome headless, 2000 mục, scrollTop):
  chuỗi của verify r3 0.0004; cả phiên cuộn 0.0019.
- **Khoảng trắng dư ở đầu:** `views/fitSpacer` — vẽ hết rồi thì gỡ phần dư của khoảng giữ chỗ khi không ai thấy (mốc khuất trên → co + bù
  scrollTop; đang nhìn khoảng trống → co không bù, nội dung vẫn khuất). Đo: dư 396 px thay vì hàng trăm nghìn px. KHÔNG co khi còn vẽ
  dần: co giữa lúc cuộn nhanh làm Chrome tính mục `cvx` là dịch (đo 0.32) → lúc đó thanh cuộn còn dài hơn thật (ước lượng theo mục mới).
- **GĐ3:** `/manager` = island. Lối thoát MỘT bản phát hành: `?ui=old` (cookie `ahub_mgr_ui=old`, path `/manager`; `?ui=new` quay lại;
  yêu cầu `X-Ahub-Partial: grid` của app.js cũ luôn là trang cũ). Chưa có điều phối viên → vẫn phần chỉ định của `manager.html`.
  Vì còn lối thoát nên CHƯA xoá trang cũ (`manager.html`, `_manager*.html`, `initManager`/`mgrBoot`, `/sse/manager`, `/partials/manager/tabs`,
  `_converter`); test của trang cũ chạy với fixture `old_manager_ui` (conftest). Bản sau: gỡ hết những thứ đó + fixture, chuyển phần chỉ
  định điều phối viên sang island. `/partials/manager` (thẻ Điều phối ở Tổng quan) và `/manager/send|stop|read` island vẫn dùng — giữ.
- voice.js: MutationObserver chỉ theo dõi `#coord` (hoặc cha của `#mgr-grid`), bỏ qua thay đổi trong `.md`. Chấm "đang chạy" và mic dùng
  `::after` transform/opacity cho MỌI trang (app.css), bỏ `ws-ping`/`voice-pulse` box-shadow.
- **Còn mở:** ngân sách JS 30 KB gzip (coord ~30.6 + vendor ~11.1 ≈ 41.7 KB, chặn ở 42 KB) — chờ người dùng chốt; `msg.done` có đoạn mở
  đầu trước tool mà ảnh chụp bỏ (chữ đổi khi tải lại); long task 50–75 ms khi cuộn rất nhanh xuống qua 2000 mục đã vẽ hết.
