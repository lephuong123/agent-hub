# Plan — nhiều hệ cfharness trên một Mac mini (mỗi người một workspace, ra lệnh từ ngoài)

Trạng thái: **DỰ THẢO — chờ chủ repo duyệt**. Ngày: 2026-11-07. Nguồn: khảo sát code (3 luồng: đa instance,
cô lập quyền, kênh vào từ ngoài), trích `file:line` theo `main` @ b066b38.

## 1. Mục tiêu

- N người dùng chung 1 Mac mini, mỗi người có **hub cfharness riêng** (daemon + web + MCP + bot).
- Mỗi hub quản lý **đúng một workspace W** (vd `~/Workspace/<dự-án>`); agent của hub **chỉ đọc + ghi trong W**
  (cộng toolchain cần thiết), không thấy dự án/secret/hub của người khác.
- Mỗi người **ra lệnh từ ngoài**: điện thoại (Telegram/Slack), laptop (web, Claude Code qua MCP), dịch vụ khác (CI).

Không mục tiêu: một hub đa người thuê (multi-tenant trong 1 daemon) — đi ngược nguyên tắc "1 người, 1 máy" (Q-11) và
toàn bộ mô hình người/agent của `auth.py`.

## 2. Hiện trạng — kết luận khảo sát

### 2.1 Chạy nhiều hub: đã gần được
Đã tách theo `CFHARNESS_HOME`: DB, config, secret, pidfile (`paths.py:65-123`), socket `HUB_HOME/hub.sock` (fallback
`/tmp/cfharness-<uid>-<hash(home)>.sock`, `paths.py:79-91`), token người `<socket>.auth` (`auth.py:223`), nhãn launchd
`com.cfharness.daemon.<sha1(home)[:8]>` (`systems/autostart.py:62-70`), update helper chỉ khởi động lại hub của nó.

Va chạm còn lại:

| # | Tài nguyên | Chỗ | Mức |
|---|---|---|---|
| C1 | Web và MCP HTTP cùng mặc định cổng **8765** | `web/cli.py:20`, `mcp_server/cli.py:93`, `config.py:264` | Cao |
| C2 | Cookie `ahub_web` trên cùng host `cfharness.localhost` (cookie không tách theo cổng) → đăng nhập hub B đá phiên hub A | `web/security.py:5-7,39` | Trung bình |
| C3 | Callback OAuth MCP (CLI) cố định `127.0.0.1:33418` | `mcp_client/oauth_cli.py:20` | Thấp |
| C4 | `uv tool install --force` ghi đè venv dùng chung của **một user OS** → hub khác cùng user chạy code lẫn phiên bản | `updater.py:200`, `update_helper.py:113` | Cao (nếu chung user) |
| C5 | Đăng nhập `claude` (OAuth, `~/.claude*`) + hoá đơn + rate limit dùng chung theo user OS | AGENTS.md (không đặt `CLAUDE_CONFIG_DIR`) | Cao (nếu chung user) |
| C6 | `workspace.allowed_roots` mặc định `~/Workspace` cho mọi hub | `config.py:538`, `workspace.py:60-70` | Cao |
| C7 | Một bot Telegram/app Slack cho 2 hub → 409 `getUpdates` / Socket Mode chia event cho 1 kết nối bất kỳ | `chat/bot.py:154,165`, `chat/slack/bot.py:342` | Cao nếu dùng chung |
| C8 | `/cfh` trùng khi 2 app Slack cùng workspace | `notify.slack.slash_command` (đã cấu hình được) | Thấp |

### 2.2 Cô lập quyền: chặn GHI tốt, chặn ĐỌC gần như không có
- **Ghi**: sandbox OS chỉ cho cwd + tmp + allowWrite; deny thêm vùng hub/`~/.ssh`/rc/LaunchAgents (`guard/runtime.py:337-425`).
  Write/Edit ngoài workspace → `destructive/write_outside_workspace` — **người duyệt được**, không forbidden (`classifier.py:816-852`).
- **Đọc**: `denyRead` chỉ gồm token/secret/runtime/auth của CHÍNH hub, credential git, và home **mặc định** khác
  (`runtime.py:377-392`). Bash/Read đọc được mọi thứ khác trong `$HOME`: dự án người khác, `.env`, và — nếu chung user —
  `secrets/`, `auth/web.json`, `mcp_server/tokens.json`, `agents/*/runtime/mcp.json` của hub khác (home không mặc định).
