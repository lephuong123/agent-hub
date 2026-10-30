# Ma trận truy vết nghiệm thu v2 — agent-hub (BRD 0.6) — sau hardening SEC/LRN/CLI

| Mục | Nội dung |
|---|---|
| Ngày kiểm | 2026-10-18 (vòng 2, sau 3 luồng hardening) |
| Người kiểm | QA độc lập (agent), không sửa code, không gọi `claude` thật |
| Nguồn đối chiếu | `docs/BRD-chi-tiet.md` 0.6 ↔ `src/agent_hub/`, `tests/`; `docs/plan/hardening.md`, `docs/dev-notes/hardening-{SEC,LRN,CLI}.md`; so với `docs/acceptance/traceability.md` (v1) |
| Cách kiểm | Chạy LẠI toàn bộ script tái hiện của v1 (scratchpad `audit/`) + script lách MỚI (`audit/v2/`); đọc code `auth.py`, `guard/runtime.py`, `daemon.py`, `guard/service.py`; chạy test theo mã H |
| Test suite | `uv run pytest -q` → **416 passed** (144 s) (v1: 313). SEC 43/43, hardening-CLI+LRN+event_schema 59/59 |
| Kết quả hardening | **21/21 mã H đã sửa và kiểm chứng**; 3 bất biến bị vi phạm ở v1 (1, 3, 4) nay đứng vững; bất biến 5 cải thiện. **Phát hiện 1 lỗ MỚI mức Cao (V2-N1)** do lệch phối hợp giữa luồng CLI và SEC |

**Quy ước trạng thái** như v1. Cột **So với v1**: ↑ nâng (một phần/thiếu/lệch → đạt), = giữ nguyên, ↓ hạ, ✎ ghi chú mới.

---

## 1. Bảng truy vết (chỉ các dòng đổi trạng thái liệt kê chi tiết; dòng "= v1" giữ nguyên kết luận v1)

### 1.1 FR-AG — Quản lý agent

| Mã | Ưu tiên | v2 | So với v1 | Bằng chứng / ghi chú |
|---|---|---|---|---|
| FR-AG-01 | Must | Đạt | ✎ | = v1. Thêm `RESERVED_IDS` cấm id trùng từ khoá CLI (H-08) — lệch BRD, xem §3 |
| FR-AG-02 | Must | Đạt | ↑ | H-08/Thấp: `agent list --team/--status`, `hub_list_agents(team,status)`. `tests/test_hardening_cli.py` |
| FR-AG-03 | Must | **Một phần** | ↑ rồi ✎ | H-07 thêm `agent revert --to N` (test `test_h07_*`, QA chạy). **NHƯNG V2-N1**: `agent.revert` qua RPC KHÔNG đòi token người → agent khôi phục revision cũ (QA tái hiện: `allow_bypass` bật lại). Xem §4 |
| FR-AG-04 | Must | Đạt | ↑ | H-06: `rm` kiểm thành viên team, `--detach-teams`, event `agent.archived`. `test_h06_*` |
| FR-AG-05..07 | | Đạt | = | = v1 |
| FR-AG-08 | Should | Đạt | ↑ | CLI: `doctor <agent>` probe thật từng MCP + tỉ lệ event lạ. `test_low_doctor_probes_agent_mcp` |

### 1.2 FR-TM — Team: tất cả = v1 (Đạt). `team purge` vẫn backlog.

### 1.3 FR-TK — Task & điều phối

| Mã | Ưu tiên | v2 | So với v1 | Ghi chú |
|---|---|---|---|---|
| FR-TK-01 | Must | Đạt | ✎ | H-08 thêm `run … --detach` (cú pháp BRD không còn lỗi argparse) |
| FR-TK-04 | Must | Một phần | = | Assert prompt yếu (thiếu ngoặc) — hardening không chạm |
| FR-TK-06 | Must | Lệch có chủ ý | = | = v1 |
| FR-TK-10 | Must | Đạt | ↑ | H-14 thêm `hub_estop(team)` |
| các mã khác | | | = | = v1 |

