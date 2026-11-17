"""`fsself.call {run_id, op, args}` — tool hub-self `fs_*` của run có workspace thư mục chung chế độ `lane` (plan-filehub-lane.md §3, §4.1).

Ràng buộc (kiểm ở DAEMON, không tin danh sách tool):
- run phải đang `queued|running`; người gọi qua socket phải là CHÍNH run đó (cây tiến trình/env — như `taskself.call`);
- chỉ trong gốc + phạm vi của workspace run (`<sub>`), đường dẫn chuẩn hoá theo C.4 PHÍA HUB trước khi gửi (`..`, `.tangthu`, ký tự điều
  khiển, đường dẫn tuyệt đối ngoài cây → từ chối);
- run chỉ đọc (`run_opts.read_only` / agent chỉ đọc) → chỉ thao tác đọc;
- không có tool xoá hẳn, đổi ACL, mở/đặt khoá tệp.
Kết quả là DỮ LIỆU từ Không gian số (tên/đường dẫn/nội dung tệp) — đã làm sạch một dòng cho tên; nội dung văn bản trả nguyên (đã che secret).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ...errors import Blocked, HubError, NotFound
from ...redact import redact_str
from ...termsafe import one_line
from .client import LaneError, norm_rel

if TYPE_CHECKING:
    from .service import LaneService
    from .workdir import Workdir

READ_OPS = ("fs_status", "fs_list", "fs_stat", "fs_read", "fs_versions", "fs_link", "fs_pull")
WRITE_OPS = ("fs_write", "fs_mkdir", "fs_move", "fs_delete", "fs_restore")
ITEM_KEYS = ("path", "type", "size", "mtime", "version_no", "version_id", "rev", "locked")


def _item(wd: "Workdir", it: dict[str, Any]) -> dict[str, Any]:
    out = {k: it.get(k) for k in ITEM_KEYS if it.get(k) is not None}
    rel = wd.rel_of(str(it.get("path") or "")) if wd.sub else str(it.get("path") or "")
    out["path"] = one_line(rel or "", 1024)
    return out


def rel_arg(wd: "Workdir", v: Any, *, allow_root: bool = True) -> str:
    """Tham số đường dẫn của agent → rel trong phạm vi. Nhận đường dẫn tương đối (theo cwd = gốc cây) hoặc tuyệt đối TRONG cây."""
    s = str(v or "").strip()
    if s.startswith("/"):
        root = str(wd.path).rstrip("/")
        rs = os.path.normpath(s)
        if rs == root:
            s = ""
        elif rs.startswith(root + "/"):
            s = rs[len(root) + 1:]
        else:
            raise HubError(f"đường dẫn ngoài cây làm việc của run: {one_line(s, 200)!r}")
    if s in (".", "./"):
        s = ""
    if s.startswith("./"):
        s = s[2:]
    try:
        return norm_rel(s, allow_root=allow_root)
    except LaneError as e:
        raise HubError(e.message) from None


async def call(svc: "LaneService", wd: "Workdir", op: str, args: dict[str, Any], *, read_only: bool) -> Any:
    if op not in READ_OPS + WRITE_OPS:
        raise NotFound(f"không có tool '{op}'")
    if op in WRITE_OPS and (read_only or wd.read_only):
        raise Blocked(f"{op}: run chỉ đọc — không ghi được vào thư mục chung")
    if wd.revoked:
        raise Blocked("quyền với thư mục chung này đã bị thu hồi trên Không gian số")
    c = svc.client(wd.server)
    if op == "fs_status":
        return {**wd.info(), "cwd": str(wd.path), "scope": f"{wd.folder}/{wd.sub}" if wd.sub else wd.folder,
                "pending_paths": sorted(wd.pending)[:50], "blocked_paths": sorted(wd.blocked)[:50], "lazy_paths": wd.lazy[:50],
                "failed_paths": {k: v for k, v in list(wd.failed.items())[:50]}}
    if op == "fs_list":
        rel = rel_arg(wd, args.get("path"))
        lim = max(1, min(2000, int(args.get("limit") or 500)))
        res = await c.list(wd.root, wd.remote(rel), recursive=bool(args.get("recursive")), limit=lim)
        return {"items": [_item(wd, i) for i in res.get("items") or [] if isinstance(i, dict)][:lim],
                "truncated": bool(res.get("next_page"))}
    if op == "fs_stat":
        rel = rel_arg(wd, args.get("path"))
        it = await c.stat(wd.root, wd.remote(rel))
        wd.read_paths.add(rel)
        return {**_item(wd, it), "local": str(wd.path / rel) if (wd.path / rel).exists() and rel else None}
    if op == "fs_read":
        rel = rel_arg(wd, args.get("path"), allow_root=False)
        lim = int(svc.cfg("read_max_bytes") or 262144)
        try:
            data, hdr = await c.read(wd.root, wd.remote(rel), version=args.get("version") or None, max_bytes=lim)
        except LaneError as e:
            if e.lane_code == "too_large":
                raise HubError(f"tệp lớn hơn {lim} byte — dùng fs_pull rồi đọc bằng Read") from None
            raise
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HubError("tệp nhị phân — dùng fs_pull rồi mở bằng công cụ phù hợp") from None
        wd.read_paths.add(rel)
        return {"path": rel, "version_id": (hdr.get("etag") or "").strip('"') or None, "text": redact_str(text)}
    if op == "fs_versions":
        rel = rel_arg(wd, args.get("path"), allow_root=False)
        vs = await c.versions(wd.root, wd.remote(rel))
        return {"path": rel, "versions": [{k: v.get(k) for k in ("version_id", "no", "size", "at", "by_kind")} for v in vs[:50] if isinstance(v, dict)]}
    if op == "fs_link":
        rel = rel_arg(wd, args.get("path"))
        it = await c.stat(wd.root, wd.remote(rel)) if rel else {}
        srv = svc.fh.server()
        if not srv:
            return {"path": rel, "url": None}
        if not rel:
            return {"path": "", "url": f"{srv}/files" if wd.kind == "workspace" and not wd.sub else None}
        if it.get("type") == "file":
            return {"path": rel, "url": f"{srv}/files/{it.get('parent_id')}?file={it.get('id')}"}
        return {"path": rel, "url": f"{srv}/files/{it.get('id')}"}
    if op == "fs_pull":
        rel = rel_arg(wd, args.get("path"), allow_root=False)
        return await svc.pull(wd, rel)
    return await svc.write_op(wd, op, args)


__all__ = ["call", "READ_OPS", "WRITE_OPS", "rel_arg"]
