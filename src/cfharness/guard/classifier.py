"""Bộ phân loại thao tác phá huỷ (FR-SG-01, FR-SG-03 bước 1 "luật tĩnh").

`classify(tool_name, tool_input, ctx) -> Verdict` là hàm thuần (chỉ đọc filesystem/git để biết đích có phải
"file run tự tạo" hay không). Kết quả:
- `safe`        : chỉ đọc → cho chạy.
- `write`       : ghi thông thường → cho chạy + audit (không hỏi).
- `destructive` : thuộc danh mục FR-SG-01 hoặc chạm tài nguyên được bảo vệ → mở ApprovalRequest.
- `uncertain`   : không phân tích được (biến lạ, base64, script tải về, thay thế lệnh…) → deny-by-default:
                  xử lý NHƯ phá huỷ (mở approval, không chạy).
Thêm cờ `forbidden`: agent tự duyệt/đụng socket điều khiển hub → từ chối thẳng (không mở approval), `guard.blocked`.

Bước 2 của BRD (LLM phụ cho `uncertain`): KHÔNG ở đây (hàm thuần) mà ở daemon — `guard/second_opinion.py`, gọi từ
`GuardService.check`, tắt mặc định (`guard.second_opinion.enabled`, chỉ người bật). Nó chỉ được HẠ `uncertain` → safe/write;
không bao giờ chạm destructive/forbidden/tài nguyên bảo vệ. Luật "luôn cho phép" người tạo khi duyệt (`approve --always`) được
tra ở `classify()` qua `GuardContext.always_allow` (chỉ khớp chính xác dấu vân tay thao tác, chỉ hạ `uncertain`).
"""

from __future__ import annotations

import base64
import binascii
import glob as _glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import names as _names
from ..oscompat import IS_WINDOWS, no_window_kwargs
from . import shell
from .policy import Policy, ProtectedResource, canon_path, fold_path

SEVERITY = {"safe": 0, "write": 1, "uncertain": 2, "destructive": 3}

READ_ONLY_TOOLS = {
    "Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "TodoWrite", "TodoRead", "NotebookRead", "BashOutput",
    "TaskOutput", "ToolSearch", "ListMcpResourcesTool", "ReadMcpResourceTool", "ExitPlanMode", "EnterPlanMode",
    "AskUserQuestion", "Monitor", "Skill",
}
SUBAGENT_TOOLS = {"Task", "Agent", "TaskStop", "KillShell", "KillBash", "SendMessage"}
FILE_WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

READ_ONLY_CMDS = {
    "ls", "cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "ag", "ack", "wc", "stat", "file",
    "pwd", "echo", "printf", "true", "false", ":", "test", "[", "[[", "which", "whereis", "type", "whoami", "id",
    "date", "cal", "uname", "hostname", "env", "printenv", "diff", "cmp", "comm", "sort", "uniq", "cut", "tr", "awk",
    "jq", "yq", "column", "nl", "od", "xxd", "hexdump", "strings", "md5", "md5sum", "sha1sum", "sha256sum", "shasum",
    "basename", "dirname", "realpath", "readlink", "du", "df", "ps", "top", "uptime", "free", "tree", "fd", "locate",
    "man", "help", "history", "seq", "sleep", "wait", "jobs", "fg", "bg", "cd", "pushd", "popd", "dirs", "export",
    "set", "unset", "alias", "unalias", "local", "declare", "typeset", "readonly", "read", "shift", "return", "exit",
    "source", ".", "tput", "clear", "lsof", "netstat", "ping", "dig", "nslookup", "host", "sw_vers", "defaults",
    "pbpaste", "say", "column", "fold", "fmt", "rev", "tac", "paste", "join", "expand", "unexpand", "look", "zcat",
    "bzcat", "xzcat", "gzcat", "tldr", "getconf", "locale", "ulimit", "umask", "hash", "times", "trap", "let",
    "expr", "bc", "dc", "factor", "yes", "nproc", "arch", "sysctl", "vm_stat", "iostat", "mdfind", "mdls",
    "otool", "nm", "objdump", "lipo", "codesign", "spctl", "xattr", "plutil", "sqlite3_analyzer", "tsort",
}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ash", "mksh", "csh", "tcsh", "busybox"}
INTERPRETERS = {"python", "python2", "python3", "pypy", "pypy3", "node", "nodejs", "deno", "bun", "perl", "ruby", "php", "osascript", "Rscript", "lua", "tclsh", "pwsh", "powershell"}
DB_CLIENTS = {
    "psql", "pgcli", "mysql", "mariadb", "mycli", "sqlite3", "sqlite", "litecli", "duckdb", "mongo", "mongosh",
    "redis-cli", "clickhouse", "clickhouse-client", "cqlsh", "sqlcmd", "snowsql", "bq", "cockroach", "influx", "usql",
}
CLOUD_CLIS = {"aws", "gcloud", "gsutil", "az", "doctl", "flyctl", "fly", "heroku", "vercel", "netlify", "firebase", "supabase", "gh", "glab", "linode-cli", "oci", "ibmcloud", "wrangler", "railway", "render"}
DELETE_VERBS = re.compile(r"^(delete|delete-.*|.*-delete|rm|rb|rmdir|destroy|remove|remove-.*|purge|terminate|terminate-.*|deprovision|wipe|erase|drop|drop-.*|uninstall|nuke)$", re.I)
WRAPPERS_SIMPLE = {"nohup", "time", "builtin", "stdbuf", "chronic", "caffeinate", "unbuffer", "setsid", "noglob", "nocorrect", "torsocks", "proxychains", "proxychains4", "doas", "sudo", "nice", "ionice", "timeout", "gtimeout", "command", "exec", "env", "arch", "xcrun", "then", "else", "do", "!", "if", "elif", "while", "until", "time"}
CRITICAL_DIRS = {"/", "/bin", "/sbin", "/usr", "/etc", "/var", "/System", "/Library", "/Applications", "/Users", "/home", "/opt", "/private", "/private/etc", "/private/var", "/boot", "/lib", "/lib64", "/root", "/Volumes", "/dev"}
TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/dev/shm/")
# Windows: gốc ổ đĩa + thư mục hệ thống (so trên fold_path); thư mục tạm của user (Git Bash `/tmp` trỏ vào đây)
WIN_CRITICAL_DIRS = {"c:/windows", "c:/windows/system32", "c:/program files", "c:/program files (x86)", "c:/programdata", "c:/users"}
_WIN_DRIVE_ROOT = re.compile(r"^[a-z]:/?$")
# đường dẫn Windows có `\` viết trong lệnh bash (`C:\Users\x`, `C:\\x`) — Git Bash ăn `\` ngoài quote, nhưng ý định của
# agent là đường dẫn Windows → phân loại theo đường dẫn đó (đổi `\` → `/`) thay vì chuỗi đã bị bash làm hỏng (bảo thủ)
_WIN_BS_PATH = re.compile(r"(?<![\w$])[A-Za-z]:\\[^\s'\"`;|&<>(){}]*")
SPECIAL_FILES = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero", "/dev/fd/")
DISK_DEV = re.compile(r"^/dev/(r?disk\d|sd[a-z]|nvme|hd[a-z]|mmcblk|xvd|vd[a-z]|md\d|dm-)")
DELETE_CMDS = {"rm", "unlink", "shred", "srm", "wipe", "gshred", "grm", "trash", "trash-put", "rmtrash"}
HUB_FORBIDDEN_SUB = {"approve", "deny"}
HUB_DESTRUCTIVE_SUB = {"guard", "config", "secret", "secrets", "estop", "mcp", "purge", "trash", "audit", "policy"}
# H-01/H-05: lệnh CLI ứng với method RPC CHỈ-NGƯỜI (auth.HUMAN_ONLY) — từ trong run luôn bị daemon từ chối → chặn thẳng
HUB_HUMAN_ONLY_CLI: dict[str, set[str] | None] = {
    "approve": None, "deny": None, "agent-purge": None, "agent-clone": None, "agent-import": None, "run-revert": None, "takeover": None,
    "approvals": {"approve", "deny"},
    "agent": {"create", "update", "edit", "rm", "remove", "delete", "purge", "restore", "clone", "import", "archive", "revert", "pause", "resume"},
    "trash": {"restore", "purge", "empty", "rm"},  # V2-N1
    "task": {"archive"},
    "learn": {"propose", "rollback", "retry", "refine", "policy", "pin", "unpin", "review-cancel", "review_cancel", "cancel"},
    "knowledge": {"pin", "unpin"},
    "golden": {"add", "rm", "remove", "edit", "update", "from-run", "from_run", "restore", "dismiss"},
    "guard": {"protect", "integrity", "sandbox", "repo-deny", "repo_deny", "always"},
    "config": {"set", "unset"},
    "secret": {"set", "rm", "remove", "delete"},
    "secrets": {"set", "rm", "remove", "delete"},
    "mcp": {"add", "edit", "update", "rm", "remove", "restore", "import", "assign", "unassign", "token", "agent-config"},
    "estop": {"on", "off", "set", "clear"},
    "team": {"create", "edit", "update", "archive", "restore", "member", "estop", "rm"},
    "run": {"revert", "takeover"},
    "runs": {"revert"},
    "curation": {"rollback"},
    "daemon": {"stop", "restart"},
    "auth": None,
    "notify": {"telegram"},  # token bot / allowlist chat / gửi tin thay người (chat/rpc.py)
    "answer": None,  # UX: trả lời câu hỏi = quyết định của người (agent không tự trả lời câu hỏi của mình/agent khác)
    "questions": {"answer", "escalate", "cancel"},
    "web": None,  # WEB: `cfharness web` sinh link đăng nhập = phiên NGƯỜI trên giao diện web
    "manager": {"set", "chat", "tick", "capabilities"},  # M7: chỉ định/ra lệnh điều phối viên + khung quyền §9 = quyền người (manager.* ∈ HUMAN_ONLY)
    "chat": None,  # M7 terminal: `cfharness chat` = ra lệnh điều phối viên (manager.chat ∈ HUMAN_ONLY)
    "system": {"scan", "add", "rm", "lock", "unlock", "pause", "resume", "restart"},  # job launchd chạy NGOÀI sandbox (system.* ∈ HUMAN_ONLY)
    "home": {"migrate"},  # đổi tên cfharness: chuyển HUB_HOME (dừng daemon, đổi thư mục, viết lại DB) — chỉ người
    # Lịch chạy tự động (schedules/): schedule.* ghi ∈ HUMAN_ONLY — agent không tự đặt việc chạy lặp
    "schedule": {"add", "create", "edit", "update", "pause", "resume", "rm", "remove", "delete", "run-now", "run_now"},
}

# H-03: API dạng phương thức (`Path(..).unlink()`) đứng sau `)`/`]` nên KHÔNG dùng `\b` trước dấu chấm
PY_DESTRUCTIVE = re.compile(
    r"(\brmtree\b|\bos\.(remove|unlink|rmdir|removedirs|truncate|ftruncate)\b|\.unlink\s*\(|\.rmdir\s*\(|\bsend2trash\b|\btruncate\s*\(|"
    r"\.truncate\s*\(\s*0?\s*\)|\bshutil\.rmtree\b|\bos\.system\s*\(\s*['\"]\s*rm\b)", re.I)
JS_DESTRUCTIVE = re.compile(
    r"(\brmSync\b|\brmdirSync\b|\bunlinkSync\b|\bfs\.rm\b|\bfs\.unlink\b|\bfs\.rmdir\b|\bfsp?\.rm\s*\(|\bfs\.promises\.(rm|unlink|rmdir)\b|"
    r"\brimraf\b|\bdel\s*\(|fs-extra.*remove|\bemptyDir|\btruncateSync\b|\bfs\.truncate\b|\bDeno\.remove\b|\.delete\s*\(\s*\))", re.I)
PL_DESTRUCTIVE = re.compile(r"\b(unlink|rmtree|remove_tree|rmdir)\b|FileUtils\.(rm|rm_r|rm_rf|remove|remove_dir|remove_entry|rm_f)|File\.(delete|unlink)|Dir\.(delete|rmdir)|\brm_rf\b|do shell script|\bftruncate\b|File\.truncate", re.I)
JS_TEMPLATE = re.compile(r"`([^`$\\]*)`")
# H-01: đụng mặt điều khiển hub từ mã/lệnh → forbidden
# Tên mới (cfharness) LẪN tên cũ (cfharness) — `names.py`, nhận diện vĩnh viễn
_SOCK_PFX = _names.alt(_names.HUB_SOCKET_PREFIXES)
_MOD = _names.alt(_names.HUB_MODULE_NAMES)
_CLI = _names.alt(_names.HUB_CLI_NAMES)
_ENV_HOME = _names.alt(_names.env_names("HOME"))
_HOME_DIR = _names.alt(_names.HUB_HOME_DIRNAMES)
HUB_SOCKET_RE = re.compile(rf"hub\.sock|{_SOCK_PFX}[^/\s'\"]*\.sock|\b{_names.alt(_names.env_names('SOCKET'))}\b|\.sock\.auth\b|human\.token|mcp_server/tokens\.json", re.I)
HUB_MODULE_RE = re.compile(rf"(\bimport\s+{_MOD}\b|\bfrom\s+{_MOD}\b|\b{_MOD}\.(rpc|cli|daemon|auth|approvals|guard|mcp_server|learning|evals|hub)\b|__import__\(\s*['\"]{_MOD}|import_module\(\s*['\"]{_MOD}|require\(['\"][^'\"]*{_CLI})")
HUB_HOME_REF_RE = re.compile(rf"\b{_ENV_HOME}\b|{_HOME_DIR}\b")
# R3-1: đọc thông tin xác thực từ keychain / credential helper / gh (sandbox KHÔNG chặn Mach IPC tới SecurityServer)
KEYCHAIN_CODE_RE = re.compile(r"SecKeychain\w*|SecItemCopyMatching|SecItemCopy|kSecClass|Security\.framework|/System/Library/Frameworks/Security|"
                              r"\bkeyring\.get_password|\bkeytar\b|find-(generic|internet)-password|dump-keychain|credential-osxkeychain|"
                              r"\bgit\s+credential\b|gh\s+auth\s+token", re.I)
SECURITY_READ_SUBS = re.compile(r"^(find-(generic|internet)-password|find-certificate|find-identity|find-key|dump-keychain|export|"
                                r"export-key|unlock-keychain|set-keychain-settings|show-keychain-info|dump-trust-settings|authorizationdb|"
                                r"set-generic-password-partition-list|set-key-partition-list|add-(generic|internet)-password)$")
# WT-3: dấu hiệu đường dẫn trỏ vào vùng nhạy cảm của HUB_HOME (dùng cho đích động không giải được)
SENSITIVE_HINT = re.compile(rf"(^|/)(auth|secrets)(/|$)|/(web|tokens)\.json$|\.sock(\.auth)?$|/hub\.db|{_ENV_HOME}|{_HOME_DIR}")
UNIX_SOCKET_RE = re.compile(r"\bAF_UNIX\b|UNIX-CONNECT|UNIX-CLIENT|\bcreateConnection\s*\(\s*\{?\s*path|\bnet\.connect\s*\(\s*\{?\s*path|\bnet\.connect\s*\(\s*['\"]/|UNIXSocket|socket\.AF_UNIX|IO::Socket::UNIX|\bunix:/", re.I)
LIST_ARGV_RE = re.compile(r"""\[\s*((?:(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*,\s*)*(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'))\s*,?\s*\]""")
JS_SPAWN_RE = re.compile(r"""\b(?:spawn|spawnSync|execFile|execFileSync|Deno\.Command|Bun\.spawn)\s*\(\s*(["'])([^"']+)\1\s*,\s*\[([^\]]*)\]""")
_L = r"""(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')"""
# Windows: lệnh/cmdlet tạo lịch chạy, dịch vụ, mục khởi động hoặc tiến trình qua WMI — chạy NGOÀI Job Object của run (tương đương
# `launchctl submit` thoát sandbox trên macOS), như người, cả sau khi run kết thúc. Dùng cho mã PowerShell/Python… và chuỗi lệnh.
WIN_PERSIST_CODE = re.compile(
    r"(?i)\b(Register-ScheduledTask|New-ScheduledTask(Action|Trigger|Principal|Settings(Set)?)?|Set-ScheduledTask|Start-ScheduledTask|"
    r"Enable-ScheduledTask|Disable-ScheduledTask|Unregister-ScheduledTask|Stop-ScheduledTask|New-Service|Set-Service|Start-Service|Remove-Service|"
    r"Invoke-WmiMethod|Invoke-CimMethod|Set-WmiInstance|New-CimInstance|Register-WmiEvent|Register-CimIndicationEvent)\b"
    r"|\bschtasks(\.exe)?\s+[/-](create|change|run|delete|end)\b"
    r"|\bsc(\.exe)?\s+(create|config|start|stop|delete|failure)\b"
    r"|\bwmic(\.exe)?\b.*\bcall\s+create\b"
    r"|\bStart-Process\b.*-Verb\s+RunAs\b"
    r"|\breg(\.exe)?\s+(add|delete|import|restore|load)\b"
    r"|\b(Set|New)-ItemProperty\b.*\\(Run|RunOnce|Winlogon)\b")
