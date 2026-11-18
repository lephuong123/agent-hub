# Thư mục dùng chung với "Không gian số" (`filehub/`)

Cập nhật: 2026-11-18 (H5 — gỡ đồng bộ 2 chiều cũ). Chế độ DUY NHẤT: **cổng cfharness** (`filehub/lane/`) — hợp đồng §C, kiến trúc, phần đã làm:
`plan-filehub-lane.md`. Quyết định giữ từ `plan-cloud-files.md` §7: xoá trong thư mục chung = `write` (không hỏi, chỉ báo sau run), mọi run được
đọc, nội dung thư mục là DỮ LIỆU. Không có migration.

## Tóm tắt

Người dùng tạo khoá ở Không gian số › Hồ sơ › Cổng cfharness (cho chính tài khoản của mình, chọn thư mục/workspace cấp cho hub) → dán ở web ›
Cài đặt › Không gian số (hoặc `cfharness filehub lane setup`, bỏ trống địa chỉ: hub thử 8223 rồi 8233) → mọi gốc được cấp thành `filehub:<tên>`
dùng được ngay. Không bản sao lâu dài trên máy: run có workspace `filehub:<tên>[/<con>]` có cây làm việc riêng (`<agent home>/workspaces/lane-<tree>`),
lưu lên Không gian số trong vài giây, thay đổi của người về cây + steer. Cấp thêm/thu hồi trên Không gian số → hub tự cập nhật (vòng sức khoẻ mỗi
phút, hoặc "Thử kết nối").

| Thành phần | Vai trò |
|---|---|
| `filehub/service.py` | `hub.filehub`: thư mục (`filehub.folders`), thư mục đồng bộ cũ cần chuyển (`legacy_folders`, `LEGACY_TEXT`), giải ref (`lane_target`; `resolve_ref` luôn ném lỗi có giải thích), `link`, `remove`/`rename`/`resume`, dừng tiến trình sync còn sót (`stop_orphan_sync`), khối prompt "Shared folder" (`shared_folders`) |
| `filehub/lane/` | `client.py` (§C, chỉ loopback), `service.py` (`LaneService`: khoá, `hello` + đối soát gốc, luồng sự kiện, cây theo run, presence, mồ côi, hoàn tác), `workdir.py`, `tools.py` (`fs_*`), `safefs.py` |
| `filehub/rpc.py`, `filehub/cli.py` | RPC `filehub.status|list|link` (đọc) + `remove|rename|resume` + `lane_*` (HUMAN_ONLY, tự audit); CLI `cfharness filehub status|remove|rename|resume --root|lane …` |
| `web/filehub_pages.py` | Thẻ "Không gian số — cổng cfharness" (`/partials/filehub/lane`; `/partials/filehub` = cùng thẻ) + `/filehub/open` (link "Mở trên Không gian số") |

Tệp: `HUB_HOME/filehub/` (trạng thái cổng `lane/`, cây mồ côi `lane-orphans/`; thư mục 0700; sandbox denyRead/denyWrite + `permissions.deny`;
classifier `filehub_metadata` = cấm), khoá `HUB_HOME/secrets/FILEHUB_LANE_<CỔNG>` (0600). Backup bỏ `filehub/`; cấu hình `filehub.*` nằm trong
`config.yaml` (∈ `integrity.SECURITY_KEYS`). `filehub.server` = URL WEB của Không gian số, chỉ để dựng link (`cfharness config set filehub.server …`,
`setup-hub-user.sh` đặt từ `KGS`).

## Workspace `filehub:<tên>[/<con>]`

- `FilehubService.lane_target(ref)` → `{folder, server, root, sub, kind, key}`; khớp tên cfharness → bí danh cũ (`folders[].aliases` sau `rename`).
  `RunManager.submit`/task/dispatcher/`WorkspaceLocks`/`validate_workspace`/quyền điều phối viên đều đi qua đây; không khớp → `resolve_ref`
  ném lỗi có giải thích (thư mục qua cổng → `data.lane`; thư mục đồng bộ cũ → `data.needs_migration` + `LEGACY_TEXT`; tên sai → liệt kê tên có thật).
- Khoá = gốc thư mục chung trên server (`filehub-lane:<cổng>:<root>`); run chỉ đọc không lấy khoá. Diff/hoàn tác: `plan-filehub-lane.md` §10.
- Mọi run được ĐỌC thư mục qua cổng (`fs_*` chỉ đọc, `filehub.lane.read_all_runs`); khối prompt `# Shared folder (Không gian số)` (tiếng Anh).

## Nâng cấp từ đồng bộ 2 chiều cũ (H5)

