"""Giao diện web của cfharness (`cfharness web`) — CLIENT của daemon qua RPC, như CLI/TUI.

- `security.py`: chỉ loopback, đăng nhập bằng link một lần → cookie phiên HttpOnly/SameSite=Strict, CSRF, kiểm Host/Origin.
- `present.py`: định dạng thuần (nhãn trạng thái tiếng Việt, event → mục transcript) — test được không cần server.
- `app.py`: Starlette + Jinja2 + htmx (vendored) + SSE (`events.subscribe` của daemon).
- `cli.py`: lệnh `cfharness web [--port] [--open]`.

Token người (`<socket>.auth`) chỉ nằm trong tiến trình web (gửi kèm RPC như CLI) — không bao giờ vào HTML/cookie/log.
"""
