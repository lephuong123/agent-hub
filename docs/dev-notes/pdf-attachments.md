# PDF đính kèm cho điều phối viên

Người gửi PDF cho điều phối viên qua web, Telegram hoặc Slack. Cả ba kênh đi qua `manager.chat` → `ManagerService._save_files_async`
→ `write_attachment_files` (`manager/service.py`), nên mọi kênh có cùng hành vi.

Telegram và Slack nhận update TUẦN TỰ (vòng `getUpdates`; một `_worker` Slack). Tin có tệp (và tin gửi điều phối viên của cùng chat
đến sau nó, khi tin trước chưa xong) được xử lý trong task riêng của chat (`ChatBot._chain_chat`, `chat/base.py`): tin của MỘT chat
vẫn đúng thứ tự, còn vòng nhận không phải chờ tải + trích PDF (tới `attachments.pdf.timeout_s`). Chat khác, nút duyệt, E-stop, lệnh
`/` Telegram (trừ `/moi`, `/new`: đổi cuộc trò chuyện nên xếp hàng sau tin có tệp), slash command Slack được xử lý ngay; hàng đợi
Slack không dồn (khi mất kết nối, `inbox.join()` chỉ chờ 10 s).

TUI và REPL (`cfharness chat`, `top` → `m`) KHÔNG nằm trong luồng này: kéo–thả PDF chỉ để lại đường dẫn trong chữ của tin
(`manager/attachments.py` chỉ tách ảnh), nên không có bản chữ, không tách phần, không có ghi chú trong `files_note`. Agent chỉ có khối
system prompt "đọc PDF chỉ với `file_path`" (mục 4).

## 1. Vì sao cần

Kết quả kiểm với claude 2.1.329 (tool Read, khung quyền strict chỉ có Read):

| Cách gọi Read | Kết quả |
|---|---|
| Chỉ `file_path` (PDF 2 trang, 30 trang) | Đọc được: CLI gửi PDF thành khối document gốc, không cần poppler |
| Có `pages` | Read gọi `pdftoppm` (poppler) để vẽ trang. Máy không có poppler thì báo `pdftoppm is not installed. Install poppler-utils …` |

Haiku tự truyền `pages`, kể cả với PDF 2 trang. Khung strict không có Bash nên agent không tự xoay được. Git Bash có `pdftotext` nhưng
không có `pdftoppm`; daemon không dựa vào binary của Git Bash.

Lệnh tái hiện (trong thư mục có PDF):
`claude -p "<prompt>" --tools Read --permission-mode bypassPermissions --model haiku --output-format stream-json --verbose`.

## 2. Luồng

1. `decode_files` nhận PDF theo nội dung (`%PDF-`), trần cứng 10 MB (`FILE_MAX_BYTES`).
2. `ManagerService.chat` khoá theo hội thoại (`_conv_locks`, đếm lượt giữ/chờ — hết lượt thì xoá khỏi dict) từ lúc lưu tệp tới lúc
   ghi tin + submit/steer. Tin thứ hai của cùng hội
   thoại gửi trong lúc tin có PDF đang trích sẽ chờ, rồi steer vào run của tin PDF (chờ run đó rời `queued` tối đa `QUEUED_WAIT_S`
   = 5 s). Không có khoá thì tin sau chạy trước và tin PDF bị steer vào run của nó, thứ tự tin của người bị đảo.
3. `_save_files_async` lấy thư mục hội thoại và cấu hình trên event loop, rồi chạy `write_attachment_files` trong thread
   (`asyncio.to_thread`). Trích PDF có thể mất nhiều giây và không được chặn loop của daemon.
4. Bản gốc ghi vào `attachments/doc-<thời điểm>-<ngẫu nhiên>-<tên>.pdf` (0600, `O_EXCL`).
5. Các PDF của một tin được trích SONG SONG (`ThreadPoolExecutor`, mỗi PDF một tiến trình con), mỗi cái cùng hạn giờ cứng
   `attachments.pdf.timeout_s` (mặc định 40 s, tối đa 50 s). Cả tin vì thế tốn tối đa khoảng một hạn giờ, dưới hạn 60 s mà web chờ
   `manager.chat`. Trước đây trích lần lượt: 4 PDF chậm vượt 60 s, web báo lỗi trong khi daemon vẫn nhận tin, người gửi lại thành
   lượt trùng.
