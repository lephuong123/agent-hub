# Workspace song song: khoá, worktree, tích hợp

Mục tiêu: agent làm song song trên cùng repo khi không ghi đè nhau; khi cần cô lập thì hub tự tách worktree rồi ghép lại.
Bối cảnh đo thật (25/10–22/11/2026, một hub): ~10% lượt khởi chạy task phải chờ khoá repo, ~38 giờ chờ; nóng nhất
commonweblib, workflowswebapp, các worktree `_wt` dùng chung, web-project-docs. 247/1.106 task chạy trong `_wt` (worktree do
agent/hub tạo, lưu kind `dir`), 0 task dùng kind `worktree`.

## Lỗi đã xác nhận (v0.2.29)
| # | Lỗi | Chỗ |
|---|---|---|
| B1 | kind `worktree` (cwd = repo, `claude --worktree`): dispatcher coi không khoá (`key_for(path,"worktree")` → None) nhưng `RunManager._lock_key` khoá GỐC repo chính → task "running" mà run nằm hàng đợi | `dispatch/dispatcher.py` `_lock_block`, `runs.py` `_lock_key` |
| B2 | kind `worktree`: không checkpoint/diff/revert; `bypassPermissions` được phép dù cwd là checkout chính | `runs.py` (checkpoint chỉ `dir`), `runtime/claude_cli.py` |
| B3 | Khoá không được nhả khi recovery không resume run (auto_resume tắt, hết trần, resume_conflicts, agent pause, ESTOP, freeze, task blocked); không có lệnh gỡ khoá; không có event khoá | `recovery.py`, `guard/locks.py`, `dispatch/rpc.py` |
| B4 | `extra_write_roots` ghi vào repo khác mà không lấy khoá repo đó | `runs.py` `_lock_key` |
| B5 | Thư mục không phải git: cha và con có khoá khác nhau → hai run ghi cùng file | `workspace.py` `WorkspaceLocks.key_for` |
| B6 | Không ai dọn worktree (`housekeeping` chỉ nhận kind `worktree`, chỉ báo); slug nhánh trùng (`a/b` = `a-b`, cắt 80 ký tự); không kiểm `core.longpaths`; metadata worktree bị bỏ khi tạo task | `workspace.py` `ensure_worktree`, `tasks.py`, `housekeeping.py` |
| B7 | diff/revert giả định một người ghi mỗi repo (gán mọi thay đổi trong run cho run) | `workspace.py` `compute_diff`/`revert` |
| B8 | Rule SOUL của agent mâu thuẫn template manager (nhiều agent "never create worktree"); SOUL là bản sao theo máy nên phải đưa rule chung vào CODE (prompt hub chèn) | `prompt.py`, `templates/*.yaml` |

## Thiết kế theo luồng (mỗi luồng một nhánh, ghép vào `feat/workspace-parallel`)

### A — Khoá đúng & gỡ được (`ws/locks`)
- Một hàm duy nhất tính khoá cho task/run, dùng ở CẢ `dispatcher._lock_block`, `RunManager._lock_key`, `RunManager.lock_pending`,
  `TaskService._same_lock_tasks` (B1). Kind `worktree` (đường cũ): khoá = thư mục worktree `<repo>/.claude/worktrees/<tên>`
  (realpath, có thể chưa tồn tại), KHÔNG khoá repo chính.
- B2: checkpoint/diff cho kind `worktree` trên thư mục worktree khi đã tồn tại; bỏ `bypassPermissions` khi kind `worktree` mà cwd là checkout chính.
- B3: `recovery.reconcile` + đường không resume → nhả khoá của session không còn run sống. `RepoLocks` có quét người giữ chết
  (`is_active(session_id)` = có run `queued|running|waiting_human` của session) ở `try_acquire`/`blocked_for`.
  `guard.lock_waiting_ttl_s` (mặc định 14400): run `waiting_human` quá TTL → khoá nhả (resume sau đó xin lại, kiểm `resume_conflicts`).
  RPC `workspace.unlock {path|key, force}` (HUMAN_ONLY, MUTATING, audit) + CLI `cfharness workspace unlock <path>`.
  Event `workspace.lock_acquired|lock_released|lock_blocked` (thêm `EVENT_LEVELS`), payload {key, session_id, run_id, reason}.
