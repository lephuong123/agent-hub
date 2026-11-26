#!/bin/bash
# Tạo một hub cfharness cho một user macOS (mặc định: lamlinh) — docs/dev-notes/huong-dan-tao-hub-lamlinh.md
# Chạy trong Terminal.app của admin (cần TTY để nhập mật khẩu sudo + dán token; không chạy qua `!` của Claude Code):
#   sudo bash scripts/setup-hub-user.sh [user] ["Họ tên"] [cổng web] [cổng mcp]
#   Tên miền con khác tên user: sudo SUB=admin bash scripts/setup-hub-user.sh hubadmin "Hub Admin" 8723 8724
#   Miền khác trungtamdhs.com:  sudo SUB=admin DOMAIN=cchchn.com bash scripts/setup-hub-user.sh cchcadmin "Admin CCHC" 8725 8726
#   Làm lại hub đã có từ đầu (cất dữ liệu cũ, không xoá): thêm RESET=1
#   Kèm thư mục chung Không gian số: thêm KGS=https://trungtamai.cchchn.com — qua CỔNG CFHARNESS (Không gian số chạy cùng máy): dán khoá
#   cổng của tài khoản (Không gian số › Hồ sơ › Cổng cfharness) là xong — mọi thư mục/workspace khoá được cấp dùng được ngay.
#   (Đồng bộ 2 chiều cũ đã bỏ; KGS_MODE/KGS_SCOPE/KGS_FOLDER/KGS_WS của bản cũ vẫn nhận nhưng không còn tác dụng.)
#   Hub ĐÃ có, chỉ nối Không gian số: sudo ONLY_KGS=1 KGS=https://trungtamai.cchchn.com bash scripts/setup-hub-user.sh vanha
#   Kèm connector claude.ai (Google Drive…) cho agent: thêm CONNECTORS="Google Drive" CONNECTOR_AGENTS="dieu-phoi agent-a" (hoặc "all") — đăng nhập
#   `claude auth login` (tài khoản claude.ai) cho user hub; token setup-token KHÔNG nạp connector (docs/dev-notes/manager-connectors.md).
#   Hub ĐÃ có, chỉ nâng cfharness + nối connector (không đụng token/tunnel/TOTP):
#     sudo ONLY_CONNECTORS=1 CONNECTORS=auto bash scripts/setup-hub-user.sh cchcadmin      # mọi agent tự dùng connector tài khoản đã Connect
#     sudo ONLY_CONNECTORS=1 CONNECTORS="Google Drive" CONNECTOR_AGENTS="dieu-phoi" bash scripts/setup-hub-user.sh cchcadmin
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
VER="${VER:-v0.2.40}"   # cần ≥ v0.2.5 (khoá một daemon/HUB_HOME, enroll không in bí mật TOTP)
CONNECTORS="${CONNECTORS:-}"              # connector claude.ai, phân cách dấu phẩy (vd "Google Drive,Claude Docs"); "auto" = config
                                          # connectors.auto: mọi agent tự nạp mọi connector tài khoản đã Connect; trống = bỏ qua
