"""Biểu thức lịch — hàm THUẦN, chỉ stdlib (không thêm dependency).

Dạng nhận (không phân biệt hoa thường, khoảng trắng thừa bỏ qua):
- cron 5 trường `phút giờ ngày tháng thứ`: `*`, `*/15`, `1-5`, `1-10/2`, danh sách `1,15,30`, tên tháng `jan..dec`, tên thứ
  `sun..sat` (0 và 7 = Chủ nhật). Ngày-trong-tháng và thứ cùng bị giới hạn → khớp MỘT trong hai (như cron chuẩn).
- `@hourly @daily @weekly @monthly @yearly`.
- `every 30m` / `every 2h` / `every 1d` (khoảng cố định, mốc = lúc tạo) — tối thiểu 1 phút.
- `hourly` (phút 0), `hourly :15`.
- `daily 08:00`, `weekdays 08:30` (T2–T6), `weekends 09:00`, `weekly mon 09:00` (`weekly mon,thu 09:00`), `monthly 1 09:00`.
- `at 2026-10-20 09:00` (hoặc `2026-10-20T09:00`), `at 09:00` (lần tới của giờ đó), `in 30m` — MỘT lần; quy về mốc tuyệt đối lúc tạo
  (`resolve_once`).
- Tiếng Việt tiện dụng: `mỗi 15 phút|giờ|ngày`, `hằng ngày 08:00` / `mỗi ngày 08:00`, `ngày thường 08:30`, `cuối tuần 09:00`.

Múi giờ: giờ máy (tên IANA đọc từ `TZ` hoặc `/etc/localtime`), tính trên giờ TƯỜNG của múi đó rồi đổi ra UTC. Giờ không tồn tại /
lặp lại do DST không làm lỗi: lấy offset `fold=0` của zoneinfo, kết quả luôn tăng dần (bỏ mốc ≤ `after`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Iterator

MONTHS = {n: i for i, n in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DOWS = {n: i for i, n in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *",
           "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
_UNIT = {"s": 1, "sec": 1, "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "phút": 60, "phut": 60,
         "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600, "giờ": 3600, "gio": 3600, "tiếng": 3600,
         "d": 86400, "day": 86400, "days": 86400, "ngày": 86400, "ngay": 86400, "w": 604800, "week": 604800, "weeks": 604800, "tuần": 604800}
_HM = re.compile(r"^(\d{1,2})(?::|h)(\d{2})?$")
_DUR = re.compile(r"^(\d+)\s*([a-zà-ỹđ]+)$", re.I)
MAX_YEARS = 5  # tìm mốc kế tiếp tối đa 5 năm (vd 29/2 + thứ cụ thể) — quá → None


class CronError(ValueError):
    """Biểu thức lịch không hợp lệ (thông điệp tiếng Việt cho người/điều phối viên)."""


@dataclass(frozen=True)
class Spec:
    kind: str  # cron | every | once
    expr: str  # dạng chuẩn hoá (lưu DB)
    minutes: frozenset[int] = field(default_factory=frozenset)
    hours: frozenset[int] = field(default_factory=frozenset)
    doms: frozenset[int] = field(default_factory=frozenset)
    months: frozenset[int] = field(default_factory=frozenset)
    dows: frozenset[int] = field(default_factory=frozenset)
    dom_star: bool = True
    dow_star: bool = True
    interval_s: int = 0
    at: datetime | None = None  # once: mốc tuyệt đối (aware)


# ============================================================================ múi giờ
def local_tz_name() -> str | None:
    """Tên IANA của giờ máy (TZ hoặc symlink /etc/localtime) — None nếu không biết (dùng offset hiện tại)."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:  # pragma: no cover
        return None
    m = re.search(r"zoneinfo/(.+)$", target)
    return m.group(1) if m else None


def get_tz(name: str | None) -> tzinfo:
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # tên lạ / không có tzdata → giờ máy
            pass
    n = local_tz_name()
    if n and n != name:
        return get_tz(n)
    return datetime.now().astimezone().tzinfo or timezone.utc


