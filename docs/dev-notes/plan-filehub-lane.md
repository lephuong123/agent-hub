# Plan: cổng cfharness — thư mục chung Không gian số không qua đồng bộ (`filehub` chế độ `lane`)

Ngày: 2026-11-17. Trạng thái: H1–H4 ĐÃ LÀM trên fake (§10); nghiệm thu thật chờ K1–K4. Nhánh `feat/filehub-lane`. Thay thế dần `plan-cloud-files.md` §3–§5 + §8.3
(đồng bộ 2 chiều qua `tang-thu-sync-headless`); quyết định §7 của plan đó (xoá = `write`, mọi run được đọc, nội dung là DỮ LIỆU) GIỮ NGUYÊN.
Plan phía Không gian số: `quan-ly-thu-muc-ai` `docs/PLAN-cong-cfharness.md` (cùng §C).

## 1. Vì sao

Đồng bộ 2 chiều luôn bật (`filehub/supervisor.py` + binary `tang-thu-sync-headless`) làm được nhưng sai hình cho việc agent:

| Đau | Hệ quả |
|---|---|
| Hai bản sự thật (`~/KhongGianSo/<tên>` + server) | xung đột, bản `(xung đột)`, `mass_delete` dừng chiều đẩy, người phải `filehub resume` |
| Bản sao vĩnh viễn trên máy hub, cho MỌI user macOS | tốn đĩa, dữ liệu cơ quan nằm rải ở 4 home, backup/clone phải né |
| Đăng nhập thiết bị OAuth + chiếm suất thiết bị, token hết hạn | `auth_expired`, relay OAuth qua web public, wizard dài |
| Trễ = nhịp quét của binary + nhịp `status.json` 1 s + nhịp kéo | agent sửa trên bản cũ; "người vừa sửa" đến muộn |
| cfharness không gọi được API (chỉ đọc file trạng thái) | không biết ai sửa, không có phiên bản/khôi phục/presence theo tệp |
| Giám sát tiến trình ngoài, hợp đồng 3 repo | thêm một binary phải cài + cập nhật đúng phiên bản |

Chủ repo đã duyệt hướng mới: **cổng cfharness** = kênh riêng trên chính máy chủ Không gian số (cùng Mac mini), realtime 2 chiều CHỈ khi
agent chạy, **một nguồn sự thật = Không gian số**, không bản sao cục bộ lâu dài.

## 2. Kiến trúc

```
 run (claude, sandbox: KHÔNG TCP localhost)          daemon cfharness (user macOS X)               Không gian số (user phuonglq)
 ┌──────────────────────────────┐   stdio MCP     ┌─────────────────────────────────┐  HTTP+SSE   ┌──────────────────────────────┐
 │ Read/Edit/Bash trên          │──hub-self fs_*─▶│ LaneService (hub.filehub.lane)  │◀──────────▶│ listener lane 127.0.0.1:8223 │
 │ HUB_HOME/workspaces/lane/<r> │                 │  • LaneClient (khoá ở secrets/) │  Bearer    │  /lane/v1/* → cùng hàm của   │
 │ (cây làm việc theo run)      │◀─kéo/đẩy ngay──│  • Workdir: dựng/xem/đẩy/kéo    │  tt_lane_  │  syn/ + fld/ (ACL, khoá, hạn │
 └──────────────────────────────┘   steer <1 s    │  • events → steer run           │            │  mức, ClamAV, phiên bản,     │
                                                  └─────────────────────────────────┘            │  audit, fs-events web)       │
                                                                                                 └──────────────────────────────┘
```

Quyết định:
1. **Không gian số giữ mọi kiểm tra.** Lane là bộ chuyển đổi mỏng trên ĐÚNG hàm của giao thức đồng bộ (`syn/write.ts`, `syn/commit.ts`,
   `fld/*`) — không có đường ghi thứ hai. Khoá hub gắn MỘT tài khoản → ACL như người đó; thêm luật riêng: khoá tệp của bất kỳ ai (kể cả
   chính người đó) chặn agent; thư mục `claude_allowed=false` và Tối mật = vô hình.
2. **Agent không bao giờ cầm khoá, không bao giờ nói chuyện với lane.** Khoá ở secret store của hub (`FILEHUB_LANE_<SERVER>`), chỉ
   daemon dùng. Sandbox đã chặn TCP localhost của agent (`guard/runtime.py`), nên lane không thành bề mặt tấn công từ agent.
3. **Cây làm việc theo run (mặc định).** `claude` dùng tốt nhất Read/Edit/Grep/Bash trên tệp thật; chỉ có tool `fs_*` sẽ làm agent kém
   đi. Nên run ghi trên `filehub:` có thư mục `HUB_HOME/workspaces/lane/<run_id>/` dựng từ `list?recursive=1` của ĐÚNG phạm vi ref
   (`filehub:chung/Báo cáo` chỉ dựng `Báo cáo/`), đẩy từng tệp đã lưu ngay, kéo thay đổi của người ngay, XOÁ khi run kết thúc.
   Phạm vi quá lớn (`filehub.lane.tree_max_bytes` 2 GB / `tree_max_files` 20 000) hoặc run chỉ đọc → chế độ `tools`: không dựng
   cây, agent dùng `fs_*` + `fs_pull` kéo đúng phần cần vào thư mục run.
4. **SSE + REST thường**, không WebSocket: dễ chạy trên listener thứ hai bên Node, client Python chỉ cần `httpx2` (đã có qua `mcp`).
5. **Listener riêng loopback** (`127.0.0.1:8223`), không qua Caddy/cloudflared; đường lane không tồn tại trên listener công khai.
6. **Đồng bộ cũ giữ sau cờ** (`filehub.mode: sync|lane`, theo từng thư mục) tới khi lane chạy thật ≥ 2 tuần; `filehub:<tên>` không đổi.

## 3. An toàn — bất biến

- Khoá hub: chỉ trong `HUB_HOME/secrets/FILEHUB_LANE_<SERVER>` (0600, đăng ký bộ che theo GIÁ TRỊ — `redact.py`). KHÔNG BAO GIỜ trong
  env/argv của run hay của tiến trình nào khác, DB, `config.yaml`, audit, log, event, mcp.json, Telegram/Slack, gói xuất hub (`clone`
  bỏ — thêm tiền tố `FILEHUB_LANE_` vào `transfer_secrets.CLONE_SKIP_PREFIXES`; `move` giữ). Nhập khoá: CLI `getpass` / web bước 2
  (`Action.reenter`, như passphrase) — không qua tham số dòng lệnh.
- URL lane phải là `http://127.0.0.1:<cổng>` hoặc `http://[::1]:<cổng>` (kiểm khi setup + mỗi lần nối) — không gửi khoá ra ngoài máy.
- Agent: sandbox chặn TCP localhost (đã kiểm thật, `docs/dev-notes/web.md`); classifier thêm `filehub_lane` (forbidden): `curl`/`nc`/
  `python -c` nhắm `127.0.0.1:<cổng lane>` + đọc `secrets/FILEHUB_LANE_*` (đã ∈ `sensitive_read` qua `secrets/`).
- `fsself.call {run_id, op, args}`: daemon ràng `run_id` (như `taskself.call`), chỉ gốc/phạm vi của workspace run, `read_only` → chỉ đọc,
  đường dẫn chuẩn hoá theo C.4 PHÍA HUB trước khi gửi. Không có tool xoá hẳn, không đổi ACL, không mở khoá tệp.
- Quét secret TRƯỚC khi đẩy: tệp mới/sửa chứa GIÁ TRỊ secret hub biết (`redact.contains_secret_value`) → KHÔNG đẩy, giữ trong cây
  run, event `filehub.secret_blocked{run, path}`, steer agent câu cố định. (Trước: chỉ cảnh báo sau run — lane chặn được trước.)
