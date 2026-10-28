# Skill manager — hợp đồng RPC `knowledge.skill_*`

Lớp RPC trên daemon để NGƯỜI quản lý skill (CLI và trang web xây trên đây). Mã: `src/cfharness/learning/skill_rpc.py`
(đăng ký trong `learning/rpc.py:register_rpc`). Test: `tests/test_skill_manage_rpc.py`.

## Nguyên tắc
- Mọi GHI đi qua pipeline học (`hub.learning.pipeline.propose`, `created_by="user"`) — cùng đường với `learn.propose`:
  guard (quét nội dung/threat scan, pin, hạn mức, tên skill), ledger (snapshot + entry), rollback bằng `learn.rollback <entry>`.
  Không ghi thẳng file.
- 5 method ghi nằm trong `MUTATING_M4A` (learning/rpc.py), `MUTATING` (mcp_server/rpc.py → bọc audit) và `HUMAN_ONLY`
  (auth.py → agent trong run bị từ chối). Tool MCP `hub_knowledge_skill_*` và lệnh CLI `cfharness skill <save|archive|restore|import|promote|…>`
  bị bộ phân loại guard chặn khi agent gọi (`guard/classifier.py`). **CLI nên dùng nhóm lệnh `skill`** cho khớp luật này (nếu đặt
  tên khác, thêm vào `HUB_HUMAN_ONLY_CLI`). Mọi lời gọi ghi từ UI nên gửi `_audit: {"channel": "cli"|"web", "actor": "user"}`.
- Chọn tầng: `agent` (mặc định khi có `agent`), hoặc `scope: "team"` + `team` (bỏ `team` thì lấy team DUY NHẤT của `agent`),
  hoặc `scope: "global"`. `owner` trong kết quả: id agent | id team | `null` (global).
- Kết quả ghi = kết quả pipeline: `status` ∈ `applied` | `canary` (tầng team/global: thay đổi của người áp dụng NGAY,
  canary mode `watch` chỉ theo dõi, không eval trước, không tự rollback) | `blocked` (guard; có `rule`, `reason`) |
  `deferred_*` (hiếm với người). Luôn có `proposal_id`; có `ledger_entry_id` khi đã ghi. **`blocked` là kết quả bình thường
  (HTTP/RPC thành công), không phải lỗi** — UI phải hiện `reason`.
- Lỗi: tham số sai / tên sai / mô tả quá dài → `HubError` (-32001 hoặc -32602); không có → `NotFound` (-32004);
  trùng → `Conflict` (-32009); agent gọi → `Blocked` (-32003). Thông báo tiếng Việt trong `message`.

## Đọc

### `knowledge.skill_list`
Params: `{agent?, scope?: agent|team|global, team?, include_archived?: bool}`
- `agent` không kèm `scope` → `mode: "effective"`: tập skill hiệu lực của agent (agent che team che global), có `shadows`;
  `include_archived` thêm skill đã archive của tầng agent.
- có `scope` → `mode: "tier"`: đúng một tầng.
- không có gì → `mode: "all"`: mọi agent + mọi team + global (trang web tổng).
```json
{"mode": "effective", "agent": "a1", "skills": [
  {"name": "daily-report", "description": "Daily status report", "scope": "agent", "owner": "a1", "created_by": "user",
   "state": "active", "use_count": 0, "last_used_at": null, "pinned": false, "shadows": ["team:t1"]},
  {"name": "old-skill", "scope": "agent", "owner": "a1", "state": "archived", "...": "...",
   "deleted_at": "2026-10-27T10:46:30.255Z", "archived_path": ".../agents/a1/.archive/skills/old-skill__2026…",
   "purge_after": "2026-11-26T…", "restore_entry_id": "le_86e3f72366cf"}]}
```
`restore_entry_id` = entry archive còn khôi phục được (null nếu không còn).

