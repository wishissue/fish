#!/usr/bin/env python3
"""Fish, stage 1 prototype: lexer, parser, interpreter.

Purpose: let us run real Fish programs and test the syntax before writing the
native compiler. Usage:
    python3 fish.py run program.fish
    python3 fish.py check program.fish
"""
import sys
import difflib

sys.setrecursionlimit(20000)
VERSION = "0.1.0-prototype"

# ---------------------------------------------------------------- errors


class FishError(Exception):
    def __init__(self, msg, line=None, help=None):
        super().__init__(msg)
        self.msg, self.line, self.help = msg, line, help


# ---------------------------------------------------------------- lexer

KEYWORDS = {
    "so", "var", "keep", "fun", "give", "gives", "if", "else", "stop", "match",
    "when", "for", "in", "while", "loop", "break", "continue", "and", "or",
    "not", "is", "to", "until", "as", "of", "true", "false", "none", "mod",
    "say", "record", "choice", "extend", "ref", "mut",
}

# Conventional words Fish deliberately does not use. Reserving them gives
# beginners a helpful message instead of a confusing one.
HINTS = {
    "end": "blocks are closed with `stop`",
    "make": "declare values with `so`",
    "let": "declare values with `so`",
    "const": "constants are declared with `keep`",
    "return": "send a value back with `give`",
    "function": "functions are declared with `fun`",
    "fn": "functions are declared with `fun`",
    "print": "print with `say`",
    "println": "print with `say`",
    "string": "the text type is called `text`",
    "elif": "write `else if`",
    "then": "Fish has no `then`; just start the next line",
    "class": "Fish has `record` (data) and `extend` (methods) instead of classes",
    "struct": "data types are declared with `record`",
    "enum": "alternatives are declared with `choice`",
    "impl": "methods are added with `extend`",
}


class Tok:
    __slots__ = ("kind", "value", "line")

    def __init__(self, kind, value, line):
        self.kind, self.value, self.line = kind, value, line

    def __repr__(self):
        return f"Tok({self.kind},{self.value!r},{self.line})"


TWO = {"**", "==", "!=", ">=", "<=", "+=", "-=", "*=", "/=", "%="}
ONE = set("+-*/%=<>(),[].:")
ESC = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "{": "{", "}": "}"}


def tokenize(src, line0=1):
    toks, i, n, line, depth = [], 0, len(src), line0, 0

    def add(kind, val):
        toks.append(Tok(kind, val, line))

    while i < n:
        c = src[i]
        if c == "\n":
            if depth == 0 and toks and toks[-1].kind != "nl":
                add("nl", None)
            line += 1
            i += 1
        elif c in " \t\r":
            i += 1
        elif c == "#":
            while i < n and src[i] != "\n":
                i += 1
        elif c == "{":  # block comment, may nest
            start, d, i = line, 1, i + 1
            while i < n and d > 0:
                if src[i] == "{":
                    d += 1
                elif src[i] == "}":
                    d -= 1
                elif src[i] == "\n":
                    line += 1
                i += 1
            if d > 0:
                raise FishError("this { comment was never closed", start,
                                "close it with }")
        elif c == "}":
            raise FishError("found } without a matching {", line,
                            "{ } is used for block comments")
        elif c.isdigit():
            j, is_float = i, False
            while j < n and (src[j].isdigit() or src[j] == "_"):
                j += 1
            if j + 1 < n and src[j] == "." and src[j + 1].isdigit():
                is_float = True
                j += 1
                while j < n and (src[j].isdigit() or src[j] == "_"):
                    j += 1
            txt = src[i:j].replace("_", "")
            add("num", float(txt) if is_float else int(txt))
            i = j
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            w = src[i:j]
            i = j
            if w in HINTS:
                raise FishError(f"`{w}` is not a Fish word", line, HINTS[w])
            add("kw" if w in KEYWORDS else "id", w)
        elif c == '"':
            sline, i, parts, buf = line, i + 1, [], []
            while True:
                if i >= n or src[i] == "\n":
                    raise FishError("this text never ends", sline,
                                    'add a closing " on the same line')
                ch = src[i]
                if ch == '"':
                    i += 1
                    break
                if ch == "\\":
                    nx = src[i + 1] if i + 1 < n else ""
                    if nx not in ESC:
                        raise FishError(f"unknown escape \\{nx}", line)
                    buf.append(ESC[nx])
                    i += 2
                elif ch == "{":
                    d, j = 1, i + 1
                    while j < n and d > 0 and src[j] != "\n":
                        d += (src[j] == "{") - (src[j] == "}")
                        j += 1
                    if d > 0:
                        raise FishError("{ inside text was never closed", line,
                                        "write \\{ for a literal brace")
                    if buf:
                        parts.append(("t", "".join(buf)))
                        buf = []
                    parts.append(("e", src[i + 1:j - 1], line))
                    i = j
                else:
                    buf.append(ch)
                    i += 1
            if buf:
                parts.append(("t", "".join(buf)))
            add("str", parts)
        else:
            two = src[i:i + 2]
            if two in TWO:
                add("op", two)
                i += 2
            elif c in ONE:
                if c in "([":
                    depth += 1
                elif c in ")]":
                    depth = max(0, depth - 1)
                add("op", c)
                i += 1
            else:
                raise FishError(f"I don't understand the character {c!r}", line)
    if toks and toks[-1].kind != "nl":
        add("nl", None)
    add("eof", None)
    return toks


# ---------------------------------------------------------------- parser
# Node shape: (kind, line, ...)


