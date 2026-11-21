# Nghiệm thu Super Admin Console trên máy thật (Mac mini)

Chạy sau khi triển khai theo `docs/dev-notes/superadmin.md` §"Triển khai". Ghi kết quả (đạt / lỗi + ghi chú) vào cột cuối.

## 0. Spike giả định (plan-superadmin §9)

| # | Kiểm | Cách | Kết quả |
|---|---|---|---|
| A1 | Token API thấy mọi zone dùng cho hub | Tên miền → Khai zone từng zone | |
| A2 | Access dùng được cho `superadmin.trungtamdhs.com` | Tạo ứng dụng Access, mở từ điện thoại: chưa đăng nhập Access → trang Cloudflare; email lạ → bị chặn | |
| A3 | `sysadminctl`/`dseditgroup`/`launchctl` chạy được từ admind (không phiên GUI) | Tạo user thử `satest1` khi đăng xuất khỏi màn hình | |
| A4 | Tunnel chạy dạng system, sống qua đăng xuất admin | Đăng xuất phiên GUI của admin, mở các tên miền hub | |
| A5 | Token Claude nạp qua stdin, không lộ | Trong lúc job `user.create` chạy: `ps auxww | grep sk-ant` trả rỗng; `grep -r sk-ant /var/db/cfharness-superadmin/sac.db /var/log/cfharness-admind.log` rỗng | |

## 1. Truy cập & bảo mật SAC

| # | Kịch bản | Mong đợi | Kết quả |
|---|---|---|---|
| 1.1 | `curl -H 'Host: superadmin.trungtamdhs.com' http://127.0.0.1:8700/login` (không JWT) | 404 "Not Found" | |
| 1.2 | `curl -H 'Host: evil.com' http://127.0.0.1:8700/login` | 421 | |
| 1.3 | Đăng nhập sai TOTP 5 lần | Khoá 15 phút, nhật ký có dòng `denied` | |
| 1.4 | Để phiên 16 phút không thao tác | Về trang đăng nhập | |
| 1.5 | Operator mở `/admins`, `/settings` | 404; menu không có mục đó | |
| 1.6 | Xoá tài khoản quản trị khi có 2 Super Admin | Tạo yêu cầu; người thứ hai duyệt mới chạy | |
| 1.7 | Nhật ký → kiểm chuỗi băm | "chuỗi băm toàn vẹn" | |

## 2. Người dùng & hub (G2, G3)

| # | Kịch bản | Mong đợi | Kết quả |
|---|---|---|---|
| 2.1 | Nhập user có sẵn (mọi hub đang chạy) | Danh sách đủ, tên miền đúng, không hub nào bị khởi động lại | |
| 2.2 | Tạo user `satest1` + tên miền `satest1.trungtamdhs.com` (tính giờ) | Job 11 bước xanh ≤ 10 phút; `https://satest1…` ra trang đăng nhập; link TOTP hiện một lần | |
| 2.3 | Từ điện thoại: cấp lại TOTP cho `satest1` (tính giờ) | ≤ 2 phút, link mới dùng được, phiên cũ bị huỷ | |
| 2.4 | Tạm khoá `satest1` | Run dừng, web không vào được, user thấy "Hub đang tạm dừng" | |
| 2.5 | Mở khoá | Hub chạy lại | |
| 2.6 | Thu hồi → khôi phục | Gỡ nhóm + tên miền + TOTP; khôi phục cấp TOTP mới | |
| 2.7 | Cập nhật theo đợt lên tag mới (hub thử = `satest1`) | Hub thử cập nhật + chờ; các lô sau chạy; ghim phiên bản được bỏ qua | |
| 2.8 | Xoá hẳn `satest1` | Sao lưu `.gz.enc` có trong `backups/`, four-eyes + chờ 24 giờ, user OS bị xoá | |

## 3. Tên miền

| # | Kịch bản | Mong đợi | Kết quả |
|---|---|---|---|
| 3.1 | Sửa tay `tunnel.yml` cho sai cú pháp rồi cấp tên miền | Job lỗi ở bước ingress, file cũ giữ nguyên, các hub khác vẫn vào được | |
| 3.2 | Bảng tên miền sau 10 phút | DNS/ingress/HTTP/TLS đúng; hostname tạo nhầm zone trước đây hiện ở "Lệch cần xử lý" | |
| 3.3 | Đổi tên miền con | Tên mới vào được, tên cũ ghi chuyển hướng | |

## 4. Ẩn quản trị với user thường (G6)

| # | Kịch bản | Mong đợi | Kết quả |
|---|---|---|---|
| 4.1 | Đăng nhập web hub của user thường | Không có "Tổng quan máy", "Người dùng máy", "Quản trị máy"; `/admin/users`, `/machine` → 404 | |
| 4.2 | `sudo -u lamlinh -i cfharness --help` / `cfharness admin overview` | Không có lệnh `admin`, `admind`, `superadmin` | |
| 4.3 | Bật ESTOP cho `lamlinh` | Web/Telegram/Slack của user chỉ "Hub đang tạm dừng — liên hệ quản trị", không mã lý do | |

## 5. Báo cáo & Team

| # | Kịch bản | Mong đợi | Kết quả |
|---|---|---|---|
| 5.1 | Sáng hôm sau 08:00 | Báo cáo toàn hệ tới Telegram/Slack của quản trị; trang Báo cáo có số liệu mọi user | |
| 5.2 | Tạo team, giao việc cho `lamlinh` | Hub `lamlinh` hiện "Việc được giao" trong ≤ 30 giây; Nhận → task cho agent; bảng team "Đang làm" ≤ 1 phút | |
| 5.3 | Thành viên Nộp → trưởng team Trả lại → Nộp lại → Duyệt | Cột chuyển đúng, việc phụ thuộc được giao tự động | |
| 5.4 | Team Lead đăng nhập SAC | Chỉ thấy team mình + báo cáo thành viên; không thấy Tên miền/Cấu hình | |

## 6. Chính sách & Claude

| # | Kịch bản | Mong đợi | Kết quả |
|---|---|---|---|
| 6.1 | Chính sách mặc định ngân sách 20 USD/ngày, nhóm A 5 USD | `/Users/Shared/cfharness/policy/<u>.json` root 0644 đúng giá trị chặt hơn; hub báo `rev_applied` khớp (không cảnh báo `policy_mismatch`) | |
| 6.2 | Mô phỏng `min_version` cao hơn hub cũ | Liệt kê đúng hub sẽ bị chặn | |
| 6.3 | Thêm token Claude vào kho, gán cho 2 hub | Run của 2 hub chạy bằng tài khoản mới; token không hiện ở đâu | |
