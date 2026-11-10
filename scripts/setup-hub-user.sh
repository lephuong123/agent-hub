#!/bin/bash
# Tạo một hub cfharness cho một user macOS (mặc định: lamlinh) — docs/dev-notes/huong-dan-tao-hub-lamlinh.md
# Chạy trong Terminal.app của admin (cần TTY để nhập mật khẩu sudo + dán token; không chạy qua `!` của Claude Code):
#   sudo bash scripts/setup-hub-user.sh [user] ["Họ tên"] [cổng web] [cổng mcp]
# Đừng `| tee` ra file: màn hình có token Claude (bước 3) và link đăng ký TOTP (bước 8).
# Chạy lại được (bỏ qua bước đã xong).
set -euo pipefail

U="${1:-lamlinh}"
FULL="${2:-Lam Linh}"
WEB="${3:-8721}"
MCP="${4:-8722}"
DOMAIN="trungtamdhs.com"
VER="${VER:-v0.2.5}"   # cần ≥ v0.2.5 (khoá một daemon/HUB_HOME, enroll không in bí mật TOTP)
H="/Users/$U"
WS="$H/Workspace"
ADMIN="${SUDO_USER:-}"
UPATH="$H/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
as_u() { sudo -u "$U" -H env HOME="$H" USER="$U" LOGNAME="$U" PATH="$UPATH" "$@"; }
cfh()  { as_u "$H/.local/bin/cfharness" "$@"; }

[ "$(id -u)" = 0 ] || { echo "Chạy bằng sudo: sudo bash $0"; exit 1; }
[ -n "$ADMIN" ] && [ "$ADMIN" != root ] || { echo "Chạy bằng 'sudo' từ user admin (không chạy trực tiếp bằng root)"; exit 1; }
cd /

step "1/8 User macOS $U"
if id "$U" >/dev/null 2>&1; then ok "đã có user $U"; else
  PW="$(openssl rand -base64 24)"   # mật khẩu ngẫu nhiên, không lưu: $U không bao giờ đăng nhập màn hình
  sysadminctl -addUser "$U" -fullName "$FULL" -password "$PW" -home "$H" 2>&1 | grep -v '^$' || true
  unset PW
  createhomedir -c -u "$U" >/dev/null 2>&1 || true
  dscl . create "/Users/$U" IsHidden 1      # ẩn khỏi màn hình đăng nhập
  id "$U" >/dev/null || { echo "Tạo user thất bại"; exit 1; }
  ok "đã tạo user $U (ẩn khỏi màn hình đăng nhập)"
fi
chmod 700 "$H"
as_u mkdir -p "$WS"
ok "home 0700, workspace $WS"

step "2/8 Cài uv, claude, cfharness $VER (riêng cho $U)"
[ -x "$H/.local/bin/uv" ] || as_u sh -c 'curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh' >/dev/null
[ -x "$H/.local/bin/claude" ] || as_u bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/dev/null
as_u "$H/.local/bin/uv" tool install --force --python 3.13 "git+https://github.com/lephuong123/agent-hub@$VER" 2>&1 | tail -1
ok "uv $(as_u "$H/.local/bin/uv" --version | awk '{print $2}') · claude $(as_u "$H/.local/bin/claude" --version | awk '{print $1}') · $(cfh --version 2>/dev/null || echo cfharness)"

step "3/8 Token Claude dùng chung (CLAUDE_CODE_OAUTH_TOKEN)"
if cfh secret list 2>/dev/null | grep -q CLAUDE_CODE_OAUTH_TOKEN; then ok "hub đã có token"; else
  echo "Dán token sk-ant-oat01-… (không hiện ra màn hình). Chưa có → nhấn Enter để tạo bằng 'claude setup-token':"
  read -rs TOKEN; echo
  if [ -z "$TOKEN" ]; then
    sudo -u "$ADMIN" -H "/Users/$ADMIN/.local/bin/claude" setup-token
    echo "Dán token vừa in ở trên (sk-ant-oat01-…):"
    read -rs TOKEN; echo
    printf '\033[3J\033[H\033[2J'   # xoá màn hình + scrollback: token vừa hiện ra không nằm lại trong Terminal
  fi
  case "$TOKEN" in sk-ant-*) ;; *) echo "Token không đúng dạng sk-ant-…"; exit 1;; esac
  printf '%s' "$TOKEN" | cfh secret set CLAUDE_CODE_OAUTH_TOKEN --value-stdin >/dev/null
  unset TOKEN
  ok "đã lưu token vào secret store của $U (nhớ lưu token ở nơi an toàn để dùng cho user sau)"
fi

step "4/8 Cấu hình hub"
cfh config set mcp_server.http_port "$MCP" >/dev/null
cfh config set workspace.allowed_roots "[\"$WS\"]" >/dev/null
cfh config set workspace.confine.root "$WS" >/dev/null
cfh config set systems.roots "[\"$WS\"]" >/dev/null
cfh config set web.public_origin "https://$U.$DOMAIN" >/dev/null
cfh config set web.client_ip_header cf-connecting-ip >/dev/null
cfh config set admin_export.enabled true >/dev/null
ok "cổng MCP $MCP · agent chỉ làm trong $WS · web public https://$U.$DOMAIN"

step "5/8 Tự chạy khi máy khởi động (LaunchDaemon: daemon + web :$WEB)"
cfh daemon stop >/dev/null 2>&1 || true
cfh daemon uninstall-service >/dev/null 2>&1 || true
CFHARNESS_HOME="$(mktemp -d)" "$H/.local/bin/cfharness" admin install-daemon --user "$U" --web-port "$WEB" --write
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $U.$DOMAIN" "http://127.0.0.1:$WEB/login" || true)"
  [ "$code" = 200 ] || [ "${code:0:1}" = 3 ] && break; sleep 1
done
[ "${code:-000}" = 200 ] || [ "${code:0:1}" = 3 ] && ok "web :$WEB trả $code" || warn "web :$WEB chưa lên (mã $code) — xem $H/.cfharness/logs/web.out"

step "6/8 Kiểm tra"
cfh doctor || warn "doctor có mục chưa xanh (xem ở trên)"
cfh doctor multi || warn "doctor multi có mục chưa xanh (xem ở trên)"

step "7/8 Thư mục tổng quan admin (/Users/Shared/cfharness/status, quyền 3773)"
dseditgroup -o read cfharness-admin >/dev/null 2>&1 || dseditgroup -o create -r "cfharness admin overview" cfharness-admin
dseditgroup -o edit -a "$ADMIN" -t user cfharness-admin
mkdir -p /Users/Shared/cfharness/status
chown "$ADMIN:cfharness-admin" /Users/Shared/cfharness /Users/Shared/cfharness/status
chmod 755 /Users/Shared/cfharness
chmod 3773 /Users/Shared/cfharness/status
ok "nhóm cfharness-admin (+$ADMIN), thư mục status 3773"

step "8/8 Mã đăng nhập web (TOTP) cho $U"
cfh web totp enroll

printf '\n\033[1;32mXONG.\033[0m Gửi link /enroll ở trên cho %s qua kênh riêng.\n' "$FULL"
echo "Sau khi đăng ký: https://$U.$DOMAIN → nhập mã 6 số."
echo "Log: sudo -u $U tail -f $H/.cfharness/logs/daemon.out   ·   Khởi động lại web: sudo launchctl kickstart -k system/com.cfharness.web.$U"