class Parser:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    # helpers
    def peek(self, k=0):
        return self.t[min(self.i + k, len(self.t) - 1)]

    def at(self, kind, val=None):
        t = self.peek()
        return t.kind == kind and (val is None or t.value == val)

    def kw(self, v):
        return self.at("kw", v)

    def op(self, v):
        return self.at("op", v)

    def idw(self, v):
        return self.at("id", v)

    def next(self):
        t = self.t[self.i]
        self.i += 1
        return t

    def skip_nl(self):
        while self.at("nl"):
            self.i += 1

    def eol(self):
        if self.at("eof"):
            return
        if not self.at("nl"):
            t = self.peek()
            raise FishError("I expected the end of the line here", t.line,
                            "put one statement on each line")
        self.skip_nl()

    def expect_kw(self, v, why=""):
        if not self.kw(v):
            t = self.peek()
            raise FishError(f"I expected `{v}` here{why}", t.line)
        return self.next()

    def expect_op(self, v):
        if not self.op(v):
            raise FishError(f"I expected `{v}` here", self.peek().line)
        return self.next()

    def ident(self):
        t = self.peek()
        if t.kind != "id":
            raise FishError("I expected a name here", t.line)
        self.i += 1
        return t.value

    def typ(self):
        if self.kw("mut"):
            self.next()
            self.expect_kw("ref")
            return "mut ref " + self.typ()
        if self.kw("ref"):
            self.next()
            return "ref " + self.typ()
        n = self.ident()
        if self.kw("of"):
            self.next()
            return n + " of " + self.typ()
        if self.kw("to"):
            self.next()
            return n + " to " + self.typ()
        return n

    # statements
    def program(self):
        out = []
        while True:
            self.skip_nl()
            if self.at("eof"):
                return out
            out.append(self.stmt())

    def block(self, stops):
        out = []
        while True:
            self.skip_nl()
            t = self.peek()
            if t.kind == "eof":
                raise FishError("the file ended but a block was never closed",
                                t.line, "add `stop` to close it")
            if t.kind == "kw" and t.value in stops:
                return out
            out.append(self.stmt())

    def stmt(self):
        t = self.peek()
        if t.kind == "kw":
            v = t.value
            if v in ("so", "keep"):
                return self.so_stmt()
            if v == "fun":
                return self.fun_stmt()
            if v == "give":
                self.next()
                val = None
                if not (self.at("nl") or self.at("eof") or self.kw("stop")):
                    val = self.expr()
                self.eol_or_block_end()
                return ("give", t.line, val)
            if v == "if":
                return self.if_stmt()
            if v == "for":
                return self.for_stmt()
            if v == "while":
                self.next()
                c = self.expr()
                self.eol()
                body = self.block(("stop",))
                self.expect_kw("stop")
                return ("while", t.line, c, body)
            if v == "loop":
                self.next()
                self.eol()
                body = self.block(("stop",))
                self.expect_kw("stop")
                return ("loop", t.line, body)
            if v in ("break", "continue"):
                self.next()
                self.eol_or_block_end()
                return (v, t.line)
            if v == "match":
                return self.match_stmt()
            if v == "record":
                return self.record_stmt()
            if v == "choice":
                return self.choice_stmt()
            if v == "extend":
                return self.extend_stmt()
            if v == "say":
                self.next()
                items = []
                if not (self.at("nl") or self.at("eof") or self.kw("stop")):
                    items.append(self.expr())
                    while self.op(","):
                        self.next()
                        items.append(self.expr())
                self.eol_or_block_end()
                return ("say", t.line, items)
            if v in ("stop", "else", "or", "when"):
                raise FishError(f"this `{v}` does not belong to anything", t.line,
                                "check that every `if`, `for`, `fun`, `match` "
                                "has exactly one `stop`")
        e = self.expr()
        if self.at("op") and self.peek().value in ("=", "+=", "-=", "*=", "/=", "%="):
            if e[0] not in ("name", "index", "member"):
                raise FishError("you can only change a name or a list item", t.line)
            op = self.next().value
            rhs = self.expr()
            self.eol_or_block_end()
            return ("assign", t.line, e, op, rhs)
        self.eol_or_block_end()
        return ("expr", t.line, e)

    def eol_or_block_end(self):
        if self.at("eof") or (self.kw("stop")):
            return
        self.eol()

    def so_stmt(self):
        t = self.next()
        kind = t.value
        if kind == "so" and self.kw("var"):
            self.next()
            kind = "var"
        ty = None
        if self.at("id") and (self.peek(1).kind == "id" or
                              (self.peek(1).kind == "kw" and self.peek(1).value == "of")):
            ty = self.typ()
        name = self.ident()
        if self.op(":"):
            self.next()
            ty = self.typ()
        if not self.op("="):
            raise FishError(f"`{name}` needs a value", t.line,
                            f"write `{kind} {name} = ...`")
        self.next()
        val = self.expr()
        self.eol_or_block_end()
        return ("so", t.line, kind, name, ty, val)

    def fun_stmt(self):
        t = self.next()
        name = self.ident()
        self.expect_op("(")
        params = []
        while not self.op(")"):
            pmut = False
            if self.kw("mut") and self.peek(1).kind == "id" and self.peek(1).value == "self":
                self.next()
                pmut = True
            pn = self.ident()
            pt = None
            if self.op(":"):
                self.next()
                pt = self.typ()
            if pmut:
                pt = "mut ref @self"
            params.append((pn, pt))
            if self.op(","):
                self.next()
            else:
                break
        self.expect_op(")")
        rt = None
        if self.kw("gives"):
            self.next()
            rt = self.typ()
        self.eol()
        body = self.block(("stop",))
        self.expect_kw("stop")
        return ("fun", t.line, name, params, rt, body)

    def record_stmt(self):
        t = self.next()
        name = self.ident()
        self.eol()
        fields = []
        while True:
            self.skip_nl()
            if self.kw("stop"):
                break
            fname = self.ident()
            self.expect_op(":")
            fields.append((fname, self.typ()))
            self.eol()
        self.expect_kw("stop")
        return ("record", t.line, name, fields)

    def choice_stmt(self):
        t = self.next()
        name = self.ident()
        self.eol()
        variants = []
        while True:
            self.skip_nl()
            if self.kw("stop"):
                break
            vname = self.ident()
            fields = []
            if self.op("("):
                self.next()
                while not self.op(")"):
                    fname = self.ident()
                    self.expect_op(":")
                    fields.append((fname, self.typ()))
                    if self.op(","):
                        self.next()
                    else:
                        break
                self.expect_op(")")
            variants.append((vname, fields))
            self.eol()
        self.expect_kw("stop")
        return ("choice", t.line, name, variants)

    def extend_stmt(self):
        t = self.next()
        name = self.ident()
        self.eol()
        funs = []
        while True:
            self.skip_nl()
            if self.kw("stop"):
                break
            if not self.kw("fun"):
                raise FishError("only `fun` can go inside `extend`", self.peek().line)
            funs.append(self.fun_stmt())
        self.expect_kw("stop")
        return ("extend", t.line, name, funs)

    def if_stmt(self):
        t = self.next()
        cond = self.expr()
        self.eol()
        branches = [(cond, self.block(("else", "or", "stop")))]
        orelse = None
        while self.kw("else") or self.kw("or"):
            self.next()
            if self.kw("if"):
                self.next()
                c = self.expr()
                self.eol()
                branches.append((c, self.block(("else", "or", "stop"))))
            else:
                self.eol()
                orelse = self.block(("stop",))
                break
        self.expect_kw("stop")
        return ("if", t.line, branches, orelse)

    def for_stmt(self):
        t = self.next()
        names = [self.ident()]
        if self.op(","):
            self.next()
            names.append(self.ident())
        self.expect_kw("in")
        it = self.expr()
        self.eol()
        body = self.block(("stop",))
        self.expect_kw("stop")
        return ("for", t.line, names, it, body)

    def match_stmt(self):
        t = self.next()
        subj = self.expr()
        self.eol()
        arms, orelse = [], None
        self.skip_nl()
        while self.kw("when"):
            self.next()
            pats = [self.pattern()]
            while self.kw("or"):
                self.next()
                pats.append(self.pattern())
            guard = None
            if self.kw("if"):
                self.next()
                guard = self.expr()
            self.eol()
            arms.append((pats, guard, self.block(("when", "else", "or", "stop"))))
        if self.kw("else") or self.kw("or"):
            self.next()
            self.eol()
            orelse = self.block(("stop",))
        self.expect_kw("stop")
        return ("match", t.line, subj, arms, orelse)

    def pattern(self):
        t = self.peek()
        nxt = self.peek(1)
        if t.kind == "id" and t.value[0].isupper() and (
                nxt.kind == "op" and nxt.value == "(" or not t.value.isupper() or len(t.value) == 1):
            self.next()
            subs = []
            if self.op("("):
                self.next()
                while not self.op(")"):
                    subs.append(self.pattern())
                    if self.op(","):
                        self.next()
                    else:
                        break
                self.expect_op(")")
            return ("variant", t.value, subs, t.line)
        if t.kind == "id" and not t.value.isupper() and not (
                nxt.kind == "op" and nxt.value == "("):
            self.next()
            return ("bind", t.value)
        lo = self.additive()
        if self.kw("to") or self.kw("until"):
            incl = self.next().value == "to"
            hi = self.additive()
            return ("range", lo, hi, incl)
        return ("lit", lo)

    # expressions
    def expr(self):
        return self.or_()

    def or_(self):
        l = self.and_()
        while self.kw("or"):
            t = self.next()
            l = ("or", t.line, l, self.and_())
        return l

    def and_(self):
        l = self.not_()
        while self.kw("and"):
            t = self.next()
            l = ("and", t.line, l, self.not_())
        return l

    def not_(self):
        if self.kw("not"):
            t = self.next()
            return ("not", t.line, self.not_())
        return self.cmp()

    def cmp(self):
        l = self.range_()
        t = self.peek()
        op = None
        if t.kind == "kw" and t.value == "is":
            self.next()
            if self.kw("not"):
                self.next()
                op = "!="
            elif self.idw("above"):
                self.next()
                op = ">"
            elif self.idw("below"):
                self.next()
                op = "<"
            elif self.idw("at"):
                self.next()
                if self.idw("least"):
                    op = ">="
                elif self.idw("most"):
                    op = "<="
                else:
                    raise FishError("after `is at` I expected `least` or `most`",
                                    t.line)
                self.next()
            else:
                op = "=="
        elif t.kind == "op" and t.value in ("==", "!=", "<", ">", "<=", ">="):
            op = self.next().value
        elif t.kind == "kw" and t.value == "in":
            self.next()
            return ("in", t.line, l, self.range_())
        if op is None:
            return l
        r = self.range_()
        nt = self.peek()
        if (nt.kind == "kw" and nt.value == "is") or (
                nt.kind == "op" and nt.value in ("==", "!=", "<", ">", "<=", ">=")):
            raise FishError("comparisons can't be chained", nt.line,
                            "join them with `and`, like `a is below b and b is below c`")
        return ("cmp", t.line, op, l, r)

    def range_(self):
        lo = self.additive()
        down = False
        if self.idw("down") and self.peek(1).kind == "kw" and self.peek(1).value == "to":
            self.next()
            down = True
        if self.kw("to") or self.kw("until"):
            t = self.next()
            hi = self.additive()
            step = None
            if self.idw("step"):
                self.next()
                step = self.additive()
            return ("range", t.line, lo, hi, t.value == "to", step, down)
        return lo

    def additive(self):
        l = self.mult()
        while self.at("op") and self.peek().value in ("+", "-"):
            t = self.next()
            l = ("bin", t.line, t.value, l, self.mult())
        return l

    def mult(self):
        l = self.unary()
        while (self.at("op") and self.peek().value in ("*", "/", "%")) or self.kw("mod"):
            t = self.next()
            l = ("bin", t.line, "%" if t.value == "mod" else t.value, l, self.unary())
        return l

    def unary(self):
        if self.kw("ref") or self.kw("mut"):
            t = self.next()
            m = False
            if t.value == "mut":
                self.expect_kw("ref")
                m = True
            return ("ref", t.line, self.postfix(), m)
        if self.op("-"):
            t = self.next()
            return ("neg", t.line, self.unary())
        return self.power()

    def power(self):
        b = self.postfix()
        if self.op("**"):
            t = self.next()
            return ("bin", t.line, "**", b, self.unary())
        return b

    def postfix(self):
        e = self.primary()
        while True:
            if self.op("("):
                t = self.next()
                args = []
                while not self.op(")"):
                    args.append(self.expr())
                    if self.op(","):
                        self.next()
                    else:
                        break
                self.expect_op(")")
                e = ("call", t.line, e, args)
            elif self.op("["):
                t = self.next()
                idx = self.expr()
                self.expect_op("]")
                e = ("index", t.line, e, idx)
            elif self.op("."):
                t = self.next()
                e = ("member", t.line, e, self.ident())
            else:
                return e

    def primary(self):
        t = self.next()
        if t.kind == "num":
            return ("num", t.line, t.value)
        if t.kind == "str":
            parts = []
            for p in t.value:
                if p[0] == "t":
                    parts.append(p)
                else:
                    sub = Parser(tokenize(p[1], p[2]))
                    e = sub.expr()
                    if not (sub.at("nl") or sub.at("eof")):
                        raise FishError("this {…} inside text has extra words in it", p[2])
                    parts.append(("e", e))
            return ("str", t.line, parts)
        if t.kind == "kw" and t.value in ("true", "false"):
            return ("bool", t.line, t.value == "true")
        if t.kind == "kw" and t.value == "none":
            return ("none", t.line)
        if t.kind == "id":
            return ("name", t.line, t.value)
        if t.kind == "op" and t.value == "[":
            items = []
            while not self.op("]"):
                items.append(self.expr())
                if self.op(","):
                    self.next()
                else:
                    break
            self.expect_op("]")
            return ("list", t.line, items)
        if t.kind == "op" and t.value == "(":
            e = self.expr()
            self.expect_op(")")
            return e
        shown = "the end of the line" if t.kind in ("nl", "eof") else f"`{t.value}`"
        raise FishError(f"I didn't expect {shown} here", t.line,
                        "a value (number, text, name, list) should go here")