- B4: run giữ NHIỀU khoá: khoá cwd + khoá mỗi `extra_write_roots` (theo cùng hàm). Giao diện `RepoLocks` giữ nguyên; thêm
  `try_acquire_many(keys, session_id, run_id)` nguyên tử (tất cả hoặc không). `ActiveRun.lock_key` giữ khoá chính, thêm `lock_keys`.
- B5: khoá thư mục không phải git xung đột với khoá là cha/con của nó (so tiền tố đường dẫn đã chuẩn hoá, không phân biệt hoa thường trên Windows).

### B — Vòng đời worktree (`ws/worktree`)
- `ensure_worktree(...)` trả thêm metadata: `{"repo","branch","base","path","owned","created_at"}` (giữ hàm cũ trả tuple cho tương thích
  hoặc thêm `ensure_worktree_meta`). Slug: `<repo>-<nhánh đã làm sạch, ≤40>-<sha1(nhánh)[:6]>` → không trùng. Cảnh báo khi
  Windows + `core.longpaths` chưa bật (không tự đổi cấu hình người).
- `tasks.create`: lưu `workspace = {"kind":"dir","path":<wt>,"worktree":<metadata>}`. Kind `worktree` + `path=repo` (đường B cũ)
  → chuyển thành đường A: `ensure_worktree(repo, branch="cfh/<name hoặc task id>")` (task mới; task cũ vẫn chạy như A định nghĩa).
- Phụ thuộc: `link_deps(wt, repo, mode)`; `workspace.deps` ∈ `link|install|none` (mặc định `link`): `package.json` + lockfile
  giống hệt repo chính → junction (Windows `mklink /J`, POSIX symlink) `node_modules`; khác → ghi `deps.needs_install=true` vào metadata
  (prompt sẽ nói agent chạy `npm ci`). Ghi `deps` vào metadata.
- `remove_worktree(meta)` an toàn junction: gỡ junction/symlink `node_modules` TRƯỚC (không đi theo link), rồi `git worktree remove`
  (không `--force` khi còn link), rồi `git branch -d` nếu đã merge. `housekeeping`: nhận worktree qua metadata; `cleanable` khi task
  xong + worktree sạch + nhánh đã merge vào base (hoặc đã có trên remote). CLI `cfharness worktree list|clean [--dry-run]`.

### C — Rule chung trong prompt + template + plan (`ws/prompt`)
- `prompt.py`: khối "Workspace rules" (tiếng Anh) hub chèn cho MỌI run có workspace ghi, theo ngữ cảnh: đang ở worktree do hub cấp
  (nhánh, base, cần `npm ci` hay không) hay checkout chính (khoá repo; không `git worktree add` tự phát trên checkout chính — nhờ lead
  tạo task kèm `worktree`; không `git add -A`/`commit -a` gom việc người khác; không checkout/switch nhánh của checkout chính).
  Bật/tắt `prompt.workspace_rules` (mặc định true). Khối này đứng SAU quy ước hub, TRƯỚC SOUL, và được tính vào hash prompt.
- `templates/*.yaml` (manager/coder/reviewer): thống nhất với khối trên.
- `dispatch/plan.py` `PLAN_SCHEMA`: mỗi subtask thêm `isolate: bool` (tuỳ chọn) — lead yêu cầu worktree riêng cho subtask.
  Luồng D dùng trường này.

### D — Tự cô lập & tích hợp (`ws/integrate`, sau A+B+C)
- `workspace.isolate` ∈ `never|auto|always` (mặc định `auto`; ghi đè theo team `TeamPolicies` và theo task). `auto`: task ghi trên repo git
  mà repo đang có task mở khác cùng khoá, hoặc subtask có `isolate=true` → hub tạo worktree.
