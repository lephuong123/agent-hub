# Plan — Super Admin Console (SAC) + admind

Trạng thái: ĐÃ LÀM M + phần lớn S (2026-11-21), chờ nghiệm thu trên máy thật (`docs/acceptance/superadmin.md`). Tài liệu vận hành: `docs/dev-notes/superadmin.md`. Nguồn: `docs/BRD-Super-Admin-Console.md` v0.1 (Q1–Q3, Q5–Q10 đã chốt; Q4 còn mở).
Nền code: P1+P2 của `plan-admin-users.md` (`admin_*.py`, `web/admin_pages.py`, `scripts/setup-hub-user.sh`), cfharness v0.2.27.

## 0. Kết luận chính

1. **Hai tiến trình mới, một gói mỗi bên:** `cfharness.superadmin` (web SAC, uid `_cfh_sac`, loopback, không agent) và
   `cfharness.admind` (root, LaunchDaemon hệ thống, socket Unix, **nơi duy nhất ghi `sac.db`**, giữ secret Cloudflare/bot/token Claude).
   SAC chỉ là lớp giao diện mỏng: mọi đọc/ghi đều là một **mã thao tác có kiểu** gửi tới admind. SAC bị chiếm mà không có mật khẩu +
   TOTP của quản trị thì không làm được gì (admind tự kiểm phiên, vai trò, step-up, four-eyes — BR-SA-01).
2. **Hub user gần như không đổi kiến trúc.** Vẫn mô hình kéo từ file trong `/Users/Shared/cfharness`. Thêm: bản xuất `profile` (SPEC v3),
   báo cáo ngày, hộp thư việc team, ẩn giao diện quản trị (VIS), lệnh CLI phục vụ admind (`web unlock-login`, `totp reset --json`…).
   admind tác động vào hub bằng **CLI của chính hub chạy dưới uid user** (`sudo -u <u> cfharness …`, giống hàm `cfh()` của script) —
   không mở socket chéo vào hub.
3. **`setup-hub-user.sh` thành job Python có bước idempotent** (`admind/steps/user_create.py`), mỗi bước có `check()` + `apply()` +
   `undo()`; script giữ lại làm đường dự phòng cho tới khi SA2 chạy ổn trên máy thật.
4. **Cloudflare đi qua API token theo zone** (thay `cert.pem`), sửa tận gốc lỗi tạo nhầm zone. Mọi lần sửa ingress:
   ghi file tạm → `cloudflared tunnel ingress validate` → thay nguyên tử → kickstart → kiểm HTTP → lỗi thì tự quay lại bản cũ.
5. **Thứ tự làm theo BRD (SA0→SA5)**, nhưng tách nhỏ thành 24 PR. Mỗi PR có test không gọi `sudo`/`launchctl`/`sysadminctl`/
   Cloudflare/`claude` thật (runner giả + server CF giả). Ước lượng ≈ 45–55 ngày công cho M + S; C để sau.
6. **Hai cổng cần chốt trước khi bật ngoài máy thật:** (a) Q4 (thông báo cho user) chặn việc bật mức xem `Hoạt động` trên production —
   code vẫn làm, công tắc SYS-03 để tắt cho tới khi có quyết định; (b) pentest SAC trước khi mở `superadmin.trungtamdhs.com` ra ngoài
   (NFR-SA-02).

## 1. Điểm trong BRD cần chỉnh/chốt thêm trước khi code

| # | Chỗ trong BRD | Vấn đề | Đề xuất trong plan này |
|---|---|---|---|
| B1 | Q10 chốt `host_id` thật, nhưng 3.2 vẫn ghi "quản trị nhiều máy chủ" là Won't; NFR-SA-06 vẫn "một máy" | Mâu thuẫn; làm fleet thật cần agent từ xa + kênh mạng cho admind (phá C3) | Làm **data model + bộ lọc UI** (`hosts`, mọi bảng có `host_id`, máy hiện tại tự đăng ký). **Không** làm điều khiển máy khác ở bản này. Sửa 3.2 thành "điều khiển từ xa máy khác: Won't" |
| B2 | Q3 bật mức `Hoạt động` toàn hệ; NFR-SA-10 đòi có quy định nội bộ; Q4 còn mở | Bật trước khi chốt Q4 là rủi ro pháp lý/nhân sự | Code đủ cả 4 mức; SYS-03 mặc định **tắt** `Hoạt động`; bật cần nhập số văn bản quy định (đã có trong NFR-SA-10) — là việc vận hành, không chặn code |
| B3 | TEAM-06: hub "đọc + đánh dấu đã nhận" trong `teams/<team>/inbox/<u>/` | Muốn đánh dấu thì hub phải ghi được vào hộp thư → hub sửa/xoá được việc admind giao | Hộp thư **chỉ đọc** với user (`root:<u> 0750`, file 0640). Nhận/từ chối/xong ghi vào file của chính hub `teams/<team>/status/<u>.json` (đã có trong 10.2). admind đối chiếu hai bên |
| B4 | AUTH-03 passkey/WebAuthn | Cần thư viện WebAuthn (chưa có trong dependency) | Bản đầu: mật khẩu (`hashlib.scrypt`, stdlib) + TOTP (dùng lại `web/totp.py`). Passkey → SA5 (S) |
| B5 | USR-04, RPT-06, MON-03: gửi email | Repo chưa có cấu hình SMTP | SA1/SA2 dùng Telegram/Slack (đã có `notify/`). Email = PR riêng ở SA3 (`sys.smtp_*` trong SYS-01) |
| B6 | AUTH-02: kiểm JWT Access | Cần lấy khoá công khai của team Access (`/cdn-cgi/access/certs`) | `cryptography` đã là dependency → tự kiểm RS256, cache JWKS 1 giờ, kiểm `aud` = AUD tag của ứng dụng Access |
| B7 | RPT-02 "tóm tắt 1–3 câu do hub sinh" | Gọi LLM thì tốn tiền mỗi ngày mỗi hub | Không gọi LLM: lấy câu kết quả từ `task_report.parse()` (khung "Đã làm/Còn mở/Cần bạn" đã có), cắt 300 ký tự, qua `redact` |
| B8 | API-02 "RPC `superadmin.*` ∈ HUMAN_ONLY" | SAC không đi qua RPC của hub; admind có giao thức riêng | Hub **không có** method `superadmin.*` nào; test khẳng định hub trả `METHOD_NOT_FOUND`. RPC mới phía hub (`team.inbox_*`) ∈ `HUMAN_ONLY`, không vào `MANAGER_ALLOWED` |
| B9 | AUTH-06 hệ chỉ 1 Super Admin → chờ 1 giờ | Q8 khuyến nghị ≥ 2 nhưng chưa có danh sách | Làm cả hai nhánh; `superadmin init` cảnh báo khi chỉ có 1 Super Admin |
| B10 | 5.1 đảo quyết định: trang `/admin/*` và `/machine` của hub | Chưa nói số phận trang `/admin/*` của hub admin khi đã có SAC | SA1: `/admin/*` vẫn chạy cho nhóm `cfharness-admin`, ẩn hoàn toàn với người khác (VIS-02). SA2 xong: gỡ `/admin/*` khỏi hub (SAC thay thế), giữ RPC đọc cho CLI cũ một phiên bản rồi gỡ |

