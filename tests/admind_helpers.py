"""Tiện ích test admind: State trên thư mục tạm, đồng hồ giả, tạo tài khoản quản trị đã kích hoạt, gọi thao tác."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cfharness.admind.dispatch import Dispatcher, Peer, State
from cfharness.admind.paths import AdmindPaths
from cfharness.web import totp as T

SAC_UID = 501
PW = "mat-khau-rat-dai-123"


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float) -> None:
        self.t += s


@dataclass
class Env:
    state: State
    disp: Dispatcher
    clock: Clock
    tickets: dict[str, str] = field(default_factory=dict)

    def call(self, op: str, params: dict[str, Any] | None = None, *, as_: str | None = None, root: bool = False, ip: str = "1.2.3.4",
             ticket: str | None = None) -> dict[str, Any]:
        req: dict[str, Any] = {"op": op, "params": params or {}, "meta": {"ip": ip, "ua": "pytest"}}
        tk = ticket or (self.tickets.get(as_) if as_ else None)
        if tk:
            req["ticket"] = tk
        return self.disp.handle(req, Peer(uid=0 if root else SAC_UID, root=root))

    def ok(self, op: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        r = self.call(op, params, **kw)
        assert r["ok"], r
        return r["result"]

    def err(self, op: str, params: dict[str, Any] | None = None, **kw: Any) -> str:
        r = self.call(op, params, **kw)
        assert not r["ok"], r
        return r["error"]["code"]

    def code_for(self, admin_id: str) -> str:
        sec = self.state.vault.get(f"totp.{admin_id}")
        self.clock.tick(31)  # mỗi lần dùng một bước TOTP mới (chống dùng lại)
        return T.totp(sec, self.clock())

    def make_admin(self, name: str, role: str = "super_admin", scope: dict[str, Any] | None = None) -> dict[str, Any]:
        acct, code = self.state.auth.create(email=f"{name}@ex.com", name=name, role=role, scope=scope or {}, created_by="test")
        info = self.state.auth.enroll_begin(code)
        self.clock.tick(31)
        self.state.auth.enroll_finish(code, PW, T.totp(info["secret"], self.clock()))
        r = self.ok("auth.login", {"email": f"{name}@ex.com", "password": PW, "totp": self.code_for(acct["id"])})
        self.tickets[name] = r["ticket"]
        return r["account"]

    def step_up(self, name: str) -> None:
        acct = self.state.auth.account_by_email(f"{name}@ex.com")
        self.ok("auth.step_up", {"totp": self.code_for(acct["id"])}, as_=name)


def make_env(tmp_path: Path, monkeypatch: Any, **kw: Any) -> Env:
    root = tmp_path / "admind"
    monkeypatch.setenv("CFHARNESS_TEST_ADMIND_ROOT", str(root))
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(tmp_path / "shared"))
    clock = Clock()
    st = State.open(AdmindPaths.default(), clock=clock, scrypt_n=2**8, **kw)
    st.jobs.synchronous = True
    disp = Dispatcher(st, allowed_uids={SAC_UID})
    st.extra["dispatcher"] = disp
    return Env(state=st, disp=disp, clock=clock)
