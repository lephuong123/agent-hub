"""`cfharness hub doctor` — kiểm hub SAU khi nhập từ máy khác (PR5 của docs/dev-notes/plan-hub-clone.md §7). CHỈ ĐỌC + gợi ý lệnh; chạy
được nhiều lần; chạy được khi daemon DỪNG (đọc hub.db ở chế độ chỉ đọc, config.yaml, TÊN secret — không bao giờ đọc giá trị secret).

Kiểm: `claude` trên PATH + phiên bản so `runtime.claude.tested_versions` + đăng nhập (secret `CLAUDE_CODE_OAUTH_TOKEN`/`CLAUDE_ACCT_*`, cờ
`auth_block`; `--probe` mới chạy `claude auth status` — không bao giờ gọi model), `${NAME}` trong config.yaml + cấu hình MCP có secret/env,
MCP (đánh dấu thiếu lệnh của lần nhập, OAuth chưa kết nối; `--probe` + daemon chạy → `mcp.probe` từng server), Telegram/Slack bật + có token,
thư mục chung Không gian số (filehub: cần dán khoá cổng sau nhập, thư mục đồng bộ cũ cần chuyển, cổng thiếu khoá), repo workspace (có chưa, HEAD khớp
`repos.json` của gói), agent bị tạm dừng vì thiếu workspace, lịch đang tạm dừng (clone), việc viết lại
tri thức còn dở (`import_followups`), `imported_from`/`cloned_from`/`migrated_to`, tự khởi động (gợi ý đúng OS), `cfharness[vector]`,
`pdftoppm` (poppler, cảnh báo — `tools.poppler_path`, pdf_support.py),
quyền file (POSIX), dung lượng HUB_HOME theo mục cấp 1 + thư mục làm việc dọn được (`housekeeping.py` — chỉ báo; dọn = `--cleanup`,
cần daemon, RPC `hub.cleanup`). Kết quả `{ok, home, checks[{id, check, ok, level ok|info|warning|error, detail, fix?}], summary}`.

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
    except Exception as e:  # best-effort: lỗi được báo trong kết quả doctor
        cfg = {}
        ck.add("config", "config.yaml", False, f"không đọc được: {e}", fix=f"sửa {paths.config}")
    c = _ro(paths.db)
    try:
        if c is None:
            ck.add("hub_db", "hub.db", False, f"không có/không mở được {paths.db}", fix="cfharness hub import <gói> hoặc cfharness daemon start")
        secrets = _secret_names(home)
        _claude(ck, cfg, secrets, c, probe=probe, binary=claude_binary, home=home)
        _secret_refs(ck, paths, c, secrets)
        _mcp(ck, c, secrets, mcp_probe if probe else None)
        _claude_mcp(ck, c, home)
        _channels(ck, cfg, secrets)
        _filehub(ck, cfg, c, home)
        _repos(ck, c)
        _schedules(ck, c)
        _followups(ck, c)
        _origin(ck, c)
        _daemon(ck, paths)
        _autostart(ck, paths)
        _vector(ck, cfg)
        _poppler(ck, cfg)
        _perms(ck, paths)
        hk = _housekeeping(ck, paths, c)
        recent = _recent_errors(ck, paths)
    finally:
        if c is not None:
            c.close()
    errs = sum(1 for x in ck.items if x["level"] == "error")
    warns = sum(1 for x in ck.items if x["level"] == "warning")
    return {"ok": errs == 0, "home": str(home), "checks": ck.items, "summary": {"errors": errs, "warnings": warns, "total": len(ck.items)},
            "housekeeping": hk, "recent_errors": recent}


# ============================================================================ từng nhóm
def _claude(ck: _Checks, cfg: dict[str, Any], secrets: set[str], c: sqlite3.Connection | None, *, probe: bool, binary: str | None,
            home: Path | None = None) -> None:
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
    _connectors(ck, cfg, c, probe=probe, path=path, tok=bool(tok or accts), home=home)


def _connector_users(cfg: dict[str, Any], c: sqlite3.Connection | None) -> list[str]:
    """Agent (và điều phối viên) có connector claude.ai người cho phép."""
    out: list[str] = []
    if (((cfg.get("manager") or {}).get("capabilities") or {}).get("connectors")):
        out.append("(điều phối viên)")
    if (cfg.get("connectors") or {}).get("auto"):  # connectors.auto (mặc định bật): mọi run làm việc của agent dùng connector
        out.append("(connectors.auto)")
    if c is not None and "agents" in _tables(c):
        for r in c.execute("SELECT id, tool_policy FROM agents WHERE deleted_at IS NULL"):
            try:
                tp = json.loads(r["tool_policy"] or "{}")
            except ValueError:
                continue
            if isinstance(tp, dict) and tp.get("connectors"):
                out.append(str(r["id"]))
    return out


def _connector_account(c: sqlite3.Connection | None, home: Path | None) -> tuple[str, Path | None]:
    """(nhãn tài khoản đang dùng, thư mục cấu hình claude riêng | None) theo con trỏ `claude_account_active` (doctor không đọc giá
    trị secret nên không so token): tài khoản token → `HUB_HOME/claude-config/<id>/`; machine/chưa cấu hình → cấu hình mặc định."""
    from .accounts import claude_config

    aid = _state(c, "claude_account_active")
    d = claude_config.account_dir(home, str(aid)) if (home is not None and isinstance(aid, str)) else None
    return (str(aid) if d is not None else "đăng nhập của máy"), d


def _connectors(ck: _Checks, cfg: dict[str, Any], c: sqlite3.Connection | None, *, probe: bool, path: str | None, tok: bool,
                home: Path | None = None) -> None:
    """Token `setup-token` không nạp connector claude.ai → run có connector cần phiên claude.ai của TÀI KHOẢN ĐANG DÙNG (tài khoản
    token: thư mục cấu hình riêng; machine: phiên `/login` mặc định của user OS) — docs/dev-notes/manager-connectors.md,
    `runtime.claude.connectors_auth`. `connectors.auto` tính là "đang dùng connector"."""
    users = _connector_users(cfg, c)
    if not users:
        return
    who = ", ".join(users[:5]) + ("…" if len(users) > 5 else "")
    mode = str(((cfg.get("runtime") or {}).get("claude") or {}).get("connectors_auth") or "auto")
    acct, cdir = _connector_account(c, home)
    fix = ("web hub › Cài đặt › Tài khoản Claude › «Gắn connector» ở tài khoản đang dùng (hoặc `cfharness account connector "
           f"{acct if cdir is not None else 'machine'} login`), rồi kết nối connector trên claude.ai → Settings → Connectors")
    if mode == "token":
        ck.add("claude_connectors", "connector claude.ai", False, f"{who}: runtime.claude.connectors_auth=token — token setup-token không nạp connector",
               "warning", fix="cfharness config set runtime.claude.connectors_auth auto")
        return
    if not (probe and path):
        if tok:
            ck.add("claude_connectors", "connector claude.ai", True, f"{who}: cần phiên claude.ai của tài khoản {acct} — chạy --probe để kiểm", "warning")
        return
    if cdir is not None and not cdir.is_dir():
        ck.add("claude_connectors", "connector claude.ai", False, f"{who}: tài khoản {acct} CHƯA gắn connector — connector sẽ không nạp",
               "warning", fix=fix)
        return
    if cdir is not None:
        from .accounts import claude_config

        if not claude_config.shared(cdir):  # run dựng lại symlink trước khi dùng; vẫn không phải symlink → run rơi về token
            ck.add("claude_connectors", "connector claude.ai", False,
                   f"{who}: thư mục cấu hình của tài khoản {acct} không dùng chung session (projects không phải symlink) — run connector "
                   "chạy bằng token (connector.auth_fallback config_dir_unshared)", "warning",
                   fix=f"tạo symlink {cdir}/projects → {claude_config.default_home()}/projects (Windows: bật Developer Mode / quyền tạo symlink)")
            return
    try:
        from .oscompat import exec_argv, no_window_kwargs
        from .runtime.claude_cli import OAUTH_TOKEN_SECRET, child_env

        env = child_env((cfg.get("runtime") or {}).get("claude") or {})
        env.pop(OAUTH_TOKEN_SECRET, None)
        if cdir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(cdir)
        r = subprocess.run(exec_argv([path, "auth", "status"]), capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                           env=env, **no_window_kwargs())
        info = json.loads(r.stdout)
        ok = bool(info.get("loggedIn")) and info.get("authMethod") == "claude.ai"
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as e:
        ck.add("claude_connectors", "connector claude.ai", False, f"không kiểm được: {type(e).__name__}", "warning", fix=fix)
        return
    ck.add("claude_connectors", "connector claude.ai", ok,
           f"{who}: tài khoản {acct} " + ("có phiên claude.ai — run connector dùng phiên này" if ok else "CHƯA có phiên claude.ai — connector sẽ không nạp"),
           "warning", fix=None if ok else fix)


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

    hub_oauth: list[str] = []
    for r in rows:
        name = r["name"]
        try:
            conf = json.loads(r["config"] or "{}") or {}
        except ValueError:
            conf = {}
        if conf.get("source") != "claude" and isinstance(conf.get("auth"), dict) and conf["auth"].get("type") == "oauth":
            hub_oauth.append(name)
        perr = str(r["probe_error"] or "")
        if conf.get("source") == "claude":  # cấu hình + token do claude giữ theo tài khoản — hub không probe (plan-mcp-claude-native)
            ck.add(f"mcp:{name}", f"MCP {name}", True, f"{r['transport']} · nguồn claude (cấu hình + token theo tài khoản Claude đang dùng)")
            continue
        if probe is not None:
            try:
                pr = probe(name) or {}
                ck.add(f"mcp:{name}", f"MCP {name}", bool(pr.get("ok")), f"kết nối được, {len(pr.get('tools') or {})} tool" if pr.get("ok")
                       else f"không kết nối được: {pr.get('error')}", fix=f"cfharness mcp edit {name}")
            except Exception as e:  # best-effort: lỗi được báo trong kết quả doctor  # noqa: BLE001
                ck.add(f"mcp:{name}", f"MCP {name}", False, f"không thử được: {e}", "warning", fix=f"cfharness mcp probe {name}")
            continue
        if perr.startswith("[hub]"):
            ck.add(f"mcp:{name}", f"MCP {name}", False, perr[:200], fix=f"cfharness mcp edit {name}")
        elif isinstance(conf.get("auth"), dict) and conf["auth"].get("type") == "oauth" and oauth_secret(name) not in secrets:
            ck.add(f"mcp:{name}", f"MCP {name}", False, "OAuth chưa kết nối ở máy này", "warning", fix=f"cfharness mcp login {name}")
        else:
            ck.add(f"mcp:{name}", f"MCP {name}", True, f"{r['transport']}" + (f" · lần thử cuối lỗi: {perr[:120]}" if perr else "")
                   + ("" if r["last_probe_at"] else " · chưa thử (--probe)"))
    if hub_oauth:  # plan-mcp-claude-native PR4: OAuth tự viết của hub sẽ bị gỡ (giai đoạn B) — khuyên chuyển sang claude theo tài khoản
        ck.add("mcp_hub_oauth", "MCP OAuth do hub giữ", False,
               f"nên chuyển sang claude: {', '.join(hub_oauth[:8])} — OAuth + proxy của hub sẽ bị gỡ ở bản sau", "warning",
               fix="; ".join(f"cfharness mcp migrate-to-claude {n} --account <id>" for n in hub_oauth[:3]))


def _claude_mcp(ck: _Checks, c: sqlite3.Connection | None, home: Path) -> None:
    """MCP do claude quản lý (plan-mcp-claude-native PR3) của TÀI KHOẢN ĐANG DÙNG: server đã đăng ký nguồn claude mà cấu hình claude của
    tài khoản không có (run của agent gán server đó sẽ hỏng), và server cần đăng nhập theo lần kiểm gần nhất (`hub_state mcp.claude_status`,
    cập nhật bởi `cfharness mcp claude status` / nút «Kiểm tra MCP» — doctor KHÔNG chạy `claude mcp list`)."""
    if c is None:
        return
    from .mcp_client import claude_source as cs
    from .mcp_client.claude_admin import CACHE_KEY, DEFAULT_KEY

    _import_mcp_claude(ck, c, home)
    label, cdir = _connector_account(c, home)
    key = DEFAULT_KEY if cdir is None else cdir.name
    acct = "" if cdir is None else f" --account {cdir.name}"
    try:
        have = cs.all_servers(cdir)
    except Exception as e:  # best-effort: doctor báo, không dừng  # noqa: BLE001
        ck.add("mcp_claude", "MCP nguồn claude", False, f"không đọc được cấu hình claude ({type(e).__name__})", "warning")
        return
    reg: list[str] = []
    if "mcp_servers" in _tables(c):
        for r in c.execute("SELECT name, config FROM mcp_servers WHERE deleted_at IS NULL ORDER BY name").fetchall():
            try:
                if (json.loads(r["config"] or "{}") or {}).get("source") == "claude":
                    reg.append(r["name"])
            except ValueError:
                continue
    missing = [n for n in reg if n not in have]
    if missing:
        ck.add("mcp_claude_missing", "MCP nguồn claude thiếu ở tài khoản đang dùng", False,
               f"tài khoản {label}: thiếu {', '.join(missing[:8])} — run của agent gán các server này sẽ hỏng", "warning",
               fix="; ".join(f"cfharness mcp claude add {n} --url <URL>{acct}" for n in missing[:3]))
    cache = _state(c, CACHE_KEY)
    entry = cache.get(key) if isinstance(cache, dict) else None
    servers = (entry or {}).get("servers") if isinstance(entry, dict) else None
    if not isinstance(servers, dict):
        if have:
            ck.add("mcp_claude_auth", "Đăng nhập MCP nguồn claude", True,
                   f"tài khoản {label}: {len(have)} server, chưa kiểm trạng thái (cfharness mcp claude status)", "info", fix=f"cfharness mcp claude status{acct}")
        return
    need = sorted(n for n, st in servers.items() if st == "needs_auth" and n in have)
    if need:
        ck.add("mcp_claude_auth", "Đăng nhập MCP nguồn claude", False,
               f"tài khoản {label}: cần đăng nhập {', '.join(need[:8])} (kiểm lúc {entry.get('checked_at') or '?'})", "warning",
               fix="; ".join(f"cfharness mcp claude login {n}{acct}" for n in need[:3]))
    elif have:
        ck.add("mcp_claude_auth", "Đăng nhập MCP nguồn claude", True, f"tài khoản {label}: không server nào cần đăng nhập "
               f"(kiểm lúc {entry.get('checked_at') or '?'})")


def _import_mcp_claude(ck: _Checks, c: sqlite3.Connection, home: Path) -> None:
    """Sau `hub import`: MCP nguồn claude phải thêm lại + đăng nhập lại trên tài khoản (`hub_state import_mcp_claude`, hub_transfer).
    Mục đã có trong cấu hình claude của tài khoản đó VÀ lần kiểm gần nhất `connected` thì thôi báo."""
    from .accounts import claude_config
    from .hub_transfer import MCP_CLAUDE_STATE, mcp_claude_steps
    from .mcp_client import claude_source as cs
    from .mcp_client.claude_admin import CACHE_KEY, DEFAULT_KEY

    items = _state(c, MCP_CLAUDE_STATE)
    if not isinstance(items, list) or not items:
        return
    cache = _state(c, CACHE_KEY)
    cache = cache if isinstance(cache, dict) else {}
    todo: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict) or not it.get("server"):
            continue
        left = []
        for a in it.get("accounts") or ["machine"]:
            a = str(a)
            cdir = claude_config.account_dir(home, a) if a.startswith("a_") else None
            key = DEFAULT_KEY if cdir is None else a
            try:
                have = it["server"] in cs.read_servers(cdir)
            except Exception:  # best-effort: không đọc được thì vẫn nhắc  # noqa: BLE001
                have = False
            st = ((cache.get(key) or {}).get("servers") or {}).get(it["server"]) if isinstance(cache.get(key), dict) else None
            if not (have and st == "connected"):
                left.append(a)
        if left:
            todo.append({**it, "accounts": left})
    if not todo:
        ck.add("mcp_claude_import", "MCP nguồn claude sau khi nhập", True, "đã thêm + đăng nhập lại đủ trên các tài khoản")
        return
    steps = [s for it in todo for s in mcp_claude_steps(it)]
    ck.add("mcp_claude_import", "MCP nguồn claude sau khi nhập", False,
           "đăng nhập lại MCP " + "; ".join(f"{it['server']} trên tài khoản {', '.join(it['accounts'])}" for it in todo[:6])
           + " — cấu hình + token ở máy cũ (url đầy đủ không đi theo gói: dùng URL gốc của server)", "warning", fix="; ".join(steps[:4]))


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
    """Thư mục chung Không gian số (qua cổng cfharness): lần nhập còn chờ khoá (`hub_state.import_filehub`), thư mục còn cấu hình đồng bộ
    2 chiều cũ (đã bỏ — cần chuyển), cổng đã cấu hình mà máy này chưa có khoá (`secrets/FILEHUB_LANE_*` — chỉ xét TÊN tệp, không đọc)."""
    from .filehub.service import legacy_text
    from .hub_transfer import FILEHUB_COMMAND

    fh = (cfg.get("filehub") or {}) if cfg else {}
    imp = _state(c, "import_filehub")
    folders = [f for f in fh.get("folders") or [] if isinstance(f, dict) and f.get("name")]
    servers = ((fh.get("lane") or {}).get("servers") or {}) if isinstance(fh.get("lane"), dict) else {}
    if not fh.get("enabled") and not folders and not imp and not servers:
        ck.add("filehub", "Không gian số", True, "chưa thiết lập", "info")
        return

    def _legacy(f: dict[str, Any]) -> bool:
        m = f.get("mode")
        return m == "sync" or (m not in ("sync", "lane") and fh.get("mode") != "lane" and bool(f.get("local_path")))

    legacy = [str(f["name"]) for f in folders if _legacy(f)]
    no_key = sorted(str(n) for n in servers if not (home / "secrets" / f"FILEHUB_LANE_{str(n).upper()}").is_file())
    if imp:
        ck.add("filehub", "Không gian số", False, "hub nhập từ máy khác — dán khoá cổng cfharness ở máy này", "warning", fix=FILEHUB_COMMAND)
    elif legacy:
        ck.add("filehub", "Không gian số", False, "; ".join(legacy_text(n) for n in legacy[:5]), "warning", fix=FILEHUB_COMMAND)
    elif no_key:
        ck.add("filehub", "Không gian số", False, f"cổng {', '.join(no_key)} chưa có khoá ở máy này", "warning", fix=FILEHUB_COMMAND)
    elif not servers:
        ck.add("filehub", "Không gian số", False, "có cấu hình nhưng chưa dán khoá cổng cfharness", "warning", fix=FILEHUB_COMMAND)
    else:
        ck.add("filehub", "Không gian số", True, ("bật" if fh.get("enabled") else "TẮT") + f", {len(folders)} thư mục chung qua cổng",
               "ok" if fh.get("enabled") else "info")


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
    except Exception as e:  # best-effort: lỗi được báo trong kết quả doctor  # pragma: no cover
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


def _poppler(ck: _Checks, cfg: dict[str, Any]) -> None:
    """`pdftoppm` (poppler) cho tool Read đọc PDF có `pages` — thiếu chỉ là cảnh báo: hub vẫn trích chữ + tách phần PDF và nhắc agent
    đọc PDF chỉ với `file_path` (docs/dev-notes/pdf-attachments.md)."""
    from .pdf_support import CONFIG_KEY, FIX_HINT, pdftoppm_status, validate_poppler_path

    configured = ((cfg.get("tools") or {}).get("poppler_path")) if cfg else None
    bad = ""
    if configured is not None:
        try:
            validate_poppler_path(configured)
        except ValueError as e:
            bad = str(e)
    st = pdftoppm_status(cfg, ttl_s=0, block=True)  # doctor cần kết quả thật (không lấy `pending`)
    if bad:
        ck.add("pdftoppm", "pdftoppm (poppler) cho đọc PDF", False, f"{bad}" + (f" — đang dùng {st['path']} trên PATH" if st["usable"] else ""),
               "warning", fix=f"sửa {CONFIG_KEY} (thư mục chứa pdftoppm) hoặc `cfharness config set {CONFIG_KEY} null`")
    elif st["usable"]:
        ck.add("pdftoppm", "pdftoppm (poppler) cho đọc PDF", True, f"{st['path']}" + (f" ({CONFIG_KEY})" if st["source"] == "config" else ""))
    elif st.get("broken"):
        ck.add("pdftoppm", "pdftoppm (poppler) cho đọc PDF", False,
               f"{st['broken']} có nhưng `pdftoppm -v` không chạy được — Read của agent vẫn lỗi khi đọc PDF theo trang", "warning",
               fix="cài lại poppler — " + FIX_HINT)
    else:
        ck.add("pdftoppm", "pdftoppm (poppler) cho đọc PDF", False,
               "không có — Read của agent không đọc PDF theo trang (`pages`); hub vẫn trích chữ + tách phần PDF đính kèm", "warning",
               fix=FIX_HINT)


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


DISK_FREE_WARN = 2 * 1024 ** 3  # chỗ trống ổ chứa HUB_HOME dưới 2 GB → cảnh báo


def _housekeeping(ck: _Checks, paths: "HubPaths", c: sqlite3.Connection | None) -> dict[str, Any] | None:
    """Dung lượng HUB_HOME theo mục cấp 1 + thư mục làm việc dọn được (housekeeping.py). CHỈ BÁO — dọn = `hub doctor --cleanup`."""
    from .housekeeping import LABELS, human, report

    try:
        hk = report(paths.home, c)
    except Exception as e:  # noqa: BLE001 — quét đĩa lỗi không làm hỏng doctor
        ck.add("disk", "dung lượng HUB_HOME", False, f"không quét được: {e}"[:200], "info")
        return None
    du = hk["disk"]
    top = ", ".join(f"{x['name']} {human(x['bytes'])}" for x in du["entries"][:6] if x["bytes"])
    ws = du.get("agent_workspaces") or {}
    disk = du.get("disk") or {}
    low = bool(disk) and disk.get("free", 0) < DISK_FREE_WARN
    ck.add("disk", "dung lượng HUB_HOME", True, f"{human(du['total'])} — {top or 'trống'}"
           + (f" · workspaces của agent {human(ws.get('bytes'))}" if ws.get("bytes") else "")
           + (f" · ổ đĩa còn trống {human(disk.get('free'))}" if disk else ""), "info")
    if low:
        ck.add("disk_free", "chỗ trống ổ đĩa", False, f"chỉ còn {human(disk.get('free'))} trên ổ chứa HUB_HOME", "warning",
               fix="cfharness hub doctor --cleanup; giảm retention.checkpoints_days / backup.keep; xoá gói cũ trong exports/")
    cl = hk["cleanup"]
    kinds = "; ".join(f"{v['count']} {LABELS.get(k, k)} ({human(v['bytes'])})" for k, v in sorted(cl["by_kind"].items())
                      if k != "scratch_stale")
    n = cl["cleanable"]["count"]
    ck.add("cleanup", "thư mục làm việc dọn được", n == 0,
           (f"{n} thư mục, {human(cl['cleanable']['bytes'])}: {kinds}" if n else "không có")
           + (f" · {cl['busy']} thư mục đang có run/task dùng (giữ)" if cl["busy"] else ""),
           "warning", fix="cfharness hub doctor --cleanup (liệt kê rồi hỏi y/N; không xoá run/ledger/audit)")
    st = cl["stale"]
    if st["count"]:
        ck.add("cleanup_stale", f"workspace scratch của run đã xong > {st['days']:.0f} ngày", True,
               f"{st['count']} thư mục, {human(st['bytes'])} — giữ để `run continue` còn thấy file", "info")
        ck.items[-1]["fix"] = f"cfharness hub doctor --cleanup --older-than {st['days']:.0f}"
    for t in cl.get("task_worktrees") or []:
        ck.add(f"task_worktree:{t['path']}", "git worktree của task đã xong", True, f"{t['path']} (task {t['task']}) — hub không tự xoá", "info")
        ck.items[-1]["fix"] = t["fix"]
    return hk


def _recent_errors(ck: _Checks, paths: "HubPaths") -> dict[str, Any] | None:
    """L6 (B5): top 5 thông điệp ERROR 24 h trong log (mọi tiến trình, gom theo logger + mẫu thông điệp, có req_id mẫu). Trần byte quét."""
    from .log_query import recent_errors

    try:
        rep = recent_errors(paths.logs_dir)
    except Exception as e:  # best-effort: doctor không được hỏng vì log hỏng
        ck.add("recent_errors", "Lỗi gần đây (24 giờ)", False, f"không đọc được log: {type(e).__name__}", "info")
        return None
    n = int(rep.get("total") or 0)
    ck.add("recent_errors", "Lỗi gần đây (24 giờ)", n == 0, "không có" if n == 0 else
           f"{n} dòng ERROR, {rep.get('distinct', 0)} loại" + (" (chạm trần quét)" if rep.get("truncated") else ""), "warning",
           fix="cfharness logs --level error --since 24h  ·  cfharness trace <req_id>")
    return rep


def cleanup_text(plan: dict[str, Any]) -> str:
    """Danh sách thư mục sẽ dọn (cho bước xác nhận y/N của `hub doctor --cleanup`)."""
    from .housekeeping import LABELS, human

    items = [x for x in plan.get("items") or [] if x.get("cleanable")]
    out = [f"{len(items)} thư mục dọn được ({human(sum(x['bytes'] for x in items))}):"]
    for x in items:
        out.append(f"  - {x['rel']}  {human(x['bytes'])}  [{LABELS.get(x['kind'], x['kind'])}]")
    busy = [x for x in plan.get("items") or [] if x.get("busy")]
    if busy:
        out.append(f"giữ {len(busy)} thư mục đang có run/task dùng: " + ", ".join(x["rel"] for x in busy[:8]))
    return "\n".join(out)


def text(rep: dict[str, Any]) -> str:
    mark = {"ok": "✓", "info": "·", "warning": "!", "error": "✗"}
    out = [f"hub doctor — {rep.get('home')}"]
    for x in rep.get("checks") or []:
        out.append(f"{mark.get(x['level'], '?')} {x['check']}: {x['detail']}")
        if x.get("fix") and x["level"] != "ok":
            out.append(f"    → {x['fix']}")
    groups = (rep.get("recent_errors") or {}).get("groups") or []
    if groups:
        from .termsafe import one_line

        out.append("Lỗi gần đây (24 giờ, gom theo logger + mẫu thông điệp):")
        for g in groups:
            rid = f"  · mẫu {g['req_id']}" if g.get("req_id") else ""
            out.append(f"  {g.get('count', 0):>4}× {one_line(str(g.get('logger') or ''), 80)}: {one_line(str(g.get('template') or ''), 160)}{rid}")
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
    # dung lượng + dọn thư mục làm việc: hub.housekeeping (đọc) + hub.cleanup (tự audit) — housekeeping.py
    from .housekeeping import register_rpc as _register_housekeeping

    _register_housekeeping(daemon)


__all__ = ["run", "text", "register_rpc"]