6. `doc_extract.extract_isolated` chạy `python -I -m cfharness.manager.pdf_worker` (tiến trình con, cửa sổ ẩn trên Windows):
   - stdin: một dòng JSON (`out_dir` + tuỳ chọn), sau đó là byte PDF. `out_dir` = thư mục tạm riêng `.pdf-work-*` mà
     `write_attachment_files` tạo cạnh bản gốc (cùng ổ đĩa), mỗi PDF một thư mục, xoá ở `finally`.
   - Tiến trình con ghi THẲNG vào `out_dir`: `events.jsonl` (mỗi sự kiện của `pdf_events` một dòng JSON, ghi và flush ngay:
     `{"pages"}` → các `{"part", "first", "last", "file"}` → `{"parts_end"}` → các `{"page", "text"}` → `{"text_end"}` → `{"done"}`)
     và mỗi phần một tệp `part-NNNN.pdf`; stdout không dùng, stderr vào `stderr.txt`. Trước đây các phần đi qua stdout dạng base64:
     daemon giữ cùng lúc stdout thô, các dòng, JSON đã giải và byte các phần của tới 4 PDF — đo được đỉnh ~627 MB trong tiến trình
     daemon với 4 PDF 9,5 MB mà mọi trang dùng chung một ảnh 9 MB. Giờ daemon chỉ đọc `events.jsonl` từng dòng (dòng ≤
     `EVENT_LINE_MAX_BYTES`, tổng ≤ `EVENTS_MAX_BYTES`) rồi `os.replace` các tệp phần vào chỗ: cùng ca đo còn ~81 MB, đĩa 216 → 72 MB.
   - Tách phần chạy TRƯỚC vì chỉ chép đối tượng trang (nhanh), dùng tối đa 40% ngân sách mềm; chữ dùng phần còn lại.
   - Ngân sách mềm `time_budget_s` ≈ 80% hạn giờ cứng, chỉ kiểm giữa các trang. Một trang bệnh hoạn treo `extract_text` thì hạn giờ
     cứng giết tiến trình; các dòng và tệp phần đã ghi vẫn còn trong `out_dir`, nên các phần và trang đã xong được giữ, bản chữ ghi rõ
     dừng vì hết giờ.
   - `pdf_assemble` (phía daemon) dựng kết quả từ các dòng và kiểm TỪNG mục: phần phải khớp `.pNNN-MMM.pdf` (3–8 chữ số, đúng
     `first`/`last`, nối tiếp phần trước), `file` phải đúng mẫu `part-NNNN.pdf` và là tệp thường (không symlink) ngay trong `out_dir`,
     tổng dung lượng phần ≤ `parts_max_bytes` (kiểm lại ở daemon, không tin tiến trình con); trang phải đúng thứ tự. Mục sai bị bỏ
     riêng, không bỏ cả kết quả. (Trước đây regex chỉ nhận 3–5 chữ số nên PDF ≥ 100 000 trang mất cả bản chữ lẫn mọi phần.)
   - Dòng chữ TRONG PDF giống tiêu đề của hub (`--- page 7 ---`, `[PDF text copy made by cfharness…`, `--- (end of text copy…`) được
     thêm tiền tố `(in PDF) ` để không làm agent hiểu sai ranh giới trang hay phạm vi bản chữ.
7. Bản trích ghi cạnh bản gốc:
   - `<tên>.pdf.txt`: dòng đầu nêu phạm vi trang, vd `[PDF text copy made by cfharness: ONLY pages 1-13 of 400 (stopped: extraction
     time limit) …]`; sau đó chữ theo trang, mỗi trang có tiêu đề `--- page N ---`; bị cắt thì cuối tệp nhắc lại phạm vi. Trước đây
     chỉ có một dòng tiếng Việt ở CUỐI tệp; tệp vài MB thì Read (trần 25k token) không tới được cuối, và điều phối viên trả lời "13
     trang" cho PDF 400 trang.
   - `<tên>.pdf.p001-050.pdf`, …: chỉ có khi PDF dài hơn `split_pages` trang. Read đọc từng phần chỉ với `file_path` và vẫn thấy
     bảng, hình.