- **Socket**: sandbox chặn Unix socket (trừ allowlist) → agent không gọi được socket hub khác. Nhưng token đọc được ở trên
  vẫn dùng được qua MCP HTTP (nếu hub kia mở), và daemon B coi tiến trình ngoài cây run của B mang token B là NGƯỜI.
- **Quyền file**: chỉ `secrets/`, `auth/`, token, socket là riêng tư; `HUB_HOME` 0755, `hub.db`/`logs/`/`runs/` 0644
  (đã kiểm trên máy này) → user OS khác cùng nhóm `staff` đọc được transcript nếu `$HOME` cho đi qua.
- `trust_full` tắt sandbox hoàn toàn; env `CFHARNESS_EXTRA_WORKSPACE_ROOTS` (chỉ cho test) vẫn được đọc ở đường chạy thật.
- Fallback socket `/tmp/...` tên đoán được, client không kiểm chủ socket trước khi gửi `_auth`.

### 2.3 Ra lệnh từ ngoài
| Kênh | Từ xa hôm nay? | Ghi chú |
|---|---|---|
| Telegram | **Có** (long polling, đi ra, qua NAT) | 1 bot/hub, ghép nối `/start <mã>` + allowlist |
| Slack Socket Mode | **Có** (WebSocket đi ra) | 1 app/hub, slash command riêng (`/cfh-an`…) |
| Web | Chỉ loopback (`web/security.py:66`) | Qua **SSH tunnel** chạy ngay; `tailscale serve`/reverse proxy bị chặn Host (421) + Origin (403) |
| MCP HTTP | Chỉ loopback + bearer | Qua SSH tunnel cho Claude Code; claude.ai connector KHÔNG dùng được (không OAuth) |
| CLI qua SSH | **Có** | Mạnh nhất (cô lập theo user OS) |
| Webhook/API cho dịch vụ | **Không có** | Thay bằng SSH forced-command hoặc MCP qua tunnel |

## 3. Kiến trúc đề xuất

```
Mac mini
├── user OS "an"    ── ~/.cfharness (0700) ── daemon, web :8711, MCP :8712, bot TG/Slack riêng, claude login riêng
│                      workspace.confine.root = ~/Workspace/du-an-A   (agent chỉ đọc/ghi ở đây)
├── user OS "binh"  ── ~/.cfharness (0700) ── daemon, web :8721, MCP :8722, …
│                      workspace.confine.root = ~/Workspace/du-an-B
└── (admin) phuonglq — tạo user, cấp cổng, theo dõi tài nguyên; KHÔNG chạy hub của người khác

Ngoài vào:
  điện thoại ──► Telegram bot / Slack app của người đó (đi ra từ daemon, không mở cổng)
  laptop     ──► Tailscale SSH ──► ssh -L 8711:127.0.0.1:8711 an@mini  → http://cfharness.localhost:8711
                                     (Claude Code: MCP http://127.0.0.1:8712/mcp + token control)
  CI/dịch vụ ──► ssh key + forced command (`cfharness run …` giới hạn) vào đúng user
```

**Quyết định cốt lõi: mỗi người = một user macOS.** Đây là cách DUY NHẤT tách được đăng nhập Claude/hoá đơn (C5),
venv/update (C4), LaunchAgents, và để quyền file 0600/0700 thực sự có nghĩa. Chung một user OS với nhiều `CFHARNESS_HOME`
chỉ chấp nhận cho MỘT người chạy nhiều hub của chính mình.

Hai lớp cô lập:
1. **Giữa người với người**: user OS (+ HUB_HOME 0700, umask 0077 — mục P1).
2. **Agent trong workspace W**: sandbox + classifier của hub (mục P2) — cần cả khi mỗi người một user, vì agent của An
   vẫn đọc được `~/Documents`, `~/Downloads`… của chính An.

## 4. Các phase

