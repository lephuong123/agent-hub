# Plan: thư mục dùng chung người ↔ agent trên "Không gian số" (thu-muc-ai)

Ngày: 2026-11-15 (bản 4). Trạng thái: **đã chốt, đang triển khai** (§8 = hợp đồng giữa 3 repo).
Liên quan: `file-open.md`, `web-public.md`, `confine.md`, `M3.md` (checkpoint/diff), `approval-profiles.md` (`extra_write_roots`).
Repo bên kia:
- `~/Workspace/quan-ly-thu-muc-ai`: server "Không gian số", tên kỹ thuật `qltm`. Bản prod `https://thumucai.trungtamdhs.com`, chạy cùng máy Mac này.
- `~/Workspace/tang-thu-sync`: client đồng bộ 2 chiều, lõi Rust `sync-core`, có sẵn binary `tang-thu-sync-headless`.

## 0. Quyết định của chủ repo

| # | Nội dung | Chốt |
|---|---|---|
| 1 | Ghi file qua web **cfharness** từ cloud | Không. Web cfharness chỉ xem |
| 2 | Repo thật `dir:` | Chỉ sửa tại máy |
| 3 | Người và agent đụng cùng file | Chỉ cảnh báo, không chặn |
| 4 | Step-up TOTP | Không |
| 5 | Push nhánh / PR GitHub | Không |
| 6 | Hub file | **Không gian số**, được sửa code bên đó |
| 7 | (bản 3) Cấu trúc thư mục | **Không chia theo agent.** Hub tạo một thư mục RỖNG cho người dùng; người tự thiết kế cấu trúc. Người **thêm/sửa/xoá** được trên Không gian số, agent cũng đọc/ghi được, không chỉ agent mới sửa |

Hệ quả: đây không còn là "xuất bản output agent" một chiều mà là **một thư mục làm việc chung, đồng bộ 2 chiều**:
- người dùng làm việc trên Không gian số (web hoặc điện thoại, từ bất cứ đâu);
- agent làm việc trên bản sao cục bộ của chính thư mục đó trên máy Mac, dùng tool bình thường: Read, Edit, Bash, Office.

## 1. Mô hình

```
          Không gian số (cloud, qua tunnel)                          Máy Mac chạy cfharness
  ┌──────────────────────────────────────┐                ┌───────────────────────────────────────────┐
  │ Thư mục "<tên người đặt>"  (Owner = người)  ◀── sync 2 chiều ──▶  ~/KhongGianSo/<tên>/   (bản sao cục bộ) │
  │  ├ cấu trúc người tự tạo              │   tang-thu-sync-headless │   ▲ agent đọc/ghi bằng tool thường      │
  │  ├ phiên bản, thùng rác, preview      │   (daemon cfharness      │   │ workspace "filehub:<tên>"          │
  │  └ tìm toàn văn, chia sẻ, audit       │    giám sát tiến trình)  │   └ guard + sandbox + checkpoint/diff   │
  └──────────────────────────────────────┘                └───────────────────────────────────────────┘
```

- **Người là Owner** thư mục, được toàn quyền như mọi thư mục khác trong Không gian số. Không cần tài khoản dịch vụ, cũng không cần khoá
  "chỉ xem" (`managed_by`). Hai mục này của bản 2 **bỏ**.
- **cfharness đồng bộ với tư cách người dùng**, qua một *thiết bị* tên "cfharness · <tên máy>". Theo D10 của tang-thu-sync, mỗi người tối đa
  5 thiết bị. Mọi thay đổi do agent gây ra hiện trên Không gian số là "<người> · thiết bị cfharness", và audit bên đó vẫn lần ra được.
- Mỗi thay đổi thành một **phiên bản**. Xoá vào **thùng rác**, cả từ máy lẫn từ web, nên agent làm hỏng thì người khôi phục được trên web.
- Xung đột (người sửa trên web cùng lúc agent sửa ở máy): sync-core tạo **bản xung đột** (conflict copy), không mất dữ liệu. Tệp đang khoá
  (423) thì chờ, quá 30 phút mới tạo bản xung đột (D5).

