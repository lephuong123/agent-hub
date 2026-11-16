"""`cfharness hub doctor` — kiểm hub SAU khi nhập từ máy khác (PR5 của docs/dev-notes/plan-hub-clone.md §7). CHỈ ĐỌC + gợi ý lệnh; chạy
được nhiều lần; chạy được khi daemon DỪNG (đọc hub.db ở chế độ chỉ đọc, config.yaml, TÊN secret — không bao giờ đọc giá trị secret).

Kiểm: `claude` trên PATH + phiên bản so `runtime.claude.tested_versions` + đăng nhập (secret `CLAUDE_CODE_OAUTH_TOKEN`/`CLAUDE_ACCT_*`, cờ
`auth_block`; `--probe` mới chạy `claude auth status` — không bao giờ gọi model), `${NAME}` trong config.yaml + cấu hình MCP có secret/env,
MCP (đánh dấu thiếu lệnh của lần nhập, OAuth chưa kết nối; `--probe` + daemon chạy → `mcp.probe` từng server), Telegram/Slack bật + có token,
thư mục chung Không gian số (filehub: cần đăng nhập lại sau nhập, chưa đăng nhập, binary sync thiếu), repo workspace (có chưa, HEAD khớp
`repos.json` của gói), agent bị tạm dừng vì thiếu workspace, lịch đang tạm dừng (clone), việc viết lại
tri thức còn dở (`import_followups`), `imported_from`/`cloned_from`/`migrated_to`, tự khởi động (gợi ý đúng OS), `cfharness[vector]`,
quyền file (POSIX). Kết quả `{ok, home, checks[{id, check, ok, level ok|info|warning|error, detail, fix?}], summary}`.

RPC `hub.doctor {}` (HUMAN_ONLY: lộ đường dẫn/tên secret/repo của người; chỉ đọc, không audit) — cho web PR6; không probe mạng trong RPC
(web gọi `mcp.probe` riêng nếu cần).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .daemon import Daemon
    from .paths import HubPaths

REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class _Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, cid: str, check: str, ok: bool, detail: str, level: str = "error", fix: str | None = None) -> None:
        self.items.append({"id": cid, "check": check, "ok": bool(ok), "level": "ok" if ok and level != "info" else level, "detail": detail,
                           "fix": None if ok else fix})


def _ro(db: Path) -> sqlite3.Connection | None:
    if not db.is_file():
        return None
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return c
    except sqlite3.Error:
        return None


def _state(c: sqlite3.Connection | None, key: str) -> Any:
    if c is None:
        return None
    try:
        r = c.execute("SELECT value FROM hub_state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None
    except (sqlite3.Error, ValueError):
        return None


def _tables(c: sqlite3.Connection | None) -> set[str]:
    if c is None:
        return set()
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _secret_names(home: Path) -> set[str]:
    from .secrets import NAME_RE

    try:
        return {n for n in os.listdir(home / "secrets") if NAME_RE.match(n) and (home / "secrets" / n).is_file()}
    except OSError:
        return set()


def run(paths: "HubPaths", *, probe: bool = False, claude_binary: str | None = None,
        mcp_probe: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Chạy mọi kiểm tra. `mcp_probe(name)` (chỉ khi `probe` và daemon chạy) → kết quả RPC `mcp.probe`."""
    from .config import HubConfig

    ck = _Checks()
    home = paths.home
    try:
        cfg = HubConfig(paths.config).data
    except Exception as e:
        cfg = {}
        ck.add("config", "config.yaml", False, f"không đọc được: {e}", fix=f"sửa {paths.config}")
    c = _ro(paths.db)
    try:
        if c is None:
            ck.add("hub_db", "hub.db", False, f"không có/không mở được {paths.db}", fix="cfharness hub import <gói> hoặc cfharness daemon start")
        secrets = _secret_names(home)
        _claude(ck, cfg, secrets, c, probe=probe, binary=claude_binary)
        _secret_refs(ck, paths, c, secrets)
        _mcp(ck, c, secrets, mcp_probe if probe else None)
        _channels(ck, cfg, secrets)
        _filehub(ck, cfg, c, home)
        _repos(ck, c)
        _schedules(ck, c)
        _followups(ck, c)
        _origin(ck, c)
        _daemon(ck, paths)
        _autostart(ck, paths)
        _vector(ck, cfg)
        _perms(ck, paths)
    finally:
        if c is not None:
            c.close()
    errs = sum(1 for x in ck.items if x["level"] == "error")
    warns = sum(1 for x in ck.items if x["level"] == "warning")
    return {"ok": errs == 0, "home": str(home), "checks": ck.items, "summary": {"errors": errs, "warnings": warns, "total": len(ck.items)}}