Đã duyệt 2026-11-21; BRD v0.2 đã cập nhật theo B1–B10.

## 2. Kiến trúc triển khai

### 2.1 Tiến trình, user, đường dẫn

| Thành phần | Chạy bằng | Nghe | Dữ liệu |
|---|---|---|---|
| `cfharness superadmin serve` | `_cfh_sac` (user hệ thống ẩn, không shell, UID < 500) | `127.0.0.1:8700` (cấu hình được), chỉ Host `superadmin.<zone>` | Không có gì trên đĩa ngoài `/var/lib/cfharness-sac/web.json` (cổng, khoá ký cookie). Phiên ở RAM |
| `cfharness admind` | root, `/Library/LaunchDaemons/com.cfharness.admind.plist` | `/var/run/cfharness/admind.sock` `root:_cfh_sac 0660` trong thư mục `0750` | `/var/db/cfharness-superadmin/sac.db` (root 0600), `secrets/` (root 0600), `backups/` |
| Tunnel | root, `com.cfharness.tunnel` (system domain — A4) | — | `/etc/cfharness/tunnel.yml` (root 0644; chuyển từ `~admin/.cloudflared/cfharness-config.yml`) |
| Hub user | uid user | như cũ | như cũ + file xuất mới trong `/Users/Shared/cfharness` |

Bản cài SAC/admind: `/opt/cfharness` root-owned (`--system-install` đã có ở `systems/autostart.LaunchDaemonPlan`) — admind **không bao giờ**
chạy code nằm trong thư mục user ghi được.

Xác thực kết nối socket: admind kiểm uid đầu kia bằng `LOCAL_PEERCRED`/`getpeereid` (macOS) → chỉ nhận `_cfh_sac` và root (CLI
`cfharness superadmin …` chạy bằng sudo). Dùng lại khung `transport.peer_pid`, thêm `peer_uid`.

### 2.2 Giao thức SAC ↔ admind

JSON một dòng (cùng kiểu `rpc.encode`): `{"op": "user.suspend", "params": {...}, "ticket": "<phiên>", "req_id": "..."}` →
`{"ok": true, "result": ...}` / `{"ok": false, "error": {"code": "forbidden|step_up_required|approval_required|invalid|...", ...}}`.

- `ticket` do admind cấp ở `auth.login` (sau khi kiểm mật khẩu + TOTP), gắn `admin_id, role, scope, strong_auth_at, ip, ua`.
  SAC giữ ticket trong phiên RAM. Hết hạn: 15 phút không dùng / 8 giờ (AUTH-04) — admind là nơi quyết định, SAC chỉ phản chiếu.
- **Sổ đăng ký thao tác** `admind/ops/__init__.py`: `OPS[name] = Op(params=<pydantic model, regex mọi chuỗi>, risk="read|normal|high|critical",
  roles={...}, scope=<hàm lấy đối tượng để kiểm phạm vi>, handler)`. Không có thao tác nào nhận chuỗi lệnh; tham số tên user/hostname/
  tag/cổng đều qua regex (dùng lại `admin_control.valid`).
- Pipeline một lời gọi: giải mã → tra `OPS` (không có → `invalid`, không lộ danh sách) → validate → kiểm vai trò + phạm vi →
  `high`: kiểm `strong_auth_at` ≤ 5 phút (AUTH-05) → `critical`: tạo `ApprovalRequest`, trả `approval_required` (AUTH-06) →
  chạy (thao tác nhiều bước thì tạo Job, trả `job_id`) → ghi `AuditEntry` (cả khi bị từ chối) → trả kết quả.

### 2.3 Danh mục mã thao tác (bản đầu)