## 2. Vì sao dùng `tang-thu-sync` thay vì tự viết sync trong cfharness

- Giao thức đồng bộ `/api/sync/*` đã có sẵn phía server: journal, change feed long-poll, `blobs/check`, `files/commit` với `baseRev`,
  Idempotency-Key, bản xung đột.
- Lõi Rust đã chạy mô phỏng 10.000 seed và test hỗn loạn (giết tiến trình giữa lúc tải lên, tải về, đổi tên).
- Tự viết lại bằng Python là làm lại phần khó nhất và dễ sai nhất.
- `tang-thu-sync-headless` đã chạy được không giao diện (`crates/sync-core/src/bin/tang-thu-sync-headless.rs`). File đó tự ghi "không phải sản
  phẩm" vì token để trong tệp và không có đăng nhập. Cần nâng cấp thêm mấy việc ở §4.2.

Đã loại: "Kết nối nguồn" (kéo một chiều, prod chặn `file://`), MCP `thu-muc-ai` (≤ 512 KB, không hợp để agent làm việc với file Office, ảnh).
MCP vẫn dùng như hiện nay khi cần tra cứu.

## 3. Phía cfharness — module `filehub/`

### 3.1 Thành phần
| File | Vai trò |
|---|---|
| `filehub/supervisor.py` | Chạy `tang-thu-sync-headless` làm tiến trình con của daemon:<br>- khởi động ở `hub.started`, dừng êm khi daemon dừng;<br>- restart có backoff;<br>- đối soát `(pid, start_time)` khi daemon khởi động lại, giống `recovery.py`;<br>- đọc file `--status` (JSON, ghi nguyên tử) để lấy trạng thái;<br>- env tối thiểu: không có token người của hub, không có `CLAUDE_*` |
| `filehub/service.py` | `hub.filehub`:<br>- danh sách thư mục chung (v1: 1, cấu trúc cho sẵn N);<br>- trạng thái đồng bộ: `idle\|syncing\|conflict\|offline\|auth_expired`;<br>- tra ánh xạ đường dẫn cục bộ ↔ `file_id/folder_id` (đọc base SQLite của sync-core, chế độ chỉ đọc) để dựng link;<br>- theo dõi thay đổi từ cloud trong lúc agent đang chạy (§3.4) |
| `filehub/rpc.py`, `cli.py` | RPC `filehub.status\|list\|link` (đọc); `filehub.setup\|login\|add\|remove\|pause\|resume\|resync` (ghi, HUMAN_ONLY, tự audit).<br>CLI `cfharness filehub setup\|login\|status\|add\|remove\|pause\|resume` |
| `web/filehub_pages.py` | Thẻ "Không gian số" ở Cài đặt: kết nối, thư mục chung, trạng thái, bản xung đột, nút tạm dừng.<br>Link "Mở trên Không gian số" cạnh `file_chip` |

Không có migration: cấu hình ở `config.yaml` (`filehub.*`), trạng thái ở `hub_state` và file status của sync, ánh xạ đọc từ base của sync-core.

### 3.2 Tạo thư mục chung (luồng `cfharness filehub setup`)
1. `cfharness filehub login`: mở trình duyệt tới trang đồng ý thiết bị của Không gian số (OAuth loopback `127.0.0.1:<cổng>/cb`, PKCE, scope
   `sync`, xem `syn/device-auth.ts`). Token ghi vào `HUB_HOME/secrets/filehub/tokens.json` (0600). Sandbox đã deny đọc `secrets/`, nên agent
   không đọc được.
2. Người chọn workspace bên Không gian số và tên thư mục, mặc định "cfharness". **Hub tạo thư mục RỖNG** dưới thư mục người chọn (người là
   Owner), rồi thêm làm *sync root*.
   - Thư mục đã tồn tại → hỏi "dùng thư mục có sẵn?".
   - Thư mục có nội dung → đồng bộ xuống, và cảnh báo dung lượng.