- Task gốc có nhánh tích hợp `cfh/<root8>` trong worktree tích hợp; subtask tách nhánh `cfh/<root8>/<task8>` từ nhánh tích hợp.
- Subtask `done` → bước integrate (dưới khoá worktree tích hợp): auto-commit thay đổi chưa commit của subtask, `merge --no-ff`
  (`rerere`). Xung đột tầm thường (chỉ lockfile/khoảng trắng) → tự xử; còn lại → `merge --abort`, tạo task `merge` cho assignee
  của subtask (workspace = worktree tích hợp, danh sách file xung đột); thất bại → approval người.
- Task gốc xong → để nhánh tích hợp + báo (không push/không đụng nhánh người trừ fast-forward khi checkout chính sạch và đang ở base
  và `workspace.integrate.fast_forward=true`, mặc định false) → dọn worktree con qua `remove_worktree`.

#### D — đã làm (`src/cfharness/dispatch/integrate.py`, test `tests/test_workspace_parallel_integrate.py`)
- **Quyết định lúc DISPATCH** (`TaskDispatcher._dispatch_ready` → `Integrator.maybe_isolate`), không lúc tạo task: phản ánh khoá
  thật ngay trước khi chạy; worktree tạo đồng bộ (git) trước `runs.submit`, prompt dựng SAU khi đổi workspace nên khối "Workspace
  rules" thấy nhánh. Chỉ task con (`origin_task_id` → task `kind=root`) — task đứng riêng không có chỗ ghép nên giữ hành vi cũ.
- **Chế độ**: `workspace.isolate` (never|auto|always, mặc định auto) ← `TeamPolicies.isolate` ghi đè. Task: `workspace.isolate=true`
  (kế hoạch lead / hub-self `task_create isolate`) cô lập khi chế độ ≠ never; `false` = không cô lập task đó. `never` của người/team
  là cứng (agent không vượt được). `auto` cô lập khi: task yêu cầu; `_lock_block` = workspace_locked; `_same_lock_tasks` thấy task mở
  KHÁC cùng khoá (trừ task gốc + hậu duệ của chính nó — chúng chạy sau); hoặc task gốc đã có nhánh tích hợp trên repo đó (giữ mọi
  phần việc về một chỗ). Không cô lập: task đã từng chạy (resume phải cùng chỗ), agent chỉ đọc, không phải git, đã có worktree,
  có tổ tiên đã chạy thẳng trên checkout chính của repo đó (để thấy thay đổi chưa commit của nó). Lỗi git khi tách → comment +
  chạy như cũ. Task con có tổ tiên cô lập chưa ghép xong → `wait_reason=integration_pending`.
- **Nhánh**: tích hợp `cfh/<root id>/integration` (worktree `_wt/<slug>`, tạo lười ở lần cô lập đầu từ nhánh hiện tại của repo = base),
  task con `cfh/<root id>/<task id>` tách từ nhánh tích hợp (`worktree.base` = nhánh tích hợp → `remove_worktree` xoá nhánh con khi
  đã ghép). Vướng ref: `workspace.ref_collision` kiểm tường minh (git không giữ cùng lúc `cfh/x` và `cfh/x/…`); `ensure_worktree_meta`
  báo lỗi rõ cho mọi đường tạo (kể cả đường B cũ `cfh/<task id>`), luồng D chọn tiền tố khác: `cfh/<root>-int`, `cfharness-int/<root>`.
- **Ghép** (`_after_done` → `Integrator.on_done`, TRƯỚC `_promote`/`_refresh_root`): giữ khoá cả worktree tích hợp lẫn worktree con
  (session `integrate:<root>`, nhả ngay trong cùng lượt; bận → `pending`, tick thử lại). Auto-commit thay đổi chưa commit của task con
  (`cfh: task <id> — <title>`), worktree tích hợp phải sạch, `merge --no-ff --no-verify` với `-c rerere.enabled=true -c
  rerere.autoUpdate=true` (hook đã tắt qua `GIT_SAFE_ARGS`; thiếu user.name/email → danh tính `cfharness`). Xung đột chỉ ở lockfile
  (`MERGE_LOCKFILES`: npm/yarn/pnpm/Cargo/poetry/uv/…) → lấy bản nhánh tích hợp + `needs_lockfile_refresh`; còn lại → abort, thử lại
  `-Xignore-space-change` (`how=whitespace`); vẫn xung đột → abort + task `Merge: <title>` (assignee của task con, team của task gốc,
  workspace = worktree tích hợp, body tiếng Anh liệt kê file + hai nhánh + các bước; `workspace.merge_for`). Trong lúc có task merge mở,
  task con khác xong được xếp `pending` (không đụng worktree tích hợp). Task merge xong → hub kiểm nhánh con đã nằm trong nhánh tích hợp
  (merge đã giải hết mà chưa commit → hub commit); không → task gốc `blocked` kèm việc người phải làm; task merge bị blocked → task gốc
  blocked với lý do riêng. Người ghép tay + `task unblock` → tick (`Integrator.reconcile`) kiểm lại (`how=manual`).