# ============================================================================ từng nhóm
def _claude(ck: _Checks, cfg: dict[str, Any], secrets: set[str], c: sqlite3.Connection | None, *, probe: bool, binary: str | None) -> None:
    ccfg = ((cfg.get("runtime") or {}).get("claude") or {}) if cfg else {}
    b = binary or ccfg.get("binary") or "claude"
    path = shutil.which(b)
    ck.add("claude_path", "claude trên PATH", bool(path), path or f"không tìm thấy `{b}`",
           fix="cài Claude Code CLI (https://docs.claude.com/claude-code) rồi `claude auth login`")
    if path:
        from .runtime.claude_cli import claude_version

        v = claude_version(path, 0)
        tested = [str(x) for x in ccfg.get("tested_versions") or []]
        ok = bool(v) and v in tested
        ck.add("claude_version", "phiên bản claude", ok, f"{v or '?'} (đã kiểm: {', '.join(tested) or '—'})" + ("" if ok else " — chưa kiểm với hub"),
               "warning", fix="dùng phiên bản trong runtime.claude.tested_versions, hoặc chạy thử một run rồi theo dõi `cfharness doctor`")
    tok = "CLAUDE_CODE_OAUTH_TOKEN" in secrets
    accts = sorted(n for n in secrets if n.startswith("CLAUDE_ACCT_"))
    blocked = _state(c, "auth_block")
    if blocked:
        ck.add("claude_login", "đăng nhập claude", False, "hub đang chặn spawn vì claude chưa đăng nhập",
               fix="claude auth login (hoặc cfharness account use <id>) rồi cfharness doctor")
    elif tok or accts:
        ck.add("claude_login", "đăng nhập claude", True, "token trong secret store" + (f" + {len(accts)} tài khoản" if accts else ""))
    elif probe and path:
        try:
            from .oscompat import exec_argv, no_window_kwargs
            from .runtime.claude_cli import child_env

            r = subprocess.run(exec_argv([path, "auth", "status"]), capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                               env=child_env(ccfg), **no_window_kwargs())
            try:
                logged = bool(json.loads(r.stdout).get("loggedIn", r.returncode == 0))
            except (ValueError, AttributeError):
                logged = r.returncode == 0
            ck.add("claude_login", "đăng nhập claude", logged, "claude auth status: " + ("đã đăng nhập" if logged else "CHƯA đăng nhập"),
                   fix="claude auth login")
        except (OSError, subprocess.TimeoutExpired) as e:
            ck.add("claude_login", "đăng nhập claude", False, str(e)[:200], "warning", fix="claude auth login")
    else:
        ck.add("claude_login", "đăng nhập claude", False, "chưa xác nhận (không có token trong secret store)", "warning",
               fix="claude auth login (hoặc cfharness hub doctor --probe để kiểm `claude auth status`)")


def _secret_refs(ck: _Checks, paths: "HubPaths", c: sqlite3.Connection | None, secrets: set[str]) -> None:
    refs: dict[str, set[str]] = {}
    try:
        for n in REF_RE.findall(paths.config.read_text(encoding="utf-8")):
            refs.setdefault(n, set()).add("config.yaml")
    except OSError:
        pass
    if c is not None and "mcp_servers" in _tables(c):
        for r in c.execute("SELECT name, config FROM mcp_servers WHERE deleted_at IS NULL"):
            for n in REF_RE.findall(str(r["config"] or "")):
                refs.setdefault(n, set()).add(f"mcp {r['name']}")
    missing = sorted(n for n in refs if n not in secrets and n not in os.environ)
    imp = _state(c, "imported_from") or {}
    lost = sorted(set(imp.get("secrets_missing") or []) - secrets)
    if not refs and not lost:
        ck.add("secret_refs", "secret được tham chiếu", True, "không có ${TÊN} nào")
        return
    where = ["{} ({})".format(n, ", ".join(sorted(refs[n]))) for n in missing]
    ck.add("secret_refs", "secret được tham chiếu", not missing,
           f"{len(refs)} tên, thiếu: {', '.join(where)}" if missing else f"{len(refs)} tên đều có",
           fix="; ".join(f"cfharness secret set {n}" for n in missing[:6]))
    if lost:
        ck.add("secrets_missing", "secret không đi theo gói", False, ", ".join(lost[:12]), "warning",
               fix="; ".join(f"cfharness secret set {n}" for n in lost[:6]))


