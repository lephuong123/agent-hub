# Super Admin Console (SAC) + admind

Kế hoạch + quyết định: `docs/dev-notes/plan-superadmin.md` · BRD: `docs/BRD-Super-Admin-Console.md` (v0.2).
Thay cho quản trị bằng `sudo` + `scripts/setup-hub-user.sh` (script vẫn giữ làm đường dự phòng).

## Thành phần

| Thành phần | Chạy bằng | Mã nguồn | Vai trò |
|---|---|---|---|
| admind | root, `/Library/LaunchDaemons/com.cfharness.admind.plist` | `src/cfharness/admind/` | Socket Unix `/var/run/cfharness/admind.sock` (root:_cfh_sac 0660), sổ mã thao tác, `sac.db`, vault secret, job, việc định kỳ |
| Web SAC | `_cfh_sac`, `com.cfharness.sac`, `127.0.0.1:8700` | `src/cfharness/superadmin/` | Giao diện mỏng: Host + JWT Cloudflare Access + phiên + CSRF → mọi thao tác gửi admind |
| Hub user | uid user (không đổi kiến trúc) | `admin_profile.py`, `team_inbox.py`, `web/inbox_pages.py`, `admin_control.visibility` | Xuất `profile.json` + báo cáo ngày, hộp thư việc team, ẩn quản trị (VIS) |

```
/var/db/cfharness-superadmin/   root 0700: sac.db (0600), secrets/ (vault, mỗi secret một file 0600), backups/, jobs/
/var/run/cfharness/              root:_cfh_sac 0750: admind.sock
/var/lib/cfharness-sac/          _cfh_sac 0700: log web SAC
/Users/Shared/cfharness/         status/ report/ audit/ policy/ control/(estop, visibility.json) teams/<slug>/{inbox,status}/<u>/
```

## Bất biến bảo mật

- admind KHÔNG nghe mạng; chỉ nhận uid root và `_cfh_sac` (uid do kernel báo — `server.peer_uid`). Không thao tác nào nhận chuỗi lệnh; tham số
  pydantic `extra=forbid`, chuỗi qua regex (`ptypes.py`); runner chỉ chạy argv thuộc danh sách trắng (`runner.ALLOWED`).
- Pipeline (`dispatch.py`): phiên → **vai trò trước khi kiểm tham số** → phạm vi (nhóm/zone/team) → step-up (mức Cao, 5 phút) → four-eyes (mức Tới hạn,
  `precheck` trước khi tạo yêu cầu) → chạy → audit (kể cả bị từ chối). Ma trận `rbac.MATRIX` dùng chung cho admind (kiểm thật) và SAC (menu).
- Nhật ký quản trị chuỗi băm (`audit.py`, kiểm hằng ngày → cảnh báo `tamper`); tham số che theo tên khoá (`password`, `token`, `code`…).
- Secret (token Claude/Cloudflare/bot, TOTP quản trị, link TOTP user, token job) không vào DB/log/audit; token Claude tới hub chỉ qua **stdin**.
- Dữ liệu từ hub là không tin cậy: bộ đọc O_NOFOLLOW, giới hạn kích thước, kiểm chủ file, lọc lại cấu trúc; template autoescape; không đưa vào LLM.
- Web SAC: Host đúng `sac.hostname` (421), JWT Access RS256 + `aud` (thiếu/sai → 404 trơn "Not Found"), email đăng nhập = email Access, cookie
  `__Host-sac` Secure/HttpOnly/SameSite=Strict, CSRF + Origin, CSP không inline. Access chưa cấu hình → chỉ nhận Host loopback.
- VIS (user thường không thấy gì về quản trị): `/admin/*`, `/machine` → 404 giống route lạ; RPC `admin.*` → "không có method"; CLI `admin`,
  `admind`, `superadmin` không đăng ký; banner/tin ESTOP chỉ "Hub đang tạm dừng — liên hệ quản trị". Kiểm bằng `tests/test_vis_scan.py`.
- Hộp thư việc team: hub CHỈ ĐỌC; nội dung việc tới agent chỉ khi người bấm Nhận (BR-SA-02).
- Mức xem `Hoạt động` (tiêu đề việc, câu kết quả) mặc định TẮT; bật phải nhập số văn bản quy định (NFR-SA-10) — admind ghi `control/visibility.json`,
  hub chỉ xuất trường mức đó khi bật.