# ============================================================================ parse
def _num(tok: str, names: dict[str, int], lo: int, hi: int, what: str) -> int:
    t = tok.lower()
    if t.isalpha() and t[:3] in names:
        return names[t[:3]]
    if not t.isdigit():
        raise CronError(f"{what}: '{tok}' không phải số")
    v = int(t)
    if not lo <= v <= hi:
        raise CronError(f"{what}: {v} ngoài khoảng {lo}-{hi}")
    return v


def _field(src: str, lo: int, hi: int, names: dict[str, int], what: str) -> tuple[frozenset[int], bool]:
    """Một trường cron → (tập giá trị, "sao"). Thứ: 0..7 (7 = CN) → quy về 0..6. "Sao" theo vixie cron: trường bắt đầu bằng `*`."""
    is_dow = what == "thứ"
    top = 6 if is_dow else hi
    out: set[int] = set()
    for part in src.split(","):
        if not part:
            raise CronError(f"{what}: danh sách rỗng")
        rng, slash, step_s = part.partition("/")
        step = 1
        if slash:
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"{what}: bước '/{step_s}' không hợp lệ")
            step = int(step_s)
        if rng == "*":
            a, b = lo, top
        elif "-" in rng:
            x, _, y = rng.partition("-")
            a, b = _num(x, names, lo, hi, what), _num(y, names, lo, hi, what)
            if a > b:
                raise CronError(f"{what}: khoảng {rng} ngược")
        else:
            a = _num(rng, names, lo, hi, what)
            b = top if slash else a
        out.update(range(a, b + 1, step))
    return frozenset(v % 7 if is_dow else v for v in out), src.startswith("*")


def parse_cron(expr: str) -> Spec:
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(f"cron cần 5 trường (phút giờ ngày tháng thứ), nhận {len(parts)}: '{expr}'")
    mi, _ = _field(parts[0], 0, 59, {}, "phút")
    hr, _ = _field(parts[1], 0, 23, {}, "giờ")
    dm, dm_star = _field(parts[2], 1, 31, {}, "ngày")
    mo, _ = _field(parts[3].lower(), 1, 12, MONTHS, "tháng")
    dw, dw_star = _field(parts[4].lower(), 0, 7, DOWS, "thứ")
    return Spec("cron", " ".join(parts).lower(), mi, hr, dm, mo, dw, dm_star, dw_star)


def _hm(tok: str) -> tuple[int, int]:
    m = _HM.match(tok.strip())
    if not m:
        raise CronError(f"giờ '{tok}' phải dạng HH:MM (vd 08:30)")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        raise CronError(f"giờ '{tok}' không hợp lệ")
    return h, mi


def _duration(text: str) -> int:
    m = _DUR.match(text.strip().replace(" ", ""))
    if not m:
        raise CronError(f"khoảng '{text}' phải dạng <số><đơn vị> (30m, 2h, 1d)")
    unit = m.group(2).lower()
    if unit not in _UNIT:
        raise CronError(f"đơn vị '{unit}' không rõ (m, h, d, w)")
    return int(m.group(1)) * _UNIT[unit]


def _dows(tok: str) -> str:
    out = []
    for t in tok.split(","):
        t = t.strip().lower()
        if t[:3] not in DOWS:
            raise CronError(f"thứ '{t}' không rõ (mon..sun)")
        out.append(str(DOWS[t[:3]]))
    return ",".join(out)


_VI = ((r"^(hằng|hang|mỗi|moi)\s+(ngày|ngay)\s+(?=\d)", "daily "), (r"^(mỗi|moi)\s+", "every "), (r"^ngày\s+thường\s+", "weekdays "),
       (r"^cuối\s+tuần\s+", "weekends "), (r"^(lúc|luc|vào|vao)\s+", "at "), (r"^(sau|trong)\s+", "in "))


