#!/usr/bin/env python3
"""Windows: ghi lại, trong N giây, mọi console MỚI (conhost/OpenConsole + chuỗi tiến trình cha) và console nào có CỬA SỔ HIỆN.

    uv run python scripts/win_console_watch.py 40                         # 40 s, mọi tiến trình
    uv run python scripts/win_console_watch.py 60 --match cfh-tmp-home    # chỉ đếm console có tổ tiên chứa chuỗi này
    pwsh scripts/win_console_watch.ps1 -Seconds 40 -Match cfharness       # cùng script, gọi từ PowerShell

Dùng để đo lỗi "terminal bật lên rồi tắt liên tục" (docs/dev-notes/windows.md §6) trước/sau khi sửa.

Cách đo (chỉ đọc, không đụng tiến trình nào):
- mỗi ~20 ms chụp danh sách tiến trình (Toolhelp32) → tiến trình mới: pid, ppid, exe, dòng lệnh (PEB qua
  `NtQueryInformationProcess(ProcessCommandLineInformation)`), giữ lại cả sau khi tiến trình đã thoát để dựng chuỗi cha;
- `conhost.exe`/`OpenConsole.exe` mới = một console mới được cấp (có cửa sổ hay không);
- "có cửa sổ hiện": tiến trình con mới được hỏi `GetConsoleWindow()` qua một tiến trình phụ KHÔNG console
  (`DETACHED_PROCESS` → `AttachConsole(pid)` được). `CREATE_NO_WINDOW` → không có cửa sổ (NULL). Có cửa sổ và
  `IsWindowVisible` → hiện (Windows Terminal là terminal mặc định: lớp `PseudoConsoleWindow` = một tab/cửa sổ WT mở ra;
  `ConsoleWindowClass` ẩn bằng SW_HIDE thì không tính). Đếm theo hwnd duy nhất (nhiều tiến trình chung một console).
Giới hạn: tiến trình sống < ~20 ms có thể lọt; cửa sổ GUI thường (không phải console) không được đếm.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any

if sys.platform != "win32":  # pragma: no cover
    sys.exit("chỉ chạy trên Windows")

import ctypes
from ctypes import wintypes as w

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
u32 = ctypes.WinDLL("user32")


class _PE(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("pid", w.DWORD), ("heap", ctypes.c_size_t), ("mod", w.DWORD),
                ("threads", w.DWORD), ("ppid", w.DWORD), ("pri", ctypes.c_long), ("flags", w.DWORD), ("exe", w.WCHAR * 260)]


k32.CreateToolhelp32Snapshot.restype = w.HANDLE
k32.OpenProcess.restype = w.HANDLE
k32.CloseHandle.argtypes = (w.HANDLE,)
k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = (w.HANDLE, ctypes.POINTER(_PE))
k32.GetConsoleWindow.restype = w.HWND
ntdll.NtQueryInformationProcess.argtypes = (w.HANDLE, ctypes.c_int, w.LPVOID, w.ULONG, ctypes.POINTER(w.ULONG))
CONSOLE_HOSTS = ("conhost.exe", "openconsole.exe")


def snapshot() -> dict[int, tuple[int, str]]:
    out: dict[int, tuple[int, str]] = {}
    h = k32.CreateToolhelp32Snapshot(2, 0)
    if not h or h == w.HANDLE(-1).value:
        return out
    try:
        e = _PE()
        e.dwSize = ctypes.sizeof(_PE)
        ok = k32.Process32FirstW(h, ctypes.byref(e))
        while ok:
            out[int(e.pid)] = (int(e.ppid), e.exe)
            ok = k32.Process32NextW(h, ctypes.byref(e))
    finally:
        k32.CloseHandle(h)
    return out


def cmdline(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_string_buffer(65536)
        n = w.ULONG()
        if ntdll.NtQueryInformationProcess(h, 60, buf, 65536, ctypes.byref(n)) != 0:  # ProcessCommandLineInformation
            return ""
        ln = int.from_bytes(buf.raw[0:2], "little")  # UNICODE_STRING ngay đầu buffer, chuỗi theo sau (offset 16 trên x64)
        return ctypes.wstring_at(ctypes.addressof(buf) + 16, ln // 2)
    finally:
        k32.CloseHandle(h)


# ---------------------------------------------------------------------------- tiến trình phụ hỏi console
def probe_server() -> int:
    """Chạy trong tiến trình DETACHED (không console): đọc pid từ stdin, trả JSON {pid, hwnd, cls, visible}."""
    k32.FreeConsole()
    for line in sys.stdin:
        pid = int(line.strip() or 0)
        res = {"pid": pid, "hwnd": 0, "cls": "", "visible": False, "attached": False}
        if pid and k32.AttachConsole(pid):
            res["attached"] = True
            hwnd = k32.GetConsoleWindow()
            if hwnd:
                b = ctypes.create_unicode_buffer(256)
                u32.GetClassNameW(hwnd, b, 256)
                res.update(hwnd=int(hwnd), cls=b.value, visible=bool(u32.IsWindowVisible(hwnd)))
            k32.FreeConsole()
        elif pid:
            res["err"] = ctypes.get_last_error()
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()
    return 0


class Prober:
    """Tiến trình phụ hỏi console. `AttachConsole` vào tiến trình đang TREO (run được spawn `CREATE_SUSPENDED` để gán Job
    Object) bị chặn tới khi nó chạy tiếp → mỗi câu hỏi có hạn 0.5 s; quá hạn thì giết tiến trình phụ và tạo cái mới."""

    def __init__(self) -> None:
        self.timeouts = 0
        self._start()

    def _start(self) -> None:
        import queue
        import threading

        # python GỐC (không qua trampoline của venv uv — trampoline không console sẽ cấp console mới cho python con)
        exe = getattr(sys, "_base_executable", None) or sys.executable
        self.p = subprocess.Popen([exe, "-I", __file__, "--probe-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, creationflags=0x00000008)  # DETACHED_PROCESS
        self.q: Any = queue.Queue()
        out = self.p.stdout

        def pump() -> None:
            for line in out:  # type: ignore[union-attr]
                self.q.put(line)

        threading.Thread(target=pump, daemon=True).start()

    def ask(self, pid: int, timeout: float = 0.5) -> dict:
        import queue

        assert self.p.stdin
        try:
            self.p.stdin.write(f"{pid}\n")
            self.p.stdin.flush()
            return json.loads(self.q.get(timeout=timeout))
        except (queue.Empty, OSError, ValueError):
            self.timeouts += 1
            self.p.kill()
            self._start()
            return {"pid": pid, "hwnd": 0, "cls": "", "visible": False, "attached": False, "timeout": True}

    def close(self) -> None:
        try:
            if self.p.stdin:
                self.p.stdin.close()
            self.p.wait(5)
        except Exception:
            self.p.kill()


# ---------------------------------------------------------------------------- vòng đo
def watch(seconds: float, match: str, out, verbose: bool = False) -> dict:
    info: dict[int, tuple[int, str, str | None]] = {}
    seen = snapshot()
    for pid, (pp, exe) in seen.items():
        info[pid] = (pp, exe, None)
    prober = Prober()
    visible_hwnds: set[int] = set()
    windowed_hwnds: set[int] = set()
    consoles = visible = snaps = 0
    t0 = time.monotonic()

    def chain(pid: int, depth: int = 5) -> list[str]:
        res, p = [], pid
        for _ in range(depth):
            if p not in info:
                break
            pp, exe, cl = info[p]
            if cl is None:  # tiến trình có từ trước khi đo: đọc dòng lệnh khi cần (còn sống thì đọc được)
                cl = cmdline(p)
                info[p] = (pp, exe, cl)
            res.append(f"{p}:{exe} {cl[:220]}".rstrip())
            p = pp
        return res

    def relevant(pid: int) -> bool:
        return not match or any(match.lower() in c.lower() for c in chain(pid, 8))

    def log(msg: str) -> None:
        print(f"[{time.monotonic() - t0:7.2f}s] {msg}", file=out, flush=True)

    import queue
    import threading

    todo: Any = queue.Queue()
    lock = threading.Lock()

    def probe_loop() -> None:
        # luồng riêng: hỏi console có thể chậm (tiến trình treo → hết hạn 0.5 s) — vòng chụp tiến trình không được chờ nó
        nonlocal visible
        while True:
            item = todo.get()
            if item is None:
                return
            pid, ts, desc, tries = item
            time.sleep(max(0.0, ts + 0.03 - time.monotonic()))  # ≥ 30 ms sau khi thấy: conhost kịp tạo cửa sổ
            r = prober.ask(pid)
            if not r["attached"] and r.get("err") in (6, 31) and tries < 8:
                # console đang được giao cho Windows Terminal (defterm) → AttachConsole lỗi tạm; thử lại sau 100 ms
                todo.put((pid, time.monotonic() + 0.07, desc, tries + 1))
                continue
            with lock:
                if verbose:
                    log(f"  console của {pid}: hwnd={r['hwnd']} class={r['cls']} visible={r['visible']} "
                        f"attached={r['attached']} err={r.get('err')}")
                if r["hwnd"]:
                    windowed_hwnds.add(r["hwnd"])
                if r["visible"] and r["hwnd"] not in visible_hwnds:
                    visible_hwnds.add(r["hwnd"])
                    visible += 1
                    log(f"CỬA SỔ CONSOLE HIỆN class={r['cls']} hwnd={r['hwnd']} ← {desc}")

    th = threading.Thread(target=probe_loop, daemon=True)
    th.start()
    try:
        while time.monotonic() - t0 < seconds:
            cur = snapshot()
            now = time.monotonic()
            fresh = [pid for pid in cur if pid not in seen]
            for pid in fresh:
                pp, exe = cur[pid]
                info[pid] = (pp, exe, cmdline(pid))
            for pid in fresh:
                pp, exe = cur[pid]
                if exe.lower() in CONSOLE_HOSTS:
                    if relevant(pp):
                        consoles += 1
                        log(f"CONSOLE MỚI {exe} pid={pid} ← " + " ← ".join(chain(pp)))
                elif relevant(pid):
                    # mọi tiến trình mới (không chỉ cha của conhost): console được giao cho Windows Terminal (defterm) thì
                    # conhost chỉ sống vài ms — chỉ hỏi chính tiến trình mới thấy được tab/cửa sổ WT
                    todo.put((pid, now, " ← ".join(chain(pid)), 0))
            seen = cur
            snaps += 1
            time.sleep(0.02)
    finally:
        todo.put(None)
        th.join(5)
        prober.close()
    summary = {"seconds": seconds, "match": match, "new_consoles": consoles, "visible_console_windows": visible,
               "consoles_with_window": len(windowed_hwnds), "probe_timeouts": prober.timeouts, "snapshots": snaps}
    print("TÓM TẮT " + json.dumps(summary, ensure_ascii=False), file=out, flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("seconds", nargs="?", type=float, default=40.0)
    ap.add_argument("--match", default="", help="chỉ đếm console có tiến trình tổ tiên (exe/dòng lệnh) chứa chuỗi này")
    ap.add_argument("-v", "--verbose", action="store_true", help="in kết quả hỏi console của từng tiến trình mới")
    ap.add_argument("--probe-server", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.probe_server:
        return probe_server()
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    watch(a.seconds, a.match, sys.stdout, a.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
