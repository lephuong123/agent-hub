"""Instructions và resource `hub://huong-dan` của MCP server (FR-MS-04) — tiếng Việt (NFR-25)."""

INSTRUCTIONS = """cfharness điều phối một đội agent (mỗi agent chạy trên `claude` CLI).
THỨ TỰ: hub_status → hub_list_agents → hub_run(agent, prompt) → lặp hub_events(run_id, after_seq, epoch, wait_s≤25) tới khi done=true; kết quả nằm ở trường `result` (summary, cost_usd).
BẤT ĐỒNG BỘ: hub_run trả run_id NGAY, không chờ agent làm xong. Đừng gọi hub_run lại khi retry — truyền idempotency_key.
Truyền lại `latest_seq` và `epoch` của lần gọi trước để chỉ nhận event mới.
PHÊ DUYỆT: nếu `open_requests` khác rỗng, agent đang chờ NGƯỜI cho phép một thao tác phá huỷ. Hỏi người dùng rồi mới gọi hub_approve/hub_deny — KHÔNG tự quyết thay người.
TEAM (M3): hub_team_list → hub_run(team, prompt) → lặp hub_events(task_id, …) tới done=true; `result` là kết quả tổng hợp (lead tách việc, worker làm).
QUYỀN: stdio = control (mọi tool). HTTP token `read` chỉ gọi được tool đọc. Mọi thao tác thay đổi đều ghi nhật ký kiểm toán.
Chi tiết và ví dụ: đọc resource hub://huong-dan."""

GUIDE_MD = """# cfharness — hướng dẫn giao việc qua MCP

cfharness là hub điều phối đội agent. Mỗi agent có persona (SOUL), model, MCP riêng; mỗi lượt chạy (run)
là một tiến trình `claude -p` do daemon của hub quản lý. MCP server này là **client mỏng** của daemon.

## Quy trình chuẩn
1. `hub_status` — hub còn sống, có ESTOP không, chi phí hôm nay.
2. `hub_list_agents` — chọn agent theo `role`/`description`. Chưa có agent phù hợp → `hub_agent_create`.
3. `hub_run(agent="researcher", prompt="…")` → nhận `{run_id, status:"queued", epoch, after_seq}` **ngay**.
4. Lặp `hub_events(run_id, after_seq=<latest_seq lần trước>, epoch=<epoch lần trước>, wait_s=20)`:
   - trả về sớm khi có event mới, hoặc sau tối đa `wait_s` giây với `events=[]` (không phải lỗi);
   - event `tool.start` / `tool.complete` cho biết agent đang dùng tool gì; `message.complete` là lời agent;
   - khi `done=true`: `result.summary` là câu trả lời cuối, `result.cost_usd` là chi phí run.
5. (tuỳ chọn) `hub_feedback(run_id, kind="good|wrong|partial", note="…")` để agent học về sau.

## Ví dụ
```
hub_run(agent="haiku-helper", prompt="Đếm số từ trong câu: 'xin chào thế giới'", idempotency_key="dem-tu-1")
→ {"run_id": "r_ab12…", "status": "queued", "epoch": "e-…", "after_seq": 41}
hub_events(run_id="r_ab12…", after_seq=41, epoch="e-…", wait_s=20)
→ {"events": [...], "latest_seq": 57, "done": true, "result": {"status": "completed", "summary": "4 từ", "cost_usd": 0.004}}
```

## Giao việc cho team (M3)
1. `hub_team_list` / `hub_team_get` — team, thành viên (lead/worker/reviewer), ngân sách còn lại.
2. `hub_run(team="docs", prompt="…", idempotency_key="…")` (hoặc `hub_task_create`) → `{task_id, status}` **ngay**.
   Team có lead và `auto_decompose` → lead tách thành task con có phụ thuộc, hub định tuyến cho worker theo vai trò.
3. Lặp `hub_events(task_id=…, after_seq, epoch, wait_s=20)` tới `done=true` → `result.result_json.final` (kết quả các
   task lá) + `result.subtasks`. `result.status="blocked"` kèm `block_reason` khi cần người.
4. `hub_task_get(task_id)` xem DAG, comment, các lần chạy; `hub_task_comment` để góp ý (worker đọc khi chạy lại).
`cwd` = repo thật: hub khoá ghi theo repo, lưu checkpoint + diff mỗi run (`hub_run_status` có `diff`), hoàn tác bằng
`cfharness run revert <run_id>` (CLI).

## Phê duyệt thao tác phá huỷ
Chỉ thao tác phá huỷ/không hoàn tác (xoá dữ liệu, `git push --force`, tool MCP xoá bản ghi…) mới cần người.
Khi đó `open_requests` (trong `hub_events`, `hub_run_status`, `hub_status`) có mục chờ; run tạm dừng, không tự từ chối.
Hỏi người dùng, rồi `hub_approve(request_id)` (cho phép đúng một lần) hoặc `hub_deny(request_id, note)`.
"Luôn cho phép" (tạo luật lâu dài) chỉ khi mục có `always_offer` khác null VÀ người dùng nói rõ sau khi xem
`always_offer.summary`: `hub_approve(request_id, always=true, confirm_rule=<always_offer.confirm>)` (fingerprint | domain | tên tool MCP).
Không bao giờ tự chọn always; thao tác phá huỷ không bao giờ có luật này.
"Không hỏi lại luật này" (agent đó không bị hỏi lại với `matched_rule` của mục, KỂ CẢ phá huỷ) chỉ khi người dùng nói rõ VÀ chọn
thời hạn: `hub_approve(request_id, grant_rule=true, confirm_rule=<matched_rule>, grant_for="forever"|"8h"|"7d"…)`. Không bao giờ tự chọn.

## Điều khiển
- `hub_cancel(run_id)` dừng run (phá huỷ phần việc đang dở).
- `hub_agent_update(agent_id, patch)` đổi model/role/limits…; `hub_agent_archive` xoá mềm (khôi phục được).
- `hub_estop(on=true, reason)` dừng khẩn cấp toàn hub.

## Giới hạn
- Không tool nào giữ kết nối quá 25 s.
- `level="summary"` (mặc định) không gồm delta token — tiết kiệm context.
- Kết nối HTTP chỉ ở 127.0.0.1 với token; token `read` chỉ dùng được tool đọc.
"""