- **Task gốc xong** (`_refresh_root`: `Integrator.gate` — mọi task con cô lập phải đã ghép; rồi `finalize`): gỡ worktree con đã ghép qua
  `remove_worktree(locks=…)`; nhánh tích hợp + worktree của nó để nguyên (dọn bằng `cfharness worktree clean` khi đã merge vào base —
  `housekeeping.worktree_entries` đọc `workspace.integration.worktree`); fast-forward base ở checkout chính CHỈ khi
  `workspace.integrate.fast_forward=true` + checkout chính sạch + đang ở base (`merge --ff-only`). Kết quả: `result_json.integration
  {branch, path, base, merged, needs_lockfile_refresh, fast_forward, cleaned}` + dòng "Cần bạn"/"Còn mở" trong `result_summary`
  (`task_report.compose_root(open_extra, need_extra)`).
- **Trạng thái** ở task gốc `workspace.integration = {repo, branch, prefix, base, base_commit, path, worktree, children, merged[{task,
  branch, commit, how, merge_task?, lockfiles?}], conflicts[{task, branch, files, merge_task, status open|failed|resolved, reason}],
  pending, needs_lockfile_refresh, finalized, cleaned, fast_forward}`; task con `workspace.isolation = {root, reason
  requested|conflict|integration|always, origin_path, at}`; task merge `workspace.merge_for`. Ba khoá này chỉ hub ghi: `tasks.create`
  bỏ chúng khỏi workspace nhận từ ngoài (`integrate.strip_internal`), task con không kế thừa `integration` của task gốc.
- **Event** (EVENT_LEVELS + `docs/schema/event.v1.json`): `task.isolated {root_id, branch, path, base, reason}`, `task.integrated
  {root_id, branch, into, commit, how clean|lockfile|whitespace|whitespace+lockfile|merge_task|manual, needs_lockfile_refresh,
  merge_task_id?}`, `task.integration_conflict {root_id, branch, into, files, merge_task_id}`.
- **Sửa khi gộp A+B+C**: test A (B1/B2) dựng task kind `worktree` cũ thẳng trong DB (B đổi kind này lúc tạo); test C dùng worktree thật;
  `remove_worktree(locks=)` + `worktree.list|clean` không gỡ worktree còn người giữ khoá SỐNG (khoá chết được sweep trước); hub-self
  `task_create` của lead có `isolate` + `worktree {branch, base?, path?}` (chỉ repo của task gốc); khối Workspace rules nhận cwd là thư
  mục con của worktree (task cô lập giữ thư mục con tương đối của workspace gốc).
- **Giới hạn / chưa làm**: một repo mỗi task gốc; task con của task gốc đường B cũ (kind `worktree`, B đổi thành `cfh/<task id>` lúc tạo)
  và `worktree` do lead/người chỉ định KHÔNG được ghép tự động; task con chạy trên checkout chính trước khi có nhánh tích hợp thì việc của nó
  nằm ở checkout chính (không vào nhánh tích hợp); git chạy đồng bộ trong tick (như `tasks.create` của B); không sinh lại lockfile;
  không push. Chưa có thay đổi `CHANGELOG.md` (người tích hợp ghi).

### E — Khoá theo file (`ws/leases`, sau A; tuỳ chọn bật)
- `guard.repo_lock` ∈ `repo|files|off` (`true` = `repo`, giữ hành vi cũ). `files`: run giữ khoá repo kiểu CHIA SẺ; Edit/Write/MultiEdit/
  NotebookEdit lấy lease theo file (repo key + đường dẫn tương đối chuẩn hoá) trong `GuardService.check`, kèm sha256; khác session giữ → deny
  ngay kèm tên người giữ; file bị sửa ngoài lease → deny "đọc lại file".
