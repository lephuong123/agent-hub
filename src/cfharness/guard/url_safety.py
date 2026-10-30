"""Kiểm URL ra mạng (SSRF/exfil) cho WebFetch và curl/wget trong Bash — ý tưởng từ Hermes `tools/url_safety.py`.

Hai tầng:
- `static_check(url)` — THUẦN (không mạng): scheme chỉ http/https, không thông tin xác thực trong URL, host không rỗng,
  tên nội bộ (`localhost`, `*.localhost`, `*.local`, `*.internal`, `*.home.arpa`, `*.lan`, `metadata.google.internal`…),
  IP literal mọi dạng (thập phân/hex/bát phân/số nguyên, IPv6 kể cả IPv4-mapped/6to4/NAT64/Teredo) thuộc loopback, RFC1918,
  link-local, CGNAT 100.64/10, ULA, multicast, reserved, metadata đám mây (169.254.169.254, fd00:ec2::254, 100.100.100.200…).
- `resolve_check(host)` — phân giải DNS (trong DAEMON, không trong hook) và kiểm MỌI IP trả về như trên. Lỗi/hết giờ phân giải
  → không xác minh được (fail-closed: giữ chờ người).

Rủi ro còn lại (ghi ở docs/dev-notes/hardening-SEC.md §WEB-1): WebFetch chạy trong tiến trình `claude` và tự phân giải DNS lại
lúc tải — DNS rebinding (TTL 0: lần daemon hỏi → IP công khai, lần claude hỏi → 127.0.0.1) KHÔNG chặn được ở đây. Lớp này chỉ
chặn tên/IP lộ liễu và tên miền trỏ cố định vào mạng nội bộ.

Mọi kết quả là `UrlCheck(ok, rule, reason, host, metadata)`; `metadata=True` = đích metadata đám mây (cấm thẳng với mọi agent).
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutTimeout
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")

# metadata đám mây: AWS/GCP/Azure/OpenStack/DO (169.254.169.254), AWS IPv6, ECS task metadata, Alibaba, Oracle
METADATA_IPS = frozenset({"169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200", "192.0.0.192"})
METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata.goog", "metadata", "instance-data", "instance-data.ec2.internal",
                            "metadata.azure.com", "metadata.packet.net"})
# hậu tố tên chỉ có nghĩa trong mạng nội bộ (RFC 6761/6762/8375, ICANN `.internal`)
INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan", ".intranet", ".localdomain")
INTERNAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "broadcasthost"})

# mạng bị chặn tường minh (ngoài `not is_global`): CGNAT, benchmark, TEST-NET, 6to4 relay, IETF protocol assignments…
_BLOCKED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "255.255.255.255/32",
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64", "2001::/32", "2001:db8::/32", "2002::/16",
    "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
))
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


@dataclass
class UrlCheck:
    ok: bool
    rule: str = "url_ok"
    reason: str = ""
    host: str | None = None
    metadata: bool = False
    ips: tuple[str, ...] = ()


# ============================================================================ IP
def parse_ipv4_loose(h: str) -> ipaddress.IPv4Address | None:
    """Như `inet_aton`: 1–4 phần, mỗi phần thập phân / 0x hex / 0 bát phân (`0177.1`, `0x7f000001`, `2130706433`, `127.1`)."""
    if not h or not re.fullmatch(r"[0-9a-fA-FxX.]+", h):
        return None
    parts = (h[:-1] if h.endswith(".") else h).split(".")
    if not parts or len(parts) > 4:
        return None
    nums: list[int] = []
    for p in parts:
        if not p:
            return None
        try:
            if p.lower().startswith("0x"):
                n = int(p[2:] or "0", 16)
            elif len(p) > 1 and p.startswith("0"):
                n = int(p, 8)
            else:
                n = int(p, 10)
        except ValueError:
            return None
        nums.append(n)
    limits = {1: [2**32], 2: [256, 2**24], 3: [256, 256, 2**16], 4: [256, 256, 256, 256]}[len(nums)]
    if any(n < 0 or n >= lim for n, lim in zip(nums, limits)):
        return None
    val = 0
    for n in nums[:-1]:
        val = val * 256 + n
    val = val * limits[-1] + nums[-1] if len(nums) > 1 else nums[0]
    try:
        return ipaddress.IPv4Address(val)
    except ValueError:
        return None


def parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    h = host.strip().strip("[]")
    if "%" in h:  # zone id IPv6 (fe80::1%en0)
        h = h.split("%", 1)[0]
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        return parse_ipv4_loose(h)


def _embedded_v4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    out: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped:
        out.append(ip.ipv4_mapped)
    if ip.sixtofour:
        out.append(ip.sixtofour)
    if ip.teredo:
        out.extend(ip.teredo)
    if any(ip in n for n in _NAT64):
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    # IPv4-compatible (::a.b.c.d, đã bỏ) — vẫn coi là v4
    if int(ip) >> 32 == 0 and int(ip) > 1:
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def ip_is_metadata(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if str(ip) in METADATA_IPS:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        return any(str(v4) in METADATA_IPS for v4 in _embedded_v4(ip))
    return False


def ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Lý do chặn (chuỗi ngắn) hoặc None nếu là IP công khai."""
    if ip_is_metadata(ip):
        return "metadata đám mây"
    if isinstance(ip, ipaddress.IPv6Address):
        for v4 in _embedded_v4(ip):
            r = ip_blocked(v4)
            if r:
                return f"IPv6 nhúng IPv4 {v4} ({r})"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "CGNAT 100.64/10"
    if ip.is_private:
        return "mạng riêng (RFC1918/ULA)"
    if ip.is_reserved:
        return "reserved"
    for n in _BLOCKED_NETS:
        if ip.version == n.version and ip in n:
            return f"dải {n}"
    if not ip.is_global:
        return "không phải địa chỉ công khai"
    return None


