# Plan — "Tin cậy thiết bị này" khi đăng nhập web bằng TOTP

Trạng thái: KẾ HOẠCH (2026-11-10), chưa triển khai. Liên quan: `web-public.md`, `web.md`, `hardening-SEC.md`.

## Tóm tắt quyết định đề xuất
- Cookie thiết bị hợp lệ thay **đúng bước nhập mã TOTP**, tạo phiên mới bình thường (cookie phiên + `k` mới qua `#k=`).
  Link một lần giữ nguyên, không cấp cookie thiết bị.
- **Chỉ bật trên host public https.** Trên `http://cfharness.localhost` cookie không tách theo cổng: dev server do agent
  viết ở `cfharness.localhost:<cổng khác>` sẽ nhận cookie thiết bị → đổi được thành phiên KÈM `k` mới → phá lớp 2 của WT-1.
- Không RPC mới: store ở `HUB_HOME/auth/` như `TotpStore`, web/CLI đọc trực tiếp.

## 1. Luồng hiện tại & chỗ chèn
- Link một lần: `security.py:100-120`, `WebAuth.redeem` `:156-168`. TOTP: `login()` `app.py:431-438` → `_code_attempt` `:395-412`
  → `TotpStore.verify` `totp.py:186-196` → `auth.new_session()` → `_login_ok` `app.py:360-366`.
- Phiên chỉ ở RAM (idle 1 h, tối đa 12 h, `security.py:44-45,176-188`).
- Chèn: (1) checkbox ở form TOTP `login.html` (chỉ khi public); (2) nhánh mã đúng của `login()` + bước `confirm` của `/enroll`;
  (3) `login()` khi chưa có phiên: cookie thiết bị hợp lệ → xoay token → phiên → `_login_ok`; (4) `WebAuth.get` huỷ phiên gắn
  `device_id` đã thu hồi; (5) `logout` thêm "và quên thiết bị này".

## 2. Token thiết bị
| Mục | Đề xuất |
|---|---|
| Định dạng | `<did>.<secret>`, `did=token_urlsafe(9)`, `secret=token_urlsafe(32)`; server chỉ lưu `sha256(secret)`, so `hmac.compare_digest` |
| Lưu | `HUB_HOME/auth/trusted_devices.json` 0600 (mẫu `TotpStore._write/_read`: `O_EXCL|O_NOFOLLOW`, `owned_by_me`); flock `auth/.devices.lock` (web + CLI cùng ghi) |
| Bản ghi | `did, sha256, prev_sha256, prev_until, created_at, expires_at, last_used_at, last_ip, first_ip, ua_label, totp_fp` |
| Cookie | `__Host-<cookie_name(home)>_dev`; HttpOnly, Secure, SameSite=Strict, Path=/, Max-Age = hạn |
| Ràng buộc | KHÔNG theo IP (tunnel/di động đổi), KHÔNG chặn theo UA — chỉ lưu `ua_label` + audit khi đổi họ trình duyệt/OS |
| Gắn TOTP | `totp_fp = sha256(secret TOTP)[:16]` phải khớp → disable/enroll lại tự vô hiệu mọi thiết bị |
| Hạn | mặc định 30 ngày, trần 90; tuyệt đối (không trượt) |
| Xoay | mỗi lần dùng; hash cũ còn hợp lệ 60 s (hai tab); token cũ sau đó → thu hồi + `web.device_reuse_detected` |
| Số lượng | mặc định 5 (trần cứng 20); vượt → loại thiết bị lâu không dùng nhất |

## 3. Rủi ro & LoginLimiter
- Đăng nhập bằng thiết bị KHÔNG đi qua `_code_attempt` → người thật thoát được DoS dò mã (`web-public.md:102`).
  Token sai không cộng vào limiter (256 bit không dò nổi); xoá cookie + audit `web.device_rejected` (giới hạn ghi theo IP).
- Cookie bị đánh cắp: giảm bằng xoay + phát hiện dùng lại, hạn tuyệt đối, thu hồi, audit IP/UA.
- Máy dùng chung: checkbox mặc định không tick + cảnh báo.
- Tailscale Funnel nhiều hub cùng host khác cổng: cookie cũng không tách cổng → tắt mặc định khi `public_origin` có cổng ≠ 443.
- Sandbox tắt: agent có thể ghi `auth/trusted_devices.json` (cùng loại rủi ro `auth/web.json`) → thêm `trusted_devices` vào
  `SENSITIVE_HINT` (`classifier.py:152`).