### `knowledge.skill_get`
Params: `{agent | scope(+team), name, file?: "references/x.md", ledger_limit?: 20}` (`agent` không kèm `scope` = bản hiệu lực).
```json
{"name": "daily-report", "scope": "agent", "owner": "a1", "path": "…/skills/daily-report",
 "frontmatter": {"name": "daily-report", "description": "Daily status report"}, "body": "# Daily…", "content": "---\nname: …",
 "support_files": ["references/format.md"],
 "provenance": {"created_by": "user", "pinned": false, "state": "active", "use_count": 0, "created_at": "…", "…": "…"},
 "ledger": [{"id": "le_…", "op": "patch", "actor": "user", "applied_at": "…", "summary": "…", "reason": "…",
             "proposal_id": "pr_…", "rolled_back_by": null, "rollback_of": null, "risk_class": "L1"}],
 "shadows": ["global:global"], "file": {"path": "references/format.md", "content": "…"}}
```
`shadows` chỉ có khi có; `file` chỉ khi truyền `file`. Skill đã archive → NotFound (xem qua `skill_list include_archived`).

## Ghi

### `knowledge.skill_save`
Params: `{agent | scope(+team), name, content? | description? + body?, file?, rationale?}`
- `content` = cả SKILL.md (có/không frontmatter; `name` trong frontmatter phải trùng `name`), hoặc `description` + `body`.
  Sửa skill có sẵn: truyền phần muốn đổi (vd chỉ `description`), phần còn lại giữ nguyên; khoá frontmatter phụ được giữ.
- Chưa có ở tầng đó → `op: add`; có rồi → `op: patch` thay cả SKILL.md. Không đổi gì → `{"status": "unchanged"}`.
- `file` (vd `references/format.md`, chỉ dưới `references/ templates/ scripts/ assets/`) → tạo/thay file hỗ trợ bằng `content`;
  skill phải có sẵn.
- Kiểm: `check_skill_name`, description ≤ `learning.limits.skill_desc_chars` (60), SKILL.md ≤ `skill_chars` (guard).
```json
{"proposal_id": "pr_3b0d…", "status": "applied", "risk_class": "L1", "target": "skill", "op": "add", "target_ref": "daily-report",
 "ledger_entry_id": "le_45c4…", "summary": "a1 learned: add skill daily-report (+8/-0 lines)",
 "scope": "agent", "owner": "a1", "name": "daily-report", "created": true, "file": "SKILL.md"}
```

### `knowledge.skill_archive`
Params: `{agent | scope(+team), name, rationale?}` → `op: archive` (xoá mềm vào `<tier>/.archive/skills/`). Kết quả như trên
(không có `created`/`file`). Không có ở tầng đó → NotFound.

### `knowledge.skill_restore`
Params: `{agent | scope(+team), name, rationale?}` → tìm entry archive/merge/delete MỚI NHẤT chưa rollback của skill ở tầng đó
rồi `Ledger.rollback` nó.
```json
{"status": "restored", "name": "daily-report", "scope": "agent", "owner": "a1",
 "restored_entry_id": "le_86e3…", "ledger_entry_id": "le_f6cd…"}
```
Không có entry → NotFound (gợi ý `cfharness learn ledger --target skill`); đang có skill cùng tên ở tầng đó → Conflict;
skill bị sửa ngoài ledger / có thay đổi sau → Conflict của rollback.

### `knowledge.skill_import`
Params: `{agent | scope(+team), path, name?, description?, overwrite?: bool, rationale?}`. `path` là đường dẫn trên MÁY DAEMON:
thư mục chứa SKILL.md, hoặc file SKILL.md (khi đó đọc thêm thư mục cha), hoặc file .md bất kỳ (chỉ nội dung, không file hỗ trợ).
- Tên: `name` > frontmatter `name` > tên thư mục. SKILL.md cần frontmatter `description` (hoặc truyền `description`).
- Chỉ nhập SKILL.md + `references/ templates/ scripts/`; thứ khác, file ẩn, file không phải UTF-8 → `skipped`.
  Symlink/đường dẫn thoát khỏi thư mục → lỗi (`path_escape`); mỗi file ≤ `skill_chars`; tối đa 100 file hỗ trợ.
