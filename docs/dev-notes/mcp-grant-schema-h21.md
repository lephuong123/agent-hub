# MCP `hub_approve` grant theo luật, snapshot event schema, H-21 hết chập chờn (2026-10-30)

Ba việc nhỏ còn treo từ `trust-and-chat-settings.md` ("Chưa làm") và `hardening-LRN.md` (H-21).
Test: `tests/test_mcp_approve_always.py` (phần grant_rule), `tests/test_event_schema.py::test_snapshot_lists_every_event_type`,
`tests/test_hardening_lrn.py::test_h21_lock_race_second_task_never_spawned`.

## 1. `hub_approve(grant_rule=true, confirm_rule=<matched_rule>, grant_for=…)`
- Cùng ngữ nghĩa với nút web "Luôn cho phép luật này" / Telegram-Slack "Không hỏi lại luật này…" / `cfharness approve --rule`: tool
  gọi `approval.approve{grant_rule, grant_for_s}` — không lặp logic. Chỉ người (HUMAN_ONLY + `require_human` ở daemon), nonce/token
  MCP không đổi, audit + event `guard.rule_grant` như cũ, channel `mcp:<client>`.
- Như bước 2 bắt buộc của web: `grant_for` PHẢI có (`"forever"` = không hạn, hoặc `30m|8h|24h|7d|30d`…, `guard.cli.parse_duration`);
  không có mặc định "không hạn".
- Như `always=true`: client phải nhắc lại `confirm_rule` = `matched_rule` (đã có trong `hub_open_requests`). Daemon so ở
  `approval.approve{grant_confirm}` TRƯỚC khi quyết; sai → lỗi, approval vẫn chờ, không có grant.
- `always` và `grant_rule` loại trừ nhau; `grant_for` không kèm `grant_rule` → lỗi (không duyệt một lần ngầm).
- Mô tả tool + `guide.py` dặn model: chỉ khi người dùng nói rõ, không bao giờ tự chọn.

## 2. `docs/schema/event.v1.json`
- `guard.trust` (và `guard.rule_grant`, `control.request`, `tool.progress`…) đã được sinh lại ở `af2784a`; chạy lại
  `python -m cfharness.event_schema --write docs/schema/event.v1.json` trên `main` không đổi byte nào.
- Lỗ hổng thật: test cũ chỉ kiểm envelope + "loại đã công bố không bị bớt", nên thêm loại event mà quên sinh lại vẫn xanh (đó là
  lý do `guard.trust` từng thiếu). Test mới: snapshot phải chứa MỌI loại trong `EVENT_LEVELS`, `x-not-persisted` khớp, và cả file
  bằng đúng output của generator. Kiểm: với snapshot trước `af2784a` test báo thiếu `control.request, guard.rule_grant, guard.trust,
  ratelimit.gate, sandbox.network_request, tool.progress`.

## 3. H-21 chập chờn — nguyên nhân
- Không phải race của khoá (code H-21 đúng). Là test mất tín hiệu: task chuyển `running` khi dispatcher claim, nhưng run còn `queued`
  trong lúc `_run` chụp checkpoint git (`_checkpoint`, vài tiến trình `git` — trên Windows thường > 0.5 s). Test gọi
  `rt.release(run_id)` khi `ScriptedRuntime` CHƯA `start()` → chưa có gate → `release` không làm gì; gate tạo sau đó không bao giờ
  được set → run 1 giữ khoá mãi → task 2 không chạy → `settle` hết giờ.
- Sửa test: chờ run của task 1 `running` VÀ gate có trong `rt.gates` rồi mới đếm 0.5 s và release (cửa sổ kiểm "không spawn task 2"
  giờ phủ cả lúc run 1 đã spawn thật).
- Đo trên Windows: trước 7/20 pass (13 fail, đều `hết giờ chờ điều kiện` ở `settle` sau release; lúc release run 1 `queued`,
  không có gate); sau 50/50 pass.
- Còn lại: `FakeRuntime.release()` vẫn "không nhớ" release đến trước `start()`; test khác dùng `hold` + release sớm có thể dính cùng
  kiểu. Chưa đổi ngữ nghĩa runtime giả để không che lỗi khác.