## Việc định kỳ của admind (`scheduler.py`)

| Mỗi | Việc |
|---|---|
| 30 s | gửi cảnh báo mới/hồi phục (`notify.flush`), đồng bộ bảng việc team (`ops_teams.sync`) |
| 60 s | thu thập `status/` (`collector.collect`), chạy yêu cầu four-eyes đến hạn, gỡ ESTOP hẹn giờ, gom báo cáo ngày + gửi lúc `report.send_time` |
| 10 phút | kiểm sức khoẻ tên miền + đối chiếu lệch (`health.check_domains`) |
| 1 ngày | kiểm chuỗi băm nhật ký, tuổi token Claude |

## Tên miền

CNAME tạo bằng API Cloudflare theo **zone_id đúng zone** (không còn `cloudflared route dns` dùng `cert.pem`). Ingress (`cf.ingress_path`): ghi file tạm →
`cloudflared tunnel --config <tạm> ingress validate` → giữ `.bak-<giờ>` → thay nguyên tử → `launchctl kickstart -k system/com.cfharness.tunnel` →
(tuỳ chọn) kiểm HTTP, lỗi thì khôi phục. Cấm trỏ vào cổng MCP. Tên dành riêng: `domain.reserved`.

## Job

`jobs.py`: bước có `check()/apply()/undo()`; chạy lại bỏ qua bước đã xong; admind khởi động lại → job `running` thành `failed` + `resume_from`;
tối đa 3 job song song, một đối tượng một job. Loại job: `user.create` (11 bước, port từ `setup-hub-user.sh`), `user.suspend|resume|revoke|restore|
delete|backup`, `domain.create|revoke|rename`, `hub.update`, `hub.rollout`.

## Triển khai lên máy thật (lần đầu)

```bash
# 0. Bản cài root-owned (admind không chạy code user ghi được)
#    lệnh cài /opt/cfharness: in bởi `cfharness admin install-daemon --user <u> --system-install` (docs/dev-notes/admin-users.md)
# 1. User _cfh_sac + plist admind/SAC
sudo /opt/cfharness/bin/cfharness admind install --write
sudo /opt/cfharness/bin/cfharness admind status
# 2. Super Admin đầu tiên (in link kích hoạt MỘT lần) — khuyến nghị thêm người thứ hai ngay
sudo /opt/cfharness/bin/cfharness superadmin init --email <email> --name "<Họ tên>"
#    Trước khi có Access: mở http://127.0.0.1:8700/enroll/<mã> trên chính máy chủ
```
3. Trên SAC (loopback): Cấu hình → nạp `cf.token` (DNS edit + Tunnel edit + Access edit, chỉ các zone dùng), đặt `cf.account_id`, `cf.tunnel_id`,
   `cf.ingress_path`; Tên miền → Khai zone (`trungtamdhs.com`, `cchchn.com`).
4. Chuyển tunnel sang system domain (giả định A4): chép cấu hình sang `/etc/cfharness/tunnel.yml`, plist `com.cfharness.tunnel` vào
   `/Library/LaunchDaemons`, `launchctl bootstrap system …`.
5. `domain.sac_setup` (Cấu hình hệ thống) tạo `superadmin.trungtamdhs.com` → ingress tới `127.0.0.1:8700`; tạo ứng dụng Cloudflare Access cho
   tên miền đó (danh sách email quản trị) rồi đặt `sac.access_team` + `sac.access_aud` → khởi động lại SAC (`launchctl kickstart -k system/com.cfharness.sac`).
6. Người dùng → Nhập user có sẵn (lamlinh, hubadmin, cchcadmin…) → Tên miền: xem mục "Lệch cần xử lý" (vd hostname tạo nhầm zone).
7. Nâng các hub lên bản có SPEC v3 + VIS (Cập nhật hub → theo đợt).

Kịch bản nghiệm thu: `docs/acceptance/superadmin.md`. Giả định A1–A5 (`plan-superadmin.md` §9) chưa kiểm trên Mac mini thật — runner/lệnh đổi được
theo kết quả spike mà không đổi cấu trúc.

## Test

