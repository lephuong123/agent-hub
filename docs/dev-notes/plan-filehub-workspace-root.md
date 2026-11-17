# Plan: thư mục chung = CẢ workspace Không gian số (gốc workspace)

Ngày: 2026-11-16. Trạng thái: **bản nháp, chờ chủ repo chốt §0**. Nối tiếp `plan-cloud-files.md` (hợp đồng §8 vẫn giữ cho chế độ một thư mục).
Repo: `~/Workspace/quan-ly-thu-muc-ai` (Không gian số), `~/Workspace/tang-thu-sync` (sync), repo này (cfharness).

## Vì sao
Sự cố 2026-11-16 (hub `cchcadmin`, admin.cchchn.com): người ra lệnh "trong workspace 'admin harness', đọc `v1_2026-11-15/…`". Điều phối viên
đọc file bình thường (guard cho phép mọi Read/Glob trong `~/Workspace`), nhưng chỉ thấy cây con của MỘT thư mục `cfharness` — gốc đồng bộ cấp
lúc `filehub setup` (`oauth_grants.root_folder_ids=[id]`). Đường dẫn người nhìn trên web (tính từ workspace) không khớp cây trên máy; đổi tên
thư mục trên web cũng không đổi tên trên máy (`add_root` cố định tên lúc thêm, hub lưu cứng `local_path`).
Mục tiêu: **lấy workspace làm gốc** — bản sao cục bộ là toàn bộ workspace, đường dẫn trên máy = đường dẫn trên web tính từ workspace,
thư mục cấp 1 tạo sau tự có.

## 0. Quyết định (chủ repo chốt 2026-11-16)
W1–W5 theo cột "Đề xuất" bên dưới. Thêm:
- **W6 — tài khoản**: hub đăng nhập bằng TÀI KHOẢN CỦA NGƯỜI CHỦ DOMAIN (Workspace Admin của ws domain đó), không tạo tài khoản riêng
  cho hub. Hệ quả chấp nhận: tệp/phiên bản agent ghi mang tên người chủ trên web (phân biệt bằng tên thiết bị "cfharness · <domain>" trong
  nhật ký thiết bị + dải presence).
- **W7 — cài đặt trên WEB (sau này app desktop)**: thiết lập/đăng nhập/chuyển chế độ/tạm dừng làm được từ thẻ "Không gian số" ở Cài đặt web,
  KỂ CẢ khi mở qua link public (bỏ giới hạn `is_local_request` cho thao tác; vẫn đăng nhập web + TOTP + xác nhận bước 2 cho đăng nhập/chuyển
  chế độ/gỡ). Mọi thao tác chỉ qua RPC `filehub.*` (không logic trong template) để app desktop gọi lại cùng RPC. CLI giữ làm đường phụ.
  Đăng nhập từ xa = **relay OAuth**: `redirect_uri = <web.public_origin>/filehub/oauth/cb` (hợp đồng `openapi.yaml`), headless nhận
  `CODE <code> <state>` qua stdin thay cho listener loopback; PKCE verifier không rời tiến trình sync trên máy hub.

| # | Câu hỏi | Đề xuất |
|---|---|---|
| W1 | Ai được chọn "Toàn bộ workspace" ở trang đồng ý thiết bị? | **Chỉ Workspace Admin.** Admin thấy mọi thư mục (trừ nhãn bí mật) → feed/snapshot không phải lọc ACL từng hàng, không có "thư mục thấy được dưới cha không thấy". Người thường giữ chế độ một thư mục |
| W2 | Thư mục gắn nhãn bí mật / cây "Dự án" (download-only) | Bí mật: **loại khỏi** đồng bộ như hiện nay (`secretAllowed`). "Dự án": đồng bộ **chỉ tải xuống** (`readOnlyTree`, như hiện có) |
| W3 | Tệp đặt thẳng ở gốc workspace (không có thư mục cha) — Không gian số không có khái niệm này | Server từ chối (`422 root_files_not_allowed`); sync đưa vào danh sách "không đẩy được" (không lặp lại), cfharness hiện ở thẻ Cài đặt + hướng dẫn agent "tạo trong một thư mục cấp 1" |
| W4 | `filehub setup` mặc định chế độ nào? | **workspace** (chế độ `folder` giữ cho người không phải admin, cờ `--scope folder`) |
| W5 | Hub `cchcadmin` hiện tại | Chuyển sang workspace "admin harness" (§5): gỡ root cũ (giữ tệp), đăng nhập lại chế độ workspace, đồng bộ về thư mục mới, người xoá bản cũ sau khi kiểm |

