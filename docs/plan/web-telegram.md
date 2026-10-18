# Web UI + Telegram — 2 luồng song song

Yêu cầu chủ sản phẩm (2026-10-18): "qua terminal khó dùng quá — hỗ trợ qua website; giao tiếp ngoài Slack thêm Telegram".
Nguồn: BRD 0.7 (FR-UI-*, FR-SG-04/09/11, Q-11), `AGENTS.md`, `docs/dev-notes/hardening-SEC.md` (token người, HUMAN_ONLY, kiểm cây tiến trình, sandbox).

## Nguyên tắc chung (không đổi)
- Một người dùng / một máy (Q-11): web chỉ bind 127.0.0.1, đăng nhập bằng token người (đọc tự động khi mở bằng `agent-hub web` — in/mở URL kèm token một lần, đổi sang cookie HttpOnly, SameSite=Strict); chống CSRF cho mọi thao tác ghi; không CORS.
- Web và Telegram đều là CLIENT của daemon qua RPC (daemon là writer duy nhất). Thao tác nhạy cảm (approve/deny, sửa agent, policy, rollback…) đi qua cổng HUMAN_ONLY như CLI.
- Agent trong run KHÔNG được đọc token bot Telegram / cookie web / token người (secret store + sandbox đã chặn — kiểm lại bằng test).
- Giao diện tiếng Việt.

## Quyền sở hữu
| Luồng | Sở hữu | Nội dung |
|---|---|---|
| **WEB** | `src/agent_hub/web/` (server Starlette/FastAPI có sẵn trong deps, template Jinja2 + htmx + SSE, CSS/JS tĩnh vendored — KHÔNG cần build node), `tests/test_web*.py`, `docs/screenshots/web/` | Lệnh `agent-hub web [--port] [--open]`. Trang: Tổng quan (bảng agent live như `top`, chi phí hôm nay, yêu cầu duyệt), Agent (tạo/sửa/xoá mềm/khôi phục/revert, gán MCP từ registry, template, ngân sách), Xem run live (stream SSE từ `events.subscribe`, tool call, steer, stop, takeover hướng dẫn), Duyệt (approve/deny, waiting_human), Tasks/Teams (tạo task cho agent/team, DAG đơn giản), Learnings (feed, ledger diff, rollback, pin, tắt học, golden task), MCP (registry, probe, import), Chi phí & audit, Cài đặt (config an toàn, ESTOP, Slack/Telegram). Responsive dùng được trên màn nhỏ. |
| **TG** | `src/agent_hub/notify/telegram.py` + `src/agent_hub/chat/` (bot), `tests/test_telegram*.py` | Bot Telegram qua **long polling** (không cần endpoint công khai). Thông báo như Slack (FR-SG-09: approval.open + nhắc lại, waiting_human, lỗi lặp, guard.blocked, rollback, digest 08:00). **Duyệt bằng nút inline** (approve/deny) — chỉ chấp nhận từ chat_id trong allowlist do người cấu hình, có xác nhận 2 bước cho thao tác phá huỷ nặng. Lệnh chat: `/status`, `/agents`, `/runs`, `/approvals`, `/run <agent> <yêu cầu>` (giao việc, trả kết quả khi xong), trả lời (reply) vào tin kết quả để `continue` hội thoại, `/stop <run>`, `/estop on|off`, `/digest`. Cấu hình: `agent-hub notify telegram setup` (nhập bot token → secret store, tự lấy chat_id từ tin nhắn đầu, ghi allowlist). Chạy trong daemon (task asyncio), tắt êm khi chưa cấu hình. |

## File dùng chung — chỉ THÊM, đọc lại trước khi sửa
`daemon.py`, `cli.py`, `config.py`, `events.py`, `store.py`, `hub.py`, `auth.py` (chỉ thêm kênh `web`/`telegram` vào cơ chế xác thực người — KHÔNG nới lỏng), `notify/__init__.py`, `AGENTS.md`, `pyproject.toml`. Migration: WEB = v14 (nếu cần), TG = v15. Kênh audit: `web`, `telegram:<chat_id>`.

## Ràng buộc
- Test không gọi `claude` thật, không gọi Telegram thật (server giả cục bộ cho Bot API). Web test bằng Starlette TestClient + kiểm bảo mật (không token → 401, CSRF, bind host khác 127.0.0.1 → từ chối, thao tác HUMAN_ONLY qua web có audit `web`).
- `uv run pytest -q` xanh. Ghi `docs/dev-notes/web.md` / `telegram.md`, mục `AGENTS.md`. Không git commit (coordinator commit). Không sửa `research/`, `demo/`, `docs/BRD-*`, `docs/acceptance/*`.