3. Bản sao cục bộ: mặc định `~/KhongGianSo/<tên>/`. Phải nằm trong `workspace.confine` W nếu bật giam, và không được nằm trong HUB_HOME.
4. Hub đăng ký workspace mới loại **`filehub:<tên>`** để giao cho agent/team/task (§3.3).

Người tạo thư mục con, đổi tên, sắp xếp, xoá **trên Không gian số** như mọi thư mục khác. cfharness không đặt ra cấu trúc nào.

### 3.3 Agent dùng thư mục chung
- **Kiểu workspace `filehub:<tên>`** (cạnh `dir:`, `worktree`, `scratch`):
  - cwd = bản sao cục bộ (hoặc thư mục con: `filehub:<tên>/<đường dẫn>`);
  - khoá theo gốc (`WorkspaceLocks`, như `dir:`): một run ghi mỗi lúc, run chỉ đọc chạy song song.
- **Dùng kèm workspace khác:** agent làm trong repo/scratch nhưng cần đọc/ghi thư mục chung → `extra_write_roots` (đã có cho `dir|worktree`,
  `tasks.py:156`), mở rộng cho cả scratch. Mặc định thêm **quyền đọc** thư mục chung cho mọi run (`allowRead`), ghi chỉ khi được giao.
- **Checkpoint/diff:** thư mục không phải git dùng `_walk` (`workspace.py:285`), nên khối "Thay đổi file" của run vẫn có. `run revert` cho
  thư mục này **không bật**: hoàn tác là việc của phiên bản/thùng rác trên Không gian số, nơi người thấy được.
- **Guard** (thêm luật vào `classifier`, ca trong `tests/test_guard_classifier.py`):
  - Coi thư mục chung là **dữ liệu của người**: xoá / di chuyển ra ngoài / `rm -r` / ghi đè hàng loạt → `destructive` (hỏi người).
    Thêm/sửa file thường → `write`.
  - Ngưỡng an toàn: một run xoá > `filehub.mass_delete_max` (mặc định 20) file → tạm dừng đồng bộ chiều đẩy lên và hỏi người trước khi
    đẩy (§3.5).
  - Không cho ghi `.tangthu*` (base/metadata của sync nếu nằm trong thư mục), cũng không cho ghi thư mục profile của sync.
- **Điều phối viên:** đọc thư mục chung (`filehub.list|link`) và giao việc với workspace `filehub:`. Không được `setup|login|remove` (HUMAN_ONLY,
  không thuộc `MANAGER_ALLOWED`).

### 3.4 Cảnh báo khi người và agent cùng đụng (quyết định 3)
- **Khi agent đang chạy trên thư mục chung mà cloud đẩy xuống thay đổi:** `hub.filehub` thấy tệp đổi trong khi
  `WorkspaceLocks.holder(root)` đang giữ. Nó gọi `steer(by="hub")` với một câu cố định:
  `[cfharness] The human changed <rel> on Không gian số while you were working. Re-Read it before editing.`
  Claude Code cũng tự báo "modified since read" cho Edit/Write. `signals.on_event` phải bỏ qua steer có `by=="hub"` (không tính là correction).
- **Phía người** (sửa bên Không gian số, PR T3): thư mục đang có run cfharness ghi thì hiện dải "cfharness đang làm việc trong thư mục này
  (agent X, từ hh:mm)". cfharness đặt trạng thái này qua route nhỏ khi run bắt đầu/kết thúc. Chỉ cảnh báo, không khoá.
- **Bản xung đột** nếu có: hiện ở thẻ Cài đặt + event `filehub.conflict` → Telegram/Slack.

### 3.5 An toàn dữ liệu
- **Hướng đẩy lên** (agent → cloud) mặc định bật, nhưng có:
  - ngưỡng xoá hàng loạt (§3.3);
  - bộ lọc bỏ qua của sync (`.git/`, `node_modules/`, tệp tạm), cộng thêm tên bí mật của `fileview.secret_name` (`.env`, khoá riêng…), để
    những tệp này không bao giờ lên cloud.
