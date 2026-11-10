# Tổng quan máy cho người quản trị (Q6 — plan-multi-instance §6b)

Một Mac mini, mỗi người một user macOS + một hub cfharness riêng (`HUB_HOME` 0700 — người khác không đọc được). Người quản trị
(cũng là một user chạy hub) cần một màn tổng quan MỌI hub: chi phí hôm nay/7 ngày, run đang chạy/chờ/lỗi hôm nay, số chờ duyệt/câu hỏi,
số agent, ESTOP, daemon còn chạy + phiên bản, lần cập nhật cuối.

Cách làm: mỗi hub tự **xuất** bản tóm tắt CHỈ SỐ LIỆU ra một thư mục chung; hub của admin **đọc** thư mục đó.

| Thành phần | Vai trò |
|---|---|
| `admin_overview.py` | `AdminExporter` (`hub.admin_export`, vòng nền từ `hub.started`), `collect`, `write_atomic`, `sanitize(SPEC)`, `read_dir`, RPC `admin.overview` |
| `admin_cli.py` | `cfharness admin overview [--json]` |
| `web/admin_pages.py` + `templates/admin_overview.html` | Trang "Tổng quan máy" `/admin/overview` (chỉ đọc); mục menu "Hệ thống → Tổng quan máy" chỉ hiện khi thư mục chung tồn tại |
| `reports.outcome_counts` | Số run kết thúc theo outcome từ một mốc (dùng cho "lỗi hôm nay") |

Phần mở rộng (nhóm `cfharness-users`, bố cục `status/<u>/<hub_id>.json`, schema 2, ESTOP admin, cảnh báo, chính sách root, audit
metadata, lịch sử): `docs/dev-notes/admin-users.md`. Test: `tests/test_admin_overview.py`, `tests/test_admin_*.py`.

## Cấu hình (`admin_export.*`, chỉ người)

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `admin_export.enabled` | `false` | Bật xuất trạng thái của hub này |
| `admin_export.interval_s` | `60` | Chu kỳ ghi (10..3600 s). Người đọc coi file **quá hạn** khi cũ hơn 3× chu kỳ do hub đó khai |
| `admin_export.dir` | `/Users/Shared/cfharness/status` | Thư mục chung — hub admin cũng ĐỌC chính thư mục này |
| `admin_export.group` | `cfharness-admin` | Nhóm của file (0640). Nhóm không có / không đổi được → giữ nhóm hiện có, 0640, log cảnh báo một lần |

`config.set` ∈ HUMAN_ONLY; nhánh `admin_export` ∈ `integrity.SECURITY_KEYS` (agent sửa tay `config.yaml` → bị khôi phục). Có kiểm kiểu
(`config.validate_setting`).

## Nội dung file trạng thái (`<user>/<hub_id>.json`, cũ: `<user>.json`)

Ghi nguyên tử: `mkstemp` (O_EXCL, tên ngẫu nhiên) trong cùng thư mục → `fchown` nhóm → `fchmod 0640` → `fsync` → `rename`. Bộ xuất KHÔNG tự
tạo thư mục chung (thiếu → `last_error=dir_missing`, cảnh báo một lần). Daemon dừng → ghi lần cuối `daemon.up=false` (trạng thái "đã dừng").

Danh sách trắng `SPEC` (cả lúc xuất lẫn lúc đọc): chỉ số nguyên/số thực ≥ 0, bool, và chuỗi khớp regex (tên user, `hub_id` hex, phiên bản
`X.Y.Z`, mốc ISO). **Không bao giờ** có chữ tự do: prompt, summary, tiêu đề task, mô tả/tên/id agent, đường dẫn, hostname.

Hub ghi `<dir>/<user>/<hub_id>.json` khi admin đã cấp thư mục riêng `<dir>/<user>/` (root tạo, chủ = user); chưa có → bố cục phẳng cũ
`<dir>/<user>.json`. Bộ đọc nhận cả hai bố cục và cả schema 1 lẫn 2 (schema 2 thêm chi phí tháng, ngân sách, guard, claude, web, chat,
lịch, đĩa, chính sách, ESTOP admin — danh sách đầy đủ ở `SPEC` và `admin-users.md`).

