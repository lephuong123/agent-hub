# Đo hiệu năng — phần nào chậm (FE / web / daemon)

Bối cảnh: người dùng báo chuyển tab menu, chuyển luồng ("Luồng" ở trang Điều phối) và trả lời chậm. Công cụ này cho biết chậm ở
trình duyệt, ở tiến trình web hay ở daemon — BẬT sẵn, chi phí gần bằng 0, dữ liệu chỉ ở RAM (tối đa 60 phút, mất khi khởi động lại).

Dùng lại cái đã có: access log `cfharness.rpc` của daemon (đã đo `duration_ms` mỗi RPC, `logs.slow_rpc_ms` → `rpc.slow`) giờ nạp thêm vào
tổng hợp cuộn; log web vẫn là `web.jsonl` (`logs.setup_logging`). Không có hệ thống song song, không bảng DB mới.

## Đo gì

| Phía | Metric | Ở đâu |
|---|---|---|
| Trình duyệt (`static/perf.js`) | `nav.ttfb`, `nav.dcl`, `nav.load` (Navigation Timing, tính từ đầu điều hướng) | mỗi lần tải trang |
| | `htmx` — `htmx:beforeRequest` → `htmx:afterSettle` (lỗi/204/286: `afterRequest`), khoá theo route của request | mỗi request htmx (kể cả polling) |
| | `switch.tab` / `switch.ws` — bấm link menu (`nav.nav`, `nav.tabbar`) / tab luồng (`.ws-tab`, form "Chuyển") → trang mới `load` xong (mốc lưu `sessionStorage`, bỏ nếu > 30 s hoặc trang đích khác) | chuyển tab/luồng |
| | `sse.first` — mở/nối lại EventSource → message đầu tiên | mọi luồng SSE (bọc `window.EventSource`) |
| | `sse.lag` — `ets` (ts của event trong daemon) → lúc trình duyệt nhận; tối đa 2 mẫu/giây/luồng. Hai máy lệch đồng hồ (qua tunnel) thì số này lệch theo | `/sse/run`, `/sse/firehose`, `/sse/tasks`, `/sse/manager` |
| | `longtask` — JS chặn luồng chính ≥ 50 ms (Chrome/Edge) | |
| Web (`web/perf_pages.py`) | thời gian mỗi request theo `METHOD route-template` (không tính `/sse/*`, `/static/*`, beacon) + phân rã: số lần/ms RPC theo method, ms render template | `PerfMiddleware` |
| | RPC theo method như web thấy (gồm mở socket + chờ) — cột "p95 web thấy" | |
| Daemon (`perf.py`) | thời gian xử lý mỗi RPC theo method (mọi client), trừ method chờ lâu theo thiết kế (`daemon.SLOW_EXEMPT`) | `Daemon._access_log` |
| | `loop.lag` — độ trễ event loop của daemon: mỗi giây ngủ 1 s rồi so giờ thức thật với giờ hẹn (trễ = loop bị code đồng bộ chặn). Nằm trong bảng RPC daemon như một khoá riêng; `perf.stats.loop` có thêm giá trị gần nhất + lớn nhất từ lúc khởi động. Trên Windows nền ~0–16 ms (độ phân giải đồng hồ) | `Daemon._loop_lag` |

Tổng hợp: thùng 1 phút × 60, mỗi thùng giữ count/sum/max + 32 mẫu ngẫu nhiên (`array('f')`) → p50/p95 có trọng số; tối đa 256 khoá mỗi
bảng (thừa → `(khác)`). Bộ nhớ trần ~2 MB mỗi bảng.

Khoá chỉ là metadata: route template (`/runs/{id}`, không bao giờ id thô), tên method, tên metric. Beacon gửi đường dẫn trang, server
ánh xạ về route template bằng chính router; không nội dung trang/tin nhắn.

## Xem ở đâu

- Web: **Hệ thống → Hiệu năng** (`/perf?w=15`) — 3 bảng sắp theo p95: Trình duyệt, Web (BE), RPC daemon. JSON: `/perf.json?w=15` (cần phiên web).
- Từng request: devtools → Network → chọn request → **Timing** — header `Server-Timing`: `app` (tới lúc gửi header), `rpc` (tổng, số lần),
  `tpl` (render), `rpc-<method>` (top 6). RPC chạy đồng thời (`gather_ordered`) → `rpc` là TỔNG thời gian từng RPC, có thể lớn hơn `app`.
- Request chậm: `/logs?logger=cfharness.web.perf` hoặc `cfharness logs --proc web --level warning` — dòng `http.slow` có `route`, `status`,
  `duration_ms`, `rpc_n`, `rpc_ms`, `render_ms`. RPC chậm phía daemon: `rpc.slow` trong `hub.log` (đã có từ L2).