- Bash đổi trạng thái chung (git checkout/switch/reset/rebase/merge/pull/stash, `add -A`/`add .`/`commit -a`, npm/yarn/pnpm/pip install,
  ghi lockfile) cần khoá repo ĐỘC QUYỀN; không lấy được → deny + gợi ý worktree.
- B7: diff/revert của run giới hạn theo các file run đã lease; không revert file đang bị session khác lease.

**Đã làm (nhánh `ws/leases`):**
- Cấu hình: `guard.repo_lock` ∈ `repo|files|off` (+ `true`/`false` cũ), đọc DUY NHẤT qua `guard/locks.repo_lock_mode` (RunManager,
  WorkspaceLocks, guard, `learning/runtime` cho prompt; dispatcher không đọc trực tiếp nữa vì `lock_keys` trả rỗng khi `off`). Kiểm ở
  `config.validate_setting`. Mặc định vẫn `repo` → hành vi cũ y nguyên. `guard.lease_exclusive_ttl_s` (600) cho dấu độc quyền của Bash.
- Khoá repo chia sẻ: `RepoLocks` thêm map `hub_state.repo_locks_shared` ({khoá: {session: người giữ}}) + tham số `mode` (`shared|exclusive`,
  None = `default_mode()`) ở `blockers`/`blocked_for`/`try_acquire*`. `WorkspaceLocks.default_mode()` = `shared` khi `files` → `tick`/
  `_run`/dispatcher `_lock_block` không cần sửa: chỉ người giữ ĐỘC QUYỀN chặn. `lock_pending` trả False ở chế độ `files`. `list()` (RPC
  `workspace.locks`) giữ nguyên = khoá độc quyền; thêm `list_shared()`/`shared_holders()`. `release_*`/`force_release`/`sweep` xử lý cả hai
  map; `unlock`/`_lost_locks` hiểu khoá chia sẻ. `repo_locks_shared` ∈ `hub_transfer.MACHINE_STATE`.
- Lease: bảng `file_leases` (migration **v32**; khoá chính (repo chuẩn hoá, đường dẫn tương đối chuẩn hoá — chữ thường trên Windows) → một
  người giữ, `INSERT OR IGNORE` nguyên tử trên writer duy nhất của daemon), cột sha256 + `pending`. Lớp `guard/leases.FileLeases`
  (`WorkspaceLocks.leases`). Lease nhả trong `WorkspaceLocks._on_session_released` — móc ở MỌI đường nhả khoá của A (hết run, stop,
  `_release_unstarted`, approval/recovery `release_session`, `force_release`, quét session chết); lease của session chết còn bị dọn lười khi
  có người khác xin.