# ============================================================================ host / URL
def norm_host(host: str | None) -> str:
    return (host or "").strip().strip("[]").rstrip(".").lower()


def host_internal_name(h: str) -> str | None:
    if h in METADATA_HOSTS:
        return "metadata"
    if h in INTERNAL_NAMES or any(h.endswith(s) for s in INTERNAL_SUFFIXES):
        return "tên nội bộ"
    if "." not in h and ":" not in h and not h.isdigit():
        return "tên không đủ điều kiện (single-label, phân giải theo search domain nội bộ)"
    return None


def split_url(url: str) -> tuple[str, str | None, Any]:
    """→ (scheme, host, SplitResult). URL không có `//` coi như thiếu scheme."""
    u = urlsplit(url.strip())
    return (u.scheme or "").lower(), u.hostname, u


def static_check(url: str) -> UrlCheck:
    """Kiểm không cần mạng. ok=False → rule ∈ url_scheme | url_credentials | url_no_host | ssrf_metadata | ssrf_internal_name |
    ssrf_private_ip | url_unparsable."""
    raw = str(url or "").strip()
    if not raw:
        return UrlCheck(False, "url_no_host", "URL rỗng")
    if any(c in raw for c in ("\\", "\x00", "\r", "\n", "\t")) or any(ord(c) < 0x20 for c in raw):
        return UrlCheck(False, "url_unparsable", "URL chứa ký tự điều khiển/`\\` (mập mờ giữa các trình phân tích)")
    try:
        scheme, _h, u = split_url(raw)
        port = u.port  # ValueError nếu cổng sai
    except ValueError as e:
        return UrlCheck(False, "url_unparsable", f"URL không phân tích được: {e}")
    del port
    if scheme not in ALLOWED_SCHEMES:
        return UrlCheck(False, "url_scheme", f"scheme '{scheme or '(thiếu)'}' — chỉ http/https")
    if u.username is not None or u.password is not None or "@" in (u.netloc or ""):
        return UrlCheck(False, "url_credentials", "URL chứa thông tin xác thực (user:pass@) — kênh rò rỉ/nhầm host")
    h = norm_host(u.hostname)
    if not h:
        return UrlCheck(False, "url_no_host", "URL không có host")
    try:
        h.encode("ascii")
    except UnicodeEncodeError:
        try:
            h = h.encode("idna").decode("ascii")
        except UnicodeError:
            return UrlCheck(False, "url_unparsable", "host IDN không hợp lệ", h)
    ip = parse_ip(h)
    if ip is not None:
        if ip_is_metadata(ip):
            return UrlCheck(False, "ssrf_metadata", f"đích metadata đám mây ({h})", h, metadata=True)
        why = ip_blocked(ip)
        if why:
            return UrlCheck(False, "ssrf_private_ip", f"IP không công khai {h}: {why}", h)
        return UrlCheck(True, "url_ok", "", h, ips=(str(ip),))
    if re.fullmatch(r"[0-9a-fx.:]+", h) and any(c.isdigit() for c in h):
        return UrlCheck(False, "ssrf_private_ip", f"host dạng số lạ ({h}) — trình phân giải có thể hiểu là IP nội bộ", h)
    why = host_internal_name(h)
    if why:
        return UrlCheck(False, "ssrf_metadata" if why == "metadata" else "ssrf_internal_name", f"host {h}: {why}", h, metadata=why == "metadata")
    return UrlCheck(True, "url_ok", "", h)