def _mcp(ck: _Checks, c: sqlite3.Connection | None, secrets: set[str], probe: Callable[[str], dict[str, Any]] | None) -> None:
    if c is None or "mcp_servers" not in _tables(c):
        return
    rows = c.execute("SELECT name, transport, config, probe_error, last_probe_at FROM mcp_servers WHERE deleted_at IS NULL ORDER BY name").fetchall()
    if not rows:
        return
    from .mcp_client.oauth import secret_name as oauth_secret

    for r in rows:
        name = r["name"]
        try:
            conf = json.loads(r["config"] or "{}") or {}
        except ValueError:
            conf = {}
        perr = str(r["probe_error"] or "")
        if probe is not None:
            try:
                pr = probe(name) or {}
                ck.add(f"mcp:{name}", f"MCP {name}", bool(pr.get("ok")), f"kết nối được, {len(pr.get('tools') or {})} tool" if pr.get("ok")
                       else f"không kết nối được: {pr.get('error')}", fix=f"cfharness mcp edit {name}")
            except Exception as e:  # noqa: BLE001
                ck.add(f"mcp:{name}", f"MCP {name}", False, f"không thử được: {e}", "warning", fix=f"cfharness mcp probe {name}")
            continue
        if perr.startswith("[hub]"):
            ck.add(f"mcp:{name}", f"MCP {name}", False, perr[:200], fix=f"cfharness mcp edit {name}")
        elif isinstance(conf.get("auth"), dict) and conf["auth"].get("type") == "oauth" and oauth_secret(name) not in secrets:
            ck.add(f"mcp:{name}", f"MCP {name}", False, "OAuth chưa kết nối ở máy này", "warning", fix=f"cfharness mcp login {name}")
        else:
            ck.add(f"mcp:{name}", f"MCP {name}", True, f"{r['transport']}" + (f" · lần thử cuối lỗi: {perr[:120]}" if perr else "")
                   + ("" if r["last_probe_at"] else " · chưa thử (--probe)"))


def _channels(ck: _Checks, cfg: dict[str, Any], secrets: set[str]) -> None:
    n = (cfg.get("notify") or {}) if cfg else {}
    tg = n.get("telegram") or {}
    sl = n.get("slack") or {}
    tok = tg.get("bot_token_secret") or "TELEGRAM_BOT_TOKEN"
    if tg.get("enabled", True) and tok in secrets:
        ck.add("telegram", "Telegram", True, "bật, có token bot")
    elif tok in secrets:
        ck.add("telegram", "Telegram", True, "có token nhưng đang TẮT", "info")
    else:
        ck.add("telegram", "Telegram", True, "chưa cấu hình (máy nhân bản cần bot RIÊNG)", "info")
    btok = sl.get("bot_token_secret") or "SLACK_BOT_TOKEN"
    if sl.get("enabled", True) and sl.get("interactive") and btok in secrets:
        ck.add("slack", "Slack", True, "2 chiều bật, có token")
    elif btok in secrets or "SLACK_WEBHOOK_URL" in secrets:
        ck.add("slack", "Slack", True, "có token nhưng đang TẮT", "info")
    else:
        ck.add("slack", "Slack", True, "chưa cấu hình", "info")


