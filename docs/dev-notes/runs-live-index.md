# Hub đứng mỗi `manager.check_s` — index run đang sống (migration v22)

## Triệu chứng (2026-11-05, máy người dùng 8 GB RAM, dùng 7,5 GB)
py-spy luồng chính: 10/20 mẫu ở `ManagerService._active_runs` (`manager/service.py`), câu
`SELECT id FROM runs WHERE agent_id=? AND kind='manager' AND status IN (queued,running,waiting_human)`. Mỗi đợt đứng 3–14 s; web/chat/Telegram
đứng theo (một event loop).

## Nguyên nhân
1. DB không có `sqlite_stat1` (chưa ANALYZE) → SQLite chọn `runs_agent(agent_id, queued_at)` → duyệt MỌI run từng có của điều phối viên
   (máy chủ repo: 386 dòng), dù số run đang sống thường là 0. Số dòng tăng mãi theo lịch sử.
2. `status` là cột thứ 17; phần đứng trước (`prompt`, `argv_redacted`…) trung bình ~4,5 KB > trang 4 KB → mỗi dòng phải đọc thêm trang
   tràn. Mỗi lượt kiểm tra đọc vài MB rải rác.
3. Máy thiếu RAM → page cache bị đẩy ra → đọc đĩa thật, trên luồng event loop.

Thiếu RAM chỉ làm lộ ra; chi phí mỗi lần kiểm tra tăng tuyến tính theo lịch sử run nên sớm muộn máy nào cũng gặp.

## Sửa
Migration v22: `runs_agent_status(agent_id, status)`, `runs_session_status(session_id, status)`, `runs_team_status(team_id, status)` —
phủ các câu "run đang sống của agent / session / team" (`manager/service._active_runs`, `learning/review.agent_idle`, `auto_resume`,
`runs.py`, `dispatch/rpc.py`, `teams.py`). Trên bản sao DB thật: plan → `runs_agent_status (agent_id=? AND status=?)`, 0,69 ms → 0,02 ms
khi cache nóng; quan trọng hơn là không còn chạm dòng run cũ.

Gỡ tạm cho bản cũ: `cfharness config set manager.check_s 60` (giảm tần suất, không giảm độ dài mỗi đợt đứng).

Test: `tests/test_runs_live_index.py` (kiểm plan dùng đúng index; đỏ khi bỏ migration). Câu mới dạng `<cột>=? AND status IN (…)` trên
`runs` → thêm ca vào test này (thêm index nếu cần, bằng migration mới).
