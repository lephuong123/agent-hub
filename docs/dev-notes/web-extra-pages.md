# Web — trang bổ sung cho ngang CLI (lịch chạy, tìm kiếm hội thoại, diff/hoàn tác run, sức khoẻ)

CLI/daemon đã có `schedule.*`, `session.search`, `run.diff|revert` nhưng web chưa có màn nào. Nhánh `feat/web-missing-pages` thêm
các màn đó. Không thêm RPC, không migration; web vẫn là client thuần của daemon.

## File
| File | Vai trò |
|---|---|
| `web/extra_pages.py` | Handler + `register_actions(ACTIONS, Action, _req)` + `routes()` (cùng mẫu với `web/manager.py`). Hàm thuần: `split_patch`, `diff_view`, `snippet_parts`, `schedule_params` |
| `web/app.py` | Chỉ thêm `_extra_routes()` (đăng ký thao tác vào `ACTIONS` + trả route), gọi trong `create_app`. Không import `extra_pages` ở mức module để tránh vòng import khi test import `extra_pages` trước |
| `templates/schedules.html`, `search.html`, `_run_diff.html`, `run_diff.html`, `_health.html` | Màn mới |
| `templates/base.html` | Menu: "Tìm kiếm" (nhóm Làm việc), "Lịch chạy" (nhóm Đội ngũ), cả trong sheet "Thêm" trên điện thoại; ô tìm kiếm ở thanh bên (ẩn khi thanh bên thu gọn / màn vừa) |
| `templates/run.html`, `settings.html` | Mỗi file một dòng: chỗ tải lười bằng htmx (`hx-trigger=load`, `hx-swap=outerHTML`) |
| `static/icons.svg` | Icon mới `i-search`, `i-calendar` (cùng nét 1.75, 24×24) |
| `static/app.css` | Khối cuối file "trang bổ sung" |
| `workspace.py` | Sửa lỗi: `diff.patch` ghi với `newline=""` — trên Windows trước đây nội dung CRLF thành `\r\r\n`, đọc lại ra dòng trống thừa (cả `cfharness run diff --patch` lẫn web) |

## Route / thao tác
| Route | RPC |
|---|---|
| `GET /schedules[?status=active\|paused\|done][&preview=<biểu thức>]` | `schedule.list`, `schedule.preview` (thử biểu thức), `agent.list`, `team.list` |
| `POST /schedules/create` | lần 1: `schedule.preview` (kiểm biểu thức) → trang xác nhận phía server (đích, mốc chạy tới, lượt/ngày, ngân sách); lần 2 `confirm=yes` → `schedule.create`. Lỗi → vẽ lại trang, giữ giá trị đã nhập, 400 |
| `GET /search?q=&agent=&since=1d\|7d\|30d\|90d&role=&technical=1&offset=` | `session.search{mode:search}` (30 kết quả/trang). Bộ lọc lạ bị bỏ qua |
| `GET /partials/run/{id}/diff` | `run.get` + `run.diff{patch:true}` — rỗng nếu run không ghi file (khối biến mất khỏi trang run) |
| `GET /runs/{id}/diff` | như trên, trang đầy đủ (dùng khi không có JS — `<noscript>` trên trang run) |
| `GET /partials/health` | `hub.status`, `hub.compat`, `guard.integrity.status` (đều chỉ đọc) |

`ACTIONS` mới (form POST `/do/<tên>`, CSRF + khoá phiên WT-1, `_audit.channel=web`; daemon tự audit vì các method nằm trong `MUTATING*`):
`schedule_pause`, `schedule_resume`, `schedule_run_now` (chuyển tới run/task vừa tạo), `schedule_rm` (danger, xoá mềm), `run_revert`
(danger), `run_revert_check` (`dry_run`, không ghi file — daemon vẫn ghi một dòng audit `run.revert` kênh web).
Mọi thao tác ghi đều có hộp xác nhận (JS `data-confirm`; không JS → trang xác nhận của `do_action`), trừ `run_revert_check`.

