"""Sự kiện thông báo theo nhóm + nhãn tiếng Việt (plan-chat-setup-web §3.4, PR4) — THUẦN, không I/O.

Nguồn DUY NHẤT của: nhãn từng loại event mà kênh Slack/Telegram gửi được (`EVENT_LABELS`), nhóm hiển thị (`EVENT_GROUPS`),
preset "Nhận thông báo gì" (`PRESETS`) và tập event hợp lệ cho `notify.<kênh>.events` (`KNOWN_EVENTS`, kiểm ở
`config.validate_setting`). Ghi vẫn là danh sách đầy đủ vào `notify.slack.events` / `notify.telegram.events`.

Thêm loại event mà notifier gửi được → thêm nhãn + nhóm ở đây (test `tests/test_notify_events.py` đỏ nếu quên).
Câu hỏi của agent (`question.*`) KHÔNG đi qua bộ lọc này — kênh chat 2 chiều luôn gửi (chat/questions_tg.py).
"""

from __future__ import annotations

from collections.abc import Iterable

from .config import DEFAULTS

CHANNELS: tuple[str, ...] = ("slack", "telegram")

# (id nhóm, tiêu đề, mô tả ngắn, [(event, nhãn)]) — thứ tự nhóm/mục = thứ tự hiển thị và thứ tự ghi vào config
_GROUPS: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    ("needs_you", "Cần bạn", "agent đang chờ bạn quyết", (
        ("approval.open", "Agent cần bạn duyệt một thao tác"),
        ("approval.waiting_human", "Agent đã tạm dừng, vẫn chờ bạn duyệt"),
        ("approval.reminder", "Nhắc lại yêu cầu duyệt chưa xử lý"),
        ("approval.escalated", "Điều phối viên chuyển yêu cầu duyệt cho bạn"),
        ("team.inbox_new", "Có việc mới trong hộp việc của team"),
    )),
    ("errors", "Lỗi & ngân sách", "run hỏng liên tiếp, sắp hoặc đã hết tiền", (
        ("run.repeated_failure", "Run lỗi nhiều lần liên tiếp"),
        ("budget.warning", "Sắp chạm ngân sách"),
        ("budget.exceeded", "Đã vượt ngân sách"),
    )),
    ("learning", "Học & digest", "agent tự học, bản tin hằng ngày", (
        ("learning.digest", "Bản tin học hằng ngày"),
        ("learning.rolled_back", "Thay đổi tự học bị hoàn tác"),
        ("regression.detected", "Agent kém đi sau một thay đổi"),
        ("golden.suggested", "Gợi ý tạo bài kiểm tra mẫu (golden task)"),
        ("golden.suggestion_opened", "Đề xuất bài kiểm tra mẫu có nút duyệt"),
    )),
    ("security", "Bảo mật", "chặn, phát hiện can thiệp, đăng nhập web", (
        ("guard.blocked", "Guard đã chặn một thao tác của agent"),
        ("guard.tamper_detected", "Phát hiện sửa trái phép cấu hình/chính sách"),
        ("guard.env_tamper", "Tệp lạ trong môi trường Python của hub"),
        ("auth.denied", "Lời gọi hub bị từ chối quyền"),
        ("audit.alert", "Cảnh báo bất thường trong nhật ký kiểm toán"),
        ("audit.tamper_detected", "Nhật ký kiểm toán bị sửa"),
        ("audit.anchored", "Bản neo nhật ký kiểm toán định kỳ"),
        ("admin.alert", "Cảnh báo của quản trị máy"),
        ("hub.admin_estop", "Quản trị máy dừng khẩn cấp hub"),
        ("web.login", "Đăng nhập web"),
        ("web.logout", "Đăng xuất web"),
        ("web.login_locked", "Khoá đăng nhập web vì thử sai nhiều lần"),
        ("web.device_trusted", "Thiết bị mới được tin cậy"),
        ("web.device_reuse_detected", "Thiết bị tin cậy bị dùng lại bất thường"),
        ("web.device_revoked", "Thu hồi một thiết bị tin cậy"),
        ("web.devices_revoked_all", "Thu hồi mọi thiết bị tin cậy"),
    )),
    ("filehub", "Thư mục chung", "Không gian số — tệp agent đổi/xoá", (
        ("filehub.changed", "Tệp trong thư mục chung đã đổi"),
        ("filehub.deleted", "Agent xoá tệp trong thư mục chung"),
        ("filehub.conflict", "Bản xung đột trong thư mục chung"),
        ("filehub.secret_warning", "Tệp có thể chứa bí mật"),
        ("filehub.blocked", "Xoá hàng loạt đang bị giữ chờ bạn"),
        ("filehub.lane.roots", "Thư mục chung được thêm/mất quyền"),
    )),
)

EVENT_GROUPS: dict[str, dict] = {
    gid: {"id": gid, "title": title, "hint": hint, "events": tuple(e for e, _ in items)} for gid, title, hint, items in _GROUPS
}
EVENT_LABELS: dict[str, str] = {e: label for *_, items in _GROUPS for e, label in items}
GROUP_OF: dict[str, str] = {e: gid for gid, *_, items in _GROUPS for e, _ in items}
KNOWN_EVENTS: tuple[str, ...] = tuple(EVENT_LABELS)  # thứ tự chuẩn


