# Đổi tên agent-hub → cfharness

Ngày: 2026-10-19. Quyết định của người dùng: **bỏ hẳn** lệnh `agent-hub` (không alias), **đổi toàn bộ** sang `cfharness`
(lệnh, package Python, project, HUB_HOME, biến môi trường, socket, host web, tên MCP server, chữ hiển thị, prompt hub gửi agent).

Làm thành HAI phần để áp được lên nhánh chính sau khi các luồng khác (vd tính năng questions) đã merge:

| Phần | Nội dung | Cách áp |
|---|---|---|
| 1 — code thường (commit) | `names.py` (danh sách tên mới + cũ), guard nhận diện cả hai tên, paths dự phòng home/env cũ, sandbox chặn home mặc định còn lại, `home status|migrate`, import gói xuất tên cũ, test `tests/test_rename_cfharness.py` | merge như commit bình thường |
| 2 — máy móc (script) | `scripts/rename_cfharness.py`: `git mv src/agent_hub src/cfharness` + thay chữ toàn repo + `uv lock` + sinh lại schema | chạy SAU khi mọi nhánh đã merge, commit riêng |

## Áp phần 2

```bash
python3 scripts/rename_cfharness.py --dry-run   # thống kê, không ghi; mục [4] phải là 0
python3 scripts/rename_cfharness.py             # git mv + thay chữ + uv lock + schema
uv sync && uv run cfharness --help && uv run pytest -q
rg -n 'agent-hub|agent_hub|AGENT_HUB' src tests  # chỉ còn dòng `rename:keep`
```

Script chạy lại được (idempotent). Nó KHÔNG đổi: dòng có `rename:keep`; đường dẫn thư mục repo (`Workspace/agent-hub`);
fixture stream-json ghi thật (`*.jsonl`); chuỗi Hermes nguyên văn (`learning/prompts/hermes_*.py`, chỉ đổi dòng chú thích);
`docs/` ngoài `dev-notes/` (BRD, spike, ảnh chụp là lịch sử), `research/`, `demo/` (`agent_hub_demo`), `scripts/`.
Nếu `src/agent_hub` có file chưa track (nhánh đang làm dở), `git mv` từ chối → script chuyển thư mục thường rồi `git add`.

## Tên cũ được nhận diện VĨNH VIỄN (an toàn)

Binary/module cũ có thể còn trong venv, home cũ có thể còn trên đĩa, agent có thể đã học lệnh cũ. Nguồn duy nhất: `names.py`.

- Classifier: lệnh `cfharness|agent-hub|agent_hub` (kể cả `uv run`/`uvx`/đường dẫn tuyệt đối/`xargs`), `python -m cfharness|agent_hub…`,
  `import …`, socket `/tmp/{cfharness,agent-hub}-*.sock`, `$CFHARNESS_*`/`$AGENT_HUB_*` (lexer giải cả hai về HUB_HOME), thư mục
  `.cfharness`/`.agent-hub`, MCP server `mcp__{cfharness,agent-hub}__hub_*`, `is_hub_mcp_config`.
- `home migrate` là lệnh chỉ người (`HUB_HUMAN_ONLY_CLI`).
- Guard học (`learning/threats.py`): dặn chạy `cfharness|agent-hub approve…`, sửa `~/.cfharness|~/.agent-hub/policy/`.
- Manager (`manager/prompts.neutralize`): gỡ tiêu đề giả `[cfharness…` lẫn `[agent-hub…`, chỗ giữ khoá `{{CFHARNESS_MANAGER_KEY}}`
  lẫn tên cũ. **Vá kèm**: `_PLACEHOLDER_RE` cũ dùng `\W*` (không khớp `_`) nên bỏ lọt đúng chuỗi `{{AGENT_HUB_MANAGER_KEY}}` trong dữ
  liệu không tin cậy — chuỗi đó bị thay bằng khoá thật lúc spawn. Nay `[\W_]*`, có test.
- `auth`: đọc `CFHARNESS_RUN_ID` lẫn `AGENT_HUB_RUN_ID` của tiến trình gọi.
- Sandbox: home mặc định KHÔNG dùng (`~/.agent-hub` hoặc `~/.cfharness`) bị chặn cả đọc lẫn ghi, và là vùng bảo vệ cố định
  (workspace không được trùng/chứa).
