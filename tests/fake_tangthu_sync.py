#!/usr/bin/env python3
"""Binary sync GIẢ cho test — mô phỏng `tang-thu-sync-headless` theo ĐÚNG hợp đồng §8.3 của docs/dev-notes/plan-cloud-files.md.

Không gọi mạng. "Cloud" là thư mục `<profile>/cloud/<rootId>/`; thay đổi từ cloud được bơm qua `<profile>/inject.jsonl`
(mỗi dòng JSON {op: create|update|delete|conflict, root_id, path, content?}) → áp vào bản sao cục bộ + mục `recent` chiều `down`.
Thay đổi cục bộ (agent sửa) → quét định kỳ → mục `recent` chiều `up`.

Biến môi trường điều khiển (chỉ test):
- `FAKE_TT_VERSION`        phiên bản in ở `--version` (mặc định 0.3.0)
- `FAKE_TT_LOGIN_ERROR`    `login` thất bại với dòng `ERROR <mã>` (exit 2)
- `FAKE_TT_LOGIN_HOLD_S`   `login` chờ N giây sau `LOGIN_URL` (mô phỏng người đang bấm đồng ý)
- `FAKE_TT_EXIT_AFTER_S`   tiến trình chính tự thoát mã 3 sau N giây (kiểm backoff)
- `FAKE_TT_STATE`          ép `state` trong status (vd auth_expired)
Mọi lần chạy ghi `<profile>/argv.jsonl` (argv + tên biến môi trường) để test kiểm cờ và env tối thiểu.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = os.environ.get("FAKE_TT_VERSION", "0.3.0")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _args(argv: list[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            k = a[2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                out[k] = argv[i + 1]
                i += 2
                continue
            out[k] = True
        i += 1
    return out


def _fid(root: str, rel: str, kind: str) -> str:
    return kind[0] + hashlib.sha256(f"{root}:{rel}".encode()).hexdigest()[:12]


def login(a: dict[str, object]) -> int:
    url = f"{a.get('server')}/api/sync/oauth/authorize?root_mode=single&root_hint={a.get('root-hint') or ''}&device={a.get('device') or ''}"
    print(f"LOGIN_URL {url}", flush=True)
    hold = float(os.environ.get("FAKE_TT_LOGIN_HOLD_S") or 0)
    if hold:
        time.sleep(hold)
    err = os.environ.get("FAKE_TT_LOGIN_ERROR")
    if err:
        print(f"ERROR {err}", flush=True)
        return 2
    tok = Path(str(a["tokens"]))
    tok.parent.mkdir(parents=True, exist_ok=True)
    tmp = tok.with_name(tok.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"access_token": "fake-access-token-xyz", "refresh_token": "fake-refresh-token-xyz"}, f)
    os.replace(tmp, tok)
    name = str(a.get("root-hint") or "cfharness")
    print(f"ROOT r_{hashlib.sha256(name.encode()).hexdigest()[:10]}\tws_1\t{name}", flush=True)
    return 0


class Main:
    def __init__(self, a: dict[str, object]) -> None:
        self.a = a
        self.profile = Path(str(a["profile"]))
        self.status_path = Path(str(a["status"]))
        self.control = Path(str(a["control"])) if a.get("control") else None
        self.max_delete = int(str(a.get("max-delete-batch") or 0))
        self.stop = False
        self.recent: list[dict[str, object]] = []
        self.blocked: dict[str, str] = {}
        self.resumed: set[str] = set()
        self.conflicts: dict[str, list[dict[str, str]]] = {}
        self.control_mtime: float | None = None
        self.inject_pos = 0
        self.roots = self._load_roots()
        self.snap: dict[str, dict[str, int]] = {r["id"]: self._scan(r) for r in self.roots}

    # -- roots
    def _load_roots(self) -> list[dict[str, str]]:
        p = self.profile / "roots.json"
        roots = json.loads(p.read_text()) if p.exists() else []
        if self.a.get("add-root"):
            rid = str(self.a["add-root"])
            if not any(r["id"] == rid for r in roots):
                local = Path(str(self.a["local-parent"])) / str(self.a.get("root-name") or rid)
                roots.append({"id": rid, "name": str(self.a.get("root-name") or rid), "ws_id": str(self.a.get("ws") or ""), "local_path": str(local)})
        for r in roots:
            Path(r["local_path"]).mkdir(parents=True, exist_ok=True)
        _atomic(p, roots)
        return roots

    def _scan(self, r: dict[str, str]) -> dict[str, int]:
        base = Path(r["local_path"])
        out: dict[str, int] = {}
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d != ".git"]
            for fn in fns:
                p = Path(dp) / fn
                try:
                    out[str(p.relative_to(base))] = p.stat().st_mtime_ns ^ p.stat().st_size
                except OSError:
                    continue
        return out

    def _index(self, r: dict[str, str]) -> None:
        items: dict[str, dict[str, str]] = {}
        root_folder = "f" + r["id"]
        for rel in self.snap.get(r["id"], {}):
            parent = os.path.dirname(rel)
            pid = _fid(r["id"], parent, "folder") if parent else root_folder
            items[rel] = {"file_id": _fid(r["id"], rel, "file"), "parent_folder_id": pid}
            while parent:
                gp = os.path.dirname(parent)
                items.setdefault(parent, {"folder_id": _fid(r["id"], parent, "folder"),
                                          "parent_folder_id": _fid(r["id"], gp, "folder") if gp else root_folder})
                parent = gp
        _atomic(self.profile / "index" / f"{r['id']}.json", {"root_folder_id": root_folder, "items": items})

    def _push(self, root: str, d: str, op: str, rel: str) -> None:
        parent = os.path.dirname(rel)
        self.recent.append({"ts": _now(), "root_id": root, "dir": d, "op": op, "path": rel, "file_id": _fid(root, rel, "file"),
                            "folder_id": _fid(root, parent, "folder") if parent else "f" + root})
        self.recent = self.recent[-200:]

    # -- vòng
    def _control(self) -> None:
        if self.control is None or not self.control.exists():
            return
        m = self.control.stat().st_mtime
        if m == self.control_mtime:
            return
        self.control_mtime = m
        try:
            c = json.loads(self.control.read_text())
        except ValueError:
            return
        for rid in c.get("resume_push") or []:
            if self.blocked.pop(rid, None):
                self.resumed.add(rid)
        with (self.profile / "control_seen.jsonl").open("a") as f:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    def _inject(self) -> None:
        p = self.profile / "inject.jsonl"
        if not p.exists():
            return
        lines = p.read_text().splitlines()
        for line in lines[self.inject_pos:]:
            try:
                j = json.loads(line)
            except ValueError:
                continue
            r = next((x for x in self.roots if x["id"] == j.get("root_id")), None)
            if r is None:
                continue
            fp = Path(r["local_path"]) / j["path"]
            op = j.get("op")
            if op == "conflict":
                self.conflicts.setdefault(r["id"], []).append({"path": j["path"], "created_at": _now()})
                continue
            if op == "delete":
                try:
                    fp.unlink()
                except OSError:
                    pass
            else:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(str(j.get("content") or ""), encoding="utf-8")
            self._push(r["id"], "down", op, j["path"])
            self.snap[r["id"]] = self._scan(r)
        self.inject_pos = len(lines)

    def _local(self) -> None:
        for r in self.roots:
            now = self._scan(r)
            before = self.snap.get(r["id"], {})
            deleted = [x for x in before if x not in now]
            if self.max_delete and len(deleted) > self.max_delete and r["id"] not in self.resumed:
                self.blocked[r["id"]] = "mass_delete"
            if r["id"] in self.blocked:
                continue
            for rel in sorted(set(now) - set(before)):
                self._push(r["id"], "up", "create", rel)
            for rel in sorted(x for x in now if x in before and now[x] != before[x]):
                self._push(r["id"], "up", "update", rel)
            for rel in deleted:
                self._push(r["id"], "up", "delete", rel)
            self.snap[r["id"]] = now
            self.resumed.discard(r["id"])
            self._index(r)

    def _status(self, state: str) -> None:
        _atomic(self.status_path, {
            "version": VERSION, "pid": os.getpid(), "state": os.environ.get("FAKE_TT_STATE") or state, "updated_at": _now(),
            "roots": [{"id": r["id"], "name": r["name"], "ws_id": r["ws_id"], "local_path": r["local_path"], "state": "idle",
                       "pending_up": 0, "pending_down": 0, "blocked": self.blocked.get(r["id"]),
                       "conflicts": self.conflicts.get(r["id"], []), "last_error": None} for r in self.roots],
            "recent": self.recent[-200:],
        })

    def run(self) -> int:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        exit_after = float(os.environ.get("FAKE_TT_EXIT_AFTER_S") or 0)
        t0 = time.monotonic()
        poll = float(str(self.a.get("poll") or 0.2))
        for r in self.roots:
            self._index(r)
        while not self.stop:
            self._control()
            self._inject()
            self._local()
            self._status("idle")
            if exit_after and time.monotonic() - t0 > exit_after:
                return 3
            time.sleep(min(poll, 0.2))
        self._status("idle")
        with (self.profile / "stopped.txt").open("a") as f:
            f.write(_now() + "\n")
        return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print(f"tang-thu-sync-headless {VERSION}")
        return 0
    a = _args(argv[1:] if argv[:1] == ["login"] else argv)
    prof = a.get("profile")
    if prof:
        Path(str(prof)).mkdir(parents=True, exist_ok=True)
        with (Path(str(prof)) / "argv.jsonl").open("a") as f:
            f.write(json.dumps({"argv": argv, "env": sorted(os.environ)}) + "\n")
    if argv[:1] == ["login"]:
        return login(a)
    return Main(a).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
