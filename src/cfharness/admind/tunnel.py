"""Cấu hình ingress của tunnel Cloudflare `cfharness` (plan-superadmin SA0-3, NFR-SA-08).

File YAML (`cf.ingress_path`, mặc định `/etc/cfharness/tunnel.yml`):
```yaml
tunnel: <uuid>
credentials-file: /etc/cfharness/<uuid>.json
ingress:
  - hostname: lamlinh.trungtamdhs.com
    service: http://127.0.0.1:8721
  - service: http_status:404        # luôn đúng MỘT dòng bắt-tất-cả ở cuối
```
Sửa an toàn: dựng nội dung mới → ghi file tạm cạnh file thật → `cloudflared tunnel --config <tạm> ingress validate` → giữ bản `.bak` →
thay nguyên tử → kickstart tunnel → (tuỳ chọn) kiểm HTTP qua hàm `probe` → lỗi thì khôi phục bản cũ và kickstart lại.
Cấm trỏ ingress vào cổng MCP của bất kỳ hub nào (DOM-09).
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .runner import Runner

SERVICE_RE = re.compile(r"^http://127\.0\.0\.1:(\d{2,5})$")
CATCH_ALL = "http_status:404"
TUNNEL_LABEL = "com.cfharness.tunnel"
CLOUDFLARED = ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared")


class IngressError(RuntimeError):
    pass


@dataclass
class Ingress:
    tunnel: str = ""
    credentials: str = ""
    rules: list[dict[str, Any]] = field(default_factory=list)   # không gồm dòng bắt-tất-cả
    extra: dict[str, Any] = field(default_factory=dict)          # khoá khác ở gốc file (giữ nguyên)

    @classmethod
    def parse(cls, text: str) -> "Ingress":
        try:
            d = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise IngressError(f"YAML hỏng: {e}") from None
        if not isinstance(d, dict):
            raise IngressError("file ingress phải là object")
        rules = d.get("ingress") or []
        if not isinstance(rules, list):
            raise IngressError("ingress phải là danh sách")
        body = [r for r in rules if isinstance(r, dict) and r.get("hostname")]
        extra = {k: v for k, v in d.items() if k not in ("tunnel", "credentials-file", "ingress")}
        return cls(tunnel=str(d.get("tunnel") or ""), credentials=str(d.get("credentials-file") or ""), rules=body, extra=extra)

    def dump(self) -> str:
        d: dict[str, Any] = {}
        if self.tunnel:
            d["tunnel"] = self.tunnel
        if self.credentials:
            d["credentials-file"] = self.credentials
        d.update(self.extra)
        d["ingress"] = [*self.rules, {"service": CATCH_ALL}]
        return yaml.safe_dump(d, sort_keys=False, allow_unicode=True)

    def hosts(self) -> dict[str, str]:
        return {str(r["hostname"]).lower(): str(r.get("service") or "") for r in self.rules}

    def port_of(self, hostname: str) -> int | None:
        m = SERVICE_RE.match(self.hosts().get(hostname.lower(), ""))
        return int(m.group(1)) if m else None

    def set(self, hostname: str, service: str) -> bool:
        """Thêm/đổi; trả True nếu nội dung đổi."""
        h = hostname.lower()
        for r in self.rules:
            if str(r.get("hostname", "")).lower() == h:
                if r.get("service") == service:
                    return False
                r["service"] = service
                return True
        self.rules.append({"hostname": h, "service": service})
        return True

    def remove(self, hostname: str) -> bool:
        h = hostname.lower()
        n = len(self.rules)
        self.rules = [r for r in self.rules if str(r.get("hostname", "")).lower() != h]
        return len(self.rules) != n


def service_for(port: int) -> str:
    if not 1024 <= int(port) <= 65535:
        raise IngressError("cổng ngoài 1024..65535")
    return f"http://127.0.0.1:{int(port)}"


def cloudflared_bin() -> str:
    for p in CLOUDFLARED:
        if os.path.exists(p):
            return p
    return "cloudflared"


class TunnelConfig:
    def __init__(self, path: Path | str, runner: Runner, *, forbidden_ports: Callable[[], set[int]] = set,
                 probe: Callable[[str], bool] | None = None, kickstart_wait_s: float = 3.0) -> None:
        self.path = Path(path)
        self.runner = runner
        self.forbidden_ports = forbidden_ports
        self.probe = probe
        self.wait = kickstart_wait_s

    def load(self) -> Ingress:
        try:
            return Ingress.parse(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise IngressError(f"không thấy file ingress {self.path}") from None

    def _validate(self, tmp: Path) -> None:
        r = self.runner.run([cloudflared_bin(), "tunnel", "--config", str(tmp), "ingress", "validate"], timeout=30)
        if not r.ok:
            raise IngressError("cloudflared ingress validate lỗi: " + (r.err or r.out).strip()[:300])

    def kickstart(self) -> bool:
        return self.runner.run(["launchctl", "kickstart", "-k", f"system/{TUNNEL_LABEL}"], timeout=30).ok

    def apply(self, mutate: Callable[[Ingress], bool], *, check_host: str | None = None) -> dict[str, Any]:
        """Áp một thay đổi. `mutate` trả False = không đổi gì (không đụng file). Lỗi ở bất kỳ bước nào → giữ/khôi phục bản cũ."""
        ing = self.load()
        old_text = self.path.read_text(encoding="utf-8")
        if not mutate(ing):
            return {"changed": False}
        bad = self.forbidden_ports()
        for h, svc in ing.hosts().items():
            m = SERVICE_RE.match(svc)
            if m and int(m.group(1)) in bad:
                raise IngressError(f"cấm trỏ {h} vào cổng MCP {m.group(1)}")
        new_text = ing.dump()
        tmp = self.path.with_name(f".{self.path.name}.new")
        tmp.write_text(new_text, encoding="utf-8")
        try:
            self._validate(tmp)
            bak = self.path.with_name(f"{self.path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(self.path, bak)
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()
        self.kickstart()
        if check_host and self.probe is not None:
            time.sleep(self.wait)
            if not self.probe(check_host):
                self.path.write_text(old_text, encoding="utf-8")
                self.kickstart()
                raise IngressError(f"sau khi đổi, https://{check_host} không trả trang đăng nhập — đã khôi phục cấu hình cũ")
        return {"changed": True}
