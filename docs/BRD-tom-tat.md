# agent-hub — BRD tóm tắt

> Phiên bản 0.7 · 2026-10-18 · Chủ sản phẩm: phuonglq · Bản đầy đủ: `docs/BRD-chi-tiet.md` (mọi mã FR/NFR/R/Q dưới đây trỏ tới bản đó). Số liệu đánh dấu **[ĐỀ XUẤT]** chưa có baseline, cần chủ sản phẩm xác nhận.
>
> **Lịch sử:** 0.1 khởi tạo · 0.2 chốt Q-01/02/03/05/06 · **0.3 nâng tự cải thiện lên mức tối đa — người chỉ duyệt thao tác phá huỷ; thêm nhóm FR-SG; chốt Q-04/08/14** · 0.4–0.6 chốt toàn bộ câu hỏi mở · **0.7 cập nhật theo xây dựng thực tế: tự rollback không bao giờ chạm thay đổi của người; thêm FR-SG-10..12 (sandbox OS, xác thực người gọi, toàn vẹn chính sách); chốt định nghĩa NFR-19; thêm trạng thái hiện thực (mục 9).**

## 1. Vấn đề
Đang dùng Claude Code + Serena + nhiều MCP nội bộ nhưng: (P1) không có chỗ quản lý một *đội* agent bền vững; (P2) không giao được việc lớn cho nhiều agent phối hợp, có phụ thuộc, chịu lỗi; (P3) không xem/điều hướng được nhiều agent đang chạy từ một màn; (P4) agent không học từ cách người sửa/huỷ nên lỗi lặp lại; (P5) tự học không kiểm soát thì dễ hỏng agent, mà bắt người duyệt mọi thứ thì người thành nút thắt; (P6) không có giao thức chung để Claude Code/hệ nội bộ giao việc cho đội agent; (P7) `claude` headless thừa hưởng cấu hình máy (đã thấy hook Serena); (P8) agent ghi vào repo thật và MCP nội bộ có thể xoá nhầm dữ liệu không hoàn tác được.

## 2. Mục tiêu & KPI [ĐỀ XUẤT]
| Mục tiêu | KPI chính |
|---|---|
| G-1 Quản lý đội agent nhanh | Tạo agent → run đầu ≤ 5 phút; 100% thao tác CRUD qua CLI/MCP |
| G-2 Quan sát & điều khiển live | Event → TUI p95 ≤ 1 s; ≥ 5 agent đồng thời (mục tiêu 10); 100% run sống khi đóng TUI |
| G-3 Cắm vào hệ thống qua MCP | Giao việc từ Claude Code ≤ 3 loại tool call; ≥ 90% MCP import được từ `~/.claude.json` |
| G-4 Tự cải thiện tối đa, người tham gia ít nhất | Giảm ≥ 30% correction lặp sau 4 tuần; **0 thay đổi học phải chờ người duyệt**; thay đổi tự áp dụng bị rollback ≤ 15%, bị người rollback thủ công ≤ 5% |
| G-5 Chi phí & an toàn | **0 thao tác phá huỷ chạy khi chưa có người cho phép**; báo nhầm phá huỷ ≤ 5%; 0 đề xuất học chạm chính sách an toàn được áp dụng; overhead học ≤ 15%; cache read ≥ 70%; 0 lần vượt ngân sách không bị chặn; 0 run nhiễm cấu hình máy |

## 3. Phạm vi
**Trong:** hub daemon 1 máy (SQLite, event bus); Agent/Team/Task/Run; runtime adapter `claude` CLI; vòng tự cải thiện tự chủ (snapshot, eval gate, canary, tự rollback, giới hạn tần suất, digest), 3 tầng tri thức, agent tự sửa cấu hình; **bảo vệ thao tác phá huỷ** (danh mục, tài nguyên được bảo vệ, phân loại, chờ người, chính sách an toàn bất biến, xoá mềm); MCP server + client; CLI + TUI kiểu htop; chi phí, ngân sách, audit.
**Ngoài:** tự viết agent loop; gọi Anthropic API trực tiếp; đa nhà cung cấp model; gateway chat; phân tán/HA; đa người dùng phân quyền tổ chức; A2A/ACP; web dashboard đầy đủ; fine-tune; sandbox container.

