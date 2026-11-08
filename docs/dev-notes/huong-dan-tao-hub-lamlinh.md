# Hướng dẫn tạo hub cfharness cho user LamLinh trên Mac mini

Người làm: admin (`phuonglq`), qua Terminal/SSH trên Mac mini, có quyền `sudo`. LamLinh **không** đăng nhập màn hình máy;
LamLinh chỉ dùng **link web public** + mã 6 số (Google Authenticator / 1Password / Authy).

Thông số dùng trong tài liệu (đổi nếu cần — giữ nhất quán):

| Mục | Giá trị |
|---|---|
| User macOS | `lamlinh` (họ tên hiển thị "Lam Linh") |
| HUB_HOME | `/Users/lamlinh/.cfharness` |
| Workspace agent được phép (W) | `/Users/lamlinh/Workspace` (muốn giam vào đúng một dự án thì dùng `/Users/lamlinh/Workspace/<du-an>`) |
| Cổng web / MCP | `8721` / `8722` (user thứ k → `87k1` / `87k2`; không trùng hub khác) |
| Tên miền public | `lamlinh.trungtamdhs.com` (miền `trungtamdhs.com` trên Cloudflare) |

Tài liệu gốc: `plan-multi-instance.md` (quyết định), `multi-instance.md` (P1), `confine.md` (P2), `web-public.md` (P3),
`admin-overview.md` (Q6).

> Cần cfharness **v0.2.4** trở lên (bản có đa instance: giam workspace, web public + TOTP, LaunchDaemon, tổng quan admin).

---

## Bước 1 — Tạo user macOS `lamlinh`

```bash
sudo sysadminctl -addUser lamlinh -fullName "Lam Linh" -password -     # nhập mật khẩu (giữ kín, chỉ admin biết)
sudo chmod 700 /Users/lamlinh                                          # user khác không vào được home
sudo -u lamlinh mkdir -p /Users/lamlinh/Workspace
```

Không cần bật Remote Login/SSH cho `lamlinh` (LamLinh không SSH). Admin thao tác bằng `sudo -u lamlinh -i …`.

## Bước 2 — Cài `uv`, `claude`, `cfharness` trong user `lamlinh`

Mỗi user có bản cài riêng (cập nhật hub này không ảnh hưởng hub khác).

```bash
sudo -u lamlinh -i bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u lamlinh -i bash -lc 'curl -fsSL https://claude.ai/install.sh | bash'
sudo -u lamlinh -i bash -lc '~/.local/bin/claude --version'           # phải có phiên bản (máy admin đang 2.1.319)
```

Cài cfharness (cùng cách cài với mọi user — `cfharness update` chạy được về sau):

```bash
sudo -u lamlinh -i bash -lc 'uv tool install --python 3.13 "git+https://github.com/lephuong123/agent-hub@v0.2.4"'
```

Kiểm tra: `sudo -u lamlinh -i cfharness --help | head -3`.

> Admin cũng cập nhật hub của mình lên v0.2.4 (`cfharness update`) để dùng `cfharness admin overview` (bước 9).

## Bước 3 — Tài khoản Claude dùng chung (không cần đăng nhập màn hình)

Token tạo **một lần** cho cả máy (nếu đã có từ hub khác thì dùng lại token đó):

```bash
claude setup-token                       # chạy ở user admin, đăng nhập bằng tài khoản Claude chung → in token sk-ant-oat01-…
```

Nạp token vào secret store của hub LamLinh (nhập ẩn, không vào lịch sử shell):

```bash
sudo -u lamlinh -i cfharness secret set CLAUDE_CODE_OAUTH_TOKEN
sudo -u lamlinh -i cfharness doctor      # dòng đăng nhập claude phải xanh
```

⚠️ Mọi hub dùng chung token này; agent đọc được nó qua biến môi trường. Nghi lộ → `claude setup-token` mới rồi `secret set` lại trên
**mọi** hub.

## Bước 4 — Cấu hình hub (cổng, giam workspace)