- Nội dung từ lane (tệp, tên tệp, đường dẫn) là DỮ LIỆU agent: event bus chỉ mang đường dẫn tương đối đã `one_line` + id; steer = câu cố
  định tiếng Anh có `<rel>` đã làm sạch; `by_kind` không có tên người (hợp đồng C.8).
- Người sửa thắng: không bao giờ ghi đè phiên bản của người — `If-Match` bắt buộc, 409 → bản xung đột riêng (§4.4).

## §C. Hợp đồng cổng cfharness — giao thức `lane/v1` (chép NGUYÊN VĂN ở cả hai repo)

> Bản gốc: `agent-hub` `docs/dev-notes/plan-filehub-lane.md` §C = `quan-ly-thu-muc-ai` `docs/PLAN-cong-cfharness.md` §C. Sửa một bên
> thì sửa cả hai trong cùng đợt PR; fake server của hub (`tests/fake_kgs_lane.py`) phải theo đúng mục này.

### C.1 Vận chuyển
- HTTP/1.1 thuần, JSON UTF-8 (trừ thân tệp = byte thô). Luồng sự kiện = **SSE** (`text/event-stream`) — không WebSocket (không cần
  `upgrade` trên listener thứ hai; client Python chỉ cần đọc stream).
- Listener RIÊNG của Không gian số: `http://127.0.0.1:<LANE_PORT>` (qltm `8223`, cchc `8233`). Không qua Caddy/cloudflared. Mọi
  đường dẫn bắt đầu `/lane/v1/`. Listener chính (8221) trả `404 not_found` cho `/lane/*`; listener lane trả `404` cho mọi thứ khác.
- Listener lane từ chối (`403 lane_not_local`) request có `remoteAddress` không phải loopback HOẶC có bất kỳ header `Forwarded`,
  `X-Forwarded-*`, `CF-Connecting-IP`, `CF-Ray`. Không CORS, không cookie (cookie bị bỏ qua).

### C.2 Xác thực + quy về người dùng
- Khoá hub: `tt_lane_<43 ký tự base64url>` (32 byte ngẫu nhiên). Gửi `Authorization: Bearer <khoá>`. Server chỉ giữ `sha256` + 12 ký
  tự đầu (hiển thị). Khoá gắn với: MỘT tài khoản Không gian số (`user_id` — mọi kiểm tra ACL như người đó), `hub_label`
  (`[a-z0-9_.-]{1,32}`, vd. `lamlinh`), danh sách gốc được cấp (thư mục và/hoặc workspace = gốc ảo `rootId = wsId`), `scope`
  `rw|ro`, `expires_at` (tuỳ chọn).
- Khoá không có / sai / hết hạn / thu hồi / người bị khoá / chính sách tắt → `401 unauthorized` GIỐNG NHAU (riêng chính sách tắt:
  `403 lane_disabled` sau khi khoá đã hợp lệ). Không chiếm suất thiết bị, không OAuth.
- Header ghi nhận (do daemon đặt, server kiểm mẫu, sai → `400 invalid_input`): `X-Lane-Agent: <[A-Za-z0-9_.-]{1,64}>`,
  `X-Lane-Run: <[A-Za-z0-9_.-]{1,64}>`. Bắt buộc với mọi lệnh ghi + presence; tuỳ chọn với lệnh đọc.
- Ghi nhận ở Không gian số: audit `actor = user_id`, `after.via = "cfharness"`, `after.lane_key = <prefix>`, `after.agent`, `after.run`;
  journal `sync_changes.via = 'cfharness'`; phiên bản hiển thị **"cfharness · <agent> · hub <hub_label>"** (dựng ở server từ trường,
  không nhận chuỗi tự do).

### C.3 Bắt tay
`GET /lane/v1/hello` →
```json
{"protocol": 1, "server": {"name": "Không gian số", "instance": "qltm", "version": "2026.11.20"},
 "user": {"username": "lamlinh"}, "key": {"prefix": "tt_lane_Ab3d", "hub_label": "lamlinh", "scope": "rw", "expires_at": null},
 "roots": [{"id": "6f1c…", "kind": "folder", "ws_id": "a2e0…", "name": "cfharness", "role": "owner"},
           {"id": "a2e0…", "kind": "workspace", "ws_id": "a2e0…", "name": "Phòng CCHC", "role": "admin"}],
 "limits": {"max_file_bytes": 2147483648, "list_page_max": 2000, "presence_ttl_s": [30, 3600],
            "writes_in_flight_per_key": 8, "event_streams_per_key": 2, "path_max_bytes": 1024}}
```
`protocol` khác 1 → hub dừng lane đó (`filehub.lane.state = version_mismatch`). Gốc mất quyền (rời ws, mất Owner/Admin, thư mục bị
xoá, nâng nhãn Tối mật) không còn trong `roots`.

### C.4 Đường dẫn + mục
- Mọi thao tác tệp nằm dưới `/lane/v1/roots/{root}/…`, `{root}` ∈ `roots[].id`; ngoài → `403 root_not_granted`.
- Đường dẫn tương đối `path` (query hoặc thân JSON): NFC, `/` phân cách, không `/` đầu/cuối, không phần rỗng/`.`/`..`, không phần bắt
  đầu `.tangthu`, không ký tự điều khiển, ≤ 1024 byte, ≤ 64 cấp; `""` = chính gốc. Sai → `422 invalid_name`.
- Mục (`Item`):
```json
{"path": "Báo cáo/Q3.docx", "type": "file", "id": "0b7e…", "parent_id": "9c21…", "rev": 7,
 "version_id": "e41a…", "version_no": 12, "size": 48211, "sha256": "9f86…", "mime": "application/vnd…",
 "mtime": "2026-11-17T08:00:00Z", "locked": false, "label": null}
```
  Thư mục: `type: "folder"`, không `version_id|size|sha256|mime`. `label`: `null|"mat"` (Tối mật không bao giờ xuất hiện). Mục mà
  người dùng không thấy, thuộc thư mục `claude_allowed = false`, hoặc Tối mật → coi như không tồn tại (`404 not_found`). Tệp khớp mẫu
  loại trừ ACL-04 (`.env`, `*.pem`, khoá riêng… – như phiên Claude) cũng vô hình; ghi vào đúng tên đó → `403 forbidden`.

### C.5 Đọc
| Lệnh | Trả |
|---|---|
| `GET …/stat?path=` | `{item}` |
| `GET …/list?path=&recursive=0\|1&page=<token>&limit≤2000` | `{items: [Item], next_page: "<token>"\|null, cursor: "<cursor>"}` — `cursor` = vị trí luồng sự kiện TẠI lúc chụp trang đầu (dùng để nối luồng sau khi dựng cây; mọi trang của một lượt cùng ảnh chụp, token sống 15 phút) |
| `GET …/content?path=&version=<version_id>` | byte thô, `ETag: "<version_id>"`, `X-Lane-Sha256`, hỗ trợ `Range` (206) + `If-None-Match` (304) |
| `GET …/versions?path=` | `{versions: [{version_id, no, size, sha256, at, by_kind}]}` (≤ 50, mới trước) |
| `GET /lane/v1/cursor` | `{cursor}` = đầu luồng hiện tại (bắt đầu nghe "từ bây giờ") |

