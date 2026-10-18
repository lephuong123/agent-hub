# M5-A — Tầng tri thức team/global, USER dùng chung & canary (ghi chú phát triển)

FR: LN-11 (tầng team/global), LN-14 (USER dùng chung), LN-16 (canary/shadow), LN-05 (L3 team/global/USER), RT-10 (M5: lắp
prompt theo phiên bản tri thức gán cho agent, giữ cache). Migration **v10** (M4-B đã dùng v9; M5-B dùng v11).

## Module
| File | Vai trò |
|---|---|
| `knowledge/tiers.py` | `TierVersions` (`hub.knowledge.versions`): phiên bản tầng team/global agent nhìn thấy — `read_memory`, `tier_skills` (ghim bản trước khi có canary; ứng viên eval trong `<home>/.tier_overlay/`), `version_map`, `tier_overview`, `user_profile` |
| `learning/canary.py` | `CanaryManager` (`hub.learning.canary`): eval trước trên mẫu agent → mở canary/shadow → theo dõi (listener run/Signal + tick) → `promoted` hoặc `Ledger.rollback(actor="canary")` |
| `learning/pipeline.py` | Nhánh `_continue_tier` cho L3 scope team/global hoặc `user_profile`; `_resolve_team`; `materialize` dựng ứng viên tầng team/global vào bản sao eval; tra skill theo tầng chỉ định |
| `knowledge/store.py`, `prompt_blocks.py` | `effective_skills` + khối memory đọc tầng team/global qua `versions` (theo agent) |
| `learning/guard.py` | Pin TỪNG DÒNG (`knowledge_pins.target='memory_entry'`, ref = `entry_hash`) chặn patch/remove của vòng học |
| `learning/rpc.py`, `cli.py`, `mcp_server/learning_tools.py`, `hub_self.py` | RPC `knowledge.tier|versions|user`, `learn.canaries|canary_get|canary_check`; CLI `knowledge tier|versions|user`, `knowledge pin … memory_entry --scope`, `learn propose --scope team --team`, `learn canary list|show|check`; MCP `hub_knowledge_tier`, `hub_learn_canaries`; hub-self `propose_memory`/`propose_skill_new`/`memory_view` có `scope` |
| `tui/learnings.py` | Dòng Canary trên đầu tab Learnings (`canary_head`, RPC `learn.canaries`) |

Bảng `tier_canaries`: id, proposal, ledger entry, mode `canary|shadow`, đích (`path`), `before_blob/after_blob`, agent canary,
`watch_agents`, status `canary → promoted | rolled_back | failed`, hạn, eval, observed/baseline.
Config: `learning.canary_runs` (5), `learning.canary_hours` (24), `learning.canary_regression` (ngưỡng 100% = 2× mốc 7 ngày, ≥ 2
sự kiện), `learning.tier_eval_sample` (3), `learning.user_profile_mode` (`shadow`).

## Thiết kế
- **Một bản sống + ghim theo agent**: `Ledger.apply` ghi bản sống (một writer, snapshot/ledger như M4). Khi canary đang chạy, mọi
  agent ≠ agent canary đọc bản TRƯỚC từ blob ledger (memory) hoặc cây giải nén `HUB_HOME/knowledge/versions/<sha>/` (skill; skill mới
  bị ẩn, skill bị archive vẫn hiện). Prompt của các agent đó giống từng byte → giữ prompt cache (đã đo thật: hash w2 không đổi).
  Promote = bỏ ghim; rollback = `Ledger.rollback` → agent khác chưa từng thấy thay đổi. Hàng canary được ghi TRƯỚC `Ledger.apply`
  trong cùng lượt đồng bộ (không run nào dựng prompt ở giữa).
- **Eval trước**: golden task của mọi agent bị ảnh hưởng (team: thành viên; global/USER: mọi agent) có golden; > `tier_eval_sample`
  → mẫu (≥ 3, gồm agent khởi nguồn, ngẫu nhiên theo seed proposal). Gọi `hub.evals.gate.check(proposal + agent_id)` của M4-B song
  song; một agent tụt → `eval_failed`; không ai có golden → `deferred_no_eval` + `golden.suggest`.
- **Canary**: agent khởi nguồn (không có → agent đầu tiên có golden). Cửa sổ 5 run làm việc (adhoc/task) hoặc 24 h. Chỉ số + luật =
  `RegressionMonitor.metrics` + `evaluate` của M4-B. Kiểm ngay khi run của agent theo dõi kết thúc hoặc có Signal; hết giờ ở tick.
- **Shadow (USER)**: một hồ sơ cho mọi agent nên không canary theo agent; eval ở bước trước chính là shadow (chạy trên bản sao, không
  ảnh hưởng run thật), rồi áp dụng cho mọi agent và theo dõi các agent đã eval (gộp cửa sổ). Hồi quy → rollback `actor=canary`.
- **USER theo peer**: `global/USER.md` = hồ sơ dùng chung (chỉ đổi qua L3); `agents/<id>/USER.md` = quan sát riêng của agent (L1/L2,
  chỉ agent đó đọc — không nhiễm chéo); thêm quan sát trùng hồ sơ chung → `one_fact_one_store`. Người pin từng dòng.
