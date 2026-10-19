"""Bảo vệ thao tác phá huỷ (FR-SG-01..09, FR-RT-09) — luồng B của M1.

Module con:
- `shell`      : lexer/chuẩn hoá lệnh shell (bỏ quote, giải `$'..'`, `${IFS}`, tách `&&`/`;`/`|`, heredoc…).
- `classifier` : bộ phân loại tất định safe|write|destructive|uncertain (FR-SG-01/03), thuần, test được.
- `policy`     : chính sách an toàn trong `HUB_HOME/policy/` + tài nguyên được bảo vệ (FR-SG-02/05).
- `service`    : `GuardService` trong daemon — nhận `guard.check` từ hook/permission tool, mở approval, giữ chờ.
- `hook`       : entrypoint hook PreToolUse (`python -I -m cfharness.guard.hook`), fail-CLOSED.
- `mcp_guard`  : MCP stdio `hub-guard` cho `--permission-prompt-tool mcp__hub-guard__approve`.
- `runtime`    : sinh settings.json/mcp.json/argv bổ sung cho mỗi run.
- `locks`      : khoá ghi theo thư mục/repo (bản tối thiểu M1, đầy đủ ở M3 — FR-RT-12).

GIỮ FILE NÀY NHẸ: hook và hub-guard import gói này ở mỗi lần gọi tool (không import pydantic/hub ở đây).
"""
