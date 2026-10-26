# Ngôn ngữ hình ảnh của CLI `cfharness`

Mục tiêu: CLI, chat (`cfharness chat`) và TUI (`top`) trông như **một sản phẩm**: cùng bảng màu, cùng ký hiệu trạng thái,
cùng cách viết lỗi/gợi ý. Mã: `src/cfharness/ui/theme.py` (token, chế độ in, khối dựng) và `src/cfharness/ui/views.py`
(các màn của CLI, thuần — không I/O). Ảnh: `docs/screenshots/cli/` (ghi lại bằng `uv run python scripts/cli_screenshots.py`).

## 1. Ba chế độ in

| Chế độ | Khi nào | In gì |
|---|---|---|
| JSON | `--json` | `json.dumps` như cũ — **byte không đổi** (test `test_json_identical_when_styled`) |
| thuần | stdout không phải TTY (pipe, `$(…)`, test), `TERM=dumb`, `CFHARNESS_PLAIN=1` | đúng chuỗi văn bản cũ (bảng `_table`, `lỗi: …`) — script grep/parse không vỡ. Không ANSI |
| có kiểu | stdout là TTY, hoặc `FORCE_COLOR` (khác rỗng/`0`) | màn `rich`: tiêu đề, bảng, khối khoá/giá trị, huy hiệu trạng thái, gợi ý |

- `NO_COLOR` (theo no-color.org: có mặt và khác rỗng): giữ bố cục + đậm/mờ/đảo màu, **bỏ màu**. Có cả `FORCE_COLOR` và `NO_COLOR` → có kiểu, không màu.
- stderr quyết định riêng (`report_error` dò `sys.stderr`): `cfharness … 2>err.log` vẫn ra `lỗi: …` thuần.
- `ctx.out(data, text, view)`: lệnh truyền thêm `view` (renderable hoặc hàm) — chỉ chế độ có kiểu dùng. Lệnh chưa có `view` vẫn in `text` ở mọi chế độ (an toàn khi mở rộng dần).
- Trong pytest, `tests/conftest.py` xoá `FORCE_COLOR`/`CFHARNESS_PLAIN`/`CFHARNESS_ASCII` (autouse) để CI có `FORCE_COLOR` không làm lệch assert chuỗi thuần.

## 2. Bảng màu

Nguyên tắc: **chỉ 16 màu ANSI có tên + thuộc tính** (`bold`, `dim`, `reverse`). Terminal tự ánh xạ theo theme sáng/tối nên
cùng một bảng màu đọc được trên nền đen lẫn nền trắng; 16 màu / 256 màu / truecolor cho kết quả như nhau, `NO_COLOR` rơi về
đậm/mờ. KHÔNG dùng hex (`#7aa2f7`), `grey42…grey70` (mất chữ trên nền trắng), `white on red` (ép nền). `yellow` chỉ dùng
cho ký hiệu/nhãn ngắn và kèm `bold` (vàng trên nền trắng yếu). `blue` thường quá tối trên nền đen của conhost → dùng `bright_blue`.

| Token (`ui/theme.py`) | Giá trị | Dùng cho |
|---|---|---|
| `OK` | `green` | xong, ổn, cho phép |
| `WARN` | `yellow` | cảnh báo, đang giữ, chờ slot |
| `ERR` / `ERR_STRONG` | `red` / `bold red` | lỗi, từ chối, chờ người (cần hành động) |
| `INFO` | `cyan` | trong hàng đợi, tên tool, loại event |
| `ACCENT` | `bold bright_blue` | dấu ◆ tiêu đề màn, ▶ bắt đầu run |
| `BRAND` | `bold reverse bright_blue` | huy hiệu ` cfharness ` |
| `ALARM` | `bold reverse red` | ` ESTOP BẬT `, cảnh báo đỏ |
| `MUTED` | `dim` | thời gian, đường dẫn, chú thích, chữ phụ |
| `HEAD` | `bold` | tiêu đề mục, tiêu đề cột |
| `KEY` | `dim` | cột khoá của khối khoá/giá trị |
| `ID` | `bold` | id agent/run/approval |
| `CMD` | `cyan` | lệnh gõ lại được (`cfharness …`) |
| `COST` | `magenta` | tiền, token |
| `USER` / `MGR` | `bold cyan` / `bold magenta` | người / điều phối viên trong chat (= `chatview.C_USER`/`C_MGR`) |
| `DATA` | (mặc định) | chữ do agent sinh |

