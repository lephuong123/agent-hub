# Web public qua tunnel TLS + đăng nhập TOTP + LaunchDaemon + tài khoản Claude dùng chung

Nguồn: `docs/dev-notes/plan-multi-instance.md` §4/P3 và §6b (chủ repo chốt 2026-11-07: mỗi người một user macOS, KHÔNG đăng nhập màn hình
Mac mini, chỉ điều khiển qua link web public; một tài khoản Claude dùng chung). Test: `tests/test_web_public.py` (21 test, không mạng,
không `claude`, không launchctl/sudo thật).

## 1. Đã làm

| Phần | File | Ghi chú |
|---|---|---|
| `web.public_origin` | `web/public.py`, `web/app.py` (`WebState.public_origin/hosts/origins/is_public`), `config.py` (`DEFAULTS["web"]`, `validate_setting`) | Chỉ `https://host[:cổng]` (không path/IP/`*.localhost`). Thêm ĐÚNG Host đó (+`host:443`) vào danh sách Host hợp lệ và origin vào danh sách Origin (POST). Host khác vẫn 421. Bind VẪN loopback (`require_loopback` không đổi) |
| Cookie / HSTS | `web/app.py` `_login_ok`, `GuardMiddleware` | Request tới host public → cookie `Secure` + `Max-Age = web.session_max_age_s`; header `Strict-Transport-Security`. Host cục bộ `cfharness.localhost` giữ như cũ |
| Phiên | `web/security.py` `WebAuth(idle_s, max_s)` | `web.session_max_age_s` (mặc định 12 h), `web.session_idle_s` (1 h). Khoá phiên `k` ngoài cookie, CSRF, CSP giữ nguyên |
| TOTP | `web/totp.py` | RFC 6238 bằng `hmac` stdlib: SHA-1, 6 số, 30 s, lệch ±1 bước; chống dùng lại (`last_step`). File `HUB_HOME/auth/totp.json` + `totp_enroll.json` (0600, thư mục 0700 — đã nằm trong `denyRead`/`denyWrite` của sandbox + `permissions.deny`) |
| Đăng ký | `cfharness web totp enroll [--ttl S] [--activate] [--show-uri]`, `status`, `disable` (`web/cli.py`); trang `/enroll` (`templates/enroll.html`) | In link đăng ký MỘT LẦN (URI `otpauth://` chứa bí mật CHỈ in khi `--show-uri`) (`<public_origin>/enroll?t=…`, chỉ lưu băm). GET chỉ hiện nút (bộ xem trước link của chat không tiêu link/không thấy bí mật); POST "Hiện mã" hiện bí mật ĐÚNG MỘT lần; nhập mã đúng → kích hoạt + vào phiên. Mã cũ vẫn dùng tới khi mã mới được kích hoạt (trừ `--activate`) |
| Đăng nhập | `/login` (`templates/login.html`) | Ô mã 6 số khi đã bật TOTP. Link một lần cũ vẫn chạy (cả trên host public). Trên host public không hiện hướng dẫn `cfharness web --open` |
| Chống dò | `web/public.py` `LoginLimiter`, `client_ip` | Theo IP (5 sai/15 phút → khoá 15 phút), TOÀN CỤC mọi IP (10 sai/15 phút → khoá mọi người 15 phút), trần ngày (50 sai/24 h → khoá 24 h). Cấu hình `web.login.*`. Đang khoá thì mã ĐÚNG cũng bị từ chối (429). Audit `web.login_failed` / `web.login_locked` / `web.login` / `web.totp_confirmed` (kênh `web`), `web.totp_enrolled|disabled` (kênh `cli`) |
| IP thật | `client_ip` | Header proxy CHỈ tin khi có `public_origin` VÀ peer là loopback (tunnel chạy trên máy). `web.client_ip_header`: `x-forwarded-for` (mặc định, lấy phần tử CUỐI — do proxy gần nhất thêm) / `cf-connecting-ip` (chỉ khi chắc chắn là Cloudflare) / `none` |
| Bảo vệ cấu hình | `guard/integrity.py` (`SECURITY_KEYS += "web"`), `web/settings_schema.py` (chỉ `READONLY`) | `config.set` ∈ HUMAN_ONLY, không ở `MANAGER_ALLOWED`; agent gõ `cfharness config set …`/`web …` = forbidden; sửa tay `config.yaml` nhánh `web` ngoài RPC → bị khôi phục |
| `--no-link` | `web/cli.py` | Chạy web làm dịch vụ: không in link đăng nhập một lần ra stdout (stdout vào `logs/` — agent đọc được) |
| LaunchDaemon (Q3) | `systems/autostart.py` `LaunchDaemonPlan`, `systems/cli.py` `cfharness admin install-daemon|uninstall-daemon --user U [--home H] [--web-port N] [--program P] [--write]` | `/Library/LaunchDaemons/com.cfharness.daemon.<U>.plist` (+ `com.cfharness.web.<U>.plist` khi `--web-port`): `UserName`, `InitGroups`, env `CFHARNESS_HOME/HOME/USER/PATH`, `KeepAlive` (daemon: chỉ khi crash; web: luôn), `Umask 077`, log `HUB_HOME/logs/{daemon,web}.out`. Mặc định CHỈ IN plist + lệnh sudo; `--write` cần root. `admin` ∈ `HUB_HUMAN_ONLY_CLI` |
| Claude dùng chung | `runtime/claude_cli.py` (`OAUTH_TOKEN_SECRET`, `oauth_token`, `child_env(oauth_token=)`, `ClaudeCliRuntime(secrets=)`), `make_runtime(cfg, secrets)`, judge/curation/graph_extract/second_opinion, `doctor`, `tui/takeover.takeover_env` | Secret `CLAUDE_CODE_OAUTH_TOKEN` trong secret store → đặt vào env của MỌI tiến trình `claude` (run, oneshot, `auth status`, doctor, takeover). Ngoại lệ DUY NHẤT với `env_strip`; ghi đè giá trị cùng tên từ env daemon và `env_extra` của run. Không bao giờ vào argv/DB/plist; giá trị bị che theo giá trị (`SecretStore.preload` + `register_secret_values`, và mẫu `sk-ant-…` của redact.py) |

