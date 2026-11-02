# Web — quản lý + xem memory hệ thống và graph memory (`/memory…`)

Yêu cầu: "thêm phần quản lý + xem được memory của hệ thống + graph của memory trên website". Nhánh `feat/web-memory`.
Web vẫn là client thuần của daemon (không đọc/ghi `hub.db`, không ghi file memory): đọc qua RPC, ghi qua `learn.propose` / `knowledge.pin` /
`learn.rollback` / `learn.graph_consolidate`. Ảnh: `docs/screenshots/web/30-memory-tong-quan.png`, `31-memory-do-thi.png`,
`32-memory-graph-lich-su.png`, `33-memory-tang-agent.png`.

## File
| File | Vai trò |
|---|---|
| `web/memory_pages.py` | Handler + `routes()` (khuôn `skill_pages.py`). Hàm thuần: `entry_key` (= `canary.entry_hash`), `tier_url`, `propose_params`, `graph_params`, `parse_tier`, `safe_json`, `usage` |
| `templates/memory.html`, `memory_tier.html`, `memory_graph.html`, `memory_graph_view.html`, `memory_fact.html`, `memory_entity.html`, `memory_consolidate.html` | Màn mới (khối kết quả ghi dùng lại `_skill_result.html`) |
| `static/memory_graph.js` | Đồ thị node-link: SVG + bố cục lực tự viết (đẩy O(n²), lò xo theo cạnh, kéo về tâm, nguội dần), không thư viện, không dependency mới |
| `learning/graph_view.py` | RPC MỚI chỉ đọc `learn.graph_view` |
| `app.py` | `_memory_routes()`; `base.html`: mục menu "Memory" (nhóm Học & chất lượng + sheet điện thoại); `icons.svg`: `i-graph`; `app.css`: khối cuối file |
| `web/demo.py` | `seed_memory(hub)`: mục memory mọi tầng (qua pipeline, created_by=user) + graph nhỏ có cập nhật (lịch sử) ở tầng agent/team/global |

## Route
| Route | RPC |
|---|---|
| `GET /memory` | `agent.list`, `team.list`, `knowledge.memory` (agent ×2), `knowledge.tier` (team/global), `knowledge.pins`, `learn.canaries{status:canary}`, `learn.ledger{target:memory|user_profile}`, `learn.graph_stats` |
| `GET /memory/{agent|team|global}/{owner|-}` | như trên cho một tầng + `knowledge.versions` (agent: tầng chung nó thấy; team: từng thành viên; global: mọi agent khi có canary) |
| `GET /memory/graph?q=&agent=&tier=&history=1&min_conf=&hops=&entity=` | `learn.graph_view` (danh sách fact) |
| `GET /memory/graph/view?…` + `GET /partials/memory/graph?…` (JSON) | `learn.graph_view` |
| `GET /memory/graph/fact/{id}` | `learn.graph_fact`, `knowledge.pins` |
| `GET /memory/graph/entity/{id}` | `learn.graph_view{entity, hops:1, include_history}` |
| `POST /memory/act/{add|edit|remove|pin|graph_pin|fact_pin|fact_rollback|consolidate_preview|consolidate}` | `learn.propose` / `knowledge.pin` / `learn.rollback` / `learn.graph_consolidate` |

## RPC `learn.graph_view` (HUMAN_ONLY, không thay đổi trạng thái)
`{agent?, scope?, owner?, entity?, query?, hops 1–2, limit ≤300 nút (mặc định 150), include_history?, min_confidence?}` →
`{nodes, edges, center, tiers, truncated, mode}`. Hạt giống: `entity` (id/tên/alias → `GraphStore.neighbors`), `query` (xếp hạng lai
`Retriever.search` như agent tra, kênh vector khi model đã `ready`), không có gì → fact mới nhất. Phạm vi nhìn như `memory.recall`: `agent` →
`graph_tiers` + `graph_hidden` (ghim canary) của agent đó; bỏ trống → mọi tầng. Mọi văn bản trả ra (fact, quan hệ, tên/alias/kiểu/tóm tắt thực
thể) qua `safe_fact` (= `redact_str` + `threats.scan_load`): bị cờ → `null` + `blocked`/`flags`. Không trả văn bản episode. KHÔNG `touch`
(người xem không đổi `hits`/`last_used`, không ghi `mem_fact_uses`) — khác `memory.recall`, vốn tăng `hits` cả khi người tra.

## Quyết định
- **Sửa mục memory theo KHOÁ, không theo chữ trong form**: form gửi `key` (sha1 12 hex của mục); server đọc lại mục hiện tại, tìm đúng một mục
  khớp khoá rồi mới dựng `old_text` cho `learn.propose` (patch thay cả mục / remove). Mục đã đổi từ lúc mở trang → lỗi "tải lại", không ghi nhầm.
- **Đích propose**: USER.md toàn hub = `target: user_profile` (L3 + shadow như CLI); còn lại `target: memory` + `payload.store`. Luôn
  `created_by=user`, `rationale` tiếng Anh. Guard chặn (`status: blocked`) hiện như LỖI (luật + lý do), chữ đã nhập giữ trong phiên (`memory_draft`).