# ---------------------------------------------------------------- runtime


class Scope:
    def __init__(self, parent=None):
        self.vars, self.parent = {}, parent

    def find(self, name):
        s = self
        while s:
            if name in s.vars:
                return s
            s = s.parent
        return None

    def names(self):
        out, s = [], self
        while s:
            out += list(s.vars)
            s = s.parent
        return out


class Fun:
    def __init__(self, name, params, rtype, body, closure):
        self.name, self.params, self.rtype = name, params, rtype
        self.body, self.closure = body, closure


class Rec:
    def __init__(self, name, fields):
        self.name, self.fields = name, fields

    def __eq__(self, o):
        return isinstance(o, Rec) and self.name == o.name and self.fields == o.fields


class Var:
    def __init__(self, choice, variant, vals):
        self.choice, self.variant, self.vals = choice, variant, vals

    def __eq__(self, o):
        return isinstance(o, Var) and self.variant == o.variant and self.vals == o.vals


class Ref:
    """a `mut ref` to a plain value living in a variable, field or list item"""

    def __init__(self, get, set_):
        self.get, self.set = get, set_


def deref(v):
    return v.get() if isinstance(v, Ref) else v


class BreakEx(Exception):
    pass


class ContinueEx(Exception):
    pass


class GiveEx(Exception):
    def __init__(self, value):
        self.value = value


