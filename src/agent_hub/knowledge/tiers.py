"""Phiên bản tri thức tầng team/global theo agent (FR-LN-11/16, FR-RT-10 — M5-A).

Kho tầng team/global chỉ có MỘT bản "sống" trên đĩa (`teams/<id>/memory.md|skills/`, `global/USER.md|MEMORY.md|skills/`),
ghi duy nhất qua `Ledger.apply`. Trong lúc một thay đổi đang CANARY (bảng `tier_canaries`, status `canary`, mode `canary`),
bản sống = bản mới, nhưng mọi agent KHÁC agent canary được "ghim" về phiên bản TRƯỚC (blob `before_blob` của ledger):
- memory: đọc nội dung blob thay vì file sống;
- skill: cây skill trước thay đổi được giải nén một lần (content-addressed) vào `HUB_HOME/knowledge/versions/<sha>/`
  và chỉ mục dùng mô tả/đường dẫn của bản đó; skill mới tạo bởi canary bị ẩn; skill bị archive bởi canary vẫn hiện.
→ Prompt của agent không canary giữ NGUYÊN từng byte (hash giống → giữ prompt cache, FR-OB-04). Promote = bỏ ghim (bản
sống có hiệu lực cho mọi agent từ session mới); rollback = `Ledger.rollback` khôi phục bản sống, bỏ ghim — agent khác
chưa bao giờ thấy thay đổi.

Eval (M4-B chạy golden task trên BẢN SAO agent home): ứng viên tầng team/global được dựng vào `<home>/.tier_overlay/<đường
dẫn tương đối HUB_HOME>` (file, cây skill, hoặc dấu `<rel>.removed`) — chỉ bản sao có thư mục này, home thật không bao giờ có.

Shadow (USER profile): áp dụng cho mọi agent (không ghim), chỉ theo dõi — không ảnh hưởng ở đây.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .store import SKILL_FILE, parse_memory, parse_skill

if TYPE_CHECKING:
    from .store import KnowledgeStore

OVERLAY_DIR = ".tier_overlay"
REMOVED_SUFFIX = ".removed"


class TierVersions:
    """`hub.knowledge.versions`: chọn phiên bản tầng team/global mà một agent nhìn thấy."""

    def __init__(self, ks: "KnowledgeStore") -> None:
        self.ks = ks

    @property
    def hub(self):
        return self.ks.hub

    # ------------------------------------------------------------------ canary đang ghim
    def active_pins(self) -> list[dict[str, Any]]:
        try:
            rows = self.hub.store.conn.execute(
                "SELECT id, scope, owner_id, target, target_ref, blob_kind, path, before_blob, after_blob, canary_agent_id "
                "FROM tier_canaries WHERE status='canary' AND mode='canary'").fetchall()
        except Exception:  # bảng chưa có (DB cũ trong test thuần)
            return []
        return [dict(r) for r in rows]

    def pins_for(self, agent_id: str | None) -> dict[str, dict[str, Any]]:
        """{đường dẫn tuyệt đối đích: canary} mà agent bị ghim về bản TRƯỚC (agent ≠ agent canary)."""
        return {p["path"]: p for p in self.active_pins() if p["canary_agent_id"] != agent_id and p.get("path")}

    def blob_text(self, sha: str | None) -> str | None:
        if not sha:
            return None
        b = self.hub.learning.ledger.blobs.get(sha)
        return None if b is None else b.decode("utf-8", errors="replace")

    def version_dir(self, sha: str) -> Path:
        """Cây skill của blob `sha` giải nén một lần (chỉ đọc, dùng cho skill_view của agent bị ghim)."""
        from ..learning.ledger import tar_files

        d = self.hub.paths.home / "knowledge" / "versions" / sha
        ok = d.with_name(sha + ".ok")
        if not ok.exists() or not d.is_dir():
            tmp = d.with_name(sha + ".tmp")
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True)
            for rel, data in tar_files(self.hub.learning.ledger.blobs.get(sha)).items():
                p = tmp / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            if d.exists():
                shutil.rmtree(d)
            tmp.replace(d)
            ok.write_text("", encoding="utf-8")
        return d

    # ------------------------------------------------------------------ overlay eval
    def rel(self, path: Path) -> Path | None:
        try:
            return Path(path).relative_to(self.hub.paths.home)
        except ValueError:
            return None

    @staticmethod
    def overlay_path(home: Path | None, rel: Path | None) -> Path | None:
        if home is None or rel is None:
            return None
        root = Path(home) / OVERLAY_DIR
        return root / rel if root.is_dir() else None

    # ------------------------------------------------------------------ memory
    def read_memory(self, agent_id: str | None, scope: str, owner: str | None, store: str = "memory", home: Path | None = None) -> list[str]:
        path = self.ks.memory_path(scope, owner, store)
        ov = self.overlay_path(home, self.rel(path))
        if ov is not None:
            if Path(str(ov) + REMOVED_SUFFIX).exists():
                return []
            if ov.exists():
                return parse_memory(ov.read_text(encoding="utf-8"))
        pin = self.pins_for(agent_id).get(str(path))
        if pin is not None:
            return parse_memory(self.blob_text(pin["before_blob"]) or "")
        return parse_memory(path.read_text(encoding="utf-8")) if path.exists() else []

    # ------------------------------------------------------------------ skill
    def tier_skills(self, agent_id: str | None, scope: str, owner: str | None, home: Path | None = None) -> list[dict[str, Any]]:
        rows = {s["name"]: s for s in self.ks.list_skills(scope, owner)}
        sdir = self.ks.skills_dir(scope, owner)
        for pin in self.pins_for(agent_id).values():
            if pin["blob_kind"] != "tree" or Path(pin["path"]).parent != sdir:
                continue
            name = pin["target_ref"]
            if not pin["before_blob"]:
                rows.pop(name, None)  # skill do canary tạo → agent bị ghim chưa thấy
                continue
            d = self.version_dir(pin["before_blob"])
            rows[name] = _row_from_dir(name, d, base=rows.get(name))
        ov = self.overlay_path(home, self.rel(sdir))
        if ov is not None:
            if ov.is_dir():
                for sd in ov.iterdir():
                    if sd.is_dir() and (sd / SKILL_FILE).exists():
                        rows[sd.name] = _row_from_dir(sd.name, sd, base=rows.get(sd.name))
            if ov.parent.is_dir():
                for m in ov.glob("*" + REMOVED_SUFFIX):
                    rows.pop(m.name[: -len(REMOVED_SUFFIX)], None)
        return [rows[k] for k in sorted(rows)]

    # ------------------------------------------------------------------ xem phiên bản (CLI/RPC)
    def version_map(self, agent_id: str) -> list[dict[str, Any]]:
        """Phiên bản tri thức tầng team/global mà agent đang được gán (live | pinned_before | canary)."""
        import hashlib

        out: list[dict[str, Any]] = []
        pins = self.pins_for(agent_id)
        canary_mine = {p["path"]: p for p in self.active_pins() if p["canary_agent_id"] == agent_id}
        for scope, owner in self.ks.agent_tiers(agent_id):
            if scope == "agent":
                continue
            stores = ("memory",) if scope == "team" else ("user", "memory")
            items: list[tuple[str, str, Path, str]] = [(("user_profile" if st == "user" else "memory"), self.ks.memory_path(scope, owner, st).name,
                                                        self.ks.memory_path(scope, owner, st), "file") for st in stores]
            sdir = self.ks.skills_dir(scope, owner)
            names = {s["name"] for s in self.ks.list_skills(scope, owner)} | {p["target_ref"] for p in list(pins.values()) + list(canary_mine.values())
                                                                               if p["blob_kind"] == "tree" and Path(p["path"]).parent == sdir}
            items += [("skill", n, sdir / n, "tree") for n in sorted(names)]
            for target, ref, path, kind in items:
                key = str(path)
                if key in pins:
                    sha, src, cid = pins[key]["before_blob"], "pinned_before", pins[key]["id"]
                elif key in canary_mine:
                    sha, src, cid = canary_mine[key]["after_blob"], "canary", canary_mine[key]["id"]
                else:
                    src, cid = "live", None
                    if kind == "file":
                        sha = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                    else:
                        from ..learning.ledger import tar_dir

                        t = tar_dir(path)
                        sha = hashlib.sha256(t).hexdigest() if t is not None else None
                if sha is None and src == "live":
                    continue
                out.append({"scope": scope, "owner": owner or "global", "target": target, "ref": ref, "version": (sha or "absent")[:12],
                            "source": src, "canary_id": cid})
        return out

    # ------------------------------------------------------------------ tổng quan một tầng (bản sống) + hồ sơ USER
    def tier_overview(self, scope: str, owner: str | None) -> dict[str, Any]:
        from ..learning.canary import entry_hash
        from .store import memory_chars

        if scope == "team":
            self.hub.teams.row(owner or "")
        stores = ("memory",) if scope == "team" else ("user", "memory")
        mem = {}
        for st in stores:
            entries = self.ks.read_memory(scope, owner, st)
            okey = owner if scope == "team" else None
            mem[self.ks.memory_path(scope, owner, st).name] = {
                "entries": [{"text": e, "key": entry_hash(e), "pinned": self.ks.is_pinned(scope, okey, "memory_entry", entry_hash(e))} for e in entries],
                "chars": memory_chars(entries), "limit": self.ks.memory_limit(scope, st)}
        skills = [{k: x.get(k) for k in ("name", "description", "created_by", "pinned", "state", "use_count", "last_used_at")} for x in self.ks.list_skills(scope, owner)]
        canaries = [c for c in self.hub.learning.canary.list("canary") if c["scope"] == scope and (scope != "team" or c.get("owner_id") == owner)]
        members = [m["agent_id"] for m in self.hub.teams.members(owner)] if scope == "team" and owner else None
        return {"scope": scope, "owner": owner or "global", "memory": mem, "skills": skills, "members": members,
                "canaries": [{k: c.get(k) for k in ("id", "mode", "target", "target_ref", "canary_agent_id", "watch_agents", "started_at", "deadline", "progress")} for c in canaries]}

    def user_profile(self, agent_id: str | None = None) -> dict[str, Any]:
        """FR-LN-14: MỘT hồ sơ dùng chung (`global/USER.md`, đổi qua L3 + shadow) + quan sát riêng của từng agent (peer:
        `agents/<id>/USER.md`, L1/L2 của chính agent đó — không agent nào đọc quan sát của agent khác)."""
        from ..learning.canary import entry_hash

        shared = self.ks.read_memory("global", None, "user")
        agents = [agent_id] if agent_id else [a["id"] for a in self.hub.agents.list()]
        peers = {a: self.ks.read_memory("agent", a, "user") for a in agents}
        return {"shared": [{"text": e, "key": entry_hash(e), "pinned": self.ks.is_pinned("global", None, "memory_entry", entry_hash(e))} for e in shared],
                "shared_limit": self.ks.memory_limit("global", "user"),
                "peers": {a: v for a, v in peers.items() if v or agent_id}}


def _row_from_dir(name: str, d: Path, base: dict[str, Any] | None = None) -> dict[str, Any]:
    fm, _ = parse_skill((d / SKILL_FILE).read_text(encoding="utf-8", errors="replace")) if (d / SKILL_FILE).exists() else ({}, "")
    row = dict(base or {"created_by": None, "pinned": False, "use_count": 0, "last_used_at": None})
    row.update({"name": name, "description": str(fm.get("description") or ""), "path": str(d), "state": "active"})
    return row