def default_events(channel: str) -> list[str]:
    return list(DEFAULTS["notify"][channel]["events"])


# (id, nhãn, mô tả) — "custom" không có danh sách cố định
PRESETS: tuple[tuple[str, str, str], ...] = (
    ("needs_me", "Chỉ việc cần tôi", "chỉ yêu cầu duyệt và việc mới"),
    ("recommended", "Khuyên dùng", "mặc định: việc cần bạn, lỗi, ngân sách, học, bảo mật, thư mục chung"),
    ("all", "Tất cả", "mọi loại thông báo hub gửi được"),
    ("custom", "Tuỳ chỉnh…", "chọn theo nhóm hoặc từng loại"),
)
PRESET_IDS: tuple[str, ...] = tuple(p for p, _, _ in PRESETS)


def _ordered(events: Iterable[str]) -> list[str]:
    s = set(events)
    return [e for e in KNOWN_EVENTS if e in s]


def events_for(preset: str, channel: str = "telegram") -> list[str]:
    """Danh sách event của preset cho kênh. "recommended" = ĐÚNG mặc định của kênh (DEFAULTS, giữ thứ tự)."""
    if channel not in CHANNELS:
        raise ValueError(f"kênh không hợp lệ: {channel}")
    if preset == "recommended":
        return default_events(channel)
    if preset == "needs_me":
        return list(EVENT_GROUPS["needs_you"]["events"])
    if preset == "all":
        return list(KNOWN_EVENTS)
    raise ValueError(f"preset không có danh sách cố định: {preset}")


def preset_of(events: Iterable[str] | None, channel: str = "telegram") -> str:
    """Preset ứng với danh sách (so như TẬP, bỏ qua thứ tự/trùng); không khớp preset nào → "custom"."""
    s = set(events or [])
    for p in ("recommended", "needs_me", "all"):
        if s == set(events_for(p, channel)):
            return p
    return "custom"


def shared_preset(per_channel: dict[str, Iterable[str]]) -> str:
    """Preset CHUNG của nhiều kênh: preset đầu tiên khớp mọi kênh (mặc định Telegram = mọi loại, nên "Tất cả" và "Khuyên dùng" trùng
    nhau ở kênh đó — xét theo từng kênh riêng thì không suy ra được lựa chọn chung). Không có → "custom"."""
    sets = {ch: set(evs or []) for ch, evs in per_channel.items()}
    for p in ("recommended", "needs_me", "all"):
        if all(s == set(events_for(p, ch)) for ch, s in sets.items()):
            return p
    return "custom"


def unknown(events: Iterable[str]) -> list[str]:
    return [e for e in events if e not in EVENT_LABELS]


def normalize(events: Iterable[str]) -> list[str]:
    """Danh sách đã kiểm: bỏ trùng, theo thứ tự chuẩn. Event lạ → ValueError (nêu tên)."""
    evs = [str(e) for e in events]
    bad = unknown(evs)
    if bad:
        raise ValueError("loại thông báo không hợp lệ: " + ", ".join(sorted(set(bad))[:10]))
    return _ordered(evs)


def from_selection(groups: Iterable[str] = (), events: Iterable[str] = ()) -> list[str]:
    """Tuỳ chỉnh: tích cả nhóm = mọi event của nhóm; cộng các event tích lẻ. Nhóm/event lạ → ValueError."""
    out: list[str] = []
    for g in groups:
        if g not in EVENT_GROUPS:
            raise ValueError(f"nhóm thông báo không hợp lệ: {g}")
        out.extend(EVENT_GROUPS[g]["events"])
    return normalize([*out, *events])


def validate(value: object) -> None:
    """Cho `config.validate_setting("notify.<kênh>.events", …)`: danh sách chuỗi, mọi phần tử là event đã biết."""
    if not isinstance(value, list) or not all(isinstance(e, str) for e in value):
        raise ValueError("notify.<kênh>.events phải là danh sách tên sự kiện")
    normalize(value)


def grouped(selected: Iterable[str] = ()) -> list[dict]:
    """Dữ liệu cho template: mỗi nhóm {id, title, hint, entries:[{event, label, checked}], n, total, all}. Event lạ đã có trong
    cấu hình (đặt tay trước khi có danh mục) → nhóm "Khác" để người bỏ tích được."""
    sel = set(selected)
    out = []
    for gid, title, hint, items in _GROUPS:
        rows = [{"event": e, "label": label, "checked": e in sel} for e, label in items]
        n = sum(r["checked"] for r in rows)
        out.append({"id": gid, "title": title, "hint": hint, "entries": rows, "n": n, "total": len(rows), "all": n == len(rows)})
    extra = [e for e in dict.fromkeys(selected) if e not in EVENT_LABELS]
    if extra:
        rows = [{"event": e, "label": e, "checked": True} for e in extra]
        out.append({"id": "other", "title": "Khác", "hint": "đặt tay ngoài danh mục", "entries": rows, "n": len(rows), "total": len(rows), "all": True})
    return out


def label(event: str) -> str:
    return EVENT_LABELS.get(event, event)