| Nhóm | Mã (đọc in nghiêng không ghi audit chi tiết, chỉ đếm) | Mức |
|---|---|---|
| auth | `auth.login`, `auth.step_up`, `auth.logout`, *`auth.sessions`*, `auth.session_revoke` | — / normal |
| admin account | `admin.create`, `admin.update`, `admin.lock`, `admin.reset_totp` (high); `admin.delete`, `admin.set_superadmin` (critical) | |
| zone/domain | `zone.add`, `zone.verify` (high); `zone.remove` (critical); *`domain.list`*, `domain.create`, `domain.revoke`, `domain.rename`, `domain.access_set` (high); `domain.check_now`, `domain.reconcile` (normal) | |
| user | *`user.list`*, *`user.profile`*, `user.import_scan` (read), `user.import`, `user.create`, `user.suspend`, `user.resume`, `user.revoke`, `user.restore` (high), `user.update` (normal), `user.delete` (critical) | |
| hub | `hub.restart`, `hub.start`, `hub.stop`, `hub.doctor`, *`hub.logs`* (normal); `hub.update`, `hub.pin`, `hub.rollback`, `hub.config_set` (high); `hub.backup` (normal), `hub.restore` (critical) | |
| access | `access.totp_reset`, `access.login_unlock`, `access.sessions_revoke`, `access.devices_revoke`, `access.chat_unpair`, `access.mcp_token_rotate` (normal) | |
| policy | *`policy.get`*, *`policy.effective`*, *`policy.simulate`*, `policy.save`, `policy.rollback` (high) | |
| estop | `estop.on` (normal), `estop.off` (high), *`estop.list`* | |
| claude | `claude.account_add`, `claude.account_assign`, `claude.account_remove` (high), *`claude.accounts`*, *`cost.report`* | |
| team | `team.upsert`, `team.member_set`, `team.folder_grant` (normal, Super Admin); `team.task_create`, `team.task_assign`, `team.task_review` (normal, Team Lead trong team) | |
| report/mon | *`report.get`*, *`report.search`*, *`overview`*, *`alerts`*, `alert.ack`, `alert.mute` (normal) | |
| audit | *`audit.search`*, *`audit.verify`* | |
| job/approval | *`job.get`*, `job.retry`, `job.cancel` (normal); `approval.decide` (theo mức của thao tác gốc) | |
| system | `sys.config_set` (high), `sys.cf_token_rotate` (critical), *`sys.setup_check`*, `sys.visibility_set` (high) | |

Ma trận vai trò mục 8 của BRD → một bảng hằng `admind/rbac.py: MATRIX[(role, op_group)] = "write"|"read"|None`; SAC dùng **cùng bảng**
để dựng menu (VIS-08: ẩn hẳn mục không có quyền, URL gõ tay → 404). Test sinh từ bảng: mọi (vai trò × mã) đều có ca.

### 2.4 Job

`Job(id, kind, target, state, created_by, approved_by, steps[])`, trạng thái `queued|awaiting_approval|running|succeeded|failed|cancelled`.
Bước = lớp có `name`, `check(ctx) -> bool` (đã xong chưa — đọc trạng thái thật của máy), `apply(ctx)`, `undo(ctx)` (cho "Huỷ và dọn").
Chạy lại = bỏ qua bước `check()` đúng (JOB-02). admind khởi động lại → job `running` thành `failed` + `resume_from`. Tối đa 3 job ghi song song
(JOB-03), khoá theo đối tượng (một user chỉ một job ghi một lúc). Log bước qua `redact` + bộ che token đã biết trong job (token Claude,
link TOTP) trước khi ghi DB.

Job `user.create` (port từ `setup-hub-user.sh` 11 bước):

| # | Bước | Ghi chú |
|---|---|---|
| 1 | `os_user` — `sysadminctl -addUser` (ẩn mật khẩu, mật khẩu ngẫu nhiên, không lưu) | A3 |
| 2 | `install` — `uv`, `claude`, `cfharness@<tag>` dưới uid user | như script bước 2 |
| 3 | `claude_token` — nạp token vào secret store của hub qua stdin (`cfharness secrets set CLAUDE_CODE_OAUTH_TOKEN --stdin`), gọi thử `claude` | A5; token chỉ sống trong RAM của job |
| 4 | `config` — cổng web/MCP (DOM-09 cấp), `workspace.confine.root`, `admin_export.enabled=true` | |
| 5 | `launchd` — `LaunchDaemonPlan` daemon + web, kickstart | dùng `systems/autostart.py` |
| 6 | `manager` — agent `dieu-phoi` (tuỳ chọn) | |
| 7 | `shared_dirs` — `status/<u>/`, `audit/<u>/`, `report/<u>/`, nhóm `cfharness-users` | dùng `admin_users.AddPlan` |
| 8 | `domain` — DNS + ingress + kickstart tunnel + `web.public_origin` | dùng chung với `domain.create` |
| 9 | `kgs` — Không gian số (tuỳ chọn) | |
| 10 | `doctor` — `cfharness doctor --json` phải xanh; kiểm `https://<sub>.<zone>` trả trang đăng nhập | |
| 11 | `totp` — `totp enroll` → link hiện **một lần** trong SAC (không lưu DB, chỉ giữ trong RAM SAC tới khi xem) | USR-04 |

### 2.5 Hub ↔ thư mục chung (mở rộng 10.2 của BRD)

```
/Users/Shared/cfharness/
  status/<u>/<hub_id>.json        (có)   + profile.json (SPEC v3, mới)      <u>:cfharness-admin 2750 / 0640
  report/<u>/<YYYY-MM-DD>.json    (mới)                                      <u>:cfharness-admin 2750 / 0640
  audit/<u>/                      (có)
  policy/                         (có, giờ admind ghi; hub đọc như cũ)
  control/estop[.<u>]             (có)   + visibility.json (VIS-07, admind ghi, hub đọc)
  teams/<team>/inbox/<u>/<task>.json  (mới) root:<u> 0750 / 0640 — admind ghi, hub CHỈ đọc
  teams/<team>/status/<u>.json        (mới) <u>:cfharness-admin 0640 — hub ghi nhận/từ chối/tiến độ/xong
```

`_cfh_sac` **không** vào nhóm `cfharness-admin`: mọi đọc file của hub do admind làm, bằng bộ đọc đã gia cố (`admin_overview.read_dir`:
O_NOFOLLOW, ≤ 64 KB, kiểm chủ file, lọc SPEC). Dữ liệu từ hub là không tin cậy kể cả với root (NFR-SA-03).

