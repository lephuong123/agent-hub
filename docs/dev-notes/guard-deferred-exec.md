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

## Vòng review PR #37 (t_6ec1fa1d83)
- **PowerShell**: `WIN_PERSIST_CODE` thêm `Register-ScheduledJob`/`*-JobTrigger`/`Set|Enable-ScheduledJob`; khớp theo ĐƯỜNG DẪN
  registry (`CurrentVersion\Run|RunOnce|RunServices*|Winlogon|Policies\Explorer\Run`, `User Shell Folders`/`Shell Folders`) thay vì
  theo cmdlet → `New-Item`, `sp`/`si`, `[Microsoft.Win32.Registry]::SetValue` đều bị giữ (đổi lại: đọc khoá Run bằng PowerShell cũng
  bị giữ — hiếm, chấp nhận). `GetFolderPath(7|24)` (Startup/CommonStartup dạng số). `PS_DYNAMIC_INVOKE` thêm gọi bằng dấu chấm
  `. (…)`/`. $x`.
- **Windows trailing dot/space**: `canon_path` (Windows) bỏ `.`/khoảng trắng cuối mỗi thành phần như Win32 (`Startup./x.bat`,
  `Startup /x.bat` = `Startup/x.bat`); giữ `.`/`..`, thành phần toàn dấu chấm (`...`) giữ nguyên. Áp cho mọi so khớp (policy,
  persistence, exec_zone).
- **`cmd /c`**: `cmd_line` bỏ `^` thoát ngoài nháy (`sch^tasks`), mở biến vừa `set X=…` trong cùng dòng cho `%X%`/`%%X%%`/`!X!`, bỏ
  `call` ở đầu lệnh con.
- **Lệnh trong mã**: `SCHED_CMD_IN_LIT` cho phép tiền tố wrapper (`env|sudo|nohup|command|exec|nice|setsid|doas` + cờ), `VAR=…`,
  đường dẫn (`/usr/bin/crontab`).
- **`defaults -currentHost|-host h write …`**: động từ tính sau cờ host (trước đây coi `-currentHost` là động từ → `safe`).
- **D-Bus**: `busctl|gdbus|dbus-send|qdbus` nhắm `org.freedesktop.systemd1|login1` (trừ `list|status|tree|introspect|get-property|
  monitor`) → `schedule_persistence`.
- **Vùng persistence**: profile PowerShell (`Documents/WindowsPowerShell`, `Documents/PowerShell`, bản OneDrive, `~/.config/powershell`),
  `~/.config/environment.d`, `.pam_environment`, `.xprofile|.xinitrc|.xsession|.xsessionrc`, `~/.ipython`; file `*.pth` trong
  `site-packages` dưới home người dùng.
- `systemctl unmask|revert`, `regedit /s|*.reg`, `sfltool` (trừ `dumpbtm`) → `schedule_persistence`.
- Bỏ báo nhầm: `shell:startup` chỉ bị giữ khi được MỞ (`explorer|start|rundll32`, kể cả qua `cmd /c start`); `echo`/`grep` thì không
  (trước đây `windows_cmd` chạy cho MỌI lệnh).

## Test
`tests/test_sec_deferred_exec.py`: 26 ca lách mới (đỏ trên code cũ), 59 ca hồi quy (quoting, biến, `sh -c`, wrapper, `-enc`,
đường dẫn), 15 ca chỉ đọc không bị giữ, Write/Edit vào nơi persistence (gạch ngược + hoa thường trên Windows), thư mục hệ thống.
Vòng review: `test_review_evasions_held` (24 ca), `test_review_paths_protected_for_file_tools`, `test_windows_trailing_dot_space_normalised`,
`test_canon_path_strips_trailing_dot_space`, `test_shell_startup_opened_still_held`; `NOT_HELD` thêm `echo shell:startup`, `grep … shell:startup`,
`busctl list/status`, `defaults -currentHost read`.

## Chưa làm / giới hạn
- `git -c core.hooksPath=… <cmd>` / `-c alias.x=!…`: chạy NGAY trong run (sandbox), không hoãn → không giữ.
- `.githooks/` (hoặc thư mục `core.hooksPath` repo đã cấu hình sẵn) trong workspace: ghi vào vẫn là ghi thường; hook chạy khi
  NGƯỜI commit trong workspace đó. Cần probe `git config core.hooksPath` (git_probe) để biết thư mục — để sau.
- `atrm` (xoá job at) không giữ. Biến `%X%` lạ trong `cmd /c` (không `set` trong cùng dòng, không có trong env) → coi như literal;
  `set` qua nhiều lệnh `cmd` riêng hoặc `setx` (env bền) không theo dõi.
- `.pth` chỉ được bảo vệ dưới home người dùng (site-packages hệ thống/venv của hub đã có `hub-python-env`/exec_zone); `sitecustomize.py`/
  `usercustomize.py` ngoài site-packages của hub chưa bảo vệ riêng.
- `SCHED_CMD_IN_LIT`: wrapper có cờ mang giá trị (`sudo -u x crontab`) trong chuỗi mã không khớp — lớp `EXEC_DYNAMIC`/sandbox còn lại.
- Recall vẫn là heuristic (FR-SG-07): sandbox OS (denyWrite + chặn socket) là lớp chặn chính khi classifier sót.
