"""Thu gọn JS của island Điều phối khi phục vụ (không bước build): bỏ chú thích, thụt lề, dòng trống, khoảng trắng thừa quanh dấu câu.

Chỉ dành cho mã CỦA MÌNH ở `static/coord/*.js` (ES module, phong cách đã biết) — không phải minifier tổng quát:
- chuỗi `'…'`/`"…"` và template literal (kể cả `${…}` lồng nhau, template `htm`) giữ NGUYÊN từng ký tự (khoảng trắng trong template `htm`
  có nghĩa);
- regex literal nhận theo token đứng trước (sau toán tử / dấu mở / từ khoá như `return` → regex; sau định danh / số / `)` / `]` → phép chia);
- GIỮ xuống dòng (an toàn với ASI), chỉ gộp nhiều dòng trống thành một;
- không đổi tên, không viết lại biểu thức → ngữ nghĩa giữ nguyên (test chạy lại store/SSE trên bản đã thu gọn + `node --check`).
"""

from __future__ import annotations

_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")
_REGEX_AFTER_WORDS = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw", "instanceof", "yield", "await"}
_REGEX_AFTER_CHARS = set("(,=:[!&|?{};+-*%<>~^")


def _idch(c: str) -> bool:
    return c in _ID or ord(c) > 127


def minify(src: str) -> str:
    out: list[str] = []  # mảnh đầu ra
    i, n = 0, len(src)
    stack: list[int] = []  # mỗi `${` mở trong template: độ sâu ngoặc nhọn tại đó
    depth = 0
    last = ""  # ký tự có nghĩa cuối cùng (không phải khoảng trắng) đã ghi
    word = ""  # định danh/từ khoá cuối cùng đã ghi (để nhận regex sau `return` …)
    pend_space = False
    pend_nl = False

    def emit(s: str, *, tok_last: str | None = None) -> None:
        nonlocal pend_space, pend_nl, last
        if pend_nl and out:
            out.append("\n")
        elif pend_space and out:
            a, b = last, s[0]
            # khoảng trắng chỉ cần giữa hai ký tự định danh, hoặc `+ +` / `- -` (tránh thành `++` / `--`)
            if (_idch(a) and _idch(b)) or (a in "+-" and b == a):
                out.append(" ")
        pend_space = pend_nl = False
        out.append(s)
        last = tok_last if tok_last is not None else s[-1]

    while i < n:
        c = src[i]
        if c in " \t\r":
            pend_space = True
            i += 1
            continue
        if c == "\n":
            pend_nl = True
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            if end < 0:
                raise ValueError("chú thích khối không đóng")
            if "\n" in src[i:end]:
                pend_nl = True
            else:
                pend_space = True
            i = end + 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n":
                    raise ValueError("chuỗi không đóng")
                j += 1
            emit(src[i:j + 1])
            word = ""
            i = j + 1
            continue
        if c == "`":
            # ghi cả dấu ` mở cùng phần chữ tới ` đóng / `${`
            j = i + 1
            start = i
            while j < n:
                d = src[j]
                if d == "\\":
                    j += 2
                    continue
                if d == "`":
                    emit(src[start:j + 1])
                    j += 1
                    break
                if d == "$" and j + 1 < n and src[j + 1] == "{":
                    emit(src[start:j + 2])
                    stack.append(depth)
                    depth = 0
                    j += 2
                    break
                j += 1
            else:
                raise ValueError("template literal không đóng")
            word = ""
            i = j
            continue
        if c == "}" and stack and depth == 0:
            # đóng `${…}` → quay lại phần chữ của template
            depth = stack.pop()
            j = i + 1
            start = i
            while j < n:
                d = src[j]
                if d == "\\":
                    j += 2
                    continue
                if d == "`":
                    emit(src[start:j + 1])
                    j += 1
                    break
                if d == "$" and j + 1 < n and src[j + 1] == "{":
                    emit(src[start:j + 2])
                    stack.append(depth)
                    depth = 0
                    j += 2
                    break
                j += 1
            else:
                raise ValueError("template literal không đóng")
            word = ""
            i = j
            continue
        if c == "/" and (last == "" or last in _REGEX_AFTER_CHARS or word in _REGEX_AFTER_WORDS):
            j, cls = i + 1, False
            while j < n:
                d = src[j]
                if d == "\\":
                    j += 2
                    continue
                if d == "\n":
                    raise ValueError("regex không đóng")
                if d == "[":
                    cls = True
                elif d == "]":
                    cls = False
                elif d == "/" and not cls:
                    break
                j += 1
            j += 1
            while j < n and _idch(src[j]):  # cờ
                j += 1
            emit(src[i:j])
            word = ""
            i = j
            continue
        if _idch(c):
            j = i
            while j < n and _idch(src[j]):
                j += 1
            # số có dấu chấm / số mũ: `1.5`, `1e-3` — giữ nguyên cụm
            if c.isdigit():
                while j < n and (_idch(src[j]) or src[j] == "." or (src[j] in "+-" and src[j - 1] in "eE")):
                    j += 1
            word = src[i:j]
            emit(word)
            i = j
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        word = ""
        emit(c)
        i += 1
    return "".join(out) + "\n"


__all__ = ["minify"]
