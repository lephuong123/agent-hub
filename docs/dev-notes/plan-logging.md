# Plan — Log hệ thống: (1) kiểm soát người dùng, (2) trace log để bắt bug

Trạng thái: **đã duyệt** (2026-11-18, chủ repo chốt "theo đề xuất" — mọi câu hỏi §8 lấy cột "Đề xuất mặc định"). Tiến độ ở §9.
Nguồn: 2 agent khảo sát mã nguồn hiện tại (audit/danh tính và log/trace). Đường dẫn dưới đây tính từ `src/cfharness/`.

---

## 0. Tóm tắt

Hai mục tiêu dùng chung một xương sống: **mã yêu cầu `req_id`** sinh ra ở điểm vào (CLI, web, TUI, Telegram, Slack, MCP,
điều phối viên, lịch) và đi theo suốt luồng RPC → service → run → hook → event → log → audit. Có `req_id` thì:

- **Kiểm soát người dùng**: mỗi dòng audit trả lời được *ai* (principal theo kênh), *làm gì*, *thành công hay bị từ chối*,
  *mất bao lâu*, *gây ra run/event nào*. Nhật ký có chuỗi băm để phát hiện bị sửa, và có cảnh báo khi thấy mẫu đáng ngờ.
- **Trace bug**: `cfharness trace <req_id|run_id>` ghép audit + event + log + guard decision thành một dòng thời gian.
  Lỗi không còn bị nuốt im lặng. Đổi được log level lúc đang chạy. Có lệnh xuất gói chẩn đoán đã che secret.

Làm trong 8 PR, mỗi PR tự đứng được. Có một migration (**v28**), chỉ THÊM cột.

---

## 1. Hiện trạng (tóm từ khảo sát)

### 1.1 Kiểm soát người dùng (audit)

| Đã có | Chỗ thiếu |
|---|---|
| Bảng `audit_log` append-only bằng trigger (`store.py:152-169`), có các cột `ts, kind, actor, channel, agent_id, run_id, target, summary, data` | Không có cột outcome/lỗi/thời lượng/req_id/ip/phiên. Trigger thì ai ghi được `hub.db` cũng drop được, và **không có chuỗi băm** |
| `wrap_with_audit` (`mcp_server/rpc.py:504`) bọc các method thay đổi trạng thái, che `value/soul/passphrase/key`, cắt `prompt` | **Chỉ ghi khi gọi thành công**: thao tác lỗi hoặc bị từ chối không để lại dòng nào (trừ `auth.denied`, `file.*`, `web.login_failed`) |
| Kênh `telegram:<id>`, `slack:<id>`, `manager:<agent>`, `agent` do daemon ép | Kênh `cli`/`tui`/`web` và tên client MCP là **do client tự khai**. `actor` luôn là `user`. Token người dùng chung cho mọi client |
| Web đã ghi `web.login_failed` (ip, lý do) và `web.notify` login/logout/thiết bị | Thao tác web thường **không ghi ip/phiên/thiết bị** |
| `guard_decisions`, `approval_requests`, `ledger_entries` là các vệt riêng | Không có khoá nối audit → run → event, ngoài `run_id`. Daemon có `_audit.run_id` của run gọi nhưng **không lưu** |
| CLI `audit list`, web `/audit`, `/admin/audit` | Không lọc theo actor/khoảng thời gian/outcome, không xuất được. **`audit.list` chưa nằm trong HUMAN_ONLY** |
| — | **Không ghi lượt đọc dữ liệu nhạy cảm**: `secret.list`, transcript (`run.get{events}`, `mcp.events`), `session.search`, `memory.search`, `learn.graph_view`, `guard.decisions` |
| — | Các dòng `guard.trust/grant/sandbox` lấy `by` từ params thường → `actor` rơi về `"hub"`. `guard.repo_deny.rm` không có dòng audit |
| — | Không có cảnh báo dựa trên mẫu audit (ngoài sự kiện đăng nhập web) |

### 1.2 Trace / debug

