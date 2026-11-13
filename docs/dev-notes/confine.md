# P2 — Giam agent trong workspace W (`workspace.confine`)

Nguồn: `plan-multi-instance.md` §2.2, §4/P2, §6b (chủ repo chốt 2026-11-07: mỗi người một user macOS, mỗi hub đúng một W, agent chỉ
đọc + ghi trong W cộng toolchain; **Q4 = có**: agent `trust_full` CŨNG bị giam — trust chỉ bỏ hỏi duyệt bên trong W).
Spike: `hardening-SEC.md` §1.1 (claude 2.1.319) → **phương án A** (deny `$HOME` + allow lại W).

## 1. Cấu hình (chỉ người)
```yaml
workspace:
  confine:
    root: ~/Workspace/du-an-A     # null (mặc định) = tắt, hành vi cũ
    read_allow: [~/.npm, ~/.cache, ~/.local/share/uv, ~/.cargo, ~/.rustup, ~/.nvm, ~/Library/Caches,
                 ~/.pyenv, ~/.bun, ~/.gitconfig, ~/.config/git/config, ~/.config/git/ignore]
```
`cfharness config set workspace.confine.root ~/Workspace/du-an-A`.
- `config.set` ∈ `auth.HUMAN_ONLY`, không ở `MANAGER_ALLOWED` (điều phối viên bị từ chối ở lớp 2 — test `test_manager_check_path_and_config`).
- `workspace.confine` ∈ `integrity.SECURITY_KEYS` — `SECURITY_KEYS` nay nhận khoá con có dấu chấm (chỉ nhánh con đó bị khôi phục; khoá
  `workspace.*` khác sửa tay vẫn được nhận).
- Web: chỉ hiển thị (`settings_schema.READONLY`), không có trong danh mục sửa được.
- Kiểm khi đặt (`confine.validate_setting`, gọi trong `HubConfig.set`): root tuyệt đối; không phải `/`, `$HOME`, cha của `$HOME`; không
  chứa/nằm trong HUB_HOME. `read_allow`: danh sách đường dẫn tuyệt đối, không phải `$HOME`/cha của nó.

## 2. Các lớp khi bật
| Lớp | Chỗ | Hành vi |
|---|---|---|
| Workspace | `workspace.validate_workspace` | mọi `dir:`/worktree (repo + thư mục worktree)/`extra_write_roots`/cwd run/`workspace_default` phải trong `root` hoặc vùng làm việc của hub (`agents/`, `worktrees/`, `evals/`, `checkpoints/`). `allowed_roots` và gốc nới cho test bị bỏ qua cho phép này |
| Manager | `manager/permissions.check_path` | cùng luật (lớp 1 + lớp 2 dùng chung) |
| Hệ thống | `systems.roots` | null → `[root]` (thay vì `allowed_roots`) |
| Sandbox Bash | `guard/runtime.sandbox_settings(confine=)` → `confine.apply_sandbox` | `denyRead += [$HOME]` (+ thư mục cha của root nếu root ngoài `$HOME`); `allowRead = root ∪ read_allow ∪ agents/<id>/workspaces (liệt kê) ∪ worktrees ∪ evals ∪ cwd ∪ bin trên PATH`. Mục denyRead cụ thể (secret/auth/runtime/token của hub, credential git…) vẫn thắng |
| Lớp tĩnh | `permissions.deny` (`confine.permission_deny_rules`) | Read/Grep/Glob/Edit/Write/MultiEdit/NotebookEdit vào mọi mục anh em của `root` và của từng thư mục cha tới `$HOME` (+ mọi mục ở `$HOME`) không phải tổ tiên/con của một gốc được đọc. Luật quyền KHÔNG có allow-trong-deny nên phải liệt kê (phương án B cho riêng lớp này); trần 400 mục |
| Classifier | `classify` | Read/Grep/Glob/LS/NotebookRead ngoài vùng đọc mà nằm trong vùng bị chặn → `forbidden/read_outside_workspace` (xét literal + realpath: symlink trong W trỏ ra ngoài bị chặn; Glob xét phần không-glob của `pattern` tuyệt đối/`~`/`..`). Tool ghi file ra ngoài `root` ∪ vùng làm việc hub → `forbidden/write_outside_workspace` (thư mục tạm vẫn được, trừ khi nằm trong `$HOME`); trong `root` nhưng ngoài cwd → vẫn `destructive` (người duyệt được) như cũ |
| Trust full | `build_settings(trust_full, confine)` + `GuardService._allow_trusted` | không còn `sandbox.enabled=false`: sandbox CHỈ-GIAM (`confine_only_sandbox`: đọc như trên, ghi `root ∪ read_allow ∪ cwd ∪ extra_allow_write`, mạng `*` + `allowLocalBinding`, hub.sock + token + hub khác bị chặn) + `permissions.deny` anh em của W; hook trust từ chối `read_outside_workspace`/`write_outside_workspace`/`other_hub_write` (và `hub_secret_read` khi giam). Mọi thứ khác trong W vẫn cho phép không hỏi |
| `guard.sandbox.enabled=false` | `build_settings` | vẫn sinh sandbox chỉ-giam (người tắt sandbox không tắt được giam — tắt giam = đặt `root: null`) |

