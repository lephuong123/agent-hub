"""Phần `secrets.enc` của gói xuất hub (`hub_transfer.py`) — PR2 của `docs/dev-notes/plan-hub-clone.md` mục 5.

Mã hoá: scrypt(passphrase, salt 16 B, n=2^17, r=8, p=1) → khoá 32 B → AES-256-GCM, nonce 12 B ngẫu nhiên.
AAD = nhãn cố định + id gói + sha256 của manifest CHUẨN HOÁ đã bỏ đúng hai thứ phụ thuộc vào bản mã (mục `files` của `secrets.enc`
và `sections.secrets`) → manifest liệt kê sha256 của MỌI phần tử khác, nên tráo bất kỳ phần nào khác của gói (hub.db, tri thức,
manifest, tham số KDF, danh sách tên secret…) làm giải mã thất bại. Thẻ GCM bảo vệ chính bản mã.

Bản rõ = một tar nhỏ trong RAM, CHỈ các đường dẫn trong danh sách trắng `allowed_payload_path`:
- `secrets/<NAME>` (tên dạng `secrets.NAME_RE`) + `secrets/.meta.json` (chỉ thời điểm đặt, của các tên được mang);
- `auth/totp.json` khi xuất với `--with-totp` (bí mật TOTP đang dùng — KHÔNG mang `totp_enroll.json`: link đăng ký gắn máy cũ);
- `mcp_server/tokens.json` khi xuất với `--with-mcp-tokens`.
KHÔNG BAO GIỜ: `auth/web.json` (khoá ký phiên web), `auth/trusted_devices.json`, `<socket>.auth` — kể cả khi gói bị sửa tay để chứa
chúng: bộ đọc từ chối mọi đường dẫn ngoài danh sách trắng. Cũng KHÔNG BAO GIỜ (cả clone lẫn move): thư mục con của `secrets/`, cụ thể
`secrets/filehub/tokens.json` — token THIẾT BỊ Không gian số (`--device "cfharness · <máy>"`, binary sync tự ghi): mang sang máy khác = hai
máy đồng bộ dưới CÙNG một thiết bị. `collect` chỉ lấy file thường tên `NAME_RE` ngay trong `secrets/` → thư mục con bị bỏ qua sạch (không
lỗi, không tính là secret thiếu — `missing_secrets` cũng chỉ đếm tên `NAME_RE`); (đồng bộ 2 chiều cũ đã bỏ — máy mới dùng cổng cfharness: `cfharness filehub lane setup`).

Passphrase CHỈ sống trong tiến trình CLI của người: CLI hỏi (getpass, TTY) hoặc đọc `--passphrase-file` (phải 0600, của chính user) → daemon
đóng gói phần không mật như PR1 (RPC `hub.export` không nhận passphrase) → CLI tự đọc `HUB_HOME/secrets/` (cùng user OS) và `seal_package`
gắn `secrets.enc` vào gói. Lý do chọn phía CLI: passphrase không bao giờ đi qua socket/RPC, nên không có đường nào lọt vào audit (bọc audit
ghi params), event, log daemon, DB hay file tạm — không phải dựa vào việc nhớ gỡ khoá trước audit như `account.*`. (Web — PR6 — sẽ phải
mã hoá trong daemon: `seal_package` là hàm thuần dùng lại được; `wrap_with_audit` đã gỡ khoá `passphrase` phòng hờ.)

Phân loại khi NHẬP (`classify`): chế độ `clone` (2 hub cùng chạy) KHÔNG ghi bot Telegram/Slack (`TELEGRAM_*`, `SLACK_*` — 2 máy cùng token
→ 409/sự kiện chia đôi) và bản ghi OAuth MCP kiểu cũ (`MCP_OAUTH_*` — OAuth của hub đã gỡ từ v0.2.28;
gói cũ vẫn có thể mang bản ghi này, `clone` bỏ như trước, `move` giữ — vô hại, chỉ còn dùng để che theo giá trị). Giữ: token MCP tĩnh
(`MCP_<server>_<KEY>`, vd `…_AUTHORIZATION`, `…_OAUTH_CLIENT_SECRET` do người nhập), tài khoản Claude (`CLAUDE_ACCT_*`,
`CLAUDE_CODE_OAUTH_TOKEN`) và mọi secret khác. `move` → ghi tất cả.
"""