```json
{"schema":1,"user":"an","hub_id":"<sha256(realpath HUB_HOME)[:16]>","version":"0.2.3",
 "generated_at":"2026-11-07T08:00:00Z","generated_ts":1794..., "interval_s":60,
 "daemon":{"up":true,"started_at":"…"},"estop":{"on":false,"teams":0},
 "cost_usd":{"today":1.23,"week":8.9},
 "runs":{"running":1,"queued":0,"max_concurrent":4,"today_total":12,"today_completed":10,"today_failed":1,"today_cancelled":1},
 "approvals":{"open":2,"waiting_human":1},"questions":{"open":1,"with_manager":0},"agents":{"total":5},
 "flags":{"auth_block":false,"rate_limited":false,"sandbox_enabled":true}}
```

- "Hôm nay" = từ 0h giờ máy; "7 ngày" = 7×24 h trượt. `runs.today_failed` = outcome `failed|crashed|timed_out|budget_exceeded|rate_limited|protocol_violation`.

## Đọc (hub admin)

`cfharness admin overview [--json]` / web `/admin/overview` → RPC `admin.overview` (∈ `auth.HUMAN_ONLY`: chỉ đọc nhưng lộ hoạt động của user
khác; `cfharness admin` ∈ `HUB_HUMAN_ONLY_CLI`). File của người khác là DỮ LIỆU KHÔNG TIN CẬY:

- mở `O_NOFOLLOW|O_NONBLOCK`, chỉ file thường ≤ 64 KB, ≤ 500 file, bỏ file ẩn/tên không hợp lệ;
- JSON hỏng / không phải object / schema lạ / symlink / thư mục → trạng thái `malformed` kèm mã lỗi, không ném;
- giá trị lọc lại qua `SPEC` (khoá lạ, chữ tự do bị bỏ — web vẫn autoescape);
- chủ file (uid) phải trùng tên file VÀ trường `user` → không thì `owner_mismatch` (không hiện số liệu): user A không giả được file của B.

Trạng thái mỗi hub: `ok` · `stale` (quá 3× chu kỳ — daemon có thể đã chết) · `stopped` (daemon dừng êm) · `malformed` · `owner_mismatch`.
Tổng cộng chi phí tính mọi hub có số liệu; run đang chạy/chờ chỉ cộng hub `ok`.

## Thiết lập (admin làm một lần, cần sudo) — runbook mới (plan-admin-users)

Tách hai nhóm: `cfharness-admin` (người QUẢN TRỊ — đọc trạng thái mọi user, bật/gỡ ESTOP) và `cfharness-users` (người được dùng hub).
Người dùng thường KHÔNG vào `cfharness-admin` (biến thể 2770 cũ bắt mọi user vào nhóm admin → họ đọc được số liệu của nhau).
`cfharness admin setup check` kiểm toàn bộ dưới đây và in lệnh sửa; `cfharness admin setup check --runbook` in nguyên bộ lệnh.

```bash
# 1. Nhóm + người quản trị (ví dụ phuonglq)
sudo dseditgroup -o create -r "cfharness quản trị máy" cfharness-admin
sudo dseditgroup -o create -r "cfharness người dùng" cfharness-users
sudo dseditgroup -o edit -a phuonglq -t user cfharness-admin

# 2. Thư mục chung (đường dẫn control/, policy/, admin-log/ là HẰNG SỐ trong code)
R=/Users/Shared/cfharness
sudo mkdir -p $R/status $R/audit $R/control $R/policy $R/admin-log
sudo chown root:wheel $R $R/policy                 && sudo chmod 755 $R $R/policy
sudo chown root:cfharness-admin $R/status $R/audit && sudo chmod 751 $R/status $R/audit   # admin liệt kê; user chỉ đi vào thư mục riêng
sudo chown root:cfharness-admin $R/control $R/admin-log && sudo chmod 2775 $R/control $R/admin-log  # admin ghi; mọi user đọc

# 3. Mỗi user (in lệnh; thêm --write khi chạy bằng sudo): nhóm users + status/<u>/ + audit/<u>/ (u:cfharness-admin 2750) + LaunchDaemon
cfharness admin users add an
sudo cfharness admin users add an --write
sudo -u an cfharness config set admin_export.enabled true   # hoặc require.export trong chính sách

# 4. Khởi động lại daemon của admin để tiến trình nhận nhóm mới (launchd InitGroups / đăng xuất-đăng nhập)
cfharness admin users list     # hoặc web → Hệ thống → Người dùng máy
```