- `agent import` nhận gói `agent-hub-agent/1` cũ.

## HUB_HOME tên cũ và `home migrate`

- `resolve_home`: `--home` > `$CFHARNESS_HOME` > `$AGENT_HUB_HOME` > `~/.cfharness` nếu có > `~/.agent-hub` nếu còn (dùng tạm,
  CLI cảnh báo tối đa 1 lần/ngày) > `~/.cfharness`. Socket env tương tự.
- `cfharness home status` — home đang dùng, còn home cũ không.
- `cfharness home migrate --dry-run` → kế hoạch; `cfharness home migrate --stop` → tự dừng daemon cũ, `os.rename`, xoá socket/pid/token
  cũ, viết lại đường dẫn tuyệt đối trong DB và file cấu hình. Chạy lại được (nếu đã chuyển thì chỉ viết lại phần còn sót).

Viết lại trong `hub.db` — mọi cột chữ của bảng KHÔNG append-only, khớp đúng tiền tố đường dẫn (không khớp `~/.agent-hub2`).
Trên home thật của người dùng (đếm ngày 2026-10-19): `agents.home_path` (14), `agents.trash_path` (7), `runs.cwd` (8),
`runs.raw_log_path` (10), `runs.stderr_log_path` (10), `teams.trash_path` (1), `skills.path` (1); cùng `hub_state`, `tasks`…
nếu có. **Không sửa** (lịch sử): bảng `events`, `audit_log`, `guard_decisions`, `snapshots`, `ledger_entries`,
`integrity_baseline` (trừ cập nhật bên dưới); cột `runs.argv_redacted`, `runs.diff`.

File viết lại: `config.yaml`, `policy/*.yaml` (ngoài `history/`), `policy/golden/*`, `mcp/registry.yaml`,
`checkpoints/*/checkpoint.json`. Bản chuẩn toàn vẹn (`integrity_baseline`) của file policy/config đã viết lại được cập nhật CHỈ
khi file trên đĩa đang khớp bản chuẩn (file bị sửa ngoài vẫn bị phát hiện như trước). File khác có đường dẫn cũ (MEMORY.md, skill,
config agent — thuộc ledger, sửa ngoài ledger sẽ bị coi là thay đổi của người) chỉ được LIỆT KÊ ("CẦN NGƯỜI XEM").
Trên home thật hiện không file nào ngoài DB chứa đường dẫn home.

## Việc người dùng phải làm sau khi áp phần 2

1. `uv sync` (venv bỏ binary `agent-hub`, có `cfharness`).
2. `cfharness home migrate --dry-run`, rồi `cfharness home migrate --stop`.
3. Cập nhật Claude Code: `~/.claude.json` đang có MCP `agent-hub` trỏ `.venv/bin/agent-hub` → sẽ hỏng. Chạy `cfharness mcp snippet`
   và làm theo (gỡ mục cũ, thêm `cfharness`). Tên tool đổi `mcp__agent-hub__*` → `mcp__cfharness__*` (quyền/allowlist theo tên cũ
   trong settings của Claude Code cần sửa tay).
4. Đăng nhập web lại: host đổi `agent-hub.localhost` → `cfharness.localhost` (cookie cũ mất hiệu lực).
5. Session `claude` đang resume với prompt cũ vẫn chạy được; prompt mới mang tiêu đề `[cfharness …]`.

## Lệch BRD (cần cập nhật BRD — không sửa docs/ ở luồng này)

- Tên sản phẩm/lệnh `agent-hub` → `cfharness` khắp BRD (FR-UI-08 bộ lệnh, §8.2 `~/.agent-hub`, `AGENT_HUB_HOME`, host web WT-1).
- Thêm lệnh `home status|migrate`; thứ tự giải HUB_HOME có dự phòng tên cũ.
- `docs/schema/event.v1.json`: `$id` đổi `https://agent-hub.local/…` → `https://cfharness.local/…` (loại event/level không đổi).
- Thuộc tính span OTel `agent_hub.*` → `cfharness.*` (allowlist trong `telemetry/otel.py` đổi theo; collector/dashboard đang lọc
  theo tên cũ cần sửa).
- Định dạng gói xuất agent `cfharness-agent/1` (vẫn nhập được `agent-hub-agent/1`).