from __future__ import annotations

import base64
import copy
import getpass
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import HubError
from .secrets import NAME_RE

SECTION = "secrets"
MEMBER = "secrets.enc"
CIPHER = "aes-256-gcm"
KDF_NAME = "scrypt"
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12
# Bộ đọc chấp nhận tham số KDF trong khoảng này (gói ghi tham số; chặn tham số yếu và tham số làm treo máy)
N_RANGE = (2**14, 2**20)
R_RANGE = (8, 16)
P_RANGE = (1, 4)
MIN_PASSPHRASE = 12
MAX_PASSPHRASE = 1024
MAX_PLAIN = 32 * 1024 * 1024  # trần bản rõ (tar secret) — chống bom
AAD_LABEL = b"cfharness-export/secrets/v1\n"

TOTP_FILE = "auth/totp.json"
MCP_TOKENS_FILE = "mcp_server/tokens.json"
META_FILE = "secrets/.meta.json"
NEVER = ("auth/web.json", "auth/trusted_devices.json", "auth/totp_enroll.json", "secrets/filehub/tokens.json")  # tài liệu + test: không bao giờ vào gói

# Phân loại theo chế độ nhập (mục 2 plan). Thêm loại secret mới của hub → khai báo ở đây + ca trong tests/test_hub_transfer_secrets.py
CLONE_SKIP_PREFIXES: tuple[tuple[str, str], ...] = (
    ("TELEGRAM_", "channel"),   # TELEGRAM_BOT_TOKEN, TELEGRAM_CALLBACK_KEY
    ("SLACK_", "channel"),      # SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CALLBACK_KEY, SLACK_WEBHOOK_URL
    ("MCP_OAUTH_", "oauth"),    # MCP_OAUTH_<TÊN>_<băm> — OAuth hub kiểu cũ (gỡ từ v0.2.28); gói cũ vẫn bỏ
    ("FILEHUB_LANE_", "filehub_lane"),  # khoá cổng cfharness (filehub/lane/): gắn MỘT hub — bản nhân bản tạo khoá riêng; `move` giữ
)
KEEP_KINDS: tuple[tuple[str, str], ...] = (
    ("CLAUDE_ACCT_", "claude_account"),
    ("CLAUDE_CODE_OAUTH_TOKEN", "claude_account"),
    ("MCP_", "mcp_static"),
)


def classify(name: str) -> str:
    """Loại secret: channel | oauth (bỏ khi clone) | claude_account | mcp_static | other (luôn ghi)."""
    for pre, kind in CLONE_SKIP_PREFIXES:
        if name.startswith(pre):
            return kind
    for pre, kind in KEEP_KINDS:
        if name == pre or name.startswith(pre):
            return kind
    return "other"


def skipped_in(name: str, mode: str) -> bool:
    return mode == "clone" and classify(name) in ("channel", "oauth")


def plan_names(names: list[str], mode: str) -> dict[str, list[str]]:
    imp = [n for n in names if not skipped_in(n, mode)]
    skip = [n for n in names if skipped_in(n, mode)]
    return {"import": imp, "skip_mode": skip}


# ============================================================================ passphrase (CHỈ trong tiến trình CLI)
def check_passphrase(pw: str, *, for_export: bool) -> str:
    if not isinstance(pw, str) or not pw:
        raise HubError("passphrase rỗng")
    if len(pw) > MAX_PASSPHRASE:
        raise HubError(f"passphrase quá dài (> {MAX_PASSPHRASE} ký tự)")
    if for_export and len(pw) < MIN_PASSPHRASE:
        raise HubError(f"passphrase phải dài ít nhất {MIN_PASSPHRASE} ký tự")
    return pw


