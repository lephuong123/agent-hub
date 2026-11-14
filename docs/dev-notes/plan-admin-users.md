# Plan — Quản trị admin cho các user cfharness trên cùng máy

Trạng thái: KẾ HOẠCH (2026-11-10), chưa triển khai. Nền: Q6 `admin-overview.md`; liên quan `web-public.md`, `confine.md`.

## 0. Kết luận chính
1. Giữ mô hình **kéo từ file** của Q6 (không socket chéo user). Thiếu: kênh admin → user (chính sách, ESTOP), lịch sử, cảnh báo,
   và phân định admin (biến thể 2770 bắt mọi user vào `cfharness-admin`).
2. **Ranh giới tin cậy:** hub user chạy bằng uid user, code user tự cài (plist trỏ `~/.local/bin/cfharness`, `autostart.py:502`),
   token Claude dùng chung. Ép chính sách chỉ **chắc chắn** với agent + user trung thực; với user cố tình có shell chỉ **phát hiện**
   được. Ép thật cần biện pháp OS (nhóm, bản cài root, launchd, token riêng).
3. **Bất đối xứng:** siết (bật ESTOP) dễ; nới (gỡ ESTOP, nới ngân sách, xem nội dung) khó — sudo, xác nhận, nhật ký user thấy.

## 1. Thiếu so với Q6
User tự tắt xuất được; admin/user chung nhóm; chỉ ảnh chụp mới nhất, không cảnh báo; SPEC mỏng (guard, tamper, token Claude,
phiên bản, đăng nhập sai, ngân sách, đĩa); audit chỉ trong `hub.db` từng user và có chữ tự do; không có chiều xuống;
tool Read/Grep của agent admin đọc được thư mục chung (`admin-overview.md:113-114`); chương trình LaunchDaemon do user sở hữu.

## 2. "Theo dõi mọi thứ" — 3 tầng
**(a) Số liệu — SPEC schema 2** (bộ đọc nhận cả 1 và 2): chi phí tháng, hạn mức/phần trăm ngân sách; run chờ/chạy lâu nhất, tách lỗi;
approval chờ lâu nhất; guard forbidden/asked/tamper, integrity, sandbox, confine, số agent trust_full; claude auth_ok, cli_version,
cli_tested, tuổi OAuth token; daemon uptime/launchd/update; web public/totp/đăng nhập sai/locked, kênh chat; lịch; dung lượng đĩa;
(P2) `policy.rev_applied`, `admin_estop.seen`. Tuỳ chọn chi phí theo agent bằng id băm.

**(b) Audit metadata (P2):** `audit/<u>/<ngày>.jsonl`, mỗi dòng CHỈ `ts, kind (danh sách trắng), actor (enum, không id agent),
channel (enum, bỏ :<chat_id>), outcome`; `config.set` chỉ tên khoá thuộc `DEFAULTS`, không giá trị. Không summary/data/target.
Trần dòng/KB/ngày giữ; bộ đọc lọc lại bằng `AUDIT_SPEC`.

**(c) Nội dung (prompt/transcript): mặc định KHÔNG** — phá bất biến "không chữ tự do", rủi ro secret/dữ liệu cá nhân, prompt
injection. Nếu cần (P3): "xin xem từng vụ" (user duyệt 2 bước, xuất bản đã che của đúng run, TTL, audit hai phía) hoặc
`org_notice` (chỉ khi có quyết định pháp lý/nhân sự).

## 3. Quản lý user
**Nhóm & thư mục:** `cfharness-users` (được dùng) tách khỏi `cfharness-admin` (quản trị; bỏ biến thể 2770).
```
/Users/Shared/cfharness/          root:wheel 0755
  status/                         root:cfharness-admin 0751
    <u>/status.json               <u>:cfharness-admin 2750 / 0640   (root tạo khi cấp quyền → hết chiếm tên)
  audit/<u>/                      (P2)
  policy/{_machine,_default,<u>}.json   root:wheel 0644   (P2, chỉ root ghi)
  control/estop, estop.<u>        root:cfharness-admin 0775+sticky  (ESTOP)
  admin-log/<u>.jsonl             root:wheel 0644 (user đọc được thao tác admin)
```
Đường dẫn `policy/` và `control/` là **hằng số trong code** (không cấu hình, kẻo user trỏ đi nơi khác).

