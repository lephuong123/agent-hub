#!/bin/bash
# Tạo một hub cfharness cho một user macOS (mặc định: lamlinh) — docs/dev-notes/huong-dan-tao-hub-lamlinh.md
# Chạy trong Terminal.app của admin (cần TTY để nhập mật khẩu sudo + dán token; không chạy qua `!` của Claude Code):
#   sudo bash scripts/setup-hub-user.sh [user] ["Họ tên"] [cổng web] [cổng mcp]
#   Tên miền con khác tên user: sudo SUB=admin bash scripts/setup-hub-user.sh hubadmin "Hub Admin" 8723 8724
#   Miền khác trungtamdhs.com:  sudo SUB=admin DOMAIN=cchchn.com bash scripts/setup-hub-user.sh cchcadmin "Admin CCHC" 8725 8726
#   Kèm thư mục chung Không gian số: thêm KGS=https://trungtamai.cchchn.com — mặc định đồng bộ TOÀN BỘ workspace của domain
#   (KGS_WS=tên workspace để chọn sẵn; tài khoản chủ domain phải là Workspace Admin). Một thư mục: KGS_SCOPE=folder KGS_FOLDER=tên.
# MỘT lệnh làm hết: user, cài đặt, token, cấu hình, LaunchDaemon, tunnel + DNS, thư mục chung (nếu có KGS), TOTP.
# Đừng `| tee` ra file: màn hình có token Claude (bước 3) và link đăng ký TOTP (bước 8).
# Chạy lại được (bỏ qua bước đã xong).
set -euo pipefail

U="${1:-lamlinh}"
FULL="${2:-Lam Linh}"
WEB="${3:-8721}"
MCP="${4:-8722}"
DOMAIN="${DOMAIN:-trungtamdhs.com}"   # miền gốc (zone Cloudflare): https://$SUB.$DOMAIN
SUB="${SUB:-$U}"        # tên miền con: https://$SUB.$DOMAIN
HOST="$SUB.$DOMAIN"
VER="${VER:-v0.2.18}"   # cần ≥ v0.2.5 (khoá một daemon/HUB_HOME, enroll không in bí mật TOTP)
KGS="${KGS:-}"          # URL Không gian số để nối thư mục chung (trống = bỏ qua bước 10)
KGS_SCOPE="${KGS_SCOPE:-workspace}"   # workspace | folder
KGS_FOLDER="${KGS_FOLDER:-cfharness}"   # chỉ dùng khi KGS_SCOPE=folder
KGS_WS="${KGS_WS:-}"                  # gợi ý tên workspace (KGS_SCOPE=workspace)
# filehub có từ v0.2.10 nên tag mặc định đã đủ cho bước 10 (KGS)
TUNNEL_CFG="${TUNNEL_CFG:-}"   # mặc định /Users/<admin>/.cloudflared/cfharness-config.yml
CERT_ZONE="${CERT_ZONE:-cchchn.com}"   # zone của ~/.cloudflared/cert.pem: chỉ miền này tạo DNS tự động được
SYNC_BIN=/usr/local/bin/tang-thu-sync-headless
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

step "1/11 User macOS $U"
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

step "2/11 Cài uv, claude, cfharness $VER (riêng cho $U)"
[ -x "$H/.local/bin/uv" ] || as_u sh -c 'curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh' >/dev/null
[ -x "$H/.local/bin/claude" ] || as_u bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/dev/null
as_u "$H/.local/bin/uv" tool install --force --python 3.13 "git+https://github.com/lephuong123/agent-hub@$VER" 2>&1 | tail -1
ok "uv $(as_u "$H/.local/bin/uv" --version | awk '{print $2}') · claude $(as_u "$H/.local/bin/claude" --version | awk '{print $1}') · $(cfh --version 2>/dev/null || echo cfharness)"