```bash
L='sudo -u lamlinh -i cfharness'
$L config set mcp_server.http_port 8722
$L config set workspace.allowed_roots '["/Users/lamlinh/Workspace"]'
$L config set workspace.confine.root /Users/lamlinh/Workspace          # agent CHỈ đọc/ghi trong đây (+ thư mục công cụ npm/uv/git…)
$L config set systems.roots '["/Users/lamlinh/Workspace"]'
$L config set admin_export.enabled true                                # cho admin thấy trên "Tổng quan máy" (bước 9)
$L doctor multi                                                        # mọi mục phải OK
```

Ghi chú:
- `workspace.confine.root` không được là `/Users/lamlinh` (home) — phải là thư mục con.
- Giam áp dụng cả agent "trust full". Muốn cho agent đọc thêm thư mục nào ngoài W → thêm vào `workspace.confine.read_allow`.

## Bước 5 — Web public: tunnel Cloudflare

Máy đã có `cloudflared` (`/opt/homebrew/bin/cloudflared`). Dùng **một tunnel chung** cho mọi user, mỗi user một hostname.

```bash
# (chỉ lần đầu cho cả máy)
cloudflared tunnel login
cloudflared tunnel create cfh-mini

# mỗi user
cloudflared tunnel route dns cfh-mini lamlinh.trungtamdhs.com
```

Thêm vào `~/.cloudflared/config.yml` của admin (giữ các hostname của user khác, `http_status:404` luôn ở CUỐI):

```yaml
tunnel: cfh-mini
credentials-file: /Users/phuonglq/.cloudflared/<uuid>.json
ingress:
  - hostname: lamlinh.trungtamdhs.com
    service: http://127.0.0.1:8721
  # - hostname: <user-khác>.trungtamdhs.com ...
  - service: http_status:404
```

```bash
sudo cloudflared service install          # lần đầu; lần sau đổi config → sudo launchctl kickstart -k system/com.cloudflare.cloudflared
```

Khai báo origin public cho hub:

```bash
$L config set web.public_origin https://lamlinh.trungtamdhs.com
$L config set web.client_ip_header cf-connecting-ip
```

**Khuyến nghị mạnh:** bật **Cloudflare Access** (Zero Trust → Access → Applications) cho `lamlinh.trungtamdhs.com`, chính sách
"Emails = email của LamLinh". Khi đó có 2 lớp: email OTP của Cloudflare + mã TOTP của hub.

⛔ Không bao giờ trỏ tunnel vào cổng MCP `8722`.

## Bước 6 — Tự chạy khi máy khởi động (không cần ai đăng nhập)

Hub đang chạy tạm do các lệnh trên tự khởi động → dừng trước, rồi cài LaunchDaemon:

```bash
$L daemon stop
$L daemon uninstall-service 2>/dev/null || true                       # gỡ LaunchAgent cũ nếu có

# xem trước plist + lệnh (không cần root)
$L admin install-daemon --user lamlinh --web-port 8721

# ghi thật (root)
sudo /Users/lamlinh/.local/bin/cfharness admin install-daemon --user lamlinh --web-port 8721 --write
```

Kiểm tra:

```bash
sudo launchctl print system/com.cfharness.daemon.lamlinh | grep -E 'state|pid'
sudo launchctl print system/com.cfharness.web.lamlinh   | grep -E 'state|pid'
$L daemon status
curl -sI -H 'Host: lamlinh.trungtamdhs.com' http://127.0.0.1:8721/login | head -1     # 200 hoặc 30x, KHÔNG phải 421
```

## Bước 7 — Đăng ký mã đăng nhập (TOTP) cho LamLinh

```bash
$L web totp enroll                # in link https://lamlinh.trungtamdhs.com/enroll?t=…  (hạn 24 h)
```

Gửi link cho LamLinh qua **kênh riêng** (tin nhắn trực tiếp, không dán vào nhóm). LamLinh:
1. Mở link → bấm **"Hiện mã"** (chỉ hiện một lần) → quét QR/nhập bí mật vào app Authenticator.
2. Nhập mã 6 số đang hiện trong app → vào thẳng web.
3. Từ đó: mở `https://lamlinh.trungtamdhs.com` → nhập mã 6 số.