### 1.4 FR-RT — Runtime adapter

| Mã | Ưu tiên | v2 | So với v1 | Bằng chứng / ghi chú |
|---|---|---|---|---|
| FR-RT-02 | Must | Đạt | ↑ | H-10: `runtime.claude.flag_matrix`; `build_argv(cli_version)` → `ArgvError`, KHÔNG spawn khi cờ lạ. `test_h10_spawn_refused_with_unsupported_flag` |
| FR-RT-09 | Must | Đạt | ✎ | = v1 + lớp sandbox OS (guard/runtime). Vẫn `default_permission_mode=None` (lệch có chủ ý) |
| FR-RT-10 | Must | Đạt | ↑ | H-15: charter team vào system prompt (khối 3, ổn định theo (agent,team)); `--system-prompt-snapshot=on` mặc định. `test_h15_*` |
| FR-RT-12 | Should | Đạt | ↑ | H-21: giữ khoá repo ĐỒNG BỘ trong `tick` trước `create_task`; QA tái hiện: task thứ hai `ready/workspace_locked`, **0 run**, `consecutive_failures=0`. Worktree checkpoint vẫn backlog |
| FR-RT-15 | Must | Đạt | ↑ | H-10: kiểm phiên bản lúc `Hub.start` (`hub.compat`), cảnh báo đỏ `raw.unknown` > 5%/giờ + gợi ý ESTOP. QA chạy `exp_misc`: `cli.version_warning` cho CLI 9.9.9 |
| FR-RT-17 | Must | Đạt | ↑ | H-11: hook quan sát `PostToolUse/UserPromptSubmit/Stop` fail-OPEN (`observe_hook.py`, timeout 2 s, luôn exit 0), phát `hook.timeout`, spool khi daemon kẹt. `test_h11_*` |
| các mã khác | | | = | = v1 (RT-01,03,04,05,06,07,08,11,13,14,16,18,19) |

### 1.5 FR-LN — Tự cải thiện

| Mã | Ưu tiên | v2 | So với v1 | Bằng chứng / ghi chú |
|---|---|---|---|---|
| FR-LN-05 | Must | Đạt | ↑ | H-05 bịt đường `learn propose` mạo danh người (RPC hạ `created_by="agent"`). `auto_apply_max_items` nay có mặc định (`config.py:153`) |
| FR-LN-06 | Must | Đạt | ↑ | H-04: `Ledger._rollback_one` chỉ khôi phục khoá của entry + kiểm xung đột; QA tái hiện (`repro_cfg.py`): rollback `effort` của review GIỮ `model=haiku` của người |
| FR-LN-10 | Must | Đạt | ↑ | H-15 (charter + snapshot) |
| FR-LN-15 | Must | Đạt | ↑ | H-05: classifier `forbidden` cho `learn/agent/config/secret/mcp/estop/team` ghi; RPC hạ propose. `test_sec_auth`, `test_sec_classifier` |
| FR-LN-16 | Must | Đạt | ↑ (lệch→đạt) | Quyết định coordinator: canary/eval/regression **không bao giờ** rollback entry `actor=user`; thay đổi của người áp ngay, chỉ cảnh báo. BRD cần cập nhật (§3) |
| FR-LN-17 | Must | Đạt | ↑ | H-04 (ledger thật) + lọc actor. FakeLedger đã thay bằng Ledger thật cho ca người sửa xen giữa |
| FR-LN-13 | Must | Đạt | ↑ | H-16: `TopApp` nghe `learning.applied` → toast "X learned: …" |
| các mã khác | | | = | = v1 (LN-01..04,07,08,09(lệch),11,12,14,18,19) |

### 1.6 FR-MS — MCP server của hub