### 2.6 `sac.db`

SQLite riêng, danh sách migration riêng (`admind/db.py: MIGRATIONS`, cùng cách `store.py` dùng `PRAGMA user_version`). Bảng theo 10.1 của
BRD, mọi bảng có `host_id` (B1): `hosts`, `admin_accounts`, `admin_sessions` (chỉ để AUTH-11 liệt kê; ticket thật ở RAM admind),
`hub_users`, `hubs`, `zones`, `domains`, `groups`, `teams`, `team_members`, `team_tasks`, `policies`, `policy_versions`,
`claude_accounts`, `daily_reports`, `profile_snapshots`, `jobs`, `job_steps`, `approvals`, `alerts`, `audit` (chuỗi băm — dùng lại
`audit_chain.py`), `samples` (MON-05, thay `admin_samples` của hub admin). Ràng buộc duy nhất `(zone_id, sub)`, `(host_id, os_user)`,
`(host_id, web_port)`, `(host_id, mcp_port)`.

Nguồn sự thật: **máy thật** (user OS, plist, ingress, DNS) là sự thật; `sac.db` là bản ghi mong muốn + lịch sử. `domain.reconcile` /
`user.import_scan` so hai bên (DOM-08, USR-13).

## 3. Thay đổi phía hub

| Việc | File | FR |
|---|---|---|
| SPEC v3: `profile.json` (danh tính, hạ tầng, truy cập, agent, chỉ số, chi phí theo agent/model), danh sách trắng theo mức xem đọc từ `control/visibility.json` | `admin_overview.py` (+ `PROFILE_SPEC`) | PRO-01..08 |
| Báo cáo ngày: chốt 23:59 giờ máy (lịch nền của daemon), ghi `report/<u>/<ngày>.json`; mức `Hoạt động` mới có tiêu đề task + câu kết quả (`task_report.parse`) + tên tệp | `daily_report.py` (mới) | RPT-01..04 |
| Hộp thư team: `TeamInbox` kiểm `teams/*/inbox/<u>/` mỗi 30 s, event `team.inbox_new`; RPC `team.inbox_list|accept|reject|submit` (HUMAN_ONLY); nhận → `tasks.create` có `team_task_id`; trạng thái task → `teams/<team>/status/<u>.json` | `team_inbox.py` (mới), `tasks.py`, `web/` (khối "Việc được giao"), `notify/telegram`, `notify/slack` | TEAM-06, 07, 09 |
| Ẩn quản trị: `/admin/*`, `/machine` → 404 giống route lạ khi không phải admin (và `/machine` theo `visibility.json`); `admin.nav` rỗng; `admin` ẩn khỏi `--help` và báo "không có lệnh" khi không thuộc nhóm; RPC `admin.*` trả `METHOD_NOT_FOUND` thay vì `forbidden` | `web/app.py`, `web/admin_pages.py`, `cli.py`, `admin_rpc.py`, `admin_overview.py` | VIS-02, 03, 06 |
| Thông báo trung tính: banner ESTOP admin/chính sách/tạm khoá không mã lý do, không tên quản trị | `web/templates/base.html`, `tui/`, `notify/*` | VIS-04 |
| Lệnh cho admind (chạy dưới uid user, `--json`, không in secret): `web unlock-login` (ghi cờ `HUB_HOME/run/web-unlock`, web xoá `LoginLimiter` ở lần kiểm kế tiếp), `web sessions revoke`, `totp reset --json` (disable + enroll, trả link), `secrets set --stdin`, `mcp token rotate` | `web/cli.py`, `web/public.py`, `web/totp.py`, `secrets.py` | ACC-01..07 |
| Policy mở rộng: scope `group`/`team` (file `policy/group.<g>.json`, `team.<t>.json`), gộp chặt hơn như cũ | `admin_policy.py` | POL-02 |

Hub cũ (chưa có SPEC v3/report) vẫn hiện trong SAC từ SPEC v2 + nhãn "cần cập nhật" (NFR-SA-11).

## 4. Lộ trình theo PR

Quy ước: mỗi PR một nhánh từ `main`, test xanh, release note khi đóng một giai đoạn. "DoD" = điều kiện xong.

### SA0 — Spike & khung (≈ 4 ngày)

**PR SA0-1 · Spike A1–A5 trên Mac mini thật** (ghi kết quả vào mục 9 của file này, không merge code spike)
- A1 liệt kê zone bằng token API; A2 tạo ứng dụng Access cho `superadmin.trungtamdhs.com`; A3 `sysadminctl`/`dseditgroup`/
  `launchctl bootstrap system` từ LaunchDaemon root không phiên GUI; A4 chuyển `com.cfharness.tunnel` sang system domain, tunnel sống
  qua đăng xuất admin; A5 nạp token Claude qua stdin dưới `sudo -u`, không lộ trong `ps`/log.
- DoD: bảng kết quả 5 dòng + quyết định đi tiếp/đổi hướng cho mỗi giả định. Cập nhật BRD v0.2 (B1–B10).

**PR SA0-2 · Khung admind**
- `admind/server.py` (asyncio, socket, `peer_uid`), `admind/ops/` (sổ đăng ký + pipeline 2.2, chưa có thao tác thật ngoài `sys.ping`),
  `admind/db.py` (migration v1: `hosts`, `audit`), `admind/audit.py` (chuỗi băm, `audit.verify`), `admind/runner.py` (chạy lệnh có
  danh sách trắng argv, tiêm được — dùng chung kiểu `_default_runner` của `admin_users.py`).