- Đã có + không `overwrite` → Conflict. `overwrite` → `op: patch` thay CẢ cây (file không còn trong bản mới bị bỏ; rollback được).
```json
{"proposal_id": "pr_…", "status": "applied", "op": "add", "ledger_entry_id": "le_…", "scope": "agent", "owner": "a1",
 "name": "incident-triage", "created": true,
 "imported_files": ["SKILL.md", "references/checklist.md", "scripts/collect.sh"],
 "skipped": [{"path": "notes.txt", "reason": "chỉ nhập SKILL.md + references/templates/scripts/"}]}
```

### `knowledge.skill_promote`
Params: `{agent, name, to: team|global, team?, archive_source?: bool, rationale?}` — chép skill TẦNG AGENT (SKILL.md + file hỗ trợ)
lên tầng đích bằng proposal `op: add, scope: team|global` (L3).
```json
{"proposal_id": "pr_17ce…", "status": "canary", "risk_class": "L3", "op": "add", "ledger_entry_id": "le_9a4c…",
 "canary_id": "cn_700b…", "mode": "watch", "scope": "team", "owner": "t1", "name": "daily-report", "from_agent": "a1",
 "skipped": [], "shadowed_by_agent_copy": true}
```
- Tầng đích đã có skill cùng tên → Conflict (sửa bằng `skill_save scope=…`).
- `archive_source: true` → sau khi áp dụng, archive bản tầng agent (`source_archive` = kết quả pipeline); nếu không, bản agent vẫn
  che bản mới với chính agent đó (`shadowed_by_agent_copy: true`).

## Quyết định / lệch yêu cầu
- **Promote không dùng đường curator** (`curation.submit_team_proposal`): đường đó gắn cụm near-duplicate, `created_by=curator`,
  eval trước + canary rồi tự `merge` các bản agent. Với người, gọi thẳng pipeline `scope=team|global op=add created_by=user`.
  Hệ quả (theo quyết định hardening có sẵn trong `pipeline._continue_tier`): thay đổi tầng của NGƯỜI **không qua eval gate trước**,
  áp dụng ngay cho mọi agent, canary mode `watch` (chỉ theo dõi/cảnh báo, không tự rollback). Status trả về thường là `canary`.
- **Mở rộng pipeline tối thiểu** (`pipeline._plan_skill`): payload `files: {rel: text}` cho `op add` (file hỗ trợ đi cùng SKILL.md) và
  cho `op patch` dạng `content` cả SKILL.md (+ `replace_tree: true` để thay cả cây — dùng cho import `overwrite`). Đường dẫn qua
  `check_rel_path`, mỗi file ≤ `skill_chars`, nội dung file hỗ trợ cũng qua threat scan; guard `_policy_touch` xét cả tên file.
  `op add` nhận SKILL.md có frontmatter giờ GIỮ khoá frontmatter phụ (trước đây bỏ).
- Lưu thay cả SKILL.md (patch) không bị luật read-before-write (chỉ áp cho `created_by=review`).
- `skill_list` mode `all` quét mọi agent/team → gọi `sync_index` từng tầng (ghi chỉ mục trong daemon); đủ nhanh cho vài chục agent.
- `provenance.archived_path` sau khi restore vẫn giữ đường dẫn archive cũ (cột chỉ mục, không ảnh hưởng chức năng).
- Test symlink thoát thư mục bị skip trên Windows không có quyền tạo symlink.

## CLI
`cfharness skill …` — `src/cfharness/learning/skill_cli.py` (đăng ký trong `cli.py` ngay sau nhóm `learn`/`knowledge`). Client mỏng:
chỉ gọi `knowledge.skill_*` + `knowledge.pin`, mọi lời gọi ghi gửi `_audit: {channel: cli, actor: user}`. Test: `tests/test_skill_cli.py`
(daemon thật tiến trình con + FakeRuntime; `$EDITOR` giả là script python).

