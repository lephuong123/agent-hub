# Văn phòng 3D — "Bảng quản lý agent" (roster / mission control trong cảnh)

Chủ hub (2026-11-12): thêm trong `/office` một bảng quản lý MỌI agent; "tham khảo nhiều repo online có cái này, cái nào hay thì
lấy về làm lại ở đây". Nhánh `feat/office-agent-board`. Không thêm endpoint, RPC, quyền, thư viện hay nới CSP.

## 1. Tìm hiểu (repo → ý lấy → vì sao)
Chỉ lấy Ý TƯỞNG; không chép code/asset (một số repo AGPL). Bảng dựng lại thủ tục bằng `CanvasTexture` + `fillText` như `Board` sẵn có.

| Repo (giấy phép) | Thấy gì | Lấy gì |
|---|---|---|
| `Dicklesworthstone/claude_code_agent_farm` (MIT + rider) | Bảng tmux: mỗi agent một hàng — trạng thái (chấm màu working/idle/error…), context %, heartbeat, last activity, lỗi; tổng số ở đầu | **Đèn màu mỗi hàng** + **dải tóm tắt đếm theo trạng thái** ở đầu bảng; cột "lần cuối làm gì" → ở đây là **tool cuối** + **thời gian run** |
| `bjornjee/agent-dashboard` (MIT) | TUI: agent nhóm theo trạng thái (blocked/waiting/running…), Enter nhảy tới pane của agent, y/n duyệt nhanh | **Agent cần người lên đầu nhóm**; **bấm hàng → tới agent** (ở đây: camera bay tới nhân vật + thẻ). Duyệt nhanh KHÔNG lấy: duyệt phải xem đủ lệnh (bảng duyệt 3D sẵn có) |
| `hoveychen/claw-fleet` (AGPL-3.0 — chỉ ý) | Nhóm "Needs you / Running / Waiting / Done"; thời gian chạy + chi phí cạnh nút **Stop** | Huy hiệu **"cần bạn"** (duyệt/câu hỏi) mỗi hàng; **thời gian + chi phí đặt cạnh nút Dừng run** |
| `builderz-labs/mission-control` (MIT) | Roster agent theo vai trò + heartbeat, tổng quan "fleet status", chi phí ở phần Operations | Nhóm theo **team + vai trò team** (lead đầu nhóm); cột **chi phí hôm nay** mỗi agent |
| `pablodelucca/pixel-agents` (MIT) | Văn phòng pixel: mỗi phiên Claude Code là một nhân vật, bong bóng khi chờ quyền; roster nhân vật; không có pause/stop | Xác nhận hướng đã có (nhân vật + bong bóng). Bảng này bù phần pixel-agents thiếu: **điều khiển** (tạm dừng / dừng run) |
| `a16z-infra/ai-town` (MIT) | Thị trấn PixiJS, bấm nhân vật xem chi tiết, nút freeze/unfreeze cả thế giới | Nút **tạm dừng / tiếp tục** theo agent (ESTOP toàn hub đã có ở 2D — cố ý KHÔNG đưa vào cảnh) |

Không lấy: điều khiển hàng loạt (broadcast `/clear`, kill tất cả), sửa cấu hình agent, xoá agent — rủi ro cao, đã có ở trang 2D.

## 2. Thiết kế
- **Vị trí**: giá vẽ đứng ở mép nam **phòng nghỉ** (`world.js`, cạnh `_plant`), quay về camera như bảng "Cần bạn". Lần thử đầu treo tường tây
  trong dải cafe → 2 máy arcade che nửa dưới bảng khi camera nhìn thẳng (bấm hàng trúng máy arcade) — đã đổi; `office_roster_check.mjs`
  bắn 9 tia từ điểm camera của `flyToFrame` tới mặt bảng, phải 0 tia bị chặn. Không chồng kanban/đồng hồ (tường tây), màn hình phòng server, HUD, "Cần bạn".