CONNECTOR_AGENTS="${CONNECTOR_AGENTS:-}"  # agent bật connector, cách nhau dấu cách, hoặc "all" = mọi agent; GỘP với connector sẵn có; điều phối viên → manager capabilities (cần --mcp trusted)
ONLY_CONNECTORS="${ONLY_CONNECTORS:-}"    # 1 = hub đã có: chỉ bước 2 (nâng cfharness) + nối connector, rồi dừng
ONLY_KGS="${ONLY_KGS:-}"                  # 1 = hub đã có: chỉ bước 10 (nối Không gian số qua cổng cfharness), rồi dừng
KGS="${KGS:-}"          # URL Không gian số để nối thư mục chung (trống = bỏ qua bước 10)
# Cổng cfharness (docs/dev-notes/plan-filehub-lane.md): nhập khoá cổng (người dùng tạo ở Không gian số › Hồ sơ › Cổng cfharness cho tài
# khoản của mình — script hỏi bằng getpass, không in); hub tự thêm mọi gốc khoá được cấp. Chỉ khi Không gian số chạy CÙNG máy (loopback).
# KGS_MODE / KGS_SCOPE / KGS_FOLDER / KGS_WS: biến của đồng bộ 2 chiều cũ (đã bỏ) — vẫn nhận (script cũ không lỗi), bỏ qua.
KGS_MODE="${KGS_MODE:-lane}"
[ "$KGS_MODE" = lane ] || echo "  (KGS_MODE=$KGS_MODE: đồng bộ 2 chiều đã bỏ — dùng cổng cfharness)"
KGS_LANE_URL="${KGS_LANE_URL:-}"     # trống = thử http://127.0.0.1:8223 (qltm) rồi 8233 (cchc)
KGS_LANE_NAME="${KGS_LANE_NAME:-}"   # trống = theo Không gian số trả lời (qltm/cchc)
# filehub có từ v0.2.10 nên tag mặc định đã đủ cho bước 10 (KGS)
TUNNEL_CFG="${TUNNEL_CFG:-}"   # mặc định /Users/<admin>/.cloudflared/cfharness-config.yml
CERT_ZONE="${CERT_ZONE:-cchchn.com}"   # zone của ~/.cloudflared/cert.pem: chỉ miền này tạo DNS tự động được
H="/Users/$U"
WS="$H/Workspace"
ADMIN="${SUDO_USER:-}"
UPATH="$H/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
as_u() { sudo -u "$U" -H env HOME="$H" USER="$U" LOGNAME="$U" PATH="$UPATH" "$@"; }
cfh()  { as_u "$H/.local/bin/cfharness" "$@"; }