def read_passphrase_file(path: str | Path) -> str:
    """Đọc passphrase từ file: phải là file thường (không symlink), của chính user, quyền 0600 (không nhóm/khác đọc được)."""
    p = Path(path).expanduser()
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise HubError(f"không mở được --passphrase-file {p}: {e.strerror or e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HubError(f"--passphrase-file {p} không phải file thường")
        if os.name == "posix":
            if st.st_mode & 0o077:
                raise HubError(f"--passphrase-file {p} có quyền {oct(st.st_mode & 0o777)} — phải là 0600 (chmod 600 {p})")
            if st.st_uid != os.getuid():
                raise HubError(f"--passphrase-file {p} không thuộc user hiện tại")
        if st.st_size > MAX_PASSPHRASE + 2:
            raise HubError(f"--passphrase-file {p} quá lớn")
        raw = os.read(fd, MAX_PASSPHRASE + 3)
    finally:
        os.close(fd)
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HubError(f"--passphrase-file {p} không phải UTF-8") from e
    if txt.endswith("\n"):
        txt = txt[:-1]
    if txt.endswith("\r"):
        txt = txt[:-1]
    return txt


def prompt_passphrase(*, confirm: bool, prompt: str = "Passphrase: ") -> str:
    """Hỏi passphrase trên TTY (không vang). `confirm` → hỏi 2 lần, phải khớp."""
    if not sys.stdin.isatty():
        raise HubError("không chạy trong terminal — dùng --passphrase-file <file 0600>")
    pw = getpass.getpass(prompt)
    if confirm:
        check_passphrase(pw, for_export=True)
        if getpass.getpass("Nhập lại passphrase: ") != pw:
            raise HubError("hai lần nhập passphrase không khớp")
    return pw


# ============================================================================ mật mã
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: Any, n: int, what: str) -> bytes:
    try:
        b = base64.b64decode(str(s), validate=True)
    except (ValueError, TypeError) as e:
        raise HubError(f"manifest: {what} hỏng") from e
    if len(b) != n:
        raise HubError(f"manifest: {what} sai độ dài")
    return b


def _derive(pw: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=KEY_LEN, n=n, r=r, p=p).derive(pw.encode("utf-8"))