## 2. Runbook (admin, qua SSH vào Mac mini)

Giả sử user macOS `an`, cổng web `8711`, tên miền `an.example.com`.

```bash
# 0. user + cài cfharness trong user đó (plan P0)
sudo sysadminctl -addUser an -fullName "An" -password -    # nhập mật khẩu
sudo -u an -i bash -lc 'uv tool install git+https://github.com/<repo>@vX.Y.Z'

# 1. Claude dùng chung, không GUI/Keychain: admin chạy MỘT lần (trên máy có trình duyệt) rồi dán token cho từng hub
claude setup-token                                          # in token sk-ant-oat01-…
sudo -u an -i cfharness secret set CLAUDE_CODE_OAUTH_TOKEN  # nhập qua getpass (không vào argv/history)
sudo -u an -i cfharness doctor                              # "đăng nhập claude" phải xanh

# 2. Web public
sudo -u an -i cfharness config set web.public_origin https://an.example.com
sudo -u an -i cfharness config set web.session_max_age_s 43200   # tuỳ chọn

# 3. Tự khởi động không cần đăng nhập màn hình (daemon + web)
sudo cfharness admin install-daemon --user an --web-port 8711          # xem plist + lệnh
sudo cfharness admin install-daemon --user an --web-port 8711 --write  # ghi /Library/LaunchDaemons + bootstrap
sudo launchctl print system/com.cfharness.daemon.an | head

# 4. Tunnel (chọn một, mục 3) trỏ https://an.example.com → http://127.0.0.1:8711

# 5. Đăng ký TOTP cho An
sudo -u an -i cfharness web totp enroll       # gửi link /enroll?t=… cho An qua kênh riêng (không dán vào nhóm chat)
sudo -u an -i cfharness web totp status
```

Thu hồi: `sudo -u an -i cfharness web totp disable` + khởi động lại job web (`sudo launchctl kickstart -k system/com.cfharness.web.an`)
để huỷ cả phiên đang mở và mọi khoá tạm. Gỡ dịch vụ: `sudo cfharness admin uninstall-daemon --user an --write`.

## 3. Tunnel (code trung lập — chỉ cần proxy về loopback và giữ Host gốc)

Yêu cầu chung: tunnel chạy TRÊN Mac mini, proxy tới `http://127.0.0.1:<cổng web>`, **giữ nguyên Host header** (Host phải bằng host của
`web.public_origin`, nếu không web trả 421), thêm `X-Forwarded-For`. Tuyệt đối không trỏ tunnel vào cổng MCP HTTP.

