# M1 — luồng B (An toàn & vòng đời): ghi chú phát triển

Phạm vi: FR-SG-01..09 (phần runtime; FR-SG-06 cho agent), FR-RT-09, FR-RT-05 (resume), FR-TK-14, FR-RT-19, NFR-15.
Tất cả test: `uv run pytest -q` (118 test, ~47 s, không gọi `claude` thật).

## 1. Đã làm

### Phân loại (FR-SG-01/02/03, FR-SG-07)
- `guard/shell.py` + `guard/classifier.py`: luật tĩnh tất định. Chuẩn hoá (NFKC, chữ đồng hình Cyrillic/Hy Lạp, ký tự vô hình,
  `${IFS}`, quote, `$'\x72\x6d'`, `\rm`, `/bin/rm`), biến/`export`/`${X:-d}`, alias, `cd` (theo dõi thư mục hiện tại), nối lệnh,
  `sh -c`/`bash -lc`/`eval`/`source`/script file (đọc nội dung nếu đã có), `xargs`, `find -delete/-exec`, wrapper (`sudo`, `env`,
  `nohup`, `timeout`, `command`…), heredoc/pipe vào shell, tải-và-chạy, mã `python/node/perl/ruby -c/-e` (API xoá, lệnh trong
  `os.system`, exec/base64 động), SQL (DROP/TRUNCATE/DELETE|UPDATE không WHERE hoặc WHERE 1=1/ALTER…DROP/Mongo/Redis, kể cả `-f file`
  và `< file`), framework (`manage.py flush`, `rails db:drop`, `prisma migrate reset`), git (push force/delete/+ref/:ref/mirror,
  branch/tag -D, reset --hard, clean -f, checkout/restore bỏ thay đổi, stash drop, filter-branch/repo, reflog expire, gc --prune,
  rebase/amend sau push — có hỏi `git` thật để biết thay đổi chưa commit / HEAD đã push), hệ thống (mkfs, dd thiết bị, diskutil,
  fork bomb, chmod/chown -R hệ thống, crontab -r, shutdown), cloud/k8s/terraform/docker volume, HTTP DELETE.
- "File run tự tạo" (FR-SG-01): thay cho checkpoint FR-RT-12 (M3), dùng **birthtime ≥ lúc run đầu tiên của session vào hàng đợi**
  (cả chuỗi resume dùng chung mốc). Đích không tồn tại chỉ coi là vô hại khi nằm trong cwd của run hoặc `/tmp`.
- Không chắc → `uncertain` = giữ như phá huỷ (biến lạ trong đích xoá, `$(..)` làm tên lệnh, base64/xxd -r, script chưa đọc được,
  shell đọc stdin không rõ, lỗi parse, lỗi bộ phân loại).
- `forbidden` (từ chối thẳng + `guard.blocked`, không mở approval): agent tự duyệt (`cfharness approve|deny`, `python -m cfharness
  approve`, tool MCP `*hub_approve|hub_deny`), đụng socket hub, `cfharness guard protect add|rm`.
- MCP: `destructiveHint`, tên (token `delete/remove/purge/xoa/huy/go_bo/gỡ…`) hoặc mô tả khớp mẫu xoá → phá huỷ bất kể trust;
  `readOnlyHint` → safe; dùng `hub.mcp.tool_annotations` của A: `require_approval` hoặc server `untrusted` (không readOnly) hoặc
  tool chưa probe → giữ chờ (FR-MC-03).
- Bộ test FR-SG-07 (`tests/test_guard_classifier.py`): **163 ca phá huỷ (recall 100%)**, **116 ca thường, báo nhầm 0%**.
- Tài nguyên được bảo vệ: `HUB_HOME/policy/protected.yaml` (người thêm qua `cfharness guard protect add|rm`, có `history/` +
  event `policy.changed` + audit), mặc định theo BRD + **mã nguồn gói `cfharness`** (hook chạy từ đó) + `~/.gnupg|.aws|.kube`.

### Chặn ở runtime (FR-RT-09, FR-RT-17, FR-SG-05)
- settings.json mỗi run: `PreToolUse` matcher `*` → `<python> -I -m cfharness.guard.hook --run … --socket … --timeout 870 || exit 2`,
  `timeout` 900 (tự nâng ≥ `approval.timeout_s`+120). Fail-closed 3 lớp: mọi lỗi trong hook → in deny; `signal.alarm` trước timeout
  của CLI; Python không chạy được → `|| exit 2` (CLI chặn). `-I` để workspace của agent không chèn được module giả.
