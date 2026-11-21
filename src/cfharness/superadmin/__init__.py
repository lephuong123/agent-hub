"""Super Admin Console (SAC) — web riêng ở `superadmin.<miền>`, chạy bằng user `_cfh_sac`, không agent (plan-superadmin §2).

Lớp giao diện mỏng: mọi đọc/ghi là một mã thao tác gửi tới admind (`admind.client`). admind tự kiểm phiên/quyền/step-up/four-eyes.
"""