### P0 — Triển khai không sửa code (làm ngay, ~0.5 ngày/người)
1. Tạo user macOS cho từng người (`sysadminctl -addUser`), bật "Remote Login" chỉ cho các user đó; cài Tailscale trên mini.
2. Trong mỗi user: cài `cfharness` (`uv tool install`), `claude login` bằng tài khoản của người đó.
3. Bảng cấp cổng: user thứ k → web `87k1`, MCP `87k2` (`cfharness config set mcp_server.http_port …`, `cfharness web --port …`).
4. `cfharness config set workspace.allowed_roots '["~/Workspace/<dự-án>"]'` và `systems.roots` tương tự.
5. Mỗi người tạo bot Telegram riêng (BotFather) và/hoặc app Slack riêng (wizard web, slash command riêng) → `notify telegram setup`.
6. `chmod 700 ~` và `chmod -R go-rwx ~/.cfharness` (vá tay trước khi có P1).
7. Bật autostart daemon (launchd theo user — lưu ý: LaunchAgent `gui/<uid>` chỉ chạy khi user có phiên GUI; cần
   kiểm chứng, có thể phải dùng LaunchDaemon với `UserName` — xem Q3).

Rủi ro còn lại sau P0: agent vẫn ĐỌC được mọi thứ trong home của chính người đó; Write ngoài W vẫn duyệt được.

### P1 — Đa instance sạch (code nhỏ, ~1–2 ngày)
| Việc | File | Test |
|---|---|---|
| `paths.ensure`: chmod HUB_HOME 0700; daemon đặt `umask 0o077` khi khởi động | `paths.py`, `daemon.py` | `test_multi_instance.py`: mode file mới tạo |
| Fallback socket: dùng `$TMPDIR` (riêng theo user trên macOS) hoặc thư mục `/tmp/cfharness-<uid>/` 0700 + kiểm chủ; client `lstat` socket, đòi `st_uid == getuid()` trước khi gửi `_auth` | `paths.py`, `transport.py`/`rpc.py` | socket chủ khác → từ chối, không lộ token |
| MCP HTTP mặc định cổng khác web (vd 8766) + kiểm cổng bận báo lỗi rõ như web | `mcp_server/cli.py`, `http.py` | cổng bận → lỗi tiếng Việt |
| Cookie web có hậu tố theo hub (`ahub_web_<hash(home)[:6]>`) — hoặc mặc định `--host-name <user>.cfharness.localhost` | `web/security.py` | 2 hub cùng host không đá phiên nhau |
| Callback OAuth CLI: cổng 0 (OS chọn) hoặc theo config | `mcp_client/oauth_cli.py` | — |
| `CFHARNESS_EXTRA_WORKSPACE_ROOTS` chỉ đọc khi `CFHARNESS_TESTING=1` | `workspace.py`, `guard/profiles.py` | env đặt ngoài test → bị bỏ qua |
| Lệnh `cfharness doctor multi` (đọc): cổng, quyền file, bot trùng token (băm), roots, phiên bản | `cli.py` mới | — |

### P2 — Giam agent trong workspace W (lõi, ~3–5 ngày + spike)
**Spike trước (bắt buộc, claude thật, ghi vào `hardening-SEC.md` §1):** sandbox của `claude` phiên bản hiện dùng có hỗ trợ
"deny đọc `$HOME` nhưng cho phép lại W + toolchain" (`filesystem.allowRead` bên trong `denyRead`) không. Kết quả quyết định
phương án A hay B dưới đây.

1. **Config mới `workspace.confine`** (chỉ người; thêm vào `integrity.SECURITY_KEYS`, `HUMAN_ONLY`, `SAFE_CONFIG` KHÔNG chứa):
   ```yaml
   workspace:
     confine:
       root: ~/Workspace/du-an-A        # null = tắt (hành vi cũ)
       read_allow: [~/.npm, ~/.cache, ~/.local/share/uv, ~/.cargo, ~/.rustup, ~/.nvm, ~/Library/Caches]
   ```
   Khi bật: `validate_workspace` đòi mọi `dir:`/worktree/`extra_write_roots` nằm trong `root` (bỏ qua `allowed_roots`);
   `manager/permissions.check_path` cũng vậy; `systems.roots` mặc định = `[root]`.
