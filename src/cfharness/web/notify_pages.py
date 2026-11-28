"""Khối "Nhận thông báo gì" ở Cài đặt › Thông báo & kênh (plan-chat-setup-web §3.4, PR4).

Một lựa chọn preset (Chỉ việc cần tôi / Khuyên dùng / Tất cả / Tuỳ chỉnh…) áp cho CẢ HAI kênh: ghi danh sách đầy đủ vào
`notify.slack.events` và `notify.telegram.events` bằng `config.set` (audit kênh web; daemon kiểm lại event lạ ở
`config.validate_setting`). Tuỳ chỉnh riêng từng kênh nằm trong "Nâng cao" (dùng lại hàng multi của `settings_schema`, có nhãn).
Chạy được không cần JS (form thường); `static/app.js` chỉ thêm tiện ích (tích nhóm ↔ mục, tự chọn "Tuỳ chỉnh…").
"""

from __future__ import annotations

from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request

from .. import notify_events as NE
from .app import rpc

PRESET_LABEL = {p: label for p, label, _ in NE.PRESETS}


def ctx(raw_cfg: dict[str, Any], notify_group: dict[str, Any] | None) -> dict[str, Any]:
    """Dữ liệu template: preset chung, lựa chọn chung (để tích sẵn), nhóm có nhãn, hàng cấu hình theo kênh (Nâng cao)."""
    from ..config import get_path

    cur: dict[str, list[str]] = {}
    for ch in NE.CHANNELS:
        try:
            v = get_path(raw_cfg, f"notify.{ch}.events")
        except KeyError:
            v = None
        cur[ch] = [str(x) for x in (v if isinstance(v, list) else NE.default_events(ch))]
    shared = NE.shared_preset(cur)
    # cùng preset chung = giống nhau; "custom" thì so danh sách hai kênh
    same = shared != "custom" or set(cur["slack"]) == set(cur["telegram"])
    # tích sẵn cho "Tuỳ chỉnh…": giống nhau → danh sách Telegram (đủ nhất); khác → hợp của hai (lưu sẽ áp CÙNG một danh sách)
    selected = cur["telegram"] if same else list(dict.fromkeys([*cur["telegram"], *cur["slack"]]))
    rows = [r for r in (notify_group or {}).get("rows", []) if r["s"].key in ("notify.slack.events", "notify.telegram.events")]
    return {"nev": {
        "presets": NE.PRESETS, "shared": shared, "differ": not same,
        "groups": NE.grouped(selected), "counts": {ch: len(cur[ch]) for ch in NE.CHANNELS},
        "adv": {"id": "notify-events", "title": "Nâng cao — tuỳ chỉnh riêng từng kênh", "rows": rows},
    }}


def build(f: FormData) -> dict[str, Any]:
    preset = str(f.get("preset") or "")
    if preset not in NE.PRESET_IDS:
        raise ValueError("chọn một kiểu nhận thông báo")
    if preset == "custom":
        events = NE.from_selection([str(x) for x in f.getlist("grp")], [str(x) for x in f.getlist("ev")])
        values = {ch: events for ch in NE.CHANNELS}
    else:
        values = {ch: NE.events_for(preset, ch) for ch in NE.CHANNELS}
    return {"preset": preset, "values": values}


async def _apply(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for ch, events in p["values"].items():  # mỗi kênh một `config.set` có audit (daemon kiểm event lạ lần nữa)
        res = await rpc(request, "config.set", {"key": f"notify.{ch}.events", "value": events}, audit=True)
        out[ch] = len((res or {}).get(f"notify.{ch}.events") or events)
    return {"preset": p["preset"], "counts": out}


def _ok(r: dict[str, Any], f: FormData) -> str:
    r = r or {}
    counts = r.get("counts") or {}
    return (f"Đã đặt nhận thông báo «{PRESET_LABEL.get(r.get('preset'), '?')}» cho Slack và Telegram "
            f"({counts.get('slack', 0)} / {counts.get('telegram', 0)} loại)")


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """`notify_events`: một preset → `config.set notify.slack.events` + `notify.telegram.events` (audit web). Thu hẹp/mở rộng
    thông báo không nới quyền gì → không cần bước xác nhận."""
    actions["notify_events"] = Action("config.set", build, _ok, call=_apply)
