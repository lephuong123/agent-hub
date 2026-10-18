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
(và tổ tiên của cwd của run, vd bản sao eval), mã nguồn gói `agent_hub`, socket + token. denyRead: token, `secrets/`, `agents/*/runtime`,
`mcp_server/tokens.json`. `allowUnsandboxedCommands: false` (bỏ qua `dangerouslyDisableSandbox`), `autoAllowBashIfSandboxed: false`
(hook guard vẫn quyết từng lệnh), `excludedCommands` từ cấu hình bị bỏ. Hook và server MCP do claude chạy NGOÀI sandbox nên hook guard /
hub-self vẫn nói chuyện được với daemon. Giới hạn: sandbox chỉ phủ Bash (Write/Edit/Read do `permissions.deny` + classifier);
Linux cần bubblewrap/socat (thiếu → run lỗi, không chạy không sandbox); thao tác người đã duyệt mà ghi ngoài cwd/tmp vẫn bị sandbox chặn
(thêm `guard.sandbox.extra_allow_write`).

## 2. Từng mã H

| Mã | Đã sửa | Test chứng minh |
|---|---|---|
| H-01 | `auth.py` + `daemon._dispatch`: method trong `auth.HUMAN_ONLY` (approval.*, guard.protect.*, guard.integrity.accept, config.set, learn.*, agent.*, golden ghi, secret.*, mcp ghi, estop, team ghi, run.revert/takeover…) cần **token người** (`<socket>.auth`, 0600, sinh lại mỗi lần daemon khởi tạo, client người tự đọc trong `rpc.with_auth`/`tui/client.py` — không thêm bước tay) **và** người gọi không phải agent: pid ngang hàng do kernel báo (`LOCAL_PEERPID`/`SO_PEERCRED`) → cây ppid chứa pid run, session id = pid run (kể cả run đã kết thúc trong 24 h, đối chiếu thời điểm khởi động để tránh pid tái dùng), hoặc env `AGENT_HUB_RUN_ID`. Từ chối → `BLOCKED`, event `auth.denied` + audit + Slack. Classifier: `python -m agent_hub*`, import `agent_hub` trong mã, `agent-hub` qua xargs/find -exec/đối số động, mọi chuỗi chứa socket/`.sock.auth`/token → `forbidden`. | `tests/test_sec_auth.py` (daemon + socket thật, 3 cách của QA từ tiến trình con của "run" thật: `python -m agent_hub.cli approve`, `xargs agent-hub`, client socket kể cả khi đã đọc được token; double-fork mồ côi; setsid; người vẫn duyệt qua CLI thật), `tests/test_sec_classifier.py` |
| H-02 | Classifier: literal đường dẫn tới tài nguyên bảo vệ / HUB_HOME trong mã python/node/perl/ruby → destructive; `$AGENT_HUB_HOME` được giải; symlink tạo trong CÙNG lệnh được theo dõi (`ln -s HUB/policy pol; echo > pol/…`); `ln -s` trỏ vào vùng bảo vệ → destructive. Lớp 3 `guard/integrity.py`: bản chuẩn policy/ + config.yaml trong `integrity_baseline` (migration **v11**); vòng kiểm (`guard.integrity.tick_s`=2 s) + lúc khởi động → khôi phục, bản bị sửa vào `trash/tamper/`, event `guard.tamper_detected` + Slack; config.yaml chỉ khôi phục nhánh an toàn (`guard approval runtime secrets telemetry notify`); `hub.db` bị kết nối khác ghi (`PRAGMA data_version`) hoặc bị thay file (inode) → chỉ phát hiện. Ghi hợp lệ của daemon (PolicyStore, LearningPolicy.set, GoldenStore._mirror, HubConfig.set) được bọc → chụp lại bản chuẩn. Người sửa tay: `guard.integrity.accept {path, content}` (chỉ người). | `tests/test_sec_integrity.py`, `tests/test_sec_classifier.py` |
| H-03 | Sửa regex `\b` trước API dạng phương thức (`Path(..).unlink()`); argv dạng list (`subprocess.run(['rm',…])`), `spawn('rm',[…])`; API ghi (`open(..,'w')`, `write_text`, `writeFileSync`, `File.write`, perl `open('>')`, shutil/os move/copy…) kiểm đích literal, đích động trong mã inline → `uncertain`; `cp -r`/`rsync`/`ditto` nguồn thư mục duyệt file đích đã có; `tar -x`/`unzip` liệt kê archive; `mv` file có sẵn ra ngoài workspace, `trash`, AppleScript delete → destructive; script tạo trong cùng lệnh được phân tích, script chưa tồn tại → `uncertain`. | `tests/test_sec_classifier.py`, `tests/test_guard_classifier.py` (vẫn recall 100%, báo nhầm ≤ 5%) |
| H-05 | Classifier: `learn propose|rollback|retry|refine|policy`, `agent create|update|edit|rm|…`, `config set`, `secret`, `mcp` ghi, `estop`, `team` ghi… → `forbidden`. RPC: `learn.propose` từ người gọi không phải người bị HẠ xuống `created_by="agent"` (gán agent của run, qua guard học đầy đủ); mọi lời gọi ghi từ agent ghi audit `actor=agent` (không tin `_audit` client tự khai). | `test_sec_auth.py::test_agent_learn_propose_downgraded_and_agent_update_denied`, `::test_agent_mutating_calls_are_audited_as_agent` |
| H-12 | `redact.py`: bộ che theo GIÁ TRỊ (nạp SecretStore khi khởi động, khi `secret set`, và giá trị env-fallback đã resolve; cả dạng escape JSON) dùng chung: `Hub.publish` (bus/TUI/Slack), `Store._insert/_update/insert_event`, raw stdout (`RawLog.write`) + stderr run (che sau khi chạy), log daemon (`SecretLogFilter` + handler stderr cho thư viện → `daemon.out`), OTel (che toàn bộ body, thêm mẫu `sk-live-…`). Probe MCP: stderr vào file tạm (`errlog`) rồi che; kết quả probe che. Khởi động: xoá mọi `agents/*/runtime/*` còn sót. | `tests/test_sec_secrets.py` (e2e grep toàn HUB_HOME gồm hub.db/WAL/runs/*.gz/logs; đã thử tắt bộ che → test đỏ), QA `otel_leak.py`, `probe_leak.py` |
| H-18 | `guard/runtime.check_extra_args`: mọi biến thể `--(allow-)dangerously-skip-permissions`, `--permission-mode[= ]bypassPermissions` trên `dir`, và cờ ghi đè cấu hình an toàn (`--settings`, `--setting-sources`, `--mcp-config`, `--permission-prompt-tool`, `--allowedTools`, `--add-dir`…). MCP hub nhận theo lệnh/URL (`GuardService.hub_mcp_servers`) → tool chỉ-người (`hub_agent_update`, `hub_estop`, `hub_approve`, learn/golden/team ghi…) `forbidden` dưới mọi tên đăng ký. | `tests/test_sec_sandbox.py` |
| CI | `.github/workflows/test.yml`: macOS + Ubuntu, `uv sync` + bộ phân loại + toàn bộ `pytest`. | — |

## 3. Giới hạn còn lại
- Không có sandbox (tắt, hoặc Linux thiếu bubblewrap mà `fail_if_unavailable: false`): một tiến trình con của run gọi `setsid()`, double-fork,
  xoá `AGENT_HUB_RUN_ID` VÀ đọc được token thì qua được lớp 2. Đọc token phải lách được classifier trước; lớp 3 vẫn khôi phục policy.
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
- Tái hiện `agent-hub guard test`: với file **đã tồn tại** và không do run tạo, `truncate -s 0`, `: >`/`>`, `dd of=`, `cp`/`mv` đè, `tee` (không -a), `shred`
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
  `$AGENT_HUB_HOME`/`$HOME` cho cả nhánh ĐỌC, rồi so mọi đối số/đích với vùng nhạy cảm (`auth/`, `secrets/`, `mcp_server/tokens.json`,
  `hub.db`, `hub.sock*`). Trong vùng → `hub_secret_read` forbidden; lệnh duyệt cây (`find/grep -r/cp -r/rsync/tar/zip/chmod -R/rm -r…`)
  có TỔ TIÊN của vùng → uncertain; đích động/glob không giải được mà có dấu hiệu (`SENSITIVE_HINT`: `auth/`, `secrets/`, `web.json`,
  `tokens.json`, `.sock`, `hub.db`, `AGENT_HUB_HOME`) → uncertain. `cd`/`pushd` vào hub home là vô hại (kiểm ở lệnh sau theo cwd mới);
  policy KHÔNG đưa vào vùng này (để `protected_write` giữ `protected_id`, và đọc policy vốn vô hại).
- Cảnh báo sandbox tắt: `Hub._warn_if_sandbox_disabled` — guard bật mà `guard.sandbox.enabled=false` → event đỏ `guard.sandbox_disabled`
  (summary) + audit; `hub.status()` trả `sandbox_enabled` để TUI/web/Telegram hiển thị.
- Test: `tests/test_sec_v2.py::test_wt3_*`, `::test_sandbox_disabled_emits_red_warning`. Toàn bộ pytest xanh (476).
