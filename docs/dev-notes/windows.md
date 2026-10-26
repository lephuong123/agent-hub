# Windows — mô hình an toàn khác macOS/Linux ở đâu

cfharness chạy native trên Windows (Python 3.13, `claude` 2.1.303; contract test thật đến 2.1.310). Trên macOS/Linux mọi hành vi giữ nguyên; các khác biệt dưới đây
chỉ áp khi `sys.platform == "win32"` (gom ở `oscompat.py`, `transport.py`, nhánh `IS_WINDOWS` trong `guard/runtime.py`).
Đọc cùng `hardening-SEC.md`: ba lớp "không phụ thuộc cách agent viết lệnh" ở đó, trên Windows chỉ còn HAI (mất sandbox OS).

## 1. Spike thật (claude 2.1.303, haiku, ~$0.005)
`claude -p --settings <file> --setting-sources "" --strict-mcp-config`, hook `PreToolUse` dựng giống `hook_command`, luật deny
`Read(//c/Users/…/secret.txt)`, khoá `sandbox` như trên macOS:

| Câu hỏi | Kết quả |
|---|---|
| Hook chạy bằng shell nào | **Git Bash** (`MSYSTEM=MINGW64`, `SHELL=C:\Program Files\Git\bin\bash.exe`) — `'C:/…/python.exe' … \|\| exit 2` chạy đúng; `\` trong đường dẫn là ký tự thoát của bash nên `hook_command` đổi sang `/` |
| Dạng đường dẫn trong luật quyền | `C:\Users\x` ↔ `//c/Users/x` (ổ chữ thường); Read `C:\…\secret.txt` bị chặn (`permission_denials`). `rule_path()` đổi; POSIX giữ nguyên byte-by-byte |
| Tên ngắn 8.3 (`LEQUYP~1`) | CLI báo `file_path` dạng tên dài → luật phải dùng `realpath` (đã thêm biến thể realpath cho token người) |
| `sandbox.enabled` + `failIfUnavailable: true` | CLI **từ chối chạy**: `sandbox required but unavailable: … the Windows sandbox is not active on this session (feature gate off)` |
| `sandbox.enabled` + `failIfUnavailable: false` | CLI in `⚠ Sandbox disabled … Commands will run WITHOUT sandboxing` rồi chạy bình thường |

Quyết định: Windows mặc định `failIfUnavailable: false` (nếu không mọi run bị từ chối); khoá `sandbox` vẫn được ghi (đường dẫn `/`)
để khi Claude Code bật sandbox Windows thì tự có hiệu lực. Người muốn fail-closed đặt `guard.sandbox.fail_if_unavailable_windows: true`.
`guard.runtime.sandbox_supported()` = False trên Windows — doctor/status dùng để cảnh báo (`SANDBOX_UNAVAILABLE_NOTE`).
2.1.303 đã vào `runtime.claude.tested_versions` và `flag_matrix` (`claude --help` 2.1.303 còn đủ mọi cờ của 2.1.302 → cùng danh
sách). 2.1.303 CHỈ được kiểm bằng `--help` + spike trên — KHÔNG có fixture/contract test riêng; giữ trong danh sách vì đã chạy
thật ở spike này, nhưng mức bằng chứng thấp hơn 2.1.302/2.1.310.

**2.1.310** (2026-10-25, Windows, haiku, tổng ≈ $0.0055): `claude --help` còn đủ mọi cờ của `_FLAGS_2_1_302` (không cờ nào bị
bỏ/đổi tên) → `flag_matrix["2.1.310"]` cùng danh sách. Fixture stream-json thật ở `tests/fixtures/claude-2.1.310/` (ghi theo cách
spike M0: `run_iso.sh`/`a02_steer.py`/`a03_interrupt.py`, cwd tạm trong `%TEMP%`, tên user đã che), contract test
`tests/test_claude_contract_2_1_310.py`: isolated, permission-denied (hook `ask`), budget-exceeded, steer (`steer.ack`),
ngắt bằng `control_request interrupt` (→ `aborted_tools`, như SIGINT trên POSIX), CTRL_BREAK cho process group (→ KHÔNG có
`result`, như SIGTERM), và stdout thô của một run thật qua daemon (guard + observe hook, không vi phạm cô lập). Adapter không
cần sửa. Phát hiện: cwd dạng tên ngắn 8.3 (`C:\Users\XXXXXX~1\…` — chính là `%TEMP%` mặc định) → CLI tự từ chối ghi file bằng
`permission_denied` `decision_reason_type: safetyCheck` ("suspicious Windows path pattern"), trước cả quyết định của hook
(fixture `claude-permission-denied-8dot3-win.jsonl`). Hub không dính vì `HubPaths`, `runs.submit` và `cfharness run --cwd` đều
`resolve()` ra tên dài; chỗ mới nào dựng cwd cho run cũng phải giữ quy tắc này.

