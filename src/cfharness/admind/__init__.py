"""admind — tiến trình đặc quyền (root) của Super Admin Console (docs/dev-notes/plan-superadmin.md §2).

- Nghe DUY NHẤT socket Unix cục bộ (`paths.AdmindPaths.socket`), chỉ nhận kết nối từ uid `_cfh_sac` (web SAC) và root (CLI sudo).
- Nhận **mã thao tác có kiểu** (`ops.REGISTRY`), không bao giờ nhận chuỗi lệnh shell; tự kiểm phiên, vai trò, phạm vi, step-up,
  four-eyes (BR-SA-01) — không tin SAC mù quáng.
- Là nơi DUY NHẤT ghi `sac.db` và giữ secret (Cloudflare, bot, token Claude, TOTP quản trị) trong `vault`.
"""