| Mã | Ưu tiên | v2 | So với v1 | Bằng chứng |
|---|---|---|---|---|
| FR-MS-02 | Must | Đạt | ↑ | H-14: `hub_policy_get`, `hub_memory_search`; tham số `hub_events(agent_id)`, `hub_cancel(task_id)`, `hub_estop(team)`, `hub_list_agents(team,status)`, `hub_agent_archive(detach_teams)` |
| FR-MS-05 | Must | Đạt | ↑ | H-14: assert `open_requests` có `tool_name`+`tool_input` trong `hub_events` |
| FR-MS-08 | Must | Đạt | ↑ | H-14: `memory_search` cho worker/lead/reviewer (`memory.search_self` ràng agent của run) |
| các mã khác | | | = | = v1 (MS-01,03,04,06,07) |

### 1.7 FR-MC — MCP client cho agent

| Mã | Ưu tiên | v2 | So với v1 | Bằng chứng |
|---|---|---|---|---|
| FR-MC-02 | Must | Đạt | ↑ | H-13: `tool_rules_for_servers` sinh `--allowedTools/--disallowedTools` cho server chưa probe. QA `secret_e2e.py`: argv có `mcp__vb__vb_tim`, `mcp__vb__vb_doc` |
| FR-MC-03 | Must | Đạt | ↑ | H-13: classify untrusted → approval; `test_h13_untrusted_mcp_tool_opens_approval` |
| FR-MC-05 | Must | Đạt | ↑ | H-12: che theo GIÁ TRỊ cho RawLog/stderr/events/log/OTel; QA `secret_e2e.py` grep toàn HUB_HOME = 0 |
| các mã khác | | | = | = v1 (MC-01(lệch),04,06) |

### 1.8 FR-UI

| Mã | Ưu tiên | v2 | So với v1 | Bằng chứng |
|---|---|---|---|---|
| FR-UI-01 | Must | Đạt | ↑ | H-16: phím `f` feedback trên `top` |
| FR-UI-05 | Must | Đạt | ↑ | H-08: `stop <run|agent>` |
| FR-UI-06 | Must | Đạt | ↑ | H-16: phím `L` bật/tắt học, `b` Ledger theo agent |
| FR-UI-08 | Must | Đạt | ↑ | H-08: 18 lệnh/alias; `run list|show|logs|stop` KHÔNG còn bị parse thành `run <agent>`; `RESERVED_IDS`. Nhiều lệch BRD (§3) |
| các mã khác | | | = | = v1 (UI-02,03,04,07(MP),09,10) |

### 1.9 FR-OB

| Mã | Ưu tiên | v2 | So với v1 | Bằng chứng |
|---|---|---|---|---|
| FR-OB-07 | Could | Đạt | ↑ | H-12: QA `otel_leak.py` → `sk-live-…` KHÔNG lọt span khi `include_payloads=true` (LEAK=0) |
| FR-OB-03 | Must | Đạt | ✎ | H-19: trừ ngân sách run đang chạy — xem NFR-17 |
| các mã khác | | | = | = v1 |

### 1.10 FR-SG — Bảo vệ thao tác phá huỷ

| Mã | Ưu tiên | v2 | So với v1 | Bằng chứng / ghi chú |
|---|---|---|---|---|
| FR-SG-01 | Must | **Một phần** | ✎ | H-03 mở rộng classifier (API ghi/xoá, argv list, cp/rsync đè, tar/unzip). QA `cls_new.py`: bắt `shutil.rmtree`, `fs.rmSync`, `File.delete`, `find -delete`, `git rm`. **Vẫn heuristic** (bỏ sót `truncate`/`: >`/`dd of=` đè file, `command rm`/`\rm`/`/bin/rm` khi đích là file run tạo). Lớp CƯỠNG CHẾ thật là sandbox OS (chặn mọi ghi HUB_HOME bất kể cách viết) — nên bất biến đứng vững dù recall < 100% |
| FR-SG-03 | Must | Một phần | = | Bước 2 LLM phụ vẫn chưa làm (`uncertain`→giữ, có chủ ý). Lỗ phân tích như SG-01 |
| FR-SG-04 | Must | Đạt | ↑ | H-01: RPC `approval.*` đòi token người + người gọi không phải hậu duệ run (pid/session/env do kernel báo). QA (`test_sec_auth`, daemon+socket thật): `python -m agent_hub.cli approve`, `xargs agent-hub`, client socket kể cả khi đọc trộm token, double-fork mồ côi, setsid — ĐỀU bị từ chối; người vẫn duyệt. Lỗ tự duyệt gốc đã bịt |
| FR-SG-05 | Must | Đạt | ↑ | H-02 (classifier literal/symlink) + sandbox denyWrite `policy/` + lớp 3 `guard/integrity.py` (khôi phục ≤ tick 2 s). QA `cls.py`: Write/Bash/python vào `policy/`,`golden/`,`hub.db` → destructive |
| FR-SG-07 | Must | Một phần | ✎ | SEC thêm CI (`.github/workflows/test.yml`, macOS+Ubuntu) ✓ (v1 "không có CI"). Recall vẫn heuristic như SG-01 |
| các mã khác | | | = | = v1 (SG-02,06,08,09) |