## 2. Lớp nào còn, lớp nào đổi

| Lớp | macOS/Linux | Windows |
|---|---|---|
| Kênh điều khiển | Unix socket `hub.sock` 0600 | TCP **chỉ 127.0.0.1**, cổng ngẫu nhiên; `hub.sock` là file endpoint JSON `{host,port,pid}` (giữ tên để guard/bộ phân loại bảo vệ y cũ) |
| pid người gọi | `LOCAL_PEERPID` / `SO_PEERCRED` | bảng TCP của kernel (`GetExtendedTcpTable`, dòng local=peer, remote=ta) |
| Lọc user khác | quyền file socket | cổng loopback ai cũng nối được → daemon từ chối pid không xác định / khác SID user (`oscompat.same_user`) |
| "Session" của run | `start_new_session` + `getsid` | **Job Object**: spawn treo, gán job rồi mới chạy → mọi hậu duệ ở trong job (không `BREAKAWAY_OK`); dừng = `TerminateJobObject` |
| Biến môi trường của người gọi (`CFHARNESS_RUN_ID`) | `KERN_PROCARGS2` / `/proc/<pid>/environ` | đọc khối env qua PEB (`oscompat.win_environ`: `NtQueryInformationProcess` + `ReadProcessMemory`, chỉ tiến trình 64-bit cùng user; 32-bit → không có bằng chứng này) |
| Sandbox OS cho Bash | Seatbelt / bubblewrap | **không có** (feature gate của Claude Code tắt) |
| Token người `hub.sock.auth` | 0600, sandbox chặn đọc | chỉ ACL hồ sơ user + `permissions.deny` cho Read/Grep/Glob + bộ phân loại; Bash của agent ĐỌC ĐƯỢC nếu lách bộ phân loại — nhưng lời gọi vẫn bị nhận ra là agent qua job/ppid nên `HUMAN_ONLY` vẫn chặn |
| Symlink thư mục | `ln -s` | symlink cần admin/Developer Mode, nhưng **junction** (`mklink /J`) thì user thường tạo được — `guard/integrity.py` coi junction như symlink (không đi vào, gỡ bằng `rmdir`) |
| Quyền file 0600/0700 | bit POSIX | `os.chmod` chỉ bật/tắt read-only — không có tác dụng bảo mật; `owned_by_me` không so được `st_uid` (luôn 0) |
| `os.kill(pid, 0)` | kiểm sống | **TerminateProcess** — cấm dùng; luôn `oscompat.pid_alive` |
| Ngắt run | SIGINT → SIGTERM → SIGKILL cho group | `control_request interrupt` qua stdin; CTRL_BREAK cho group; cuối cùng TerminateJobObject |
| Lệnh checker eval (`kind: command`) | `/bin/sh -c` | Git Bash (`bash -c`, bỏ `System32\bash.exe` của WSL); không có bash → `cmd /c` |

## 3. Lỗ đã biết (chưa đóng)
- **Thoát Job Object**: tiến trình do dịch vụ khác tạo không thuộc job của run — `schtasks /create`, WMI (`Win32_Process.Create`,
  `Invoke-CimMethod`), `Start-Process` qua COM/`ShellExecute` của Explorer, Task Scheduler COM. Tương đương `launchctl submit` trên
  macOS (ở đó sandbox chặn). Trên Windows chỉ bộ phân loại lệnh chặn — lách được bằng cách viết khác. Tiến trình thoát ra như thế
  không mang job, không là hậu duệ (ppid = dịch vụ), env sạch (không có `CFHARNESS_RUN_ID` để `win_environ` bắt) → nếu nó đọc
  được token người thì được coi là NGƯỜI.
