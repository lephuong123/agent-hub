# M4-B — Đánh giá & vận hành học (ghi chú phát triển)

FR: LN-08 (curator pha 1), LN-10 (golden task + eval gate), LN-13 (digest), LN-17 (hồi quy + tự rollback); FR-SG-09 (Slack digest).
FR-UI-06 (tab Learnings) — làm SAU khi M2 xong (`tui/learnings.py`, xem mục "Tab Learnings" dưới). Thêm tool MCP `hub_steer`.

## Module
| File | Vai trò |
|---|---|
| `evals/golden.py` | `GoldenStore` (`hub.evals.golden`): CRUD golden task (chỉ người — actor agent/learning/review/curator/eval_gate/regression_monitor bị từ chối), xoá mềm, bản sao chỉ đọc `policy/golden/<agent>.yaml`, `candidates()` + `suggest()` → `golden.suggested` |
| `evals/checkers.py` | Bộ chấm tất định: `regex`, `contains`, `json_schema`, `command` (stdin = output, env `AGENT_HUB_OUTPUT_FILE`, cwd = workspace run eval), tổ hợp `all` |
| `evals/judge.py` | LLM judge: `claude -p --output-format json --json-schema --no-session-persistence --system-prompt … --setting-sources "" --strict-mcp-config --tools ""`, rubric người viết; ghi dòng `runs` kind=`eval_judge` (vào ngân sách ngày) |
| `evals/overlay.py` | Bản sao tạm `HUB_HOME/evals/tmp/<eval>/<variant>/` của agent home; `materialize` (ưu tiên `hub.learning.materialize` của A, NotFound → tự dựng từ dict), `materialize_before` (ưu tiên ledger của A), `apply_overlay`/`seed_workspace` (hook trong `RunManager`), `fingerprint` (cache baseline) |
| `evals/gate.py` | `EvalGate` (`hub.evals.gate`): `check`, `post_apply`, `post_apply_batch`, `run_batch_l1`, `run_manual`; khoá theo agent; ngân sách; bảng `eval_runs`/`eval_results`/`eval_entry_log` |
| `evals/mcp_tools.py` | Tool MCP `hub_golden_add|list|show|rm`, `hub_eval_list`, `hub_learning_digest`, resource `hub://digest` |
| `learning_ops/regression.py` | `RegressionMonitor`: `evaluate()` thuần + `check_agent/check_all`, bảng `regression_watch` |
| `learning_ops/curator.py` | `plan_transitions()` thuần (Hermes 14/30 ngày, pin, chưa dùng mà còn trẻ) + `Curator.run(dry_run)`; REPORT.md ở `logs/curator/<ts>/` |
| `learning_ops/digest.py` | `DigestService`: dựng theo team→agent từ event, bảng `digests`, Slack qua event `learning.digest`, `tui_open()` |
| `learning_ops/wiring.py` | `attach(hub)` + `LearningOps` vòng nền (tự start ở `hub.started`): digest 08:00, eval L1 lô 03:00, curator khi rảnh, hồi quy khi có run/signal |
| `learning_ops/ports.py` | Cổng sang A: `ledger_list/ledger_rollback` (Conflict → thử `chain=True`), `record_signal`, `knowledge` |
| `learning_ops/rpc.py`, `cli.py` | RPC `golden.* eval.* digest.* curator.* regression.*` (tự bọc audit — không cần `MUTATING`); CLI `golden`, `eval`, `digest` (+ `learn digest`), `curator`, `regression` |

Migration **v8** (A = v7): `golden_tasks`, `golden_suggestions`, `eval_runs`, `eval_results`, `eval_entry_log`, `regression_watch`, `digests` (`IF NOT EXISTS`).
Config: `eval.*` (gồm `eval.judge.*`, `eval.regression.*`, `eval.ops_tick_s`), `curator.*`, `digest.*`; thêm `learning.digest`, `golden.suggested` vào `notify.slack.events`.

## Hợp đồng với M4-A (đã khớp với code A)
- A gọi `hub.evals.gate.check(proposal)` (L3) / `post_apply(entry)` (L2) — đều **async**, trả `GateVerdict` (có `to_dict()`).
- `post_apply` **B tự rollback** khi tụt (`actor="eval_gate"`); A chỉ gắn cờ `unevaluated` khi `deferred_no_eval`.
- L1: B chấm theo lô hằng ngày (`run_batch_l1`), entry đã chấm ghi `eval_entry_log`.
- eval_failed ở `check` → B ghi Signal `failure` (qua `hub.learning.signals.record`).
- Run kind `eval`/`eval_judge` bị A bỏ qua khi thu Signal (A đã làm).
- **Cần A lưu ý**: khi A lắp memory/skill vào system prompt trong `_prepare_spec`, phải đọc từ `agent["home_path"]` (đã được
  `apply_overlay` đổi sang bản sao) chứ không tra lại DB — nếu không, eval của proposal memory/skill sẽ chấm tri thức THẬT.
  Hiện SOUL + cấu hình đã đúng vì `_prepare_spec` đọc `home/SOUL.md`.