def _filehub(ck: _Checks, cfg: dict[str, Any], c: sqlite3.Connection | None, home: Path) -> None:
    """Thư mục chung Không gian số: cấu hình có nhưng thiết bị máy này chưa đăng nhập (token `secrets/filehub/tokens.json` không đi theo
    gói xuất), lần nhập còn chờ đăng nhập lại (`hub_state.import_filehub`), binary sync không có ở máy này. Chỉ đọc — không chạy binary."""
    fh = (cfg.get("filehub") or {}) if cfg else {}
    imp = _state(c, "import_filehub")
    folders = [f for f in fh.get("folders") or [] if isinstance(f, dict) and f.get("name")]
    if not fh.get("enabled") and not folders and not imp:
        ck.add("filehub", "Không gian số", True, "chưa thiết lập", "info")
        return
    from .hub_transfer import filehub_command

    binary = str(fh.get("sync_binary") or "")
    bpath = os.path.expanduser(binary) if binary else ""
    binary_ok = bool(bpath) and os.path.isfile(bpath) and os.access(bpath, os.X_OK)
    info = {"folders": folders or ((imp or {}).get("folders") if isinstance(imp, dict) else None) or [], "binary": binary,
            "server": fh.get("server")}
    cmd = filehub_command(info, binary_ok=binary_ok)
    tokens = (home / "secrets" / "filehub" / "tokens.json").is_file()
    if imp:
        ck.add("filehub", "Không gian số", False, "hub nhập từ máy khác — đồng bộ TẮT, thiết bị máy này chưa đăng nhập (token thiết bị không đi "
               "theo gói)", "warning", fix=cmd)
    elif fh.get("enabled") and not tokens:
        ck.add("filehub", "Không gian số", False, "có cấu hình nhưng máy này chưa đăng nhập", "warning",
               fix="cfharness filehub login" if folders and binary_ok else cmd)
    else:
        ck.add("filehub", "Không gian số", True, ("bật" if fh.get("enabled") else "TẮT") + f", {len(folders)} thư mục chung"
               + (", đã đăng nhập" if tokens else ""), "ok" if fh.get("enabled") else "info")
    if binary and not binary_ok:
        ck.add("filehub_binary", "binary sync Không gian số", False, f"không có/không chạy được ở máy này: {binary}", "warning",
               fix="cfharness filehub setup --binary /đường/dẫn/tang-thu-sync-headless")


def _repos(ck: _Checks, c: sqlite3.Connection | None) -> None:
    from .transfer_repos import PAUSED_KEY, REPOS_KEY, clone_hint, git_info

    repos = _state(c, REPOS_KEY) or []
    for r in repos if isinstance(repos, list) else []:
        to = str(r.get("to") or "")
        p = Path(to).expanduser()
        if not p.exists():
            ck.add(f"repo:{to}", f"repo {to}", False, "chưa có ở máy này" + (" — nguồn có thay đổi chưa commit (không đi theo gói)" if r.get("dirty") else ""),
                   fix=clone_hint({**r, "to": to}) or "chép repo tay (không có remote)")
            continue
        if r.get("git"):
            cur = git_info(p)
            head_ok = not r.get("head") or cur.get("head") == r.get("head")
            ck.add(f"repo:{to}", f"repo {to}", head_ok, ("HEAD khớp máy nguồn" if head_ok else
                                                        f"HEAD {str(cur.get('head') or '?')[:12]} ≠ máy nguồn {str(r.get('head'))[:12]}"),
                   "warning", fix=f"git -C {to} fetch && git -C {to} checkout {r.get('branch') or r.get('head')}")
        else:
            ck.add(f"repo:{to}", f"thư mục {to}", True, "có")
    paused = _state(c, PAUSED_KEY) or {}
    if isinstance(paused, dict):
        for aid, info in sorted(paused.items()):
            ws = str((info or {}).get("workspace") or "")
            present = bool(ws) and Path(ws).expanduser().is_dir()
            ck.add(f"agent_paused:{aid}", f"agent {aid}", False,
                   f"paused — {(info or {}).get('reason')}: {ws}" + (" (thư mục NAY ĐÃ CÓ)" if present else ""), "warning",
                   fix=f"cfharness agent resume {aid}" if present else f"tạo/clone {ws} rồi cfharness agent resume {aid}")
    if c is not None and "agents" in _tables(c):  # agent khác (không do nhập tạm dừng) có workspace không tồn tại
        for r in c.execute("SELECT id, workspace_default FROM agents WHERE archived_at IS NULL AND workspace_default IS NOT NULL AND status != 'paused'"):
            ws = str(r["workspace_default"] or "")
            if ws and not Path(ws).expanduser().is_dir():
                ck.add(f"agent_ws:{r['id']}", f"workspace agent {r['id']}", False, f"không có {ws}", "warning",
                       fix=f"tạo {ws} hoặc cfharness agent edit {r['id']} --workspace <thư mục>")


