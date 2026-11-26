"""Danh mục cấu hình hiện trên trang Cài đặt (web) — khoá nào sửa được, kiểu ô nhập, lựa chọn.

Khoá có tập giá trị cố định (bật/tắt, enum, danh sách chọn nhiều) hiện thành dropdown — không bắt người gõ chữ. Server kiểm lại
giá trị theo đúng danh mục này (`parse_form`), rồi `config.set` của daemon kiểm tiếp (`config.validate_setting`).

Khoá BẢO VỆ (`protected=True`: guard, sandbox, hồ sơ phê duyệt, khung quyền điều phối viên, runtime, gốc workspace, web public,
đích telemetry) — có thể NỚI LỎNG lớp bảo vệ nên chỉ sửa được khi web dùng trên CHÍNH máy chạy hub (không qua tunnel/link public), qua
thao tác riêng `config_set_protected` có xác nhận 2 bước + audit (web/app.py). Qua link public: chỉ xem. `READONLY` (cũ) nay rỗng —
mọi khoá đều chỉnh được trên UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import FormData

from ..browser import BROWSER_MODES
from ..config import DEFAULTS, VALID_PERMISSION_MODES, get_path
from ..guard.profiles import GATES_MODES, ON_TIMEOUT, PROFILES

MODELS = ("haiku", "sonnet", "opus", "fable")
NOTIFY_EVENTS: tuple[str, ...] = tuple(DEFAULTS["notify"]["slack"]["events"])
ACTION_CLASSES = ("file", "db", "mcp", "git", "hub", "system", "policy")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ITEM = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str  # int | float | bool | choice | multi | time | text | list (mỗi dòng / dấu phẩy một mục)
    hint: str = ""
    choices: tuple[str, ...] = ()
    nullable: bool = False  # để trống / chọn "(mặc định)" = null
    null_label: str = "(mặc định)"
    min: float | None = None
    protected: bool = False  # khoá bảo vệ: chỉ sửa trên máy chạy hub, xác nhận 2 bước (config_set_protected)


def _s(key: str, label: str, kind: str, hint: str = "", **kw: Any) -> Setting:
    return Setting(key, label, kind, hint, **kw)


GROUPS: tuple[tuple[str, str, tuple[Setting, ...]], ...] = (
    ("runs", "Run & ngân sách", (
        _s("hub.max_concurrent_runs", "Số run chạy đồng thời tối đa (toàn hub)", "int", "để trống = không giới hạn; vd 4", nullable=True, min=1),
        _s("runs.default_max_budget_usd", "Ngân sách mặc định mỗi run ($)", "float", "để trống = không giới hạn; vd 1.5", nullable=True, min=0),
        _s("runs.stop_grace_s", "Thời gian chờ dừng êm (giây)", "int", "vd 10", min=0),
        _s("budget.hub_usd_per_day", "Ngân sách ngày toàn hub ($)", "float", "để trống = không giới hạn", nullable=True, min=0),
        _s("budget.warn_pct", "Cảnh báo khi dùng tới (% ngân sách)", "int", "vd 80", min=1),
        _s("recovery.auto_resume", "Tự resume run khi daemon khởi động lại", "bool"),
        _s("recovery.auto_resume_enabled", "Tự resume run ad-hoc dừng vì lỗi tạm thời (crash, quá giờ, rate limit, mạng)", "bool"),
        _s("recovery.auto_resume_max_attempts", "Số lần tự resume tối đa liên tiếp (gồm sau restart)", "int", "vd 3", min=0),
        _s("workspace.checkpoint", "Checkpoint + diff cho run ghi vào workspace", "bool"),
    )),
    ("approval", "Phê duyệt & câu hỏi", (
        _s("approval.timeout_s", "Giữ chờ duyệt trước khi chuyển 'chờ người' (giây)", "int", "vd 300 — không bao giờ tự từ chối", min=1),
        _s("approval.on_timeout", "Hết thời gian giữ chờ thì", "choice", "wait = run tạm dừng chờ người; deny_and_continue = agent nhận từ chối, chạy tiếp",
           choices=ON_TIMEOUT),
        _s("questions.via_manager", "Agent hỏi điều phối viên trước khi hỏi người", "bool"),
        _s("questions.manager_timeout_s", "Điều phối viên không trả lời sau (giây) → chuyển người", "int", "vd 900", min=1),
        _s("questions.max_open_per_run", "Số câu hỏi mở tối đa mỗi run", "int", "để trống = không giới hạn; vd 3", nullable=True, min=1),
    )),
    ("notify", "Thông báo", (
        _s("notify.reminder_interval_s", "Nhắc lại yêu cầu duyệt mỗi (giây)", "int", "vd 900, giãn dần", min=1),
        _s("notify.reminder_max_interval_s", "Khoảng nhắc lại tối đa (giây)", "int", "vd 7200", min=1),
        _s("notify.reminder_max_count", "Số lần nhắc tối đa mỗi yêu cầu", "int", "để trống = không giới hạn; 0 = không nhắc", nullable=True, min=0),
        _s("notify.repeated_failure_threshold", "Báo khi run lỗi liên tiếp", "int", "vd 3", min=1),
        _s("notify.slack.enabled", "Bật thông báo Slack", "bool"),
        _s("notify.slack.events", "Sự kiện gửi qua Slack", "multi", choices=NOTIFY_EVENTS),
        _s("notify.slack.confirm_classes", "Slack: lớp thao tác duyệt 2 bước", "multi", choices=ACTION_CLASSES),
        _s("notify.slack.busy_indicator", "Slack: báo đang xử lý bằng", "choice", "status = dòng 'đang xử lý…'; reaction = chỉ icon ⏳",
           choices=("status", "reaction")),
        _s("notify.slack.allow_channels", "Slack: cho tag bot trong kênh chung", "bool", "vẫn chỉ người trong allowlist"),
        _s("notify.slack.thread_followup", "Slack: nói tiếp trong thread không cần tag", "bool"),
        _s("notify.slack.channel_root_posts", "Slack: báo cáo lịch đăng thẳng vào kênh (không vào thread)", "bool",
           "chỉ kết quả lịch có nơi nhận là kênh đó; trả lời chat vẫn vào thread"),
        _s("notify.slack.mention_reply", "Slack: kiểu trả lời khi được tag trong kênh", "choice",
           "full = như cũ (nói đã giao việc gì…); result = chỉ kết quả, ngắn gọn, không kể cách xử lý", choices=("full", "result")),
        _s("notify.slack.thread_context", "Slack: đọc tin trước đó của thread làm ngữ cảnh", "bool"),
        _s("notify.slack.manager_progress", "Slack: tin tiến độ + nút Dừng cho điều phối viên", "bool"),
        _s("notify.slack.deliver_files", "Slack: gửi tệp kết quả", "bool"),
        _s("notify.telegram.enabled", "Bật thông báo Telegram", "bool"),
        _s("notify.telegram.events", "Sự kiện gửi qua Telegram", "multi", choices=NOTIFY_EVENTS),
        _s("notify.telegram.confirm_classes", "Telegram/web: lớp thao tác duyệt 2 bước", "multi", choices=ACTION_CLASSES),
        _s("notify.telegram.allow_groups", "Telegram: cho dùng trong nhóm", "bool", "vẫn chỉ người trong allowlist"),
        _s("notify.telegram.deliver_files", "Telegram: gửi tệp kết quả", "bool"),
    )),
    ("manager", "Điều phối viên", (
        _s("manager.heartbeat", "Nhịp heartbeat (tự tick)", "bool", "tắt = chỉ chạy khi người nhắn"),
        _s("manager.tick_interval_s", "Tick định kỳ dự phòng (giây)", "int", "vd 1800", min=1),
        _s("manager.tick_budget_usd", "Trần chi phí mỗi tick ($)", "float", "để trống = không giới hạn; vd 0.5", nullable=True, min=0),
        _s("manager.chat_budget_usd", "Trần chi phí mỗi lượt hội thoại ($)", "float", "để trống = theo agent", nullable=True, min=0),
        _s("manager.rotate_after_turns", "Đổi session sau (lượt)", "int", "vd 80", min=1),
        _s("manager.rotate_session_cost_usd", "Đổi session khi chi phí luỹ kế ≥ ($)", "float", "vd 3; 0 = tắt", min=0),
        _s("manager.rotate_context_tokens", "Session lớn từ (token ngữ cảnh)", "int", "vd 40000; tick/lịch luôn đổi session lớn", min=0),
        _s("manager.rotate_idle_s", "Đổi session lớn khi nghỉ ≥ (giây)", "int", "vd 3600 (cache nguội); 0 = tắt", min=0),
        _s("manager.max_agents_created_per_day", "Tạo agent tối đa / ngày", "int", "để trống = không giới hạn", nullable=True, min=0),
        _s("manager.max_tasks_per_hour", "Tạo task tối đa / giờ", "int", "để trống = không giới hạn", nullable=True, min=0),
        _s("manager.max_runs_per_hour", "Chạy run tối đa / giờ", "int", "để trống = không giới hạn", nullable=True, min=0),
        _s("manager.max_system_ops_per_hour", "Thao tác hệ thống ngoài tối đa / giờ", "int", "để trống = không giới hạn", nullable=True, min=0),
        _s("attachments.pdf.split", "PDF đính kèm dài: tách thành các phần", "bool", "điều phối viên đọc từng phần (giữ bảng/hình); luôn có bản chữ"),
        _s("attachments.pdf.split_pages", "Số trang mỗi phần PDF", "int", "vd 50; PDF nhiều hơn N trang mới tách (1–100)", min=1),
        _s("attachments.pdf.max_parts", "Số phần tối đa mỗi PDF", "int", "vd 20 (1–200); tự nới trang/phần để phủ hết PDF", min=1),
    )),
    ("tasks", "Task & team", (
        _s("tasks.failure_limit", "Số lần lỗi tối đa mỗi task", "int", "task không thuộc team", min=1),
        _s("tasks.require_completion_call", "Bắt agent gọi task_complete/task_block", "bool"),
        _s("tasks.max_subtasks", "Số task con tối đa", "int", "để trống = không giới hạn", nullable=True, min=1),
        _s("tasks.review_budget_usd", "Trần chi phí run review ($)", "float", "để trống = như run thường", nullable=True, min=0),
        _s("runtime.claude.chrome.default_mode", "Trình duyệt mặc định của agent", "choice", "chrome/auto = Chrome THẬT của bạn",
           choices=BROWSER_MODES),
        _s("runtime.claude.chrome.max_concurrent_runs", "Số run dùng Chrome cùng lúc", "int", "để trống = không giới hạn; vd 1 (Chrome thật dùng chung)",
           nullable=True, min=1),
    )),
    ("schedules", "Lịch chạy", (
        _s("schedules.enabled", "Bật lịch chạy tự động", "bool", "tắt = không bắn lịch nào (lịch vẫn giữ)"),
        _s("schedules.catchup_grace_s", "Chạy bù nếu trễ không quá (giây)", "int", "vd 3600", min=0),
        _s("schedules.max_failures", "Lỗi liên tiếp → tự tạm dừng", "int", "vd 3", min=1),
        _s("schedules.default_budget_usd", "Trần mỗi lượt khi lịch không đặt ($)", "float", "để trống = không giới hạn; vd 0.5", nullable=True, min=0),
        _s("schedules.manager_max_active", "Điều phối viên: số lịch đang bật tối đa", "int", "để trống = không giới hạn", nullable=True, min=0),
        _s("schedules.min_interval_s", "Điều phối viên: khoảng tối thiểu giữa 2 lượt (giây)", "int", "để trống = không giới hạn", nullable=True, min=0),
        _s("schedules.manager_run_now_per_hour", "Điều phối viên: chạy-ngay tối đa / giờ", "int", "để trống = không giới hạn", nullable=True, min=0),
    )),
    ("learning", "Học & đánh giá", (
        _s("learning.review_budget_usd", "Trần chi phí review học ($)", "float", "để trống = không trần riêng", nullable=True, min=0),
        _s("learning.review_budget_max_usd", "Trần tối đa review học kể cả nạp lại ngữ cảnh ($)", "float", "để trống = không chặn trên",
           nullable=True, min=0),
        _s("learning.review_model", "Model review", "choice", "mặc định = cùng model với run gốc (giữ cache)", choices=MODELS, nullable=True),
        _s("learning.review_scope", "Phạm vi review", "choice", "graph = review trích fact vào graph memory", choices=("combined", "skill", "memory", "graph")),
        _s("learning.graph_extract", "Trích xuất graph memory cuối session", "bool", "một lần gọi LLM rẻ mỗi session"),
        _s("learning.graph_mirror_memory", "Mirror MEMORY.md/USER.md vào graph", "bool", "mục memory mới → fact graph cùng tầng; mục mất → đóng fact"),
        _s("learning.graph_extract_budget_usd", "Trần mỗi lần trích xuất graph ($)", "float", "để trống = không giới hạn", nullable=True, min=0),
        _s("learning.graph_extract_daily_usd_per_agent", "Trần trích xuất graph / agent / ngày ($)", "float", "để trống = không giới hạn", nullable=True, min=0),
        _s("learning.review_max_per_agent_per_day", "Review tối đa / agent / ngày", "int", "để trống = không giới hạn", nullable=True, min=0),
        _s("learning.canary_runs", "Canary: số run", "int", min=1),
        _s("learning.canary_hours", "Canary: số giờ", "int", min=1),
        _s("eval.enabled", "Bật eval trước khi áp dụng", "bool"),
        _s("eval.samples", "Số lần chạy mỗi golden task", "int", min=1),
        _s("eval.budget_usd", "Trần chi phí một lần gate ($)", "float", "để trống = không giới hạn", nullable=True, min=0),
        _s("eval.run_budget_usd", "Trần mỗi run eval ($)", "float", "để trống = không giới hạn", nullable=True, min=0),
        _s("eval.batch_enabled", "Eval theo lô hằng ngày", "bool"),
        _s("eval.batch_time", "Giờ chạy eval theo lô (giờ máy)", "time"),
        _s("eval.judge.model", "Model chấm điểm (judge)", "choice", choices=MODELS),
        _s("curator.enabled", "Bật curator (dọn skill cũ)", "bool"),
        _s("curator.interval_days", "Curator chạy mỗi (ngày)", "int", min=1),
        _s("curation.enabled", "Bật curation liên agent", "bool"),
        _s("curation.model", "Model curation", "choice", choices=MODELS),
        _s("curation.daily_budget_usd", "Trần chi phí curation / ngày ($)", "float", "để trống = không giới hạn", nullable=True, min=0),
        _s("curation.cross_agent.llm_merge", "Gộp skill bằng LLM", "bool"),
        _s("curation.phase2.enabled", "Curator pha 2 (LLM)", "bool"),
        _s("digest.enabled", "Bật bản tin học hằng ngày", "bool"),
        _s("digest.schedule", "Giờ gửi bản tin (giờ máy)", "time"),
        _s("digest.send_empty", "Gửi cả bản tin trống", "bool"),
    )),
    ("retention", "Lưu trữ", (
        _s("retention.soft_delete_days", "Giữ thùng rác (ngày)", "int", "vd 30", min=0),
        _s("retention.auto_purge", "Tự xoá hẳn khi quá hạn thùng rác", "bool"),
        _s("retention.events_days", "Giữ event/transcript (ngày)", "int", "để trống = vĩnh viễn", nullable=True, min=0),
        _s("retention.raw_days", "Giữ stdout thô (ngày)", "int", "để trống = vĩnh viễn", nullable=True, min=0),
        _s("retention.checkpoints_days", "Giữ checkpoint (ngày)", "int", "cửa sổ `run revert`", nullable=True, min=0),
    )),
    ("telemetry", "Telemetry", (
        _s("telemetry.otel.enabled", "Bật OpenTelemetry", "bool", "chỉ metadata"),
        _s("telemetry.otel.exporter", "Exporter", "choice", choices=("otlp_http", "file")),
    )),
)

# `files.*`: sửa được trên web, nhưng thay đổi NỚI LỎNG (fileview.loosens) chỉ nhận khi dùng web trên chính máy chạy hub
# (kiểm ở action `config_set` của app.py) — qua link public chỉ được siết.
FILES_GROUP = ("files", "Xem / mở file", (
    _s("files.cloud_download", "Cho tải file về khi dùng web qua link public (cloud)", "bool", "xem trong trình duyệt luôn được"),
    _s("files.max_download_mb", "Dung lượng tối đa khi tải về / xem ảnh, PDF (MB)", "int", "1–50", min=1),
    _s("files.open_extensions", "Loại file được mở bằng ứng dụng (máy chạy hub)", "list",
       "mỗi dòng một đuôi, vd .pdf — chương trình/script/HTML/Office có macro luôn bị từ chối"),
    _s("files.secret_patterns", "Tên file coi là bí mật (không xem/mở/tải qua web)", "list", "mỗi dòng một tên hoặc mẫu glob, vd .env, *.pem"),
    _s("files.secret_allow", "Ngoại lệ (không coi là bí mật)", "list", "vd .env.example, *.pub"),
))
GROUPS = GROUPS + (FILES_GROUP,)

def _p(key: str, label: str, kind: str, hint: str = "", **kw: Any) -> Setting:
    return Setting(key, label, kind, hint, protected=True, **kw)


_BY_PROFILE = "(theo hồ sơ phê duyệt)"
# Khoá BẢO VỆ (trước đây chỉ xem — nay sửa được trên UI khi dùng web trên chính máy chạy hub, xác nhận 2 bước)
PROTECTED_GROUP = ("security", "Bảo vệ & quyền", (
    _p("gates.mode", "Cổng duyệt toàn hệ thống", "choice", "human = người duyệt; manager = điều phối viên quyết; off = không cổng chờ người",
       choices=tuple(GATES_MODES)),
    _p("guard.enabled", "Bật guard (chặn thao tác phá huỷ)", "bool", "tắt = không hook phân loại/hỏi duyệt"),
    _p("approval.profile", "Hồ sơ phê duyệt", "choice", "strict = hỏi nhiều nhất; autonomous = tự cho nhiều nhất", choices=tuple(PROFILES)),
    _p("guard.hold_uncertain", "Giữ chờ lệnh không chắc (uncertain)", "bool", nullable=True, null_label=_BY_PROFILE),
    _p("guard.trust_new_agents", "Trust mặc định cho agent mới", "choice", "full = agent mới chạy không hỏi duyệt", choices=("off", "full")),
    _p("approval.checkpoint_auto_allow", "Tự cho phép phá huỷ có checkpoint", "bool", nullable=True, null_label=_BY_PROFILE),
    _p("approval.scratch_auto_allow", "Tự cho phép phá huỷ trong scratch/worktree", "bool", nullable=True, null_label=_BY_PROFILE),
    _p("approval.delegate_to_manager.enabled", "Giao điều phối viên duyệt", "bool", nullable=True, null_label=_BY_PROFILE),
    _p("guard.second_opinion.enabled", "LLM thứ hai cho lệnh không chắc", "bool"),
    _p("guard.sandbox.enabled", "Sandbox OS cho Bash", "bool"),
    _p("guard.sandbox.allowed_domains", "Domain mạng Bash được ra", "list", "mỗi dòng một domain, vd registry.npmjs.org, *.pypi.org"),
    _p("guard.sandbox.allow_ssh_agent", "Cho agent dùng ssh-agent (git push)", "bool"),
    _p("guard.web.allow_domains", "Domain WebFetch được phép (toàn hub)", "list", "trống = không giới hạn"),
    _p("manager.capabilities.profile", "Khung quyền điều phối viên", "choice",
       "strict = chỉ đọc (Read/Grep/Glob) + tool hub; hermes = thêm Bash/Write/Edit/Web trong thư mục hội thoại", choices=("strict", "hermes")),
    _p("manager.web_domains", "WebFetch của điều phối viên", "text", "any = mọi domain; hoặc danh sách domain cách nhau dấu phẩy"),
    _p("manager.allow_hermes_without_sandbox", "Khung rộng khi không có sandbox OS (Windows)", "bool"),
    _p("workspace.allowed_roots", "Gốc workspace được phép", "list", "mỗi dòng một thư mục, vd ~/Workspace"),
    _p("workspace.confine.root", "Giam agent trong workspace (W)", "text", "thư mục gốc; trống = tắt", nullable=True, null_label="(tắt)"),
    _p("runtime.kind", "Runtime toàn hub", "choice", "fake = phát lại fixture (demo, không gọi model)", choices=("claude", "fake")),
    _p("runtime.claude.default_permission_mode", "Chế độ quyền mặc định (Claude)", "choice", nullable=True,
       choices=tuple(VALID_PERMISSION_MODES)),
    _p("runtime.claude.chrome.enabled", "Cho dùng Claude in Chrome", "bool"),
    _p("tools.poppler_path", "Thư mục poppler (pdftoppm) cho run", "text", nullable=True),
    _p("web.public_origin", "Địa chỉ web public (tunnel TLS)", "text", "vd https://an.example.com — trống = chỉ trên máy này", nullable=True,
       null_label="(chỉ trên máy này)"),
    _p("web.session_max_age_s", "Tuổi tối đa phiên web (giây)", "int", min=60),
    _p("web.trusted_devices.enabled", "Cho tin cậy thiết bị (web public + TOTP)", "bool"),
    _p("web.trusted_devices.ttl_days", "Hạn thiết bị tin cậy (ngày, tuyệt đối)", "int", min=1),
    _p("telemetry.otel.endpoint", "Đích OTLP", "text"),
    _p("telemetry.otel.include_payloads", "Gửi kèm input tool", "bool"),
))
# runtime codex / grok theo agent (đường dẫn CLI / model / effort ở thẻ tài khoản — Cài đặt › Tài khoản AI)
RUNTIME_GROUP = ("runtimes", "ChatGPT (Codex) & Grok", (
    _p("runtime.codex.sandbox", "Sandbox mặc định của Codex", "choice", "agent permission_mode=plan / khung chỉ đọc luôn read-only",
       choices=("workspace-write", "read-only")),
    _p("runtime.codex.approval_policy", "Codex hỏi duyệt", "choice", "untrusted = mọi lệnh/patch hỏi guard (khuyên dùng)",
       choices=("untrusted", "on-request")),
    _p("runtime.codex.windows_sandbox", "Sandbox Windows của Codex", "choice", "mxc (Windows 11 24H2+) — thiếu thì lệnh đã duyệt chạy ngoài sandbox",
       choices=("mxc", "elevated")),
    _p("runtime.codex.mcp_approval_mode", "Tool MCP của hub (Codex)", "choice", "prompt = mọi tool hỏi guard; writes = chỉ tool ghi",
       choices=("prompt", "writes")),
    _p("runtime.codex.approval_timeout_s", "Codex: chờ quyết định tối đa (giây)", "int", "hết giờ = từ chối", min=10),
    _p("runtime.grok.isolate", "Grok: cô lập khỏi cấu hình Claude/Cursor của máy", "bool", "tắt = Grok nạp skill/MCP/hook của Claude Code trên máy"),
    _p("runtime.grok.approval_timeout_s", "Grok: chờ quyết định tối đa (giây)", "int", "hết giờ = từ chối", min=10),
))
GROUPS = GROUPS + (PROTECTED_GROUP, RUNTIME_GROUP)
READONLY: tuple[tuple[str, str], ...] = ()  # (cũ) mọi khoá bảo vệ nay ở PROTECTED_GROUP

BY_KEY: dict[str, Setting] = {s.key: s for _, _, items in GROUPS for s in items}


def _num(raw: str, s: Setting) -> int | float:
    try:
        v: int | float = int(raw) if s.kind == "int" else float(raw)
    except ValueError:
        raise ValueError(f"{s.label}: cần {'số nguyên' if s.kind == 'int' else 'số'}, nhận '{raw}'") from None
    if s.min is not None and v < s.min:
        raise ValueError(f"{s.label}: phải ≥ {s.min:g}")
    return v


def parse_form(form: FormData, *, protected: bool = False) -> tuple[str, Any]:
    """(key, value) từ form `config_set` — chỉ khoá trong danh mục, giá trị đúng kiểu/lựa chọn. Khoá bảo vệ CHỈ qua
    `config_set_protected` (`protected=True`: kiểm máy chạy hub + xác nhận 2 bước) — form thường không đổi được."""
    key = str(form.get("key") or "").strip()
    s = BY_KEY.get(key)
    if s is None:
        raise ValueError(f"khoá '{key}' không sửa được từ web — dùng `cfharness config set` trong terminal")
    if s.protected and not protected:
        raise ValueError(f"'{s.label}' là khoá bảo vệ — sửa ở mục Bảo vệ & quyền (chỉ trên máy chạy hub)")
    if protected and not s.protected:
        raise ValueError(f"'{s.label}' không phải khoá bảo vệ")
    if s.kind == "list":
        items = [x.strip() for x in str(form.get("value") or "").replace(",", "\n").split("\n")]
        return key, list(dict.fromkeys(x for x in items if x))
    if s.kind == "multi":
        picked = [str(v) for v in form.getlist("value") if str(v)]
        bad = [v for v in picked if v not in s.choices and not _ITEM.match(v)]
        if bad:
            raise ValueError(f"{s.label}: lựa chọn không hợp lệ {', '.join(bad)}")
        # giữ thứ tự danh mục; mục ngoài danh mục (người đặt tay bằng CLI, được hiện sẵn tích) giữ ở cuối
        return key, [c for c in s.choices if c in picked] + [v for v in dict.fromkeys(picked) if v not in s.choices]
    raw = str(form.get("value") or "").strip()
    if raw == "":
        if s.nullable:
            return key, None
        raise ValueError(f"{s.label}: không được để trống")
    if s.kind == "bool":
        if raw not in ("true", "false"):
            raise ValueError(f"{s.label}: chọn Bật hoặc Tắt")
        return key, raw == "true"
    if s.kind == "choice":
        if raw not in s.choices:
            raise ValueError(f"{s.label}: chọn một trong {', '.join(s.choices)}")
        return key, raw
    if s.kind == "time":
        if not _TIME.match(raw):
            raise ValueError(f"{s.label}: dạng HH:MM, vd 08:00")
        return key, raw
    if s.kind in ("int", "float"):
        return key, _num(raw, s)
    return key, raw


def show(v: Any) -> str:
    """Giá trị hiển thị (chỉ đọc)."""
    if v is None:
        return "(mặc định)"
    if isinstance(v, bool):
        return "bật" if v else "tắt"
    if isinstance(v, (list, tuple)):
        return ", ".join(map(str, v)) if v else "(trống)"
    return str(v)


def view(cfg: dict[str, Any]) -> dict[str, Any]:
    """Dữ liệu cho template: nhóm sửa được (giá trị hiện tại theo kiểu) + bảng chỉ đọc."""

    def val(key: str) -> Any:
        try:
            return get_path(cfg, key)
        except KeyError:
            return None

    groups = []
    for gid, title, items in GROUPS:
        rows = []
        for s in items:
            v = val(s.key)
            if s.kind == "multi":
                cur: Any = [str(x) for x in (v or [])]
                extra = [x for x in cur if x not in s.choices]  # giá trị người đặt tay ngoài danh mục: vẫn hiện, bỏ tích = gỡ
                rows.append({"s": s, "value": cur, "choices": list(s.choices) + extra})
            elif s.kind == "list":
                rows.append({"s": s, "value": "\n".join(str(x) for x in (v or [])), "count": len(v or [])})
            elif s.kind == "bool":
                rows.append({"s": s, "value": "" if v is None else ("true" if v else "false")})
            else:
                rows.append({"s": s, "value": "" if v is None else str(v)})
        groups.append({"id": gid, "title": title, "rows": rows})
    ro = [{"key": k, "label": label, "value": show(val(k))} for k, label in READONLY]
    return {"groups": groups, "readonly": ro}