### 1.11 NFR

| Mã | Ưu tiên | v2 | So với v1 | Bằng chứng |
|---|---|---|---|---|
| NFR-10 | NFR | Đạt | ↑ | H-12 (che theo giá trị, e2e grep toàn HUB_HOME) |
| NFR-12 | NFR | Đạt | ↑ | H-01 (fail-closed + không tự duyệt). Caveat V2-N1 (admin methods khác, §4) |
| NFR-15 | NFR | Đạt | ↑ | H-18 `check_extra_args` chặn mọi biến thể bypass + cờ ghi đè settings; sandbox `allowUnsandboxedCommands=false`. `test_sec_sandbox` |
| NFR-16 | NFR | Một phần | = | Prompt injection — hardening không chạm |
| NFR-17 | NFR | Đạt | ↑ | H-19: `reserved_budget` trừ run `queued|running`. `test_h19_parallel_runs_cannot_exceed_daily_budget` |
| NFR-21 | NFR | Đạt | ↑ | H-20: `run_logger`/`RunContextFilter` gắn `run_id/agent_id` |
| NFR-22 | NFR | Đạt | ↑ | H-20 + LRN: judge & curator qua `ClaudeCliRuntime.oneshot`. `test_judge_goes_through_runtime_oneshot`, `test_h20_oneshot_via_runtime` |
| NFR-23 | NFR | Đạt | ↑ | H-20: `docs/schema/event.v1.json` + `tests/test_event_schema.py` (envelope + fixture hợp lệ) |
| NFR-26 | NFR | Đạt | ↑ | CLI: màn `?` liệt kê mọi phím. `test_low_tui_help_screen_lists_all_keys` |
| NFR-28 | NFR | Đạt | ↑ (thiếu→đạt) | H-09: migration v12, `recovery.purge_retention` (events/transcript/stdout/checkpoint/trash 30 ngày), trigger giữ append-only. `test_h09_*` |
| NFR-29 | NFR | Đạt | ↑ | H-02 (mọi đường ghi policy) + sandbox + integrity. `test_sec_classifier`, `test_sec_integrity` |
| NFR-05, 07, 18, 19, 31 | NFR | Một phần | = | Không thuộc phạm vi hardening. NFR-19 overhead học vẫn 68,6% > 15% (M5 DoD chưa đạt) |
| các mã khác | | | = | = v1 (01,02,03,04,06,08,09,11,13,14,20,24,25,27,30,32 Đạt) |

### 1.12 DoD mốc

| Mốc | v2 | So với v1 |
|---|---|---|
| M1 "phá huỷ bị giữ … duyệt 1 lần" | **Đạt** | ↑ — lỗ tự duyệt + ghi `policy/` đã bịt (H-01/H-02/H-05). Caveat V2-N1 |
| M1 "agent không ghi `policy/`" | Đạt | ↑ (3 lớp) |
| M1 "agent dùng MCP, tool ghi có audit" | Đạt | ↑ (H-13 sinh allow/deny) |
| M4 "correction tăng → tự rollback" | Đạt | ↑ (H-04: không ghi đè người) |
| M5 "overhead học ≤ 15%" | **Chưa đạt** | = (68,6%) |
| các mốc khác | | = v1 |