2. **Sandbox** (`guard/runtime.sandbox_settings`):
   - A (spike OK): `denyRead: [$HOME]` + allowRead `root`, vùng workspace/scratch của chính hub, `read_allow`, phần
     `~/.claude*` mà CLI cần.
   - B (không hỗ trợ allow-trong-deny): liệt kê `denyRead` từng thư mục anh em của `root` và của các cha tới `$HOME`
     (tính lúc dựng settings; thư mục mới tạo sau đó → lần run sau mới chặn — ghi rõ giới hạn).
3. **Classifier** (`guard/classifier.py`):
   - Nhánh `READ_ONLY_TOOLS` (Read/Grep/Glob): đường dẫn ngoài `root` ∪ allow → `forbidden/read_outside_workspace` khi confine.
   - `write_outside_workspace` → `forbidden` khi confine (hôm nay là destructive/duyệt được).
   - Thêm `permissions.deny` tương ứng vào settings (lớp thứ 2 cho tool không qua sandbox).
   - Ca mới vào `tests/test_guard_classifier.py` (recall 100% trên `DESTRUCTIVE_*`, báo nhầm ≤ 5% `SAFE_*`).
4. **Hub khác** (trường hợp chung user OS): tổng quát `other_default_homes`/`sensitive_read` thành "mọi hub home đã biết"
   — registry `~/.config/cfharness/hubs.json` (mỗi daemon tự đăng ký khi khởi động) → deny đọc + ghi.
5. **`trust_full` + confine**: KHÔNG tắt sandbox phần đọc/ghi ngoài `root` (trust chỉ bỏ hỏi duyệt bên trong W). Cần chủ repo
   quyết (Q4) vì đổi ngữ nghĩa trust hiện có.
6. Hồ sơ manager `hermes`: cwd vẫn `workspaces/conv-<id>` nhưng đọc/ghi bị giới hạn bởi cùng luật confine.
7. Test: `tests/test_confine.py` (sandbox settings sinh ra, classifier đỏ→xanh, validate_workspace, manager check_path),
   e2e với `tests/fake_claude_guard.py`.

### P3 — Truy cập từ xa tiện hơn (tuỳ chọn, ~1–2 ngày)
1. **`web.public_origin`** (vd `https://mini.<tailnet>.ts.net:8711`, chỉ người, kiểm toàn vẹn): thêm Host vào `allowed_hosts`,
   Origin vào `origins`, cookie `Secure` khi https. Bind VẪN loopback (`tailscale serve` proxy về 127.0.0.1) → không phá WT-1.
   Cần kiểm chứng `tailscale serve` giữ Host gốc. Lợi ích: mở web từ điện thoại qua tailnet, không cần SSH tunnel.
2. MCP HTTP: `TransportSecuritySettings` cho cùng host (`mcp_server/http.py:70`).
3. **Không** thêm bind non-loopback; **không** mở cổng ra Internet.
4. Hướng dẫn SSH forced-command cho CI: `command="cfharness run --agent ci-bot --from-stdin",no-pty,no-port-forwarding ssh-ed25519 …`
   — cần thêm cờ đọc prompt từ stdin nếu chưa có (kiểm lại `cli.py`).

### P4 — Vận hành (tài liệu + công cụ admin, ~1 ngày)
- `docs/dev-notes/multi-instance.md`: runbook thêm người (P0 từng bước), bảng cổng, thu hồi người (khoá user, revoke bot).
- Update: mỗi user tự `cfharness update` (venv riêng theo user → không va C4). Nếu buộc chung user: dừng hết hub trước khi update.
- Tài nguyên máy: N daemon + N×`max_concurrent` tiến trình `claude` — đặt `runs.max_concurrent` theo RAM (vd mini 16 GB:
  tổng ≤ 6–8 run đồng thời toàn máy); không có trần toàn máy giữa các user → chia thủ công trong config từng hub.

## 5. Nghiệm thu
- Hai user OS An/Bình chạy song song, web/MCP/bot không va nhau; update hub An không ảnh hưởng Bình.
- Agent An: `cat ~/Workspace/du-an-B/.env` → bị sandbox chặn; Read tool → forbidden; Write ra ngoài W → forbidden (không hỏi duyệt).
- Agent An vẫn `npm install`/`uv sync`/`git` trong W bình thường (toolchain allow).
- User Bình (shell) không đọc được `~an/.cfharness/hub.db`.
- An ra lệnh qua Telegram từ 4G → run chạy, nút duyệt hoạt động; mở web từ laptop qua `ssh -L` (và qua `tailscale serve` nếu làm P3).
- Toàn bộ `uv run pytest -q` xanh, không gọi `claude` thật (trừ spike P2 ghi vào dev-notes).