step "3/11 Token Claude dùng chung (CLAUDE_CODE_OAUTH_TOKEN)"
# Token `claude setup-token` dài ~108 ký tự và thường bị Terminal XUỐNG DÒNG khi in → dán vào `read` một dòng sẽ CẮT CỤT
# (vẫn bắt đầu bằng sk-ant- nên trước đây lọt kiểm tra → run lỗi 401 "OAuth access token is invalid"). Đọc nhiều dòng, bỏ
# khoảng trắng, rồi GỌI THỬ claude bằng token trước khi lưu.
TOKFILE="$H/.cfharness/secrets/CLAUDE_CODE_OAUTH_TOKEN"
token_ok() {  # $1 = token → claude trả lời được không (một câu rất ngắn, chạy dưới user $U)
  local out
  out="$(cd "$WS" && as_u env CLAUDE_CODE_OAUTH_TOKEN="$1" "$H/.local/bin/claude" -p --max-turns 1 --setting-sources "" "Reply with just: OK" 2>&1 || true)"
  case "$out" in *OK*) return 0;; *) printf '  claude: %s\n' "$(printf '%s' "$out" | tail -1 | cut -c1-160)"; return 1;; esac
}
read_token() {  # đọc tới khi đủ dài (ghép các dòng bị xuống dòng), bỏ mọi khoảng trắng
  local line t=""
  while IFS= read -rs line; do
    t+="${line//[[:space:]]/}"
    [ -z "$line" ] && [ -z "$t" ] && break   # Enter trên dòng trống đầu tiên = chưa có token
    [ ${#t} -ge 100 ] && break
  done
  echo
  TOKEN="$t"
}
if [ -s "$TOKFILE" ] && token_ok "$(cat "$TOKFILE")"; then ok "hub đã có token (đã gọi thử claude: OK)"; else
  [ -s "$TOKFILE" ] && warn "token đang lưu trong hub KHÔNG dùng được — nhập lại"
  echo "Dán token sk-ant-oat01-… (không hiện ra màn hình; dán cả khi bị xuống dòng). Chưa có → nhấn Enter để tạo bằng 'claude setup-token':"
  read_token
  if [ -z "$TOKEN" ]; then
    sudo -u "$ADMIN" -H "/Users/$ADMIN/.local/bin/claude" setup-token
    echo "Dán token vừa in ở trên (sk-ant-oat01-…):"
    read_token
    printf '\033[3J\033[H\033[2J'   # xoá màn hình + scrollback: token vừa hiện ra không nằm lại trong Terminal
  fi
  case "$TOKEN" in sk-ant-*) ;; *) echo "Token không đúng dạng sk-ant-…"; exit 1;; esac
  token_ok "$TOKEN" || { echo "Token bị Claude từ chối (dán thiếu? hết hạn?) — chạy lại script"; exit 1; }
  printf '%s' "$TOKEN" | cfh secret set CLAUDE_CODE_OAUTH_TOKEN --value-stdin >/dev/null
  unset TOKEN
  ok "đã lưu token — đã gọi thử claude: OK. Nhớ cất token ở nơi an toàn cho user sau"
fi

step "4/11 Cấu hình hub"
cfh config set mcp_server.http_port "$MCP" >/dev/null
cfh config set workspace.allowed_roots "[\"$WS\"]" >/dev/null
cfh config set workspace.confine.root "$WS" >/dev/null
cfh config set systems.roots "[\"$WS\"]" >/dev/null
cfh config set web.public_origin "https://$HOST" >/dev/null
cfh config set web.client_ip_header cf-connecting-ip >/dev/null
cfh config set admin_export.enabled true >/dev/null
ok "cổng MCP $MCP · agent chỉ làm trong $WS · web public https://$HOST"

step "5/11 Tự chạy khi máy khởi động (LaunchDaemon: daemon + web :$WEB)"
cfh daemon stop >/dev/null 2>&1 || true
cfh daemon uninstall-service >/dev/null 2>&1 || true
CFHARNESS_HOME="$(mktemp -d)" "$H/.local/bin/cfharness" admin install-daemon --user "$U" --web-port "$WEB" --write
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $HOST" "http://127.0.0.1:$WEB/login" || true)"
  [ "$code" = 200 ] || [ "${code:0:1}" = 3 ] && break; sleep 1