- **Quét nội dung sau run:** tệp mới/sửa trong thư mục chung có chứa GIÁ TRỊ secret đã biết (`redact.py`) → event cảnh báo + Telegram:
  "tệp X có thể chứa bí mật, đã lên Không gian số". v1 chỉ cảnh báo. Muốn chặn trước khi đẩy thì cần hook trong sync-core (để sau).
- **Tệp người tải lên là dữ liệu không tin cậy với agent:** prompt injection trong tài liệu. Đây là rủi ro sẵn có khi agent đọc tài liệu
  ngoài, guard/sandbox vẫn áp như thường. System prompt ghi rõ: nội dung thư mục chung là DỮ LIỆU.
- **Giam:** bản sao cục bộ phải nằm trong W (`confine.validate_workspace`). Token sync chỉ ở `secrets/`, không vào env của run.
- Nhánh `filehub` ∈ `integrity.SECURITY_KEYS`; không có trong `SAFE_CONFIG` của web. Bật/sửa bằng CLI hoặc trang Cài đặt cục bộ
  (`is_local_request`), qua cloud chỉ xem trạng thái (quyết định 1).

### 3.6 Giao diện cfharness
- `file_chip` của tệp nằm trong thư mục chung → thêm "Mở trên Không gian số" (`/files/<folder_id>?file=<file_id>`, deep link sẵn có
  `apps/web/src/pages/Home.tsx:122`). Như vậy từ cloud, người bấm từ run sang thẳng Không gian số để xem/sửa.
- Báo kết quả run qua Telegram/Slack: thêm dòng cố định "N tệp trong <thư mục chung> đã đổi · Mở" (link tới thư mục).
- Trình xem cục bộ `/files/view` giữ nguyên.

## 4. Thay đổi bên ngoài cfharness

### 4.1 Không gian số (`quan-ly-thu-muc-ai`)
| PR | Việc | Ghi chú |
|---|---|---|
| T1 | **Grant thiết bị giới hạn trong một thư mục** (`oauth_grants.root_folder_ids`): token sync của thiết bị cfharness chỉ thấy/ghi các sync root đã chọn lúc đồng ý; trang đồng ý hiện "Thiết bị này chỉ truy cập: <thư mục>". Chặn ở `syn/*` (roots, feed, commit, blobs/check) | Hiện scope `sync` = mọi thư mục người dùng thấy (`syn/device-auth.ts:187`). Agent bị lừa cũng chỉ phá được trong thư mục chung |
| T2 | Route `POST /api/sync/roots/ensure {wsId, parentId, name}` (tạo thư mục rỗng + trả root id, idempotent) cho token sync có giới hạn; hoặc cho phép trong luồng đồng ý: người chọn/tạo thư mục ngay ở trang đồng ý | Để `filehub setup` không cần token PAT riêng |
| T3 | Dải "cfharness đang làm việc ở đây" (§3.4): `PUT /api/sync/roots/:id/presence {agent, since, until}` (chỉ thiết bị có grant root đó), hiện ở `FilesPage`, hết hạn tự động | Chỉ cảnh báo |
| T4 | Cờ thư mục **"Không cho Claude/Trợ lý đọc"**: áp cả phiên Claude project, MCP và Trợ lý (hiện cờ Claude không chặn Trợ lý, `mcp/che-bien.ts:2-3`) | Chỉ làm nếu chủ repo muốn (câu hỏi §7.1) |
| T5 | Kiểm preview HTML/SVG: không chạy script trên origin của Không gian số; thêm test nếu thiếu | File do agent tạo |