INT_TYPES = {"whole", "i8", "i16", "i32", "i64", "i128", "u8", "u16", "u32",
             "u64", "u128", "size", "offset", "byte"}
FLOAT_TYPES = {"decimal", "f32", "f64"}


def kind_of(v):
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "whole"
    if isinstance(v, float):
        return "decimal"
    if isinstance(v, str):
        return "text"
    if isinstance(v, (list, range)):
        return "list"
    if v is None:
        return "none"
    if isinstance(v, Rec):
        return v.name
    if isinstance(v, Var):
        return v.choice
    if isinstance(v, Fun) or callable(v):
        return "function"
    return "value"


def fmt_decimal(x):
    """Decimals show with up to 6 places, trailing zeros trimmed (at least one kept)."""
    if x != x or x in (float("inf"), float("-inf")):
        raise FishError("can't show infinity or nan")
    if abs(x) >= 9.2e12:
        raise FishError("this decimal is too large to show (limit about 9.2 trillion)")
    t = f"{x:.6f}".rstrip("0")
    return t + "0" if t.endswith(".") else t


def fmt(v, inner=False):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "none"
    if isinstance(v, str):
        return f'"{v}"' if inner else v
    if isinstance(v, (list, range)):
        return "[" + ", ".join(fmt(x, True) for x in v) + "]"
    if isinstance(v, float):
        return fmt_decimal(v)
    if isinstance(v, Rec):
        return v.name + "(" + ", ".join(f"{k}: {fmt(x, True)}" for k, x in v.fields.items()) + ")"
    if isinstance(v, Var):
        if not v.vals:
            return v.variant
        return v.variant + "(" + ", ".join(f"{k}: {fmt(x, True)}" for k, x in v.vals.items()) + ")"
    if isinstance(v, Fun):
        return f"<fun {v.name}>"
    return str(v)