done
[ "${code:-000}" = 200 ] || [ "${code:0:1}" = 3 ] && ok "web :$WEB trả $code" || warn "web :$WEB chưa lên (mã $code) — xem $H/.cfharness/logs/web.out"

step "6/11 Điều phối viên (agent dieu-phoi, mẫu manager)"
MGR="${MGR:-dieu-phoi}"
if cfh manager get --json 2>/dev/null | grep -Eq '"agent_id": *"'; then ok "hub đã có điều phối viên"; else
  cfh agent show "$MGR" >/dev/null 2>&1 || cfh agent create "$MGR" --template manager --name "Điều phối viên" >/dev/null
  cfh manager set "$MGR" >/dev/null
  ok "đã tạo $MGR và chỉ định làm điều phối viên (khung quyền strict: Read/Grep/Glob)"
fi

step "7/11 Kiểm tra"
cfh doctor || warn "doctor có mục chưa xanh (xem ở trên)"
cfh doctor multi || warn "doctor multi có mục chưa xanh (xem ở trên)"

step "8/11 Thư mục tổng quan admin (/Users/Shared/cfharness/status, quyền 3773)"
dseditgroup -o read cfharness-admin >/dev/null 2>&1 || dseditgroup -o create -r "cfharness admin overview" cfharness-admin
dseditgroup -o edit -a "$ADMIN" -t user cfharness-admin
mkdir -p /Users/Shared/cfharness/status
chown "$ADMIN:cfharness-admin" /Users/Shared/cfharness /Users/Shared/cfharness/status
chmod 755 /Users/Shared/cfharness
chmod 3773 /Users/Shared/cfharness/status
ok "nhóm cfharness-admin (+$ADMIN), thư mục status 3773"

step "9/11 Tunnel Cloudflare + DNS cho https://$HOST"
TUNNEL_CFG="${TUNNEL_CFG:-/Users/$ADMIN/.cloudflared/cfharness-config.yml}"
as_admin() { sudo -u "$ADMIN" -H "$@"; }
if [ ! -f "$TUNNEL_CFG" ]; then warn "không thấy $TUNNEL_CFG — bỏ qua tunnel (xem huong-dan-tao-hub-lamlinh.md bước 5)"; else
  TID="$(awk '/^tunnel:/{print $2}' "$TUNNEL_CFG")"
  if grep -q "hostname: $HOST\$" "$TUNNEL_CFG"; then ok "ingress $HOST đã có"; else
    as_admin cp "$TUNNEL_CFG" "$TUNNEL_CFG.bak-$(date +%Y%m%d-%H%M%S)"
    as_admin python3 - "$TUNNEL_CFG" "$HOST" "$WEB" <<'EOF'
import sys
p, host, port = sys.argv[1:]
s = open(p).read(); tail = "  - service: http_status:404"
assert s.count(tail) == 1, "cfharness-config.yml phải có đúng một dòng http_status:404 cuối"
open(p, "w").write(s.replace(tail, f"  - hostname: {host}\n    service: http://127.0.0.1:{port}\n{tail}"))
EOF
    ok "đã thêm ingress $HOST → 127.0.0.1:$WEB"
  fi
  as_admin /opt/homebrew/bin/cloudflared tunnel --config "$TUNNEL_CFG" ingress validate >/dev/null && ok "ingress hợp lệ" || warn "ingress KHÔNG hợp lệ — xem $TUNNEL_CFG"
  launchctl kickstart -k "gui/$(id -u "$ADMIN")/com.cfharness.tunnel" 2>/dev/null && ok "đã khởi động lại tunnel" || warn "không khởi động lại được com.cfharness.tunnel"
  if [ -n "$(dig +short "$HOST" | head -1)" ]; then ok "DNS $HOST đã có"
  elif [ "$DOMAIN" = "$CERT_ZONE" ]; then
    (cd "/Users/$ADMIN" && as_admin /opt/homebrew/bin/cloudflared tunnel --config "$TUNNEL_CFG" route dns "$TID" "$HOST" 2>&1 | tail -1) || warn "không tạo được DNS $HOST — tạo tay: CNAME $SUB → $TID.cfargotunnel.com"
  else
    warn "DNS $HOST chưa có — tạo trong Cloudflare dashboard (zone $DOMAIN): CNAME $SUB → $TID.cfargotunnel.com (Proxied)"
  fi
