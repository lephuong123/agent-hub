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

Không migration, không event mới. Test: `tests/test_admin_overview.py`.

## Cấu hình (`admin_export.*`, chỉ người)

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `admin_export.enabled` | `false` | Bật xuất trạng thái của hub này |
| `admin_export.interval_s` | `60` | Chu kỳ ghi (10..3600 s). Người đọc coi file **quá hạn** khi cũ hơn 3× chu kỳ do hub đó khai |
| `admin_export.dir` | `/Users/Shared/cfharness/status` | Thư mục chung — hub admin cũng ĐỌC chính thư mục này |
| `admin_export.group` | `cfharness-admin` | Nhóm của file (0640). Nhóm không có / không đổi được → giữ nhóm hiện có, 0640, log cảnh báo một lần |

`config.set` ∈ HUMAN_ONLY; nhánh `admin_export` ∈ `integrity.SECURITY_KEYS` (agent sửa tay `config.yaml` → bị khôi phục). Có kiểm kiểu
(`config.validate_setting`).

## Nội dung file `<user>.json`

Ghi nguyên tử: `mkstemp` (O_EXCL, tên ngẫu nhiên) trong cùng thư mục → `fchown` nhóm → `fchmod 0640` → `fsync` → `rename`. Bộ xuất KHÔNG tự
tạo thư mục chung (thiếu → `last_error=dir_missing`, cảnh báo một lần). Daemon dừng → ghi lần cuối `daemon.up=false` (trạng thái "đã dừng").

Danh sách trắng `SPEC` (cả lúc xuất lẫn lúc đọc): chỉ số nguyên/số thực ≥ 0, bool, và chuỗi khớp regex (tên user, `hub_id` hex, phiên bản
`X.Y.Z`, mốc ISO). **Không bao giờ** có chữ tự do: prompt, summary, tiêu đề task, mô tả/tên/id agent, đường dẫn, hostname.

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

## Thiết lập (admin làm một lần, cần sudo)

```bash
# 1. Nhóm cfharness-admin + thêm người quản trị (ví dụ phuonglq)
sudo dseditgroup -o create -r "cfharness admin overview" cfharness-admin
sudo dseditgroup -o edit -a phuonglq -t user cfharness-admin

# 2. Thư mục chung: chủ = admin, nhóm cfharness-admin, setgid (file mới thừa hưởng nhóm)
sudo mkdir -p /Users/Shared/cfharness/status
sudo chown phuonglq:cfharness-admin /Users/Shared/cfharness/status
sudo chmod 2770 /Users/Shared/cfharness/status
```

Với `2770`, chỉ thành viên nhóm ghi được → **mỗi user chạy hub phải được thêm vào nhóm**:

```bash
sudo dseditgroup -o edit -a an -t user cfharness-admin
sudo dseditgroup -o edit -a binh -t user cfharness-admin
```

Lưu ý riêng tư: khi đó mọi thành viên nhóm cũng ĐỌC được file của nhau (0640, nhóm đọc). Nội dung chỉ là số liệu, nhưng nếu không muốn
người dùng thấy chi phí của nhau, dùng biến thể **sticky + chỉ ghi cho người khác** (user hub KHÔNG cần vào nhóm):

```bash
sudo chmod 3773 /Users/Shared/cfharness/status   # rwxrws-wt: người khác tạo được file nhưng không liệt kê/đọc;
                                                 # sticky: không xoá/đổi tên/ghi đè file của người khác
```

macOS (BSD) cho file mới mang nhóm của thư mục bất kể user có thuộc nhóm hay không, nên file vẫn là `cfharness-admin` 0640 → chỉ chủ file
+ admin đọc được. (Bộ xuất thử `chown` nhóm; không được thì giữ nhóm thừa hưởng, chỉ cảnh báo.)

```bash
# 3. Mỗi hub (kể cả hub admin nếu muốn tự xuất)
cfharness config set admin_export.enabled true
# tuỳ chọn: cfharness config set admin_export.interval_s 120

# 4. Hub admin: xem
cfharness admin overview          # hoặc web → Hệ thống → Tổng quan máy
```

Kiểm tra: `ls -l /Users/Shared/cfharness/status` → mỗi user một file `-rw-r----- <user> cfharness-admin`.

## An toàn với agent

- Agent của MỌI hub không đọc/ghi được `/Users/Shared/cfharness` từ Bash (sandbox `denyRead` + `denyWrite`, `guard/runtime.sandbox_settings`).
- RPC `admin.overview` ∈ HUMAN_ONLY; `cfharness admin …` bị classifier chặn thẳng với agent.

## Giới hạn / việc dở

- Đường thư mục khác mặc định (`admin_export.dir` tuỳ chỉnh) không tự vào sandbox — thêm vào `guard.sandbox.extra_deny_read|extra_deny_write`.
- Tool Read/Grep của Claude (không qua sandbox Bash) chưa có luật deny riêng cho thư mục chung; agent của admin (user thuộc nhóm) về lý
  thuyết đọc được các file số liệu này qua Read. Nội dung chỉ là số, không có chữ tự do.
- Người trong nhóm `cfharness-admin` (biến thể 2770) có thể tạo file mang tên người khác — bị phát hiện bằng kiểm chủ file (`owner_mismatch`),
  nhưng có thể chiếm chỗ tên file khiến hub thật không ghi đè được (`rename` bị chặn bởi sticky ở biến thể 3773; ở 2770 thì ghi đè được).
- Chi phí "7 ngày" là cửa sổ trượt, không phải tuần lịch. Không có lịch sử — chỉ ảnh chụp mới nhất.
- Windows: không có `pwd/grp` → bỏ kiểm chủ file/nhóm; tính năng này nhắm macOS.