## 4. Thu hồi
- CLI `cfharness web devices list [--json] | revoke <did> | revoke-all` (nhánh trong `web/cli.py` cạnh `totp`), audit kênh `cli`.
- Cài đặt web: khối "Thiết bị tin cậy" (đánh dấu "thiết bị này"), `ACTIONS` `device_revoke`/`device_revoke_all` dạng `call=` cục bộ,
  `confirm` + `danger`.
- Tự thu hồi: `totp disable` xoá file; `TotpStore.confirm` / `enroll(activate=True)` xoá hết; `totp_fp` lệch là lớp dự phòng.
- Phiên đang mở gắn thiết bị chết ngay khi thiết bị bị thu hồi (`WebAuth.get` → callback `device_ok`, cache theo mtime).
- Audit: `web.device_trusted|device_login|device_rejected{reason}|device_reuse_detected|device_evicted|device_revoked|devices_revoked_all|device_ua_changed`.

## 5. Config / quyền
- `web.trusted_devices.{enabled, ttl_days(1..90, 30), max_devices(1..20, 5), rotate_grace_s(0..300, 60)}`; validator ở `public.py`;
  nhánh `web` đã ∈ `integrity.SECURITY_KEYS`; `settings_schema.py` chỉ READONLY.
- Không RPC mới → `HUMAN_ONLY` không đổi. `HUB_HUMAN_ONLY_CLI["web"]=None` đã chặn mọi `cfharness web …` (chỉ thêm test).
- Sandbox đã denyRead/denyWrite `HUB_HOME/auth` (`guard/runtime.py:392`, `:173-177`).

## 6. Triển khai
Tạo `web/devices.py` (`DeviceStore`: trust, redeem_and_rotate, valid, list, revoke, revoke_all, purge_expired; `ua_label()`),
`tests/test_web_devices.py`. Sửa `web/app.py`, `web/security.py`, `web/totp.py`, `web/public.py`, `config.py`, `web/cli.py`,
`templates/login.html|settings.html`, `static/app.js`, `web/settings_schema.py`, `guard/classifier.py`, docs.

Thứ tự: devices.py + unit test → config/validator → hook totp.py → `device_ok` trong security.py → luồng app.py → CLI → Cài đặt
→ classifier → docs.

Test (đỏ→xanh, fixture `env` của `tests/test_web_public.py`): cookie đúng thuộc tính + file chỉ có hash 0600; host cục bộ bỏ qua;
cookie giả/did lạ/secret sai (limiter không tăng); hết hạn; thu hồi (phiên đang mở chết ngay); disable/enroll lại; xoay + grace +
reuse; Host lạ 421, Origin lạ 403, thiếu `k` 403; CSRF cho `device_revoke`; brute force 1000 token; limiter đang khoá vẫn vào
bằng thiết bị; vượt `max_devices`; symlink/file người khác; classifier; validator + integrity.

## 7. Câu hỏi mở
1. Hạn 30/90 ngày, tuyệt đối hay trượt? (đề xuất tuyệt đối)
2. SameSite=Strict làm link từ Slack/email không tự đăng nhập: để `app.js` tự POST "tiếp tục bằng thiết bị", hay Lax + `Path=/login`?
3. Funnel nhiều hub cùng host: tắt mặc định / cảnh báo / cấm?
4. Cho tin cậy ngay ở bước `/enroll`?
5. Logout mặc định giữ hay quên thiết bị?
6. Báo Telegram/Slack khi có thiết bị mới / phát hiện dùng lại token?
7. Step-up TOTP cho thao tác cực nhạy dù phiên từ thiết bị? (ngoài phạm vi lần này)

## 8. Quyết định của chủ repo (2026-11-10)
- Hạn **tuyệt đối 30 ngày** (trần cấu hình 90), không trượt.
- **Đăng xuất giữ thiết bị** (nút riêng "Đăng xuất và quên thiết bị này").
- **Báo Telegram**: thiết bị mới được tin cậy, phát hiện token bị dùng lại, thiết bị bị thu hồi.
- **Báo Telegram mọi lượt vào/ra web** (đăng nhập bằng link / TOTP / thiết bị tin cậy, đăng xuất, bị khoá do sai mã) lên
  **group chat private của hub** (chat nhóm đã nằm trong allowlist Telegram). Tin chỉ metadata: cách đăng nhập, nhãn thiết bị
  (`ua_label`), IP, giờ — không token, không cookie.
- Các câu còn lại theo đề xuất: tin cậy được cả ở bước `/enroll`; Funnel nhiều hub cùng host khác cổng → tắt mặc định;
  SameSite=Strict + `app.js` tự POST "tiếp tục bằng thiết bị" một lần trên `/login`; step-up ngoài phạm vi.
