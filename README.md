# agent-hub

Hub điều phối đội agent AI chạy trên `claude` CLI (Claude Code) đã đăng nhập trên máy.
Bản hiện tại: **mốc M0 — lõi chạy 1 agent** (daemon + SQLite + event bus, CRUD agent, run ad-hoc,
giới hạn đồng thời toàn cục, ESTOP, `ClaudeCliRuntime` + `FakeRuntime`).

Tài liệu nghiệp vụ: `docs/BRD-chi-tiet.md`. Quyết định đã chốt: `research/BRIEF.md`.
Kết quả spike cô lập/phê duyệt/steer/ngắt: `docs/spikes/M0-spike-report.md`.

## Cài đặt

```bash
uv sync                 # Python ≥ 3.12 (ghim 3.13 trong .python-version)
uv run agent-hub --help
```

Yêu cầu: `claude` trong PATH và đã `claude auth login` (đã kiểm với 2.1.302). `agent-hub doctor` kiểm các điều này mà không tốn tiền.

## Dùng nhanh

```bash
export AGENT_HUB_HOME=~/.agent-hub          # mặc định; đổi sang thư mục khác để thử nghiệm
uv run agent-hub daemon start               # CLI cũng tự khởi động daemon khi cần
uv run agent-hub agent create researcher --role "Nghiên cứu" --model haiku --budget-per-run 0.2
uv run agent-hub run researcher "Tóm tắt README trong thư mục này" --cwd . --follow
uv run agent-hub runs list
uv run agent-hub runs show <run_id> --events
uv run agent-hub agent edit researcher --model sonnet --set limits.budget_usd_per_day=2
uv run agent-hub agent rm researcher && uv run agent-hub agent restore researcher   # xoá mềm / khôi phục
uv run agent-hub config set hub.max_concurrent_runs 2
uv run agent-hub estop on --reason "bảo trì" --stop-running ; uv run agent-hub estop off
uv run agent-hub daemon stop
```

Mọi lệnh có `--json`. Mã thoát: 0 ok · 1 lỗi người dùng · 2 lỗi hệ thống · 3 bị chặn (ESTOP/ngân sách/chưa đăng nhập).

Chạy thử không tốn tiền: `agent-hub config set runtime.kind fake` (phát lại stdout `claude` đã ghi).

## Bố cục `HUB_HOME`

```
hub.db (SQLite WAL)  hub.sock  hub.pid  config.yaml
agents/<id>/{config.yaml, SOUL.md, MEMORY.md, USER.md, skills/, .archive/, runtime/, workspaces/}
runs/<run_id>.jsonl.gz (stdout thô)  runs/<run_id>.stderr.log
trash/agents/<id>__<ts>/   (xoá mềm, giữ 30 ngày)
logs/hub.log (JSON lines, xoay vòng)  mcp/registry.yaml
```

## Kiểm thử

```bash
uv run pytest           # chỉ dùng FakeRuntime / `claude` giả — không gọi model thật
```