## 3. Từ vựng trạng thái

`theme.STATUS[miền][trạng_thái] = Status(icon, ascii, style, label)`; `theme.badge(miền, trạng_thái, mode)` → `Text("● đang làm")`.
Một ký hiệu = một ý nghĩa ở mọi miền:

| Ký hiệu | ASCII | Ý nghĩa | Màu |
|---|---|---|---|
| ● | `*` | đang chạy / đang làm | bold green |
| ◐ | `~` | máy đang giữ/chờ (slot, approval đang giữ, review, rate limit) | (bold) yellow |
| ⏸ | `!` | **chờ NGƯỜI** duyệt | bold red |
| ○ | `o` | rảnh / cần làm | dim |
| ◌ | `.` | trong hàng đợi / sẵn sàng | cyan |
| ✓ | `+` | xong / cho phép / ổn | green |
| ✖ (✗ cho kiểm tra) | `x` | lỗi / sập / quá giờ / từ chối | (bold) red |
| ⊘ | `-` hoặc `!` | huỷ, tắt, xoá mềm (dim) · bị chặn (bold red) | |
| ‖ | `=` | tạm dừng | dim |
| ↺ / ↪ | `~` / `>` | bị ngắt (sẽ resume) / đã tiếp nối | yellow / dim |
| $ | `$` | vượt ngân sách | bold yellow |

- **agent**: working ● đang làm · waiting ◐ chờ slot · waiting_human ⏸ chờ người duyệt · idle ○ rảnh · error ✖ lỗi · paused ‖ tạm dừng · disabled ⊘ đã tắt · archived ⊘ đã xoá mềm. Ký hiệu khớp `tui/model.py` `STATUS_STYLE` (BRD FR-UI-01) — có test `test_status_vocabulary_matches_tui`.
- **run**: queued ◌ trong hàng đợi · running ● đang chạy · waiting_human ⏸ · completed ✓ xong · failed ✖ lỗi · cancelled ⊘ đã huỷ · interrupted ↺ bị ngắt · resumed ↪ đã tiếp nối · budget_exceeded $ vượt ngân sách · timed_out ✖ quá giờ · crashed ✖ sập · rate_limited ◐ rate limit · blocked ⊘ bị chặn · protocol_violation ✖ sai giao thức (đủ `runs.TERMINAL_STATUSES`).
- **approval**: open/pending ◐ đang giữ · waiting_human ⏸ chờ người · allowed ✓ đã cho phép · denied ✖ đã từ chối · cancelled ⊘ đã huỷ.
- **task**: triage ◇ phân loại · todo ○ cần làm · ready ◌ sẵn sàng · running ● đang làm · review ◐ chờ review · done ✓ xong · blocked ⊘ bị chặn · archived ⊘ lưu trữ.
- **check** (doctor): ok ✓ · warning ! · error ✗.

Nhãn tiếng Việt chỉ ở màn có kiểu; JSON và chế độ thuần giữ id tiếng Anh (`completed`, `working`…) — cờ lọc `--status` vẫn nhận id tiếng Anh.

## 4. Kiểu chữ & khối dựng