Resolver = Callable[[str], Iterable[str]]


def default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(str(i[4][0]) for i in infos))


_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cfharness-dns")


def resolve_check(host: str, *, resolver: Resolver | None = None, timeout_s: float = 3.0) -> UrlCheck:
    """Phân giải DNS và kiểm MỌI IP. Gọi TRONG daemon (đồng bộ, có hạn giờ). Không phân giải được → ok=False (fail-closed)."""
    h = norm_host(host)
    if parse_ip(h) is not None:
        return UrlCheck(True, "url_ok", "", h, ips=(h,))
    fn = resolver or default_resolver
    try:
        ips = list(_POOL.submit(lambda: list(fn(h))).result(timeout=timeout_s))
    except _FutTimeout:
        return UrlCheck(False, "url_dns_failed", f"phân giải DNS {h} quá {timeout_s}s — không xác minh được đích", h)
    except Exception as e:  # gaierror, lỗi resolver giả trong test…
        return UrlCheck(False, "url_dns_failed", f"không phân giải được {h} ({type(e).__name__}) — không xác minh được đích", h)
    if not ips:
        return UrlCheck(False, "url_dns_failed", f"{h} không có địa chỉ IP", h)
    for s in ips:
        ip = parse_ip(str(s))
        if ip is None:
            return UrlCheck(False, "url_dns_failed", f"{h} → '{s}' không phải IP", h)
        if ip_is_metadata(ip):
            return UrlCheck(False, "ssrf_metadata", f"{h} phân giải về metadata đám mây {s}", h, metadata=True, ips=tuple(ips))
        why = ip_blocked(ip)
        if why:
            return UrlCheck(False, "ssrf_private_resolved", f"{h} phân giải về {s} ({why})", h, ips=tuple(ips))
    return UrlCheck(True, "url_ok", "", h, ips=tuple(str(x) for x in ips))


# ============================================================================ danh sách domain (glob)
def domain_matches(host: str, patterns: Iterable[str]) -> str | None:
    """Mẫu glob trên host (`example.com`, `*.example.com`, `docs.*.org`). `*.x.com` KHÔNG khớp `x.com` (ghi cả hai nếu cần).
    Trả mẫu khớp đầu tiên."""
    h = norm_host(host)
    for p in patterns or ():
        pat = str(p or "").strip().lower().rstrip(".")
        if not pat:
            continue
        if pat == "*" or fnmatch.fnmatchcase(h, pat):
            return pat
    return None


_DOMAIN_PAT = re.compile(r"\*|(\*\.)?[a-z0-9*?-]+(\.[a-z0-9*?-]+)*")


