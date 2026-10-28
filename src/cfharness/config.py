"""Cấu hình cấp hub: `HUB_HOME/config.yaml`.

Mọi giới hạn là SETTING (Q-02), không trần cứng trong code. Daemon đọc lại cấu hình
mỗi lần cần (dispatcher, spawn) nên `config set` có hiệu lực ngay, không cần restart.

Các cờ cô lập/permission của `claude` nằm dưới `runtime.claude.*` — CẤU HÌNH ĐƯỢC vì
tổ hợp cờ đúng phụ thuộc kết quả spike A-01..A-05 (docs/spikes/M0-spike-report.md).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

# Cờ `claude` được phép (runtime.claude.flag_matrix) — lấy từ `claude --help` 2.1.302, kiểm lại trên 2.1.303 và 2.1.310.
_FLAGS_2_1_302: list[str] = [
    "-p", "--print", "--add-dir", "--agent", "--agents", "--allowed-tools", "--allowedTools", "--append-system-prompt",
    "--autocompact", "--bare", "--betas", "--brief", "--continue", "--debug", "--debug-file", "--disable-slash-commands",
    "--disallowed-tools", "--disallowedTools", "--effort", "--exclude-dynamic-system-prompt-sections", "--fallback-model",
    "--file", "--fork-session", "--forward-subagent-text", "--include-hook-events", "--include-partial-messages",
    "--input-format", "--json-schema", "--max-budget-usd", "--mcp-config", "--model", "--name", "--no-session-persistence",
    "--output-format", "--permission-mode", "--permission-prompt-tool", "--permission-prompts", "--plugin-dir",
    "--replay-user-messages", "--restricted", "--resume", "--safe-mode", "--session-id", "--setting-sources", "--settings",
    "--strict-mcp-config", "--system-prompt", "--system-prompt-snapshot", "--tools", "--verbose", "--worktree",
]

DEFAULTS: dict[str, Any] = {
    "hub": {
        # FR-TK-07: giới hạn số run đồng thời toàn hub (giá trị khởi tạo 4)
        "max_concurrent_runs": 4,
    },
    "runs": {
        # ngân sách mặc định cho một run khi agent không khai báo (null = không truyền --max-budget-usd)
        "default_max_budget_usd": None,
        # grace (giây) khi dừng: SIGINT → chờ → SIGTERM → SIGKILL (FR-RT-07)
        "stop_grace_s": 10,
    },
    "retention": {
        "soft_delete_days": 30,  # Q-17
        # M1-B: purge tự động agent đã xoá mềm quá hạn (chính sách do người đặt; có audit, FR-SG-06)
        "auto_purge": True,
        # hardening H-09 (NFR-28, Q-10): event/transcript và stdout thô giữ 30 ngày (0/null = vĩnh viễn); cost tổng hợp,
        # ledger, snapshot, audit, guard_decisions giữ vĩnh viễn. Job retention chạy tối đa 1 lần / interval_s.
        "events_days": 30,
        "raw_days": 30,
        "checkpoints_days": 30,  # checkpoints/<run>/ — cửa sổ `run revert` (giữ run chưa kết thúc hẳn)
        "interval_s": 3600,
    },
    # M1 luồng B — bảo vệ thao tác phá huỷ (FR-SG-*, FR-RT-09)
    "guard": {
        "enabled": True,
        # hook PreToolUse: timeout CLI (giây) — phải > approval.timeout_s; CLI hết giờ hook = fail-OPEN
        "hook_timeout_s": 900,
        # kênh thứ hai: --permission-prompt-tool mcp__hub-guard__approve
        "permission_prompt_tool": True,
        # `uncertain` (lệnh không phân tích chắc được: script động, python -c ghi file biến…) có giữ chờ người duyệt không.
        # false = chỉ `destructive` mới hỏi người (uncertain → cho chạy + audit); vẫn giữ uncertain vùng hub/policy và tool MCP
        # người đặt `require_approval`. Sandbox OS + permissions.deny vẫn áp. Người ít phải chạm nhất: false.
        # null = theo `approval.profile` (strict → true; balanced/autonomous → false). Giá trị người đặt rõ trong config.yaml luôn thắng.
        "hold_uncertain": None,
        # `git reset --hard <ref>` trong worktree/scratch: hạ về `write` khi probe git chứng minh không mất gì (cây sạch, HEAD còn trên
        # nhánh remote). null = theo profile (strict → tắt).
        "safe_reset_probe": None,
        # Edit/Write vào git worktree (cùng repo với cwd của run) do CHÍNH chuỗi run tạo, nằm trong workspace.allowed_roots → `write`.
        # null = theo profile (strict → tắt; vẫn hỏi người như cũ).
        "own_worktree_writes": None,
        # `node|python <script>` mà script được git track, chưa sửa so với HEAD và commit TRƯỚC khi task bắt đầu (tooling của repo,
        # không phải mã agent vừa viết) → API xoá trong mã không làm lệnh thành `destructive`. null = theo profile (strict → tắt).
        "trusted_repo_scripts": None,
        # Agent MỚI tạo (người, điều phối viên, clone, import) tự nhận trust như `cfharness guard trust <agent> <mode>`: "full" = không rào
        # chắn (xem docs/dev-notes/trust-and-chat-settings.md), "off" = như cũ. Chỉ áp lúc tạo; agent có sẵn giữ nguyên. Chỉ người đặt.
        "trust_new_agents": "off",
        # Xoá file/thư mục do CHUỖI RUN CỦA TASK tạo (không chỉ session hiện tại) → `write`; thư mục trong thư mục tạm của hệ thống
        # (%TEMP%, $TMPDIR) mà chính nó được tạo sau khi task bắt đầu → `write` không cần duyệt từng file con. null = theo profile.
        "task_created_deletes": None,
        # WEB-1: WebFetch/curl — SSRF (loopback/RFC1918/link-local/CGNAT/metadata/IPv6, scheme, user:pass@) luôn chặn; DNS phân giải
        # trong daemon, mọi IP phải công khai (không phân giải được → giữ chờ người). Glob domain: deny → chặn thẳng;
        # allow (rỗng = mọi domain công khai) → ngoài danh sách giữ chờ người (WebFetch; Bash đã có sandbox.allowed_domains).
        "web": {"allow_domains": [], "deny_domains": [], "resolve_dns": True, "dns_timeout_s": 3},
        # LLM thứ hai cho `uncertain` (guard/second_opinion.py): CHỈ người bật; chỉ hạ uncertain → safe/write, mọi lỗi/hết giờ/hết ngân
        # sách/trả lời khác → giữ chờ người. Không bao giờ áp cho destructive/forbidden/tài nguyên bảo vệ/vùng hub.
        "second_opinion": {"enabled": False, "model": "haiku", "binary": None, "timeout_s": 45, "call_budget_usd": 0.02,
                           "daily_budget_usd": 0.5, "max_calls_per_day": 200, "cache_ttl_s": 3600},
        # python chạy hook/hub-guard (null = python của daemon)
        "python": None,
        # gọi `git` để biết thay đổi chưa commit / nhánh đã push (FR-SG-01 nhóm Git)
        "git_probe": True,
        # khoá ghi theo thư mục dir: (bản tối thiểu M1; FR-RT-12 đầy đủ ở M3)
        "repo_lock": True,
        # lớp tĩnh --disallowedTools cho mẫu luôn-phá-huỷ (CLI tự chặn nếu hook không chạy)
        "static_disallowed_tools": [
            "Bash(git push --force:*)",
            "Bash(git push -f:*)",
            "Bash(git push --force-with-lease:*)",
            "Bash(git filter-branch:*)",
            "Bash(mkfs:*)",
        ],
        # hardening SEC: sandbox OS của Claude Code cho Bash (đã kiểm chứng 2.1.302) — chặn ghi HUB_HOME/đọc token+secret/Unix socket hub
        "sandbox": {
            "enabled": True,
            "fail_if_unavailable": True,  # không khởi động được sandbox → run lỗi (fail-closed), KHÔNG chạy không sandbox
            # R3-1: mạng của Bash mặc định HẸP (sandbox không chặn được keychain qua Mach IPC → chặn ĐƯỜNG RA của token):
            # registry gói + Anthropic; KHÔNG gồm github/gitlab/bitbucket. Host của MCP server gán cho agent tự thêm. Người mở thêm theo agent:
            # `cfharness guard sandbox allow-domain <agent> <domain>`; đặt ["*"] để quay về như cũ.
            "allowed_domains": ["registry.npmjs.org", "*.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org",
                                "*.pythonhosted.org", "api.anthropic.com", "*.anthropic.com"],
            "allow_unix_sockets": [],  # macOS: socket được phép (KHÔNG bao giờ gồm hub.sock)
            # R2-2: mặc định TẮT — agent không có thông tin xác thực git (ssh-agent, ~/.ssh, gh token, keychain helper) nên không
            # `git push` được bất kể viết lệnh kiểu gì; người push. Bật lại (chỉ người) nếu agent cần push repo riêng.
            "allow_ssh_agent": False,
            "deny_git_credentials": True,
            "protect_hub_code": True,  # chặn ghi mã nguồn gói cfharness (hook/guard chạy từ đó)
            "extra_deny_write": [],
            "extra_deny_read": [],
            "extra_allow_write": [],
        },
        # hardening SEC: toàn vẹn policy/ + config.yaml (nhánh an toàn) + hub.db — phát hiện ghi ngoài RPC
        "integrity": {"enabled": True, "tick_s": 2.0, "restore": True},
    },
    "approval": {
        "timeout_s": 300,  # Q-04: giữ chờ trong hook; quá → waiting_human (KHÔNG tự từ chối)
        "reminder_check_s": 30,  # chu kỳ kiểm nhắc lại
        "pause_delay_s": 0.5,  # chờ hook trả deny tới CLI rồi mới dừng tiến trình
        # Hồ sơ phê duyệt (docs/dev-notes/approval-profiles.md) — CHỈ người đặt (config.set ∈ HUMAN_ONLY, nhánh `approval` kiểm toàn vẹn).
        # strict = như trước; balanced = uncertain cho chạy + audit, thao tác phá huỷ trong workspace có checkpoint revert được tự cho
        # phép, giao điều phối viên duyệt một số lớp; autonomous = balanced + tự cho phép phá huỷ trong scratch/worktree của agent có
        # tool_policy.allow_bypass (người đặt). Khoá đặt rõ trong config.yaml thắng profile.
        "profile": "balanced",
        # null = theo profile. true: thao tác phá huỷ mà MỌI đích nằm trong repo có checkpoint của run và revert được → cho phép + audit
        "checkpoint_auto_allow": None,
        # null = theo profile (chỉ autonomous). Run scratch/worktree của agent có allow_bypass: phá huỷ trong cwd của run → cho phép
        "scratch_auto_allow": None,
        # Giao điều phối viên duyệt approval của agent KHÁC thuộc các lớp này (không bao giờ: danh sách "không bao giờ tự động").
        # enabled null = theo profile (balanced/autonomous bật). Quá timeout_s chưa quyết → chuyển người (approval.escalated).
        "delegate_to_manager": {"enabled": None, "classes": ["file", "git", "mcp"], "timeout_s": 600},
        # Hết `timeout_s` giữ chờ: wait (Q-04: run tạm dừng chờ người) | deny_and_continue (agent nhận từ chối kèm hướng dẫn, run chạy
        # tiếp, approval VẪN mở; người duyệt sau → resume như luồng waiting_human). Theo agent: on_timeout_by_agent {agent_id: …}.
        "on_timeout": "wait",
        "on_timeout_by_agent": {},
    },
    # UX — câu hỏi có lựa chọn của agent (questions/). Nhắc lại dùng notify.reminder_interval_s (giãn dần), KHÔNG tự trả lời.
    "questions": {
        "via_manager": True,  # agent trong task hỏi điều phối viên trước; không chốt được → người
        "manager_timeout_s": 900,  # điều phối viên không xử lý trong N giây → tự chuyển lên người
        "max_open_per_run": 3,
        "check_s": 30,  # chu kỳ vòng nền (chuyển lên người khi quá hạn, nhắc lại)
    },
    "notify": {
        "reminder_interval_s": 900,  # FR-SG-09: nhắc lại mỗi 15 phút…
        "reminder_max_interval_s": 7200,  # …giãn dần tới 2 giờ
        "reminder_max_count": None,  # số lần nhắc tối đa cho mỗi yêu cầu duyệt (null = không giới hạn, 0 = không nhắc)
        "repeated_failure_threshold": 3,  # run lỗi liên tiếp → thông báo
        "slack": {
            "enabled": True,  # tắt êm nếu chưa có URL/token
            "webhook_secret": "SLACK_WEBHOOK_URL",  # tên secret chứa Incoming Webhook URL
            "webhook_url": None,  # chỉ để thử; nên dùng secret
            "bot_token_secret": "SLACK_BOT_TOKEN",
            "channel": None,
            "api_base": "https://slack.com/api",
            # Slack 2 chiều (docs/plan/slack-2way.md): Socket Mode trong daemon — duyệt bằng nút, lệnh, hội thoại. Bật bằng
            # `cfharness notify slack setup` (bot token + app token → secret store, ghép nối DM, allowlist người). Khi bot hoạt động,
            # thông báo đi DM có nút thay cho webhook/kênh ở trên (không gửi trùng).
            "interactive": False,
            "app_token_secret": "SLACK_APP_TOKEN",  # App-Level Token (xapp-, scope connections:write)
            "callback_key_secret": "SLACK_CALLBACK_KEY",  # khoá HMAC ký giá trị nút (tự sinh)
            "team_id": None,  # workspace ghim lúc setup — envelope từ workspace khác bị bỏ qua
            "allowed_chat_ids": [],  # DM (D…) được nhận thông báo + điều khiển hub
            "allowed_user_ids": [],  # người (U…) được phép — Slack xác thực qua kết nối Socket Mode
            "allow_channels": False,  # kênh chung phải bật rõ ràng; mặc định chỉ DM
            "thread_context": False,  # P8: tag bot giữa thread chưa có bot → đọc tin trước đó (conversations.replies) làm ngữ cảnh DỮ LIỆU
            "slash_command": "/cfh",  # lệnh đăng ký trong manifest app (mỗi người một app → nên đặt riêng, vd /cfh-ten)
            "confirm_classes": ["db", "git", "system", "hub", "policy", "mcp", "file"],
            "callback_ttl_s": 604800,
            "pair_ttl_s": 600,
            "idle_check_s": 15,
            "progress_after_s": 30,
            "progress_edit_s": 10,
            "result_max_chars": 1500,
            "deliver_files": True,
            "deliver_max_bytes": 20971520,
            "deliver_max_files": 5,
            "document_max_bytes": 1048576,  # tệp văn bản người gửi tối đa (trần cứng 10 MB ở manager.chat)
            "files_base": None,  # CHỈ cho test: gốc URL tải tệp thay https://files.slack.com/ (không bao giờ theo URL tuỳ ý)
            "modal_ttl_s": 3600,  # P2: hộp nhập (modal) còn hiệu lực — nonce phía hub `slack.modals`
            "events": ["approval.open", "approval.waiting_human", "approval.reminder", "approval.escalated", "guard.blocked", "run.repeated_failure", "learning.rolled_back",
                       "learning.digest", "golden.suggested", "regression.detected", "guard.tamper_detected", "auth.denied", "guard.env_tamper"],  # M4-B: digest + nhắc chọn golden task (Q-23)
            "max_attempts": 5,
            "backoff_base_s": 2,
            "timeout_s": 10,
        },
        # Telegram (docs/dev-notes/telegram.md): bot long polling trong daemon; tắt êm khi chưa có token.
        # `cfharness notify telegram setup` ghi token vào secret store + allowlist chat_id (chỉ người).
        "telegram": {
            "enabled": True,
            "bot_token_secret": "TELEGRAM_BOT_TOKEN",
            "callback_key_secret": "TELEGRAM_CALLBACK_KEY",  # khoá HMAC ký callback nút inline (tự sinh)
            "api_base": "https://api.telegram.org",
            "progress_after_s": 30,  # run do chat giao chạy quá N giây → một tin tiến độ (sửa tại chỗ)
            "progress_edit_s": 10,  # sửa tin tiến độ tối đa mỗi N giây (tránh 429)
            "allowed_chat_ids": [],  # CHỈ các chat này được nhận thông báo + điều khiển hub
            "allowed_user_ids": [],  # WT-4: from.id được phép (ghi lúc ghép nối); nhóm CHỈ nhận người trong danh sách này
            "allow_groups": False,  # nhóm phải bật rõ ràng; mặc định chỉ chat riêng
            "events": ["approval.open", "approval.waiting_human", "approval.reminder", "approval.escalated", "guard.blocked", "run.repeated_failure", "learning.rolled_back",
                       "learning.digest", "golden.suggested", "regression.detected", "guard.tamper_detected", "auth.denied", "guard.env_tamper"],
            "confirm_classes": ["db", "git", "system", "hub", "policy", "mcp", "file"],  # nhãn "THAO TÁC NẶNG" (mọi cho phép đều 2 bước — WT-5)
            "callback_ttl_s": 604800,  # nút duyệt còn hiệu lực 7 ngày (waiting_human có thể chờ lâu)
            "pair_ttl_s": 600,
            "poll_timeout_s": 25,
            "idle_check_s": 15,  # chưa cấu hình → kiểm lại sau mỗi chừng này giây
            "result_max_chars": 1500,
            # Tệp/ảnh với điều phối viên: ảnh (PNG/JPEG/GIF/WebP ≤ 5 MB, ≤ 4/tin — cùng giới hạn TUI) + tệp VĂN BẢN người gửi;
            # tệp điều phối viên đánh dấu `[[file: …]]` trong trả lời → gửi thành document (chỉ trong cwd hội thoại)
            "document_max_bytes": 1048576,  # tệp văn bản người gửi tối đa (trần cứng 10 MB ở manager.chat)
            "media_group_wait_s": 1.5,  # album (nhiều ảnh một lần) → gom thành MỘT tin cho điều phối viên
            "deliver_files": True,
            "deliver_max_bytes": 20971520,  # tệp gửi ra tối đa (Bot API cho tới 50 MB)
            "deliver_max_files": 5,
            "max_attempts": 5,
            "backoff_base_s": 2,
            "timeout_s": 15,
        },
    },
    "recovery": {
        "auto_resume": True,  # Q-09: daemon restart → dừng-và-resume tự động (trừ waiting_human)
        "pid_start_tolerance_s": 5,
    },
    # M1 luồng A — MCP client registry (FR-MC-*)
    "mcp": {
        # server MCP nội bộ của hub không ghi audit tool.write (tool điều khiển của guard)
        "audit_ignore_servers": ["hub-guard"],
        "probe_timeout_s": 30,
    },
    "secrets": {
        # `${VAR}` không có trong secret store thì lấy biến môi trường của daemon (tương thích ~/.claude.json)
        "env_fallback": True,
    },
    # M1 luồng A — MCP server của hub (FR-MS-*)
    "mcp_server": {
        "http_port": 8765,  # Streamable HTTP chỉ bind 127.0.0.1 (Q-11, NFR-14)
        "max_wait_s": 25,  # FR-MS-03: không giữ kết nối quá 25 s
        "max_events_per_call": 200,
    },
    # M7 MGR-CORE — điều phối viên (manager/): hội thoại liên tục + nhịp heartbeat
    "manager": {
        "heartbeat": True,  # tắt = chỉ chạy khi người nhắn / `manager tick`
        "tick_interval_s": 1800,  # tick định kỳ dự phòng (chỉ chạy khi có việc đáng xử lý)
        "check_s": 10,  # chu kỳ vòng nền kiểm sự kiện gom được
        "event_debounce_s": 45,  # gom sự kiện trong cửa sổ này rồi mới tick (một tick cho nhiều sự kiện)
        "approval_wait_min": 2,  # approval chờ người lâu hơn N phút → sự kiện cho tick (điều phối viên xin phép người bằng nút)
        "crash_repeat": 2,  # run crashed/failed lặp ≥ N lần/giờ cùng agent → sự kiện
        "max_pending_events": 40,  # trần sự kiện đưa vào một prompt tick (cũ hơn bị gộp đếm)
        "tick_budget_usd": 0.5,  # trần chi phí mỗi run tick
        "chat_budget_usd": None,  # trần mỗi run hội thoại (null = theo limits của agent)
        "rotate_after_turns": 80,  # sau N lượt cùng session → session mới (mang theo tóm tắt), ngoài autocompact của CLI
        # MGR-6: trần tần suất thao tác của manager (nhánh config được kiểm toàn vẹn; ngân sách/model/effort kẹp theo learn policy config_bounds)
        "max_agents_created_per_day": 20,
        "max_tasks_per_hour": 60,
        "max_runs_per_hour": 30,
        "max_system_ops_per_hour": 10,  # tạm dừng/bật lại/khởi động lại job launchd/Task Scheduler của hệ thống đã đăng ký
        # §9 M7-core.md: khung quyền của điều phối viên (CHỈ người: config.set ∈ HUMAN_ONLY, nhánh `manager` kiểm toàn vẹn).
        # profile strict = Read/Grep/Glob (như MGR-8); hermes = Bash/Read/Write/Edit/Grep/Glob/TodoWrite + Web + MCP trusted (chỉ đọc),
        # VẪN dưới hook guard + sandbox OS + approval. null = theo profile. `cfharness manager capabilities show|set`.
        # connectors: connector claude.ai cho phép (cần mcp=trusted + guard bật) — manager/connectors.py
        "capabilities": {"profile": "strict", "tools": None, "mcp": None, "web": None, "connectors": None},
        # WEB-1: WebFetch của điều phối viên — `any` = mọi domain công khai (vẫn chặn SSRF + guard.web.deny_domains);
        # `allowlist` = chỉ guard.web.allow_domains (+ host người đã "luôn cho phép"), ngoài danh sách → giữ chờ người duyệt
        "web_domains": "any",
        # Windows (không sandbox OS): khung rộng (hermes) bị từ chối/hạ về strict trừ khi NGƯỜI đặt true — manager/sandbox_gate.py
        "allow_hermes_without_sandbox": False,
    },
    # M3 — team & điều phối (FR-TK-*, FR-RT-12/16, FR-OB-03)
    "tasks": {
        "tick_s": 5,  # FR-TK-06: chu kỳ tick dispatcher (ngoài tick theo sự kiện)
        "lease_ttl_s": 120,  # không heartbeat quá TTL + tiến trình chết → crashed
        "heartbeat_write_s": 5,  # ghi heartbeat vào DB tối đa mỗi N giây
        "failure_limit": 2,  # task không thuộc team (team dùng policies.failure_limit)
        "require_completion_call": False,  # True: kết thúc mà không gọi task_complete/task_block → protocol_violation
        "hub_self_mcp": True,  # FR-MS-08: thêm MCP hub-self vào run của task
        "rate_limit_backoff_s": 30,  # FR-RT-16: 30 s nhân đôi…
        "rate_limit_backoff_max_s": 600,  # …tới 10 phút
        "max_subtasks": 20,
        "review_budget_usd": None,  # trần chi phí run review (null = như run thường)
    },
    "ratelimit": {
        "throttle_utilization": 0.9,  # FR-RT-16: ≥ 90% → giảm đồng thời
        "throttle_concurrency": 1,
    },
    "budget": {
        "hub_usd_per_day": None,  # FR-OB-03: ngân sách ngày toàn hub (null = không giới hạn)
        "warn_pct": 80,  # cảnh báo budget.warning
    },
    # M4-A — lõi học (FR-LN-*). Công tắc học, config_bounds, rate là CHÍNH SÁCH AN TOÀN → `policy/learning.yaml`
    # (cfharness learn policy …), KHÔNG ở đây.
    "learning": {
        "review_budget_usd": 0.20,  # FR-LN-03 --max-budget-usd của review fork (null = không trần riêng, như run thường)
        "review_model": None,  # null = cùng model với run gốc (giữ cache, A-12)
        "review_scope": "combined",  # combined | skill | memory (prompt Hermes tương ứng)
        "review_debounce_s": 10,  # gom tín hiệu: review chạy ≥ N giây sau Signal cuối (newest-wins)
        "review_max_per_agent_per_day": 10,
        "review_timeout_s": 600,
        "trigger_turns": 10,  # FR-LN-02: số turn người kể từ lần review gần nhất
        "trigger_tool_rounds": 10,  # FR-LN-02: số vòng tool
        "high_signals": ["correction", "reject", "edit_output"],
        "auto_apply_max_chars": 800,  # Q-03 ngưỡng đường nhanh L1
        "auto_apply_max_items": 1,
        "limits": {"memory_chars": 2200, "user_chars": 1375, "team_memory_chars": 2200, "skill_chars": 100_000, "skill_desc_chars": 60},
        "repeat_pattern_n": 3,  # FR-LN-01: n-gram tool + số run tối thiểu
        "repeat_pattern_window_runs": 20,
        "failure_min_runs": 2,
        "knowledge_tools": True,  # thêm hub-self (skill_view) vào run khi agent có skill (FR-RT-10)
        "tick_s": 15,  # vòng nền: review đến hạn, proposal hoãn đến hạn
        "snapshot_keep": 20,
        "cache_warn_hash_changes_per_day": 3,  # FR-OB-04
        # M5-A — tầng team/global/USER (FR-LN-11/14/16, Q-22)
        "canary_runs": 5,  # cửa sổ canary: N run làm việc của agent canary…
        "canary_hours": 24,  # …hoặc N giờ, cái nào tới trước (Q-22)
        "canary_regression": {"threshold_pct": 100, "min_events": 2, "cost_threshold_pct": 100, "min_runs_cost": 3, "baseline_days": 7},
        "tier_eval_sample": 3,  # eval trước áp dụng: golden task của mọi agent bị ảnh hưởng; nhiều hơn N → mẫu N agent (≥ 3, gồm agent khởi nguồn)
        "user_profile_mode": "shadow",  # USER dùng chung không canary theo agent được: eval (shadow) → áp dụng cho mọi agent + theo dõi
        "steer_correction_regex": r"(?i)(\bđừng\b|\bsai\s+rồi\b|\bkhông\s+phải\b|\bkhông\s+đúng\b|\bsửa\s+lại\b|\bdon'?t\b|\bdo\s+not\b|\bstop\s+doing\b|\bwrong\b|\bthat'?s\s+not\b|\bnot\s+what\s+i\b|\binstead\b)",
    },
    # M4-B — đánh giá & vận hành học (FR-LN-08/10/13/17)
    "eval": {
        "enabled": True,  # tắt → gate trả deferred_no_eval (L3 hoãn), post-apply gắn unevaluated
        "samples": 2,  # FR-LN-10: mỗi golden task chạy N lần, lấy trung bình
        "drop_threshold": 0.10,  # tụt > 10% điểm trung bình → eval_failed / rollback
        "budget_usd": 1.0,  # eval_budget_usd: trần chi phí MỘT lần gate (mọi run eval + judge); agent ghi đè bằng agents.learning.eval_budget_usd
        "run_budget_usd": 0.30,  # --max-budget-usd mỗi run eval
        "run_timeout_s": 600,
        "baseline_cache_h": 24,  # dùng lại điểm baseline cùng dấu vân tay cấu hình+tri thức trong N giờ
        "min_score_abs": 0.5,  # post-apply không dựng được mốc trước: đạt nếu điểm ≥ ngưỡng này
        "batch_enabled": True,  # L1: eval theo lô hằng ngày
        "batch_time": "03:00",  # giờ máy
        "batch_lookback_days": 2,
        "suggest_every_h": 24,  # gợi ý golden task cho agent chưa có (không lặp quá dày)
        "keep_tmp": False,  # giữ bản sao home tạm (gỡ lỗi)
        "ops_tick_s": 30,  # chu kỳ vòng nền M4-B (digest/curator/eval lô/hồi quy)
        "judge": {"model": "haiku", "budget_usd": 0.05, "timeout_s": 180, "binary": None},  # LLM judge (null binary = runtime.claude.binary)
        "regression": {  # FR-LN-17
            "watch_runs": 10,
            "baseline_days": 7,
            "threshold_pct": 50,  # +50% so với mốc …
            "min_events": 2,  # … và tối thiểu 2 sự kiện trong cửa sổ
            "cost_threshold_pct": 50,  # so trung vị chi phí/run (bỏ run huỷ)
            "min_runs_cost": 5,
            "unevaluated_factor": 0.5,  # entry không có golden task: ngưỡng correction/failure chặt hơn (không áp cho chi phí)
            "track_days": 14,
            "full_check_min": 30,
        },
    },
    "curator": {  # FR-LN-08 pha 1 (tất định)
        "enabled": True,
        "interval_days": 7,
        "min_idle_min": 30,
        "stale_after_days": 14,
        "archive_after_days": 30,
    },
    "digest": {  # FR-LN-13, Q-18
        "enabled": True,
        "schedule": "08:00",  # giờ máy
        "default_period_h": 24,
        "send_empty": False,  # kỳ không có gì → lưu nhưng không gửi Slack
    },
    # M5-B — curator liên agent (FR-LN-12) + curator pha 2 LLM (FR-LN-09); chạy nền khi hub rảnh, có ngân sách riêng
    "curation": {
        "enabled": True,  # vòng nền (pha liên agent tất định mặc định bật; pha 2 LLM bật riêng)
        "tick_s": 300,
        "min_idle_min": 30,  # chỉ chạy khi không run nào đang chạy ≥ N phút
        "daily_budget_usd": 0.5,  # trần chi phí LLM của curation mỗi ngày (giờ máy)
        "run_budget_usd": 0.3,  # --max-budget-usd mỗi run curator
        "model": "haiku",
        "timeout_s": 600,
        "binary": None,  # null = runtime.claude.binary (gộp LLM liên agent gọi `claude -p --json-schema`)
        "cross_agent": {
            "enabled": True,
            "interval_h": 24,
            "threshold": 0.5,  # điểm tương tự tối thiểu (0..1) giữa skill của 2 agent khác nhau
            "min_agents": 2,
            "llm_merge": False,  # True: gộp nội dung bằng LLM (tuỳ chọn) thay vì gộp tất định
            "max_promotions_per_run": 3,
        },
        "phase2": {
            "enabled": False,  # FR-LN-09: "bật bằng setting"
            "interval_days": 7,
            "min_skills": 2,
            "max_candidates": 80,
        },
    },
    # M5-B — FR-OB-07: OpenTelemetry (opt-in, tắt mặc định). Chỉ metadata; không input/output tool MCP, không secret.
    "telemetry": {
        "otel": {
            "enabled": False,
            "exporter": "otlp_http",  # otlp_http | file
            "endpoint": "http://127.0.0.1:4318",  # OTLP/HTTP JSON → <endpoint>/v1/traces
            "headers": {},  # giá trị có thể là ${SECRET_NAME}
            "file": None,  # exporter=file: đường dẫn JSONL (mặc định HUB_HOME/logs/otel.jsonl)
            "service_name": "cfharness",
            "include_payloads": False,  # True: kèm input đã che của tool BUILT-IN (không bao giờ tool mcp__*)
            "batch_size": 64,
            "flush_s": 2.0,
            "timeout_s": 5.0,
        },
    },
    # Lịch chạy tự động (schedules/, docs/dev-notes/schedules.md). Nhánh được kiểm toàn vẹn (trần của điều phối viên).
    "schedules": {
        "enabled": True,  # tắt = vòng nền không bắn lịch nào (lịch vẫn giữ)
        "tick_s": 15,  # chu kỳ vòng nền
        "catchup_grace_s": 3600,  # daemon tắt/máy ngủ: chạy bù MỘT lần nếu mốc gần nhất trễ ≤ N giây, còn lại bỏ (`skipped: missed`)
        "max_failures": 3,  # lỗi liên tiếp ≥ N → tự tạm dừng + báo người
        "default_budget_usd": 0.5,  # trần mỗi lượt khi lịch không đặt budget_usd
        "prompt_max_chars": 4000,
        # trần của ĐIỀU PHỐI VIÊN (chỉ người đổi): số lịch đang bật, khoảng tối thiểu giữa 2 lượt, chạy-ngay/giờ, $/ngày ước tính
        # (null = learn policy config_bounds.budget_usd_per_day)
        "manager_max_active": 10,
        "min_interval_s": 900,
        "manager_run_now_per_hour": 6,
        "manager_daily_budget_usd": None,
    },
    # Hệ thống ngoài hub (systems/): app trong thư mục gốc + job launchd (macOS) / task Task Scheduler (Windows) của chúng — người đăng ký, điều phối viên tạm dừng/bật lại/khởi động lại
    "systems": {
        "roots": None,  # null = theo workspace.allowed_roots
        "launch_agents_dir": "~/Library/LaunchAgents",
        "launchctl": "/bin/launchctl",  # macOS
        "schtasks": "schtasks",  # Windows: ghi (/End, /Change /DISABLE|/ENABLE, /Run)
        "powershell": "powershell",  # Windows: đọc task (Get-ScheduledTask → JSON; mất gấp đôi timeout_s)
        "timeout_s": 15,  # mỗi lệnh launchctl / schtasks
        "log_tail_bytes": 6000,  # system_get: đuôi log stdout/stderr mỗi job (đã che secret)
    },
    "workspace": {
        # MGR-1: gốc người cho phép đặt workspace `dir`. Ngoài ra luôn cho vùng scratch/worktree/eval của hub. Người thêm gốc tại đây (chỉ người).
        "allowed_roots": ["~/Workspace"],
        "checkpoint": True,  # FR-RT-12: checkpoint + diff cho run ghi vào workspace dir
        "max_file_bytes": 5_000_000,  # file lớn hơn: chỉ lưu hash (không revert được)
        "max_files": 20_000,
    },
    "bus": {
        "ring_events": 2000,  # FR-OB-01 [ĐỀ XUẤT]
        "ring_bytes": 4_000_000,
        "subscriber_queue": 10_000,
    },
    "runtime": {
        # "claude" (thật) hoặc "fake" (phát lại fixture JSONL, cho test/demo không tốn tiền)
        "kind": "claude",
        "fake": {
            "fixture": None,  # null = fixture mẫu đóng gói kèm package
            "line_delay_s": 0.0,
        },
        "claude": {
            "binary": "claude",
            # Phiên bản đã kiểm (FR-RT-15). Phiên bản lạ → cảnh báo, vẫn chạy.
            # 2.1.302: fixture + contract test M0. 2.1.303: CHỈ `claude --help` + spike sandbox Windows (không fixture riêng).
            # 2.1.310: `claude --help` + fixture stream-json thật tests/fixtures/claude-2.1.310/ + tests/test_claude_contract_2_1_310.py.
            "tested_versions": ["2.1.302", "2.1.303", "2.1.310"],
            "include_partial_messages": True,
            # Spike A-04: bật để kiểm hậu nghiệm không có hook lạ (hook Serena…) trong stream
            "include_hook_events": True,
            # "stream-json": prompt gửi qua stdin dạng dòng user message (+ --replay-user-messages),
            #   mở đường cho steer/interrupt mềm (spike A-02/A-03). "text": prompt qua argv `-p "<prompt>"`.
            "input_format": "stream-json",
            # "on" | "off" | null (không truyền cờ). Hardening H-15 (FR-RT-10): mặc định "on" — CLI ghi system prompt một lần
            # mỗi conversation và dùng lại nguyên văn khi resume/fork (cùng cơ chế frozen của hub → giữ prompt cache)
            "system_prompt_snapshot": "on",
            # "append" (--append-system-prompt) | "replace" (--system-prompt)
            "system_prompt_mode": "append",
            # Biến môi trường KHÔNG truyền cho tiến trình con (tránh lẫn với phiên Claude Code bao ngoài).
            "env_strip": [
                "CLAUDECODE",
                "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_CHILD_SESSION",
                "CLAUDE_CODE_SESSION_ATTENDED",
                "CLAUDE_CODE_MESSAGING_SOCKET",
                "CLAUDE_CODE_MESSAGING_TOKEN",
                "CLAUDE_CODE_EXECPATH",
                "CLAUDE_PID",
                "CLAUDE_EFFORT",
            ],
            # MCP_TOOL_TIMEOUT (ms): permission tool chờ người (spike A-13) — có tác dụng từ M1
            "env_extra": {"MCP_TOOL_TIMEOUT": "900000"},
            # Nội dung `settings.json` hub sinh cho mỗi run (truyền qua --settings). Hooks của hub vào đây từ M1.
            # Spike A-05: tắt auto-memory của Claude Code (memory do hub quản lý)
            "settings": {"autoMemoryEnabled": False},
            "isolation": {
                # Tổ hợp cờ cô lập — mặc định theo khuyến nghị spike A-04/A-05 (docs/spikes/M0-spike-report.md):
                # --setting-sources "" + --settings <file hub sinh> + --strict-mcp-config --mcp-config <file>.
                # setting_sources: null = không truyền; "" = danh sách rỗng (KHÔNG nạp user/project/local,
                # kể cả CLAUDE.md thư mục cha; OAuth vẫn chạy). "project" vẫn nạp CLAUDE.md cha → tránh.
                "setting_sources": "",
                # truyền --settings <agents/<id>/runtime/settings.json> (file do hub sinh, đã kiểm hợp lệ)
                "pass_settings_file": True,
                # --mcp-config <runtime/mcp.json> + --strict-mcp-config (đã kiểm trong BRIEF)
                "strict_mcp_config": True,
                # các cờ tuỳ chọn khác từ spike, vd. ["--safe-mode"] hoặc ["--restricted"]
                "extra_args": [],
                # Tên hook được phép xuất hiện trong stream (hook của hub). Hook khác → isolation.violation.
                "allowed_hooks": [],
                # MCP server luôn được phép ngoài server agent khai báo (vd "hub-self" từ M1)
                "allowed_mcp_servers": [],
                # Plugin builtin của CLI được chấp nhận (tiền tố); plugin khác trong system/init → vi phạm
                "allowed_plugin_prefixes": ["cc-plugin-"],
                # memory_paths trong system/init phải null (auto-memory tắt)
                "require_no_auto_memory": True,
            },
            # Permission mặc định khi agent không khai báo (null = để mặc định của CLI)
            "default_permission_mode": None,
            # cache `claude --version` (giây)
            "version_cache_s": 600,
            # hardening H-10 (FR-RT-02/15): cờ được phép theo phiên bản CLI. Phiên bản CÓ trong bảng → build_argv ném ArgvError
            # với cờ không có trong danh sách (gồm extra_args). Phiên bản lạ → không kiểm (cảnh báo vàng `cli.version_warning`).
            # Danh sách 2.1.302 lấy từ `claude --help` (bỏ cố ý --dangerously-skip-permissions / --allow-dangerously-skip-permissions).
            # 2.1.303 (spike Windows): `claude --help` vẫn có đủ mọi cờ của 2.1.302 → cùng danh sách (cờ mới chưa dùng thì không thêm).
            "flag_matrix": {
                "2.1.302": _FLAGS_2_1_302,
                "2.1.303": list(_FLAGS_2_1_302),
                # 2.1.310: `claude --help` còn đủ mọi cờ trong danh sách (không cờ nào bị bỏ/đổi tên) → cùng danh sách.
                # + `--chrome`/`--no-chrome` (Claude in Chrome, có trong `claude --help` 2.1.310; spike docs/dev-notes/chrome.md).
                "2.1.310": list(_FLAGS_2_1_302) + ["--chrome", "--no-chrome"],
            },
            # Claude in Chrome (cfharness/browser.py, docs/dev-notes/chrome.md). `enabled` = kill switch CHỈ NGƯỜI đặt (nhánh runtime
            # ∈ integrity.SECURITY_KEYS); tắt → agent `chrome`/`auto` chạy như `playwright`, tool chrome bị guard chặn.
            "chrome": {
                "enabled": True,
                "default_mode": "auto",  # agent KHÔNG đặt tool_policy.browser dùng chế độ này (none|playwright|chrome|auto)
                "max_concurrent_runs": 1,  # Chrome THẬT của người dùng chung → số run dùng tool chrome cùng lúc
                "pass_no_chrome": True,  # run không dùng Chrome nhận --no-chrome (khi phiên bản CLI có cờ)
                "annotations": {},  # ghi đè của người: {glob: {destructiveHint?, readOnlyHint?}} (`mcp annotate claude-in-chrome …`)
            },
            # hardening H-11 (FR-RT-17): hook QUAN SÁT fail-OPEN (cfharness/observe_hook.py) — luôn exit 0, quá timeout_s → hook.timeout
            "observe_hooks": {"enabled": True, "events": ["PostToolUse", "UserPromptSubmit", "Stop"], "timeout_s": 2.0, "cli_timeout_s": 5},
            # hardening H-10: tỉ lệ raw.unknown trong cửa sổ trượt → `cli.unknown_rate_alert` (đỏ, gợi ý ESTOP)
            "unknown_alert": {"threshold": 0.05, "window_s": 3600, "min_events": 50},
        },
    },
}

VALID_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan", "default")


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def get_path(d: dict[str, Any], key: str) -> Any:
    cur: Any = d
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(key)
        cur = cur[part]
    return cur


def set_path(d: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def parse_value(raw: str) -> Any:
    """Giá trị từ dòng lệnh được đọc như YAML: `4` → int, `true` → bool, `null`, `[a, b]`…"""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


class HubConfig:
    """Đọc/ghi `config.yaml`. Chỉ daemon ghi (một writer)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._overrides: dict[str, Any] = {}
        self._mtime: float | None = None
        self.reload()

    def reload(self) -> None:
        if self.path.exists():
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError(f"{self.path}: cấu hình phải là một mapping YAML")
            self._overrides = data
            self._mtime = self.path.stat().st_mtime
        else:
            self._overrides = {}
            self._mtime = None

    def _maybe_reload(self) -> None:
        try:
            m = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            m = None
        if m != self._mtime:
            self.reload()

    @property
    def data(self) -> dict[str, Any]:
        self._maybe_reload()
        return deep_merge(DEFAULTS, self._overrides)

    def get(self, key: str | None = None) -> Any:
        data = self.data
        return data if not key else get_path(data, key)

    def explicit(self, key: str) -> tuple[bool, Any]:
        """(có, giá trị) nếu NGƯỜI đặt rõ `key` trong config.yaml (không tính DEFAULTS; null = coi như không đặt).
        Dùng cho hồ sơ phê duyệt: khoá đặt rõ thắng `approval.profile` (cài đặt cũ có `guard.hold_uncertain` vẫn giữ nguyên)."""
        self._maybe_reload()
        try:
            v = get_path(self._overrides, key)
        except KeyError:
            return False, None
        return (v is not None), v

    def set(self, key: str, value: Any) -> Any:
        validate_setting(key, value)
        self._maybe_reload()
        over = copy.deepcopy(self._overrides)
        set_path(over, key, value)
        if key == "manager" or key.startswith("manager.capabilities"):  # Windows không sandbox OS: khung rộng cần người opt-in
            from .manager.sandbox_gate import check_set

            check_set(key, deep_merge(DEFAULTS, over))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(over, allow_unicode=True, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        self.reload()
        return self.get(key)


def validate_setting(key: str, value: Any) -> None:
    """Kiểm tối thiểu các khoá quan trọng; khoá lạ vẫn cho ghi (mở rộng sau)."""
    if key == "manager" or key.startswith("manager.capabilities"):
        from .manager.permissions import validate_capabilities

        if key == "manager":
            if isinstance(value, dict) and "capabilities" in value:
                validate_capabilities("manager.capabilities", value["capabilities"])
        else:
            validate_capabilities(key, value)
    if key in ("guard", "manager", "manager.web_domains") or key.startswith(("guard.web", "guard.second_opinion")):
        from .guard.url_safety import validate_setting as _validate_web

        _validate_web(key, value)
    if key == "approval" or key.startswith("approval.") or key in ("guard", "guard.hold_uncertain", "guard.safe_reset_probe", "guard.own_worktree_writes",
                                                                     "guard.trusted_repo_scripts", "guard.task_created_deletes"):
        from .guard.profiles import validate_setting as _validate_profile

        _validate_profile(key, value)
    if key == "runtime.claude.chrome" or key.startswith("runtime.claude.chrome."):
        from .browser import validate_setting as _validate_chrome

        _validate_chrome(key, value)
    if key in ("approval.timeout_s", "notify.reminder_interval_s", "guard.hook_timeout_s"):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{key} phải là số > 0")
    if key == "guard.trust_new_agents" and value not in ("off", "full"):
        raise ValueError("guard.trust_new_agents chỉ nhận 'off' hoặc 'full'")
    if key == "notify.reminder_max_count":
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError("notify.reminder_max_count phải là số nguyên ≥ 0 hoặc null")
    if key == "hub.max_concurrent_runs":
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("hub.max_concurrent_runs phải là số nguyên ≥ 1")
    elif key == "runtime.kind":
        if value not in ("claude", "fake"):
            raise ValueError("runtime.kind chỉ nhận 'claude' hoặc 'fake'")
    elif key == "runs.default_max_budget_usd":
        if value is not None and (not isinstance(value, (int, float)) or value <= 0):
            raise ValueError("runs.default_max_budget_usd phải > 0 hoặc null")
    elif key == "learning.review_budget_usd":
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0):
            raise ValueError("learning.review_budget_usd phải > 0 hoặc null (null = review không có trần riêng)")
    elif key == "runtime.claude.default_permission_mode":
        if value is not None and value not in VALID_PERMISSION_MODES:
            raise ValueError(f"permission mode không hợp lệ; chọn một trong {VALID_PERMISSION_MODES}")
