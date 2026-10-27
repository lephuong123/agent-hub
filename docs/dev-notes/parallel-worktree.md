# Task song song cùng repo và dòng "task chờ" trên bảng agent (2026-10-27)

Sự cố: điều phối viên báo 3 agent code "chạy song song", nhưng bảng của `cfharness chat` chỉ hiện `1 agent đang chạy`. Bảng đúng:
task B và C ở `ready`, `wait_reason = workspace_locked`, agent `web-coder-2`/`web-coder-3` idle.
Test: `tests/test_parallel_worktree.py`.

## Nguyên nhân
Điều phối viên làm theo persona cũ: `workspace` = repo chính, worktree riêng chỉ nằm trong `extra_write_roots`. Khoá repo
(`WorkspaceLocks.key_for`) tính theo `workspace.path`, nên B và C cùng khoá với task A và chờ A xong. `extra_write_roots` chỉ cho
ghi không cần duyệt, không gỡ khoá. Persona không có cách đúng: worktree phải có sẵn thì mới làm `workspace` dir được, mà điều
phối viên chạy trong scratch nên không tạo được.

## Sửa
- **`worktree` của task** (`task.create`, tool `task_create` của điều phối viên, CLI `task create --worktree-branch/--worktree-base/
  --worktree-path`): `{repo, branch, base?, path?}`. Hub chạy `git worktree add` ngay lúc tạo task (`workspace.ensure_worktree`)
  rồi đặt `workspace = {kind: dir, path: <worktree>}`. Khoá tính theo gốc worktree, nên các task cùng repo chạy song song.
  - `repo` mặc định là `workspace.path`; `path` mặc định `<cha của repo>/_wt/<tên repo>-<nhánh>`. Repo và thư mục worktree phải
    nằm trong `workspace.allowed_roots` (`validate_workspace`); điều phối viên còn qua `check_path` cho `worktree.repo|path`.
  - Nhánh đã có thì checkout nhánh đó (git từ chối nếu nhánh đang mở ở worktree khác); chưa có thì tạo từ `base` (mặc định HEAD).
  - Thư mục đã có: chỉ dùng lại khi đúng là worktree của cùng repo, trên đúng nhánh; khác thì báo lỗi, không đụng vào.
  - Hub không tự xoá worktree. Cảnh báo trả về có sẵn lệnh `git worktree remove` để chạy lúc ghép nhánh.
- **Cảnh báo dùng chung khoá**: task `dir` mới có cùng khoá repo với task đang mở (`OPEN`) → `warnings` nêu id các task đó,
  nói rõ task sẽ chờ (`workspace_locked`) và chỉ cách dùng `worktree`. Điều phối viên nhận `warnings` trong kết quả `task_create`.
- **Persona** (`templates/manager.yaml`): làm song song thì dùng `worktree`, không dùng repo + `extra_write_roots`; kiểm bằng
  `task_get`/`task_list` trước khi báo người là đang chạy song song. Agent điều phối viên đã tạo từ trước giữ SOUL cũ, cần cập nhật
  SOUL của nó (`agent_update` hoặc sửa tay) thì mới có luật này.
- **Bảng agent** (`tui.snapshot` thêm `waiting_tasks`: task `ready` có `wait_reason`, tối đa 100):
  - `AgentMonitor`: agent idle có task chờ → trạng thái `waiting_task`, dòng hiện `chờ (<lý do>): <tiêu đề task>`. Không tính vào
    "agent đang chạy".
  - Dòng gọn (REPL `summary_line`, TUI `panel_head`): thêm `N task chờ: <lý do>` (lý do chỉ hiện khi mọi task chờ cùng một lý do,
    chữ lấy từ `web.present.WAIT_REASON`).
