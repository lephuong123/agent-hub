# V2-N1 — `agent.revert|archive`, `trash.restore|purge` chỉ người, ở mọi đường vào

## Vì sao
Kiểm toán v2 (traceability-v2 §3.1) phát hiện 4 RPC quản trị của luồng CLI không xác thực người gọi (FR-AG-03, FR-SG-11): agent
`agent.revert` về revision cũ = bật lại `allow_bypass`; `trash.purge` = xoá vĩnh viễn; `agent.archive` = DoS. Đợt hardening-SEC đã đưa
4 method vào `auth.HUMAN_ONLY` và đặt `require_human` trong `recovery.purge_agent`. Còn hở: (1) handler revert/archive/restore không có
cổng tầng service — lời gọi nội bộ lọt cổng RPC vẫn chạy; (2) cổng tầng service từ chối mà KHÔNG audit; (3) bộ phân loại để lọt tool MCP
của hub `hub_agent_revert` (`write` ALLOW), `hub_trash_restore|purge` (chỉ destructive, người duyệt được thay vì cấm).

## Hành vi
- Lớp 1 (không đổi): `Daemon._dispatch` — method ∈ `HUMAN_ONLY` mà người gọi không phải người → `-32003` + `auth.denied`. Áp cho socket,
  `call_as` (Telegram/Slack ngoài allowlist) và điều phối viên (chỉ `agent.archive` ∈ MANAGER_ALLOWED).
- Lớp 2 (mới): `auth.require_human(what, allow_manager=False)` trong 4 handler `mcp_server/surface_rpc.py`. `agent.archive` (= `agent_delete`,
  cũng phục vụ `agent.delete`) cho điều phối viên qua `allow_manager=True`; 3 cái còn lại chỉ người. Ngoài ngữ cảnh RPC (vòng nền,
  `CURRENT_CALLER` rỗng) vẫn cho qua như cũ — retention purge chạy được.
- `require_human` từ chối → audit `auth.denied` với `layer=service`, `what`, thông tin người gọi, `actor=agent|manager:<id>`,
  `channel=<via>`. Áp cho MỌI chỗ gọi `require_human` (guard, systems, manager.set, purge), không chỉ 4 method này. Audit lỗi không mở cổng.
- Bộ phân loại: `HUB_MCP_HUMAN_ONLY` thêm `agent_(revert|pause|resume)`, `trash_(restore|purge|empty|rm)`, `task_archive` → forbidden
  cho MCP server của hub dưới mọi tên đăng ký. CLI đã forbidden từ trước (`HUB_HUMAN_ONLY_CLI`).
- Web: không đổi — client web gửi token người qua socket nên đi đúng 2 lớp trên.

## Test
`tests/test_sec_v2_n1.py`: tầng service × 4 người gọi không phải người × 4 method (gọi thẳng handler, bỏ qua `_dispatch`), người vẫn làm
được, cổng RPC × mọi kênh, `_audit`/`_auth` giả, lách CLI (`python -m`, `uv run`, `sh -c`, env), tool MCP của hub (kể cả tên đăng ký khác),
hub-self không lộ tool. Trên code cũ: 23 ca đỏ.

## Việc dở
- Cổng tầng service chưa phủ `agent.pause|resume`, `task.archive` (đã ∈ HUMAN_ONLY ở lớp 1).
- BRD §15 vẫn ghi V2-N1 "chặn phát hành" — chờ BA cập nhật sau khi QA kiểm lại.