- `permissions.deny` cho Edit/Write/MultiEdit/NotebookEdit vào `//<HUB_HOME>/policy/**` (đã kiểm với claude thật: bị chặn tĩnh).
- MCP nội bộ `hub-guard` (tự cài giao thức MCP stdio tối thiểu, không phụ thuộc `mcp`) cho
  `--permission-prompt-tool mcp__hub-guard__approve`; trả đúng một text block; lỗi → deny. Gộp vào mcp.json cùng
  `mcp_client.build_agent_mcp_servers(agent)` (+ `agent_mcp_tool_rules` của A đưa vào allowed/disallowed).
- `--disallowedTools` tĩnh (`guard.static_disallowed_tools`: push --force/-f/--force-with-lease, filter-branch, mkfs).
- Mọi quyết định → `guard_decisions` (append-only, trigger) + event `guard.decision` (debug) + `audit.record` của A
  (write/hold/deny; safe không audit để không phình bảng).

### Phê duyệt & waiting_human (FR-RT-09, FR-SG-04)
- `approval_requests` (migration v4). Giữ trong hook tới `approval.timeout_s` (300). Người duyệt trong lúc giữ → tool chạy ngay
  (vé dùng luôn). Hết giờ → request + run `waiting_human`, hook trả deny "đang tạm dừng, đừng thử lại", hub dừng tiến trình
  (`approval.pause_delay_s` sau), **nhả slot, giữ khoá thư mục, không tự từ chối**; agent `waiting`.
- Duyệt sau đó → run mới `--resume <session>` cùng cwd, prompt nêu quyết định; vé một lần theo `action_fingerprint` (lệnh chuẩn hoá,
  bỏ `description`); argv bỏ đúng mẫu `--disallowedTools` khớp thao tác (hook allow không thắng deny tĩnh — spike). Nếu người duyệt
  TRONG lúc giữ mà mẫu tĩnh chặn → hub dừng & resume ngay (đường "restart"). Từ chối → agent nhận lý do (trực tiếp hoặc qua resume)
  + Signal `reject` (bảng `signals` của A). Huỷ run `waiting_human` → approval `cancelled`, nhả khoá.
- Đồng hồ `max_runtime_s` dừng trong lúc giữ; `runs.human_wait_s`.
- Nhắc lại: `notify.reminder_interval_s` (900) nhân đôi mỗi lần tới `reminder_max_interval_s` (7200); event `approval.reminder`.
- RPC: `approval.list {status: open|pending|…}` (`open` = mọi request chưa quyết, gồm waiting_human — khớp `open_requests` của A),
  `approval.get`, `approval.approve|deny {id|request_id, note, channel, actor|by}`. CLI `cfharness approvals|approve <id>|deny <id>`.
  Đã khớp với `hub_open_requests`/`hub_approve`/`hub_deny` của A — **không cần sửa mã A**; test e2e `test_mcp_hub_approve_resolves_approval`.

### Slack (FR-SG-09)
- `notify/slack.py`: listener bus; `approval.open|waiting_human|reminder`, `guard.blocked`, `run.repeated_failure` (≥3 lỗi liên tiếp),
  `learning.rolled_back`. Webhook từ secret `SLACK_WEBHOOK_URL` hoặc bot `SLACK_BOT_TOKEN`+`notify.slack.channel`. Tin đã che
  (`redact`), tool MCP chỉ tên. Lỗi → log + `notify.failed` + backoff 2·2ⁿ s, tối đa 5 lần. Chưa cấu hình → tắt êm.
  `cfharness notify test`. Test bằng HTTP server giả cục bộ.

### Resume/continue & khôi phục (FR-RT-05, FR-TK-14, FR-RT-19)
- `cfharness run continue <run|session> "…"` (RPC `run.continue`), `run resume <run>`; session bận → lỗi rõ.
- Chi phí = chênh lệch luỹ kế session (đã có từ M0, thêm test 3 lượt: 0.01 / 0.015 / 0.005, tổng ngày = 0.03).
- `recovery.reconcile` khi daemon khởi động: run `running` → đối soát `(pid, ps -o lstart)` ±5 s → dừng process group mồ côi
  (pid tái sử dụng thì không đụng) → `interrupted` → tự resume (`--resume`, lời nhắc chuẩn) trừ run có approval chờ (→ waiting_human),
  ESTOP, agent paused/xoá. Daemon dừng có kiểm soát ghi `pending_resume`; run `queued` được xếp lại (M0 cũ huỷ chúng).

