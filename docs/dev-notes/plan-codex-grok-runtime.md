# Plan: thêm runtime Codex (gói ChatGPT) và Grok Build (gói SuperGrok) theo cách javis-os

> **Đã cài (2026-11-25, nhánh `feat/multi-runtime`)**: `runtime/rpcbase.py` (nền JSON-RPC + dịch sang stream-json kiểu Claude),
> `runtime/acp.py` (Grok, ACP), `runtime/codex.py` (`codex app-server`), `runtime/doctor.py`, `agents.runtime` + migration v37,
> CLI `--runtime`, form web, test `tests/test_runtime_rpc.py` (server giả theo fixture đo thật). Chưa làm: oneshot qua codex/grok (PR4).
>
> **Cập nhật sau PR0 (2026-11-25)**: đã đo — xem `docs/spikes/codex-grok-spike-report.md`. Cả Codex (`codex app-server`) lẫn Grok
> (`grok agent stdio`, ACP) đều có **kênh duyệt từng tool giữa chừng** → mục 3 dưới đây (fail-closed tĩnh) chỉ còn áp cho oneshot;
> `windows.sandbox="unelevated"` của javis-os hỏng, dùng `"mxc"`; thứ tự PR mới ở mục 2 của báo cáo.

Trạng thái: PLAN (2026-11-25). Chưa đo trên binary. Thay cho hướng `agy` (xem `docs/spikes/agy-spike-report.md`: kỹ thuật đạt qua ACP
nhưng FAQ Antigravity nói bọc `agy` trong app bên thứ ba "not supported under consumer plans").
Đổi phạm vi BRD: O3/C-05 (đa provider) — đã được chủ dự án đồng ý sửa (2026-11-25).