| Đã có | Chỗ thiếu |
|---|---|
| `logs.setup_logging` → `logs/hub.log` JSON, xoay vòng, gắn `run_id/agent_id` qua contextvar (`logs.py:24`, `:168`) | **Chỉ daemon gọi setup**. Web, MCP stdio, CLI, `update_helper`, import-job đều ghi text thô hoặc stderr |
| `SecretLogFilter` che secret | Web process không có filter che secret. WARNING bị ghi **2 lần** (`hub.log` + `daemon.out`) |
| Event envelope có `epoch/seq` (`events.py:253`), lưu DB | Không có `req_id`. Listener của bus được gọi **không có try/except** (`bus.py:148`): một listener lỗi làm hỏng cả `publish` |
| OTel opt-in, có span run/tool/learning | `trace_id` = băm `run_id`. Không có span cho RPC/web/chat/hook |
| `Daemon._dispatch` log `exception` khi có lỗi bất ngờ | **Không có access log RPC**: không ghi thời lượng/caller. Lỗi `HubError`/`INVALID_PARAMS` trả về client mà không ghi log |
| `daemon logs`, `runs show/logs/follow`, `hub doctor`, `report.hangs` | `daemon logs` lọc bằng chuỗi con và bỏ qua file đã xoay. Không trang web nào xem được log. Doctor không nhìn lỗi gần đây |
| — | Level cứng INFO, không có khoá cấu hình, không bật được DEBUG khi chưa sửa code |
| — | Không có `loop.set_exception_handler`/`sys.excepthook`/`faulthandler`. 47 `create_task` mà lỗi task chỉ lộ thành "never retrieved" |
| — | 521 `except Exception`, trong đó khoảng 138 chỗ `pass/continue/return` thẳng. Nặng nhất: `guard/service.py`, `tui/app.py`, `tui/learnings.py`, `runs.py`, `knowledge/graph/provider.py`, `hub_transfer*.py` |
| — | `web.log`, `update.log`, `import-job.out` **không xoay vòng**. filehub sync chỉ giữ 300 ký tự stderr cuối |
| — | **Không có gói chẩn đoán**: `backup` bỏ `logs/` và `runs/`. Muốn lấy bằng chứng bug phải xin tay từng lệnh |

---

## 2. Nguyên tắc thiết kế (bất biến mới)

1. **Log là metadata, không phải nội dung.** Ở mọi level, log/audit không bao giờ chứa prompt, text trả lời, input/output
   tool, diff, giá trị secret hay token. DEBUG chỉ được thêm *độ dài*, *băm*, *id*, *tên*. Nội dung đã có chỗ riêng (events,
   raw run log) với retention riêng.
2. **Mọi thứ đi qua `redact.py`.** Áp cho mọi process (daemon, web, MCP stdio, helper), mọi handler, và gói chẩn đoán (quét lại).
3. **Audit không bao giờ được làm hỏng thao tác** (giữ như hiện nay). Riêng **thao tác bị từ chối do quyền** phải có dòng audit.
4. **Danh tính do daemon xác định, không tin client tự khai.** Trường client khai (kênh, tên MCP client) được lưu ở cột
   `declared_*` riêng, tách khỏi phần daemon chứng thực.
5. **Agent không đọc được log/audit/gói chẩn đoán.** Thêm `logs/`, `support/` vào sandbox denyRead, thêm `sensitive_read` vào
   classifier. `audit.*`/`logs.*`/`support.*` thuộc HUMAN_ONLY, không thuộc MANAGER_ALLOWED.
6. **Chỉ thêm, không sửa.** Migration v28 chỉ thêm cột và bảng. Envelope event thêm trường tuỳ chọn `req_id`, giữ `v=1`.
7. **Không ghi log trên đường nóng mà phải chờ IO.** Access log RPC đi qua hàng đợi hoặc handler sẵn có. Đo overhead bằng bench NFR-19.

---

## 3. Xương sống chung: `req_id`

- **Định dạng**: `r_<ulid>` (26 ký tự, sắp được theo thời gian). Module mới `trace.py` gồm contextvar `REQ_CTX` với
  `{req_id, principal, channel}`, cùng các hàm `new_req_id()`, `current()` và `bind(...)`.
- **Sinh ở điểm vào**:
  - `RpcClient`/`AsyncHubClient` gắn `params._trace = {req_id}` (CLI, TUI, web, MCP server).
  - `ChatBot` (Telegram/Slack) sinh req_id cho mỗi update.
  - `ManagerService`, `ScheduleService`, `TaskDispatcher`, `auto_resume` sinh req_id con, có `parent_req_id`.
- **Daemon** (`Daemon._dispatch`): nhận `_trace.req_id` nếu đúng định dạng, không có hoặc sai thì tự sinh, rồi `bind`
  contextvar cho cả lượt xử lý. Trả `req_id` trong response (trường `_meta`) để client in ra khi có lỗi:
  *"Lỗi … (mã yêu cầu r_01J…)"*.
- **Lan truyền**:
  - `runs.req_id` (cột mới) = req_id của lệnh tạo run. Run resume/continue mang req_id mới, kèm `parent_run_id` như cũ.
  - Env `CFHARNESS_REQ_ID` cho tiến trình `claude`, để hook guard/observe ghi kèm vào `guard.check`/spool.
  - `HubEvent.req_id` (tuỳ chọn): `Hub.publish` tự điền từ contextvar, event của adapter lấy từ run.
  - `JsonFormatter` thêm `req_id`, `channel`, `pid`, `proc` (daemon|web|mcp|helper|cli), `src` (module:line), `epoch`.
  - Audit có cột `req_id`. OTel lấy `trace_id` từ req_id (run vẫn là span con), thêm span `rpc`.

