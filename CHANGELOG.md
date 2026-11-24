# Release notes — cfharness

Mỗi phiên bản là một tag `vX.Y.Z` (danh sách: [Releases](https://github.com/lephuong123/agent-hub/releases)).
Cập nhật hub đang chạy: `cfharness update` hoặc nút "Cập nhật hệ thống" ở trang Cài đặt web.

Quy tắc: **viết release note ở file này TRƯỚC khi phát hành** (xem mục "Phát hành" trong `AGENTS.md`).
Mục mới nhất ở trên cùng. Thay đổi đã gộp mà chưa có tag nằm ở mục "Chưa phát hành".

---

## Chưa phát hành

### Điều hướng tập trung vào trò chuyện
- **Mở web là vào thẳng trò chuyện với điều phối viên**: thanh bên chính gọn còn luồng việc hằng ngày (Cần bạn, Công việc, Lượt chạy, Đội ngũ, Hoạt động) và danh sách **Hội thoại** (luồng đang mở + hội thoại gần đây) hiện trên mọi trang. Trang Tổng quan đổi tên thành **Hoạt động** (`/activity`); link `/` cũ tự chuyển về trò chuyện. Các trang cấu hình (Team, Lịch chạy, Skill, Memory, Tự học, MCP, Hệ thống ngoài, Chi phí, Nhật ký, Nhật ký hệ thống, Quyền đã cấp) vào qua khối **Quản lý** ở đầu trang Cài đặt — đường dẫn giữ nguyên. Nút **Dừng khẩn cấp** luôn có ở chân thanh bên (bấm là hỏi xác nhận; đang bật thì có nút Tắt); khối "Bạn" ở chân thành menu: Cài đặt, Chi phí, Cập nhật (có chấm khi có bản mới), Tải app, Quản trị máy, Đăng xuất. Trên điện thoại: thanh dưới là Chat · Cần bạn · Công việc · Lượt chạy · Thêm (Dừng khẩn cấp đứng đầu), trang chat bớt một tầng thanh trên cùng.

### Luồng việc ngay trong trò chuyện
- **Theo dõi và duyệt việc của cả đội mà không rời khung chat**: khay **"N việc cần bạn"** ghim ngay trên ô nhập (cả ở hội thoại mới) liệt kê thao tác phá huỷ chờ duyệt và câu hỏi chờ bạn của mọi agent — cho phép/từ chối/trả lời tại chỗ với cùng bước xác nhận như trang Duyệt (thao tác nặng vẫn sang trang Duyệt). Khi điều phối viên giao task/chạy việc, dưới khối "Đã làm N việc" có **thẻ việc tự cập nhật**: agent, trạng thái, bước đang làm, kết quả khi xong, nút **Xem** (diễn biến trong panel trượt bên phải), **Nhắn thêm**, **Dừng**. Panel phải đổi thành **"Đội đang làm gì"** (việc cần bạn, agent đang làm, chi phí hôm nay); "Kiểm tra ngay"/"Huỷ chỉ định" gọn vào mục "Điều phối viên". Hội thoại mới cho biết bao nhiêu agent đang có việc/việc cần bạn và lại hiện các gợi ý câu hỏi; gợi ý phím tắt chỉ hiện khi đang gõ. **Tìm kiếm** nhóm kết quả theo hội thoại và mở đúng hội thoại (tô sáng tin tìm được); kết quả của lượt chạy thường vẫn mở lượt chạy. Bộ lọc **Lượt chạy → Chờ duyệt** giờ gồm cả lượt đang chạy có thao tác chờ duyệt.

### Giao diện dễ đọc hơn
- **Web dễ đọc hơn ở cả nền sáng lẫn tối**: nút chính, nút Dừng khẩn cấp/nguy hiểm ở nền tối, nhãn trạng thái (ok/cảnh báo/nhấn) và chữ phụ (gợi ý phím tắt, dòng lưu ý, giờ) giờ đủ tương phản (chuẩn WCAG AA); mục đang mở ở thanh bên có vạch màu bên trái; ô nhập và khung chat có viền rõ khi đang gõ; nút đóng thông báo và nút thêm hội thoại dễ bấm hơn; gợi ý phím tắt dưới ô chat gọn còn một dòng; thẻ "có bản cập nhật" dùng màu thông tin thay vì đỏ; trang Cài đặt trên điện thoại không còn bị tràn ngang khi email tài khoản dài.

### Trạng thái thống nhất & từ ngữ dễ hiểu
- **Một trạng thái cho mọi nơi**: agent hoặc lượt chạy đang chờ duyệt thao tác phá huỷ giờ hiện "Chờ duyệt" ở Lượt chạy, đầu trang lượt chạy, Agent, Tổng quan, panel điều phối viên và chế độ 3D (trước đây có nơi vẫn ghi "Đang chạy"); agent đang hỏi bạn hiện "Chờ bạn trả lời", agent hỏi điều phối viên hiện "Chờ điều phối viên". Số "Cần bạn" ở menu và ở chế độ 3D nay là cùng một con số (câu hỏi còn ở điều phối viên không tính). Bảng Công việc gộp "Mới / Cần làm / Sẵn sàng" thành "Chưa bắt đầu", cột trống thu thành dải hẹp nên cột "Xong" luôn thấy trên màn rộng; trên điện thoại là danh sách nhóm theo trạng thái (Bị chặn, Đang làm… Xong cuối). Trang Cần bạn đưa thao tác phá huỷ đang chặn agent lên đầu, câu hỏi trả lời bằng một chạm (gõ câu khác ở "Trả lời khác…"). Bớt từ kỹ thuật: tiêu đề lượt chạy không còn hiện prompt nội bộ tiếng Anh ("Tiếp tục sau khi bạn trả lời"…), mã `r_…`/`t_…` và mô hình gập vào "Chi tiết kỹ thuật", "steer" thành "Nhắn thêm", "Phụ thuộc (DAG)" thành "Thứ tự thực hiện", "Nhịp" thành "Kiểm tra định kỳ"; thẻ duyệt nói "vì sao cần duyệt" bằng lời thường trước tên luật.

### Gỡ giao diện Điều phối cũ
- **Giao diện Điều phối cũ đã được gỡ hẳn** — `/manager` chỉ còn trang Điều phối mới (ra mắt ở v0.2.31). Link `/manager?ui=old` không còn mở trang cũ: nó mở trang mới như bình thường, lựa chọn "giao diện cũ" trình duyệt từng nhớ cũng không còn tác dụng. Hub chưa có điều phối viên vẫn thấy phần hướng dẫn + chỉ định như trước; thẻ Điều phối ở trang Tổng quan không đổi.

### Sửa lỗi
- Không gian số (cổng cfharness): nhiều run ghi vào CÙNG một thư mục chung giờ chạy song song (trước đây run sau phải chờ run trước xong, kể cả khi sửa tệp khác nhau). Mỗi run vẫn có cây làm việc riêng; hai run cùng sửa một tệp → bản xung đột riêng, không ghi đè. Thay đổi một run vừa đẩy lên được kéo về cây của run kia và agent được báo là "run khác" sửa. Muốn chạy tuần tự như cũ: `cfharness config set filehub.lane.serialize_runs true`.

### MCP: hub thôi tự giữ cấu hình MCP bên thứ ba
- **MCP bên thứ ba chỉ còn do claude giữ theo tài khoản — hub thôi tự giữ cấu hình MCP.** Gỡ đường "nguồn hub": thêm/sửa lệnh, URL, header, env của server trong hub (`cfharness mcp add -- <lệnh>`, `--url`, `--env`, `--header`), nhập từ `~/.claude.json` (`mcp import`), danh mục MCP Registry + cài (`/mcp/browse`), kiểm kết nối (`mcp probe|test`, nút «Kiểm tra»), OAuth kiểu cũ (`mcp login|logout`, stub `mcp.oauth_*`, thẻ «Chuyển sang claude», `mcp migrate-to-claude`) và mục server bên thứ ba trong `mcp.json` của run. Registry chỉ còn giữ chính sách (tin cậy, include/exclude/luôn hỏi duyệt, ghi đè annotations, mô tả, host); claude nạp server từ cấu hình của tài khoản đang dùng (`--strict-mcp-config` bỏ cho run có MCP, guard bắt buộc). Thêm server: `cfharness mcp claude add <tên> --url … | -- <lệnh>` hoặc Cài đặt › Tài khoản Claude › MCP của tài khoản (rồi «Đăng ký cho hub» / `cfharness mcp add <tên>`). Server có header bí mật thêm được cả ở tài khoản «Đăng nhập của máy» (agent bị chặn đọc `~/.claude.json`). Chế độ Chrome vẫn gỡ Playwright khỏi run (nhận theo lệnh trong cấu hình claude). `cfharness doctor <agent>` kiểm MCP gán có trong cấu hình claude của tài khoản đang dùng thay cho probe.

### Lưu ý khi cập nhật
- Migration DB v34 đổi MỌI dòng MCP nguồn hub sang nguồn claude tại chỗ: giữ tên (agent đã gán giữ nguyên), tin cậy, tool include/exclude/luôn hỏi duyệt, annotations; bỏ lệnh/URL đầy đủ/header/env/OAuth (secret trong secret store giữ nguyên, vẫn được che). Server trỏ về chính hub bị xoá mềm.
- Server phải CÓ trong cấu hình claude của tài khoản đang dùng (`cfharness mcp claude status`), nếu không run của agent gán server đó báo lỗi rõ (không chạy thiếu server) và `hub doctor` cảnh báo. Server từng nhập từ `~/.claude.json` thường đã có sẵn; server thêm tay trong hub (lệnh/URL/OAuth) cần thêm lại bằng `cfharness mcp claude add` (+ `cfharness mcp claude login` nếu OAuth).
- Khoá cấu hình `mcp.probe_timeout_s` không còn dùng (config.yaml cũ vẫn đọc bình thường).

---

## v0.2.31 — 2026-11-24

### Trang Điều phối mới: đổi tab tức thì, không gián đoạn (#200)
- **`/manager` nay là trang Điều phối dựng lại bằng Preact.** Đổi qua lại luồng/hội thoại đã mở không tải lại trang và không nối lại luồng sự kiện: giữ nguyên chữ đang gõ (kể cả sau F5), tệp đính kèm và vị trí cuộn của từng tab. Đo trên hub demo: đổi sang tab đã mở ~210–310 ms → ~41 ms, mở tab lần đầu 50–130 ms.
- **Chạy chữ mượt**: câu trả lời hiện markdown ngay khi đang gõ (bảng, code, danh sách; link lọc qua DOMPurify) rồi thay bằng đúng bản đã lưu khi xong — nội dung không đổi sau khi tải lại. Cuộn "dính đáy" chỉ bỏ khi chính bạn cuộn lên, có nút "Tin mới nhất"; hội thoại 2.000 tin mở ~50 ms, tin cũ vẽ dần khi cuộn lên, không nhảy bố cục. Mở/đóng panel xem tệp không làm lệch chỗ đang đọc.
- **Duyệt và trả lời ngay trong hội thoại**: thẻ thao tác chờ duyệt có Cho phép một lần / Từ chối, trả lời câu hỏi không tải lại trang; kết quả hiện ngay, mạng lỗi thì trả về như cũ và bấm lại an toàn (không bị làm hai lần). Tin bạn gửi từ máy/tab khác hiện ngay ở mọi tab đang mở hội thoại đó. Dải cảnh báo khi ESTOP đang bật.
- **Một luồng sự kiện cho cả trang** (`/sse/manager/v2`, ảnh chụp `/api/manager/snapshot`): mỗi sự kiện có id, nối lại (mất mạng, máy ngủ) không mất/không trùng tin, tự tải lại khi hở quá xa; trạng thái mọi luồng đẩy tới ngay thay cho hỏi lại mỗi 20 s. Hợp đồng: `docs/dev-notes/dieu-phoi-island.md`.
- **Trang cũ cũng nhanh hơn** (vẫn mở được bằng `/manager?ui=old`, trình duyệt nhớ lựa chọn, `?ui=new` để quay lại): server chỉ vẽ phần lưới khi đổi tab, hàng tab cập nhật tại chỗ, chỉ làm mờ khi chờ quá 400 ms, "đã đọc" ghi bằng POST riêng, khung "đang trả lời" của lượt dài hiện đúng tool/checklist mới nhất.
- **Web nhẹ hơn**: tệp tĩnh có phiên bản được cache hẳn, trang/JS nén gzip; chấm "đang chạy" và nút mic nhấp nháy không còn vẽ lại cả khung hình. Thư viện JS (Preact, htm, signals, streaming-markdown, DOMPurify) đóng gói sẵn, ghim phiên bản, không tải từ CDN.
- RPC `events.query` nhận thêm `since`/`epoch`; event hub mới `manager.message` (tin người gửi cho điều phối viên). Hub demo: tin có `[demo]` cho câu trả lời chạy chữ ~100 token/s kèm tool, thẻ duyệt và câu hỏi.

### Run và daemon ổn định hơn (#201)
- **Run đã trả lời xong không còn bị tính quá giờ vì task nền**: khi một lệnh Bash chạy lâu bị Claude CLI chuyển sang chạy nền (vd `git fetch` treo vì mất mạng), CLI không thoát sau `result`, nên run bị giữ tới hạn `max_runtime_s` (30 phút) rồi ghi `timed_out` dù điều phối viên đã trả lời. Nay CLI im lặng quá `runtime.claude.exit_grace_s` (mặc định 60 s) sau `result` thì hub dừng tiến trình (gồm cả task nền) và giữ kết quả theo `result`.
- **Daemon bị treo thì để lại dấu vết**: thread watchdog mới phát hiện event loop của daemon không phản hồi quá `perf.loop_stall_dump_s` (mặc định 15 s; 0 = tắt) và ghi stack mọi thread vào `logs/faults.log` ngay lúc đang treo (tối đa 3 lần mỗi lần treo), kèm dòng `loop.stall` / `loop.stall_end` trong hub.log. Trên Windows trước đây không có cách nào lấy stack (`daemon dump-stacks` chỉ có trên macOS/Linux).

### Lưu ý khi cập nhật
- Không có migration DB hay cấu hình phải sửa tay (hai setting mới `runtime.claude.exit_grace_s`, `perf.loop_stall_dump_s` có mặc định). Sau khi cập nhật, tải lại tab web đang mở để nhận giao diện mới.
- Giao diện Điều phối cũ (`?ui=old`) sẽ bị gỡ ở bản phát hành sau.

---

## v0.2.30 — 2026-11-23

### Điều phối: tin không còn lẫn giữa các luồng (#193, #192)
- **Điều phối: tin của luồng này không còn hiện ở luồng khác.** Lượt nhịp không biết hội thoại gốc giờ chỉ báo/hỏi về Sảnh (hoặc trong chính luồng của việc), không đoán "cuộc người hoạt động gần nhất" ở luồng bất kỳ; áp dụng cả khi trả lời câu hỏi và khi lịch mất hội thoại chỉ định. Trang Điều phối không kéo thẻ hành động của hội thoại khác vào hội thoại đang mở, "Hành động gần đây" chỉ thêm thẻ đã qua bộ lọc, và chuyển luồng ngay sau khi gửi không còn làm hỏng nháp/URL của luồng mới. Script chỉ đọc `scripts/list_misrouted_notices.py` liệt kê tin cũ bị đoán sai.
- **Trang Điều phối — "Hành động gần đây" đúng và theo luồng**: trước đây panel đọc 3.000 sự kiện CŨ NHẤT của điều phối viên (nên hiện việc từ cả tuần trước); nay đọc thẳng các thẻ hành động mới nhất, chỉ của luồng đang xem (Sảnh = hành động không gắn luồng). TUI `manager` cũng đọc thẻ hành động mới nhất.

### Web và hub nhanh hơn, bớt khựng (#192, #196, #197, #195)
- **Web/TUI bớt khựng khi hub có nhiều dữ liệu**: daemon chạy SQLite ngay trên vòng sự kiện, nên một truy vấn chậm làm đứng mọi RPC và luồng SSE (đổi tab, chữ chạy của trả lời khựng 0,2–3,7 s trên hub.db 1,5 GB). Thêm index cho bảng `runs` (migration v32), danh sách run không còn tải cột `diff` (patch chỉ đọc khi mở một run), chi phí hôm nay của `agent.list` gộp một truy vấn, đếm sự kiện học của `top` dùng index, trang Điều phối chỉ hỏi trạng thái của điều phối viên (RPC mới `tui.agent_state`). Đo trên bản sao DB thật: `run.list` 3,8 s → 5 ms, `hub.status` 55 → 3 ms.
- **Truy vấn nặng không còn làm đứng cả hub**: các RPC chỉ đọc nặng (`run.list`, `events.query`, `task.list`, `learn.feed`, `learn.proposals`, báo cáo `report.*`, `approval.stats`, `audit.list|activity`, `system.list`) chạy trên luồng đọc riêng với kết nối SQLite chỉ đọc thay vì trên vòng sự kiện của daemon — trong lúc chúng chạy, RPC khác và luồng SSE (đổi tab, chữ chạy của trả lời) vẫn trôi. Đo trên bản sao hub.db 1,5 GB: vòng sự kiện bị chặn lâu nhất khi gọi `report.get` 1,2 s → ~20 ms, `learn.feed` 0,9 s → ~20 ms, `system.list` 2,6 s → ~40 ms; `hub.ping` chạy song song trả trong < 1 ms. Số đo mới `loop.lag` (độ trễ vòng sự kiện, trang `/perf` và `cfharness perf`) + cảnh báo `loop.lag` trong log khi vượt `perf.loop_lag_warn_ms` (mặc định 250 ms).
- **Chuyển trang và đổi luồng trên web nhanh hơn**: trang Điều phối hỏi daemon đồng thời thay vì lần lượt ~15 lần; số trên menu (duyệt, câu hỏi, ESTOP) được nhớ 2 giây theo phiên nên chuyển tab liên tục không hỏi lại (bấm nút nào cũng làm mới ngay); web giữ lại kết nối tới daemon thay vì mở mới mỗi lần; daemon nhớ ngắn hạn chuỗi tiến trình cha khi kiểm quyền người (Windows tốn 20–35 ms mỗi lần). Hub demo: `/manager` ~65 → ~26–36 ms, đổi luồng ~68 → ~23–28 ms, `/runs` 7 → 3 RPC.
- **Tab web ở nền không còn hỏi daemon**: các khung tự làm mới (Tổng quan mỗi 3 s, trạng thái điều phối viên mỗi 10 s, thanh run, cập nhật, chuyển hub, Slack chờ duyệt) ngừng gửi request khi tab bị ẩn và tải lại đúng một lần khi bạn quay về tab; cách hỏi dự phòng 15 s của bảng công việc (khi SSE mất) cũng vậy. Daemon phục vụ mọi request trên một vòng sự kiện nên tab nền để quên làm chậm cả người đang dùng.

### Đo hiệu năng (#191)
- **Đo hiệu năng — xem phần nào chậm** (bật sẵn, gần như không tốn): trang web mới **Hiệu năng** (`/perf`, menu Hệ thống) có bảng "chậm nhất" (p50/p95/max trong 5–60 phút) cho trình duyệt (tải trang, request htmx, chuyển tab/luồng, độ trễ SSE, long task), web (từng route) và RPC daemon (từng method); mỗi response web có header `Server-Timing` (devtools → Network → Timing: thời gian RPC, render); request chậm hơn `perf.slow_ms` (mặc định 1500 ms) ghi `http.slow` vào log web. Terminal: `cfharness perf`. Cấu hình `perf.enabled|slow_ms|window_min`. Chi tiết: `docs/dev-notes/perf-monitor.md`.

### Agent làm song song trên cùng repo: khoá đúng, worktree tự động, tự ghép nhánh, khoá theo file (#194)
Trước đây một agent sửa 1 file là cả repo (hoặc cả thư mục) bị khoá suốt run; đo trên một hub thật: ~10% lượt khởi chạy task phải chờ, ~38 giờ/4 tuần.
- **Sửa lỗi khoá**: task `worktree` kiểu cũ không còn khoá repo chính (task "running" mà run nằm hàng đợi); khoá của session không còn run sống tự được nhả (kể cả sau khi daemon khởi động lại); run chờ duyệt quá `guard.lock_waiting_ttl_s` (mặc định 4 giờ) nhả khoá; `extra_write_roots` giờ cũng được khoá; thư mục không phải git: thư mục cha và con xung đột đúng; không còn `bypassPermissions` khi cwd là checkout chính.
- **Lệnh mới** `cfharness workspace locks|unlock <path> [--force]` (chỉ người) và event `workspace.lock_acquired|lock_released|lock_blocked`.
- **Vòng đời worktree**: task lưu metadata worktree (nhánh, base, trạng thái `node_modules`); tên thư mục không trùng; cảnh báo `core.longpaths`; `node_modules` dùng junction/symlink khi lockfile giống checkout chính (`workspace.deps`); gỡ worktree an toàn junction. Lệnh `cfharness worktree list|clean [--dry-run]`.
- **Rule chung trong prompt**: hub chèn khối "Workspace rules" vào mọi run có quyền ghi (ưu tiên hơn SOUL cũ): ở worktree của hub hay ở checkout chính, không tự `git worktree add` trên checkout chính, không `git add -A`/`commit -a`. Tắt: `prompt.workspace_rules=false`.
- **Tự cô lập & tự ghép** (`workspace.isolate` = `never|auto|always`, mặc định `auto`; ghi đè theo team/task; kế hoạch lead + `task_create` có `isolate`): subtask ghi cùng repo được hub tách worktree riêng trên nhánh `cfh/<root>/<task>`, xong thì tự merge vào `cfh/<root>/integration` (xung đột lockfile/khoảng trắng tự xử; còn lại tạo task "Merge:"; không được thì chặn task gốc, báo người). Hub KHÔNG push; chỉ fast-forward checkout chính khi bật `workspace.integrate.fast_forward`.
- **Khoá theo file (tuỳ chọn)**: `guard.repo_lock: files` — nhiều agent sửa cùng repo, khác file; trùng file → agent đến sau bị từ chối ngay; file bị sửa ngoài → yêu cầu đọc lại; lệnh git/npm đổi trạng thái chung cần giữ repo độc quyền tạm thời; diff/revert của run chỉ gồm file nó đã giữ. Xem `cfharness workspace locks`.

### Lưu ý khi cập nhật
- Migration DB **v32** (thêm index cho bảng `runs`) tự chạy khi daemon khởi động; trên hub.db lớn (~1,5 GB) lần khởi động đầu có thể lâu hơn vài giây.
- Daemon cũ + web mới vẫn chạy (trang Điều phối tự quay về `tui.snapshot` khi daemon chưa có `tui.agent_state`), nhưng nên khởi động lại cả daemon lẫn web sau khi cập nhật để có đủ thay đổi.
- Muốn xem lại tin cũ bị đoán sai hội thoại trước bản này: `uv run python scripts/list_misrouted_notices.py --all` (chỉ đọc, không sửa dữ liệu).
- Migration DB **v33** (bảng `file_leases`, #194). Mặc định hành vi khoá giữ như cũ (`guard.repo_lock: repo`); `workspace.isolate: auto` chỉ tác động subtask của task gốc.
- Worktree cũ trong `_wt` không tự xoá: xem `cfharness worktree list`, dọn bằng `cfharness worktree clean`.
- Chi tiết thiết kế: `docs/dev-notes/workspace-parallel.md`.

---

## v0.2.29 — 2026-11-22

### Super Admin Console — quản trị mọi user, hub, tên miền từ `superadmin.<miền>` (#175, #177–#187)
- **Tiến trình mới `cfharness admind`** (root, LaunchDaemon) + **web SAC** (`cfharness superadmin serve`, user hệ thống `_cfh_sac`): đăng nhập 2 lớp (Cloudflare Access + mật khẩu/TOTP), vai trò Super Admin / Operator / Auditor / Team Lead, step-up TOTP cho thao tác quan trọng, Super Admin thứ hai duyệt thao tác tới hạn, nhật ký quản trị chuỗi băm.
- **Người dùng & hub**: nhập hub có sẵn, tạo user mới bằng wizard (thay `setup-hub-user.sh`, chạy từng bước, thử lại được), tạm khoá / mở khoá / thu hồi / khôi phục / xoá hẳn có sao lưu, khởi động lại / chẩn đoán / xem log / cập nhật (từng hub, hàng loạt, theo đợt), cấp lại TOTP, gỡ khoá đăng nhập, nhập CSV, hồ sơ 360°.
- **Tên miền**: tạo/thu hồi/đổi tên DNS qua API Cloudflare đúng zone + ingress tunnel có kiểm và tự quay lại khi lỗi, kiểm sức khoẻ 10 phút, phát hiện lệch.
- **Giám sát**: tổng quan, cảnh báo (gửi Telegram/Slack/email của quản trị), ESTOP toàn máy/user/nhóm/team (có hẹn tự gỡ), tài nguyên máy.
- **Báo cáo hằng ngày** (hub xuất lúc 23:59, gửi 08:00) + tuần/tháng; **Team user**: bảng việc, giao việc vào hub thành viên, Nhận/Từ chối/Nộp, duyệt/trả lại.
- **Chính sách** theo máy/mặc định/nhóm/team/user có phiên bản, mô phỏng, rollback; **kho tài khoản Claude** gán hàng loạt, xoay token.
- Tài liệu: `docs/dev-notes/superadmin.md` (triển khai), `docs/acceptance/superadmin.md` (nghiệm thu).

### Hub: ẩn quản trị với người dùng thường, việc được giao, báo cáo ngày (#181, #183)
- Người không thuộc nhóm quản trị **không còn thấy** "Tổng quan máy", "Người dùng máy", "Quản trị máy" (trang trả 404), lệnh `cfharness admin` và RPC `admin.*`. ESTOP do quản trị bật chỉ hiện "Hub đang tạm dừng — liên hệ quản trị" (không mã lý do).
- Trang **Việc được giao** (`/team-inbox`) khi máy có Team user: Nhận (giao cho agent/điều phối viên), Từ chối kèm lý do, Nộp kết quả. Tin Telegram/Slack khi có việc mới.
- `cfharness web unlock-login`: gỡ khoá đăng nhập sai mà không khởi động lại web.
- Hub có thư mục riêng ở `/Users/Shared/cfharness/status/<u>/` xuất thêm `profile.json` và `report/<u>/<ngày>.json` (mức chi tiết việc chỉ khi quản trị bật theo quy định nội bộ).

### Xem file ngay cạnh cuộc trò chuyện (panel trượt, #176)
- Bấm **Xem** ở file agent đã đọc/ghi giờ mở **panel trượt từ bên phải** (giống claude.ai) thay vì tab mới — chat / diễn biến lượt chạy vẫn ở bên trái. Đóng bằng × hoặc Esc; nút ↗ hoặc Ctrl/⌘+bấm để mở tab riêng như trước. Trên điện thoại panel phủ toàn màn.
- File **Markdown** (`.md`) hiện bản **đã trình bày đẹp** (tiêu đề, bảng, khối mã, checklist); nút **Mã nguồn** để xem lại mã thô có số dòng. Lựa chọn được nhớ cho lần sau.

### Sửa lỗi (#188)
- Super Admin: giao việc Team không còn lỗi quyền ghi hộp thư khi admind chạy không phải root (máy dev, CI); admind chạy root như bình thường thì không đổi.

### Lưu ý khi cập nhật
- Trang "Quản trị máy" (`/machine`) phía user mặc định **ẩn**; quản trị bật lại bằng công tắc minh bạch trên SAC (`visibility.transparency`).
- Muốn dùng SAC: làm theo `docs/dev-notes/superadmin.md` §"Triển khai" (cần sudo trên máy chủ). Không cài admind thì hub vẫn chạy như cũ.

---

## v0.2.28 — 2026-11-21

### Sửa lỗi (#173)
- Cập nhật trên macOS (launchd) không còn báo nhầm "khởi động lại daemon lỗi (mã 1)" khi daemon mới khởi động chậm vài giây: lệnh chờ daemon giờ thử lại khi daemon chưa kịp trả lời thay vì thoát lỗi.

### Gỡ OAuth + proxy MCP do hub tự giữ — OAuth MCP từ nay do `claude` giữ theo tài khoản (`docs/dev-notes/plan-mcp-claude-native.md` PR4 giai đoạn B, #174)
- Hub không còn tự đăng nhập OAuth cho MCP server, không làm mới token, không chèn `Authorization: Bearer` vào cấu hình MCP của run và không chạy proxy loopback nữa. Server cần đăng nhập OAuth thêm vào claude theo tài khoản: Cài đặt › Tài khoản Claude › **Thêm MCP** (hoặc `cfharness mcp claude add <tên> --account <id> --url <url>` rồi `cfharness mcp claude login <tên> --account <id>`). Trang **Danh mục MCP** với server cần OAuth chỉ hướng sang đó, không cài nguồn hub nữa; `cfharness mcp add --oauth` và ô "Đăng nhập OAuth" ở trang MCP báo lỗi hướng dẫn.
- `cfharness mcp login|logout`, nút **Kết nối / Ngắt kết nối** và RPC `mcp.oauth_*` đã gỡ — trong bản này chúng chỉ trả lỗi hướng dẫn chuyển sang claude.
- Server OAuth kiểu cũ (nguồn hub, `auth: oauth`) còn trong registry: trang MCP hiện nhãn **cần chuyển sang claude** kèm form **Chuyển sang claude**; run của agent gán server đó bị từ chối trước khi chạy với lỗi nêu tên server và lệnh chuyển (event `mcp.unavailable{status: needs_migration}`); `cfharness hub doctor` báo lỗi; daemon ghi một dòng cảnh báo lúc khởi động. Hub không tự xoá dòng hay token nào.
- Không đổi: MCP stdio và MCP http dùng header/secret tĩnh (nguồn hub) chạy như cũ.

### MCP do `claude` quản lý: che secret + đăng ký trên web (#174)
- Bảo mật: giá trị header/env/tham số/mật khẩu trong url của MCP do `claude` quản lý (`.claude.json` của cấu hình mặc định và mọi tài khoản, `remote-settings.json`) nay được che theo giá trị ở log, event, transcript run — kể cả khi server MCP in lại header trong thông báo lỗi; nạp lúc daemon khởi động và mỗi lần run bắt đầu (đọc lại khi file đổi).
- Web: ở Cài đặt › Tài khoản Claude › MCP của tài khoản, server có trong cấu hình claude mà hub chưa đăng ký có nút **Đăng ký cho hub** (tuỳ chọn **Tin cậy**, có xác nhận); server trỏ về chính hub không đăng ký được. Form agent gắn nhãn `claude` cho MCP nguồn claude.

### Lưu ý khi cập nhật
- Server MCP OAuth kiểu cũ **ngừng chạy** (agent gán nó bị từ chối) tới khi chuyển sang claude. Xem server cần chuyển: `cfharness hub doctor` (mục "MCP OAuth do hub giữ") hoặc trang MCP. Chuyển từng server: `cfharness mcp migrate-to-claude <tên> --account <id>` (web: trang MCP của server → **Chuyển sang claude**), rồi đăng nhập ở hàng tài khoản trong Cài đặt — hub giữ nguyên trust, tool chặn/luôn hỏi duyệt, nhãn tool và agent đã gán; token OAuth cũ bị xoá sau khi đăng nhập claude thành công. Không dùng server nữa: `cfharness mcp rm <tên>`. Khoá cấu hình `mcp.oauth_proxy.*` và `mcp_client.oauth_callback_port` không còn tác dụng (config.yaml còn các khoá này vẫn đọc bình thường, có thể xoá).
- Trên hub có server OAuth kiểu cũ: vào trang MCP của server đó, bấm **Chuyển sang claude**, chọn tài khoản, rồi đăng nhập ở Cài đặt › Tài khoản Claude › MCP của tài khoản (hoặc `cfharness mcp migrate-to-claude <tên> --account <id>`).

---

## v0.2.27 — 2026-11-21

### Connector claude.ai theo từng tài khoản Claude (#172)
- Connector claude.ai giờ **theo từng tài khoản Claude**: mỗi tài khoản trong Cài đặt › Tài khoản Claude có nút **Gắn connector** / **Gỡ connector** và hiện email claude.ai đã gắn; agent dùng connector (Google Drive, Gmail…) của tài khoản **đang dùng**, đổi tài khoản là đổi luôn bộ connector. Thêm tài khoản xong, bước "Gắn connector" gắn cho chính tài khoản vừa thêm. Terminal: `cfharness account connector <id> login|logout|status`. `cfharness hub doctor` tính cả `connectors.auto` và kiểm phiên của tài khoản đang dùng.
  - Lưu ý khi cập nhật: nếu trước đây bạn đã "Gắn connector" ở hàng **Đăng nhập của máy** nhưng hub đang dùng một tài khoản khác (vd "Mặc định"), agent tạm không dùng được connector — bấm **Gắn connector** trên hàng tài khoản đang dùng (đăng nhập claude.ai lại một lần).

### MCP do `claude` quản lý — hub chỉ giữ lớp an toàn (#172, `docs/dev-notes/plan-mcp-claude-native.md`)
- MCP server **do `claude` quản lý** (nguồn `claude`): server đã thêm vào cấu hình claude của tài khoản (`claude mcp add -s user …`, token do claude giữ) đăng ký vào hub bằng `cfharness mcp add --source claude <tên>` — hub chỉ giữ trust / include / exclude / require_approval / nhãn tool và gán cho agent, không giữ lệnh, URL đầy đủ hay secret. Xem server của tài khoản: `cfharness mcp claude-servers`. Run của agent có server loại này chạy như run connector (cần `guard.enabled`), chỉ thấy đúng server được gán; tài khoản đang dùng thiếu server → run báo lỗi rõ; server chưa đăng nhập → event `mcp.unavailable`. Tool của server nguồn claude không có nhãn thì hub suy theo tên (đọc / ghi / xoá) thay vì luôn hỏi; gắn nhãn tay bằng `cfharness mcp annotate`.
- Run đã bỏ `--strict-mcp-config` (connector claude.ai hoặc MCP nguồn claude) giờ gỡ khỏi ngữ cảnh agent MỌI server MCP mà claude tự nạp nhưng agent không được gán — kể cả server do **tổ chức claude.ai cấp** (`remote-settings.json`, `managed-mcp.json`) và server lạ đã gặp ở run trước — thay vì để tool của chúng hiện ra rồi mới bị guard chặn. Server tổ chức cấp cũng đăng ký được làm MCP nguồn claude.
- **Thêm + đăng nhập MCP qua `claude` ngay trên hub**: mỗi hàng ở Cài đặt › Tài khoản Claude có mục **MCP của tài khoản** — danh sách server do claude quản lý của tài khoản đó kèm trạng thái (đã kết nối / cần đăng nhập / lỗi kết nối), nút **Đăng nhập** (mở link, đồng ý cấp quyền, dán lại URL chuyển hướng), **Đăng xuất**, **Gỡ**, **Kiểm tra MCP** và form **Thêm MCP** (tên, URL, http/sse, header tuỳ chọn — giá trị không bao giờ hiện lại). Server thêm ở đây tự đăng ký vào hub (nguồn `claude`) để gán cho agent. Terminal: `cfharness mcp claude add|rm|login|logout|status [--account ID]` (giá trị header/env và URL chuyển hướng được hỏi khi chạy, không qua dòng lệnh). `cfharness hub doctor` nhắc server của tài khoản đang dùng cần đăng nhập hoặc bị thiếu. Header/env bí mật chỉ nhận cho tài khoản token (thư mục cấu hình riêng); với "Đăng nhập của máy", server cần secret tĩnh hãy thêm bằng `cfharness mcp add` (nguồn hub).
- **Chuyển MCP OAuth của hub sang `claude`**: `cfharness mcp migrate-to-claude <tên> --account <id>` (web: nút **Chuyển sang claude** ở trang MCP của server, chọn tài khoản) thêm server vào cấu hình claude của tài khoản, đổi server sang nguồn `claude` mà vẫn giữ trust, tool chặn/luôn hỏi duyệt, nhãn tool và agent đã gán, rồi mở lượt đăng nhập trên claude. Token OAuth cũ của hub chỉ bị xoá sau khi đăng nhập claude thành công (`--keep-hub-token` để giữ; `--drop-hub-token` xoá tay nếu bạn đã đăng nhập ngoài hub). Chỉ server OAuth mới chuyển; server dùng secret tĩnh (header/env) giữ nguồn hub. `cfharness hub doctor`, trang OAuth trên web và `cfharness mcp login|logout` nhắc: OAuth do hub tự giữ sẽ bị gỡ ở bản sau. Sau `hub import`, báo cáo và doctor liệt kê MCP nguồn claude phải thêm + đăng nhập lại trên tài khoản nào (cấu hình + token nằm ở máy cũ).

### An toàn (#171)
- Agent không còn đọc được cấu hình/đăng nhập của Claude Code — `~/.claude.json` (và các bản sao lưu `~/.claude.json*`, `~/.claude/backups/`), `.credentials.json`, thư mục `$CLAUDE_CONFIG_DIR` và `HUB_HOME/claude-config/` — vốn có thể chứa header/env bí mật của MCP và token OAuth. Chặn ở sandbox, luật quyền tool Read/Grep/Glob và bộ phân loại (`claude_credentials`, cấm thẳng), áp cho mọi agent kể cả agent được trust full.

### Điều phối viên đọc được PDF đính kèm trên máy không có poppler (#170)
- Trước đây PDF gửi cho điều phối viên (web, Telegram, Slack) có thể không đọc được: model hay truyền `pages` cho tool Read, mà cách đọc
  đó cần `pdftoppm` (poppler) — máy chưa cài thì báo "pdftoppm is not installed", và khung quyền strict không có cách khác.
- Hub giờ tự trích chữ của PDF đính kèm thành `<tên>.pdf.txt` (mỗi trang có tiêu đề `--- page N ---`, tối đa 500 trang / 8 MB). Dòng
  đầu tệp nêu phạm vi trang (vd `pages 1-13 of 400`) để điều phối viên không coi bản bị cắt là cả tài liệu. PDF dài hơn 50 trang còn được
  tách thành các phần `<tên>.pdf.p001-050.pdf`… để điều phối viên đọc từng phần mà vẫn thấy bảng, hình; tối đa 20 phần, hub tự nới tới
  100 trang/phần nên các phần phủ tối đa 2000 trang đầu (PDF dài hơn: phần sau chỉ có trong bản gốc). Tổng dung lượng các phần tối đa
  48 MB cho cả tin (chia đều cho các PDF của tin). Ghi chú gửi điều phối viên nêu tổng số trang, bản chữ và các phần phủ tới trang nào, và trang
  còn lại đọc thế nào (PDF quá 600 trang mà máy không có poppler: nói rõ phần sau không đọc được, không đoán). PDF scan, mã hoá có mật
  khẩu hoặc hỏng: hub nói rõ lý do không có bản chữ (PDF có mật khẩu: điều phối viên được dặn không thử Read mà xin người bản không
  khoá; mọi trang trích lỗi thì không bị gọi nhầm là PDF scan).
- Việc trích chạy trong tiến trình riêng có hạn giờ nên PDF lạ không làm treo hub. Các PDF của một tin được trích song song, cả tin tối
  đa `attachments.pdf.timeout_s` (mặc định 40 giây, dưới hạn chờ 60 giây của web). Hết giờ thì hub giữ các trang và phần đã trích xong.
  Web chờ tin có tệp tới 120 giây; nếu vẫn quá, web báo tin có thể đã được nhận thay vì lỗi 500 (gửi lại sẽ thành lượt trùng).
- Tin gửi trong lúc tin có PDF của cùng hội thoại đang được trích sẽ chờ tin đó, nên thứ tự tin giữ đúng.
- Telegram/Slack: tin có tệp được tải + trích trong tác vụ riêng của chat đó, nên trong lúc trích PDF bot vẫn xử lý chat khác, nút duyệt,
  E-stop và lệnh `/` ngay; tin sau của cùng chat xếp hàng sau tin có tệp.
- Tiến trình trích ghi chữ từng trang và các phần thẳng ra thư mục tạm cạnh tệp gốc rồi hub chuyển chỗ — daemon không giữ các phần trong bộ nhớ.
- Máy không có `pdftoppm`: mọi agent (kể cả run eval và takeover) được nhắc đọc PDF chỉ với `file_path` (không truyền `pages`). Hub chạy
  thử `pdftoppm -v`; bản hỏng bị coi như không có.
- Cài poppler để Read đọc được PDF theo trang: `scoop install poppler` hoặc `winget install oschwartz10612.Poppler` (Windows),
  `brew install poppler` (macOS), `apt install poppler-utils` (Linux). Poppler không nằm trên PATH thì đặt
  `cfharness config set tools.poppler_path <thư mục chứa pdftoppm>` — hub thêm thư mục đó vào cuối PATH của mọi run, có hiệu lực ngay.
  `cfharness hub doctor` có mục kiểm `pdftoppm`.
- Cấu hình mới: `attachments.pdf.split`, `attachments.pdf.split_pages` (mặc định 50, tối đa 100), `attachments.pdf.max_parts` (20) — sửa
  được ở Cài đặt → Điều phối viên; `attachments.pdf.timeout_s` (40 giây, 5–50); `tools.poppler_path` (chỉ qua `cfharness config set`,
  được kiểm toàn vẹn).

#### Lưu ý khi cập nhật (PDF)
- Phụ thuộc mới: `pypdf` (thuần Python, giấy phép BSD), khoá ở 6.21.4 (`>=6.0,<7`) — `cfharness update` / `uv tool install` tự cài.
- Cài poppler bằng scoop/winget khi daemon đang chạy: daemon chỉ thấy `pdftoppm` sau khi khởi động lại (`cfharness daemon stop` rồi
  `cfharness daemon start`), vì daemon giữ PATH lúc khởi động. Đặt `tools.poppler_path` thì không cần khởi động lại.
- Kéo–thả PDF trong TUI/REPL vẫn chỉ là đường dẫn trong tin (không có bản chữ, không tách phần); chỉ web, Telegram, Slack gửi tệp.

### Lưu ý khi cập nhật
- Connector: nếu trước đây bạn bấm "Gắn connector" ở hàng **Đăng nhập của máy** nhưng hub đang dùng một tài khoản token khác, bấm **Gắn connector** lại ở đúng hàng tài khoản đang dùng (Cài đặt › Tài khoản Claude).
- MCP OAuth do hub quản lý vẫn chạy như cũ; nên chuyển dần sang `claude` bằng `cfharness mcp migrate-to-claude <tên> --account <id>` (hoặc nút **Chuyển sang claude**). `cfharness hub doctor` liệt kê server nên chuyển. Code OAuth/proxy của hub sẽ gỡ ở một bản sau.
- Run dùng connector hoặc MCP nguồn claude giờ có thêm `--disallowedTools mcp__<server>__*` cho các server không gán — agent không còn thấy tool của MCP cá nhân / MCP tổ chức trong các run này.
- Agent không đọc được `~/.claude.json`, `~/.claude/.credentials.json` nữa (kể cả trust full) — agent nào từng dựa vào việc đọc file này cần đổi cách.

---

## v0.2.26 — 2026-11-20

### Connector claude.ai: mặc định bật, gắn ngay vào tài khoản (#169)
- Agent tự dùng mọi connector đã Connect trên claude.ai (Google Drive, Gmail…) của tài khoản đã gắn connector — **bật mặc định**, không còn công tắc trên web.
- Bỏ khối "Connector claude.ai" riêng: trạng thái nằm ngay trên hàng **Đăng nhập của máy** trong bảng Tài khoản Claude (email đang gắn, nút **Gắn connector** / **Đổi tài khoản connector** / **Gỡ connector**).
- Thêm tài khoản mới (đăng nhập hoặc dán token) xong thì hiện luôn bước **Gắn connector** cho tài khoản đó (đăng nhập claude.ai thêm một lần; có nút Bỏ qua).

#### Lưu ý khi cập nhật
- Máy chạy hub đã đăng nhập claude.ai (`claude /login`) cho user OS của hub: sau khi cập nhật, run của MỌI agent chưa chọn connector riêng tự nạp connector của tài khoản đó, và nạp kèm cấu hình `~/.claude` của user (hook, CLAUDE.md; MCP ngoài connector vẫn bị guard chặn, plugin user bị tắt). Agent ghi bằng tài khoản thật (gửi mail, sửa Drive…) không hỏi duyệt, trừ thao tác xoá. Không muốn: `cfharness config set connectors.auto false`, hoặc gỡ bớt connector trên claude.ai.

---

## v0.2.25 — 2026-11-20

### Người dùng tự đăng nhập claude.ai cho connector ngay trên web hub (#168)
- Cài đặt › Tài khoản Claude có khối **Connector claude.ai**: bấm "Đăng nhập claude.ai", mở link, đăng nhập tài khoản đã Connect Google Drive/Gmail… trên claude.ai, dán code — xong là agent dùng được connector, không cần admin vào máy chạy lệnh. Khối hiện tài khoản đang đăng nhập, nút đăng xuất và công tắc "Tự dùng connector cho mọi agent" (`connectors.auto`, bật cần xác nhận 2 bước).

#### Lưu ý khi cập nhật
- Đăng nhập chạy `claude auth login` dưới user OS của hub. Hub chạy nền (LaunchDaemon, user không đăng nhập màn hình) có thể không lưu được phiên vào Keychain — web sẽ báo "chưa thấy phiên claude.ai" thay vì báo thành công; khi đó đăng nhập bằng terminal của user hub (`sudo -u <user> -H claude` → `/login`).

---

## v0.2.24 — 2026-11-20

### Lịch — gửi kết quả tới nhiều chat riêng cùng lúc (#165)
- Một lịch gửi được kết quả tới tối đa 4 chat riêng (Telegram chat riêng, Slack DM) ngoài nơi nhận chính — vd báo cáo sáng về cả Telegram
  lẫn Slack: `cfharness schedule edit <id> --also telegram:<chat_id> --also slack:D…` (`--no-also` để bỏ; `schedule add` cũng nhận `--also`).
- Chỉ người đặt được (điều phối viên không thể tự thêm chat nhận); chat không còn trong allowlist của bot thì không nhận. Trang lịch trên web
  hiện các kênh gửi thêm (chỉ xem).

### Daemon — không còn lặp "thoát 1" sau `cfharness update` trên macOS (#166)
- Đã cài tự khởi động (LaunchAgent) thì `cfharness update` và lệnh tự khởi động daemon nhờ launchd chạy daemon (`launchctl kickstart`, giữ
  đúng `--home`) thay vì bật một bản ngoài launchd. Trước đây bản ngoài launchd giữ `daemon.lock`, job launchd thua khoá, thoát 1 và bị
  launchd chạy lại vô hạn (hàng nghìn lần/ngày). Job launchd lỡ thua khoá giờ chờ khoá (thử lại mỗi 30 giây) và tiếp quản ngay khi bản kia dừng.
- Phát hiện và sửa bởi team agent giám sát của hub.

#### Lưu ý khi cập nhật
- Hub đang chạy daemon NGOÀI launchd (kiểm: `launchctl print gui/$(id -u)/com.cfharness.daemon.<…>` thấy `state = spawn scheduled`, `runs`
  rất lớn): lần cập nhật này vẫn đi qua đường cũ. Sau khi cập nhật xong, đưa daemon về launchd một lần:
  `cfharness daemon stop && launchctl kickstart -k gui/$(id -u)/com.cfharness.daemon.<…>` (khi không có run đang chạy).

---

## v0.2.23 — 2026-11-20

### Agent tự dùng connector đã Connect trên tài khoản Claude (#164)
- `cfharness config set connectors.auto true`: mọi agent chưa đặt connector riêng tự dùng MỌI connector (Google Drive, Gmail, Docs…) mà tài khoản claude.ai đăng nhập `claude /login` của hub đã Connect — không phải khai từng agent. Chỉ bật khi hub có phiên `/login` (token setup-token không nạp connector). Không áp cho điều phối viên và run kỹ thuật (review/eval/curator). Guard vẫn áp: đọc cho qua, ghi được ghi nhật ký, xoá phải duyệt; MCP lạ của `~/.claude` vẫn bị chặn.
- `--connectors all` cho một agent (hoặc điều phối viên: `manager capabilities set --connectors all`) = mọi connector đã Connect.
- `scripts/setup-hub-user.sh`: `CONNECTORS=auto` (đăng nhập `/login` + bật `connectors.auto`); `CONNECTOR_AGENTS=all` bật connector cho mọi agent; connector được GỘP với connector agent đang có thay vì ghi đè. (#163)

#### Lưu ý khi cập nhật
- `connectors.auto` tắt mặc định — không đổi gì cho đến khi người bật. Khi bật, agent ghi bằng tài khoản thật (Gmail, Slack…) không hỏi duyệt trừ thao tác xoá: chỉ Connect trên claude.ai những connector muốn agent dùng.

---

## v0.2.22 — 2026-11-20

### Connector claude.ai trên hub dùng token chung (#162)
- Agent/điều phối viên bật connector claude.ai (`--connectors "Google Drive"`…) giờ dùng được connector trên hub đăng nhập bằng token `claude setup-token`: token này không nạp connector nào, nên run có connector tự chạy bằng phiên `claude /login` của user OS khi có (`runtime.claude.connectors_auth`: `auto` mặc định, `login`, `token`). Chưa đăng nhập `/login` → run vẫn chạy bằng token như cũ và có event `connector.auth_fallback`; connector chưa nối trên claude.ai (vd Google Drive `needs-auth`) → event `connector.unavailable` thay vì im lặng. `cfharness hub doctor --probe` có thêm mục "connector claude.ai". Hướng dẫn: `docs/dev-notes/manager-connectors.md`.
- `scripts/setup-hub-user.sh` thêm bước nối connector: `CONNECTORS="Google Drive" CONNECTOR_AGENTS="<agent…>"`; hub đã có thì `sudo ONLY_CONNECTORS=1 CONNECTORS="Google Drive" CONNECTOR_AGENTS="<agent>" bash scripts/setup-hub-user.sh <user>` chỉ nâng cfharness, đăng nhập `claude auth login` cho user hub và bật connector (không đụng token, tunnel, TOTP).

#### Lưu ý khi cập nhật (connector)
- Hub đã bật connector và user OS của hub CÓ phiên `claude /login`: run có connector giờ chạy bằng tài khoản của phiên đó (hạn mức/chi phí tính vào tài khoản đó, không theo `cfharness account use`). Muốn giữ token chung như cũ: `cfharness config set runtime.claude.connectors_auth token`.

### Web — bật connector claude.ai cho từng agent (#161)
- Form sửa/tạo agent có phần **Connector claude.ai**: tick connector (gợi ý từ danh sách của điều phối viên, connector đang bật và connector
  mà `allowed_tools` cần) hoặc gõ tên khác. Đổi connector phải xác nhận (agent ghi bằng tài khoản thật). Trước đây chỉ đặt được bằng
  `cfharness agent edit <id> --connectors`.
- Trang agent và form báo đỏ khi `allowed_tools` cho gọi `mcp__claude_ai_X__*` mà connector X chưa bật. Đây là nguyên nhân jira-ops và
  jira-analyst không thấy Atlassian MCP (run khởi động với `--setting-sources ""` + `--strict-mcp-config` nên không nạp connector).

### Log hệ thống và nhật ký kiểm toán — `docs/dev-notes/plan-logging.md` (#150, #153, #155, #156, #157, #158, #159, #160)
- Web và `cfharness mcp serve` giờ ghi log JSON có che secret như daemon (`logs/web.jsonl`, `logs/mcp.jsonl`); mỗi dòng log có thêm tiến trình, pid và vị trí mã. Log của thư viện (asyncio, mcp, uvicorn…) vào cùng file thay vì chỉ ra stderr; cảnh báo không còn bị ghi hai lần (`hub.log` + `daemon.out`).
- `cfharness logs level` xem level log đang áp dụng; `cfharness logs level cfharness.guard debug --for 30m` bật DEBUG tạm cho một phần hub, tự về mức cũ khi hết giờ — không cần khởi động lại daemon. Đổi lâu dài: `cfharness config set logs.level …` (có hiệu lực ngay).
- Lỗi trước đây chết im lặng giờ được ghi lại: tác vụ nền lỗi, lỗi trong thread, lỗi không ai bắt, listener event lỗi (không còn làm hỏng việc phát event). Daemon treo: `cfharness daemon dump-stacks` ghi stack mọi thread vào `logs/faults.log`.
- `web.log`, `update.out`, `update.log`, `import-job.out` được xoay vòng khi quá `logs.max_bytes`.
- Mỗi lệnh (CLI, web, TUI, MCP, Telegram/Slack, lịch, điều phối viên) có một mã yêu cầu `r_…` đi theo suốt: dòng audit, run, event, quyết định guard và log đều mang cùng mã. Lỗi từ CLI in kèm `(mã yêu cầu r_…)` để tra trong `logs/hub.log`. `hub.log` có thêm một dòng cho mỗi lời gọi RPC (method, thời lượng, kết quả — không có nội dung); lời gọi chậm hơn `logs.slow_rpc_ms` (mặc định 2000 ms) được cảnh báo `rpc.slow`. Daemon cập nhật lên DB phiên bản 28 (chỉ thêm cột) (L2).
- Nhật ký kiểm toán đầy đủ hơn (L3): mỗi dòng ghi **ai** làm do daemon xác định (`principal`: `os:<user>` cho CLI/TUI, `web:dev_…`/`web:sess_…` cho từng thiết bị/phiên web — chỉ là mã băm, `telegram:<id>`, `slack:<id>`, `mcp:stdio`/`mcp:http:<mức token>`, `manager:<agent>`, `agent:<id>`), **kết quả** (`ok`/`error`/`denied` + mã lỗi) và **thời lượng**. Thao tác bị từ chối hoặc lỗi giờ cũng để lại dòng (trước đây chỉ ghi khi thành công). Web dùng token riêng nên kênh `web` không còn giả được từ client khác (kênh client tự khai lưu riêng ở `declared_channel`); qua link public có thêm ip. Lượt đọc dữ liệu nhạy cảm (danh sách secret, transcript run, tìm hội thoại/memory, graph memory, quyết định guard, đọc audit, `hub doctor`, level log) được ghi `read:<method>` (chỉ đích + số bản ghi, không nội dung; gộp mỗi 10 phút). Gỡ mẫu chặn repo (`guard repo-deny rm`) có dòng audit; dòng trust/grant/sandbox ghi đúng người và kênh. `cfharness audit list` thêm cột kết quả + principal; đọc audit giờ chỉ người (agent bị chặn).
- Công cụ debug (L6): `cfharness logs --since 2h --level warning --logger cfharness.guard --req r_… --run … --proc web` lọc log theo trường (mọi tiến trình, kể cả file đã xoay vòng và `.gz`), `-f` theo dõi dòng mới; daemon dừng vẫn đọc được file. `cfharness trace <mã yêu cầu|run|task>` ghép một dòng thời gian: audit, log, event (chỉ tóm tắt), quyết định guard, phê duyệt và run con. Web có trang "Nhật ký hệ thống" (`/logs`) và trang dòng thời gian (`/trace/<mã>`); bấm mã yêu cầu ở Nhật ký kiểm toán hoặc "Dòng thời gian" ở trang run. Thẻ Sức khoẻ hiện số lỗi/cảnh báo trong log 1 giờ/24 giờ theo logger, số RPC chậm và lỗi không ai bắt; `hub doctor` thêm mục "Lỗi gần đây" (top 5 lỗi 24 giờ, kèm mã yêu cầu mẫu). Agent không còn đọc được `HUB_HOME/logs/`.
- Nhật ký kiểm toán chống sửa (L4): mỗi dòng audit mới được nối vào một chuỗi băm (sha256); hub định kỳ (mỗi giờ, `audit.anchor_interval_s`) và khi daemon dừng ghi "bản neo" của dòng cuối vào `audit-anchors.jsonl` (và vào file metadata của quản trị máy khi bật xuất trạng thái admin). `cfharness audit verify [--since-id N] [--json]` kiểm toàn bộ chuỗi + bản neo, báo đúng dòng bị sửa/xoá; ai sửa DB rồi tính lại cả chuỗi vẫn bị bản neo bắt. Phát hiện sửa/xoá (kể cả trong lúc daemon dừng — daemon kiểm phần đuôi khi khởi động) → cảnh báo `audit.tamper_detected` qua Telegram/Slack. Gửi bản neo ra Telegram/Slack để giữ bằng chứng ngoài máy: `cfharness config set audit.anchor_notify true` (tắt mặc định). Bản neo đi cùng bản sao lưu và gói xuất hub nên khôi phục/nhập không báo nhầm.

- Truy vấn audit + cảnh báo (L5): `cfharness audit list` lọc thêm `--principal --actor --outcome denied --req r_… --since 24h --until … --q <chữ>` (`--q` chỉ tìm trong tóm tắt/đích, không bao giờ trong dữ liệu) và phân trang `--before-id N`; `cfharness audit export --since 7d [--format jsonl|csv]` ghi `HUB_HOME/exports/audit-<giờ>.<đuôi>` (0600, kèm cột băm để kiểm lại). Trang web "Nhật ký kiểm toán" có các bộ lọc này, cột kết quả có màu, nút "Cũ hơn" và thẻ "Hoạt động theo người" (số dòng theo danh tính × ngày, 7 ngày, kèm số lần bị từ chối). Hub tự cảnh báo `audit.alert` qua Telegram (nhóm bảo mật) / Slack khi thấy mẫu đáng ngờ: ≥ 5 thao tác bị từ chối / 10 phút cùng một danh tính, ≥ 5 lần đăng nhập web sai / 15 phút (theo IP; toàn cục khi từ nhiều IP), đổi cấu hình an toàn (`gates.mode`, `approval.profile`, `guard.enabled`, `guard.sandbox.*`, `workspace.confine`, `audit.*`, `audit_watch.*`, bật trust full, cấp quyền theo luật, ngoại lệ sandbox), danh tính mới lần đầu xuất hiện (thiết bị web tin cậy, user Telegram/Slack, token MCP…), ≥ 30 lượt đọc dữ liệu nhạy cảm / 10 phút, nhật ký/chính sách bị sửa ngoài hub; truy cập secret ngoài giờ khi đặt `audit_watch.secret_access.hours` (vd `"08:00-19:00"`). Mỗi cảnh báo chỉ báo một lần cho tới khi hết, và có tin "Hết cảnh báo" khi qua trọn cửa sổ không còn lặp lại. Ngưỡng ở `audit_watch.*` (chỉ người đổi, được kiểm toàn vẹn). Tuỳ chọn lưu trữ audit cũ (tắt mặc định — audit vẫn giữ vĩnh viễn trong DB): `cfharness config set audit.archive_after_days 365` dồn các tháng đã trọn ra `HUB_HOME/audit-archive/<yyyy-mm>.jsonl.gz` (giữ chuỗi băm, có `MANIFEST.json`) rồi mới xoá khỏi DB; `audit verify` vẫn kiểm được chuỗi, `audit verify --archive` còn kiểm từng file lưu trữ. Agent không đọc/ghi được `audit-archive/`.
- Gói chẩn đoán (L7): `cfharness support bundle [--since 24h] [--run <id> …]` in danh sách file + cỡ, hỏi xác nhận rồi ghi `HUB_HOME/exports/support-<giờ>.tar.gz` (0600) để gửi người hỗ trợ khi gỡ lỗi: log hệ thống trong khoảng thời gian chọn (cả file đã xoay vòng), `hub doctor`, trạng thái hub, phiên bản (cfharness, claude, python, OS), cấu hình đã lọc (khoá nhạy cảm bị che, nhánh `web` chỉ giữ bật/tắt), dòng thời gian (chỉ metadata) của các run chọn, và `MANIFEST.json` (sha256 từng file + số chỗ đã che theo file). Mọi byte được quét che secret (theo giá trị + mẫu token) ngay trước khi ghi; không bao giờ có hub.db, secret, đăng nhập web, token, session Claude, checkpoint, bản sao lưu, memory/skill. Raw transcript chỉ khi thêm `--include-raw` và xác nhận bước 2 (gõ `raw`; `--yes` không bỏ qua bước này). Trần `support.max_mb` (mặc định 200) và `support.max_file_mb` (50). Daemon dừng vẫn tạo được. Web: thẻ "Gói chẩn đoán" ở Cài đặt — xem trước, tạo (raw cần bước 2), tải về bằng link dùng một lần. `update.log` và `import-job.out` ghi dòng JSON cùng dạng `hub.log` (trang cập nhật vẫn hiện chữ như cũ). Agent không đọc được `exports/`, `imports/`, `backups/` kể cả qua lệnh shell.
- Ít lỗi chết im lặng hơn (L8): khoảng 300 chỗ hub bắt lỗi rồi bỏ qua giờ hoặc ghi log kèm traceback (DEBUG cho việc phụ, WARNING cho lỗi đáng ngờ như kiểm tài nguyên bảo vệ của workspace, eval gate lỗi hạ tầng, đẩy cây lane lỗi), hoặc được đánh dấu rõ là cố ý (`fail-closed` của guard: lỗi = chặn; `best-effort`). Bật `cfharness logs level cfharness debug --for 30m` để thấy cả lỗi phụ khi gỡ rối. Lệnh dừng run (timeout, ESTOP, dừng tay, chờ người) và vài tác vụ nền khác giờ ghi ERROR có tên tác vụ nếu lỗi thay vì mất dấu. Không đổi hành vi chặn/cho phép của guard.

#### Lưu ý khi cập nhật (L5)
- Daemon nâng DB lên phiên bản 31 (chỉ thay trigger `audit_no_delete`: vẫn chặn xoá mọi dòng audit, trừ dòng đã chuyển ra `audit-archive/` khi bạn bật `audit.archive_after_days`). Lần đầu chạy sau cập nhật, hub nạp danh sách danh tính đã thấy từ audit sẵn có nên không cảnh báo "danh tính mới" cho những danh tính cũ. Danh sách `notify.<kênh>.events` đã sửa tay sẽ không tự có `audit.alert` — thêm vào nếu muốn nhận cảnh báo.

#### Lưu ý khi cập nhật (L4)
- Daemon nâng DB lên phiên bản 30 (thêm `prev_hash`/`row_hash` vào `audit_log`). Dòng audit cũ để trống và không được kiểm; chuỗi bắt đầu từ dòng đầu tiên sau khi cập nhật. Danh sách `notify.<kênh>.events` đã sửa tay sẽ không tự có `audit.tamper_detected` — thêm vào nếu muốn nhận cảnh báo.

#### Lưu ý khi cập nhật (L3)
- Daemon nâng DB lên phiên bản 29 (chỉ thêm cột vào `audit_log`, dòng cũ để trống).
- **Khởi động lại web (`cfharness web`) và `cfharness mcp serve` sau khi cập nhật daemon** để dùng token riêng mới (`<socket>.web.auth`, `<socket>.mcp.auth`). Web/MCP chạy từ trước vẫn hoạt động nhưng được ghi là `os:<user>` với kênh `cli` (kênh tự khai nằm ở `declared_channel`).

---

## v0.2.21 — 2026-11-18

### Lưu ý khi cập nhật
- **Đồng bộ 2 chiều cũ với Không gian số đã bỏ** (tiến trình `tang-thu-sync-headless`, đăng nhập thiết bị, bản sao `~/KhongGianSo/…`):
  thư mục chung giờ CHỈ đi qua cổng cfharness. Hub còn thư mục đồng bộ cũ thì sau khi cập nhật thấy "Thư mục X đang dùng đồng bộ cũ
  (đã bỏ) — dán khoá cổng cfharness ở Cài đặt để dùng tiếp; bản sao trên máy để nguyên" (Cài đặt › Không gian số, `cfharness filehub
  status`); run/task giao vào thư mục đó bị từ chối với cùng câu. Dán khoá cổng (tạo ở Không gian số › Hồ sơ › Cổng cfharness, cấp đúng
  thư mục/workspace đó) là hub tự chuyển thư mục sang cổng — giữ tên `filehub:<tên>`. Hub KHÔNG xoá gì: bản sao trên máy, `HUB_HOME/filehub/`
  để nguyên (kiểm rồi tự xoá); thu hồi thiết bị đồng bộ cũ ở Không gian số › Hồ sơ › Thiết bị. Tiến trình đồng bộ còn chạy từ bản cũ được
  hub dừng khi khởi động. Khoá cấu hình cũ (`filehub.sync_binary`, `local_parent`, `device_name`, `mode`…) không gây lỗi, được bỏ qua.
- Lệnh `cfharness filehub setup|login|add|pause|switch|switch-to-workspace` không còn (chỉ in hướng dẫn); `filehub resume` cần `--root`
  (lô xoá đang giữ của cổng). `scripts/setup-hub-user.sh`: `KGS=…` luôn đi qua cổng (biến `KGS_MODE`, `KGS_SCOPE`… vẫn nhận, bỏ qua).
- Không có migration DB.

### Không gian số — chỉ còn cổng cfharness (H5, #154)
- Gỡ đồng bộ 2 chiều cũ: tiến trình giám sát binary sync, đăng nhập thiết bị + relay OAuth qua web public (`/filehub/oauth/cb`, trang
  `/filehub/login`), bản sao cục bộ, chặn xoá hàng loạt của binary, chuyển chế độ sync ↔ cổng. Thẻ Cài đặt gộp thành một thẻ "Không gian số —
  cổng cfharness" (thêm đổi tên / gỡ thư mục, báo thư mục cần chuyển).
- Thư mục đồng bộ cũ tự chuyển sang cổng khi khoá được cấp đúng gốc (khi dán khoá, "Thử kết nối" hoặc kiểm tra nền mỗi phút).
- Link "Mở trên Không gian số" (chip tệp ở diff của run, tool `filehub_link` của điều phối viên) dùng id do cổng trả.
- Nhập gói hub cũ (`hub import`) còn cấu hình đồng bộ: bỏ khoá của đồng bộ, thư mục thành "cần chuyển" (`move`) / gỡ (`clone`);
  `hub doctor` báo thư mục cần chuyển và cổng chưa có khoá.
- Hub chỉ dán khoá cổng (chưa đặt `filehub.server`) giờ có link "Mở trên Không gian số": cổng báo địa chỉ web công khai trong `hello`
  (`server.public_url`, cần Không gian số bản mới — bản cũ không gửi thì như trước). Hub kiểm chặt (chỉ https, không đường dẫn/userinfo), tự
  đặt `filehub.server` khi còn trống và cập nhật nếu cổng đổi địa chỉ; giá trị người đặt không bao giờ bị ghi đè; nhiều cổng → link mỗi cổng
  về web của chính nó.

---

## v0.2.20 — 2026-11-18

Hoàn thiện cổng cfharness: hoàn tác run trên Không gian số, nút duyệt lô xoá trên Telegram/Slack, mọi run đọc được thư mục chung qua cổng,
chặn run đọc/ghi cây làm việc của run khác; sửa test chập chờn của nhịp điều phối viên (#151).

### Lưu ý khi cập nhật
- Không có migration DB của hub. Cổng cfharness giờ có trên cả hai hệ Không gian số (bản chính cổng 8223, bản CCHC cổng 8233) — hub nối
  CCHC dán khoá cổng là chuyển được (hub tự thử 8223 rồi 8233).
- Mặc định mọi run đọc được thư mục chung chế độ cổng; muốn tắt: `cfharness config set filehub.lane.read_all_runs false`.

### Không gian số — cổng cfharness (#151)
- Cổng cfharness: hoàn tác run làm việc trên thư mục chung (`cfharness run revert <run>`, nút "Hoàn tác trên Không gian số" ở trang run) —
  khôi phục phiên bản trước run, lấy lại tệp run xoá từ thùng rác, chuyển tệp run tạo vào thùng rác, đổi tên lại; từ chối khi tệp đã bị
  sửa tiếp sau run. Run cũ (trước bản này) vẫn chỉ khôi phục được trên Không gian số.
- Telegram/Slack: nút "Cho xoá"/"Không xoá" cho lô xoá hàng loạt đang giữ của cổng cfharness (2 bước, bước 2 liệt kê danh sách hiện tại).
- Cổng cfharness: mọi run (kể cả run không làm trong thư mục chung) đọc được thư mục chung chế độ cổng qua tool đọc `fs_list`/`fs_read`…
  (tắt: `filehub.lane.read_all_runs: false`); run không đọc/ghi được cây làm việc của run khác; dải "cfharness đang làm việc" trên Không gian
  số vẫn giữ khi run chờ bạn duyệt.

### Điều phối viên
- Sửa lỗi nhịp đầu tiên của điều phối viên đôi khi bị bỏ lượt (mốc thời gian lệch mili-giây) (#151).

---

## v0.2.19 — 2026-11-18

Cổng cfharness: agent làm việc thẳng trên Không gian số cùng máy, realtime hai chiều, thay đồng bộ 2 chiều thường trực — dán khoá là dùng được
ngay (#147, #148). Kèm sửa bản sao ngoài vùng giam (#141), `cfharness update` tự chạy lại web (#144), gỡ MCP đã xoá (#145), `RESET=1` (#146).

### Lưu ý khi cập nhật
- Không có migration DB của hub. Cấu hình mới đều có mặc định; hub đang dùng đồng bộ cũ (`filehub.mode: sync`) **không đổi gì** cho tới khi
  người dán khoá cổng.
- Muốn dùng cổng: Không gian số phải là bản có cổng cfharness (đã deploy 2026-11-18, cổng `127.0.0.1:8223`) và Super Admin bật "Cho phép
  cổng cfharness" ở Hệ thống › Chính sách. Sau đó Workspace Admin tạo khoá ở Hồ sơ › Cổng cfharness → dán vào cfharness › Cài đặt › Cổng
  cfharness. Bản sao `~/KhongGianSo/<tên>` của đồng bộ cũ được để nguyên — kiểm rồi tự xoá khi đã chuyển.
- Hệ Không gian số CCHC (cổng 8233) chưa có cổng cfharness — hub nối CCHC tiếp tục dùng đồng bộ cũ.

### Cài đặt
- `scripts/setup-hub-user.sh` thêm `RESET=1`: làm lại hub của một user từ đầu — dừng dịch vụ, CẤT (không xoá) dữ liệu hub, Workspace, session Claude cũ sang `~/.reset-<giờ>`, hỏi gõ "reset" để xác nhận (#146).

### Không gian số (thư mục chung)
- Hub đang giam agent trong một workspace (`workspace.confine`) mà thư mục cha của bản sao (`filehub.local_parent`, mặc định `~/KhongGianSo`) nằm ngoài vùng giam → bản sao tự đặt ở `<vùng giam>/KhongGianSo/<tên>` thay vì báo "nằm ngoài workspace được giam" (nối thư mục qua web public không sửa được đường dẫn nên bị kẹt). `filehub setup --local-parent` ngoài vùng giam bị từ chối ngay, không ghi cấu hình (#141).

### Không gian số — cổng cfharness (chế độ `lane`, chưa bật mặc định) (#147, #148)
- `cfharness filehub lane setup --url http://127.0.0.1:8223` (khoá hỏi bằng getpass, chỉ lưu trong secret store), `lane status|test|disable`:
  nối hub với cổng cfharness của Không gian số trên cùng máy — chỉ nhận URL loopback, khoá không bao giờ vào cấu hình/nhật ký/gói nhân bản.
  Hub đang dùng đồng bộ cũ không đổi gì cho tới khi người chuyển thư mục sang `lane`.
- Run trên thư mục chế độ `lane` (`filehub:<tên>[/<con>]`) có cây làm việc riêng dựng từ Không gian số lúc bắt đầu (đúng phạm vi ref) và
  xoá khi run xong; người sửa/xoá/di chuyển tệp trong phạm vi → hub kéo về và nhắn agent ngay (thường < 1 s). Run chỉ đọc hoặc phạm vi quá
  lớn: cây rỗng + tool `fs_list|fs_read|fs_stat|fs_versions|fs_link|fs_pull`. Agent không bao giờ gọi được cổng trực tiếp (bị chặn).
- Agent lưu tệp trong cây → lên Không gian số trong vài giây thành phiên bản mới (không bao giờ ghi đè bản người vừa lưu: giữ bản của agent
  thành tệp `<tên> (cfharness <agent> <giờ>)`); tệp đang bị người khoá → xếp hàng, mở khoá là lưu. **Tệp chứa giá trị secret của hub bị
  chặn TRƯỚC khi đẩy** (không một byte nào rời máy, agent được báo). Hết run còn thay đổi chưa đẩy được → giữ ở cây mồ côi
  (`cfharness filehub lane orphan push|drop <run>`, tự dọn sau 7 ngày). Diff của run lấy từ nhật ký thao tác; hoàn tác ở phiên bản/thùng rác
  Không gian số.
- Chuyển từng thư mục: `cfharness filehub switch <tên> --to lane|sync` (từ chối khi đồng bộ còn dở / gốc chưa được cấp cho khoá / có run
  đang ghi) — bản sao `~/KhongGianSo/<tên>` cũ **để nguyên** (hub không xoá), được báo ở `filehub lane status` và thẻ web. Thư mục mới:
  tự thêm khi dán khoá (xem dưới); ca tay `cfharness filehub lane add <id gốc>`. Thẻ "Cổng cfharness" ở Cài đặt web (khoá chỉ nhập ở bước 2). Trên Không gian số thấy dải
  "cfharness · <agent> · đang sửa <tệp>" trong lúc run ghi. `setup-hub-user.sh`: `KGS_MODE=lane`.
- An toàn hơn khi agent làm việc trên thư mục chế độ `lane`: hub không bao giờ đi xuyên symlink agent đặt trong cây (xoá/di chuyển/đọc để
  đẩy đều từ chối); tệp tạm/khoá Office/`.git` của người trên Không gian số không bao giờ bị xoá; agent xoá tệp mà người vừa lưu bản mới →
  không xoá, kéo bản của người về; tệp lớn chưa tải không bị ghi đè bằng nội dung cụt (bản của agent thành tệp riêng, nhắc `fs_pull`); hai
  bản xung đột cùng phút không đè nhau; người xoá thư mục thì tệp agent chưa lưu bên trong được giữ thành bản riêng. **Xoá hàng loạt**
  (> `filehub.lane.max_delete_batch`, mặc định 20, mỗi run) bị GIỮ, báo người — `cfharness filehub resume --root <id>` để xoá tiếp, thêm
  `--discard` để không xoá và kéo tệp về lại — hoặc nút "Cho xoá"/"Không xoá" ở thẻ "Cổng cfharness" (Cài đặt web). Tool `fs_delete` của
  agent đi cùng đường xoá đó (cùng trần, không xoá tệp tạm/khoá/`.git` của người và giữ thư mục còn chứa chúng). Xoá thư mục: tệp trước, thư mục sau (Không gian số chỉ xoá thư mục rỗng). Ghi lên Không gian số chờ quét virus tới `filehub.lane.write_timeout_s` (mặc định 360 s) và
  gửi lại an toàn (cùng Idempotency-Key). `cfharness filehub status` liệt kê cả thư mục chế độ lane. Run chỉ đọc không bao giờ đẩy (kể cả
  sau khi daemon khởi động lại); người di chuyển tệp lên đúng tên agent vừa tạo → bản của agent thành tệp riêng; người sửa tệp lớn chưa tải
  → không tải cả tệp mỗi lần.
- **Dán khoá là chạy được luôn**: `filehub lane setup` / thẻ "Cổng cfharness" tự thêm MỌI thư mục/workspace khoá được cấp thành thư mục
  chung chế độ lane (tên theo Không gian số, tự thêm số nếu trùng; workspace → cả workspace) — giao việc `filehub:<tên>` được ngay, không
  cần `lane test`/`lane add` hay id gốc. Hub tự đối soát mỗi lần nói chuyện với cổng: cấp thêm thư mục → tự thêm; thư mục bị thu hồi →
  báo "mất quyền" (không xoá gì); thư mục đang đồng bộ kiểu cũ cùng gốc → giữ nguyên, có nút "Chuyển sang cổng". Báo qua Telegram/Slack
  (event `filehub.lane.roots`). Bỏ trống địa chỉ → hub thử 8223 rồi 8233, tên cổng theo Không gian số; trên web chỉ cần dán khoá.
  `setup-hub-user.sh KGS_MODE=lane` kết thúc với thư mục dùng được ngay. `lane add` vẫn còn cho ca tay; "Xoá khoá" gỡ luôn các thư mục chỉ
  có qua cổng đó (tệp trên Không gian số giữ nguyên, dán lại khoá là về).
- Điều phối viên và `filehub.list`/`filehub status` thấy thư mục chế độ lane (chế độ, loại, trạng thái `ok|revoked|offline…`); thẻ
  "Không gian số" không còn báo "chưa thiết lập" khi hub dùng cổng; tên thư mục gõ sai → lỗi liệt kê các tên có thật.
- Thẻ "Cổng cfharness": trạng thái bằng chữ + việc cần làm (vd khoá bị thu hồi → "tạo khoá mới … rồi dán lại", không hiện "còn N ngày"),
  hạn khoá theo ngày giờ máy + số ngày nguyên, nút "Thử kết nối", mọi thao tác quay về đúng thẻ. Hub tự kiểm tra cổng định kỳ (cả lúc không
  có run): "không kết nối được" tự về "đang hoạt động" khi Không gian số chạy lại. Lô xoá hàng loạt đang giữ liệt kê đường dẫn (50 mục đầu
  + "và N mục nữa") trên thẻ, trang xác nhận và tin Telegram/Slack.
- Lô xoá hàng loạt đang giữ vẫn hiện "N mục xoá đang chờ duyệt" + "Cho xoá"/"Không xoá" (cùng nghĩa) sau khi run đã kết thúc, và có ở
  trang "Cần bạn" (task bị chặn vì lô xoá có nút tới lô đó). Thay đổi còn kẹt sau run ghi rõ thao tác từng mục (xoá / ghi / di chuyển) + đường
  dẫn; trang xác nhận nói thẳng "Xoá N tệp trên Không gian số …" / "Không xoá — giữ N tệp, bỏ thay đổi của agent"; "Làm lại trên Không gian
  số" không bao giờ ngầm xoá lô đang giữ. Ô địa chỉ "Nâng cao" để trống (tự thử 8223 rồi 8233). Cổng được kiểm lại mỗi phút (khoá bị thu hồi
  / Không gian số tắt thấy trong ≤ 1 phút), thẻ ghi "kiểm tra lúc …". Tool đọc của điều phối viên (`filehub_list`…) không còn ghi nhật ký như
  thao tác ghi; điều phối viên thấy lý do task đang chờ bằng chữ rõ (vd chờ slot chạy) thay vì mã thô.


### Cập nhật
- `cfharness update` không còn bảo "tắt và mở lại giao diện web" — người dùng hub chạy trên máy chủ (web qua LaunchDaemon, vào bằng link
  public) không làm được việc đó. Giờ lệnh chờ giao diện web tự chạy lại bằng bản mới (tối đa 2 phút), quá giờ thì tự chạy lại web, rồi báo
  kết quả; chỉ nhắc mở lại `cfharness chat`/`top` khi thật sự đang có cửa sổ như vậy. Trang Cài đặt web cũng thôi bảo bấm Ctrl+C (#144).

### MCP
- **Gỡ được MCP đã xoá khỏi agent/điều phối viên**: `cfharness mcp unassign <server> <agent>` không còn đòi server phải còn trong registry,
  và sửa agent không còn bị chặn vì một MCP đã gán từ trước nay không còn. Lỗi "điều phối viên … MCP 'x' không có trong registry" giờ
  chỉ thẳng lệnh sửa (#145).

---

## v0.2.18 — 2026-11-17

Điều phối viên dùng được MCP trusted (chỉ đọc) ngay với khung mặc định, và sửa cờ dòng lệnh `cfharness mcp add|edit` (#140, #142).

### Điều phối viên
- Khung quyền mặc định `strict` nay cho dùng MCP **trusted ở chế độ chỉ đọc** (`mcp: read_only_trusted`): điều phối viên gắn MCP
  đã đánh dấu trusted không còn bị chặn "vượt khung" mà không phải chạy `cfharness manager capabilities set --mcp read_only_trusted`.
  Tool MCP ghi vẫn bị chặn tĩnh, MCP untrusted vẫn bị từ chối; muốn tắt hẳn: `cfharness manager capabilities set --mcp none` (#142).

### MCP server
- `cfharness mcp add|edit <tên> --url … --oauth …`: cờ đặt SAU tên được đọc đúng — trước đây bị lưu lặng lẽ thành lệnh stdio
  (`--transport …`). Lệnh stdio vẫn viết sau `--` (#140).
- `mcp edit --transport http` (và sửa trên web) từ server stdio tự bỏ `command`/`args`/`env` cũ — không còn lỗi "transport http không dùng 'command'" (#140).
- Trang MCP server liệt kê cả redirect URI qua tên miền public (khi đang vào web qua đó) để khai cho app OAuth tạo tay ở nhà cung cấp (#140).

### Lưu ý khi cập nhật
- Không có migration. Hub đã đặt `manager.capabilities.mcp: none` rõ ràng thì giữ nguyên (không tự mở MCP). Điều phối viên chỉ dùng
  được MCP đã đánh dấu trusted — MCP untrusted vẫn báo lỗi kèm lệnh `cfharness mcp edit <tên> --trust trusted`.

---

## v0.2.17 — 2026-11-17

Trang "Hệ thống ngoài" trên web + chat, `hub doctor` báo dung lượng và dọn thư mục mồ côi, tìm hội thoại cũ trong `cfharness chat`,
đổi tên thư mục chung Không gian số, bộ tự kiểm bảo mật `cfharness selftest --live`, và proxy loopback cho MCP OAuth (bật mặc định —
run dài không còn 401, token không còn nằm trong file của run) (#129–#137).

### Hệ thống ngoài + Sức khoẻ hub
- **Trang "Hệ thống ngoài" trên web** (`/systems`, nhóm "Hệ thống" ở thanh bên): xem các app trong thư mục làm việc và job launchd của chúng
  (trạng thái, pid, ai tạm dừng, lịch), quét tìm + đăng ký hệ thống mới, thêm theo đường dẫn, gỡ, khoá/mở khoá job, tạm dừng / bật lại /
  khởi động lại job; trang chi tiết có đuôi log (đã che secret). Mọi thao tác có hộp xác nhận và ghi nhật ký kênh `web`.
- **Lệnh chat `/hethong`** (Telegram; Slack: `/cfh hethong`): xem hệ thống ngoài + job, nút Dừng / Bật lại / Khởi động lại từng job —
  luôn 2 bước. Đăng ký, gỡ, khoá chỉ làm ở web/CLI.
- **Sức khoẻ: dung lượng HUB_HOME + dọn thư mục làm việc mồ côi**: `cfharness hub doctor` và thẻ Sức khoẻ (Cài đặt web) báo dung lượng
  theo thư mục (logs, checkpoints, backups, exports, workspaces của agent…), chỗ trống ổ đĩa, và các thư mục làm việc không còn dùng —
  thư mục của hội thoại điều phối viên đã lưu trữ/không còn, workspace/worktree không run/task nào tham chiếu, workspace của run đã xong
  quá 30 ngày, git worktree của task đã xong. Mặc định CHỈ BÁO; dọn bằng `cfharness hub doctor --cleanup [--older-than NGÀY]` (liệt kê
  rồi hỏi y/N, ghi nhật ký kiểm toán). Không bao giờ xoá run/ledger/audit hay thư mục đang có run chạy; git worktree ngoài HUB_HOME chỉ
  được gợi ý lệnh `git worktree remove`.

### Không gian số (thư mục dùng chung)
- **Báo sau run cho thư mục ghi thêm**: run có `extra_write_roots` nằm trong thư mục chung (vd task làm ở scratch nhưng được ghi vào
  `~/KhongGianSo/<tên>/Báo cáo`) giờ cũng báo tệp đã xoá / đã đổi / có thể chứa bí mật sau run — như run có workspace `filehub:`.
- **Đổi tên thư mục chung trong cfharness**: `cfharness filehub rename <tên cũ> <tên mới>` hoặc "Đổi tên trong cfharness" ở thẻ Không gian số
  (Cài đặt). Chỉ đổi tên dùng để giao việc (`filehub:<tên>`); đường dẫn trên máy và tên trên Không gian số giữ nguyên, tên cũ vẫn khớp
  (task/agent đã ghi `filehub:<tên cũ>` chạy tiếp). Từ chối khi có run đang ghi vào thư mục hoặc thư mục chưa đồng bộ lần đầu.

### Tìm hội thoại cũ
- **`/search <từ khoá>` trong `cfharness chat`** (cả màn hình đầy đủ lẫn `--classic`): tìm trong mọi hội thoại với điều phối viên và
  session của các agent (có dấu/không dấu đều khớp, `"cụm từ"` trong ngoặc kép), kết quả gộp theo luồng, đánh số, kèm đoạn trích.
  `/open <số>` (hoặc click kết quả ở màn hình đầy đủ) mở hội thoại cli tìm được; luồng khác thì hiện đoạn hội thoại quanh chỗ khớp
  (chỉ xem) và gợi ý `/attach <run>`. Có trong menu `/` và bảng phím tắt `?`.
- **`cfharness session reindex`**: dựng lại chỉ mục tìm kiếm hội thoại khi kết quả tìm thiếu/lệch — nạp lại FTS5 và quét lại toàn bộ
  run/event/hội thoại để bù tin bị sót (`--fts-only` chỉ nạp lại FTS, `--wait` chờ xong). Chạy nền trong daemon, không xoá văn bản đã
  chỉ mục; chỉ người làm được (agent bị chặn), có ghi nhật ký kiểm toán. `cfharness session status` xem số tin đã chỉ mục và tiến độ.

### Bảo mật
- **`cfharness selftest --live` — tự kiểm bảo mật với `claude` thật trước khi phát hành**: tạo hub TẠM (daemon riêng, không đụng hub thật),
  cho một agent model rẻ (mặc định haiku) thử 12 thao tác bị cấm — ghi `policy/` (shell, python, tool Write), đọc secret / token người,
  gọi thẳng socket hub, `cfharness approve`, `cfharness config set`, curl dịch vụ loopback, ghi/đọc ngoài workspace khi giam — rồi đo
  hậu quả thật và báo ĐẠT / TRƯỢT / KHÔNG KẾT LUẬN từng ca. Trần chi phí cứng (`--budget-usd`, mặc định $0.50; mỗi run `--per-run-usd`
  $0.10), đạt trần thì dừng ngay và bỏ qua các ca còn lại; xong thì dọn sạch hub tạm. `cfharness selftest --dry-run` chỉ liệt kê ca,
  không tốn tiền. Lệnh chỉ người (agent gõ bị chặn). Hướng dẫn: `docs/dev-notes/selftest.md`.

### Tích hợp MCP
- **Proxy loopback cho MCP đăng nhập OAuth (bật mặc định)**: run không còn nhận token OAuth trong file mcp.json — `claude` nói
  chuyện với một proxy của daemon trên `127.0.0.1` (URL + khoá riêng từng run, hết hiệu lực khi run kết thúc), proxy chèn token mới nhất và tự làm mới khi sắp hết hạn hoặc khi server trả 401. Run
  dài hơn hạn access token (thường 1 giờ) không còn bị 401 giữa chừng. Hỗ trợ Streamable HTTP và SSE. Proxy không khởi động được →
  tự dùng lại cách cũ (Bearer trong mcp.json). Đã nghiệm thu với Claude Code 2.1.326 thật (guard + sandbox bật). Tắt nếu cần:
  `cfharness config set mcp.oauth_proxy.enabled false`.
- **Máy có `HTTP_PROXY`/`HTTPS_PROXY`**: tiến trình `claude` của run luôn có `127.0.0.1,localhost,::1` trong `NO_PROXY`, và daemon gọi
  máy chủ OAuth/MCP chạy trên máy này đi thẳng — trước đây request tới địa chỉ cục bộ bị đẩy qua proxy công ty và hỏng.
- **`cfharness mcp edit --url` giữ nguyên transport `sse`** (trước đây tự đổi sang `http`, làm hỏng server SSE cũ).

### Sửa lỗi
- **Web không lên lại sau khi khởi động lại máy (lỗi 502 qua tunnel)**: số tiến trình ghi trong `auth/web.json` bị tiến trình khác của
  hệ thống dùng lại sau khi máy khởi động → `cfharness web` tưởng đã có server đang chạy, in link đăng nhập rồi thoát, launchd bật lại
  mãi mà không ai nghe cổng. Giờ web so thời điểm tiến trình bắt đầu với lúc ghi trạng thái, nhận ra file cũ và tự chạy bình thường.
  Hub đang bị kẹt: xoá `~/.cfharness/auth/web.json` của user đó (launchd bật lại web trong khoảng 30 giây) (#136).
- **Kết nối MCP OAuth qua link public lỗi sau khi bấm Chấp nhận**: web luôn bảo nhà cung cấp quay về `http://127.0.0.1:<cổng>/…`,
  địa chỉ chỉ đúng khi trình duyệt chạy trên chính máy hub — người vào qua `https://<tên>.…` (tunnel) bị lỗi không kết nối được. Giờ vào
  qua link public thì quay về `https://<link public>/mcp/oauth/callback`; vào từ chính máy vẫn như cũ. MCP server đăng ký client tự động
  (DCR) tự nhận địa chỉ mới; server dùng `client_id` nhập tay cần khai thêm địa chỉ này ở phía nhà cung cấp (#137).

### Lưu ý khi cập nhật
- Không có migration.
- Proxy MCP OAuth bật sẵn: MCP server đăng nhập OAuth sẽ đi qua proxy của daemon trên `127.0.0.1`. Nếu gặp lỗi kết nối MCP, tắt bằng
  `cfharness config set mcp.oauth_proxy.enabled false` (quay về cách cũ) và báo lại. Máy có `HTTP_PROXY`/`HTTPS_PROXY` không cần làm gì.
- Sau khi cập nhật có thể chạy `cfharness selftest --live` (vài cent với haiku) để kiểm lớp bảo vệ trên máy mình.

---

## v0.2.16 — 2026-11-17

Thêm lệnh nhận bản chuẩn toàn vẹn sau khi cài lại cfharness (#127); test listener của daemon chạy được trên Linux/macOS (#126).

### Lệnh mới
- **`cfharness guard integrity status|accept`**: cảnh báo "môi trường Python của hub bị thay đổi" (vd sau khi cài lại cfharness) bảo chạy
  `cfharness guard integrity accept` nhưng CLI chưa có lệnh này. Giờ `status` xem lần kiểm gần nhất và các lệch gần đây, `accept` (chỉ
  người) nhận trạng thái hiện tại làm bản chuẩn, gồm cả `.pth`/`sitecustomize`.

### Lưu ý khi cập nhật
- Trên Windows vẫn phải dừng daemon và đóng mọi cửa sổ `cfharness` trước khi `uv tool install --force` (xem v0.2.15).
- Nếu sau khi cài lại có cảnh báo "môi trường Python của hub bị thay đổi", xem `cfharness guard integrity status` rồi chạy
  `cfharness guard integrity accept`.

---

## v0.2.15 — 2026-11-17

Bản sửa lỗi: daemon trên Windows không còn bị "điếc" sau khi một client ngắt kết nối giữa chừng (#124).

### Sửa lỗi
- **Daemon sống mà "điếc" trên Windows — `cfharness chat`/web/CLI không gửi được gì, báo "daemon không chạy"**: khi một client đóng kết
  nối giữa chừng, `accept` lỗi (WinError 64) và asyncio đóng hẳn socket nghe của daemon; tiến trình vẫn chạy nhưng không nhận kết nối
  mới cho tới khi bị giết. Daemon giờ kiểm socket nghe mỗi 2 giây và tự mở lại (ghi cổng mới vào `hub.sock`), client nối lại bình thường (#124).

### Lưu ý khi cập nhật
- Trên Windows, dừng daemon và đóng mọi cửa sổ `cfharness` (TUI, `chat`, `web`) trước khi `uv tool install --force`; nếu còn
  `cfharness.exe` đang chạy, uv báo `Access is denied` khi xoá thư mục `Scripts` và cập nhật thất bại.

---

## v0.2.14 — 2026-11-16

Thư mục dùng chung với Không gian số giờ là **cả workspace** của domain, và đăng nhập được ngay trên web hub kể cả qua link public (#122).

### Thư mục dùng chung với "Không gian số": cả workspace + cài đặt trên web
- Thư mục chung giờ có thể là **toàn bộ một workspace** Không gian số (mặc định khi `cfharness filehub setup`; tài khoản đăng nhập phải là
  Workspace Admin): đường dẫn trên máy = đường dẫn bạn thấy trên web tính từ workspace, thư mục cấp 1 tạo sau tự về máy. Chế độ một thư mục cũ
  vẫn dùng được (`--scope folder`).
- Đổi tên workspace/thư mục trên web: đồng bộ chạy tiếp, trên máy giữ tên và đường dẫn cũ; giao việc bằng tên mới (`filehub:<tên mới>/…`)
  vẫn trúng đúng thư mục. Thẻ Cài đặt hiện "Toàn bộ workspace «tên mới»" và tài khoản đã đăng nhập.
- Tệp đặt thẳng ở gốc workspace không lên Không gian số (Không gian số không có tệp ở gốc) — thẻ Cài đặt liệt kê để bạn chuyển vào một thư
  mục cấp 1; agent được dặn tạo tệp trong thư mục cấp 1.
- Tài khoản bị gỡ quyền admin của workspace → thẻ Cài đặt báo đỏ kèm tên tài khoản, nút "Đăng nhập lại".
- Luồng tự thiết lập trên trang Cài đặt web (có từ v0.2.13, #121) thêm **chọn chế độ** (toàn bộ workspace — mặc định — hoặc một thư mục),
  nút **Chuyển sang toàn bộ workspace**; nút "Đăng nhập lại" gộp với "Thêm thư mục khác…" (chế độ một thư mục). Thiết lập, đăng nhập lại,
  chuyển chế độ và gỡ thư mục giờ có xác nhận 2 bước; nút Gỡ ẩn khi có run đang ghi vào thư mục đó.
- Qua link public, đăng nhập theo kiểu chuyển tiếp: Không gian số đưa trình duyệt về `https://<hub>/filehub/oauth/cb` (cần
  `web.public_origin`) — trước đây link đồng ý mở qua link public không đăng nhập xong được. Qua link public không đổi được máy chủ/binary đã
  đặt; binary tự tìm lần đầu phải là bản quản trị cài (người dùng hub không ghi được). Web trên chính máy chạy hub đăng nhập trực tiếp như terminal.
- Lệnh mới `cfharness filehub switch-to-workspace`: chuyển thư mục chung đang dùng sang toàn bộ workspace (giữ nguyên tệp cũ).
- Bị gỡ quyền admin rồi được cấp lại → đồng bộ tự chạy tiếp, không phải đăng nhập lại; workspace bị xoá trên web → báo "workspace đã bị xoá".
- Thư mục đã gỡ khỏi hub (hoặc bản cũ sau khi chuyển chế độ) không còn âm thầm đồng bộ tiếp.
- `scripts/setup-hub-user.sh` bước Không gian số mặc định đồng bộ toàn bộ workspace (`KGS_WS=` chọn sẵn tên; một thư mục: `KGS_SCOPE=folder`).

### Lưu ý khi cập nhật
- Hub đang đồng bộ MỘT thư mục vẫn chạy như cũ. Muốn chuyển sang cả workspace: `cfharness filehub switch-to-workspace` (hoặc nút "Chuyển sang
  toàn bộ workspace" ở Cài đặt web) — đăng nhập bằng tài khoản admin của workspace; xong thì kiểm bản sao mới, tự xoá bản sao cũ và thu hồi
  thiết bị cũ ở Không gian số › Hồ sơ › Thiết bị.
- Chế độ workspace và đăng nhập qua link public cần `tang-thu-sync-headless` bản mới (có `--root-mode`/`--redirect-uri`) và Không gian số
  bản có gốc workspace. Binary cũ: thiết lập chế độ workspace báo "binary sync chưa hỗ trợ…" — dùng `--scope folder` hoặc cập nhật binary.
  Bản sync mới vẫn báo `--version` 0.1.1 (chưa phát hành app desktop mới) nên `filehub.sync_min_version` giữ nguyên — hub nhận diện bản mới
  theo tính năng. Máy chủ Không gian số (thumucai.trungtamdhs.com, trungtamai.cchchn.com) đã chạy bản có gốc workspace từ 2026-11-16.
- Đăng nhập Không gian số qua link public cần đặt `web.public_origin` (https) ở terminal.

---

## v0.2.13 — 2026-11-16

Tự thiết lập thư mục chung Không gian số ngay trên web, và sửa lỗi trang Cài đặt báo lỗi sau khi cập nhật lên v0.2.12: giao diện web giờ tự chạy lại sang bản mới sau mỗi lần cập nhật.

### Tính năng mới — thư mục chung Không gian số
- Trang **Cài đặt** web có luồng tự thiết lập Không gian số, dùng được cả qua link public: nhập địa chỉ Không gian số và tên thư mục, bấm
  **Đăng nhập & nối thư mục**, rồi đồng ý thiết bị trên trang Không gian số (link mở ngay trên thẻ, thẻ tự cập nhật khi xong). Trên web cũng
  đăng nhập lại khi hết phiên, thêm thư mục khác, gỡ thư mục, tạm dừng/chạy lại đồng bộ. Không còn phải nhờ admin chạy lệnh ở máy chủ (#121).
- Binary sync chỉ do admin cài: web không nhận đường dẫn binary; hub dùng cấu hình hoặc tự tìm ở `/usr/local/bin`, `/opt/homebrew/bin`,
  `~/.local/bin`. Chỗ đặt bản sao trên máy chỉ đổi được khi dùng web trên chính máy chạy hub.

### Sửa lỗi
- **Trang Cài đặt lỗi 500 sau khi cập nhật lên v0.2.12 (macOS/Linux)**: bước cập nhật chỉ khởi động lại daemon, còn tiến trình web
  (`cfharness web`, gồm cả web chạy bằng LaunchDaemon) vẫn chạy code cũ nhưng đọc giao diện mới → trang nào dùng thông tin mới (thẻ
  "Ứng dụng máy tính" ở Cài đặt) báo lỗi. Giờ web tự chạy lại sang bản mới khi bản cài đổi (khoảng 30 giây), và daemon bản mới khi lên
  sẽ dừng web còn chạy code cũ để launchd bật lại (không có launchd thì daemon tự chạy lại đúng lệnh cũ). Sau khi web chạy lại cần đăng
  nhập lại (#120).

### Lưu ý khi cập nhật
- Không có migration, không có cấu hình phải sửa.
- Hub đang lỗi trang Cài đặt (đã lên v0.2.12): mở thẳng trang **`/update`** của web (ví dụ `https://<hub>/update`) và bấm cập nhật
  lên bản này — không cần vào Cài đặt. Khoảng 1 phút sau khi daemon lên lại, web tự chạy lại bằng code mới; đăng nhập lại là xong.

---

## v0.2.12 — 2026-11-16

Tải app máy tính ngay trên web, task xong luôn có báo cáo về người giao việc, và sửa cập nhật trên Windows khi web UI đang mở.

### Tính năng mới
- **Tải app máy tính ngay trên web**: trang Cài đặt có thẻ "Ứng dụng máy tính (Cfharness Desktop)" với nút tải macOS (.dmg) / Windows (.exe)
  bản mới nhất + hướng dẫn mở app chưa ký; chân thanh bên có link "Tải app máy tính" (#117).

### Thay đổi
- **Task xong luôn có báo cáo gửi về người giao việc**: trước đây chỉ điều phối viên báo (khi nó chạy nền và tự quyết gọi
  `notify_owner`), và nó hay kết thúc bằng `NO_ACTION` nên người không nghe gì. Giờ khi một task người giao xong, hub tự gửi báo cáo về
  đúng hội thoại/thread đã giao việc, theo khung cố định **Đã làm / Còn mở / Cần bạn** (tắt bằng `manager.auto_report_tasks: false`).
  Task không gắn hội thoại nào (tạo từ CLI/web) thì điều phối viên bắt buộc phải báo, không được bỏ qua (#118).
- Agent làm task kết thúc bằng báo cáo theo khung trên. Kết quả lưu vào task là bản **đầy đủ hơn** giữa tin nhắn cuối và `summary` của
  `task_complete`; trước đây `summary` ngắn luôn thắng nên báo cáo bị mất chi tiết.
- Task gốc tách thành task con: kết quả được gộp lại theo cùng khung (mỗi task con một dòng, gom "Còn mở"/"Cần bạn" của các task con),
  không còn hiện id nội bộ.
- Sự kiện "task xong" gửi điều phối viên mang câu kết quả + "Còn mở" + "Cần bạn" (trước đây 200 ký tự đầu).

### Sửa lỗi
- **Cập nhật trên Windows làm hub chết hẳn khi web UI đang mở**: `cfharness web` (và tiến trình khác chạy từ bản cài) giữ file trong venv
  → `uv tool install` lỗi "Access is denied" giữa chừng, venv bị xoá dở, bước quay về bản cũ cũng lỗi theo. Tiến trình cập nhật giờ dừng
  mọi tiến trình chạy từ bản cài trước khi cài, thử cài lại một lần nếu file vẫn bị khoá, và bật lại web UI sau khi xong (không mở tab
  trình duyệt mới). macOS/Linux không đổi. Có hiệu lực từ lần cập nhật **sau** khi hub đã chạy bản có sửa lỗi này (#116).

### Lưu ý khi cập nhật
- Không có migration. Báo cáo task tự động bật sẵn; tắt bằng `cfharness config set manager.auto_report_tasks false`.
- Windows: bản sửa cập nhật (#116) chỉ có tác dụng từ lần cập nhật **sau** v0.2.12 — lần này nếu web UI đang mở, đóng nó trước khi bấm cập nhật.

---

## v0.2.11 — 2026-11-16

### Sửa lỗi
- **Điều phối viên không trả lời được trên Windows** (`không spawn được claude.EXE: [WinError 206] The filename or extension is too long`):
  system prompt của điều phối viên lên ~36.600 ký tự sau khi thêm danh sách lệnh CLI (v0.2.10), vượt giới hạn dòng lệnh Windows 32.767.
  Prompt dài hơn 8.000 ký tự giờ truyền qua file (`--append-system-prompt-file`) thay vì dòng lệnh (#114).
- Danh sách lệnh CLI trong prompt điều phối viên gọn lại (mỗi nhóm lệnh một dòng, cờ của lệnh con xem `--help`): ~14.700 → ~6.800 ký tự,
  bớt token mỗi lượt (#114).
- Run không khởi chạy được vẫn ghi lại system prompt đã dùng, để điều tra (#114).

### Giám sát cỡ system prompt (#114)
- Daemon ghi log cảnh báo khi system prompt của một agent vượt `runtime.claude.system_prompt_warn_chars` (mặc định 30.000 ký tự),
  kèm các khối lớn nhất.
- `cfharness doctor` báo vàng agent có prompt vượt ngưỡng; bình thường in agent có prompt lớn nhất.

### Lưu ý khi cập nhật
- Không có migration. Cấu hình mới có giá trị mặc định: `runtime.claude.system_prompt_file_min_chars` (8000),
  `runtime.claude.system_prompt_warn_chars` (30000).

---

## v0.2.10 — 2026-11-15

### Cài đặt hub cho user
- `scripts/setup-hub-user.sh` làm trọn trong một lệnh: thêm ingress tunnel + DNS cho tên miền con (biến `DOMAIN`, #108), nối thư mục chung Không gian số khi đặt `KGS=<URL>` (#109).

### Điều phối viên
- Biết danh sách lệnh CLI thật của `cfharness`; link trần trong câu trả lời bấm được; file được nhắc tới có chip **Xem/Mở** (#103).
- **Nhập bằng giọng nói** trên web (nút micro ở ô chat Điều phối, ô "Nói tiếp" của run, ô giao việc): chế độ đọc chép và chế độ hội thoại.
  Tin nói bằng giọng không bao giờ duyệt approval hay trả lời câu hỏi xin phép (có thể nghe nhầm) — điều phối viên phải hỏi lại bằng nút (#105).
- Đổi người làm task mà vẫn giữ task trong task gốc: tool `task_reassign` của điều phối viên / RPC `task.reassign` (#104).

### Thư mục dùng chung với "Không gian số" (#106)
- Thư mục đồng bộ 2 chiều giữa máy chạy hub và Không gian số: bạn thêm/sửa/xoá tệp trên web hoặc điện thoại, agent làm việc trên bản sao cục bộ
  (`~/KhongGianSo/<tên>`). Thiết lập: `cfharness filehub setup --server https://… --binary /đường/dẫn/tang-thu-sync-headless` (đồng ý thiết bị,
  thư mục rỗng tạo ngay ở trang đồng ý); trạng thái: `cfharness filehub status` hoặc thẻ "Không gian số" ở Cài đặt web (qua link public chỉ xem).
- Giao việc với workspace `filehub:<tên>` hoặc `filehub:<tên>/<thư mục con>`; mọi run được đọc thư mục chung. Agent xoá/di chuyển tệp ở đó
  không cần duyệt — sau run hub báo (Telegram/Slack + Cài đặt), khôi phục ở thùng rác Không gian số. Không có "Hoàn tác" ở hub cho thư mục này.
- Bạn sửa tệp trên Không gian số trong lúc agent đang làm → agent được nhắc đọc lại; Không gian số hiện dải "cfharness đang làm việc".
  Bản xung đột, tệp có thể chứa bí mật và chiều đẩy lên bị dừng đều được báo.
- Chip tệp của run trên thư mục chung có thêm "Không gian số" (mở thẳng tệp trên web Không gian số).
- **Lưu ý khi cập nhật**: cần binary `tang-thu-sync-headless` (≥ `filehub.sync_min_version`); tắt mặc định, không có migration.

### App desktop (hỗ trợ phía hub) (#110)
- `cfharness web --json [--ensure] [--port N]`: in một dòng JSON (địa chỉ, link đăng nhập một lần, mã hub, phiên bản, tên); `--ensure` tự
  khởi động web chạy nền (cổng riêng theo hub, log ở `HUB_HOME/logs/web.log`). Cấu hình mới `web.port`, `web.display_name`.
- Endpoint `GET /.well-known/cfharness.json` (công khai, chỉ cho biết đây là hub cfharness) và `GET /app/badge` (cần đăng nhập, chỉ số
  duyệt/câu hỏi/run đang chạy/ESTOP — không gia hạn phiên) cho app desktop.
- Nhập bằng giọng nói dùng được trong app desktop: `voice.js` dùng bộ nhận dạng giọng nói của máy (cầu nối `window.cfhNative.speech`) khi có,
  vẫn giữ quy tắc tin giọng nói không bao giờ duyệt/chốt.
- **Directory hub** cho app desktop (hub của người quản trị máy): `cfharness admin directory token [--rotate]` in một lần URL
  `/.well-known/cfharness-directory?t=…` — app nhập một lần danh sách hub có địa chỉ công khai (tên, user, origin; không cấp quyền đăng nhập).
  `cfharness admin directory export [--out]`, `… status`; cấu hình mới `admin_directory.name`. Token sai → 404, thử sai liên tục → 429.
- Bản tóm tắt gửi người quản trị (`admin_export`) thêm `web.public_origin` (đã chuẩn hoá) và `web.display_name` (schema 3); hub chưa đặt hai
  khoá này vẫn ghi schema 2 (bộ đọc nhận schema 1/2/3).
- Agent không đọc/ghi được thư mục dữ liệu của app desktop (`io.cfharness.desktop`, và `~/Library/{WebKit,HTTPStorages,Caches}/cfharness-desktop`
  của bản dev chưa đóng gói — cookie phiên, token directory): chặn bằng sandbox,
  luật quyền tĩnh và bộ phân loại — agent trust full cũng bị chặn, kể cả qua Bash và Glob dùng ký tự đại diện.
- `cfharness web --json --ensure` gọi cùng lúc (app mở + gõ lệnh) chỉ bật một server web; `started` báo đúng trên Windows.
- **Lưu ý khi cập nhật**: máy nhiều user — nâng cấp **hub của người quản trị trước**. Hub đã đặt `web.public_origin`/`web.display_name` ghi bản tóm tắt schema 3 — hub
  admin bản cũ sẽ báo `unsupported_schema` cho hub đó cho tới khi được nâng cấp.

### Chi phí
- Lượt chạy tiếp sau khi **người** duyệt/từ chối approval không bị trần ngân sách ngày chặn (trước đây báo `resume_failed`, quyết định của người
  thành vô ích). Vẫn áp trần mỗi run và chính sách root của admin. Điều phối viên/hub tự quyết thì không được miễn.
  Khi miễn trừ thực sự được dùng → audit `budget.exempt_approved` (#105).

### Chuyển hub sang máy khác (#111)
- `cfharness hub export` đóng gói toàn bộ hub (agent, team, task, tri thức/ledger, lịch, lịch sử run) thành MỘT file `.cfhx`;
  `cfharness hub import <file> [--dry-run] [--map-path CŨ=MỚI]` nhập vào máy khác, tự đổi đường dẫn HUB_HOME/`$HOME`. Mặc định chế độ
  **nhân bản** (hai máy cùng chạy): Telegram/Slack tắt, lịch tạm dừng, task đang dở bị chặn, yêu cầu duyệt/câu hỏi đang chờ của máy nguồn
  được huỷ ("[hub] nhân bản — chưa chạy lại") ở máy mới.
- Gói **mang secret đã mã hoá** bằng passphrase (≥ 12 ký tự, hỏi 2 lần; không TTY: `--passphrase-file` quyền 0600): token MCP, tài khoản Claude,
  bot…; `--with-totp`, `--with-mcp-tokens` để mang thêm TOTP web / token MCP HTTP; `--no-secrets` để không mang. Khi nhập: hỏi passphrase và
  thử ngay — sai thì dừng trước khi ghi gì; `--skip-secrets` nhập phần còn lại (quên passphrase). Chế độ nhân bản KHÔNG nhập bot Telegram/Slack
  và đăng nhập OAuth MCP của máy nguồn (cần bot riêng, đăng nhập lại MCP); token MCP tĩnh và tài khoản Claude được nhập.
- `hub import --replace` lỗi giữa chừng → tự khôi phục hiện trạng trước khi nhập (bản `pre-restore`).
- Thư mục **Không gian số** (filehub): token thiết bị không bao giờ đi theo gói; sau khi nhập đồng bộ ở máy mới TẮT tới khi bạn chạy
  `cfharness filehub setup --name <tên>` (đăng nhập thiết bị mới — báo cáo nhập, `--dry-run` và `hub doctor` đều nhắc); chuyển nhà giữ thư
  mục chung (đường dẫn đổi theo máy mới), nhân bản gỡ thư mục chung để hai hub không tự đồng bộ chung; máy cũ đóng băng vẫn đồng bộ tệp.
- **Làm được trên web**: Cài đặt → **Chuyển hub sang máy khác** (`/hub-transfer`). Xuất: chọn gọn/đầy đủ, nhân bản/chuyển nhà, có mang secret
  không → xác nhận 2 bước (passphrase gõ ở bước 2) → chạy nền, xong bấm **Tải về** (link dùng một lần, hết hạn sau 10 phút). Nhập: tải file
  `.cfhx` lên (trần `transfer.max_upload_mb`, mặc định 4096 MB) → xem toàn bộ kế hoạch như `--dry-run` (sửa đường dẫn, chế độ, thử passphrase)
  → xác nhận 2 bước (gõ lại id gói) → hub tạm dừng vài phút để nhập rồi tự chạy lại, trang hiện báo cáo + kết quả `hub doctor`; vẫn giữ đăng
  nhập web. Máy mới tinh có thẻ **"Nhập hub từ máy khác"** ở trang chủ. Hub đã chuyển nhà có nút **Mở lại hub này**.
- Gói mang **session Claude Code** của hub để hội thoại điều phối viên, task chưa xong và run dở **chạy tiếp (`--resume`) ở máy mới**
  (profile `core`: chỉ phần đang dở; `full` — mặc định: mọi session của hub; session Claude Code bạn tự chạy ngoài hub không bao giờ bị lấy).
  Khi nhập, session được đặt vào đúng thư mục `~/.claude/projects/…` theo đường dẫn mới và đường dẫn trong transcript được đổi theo;
  session đã có ở máy đích (trùng id) giữ nguyên. Transcript trong gói đã **che secret theo giá trị** (`***`). Áp cả ở chế độ nhân bản.
- Nhập được **khác hệ điều hành, hai chiều** (macOS/Linux ↔ Windows): đường dẫn trong DB, cấu hình, chính sách và transcript session
  được đổi kiểu (`/Users/a/x` ↔ `C:\Users\a\x`, cả dạng `C:/…` và dạng escape trong JSON); `--map-path` nhận cả đường dẫn Windows.
  Bản kiểm tra `--dry-run` báo thêm: tên file không tạo được ở máy đích (ký tự `<>:"|?*`, tên `CON`/`NUL`…, hai tên chỉ khác hoa/thường
  trên ổ không phân biệt hoa/thường — kể cả macOS mặc định) — file skill/tri thức được **đổi tên an toàn** (vd skill `con` → `con-skill`) và
  ghi lại, file khác thì dừng nhập kèm tên file cần đổi ở máy nguồn; sandbox OS không có trên Windows; workspace còn kiểu đường dẫn máy cũ.
- Mọi lần nhập: MCP server chạy bằng lệnh không có ở máy mới (vd `/opt/homebrew/bin/node` khi sang Windows) được **đánh dấu lỗi** kèm gợi ý
  `cfharness mcp edit <tên>`. Tự khởi động daemon không được chuyển — cài lại bằng `cfharness daemon install-service`.
- **Chuyển nhà** `cfharness hub export --for move`: sau khi gói qua kiểm tra, máy cũ bị **đóng băng** (bền qua khởi động lại daemon) — không
  chạy run nào (kể cả điều phối viên, lịch, task), không chạy lịch/nhịp/học, Telegram/Slack ngừng nhận và gửi (máy mới giữ bot); sao lưu tự động
  vẫn chạy. `daemon status`, `top` và mọi trang web hiện banner "đã chuyển sang máy khác lúc …". Dùng lại máy cũ: `cfharness hub unfreeze`
  (có xác nhận). Nhập ở máy mới với `--mode move` để giữ nguyên bot/lịch.
- Gói ghi danh sách **repo workspace** (remote đã bỏ thông tin đăng nhập, nhánh, HEAD, có thay đổi chưa commit không — repo KHÔNG nằm trong gói).
  Khi nhập: repo chưa có ở máy mới → báo kèm lệnh `git clone …`; `--clone-repos` tự clone (không bao giờ vào thư mục đã có); agent có
  workspace chưa có → nhập ở trạng thái **paused** (tạo/clone repo rồi `cfharness agent resume <id>`). Repo có thay đổi chưa commit ở máy cũ → cảnh báo.
- Đường dẫn máy cũ trong **memory, skill, SOUL và graph memory** được tự viết lại khi daemon máy mới lên lần đầu — qua ledger (actor `import`,
  rollback được; không bao giờ bị tự rollback). Lịch sử (episode, fact đã hết hiệu lực, skill đã lưu trữ) giữ nguyên.
- `cfharness hub doctor [--probe] [--json]`: kiểm hub sau khi nhập (claude + phiên bản + đăng nhập, secret được tham chiếu, MCP, Telegram/Slack,
  repo + HEAD, agent/lịch đang tạm dừng, việc viết lại tri thức, tự khởi động, `cfharness[vector]`, quyền file) và gợi ý lệnh sửa; tự chạy sau
  `hub import`. Chỉ đọc, chạy được khi daemon dừng; `--probe` thử thật `claude auth status` + kết nối MCP (không gọi model).
### An toàn (guard) (#107)
- Lệnh "chỉ đọc" có cờ ghi file hoặc chạy lệnh tuỳ ý không còn được cho qua như an toàn: `sort -o`, `awk` (`system()`, `print > file`,
  pipe tới lệnh, `-i inplace`), `git --output`/`-c core.pager=…`/`--paginate`/`--ext-diff`, `diff --output`, `rg --pre`, `man -P`,
  `env -S`/`--split-string=`, `yq -i` — giờ là ghi/cần duyệt và vẫn kiểm đường dẫn được bảo vệ.
- Vé duyệt một lần và luật "luôn cho phép" (`exact`) phân biệt lệnh chạy ngoài sandbox (`dangerouslyDisableSandbox`), khoảng trắng trong
  dấu nháy và thư mục chạy (cwd) của Bash — duyệt lệnh ở thư mục này không còn áp cho thư mục khác.
- Luật "luôn cho phép" `exact` bắt buộc gắn agent; luật cũ thiếu agent (sửa tay) không còn áp cho mọi agent (trang Quyền ghi "không áp dụng").

### Web (#107)
- Sau khi gửi tin đọc bằng giọng nói, tin gõ tay kế tiếp không còn bị gắn nhãn giọng nói.

### An toàn (quyền agent / điều phối viên / Slack) (#107)
- Agent không còn tự ghi được dòng audit giả danh người (`actor=user`, kênh web…): RPC `audit.record` giờ chỉ người gọi được.
- Điều phối viên/agent không còn giao task cho điều phối viên hoặc team đang ESTOP bằng tên khoá phụ `assignee_agent_id`/`team_id`.
- Điều phối viên không còn yêu cầu học/tinh chỉnh agent điều phối viên bằng cách gửi kèm `id` của agent khác (`learn.refine`).
- Bot Slack chưa ghim workspace (token đặt tay) chỉ nhận tin từ workspace của chính token, không còn nhận từ mọi workspace.

### Run, task và lịch (sửa lỗi) (#104, #107)
- Task gốc không còn kẹt "đang chạy" khi task con cuối bị huỷ: còn task con xong → task gốc xong; không còn task con nào → task gốc `blocked` kèm lý do (hiện ở "Cần bạn") (#104).
- Task không còn kẹt vĩnh viễn ở trạng thái "đang nhận" (`__claiming__`) khi khởi chạy run gặp lỗi bất ngờ hay daemon dừng giữa chừng.
- Tự resume: chuỗi có run gốc đã bị dọn vẫn bị giới hạn ngân sách (trước đây thành không giới hạn); `recovery.auto_resume_max_attempts` > 50 nay có hiệu lực.
- Chi phí luỹ kế của session không còn bị tụt khi một lượt resume thiếu `total_cost_usd` — chi phí lượt sau không bị cộng hai lần.
- Lịch cron không bao giờ chạy được (vd `0 0 30 2 *`) bị từ chối ngay lúc tạo/xem trước, kèm lỗi tiếng Việt.
- Khởi động lại daemon: run bị ngắt không còn mất lượt tự resume khi khởi động không có event loop, và không còn bị resume hai lần.
- Approval đã quyết nhưng lượt chạy tiếp bị chặn (ESTOP, ngân sách…): run chuyển `failed` thay vì kẹt "chờ duyệt"; chạy tiếp bằng `cfharness run continue`.
- Gửi run lỗi giữa chừng không còn để lại run "đang xếp hàng" ma giữ chỗ ngân sách ngày.

### Học và tri thức (sửa lỗi) (#107)
- Thay đổi học chờ eval gate (L3) được kiểm guard lại trước khi áp dụng: ghim/hạn mức/đọc-trước-khi-ghi người vừa đặt trong lúc chấm không còn bị bỏ qua.
- Quét an toàn nội dung tri thức quét toàn bộ nội dung (trước đây chỉ 64K đầu, SKILL.md được tới 100K — injection đặt sau mốc 64K lọt qua).
- Sửa file hỗ trợ của skill (`references/`…) bị giới hạn cùng trần `skill_chars` như SKILL.md (trước đây không giới hạn).
- Memory/skill chứa token bí mật dạng thô (`sk-ant-…`, `ghp_…`, `github_pat_…`, `AKIA…`, `xoxb-/xoxp-…`…) bị chặn, không còn được ghi vào tri thức.
- Nhiều tiến trình cùng mở một `hub.db` cũ không còn lỗi migration chạy hai lần ("table … already exists").
- Rollback memory không gỡ được mục (mục đã bị sửa tiếp sau entry) giờ báo `partial` kèm số mục bỏ qua thay vì báo thành công im lặng.
- Tên skill có ký tự xuống dòng ở cuối (`"my-skill\n"`) bị từ chối.
- Rollback không còn khôi phục từ blob ledger hỏng/bị sửa: blob sai sha256 bị từ chối kèm lỗi.
- Nhập/nhân bản agent (`agent import`, `agent clone`) giờ đi qua quét an toàn tri thức (threat scan) như mọi thay đổi memory/skill:
  nội dung trúng mẫu nguy hiểm thì bị từ chối và không ghi gì vào hub.
- Áp dụng thay đổi học (memory/skill/cấu hình/graph) an toàn khi daemon chết giữa chừng: hub ghi ý định trước, khởi động lại thì tự hoàn tất
  hoặc bỏ thay đổi dở dang; trường hợp không xác định được thì cảnh báo (`learning.ledger_recovery`) và giữ bản trước để khôi phục tay.
  Thêm migration v27 (bảng `ledger_pending`) — tự chạy khi cập nhật.

### Lưu ý khi cập nhật
- Không có migration mới (vẫn v26).
- Máy nhiều user: nâng cấp **hub của người quản trị trước** — hub đã đặt `web.public_origin`/`web.display_name` ghi bản tóm tắt schema 3,
  hub admin bản cũ sẽ báo `unsupported_schema`.
- Thư mục chung Không gian số tắt mặc định; muốn dùng cần binary `tang-thu-sync-headless` (≥ `filehub.sync_min_version`) rồi `cfharness filehub setup`.
- Sau khi nhập hub từ máy khác: tự khởi động daemon không đi theo gói (`cfharness daemon install-service`), Không gian số phải `filehub setup` lại;
  chạy `cfharness hub doctor` để xem việc còn phải làm.
- Luật "luôn cho phép" `exact` sửa tay mà thiếu agent sẽ không còn áp dụng — tạo lại luật gắn agent (trang Quyền).

---

## v0.2.9 — 2026-11-14

### Tin cậy thiết bị (web public)
- Khi nhập mã TOTP, tick **"Tin cậy thiết bị này"** → 30 ngày không phải nhập lại. Chỉ bật khi vào qua địa chỉ public https.
- Đăng xuất vẫn giữ thiết bị; nút riêng "Đăng xuất và quên thiết bị này".
- Mỗi lượt vào/ra web, khoá do sai mã, thiết bị mới hay bị thu hồi đều báo lên Telegram.
- Xem/thu hồi: `cfharness web devices list|revoke|revoke-all` hoặc Cài đặt.

### Quản trị admin (nhiều hub trên một máy)
- Admin xem danh sách user, ESTOP từ admin (user không tự gỡ được), cảnh báo `admin.alert`.
- Chính sách root ép xuống từng user: trần ngân sách, số run đồng thời, bắt buộc sandbox.
- Nhật ký kiểm toán chỉ có metadata; trang "Quản trị máy" cho user thấy admin thấy gì về mình; `cfharness admin setup check`.

### Chi phí
- Trần ngày riêng cho điều phối viên; giới hạn số lần một task bị gián đoạn được xếp lại hàng.
- Run hỏng giữa chừng vẫn tính chi phí ước lượng; `budget.warning`/`budget.exceeded` báo một lần mỗi ngày.
- Bỏ mọi hạn mức dùng mặc định (`null` = không giới hạn) (#99).

### Sao lưu và cập nhật an toàn
- `cfharness backup create|verify|list|restore`: tự sao lưu 03:30 hằng ngày, giữ 7 bản; không chứa secret và dữ liệu đăng nhập web.
- Trước mỗi lần cập nhật đều chụp một bản; daemon không lên thì tự quay về bản cũ; `cfharness update --rollback`.

### Tài khoản Claude
- Danh sách tài khoản Claude, người bấm đổi để chạy tiếp khi một tài khoản hết hạn mức (web, CLI, `/taikhoan` trên Telegram/Slack) (#102).

### Điều phối viên & luồng việc
- **Luồng việc (workstream)**: mỗi epic một luồng, có STATE.md + task mở; tab luồng trên khung chat, đổi tab không tải lại trang, đủ CRUD (#83, #84, #89, #90).
- Giao diện chat kiểu Claude: markdown, nút Dừng, chữ chạy, thao tác dưới tin, SSE có `seq` tự nối lại (#95, #98).
- Khối "Response style" + ghi chú định dạng theo kênh; golden eval phong cách trả lời (#91, #97).
- Nút "Chạy nhịp ngay".

### Web
- Trang **Quyền đã cấp** (`/permissions`); sửa lịch chạy + xem lịch sử lượt.
- Trang Công việc realtime (#85).
- Transcript trang run kiểu Claude: markdown, gom tool, subagent lồng, diff, bố cục một cột; lưu suy nghĩ của agent để phát lại (#92, #93, #96).
- Bấm xem / mở file agent đã thao tác (cục bộ + cloud), cấu hình ở Cài đặt `files.*` (#87, #88).
- Văn phòng 3D: bảng quản lý agent, tạm dừng/tiếp tục/dừng run qua xác nhận 3D (#86).

### Telegram / Slack
- Nút tạm dừng/bật lịch; tin hiện markdown (Telegram bỏ `<pre>`, Slack dùng mdconv, chia tin theo khối) (#94).

### Agent
- Agent nạp plugin Claude Code (`agent.plugins` → `--plugin-dir`) (#82); agent thường dùng connector claude.ai (`tool_policy.connectors`) (#70).

### Lưu ý khi cập nhật
- Hub nào đã tự đặt `notify.*.events` thì phải thêm tay `web.*`, `budget.*`, `admin.alert`.
- Migration tới v26.

---

## v0.2.8 — 2026-11-10

### Sửa lỗi tạo điều phối viên
- `agent create --template manager` lỗi `KeyError` trên mọi hub (SOUL có ngoặc nhọn bị `str.format` hiểu nhầm). Nay chỉ thay `{name}`/`{role}`.
- `scripts/setup-hub-user.sh` bước 6/9 tạo sẵn agent `dieu-phoi` (mẫu manager) và chỉ định làm điều phối viên.
- Hướng dẫn tạo hub: thêm bước 6b điều phối viên.

## v0.2.7 — 2026-11-10

### Sửa lỗi vận hành nhiều hub
- Cập nhật khi hub chạy bằng LaunchDaemon/LaunchAgent: launchd chạy lại daemon bằng bản mới (trước đây daemon mới chạy ngoài launchd, chết thì không tự lên lại).
- `scripts/setup-hub-user.sh`: ghép token Claude bị xuống dòng khi dán, gọi thử Claude trước khi lưu; token hỏng thì hỏi lại (hết lỗi 401).

## v0.2.6 — 2026-11-10

### Đăng ký đăng nhập bằng mã QR
- Trang `/enroll` → "Hiện mã bí mật" hiện **mã QR** cho Google/Microsoft Authenticator, Authy, 1Password; vẫn có link `otpauth://` và khoá nhập tay.
- QR sinh ở server (`segno`, không CDN), trang `Cache-Control: no-store`.

## v0.2.5 — 2026-11-10

### Sửa lỗi đa instance
- **Một daemon cho mỗi HUB_HOME** (khoá `daemon.lock`): LaunchDaemon và CLI tự khởi động cùng lúc không còn bật 2 daemon cùng ghi `hub.db`.
- `cfharness web totp enroll` không in bí mật (chỉ in link `/enroll`; `--show-uri` nếu cần).
- `scripts/setup-hub-user.sh`: tạo hub cho một user macOS bằng một lệnh.

## v0.2.4 — 2026-11-08

- **Nhiều hub trên một Mac mini**: giam agent trong workspace (`workspace.confine`), web public qua tunnel TLS + TOTP, LaunchDaemon (`cfharness admin install-daemon`), tổng quan admin (#75).
- Slack: tag bot trong thread đã tham gia → đọc được tin người khác; `thread_context` mặc định bật (#73).
- Slack: đọc tin được chuyển tiếp (Forward) (#74).

## v0.2.3 — 2026-11-07

- Slack: điều phối viên tag được người dùng (#69); báo cáo lịch đăng thẳng vào kênh chung (`notify.slack.channel_root_posts`) (#71).
- Slack mặc định `allow_channels` + reaction (#72).
- Chat: gỡ nút khi approval/câu hỏi đã được quyết trong lúc đang gửi (#59).

## v0.2.2 — 2026-11-05

- Index run đang sống (migration v22) — hết đứng mỗi chu kỳ `manager.check_s` (#67).

## v0.2.1 — 2026-11-05

### Học / memory
- **Graph memory**: kho thực thể + fact có thời gian trên SQLite, truy hồi FTS5 + bge-m3, tầng agent/team/global (#49); mirror MEMORY.md vào graph (#51).
- Web `/memory`: quản lý + xem memory hệ thống và graph memory (#50).
- Tự đề xuất golden task từ task done, người duyệt một chạm (#40).

### Điều phối viên
- Việc cho thành viên team luôn đi qua team (#53); kết quả heartbeat báo về đúng hội thoại đã giao việc (#56).
- `task_cancel` archive task chờ (#62).

### Guard & bảo mật
- Bịt ca lách lập lịch hoãn / chạy bền (#37); allowlist domain WebFetch theo agent (`tool_policy.web_allow_domains`) (#44).
- MCP `hub_approve` grant theo luật (#45); `agent.revert/archive`, `trash.restore/purge` chỉ người ở mọi đường vào (#39).

### Tin cậy
- Tự resume có giới hạn run dừng vì lỗi tạm thời + thống kê run treo ở thẻ Sức khoẻ (#42).
- Xoay vòng log daemon an toàn trên Windows (#43); đóng `eval_runs` kẹt khi daemon khởi động lại (#64); sửa đường dẫn autostart (#65).

### Văn phòng 3D
- Khu Cafe điện tử cho agent rảnh, chế độ "Vào văn phòng" (đi bộ), điều phối viên là nhân vật chính, làm lại hình ảnh (PBR, bóng), mượt 60 FPS (#52, #54, #55, #57, #58, #60, #61, #63).

### Telegram / Slack
- Tin chủ động chỉ gửi về chat đã giao việc (#48); Slack trả lời điều phối viên dạng trích dẫn có link (#58).

## v0.2.0 — 2026-10-31

### Slack như Claude Tag
- Tag bot ở mọi kênh không cần ghép nối (#24); đang xử lý + tiến độ + nút Dừng, nói tiếp trong thread, ngữ cảnh thread (#25, #26).
- Không đăng lên gốc kênh — trả lời vào thread của cuộc, không có thì DM (#27); chế độ gọn, icon ⏳ → ✅/❌ (#28, #29, #30).

### Điều phối viên & duyệt
- Nhận đính kèm PDF/Excel/Word/PowerPoint (#31).
- **Cổng duyệt** `gates.mode` human|manager|off + không hỏi lại theo agent + luật (#32).
- Skill manager trên web, guard trust all (#23).

### Web
- **Chế độ 3D "Văn phòng"**: agent là nhân vật chibi, duyệt + chat ngay trong 3D (#33).
- Tài nguyên tĩnh có phiên bản (hết kẹt cache `app.js`) (#22); Cài đặt hiện cấu hình theo nhóm, dropdown cho mục có danh sách giá trị.

### Học / memory (ngang Hermes)
- Tool `memory` cho agent ghi memory ngay trong session (#35), không giới hạn số lần, ghi được tầng team/global (#41).
- Thu hẹp khoảng cách memory với Hermes (#36); `session_search` giữ kết quả tool 2.000 ký tự + gộp theo luồng (#38).

### Khác
- Tin Telegram/Slack gọn: ai — cần gì — bấm gì (#21); README hướng dẫn cài cho người dùng (#20).
- Daemon không ghi đè token người khi đã có daemon sống; launchd/systemd mang PATH lúc cài (#46); sửa 7 lỗi từ rà log (#34).

## v0.1.0 — 2026-10-27

Bản phát hành đầu tiên cho người dùng. Cài: `uv tool install --python 3.13 git+https://github.com/lephuong123/agent-hub@v0.1.0`.

- **Lõi**: daemon điều phối agent chạy trên `claude` CLI (mỗi run một tiến trình `claude -p … stream-json`), cô lập cấu hình, hàng đợi FIFO,
  ngân sách, ESTOP, khôi phục sau restart, tự resume; CLI `cfharness`, JSON-RPC qua Unix socket; SQLite WAL.
- **Bề mặt**: TUI `cfharness top` (kiểu htop, attach, steer, takeover), `cfharness chat` fullscreen kiểu Claude Code, web cục bộ, MCP server `hub_*`
  (stdio + HTTP có token), Telegram, Slack 2 chiều (Socket Mode: duyệt bằng nút, `/cfh`, thread = reply, modal, ảnh/tệp).
- **An toàn**: hook guard phân loại thao tác (safe/write/destructive), phê duyệt của người (không bao giờ tự từ chối do hết giờ), "luôn cho phép",
  hồ sơ phê duyệt strict|balanced|autonomous, sandbox OS, kiểm mạng WebFetch/curl, trust full theo agent, xác thực RPC, toàn vẹn policy, che secret.
- **Team & điều phối**: team (lead/worker/reviewer), task DAG, dispatcher, workspace repo thật (checkpoint/diff/revert, worktree cho task song song),
  **điều phối viên** (manager) giao việc + heartbeat, câu hỏi có lựa chọn, xin phép người bằng nút, lịch chạy tự động, hệ thống ngoài hub (launchd).
- **Học**: Signal, review fork, proposal tự áp dụng qua ledger (rollback được), eval gate với golden task, regression monitor, curator, digest,
  tầng tri thức team/global + canary, tìm kiếm hội thoại FTS5.
- **Tích hợp**: registry MCP + OAuth 2.1, danh mục ~64 MCP server, connector claude.ai, Claude in Chrome, secret store, audit.
- **Nền tảng**: macOS, Linux, Windows native; tự khởi động daemon (launchd / systemd / Task Scheduler); đổi tên agent-hub → cfharness (`home migrate`).
