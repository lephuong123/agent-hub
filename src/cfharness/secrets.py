"""Secret store của hub (FR-MC-05, NFR-10).

Lựa chọn: **file 0600 trong `HUB_HOME/secrets/`** (thư mục 0700), mỗi secret một file `<NAME>`.
Lý do không dùng macOS Keychain qua lệnh `security` ở M1:
- `security add-generic-password -w <giá trị>` đưa secret vào argv → lộ qua `ps` trong lúc chạy
  (muốn tránh phải dùng chế độ nhắc tương tác, không hợp với daemon chạy nền).
- Keychain có thể bật hộp thoại xin quyền khi daemon (tiến trình khác) đọc → treo run lúc spawn.
- Không chạy trên Linux (NFR-27 thứ yếu) và khó test tự động.
BRD ghi Keychain là tuỳ chọn M2 — interface `SecretStore` giữ nguyên để thêm backend sau.

Quy tắc:
- Giá trị secret KHÔNG BAO GIỜ ghi vào DB, event, log, audit, export. Registry MCP chỉ giữ tham chiếu `${NAME}`.
- Tên secret: `[A-Z0-9_]`, ≤ 128 ký tự (tự chuyển chữ hoa; ký tự khác → `_`).
- `resolve(obj)` thay mọi `${NAME}` trong chuỗi/dict/list; thiếu secret → `SecretMissing` (fail-closed),
  trừ khi `secrets.env_fallback` bật và biến môi trường cùng tên tồn tại (tương thích cấu hình
  `${VAR}` sẵn có trong `~/.claude.json`).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .errors import HubError
from .redact import register_secret_values

NAME_RE = re.compile(r"^[A-Z0-9_]{1,128}$")
REF_RE = re.compile(r"\$\{([^}]+)\}")


class SecretMissing(HubError):
    """Tham chiếu `${NAME}` không có trong secret store."""


def normalize_name(name: str) -> str:
    n = re.sub(r"[^A-Z0-9_]", "_", name.strip().upper())
    n = re.sub(r"_+", "_", n).strip("_")
    if not n or not NAME_RE.match(n):
        raise HubError(f"tên secret không hợp lệ: {name!r}")
    return n


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(REF_RE.fullmatch(value.strip()))


def refs_in(obj: Any) -> set[str]:
    """Tên mọi `${NAME}` xuất hiện trong obj (đệ quy)."""
    out: set[str] = set()
    if isinstance(obj, str):
        out.update(m.group(1) for m in REF_RE.finditer(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            out |= refs_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= refs_in(v)
    return out


class SecretStore:
    def __init__(self, directory: Path, *, env_fallback: bool = True) -> None:
        self.dir = Path(directory)
        self.env_fallback = env_fallback

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:  # pragma: no cover
            pass

    def _path(self, name: str) -> Path:
        return self.dir / normalize_name(name)

    # -- CRUD
    def set(self, name: str, value: str) -> str:
        if not isinstance(value, str) or value == "":
            raise HubError("giá trị secret rỗng")
        n = normalize_name(name)
        self._ensure_dir()
        p = self.dir / n
        tmp = self.dir / f".{n}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        os.chmod(tmp, 0o600)
        tmp.replace(p)
        register_secret_values(value)  # H-12: che theo giá trị ở mọi đầu ra
        return n

    def get(self, name: str) -> str | None:
        p = self._path(name)
        if not p.is_file():
            return None
        v = p.read_text(encoding="utf-8")
        register_secret_values(v)
        return v

    def exists(self, name: str) -> bool:
        try:
            return self._path(name).is_file()
        except HubError:
            return False

    def delete(self, name: str) -> bool:
        p = self._path(name)
        if p.is_file():
            p.unlink()
            return True
        return False

    def list(self) -> list[dict[str, Any]]:
        """Chỉ tên + thời điểm sửa — KHÔNG trả giá trị."""
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.iterdir()):
            if p.is_file() and NAME_RE.match(p.name):
                st = p.stat()
                out.append({"name": p.name, "mode": oct(st.st_mode & 0o777), "updated_at": st.st_mtime})
        return out

    # -- tham chiếu
    def lookup(self, name: str) -> str | None:
        try:
            v = self.get(name)
        except HubError:
            v = None
        if v is None and self.env_fallback:
            v = os.environ.get(name)
            register_secret_values(v)  # H-12: giá trị env-fallback đã resolve cũng phải che
        return v

    def preload(self) -> int:
        """Nạp mọi giá trị secret vào bộ che theo giá trị (H-12) — gọi khi hub khởi tạo. Trả số secret."""
        n = 0
        if not self.dir.is_dir():
            return 0
        for p in self.dir.iterdir():
            if p.is_file() and NAME_RE.match(p.name):
                try:
                    register_secret_values(p.read_text(encoding="utf-8"))
                    n += 1
                except OSError:  # pragma: no cover
                    continue
        return n

    def missing(self, obj: Any) -> list[str]:
        return sorted(n for n in refs_in(obj) if self.lookup(n) is None)

    def resolve(self, obj: Any) -> Any:
        """Thay `${NAME}` bằng giá trị secret. Thiếu → SecretMissing (không bao giờ để nguyên chuỗi `${…}`)."""
        if isinstance(obj, str):

            def _sub(m: re.Match[str]) -> str:
                v = self.lookup(m.group(1))
                if v is None:
                    raise SecretMissing(
                        f"thiếu secret '{m.group(1)}' — đặt bằng `cfharness secret set {m.group(1)}`",
                        data={"secret": m.group(1)},
                    )
                return v

            return REF_RE.sub(_sub, obj)
        if isinstance(obj, dict):
            return {k: self.resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.resolve(v) for v in obj]
        return obj

    def mask_values(self, text: str) -> str:
        """Che mọi giá trị secret đang lưu nếu xuất hiện trong text (dùng trước khi ghi audit/log)."""
        if not text or not self.dir.is_dir():
            return text
        for p in self.dir.iterdir():
            if p.is_file() and NAME_RE.match(p.name):
                try:
                    v = p.read_text(encoding="utf-8")
                except OSError:  # pragma: no cover
                    continue
                if len(v) >= 6 and v in text:
                    text = text.replace(v, f"${{{p.name}}}")
        return text
