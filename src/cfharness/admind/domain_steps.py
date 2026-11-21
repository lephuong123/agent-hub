"""Bước job cho tên miền (DOM-03..05): DNS (API, đúng zone) → ingress (validate + rollback) → `web.public_origin` của hub → kiểm HTTPS.

Dùng chung cho job `domain.create|revoke|rename` và bước `domain` của job `user.create`. `jc.params` cần `domain_id`.
"""

from __future__ import annotations

from typing import Any

from .jobs import JobCtx, Step, StepError, kind


def _domain(jc: JobCtx, key: str = "domain_id") -> dict[str, Any]:
    d = jc.state.db.one("SELECT d.*, z.name AS zone, z.cf_zone_id FROM domains d JOIN zones z ON z.id=d.zone_id WHERE d.id=?",
                        (jc.params.get(key) or jc.data.get(key),))
    if not d:
        raise StepError("không có bản ghi tên miền")
    return d


def fqdn(d: dict[str, Any]) -> str:
    return d["zone"] if d["sub"] == "@" else f"{d['sub']}.{d['zone']}"


def tunnel_id(jc: JobCtx) -> str:
    from .ops_system import setting

    tid = setting(jc.state.db, "cf.tunnel_id")
    if not tid:
        try:
            tid = _tunnel(jc).load().tunnel
        except Exception:  # best-effort: chưa đọc được file tunnel → hỏi lại cf.tunnel_id
            tid = ""
    if not tid:
        raise StepError("chưa biết tunnel id (Cấu hình → cf.tunnel_id)")
    return tid


def _tunnel(jc: JobCtx) -> Any:
    from .ops_system import setting
    from .tunnel import TunnelConfig

    def mcp_ports() -> set[int]:
        return {int(r["mcp_port"]) for r in jc.state.db.all("SELECT mcp_port FROM hubs WHERE mcp_port IS NOT NULL")}

    return TunnelConfig(setting(jc.state.db, "cf.ingress_path"), jc.runner, forbidden_ports=mcp_ports,
                        probe=jc.state.extra.get("ingress_probe"))


def _cf(jc: JobCtx) -> Any:
    from .cloudflare import from_state
    from .ops import OpError

    try:
        return from_state(jc.state)
    except OpError as e:
        raise StepError(e.message) from None


def _service(jc: JobCtx, d: dict[str, Any]) -> str:
    if d.get("ingress_service"):
        return d["ingress_service"]
    if d["kind"] == "sac":
        from .ops_system import setting

        return setting(jc.state.db, "sac.service")
    h = jc.state.db.one("SELECT web_port FROM hubs WHERE user_id=?", (d["hub_user_id"],))
    if not h or not h["web_port"]:
        raise StepError("hub chưa có cổng web")
    from .tunnel import service_for

    return service_for(int(h["web_port"]))


class DnsCreate(Step):
    name, title = "dns", "Tạo bản ghi DNS (CNAME → tunnel) trong đúng zone"

    def check(self, jc: JobCtx) -> bool:
        d = _domain(jc)
        if not d["cf_zone_id"]:
            return False
        want = f"{tunnel_id(jc)}.cfargotunnel.com"
        return any(r.get("type") == "CNAME" and r.get("content") == want for r in _cf(jc).dns_find(d["cf_zone_id"], fqdn(d)))

    def apply(self, jc: JobCtx) -> None:
        d = _domain(jc)
        if not d["cf_zone_id"]:
            raise StepError(f"zone {d['zone']} chưa xác minh với Cloudflare (Tên miền → Zone → Kiểm)")
        try:
            r = _cf(jc).cname_upsert(d["cf_zone_id"], fqdn(d), f"{tunnel_id(jc)}.cfargotunnel.com")
        except Exception as e:  # noqa: BLE001
            raise StepError(str(e)) from None
        jc.state.db.exec("UPDATE domains SET dns_record_id=? WHERE id=?", (r.get("id"), d["id"]))
        jc.log(f"CNAME {fqdn(d)} → {tunnel_id(jc)}.cfargotunnel.com")

    def undo(self, jc: JobCtx) -> None:
        DnsDelete().apply(jc)