- Một đích chỉ một canary; proposal khác cùng đích → `deferred_rate_limited` (`block_reason=canary_active`), tự thử lại khi canary kết thúc.
- Không có bước hỏi người: event `learning.canary` (phase started/rolled_back), `learning.promoted`, `learning.rolled_back`
  (`actor=canary`) → digest/Slack/tab Learnings của M4-B.

## Lệch BRD / quyết định
- Canary rollback áp dụng cho MỌI thay đổi tầng team/global (kể cả người tạo qua `learn propose`) — đây là cơ chế rollout, khác
  regression monitor (B chỉ tự rollback thay đổi của vòng học). Entry bị rollback ở nơi khác (người/B) → canary kết thúc `rolled_back`.
- Cửa sổ 5 run có thể kết thúc trước khi người kịp `feedback` run thứ 5 → đã promoted; hồi quy muộn do regression monitor của B xử lý.
- Tên khoá theo BRD `learning.canary_runs|canary_hours`; ngưỡng ở `learning.canary_regression` (BRD không đặt tên).
- Prompt block giữ nguyên tiêu đề cũ ("USER PROFILE … — shared"/"— team <id>") để không làm đổi hash prompt của agent hiện có.

## Hợp đồng
- **M5-B** (curator liên agent): gọi `hub.learning.pipeline.propose(target="skill", op="add"|"patch", scope="team", team_id=…,
  agent_id=<agent nguồn>, payload={name, description, body}, created_by="curator")` → đi eval + canary như trên; theo dõi kết quả qua
  event `learning.promoted` (payload `proposal_id`) rồi archive bản agent bằng `hub.knowledge.archive_skill` (M4-A). Không ghi thẳng tầng team.
- **M4-B — gợi ý**: `evals/overlay.fingerprint` chưa băm tri thức tầng team/global → baseline cache 24 h có thể dùng điểm của
  phiên bản team cũ (thấy ở nghiệm thu: lần gate thứ 2 dùng baseline cache). Nên thêm `hub.knowledge.versions.version_map(agent)`
  vào dấu vân tay.

## Nghiệm thu thật (2026-10-18, claude 2.1.302, haiku effort low `--tools ''`, HUB_HOME tạm trong scratchpad)
Team `rel` = w1, w2, w3; mỗi agent 1 golden task (17*23 → regex `\b391\b`), `eval.samples=1`.
1. **Team → eval → canary → promoted, không hỏi người**: `learn refine w1 --focus …` → review fork tự gọi `propose_memory scope=team`
   ("The team files tickets with the prefix REL-…") → L3 → eval 3 agent (baseline không có REL-, candidate có — kiểm file prompt)
   đều 1.00 → 1.00 → canary trên w1. Trong canary: prompt w2 = đúng hash trước thay đổi (6444fad…), trả "UNKNOWN"; `knowledge
   versions` w2/w3 = `pinned_before`, w1 = `canary`. 5 run w1 (đều "REL-") → `promoted` tự động; run w3 sau đó có REL- trong prompt.
2. **Team gây hồi quy ở canary → tự rollback trước khi lan**: review w2 đề xuất "ships releases only on Fridays" (scope team) → eval
   đạt → canary trên w2. Trong canary, run w1 và w3 KHÔNG có "Fridays" trong prompt (trả UNKNOWN). 2 run w2 + `feedback wrong`
   (giả lập correction) → ngay sau Signal thứ 2: `learning.rolled_back actor=canary` ("correction 0% → 100% (2/2 run) — rollback
   trước khi lan"), `teams/rel/memory.md` về bản trước, proposal `rolled_back`. Run w1/w3 sau rollback: prompt không có "Fridays".
3. `learn status`: hàng chờ duyệt 0; proposal hôm nay {promoted 1, rolled_back 1}.
**Chi phí tổng: $0.0111** (adhoc 14 run $0.0027, eval 9 run $0.0042, review 2 $0.0042); run đắt nhất $0.0026.

## Test (không gọi claude)
`tests/test_canary.py` (canary promoted/rollback, Signal kích kiểm ngay, hết hạn, eval_failed, không golden, mẫu ≥ 3, đích bận,
rollback nơi khác, skill team ẩn/ghim bản cũ, đề xuất của người, dòng Canary TUI), `test_tier_knowledge.py` (thứ tự prompt,
ứng viên chỉ trong bản sao eval, skill patch/archive overlay, agent ngoài team, global, resolve team), `test_user_profile.py`
(shadow → mọi agent + theo dõi, hồi quy rollback, pin dòng, peer riêng + không trùng), `test_tier_cli.py` (CLI qua daemon thật,
FakeRuntime); helper `tests/m5a_fake.py`. `uv run pytest -q` → 311 passed.

## Việc dở
- Shadow chưa phát lại "run gần đây" (chỉ golden task) — cần bộ chấm cho run không có golden.
- Curator pha 2 / gom skill liên agent: M5-B.
- `doctor`/TUI chưa có màn xem `knowledge versions`; dùng CLI.