---

## 4. Mục tiêu 1 — Kiểm soát người dùng

### A1. Mở rộng schema audit (migration v28)
Thêm cột vào `audit_log`, tất cả đều nullable:
`req_id, outcome (ok|error|denied), error_code, duration_ms, principal, declared_channel, session_ref, ip,
caller_run_id, prev_hash, row_hash`. Thêm index `(req_id)`, `(principal, ts)`, `(outcome, ts)`.
`AuditLog.record` nhận thêm các trường này (mặc định lấy từ `trace.current()`).

### A2. Principal — danh tính do daemon chứng thực
Giữ mô hình **một chủ** (Q-11) nhưng gắn nhãn ổn định theo kênh:

| Kênh | `principal` | Nguồn chứng thực |
|---|---|---|
| CLI/TUI qua socket | `os:<user>` (uid của peer) | `LOCAL_PEERPID`/`SO_PEERCRED` sẵn có |
| Web | `web:dev_<băm 8>` hoặc `web:sess_<băm 8>` | Daemon cấp **token riêng cho web process** (khác token CLI), web gửi `session_ref` + `ip` (chỉ khi public) |
| Telegram/Slack | `telegram:<from_id>`, `slack:<user_id>` | `telegram_caller`/`slack_caller` sẵn có |
| MCP stdio/HTTP | `mcp:stdio` / `mcp:http:<tên token>` | Loại token HTTP. Tên clientInfo chỉ vào `declared_channel` |
| Điều phối viên / agent | `manager:<id>` / `agent:<id>` | Daemon ép như hiện nay, kèm `caller_run_id` |
| Nội bộ | `hub:<service>` (schedule, dispatcher…) | Mã |

- Kênh `web`/`tui` chuyển từ "client khai" sang "token xác định". Token web ở `HUB_HUMAN_ONLY`, chung đời với `<socket>.auth`.
- **Câu hỏi Q1 cho chủ repo** (§8): có cần danh tính *từng người* (nhiều người cùng dùng một hub) hay không.

### A3. Ghi cả thất bại
- `wrap_with_audit`: bọc `try/except`. Gặp `Blocked`/`Forbidden`/lỗi quyền thì `outcome=denied`, gặp `HubError` khác thì
  `outcome=error` + `error_code`, sau đó **ném lại** lỗi gốc. Thêm `duration_ms`. Params che như cũ, không ghi traceback.
- `approval.*`/`guard.*` ghi tay cũng nhận `outcome`.

### A4. Ghi lượt đọc nhạy cảm
- Thêm danh sách `SENSITIVE_READS` (cạnh `MUTATING`): `secret.list`, `run.get` khi có `events`, `mcp.events`, `session.search`,
  `memory.search(_self)`, `learn.graph_view`, `guard.decisions`, `audit.list`, `hub.doctor`, `logs.*`, `support.*`.
- Ghi kind `read:<method>` với `target` và số bản ghi trả về (không ghi nội dung). **Gộp**: một dòng cho mỗi
  (principal, method, target) trong 10 phút, kèm `count`, để audit không phình.
- Web bỏ hẳn cờ `audit=True` thủ công (daemon tự quyết theo danh sách).

### A5. Chuẩn hoá actor và vá chỗ sót
- `guard.trust/grant/sandbox/repo_deny` lấy actor/channel từ `_audit` (giữ `by` trong params để tương thích), thêm audit cho
  `guard.repo_deny.rm`.
- Lưu `caller_run_id` từ `_audit.run_id`.
- Rà `eval.run|batch|check`, `digest.run`, `curator.run`, `regression.check`, `managerself.call`: cái nào chưa bọc thì bọc.
- Thêm test kiểm "mọi method ∈ `auth.all_mutating()` đều có audit": test duyệt bảng method của daemon, gọi thử với
  service giả, đòi có ≥ 1 dòng audit. Test này sẽ bắt method mới quên bọc.
- `audit.list` vào HUMAN_ONLY + ca trong `tests/test_sec_auth.py`.

### A6. Chống sửa nhật ký — chuỗi băm
- `row_hash = sha256(prev_hash ‖ canonical_json(các cột cố định))`. `prev_hash` lấy từ dòng cuối và ghi trong cùng
  transaction (daemon là writer duy nhất nên không bị tranh chấp).