# Connector claude.ai: phiên `claude auth login` của user hub (KHÔNG kèm token chung), bật connector cho agent, kiểm bằng doctor --probe
connectors_step() {
  step "Connector claude.ai: $CONNECTORS"
  if [ -z "$CONNECTORS" ]; then ok "bỏ qua (không đặt CONNECTORS=…)"; return 0; fi
  claude_login() { as_u env -u CLAUDE_CODE_OAUTH_TOKEN "$H/.local/bin/claude" auth "$@"; }
  if claude_login status --json 2>/dev/null | grep -q '"authMethod": *"claude.ai"'; then
    ok "user $U đã đăng nhập claude.ai: $(claude_login status --json 2>/dev/null | sed -n 's/.*"email": *"\([^"]*\)".*/\1/p')"
  else
    echo "Đăng nhập tài khoản claude.ai mà agent sẽ dùng connector (mở link in ra, đăng nhập, dán mã lại đây):"
    claude_login login || warn "claude auth login lỗi"
    claude_login status --json 2>/dev/null | grep -q '"authMethod": *"claude.ai"' && ok "đã đăng nhập claude.ai" \
      || { warn "CHƯA có phiên claude.ai cho $U — connector sẽ không nạp (chạy lại script)"; return 0; }
  fi
  echo "  → Trên claude.ai của tài khoản này: Settings › Connectors › bấm Connect cho: $CONNECTORS (chọn đúng tài khoản Google…)."
  for _ in $(seq 1 30); do cfh daemon status >/dev/null 2>&1 && break; sleep 1; done
  if [ "$CONNECTORS" = auto ]; then
    cfh config set connectors.auto true >/dev/null && ok "connectors.auto = true: mọi agent (chưa đặt connector riêng) tự dùng connector tài khoản đã Connect" \
      || warn "không đặt được connectors.auto (cần cfharness ≥ v0.2.23)"
    cfh hub doctor --probe 2>/dev/null | grep -i "connector" || true
    echo "  Chạy thử một run: event connector.unavailable{*, none_connected} = tài khoản chưa Connect connector nào trên claude.ai."
    return 0
  fi
  MGR_ID="$(cfh manager get --json 2>/dev/null | sed -n 's/.*"agent_id": *"\([^"]*\)".*/\1/p' | head -1)"
  # "all" = mọi agent (chưa lưu trữ). GỘP với connector agent đang có (agent edit --connectors GHI ĐÈ cả danh sách)
  # → dòng "<id>\t<danh sách đã gộp>"; agent không đổi gì thì bỏ qua
  plan="$(cfh agent list --json 2>/dev/null | CONNECTORS="$CONNECTORS" WANT="$CONNECTOR_AGENTS" python3 -c '
import json, os, re, sys
slug = lambda n: re.sub(r"[^A-Za-z0-9_-]", "_", n.strip().removeprefix("claude.ai ").removeprefix("claude_ai_"))
add = [x.strip() for x in os.environ["CONNECTORS"].split(",") if x.strip()]
want = os.environ["WANT"].split()
agents = {a["id"]: a for a in json.load(sys.stdin)}
ids = list(agents) if want == ["all"] else want
for i in ids:
    if i not in agents:
        print(f"{i}\t!missing"); continue
    cur = list((agents[i].get("tool_policy") or {}).get("connectors") or [])
    have = {slug(c) for c in cur}
    new = cur + [c for c in add if slug(c) not in have]
    print(f"{i}\t" + (",".join(new) if len(new) > len(cur) else "!same"))
')" || { warn "không đọc được danh sách agent"; plan=""; }
  while IFS=$'\t' read -r a conns; do
    [ -n "$a" ] || continue
    case "$conns" in
      '!missing') warn "không có agent $a"; continue ;;
      '!same') ok "agent $a: đã có $CONNECTORS"; continue ;;
    esac
    if [ -n "$MGR_ID" ] && [ "$a" = "$MGR_ID" ]; then
      cur="$(cfh config get manager.capabilities 2>/dev/null | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin).get("connectors") or []))' 2>/dev/null || true)"
      cfh manager capabilities set --mcp trusted --connectors "${cur:+$cur,}$CONNECTORS" >/dev/null && ok "điều phối viên $a: connector ${cur:+$cur,}$CONNECTORS" \
        || warn "không bật được connector cho điều phối viên $a (cần profile hermes + guard/sandbox bật — cfharness manager capabilities)"
    else
      cfh agent edit "$a" --connectors "$conns" >/dev/null && ok "agent $a: connector $conns" || warn "không bật được connector cho agent $a"
    fi
  done <<< "$plan"
  [ -n "$CONNECTOR_AGENTS" ] || warn "chưa đặt CONNECTOR_AGENTS — bật sau: sudo -u $U -i cfharness agent edit <id> --connectors \"$CONNECTORS\""
  cfh hub doctor --probe 2>/dev/null | grep -i "connector" || true
  echo "  Chạy thử một run của agent: event connector.unavailable = connector chưa Connect trên claude.ai; connector.auth_fallback = chưa đăng nhập."
}

# Bước 10: nối thư mục chung Không gian số qua cổng cfharness (dùng chung cho chạy đủ và ONLY_KGS=1)
kgs_step() {
  step "10/11 Thư mục chung Không gian số${KGS:+: $KGS}"
  if [ -z "$KGS" ]; then ok "bỏ qua (không đặt KGS=<URL Không gian số>)"; return 0; fi
  for _ in $(seq 1 30); do cfh filehub status >/dev/null 2>&1 && break; sleep 1; done
  cfh config set filehub.server "$KGS" >/dev/null  # URL web — để dựng link "Mở trên Không gian số"
  if cfh filehub lane test ${KGS_LANE_NAME:+--server "$KGS_LANE_NAME"} >/dev/null 2>&1; then ok "cổng cfharness đã nối"; else
    echo "Dán khoá cổng cfharness của tài khoản $U (Không gian số › Hồ sơ › Cổng cfharness — chọn đúng thư mục/workspace cấp cho hub):"
    # bỏ trống KGS_LANE_URL / KGS_LANE_NAME → hub thử 8223 rồi 8233 và lấy tên cổng theo Không gian số trả lời
    cfh filehub lane setup ${KGS_LANE_NAME:+--server "$KGS_LANE_NAME"} ${KGS_LANE_URL:+--url "$KGS_LANE_URL"} \
      || warn "chưa nối được cổng — chạy lại script hoặc dùng web › Cài đặt › Không gian số (chỉ cần dán khoá)"
  fi
  # hub TỰ thêm mọi thư mục/workspace khoá được cấp (setup + mỗi lần kiểm tra); thư mục đồng bộ cũ cùng gốc tự chuyển sang cổng
  cfh filehub lane test ${KGS_LANE_NAME:+--server "$KGS_LANE_NAME"} || true
  cfh filehub status || true
}

[ "$(id -u)" = 0 ] || { echo "Chạy bằng sudo: sudo bash $0"; exit 1; }
[ -n "$ADMIN" ] && [ "$ADMIN" != root ] || { echo "Chạy bằng 'sudo' từ user admin (không chạy trực tiếp bằng root)"; exit 1; }
cd /

if [ "$ONLY_KGS" = 1 ]; then
  id "$U" >/dev/null 2>&1 && [ -x "$H/.local/bin/cfharness" ] || { echo "Chưa có hub của $U — chạy script đủ (không ONLY_KGS) trước"; exit 1; }
  [ -n "$KGS" ] || { echo "ONLY_KGS=1 cần KGS=<URL Không gian số>"; exit 1; }
  kgs_step
  printf '\n\033[1;32mXONG.\033[0m Thư mục chung: sudo -u %s -i cfharness filehub status\n' "$U"
  exit 0
fi

if [ "${RESET:-}" = 1 ] && id "$U" >/dev/null 2>&1; then
  step "0/11 RESET hub của $U (dừng dịch vụ, cất dữ liệu cũ sang $H/.reset-…)"
  printf 'Làm lại hub của %s từ đầu? Dữ liệu hub, Workspace, session Claude cũ sẽ được CẤT sang %s/.reset-<giờ> (không xoá). Gõ "reset" để tiếp tục: ' "$U" "$H"
  read -r ans; [ "$ans" = reset ] || { echo "Huỷ"; exit 1; }
  for k in web daemon; do
    launchctl bootout "system/com.cfharness.$k.$U" 2>/dev/null || true
    rm -f "/Library/LaunchDaemons/com.cfharness.$k.$U.plist"
  done
  pkill -u "$U" 2>/dev/null || true; sleep 2; pkill -9 -u "$U" 2>/dev/null || true
  BK="$H/.reset-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BK"; chown "$U" "$BK"; chmod 700 "$BK"
  for p in .cfharness .claude .claude.json .config/cfharness Workspace; do
    if [ -e "$H/$p" ]; then mkdir -p "$BK/$(dirname "$p")"; mv "$H/$p" "$BK/$p"; fi
  done
  rm -rf "/Users/Shared/cfharness/status/$U" "/Users/Shared/cfharness/status/$U.json"
  ok "đã dừng hub cũ, cất dữ liệu ở $BK (xoá hẳn khi chắc chắn: sudo rm -rf $BK)"
fi

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

if [ "$ONLY_CONNECTORS" = 1 ]; then
  launchctl kickstart -k "system/com.cfharness.daemon.$U" 2>/dev/null || { cfh daemon stop >/dev/null 2>&1 || true; cfh daemon start >/dev/null 2>&1 || true; }
  launchctl kickstart -k "system/com.cfharness.web.$U" 2>/dev/null || true
  ok "đã khởi động lại daemon + web của $U (bản mới)"
  connectors_step
  printf '\n\033[1;32mXONG.\033[0m\n'
  exit 0
fi

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

kgs_step

connectors_step

step "11/11 Mã đăng nhập web (TOTP) cho $U"
cfh web totp enroll

printf '\n\033[1;32mXONG.\033[0m Gửi link /enroll ở trên cho %s qua kênh riêng.\n' "$FULL"
echo "Sau khi đăng ký: https://$HOST → nhập mã 6 số."
if [ -n "$KGS" ]; then echo "Thư mục chung: sudo -u $U -i cfharness filehub status"; fi
echo "Log: sudo -u $U tail -f $H/.cfharness/logs/daemon.out   ·   Khởi động lại web: sudo launchctl kickstart -k system/com.cfharness.web.$U"