- **Đọc env qua PEB không phủ hết**: `oscompat.win_environ` (dùng bởi `auth.proc_environ`) chỉ đọc được tiến trình **64-bit cùng
  user**; tiến trình 32-bit (WOW64) hoặc đã thoát → không có bằng chứng env (vẫn còn bằng chứng Job Object + cây ppid).
- **Không sandbox**: agent ghi được mọi nơi user ghi được (HUB_HOME, mã hub, Startup, profile PowerShell); chỉ `permissions.deny`
  tĩnh (tool Edit/Write) + hook guard (Bash) chặn — Bash/python/node lách được bộ phân loại. Kiểm toàn vẹn chính sách
  (`guard/integrity.py`) vẫn phát hiện và khôi phục sửa đổi ngoài RPC ở `policy/` và các nhánh an toàn của `config.yaml`.
- `tui/bench.py` (đo NFR) cần `resource` + `ps` — trên Windows **bỏ qua êm**: in lý do ra stderr, in `{"skipped": true, …}` ra
  stdout, mã thoát 0 (`bench.unsupported_reason()`). Chưa có bản đo NFR cho Windows.
- `cfharness system show --log` (Task Scheduler): Task Scheduler không lưu stdout/stderr của task. Log chỉ có khi action chạy qua
  `cmd.exe /c …` và tự chuyển hướng (`>`, `>>`, `1>`, `2>`, `2>&1`; `%VAR%` mở theo môi trường hiện tại, đường dẫn tương đối tính
  từ thư mục làm việc của task) — `taskscheduler.redirect_logs`. Task khác → CLI in ghi chú `(log: Task Scheduler không lưu …)`
  kèm cách sửa action. Chuyển hướng trong script `.cmd`/`.ps1` mà task gọi thì KHÔNG đọc ra được.
