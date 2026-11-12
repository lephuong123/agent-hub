# Xem / mở file agent đã thao tác (web cục bộ + web public)

Yêu cầu chủ repo: "file mà agent thao tác — dù tôi dùng bản local hay cloud — tôi cần bấm mở được; nếu local thì phải mở chính file đó."
Test: `tests/test_web_files.py` (17 test, mở app luôn bị thay bằng hàm giả — không bao giờ chạy ứng dụng thật trong test).

## 1. Quyết định
| Ngữ cảnh web | "Xem" (trình xem chỉ đọc trong trình duyệt) | "Mở" (app mặc định của OS) / "Thư mục" (Explorer/Finder) |
|---|---|---|
| **Cục bộ** — web mở trên chính máy chạy hub (`cfharness.localhost:<cổng>`) | có | có |
| **Cloud** — qua tunnel TLS `web.public_origin` (web-public.md) | có | KHÔNG (sẽ mở trên máy chủ, không ai thấy) → 403 |

"Cục bộ" do SERVER quyết định (`web/file_pages.is_local_request`): peer là loopback **và** Host là host cục bộ của web (không phải
public origin) **và** không có header do proxy/tunnel thêm (`X-Forwarded-For`, `Forwarded`, `CF-Connecting-IP`, `X-Real-IP`, `CF-Ray`,
`True-Client-IP`…). Header client tự gửi chỉ có thể làm request bị coi là KHÔNG cục bộ (hạ quyền), không bao giờ ngược lại — tunnel luôn
thêm header proxy mà client từ xa không xoá được. `static/files.js` chỉ ẩn/hiện nút theo tên host (gợi ý giao diện), server vẫn tự kiểm.

## 2. Ai đọc file — daemon, không phải web
Web là client thuần của daemon (web.md §1) → thêm 2 RPC trong `src/cfharness/fileview.py` (đăng ký ở `daemon.py`):
- `file.read {run_id|agent_id, path, max_bytes?, whole?}` — chỉ đọc, **HUMAN_ONLY**: agent gọi qua socket sẽ đọc được workspace của agent
  KHÁC / ngoài sandbox của chính nó (sandbox chỉ bao Bash/tool của run, daemon thì không) — nên không cho agent/điều phối viên.
- `file.open {run_id|agent_id, path, reveal?}` — không đổi dữ liệu hub nhưng là TÁC DỤNG PHỤ (chạy tiến trình ngoài sandbox trên máy) →
  **HUMAN_ONLY** + `MUTATING_FILES` (đưa vào `auth.all_mutating()` để test phân loại quyền bắt buộc phải xếp loại).
- Cả hai tự ghi audit MỌI lời gọi, kể cả bị từ chối (dò đường dẫn là tín hiệu đáng xem): kind `file.read`/`file.open`, data
  `{path, resolved, size, kind, ok, error}`, kênh theo `_audit` (web → `channel=web, actor=user`). Nội dung file KHÔNG bao giờ vào audit.
  Không giới hạn tốc độ. I/O chạy `asyncio.to_thread` (không chặn event loop của daemon).

Daemon cũ (chưa có `file.*`) → web báo "daemon đang chạy bản cũ hơn web — khởi động lại daemon".

## 3. Luật bảo mật (`fileview.resolve_in_roots`)
1. **Gốc do daemon tự suy ra, không bao giờ nhận từ client.** Run: `runs.cwd` (thư mục làm việc thật lúc spawn) + `diff.root` (gốc repo của
   checkpoint); run chưa spawn → theo agent. Agent: `workspace_default` + `<home agent>/workspaces/` (scratch, hội thoại điều phối viên).
   Gốc không tồn tại hoặc là gốc ổ đĩa/`/` thì bỏ.