8. `write_attachment_files` trả thêm `pdf_info` = {bản gốc: info} (`status`, `pages`, `text_pages`, `text_reason`, `parts_last`,
   `parts_reason`, `no_text`). `prompts.files_note` dùng nó để nói với điều phối viên:
   - Tổng số trang; bản chữ phủ trang nào (`text copy of pages 1-500 of 2000 ONLY (stopped: page limit)`); các phần phủ trang nào.
   - Trang còn lại: có `pdftoppm` → Read bản gốc với `pages`; không có và bản gốc ≤ 600 trang → Read bản gốc chỉ với `file_path`
     (đắt, chỉ khi cần); không có và > 600 trang → nói rõ không đọc được (API từ chối PDF > 600 trang), báo người, không đoán.
   - Không có bản chữ: lý do cụ thể (scan, không phân tích được, hết giờ, mọi trang trích lỗi — `no_text: page_errors`, không gọi
     nhầm là PDF scan). PDF có mật khẩu: dặn KHÔNG Read (Read cũng không giải mã được) mà báo người, xin bản không khoá.
   - Máy không có `pdftoppm`: thêm câu "never pass `pages`".

## 3. Trích (`manager/doc_extract.py`, thư viện `pypdf`)

| Trường hợp | Kết quả |
|---|---|
| PDF có chữ | `.txt`, tối đa `PDF_TEXT_MAX_PAGES` = 500 trang và `OUT_MAX_BYTES` = 8 MB; dòng đầu nêu phạm vi trang và lý do cắt |
| PDF scan (không trang nào có chữ) | Không có `.txt` (`no_text: scanned`); vẫn tách phần nếu dài |
| Mọi trang trích lỗi (trần giải nén, content stream hỏng) | Không có `.txt` (`no_text: page_errors`) |
| Mã hoá | Thử mật khẩu rỗng. Mở được thì xử lý bình thường; không mở được thì không có bản trích (`status: encrypted`) |
| Hỏng, cụt, rác | Không ném lỗi; không có bản trích (`status: unreadable`), chỉ lưu bản gốc |
| Một trang lỗi | Trang đó ghi `(the text of this page could not be extracted)`, các trang khác vẫn trích |

Giới hạn công việc:
- pypdf có sẵn trần giải nén (`ZLIB_MAX_OUTPUT_LENGTH`, `MAX_DECLARED_STREAM_LENGTH`…). pypdf khoá ở 6.21.4 (`>=6.0,<7`): bản đã ra
  hơn 2 tuần; lên major mới phải xem lại vì thư viện này đọc tệp không tin cậy.
- Ngân sách thời gian mềm được kiểm giữa các trang (tách phần ≤ 40%, chữ phần còn lại); hạn giờ cứng của tiến trình con lo trang treo.
- Tách phần (`pdf_part_pages`): tối đa `attachments.pdf.max_parts` phần; số trang mỗi phần = `split_pages`, nới lên tới
  `PDF_PART_MAX_PAGES` = 100 để các phần phủ hết tài liệu (2000 trang, 20 phần → 100 trang/phần). PDF 3000 trang thì các phần phủ
  trang 1–2000 và `files_note` nói rõ. Tổng tối đa `PDF_PARTS_MAX_BYTES` (48 MB) cho CẢ TIN, chia đều cho các PDF của tin (4 PDF →
  mỗi cái ≤ 12 MB). PDF mà mọi trang dùng chung một tài nguyên lớn (font, ảnh) làm mỗi phần chép lại tài nguyên đó: trần theo tin
  giữ cả bộ nhớ lẫn đĩa trong giới hạn. `split_pages` tối đa 100 vì Read đọc cả phần một
  lần (API từ chối PDF > 600 trang).
- Tiến trình con trên POSIX tự đặt `RLIMIT_AS` 2 GB. Windows chỉ có hạn giờ.

Logger `pypdf` bị hạ xuống ERROR để PDF lỗi nhẹ không làm ồn `hub.log`.

Đã đo trên máy dev (Windows 11): 4 PDF chậm 8 trang (200k toán tử `Tj`/trang) trong một tin: 38,5 s (trước: 131 s, lần lượt). PDF 400
trang chữ dày: bản chữ trang 1–16 + 8 phần phủ 1–400. PDF 3000 trang: bản chữ 1–500 + 20 phần × 100 trang phủ 1–2000. PDF 100 000
trang: 20 phần tên 6 chữ số được giữ. PDF có một trang treo, hạn giờ 5 s: giữ trang 1–3 và mọi phần.

## 4. Poppler (`pdf_support.py`)

