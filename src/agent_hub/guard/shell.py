"""Lexer lệnh shell cho bộ phân loại (FR-SG-03 bước 1 — "chuẩn hoá lệnh").

Không cố làm bash đầy đủ; mục tiêu là KHÔNG bỏ sót thao tác phá huỷ:
- chuẩn hoá Unicode (NFKC + bảng chữ đồng hình Cyrillic/Hy Lạp), bỏ ký tự vô hình, `${IFS}` → khoảng trắng;
- bỏ quote (`'..'`, `".."`, `\\x`), giải ANSI-C `$'\\x72\\x6d'`, `~`, `$HOME`, biến đã gán trong cùng lệnh;
- tách lệnh theo `;` `&&` `||` `|` `&` xuống dòng `(` `)` `{` `}`; ghi lại pipeline (lệnh trước đưa stdin);
- thu nội dung `$(..)`, `` `..` ``, `<(..)` để phân tích đệ quy; heredoc/here-string thành stdin của lệnh.
Phần không giải được (biến lạ, thay thế lệnh) được đánh dấu `dynamic` → bộ phân loại coi là "không chắc".
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

# Chữ đồng hình hay dùng để lách bộ lọc (Cyrillic/Hy Lạp/Latin mở rộng) → ASCII
_HOMOGLYPHS = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "һ": "h", "ӏ": "l", "ɡ": "g", "ո": "n", "ս": "u", "ᴍ": "m", "ⅿ": "m", "ꜰ": "f",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    "м": "m", "т": "t", "к": "k", "п": "n", "г": "r", "ԛ": "q", "ԝ": "w", "ѵ": "v", "ү": "y", "ь": "b", "в": "b", "з": "3",
    "ο": "o", "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "﹣": "-", "－": "-",
}
_INVISIBLE = "​‌‍⁠﻿­᠎"
_TRANS = str.maketrans({**_HOMOGLYPHS, **{c: None for c in _INVISIBLE}})

DYN = "\x00DYN\x00"  # đánh dấu phần không giải được trong một từ

OPS = (";;", "&&", "||", ";", "|&", "|", "&", "\n", "(", ")")
_DECL = frozenset({"export", "declare", "local", "readonly", "typeset", "read"})
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?=)(.*)$", re.S)


def normalize(cmd: str) -> str:
    s = unicodedata.normalize("NFKC", cmd).translate(_TRANS)
    s = re.sub(r"\$\{IFS(?:[^}]*)\}|\$IFS\b", " ", s)
    s = s.replace("\\\r\n", "").replace("\\\n", "")  # nối dòng
    return s


@dataclass
class Word:
    text: str
    dynamic: bool = False  # có phần không giải được ($VAR lạ, $(..))
    glob: bool = False  # có ký tự glob KHÔNG nằm trong quote

    @property
    def literal(self) -> str:
        return self.text.replace(DYN, "")


@dataclass
class SimpleCommand:
    words: list[Word] = field(default_factory=list)
    redirects: list[tuple[str, Word]] = field(default_factory=list)  # (toán tử, đích)
    stdin_text: str | None = None  # heredoc / here-string
    stdin_dynamic: bool = False
    pipe_from: "SimpleCommand | None" = None  # lệnh trước trong pipeline
    background: bool = False


@dataclass
class ParseResult:
    commands: list[SimpleCommand]
    substitutions: list[str]
    error: str | None = None


class ParseError(Exception):
    pass


def _match_paren(s: str, i: int) -> int:
    """s[i] ngay sau '(' mở; trả chỉ số của ')' đóng tương ứng (tôn trọng quote)."""
    depth = 1
    q: str | None = None
    while i < len(s):
        c = s[i]
        if q:
            if c == "\\" and q == '"':
                i += 2
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == "\\":
            i += 2
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ParseError("thiếu ')'")


def _ansi_c(body: str) -> str:
    out, i = [], 0
    simple = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 >= len(body):
            out.append(c)
            i += 1
            continue
        n = body[i + 1]
        if n in simple:
            out.append(simple[n])
            i += 2
        elif n == "x":
            m = re.match(r"[0-9a-fA-F]{1,2}", body[i + 2 :])
            if m:
                out.append(chr(int(m.group(0), 16)))
                i += 2 + len(m.group(0))
            else:
                out.append("\\x")
                i += 2
        elif n in "uU":
            m = re.match(r"[0-9a-fA-F]{1,8}", body[i + 2 :])
            if m:
                out.append(chr(int(m.group(0), 16)))
                i += 2 + len(m.group(0))
            else:
                out.append("\\" + n)
                i += 2
        elif n in "01234567":
            m = re.match(r"[0-7]{1,3}", body[i + 1 :])
            assert m
            out.append(chr(int(m.group(0), 8)))
            i += 1 + len(m.group(0))
        else:
            out.append("\\" + n)
            i += 2
    return "".join(out)


def decode_printf(fmt: str) -> str:
    """printf/echo -e: giải \\xHH, \\NNN, \\n… (đủ cho việc phát hiện lệnh ẩn)."""
    return _ansi_c(fmt.replace("%s", "").replace("%b", ""))


class Lexer:
    def __init__(self, src: str, env: dict[str, str | None] | None = None, home: str | None = None) -> None:
        self.s = normalize(src)
        self.i = 0
        self.env: dict[str, str | None] = dict(env or {})  # None = biến động (không biết giá trị)
        self.home = home or os.path.expanduser("~")
        self.env.setdefault("HOME", self.home)
        self.subs: list[str] = []
        self.pending_heredocs: list[tuple[SimpleCommand, str, bool]] = []

    # ------------------------------------------------------------- expansions
    def _var(self, name: str) -> tuple[str, bool]:
        if name in self.env:
            v = self.env[name]
            return (DYN, True) if v is None else (v, False)
        if name in ("PWD",):
            return (DYN, True)
        return (DYN, True)

    def _dollar(self, buf: list[str], flags: dict[str, bool]) -> None:
        s, i = self.s, self.i  # s[i] == '$'
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if nxt == "(":
            if s[i + 2 : i + 3] == "(":  # $(( arith ))
                j = _match_paren(s, i + 2)
                self.i = j + 2 if j + 1 < len(s) and s[j + 1] == ")" else j + 1
                buf.append(DYN)
                flags["dynamic"] = True
                return
            j = _match_paren(s, i + 2)
            self.subs.append(s[i + 2 : j])
            buf.append(DYN)
            flags["dynamic"] = True
            self.i = j + 1
            return
        if nxt == "{":
            j = s.find("}", i + 2)
            if j < 0:
                raise ParseError("thiếu '}'")
            inner = s[i + 2 : j]
            self.i = j + 1
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(?::?[-=](.*))?$", inner, re.S)
            if m:
                val, dyn = self._var(m.group(1))
                if dyn and m.group(2) is not None and m.group(1) not in self.env:
                    buf.append(m.group(2))  # ${X:-default} với X chưa gán → default
                    return
                buf.append(val)
                flags["dynamic"] |= dyn
                return
            buf.append(DYN)
            flags["dynamic"] = True
            return
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", s[i + 1 :])
        if m:
            val, dyn = self._var(m.group(0))
            buf.append(val)
            flags["dynamic"] |= dyn
            self.i = i + 1 + len(m.group(0))
            return
        if nxt and nxt in "0123456789@*#?$!-":
            buf.append(DYN)
            flags["dynamic"] = True
            self.i = i + 2
            return
        buf.append("$")
        self.i = i + 1

    # ------------------------------------------------------------- words
    def _word(self) -> Word:
        s = self.s
        buf: list[str] = []
        flags = {"dynamic": False, "glob": False}
        start = self.i
        while self.i < len(s):
            c = s[self.i]
            if c in " \t\r":
                break
            if c in ";&|\n()<>":
                if c in "<>" and s[self.i + 1 : self.i + 2] == "(":  # process substitution <( ) >( )
                    j = _match_paren(s, self.i + 2)
                    self.subs.append(s[self.i + 2 : j])
                    buf.append(DYN)
                    flags["dynamic"] = True
                    self.i = j + 1
                    continue
                break
            if c == "\\":
                if self.i + 1 < len(s):
                    buf.append(s[self.i + 1])
                self.i += 2
                continue
            if c == "'":
                j = s.find("'", self.i + 1)
                if j < 0:
                    raise ParseError("thiếu dấu ' đóng")
                buf.append(s[self.i + 1 : j])
                self.i = j + 1
                continue
            if c == "$" and s[self.i + 1 : self.i + 2] == "'":
                j = self.i + 2
                while j < len(s) and s[j] != "'":
                    j += 2 if s[j] == "\\" else 1
                if j >= len(s):
                    raise ParseError("thiếu dấu ' đóng")
                buf.append(_ansi_c(s[self.i + 2 : j]))
                self.i = j + 1
                continue
            if c == '"' or (c == "$" and s[self.i + 1 : self.i + 2] == '"'):
                self.i += 1 if c == '"' else 2
                while True:
                    if self.i >= len(s):
                        raise ParseError('thiếu dấu " đóng')
                    d = s[self.i]
                    if d == '"':
                        self.i += 1
                        break
                    if d == "\\" and self.i + 1 < len(s) and s[self.i + 1] in '$`"\\\n':
                        buf.append(s[self.i + 1])
                        self.i += 2
                        continue
                    if d == "$":
                        self._dollar(buf, flags)
                        continue
                    if d == "`":
                        j = s.find("`", self.i + 1)
                        if j < 0:
                            raise ParseError("thiếu ` đóng")
                        self.subs.append(s[self.i + 1 : j])
                        buf.append(DYN)
                        flags["dynamic"] = True
                        self.i = j + 1
                        continue
                    buf.append(d)
                    self.i += 1
                continue
            if c == "$":
                self._dollar(buf, flags)
                continue
            if c == "`":
                j = s.find("`", self.i + 1)
                if j < 0:
                    raise ParseError("thiếu ` đóng")
                self.subs.append(s[self.i + 1 : j])
                buf.append(DYN)
                flags["dynamic"] = True
                self.i = j + 1
                continue
            if c == "~" and self.i == start:
                nx = s[self.i + 1 : self.i + 2]
                if nx in ("", "/", " ", "\t", ";", "\n", "|", "&", ")"):
                    buf.append(self.home)
                    self.i += 1
                    continue
                m = re.match(r"~([A-Za-z0-9_.-]+)", s[self.i :])
                if m:
                    buf.append(os.path.join(os.path.dirname(self.home), m.group(1)))
                    self.i += len(m.group(0))
                    continue
            if c in "*?[":
                flags["glob"] = True
            buf.append(c)
            self.i += 1
        return Word("".join(buf), flags["dynamic"], flags["glob"])

    def _skip_ws(self) -> None:
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c in " \t\r":
                self.i += 1
            elif c == "#" and (self.i == 0 or s[self.i - 1] in " \t\n;&|()"):
                while self.i < len(s) and s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def _read_heredocs(self) -> None:
        """Sau một dòng mới: đọc thân các heredoc đang chờ."""
        s = self.s
        for cmd, delim, strip_tabs in self.pending_heredocs:
            lines = []
            while self.i < len(s):
                j = s.find("\n", self.i)
                line = s[self.i :] if j < 0 else s[self.i : j]
                self.i = len(s) if j < 0 else j + 1
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    break
                lines.append(line)
            body = "\n".join(lines)
            cmd.stdin_text = (cmd.stdin_text + "\n" if cmd.stdin_text else "") + body
        self.pending_heredocs = []

    # ------------------------------------------------------------- commands
    def parse(self) -> ParseResult:
        cmds: list[SimpleCommand] = []
        cur = SimpleCommand()
        prev_pipe: SimpleCommand | None = None
        at_start = True
        s = self.s

        def finish(op: str | None) -> None:
            nonlocal cur, prev_pipe, at_start
            if cur.words or cur.redirects:
                cur.pipe_from = prev_pipe
                if op == "&":
                    cur.background = True
                cmds.append(cur)
                prev_pipe = cur if op in ("|", "|&") else None
            elif op not in ("|", "|&"):
                prev_pipe = None
            cur = SimpleCommand()
            at_start = True

        while True:
            self._skip_ws()
            if self.i >= len(s):
                break
            c = s[self.i]
            # toán tử điều khiển
            op = next((o for o in OPS if s.startswith(o, self.i)), None)
            if op is not None:
                self.i += len(op)
                finish(op)
                if op == "\n" and self.pending_heredocs:
                    self._read_heredocs()
                continue
            # chuyển hướng: [n]> >> < << <<< &> >& >|
            m = re.match(r"(\d*)(&>>|&>|>>|>\||>&|<<<|<<-|<<|<>|<&|>|<)", s[self.i :])
            if m and not (m.group(2) in ("<", ">") and s[self.i + len(m.group(0)) : self.i + len(m.group(0)) + 1] == "("):
                rop = m.group(2)
                self.i += len(m.group(0))
                self._skip_ws()
                if rop in ("<<", "<<-"):
                    dm = re.match(r"""(['"]?)([^\s'";&|<>]+)\1""", s[self.i :])
                    if not dm:
                        raise ParseError("heredoc thiếu delimiter")
                    self.i += len(dm.group(0))
                    self.pending_heredocs.append((cur, dm.group(2), rop == "<<-"))
                    continue
                w = self._word()
                if rop == "<<<":
                    cur.stdin_text = w.literal
                    cur.stdin_dynamic = w.dynamic
                elif rop in (">&", "<&") and re.fullmatch(r"\d+|-", w.text):
                    pass  # 2>&1
                else:
                    cur.redirects.append((rop, w))
                continue
            w = self._word()
            if not w.text and not w.dynamic:
                if self.i < len(s) and s[self.i] not in " \t\r":
                    self.i += 1  # ký tự lạ — bỏ qua để không lặp vô hạn
                continue
            if w.text in ("{", "}") and not w.dynamic:
                finish(";")
                continue
            if at_start:
                am = _ASSIGN_RE.match(w.text) if not w.glob else None
                if am and not cur.words:
                    self.env[am.group(1)] = None if w.dynamic else am.group(3)
                    continue
                at_start = False
            elif cur.words and cur.words[0].text in _DECL and not w.glob:
                am = _ASSIGN_RE.match(w.text)
                if am:
                    self.env[am.group(1)] = None if w.dynamic else am.group(3)
                elif cur.words[0].text == "read" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", w.text):
                    self.env[w.text] = None
            cur.words.append(w)
        finish(None)
        if self.pending_heredocs:
            self._read_heredocs()
        return ParseResult(cmds, self.subs)


def parse(src: str, env: dict[str, str | None] | None = None, home: str | None = None) -> ParseResult:
    lx = Lexer(src, env, home)
    try:
        return lx.parse()
    except ParseError as e:
        return ParseResult([], lx.subs, str(e))