## 6. Câu hỏi cần chủ repo quyết
- **Q1.** Chấp nhận mô hình "mỗi người một user macOS" (khuyến nghị)? Hay buộc chung một user (khi đó P2.4 thành bắt buộc
  và mọi người chung 1 tài khoản Claude/hoá đơn)?
- **Q2.** Tài khoản Claude: mỗi người tự đăng nhập gói của mình, hay dùng chung một gói (chỉ được khi chung user OS)?
- **Q3.** Hub cần chạy khi người đó KHÔNG đăng nhập GUI trên mini (LaunchDaemon `UserName=` thay vì LaunchAgent)? Ảnh hưởng
  `systems/autostart.py` và Keychain (OAuth claude nằm trong Keychain của user → có thể không mở được khi không có phiên).
- **Q4.** `trust_full` khi đã confine: vẫn giữ sandbox đọc/ghi ngoài W (khuyến nghị) hay giữ nguyên ngữ nghĩa cũ?
- **Q5.** Làm P3 (`tailscale serve` cho web) hay chỉ SSH tunnel?
- **Q6.** Người quản trị (phuonglq) có cần xem tổng quan mọi hub (chi phí, run) không? Nếu có → cần kênh đọc chéo user
  (vd mỗi hub xuất báo cáo ra `/Users/Shared/cfharness/<user>.json` theo lịch) — chưa có trong plan.

## 6b. Đã chốt (chủ repo, 2026-11-07)
- **Q1** mỗi người một user macOS. **Q2** dùng CHUNG một tài khoản Claude.
- **Q3** người dùng KHÔNG đăng nhập màn hình mini; chỉ điều khiển qua **link web public ra Internet**.
- **Q4** có: `trust_full` vẫn bị giam trong W. **Q5** có. **Q6** có.

Hệ quả thiết kế (thay phần tương ứng ở trên):
- **Claude dùng chung không cần GUI**: admin chạy `claude setup-token` một lần → token OAuth dài hạn lưu vào secret store
  từng hub (`CLAUDE_CODE_OAUTH_TOKEN`), daemon chèn vào env tiến trình `claude` (miễn trừ khỏi `runtime.claude.env_strip`
  đúng một biến này). Không dùng Keychain. Giới hạn: agent đọc được biến môi trường của chính tiến trình (Bash `env`) —
  giảm thiểu bằng allowlist mạng của sandbox; ghi rõ trong dev-notes.
- **Tự khởi động không phiên GUI**: LaunchDaemon (`/Library/LaunchDaemons`, `UserName=<user>`, `CFHARNESS_HOME`) thay
  LaunchAgent; `cfharness admin install-daemon --user <u>` sinh plist (chạy bằng sudo, chỉ người).
- **Web public (P3 nâng cấp)**: bind VẪN loopback; ra Internet qua tunnel có TLS (Cloudflare Tunnel hoặc Tailscale Funnel,
  code trung lập: `web.public_origin`). Vì người dùng không có máy để mở link đăng nhập một lần: thêm **đăng nhập TOTP**
  (đăng ký một lần bằng link admin cấp qua SSH), giới hạn tốc độ + khoá tạm khi sai, cookie `Secure`, phiên có hạn,
  CSP/CSRF/khoá phiên giữ nguyên. MCP HTTP KHÔNG public.
- **Q6 tổng quan admin**: mỗi hub định kỳ ghi bản tóm tắt CHỈ số liệu (chi phí, số run, trạng thái, không chữ tự do) ra
  `/Users/Shared/cfharness/status/<user>.json` (nhóm `cfharness-admin`, 0640); `cfharness admin overview` + trang web
  "Tổng quan máy" trên hub của admin đọc thư mục đó.

## 7. Thứ tự đề xuất
P0 (ngay, chạy thử 2 người) → spike P2 → P1 → P2 → P4 → P3 (nếu Q5 = có). P1 và P2 tách 2 nhánh/PR được; P2 phụ thuộc kết quả spike.