- `cfharness admind serve|status`, plist `com.cfharness.admind` (qua `LaunchDaemonPlan`).
- Test `tests/test_admind_server.py`: uid lạ bị từ chối, op lạ → `invalid`, tham số sai regex, audit ghi cả khi từ chối, chuỗi băm phát hiện sửa dòng.

**PR SA0-3 · Tunnel system + module ingress**
- `admind/tunnel.py`: đọc/ghi `tunnel.yml` (YAML, giữ thứ tự), thêm/gỡ cặp hostname→service, cấm trỏ cổng MCP, `validate` → thay nguyên tử →
  kickstart → kiểm HTTP → quay lại khi lỗi (NFR-SA-08). Lệnh `cfharness admind tunnel migrate` chuyển file + plist sang system.
- Test với `cloudflared` giả + server HTTP giả: validate lỗi giữ file cũ; kiểm HTTP lỗi → khôi phục.

### SA1 — Nền (≈ 12 ngày)

**PR SA1-1 · Tài khoản quản trị + đăng nhập trong admind** (AUTH-03, 04, 07, 08, 11)
- Migration: `admin_accounts`, `admin_sessions`. Mật khẩu `hashlib.scrypt` (n=2^15, r=8, p=1), TOTP dùng lại `web/totp.py` (secret trong
  `secrets/` của admind, không trong DB). Ticket RAM; `auth.login|step_up|logout|sessions|session_revoke`; khoá dò dùng lại `LoginLimiter`.
- `cfharness superadmin init` (sudo, loopback): tạo Super Admin đầu tiên + mã khôi phục; cảnh báo nếu chỉ có 1 Super Admin (B9).
- Không tự hạ quyền / không xoá Super Admin cuối cùng (AUTH-08). Test đủ các nhánh.

**PR SA1-2 · Ứng dụng web SAC** (AUTH-01, 02, 04, VIS-01, VIS-08, NFR-SA-02, 09)
- `superadmin/app.py` (Starlette, dùng lại `web/security.py`: `Session`, CSRF, cookie `__Host-`), `superadmin/access.py` (kiểm JWT
  `Cf-Access-Jwt-Assertion`, JWKS cache, `aud`), `superadmin/client.py` (client socket admind), `superadmin/nav.py` (menu từ `rbac.MATRIX`).
- Host/Origin khác → 421/403; thiếu phiên → **404 trơn** (không tên sản phẩm). CSP chặt, không script ngoài. Giao diện tiếng Việt,
  dùng được ở 360 px, sáng/tối theo hệ thống.
- Trang: đăng nhập, step-up (modal), tài khoản quản trị, phiên đang hoạt động.
- Test `tests/test_sac_web.py`: thiếu JWT → 404, JWT sai `aud`/hết hạn, Host lạ, CSRF, idle 15 phút, max 8 giờ, menu theo từng vai trò.

**PR SA1-3 · Thu thập + nhập user có sẵn + danh sách** (USR-01, 13, PRO-01..03, 05, NFR-SA-05, 11)
- `admind/collector.py`: mỗi 60 s đọc `status/` bằng `admin_overview.read_dir` (giữ nguyên gia cố), ghi `hubs`, `samples`.
- `user.import_scan` / `user.import`: ghép nhóm `cfharness-users` + plist `com.cfharness.daemon.<u>` + hostname trong ingress → đưa vào
  `hub_users`/`hubs`/`domains` không chạm hub.
- Trang Danh sách user (lọc, sắp xếp, chọn nhiều — 200 dòng ≤ 1 s) và Hồ sơ 360° mức Số liệu/Hồ sơ (từ SPEC v2 khi chưa có v3).

**PR SA1-4 · Tên miền chỉ đọc + kiểm sức khoẻ** (DOM-01, 02, 07, 09)
- `admind/cloudflare.py` (urllib, không thêm dependency; token trong `secrets/`): liệt kê zone, DNS, kiểm token đủ quyền.
- `zone.add|verify`, bảng tên miền trung tâm, kiểm mỗi 10 phút (DNS, ingress khớp, tunnel có connector, HTTPS ra trang đăng nhập, chứng chỉ),
  cảnh báo `domain_dns|domain_tunnel|domain_tls`. Bộ cấp cổng (dải 8711–8799) — chỉ đọc ở PR này, ghi ở SA2.
- Test với server Cloudflare API giả (`tests/fakes/cf_api.py`).

**PR SA1-5 · ESTOP + tổng quan + cảnh báo + audit** (EST-01, 02, 04, MON-01..03, AUD-01..05)
- `estop.on|off|list` ghi `control/estop[.<u>]` (dùng lại `admin_control.set_flag`); nút ESTOP luôn ở thanh trên cùng.
- `admind/alerts.py`: chuyển logic `AdminWatcher.hub_codes` sang admind (gọi lại hàm thuần), thêm mã mới; ack/mute/ghi chú; gửi qua
  Telegram/Slack bằng bot riêng của quản trị (token trong `secrets/` admind; dùng lại `notify/slack.format_message`).
- Trang Tổng quan (làm mới 5 s), Cảnh báo, Nhật ký (lọc, xuất CSV), kiểm toàn vẹn chuỗi hằng ngày → `tamper`.
- Hub admin: `AdminWatcher` tắt khi admind đang chạy (tránh báo đôi).

**PR SA1-6 · Ẩn quản trị phía hub + test quét** (VIS-02..06, VIS-10, NFR-SA-04)
- Các thay đổi "Ẩn quản trị" + "Thông báo trung tính" ở mục 3. `control/visibility.json` (mặc định `transparency=false`).
- `tests/test_vis_scan.py`: dựng web app với user thường, quét **mọi route** đã đăng ký (lấy từ `app.routes`), mọi lệnh trong
  `cli_surface`, mọi method RPC, mọi tool MCP `hub-self`, mẫu tin Telegram/Slack → không chứa `admin`, `superadmin`, `quản trị máy`,
  tên miền SAC. Chạy trong CI mỗi PR; lộ = đỏ.
