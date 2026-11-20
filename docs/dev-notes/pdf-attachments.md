# PDF đính kèm cho điều phối viên

Người gửi PDF cho điều phối viên qua web, Telegram hoặc Slack. Cả ba kênh đi qua `manager.chat` → `ManagerService._save_files_async`
→ `write_attachment_files` (`manager/service.py`), nên mọi kênh có cùng hành vi.

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
2. `_save_files_async` lấy thư mục hội thoại và cấu hình trên event loop, rồi chạy `write_attachment_files` trong thread
   (`asyncio.to_thread`). Trích PDF có thể mất vài giây và không được chặn loop của daemon.
3. Bản gốc ghi vào `attachments/doc-<thời điểm>-<ngẫu nhiên>-<tên>.pdf` (0600, `O_EXCL`).
4. `doc_extract.extract_isolated` chạy `python -I -m cfharness.manager.pdf_worker` (tiến trình con, cửa sổ ẩn trên Windows):
   - stdin: một dòng JSON tuỳ chọn, sau đó là byte PDF. stdout: `[[hậu tố, base64], …]`.
   - Hạn giờ cứng `attachments.pdf.timeout_s` (mặc định 90 s). Quá hạn thì tiến trình bị giết và hub chỉ lưu bản gốc.
   - Hậu tố trả về phải khớp `PDF_COPY_SUFFIX` (`.txt` hoặc `.pNNN-MMM.pdf`). Hậu tố lạ thì bỏ toàn bộ kết quả.
5. Bản trích ghi cạnh bản gốc:
   - `<tên>.pdf.txt`: chữ theo trang, mỗi trang có tiêu đề `--- page N ---`.
   - `<tên>.pdf.p001-050.pdf`, …: chỉ có khi PDF dài hơn `split_pages` trang. Read đọc từng phần chỉ với `file_path` và vẫn thấy
     bảng, hình.
6. `prompts.files_note` báo cho điều phối viên từng trường hợp:
   - Có bản chữ: liệt kê bản chữ và các phần.
   - Không có bản chữ (scan, mã hoá, hỏng): nói rõ, bảo Read bản gốc chỉ với `file_path`.
   - Máy không có `pdftoppm`: thêm câu "never pass `pages`".

## 3. Trích (`manager/doc_extract.py`, thư viện `pypdf`)

| Trường hợp | Kết quả |
|---|---|
| PDF có chữ | `.txt`, tối đa `PDF_TEXT_MAX_PAGES` = 500 trang và `OUT_MAX_BYTES` = 8 MB; bị cắt thì cuối chữ có dòng `--- (cắt bớt: …) ---` |
| PDF scan (không trang nào có chữ) | Không có `.txt`; vẫn tách phần nếu dài |
| Mã hoá | Thử mật khẩu rỗng. Mở được thì xử lý bình thường; không mở được thì không có bản trích |
| Hỏng, cụt, rác | Không ném lỗi; trả `[]` (chỉ lưu bản gốc) |
| Một trang lỗi | Trang đó ghi `(không đọc được chữ của trang này)`, các trang khác vẫn trích |

Giới hạn công việc:
- pypdf có sẵn trần giải nén (`ZLIB_MAX_OUTPUT_LENGTH`, `MAX_DECLARED_STREAM_LENGTH`…).
- Ngân sách thời gian `PDF_TIME_BUDGET_S` được kiểm giữa các trang, một lần cho phần chữ và một lần cho phần tách.
- Tách phần: tối đa `attachments.pdf.max_parts` phần, tổng tối đa `PDF_PARTS_MAX_BYTES` (48 MB).
- Tiến trình con trên POSIX tự đặt `RLIMIT_AS` 2 GB. Windows chỉ có hạn giờ.

Logger `pypdf` bị hạ xuống ERROR để PDF lỗi nhẹ không làm ồn `hub.log`.

## 4. Poppler (`pdf_support.py`)

- `pdftoppm_status(cfg)` dò `pdftoppm` trên PATH của daemon, sau đó trong `tools.poppler_path`. Kết quả cache theo
  (giá trị cấu hình, PATH) trong `CACHE_TTL_S` = 300 s, nên không gọi tiến trình con cho mỗi tin. Đổi `tools.poppler_path` thì dò lại ngay.