Nguồn tham khảo: javis-os (https://github.com/blogminhquy/javis-os) — `server/claude_cli.py` (lớp `CodexCLI`, khoảng dòng 1550–2500),
`server/codex_steer.py`, `server/grok_cli.py`, `server/openai_oauth.py`, `docs/10-models-va-engine.md`.

## 1. javis-os làm thế nào

### Codex (ChatGPT)
- Chạy **`codex exec --json --skip-git-repo-check`**, prompt qua stdin; mỗi lượt một tiến trình. Cờ global đặt **trước** `exec`
  (một số bản CLI từ chối `exec --ask-for-approval`, thoát mã 2).
- Quyền: **không duyệt từng tool**. Mức quyền của Javis → sandbox của Codex + `--ask-for-approval never`:
  `suggest` → `--sandbox read-only`, `auto` → `--sandbox workspace-write`, `full` → `--dangerously-bypass-approvals-and-sandbox`.
  Windows: thêm `-c windows.sandbox="unelevated"`, nếu không Codex lặng lẽ hạ `workspace-write` xuống `read-only` (#1133).
- MCP của Javis đưa vào bằng `-c mcp_servers.<tên>.…` (không sửa `~/.codex/config.toml`).
- Cô lập/nhiều tài khoản: **`CODEX_HOME` riêng** mỗi tài khoản (thư mục phải tồn tại trước).
- Steer: chỉ ở chat chính, dùng **`codex app-server`** (`thread/start` mang model/sandbox/approval/`developerInstructions`,
  `turn/steer` chèn tin giữa lượt, `item/agentMessage/delta` để stream); app-server bỏ qua cờ root `-m/--sandbox/--ask-for-approval`
  và từ chối `-p <profile>` → mọi thứ qua `thread/start` / `-c`. Hỏng trước khi lượt bắt đầu → lùi về `codex exec`.
- Mạng: WebSocket tới ChatGPT hay đứt → khai provider riêng `supports_websockets=false` (`requires_openai_auth`).
- Đăng nhập: **tự cài lại OAuth của Codex** (device code + PKCE, dùng `CLIENT_ID` của chính Codex), lưu token trong settings và đồng bộ
  với `~/.codex/auth.json`. Chính file đó ghi *"⚠️ Không chính thức cho app ngoài Codex"*; từng làm hỏng refresh token
  (OpenAI huỷ refresh token cũ khi dùng → hai bên giữ một mã là một bên chết).

### Grok Build (SuperGrok / X Premium+)
- Chạy **`grok -p/--single` + `--prompt-file <file>` + `--output-format streaming-json`**, `--no-auto-update`
  + `GROK_DISABLE_AUTOUPDATER=1` (tự cập nhật giữa lượt làm hỏng luồng kết quả).
- Event đo thật (29/08/2026): `{"type":"text","data":…}` (chữ ở khoá **`data`**), `available_commands` (bảng tool, không phải câu
  trả lời), `usage` (bọc trong `usage`), `end` (`stopReason`, `sessionId`, `total_cost_usd`, `num_turns`).
- Quyền: `--permission-mode bypassPermissions` + luật **`--deny`** theo mẫu `Bash(*)`, `Write(*)`, `Edit(*)`, `MCPTool(...)`
  (mức chỉ đọc = deny cả ba); `--allow` cũng có; `--max-turns N`.
- Phiên: `-r/--resume <id>` (id lấy từ event `end`, không tự sinh), `-s/--session-id`, `-c/--continue`.
- MCP: `<cwd>/.grok/config.toml` `[mcp_servers.<tên>]` — **theo thư mục làm việc**, không đụng `~/.grok/config.toml`.
- Đăng nhập: **mượn phiên của chính CLI** (`~/.grok/auth.json`, đọc được để báo trạng thái); `grok login --device-auth` in URL + mã.
- Mọi cờ đi qua `co_co()`: dò `grok --help` (cache 5 phút), chỉ truyền cờ binary khai — CLI mới, đổi cờ liên tục.

### Mô hình an toàn chung của javis-os
Không có người duyệt từng tool trong lúc chạy. An toàn = **mức quyền tĩnh trước khi chạy** (sandbox/deny của CLI) + **rào ở MCP hub**
(tiền/đơn/đăng bài đi qua hub). Khác Claude Code, Codex không có allowlist theo từng lời gọi.

## 2. cfharness làm theo — và hai chỗ KHÔNG chép

| Hạng mục | Làm theo javis-os | Ghi chú cho cfharness |
|---|---|---|
| Codex chạy `exec --json`, cờ global trước `exec` | Có | `CodexCliRuntime` |
| Mức quyền → sandbox Codex + `--ask-for-approval never` | Có | Map từ profile phê duyệt/workspace của agent (mục 3) |
| `-c windows.sandbox="unelevated"` trên Windows | Có | Máy chính là Windows |
| MCP hub qua `-c mcp_servers.*` (Codex) / `<cwd>/.grok/config.toml` (Grok) | Có | hub-self + proxy theo run như hiện nay |
| `CODEX_HOME` riêng | Có | **Theo run** (copy `auth.json` vào? — xem đăng nhập) hoặc theo tài khoản |
| Steer qua `codex app-server` | Sau (PR riêng) | `capabilities.steer=False` ở bản đầu |
| Grok `-p --prompt-file --output-format streaming-json`, `--deny`, `--max-turns`, `--resume` | Có | `GrokCliRuntime` |
| Dò cờ bằng `--help` trước khi truyền | Có | Ghi vào `runtime/compat.py` |
| Tắt tự cập nhật Grok khi chạy | Có | |
| **Tự cài lại OAuth của Codex bằng `CLIENT_ID` của Codex** | **Không** | Mạo danh client Codex, chính javis ghi là không chính thức; dễ làm chết refresh token. cfharness **mượn phiên `codex login`** của chính CLI (như javis làm với Grok); nếu cần OAuth riêng thì đi chương trình chính thức "Sign in with ChatGPT" cho app cục bộ/mã nguồn mở (https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) |
| **Chạy `full` = `--dangerously-bypass-approvals-and-sandbox` / `bypassPermissions` không rào** | **Không mặc định** | Chỉ cho agent được người đặt `trust_full` (như ngoại lệ NFR-15 hiện có). Còn lại tối đa `workspace-write` / deny luật nguy hiểm |

## 3. Guard của cfharness với runtime không có kênh duyệt động

Claude: guard hỏi người qua hook/permission tool trong lúc chạy. Codex `exec` và Grok `-p` không có kênh đó (javis-os cũng không dùng).
Cách làm: **guard quyết trước khi chạy, fail-closed**:
- Profile phê duyệt của agent → mức tĩnh: chỉ đọc (`read-only` / deny `Write(*) Edit(*) Bash(*)`), ghi trong workspace
  (`workspace-write` / deny theo luật profile), full (chỉ `trust_full`).
- Mọi luật "ask" của profile → **deny** với runtime này (không có ai để hỏi). UI ghi rõ "runtime này không hỏi giữa chừng".
- Tool MCP của hub: guard của hub-self/proxy vẫn chặn ở phía hub như hiện nay (giống "rào ở MCP hub" của javis-os) — đây là lớp
  duy nhất còn hỏi người được, vì nó nằm trong hub.
- `capabilities()`: `approval=False`, `steer=False` (Codex bản đầu), `resume=True`, `budget=False` (hub cắt bằng `--max-turns`/stop).

## 4. Điều khoản (đã kiểm 2026-11-25)

- **OpenAI**: có chương trình chính thức dùng gói ChatGPT cho *"open-source and locally hosted apps"*; app trả phí/host từ xa phải
  xin. Terms cấm chia sẻ tài khoản → **mỗi người dùng hub dùng tài khoản ChatGPT của chính họ**. Mượn `codex login` của CLI chạy
  trong app bên thứ ba: không có văn bản cấm, cũng không có văn bản nói rõ.
- **xAI**: docs Grok Build nói ACP/headless để *"integration into other apps"*, khuyên API key cho CI/tự động; trang Terms không đọc
  được (403) → **chưa rõ**, phải đọc trong trình duyệt trước khi bật cho người dùng.

## 5. PR

0. **Đo** (bắt buộc, bài học javis-os: đo binary, không đoán): cài `codex` + `grok`, người dùng tự `codex login` / `grok login`;
   ghi `--help`, mẫu event thật, sandbox Windows, MCP qua `-c`/config theo cwd, `--deny`, resume → `docs/spikes/codex-grok-spike-report.md`.
1. Runtime theo agent: registry `{claude, codex, grok, fake}`, `agents.runtime_kind`, `RunHandle.adapter` thành interface chung.
2. `CodexCliRuntime` + `CodexStreamAdapter` (fixture từ PR0) + map quyền (mục 3).
3. `GrokCliRuntime` + `GrokStreamAdapter` + `.grok/config.toml` theo run.
4. Guard: profile → mức tĩnh fail-closed; từ chối `RunSpec` có trường Claude-only (connectors, plugins, agents_json…).
5. UI/CLI/doctor: chọn runtime khi tạo agent, ẩn tính năng theo `capabilities()`, `cfharness doctor` kiểm `codex`/`grok` + đăng nhập.
6. (Sau) Codex steer qua `app-server`.
7. Tài liệu: BRD (O3/C-05), README, CHANGELOG.