- Hoàn tất các ca VIS trong `test_sec_auth.py`.

**Mốc SA1 — phát hành v0.3.0:** SAC chạy trên máy thật sau Access, đọc được toàn bộ user/hub/tên miền, ESTOP, cảnh báo, nhật ký; user
thường không còn thấy gì về quản trị. Chưa ghi gì vào máy ngoài ESTOP.

### SA2 — Vòng đời (≈ 13 ngày)

**PR SA2-1 · Engine job + four-eyes** (JOB-01, 02, AUTH-05, 06)
- `admind/jobs.py` (2.4), `admind/approvals.py` (yêu cầu chờ, hết hạn 24 giờ, hệ 1 Super Admin → chờ 1 giờ + cảnh báo).
- Trang Job (tiến trình từng bước, log đã che, "Thử lại từ bước này", "Huỷ và dọn"), trang Yêu cầu duyệt.
- Test: chạy lại bỏ qua bước xong; admind chết giữa chừng → `failed` + `resume_from`; người yêu cầu không tự duyệt được.

**PR SA2-2 · Cấp / thu hồi / đổi tên miền** (DOM-03, 04, 05, 08, 09)
- `domain.create`: kiểm tên (regex, ≤ 63, danh sách cấm `superadmin|www|mail|api|admin|…`), CNAME → `<tunnel-id>.cfargotunnel.com`
  **đúng zone** qua API, ingress (SA0-3), `web.public_origin` của hub. `domain.revoke` theo thứ tự DOM-05. `domain.rename` với 301 N ngày.
- `domain.reconcile`: hostname mồ côi, hub không hostname, cổng trùng, CNAME trỏ tunnel khác → nút "Sửa" tạo job.

**PR SA2-3 · Wizard tạo user** (USR-02, 03, 04, CLA-01 phần chọn tài khoản)
- Job `user.create` 11 bước (2.4). Wizard 5 bước + xem lại; dán token ẩn khi gõ, chỉ gửi một lần tới admind.
- Link TOTP hiện một lần + nút gửi Telegram/Slack (email ở SA3). Email chào mừng không nhắc SAC.
- Test bằng runner giả cho từng bước (`check` đúng/sai, `apply` lỗi, `undo`); một ca "máy giả" chạy hết 11 bước.
- DoD trên máy thật: tạo `testsa1.trungtamdhs.com` ≤ 10 phút không bước tay ngoài dán token (G2).

**PR SA2-4 · Tạm khoá / mở khoá / thu hồi / khôi phục / sửa** (USR-06..09, 11)
- `user.suspend`: ESTOP user + dừng run + `web sessions revoke` + ingress trả trang "Tạm khoá" (service tĩnh của SAC, không lộ tên) ;
  `user.resume` đảo lại. `user.revoke` theo `RevokePlan` có sẵn + thu hồi tên miền + vô hiệu TOTP/thiết bị. `user.restore`.
- Bảng chuyển trạng thái 9.1 là hằng; chuyển sai → `invalid`.

**PR SA2-5 · Điều khiển hub** (HUB-01, 02, 06, 07)
- `hub.restart|start|stop` (`launchctl kickstart -k system/com.cfharness.{daemon,web}.<u>`, hỏi dừng run hay chờ), `hub.update`
  (một hoặc nhiều hub, cả kiểu `uv tool` lẫn `--system-install`; hiện đoạn `CHANGELOG.md` của tag), `hub.doctor`, `hub.logs` (2.000 dòng
  cuối, qua `redact`).

**PR SA2-6 · Truy cập của user** (ACC-01, 02, 03)
- Lệnh CLI mới phía hub (mục 3) + `access.totp_reset|login_unlock|sessions_revoke`. Luồng 4 của BRD ≤ 2 phút trên điện thoại.

**PR SA2-7 · Thao tác hàng loạt + dọn hub admin cũ** (JOB-03, B10)
- Chọn nhiều user → cập nhật/ESTOP/khởi động lại; tối đa 3 job song song.
- Gỡ `/admin/*` khỏi web của hub (SAC thay thế); CLI `cfharness admin …` in "dùng `cfharness superadmin …`" một phiên bản.

**Mốc SA2 — v0.3.x:** toàn bộ vòng đời user/hub/tên miền không SSH. `setup-hub-user.sh` đánh dấu "dự phòng".

### SA3 — Báo cáo & Team (≈ 13 ngày)

**PR SA3-1 · SPEC v3 profile + báo cáo ngày phía hub** (PRO-04, 06..09, RPT-01..04)
- Mục 3 (hai dòng đầu). Mức xem lấy từ `control/visibility.json`; hub không bao giờ xuất trường cao hơn mức đang bật.

**PR SA3-2 · Báo cáo trong SAC + gửi** (RPT-05, 06, 08, 09, SYS-04)
- 00:05 gom, đánh dấu `missing`, tính bất thường; 08:00 gửi. Báo cáo user/team/toàn hệ; lưu 90 ngày; tìm, xuất CSV/xlsx/PDF
  (xlsx/PDF: chỉ khi không cần dependency mới — nếu cần thì CSV trước, xlsx/PDF ở SA5).

**PR SA3-3 · Email (SMTP)** (USR-04, RPT-06, MON-03 kênh email) — B5.

**PR SA3-4 · Team: dữ liệu + thành viên + thư mục** (TEAM-01, 02, 04)
- `team.upsert|member_set|folder_grant`; thư mục team qua filehub (lane) của từng hub thành viên; thu hồi khi rời team.