---

## 2. Tổng hợp

### 2.1 Tổng thể

| Nhóm | Tổng | Đạt | Một phần | Thiếu | Lệch có chủ ý |
|---|---|---|---|---|---|
| FR | 105 | 94 (90%) | 7 | 0 | 4 |
| NFR | 32 | 26 (81%) | 6 | 0 | 0 |
| **Cộng** | **137** | **120 (88%)** | **13 (9%)** | **0** | **4** |

Tính cả "Lệch có chủ ý": 124/137 = **91%**. (v1: 81/137 = 59%; +lệch 63%.)

### 2.2 Theo ưu tiên (FR)

| Ưu tiên | Tổng | Đạt | Một phần | Lệch | Tỉ lệ đạt (+lệch) |
|---|---|---|---|---|---|
| Must | 83 | **75** | 5 | 3 | **90% (94%)** |
| Should | 17 | 16 | 1 | 0 | 94% |
| Could | 5 | 3 | 1 | 1 | 60% (80%) |

Must một phần còn lại: FR-AG-03 (V2-N1), FR-TK-04 (assert yếu), FR-SG-01, FR-SG-03, FR-SG-07 (recall heuristic — bất biến vẫn đứng nhờ sandbox). Must lệch: FR-TK-06, FR-RT-06, FR-MC-01.

### 2.3 So với v1 (Must)

v1: Must đạt 50/83 = 60% (65% +lệch). v2: **75/83 = 90% (94% +lệch)**. +25 mã Must lên Đạt.

---

## 3. Bất biến an toàn §4 — kiểm lại từng cái (QA tự chạy lại script v1 + lách mới)

| # | Bất biến | v1 | v2 | Kết luận QA (độc lập) |
|---|---|---|---|---|
| 1 | Thao tác phá huỷ luôn cần người | **VI PHẠM** | **Đạt (còn 1 lỗ Cao V2-N1 + rủi ro tồn dư)** | Lỗ gốc (RPC không xác thực người gọi) đã bịt: token người (0600 cạnh socket) + nhận diện hậu duệ run qua pid/session/env (kernel báo) + sandbox chặn đọc token & kết nối socket. QA chạy lại 3 cách v1 + double-fork/setsid/đọc trộm token → ĐỀU từ chối; người vẫn duyệt. **NHƯNG** xem V2-N1 (agent.revert/archive/trash.* chưa nằm trong `HUMAN_ONLY`) và V2-N2 (lập lịch hoãn) |
| 2 | Hook fail-closed | Đạt | Đạt | = v1, nay thêm sandbox + hook quan sát fail-open riêng (không đụng lớp fail-closed) |
| 3 | Agent không ghi `policy/`, `golden/` | **VI PHẠM** | **Đạt** | 3 lớp độc lập: classifier (literal/symlink/interpreter — QA `cls.py`), sandbox denyWrite, integrity khôi phục ≤ 2 s (`guard/integrity.py`, migration v11). Không còn phụ thuộc cách viết lệnh |
| 4 | Tự rollback không chạm thay đổi của người | **VI PHẠM** | **Đạt** | H-04: hàm áp dụng duy nhất `Ledger._rollback_one` chỉ khôi phục khoá của entry + chặn khi actor ngoài `{learning,review,agent,curator}` hoặc đích bị sửa ngoài ledger. QA `repro_cfg.py`/`repro_cfg2.py`: `model=haiku` của người được GIỮ (v1: bị ghi đè về `sonnet`). Canary cũng không rollback `actor=user` |
| 5 | Secret không lộ argv/log/DB/OTel | Một phần | **Đạt** | H-12: che theo giá trị dùng chung; QA `secret_e2e.py` (grep toàn HUB_HOME = 0; argv sạch; mcp.json xoá sau run; dọn runtime lúc start), `otel_leak.py` (0), `probe_leak.py` (0) |
| 6 | HTTP MCP chỉ localhost | Đạt | Đạt | = v1 |

### 3.1 Lỗ hổng MỚI phát hiện ở vòng 2 (không nằm trong dev-notes)

