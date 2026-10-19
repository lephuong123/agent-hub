# Hardening — luồng SEC (H-01, H-02, H-03, H-05, H-12, H-18 + CI)

Mục tiêu: các bất biến §4.1/3/5 (traceability) đứng vững trước agent chạy CÙNG user OS có Bash/Python. Phòng thủ nhiều lớp;
ba lớp KHÔNG phụ thuộc cách agent viết lệnh: sandbox OS, xác thực người gọi RPC, kiểm toàn vẹn chính sách.

## 1. Spike sandbox OS (claude 2.1.302, haiku, ~$0.005)
`claude --help` không có cờ sandbox, nhưng schema settings của binary có khoá `sandbox` (Seatbelt trên macOS, bubblewrap trên Linux):
`enabled`, `failIfUnavailable`, `autoAllowBashIfSandboxed`, `allowUnsandboxedCommands`, `filesystem.{denyWrite,denyRead,allowWrite}`,
`network.{allowedDomains,allowUnixSockets,allowAllUnixSockets}`. Chạy thật `claude -p --settings <file> --setting-sources ""` với
một script Bash (thư mục `scratchpad/sbx/`). Kết quả:

| Thao tác trong Bash của agent | Kết quả |
|---|---|
| `echo >> policy/…`, `python open(...,'a')`, tạo file mới trong policy/, ghi qua symlink, `rm` policy | **Operation not permitted** |
| `sqlite3`/python ghi `hub.db` | **readonly database** |
| đọc token người / `secrets/` | **Operation not permitted** |
| kết nối Unix socket (không có trong `allowUnixSockets`) | **Operation not permitted** |
| cháu double-fork + `setsid()` kết nối socket sau khi Bash trả về | **bị chặn** (sandbox kế thừa) |
| `launchctl submit …` | **thất bại** (rc=1) |
| ghi trong cwd, ghi `$TMPDIR`, `curl https://…` (`allowedDomains: ["*"]`) | chạy bình thường |

Kết luận: đủ dùng làm lớp chính → `guard/runtime.sandbox_settings` sinh khoá `sandbox` cho settings.json của mọi run khi guard bật
(`guard.sandbox.*`, mặc định bật, `failIfUnavailable: true` = fail-closed). denyWrite: mọi mục của HUB_HOME trừ `agents/<id>/workspaces`
(và tổ tiên của cwd của run, vd bản sao eval), mã nguồn gói `cfharness`, socket + token. denyRead: token, `secrets/`, `agents/*/runtime`,
`mcp_server/tokens.json`. `allowUnsandboxedCommands: false` (bỏ qua `dangerouslyDisableSandbox`), `autoAllowBashIfSandboxed: false`
(hook guard vẫn quyết từng lệnh), `excludedCommands` từ cấu hình bị bỏ. Hook và server MCP do claude chạy NGOÀI sandbox nên hook guard /
hub-self vẫn nói chuyện được với daemon. Giới hạn: sandbox chỉ phủ Bash (Write/Edit/Read do `permissions.deny` + classifier);
Linux cần bubblewrap/socat (thiếu → run lỗi, không chạy không sandbox); thao tác người đã duyệt mà ghi ngoài cwd/tmp vẫn bị sandbox chặn
(thêm `guard.sandbox.extra_allow_write`).

## 2. Từng mã H

