"""Tiện ích test thư mục chung (filehub/): binary sync GIẢ (`fake_tangthu_sync.py`, đúng hợp đồng §8.3) + cấu hình sẵn một thư mục chung.

Không gọi mạng, không gọi claude. "Cloud" của binary giả = `<profile>/cloud`; bơm thay đổi từ cloud bằng `inject()`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable

FAKE = Path(__file__).with_name("fake_tangthu_sync.py")


def fake_bin(tmp: Path, env: dict[str, str] | None = None) -> Path:
    """Shim `#!/bin/sh` chạy binary giả bằng python hiện tại. `env` (FAKE_TT_*) cài cứng vào shim vì supervisor dùng env TỐI THIỂU."""
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / "tang-thu-sync-headless"
    exports = "".join(f"export {k}='{v}'\n" for k, v in (env or {}).items())
    p.write_text(f"#!/bin/sh\n{exports}exec {sys.executable} {FAKE} \"$@\"\n")
    p.chmod(0o755)
    return p


def configure(hub: Any, tmp: Path, *, name: str = "chung", root_id: str = "r_chung", env: dict[str, str] | None = None,
              local: Path | None = None) -> Path:
    """Bật filehub với một thư mục chung đã có root (như sau `filehub setup`). Trả đường dẫn bản sao cục bộ."""
    b = fake_bin(tmp / "bin", env)
    local = local or (tmp / "KhongGianSo" / name)
    local.mkdir(parents=True, exist_ok=True)
    hub.config.set("filehub.server", "https://kgs.example.test")
    hub.config.set("filehub.sync_binary", str(b))
    hub.config.set("filehub.tick_s", 0.05)
    hub.config.set("filehub.restart_backoff_s", [0.1, 0.4])
    hub.config.set("filehub.stop_grace_s", 3)
    hub.config.set("filehub.poll_s", 0.1)
    hub.config.set("filehub.folders", [{"name": name, "root_id": root_id, "ws_id": "ws_1", "local_path": str(local.resolve())}])
    hub.config.set("filehub.enabled", True)
    return local.resolve()


def inject(hub: Any, **entry: Any) -> None:
    p = hub.filehub.profile_dir / "inject.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def argv_log(hub: Any) -> list[dict[str, Any]]:
    p = hub.filehub.profile_dir / "argv.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


async def until(cond: Callable[[], Any], timeout: float = 10.0, step: float = 0.05) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        v = cond()
        if v:
            return v
        if loop.time() > deadline:
            raise AssertionError("hết thời gian chờ điều kiện")
        await asyncio.sleep(step)


def events(hub: Any, etype: str) -> list[Any]:
    return [e for e in hub.store.query_events(types=[etype])]