## Sửa ở file dùng chung (ít dòng)
`store.py` (v8), `events.py` (golden.*, eval.*, regression.detected, learning.digest, curator.report), `config.py`,
`runtime/base.py` + `claude_cli.py` (`RunSpec.no_session_persistence` → `--no-session-persistence`),
`runs.py` (overlay trong `tick` + seed workspace + cờ trong `_prepare_spec`), `recovery.py` (không resume/requeue run eval),
`hub.py`/`daemon.py`/`cli.py`/`mcp_server/server.py` (gắn), `notify/slack.py` (format digest + nhắc golden Q-23; `digest_id` trong notify.*),
`guard/classifier.py`: `agent-hub golden <ghi>` và `mcp__*__hub_golden_(add|rm|edit|…)` = `policy_change` forbidden;
**vá lỗ hổng có sẵn**: `uv run|uvx|npx|poetry run … agent-hub approve` trước đây lọt (generic write) → nay bóc trình chạy gói.

## Tab Learnings (FR-UI-06) + màn golden task — `tui/learnings.py`
- `LearningsScreen` (phím `l`, thay chỗ trống M2): header đếm hôm nay (áp dụng/rollback/eval_failed/hoãn/bị chặn),
  dòng **Canary: placeholder M5**, digest gần nhất; bảng feed từ `learn.feed` (A, 7 ngày): lúc, agent, loại, tầng, L1–L3,
  đích, op, **eval trước→sau** (payload `scores` hoặc `eval.list` khớp proposal), entry/proposal, chi tiết; guard chặn
  chạm chính sách an toàn = 🔴. Chi tiết dòng: diff (payload hoặc `learn.show`) + lệnh rollback.
  Phím: `f` lọc · `r` rollback (xác nhận; Conflict chuỗi → hỏi rollback chuỗi) · `p` pin/unpin (`knowledge.pin`) ·
  `D` digest · `j` journey (`learn.journey`) · `G` golden task.
- `GoldenScreen`: bảng golden + agent chưa có golden (cảnh báo Q-23) + run ứng viên; `a` thêm từ run (prefill ứng viên,
  cách chấm nhập nhanh `regex: … | contains: a | b | judge: <rubric> | json: {…} | cmd: …`), Enter xem (kết quả eval gần
  đây), `e` sửa cách chấm/rubric (prefill), Del xoá mềm, `A` hiện đã xoá, `u` khôi phục. Mọi gọi gửi `_audit.channel=tui`
  → `created_by=user:tui`.
- Digest khi mở TUI lần đầu trong ngày: `TopApp.on_mount` → `maybe_show_digest` (`digest.tui_open`); digest rỗng không bật.
- Ảnh: `docs/screenshots/m4/01..07-*.svg` (`AGENT_HUB_SCREENSHOTS=docs/screenshots/m4 uv run pytest tests/test_tui_learnings.py`).
- Tool MCP `hub_steer(run_id, text)` → RPC `run.steer` (M2), annotations readOnly=false/destructive=false/idempotent=false,
  cần token control, audit `run.steer`.

## Chỉ tự rollback thay đổi của vòng học (sửa sau phản hồi của M4-A)
Nghiệm thu lần 1 của A: regression monitor tự rollback một skill do NGƯỜI tạo (`learn propose`, ledger actor=user).
Nay: `ports.AUTO_ROLLBACK_ACTORS = {learning, review, agent, curator}`; regression monitor chọn thay đổi mới nhất CÓ actor
thuộc tập này để rollback; nếu chỉ có thay đổi của người → `regression_watch.status=warned` + event
`regression.detected{human_change: true, hint: "agent-hub learn rollback <id>"}` → digest (dòng ⚠️) + Slack (thêm vào
`notify.slack.events`). Eval gate `post_apply` cũng vậy (cờ `human_change`, không rollback). Migration **v9**:
`regression_watch.actor`. Test: `test_human_change_is_never_auto_rolled_back`, `test_human_change_skipped_but_learning_change_rolled_back`,
`test_post_apply_never_rolls_back_human_change`. (Ở nghiệm thu thật của B, thay đổi effort tạo bằng `learn propose` — actor
user — từng bị rollback; với luật mới nó chỉ được cảnh báo.)

