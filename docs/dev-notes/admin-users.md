# Quản trị admin cho các user cfharness trên cùng máy (plan-admin-users P1 + P2)

Kế hoạch + quyết định: `docs/dev-notes/plan-admin-users.md` (§7). Nền: `admin-overview.md` (Q6 — runbook nhóm/thư mục mới ở đó).
P3 (xem nội dung có đồng ý, `org_notice`, admind) KHÔNG làm.

Ranh giới tin cậy: hub user chạy bằng uid user, code do user cài, token Claude dùng chung → mọi cơ chế dưới đây CHẮC CHẮN với agent +
user trung thực; với user cố tình có shell chỉ PHÁT HIỆN được (stale/missing/policy_mismatch). Ép thật cần biện pháp OS (nhóm, bản cài
root `--system-install`, token riêng).

## Thành phần

| Module | Vai trò |
|---|---|
| `admin_control.py` | Nhóm (`cfharness-admin`, `cfharness-users`), `is_admin()` (gid ∈ `os.getgroups()`), đường dẫn HẰNG SỐ `shared_root()/control|policy|admin-log`, ESTOP admin bằng file cờ (`read_flags`, `set_flag`, `merge_estop`, `AdminControl` = `hub.admin_control`, `submit_gate`), admin-log (`admin_log_append|read`, `ADMIN_LOG_SPEC`) |
| `admin_policy.py` | Chính sách root-owned: `read_policy_file` (O_NOFOLLOW, file thường ≤16 KB, chủ uid 0, không ghi được bởi nhóm/khác, `POLICY_SPEC`), `effective(user)` (gộp chặt hơn), `PolicyCache` (`hub.admin_policy`, TTL 10 s), áp dụng: `budget_scope`, `cap_per_run`, `cap_concurrency`, `submit_gate`, `require_export` |
| `admin_watch.py` | `AdminWatcher` (`hub.admin_watch`): chỉ khi daemon thuộc `cfharness-admin`; `hub_codes` (thuần), `evaluate` → `admin.alert` on/resolved, chống trùng qua `hub_state.admin_watch.active`; mẫu lịch sử `admin_samples` + `history()` + `prune()` |
| `admin_users.py` | `list_users` (nhóm + plist LaunchDaemon + thư mục đã cấp + file trạng thái → `USER_STATES`), `setup_check`, `AddPlan`/`RevokePlan` (in lệnh; `--write` cần root, runner tiêm được), `runbook` |
| `admin_audit.py` | Audit METADATA: tap của `AuditLog` (`AuditLog.taps`) ghi `audit/<u>/<ngày>.jsonl`; `line_for` (danh sách trắng), `prune` (30 ngày), `read_audit` (bộ đọc admin, lọc `AUDIT_SPEC`) |
| `admin_rpc.py` | RPC `admin.*` (ngoài `admin.overview` ở admin_overview.py) + bọc `estop.set` để báo "ESTOP quản trị vẫn bật" |
| `admin_overview.py` | SPEC schema 2, `_collect_v2`, bố cục `status/<u>/<hub_id>.json` (bộ đọc vẫn đọc file phẳng cũ, nhận schema 1 và 2), `overview(hub)` |
| `admin_cli.py` | `cfharness admin overview | users list|show|add|revoke | estop on|off|list | alerts | audit | policy show|check|template | setup check [--runbook] | me` |
| `web/admin_pages.py` + `templates/admin_{users,user,alerts,audit,machine}.html` | `/admin/users[/<u>]` (biểu đồ SVG trong template, tọa độ tính ở `bars()`), `/admin/alerts`, `/admin/audit`, `/machine` (phía user); ACTIONS `admin_estop_on|off` |
| `systems/autostart.py` | `LaunchDaemonPlan(system_install=True)` → chạy `/opt/cfharness/bin/cfharness` + in lệnh cài root-owned; `cfharness admin install-daemon --system-install` |
| `secrets.py` | metadata `secrets/.meta.json` `{NAME: {set_at}}` (không giá trị) → `SecretStore.set_at()` → `claude.oauth_token_age_d` |

Migration **v23** (`admin_samples`). Event mới: `hub.admin_estop`, `admin.alert` (EVENT_LEVELS + `docs/schema/event.v1.json`).
Config mới: nhánh `admin_watch.*` (∈ `integrity.SECURITY_KEYS`, có kiểm kiểu). `notify.{slack,telegram}.events` mặc định thêm
`admin.alert`, `hub.admin_estop` (cấu hình đã ghi đè danh sách thì phải tự thêm).