def aad_for(manifest: dict[str, Any]) -> bytes:
    """AAD = nhãn + id + sha256(manifest chuẩn hoá, bỏ mục files của secrets.enc và sections.secrets)."""
    m = copy.deepcopy(manifest)
    m["files"] = [f for f in (m.get("files") or []) if not (isinstance(f, dict) and f.get("path") == MEMBER)]
    if isinstance(m.get("sections"), dict):
        m["sections"].pop(SECTION, None)
    canon = json.dumps(m, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return AAD_LABEL + str(manifest.get("id") or "").encode("utf-8") + b"\n" + hashlib.sha256(canon).hexdigest().encode("ascii")


def check_meta(manifest: dict[str, Any]) -> list[str]:
    """Lỗi chặn của khối `secrets` trong manifest (tham số KDF/cipher) — không cần passphrase."""
    s = manifest.get(SECTION)
    if not isinstance(s, dict):
        return ["manifest: thiếu khối secrets"]
    errs: list[str] = []
    kdf = s.get("kdf") if isinstance(s.get("kdf"), dict) else {}
    if s.get("cipher") != CIPHER or kdf.get("name") != KDF_NAME:
        errs.append("manifest: thuật toán mã hoá secret không hỗ trợ")
        return errs
    n, r, p = kdf.get("n"), kdf.get("r"), kdf.get("p")
    if not (isinstance(n, int) and N_RANGE[0] <= n <= N_RANGE[1] and n & (n - 1) == 0):
        errs.append("manifest: tham số scrypt n ngoài khoảng cho phép")
    if not (isinstance(r, int) and R_RANGE[0] <= r <= R_RANGE[1]) or not (isinstance(p, int) and P_RANGE[0] <= p <= P_RANGE[1]):
        errs.append("manifest: tham số scrypt r/p ngoài khoảng cho phép")
    try:
        _unb64(kdf.get("salt"), SALT_LEN, "salt")
        _unb64(s.get("nonce"), NONCE_LEN, "nonce")
    except HubError as e:
        errs.append(str(e))
    names = s.get("names")
    if not isinstance(names, list) or not all(isinstance(x, str) and NAME_RE.match(x) for x in names):
        errs.append("manifest: danh sách tên secret hỏng")
    return errs


def allowed_payload_path(name: str) -> bool:
    if name in (TOTP_FILE, MCP_TOKENS_FILE, META_FILE):
        return True
    parts = PurePosixPath(name).parts
    return len(parts) == 2 and parts[0] == "secrets" and bool(NAME_RE.match(parts[1])) and name == "/".join(parts)


def _payload_tar(items: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in items:
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = len(data), 0o600, 0
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _read_payload(plain: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as tf:
        for m in tf:
            if not m.isreg() or not allowed_payload_path(m.name) or m.name in out:
                raise HubError(f"secrets.enc chứa phần tử không hợp lệ: {m.name!r}")
            f = tf.extractfile(m)
            out[m.name] = f.read() if f is not None else b""
    return out


def collect(home: Path, *, with_totp: bool = False, with_mcp_tokens: bool = False) -> list[tuple[str, bytes]]:
    """Đọc phần secret của HUB_HOME (file thường, không symlink) theo danh sách trắng."""
    items: list[tuple[str, bytes]] = []

    def _read(p: Path) -> bytes | None:
        try:
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            chunks = []
            while True:
                b = os.read(fd, 1 << 16)
                if not b:
                    break
                chunks.append(b)
            return b"".join(chunks)
        finally:
            os.close(fd)

    sd = home / "secrets"
    names: list[str] = []
    try:
        listing = sorted(os.listdir(sd))
    except OSError:
        listing = []
    for n in listing:
        if not NAME_RE.match(n):  # gồm thư mục con `filehub/` (token thiết bị Không gian số — không bao giờ mang theo)
            continue
        data = _read(sd / n)
        if data:  # secret rỗng không hợp lệ (SecretStore từ chối) — bỏ
            items.append((f"secrets/{n}", data))
            names.append(n)
    meta = _read(sd / ".meta.json")
    if meta is not None and names:
        try:
            d = json.loads(meta.decode("utf-8"))
            d = {k: v for k, v in d.items() if k in names and isinstance(v, dict)} if isinstance(d, dict) else {}
            items.append((META_FILE, json.dumps(d, sort_keys=True).encode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            pass
    if with_totp:
        data = _read(home / TOTP_FILE)
        if data is not None:
            items.append((TOTP_FILE, data))
    if with_mcp_tokens:
        data = _read(home / MCP_TOKENS_FILE)
        if data is not None:
            items.append((MCP_TOKENS_FILE, data))
    return items


def names_of(items: list[tuple[str, bytes]]) -> list[str]:
    return sorted(n.split("/", 1)[1] for n, _ in items if n.startswith("secrets/") and n != META_FILE)


def encrypt(manifest: dict[str, Any], items: list[tuple[str, bytes]], passphrase: str) -> tuple[dict[str, Any], bytes]:
    """Thêm khối `secrets` vào bản sao manifest, mã hoá payload với AAD của manifest đó. Trả (manifest mới chưa có mục files của
    secrets.enc, bản mã)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    check_passphrase(passphrase, for_export=True)
    bad = [n for n, _ in items if not allowed_payload_path(n)]
    if bad:
        raise HubError(f"không mang được {bad[0]!r} trong phần secret (ngoài danh sách trắng)")
    plain = _payload_tar(items)
    if len(plain) > MAX_PLAIN:
        raise HubError("phần secret quá lớn để mang theo gói")
    salt, nonce = os.urandom(SALT_LEN), os.urandom(NONCE_LEN)
    m = copy.deepcopy(manifest)
    names = names_of(items)
    m[SECTION] = {"cipher": CIPHER, "kdf": {"name": KDF_NAME, "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salt": _b64(salt)},
                  "nonce": _b64(nonce), "names": names, "totp": any(n == TOTP_FILE for n, _ in items),
                  "mcp_tokens": any(n == MCP_TOKENS_FILE for n, _ in items)}
    m["missing_secrets"] = sorted(set(m.get("missing_secrets") or []) - set(names))
    key = _derive(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    ct = AESGCM(key).encrypt(nonce, plain, aad_for(m))
    return m, ct


def decrypt(manifest: dict[str, Any], blob: bytes, passphrase: str) -> dict[str, bytes]:
    """Giải mã + kiểm toàn vẹn. Sai passphrase / gói bị tráo → HubError (không phân biệt hai trường hợp — không làm lộ thông tin)."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    errs = check_meta(manifest)
    if errs:
        raise HubError("; ".join(errs))
    check_passphrase(passphrase, for_export=False)
    s = manifest[SECTION]
    kdf = s["kdf"]
    key = _derive(passphrase, _unb64(kdf["salt"], SALT_LEN, "salt"), int(kdf["n"]), int(kdf["r"]), int(kdf["p"]))
    try:
        plain = AESGCM(key).decrypt(_unb64(s["nonce"], NONCE_LEN, "nonce"), blob, aad_for(manifest))
    except InvalidTag:
        raise HubError("passphrase sai hoặc gói đã bị sửa — không giải mã được phần secret", code=-32003) from None
    if len(plain) > MAX_PLAIN:
        raise HubError("phần secret quá lớn")
    payload = _read_payload(plain)
    got = sorted(n.split("/", 1)[1] for n in payload if n.startswith("secrets/") and n != META_FILE)
    if got != sorted(s.get("names") or []):
        raise HubError("secrets.enc không khớp danh sách tên trong manifest")
    return payload


# ============================================================================ ghi vào home đích (nhập)
def apply(home: Path, payload: dict[str, bytes], *, mode: str) -> tuple[dict[str, Any], list[tuple[Path, bytes | None]]]:
    """Ghi secret (0600, thư mục 0700, qua `SecretStore.set_bytes`) theo chế độ; TOTP/token MCP chỉ khi đích CHƯA có (khác → giữ của máy
    này, báo). Trả (báo cáo chỉ TÊN, nhật ký hoàn tác [(đường dẫn, nội dung cũ | None)])."""
    from .secrets import SecretStore

    store = SecretStore(home / "secrets", env_fallback=False)
    undo: list[tuple[Path, bytes | None]] = []
    rep: dict[str, Any] = {"imported": [], "skipped_mode": [], "kept_target": []}
    meta: dict[str, Any] = {}
    if META_FILE in payload:
        try:
            meta = json.loads(payload[META_FILE].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            meta = {}
    meta = meta if isinstance(meta, dict) else {}

    def _prev(p: Path) -> bytes | None:
        try:
            return p.read_bytes() if p.is_file() and not p.is_symlink() else None
        except OSError:
            return None

    try:
        undo.append((store.dir / store.META, _prev(store.dir / store.META)))  # thời điểm đặt — hoàn tác cùng secret
        for name in sorted(payload):
            if not name.startswith("secrets/") or name == META_FILE:
                continue
            n = name.split("/", 1)[1]
            if skipped_in(n, mode):
                rep["skipped_mode"].append(n)
                continue
            undo.append((store.dir / n, _prev(store.dir / n)))
            set_at = (meta.get(n) or {}).get("set_at") if isinstance(meta.get(n), dict) else None
            store.set_bytes(n, payload[name], set_at=set_at if isinstance(set_at, (int, float)) else None)  # tự đăng ký bộ che
            rep["imported"].append(n)
        for rel, key in ((TOTP_FILE, "totp"), (MCP_TOKENS_FILE, "mcp_tokens")):
            if rel not in payload:
                continue
            dst = home / rel
            cur = _prev(dst)
            if cur is not None and cur != payload[rel]:
                rep["kept_target"].append(rel)
                continue
            if dst.is_symlink():
                raise HubError(f"{rel} ở home đích là symlink — không ghi")
            d = dst.parent
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            undo.append((dst, cur))
            _write_private(dst, payload[rel])
            rep[key] = True
    except BaseException:
        rollback(undo)
        raise
    return rep, undo


def _write_private(p: Path, data: bytes) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def rollback(undo: list[tuple[Path, bytes | None]]) -> None:
    """Hoàn tác `apply` (ngược thứ tự). Cố gắng hết sức — không ném."""
    for p, old in reversed(undo):
        try:
            if old is None:
                p.unlink(missing_ok=True)
            else:
                _write_private(p, old)
        except OSError:  # pragma: no cover
            pass


__all__ = ["SECTION", "MEMBER", "classify", "skipped_in", "plan_names", "check_passphrase", "read_passphrase_file", "prompt_passphrase",
           "aad_for", "check_meta", "collect", "encrypt", "decrypt", "apply", "rollback", "allowed_payload_path", "names_of", "NEVER",
           "MIN_PASSPHRASE"]