**PR SA3-5 · Hộp thư việc + bảng kanban** (TEAM-05, 06, 07, 09, 12, 13)
- admind ghi `teams/<team>/inbox/<u>/` (B3); hub `TeamInbox` + khối "Việc được giao" trên web/Telegram/Slack; Nhận → task; trạng thái về
  `teams/<team>/status/<u>.json` → bảng cập nhật ≤ 1 phút. Nội dung việc: giới hạn độ dài, escape khi hiển thị, chỉ tới agent sau khi
  người bấm Nhận (BR-SA-02).
- Test luồng 2 của BRD đầu-cuối với hai hub giả.

**PR SA3-6 · Vai trò Team Lead** (TEAM-17, VIS-09, Q5)
- Team Lead đăng nhập SAC (tài khoản quản trị vai trò `team_lead`, gắn `user_id` + team); chỉ thấy khu team mình, mức Hoạt động của thành viên.

**Mốc SA3 — v0.4.0.** Mức xem `Hoạt động` vẫn tắt cho tới khi Q4 có quyết định (B2).

### SA4 — Chính sách & chi phí (≈ 7 ngày)

**PR SA4-1 · Chính sách qua admind** (POL-01..06)
- Form theo `POLICY_SPEC`, scope máy/mặc định/nhóm/team/user; mỗi lần lưu `rev+1` + diff trong `policy_versions`, admind ghi file root
  `policy/*.json` (0644) nguyên tử; xem hiệu lực từng khoá + nguồn; mô phỏng (hub sẽ bị chặn do `min_version`…); rollback một bước;
  `policy_mismatch` sau 2 chu kỳ.

**PR SA4-2 · Tài khoản Claude + chi phí + ngân sách** (CLA-01..05, TEAM-10, 11)
- Kho token (secret store admind; chỉ tên, `auth status`, tuổi), gán/đổi hàng loạt (`account use` dưới uid user), xoay token;
  báo cáo chi phí theo user/team/nhóm/tài khoản/model; ngân sách qua chính sách, cảnh báo 80/100%, dự báo cuối tháng.

### SA5 — Mở rộng (S/C, ≈ 8 ngày, làm theo nhu cầu)

USR-10 + USR-12 (xoá hẳn có sao lưu mã hoá, four-eyes, chờ 24 giờ), HUB-03/04/05 (cập nhật theo đợt, ghim, quay về), HUB-08 (sao lưu/khôi phục),
JOB-04 (nhập CSV), AUTH-03 passkey, AUTH-09 break-glass, DOM-06/ACC-08 (Access cho hub), DOM-10 (danh mục app desktop), ACC-04..07,
EST-03, MON-04/05, RPT-07, PRO-10, TEAM-03/08/14/15/16, HUB-09/10, xlsx/PDF. CNT (Q1: chưa làm), PRO-11/RPT-10/VIS-07 (C).

## 5. Truy vết FR → PR (M)

| Nhóm | PR |
|---|---|
| AUTH-01..08, 11 | SA1-1, SA1-2, SA2-1 (05, 06) |
| DOM-01, 02, 07, 09 / 03, 05 | SA1-4 / SA2-2 |
| USR-01, 13 / 02..09 | SA1-3 / SA2-3, SA2-4 |
| PRO-01..03, 05 / 04, 06, 08 | SA1-3 / SA3-1 |
| RPT-01..06, 08 | SA3-1, SA3-2, SA3-3 |
| TEAM-01, 02, 04..07, 09, 12, 13, 17 | SA3-4, SA3-5, SA3-6 |
| VIS-01..06, 08..10 | SA1-2, SA1-6, SA3-6 |
| HUB-01, 02, 06 | SA2-5 |
| ACC-01..03 | SA2-6 |
| POL-01..03, 05, 06 | SA4-1 |
| EST-01, 02, 04 | SA1-5 |
| CLA-01, 02, 04, 05 | SA2-3, SA4-2 |
| MON-01..03 | SA1-5 |
| AUD-01..05 | SA0-2, SA1-5 |
| JOB-01..03 | SA2-1, SA2-7 |
| SYS-01..05 | SA1-4 (01), SA2-3 (02), SA3-1 (03), SA3-2 (04), SA1-6 (05) |
| API-02, 03 | SA1-6 (test), SA0-2 (admind không nghe mạng) |

## 6. Kiểm thử

- Không gọi thật: `sudo`, `sysadminctl`, `dseditgroup`, `launchctl`, `cloudflared`, Cloudflare API, `claude`. Tiêm `runner`, `owner_of`,
  `peer_uid`, đồng hồ; server CF giả trong `tests/fakes/`. `CFHARNESS_TEST_SHARED_ROOT` như hiện có.
- Bảo mật: ma trận vai trò × mã thao tác sinh tự động (`test_admind_rbac.py`); step-up/four-eyes; ticket hết hạn; uid socket; secret không
  vào log/audit/DB (quét chuỗi token mẫu trong mọi bảng + log sau một job đầy đủ); dữ liệu hub độc hại (symlink, file 10 MB, JSON sai,
  chủ file sai, chuỗi HTML/prompt injection trong tiêu đề việc) không làm hỏng SAC và được escape.
- Hồi quy: `test_sec_auth.py`, `test_admin_*.py`, `test_guard_classifier.py`, `test_confine.py` xanh 100% (G7).
- Ẩn: `test_vis_scan.py` trong CI (G6, NFR-SA-04).
- Hiệu năng: 200 user giả → danh sách ≤ 1 s, hồ sơ ≤ 1 s, báo cáo toàn hệ ≤ 2 phút (NFR-SA-05).
- Máy thật cuối mỗi mốc: kịch bản trong `docs/acceptance/superadmin-SAx.md` (tạo user thử, cấp lại TOTP từ điện thoại, ESTOP, quay lại ingress lỗi).

## 7. Triển khai lên máy thật