def _schedules(ck: _Checks, c: sqlite3.Connection | None) -> None:
    if c is None or "schedules" not in _tables(c):
        return
    rows = c.execute("SELECT id, paused_by FROM schedules WHERE status='paused' AND deleted_at IS NULL").fetchall()
    by_import = [r["id"] for r in rows if r["paused_by"] == "import"]
    if by_import:
        ck.add("schedules", "lịch tạm dừng do nhân bản", False, f"{len(by_import)} lịch: {', '.join(by_import[:8])}", "info",
               fix="cfharness schedule list → cfharness schedule resume <id> cho lịch cần chạy ở máy này (tránh chạy 2 lần)")
    elif rows:
        ck.add("schedules", "lịch tạm dừng", True, f"{len(rows)} lịch đang tạm dừng", "info")


def _followups(ck: _Checks, c: sqlite3.Connection | None) -> None:
    fu = _state(c, "import_followups")
    if not isinstance(fu, dict):
        return
    st = fu.get("status")
    res = fu.get("results") or {}
    if st == "done":
        ck.add("followups", "viết lại đường dẫn trong tri thức", True,
               f"xong lúc {fu.get('processed_at')}: {res.get('rewritten', 0)} file, {len(res.get('graph') or [])} tầng graph"
               + (f", bỏ qua {len(res.get('skipped') or [])} (lịch sử/ngoài ledger)" if res.get("skipped") else ""))
    elif st == "partial":
        ck.add("followups", "viết lại đường dẫn trong tri thức", False,
               f"còn {len(fu.get('retry') or [])} file lỗi: " + "; ".join(f"{e.get('path')}: {e.get('error')}" for e in (res.get("errors") or [])[:3]),
               "warning", fix="khởi động lại daemon (tự thử lại) — hoặc sửa tay qua `cfharness learn …`")
    else:
        ck.add("followups", "viết lại đường dẫn trong tri thức", False,
               f"CHỜ daemon lên lần đầu: {len(fu.get('files') or [])} file, {fu.get('graph_rows') or 0} hàng graph", "info",
               fix="cfharness daemon start")
    for m in fu.get("mcp_missing") or []:
        if not any(x["id"] == f"mcp:{m.get('server')}" for x in ck.items):
            ck.add(f"mcp:{m.get('server')}", f"MCP {m.get('server')}", False, f"lệnh {m.get('command')}: {m.get('reason')}",
                   fix=f"cfharness mcp edit {m.get('server')}")
    if fu.get("renamed"):
        ck.add("renamed", "file tri thức bị đổi tên khi nhập", True, f"{len(fu['renamed'])} file — tham chiếu theo tên cũ cần người sửa", "info")


def _origin(ck: _Checks, c: sqlite3.Connection | None) -> None:
    from .hub_freeze import KEY as FROZEN, banner

    imp = _state(c, "imported_from")
    if isinstance(imp, dict):
        ck.add("imported_from", "nhập từ", True, f"gói {imp.get('export_id')} (máy {imp.get('source_host') or '?'}, cfharness "
               f"{imp.get('source_version') or '?'}) chế độ {imp.get('mode')} lúc {imp.get('at')}", "info")
    cl = _state(c, "cloned_from")
    if isinstance(cl, dict):
        ck.add("cloned_from", "bản nhân bản", True, f"từ hub {cl.get('hub_id')} — hai hub độc lập, mỗi hub cần bot Telegram/Slack + "
               "đăng nhập MCP OAuth riêng", "info")
    mig = _state(c, FROZEN)
    if isinstance(mig, dict) and mig:
        ck.add("migrated_to", "đóng băng (đã chuyển nhà)", False, banner(mig), "warning", fix="cfharness hub unfreeze (chỉ khi KHÔNG dùng máy mới)")