## 4. Bốn năng lực chính
1. **Quản lý đội agent & điều phối** — Agent = home cô lập + DB chung, xoá mềm (FR-AG-01..08); Team với vai trò và chính sách (FR-TM-01..04); Task DAG, Run tách Task, dispatcher lease/heartbeat `(pid,start_time)`, định tuyến theo vai trò, lead tách việc `--json-schema`, giới hạn đồng thời là setting, circuit breaker, ESTOP (FR-TK-01..14).
2. **Runtime = `claude` CLI** — `AgentRuntime` + `FakeRuntime` (FR-RT-01); ánh xạ cờ CLI (FR-RT-02); cô lập settings/MCP (FR-RT-03); `stream-json` → event hub (FR-RT-04); session/resume/fork (FR-RT-05); steer (FR-RT-06); dừng (FR-RT-07); ngân sách (FR-RT-08); **cầu nối phê duyệt chỉ cho thao tác phá huỷ, 300 s rồi `waiting_human`; mặc định "ghi chạy ngay, có audit"; mọi run trong sandbox OS** (FR-RT-09, FR-SG-10); prompt cache (FR-RT-10); repo thật với khoá/checkpoint/`run revert` (FR-RT-12); hooks hub (FR-RT-17); tương thích phiên bản, rate limit, khôi phục (FR-RT-15/16/19).
3. **Tự cải thiện mức tối đa + bảo vệ thao tác phá huỷ** — Signal (FR-LN-01) → review fork bằng `claude --resume --fork-session`, chỉ đề xuất (FR-LN-02/03) → Proposal không có trạng thái chờ duyệt (FR-LN-04) → **pipeline tự áp dụng L1/L2/L3** (FR-LN-05): tầng agent áp dụng ngay; cấu hình agent, team/global, USER **eval gate bắt buộc trước** (FR-LN-10, M4) + **canary/shadow** cho team/global (FR-LN-16, M5); snapshot + ledger + rollback (FR-LN-06); guard (FR-LN-07); curator (FR-LN-08/09/12); 3 tầng (FR-LN-11/14); **digest "báo chứ không hỏi"** (FR-LN-13); công tắc học do người giữ (FR-LN-15); theo dõi hồi quy + tự rollback (FR-LN-17) — **mọi cơ chế tự rollback không bao giờ chạm thay đổi `actor=user`; thay đổi team/global/USER của người áp ngay, chỉ theo dõi + cảnh báo** (FR-LN-06/16/17); giới hạn tần suất (FR-LN-18); agent tự sửa model/SOUL/tool/MCP/limits trong biên người đặt (FR-LN-19). **Bảo vệ thao tác phá huỷ**: danh mục (FR-SG-01), tài nguyên được bảo vệ (FR-SG-02), phân loại tại PreToolUse + deny-by-default khi không chắc (FR-SG-03), approval chờ người không tự từ chối (FR-SG-04), agent không sửa được chính sách an toàn / không tự nâng quyền (FR-SG-05), xoá mềm mọi thứ trong hub (FR-SG-06), bộ test lách ≥ 200 ca (FR-SG-07), audit (FR-SG-08), Slack (FR-SG-09); ba lớp không phụ thuộc cách viết lệnh: **sandbox OS bắt buộc** (FR-SG-10), **xác thực người gọi** RPC nhạy cảm bằng token người + kiểm cây tiến trình (FR-SG-11), **kiểm toàn vẹn chính sách** khôi phục ≤ 2 s (FR-SG-12).
4. **Tích hợp & giao diện** — MCP server `hub_*` run bất đồng bộ + long-poll, `hub_run_status`, `hub_open_requests`, `hub_approve`/`hub_deny`, `hub_learning_digest`, `hub_learn_*`, `hub_knowledge_*`, `hub_policy_get` chỉ đọc (FR-MS-01..08); MCP client: registry, trust, tool ghi có audit, tool xoá luôn cần người, import `~/.claude.json`, secret (FR-MC-01..06); TUI `top` với cột `waiting_human`, attach/detach, approve (chỉ phá huỷ)/steer/stop, tab Learnings không hàng chờ duyệt, lệnh `guard` (FR-UI-01..10); chi phí, ngân sách, audit (FR-OB-01..08).