### 4.2 tang-thu-sync
| PR | Việc |
|---|---|
| S1 | `tang-thu-sync-headless --login`: luồng OAuth loopback + PKCE (lõi `auth` đã có), ghi `--tokens` 0600; refresh token ghi lại nguyên tử |
| S2 | `--ignore <tệp mẫu>`: thêm mẫu bỏ qua (tên bí mật, `.git`…) ngoài `ignoreDefaults`; `--max-delete-batch N`: vượt thì dừng chiều đẩy lên, ghi `status.blocked = "mass_delete"` chờ lệnh `--resume-push` (file cờ) |
| S3 | `--status` thêm: bản xung đột, hàng đợi, lỗi auth; dừng êm bằng SIGTERM (không bỏ dở commit); bản phát hành binary macOS arm64/x64 trên CI (cfharness tải đúng tag, kiểm sha256) |

## 5. Lộ trình

| Bước | PR | Test chính |
|---|---|---|
| 1 | S1, S2, S3 (tang-thu-sync) | e2e với server worktree (`scripts/e2e-server.sh`), giết tiến trình giữa chừng, xoá hàng loạt bị chặn |
| 2 | T1, T2 (Không gian số) | token thiết bị ngoài root → 403 ở mọi route `syn/*`; ensure idempotent |
| 3 | H1 `filehub/supervisor.py` + `service.py` + RPC/CLI (`setup/login/status`) | `tests/test_filehub_supervisor.py` dùng binary sync GIẢ (`tests/fake_tangthu_sync.py`: đọc args, ghi status, mô phỏng thay đổi từ cloud); `test_sec_auth.py` (agent gọi `filehub.setup` → từ chối); `test_manager_security.py` |
| 4 | H2 workspace `filehub:` + `extra_write_roots` cho scratch + checkpoint `_walk` + tắt revert | `test_filehub_workspace.py`: khoá, chỉ đọc song song, confine ngoài W → lỗi, diff có |
| 5 | H3 guard: xoá/di chuyển = destructive, ngưỡng xoá hàng loạt, cấm ghi metadata sync | ca `DESTRUCTIVE_FILEHUB`/`SAFE_FILEHUB` trong `test_guard_classifier.py` |
| 6 | H4 cảnh báo đồng thời: steer `by=hub`, signal bỏ qua, event `filehub.conflict`; T3 dải presence | `test_filehub_concurrency.py` (ScriptedRuntime giữ run running + binary giả đẩy thay đổi) |
| 7 | H5 web/Telegram/Slack: thẻ Cài đặt, link trên `file_chip`, dòng kết quả run; quét secret sau run | `test_web_filehub.py`, ca Telegram/Slack (link là chữ cố định, không nút) |
| 8 | T4, T5 (nếu chốt) + H6 tài liệu `docs/dev-notes/filehub.md`, AGENTS.md, CHANGELOG | — |

Event mới: `filehub.state`, `filehub.conflict`, `filehub.blocked`, `filehub.secret_warning` → `EVENT_LEVELS` + sinh lại `docs/schema/event.v1.json`.

Ước lượng: tang-thu-sync 2–3 ngày, Không gian số 2–3 ngày, cfharness 6–7 ngày.

**Nghiệm thu thật trên máy này:**
1. `cfharness filehub setup` → trên Không gian số xuất hiện thư mục rỗng "cfharness"; trên máy có `~/KhongGianSo/cfharness/`.
2. Từ điện thoại 4G: tạo thư mục con "Báo cáo", tải lên `yeu-cau.docx` → vài giây sau có ở máy.
3. Giao agent (workspace `filehub:cfharness/Báo cáo`) "đọc yêu cầu, viết báo cáo .docx" → trên điện thoại thấy tệp mới; dải "cfharness đang làm
   việc" hiện trong lúc chạy.
4. Người sửa báo cáo trên web trong khi agent còn chạy → agent nhận steer; nếu đụng cùng lúc thì có bản xung đột, không mất bản nào.
5. Agent `rm -r` thư mục con → hỏi duyệt; duyệt xong thì tệp nằm trong thùng rác Không gian số, khôi phục được.
6. Token thiết bị cfharness gọi `syn/roots` → chỉ thấy thư mục "cfharness".