- **Neo** định kỳ (mặc định mỗi giờ và khi daemon dừng) hash dòng cuối vào: (a) `integrity` baseline, (b) file metadata admin
  `audit/<u>/…` (root-owned khi có admin), (c) tuỳ chọn gửi digest ngắn vào chat bảo mật Telegram (`security_chats`).
  Ai xoá/sửa dòng trong DB rồi tính lại chuỗi cũng không khớp được với bản neo ở ngoài.
- `cfharness audit verify [--since]` + RPC `audit.verify` (HUMAN_ONLY): phát hiện chỗ gãy, phát event `audit.tamper_detected`
  (level alert) → Telegram/Slack. Dòng cũ trước v28 có `row_hash=NULL`: chuỗi bắt đầu từ dòng v28 đầu tiên.

### A7. Truy vấn, xuất, giao diện
- `AuditLog.query` thêm lọc `actor/principal, since/until, outcome, req_id, kind prefix, q` (tìm trong summary/target) và
  phân trang theo `before_id`.
- CLI: `cfharness audit list --principal --since 24h --outcome denied --req r_…`, `audit export --since --format jsonl|csv`
  (ghi vào `HUB_HOME/exports/`, 0600), `audit verify`.
- Web `/audit`: bộ lọc mới, cột outcome có màu, bấm `req_id` mở **dòng thời gian của yêu cầu** (dùng chung `trace`, B6).
  Thêm thẻ "Hoạt động theo người" (đếm theo principal × ngày).

### A8. Cảnh báo theo mẫu audit — `audit_watch.py`
Listener audit (tap sẵn có trong `AuditLog.record`) chạy luật, phát `audit.alert{rule, principal, count, window}` (chỉ
metadata) → notifier sẵn có, chống trùng và báo khi hết. Luật mặc định (ngưỡng nằm trong config `audit_watch.*` ∈ SECURITY_KEYS):

| Luật | Mặc định |
|---|---|
| `denied_burst` | ≥ 5 `outcome=denied` / 10 phút / principal |
| `login_fail_burst` | ≥ 5 `web.login_failed` / 15 phút (theo IP và toàn cục) |
| `security_change` | Mỗi lần đổi `gates.mode`, `approval.profile`, `guard.enabled`, `guard.sandbox.*`, trust full, grant luật, `workspace.confine` |
| `secret_access` | `secret.list`/`secret.set` ngoài giờ cấu hình (tuỳ chọn) |
| `new_principal` | Principal lần đầu xuất hiện (thiết bị web mới, user Telegram mới, token MCP mới) |
| `mass_read` | ≥ N `read:*` / 10 phút (gợi ý dò dữ liệu) |
| `tamper` | `audit.verify` gãy, `guard.tamper_detected` |

### A9. Retention audit
Hiện audit giữ vĩnh viễn trong DB. Đề xuất `audit.archive_after_days` (mặc định **0 = không lưu trữ**, giữ nguyên hành vi cũ).
Khi bật thì dồn dòng cũ ra `HUB_HOME/audit-archive/<yyyy-mm>.jsonl.gz` (0600, giữ chuỗi băm, có manifest), DB giữ dòng neo.
Việc này cần nới trigger `audit_no_delete` theo kiểu `retention_cutoff` của v12 → **Q3** (§8).

---

## 5. Mục tiêu 2 — Trace log bắt bug

### B1. Nền tảng log cho mọi process
- `logs.setup_logging(proc=…)` dùng cho daemon, web (`web.log` → `web.jsonl`), MCP stdio (`mcp-<pid>.jsonl`, không ghi
  stdout vì stdout là kênh MCP), CLI (chỉ khi `--debug`, file `cli.jsonl`).
- `update_helper`/import-job chỉ được dùng stdlib: viết mini JSON formatter ngay trong helper, cùng schema field.
- Mọi file đều xoay vòng (`logs.max_bytes`), cùng `max_age_days`. filehub sync: stderr ghi vào `filehub-sync.log` xoay vòng thay vì giữ 300 ký tự.
- `SecretLogFilter` cài cho mọi handler của mọi process.
- Bỏ ghi trùng: `daemon.out` chỉ nhận stderr thô (tracebacks C, thư viện ngoài), không nhận record `cfharness`.
- Các `print(` trong module không phải CLI (khoảng 43 chỗ) chuyển sang `log.*`.

### B2. Log level đổi lúc chạy
- Config `logs.level` (mặc định INFO) và `logs.levels: {"cfharness.guard": "DEBUG", …}`, đọc lại theo mtime như mọi setting.
- `cfharness logs level <logger> debug --for 30m`: tự hạ về mức cũ khi hết giờ (lưu `hub_state logs.temp_levels`), để không
  quên bật DEBUG mãi.
- Áp dụng nguyên tắc §2.1: DEBUG cũng không chứa nội dung. Thêm test quét record DEBUG khi chạy bộ fixture, đòi không chứa
  chuỗi prompt/secret mẫu.