| Lệnh | RPC |
|---|---|
| `skill ls [agent] [--scope team\|global] [--team X] [--archived] [--json]` | `skill_list` (không đối số = mode `all`; có agent = bản hiệu lực); phần archive kèm gợi ý `skill restore …` |
| `skill show <agent> <name> [--file rel] [--ledger N]` | `skill_get` — đầu mục (tầng, by, state, dùng, pin), file hỗ trợ, ledger (id, op, actor, giờ, đã rollback?), nội dung |
| `skill add <agent> <name> --from FILE\|DIR \| --description D --body-file F \| (không nguồn → $EDITOR với mẫu)` | `skill_save` (tạo; đã có → lỗi, gợi ý `edit`) / `skill_import` khi `--from` là thư mục |
| `skill edit <agent> <name> [--file rel] [--from FILE]` | `skill_get` → `$VISUAL`/`$EDITOR` (mặc định `notepad` Windows / `vi`) trên file tạm → `skill_save`; không đổi → "không đổi", không ghi |
| `skill import <agent> <path> [--name] [--description] [--overwrite]` | `skill_import` (đường dẫn gửi dạng tuyệt đối — daemon cùng máy) |
| `skill rm <agent> <name> [--rationale] [-y]` | `skill_archive`; hỏi y/N trừ khi `-y`, `--json` hoặc stdin không phải TTY; in lệnh khôi phục |
| `skill restore <agent> <name>` | `skill_restore` |
| `skill pin\|unpin <agent> <name>` | `skill_get` (tìm tầng) → `knowledge.pin target=skill` |
| `skill promote <agent> <name> --to team\|global [--team] [--archive-source]` | `skill_promote` |

- Tầng: `<agent>` = tầng agent; `--scope team [--team X]` / `--scope global` thì bỏ `<agent>` (vd `skill show --scope global weekly-summary`;
  argparse điền từ trái nên CLI tự dời đối số). `--team X` một mình ngụ ý `--scope team`.
- Kết quả ghi: in trạng thái, `proposal`/`ledger` id, canary (id + mode) cho team/global, file đã nhập/bỏ qua, và `hoàn tác: cfharness learn rollback <entry>`.
  **`status: blocked`** (guard học, có `rule`/`reason`) → in "BỊ CHẶN … [rule] reason" ra stderr, **mã thoát 3** (cùng mã với `Blocked` và
  `learn propose` bị chặn); với `--json` vẫn in nguyên kết quả và thoát 3. `deferred_*` → nói rõ "CHƯA áp dụng", thoát 0 (như `learn propose`).
- Lệch/ quyết định:
  - `edit`/`show`/`pin` với chỉ `<agent>` dùng bản HIỆU LỰC (có thể là skill tầng team/global) rồi ghi/pin ĐÚNG tầng đó (in tầng ra) — không
    tạo bản sao tầng agent che mất bản team. `add`/`rm`/`restore`/`import` với `<agent>` luôn là tầng agent (xoá tầng team phải ghi rõ `--scope team`).
  - `edit --file rel` tạo được file hỗ trợ mới (file chưa có → bắt đầu từ rỗng); đường dẫn do daemon kiểm (`check_rel_path`).
  - Nội dung từ trình soạn thảo/file được chuẩn hoá CRLF → LF (notepad) trước khi gửi.
  - `HUB_HUMAN_ONLY_CLI["skill"]` (classifier) thêm `add`, `pin`, `unpin` cho khớp tên lệnh thật; `ls|list|show` vẫn là lệnh đọc.

## Web
Mã: `src/cfharness/web/skill_pages.py` (route + hàm thuần), template `skills.html`, `skill.html`, `skill_form.html`, `_skill_result.html`;
`app.py` chỉ thêm `_skill_routes()`; menu "Skill" (nhóm Học & chất lượng) trong `base.html`; trang agent: chip skill → trang chi tiết + link
"Quản lý skill". Test: `tests/test_web_skills.py`. Chỉ gọi RPC `knowledge.skill_*` + `knowledge.pin` (+ đọc `agent.list`, `team.list`,
`config.get learning.limits.skill_desc_chars`, `learn.canary_get`).