### C.6 Ghi (mọi lệnh ghi: `X-Lane-Agent` + `X-Lane-Run` + `Idempotency-Key: <uuid>`; gửi lại cùng khoá trong 24 h → đúng phản hồi lần đầu — xem "Idempotency-Key" dưới bảng)
| Lệnh | Thân | Trả |
|---|---|---|
| `PUT …/content?path=&parents=0\|1` | byte thô; `Content-Length` BẮT BUỘC (chunked → `411`); `X-Lane-Sha256: <hex>` BẮT BUỘC; `If-Match: "<version_id>"` (sửa) HOẶC `If-None-Match: *` (tạo mới) — thiếu cả hai → `428 precondition_required` | `201 {item}` tạo / `200 {item}` phiên bản mới |
| `POST …/mkdir` | `{"path": "A/B", "parents": true}` | `201 {item}`; đã có thư mục → `200 {item}` |
| `POST …/move` | `{"from": "A/x.md", "to": "B/y.md", "if_rev": 7}` | `200 {item}`; đích đã có → `409 name_conflict {current}` (không ghi đè). Thư mục: cây con có mục agent không thấy → `403 hidden_content`; có tệp đang khoá → `423 file_locked` |
| `DELETE …/item?path=&if_rev=` | — | `200 {trash_batch: "<id>", item}` (vào Thùng rác; không xoá hẳn). Thư mục: CHỈ khi rỗng (kể cả mục vô hình với agent) — còn mục → `409 not_empty`; hub xoá tệp trước rồi rmdir từ dưới lên |
| `POST …/restore` | `{"trash_batch": "<id>"}` HOẶC `{"path": "x.md", "version_id": "…", "if_match": "<version_id hiện tại>"}` | `200 {item}` (khôi phục phiên bản = tạo phiên bản mới) |
| `PUT /lane/v1/presence` | `{"root": "…", "path": "Báo cáo/Q3.docx"\|null, "ttl_s": 300}` | `200 {since, until}`; nhãn do server dựng: `cfharness · <agent>` (+ ` · đang sửa <tên tệp>`) |
| `DELETE /lane/v1/presence?root=` | — | `204` (xoá presence của `X-Lane-Run`) |

- Ghi nội dung là ĐỒNG BỘ: server nhận hết thân → kiểm sha256 → quét ClamAV → chốt phiên bản trong giao dịch (kiểm lại base, quyền,
  khoá, hạn mức) → journal + sự kiện `fs` cho web → trả. Không có trạng thái "đang quét" phía client. Server chờ quét tối đa
  `LANE_SCAN_WAIT_S` (mặc định **300 s**; quá → `503 scan_unavailable`) → timeout đọc phản hồi của client cho `PUT …/content` phải
  > 300 s + thời gian gửi thân (hub: ≥ 360 s).
- Khoá tệp (FLD-10): BẤT KỲ khoá đang hiệu lực nào trên tệp — kể cả khoá của CHÍNH người dùng gắn với khoá hub — chặn lane (`423`).
  Người khoá tệp = agent không ghi được.
- Cây chỉ xuống (D7: thư mục hệ thống / tự quản / gắn Nguồn) kiểm ở CẢ nguồn lẫn đích: di chuyển / đổi tên / xoá mục trong cây đó →
  `403 forbidden`.
- Idempotency-Key: server giữ dấu "đang chạy" trước khi thực thi — request trùng khoá tới khi lần đầu chưa xong → `409 in_progress
  {retry_after}` (không chạy lần hai; hub thử lại CÙNG khoá sau `retry_after`). Chỉ lưu kết quả cuối (2xx / 4xx); lỗi tạm `423`, `429`,
  `5xx` và lỗi đọc thân (thân bị cắt / sai băm khi truyền) KHÔNG lưu → gửi lại cùng khoá chạy thật.
- `scope: ro` → mọi lệnh ghi `403 read_only_key`. Gốc workspace: tệp ở ngay gốc → `422 root_files_not_allowed`; xoá/di chuyển chính gốc
  → `403 root_not_granted`.

### C.7 Mã lỗi (thân `{"code": "...", ...extra}` như API hiện có)
| HTTP | `code` | extra | Hub làm gì |
|---|---|---|---|
| 400 | `invalid_input`, `hash_mismatch` | — | lỗi lập trình → log, không thử lại |
| 401 | `unauthorized` | — | dừng lane, `filehub.lane.state=auth_failed`, báo người |
| 403 | `lane_disabled`, `lane_not_local`, `root_not_granted`, `forbidden`, `read_only_key` | — | trả lỗi cho agent; `root_not_granted` → nạp lại `hello` |
| 403 | `hidden_content` | — | di chuyển / đổi tên thư mục có mục agent không thấy: trả lỗi cho agent (không thử lại) |
| 404 | `not_found` | — | như tệp không có |
| 409 | `conflict` | `current: Item` | KHÔNG ghi đè: lưu bản của agent thành bản xung đột (§4.4), steer agent |
| 409 | `exists`, `name_conflict` | `current: Item` | như trên |
| 409 | `parent_missing` | — | thử lại với `parents=1` nếu ý định rõ |
| 409 | `not_empty` | — | xoá thư mục còn mục: xoá các mục con trước rồi thử lại; vẫn còn (mục vô hình với agent) → trả lỗi cho agent, giữ thư mục |
| 409 | `in_progress` | `retry_after` | lệnh cùng `Idempotency-Key` đang chạy: chờ `retry_after` rồi gửi lại CÙNG khoá |
| 411 / 428 | `length_required` / `precondition_required` | — | lỗi lập trình |
| 413 | `too_large` | `max_bytes` | trả lỗi cho agent |
| 422 | `invalid_name`, `root_files_not_allowed` | — | trả lỗi cho agent |
| 422 | `infected` | `quarantined: true` | KHÔNG tạo phiên bản; event `filehub.infected`; báo người |
| 423 | `file_locked` | `until` | giữ thay đổi ở hàng đợi, thử lại sau `until`/60 s; steer agent một lần |
| 429 | `rate_limited`, `too_many_writes` | `retry_after` (giây) | lùi theo `retry_after` |
| 503 | `scan_unavailable` | `retry_after` | lùi + thử lại (fail-closed: không bao giờ ghi khi chưa quét) |
| 507 | `storage_full` | `scope`, `used_bytes`, `quota_bytes` | dừng đẩy gốc đó, báo người |

`current` bị bỏ khi mục đang chiếm tên vô hình với agent (thư mục Claude tắt, Tối mật, mẫu loại trừ) – không lộ gì thêm.

### C.8 Luồng sự kiện
`GET /lane/v1/events?cursor=<cursor>` (hoặc header `Last-Event-ID`), `Accept: text/event-stream`. Không có cursor → từ đầu luồng hiện
tại. Mỗi khung:
```
id: c1.eyJhMmUwIjo4ODQxMn0
event: change
data: {"root":"6f1c…","op":"update","type":"file","id":"0b7e…","path":"Báo cáo/Q3.docx","from_path":null,"rev":8,
       "version_id":"77aa…","size":48900,"sha256":"1b4e…","by_kind":"human","self":false,"agent":null,"request_id":null,
       "at":"2026-11-17T08:01:02Z"}
```
- `op` ∈ `create|update|move|rename|delete|restore|acl`. `path` = đường dẫn SAU thay đổi (xoá: đường dẫn cuối cùng; mục đã bị dọn hẳn
  khỏi DB: `path: null`, `gone: true`); `from_path` khi `move|rename` (null khi chỗ cũ ngoài gốc). Một mục thuộc nhiều gốc được cấp → một khung cho mỗi gốc.
- `by_kind` ∈ `human|cfharness|claude|device|source|system` — KHÔNG có tên người. `self: true` khi chính khoá này ghi (kèm `agent`,
  `request_id` = `Idempotency-Key`) → hub bỏ qua tiếng vọng.
