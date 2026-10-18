# M7 — Điều phối viên (agent harness): 2 luồng song song

Yêu cầu chủ sản phẩm (2026-10-19): "muốn có 1 con agent như harness để quản lý và tạo nhiệm vụ cho các con agent còn lại".
Quyết định quyền: **"Toàn quyền trừ an toàn"** — điều phối viên tự tạo/sửa/xoá mềm agent và team, đổi tool/MCP/model tự do;
KHÔNG được: duyệt thao tác phá huỷ, sửa policy/protected/guard, golden task & rubric, ngoại lệ sandbox, xoá vĩnh viễn (purge),
tắt ESTOP, deploy/đụng prod, sửa chính cấu hình của mình hoặc của bất kỳ agent manager nào, đặt `permission_mode=bypassPermissions`/
`allow_bypass`/`extra_args`, cấp cho agent khác vai trò manager.

## Mô hình
- **Vai trò manager**: một agent được NGƯỜI chỉ định (`agent-hub manager set <agent>`, HUMAN_ONLY, audit). Hub-self MCP của run thuộc agent manager có bộ tool `manager` (dưới). Quyền được kiểm ở DAEMON (không tin tool list): RPC từ run của agent manager → tập `MANAGER_ALLOWED`; mọi trường cấm bị từ chối kèm lý do; mọi thao tác ghi audit actor `manager:<agent>` và ledger (rollback được). Thay đổi cấu hình agent do manager tạo là thay đổi tự động (KHÔNG phải của người) ⇒ regression monitor được phép tự rollback nếu hồi quy.
- **Hội thoại liên tục**: mỗi kênh (telegram:<chat_id>, web, cli) có một conversation với manager = một session claude dài, các tin tiếp theo `--resume` cùng session (autocompact của CLI lo ngữ cảnh dài; ghi rõ ngưỡng). Một tin người → một run của manager.
- **Nhịp (heartbeat)**: tick theo SỰ KIỆN (task failed/blocked, run crashed lặp, approval chờ > N phút, digest, run của team xong) + tick định kỳ dự phòng (setting `manager.tick_interval_s`, mặc định 1800) — chỉ chạy khi có gì đáng xử lý (gom các sự kiện vào prompt tick); không chạy khi ESTOP; có khoá chống chồng tick.
- **Báo cáo**: kết quả tick/hội thoại gửi về kênh của người (Telegram/web) khi có việc cần người hoặc khi hoàn thành mục tiêu.

## Bộ tool `manager` (hub-self MCP)
Đọc: `hub_overview` (agent/team/task/run/approvals/cost tóm tắt), `agent_get`, `team_get`, `task_list|get`, `run_list|get|events` (tóm tắt, cắt), `learn_feed`, `digest_get`, `cost_report`, `templates`, `mcp_registry_list` (không secret).
Ghi (trong quyền): `agent_create` (từ template hoặc spec; chỉ MCP có trong registry; không sandbox override), `agent_update` (trường cho phép: name/role/description/SOUL/model/effort/fallback/tools/allowed/disallowed (không được BỎ mẫu chặn có sẵn do người đặt — chỉ thêm), mcp, limits, workspace), `agent_archive`/`agent_restore`, `team_create|edit|member_add|member_rm|archive|restore`, `task_create` (bất kỳ team/agent, DAG), `task_cancel|retry|comment`, `run_start` (ad-hoc), `run_steer`, `run_stop`, `learn_propose` (đi pipeline có eval/canary), `learn_refine`, `estop_on` (chỉ BẬT), `notify_owner` (gửi tin tới người qua kênh hội thoại).

## Quyền sở hữu
| Luồng | Sở hữu |
|---|---|
| **MGR-CORE** | `src/agent_hub/manager/` (chỉ định, quyền, conversation, heartbeat/tick, prompt tick), `mcp_server/hub_self.py` (role `manager` + tool), `auth.py` (MANAGER_ALLOWED + kiểm trường cấm — KHÔNG nới HUMAN_ONLY cho người khác), `runs.py`/`daemon.py`/`cli.py` phần thêm, template persona `templates/manager*` (tiếng Anh), tests `tests/test_manager*.py` |
| **MGR-UI** | Web: trang "Điều phối" (chat với manager theo conversation web: gửi tin, xem trả lời stream, feed hành động của manager, nút chỉ định/huỷ manager — HUMAN_ONLY), Telegram: tin KHÔNG phải lệnh `/` từ chat trong allowlist → `manager.chat` (khi chưa có manager thì trả hướng dẫn), reply giữ conversation; tests `tests/test_web_manager*.py`, `tests/test_telegram_manager*.py` |

## Hợp đồng RPC (MGR-CORE cung cấp; MGR-UI dùng — trước khi có thì dùng stub cùng chữ ký)
- `manager.get()` → `{agent_id|null, conversations:[…], last_tick, next_tick}`; `manager.set({agent_id|null})` HUMAN_ONLY.
- `manager.chat({channel, conversation_id?, text})` → `{conversation_id, run_id}`; trả lời đến qua event `manager.reply{conversation_id, run_id, text}` và kết thúc run như mọi run (`events.subscribe`).
- `manager.conversations({channel?})`, `manager.history({conversation_id, limit})`.
- Event `manager.action{tool, target, summary, audit_id}` cho mọi thao tác ghi của manager.

## Ràng buộc
- Test không gọi `claude` thật/Telegram thật. Nghiệm thu thật cuối (MGR-CORE): haiku cho manager trong HUB_HOME tạm, tổng ≤ $2: (1) người nói "tạo agent researcher để tóm tắt README của repo X rồi giao việc đó" → manager tạo agent + task, theo dõi tới xong, trả tóm tắt; (2) manager cố duyệt approval / sửa policy / bật sandbox / sửa chính nó / đặt bypassPermissions → đều bị daemon từ chối + audit.
- `uv run pytest -q` xanh. Ghi `docs/dev-notes/M7-core.md` / `M7-ui.md`, mục `AGENTS.md`. KHÔNG git commit. Migration: CORE = số kế tiếp chưa dùng, UI = số sau đó (đọc `store.py` trước).