## Quyết định
- **Đoạn trích tìm kiếm**: `session_search._snippet` đánh dấu `«…»`; web tách thành đoạn ở server (`snippet_parts`) và template escape từng
  đoạn, chỉ bọc `<mark>` — không dùng `|safe` (chữ người/agent có thể chứa HTML; có test `<script>`).
- **Diff**: phân tích patch unified theo số dòng của header `@@` (dòng nội dung bắt đầu bằng `--- ` không bị hiểu nhầm là đầu file mới).
  Trần: 600 000 ký tự patch được phân tích, 400 dòng/file, 150 file; quá trần → thông báo + lệnh `cfharness run diff <run> --patch`.
  ≤ 4 file → mở sẵn, nhiều hơn → gập (`<details>`). File đã bị sửa tiếp sau run (`modified_since`) được tô đỏ + banner liệt kê, và
  KHÔNG hiện nút "Hoàn tác" (chỉ "Kiểm tra lại"). Ép gọi vẫn an toàn: daemon trả `Conflict` → toast lỗi nêu file, không ghi gì.
- **Hoàn tác bị ẩn khi**: đã hoàn tác, workspace không phải git, checkpoint đã bị retention xoá, có file quá lớn lúc checkpoint, run/session
  còn chạy, hoặc run không sửa file — kèm câu giải thích. Hộp xác nhận nói rõ: chỉ được khi file chưa bị sửa tiếp, không đổi HEAD/nhánh.
- **Khối diff tải lười** để trang run không phải đọc patch (có thể lớn) mỗi lần mở, và để không sửa `run_page` trong `app.py`
  (hai nhánh khác cũng đang sửa `app.py`).
- **Sức khoẻ**: không có RPC `doctor` (lệnh `cfharness doctor` chạy `claude --version`/`auth status` cục bộ) → thẻ chỉ dùng RPC đọc sẵn có:
  daemon, cờ "claude chưa đăng nhập" (`auth_block`), sandbox, ESTOP, rate limit, phiên bản CLI đã kiểm (`hub.compat`), tỉ lệ event lạ,
  toàn vẹn chính sách. Thẻ nhắc chạy `cfharness doctor` để kiểm đầy đủ.
- **Tạo lịch có trang xác nhận** (CLI không hỏi): lịch là việc tốn tiền lặp lại; trang xác nhận hiện cách hub HIỂU biểu thức trước khi tạo.

## Test — `tests/test_web_pages_extra.py` (9 ca, không gọi `claude`)
Hàm thuần (`split_patch` với dòng giống header, trần/điều kiện hoàn tác của `diff_view`, `snippet_parts`, `schedule_params`); chưa đăng nhập
→ 303/401, thiếu CSRF/khoá phiên → 403; trang Lịch chạy + menu + xem trước biểu thức; tạo lịch (xác nhận → tạo, biểu thức sai/agent không có
→ 400 giữ giá trị), tạm dừng/bật lại/chạy ngay/xoá + audit kênh web; tìm kiếm không dấu khớp có dấu, `<mark>`, link `/runs/<id>`, escape
`<script>`, lọc agent/thời gian/vai trò; diff (repo git tạm gắn vào run): khối rỗng khi không ghi file, kiểm tra trước, xung đột khi người
sửa tiếp (không ghi đè), hoàn tác thật + audit, trạng thái "Đã hoàn tác"; thẻ sức khoẻ.

## Chưa làm
- Sửa lịch (`schedule.update`) từ web — dùng `cfharness schedule edit`. Xem chi tiết các lượt (`schedule.get{fires}`) — chỉ hiện lượt gần nhất.
- Tìm kiếm chế độ `around`/`read`/`browse`, gom kết quả theo luồng.
- `run.diff` luôn trả cả patch qua RPC (chưa có tham số giới hạn kích thước phía daemon); web chỉ cắt khi vẽ.
- Chưa chụp ảnh màn hình mới vào `docs/screenshots/web/`.
