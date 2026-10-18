# M5-B — Curator liên agent & quan sát (ghi chú phát triển)

FR: LN-12 (curator liên agent → promote lên team), LN-09 (curator pha 2 LLM gộp), OB-07 (OTel tuỳ chọn); đo NFR-19 (overhead học)
và công cụ KPI G-4. Sở hữu: `src/agent_hub/curation/`, `src/agent_hub/telemetry/`, `tests/test_curation*.py`, `test_otel*.py`, `test_overhead*.py`.

## Module
| File | Vai trò |
|---|---|
| `curation/similarity.py` | Hàm THUẦN: điểm tương tự skill = 0.25·Jaccard token tên + 0.75·cosine TF nội dung (bỏ stopword/markdown); `find_clusters` (union-find, chỉ cặp của 2 agent KHÁC nhau, ≥ `min_agents`); `cluster_key`; `merge_deterministic` (gốc = dùng nhiều nhất → thân dài nhất; nối dòng quy tắc riêng của bản khác) |
| `curation/service.py` | `CurationService` (`hub.curation`): `detect`, `cross_agent`, `submit_team_proposal`, archive bản agent sau promoted, `phase2`, chặn tool của run curator, `finalize`, `rollback`, vòng nền `tick` (rảnh + ngân sách), bản ghi `logs/curation/<id>/record.json` + `REPORT.md` |
| `curation/prompts.py` | Pha 2 = `render_curator_prompt()` (CURATOR_REVIEW_PROMPT Hermes NGUYÊN VĂN, chỉ đổi tên tool — Q-13, NOTICE) + ghi chú hub + candidate list. Gộp liên agent: prompt hub + đoạn "Consolidation means DISTILLING…" trích nguyên văn Hermes (ghi nguồn), `MERGE_SCHEMA` |
| `curation/llm.py` | `claude -p --json-schema --no-session-persistence --tools "" --strict-mcp-config` (như judge M4-B); ghi `runs` kind=`curator` (chi phí vào ngân sách/cost) |
| `curation/kpi.py` | `kpi_report` (G-4) + `overhead_report` (NFR-19) — chỉ đọc DB; `render_*` |
| `curation/overhead.py` | Bench FakeRuntime `python -m agent_hub.curation.overhead --home <tmp> [-n 30]` (học tắt vs bật, CPU listener học) |
| `curation/rpc.py`, `cli.py` | RPC `curation.run|detect|phase2|list|show|rollback|status|tick`, `report.kpi|overhead`, `telemetry.status|flush` (method ghi tự bọc audit); CLI `agent-hub curation …`, `report kpi|overhead` (viết lại argv → `report-kpi|report-overhead`), `telemetry status|flush` |
| `telemetry/otel.py` | `OtelExporter` (`hub.telemetry`): listener bus → span OTLP; hàng đợi giới hạn + luồng nền; exporter `otlp_http` (JSON → `<endpoint>/v1/traces`, header có thể `${SECRET}`), `file` (JSONL), `memory` (test) |

Sửa file dùng chung (ít dòng): `config.py` (khoá `curation.*`, `telemetry.otel.*`), `events.py` (`curation.*`), `hub.py` (attach cuối `__init__`),
`daemon.py` (register trước `mcp_server.rpc`), `cli.py` (parser + rewrite argv). **Không có migration** (bản ghi ở file + `hub_state`; snapshot dùng bảng
`snapshots` sẵn có với `kind=bulk_tree`) → slot v11 để trống.