class DnsDelete(Step):
    name, title = "dns_delete", "Gỡ bản ghi DNS"

    def check(self, jc: JobCtx) -> bool:
        d = _domain(jc)
        return not d["cf_zone_id"] or not _cf(jc).dns_find(d["cf_zone_id"], fqdn(d))

    def apply(self, jc: JobCtx) -> None:
        d = _domain(jc)
        cf = _cf(jc)
        for r in cf.dns_find(d["cf_zone_id"], fqdn(d)):
            if r.get("type") == "CNAME" and str(r.get("content", "")).endswith(".cfargotunnel.com"):
                cf.dns_delete(d["cf_zone_id"], r["id"])
                jc.log(f"đã xoá CNAME {fqdn(d)}")
        jc.state.db.exec("UPDATE domains SET dns_record_id=NULL WHERE id=?", (d["id"],))


class IngressAdd(Step):
    name, title = "ingress", "Thêm hostname vào ingress tunnel (validate → thay → kickstart → kiểm)"

    def check(self, jc: JobCtx) -> bool:
        d = _domain(jc)
        try:
            return _tunnel(jc).load().hosts().get(fqdn(d)) == _service(jc, d)
        except Exception:  # best-effort: không đọc được ingress = coi như chưa có, bước apply sẽ báo lỗi thật
            return False

    def apply(self, jc: JobCtx) -> None:
        from .tunnel import IngressError

        d = _domain(jc)
        svc = _service(jc, d)
        try:
            r = _tunnel(jc).apply(lambda ing: ing.set(fqdn(d), svc), check_host=fqdn(d) if jc.state.extra.get("ingress_probe") else None)
        except IngressError as e:
            raise StepError(str(e)) from None
        jc.state.db.exec("UPDATE domains SET ingress_service=? WHERE id=?", (svc, d["id"]))
        jc.log(f"ingress {fqdn(d)} → {svc} ({'đổi' if r['changed'] else 'không đổi'})")

    def undo(self, jc: JobCtx) -> None:
        IngressRemove().apply(jc)


class IngressRemove(Step):
    name, title = "ingress_remove", "Gỡ hostname khỏi ingress tunnel"

    def check(self, jc: JobCtx) -> bool:
        try:
            return fqdn(_domain(jc)) not in _tunnel(jc).load().hosts()
        except Exception:  # best-effort: không đọc được ingress = chưa gỡ
            return False

    def apply(self, jc: JobCtx) -> None:
        from .tunnel import IngressError

        d = _domain(jc)
        try:
            _tunnel(jc).apply(lambda ing: ing.remove(fqdn(d)))
        except IngressError as e:
            raise StepError(str(e)) from None
        jc.log(f"đã gỡ ingress {fqdn(d)}")


class PublicOrigin(Step):
    name, title = "public_origin", "Đặt địa chỉ public của hub (web.public_origin) + khởi động lại web"

    def __init__(self, unset: bool = False) -> None:
        self.unset = unset
        if unset:
            self.name, self.title = "public_origin_unset", "Gỡ địa chỉ public của hub"

    def apply(self, jc: JobCtx) -> None:
        d = _domain(jc)
        if d["kind"] != "hub" or not d["hub_user_id"]:
            return
        u = jc.state.db.one("SELECT os_user FROM hub_users WHERE id=?", (d["hub_user_id"],))
        args = ["config", "unset", "web.public_origin"] if self.unset else ["config", "set", "web.public_origin", f"https://{fqdn(d)}"]
        r = jc.runner.cfh(u["os_user"], *args, timeout=60)
        if not r.ok:
            raise StepError("không đặt được web.public_origin: " + (r.err or r.out).strip()[:200])
        jc.runner.run(["launchctl", "kickstart", "-k", f"system/com.cfharness.web.{u['os_user']}"], timeout=30)
        jc.log(("đã gỡ" if self.unset else "đã đặt") + f" web.public_origin của {u['os_user']}")