| # | Mức | Lỗ hổng | Bằng chứng QA | Đề xuất |
|---|---|---|---|---|
| **V2-N1** | **Cao** | **Lệch phối hợp CLI↔SEC**: luồng CLI thêm `agent.archive`, `agent.revert`, `trash.restore`, `trash.purge` vào `MUTATING` và dev-notes CLI ĐÃ yêu cầu SEC đưa chúng vào token điều khiển — nhưng `auth.HUMAN_ONLY` KHÔNG có chúng. Hệ quả: một tiến trình cục bộ (kể cả agent trong run) gọi RPC `agent.revert` **không cần token** → khôi phục agent về revision cũ (QA tái hiện: `allow_bypass` bật lại = nâng quyền/tamper). `trash.purge` gọi `agent.purge` (human-only) qua lời gọi NỘI BỘ → **bỏ qua** cổng token → xoá vĩnh viễn agent. `agent.archive` = DoS. Khi sandbox TẮT (Linux thiếu bubblewrap, hoặc `guard.sandbox.enabled=false`) agent gọi thẳng `agent-hub agent revert …` (classifier chỉ `write/ALLOW`) | Daemon thật (QA, không token, tiến trình con): `agent.revert{id,to:2}` → `result` ok, `allow_bypass=True`. `agent.revert`/`agent.archive`/`trash.*` ∈ MUTATING \ HUMAN_ONLY (8 method admin lọt; phần còn lại như `run.start`,`task.create` là bề mặt hợp lệ trong run) | Thêm `agent.archive, agent.revert, trash.restore, trash.purge` vào `auth.HUMAN_ONLY`; classifier `forbidden` cho `agent-hub agent revert|archive`, `agent-hub trash restore|purge`. Thêm ca test như `test_sec_auth` |
| **V2-N2** | Trung bình | **Mạo danh người HOÃN**: classifier `ALLOW` cho `crontab -`, `at`, và ghi `~/.config/git/config` (hooksPath/alias → RCE khi người chạy git), `~/Library/LaunchAgents/*.plist`. Các tiến trình này chạy NGOÀI run/sandbox như người → đọc được token người → tự duyệt về sau. (`~/.zshrc` đã bị bắt `protected_path`; `launchctl submit` bị sandbox chặn theo spike SEC) | QA `cls_new.py`: `crontab -l|…|crontab -`, `at …`, Write `~/.config/git/config`, `~/Library/LaunchAgents/evil.plist` → ALLOW | Bổ sung `protected_path` cho `~/.config/git/`, `~/Library/LaunchAgents/`, `~/Library/LaunchDaemons/`, `~/.gitconfig` (đã có); classifier HOLD cho `crontab`/`at`/`launchctl load`. Ưu tiên sau V2-N1. **Đã sửa (2026-10-30)**: đề xuất gốc có từ hardening SEC; bổ sung bịt ca lách (lệnh lập lịch trong mã python/node, `systemd-run`, `loginctl enable-linger`, `systemctl` mọi phạm vi, `defaults`/`plutil` ghi LaunchAgents + LoginHook, `osascript` login item, PowerShell gọi động + Startup + khoá Run viết `/`, `cmd /c` với `%APPDATA%`, Startup/`~/.local/share/systemd`/`/etc/cron.*`… vào `protected_path`) — `docs/dev-notes/guard-deferred-exec.md`, `tests/test_sec_deferred_exec.py` |
| V2-N3 | Thấp/Info | Classifier bỏ sót `socat UNIX-CONNECT:…hub.so''ck` (nối chuỗi) và một số `rm` có tiền tố (`\rm`,`command rm`,`/bin/rm`,`env rm`) khi đích là file run tạo. Chỉ còn sandbox làm lớp chặn | QA `cls_new.py` | Chấp nhận được nếu sandbox luôn bật; nếu không, mở rộng chuẩn hoá lệnh |

---

## 4. Lệch BRD tổng hợp — danh sách thay đổi cụ thể cần áp vào BRD (cho coordinator)