## 5. Quyết định đã chốt (BRIEF 2026-10-18)
1. **Runtime = `claude` CLI trên chính máy** (v2.1.302), dùng đăng nhập sẵn có, được chạy nhiều agent, không `--bare` (Q-01); không tự viết loop, không gọi API trực tiếp.
2. **Tự cải thiện mức tối đa** (quyết định bổ sung lần 2, thay quyết định #2): agent **tự** áp dụng thay đổi tri thức agent/team/global, hồ sơ USER, xoá/gộp/archive skill, sửa cấu hình agent — không cần người duyệt; bù bằng xoá mềm, snapshot/ledger, eval gate, canary, tự rollback, giới hạn tần suất, digest. **Chỉ thao tác phá huỷ/không hoàn tác được cần người.** Agent không được sửa chính sách an toàn.
3. Q-02 giới hạn đồng thời/ngân sách là setting · Q-03 ngưỡng 800 ký tự/1 mục (nay là ngưỡng đường nhanh L1) · Q-05 dùng tool ghi MCP nội bộ có audit · Q-06 ghi thẳng repo thật (khoá, checkpoint, `run revert`) · **Q-04** chờ duyệt 300 s rồi `waiting_human`, chờ tới khi có người · **Q-08** Python 3.12+ + Textual · **Q-14** persona/skill/memory tiếng Anh, BRD tiếng Việt.
4. **Quyết định 0.7 (hardening)**: canary/eval gate/regression monitor **không bao giờ** rollback thay đổi của người; bất biến an toàn phải đứng vững trước agent chạy cùng user OS — không dựa vào liệt kê mẫu lệnh làm lớp duy nhất (FR-SG-10..12, 6 bất biến ở §6.10 bản chi tiết).

## 6. Lộ trình
| Mốc | Nội dung | Tiêu chí hoàn thành |
|---|---|---|
| **M0** (1 tuần) | Daemon, SQLite, event bus, Agent CRUD, runtime `claude` + FakeRuntime, run ad-hoc, ESTOP, spike A-01..A-05 | `agent-hub run researcher "…"` chạy thật, event + cost vào DB, không nhiễm hook Serena |
| **M1** (1–2 tuần) | MCP server/client, import, resume, khôi phục, audit, **FR-SG-01..04, 05 (runtime), 06 (agent/team/task), 07, 08; 10–12 thêm ở 0.7** | Claude Code giao việc qua MCP; tool ghi chạy có audit không hỏi; thao tác phá huỷ bị giữ, sau 300 s `waiting_human`, cho phép thì chạy đúng một lần; recall phân loại 100%; khôi phục ≤ 30 s |
| **M2** (1–2 tuần) | TUI htop, attach/detach, replay, approve/steer/stop | 3 agent song song xem live; đóng TUI agent vẫn chạy; baseline KPI G-1..G-3 |
| **M3** (2 tuần) | Team, Task DAG, dispatcher, lead tách việc, ngân sách ngày, rate limit | Lead tách ≥ 2 task con, retry có resume, ngân sách team chặn đúng |
| **M4** (3–4 tuần) | Vòng học tự chủ: pipeline L1/L2/L3-cấu-hình, **eval gate (dời từ M5)**, snapshot/ledger/rollback, theo dõi hồi quy, giới hạn tần suất, tự sửa cấu hình, digest, FR-SG-05 phần học, tab Learnings | Agent tự patch skill sau khi bị sửa 2 lần; đổi model qua eval tự áp dụng, eval tụt thì không áp dụng; hồi quy bị tự rollback; đề xuất chạm chính sách an toàn bị chặn; **không có hàng chờ duyệt** |
| **M5** (2–3 tuần) | Tầng team/global + **canary/shadow**, curator liên agent, USER profile, curator pha 2 | Thay đổi tầng team qua eval → canary → promoted không hỏi người; hồi quy ở canary bị rollback trước khi lan; overhead học ≤ 15% (NFR-19 — **chưa đạt**) |

## 7. Rủi ro top 5
| Mã | Rủi ro | Giảm thiểu |
|---|---|---|
| R-03 | **Tự cải thiện làm hỏng agent/cả team** — nâng lên Cao/Rất cao vì bỏ duyệt người | Pipeline L1–L3, eval trước cho L3, canary, theo dõi hồi quy + tự rollback, snapshot, giới hạn tần suất, digest (FR-LN-05..18) |
| R-17 | **Lách bộ phân loại phá huỷ** (obfuscate, script trung gian, tool MCP tên vô hại) | Chuẩn hoá lệnh, phân tích đích, deny-by-default khi không chắc, `--disallowedTools` dự phòng, bộ test ≥ 200 ca; lớp cưỡng chế thật là sandbox OS + xác thực người gọi + toàn vẹn chính sách (FR-SG-01..03/07/10..12) |
| R-16 | **Agent tự sửa cấu hình gây trôi hành vi** | Eval trước mọi thay đổi cấu hình, cooldown/giới hạn tần suất, `config_bounds`, so với baseline gốc, rollback chuỗi revision (FR-LN-10/17..19) |
| R-04 | Prompt injection từ MCP (tool ghi chạy không hỏi — Q-05) | Audit mọi lần ghi, tool xoá luôn cần người, guard học chặn chỉ thị, chính sách an toàn bất biến (FR-MC-03, FR-LN-07, FR-SG-05) |
| R-06 | Không có cơ chế phê duyệt động/giữ chờ lâu trong `-p` (A-01, A-13) | Spike M0/M1; dự phòng chặn tĩnh + `request_destructive` + tạm dừng & tiếp tục với vé cho phép một lần (FR-RT-09, FR-SG-04) |

## 8. Câu hỏi mở
Đã chốt toàn bộ 23/23 câu (Q-10: event/transcript giữ 30 ngày; Q-12: xác nhận KPI; Q-13: mượn nguyên văn prompt Hermes kèm NOTICE MIT; Q-15: chỉ takeover — Q-07: golden task do người dùng quyết định, agent chỉ gợi ý; Q-09: dừng-và-resume tự động; Q-11: ai cài thì người đó dùng, HTTP chỉ localhost — xem BRD chi tiết mục 14.1). Q-16..Q-23: thông báo qua Slack; `waiting_human` nhả slot, giữ khoá repo; xoá mềm giữ 30 ngày; digest 08:00 hằng ngày; rate limit 5/2/3 + cooldown 24 h; config_bounds opus/sonnet/haiku, ≤ $5/run, ≤ $20/ngày/agent; canary 5 run/24 h; chưa có golden task thì hoãn L3. Không còn câu hỏi mở.

## 9. Trạng thái hiện thực (nghiệm thu v2 sau hardening, 2026-10-18)
- **120/137 yêu cầu đạt (88%)**; FR 94/105 (90%), NFR 26/32 (81%); **Must FR 75/83 (90%)**, thêm 3 lệch có chủ ý đã ghi vào BRD (FR-TK-06, FR-RT-06, FR-MC-01). 416 test xanh. Bất biến an toàn 2–6 đạt; bất biến 1 (phá huỷ luôn cần người) đạt nhưng còn lỗ V2-N1 bên dưới; an toàn dựa vào điều kiện sandbox luôn bật.
- **Must chưa đạt đầy đủ** (giữ yêu cầu, sửa code): FR-AG-03/FR-SG-11 — lỗ **V2-N1 (Cao, chặn phát hành)**: `agent.revert/archive`, `trash.restore/purge` chưa đòi xác thực người gọi; FR-TK-04 (test yếu); FR-SG-01/03/07 — bộ phân loại vẫn heuristic, recall < 100% trên ca lách mới.
- Lỗ khác: **V2-N2 (Trung bình)** — lập lịch hoãn (`crontab`, `at`, git config, LaunchAgents) để mạo danh người.
- **NFR-19 chưa đạt**: định nghĩa chốt = chi phí học ÷ chi phí làm việc (7 ngày trượt), chỉ tiêu ≤ 15%; đo thử 68,6% trên khối lượng nhỏ — cần đo lại trên khối lượng thật ≥ 2 tuần.
- Việc trước phát hành: sửa V2-N1, V2-N2; kiểm chứng thật bằng agent haiku cố tự duyệt và ghi `policy/`; đo NFR-19.