def parse(expr: str, *, now: datetime | None = None, tz: tzinfo | None = None) -> Spec:
    """Biểu thức → Spec. `at`/`in` được quy về mốc tuyệt đối theo `now` (mặc định giờ hiện tại)."""
    raw = " ".join(str(expr or "").split())
    if not raw:
        raise CronError("thiếu biểu thức lịch")
    low = raw.lower()
    for pat, rep in _VI:
        low = re.sub(pat, rep, low)
    if low in ALIASES:
        return parse_cron(ALIASES[low])
    head, _, rest = low.partition(" ")
    if head == "every":
        if rest in ("day", "ngày"):
            rest = "1d"
        if rest in ("hour", "giờ"):
            rest = "1h"
        n = _duration(rest)
        if n < 60:
            raise CronError("khoảng tối thiểu 1 phút")
        if n % 86400 == 0:
            norm = f"{n // 86400}d"
        elif n % 3600 == 0:
            norm = f"{n // 3600}h"
        elif n % 60 == 0:
            norm = f"{n // 60}m"
        else:
            norm = f"{n}s"
        return Spec("every", f"every {norm}", interval_s=n)
    if head == "hourly":
        mi = 0
        if rest:
            if not re.fullmatch(r":?\d{1,2}", rest) or int(rest.lstrip(":")) > 59:
                raise CronError("hourly :MM (phút 0-59)")
            mi = int(rest.lstrip(":"))
        return parse_cron(f"{mi} * * * *")
    if head in ("daily", "weekdays", "weekends"):
        h, mi = _hm(rest)
        dow = {"daily": "*", "weekdays": "1-5", "weekends": "0,6"}[head]
        return parse_cron(f"{mi} {h} * * {dow}")
    if head == "weekly":
        d, _, t = rest.partition(" ")
        h, mi = _hm(t)
        return parse_cron(f"{mi} {h} * * {_dows(d)}")
    if head == "monthly":
        d, _, t = rest.partition(" ")
        if not d.isdigit() or not 1 <= int(d) <= 31:
            raise CronError("monthly <ngày 1-31> HH:MM")
        h, mi = _hm(t)
        return parse_cron(f"{mi} {h} {int(d)} * *")
    if head in ("at", "in"):
        return resolve_once(low, now=now, tz=tz)
    if head.startswith("@"):
        raise CronError(f"alias '{head}' không rõ ({', '.join(ALIASES)})")
    return parse_cron(low)


def resolve_once(text: str, *, now: datetime | None = None, tz: tzinfo | None = None) -> Spec:
    tz = tz or get_tz(None)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    head, _, rest = text.partition(" ")
    rest = rest.strip()
    if head == "in":
        at = now + timedelta(seconds=_duration(rest))
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}[ t]\d{1,2}:\d{2}(:\d{2})?([+-]\d{2}:?\d{2}|z)?", rest):
        try:
            dt = datetime.fromisoformat(rest.upper().replace(" ", "T").replace("Z", "+00:00"))
        except ValueError as e:
            raise CronError(f"mốc '{rest}' không hợp lệ: {e}") from None
        at = dt if dt.tzinfo else _wall(dt.date(), dt.time(), tz)
    elif _HM.match(rest):
        h, mi = _hm(rest)
        at = _wall(now.date(), time(h, mi), tz)
        if at <= now:
            at = _wall(now.date() + timedelta(days=1), time(h, mi), tz)
    else:
        raise CronError(f"'at' cần 'YYYY-MM-DD HH:MM' hoặc 'HH:MM', nhận '{rest}'")
    at = at.astimezone(timezone.utc).replace(microsecond=0)
    return Spec("once", "at " + at.isoformat().replace("+00:00", "Z"), at=at)


# ============================================================================ tính mốc
def _wall(d: date, t: time, tz: tzinfo) -> datetime:
    return datetime.combine(d, t).replace(tzinfo=tz)


def _day_ok(s: Spec, d: date) -> bool:
    if d.month not in s.months:
        return False
    dom_ok = d.day in s.doms
    dow_ok = (d.isoweekday() % 7) in s.dows
    if s.dom_star and s.dow_star:
        return True
    if s.dom_star:
        return dow_ok
    if s.dow_star:
        return dom_ok
    return dom_ok or dow_ok