def trunc_div(a, b):
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


class Interp:
    def __init__(self):
        self.globals = Scope()
        self.records, self.choices, self.variants, self.methods = {}, {}, {}, {}
        g = self.globals.vars
        for name, fn in {
            "ask": self.b_ask, "whole": self.b_whole, "decimal": self.b_decimal,
            "text": lambda v: fmt(v),
            "length": lambda v: len(v.encode("utf-8")) if isinstance(v, str) else len(v),
            "sqrt": self.b_sqrt, "abs": abs, "min": min, "max": max,
            "panic": self.b_panic, "check": self.b_check,
        }.items():
            g[name] = [fn, False, "builtin"]

    # builtins
    def b_ask(self, prompt=""):
        try:
            return input(prompt)
        except EOFError:
            return ""

    def b_sqrt(self, v):
        if not isinstance(v, float):
            raise FishError(f"`sqrt` needs a decimal, but got {kind_of(v)}", None,
                            "convert with decimal(x)")
        if v < 0:
            raise FishError("can't take the square root of a negative number", None)
        return v ** 0.5

    def b_whole(self, v):
        try:
            r = int(v) if not isinstance(v, str) else int(v.strip())
            if not -2**63 <= r < 2**63:
                raise OverflowError
            return r
        except ValueError:
            raise FishError(f'"{v}" is not a whole number', None)
        except OverflowError:
            raise FishError("a whole number got too big (overflow)", None)

    def b_decimal(self, v):
        if isinstance(v, str):
            raise FishError("`decimal` converts whole numbers; text parsing comes later", None)
        try:
            return float(v)
        except ValueError:
            raise FishError(f'"{v}" is not a decimal number', None)

    def b_panic(self, msg="panic"):
        raise FishError(f"panic: {msg}", None)

    def b_check(self, cond, msg="check failed"):
        if cond is not True:
            raise FishError(f"check failed: {msg}", None)

    # types
    def check_type(self, ty, v, what, line):
        if ty is None:
            return v
        base = ty.split()[0]
        k = kind_of(v)
        if base in INT_TYPES:
            ok = k == "whole"
        elif base in FLOAT_TYPES:
            ok = k == "decimal"
        elif base == "text":
            ok = k == "text"
        elif base == "bool":
            ok = k == "bool"
        elif base == "char":
            ok = k == "text" and len(v) == 1
        elif base in ("list", "array", "slice"):
            ok = k == "list"
        elif base == "nothing":
            ok = v is None
        elif base in self.records or base in self.choices:
            ok = k == base
        else:
            err_unknown = True
            raise FishError(f"I don't know the type `{base}`", line,
                            "types: whole, decimal, text, bool, list, or one you made with `record` / `choice`")
        if not ok:
            raise FishError(f"{what} should be {ty} but got {k}", line)
        return v

    # execution
    def run(self, prog):
        for s in prog:  # hoist declarations so order doesn't matter
            if s[0] in ("record", "choice"):
                self.exec(s, self.globals)
        for s in prog:
            if s[0] in ("fun", "extend"):
                self.exec(s, self.globals)
        for s in prog:
            if s[0] not in ("fun", "record", "choice", "extend"):
                self.exec(s, self.globals)

    def run_block(self, stmts, env):
        for s in stmts:
            self.exec(s, env)

    def truth(self, v, line, what):
        if not isinstance(v, bool):
            raise FishError(f"{what} must be true or false, but this is {kind_of(v)}",
                            line, "use a comparison such as `is above` or `is not`")
        return v

    def exec(self, n, env):
        k, line = n[0], n[1]
        if k == "so":
            _, _, kind, name, ty, val = n
            v = self.check_type(ty, self.eval(val, env), f"`{name}`", line)
            env.vars[name] = [v, kind == "var", kind]
        elif k == "say":
            print(" ".join(fmt(self.eval(e, env)) for e in n[2]))
        elif k == "expr":
            self.eval(n[2], env)
        elif k == "assign":
            self.assign(n, env)
        elif k == "fun":
            env.vars[n[2]] = [Fun(n[2], n[3], n[4], n[5], env), False, "fun"]
        elif k == "record":
            self.records[n[2]] = n[3]
        elif k == "choice":
            self.choices[n[2]] = n[3]
            for vname, fields in n[3]:
                self.variants[vname] = (n[2], fields)
        elif k == "extend":
            tbl = self.methods.setdefault(n[2], {})
            for f in n[3]:
                tbl[f[2]] = Fun(f[2], f[3], f[4], f[5], env)
        elif k == "give":
            raise GiveEx(None if n[2] is None else self.eval(n[2], env))
        elif k == "if":
            for cond, body in n[2]:
                if self.truth(self.eval(cond, env), line, "an `if` condition"):
                    self.run_block(body, Scope(env))
                    return
            if n[3] is not None:
                self.run_block(n[3], Scope(env))
        elif k == "while":
            while self.truth(self.eval(n[2], env), line, "a `while` condition"):
                try:
                    self.run_block(n[3], Scope(env))
                except BreakEx:
                    break
                except ContinueEx:
                    continue
        elif k == "loop":
            while True:
                try:
                    self.run_block(n[2], Scope(env))
                except BreakEx:
                    break
                except ContinueEx:
                    continue
        elif k == "for":
            names, it = n[2], self.eval(n[3], env)
            if isinstance(it, str):
                it = list(it)
            if not isinstance(it, (list, range)):
                raise FishError(f"I can only loop over a list, text, or range, not {kind_of(it)}", line)
            pairs = enumerate(list(it)) if len(names) == 2 else ((None, x) for x in list(it))
            for i, x in pairs:
                s = Scope(env)
                if len(names) == 2:
                    s.vars[names[0]] = [i, False, "so"]
                    s.vars[names[1]] = [x, False, "so"]
                else:
                    s.vars[names[0]] = [x, False, "so"]
                try:
                    self.run_block(n[4], s)
                except BreakEx:
                    break
                except ContinueEx:
                    continue
        elif k == "break":
            raise BreakEx()
        elif k == "continue":
            raise ContinueEx()
        elif k == "match":
            self.do_match(n, env)

    def do_match(self, n, env):
        subj = self.eval(n[2], env)
        for pats, guard, body in n[3]:
            for p in pats:
                s = Scope(env)
                if self.pat_ok(p, subj, s):
                    if guard is None or self.truth(self.eval(guard, s), n[1], "a `when … if` guard"):
                        self.run_block(body, s)
                        return
                    break
        if n[4] is not None:
            self.run_block(n[4], Scope(env))
        elif isinstance(subj, Var):
            raise FishError(f"no `when` matched {fmt(subj)}", n[1],
                            "add the missing cases or an `else`")

    def pat_ok(self, p, v, scope):
        if p[0] == "variant":
            if p[1] not in self.variants:
                raise FishError(f"I don't know a choice called `{p[1]}`", p[3])
            fields = self.variants[p[1]][1]
            if len(p[2]) != len(fields):
                raise FishError(f"`{p[1]}` has {len(fields)} value(s), but this pattern has {len(p[2])}", p[3])
            if not isinstance(v, Var) or v.variant != p[1]:
                return False
            return all(self.pat_ok(sp, x, scope) for sp, x in zip(p[2], v.vals.values()))
        if p[0] == "bind":
            if p[1] != "_":
                scope.vars[p[1]] = [v, False, "so"]
            return True
        if p[0] == "lit":
            return self.same(self.eval(p[1], scope), v)
        lo, hi = self.eval(p[1], scope), self.eval(p[2], scope)
        if not (isinstance(v, (int, float)) and not isinstance(v, bool)):
            return False
        return lo <= v <= hi if p[3] else lo <= v < hi

    @staticmethod
    def same(a, b):
        if isinstance(a, bool) != isinstance(b, bool):
            return False
        return a == b

    def assign(self, n, env):
        _, line, target, op, rhs = n
        val = self.eval(rhs, env)
        if target[0] == "name":
            name = target[2]
            s = env.find(name)
            if s is None:
                raise FishError(f"I don't know the name `{name}`", line,
                                f"create it first with `so var {name} = ...`")
            cell = s.vars[name]
            if isinstance(cell[0], Ref):
                cur = cell[0].get()
                cell[0].set(self.binop(op[0], cur, val, line) if op != "=" else val)
                return
            if not cell[1]:
                how = {"so": f"it was made with `so`; use `so var {name} = ...` if it should change",
                       "keep": "`keep` values never change",
                       "fun": "functions can't be reassigned",
                       "builtin": "it is built in"}.get(cell[2], "it can't change")
                raise FishError(f"`{name}` can't be changed: {how}", line)
            if op != "=":
                val = self.binop(op[0], cell[0], val, line)
            cell[0] = val
        elif target[0] == "member":
            root = target
            while root[0] in ("member", "index"):
                root = root[2]
            if root[0] == "name":
                s = env.find(root[2])
                if s is not None and not s.vars[root[2]][1]:
                    raise FishError(f"`{root[2]}` can't be changed, so neither can its `{target[3]}`", line,
                                    f"use `so var {root[2]} = ...`")
            obj = self.eval(target[2], env)
            if not isinstance(obj, Rec) or target[3] not in obj.fields:
                raise FishError(f"{kind_of(obj)} has no field `{target[3]}`", line)
            ftype = dict(self.records[obj.name])[target[3]]
            if op != "=":
                val = self.binop(op[0], obj.fields[target[3]], val, line)
            obj.fields[target[3]] = self.check_type(ftype, val, f"`{target[3]}`", line)
        else:
            obj, idx = self.eval(target[2], env), self.eval(target[3], env)
            if not isinstance(obj, list):
                raise FishError("only lists have items you can change", line)
            self.check_index(obj, idx, line)
            if op != "=":
                val = self.binop(op[0], obj[idx], val, line)
            obj[idx] = val

    def check_index(self, obj, idx, line):
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise FishError(f"an item number must be a whole number, not {kind_of(idx)}", line)
        if not (0 <= idx < len(obj)):
            raise FishError(f"item {idx} is outside this {kind_of(obj)}, "
                            f"which has {len(obj)} items (numbered from 0)", line)

    # expressions
    def eval(self, n, env):
        k, line = n[0], n[1]
        if k == "num":
            return n[2]
        if k == "str":
            return "".join(p[1] if p[0] == "t" else fmt(self.eval(p[1], env)) for p in n[2])
        if k == "bool":
            return n[2]
        if k == "none":
            return None
        if k == "ref":
            return self.make_ref(n, env)
        if k == "name":
            s = env.find(n[2])
            if s is None and n[2] in self.variants:
                cname, fields = self.variants[n[2]]
                if fields:
                    raise FishError(f"`{n[2]}` needs {len(fields)} value(s)", line,
                                    f"write `{n[2]}(...)`")
                return Var(cname, n[2], {})
            if s is None:
                close = difflib.get_close_matches(n[2], env.names(), n=1)
                raise FishError(f"I don't know the name `{n[2]}`", line,
                                f"did you mean `{close[0]}`?" if close else None)
            return deref(s.vars[n[2]][0])
        if k == "list":
            return [self.eval(e, env) for e in n[2]]
        if k == "neg":
            v = self.eval(n[2], env)
            self.need_num(v, line, "-")
            return -v
        if k == "not":
            return not self.truth(self.eval(n[2], env), line, "`not` needs a value that")
        if k == "and":
            l = self.truth(self.eval(n[2], env), line, "`and` needs a value that")
            return l and self.truth(self.eval(n[3], env), line, "`and` needs a value that")
        if k == "or":
            l = self.truth(self.eval(n[2], env), line, "`or` needs a value that")
            return l or self.truth(self.eval(n[3], env), line, "`or` needs a value that")
        if k == "bin":
            return self.binop(n[2], self.eval(n[3], env), self.eval(n[4], env), line)
        if k == "cmp":
            return self.compare(n[2], self.eval(n[3], env), self.eval(n[4], env), line)
        if k == "in":
            a, b = self.eval(n[2], env), self.eval(n[3], env)
            if isinstance(b, str) and isinstance(a, str):
                return a in b
            if isinstance(b, (list, range)):
                return any(self.same(a, x) for x in b)
            raise FishError(f"`in` needs a list, text, or range on the right, not {kind_of(b)}", line)
        if k == "range":
            lo, hi = self.eval(n[2], env), self.eval(n[3], env)
            step = 1 if n[5] is None else self.eval(n[5], env)
            for x in (lo, hi, step):
                if isinstance(x, bool) or not isinstance(x, int):
                    raise FishError("ranges need whole numbers", line)
            if step <= 0:
                raise FishError("`step` must be above 0", line)
            if n[6]:
                return range(lo, hi - 1 if n[4] else hi, -step)
            return range(lo, hi + 1 if n[4] else hi, step)
        if k == "index":
            obj, idx = self.eval(n[2], env), self.eval(n[3], env)
            if isinstance(obj, str):
                self.check_index(obj, idx, line)
                return obj[idx]
            if not isinstance(obj, (list, range)):
                raise FishError(f"you can't pick items out of {kind_of(obj)}", line)
            self.check_index(obj, idx, line)
            return obj[idx]
        if k == "member":
            return self.member(self.eval(n[2], env), n[3], line)
        if k == "call":
            return self.call(n, env)
        raise FishError(f"internal error: unknown node {k}", line)

    def make_ref(self, n, env):
        _, line, op, mut = n
        if not mut:
            return self.eval(op, env)
        if op[0] == "name":
            s = env.find(op[2])
            if s is None:
                raise FishError(f"I don't know the name `{op[2]}`", line)
            cell = s.vars[op[2]]
            if isinstance(cell[0], Ref):
                return cell[0]
            if not cell[1]:
                raise FishError(f"`{op[2]}` can't be lent as `mut ref`", line,
                                f"make it with `so var {op[2]} = ...`")
            if isinstance(cell[0], (list, Rec, Var)):
                return cell[0]
            return Ref(lambda: cell[0], lambda v: cell.__setitem__(0, v))
        obj = self.eval(op[2], env)
        if op[0] == "member" and isinstance(obj, Rec):
            f = obj.fields
            if isinstance(f[op[3]], (list, Rec, Var)):
                return f[op[3]]
            return Ref(lambda: f[op[3]], lambda v: f.__setitem__(op[3], v))
        if op[0] == "index" and isinstance(obj, list):
            i = self.eval(op[3], env)
            self.check_index(obj, i, line)
            if isinstance(obj[i], (list, Rec, Var)):
                return obj[i]
            return Ref(lambda: obj[i], lambda v: obj.__setitem__(i, v))
        raise FishError("this can't be lent as `mut ref`", line)

    def need_num(self, v, line, op):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise FishError(f"`{op}` needs numbers, but got {kind_of(v)}", line)

    def binop(self, op, a, b, line):
        if op == "+" and isinstance(a, str) and isinstance(b, str):
            return a + b
        if op == "+" and isinstance(a, list) and isinstance(b, list):
            return a + b
        if op == "+" and (isinstance(a, str) or isinstance(b, str)):
            raise FishError(f"can't add {kind_of(a)} and {kind_of(b)}", line,
                            'put values inside text with braces: "{a} and {b}"')
        self.need_num(a, line, op)
        self.need_num(b, line, op)
        if type(a) is not type(b):
            raise FishError(f"can't mix {kind_of(a)} and {kind_of(b)}", line,
                            "convert one side with decimal(x) or whole(x)")
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op in ("/", "%") and b == 0:
            raise FishError("can't divide by zero", line)
        if op == "/":
            if isinstance(a, int) and isinstance(b, int):
                return trunc_div(a, b)
            return a / b
        if op == "%":
            if isinstance(a, int) and isinstance(b, int):
                return a - b * trunc_div(a, b)
            return a - b * int(a / b)
        if op == "**":
            if isinstance(a, int):
                if b < 0:
                    raise FishError("a whole number can't be raised to a negative power", line,
                                    "use decimals: decimal(a) ** decimal(b)")
                return a ** b
            r = a ** b
            if isinstance(r, complex):
                raise FishError("this power has no real result", line)
            return r
        raise FishError(f"unknown operator {op}", line)

    def compare(self, op, a, b, line):
        if op == "==":
            return self.same(a, b)
        if op == "!=":
            return not self.same(a, b)
        both_num = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b))
        both_text = isinstance(a, str) and isinstance(b, str)
        if not (both_num or both_text) or type(a) is not type(b):
            raise FishError(f"can't compare {kind_of(a)} with {kind_of(b)}", line)
        return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]

    def member(self, obj, name, line):
        if isinstance(obj, Rec):
            if name in obj.fields:
                return obj.fields[name]
            raise FishError(f"{obj.name} has no field `{name}`", line,
                            "its fields: " + ", ".join(obj.fields))
        if name == "length" and isinstance(obj, str):
            return len(obj.encode("utf-8"))  # text length is in bytes
        if name == "length" and isinstance(obj, (list, range)):
            return len(obj)
        raise FishError(f"{kind_of(obj)} has no property `{name}`", line)

    def call(self, n, env):
        _, line, callee, argn = n
        args = [self.eval(a, env) for a in argn]
        if callee[0] == "name" and env.find(callee[2]) is None:
            nm = callee[2]
            if nm in self.records:
                return self.construct(nm, self.records[nm], args, line, None)
            if nm in self.variants:
                cname, fields = self.variants[nm]
                return self.construct(nm, fields, args, line, cname)
        if callee[0] == "member":
            obj = self.eval(callee[2], env)
            return self.method(obj, callee[3], args, line)
        f = self.eval(callee, env)
        if isinstance(f, Fun):
            return self.call_fun(f, args, line)
        if callable(f):
            try:
                return f(*args)
            except FishError as e:
                if e.line is None:
                    e.line = line
                raise
            except TypeError:
                raise FishError("wrong number or kind of values for this call", line)
        raise FishError(f"{kind_of(f)} is not a function, so it can't be called", line)

    def construct(self, name, fields, args, line, choice):
        if len(args) != len(fields):
            raise FishError(f"`{name}` needs {len(fields)} value(s) but got {len(args)}", line)
        vals = {fn: self.check_type(ft, a, f"`{fn}`", line) for (fn, ft), a in zip(fields, args)}
        return Rec(name, vals) if choice is None else Var(choice, name, vals)

    def call_fun(self, f, args, line):
        if len(args) != len(f.params):
            raise FishError(f"`{f.name}` needs {len(f.params)} value(s) but got {len(args)}", line)
        env = Scope(f.closure)
        for (pn, pt), a in zip(f.params, args):
            if pt and pt.startswith(("ref ", "mut ref ")):
                base = pt.split("ref ", 1)[1]
                if not base.startswith("@"):
                    self.check_type(base, deref(a), f"`{pn}`", line)
                env.vars[pn] = [a, pt.startswith("mut ref "), "so"]
            else:
                env.vars[pn] = [self.check_type(pt, a, f"`{pn}`", line), False, "so"]
        result = None
        try:
            body = f.body
            for s in body[:-1]:
                self.exec(s, env)
            if body:
                last = body[-1]
                result = self.eval(last[2], env) if last[0] == "expr" else self.exec(last, env)
        except GiveEx as g:
            result = g.value
        return self.check_type(f.rtype, result, f"the value `{f.name}` gives", line)

    def method(self, obj, name, args, line):
        if name == "copy" and isinstance(obj, (str, list, Rec, Var)):
            import copy as _copy
            return _copy.deepcopy(obj)
        if isinstance(obj, (Rec, Var)):
            tn = obj.name if isinstance(obj, Rec) else obj.choice
            m = self.methods.get(tn, {}).get(name)
            if m is None:
                raise FishError(f"{tn} has no method `.{name}`", line,
                                f"add it with `extend {tn}`")
            return self.call_fun(m, [obj] + args, line)
        def need(count):
            if len(args) != count:
                raise FishError(f"`.{name}` needs {count} value(s)", line)
        if isinstance(obj, str):
            m = {
                "to_upper": lambda: obj.upper(), "to_lower": lambda: obj.lower(),
                "trim": lambda: obj.strip(), "is_empty": lambda: len(obj) == 0,
                "split": lambda: obj.split(args[0]) if args else obj.split(),
                "contains": lambda: args[0] in obj,
                "starts_with": lambda: obj.startswith(args[0]),
                "ends_with": lambda: obj.endswith(args[0]),
                "replace": lambda: obj.replace(args[0], args[1]),
            }
        elif isinstance(obj, (list, range)):
            lst = obj if isinstance(obj, list) else list(obj)
            m = {
                "push": lambda: lst.append(args[0]),
                "pop": lambda: lst.pop() if lst else self.fail("can't pop from an empty list", line),
                "contains": lambda: any(self.same(args[0], x) for x in lst),
                "is_empty": lambda: len(lst) == 0,
                "reversed": lambda: lst[::-1],
                "join": lambda: args[0].join(fmt(x) for x in lst),
            }
        else:
            m = {}
        if name not in m:
            raise FishError(f"{kind_of(obj)} has no method `.{name}`", line)
        try:
            return m[name]()
        except IndexError:
            raise FishError(f"`.{name}` is missing a value", line)

    @staticmethod
    def fail(msg, line):
        raise FishError(msg, line)


# ---------------------------------------------------------------- CLI


def report(e, path, src):
    lines = src.splitlines()
    print(f"error: {e.msg}", file=sys.stderr)
    if e.line:
        print(f"  --> {path}:{e.line}", file=sys.stderr)
        if 1 <= e.line <= len(lines):
            print("   |", file=sys.stderr)
            print(f"{e.line:>3}| {lines[e.line - 1]}", file=sys.stderr)
            print("   |", file=sys.stderr)
    if e.help:
        print(f"help: {e.help}", file=sys.stderr)


def main(argv):
    if len(argv) >= 2 and argv[1] == "--version":
        print(f"fish {VERSION}")
        return 0
    if len(argv) != 3 or argv[1] not in ("run", "check"):
        print("usage: fish.py run FILE | fish.py check FILE", file=sys.stderr)
        return 2
    cmd, path = argv[1], argv[2]
    try:
        src = open(path, encoding="utf-8").read()
    except OSError as ex:
        print(f"error: can't read {path}: {ex.strerror}", file=sys.stderr)
        return 2
    try:
        prog = Parser(tokenize(src)).program()
        if cmd == "run":
            Interp().run(prog)
        else:
            print("ok: no problems found")
    except FishError as e:
        report(e, path, src)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
