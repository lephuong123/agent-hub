# Plan: tối ưu gọi API — cache và bỏ gọi lặp

Ngày: 2026-11-25. Nguồn: 4 agent phân tích chỉ đọc theo 4 tầng (trình duyệt → web, web/MCP/TUI → daemon RPC,
gọi ra ngoài và subprocess, dữ liệu trong daemon). Các con số đánh dấu *(đo)* là đo thật; còn lại là ước tính từ code.

## Tóm tắt

Có ba nguồn lãng phí lớn:

1. **Daemon tự làm chậm mình ở mỗi event.** `HubConfig.get()` deep-copy toàn bộ `DEFAULTS` mỗi lần gọi:
   **1,65 ms/lần** *(đo)*, 387 chỗ gọi. Hàm này bị gọi khoảng 5–6 lần cho mỗi event stream (kể cả từng `message.delta`)
   và 12–20 lần cho mỗi tool call qua guard. Tính ra khoảng 8–10 ms CPU trên event loop cho một event.
2. **Gọi đồng bộ chặn event loop.** Handler RPC chạy thẳng trên loop (`daemon.py:550`). Các lời gọi `system.list`
   (PowerShell, tới 30 s), `claude --version` (tới 20 s) và guard `git rev-parse` (3 s) làm daemon đứng hẳn trong lúc chờ:
   Slack mất ACK, stream-json không được đọc, mọi RPC khác phải xếp hàng.
3. **Web poll và gọi lặp dù đã có SSE.** Run-bar poll mỗi 2 s, Tổng quan mỗi 3 s, trong khi SSE đã báo đúng các
   thay đổi đó. Mỗi trang gọi 4 RPC chỉ để vẽ badge, một trong số đó kéo về 500 dòng chỉ để đếm. `tui.snapshot`,
   handler nặng nhất, bị gọi 2 lần mỗi lần tải Tổng quan, rồi bị poll tiếp. Mỗi RPC lại mở một kết nối mới và
   phải xác thực lại từ đầu.

## Nguyên tắc chung

- Daemon là writer duy nhất, nên cache trong RAM của daemon có thể **xoá đúng lúc ghi** thay vì chờ TTL. Ngoại lệ là
  `hub_state`, vì nó bị ghi trực tiếp ở `hub_freeze`, `hub_transfer` (kể cả từ tiến trình CLI), `transfer_repos`,
  `session_search`. Chỉ cache những khoá nóng, và có đường xoá cache tường minh.
- Mỗi daemon/web phục vụ một user OS, nên cache trên `WebState` dùng chung được. Nếu sau này các phiên có quyền khác
  nhau thì khoá cache phải thêm phiên.
- Không cache `access_token` ở đâu ngoài secret store. File cache trong `HUB_HOME` có thông tin định danh thì đặt quyền 0600.
- Chỉ retry tự động với **method chỉ đọc**; method ghi có thể bị thực thi hai lần.
- Migration index **thêm vào cuối** `MIGRATIONS`, không sửa migration cũ.
- Mỗi PR có số đo trước/sau (microbench hoặc bộ đếm RPC), không chỉ cảm nhận.

## Đo trước khi sửa (PR0, nhỏ)

- Microbench đường publish: 1.000 `message.delta` qua `hub.publish` có đủ listener, đo tổng CPU.
- Bộ đếm RPC theo request web, ghi vào log debug (`X-Rpc-Count` khi bật debug), để so số RPC mỗi trang trước/sau.
- `EXPLAIN QUERY PLAN` cho các query liệt kê ở PR3, lưu kết quả vào ghi chú này.

## Các PR, xếp theo tác động / công sức

### PR1 — Config và listener trên đường publish *(lợi lớn nhất, sửa nhỏ)*
- `config.py`: cache sẵn bản `merged` mỗi lần `reload()`. `get(key)` lấy subtree từ cache: giá trị vô hướng trả
  thẳng, dict/list thì chỉ deepcopy riêng subtree đó. `data` giữ deepcopy như cũ.
  `_maybe_reload` chỉ gọi `stat()` một lần (thay cho `exists()+stat()`). Throttle 250 ms theo `monotonic`, và `set()`
  vẫn `reload()` ngay nên ghi qua RPC có hiệu lực tức thì.