| Khối | Hàm | Quy tắc |
|---|---|---|
| Huy hiệu đầu | `brand(version, tagline)` | ` cfharness ` đảo màu + `vX` mờ — chỉ màn tổng quan |
| Tiêu đề màn | `header(title, meta)` | `◆ Tiêu đề  meta-mờ`, rồi một dòng trống |
| Tiêu đề mục | `section(title)` | đậm, không ký hiệu; nội dung thụt 2 cột |
| Khoá/giá trị | `kv(pairs, width)` | khoá mờ, giá trị thường; giá trị dài gập; `< 60` cột → khoá một dòng, giá trị thụt dưới |
| Bảng | `table(cols, rows)` | tiêu đề cột ĐẬM IN HOA, gạch `─` dưới tiêu đề, không viền, ô một dòng cắt `…`; cột phụ có `drop_below` tự ẩn khi hẹp; số/tiền canh phải |
| Ô trống | `cell(None)` | `-` mờ |
| Thành công | `success(msg)` | `✓ thông điệp` (✓ xanh đậm) |
| Cảnh báo | `warning(msg)` | `! Cảnh báo: …` (vàng đậm) |
| Lỗi | `error(msg, fix=, details=)` | `✗ Lỗi: thông điệp` · `  • trường: lỗi` · `  Cách sửa: lệnh` |
| Gợi ý | `hint(text)` | `→ chữ mờ` + lệnh trong `…` tô `CMD` (dấu backtick bị bỏ) |
| Rỗng | `empty(text)` | `(chưa có …)` mờ + một `hint` cách tạo |

