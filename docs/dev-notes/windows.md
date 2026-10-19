# Windows — mô hình an toàn khác macOS/Linux ở đâu

cfharness chạy native trên Windows (Python 3.13, `claude` 2.1.303). Trên macOS/Linux mọi hành vi giữ nguyên; các khác biệt dưới đây
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
  không mang job, không là hậu duệ (ppid = dịch vụ), env sạch → nếu nó đọc được token người thì được coi là NGƯỜI.
- **Không sandbox**: agent ghi được mọi nơi user ghi được (HUB_HOME, mã hub, Startup, profile PowerShell); chỉ `permissions.deny`
  tĩnh (tool Edit/Write) + hook guard (Bash) chặn — Bash/python/node lách được bộ phân loại. Kiểm toàn vẹn chính sách
  (`guard/integrity.py`) vẫn phát hiện và khôi phục sửa đổi ngoài RPC ở `policy/`.
- `tui/bench.py` (đo NFR) chỉ chạy macOS/Linux (`resource`, `ps`).
- Chat terminal (`manager/attachments.py`): ảnh trong clipboard (`clipboard_image`/`clipboard_text`) chưa hỗ trợ Windows; kéo–thả
  đường dẫn thì có (`C:\a\b`, `"C:\a b\c"`, `\\máy\share\…` — `\` là dấu phân cách, không phải escape như macOS).
- Revert workspace (`workspace.py`): bản trước run của file sạch lấy bằng `git cat-file --filters` trên Windows (đúng byte checkout
  ghi ra khi `core.autocrlf=true`); trên macOS/Linux vẫn `git show` như cũ.

Khuyến nghị khi chạy Windows: workspace `scratch`/`worktree` cho agent không tin cậy, không cấp MCP ghi, giữ `guard.enabled: true`.