def validate_domain_list(key: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{key} phải là danh sách domain (glob), vd ['docs.python.org', '*.github.io']")
    for x in value:
        if not _DOMAIN_PAT.fullmatch(x.strip().lower().rstrip(".")):
            raise ValueError(f"{key}: mẫu domain không hợp lệ {x!r}")


WEB_DOMAIN_MODES = ("any", "allowlist")


# ============================================================================ allowlist theo agent (`tool_policy.web_allow_domains`)
def normalize_agent_domains(value: Any) -> list[str] | None:
    """Kiểm + chuẩn hoá `tool_policy.web_allow_domains`: None = chưa đặt (hành vi cũ); danh sách = CHỈ các host khớp mẫu (rỗng = chặn mọi
    WebFetch). Chữ thường, bỏ dấu chấm cuối, IDN → punycode (khớp với host `static_check` trả về). `*` bị từ chối (= không giới hạn:
    dùng null). Sai → ValueError."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("web_allow_domains phải là danh sách domain (glob), vd ['docs.python.org', '*.github.io'] hoặc null")
    out: list[str] = []
    for x in value:
        d = x.strip().lower().rstrip(".")
        try:
            d.encode("ascii")
        except UnicodeEncodeError:
            try:
                d = ".".join(lab if lab == "*" or lab.isascii() else lab.encode("idna").decode("ascii") for lab in d.split("."))
            except UnicodeError:
                raise ValueError(f"web_allow_domains: domain IDN không hợp lệ {x!r}") from None
        if d == "*" or not _DOMAIN_PAT.fullmatch(d):
            raise ValueError(f"web_allow_domains: mẫu domain không hợp lệ {x!r} (không giới hạn = null, không dùng '*')")
        if d not in out:
            out.append(d)
    return out


def agent_domains_widening(cur: Any, new: Any) -> str | None:
    """Đổi `web_allow_domains` có NỚI quyền không (chỉ người được nới): bỏ allowlist đang đặt, hoặc thêm mẫu chưa có. Thu hẹp/đặt mới → None."""
    if cur is None:
        return None
    if new is None:
        return "bỏ allowlist domain WebFetch (web_allow_domains) — chỉ người được nới"
    cur_l = {str(x).strip().lower().rstrip(".") for x in cur} if isinstance(cur, list) else set()
    try:
        new_l = normalize_agent_domains(new) or []
    except ValueError as e:
        return str(e)
    added = [x for x in new_l if x not in cur_l]
    if added:
        return f"thêm domain vào web_allow_domains: {added} — chỉ người được nới"
    return None


def validate_setting(key: str, value: Any) -> None:
    """Kiểm `guard.web.*`, `guard.second_opinion.*`, `manager.web_domains` (gọi từ `config.validate_setting`) — sai → ValueError."""
    if key in ("guard.web", "guard"):
        d = value.get("web") if key == "guard" and isinstance(value, dict) else value
        if key == "guard" and not isinstance(d, dict):
            d = None
        if d is not None:
            if not isinstance(d, dict):
                raise ValueError("guard.web phải là object {allow_domains, deny_domains, resolve_dns, dns_timeout_s}")
            for k, v in d.items():
                validate_setting(f"guard.web.{k}", v)
        if key == "guard" and isinstance(value, dict) and value.get("second_opinion") is not None:
            validate_setting("guard.second_opinion", value["second_opinion"])
        return
    if key in ("guard.web.allow_domains", "guard.web.deny_domains"):
        validate_domain_list(key, value)
    elif key == "guard.web.resolve_dns":
        if value is not True:  # tắt phân giải DNS = bỏ lớp kiểm tên miền trỏ nội bộ → không cho tắt qua config.set
            raise ValueError("guard.web.resolve_dns chỉ nhận true (phân giải DNS là một phần kiểm SSRF)")
    elif key == "guard.web.dns_timeout_s":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value <= 30:
            raise ValueError("guard.web.dns_timeout_s phải trong (0, 30]")
    elif key.startswith("guard.web."):
        raise ValueError(f"khoá lạ {key} (được: allow_domains, deny_domains, resolve_dns, dns_timeout_s)")
    elif key == "guard.second_opinion":
        if not isinstance(value, dict):
            raise ValueError("guard.second_opinion phải là object")
        for k, v in value.items():
            validate_setting(f"guard.second_opinion.{k}", v)
    elif key.startswith("guard.second_opinion."):
        sub = key.rsplit(".", 1)[1]
        if sub == "enabled":
            if not isinstance(value, bool):
                raise ValueError("guard.second_opinion.enabled phải là true/false")
        elif sub in ("daily_budget_usd", "call_budget_usd", "timeout_s", "cache_ttl_s"):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{key} phải là số > 0")
            if sub == "timeout_s" and value > 120:
                raise ValueError("guard.second_opinion.timeout_s tối đa 120")
        elif sub in ("max_calls_per_day",):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{key} phải là số nguyên ≥ 1")
        elif sub in ("model", "binary"):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{key} phải là chuỗi")
        else:
            raise ValueError(f"khoá lạ {key}")
    elif key == "manager.web_domains":
        if value not in WEB_DOMAIN_MODES:
            raise ValueError(f"manager.web_domains phải là {'|'.join(WEB_DOMAIN_MODES)}")
    elif key == "manager" and isinstance(value, dict) and "web_domains" in value:
        validate_setting("manager.web_domains", value["web_domains"])