```bash
$L web totp status                # phải báo đã kích hoạt
```

Nếu link báo "đã dùng" mà LamLinh chưa dùng → có người mở trước: chạy lại `enroll` (bí mật cũ bị thay khi bí mật mới được kích hoạt).

## Bước 8 — (Tuỳ chọn) Telegram / Slack riêng cho LamLinh

Mỗi hub **một bot Telegram / một app Slack riêng** (dùng chung bot giữa hai hub sẽ hỏng: Telegram 409, Slack chia event).
- Telegram: LamLinh tạo bot ở @BotFather → gửi token cho admin qua kênh riêng → `$L notify telegram setup` (làm theo lời nhắc ghép nối).
- Slack: app riêng với slash command riêng, ví dụ `/cfh-lamlinh` (`notify.slack.slash_command`), làm qua web → Cài đặt → Slack 2 chiều.

## Bước 9 — Thư mục tổng quan admin (chỉ làm một lần cho cả máy)

```bash
sudo dseditgroup -o create -r "cfharness admin overview" cfharness-admin 2>/dev/null || true
sudo dseditgroup -o edit -a phuonglq -t user cfharness-admin
sudo mkdir -p /Users/Shared/cfharness/status
sudo chown phuonglq:cfharness-admin /Users/Shared/cfharness/status
sudo chmod 3773 /Users/Shared/cfharness/status      # user không đọc/ghi đè file của nhau, KHÔNG cần thêm lamlinh vào nhóm
```

Sau ~1 phút: `ls -l /Users/Shared/cfharness/status` có `lamlinh.json` (`-rw-r----- lamlinh cfharness-admin`), và
`cfharness admin overview` (ở user admin) hiện dòng `lamlinh … đang chạy`.

---

## Kiểm tra cuối (checklist)

- [ ] `$L doctor` và `$L doctor multi` không còn lỗi.
- [ ] Từ điện thoại 4G (ngoài mạng nhà): `https://lamlinh.trungtamdhs.com` → (Cloudflare Access) → trang nhập mã → vào được.
- [ ] Nhập sai mã 5 lần → bị khoá 15 phút (đúng như thiết kế; admin gỡ bằng `sudo launchctl kickstart -k system/com.cfharness.web.lamlinh`).
- [ ] Tạo một agent thử, chạy prompt `cat /Users/phuonglq/.zshrc` → bị chặn; `ls /Users/lamlinh/Workspace` → chạy được.
- [ ] Khởi động lại Mac mini (không đăng nhập ai) → web của LamLinh vẫn vào được.
- [ ] `cfharness admin overview` (admin) thấy hub `lamlinh`.

## Vận hành

| Việc | Lệnh |
|---|---|
| Xem log | `sudo -u lamlinh tail -f /Users/lamlinh/.cfharness/logs/daemon.out` (web: `logs/web.out`) |
| Khởi động lại hub | `sudo launchctl kickstart -k system/com.cfharness.daemon.lamlinh` (web: `…web.lamlinh`) |
| Dừng khẩn mọi agent | `$L estop on` (hoặc nút ESTOP trên web) |
| Cấp lại mã đăng nhập (mất điện thoại) | `$L web totp disable` → `$L web totp enroll` → `sudo launchctl kickstart -k system/com.cfharness.web.lamlinh` (huỷ mọi phiên) |
| Cập nhật cfharness | `$L update --yes` |

## Thu hồi / xoá LamLinh

```bash
$L web totp disable
sudo /Users/lamlinh/.local/bin/cfharness admin uninstall-daemon --user lamlinh --write
# xoá hostname lamlinh khỏi ~/.cloudflared/config.yml + Cloudflare Access + DNS, rồi kickstart cloudflared
sudo rm -f /Users/Shared/cfharness/status/lamlinh.json
# (tuỳ) xoá user và toàn bộ dữ liệu — KHÔNG hoàn tác được, sao lưu /Users/lamlinh trước:
# sudo sysadminctl -deleteUser lamlinh
```
Bot Telegram/Slack của LamLinh: thu hồi token ở BotFather / gỡ app Slack.
