"""A deliberately small shell scanner.

It keeps the raw text of every word (quotes, globs, ``$VARS`` intact) together with
character offsets, so a rewrite can splice new text into the original command without
re-quoting anything. Anything it does not fully understand raises :class:`Bail`; the
caller then leaves the command untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["Bail", "Simple", "Tok", "scan"]


class Bail(Exception):
    """The command uses shell syntax we refuse to reason about."""


@dataclass
class Tok:
    raw: str
    start: int
    end: int
    kind: str = "word"  # "word" | "redir" | "op"
    val: str = ""  # raw with quotes/backslashes removed
    glob: bool = False  # unquoted * ? [
    expands: bool = False  # contains $ or a leading unquoted ~
    quoted: bool = False


@dataclass
class Simple:
    """One simple command: ``[NAME=val ...] word [word ...] [redirections]``."""

    start: int
    end: int
    env: list[Tok] = field(default_factory=list)
    argv: list[Tok] = field(default_factory=list)  # argv[0] is the command word
    redirs: list[tuple[Tok, Tok]] = field(default_factory=list)


_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATORS = {"&&", "||", ";", "|"}
_GLOB_CHARS = "*?["


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.toks: list[Tok] = []
        self.raw: list[str] = []
        self.val: list[str] = []
        self.wstart = 0
        self.glob = self.expands = self.quoted = False

    def _add(self, i: int, raw: str, val: str) -> None:
        if not self.raw:
            self.wstart = i
        self.raw.append(raw)
        self.val.append(val)

    def _flush(self, end: int) -> None:
        if not self.raw:
            return
        raw = "".join(self.raw)
        if raw in ("{", "}", "!", "[[", "]]", "function"):
            raise Bail(f"reserved word {raw}")
        self.toks.append(Tok(raw, self.wstart, end, "word", "".join(self.val), self.glob, self.expands, self.quoted))
        self.raw, self.val = [], []
        self.glob = self.expands = self.quoted = False

    def _operator(self, i: int) -> int:
        text, c = self.text, self.text[i]
        nxt = text[i + 1 : i + 2]
        if c == ";":
            if nxt == ";":
                raise Bail("case terminator")
            return self._emit_op(i, ";", 1)
        if c == "|":
            if nxt == "&":
                raise Bail("|&")
            return self._emit_op(i, "||" if nxt == "|" else "|", 2 if nxt == "|" else 1)
        if c == "&":
            if nxt == "&":
                return self._emit_op(i, "&&", 2)
            if nxt == ">":
                return self._redirect(i, text[i : i + 3] if text[i + 2 : i + 3] == ">" else "&>")
            raise Bail("background job")
        # < or >
        if nxt == "(":
            raise Bail("process substitution")
        if c == "<" and nxt in ("<", ">"):
            raise Bail("heredoc / here-string / read-write redirect")
        digits = ""
        if self.raw and not self.quoted and "".join(self.raw).isdigit():
            digits = "".join(self.raw)
            start = self.wstart
            self.raw, self.val = [], []
            self.glob = self.expands = False
        else:
            self._flush(i)
            start = i
        op = c
        if c == ">" and nxt in (">", "&", "|"):
            op = c + nxt
        elif c == "<" and nxt == "&":
            op = "<&"
        self.toks.append(Tok(digits + op, start, i + len(op), "redir"))
        return i + len(op)

    def _redirect(self, i: int, op: str) -> int:
        self._flush(i)
        self.toks.append(Tok(op, i, i + len(op), "redir"))
        return i + len(op)

    def _emit_op(self, i: int, op: str, width: int) -> int:
        self._flush(i)
        self.toks.append(Tok(op, i, i + width, "op"))
        return i + width

    def _double_quoted(self, i: int) -> int:
        text, n, j, out = self.text, len(self.text), i + 1, []
        while j < n:
            ch = text[j]
            if ch == '"':
                self._add(i, text[i : j + 1], "".join(out))
                self.quoted = True
                return j + 1
            if ch == "`" or text.startswith("$(", j):
                raise Bail("command substitution")
            if ch == "\\" and j + 1 < n:
                if text[j + 1] in "\n\r":
                    raise Bail("line continuation")
                if text[j + 1] in '$`"\\':
                    out.append(text[j + 1])
                else:
                    out.append(ch + text[j + 1])
                j += 2
                continue
            if ch == "$":
                self.expands = True
            out.append(ch)
            j += 1
        raise Bail("unterminated double quote")

    def _dollar(self, i: int) -> int:
        text = self.text
        nxt = text[i + 1 : i + 2]
        if nxt == "(":
            raise Bail("command substitution / arithmetic")
        self.expands = True
        if nxt == "{":
            close = text.find("}", i)
            if close < 0 or any(ch in text[i:close] for ch in "'\"`()\n"):
                raise Bail("complex parameter expansion")
            self._add(i, text[i : close + 1], text[i : close + 1])
            return close + 1
        self._add(i, "$", "$")
        return i + 1

    def run(self) -> list[Tok]:
        text, n, i = self.text, len(self.text), 0
        while i < n:
            c = text[i]
            if c in " \t":
                self._flush(i)
                i += 1
            elif c in "\n\r":
                raise Bail("newline outside quotes")
            elif c == "#" and not self.raw:
                raise Bail("comment")
            elif c == "\\":
                if i + 1 >= n or text[i + 1] in "\n\r":
                    raise Bail("line continuation")
                self._add(i, text[i : i + 2], text[i + 1])
                self.quoted = True
                i += 2
            elif c == "'":
                close = text.find("'", i + 1)
                if close < 0:
                    raise Bail("unterminated single quote")
                self._add(i, text[i : close + 1], text[i + 1 : close])
                self.quoted = True
                i = close + 1
            elif c == '"':
                i = self._double_quoted(i)
            elif c == "`":
                raise Bail("backtick")
            elif c == "$":
                i = self._dollar(i)
            elif c in "()":
                raise Bail("subshell / grouping")
            elif c in ";|&<>":
                i = self._operator(i)
            else:
                if c in _GLOB_CHARS:
                    self.glob = True
                if c == "~" and not self.raw:
                    self.expands = True
                self._add(i, c, c)
                i += 1
        self._flush(n)
        return self.toks


def scan(command: str) -> list[Simple]:
    """Split *command* into simple commands. Raises :class:`Bail` on anything unclear."""
    toks = _Scanner(command).run()
    segments: list[Simple] = []
    current: list[Tok] = []

    def close(sep: Tok | None) -> None:
        if not current:
            if sep is not None:
                raise Bail("empty command")
            return
        segments.append(_simple(current))
        current.clear()

    for tok in toks:
        if tok.kind == "op" and tok.raw in _SEPARATORS:
            close(tok)
        else:
            current.append(tok)
    if current:
        close(None)
    elif toks and toks[-1].raw in ("&&", "||", "|"):
        raise Bail("dangling operator")
    return segments


def _simple(toks: list[Tok]) -> Simple:
    seg = Simple(start=toks[0].start, end=toks[-1].end)
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.kind == "redir":
            if i + 1 >= len(toks) or toks[i + 1].kind != "word":
                raise Bail("redirection without target")
            seg.redirs.append((tok, toks[i + 1]))
            i += 2
            continue
        if not seg.argv and _ASSIGN.match(tok.raw):
            seg.env.append(tok)
        else:
            seg.argv.append(tok)
        i += 1
    return seg
