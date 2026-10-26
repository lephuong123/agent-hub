"""Chính sách an toàn (FR-SG-02, FR-SG-05): tài nguyên được bảo vệ + mở rộng danh mục phá huỷ.

Lưu ở `HUB_HOME/policy/` — NGOÀI home của agent, tự nằm trong danh sách bảo vệ:
- `policy/protected.yaml`  : tài nguyên được bảo vệ do NGƯỜI khai báo (`cfharness guard protect add|rm`).
- `policy/catalog.yaml`    : mở rộng danh mục phá huỷ (regex lệnh Bash, mẫu tên tool MCP) do người thêm.
- `policy/history/`        : bản sao mỗi lần sửa (có phiên bản) — chỉ daemon ghi theo lệnh của người.
Agent không ghi được: settings.json của run có `permissions.deny` cho Edit/Write/MultiEdit/NotebookEdit vào
`policy/`, và bộ phân loại coi mọi lệnh Bash ghi vào đó là phá huỷ (mở approval).

Mặc định luôn bảo vệ (không xoá được bằng `protect rm`): `HUB_HOME/{policy,ledger,snapshots,secrets,hub.db*,
config.yaml,mcp,audit,trash,runs}`, mã nguồn gói `cfharness` (hook/guard chạy từ đó), `~/.ssh`, `~/.claude*`,
`~/.claude.json`, file rc của shell, `**/.env*`, nhánh `main|master|release/*` của mọi repo.

Thao tác (`ops_protected`) có thứ bậc: `delete` ⊂ `overwrite` ⊂ `write`. Tài nguyên bảo vệ `write` thì mọi
sửa/xoá đều là phá huỷ; bảo vệ `delete` thì chỉ xoá.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..oscompat import IS_WINDOWS

KINDS = ("path", "repo", "db", "mcp_tool", "hub")
ALWAYS_KINDS = ("exact", "web_domain", "mcp_tool")  # luật "luôn cho phép" (policy/always_allow.yaml)
OPS = ("delete", "overwrite", "write")
OP_LEVEL = {"delete": 1, "overwrite": 2, "write": 3}
HUB_KINDS = ("ledger", "events", "snapshots", "secrets", "policy", "audit", "config", "trash", "runs", "db", "mcp")


@dataclass
class ProtectedResource:
    id: str
    kind: str
    selector: str
    ops_protected: list[str] = field(default_factory=lambda: ["delete", "overwrite", "write"])
    note: str = ""
    created_by: str = "user"
    created_at: str = ""
    builtin: bool = False

    def guards(self, op: str) -> bool:
        lvl = max((OP_LEVEL.get(o, 3) for o in self.ops_protected), default=3)
        return OP_LEVEL.get(op, 3) <= lvl

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_MSYS_DRIVE = re.compile(r"^/{1,2}([A-Za-z])(?=/|$)")
_DRIVE = re.compile(r"^([A-Za-z]):/*")


def canon_path(p: str) -> str:
    """Dạng so sánh DUY NHẤT của một đường dẫn. POSIX: giữ nguyên (không đổi hành vi). Windows (Git Bash chạy lệnh của agent):
    `C:\a\b`, `C:/a/b`, `/c/a/b` (MSYS), `//c/a/b` (luật Claude Code) → `C:/a/b`; ổ đĩa viết hoa, gộp `//`, `.`/`..`;
    `C:x` (tương đối theo ổ) coi như `C:/x` (bảo thủ). Đường dẫn gốc không ổ đĩa (`/data/x`, `/dev/null`) giữ dạng POSIX.
    KHÔNG gập chữ hoa/thường — so sánh dùng `fold_path`."""
    if not IS_WINDOWS or not p:
        return p
    import posixpath

    s = p.replace("\\", "/")
    m = _MSYS_DRIVE.match(s)
    if m:
        s = m.group(1) + ":/" + s[m.end():].lstrip("/")
    m = _DRIVE.match(s)
    if m:
        rest = posixpath.normpath("/" + s[m.end():])
        return m.group(1).upper() + ":" + rest
    if s.startswith("/"):
        return "/" + posixpath.normpath(s).lstrip("/")  # posixpath giữ `//x` — gốc MSYS chỉ một `/`
    return posixpath.normpath(s) if s not in ("", ".") else s


def fold_path(p: str) -> str:
    """Khoá so sánh: Windows không phân biệt hoa/thường (NTFS) → `canon_path` + lower; POSIX: giữ nguyên."""
    return canon_path(p).lower() if IS_WINDOWS else p


def _glob_regex(pat: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pat.find("]", i)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                out.append("[" + pat[i + 1 : j].replace("!", "^", 1) + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.I if IS_WINDOWS else 0)


def _has_glob(s: str) -> bool:
    return any(c in s for c in "*?[")


def path_selector_matches(selector: str, path: str) -> bool:
    """`path` (tuyệt đối, đã chuẩn hoá) nằm trong tài nguyên `selector` (glob hoặc tiền tố thư mục).
    Windows: so trên `canon_path` (selector có thể là `C:\…`, `/c/…`), không phân biệt hoa/thường."""
    if IS_WINDOWS:
        selector, path = canon_path(selector), fold_path(path)
        if not _has_glob(selector):
            selector = selector.lower()
    if not _has_glob(selector):
        sel = selector.rstrip("/") or "/"
        return path == sel or path.startswith(sel + "/") or sel == "/"
    rx = _glob_regex(selector)
    if rx.match(path):
        return True
    # một tổ tiên của path khớp (vd selector '~/.claude*' và path '~/.claude/x')
    p = path
    while True:
        parent = os.path.dirname(p)
        if parent == p:
            return False
        if rx.match(parent):
            return True
        p = parent


def selector_root(selector: str) -> str | None:
    """Phần tiền tố cố định (trước ký tự glob đầu tiên) — dùng để biết xoá thư mục cha có chạm tài nguyên không."""
    selector = canon_path(selector)
    if selector.startswith("**"):
        return None
    if not _has_glob(selector):
        return selector.rstrip("/") or "/"
    idx = min(i for i in (selector.find(c) for c in "*?[") if i >= 0)
    root = selector[:idx]
    return root.rstrip("/") if "/" in root else None


@dataclass
class Policy:
    hub_home: Path
    user_home: Path
    resources: list[ProtectedResource]
    bash_patterns: list[dict[str, str]] = field(default_factory=list)  # [{pattern, note}]
    mcp_patterns: list[str] = field(default_factory=list)  # glob server/tool luôn phá huỷ
    version: int = 0

    # ------------------------------------------------------------------ path
    def match_path(self, path: str, op: str) -> ProtectedResource | None:
        for r in self.resources:
            if r.kind in ("path", "hub") and r.guards(op) and path_selector_matches(r.selector, path):
                return r
        return None

    def protected_under(self, directory: str) -> ProtectedResource | None:
        """Có tài nguyên được bảo vệ (theo đường dẫn) nằm BÊN TRONG `directory` (xoá đệ quy sẽ chạm)."""
        d = fold_path(directory).rstrip("/") or "/"
        for r in self.resources:
            if r.kind not in ("path", "hub") or not r.guards("delete"):
                continue
            root = selector_root(r.selector)
            if root and IS_WINDOWS:
                root = root.lower()
            if root and (d == "/" or root == d or root.startswith(d + "/")):
                return r
        return None

    def match_branch(self, branch: str, op: str) -> ProtectedResource | None:
        for r in self.resources:
            if r.kind == "repo" and r.guards(op):
                sel = r.selector.split(":", 1)[-1] if ":" in r.selector else r.selector
                if any(fnmatch.fnmatchcase(branch, s.strip()) for s in sel.split("|")):
                    return r
        return None

    def match_mcp(self, server: str, tool: str) -> ProtectedResource | None:
        name = f"{server}/{tool}"
        for r in self.resources:
            if r.kind == "mcp_tool" and fnmatch.fnmatchcase(name, r.selector):
                return r
        for pat in self.mcp_patterns:
            if fnmatch.fnmatchcase(name, pat):
                return ProtectedResource("catalog", "mcp_tool", pat, note="policy/catalog.yaml", builtin=True)
        return None

    def match_db(self, text: str) -> ProtectedResource | None:
        low = text.lower()
        for r in self.resources:
            if r.kind != "db":
                continue
            dsn, _, table = r.selector.partition(":")
            if dsn and dsn.lower() not in low:
                continue
            if not table or any(fnmatch.fnmatchcase(tok, table.lower()) for tok in re.findall(r"[\w.]+", low)):
                return r
        return None

    @property
    def policy_dir(self) -> Path:
        return self.hub_home / "policy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "policy_dir": str(self.policy_dir),
            "resources": [r.to_dict() for r in self.resources],
            "bash_patterns": self.bash_patterns,
            "mcp_patterns": self.mcp_patterns,
        }


def package_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def default_resources(hub_home: Path, user_home: Path) -> list[ProtectedResource]:
    h, u = str(hub_home), str(user_home)
    items: list[tuple[str, str, str, list[str]]] = [
        ("hub-policy", "hub", f"{h}/policy", ["write"]),
        ("hub-ledger", "hub", f"{h}/ledger", ["write"]),
        ("hub-snapshots", "hub", f"{h}/snapshots", ["write"]),
        ("hub-secrets", "hub", f"{h}/secrets", ["write"]),
        ("hub-db", "hub", f"{h}/hub.db*", ["write"]),
        ("hub-config", "hub", f"{h}/config.yaml", ["write"]),
        ("hub-mcp", "hub", f"{h}/mcp", ["write"]),
        ("hub-audit", "hub", f"{h}/audit", ["write"]),
        ("hub-trash", "hub", f"{h}/trash", ["delete", "overwrite"]),
        ("hub-runs", "hub", f"{h}/runs", ["delete", "overwrite"]),
        ("hub-code", "path", str(package_dir()), ["write"]),
        ("ssh", "path", f"{u}/.ssh", ["write"]),
        ("claude-config", "path", f"{u}/.claude*", ["write"]),
        ("gnupg", "path", f"{u}/.gnupg", ["write"]),
        ("aws", "path", f"{u}/.aws", ["write"]),
        ("kube", "path", f"{u}/.kube", ["write"]),
        ("dotenv", "path", "**/.env*", ["delete", "overwrite"]),
    ]
    for rc in (".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".config/fish/config.fish", ".gitconfig"):
        items.append((f"rc{rc.replace('/', '-')}", "path", f"{u}/{rc}", ["write"]))
    items.append(("branches", "repo", "main|master|release/*", ["delete", "overwrite"]))
    return [ProtectedResource(i, k, s, ops, note="mặc định (BRD FR-SG-02)", created_by="hub", builtin=True) for i, k, s, ops in items]


class PolicyStore:
    """Đọc/ghi `HUB_HOME/policy/`. Chỉ daemon ghi, theo lệnh của người (CLI/TUI)."""

    def __init__(self, hub_home: Path, user_home: Path | None = None) -> None:
        self.hub_home = Path(hub_home)
        self.user_home = Path(user_home or os.path.expanduser("~"))
        self.dir = self.hub_home / "policy"
        self._cache: tuple[float, Policy] | None = None

    @property
    def protected_file(self) -> Path:
        return self.dir / "protected.yaml"

    @property
    def catalog_file(self) -> Path:
        return self.dir / "catalog.yaml"

    @property
    def repo_deny_file(self) -> Path:
        return self.dir / "repo_deny.yaml"

    # ------------------------------------------------------------------ MGR-2: mẫu chặn theo repo (người đặt)
    def _repo_deny_data(self) -> dict[str, Any]:
        return self._read(self.repo_deny_file)

    def repo_deny_list(self) -> dict[str, list[str]]:
        d = self._repo_deny_data().get("repos") or {}
        return {str(k): [str(x) for x in (v or [])] for k, v in d.items()} if isinstance(d, dict) else {}

    def repo_deny_add(self, repo: str, pattern: str) -> dict[str, list[str]]:
        repo = os.path.realpath(os.path.expanduser(repo))
        if not pattern.strip():
            raise ValueError("pattern rỗng")
        self.ensure()
        self._snapshot()
        data = self._repo_deny_data()
        repos = data.get("repos") if isinstance(data.get("repos"), dict) else {}
        pats = list(repos.get(repo) or [])
        if pattern not in pats:
            pats.append(pattern)
        repos[repo] = pats
        data["repos"] = repos
        data["version"] = int(data.get("version") or 1) + 1
        self._write(self.repo_deny_file, data)
        self._cache = None
        return self.repo_deny_list()

    def repo_deny_rm(self, repo: str, pattern: str | None = None) -> dict[str, list[str]]:
        repo = os.path.realpath(os.path.expanduser(repo))
        self.ensure()
        data = self._repo_deny_data()
        repos = data.get("repos") if isinstance(data.get("repos"), dict) else {}
        if repo not in repos:
            raise KeyError(repo)
        self._snapshot()
        if pattern is None:
            repos.pop(repo, None)
        else:
            repos[repo] = [p for p in repos.get(repo) or [] if p != pattern]
            if not repos[repo]:
                repos.pop(repo, None)
        data["repos"] = repos
        data["version"] = int(data.get("version") or 1) + 1
        self._write(self.repo_deny_file, data)
        self._cache = None
        return self.repo_deny_list()

    # ------------------------------------------------------------------ luật "luôn cho phép" (người tạo khi duyệt `--always`)
    @property
    def always_file(self) -> Path:  # kinds: ALWAYS_KINDS
        return self.dir / "always_allow.yaml"

    def always_list(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        """Luật hẹp: `exact` = agent + tool + dấu vân tay thao tác; `web_domain` = agent + host CHÍNH XÁC (không wildcard);
        `mcp_tool` = agent (bắt buộc) + ĐÚNG tên tool MCP đầy đủ — chỉ hạ verdict do annotations của server (approval-profiles.md).
        Đọc lỗi/không phải dict → [] (không luật nào = giữ chờ người, fail-closed)."""
        try:
            rules = self._read(self.always_file).get("rules") or []
        except Exception:
            return []
        out = [dict(r) for r in rules if isinstance(r, dict) and r.get("id") and r.get("kind") in ALWAYS_KINDS]
        return out if include_disabled else [r for r in out if not r.get("disabled")]

    def always_add(self, rule: dict[str, Any]) -> dict[str, Any]:
        if rule.get("kind") not in ALWAYS_KINDS:
            raise ValueError("kind luật luôn-cho-phép phải là exact|web_domain|mcp_tool")
        if rule["kind"] == "mcp_tool" and (not rule.get("agent_id") or not str(rule.get("tool") or "").startswith("mcp__")
                                           or any(c in str(rule.get("tool")) for c in "*?[]")):
            raise ValueError("luật mcp_tool cần agent_id + tên tool mcp__<server>__<tool> chính xác (không glob)")
        if rule["kind"] == "exact" and not (rule.get("tool") and rule.get("fingerprint")):
            raise ValueError("luật exact cần tool + fingerprint")
        if rule["kind"] == "web_domain" and (not rule.get("domain") or any(c in str(rule["domain"]) for c in "*?[]")):
            raise ValueError("luật web_domain cần host chính xác (không wildcard)")
        self.ensure()
        self._snapshot()
        data = self._read(self.always_file)
        rules = [r for r in (data.get("rules") or []) if isinstance(r, dict)]
        for r in rules:  # trùng (cùng agent + cùng đích) → trả luật cũ (bật lại nếu đã gỡ)
            same = r.get("kind") == rule["kind"] and r.get("agent_id") == rule.get("agent_id") and (
                (rule["kind"] == "exact" and r.get("tool") == rule.get("tool") and r.get("fingerprint") == rule.get("fingerprint"))
                or (rule["kind"] == "web_domain" and r.get("domain") == rule.get("domain"))
                or (rule["kind"] == "mcp_tool" and r.get("tool") == rule.get("tool")))
            if same and not r.get("disabled"):
                return dict(r)
        new = {"id": "aa_" + secrets.token_hex(4), **rule, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        rules.append(new)
        data["rules"] = rules
        data["version"] = int(data.get("version") or 1) + 1
        self._write(self.always_file, data)
        return new

    def always_rm(self, rid: str, *, by: str = "user") -> dict[str, Any]:
        """Gỡ (vô hiệu hoá, giữ lịch sử trong file + history/) — không xoá cứng để còn truy vết."""
        self.ensure()
        data = self._read(self.always_file)
        rules = [r for r in (data.get("rules") or []) if isinstance(r, dict)]
        hit = next((r for r in rules if r.get("id") == rid and not r.get("disabled")), None)
        if hit is None:
            raise KeyError(rid)
        self._snapshot()
        hit["disabled"] = True
        hit["disabled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        hit["disabled_by"] = by
        data["rules"] = rules
        data["version"] = int(data.get("version") or 1) + 1
        self._write(self.always_file, data)
        return dict(hit)

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "history").mkdir(exist_ok=True)
        if not self.protected_file.exists():
            self._write(self.protected_file, {"version": 1, "resources": []})
        if not self.catalog_file.exists():
            self._write(
                self.catalog_file,
                {
                    "version": 1,
                    "bash_patterns": [],  # [{pattern: "<regex>", note: "..."}] — luôn phá huỷ
                    "mcp_patterns": [],  # ["server/tool-glob"] — luôn phá huỷ
                },
            )

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.chmod(tmp, 0o644)
        tmp.replace(path)

    def _read(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}

    def _mtime(self) -> float:
        m = 0.0
        for p in (self.protected_file, self.catalog_file):
            try:
                m = max(m, p.stat().st_mtime_ns / 1e9)
            except OSError:
                pass
        return m

    def load(self) -> Policy:
        m = self._mtime()
        if self._cache and self._cache[0] == m:
            return self._cache[1]
        prot = self._read(self.protected_file)
        cat = self._read(self.catalog_file)
        res = default_resources(self.hub_home, self.user_home)
        for r in prot.get("resources") or []:
            try:
                res.append(ProtectedResource(**{k: v for k, v in r.items() if k in ProtectedResource.__dataclass_fields__}))
            except TypeError:
                continue
        pol = Policy(
            self.hub_home,
            self.user_home,
            res,
            bash_patterns=[p for p in cat.get("bash_patterns") or [] if isinstance(p, dict) and p.get("pattern")],
            mcp_patterns=[str(p) for p in cat.get("mcp_patterns") or []],
            version=int(prot.get("version") or 1) + int(cat.get("version") or 1),
        )
        self._cache = (m, pol)
        return pol

    def _snapshot(self) -> None:
        hist = self.dir / "history" / time.strftime("%Y%m%dT%H%M%S")
        hist.mkdir(parents=True, exist_ok=True)
        for p in (self.protected_file, self.catalog_file, self.always_file, self.repo_deny_file):
            if p.exists():
                shutil.copy2(p, hist / p.name)

    def add(self, kind: str, selector: str, ops: list[str] | None = None, note: str = "") -> ProtectedResource:
        if kind not in KINDS:
            raise ValueError(f"kind phải là một trong {KINDS}")
        ops = ops or ["delete", "overwrite", "write"]
        bad = [o for o in ops if o not in OPS]
        if bad:
            raise ValueError(f"ops không hợp lệ: {bad}; chọn trong {OPS}")
        if not selector.strip():
            raise ValueError("selector rỗng")
        if kind == "path":
            selector = os.path.expanduser(selector)
            if not selector.startswith("/") and not selector.startswith("**") and not (IS_WINDOWS and re.match(r"^[A-Za-z]:[\\/]", selector)):
                selector = os.path.abspath(selector)
            selector = canon_path(selector)
        if kind == "hub":
            if selector not in HUB_KINDS:
                raise ValueError(f"hub selector phải là một trong {HUB_KINDS}")
            selector = str(self.hub_home / {"db": "hub.db*", "config": "config.yaml", "events": "hub.db*"}.get(selector, selector))
        self.ensure()
        self._snapshot()
        data = self._read(self.protected_file)
        r = ProtectedResource(
            "pr_" + secrets.token_hex(4), kind, selector, ops, note, "user", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        )
        data.setdefault("resources", []).append(r.to_dict())
        data["version"] = int(data.get("version") or 1) + 1
        self._write(self.protected_file, data)
        self._cache = None
        return r

    def remove(self, rid: str) -> ProtectedResource:
        pol = self.load()
        for r in pol.resources:
            if r.id == rid and r.builtin:
                raise ValueError(f"'{rid}' là tài nguyên bảo vệ mặc định — không gỡ được")
        self.ensure()
        data = self._read(self.protected_file)
        keep, found = [], None
        for r in data.get("resources") or []:
            if r.get("id") == rid:
                found = r
            else:
                keep.append(r)
        if found is None:
            raise KeyError(rid)
        self._snapshot()
        data["resources"] = keep
        data["version"] = int(data.get("version") or 1) + 1
        self._write(self.protected_file, data)
        self._cache = None
        return ProtectedResource(**{k: v for k, v in found.items() if k in ProtectedResource.__dataclass_fields__})


def policy_json(pol: Policy) -> str:
    return json.dumps(pol.to_dict(), ensure_ascii=False)


def repo_deny_for(store: "PolicyStore", cwd: str) -> list[str]:
    """Mẫu chặn (disallowedTools kiểu Claude) áp cho run có `cwd` nằm TRONG repo người đã đặt (MGR-2)."""
    try:
        from ..workspace import git_root
    except Exception:  # pragma: no cover
        git_root = lambda _p: None  # type: ignore
    repos = store.repo_deny_list()
    if not repos:
        return []
    cwd_real = os.path.realpath(os.path.expanduser(cwd))
    root = git_root(cwd_real)
    fc, fr = fold_path(cwd_real), fold_path(root) if root else None
    out: list[str] = []
    my_urls: set[str] | None = None
    for repo, pats in repos.items():
        r = os.path.realpath(os.path.expanduser(repo))
        f = fold_path(r)
        if fc == f or fc.startswith(f + "/") or (fr and (fr == f or fr.startswith(f + "/"))):
            out.extend(pats)
            continue
        # R2-2: clone / `git worktree` đặt NGOÀI repo nhưng cùng remote → coi là cùng repo
        if root:
            if my_urls is None:
                my_urls = _remote_urls(root)
            if my_urls and my_urls & _remote_urls(r):
                out.extend(pats)
    return list(dict.fromkeys(out))


def _norm_url(u: str) -> str:
    u = u.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    u = re.sub(r"^[a-z+]+://([^@/]*@)?", "", u)  # bỏ scheme + user@
    u = re.sub(r"^[^@]+@", "", u).replace(":", "/", 1) if "://" not in u and "@" in u else u
    return u.lower()


def _remote_urls(repo: str) -> set[str]:
    try:
        from ..workspace import _git

        r = _git(repo, "remote", "-v")
    except Exception:
        return set()
    if r.returncode != 0:
        return set()
    return {_norm_url(line.split()[1]) for line in r.stdout.splitlines() if len(line.split()) >= 2}