### B3. Access log RPC
`Daemon._dispatch` ghi một record `rpc` cho mỗi lượt: `method, req_id, caller.via, principal, duration_ms, outcome, code`.
- Method đọc (không lỗi) ghi ở DEBUG. Method ghi, `HubError`, lỗi quyền ghi ở INFO/WARNING. `SYSTEM_ERROR` ghi ở ERROR kèm traceback.
- Gọi chậm hơn `logs.slow_rpc_ms` (mặc định 2000) → WARNING `rpc.slow`.
- Bỏ qua `events.subscribe` (luồng dài): chỉ ghi lúc mở và lúc đóng (thời lượng, lý do đóng, `slow_consumer`).

### B4. Không để lỗi chết im lặng
- `loop.set_exception_handler` → log ERROR + đếm. `sys.excepthook`/`threading.excepthook` → log. `faulthandler.enable(logs/faults.log)`
  để có stack khi daemon treo/segfault (gửi `SIGUSR1` thì dump stack mọi thread: `cfharness daemon dump-stacks`).
- Helper `tasks.spawn(coro, name=…)` thay cho 47 chỗ `create_task`: giữ ref, `add_done_callback` log exception kèm tên task + req_id.
- `EventBus.publish`: mỗi listener bọc `try/except`, lỗi → log + bộ đếm `bus.listener_errors`, không làm hỏng các listener khác.
- **Quy tắc nuốt lỗi**: `except Exception` phải có (a) `log.*(..., exc_info=True)` (ít nhất DEBUG), hoặc (b) chú thích
  `# fail-closed: <lý do>`/`# best-effort: <lý do>`. Kiểm bằng script `scripts/check_broad_except.py` trong test
  (danh sách ngoại lệ hiện có được đóng băng làm baseline, chỉ được GIẢM). Dọn theo thứ tự module ở §1.2.
  **Không đổi ngữ nghĩa fail-closed của guard/hook**, chỉ thêm log.

### B5. Bộ đếm lỗi và sức khoẻ
- `hub.status`/thẻ Sức khoẻ thêm: số ERROR/WARNING trong 1 h/24 h theo logger, `bus.listener_errors`, `rpc.slow`, task chết.
- `hub doctor` thêm mục "Lỗi gần đây" (top 5 thông điệp ERROR gom theo `logger+msg` mẫu, có req_id mẫu).

### B6. Công cụ tra cứu
- `cfharness logs [--since 2h] [--level warning] [--logger cfharness.guard] [--req r_…] [--run …] [--agent …] [--proc web] [-f]`:
  lọc **theo trường JSON**, đọc cả file đã xoay/`.gz`, gộp mọi file `*.jsonl` theo `ts`. `daemon logs` giữ làm alias.
- `cfharness trace <req_id|run_id|task_id>` + RPC `trace.get` (HUMAN_ONLY): dòng thời gian gộp
  **audit → log → event (summary) → guard decision → approval → run con**, theo `req_id`/`parent_req_id`/`parent_run_id`.
  Có `--json`.
- Web "Nhật ký hệ thống" (`/logs`, HUMAN_ONLY, tuân CSP): bảng log có lọc + trang `/trace/<id>`. Chữ là dữ liệu: autoescape,
  không `|safe`.

### B7. Gói chẩn đoán (support bundle)
`cfharness support bundle [--since 24h] [--run R …] [--include-raw]` + thẻ ở Cài đặt (`support.create`, HUMAN_ONLY, xác nhận):
- Nội dung: log `*.jsonl` trong khoảng thời gian, `faults.log`, `hub doctor --json`, `hub.status`, phiên bản (cfharness,
  claude, python, OS), config **đã lọc** (bỏ `SECURITY_KEYS` có giá trị nhạy cảm), event summary + audit metadata của các run được
  chọn. `runs/<id>.jsonl.gz` **chỉ khi `--include-raw`** (vì có nội dung hội thoại).
- **Không bao giờ**: `hub.db`, `secrets/`, `auth/`, `mcp_server/tokens.json`, session Claude, checkpoint.
- Quét lại toàn bộ bằng giá trị secret (`redact`) **trước khi** đóng gói. Có dòng nào khớp → che + ghi `redaction_report`.
- In danh sách file + kích thước, người xác nhận rồi mới ghi `HUB_HOME/exports/support-<ts>.tar.gz` (0600).
  `support/`/`exports/` đã ∈ sandbox deny.

---

## 6. Chia PR