def next_after(s: Spec, after: datetime, *, tz: tzinfo | None = None, anchor: datetime | None = None) -> datetime | None:
    """Mốc chạy KẾ TIẾP (UTC, aware) sau `after` (không tính `after`). once: mốc `at` nếu còn tương lai."""
    after = after.astimezone(timezone.utc)
    if s.kind == "once":
        return s.at if s.at is not None and s.at > after else None
    if s.kind == "every":
        base = (anchor or after).astimezone(timezone.utc)
        if base > after:
            return base
        n = int((after - base).total_seconds() // s.interval_s) + 1
        return base + timedelta(seconds=n * s.interval_s)
    tz = tz or get_tz(None)
    loc = after.astimezone(tz)
    d = loc.date()
    hours, minutes = sorted(s.hours), sorted(s.minutes)
    end = d + timedelta(days=366 * MAX_YEARS)
    while d <= end:
        if _day_ok(s, d):
            for h in hours:
                for m in minutes:
                    cand = _wall(d, time(h, m), tz)
                    if cand.astimezone(timezone.utc) > after:
                        return cand.astimezone(timezone.utc)
        d += timedelta(days=1)
    return None


def occurrences(s: Spec, after: datetime, *, until: datetime | None = None, limit: int = 1000, tz: tzinfo | None = None,
                anchor: datetime | None = None) -> Iterator[datetime]:
    cur = after
    for _ in range(limit):
        nxt = next_after(s, cur, tz=tz, anchor=anchor)
        if nxt is None or (until is not None and nxt > until):
            return
        yield nxt
        cur = nxt


def min_interval_s(s: Spec, *, now: datetime, tz: tzinfo | None = None) -> float | None:
    """Khoảng NGẮN NHẤT giữa hai lần chạy (xét ~200 lần tới). None = chạy một lần."""
    if s.kind == "once":
        return None
    if s.kind == "every":
        return float(s.interval_s)
    pts = list(occurrences(s, now, limit=200, tz=tz))
    if len(pts) < 2:
        return None
    return min((b - a).total_seconds() for a, b in zip(pts, pts[1:]))


def runs_per_day(s: Spec, *, now: datetime, tz: tzinfo | None = None) -> float:
    """Ước tính số lần chạy mỗi ngày (trung bình 7 ngày tới; lấy ngày NHIỀU nhất để ước tính không thấp hơn thật)."""
    if s.kind == "once":
        return 1.0
    if s.kind == "every":
        return 86400.0 / s.interval_s
    tz = tz or get_tz(None)
    per_day: dict[date, int] = {}
    for t in occurrences(s, now, until=now + timedelta(days=7), limit=7 * 1440 + 1, tz=tz):
        k = t.astimezone(tz).date()
        per_day[k] = per_day.get(k, 0) + 1
    return float(max(per_day.values())) if per_day else 0.0


def describe(s: Spec) -> str:
    """Mô tả ngắn tiếng Việt (cho CLI/Telegram)."""
    if s.kind == "once":
        return "một lần lúc " + (s.at.astimezone(get_tz(None)).strftime("%Y-%m-%d %H:%M") if s.at else "?")
    if s.kind == "every":
        n = s.interval_s
        return "mỗi " + (f"{n // 86400} ngày" if n % 86400 == 0 else f"{n // 3600} giờ" if n % 3600 == 0 else f"{n // 60} phút")
    p = s.expr.split()
    if p[2:4] == ["*", "*"] and len(s.hours) == 1 and len(s.minutes) == 1:
        hm = f"{next(iter(s.hours)):02d}:{next(iter(s.minutes)):02d}"
        if p[4] == "*":
            return f"hằng ngày {hm}"
        if p[4] == "1-5":
            return f"thứ 2–6 lúc {hm}"
        if p[4] == "0,6":
            return f"cuối tuần lúc {hm}"
    return f"cron `{s.expr}`"