- `notify/base.py:68-70`: trả về sớm bằng một `frozenset` các loại event có thể được thông báo, đặt **trước** `cfg()`
  và `frozen()`. Hiện cả 3 notifier đều chạy SQL `state_get` cho mỗi delta.
- `manager/service.py:1020`: cache `agent_id()` trong RAM, xoá khi `ManagerService` tự ghi.
- `store.py:1124`: bỏ lần `_mask_secrets` thứ hai, vì `hub.publish` đã mask rồi. Hoặc đánh dấu event đã mask.
- `runs.py:1117` `_heartbeat`: đưa phần đọc config ra sau bước throttle.
- Test: test hành vi cho `get` (sửa giá trị trả về không làm hỏng cache), microbench có ngưỡng.
- Rủi ro: thấp. Sửa tay `config.yaml` có hiệu lực trễ tối đa 250 ms.

### PR2 — Không chặn event loop
- `systems/rpc.py:46-47`: bọc `system.list/get` bằng `asyncio.to_thread`. Thêm singleflight (nhiều yêu cầu dùng
  chung một lần query PowerShell). `cache_s` từ 2 s lên 15–30 s; các thao tác ghi đã xoá `_cache`.
- `runtime/claude_cli.py:392/523/551`: `version()` đổi sang stale-while-revalidate, tính trong thread, có lock,
  theo mẫu `pdf_support.py`. TTL 1 h, hoặc khoá theo `mtime` của binary.
- `claude_cli.py:470-506` `login_available` / `auth_status`: singleflight bằng `asyncio.Future` theo khoá, cache
  cả kết quả `None` 30–60 s, gộp `auth_status` trong cửa sổ 5 s.
- `guard/classifier.py:1133,3354`: cache `rev-parse --git-dir/--git-common-dir/--show-toplevel` theo (run, cwd).
  `status --porcelain` không cache. Chạy trong thread.
- Rà thêm: tìm mọi `subprocess.run` hoặc HTTP đồng bộ còn lại trên đường handler RPC.
- Test: handler chậm giả lập không làm một RPC khác trễ quá X ms.

### PR3 — `tui.snapshot`, `cost_today` và index
- `tui/rpc.py:196`: đổi `type LIKE 'learning.%'` thành `type >= 'learning.' AND type < 'learning/'` để dùng
  được index `events_type_ts` (hiện có khả năng quét toàn bảng `events`).
- `tui/rpc.py:129-147`: bỏ N+1 (`get_run`, `list_runs(limit=1)`, `_task_title` cho từng agent) bằng một query JOIN.
- Cache snapshot ở daemon, khoá theo `(bus.last_seq, approvals_rev)`, TTL 0,5–1 s. Lợi cho cả TUI (poll mỗi 1 s) và web.
- Thêm `tui.snapshot {agent_id}` (hoặc `agent.live {id}`) cho trang run, trang agent và `mgr_state`.
- `daemon.py:221` `_agent_list`: N lần `cost_today` gộp thành một `GROUP BY agent_id`. `hub.py:327`: đổi
  `list_agents()` sang `COUNT(*)`. Cân nhắc giữ map chi phí theo ngày trong RAM, cộng dồn ở `_record_cost/_finalize`.
- Migration mới (cuối danh sách):
  `runs(ended_at)`, `runs(agent_id, ended_at)`, `tasks(current_run_id)`, `runs(kind, queued_at)`,
  `events(agent_id, epoch, seq)` (chỉ thêm đúng index này cho `events`, vì đây là bảng ghi nhiều nhất).
- `PRAGMA optimize` khi mở/đóng Store và chạy định kỳ.
- Test: so `EXPLAIN QUERY PLAN` trước/sau; snapshot có cache trả đúng sau mỗi event.

### PR4 — Web: bớt RPC mỗi trang
- RPC mới `ui.badges` trả về `{approvals, waiting_human, estop, admin_estop, questions_open, migrated_to}` bằng `COUNT(*)`,
  thay 4 RPC trong `render()` (`web/app.py:392-410`). Cache 2–3 s trên `WebState`, xoá khi firehose báo `approval.*`,
  `question.*`, `hub.estop`. Gộp luôn `nav_badges`.