| PR | Nội dung | Phụ thuộc | Test chính |
|---|---|---|---|
| **L1** Nền tảng log | B1, B2, B4 (excepthook, `tasks.spawn`, bus an toàn, faulthandler), bỏ ghi trùng | — | `tests/test_logging.py`: JSON field, xoay vòng mọi file, filter che secret ở web/MCP, level theo mtime, `--for` tự hạ, listener lỗi không phá bus |
| **L2** `req_id` | §3: `trace.py`, client gắn `_trace`, daemon bind + trả `_meta.req_id`, `runs.req_id`, env hook, `HubEvent.req_id`, OTel trace_id; B3 access log | L1 | `test_trace_id.py`: một lệnh web → cùng req_id ở audit, run, event, log, guard decision; req_id sai định dạng bị thay; schema event chỉ thêm trường |
| **L3** Audit đầy đủ | Migration **v28**; A1, A2 (token web riêng), A3, A4, A5 | L2 | `test_audit_full.py`: ghi denied/error, gộp read, principal mỗi kênh, client khai `channel=cli` qua token web vẫn thành `web`; test "mọi method mutating đều có audit"; `test_sec_auth.py::audit_list_human_only` |
| **L4** Chuỗi băm | A6: hash, neo, `audit verify`, event `audit.tamper_detected` | L3 | Sửa/xoá dòng bằng sqlite thô → verify báo đúng vị trí; tính lại chuỗi → bản neo bắt được |
| **L5** Truy vấn + cảnh báo | A7 (CLI/RPC/export/web), A8 `audit_watch` | L3 | Mỗi luật: ca đỏ→xanh, chống trùng, báo khi hết; export không chứa trường đã che; web XSS ở summary/target |
| **L6** Công cụ debug | B5, B6: `logs`, `trace`, `/logs`, `/trace`, doctor "Lỗi gần đây" | L2 | `trace` ghép đúng thứ tự từ fixture; đọc `.gz`; web autoescape |
| **L7** Gói chẩn đoán | B7 | L1, L6 | Gói không chứa tên file cấm; chèn secret giả vào log → bị che + có trong report; `--include-raw` mặc định tắt; agent gọi → forbidden |
| **L8** Dọn nuốt lỗi | B4 quy tắc + script baseline; dọn module nặng | L1 | Script chạy trong pytest; baseline chỉ giảm |

L5/L6/L7 làm song song được sau khi L3 (hoặc L2) xong. Mỗi PR thêm một dòng vào mục "Chưa phát hành" của `CHANGELOG.md`.
L3 có "Lưu ý khi cập nhật" (migration v28, token web mới: web cần khởi động lại sau khi cập nhật).

---

## 7. Đo nghiệm thu

- **Kiểm soát**: 100 % method ∈ `all_mutating()` có audit (test tự động). Thao tác bị từ chối có dòng `denied`. Mọi dòng từ
  v28 có `principal` do daemon chứng thực và `req_id`. `audit verify` phát hiện 100 % ca sửa/xoá trong test.
- **Trace**: chọn ngẫu nhiên 1 thao tác web/Telegram → `cfharness trace <req_id>` ra đủ chuỗi (audit, run, event, log) trong < 2 s
  trên hub có 1 tháng dữ liệu. Không còn task asyncio chết mà không có log ERROR. Số `except Exception` không log/không chú thích
  ≤ baseline và giảm dần.
- **Hiệu năng**: overhead daemon (NFR-19 bench `report overhead`) tăng ≤ 3 %. Access log RPC không chặn loop.
- **An toàn**: test quét log/audit/gói chẩn đoán từ bộ fixture không thấy secret/prompt mẫu. Agent đọc `logs/` bị sandbox +
  classifier chặn (`selftest --live` thêm 1 `Case`).

---

## 8. Câu hỏi cần chủ repo chốt

| # | Câu hỏi | Đề xuất mặc định |
|---|---|---|
| **Q1** | Có cần danh tính **từng người** (nhiều người cùng dùng 1 hub, mỗi người một token/tài khoản web) hay vẫn **một chủ** + principal theo kênh/thiết bị? | Giữ một chủ + principal (L3). Danh tính từng người là plan riêng, vì đụng auth/web/chat |
| **Q2** | Ghi lượt đọc nhạy cảm ở mức nào? | Danh sách `SENSITIVE_READS` ở A4, gộp 10 phút |
| **Q3** | Audit giữ vĩnh viễn trong DB hay cho lưu trữ ra file sau N ngày? | Mặc định giữ vĩnh viễn, có tuỳ chọn lưu trữ (A9) |
| **Q4** | Neo chuỗi băm gửi digest ra Telegram bảo mật? | Tắt mặc định, chỉ neo vào integrity + file admin |
| **Q5** | Gói chẩn đoán có cho kèm raw transcript không? | Chỉ khi `--include-raw` + xác nhận bước 2 |
| **Q6** | Thứ tự ưu tiên: kiểm soát (L3–L5) trước hay debug (L6–L7) trước? | L1 → L2 → L3 → L6 → L4 → L5 → L7 → L8 |