| Mã | Đã sửa | Test chứng minh |
|---|---|---|
| H-01 | `auth.py` + `daemon._dispatch`: method trong `auth.HUMAN_ONLY` (approval.*, guard.protect.*, guard.integrity.accept, config.set, learn.*, agent.*, golden ghi, secret.*, mcp ghi, estop, team ghi, run.revert/takeover…) cần **token người** (`<socket>.auth`, 0600, sinh lại mỗi lần daemon khởi tạo, client người tự đọc trong `rpc.with_auth`/`tui/client.py` — không thêm bước tay) **và** người gọi không phải agent: pid ngang hàng do kernel báo (`LOCAL_PEERPID`/`SO_PEERCRED`) → cây ppid chứa pid run, session id = pid run (kể cả run đã kết thúc trong 24 h, đối chiếu thời điểm khởi động để tránh pid tái dùng), hoặc env `CFHARNESS_RUN_ID`. Từ chối → `BLOCKED`, event `auth.denied` + audit + Slack. Classifier: `python -m cfharness*`, import `cfharness` trong mã, `cfharness` qua xargs/find -exec/đối số động, mọi chuỗi chứa socket/`.sock.auth`/token → `forbidden`. | `tests/test_sec_auth.py` (daemon + socket thật, 3 cách của QA từ tiến trình con của "run" thật: `python -m cfharness.cli approve`, `xargs cfharness`, client socket kể cả khi đã đọc được token; double-fork mồ côi; setsid; người vẫn duyệt qua CLI thật), `tests/test_sec_classifier.py` |
| H-02 | Classifier: literal đường dẫn tới tài nguyên bảo vệ / HUB_HOME trong mã python/node/perl/ruby → destructive; `$CFHARNESS_HOME` được giải; symlink tạo trong CÙNG lệnh được theo dõi (`ln -s HUB/policy pol; echo > pol/…`); `ln -s` trỏ vào vùng bảo vệ → destructive. Lớp 3 `guard/integrity.py`: bản chuẩn policy/ + config.yaml trong `integrity_baseline` (migration **v11**); vòng kiểm (`guard.integrity.tick_s`=2 s) + lúc khởi động → khôi phục, bản bị sửa vào `trash/tamper/`, event `guard.tamper_detected` + Slack; config.yaml chỉ khôi phục nhánh an toàn (`guard approval runtime secrets telemetry notify`); `hub.db` bị kết nối khác ghi (`PRAGMA data_version`) hoặc bị thay file (inode) → chỉ phát hiện. Ghi hợp lệ của daemon (PolicyStore, LearningPolicy.set, GoldenStore._mirror, HubConfig.set) được bọc → chụp lại bản chuẩn. Người sửa tay: `guard.integrity.accept {path, content}` (chỉ người). | `tests/test_sec_integrity.py`, `tests/test_sec_classifier.py` |
| H-03 | Sửa regex `\b` trước API dạng phương thức (`Path(..).unlink()`); argv dạng list (`subprocess.run(['rm',…])`), `spawn('rm',[…])`; API ghi (`open(..,'w')`, `write_text`, `writeFileSync`, `File.write`, perl `open('>')`, shutil/os move/copy…) kiểm đích literal, đích động trong mã inline → `uncertain`; `cp -r`/`rsync`/`ditto` nguồn thư mục duyệt file đích đã có; `tar -x`/`unzip` liệt kê archive; `mv` file có sẵn ra ngoài workspace, `trash`, AppleScript delete → destructive; script tạo trong cùng lệnh được phân tích, script chưa tồn tại → `uncertain`. | `tests/test_sec_classifier.py`, `tests/test_guard_classifier.py` (vẫn recall 100%, báo nhầm ≤ 5%) |
| H-05 | Classifier: `learn propose|rollback|retry|refine|policy`, `agent create|update|edit|rm|…`, `config set`, `secret`, `mcp` ghi, `estop`, `team` ghi… → `forbidden`. RPC: `learn.propose` từ người gọi không phải người bị HẠ xuống `created_by="agent"` (gán agent của run, qua guard học đầy đủ); mọi lời gọi ghi từ agent ghi audit `actor=agent` (không tin `_audit` client tự khai). | `test_sec_auth.py::test_agent_learn_propose_downgraded_and_agent_update_denied`, `::test_agent_mutating_calls_are_audited_as_agent` |
| H-12 | `redact.py`: bộ che theo GIÁ TRỊ (nạp SecretStore khi khởi động, khi `secret set`, và giá trị env-fallback đã resolve; cả dạng escape JSON) dùng chung: `Hub.publish` (bus/TUI/Slack), `Store._insert/_update/insert_event`, raw stdout (`RawLog.write`) + stderr run (che sau khi chạy), log daemon (`SecretLogFilter` + handler stderr cho thư viện → `daemon.out`), OTel (che toàn bộ body, thêm mẫu `sk-live-…`). Probe MCP: stderr vào file tạm (`errlog`) rồi che; kết quả probe che. Khởi động: xoá mọi `agents/*/runtime/*` còn sót. | `tests/test_sec_secrets.py` (e2e grep toàn HUB_HOME gồm hub.db/WAL/runs/*.gz/logs; đã thử tắt bộ che → test đỏ), QA `otel_leak.py`, `probe_leak.py` |
| H-18 | `guard/runtime.check_extra_args`: mọi biến thể `--(allow-)dangerously-skip-permissions`, `--permission-mode[= ]bypassPermissions` trên `dir`, và cờ ghi đè cấu hình an toàn (`--settings`, `--setting-sources`, `--mcp-config`, `--permission-prompt-tool`, `--allowedTools`, `--add-dir`…). MCP hub nhận theo lệnh/URL (`GuardService.hub_mcp_servers`) → tool chỉ-người (`hub_agent_update`, `hub_estop`, `hub_approve`, learn/golden/team ghi…) `forbidden` dưới mọi tên đăng ký. | `tests/test_sec_sandbox.py` |
| CI | `.github/workflows/test.yml`: macOS + Ubuntu, `uv sync` + bộ phân loại + toàn bộ `pytest`. | — |

## 3. Giới hạn còn lại
- Không có sandbox (tắt, hoặc Linux thiếu bubblewrap mà `fail_if_unavailable: false`): một tiến trình con của run gọi `setsid()`, double-fork,
  xoá `CFHARNESS_RUN_ID` VÀ đọc được token thì qua được lớp 2. Đọc token phải lách được classifier trước; lớp 3 vẫn khôi phục policy.
- HTTP MCP của hub (`mcp_server/tokens.json`): agent đọc được token control sẽ gọi qua HTTP (tiến trình của người). Chặn bằng sandbox denyRead + classifier.
- Integrity là HẬU KIỂM (cửa sổ ≤ tick_s); `hub.db` chỉ phát hiện, không tự khôi phục.
- Classifier vẫn là phỏng đoán: `vim -c`, `git apply`, `patch`, `make` tuỳ ý, `sed -i` file có sẵn = ghi thường (giữ báo nhầm thấp).
- Chưa làm kiểm chứng thật cuối bằng agent haiku được yêu cầu tự duyệt (3 cách) + ghi policy bằng python. Đã có: spike sandbox thật (§1) và
  test với daemon/socket/tiến trình thật (`test_sec_auth.py`). Nên chạy lần kiểm chứng này trước khi phát hành.

## 4. File của luồng khác đã chạm (ghi chú cho coordinator)
- `runtime/claude_cli.py` (CLI): `build_argv` gọi `guard.runtime.check_extra_args` (thay 2 dòng kiểm cũ).
- `runtime/base.py` (CLI): `RawLog.write` che theo giá trị; `close` che `<run>.stderr.log`.
- `runs.py`: thêm `cwd=cwd` vào `build_settings`.
- Dùng chung (chỉ thêm): `store.py` (v11 + `_mask_secrets`), `hub.py` (gắn integrity, che ở `publish`, dọn runtime lúc start), `config.py`
  (`guard.sandbox`, `guard.integrity`, sự kiện Slack), `events.py` (`guard.tamper_detected`, `auth.denied`), `tui/client.py` (gửi token).

## 5. Kiểm toán vòng 2 (traceability-v2 §3)
| Mã | Đã sửa | Test |
|---|---|---|
| V2-N1 (Cao) | `auth.HUMAN_ONLY` thêm `agent.revert|archive|pause|resume`, `trash.restore|purge`, `task.archive`. Cổng ở TẦNG SERVICE `auth.require_human()` trong `recovery.purge_agent` (lời gọi nội bộ từ `trash.purge` không lách được; vòng retention không có người gọi RPC nên vẫn chạy). Daemon dùng `auth.all_mutating()` (hợp MUTATING của mọi luồng) để xác định người gọi đầy đủ. `auth.AGENT_ALLOWED_MUTATING` = method ghi agent được gọi, mỗi cái kèm lý do. Classifier: `agent revert|pause|resume`, `trash restore|purge`, `task archive` → forbidden. | `tests/test_sec_v2.py::test_every_mutating_method_is_classified` (đỏ nếu luồng nào thêm method ghi mà chưa phân loại), `::test_admin_surface_requires_human_token_over_socket`, `::test_purge_gate_at_service_layer`, `::test_v2_n1_cli_admin_forbidden` |
| V2-N2 (TB) | `classifier.protected_path`: vùng cứng (không cần khai báo) = dữ liệu nội bộ HUB_HOME trừ workspace, môi trường Python của hub (site-packages/bin), nơi chạy trễ (`~/Library/LaunchAgents|LaunchDaemons`, `~/.config/git|systemd|autostart`, `/etc/cron.d`…), `.git/hooks` + `.git/config` ở mọi repo. `crontab` (trừ `-l`), `at`/`batch`, `launchctl load|bootstrap|submit|…`, `systemctl --user enable|start` → destructive (chờ người). Sandbox denyWrite thêm `~/.ssh`, `~/.gitconfig`, `~/.config/git`, LaunchAgents/Daemons, file rc shell, `~/.claude*`, `~/.local/bin`. | `::test_v2_n2_scheduler_commands_held`, `::test_v2_n2_persistence_locations_protected` |
| V2-N3 (Thấp) | Kiểm socket/token trên từ ĐÃ bỏ quote/nối chuỗi; `socat`/`nc` với UNIX/`-U`/đích động → uncertain. Tên lệnh đã chuẩn hoá (`\rm`, `/bin/rm`, `command`, `env`) → cùng phán quyết với `rm` (xoá file run tự tạo vẫn là ghi thường — đúng FR-SG-01). | `::test_v2_n3_command_name_normalized` |

Ghi chú: `agents.revert` (agents.py, luồng CLI) mới chỉ chặn ở tầng RPC; nên gọi `auth.require_human("agent revert")` ở service nếu có lời gọi nội bộ mới.

## 6. Bổ sung theo coordinator (mất dữ liệu trên file có sẵn, FR-TK-04)
- Tái hiện `cfharness guard test`: với file **đã tồn tại** và không do run tạo, `truncate -s 0`, `: >`/`>`, `dd of=`, `cp`/`mv` đè, `tee` (không -a), `shred`
  vốn đã là `destructive` (`overwrite_existing`). Kết quả "ghi" chỉ xảy ra khi đích CHƯA tồn tại lúc kiểm (vd `~/proj/...` không có trên máy) — cắt cụt
  file chưa có là vô hại, giữ là `write`. Đích không giải được (`$VAR`) → `uncertain`.
- Mới: `GuardContext.checkpoint_root` (GuardService lấy từ `runs.checkpoint` kind=git) — file **đã git track** trong repo có checkpoint của run
  ghi đè/xoá được revert → `write` (`overwrite_checkpointed`/`delete_checkpointed`); không có checkpoint → vẫn giữ chờ người.
- Mới: SQL `VACUUM INTO '<file>'` → `sql_vacuum_into` (destructive).
- Test FR-SG-07: `tests/test_sec_classifier.py::test_data_loss_on_existing_files_held`, `::test_nonexistent_targets_are_plain_writes`,
  `::test_checkpointed_git_file_overwrite_is_revertable_write`; báo nhầm trên `SAFE_*` vẫn ≤ 5% (`test_false_positive_rate`).
- FR-TK-04: code đúng (`TaskService._parent_context` đưa `result_summary` + `result_json` của mọi task cha vào prompt task con). Thêm test chặt
  `tests/test_team_task.py::test_fr_tk04_child_prompt_contains_parent_results` (2 cha, kiểm nguyên văn kết quả + id từng cha).

## 7. Kiểm toán web/Telegram — WT-3 (đọc vùng nhạy cảm HUB_HOME khi sandbox tắt)
- `BashAnalyzer.sensitive_sweep` (chạy cho MỌI lệnh con): giải `cd` trong chuỗi (cwd kế thừa), mở glob, mở rộng biến đã biết +
  `$CFHARNESS_HOME`/`$HOME` cho cả nhánh ĐỌC, rồi so mọi đối số/đích với vùng nhạy cảm (`auth/`, `secrets/`, `mcp_server/tokens.json`,
  `hub.db`, `hub.sock*`). Trong vùng → `hub_secret_read` forbidden; lệnh duyệt cây (`find/grep -r/cp -r/rsync/tar/zip/chmod -R/rm -r…`)
  có TỔ TIÊN của vùng → uncertain; đích động/glob không giải được mà có dấu hiệu (`SENSITIVE_HINT`: `auth/`, `secrets/`, `web.json`,
  `tokens.json`, `.sock`, `hub.db`, `CFHARNESS_HOME`) → uncertain. `cd`/`pushd` vào hub home là vô hại (kiểm ở lệnh sau theo cwd mới);
  policy KHÔNG đưa vào vùng này (để `protected_write` giữ `protected_id`, và đọc policy vốn vô hại).
- Cảnh báo sandbox tắt: `Hub._warn_if_sandbox_disabled` — guard bật mà `guard.sandbox.enabled=false` → event đỏ `guard.sandbox_disabled`
  (summary) + audit; `hub.status()` trả `sandbox_enabled` để TUI/web/Telegram hiển thị.
- Test: `tests/test_sec_v2.py::test_wt3_*`, `::test_sandbox_disabled_emits_red_warning`. Toàn bộ pytest xanh (476).

## 8. Ngoại lệ sandbox theo agent: localhost/loopback (chủ sản phẩm quyết — team toa-soan-ia)
**Spike thật** (claude 2.1.302, haiku, tổng ≈ $0.013, `scratchpad/sbx2/`), server thật LISTEN 127.0.0.1:8221:

| Cấu hình `sandbox.network` | curl 127.0.0.1 | curl --noproxy | python connect | bind+tự kết nối | node listen | mạng ngoài |
|---|---|---|---|---|---|---|
| `allowedDomains:["*"]` (mặc định hub) | exit 7 | exit 7 | EPERM | EPERM | EPERM | 200 |
| + `allowLocalBinding: true` | **OK** | **OK** | **OK** | **OK** | **OK** | 200 |
| + thêm `localhost`/`127.0.0.1` vào allowedDomains | OK | OK | OK | OK | OK | 200 (không cần thêm) |

Kết luận: **chỉ `allowLocalBinding: true`** là có tác dụng (bind/listen + kết nối loopback, cả qua proxy lẫn thẳng). Cùng cấu hình đó, đã kiểm
thật: kết nối **Unix socket vẫn EPERM**, `denyRead`/`denyWrite` vẫn chặn (đọc/ghi `auth/` → Operation not permitted).

**Cài đặt:** `GuardService.sandbox_overrides/sandbox_set` lưu ở `hub_state.sandbox_overrides` = `{agent: {allow_localhost, by, channel, at}}` —
NGOÀI cấu hình agent nên vòng học/ledger/proposal, `agent update` (khoá lạ bị từ chối), revert/clone/export không đặt/giữ được. Chỉ người:
RPC `guard.sandbox.set` ∈ `auth.HUMAN_ONLY` + cổng tầng service `auth.require_human`; classifier `cfharness guard sandbox …` (trừ `list`) forbidden.
Audit + event `guard.sandbox_override`. `guard/runtime.sandbox_settings(overrides=…)` chỉ thêm `network.allowLocalBinding` — filesystem,
Unix socket, `allowUnsandboxedCommands=false` giữ nguyên. CLI: `cfharness guard sandbox list | allow-localhost <agent> on|off`
(tương đương `agent edit --sandbox-allow-localhost` coordinator gợi ý). Nhãn: TUI cột tên `⚠lo` (đỏ); `agent.list` trả `sandbox_overrides`
(luồng WEB hiển thị nhãn từ trường này — chưa sửa template web vì là file của luồng WEB).
Test: `tests/test_sec_sandbox_localhost.py`.

**Rủi ro (agent có loopback):** chạm mọi dịch vụ trên 127.0.0.1/::1 của máy: web hub (`cfharness.localhost:8765`, cần phiên — khoá ký ở
`auth/` agent không đọc được), HTTP MCP của hub (cần bearer — `mcp_server/tokens.json` bị denyRead), cổng CDP/remote-debugging của Chrome
CỦA NGƯỜI nếu đang mở (điều khiển trình duyệt đã đăng nhập!), DB/Redis/dev server cục bộ không xác thực, và có thể mở cổng lắng nghe.
Khuyến nghị: Chrome cho agent dùng profile riêng + cổng ngẫu nhiên; không bật remote-debugging trên Chrome cá nhân; DB cục bộ có mật khẩu.

**Đã bật thật** (~/.cfharness, daemon khởi động lại, không run nào đang chạy): toa-lead, dev-soan-thao, dev-du-lieu, dev-giao-dien-quyen,
phep-thu-nguoc, nghiem-thu-chrome. Thăm dò lại `cfharness run nghiem-thu-chrome … --model haiku --budget 0.2 --follow` (run r_7273ecaad352,
$0.0014): `curl http://127.0.0.1:8221/` → 404 rc=0 (trước: exit 7), `net.createServer().listen(0,'127.0.0.1')` → `listen ok 53761` (trước: EPERM).

## 9. Kiểm toán vai trò manager — MGR-1, MGR-2, MGR-8 (phần SEC)
**MGR-1 (thoát sandbox qua PATH).** (a) `workspace.validate_workspace` — dùng ở TẦNG SERVICE khi tạo/sửa agent (`workspace_default`),
tạo task (`workspace.dir`), chạy ad-hoc (`--cwd`), cho CẢ người lẫn manager: workspace `dir` phải nằm trong `workspace.allowed_roots`
(mặc định `~/Workspace` + vùng scratch/worktree/eval/checkpoint của hub) VÀ không trùng/chứa/nằm trong bất kỳ `fixed_protected_dirs`
hoặc protected path nào. (b) `sandbox_settings`: nhánh gỡ denyWrite theo cwd KHÔNG bao giờ gỡ mục thuộc `fixed_protected_dirs`
(HUB_HOME nội bộ, mã cfharness, token/socket, `home_persistence_dirs`, `path_bin_dirs`). (c) `path_bin_dirs()` (mọi thư mục `$PATH`
thuộc `$HOME` + `~/.local/bin`, `~/.cargo/bin`…) thêm vào denyWrite sandbox VÀ `classifier.protected_path` (chặn cả Write tool, không chỉ Bash).
Test: `tests/test_sec_manager.py::test_fixed_protected_dirs_never_dropped_by_cwd`, `::test_path_bin_dirs_*`, `::test_workspace_in_sensitive_dir_rejected_*`, `::test_agent_create_rejects_*`, `::test_run_adhoc_cwd_*`.

**MGR-2 (lách mẫu chặn bằng agent khác trên cùng repo).** Chính sách theo repo do NGƯỜI đặt: `policy/repo_deny.yaml`
(`PolicyStore.repo_deny_*`, bọc integrity — chụp lại bản chuẩn khi ghi hợp lệ, nếu không sẽ bị khôi phục như tamper). RPC
`guard.repo_deny.list|add|rm` (add/rm ∈ `auth.HUMAN_ONLY`, audit); CLI `cfharness guard repo-deny list|add <repo> "<pattern>"|rm`.
Áp cho MỌI run có cwd trong repo (qua `git_root`), bất kể agent: vào `disallowedTools` tĩnh (`runs.py`) VÀ classifier (`GuardContext.repo_deny`
→ `repo_deny` destructive). Manager/agent đặt mẫu → forbidden. Classifier forbidden cho `cfharness guard repo-deny add|rm` từ agent.
Đã chuyển 7 mẫu của toa-soan-ia (`npm run deploy|setup-db|seed-prompt|chuyen-kho*`, `git push`) sang repo `/Users/phuonglq/Workspace/toa-soan-ia`
trong HUB_HOME thật (mẫu trên 5 agent giữ nguyên). Test: `::test_repo_deny_applies_to_any_agent_in_repo`, `::test_repo_deny_set_is_human_only`, `::test_repo_deny_cli_forbidden_from_agent`.

**MGR-8 (manager sửa policy → approval thay vì cấm).** `GuardContext.manager_run` (run có `run_opts.hub_self=manager`): mọi verdict
lớp `policy|hub|guard` (kể cả ghi `policy/`) → `forbidden` thẳng, không mở approval (người không bị ép bấm duyệt thay đổi chính sách).
Test: `::test_manager_run_policy_edit_is_forbidden_not_approval`.

**Fixture test.** `conftest._allow_pytest_tmp_workspaces` (autouse, session): cho phép gốc tạm của pytest qua `workspace._EXTRA_ALLOWED_ROOTS`
(cùng tiến trình) + biến môi trường `CFHARNESS_EXTRA_WORKSPACE_ROOTS` (daemon tiến trình con `spawn_background` kế thừa) — CHỈ test, sản
phẩm không bao giờ đặt. Toàn bộ pytest xanh (554, 1 skip), chạy 2 lần. Hub thật: 6 agent toa-soan-ia (`~/Workspace/toa-soan-ia`) và
`dieu-phoi` (scratch) hợp lệ với `allowed_roots` mặc định `~/Workspace`.

**Rủi ro tồn dư.** `agents.revert` (luồng CLI) đã HUMAN_ONLY; `validate_workspace` phụ thuộc `git_root` cho repo-deny (symlink/bind-mount ra
ngoài repo có thể lệch — sandbox denyWrite vẫn là lớp chặn ghi). `_EXTRA_ALLOWED_ROOTS` là lối mở rộng chỉ-test; không đọc ở đường sản phẩm.

## 10. Kiểm toán manager vòng 2 — R2-1, R2-2, R2-4, R2-5 (phần SEC; ESTOP do coordinator bật, KHÔNG tắt)
**R2-1 (Nghiêm trọng — tool Write/Edit không chịu sandbox).** Nhiều lớp:
- `guard/runtime.executable_zones()`: mọi thư mục `$PATH` (tuyệt đối), `sys.prefix`/`base_prefix` + `sysconfig` purelib/platlib/scripts/data của
  interpreter chạy hub, `.venv` + TOÀN BỘ repo mã cfharness (`hub_repo_root()`), `~/.local/share/uv`, `/opt/homebrew`, `/usr/local`, dotfile công cụ
  (`~/.npmrc`, `~/.yarnrc*`, `~/.config/uv|pip|npm|gh`, `~/.pythonrc*`, `~/.tmux.conf`, `~/.netrc`, `~/.docker`…). Dùng ở 4 chỗ:
  (1) classifier `classify_file_write`: đích (đã realpath) trong vùng này → `exec_zone_write` **forbidden**; ngoài `write_roots` (cwd run ∪ /tmp ∪
  $TMPDIR ∪ `guard.sandbox.extra_allow_write`) → `write_outside_workspace` destructive (chờ người); `protected_path` cũng trả `exec-zone` (Bash).
  (2) `permissions.deny` tĩnh của Claude Code `Write|Edit|MultiEdit|NotebookEdit(//zone/**)` — **kiểm thật** (haiku, $0.005): Write vào thư mục bị
  deny → "denied by your permission settings" kể cả `--permission-mode acceptEdits`; Write qua SYMLINK trong workspace trỏ vào thư mục deny →
  cũng bị chặn (Claude giải symlink). (3) sandbox denyWrite cho Bash (gồm venv của hub). (4) `fixed_protected_dirs` → `validate_workspace`
  từ chối workspace trong repo hub (`tests/`, `docs/`, `.git`).
- R2-1(c): `guard.integrity.check_python_env` lúc hub khởi động: bản chuẩn `{*.pth, sitecustomize.py, usercustomize.py}` của site-packages
  (`hub_state.pyenv_baseline`); thêm/sửa → event đỏ `guard.env_tamper` + audit + Slack (Telegram: luồng TG thêm định dạng nếu muốn). Người cài gói
  hợp lệ: `cfharness guard integrity accept` (nhận cả bản chuẩn Python env). Không tự xoá file.
Test: `tests/test_sec_r2.py::test_write_tool_into_exec_zones_forbidden`, `::test_write_tool_outside_workspace_needs_human`,
`::test_write_tool_symlink_into_exec_zone_resolved`, `::test_static_deny_and_sandbox_cover_exec_zones`, `::test_workspace_cannot_be_inside_hub_repo`,
`::test_python_env_tamper_detected`.

**R2-2 (Cao — lách repo_deny).** Chuyển trọng tâm sang TÀI NGUYÊN, repo_deny thành lớp phụ:
- Đọc `toa-soan-ia`: `deploy` = `scripts/deploy.sh` → build `.next-build`, HOÁN ĐỔI vào `.next` (prod, chạy bởi launchd) + `.next-old`,
  `launchctl kickstart`; `setup-db|seed-prompt|chuyen-kho*|…` = `tsx --env-file=.env.local scripts/*` → NocoDB `127.0.0.1:8080` bằng token trong
  `.env.local`. Bản `:dev` thêm `--env-file=.env.development.local` chỉ đổi `NOCODB_BASE_TITLE` (CÙNG host + token).
- Đã đặt trên HUB_HOME thật (người, qua CLI): `guard protect add path <repo>/.next` và `<repo>/.next-old` (overwrite,write) — tài nguyên người khai
  báo nay vào cả sandbox denyWrite (không gỡ theo cwd) VÀ `permissions.deny` tĩnh cho tool ghi (`protected_write_paths`); `validate_workspace`
  cho phép tài nguyên người khai báo NẰM TRONG workspace. `launchctl bootout|unload|kill|stop|remove|disable` → destructive (thêm vào V2-N2).
  Thêm repo_deny `Bash(tsx --env-file=.env.local scripts/:*)` (chỉ khớp bản prod; `:dev` có `--env-file` thứ hai trước `scripts/`).
- `git push`: sandbox `denyRead` `~/.ssh`, `~/.git-credentials`, `~/.config/gh`, `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.docker/config.json`…;
  `guard.sandbox.allow_ssh_agent` mặc định **False**. Kiểm thật: trong sandbox `ssh-add -l` → "Error connecting to agent: Operation not permitted",
  `ls ~/.ssh`/`~/.config/gh` → Operation not permitted; `git credential-osxkeychain get` trả rỗng (chưa xác nhận được là bị chặn hay chưa lưu);
  `git ls-remote` repo công khai vẫn được (không cần xác thực).
- repo_deny (lớp phụ) khớp theo REPO ĐÍCH trên lệnh ĐÃ CHUẨN HOÁ: git bỏ `-C/-c/--no-pager/--git-dir`, đường dẫn tuyệt đối, `command`; `send-pack`=push;
  `npm|pnpm|yarn|bun` bỏ cờ toàn cục, `run|run-script|rum|urn`, `--prefix/-C/--cwd`, `pnpm/yarn/bun <script>`, `npm exec --` → phân loại lệnh bên trong;
  **giải script trong package.json và phân loại THÂN script** (cả script agent mới thêm); chạy file theo đường dẫn (`./x.sh`) → đọc nội dung; tên script
  động/qua xargs → uncertain; literal `npm|tsx|launchctl|make…` trong mã node/python → phân tích; `cd X &&` → repo X; clone/worktree NGOÀI repo cùng
  remote `origin` → cùng chính sách; tiền tố mẫu `forbid:` → forbidden (không mở approval). Test: `::test_repo_deny_variants_in_repo_held` (31 dạng QA),
  `::test_repo_deny_from_outside_cwd_and_clones_held`, `::test_repo_deny_benign_dev_still_allowed`, `::test_repo_deny_forbid_prefix_is_forbidden`,
  `::test_git_credentials_not_readable_in_sandbox`, `::test_human_protected_resource_enters_sandbox_and_static_deny`.
- **Giới hạn còn lại:** NocoDB prod/dev dùng CÙNG host + token (`127.0.0.1:8080`) — không tách được bằng tài nguyên; agent có ngoại lệ loopback
  + đọc được `.env.local` (Next cần cho `npm run dev`) có thể tự gọi API NocoDB prod bằng `curl`/script mới. Khuyến nghị chủ sản phẩm: token NocoDB
  RIÊNG cho dev (quyền chỉ trên base dev) đặt trong `.env.development.local`, bỏ token prod khỏi máy agent dùng (hoặc NocoDB dev riêng cổng) — khi đó
  denyRead `.env.local` + chặn cổng prod được. `make <target>` chưa giải Makefile. Mẫu `:*` là tiền tố: `Bash(npm run setup-db:*)` cũng khớp
  `setup-db:dev` (người có thể đổi sang mẫu chính xác). Keychain helper: chưa chứng minh được bị chặn.

**R2-4.** Classifier: `git config` ghi khoá CHẠY LỆNH (`core.fsmonitor|hooksPath|sshCommand|pager|editor`, `alias.*`, `filter.*.clean|smudge`,
`diff.*.textconv`, `credential.*helper`, `include*`…) → destructive; ghi `.git/hooks` đã là protected (V2-N2). Daemon/guard chạy git với
`-c core.fsmonitor=false -c core.hooksPath=/dev/null -c core.sshCommand= …` + `GIT_CONFIG_NOSYSTEM=1` (`workspace.GIT_SAFE_ARGS`) —
kiểm thật: fsmonitor cài trong `.git/config` KHÔNG chạy khi daemon `git status`. Test: `::test_git_config_exec_keys_destructive`, `::test_daemon_git_disables_exec_config`.

**R2-5.** `_prepare_spec` kiểm lại `validate_workspace(realpath(cwd))` lúc spawn (dir/worktree) — workspace đổi thành symlink ra ngoài gốc sau khi đặt
→ run lỗi. Test: `::test_workspace_rechecked_at_spawn`.

Toàn bộ pytest xanh 2 lần (574, 1 skip). Hub thật: daemon đã khởi động lại (không run nào chạy, ESTOP giữ BẬT); toa-soan-ia + dieu-phoi hợp lệ;
`guard test` thật: `npm run-script deploy`, `git -C . push`, `npm run setup-db`, `tsx --env-file=.env.local scripts/…` → destructive; `npm run dev` → write;
Write `.pth`/`~/.npmrc`/`tests/conftest.py` của hub/`/opt/homebrew/bin` → forbidden.

## 11. Kiểm toán manager vòng 3 — R3-1 (CAO): token forge trong keychain đọc được trong sandbox
**Bản chất.** Hồ sơ Seatbelt của Claude Code cài CỨNG `(allow mach-lookup (global-name "com.apple.SecurityServer"))` (thấy trong binary);
khoá `sandbox.network.allowMachLookup` chỉ THÊM dịch vụ, không gỡ được SecurityServer. Nên `security find-generic-password … -w` chạy rc=0
trong sandbox → đọc token GitHub → `git push` mã độc. Không có settings nào tắt mach-lookup này.

**Sửa nhiều lớp.**
1. Classifier + deny tĩnh (phụ thuộc chuỗi lệnh nhưng là lớp đầu): `security find-*-password|dump-keychain|export|find-certificate|find-identity|
   unlock-keychain|…` → forbidden; `git credential (fill|approve|…)`, `git-credential-*`, `git config credential.*` → forbidden; `gh auth
   token|status --show-token|refresh|login|setup-git` → forbidden; `osascript` chạm keychain/password → forbidden; mã python/node gọi
   `Security.framework`/`SecKeychain*`/`SecItemCopyMatching`/`keytar`/`keyring.get_password`/`git credential`/`gh auth token` → forbidden
   (`KEYCHAIN_CODE_RE`); đọc `~/.config/gh`, `~/.git-credentials`, `~/Library/Keychains`, `~/.netrc`, `~/.ssh` qua Read/Bash → forbidden
   (`sensitive_read` + `sensitive_regions` + `permissions.deny` tĩnh `Read|Grep|Glob(…)`). Sandbox `denyRead` cũng thêm `~/Library/Keychains`.
2. Lớp KHÔNG phụ thuộc chuỗi lệnh — **giới hạn MẠNG**: `sandbox.network.allowedDomains` mặc định HẸP (`DEFAULT_ALLOWED_DOMAINS`:
   registry npm/pypi/yarn + `*.anthropic.com`), **không** github/gitlab/bitbucket. `network_domains()` cộng thêm: domain người mở theo agent
   (`guard.sandbox.allow_domain`, HUMAN_ONLY, audit + event, nhãn TUI `⚠net`) + HOST của mọi MCP server gán cho agent (tự lấy từ URL —
   `agent_mcp_domains`) + loopback nếu agent có ngoại lệ localhost. **Kiểm thật** (haiku, $0.003): cùng cấu hình, `security find-generic-password`
   vẫn rc=0 (đọc được token — xác nhận sandbox KHÔNG chặn keychain), NHƯNG `curl https://github.com`/`api.github.com` và `git ls-remote github`
   → `deny network-outbound github.com:443 (user denied)` (block); `registry.npmjs.org` → 200; loopback → 404 (vẫn chạy). Vậy token đọc được
   cũng không đẩy ra forge được.
CLI: `cfharness guard sandbox allow-domain <agent> <domain> [--remove]`; `guard sandbox list` hiện cột "DOMAIN MỞ THÊM".
Test: `tests/test_sec_r3.py` (lệnh/mã keychain forbidden, đọc file xác thực forbidden, default domains loại forge, allow-domain HUMAN_ONLY +
audit + CLI forbidden từ agent, host MCP của agent tự vào domain).

**Giới hạn còn lại (§11).**
- Keychain VẪN đọc được trong sandbox (giới hạn của Claude Code); phòng thủ là chặn lệnh (lách được nếu có cách gọi SecurityServer mà
  `KEYCHAIN_CODE_RE`/deny không bắt — vd binary biên dịch sẵn người để trong workspace) + chặn ĐƯỜNG RA mạng. Khi `allowedDomains` chứa `*`
  (người đặt lại) hoặc agent được mở `github.com`, lớp mạng mất tác dụng cho agent đó.
- `allowed_domains` theo HUB (một danh sách), per-agent chỉ CỘNG THÊM — không thu hẹp dưới mặc định.
- Agent dùng MCP forge-like hoặc proxy nội bộ có thể gián tiếp ra ngoài; wildcard domain của MCP không được (chỉ host chính xác từ URL).
- `curl https://github.com` vẫn phân loại `write` ở classifier (lớp chặn là sandbox mạng, không phải classifier) — khi sandbox tắt thì không còn chặn.
- Mạng hẹp có thể chặn agent lấy nguồn tin ngoài web: người mở domain cần dùng cho từng agent. toa-soan-ia: MCP `toasoan.cchchn.com` tự được thêm;
  các host khác (*.trungtamdhs.com, phuongthuongcat.com…) chỉ vào nếu là URL MCP gán cho agent, nếu không người mở bằng `allow-domain`.

Toàn bộ pytest xanh 2 lần (583, 1 skip). Daemon thật khởi động lại (không run nào chạy, ESTOP giữ BẬT); `guard test` thật:
`security find-generic-password`/`gh auth token`/`git credential fill` → forbidden; MCP domain của toa agent (`toasoan.cchchn.com`) tự vào allowlist.