- `op: acl` hoặc mục rời khỏi phạm vi nhìn thấy → `{"op":"acl","gone":true,…}`: hub `stat`/`list` lại `path` (= thư mục gần nhất
  CÒN thấy được – không lộ tên mới của mục vô hình; `""` = gốc).
- Mục agent CHƯA TỪNG thấy (trong thư mục vô hình, khớp mẫu loại trừ, Claude tắt…) → KHÔNG có khung nào (không id / rev / thời điểm).
  `gone` chỉ phát khi mục thấy được TRƯỚC thay đổi (chỗ cũ của move / tên cũ của rename thấy được; dòng `acl` của thư mục có cha còn
  thấy được). `from_path` = `null` khi tên / chỗ cũ không thấy được với agent (vd. đổi tên từ `.env`).
- `event: reset` `{"root": "…", "reason": "compacted|acl|restored"}` — cursor quá cũ (journal đã nén, > 30 ngày) hoặc thay đổi hàng loạt:
  hub `list?recursive=1` lại gốc đó rồi nối luồng bằng `cursor` của lượt list.
- `event: root_revoked` `{"root": "…"}` · `event: key_revoked` (rồi đóng) · `event: replaced` (luồng thứ 3 mở → luồng cũ nhất bị đóng)
  · `event: ping` `{}` mỗi 15 s.
- Cursor: chuỗi mờ (`c1.` + base64url map `{ws_id: seq}` — vì thứ tự `seq` chỉ đúng TRONG một workspace). Hub coi là mờ, lưu
  cursor của khung CUỐI đã xử lý xong. Giao ít nhất một lần; hub khử trùng theo `(id, rev)`. Độ trễ mục tiêu commit → khung ≤ 1 s
  (chuông `pg_notify qltm_sync` hiện có; kiểm định kỳ 5 s phòng mất chuông).

### C.9 Giới hạn
Tệp ≤ `MAX_UPLOAD_MB` (mặc định 2 GB) · `list` ≤ 2000/trang · ghi đồng thời ≤ 8/khoá (`429 too_many_writes`) · 600 lệnh/phút/khoá ·
luồng sự kiện ≤ 2/khoá · presence ttl 30..3600 s · `Idempotency-Key` 24 h · phiên bản giữ theo chính sách hiện có (50 bản / 90 ngày).

## 4. Phía cfharness — module

### 4.1 Tệp
| Tệp | Vai trò |
|---|---|
| `filehub/lane/client.py` (mới) | `LaneClient`: httpx2 async, `hello/stat/list/read/write/mkdir/move/delete/restore/versions/presence`, `events()` (bộ đọc SSE, nối lại lùi 0,5→10 s, `Last-Event-ID`), ánh xạ mã lỗi C.7 → `LaneError(code, status, extra)`; gỡ khoá khỏi mọi lỗi/repr |
| `filehub/lane/service.py` (mới) | `LaneService` (`hub.filehub.lane`): một kết nối/server, cursor ở `hub_state filehub.lane.cursor.<server>` (∈ `hub_transfer.MACHINE_STATE`), phát sự kiện tới `Workdir` + steer; trạng thái `filehub.lane.state` (`ok|offline|auth_failed|version_mismatch|disabled`) |
| `filehub/lane/workdir.py` (mới) | `Workdir` theo run: dựng cây, bảng `base {rel: (version_id, sha256, mtime, size)}`, bộ theo dõi (hook PostToolUse Write/Edit → đẩy ngay; quét mtime 500 ms cho Bash/script), hàng đợi đẩy (gộp 2 s yên lặng, ≤ 1 phiên bản/30 s/tệp, flush khi run kết thúc), kéo + ghi nguyên tử, nhật ký thao tác (`ops`) cho diff |
| `filehub/lane/tools.py` (mới) | xử lý `fsself.call`: `fs_status`, `fs_list`, `fs_stat`, `fs_read` (văn bản ≤ 256 KB; nhị phân → `fs_pull`), `fs_pull`, `fs_write`, `fs_mkdir`, `fs_move`, `fs_delete`, `fs_versions`, `fs_restore`, `fs_link` |
| `mcp_server/hub_self.py` | thêm nhóm tool `fs_*` (vai trò mọi run có `run_opts.filehub_lane`), mô tả tiếng Anh |
| `filehub/service.py` | `resolve_ref` theo `mode`: `sync` → như cũ; `lane` → `(workdir_path, folder, sub)` do `Workdir.prepare` cấp; `report_*` lấy từ `ops` thay vì walk |
| `filehub/rpc.py`, `filehub/cli.py`, `web/filehub_pages.py` | `filehub lane setup|status|test|disable` + thẻ Cài đặt "Cổng cfharness"; RPC `filehub.lane_setup|lane_disable` ∈ HUMAN_ONLY + `MUTATING_FILEHUB` |
| `runs.py` | `submit`: ref `filehub:` ở chế độ lane → `run_opts.workspace_kind=filehub_lane`; `_prepare_spec` dựng cây TRƯỚC spawn (lỗi → run `failed` mã `filehub_unavailable`); `_finalize` flush + xoá cây (§4.5) |
| `workspace.py` | khoá vẫn theo GỐC thư mục chung (`WorkspaceLocks.key_for`); checkpoint/diff của `filehub_lane` = `Workdir.ops` (không walk); `run.revert` → khôi phục phiên bản trước run qua `restore` (bước mới, HUMAN_ONLY) |
| `guard/classifier.py`, `guard/runtime.py` | gốc cây run ∈ `filehub_roots` (luật `filehub_delete|overwrite` = `write`, xoá gốc = destructive); `filehub_lane` forbidden; sandbox denyRead `HUB_HOME/workspaces/lane/*` trừ cây của chính run |
| `config.py` | `filehub.mode`, `filehub.lane.{servers, tree_max_bytes, tree_max_files, push_quiet_s, push_min_interval_s, final_flush_s, orphan_keep_days}` (∈ `SECURITY_KEYS` như cả nhánh) |
| `events.py` | `filehub.lane.state`, `filehub.human_changed`, `filehub.push_failed`, `filehub.secret_blocked`, `filehub.infected` (+ schema) |

### 4.2 Bắt đầu run (chế độ `tree`)
1. `LaneService.ensure(server)` (đã nối, `hello` ok, gốc còn trong `roots`). 2. `list?recursive=1` phạm vi ref → ghi tệp vào
`workspaces/lane/<run>/` (song song ≤ 8, tệp > `fetch_lazy_bytes` 50 MB tạo thành **tệp trống + ghi chú** trong khối prompt "Shared folder":
"large files listed but not downloaded; call fs_pull"). 3. Lưu `cursor` của lượt list; luồng sự kiện chung của server phát khung cho các
run có gốc trùng. 4. `PUT presence` (làm mới ở nửa ttl; `path` = tệp đẩy gần nhất). 5. Khoá thư mục = gốc (như hiện nay).

### 4.3 Trong run
- **Đẩy:** thay đổi cục bộ (tạo/sửa/xoá/đổi tên — đổi tên nhận diện bằng sha256 + biến mất cùng lượt quét) → quét secret → `PUT content`
  (`If-Match` = base) / `move` / `DELETE` → cập nhật base. Bỏ qua mẫu `SYNC_IGNORE_DEFAULTS` (`.DS_Store`, `~$*`, `*.tmp`, `.git/`, …).
- **Kéo:** khung `change` `self:false` trong phạm vi → nếu tệp cục bộ trùng base → tải + ghi nguyên tử (`.lane-tmp` rồi `rename`), cập nhật
  base; steer (một lần/(run, tệp)): `[cfharness] The human changed <rel> on Không gian số just now. Re-Read it before editing.`
  `delete`/`move` của người → áp dụng cục bộ + steer tương ứng. Khung `self:true` → bỏ (khử tiếng vọng).