## Luồng
**Liên agent (FR-LN-12)** — mặc định bật, tất định, mỗi `curation.cross_agent.interval_h` (24 h) khi hub rảnh ≥ `curation.min_idle_min`:
team còn hoạt động ≥ 2 thành viên → skill tầng agent không pin → cụm gần trùng (ngưỡng `threshold` 0.5) → nội dung gộp (tất định; `--llm` /
`cross_agent.llm_merge` → `claude -p --json-schema`, `same_class=false` → bỏ cụm) → **proposal L3 `scope=team op=add created_by=curator`**
qua `hub.learning.proposals.submit` (nếu A có) hoặc `hub.learning.pipeline.propose` (đang dùng — A làm phần L3 ngay trong pipeline) →
A: eval trên golden của mọi agent bị ảnh hưởng → canary → `learning.promoted`. Listener của B nghe `learning.promoted` (hoặc `learning.applied` nếu hub
không có `learning.canary`) → archive (xoá mềm) từng bản agent bằng proposal `op=merge absorbed_into=<skill team>` (actor `curator`).
Dấu vân tay cụm trong `hub_state["curation.index"]` → không đề xuất lặp (trừ khi proposal trước `blocked|eval_failed|rolled_back|superseded`).
**Pha 2 (FR-LN-09)** — tắt mặc định (`curation.phase2.enabled`), mỗi `interval_days` (7) / agent, hoặc `agent-hub curation phase2 <agent> [--force]`:
snapshot cả cây `skills/` (blob ledger + `snapshots kind=bulk_tree`) → Run `kind=curator` (`run_opts.learning_review+curator_pass`, hub-self vai trò
`learning`, `--tools Read,Grep,Glob`, `--max-budget-usd min(run_budget_usd, ngân sách còn lại)`). B bọc `hub.learning.learnself_call` (instance attr):
run có `curator_pass` chỉ được `skills_list` (lọc tầng agent), `skill_view`, `propose_skill_new|patch|archive|merge`; **read-before-write** bắt buộc
(cả skill đích hấp thụ), archive phải có `absorbed_into` là skill tầng agent khác đã đọc; lý do archive luôn có tiền tố `consolidated:` (không lách
miễn trừ `stale` của pha 1); ghi qua pipeline `created_by=curator` → ledger `actor=curator` (vẫn được eval gate/regression tự rollback). Kết thúc →
khối YAML "Structured summary" của Hermes → `structured` + cảnh báo `unlisted_archives`. `agent-hub curation rollback <id>` = rollback mọi ledger entry
của lượt (mới → cũ, Conflict → `chain`).
**Ngân sách**: tổng `runs.kind='curator'` từ nửa đêm giờ máy ≤ `curation.daily_budget_usd` (0.5); mỗi run ≤ `run_budget_usd` (0.3).

## OTel (FR-OB-07)
Tắt mặc định; `agent-hub config set telemetry.otel.enabled true` (+ `endpoint`, `headers`). Span: `run <kind>` (trace = băm run_id), `tool <name>` (con của run),
`learning <event>` (learning.*/review.*/curation.*/curator.*/guard.blocked/eval.completed). Thuộc tính theo ALLOWLIST: id, kind, model, outcome, cost, token,
thời lượng, tên tool, is_error, `tool.mcp_server`. KHÔNG bao giờ: prompt, text, input/output/result tool, diff, nội dung MCP. `include_payloads=true` chỉ thêm
input ĐÃ CHE (`redact`) cắt 512 ký tự của tool built-in; `mcp__*` không bao giờ. Lỗi export không ảnh hưởng hub (đếm `errors/dropped`).

## Hợp đồng với M5-A (đã khớp với `docs/dev-notes/M5-A.md`)
1. Promote = `hub.learning.pipeline.propose(target="skill", op="add", scope="team", team_id, agent_id=<agent khởi nguồn>, created_by="curator",
   payload={name, description, body, curation:{id,key,members,score}})` → A: eval trên golden mọi agent bị ảnh hưởng → canary → `learning.promoted`
   (payload `proposal_id`). Nếu A sau này thêm `learning.proposals.submit(...)` cùng kwargs, B tự dùng (dò lúc chạy).
2. Archive bản agent sau promoted: proposal `op=merge absorbed_into=<skill team>` (actor curator). Bản agent TRÙNG TÊN skill team bị che khuất
   (`find_skill(agent)` trả chính nó → guard `absorbed_into`) → rơi về cổng `hub.knowledge.archive_skill(agent, name, "curator", reason)` theo hợp đồng
   M5-A (xoá mềm, ledger actor=curator) — chỉ khi skill team đã tồn tại trên đĩa. Gợi ý A (không bắt buộc): cho `_plan_skill` nhận `absorbed_scope/owner`.

