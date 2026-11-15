"""Nhập hub KHÁC hệ điều hành / khác hệ thống file (PR4 của `docs/dev-notes/plan-hub-clone.md` §5b) — phần dùng chung của `hub_transfer`.

Mọi hàm nhận `target_os` ("posix" | "windows") làm THAM SỐ (mặc định = OS đang chạy, `pathmap.host_os()`), không đọc `IS_WINDOWS` toàn
cục — để test trên macOS mô phỏng được đích Windows.

1. Tên file (`plan_names`): kiểm MỌI phần tử của gói TRƯỚC khi giải nén (Windows không ghi được tên hỏng → phải biết trước):
   - đích Windows: ký tự `<>:"/\\|?*` + ký tự điều khiển, tên dành riêng (CON, PRN, AUX, NUL, COM1–9, LPT1–9, kể cả kèm đuôi `nul.txt`),
     đuôi dấu chấm/khoảng trắng;
   - hệ thống file KHÔNG phân biệt hoa/thường (Windows, macOS mặc định — dò thật bằng `fs_case_insensitive`, không ghi gì): hai tên chỉ
     khác hoa/thường trong cùng thư mục.
   Xử lý: file TRI THỨC (bên trong `skills/`, `.archive/` của agent/team/global; bên trong một mục `trash/`) → đổi tên an toàn theo quy tắc
   tất định (`safe_segment`, `_dedupe`: ký tự hỏng → `_`, tên dành riêng → `con_`/`con_.txt`, đuôi `.`/khoảng trắng → `_`; tên SKILL
   giữ `NAME_RE` của knowledge: `con` → `con-skill`, trùng → `-2`, `-3`…), ghi báo cáo + `hub_state.import_followups.renamed`. KHÔNG đi qua
   ledger (đổi tên skill qua ledger = xoá + tạo lại, mất lịch sử use_count/pin và hàng ledger cũ trỏ tên cũ) — đổi trong STAGE: thư mục,
   `skills.name|path` (cặp đường dẫn vào pathmap), `name:` trong frontmatter SKILL.md; hàng ledger/append-only giữ tên cũ (lịch sử).
   Phần `claude/` (optional) → bỏ file đó + cảnh báo. Mọi file khác (id agent/team, chính sách, ledger, prompts…) → TỪ CHỐI cả gói, nêu
   rõ tên + lý do (đổi tên ở máy nguồn rồi xuất lại).
2. MCP stdio (`mcp_missing`, MỌI lần nhập, không chỉ khác OS): `command` tuyệt đối không tồn tại ở máy đích (hoặc là đường dẫn kiểu OS
   khác), lệnh trần không có trên PATH (`shutil.which`) → server được ĐÁNH DẤU (`probe_error` "[hub] …", không có cột bật/tắt — không thêm
   migration) + gợi ý `cfharness mcp edit <tên>`; run dùng server đó vẫn chạy, Claude Code báo MCP không kết nối được.
3. Cảnh báo theo OS đích (`os_warnings`): khác họ OS, sandbox OS không có trên Windows (`docs/dev-notes/windows.md`), giam workspace dựa
   trên sandbox, HUB_HOME Windows ngoài hồ sơ người dùng (quyền file chỉ còn ACL hồ sơ — `oscompat`), gốc workspace còn kiểu đường dẫn
   OS khác (cần `--map-path`), autostart không chuyển (gợi ý lệnh đúng OS).
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .pathmap import OsKind, PathMap, host_os, path_style

WIN_BAD_CHARS = frozenset('<>:"/\\|?*')
WIN_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
                          "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"})
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")  # = knowledge.store.NAME_RE
TIERS = ("agents", "teams")
MCP_MARK = "[hub] nhập từ máy khác: không thấy lệnh '{cmd}' ở máy này — sửa bằng `cfharness mcp edit {name}`"


def os_family(name: Any) -> OsKind:
    """`source.os` của manifest (`platform.system().lower()`: darwin/linux/windows) → họ kiểu đường dẫn."""
    return "windows" if str(name or "").lower().startswith("win") else "posix"


# ============================================================================ tên file
def invalid_reason(seg: str) -> str | None:
    """Lý do một thành phần tên KHÔNG tạo được trên Windows (None = hợp lệ)."""
    for c in seg:
        if c in WIN_BAD_CHARS:
            return f"ký tự {c!r} không hợp lệ trên Windows"
        if ord(c) < 32:
            return "có ký tự điều khiển"
    if seg.endswith((".", " ")):
        return "kết thúc bằng dấu chấm/khoảng trắng (Windows tự cắt)"
    if seg.split(".")[0].rstrip(" ").upper() in WIN_RESERVED:
        return "tên dành riêng của Windows (CON, PRN, AUX, NUL, COM1–9, LPT1–9)"
    return None


def safe_segment(seg: str, *, skill: bool = False) -> str:
    """Tên thay thế hợp lệ trên Windows — tất định (cùng đầu vào → cùng kết quả)."""
    s = "".join("_" if (c in WIN_BAD_CHARS or ord(c) < 32) else c for c in seg)
    if skill:
        s = re.sub(r"[^a-z0-9-]", "-", s.lower())
    stripped = s.rstrip(". ")
    if stripped != s:
        s = (stripped or "x") + ("-x" if skill else "_")
    base = s.split(".")[0]
    if base.rstrip(" ").upper() in WIN_RESERVED:
        s = base + ("-skill" if skill else "_") + s[len(base):]
    if skill and not SKILL_NAME_RE.match(s):
        s = (s.strip("-") or "skill")[:55] + "-skill"
    return s or "_"


def _fold(s: str) -> str:
    return s.casefold()


def _dedupe(new: str, taken: dict[str, str], *, skill: bool, ci: bool) -> str:
    key = _fold if ci else (lambda x: x)
    stem, dot, ext = new.rpartition(".") if (not skill and "." in new[1:]) else (new, "", "")
    for k in range(2, 10_000):
        cand = f"{stem}-{k}" if skill else f"{stem}_{k}{dot}{ext}"
        if key(cand) not in taken:
            return cand
    raise ValueError(f"không tìm được tên thay thế cho {new!r}")  # pragma: no cover


def _kind(parts: tuple[str, ...], i: int) -> str | None:
    """Thành phần `parts[i]` của đường dẫn trong stage được đổi tên không: "skill" (tên thư mục skill — giữ NAME_RE), "file" (đổi tên
    tự do), "optional" (phần `claude/`: bỏ file), None (không được đổi → từ chối)."""
    top = parts[0]
    if top == "claude":
        return "optional"
    if top in TIERS and len(parts) >= 4:
        if parts[2] == "skills":
            return "skill" if (i == 3 and len(parts) > 4) else ("file" if i >= 3 else None)
        if parts[2] == ".archive":
            return "file" if i >= 3 else None
        return None
    if top == "global" and len(parts) >= 3:
        if parts[1] == "skills":
            return "skill" if (i == 2 and len(parts) > 3) else ("file" if i >= 2 else None)
        if parts[1] == ".archive":
            return "file" if i >= 2 else None
        return None
    if top == "trash" and len(parts) >= 3:
        return "file" if i >= 2 else None
    return None


def plan_names(rels: Iterable[str], *, target_os: OsKind, case_insensitive: bool) -> dict[str, Any]:
    """Kế hoạch tên cho các đường dẫn TƯƠNG ĐỐI trong stage (luôn `/`, như phần tử tar). Trả:
    `renames {rel: rel mới}`, `refused [{path, reason}]`, `skipped [{path, reason}]` (phần claude/), `skills [{scope, owner, old, new,
    dir_old, dir_new}]`, `checked {windows, case_insensitive}`."""
    win = target_os == "windows"
    ci = bool(case_insensitive or win)
    memo: dict[tuple[str, ...], tuple[str, ...]] = {(): ()}
    used: dict[tuple[str, ...], dict[str, str]] = {}
    renames: dict[str, str] = {}
    refused: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    skills: list[dict[str, str]] = []
    # ở MỖI cấp thư mục, tên vốn hợp lệ được giữ chỗ TRƯỚC (thành phần hỏng xếp sau) → tên thay thế không bao giờ chiếm tên của file thật
    order = sorted(set(rels), key=lambda r: tuple((win and invalid_reason(x) is not None, x) for x in r.split("/")))
    for rel in order:
        parts = tuple(rel.split("/"))
        bad: str | None = None
        for i in range(len(parts)):
            key = parts[: i + 1]
            if key in memo:
                continue
            parent = memo[parts[:i]]
            seg = parts[i]
            kind = _kind(parts, i)
            reason = invalid_reason(seg) if win else None
            new = seg
            if reason:
                if kind is None or kind == "optional":
                    bad = f"{'/'.join(parts[: i + 1])}: {reason}"
                    break
                new = safe_segment(seg, skill=kind == "skill")
            taken = used.setdefault(parent, {})
            fk = _fold(new) if ci else new
            if fk in taken and taken[fk] != seg:
                if kind is None or kind == "optional":
                    other = "/".join((*parent, taken[fk]))
                    bad = f"{'/'.join(parts[: i + 1])}: trùng tên chỉ khác hoa/thường với {other} (hệ thống file đích không phân biệt)" \
                        if ci and new.casefold() == taken[fk].casefold() else f"{'/'.join(parts[: i + 1])}: trùng tên với {other} sau khi đổi"
                    break
                new = _dedupe(new, taken, skill=kind == "skill", ci=ci)
                fk = _fold(new) if ci else new
            taken[fk] = seg
            memo[key] = (*parent, new)
            if new != seg and kind == "skill":
                top = parts[0]
                scope = {"agents": "agent", "teams": "team"}.get(top, "global")
                owner = parts[1] if top in TIERS else "global"
                skills.append({"scope": scope, "owner": owner, "old": seg, "new": new, "dir_old": "/".join(key),
                               "dir_new": "/".join(memo[key])})
        if bad is not None:
            (skipped if parts[0] == "claude" else refused).append({"path": rel, "reason": bad})
            continue
        nr = "/".join(memo[parts])
        if nr != rel:
            renames[rel] = nr
    return {"renames": renames, "refused": refused, "skipped": skipped, "skills": skills,
            "checked": {"windows": win, "case_insensitive": ci}}


def fs_case_insensitive(d: Path, *, default: bool | None = None) -> bool:
    """Hệ thống file chứa `d` (hoặc thư mục tổ tiên gần nhất đang có) KHÔNG phân biệt hoa/thường? Dò KHÔNG ghi gì: đổi hoa/thường tên
    của chính thư mục đó / một mục con rồi xem có trỏ cùng file không. Không dò được → `default` (mặc định: Windows/macOS = True)."""
    p = Path(d)
    while not p.exists() and p.parent != p:
        p = p.parent
    cands = [p]
    try:
        cands += sorted(Path(e.path) for e in os.scandir(p))[:50]
    except OSError:
        pass
    for c in cands:
        sw = c.name.swapcase()
        if not c.name or sw == c.name:
            continue
        alt = c.with_name(sw)
        try:
            if not alt.exists():
                return False
            return os.path.samefile(c, alt)
        except OSError:
            continue
    if default is not None:
        return default
    import sys

    return sys.platform in ("win32", "darwin")


# ============================================================================ đổi tên skill trong stage
def apply_skill_renames(stage: Path, db: Path, skills: list[dict[str, str]]) -> int:
    """Sau khi giải nén (thư mục đã ở tên mới): `skills.name` trong DB + `name:` trong frontmatter SKILL.md. `skills.path`/`archived_path`
    đi theo cặp đường dẫn trong pathmap (`rename_pairs`). Trả số hàng DB đã đổi."""
    n = 0
    if not skills:
        return n
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for s in skills:
            if "skills" in tabs:
                cur = conn.execute("UPDATE skills SET name=? WHERE scope=? AND owner_id=? AND name=?", (s["new"], s["scope"], s["owner"], s["old"]))
                n += cur.rowcount
            _rename_frontmatter(stage.joinpath(*s["dir_new"].split("/"), "SKILL.md"), s["old"], s["new"])
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return n


def _rename_frontmatter(p: Path, old: str, new: str) -> None:
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if not text.startswith("---\n"):
        return
    end = text.find("\n---", 4)
    if end < 0:
        return
    fm = re.sub(rf"(?m)^name:\s*(['\"]?){re.escape(old)}\1\s*$", f"name: {new}", text[4:end], count=1)
    p.write_text("---\n" + fm + text[end:], encoding="utf-8")


def rename_pairs(renames: dict[str, str], src_home: str, dst_home: str) -> list[tuple[str, str]]:
    """Cặp đường dẫn TUYỆT ĐỐI (nguồn → đích) cho mọi mục bị đổi tên — gộp vào pathmap để `skills.path`, `archived_path`, cấu hình…
    trỏ đúng tên mới. Mỗi kiểu phía dùng dấu phân cách của chính nó."""
    def join(root: str, rel: str) -> str:
        sep = "\\" if path_style(root) == "windows" else "/"
        return root.rstrip("\\/") + sep + rel.replace("/", sep)

    out: dict[str, str] = {}
    for old, new in sorted(renames.items()):
        op, np_ = old.split("/"), new.split("/")
        for k in range(1, len(op) + 1):  # mỗi thành phần bị đổi (thư mục skill, file) là một cặp; con của thư mục đổi tên đi theo cặp cha
            if op[k - 1] != np_[k - 1]:
                out.setdefault(join(src_home, "/".join(op[:k])), join(dst_home, "/".join(np_[:k])))
    return list(out.items())


# ============================================================================ MCP stdio
def mcp_missing(db: Path, pm: PathMap | None, *, target_os: OsKind, which: Any = shutil.which) -> list[dict[str, str]]:
    """MCP stdio (chưa xoá mềm) có `command` không chạy được ở máy đích. `pm` = đổi đường dẫn trước khi kiểm (dry-run: DB chưa viết lại)."""
    import json

    out: list[dict[str, str]] = []
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover
        return out
    try:
        tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "mcp_servers" not in tabs:
            return out
        cols = {x[1] for x in c.execute("PRAGMA table_info(mcp_servers)")}
        if not {"name", "config"} <= cols:
            return out
        where = " WHERE deleted_at IS NULL" if "deleted_at" in cols else ""
        rows = c.execute(f"SELECT name, config FROM mcp_servers{where} ORDER BY name").fetchall()
    except sqlite3.Error:
        return out
    finally:
        c.close()
    for name, conf in rows:
        try:
            cfg = json.loads(conf) or {}
        except (TypeError, ValueError):
            continue
        cmd = cfg.get("command") if isinstance(cfg, dict) else None
        if not cmd or not isinstance(cmd, str):
            continue
        cmd2 = pm.rewrite(cmd, syntax="text") if pm else cmd
        why = command_problem(cmd2, target_os=target_os, which=which)
        if why:
            out.append({"server": str(name), "command": cmd2, "reason": why})
    return out


def command_problem(cmd: str, *, target_os: OsKind, which: Any = shutil.which) -> str | None:
    st = path_style(cmd)
    if st is not None:
        if st != target_os:
            return f"đường dẫn kiểu {st} trên máy {target_os}"
        return None if os.path.exists(cmd) else "không có file này"
    return None if which(cmd) is not None else "không có trên PATH"


def mark_mcp(db: Path, missing: list[dict[str, str]]) -> int:
    """Đánh dấu server MCP thiếu lệnh (`probe_error`, xoá `last_probe_at`) — hiện ở `cfharness mcp list|show`."""
    if not missing:
        return 0
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        cols = {x[1] for x in conn.execute("PRAGMA table_info(mcp_servers)")}
        if "probe_error" not in cols:
            return 0
        sets = "probe_error=?" + (", last_probe_at=NULL" if "last_probe_at" in cols else "")
        with conn:
            for m in missing:
                conn.execute(f"UPDATE mcp_servers SET {sets} WHERE name=?", (MCP_MARK.format(cmd=m["command"], name=m["server"]), m["server"]))
        return len(missing)
    finally:
        conn.close()


# ============================================================================ cảnh báo theo OS đích
def os_warnings(manifest: dict[str, Any], *, target_os: OsKind, pm: PathMap, home: Path, user_home: Path,
                config: Any = None) -> list[str]:
    """Cảnh báo cho kế hoạch nhập — chỉ chữ cố định + đường dẫn/tên, không chặn."""
    src = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    src_os = os_family(src.get("os"))
    out: list[str] = []
    if src_os != target_os:
        out.append(f"gói từ máy {src.get('os')} ({src_os}) → nhập sang {target_os}: đường dẫn được đổi kiểu (dấu phân cách, ổ đĩa) theo bản đồ; "
                   "đường dẫn ngoài các gốc trong bản đồ giữ nguyên — thêm --map-path CŨ=MỚI nếu cần")
        auto = "`cfharness daemon install-service`" + (" (hoặc `cfharness admin install-daemon` cho LaunchDaemon macOS)" if target_os != "windows" else "")
        out.append(f"tự khởi động daemon của máy nguồn không được chuyển — cài lại theo hệ điều hành này: {auto}")
    roots = manifest.get("path_roots") if isinstance(manifest.get("path_roots"), dict) else {}
    for w in roots.get("workspaces") or []:
        if not isinstance(w, str):
            continue
        nw = pm.rewrite(w, syntax="text") if pm else w
        st = path_style(nw)
        if st is not None and st != target_os:
            out.append(f"workspace {nw} vẫn là đường dẫn kiểu {st} — thêm --map-path {w}=<thư mục ở máy này>")
    if target_os == "windows":
        def cget(key: str, dflt: Any) -> Any:
            try:
                return config.get(key) if config is not None else dflt
            except KeyError:
                return dflt

        if cget("guard.sandbox.enabled", True):
            out.append("Windows không có sandbox OS cho Bash của agent (docs/dev-notes/windows.md) — chỉ còn hook guard + permissions.deny"
                       + ("; guard.sandbox.fail_if_unavailable_windows=true → MỌI run bị từ chối" if cget("guard.sandbox.fail_if_unavailable_windows", False) else ""))
        if cget("workspace.confine.root", None):
            out.append("workspace.confine dựa trên sandbox OS — trên Windows chỉ còn luật permissions.deny (không giam được Bash)")
        try:
            inside = os.path.commonpath([str(home).lower(), str(user_home).lower()]) == str(user_home).lower()
        except ValueError:
            inside = False
        if not inside:
            out.append(f"HUB_HOME {home} nằm ngoài hồ sơ người dùng {user_home} — trên Windows quyền 0600/0700 không có tác dụng, chỉ ACL hồ sơ "
                       "người dùng bảo vệ secret/auth/gói (oscompat) — nên đặt HUB_HOME trong hồ sơ")
    return out


def default_target_os(target_os: str | None) -> OsKind:
    return "windows" if target_os == "windows" else ("posix" if target_os else host_os())


__all__ = ["os_family", "invalid_reason", "safe_segment", "plan_names", "fs_case_insensitive", "apply_skill_renames", "rename_pairs",
           "mcp_missing", "command_problem", "mark_mcp", "os_warnings", "default_target_os", "WIN_RESERVED"]
