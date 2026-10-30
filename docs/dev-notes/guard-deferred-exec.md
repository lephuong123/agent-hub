# Guard: lập lịch hoãn / chạy bền — đóng nốt V2-N2 (2026-10-30)

V2-N2 (`docs/acceptance/traceability-v2.md` §4): agent cài thứ CHẠY VỀ SAU như người, ngoài run/sandbox (cron, at, Task
Scheduler, git hooks/alias, LaunchAgents, systemd user, Startup/Run) → tiến trình đó đọc được token người → tự duyệt. Lớp phân
loại đã có từ hardening SEC (`schedule_persistence`, `git_config_exec`, `protected_path` "persistence"/"git-hooks", xem
`hardening-SEC.md`); lần này probe lách có hệ thống và bịt các lỗ còn ALLOW. Tất cả = `destructive` (giữ chờ người, như các lớp
leo thang khác) hoặc `uncertain` khi không biết lệnh thật; không có ca nào `forbidden` (người vẫn có thể duyệt việc hợp lệ).

## Lỗ đã bịt (`guard/classifier.py`)
- **Lệnh trong mã trình thông dịch**: chuỗi cho `os.system`/`execSync` chỉ được phân tích khi BẮT ĐẦU bằng lệnh trong danh sách
  → `os.system('crontab f')`, `execSync('at now')`, `'echo x | at now'` lọt. `SCHED_CMD_IN_LIT` khớp lệnh lập lịch ở đầu chuỗi
  hoặc sau `|`/`;`/`&`/`(` (`at`/`batch` chỉ khi theo sau là dạng thời gian/cờ — tránh "at least…" trong chuỗi thường).
- **systemd**: `systemd-run` (timer/unit tạm) → `schedule_persistence`; `loginctl enable-linger`; `systemctl` mọi phạm vi (không
  chỉ `--user`) với `SYSTEMCTL_PERSIST_VERBS` (thêm `reenable|restart|preset|add-wants|set-environment|…`). `status`,
  `list-timers`, `daemon-reload` vẫn qua.
- **macOS**: `defaults`/`plutil` nằm trong `READ_ONLY_CMDS` → `defaults write ~/Library/LaunchAgents/x …`, `plutil -create
  …/LaunchAgents/x.plist` là `safe`. Nay `defaults write|import|delete|rename` và `plutil -create|-insert|-replace|-remove|-convert|-o`
  kiểm đối số đường dẫn như lệnh chung (`protected_touch`); `defaults write com.apple.loginwindow LoginHook|LogoutHook` /
  loginitems → `schedule_persistence`. `osascript … make login item` → `schedule_persistence`.
- **Windows**: `WIN_PERSIST_CODE` khớp khoá Run viết bằng `/` (`HKCU:/…/Run`), thư mục Startup (`Start Menu\Programs\Startup`,
  `GetFolderPath('Startup')`, `shell:startup`). PowerShell gọi động (`& ('Reg'+'ister…')`, `iex`, `[scriptblock]::Create`) →
  `uncertain/code_obfuscated` (`PS_DYNAMIC_INVOKE`, chỉ khi không khớp cmdlet lập lịch). `& git status` / `& "C:\x.exe"` không bị
  giữ; `& $exe` thì bị (không biết tên).
- **`cmd /c`**: dòng lệnh ghép lại bằng `literal` (mất nháy → "Start Menu" tách 2 từ) và `%APPDATA%` không mở → `copy x
  "%APPDATA%\…\Startup"` = ghi thường. `BashAnalyzer.cmd_line`: một đối số có khoảng trắng = cả dòng lệnh, đối số có khoảng trắng
  bọc nháy như MSYS, `%APPDATA%|%LOCALAPPDATA%|%USERPROFILE%` mở theo `policy.user_home`, `%PROGRAMDATA%` → `C:/ProgramData`,
  biến khác lấy từ env, `\` → `/`.
- **Vùng persistence** (`_persistence_dirs`, Write/Edit + đối số lệnh): thêm `~/.local/share/systemd`, Startup của user
  (`AppData/Roaming/…/Startup`) và `C:/ProgramData/…/StartUp`, `~/Library/Application Support/com.apple.backgroundtaskmanagementagent`,
  `/Library/StartupItems`, `/etc/crontab|anacrontab|cron.*`, `/var/spool/at`, `/var/at`, `/usr/lib/cron` (thay `…/tabs`),
  `/etc/systemd`, `/etc/xdg/autostart`, `/etc/init.d`, `/etc/rc.local`. Trước đây Startup chỉ được bảo vệ qua `executable_zones`
  của home THẬT (không theo `policy.user_home`); thư mục hệ thống chỉ bị giữ nhờ "ngoài workspace" (mất khi người cấp add-dir).
- Sandbox (`guard/runtime.py`): denyWrite thêm `~/.local/share/systemd`.

## Test
`tests/test_sec_deferred_exec.py`: 26 ca lách mới (đỏ trên code cũ), 59 ca hồi quy (quoting, biến, `sh -c`, wrapper, `-enc`,
đường dẫn), 15 ca chỉ đọc không bị giữ, Write/Edit vào nơi persistence (gạch ngược + hoa thường trên Windows), thư mục hệ thống.

## Chưa làm / giới hạn
- `git -c core.hooksPath=… <cmd>` / `-c alias.x=!…`: chạy NGAY trong run (sandbox), không hoãn → không giữ.
- `.githooks/` (hoặc thư mục `core.hooksPath` repo đã cấu hình sẵn) trong workspace: ghi vào vẫn là ghi thường; hook chạy khi
  NGƯỜI commit trong workspace đó. Cần probe `git config core.hooksPath` (git_probe) để biết thư mục — để sau.
- `atrm` (xoá job at) không giữ. Biến `%X%` lạ trong `cmd /c` không mở được → coi như literal.
- Recall vẫn là heuristic (FR-SG-07): sandbox OS (denyWrite + chặn socket) là lớp chặn chính khi classifier sót.