`GuardContext` thêm `confine_root`, `confine_read`, `confine_deny`, `other_hub_homes` (điền ở `GuardService._confine_ctx`).

## 3. Registry hub (P2.4, nhiều hub chung một user OS)
`~/.config/cfharness/hubs.json` (env `CFHARNESS_HUBS_REGISTRY` — test đặt về thư mục tạm trong `conftest.py`, KHÔNG BAO GIỜ ghi home thật):
`{"hubs": [{"home": "<realpath>"}]}`, ghi nguyên tử (`mkstemp` + `os.replace`), file 0600, thư mục 0700; `Hub.start` tự đăng ký và bỏ
mục không còn tồn tại. `guard/runtime.other_default_homes` = home mặc định khác + home đã đăng ký khác → sandbox deny đọc + ghi, và
`fixed_protected_dirs` (workspace không được nằm trong hub khác). Classifier: đọc → `hub_secret_read` (forbidden), ghi → `other_hub_write`
(forbidden). Không phụ thuộc giam.

## 4. Test
`tests/test_confine.py` (15): cấu hình + validate, HUMAN_ONLY/integrity (sửa tay config.yaml gỡ giam → khôi phục), validate_workspace
đỏ→xanh (agent/run/task), systems.roots, sandbox settings (A + lớp tĩnh), trust_full vẫn giam (settings + `guard.check`), classifier
(đọc/symlink/ghi), manager `check_path` + `config.set` bị chặn, registry, **e2e** daemon thật + `fake_claude_guard.py` (thêm dòng
`TOOL: <Tên> <json>`) với agent trust full: đọc trong W chạy, đọc/ghi ngoài W bị hook thật từ chối. `tests/test_guard_classifier.py`:
`DESTRUCTIVE_CONFINE` (recall 100% forbidden) + báo nhầm trên `SAFE_*` dưới ngữ cảnh giam ≤ 5%.

## 5. Giới hạn còn lại
- **Bash đọc ngoài W chỉ do sandbox chặn** (classifier không phân tích đọc của Bash). Không có sandbox (Windows, Linux thiếu bubblewrap mà
  `fail_if_unavailable: false`) → Bash vẫn đọc được `$HOME`.
- Root ngoài `$HOME` (vd `/Volumes/data/W`): chỉ chặn thêm thư mục cha của root; các vùng khác ngoài `$HOME` (`/opt`, `/Volumes/khác`,
  `/Users/<người khác>` nếu quyền file cho phép) KHÔNG bị chặn đọc — dựa vào quyền file của user OS (P1).
- `permissions.deny` tĩnh liệt kê lúc dựng settings: thư mục tạo SAU đó không có trong danh sách (classifier + sandbox vẫn chặn); không
  liệt kê sâu trong tổ tiên của `read_allow` (vd anh em của `~/Library/Caches` trong `~/Library`) — chỉ classifier + sandbox chặn.
- Chế độ chỉ-giam (trust / sandbox tắt) chặn Unix socket trừ `guard.sandbox.allow_unix_sockets` (vd Docker socket phải khai báo); mạng mở.
- Ghi của Bash trong W nhưng ngoài cwd vẫn bị sandbox chặn như trước (trừ trust: ghi được cả W) — dùng `extra_write_roots`/worktree.
- Worktree mặc định nằm ở `<cha của repo>/_wt/…`: nếu repo = chính `root` thì thư mục này ngoài W → bị từ chối; truyền `path` trong W.
- Đọc `policy/`, skill, memory của hub bằng Read tool bị cấm khi giam (HUB_HOME nằm trong `$HOME`) — agent dùng tool hub-self.
- Không bật `permissions.blockReadsOutsideWorkingDirectories` của CLI mới (phụ thuộc phiên bản; xem `hardening-SEC.md` §1.1).