- **Đo:** event `filehub.human_changed{run, rel, op}`; độ trễ commit-server → steer ghi histogram (mục tiêu p95 ≤ 1 s).

### 4.4 Xung đột
Tệp cục bộ đã sửa VÀ server có phiên bản mới (khung kéo, hoặc `409 conflict` khi đẩy) → không ghi đè cả hai phía: đẩy bản của agent
thành `<tên> (cfharness <agent> <HHMM>).<đuôi>` cùng thư mục (`If-None-Match: *`), kéo bản của người về đúng tên, steer:
`[cfharness] Conflict on <rel>: the human saved a newer version. Your version was kept as <rel2>. Re-Read <rel> and merge.`
`423 file_locked` → giữ trong hàng đợi, thử lại sau `until`; steer một lần.

### 4.5 Kết thúc run
Flush hàng đợi (≤ `final_flush_s` 60 s, trừ khi lỗi 401/507) → `DELETE presence` → báo `filehub.changed|deleted` từ `ops` → xoá cây.
Còn thay đổi chưa đẩy được → chuyển cây sang `HUB_HOME/filehub/lane-orphans/<run>/` (0700, sandbox deny), event `filehub.push_failed`,
nút "Đẩy lại"/"Bỏ" trên web; dọn sau `orphan_keep_days` 7. `waiting_human` giữ cây + presence (resume dùng lại cây, đối soát base với
`stat` trước khi chạy tiếp). Daemon khởi động lại: `recovery` quét `workspaces/lane/*` của run không còn chạy → đối soát + đẩy/mồ côi.

## 5. Chuyển từ đồng bộ sang lane
1. Không gian số bật chính sách "Cho phép cổng cfharness"; người dùng tạo khoá ở Hồ sơ › Cổng cfharness (chọn gốc = đúng thư mục/ws
   hub đang đồng bộ). 2. `cfharness filehub lane setup --server qltm --url http://127.0.0.1:8223` (khoá qua getpass) → `hello` →
   ánh xạ `folders[].root_id` ↔ `roots[].id`. 3. `cfharness filehub switch <tên> --to lane`: kiểm `status.json` gốc đó `idle`, không
   bản xung đột/không chờ đẩy → gỡ root khỏi tiến trình sync → đổi `folders[].mode=lane` → bản sao `~/KhongGianSo/<tên>` CHUYỂN vào
   `HUB_HOME/trash/filehub-<tên>-<ngày>` (không xoá; người xoá sau). 4. Khi mọi thư mục ở lane: supervisor không chạy, thiết bị trên Không
   gian số được thu hồi (người bấm, hướng dẫn in ra). 5. Sau 2 bản phát hành ổn: PR gỡ supervisor, `login`, `local_parent`,
   `max_delete_batch`, relay OAuth, `fake_tangthu_sync.py` (giữ đọc cấu hình cũ để `switch` vẫn chạy). `scripts/setup-hub-user.sh` thêm bước
   lane (theo quy ước "một script mỗi user"). Hub ở máy khác (không cùng máy Không gian số) → giữ `sync` (câu hỏi Q6).

## 6. Test (không gọi Không gian số thật, không mạng ngoài)
- `tests/fake_kgs_lane.py`: ThreadingHTTPServer (stdlib, TCP loopback thật) trên `127.0.0.1:0`, cây trong RAM theo C.4–C.9 (phiên bản, `If-Match`, khoá, hạn mức,
  `infected` theo tên tệp chứa `EICAR`, idempotency, SSE có cursor/`reset`/`root_revoked`), API phụ cho test: `human_write(path, bytes)`,
  `lock(path)`, `revoke()`, `drop_stream()`. Hợp đồng đổi → sửa fake trong CÙNG PR.
- `test_lane_client.py`: ánh xạ mọi mã C.7, SSE nối lại + `Last-Event-ID`, khoá không lộ trong lỗi/log/repr.
- `test_lane_workdir.py`: dựng cây, đẩy khi lưu (hook + quét), gộp 2 s, kéo + steer ≤ 1 s (đồng hồ giả), xung đột 409/khung kéo, 423,
  `secret_blocked` không gửi byte nào, flush cuối run, mồ côi, resume sau `waiting_human`, khởi động lại daemon.
- `test_lane_tools.py`: `fsself.call` ràng run, chỉ đọc, `..`/`.tangthu`/symlink, tool không có xoá hẳn.
- `test_lane_security.py` (+ ca trong `test_sec_auth.py`, `test_guard_classifier.py`): khoá không trong env/argv/mcp.json của run, không
  trong DB/audit/event/gói `clone`; URL không loopback bị từ chối; classifier `filehub_lane`; RPC lane ∈ HUMAN_ONLY, không ∈ MANAGER_ALLOWED.
- `test_lane_migration.py`: `switch` từ chối khi sync chưa `idle`; chuyển bản sao vào trash; `filehub:` ref chạy được ở cả hai chế độ.
- e2e: `tests/fake_claude_team.py` sửa tệp trong cây → fake lane nhận phiên bản; fake lane `human_write` → run nhận steer.

## 7. Chia PR (hai repo)

| # | Repo | Nội dung | Phụ thuộc |
|---|---|---|---|
| K1 | Không gian số | migration 048 + khoá hub (tạo/thu hồi ở Hồ sơ, danh sách ở Quản trị) + listener lane + `hello` | — |
| H1 | cfharness | `LaneClient` + `fake_kgs_lane.py` + `filehub lane setup|status|test` (secret, kiểm loopback) | §C (không cần K1) |
| K2 | Không gian số | đọc (`stat/list/content/versions/cursor`) + SSE `events` | K1 |
| H2 | cfharness | `LaneService` (luồng sự kiện, cursor, state) + tool `fs_*` chỉ đọc + chế độ `tools` cho run chỉ đọc | H1 |
| K3 | Không gian số | ghi (`content PUT` qua quét, `mkdir/move/item DELETE/restore`) + ghi nhận cfharness + luật khoá/`claude_allowed` | K2 |
| H3 | cfharness | `Workdir` (cây theo run, đẩy/kéo, xung đột, secret chặn trước, flush, mồ côi) + diff/báo từ `ops` + `fs_*` ghi | H2 |
| K4 | Không gian số | presence lane + dải "cfharness · <agent> · đang sửa X" trên web | K3 |
| H4 | cfharness | presence, `switch` sync→lane, thẻ web, setup script, CHANGELOG; nghiệm thu thật trên qltm | H3, K1–K4 deploy |
| K5 | cchc | chép K1–K4 sang `quan-ly-thu-muc-ai-cchc` (cổng 8233) | K4 ổn |
| H5 | cfharness | gỡ đồng bộ cũ (supervisor, login, relay, local_parent, mass_delete) | H4 + 2 bản phát hành |

H1 song song với K1; H2/H3 viết trên fake, nghiệm thu thật chỉ sau khi K tương ứng lên prod. Mỗi PR có dòng "Chưa phát hành" ở CHANGELOG.

## 8. Đã chốt (chủ repo, 2026-11-17)
1. **TCP loopback** `127.0.0.1:8223` (qltm) / `8233` (cchc) + khoá — không Unix socket.
2. **Khoá do chính người dùng cấp** cho tài khoản Không gian số của mình; hub lưu trong secret store qua lệnh/thẻ web CHỈ NGƯỜI
   (`cfharness filehub lane setup` getpass / thẻ Cài đặt bước 2).