## RPC (mọi method ∈ `auth.HUMAN_ONLY`, KHÔNG ∈ `MANAGER_ALLOWED`)

| Method | Phía | Ghi? | Ghi chú |
|---|---|---|---|
| `admin.overview` | admin | — | + `require_admin` (trước đây chỉ HUMAN_ONLY) |
| `admin.users`, `admin.user {user, days}` | admin | — | danh sách / chi tiết (hub, cảnh báo, lịch sử, chính sách, cờ, admin-log, audit 7 ngày) |
| `admin.estop_list`, `admin.estop_set {on, user|all, reason_code}` | admin | `estop_set` | tự audit (`MUTATING_ADMIN` ∈ `auth.all_mutating`), ghi admin-log, đánh giá cảnh báo ngay |
| `admin.alerts`, `admin.audit {user, days, limit}` | admin | — | |
| `admin.policy {user?}` | user (của mình) / admin (user khác) | — | |
| `admin.nav`, `admin.self`, `admin.setup_check` | user | — | menu, trang minh bạch, kiểm thiết lập |

## ESTOP quản trị máy

- Cờ `control/estop` (mọi user) / `control/estop.<u>`; CÓ MẶT = BẬT kể cả nội dung hỏng, thư mục, symlink treo. Nội dung chỉ
  `reason_code ∈ REASON_CODES` (`maintenance|budget|security|policy|revoked|incident|other`), mã lạ → `unknown`.
- Hub user: `AdminControl.check()` mỗi `CHECK_S`=5 s (và ở `hub.started`, trước mỗi submit) → `hub_state.admin_estop`, event `hub.admin_estop`
  {on, scope, reason_code, since}, bật: dừng run đang chạy; gỡ: tick hàng đợi.
- `RunManager.estop` = `merge_estop(estop của user, admin_estop)` → mọi chỗ đọc ESTOP (dispatcher, manager, lịch, auto-resume, review…)
  tự thấy ESTOP admin. `estop.set off` của user chỉ tắt cờ của chính user; kết quả mang `admin_estop` + `note`.
- Banner: web (`badge.admin_estop` → pill "ESTOP quản trị", trang Cài đặt ẩn nút "Tắt ESTOP" khi chỉ có cờ admin), TUI header
  "■ ESTOP QUẢN TRỊ MÁY", Telegram/Slack (`hub.admin_estop`).
- Bật/gỡ không cần sudo (quyền ghi `control/` = nhóm `cfharness-admin`); luôn ghi admin-log, gỡ → cảnh báo hồi phục `admin_estop_on` resolved.

## Cảnh báo (`admin.alert`)

Mã (`ALERT_CODES`): `stale, stopped, owner_mismatch, malformed, missing, budget_80, budget_100, tamper, auth_block, approval_waiting_long,
version_behind, policy_mismatch, admin_estop_on, revoked_running`. Payload `{user, hub_id, code, state on|resolved}` — chỉ id/enum; định dạng
tin ở `notify/slack.format_message` (Telegram dùng lại), lọc lại user/mã trước khi in. Ngưỡng: `admin_watch.approval_wait_s` (1800),
`budget_warn_pct` (80). `policy_mismatch` = hub `ok` báo `policy.valid=false` hoặc `rev_applied` ≠ rev mà admin đọc được cho user đó.

## Chính sách root (`policy/`)

```json
{"rev": 3, "policy_required": false, "budget": {"usd_per_day": 20, "usd_per_run_max": 5}, "runs": {"max_concurrent": 2},
 "require": {"sandbox": true, "confine": false, "export": true}, "models": {"allow": ["sonnet"]}, "min_version": "0.2.8"}
```
`cfharness admin policy template` in mẫu + lệnh `sudo install -m 0644 -o root -g wheel`. Hiệu lực = `_machine` ∧ (`<u>` hoặc `_default`),
lấy giá trị chặt hơn, rồi chặt hơn nữa với cấu hình hub: scope ngân sách `admin` (hub-wide, giữ chỗ H-19), `min(per_run)`, `min(đồng thời)`;
submit `Blocked` khi vi phạm `require.sandbox|confine`, `min_version`, `models.allow` (khớp đúng tên hoặc tiền tố `<tên>-`); `require.export`
ép bật bộ xuất. Fail-open + cảnh báo khi file hỏng/sai chủ/ghi được bởi người khác; fail-closed khi `_machine.json` hợp lệ có
`policy_required: true` mà phần của user hỏng/thiếu.

## Audit metadata (`audit/<u>/<YYYY-MM-DD>.jsonl`)

