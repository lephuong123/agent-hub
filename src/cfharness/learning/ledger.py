"""Ledger + snapshot + rollback (FR-LN-06) — HÀM ÁP DỤNG DUY NHẤT cho mọi thay đổi memory/skill/cấu hình.

- Blob content-addressed: `HUB_HOME/ledger/blobs/<sha256>` (file: nguyên byte; cây skill: tar tất định; cấu hình:
  JSON `{config, soul}`). `HUB_HOME/ledger`, `snapshots` thuộc tài nguyên bảo vệ mặc định (guard M1) — agent không xoá được.
- TRƯỚC mỗi thay đổi: chụp snapshot đích (bảng `snapshots`, trỏ blob) → áp dụng → `ledger_entries` (append-only,
  trigger chặn DELETE và sửa trường lõi; chỉ `rolled_back_by`/`eval_*`/`flags` được cập nhật).
- Write-ahead (S8-B): snapshot + `ledger_pending` commit TRƯỚC khi ghi đích; entry (+ hàng graph) + xoá pending trong MỘT transaction;
  `recover_pending` (Hub.start) hoàn tất / bỏ / gắn cờ pending sót khi daemon chết giữa chừng (docs/dev-notes/M4-A.md).
- Rollback từng entry tạo entry MỚI (`op=rollback`, `rollback_of`); rollback của rollback cũng được. Có entry sau cùng
  đích chưa rollback → `Conflict` + gợi ý chuỗi (`chain=True` rollback ngược thời gian). Đích bị sửa ngoài ledger → Conflict
  (trừ `force`). Rollback KHÔNG bị giới hạn tần suất (FR-LN-18).
- Memory tầng agent (MEMORY.md/USER.md — kho theo MỤC): rollback ĐẢO NGƯỢC đúng các mục entry đã thêm/sửa/xoá trên nội dung
  hiện tại (`invert_memory`), không khôi phục cả file → không cần chuỗi, không gỡ fact thêm sau, mục đã bị sửa tiếp thì bỏ qua.
- Graph memory (`kind="graph"`, docs/dev-notes/plan-graph-memory.md): blob before/after = trạng thái JSON của đúng các HÀNG entry chạm
  (`GraphStore.rows_state`); rollback THEO HÀNG (`GraphStore.invert_rows`): hàng entry tạo bị gỡ (`retracted_by`, không xoá cứng), hàng
  entry đóng hiệu lực được mở lại, hàng đã bị đổi sau entry thì bỏ qua — không chuỗi, như memory theo mục. Đích = `graph://<scope>/<owner>`.
- Xoá skill = chuyển `.archive/` (xoá mềm, FR-SG-06); rollback khôi phục đúng nội dung từ blob.
- Cấu hình (hardening H-04): rollback CHỈ khôi phục các khoá entry đã đổi (`target_ref` ∪ khoá khác nhau before/after; SOUL chỉ
  khi entry đổi SOUL), không ghi đè khoá khác. "Cùng đích" của cấu hình = trùng ít nhất một khoá. Khoá của entry bị sửa sau
  entry (người `agent edit`…) → `Conflict` như file/cây (người dùng `force` được).
- Bất biến "tự rollback không chạm thay đổi của người" (§4 bất biến 4, quyết định coordinator): bên gọi TỰ ĐỘNG
  (`AUTO_ROLLBACK_CALLERS`: regression_monitor/eval_gate/canary) KHÔNG BAO GIỜ rollback entry có actor ngoài
  `AUTO_CHANGE_ACTORS` (vd `user`, kể cả nằm trong chuỗi) và không ghi đè đích đã bị sửa ngoài ledger — `Conflict`
  (`data.human_change=True`), không ghi gì, phát `regression.detected{human_change}` (→ digest + Slack). `force` vô hiệu với actor tự động.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import tarfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError, NotFound
from ..events import make_event
from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger(__name__)

CONFIG_FIELDS = ("model", "effort", "fallback_model", "tool_policy", "mcp_servers", "limits",
                 # M7: điều phối viên sửa cả các trường mô tả/workspace qua ledger (rollback được); entry cũ không có khoá → không đổi
                 "name", "role", "description", "workspace_default")
# Bên gọi rollback TỰ ĐỘNG (không phải người).
AUTO_ROLLBACK_CALLERS = frozenset({"regression_monitor", "eval_gate", "canary"})
# Actor của entry mà cơ chế tự động được phép rollback (= learning_ops.ports.AUTO_ROLLBACK_ACTORS).
AUTO_CHANGE_ACTORS = frozenset({"learning", "review", "agent", "curator"})
# M7: thay đổi do điều phối viên (actor `manager:<agent>`) là thay đổi TỰ ĐỘNG (không phải của người) → được tự rollback.
MANAGER_ACTOR_PREFIX = "manager:"


def is_auto_change_actor(actor: Any) -> bool:
    a = str(actor or "")
    return a in AUTO_CHANGE_ACTORS or a.startswith(MANAGER_ACTOR_PREFIX)


def config_keys(e: dict[str, Any], before: dict[str, Any] | None = None, after: dict[str, Any] | None = None) -> tuple[list[str], bool]:
    """(khoá cấu hình, có SOUL) mà entry cấu hình đã đổi: khoá trong `target_ref` ∪ khoá khác nhau giữa blob before/after."""
    refs = {k.strip() for k in str(e.get("target_ref") or "").split(",") if k.strip()}
    keys = {k for k in CONFIG_FIELDS if k in refs}
    soul = "soul" in refs
    if before is not None and after is not None:
        bc, ac = before.get("config") or {}, after.get("config") or {}
        keys |= {k for k in CONFIG_FIELDS if bc.get(k) != ac.get(k)}
        soul = soul or (before.get("soul") or "") != (after.get("soul") or "")
    return [k for k in CONFIG_FIELDS if k in keys], soul


# ============================================================================ blob + tar tất định
class Blobs:
    def __init__(self, base: Path) -> None:
        self.base = base

    def put(self, data: bytes | None) -> str | None:
        if data is None:
            return None
        sha = hashlib.sha256(data).hexdigest()
        p = self.base / sha
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)
        return sha

    def get(self, sha: str | None) -> bytes | None:
        if not sha:
            return None
        p = self.base / sha
        if not p.exists():
            raise HubError(f"thiếu blob {sha[:12]} trong ledger/blobs")
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:  # blob hỏng/bị sửa → không bao giờ khôi phục nội dung sai một cách im lặng
            raise HubError(f"blob {sha[:12]} trong ledger/blobs không khớp sha256 (hỏng hoặc bị sửa) — không dùng được")
        return data


def tar_dir(path: Path) -> bytes | None:
    if not path.is_dir():
        return None
    return tar_from_files({str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if not p.is_symlink() and p.is_file()})


def tar_from_files(files: dict[str, bytes]) -> bytes:
    """Tar tất định từ {rel: nội dung} — CÙNG byte với `tar_dir` của cây có đúng các file đó (dùng để biết trước hash after)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for rel in sorted(files, key=Path):
            data = files[rel]
            ti = tarfile.TarInfo(rel)
            ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = len(data), 0, 0o644, 0, 0, "", ""
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def tar_files(data: bytes | None) -> dict[str, bytes]:
    if data is None:
        return {}
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tf:
        for m in tf.getmembers():
            if not m.isfile() or m.name.startswith("/") or ".." in Path(m.name).parts:
                continue
            f = tf.extractfile(m)
            if f is not None:
                out[m.name] = f.read()
    return out


