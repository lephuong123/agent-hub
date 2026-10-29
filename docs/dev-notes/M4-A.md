# M4-A — Lõi học (ghi chú phát triển)

Phạm vi: FR-LN-01..07, 11 (tầng agent), 15, 18, 19; FR-SG-05 (phần học), FR-SG-06 (skill/memory); FR-RT-10, FR-RT-17 (M4); FR-OB-04.
Bản đồ module: `src/cfharness/learning/__init__.py` (docstring), `src/cfharness/knowledge/`.

## Đã làm
- **Kho tri thức** (`knowledge/store.py`): `agents/<id>/MEMORY.md|USER.md` (mục ngăn `\n§\n`, hạn mức `learning.limits` 2.200/1.375),
  `skills/<name>/SKILL.md` (frontmatter name/description, tiếng Anh), chỉ mục DB `skills` (provenance `created_by`, `state`,
  `use_count`, `last_used_at`), pin (`knowledge_pins`), xoá mềm vào `.archive/skills/<name>__<ts>`. Đọc tầng team/global (che khuất
  agent → team → global), ghi tầng team/global = L3.
- **Prompt (FR-RT-10)** `learning/runtime.py`: quy ước → SOUL → memory (global → team → agent) → chỉ mục skill (mô tả ≤ 60 ký tự) +
  hướng dẫn `skill_view`. Đọc từ `agent["home_path"]` (bản sao home của eval M4-B dùng được — có test). Văn bản lưu
  content-addressed `HUB_HOME/prompts/<hash>.md`; run resume/fork dùng lại ĐÚNG prompt của session (hash giống → cache).
  Run có skill được thêm MCP `hub-self` vai trò `knowledge` (`skills_list`, `skill_view` — đếm lượt dùng cho curator).
- **Signal** (`learning/signals.py`, `hub.learning.signals.record(...)`): nguồn cũ (feedback, approval reject — nay có event,
  reviewer reject) + steer/continue có cụm phủ định (regex VI+EN), người dừng run (bọc RPC `run.stop`), `run.reverted` → undo,
  `edit_output` (người sửa file run trước ghi, phát hiện ở checkpoint của run kế tiếp cùng repo, kèm diff), `failure` (lỗi tool
  cùng dấu vân tay ≥ 2 run; quan sát lưu kind nội bộ `failure_obs`), `repeat_pattern` (n-gram tool ≥ 3 run).
  Hook thu tín hiệu (FR-RT-17): KHÔNG cần — suy từ event stream-json/hub (A-11 phương án dự phòng); hook PreToolUse của M1 giữ nguyên.
- **Trigger + review fork** (`learning/review.py`): Signal mức cao → job ngay; bộ đếm turn/tool 10/10; `learn refine`. Gộp
  newest-wins, debounce `learning.review_debounce_s` (mặc định 10 s, để gom nhiều lần sửa), chạy khi agent rảnh; review không chiếm
  slot `max_concurrent_runs`. Run `kind=review`: `--resume <src> --fork-session --session-id <mới>` (đã kiểm CLI 2.1.302: cờ
  `--session-id` đi được với `--fork-session`; `total_cost_usd` của fork LUỸ KẾ cả session gốc → `_record_cost` trừ mốc gốc),
  `--tools Read,Grep,Glob`, `--permission-prompts none`, chỉ MCP `hub-self` vai trò `learning`, không hub-guard/permission tool,
  `--max-budget-usd learning.review_budget_usd`.
  *Sửa 2026-10-29 (dữ liệu thật: 44/126 review chết sau 1–3 lượt, $28.6 phí; 14/37 proposal hoãn bị chặn oan):*
  trần review = `review_budget_usd` (phần khám phá) + token ngữ cảnh lần cuối của session gốc (`runs.model_usage`, `usage` có thể
  = 0) × `review_reload_usd_per_mtok` (mặc định 10), chặn trên `review_budget_max_usd` (mặc định 1.5) — lượt đầu của fork ghi lại
  cả ngữ cảnh vào cache. Review `rate_limited` → xếp lại (backoff `review_retry_backoff_s` nhân đôi, ≤ `review_rate_limit_retries`
  lần), Signal giữ nguyên; `budget_exceeded` chưa có proposal → thử lại `review_budget_retries` lần ở trần, hết lượt → `failed`
  + tiêu Signal. Lịch sử thử ở `review_jobs.result.attempts`. Review fork KHÔNG có trình duyệt (`tool_policy.browser=none`
  trong `learning/runtime.apply_mcp` — trước đây `--chrome` của agent lọt vào → `isolation.violation` giả). Đọc-trước-khi-ghi:
  bằng chứng đọc (sha256 file lúc đề xuất) lưu ở `proposals.payload._read_evidence`, nên proposal hoãn thử lại được sau khi
  review kết thúc / daemon khởi động lại; file đã đổi (trừ patch neo `old_string`) → `superseded{target_changed}`.
- **Prompt Hermes nguyên văn** (Q-13): `learning/prompts/hermes_review.py` (dòng 370–569 `agent/background_review.py`),
  `hermes_curator.py` (CURATOR_REVIEW_PROMPT), chỉ đổi tên tool lúc render (`TOOL_RENAMES`); `NOTICE` ở gốc repo (MIT, Nous Research).
  `threats.py` phỏng `tools/threat_patterns.py` (ghi trong NOTICE).