Gộp từ v1 §5 (các mục "Có — cập nhật BRD") + `hardening-CLI.md` mục "Lệch BRD" + lệch mới do hardening tạo ra. Định dạng: **mã FR** — câu/ý CŨ → câu/ý MỚI.

### 4.1 Do hardening quyết định (bắt buộc cập nhật)

1. **FR-LN-16 / FR-LN-06 / FR-LN-17 / §4 bất biến 4** — CŨ: "rollout canary áp cho mọi thay đổi tầng team/global (kể cả của người), hồi quy → rollback". → MỚI: "Mọi cơ chế tự động (canary, eval gate, regression monitor) **KHÔNG BAO GIỜ** rollback entry do người tạo (`actor=user`). Thay đổi tầng team/global/USER của người áp dụng NGAY, không eval/ghim trước, chỉ theo dõi + cảnh báo (digest + Slack)."
2. **FR-RT-10 / §8 data model** — CŨ: "charter team nằm trong tin nhắn giao việc". → MỚI: "charter team là khối (3) trong SYSTEM PROMPT (giữa SOUL và memory), ổn định theo (agent, team); chỉ run có `team_id`; `--system-prompt-snapshot=on` mặc định."
3. **NFR-15 / FR-RT-09** — CŨ: "mặc định `acceptEdits` trên repo". → MỚI: "mô hình 'ghi chạy ngay, có audit' (`default_permission_mode=None`); thêm LỚP sandbox OS của Claude Code (`guard.sandbox.*`, mặc định bật, `failIfUnavailable=true`): chặn ghi HUB_HOME (trừ workspace), đọc token/secret, kết nối Unix socket hub — không phụ thuộc cách viết lệnh."
4. **FR-SG-05 / NFR-29** — bổ sung: "lớp 3 kiểm toàn vẹn `policy/` + nhánh an toàn `config.yaml` + `hub.db` (`guard.integrity.*`), khôi phục ghi ngoài RPC ≤ tick; người sửa tay qua `guard integrity accept`."
5. **FR-SG-04 / NFR-12 / §4 bất biến 1** — bổ sung yêu cầu: "mọi method RPC nhạy cảm đòi **token người** (file 0600 cạnh socket) VÀ người gọi không phải hậu duệ tiến trình run (pid/session/env do kernel báo)." (Và sửa V2-N1: liệt kê đủ method admin.)

### 4.2 Đổi tên / cú pháp lệnh (FR-UI-08 và liên quan) — từ hardening-CLI.md

6. **FR-UI-08** — CŨ `run list|show|logs <run>` → MỚI: giữ cả `run list|show|logs|stop|follow` (alias) VÀ nhóm gốc `runs …`; thêm `run follow`.
7. **FR-UI-08** — CŨ: `agent archive` và `agent rm` là hai lệnh → MỚI: cùng một thao tác (`archive` = `rm` = xoá mềm + chuyển home vào trash; khôi phục `restore`).
8. **FR-AG-01** — bổ sung: id agent KHÔNG được trùng từ khoá CLI (`RESERVED_IDS`: list, show, logs, stop, follow, continue, resume, revert, diff, steer, status, all, help, detach, wait, takeover, attach).
9. **FR-UI-08** — `learn pin|unpin` là alias của `knowledge pin|unpin <agent> <skill|memory|config|memory_entry> <ref>`.
10. **FR-UI-08** — `learn enable|disable [agent]` là alias của `learn policy set [agents.<id>.]enabled true|false`.
11. **FR-UI-08** — `eval golden add|list|rm`, `eval results` là alias; nhóm chính là `golden add|from-run|list|show|rm|restore|edit|candidates` và `eval list|show`.
12. **FR-UI-08** — `approve`/`requests`: `approvals` (danh sách) + `approve`/`deny`; `requests` = `approvals`, `approve --deny` = `deny`.
13. **FR-UI-08** — `feedback <run> --good|--wrong [--note]` → dạng chính positional `feedback <run> good|wrong|partial` (alias cờ).
14. **FR-TK-14 / FR-UI-08** — `run … --continue <run>` giữ; dạng chính `run continue <run|session> "<tin>"` (+ `run resume`).
15. **FR-UI-08** — `trash list|restore|purge` gồm agent/team/MCP; `purge` chỉ agent.
16. **FR-SG-02 / FR-UI-08** — `guard policy show|edit` → `guard policy` (xem) + `guard protect add|list|rm`; sửa catalog = sửa file `policy/` bằng tay (hoặc qua integrity accept).
17. **FR-UI-08** — bổ sung vào danh mục lệnh: `stop <run|agent>`, `hook <Event>`, `daemon logs [-n][--run][--agent]`, và `top, attach, steer, takeover, template, report, cost, digest, regression, curation, telemetry, secret, audit, notify, config get|set`.
18. **FR-RT-17** — CŨ "hook gọi `agent-hub hook <event>`" → MỚI: settings gọi `python -I -m agent_hub.observe_hook <event>` (nhanh, không phụ thuộc PATH); `agent-hub hook` là cùng thân cho người/script.
19. **FR-RT-15** — bổ sung: ma trận tương thích theo CỜ (`runtime.claude.flag_matrix`); phiên bản lạ chỉ cảnh báo vàng, không kiểm cờ.
20. **FR-MS-02** — `hub_cancel(run_id|task_id)` (task_id dừng cả run của task con); `hub_memory_search` hiện là tìm từ khoá tất định trên file + `runs` (mục tiêu FTS5/transcript đầy đủ).

