# Danh sách tài khoản Claude: đổi bằng tay để chạy tiếp (`feat/claude-accounts`)

Mục tiêu: hub giữ một DANH SÁCH tài khoản Claude (token `claude setup-token`). Người dùng thêm tài khoản bằng luồng
"bấm mở trang đăng nhập → đăng nhập → dán code", rồi bấm "Dùng" để chuyển. Khi tài khoản đang dùng hết hạn mức hoặc lỗi
đăng nhập, người dùng bấm đổi sang tài khoản khác và hàng đợi chạy tiếp ngay.

**KHÔNG tự đổi tài khoản** (đã chốt với người dùng 2026-11-13): xoay vòng tự động để né hạn mức có thể vi phạm điều khoản
Anthropic. Mọi lần đổi do NGƯỜI bấm.

## Cơ chế
- Token mỗi tài khoản: secret store, tên `CLAUDE_ACCT_<ID>` (ID dạng `a_<8 hex>`, viết hoa trong tên secret theo `normalize_name`).
- "Đổi tài khoản" = chép token của tài khoản được chọn vào secret `CLAUDE_CODE_OAUTH_TOKEN` (chỗ cũ, `runtime/claude_cli.py`
  đọc lại mỗi lần spawn) → run mới, takeover, doctor tự theo. Run đang chạy giữ tài khoản cũ.
- Tài khoản đặc biệt `machine` = "Đăng nhập của máy" (`claude auth login` sẵn có): dùng nó = XOÁ secret `CLAUDE_CODE_OAUTH_TOKEN`.
- Con trỏ đang dùng: `store.state_get("claude_account_active")` → `"a_xxxxxxxx" | "machine" | None` (None = chưa cấu hình,
  hành vi như hiện nay). Nếu hash của `CLAUDE_CODE_OAUTH_TOKEN` không khớp tài khoản nào → hiển thị `kind="custom"`.
- Migrate lần đầu: đã có `CLAUDE_CODE_OAUTH_TOKEN` mà chưa có tài khoản nào → tạo tài khoản "Mặc định" từ nó và đặt active.

## Hợp đồng (các luồng làm song song phải giữ đúng)

### RPC (mọi method dưới đây trừ `account.list` là GHI: `HUMAN_ONLY` + bọc audit; audit KHÔNG giữ `token`, `code`)
| Method | Params | Kết quả |
|---|---|---|
| `account.list` | `{}` | `{active, accounts: [Account]}` |
| `account.login_start` | `{label}` | `{login_id, url, expires_at}` |
| `account.login_finish` | `{login_id, code}` | `{account: Account}` |
| `account.login_cancel` | `{login_id}` | `{ok: true}` |
| `account.add_token` | `{label, token}` | `{account: Account}` |
| `account.use` | `{id}` | `{active, previous}` |
| `account.rename` | `{id, label}` | `{account: Account}` |
| `account.rm` | `{id}` | `{ok: true}` (xoá tài khoản đang dùng → `Conflict`; `machine` không xoá được) |
| `account.check` | `{id}` | `{account: Account}` (kiểm `claude auth status` với token đó) |

`Account` = `{id, label, kind: "token"|"machine"|"custom", active: bool, created_at, expires_at, last_used_at,
status: "ok"|"auth_error"|"rate_limited"|"expiring"|"unknown", last_error, rate_limit: {until, utilization}|null,
cost_today_usd}` — KHÔNG BAO GIỜ có token. `expiring` = còn < 14 ngày tới `expires_at`.

`hub.status` thêm `account: {id, label} | null`.

### Event (thêm vào `EVENT_LEVELS`, level `summary`; payload không có token)
- `account.added {id, label}`, `account.removed {id}`, `account.renamed {id, label}`
- `account.switched {from, to, by}` — luồng rate-limit/dispatcher NGHE event này để đưa việc đang chờ chạy lại.

### State key
- `claude_account_active` — luồng lõi ghi.
- `rate_limit_gates` = `{<account_key>: {until, source, run_id, set_at}}` và `rate_limit_by_account` = `{<account_key>: <state rate_limit>}`
  — luồng rate-limit ghi. `account_key` = id active lúc run spawn (hoặc `"_default"` khi `claude_account_active` là None).
  Giữ `rate_limit` và `rate_limit_gate` cũ = bản của tài khoản ĐANG dùng (tương thích `hub.status`, admin, web health).
- `runs.account_id` (cột mới, migration của luồng lõi) — đặt lúc spawn = `claude_account_active`.

### Đăng nhập qua PTY (`src/cfharness/accounts/login.py`)
```python
class LoginHandle:
    url: str
    async def submit(self, code: str, timeout: float = 60) -> str: ...   # trả token sk-ant-oat01-…; lỗi → HubError tiếng Việt
    async def close(self) -> None: ...                                     # kill tiến trình, idempotent
async def start_login(binary: str, env: dict[str, str], timeout: float = 45) -> LoginHandle: ...
```
Chạy `claude setup-token` trong PTY (POSIX: `pty` stdlib; Windows: `pywinpty`, dependency chỉ win32), không mở trình
duyệt trên máy daemon, đọc URL/token bằng regex chịu mã ANSI + xuống dòng. Token không vào log.

## Chia việc
| Luồng | Sở hữu file | Không chạm |
|---|---|---|
| core | `accounts/__init__.py`, `accounts/service.py`, `accounts/rpc.py`, migration `store.py`, `auth.py` (HUMAN_ONLY + `all_mutating`), `events.py`, `hub.py`, `daemon.py` wiring, `runs.py` (chỉ ghi `account_id` + thông báo auth_block nhắc đổi tài khoản), CLI `cfharness account`, `tests/test_accounts.py` | `accounts/login.py`, `dispatch/`, `web/` |
| login | `accounts/login.py`, `pyproject.toml`/`uv.lock` (pywinpty), `tests/test_accounts_login.py`, `tests/fake_claude_setup_token.py` | mọi file khác |
| ratelimit | `dispatch/dispatcher.py`, `auto_resume.py`, `admin_overview.py`, `tests/test_accounts_ratelimit.py` | `runs.py`, `hub.py`, `accounts/` |
| web | `web/app.py` (route), `web/templates/*`, `web/static/*`, `web/extra_pages.py` (health), `tests/test_web_accounts.py` | ngoài `web/` |

## Sau khi đổi tài khoản (luồng ratelimit)
- task `wait_reason=rate_limit_reset` → `not_before=now`, giữ `rate_limit_hits`.
- auto_resume đã hẹn giờ với `cause=rate_limit` → chạy sớm.
- task `blocked` với lý do "claude chưa đăng nhập" → `ready`.
- dispatcher chỉ xét cổng của tài khoản đang dùng.

## Chưa làm
- Tự đổi tài khoản (cố ý không làm).
- Lệnh `/account` trên Telegram/Slack.
- Chưa kiểm: `claude --resume` session của tài khoản A bằng tài khoản B (cần 2 tài khoản thật).