1. Cài `/opt/cfharness` bản có admind; tạo `_cfh_sac`; `cfharness admind install` (plist + thư mục `/var/db`, `/var/run`).
2. `cfharness admind tunnel migrate` (tunnel sang system, cấu hình sang `/etc/cfharness/tunnel.yml`) — làm ngoài giờ, có lệnh quay lại.
3. Nạp token Cloudflare (DNS + Tunnel + Access edit, chỉ các zone khai báo) → `zone.add trungtamdhs.com`, `cchchn.com`.
4. Tạo ứng dụng Access cho `superadmin.trungtamdhs.com` với danh sách email quản trị (Q8, Q9) → ingress SAC.
5. `cfharness superadmin init` → Super Admin đầu tiên (khuyến nghị thêm người thứ hai ngay).
6. `user.import_scan` → `user.import` các hub hiện có (lamlinh, hubadmin, cchcadmin…) → `domain.reconcile` dọn lệch (vd hostname
   `lamlinh.trungtamdhs.com.cchchn.com`).
7. Nâng các hub lên bản có VIS + SPEC v3 (`hub.update` hàng loạt từ SA2).

Quay lại: admind/SAC dừng không ảnh hưởng hub (NFR-SA-07); tunnel có bản cấu hình cũ + plist cũ để nạp lại.

## 8. Rủi ro triển khai

| Rủi ro | Giảm thiểu trong plan |
|---|---|
| admind (root) đọc file do user tạo | Bộ đọc gia cố có sẵn + pydantic + giới hạn kích thước; không bao giờ `eval`/shell; xem xét tách collector sang tiến trình con hạ quyền ở SA5 |
| Sửa ingress làm sập mọi hub | SA0-3: validate + thay nguyên tử + kiểm HTTP + tự quay lại; test riêng |
| `sysadminctl` cần phiên GUI / quyền Full Disk Access | Spike A3 trước; nếu cần FDA thì cấp cho binary `/opt/cfharness` của admind, ghi vào runbook |
| Token Claude lộ qua `ps` / log khi nạp | A5; chỉ qua stdin; bộ che token trong log job; test quét |
| SAC lệch trạng thái máy thật | Máy thật là nguồn sự thật; reconcile 10 phút; mọi job bắt đầu bằng `check()` |
| Hai bộ cảnh báo (hub admin + admind) báo đôi | `AdminWatcher` tự tắt khi thấy socket admind (SA1-5) |
| Khối lượng lớn, dễ kéo dài | Mỗi mốc phát hành được độc lập; SA1 đã có giá trị (nhìn toàn hệ + ESTOP + ẩn quản trị) |

## 9. Kết quả spike (điền ở SA0-1)

| Giả định | Kết quả | Quyết định |
|---|---|---|
| A1 | | |
| A2 | | |
| A3 | | |
| A4 | | |
| A5 | | |

## 10. Cần chủ repo trả lời

1. ~~Duyệt B1–B10~~ — đã duyệt 2026-11-21.
2. Q4 của BRD — vẫn chờ pháp chế/nhân sự; plan không chặn code, chỉ chặn bật trên production.
3. Danh sách email Super Admin ban đầu (≥ 2) cho Access + `superadmin init` — cần lúc triển khai (mục 7), không chặn code.
4. Bot cảnh báo: mặc định làm **bot riêng cho quản trị** (token trong secret store admind); đổi được bằng cấu hình.

## 11. Tiến độ

Spike SA0-1 cần Mac mini thật (máy dev hiện tại là Windows) → làm lúc triển khai; code dựa trên giả định A1–A5 và có runner tiêm được
để đổi lệnh theo kết quả spike mà không đổi cấu trúc.

| PR | Phạm vi | Trạng thái |
|---|---|---|
| #175 | Plan + BRD v0.2 | xong |
| #177 | SA0-2 + SA1-1: khung admind, đăng nhập, ma trận quyền, step-up, four-eyes, audit chuỗi băm, job | xong |
| #178 | SA1-2: web SAC (Access JWT, phiên, CSRF, menu theo vai trò) | xong |
| #179 | SA0-3 + SA1-3..5 + DOM-03..05: tunnel/ingress, Cloudflare API, nhập user, collector, hồ sơ, tên miền, cảnh báo, ESTOP | xong |
| #180 | SA2: vòng đời user (job tạo 11 bước…), hub, truy cập | xong |
| #181 | SA1-6: ẩn quản trị với user thường (VIS) + test quét | xong |
| #182 | Sửa quy ước repo (broad except, no_window) | xong |
| #183 | SA3: hồ sơ SPEC v3 + báo cáo ngày + Team user + Team Lead + chi phí + email | xong |
| #184 | SA4: chính sách qua admind + kho tài khoản Claude | xong |
| #185 | SA5: ESTOP hẹn gỡ, tài nguyên/lịch sử, nhập CSV, cập nhật theo đợt, báo cáo tuần/tháng, xuất hồ sơ | xong |

Chưa làm (để sau, không chặn vận hành): passkey/WebAuthn (AUTH-03 phần S — cần thư viện), Cloudflare Access cho từng hub (DOM-06, ACC-08),
danh mục app desktop qua SAC (DOM-10), thiết bị tin cậy từng cái / ghép nối Telegram-Slack / xoay token MCP (ACC-04, 06, 07), quay về bản cũ
có kiểm migration (HUB-05), sao lưu/khôi phục hub theo lịch (HUB-08 — đã có sao lưu home khi xoá), nhân bản hub (HUB-09), thư mục team qua
Không gian số tự động (TEAM-04 — hiện ghi đường dẫn, cấp tay), mẫu team (TEAM-16), tóm tắt AI (RPT-11), trang minh bạch đầy đủ phía user
(PRO-11/RPT-10 — công tắc VIS-07 đã có), xem nội dung (CNT — Q1: không làm), Windows.