- `pdftoppm_status(cfg)` dò `pdftoppm` trên PATH của daemon, sau đó trong `tools.poppler_path`, rồi CHẠY thử `pdftoppm -v`
  (`probe`) theo quy tắc của Claude Code 2.1.329: dùng được khi mã thoát 0, hoặc mã khác 127 và có in ra stderr. File có mà chạy
  không được (hỏng, sai kiến trúc) → không dùng được (`broken`): hub vẫn nhắc "never pass `pages`", doctor cảnh báo.
  Kết quả cache theo (giá trị cấu hình, PATH) trong `CACHE_TTL_S` = 300 s, nên không gọi tiến trình con cho mỗi tin. Đổi
  `tools.poppler_path` thì dò lại ngay.
- Không bao giờ chạy `pdftoppm -v` trên event loop của daemon: gọi trong event loop (`block=None` → False) thì cache hết hạn trả giá
  trị cũ và dò lại trong thread nền; chưa có cache trả `usable=False, pending=True` (chỉ thêm lời nhắc, an toàn) rồi dò nền.
  `_chat_turn` gọi `pdf_render_available` qua `asyncio.to_thread`; `RunManager` làm tươi cache bằng `asyncio.to_thread` ngay trước
  `_prepare_spec` (system prompt + `path_extra` chỉ đọc cache); daemon dò sẵn lúc khởi động (`pdf_support.warm`). Doctor chạy trong
  thread (`block=True`).
- PATH là PATH của daemon lúc khởi động. Cài poppler bằng scoop/winget khi daemon đang chạy: `hub doctor` (tiến trình mới, PATH mới)
  báo có, nhưng daemon vẫn nhắc "không có pdftoppm" tới khi khởi động lại. Đặt `tools.poppler_path` thì có hiệu lực ngay. Doctor nói
  điều này trong cách sửa khi THIẾU pdftoppm; khi đã dùng được thì không nhắc.
- Không có `pdftoppm`:
  - Mọi run có khối `# Reading PDF files` ngay sau quy ước hub (`prompt.build_system_prompt(pdf_note=…)`). Khối này ổn định theo máy
    nên không phá prompt cache. Run resume dùng lại prompt cũ của session. Takeover (`tui/takeover.py`) và dấu vân tay prompt của
    eval (`evals/overlay.py`) cũng truyền `pdf_note`, nên prompt eval giống prompt run thật.
  - `files_note` có thêm câu nhắc khi tin có PDF.
- `tools.poppler_path` = thư mục chứa `pdftoppm(.exe)`:
  - Khi đặt, giá trị phải là đường dẫn tuyệt đối tới thư mục có sẵn, chứa `pdftoppm`; không chứa NUL, xuống dòng hay ký tự ngăn cách
    PATH. Một giá trị không thể chèn nhiều mục PATH.
  - Lúc spawn, hub kiểm lại. Thư mục không còn hợp lệ thì bỏ qua và ghi log, không chặn run.
  - Thư mục được thêm vào CUỐI PATH của tiến trình `claude` (`RunSpec.path_extra` → `ClaudeCliRuntime._run_env`). Kế hoạch ban đầu là
    chèn đầu PATH; thêm cuối an toàn hơn vì thư mục poppler không che được `git`, `python`… sẵn có, chỉ bổ sung lệnh còn thiếu.
  - Nhánh `tools` ∈ `integrity.SECURITY_KEYS` (đổi PATH của mọi run). Web chỉ hiển thị khoá này; đổi bằng
    `cfharness config set tools.poppler_path <thư mục>`.
- `pdftoppm` tìm thấy trên PATH của daemon: thư mục của nó cũng vào `path_extra`. `env_extra` của runtime có thể thay hẳn PATH của
  run; khi đó hub đã bỏ lời nhắc "never pass `pages`" mà run lại không thấy `pdftoppm`.
- `hub doctor` có mục `pdftoppm` (mức cảnh báo):
  - Thiếu: gợi ý `scoop install poppler`, `winget install oschwartz10612.Poppler`, `brew install poppler`, `apt install poppler-utils`
    (rồi khởi động lại daemon), hoặc đặt `tools.poppler_path`.
  - Có file mà `pdftoppm -v` không chạy được: cảnh báo, gợi ý cài lại.
  - `tools.poppler_path` hỏng (thư mục mất, không còn `pdftoppm`): cảnh báo kèm cách sửa.