- Memo `rpc()` theo request (`request.state`, khoá là method + params, **chỉ method đọc**). Bỏ được các chỗ lặp
  như inbox gọi `approval.list` 2 lần, overview gọi `question.list` 2 lần và `tui.snapshot` 2 lần.
- Dùng `asyncio.gather` cho các lời gọi độc lập: `agent_page` (8), `run_page` (9), `manager_page` (14).
- `run_page`: truyền `run` đã có vào `_run_ctx`, không gọi `run.get` lần nữa. Dùng RPC nhẹ `bus.position` thay
  `hub.status` khi chỉ cần `epoch/last_seq`.
- `task_titles` (`app.py:1025`): thay `task.list {limit:1000}` bằng `task.titles {ids}`, hoặc JOIN sẵn trong `run.list`.
- `_workstreams` (`web/manager.py:127`): gộp 3 RPC thành `workstream.overview`; chỉ gọi `conversation_read` khi thật
  sự có tin chưa đọc.
- Đo: số RPC mỗi trang trước/sau (bộ đếm ở PR0).

### PR5 — Web: polling theo SSE, dừng khi tab ẩn
- Run-bar (`templates/run.html:14`): bỏ `every 2s` khi SSE đang mở. Bật poll dự phòng 15 s khi `es.onerror`, tắt lại ở
  `onopen`. Bỏ chỗ `app.js:484` gọi `htmx.ajax` thêm một lần mỗi khi nhận `refresh`.
- Tổng quan (`overview.html:16,28`): đổi sang `hx-trigger="ov-dirty from:body, every 30s"`; firehose phát `ov-dirty`
  có debounce 1 s. Cần kiểm mọi thay đổi trạng thái agent đều có event summary, nếu không thì poll 30 s sẽ bù.
- Một listener chung trong `app.js`: trên `htmx:beforeRequest`, nếu `document.hidden` và trigger có `every` thì
  `preventDefault()`. Trên `visibilitychange` khi tab hiện lại thì phát `cfh-wake` để tải ngay một lần.
- Điều phối: mgr-status chạy theo SSE `status/run_end`, poll dự phòng 60 s; poll tab từ 20 s lên 60 s.
- Kiểm bằng trình duyệt thật (Playwright): đếm request/phút ở mỗi trang khi tab mở và khi tab ẩn.

### PR6 — Static asset
- `web/app.py:219`: URL có `?v=` thì trả `public, max-age=31536000, immutable`. `vendor/three-0.169.0/*` cũng vậy.
  `office/*.js` giữ `no-cache`, hoặc thêm version qua import map.
- `static_url()` (`app.py:2096`): memo mtime theo tiến trình, vì macro `icon()` hiện gọi `stat()` cho mỗi icon.
- Tuỳ chọn: gzip **chỉ cho `/static/`**. Không nén HTML có csrf (rủi ro BREACH) và không đụng `text/event-stream`.

### PR7 — Kết nối RPC và xác thực *(nhạy cảm bảo mật, làm sau cùng trong nhóm daemon)*
- `AsyncHubClient` (`tui/client.py:44`): pool 2–4 kết nối giữ lâu, mỗi kết nối xử lý tuần tự một request.
  Kết nối hỏng thì mở lại; **chỉ gửi lại** với method chỉ đọc.
- Cache token client theo `(path, mtime, ino)`, đọc lại khi gặp lỗi auth.
- Daemon: cache kết quả `CallerResolver.resolve` theo **từng kết nối**, TTL khoảng 5 s. Không giữ trạng thái "không
  phải agent" lâu hơn vòng đời một run (pid có thể bị tái dùng, hoặc một run mới nhận nó làm con).
- `mcp_server/cli.py:66` `DaemonCaller`: dùng lại client thay vì tạo `RpcClient` mới mỗi lần gọi.
- Lợi lớn nhất trên Windows: mỗi lần gọi bỏ được một lần quét bảng TCP, một lần snapshot tiến trình và một lần đọc PEB.
- Cần review bảo mật riêng (đối chiếu `docs/dev-notes/hardening-SEC.md`).