3. **`claude_allowed=false` = vô hình** với cfharness.
4. **Không gian số gộp phiên bản dồn dập** phía server (hub vẫn ≤ 1 phiên bản/30 s/tệp + flush cuối).
5. **Ngưỡng cây theo run** như plan: 2 GB / 20 000 tệp, tệp > 50 MB tải lười.
6. **Hub ở máy khác: ngoài phạm vi** — giữ `sync`.
7. **Agent KHÔNG khoá tệp** (FLD-10): chỉ `If-Match`/409 + presence.
8. **Bản sao `~/KhongGianSo/*` cũ để nguyên** và báo (status/thẻ web) — hub KHÔNG BAO GIỜ xoá/di chuyển (thay §5 bước 3 "chuyển vào trash").
9. (thêm) **Khoá hết hạn sau 180 ngày**, cảnh báo trước khi hết hạn (`filehub.lane.key_warn_days`, mặc định 14): `expires_at` của server
   nếu có, không thì `set_at + filehub.lane.key_max_days`.

## 10. Đã làm phía cfharness (H1–H4, nhánh `feat/filehub-lane`) — chạy trên server giả, chưa nghiệm thu thật

| PR | Nội dung | Test |
|---|---|---|
| H1 | `filehub/lane/client.py` (§C đầy đủ: C.3 hello, C.5 đọc, C.6 ghi + Idempotency-Key, C.7 → `LaneError`, C.8 SSE một kết nối), `check_url` chỉ loopback, `norm_rel` (C.4 phía hub); `LaneService.setup|test|disable|status` — khoá `FILEHUB_LANE_<TÊN>` chỉ secret store, gỡ khỏi params trước audit; hạn khoá 180 ngày + cảnh báo; `filehub.mode` + `folders[].mode` (mặc định `sync`); RPC/CLI `filehub lane …` (HUMAN_ONLY); gói `clone` bỏ khoá | `test_lane_client.py` |
| H2 | Luồng SSE/server khi có cây (cursor bền `hub_state filehub.lane`, nối lại 0,5→10 s + `Last-Event-ID`, `reset`/`root_revoked`/`key_revoked`); `Workdir.materialize` (đúng phạm vi, lười > 50 MB, chế độ `tools` khi quá ngưỡng/chỉ đọc); kéo thay đổi của người + steer một lần/(run, tệp) + `filehub.human_changed{latency_ms}`; hub-self `fs_*` (`--fs ro|rw`) → `fsself.call` (ràng run như `taskself.call`, ∈ `auth.SCOPED_READ`); RunManager: ref lane → `run_opts.filehub_lane`, khoá `filehub-lane:<server>:<root>`, dựng cây trước spawn (lỗi → `failed` `filehub_unavailable`); task/dispatcher/locks/manager hiểu ref lane; classifier `filehub_lane` (cấm, ∈ `TRUST_NEVER_RULES`) | `test_lane_service.py`, `test_guard_classifier.py::test_lane_port_forbidden` |
| H3 | Đẩy: quét mtime + kích theo `tool.complete`, gộp `push_quiet_s` + trần `push_min_interval_s`, `If-Match`/`If-None-Match: *`, xoá → thùng rác, đổi tên → `move` (theo sha256), thư mục; bỏ symlink/liên kết cứng/tệp rác; **quét secret trước khi đẩy**; 409 → bản xung đột; 423 → hàng đợi + steer một lần; 429/503 lùi; 422 infected; 401/507 dừng; flush cuối run → `runs.diff{kind: lane}` + `workspace.diff` + `filehub.changed|deleted`; mồ côi `filehub/lane-orphans/<tree>` + `filehub.push_failed` + `filehub.lane_orphan push|drop`; `waiting_human` → đỗ cây, chạy tiếp đối soát (thay đổi của người lúc chờ vào tin đầu); vòng bảo trì (`export_quiesced`) đẩy nốt cây của run đã chết, dọn mồ côi > 7 ngày | `test_lane_workdir.py` |
| H4 | Presence (`PUT` ở nửa ttl / khi tệp đẩy gần nhất đổi, `DELETE` cuối run, run chỉ đọc không có); `filehub switch <tên> --to lane|sync` (từ chối khi đồng bộ chưa yên / gốc chưa cấp / có run; bản sao cũ để nguyên + báo); `filehub lane add`; thẻ web "Cổng cfharness" (khoá chỉ ở bước 2 — `Action.reenter` + `secret_fields`); `setup-hub-user.sh` `KGS_MODE=lane` | `test_lane_switch.py` |

Lệch plan (không đổi §C ngoài tên tệp fake):
- **Cây ở `<HUB_HOME>/agents/<agent>/workspaces/lane-<tree>`** thay cho `HUB_HOME/workspaces/lane/<run>`: classifier/sandbox coi mọi đường dẫn
  HUB_HOME ngoài vùng làm việc của agent là vùng hub (ghi/xoá bị giữ chờ người). `tree` = run ĐẦU của chuỗi (continue/resume/retry/auto-resume
  dùng lại CÙNG cây → `claude --resume` tìm session theo cwd). `workspace_kind` của spec vẫn `dir` (không bao giờ scratch/bypass).
- Fake server: `tests/fake_kgs_lane.py` (ThreadingHTTPServer stdlib, TCP loopback thật — SSE đứt/nối lại thật) — §C ghi tên này ở cả hai repo.
- Kích quét bằng event `tool.complete` của run (thay cho hook PostToolUse riêng) + quét mtime `scan_interval_s`.
- Đẩy từ bộ nhớ, trần 256 MB/tệp (`client.UPLOAD_MAX_BYTES`, httpx async không gửi tệp đồng bộ có Content-Length) — lớn hơn: báo agent `too_large`.
- Chuyển chế độ KHÔNG chuyển bản sao cũ vào trash (quyết định 8): để nguyên, báo ở `lane status`/thẻ web.
- Xung đột dùng lại event `filehub.conflict` (thêm `run_id`, `copy`). `filehub.lane_test` là HUMAN_ONLY (dùng khoá dù chỉ đọc).
- Thay đổi của người khi run chưa chạy (đối soát lúc chạy tiếp) không steer được → vào TIN ĐẦU của lượt sau.

Sửa theo review (2026-11-17, `tests/test_lane_review_fixes.py` — mỗi ca đỏ trước khi sửa; KHÔNG đổi §C):
- **Symlink**: mọi thao tác tệp của daemon trong cây đi qua `lane/safefs.SafeTree` (mở gốc + từng thành phần bằng `dir_fd` + `O_NOFOLLOW`,
  thao tác cuối theo fd: đọc để đẩy, tải về, ghi, xoá — `rmtree(dir_fd=)` —, đổi tên). Thành phần symlink → `UnsafePath`: `fs_delete`/`fs_move`
  từ chối TRƯỚC lệnh trên server; khung xoá/di chuyển của người không đụng đĩa (di chuyển: kéo tệp ở chỗ mới); đẩy → `failed=symlink`
  (cây mồ côi). Gốc cây không còn là thư mục thật → quét dừng (không suy ra "agent xoá hết").
- **Mẫu bỏ qua**: mục khớp `ignored()` trên server không vào base (materialize/reconcile/fs_pull/khung change), ghi vào `hidden`; xoá thư mục
  còn chứa mục `hidden` → chỉ quên thư mục, không xoá.
- **Xoá đang chờ vs bản người mới hơn**: khung change của tệp agent vừa xoá/di chuyển (chưa đẩy) → huỷ lệnh xoá, kéo bản người về, steer
  (`STEER_UNDELETED`); di chuyển → bản ở chỗ mới thành tệp mới. `if_rev` GHIM lúc phát hiện xoá/di chuyển (scan), không lấy base lúc đẩy.