- Mỗi màn danh sách kết thúc bằng **một** dòng gợi ý bước tiếp theo (`→ chi tiết: cfharness runs show <run>`).
- Thông điệp của hub có lệnh trong dấu `` ` `` → `cmds()` tô lệnh; không in dấu backtick ra màn có kiểu.
- Lỗi: `fix_for(code, message, data)` chọn "Cách sửa": `data.hint` → (thông điệp đã kèm lệnh: không thêm) → theo mã: NOT_FOUND → lệnh liệt kê đúng loại (`agent list`, `runs list`, `approvals --all`…); BLOCKED → ESTOP/ngân sách/đăng nhập; CONFLICT "đã tồn tại"; SYSTEM_ERROR → `daemon logs`; daemon không chạy → `daemon start`/`doctor`. **Mã thoát giữ nguyên 0/1/2/3.**
- Thời gian trong bảng: `MM-DD HH:MM` giờ máy (`fmt_ts`); thời lượng `2.9s`/`3m05s`/`1h02m` (`fmt_ms`); tiền `$0.0047` (`fmt_usd`, 4 số lẻ như chế độ thuần).

## 5. Khoảng cách & bề rộng

- Thụt chuẩn 2 cột; một dòng trống giữa tiêu đề màn và nội dung, giữa các mục; không dòng trống thừa cuối màn.
- `Padding` không giãn hết bề rộng (`theme.Padding`, `expand=False`) — không có khoảng trắng cuối dòng khi copy.
- Bề rộng: `rich` tự đo; `COLUMNS` ghi đè. `NARROW = 60`: khối khoá/giá trị xếp dọc, tổng quan xếp lệnh/mô tả hai dòng. Bảng ẩn cột phụ theo `drop_below` (vd `agent list`: VAI TRÒ < 96, TOOL CUỐI < 120, MODEL < 72). Đường dẫn dài gập theo ký tự (`print(..., overflow="fold")`), không ngắt giữa nhãn và giá trị.

## 6. Unicode ↔ ASCII

`Mode.unicode` (`theme.unicode_ok`): `CFHARNESS_ASCII=1` ép ASCII, `=0` ép unicode; không thì ASCII khi code page GỐC không
phải UTF (cp437/cp1252 — `remember_encodings()` ghi lại trước khi `cli.main` ép stdout sang UTF-8), hoặc khi trên Windows
không có dấu hiệu terminal hiện đại (`WT_SESSION`, `TERM_PROGRAM`, `ConEmuANSI`, `TERM`, `ALACRITTY_LOG`, `WEZTERM_EXECUTABLE`, `ANSICON`)
= conhost cũ, font raster/Consolas thiếu glyph. ASCII: ký hiệu theo cột ASCII ở §3, `GLYPHS` (`✓→+`, `→→->`, `◆→*`…), bảng dùng
khung `ASCII_SIMPLE_HEAD` (`-` dưới tiêu đề). Giữ `·` (U+00B7, có trong cp437/cp1252) và chữ tiếng Việt.
Conhost không VT: `rich` tự dùng API win32; khi `FORCE_COLOR` qua pipe trên Windows thì ép `legacy_windows=False` (API win32 chỉ hợp với console thật).

## 7. Chữ của agent là DỮ LIỆU

Mọi chữ do agent sinh (tin nhắn, prompt, preview tool/approval, lỗi/tóm tắt run, tên/vai trò agent) đi qua `theme.data()` /
`theme.cell()` = `termsafe.clean` + `rich.text.Text` (không parse markup). Console của `theme.console()` tắt `markup`,
`emoji`, `highlight` — chuỗi `[bold red]…`, `[link=…]`, `:emoji:` hiện nguyên văn; ANSI/OSC 8/bidi bị bỏ. Chế độ thuần của
`run --follow` cũng qua `termsafe.clean` (trước đây in thô khi pipe). Test: `test_markup_injection_inert_in_views`,
`test_error_message_markup_inert`.

## 8. Lệnh đã áp dụng

| Lệnh | Màn có kiểu (`ui/views.py`) |
|---|---|
| `cfharness` (không đối số) | `overview` — 4 nhóm lệnh thường dùng (trước đây: lỗi argparse, mã 2; nay mã 0, cả khi pipe có bản thuần `plain_overview`) |
| `cfharness --help` / `-h` | TTY: `overview` + "Mọi lệnh" theo nhóm (bỏ lệnh ẩn `==SUPPRESS==`); pipe: trợ giúp argparse như cũ. `cfharness <lệnh> --help` không đổi |
| `doctor` | `doctor` — ✓/!/✗ + tên kiểm + chi tiết (lệnh tô màu), tổng kết lỗi/cảnh báo |
| `daemon start/stop/status` | `daemon_started`, `daemon_status` (khoá/giá trị, ESTOP đảo đỏ, cảnh báo auth/compat dạng lỗi có cách sửa), `daemon_down` |
| `agent list/show/create/edit/rm/restore/pause/resume` | `agent_list` (bảng co theo bề rộng), `agent_show` (khối + bảng run gần nhất), `agent_created`, dòng thành công |
| `runs list/show/stop`, `run` | `runs_list`, `run_show` (khối, prompt/tóm tắt/lỗi, argv mờ, bảng event với `--events`), `run_queued` |
| `run -f`, `runs follow` | `event_line` — một dòng/event, cùng tập event với `render_event` |
| `approvals` | `approvals` — bảng + gợi ý approve/deny (sửa tối thiểu `guard/cli.py`, không đụng logic approval) |
| `estop on/off/status` | `estop` — ` ESTOP BẬT ` đảo đỏ + lý do + cách tắt |
| `config get/set` | `config_value` (dict → khoá chấm phẳng `runs.stop_grace_s` = đúng khoá cho `config set`; kiểu giá trị có màu), `config_set` |
| mọi lỗi (`HubError`/RPC, daemon không chạy, `ValueError`) | `report_error` → `theme.error` + "Cách sửa" (stderr TTY); thuần giữ `lỗi: …` |

Lệnh khác (team, task, learn, mcp…) vẫn in văn bản cũ ở mọi chế độ; áp dụng dần bằng cách truyền `view` cho `ctx.out`.

## 9. Ảnh màn

Ghi bằng hub tạm `runtime.kind fake` (`scripts/cli_screenshots.py`, che thư mục home/tên người dùng). Màn approvals dùng
2 yêu cầu mẫu (FakeRuntime không sinh thao tác phá huỷ); còn lại là dữ liệu thật của hub tạm.

- `docs/screenshots/cli/01-tong-quan.svg` — `cfharness`
- `docs/screenshots/cli/02-daemon-status.svg` — `cfharness daemon status`
- `docs/screenshots/cli/03-agent-list.svg` — `cfharness agent list`
- `docs/screenshots/cli/04-runs-list.svg` — `cfharness runs list`
- `docs/screenshots/cli/05-run-follow.svg` — `cfharness run coder "…" -f`
- `docs/screenshots/cli/06-approvals-estop-loi.svg` — `approvals` · `estop on` · lỗi có "Cách sửa"

## 10. Danh sách chuyển đổi cho chat và TUI (CHƯA sửa — agent khác đang làm các file này)

Import: `from cfharness.ui import theme as th` (chỉ phụ thuộc `rich` + `termsafe`, không I/O).

### `src/cfharness/manager/chatview.py`
| Hiện tại | Đổi sang |
|---|---|
| `C_USER = "bold cyan"` | `C_USER = th.USER` (cùng giá trị) |
| `C_MGR = "bold magenta"` | `C_MGR = th.MGR` (cùng giá trị) |
| mọi `"grey42"`, `"grey50"`, `"grey62"`, `"grey70"` (`status_text`, `_header`, `render` action/approval/steer/note, `banner`, `help_text`, `conv_list_text`) | `th.MUTED` |
| `"bold red"` (`render` manager lỗi) | `th.ERR_STRONG` |
| `"red"` / `"green"` (`render` action) | `th.ERR` / `th.OK` |
| `"bold yellow"`, `"yellow"` (notify, approval, `status_text` chờ duyệt, tone `warn`) | `f"bold {th.WARN}"` / `th.WARN` |
| `"✓ "` / `"✗ "` (action) | `th.glyph("ok", u)` / `th.glyph("err", u)` với `u = th.detect().unicode` |
| `"  ⏸ "` (approval) | `th.STATUS["approval"]["waiting_human"].icon` |
| `"  • "` (note) | `th.glyph("bullet", u)` |
| `"  ↪ "` (steer) | `th.STATUS["run"]["resumed"].icon` |
| dòng `cfharness approve {rid}  ·  cfharness deny {rid}` | `th.hint(f"duyệt: \`cfharness approve {rid}\` · từ chối: \`cfharness deny {rid}\`")` (lệnh tô `CMD`) |
| `"◆ "` / `"▸ "` | giữ `◆` (= `th.glyph("mark")`, điều phối viên); `▸` giữ |

### `src/cfharness/tui/model.py`
| Hiện tại | Đổi sang |
|---|---|
| `STATUS_STYLE` (icon, label, style) | dựng từ `th.STATUS["agent"]`: `{k: (s.icon, k, s.style) for k, s in th.STATUS["agent"].items()}` (giữ nhãn tiếng Anh nếu muốn, hoặc `s.label` tiếng Việt); style `"grey62"` (idle), `"grey50"` (paused) → `"dim"` |
| `" cfharness top "` style `"bold reverse #7aa2f7"` | `th.BRAND` |
| `"bold white on red"` (approval chờ người, ESTOP BẬT, event lạ) | `th.ALARM` |
| `"#7aa2f7"` (firehose `run.started`) | `th.ACCENT` |
| `"grey42"`, `"grey50"`, `"grey62"`, `"grey70"` (header, ô task/tool, firehose) | `th.MUTED` |
| `"bold magenta"` / `"magenta"` (chi phí, học hôm nay) | `f"bold {th.COST}"` / `th.COST` |
| map kết thúc run `{"run.completed": "green", "run.cancelled": "grey62", "run.interrupted": "yellow"}` | `th.status("run", outcome).style` |
| `"cyan"` (tool đang chạy) | `th.INFO` |
| `"bold red"` / `"bold yellow"` / `"yellow"` (chờ duyệt, cảnh báo) | `th.ERR_STRONG` / `f"bold {th.WARN}"` / `th.WARN` |

Giữ nguyên ở TUI: `fmt_cost` (2–3 số lẻ, cột hẹp) khác `th.fmt_usd` (4 số lẻ, CLI) — có chủ ý. Textual tự xử lý màu nền nên
`ALARM` (`reverse red`) cho kết quả giống `white on red` trên nền tối và đúng hơn trên nền sáng.