WIN_ESCAPE = "tiến trình do Task Scheduler/dịch vụ/WMI tạo nằm NGOÀI Job Object của run (như launchctl submit thoát sandbox)"
EXEC_DYNAMIC = re.compile(r"\b(os\.system|subprocess|popen|os\.exec|execSync|exec\(|spawn|child_process|system\(|eval\(|`|__import__|b64decode|base64|codecs\.decode|fromCharCode|Function\()", re.I)
STR_LITERAL = re.compile(r"""(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:|(\w+)\s*\(\s*\)\s*\{\s*\1\s*\|\s*\1\s*&\s*\}")
OBFUSCATION = re.compile(r"\b(base64\s+(-d|--decode|-D)|openssl\s+(enc|base64)\s.*-d|xxd\s+-r|uudecode|rev\s*\|)", re.I)

MCP_DELETE_TOKENS = {"delete", "del", "remove", "rm", "purge", "drop", "destroy", "wipe", "erase", "truncate", "xoa", "xoá", "xóa", "huy", "huỷ", "hủy", "gỡ", "gobo", "unlink", "trash", "clear", "reset"}
MCP_DESC_RE = re.compile(r"\b(delete[sd]?|deleting|remove[sd]?|removing|purge[sd]?|drop(s|ped)?|destroy(s|ed)?|wipe[sd]?|erase[sd]?|permanently)\b|xoá|xóa|huỷ|hủy|gỡ bỏ", re.I)
MCP_FORBIDDEN = re.compile(r"^(hub_)?(approve|deny|approval_resolve|policy_set|protect_(add|rm|remove)|golden_(add|rm|remove|edit|update|from_run|restore))$")  # M4-B: golden = chính sách an toàn (Q-07)
# H-18: tool của MCP server CỦA HUB (nhận theo lệnh/URL, không theo tên đăng ký) ứng với method chỉ-người
HUB_MCP_HUMAN_ONLY = re.compile(
    r"^(hub_)?(approve|deny|agent_(create|update|archive|delete|purge|restore)|estop|config_set|secret_\w+|mcp_(add|update|remove|assign)|"
    r"learn_(propose|rollback|retry|refine|policy_set)|knowledge_pin|team_(create|update|archive|member_set|member_rm|estop)|golden_(add|rm|remove|edit|update|from_run|restore|dismiss)|takeover|run_revert)$")


# ============================================================================ data
@dataclass
class Verdict:
    verdict: str = "safe"
    rule: str = "default"
    klass: str | None = None  # file | db | mcp | git | hub | system | policy
    reason: str = ""
    targets: list[str] = field(default_factory=list)
    protected_id: str | None = None
    forbidden: bool = False
    net_urls: list[str] = field(default_factory=list)  # WEB-1: URL curl/wget trong lệnh Bash — daemon kiểm SSRF/DNS/domain

    @property
    def hold(self) -> bool:
        return self.verdict in ("destructive", "uncertain") and not self.forbidden

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict, "rule": self.rule, "klass": self.klass, "reason": self.reason,
            "targets": self.targets[:20], "protected_id": self.protected_id, "forbidden": self.forbidden,
        }


@dataclass
class GuardContext:
    cwd: Path
    policy: Policy
    run_since: float | None = None  # epoch giây: mọi file có birthtime ≥ mốc này là "file run tự tạo"
    mcp_annotations: Callable[[str, str], dict[str, Any] | None] | None = None
    hub_socket: str | None = None
    git_probe: bool = True  # gọi `git` để biết thay đổi chưa commit/nhánh đã push (tắt trong test thuần)
    max_walk: int = 3000
    hub_mcp_servers: frozenset[str] = frozenset()  # H-18: tên đăng ký của MCP server trỏ về chính hub (theo lệnh/URL)
    checkpoint_root: str | None = None  # gốc repo git có checkpoint của run (FR-RT-12) → file đã track ghi đè/xoá được revert
    manager_run: bool = False  # MGR-8: run của agent điều phối → mọi verdict policy/hub/guard = forbidden (không phải approval)
    repo_deny: tuple[str, ...] = ()  # MGR-2: mẫu disallowedTools theo repo (người đặt) áp cho run có cwd trong repo
    extra_write_roots: tuple[str, ...] = ()  # R2-1: thư mục người cấp thêm cho tool ghi (guard.sandbox.extra_allow_write, add-dir)
    repo_deny_lookup: Callable[[str], list[str]] | None = None  # R2-2: mẫu chặn của repo ĐÍCH (git -C, npm --prefix, cd …)
    agent_id: str | None = None  # agent của run (luật "luôn cho phép" theo agent)
    always_allow: tuple[dict[str, Any], ...] = ()  # luật người tạo khi duyệt `--always` (policy/always_allow.yaml), chỉ hạ uncertain
    # run điều phối viên có connector claude.ai (manager.capabilities.connectors): slug connector được phép; None = không phải
    # run connector. Run connector nạp cả MCP của ~/.claude → server ngoài `run_mcp_servers` + connector cho phép = forbidden
    connectors: tuple[str, ...] | None = None
    run_mcp_servers: frozenset[str] = frozenset()

    @property
    def home(self) -> str:
        return canon_path(str(self.policy.user_home))


def _max(a: Verdict, b: Verdict) -> Verdict:
    if b.forbidden and not a.forbidden:
        return b
    if a.forbidden:
        return a
    return b if SEVERITY[b.verdict] > SEVERITY[a.verdict] else a


def _linux_statx_btime():
    """Linux: `os.stat` của Python KHÔNG có st_birthtime → đọc btime qua statx(2) của glibc (≥2.28). None nếu không có."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fn = libc.statx
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
    fn.restype = ctypes.c_int
    AT_FDCWD, AT_SYMLINK_NOFOLLOW, STATX_BTIME = -100, 0x100, 0x800

    def btime(path: str) -> float | None:
        buf = ctypes.create_string_buffer(256)  # struct statx
        if fn(AT_FDCWD, os.fsencode(path), AT_SYMLINK_NOFOLLOW, STATX_BTIME, buf) != 0:
            return None
        mask = int.from_bytes(buf.raw[0:4], sys.byteorder)
        if not mask & STATX_BTIME:
            return None  # hệ tệp không ghi thời điểm tạo
        sec = int.from_bytes(buf.raw[80:88], sys.byteorder, signed=True)  # stx_btime.tv_sec
        nsec = int.from_bytes(buf.raw[88:92], sys.byteorder)
        return sec + nsec / 1e9

    return btime


_STATX_BTIME = _linux_statx_btime()


def _birth(path: str, st: os.stat_result) -> float | None:
    """Thời điểm TẠO của `path` (lstat). None = không biết → người gọi coi như KHÔNG phải file run tạo (bảo thủ).
    KHÔNG dùng st_ctime trên POSIX: ctime đổi khi ghi/chmod/rename/touch → file có sẵn trông như "run vừa tạo" (Linux trước đây:
    `echo >> f; rm f` hoặc `mv bak f; rm -rf f` lọt không cần duyệt). Windows: st_ctime = thời điểm tạo (Python < 3.12)."""
    b = getattr(st, "st_birthtime", None)
    if b:
        return b
    if IS_WINDOWS:
        return st.st_ctime
    return _STATX_BTIME(path) if _STATX_BTIME is not None else None


def created_by_run(path: str, ctx: GuardContext) -> bool:
    """Đích là "file run tự tạo" (FR-SG-01): tồn tại và MỌI phần tử có thời điểm tạo ≥ mốc bắt đầu chuỗi run.
    Không có mốc (vd `guard test`) → False (bảo thủ). Đích không tồn tại → True (xoá cái không có = vô hại)."""
    if not os.path.lexists(path):
        # xoá cái không tồn tại là vô hại — nhưng chỉ tin điều đó trong workspace của run hoặc /tmp
        cwd = canon_path(str(ctx.cwd)).rstrip("/")
        return bool(_rel_parts(path, cwd)) or _is_tmp(path)
    if ctx.run_since is None:
        return False
    since = ctx.run_since - 1.0
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    b = _birth(path, st)
    if b is None or b < since:
        return False
    if not os.path.isdir(path) or os.path.islink(path):
        return True
    n = 0
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            n += 1
            if n > ctx.max_walk:
                return False
            try:
                fp = os.path.join(root, name)
                b = _birth(fp, os.lstat(fp))
                if b is None or b < since:
                    return False
            except OSError:
                return False
    return True


# ============================================================================ entry
def classify(tool_name: str, tool_input: dict[str, Any] | None, ctx: GuardContext) -> Verdict:
    v = _classify(tool_name, tool_input, ctx)
    # MGR-8: run của agent điều phối (manager) — thao tác chính sách/hub/guard là CẤM thẳng, không phải approval chờ người
    if ctx.manager_run and not v.forbidden and (v.klass in ("policy", "hub", "guard") or v.rule in ("policy_write", "protected_write") and v.protected_id == "hub-policy"):
        return Verdict("destructive", v.rule, v.klass, f"điều phối viên không được chạm chính sách/hub an toàn ({v.reason})",
                       v.targets, v.protected_id, forbidden=True)
    # §9 (manager.capabilities hermes): điều phối viên có Bash/Write/Web — ghi dữ liệu nội bộ hub (config.yaml, hub.db, agents/*, mã hub…),
    # gọi web/dịch vụ loopback (web cfharness, MCP HTTP của hub), chạy `claude` lồng = CẤM thẳng, không mở approval (tránh xin người
    # duyệt một thao tác nâng quyền của chính nó). Agent thường: các ca này vẫn giữ chờ người như cũ.
    if ctx.manager_run and not v.forbidden and v.verdict != "safe" and (
            (v.protected_id or "").startswith("hub-") or v.rule in ("nested_claude",) or v.rule in NET_BLOCK_RULES or tool_name in ("Task", "Agent")
            or any(_MGR_SELF_CFG.search(str(t)) for t in v.targets)):
        return Verdict("destructive", v.rule, v.klass or "hub", f"điều phối viên không được làm việc này ({v.reason})",
                       v.targets, v.protected_id, forbidden=True, net_urls=v.net_urls)
    # luật "luôn cho phép" do NGƯỜI tạo khi duyệt (`approve --always`): chỉ HẠ uncertain đủ điều kiện, khớp chính xác dấu vân tay + agent
    if ctx.always_allow and always_eligible(v):
        rule = match_always(tool_name, tool_input, ctx)
        if rule is not None:
            return Verdict("write", "always_allow", v.klass, f"luật luôn-cho-phép {rule.get('id')} (người duyệt {rule.get('approval_id') or '-'}); "
                           f"phân loại gốc: {v.rule}", v.targets, None, net_urls=v.net_urls)
    return v


# tuỳ chọn curl/wget/httpie nhận giá trị (giá trị đó không phải URL đích)
_NET_ARG_FLAGS = frozenset({"-o", "--output", "-O", "--output-document", "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F",
                            "--form", "-T", "--upload-file", "-H", "--header", "-u", "--user", "-A", "--user-agent", "-e", "--referer", "-b",
                            "--cookie", "-c", "--cookie-jar", "-K", "--config", "-P", "--directory-prefix", "-x", "--proxy", "--resolve",
                            "--connect-to", "-w", "--write-out", "--cacert", "--cert", "--key", "-m", "--max-time", "-a", "--append-output",
                            "-i", "--input-file", "--header-file"})
# WEB-1: luật mạng — run manager CẤM thẳng (không mở approval); agent thường giữ chờ người (trừ metadata: cấm với mọi agent)
NET_BLOCK_RULES = frozenset({"webfetch_local", "ssrf_metadata", "ssrf_private_ip", "ssrf_internal_name", "ssrf_private_resolved",
                             "url_scheme", "url_credentials", "url_no_host", "url_unparsable"})
# không bao giờ cho "luôn cho phép"/LLM hạ cấp: luật mạng/SSRF, domain người chặn/ngoài allowlist (chính sách của người), MCP người
# đánh dấu cần duyệt, lỗi bộ phân loại/không phân tích được đích, run không hoạt động/ESTOP
NEVER_DOWNGRADE_RULES = NET_BLOCK_RULES | frozenset({
    "url_dns_failed", "web_domain_denied", "web_domain_not_allowed", "mcp_require_approval", "classifier_error", "unknown_run", "estop",
    "nesting_too_deep", "hub_secret_read", "self_approval",
})
NEVER_DOWNGRADE_KLASS = frozenset({"hub", "policy", "guard"})


def always_eligible(v: Verdict) -> bool:
    """`uncertain` có được hạ bởi luật luôn-cho-phép / LLM thứ hai không. Destructive/forbidden/tài nguyên bảo vệ/vùng hub → KHÔNG."""
    return (v.verdict == "uncertain" and not v.forbidden and not v.protected_id and (v.klass or "") not in NEVER_DOWNGRADE_KLASS
            and v.rule not in NEVER_DOWNGRADE_RULES)


def match_always(tool_name: str, tool_input: dict[str, Any] | None, ctx: GuardContext) -> dict[str, Any] | None:
    """Luật `exact`: cùng agent + cùng tool + cùng `action_fingerprint` (lệnh đã chuẩn hoá). Không có wildcard."""
    fp = None
    for r in ctx.always_allow:
        if not isinstance(r, dict) or r.get("kind") != "exact" or r.get("disabled"):
            continue
        if r.get("agent_id") and r.get("agent_id") != ctx.agent_id:
            continue
        if r.get("tool") != tool_name:
            continue
        fp = fp or action_fingerprint(tool_name, tool_input)
        if r.get("fingerprint") == fp:
            return r
    return None


# §9: file cấu hình/bộ nhớ mà `claude` có thể nạp ở lượt sau (tự cài chỉ dẫn/hook/MCP cho chính điều phối viên)
_MGR_SELF_CFG = re.compile(r"(^|/)(CLAUDE(\.local)?\.md|\.mcp\.json|\.claude(/|$))", re.I)
_NUMERIC_HOST_RE = re.compile(r"^[0-9a-fx.:]+$", re.I)


def local_host(host: str | None) -> bool:
    """Host loopback/mạng nội bộ (web cfharness `*.localhost`, MCP HTTP 127.0.0.1, dịch vụ LAN) — kể cả dạng IP số/hex/bát phân."""
    import ipaddress

    if not host:
        return False
    h = host.strip("[]").rstrip(".").lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        try:
            ip = ipaddress.ip_address(int(h, 0))
        except (ValueError, TypeError):
            # dạng số lạ (0177.0.0.1, 0x7f.1 …) mà trình phân giải vẫn hiểu là IP → coi là nội bộ (bảo thủ)
            return bool(_NUMERIC_HOST_RE.match(h)) and any(c.isdigit() for c in h)
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved
    h = host.strip("[]").rstrip(".").lower()
    if h == "localhost" or h.endswith(".localhost") or h in ("0", "0.0.0.0", "::", "::1"):
        return True
    for cand in (h,):
        try:
            ip = ipaddress.ip_address(cand)
        except ValueError:
            try:
                ip = ipaddress.ip_address(int(cand, 0))
            except (ValueError, TypeError):
                return bool(_PRIVATE_HOST_RE.match(h)) and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", h)
        return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved
    return False


def _classify(tool_name: str, tool_input: dict[str, Any] | None, ctx: GuardContext) -> Verdict:
    ti = tool_input if isinstance(tool_input, dict) else {}
    try:
        if tool_name == "Bash":
            cmd = ti.get("command")
            if not isinstance(cmd, str):
                return Verdict("uncertain", "bash_no_command", "system", "Bash không có command dạng chuỗi")
            ba = BashAnalyzer(ctx)
            bv = ba.run(cmd)
            urls = list(dict.fromkeys(ba.state.get("net_urls") or []))[:20]
            if urls:
                bv = Verdict(bv.verdict, bv.rule, bv.klass, bv.reason, bv.targets, bv.protected_id, bv.forbidden, urls)
            return bv
        if tool_name.startswith("mcp__"):
            return classify_mcp(tool_name, ti, ctx)
        if tool_name in FILE_WRITE_TOOLS:
            return classify_file_write(tool_name, ti, ctx)
        if tool_name == "WebFetch":
            from urllib.parse import urlsplit

            from .url_safety import static_check

            url = str(ti.get("url") or "")
            try:
                host = urlsplit(url if "//" in url else "//" + url).hostname
            except ValueError:
                host = None
            if host is None or local_host(host):
                chk = static_check(url)
                if chk.metadata:
                    return Verdict("destructive", "ssrf_metadata", "network", f"WebFetch tới metadata đám mây ({chk.host})", [url[:200]], forbidden=True)
                if not chk.ok and chk.rule.startswith("url_"):
                    return Verdict("uncertain", chk.rule, "network", f"WebFetch: {chk.reason}", [url[:200]])
                return Verdict("uncertain", "webfetch_local", "network", f"WebFetch tới host nội bộ/loopback ({host or url[:80]}) — web/MCP của hub, dịch vụ LAN", [url[:200]])
            # WEB-1 (SSRF, ý tưởng Hermes url_safety): scheme, thông tin xác thực trong URL, metadata, tên nội bộ, IP dạng lạ.
            # Phân giải DNS + danh sách domain: ở daemon (GuardService), không ở hàm thuần này.
            chk = static_check(url)
            if not chk.ok:
                if chk.metadata:
                    return Verdict("destructive", "ssrf_metadata", "network", f"WebFetch tới metadata đám mây: {chk.reason}", [url[:200]], forbidden=True)
                return Verdict("uncertain", chk.rule, "network", f"WebFetch: {chk.reason}", [url[:200]])
        if tool_name in READ_ONLY_TOOLS:
            for k in ("file_path", "path", "notebook_path"):
                v = ti.get(k)
                if isinstance(v, str) and v and sensitive_read(_abs(v, ctx.cwd), ctx):
                    return Verdict("destructive", "hub_secret_read", "hub", "agent không được đọc token người/secret/mcp.json của hub", [v], forbidden=True)
            return Verdict("safe", "read_only_tool")
        if tool_name in SUBAGENT_TOOLS:
            return Verdict("write", "subagent_tool", reason="subagent/điều khiển task — tool con của nó đi qua hook riêng")
        toks = _name_tokens(tool_name)
        if toks & MCP_DELETE_TOKENS:
            return Verdict("destructive", "tool_name_delete", "file", f"tên tool '{tool_name}' mang nghĩa xoá")
        return Verdict("write", "unknown_tool", reason=f"tool '{tool_name}' chưa có trong danh mục → coi là ghi (có audit)")
    except Exception as e:  # lỗi bộ phân loại → deny-by-default (FR-SG-03 bước 3)
        return Verdict("uncertain", "classifier_error", None, f"lỗi bộ phân loại: {e!r}")


_WIN_ABS = re.compile(r"^[A-Za-z]:/")


def _isabs(p: str) -> bool:
    """Windows (Git Bash): `/x` (gốc MSYS), `/c/x`, `C:/x`, `C:\\x` đều tuyệt đối (ntpath 3.13 coi `/x` là tương đối)."""
    if IS_WINDOWS:
        q = canon_path(p)
        return q.startswith("/") or bool(_WIN_ABS.match(q))
    return os.path.isabs(p)


def _norm(p: str) -> str:
    return canon_path(os.path.normpath(p)) if IS_WINDOWS else os.path.normpath(p)


def _abs(p: str, cwd: Path | str) -> str:
    p = os.path.expanduser(p)
    if IS_WINDOWS:
        p = canon_path(p)
        return p if _isabs(p) else canon_path(canon_path(str(cwd)).rstrip("/") + "/" + p)
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(str(cwd), p))


def _real(p: str) -> str:
    if IS_WINDOWS:  # realpath: tên dài (bỏ 8.3 `LEQUYP~1`), đúng hoa/thường của phần đã tồn tại, giải symlink/junction
        q = canon_path(p)
        if not _WIN_ABS.match(q):
            return q  # `/dev/null`, `/data/x` (gốc MSYS) — không có trên ổ đĩa theo dạng này
        try:
            return canon_path(os.path.realpath(q))
        except (OSError, ValueError):
            return q
    try:
        return os.path.realpath(p)
    except OSError:
        return p


def _hub_home(ctx: GuardContext) -> str:
    return canon_path(str(ctx.policy.hub_home)).rstrip("/")


def _under(p: str, base: str) -> bool:
    if IS_WINDOWS:
        p, base = fold_path(p), fold_path(base)
    base = base.rstrip("/")
    return p == base or p.startswith(base + "/")


def _same(a: str, b: str) -> bool:
    return fold_path(a) == fold_path(b) if IS_WINDOWS else a == b


def _rel_parts(p: str, base: str) -> list[str]:
    """Các phần của `p` NẰM DƯỚI `base` (không tính chính `base`); [] nếu không nằm dưới."""
    if not _under(p, base) or _same(p.rstrip("/"), base.rstrip("/")):
        return []
    return canon_path(p)[len(canon_path(base).rstrip("/")) + 1 :].split("/")


def _contains(hay: str, needle: str) -> bool:
    """`needle` (đường dẫn) xuất hiện trong chuỗi lệnh/mã — Windows: cả dạng `\\` lẫn `/`, không phân biệt hoa/thường."""
    if not IS_WINDOWS:
        return needle in hay
    h = hay.replace("\\", "/").lower()
    return any(n in h for n in {needle.replace("\\", "/").lower(), canon_path(needle).lower()})


def _is_tmp(p: str) -> bool:
    """Thư mục tạm "vô hại" — chỉ tiền tố literal `/tmp/`… (Windows: KHÔNG tính %TEMP% thật, như macOS không tính $TMPDIR —
    workspace có thể nằm trong đó; bảo thủ)."""
    return p.startswith(TMP_PREFIXES)


def _tmp_scratch(p: str, ctx: GuardContext) -> bool:
    """Ghi đè file CÓ SẴN ở `p` được coi là nháp trong /tmp (không cần người) — chỉ khi cả đường dẫn literal lẫn realpath
    nằm trong /tmp VÀ không nằm trong workspace của run. Linux: dự án/repo nằm trong /tmp (clone vào `mktemp -d`, CI,
    thư mục tạm của pytest) là dữ liệu thật — trước đây `: > /tmp/…/repo/README.md` lọt thành `write` không cần duyệt;
    symlink `/tmp/x → ~/proj/f` cũng phải đi đường kiểm tra file có sẵn."""
    rp = _real(p)
    if not (_is_tmp(p) and _is_tmp(rp)):
        return False
    ws = {canon_path(str(ctx.cwd)), _real(str(ctx.cwd))}
    return not any(_under(c, w) for c in {p, rp} for w in ws if w)