## Nghiệm thu thật (2026-10-18, claude 2.1.302, haiku, HUB_HOME tạm; script `accept_m5b.py`, `overhead_real.py` trong scratchpad)
Team `t` = r1, r2 (mỗi agent 1 golden regex `OK`); r1 `weekly-report`, r2 `weekly-status-report` (gần trùng, viết tay qua `learn propose`).
1. `curation detect`: 1 cụm, điểm 0.7243 (tên 0.67, nội dung 0.74).
2. `curation run --llm`: LLM gộp → proposal scope team, created_by curator → pipeline A: eval 2/2 agent 1.00 → 1.00 **passed** →
   **canary** trên r1 (skill team ghi vào `teams/t/skills/weekly-status-report`, r2 ghim bản cũ). Lượt 2 (cửa sổ mặc định 5 run): 5 run r1 →
   **promoted** tự động → B archive `r1/weekly-report` (pipeline merge, absorbed_into skill team) và `r2/weekly-status-report` (trùng tên → `archive_skill`),
   cả hai **applied**, ledger actor=curator. (Lượt 1 với `canary_runs=2` trước khi có fallback: bản r2 bị chặn do che khuất → đã sửa.)
3. Pha 2 trên agent `w` (`release-notes` + `release-notes-format`): snapshot `bulk_tree` 10 KB → run curator ($0.0028): skill_view cả hai → patch umbrella
   thêm mục "Format rules" (L1 applied) → archive `release-notes-format absorbed_into=release-notes` (L2 applied); structured summary đúng định dạng Hermes,
   không archive nào thiếu trong danh sách. `curation rollback` → 2 rollback entry, nội dung 2 SKILL.md **khớp từng byte** trước lượt.
4. Overhead mẫu thật nhỏ (10 run adhoc haiku câu hỏi 1 dòng, review theo bộ đếm 5 turn): làm việc $0.0018/12.6 s, review $0.0013/1.6 s →
   **thời gian 13.0% (đạt), chi phí 68.6% (vượt)** — run làm việc quá rẻ ($0.0002) so với một review cố định; NFR-19 cần đo trên tải thật (công cụ có sẵn:
   `agent-hub report overhead`). Trong lượt nghiệm thu 1 (2 run làm việc + 5 eval + 2 curator) tỉ lệ vô nghĩa (mẫu số quá nhỏ) — chỉ minh hoạ báo cáo.
   Bench FakeRuntime n=30: thời gian +5.1%, CPU listener học 7.9% thời gian tường.
5. `report kpi`: mốc tuần 1, proposal chờ duyệt 0; chưa đủ 4 tuần để so G-4 (báo rõ).
**Chi phí: lượt 1 $0.0067 + lượt 2 (đầy đủ, canary 5 run) $0.0034 + mẫu overhead $0.0031 → tổng ≈ $0.013** (≤ $3); run đắt nhất $0.0028 (curator pha 2).
Traceback lúc kết thúc script lượt 2 là do script đóng DB khi run eval post-apply nền còn chạy (teardown của script, không phải lỗi hub).

## Lệch BRD / quyết định
- Không bảng DB mới (BRD không đòi bảng curation); không migration.
- Độ tương tự: từ vựng (BRD "từ vựng trước, embedding sau") — embedding chưa làm.
- Pha 2 dùng hub-self + pipeline (giống Hermes: tool ledgered, read-before-write) thay vì một kế hoạch `--json-schema` duy nhất (BRD gợi ý); YAML "Structured
  summary" của Hermes đóng vai output có cấu trúc. Gộp liên agent bằng LLM thì dùng `--json-schema`.
- Run curator dùng system prompt thường của agent (SOUL + tri thức) — chưa có system prompt riêng (cần sửa `runs.py`/`learning/runtime.py`).
- KPI "loại việc" = `task:<role|kind>` hoặc `adhoc`; correction = `CORRECTION_KINDS` của regression monitor.
- NFR-19 tính học/làm việc (overhead "so với không học"), không phải học/tổng.

## Test (không gọi claude)
`tests/test_curation.py` (hàm thuần, prompt nguyên văn, liên agent qua pipeline thật, archive sau promoted, LLM gộp bằng binary giả, tick rảnh/ngân sách,
pha 2 với runtime gọi tool in-process: read-before-write, absorbed_into, pin, memory ngoài phạm vi, snapshot, rollback), `tests/test_otel.py` (tắt mặc
định, chỉ metadata, không lộ secret/MCP, OTLP HTTP cục bộ, lỗi export), `tests/test_overhead.py` (overhead, KPI G-4, rollback metrics, bench).
`uv run pytest -q` → 311 passed (lúc viết).