- **Base nhất quán**: `_download` lấy `version_id` từ `ETag` + sha256 của CHÍNH lượt tải (`LaneClient.read_to_fd`); `stat` chỉ để lấy rev khi
  metadata không khớp ETag — server đã đi tiếp thì ghi rev thấp hơn (khung mới hơn kéo lại; xoá/di chuyển với rev đó → 409).
- **Tệp lười**: đẩy tệp có base `lazy` (chưa `fs_pull`) → bản của agent thành tệp riêng, trả lại tệp trống, steer `fs_pull` (`STEER_LAZY`).
- **Thử lại**: `Idempotency-Key` lưu trong mục hàng đợi (`idem`, theo sha256 nội dung — đổi nội dung = khoá mới) và dùng lại qua mọi lần thử;
  PUT nội dung dùng `filehub.lane.write_timeout_s` (mặc định 360 s ≥ §C.6; chờ quét 300 s); 409 mà `current.sha256` = bản cục bộ → coi là đã lưu.
- **Xoá hàng loạt**: `filehub.lane.max_delete_batch` (mặc định 20, 0 = tắt) — lệnh xoá chưa duyệt + đã đẩy từ lần duyệt gần nhất vượt trần
  → GIỮ mọi lệnh xoá (`held`), `filehub.blocked{reason: mass_delete, lane: true, count, paths}` (đã có trong danh sách thông báo Telegram/Slack)
  + steer agent. Người: `cfharness filehub resume --root <id>` (RPC `filehub.resume`, HUMAN_ONLY sẵn) đẩy tiếp, `--discard` kéo tệp về lại
  + steer. Không ai quyết → cây mồ côi `code=mass_delete`; `lane orphan push` (người) = duyệt.
- **Người xoá thư mục**: tệp agent tạo/sửa chưa đẩy bên trong → đổi tên thành bản `(cfharness …)` (đẩy như bản xung đột, tạo lại thư mục
  chỉ chứa chúng), phần còn lại xoá; steer `STEER_DELETED_KEPT`.
- **Khử trùng khung**: `(id, rev)` chỉ khi khung có `rev` (khung thư mục/acl thiếu rev không bao giờ bị nuốt).
- **Tên bản xung đột** duy nhất (`conflict_name(taken=…)`: trong cây / base / hàng đợi → ` 2`, ` 3`…).
- `filehub status` (`status.lane_folders`) liệt kê thư mục chế độ lane. Test helper `finish()` chờ `end_run` xong (hết race event sau trạng thái).
- Theo §C cập nhật (phía KGS): xoá thư mục CHỈ khi rỗng → quét: `rmdir` chờ mọi mục con trong hàng đợi xong (`_due`), `fs_delete` thư mục
  xoá tệp → thư mục con (dưới lên) → chính nó; `409 not_empty` khi quét → không ép: giữ thư mục, liệt kê lại cây con, kéo mục mới về, steer
  (`STEER_RMDIR_NOT_EMPTY`). Di chuyển thư mục `403 hidden_content` / `423` → lỗi trả agent (cây cục bộ không đổi). `409 in_progress
  {retry_after}` → chờ rồi gửi lại CÙNG Idempotency-Key (không bản xung đột). Fake: `hidden`, `inflight`, `put_delay` = quét TRƯỚC khi chốt.

Sửa theo vòng kiểm chứng (2026-11-17, `tests/test_lane_verify_fixes.py` + `test_web_filehub.py::test_lane_held_mass_delete_buttons` — mỗi ca đỏ
trước khi sửa; KHÔNG đổi §C):
- **`fs_delete` = cùng đường xoá với quét** (`Workdir.tool_delete`): liệt kê cây con → mục bị bỏ qua (tệp tạm/khoá Office/`.git`) vào `hidden`,
  thư mục tổ tiên của chúng (kể cả chính thư mục được xoá) GIỮ — kết quả `kept_parent` + `note` cho agent; phần còn lại thành lệnh
  `delete`/`rmdir` trong hàng đợi (`if_rev` theo lượt liệt kê) → `_mass_delete_gate` + `del_count` chung (quá trần → `held`, kết quả
  `held: true`, `filehub.blocked{mass_delete}`) → `push_pending(final, only=…)` ghi trạng thái SAU MỖI mục. Lỗi tạm (423/429/mạng) → mục ở lại
  hàng đợi (`queued`), lỗi hẳn → mục về lại cây + `errors`. Chế độ tools: mục chưa từng kéo về (`unpulled`) chỉ được quên khi người bỏ lô.
- `recover()` giữ cờ `read_only` bền: cây của run chỉ đọc chỉ được dọn, không bao giờ đẩy.
- Người di chuyển tệp lên tên agent vừa tạo (chưa đẩy, nội dung khác) → bản của agent thành bản `(cfharness …)` TRƯỚC, tệp người về đúng tên,
  chỗ cũ không bị đẩy lại thành tệp mới.
- 409 mà `current.sha256` = nội dung một lần gửi TRƯỚC của chính agent (`prev_shas` — đã chốt sau timeout) → lấy bản đó làm base, đẩy lại MỘT
  lần với Idempotency-Key MỚI (If-Match đã đổi), không bản xung đột giả.
- Người sửa tệp lười (chưa tải) → chỉ cập nhật metadata base (vẫn `lazy`), không tải.
- Xung đột giữ bản người (`_conflict_local(keep_remote)`) CHÉP bản agent sang tên mới rồi tải đè nguyên tử — tệp gốc không lúc nào biến mất.
- Web: thẻ "Cổng cfharness" liệt kê lô xoá đang giữ theo run (`info().held`) + nút "Cho xoá"/"Không xoá" (ACTIONS `filehub_lane_deletes` →
  `filehub.resume {root_id, discard?}`, xác nhận, audit web). Chat (Telegram/Slack): có nút từ 2026-11-18 (mục "Việc tiếp theo").

Sửa theo nghiệm thu UI + e2e (2026-11-17, nhánh `fix/lane-onboarding`, `tests/test_lane_onboarding.py` — mỗi ca đỏ trước khi sửa; KHÔNG đổi §C):
- **B0 dán khoá là xong**: `LaneService.setup` → `reconcile(server, hello)`: mọi `hello.roots` chưa có thư mục → thêm `folders[]` `{mode: lane,
  server, kind: workspace|folder}` (tên `safe_name` + ` 2`, ` 3`… nếu trùng tên/bí danh); gốc đang `sync` → giữ, báo (`sync` + nút chuyển). Mỗi
  `hello` MỚI (setup, test, `prepare_run`, kiểm tra sức khoẻ, sau khung `root_revoked`) đối soát lại: gốc mới → thêm; thư mục lane của cổng mà
  gốc không còn trong `roots` → `hub_state filehub.lane.revoked` (KHÔNG xoá gì; `status.folders[].revoked`, `filehub.list status=revoked`); cấp
  lại → gỡ. Có thay đổi → event `filehub.lane.roots{server, added, revoked, regranted, sync}` (Telegram/Slack). `lane add` không đối soát trước
  (người chọn tên). `lane_disable` gỡ thư mục lane KHÔNG có `local_path` của cổng đó (chỉ cấu hình), thư mục từng sync vẫn cần `switch` trước.
  `filehub.lane_test` ∈ `MUTATING_FILEHUB` (đối soát có thể thêm thư mục → audit).
- **B0b**: `url` bỏ trống → `filehub.lane.default_urls` (8223, 8233; chỉ nhận cổng trả `hello` hợp lệ với khoá vừa dán, ưu tiên báo lỗi khác
  `offline`); `server` bỏ trống → `hello.server.instance` (khớp `SERVER_RE`, không thì `qltm`). CLI `--url/--server` tuỳ chọn; thẻ web chỉ còn
  nút + ô địa chỉ trong "Nâng cao".