`tests/test_admind_{core,server,users,lifecycle}.py`, `tests/test_sac_{web,pages}.py`, `tests/test_sa_teams_reports.py`, `tests/test_sa_policy_claude.py`,
`tests/test_sa5_extra.py`, `tests/test_vis_scan.py`. Không gọi sudo/launchctl/sysadminctl/cloudflared/Cloudflare/claude thật (`runner.FakeRunner`,
`tests/admind_fakes.py`). Socket thật chỉ chạy trên POSIX.

## Danh mục mã thao tác (117)

| Mã | Nhóm quyền | Mức | Mô tả |
|---|---|---|---|
| `access.login_unlock` | access | normal | Gỡ khoá do nhập sai (không khởi động lại web nếu hub đã có `web unlock-login`; bản cũ → khởi động lại web). |
| `access.sessions_revoke` | access | normal | Huỷ mọi phiên web + thiết bị tin cậy của user. |
| `access.totp_reset` | access | normal | Cấp lại TOTP: tắt → tạo link mới (hạn 24 giờ, hiện MỘT lần) → khởi động lại web (huỷ mọi phiên). Thiết bị tin cậy cũ tự vô hiệu. |
| `admin.create` | admin_accounts | high | Mời tài khoản quản trị mới (Super Admin mới: mức Tới hạn qua `admin.set_role`). Trả mã kích hoạt MỘT lần. |
| `admin.delete` | admin_accounts | critical | Xoá tài khoản quản trị (four-eyes). |
| `admin.list` | admin_accounts | read | Danh sách tài khoản quản trị. |
| `admin.lock` | admin_accounts | high | Khoá tài khoản quản trị (huỷ mọi phiên). |
| `admin.reset_totp` | admin_accounts | high | Đặt lại mật khẩu + TOTP: tài khoản về chờ kích hoạt, mã kích hoạt mới (MỘT lần). |
| `admin.set_role` | admin_accounts | critical | Đổi vai trò (thêm/hạ Super Admin — mức Tới hạn, four-eyes). |
| `admin.unlock` | admin_accounts | high | Mở khoá tài khoản quản trị (về hoạt động nếu đã kích hoạt, không thì chờ kích hoạt). |
| `admin.update` | admin_accounts | high | Sửa tên / phạm vi của tài khoản quản trị. |
| `alert.ack` | monitor | normal | Xác nhận đang xử lý (+ ghi chú). |
| `alert.list` | monitor | read | Cảnh báo (mở / cả đã xử lý). Kèm tên user cho cảnh báo theo user. |
| `alert.mute` | monitor | normal | Tắt tiếng N giờ (không gửi tin, vẫn hiện). |
| `alert.resolve` | monitor | normal | Đóng tay một cảnh báo (nếu điều kiện còn, lần thu thập sau sẽ mở lại). |
| `approval.cancel` | self | normal | Huỷ yêu cầu (người yêu cầu / người duyệt, trước khi chạy). |
| `approval.decide` | approval | high | Duyệt / từ chối yêu cầu của Super Admin khác. |
| `approval.list` | approval | read | Yêu cầu four-eyes. |
| `audit.search` | audit | read | Tìm nhật ký quản trị. Operator chỉ thấy dòng của chính mình (ma trận mục 8). |
| `audit.verify` | audit | read | Kiểm chuỗi băm nhật ký quản trị (AUD-02). |
| `auth.enroll_begin` | self (public) | read | Mở link kích hoạt: trả QR TOTP để quét. |
| `auth.enroll_finish` | self (public) | normal | Đặt mật khẩu + xác nhận TOTP → tài khoản hoạt động. |
| `auth.login` | self (public) | normal | Đăng nhập: mật khẩu + TOTP → ticket phiên. |
| `auth.logout` | self | normal | Đăng xuất phiên hiện tại. |
| `auth.me` | self | read | Tài khoản + menu được thấy + phiên còn mạnh (step-up) không. |
| `auth.session_revoke` | self | normal | Thu hồi một phiên (của mình; Super Admin thu hồi được phiên người khác). |
| `auth.sessions` | self | read | Phiên SAC đang hoạt động — của mình, hoặc mọi người (Super Admin). |
| `auth.step_up` | self | normal | Nhập lại TOTP cho thao tác mức Cao (5 phút). |
| `claude.account_add` | claude | high | Thêm token vào kho. |
| `claude.assign` | claude | high | Gán / đổi tài khoản Claude cho một hoặc nhiều hub (ghi token vào secret store của hub + khởi động lại daemon). |
| `claude.check` | claude | normal | Gọi thử Claude bằng token (một câu rất ngắn, chạy dưới uid một hub user; token qua stdin). |
| `claude.list` | claude | read | Tài khoản trong kho: tên, trạng thái, tuổi token, số user đang gán (token không bao giờ hiện lại). |
| `claude.remove` | claude | high | Gỡ tài khoản khỏi kho (không còn user nào đang gán). |
| `claude.rotate` | claude | high | Xoay token: thay token trong kho → ghi lại cho mọi user đang dùng tài khoản này. |
| `cost.report` | cost | read | Chi phí theo user / nhóm / team / tài khoản Claude / ngày (từ mẫu lịch sử: max chi phí-hôm-nay mỗi ngày mỗi user). |
| `domain.check_now` | domain | normal | Kiểm sức khoẻ ngay một tên miền. |
| `domain.create` | domain | high | Cấp tên miền con cho hub: DNS (đúng zone) → ingress → public_origin → kiểm HTTPS. Chạy thành job. |
| `domain.list` | domain | read | Bảng trung tâm: hostname · zone · user · cổng · DNS · ingress · Access · HTTP · TLS · lần kiểm. |
| `domain.reconcile` | domain | read | Lệch giữa máy thật và SAC (lần kiểm gần nhất): ingress mồ côi, hub không tên miền, cổng trùng. |
| `domain.rename` | domain | high | Đổi tên miền con: tạo tên mới → public_origin mới → tên cũ chuyển hướng N ngày → huỷ phiên web. |
| `domain.revoke` | domain | high | Thu hồi: gỡ ingress → gỡ DNS → gỡ Access → xoá public_origin. Hub vẫn chạy cục bộ. |
| `domain.sac_setup` | system | high | Tạo tên miền cho chính SAC (`superadmin.<zone>`) → ingress tới web SAC loopback. Access cấu hình trên dashboard Cloudflare. |
| `estop.list` | estop_view | read | Cờ ESTOP đang bật + hub đã áp chưa (EST-04). |
| `estop.off` | estop_off | high | Gỡ ESTOP quản trị. |
| `estop.on` | estop_on | normal | Bật ESTOP quản trị (toàn máy / user / nhóm / team). Hub dừng run đang chạy, user không tự gỡ được. |
| `estop.on_timed` | estop_on | normal | Bật ESTOP và hẹn tự gỡ sau N giờ (bảo trì có lịch). Việc gỡ do admind thực hiện, ghi audit. |
| `group.create` | user_lifecycle | normal | Tạo nhóm. |
| `group.delete` | user_lifecycle | normal | Xoá nhóm rỗng. |
| `group.list` | user_view | read | Nhóm quản trị (phòng ban). |
| `hub.config_set` | hub | high | Đặt khoá cấu hình thuộc danh sách quản trị được phép (HUB-10); khoá khác thuộc quyền user. |
| `hub.doctor` | hub | normal | Chẩn đoán từ xa (`cfharness doctor`, không tốn tiền) — kết quả đã che. |
| `hub.logs` | hub | read | Log hệ thống của hub (tối đa 2.000 dòng cuối, đã lọc redact). Không gồm transcript run. |
| `hub.pin` | hub | high | Ghim hub ở một tag (loại khỏi cập nhật hàng loạt); bỏ trống = bỏ ghim. |
| `hub.restart` | hub | normal | Khởi động lại daemon / web (một hoặc nhiều hub). Run đang chạy sẽ được daemon tự tiếp tục (auto-resume). |
| `hub.rollout` | hub | high | Cập nhật theo đợt: 1 hub thử → doctor xanh + chờ N phút không lỗi → phần còn lại theo lô; lỗi → dừng đợt. |
| `hub.start` | hub | normal | Chạy daemon/web. |
| `hub.stop` | hub | normal | Dừng daemon/web (không gỡ LaunchDaemon; khởi động máy lại sẽ chạy lại). |
| `hub.update` | hub | high | Cập nhật một hoặc nhiều hub lên tag (hub đã ghim phiên bản bị bỏ qua — HUB-04). |
| `job.cancel` | job | normal | Huỷ job; `cleanup` = chạy `undo` các bước đã làm theo thứ tự ngược ("Huỷ và dọn"). |
| `job.get` | job | read | Chi tiết job + từng bước + log đã che. |
| `job.list` | job | read | Danh sách job. |
| `job.retry` | job | normal | Thử lại từ bước lỗi (bước đã xong được bỏ qua). |
| `overview` | user_view | read | Số liệu trang tổng quan (MON-01) — trong phạm vi của tài khoản. |
| `policy.effective` | policy | read | Với một user: từng khoá, giá trị cuối và nguồn (máy / mặc định / nhóm / team / user) — POL-03. |
| `policy.get` | policy | read | Một phạm vi: bản hiện tại + lịch sử phiên bản. |
| `policy.list` | policy | read | Mọi phạm vi có chính sách + rev hiện tại + người sửa cuối. |
| `policy.rollback` | policy | high | Quay về bản trước một bước (lưu thành bản mới). |
| `policy.save` | policy | high | Lưu chính sách (rev+1, diff) → ghi lại file cho các user bị ảnh hưởng. |
| `policy.simulate` | policy | read | Trước khi lưu: user bị ảnh hưởng + hub sẽ bị chặn run (vd phiên bản < min_version, chưa bật sandbox mà require.sandbox). |
| `port.next` | domain | read | Cặp cổng web/MCP trống tiếp theo. |
| `report.day` | report | read | Báo cáo gộp một ngày (toàn hệ / team): user × chỉ số, so với hôm trước + trung bình 7 ngày (RPT-05). |
| `report.get` | report | read | Một báo cáo ngày (mức Hoạt động chỉ khi được xem — ghi audit vì có thể lộ tiêu đề việc). |
| `report.period` | report | read | Báo cáo gộp tuần (7 ngày) / tháng (30 ngày) kết thúc ở `end`, có chuỗi xu hướng theo ngày. |
| `report.search` | user_activity | read | Tìm theo từ khoá tiêu đề việc trong báo cáo (RPT-08) — cần mức Hoạt động. |
| `report.send_now` | system | normal | Gửi lại báo cáo một ngày ngay (kiểm kênh gửi). |
| `report.user` | report | read | Báo cáo ngày của một user trong N ngày. |
| `sys.bootstrap` | system (root) | normal | `cfharness superadmin init`: tạo Super Admin (đầu tiên hoặc khôi phục khi không còn ai) — trả mã kích hoạt một lần. |
| `sys.cf_token_rotate` | system | critical | Xoay token API Cloudflare (four-eyes). |
| `sys.config_get` | system | read | Cấu hình SAC (không có secret). |
| `sys.config_set` | system | high | Đặt một khoá cấu hình SAC (kiểm kiểu, giới hạn). |
| `sys.history` | monitor | read | Chuỗi theo ngày: chi phí (tổng max-trong-ngày mỗi user), run đang chạy lớn nhất, số lỗi. |
| `sys.hosts` | user_view | read | Danh sách máy chủ (Q10 — bản này chỉ máy cục bộ điều khiển được). |
| `sys.ping` | self (public) | read | Kiểm admind còn sống (không lộ gì ngoài phiên bản). |
| `sys.recover` | system (root) | normal | Break-glass tối thiểu (AUTH-09): root trên máy cấp lại mã kích hoạt cho một Super Admin (mất TOTP). |
| `sys.resources` | monitor | read | CPU (tải), RAM, đĩa (tổng + theo user, từ số liệu hub), số tiến trình claude, tunnel có connector. |
| `sys.sac_config` | self (public) | read | Cấu hình công khai cho web SAC lúc khởi động (tên miền, Access AUD/team — không phải secret). |
| `sys.secret_set` | system | high | Nạp/đổi secret hệ thống (không bao giờ hiện lại). Đổi token Cloudflare đang có = `sys.cf_token_rotate` (Tới hạn). |
| `team.archive` | team_admin | normal | Giải tán team — giữ lịch sử việc và báo cáo. |
| `team.get` | team_board | read | Team + thành viên (đang làm gì) + bảng việc + quá hạn + chi phí team (TEAM-12). |
| `team.list` | team_board | read | Danh sách team (Team Lead: team của mình). |
| `team.member_remove` | team_admin | normal | Bớt thành viên — việc chưa xong về 'Chưa giao'; thu hồi hộp thư. |
| `team.member_set` | team_admin | normal | Thêm / đổi vai trò thành viên (TEAM-02, 03). Tạo sẵn thư mục hộp thư + trạng thái của thành viên. |
| `team.task_assign` | team_board | normal | Giao (hoặc giao lại) việc cho thành viên → hộp thư hub của họ. |
| `team.task_create` | team_board | normal | Tạo việc (TEAM-05). Có người nhận và việc phụ thuộc đã xong → giao ngay vào hộp thư hub (TEAM-06, 08). |
| `team.task_delete` | team_board | normal | Huỷ việc (gỡ khỏi hộp thư). |
| `team.task_review` | team_board | normal | Duyệt → Xong (mở việc phụ thuộc) / Trả lại kèm nhận xét → về hộp thư thành viên (TEAM-09). |
| `team.upsert` | team_admin | normal | Tạo / sửa team (TEAM-01). |
| `user.backup` | user_lifecycle | normal | Sao lưu home của user ngay (mã hoá, kho quản trị). |
| `user.create` | user_lifecycle | high | Tạo user + hub + tên miền (job 11 bước, chạy lại được). Token Claude chỉ đi vào job (RAM), không lưu DB. |
| `user.create_defaults` | user_lifecycle | read | Giá trị mặc định cho wizard tạo user. |
| `user.delete` | user_delete | critical | Xoá hẳn (chỉ sau thu hồi): sao lưu mã hoá → xác minh → `sysadminctl -deleteUser`. Four-eyes + chờ 24 giờ sau duyệt. |
| `user.export` | user_view | read | Xuất hồ sơ (mức Số liệu/Hồ sơ) của các user đã chọn thành CSV. |
| `user.import` | user_lifecycle | high | Đưa một user có sẵn vào SAC — KHÔNG chạm hub (chỉ ghi sac.db). |
| `user.import_csv` | user_lifecycle | high | Tạo nhiều user từ CSV (cột: os_user, full_name, email, group, team, sub). `dry_run` = chỉ kiểm, báo dòng lỗi. |
| `user.import_scan` | user_lifecycle | read | Quét máy: thành viên `cfharness-users` + plist LaunchDaemon + thư mục trạng thái + hostname ingress → ứng viên chưa có trong SAC. |
| `user.list` | user_view | read | Danh sách user (USR-01) — Operator/Team Lead chỉ thấy user trong phạm vi. |
| `user.profile` | user_view | read | Hồ sơ 360° (FR-SA-PRO). Trường mức Hoạt động chỉ trả khi vai trò được xem VÀ hệ đã bật mức này (SYS-03). |
| `user.restore` | user_lifecycle | high | Khôi phục user đã thu hồi: nhóm, LaunchDaemon, (tên miền mới nếu chọn), TOTP mới. |
| `user.resume` | user_suspend | high | Mở khoá (đảo tạm khoá). |
| `user.revoke` | user_lifecycle | high | Thu hồi: ESTOP revoked → bootout → gỡ nhóm → thu hồi tên miền → vô hiệu TOTP/thiết bị. Không xoá HUB_HOME. |
| `user.suspend` | user_suspend | high | Tạm khoá: ESTOP + dừng run + huỷ phiên web + dừng web. Dữ liệu giữ nguyên. |
| `user.totp_link` | user_lifecycle | normal | Lấy link đăng ký TOTP do job tạo (MỘT lần — sau đó mất). |
| `user.update` | user_lifecycle | normal | Sửa họ tên, email, nhóm, ghi chú quản trị (USR-06 — ghi chú chỉ SAC thấy, không tới agent). |
| `zone.add` | domain | high | Khai zone Cloudflare dùng được — kiểm token API thấy zone. |
| `zone.list` | domain | read | Zone đã khai + số tên miền. |
| `zone.remove` | domain | critical | Bỏ zone khỏi SAC (không xoá trên Cloudflare). Zone còn tên miền → không bỏ được. |
| `zone.verify` | domain | normal | Kiểm lại token còn thấy zone. |