## 5. Cấu hình

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `attachments.pdf.split` | `true` | Tách PDF dài thành các phần (bản chữ vẫn luôn có) |
| `attachments.pdf.split_pages` | `50` | Số trang mỗi phần; PDF nhiều hơn N trang mới tách (1–100) |
| `attachments.pdf.max_parts` | `20` | Số phần tối đa mỗi PDF (1–200); số trang mỗi phần tự nới (≤ 100) để phủ hết PDF |
| `attachments.pdf.timeout_s` | `40` | Hạn giờ cứng trích PDF của một tin, các PDF chạy song song (5–50 s, dưới hạn 60 s của web) |
| `tools.poppler_path` | `null` | Thư mục chứa `pdftoppm`; chỉ người đặt qua CLI |

Ba khoá `split*`/`max_parts` sửa được trên web (Cài đặt → Điều phối viên). `config.validate_setting` kiểm kiểu và khoảng của mọi khoá
(khoảng số lấy từ `doc_extract.PDF_LIMITS`, cùng chỗ với `pdf_options`).

Web (`web/manager.py`): tin có tệp gọi `manager.chat` với hạn chờ `CHAT_FILES_TIMEOUT_S` = 120 s thay vì 60 s mặc định. Vẫn quá hạn
thì trả 504 với lời báo "tin có thể vẫn đã được nhận — tải lại hội thoại và kiểm tra trước khi gửi lại", không để lỗi 500.

## 6. Việc chưa làm

- Run khác ngoài `ClaudeCliRuntime.start` không thêm `tools.poppler_path` vào PATH: lời gọi một lần (`oneshot`, judge, `--version`)
  và takeover tương tác. Các lời gọi này không đọc PDF đính kèm.
- PDF trong workspace của agent (không phải tệp người gửi cho điều phối viên) không được trích. Agent dùng khối system prompt (chỉ
  `file_path`) hoặc cài poppler.
- Trích PDF vẫn chạy trước khi `manager.chat` trả (tối đa một hạn giờ, ≤ 50 s). Telegram/Slack không còn bị chặn (task riêng của
  chat); web chờ tới 120 s. Cách triệt để hơn (trả ngay, gắn bản trích khi xong) chưa làm.

## 7. Test

- `tests/test_doc_extract.py`: PDF sinh trong test bằng pypdf (`make_pdf`, có font Identity-H cho tiếng Việt, mã hoá AES).
  Phủ các ca: chữ, tiếng Việt, hỏng/cụt, mã hoá, scan, trần trang/dung lượng/thời gian (dòng đầu nêu phạm vi), tách phần + nới
  trang/phần, tiến trình con (thật; ghi phần vào thư mục tạm, không qua stdout; bị giết giữa chừng vẫn giữ dòng đã ghi; mục lạ/tên
  tệp lạ bị bỏ từng cái; trần dung lượng phần kiểm lại ở daemon; dòng quá dài bị bỏ; tên phần 6 chữ số), `write_attachment_files`
  (song song, trần phần chia theo tin, xoá thư mục tạm), mọi trang lỗi ≠ scan, dòng giả tiêu đề, `files_note` (phạm vi phủ, > 600
  trang, có/không pdftoppm, PDF có mật khẩu).
- `tests/test_pdf_support.py`: kiểm `tools.poppler_path` + `attachments.pdf.*`, cache dò, không dò trên event loop (`pending`, giá trị
  cũ + dò nền), `probe` (`pdftoppm -v`), binary hỏng, khối system prompt (cả prompt eval), PATH của run (`_run_env` và `RunSpec` qua
  `Hub` + `FakeRuntime`), mục doctor (không nhắc khởi động lại khi đã dùng được).
- `tests/test_manager.py::test_pdf_message_keeps_order_with_message_sent_during_extraction`: thứ tự tin khi trích PDF lâu.
- `tests/test_web_manager.py::test_send_with_files_waits_longer_and_timeout_is_clear`: hạn chờ web + 504 rõ ràng.
- Kênh: `tests/test_telegram_media.py::test_pdf_document_gets_text_copy_and_parts`, `tests/test_slack_media.py::test_pdf_file_accepted_with_text_copy`;
  vòng nhận không bị chặn khi trích: `test_pdf_extraction_does_not_stall_update_loop` (Telegram, kể cả `/moi` xếp hàng sau tin có tệp),
  `test_pdf_extraction_does_not_stall_slack_worker`.