### 4.3 Từ v1 §5 vẫn còn hiệu lực (chưa áp)

21. **FR-MS-02** — tên tool M1-A: `hub_run_status`, `hub_open_requests`, `hub_deny` tách, `hub_run(prompt)`, `hub_events(wait_s)`; tầng học `hub_learn_*`/`hub_knowledge_*`.
22. **FR-MC-01** — CŨ `trusted|full` → MỚI `trusted|untrusted` (`full` = alias); DB là nguồn sự thật, yaml bản sao; thêm `tools.require_approval`.
23. **FR-OB-08** — nới "before/after đầy đủ" → "params đã che + tóm tắt".
24. **FR-MC-05** — ghi secret env-fallback mặc định bật + yêu cầu che theo giá trị (đã làm ở H-12).
25. **FR-SG-03** — hạ bước 2 (LLM phụ) thành Should/mốc sau; ghi "uncertain → giữ chờ người" là hành vi chuẩn.
26. **FR-TK-05** — danh mục outcome: `crashed` vs `failed`, spawn lỗi = `failed`.
27. **FR-OB-01** — ring buffer gom theo agent (BRD ghi theo run) — ghi lệch.
28. **NFR-19** — chốt định nghĩa overhead = học/làm-việc (không phải học/tổng); đồng thời ghi rõ M5 CHƯA đạt chỉ tiêu (68,6%).
29. **§8** — golden ở DB + bản sao YAML `policy/golden/`; `protected_resources` ở `policy/protected.yaml`.
30. **FR-UI-10** — takeover không qua guard (có chủ ý).

---

## 5. Phụ lục bằng chứng (ngoài repo)

Script QA vòng 2 ở `/private/tmp/claude-501/-Users-phuonglq-Workspace-agent-hub/e77b5236-4485-4813-8b88-ed8c360a2291/scratchpad/audit/`:
- v1 (chạy lại, đều phản ánh ĐÃ SỬA): `repro_cfg.py`, `repro_cfg2.py` (H-04), `repro_l1.py` (H-17), `otel_leak.py` (H-12), `scripts/probe_leak.py`, `scripts/secret_e2e.py` (H-12), `scripts/lock_race.py` (H-21), `scripts/verify_cls.py`, `cls.py`, `exp_misc.py` (H-10).
- v2 mới (`audit/v2/`): `cls_new.py`, `cls_new2.py` (lách classifier mới), `sandbox_argv.py` (sandbox trong settings MỌI run + argv), probe daemon xác nhận V2-N1 (agent.revert không token).
- `pytest_v2.txt`: 416 passed.