Mỗi dòng CHỈ `ts, kind, actor, channel, outcome` (+ `key` cho `config.set` — tên khoá thuộc `DEFAULTS`, đuôi lạ cắt về nhánh gốc; KHÔNG giá trị).
`kind` ∈ `auth.all_mutating() ∪ HUMAN_ONLY ∪ EXTRA_KINDS`; `actor` enum (bỏ id agent), `channel` enum (bỏ `:<chat_id>`). Ghi khi bộ xuất bật và
thư mục `audit/<u>/` đã được cấp; trần 5000 dòng / 1 MB mỗi ngày; xoá file > 30 ngày. Bộ đọc: O_NOFOLLOW, chủ file = user, lọc lại `AUDIT_SPEC`.

## Lịch sử + biểu đồ

`admin_samples` (hub admin): mỗi `admin_watch.sample_interval_s` (300 s) một dòng/hub (chi phí hôm nay/tháng, run chạy/chờ, lỗi hôm nay,
chờ duyệt, % ngân sách); xoá > `history_days` (90). `/admin/users/<u>`: cột chi phí theo ngày (max cost_today mỗi hub mỗi ngày, cộng các hub) +
run đang chạy theo giờ (24 h). SVG vẽ trong template từ tọa độ số (`web/admin_pages.bars`), style ở `static/app.css` (`.chart-*`).

## Bất biến

- Không chữ tự do hai chiều: bản xuất/audit/admin-log/cờ ESTOP/chính sách/cảnh báo đều qua danh sách trắng (SPEC/AUDIT_SPEC/ADMIN_LOG_SPEC/
  POLICY_SPEC/REASON_CODES/ALERT_CODES) cả lúc ghi lẫn lúc đọc. Thêm trường → spec + ca trong test.
- Đường dẫn `control/`, `policy/`, `admin-log/` là hằng số (`SHARED_ROOT`); chỉ test đổi được (`CFHARNESS_TESTING=1` + `CFHARNESS_TEST_SHARED_ROOT`).
- Mọi RPC `admin.*` mới → `auth.HUMAN_ONLY` + `admin_rpc.READ_ADMIN|MUTATING_ADMIN` + ca trong `tests/test_sec_auth.py`; không vào `MANAGER_ALLOWED`.
- Agent không đọc/ghi `SHARED_ROOT`: sandbox + `permissions.deny` (mọi hub, kể cả trust_full) + classifier `admin_shared` (forbidden, không hạ cấp).
- Daemon vẫn là writer duy nhất của `hub.db`; `AdminWatcher` đọc file trong thread, ghi DB/bus trên loop.
- Test: `tests/test_admin_{control,policy,watch,users,audit}.py`, `tests/test_web_admin.py`, ca trong `test_admin_overview.py`, `test_sec_auth.py`,
  `test_guard_classifier.py`. Fixture `as_admin` (conftest) giả nhóm; mặc định test KHÔNG thuộc nhóm nào (máy dev có nhóm thật cũng không ảnh hưởng).

## Lệch kế hoạch

- `admin-log/` là `root:cfharness-admin 2775`, file 0664 (kế hoạch: root:wheel 0644) — để daemon admin ghi được khi bật/gỡ ESTOP không sudo
  (quyết định §7.3). Hệ quả: admin khác trong nhóm sửa được log (admin được tin).
- `control/` là `2775` không sticky (kế hoạch: 0775+sticky) — sticky làm admin B không gỡ được cờ admin A tạo.
- `admin_estop_on` là cảnh báo "cờ đang bật cho user" (không phải "hub chưa áp cờ"); gỡ cờ = cảnh báo hồi phục.
- `admin.estop_set` trên web chưa có step-up TOTP khi web public (chỉ xác nhận); `/admin/audit|alerts` không có menu riêng (link từ trang Người dùng máy).
- `policy set` không có: CLI/web chỉ `policy template` (in mẫu + lệnh sudo) — đúng quyết định §7.10.
- Chi phí theo agent không xuất (quyết định §7.6).

## Việc dở

- Step-up TOTP cho ESTOP admin trên web public.
- `cfharness update` khi cài `--system-install` không nâng được (root-owned) — admin chạy lại lệnh cài với tag mới; chưa có lệnh riêng.
- `models.allow` không kiểm được khi agent dùng model mặc định của CLI (không đặt `model`) — cho qua.
- `list_users` đọc `gr_mem` (thành viên khai báo qua dseditgroup); thành viên qua nhóm chính (PrimaryGroupID) không hiện.
- Chưa có màn TUI cho admin (chỉ banner ESTOP admin phía user).