| Route | Việc |
|---|---|
| `GET /skills?agent=&scope=&state=&archived=1&q=` | không agent → `skill_list` mode `all`; có agent → mode `effective` (agent che team che global). Lọc tầng/trạng thái/chữ (tên + mô tả) ở server; archived ẩn trừ khi tích "Hiện đã archive" hoặc lọc state archived/deleted. Dòng archived có nút Khôi phục (khi có `restore_entry_id`) |
| `GET /skills/{scope}/{owner}/{name}[?file=…]` | `skill_get` (luôn kèm `scope` → đúng tầng, không lấy bản hiệu lực). Owner tầng global = `-`. SKILL.md / file hỗ trợ trong `<pre>` autoescape. Skill archived (get → NotFound) → trang rút gọn từ `skill_list include_archived` + Khôi phục |
| `GET /skills/new[?agent=]`, `GET /skills/{…}/edit[?file=…\|?mode=file]` | form tạo (tên + chọn tầng agent/team/global) / sửa: mô tả + thân, hoặc cả SKILL.md, hoặc một file hỗ trợ. Trang tạo có form nhập (đường dẫn TRÊN MÁY DAEMON, đổi tên, mô tả, ghi đè) |
| `POST /skills/act/{save\|archive\|restore\|import\|promote\|pin}` | `post_guard` (CSRF + Origin + khoá phiên WT-1 — như mọi thao tác ghi khác của web) → RPC với `_audit.channel=web` |

- Xác nhận phía server (JS `data-confirm` làm sẵn): archive (danger), restore, promote (danger), import (danger khi overwrite), save ở
  tầng team/global (áp dụng ngay cho mọi agent). Save tầng agent và pin không hỏi (giống `pin` cũ).
- Kết quả ghi (`result_view`): `applied|canary|promoted|restored|unchanged` → flash ok; `blocked` → flash **lỗi** "Bị guard chặn … Luật: <rule> — <reason>"
  (save/import: vẽ lại form, giữ chữ đã nhập); `deferred_*`/khác → flash lỗi. Khối kết quả (giữ trong `Session.extra["skill_result"]`,
  hiện một lần ở trang đích) có proposal id, link `/learnings/<ledger_entry_id>`, gợi ý rollback (`cfharness learn rollback <id>`),
  canary id + mode + status (`learn.canary_get`), file đã nhập / bỏ qua, archive bản agent khi promote. Lỗi RPC (NotFound/Conflict/HubError) → flash lỗi.
- Đích sau khi ghi: save/import/promote/restore → trang chi tiết của skill ở tầng kết quả; archive → `/skills?archived=1[&agent=…]`.
- Pin dùng `knowledge.pin{scope, agent|team, target: skill, ref, on}` (cả tầng team/global), không qua bảng `ACTIONS` (thao tác `pin` cũ chỉ tầng agent).

Lệch / ghi chú:
- Web không có token read/control riêng (đó là token MCP HTTP); quyền ghi của web = phiên đăng nhập + khoá phiên ngoài cookie, cùng mức
  với mọi thao tác ghi khác. Test "chỉ đọc không ghi được" = có cookie + CSRF nhưng thiếu khoá phiên → 403 (và chưa đăng nhập → 401).
- Không thêm route vào `ACTIONS` (cần phân loại flash ok/lỗi theo `status` và vẽ lại form khi bị chặn) — route riêng `/skills/act/{op}`.
- `cfharness.web.demo` hiện lỗi khi seed điều phối viên (`KeyError: 'repo, branch, base'` trong `templates.render` — chuỗi `{…}` trong
  SOUL template, có sẵn trước nhánh này); kiểm chứng trình duyệt dùng script riêng (daemon + uvicorn + FakeRuntime): tạo bị guard chặn
  hiện flash lỗi, tạo thành công, promote team qua hộp xác nhận JS → canary watch, không lỗi console.