### PR8 — Gọi ra dịch vụ ngoài
- Cài đặt: `web/account_pages.py:112` hiện chạy `claude auth status` mỗi lần tải trang. Thêm cờ `cached` cho
  `account.claudeai_status`, như khối MCP đang làm. Chỉ probe lại khi cache hết hạn, khi người dùng bấm, hoặc sau login/logout.
- Slack, Telegram: giữ một `httpx.AsyncClient` sống lâu cho mỗi đối tượng API (`chat/slack/api.py`,
  `notify/telegram.py`, `chat/slack/media.py`), theo mẫu `filehub/lane/service.py:222`. Tạo lại khi token đổi.
- MCP OAuth (`mcp_client/oauth.py:625`): kiểm lại `expires_at` sau khi lấy lock (double-checked), không refresh lại
  sau `invalid_grant`, backoff khi lỗi mạng, `gather` các server.
- Catalog (`mcp_client/catalog.py`): cache kết quả lỗi 60 s, `check_oauth` song song, `_mem` thành LRU khoảng 200 mục,
  dọn file cache cũ hơn 7 ngày.
- Mục nhỏ:
  - updater: thêm ETag/`If-None-Match`, lưu `HUB_HOME/update/latest.json` và singleflight.
  - Slack `users.info`: `_fill_name` dùng cache; `_names` thành LRU kèm TTL 24 h; `gather` các uid.
  - `ClaudeMcpAdmin.status`: kiểm lại cache sau khi lấy lock.
  - `QueuedNotifier`: tôn trọng `Retry-After`.
  - OTel: circuit breaker 60 s khi endpoint chết.

### PR9 — Panel xem file *(thấp, tuỳ chọn)*
- `files.js:142`: cache phía client `Map(href → html)` với TTL 30–60 s, khoá có kèm mtime; xoá khi SSE vẽ lại chip của file.
- Ảnh/PDF: `file.read {meta_only}` để không đọc 1 MB rồi bỏ; `/files/raw` trả ETag = mtime + size kèm `private, no-cache`.

## Thứ tự đề xuất

PR0 → PR1 → PR2 → PR3 → PR4 → PR5 → PR6 → PR8 → PR7 → PR9.

- PR1, PR2 cho lợi lớn nhất với ít code nhất, và không đổi giao diện.
- PR3, PR4, PR5 cần làm cùng lúc thì mới thấy hết lợi ích ở web: bớt RPC, mỗi RPC nhẹ hơn, và bớt poll.
- PR7 đụng tới xác thực nên để sau cùng, kèm review bảo mật.

## Quyết định đã chốt (2026-11-25)

1. Badge, Tổng quan, snapshot được phép cũ **1–3 s**.
2. **Audit mỗi lần xem file.** PR9: dù nội dung lấy từ cache phía client, mỗi lần mở panel vẫn gửi một lời gọi audit
   nhẹ (không đọc lại nội dung file) để daemon ghi dòng audit như hiện nay.
3. **Có** thêm index `events(agent_id, epoch, seq)`.
4. PR7 (pool kết nối, cache resolve) **làm trong đợt này**, kèm review bảo mật.

## Những chỗ đã tốt, giữ nguyên làm mẫu

- Bảng Công việc: SSE gom loạt 400 ms, chỉ poll khi mất SSE, hoãn khi đang gõ.
- Run-bar trả 286 để dừng poll khi run xong. Office chạy theo SSE, có debounce và dừng khi tab ẩn.
- `nav_badges` / `admin.nav` cache 60 s; `update.check` cache 10 phút, có cache cả lỗi và chạy trong thread.
- `claude mcp list`: TTL 30 s, lock, chế độ `cached`. Second opinion: TTL, double-checked lock, giới hạn 2000 mục
  (mẫu tốt nhất để chép).
- Slack `auth.test` và Telegram `getMe` chỉ gọi lại khi fingerprint token đổi. Lane filehub dùng client bền.
- `PolicyStore` và registry hub cache theo mtime; `Aggregator` giữ last_tool/usage; `error_stats` đếm trong RAM.