- Chat terminal (`manager/attachments.py`): ảnh/chữ trong clipboard trên Windows đi qua PowerShell `-NoProfile -NonInteractive
  -STA -EncodedCommand` (Windows PowerShell 5.1 trước, `pwsh` 7 nếu không có): ảnh → PNG qua tệp tạm (đường dẫn truyền qua biến
  môi trường, xoá sau khi đọc, trần `CLIP_READ_CAP`), chữ → UTF-8 (bỏ BOM, `\r\n` → `\n`); không có/lỗi/quá 5 s → None/"". Test
  giả subprocess ở `tests/test_manager_attachments.py`; đã thử clipboard thật trên PowerShell 5.1 (chữ tiếng Việt, ảnh), `pwsh` 7
  chưa thử. Kéo–thả đường dẫn thì có (`C:\a\b`, `"C:\a b\c"`, `\\máy\share\…` — `\` là dấu phân cách, không
  phải escape như macOS).
- Revert workspace (`workspace.py`): bản trước run của file sạch lấy bằng `git cat-file --filters` trên Windows (đúng byte checkout
  ghi ra khi `core.autocrlf=true`); trên macOS/Linux vẫn `git show` như cũ.
- Dịch vụ tự khởi động (§4): nhánh Windows (Task Scheduler + dự phòng Startup) ĐÃ kiểm trên máy thật (Windows 11 Pro build
  26200, 2026-10-25 — chi tiết cuối §4); plist macOS và unit systemd Linux vẫn CHƯA kiểm trên máy thật — dựng theo tài liệu,
  test chỉ kiểm nội dung + chuỗi lệnh giả.

## 4. Tự khởi động daemon: `cfharness daemon install-service | uninstall-service`
`systems/autostart.py`. Cả hai lệnh idempotent (định nghĩa trùng nội dung và đã đăng ký → `không đổi`), có `--dry-run` (chỉ in kế
hoạch) và `--json`. Tên gắn băm 8 ký tự của HUB_HOME → nhiều HUB_HOME cùng máy không đè nhau. `daemon status` có thêm dòng
`tự khởi động: …` (cả khi daemon không chạy; trường `autostart` trong `--json`).

| Hệ điều hành | Cơ chế |
|---|---|
| Windows | Task Scheduler `\cfharness\daemon-<băm>`, đăng ký bằng XML (`HUB_HOME/service/daemon-<băm>.xml`, UTF-16) để user thường không cần admin: `LogonTrigger` của CHÍNH user, `LeastPrivilege`, `ExecutionTimeLimit=PT0S` (mặc định 72 h sẽ giết daemon), `RestartOnFailure` 1 phút × 5 (chỉ khi task KHÔNG khởi động được), `IgnoreNew`, chạy cả khi dùng pin. Lệnh: `<pythonw GUI> -c <mồi> daemon run --home <HUB_HOME>` — pythonw là tệp PE hệ con GUI (cạnh python của venv nếu là GUI, không thì `pythonw.exe` của python gốc + `__PYVENV_LAUNCHER__` = python của venv như `venvwlauncher`); mồi chạy `-m cfharness …`, chờ, con thoát mã ≠ 0 → chạy lại sau 60 s, tối đa 5 lần; mã 0 → thôi. Không console → `run_foreground` thấy không có stdout/stderr → ghi `logs/daemon.out`. |
| Windows (dự phòng) | `schtasks /Create` lỗi → file `cfharness-daemon-<băm>.cmd` trong thư mục Startup của user (`chcp 65001` rồi `start "" /min <cùng lệnh>`, cửa sổ cmd chớp một lần). Lần cài sau mà schtasks chạy được → chuyển sang Task Scheduler và xoá file Startup. |
| macOS | `~/Library/LaunchAgents/com.cfharness.daemon.<băm>.plist`: `RunAtLoad`, `KeepAlive.SuccessfulExit=false` (chỉ chạy lại khi crash), `ThrottleInterval` 30 s, env `CFHARNESS_HOME`, log `logs/daemon.out`; `launchctl bootstrap gui/<uid>` (chạy ngay). |
| Linux | `~/.config/systemd/user/cfharness-daemon-<băm>.service`: `Restart=on-failure`, `WantedBy=default.target`; `systemctl --user daemon-reload` + `enable` (chạy ở lần đăng nhập sau; `loginctl enable-linger` nếu muốn chạy khi chưa đăng nhập). |

`daemon stop` (RPC `hub.shutdown`) thoát mã 0 → không bị dịch vụ khởi động lại. Daemon đang chạy sẵn khi launchd/systemd khởi
động bản của dịch vụ → bản mới thoát lỗi "daemon đã chạy" và thử lại cho tới khi tiếp quản. Lệnh này do NGƯỜI chạy trên terminal;
agent tự tạo task/LaunchAgent vẫn bị bộ phân loại chặn (`schedule_persistence`).

**Kiểm trên máy thật — Windows 11 Pro 10.0.26200, 2026-10-25** (user Entra ID `AzureAD\…`, tiến trình Medium integrity = không
admin, venv do uv 0.11.14 tạo, CPython 3.13; HUB_HOME tạm dưới `%TEMP%`, đã gỡ + xoá sau khi kiểm):
- `--dry-run` không ghi gì; XML ghi ra UTF-16 có BOM, `schtasks /Create /XML` đăng ký được KHÔNG cần admin; `schtasks /Query /XML`
  đọc lại đúng `LogonTrigger` của chính user, `InteractiveToken`, `ExecutionTimeLimit PT0S`, `RestartOnFailure PT1M×5`,
  `IgnoreNew` (`LeastPrivilege` là mặc định nên Windows không in lại).
- Cài lần hai → `không đổi`; định nghĩa đổi → ghi đè (`/F`). `daemon status` có dòng `tự khởi động: đã cài (Task Scheduler …)`.
- `schtasks /Run` → daemon chạy trên đúng HUB_HOME tạm (`daemon status` OK, `logs/daemon.out` được tạo), không cửa sổ nào (của chính daemon — nhưng mỗi run `claude` của daemon pythonw từng bật một cửa sổ WT, xem §6).
- `daemon stop` thoát 0 → task `Ready, Last Result 0`, chờ > 60 s không chạy lại. Giết tiến trình daemon (`taskkill /F`) → mồi
  chạy lại sau 60 s.
- Dự phòng Startup (ép `schtasks /Create` lỗi bằng runner bọc): file `.cmd` chạy được, daemon lên đúng HUB_HOME; cài lại khi
  schtasks chạy được → chuyển sang Task Scheduler và xoá `.cmd`. HUB_HOME có dấu (`cfh-tự-khởi-động`) chạy đúng qua cả hai đường.
- Lỗi tìm được và đã sửa (test hồi quy trong `tests/test_autostart.py`):
  1. `.venv\Scripts\pythonw.exe` do uv tạo là bản sao CONSOLE của `python.exe` → mỗi lần chạy task bật một cửa sổ Windows Terminal
     (đóng = giết daemon) và stdout trỏ vào console nên `logs/daemon.out` không được ghi. Nay kiểm hệ con PE, dùng pythonw GUI
     của python gốc + `__PYVENV_LAUNCHER__`.
  2. `RestartOnFailure` của Task Scheduler KHÔNG chạy lại tiến trình thoát mã ≠ 0 (giết daemon → `Ready, Last Result 1`, không
     chạy lại) → thêm mồi `-c` tự chạy lại (60 s × 5).
  3. File Startup `.cmd` ghi UTF-8 nhưng cmd.exe đọc theo code page OEM → HUB_HOME có dấu bị đọc sai, daemon chạy trên một thư
     mục rác mới tạo. Nay `chcp 65001` trước dòng `start`.
- Còn lại: trigger đăng nhập THẬT (đăng xuất/đăng nhập lại) chưa thử — `schtasks /Run` chạy cùng action; `uninstall-service` để
  lại thư mục rỗng `\cfharness` trong Task Scheduler (vô hại). macOS (launchd) và Linux (systemd --user) chưa kiểm trên máy thật.

## 5. Điều phối viên khung `hermes` trên Windows: mặc định TỪ CHỐI
Khung `manager.capabilities=hermes` (Bash/Write/Web/MCP) được thiết kế để chạy dưới hook guard + approval **và sandbox OS**.
Windows không có sandbox OS → `manager/sandbox_gate.py` FAIL-CLOSED cho mọi khung rộng hơn strict (tool ngoài Read/Grep/Glob hoặc
MCP ngoài hub-self):
- `config set manager.capabilities…` / `manager capabilities set --profile hermes` → bị từ chối, không ghi;
- khung rộng có sẵn trong `config.yaml` (sửa tay, mang từ macOS sang) → khung HIỆU LỰC là strict + cảnh báo (`ManagerPermissions.capabilities`),
  nên manager có Bash/Write bị `manager.set`/run từ chối như vượt khung;
- `cfharness doctor` cảnh báo khi config ghi khung rộng (bị hạ) hoặc khi đã opt-in (đang chạy không sandbox).

Mở khoá chỉ bằng opt-in của NGƯỜI: `cfharness config set manager.allow_hermes_without_sandbox true` (mặc định `false`; chỉ đúng
giá trị `true` mới tính). Khoá nằm trong nhánh `manager` ∈ `integrity.SECURITY_KEYS` (agent sửa `config.yaml` → bị khôi phục) và
`config.set` ∈ `HUMAN_ONLY`. Tắt opt-in luôn được; khung rộng khi đó tự hạ về strict. macOS/Linux không đổi gì.

Khuyến nghị khi chạy Windows: workspace `scratch`/`worktree` cho agent không tin cậy, không cấp MCP ghi, giữ `guard.enabled: true`.

## 6. Cửa sổ console bật lên rồi tắt liên tục (2026-10-26)
Báo lỗi: "màn hình terminal bật lên rồi tắt liên tục" khi agent chạy. Windows 11 mặc định Windows Terminal làm terminal → mỗi
console MỚI có cửa sổ là một tab/cửa sổ WT mở ra rồi đóng.

**Đo bằng** `scripts/win_console_watch.py` (vỏ `scripts/win_console_watch.ps1`): mỗi ~30 ms chụp tiến trình (Toolhelp32 + dòng
lệnh từ PEB), ghi `conhost.exe` mới kèm chuỗi cha, và hỏi console của MỌI tiến trình mới qua một tiến trình phụ `DETACHED_PROCESS`
(`AttachConsole` + `GetConsoleWindow` + `IsWindowVisible`; lớp `PseudoConsoleWindow` = console đã giao cho WT). Lưu ý khi đọc số:
`conhost.exe` mới KHÔNG có nghĩa là có cửa sổ — `CREATE_NO_WINDOW` vẫn tạo conhost (console không cửa sổ). Đối chứng dương:
`CREATE_NEW_CONSOLE` → `CỬA SỔ CONSOLE HIỆN class=PseudoConsoleWindow`.

**Nguyên nhân (đã xác nhận trên máy thật, claude 2.1.310)**:
- Mọi tiến trình con của `claude` (hook qua Git Bash, MCP stdio, `git`, `rg`, `reg`…) đều được node spawn với `CREATE_NO_WINDOW`
  → mỗi cái một conhost KHÔNG cửa sổ (đây là các conhost "mỗi lần gọi hook" thấy trong báo lỗi) — tự chúng không bật cửa sổ.
- Cửa sổ thật xuất hiện khi một tiến trình **không có console** spawn chương trình console **không** có `CREATE_NO_WINDOW`:
  Windows cấp console MỚI có cửa sổ. Ca chính: daemon chạy bằng `pythonw` (dịch vụ tự khởi động, §4 — Task Scheduler + mồi
  `-c`): mỗi run `claude.exe` được cấp console riêng có cửa sổ WT (mở suốt run rồi đóng), và mọi `git`/`claude --version`/
  `claude auth status`… daemon gọi trực tiếp (checkpoint/diff workspace, doctor) cũng vậy. Hệ quả phụ: `interrupt_group`
  (`CTRL_BREAK` = `GenerateConsoleCtrlEvent`, chỉ tới tiến trình CHUNG console) không chạy được với run như thế.
- Daemon khởi động bằng `cfharness daemon start` (`detached_kwargs` = `CREATE_NO_WINDOW`) có console không cửa sổ → `claude` kế
  thừa → không cửa sổ nào; nhưng mọi chỗ spawn khác vẫn phụ thuộc may rủi "cha có console hay không".

| Đo (run haiku 3 lệnh Bash: guard + 3 hook quan sát, HUB_HOME tạm tên dài) | console mới | cửa sổ hiện |
|---|---|---|
| daemon `daemon start`, TRƯỚC sửa | 4 | 0 |
| daemon `daemon start`, SAU sửa | 2 (chỉ con trực tiếp của `claude` do node tạo: `git`, `claude` con) | 0 |
| daemon kiểu tự khởi động (`pythonw` + mồi, không console), TRƯỚC sửa | 25 | **1 / run** (`claude.exe` → `PseudoConsoleWindow`) |
| daemon kiểu tự khởi động, SAU sửa | 24 (đều không cửa sổ) | **0** |

**Sửa**:
1. `oscompat.no_window_kwargs(flags=0)` (Windows `{"creationflags": CREATE_NO_WINDOW | flags}`, POSIX `{}`) cho MỌI subprocess
   không tương tác: `guard/classifier._git` (chạy TRONG hook — hook là pythonw không console, thiếu cờ thì mỗi probe git bật một
   cửa sổ; spike đã thấy ca này: pythonw spawn console KHÔNG cờ → `PseudoConsoleWindow`, CÓ cờ → không cửa sổ), `workspace._git`,
   `evals/checkers` (lệnh checker), `manager/attachments` (PowerShell clipboard), `cli` (`claude auth status`), `runtime/claude_cli`
   (`--version`, oneshot judge/curator), `systems/taskscheduler`, `oscompat.kill_tree`. KHÔNG áp cho spawn run chính
   (`ClaudeCliRuntime.start` — phải chung console với daemon để nhận CTRL_BREAK) và takeover tương tác (`tui/app.py`, `tui/cli.py`).
   `tests/test_windows_no_console.py` quét AST toàn `src/cfharness`: mọi `subprocess.run|Popen|call|check_output|check_call` /
   `asyncio.create_subprocess_*` phải dùng `no_window_kwargs`/`new_group_kwargs`/`detached_kwargs` hoặc nằm trong ALLOWLIST có lý do.
2. Hook (`guard.hook`, `observe_hook`) và MCP của run (`hub-guard`, `hub-self`) chạy bằng `pythonw` GUI (`guard.runtime.child_python`,
   dùng lại `systems.autostart.windows_gui_python` — pythonw của venv uv là bản console): hook =
   `__PYVENV_LAUNCHER__=<python venv> '<pythonw gốc>' -I -m … || exit 2`, MCP = `command: <pythonw gốc>`, `env.__PYVENV_LAUNCHER__`.
   Không cấp console → không thể bật cửa sổ dù cha nào. `guard.python` do người đặt → dùng nguyên; không có pythonw GUI → như cũ.
3. `oscompat.ensure_hidden_console()` gọi đầu `daemon.run_foreground`: không có console (pythonw) → `AllocConsoleWithOptions(NO_WINDOW)`
   (Windows 11 24H2+; máy kiểm 26200 đi nhánh này) → dự phòng: gắn (`AttachConsole`) vào console không cửa sổ của một `cmd /c pause`
   phụ `CREATE_NO_WINDOW` rồi cho nó thoát → cuối cùng `AllocConsole` + `ShowWindow(SW_HIDE)`. Log `daemon` ghi cách đã dùng. Đã kiểm
   dưới pythonw: nhánh `no_window` và nhánh `attached` (ép bỏ `AllocConsoleWithOptions`) → con console không cờ kế thừa console
   không cửa sổ (hwnd 0); `interrupt_group` → con nhận `CTRL_BREAK` (SIGBREAK, mã 7).

**Spike pythonw** (Git Bash như CLI chạy hook; pythonw gốc của uv `cpython-3.13`, `__PYVENV_LAUNCHER__` = `.venv\Scripts\python.exe`):
- `-I` vẫn vào venv (`sys.prefix` = `.venv`, import được `cfharness`); không có `__PYVENV_LAUNCHER__` → python gốc, KHÔNG có cfharness.
- stdin/stdout/stderr qua pipe chạy (`sys.stdout` không None), không có console (`GetConsoleCP() == 0`).
- Mã thoát giữ nguyên (3 → 3); `… exit 1 || exit 2` → 2; pythonw không tồn tại → 127 → `|| exit 2` → 2 (fail-closed giữ nguyên);
  `cfharness.guard.hook` với stdin hỏng → in `deny` + exit 0.
- Run thật qua daemon tạm (haiku): 3 `guard.decision` từ hook, 4 `hook.observed`, `mcp_servers: hub-guard connected`.
- Chi phí cả buổi (8 run haiku + 1 review fork tự động): $0.0190.

**Test**: `tests/test_windows_no_console.py` (quét AST, `no_window_kwargs`, `ensure_hidden_console` thật dưới pythonw trên Windows,
`child_python`/`hook_command`/MCP entry/observe hook với `IS_WINDOWS` giả). `tests/fake_claude_guard.py` nay chạy lệnh hook qua bash
như CLI thật (Windows: Git Bash — `VAR=… prog` không chạy trong `cmd.exe`).

**Còn lại / giới hạn**: conhost không cửa sổ của các con trực tiếp của `claude` (do node tạo) vẫn còn — vô hại, ngoài tầm hub.
`win_console_watch.ps1` là vỏ mỏng gọi bản Python (môi trường kiểm không chạy được PowerShell) — chưa chạy thử bản `.ps1`.
Chưa kiểm daemon do Task Scheduler THẬT chạy sau bản sửa (đã mô phỏng đúng action: `pythonw -c <mồi> daemon run --home …` spawn
`DETACHED_PROCESS`). Báo lỗi gốc có thể đến từ daemon tự khởi động hoặc một nguồn ngoài hub (máy đo có cả phiên Claude Code tương
tác — các bash/git của nó cũng đều không cửa sổ).