Rủi ro chấp nhận khi chọn workspace: agent ghi được MỌI thư mục của workspace (kể cả thư mục người khác tạo sau). Lưới an toàn giữ nguyên:
phiên bản + thùng rác Không gian số, `filehub.deleted` báo sau run, `--max-delete-batch` tuỳ chọn, guard/sandbox/confine.

## 1. Mô hình
- **Gốc ảo = `wsId`.** Server không có hàng thư mục gốc workspace (thư mục cấp 1 là `folders.parent_id IS NULL`). Giao thức sync dùng
  `wsId` làm `rootId`: mọi item cấp 1 trả `parentId = wsId`; `POST /api/sync/folders {parentId: wsId}` = tạo thư mục cấp 1;
  `PATCH {parentId: wsId}` = chuyển lên cấp 1. Engine sync KHÔNG cần biết khác biệt (root vẫn là "một id"; sim đã mô hình `ws_root`).
- Đổi tên workspace / thư mục trên web không đổi id → bản sao cục bộ giữ đường dẫn; tên hiển thị lấy sống từ server.
- Bản sao cục bộ: `<filehub.local_parent>/<tên workspace lúc thêm>/` (vd `~/Workspace/KhongGianSo/admin harness/`). Workspace cfharness
  `filehub:<tên>` trỏ vào gốc này; `filehub:<tên>/<thư mục cấp 1>/…` = đúng đường dẫn người thấy trên web.

### 1b. Tài khoản Không gian số của hub ≠ người chủ hub
Mô hình triển khai: mỗi domain/hub (vd `admin.cchchn.com`) có MỘT workspace riêng do người của domain đó quản lý; domain mới → workspace
mới, Super Admin tạo workspace với `adminUserId` = người đó (`fld/workspaces.ts:94–100`). Tài khoản mà hub đăng nhập (grant thiết bị)
có thể KHÁC người chủ hub (tài khoản riêng cho hub, hoặc tài khoản của một người khác trong domain). Hệ quả, kiểm từ code:
- **Grant gắn (tài khoản, wsId), quyền xét SỐNG mỗi request**: token thiết bị nạp lại `loadMemberships` mỗi lần (`syn/device-auth.ts:138`)
  → `adminOf` luôn là hiện tại. Cưỡng chế chế độ workspace = `wsId ∈ root_ws_ids` **VÀ** `wsId ∈ a.adminOf` ở mọi route/WS. Tài khoản bị
  gỡ admin / rời workspace / bị khoá → `root_revoked` ngay (không chờ hết hạn token); thêm lại admin → đăng nhập lại.
- **Một tài khoản ở nhiều workspace** (`workspace_members` nhiều dòng, `is_admin` theo từng ws): trang đồng ý bắt chọn ĐÚNG workspace
  (`root_hint` = tên ws hub đề xuất, không tự chọn khi > 1). Dùng chung một tài khoản cho nhiều hub được (mỗi hub một thiết bị, một ws),
  nhưng ăn trần `policy.sync.maxDevicesPerUser` của tài khoản đó.
- **Super Admin**: là admin ẢO của mọi workspace nhưng không là thành viên (`lib/session.ts:86–97`) → **từ chối** chế độ workspace
  cho tài khoản `system_role='super'` (`400 super_not_member`, giống thêm thành viên) — hub phải dùng tài khoản thành viên admin của ws.
- **Ghi nhận tác giả**: mọi tệp/phiên bản agent ghi mang tên TÀI KHOẢN hub trên web, không phải người chủ hub. Đã chốt (W6): dùng tài khoản
  người chủ domain; phân biệt agent bằng tên thiết bị.
- **cfharness**: không suy tài khoản từ user OS/domain; dòng `ROOT` + status của headless thêm `account` (username, chỉ để hiển thị) →
  thẻ Cài đặt "Đăng nhập bằng <tài khoản> · workspace <tên>"; `root_revoked` → thẻ đỏ "tài khoản <x> không còn là admin của <ws> —
  đăng nhập lại bằng tài khoản admin".

