# Trang Công việc realtime (`/tasks`, `/tasks/<id>`) — 2026-11-12

Trước: bảng kanban vẽ một lần, đổi trạng thái task phải tải lại tay. Giờ cột/thẻ tự cập nhật.

## Cách chạy
| Phần | Vai trò |
|---|---|
| `GET /sse/tasks[?task=a,b]` (`sse_tasks`, app.py) | Dùng lại `_stream()`: level `summary`, `since_seq` = `hub.status.last_seq` (KHÔNG phát lại lịch sử), epoch hiện tại. Chỉ phát `{"t":"refresh"}` |
| `tasks_refresh(ev, task_ids)` (thuần) | `task.*` (trừ `task.background` = tool Task của claude, mức detail); `question.*`, `run.started` và run kết thúc (`TERMINAL_RUN_EVENTS`) CHỈ khi event có `task_id` (chi phí, "chờ bạn" trên thẻ). Có `task_ids` → chỉ event của các task đó |
| `GET /partials/tasks` (`_tasks_board.html`) | Cùng dữ liệu với `/tasks` qua helper `_tasks_board()` (tham số `team`, `all`) |
| `GET /partials/task/<id>` (`_task_main.html` + `_task_comments.html` oob) | Phần trên trang task (tiêu đề + nút, câu hỏi, chi tiết, DAG, lượt chạy) + danh sách bình luận (`hx-swap-oob`). Form bình luận nằm NGOÀI → không mất chữ đang gõ. Trang task lọc theo id của nó + cha/con/việc con |
| `app.js` `[data-sse-tasks]` | EventSource → gom loạt 400 ms → `htmx.ajax(GET, data-refresh + location.search, outerHTML)`. Thẻ đổi (`data-id`: trạng thái/chữ khác) được thêm `.tcard-changed` 2,5 s; `prefers-reduced-motion` → viền tĩnh, không animation |

GuardMiddleware: các đường dẫn mới bắt đầu bằng `/sse/`, `/partials/` → chưa đăng nhập là 401 (không chuyển /login), không gia hạn phiên.

## Quyết định
- **Không dùng `hx-trigger="every 15s"`** trên bảng: nó chạy cả khi SSE đang tốt → tải đôi. Thay bằng hỏi lại 15 s CHỈ khi SSE không mở
  (lỗi/đang nối lại). EventSource bị đóng hẳn (401/5xx, `readyState=CLOSED`) → tự nối lại giãn 2 s→60 s; nối lại thành công → tải lại một lần
  (server không phát lại event lỡ trong lúc rớt).
- **Không ghi đè khi người đang thao tác**: hộp xác nhận (`dialog[open]`) đang mở, focus ở ô nhập trong khung, hoặc ô chữ đã sửa
  (`value !== defaultValue`, ví dụ đang gõ câu trả lời) → hoãn 3 s rồi thử lại. Form "Tạo công việc" nằm ngoài khung nên luôn giữ nguyên.
- Focus trên một thẻ được trả lại thẻ cùng `data-id` sau khi thay.
- `run.queued` không gắn `task_id` → bỏ; trạng thái task đổi đã có `task.status` (claim) đi kèm.

## Kiểm chứng
- `tests/test_web_tasks_realtime.py`: bộ lọc event, 401 khi chưa đăng nhập, partial = bảng (lọc team, escape), partial task có oob bình luận,
  SSE thật (daemon in-process) phát `refresh` khi `task.block`, trang task khác không bị báo.
- Chạy thật (2026-11-12): `python -m cfharness.web.demo --home <tmp>` + Chromium headless; `cfharness task block|unblock|comment` qua daemon →
  thẻ chuyển cột sau ~0,5 s, không tải lại trang, form "Tạo công việc" vẫn mở và giữ chữ đang gõ, bình luận mới hiện trên trang task, 0 lỗi console;
  mỗi thay đổi đúng 1 lần GET partial.