## 6. Rủi ro
- **Cùng một máy:** máy Mac tắt thì cả hai cùng tắt. Lúc đó người vẫn không xem được, vì Không gian số cũng chạy trên máy này.
- **Tiến trình sync phụ thuộc binary Rust:** cần bản phát hành ổn định; daemon kiểm phiên bản (`filehub.sync_min_version`), sai thì không chạy
  và báo ở thẻ Cài đặt.
- **Thiết bị thứ 6:** D10 giới hạn 5 thiết bị/người, nên cfharness chiếm một suất.
- **Dung lượng:** tệp lớn ăn hạn mức workspace (`storage_full`); trạng thái hiện ở Cài đặt.
- **Agent sửa nhầm dữ liệu người:** giảm bằng guard (xoá = hỏi), ngưỡng xoá hàng loạt, phiên bản + thùng rác.

## 7. Đã chốt (2026-11-15)
- §7.1 cho Claude/Trợ lý Không gian số đọc → **không làm T4**. §7.2 một thư mục (code sẵn cho N). §7.4 mọi run được ĐỌC thư mục chung.
- §7.3 **Xoá KHÔNG hỏi** — guard coi xoá/di chuyển trong thư mục chung là `write` (không approval); sau run hub BÁO người
  (event `filehub.deleted{run_id, count, paths≤20}` → Telegram/Slack + thẻ Cài đặt), khôi phục ở thùng rác Không gian số.
  Ngưỡng `--max-delete-batch` của sync mặc định TẮT (0) — chỉ là công tắc an toàn tuỳ chọn. Thay cho §3.3 "destructive (hỏi người)".
- T2 gộp vào T1 (chọn/tạo thư mục rỗng ngay ở trang đồng ý thiết bị). T3 presence đi qua binary sync (cfharness không cầm token).

### Câu hỏi gốc (lưu lại)
1. Thư mục chung có cho **Claude/Trợ lý bên Không gian số** đọc không? Nếu không → làm T4. Đề xuất: cho đọc (vì là dữ liệu của chính
   anh); tắt được từng thư mục con khi cần.
2. **Một hay nhiều** thư mục chung? Đề xuất v1: một thư mục (người tự chia thư mục con bên trong), cấu trúc code sẵn cho nhiều.
3. Agent xoá tệp trong thư mục chung **luôn hỏi** (đề xuất), hay cho xoá tự do vì đã có thùng rác?
4. Mặc định **mọi run được đọc** thư mục chung (đề xuất), hay chỉ run được giao workspace `filehub:`?

---
### Phụ lục — đã loại qua các vòng
- Bản 1: tự xây file manager + editor CodeMirror trong web cfharness. Không cần, vì Không gian số đã có, và web cfharness không ghi qua cloud.
- Bản 2: đẩy một chiều, chia theo agent, tài khoản dịch vụ + khoá chỉ xem. Thay bằng thư mục chung 2 chiều do người quản lý (quyết định 7).
- code-server / File Browser / WebDAV / Syncthing / git push PR: toàn quyền shell hoặc ngoài phạm vi đã chốt.

## 8. Hợp đồng giữa 3 repo (cố định để làm song song)

### 8.1 Không gian số — đồng ý thiết bị giới hạn một thư mục (T1)
- `GET /api/sync/oauth/authorize` (luồng thiết bị hiện có, `syn/device-auth.ts`) nhận thêm `root_mode=single` + `root_hint=<tên>` (tuỳ).
  Trang đồng ý (`SyncConsentPage`) khi `root_mode=single`: người chọn workspace + thư mục cha (mặc định gốc workspace) + tên (điền sẵn
  `root_hint`) → nếu chưa có thì TẠO thư mục rỗng (người là Owner) → grant ghi `root_folder_ids=[id]` (migration mới).
- Token response (đổi code + refresh) thêm `roots: [{id, wsId, name}]` khi grant có giới hạn.
- Mọi route `syn/*` (roots, feed/changes, items, commit, blobs/check, uploads kèm sync, ws) với token có giới hạn: chỉ trong cây con của
  `root_folder_ids`; ngoài → 403 `root_not_granted` (roots list chỉ trả các root đó).