## 2. Không gian số (`quan-ly-thu-muc-ai`) — PR T6
| Phần | Việc | Chỗ sửa |
|---|---|---|
| Migration 047 | `oauth_grants.root_ws_ids uuid[]` (NULL = không có); bảng `sync_presence_ws(ws_id → workspaces ON DELETE CASCADE, device_id, label, since, until, PK(ws_id, device_id))` | `packages/db/migrations/047_sync_ws_root.sql` |
| Đồng ý thiết bị | `root_mode=workspace` (authorize nhận `single\|workspace`); GET consent trả `root.mode`, `workspaces[{id,name,isAdmin}]`; POST `root {wsId, scope:'workspace'}` → kiểm `isWsAdmin` (W1), không phải Super Admin (§1b), ws `active` → grant `root_ws_ids=[wsId]`, audit `device_registered {rootWsIds}` | `syn/device-auth.ts` (`syncAuthorize` 194–217, consent 450–534) |
| Relay + tài khoản (W6/W7) | authorize/consent chấp nhận `redirect_uri = https://<host>/filehub/oauth/cb` CHỈ khi có `root_mode` (https, path cố định, không query/fragment/userinfo, host không phải IP, ≤ 253 ký tự); trang đồng ý ghi rõ host đích; token response thêm `account {username, fullName}`; lỗi consent `super_not_member` / `not_ws_admin` trả về redirect_uri | `syn/device-auth.ts`, `modules/mcp/oauth.ts` (kiểm redirect) |
| Token / actor | `roots: [{id: wsId, wsId, name: ws.name, kind: 'workspace'}]` (`grantRootsOut`); `Actor.device.roots` → `{folders, workspaces}` | `device-auth.ts` 127–143, 245–252; `lib/session.ts:50` |
| Cưỡng chế | `requireInRoots`: item thuộc `ws_id ∈ root_ws_ids` → qua (thêm vào nhánh cây con hiện có); không có wsId nào xoá/chuyển được (không phải item); browse chỉ liệt kê workspace được cấp; `readableBlobs` lọc `ws_id`; ws.ts subscribe nhận `wsId`; mất quyền admin / ws `deleting` → `root_revoked` | `syn/root-grant.ts`, `syn/routes.ts:71,95–120`, `syn/write.ts`, `syn/commit.ts:127`, `fld/uploads.ts:55,91`, `syn/ws.ts:125,167,201` |
| Feed / snapshot gốc ảo | `rootCtx`: `rootId` là workspace → `{wsId, rootId: wsId, virtual: true}`; changes = `ws_id=$1 AND seq>$2` (bỏ lọc `ancestors`) trừ cây nhãn bí mật; snapshot BFS từ mọi thư mục cấp 1, `root` = item tổng hợp `{id: wsId, name: ws.name, kind: folder}`; ánh xạ `parent_id NULL ↔ wsId` ở lớp view (`syn/view.ts`) cho cả đọc lẫn ghi | `syn/routes.ts:69`, `syn/feed.ts:35,96–100,169–211`, `syn/view.ts` |
| Ghi ở gốc | tạo/chuyển thư mục lên cấp 1 cần `isWsAdmin` (đã đúng với W1); tệp ở gốc → `422 root_files_not_allowed` (W3) | `syn/write.ts:107,130–172,238`, `fld/uploads.ts` |
| Presence | `PUT/DELETE /api/sync/roots/:wsId/presence` → `sync_presence_ws`; `presenceAt` gộp presence workspace cho mọi thư mục của ws | `syn/presence.ts`, `fld/folders.ts:905` |
| Web | Trang đồng ý: lựa chọn "Toàn bộ workspace <tên>" (chỉ hiện khi admin), dòng "Thiết bị này truy cập toàn bộ workspace <tên>"; Hồ sơ › Thiết bị "Toàn bộ workspace: <tên>" | `apps/web/src/pages/Devices.tsx` (`RootPicker` 221–272, `DeviceTable` 43–47), `api.ts`, i18n vi/en |
| OpenAPI | `rootId` = folder id HOẶC workspace id; `GrantedRoot.kind`; mã lỗi `root_files_not_allowed` | `tang-thu-sync/protocol/openapi.yaml` (nguồn hợp đồng chung) |
| Test | `apps/api/test/sync-ws-root.test.ts`: consent (admin được / không admin 403 / ws deleting), token roots, feed/snapshot gốc ảo (cấp 1 parentId=wsId, thư mục cấp 1 mới hiện trong changes, cây bí mật vắng), ghi (tạo cấp 1, chuyển lên cấp 1, tệp ở gốc 422), cưỡng chế ngoài ws → 403 mọi route + WS, mất admin → revoked, presence; web `sync-ws-root.test.tsx` | — |

