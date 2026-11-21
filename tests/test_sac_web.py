"""Web SAC (plan-superadmin SA1-2): Host, Cloudflare Access JWT, đăng nhập/kích hoạt, CSRF/Origin, step-up, menu theo vai trò."""

from __future__ import annotations

import base64
import json
import re

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from starlette.testclient import TestClient

from cfharness.admind.client import AdmindClient
from cfharness.admind.dispatch import Peer
from cfharness.superadmin.access import AccessVerifier
from cfharness.superadmin.app import create_app, form_params
from cfharness.web import totp as T

from admind_helpers import PW, SAC_UID, make_env

HOST = "superadmin.example.com"
BASE = f"https://{HOST}"
TEAM, AUD = "acme", "aud-123"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Signer:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub = self.key.public_key().public_numbers()
        self.jwks = {"keys": [{"kty": "RSA", "kid": "k1", "n": _b64(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
                               "e": _b64(pub.e.to_bytes(3, "big"))}]}

    def token(self, email: str, *, aud: str = AUD, exp: float = 4_000_000_000, kid: str = "k1") -> str:
        h = _b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
        p = _b64(json.dumps({"email": email, "aud": [aud], "exp": exp, "iss": f"https://{TEAM}.cloudflareaccess.com"}).encode())
        sig = self.key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{h}.{p}.{_b64(sig)}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    yield e
    e.state.db.close()


@pytest.fixture
def signer():
    return Signer()


def make_client(env, signer, *, access=True):
    cl = AdmindClient(transport=lambda req: env.disp.handle(req, Peer(uid=SAC_UID)))
    av = AccessVerifier(team=TEAM, aud=AUD, fetch=lambda team: signer.jwks, clock=env.clock) if access else None
    app = create_app(cl, hostname=HOST, access=av, loopback_ok=not access, clock=env.clock)
    return TestClient(app, base_url=BASE)


def H(signer, email="boss@ex.com", **kw):
    return {"cf-access-jwt-assertion": signer.token(email, **kw), "origin": BASE}


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, html[:500]
    return m.group(1)


def login(env, c, signer, name="boss"):
    acct = env.state.auth.account_by_email(f"{name}@ex.com")
    r = c.post("/login", data={"email": f"{name}@ex.com", "password": PW, "totp": env.code_for(acct["id"])}, headers=H(signer, f"{name}@ex.com"),
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/", r.text[:300]
    return r


def test_access_jwt_required(env, signer):
    c = make_client(env, signer)
    assert c.get("/login").status_code == 404
    assert c.get("/login").text == "Not Found"  # không tên sản phẩm
    assert c.get("/login", headers=H(signer, aud="khac")).status_code == 404
    assert c.get("/login", headers=H(signer, exp=1)).status_code == 404
    other = Signer()
    assert c.get("/login", headers={"cf-access-jwt-assertion": other.token("boss@ex.com")}).status_code == 404
    assert c.get("/login", headers=H(signer)).status_code == 200


def test_wrong_host_421(env, signer):
    c = make_client(env, signer)
    r = c.get("https://evil.example.com/login", headers=H(signer))
    assert r.status_code == 421


def test_login_and_pages(env, signer):
    env.make_admin("boss")
    c = make_client(env, signer)
    login(env, c, signer)
    r = c.get("/", headers=H(signer))
    assert r.status_code == 200 and "Tổng quan" in r.text
    assert "Content-Security-Policy" in r.headers and r.headers["Cache-Control"] == "no-store"
    for path in ("/admins", "/audit", "/jobs", "/approvals", "/settings", "/sessions"):
        assert c.get(path, headers=H(signer)).status_code == 200, path
    assert c.get("/khong-co", headers=H(signer)).status_code == 404


def test_login_email_must_match_access(env, signer):
    env.make_admin("boss")
    c = make_client(env, signer)
    acct = env.state.auth.account_by_email("boss@ex.com")
    r = c.post("/login", data={"email": "boss@ex.com", "password": PW, "totp": env.code_for(acct["id"])}, headers=H(signer, "khac@ex.com"))
    assert r.status_code == 400 and "Cloudflare Access" in r.text


def test_bad_login(env, signer):
    env.make_admin("boss")
    c = make_client(env, signer)
    r = c.post("/login", data={"email": "boss@ex.com", "password": "sai", "totp": "123456"}, headers=H(signer))
    assert r.status_code == 401


def test_csrf_and_origin(env, signer):
    env.make_admin("boss")
    c = make_client(env, signer)
    login(env, c, signer)
    html = c.get("/admins", headers=H(signer)).text
    tok = csrf_of(html)
    data = {"csrf": "sai", "p.email": "o@ex.com", "p.name": "O", "p.role": "operator"}
    assert c.post("/op/admin.create", data=data, headers=H(signer)).status_code == 403
    hdr = H(signer)
    hdr["origin"] = "https://evil.example.com"
    assert c.post("/op/admin.create", data={**data, "csrf": tok}, headers=hdr).status_code == 403


def test_op_with_step_up_and_reveal(env, signer):
    env.make_admin("boss")
    c = make_client(env, signer)
    login(env, c, signer)
    env.clock.tick(6 * 60)  # quá 5 phút → admin.create (mức Cao) cần step-up
    tok = csrf_of(c.get("/admins", headers=H(signer)).text)
    r = c.post("/op/admin.create", data={"csrf": tok, "p.email": "o@ex.com", "p.name": "O", "p.role": "operator", "next": "/admins"},
               headers=H(signer), follow_redirects=False)
    assert r.headers["location"] == "/step-up"
    page = c.get("/step-up", headers=H(signer)).text
    acct = env.state.auth.account_by_email("boss@ex.com")
    r = c.post("/step-up", data={"csrf": csrf_of(page), "totp": env.code_for(acct["id"])}, headers=H(signer), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admins"
    html = c.get("/admins", headers=H(signer)).text
    assert "Link kích hoạt" in html and "/enroll/" in html and "o@ex.com" in html
    # link chỉ hiện một lần
    assert "/enroll/" not in c.get("/admins", headers=H(signer)).text


def test_enroll_flow(env, signer):
    env.make_admin("boss")
    acct, code = env.state.auth.create(email="new@ex.com", name="New", role="auditor", scope={}, created_by="x")
    c = make_client(env, signer)
    r = c.get(f"/enroll/{code}", headers=H(signer, "new@ex.com"))
    assert r.status_code == 200 and "data:image/svg+xml" in r.text
    sec = re.search(r'<code class="mono">([A-Z2-7]+)</code>', r.text).group(1)
    env.clock.tick(31)
    r = c.post(f"/enroll/{code}", data={"password": PW, "password2": PW, "totp": T.totp(sec, env.clock())}, headers=H(signer, "new@ex.com"))
    assert r.status_code == 200 and "Đã kích hoạt" in r.text
    assert env.state.auth.account(acct["id"])["state"] == "active"
    assert c.get(f"/enroll/{code}", headers=H(signer, "new@ex.com")).status_code == 404  # dùng một lần


def test_menu_by_role(env, signer):
    env.make_admin("boss")
    env.make_admin("aud", role="auditor")
    c = make_client(env, signer)
    login(env, c, signer, "aud")
    html = c.get("/audit", headers=H(signer, "aud@ex.com")).text
    assert 'href="/audit"' in html and 'href="/admins"' not in html and 'href="/settings"' not in html
    assert c.get("/admins", headers=H(signer, "aud@ex.com")).status_code == 404
    assert c.get("/settings", headers=H(signer, "aud@ex.com")).status_code == 404


def test_session_idle_logs_out(env, signer):
    env.make_admin("boss")
    c = make_client(env, signer)
    login(env, c, signer)
    env.clock.tick(16 * 60)
    r = c.get("/admins", headers=H(signer), follow_redirects=False)
    assert r.status_code == 404  # không phiên → trang lạ 404; "/" → về đăng nhập
    assert c.get("/", headers=H(signer), follow_redirects=False).headers["location"] == "/login"


def test_loopback_mode_without_access(env, signer):
    c = make_client(env, signer, access=False)
    assert c.get("/login").status_code == 421  # Access chưa cấu hình: Host công khai bị từ chối
    assert TestClient(c.app, base_url="http://127.0.0.1:8700").get("/login").status_code == 200
    assert TestClient(c.app, base_url="https://other.example.com").get("/login").status_code == 421


def test_form_params():
    class F(dict):
        def keys(self):
            return [k for k, _ in self._items]

        def getlist(self, k):
            return [v for kk, v in self._items if kk == k]

    f = F()
    f._items = [("p.a", "x"), ("p.n:int", "5"), ("p.b:bool", "on"), ("p.s.t:list", "a, b\nc"), ("p.e", ""), ("p.m:multi", "1"), ("p.m:multi", "2"),
                ("csrf", "z")]
    assert form_params(f) == {"a": "x", "n": 5, "b": True, "s": {"t": ["a", "b", "c"]}, "m": ["1", "2"]}
