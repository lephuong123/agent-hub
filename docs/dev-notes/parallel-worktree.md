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
  - `repo` mặc định là `workspace.path`; `path` mặc định `<cha của repo>/_wt/<tên repo>-<nhánh đã làm sạch ≤40>-<sha1(nhánh)[:6]>`
    (từ luồng B — xem cuối file). Repo và thư mục worktree phải
    nằm trong `workspace.allowed_roots` (`validate_workspace`); điều phối viên còn qua `check_path` cho `worktree.repo|path`.
  - Nhánh đã có thì checkout nhánh đó (git từ chối nếu nhánh đang mở ở worktree khác); chưa có thì tạo từ `base` (mặc định HEAD).
  - Thư mục đã có: chỉ dùng lại khi đúng là worktree của cùng repo, trên đúng nhánh; khác thì báo lỗi, không đụng vào.
  - Dọn: `cfharness worktree list|clean` (luồng B — xem cuối file).
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

## Luồng B — vòng đời worktree (2026-11, `docs/dev-notes/workspace-parallel.md` mục B, lỗi B6)
Test: `tests/test_workspace_parallel_worktree.py`.

- **Metadata lưu vào task**: `workspace = {"kind": "dir", "path": <worktree>, "worktree": {...}}` với
  `{repo, branch, base, path, owned, created_at, branch_created, deps}` (`workspace.ensure_worktree_meta`; `ensure_worktree` cũ trả tuple
  vẫn giữ). `owned` = hub vừa tạo thư mục; `branch_created` = hub tạo nhánh (chỉ khi đó mới xoá nhánh lúc dọn); `base` = nhánh/commit
  gốc để xét "đã merge" (mặc định nhánh hiện tại của repo chính); `deps` = kết quả `link_deps`.
  Task con kế thừa workspace kèm metadata: `TaskService._check_worktree_meta` kiểm `path` = workspace và là worktree git của `repo`
  (không ai gắn metadata giả để `worktree clean` gỡ thư mục khác); điều phối viên còn qua `check_path` cho `workspace.worktree.repo|path`.
- **Slug**: `<repo>-<nhánh làm sạch ≤40>-<sha1(nhánh)[:6]>` — `a/b` và `a-b`, hay hai nhánh dài chung 40 ký tự đầu, không còn trùng thư mục.
- **Windows**: `core.longpaths` chưa bật → cảnh báo trong `warnings` của task (hub chỉ đọc, không đổi cấu hình người).
- **Đường B cũ** (kind `worktree` + `path` = repo, chạy `claude --worktree`): task MỚI đổi thành worktree do hub tạo trên nhánh
  `cfh/<workspace.name>` hoặc `cfh/<task id>` (id sinh trước workspace, ổn định, không đổi khi retry). Task gốc tách việc (`kind=root`) giữ
  nguyên đường B: nó không tự chạy, task con kế thừa workspace và được đổi lúc tạo (mỗi task con một nhánh `cfh/<id>` nếu không đặt `name`).
  Task cũ trong DB không đổi.
- **Phụ thuộc** (`workspace.deps` ∈ `link|install|none`, mặc định `link`): `link` — `package.json` + lockfile (package-lock/yarn/pnpm)
  giống hệt từng byte với repo chính và repo chính có `node_modules` → `node_modules` của worktree là junction (Windows,
  `_winapi.CreateJunction`, dự phòng `mklink /J`) / symlink (POSIX) sang đó; khác → `deps.needs_install=true` + cảnh báo "chạy `npm ci`".
  Liên kết không bị git bỏ qua (mẫu `node_modules/` không khớp symlink POSIX) → thêm `/node_modules` vào `info/exclude` của repo
  (`deps.excluded=true`).
- **Gỡ** (`workspace.remove_worktree(meta, dry_run, force)`): worktree bẩn → Conflict (trừ `force`); gỡ MỌI junction/symlink thư mục không
  được git theo dõi (gỡ chính liên kết, không đi theo — `git worktree remove` trên Windows coi junction là thư mục và sẽ xoá nội dung đích);
  `git worktree remove` (không `--force` trừ khi người chọn cho worktree bẩn); nhánh hub tạo đã merge vào base / có trên upstream/remote →
  `git branch -d` (`-D` khi `-d` từ chối vì base ≠ HEAD của checkout chính — đã kiểm merge trước).
- **Dọn** (`housekeeping.worktree_entries|worktree_status|worktree_list|worktree_clean`): worktree nhận qua metadata + đường B cũ
  (`<repo>/.claude/worktrees/<name|task id>`). Dọn được khi hub tạo + mọi task dùng nó `done`/`archived` + không run sống + sạch + nhánh
  đã merge. RPC `worktree.list` (đọc) / `worktree.clean {paths?, dry_run?}` (MUTATING_WORKTREE, tự audit) — cả hai HUMAN_ONLY;
  CLI `cfharness worktree list|clean [--dry-run] [--yes] [paths…]` (∈ HUB_HUMAN_ONLY_CLI). `hub doctor` chỉ báo worktree của task đã xong
  còn trên đĩa và gợi ý lệnh này.
