"""Danh mục cấu hình hiện trên trang Cài đặt (web) — khoá nào sửa được, kiểu ô nhập, lựa chọn.

Khoá có tập giá trị cố định (bật/tắt, enum, danh sách chọn nhiều) hiện thành dropdown — không bắt người gõ chữ. Server kiểm lại
giá trị theo đúng danh mục này (`parse_form`), rồi `config.set` của daemon kiểm tiếp (`config.validate_setting`).

`READONLY`: khoá NỚI LỎNG lớp bảo vệ (guard, sandbox, hồ sơ phê duyệt, khung quyền điều phối viên, runtime, gốc workspace,
đích telemetry) — web chỉ HIỂN THỊ, đổi bằng `cfharness config set` trong terminal (docs/dev-notes/web.md §2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import FormData

from ..browser import BROWSER_MODES
from ..config import DEFAULTS, get_path
from ..guard.profiles import ON_TIMEOUT

MODELS = ("haiku", "sonnet", "opus", "fable")
NOTIFY_EVENTS: tuple[str, ...] = tuple(DEFAULTS["notify"]["slack"]["events"])
ACTION_CLASSES = ("file", "db", "mcp", "git", "hub", "system", "policy")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ITEM = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str  # int | float | bool | choice | multi | time | text
    hint: str = ""
    choices: tuple[str, ...] = ()
    nullable: bool = False  # để trống / chọn "(mặc định)" = null
    null_label: str = "(mặc định)"
    min: float | None = None


def _s(key: str, label: str, kind: str, hint: str = "", **kw: Any) -> Setting:
    return Setting(key, label, kind, hint, **kw)


GROUPS: tuple[tuple[str, str, tuple[Setting, ...]], ...] = (
    ("runs", "Run & ngân sách", (
        _s("hub.max_concurrent_runs", "Số run chạy đồng thời tối đa (toàn hub)", "int", "vd 4", min=1),
        _s("runs.default_max_budget_usd", "Ngân sách mặc định mỗi run ($)", "float", "để trống = không giới hạn; vd 1.5", nullable=True, min=0),
        _s("runs.stop_grace_s", "Thời gian chờ dừng êm (giây)", "int", "vd 10", min=0),
        _s("budget.hub_usd_per_day", "Ngân sách ngày toàn hub ($)", "float", "để trống = không giới hạn", nullable=True, min=0),
        _s("budget.warn_pct", "Cảnh báo khi dùng tới (% ngân sách)", "int", "vd 80", min=1),
        _s("recovery.auto_resume", "Tự resume run khi daemon khởi động lại", "bool"),
        _s("workspace.checkpoint", "Checkpoint + diff cho run ghi vào workspace", "bool"),
    )),
    ("approval", "Phê duyệt & câu hỏi", (
        _s("approval.timeout_s", "Giữ chờ duyệt trước khi chuyển 'chờ người' (giây)", "int", "vd 300 — không bao giờ tự từ chối", min=1),
        _s("approval.on_timeout", "Hết thời gian giữ chờ thì", "choice", "wait = run tạm dừng chờ người; deny_and_continue = agent nhận từ chối, chạy tiếp",
           choices=ON_TIMEOUT),
        _s("questions.via_manager", "Agent hỏi điều phối viên trước khi hỏi người", "bool"),
        _s("questions.manager_timeout_s", "Điều phối viên không trả lời sau (giây) → chuyển người", "int", "vd 900", min=1),
        _s("questions.max_open_per_run", "Số câu hỏi mở tối đa mỗi run", "int", "vd 3", min=1),
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
        _s("manager.tick_budget_usd", "Trần chi phí mỗi tick ($)", "float", "vd 0.5", min=0),
        _s("manager.chat_budget_usd", "Trần chi phí mỗi lượt hội thoại ($)", "float", "để trống = theo agent", nullable=True, min=0),
        _s("manager.rotate_after_turns", "Đổi session sau (lượt)", "int", "vd 80", min=1),
        _s("manager.rotate_session_cost_usd", "Đổi session khi chi phí luỹ kế ≥ ($)", "float", "vd 3; 0 = tắt", min=0),
        _s("manager.rotate_context_tokens", "Session lớn từ (token ngữ cảnh)", "int", "vd 40000; tick/lịch luôn đổi session lớn", min=0),
        _s("manager.rotate_idle_s", "Đổi session lớn khi nghỉ ≥ (giây)", "int", "vd 3600 (cache nguội); 0 = tắt", min=0),
        _s("manager.max_agents_created_per_day", "Tạo agent tối đa / ngày", "int", min=0),
        _s("manager.max_tasks_per_hour", "Tạo task tối đa / giờ", "int", min=0),
        _s("manager.max_runs_per_hour", "Chạy run tối đa / giờ", "int", min=0),
        _s("manager.max_system_ops_per_hour", "Thao tác hệ thống ngoài tối đa / giờ", "int", min=0),
    )),
    ("tasks", "Task & team", (
        _s("tasks.failure_limit", "Số lần lỗi tối đa mỗi task", "int", "task không thuộc team", min=1),
        _s("tasks.require_completion_call", "Bắt agent gọi task_complete/task_block", "bool"),
        _s("tasks.max_subtasks", "Số task con tối đa", "int", min=1),
        _s("tasks.review_budget_usd", "Trần chi phí run review ($)", "float", "để trống = như run thường", nullable=True, min=0),
        _s("runtime.claude.chrome.default_mode", "Trình duyệt mặc định của agent", "choice", "chrome/auto = Chrome THẬT của bạn",
           choices=BROWSER_MODES),
    )),
    ("schedules", "Lịch chạy", (
        _s("schedules.enabled", "Bật lịch chạy tự động", "bool", "tắt = không bắn lịch nào (lịch vẫn giữ)"),
        _s("schedules.catchup_grace_s", "Chạy bù nếu trễ không quá (giây)", "int", "vd 3600", min=0),
        _s("schedules.max_failures", "Lỗi liên tiếp → tự tạm dừng", "int", "vd 3", min=1),
        _s("schedules.default_budget_usd", "Trần mỗi lượt khi lịch không đặt ($)", "float", "vd 0.5", min=0),
        _s("schedules.manager_max_active", "Điều phối viên: số lịch đang bật tối đa", "int", min=0),
        _s("schedules.min_interval_s", "Điều phối viên: khoảng tối thiểu giữa 2 lượt (giây)", "int", min=0),
    )),
    ("learning", "Học & đánh giá", (
        _s("learning.review_budget_usd", "Trần chi phí review học ($)", "float", "để trống = không trần riêng", nullable=True, min=0),
        _s("learning.review_model", "Model review", "choice", "mặc định = cùng model với run gốc (giữ cache)", choices=MODELS, nullable=True),
        _s("learning.review_scope", "Phạm vi review", "choice", choices=("combined", "skill", "memory")),
        _s("learning.review_max_per_agent_per_day", "Review tối đa / agent / ngày", "int", min=0),
        _s("learning.canary_runs", "Canary: số run", "int", min=1),
        _s("learning.canary_hours", "Canary: số giờ", "int", min=1),
        _s("eval.enabled", "Bật eval trước khi áp dụng", "bool"),
        _s("eval.samples", "Số lần chạy mỗi golden task", "int", min=1),
        _s("eval.budget_usd", "Trần chi phí một lần gate ($)", "float", min=0),
        _s("eval.run_budget_usd", "Trần mỗi run eval ($)", "float", min=0),
        _s("eval.batch_enabled", "Eval theo lô hằng ngày", "bool"),
        _s("eval.batch_time", "Giờ chạy eval theo lô (giờ máy)", "time"),
        _s("eval.judge.model", "Model chấm điểm (judge)", "choice", choices=MODELS),
        _s("curator.enabled", "Bật curator (dọn skill cũ)", "bool"),
        _s("curator.interval_days", "Curator chạy mỗi (ngày)", "int", min=1),
        _s("curation.enabled", "Bật curation liên agent", "bool"),
        _s("curation.model", "Model curation", "choice", choices=MODELS),
        _s("curation.daily_budget_usd", "Trần chi phí curation / ngày ($)", "float", min=0),
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

# Khoá nới lỏng bảo vệ: web chỉ hiện giá trị + lệnh CLI.
READONLY: tuple[tuple[str, str], ...] = (
    ("gates.mode", "Cổng duyệt toàn hệ thống (thẻ Cổng duyệt ở trên)"),
    ("guard.enabled", "Bật guard (chặn thao tác phá huỷ)"),
    ("approval.profile", "Hồ sơ phê duyệt"),
    ("guard.hold_uncertain", "Giữ chờ lệnh không chắc (uncertain)"),
    ("guard.trust_new_agents", "Trust mặc định cho agent mới"),
    ("approval.checkpoint_auto_allow", "Tự cho phép phá huỷ có checkpoint"),
    ("approval.scratch_auto_allow", "Tự cho phép phá huỷ trong scratch/worktree"),
    ("approval.delegate_to_manager.enabled", "Giao điều phối viên duyệt"),
    ("guard.second_opinion.enabled", "LLM thứ hai cho uncertain"),
    ("guard.sandbox.enabled", "Sandbox OS cho Bash"),
    ("guard.sandbox.allowed_domains", "Domain mạng Bash được ra"),
    ("guard.sandbox.allow_ssh_agent", "Cho agent dùng ssh-agent (git push)"),
    ("guard.web.allow_domains", "Domain WebFetch được phép"),
    ("manager.capabilities.profile", "Khung quyền điều phối viên"),
    ("manager.web_domains", "WebFetch của điều phối viên"),
    ("manager.allow_hermes_without_sandbox", "Khung rộng khi không có sandbox (Windows)"),
    ("workspace.allowed_roots", "Gốc workspace được phép"),
    ("runtime.kind", "Runtime"),
    ("runtime.claude.default_permission_mode", "Chế độ quyền mặc định"),
    ("runtime.claude.chrome.enabled", "Cho dùng Claude in Chrome"),
    ("telemetry.otel.endpoint", "Đích OTLP"),
    ("telemetry.otel.include_payloads", "Gửi kèm input tool"),
)

BY_KEY: dict[str, Setting] = {s.key: s for _, _, items in GROUPS for s in items}


def _num(raw: str, s: Setting) -> int | float:
    try:
        v: int | float = int(raw) if s.kind == "int" else float(raw)
    except ValueError:
        raise ValueError(f"{s.label}: cần {'số nguyên' if s.kind == 'int' else 'số'}, nhận '{raw}'") from None
    if s.min is not None and v < s.min:
        raise ValueError(f"{s.label}: phải ≥ {s.min:g}")
    return v


def parse_form(form: FormData) -> tuple[str, Any]:
    """(key, value) từ form `config_set` — chỉ khoá trong danh mục, giá trị đúng kiểu/lựa chọn."""
    key = str(form.get("key") or "").strip()
    s = BY_KEY.get(key)
    if s is None:
        raise ValueError(f"khoá '{key}' không sửa được từ web — dùng `cfharness config set` trong terminal")
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
            elif s.kind == "bool":
                rows.append({"s": s, "value": "" if v is None else ("true" if v else "false")})
            else:
                rows.append({"s": s, "value": "" if v is None else str(v)})
        groups.append({"id": gid, "title": title, "rows": rows})
    ro = [{"key": k, "label": label, "value": show(val(k))} for k, label in READONLY]
    return {"groups": groups, "readonly": ro}