def _critical(p: str, home: str) -> bool:
    """Thư mục hệ thống/home (xoá/mv/chmod -R = phá huỷ)."""
    if not IS_WINDOWS:
        return p in CRITICAL_DIRS or p == home
    f = fold_path(p).rstrip("/") or "/"
    return (f in {d.lower() for d in CRITICAL_DIRS} or f in WIN_CRITICAL_DIRS or bool(_WIN_DRIVE_ROOT.match(f)) or _same(p, home))


def _jn(p: str) -> str:
    """Kết quả os.path.join: Windows → dạng canon (gạch ngược → `/`); POSIX giữ nguyên (không thêm normpath)."""
    return canon_path(os.path.normpath(p)) if IS_WINDOWS else p


def _win_slashes(script: str) -> str:
    return _WIN_BS_PATH.sub(lambda m: re.sub(r"\\+", "/", m.group(0)), script) if IS_WINDOWS else script


def _win_cred_dirs() -> tuple[str, ...]:
    """R3-1 Windows: nơi lưu thông tin xác thực của người (tương đương Keychain/.config/gh)."""
    if not IS_WINDOWS:
        return ()
    return ("AppData/Roaming/GitHub CLI", "AppData/Roaming/gh", "AppData/Local/GitCredentialManager", "AppData/Roaming/Microsoft/Credentials",
            "AppData/Local/Microsoft/Credentials", "AppData/Roaming/Microsoft/Protect", "AppData/Roaming/Microsoft/Crypto")


def sensitive_read(p: str, ctx: GuardContext) -> bool:
    """Đường dẫn mà agent KHÔNG được đọc: token người cạnh socket, secrets/, mcp.json đã resolve, token MCP HTTP."""
    homes = {_hub_home(ctx), _real(_hub_home(ctx))}
    cands = {canon_path(p), _real(p)}
    uh = canon_path(str(ctx.policy.user_home)).rstrip("/")
    for c in cands:  # R3-1: thông tin xác thực git/forge/keychain của người
        for x in (".config/gh", ".git-credentials", ".config/git/credentials", "Library/Keychains", ".netrc", ".ssh") + _win_cred_dirs():
            if _under(c, f"{uh}/{x}"):
                return True
    for c in cands:
        if fold_path(c).endswith(".sock.auth") or (os.path.basename(c).lower() if IS_WINDOWS else os.path.basename(c)) == "human.token":
            return True
        if ctx.hub_socket and _same(c, ctx.hub_socket + ".auth"):
            return True
        for h in homes:
            if _under(c, h + "/secrets") or _under(c, h + "/auth") or _same(c, h + "/mcp_server/tokens.json"):
                return True
            parts = [x.lower() for x in _rel_parts(c, h)] if IS_WINDOWS else _rel_parts(c, h)
            if len(parts) >= 3 and parts[0] == "agents" and parts[2] == "runtime":
                return True
    return False


def _python_env_dirs() -> tuple[str, ...]:
    import sys
    import sysconfig

    out = {_real(sys.prefix) + "/bin"}
    if IS_WINDOWS:  # venv Windows: Scripts/ (python.exe, cfharness.exe) + Lib/site-packages
        out |= {_real(sys.prefix) + "/Scripts", _real(sys.prefix) + "/Lib"}
    for k in ("purelib", "platlib", "scripts"):
        if k == "scripts" and not IS_WINDOWS:
            continue
        try:
            out.add(_real(sysconfig.get_paths()[k]))
        except KeyError:  # pragma: no cover
            pass
    return tuple(out)


def _persistence_dirs(home: str) -> tuple[str, ...]:
    """V2-N2: nơi đặt thứ CHẠY TRỄ như người, ngoài run/sandbox (cron/launchd/git hooks/autostart);
    MGR-1: + thư mục bin trên $PATH thuộc $HOME (người/daemon THỰC THI binary ngoài sandbox → giả lệnh = thoát sandbox)."""
    base = (f"{home}/Library/LaunchAgents", f"{home}/Library/LaunchDaemons", "/Library/LaunchAgents", "/Library/LaunchDaemons",
            f"{home}/.config/git", f"{home}/.config/systemd", f"{home}/.config/autostart", f"{home}/.git-templates",
            "/etc/cron.d", "/var/spool/cron", "/usr/lib/cron/tabs")
    try:
        from .runtime import path_bin_dirs

        return base + tuple(path_bin_dirs())
    except Exception:  # pragma: no cover
        return base


def protected_path(ctx: GuardContext, p: str, op: str) -> ProtectedResource | None:
    """Tài nguyên bảo vệ theo chính sách + các vùng mặc định cứng: dữ liệu nội bộ HUB_HOME (trừ workspace của agent),
    môi trường Python của hub (site-packages/bin), nơi đặt cơ chế chạy trễ (V2-N2), git hooks/config."""
    pol = ctx.policy
    hh = _hub_home(ctx)
    for cand in {p, _real(p)}:
        r = pol.match_path(cand, op)
        if r:
            return r
        for h in {hh, _real(hh)}:
            rel = _rel_parts(cand, h)
            if rel:
                if IS_WINDOWS:
                    rel = [x.lower() for x in rel]
                if not (len(rel) >= 3 and rel[0] == "agents" and rel[2] == "workspaces"):
                    return ProtectedResource("hub-home", "hub", h, note="dữ liệu nội bộ HUB_HOME (chỉ daemon ghi)", builtin=True)
        for d in _python_env_dirs():
            if _under(cand, d):
                return ProtectedResource("hub-python-env", "path", d, note="môi trường Python chạy hub/hook", builtin=True)
        z = exec_zone(cand)
        if z is not None:  # R2-1: vùng thực thi ngoài sandbox (PATH, venv, /opt/homebrew, repo hub, dotfile)
            return ProtectedResource("exec-zone", "path", z, note="vùng thực thi ngoài sandbox (R2-1)", builtin=True)
        for d in _persistence_dirs(canon_path(str(pol.user_home))):
            if _under(cand, d):
                return ProtectedResource("persistence", "path", d, note="cơ chế chạy trễ ngoài sandbox (V2-N2)", builtin=True)
        parts = canon_path(cand).split("/")
        lparts = [x.lower() for x in parts] if IS_WINDOWS else parts  # NTFS: `.GIT/Hooks` = `.git/hooks`
        if ".git" in lparts:
            i = lparts.index(".git")
            if lparts[i + 1 : i + 2] in (["hooks"], ["config"]):
                return ProtectedResource("git-hooks", "path", "/".join(parts[: i + 2]), note="git hooks/config chạy lệnh khi người dùng git (V2-N2)", builtin=True)
    return None


def exec_zone(p: str) -> str | None:
    """R2-1: đường dẫn nằm trong vùng THỰC THI ngoài sandbox (PATH, venv/site-packages của hub, /opt/homebrew, repo hub, dotfile)."""
    try:
        from .runtime import executable_zones

        zones = executable_zones()
    except Exception:  # pragma: no cover
        return None
    for c in {p, _real(p)}:
        for z in zones:
            if _under(c, z):
                return z
    return None


def write_roots(ctx: GuardContext) -> list[str]:
    """R2-1: nơi tool ghi file ĐƯỢC ghi không cần người — workspace của run + thư mục tạm (+ extra_allow_write/add-dir do người cấp)."""
    import tempfile

    roots = [canon_path(str(ctx.cwd)), _real(str(ctx.cwd)), "/tmp", "/private/tmp", _real(tempfile.gettempdir())]
    roots += [canon_path(r) for r in ctx.extra_write_roots]
    return [r.rstrip("/") for r in dict.fromkeys(roots) if r]


def classify_file_write(tool_name: str, ti: dict[str, Any], ctx: GuardContext) -> Verdict:
    key = FILE_WRITE_TOOLS[tool_name]
    p = ti.get(key)
    if not isinstance(p, str) or not p:
        return Verdict("uncertain", "file_tool_no_path", "file", f"{tool_name} thiếu {key}")
    ap = _abs(p, ctx.cwd)
    rp = _real(ap)
    op = "overwrite" if tool_name == "Write" and os.path.exists(ap) else "write"
    for cand in {ap, rp}:
        if _under(cand, canon_path(str(ctx.policy.policy_dir))):
            return Verdict("destructive", "policy_write", "policy", "agent không được ghi chính sách an toàn (FR-SG-05)", [ap], "hub-policy")
    # R2-1: tool ghi file KHÔNG chịu sandbox OS → vùng thực thi ngoài sandbox = CẤM thẳng
    z = exec_zone(ap)
    if z is not None:
        return Verdict("destructive", "exec_zone_write", "system", f"{tool_name} vào vùng thực thi ngoài sandbox ({z}) — chạy như người khi nạp/khởi động",
                       [ap], "exec-zone", forbidden=True)
    for cand in {ap, rp}:
        r = protected_path(ctx, cand, op)
        if r:
            return Verdict("destructive", "protected_path", "file", f"{tool_name} vào tài nguyên được bảo vệ {r.selector} ({op})", [ap], r.id)
    # R2-1: ngoài workspace của run (+ tmp/add-dir) → chờ người (cùng mô hình sandbox Bash)
    roots = write_roots(ctx)
    if not any(_under(c, r) for c in {ap, rp} for r in roots):
        return Verdict("destructive", "write_outside_workspace", "file", f"{tool_name} ngoài workspace của run ({ap}) — cần người duyệt", [ap])
    return Verdict("write", "file_write", "file", targets=[ap])