- **Nội dung** (`static/office/roster.js`, canvas 1400×860, 3,9 m): tiêu đề + số agent; dải tóm tắt (đang làm / cần bạn / lỗi / rảnh / tạm dừng);
  nhóm **Điều phối viên → team theo thứ tự `team.list` → Không team**; trong nhóm: lead → reviewer → verifier → worker, rồi mức "cần chú ý"
  (`waiting_human` → `asking` → `error` → `waiting` → `working` → `queued` → `idle` → `paused`), rồi tên. Mỗi hàng: đèn `STATUS_COLOR`
  (vòng sáng khi đang làm / cần bạn), tên (★ điều phối viên) + trạng thái tiếng Việt · vai trò team, việc (dòng đầu), tool cuối, thời gian run
  (theo PHÚT — texture chỉ vẽ lại khi chữ đổi), chi phí hôm nay (đã xong + run đang chạy), huy hiệu 🛡duyệt / ?câu hỏi, nút.
- **Chia trang**: ~8 hàng / trang; tiêu đề nhóm không đứng một mình cuối trang, sang trang lặp "(tiếp)"; nút ◀ Trước / Sau ▶ ở chân.
- **Bấm**: chỉ khi đang XEM THẲNG bảng (`focusBoard === bảng`); bấm từ toàn cảnh = camera bay tới bảng trước (tránh bấm nhầm từ xa).
  Bấm hàng → `select(id)` (camera bay tới nhân vật + thẻ, như bấm nhân vật). Chế độ đi bộ: [E] gần bảng → xem bảng.
- **Điều khiển** (trên bảng và trên thẻ nhân vật): `⏸ Tạm dừng` / `▶ Tiếp tục` (theo `paused`), `■ Dừng run` (khi có `run_id`).
  `flows.control(name, id, agent)` → `POST /office/do/<name>` KHÔNG kèm `confirm` → server trả `need:"confirm"` (thông điệp của Action 2D:
  "Tạm dừng agent X?", "Dừng run r_…? Agent sẽ ngừng việc đang làm.") → bảng xác nhận 3D; chỉ nút do NGƯỜI bấm ("Đồng ý" / "Dừng run") gửi
  `confirm=yes`. Xong: thông báo + biểu tượng 💤 / ▶️ / ⏹ trên đầu nhân vật, tải lại state.
- **Trình đọc màn hình**: nút sr-only "Xem bảng quản lý agent", mỗi agent "Tạm dừng X" / "Cho X hoạt động lại" / "Dừng run đang chạy của X";
  `#office-panel` đọc toàn bộ danh sách khi xem bảng.

## 3. Hợp đồng
### `/office/state` — thêm vào mỗi phần tử `agents[]` (chỉ số / cờ / id / giờ, KHÔNG chữ tự do mới)
```json
{"paused": false,            // agent.status == paused (tui.snapshot.paused)
 "run_status": "running",    // running|queued khi có run_id, ngược lại null; chỉ từ khoá [a-z_]
 "started_at": "ISO|null",   // giờ bắt đầu run đang chạy, ≤ 40 ký tự
 "run_cost_usd": 0.12,       // chi phí ước tính của run đang chạy
 "cost_today_usd": 0.42,     // tổng chi phí các run đã xong hôm nay (UTC)
 "questions": 1}             // số câu hỏi đang chờ người (question.list{pending})
```
### `POST /office/do/{agent_pause|agent_resume|stop}` (thêm vào `OFFICE_ACTIONS`)
- Ánh xạ ĐÚNG `app.ACTIONS["agent_pause"|"agent_resume"|"stop"]` (RPC `agent.pause` / `agent.resume` / `run.stop`, `id` = id agent / id run),
  cùng `post_guard` (Origin/Sec-Fetch-Site + CSRF + khoá phiên `k`), audit kênh `web`. Các Action này có `confirm` nên `plan_action` LUÔN trả
  bước xác nhận khi chưa có `confirm=yes` — không có RPC ghi nào trước đó. `run_resume`, `feedback`, `estop`, `agent_delete`… vẫn 404.