| Đường dẫn | Chủ:nhóm | Quyền | Ai ghi / ai đọc |
|---|---|---|---|
| `/Users/Shared/cfharness/` | root:wheel | 0755 | — |
| `status/` | root:cfharness-admin | 0751 | admin liệt kê; user không liệt kê được, chỉ đi vào `status/<u>/` |
| `status/<u>/<hub_id>.json` | `<u>`:cfharness-admin | 2750 / 0640 | hub của u ghi; admin đọc. Root tạo thư mục khi cấp quyền → không ai chiếm tên |
| `audit/<u>/<ngày>.jsonl` | `<u>`:cfharness-admin | 2750 / 0640 | audit METADATA (P2) |
| `control/estop`, `control/estop.<u>` | root:cfharness-admin | 2775 / 0644 | admin bật/gỡ (không cần sudo); hub user chỉ đọc |
| `policy/{_machine,_default,<u>}.json` | root:wheel | 0755 / 0644 | CHỈ root ghi |
| `admin-log/<u>.jsonl`, `_all.jsonl` | root:cfharness-admin | 2775 / 0664 | admin ghi khi bật/gỡ ESTOP, cấp/thu quyền; user ĐỌC được |

### Chuyển từ bố cục Q6 cũ (`status/` 2770/3773, file phẳng `<u>.json`)

```bash
sudo chown root:cfharness-admin /Users/Shared/cfharness/status && sudo chmod 751 /Users/Shared/cfharness/status
sudo dseditgroup -o edit -d an -t user cfharness-admin          # gỡ user thường khỏi nhóm admin
sudo cfharness admin users add an --write --no-daemon           # tạo status/an/ + audit/an/ (và vào cfharness-users)
sudo rm -f /Users/Shared/cfharness/status/an.json                # sau khi hub của an đã ghi status/an/<hub_id>.json
```
Bộ đọc vẫn đọc file phẳng cũ trong lúc chuyển (cùng kiểm chủ file). Hub chỉ ghi bố cục mới khi `status/<u>/` đã tồn tại.

## An toàn với agent

- Agent của MỌI hub không đọc/ghi được `/Users/Shared/cfharness` từ Bash (sandbox `denyRead` + `denyWrite`, `guard/runtime.sandbox_settings`).
- Tool Read/Grep/Glob/Edit/Write (không qua sandbox Bash): `permissions.deny` tĩnh `guard/runtime.admin_shared_deny_rules()` cho mọi hub, kể
  cả agent trust_full; classifier `admin_shared` (forbidden) cho Read/Grep/Glob/Write/Edit/Bash chạm thư mục này.
- RPC `admin.*` ∈ HUMAN_ONLY (+ nhóm `cfharness-admin` cho phía admin); `cfharness admin …` bị classifier chặn thẳng với agent.

## Giới hạn / việc dở

- Đường thư mục khác mặc định (`admin_export.dir` tuỳ chỉnh) không tự vào sandbox — thêm vào `guard.sandbox.extra_deny_read|extra_deny_write`.
- (Đã xử lý ở plan-admin-users) Read/Grep thư mục chung: luật deny + classifier. Chiếm tên file: bố cục `status/<u>/` do root tạo.
  Lịch sử: bảng `admin_samples` của hub admin (90 ngày).
- Bố cục phẳng cũ (`<u>.json`) vẫn có rủi ro chiếm tên ở biến thể 2770 — chuyển sang `status/<u>/`.
- Chi phí "7 ngày" là cửa sổ trượt, không phải tuần lịch.
- Windows: không có `pwd/grp` → bỏ kiểm chủ file/nhóm; tính năng này nhắm macOS.