---

## 9. Tiến độ

### L1 — Nền tảng log (nhánh `feat/logging-l1`)
Đã làm:
- `logs.setup_logging(proc=, filename=, shared=)`: mỗi tiến trình một file JSONL — daemon `hub.log`, web `web.jsonl`, MCP stdio
  `mcp.jsonl` (`shared=True`: append + xoay lúc mở, vì nhiều `mcp serve` cùng ghi). Trường mới: `proc`, `pid`, `src` (module:dòng),
  `ts` lấy từ lúc tạo bản ghi; sẵn chỗ cho `req_id`/`channel` (L2). Handler ở root ghi log thư viện ngoài (asyncio, mcp, uvicorn…) vào
  CÙNG file, qua handler đích (một luồng xoay vòng), không lặp bản ghi `cfharness`. `SecretLogFilter` gắn trong `setup_logging` → mọi
  tiến trình đều che. `setup_process_logging` cho web/MCP: lỗi IO (sandbox, đĩa) chỉ bỏ qua.
- Bỏ ghi trùng: handler stderr của `install_stderr_masking` không ghi khi tiến trình đã có log JSONL (`file_logging_active`).
- Level đổi lúc chạy: `logs.level` + `logs.levels` trong config, level tạm `hub_state logs.temp_levels` (60 s – 24 h, tự hết hạn);
  `LevelController` + `level_loop` (5 s, nền trong `Hub.start`; chỉ áp khi tiến trình đã `setup_logging` → test in-process không đổi
  level toàn cục). Chỉ đổi được `cfharness[.x]` + `logs.LIB_LOGGERS` (không httpx/urllib3: DEBUG của chúng in URL có token bot).
  RPC `logs.levels|level` (HUMAN_ONLY, `logs.level` tự audit), CLI `cfharness logs level [<logger> <level|clear>] [--for 30m]`.
- Lỗi không ai bắt: `install_crash_handlers` (sys/threading excepthook, `faulthandler` → `logs/faults.log`, SIGUSR1 = dump stack —
  `cfharness daemon dump-stacks`), `loop.set_exception_handler` trong `Daemon.serve`, `aiotasks.track/spawn` cho các tác vụ nền "bắn rồi
  quên" (9 chỗ đổi: filehub, lane, chat, slack progress, graph extract/mirror, pipeline, review, claude_cli control). Bộ đếm `logs.ERROR_COUNTS`
  + `bus.listener_errors` hiện ở `cfharness logs level`.
- `EventBus.publish`: listener lỗi bị log + đếm, không làm hỏng publish/listener khác.
- Xoay vòng lúc bật cho `web.log`, `update.out`, `update.log`, `import-job.out`; `purge_old_logs` dọn cả `*.jsonl.N`.
- Classifier: `cfharness logs …` (mọi lệnh con) và `cfharness daemon dump-stacks` = forbidden với agent.
- Test: `tests/test_logging_l1.py`, `tests/test_guard_classifier.py::test_logs_cli_human_only`.

Lệch / để sau:
- Đổi `print(` trong module không phải CLI sang `log.*` → gộp vào L8 (cùng đợt dọn nuốt lỗi).
- stderr của tiến trình sync filehub (đang giữ 300 ký tự cuối) → để L7 (gói chẩn đoán cần nó), không đổi hành vi supervisor ở L1.
- `update_helper` / import-job vẫn ghi text (chỉ stdlib, không import cfharness): đã xoay vòng; formatter JSON mini để L7.
- 38 chỗ `create_task` còn lại (giữ tham chiếu riêng, có await/huỷ theo vòng đời) đã được `loop.set_exception_handler` bắt khi lỗi
  không ai lấy; đổi sang `aiotasks.spawn` dần trong L8.

### L2 — `req_id` + access log RPC (nhánh `feat/logging-l2`)
Đã làm:
- `trace.py`: `new_req_id()` = `r_` + ULID Crockford 26 ký tự (stdlib, sắp theo thời gian), `valid_req_id` (dài đúng 28, chữ HOA, ký tự đầu ≤ 7),
  contextvar `REQ_CTX` {req_id, parent_req_id, channel, principal}, `bind`/`set_ctx`/`reset`, `child(channel)` + decorator `traced(channel)`,
  `outgoing` (client), `accept` (daemon), `TraceFilter` (cài trong `setup_logging` cạnh `RunContextFilter`).
- Client: `rpc.with_auth` gắn `params._trace` → `RpcClient`, `AsyncHubClient` (web/TUI) và `DaemonCaller` của MCP (một req_id cho cả lần thử lại sau
  khi tự khởi động daemon). `RpcError.req_id` (từ `_meta.req_id`, không có thì mã đã gửi); `client.last_req_id`. CLI in `(mã yêu cầu r_…)`, `--json` thêm `error.req_id`.