- Terminal / agent của người: `cfharness perf [--window 15] [-n 15] [--json]` → RPC `perf.stats` (daemon RPC + ảnh chụp FE/BE mà web đẩy
  lên qua `perf.report` tối đa mỗi 30 s). Cả hai RPC HUMAN_ONLY; `perf` ∈ `HUB_HUMAN_ONLY_CLI` (agent trong run không gọi được).

Đọc nhanh: `switch.*` cao mà `nav.ttfb` cũng cao → chậm ở server: xem bảng Web, route đó có `rpc` lớn không, rồi bảng RPC daemon xem
method nào. `nav.ttfb` thấp mà `nav.load`/`longtask` cao → chậm ở trình duyệt (JS/CSS/ảnh). Mọi trang đầy đủ đều gọi `render()` →
`approval.list` + `estop.get` + `question.list` + `hub.frozen` + badge admin/inbox — chi phí nền của mỗi lần chuyển tab.

## Cấu hình (`config.yaml`, `cfharness config set …`)

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `perf.enabled` | `true` | `false`: web không đo, không thêm `Server-Timing`, bỏ beacon (daemon vẫn đo RPC — một lệnh cộng trong RAM) |
| `perf.slow_ms` | `1500` | request web chậm hơn → WARNING `http.slow` (0 = tắt) |
| `perf.window_min` | `15` | cửa sổ mặc định của bảng (1–60) |
| `perf.loop_lag_warn_ms` | `250` | event loop daemon bị chặn lâu hơn → WARNING `loop.lag` (`duration_ms` = thời gian bị chặn) trong `hub.log` (0 = tắt cảnh báo, vẫn đo) |

Web không đọc `config.yaml`: nhận `perf.*` trong phản hồi `perf.report` (≤ 30 s sau khi đổi). Ngưỡng `rpc.slow` vẫn là `logs.slow_rpc_ms`.

## RPC đọc chạy ngoài event loop

Daemon chạy mọi handler trên MỘT event loop; truy vấn SQLite đồng bộ chậm làm đứng mọi RPC khác và mọi luồng SSE. Các RPC chỉ đọc nặng
(`daemon.OFFLOAD_READS`: `run.list`, `events.query`, `task.list`, `learn.feed`, `learn.proposals`, `report.get|hangs|kpi|overhead|cost`,
`approval.stats`, `audit.list|activity`, `system.list`) chạy qua `Store.read` trên 2 luồng đọc, mỗi luồng một kết nối `mode=ro` (WAL cho
đọc song song với writer). Trên luồng đọc `store.conn` trả kết nối chỉ đọc, nên service đọc qua `store.conn` không phải sửa; lỡ ghi → lỗi
`readonly database` (vẫn một writer duy nhất). Kết nối ghi ở chế độ autocommit, `tx()` không giữ qua `await` → mọi lần ghi trước lượt đọc đã
commit, lượt đọc thấy ngay. Thêm method: handler đồng bộ, chỉ SELECT, không đụng trạng thái RAM dùng chung → thêm vào `OFFLOAD_READS` + ca
so kết quả trong `tests/test_daemon_offload.py`. Đo trên bản sao hub.db 1,5 GB: xem PR `perf/daemon-offload-reads`.

Còn trên loop (chưa chuyển): `memory.search` (0,8–1,4 s lần đầu — graph/vector giữ kết nối riêng, cần xem riêng), `learn.status`
(đọc policy), `tui.snapshot`/`tui.agent_state`/`hub.status` (trộn trạng thái RAM), backfill khi mở `events.subscribe`.

## Bảo mật beacon

`POST /perf/beacon`: `navigator.sendBeacon` không đặt được header → thân `text/plain` chứa JSON `{csrf, k, s: [{m, r, d}]}`; server kiểm
phiên + `origin_guard` + CSRF + khoá phiên ngoài cookie (`k`) như `post_guard`, trần 32 KiB và 200 mẫu (413), `m` ∈ `FE_METRICS`, `d` trong
0..600 000 ms, `r` phải bắt đầu `/`; mẫu sai bị bỏ lẻ. Beacon không gia hạn phiên (như polling/SSE). Trả 204.

## Chưa làm

- Không lưu mẫu xuống đĩa (ngoài dòng `http.slow`/`rpc.slow` trong log) — khởi động lại là mất bảng.
- Không đo thời gian "trả lời" của điều phối viên/agent end-to-end (gửi tin → reply) — hiện chỉ thấy qua `sse.lag` + RPC `manager.chat`.
- `sse.lag` phụ thuộc đồng hồ hai phía (cùng máy thì chính xác).
- Test: `tests/test_perf_monitor.py`, `tests/test_daemon_offload.py` (đọc ngoài loop + `loop.lag`).
