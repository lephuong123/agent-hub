"""Toàn vẹn chính sách (H-02 lớp 3, bất biến §4.3): phát hiện + khôi phục thay đổi NGOÀI RPC.

Bản chuẩn (`integrity_baseline`, migration v11 trong hub.db — daemon là writer duy nhất) gồm:
- mọi file dưới `HUB_HOME/policy/` (protected/catalog/learning.yaml, `golden/` bản sao, `history/`);
- `HUB_HOME/config.yaml` — chỉ các nhánh an toàn (`SECURITY_KEYS`) bị khôi phục; khoá khác do người sửa tay được nhận.

Bản chuẩn chỉ cập nhật khi chính daemon ghi theo lệnh của người (bọc `PolicyStore.add/remove`, `LearningPolicy.set`,
`GoldenStore._mirror`, `HubConfig.set`) hoặc người chạy `cfharness guard integrity accept`. Vòng kiểm (`guard.integrity.tick_s`)
và lúc khởi động so đĩa với bản chuẩn: lệch → khôi phục (bản bị sửa chuyển vào `trash/tamper/<ts>/` để không mất gì),
event `guard.tamper_detected` (+ Slack, audit). `hub.db` bị ghi bởi kết nối khác (`PRAGMA data_version`) hoặc bị thay file
(inode) → chỉ PHÁT HIỆN (event + Slack), không tự khôi phục.

Đây là lớp HẬU KIỂM: không ngăn được ghi trong khoảng giữa hai lần kiểm, nhưng không phụ thuộc cách agent viết lệnh.
Lớp chặn trước là sandbox OS (`guard/runtime.py`) và bộ phân loại.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import logging
import os
import posixpath
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import yaml

from ..errors import HubError
from ..events import make_event

if TYPE_CHECKING:
    from ..daemon import Daemon
    from ..hub import Hub

log = logging.getLogger("cfharness.guard.integrity")

POLICY = "policy"
CONFIG = "config.yaml"
SECURITY_KEYS = ("guard", "approval", "runtime", "secrets", "telemetry", "notify", "learning_guard", "manager", "schedules")  # M7 MGR-6: trần của điều phối viên
MAX_FILE = 2_000_000


def _islink(p: str | os.PathLike[str]) -> bool:
    """symlink HOẶC directory junction (Windows: agent tạo junction không cần quyền admin — `mklink /J` — tương đương
    symlink thư mục trên POSIX; `os.path.islink` không nhận junction)."""
    return os.path.islink(p) or os.path.isjunction(p)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class IntegrityMonitor:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.home = hub.paths.home
        self.task: asyncio.Task | None = None
        self._db_version: int | None = None
        self._db_ino: int | None = None
        self.last_check: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ cấu hình
    def cfg(self, key: str, default: Any) -> Any:
        try:
            v = self.hub.config.get(f"guard.integrity.{key}")
        except KeyError:
            return default
        return default if v is None else v

    # ------------------------------------------------------------------ đọc đĩa
    def scan_policy(self) -> dict[str, dict[str, Any]]:
        """{rel: {kind: file|dir|symlink|other, sha, content}} cho cây policy/ (KHÔNG theo symlink)."""
        out: dict[str, dict[str, Any]] = {}
        root = self.home / POLICY
        if _islink(root):
            out[POLICY] = {"kind": "symlink", "target": os.readlink(root)}
            return out
        if not root.exists():
            return out
        out[POLICY] = {"kind": "dir"}
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            names = list(dirnames) + filenames
            dirnames[:] = [d for d in dirnames if not _islink(Path(dirpath) / d)]  # ghi nhận junction nhưng KHÔNG đi vào
            for name in names:
                full = Path(dirpath) / name
                rel = full.relative_to(self.home).as_posix()  # khoá luôn dùng `/` (Windows: không thì mọi file thành 'lạ')
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                import stat as _st

                if _st.S_ISLNK(st.st_mode) or _islink(full):
                    out[rel] = {"kind": "symlink", "target": os.readlink(full)}
                elif _st.S_ISDIR(st.st_mode):
                    out[rel] = {"kind": "dir"}
                elif _st.S_ISREG(st.st_mode):
                    if st.st_size > MAX_FILE:
                        out[rel] = {"kind": "other", "size": st.st_size}
                        continue
                    try:
                        b = full.read_bytes()
                    except OSError:
                        continue
                    out[rel] = {"kind": "file", "sha": _sha(b), "content": b}
                else:
                    out[rel] = {"kind": "other"}
        return out

    def _config_security(self, raw: bytes | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            d = yaml.safe_load(raw.decode("utf-8")) or {}
        except Exception:
            return {"__unparseable__": True}
        if not isinstance(d, dict):
            return {"__unparseable__": True}
        return {k: d.get(k) for k in SECURITY_KEYS if k in d}

    def _read_config(self) -> bytes | None:
        p = self.home / CONFIG
        try:
            if _islink(p):
                return b"__symlink__"
            return p.read_bytes()
        except OSError:
            return None

    # ------------------------------------------------------------------ bản chuẩn (DB)
    @property
    def conn(self) -> Any:
        return self.hub.store.conn

    def baseline(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in self.conn.execute("SELECT path, kind, sha256, content FROM integrity_baseline"):
            out[r[0]] = {"kind": r[1], "sha": r[2], "content": bytes(r[3]) if r[3] is not None else None}
        return out

    def rebaseline(self, reason: str = "daemon_write") -> int:
        """Chụp bản chuẩn từ đĩa (gọi SAU khi chính daemon ghi theo lệnh người)."""
        cur = self.scan_policy()
        rows: list[tuple[Any, ...]] = []
        for rel, e in cur.items():
            if e["kind"] in ("file", "dir"):
                rows.append((rel, e["kind"], e.get("sha"), e.get("content"), _now()))
        cfg = self._read_config()
        if cfg is not None and cfg != b"__symlink__":
            rows.append((CONFIG, "config", _sha(cfg), cfg, _now()))
        with self.hub.store.tx():
            self.conn.execute("DELETE FROM integrity_baseline")
            self.conn.executemany("INSERT INTO integrity_baseline(path, kind, sha256, content, updated_at) VALUES (?,?,?,?,?)", rows)
        self._db_snapshot()
        return len(rows)

    # ------------------------------------------------------------------ kiểm + khôi phục
    def _aside(self, rel: str, stamp: str) -> str | None:
        """Chuyển bản bị sửa/thêm vào trash/tamper/<stamp>/ (không xoá cứng)."""
        src = self.home / rel
        dst = self.home / "trash" / "tamper" / stamp / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(src):
                shutil.move(str(src), str(dst))
                return str(dst)
        except OSError:
            try:
                if os.path.isjunction(src):
                    os.rmdir(src)  # chỉ gỡ junction, không đụng thư mục đích
                elif _islink(src) or src.is_file():
                    src.unlink()
                elif src.is_dir():
                    shutil.rmtree(src)
            except OSError:
                log.exception("không dọn được %s", src)
        return None

    def _write(self, rel: str, content: bytes) -> None:
        p = self.home / rel
        # cha bị thay bằng symlink → dọn trước khi ghi (không ghi xuyên symlink)
        parts = Path(rel).parts
        for i in range(1, len(parts)):
            anc = self.home.joinpath(*parts[:i])
            if _islink(anc) or (anc.exists() and not anc.is_dir()):
                self._aside(Path(*parts[:i]).as_posix(), "parent-" + str(int(time.time())))
            anc.mkdir(exist_ok=True)
        tmp = p.with_name(f".{p.name}.integrity.tmp")
        tmp.write_bytes(content)
        os.replace(tmp, p)

    def check(self, *, restore: bool | None = None) -> dict[str, Any]:
        """So đĩa với bản chuẩn. Trả {policy: {...}, config: {...}, db: {...}}. Có lệch → event + (tuỳ cấu hình) khôi phục."""
        if restore is None:
            restore = bool(self.cfg("restore", True))
        base = self.baseline()
        if not base:
            self.rebaseline("first_baseline")
            self.last_check = {"ts": _now(), "first_baseline": True}
            return self.last_check
        result: dict[str, Any] = {"ts": _now()}
        stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        # --- policy/
        cur = self.scan_policy()
        bpol = {k: v for k, v in base.items() if k == POLICY or k.startswith(POLICY + "/")}
        changed = [k for k, v in bpol.items() if v["kind"] == "file" and (cur.get(k, {}).get("kind") != "file" or cur[k].get("sha") != v["sha"])]
        missing_dirs = [k for k, v in bpol.items() if v["kind"] == "dir" and cur.get(k, {}).get("kind") != "dir"]
        extra = [k for k, v in cur.items() if k not in bpol or v["kind"] not in ("file", "dir") or bpol[k]["kind"] != v["kind"]]
        if changed or missing_dirs or extra:
            restored, moved = [], []
            if restore:
                # thứ tự: dọn phần tử lạ (symlink/file thêm) từ nông tới sâu, rồi dựng lại thư mục, rồi ghi file chuẩn
                for k in sorted(set(extra), key=lambda x: x.count("/")):
                    if not os.path.lexists(self.home / k):
                        continue
                    if k in bpol and bpol[k]["kind"] == "dir" and cur.get(k, {}).get("kind") == "dir":
                        continue
                    if k in bpol and bpol[k]["kind"] == "file" and cur.get(k, {}).get("kind") == "file":
                        continue  # file chuẩn bị sửa nội dung — xử lý ở bước ghi
                    d = self._aside(k, stamp)
                    moved.append({"path": k, "kind": cur.get(k, {}).get("kind"), "saved_to": d})
                for k in sorted(missing_dirs, key=lambda x: x.count("/")):
                    try:
                        if os.path.lexists(self.home / k) and not (self.home / k).is_dir():
                            self._aside(k, stamp)
                        (self.home / k).mkdir(parents=True, exist_ok=True)
                    except OSError:
                        log.exception("không dựng lại được %s", k)
                for k in sorted(changed):
                    try:
                        if os.path.lexists(self.home / k):
                            self._aside_copy(k, stamp)
                        self._write(k, base[k]["content"] or b"")
                        restored.append(k)
                    except OSError:
                        log.exception("không khôi phục được %s", k)
            result["policy"] = {"changed": changed, "missing_dirs": missing_dirs, "extra": extra, "restored": restored, "moved": moved, "restore": restore}
            self._alert("policy", result["policy"])
        # --- config.yaml (chỉ nhánh an toàn)
        bc = base.get(CONFIG)
        raw = self._read_config()
        if bc is not None and raw is not None and (raw == b"__symlink__" or _sha(raw) != bc["sha"]):
            before, after = self._config_security(bc["content"]), self._config_security(None if raw == b"__symlink__" else raw)
            if raw == b"__symlink__" or before != after:
                keys = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
                info: dict[str, Any] = {"keys": keys, "restore": restore}
                if restore:
                    try:
                        self._aside_copy(CONFIG, stamp)
                        merged: dict[str, Any] = {}
                        if raw != b"__symlink__":
                            try:
                                merged = yaml.safe_load(raw.decode("utf-8")) or {}
                            except Exception:
                                merged = {}
                            if not isinstance(merged, dict):
                                merged = {}
                        else:
                            self._aside(CONFIG, stamp)
                        for k in SECURITY_KEYS:
                            if k in before:
                                merged[k] = copy.deepcopy(before[k])
                            else:
                                merged.pop(k, None)
                        self._write(CONFIG, yaml.safe_dump(merged, allow_unicode=True, sort_keys=False).encode("utf-8"))
                        info["restored"] = True
                    except OSError:
                        log.exception("không khôi phục được config.yaml")
                result["config"] = info
                self._alert("config", info)
            # nhận thay đổi khoá KHÔNG an toàn do người sửa tay
            self._adopt_config()
        # --- hub.db
        db = self._db_check()
        if db:
            result["db"] = db
            self._alert("hub.db", db)
        self.last_check = result
        return result

    def _aside_copy(self, rel: str, stamp: str) -> None:
        src = self.home / rel
        dst = self.home / "trash" / "tamper" / stamp / rel
        try:
            if _islink(src):
                self._aside(rel, stamp)
                return
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                shutil.copy2(src, dst)
        except OSError:  # pragma: no cover
            pass

    def _adopt_config(self) -> None:
        raw = self._read_config()
        if raw is None or raw == b"__symlink__":
            return
        self.conn.execute(
            "INSERT INTO integrity_baseline(path, kind, sha256, content, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, content=excluded.content, updated_at=excluded.updated_at",
            (CONFIG, "config", _sha(raw), raw, _now()),
        )

    # ------------------------------------------------------------------ hub.db
    def _db_snapshot(self) -> None:
        try:
            self._db_version = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
            self._db_ino = os.stat(self.hub.paths.db).st_ino
        except Exception:  # pragma: no cover
            pass

    def _db_check(self) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        try:
            v = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
            if self._db_version is not None and v != self._db_version:
                out["external_write"] = True
            self._db_version = v
            ino = os.stat(self.hub.paths.db).st_ino
            if self._db_ino is not None and ino != self._db_ino:
                out["file_replaced"] = True
            self._db_ino = ino
        except FileNotFoundError:
            out["file_missing"] = True
        except Exception:  # pragma: no cover
            return None
        return out or None

    # ------------------------------------------------------------------ cảnh báo
    def _alert(self, target: str, detail: dict[str, Any]) -> None:
        payload = {"target": target, **{k: v for k, v in detail.items() if k != "content"}}
        self.events.append(payload)
        try:
            self.hub.publish(make_event("guard.tamper_detected", None, payload, source="hub"))
        except Exception:  # pragma: no cover
            log.exception("không phát được guard.tamper_detected")
        try:
            from ..approvals import audit_record

            audit_record("guard.tamper_detected", agent_id=None, run_id=None, data=payload)
        except Exception:  # pragma: no cover
            pass
        log.warning("phát hiện thay đổi ngoài RPC ở %s: %s", target, payload)

    # ------------------------------------------------------------------ vòng nền
    def start(self) -> None:
        try:
            self.check()
        except Exception:  # pragma: no cover
            log.exception("kiểm toàn vẹn lúc khởi động lỗi")
        if not self.cfg("enabled", True):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.task is None or self.task.done():
            self.task = loop.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(float(self.cfg("tick_s", 2.0)))
            try:
                self.check()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:  # pragma: no cover
                log.exception("kiểm toàn vẹn lỗi")

    def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None


# ============================================================================ gắn vào hub
def _after(obj: Any, name: str, cb: Callable[[], None]) -> None:
    fn = getattr(obj, name, None)
    if fn is None or getattr(fn, "_integrity_wrapped", False):
        return

    @functools.wraps(fn)
    def wrapped(*a: Any, **kw: Any) -> Any:
        try:
            return fn(*a, **kw)
        finally:
            cb()

    wrapped._integrity_wrapped = True  # type: ignore[attr-defined]
    setattr(obj, name, wrapped)


def attach(hub: "Hub") -> IntegrityMonitor:
    """Gọi CUỐI `Hub.__init__`: tạo `hub.integrity`, bọc các writer hợp lệ (ghi xong → chụp lại bản chuẩn)."""
    mon = IntegrityMonitor(hub)
    hub.integrity = mon  # type: ignore[attr-defined]

    def rb() -> None:
        try:
            mon.rebaseline()
        except Exception:  # pragma: no cover
            log.exception("không chụp lại được bản chuẩn")

    guard = getattr(hub, "guard", None)
    if guard is not None:
        for n in ("add", "remove", "ensure", "repo_deny_add", "repo_deny_rm"):  # MGR-2: ghi repo_deny.yaml hợp lệ → chụp lại bản chuẩn
            _after(guard.policy_store, n, rb)
    learning = getattr(hub, "learning", None)
    if learning is not None and getattr(learning, "policy", None) is not None:
        _after(learning.policy, "set", rb)
    evals = getattr(hub, "evals", None)
    if evals is not None and getattr(evals, "golden", None) is not None:
        _after(evals.golden, "_mirror", rb)
    _after(hub.config, "set", rb)
    return mon


# ============================================================================ dọn runtime (H-12)
def cleanup_runtime_dirs(hub: "Hub", keep: set[str] | None = None) -> list[str]:
    """Xoá `agents/*/runtime/<run>` còn sót (mcp.json có secret đã resolve) khi daemon khởi động. Trả danh sách đã xoá."""
    keep = keep or set()
    removed: list[str] = []
    base = hub.paths.agents_dir
    if not base.is_dir():
        return removed
    for agent_dir in base.iterdir():
        rt = agent_dir / "runtime"
        if not rt.is_dir() or rt.is_symlink():
            continue
        for d in rt.iterdir():
            if d.name in keep:
                continue
            try:
                if d.is_dir() and not d.is_symlink():
                    shutil.rmtree(d)
                else:
                    d.unlink()
                removed.append(d.relative_to(hub.paths.home).as_posix())
            except OSError:  # pragma: no cover
                log.exception("không xoá được %s", d)
    return removed


# ============================================================================ RPC
def register_rpc(d: "Daemon") -> None:
    hub = d.hub

    def mon() -> IntegrityMonitor:
        m = getattr(hub, "integrity", None)
        if m is None:
            raise HubError("integrity monitor chưa gắn")
        return m

    def status(p: dict[str, Any]) -> dict[str, Any]:
        m = mon()
        return {"last_check": {k: v for k, v in m.last_check.items()}, "baseline": sorted(m.baseline()), "recent_events": m.events[-20:],
                "restore": bool(m.cfg("restore", True)), "tick_s": float(m.cfg("tick_s", 2.0))}

    def check(p: dict[str, Any]) -> dict[str, Any]:
        return mon().check()

    def accept(p: dict[str, Any]) -> dict[str, Any]:
        """CHỈ người: nhận trạng thái đĩa hiện tại làm bản chuẩn; hoặc cài `content` vào `path` (trong policy/ hoặc config.yaml)."""
        m = mon()
        rel = p.get("path")
        if rel:
            # khoá dạng POSIX trên mọi OS (os.path.normpath trên Windows đổi sang `\` → không bao giờ khớp `policy/`)
            rel = posixpath.normpath(str(rel).replace(chr(92), "/"))
            if not (rel == CONFIG or rel.startswith(POLICY + "/")) or ".." in rel.split("/") or ":" in rel:
                raise HubError("path phải nằm trong policy/ hoặc là config.yaml")
            if p.get("content") is None:
                raise HubError("thiếu 'content' cho path")
            m._write(rel, str(p["content"]).encode("utf-8"))
        n = m.rebaseline("human_accept")
        check_python_env(hub, adopt=True)  # R2-1(c): người xác nhận → nhận luôn trạng thái .pth/sitecustomize hiện tại
        from ..approvals import audit_record

        audit_record("guard.integrity_accept", agent_id=None, run_id=None, data={"path": rel, "entries": n})
        return {"ok": True, "entries": n, "path": rel}

    d.methods.update({"guard.integrity.status": status, "guard.integrity.check": check, "guard.integrity.accept": accept})


# ============================================================================ R2-1(c): môi trường Python của hub (.pth / sitecustomize)
def python_env_files() -> dict[str, str]:
    """{đường dẫn: sha256} của mọi `*.pth`, `sitecustomize.py`, `usercustomize.py` trong site-packages của interpreter chạy hub —
    những file Python CHẠY khi khởi động (ngoài sandbox, trước mọi kiểm tra)."""
    import site
    import sysconfig

    dirs: set[str] = set()
    for k in ("purelib", "platlib"):
        try:
            dirs.add(os.path.realpath(sysconfig.get_paths()[k]))
        except KeyError:  # pragma: no cover
            pass
    try:
        dirs |= {os.path.realpath(d) for d in site.getsitepackages()}
    except Exception:  # pragma: no cover
        pass
    try:
        dirs.add(os.path.realpath(site.getusersitepackages()))
    except Exception:  # pragma: no cover
        pass
    out: dict[str, str] = {}
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if n.endswith(".pth") or n in ("sitecustomize.py", "usercustomize.py"):
                f = os.path.join(d, n)
                try:
                    out[f] = _sha(Path(f).read_bytes())
                except OSError:
                    continue
    return out


def check_python_env(hub: "Hub", *, adopt: bool = False) -> dict[str, Any] | None:
    """So .pth/sitecustomize với bản chuẩn (`hub_state.pyenv_baseline`). Lần đầu / `adopt` → nhận. Lệch → event đỏ
    `guard.env_tamper` + audit (+ Slack/Telegram qua listener). KHÔNG tự xoá (có thể là cài gói hợp lệ của người)."""
    cur = python_env_files()
    base = hub.store.state_get("pyenv_baseline")
    if base is None or adopt:
        hub.store.state_set("pyenv_baseline", cur)
        return None
    added = sorted(set(cur) - set(base))
    changed = sorted(k for k in set(cur) & set(base) if cur[k] != base[k])
    removed = sorted(set(base) - set(cur))
    if not (added or changed):
        if removed:
            hub.store.state_set("pyenv_baseline", cur)
        return None
    info = {"added": added, "changed": changed, "removed": removed,
            "message": "File chạy-khi-khởi-động (.pth/sitecustomize) trong môi trường Python của hub bị thêm/sửa — kiểm tra; nếu do bạn cài gói: "
                       "`cfharness guard integrity accept`"}
    try:
        hub.publish(make_event("guard.env_tamper", None, info, source="hub"))
        from ..approvals import audit_record

        audit_record("guard.env_tamper", agent_id=None, run_id=None, data=info)
    except Exception:  # pragma: no cover
        log.exception("không phát được guard.env_tamper")
    log.warning("môi trường Python của hub bị thay đổi: %s", info)
    return info