- Daemon `_dispatch`: pop `_trace` (handler không bao giờ thấy), nhận nếu đúng định dạng, không thì sinh mới; `call_as` trong một lượt đang bind
  dùng lại mã đó; bind cho cả lượt; mọi phản hồi có thêm thành viên cấp cao `"_meta": {"req_id": …}` (client cũ bỏ qua). Thân cũ tách thành
  `_dispatch_inner` (không đổi logic quyền).
- B3: logger `cfharness.rpc`, một bản ghi mỗi lượt `{event, method, req_id, via, channel, duration_ms, outcome ok|error|denied, code}` (`logs.EXTRA_FIELDS`
  vào JSON). Đọc thành công = DEBUG, method ∈ `all_mutating()` = INFO, lỗi nghiệp vụ/tham số = INFO, `BLOCKED` = WARNING `denied`, lỗi bất ngờ giữ
  `log.exception` ERROR (+ bản ghi access INFO). `logs.slow_rpc_ms` (mặc định 2000, đọc lại ≤ 5 s một lần) → WARNING `rpc.slow`, trừ `daemon.SLOW_EXEMPT`
  (`run.wait`, `mcp.events`, `mcp.task_events`, `guard.check` — chờ lâu theo thiết kế). `events.subscribe`: chỉ `rpc.stream_open`/`rpc.stream_close`
  (thời lượng, lý do; `slow_consumer` = WARNING). Kiểm `isEnabledFor` trước khi dựng bản ghi.
- Lan truyền: migration **v28** = `runs.req_id` (+ index), `audit_log.req_id` (+ index), `events.req_id` (không index). `RunManager.submit` lưu req_id
  đang bind (không có → mã riêng, để MỌI run từ v28 có req_id); `_run` bind req_id của run → event adapter/finalize/log/audit trong task của run cùng mã;
  env `CFHARNESS_REQ_ID` cho tiến trình `claude` (env_extra của run, không đụng `env_strip`); hook guard + `hub-guard` (`guard/hook.with_trace`, chỉ
  chuỗi ngắn ký tự an toàn, vẫn fail-closed) gửi kèm `guard.check` → event `guard.decision` + audit `guard.*` cùng mã. `HubEvent.req_id` tuỳ chọn
  (`Hub.publish` điền), schema `docs/schema/event.v1.json` sinh lại. `AuditLog.record` ghi req_id từ contextvar.
- Điểm vào nội bộ (`@traced`): Telegram `handle_update`, Slack `handle_envelope`, `ManagerService._tick`, `ScheduleService._fire`,
  `TaskDispatcher._submit_claimed`, `AutoResumer.fire` — mã mới, cha = mã đang bind.
- OTel: trace_id của span run/tool = băm `req:<req_id>` của run (không có → `run:<run_id>` như cũ); thuộc tính `cfharness.req_id`; allowlist
  `otel.RUN_SPAN_KEYS` lọc thuộc tính span run.
- Test: `tests/test_trace_id.py` (daemon thật + socket thật + hook guard là tiến trình con thật: một lệnh web → cùng req_id ở `_meta`, audit, run, mọi
  event của run, `guard.decision`, dòng `hub.log`; `_trace` sai bị thay; lỗi mang req_id; CLI in mã; level access log; `rpc.slow`; điểm vào nội bộ),
  `tests/test_otel.py::test_req_id_trace_and_allowlist`.

Lệch / để sau:
- v28 chỉ có các cột req_id (theo chốt khi giao L2); cột audit A1 (`outcome`, `principal`, `duration_ms`…) → L3 ở **v29**. Thêm `events.req_id` (không có
  trong §3 ban đầu) vì event lưu theo cột — thiếu thì mất req_id khi đọc lại từ DB; không index (bảng ghi nóng, tra qua `run_id`), L6 thêm nếu cần.
- `guard_decisions` chưa có cột req_id: đối chiếu qua event `guard.decision` (+ dòng audit khi không `safe`). L3/L6 quyết.
- `principal` trong contextvar để trống (L3 — danh tính do daemon chứng thực). `channel` = `_audit.channel` (người tự khai cho cli/tui/web; daemon ép
  cho agent/manager/chat) hoặc loại người gọi.
- req_id do client gửi được nhận kể cả từ agent (chỉ là mã đối chiếu, không có quyền gì) — agent có thể "mượn" mã của lệnh khác để làm rối trace,
  không làm được gì hơn. Không có span OTel cho RPC. Hook quan sát (`observe_hook`) chưa gửi req_id (spool riêng, L6).