## 4. Kiểm tra
- `tests/test_office_agent_board.py`: trường state mới (thuần + daemon thật), 401/403 cho 3 thao tác, tạm dừng/tiếp tục/dừng run hỏi xác nhận
  trước rồi mới ghi + audit `web`, danh sách trắng ánh xạ đúng Action 2D có `confirm`, tĩnh (roster.js không dựng HTML/mạng; `flows.control`
  không tự gửi `confirm=yes`), chạy `tests/office_roster_check.mjs` (Node): nhóm/thứ tự, chia trang 0–80 agent, vùng bấm, nút trang, vị trí + không bị che.
- Chạy tay (hub demo `python -m cfharness.web.demo`, Chromium headless SwiftShader): xem bảng → nút "Dừng run" hiện hộp "Dừng run r_…?" (Huỷ),
  "Tạm dừng" → Đồng ý → agent ngủ 💤, nút đổi "▶ Tiếp tục"; bấm hàng → camera tới điều phối viên + thẻ có nút "▶ Tiếp tục". Không lỗi console.

## 5. Còn lại
- Không có số hàng đợi theo agent (snapshot không có); `run_status = queued` hiện "· xếp hàng" dưới tên.
- Thời gian run đổi theo phút (cố ý, đỡ upload texture); màn hình phòng server vẫn hiện m:ss.
- Bảng chỉ đổi trang bằng nút (không cuộn bằng con lăn).

## 6. Bản 2 — "Điều khiển agent" thành bảng 2D (2026-11-12)
Chủ hub: "phần Điều khiển agent 3D cần làm dạng 2D thôi ở góc màn hình, có mọi quyền thao tác để control".
- **Bỏ khỏi cảnh**: giá vẽ "Bảng quản lý agent" (world.js), vùng bấm/phân trang trên canvas, mục [E] khi đi bộ, nút sr-only "Xem bảng quản lý
  agent", và nút ⏸/▶/■ trên thẻ nhân vật. `roster.js` chỉ còn hàm THUẦN (`rosterItems`, `rosterSummary`, `rowControls`, `agentActions`, `fmtAge`).
- **Bảng 2D** `static/office/dock.js` (DOM, góc trên trái; ≤ 640 px: đáy màn hình, cao ≤ 48vh). Thanh tiêu đề luôn hiện số agent + "N cần bạn";
  bấm để thu gọn/mở (nhớ bằng localStorage). Mỗi hàng: đèn trạng thái, tên (bấm → camera bay tới nhân vật + thẻ), trạng thái · vai trò, việc,
  tool · thời gian run · chi phí hôm nay, và **mọi nút**: 🛡 Duyệt, ❓ Trả lời, 💬 Nhắn (agent thường → bảng hội thoại talk.js) / 💬 Chat (điều phối
  viên), ⏸ Tạm dừng / ▶ Tiếp tục, ■ Dừng run. Chọn nhân vật trong cảnh → hàng của nó được đánh dấu + cuộn tới.
- **Ghi**: tạm dừng/tiếp tục/dừng run như cũ — lần gửi đầu KHÔNG có `confirm` → server trả thông điệp xác nhận → hộp xác nhận DOM trong bảng;
  chỉ nút do người bấm mới gửi `confirm=yes` (Esc / Huỷ = thôi). Duyệt / trả lời / nhắn mở đúng bảng 3D sẵn có (đọc đủ lệnh, bước 2…).
  Không thêm endpoint/RPC/quyền; `OFFICE_ACTIONS` giữ nguyên (ESTOP, xoá agent vẫn chỉ ở 2D).