def write_tree(path: Path, files: dict[str, bytes]) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


# ============================================================================ thay đổi
@dataclass
class Change:
    """Mô tả một thay đổi; `Ledger.apply` là nơi DUY NHẤT thực hiện nó."""

    scope: str
    owner_id: str | None
    target: str  # memory | skill | config | user_profile
    target_ref: str
    op: str  # add | patch | merge | archive | delete | rollback
    actor: str
    kind: str = "file"  # file | tree | config | graph
    path: Path | None = None
    after: bytes | None = None  # file: nội dung mới; tree: dùng `files`
    files: dict[str, bytes | None] = field(default_factory=dict)  # tree: rel → nội dung (None = bỏ file)
    replace_tree: bool = False  # tree: thay toàn bộ cây bằng `files`
    archive: bool = False  # tree: chuyển thư mục vào .archive (xoá mềm)
    config_changes: dict[str, Any] | None = None
    soul: str | None = None
    agent_id: str | None = None
    proposal_id: str | None = None
    risk_class: str | None = None
    reason: str | None = None
    summary: str | None = None
    flags: list[str] = field(default_factory=list)
    created_by: str = "agent"  # provenance cho skill mới
    rollback_of: str | None = None
    graph_rows: list[dict[str, Any]] = field(default_factory=list)  # graph: [{table, id, row}] — hàng SAU (GraphStore.plan_ops)
    graph_key: str | None = None  # graph: `graph://<scope>/<owner>` (đường dẫn tổng hợp cho canary/ghim)


def change_key(ch: Any) -> str:
    """Khoá đích của một Change cho canary (một đích một canary) và ghim: đường dẫn file/cây, hoặc `graph://…` cho graph."""
    gk = getattr(ch, "graph_key", None)
    if gk:
        return str(gk)
    p = getattr(ch, "path", None)
    return str(p) if p else ""


def is_entry_graph(e: dict[str, Any]) -> bool:
    return e.get("blob_kind") == "graph"


def is_entry_memory(e: dict[str, Any]) -> bool:
    """Entry memory (mọi tầng; USER dùng chung là target `user_profile` — vẫn cả file) → rollback theo mục: gỡ một fact không kéo các
    fact ghi sau. Tầng team/global theo mục từ 2026-10-30, để watch gỡ đúng mục tool `memory` của agent đã ghi."""
    return e.get("blob_kind") == "file" and e.get("target") == "memory"