fi

step "10/11 Thư mục chung Không gian số"
if [ -z "$KGS" ]; then ok "bỏ qua (không đặt KGS=<URL Không gian số>)"; else
  if [ -x "/Users/$ADMIN/.local/bin/tang-thu-sync-headless" ]; then install -m 0755 "/Users/$ADMIN/.local/bin/tang-thu-sync-headless" "$SYNC_BIN"; fi
  [ -x "$SYNC_BIN" ] || { warn "chưa có $SYNC_BIN (build tang-thu-sync: cargo build -p sync-core --release --bin tang-thu-sync-headless) — bỏ qua"; SKIP_KGS=1; }
  if [ -z "${SKIP_KGS:-}" ] && [ "$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$KGS/api/sync/roots/00000000-0000-0000-0000-000000000000/presence")" = 404 ]; then
    warn "$KGS chưa có bản Không gian số hỗ trợ thư mục chung — deploy rồi chạy lại script"; SKIP_KGS=1
  fi
  if [ -z "${SKIP_KGS:-}" ]; then
    for _ in $(seq 1 30); do cfh filehub status >/dev/null 2>&1 && break; sleep 1; done
    FH_JSON="$(cfh filehub status --json 2>/dev/null || true)"
    if [[ "$FH_JSON" == *'"local_path"'* ]]; then ok "đã nối: $(cfh filehub status | head -1)"; else
      as_u mkdir -p "$WS/KhongGianSo"
      if [ "$KGS_SCOPE" = folder ]; then KGS_NAME="$KGS_FOLDER"
        echo "Mở link dưới bằng trình duyệt TRÊN MÁY NÀY → đăng nhập $KGS → chọn chỗ đặt thư mục (nơi mình là Chủ sở hữu) → Cho phép."
      else KGS_NAME="$KGS_WS"
        echo "Mở link dưới bằng trình duyệt TRÊN MÁY NÀY → đăng nhập $KGS bằng tài khoản chủ domain (Workspace Admin) → chọn \"Toàn bộ workspace\" → Cho phép."
        echo "(Hoặc bỏ qua bước này và đăng nhập sau ở web hub › Cài đặt › Không gian số.)"
      fi
      echo "Script chờ tới khi bấm xong."
      cfh filehub setup --server "$KGS" --binary "$SYNC_BIN" --scope "$KGS_SCOPE" ${KGS_NAME:+--name "$KGS_NAME"} --local-parent "$WS/KhongGianSo" --no-browser \
        || warn "chưa nối được — chạy lại script (bước đã xong được bỏ qua)"
    fi
  fi
fi

step "11/11 Mã đăng nhập web (TOTP) cho $U"
cfh web totp enroll

printf '\n\033[1;32mXONG.\033[0m Gửi link /enroll ở trên cho %s qua kênh riêng.\n' "$FULL"
echo "Sau khi đăng ký: https://$HOST → nhập mã 6 số."
if [ -n "$KGS" ]; then echo "Thư mục chung: sudo -u $U -i cfharness filehub status"; fi
echo "Log: sudo -u $U tail -f $H/.cfharness/logs/daemon.out   ·   Khởi động lại web: sudo launchctl kickstart -k system/com.cfharness.web.$U"