### Xoá mềm/purge (FR-SG-06 cho agent), audit (FR-SG-08)
- `cfharness agent purge <id> --confirm <id>` (người gõ lại id); purge tự động theo `retention.soft_delete_days` (`retention.auto_purge`)
  trong vòng nền; agent khởi xướng purge (Bash `cfharness agent purge`) → phá huỷ → approval. Event `agent.purged` + audit.

## 2. Kiểm chứng với `claude` thật (2.1.302, haiku, `--max-budget-usd 0.3`/run, HUB_HOME tạm trong scratch, `approval.timeout_s: 20`)
| Ca | Kết quả |
|---|---|
| (a) `rm -rf dummy` (thư mục có sẵn) | hook giữ, `ap_…` `open`, luật `delete_not_run_created`, `dummy/` còn nguyên |
| (b) chờ > 20 s | approval + run `waiting_human`, `outcome=None` (không tự từ chối), daemon `0 đang chạy` (slot nhả), không vi phạm cô lập |
| (c) `cfharness approve` | run mới `--resume` cùng session, `Bash(rm -rf dummy)` chạy ĐÚNG 1 lần (guard_decisions: hold → allow bằng vé) |
| (d) `ls`, `touch notes.txt`, `echo > notes.txt` | không approval nào |
| thêm: Write vào `policy/` | CLI chặn tĩnh "denied by your permission settings" (luật `//…/policy/**` đúng cú pháp) |
**Tổng chi phí: $0.0031** (5 run).

## 3. Lệch BRD / quyết định riêng
- FR-SG-03 bước 2 (LLM phụ phân loại) **chưa làm**: mọi `uncertain` giữ chờ người (an toàn hơn, có thể báo nhầm ở lệnh động).
- "File run tự tạo" dùng birthtime thay checkpoint (FR-RT-12 là M3). Trên Linux không có birthtime → dùng ctime (gần đúng).
- `protected_resources` lưu ở `HUB_HOME/policy/protected.yaml` (BRD §8.1 ghi bảng DB) — vì FR-SG-05 yêu cầu chính sách nằm trong `policy/`.
- Hook trả `allow` cho safe/write → vượt hỏi quyền mặc định của CLI (đúng mô hình "ghi chạy ngay, có audit"); luật deny tĩnh vẫn thắng.
- Khoá thư mục: bản tối thiểu theo SESSION cho workspace `dir:` (key = realpath cwd), lưu `hub_state.repo_locks`.
- Run tạm dừng giữ bản ghi riêng (status `waiting_human` → `resumed` + `continued_in_run_id`), phần tiếp tục là run mới.
- `TERMINAL_RUN_EVENTS` thêm `run.waiting_human` (để `run --follow`/`until_run_end` kết thúc khi tiến trình dừng chờ người).
- Hạn chế đã biết: agent cùng user OS về lý thuyết vẫn tự viết client socket bằng mã khó phân tích (mã động → `uncertain` → giữ, nhưng
  mã tĩnh mới lạ có thể lọt) — cần token điều khiển cho `approval.*` (M2+). `npm run <script>`/`make` không phân tích nội dung.

## 4. Còn dở / gợi ý cho mốc sau
- LLM phụ cho `uncertain` (FR-SG-03 bước 2); `false_positive` đánh dấu từ TUI.
- TUI: badge/chuông approval (FR-UI-04) — dữ liệu và event đã sẵn.
- FR-RT-12 đầy đủ (khoá theo gốc repo, checkpoint, đối chiếu diff trước khi resume sau restart — hiện resume không kiểm người đã sửa file).
- Digest hằng ngày qua Slack (FR-LN-13, M4). Duyệt bằng nút Slack: đã có — Slack 2 chiều (`docs/dev-notes/slack.md`).
- Chưa thử giữ hook 10 phút–giờ với CLI thật (chỉ 20 s ở đây, 150 s ở spike M0); thiết kế không phụ thuộc vì quá 300 s là dừng & resume.