## 3. tang-thu-sync — PR S5
| Phần | Việc |
|---|---|
| Login | `login … [--root-mode single\|workspace]` (mặc định `single` — giữ hợp đồng cũ); dòng `ROOT <id>\t<wsId>\t<name>\t<kind>\t<account>` (cột 4–5 mới; thiếu = `folder` / rỗng; `account` = username từ token response). Relay (W7): `--redirect-uri https://<host>/filehub/oauth/cb` → không mở listener, in `LOGIN_URL …` rồi đọc MỘT dòng `CODE <code>\t<state>` từ stdin (state sai → `ERROR state_mismatch`, thoát 1; hết `--timeout` → thoát 1) |
| Proto | `GrantedRoot.kind`; `RootRow.kind` (cột mới trong `sync_roots`, migration store) |
| Engine | Gốc ảo là root bình thường. Thêm: lỗi `root_files_not_allowed` → tệp vào danh sách không đẩy được (không retry); `.syncignore` ở gốc workspace vẫn đọc |
| Status / index | `roots[].kind`, `roots[].server_name` (tên sống từ snapshot/feed root item, để cfharness hiện tên mới khi đổi); `index/<wsId>.json` `root_folder_id = wsId`, thêm `root_kind` |
| Sim | Thế giới chế độ workspace: gốc = `ws_root`, tạo/xoá/chuyển thư mục cấp 1 cả hai phía, tệp ở gốc bị từ chối; chạy 1000 seed one-way + two-way |
| e2e | `e2e_twoway` thêm ca gốc workspace với server worktree có T6 |
| Tài liệu | `docs/headless-cfharness.md` + mục lệch |

## 4. cfharness — PR H7
| Phần | Việc | Chỗ sửa |
|---|---|---|
| Cấu hình | `filehub.folders[]` thêm `kind: folder\|workspace` (thiếu = folder); `filehub.scope` mặc định `workspace` (W4) | `filehub/service.py` (`config_folders`, `add`, `setup`) |
| Đăng nhập | `login --root-mode <scope>`; đọc cột `kind` của dòng `ROOT`; `auto_add` ghi `kind` | `service.login`, `_finish_login`; `filehub/cli.py` (`setup --scope`) |
| Trạng thái | thẻ Cài đặt + `filehub status` hiện "Toàn bộ workspace <tên sống>", danh sách tệp ở gốc không đẩy được (W3); link gốc → trang workspace trên web (route xác định ở T6) | `service.status`, `link_for`, `web/filehub_pages.py`, template |
| Đổi tên | Workspace/thư mục gốc đổi tên trên web: id không đổi → đồng bộ chạy tiếp, `local_path` + tên cfharness `filehub:<tên>` GIỮ (không di chuyển thư mục đang có run/khoá). `resolve_ref` nhận thêm tên sống (`status.roots[].server_name`) làm BÍ DANH của tên cũ; `filehub_list`/prompt ghi cả hai tên ("<tên cũ> — trên web hiện là <tên mới>") để người gọi tên mới vẫn khớp. Đổi tên cục bộ chỉ khi người chạy `cfharness filehub rename <cũ> <mới>` (HUMAN_ONLY, dừng sync → `mv` → sửa `local_path`/`sync_roots.local_path` qua lệnh của headless → chạy lại) — để sau | `service.resolve_ref`, `list`, prompt |
| Web (W7) | Thẻ "Không gian số": thao tác (setup, đăng nhập chế độ workspace, chuyển chế độ, tạm dừng/tiếp, gỡ) cả khi mở qua public; đăng nhập/chuyển/gỡ = `Action.step2`; hiện tài khoản + workspace + tên sống. Đăng nhập từ xa: RPC `filehub.login{relay:true}` → daemon chạy headless với `--redirect-uri <public_origin>/filehub/oauth/cb`, stdin PIPE, giữ `state` đang chờ (≤ 10 phút, một lần) → trang mở LOGIN_URL tab mới → `GET /filehub/oauth/cb?code&state` (route công khai: cookie SameSite=Strict không đi theo điều hướng chéo site) → RPC `filehub.login_callback{code,state}` (HUMAN_ONLY qua web; khớp `compare_digest` với state chờ, không khớp/hết hạn → 400 chung) → ghi `CODE` vào stdin → trang "Đã đăng nhập, quay lại Cài đặt". Mở trên máy hub (loopback) vẫn dùng listener cũ | `web/filehub_pages.py`, `filehub/rpc.py`, `service.login`, `web/security` (route công khai mới), template |
| Agent | khối prompt "Shared folder" ghi rõ gốc = workspace + "tệp mới phải nằm trong một thư mục cấp 1"; `filehub_list` thêm `kind` (điều phối viên) | prompt filehub, `manager/ops.py` |
| Chuyển chế độ | `cfharness filehub switch-to-workspace` (HUMAN_ONLY, ∈ `HUB_HUMAN_ONLY_CLI`): kiểm không run giữ khoá → gỡ root cũ khỏi cấu hình (giữ tệp) → đăng nhập `--root-mode workspace` → thêm root mới (`local_path` mới, không trùng/chứa bản cũ) → in hướng dẫn xoá bản cũ + thu hồi thiết bị cũ trên Hồ sơ (D10 tối đa 5 thiết bị) | `filehub/rpc.py` (`MUTATING_FILEHUB`), `auth.HUMAN_ONLY`, `filehub/cli.py` |
| Binary giả | `tests/fake_tangthu_sync.py`: `--root-mode`, cột `kind`, status `kind/server_name`, tệp ở gốc bị từ chối | — |
| Test | `test_filehub_supervisor.py` (login workspace, ROOT 4 cột, ROOT 3 cột cũ vẫn chạy), `test_filehub_workspace.py` (`filehub:<ws>/<cấp1>/…`, confine W), `test_web_filehub.py`, `test_sec_auth.py` (switch HUMAN_ONLY), `test_manager_security.py` (manager không switch) | — |
| Tài liệu | `docs/dev-notes/filehub.md`, AGENTS.md (mục filehub), CHANGELOG "Chưa phát hành" + "Lưu ý khi cập nhật" | — |