**Danh sách:** `admin users list` ghép thành viên nhóm (runner giả được), plist LaunchDaemon, file trạng thái, thư mục đã cấp →
`chưa cài | đã cài chưa xuất | ok | quá hạn | sai chính sách | bị thu quyền còn chạy`.

**Cấp/thu** (mẫu `install-daemon`: in lệnh, `--write` cần root): add = vào nhóm → tạo thư mục → install-daemon → policy mặc định
→ in bước tiếp (token, totp). revoke = cờ ESTOP user → bootout → gỡ nhóm → admin-log; không xoá HUB_HOME. Ép thật: bản cài
`/opt/cfharness` root-owned (`--system-install`).

**Chính sách ép xuống (P2):** file root-owned 0644, đọc như `read_file` + chủ uid 0 + không ghi được bởi nhóm/khác, lọc
`POLICY_SPEC` (`rev, budget.usd_per_day, budget.usd_per_run_max, runs.max_concurrent, require.{sandbox,confine,export},
models.allow, min_version`). Áp dụng **lấy giá trị chặt hơn**: scope `admin` trong `budget_scopes` (`runs.py:143-159`, giữ chỗ
H-19 tự áp), `min` ở `_budget_for`/`effective_limit`, `submit` chặn `Blocked` khi vi phạm require/min_version,
`require.export` ép bật exporter. Bản xuất gửi lại `policy.rev_applied`/`valid` → chênh lệch thì cảnh báo.

**ESTOP admin (P1):** file `control/estop[.<u>]` có mặt = bật (kể cả hỏng). Daemon kiểm mỗi 5–10 s, lưu `hub_state.admin_estop`,
`submit` kiểm trước `self.estop` (`runs.py:232-234`), `set_estop(off)` của user không gỡ được, `stop_running` dừng run,
event `hub.admin_estop`, banner web/TUI. Thông điệp chỉ `reason_code` enum (không chữ tự do admin tới agent).

**Cảnh báo (P1):** `AdminWatcher` trong hub admin (chỉ khi thuộc `cfharness-admin`) so trạng thái → event `admin.alert{user, code}`
(`stale, stopped, owner_mismatch, malformed, missing, budget_80, budget_100, tamper, auth_block, approval_waiting_long,
version_behind, policy_mismatch, admin_estop_on`), qua Telegram/Slack sẵn có, chống trùng + báo hồi phục; ngưỡng
`admin_watch.*` ∈ SECURITY_KEYS.

## 4. Kiến trúc
- Không socket/HTTP chéo hub; quyền POSIX là xác thực. `cfharness-admind` (root) — chưa nên làm.
- Admin = quyền file thật + `is_admin()` (gid `cfharness-admin` ∈ `os.getgroups()`) cho UI/RPC; thao tác root kiểm `euid==0`.
- CLI: `admin overview | users [list|show|add|revoke] | policy show|set|check | estop on|off [U|--all] | audit | alerts | setup check`.
  RPC mới ∈ `HUMAN_ONLY` (+ `tests/test_sec_auth.py`), không vào `MANAGER_ALLOWED`.
- Web admin: `/admin/users[/<u>]`, `/admin/audit`, `/admin/alerts`; ghi duy nhất = ESTOP (`ACTIONS` confirm, step-up TOTP khi public);
  sửa chính sách chỉ hiện lệnh sudo. Web user: trang "Quản trị máy" chỉ đọc (xem trước JSON xuất, chính sách, ESTOP, admin-log).

| Đe doạ | Giảm thiểu |
|---|---|
| User A giả file B | kiểm chủ file (có) + chủ thư mục do root cấp |
| User nói dối / tắt xuất | `require.export` + cảnh báo stale/missing; bản cài root; chấp nhận là phát hiện |
| User nới chính sách / gỡ ESTOP | file root/nhóm admin, đường dẫn cứng, `estop off` không gỡ cờ admin |
| Agent đọc/ghi dữ liệu admin | sandbox (có) + **thêm** `permissions.deny` Read/Grep/Glob/Edit/Write và classifier forbidden cho `SHARED_ROOT` |
| Agent của admin lạm quyền | luật deny trên cho mọi hub; `policy/` chỉ root; tệ nhất agent bật ESTOP (có cảnh báo khi gỡ) |
| Admin xem dữ liệu riêng | mặc định không nội dung; admin-log user đọc được; nội dung chỉ qua đồng ý; root vẫn toàn quyền |
| Prompt injection chéo | không chữ tự do hai chiều |