- Không có `pdftoppm`:
  - Mọi run có khối `# Reading PDF files` ngay sau quy ước hub (`prompt.build_system_prompt(pdf_note=…)`). Khối này ổn định theo máy
    nên không phá prompt cache. Run resume dùng lại prompt cũ của session.
  - `files_note` có thêm câu nhắc khi tin có PDF.
- `tools.poppler_path` = thư mục chứa `pdftoppm(.exe)`:
  - Khi đặt, giá trị phải là đường dẫn tuyệt đối tới thư mục có sẵn, chứa `pdftoppm`; không chứa NUL, xuống dòng hay ký tự ngăn cách
    PATH. Một giá trị không thể chèn nhiều mục PATH.
  - Lúc spawn, hub kiểm lại. Thư mục không còn hợp lệ thì bỏ qua và ghi log, không chặn run.
  - Thư mục được thêm vào CUỐI PATH của tiến trình `claude` (`RunSpec.path_extra` → `ClaudeCliRuntime._run_env`). Kế hoạch ban đầu là
    chèn đầu PATH; thêm cuối an toàn hơn vì thư mục poppler không che được `git`, `python`… sẵn có, chỉ bổ sung lệnh còn thiếu.
  - Nhánh `tools` ∈ `integrity.SECURITY_KEYS` (đổi PATH của mọi run). Web chỉ hiển thị khoá này; đổi bằng
    `cfharness config set tools.poppler_path <thư mục>`.
- `hub doctor` có mục `pdftoppm` (mức cảnh báo):
  - Thiếu: gợi ý `scoop install poppler`, `winget install oschwartz10612.Poppler`, `brew install poppler`, `apt install poppler-utils`,
    hoặc đặt `tools.poppler_path`.
  - `tools.poppler_path` hỏng (thư mục mất, không còn `pdftoppm`): cảnh báo kèm cách sửa.

## 5. Cấu hình

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `attachments.pdf.split` | `true` | Tách PDF dài thành các phần (bản chữ vẫn luôn có) |
| `attachments.pdf.split_pages` | `50` | Số trang mỗi phần; PDF nhiều hơn N trang mới tách (1–10000) |
| `attachments.pdf.max_parts` | `20` | Số phần tối đa mỗi PDF (1–200) |
| `attachments.pdf.timeout_s` | `90` | Hạn giờ tiến trình trích một PDF (5–600 s) |
| `tools.poppler_path` | `null` | Thư mục chứa `pdftoppm`; chỉ người đặt qua CLI |

Ba khoá `split*`/`max_parts` sửa được trên web (Cài đặt → Điều phối viên). `config.validate_setting` kiểm kiểu và khoảng của mọi khoá.

## 6. Việc chưa làm

- Run khác ngoài `ClaudeCliRuntime.start` không thêm `tools.poppler_path` vào PATH: lời gọi một lần (`oneshot`, judge, `--version`)
  và takeover tương tác. Các lời gọi này không đọc PDF đính kèm.
- PDF trong workspace của agent (không phải tệp người gửi cho điều phối viên) không được trích. Agent dùng khối system prompt (chỉ
  `file_path`) hoặc cài poppler.

## 7. Test

- `tests/test_doc_extract.py`: PDF sinh trong test bằng pypdf (`make_pdf`, có font Identity-H cho tiếng Việt, mã hoá AES).
  Phủ các ca: chữ, tiếng Việt, hỏng/cụt, mã hoá, scan, trần trang/dung lượng/thời gian, tách phần, tiến trình con (thật + hết giờ +
  kết quả lạ), `write_attachment_files`, `files_note`.
- `tests/test_pdf_support.py`: kiểm `tools.poppler_path` + `attachments.pdf.*`, cache dò, khối system prompt, PATH của run (`_run_env`
  và `RunSpec` qua `Hub` + `FakeRuntime`), mục doctor.
- Kênh: `tests/test_telegram_media.py::test_pdf_document_gets_text_copy_and_parts`, `tests/test_slack_media.py::test_pdf_file_accepted_with_text_copy`.