Đồng bộ cũ (`tang-thu-sync-headless` chạy dưới daemon, đăng nhập thiết bị OAuth + relay qua web public, bản sao `~/KhongGianSo/<tên>`, `mass_delete`
của binary, `filehub switch`) đã gỡ ở H5 — mã cũ và tài liệu cũ: lịch sử git trước PR H5 (`docs/dev-notes/filehub.md` bản 2026-11-17).
Hub còn cấu hình cũ:
- Thư mục cần chuyển = `folders[].mode == sync`, hoặc thiếu `mode` mà có `local_path` (và `filehub.mode` cũ ≠ `lane`) — `folder_mode()`.
- Daemon khởi động (`FilehubService.start` ở `hub.started`): KHÔNG xoá gì (bản sao, `HUB_HOME/filehub/` cũ, `secrets/filehub/`), event
  `filehub.state{state: needs_migration, folders}` (một lần mỗi tập tên), `filehub.status.state = needs_migration` + `legacy[]{name, message,
  old_copy}`; `filehub.list` của điều phối viên có `status: needs_migration` + "do NOT assign". Thẻ web + `filehub status` in đúng câu
  `LEGACY_TEXT`: "Thư mục «X» đang dùng đồng bộ cũ (đã bỏ) — dán khoá cổng cfharness ở Cài đặt để dùng tiếp; bản sao trên máy để nguyên".
- Run/task/`workspace_default` nhắm thư mục đó → từ chối với câu trên; run tạo trước H5 trên bản sao cũ (`run_opts.workspace_kind = filehub`,
  không `filehub_lane`) còn xếp hàng/được resume → `failed` với câu trên, không bao giờ chạy trong bản sao cũ.
- Khoá cổng cấp đúng gốc (`LaneService.reconcile` ở mọi `hello` mới: setup, "Thử kết nối", vòng sức khoẻ, `prepare_run`) → TỰ chuyển: `mode: lane`,
  `server`; giữ tên, bí danh, `local_path` (chỉ để báo "bản sao cũ … hub không xoá"); event `filehub.lane.roots{converted}`.
- Tiến trình sync còn sót của daemon cũ (`HUB_HOME/filehub/supervisor.json`): `stop_orphan_sync` đối soát `(pid, start_time)` → `kill_group`
  (SIGINT → SIGTERM → SIGKILL, chờ ≤ 15 s mỗi bước, luồng phụ), xoá tệp pid; pid không khớp (đã dùng lại) → không đụng.
- Khoá cấu hình cũ (`sync_binary`, `local_parent`, `device_name`, `scope`, `mode`, `max_delete_batch`, `relay_timeout_s`…) bị bỏ qua (không có
  trong DEFAULTS; `config.get` vẫn đọc được nếu có). Lệnh CLI cũ (`setup|login|add|pause|switch|switch-to-workspace`) chỉ in hướng dẫn, exit 2.
- Nhập gói hub cũ (`hub_transfer._adjust_filehub`): bỏ `SYNC_ONLY_KEYS`, `move` giữ thư mục (thư mục cũ ghi rõ `mode: sync`, `local_path` qua
  pathmap), `clone` gỡ `folders`; `hub_state.import_filehub{needs_migration, lane_folders, command: "cfharness filehub lane setup"}` (xoá khi
  `lane_setup` thành công); `hub doctor` báo thư mục cần chuyển / cổng chưa có khoá ở máy này.

## Guard

- `HUB_HOME/filehub/**` → `filehub_metadata` (cấm, kể cả đọc qua Bash = `sensitive_read`); `.tangthu*` trong BẢN SAO CŨ (`GuardContext.
  filehub_old_copies` ← `filehub.service.old_copies`) vẫn cấm — đề phòng bản sao còn trên máy. Bản sao cũ là thư mục thường: KHÔNG còn nới
  xoá/ghi đè thành `write` (luật `filehub_delete|overwrite|root_delete` đã gỡ cùng đồng bộ).
- Cổng: `filehub_lane` (agent nhắm cổng loopback = cấm), `lane_other_tree` (cây của run khác) — ∈ `TRUST_NEVER_RULES`.
- `cfharness filehub <ghi>` ∈ `HUB_HUMAN_ONLY_CLI` (giữ cả tên lệnh cũ).

## Quyền

RPC ghi `filehub.*` ∈ `auth.HUMAN_ONLY` + `MUTATING_FILEHUB` (bọc audit), KHÔNG ∈ `MANAGER_ALLOWED`. Điều phối viên: `filehub_list`, `filehub_link`
(`path = <tên thư mục>/<đường dẫn>`) + giao việc `filehub:`. Web: `ACTIONS` (CSRF + Origin + khoá phiên, audit `web`) — `filehub_lane_setup` (khoá chỉ
ở bước 2), `filehub_lane_test|disable|orphan|deletes`, `filehub_rename`, `filehub_remove` (bước 2).

## Test (không mạng, không claude)

Server giả `tests/fake_kgs_lane.py` + `tests/lane_fake.py`: `test_lane_*.py`, `test_filehub_h5.py` (nâng cấp: cần chuyển / từ chối run /
không xoá / tự chuyển / tiến trình sót / CLI cũ / web), `test_web_filehub.py`, `test_hub_transfer_filehub.py`, ca `DESTRUCTIVE_OLD_COPY` + lane trong
`test_guard_classifier.py`, `test_sec_auth.py::test_filehub_writes_human_only`, `test_manager_security.py::test_manager_filehub_read_and_assign_but_no_writes`.