## 5. Giai đoạn
**P1 (~3 ngày, không migration):** tách nhóm + `admin setup check` + bố cục `status/<u>/` (vẫn đọc bố cục cũ); SPEC v2;
`admin users list` + `/admin/users`; ESTOP admin; `AdminWatcher` + `admin.alert` (EVENT_LEVELS + sinh lại schema); vá Read/Grep
`SHARED_ROOT`; kiểm nhóm admin ở RPC/menu.
**P2:** chính sách root (`admin_policy.py`) + lớp chặt-hơn trong `RunManager`; audit metadata; lịch sử `admin_samples`
(migration mới cuối `MIGRATIONS`, giữ 90 ngày) + biểu đồ; trang minh bạch phía user + admin-log; `admin users add|revoke`,
`install-daemon --system-install`.
**P3:** xem nội dung có đồng ý; `org_notice`; tuỳ chọn admind / token riêng từng user / đối chiếu chi phí.

File mới: `admin_control.py`, `admin_watch.py`, `admin_users.py`, `admin_policy.py`, `admin_audit.py`. Sửa: `admin_overview.py`,
`admin_cli.py`, `runs.py`, `events.py` + schema, `auth.py`, `guard/runtime.py`, `guard/classifier.py`, `confine.py`, `config.py`,
`guard/integrity.py`, `systems/autostart.py`, `web/admin_pages.py`, `store.py` (P2).
Test: `test_admin_overview.py` (mở rộng), `test_admin_control.py`, `test_admin_watch.py`, `test_admin_users.py`,
`test_admin_policy.py`, `test_admin_audit.py`, `test_sec_auth.py`, `test_guard_classifier.py`, `test_confine.py`, web, schema.
Không gọi claude/sudo/launchctl thật (tiêm `owner_of`, runner giả).

Runbook vận hành: tạo `cfharness-users`, chuyển user, gỡ user khỏi `cfharness-admin`, tạo `status/<u>/`, `status/` 0751,
khởi động lại daemon (InitGroups).

## 6. Câu hỏi mở
1. Chỉ ép với agent + user trung thực (đủ P1–P2), hay chống cả user cố tình (cần bản cài root + bỏ token Claude dùng chung)?
2. Chính sách hỏng/sai chủ: fail-closed hay fail-open + cảnh báo?
3. Gỡ ESTOP admin: nhóm admin không cần sudo, hay bắt buộc sudo?
4. Có cần xem nội dung? "đồng ý từng vụ" hay "thông báo tổ chức"?
5. User có được thấy chi phí người khác? (đề xuất: không)
6. Chi phí theo agent (id băm) hay chỉ tổng?
7. Lưu `set_at` cho secret để xuất tuổi OAuth token?
8. Kênh cảnh báo admin: bot của hub admin hay kênh riêng?
9. Thời gian giữ lịch sử (90 ngày?) / audit metadata (30 ngày?).
10. Web admin sửa chính sách (cần admind) hay chỉ hiện lệnh sudo?
11. Nhiều hub trên một user OS: đổi sang `status/<u>/<hub_id>.json` ngay P1?

## 7. Quyết định của chủ repo (2026-11-10): làm P1 + P2 theo đề xuất
1. Mức tin cậy: ép với agent + user trung thực; có tuỳ chọn `install-daemon --system-install` (P2) nhưng không bắt buộc.
2. Chính sách hỏng/sai chủ: fail-open + cảnh báo; fail-closed chỉ khi `_machine.json` có `policy_required: true`.
3. ESTOP admin: bật/gỡ không cần sudo (nhóm `cfharness-admin`), gỡ luôn ghi admin-log + cảnh báo.
4. KHÔNG xem nội dung (P3 hoãn).
5. User không thấy chi phí người khác. 6. Chỉ tổng theo user (không chi phí theo agent).
7. Lưu metadata `set_at` cho secret để xuất tuổi OAuth token. 8. Cảnh báo qua bot Telegram/Slack của hub admin.
9. Lịch sử 90 ngày, audit metadata 30 ngày. 10. Web chỉ hiện lệnh sudo cho chính sách.
11. Dùng `status/<u>/<hub_id>.json` ngay từ P1.
