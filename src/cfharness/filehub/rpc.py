"""RPC thư mục chung (filehub):

- đọc: `filehub.status {}` → trạng thái + thư mục + phiên đăng nhập; `filehub.list {}` → `[{name, workspace, local_path, root_id}]`;
  `filehub.link {path, run_id?}` → `{folder, rel, url}` (link "Mở trên Không gian số").
- ghi (HUMAN_ONLY, tự audit — `auth.all_mutating`): `filehub.setup {server, binary, name?, scope?, local_parent?, local_path?, no_browser?,
  relay?, remote?}` → cấu hình + chạy `<binary> login` → `{login_url}`; `filehub.login {root_hint?, no_browser?, relay?, mode?}` (đăng nhập
  lại — giữ chế độ đang có); `filehub.login_callback {code, state, error?}` (relay OAuth — web gọi từ route công khai `/filehub/oauth/cb`);
  `filehub.switch_to_workspace {local_path?, relay?, no_browser?}`; `filehub.add {root_id, name?, ws_id?, local_path?, kind?}`;
  `filehub.remove {name}`; `filehub.rename {name, new_name}` (đổi tên cfharness — tên cũ thành bí danh); `filehub.pause {}`; `filehub.resume {root_id?}` (root_id = cho chiều đẩy lên đang bị chặn đi tiếp).
- cổng cfharness (chế độ lane, filehub/lane/): `filehub.lane_status {}` (đọc, không khoá) · `filehub.lane_setup {server, url, key}` (khoá
  chỉ người nhập — CLI getpass / web bước 2; gỡ khỏi params trước audit) · `filehub.lane_test {server}` · `filehub.lane_disable {server, force?}`.
- `relay: true` (web mở qua `web.public_origin`): binary đăng nhập với redirect về `<public_origin>/filehub/oauth/cb` thay cho listener loopback.
Điều phối viên KHÔNG có method ghi nào ở đây (không thuộc MANAGER_ALLOWED); đọc qua tool hub-self `filehub_list|filehub_link`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_FILEHUB = ("filehub.setup", "filehub.login", "filehub.login_callback", "filehub.switch_to_workspace", "filehub.add", "filehub.remove",
                    "filehub.rename", "filehub.pause", "filehub.resume",
                    # cổng cfharness (filehub/lane/): khoá hub chỉ người nhập (CLI getpass / web bước 2), gỡ khỏi params trước audit
                    "filehub.lane_setup", "filehub.lane_disable", "filehub.lane_orphan")
# Cổng cfharness — chỉ người (HUMAN_ONLY), KHÔNG ∈ MANAGER_ALLOWED. `lane_test` không ghi nhưng dùng khoá → chỉ người.
HUMAN_ONLY_LANE = ("filehub.lane_setup", "filehub.lane_disable", "filehub.lane_test", "filehub.lane_orphan")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) in (None, ""):
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


def register_rpc(daemon: "Daemon") -> None:
    from ..mcp_server.rpc import wrap_with_audit

    s = daemon.hub.filehub  # type: ignore[attr-defined]

    async def _setup(p: dict[str, Any]) -> dict[str, Any]:
        cfg = s.setup(p)
        res = await s.login(root_hint=cfg["name"] or None, no_browser=bool(p.get("no_browser")), auto_add=True, local_path=p.get("local_path"),
                            mode=cfg["scope"], relay=bool(p.get("relay")))
        return {**cfg, **res}

    async def _login(p: dict[str, Any]) -> dict[str, Any]:
        mode = p.get("mode")
        if mode is not None and mode not in ("folder", "workspace"):
            raise HubError("mode phải là folder|workspace", code=-32602)
        return await s.login(root_hint=p.get("root_hint"), no_browser=bool(p.get("no_browser")), auto_add=not s.folders(),
                             local_path=p.get("local_path"), mode=mode, relay=bool(p.get("relay")))

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {
        "filehub.status": lambda p: s.status(),
        "filehub.list": lambda p: s.list(),
        "filehub.link": lambda p: s.link(p),
        "filehub.setup": _setup,
        "filehub.login": _login,
        "filehub.login_callback": s.login_callback,  # gỡ code/state khỏi p TRƯỚC khi audit ghi
        "filehub.switch_to_workspace": s.switch_to_workspace,
        "filehub.add": lambda p: s.add(str(_req(p, "root_id")), p.get("name"), ws_id=p.get("ws_id"), local_path=p.get("local_path"), kind=p.get("kind")),
        "filehub.remove": lambda p: s.remove(str(_req(p, "name"))),
        "filehub.rename": lambda p: s.rename(str(_req(p, "name")), str(_req(p, "new_name"))),
        "filehub.pause": lambda p: s.pause(),
        "filehub.resume": lambda p: s.resume(p.get("root_id")),
        # cổng cfharness (chế độ lane)
        "filehub.lane_status": lambda p: s.lane.status(),
        "filehub.lane_setup": s.lane.setup,  # khoá bị pop khỏi p NGAY đầu hàm (audit không thấy)
        "filehub.lane_test": s.lane.test,
        "filehub.lane_disable": s.lane.disable,
        "filehub.lane_orphan": s.lane.orphan,  # {tree, action: push|drop} — cây mồ côi (thay đổi chưa đẩy được khi run kết thúc)
        # tool hub-self `fs_*` của run có workspace thư mục chung chế độ lane — daemon ràng run_id với tiến trình gọi (như taskself.call)
        "fsself.call": s.lane.fsself_call,
    }
    for name in MUTATING_FILEHUB:
        methods[name] = wrap_with_audit(daemon.hub, name, methods[name])
    daemon.methods.update(methods)