- **Xác nhận**: xoá mục, rollback fact, áp dụng consolidation, bỏ ghim graph luôn xác nhận; thêm/sửa ở tầng team/global xác nhận (áp dụng ngay
  cho mọi agent). JS `data-confirm` + trang xác nhận phía server khi không có JS. Mọi POST qua `post_guard` (CSRF + Origin + khoá phiên WT-1),
  RPC `audit=True` → `audit_log` `channel=web, actor=user`.
- **Rollback fact = rollback cả lô** (ledger entry tạo nó), hộp xác nhận nói rõ; `Conflict` (còn entry sau cùng đích) → toast lỗi, không ghi.
- **Fact bị cờ quét**: danh sách/đồ thị chỉ hiện "bị ẩn"; trang chi tiết fact (`learn.graph_fact` trả văn bản thô cho người) che secret
  + giấu sau `<details>` có cảnh báo — người cần thấy để quyết định rollback. Mọi chữ autoescape; JS chỉ `textContent`/`setAttribute`; JSON
  thoát `<`, `>`, `&`, U+2028/2029 thành `\uXXXX`, `Content-Type: application/json` + `nosniff`.
- **JSON dưới `/partials/`**: chưa đăng nhập → 401 (không redirect), không gia hạn phiên — cùng luật polling hiện có.
- **Fact không có đích** (thuộc tính) không vẽ thành cạnh: hiện trong panel của nút, đếm vào cỡ nút.
- **"Lần đổi gần nhất"** lấy từ `learn.ledger` (500 entry memory + 500 user_profile mới nhất) — tầng có thay đổi cũ hơn cửa sổ đó hiện "—".

## Sửa kèm (ngoài web)
- `graph_quality.consolidate(dry_run=True)` không còn ghi `graph.consolidated_at` (xem trước trên web từng dời lịch consolidation nền).
- `learn.graph_consolidate` / `learn.graph_settle` thêm vào `mcp_server.rpc.MUTATING` → được bọc audit (trước chỉ nằm trong `MUTATING_M4A`
  nên daemon không ghi audit; web/CLI gọi hai method này không để lại dấu).
- `graph_quality.stats`: thêm `last_extraction_at` và `last_ingested_at` theo tầng.

## Test — `tests/test_web_memory.py` (11 ca, không gọi claude, không tải model)
Hàm thuần; mọi route + JSON cần đăng nhập (303/401), thiếu khoá phiên/CSRF sai/Origin lạ → 403; `learn.graph_view` HUMAN_ONLY + không mutating;
thêm/sửa/ghim/bỏ ghim/xoá mục agent đi qua pipeline (ledger actor=user, audit `learn.propose` kênh web, khoá cũ → lỗi, xoá cần xác nhận);
guard chặn → hiện luật, không ghi, bản nháp giữ; XSS mục memory escape; tầng global/team cần xác nhận + tổng quan liệt kê mọi tầng + bảng phiên
bản team; RPC view: phạm vi agent, quét lúc nạp chặn fact bị sửa ngoài pipeline, entity/query/limit, không tăng `hits`; trang graph/JSON/đồ thị/
thực thể/fact escape XSS, JSON content-type + `<`, CSP + không script inline; lịch sử + ghim fact + rollback (xác nhận, fact cũ mở lại, audit
kênh web) + ghim graph tầng; consolidation xem trước (không ghi, không dời lịch) → xác nhận → áp dụng (audit kênh web).

## Kiểm chứng thật
Server demo (`web.demo`, qua wrapper bỏ `seed_manager` — xem dưới) + Chromium (Playwright MCP) 1280×860: `/memory`, tầng agent/team/global,
`/memory/graph` (lọc + lịch sử), fact, thực thể, `/memory/graph/view` — đều 200, console 0 lỗi/cảnh báo (CSP không chặn gì). Đồ thị 15 nút /
11 cạnh, bấm nút → panel fact. Thêm mục memory từ trình duyệt thật (khoá phiên trong localStorage) → `applied`, ledger entry mới.

## Chưa làm / giới hạn
- `python -m cfharness.web.demo` hiện CRASH trên main trước khi tới seed memory: `seed_manager` render template điều phối viên có
  `{repo, branch, base}` trong SOUL → `KeyError` ở `templates.render` (lỗi có sẵn, không thuộc nhánh này). Ảnh chụp dùng wrapper thay
  `seed_manager`/`seed_questions` bằng stub.
- Không có sửa/xoá fact trực tiếp từ web (chỉ rollback theo lô, ghim, consolidation) — thêm fact bằng tay chưa có form.
- Đồ thị: O(n²) mỗi bước, đủ cho ≤300 nút; không có ANN/Barnes-Hut. Không lưu vị trí nút. Không hiện cạnh tới thực thể ngoài trần nút.
- Tầng global trên trang tầng: bảng phiên bản chỉ đọc `knowledge.versions` của mọi agent khi có canary (≤40 agent).
- Ảnh tối/điện thoại chưa chụp.