# ============================================================================ MCP
def _name_tokens(name: str) -> set[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
    toks = {t.lower() for t in re.split(r"[_\-\s./:]+", s) if t}
    parts = [t.lower() for t in re.split(r"[_\-\s./:]+", s) if t]
    toks |= {a + b for a, b in zip(parts, parts[1:])}  # go_bo → gobo
    return toks


# tool connector có tên dạng đọc → safe (không audit ghi); còn lại = write (audit), mẫu xoá vẫn qua approval
_CONNECTOR_READ_RE = re.compile(r"^(get|list|search|read|query|fetch|lookup|find|whoami|guide|download|export|suggest|atlassian_?user)", re.I)


def classify_connector(server: str, tool: str, ctx: GuardContext) -> Verdict:
    prot = ctx.policy.match_mcp(server, tool)
    if prot is not None:
        return Verdict("destructive", "protected_mcp_tool", "mcp", f"tool MCP được bảo vệ ({prot.selector})", [f"{server}/{tool}"], prot.id)
    hit = _name_tokens(tool) & MCP_DELETE_TOKENS
    if hit:
        return Verdict("destructive", "mcp_delete_name", "mcp", f"tên tool khớp mẫu xoá ({', '.join(sorted(hit))})", [f"{server}/{tool}"])
    if _CONNECTOR_READ_RE.match(tool):
        return Verdict("safe", "claude_connector_read")
    return Verdict("write", "claude_connector_write", "mcp", "tool connector claude.ai (người cho phép ở manager.capabilities.connectors)", [f"{server}/{tool}"])


def classify_mcp(tool_name: str, ti: dict[str, Any], ctx: GuardContext) -> Verdict:
    rest = tool_name[len("mcp__") :]
    server, _, tool = rest.partition("__")
    if server == "hub-guard":
        return Verdict("safe", "hub_guard_tool")
    if ctx.connectors is not None and server not in ctx.run_mcp_servers and server not in ("hub-self", "hub-guard"):
        # run connector: CLI nạp thêm MCP/plugin của ~/.claude — chỉ connector người cho phép mới qua (slug có thể chứa "__")
        for slug in ctx.connectors:
            pre = f"claude_ai_{slug}__"
            if rest.startswith(pre):
                return classify_connector(f"claude_ai_{slug}", rest[len(pre):], ctx)
        return Verdict("destructive", "mcp_not_in_run", "mcp", f"{tool_name}: server không thuộc agent và không nằm trong connector được phép",
                       [tool_name[:200]], forbidden=True)
    if MCP_FORBIDDEN.match(tool) or (server.startswith("hub") and tool in ("approve", "deny")):
        return Verdict("destructive", "self_approval", "hub", f"agent không được tự duyệt/sửa chính sách qua {tool_name}", forbidden=True)
    is_hub = server in ctx.hub_mcp_servers or server in _names.HUB_MCP_SERVER_NAMES
    if (is_hub or tool.startswith("hub_")) and HUB_MCP_HUMAN_ONLY.match(tool):
        # H-18: MCP server của hub (kể cả đăng ký dưới tên khác) — tool ứng với method chỉ-người
        return Verdict("destructive", "hub_mcp_human_only", "hub", f"{tool_name}: thao tác chỉ dành cho người (agent không được gọi)", forbidden=True)
    if server == "hub-self":  # M3 FR-MS-08: tool task_* của hub, ràng với run — không phá huỷ
        from ..mcp_server.hub_self import MANAGER_READ_TOOLS  # M7: tool đọc của điều phối viên (quyền ghi kiểm ở daemon)

        safe = tool in ("task_show", "team_roster", "skills_list", "skill_view", "memory_view", "memory_search") or tool in MANAGER_READ_TOOLS
        return Verdict("safe" if safe else "write", "hub_self_tool")  # M4-A: tool đọc tri thức
    ann = None
    if ctx.mcp_annotations is not None:
        try:
            ann = ctx.mcp_annotations(server, tool)
        except Exception:
            ann = None
    ann = ann or {}
    prot = ctx.policy.match_mcp(server, tool)
    if prot is not None:
        return Verdict("destructive", "protected_mcp_tool", "mcp", f"tool MCP được bảo vệ ({prot.selector})", [f"{server}/{tool}"], prot.id)
    if ann.get("destructiveHint") is True:
        return Verdict("destructive", "mcp_destructive_hint", "mcp", "tool có destructiveHint", [f"{server}/{tool}"])
    toks = _name_tokens(tool)
    hit = toks & MCP_DELETE_TOKENS
    if hit:
        return Verdict("destructive", "mcp_delete_name", "mcp", f"tên tool khớp mẫu xoá ({', '.join(sorted(hit))})", [f"{server}/{tool}"])
    if ann.get("require_approval") is True:
        return Verdict("uncertain", "mcp_require_approval", "mcp", "người đặt tool này cần duyệt (tools.require_approval, FR-MC-03)", [f"{server}/{tool}"])
    if ann.get("readOnlyHint") is True:
        return Verdict("safe", "mcp_read_only")
    desc = str(ann.get("description") or "")
    if desc and MCP_DESC_RE.search(desc):
        return Verdict("destructive", "mcp_delete_description", "mcp", "mô tả tool khớp mẫu xoá", [f"{server}/{tool}"])
    if ctx.mcp_annotations is not None and not ann:
        return Verdict("uncertain", "mcp_unknown_tool", "mcp", "tool MCP chưa thấy khi probe → cần duyệt tới khi probe lại (FR-MC-03)", [f"{server}/{tool}"])
    if str(ann.get("trust") or "") == "untrusted":
        return Verdict("uncertain", "mcp_untrusted_write", "mcp", "server untrusted: tool không readOnlyHint → phê duyệt động (FR-MC-03)", [f"{server}/{tool}"])
    return Verdict("write", "mcp_write", "mcp", "tool MCP không có readOnlyHint → ghi (audit)", [f"{server}/{tool}"])


# ============================================================================ SQL
def _strip_sql_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    return re.sub(r"--[^\n]*", " ", s)


def sql_destructive(text: str) -> str | None:
    if not text:
        return None
    t = _strip_sql_comments(text)
    up = t.upper()
    if re.search(r"\bDROP\s+(DATABASE|SCHEMA|TABLE|VIEW|INDEX|COLLECTION|USER|ROLE|TABLESPACE|MATERIALIZED\s+VIEW|SEQUENCE|FUNCTION|PROCEDURE|TRIGGER|TYPE|EXTENSION|KEYSPACE|OWNED)\b", up):
        return "sql_drop"
    if re.search(r"\bTRUNCATE\b", up):
        return "sql_truncate"
    if re.search(r"\bVACUUM\b[^;]*\bINTO\b", up):
        return "sql_vacuum_into"  # ghi đè file đích
    if re.search(r"\bALTER\s+TABLE\b[^;]*\bDROP\b", up):
        return "sql_alter_drop"
    for stmt in re.split(r";", up):
        st = stmt.strip()
        m = re.search(r"\b(DELETE\s+FROM|DELETE\s+\w|UPDATE\s+[\w.\"`\[\]]+\s+SET)\b", st)
        if m:
            where = re.search(r"\bWHERE\b(.*)$", st[m.start():], re.S)
            if not where:
                return "sql_no_where"
            cond = where.group(1).strip()
            if re.fullmatch(r"(1\s*=\s*1|TRUE|'1'\s*=\s*'1'|1|NOT\s+FALSE)\s*", cond):
                return "sql_tautology_where"
    if re.search(r"\.drop\s*\(|dropDatabase|dropCollection|\bdeleteMany\s*\(\s*\{\s*\}\s*\)|\.remove\s*\(\s*\{\s*\}\s*\)|\bdropIndexes\b", t):
        return "mongo_drop"
    if re.search(r"\b(FLUSHALL|FLUSHDB)\b", up):
        return "redis_flush"
    return None


# ============================================================================ Bash
class BashAnalyzer:
    MAX_DEPTH = 6

    def __init__(self, ctx: GuardContext, depth: int = 0, cwd: str | None = None, aliases: dict[str, str] | None = None,
                 state: dict[str, Any] | None = None) -> None:
        self.ctx = ctx
        self.depth = depth
        self.cwd: str | None = cwd if cwd is not None else canon_path(str(ctx.cwd))
        self.aliases: dict[str, str] = aliases if aliases is not None else {}
        # H-02: symlink tạo TRONG cùng lệnh (`ln -s <HUB>/policy pol; echo x > pol/…`) — dùng chung cho lệnh con
        self.state: dict[str, Any] = state if state is not None else {"links": {}}
        self.v = Verdict("safe", "read_only")

    # -------------------------------------------------------------- helpers
    def hit(self, verdict: str, rule: str, klass: str | None, reason: str, targets: list[str] | None = None, prot: ProtectedResource | None = None, forbidden: bool = False) -> None:
        if self.ctx.manager_run and verdict != "safe" and any(_MGR_SELF_CFG.search(str(t)) for t in targets or []):
            # §9: lệnh ghi CLAUDE.md/.claude/.mcp.json (thường chỉ là "write") — giữ đích để classify() cấm cho run manager
            verdict, forbidden, reason = "destructive", True, f"điều phối viên không ghi cấu hình/bộ nhớ của claude ({reason})"
        self.v = _max(self.v, Verdict(verdict, rule, klass, reason, list(targets or []), prot.id if prot else None, forbidden))

    def sub(self, script: str, cwd: str | None = None) -> Verdict:
        if self.depth >= self.MAX_DEPTH:
            return Verdict("uncertain", "nesting_too_deep", "system", "lệnh lồng quá sâu")
        a = BashAnalyzer(self.ctx, self.depth + 1, cwd if cwd is not None else self.cwd, dict(self.aliases), self.state)
        return a.run(script)

    def merge(self, v: Verdict, prefix: str = "") -> None:
        if prefix and v.verdict != "safe":
            v = Verdict(v.verdict, v.rule, v.klass, f"{prefix}: {v.reason}", v.targets, v.protected_id, v.forbidden)
        self.v = _max(self.v, v)

    def path(self, w: shell.Word | str) -> str | None:
        txt = w.text if isinstance(w, shell.Word) else w
        if shell.DYN in txt:
            return None
        if _isabs(os.path.expanduser(txt)):
            return self.follow_links(_abs(txt, "/"))
        if self.cwd is None:
            return None
        return self.follow_links(_abs(txt, self.cwd))

    def follow_links(self, p: str) -> str:
        """Thay tiền tố là symlink do chính lệnh này tạo bằng đích của nó (lặp tối đa 8 lần)."""
        links: dict[str, str] = self.state.get("links") or {}
        for _ in range(8):
            for link, target in links.items():
                if _under(p, link):
                    p = _norm(target + canon_path(p)[len(canon_path(link)):])
                    break
            else:
                return p
        return p

    def expand(self, w: shell.Word) -> list[str] | None:
        """Đường dẫn tuyệt đối của một đối số (mở glob). None nếu không giải được."""
        p = self.path(w)
        if p is None:
            return None
        if w.glob:
            hits = _glob.glob(p, recursive=False, include_hidden=False)
            return [_norm(h) for h in hits] or []
        return [p]

    def protected(self, p: str, op: str) -> ProtectedResource | None:
        return protected_path(self.ctx, p, op)

    def check_delete_target(self, p: str, what: str, recursive: bool = True) -> None:
        if _critical(p, self.ctx.home) or _same(p, os.path.dirname(self.ctx.home)):
            self.hit("destructive", "delete_critical", "system", f"{what} thư mục hệ thống/home {p}", [p])
            return
        r = self.protected(p, "delete")
        if r is None and recursive and os.path.isdir(p):
            r = self.ctx.policy.protected_under(p)
        if r is not None:
            self.hit("destructive", "protected_delete", "file", f"{what} chạm tài nguyên được bảo vệ {r.selector}", [p], r)
            return
        if not created_by_run(p, self.ctx):
            if self.checkpointed(p):
                self.hit("write", "delete_checkpointed", "file", f"{what} {p} — có checkpoint của run (revert được)", [p])
                return
            self.hit("destructive", "delete_not_run_created", "file", f"{what} {p} — không phải file run tự tạo", [p])
        else:
            self.hit("write", "delete_run_created", "file", f"{what} file run tự tạo", [p])

    def check_overwrite_target(self, p: str, what: str) -> None:
        if p.startswith(SPECIAL_FILES) or p == "/dev/null":
            return
        if DISK_DEV.match(p):
            self.hit("destructive", "write_block_device", "system", f"{what} ghi thẳng thiết bị đĩa {p}", [p])
            return
        exists = os.path.lexists(p)
        r = self.protected(p, "overwrite" if exists else "write")
        if r is not None:
            self.hit("destructive", "protected_write", "file", f"{what} vào tài nguyên được bảo vệ {r.selector}", [p], r)
            return
        if _tmp_scratch(p, self.ctx) or not exists or os.path.isdir(p):
            self.hit("write", "write_file", "file", what, [p])
            return
        if not created_by_run(p, self.ctx):
            if self.checkpointed(p):
                self.hit("write", "overwrite_checkpointed", "file", f"{what} {p} — có checkpoint của run (revert được)", [p])
                return
            self.hit("destructive", "overwrite_existing", "file", f"{what} ghi đè file có sẵn {p} (không phải file run tự tạo)", [p])
        else:
            self.hit("write", "overwrite_run_created", "file", what, [p])

    def checkpointed(self, p: str) -> bool:
        """File THƯỜNG đã được git track trong repo có checkpoint của run → `cfharness run revert` khôi phục được."""
        root = self.ctx.checkpoint_root
        if not root or os.path.isdir(p) or not _under(_real(p), _real(root)):
            return False
        r = self._git(root, "ls-files", "--error-unmatch", "--", os.path.relpath(_real(p), _real(root)))
        return r is not None and r.returncode == 0

    def check_write_target(self, p: str, what: str, op: str = "write") -> None:
        r = self.protected(p, op)
        if r is not None:
            self.hit("destructive", "protected_write", "file", f"{what} vào tài nguyên được bảo vệ {r.selector}", [p], r)
        else:
            self.hit("write", "write_file", "file", what, [p])

    # -------------------------------------------------------------- run
    def run(self, script: str) -> Verdict:
        norm = shell.normalize(_win_slashes(script))
        if FORK_BOMB.search(norm):
            self.hit("destructive", "fork_bomb", "system", "fork bomb")
        if OBFUSCATION.search(norm):
            self.hit("uncertain", "obfuscated", "system", "lệnh giải mã/che giấu (base64/xxd -r…) — không phân tích được")
        sock = self.ctx.hub_socket
        if (sock and _contains(norm, sock)) or HUB_SOCKET_RE.search(norm):
            self.hit("destructive", "hub_socket", "hub", "agent đụng socket/token điều khiển hub", forbidden=True)
        hh = _hub_home(self.ctx)
        if _contains(norm, hh + "/auth") or _contains(norm, hh + "/secrets"):
            self.hit("destructive", "hub_secret_read", "hub", "agent đụng token người/secret store của hub", forbidden=True)
        for pat in self.ctx.policy.bash_patterns:
            try:
                if re.search(pat["pattern"], norm):
                    self.hit("destructive", "catalog_pattern", "policy", pat.get("note") or f"khớp mẫu {pat['pattern']}")
            except re.error:
                continue
        env: dict[str, str | None] = {"HOME": self.ctx.home}
        for n in _names.env_names("HOME"):  # cả tên cũ: `$CFHARNESS_HOME/...` vẫn giải ra HUB_HOME
            env[n] = _hub_home(self.ctx)
        if self.ctx.hub_socket:
            for n in _names.env_names("SOCKET"):
                env[n] = self.ctx.hub_socket
        res = shell.parse(norm, env=env, home=self.ctx.home)
        if res.error:
            self.hit("uncertain", "parse_error", "system", f"không phân tích được lệnh: {res.error}")
        for s in res.substitutions:
            self.merge(self.sub(s), "trong $(..)")
        for sc in res.commands:
            self.command(sc)
            self._repo_deny(sc)
        return self.v

    def _repo_deny(self, sc: shell.SimpleCommand) -> None:
        """MGR-2: mẫu chặn theo repo do NGƯỜI đặt (vd `Bash(npm run deploy:*)`) → destructive cho MỌI run có cwd trong repo."""
        if not sc.words:
            return
        self.apply_repo_deny(" ".join(w.literal for w in sc.words), self.cwd)  # cwd hiện tại (sau `cd X &&`) = repo đích

    def apply_repo_deny(self, cmd: str, target_dir: str | None) -> bool:
        """R2-2: khớp mẫu chặn của repo ĐÍCH (`target_dir`: -C/--prefix/cd …; None = cwd của run) trên lệnh ĐÃ CHUẨN HOÁ.
        Mẫu `forbid:<rule>` → forbidden (từ chối thẳng, không mở approval)."""
        from .runtime import pattern_matches

        pats: list[str] = list(self.ctx.repo_deny)
        if target_dir and self.ctx.repo_deny_lookup is not None:
            try:
                pats = list(dict.fromkeys(pats + list(self.ctx.repo_deny_lookup(target_dir))))
            except Exception:  # pragma: no cover
                pass
        if not pats:
            return False
        ti = {"command": re.sub(r"\s+", " ", cmd).strip()}
        for pat in pats:
            forbid = pat.startswith("forbid:")
            rule = pat[len("forbid:"):] if forbid else pat
            if pattern_matches(rule, "Bash", ti):
                self.hit("destructive", "repo_deny", "policy", f"mẫu chặn theo repo (người đặt): {rule}", forbidden=forbid)
                return True
        return False

    # -------------------------------------------------------------- simple command
    def command(self, sc: shell.SimpleCommand) -> None:
        # V2-N3: kiểm socket/token trên từ ĐÃ bỏ quote/nối chuỗi (`hub.so''ck`)
        for w in list(sc.words) + [rw for _, rw in sc.redirects]:
            lit = w.literal
            if HUB_SOCKET_RE.search(lit) or (self.ctx.hub_socket and _contains(lit, self.ctx.hub_socket)):
                self.hit("destructive", "hub_socket", "hub", "agent đụng socket/token điều khiển hub", forbidden=True)
        if sc.words and os.path.basename(sc.words[0].text.lstrip("\\")) in ("socat", "nc", "ncat", "netcat") and any(
            "UNIX" in w.text.upper() or w.text == "-U" or w.dynamic for w in sc.words[1:]):
            self.hit("uncertain", "unix_socket_cmd", "hub", "kết nối Unix socket / đích động — không xác định được")
        self.sensitive_sweep(sc)
        for op, w in sc.redirects:
            if op in ("<", "<>", "<&"):
                continue
            p = self.path(w)
            if p is None:
                self.hit("uncertain", "redirect_dynamic", "file", "chuyển hướng ghi tới đích động", [w.literal])
                continue
            # nhớ nội dung file tạo bằng heredoc trong CÙNG lệnh (`cat > s.py <<EOF … EOF; python3 s.py`)
            if sc.stdin_text is not None and not sc.stdin_dynamic:
                self.state.setdefault("files", {})[p] = sc.stdin_text
            elif sc.stdin_dynamic:
                self.state.setdefault("files", {})[p] = None
            elif sc.words and os.path.basename(sc.words[0].text) in ("echo", "printf"):
                ws = sc.words[1:]
                self.state.setdefault("files", {})[p] = None if any(x.dynamic for x in ws) else " ".join(x.text for x in ws if x.text not in ("-e", "-n", "-E"))
            else:
                self.state.setdefault("files", {})[p] = None
            if op in (">>", "&>>"):
                self.check_write_target(p, "ghi nối (>>)")
            else:
                self.check_overwrite_target(p, f"chuyển hướng {op}")
        words = list(sc.words)
        self.words(words, sc)

    def sensitive_regions(self) -> list[str]:
        """Vùng NHẠY CẢM của HUB_HOME: đọc được = tự duyệt/ký link/lộ secret (WT-3). Không phụ thuộc cách viết lệnh."""
        # chỉ vùng mà ĐỌC ĐƯỢC = lộ secret/token/tự duyệt (policy đã do protected_write bắt — không đưa vào đây để không che protected_id)
        out: list[str] = []
        for h in {_hub_home(self.ctx), _real(_hub_home(self.ctx))}:
            out += [f"{h}/auth", f"{h}/secrets", f"{h}/mcp_server/tokens.json", f"{h}/hub.db"]
        if self.ctx.hub_socket:
            sock = canon_path(self.ctx.hub_socket)
            out += [sock, sock + ".auth"]
        uh = canon_path(str(self.ctx.policy.user_home)).rstrip("/")  # R3-1: thông tin xác thực của người
        out += [f"{uh}/{x}" for x in (".config/gh", ".git-credentials", ".config/git/credentials", "Library/Keychains", ".netrc", ".ssh") + _win_cred_dirs()]
        return list(dict.fromkeys(out))

    def _in_region(self, p: str, region: str) -> bool:
        if IS_WINDOWS:
            p, region = fold_path(p), fold_path(region)
        p, region = p.rstrip("/"), region.rstrip("/")
        return p == region or p.startswith(region + "/")

    def _ancestor_of_region(self, p: str, region: str) -> bool:
        if IS_WINDOWS:
            p, region = fold_path(p.rstrip("*")), fold_path(region)
        p = p.rstrip("*").rstrip("/")
        return bool(p) and region.rstrip("/").startswith(p + "/")

    _RECURSIVE = {"find", "grep", "egrep", "fgrep", "rg", "ag", "cp", "scp", "rsync", "tar", "gtar", "bsdtar", "zip",
                  "chmod", "chown", "chgrp", "rm", "shred", "ditto"}

    def sensitive_sweep(self, sc: shell.SimpleCommand) -> None:
        """WT-3: mọi đối số/đích có thể chạm vùng nhạy cảm (sau khi giải cd/biến/glob) → đọc secret forbidden;
        không giải được chắc chắn mà có dấu hiệu trỏ vào vùng đó → uncertain (giữ chờ người)."""
        if not sc.words:
            return
        base = os.path.basename(sc.words[0].text.lstrip("\\"))
        if base in ("cd", "pushd", "popd"):  # đổi thư mục (kiểm ở lệnh sau theo cwd mới); vào hub home là vô hại
            return
        recursive = base in self._RECURSIVE
        regions = self.sensitive_regions()
        words = list(sc.words[1:]) + [w for op, w in sc.redirects if op not in ("<&",)]
        for w in words:
            t = w.text
            if not t or (t.startswith("-") and "=" not in t):
                continue
            if "=" in t and t.startswith("-"):
                t = t.split("=", 1)[1]
            if w.dynamic:
                # đích động: phần literal (đã bỏ $..) trỏ vào/kết thúc bằng tên nhạy cảm → uncertain (giữ chờ người)
                lit = w.literal
                if any(_contains(lit, region) for region in regions) or SENSITIVE_HINT.search(lit.replace("\\", "/") if IS_WINDOWS else lit):
                    self.hit("uncertain", "sensitive_dynamic", "hub", "đường dẫn động có thể chạm vùng nhạy cảm hub", [lit])
                    return
                continue
            exp = self.expand(w)
            cands = exp if exp else ([p] if (p := self.path(w)) else [])
            for cand in cands:
                for r in {cand, _real(cand)}:
                    for region in regions:
                        if self._in_region(r, region):
                            if sensitive_read(r, self.ctx) or region.rstrip("/").endswith(("/auth", "/secrets", "/.config/gh", "/Keychains", "/.ssh")) or region.endswith((".sock", ".sock.auth", "hub.db", ".git-credentials", ".netrc")):
                                self.hit("destructive", "hub_secret_read", "hub", f"lệnh chạm vùng nhạy cảm hub ({region})", [cand], forbidden=True)
                            else:
                                self.hit("destructive", "hub_sensitive", "hub", f"lệnh chạm vùng nhạy cảm hub ({region})", [cand])
                            return
                        if recursive and self._ancestor_of_region(r, region):  # duyệt cây có thể chạm vùng nhạy cảm
                            self.hit("uncertain", "sensitive_recursive", "hub", f"lệnh duyệt cây có thể chạm vùng nhạy cảm hub ({region})", [cand])
                            return
            if exp == [] and w.glob:  # glob không khớp gì NHƯNG tiền tố cố định trỏ vào/chứa vùng nhạy cảm
                fixed = self.path(shell.Word(re.split(r"[*?\[]", t)[0]))
                for region in regions:
                    if fixed and (self._in_region(fixed, region) or self._ancestor_of_region(fixed, region)):
                        self.hit("uncertain", "sensitive_glob", "hub", f"glob có thể chạm vùng nhạy cảm hub ({region})", [t])
                        return

    def _expand_marker(self, lit: str, region: str) -> str:
        return lit

    def words(self, words: list[shell.Word], sc: shell.SimpleCommand, unknown_targets: bool = False) -> None:
        # bỏ từ khoá + wrapper
        while words:
            w0 = words[0]
            if w0.dynamic:
                self.hit("uncertain", "dynamic_command", "system", "tên lệnh động ($VAR/$(..)) — không biết lệnh gì", [w0.literal])
                return
            name = w0.text.lstrip("\\")
            base = os.path.basename(name)
            if not base.isascii():
                self.hit("uncertain", "non_ascii_command", "system", f"tên lệnh chứa ký tự lạ ({base!r}) — có thể là chữ đồng hình")
                return
            if base in ("if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "fi", "done", "esac", "in"):
                words = words[1:]
                continue
            if base in self.aliases and self.depth < self.MAX_DEPTH:
                rest = " ".join(_q(w.literal) for w in words[1:])
                self.merge(self.sub(self.aliases[base] + " " + rest), f"alias {base}")
                return
            nw = self.unwrap(base, words)
            if nw is None:
                return
            if nw is words:
                break
            words = nw
            if unknown_targets is False and getattr(self, "_xargs", False):
                unknown_targets = True
                self._xargs = False
        if not words:
            return
        self.dispatch(os.path.basename(words[0].text.lstrip("\\")), words, sc, unknown_targets)

    def unwrap(self, base: str, words: list[shell.Word]) -> list[shell.Word] | None:
        args = words[1:]
        if base in ("sudo", "doas"):
            i = 0
            while i < len(args) and args[i].text.startswith("-"):
                i += 2 if args[i].text in ("-u", "-g", "-U", "-C", "-D", "-p", "-h", "-r", "-t", "-T") else 1
            return args[i:]
        if base == "env":
            i = 0
            while i < len(args):
                t = args[i].text
                if t in ("-u", "--unset", "-C", "--chdir"):
                    i += 2
                elif t in ("-S", "--split-string") and i + 1 < len(args):
                    self.merge(self.sub(args[i + 1].literal + " " + " ".join(_q(w.literal) for w in args[i + 2 :])))
                    return None
                elif t.startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
                    i += 1
                else:
                    break
            return args[i:]
        if base in ("nohup", "builtin", "chronic", "unbuffer", "setsid", "noglob", "nocorrect", "torsocks", "proxychains", "proxychains4", "caffeinate", "stdbuf", "xcrun", "arch", "time", "exec"):
            i = 0
            while i < len(args) and args[i].text.startswith("-"):
                i += 2 if (base, args[i].text) in (("exec", "-a"), ("arch", "-e")) else 1
            return args[i:]
        # M4-B: trình chạy gói (`uv run cfharness golden add …`, `uvx cfharness approve …`) — phân loại lệnh bên trong
        if (base in ("uv", "poetry", "pdm", "hatch", "rye", "pipx") and args and args[0].text in ("run", "exec")) or base in ("uvx", "npx", "bunx", "pnpx"):
            i = 0 if base in ("uvx", "npx", "bunx", "pnpx") else 1
            while i < len(args) and args[i].text.startswith("-"):
                i += 2 if args[i].text in ("--with", "--from", "--python", "-p", "--project", "--directory", "--package", "--env-file", "-e", "--spec") else 1
            return args[i:] or None
        if base == "command":
            if args and args[0].text in ("-v", "-V"):
                self.hit("safe", "command_lookup", None, "")
                return None
            return [a for a in args if a.text != "-p"] if args else None
        if base in ("nice", "ionice"):
            i = 0
            while i < len(args) and args[i].text.startswith("-"):
                i += 2 if args[i].text in ("-n", "-c", "-p") else 1
            return args[i:]
        if base in ("timeout", "gtimeout"):
            i = 0
            while i < len(args) and args[i].text.startswith("-"):
                i += 2 if args[i].text in ("-s", "-k", "--signal", "--kill-after") else 1
            return args[i + 1 :]
        if base == "watch":
            i = 0
            while i < len(args) and args[i].text.startswith("-"):
                i += 2 if args[i].text in ("-n", "--interval", "-d") else 1
            self.merge(self.sub(" ".join(w.literal for w in args[i:])), "watch")
            return None
        if base in ("xargs", "parallel"):
            i = 0
            while i < len(args) and args[i].text.startswith("-"):
                t = args[i].text
                i += 2 if t in ("-I", "-i", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "--max-args", "--max-procs", "-j", "--jobs") else 1
            rest = args[i:]
            if base == "parallel" and any(w.text == ":::" for w in rest):
                rest = rest[: [w.text for w in rest].index(":::")]
            if not rest:
                rest = [shell.Word("echo")]
            self._xargs = True
            return rest
        return words

    # -------------------------------------------------------------- per command
    def dispatch(self, base: str, words: list[shell.Word], sc: shell.SimpleCommand, unknown_targets: bool) -> None:
        args = words[1:]
        texts = [a.text for a in args]
        if base in ("cd", "pushd"):
            tgt = args[0] if args else None
            if tgt is None:
                self.cwd = self.ctx.home
            elif tgt.text == "-":
                self.cwd = None
            else:
                p = self.path(tgt)
                self.cwd = p  # None nếu động → đích tương đối phía sau không xác định
            return
        if base == "alias":
            for a in args:
                m = re.match(r"^([^=]+)=(.*)$", a.text, re.S)
                if m:
                    self.aliases[m.group(1)] = m.group(2)
            return
        if base in ("eval",):
            if any(a.dynamic for a in args):
                self.hit("uncertain", "eval_dynamic", "system", "eval nội dung động")
            self.merge(self.sub(" ".join(a.literal for a in args)), "eval")
            return
        if base in ("source", "."):
            self.script_file(args[0] if args else None, "source")
            return
        if base == "trap":
            if args:
                self.merge(self.sub(args[0].literal), "trap")
            return
        if self.windows_cmd(base, args, sc):
            return
        if base in SHELLS or re.fullmatch(r"(ba|z|k|da)?sh\d*(\.\d+)?", base):
            self.shell_cmd(base, args, sc)
            return
        if base in INTERPRETERS or re.fullmatch(r"(python|pypy|ruby|perl|node|php)[\d.]*", base):
            self.interpreter(base, args, sc)
            return
        if base in _names.HUB_CLI_NAMES:
            self.hub_cli(texts, unknown=unknown_targets or any(a.dynamic for a in args))
            return
        if base == "claude" or base.endswith("claude-code") or (base in ("npx", "bunx", "pnpx", "pnpm", "yarn") and any("claude-code" in t for t in texts)):
            # §9: `claude` lồng chạy ngoài hook guard của hub (đọc cấu hình người) — cờ bỏ phân quyền = lách guard → cấm mọi agent;
            # run điều phối viên: cấm mọi `claude` lồng (giao việc bằng run_start/task_create) — ở classify()
            joined = " ".join(texts)
            if re.search(r"--(allow-)?dangerously-skip-permissions|bypassPermissions|--permission-prompt-tool|--settings\b|--mcp-config\b|--setting-sources\b", joined):
                self.hit("destructive", "nested_claude", "hub", "claude lồng với cờ bỏ/ghi đè phân quyền — lách guard của hub", forbidden=True)
            else:
                self.hit("write", "nested_claude", None, "claude lồng (ngoài hook guard của hub)")
            return
        if base in DB_CLIENTS or base in ("dropdb", "dropuser", "mysqladmin", "pg_restore", "mongorestore", "redis-server"):
            self.db_client(base, args, sc)
            return
        if base in ("manage.py",) or (base.endswith("manage.py")):
            if any(t in ("flush", "reset_db", "sqlflush") for t in texts):
                self.hit("destructive", "framework_db_reset", "db", "manage.py flush/reset_db xoá dữ liệu")
            return
        if base in ("rake", "rails", "bin/rails"):
            if any(re.match(r"db:(drop|reset|schema:load|purge|truncate_all)", t) for t in texts):
                self.hit("destructive", "framework_db_reset", "db", "rails/rake db:drop/reset xoá CSDL")
            else:
                self.hit("write", "build_tool", None, "")
            return
        if base in ("prisma",) or (base in ("npx", "bunx", "pnpx") and "prisma" in texts):
            if ("migrate" in texts and "reset" in texts) or "--force-reset" in texts or "--accept-data-loss" in texts:
                self.hit("destructive", "framework_db_reset", "db", "prisma reset/force-reset xoá dữ liệu")
            else:
                self.hit("write", "build_tool", None, "")
            return
        if base in DELETE_CMDS:
            self.delete_cmd(base, args, unknown_targets)
            return
        if base == "rmdir":
            for a in args:
                if a.text.startswith("-"):
                    continue
                p = self.path(a)
                if p is None:
                    self.hit("uncertain", "rmdir_dynamic", "file", "rmdir đích động")
                elif self.protected(p, "delete"):
                    self.check_delete_target(p, "rmdir", recursive=False)
                else:
                    self.hit("write", "rmdir_empty", "file", "rmdir (chỉ thư mục rỗng)", [p])
            return
        if base in ("truncate", "gtruncate"):
            files = [a for i, a in enumerate(args) if not a.text.startswith("-") and not (i > 0 and args[i - 1].text in ("-s", "--size", "-r", "--reference"))]
            for a in files:
                p = self.path(a)
                if p is None:
                    self.hit("uncertain", "truncate_dynamic", "file", "truncate đích động")
                else:
                    self.check_overwrite_target(p, "truncate")
            return
        if base == "find":
            self.find_cmd(args)
            return
        if base == "rsync":
            self.rsync(args)
            return
        if base in ("mv", "gmv"):
            self.mv_cp(base, args, unknown_targets)
            return
        if base in ("cp", "gcp", "install", "ditto"):
            self.mv_cp(base, args, unknown_targets)
            return
        if base == "tee":
            append = any(t in ("-a", "--append") for t in texts)
            for a in args:
                if a.text.startswith("-"):
                    continue
                p = self.path(a)
                if p is None:
                    self.hit("uncertain", "tee_dynamic", "file", "tee đích động")
                elif append:
                    self.check_write_target(p, "tee -a")
                else:
                    self.check_overwrite_target(p, "tee")
            return
        if base == "dd":
            for t in texts:
                if t.startswith("of="):
                    p = self.path(t[3:])
                    if p is None:
                        self.hit("uncertain", "dd_dynamic", "system", "dd of= động")
                    else:
                        self.check_overwrite_target(p, "dd of=")
            return
        if base in ("sed", "gsed", "perl") and any(t == "-i" or t.startswith("-i") or t.startswith("--in-place") or (t.startswith("-") and not t.startswith("--") and "i" in t[1:] and base != "perl") for t in texts):
            for a in args:
                if a.text.startswith("-") or a.dynamic or not a.text:
                    continue
                p = self.path(a)
                if p and (os.path.exists(p) or "/" in a.text):
                    self.check_write_target(p, f"{base} -i", "overwrite")
            self.hit("write", "inplace_edit", "file", f"{base} -i")
            return
        if base in ("chmod", "chown", "chgrp", "chflags", "setfacl"):
            rec = any(t.startswith("-") and "R" in t for t in texts)
            for a in args:
                if a.text.startswith("-") or a.dynamic:
                    continue
                p = self.path(a)
                if not p or not (os.path.exists(p) or a.text.startswith(("/", "~", "."))):
                    continue
                if rec and _critical(p, self.ctx.home):
                    self.hit("destructive", "chmod_recursive_system", "system", f"{base} -R trên {p}", [p])
                else:
                    self.check_write_target(p, base)
            return
        if base in ("ln", "gln"):
            self.ln_cmd(args)
            return
        if base == "git":
            self.git(args)
            return
        if base in ("tar", "gtar", "bsdtar", "unzip"):
            self.extract(base, args)
            return
        if base in ("mkfs", "newfs", "fdisk", "sfdisk", "gdisk", "parted", "wipefs", "format", "mkswap", "badblocks") or base.startswith(("mkfs.", "newfs_")):
            self.hit("destructive", "disk_admin", "system", f"{base}: lệnh quản trị đĩa huỷ dữ liệu")
            return
        if base == "diskutil":
            if any(re.match(r"(erase\w*|zeroDisk|randomDisk|secureErase|partitionDisk|reformat|apfs|deleteVolume|deleteContainer|unmountDisk)$", t, re.I) for t in texts):
                self.hit("destructive", "disk_admin", "system", "diskutil erase/partition")
            return
        if base in ("shutdown", "reboot", "halt", "poweroff", "init", "systemctl") and (base != "systemctl" or any(t in ("poweroff", "reboot", "halt", "kexec") for t in texts)) and (base != "init" or any(t in ("0", "6") for t in texts)):
            self.hit("destructive", "system_power", "system", f"{base}: tắt/khởi động lại máy")
            return
        if base == "crontab" and any(t.startswith("-") and "r" in t for t in texts):
            self.hit("destructive", "crontab_remove", "system", "crontab -r xoá toàn bộ lịch")
            return
        if base == "crontab" and "-l" not in texts:
            # V2-N2: cài lịch = lệnh chạy về sau NGOÀI run/sandbox như người
            self.hit("destructive", "schedule_persistence", "system", "crontab cài/sửa lịch (chạy ngoài sandbox như người)")
            return
        if base in ("at", "batch") and not (texts and texts[0] in ("-l", "-q", "-V")):
            self.hit("destructive", "schedule_persistence", "system", f"{base}: hẹn giờ chạy lệnh ngoài sandbox")
            return
        if base == "launchctl" and any(t in ("load", "bootstrap", "submit", "enable", "kickstart", "start", "setenv", "asuser", "bootout", "unload",
                                             "kill", "stop", "remove", "disable", "reboot") for t in texts):
            self.hit("destructive", "schedule_persistence", "system", "launchctl nạp/chạy job launchd (ngoài sandbox)")
            return
        if base == "systemctl" and "--user" in texts and any(t in ("enable", "start", "link", "edit") for t in texts):
            self.hit("destructive", "schedule_persistence", "system", "systemctl --user enable/start")
            return
        if base == "security" and any(SECURITY_READ_SUBS.match(t) for t in texts):
            self.hit("destructive", "keychain_access", "system", "agent không được đọc/xuất keychain (thông tin xác thực của người — R3-1)", forbidden=True)
            return
        if base == "security" and any(t.startswith("delete-") for t in texts):
            self.hit("destructive", "keychain_delete", "system", "xoá mục keychain")
            return
        if base == "gh" and len(texts) >= 2 and texts[0] == "auth" and (texts[1] in ("token", "refresh", "login", "setup-git") or "--show-token" in texts or "-t" in texts):
            self.hit("destructive", "forge_token_access", "system", "agent không được đọc/đặt token GitHub (R3-1)", forbidden=True)
            return
        if base.startswith("git-credential") or base in ("git-credential-osxkeychain", "git-credential-manager", "git-credential-store"):
            self.hit("destructive", "git_credential", "git", "agent không được gọi credential helper của git (R3-1)", forbidden=True)
            return
        if base == "tmutil" and any(t.startswith("delete") for t in texts):
            self.hit("destructive", "backup_delete", "system", "xoá bản sao lưu Time Machine")
            return
        if base in ("kill", "pkill", "killall") and ("-1" in texts and base == "kill"):
            self.hit("destructive", "kill_all", "system", "kill -1 (mọi tiến trình)")
            return
        if base in ("docker", "podman", "nerdctl"):
            j = " ".join(texts)
            if re.search(r"\b(volume\s+(rm|remove|prune)|system\s+prune|image\s+prune\s.*-a|builder\s+prune|container\s+prune|network\s+prune)\b", j) or re.search(r"\bcompose\b.*\bdown\b.*(-v\b|--volumes)", j) or re.search(r"\brm\b.*(-v\b|--volumes)", j):
                self.hit("destructive", "docker_data_delete", "system", "docker xoá volume/prune")
            else:
                self.hit("write", "container_cmd", None, "")
            return
        if base in ("kubectl", "oc", "helm"):
            if any(t in ("delete", "uninstall", "drain", "replace") for t in texts) or "--force" in texts and "apply" in texts:
                self.hit("destructive", "k8s_delete", "system", f"{base} delete/uninstall")
            else:
                self.hit("write", "k8s_cmd", None, "")
            return
        if base in ("terraform", "tofu", "pulumi", "cdk", "serverless", "sls"):
            if any(t in ("destroy", "-destroy", "--destroy", "rm", "remove") for t in texts) or ("state" in texts and "rm" in texts):
                self.hit("destructive", "iac_destroy", "system", f"{base} destroy")
            else:
                self.hit("write", "iac_cmd", None, "")
            return
        if base in CLOUD_CLIS:
            verbs = [t for t in texts if not t.startswith("-")]
            if any(DELETE_VERBS.match(t) for t in verbs):
                self.hit("destructive", "cloud_delete", "system", f"{base}: lệnh xoá tài nguyên cloud")
            else:
                self.hit("write", "cloud_cmd", None, "")
            return
        if base in ("npm", "yarn", "pnpm") and "unpublish" in texts:
            self.hit("destructive", "package_unpublish", "system", "unpublish gói")
            return
        if base in ("npm", "pnpm", "yarn", "bun"):
            if self.pkg_run(base, args):
                return
        # R2-2: chạy file theo ĐƯỜNG DẪN (`./scripts/deploy.sh`, `scripts/x`) → đọc nội dung như `bash x`
        name0 = words[0].text.lstrip("\\")
        if "/" in name0 and not words[0].dynamic:
            sp = self.path(words[0])
            if sp and os.path.isfile(sp) and not sp.startswith(("/usr/", "/bin/", "/sbin/", "/opt/homebrew/", "/System/")):
                try:
                    head = Path(sp).read_bytes()[:4]
                except OSError:
                    head = b""
                if head[:2] == b"#!" or sp.endswith((".sh", ".bash", ".zsh")):
                    self.script_file(words[0], "chạy script")
                    return
        if base in ("curl", "wget", "http", "xh"):
            # WEB-1: thu URL (đối số trông như URL) cho daemon kiểm SSRF/DNS/domain; metadata đám mây literal → CẤM với mọi agent
            from .url_safety import static_check as _url_static

            for i, t in enumerate(texts):
                if t.startswith("-") or shell.DYN in t or not re.match(r"^(https?://|[a-z0-9.\[\]-]+\.[a-z]{2,}(:\d+)?(/|$)|\d+\.\d+\.\d+\.\d+)", t, re.I):
                    continue
                u = t if "://" in t else "http://" + t
                # giá trị của tuỳ chọn (file ra, header, data…) không phải URL đích → không phân giải DNS; nhưng vẫn kiểm metadata
                # (curl `-O <url>` là cờ không nhận giá trị — không để nó thành lối tránh)
                if not (i and texts[i - 1] in _NET_ARG_FLAGS and "://" not in t):
                    self.state.setdefault("net_urls", []).append(u[:500])
                if _url_static(u).metadata:
                    self.hit("destructive", "ssrf_metadata", "network", f"{base} tới metadata đám mây ({t[:80]})", [t[:200]], forbidden=True)
                    return
            if self.ctx.manager_run:  # §9: điều phối viên không gọi dịch vụ loopback/LAN (web/MCP HTTP của hub) — sandbox là lớp chính
                from urllib.parse import urlsplit

                for t in texts:
                    if t.startswith("-") or not ("://" in t or ":" in t or "." in t or t.lower().startswith("localhost")):
                        continue  # bỏ giá trị của tuỳ chọn (`-m 3`) — chỉ xét đối số trông như URL/host
                    try:
                        host = urlsplit(t if "//" in t else "//" + t).hostname
                    except ValueError:
                        host = None
                    if host and local_host(host):
                        self.hit("destructive", "webfetch_local", "hub", f"{base} tới host nội bộ/loopback ({host})", forbidden=True)
                        return
            outs = [texts[i + 1] for i, t in enumerate(texts[:-1]) if t in ("-o", "--output", "-O", "--output-document")]
            for o in outs:
                p = self.path(o)
                if p:
                    self.check_overwrite_target(p, f"{base} -o")
            if any(t in ("-X", "--request") and i + 1 < len(texts) and texts[i + 1].upper() == "DELETE" for i, t in enumerate(texts)) or "-XDELETE" in texts:
                self.hit("destructive", "http_delete", "system", "HTTP DELETE")
            else:
                self.hit("write", "network_cmd", None, "")
            return
        if base in READ_ONLY_CMDS:
            if base == "echo" or base == "printf":
                return
            return
        # lệnh chung: kiểm đối số đường dẫn chạm tài nguyên được bảo vệ
        self.generic(base, args, unknown_targets)

    def windows_cmd(self, base: str, args: list[shell.Word], sc: shell.SimpleCommand) -> bool:
        """Lệnh Windows (agent gọi từ Git Bash của Claude Code trên Windows): Task Scheduler / dịch vụ / registry / WMI = lịch chạy
        hoặc tiến trình NGOÀI Job Object của run → `schedule_persistence`. `cmd /c …` phân tích thêm lệnh bên trong; PowerShell
        (kể cả `-EncodedCommand`) quét cmdlet tạo lịch/dịch vụ/WMI rồi vẫn đi tiếp luồng trình thông dịch cũ. True = đã xử lý xong."""
        wb = re.split(r"[\\/]", base)[-1].lower()
        wb = wb[:-4] if wb.endswith(".exe") else wb
        low = [a.text.lower() for a in args]
        if wb == "schtasks" and any(t[:1] in "/-" and t.lstrip("/-") in ("create", "change", "run", "delete", "end") for t in low):
            self.hit("destructive", "schedule_persistence", "system", f"schtasks tạo/sửa/chạy/xoá task Task Scheduler — {WIN_ESCAPE}")
            return True
        if wb == "sc" and low and low[0] in ("create", "config", "start", "stop", "delete", "failure"):
            self.hit("destructive", "schedule_persistence", "system", f"sc {low[0]}: tạo/sửa/chạy dịch vụ Windows — {WIN_ESCAPE}")
            return True
        if wb == "reg" and low and low[0] in ("add", "delete", "import", "restore", "load", "unload", "copy"):
            run_key = any(re.search(r"\\(run|runonce|winlogon)\b|\\services\\", t) for t in low)
            self.hit("destructive", "schedule_persistence" if run_key else "registry_write", "system",
                     f"reg {low[0]}: ghi registry" + (f" (mục tự khởi động/dịch vụ — {WIN_ESCAPE})" if run_key else " (cấu hình hệ thống ngoài workspace)"))
            return True
        if wb == "wmic" and "create" in low and "call" in low:
            self.hit("destructive", "schedule_persistence", "system", f"wmic … call create: tạo tiến trình qua WMI — {WIN_ESCAPE}")
            return True
        if wb == "cmd":
            for i, t in enumerate(low):
                if t in ("/c", "/k", "//c", "//k"):  # Git Bash đổi `/c` thành đường dẫn → người viết `//c`
                    rest = args[i + 1:]
                    if any(a.dynamic for a in rest):
                        self.hit("uncertain", "shell_c_dynamic", "system", "cmd /c với nội dung động")
                    self.merge(self.sub(" ".join(a.literal for a in rest)), "cmd /c")
                    break
            return False
        if wb in ("powershell", "pwsh"):
            code = " ".join(a.literal for a in args)
            for i, t in enumerate(low[:-1]):
                name = t.lstrip("-/")
                if t[:1] in "-/" and name and (name in ("e", "ec") or (len(name) >= 3 and "encodedcommand".startswith(name))):
                    try:
                        code += "\n" + base64.b64decode(args[i + 1].text + "===").decode("utf-16-le", errors="replace")
                    except (ValueError, binascii.Error):
                        self.hit("uncertain", "interp_dynamic_code", "system", f"{wb} -EncodedCommand không giải mã được")
            if WIN_PERSIST_CODE.search(code):
                self.hit("destructive", "schedule_persistence", "system", f"{wb}: cmdlet tạo lịch/dịch vụ/tiến trình WMI/mục Run — {WIN_ESCAPE}")
            if wb != base:  # `powershell.exe`, đường dẫn đầy đủ… → luồng trình thông dịch như `powershell`
                self.interpreter(wb, args, sc)
                return True
        return False

    _PKG_RUN_SUB = {"npm": ("run", "run-script", "rum", "urn"), "pnpm": ("run", "run-script"), "yarn": ("run",), "bun": ("run",)}
    _PKG_VAL_FLAGS = ("--prefix", "-C", "--dir", "--cwd", "--workspace", "-w", "--filter", "-F", "--loglevel")

    def pkg_run(self, base: str, args: list[shell.Word]) -> bool:
        """R2-2: `npm|pnpm|yarn|bun [cờ] run|run-script|rum <script>` (và `pnpm/yarn/bun <script>`) → giải script trong
        package.json của thư mục ĐÍCH (--prefix/-C/--cwd) rồi (1) khớp mẫu chặn repo trên dạng chuẩn `npm run <script>`,
        (2) phân loại THÂN script như lệnh shell (kể cả script agent mới thêm). Trả True nếu đã xử lý."""
        import json as _json

        texts = [a.text for a in args]
        target = self.cwd
        i = 0
        rest: list[str] = []
        while i < len(texts):
            t = texts[i]
            if t in self._PKG_VAL_FLAGS and i + 1 < len(texts):
                if t in ("--prefix", "-C", "--dir", "--cwd"):
                    target = self.path(args[i + 1]) or target
                i += 2
                continue
            if t.startswith(("--prefix=", "--dir=", "--cwd=")):
                target = self.path(t.split("=", 1)[1]) or target
                i += 1
                continue
            if t.startswith("-"):
                i += 1
                continue
            rest = texts[i:]
            break
        if not rest:
            return False
        sub = rest[0]
        script = None
        if sub in ("exec", "x", "dlx") and base in ("npm", "pnpm", "bun"):  # `npm exec -- tsx …` → phân loại lệnh bên trong
            inner = [x for x in rest[1:] if x != "--"]
            while inner and inner[0].startswith("-"):
                inner = inner[1:]
            if inner:
                self.merge(self.sub(" ".join(_q(x) for x in inner), cwd=target), f"{base} {sub}")
                return True
            return False
        if sub in self._PKG_RUN_SUB.get(base, ()):
            names = [x for x in rest[1:] if not x.startswith("-")]
            if not names or any(shell.DYN in x for x in names):
                # `echo deploy | xargs npm run`, `npm run "$(…)"` — không biết script nào
                self.hit("uncertain", "pkg_run_dynamic", None, f"{base} run với tên script động/không rõ")
                return True
            script = names[0]
        elif base in ("pnpm", "yarn", "bun") and sub not in ("install", "add", "remove", "i", "x", "dlx", "exec", "create", "init", "test", "build", "why", "info", "list", "ls", "outdated", "upgrade", "update"):
            script = sub
        if script is None:
            return False
        self.apply_repo_deny(f"npm run {script}", target)
        body = None
        try:
            pj = os.path.join(target or ".", "package.json")
            if os.path.isfile(pj):
                body = (_json.loads(Path(pj).read_text(encoding="utf-8")).get("scripts") or {}).get(script)
        except (OSError, ValueError):
            body = None
        if isinstance(body, str) and body.strip():
            self.merge(self.sub(body, cwd=target), f"{base} run {script}")
        self.hit("write", "pkg_run", None, f"{base} run {script}")
        return True

    def generic(self, base: str, args: list[shell.Word], unknown_targets: bool) -> None:
        for a in args:
            t = a.text
            if a.dynamic or not t or t.startswith("-") and "=" not in t:
                continue
            if "=" in t and t.startswith("-"):
                t = t.split("=", 1)[1]
            if not ("/" in t or t.startswith(".") or t.startswith("~")):
                continue
            p = self.path(t)
            if p:
                r = self.protected(p, "write")
                if r:
                    self.hit("destructive", "protected_touch", "file", f"{base} với đối số trong tài nguyên được bảo vệ {r.selector}", [p], r)
        self.hit("write", "generic_cmd", None, "")

    # -------------------------------------------------------------- groups
    def delete_cmd(self, base: str, args: list[shell.Word], unknown_targets: bool) -> None:
        targets, opts_done = [], False
        for a in args:
            if not opts_done and a.text == "--":
                opts_done = True
                continue
            if not opts_done and a.text.startswith("-") and len(a.text) > 1:
                continue
            targets.append(a)
        recursive = base != "unlink" and any(a.text.startswith("-") and not a.text.startswith("--") and ("r" in a.text or "R" in a.text) or a.text == "--recursive" for a in args)
        what = f"{base}{' -r' if recursive else ''}"
        if base in ("shred", "srm", "wipe", "gshred"):
            what = base
        if unknown_targets or not targets and base != "rm":
            self.hit("destructive", "delete_unknown_targets", "file", f"{what} với đích lấy từ stdin/xargs — không xác định được")
            return
        for a in targets:
            if a.dynamic:
                self.hit("uncertain", "delete_dynamic_target", "file", f"{what} với đích động ({a.literal or '$…'})", [a.literal])
                continue
            if a.glob and self.cwd is None and not _isabs(a.text):
                self.hit("uncertain", "delete_unknown_cwd", "file", f"{what} glob khi không biết thư mục hiện tại")
                continue
            exp = self.expand(a)
            if exp is None:
                self.hit("uncertain", "delete_unknown_cwd", "file", f"{what} đường dẫn tương đối khi không biết thư mục hiện tại", [a.literal])
                continue
            for p in exp:
                self.check_delete_target(p, what, recursive)

    def find_cmd(self, args: list[shell.Word]) -> None:
        texts = [a.text for a in args]
        roots: list[shell.Word] = []
        i = 0
        while i < len(args) and args[i].text in ("-H", "-L", "-P", "-E", "-X", "-s", "-x", "-d", "-f"):
            i += 2 if args[i].text == "-f" else 1
        while i < len(args) and not args[i].text.startswith(("-", "(", "!")):
            roots.append(args[i])
            i += 1
        if not roots:
            roots = [shell.Word(".")]
        root_paths = []
        for r in roots:
            p = self.path(r)
            if p is None:
                self.hit("uncertain", "find_dynamic_root", "file", "find với gốc động")
                return
            root_paths.append(p)
        deleting = "-delete" in texts
        j = i
        while j < len(args):
            t = args[j].text
            if t in ("-exec", "-execdir", "-ok", "-okdir"):
                k = j + 1
                sub: list[shell.Word] = []
                while k < len(args) and args[k].text not in (";", "\\;", "+"):
                    sub.append(args[k])
                    k += 1
                j = k + 1
                if not sub:
                    continue
                sbase = os.path.basename(sub[0].text)
                if sbase in DELETE_CMDS or sbase in ("shred", "truncate", "mv") or (sbase in SHELLS) or sbase in INTERPRETERS:
                    if sbase in SHELLS or sbase in INTERPRETERS:
                        script = " ".join(w.literal for w in sub[1:]).replace("{}", "__FIND_TARGET__")
                        v = self.sub(script)
                        if v.verdict in ("safe", "write") and "__FIND_TARGET__" not in script:
                            self.merge(v)
                            continue
                        if v.verdict in ("safe",):
                            continue
                    deleting = True
                else:
                    fake = shell.SimpleCommand([w if w.text != "{}" else shell.Word("__FIND_TARGET__") for w in sub])
                    self.words(list(fake.words), fake)
                continue
            if t in ("-fprint", "-fprintf", "-fls") and j + 1 < len(args):
                p = self.path(args[j + 1])
                if p:
                    self.check_overwrite_target(p, f"find {t}")
            j += 1
        if deleting:
            for p in root_paths:
                self.check_delete_target(p, "find -delete/-exec rm")

    def rsync(self, args: list[shell.Word]) -> None:
        texts = [a.text for a in args]
        paths = [a for a in args if not a.text.startswith("-")]
        dest = paths[-1] if len(paths) >= 2 else None
        dp = self.path(dest) if dest is not None and ":" not in dest.text else None
        if any(t.startswith("--delete") or t in ("--remove-source-files", "--del") for t in texts):
            if dest is not None and ":" in dest.text:
                self.hit("destructive", "rsync_delete", "file", "rsync --delete tới máy khác")
            elif dp is None:
                self.hit("uncertain", "rsync_dynamic", "file", "rsync --delete đích động")
            else:
                self.check_delete_target(dp, "rsync --delete")
            return
        if dp:
            srcs = [a for a in paths[:-1] if ":" not in a.text]
            for sw in srcs:
                sp = self.path(sw)
                if sp is None:
                    self.hit("uncertain", "rsync_dynamic", "file", "rsync nguồn động")
                elif os.path.isdir(sp):
                    self.check_tree_overwrite(sp, dp, "rsync", contents=sw.text.endswith("/"))
                elif os.path.isfile(sp):
                    t = os.path.join(dp, os.path.basename(sp)) if os.path.isdir(dp) else dp
                    if os.path.lexists(t) and not os.path.isdir(t) and not any(x in texts for x in ("--ignore-existing", "--update", "-u")):
                        self.check_overwrite_target(t, "rsync")
            self.check_write_target(dp, "rsync")
        else:
            self.hit("write", "rsync", "file", "rsync")

    def mv_cp(self, base: str, args: list[shell.Word], unknown_targets: bool) -> None:
        texts = [a.text for a in args]
        noclobber = any(t in ("-n", "--no-clobber", "-i", "--interactive", "--update=none") or (t.startswith("-") and not t.startswith("--") and "n" in t) for t in texts)
        tdir = None
        for i, t in enumerate(texts):
            if t in ("-t", "--target-directory") and i + 1 < len(texts):
                tdir = args[i + 1]
            elif t.startswith("--target-directory="):
                tdir = shell.Word(t.split("=", 1)[1])
        paths = [a for i, a in enumerate(args) if not a.text.startswith("-") and not (i > 0 and texts[i - 1] in ("-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "-g"))]
        if tdir is not None:
            srcs, dst = paths, tdir
        elif len(paths) >= 2:
            srcs, dst = paths[:-1], paths[-1]
        else:
            self.hit("write", base, "file", base)
            return
        is_mv = base in ("mv", "gmv")
        for s in srcs:
            sp = self.path(s)
            if sp is None:
                if is_mv:
                    self.hit("uncertain", "mv_dynamic", "file", "mv nguồn động")
                continue
            if is_mv:
                r = self.protected(sp, "delete")
                if r is not None:
                    self.hit("destructive", "protected_move", "file", f"mv tài nguyên được bảo vệ {r.selector}", [sp], r)
                elif _critical(sp, self.ctx.home):
                    self.hit("destructive", "move_critical", "system", f"mv thư mục hệ thống {sp}", [sp])
                else:
                    dp0 = self.path(dst)
                    root = canon_path(str(self.ctx.cwd)).rstrip("/")
                    if os.path.lexists(sp) and _under(sp, root) and dp0 is not None and not _under(dp0, root) and not created_by_run(sp, self.ctx):
                        # H-03: chuyển file/thư mục CÓ SẴN ra khỏi workspace = xoá khỏi repo của người
                        self.hit("destructive", "move_out_existing", "file", f"mv {sp} ra ngoài workspace (file có sẵn)", [sp])
        dp = self.path(dst)
        if dp is None:
            self.hit("uncertain", f"{base}_dynamic_dest", "file", f"{base} đích động")
            return
        if dp == "/dev/null":
            if is_mv:
                self.hit("destructive", "mv_devnull", "file", "mv vào /dev/null")
            return
        recursive = any(t in ("-r", "-R", "--recursive", "-a", "--archive") or (t.startswith("-") and not t.startswith("--") and any(c in t[1:] for c in "rRa")) for t in texts) or base == "ditto"
        if not is_mv and recursive and not noclobber:
            # H-03: nguồn là thư mục → duyệt file đích đã tồn tại (`cp -r src/. docs/` ghi đè docs/*)
            for s in srcs:
                sp = self.path(s)
                if sp and os.path.isdir(sp):
                    contents = s.text.rstrip("/").endswith("/.") or base == "ditto" or not os.path.isdir(dp)
                    self.check_tree_overwrite(sp, dp, f"{base} -r", contents=contents)
        if os.path.isdir(dp):
            for s in srcs:
                name = os.path.basename(s.literal.rstrip("/"))
                if not name or name == ".":
                    continue
                target = os.path.join(dp, name)
                if os.path.lexists(target) and not os.path.isdir(target) and not noclobber:
                    self.check_overwrite_target(target, base)
                else:
                    self.check_write_target(target, base)
            return
        if noclobber:
            self.check_write_target(dp, base)
        else:
            self.check_overwrite_target(dp, base)

    def ln_cmd(self, args: list[shell.Word]) -> None:
        """`ln [-s] [-f] TARGET [LINK]` (H-02): đích của symlink trong vùng bảo vệ → phá huỷ; nhớ symlink cho lệnh sau."""
        texts = [a.text for a in args]
        force = any(t.startswith("-") and not t.startswith("--") and "f" in t[1:] for t in texts) or "--force" in texts
        symbolic = any(t.startswith("-") and not t.startswith("--") and "s" in t[1:] for t in texts) or "--symbolic" in texts
        tdir = None
        for i, t in enumerate(texts):
            if t in ("-t", "--target-directory") and i + 1 < len(args):
                tdir = args[i + 1]
        paths = [a for i, a in enumerate(args) if not a.text.startswith("-") and not (i > 0 and texts[i - 1] in ("-t", "--target-directory", "-S", "--suffix"))]
        if not paths:
            return
        srcs = paths if tdir is not None else (paths[:-1] if len(paths) >= 2 else paths)
        for src in srcs:
            if src.dynamic:
                self.hit("uncertain", "ln_dynamic", "file", "ln nguồn động")
                continue
            raw = src.text
            if symbolic and not _isabs(os.path.expanduser(raw)):
                tgt = None  # đích tương đối tính theo thư mục của link — tính sau khi biết link
            else:
                tgt = self.path(raw)
            if tgt is not None:
                r = self.protected(tgt, "write")
                if r is not None or _under(tgt, _hub_home(self.ctx)) and not self._hub_workspace(tgt) or sensitive_read(tgt, self.ctx):
                    self.hit("destructive", "symlink_to_protected", "file", f"ln trỏ vào tài nguyên được bảo vệ / HUB_HOME ({tgt})", [tgt], r)
        if tdir is not None:
            dpath = self.path(tdir)
            links = [_jn(os.path.join(dpath, os.path.basename(a.text.rstrip("/")))) for a in srcs] if dpath else []
        elif len(paths) >= 2:
            dpath = self.path(paths[-1])
            if dpath is None:
                self.hit("uncertain", "ln_dynamic", "file", "ln đích động")
                return
            links = [_jn(os.path.join(dpath, os.path.basename(paths[0].text.rstrip("/"))))] if os.path.isdir(dpath) and not force else [dpath]
        else:
            links = [_jn(os.path.join(self.cwd or canon_path(str(self.ctx.cwd)), os.path.basename(paths[0].text.rstrip("/"))))]
        for link in links:
            if link is None:
                continue
            if force:
                self.check_overwrite_target(link, "ln -f")
            else:
                self.check_write_target(link, "ln")
            src = srcs[0]
            if not src.dynamic:
                raw = os.path.expanduser(src.text)
                tgt = _abs(raw, os.path.dirname(link) if symbolic else (self.cwd or canon_path(str(self.ctx.cwd)))) if IS_WINDOWS else \
                    os.path.normpath(raw if os.path.isabs(raw) else os.path.join(os.path.dirname(link) if symbolic else (self.cwd or str(self.ctx.cwd)), raw))
                tgt = self.follow_links(tgt)
                if symbolic:
                    r = self.protected(tgt, "write")
                    if r is not None or (_under(tgt, _hub_home(self.ctx)) and not self._hub_workspace(tgt)):
                        self.hit("destructive", "symlink_to_protected", "file", f"symlink {link} → {tgt} (vùng bảo vệ)", [tgt], r)
                self.state.setdefault("links", {})[_norm(link)] = tgt

    def extract(self, base: str, args: list[shell.Word]) -> None:
        """tar -x / unzip (H-03): liệt kê archive (nếu có) và kiểm file đích đã tồn tại sẽ bị ghi đè."""
        import tarfile
        import zipfile

        texts = [a.text for a in args]
        dest = self.cwd or canon_path(str(self.ctx.cwd))
        archive: str | None = None
        if base == "unzip":
            if any(t in ("-l", "-v", "-t", "-Z", "-p", "-n") for t in texts):
                self.hit("safe" if "-n" not in texts else "write", "archive_list", None, "")
                return
            for i, t in enumerate(texts):
                if t == "-d" and i + 1 < len(args):
                    d = self.path(args[i + 1])
                    dest = d or dest
            pos = [a for i, a in enumerate(args) if not a.text.startswith("-") and not (i > 0 and texts[i - 1] == "-d")]
            archive = self.path(pos[0]) if pos else None
        else:
            flags = "".join(t.lstrip("-") for t in texts if t.startswith("-") and not t.startswith("--")) + ("x" if texts and not texts[0].startswith("-") and "x" in texts[0] else "")
            if "x" not in flags and "--extract" not in texts and "--get" not in texts:
                self.hit("write", "archive_create_or_list", None, "")
                return
            for i, t in enumerate(texts):
                if t in ("-C", "--directory") and i + 1 < len(args):
                    dest = self.path(args[i + 1]) or dest
                elif t.startswith("--directory="):
                    dest = self.path(t.split("=", 1)[1]) or dest
                elif t == "-f" and i + 1 < len(args):
                    archive = self.path(args[i + 1])
                elif t.startswith("-") and not t.startswith("--") and t.endswith("f") and i + 1 < len(args):
                    archive = self.path(args[i + 1])
            if archive is None and texts and not texts[0].startswith("-") and "f" in texts[0] and len(args) > 1:
                archive = self.path(args[1])
        names: list[str] | None = None
        try:
            if archive and os.path.isfile(archive) and os.path.getsize(archive) < 200_000_000:
                if base == "unzip" or zipfile.is_zipfile(archive):
                    with zipfile.ZipFile(archive) as z:
                        names = z.namelist()[: self.ctx.max_walk + 1]
                else:
                    with tarfile.open(archive) as tf:
                        names = [m.name for m in tf.getmembers()[: self.ctx.max_walk + 1] if not m.isdir()]
        except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError):
            names = None
        if names is None:
            self.check_write_target(dest, f"{base} giải nén")
            return
        if len(names) > self.ctx.max_walk:
            self.hit("uncertain", "archive_too_big", "file", f"{base}: archive quá lớn để kiểm ghi đè")
            return
        for n in names:
            t = _norm(os.path.join(dest, n))
            if n.startswith("/") or ".." in n.split("/") or (IS_WINDOWS and (_isabs(n) or ".." in n.replace("\\", "/").split("/"))):
                self.hit("destructive", "archive_path_escape", "file", f"{base}: mục archive thoát khỏi thư mục đích ({n})", [t])
            elif os.path.lexists(t) and not os.path.isdir(t):
                self.check_overwrite_target(t, f"{base} giải nén")
            else:
                r = self.protected(t, "write")
                if r is not None:
                    self.hit("destructive", "protected_write", "file", f"{base} giải nén vào tài nguyên được bảo vệ {r.selector}", [t], r)
        self.check_write_target(dest, f"{base} giải nén")

    def check_tree_overwrite(self, src: str, dst: str, what: str, contents: bool) -> None:
        """cp -r / rsync / ditto với nguồn là THƯ MỤC (H-03): mọi file đích ĐÃ CÓ sẽ bị ghi đè → kiểm từng file."""
        base = dst if contents else os.path.join(dst, os.path.basename(src.rstrip("/")))
        n = 0
        for root, dirs, files in os.walk(src):
            for name in files:
                n += 1
                if n > self.ctx.max_walk:
                    self.hit("uncertain", "tree_too_big", "file", f"{what}: thư mục nguồn quá lớn để kiểm ghi đè")
                    return
                rel = os.path.relpath(os.path.join(root, name), src)
                t = os.path.join(base, rel)
                if os.path.lexists(t) and not os.path.isdir(t):
                    self.check_overwrite_target(t, what)
                else:
                    r = self.protected(t, "write")
                    if r is not None:
                        self.hit("destructive", "protected_write", "file", f"{what} vào tài nguyên được bảo vệ {r.selector}", [t], r)
        self.check_write_target(base, what)

    def script_file(self, w: shell.Word | None, how: str) -> None:
        if w is None:
            self.hit("uncertain", "script_stdin", "system", f"{how} không rõ script")
            return
        p = self.path(w)
        made = (self.state.get("files") or {})
        if p is not None and p in made:
            if made[p] is None:
                self.hit("uncertain", "script_unknown", "system", f"{how} chạy script tạo trong cùng lệnh với nội dung động ({w.literal})")
            else:
                self.merge(self.sub(made[p]), f"script {os.path.basename(p)} (tạo trong cùng lệnh)")
            return
        if p is None or not os.path.isfile(p):
            self.hit("uncertain", "script_unknown", "system", f"{how} chạy script không đọc được trước ({w.literal})")
            return
        try:
            if os.path.getsize(p) > 512_000:
                self.hit("uncertain", "script_too_big", "system", f"script {p} quá lớn để phân tích")
                return
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            self.hit("uncertain", "script_unreadable", "system", f"không đọc được script {p}")
            return
        self.merge(self.sub(text), f"script {os.path.basename(p)}")

    def stdin_source(self, sc: shell.SimpleCommand) -> tuple[str | None, str]:
        """Nội dung stdin của lệnh nếu biết: (text|None, mô tả)."""
        if sc.stdin_text is not None:
            return (None, "here-string động") if sc.stdin_dynamic else (sc.stdin_text, "heredoc")
        for op, w in sc.redirects:
            if op == "<":
                p = self.path(w)
                if p and os.path.isfile(p):
                    try:
                        return Path(p).read_text(encoding="utf-8", errors="replace")[:512_000], f"file {p}"
                    except OSError:
                        return None, "file không đọc được"
                return None, "file chưa có"
        src = sc.pipe_from
        if src is None:
            return "", "không có stdin"
        if not src.words:
            return None, "pipe"
        b = os.path.basename(src.words[0].text)
        if b in ("echo", "printf") and not any(w.dynamic for w in src.words[1:]):
            parts = [w.text for w in src.words[1:] if not (b == "echo" and w.text in ("-e", "-n", "-E"))]
            txt = " ".join(parts)
            return (shell.decode_printf(txt) if b == "printf" or "-e" in [w.text for w in src.words] else txt), "echo"
        if b == "cat" and len(src.words) == 2:
            p = self.path(src.words[1])
            if p and os.path.isfile(p):
                try:
                    return Path(p).read_text(encoding="utf-8", errors="replace")[:512_000], f"cat {p}"
                except OSError:
                    pass
        if b in ("curl", "wget", "fetch", "http", "xh", "nc", "ncat"):
            return None, "download"
        return None, f"pipe từ {b}"

    def shell_cmd(self, base: str, args: list[shell.Word], sc: shell.SimpleCommand) -> None:
        i = 0
        script: shell.Word | None = None
        file_arg: shell.Word | None = None
        while i < len(args):
            t = args[i].text
            if t == "-c" or (t.startswith("-") and not t.startswith("--") and "c" in t[1:] and base != "busybox"):
                script = args[i + 1] if i + 1 < len(args) else None
                break
            if t in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
                i += 2
                continue
            if t.startswith("-") or t.startswith("+"):
                i += 1
                continue
            if base == "busybox" and t in ("sh", "ash"):
                i += 1
                continue
            file_arg = args[i]
            break
        if script is not None:
            if script.dynamic:
                self.hit("uncertain", "shell_c_dynamic", "system", f"{base} -c với nội dung động (vd $(curl …)/base64)")
            self.merge(self.sub(script.literal), f"{base} -c")
            return
        if file_arg is not None:
            self.script_file(file_arg, base)
            return
        text, how = self.stdin_source(sc)
        if text is None:
            if how == "download":
                self.hit("destructive", "download_and_execute", "system", f"tải về rồi chạy bằng {base} (curl | sh)")
            else:
                self.hit("uncertain", "shell_stdin_unknown", "system", f"{base} đọc lệnh từ {how} — không phân tích được")
            return
        self.merge(self.sub(text), f"{base} (stdin {how})")

    def interpreter(self, base: str, args: list[shell.Word], sc: shell.SimpleCommand) -> None:
        code: str | None = None
        dyn = False
        file_arg: shell.Word | None = None
        i = 0
        while i < len(args):
            t = args[i].text
            if t in ("-c", "-e", "-E", "--eval", "-r", "-p", "--print", "-x") or (base.startswith("perl") and re.fullmatch(r"-[a-zA-Z]*e", t)):
                if i + 1 < len(args):
                    code = args[i + 1].literal
                    dyn = args[i + 1].dynamic
                break
            if t == "-m":
                mod = args[i + 1].text if i + 1 < len(args) else ""
                if mod.split(".")[0] in _names.HUB_MODULE_NAMES or shell.DYN in mod:
                    # H-01: `python -m cfharness.cli approve …` — mọi module của hub từ trong run là forbidden
                    self.hit("destructive", "hub_module", "hub", f"agent không được chạy module nội bộ của hub ({mod or '?'})", forbidden=True)
                elif mod in ("shutil", "os"):
                    self.hit("uncertain", "python_module", "system", f"{base} -m {mod}")
                else:
                    self.hit("write", "python_module", None, "")
                return
            if t.startswith("-"):
                i += 1
                continue
            file_arg = args[i]
            break
        inline = code is not None
        if code is None and file_arg is not None:
            p = self.path(file_arg)
            made = self.state.get("files") or {}
            if p and p in made:
                if made[p] is None:
                    self.hit("uncertain", "script_unknown", "system", f"{base} chạy script tạo trong cùng lệnh với nội dung động")
                    return
                code = made[p]
            elif p and os.path.isfile(p):
                try:
                    code = Path(p).read_text(encoding="utf-8", errors="replace")[:512_000]
                except OSError:
                    code = None
            later = [a.text for a in args[i + 1 :]]
            if os.path.basename(file_arg.text) == "manage.py" and any(t in ("flush", "reset_db", "sqlflush") for t in later):
                self.hit("destructive", "framework_db_reset", "db", "manage.py flush/reset_db xoá dữ liệu")
                return
            if code is None:
                if p is not None and not os.path.exists(p) and not file_arg.text.startswith("-"):
                    # file chưa có lúc kiểm — có thể được tạo/tải ngay trong lệnh này → không phân tích được
                    self.hit("uncertain", "script_unknown", "system", f"{base} chạy script chưa tồn tại ({file_arg.literal})")
                else:
                    self.hit("write", "script_run", None, f"{base} chạy {file_arg.literal}")
                return
        if code is None:
            text, how = self.stdin_source(sc)
            if text is None:
                if how == "download":
                    self.hit("destructive", "download_and_execute", "system", f"tải về rồi chạy bằng {base}")
                elif how != "không có stdin":
                    self.hit("uncertain", "interp_stdin_unknown", "system", f"{base} đọc mã từ {how}")
                return
            code = text
            inline = True
        if dyn:
            self.hit("uncertain", "interp_dynamic_code", "system", f"{base} với mã động")
        self.scan_code(base, code, inline=inline)

    def scan_code(self, base: str, code: str, inline: bool = True) -> None:
        """Quét mã trình thông dịch (H-01/H-02/H-03). `inline` = mã nằm ngay trong lệnh (-c/-e/stdin) — đích ghi không
        giải được thì `uncertain`; với file script của run, ghi đích động coi là ghi thường."""
        # --- mặt điều khiển hub (H-01): socket/token/module nội bộ → forbidden; HUB_HOME → giữ chờ người
        if KEYCHAIN_CODE_RE.search(code):
            self.hit("destructive", "keychain_access", "system", f"mã {base} truy cập keychain/credential helper (R3-1)", forbidden=True)
        if HUB_SOCKET_RE.search(code) or (self.ctx.hub_socket and _contains(code, self.ctx.hub_socket)):
            self.hit("destructive", "hub_socket", "hub", f"mã {base} đụng socket/token điều khiển hub", forbidden=True)
        if HUB_MODULE_RE.search(code):
            self.hit("destructive", "hub_module", "hub", f"mã {base} dùng module nội bộ cfharness (client RPC/CLI)", forbidden=True)
        if HUB_HOME_REF_RE.search(code):
            self.hit("destructive", "hub_home_in_code", "policy", f"mã {base} tham chiếu HUB_HOME — chỉ người được đụng dữ liệu/chính sách hub")
        if WIN_PERSIST_CODE.search(code):
            self.hit("destructive", "schedule_persistence", "system", f"mã {base} tạo lịch/dịch vụ/tiến trình WMI/mục Run của Windows — {WIN_ESCAPE}")
        if UNIX_SOCKET_RE.search(code):
            self.hit("uncertain", "unix_socket_in_code", "hub", f"mã {base} kết nối Unix socket — không xác định được đích")
        rx = {"node": JS_DESTRUCTIVE, "nodejs": JS_DESTRUCTIVE, "deno": JS_DESTRUCTIVE, "bun": JS_DESTRUCTIVE}.get(base)
        if rx is None:
            rx = PL_DESTRUCTIVE if base.startswith(("perl", "ruby", "osascript", "php")) else PY_DESTRUCTIVE
        if rx.search(code) or (rx is not PY_DESTRUCTIVE and PY_DESTRUCTIVE.search(code) and base.startswith("python")):
            self.hit("destructive", "code_delete_api", "file", f"mã {base} gọi API xoá file")
        if base == "osascript" and re.search(r"(?i)keychain|security\s+find|password", code):
            self.hit("destructive", "keychain_access", "system", "AppleScript truy cập keychain/mật khẩu (R3-1)", forbidden=True)
        if base == "osascript" and re.search(r"(?i)\b(delete|empty\s+(the\s+)?trash|move\b.*\bto\s+(the\s+)?trash)\b", code):
            self.hit("destructive", "applescript_delete", "file", "AppleScript xoá/chuyển vào thùng rác")
        s = sql_destructive(code) if re.search(r"(?i)\b(drop|truncate|delete|update|flushall|flushdb)\b", code) else None
        if s:
            self.hit("destructive", s, "db", f"mã {base} chứa SQL phá huỷ")
        # --- literal đường dẫn tới tài nguyên được bảo vệ / HUB_HOME (H-02): không cần biết mã làm gì với nó
        lits = [m.group(1) if m.group(1) is not None else m.group(2) for m in STR_LITERAL.finditer(code)]
        lits += [m.group(1) for m in JS_TEMPLATE.finditer(code)]
        for lit in lits:
            if not lit or len(lit) > 4096 or "\n" in lit:
                continue
            if not ("/" in lit or lit.startswith(("~", "."))):
                continue
            pth = self.path(lit.replace("\\\\", "\\"))
            if pth is None:
                continue
            if sensitive_read(pth, self.ctx):
                self.hit("destructive", "hub_secret_read", "hub", f"mã {base} đụng token/secret của hub ({lit})", [pth], forbidden=True)
                continue
            r = self.protected(pth, "write")
            if r is None and _under(pth, _hub_home(self.ctx)) and not self._hub_workspace(pth):
                r = ProtectedResource("hub-home", "hub", _hub_home(self.ctx), note="HUB_HOME", builtin=True)
            if r is not None:
                self.hit("destructive", "protected_literal_in_code", "policy" if r.id.startswith("hub") else "file",
                         f"mã {base} chứa đường dẫn tài nguyên được bảo vệ {r.selector} ({lit})", [pth], r)
        # --- API ghi file (H-03): đích literal → kiểm ghi đè; đích động → uncertain (mã inline)
        self.code_writes(base, code, inline)
        # --- lệnh shell bên trong mã: os.system/execSync("…"), subprocess([...]), spawn('rm', [...])
        if EXEC_DYNAMIC.search(code):
            found = False
            for m in STR_LITERAL.finditer(code):
                lit = m.group(1) if m.group(1) is not None else m.group(2)
                if not lit or len(lit) > 4000:
                    continue
                if re.match(r"^\s*(rm|git|find|dd|mkfs|shred|truncate|chmod|chown|psql|mysql|sqlite3|sh|bash|zsh|curl|sudo|mv|cp|ln|tee|install|rsync|"
                            rf"unlink|{_CLI}|python[\d.]*|node|perl|ruby|nc|socat|xargs|kill|pkill|docker|kubectl|terraform|aws|gh|npm|pnpm|yarn|bun|npx|tsx|launchctl|make|"
                            r"schtasks|sc|reg|wmic|cmd|powershell|pwsh)\b", lit):
                    found = True
                    self.merge(self.sub(lit), f"lệnh trong mã {base}")
            for m in LIST_ARGV_RE.finditer(code):
                items = [x.group(1) if x.group(1) is not None else x.group(2) for x in STR_LITERAL.finditer(m.group(1))]
                if items and re.match(r"^[\w./-]+$", items[0] or ""):
                    found = True
                    self.merge(self.sub(" ".join(_q(x) for x in items)), f"argv trong mã {base}")
            for m in JS_SPAWN_RE.finditer(code):
                items = [m.group(2)] + [x.group(1) if x.group(1) is not None else x.group(2) for x in STR_LITERAL.finditer(m.group(3))]
                found = True
                self.merge(self.sub(" ".join(_q(x) for x in items)), f"spawn trong mã {base}")
            if re.search(r"b64decode|base64|codecs\.decode|fromCharCode|\bexec\(|\beval\(|__import__", code, re.I):
                self.hit("uncertain", "code_obfuscated", "system", f"mã {base} giải mã/exec động")
            elif not found and re.search(r"os\.system|subprocess|execSync|child_process|spawn|popen|system\(", code, re.I):
                if re.search(r"""(os\.system|subprocess\.\w+|execSync|exec|spawn|popen|system)\s*\(\s*[A-Za-z_]""", code):
                    self.hit("uncertain", "code_exec_dynamic", "system", f"mã {base} chạy lệnh shell từ biến")
        self.hit("write", "interpreter", None, "")

    def _hub_workspace(self, p: str) -> bool:
        """Đường dẫn trong workspace scratch của agent (HUB_HOME/agents/<id>/workspaces/…) — vùng agent được ghi."""
        rel = _rel_parts(p, _hub_home(self.ctx))
        if IS_WINDOWS:
            rel = [x.lower() for x in rel]
        return len(rel) >= 3 and rel[0] == "agents" and rel[2] == "workspaces"

    def code_writes(self, base: str, code: str, inline: bool) -> None:
        L = _L
        found_dynamic = False

        def lit(m: re.Match[str], g: int) -> str:
            return m.group(g) if m.group(g) is not None else m.group(g + 1)

        def target(t: str, op: str, what: str) -> None:
            pth = self.path(t)
            if pth is None:
                self.hit("uncertain", "code_write_dynamic", "file", f"mã {base}: {what} tới đích không xác định")
            elif op == "overwrite":
                self.check_overwrite_target(pth, f"mã {base}: {what}")
            elif op == "delete":
                r = self.protected(pth, "delete")
                if r is not None:
                    self.hit("destructive", "protected_move", "file", f"mã {base}: {what} tài nguyên được bảo vệ {r.selector}", [pth], r)
                elif not created_by_run(pth, self.ctx):
                    self.hit("destructive", "code_move_existing", "file", f"mã {base}: {what} file có sẵn {pth}", [pth])
            else:
                self.check_write_target(pth, f"mã {base}: {what}")

        # Python open(path, mode) / io.open / codecs.open
        for m in re.finditer(r"\b(?:io\.|codecs\.)?open\s*\(\s*" + L + r"\s*,\s*(?:mode\s*=\s*)?" + L, code):
            mode = lit(m, 3)
            if any(c in mode for c in "wx"):
                target(lit(m, 1), "overwrite", f"open(..,'{mode}')")
            elif any(c in mode for c in "a+"):
                target(lit(m, 1), "write", f"open(..,'{mode}')")
        for m in re.finditer(r"\b(?:io\.)?open\s*\(\s*([A-Za-z_][\w.\[\]'\"]*|f['\"][^'\"]*['\"])\s*,\s*(?:mode\s*=\s*)?['\"]([rwxab+t]+)['\"]", code):
            if any(c in m.group(2) for c in "wxa+"):
                found_dynamic = True
        # pathlib: Path('x').write_text/write_bytes/touch/rename/replace/symlink_to/open('w')
        for m in re.finditer(r"\bPath\s*\(\s*" + L + r"\s*\)\s*\.\s*(write_text|write_bytes|touch|rename|replace|symlink_to|hardlink_to|open)\s*\(\s*(?:" + L + r")?", code):
            meth = m.group(3)
            if meth in ("rename", "replace"):
                target(lit(m, 1), "delete", f"Path.{meth}")
                if m.group(4) is not None or m.group(5) is not None:
                    target(lit(m, 4), "overwrite", f"Path.{meth} đích")
            elif meth == "open":
                mode = lit(m, 4) or "r"
                if any(c in mode for c in "wxa+"):
                    target(lit(m, 1), "overwrite" if any(c in mode for c in "wx") else "write", f"Path.open('{mode}')")
            elif meth == "symlink_to":
                target(lit(m, 1), "write", "Path.symlink_to")
            else:
                target(lit(m, 1), "overwrite", f"Path.{meth}")
        if re.search(r"\.(write_text|write_bytes)\s*\(", code) and not re.search(r"\bPath\s*\(\s*['\"][^'\"]*['\"]\s*\)\s*\.\s*write_(text|bytes)", code):
            found_dynamic = True
        # shutil / os: copy, move, rename, replace, link
        for m in re.finditer(r"\b(shutil\.(?:copy|copy2|copyfile|copytree|move)|os\.(?:rename|replace|renames|link|symlink))\s*\(\s*(?:" + L + r"|[^,()]+)\s*,\s*(?:" + L + r"|([^,()]+))", code):
            fn = m.group(1)
            if fn.endswith(("move", "rename", "replace", "renames")) and (m.group(2) is not None or m.group(3) is not None):
                target(lit(m, 2), "delete", fn)
            if m.group(4) is not None or m.group(5) is not None:
                target(lit(m, 4), "overwrite", fn)
            else:
                found_dynamic = True
        # Node: fs.writeFileSync/appendFileSync/createWriteStream/copyFileSync/renameSync/cpSync/symlinkSync…
        for m in re.finditer(r"\b(writeFileSync|writeFile|appendFileSync|appendFile|createWriteStream|outputFileSync|outputFile|writeJsonSync|writeJSONSync)\s*\(\s*(?:" + L + r"|([^,()]+))", code):
            if m.group(4) is not None:
                found_dynamic = True
            else:
                target(lit(m, 2), "write" if m.group(1).startswith("append") else "overwrite", m.group(1))
        for m in re.finditer(r"\b(copyFileSync|copyFile|cpSync|renameSync|rename|symlinkSync|linkSync|moveSync|copySync)\s*\(\s*(?:" + L + r"|[^,()]+)\s*,\s*(?:" + L + r"|([^,()]+))", code):
            fn = m.group(1)
            if fn.startswith(("rename", "move")) and (m.group(2) is not None or m.group(3) is not None):
                target(lit(m, 2), "delete", fn)
            if m.group(6) is not None:
                found_dynamic = True
            elif m.group(4) is not None or m.group(5) is not None:
                target(lit(m, 4), "overwrite", fn)
        for m in re.finditer(r"\b(Deno\.writeTextFile(?:Sync)?|Deno\.writeFile(?:Sync)?|Bun\.write|file_put_contents)\s*\(\s*(?:" + L + r"|([^,()]+))", code):
            if m.group(4) is not None:
                found_dynamic = True
            else:
                target(lit(m, 2), "overwrite", m.group(1))
        # Ruby: File.write / IO.write / File.open(x,'w') / FileUtils.cp|mv|install
        for m in re.finditer(r"\b(?:File|IO)\.(write|binwrite)\s*\(\s*(?:" + L + r"|([^,()]+))", code):
            if m.group(4) is not None:
                found_dynamic = True
            else:
                target(lit(m, 2), "overwrite", f"File.{m.group(1)}")
        for m in re.finditer(r"\b(?:File\.open|fopen)\s*\(\s*" + L + r"\s*,\s*" + L, code):
            mode = lit(m, 3)
            if any(c in mode for c in "wxa+"):
                target(lit(m, 1), "overwrite" if any(c in mode for c in "wx") else "write", f"open('{mode}')")
        for m in re.finditer(r"\bFileUtils\.(cp|cp_r|mv|move|copy|install|ln_s|ln_sf)\s*\(\s*(?:" + L + r"|[^,()]+)\s*,\s*(?:" + L + r"|([^,()]+))", code):
            if m.group(1) in ("mv", "move") and (m.group(2) is not None or m.group(3) is not None):
                target(lit(m, 2), "delete", f"FileUtils.{m.group(1)}")
            if m.group(6) is not None:
                found_dynamic = True
            else:
                target(lit(m, 4), "overwrite", f"FileUtils.{m.group(1)}")
        # Perl: open(F, '>x') / open(my $f, '>', 'x')
        for m in re.finditer(r"\bopen\s*\(?\s*(?:my\s+)?[\$\w]+\s*,\s*['\"]\s*(>>|>|\+<|\+>)\s*([^'\"]+)['\"]", code):
            target(m.group(2).strip(), "write" if m.group(1) == ">>" else "overwrite", f"open('{m.group(1)}')")
        for m in re.finditer(r"\bopen\s*\(?\s*(?:my\s+)?[\$\w]+\s*,\s*['\"](>>|>|\+<|\+>)['\"]\s*,\s*(?:" + L + r"|([^,()]+))", code):
            if m.group(4) is not None:
                found_dynamic = True
            else:
                target(lit(m, 2), "write" if m.group(1) == ">>" else "overwrite", f"open('{m.group(1)}')")
        if found_dynamic and inline:
            self.hit("uncertain", "code_write_dynamic", "file", f"mã {base} ghi file tới đích không xác định (biến)")

    def db_client(self, base: str, args: list[shell.Word], sc: shell.SimpleCommand) -> None:
        texts = [a.literal for a in args]
        if base in ("dropdb", "dropuser"):
            self.hit("destructive", "sql_drop", "db", f"{base} xoá CSDL/người dùng")
            return
        if base == "mysqladmin" and any(t in ("drop", "DROP") for t in texts):
            self.hit("destructive", "sql_drop", "db", "mysqladmin drop")
            return
        if base in ("pg_restore", "mongorestore") and any(t in ("--clean", "-c", "--drop") for t in texts):
            self.hit("destructive", "db_restore_clean", "db", f"{base} --clean/--drop ghi đè CSDL")
            return
        sql_parts = [t for t in texts]
        for a in args:
            if not a.dynamic and ("/" in a.text or a.text.startswith(".")) and not a.text.startswith("-"):
                p = self.path(a)
                r = self.protected(p, "write") if p else None
                if r is not None:
                    self.hit("destructive", "protected_db_file", "db", f"{base} mở file được bảo vệ {r.selector}", [p], r)
                    return
        for i, t in enumerate(texts):
            if t in ("-f", "--file", "-i", "--init") and i + 1 < len(texts):
                p = self.path(texts[i + 1])
                if p and os.path.isfile(p):
                    try:
                        sql_parts.append(Path(p).read_text(encoding="utf-8", errors="replace")[:512_000])
                    except OSError:
                        self.hit("uncertain", "sql_file_unreadable", "db", "không đọc được file SQL")
                else:
                    self.hit("uncertain", "sql_file_unknown", "db", f"{base} -f file SQL chưa có")
            if t.startswith(".read "):
                self.hit("uncertain", "sql_file_unknown", "db", "sqlite .read")
        if any(a.dynamic for a in args):
            self.hit("uncertain", "sql_dynamic", "db", f"{base} với tham số động")
        text, how = self.stdin_source(sc)
        if text is None:
            self.hit("uncertain", "sql_stdin_unknown", "db", f"{base} đọc SQL từ {how}")
        else:
            sql_parts.append(text)
        joined = "\n".join(sql_parts)
        if base == "redis-cli":
            up = [t.upper() for t in texts]
            if any(t in ("FLUSHALL", "FLUSHDB", "DEL", "UNLINK") for t in up) or re.search(r"(?im)^\s*(FLUSHALL|FLUSHDB|DEL|UNLINK)\b", text or ""):
                self.hit("destructive", "redis_delete", "db", "redis-cli xoá khoá/flush")
                return
        rule = sql_destructive(joined)
        if rule:
            self.hit("destructive", rule, "db", f"{base}: SQL phá huỷ ({rule})")
            return
        prot = self.ctx.policy.match_db(joined)
        if prot is not None and re.search(r"(?i)\b(insert|update|delete|alter|create|drop|replace|merge|upsert)\b", joined):
            self.hit("destructive", "protected_db_write", "db", f"ghi vào CSDL được bảo vệ {prot.selector}", prot=prot)
            return
        self.hit("write", "db_client", "db", base)

    def hub_cli(self, texts: list[str], unknown: bool = False) -> None:
        t: list[str] = []
        skip = False
        for x in texts:  # bỏ cờ toàn cục (`--home X`, `--json`)
            if skip:
                skip = False
                continue
            if x == "--home":
                skip = True
                continue
            if x.startswith("--home=") or x == "--json":
                continue
            t.append(x)
        sub = t[0] if t else ""
        if unknown or any(shell.DYN in x or x == "__FIND_TARGET__" for x in t):
            # H-01: đối số lấy từ stdin/xargs/find -exec/biến → không biết lệnh gì → coi như tự duyệt
            self.hit("destructive", "hub_cli_dynamic", "hub", "cfharness với đối số động/xargs/find -exec — không được dùng từ agent", forbidden=True)
            return
        if sub in HUB_FORBIDDEN_SUB or (sub == "approvals" and len(t) > 1 and t[1] in ("approve", "deny")):
            self.hit("destructive", "self_approval", "hub", "agent không được tự duyệt/từ chối approval", forbidden=True)
            return
        if sub == "golden" and len(t) > 1 and t[1] not in ("list", "show", "candidates", "ls"):  # M4-B: golden task = chính sách an toàn (Q-07)
            self.hit("destructive", "policy_change", "policy", "agent không được tạo/sửa/xoá golden task (chính sách an toàn, Q-07)", forbidden=True)
            return
        if (sub == "learn" and len(t) > 2 and t[1] == "policy" and t[2] not in ("get", "show")) or (sub == "knowledge" and len(t) > 1 and t[1] in ("pin", "unpin")):
            # M4-A: công tắc học / config_bounds / rate / pin là chính sách an toàn (FR-LN-15, FR-SG-05) — chỉ người
            self.hit("destructive", "policy_change", "policy", "agent không được sửa chính sách học/pin (FR-LN-15, FR-SG-05)", forbidden=True)
            return
        if sub in ("guard",) and len(t) > 1 and t[1] in ("protect",) and len(t) > 2 and t[2] in ("add", "rm", "remove"):
            self.hit("destructive", "policy_change", "policy", "agent không được sửa chính sách an toàn (FR-SG-05)", forbidden=True)
            return
        if sub in HUB_HUMAN_ONLY_CLI:
            verbs = HUB_HUMAN_ONLY_CLI[sub]
            v1 = t[1] if len(t) > 1 else ""
            if sub == "learn" and v1 == "policy" and len(t) > 2 and t[2] in ("get", "show"):
                verbs = set()
            if sub == "guard" and v1 in ("protect", "integrity", "sandbox", "repo-deny", "repo_deny") and len(t) > 2 and t[2] in ("list", "ls", "status", "show"):
                verbs = set()
            if sub == "team" and v1 == "member" and len(t) > 2 and t[2] in ("list", "ls"):
                verbs = set()
            if sub == "manager" and v1 == "capabilities" and len(t) > 2 and t[2] in ("show", "get"):
                verbs = set()
            if verbs is None or v1 in verbs:
                # H-05: lệnh ứng với method RPC chỉ-người — daemon từ chối mọi lời gọi từ run → chặn thẳng, không mở approval
                self.hit("destructive", "hub_human_only", "hub", f"`cfharness {sub} {v1}`: chỉ người được làm (agent không mạo danh người)", forbidden=True)
                return
        if sub in HUB_DESTRUCTIVE_SUB and not (len(t) > 1 and t[1] in ("list", "get", "show", "status", "test", "ls")):
            self.hit("destructive", "hub_admin", "hub", f"cfharness {sub} (quản trị hub / chính sách)")
            return
        if sub == "daemon" and len(t) > 1 and t[1] in ("stop",):
            self.hit("destructive", "hub_stop", "hub", "dừng daemon hub")
            return
        self.hit("write", "hub_cli", "hub", "cfharness")

    def git(self, args: list[shell.Word]) -> None:
        cwd = self.cwd
        i = 0
        texts = [a.text for a in args]
        while i < len(texts) and texts[i].startswith("-"):
            if texts[i] == "-C" and i + 1 < len(texts):
                cwd = self.path(args[i + 1])
                i += 2
            elif texts[i] in ("-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") and i + 1 < len(texts):
                i += 2
            else:
                i += 1
        if i >= len(texts):
            return
        sub, rest = texts[i], texts[i + 1 :]
        flags = [t for t in rest if t.startswith("-")]
        pos = [t for t in rest if not t.startswith("-")]
        # R2-2: chuẩn hoá (bỏ -C/-c/--no-pager/--git-dir, đường dẫn tuyệt đối tới git) rồi khớp mẫu chặn của REPO ĐÍCH
        self.apply_repo_deny(" ".join(["git", "push" if sub == "send-pack" else sub, *rest]), cwd)

        def short(ch: str) -> bool:
            return any(t.startswith("-") and not t.startswith("--") and ch in t[1:] for t in rest)

        D = lambda rule, why: self.hit("destructive", rule, "git", why)  # noqa: E731
        if sub == "push":
            if any(t in ("--force", "--force-with-lease", "--force-if-includes", "--mirror", "--delete", "--prune") or t.startswith("--force-with-lease=") for t in rest) or short("f") or short("d"):
                D("git_push_force_or_delete", "git push --force/--delete/--mirror/--prune")
            elif any(p.startswith("+") or p.startswith(":") or ":" in p and p.split(":", 1)[0] == "" for p in pos[1:]):
                D("git_push_refspec", "git push +refspec/:ref (ghi đè/xoá nhánh remote)")
            else:
                for p in pos[1:]:
                    br = p.split(":")[-1].replace("refs/heads/", "")
                    r = self.ctx.policy.match_branch(br, "write")
                    if r is not None:
                        self.hit("destructive", "protected_branch_push", "git", f"push lên nhánh được bảo vệ ghi {br}", prot=r)
                self.hit("write", "git_push", "git", "git push")
            return
        if sub == "branch":
            if any(t in ("-D", "--delete", "-d", "-M", "-C") for t in flags) or (short("d") or short("D")) or ("--force" in flags or short("f")) and pos:
                D("git_branch_delete", "xoá/ghi đè nhánh git")
            else:
                self.hit("write" if pos else "safe", "git_branch", "git", "")
            return
        if sub == "tag":
            if any(t in ("-d", "--delete", "-f", "--force") for t in flags):
                D("git_tag_delete", "xoá/ghi đè tag git")
            else:
                self.hit("write" if pos else "safe", "git_tag", "git", "")
            return
        if sub == "reset":
            if "--hard" in flags:
                if pos and pos[0] not in ("HEAD",) or self._git_dirty(cwd, tracked_only=True) is not False:
                    D("git_reset_hard", "git reset --hard (mất thay đổi/commit)")
                else:
                    self.hit("write", "git_reset_hard_clean", "git", "reset --hard khi không có thay đổi")
            else:
                self.hit("write", "git_reset", "git", "")
            return
        if sub == "clean":
            if "-n" in flags or "--dry-run" in flags or short("n"):
                return
            if short("f") or "--force" in flags:
                files = self._git_clean_list(cwd, rest)
                if files is None:
                    D("git_clean", "git clean -f (xoá file chưa track)")
                else:
                    for f in files:
                        self.check_delete_target(f, "git clean")
            return
        if sub in ("checkout", "switch", "restore"):
            if sub == "restore" and ("--staged" in flags or short("S")) and not ("--worktree" in flags or short("W")):
                self.hit("write", "git_restore_staged", "git", "")
                return
            discard = ("--" in rest) or sub == "restore" or "." in pos or any(t in ("-f", "--force", "-B", "-C", "--discard-changes", "--ours", "--theirs") for t in flags)
            if discard:
                paths = rest[rest.index("--") + 1 :] if "--" in rest else ([] if sub != "restore" else pos)
                dirty = self._git_dirty(cwd, paths=paths or None)
                if dirty is False and not any(t in ("-B", "-C") for t in flags):
                    self.hit("write", "git_discard_clean", "git", "không có thay đổi chưa commit để mất")
                else:
                    D("git_discard_changes", f"git {sub} bỏ thay đổi chưa commit")
            else:
                self.hit("write", "git_checkout", "git", "")
            return
        if sub == "stash" and pos and pos[0] in ("drop", "clear"):
            D("git_stash_drop", "xoá stash")
            return
        if sub in ("filter-branch", "filter-repo"):
            D("git_rewrite_history", f"git {sub} viết lại lịch sử")
            return
        if sub == "reflog" and pos and pos[0] in ("expire", "delete"):
            D("git_reflog_expire", "xoá reflog")
            return
        if sub == "gc" and any(t.startswith("--prune") and t not in ("--prune", "--no-prune") or t == "--aggressive" and False for t in flags):
            D("git_gc_prune", "git gc --prune (xoá vĩnh viễn object)")
            return
        if sub == "prune" or (sub == "update-ref" and ("-d" in flags or "--stdin" in flags)):
            D("git_ref_delete", f"git {sub}")
            return
        if sub == "rebase":
            if any(t in ("--abort", "--continue", "--skip", "--quit", "--edit-todo", "--show-current-patch") for t in flags):
                self.hit("write", "git_rebase_ctl", "git", "")
            elif self._git_head_pushed(cwd) is not False:
                D("git_rebase_pushed", "git rebase trên nhánh đã push (viết lại lịch sử)")
            else:
                self.hit("write", "git_rebase_local", "git", "rebase nhánh chưa push")
            return
        if sub == "commit" and "--amend" in flags:
            if self._git_head_pushed(cwd) is not False:
                D("git_amend_pushed", "commit --amend sau khi đã push")
            else:
                self.hit("write", "git_amend_local", "git", "")
            return
        if sub == "worktree" and pos[:1] == ["remove"] and ("--force" in flags or short("f")):
            D("git_worktree_force_remove", "git worktree remove --force")
            return
        if sub in ("submodule",) and pos[:1] == ["deinit"] and ("--force" in flags or short("f") or "--all" in flags):
            D("git_submodule_deinit", "git submodule deinit -f")
            return
        if sub in ("rm",):
            for t in pos:
                p = self.path(t) if cwd else None
                if p:
                    self.check_delete_target(p, "git rm", "-r" in flags)
            return
        if sub == "credential" or sub.startswith("credential-"):
            self.hit("destructive", "git_credential", "git", "agent không được lấy/đặt thông tin xác thực git (R3-1)", forbidden=True)
            return
        if sub == "config" and any(re.match(r"^credential(\..*)?(helper|username|usehttppath)?$", t.lower()) or t.lower().startswith("credential.") for t in pos):
            self.hit("destructive", "git_credential", "git", "agent không được sửa cấu hình credential của git (R3-1)", forbidden=True)
            return
        if sub == "config":
            # R2-4: khoá git CHẠY LỆNH (fsmonitor/hooksPath/sshCommand/alias !…/filter/pager/editor…) — daemon/người chạy git ngoài sandbox
            keys = [t.lower() for t in pos]
            exec_key = any(re.match(r"^(core\.(fsmonitor|hookspath|sshcommand|pager|editor|askpass|gitproxy)|alias\..+|filter\..+\.(clean|smudge|process)|"
                                    r"diff\..+\.(textconv|command)|merge\..+\.driver|credential\..*helper|credential\.helper|"
                                    r"sequence\.editor|gpg\..*program|uploadpack\..+|receive\..+|includeif\..+|include\.path|init\.templatedir)$", k) for k in keys)
            writes = len(pos) >= 2 or any(t in ("--add", "--replace-all", "--unset", "--unset-all", "--remove-section", "--rename-section", "-e", "--edit") for t in flags)
            if exec_key and writes:
                self.hit("destructive", "git_config_exec", "git", "git config đặt khoá CHẠY LỆNH (chạy ngoài sandbox khi người/daemon dùng git)")
            elif writes:
                self.hit("write", "git_config", "git", "")
            return
        if sub in ("status", "log", "diff", "show", "blame", "rev-parse", "describe", "ls-files", "ls-tree", "remote", "config", "fetch", "grep", "shortlog", "cat-file", "for-each-ref", "reflog", "help", "version", "whatchanged", "name-rev", "merge-base", "ls-remote", "count-objects", "check-ignore"):
            if sub == "config" and any(t in ("--unset", "--unset-all", "--remove-section") for t in flags):
                self.hit("write", "git_config", "git", "")
            elif sub == "remote" and pos[:1] in (["remove"], ["rm"], ["set-url"], ["prune"]):
                self.hit("write", "git_remote_change", "git", "")
            return
        self.hit("write", f"git_{sub}", "git", "")

    # -------------------------------------------------------------- git probes
    def _git(self, cwd: str | None, *args: str) -> subprocess.CompletedProcess[str] | None:
        if not self.ctx.git_probe or cwd is None:
            return None
        try:
            from ..workspace import GIT_ENV, GIT_SAFE_ARGS  # R2-4: probe git của guard chạy NGOÀI sandbox → tắt khoá chạy lệnh

            return subprocess.run(["git", *GIT_SAFE_ARGS, "-C", cwd, *args], capture_output=True, encoding="utf-8", errors="replace", timeout=3,
                                  env={**os.environ, **GIT_ENV}, **no_window_kwargs())  # git xuất UTF-8; Windows mặc định cp1252
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _git_dirty(self, cwd: str | None, paths: list[str] | None = None, tracked_only: bool = False) -> bool | None:
        r = self._git(cwd, "status", "--porcelain", *(["--untracked-files=no"] if tracked_only else []), *(["--", *paths] if paths else []))
        if r is None or r.returncode != 0:
            return None
        return bool(r.stdout.strip())

    def _git_head_pushed(self, cwd: str | None) -> bool | None:
        r = self._git(cwd, "branch", "-r", "--contains", "HEAD")
        if r is None or r.returncode != 0:
            return None
        return bool(r.stdout.strip())

    def _git_clean_list(self, cwd: str | None, rest: list[str]) -> list[str] | None:
        flags = [t for t in rest if t.startswith("-") and t not in ("-f", "--force")]
        flags = [f.replace("f", "") if not f.startswith("--") else f for f in flags]
        flags = [f for f in flags if f not in ("-", "")]
        r = self._git(cwd, "clean", "-n", *flags)
        if r is None or r.returncode != 0 or cwd is None:
            return None
        out = []
        for line in r.stdout.splitlines():
            m = re.match(r"^Would remove (.+)$", line.strip())
            if m:
                out.append(_norm(os.path.join(cwd, m.group(1))))
        return out


def _q(s: str) -> str:
    import shlex

    return shlex.quote(s)


def action_fingerprint(tool_name: str, tool_input: dict[str, Any] | None) -> str:
    """Dấu vân tay thao tác cho vé cho phép một lần (FR-SG-04): tool + input chuẩn hoá (bỏ mô tả/timeout)."""
    import hashlib

    ti = dict(tool_input or {})
    for k in ("description", "timeout", "run_in_background", "dangerouslyDisableSandbox"):
        ti.pop(k, None)
    if tool_name == "Bash" and isinstance(ti.get("command"), str):
        ti["command"] = re.sub(r"\s+", " ", shell.normalize(ti["command"])).strip()
    blob = json.dumps({"tool": tool_name, "input": ti}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