2. Chuỗi đường dẫn (`check_raw_path`): ≤ 4096 ký tự, không NUL/ký tự điều khiển, không UNC (`\\server`, `//server`), không `\\?\`/`\\.\`,
   không `:` sau ký tự ổ đĩa (luồng dữ liệu phụ NTFS), không thành phần là tên thiết bị Windows (CON, NUL, COM1…).
3. Tương đối → ghép với từng gốc; tuyệt đối → giữ nguyên. Rồi `realpath` (đi hết symlink/junction) phải nằm TRONG một gốc
   (so sánh `normcase`, có ranh giới thư mục) → chặn `..`, đường dẫn tuyệt đối ngoài gốc, symlink/junction trỏ ra ngoài.
4. Luôn chặn: nội bộ `HUB_HOME` (trừ `agents/*/workspaces/**` và `worktrees/**`), token người `<socket>.auth` + socket, và tên file bí mật
   (`.env`, `.env.*` trừ `.example/.sample/.template/.dist/.default(s)`, `id_rsa`/`id_ed25519`… (không chặn `.pub`), `*.pem|key|p12|pfx|jks|kdbx|ppk`,
   `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.pgpass`, `credentials(.json)`, `*secret(s).{json,yaml,toml,env}`).
   Đây là lớp chống lộ bí mật qua link public — muốn xem thì mở trực tiếp trên máy.
5. Đọc: `O_NOFOLLOW` cho thành phần cuối, `fstat` phải là file thường (không FIFO/thiết bị). Còn khe TOCTOU nhỏ khi agent đổi thư mục cha
   thành symlink giữa lúc kiểm và lúc mở — chấp nhận (cần agent đang chạy + đúng thời điểm; kết quả vẫn chỉ là đọc, có audit).
6. **Mở bằng app: DANH SÁCH CHO PHÉP** (`OPEN_EXTS`) thay vì danh sách cấm — an toàn hơn vì đuôi lạ có thể gắn với trình thông dịch
   (Windows mở `.js/.vbs/.wsf` = CHẠY bằng WSH, `.py` = chạy bằng py launcher). Cho phép: văn bản thuần (.txt .md .log .csv .json .yaml
   .toml .ini .xml .diff …), PDF, ảnh raster, Office không macro (.docx .xlsx .pptx .odt …), âm thanh/video, .zip. KHÔNG: mã nguồn/script
   (.py .js .sh .ps1 .bat .cmd …), chương trình/lối tắt (.exe .com .msi .lnk .scr .hta .reg .app .jar …), `.html/.svg` (trình duyệt chạy JS
   từ file://), Office có/có thể có macro (.docm .xlsm, .doc .xls .ppt cũ). Loại bị chặn vẫn "Xem" được và "Mở thư mục" được.
7. Lệnh OS (`fileview.open_command`): Windows `os.startfile(path, "open")` / reveal `explorer.exe /select,<path>`; macOS `open <path>` /
   `open -R`; Linux `xdg-open <path>` / `xdg-open <thư mục cha>`. Không qua shell, `stdin/stdout` = DEVNULL, tiến trình tách nhóm.
   Daemon chạy dạng LaunchDaemon không có phiên đồ hoạ → mở thất bại, báo lỗi (không ảnh hưởng gì khác).

## 4. Hợp đồng HTTP (`web/file_pages.py`)
| Route | Ghi chú |
|---|---|
| `GET /files/view?run=<id>\|agent=<id>&path=<p>` | Trang xem: văn bản/mã escape (autoescape, không markdown) + số dòng (CSS counter), tối đa 1 MB (`VIEW_MAX`, quá thì banner "chỉ hiển thị…"); ảnh `<img src=/files/raw…>`; PDF: link mở/tải; nhị phân: chỉ tải về. Nút "Mở bằng ứng dụng"/"Mở thư mục" chỉ khi cục bộ. Chưa đăng nhập → 303 /login. Bị chặn → 400, không có → 404 |
| `GET /files/raw?…[&dl=1]` | Byte gốc toàn bộ file ≤ 10 MB (`RAW_MAX`, base64 qua RPC; lớn hơn → lỗi, không trả file cắt). Inline CHỈ: ảnh raster đúng content-type, PDF, SVG (kèm CSP `sandbox` — không chạy script). Mọi loại khác: `application/octet-stream` + `Content-Disposition: attachment` + CSP sandbox. Luôn `X-Content-Type-Options: nosniff`, `no-store`. CSP chung của trang không đổi |
| `POST /files/open` (`run|agent`, `path`, `reveal`, `csrf`, `k`) | `post_guard`: CSRF + Origin/Referer/Sec-Fetch-Site + khoá phiên WT-1. Không cục bộ → 403. `Accept: application/json` → `{ok, message}` (files.js hiện toast); không thì 303 + flash |

Giao diện: macro `file_chip(f)` (`_macros.html`) — "Xem" (tab mới) + "Mở" + "Thư mục" (ẩn tới khi `files.js` gắn `html.file-local`), tooltip
giải thích cục bộ/cloud. Có ở: dòng tool Read/Edit/Write/MultiEdit/NotebookEdit trong diễn biến run (`present.file_ref` lấy
`input.file_path|notebook_path` của `tool.start`; phát lại từ DB `replay_items` và SSE `_run_converter` đều giữ chip khi `tool.complete`
thay mục tại chỗ — `tool.complete` không mang input), và khối "Thay đổi file" (`_run_diff.html`, trừ file đã xoá). `static/files.js`
ủy quyền sự kiện (chạy cả với mục đổ vào qua SSE), không inline script (CSP), thông báo gán bằng `textContent`. CSS riêng `static/files.css`.

## 5. Kiểm chứng
- `uv run pytest -q` xanh (trừ `tests/test_admin_overview.py` — import `grp`, chỉ chạy trên POSIX, lỗi sẵn có trên Windows, không liên quan).
- Chạy thật trên Windows 11: demo web (`web.demo` + script bọc thêm một run có Read/Edit/Write và diff, `CFHARNESS_HOME` tạm) + Chromium
  headless: 4 chip trên dòng tool, 2 chip ở "Thay đổi file" (file đã xoá không có chip), nút Mở/Thư mục hiện trên `cfharness.localhost`;
  bấm "Mở" với `.env` → toast "Bị chặn: file có thể chứa bí mật"; trình xem mã hiện 6 dòng có số, `<script>` hiện dạng chữ; ảnh PNG tải
  được; `../../secret.txt` → trang lỗi "nằm ngoài thư mục làm việc"; 390 px không tràn ngang; console chỉ có 2 lỗi 400 đúng như mong đợi.

## 6. Chưa làm / câu hỏi mở
- Chat Điều phối: tệp người đính kèm được hub đổi tên (`attachments/doc-<thời điểm>-…`) và tên thật không trả về web; chữ của điều phối viên
  có thể nhắc đường dẫn nhưng thường là workspace của agent KHÁC (ngoài gốc của run điều phối viên). Chưa tự gắn chip vào chat — xem file
  qua link "xem lượt chạy" (dòng Read của run điều phối viên có chip). RPC đã nhận `agent_id` để làm tiếp phần này.
- Trang task: chưa hiện đường dẫn file nào nên chưa có chip.
- Danh sách `OPEN_EXTS`/`SECRET_NAMES` là lựa chọn mặc định cứng — nếu cần chỉnh theo người dùng thì thêm khoá cấu hình (chỉ người đặt).
- Web public: có nên cho TẢI VỀ file nhị phân lớn qua tunnel không, hay chỉ xem? Hiện cho tải ≤ 10 MB (đã audit).