- **Proposal + pipeline** (`learning/pipeline.py`): kế hoạch thuần → `learning.proposal` → công tắc → guard → L1/L2/L3 → rate →
  (L3) eval gate trước → `Ledger.apply` → `learning.applied` (kèm lệnh rollback) → (L2) `post_apply`. Không có trạng thái chờ duyệt.
  Gate B async → proposal `applying` rồi hoàn tất trong task nền.
- **Ledger** (`learning/ledger.py`): hàm áp dụng duy nhất, blob sha256 `ledger/blobs`, bảng `snapshots`, `ledger_entries`
  append-only (trigger chặn DELETE và sửa trường lõi). Rollback từng entry/chuỗi (`--chain`), rollback của rollback, xung đột khi
  đích bị sửa ngoài ledger (`--force`), cấu hình → revision mới `actor=rollback`.
- **Guard học** (`learning/guard.py`): category `policy_touch | self_escalation | bounds | guard_rule` (+ `rule` chi tiết) → `guard.blocked`.
- **Chính sách học** `policy/learning.yaml` (`learning/policy.py`): `enabled/auto_apply` toàn cục + `agents.<id>` (+ team policies),
  `config_bounds` (Q-20), `rate` (Q-19). CLI `learn policy set` (audit + `policy/history/`); classifier coi `cfharness learn policy set`
  / `knowledge pin` từ agent là `policy_change` (forbidden).
- **FR-OB-04** `learning/cache.py`: tỉ lệ cache theo run/agent, `cache.warning` khi hash prompt của session mới đổi > 3 lần/ngày.
- **Bề mặt**: RPC `learn.*`, `knowledge.*`, `learnself.call`; CLI `cfharness learn …`, `cfharness knowledge …`; MCP `hub_learn_feed|status|ledger|rollback|refine`,
  `hub_knowledge_get` (`mcp_server/learning_tools.py`).
- Migration **v7** (B = v8).

## Lệch BRD / quyết định
- Tool review đặt theo BRD (`propose_*`) + giữ `skills_list`/`skill_view` của Hermes; `propose_skill_patch` gộp `write_file`/`remove_file`.
- Memory: `target_ref` = tên file (`MEMORY.md`/`USER.md`); cooldown 24 h chỉ áp cho skill/cấu hình (memory chịu trần/ngày).
- Review fork KHÔNG giữ nguyên `tools[]` của run gốc (BRD đòi chỉ Read/Grep/Glob + hub-learning) → phần tools/system của prefix khác
  run gốc; nhưng thực đo cache vẫn cao (review: cache_read 70k / creation 13k) nhờ phần hội thoại.
- `USER.md` tầng agent = target `memory` store `user` (L1/L2); `propose_user_profile` = `global/USER.md` (L3).
- Guard: memory dạng mệnh lệnh / không phải tiếng Anh / khẳng định tiêu cực về tool / lỗi môi trường bị chặn (heuristic regex).
- Curator pha 1 của B archive qua `hub.knowledge.archive_skill(owner, name, actor, reason)` → pipeline (`reason` bắt đầu `stale` được
  miễn `absorbed_into`, miễn rate limit).

## Nghiệm thu thật (haiku, HUB_HOME tạm, script `accept.py` trong scratchpad)
Lần 2 (đạt): agent `acc` có skill viết tay `daily-status`; prompt "Today is 2026-10-18. Write today's daily status line."
1. run1, run2 → `Status 2026-10-18: OK` (sai); người `hub_feedback wrong` 2 lần ("dd/mm/yyyy") → MỘT review job (2 Signal, nguồn run2).
2. Review fork: argv có `--resume <run2> --fork-session --session-id <mới>`, `--tools Read,Grep,Glob`, `--permission-prompts none`;
   `system/init.mcp_servers = [hub-self]`, tools built-in chỉ Glob/Grep/Read; hash system prompt = run gốc; gọi `skill_view` →
   `propose_skill_patch` → **applied L1** (ledger actor=review). Transcript session gốc trong `~/.claude/projects` **không đổi** (sha256).
3. run3 (session mới): `skill_view daily-status` → `Status 18/10/2026: OK` — không lặp lỗi.
4. `learn propose` chạm `../../../policy/protected.yaml` và cấu hình `learning.enabled` → `blocked`, `guard.blocked` category `policy_touch`.
5. `learn.status.pending_approval_queue = 0`; không proposal nào ở trạng thái chờ duyệt.
Chi phí: lần 2 $0.0083 (review $0.0038); lần 1 $0.0074; thử cờ fork $0.0013 → **tổng ≈ $0.017** (≤ $3).

Lần 1 phát hiện: skill seed qua `learn propose` (ledger actor=user) bị **regression monitor của M4-B tự rollback** vì sau đó có 2
correction (0% → 100%) — review sau đó không còn skill để vá (đã lưu memory USER.md thay thế). Đề nghị B loại entry `actor=user`
(hoặc thay đổi không do vòng học tạo) khỏi tự rollback; lần 2 seed skill bằng file viết tay để tránh.

## Việc dở / gợi ý
- Canary/shadow, ghi tầng team/global thật (M5). Tab Learnings thuộc B.
- `doctor` chưa hiển thị cảnh báo cache (có `learn cache` + event `cache.warning`).
- Phát hiện `edit_output` chỉ khi workspace là repo git và có run kế tiếp cùng repo.

## Test
`tests/test_knowledge.py`, `tests/test_learn_pipeline.py`, `tests/test_learn_review.py` (+ `tests/fake_claude_learn.py`: `claude` giả
chạy MCP hub-self stdio thật, mô phỏng transcript/fork/chi phí luỹ kế). `uv run pytest -q` → 261 passed.
