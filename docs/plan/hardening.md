# Hardening sau kiểm toán — 3 luồng song song

Nguồn: `docs/acceptance/traceability.md` (mã H-01..H-21, §4 bất biến, §6 race). Script tái hiện của QA:
`/private/tmp/claude-501/-Users-phuonglq-Workspace-agent-hub/e77b5236-4485-4813-8b88-ed8c360a2291/scratchpad/audit/` — mỗi lỗ hổng phải có test hồi quy tái hiện đúng kịch bản QA trước khi sửa (đỏ → xanh).

## Quyết định của coordinator (theo nguyên tắc chủ sản phẩm đã chốt: tự chủ tối đa nhưng không động vào thay đổi của người)
- Canary và mọi cơ chế tự rollback (eval gate, regression monitor, canary) **không bao giờ rollback entry `actor=user`**. Thay đổi tầng team/global do người tạo áp dụng ngay cho cả team, chỉ theo dõi và cảnh báo (digest + Slack). Ghi vào dev-notes; BRD sẽ được cập nhật theo.
- Bất biến an toàn phải đứng vững trước agent chạy CÙNG user OS: không dựa vào việc liệt kê mẫu lệnh làm lớp duy nhất. Cần ít nhất một lớp mà cách viết lệnh khác không lách được (sandbox OS của Claude Code nếu kiểm chứng được, xác thực người gọi RPC, kiểm toàn vẹn chính sách).

## Quyền sở hữu
| Luồng | Mã | Sở hữu chính |
|---|---|---|
| **SEC — An toàn** | H-01, H-02, H-03, H-05, H-12, H-18; phần "canary không rollback người" KHÔNG thuộc SEC | `guard/`, `approvals.py`, `rpc.py`/`daemon.py` (xác thực người gọi), `secrets.py`, `redact.py`, `telemetry/`, `mcp_client/` (probe errlog), `guard/runtime.py` (settings/sandbox), CI `.github/workflows/` |
| **LRN — Học & điều phối** | H-04, H-09, H-15, H-16, H-17, H-19, H-21; canary không rollback `actor=user` | `learning/`, `learning_ops/`, `evals/`, `knowledge/`, `prompt.py`, `runs.py` phần khoá/budget, `dispatch/`, `workspace.py`, `recovery.py` phần retention, `tui/learnings.py` |
| **CLI — Bề mặt & runtime** | H-06, H-07, H-08, H-10, H-11, H-13, H-14, H-20 | `agents.py`, `cli.py`, `dispatch/cli.py`, `runtime/` (trừ settings bảo mật của SEC), `mcp_server/`, `logs.py`, `docs/schema/` |

File dùng chung (`store.py`, `config.py`, `events.py`, `hub.py`, `AGENTS.md`): chỉ thêm, đọc lại trước khi sửa. Migration: SEC = v11, LRN = v12, CLI = v13 (chỗ giữ như trước). Xung đột ở file của luồng khác → nhắn coordinator qua ghi chú trong dev-notes, không sửa thay.

## Ràng buộc
- Test không gọi `claude` thật; kiểm chứng thật (chỉ khi cần, ví dụ sandbox) dùng haiku ≤ $0.3/run, tổng mỗi luồng ≤ $2.
- `uv run pytest -q` xanh cuối cùng. Ghi `docs/dev-notes/hardening-<SEC|LRN|CLI>.md`: mỗi mã H-xx → đã sửa thế nào, test nào chứng minh, còn gì. Không git commit. Không sửa `research/`, `demo/`, `docs/BRD-*`, `docs/acceptance/*`.