**Cloudflare Tunnel** (tên miền riêng trên Cloudflare):
```bash
brew install cloudflared
cloudflared tunnel login && cloudflared tunnel create cfh-an
cloudflared tunnel route dns cfh-an an.example.com
# ~/.cloudflared/config.yml (của user chạy cloudflared):
#   tunnel: cfh-an
#   credentials-file: /Users/<admin>/.cloudflared/<uuid>.json
#   ingress:
#     - hostname: an.example.com
#       service: http://127.0.0.1:8711
#     - service: http_status:404
sudo cloudflared service install       # LaunchDaemon của cloudflared
sudo -u an -i cfharness config set web.client_ip_header cf-connecting-ip   # tuỳ chọn: Cloudflare luôn ghi đè header này
```
cloudflared mặc định giữ Host gốc (đừng đặt `httpHostHeader` khác `an.example.com`). Một tunnel phục vụ nhiều user bằng nhiều
`hostname` trong `ingress`. **Khuyến nghị mạnh**: bật thêm Cloudflare Access (email OTP / SSO) cho hostname — một lớp xác thực
nữa trước TOTP (mục 5).

**Tailscale Funnel** (tên `*.ts.net`, không cần tên miền):
```bash
sudo -u an -i tailscale funnel --bg --https=443 http://127.0.0.1:8711
sudo -u an -i cfharness config set web.public_origin https://<máy>.<tailnet>.ts.net
```
Mỗi máy chỉ có một tên `*.ts.net` → nhiều user dùng cổng khác nhau (`--https=8443`, `--https=10000`, origin `https://<máy>.<tailnet>.ts.net:8443`).
**Cần kiểm chứng trên máy thật** (chưa làm trong luồng này): Funnel giữ Host gốc — nếu web trả 421, xem log `logs/web.out`.
Không đặt `web.client_ip_header cf-connecting-ip` với Funnel (client tự gửi được header này).

## 4. MCP HTTP — KHÔNG public
`cfharness mcp serve --http` vẫn chỉ bind 127.0.0.1 + bearer token; không thêm Host/Origin public, không đưa qua tunnel. Dùng từ xa: SSH
tunnel (`ssh -L 8712:127.0.0.1:8712 an@mini`). Lý do: token `control` = toàn quyền người, không có OAuth/giới hạn tốc độ như web.

## 5. Rủi ro còn lại / giới hạn
- **Agent đọc được env của chính tiến trình**: `CLAUDE_CODE_OAUTH_TOKEN` nằm trong env của `claude` nên Bash của agent (`env`,
  `/proc`-tương đương, `ps eww` cùng user) đọc được. Sandbox chặn đọc `secrets/` nhưng KHÔNG che được env. Token này là token của tài
  khoản Claude DÙNG CHUNG cho mọi hub → lộ một nơi = lộ mọi nơi. Giảm thiểu: allowlist mạng của sandbox (`guard.sandbox.allowed_domains`)
  để agent khó gửi token ra ngoài; theo dõi dùng bất thường ở console Anthropic; xoay token (`claude setup-token` mới + `secret set`
  trên từng hub) khi nghi lộ. Dòng che theo giá trị chặn token lọt vào transcript/DB/log/Slack/Telegram nếu agent in nó ra.
- **TOTP là yếu tố duy nhất**: 6 số, ±1 bước → mỗi lần đoán ~3·10⁻⁶. Trần ngày 50 lần sai → ~1,5·10⁻⁴/ngày nếu kẻ tấn công dò liên tục
  không ai để ý (khoảng 5 %/năm). Vì vậy: theo dõi audit `web.login_failed`/`web.login_locked`, và nên bật Cloudflare Access (hoặc
  Tailscale chỉ tailnet thay vì Funnel). Kẻ dò cũng làm người dùng thật bị khoá (DoS) — admin gỡ bằng khởi động lại job web.
- Khoá tạm và phiên chỉ trong RAM của tiến trình web: khởi động lại web = mất phiên + mất khoá (cố ý: là cách "mở khoá").
- `web.public_origin` đọc qua RPC `config.get` lúc `cfharness web` khởi động → đổi cấu hình phải khởi động lại job web.
- Link đăng ký: ai mở và bấm "Hiện mã" trước sẽ có bí mật — gửi qua kênh riêng; người dùng thật thấy "đã dùng" thì báo admin cấp lại.
  Lệnh `enroll` mặc định KHÔNG in URI `otpauth://` (bí mật hay bị chép vào chat/log qua scrollback hoặc `tee`); `--show-uri` để in
  khi admin trao tận tay (admin thấy bí mật — chấp nhận, admin vốn có quyền root trên máy).
- LaunchDaemon: chưa kiểm thật trên Mac mini (cần root) — test chỉ dựng plist + runner giả. Không dùng Keychain (`claude` qua token);
  thứ khác cần Keychain trong phiên không GUI (vd MCP lưu credential vào Keychain) có thể không chạy.
- Khi đã có LaunchAgent cũ (`cfharness daemon install-service`) cho cùng HUB_HOME: gỡ trước (`cfharness daemon uninstall-service`) để
  không có hai launchd cùng giữ một daemon.