Không đổi: khoá theo gốc (`WorkspaceLocks`), checkpoint `_walk`, revert tắt, guard `filehub_metadata`, sandbox deny `HUB_HOME/filehub`,
hub_transfer (`_adjust_filehub` bỏ `folders` khi clone — `kind` đi theo khi move).

## 5. Lộ trình
| Bước | PR | Ghi chú |
|---|---|---|
| 1 | Chốt §0, cập nhật `openapi.yaml` (hợp đồng chung) | trước khi làm song song |
| 2 | T6 (Không gian số) ∥ S5 (sync, chạy trên sim + server worktree) ∥ H7 (cfharness, binary giả) | ba repo song song theo hợp đồng |
| 3 | e2e: server worktree T6 + headless S5 + cfharness H7 | |
| 4 | Triển khai prod Không gian số (`quan-ly-thu-muc-ai-prod`, trungtamai.cchchn.com / thumucai.trungtamdhs.com) + build headless mới vào `/usr/local/bin` + phát hành cfharness | `filehub.sync_min_version` nâng lên bản S5 |
| 5 | Chuyển hub `cchcadmin` (W5): `cfharness filehub switch-to-workspace` dưới user cchcadmin; điều phối viên chạy lại lệnh "đọc `v1_2026-11-15/00_Tong_quan/…`" | |

**Nghiệm thu thật:**
1. Đăng nhập chế độ workspace bằng tài khoản admin của "admin harness" → máy có `KhongGianSo/admin harness/` với mọi thư mục cấp 1.
2. Tạo thư mục cấp 1 mới trên web → vài giây sau có trên máy; agent tạo thư mục cấp 1 → hiện trên web.
3. Đổi tên workspace trên web → máy giữ đường dẫn, đồng bộ không gián đoạn, thẻ Cài đặt hiện tên mới; giao việc bằng tên mới
   (`filehub:<tên mới>/…`) vẫn trúng đúng thư mục.
4. Agent ghi tệp thẳng ở gốc → không lên cloud, thẻ Cài đặt báo "tệp ở gốc workspace không đồng bộ".
5. Tài khoản không phải admin → trang đồng ý không có lựa chọn workspace.
6. Token thiết bị gọi route sync của workspace khác → 403 `root_not_granted`.
7. Tài khoản hub là admin của 2 workspace → trang đồng ý bắt chọn, chỉ ws đã chọn về máy; tài khoản Super Admin → bị từ chối chế độ workspace.
8. Gỡ quyền admin của tài khoản hub trên web → lượt sync kế tiếp `root_revoked`, thẻ Cài đặt báo đỏ kèm tên tài khoản; cấp lại + đăng nhập lại → chạy tiếp.

Ước lượng: T6 2–3 ngày, S5 1–2 ngày, H7 2 ngày, e2e + chuyển hub 1 ngày.