- Kiểm trong `GuardService.check` (`lease_check`, sau ESTOP, TRƯỚC lối tắt trust — trust full vẫn chịu), chỉ khi `files`, deny NGAY (không
  approval), ghi `guard_decisions` (rule `file_lease_held|file_changed_externally|repo_exclusive_busy|repo_exclusive_needed`) + event
  `workspace.lease_denied` (summary). Lỗi → ném ra → hook deny (fail-closed).
  - Edit/Write/MultiEdit/NotebookEdit: file nằm trong repo đang được giữ chia sẻ → lease; session khác còn sống giữ → deny kèm agent/run +
    lời khuyên (file khác / nhờ lead tách worktree). Hash hiện tại ≠ hash đã ghi nhận và `pending=0` → deny "đọc lại file" (ghi nhận hash mới
    → lần sau qua). `pending=1` (đã cho ghi, PostToolUse chưa về) → khác hash coi là của chính session: hook quan sát fail-open, mất
    PostToolUse chỉ làm yếu phát hiện chứ không chặn nhầm.
  - PostToolUse: `observe_hook` gửi thêm `file_path` + `cwd` (chỉ đường dẫn) cho tool ghi file; `hook.observe` → `leases.observe` làm mới hash,
    bỏ `pending`; Bash → nhả dấu độc quyền của session.
  - Bash (`leases.repo_ops`, thuần, dùng lexer `guard/shell.py`, theo `cd`/`git -C`/wrapper/`sh -c`/`$(..)`): ĐỘC QUYỀN = git checkout
    (không `--`)/switch/reset (không `-- paths`)/rebase/merge/pull/stash (trừ list/show)/cherry-pick/revert/am/clean, `git add -A|.|-u|--all|<glob>`,
    `git commit -a|--all|--amend`, `git checkout .`/`restore .`, npm/yarn/pnpm/bun install|ci|add|remove|update… (không `-g`), `npm audit fix`,
    pip/`python -m pip`/`uv pip` install|uninstall, uv add|remove|sync|lock, poetry add|remove|install|lock|update, ghi lockfile (redirect,
    tee/cp/mv/sed…). Có session khác còn sống giữ khoá chia sẻ/lease/dấu độc quyền → deny + gợi ý commit theo đường dẫn hoặc worktree;
    không ai → đặt dấu độc quyền (hàng `path=''`, hết hạn TTL) — Edit của session khác bị "repo bận" tới PostToolUse của Bash/hết run.
    THEO ĐƯỜNG DẪN (`git add/commit/restore/rm/mv <paths>`, `checkout|reset -- <paths>`) → chỉ deny khi đường dẫn (hoặc file dưới thư mục) đang
    bị session KHÁC lease. `git commit` không đường dẫn (commit cả index chung) → deny khi index có file session khác lease (`git diff --cached`).
    Ca test ở `tests/test_guard_classifier.py` (`LEASE_EXCLUSIVE`/`LEASE_SAFE`/đường dẫn/index).
- B7: `compute_diff(..., paths=)` / `revert(..., paths=, leased_by_others=)`. Chế độ `files` + checkpoint git → `RunManager._lease_scope` =
  file session của run đã lease trong repo (đọc TRƯỚC khi nhả khoá trong `finally`) → diff có `scope: "leases"`, `scoped_paths`. `run.revert`
  với diff `scope=leases`: không chặn cả repo vì người giữ chia sẻ; file đang bị session khác lease → `Conflict` liệt kê. Chế độ repo: như cũ.
- Hiển thị: RPC `workspace.leases` (chỉ đọc: `{mode, shared, leases}`, quét người giữ chết trước) + `cfharness workspace locks` in chế độ, khoá
  chia sẻ và lease. Event `workspace.lease_denied` (summary), `lease_acquired|lease_released` (debug) — `EVENT_LEVELS` + `docs/schema/event.v1.json`.
- Prompt: chế độ `files` thêm một dòng vào khối "Workspace rules" (file được chia cho từng agent; bị từ chối thì làm file khác / nhờ lead
  tách worktree; commit `-- <your files>`); câu "holds the repo lock" chỉ ở chế độ `repo`.
- Test: `tests/test_workspace_parallel_leases.py`.

**Giới hạn đã biết:** điều phối giữa agent hợp tác, KHÔNG phải ranh giới an ninh — ghi file bằng Bash/script (`sed -i`, codegen, formatter)
không lấy lease, không vào diff/revert của run (B7 chỉ tính file đã lease) và có thể làm session đang giữ lease nhận "đọc lại file"; lệnh
độc quyền chạy qua script/alias không nhận ra. Dấu độc quyền giữ tới PostToolUse/TTL kể cả khi chính lệnh đó bị guard giữ chờ duyệt.
`tasks._same_lock_tasks` (cảnh báo "task CHỜ") vẫn dùng `conflicts` thuần → ở chế độ `files` cảnh báo sai (task thực ra chạy song song) —
thuộc vùng luồng D. `commits_made` của diff phản ánh mọi commit trên checkout (có thể của session khác).

## Kiểm thử
`uv run pytest -q` (không gọi claude thật). Mỗi luồng thêm test riêng (`tests/test_workspace_parallel_*.py`). Thêm dòng vào
"Chưa phát hành" trong `CHANGELOG.md` khi có thay đổi người dùng thấy.
