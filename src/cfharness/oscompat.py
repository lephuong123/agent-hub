"""Khác biệt hệ điều hành (macOS/Linux ↔ Windows) gom một chỗ — chỉ stdlib + ctypes (hook import được, chạy nhanh).

Nguyên tắc: trên POSIX mọi hàm giữ ĐÚNG hành vi cũ (os.kill/killpg, st_uid, start_new_session); nhánh Windows thay
bằng cơ chế tương đương do KERNEL đảm bảo:

- tiến trình còn sống / ppid / thời điểm bắt đầu: `OpenProcess` + `GetExitCodeProcess`, `CreateToolhelp32Snapshot`,
  `GetProcessTimes` (KHÔNG dùng `os.kill(pid, 0)` — trên Windows lệnh đó là TerminateProcess);
- "session" của run (POSIX: `start_new_session` + `getsid`) → **Job Object**: run được spawn treo (`CREATE_SUSPENDED`),
  gán vào job rồi mới chạy, nên mọi hậu duệ (kể cả `start /b`, tiến trình mồ côi) ở trong job — không thoát được vì job
  không bật `BREAKAWAY_OK`. Dừng run = `TerminateJobObject` (thay `killpg`);
- quyền file 0600: hồ sơ người dùng trên NTFS đã chỉ cho chính user (+SYSTEM/Administrators) — `st_uid` luôn 0 nên
  không so được, coi như của mình.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Any

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:  # pragma: no cover — chỉ chạy trên Windows
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _iphlp = ctypes.WinDLL("iphlpapi")

    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_SUSPEND_RESUME = 0x0800
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    SYNCHRONIZE = 0x00100000
    STILL_ACTIVE = 259
    TH32CS_SNAPPROCESS = 0x00000002
    TOKEN_QUERY = 0x0008
    TOKEN_USER_CLASS = 1
    AF_INET = 2
    TCP_TABLE_OWNER_PID_ALL = 5
    CREATE_SUSPENDED = 0x00000004
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    JobObjectBasicProcessIdList = 3

    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    _k32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    _adv.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    _adv.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _adv.ConvertSidToStringSidW.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
    _k32.LocalFree.argtypes = (wintypes.HLOCAL,)
    _ntdll.NtQueryInformationProcess.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG))
    _k32.ReadProcessMemory.argtypes = (wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
    _k32.IsWow64Process.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    _iphlp.GetExtendedTcpTable.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    _k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    _k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))

    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD),
                    ("dwRemoteAddr", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]

    def _open(pid: int, access: int) -> Any:
        return _k32.OpenProcess(access, False, int(pid)) or None

    def _filetime_to_epoch(ft: Any) -> float:
        # FILETIME: 100 ns kể từ 1601-01-01
        return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7 - 11644473600.0


# ============================================================================ tiến trình
def pid_alive(pid: int | None) -> bool:
    """Tiến trình còn sống? (POSIX: `os.kill(pid, 0)`, tiến trình của user khác vẫn tính là sống)."""
    if not pid or int(pid) <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)  # pragma: no cover
    if not h:
        return ctypes.get_last_error() == 5  # ACCESS_DENIED → có tồn tại (của user khác)
    try:
        code = wintypes.DWORD()
        return bool(_k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


def win_parent_pids() -> dict[int, int]:  # pragma: no cover — Windows
    """pid → ppid của mọi tiến trình (một snapshot Toolhelp32)."""
    out: dict[int, int] = {}
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = int(e.th32ParentProcessID)
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return out


def win_start_time(pid: int) -> float | None:  # pragma: no cover — Windows
    """Thời điểm tiến trình bắt đầu (epoch giây) theo `GetProcessTimes`; None nếu không mở được/đã thoát."""
    h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return _filetime_to_epoch(c)
    finally:
        _k32.CloseHandle(h)


def _token_user_sid(proc_handle: Any) -> str | None:  # pragma: no cover — Windows
    """SID user của token tiến trình, dạng `S-1-5-…` (đổi ngay khi buffer còn sống — Sid là con trỏ VÀO buffer)."""
    tok = wintypes.HANDLE()
    if not _adv.OpenProcessToken(proc_handle, TOKEN_QUERY, ctypes.byref(tok)):
        return None
    try:
        need = wintypes.DWORD()
        _adv.GetTokenInformation(tok, TOKEN_USER_CLASS, None, 0, ctypes.byref(need))
        buf = ctypes.create_string_buffer(need.value or 256)
        if not _adv.GetTokenInformation(tok, TOKEN_USER_CLASS, buf, len(buf), ctypes.byref(need)):
            return None
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        out = wintypes.LPWSTR()
        if not _adv.ConvertSidToStringSidW(psid, ctypes.byref(out)):
            return None
        try:
            return out.value
        finally:
            _k32.LocalFree(out)
    finally:
        _k32.CloseHandle(tok)


def win_environ(pid: int) -> dict[str, str] | None:  # pragma: no cover — Windows
    """Biến môi trường của tiến trình 64-bit cùng user: PEB → RTL_USER_PROCESS_PARAMETERS.Environment (khối UTF-16).
    None nếu không mở/đọc được (khác user, tiến trình 32-bit, đã thoát) — người gọi coi như không có bằng chứng."""
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    h = _open(pid, PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)
    if not h:
        return None
    try:
        wow = wintypes.BOOL()
        if _k32.IsWow64Process(h, ctypes.byref(wow)) and wow.value:
            return None

        def read(addr: int, n: int) -> bytes | None:
            buf = ctypes.create_string_buffer(n)
            got = ctypes.c_size_t()
            if not _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
                return None
            return buf.raw[: got.value]

        pbi = (ctypes.c_void_p * 6)()  # PROCESS_BASIC_INFORMATION: [1] = PebBaseAddress
        if _ntdll.NtQueryInformationProcess(h, 0, pbi, ctypes.sizeof(pbi), None) != 0 or not pbi[1]:
            return None
        raw = read(pbi[1] + 0x20, 8)  # PEB.ProcessParameters (x64)
        if not raw:
            return None
        params = int.from_bytes(raw, "little")
        raw = read(params + 0x80, 8)  # RTL_USER_PROCESS_PARAMETERS.Environment
        size_raw = read(params + 0x3F0, 8)  # .EnvironmentSize (Win8+)
        if not raw:
            return None
        env_addr = int.from_bytes(raw, "little")
        size = int.from_bytes(size_raw, "little") if size_raw else 0
        if not 0 < size <= 4 * 1024 * 1024:
            size = 64 * 1024
        block = read(env_addr, size)
        if not block:
            return None
        text = block.decode("utf-16-le", "replace").split("\0\0", 1)[0]
        env: dict[str, str] = {}
        for item in text.split("\0"):
            k, sep, v = item.partition("=")
            if sep and k:  # bỏ mục kiểu "=C:=C:\x" (k rỗng)
                env[k] = v
        return env
    finally:
        _k32.CloseHandle(h)


def same_user(pid: int | None) -> bool:
    """Tiến trình `pid` chạy dưới CÙNG user OS với tiến trình này? (POSIX: True — quyền file socket 0600 đã lọc.)"""
    if not IS_WINDOWS:
        return True
    if not pid:  # pragma: no cover
        return False
    h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)  # pragma: no cover
    if not h:
        return False
    try:
        theirs = _token_user_sid(h)
        return theirs is not None and theirs == _token_user_sid(_k32.GetCurrentProcess())
    finally:
        _k32.CloseHandle(h)



def owned_by_me(st: os.stat_result) -> bool:
    """File thuộc user hiện tại? Windows: `st_uid` luôn 0 — dựa vào ACL hồ sơ người dùng (xem docstring module)."""
    if IS_WINDOWS:
        return True
    return st.st_uid == os.getuid()


def uid_tag() -> str:
    """Định danh user cho tên file tạm dùng chung (/tmp) — Windows dùng tên user."""
    if hasattr(os, "getuid"):
        return str(os.getuid())
    return os.environ.get("USERNAME", "user")


# ============================================================================ spawn / dừng
def new_group_kwargs(*, suspended: bool = False) -> dict[str, Any]:
    """kwargs cho Popen/create_subprocess_exec: tiến trình (và hậu duệ) thành một nhóm dừng được cùng nhau.
    POSIX: `start_new_session` (session = pid). Windows: process group mới (nhận CTRL_BREAK), tuỳ chọn spawn treo để
    gán Job Object trước khi tiến trình kịp sinh con (`WinJob.adopt`)."""
    if not IS_WINDOWS:
        return {"start_new_session": True}
    flags = CREATE_NEW_PROCESS_GROUP  # pragma: no cover
    if suspended:
        flags |= CREATE_SUSPENDED
    return {"creationflags": flags}


NO_WINDOW_FLAG = 0x08000000  # CREATE_NO_WINDOW — hằng số ở mức module để test giả IS_WINDOWS trên POSIX vẫn dùng được


def no_window_kwargs(flags: int = 0) -> dict[str, Any]:
    """kwargs cho subprocess KHÔNG tương tác (git, `claude --version`, PowerShell clipboard…): Windows → `CREATE_NO_WINDOW`
    (gộp OR với `flags` nếu có), không bao giờ bật cửa sổ console — kể cả khi tiến trình gọi không có console (pythonw,
    daemon do Task Scheduler chạy) — xem docs/dev-notes/windows.md §6. POSIX: `{}`.
    KHÔNG dùng cho tiến trình run chính (`ClaudeCliRuntime.start` — phải chung console với daemon để nhận CTRL_BREAK,
    `interrupt_group`) hay takeover tương tác (cần console của người)."""
    if not IS_WINDOWS:
        return {}  # POSIX: Popen từ chối creationflags ≠ 0
    return {"creationflags": NO_WINDOW_FLAG | flags}



def ensure_hidden_console() -> str:
    """Windows: tiến trình KHÔNG có console (daemon chạy bằng pythonw — dịch vụ tự khởi động) → tự lấy một console KHÔNG
    cửa sổ, để `claude` và mọi hậu duệ kế thừa nó: không thì mỗi run (và mỗi git/`claude --version` thiếu
    `CREATE_NO_WINDOW`) được Windows cấp console MỚI có cửa sổ (Windows Terminal bật lên rồi tắt), và `interrupt_group`
    (CTRL_BREAK chỉ gửi được trong CÙNG console) không chạy. Trả cách đã dùng: `"posix"`, `"existing"`, `"no_window"`
    (`AllocConsoleWithOptions(NO_WINDOW)`, Windows 11 24H2+), `"attached"` (gắn vào console không cửa sổ của một tiến
    trình phụ `CREATE_NO_WINDOW`), `"hidden"` (`AllocConsole` + `ShowWindow(SW_HIDE)` — có thể chớp một lần) hoặc `"failed"`.
    Không đụng `sys.stdout/stderr` (pythonw: đã trỏ vào `logs/daemon.out`)."""
    if not IS_WINDOWS:
        return "posix"
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # pragma: no cover — Windows
    k32.GetConsoleWindow.restype = wintypes.HWND
    if k32.GetConsoleCP():  # đã có console (có hoặc không cửa sổ) → giữ nguyên
        return "existing"
    try:  # Windows 11 24H2+: console không cửa sổ, không qua Windows Terminal
        class _Opts(ctypes.Structure):
            _fields_ = [("mode", ctypes.c_int), ("useShowWindow", wintypes.BOOL), ("showWindow", wintypes.WORD)]

        fn = k32.AllocConsoleWithOptions
        fn.restype = ctypes.c_long
        res = ctypes.c_int(0)
        if fn(ctypes.byref(_Opts(2, False, 0)), ctypes.byref(res)) == 0 and k32.GetConsoleCP():  # ALLOC_CONSOLE_MODE_NO_WINDOW
            return "no_window"
    except AttributeError:
        pass
    try:  # bản Windows cũ hơn: mượn console không cửa sổ của một tiến trình phụ rồi cho nó thoát (console còn vì ta đã gắn)
        p = subprocess.Popen(["cmd.exe", "/d", "/c", "pause"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **no_window_kwargs())
        try:
            for _ in range(50):
                if k32.AttachConsole(p.pid):
                    return "attached"
                time.sleep(0.02)
        finally:
            p.kill()
            p.wait(5)
    except OSError:
        pass
    if k32.AllocConsole():
        hwnd = k32.GetConsoleWindow()
        if hwnd:
            ctypes.WinDLL("user32").ShowWindow(hwnd, 0)  # SW_HIDE
        return "hidden"
    return "failed"


def exec_argv(argv: list[str]) -> list[str]:
    """Windows: CreateProcess chỉ tự thêm `.exe` — tìm `claude.cmd`/… trong PATH; script `.py` (claude giả trong test)
    chạy bằng python hiện tại. POSIX: giữ nguyên."""
    if not IS_WINDOWS or not argv:
        return argv
    if argv[0].lower().endswith(".py"):  # pragma: no cover
        return [sys.executable, *argv]
    import shutil  # pragma: no cover

    return [shutil.which(argv[0]) or argv[0], *argv[1:]]


def detached_kwargs() -> dict[str, Any]:
    """kwargs spawn daemon nền: tách khỏi terminal. Windows: console ẩn riêng (không DETACHED_PROCESS — tiến trình con
    cần chung console để nhận CTRL_BREAK) + group mới (Ctrl+C của terminal không lan tới)."""
    if not IS_WINDOWS:
        return {"start_new_session": True}
    return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}  # pragma: no cover


def interrupt_group(pid: int) -> bool:
    """Gửi "ngắt" cho cả nhóm: POSIX SIGINT (killpg); Windows CTRL_BREAK_EVENT (node hiểu là SIGBREAK).
    Windows: CHỈ gọi với pid là gốc process group do chính ta spawn (`new_group_kwargs`) — pid khác thì
    GenerateConsoleCtrlEvent phát cho mọi tiến trình chung console, kể cả tiến trình gọi."""
    try:
        if IS_WINDOWS:
            os.kill(int(pid), signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            os.killpg(int(pid), signal.SIGINT)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def kill_tree(pid: int) -> bool:
    """Giết cả cây tiến trình theo ppid (Windows, khi không có Job Object — vd sau khi daemon khởi động lại)."""
    if not IS_WINDOWS:
        try:
            os.killpg(int(pid), signal.SIGKILL)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    try:  # pragma: no cover
        cp = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=15, **no_window_kwargs())
        return cp.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class WinJob:  # pragma: no cover — Windows
    """Job Object của MỘT run: tương đương session/process group trên POSIX (xem docstring module)."""

    def __init__(self) -> None:
        self.handle = _k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW lỗi")
        self.closed = False

    def adopt(self, pid: int, *, resume: bool) -> None:
        """Gán tiến trình (đang treo nếu spawn `suspended=True`) vào job rồi cho chạy tiếp."""
        h = _open(pid, PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME | PROCESS_QUERY_LIMITED_INFORMATION)
        if not h:
            raise OSError(ctypes.get_last_error(), f"OpenProcess({pid}) lỗi")
        try:
            if not _k32.AssignProcessToJobObject(self.handle, h):
                err = ctypes.get_last_error()
                if resume:
                    _ntdll.NtResumeProcess(h)
                raise OSError(err, f"AssignProcessToJobObject({pid}) lỗi")
            if resume:
                _ntdll.NtResumeProcess(h)
        finally:
            _k32.CloseHandle(h)

    def contains(self, pid: int) -> bool:
        if self.closed:
            return False
        h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
        if not h:
            return False
        try:
            res = wintypes.BOOL()
            return bool(_k32.IsProcessInJob(h, self.handle, ctypes.byref(res))) and bool(res.value)
        finally:
            _k32.CloseHandle(h)

    def pids(self) -> list[int]:
        if self.closed:
            return []
        # JOBOBJECT_BASIC_PROCESS_ID_LIST: DWORD assigned, DWORD số pid trong danh sách, ULONG_PTR pid[]
        n = 1024
        buf = ctypes.create_string_buffer(8 + n * ctypes.sizeof(ctypes.c_size_t))
        if not _k32.QueryInformationJobObject(self.handle, JobObjectBasicProcessIdList, buf, len(buf), None):
            return []
        count = min(int(wintypes.DWORD.from_buffer(buf, 4).value), n)
        ids = (ctypes.c_size_t * n).from_buffer(buf, 8)
        return [int(ids[i]) for i in range(count)]

    def terminate(self, code: int = 1) -> bool:
        if self.closed:
            return False
        return bool(_k32.TerminateJobObject(self.handle, code))

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            _k32.CloseHandle(self.handle)


class JobRegistry:
    """run → Job Object, giữ `keep_s` sau khi run kết thúc để vẫn nhận ra hậu duệ mồ côi (như session của run trên POSIX).
    Trên POSIX mọi thao tác là no-op."""

    def __init__(self, keep_s: float = 24 * 3600) -> None:
        self.keep_s = keep_s
        self._jobs: dict[str, tuple[Any, float | None]] = {}

    def create(self, run_id: str) -> Any:
        if not IS_WINDOWS:
            return None
        self.prune()  # pragma: no cover
        job = WinJob()
        self._jobs[run_id] = (job, None)
        return job

    def get(self, run_id: str) -> Any:
        ent = self._jobs.get(run_id)
        return ent[0] if ent else None

    def finished(self, run_id: str) -> None:
        ent = self._jobs.get(run_id)
        if ent:
            self._jobs[run_id] = (ent[0], time.time())

    def run_of(self, pid: int) -> str | None:
        """run có job chứa `pid` (đang chạy trước, rồi run đã xong trong `keep_s`)."""
        for rid, (job, _done) in sorted(self._jobs.items(), key=lambda kv: kv[1][1] is not None):
            if job.contains(pid):
                return rid
        return None

    def prune(self) -> None:
        now = time.time()
        for rid, (job, done) in list(self._jobs.items()):
            if done is not None and now - done > self.keep_s:
                job.close()
                del self._jobs[rid]

    def close_all(self) -> None:
        for job, _ in self._jobs.values():
            job.close()
        self._jobs.clear()


JOBS = JobRegistry()


# ============================================================================ peer pid qua TCP loopback (Windows)
def tcp_peer_pid(sock: Any) -> int | None:
    """pid sở hữu đầu kia một kết nối TCP loopback IPv4 — từ bảng TCP của KERNEL (`GetExtendedTcpTable`), không giả
    được từ payload. Dòng cần tìm: local = địa chỉ ngang hàng của ta, remote = địa chỉ của ta."""
    if not IS_WINDOWS or sock is None:
        return None
    try:  # pragma: no cover
        lhost, lport = sock.getsockname()[:2]
        rhost, rport = sock.getpeername()[:2]
    except OSError:
        return None
    import socket as _s  # pragma: no cover
    import struct

    def _addr(host: str) -> int:
        return struct.unpack("<I", _s.inet_aton(host))[0]

    def _port(p: int) -> int:
        return _s.htons(p)

    size = wintypes.DWORD(0)  # pragma: no cover
    _iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wintypes.DWORD(len(buf))
        if _iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0) == 0:
            break
    else:
        return None
    n = struct.unpack_from("<I", buf.raw)[0]
    rows = ctypes.cast(ctypes.addressof(buf) + 4, ctypes.POINTER(MIB_TCPROW_OWNER_PID))
    want = (_addr(rhost), _port(rport), _addr(lhost), _port(lport))
    for i in range(n):
        r = rows[i]
        if (r.dwLocalAddr, r.dwLocalPort & 0xFFFF, r.dwRemoteAddr, r.dwRemotePort & 0xFFFF) == want:
            return int(r.dwOwningPid) or None
    return None


def utf8_stdio() -> None:
    """stdin/stdout/stderr UTF-8 cho tiến trình hook/MCP (`python -I` bỏ qua PYTHONUTF8; Windows mặc định cp1252 —
    Claude Code đọc JSON quyết định của hook là UTF-8)."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