- Khi bảng 3D (duyệt / câu hỏi / chat / nhắn) đang mở: thân bảng 2D ẩn, chỉ còn thanh tiêu đề (không che bảng). Không có WebGL: bảng 2D vẫn
  chạy, chỉ còn nút tạm dừng/tiếp tục/dừng run.
- Chữ agent chỉ qua `textContent`; không style inline (CSP) — màu theo class `st-<trạng thái>` trong office.css.
- Kiểm: `tests/office_roster_check.mjs` (nút mỗi hàng, World không còn bảng agent), `test_dock_static_rules`; chạy tay hub demo (Chromium):
  Tạm dừng → hộp "Tạm dừng agent review?" → Đồng ý → hàng đổi "Tạm dừng" + nút "▶ Tiếp tục"; Duyệt → bảng duyệt 3D, bảng 2D thu lại. 0 lỗi console.

## 7. Bản 3 — UX + mọi quyền điều khiển (2026-11-13)
Chủ hub: "làm UI/UX tốt, phải điều khiển cả những agent đang chưa làm gì — nếu trong team thì chia theo team"; chọn thêm ESTOP + xoá agent.
- **Nhóm theo team**: Điều phối viên → từng team (thứ tự `team.list`) → Không team. Tiêu đề nhóm bấm để thu gọn (nhớ localStorage), dính đầu
  khi cuộn, có chấm đếm (cần bạn / đang làm / lỗi / rảnh / tạm dừng). Team có nút **⛔ Dừng team** / **▶ Chạy lại** (`team_estop`); team đang
  dừng khẩn được viền đỏ + dòng chú thích.
- **Agent rảnh điều khiển được**: nút **▶ Giao việc** (mở bảng hội thoại → run mới, vẫn hỏi xác nhận), Tạm dừng, menu ⋯ → 🗑 Xoá agent.
  Agent đang chạy: 💬 Nhắn (chen vào run) + ■ Dừng run. Hàng không có việc ghi "Chưa có việc".
- **Lọc + tìm**: chip Tất cả / Cần bạn / Đang làm / Rảnh / Tạm dừng / Lỗi (kèm số, ẩn chip rỗng); ô tìm (tên/id/việc) khi ≥ 6 agent.
- **ESTOP toàn hub** (`estop`): nút ⛔ ESTOP → chọn mức (chỉ chặn run mới / chặn + dừng run đang chạy) → xác nhận → bước 2. Khi bật: dải đỏ
  "ESTOP đang bật" + "▶ Cho chạy lại", viền bảng đỏ, nhãn ESTOP trên thanh tiêu đề (thấy cả khi thu gọn).
- **Bước 2 mới ở server** (`OFFICE_CONFIRM2` trong web/office.py) cho `estop`, `team_estop`, `agent_delete` (bật lẫn tắt): sau hộp xác nhận của
  Action 2D, `/office/do` trả `need:"confirm2", kind:"check", check:"Tôi hiểu: …"`; chỉ khi có `confirm2=yes` mới gọi RPC. Bảng 2D: phải tích ô
  mới bấm "Xác nhận lần 2".
- **Xoá agent trong team**: daemon chặn xoá thành viên team → Action `agent_delete` nhận thêm `detach_teams=1` (2D không gửi → như cũ); bảng 2D
  gửi khi agent có team, hộp xác nhận nói rõ "sẽ được gỡ khỏi mọi team trước". Điều phối viên không có nút xoá.
- Kiểm: `test_estop_two_steps_then_audited`, `test_team_estop_and_agent_delete_two_steps`, `test_dock_static_rules`, Node `office_roster_check.mjs`
  (nút mỗi hàng, nhóm/tóm tắt, lọc/tìm). Chạy tay hub demo: Dừng team → chọn mức → xác nhận → tích ô → team viền đỏ + "▶ Chạy lại"; xoá agent
  trong team qua ⋯ → 2 bước → biến khỏi bảng; ESTOP hub → dải đỏ. Màn 390 px: bảng ở đáy, ≤ 55vh. 0 lỗi console.
