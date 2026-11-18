"""RPC thư mục chung (filehub) — cổng cfharness là chế độ duy nhất (đồng bộ 2 chiều cũ đã bỏ ở H5):

- đọc: `filehub.status {}` → trạng thái, thư mục qua cổng, thư mục đồng bộ cũ cần chuyển (`legacy`); `filehub.list {}`;
  `filehub.link {path, run_id?}` → `{folder, rel, url}` (link "Mở trên Không gian số" — id do cổng trả).
- ghi (HUMAN_ONLY, tự audit — `auth.all_mutating`): `filehub.remove {name}`; `filehub.rename {name, new_name}` (đổi tên cfharness — tên cũ
  thành bí danh); `filehub.resume {root_id, discard?}` (lô xoá hàng loạt đang giữ: đẩy tiếp / không xoá).
- cổng cfharness (filehub/lane/): `filehub.lane_status {}` (đọc, không khoá) · `filehub.lane_setup {server?, url?, key}` (khoá chỉ người
  nhập — CLI getpass / web bước 2; gỡ khỏi params trước audit) · `filehub.lane_test {server}` · `filehub.lane_disable {server}` ·
  `filehub.lane_add {root_id, name?, server?}` · `filehub.lane_orphan {tree, action: push|drop}` — mọi ghi là HUMAN_ONLY.
Điều phối viên KHÔNG có method ghi nào ở đây (không thuộc MANAGER_ALLOWED); đọc qua tool hub-self `filehub_list|filehub_link`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_FILEHUB = ("filehub.remove", "filehub.rename", "filehub.resume",
                    # cổng cfharness (filehub/lane/): khoá hub chỉ người nhập (CLI getpass / web bước 2), gỡ khỏi params trước audit
                    "filehub.lane_setup", "filehub.lane_disable", "filehub.lane_orphan", "filehub.lane_add",
                    "filehub.lane_test")  # lane_test: hello + đối soát → có thể THÊM / CHUYỂN thư mục chung (gốc mới được cấp)
# Cổng cfharness — chỉ người (HUMAN_ONLY), KHÔNG ∈ MANAGER_ALLOWED. `lane_test` không ghi nhưng dùng khoá → chỉ người.
HUMAN_ONLY_LANE = ("filehub.lane_setup", "filehub.lane_disable", "filehub.lane_test", "filehub.lane_orphan", "filehub.lane_add")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) in (None, ""):
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


def register_rpc(daemon: "Daemon") -> None:
    from ..mcp_server.rpc import wrap_with_audit

    s = daemon.hub.filehub  # type: ignore[attr-defined]

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {
        "filehub.status": lambda p: s.status(),
        "filehub.list": lambda p: s.list(),
        "filehub.link": s.link,
        "filehub.remove": lambda p: s.remove(str(_req(p, "name"))),
        "filehub.rename": lambda p: s.rename(str(_req(p, "name")), str(_req(p, "new_name"))),
        "filehub.resume": lambda p: s.resume(p.get("root_id"), discard=bool(p.get("discard"))),  # {root_id, discard?} = người quyết lô xoá hàng loạt
        # cổng cfharness
        "filehub.lane_status": lambda p: s.lane.status(),
        "filehub.lane_setup": s.lane.setup,  # khoá bị pop khỏi p NGAY đầu hàm (audit không thấy)
        "filehub.lane_test": s.lane.test,
        "filehub.lane_disable": s.lane.disable,
        "filehub.lane_orphan": s.lane.orphan,  # {tree, action: push|drop} — cây mồ côi (thay đổi chưa đẩy được khi run kết thúc)
        "filehub.lane_add": s.lane.add_folder,  # {root_id, name?, server?} — thư mục mới qua cổng (không bản sao)
        # tool hub-self `fs_*` của run có workspace thư mục chung — daemon ràng run_id với tiến trình gọi (như taskself.call)
        "fsself.call": s.lane.fsself_call,
    }
    for name in MUTATING_FILEHUB:
        methods[name] = wrap_with_audit(daemon.hub, name, methods[name])
    daemon.methods.update(methods)