- **B1**: `FilehubService.list()` thêm thư mục lane `{mode: lane, kind, server, status, local_path: null}` (tool `filehub_list` của điều phối
  viên), sync có `mode: sync`; `filehub.status.lane_configured`; `not_found` liệt kê tên có thật; thẻ "Không gian số" + `filehub status` CLI
  nói "qua cổng cfharness" khi chỉ có cổng.
- **B5**: `Workdir.held_view()` (≤ `HELD_SHOW` = 50) → `info().held_paths`, `filehub.blocked.paths` ≤ 50; thẻ web liệt kê + "và N mục nữa";
  `Action.preview` (web/app.py — chữ đọc từ daemon lúc xác nhận, áp cho cả 2D/3D) cho trang xác nhận "Cho xoá/Không xoá"; tin Telegram/Slack
  liệt kê 10 mục đầu.
- **B9/B10/B12**: `lane.STATE_TEXT` (nhãn + việc cần làm) trong `lane_status.servers[].label|hint`, `expiry.date` (giờ máy, dd/mm/yyyy) +
  `expiry.days` (nguyên); thẻ không hiện hạn khi `auth_failed`. `LaneService.health_check` (vòng nền `_health_loop`, `lane.start()`):
  `hello` mỗi `health_ok_s` (60; trước là 300) khi ổn, lùi `health_retry_s` [5, 300] khi lỗi — `offline` tự về `ok` không cần run. Nút "Thử kết nối"
  (ACTIONS `filehub_lane_test`), thao tác lane quay về `#filehub-lane`, bỏ câu "Nhập lại giá trị ở trang Cài đặt" ở bước 1 khi thao tác có
  `reenter` (khoá hỏi ở bước 2).

Sửa theo nghiệm thu lần 2 (2026-11-17, `tests/test_lane_accept_fixes.py`, mỗi ca đỏ trước khi sửa):
- Lô xoá đang giữ KHÔNG đổi nghĩa khi run kết thúc: `orphans()[]` có `root_id`, `task_id`, `held`, `held_paths`, `items[] {path, op, label, from?,
  held}` (`LaneService.change_items`, `OP_TEXT` xoá/ghi/di chuyển…); `filehub.resume {root_id, discard?}` áp cả cây mồ côi (`_release_orphan`:
  Cho xoá = đẩy ĐÚNG các lệnh xoá; Không xoá = bỏ lệnh xoá, tệp ở lại). `lane_orphan push` KHÔNG còn ngầm duyệt lô xoá (trả `held`).
  Web: lô xoá gộp theo gốc (`filehub_pages.held_batches`, `_held_batch.html`) ở thẻ + "Cần bạn" (task bị chặn → link `#held-<gốc>`); trang xác
  nhận nêu thao tác từng mục (`_orphan_preview`) và số tệp ("Xoá N tệp trên Không gian số" / "Không xoá — giữ N tệp, bỏ thay đổi của agent").
- Ô địa chỉ "Nâng cao" để trống (placeholder) → tự thử `default_urls`. Thẻ ghi "kiểm tra lúc HH:MM:SS" (`servers[].checked`).
- `ToolWriteWatcher` bỏ qua tool đọc của hub-self (`hub_self.READ_ONLY_TOOLS`) — trước đây mọi tool hub-self (cả `filehub_list`) bị audit `tool.write`.
- Điều phối viên: `open_tasks[]/task_list[]/task_get` có `wait` (câu tiếng Anh từ `wait_reason`, `manager/ops.wait_text`) — `rate_limit` nghĩa là
  "giảm số run song song vì gần hạn mức", không phải lỗi rate limit.

Việc tiếp theo (2026-11-18, nhánh `fix/lane-followups`, `tests/test_lane_followups.py` + ca trong `test_guard_classifier.py`, `test_telegram.py`,
`test_slack.py`; KHÔNG đổi §C — fake chỉ làm đủ `restore {trash_batch}` đã có trong §C.6):
- **`run revert` cho run lane** (RPC `run.revert`, HUMAN_ONLY sẵn): nhật ký thao tác ghi `before`/`after` (version_id), `trash` (lô thùng rác
  của lệnh xoá), `parents` (thư mục cha server tạo kèm `parents=1`) → `runs.diff.revert_ops` (≤ 2000, không nội dung) + `revertible: true`.
  `LaneService.revert_run`: KIỂM TRƯỚC trạng thái cuối run của từng đường dẫn (phiên bản = `after`, chỗ run để trống vẫn trống; đổi tên thư
  mục kéo theo con) — khác → `Conflict{conflicts}`, không ghi gì; rồi đi NGƯỢC: sửa → `restore {path, version_id: before}`, tạo → `DELETE`
  (thùng rác, + thư mục cha tạo kèm nếu rỗng), xoá → `restore {trash_batch}`, đổi tên → `move` lại. Lỗi giữa chừng → `HubError{partial}`
  (mọi bước đã làm vẫn khôi phục được). Từ chối khi cây còn dùng/đỗ/mồ côi (thay đổi chưa đẩy). Run cũ (không có `revert_ops`) → từ chối
  như trước. Web: nút "Hoàn tác trên Không gian số" + "Kiểm tra trước" (`dry_run`).
- **Presence khi chờ người** (theo §4.5 "waiting_human giữ cây + presence"): `LaneService.parked_presence` (vòng sức khoẻ 1 s) làm mới
  presence của cây đỗ ở nửa ttl CHỈ khi run `waiting_human`; `interrupted` → để hết hạn; run đã kết thúc → xoá presence.
- **Cây lane của run khác**: sandbox denyRead + denyWrite ĐƯỜNG DẪN CỤ THỂ mọi `agents/*/workspaces/lane-*` có lúc spawn trừ cây của chính run
  (`guard/runtime.lane_tree_dirs`, cả `confine_only_sandbox` của trust full); classifier `lane_other_tree` (cấm, ∈ `TRUST_NEVER_RULES` +
  `NEVER_DOWNGRADE_RULES`) cho Read/Grep/Glob/Edit/Write + Bash (tuyệt đối, `~`, `../lane-…`) — kiểm mỗi lần gọi tool nên phủ cả cây tạo
  sau lúc spawn (sandbox thì không).
- **Mọi run được đọc** (quyết theo plan-cloud-files §7.4 — header plan này "GIỮ NGUYÊN"): run làm việc KHÔNG có workspace lane (trừ
  review/eval/curator/graph_extract) được hub-self `--fs browse` = `fs_list|fs_stat|fs_read|fs_versions|fs_link` chỉ đọc server, đường dẫn
  `<tên thư mục>/<…>` (`fs_list ""` = danh sách thư mục; gốc bị thu hồi không hiện/không đọc). Không cây, không `fs_pull`. Khối prompt
  "Shared folder" nói điều này. Tắt: `filehub.lane.read_all_runs: false`.
- **Telegram/Slack**: nút "Cho xoá"/"Không xoá" cho `filehub.blocked{mass_delete, lane}` (ChatBot core, `lx|lk` → bước 2 đọc lại
  `filehub.lane_status` theo `root_id`, tin danh sách riêng sau tiền tố cố định, tin nút riêng → `lxc|lkc` → `filehub.resume` qua `self.call`).

Việc còn lại:
- Nghiệm thu thật trên qltm sau khi K1–K4 lên (plan §7 H4); `tested` với claude thật (steer + Write/Edit trong cây); kiểm thật sandbox chặn
  `cat <cây lane khác>` từ Bash.
- H5 (gỡ đồng bộ cũ) — ngoài phạm vi.