def invert_memory(before: list[str], after: list[str], current: list[str]) -> list[str]:
    """Đảo ngược thay đổi before→after trên `current`: mục entry thêm thì gỡ, mục entry sửa thì trả bản cũ đúng chỗ, mục entry
    xoá thì thêm lại (sau mục đứng trước nó, không còn thì đầu file). Mục entry thêm/sửa đã bị thay đổi sau entry đè lên → bỏ qua
    (không đụng thay đổi sau)."""
    import difflib

    swap: dict[str, list[str]] = {}
    restore_after: dict[str | None, list[str]] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old, new = before[i1:i2], after[j1:j2]
        if new:
            swap[new[0]] = old
            for x in new[1:]:
                swap[x] = []
        else:
            restore_after.setdefault(after[j1 - 1] if j1 > 0 else None, []).extend(old)
    have = set(current)
    out = [x for x in restore_after.pop(None, []) if x not in have]
    for c in current:
        out.extend((x for x in swap.pop(c) if x not in have) if c in swap else [c])
        out.extend(x for x in restore_after.pop(c, []) if x not in have)
    out.extend(x for v in restore_after.values() for x in v if x not in have)  # mục đứng trước đã mất → thêm cuối
    return list(dict.fromkeys(out))


class Ledger:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.blobs = Blobs(hub.paths.home / "ledger" / "blobs")
        (hub.paths.home / "snapshots").mkdir(parents=True, exist_ok=True)

    @property
    def conn(self):
        return self.hub.store.conn

    # ------------------------------------------------------------------ đọc trạng thái hiện tại
    def _config_state(self, agent_id: str) -> dict[str, Any]:
        row = self.hub.agents.get_row(agent_id)
        cfg = self.hub.agents.config_of(row).model_dump()
        soul_p = Path(row["home_path"]) / "SOUL.md"
        return {"config": {k: cfg.get(k) for k in CONFIG_FIELDS}, "soul": soul_p.read_text(encoding="utf-8") if soul_p.exists() else ""}

    def graph(self):
        from ..knowledge.graph import GraphStore

        return GraphStore(self.conn)

    def state_bytes(self, ch: Change) -> bytes | None:
        """Trạng thái đích hiện tại (blob) của một Change — mọi kind, kể cả graph."""
        if ch.kind == "graph":
            return self.graph().dump_state(self.graph().rows_state(f"{m['table']}:{m['id']}" for m in ch.graph_rows))
        return self._current_bytes(ch.kind, ch.path, ch.agent_id)

    def _current_bytes(self, ch_kind: str, path: Path | None, agent_id: str | None) -> bytes | None:
        if ch_kind == "config":
            return json.dumps(self._config_state(agent_id or ""), sort_keys=True, ensure_ascii=False).encode("utf-8")
        if path is None:
            return None
        if ch_kind == "tree":
            return tar_dir(path)
        return path.read_bytes() if path.exists() else None

    # ------------------------------------------------------------------ áp dụng (duy nhất) — write-ahead (S8-B)
    def apply(self, ch: Change) -> dict[str, Any]:
        """Áp dụng một Change, an toàn khi daemon chết giữa chừng (write-ahead):
        1. ghi ý định: snapshot + hàng `ledger_pending` (blob before đã lưu, hash after dự kiến) — commit;
        2. ghi đích (file: tmp + rename; cây; cấu hình qua `agents.update`; graph: không làm gì ở bước này);
        3. MỘT transaction: (graph: ghi hàng graph) + đăng ký skill + INSERT `ledger_entries` + xoá hàng pending.
        Chết ở bất kỳ đâu → `recover_pending` lúc Hub khởi động hoàn tất / bỏ / gắn cờ hàng pending còn sót."""
        eid = "le_" + uuid.uuid4().hex[:12]
        before = self.state_bytes(ch)
        before_sha = self.blobs.put(before)
        snap_id = "sn_" + uuid.uuid4().hex[:12]
        after_known, after_sha = self._expected_after(ch, before)
        row = {
            "id": eid, "proposal_id": ch.proposal_id, "scope": ch.scope, "owner_id": ch.owner_id, "agent_id": ch.agent_id,
            "target": ch.target, "target_ref": ch.target_ref, "op": ch.op, "risk_class": ch.risk_class, "snapshot_id": snap_id,
            "before_blob": before_sha, "blob_kind": ch.kind, "actor": ch.actor, "reason": ch.reason,
            "summary": (ch.summary or "")[:2000], "flags": ch.flags or [], "rollback_of": ch.rollback_of,
        }
        with self._atomic():  # bước 1: ý định
            self.conn.execute("INSERT INTO snapshots (id, kind, scope, owner_id, target, target_ref, blob, created_at) VALUES (?,?,?,?,?,?,?,?)",
                              (snap_id, ch.kind, ch.scope, ch.owner_id, ch.target, ch.target_ref, before_sha, now_iso()))
            self.hub.store._insert("ledger_pending", {
                "id": eid, "kind": ch.kind, "path": str(ch.path) if ch.path else None, "agent_id": ch.agent_id, "snapshot_id": snap_id,
                "before_blob": before_sha, "after_known": 1 if after_known else 0, "after_sha": after_sha, "entry": row,
                "change": {"archive": ch.archive, "created_by": ch.created_by,
                           "graph_keys": [f"{m['table']}:{m['id']}" for m in ch.graph_rows] if ch.kind == "graph" else []},
                "state": "pending", "created_at": now_iso()})
        try:
            revision, archived_to = self._write_target(ch)  # bước 2
            self._commit(ch, row, archived_to=archived_to, revision=revision)  # bước 3
        except Exception:
            self._abandon_if_untouched(eid)  # lỗi thường (không phải chết): đích chưa đổi → bỏ ý định ngay
            raise
        return self.get(eid)

    def _expected_after(self, ch: Change, before: bytes | None) -> tuple[bool, str | None]:
        """(biết trước?, sha256 trạng thái SAU dự kiến). Graph ghi trong cùng transaction với entry → không cần đoán (False)."""
        def sha(b: bytes | None) -> str | None:
            return hashlib.sha256(b).hexdigest() if b is not None else None

        if ch.kind == "file":
            return True, sha(ch.after)
        if ch.kind == "tree":
            if ch.archive:
                return True, None
            files = {} if ch.replace_tree else tar_files(before)
            for rel, data in ch.files.items():
                if data is None:
                    files.pop(rel, None)
                else:
                    files[rel] = data
            return True, sha(tar_from_files(files))
        if ch.kind == "config":
            st = json.loads((before or b"{}").decode("utf-8"))
            cfg = dict(st.get("config") or {})
            cfg.update({k: v for k, v in (ch.config_changes or {}).items() if k in CONFIG_FIELDS})
            st["config"] = cfg
            if ch.soul is not None:
                st["soul"] = ch.soul
            return True, sha(json.dumps(st, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        return False, None

    def _write_target(self, ch: Change) -> tuple[int | None, str | None]:
        """Bước 2: ghi đích → (config_revision, archived_to). Graph không ghi ở đây (ghi trong transaction của entry)."""
        ks = self.hub.knowledge
        revision = archived_to = None
        if ch.kind == "file":
            assert ch.path is not None
            ch.path.parent.mkdir(parents=True, exist_ok=True)
            if ch.after is None:
                if ch.path.exists():
                    ch.path.unlink()
            else:
                tmp = ch.path.with_name(ch.path.name + ".tmp")
                tmp.write_bytes(ch.after)
                tmp.replace(ch.path)
        elif ch.kind == "tree":
            assert ch.path is not None
            if ch.archive:
                if ch.path.exists():
                    from ..knowledge.store import move_to_archive

                    archived_to = str(move_to_archive(ch.path, ks.archive_dir(ch.scope, ch.owner_id)))
            elif ch.replace_tree:
                write_tree(ch.path, {k: v for k, v in ch.files.items() if v is not None})
            else:
                ch.path.mkdir(parents=True, exist_ok=True)
                for rel, data in ch.files.items():
                    p = ch.path / rel
                    if data is None:
                        if p.exists():
                            p.unlink()
                    else:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(data)
        elif ch.kind == "config":
            rev_actor = "rollback" if ch.op == "rollback" else (ch.actor if str(ch.actor).startswith(MANAGER_ACTOR_PREFIX) else "learning")
            res = self.hub.agents.update(ch.agent_id or "", dict(ch.config_changes or {}), soul=ch.soul, actor=rev_actor)
            revision = res.get("config_revision")
        elif ch.kind != "graph":
            raise HubError(f"kind thay đổi không hợp lệ: {ch.kind}")
        return revision, archived_to

    def _commit(self, ch: Change, row: dict[str, Any], *, archived_to: str | None, revision: int | None, recovered: bool = False) -> None:
        """Bước 3: MỘT transaction — hàng graph (nếu có) + đăng ký skill + entry + đánh dấu rollback_of + xoá pending."""
        eid = row["id"]
        with self._atomic():
            if ch.kind == "graph" and not recovered:
                self.graph().write_rows(ch.graph_rows, eid)
            if ch.kind == "tree":
                if ch.archive:
                    self.hub.knowledge.mark_archived(ch.scope, ch.owner_id, ch.target_ref, archived_to)
                else:
                    self._register_tree(ch)
            after = self.state_bytes(ch) if not ch.archive else None
            entry = {**row, "after_blob": self.blobs.put(after), "applied_at": now_iso(), "config_revision": revision,
                     "summary": (row.get("summary") or "") + (f" → {archived_to}" if archived_to else "")}
            if recovered:
                entry["flags"] = list(entry.get("flags") or []) + ["recovered"]
            self.hub.store._insert("ledger_entries", entry)
            if row.get("rollback_of"):
                self.conn.execute("UPDATE ledger_entries SET rolled_back_by=? WHERE id=?", (eid, row["rollback_of"]))
            self.conn.execute("DELETE FROM ledger_pending WHERE id=?", (eid,))

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        """Transaction riêng; nếu bên gọi đã mở transaction thì dùng SAVEPOINT (lồng được)."""
        if not self.conn.in_transaction:
            with self.hub.store.tx():
                yield
            return
        sp = "ledger_" + uuid.uuid4().hex[:8]
        self.conn.execute(f"SAVEPOINT {sp}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {sp}")
            self.conn.execute(f"RELEASE {sp}")
            raise
        self.conn.execute(f"RELEASE {sp}")

    # ------------------------------------------------------------------ khôi phục sau crash (S8-B)
    def pending(self, state: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM ledger_pending", ()
        if state:
            sql, args = sql + " WHERE state=?", (state,)
        return [self.hub.store._row("ledger_pending", r) for r in self.conn.execute(sql + " ORDER BY created_at, rowid", args)]  # type: ignore[misc]

    def _pending_change(self, p: dict[str, Any]) -> Change:
        e, c = p["entry"], p["change"] or {}
        ch = Change(scope=e["scope"], owner_id=e.get("owner_id"), target=e["target"], target_ref=e.get("target_ref") or "", op=e["op"],
                    actor=e["actor"], kind=p["kind"], path=Path(p["path"]) if p.get("path") else None, agent_id=e.get("agent_id"),
                    archive=bool(c.get("archive")), created_by=str(c.get("created_by") or "agent"), rollback_of=e.get("rollback_of"))
        ch.graph_rows = [{"table": k.split(":", 1)[0], "id": k.split(":", 1)[1]} for k in c.get("graph_keys") or []]
        return ch

    def _abandon_if_untouched(self, pid: str) -> None:
        try:
            p = next((x for x in self.pending() if x["id"] == pid), None)
            if p is not None:
                cur = self.state_bytes(self._pending_change(p))
                if (hashlib.sha256(cur).hexdigest() if cur is not None else None) == p.get("before_blob"):
                    self.conn.execute("DELETE FROM ledger_pending WHERE id=?", (pid,))
        except Exception:  # pragma: no cover — để recover_pending lúc khởi động xử lý
            log.exception("không kiểm được đích của ledger_pending %s", pid)

    def recover_pending(self) -> dict[str, list[str]]:
        """Đối chiếu từng hàng `ledger_pending` còn sót (daemon chết giữa `apply`) — gọi trong `Hub.start`:
        đích == after dự kiến → hoàn tất entry (cờ `recovered`); == before → bỏ ý định (chưa ghi gì); khác → `state='flagged'`,
        GIỮ snapshot before và phát `learning.ledger_recovery{flagged}` để người tự khôi phục. Không bao giờ tự ghi đè đích."""
        out: dict[str, list[str]] = {"completed": [], "discarded": [], "flagged": []}
        for p in self.pending("pending"):
            try:
                ch = self._pending_change(p)
                cur = self.state_bytes(ch)
                cur_sha = hashlib.sha256(cur).hexdigest() if cur is not None else None
                if p.get("after_known") and cur_sha == p.get("after_sha"):
                    archived_to = None
                    if ch.archive and ch.path is not None:
                        cands = sorted(self.hub.knowledge.archive_dir(ch.scope, ch.owner_id).glob(f"{ch.path.name}__*"))
                        archived_to = str(cands[-1]) if cands else None
                    revision = None
                    if ch.kind == "config":
                        revision = self.hub.agents.get_row(ch.agent_id or "")["config_revision"]
                    self._commit(ch, p["entry"], archived_to=archived_to, revision=revision, recovered=True)
                    outcome = "completed"
                elif cur_sha == p.get("before_blob"):
                    self.conn.execute("DELETE FROM ledger_pending WHERE id=?", (p["id"],))
                    outcome = "discarded"
                else:
                    detail = f"đích hiện tại ({(cur_sha or 'vắng')[:12]}) không khớp trước/sau thay đổi — bản TRƯỚC giữ ở snapshot {p.get('snapshot_id')}"
                    self.conn.execute("UPDATE ledger_pending SET state='flagged', detail=?, flagged_at=? WHERE id=?", (detail, now_iso(), p["id"]))
                    outcome = "flagged"
            except Exception as e:  # noqa: BLE001 — một hàng lỗi không chặn các hàng khác — gắn cờ để người xem
                detail = f"không khôi phục được: {e}"
                self.conn.execute("UPDATE ledger_pending SET state='flagged', detail=?, flagged_at=? WHERE id=?", (detail[:500], now_iso(), p["id"]))
                outcome = "flagged"
            out[outcome].append(p["id"])
            e = p["entry"]
            payload = {"pending_id": p["id"], "outcome": outcome, "kind": p["kind"], "scope": e["scope"], "target": e["target"],
                       "target_ref": e.get("target_ref"), "path": p.get("path"), "snapshot_id": p.get("snapshot_id"), "before_blob": p.get("before_blob")}
            if outcome == "flagged":
                payload.update(warning=True, hint=f"khôi phục tay từ snapshot {p.get('snapshot_id')} (HUB_HOME/ledger/blobs/{p.get('before_blob') or '—'}; "
                                                  "before_blob rỗng = đích chưa tồn tại trước thay đổi)")
            self.hub.publish(make_event("learning.ledger_recovery", e.get("agent_id"), payload))
        return out

    def _register_tree(self, ch: Change) -> None:
        from ..knowledge.store import SKILL_FILE, parse_skill

        assert ch.path is not None
        f = ch.path / SKILL_FILE
        desc = ""
        if f.exists():
            fm, _ = parse_skill(f.read_text(encoding="utf-8", errors="replace"))
            desc = str(fm.get("description") or "")
        okey = ch.owner_id if ch.scope != "global" else "global"
        existing = self.conn.execute("SELECT created_by FROM skills WHERE scope=? AND owner_id=? AND name=?", (ch.scope, okey, ch.target_ref)).fetchone()
        self.hub.knowledge.register_skill(ch.scope, ch.owner_id, ch.target_ref, created_by=existing[0] if existing else ch.created_by, description=desc)

    # ------------------------------------------------------------------ đọc ledger
    def get(self, entry_id: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM ledger_entries WHERE id=?", (entry_id,)).fetchone()
        if r is None:
            rows = self.conn.execute("SELECT * FROM ledger_entries WHERE id LIKE ? LIMIT 2", (entry_id + "%",)).fetchall()
            if len(rows) != 1:
                raise NotFound(f"không tìm thấy ledger entry '{entry_id}'")
            r = rows[0]
        return self.hub.store._row("ledger_entries", r)  # type: ignore[return-value]

    def list(self, agent_id: str | None = None, *, target: str | None = None, target_ref: str | None = None, actor: str | None = None,
             since: str | None = None, limit: int = 100, scope: str | None = None, rolled_back: bool | None = None, **_: Any) -> list[dict[str, Any]]:
        where, args = [], []
        for col, v in (("agent_id", agent_id), ("target", target), ("target_ref", target_ref), ("actor", actor), ("scope", scope)):
            if v is not None:
                where.append(f"{col}=?")
                args.append(v)
        if since:
            where.append("applied_at>=?")
            args.append(since)
        if rolled_back is not None:
            where.append("rolled_back_by IS " + ("NOT NULL" if rolled_back else "NULL"))
        sql = "SELECT * FROM ledger_entries" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY applied_at DESC, rowid DESC LIMIT ?"
        args.append(int(limit))
        return [self.hub.store._row("ledger_entries", r) for r in self.conn.execute(sql, args)]  # type: ignore[misc]

    def blob_text(self, sha: str | None) -> str | None:
        b = self.blobs.get(sha)
        return None if b is None else b.decode("utf-8", errors="replace")

    def show(self, entry_id: str) -> dict[str, Any]:
        e = self.get(entry_id)
        e["diff"] = self.entry_diff(e)
        return e

    def entry_diff(self, e: dict[str, Any], max_chars: int = 20_000) -> str:
        import difflib

        b, a = self.blobs.get(e.get("before_blob")), self.blobs.get(e.get("after_blob"))
        if e.get("blob_kind") == "tree":
            bf, af = tar_files(b), tar_files(a)
            out = []
            for rel in sorted(set(bf) | set(af)):
                x, y = bf.get(rel, b"").decode("utf-8", "replace"), af.get(rel, b"").decode("utf-8", "replace")
                if x != y:
                    out += difflib.unified_diff(x.splitlines(True), y.splitlines(True), f"a/{e['target_ref']}/{rel}", f"b/{e['target_ref']}/{rel}")
            return "".join(out)[:max_chars]
        x = (b or b"").decode("utf-8", "replace")
        y = (a or b"").decode("utf-8", "replace")
        return "".join(difflib.unified_diff(x.splitlines(True), y.splitlines(True), f"a/{e['target_ref']}", f"b/{e['target_ref']}"))[:max_chars]

    def _path_for(self, e: dict[str, Any]) -> Path | None:
        ks = self.hub.knowledge
        if e["blob_kind"] in ("config", "graph"):
            return None
        if e["blob_kind"] == "tree":
            return ks.skill_dir(e["scope"], e["owner_id"], e["target_ref"])
        if e["target"] == "user_profile":
            return ks.memory_path("global", None, "user")
        store = "user" if e["target_ref"] == "USER.md" else "memory"
        return ks.memory_path(e["scope"], e["owner_id"], store)

    def later_entries(self, e: dict[str, Any]) -> list[dict[str, Any]]:
        if is_entry_memory(e) or is_entry_graph(e):
            return []  # rollback theo mục: thay đổi sau trên cùng file độc lập
        if e.get("blob_kind") == "config":
            # cấu hình: đích = TẬP KHOÁ — entry sau đổi ít nhất một khoá trùng mới là "cùng đích" (H-04)
            ks, sl = config_keys(e)
            mine = set(ks) | ({"soul"} if sl else set())
            rows = self.conn.execute(
                "SELECT * FROM ledger_entries WHERE agent_id=? AND target='config' AND rowid > (SELECT rowid FROM ledger_entries WHERE id=?) "
                "AND rolled_back_by IS NULL AND op != 'rollback' ORDER BY rowid DESC", (e.get("agent_id"), e["id"])).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                x = self.hub.store._row("ledger_entries", r)
                xk, xs = config_keys(x)  # type: ignore[arg-type]
                if mine & (set(xk) | ({"soul"} if xs else set())):
                    out.append(x)  # type: ignore[arg-type]
            return out
        rows = self.conn.execute(
            "SELECT * FROM ledger_entries WHERE scope=? AND IFNULL(owner_id,'')=IFNULL(?,'') AND target=? AND IFNULL(target_ref,'')=IFNULL(?,'') "
            "AND rowid > (SELECT rowid FROM ledger_entries WHERE id=?) AND rolled_back_by IS NULL AND op != 'rollback' ORDER BY rowid DESC",
            (e["scope"], e["owner_id"], e["target"], e["target_ref"], e["id"]),
        ).fetchall()
        # rollback entry trỏ ngược về entry sau → coi như đã xử lý
        return [self.hub.store._row("ledger_entries", r) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------ rollback
    def rollback(self, entry_id: str, actor: str = "user", reason: str | None = None, *, chain: bool = False, force: bool = False) -> dict[str, Any]:
        e = self.get(entry_id)
        if e.get("rolled_back_by"):
            raise Conflict(f"entry {e['id']} đã được rollback bởi {e['rolled_back_by']}")
        later = self.later_entries(e)
        if later and not chain:
            raise Conflict(
                f"sau {e['id']} còn {len(later)} thay đổi cùng đích chưa rollback: {', '.join(x['id'] for x in later)} — "
                f"rollback chuỗi bằng `cfharness learn rollback {e['id']} --chain`",
                data={"chain": [x["id"] for x in later] + [e["id"]]},
            )
        if actor in AUTO_ROLLBACK_CALLERS:
            human = [x for x in [*later, e] if not is_auto_change_actor(x.get("actor"))]
            if human:
                h = human[0]
                self._human_conflict(e, actor, reason, change_actor=str(h.get("actor") or "?"), change_entry=h["id"],
                                     why=f"entry {h['id']} do NGƯỜI tạo (actor={h.get('actor')}) — cơ chế tự động không rollback thay đổi của người")
        done: list[dict[str, Any]] = []
        for x in later:  # mới → cũ
            done.append(self._rollback_one(x, actor, reason or f"chuỗi rollback tới {e['id']}", force=force))
        done.append(self._rollback_one(e, actor, reason, force=force))
        last = done[-1]
        last["chain"] = [d["rollback_of"] for d in done]
        return last

    def _human_conflict(self, e: dict[str, Any], actor: str, reason: str | None, *, change_actor: str, change_entry: str | None, why: str,
                        drifted: list[str] | None = None) -> None:
        """Bên gọi tự động gặp thay đổi của người → KHÔNG ghi gì; cảnh báo (digest + Slack) rồi `Conflict`."""
        hint = f"cfharness learn rollback {e['id']}" + (" --force" if drifted else "")
        msg = f"không tự rollback {e['id']} ({e['target']} {e['target_ref']}): {why}"
        self.hub.publish(make_event("regression.detected", e.get("agent_id"), {
            "ledger_entry_id": e["id"], "rolled_back": False, "human_change": True, "actor": change_actor, "change_entry_id": change_entry,
            "source": actor, "reason": (f"{reason} — " if reason else "") + msg, "drifted": drifted or [], "hint": hint,
            "target": e["target"], "target_ref": e["target_ref"], "scope": e["scope"]}))
        raise Conflict(msg, data={"human_change": True, "entry": e["id"], "change_entry": change_entry, "drifted": drifted or [], "hint": hint})

    def _rollback_one(self, e: dict[str, Any], actor: str, reason: str | None, *, force: bool) -> dict[str, Any]:
        path = self._path_for(e)
        kind = e["blob_kind"]
        auto = actor in AUTO_ROLLBACK_CALLERS
        before = self.blobs.get(e.get("before_blob"))
        ch = Change(scope=e["scope"], owner_id=e["owner_id"], target=e["target"], target_ref=e["target_ref"], op="rollback", actor=actor,
                    kind=kind, path=path, agent_id=e.get("agent_id"), proposal_id=e.get("proposal_id"), risk_class=e.get("risk_class"),
                    reason=reason, summary=f"rollback {e['id']} ({e['op']} {e['target']} {e['target_ref']})", rollback_of=e["id"])
        if kind == "config":
            # H-04: chỉ khôi phục khoá entry đã đổi; khoá đó bị sửa sau entry → Conflict (không bao giờ tự ghi đè người)
            st_before = json.loads((before or b"{}").decode("utf-8"))
            st_after = json.loads((self.blobs.get(e.get("after_blob")) or b"{}").decode("utf-8"))
            cur = self._config_state(e.get("agent_id") or "")
            keys, soul = config_keys(e, st_before, st_after)
            drifted = [k for k in keys if (cur.get("config") or {}).get(k) != (st_after.get("config") or {}).get(k)]
            if soul and (cur.get("soul") or "") != (st_after.get("soul") or ""):
                drifted.append("soul")
            if drifted and (auto or not force):
                if auto:
                    self._human_conflict(e, actor, reason, change_actor="user", change_entry=None, drifted=drifted,
                                         why=f"{', '.join(drifted)} đã bị sửa ngoài ledger sau entry (giá trị hiện tại ≠ lúc áp dụng)")
                raise Conflict(f"cấu hình {', '.join(drifted)} của '{e.get('agent_id')}' đã bị sửa ngoài ledger sau entry {e['id']} — "
                               f"xem `learn show {e['id']}`; dùng --force để vẫn khôi phục", data={"drifted": drifted})
            ch.config_changes = {k: (st_before.get("config") or {}).get(k) for k in keys}
            ch.soul = (st_before.get("soul") or "") if soul else None
        elif is_entry_graph(e):
            gs = self.graph()
            b_state, a_state = gs.load_state(before), gs.load_state(self.blobs.get(e.get("after_blob")))
            rows, skipped = gs.invert_rows(b_state, a_state, gs.rows_state(a_state.keys()))
            ch.graph_rows, ch.graph_key = rows, str(e.get("target_ref") or "")
            if skipped:
                ch.flags = ["partial"]
                ch.summary = f"{ch.summary} — bỏ qua {len(skipped)} hàng đã đổi sau entry"
        elif is_entry_memory(e):
            from ..knowledge.store import parse_memory, render_memory

            current = self._current_bytes(kind, path, e.get("agent_id"))
            after = self.blobs.get(e.get("after_blob"))
            store = "user" if e["target_ref"] == "USER.md" else "memory"
            b_ent = parse_memory((before or b"").decode("utf-8", "replace"))
            a_ent = parse_memory((after or b"").decode("utf-8", "replace"))
            c_ent = parse_memory((current or b"").decode("utf-8", "replace"))
            ch.after = render_memory(invert_memory(b_ent, a_ent, c_ent), store).encode("utf-8")
            # mục entry thêm/sửa đã bị sửa tiếp sau entry → invert bỏ qua: báo partial thay vì im lặng "thành công"
            skipped = [x for x in a_ent if x not in set(b_ent) and x not in set(c_ent)]
            if skipped:
                ch.flags = ["partial"]
                ch.summary = f"{ch.summary} — bỏ qua {len(skipped)} mục đã bị sửa sau entry (không gỡ được)"
        else:
            current = self._current_bytes(kind, path, e.get("agent_id"))
            cur_sha = hashlib.sha256(current).hexdigest() if current is not None else None
            if cur_sha != e.get("after_blob") and (auto or not force):
                # skill đã archive (after=None) mà thư mục vẫn vắng → khớp
                if auto:
                    self._human_conflict(e, actor, reason, change_actor="user", change_entry=None, drifted=[str(e["target_ref"])],
                                         why=f"{e['target']} '{e['target_ref']}' đã bị sửa ngoài ledger sau entry")
                raise Conflict(f"{e['target']} '{e['target_ref']}' đã bị sửa ngoài ledger sau entry {e['id']} — xem `learn show {e['id']}`; dùng --force để vẫn khôi phục",
                               data={"current": cur_sha, "expected": e.get("after_blob")})
            if kind == "file":
                ch.after = before
            elif before is None:
                ch.archive = True  # skill do entry tạo ra → xoá mềm (vào .archive), không xoá cứng
            else:
                ch.files, ch.replace_tree = dict(tar_files(before)), True
        new = self.apply(ch)
        if e.get("proposal_id"):
            self.conn.execute("UPDATE proposals SET status='rolled_back', updated_at=? WHERE id=?", (now_iso(), e["proposal_id"]))
        self._wake_rate_deferred(e)
        ev = make_event("learning.rolled_back", e.get("agent_id"), {
            "ledger_entry_id": e["id"], "rollback_entry_id": new["id"], "actor": actor, "reason": reason, "target": e["target"],
            "target_ref": e["target_ref"], "scope": e["scope"], "proposal_id": e.get("proposal_id"),
        })
        self.hub.publish(ev)
        return new

    def _wake_rate_deferred(self, e: dict[str, Any]) -> None:
        """Rollback trả lại suất hạn mức (pipeline.rate_check bỏ entry đã rollback) → proposal đang hoãn vì hạn mức của cùng
        agent / tầng team-global được thử lại ở tick kế tiếp thay vì chờ tới nửa đêm. Còn vượt hạn mức thì retry hoãn lại."""
        if e["scope"] in ("team", "global"):
            where, args = "scope IN ('team','global')", ()
        elif e.get("agent_id"):
            where, args = "agent_id=?", (e["agent_id"],)
        else:
            return
        self.conn.execute(f"UPDATE proposals SET not_before=? WHERE status='deferred_rate_limited' AND block_reason='rate_limited' AND {where}",
                          (now_iso(), *args))

    # ------------------------------------------------------------------ hợp đồng B (eval): dựng trạng thái TRƯỚC entry trên bản sao
    def materialize_before(self, entry: dict[str, Any], home: Path, cfg: dict[str, Any]) -> bool:
        e = self.get(entry["id"]) if entry.get("id") else entry
        if e["scope"] != "agent":
            return False
        before = self.blobs.get(e.get("before_blob"))
        if e["blob_kind"] == "config":
            st = json.loads((before or b"{}").decode("utf-8"))
            cfg.update(st.get("config") or {})
            (home / "SOUL.md").write_text(st.get("soul") or "", encoding="utf-8")
            return True
        if e["blob_kind"] == "graph":  # bản chụp graph trong bản sao home (evals/overlay.make_home)
            from ..knowledge.graph import SNAPSHOT_FILE, GraphStore

            p = home / SNAPSHOT_FILE
            if not p.exists():
                return False
            snap = json.loads(p.read_text(encoding="utf-8"))
            GraphStore.revert_on_snapshot(snap, GraphStore.load_state(before), GraphStore.load_state(self.blobs.get(e.get("after_blob"))))
            p.write_bytes(GraphStore.dump_snapshot(snap))
            return True
        if e["blob_kind"] == "tree":
            d = home / "skills" / e["target_ref"]
            if before is None:
                if d.exists():
                    shutil.rmtree(d)
            else:
                write_tree(d, tar_files(before))
            return True
        p = home / (e["target_ref"] if e["target_ref"] in ("MEMORY.md", "USER.md") else "MEMORY.md")
        if before is None:
            if p.exists():
                p.unlink()
        else:
            p.write_bytes(before)
        return True