- Trang Hồ sơ › Thiết bị hiện "Chỉ truy cập: <thư mục>".

### 8.2 Không gian số — presence (T3)
- `PUT /api/sync/roots/:id/presence {label ≤80 ký tự, ttlSec 30..3600}` / `DELETE …/presence` — chỉ token thiết bị có root đó; lưu RAM/DB
  có hạn; `GET /api/folders/:id` (hoặc route web tương đương) trả `presence: [{label, device, until}]` cho thư mục và mọi thư mục con của
  root; `FilesPage` hiện dải "⚙ <label> — đang làm việc trong thư mục này (từ hh:mm)". Chỉ hiển thị, không khoá.
- T5: kiểm preview HTML/SVG không chạy script trên origin chính (thêm test nếu thiếu).

### 8.3 tang-thu-sync — `tang-thu-sync-headless`
```
tang-thu-sync-headless --version                         → in "tang-thu-sync-headless <semver>" (exit 0)
tang-thu-sync-headless login --server URL --tokens FILE --device NAME [--root-hint TÊN] [--no-browser]
    → in một dòng "LOGIN_URL <url>" (stdout, flush) rồi mở trình duyệt (trừ --no-browser); callback loopback 127.0.0.1:<cổng>/cb, PKCE,
      root_mode=single; ghi FILE (0600, nguyên tử); in "ROOT <id>	<wsId>	<name>" cho mỗi root; exit 0 (lỗi: exit ≠0 + dòng "ERROR <mã>")
tang-thu-sync-headless --profile DIR --server URL --tokens FILE --status FILE [--device ..] [--user ..] [--poll s]
    [--add-root <id> --root-name <tên> --ws <id> --local-parent DIR]
    [--extra-ignore FILE]          # mỗi dòng 1 glob kiểu .gitignore, cộng vào ignoreDefaults cho chiều lên
    [--max-delete-batch N]         # 0 = tắt (mặc định); vượt → dừng chiều lên của root, status.blocked="mass_delete"
    [--control FILE]               # cfharness ghi JSON; headless đọc lại khi mtime đổi (≤ 2 s):
                                   #   {"presence": {"<rootId>": {"label": "...", "ttl_s": 600} | null}, "resume_push": ["<rootId>"]}
    SIGTERM → dừng êm (không bỏ dở commit), exit 0.
```
- `--status` (ghi nguyên tử mỗi khi đổi, ≤ 1 lần/giây):
  `{"version": "...", "pid": n, "state": "idle|syncing|offline|auth_expired|error", "updated_at": "<ISO>",
    "roots": [{"id", "name", "ws_id", "local_path", "state", "pending_up", "pending_down", "blocked": null|"mass_delete",
               "conflicts": [{"path", "created_at"}], "last_error": null|"<mã>"}],
    "recent": [{"ts", "root_id", "dir": "up|down", "op": "create|update|delete|move", "path", "file_id", "folder_id"}]}`  (≤ 200 mục)
- Ánh xạ đường dẫn → id cho link: tệp `<profile>/index/<rootId>.json` (ghi nguyên tử, cập nhật khi đổi):
  `{"root_folder_id": "...", "items": {"<rel path>": {"file_id"|"folder_id": "...", "parent_folder_id": "..."}}}`.

### 8.4 cfharness
- Binary: `filehub.sync_binary` (đường dẫn), kiểm `--version` ≥ `filehub.sync_min_version`.
- Thư mục: profile `HUB_HOME/filehub/profile/`, status `HUB_HOME/filehub/status.json`, control `HUB_HOME/filehub/control.json`,
  token `HUB_HOME/secrets/filehub/tokens.json` (0600), extra-ignore `HUB_HOME/filehub/ignore.txt` (sinh từ `fileview.secret_name` + `.git/` …).
  `HUB_HOME/filehub/` → sandbox denyRead/denyWrite (như `secrets/`), backup: `EXCLUDED` cho profile, giữ cấu hình.
- Test dùng binary GIẢ `tests/fake_tangthu_sync.py` theo đúng §8.3.
