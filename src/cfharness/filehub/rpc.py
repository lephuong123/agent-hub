"""RPC thư mục chung (filehub):

- đọc: `filehub.status {}` → trạng thái + thư mục + phiên đăng nhập; `filehub.list {}` → `[{name, workspace, local_path, root_id}]`;
  `filehub.link {path, run_id?}` → `{folder, rel, url}` (link "Mở trên Không gian số").
- ghi (HUMAN_ONLY, tự audit — `auth.all_mutating`): `filehub.setup {server, binary, name?, local_parent?, local_path?, no_browser?}` → cấu hình
  + chạy `<binary> login` → `{login_url}`; `filehub.login {root_hint?, no_browser?}` (đăng nhập lại); `filehub.add {root_id, name?, ws_id?,
  local_path?}`; `filehub.remove {name}`; `filehub.pause {}`; `filehub.resume {root_id?}` (root_id = cho chiều đẩy lên đang bị chặn đi tiếp).
Điều phối viên KHÔNG có method ghi nào ở đây (không thuộc MANAGER_ALLOWED); đọc qua tool hub-self `filehub_list|filehub_link`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_FILEHUB = ("filehub.setup", "filehub.login", "filehub.add", "filehub.remove", "filehub.pause", "filehub.resume")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) in (None, ""):
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


def register_rpc(daemon: "Daemon") -> None:
    from ..mcp_server.rpc import wrap_with_audit

    s = daemon.hub.filehub  # type: ignore[attr-defined]

    async def _setup(p: dict[str, Any]) -> dict[str, Any]:
        cfg = s.setup(p)
        res = await s.login(root_hint=cfg["name"], no_browser=bool(p.get("no_browser")), auto_add=True, local_path=p.get("local_path"))
        return {**cfg, **res}

    async def _login(p: dict[str, Any]) -> dict[str, Any]:
        return await s.login(root_hint=p.get("root_hint"), no_browser=bool(p.get("no_browser")), auto_add=not s.folders(),
                             local_path=p.get("local_path"))

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {
        "filehub.status": lambda p: s.status(),
        "filehub.list": lambda p: s.list(),
        "filehub.link": lambda p: s.link(p),
        "filehub.setup": _setup,
        "filehub.login": _login,
        "filehub.add": lambda p: s.add(str(_req(p, "root_id")), p.get("name"), ws_id=p.get("ws_id"), local_path=p.get("local_path")),
        "filehub.remove": lambda p: s.remove(str(_req(p, "name"))),
        "filehub.pause": lambda p: s.pause(),
        "filehub.resume": lambda p: s.resume(p.get("root_id")),
    }
    for name in MUTATING_FILEHUB:
        methods[name] = wrap_with_audit(daemon.hub, name, methods[name])
    daemon.methods.update(methods)