class HttpsCheck(Step):
    name, title = "https_check", "Kiểm https://<tên miền> trả trang đăng nhập"

    def apply(self, jc: JobCtx) -> None:
        from .health import http_probe

        d = _domain(jc)
        probe = jc.state.extra.get("http_probe") or http_probe
        r = probe(fqdn(d))
        code = int(r.get("http") or 0)
        if not (200 <= code < 400 or code in (401, 403)):
            raise StepError(f"https://{fqdn(d)} trả mã {code} — DNS có thể chưa lan (thử lại sau 1–2 phút)")
        jc.state.db.exec("UPDATE domains SET state='healthy', checked_at=? WHERE id=?", (jc.state.clock(), d["id"]))
        jc.log(f"https://{fqdn(d)} → {code}")


class MarkDomain(Step):
    def __init__(self, state: str) -> None:
        self.state_value = state
        self.name, self.title = f"mark_{state}", f"Đánh dấu tên miền: {state}"

    def apply(self, jc: JobCtx) -> None:
        jc.state.db.exec("UPDATE domains SET state=? WHERE id=?", (self.state_value, _domain(jc)["id"]))


def create_steps(*, with_origin: bool = True, check: bool = True) -> list[Step]:
    steps: list[Step] = [DnsCreate(), IngressAdd()]
    if with_origin:
        steps.append(PublicOrigin())
    if check:
        steps.append(HttpsCheck())
    return steps


def revoke_steps() -> list[Step]:
    return [MarkDomain("revoking"), IngressRemove(), DnsDelete(), AccessRemove(), PublicOrigin(unset=True), MarkDomain("revoked")]


class AccessRemove(Step):
    name, title = "access_remove", "Gỡ ứng dụng Cloudflare Access (nếu có)"

    def check(self, jc: JobCtx) -> bool:
        return not _domain(jc).get("access_app_id")

    def apply(self, jc: JobCtx) -> None:
        from .ops_system import setting

        d = _domain(jc)
        acc = setting(jc.state.db, "cf.account_id")
        if acc and d.get("access_app_id"):
            _cf(jc).access_app_delete(acc, d["access_app_id"])
        jc.state.db.exec("UPDATE domains SET access_app_id=NULL WHERE id=?", (d["id"],))


@kind("domain.create")
def _k_create(params: dict[str, Any]) -> list[Step]:
    return create_steps(with_origin=params.get("kind", "hub") == "hub")


@kind("domain.revoke")
def _k_revoke(params: dict[str, Any]) -> list[Step]:
    return revoke_steps()


class RenameSwap(Step):
    name, title = "rename_swap", "Chuyển hub sang tên miền mới, giữ tên cũ chuyển hướng"

    def apply(self, jc: JobCtx) -> None:
        from .ops_system import setting

        old = jc.state.db.one("SELECT * FROM domains WHERE id=?", (jc.params["old_domain_id"],))
        days = int(setting(jc.state.db, "domain.redirect_days"))
        jc.state.db.exec("UPDATE domains SET kind='redirect', redirect_to=?, redirect_until=? WHERE id=?",
                         (jc.params["domain_id"], jc.state.clock() + days * 86400, old["id"]))
        jc.log(f"tên cũ giữ chuyển hướng {days} ngày")


@kind("domain.rename")
def _k_rename(params: dict[str, Any]) -> list[Step]:
    return [*create_steps(), RenameSwap(), WebSessionsRevoke()]


class WebSessionsRevoke(Step):
    name, title = "web_sessions_revoke", "Huỷ mọi phiên web của hub"

    def apply(self, jc: JobCtx) -> None:
        d = _domain(jc)
        u = jc.state.db.one("SELECT os_user FROM hub_users WHERE id=?", (d["hub_user_id"],))
        jc.runner.run(["launchctl", "kickstart", "-k", f"system/com.cfharness.web.{u['os_user']}"], timeout=30)