def _daemon(ck: _Checks, paths: "HubPaths") -> None:
    from .home_migrate import daemon_running

    pid = daemon_running(paths.home)
    ck.add("daemon", "daemon", pid is not None, f"đang chạy (pid {pid})" if pid else "không chạy", "info", fix="cfharness daemon start")


def _autostart(ck: _Checks, paths: "HubPaths") -> None:
    try:
        from .systems.autostart import Autostart, status_text

        st = Autostart(paths).status()
    except Exception as e:  # pragma: no cover
        ck.add("autostart", "tự khởi động", False, str(e)[:200], "info")
        return
    if st.get("method") == "unsupported":
        ck.add("autostart", "tự khởi động", True, status_text(st), "info")
        return
    ck.add("autostart", "tự khởi động", bool(st.get("installed")), status_text(st), "info",
           fix="cfharness daemon install-service" + (" (hoặc sudo cfharness admin install-daemon --user <tên> cho máy chủ macOS)"
                                                   if sys.platform == "darwin" else ""))


def _vector(ck: _Checks, cfg: dict[str, Any]) -> None:
    have = importlib.util.find_spec("fastembed") is not None
    ck.add("vector", "graph memory vector (cfharness[vector])", have, "đã cài" if have else "chưa cài — recall chỉ dùng FTS5", "info",
           fix="uv tool install --force 'cfharness[vector]' (cùng nguồn cài hiện tại)")


def _perms(ck: _Checks, paths: "HubPaths") -> None:
    if os.name != "posix":
        ck.add("perms", "quyền file", True, "Windows: dùng ACL hồ sơ người dùng (oscompat)", "info")
        return
    bad: list[str] = []
    home = paths.home

    def chk(p: Path, want_max: int) -> None:
        try:
            m = stat.S_IMODE(os.lstat(p).st_mode)
        except OSError:
            return
        if m & ~want_max:
            bad.append(f"{p.name if p != home else 'HUB_HOME'} {oct(m)}")

    chk(home, 0o700)
    chk(paths.db, 0o600)
    for d in ("secrets", "auth"):
        dp = home / d
        chk(dp, 0o700)
        try:
            for f in os.listdir(dp):
                fp = dp / f
                if fp.is_dir() and not fp.is_symlink():  # vd secrets/filehub/ (token thiết bị Không gian số): thư mục 0700, file 0600
                    chk(fp, 0o700)
                    try:
                        for g in os.listdir(fp):
                            chk(fp / g, 0o600)
                    except OSError:
                        pass
                else:
                    chk(fp, 0o600)
        except OSError:
            pass
    ck.add("perms", "quyền file (secrets/auth/hub.db chỉ chủ)", not bad, "đúng" if not bad else "rộng hơn cần: " + ", ".join(bad[:8]),
           "warning", fix=f"chmod 700 {home} {home / 'secrets'} {home / 'auth'}; chmod 600 {paths.db} {home / 'secrets'}/*")


def text(rep: dict[str, Any]) -> str:
    mark = {"ok": "✓", "info": "·", "warning": "!", "error": "✗"}
    out = [f"hub doctor — {rep.get('home')}"]
    for x in rep.get("checks") or []:
        out.append(f"{mark.get(x['level'], '?')} {x['check']}: {x['detail']}")
        if x.get("fix") and x["level"] != "ok":
            out.append(f"    → {x['fix']}")
    s = rep.get("summary") or {}
    out.append(f"{s.get('errors', 0)} lỗi, {s.get('warnings', 0)} cảnh báo" + (" — hub sẵn sàng" if rep.get("ok") else ""))
    return "\n".join(out)


def register_rpc(daemon: "Daemon") -> None:
    """`hub.doctor {}` (HUMAN_ONLY, chỉ đọc) — cùng kết quả `cfharness hub doctor --json` (không probe mạng)."""
    import asyncio

    hub = daemon.hub

    async def _doctor(p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(run, hub.paths)

    daemon.methods["hub.doctor"] = _doctor


__all__ = ["run", "text", "register_rpc"]