## Lệch BRD / quyết định nhỏ
- Golden task ở DB (writer daemon) + bản sao YAML trong `policy/golden/` (thuộc chính sách an toàn, guard bảo vệ) thay vì chỉ file.
- Ngưỡng hồi quy đặt ở `eval.regression.*` (BRD ghi `learning.watch_runs`/`learning.regression_threshold`, nhưng `learning.*` là của A).
- Lịch digest là `digest.schedule` (BRD: `learning.digest_schedule`). YAML đọc `17:40` không nháy thành số cơ số 60 → code hiểu số là phút.
- Hồi quy: rollback thay đổi MỚI NHẤT còn theo dõi của agent; run eval/review không tính; cancel tính như correction.
- Post-apply không dựng được trạng thái trước → điểm cache gần nhất, không có → ngưỡng tuyệt đối `eval.min_score_abs`.
- Baseline cache theo dấu vân tay trong `eval.baseline_cache_h` (24 h). Dấu vân tay = cấu hình + file tri thức tầng agent của
  bản sao + **system prompt thật** dựng như `RunManager` (`build_knowledge_blocks(..., home=bản sao)` — gồm tầng team/global/USER
  theo phiên bản gán cho agent) + `hub.knowledge.versions.version_map(agent)` (sửa theo phản hồi M5-A: trước đây đổi tri thức
  team/global vẫn dùng baseline cũ). Test: `test_baseline_cache_invalidated_by_team_and_global_knowledge`, `test_fingerprint_tracks_prompt_inputs`.
- Team/global/USER (M5): `check` trả `deferred_no_eval` (`scope_not_supported`).

## Nghiệm thu thật (2026-10-18, claude 2.1.302, haiku, HUB_HOME tạm, Slack giả cục bộ)
Agent `acc` (haiku, effort medium, `--tools ''`). 3 golden task: `from-run` + regex `\b391\b`; json_schema (capital=Paris); LLM judge rubric 1 câu về lá thu.
1. `eval run acc`: 1.0 cả 3 (6 run + 2 judge, $0.0016).
2. `learn propose acc --target config --payload '{changes: {effort: low}}'` → L3 → gate (baseline từ cache) 1.00 → 1.00 **passed** → A tự áp dụng (revision 2).
3. Proposal SOUL "luôn trả BANANA" → gate 1.00 → 0.00 **eval_failed**, cấu hình không đổi.
4. 3 run thật sau thay đổi effort, 2 `feedback wrong` → hồi quy correction 0% → 67% (2/3) → **tự rollback** `actor=regression_monitor`, effort về medium (revision 3); Slack nhận tin rollback.
5. Digest theo lịch (đặt `digest.schedule` = giờ hiện tại) → Slack giả nhận digest có áp dụng/rollback/eval_failed + lệnh `learn rollback`; agent `acc2` chưa có golden → L3 `deferred_no_eval` + Slack nhắc kèm run ứng viên (Q-23).
**Chi phí tổng: $0.0102** (adhoc $0.0014, eval $0.0038, judge $0.0025, 1 review của A $0.0025); run đắt nhất $0.0025.
Lỗi tìm ra khi chạy thật và đã sửa: chi phí run chưa vào `eval_results.cost_usd`; digest mất event cùng mili-giây (đổi mốc sang rowid); schedule YAML cơ số 60; payload `learning.eval_failed` của A để điểm trong `scores`.

## Test (không gọi claude)
`tests/test_eval_golden.py`, `test_eval_gate.py`, `test_regress_monitor.py`, `test_curator_phase1.py` (có ca tích hợp ledger/knowledge thật của A),
`test_digest.py`, `test_tui_learnings.py` (Pilot + daemon in-process + pipeline/ledger thật của A, `hub_steer`); helper
`tests/m4b_fake.py` (`FakeLedger`, judge `claude` giả). `uv run pytest -q` → 313 passed (toàn repo, sau M5-A).

## Việc dở
- Tab Learnings: `L` tắt học cho agent (BRD) chưa gắn — công tắc thuộc `learn policy` của A; feed chưa có dòng `regression.detected`
  (A's `learn.feed` chỉ lấy `learning.*`) — cảnh báo người xem ở digest.
- Eval trước áp dụng tầng team/global/USER + canary (M5).
- Curator pha 2 (LLM, FR-LN-09) — M5; prompt Hermes đã có ở `learning/prompts` của A.
