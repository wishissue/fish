#!/usr/bin/env python3
"""fishc: the Fish native compiler (stage 2).

Pipeline: source -> tokens -> syntax tree (shared with fish.py) -> type check
and code generation -> x86-64 assembly -> machine code (system `as` + `ld`).
Output programs use raw Linux system calls only: no C, no libc.

    python3 fishc.py build FILE [-o OUT]   compile to a native executable
    python3 fishc.py run   FILE            compile and run
    python3 fishc.py check FILE            type check only
    python3 fishc.py asm   FILE            print the generated assembly
"""
import os
import subprocess
import sys
import tempfile

from fish import FishError, Parser, tokenize, report

# ---------------------------------------------------------------- types

WHOLE, BOOL, TEXT, NOTHING, UNKNOWN = "whole", "bool", "text", "nothing", "?"
DECIMAL = "decimal"


USER = {}   # user type name -> "rec" or "choice"
EXH = set()  # ids of `match` statements that cover every case


def is_list(t):
    return isinstance(t, tuple) and t[0] == "list"


def tname(t):
    if isinstance(t, str):
        return t
    return "list of " + tname(t[1]) if t[0] == "list" else t[1]


def err(msg, line=None, help=None):
    raise FishError(msg, line, help)


def param_kind(pn, pt, first):
    """how a parameter receives its value: by value, or borrowed with ref / mut ref"""
    if pt and pt.startswith("mut ref "):
        return "mut", pt[8:]
    if pt and pt.startswith("ref "):
        return "ref", pt[4:]
    if pn == "self" and first:
        return "ref", None
    return "val", pt


def parse_type(s, line):
    if s.startswith(("ref ", "mut ref ")):
        err("`ref` types can only be used for function parameters", line,
            "values you keep (variables, fields, results) are always owned")
    if s in USER:
        return (USER[s], s)
    if s in ("whole", "i64"):
        return WHOLE
    if s in ("decimal", "f64"):
        return DECIMAL
    if s == "bool":
        return BOOL
    if s == "text":
        return TEXT
    if s == "nothing":
        return NOTHING
    if s.startswith("list of "):
        return ("list", parse_type(s[8:], line))
    if s not in ("i8", "i16", "i32", "i128", "u8", "u16", "u32", "u64", "u128", "size", "offset", "byte", "f32", "char"):
        err(f"I don't know the type `{s}`", line,
            "types: whole, decimal, bool, text, nothing, list of ..., or your own record / choice")
    err(f"the type `{s}` is not supported by the native compiler yet", line,
        "supported types: whole, bool, text, nothing, list of ..., or your own record / choice")


def lenient(*ts):
    return any(t == UNKNOWN for t in ts)


# ---------------------------------------------------------------- context


class Info:
    """ownership facts about one variable"""
    __slots__ = ("name", "owned", "borrowed", "moved", "mode", "glob", "deref")

    def __init__(self, name, owned=False, borrowed=False, mode=None, glob=False):
        self.name, self.owned, self.borrowed, self.mode, self.glob = name, owned, borrowed, mode, glob
        self.moved, self.deref = None, False


class Ctx:
    def __init__(self, name, label, dry, is_main=False):
        self.name, self.label, self.dry, self.is_main = name, label, dry, is_main
        self.code, self.scopes, self.slots = [], [{}], 0
        self.rtype, self.gives, self.loops = None, [], []
        self.implicit = False

    def emit(self, s):
        self.code.append("    " + s)

    def mark(self, label):
        self.code.append(label + ":")

    def slot(self):
        self.slots += 1
        return -8 * self.slots


def always_gives(stmts):
    for s in stmts:
        k = s[0]
        if k == "give":
            return True
        if k == "if" and s[3] is not None:
            if all(always_gives(b) for _, b in s[2]) and always_gives(s[3]):
                return True
        if k == "match" and (s[4] is not None or id(s) in EXH):
            if all(always_gives(b) for _, _, b in s[3]) and (s[4] is None or always_gives(s[4])):
                return True
        if k == "loop" and not has_break(s[2]):
            return True
    return False


def has_break(stmts):
    for s in stmts:
        k = s[0]
        if k == "break":
            return True
        if k == "if":
            if any(has_break(b) for _, b in s[2]) or (s[3] and has_break(s[3])):
                return True
        if k == "match":
            if any(has_break(b) for _, _, b in s[3]) or (s[4] and has_break(s[4])):
                return True
    return False


CMP_SET = {"==": "sete", "!=": "setne", "<": "setl", ">": "setg", "<=": "setle", ">=": "setge"}
BUILTINS = {"ask", "whole", "decimal", "sqrt", "text", "length", "abs", "min", "max", "panic", "check"}


class Compiler:
    def __init__(self):
        self.n = 0
        self.strs = {}
        self.bss = []
        self.funs, self.globals = {}, {}
        self.records, self.choices, self.variants, self.csize = {}, {}, {}, {}
        self.glue, self.glue_asm = {}, []
        self.main_owned = set()
        self.rtypes, self.inprogress, self.queue, self.done = {}, set(), [], {}
        self.fun_asm = []
        self.ctx = None

    # --- ownership helpers
    def is_owned(self, t):
        return t == TEXT or (isinstance(t, tuple) and t[0] in ("list", "rec", "choice"))

    def new_glue(self, kind, t):
        k = (kind, str(t))
        if k in self.glue:
            return self.glue[k], False
        lab = f"{kind}_{len(self.glue)}"
        self.glue[k] = lab
        return lab, True

    def drop_label(self, t):
        if t == TEXT:
            return "rt_free"
        lab, fresh = self.new_glue("drop", t)
        if not fresh:
            return lab
        o = [f"{lab}:", "    test rdi, rdi", f"    jz {lab}_end", "    push rbx", "    push r12", "    push r13", "    mov rbx, rdi"]
        if is_list(t):
            if self.is_owned(t[1]):
                e = self.drop_label(t[1])
                o += ["    mov r12, [rbx]", "    mov r13, [rbx+16]", f"{lab}_loop:", "    test r12, r12", f"    jz {lab}_done",
                      "    dec r12", "    mov rdi, [r13+r12*8]", f"    call {e}", f"    jmp {lab}_loop", f"{lab}_done:"]
            o += ["    mov rdi, [rbx+16]", "    call rt_free"]
        elif t[0] == "rec":
            for i, (fn, ft) in enumerate(self.records[t[1]]):
                if self.is_owned(ft):
                    o += [f"    mov rdi, [rbx+{8 * i}]", f"    call {self.drop_label(ft)}"]
        else:
            for vn in self.choices[t[1]]:
                _, tag, fields = self.variants[vn]
                if any(self.is_owned(ft) for _, ft in fields):
                    nxt = f"{lab}_n{tag}"
                    o += [f"    cmp qword ptr [rbx], {tag}", f"    jne {nxt}"]
                    for i, (fn, ft) in enumerate(fields):
                        if self.is_owned(ft):
                            o += [f"    mov rdi, [rbx+{8 * (1 + i)}]", f"    call {self.drop_label(ft)}"]
                    o += [f"{nxt}:"]
        o += ["    mov rdi, rbx", "    call rt_free", "    pop r13", "    pop r12", "    pop rbx", f"{lab}_end:", "    ret", ""]
        self.glue_asm.append("\n".join(o))
        return lab

    def copy_label(self, t):
        if t == TEXT:
            return "rt_copy_text"
        lab, fresh = self.new_glue("copy", t)
        if not fresh:
            return lab
        o = [f"{lab}:", "    push rbx", "    push r12", "    push r13", "    mov rbx, rdi"]
        if is_list(t):
            o += ["    mov rdi, [rbx]", "    call rt_list_new", "    mov r12, rax", "    xor r13d, r13d",
                  f"{lab}_loop:", "    cmp r13, [rbx]", f"    jae {lab}_done",
                  "    mov rcx, [rbx+16]", "    mov rsi, [rcx+r13*8]"]
            if self.is_owned(t[1]):
                o += ["    mov rdi, rsi", f"    call {self.copy_label(t[1])}", "    mov rsi, rax"]
            o += ["    mov rdi, r12", "    call rt_list_push", "    inc r13", f"    jmp {lab}_loop", f"{lab}_done:", "    mov rax, r12"]
        elif t[0] == "rec":
            fields = self.records[t[1]]
            o += [f"    mov rdi, {8 * max(1, len(fields))}", "    call rt_alloc", "    mov r12, rax"]
            for i, (fn, ft) in enumerate(fields):
                o += [f"    mov rdi, [rbx+{8 * i}]"]
                if self.is_owned(ft):
                    o += [f"    call {self.copy_label(ft)}"]
                else:
                    o += ["    mov rax, rdi"]
                o += [f"    mov [r12+{8 * i}], rax"]
            o += ["    mov rax, r12"]
        else:
            o += [f"    mov rdi, {self.csize[t[1]]}", "    call rt_alloc", "    mov r12, rax", "    mov rcx, [rbx]", "    mov [r12], rcx"]
            for vn in self.choices[t[1]]:
                _, tag, fields = self.variants[vn]
                if not fields:
                    continue
                nxt = f"{lab}_n{tag}"
                o += [f"    cmp qword ptr [rbx], {tag}", f"    jne {nxt}"]
                for i, (fn, ft) in enumerate(fields):
                    o += [f"    mov rdi, [rbx+{8 * (1 + i)}]"]
                    if self.is_owned(ft):
                        o += [f"    call {self.copy_label(ft)}"]
                    else:
                        o += ["    mov rax, rdi"]
                    o += [f"    mov [r12+{8 * (1 + i)}], rax"]
                o += [f"{nxt}:"]
            o += ["    mov rax, r12"]
        o += ["    pop r13", "    pop r12", "    pop rbx", "    ret", ""]
        self.glue_asm.append("\n".join(o))
        return lab

    # --- labels and data
    def lbl(self):
        self.n += 1
        return f".L{self.n}"

    def sl(self, s):
        """label of a static text value: [length][bytes]"""
        if s not in self.strs:
            self.strs[s] = f"S{len(self.strs)}"
        return self.strs[s]

    def e(self, s):
        self.ctx.emit(s)

    # --- scopes
    def lookup(self, name):
        for sc in reversed(self.ctx.scopes):
            if name in sc:
                return sc[name]
        return self.globals.get(name)

    def define(self, name, t, mutable, kind, store=True, borrowed=False, hidden=False):
        c = self.ctx
        top_owned = c.is_main and len(c.scopes) == 1 and not hidden and self.is_owned(t) and kind != "keep"
        if top_owned:
            self.main_owned.add(name)
        if c.is_main and len(c.scopes) == 1 and not hidden and not top_owned:
            lab = f"g{len(self.bss)}_{''.join(ch for ch in name if ch.isalnum())}"
            self.bss.append(lab)
            ent = ("G", lab, t, mutable, kind, Info(name, glob=True))
            self.globals[name] = ent
        else:
            ent = ("L", c.slot(), t, mutable, kind,
                   Info(name, owned=self.is_owned(t) and not borrowed, borrowed=borrowed))
        c.scopes[-1][name] = ent
        if store:
            self.store(ent)
        return ent

    def hidden(self, t):
        """an unnamed owned temporary (stored from rax) that is freed at the end of the scope"""
        self.n += 1
        return self.define(f"$t{self.n}", t, False, "so", hidden=True)

    def drop_scope(self, scope):
        for ent in reversed(list(scope.values())):
            if ent[5].owned:
                self.e(f"mov rdi, [rbp{ent[1]:+d}]" if ent[0] == "L" else f"mov rdi, [rbp+{ent[1]}]")
                self.e(f"call {self.drop_label(ent[2])}")

    def infos(self):
        out = []
        for sc in self.ctx.scopes:
            out += [e[5] for e in sc.values()]
        return out + [e[5] for e in self.globals.values()]

    def snap(self):
        return {id(i): (i, i.moved) for i in self.infos()}

    def restore(self, s):
        for i, m in s.values():
            i.moved = m

    @staticmethod
    def merge(states):
        out = {}
        for st in states:
            for k, (i, m) in st.items():
                if k not in out or (out[k][1] is None and m is not None):
                    out[k] = (i, m)
        return out

    def loop_check(self, s0, body):
        if always_gives(body):
            return
        for k, (i, m) in self.snap().items():
            if k in s0 and s0[k][1] is None and i.moved is not None:
                err(f"`{i.name}` is moved inside a loop, so it would be gone on the next turn", i.moved,
                    "lend it with `ref`, or move a `.copy()`")

    def is_fresh(self, n):
        """does this expression produce a brand new value that nobody else owns?"""
        k = n[0]
        if k in ("list", "str", "bin", "call"):
            return True
        if k == "name":
            return self.lookup(n[2]) is None and n[2] in self.variants
        return False

    def consume(self, n, t):
        """the value of expression n is being moved somewhere"""
        k = n[0]
        if k == "name":
            ent = self.lookup(n[2])
            if ent is None:
                return
            info = ent[5]
            if info.glob:
                err(f"`{n[2]}` is a global value, so it can't be moved", n[1],
                    f"lend it with `ref {n[2]}`, or use `{n[2]}.copy()`")
            if info.borrowed or info.mode:
                err(f"`{n[2]}` is only borrowed, so it can't be moved", n[1],
                    f"use `{n[2]}.copy()` to get a value of your own")
            info.moved = n[1]
            self.e(f"mov qword ptr [rbp{ent[1]:+d}], 0" if ent[0] == "L" else f"mov qword ptr [rbp+{ent[1]}], 0")
        elif k in ("member", "index"):
            err("can't move a value out of the thing that owns it", n[1],
                "borrow it with `ref ...`, or make a `.copy()`")

    def value(self, n, want=None):
        """compile an expression whose value is being given away (moved)"""
        t = self.expr(n, want)
        if self.is_owned(t):
            self.consume(n, t)
        return t

    def require_mutable(self, node, line, what):
        root = node
        while root[0] in ("member", "index"):
            root = root[2]
        if root[0] != "name":
            return
        ent = self.lookup(root[2])
        if ent is not None and not ent[3]:
            info = ent[5]
            if info.mode == "ref":
                err(f"`{root[2]}` is only borrowed with `ref`, so {what} can't change it", line,
                    "ask for `mut ref` to change it")
            if info.borrowed:
                err(f"`{root[2]}` is a borrowed value, so {what} can't change it", line)
            err(f"`{root[2]}` can't be changed, so neither can {what}", line,
                f"use `so var {root[2]} = ...`")

    def text_of(self, node, t, line):
        """turn the value in rax into text; True if the result is fresh and the caller must free it"""
        if t == TEXT:
            return self.is_fresh(node)
        if t == UNKNOWN:
            return False
        if self.is_owned(t) and self.is_fresh(node):
            self.e("push rax")
            self.to_text(t, line)
            self.e("mov rbx, rax")
            self.e("pop rdi")
            self.e(f"call {self.drop_label(t)}")
            self.e("mov rax, rbx")
        else:
            self.to_text(t, line)
        return True

    def cat_free(self, lf, rf):
        if lf:
            self.e("push rax")
            self.e("mov rdi, rbx")
            self.e("call rt_free")
            self.e("pop rax")
        if rf:
            self.e("push rax")
            self.e("mov rdi, r12")
            self.e("call rt_free")
            self.e("pop rax")

    def load(self, ent):
        if ent[0] == "L":
            self.e(f"mov rax, [rbp{ent[1]:+d}]")
        elif ent[0] == "P":
            self.e(f"mov rax, [rbp+{ent[1]}]")
            if ent[5].deref:
                self.e("mov rax, [rax]")
        else:
            self.e(f"mov rax, [rip+{ent[1]}]")

    def store(self, ent):
        if ent[0] == "L":
            self.e(f"mov [rbp{ent[1]:+d}], rax")
        elif ent[0] == "P":
            if ent[5].deref:
                self.e(f"mov rcx, [rbp+{ent[1]}]")
                self.e("mov [rcx], rax")
            else:
                self.e(f"mov [rbp+{ent[1]}], rax")
        else:
            self.e(f"mov [rip+{ent[1]}], rax")

    # ------------------------------------------------------------ functions
    def instantiate(self, name, argtypes, line):
        fdef = self.funs[name]
        key = (name, tuple(map(str, argtypes)))
        if key in self.rtypes:
            return self.rtypes[key], self.done[key]
        _, _, _, params, rt, body = fdef
        declared = parse_type(rt, line) if rt else None
        label = f"F{len(self.done)}_{''.join(ch for ch in name if ch.isalnum())}"
        if key in self.inprogress:
            return (declared or UNKNOWN), self.inprogress_label[key]
        self.check_params(name, params, argtypes, line)
        if declared is not None:
            self.rtypes[key], self.done[key] = declared, label
            self.queue.append((key, fdef, argtypes, label))
            return declared, label
        # dry run to infer the type this function gives
        self.inprogress.add(key)
        if not hasattr(self, "inprogress_label"):
            self.inprogress_label = {}
        self.inprogress_label[key] = label
        ctx = self.compile_fun(fdef, argtypes, label, dry=True, rtype=None)
        self.inprogress.discard(key)
        known = [t for t in ctx.gives if t != UNKNOWN]
        if known:
            rtype = known[0]
        elif ctx.gives:
            err(f"I can't work out what `{name}` gives", fdef[1],
                f"write it explicitly: `fun {name}(...) gives whole`")
        else:
            rtype = NOTHING
        self.rtypes[key], self.done[key] = rtype, label
        self.queue.append((key, fdef, argtypes, label))
        return rtype, label

    def check_params(self, name, params, argtypes, line):
        if len(params) != len(argtypes):
            err(f"`{name}` needs {len(params)} value(s) but got {len(argtypes)}", line)
        for i, ((pn, pt), at) in enumerate(zip(params, argtypes)):
            kind, tstr = param_kind(pn, pt, i == 0)
            if tstr is not None and not tstr.startswith("@"):
                want = parse_type(tstr, line)
                if at != UNKNOWN and at != want:
                    err(f"`{pn}` should be {tname(want)} but got {tname(at)}", line)

    def compile_fun(self, fdef, argtypes, label, dry, rtype):
        _, fline, name, params, _, body = fdef
        ctx = Ctx(name, label, dry)
        ctx.rtype = rtype
        n = len(params)
        for i, ((pn, _), at) in enumerate(zip(params, argtypes)):
            kind, _ = param_kind(pn, params[i][1], i == 0)
            if kind == "val":
                ent = ("P", 16 + 8 * (n - 1 - i), at, False, "so", Info(pn, owned=self.is_owned(at)))
            else:
                info = Info(pn, borrowed=True, mode=kind)
                info.deref = kind == "mut" and not self.is_owned(at) and at != UNKNOWN
                ent = ("P", 16 + 8 * (n - 1 - i), at, kind == "mut", "so", info)
            ctx.scopes[0][pn] = ent
        old, self.ctx = self.ctx, ctx
        try:
            ret = self.lbl()
            ctx.ret = ret
            for s in body[:-1]:
                self.stmt(s)
            if body:
                last = body[-1]
                if last[0] == "expr":
                    t = self.value(last[2])
                    if t != NOTHING:
                        ctx.implicit = True
                        self.do_give(t, last[1])
                else:
                    self.stmt(last)
            if (not dry and ctx.rtype not in (NOTHING, UNKNOWN, None)
                    and not (always_gives(body) or ctx.implicit)):
                err(f"`{name}` must give a value on every path", fline,
                    "add a `give` at the end, or an `else` branch that gives")
            self.drop_scope(ctx.scopes[0])
            ctx.mark(ret)
        finally:
            self.ctx = old
        return ctx

    def do_give(self, t, line):
        c = self.ctx
        if c.is_main:
            err("`give` only works inside a function", line)
        if c.rtype is None:  # inferring
            known = [x for x in c.gives if x != UNKNOWN]
            if known and t != UNKNOWN and t != known[0]:
                err(f"this gives {tname(t)} but an earlier `give` gave {tname(known[0])}", line)
            c.gives.append(t)
        elif not lenient(t, c.rtype) and t != c.rtype:
            err(f"`{c.name}` should give {tname(c.rtype)} but this gives {tname(t)}", line)
        self.e("push rax")
        for sc in reversed(c.scopes):
            self.drop_scope(sc)
        self.e("pop rax")
        self.e(f"jmp {c.ret}")

    def finish_fun(self, ctx):
        out = [f"{ctx.label}:", "    push rbp", "    mov rbp, rsp", f"    sub rsp, {8 * ctx.slots}"]
        out += ctx.code
        out += ["    mov rsp, rbp", "    pop rbp", "    ret", ""]
        return "\n".join(out)

    # ------------------------------------------------------------ program
    def program(self, prog):
        USER.clear()
        EXH.clear()
        for s in prog:
            if s[0] in ("record", "choice"):
                if s[2] in USER or s[2] in ("whole", "bool", "text", "nothing", "list", "i64"):
                    err(f"the type name `{s[2]}` is already used", s[1])
                USER[s[2]] = "rec" if s[0] == "record" else "choice"
        for s in prog:
            if s[0] == "record":
                self.records[s[2]] = [(f, parse_type(t, s[1])) for f, t in s[3]]
            elif s[0] == "choice":
                names = []
                for tag, (vn, fields) in enumerate(s[3]):
                    if vn in self.variants or vn in USER:
                        err(f"the name `{vn}` is already used", s[1])
                    fl = [(f, parse_type(t, s[1])) for f, t in fields]
                    self.variants[vn] = (s[2], tag, fl)
                    names.append(vn)
                self.choices[s[2]] = names
                self.csize[s[2]] = 8 * (1 + max([len(self.variants[v][2]) for v in names] + [0]))
        for s in prog:
            if s[0] == "fun":
                if s[2] in self.funs:
                    err(f"`{s[2]}` is already defined", s[1])
                self.funs[s[2]] = s
            elif s[0] == "extend":
                if s[2] not in USER:
                    err(f"I don't know the type `{s[2]}`", s[1], "declare it with `record` or `choice` first")
                for f in s[3]:
                    key = f"{s[2]}.{f[2]}"
                    if key in self.funs:
                        err(f"`{key}` is already defined", f[1])
                    self.funs[key] = f
        main = Ctx("main", "fish_main", False, is_main=True)
        main.ret = self.lbl()
        self.ctx = main
        for s in prog:
            if s[0] in ("record", "choice", "extend"):
                continue
            if s[0] == "fun":
                params = s[3]
                if all(pt is not None and not pt.startswith("@") for _, pt in params):  # type check eagerly
                    ats = [parse_type(param_kind(pn, pt, i == 0)[1], s[1]) for i, (pn, pt) in enumerate(params)]
                    self.instantiate(s[2], ats, s[1])
            else:
                self.stmt(s)
        self.drop_scope(main.scopes[0])
        main.mark(main.ret)
        main_asm = self.finish_fun(main)
        while self.queue:
            key, fdef, ats, label = self.queue.pop()
            ctx = self.compile_fun(fdef, ats, label, dry=False, rtype=self.rtypes[key])
            self.fun_asm.append(self.finish_fun(ctx))
        return main_asm

    # ------------------------------------------------------------ statements
    def stmt(self, n):
        k, line = n[0], n[1]
        getattr(self, "st_" + k)(n)

    def block(self, stmts):
        self.ctx.scopes.append({})
        for s in stmts:
            self.stmt(s)
        self.drop_scope(self.ctx.scopes[-1])
        self.ctx.scopes.pop()

    def st_record(self, n):
        err("`record` must be declared at the top level of the file", n[1])

    st_choice = st_extend = st_record

    def st_fun(self, n):
        err("functions must be declared at the top level of the file for now", n[1])

    def st_so(self, n):
        _, line, kind, name, ty, val = n
        want = parse_type(ty, line) if ty else None
        t = self.value(val, want)
        if t == NOTHING:
            err("this has no value to store", line)
        if want and t != UNKNOWN and t != want:
            err(f"`{name}` should be {tname(want)} but got {tname(t)}", line)
        self.define(name, want or t, kind == "var", kind)

    def st_say(self, n):
        for i, item in enumerate(n[2]):
            if i:
                self.e("call rt_print_space")
            t = self.expr(item)
            fresh = self.text_of(item, t, n[1])
            self.e("mov rbx, rax")
            self.e("mov rdi, rax")
            self.e("call rt_print_text")
            if fresh:
                self.e("mov rdi, rbx")
                self.e("call rt_free")
        self.e("call rt_print_nl")

    def st_expr(self, n):
        t = self.expr(n[2])
        if self.is_owned(t) and self.is_fresh(n[2]):
            self.e("mov rdi, rax")
            self.e(f"call {self.drop_label(t)}")

    def st_give(self, n):
        t = NOTHING if n[2] is None else self.value(n[2])
        self.do_give(t, n[1])

    def st_break(self, n):
        if not self.ctx.loops:
            err("`break` only works inside a loop", n[1])
        cont, end, depth = self.ctx.loops[-1]
        for sc in reversed(self.ctx.scopes[depth:]):
            self.drop_scope(sc)
        self.e(f"jmp {end}")

    def st_continue(self, n):
        if not self.ctx.loops:
            err("`continue` only works inside a loop", n[1])
        cont, end, depth = self.ctx.loops[-1]
        for sc in reversed(self.ctx.scopes[depth:]):
            self.drop_scope(sc)
        self.e(f"jmp {cont}")

    def cond(self, node, what, line):
        t = self.expr(node)
        if t not in (BOOL, UNKNOWN):
            err(f"{what} must be true or false, but this is {tname(t)}", line,
                "use a comparison such as `is above` or `is not`")

    def st_if(self, n):
        end = self.lbl()
        results = []
        for cnd, body in n[2]:
            nxt = self.lbl()
            self.cond(cnd, "an `if` condition", n[1])
            self.e("test al, al")
            self.e(f"jz {nxt}")
            cur = self.snap()
            self.block(body)
            if not always_gives(body):
                results.append(self.snap())
            self.restore(cur)
            self.e(f"jmp {end}")
            self.ctx.mark(nxt)
        cur = self.snap()
        if n[3] is not None:
            self.block(n[3])
            if not always_gives(n[3]):
                results.append(self.snap())
        else:
            results.append(cur)
        self.restore(self.merge(results) if results else cur)
        self.ctx.mark(end)

    def st_while(self, n):
        top, end = self.lbl(), self.lbl()
        self.ctx.mark(top)
        self.cond(n[2], "a `while` condition", n[1])
        self.e("test al, al")
        self.e(f"jz {end}")
        s0 = self.snap()
        self.ctx.loops.append((top, end, len(self.ctx.scopes)))
        self.block(n[3])
        self.ctx.loops.pop()
        self.loop_check(s0, n[3])
        self.restore(s0)
        self.e(f"jmp {top}")
        self.ctx.mark(end)

    def st_loop(self, n):
        top, end = self.lbl(), self.lbl()
        self.ctx.mark(top)
        s0 = self.snap()
        self.ctx.loops.append((top, end, len(self.ctx.scopes)))
        self.block(n[2])
        self.ctx.loops.pop()
        self.loop_check(s0, n[2])
        self.restore(s0)
        self.e(f"jmp {top}")
        self.ctx.mark(end)

    def whole_expr(self, node, what, line):
        t = self.expr(node)
        if t not in (WHOLE, UNKNOWN):
            err(f"{what} must be a whole number, but this is {tname(t)}", line)

    def st_for(self, n):
        _, line, names, itn, body = n
        c = self.ctx
        top, cont, end = self.lbl(), self.lbl(), self.lbl()
        if itn[0] == "range":
            _, rl, lo, hi, incl, step, down = itn
            if len(names) != 1:
                err("a range has no item numbers; use one name", line)
            cnt, hs, ss = c.slot(), c.slot(), c.slot()
            self.whole_expr(lo, "the start of a range", rl)
            self.e(f"mov [rbp{cnt:+d}], rax")
            self.whole_expr(hi, "the end of a range", rl)
            self.e(f"mov [rbp{hs:+d}], rax")
            if step is None:
                self.e("mov rax, 1")
            else:
                self.whole_expr(step, "`step`", rl)
            self.e("test rax, rax")
            ok = self.lbl()
            self.e(f"jg {ok}")
            self.e(f"mov rdx, {rl}")
            self.e("jmp rt_fail_step")
            c.mark(ok)
            self.e(f"mov [rbp{ss:+d}], rax")
            c.mark(top)
            self.e(f"mov rax, [rbp{cnt:+d}]")
            self.e(f"cmp rax, [rbp{hs:+d}]")
            if down:
                self.e(f"jl {end}")  # inclusive only
            else:
                self.e(f"{'jg' if incl else 'jge'} {end}")
            s0 = self.snap()
            c.loops.append((cont, end, len(c.scopes)))
            c.scopes.append({})
            self.define(names[0], WHOLE, False, "so")
            for s in body:
                self.stmt(s)
            self.drop_scope(c.scopes[-1])
            c.loops.pop()
            c.scopes.pop()
            self.loop_check(s0, body)
            self.restore(s0)
            c.mark(cont)
            self.e(f"mov rax, [rbp{cnt:+d}]")
            self.e(f"{'sub' if down else 'add'} rax, [rbp{ss:+d}]")
            self.e(f"jo {end}")
            self.e(f"mov [rbp{cnt:+d}], rax")
            self.e(f"jmp {top}")
            c.mark(end)
            return
        t = self.expr(itn)
        if t == UNKNOWN:
            et = UNKNOWN
        elif is_list(t):
            et = t[1]
        else:
            err(f"I can only loop over a list or a range here, not {tname(t)}", line,
                "looping over text is not supported by the native compiler yet")
        temp_scope = False
        if self.is_owned(t) and self.is_fresh(itn):
            c.scopes.append({})
            self.hidden(t)
            temp_scope = True
        lst, idx, ln = c.slot(), c.slot(), c.slot()
        self.e(f"mov [rbp{lst:+d}], rax")
        self.e("mov rax, [rax]")
        self.e(f"mov [rbp{ln:+d}], rax")
        self.e(f"mov qword ptr [rbp{idx:+d}], 0")
        c.mark(top)
        self.e(f"mov rax, [rbp{idx:+d}]")
        self.e(f"cmp rax, [rbp{ln:+d}]")
        self.e(f"jge {end}")
        c.scopes.append({})
        if len(names) == 2:
            self.e(f"mov rax, [rbp{idx:+d}]")
            self.define(names[0], WHOLE, False, "so")
        self.e(f"mov rcx, [rbp{lst:+d}]")
        self.e("mov rcx, [rcx+16]")
        self.e(f"mov rax, [rbp{idx:+d}]")
        self.e("mov rax, [rcx+rax*8]")
        self.define(names[-1], et, False, "so", borrowed=True)
        s0 = self.snap()
        c.loops.append((cont, end, len(c.scopes) - 1))
        for s in body:
            self.stmt(s)
        self.drop_scope(c.scopes[-1])
        c.loops.pop()
        c.scopes.pop()
        self.loop_check(s0, body)
        self.restore(s0)
        c.mark(cont)
        self.e(f"inc qword ptr [rbp{idx:+d}]")
        self.e(f"jmp {top}")
        c.mark(end)
        if temp_scope:
            self.drop_scope(c.scopes[-1])
            c.scopes.pop()

    def pat_binds(self, p, ty, out):
        if p[0] == "bind":
            if p[1] != "_":
                out.append((p[1], ty))
        elif p[0] == "variant":
            vn, subs, line = p[1], p[2], p[3]
            if vn not in self.variants:
                err(f"I don't know a choice called `{vn}`", line)
            cn, tag, fields = self.variants[vn]
            if ty != UNKNOWN and ty != ("choice", cn):
                err(f"`{vn}` belongs to `{cn}`, but the value is {tname(ty)}", line)
            if len(subs) != len(fields):
                err(f"`{vn}` has {len(fields)} value(s), but this pattern has {len(subs)}", line)
            for sp, (fn, ft) in zip(subs, fields):
                self.pat_binds(sp, ft, out)

    def pat_code(self, p, ty, vs, fail, ents, line):
        """test the value in stack slot `vs` against pattern p; jump to `fail` if no match"""
        c = self.ctx
        if p[0] == "bind":
            if p[1] != "_":
                self.e(f"mov rax, [rbp{vs:+d}]")
                self.store(ents[p[1]])
        elif p[0] == "lit":
            if ty not in (WHOLE, BOOL, TEXT, UNKNOWN):
                err(f"a plain value pattern can't match {tname(ty)}", line)
            lt = self.expr(p[1])
            if not lenient(lt, ty) and lt != ty:
                err(f"this pattern is {tname(lt)} but the value is {tname(ty)}", line)
            self.e(f"mov rcx, [rbp{vs:+d}]")
            if ty == TEXT:
                self.e("mov rsi, rax")
                self.e("mov rdi, rcx")
                self.e("call rt_text_eq")
                self.e("test al, al")
                self.e(f"jz {fail}")
            else:
                self.e("cmp rcx, rax")
                self.e(f"jne {fail}")
        elif p[0] == "range":
            if ty not in (WHOLE, UNKNOWN):
                err("range patterns need a whole number value", line)
            self.whole_expr(p[1], "a pattern range", line)
            self.e("push rax")
            self.whole_expr(p[2], "a pattern range", line)
            self.e("pop rcx")
            self.e(f"mov rdx, [rbp{vs:+d}]")
            self.e("cmp rdx, rcx")
            self.e(f"jl {fail}")
            self.e("cmp rdx, rax")
            self.e(f"{'jg' if p[3] else 'jge'} {fail}")
        else:
            cn, tag, fields = self.variants[p[1]]
            self.e(f"mov rcx, [rbp{vs:+d}]")
            self.e(f"cmp qword ptr [rcx], {tag}")
            self.e(f"jne {fail}")
            for i, (sp, (fn, ft)) in enumerate(zip(p[2], fields)):
                if sp[0] == "bind" and sp[1] == "_":
                    continue
                slot = c.slot()
                self.e(f"mov rcx, [rbp{vs:+d}]")
                self.e(f"mov rax, [rcx+{8 * (1 + i)}]")
                self.e(f"mov [rbp{slot:+d}], rax")
                self.pat_code(sp, ft, slot, fail, ents, line)

    def irrefutable(self, p):
        if p[0] == "bind":
            return True
        return p[0] == "variant" and all(self.irrefutable(x) for x in p[2])

    def st_match(self, n):
        _, line, subj, arms, orelse = n
        c = self.ctx
        st = self.expr(subj)
        is_choice = isinstance(st, tuple) and st[0] == "choice"
        if st not in (WHOLE, BOOL, TEXT, UNKNOWN) and not is_choice:
            err(f"`match` can look at whole, bool, text or choice values, not {tname(st)}", line)
        temp_scope = False
        if self.is_owned(st) and self.is_fresh(subj):
            c.scopes.append({})
            self.hidden(st)
            temp_scope = True
        sslot = c.slot()
        self.e(f"mov [rbp{sslot:+d}], rax")
        end = self.lbl()
        covered, catch_all = set(), False
        s0 = self.snap()
        results = []
        for pats, guard, body in arms:
            self.restore(s0)
            c.scopes.append({})
            nxt, gl = self.lbl(), self.lbl()
            binds = []
            for p in pats:
                self.pat_binds(p, st, binds)
            ents = {}
            for name, ty in binds:
                if name in ents and ents[name][2] != ty:
                    err(f"`{name}` has different types in the alternatives of this `when`", line)
                if name not in ents:
                    ents[name] = self.define(name, ty, False, "so", store=False, borrowed=True)
            for p in pats:
                np_ = self.lbl()
                self.pat_code(p, st, sslot, np_, ents, line)
                self.e(f"jmp {gl}")
                c.mark(np_)
                if guard is None and self.irrefutable(p):
                    if p[0] == "bind":
                        catch_all = True
                    else:
                        covered.add(p[1])
            self.e(f"jmp {nxt}")
            c.mark(gl)
            if guard is not None:
                self.cond(guard, "a `when … if` guard", line)
                self.e("test al, al")
                self.e(f"jz {nxt}")
            for s in body:
                self.stmt(s)
            if not always_gives(body):
                results.append(self.snap())
            self.drop_scope(c.scopes[-1])
            self.e(f"jmp {end}")
            c.mark(nxt)
            c.scopes.pop()
        self.restore(s0)
        if orelse is not None:
            self.block(orelse)
            if not always_gives(orelse):
                results.append(self.snap())
        else:
            results.append(s0)
        self.restore(self.merge(results))
        if orelse is not None:
            pass
        elif is_choice:
            missing = [v for v in self.choices[st[1]] if v not in covered]
            if missing and not catch_all:
                err(f"this `match` doesn't cover: {', '.join(missing)}", line,
                    "add a `when` for each one, or an `else`")
            EXH.add(id(n))
        c.mark(end)
        if temp_scope:
            self.drop_scope(c.scopes[-1])
            c.scopes.pop()

    def st_assign(self, n):
        _, line, target, op, rhs = n
        if op != "=":
            rhs = ("bin", line, op[0], target, rhs)
        if target[0] == "name":
            name = target[2]
            ent = self.lookup(name)
            if ent is None:
                err(f"I don't know the name `{name}`", line,
                    f"create it first with `so var {name} = ...`")
            if not ent[3]:
                how = {"so": f"it was made with `so`; use `so var {name} = ...` if it should change",
                       "keep": "`keep` values never change"}.get(ent[4], "it can't change")
                if ent[5].mode == "ref":
                    how = "it is only borrowed with `ref`; ask for `mut ref` to change it"
                elif ent[5].borrowed:
                    how = "it is a borrowed value"
                err(f"`{name}` can't be changed: {how}", line)
            if ent[5].mode == "mut" and self.is_owned(ent[2]):
                err(f"`{name}` is borrowed with `mut ref`, so it can't be replaced as a whole", line,
                    "change its parts instead (fields, items, `.push`)")
            t = self.value(rhs, ent[2])
            if not lenient(t, ent[2]) and t != ent[2]:
                err(f"`{name}` holds {tname(ent[2])}, but this is {tname(t)}", line)
            if self.is_owned(ent[2]):
                self.e("push rax")
                self.load(ent)
                self.e("mov rdi, rax")
                self.e(f"call {self.drop_label(ent[2])}")
                self.e("pop rax")
            self.store(ent)
            ent[5].moved = None
            return
        if target[0] == "member":
            self.require_mutable(target, line, f"its `{target[3]}`")
            rt_ = self.expr(target[2])
            if not (isinstance(rt_, tuple) and rt_[0] == "rec"):
                err(f"{tname(rt_)} has no fields you can change", line)
            fl = self.records[rt_[1]]
            hit = [(i, ft) for i, (fn, ft) in enumerate(fl) if fn == target[3]]
            if not hit:
                err(f"{rt_[1]} has no field `{target[3]}`", line, "its fields: " + ", ".join(f for f, _ in fl))
            self.e("push rax")
            t = self.value(rhs, hit[0][1])
            if not lenient(t, hit[0][1]) and t != hit[0][1]:
                err(f"`{target[3]}` holds {tname(hit[0][1])}, but this is {tname(t)}", line)
            self.e("pop rcx")
            self.e(f"mov rdx, [rcx+{8 * hit[0][0]}]")
            self.e(f"mov [rcx+{8 * hit[0][0]}], rax")
            if self.is_owned(hit[0][1]):
                self.e("mov rdi, rdx")
                self.e(f"call {self.drop_label(hit[0][1])}")
            return
        self.require_mutable(target, line, "its items")
        lt = self.expr(target[2])
        if lt != UNKNOWN and not is_list(lt):
            err("only lists have items you can change", line)
        self.e("push rax")
        self.whole_expr(target[3], "an item number", line)
        self.e("push rax")
        et = UNKNOWN if lt == UNKNOWN else lt[1]
        t = self.value(rhs, et)
        if not lenient(t, et) and t != et:
            err(f"this list holds {tname(et)}, but this is {tname(t)}", line)
        self.e("pop rcx")
        self.e("pop rdx")
        self.bounds(line)
        self.e("mov rdx, [rdx+16]")
        self.e("lea rdx, [rdx+rcx*8]")
        self.e("mov rdi, [rdx]")
        self.e("mov [rdx], rax")
        if self.is_owned(et):
            self.e(f"call {self.drop_label(et)}")

    def bounds(self, line):
        """rcx = item number, rdx = list; fails if outside"""
        ok = self.lbl()
        self.e("cmp rcx, [rdx]")
        self.e(f"jb {ok}")
        self.e("mov rdi, rcx")
        self.e("mov rsi, [rdx]")
        self.e(f"mov rdx, {line}")
        self.e("jmp rt_fail_index")
        self.ctx.mark(ok)

    # ------------------------------------------------------------ expressions
    def to_text(self, t, line):
        if t == TEXT or t == UNKNOWN:
            return
        if t == WHOLE:
            self.e("mov rdi, rax")
            self.e("call rt_int_to_text")
        elif t == BOOL:
            self.e("test al, al")
            self.e(f"lea rax, [rip+{self.sl('false')}]")
            self.e(f"lea rcx, [rip+{self.sl('true')}]")
            self.e("cmovnz rax, rcx")
        elif t == DECIMAL:
            self.e("mov rdi, rax")
            self.e("call rt_dec_to_text")
        elif t == NOTHING:
            err("this has no value to show", line)
        elif is_list(t):
            self.list_text(t[1], line)
        elif t[0] == "rec":
            self.rec_text(t[1], line)
        else:
            self.choice_text(t[1], line)

    def cat_static(self, acc, text):
        self.e(f"mov rdi, [rbp{acc:+d}]")
        self.e(f"lea rsi, [rip+{self.sl(text)}]")
        self.e("call rt_concat")
        self.e(f"mov [rbp{acc:+d}], rax")

    def cat_rax(self, acc):
        self.e("mov rsi, rax")
        self.e(f"mov rdi, [rbp{acc:+d}]")
        self.e("call rt_concat")
        self.e(f"mov [rbp{acc:+d}], rax")

    def fields_text(self, p, acc, fields, first, line):
        for i, (fn, ft) in enumerate(fields):
            self.cat_static(acc, (", " if i else "") + fn + ": ")
            self.e(f"mov rcx, [rbp{p:+d}]")
            self.e(f"mov rax, [rcx+{8 * (first + i)}]")
            self.to_text(ft, line)
            if ft == TEXT:
                self.e("push rax")
                self.cat_static(acc, '"')
                self.e("pop rax")
                self.cat_rax(acc)
                self.cat_static(acc, '"')
            else:
                self.cat_rax(acc)

    def rec_text(self, name, line):
        c = self.ctx
        p, acc = c.slot(), c.slot()
        self.e(f"mov [rbp{p:+d}], rax")
        self.e(f"lea rax, [rip+{self.sl(name + '(')}]")
        self.e(f"mov [rbp{acc:+d}], rax")
        self.fields_text(p, acc, self.records[name], 0, line)
        self.cat_static(acc, ")")
        self.e(f"mov rax, [rbp{acc:+d}]")

    def choice_text(self, cname, line):
        c = self.ctx
        p, acc = c.slot(), c.slot()
        end = self.lbl()
        self.e(f"mov [rbp{p:+d}], rax")
        for vn in self.choices[cname]:
            _, tag, fields = self.variants[vn]
            nxt = self.lbl()
            self.e(f"mov rcx, [rbp{p:+d}]")
            self.e(f"cmp qword ptr [rcx], {tag}")
            self.e(f"jne {nxt}")
            self.e(f"lea rax, [rip+{self.sl(vn + ('(' if fields else ''))}]")
            self.e(f"mov [rbp{acc:+d}], rax")
            self.fields_text(p, acc, fields, 1, line)
            if fields:
                self.cat_static(acc, ")")
            self.e(f"jmp {end}")
            c.mark(nxt)
        c.mark(end)
        self.e(f"mov rax, [rbp{acc:+d}]")

    def list_text(self, et, line):
        c = self.ctx
        lst, idx, acc = c.slot(), c.slot(), c.slot()
        top, first, end = self.lbl(), self.lbl(), self.lbl()
        self.e(f"mov [rbp{lst:+d}], rax")
        self.e(f"mov qword ptr [rbp{idx:+d}], 0")
        self.e(f"lea rax, [rip+{self.sl('[')}]")
        self.e(f"mov [rbp{acc:+d}], rax")
        c.mark(top)
        self.e(f"mov rax, [rbp{idx:+d}]")
        self.e(f"mov rcx, [rbp{lst:+d}]")
        self.e("cmp rax, [rcx]")
        self.e(f"jge {end}")
        self.e("test rax, rax")
        self.e(f"jz {first}")
        self.e(f"mov rdi, [rbp{acc:+d}]")
        self.e(f"lea rsi, [rip+{self.sl(', ')}]")
        self.e("call rt_concat")
        self.e(f"mov [rbp{acc:+d}], rax")
        c.mark(first)
        self.e(f"mov rcx, [rbp{lst:+d}]")
        self.e(f"mov rax, [rbp{idx:+d}]")
        self.e("mov rdx, [rcx+16]")
        self.e("mov rax, [rdx+rax*8]")
        self.to_text(et, line)
        if et == TEXT:
            q = self.sl('"')
            self.e("push rax")
            self.e(f"mov rdi, [rbp{acc:+d}]")
            self.e(f"lea rsi, [rip+{q}]")
            self.e("call rt_concat")
            self.e("mov rdi, rax")
            self.e("pop rsi")
            self.e("call rt_concat")
            self.e("mov rdi, rax")
            self.e(f"lea rsi, [rip+{q}]")
            self.e("call rt_concat")
        else:
            self.e("mov rsi, rax")
            self.e(f"mov rdi, [rbp{acc:+d}]")
            self.e("call rt_concat")
        self.e(f"mov [rbp{acc:+d}], rax")
        self.e(f"inc qword ptr [rbp{idx:+d}]")
        self.e(f"jmp {top}")
        c.mark(end)
        self.e(f"mov rdi, [rbp{acc:+d}]")
        self.e(f"lea rsi, [rip+{self.sl(']')}]")
        self.e("call rt_concat")

    def expr(self, n, want=None):
        k, line = n[0], n[1]
        return getattr(self, "ex_" + k)(n, want) if k != "name" else self.ex_name(n, want)

    def ex_num(self, n, want):
        if isinstance(n[2], float):
            import struct
            self.e(f"mov rax, {struct.unpack('<q', struct.pack('<d', n[2]))[0]}")
            return DECIMAL
        self.e(f"mov rax, {n[2]}")
        return WHOLE

    def ex_bool(self, n, want):
        self.e(f"mov rax, {1 if n[2] else 0}")
        return BOOL

    def ex_none(self, n, want):
        err("`none` (maybe values) is not supported by the native compiler yet", n[1])

    def ex_str(self, n, want):
        parts = n[2]
        if not parts:
            self.e(f"lea rax, [rip+{self.sl('')}]")
            return TEXT
        acc_fresh = False
        for i, p in enumerate(parts):
            if i:
                self.e("push rax")
            if p[0] == "t":
                self.e(f"lea rax, [rip+{self.sl(p[1])}]")
                pfresh = False
            else:
                t = self.expr(p[1])
                pfresh = self.text_of(p[1], t, n[1])
            if i:
                self.e("mov rsi, rax")
                self.e("pop rdi")
                if acc_fresh or pfresh:
                    self.e("mov rbx, rdi")
                    self.e("mov r12, rsi")
                self.e("call rt_concat")
                self.cat_free(acc_fresh, pfresh)
                acc_fresh = True
            else:
                acc_fresh = pfresh
        if len(parts) == 1 and parts[0][0] == "e" and not acc_fresh:
            self.e("mov rdi, rax")
            self.e("call rt_copy_text")
        return TEXT

    def ex_name(self, n, want):
        ent = self.lookup(n[2])
        if ent is None and n[2] in self.variants:
            cn, tag, fields = self.variants[n[2]]
            if fields:
                err(f"`{n[2]}` needs {len(fields)} value(s)", n[1], f"write `{n[2]}(...)`")
            self.e(f"mov rdi, {self.csize[cn]}")
            self.e("call rt_alloc")
            self.e(f"mov qword ptr [rax], {tag}")
            return ("choice", cn)
        if ent is None:
            if n[2] in self.funs or n[2] in BUILTINS:
                err(f"`{n[2]}` is a function; call it with parentheses", n[1])
            if n[2] in self.main_owned and not self.ctx.is_main:
                err(f"`{n[2]}` belongs to the main program, so a function can't see it", n[1],
                    f"pass it in: `{n[2]}: ref ...` as a parameter and `ref {n[2]}` at the call")
            names = [x for sc in self.ctx.scopes for x in sc] + list(self.globals)
            import difflib
            close = difflib.get_close_matches(n[2], names, n=1)
            err(f"I don't know the name `{n[2]}`", n[1],
                f"did you mean `{close[0]}`?" if close else None)
        if ent[5].moved is not None:
            err(f"`{n[2]}` was moved on line {ent[5].moved}", n[1],
                f"lend it with `ref {n[2]}`, or move a `.copy()` instead")
        self.load(ent)
        return ent[2]

    def ex_list(self, n, want):
        items = n[2]
        et = want[1] if is_list(want) else None
        if not items:
            if et is None:
                err("I can't tell what this empty list will hold", n[1],
                    "give it a type: `so items: list of whole = []`")
            self.e("mov rdi, 4")
            self.e("call rt_list_new")
            return ("list", et)
        self.e(f"mov rdi, {max(4, len(items))}")
        self.e("call rt_list_new")
        for it in items:
            self.e("push rax")
            t = self.value(it, et)
            if t == NOTHING:
                err("this has no value to put in a list", n[1])
            if et is None:
                et = t
            elif not lenient(t, et) and t != et:
                err(f"this list holds {tname(et)}, but this item is {tname(t)}", n[1])
            self.e("mov rsi, rax")
            self.e("mov rdi, [rsp]")
            self.e("call rt_list_push")
            self.e("pop rax")
        return ("list", et)

    def ex_neg(self, n, want):
        t = self.expr(n[2])
        if t == DECIMAL:
            self.e("btc rax, 63")
            return DECIMAL
        if t not in (WHOLE, UNKNOWN):
            err(f"`-` needs a number, but got {tname(t)}", n[1])
        self.e("neg rax")
        return WHOLE

    def ex_not(self, n, want):
        t = self.expr(n[2])
        if t not in (BOOL, UNKNOWN):
            err(f"`not` needs true or false, but got {tname(t)}", n[1])
        self.e("xor rax, 1")
        return BOOL

    def ex_and(self, n, want):
        return self.shortcircuit(n, "jz")

    def ex_or(self, n, want):
        return self.shortcircuit(n, "jnz")

    def shortcircuit(self, n, jump):
        word = "and" if jump == "jz" else "or"
        end = self.lbl()
        t = self.expr(n[2])
        if t not in (BOOL, UNKNOWN):
            err(f"`{word}` needs true or false, but got {tname(t)}", n[1])
        self.e("test al, al")
        self.e(f"{jump} {end}")
        t = self.expr(n[3])
        if t not in (BOOL, UNKNOWN):
            err(f"`{word}` needs true or false, but got {tname(t)}", n[1])
        self.ctx.mark(end)
        return BOOL

    def overflow_check(self, line):
        ok = self.lbl()
        self.e(f"jno {ok}")
        self.e(f"mov rdx, {line}")
        self.e("jmp rt_fail_overflow")
        self.ctx.mark(ok)

    def ex_bin(self, n, want):
        _, line, op, ln, rn = n
        lt = self.expr(ln)
        self.e("push rax")
        rt = self.expr(rn)
        if lt == UNKNOWN and rt == UNKNOWN:
            self.e("pop rcx")
            return UNKNOWN
        if op == "+" and TEXT in (lt, rt):
            if lt not in (TEXT, UNKNOWN) or rt not in (TEXT, UNKNOWN):
                err(f"can't add {tname(lt)} and {tname(rt)}", line,
                    'put values inside text with braces: "{a} and {b}"')
            lf, rf = self.is_fresh(ln), self.is_fresh(rn)
            self.e("mov rsi, rax")
            self.e("pop rdi")
            if lf or rf:
                self.e("mov rbx, rdi")
                self.e("mov r12, rsi")
            self.e("call rt_concat")
            self.cat_free(lf, rf)
            return TEXT
        if isinstance(lt, tuple) or isinstance(rt, tuple):
            err(f"can't use `{op}` on {tname(lt)} and {tname(rt)}", line)
        if DECIMAL in (lt, rt):
            for t in (lt, rt):
                if t not in (DECIMAL, UNKNOWN):
                    err(f"can't mix {tname(lt)} and {tname(rt)}", line,
                        "convert one side with decimal(x) or whole(x)")
            if op not in ("+", "-", "*", "/"):
                err(f"`{op}` on decimals is not supported by the native compiler yet", line,
                    "use `python3 fish.py run` for now")
            self.e("pop rcx")
            if UNKNOWN in (lt, rt):
                return DECIMAL
            self.e("movq xmm0, rcx")
            self.e("movq xmm1, rax")
            if op == "/":
                ok = self.lbl()
                self.e("xorpd xmm2, xmm2")
                self.e("ucomisd xmm1, xmm2")
                self.e(f"jp {ok}")
                self.e(f"jne {ok}")
                self.e(f"mov rdx, {line}")
                self.e("jmp rt_fail_div")
                self.ctx.mark(ok)
            self.e({"+": "addsd", "-": "subsd", "*": "mulsd", "/": "divsd"}[op] + " xmm0, xmm1")
            self.e("movq rax, xmm0")
            return DECIMAL
        for t in (lt, rt):
            if t not in (WHOLE, UNKNOWN):
                err(f"`{op}` needs whole numbers, but got {tname(t)}", line)
        self.e("pop rcx")
        if op == "+":
            self.e("add rcx, rax")
            self.overflow_check(line)
            self.e("mov rax, rcx")
        elif op == "-":
            self.e("sub rcx, rax")
            self.overflow_check(line)
            self.e("mov rax, rcx")
        elif op == "*":
            self.e("imul rcx, rax")
            self.overflow_check(line)
            self.e("mov rax, rcx")
        elif op in ("/", "%"):
            ok, normal, done = self.lbl(), self.lbl(), self.lbl()
            self.e("test rax, rax")
            self.e(f"jnz {ok}")
            self.e(f"mov rdx, {line}")
            self.e("jmp rt_fail_div")
            self.ctx.mark(ok)
            self.e("mov rbx, rax")
            self.e("mov rax, rcx")
            self.e("cmp rbx, -1")
            self.e(f"jne {normal}")
            if op == "/":
                self.e("neg rax")
                self.overflow_check(line)
            else:
                self.e("xor eax, eax")
            self.e(f"jmp {done}")
            self.ctx.mark(normal)
            self.e("cqo")
            self.e("idiv rbx")
            if op == "%":
                self.e("mov rax, rdx")
            self.ctx.mark(done)
        elif op == "**":
            self.e("mov rdi, rcx")
            self.e("mov rsi, rax")
            self.e(f"mov rdx, {line}")
            self.e("call rt_pow")
        else:
            err(f"unknown operator {op}", line)
        return WHOLE

    def ex_cmp(self, n, want):
        _, line, op, ln, rn = n
        lt = self.expr(ln)
        self.e("push rax")
        rt = self.expr(rn)
        if lenient(lt, rt):
            self.e("pop rcx")
            return BOOL
        if lt != rt:
            err(f"can't compare {tname(lt)} with {tname(rt)}", line)
        if lt == DECIMAL:
            self.e("pop rcx")
            self.e("movq xmm0, rcx")
            self.e("movq xmm1, rax")
            if op in ("==", "!="):
                self.e("ucomisd xmm0, xmm1")
                if op == "==":
                    self.e("sete al")
                    self.e("setnp cl")
                    self.e("and al, cl")
                else:
                    self.e("setne al")
                    self.e("setp cl")
                    self.e("or al, cl")
            elif op in (">", ">="):
                self.e("ucomisd xmm0, xmm1")
                self.e("seta al" if op == ">" else "setae al")
            else:
                self.e("ucomisd xmm1, xmm0")
                self.e("seta al" if op == "<" else "setae al")
            self.e("movzx rax, al")
            return BOOL
        if lt == TEXT:
            if op not in ("==", "!="):
                err("ordering text with `is above` / `is below` is not supported yet", line)
            lf, rf = self.is_fresh(ln), self.is_fresh(rn)
            self.e("mov rsi, rax")
            self.e("pop rdi")
            if lf or rf:
                self.e("mov rbx, rdi")
                self.e("mov r12, rsi")
            self.e("call rt_text_eq")
            self.cat_free(lf, rf)
            if op == "!=":
                self.e("xor rax, 1")
            return BOOL
        if isinstance(lt, tuple) or (lt == BOOL and op not in ("==", "!=")):
            err(f"can't compare {tname(lt)} that way (native comparison of lists, records and choices is not supported yet)", line)
        self.e("pop rcx")
        self.e("cmp rcx, rax")
        self.e(f"{CMP_SET[op]} al")
        self.e("movzx rax, al")
        return BOOL

    def ex_in(self, n, want):
        _, line, ln, rn = n
        lt = self.expr(ln)
        self.e("push rax")
        if rn[0] == "range":
            _, rl, lo, hi, incl, step, down = rn
            if step is not None or down:
                err("`step` and `down to` aren't supported with `in`", line)
            self.whole_expr(lo, "the start of a range", rl)
            self.e("push rax")
            self.whole_expr(hi, "the end of a range", rl)
            self.e("pop rcx")
            self.e("pop rdx")
            self.e("cmp rdx, rcx")
            self.e("setge r8b")
            self.e("cmp rdx, rax")
            self.e(f"{'setle' if incl else 'setl'} r9b")
            self.e("and r8b, r9b")
            self.e("movzx rax, r8b")
            return BOOL
        rt = self.expr(rn)
        if not is_list(rt) and rt != UNKNOWN:
            err(f"`in` needs a list or a range on the right, not {tname(rt)}", line)
        if rt != UNKNOWN and lt != UNKNOWN and rt[1] != lt:
            err(f"this list holds {tname(rt[1])}, but you are looking for {tname(lt)}", line)
        self.e("mov rdi, rax")
        self.e("pop rsi")
        self.e("call rt_list_has_text" if lt == TEXT else "call rt_list_has")
        return BOOL

    def ex_range(self, n, want):
        err("a range can only be used in a `for` loop or after `in` for now", n[1])

    def ex_index(self, n, want):
        _, line, on, inn = n
        t = self.expr(on)
        if t != UNKNOWN and not is_list(t):
            err(f"you can't pick items out of {tname(t)} natively yet", line,
                "only lists support `[ ]` in the native compiler")
        self.e("push rax")
        self.whole_expr(inn, "an item number", line)
        self.e("pop rdx")
        self.e("mov rcx, rax")
        self.bounds(line)
        self.e("mov rdx, [rdx+16]")
        self.e("mov rax, [rdx+rcx*8]")
        return UNKNOWN if t == UNKNOWN else t[1]

    def ex_member(self, n, want):
        _, line, on, name = n
        t = self.expr(on)
        if name == "length" and (t in (TEXT, UNKNOWN) or is_list(t)):
            self.length_of(on, t)
            return WHOLE
        if isinstance(t, tuple) and t[0] == "rec":
            for i, (fn, ft) in enumerate(self.records[t[1]]):
                if fn == name:
                    self.e(f"mov rax, [rax+{8 * i}]")
                    return ft
            err(f"{t[1]} has no field `{name}`", line,
                "its fields: " + ", ".join(f for f, _ in self.records[t[1]]))
        err(f"{tname(t)} has no property `{name}`", line)

    def ex_call(self, n, want):
        _, line, callee, args = n
        if callee[0] == "member":
            return self.method(callee, args, line)
        if callee[0] != "name":
            err("only named functions can be called", line)
        name = callee[2]
        if name in self.records or name in self.variants:
            return self.construct(name, args, line)
        if name in self.funs:
            ats = self.push_args(self.funs[name], args, line)
            rtype, label = self.instantiate(name, ats, line)
            self.e(f"call {label}")
            if args:
                self.e(f"add rsp, {8 * len(args)}")
            return rtype
        if name in BUILTINS:
            return self.builtin(name, args, line)
        if self.lookup(name):
            err(f"`{name}` is not a function, so it can't be called", line)
        err(f"I don't know the function `{name}`", line)

    def construct(self, name, args, line):
        if name in self.records:
            fields, first, size, ret = self.records[name], 0, 8 * max(1, len(self.records[name])), ("rec", name)
        else:
            cn, tag, fields = self.variants[name]
            first, size, ret = 1, self.csize[cn], ("choice", cn)
        if len(args) != len(fields):
            err(f"`{name}` needs {len(fields)} value(s) but got {len(args)}", line)
        self.e(f"mov rdi, {size}")
        self.e("call rt_alloc")
        if name in self.variants:
            self.e(f"mov qword ptr [rax], {tag}")
        self.e("push rax")
        for i, ((fn, ft), a) in enumerate(zip(fields, args)):
            t = self.value(a, ft)
            if not lenient(t, ft) and t != ft:
                err(f"`{fn}` should be {tname(ft)} but got {tname(t)}", line)
            self.e("mov rcx, [rsp]")
            self.e(f"mov [rcx+{8 * (first + i)}], rax")
        self.e("pop rax")
        return ret

    def root_name(self, node):
        while node[0] in ("member", "index"):
            node = node[2]
        return node[2] if node[0] == "name" else None

    def check_exclusive(self, uses, line):
        for i in range(len(uses)):
            for j in range(i + 1, len(uses)):
                if uses[i][0] == uses[j][0] and (uses[i][1] or uses[j][1]):
                    err(f"`{uses[i][0]}` is used twice in this call, and one use is a `mut ref` or a move", line,
                        "a value being changed can't be lent out at the same time")

    def address(self, op, line):
        """leave the address of a plain value's home (variable, field, item) in rax"""
        if op[0] == "name":
            ent = self.lookup(op[2])
            if ent[5].moved is not None:
                err(f"`{op[2]}` was moved on line {ent[5].moved}", line)
            if ent[0] == "G":
                self.e(f"lea rax, [rip+{ent[1]}]")
            elif ent[0] == "P":
                if ent[5].deref:
                    self.e(f"mov rax, [rbp+{ent[1]}]")
                else:
                    self.e(f"lea rax, [rbp+{ent[1]}]")
            else:
                self.e(f"lea rax, [rbp{ent[1]:+d}]")
        elif op[0] == "member":
            rt_ = self.expr(op[2])
            for i, (fn, ft) in enumerate(self.records[rt_[1]]):
                if fn == op[3]:
                    self.e(f"lea rax, [rax+{8 * i}]")
        else:
            self.expr(op[2])
            self.e("push rax")
            self.whole_expr(op[3], "an item number", line)
            self.e("pop rdx")
            self.e("mov rcx, rax")
            self.bounds(line)
            self.e("mov rdx, [rdx+16]")
            self.e("lea rax, [rdx+rcx*8]")

    def borrow(self, op, mut, line):
        """evaluate `op` for lending; rax gets the value (heap values) or, for mut ref of plain values, its address"""
        if mut and op[0] not in ("name", "member", "index"):
            err("a temporary can't be lent as `mut ref`", line, "put it in a `so var` first")
        mark = len(self.ctx.code)
        t = self.expr(op)
        if mut:
            self.require_mutable(op, line, "a `mut ref` of it")
        if self.is_owned(t):
            if self.is_fresh(op):
                self.hidden(t)
            return t
        if mut and t != UNKNOWN:
            del self.ctx.code[mark:]
            self.address(op, line)
        return t

    def push_args(self, fdef, args, line, skip=0, uses=None):
        params = fdef[3]
        if len(args) + skip != len(params):
            err(f"`{fdef[2]}` needs {len(params) - skip} value(s) but got {len(args)}", line)
        uses = list(uses or [])
        ats = []
        for i, a in enumerate(args):
            pn, pt = params[i + skip]
            kind, _ = param_kind(pn, pt, i + skip == 0)
            if kind == "val":
                if a[0] == "ref":
                    err(f"`{pn}` takes ownership, so remove `ref` here", line)
                t = self.value(a)
                r = self.root_name(a)
                if r and self.is_owned(t):
                    uses.append((r, True))
            else:
                word = "mut ref" if kind == "mut" else "ref"
                if a[0] != "ref":
                    err(f"`{pn}` is borrowed, so write `{word} ...` here", line)
                if a[3] != (kind == "mut"):
                    err(f"`{pn}` needs `{word}`", line)
                t = self.borrow(a[2], kind == "mut", line)
                r = self.root_name(a[2])
                if r:
                    uses.append((r, kind == "mut"))
            if t == NOTHING:
                err("this has no value to pass", line)
            ats.append(t)
            self.e("push rax")
        self.check_exclusive(uses, line)
        return ats

    def ex_ref(self, n, want):
        err("`ref` can only be used when passing a value to a function or method", n[1])

    def length_of(self, node, t):
        if self.is_owned(t) and self.is_fresh(node):
            self.e("mov rcx, rax")
            self.e("mov rax, [rax]")
            self.e("push rax")
            self.e("mov rdi, rcx")
            self.e(f"call {self.drop_label(t)}")
            self.e("pop rax")
        else:
            self.e("mov rax, [rax]")

    def need_args(self, name, args, count, line):
        if len(args) != count:
            err(f"`{name}` needs {count} value(s) but got {len(args)}", line)

    def builtin(self, name, args, line):
        if name == "ask":
            if len(args) > 1:
                err("`ask` takes at most one value (the prompt)", line)
            if args:
                t = self.expr(args[0])
                if t not in (TEXT, UNKNOWN):
                    err("the prompt for `ask` must be text", line)
                self.e("mov rdi, rax")
            else:
                self.e("xor edi, edi")
            self.e("call rt_ask")
            return TEXT
        if name == "text":
            self.need_args(name, args, 1, line)
            t = self.expr(args[0])
            if t == TEXT and not self.is_fresh(args[0]):
                self.e("mov rdi, rax")
                self.e("call rt_copy_text")
            else:
                self.to_text(t, line)
            return TEXT
        if name == "length":
            self.need_args(name, args, 1, line)
            t = self.expr(args[0])
            if t not in (TEXT, UNKNOWN) and not is_list(t):
                err(f"`length` needs text or a list, not {tname(t)}", line)
            self.length_of(args[0], t)
            return WHOLE
        if name == "decimal":
            self.need_args(name, args, 1, line)
            t = self.expr(args[0])
            if t == WHOLE:
                self.e("cvtsi2sd xmm0, rax")
                self.e("movq rax, xmm0")
            elif t not in (DECIMAL, UNKNOWN):
                err(f"`decimal` converts whole numbers, not {tname(t)}", line)
            return DECIMAL
        if name == "sqrt":
            self.need_args(name, args, 1, line)
            t = self.expr(args[0])
            if t not in (DECIMAL, UNKNOWN):
                err(f"`sqrt` needs a decimal, but got {tname(t)}", line, "convert with decimal(x)")
            ok = self.lbl()
            self.e("movq xmm0, rax")
            self.e("xorpd xmm1, xmm1")
            self.e("ucomisd xmm0, xmm1")
            self.e(f"jae {ok}")
            self.e(f"lea rdi, [rip+{self.sl(chr(99) + 'an' + chr(39) + 't take the square root of a negative number')}]")
            self.e(f"mov rsi, {line}")
            self.e("jmp rt_die")
            self.ctx.mark(ok)
            self.e("sqrtsd xmm0, xmm0")
            self.e("movq rax, xmm0")
            return DECIMAL
        if name == "whole":
            self.need_args(name, args, 1, line)
            t = self.expr(args[0])
            if t == DECIMAL:
                ok = self.lbl()
                self.e("movq xmm0, rax")
                self.e("cvttsd2si rax, xmm0")
                self.e("mov rcx, 0x8000000000000000")
                self.e("cmp rax, rcx")
                self.e(f"jne {ok}")
                self.e(f"mov rdx, {line}")
                self.e("jmp rt_fail_overflow")
                self.ctx.mark(ok)
            elif t == TEXT:
                self.e("mov rdi, rax")
                self.e(f"mov rsi, {line}")
                fresh = self.is_fresh(args[0])
                if fresh:
                    self.e("push rdi")
                self.e("call rt_parse_whole")
                if fresh:
                    self.e("pop rdi")
                    self.e("push rax")
                    self.e("call rt_free")
                    self.e("pop rax")
            elif t not in (WHOLE, UNKNOWN):
                err(f"`whole` can't convert {tname(t)}", line)
            return WHOLE
        if name == "abs":
            self.need_args(name, args, 1, line)
            self.whole_expr(args[0], "`abs` needs a whole number, this", line)
            skip = self.lbl()
            self.e("test rax, rax")
            self.e(f"jns {skip}")
            self.e("neg rax")
            self.overflow_check(line)
            self.ctx.mark(skip)
            return WHOLE
        if name in ("min", "max"):
            self.need_args(name, args, 2, line)
            self.whole_expr(args[0], f"`{name}` needs whole numbers, this", line)
            self.e("push rax")
            self.whole_expr(args[1], f"`{name}` needs whole numbers, this", line)
            self.e("pop rcx")
            self.e("cmp rcx, rax")
            self.e("cmovl rax, rcx" if name == "min" else "cmovg rax, rcx")
            return WHOLE
        if name == "panic":
            self.need_args(name, args, 1, line)
            t = self.expr(args[0])
            if t not in (TEXT, UNKNOWN):
                err("`panic` needs text", line)
            self.e("mov rsi, rax")
            self.e(f"lea rdi, [rip+{self.sl('panic: ')}]")
            self.e("call rt_concat")
            self.e("mov rdi, rax")
            self.e(f"mov rsi, {line}")
            self.e("jmp rt_die")
            return NOTHING
        if name == "check":
            self.need_args(name, args, 2, line)
            self.cond(args[0], "the first value of `check`", line)
            ok = self.lbl()
            self.e("test al, al")
            self.e(f"jnz {ok}")
            t = self.expr(args[1])
            if t not in (TEXT, UNKNOWN):
                err("the message for `check` must be text", line)
            self.e("mov rsi, rax")
            self.e(f"lea rdi, [rip+{self.sl('check failed: ')}]")
            self.e("call rt_concat")
            self.e("mov rdi, rax")
            self.e(f"mov rsi, {line}")
            self.e("jmp rt_die")
            self.ctx.mark(ok)
            return NOTHING

    def method(self, callee, args, line):
        _, _, on, name = callee
        t = self.expr(on)
        if name == "copy":
            self.need_args("copy", args, 0, line)
            if not self.is_owned(t):
                err("`.copy()` is for text, lists, records and choices", line)
            self.e("mov rdi, rax")
            self.e(f"call {self.copy_label(t)}")
            return t
        if isinstance(t, tuple) and t[0] in ("rec", "choice"):
            key = f"{t[1]}.{name}"
            if key not in self.funs:
                err(f"{t[1]} has no method `.{name}`", line, f"add it with `extend {t[1]}`")
            fdef = self.funs[key]
            kind, _ = param_kind("self", fdef[3][0][1] if fdef[3] else None, True)
            if not fdef[3] or fdef[3][0][0] != "self":
                err(f"`{name}` has no `self`, so it can't be called on a value", line)
            if kind == "mut":
                self.require_mutable(on, line, "a `mut self` method")
            if self.is_fresh(on):
                self.hidden(t)
            self.e("push rax")
            r = self.root_name(on)
            ats = [t] + self.push_args(fdef, args, line, skip=1, uses=[(r, kind == "mut")] if r else [])
            rtype, label = self.instantiate(key, ats, line)
            self.e(f"call {label}")
            self.e(f"add rsp, {8 * (len(args) + 1)}")
            return rtype
        islist = is_list(t)
        if name == "push" and (islist or t == UNKNOWN):
            self.need_args("push", args, 1, line)
            self.require_mutable(on, line, "pushing to it")
            self.e("push rax")
            vt = self.value(args[0])
            if islist and not lenient(vt, t[1]) and vt != t[1]:
                err(f"this list holds {tname(t[1])}, but this is {tname(vt)}", line)
            self.e("mov rsi, rax")
            self.e("pop rdi")
            self.e("call rt_list_push")
            return NOTHING
        if name == "is_empty" and (islist or t in (TEXT, UNKNOWN)):
            self.need_args("is_empty", args, 0, line)
            self.e("cmp qword ptr [rax], 0")
            self.e("sete al")
            self.e("movzx rax, al")
            return BOOL
        err(f"{tname(t)} has no method `.{name}` in the native compiler yet", line)


# ---------------------------------------------------------------- runtime


def runtime(cp):
    s = cp.sl
    return f"""
    .intel_syntax noprefix
    .section .text
    .globl _start
_start:
    mov rax, 9
    xor edi, edi
    mov rsi, 0x40000000
    mov edx, 3
    mov r10d, 0x4022
    mov r8, -1
    xor r9d, r9d
    syscall
    cmp rax, -4096
    ja 1f
    mov [rip+heap_ptr], rax
    mov [rip+heap_base], rax
    lea rcx, [rax+0x40000000]
    mov [rip+heap_end], rcx
    call fish_main
    mov eax, 60
    xor edi, edi
    syscall
1:  mov eax, 60
    mov edi, 1
    syscall

rt_alloc:
    cmp rdi, 16
    ja 1f
    mov ecx, 4
    jmp 2f
1:  lea rax, [rdi-1]
    bsr rcx, rax
    inc ecx
2:  cmp ecx, 30
    ja rt_oom
    lea r8, [rip+freelists]
    mov rax, [r8+rcx*8]
    test rax, rax
    jz 3f
    mov rdx, [rax]
    mov [r8+rcx*8], rdx
    ret
3:  mov edx, 1
    shl rdx, cl
    add rdx, 8
    mov rax, [rip+heap_ptr]
    lea r9, [rax+rdx]
    cmp r9, [rip+heap_end]
    ja rt_oom
    mov [rip+heap_ptr], r9
    mov [rax], rcx
    add rax, 8
    ret

rt_free:
    test rdi, rdi
    jz 1f
    cmp rdi, [rip+heap_base]
    jb 1f
    mov rcx, [rdi-8]
    lea r8, [rip+freelists]
    mov rax, [r8+rcx*8]
    mov [rdi], rax
    mov [r8+rcx*8], rdi
1:  ret
rt_oom:
    lea rdi, [rip+{s('error: out of memory')}]
    call rt_err_text
    lea rdi, [rip+{s(chr(10))}]
    call rt_err_text
    mov eax, 60
    mov edi, 1
    syscall

rt_print_text:
    mov rdx, [rdi]
    lea rsi, [rdi+8]
    mov edi, 1
    mov eax, 1
    syscall
    ret
rt_err_text:
    mov rdx, [rdi]
    lea rsi, [rdi+8]
    mov edi, 2
    mov eax, 1
    syscall
    ret
rt_print_space:
    lea rdi, [rip+{s(' ')}]
    jmp rt_print_text
rt_print_nl:
    lea rdi, [rip+{s(chr(10))}]
    jmp rt_print_text

rt_die:
    push rsi
    push rdi
    lea rdi, [rip+{s('error: ')}]
    call rt_err_text
    pop rdi
    call rt_err_text
    lea rdi, [rip+{s(chr(10) + '  at line ')}]
    call rt_err_text
    pop rdi
    call rt_int_to_text
    mov rdi, rax
    call rt_err_text
    lea rdi, [rip+{s(chr(10))}]
    call rt_err_text
    mov eax, 60
    mov edi, 1
    syscall

rt_die_noline:
    push rdi
    lea rdi, [rip+{s('error: ')}]
    call rt_err_text
    pop rdi
    call rt_err_text
    lea rdi, [rip+{s(chr(10))}]
    call rt_err_text
    mov eax, 60
    mov edi, 1
    syscall

rt_dec_to_text:
    push rbx
    push r12
    push r13
    sub rsp, 64
    mov r12, rdi
    mov rax, rdi
    shr rax, 52
    and eax, 0x7FF
    cmp eax, 0x7FF
    je rt_dec_inf
    mov rax, 0x7FFFFFFFFFFFFFFF
    and rax, rdi
    movq xmm0, rax
    mov rax, 4801044003149578240
    movq xmm1, rax
    ucomisd xmm0, xmm1
    jae rt_dec_big
    mov rax, 4696837146684686336
    movq xmm1, rax
    mulsd xmm0, xmm1
    cvtsd2si rbx, xmm0
    mov rax, rbx
    xor edx, edx
    mov ecx, 1000000
    div rcx
    lea rsi, [rsp+64]
    mov r13, rsi
    mov ecx, 6
1:  mov r8, rax
    mov rax, rdx
    xor edx, edx
    mov r9d, 10
    div r9
    add dl, 48
    dec rsi
    mov [rsi], dl
    mov rdx, rax
    mov rax, r8
    dec ecx
    jnz 1b
    dec rsi
    mov byte ptr [rsi], 46
2:  xor edx, edx
    mov ecx, 10
    div rcx
    add dl, 48
    dec rsi
    mov [rsi], dl
    test rax, rax
    jnz 2b
    test r12, r12
    jns 3f
    dec rsi
    mov byte ptr [rsi], 45
3:  cmp byte ptr [r13-1], 48
    jne 4f
    cmp byte ptr [r13-2], 46
    je 4f
    dec r13
    jmp 3b
4:  mov rcx, r13
    sub rcx, rsi
    mov r12, rcx
    lea rdi, [rcx+8]
    push rsi
    call rt_alloc
    pop rsi
    mov [rax], r12
    lea rdi, [rax+8]
    mov rcx, r12
    cld
    rep movsb
    add rsp, 64
    pop r13
    pop r12
    pop rbx
    ret
rt_dec_inf:
    lea rdi, [rip+{s("can't show infinity or nan")}]
    jmp rt_die_noline
rt_dec_big:
    lea rdi, [rip+{s('this decimal is too large to show (limit about 9.2 trillion)')}]
    jmp rt_die_noline

rt_fail_overflow:
    lea rdi, [rip+{s('a whole number got too big (overflow)')}]
    mov rsi, rdx
    jmp rt_die
rt_fail_div:
    lea rdi, [rip+{s("can't divide by zero")}]
    mov rsi, rdx
    jmp rt_die
rt_fail_step:
    lea rdi, [rip+{s('`step` must be above 0')}]
    mov rsi, rdx
    jmp rt_die
rt_fail_negpow:
    lea rdi, [rip+{s("a whole number can't be raised to a negative power")}]
    mov rsi, rdx
    jmp rt_die

rt_fail_index:
    push rdx
    push rsi
    push rdi
    lea rdi, [rip+{s('error: item ')}]
    call rt_err_text
    mov rdi, [rsp]
    call rt_int_to_text
    mov rdi, rax
    call rt_err_text
    lea rdi, [rip+{s(' is outside this list, which has ')}]
    call rt_err_text
    mov rdi, [rsp+8]
    call rt_int_to_text
    mov rdi, rax
    call rt_err_text
    lea rdi, [rip+{s(' items (numbered from 0)' + chr(10) + '  at line ')}]
    call rt_err_text
    mov rdi, [rsp+16]
    call rt_int_to_text
    mov rdi, rax
    call rt_err_text
    lea rdi, [rip+{s(chr(10))}]
    call rt_err_text
    mov eax, 60
    mov edi, 1
    syscall

rt_int_to_text:
    push rbx
    push r12
    sub rsp, 32
    mov rax, rdi
    xor ebx, ebx
    test rax, rax
    jns 1f
    neg rax
    mov ebx, 1
1:  lea rsi, [rsp+32]
    mov ecx, 10
2:  xor edx, edx
    div rcx
    add dl, 48
    dec rsi
    mov [rsi], dl
    test rax, rax
    jnz 2b
    test ebx, ebx
    jz 3f
    dec rsi
    mov byte ptr [rsi], 45
3:  lea rcx, [rsp+32]
    sub rcx, rsi
    mov r12, rcx
    lea rdi, [rcx+8]
    push rsi
    call rt_alloc
    pop rsi
    mov [rax], r12
    lea rdi, [rax+8]
    mov rcx, r12
    cld
    rep movsb
    add rsp, 32
    pop r12
    pop rbx
    ret

rt_concat:
    push rbx
    push r12
    push r13
    mov rbx, rdi
    mov r12, rsi
    mov r13, [rbx]
    add r13, [r12]
    lea rdi, [r13+8]
    call rt_alloc
    mov [rax], r13
    push rax
    lea rdi, [rax+8]
    lea rsi, [rbx+8]
    mov rcx, [rbx]
    cld
    rep movsb
    lea rsi, [r12+8]
    mov rcx, [r12]
    rep movsb
    pop rax
    pop r13
    pop r12
    pop rbx
    ret

rt_copy_text:
    lea rsi, [rip+{s('')}]
    jmp rt_concat

rt_text_eq:
    mov rcx, [rdi]
    cmp rcx, [rsi]
    jne 1f
    test rcx, rcx
    jz 2f
    add rdi, 8
    add rsi, 8
    cld
    repe cmpsb
    sete al
    movzx eax, al
    ret
2:  mov eax, 1
    ret
1:  xor eax, eax
    ret

rt_list_new:
    push rbx
    mov rbx, rdi
    cmp rbx, 4
    jae 1f
    mov ebx, 4
1:  mov edi, 24
    call rt_alloc
    push rax
    lea rdi, [rbx*8]
    call rt_alloc
    mov rcx, rax
    pop rax
    mov qword ptr [rax], 0
    mov [rax+8], rbx
    mov [rax+16], rcx
    pop rbx
    ret

rt_list_push:
    mov rax, [rdi]
    cmp rax, [rdi+8]
    jb 1f
    push rdi
    push rsi
    mov rdx, [rdi+8]
    shl rdx, 1
    push rdx
    lea rdi, [rdx*8]
    call rt_alloc
    pop rdx
    pop rsi
    pop rdi
    mov r8, rax
    mov rax, [rdi]
    push rsi
    push rdi
    mov rsi, [rdi+16]
    mov rdi, r8
    mov rcx, rax
    shl rcx, 3
    cld
    rep movsb
    pop rdi
    pop rsi
    push rdi
    push rsi
    push rdx
    push r8
    mov rdi, [rdi+16]
    call rt_free
    pop r8
    pop rdx
    pop rsi
    pop rdi
    mov [rdi+16], r8
    mov [rdi+8], rdx
1:  mov rax, [rdi]
    mov rcx, [rdi+16]
    mov [rcx+rax*8], rsi
    inc rax
    mov [rdi], rax
    ret

rt_list_has:
    mov rcx, [rdi]
    mov rdx, [rdi+16]
    xor eax, eax
1:  test rcx, rcx
    jz 2f
    dec rcx
    cmp [rdx+rcx*8], rsi
    jne 1b
    mov eax, 1
2:  ret

rt_list_has_text:
    push rbx
    push r12
    push r13
    mov rbx, [rdi+16]
    mov r12, [rdi]
    mov r13, rsi
1:  test r12, r12
    jz 2f
    dec r12
    mov rdi, [rbx+r12*8]
    mov rsi, r13
    call rt_text_eq
    test eax, eax
    jz 1b
    mov eax, 1
    jmp 3f
2:  xor eax, eax
3:  pop r13
    pop r12
    pop rbx
    ret

rt_pow:
    test rsi, rsi
    js rt_fail_negpow
    mov eax, 1
1:  test rsi, rsi
    jz 3f
    test rsi, 1
    jz 2f
    imul rax, rdi
    jo rt_fail_overflow
2:  shr rsi, 1
    jz 3f
    imul rdi, rdi
    jo rt_fail_overflow
    jmp 1b
3:  ret

rt_ask:
    push rbx
    push r12
    test rdi, rdi
    jz 1f
    call rt_print_text
1:  mov edi, 4104
    call rt_alloc
    mov rbx, rax
    xor r12d, r12d
2:  cmp r12, 4096
    jae 3f
    xor eax, eax
    xor edi, edi
    lea rsi, [rbx+r12+8]
    mov edx, 1
    syscall
    cmp rax, 1
    jne 3f
    cmp byte ptr [rbx+r12+8], 10
    je 3f
    inc r12
    jmp 2b
3:  mov [rbx], r12
    mov rax, rbx
    pop r12
    pop rbx
    ret

rt_parse_whole:
    push rbx
    push r12
    push r13
    push r14
    mov rbx, rdi
    mov r14, rsi
    mov rcx, [rbx]
    lea r12, [rbx+8]
    lea r13, [r12+rcx]
1:  cmp r12, r13
    jae rt_pw_bad
    cmp byte ptr [r12], 32
    jne 2f
    inc r12
    jmp 1b
2:  cmp byte ptr [r13-1], 32
    jne 3f
    dec r13
    jmp 2b
3:  xor r8d, r8d
    cmp byte ptr [r12], 45
    jne 4f
    mov r8d, 1
    inc r12
    jmp 5f
4:  cmp byte ptr [r12], 43
    jne 5f
    inc r12
5:  cmp r12, r13
    jae rt_pw_bad
    xor eax, eax
6:  movzx ecx, byte ptr [r12]
    sub ecx, 48
    cmp ecx, 9
    ja rt_pw_bad
    imul rax, rax, 10
    jo rt_pw_ovf
    sub rax, rcx
    jo rt_pw_ovf
    inc r12
    cmp r12, r13
    jb 6b
    test r8d, r8d
    jnz 7f
    neg rax
    jo rt_pw_ovf
7:  pop r14
    pop r13
    pop r12
    pop rbx
    ret
rt_pw_ovf:
    mov rdx, r14
    jmp rt_fail_overflow
rt_pw_bad:
    lea rdi, [rip+{s(chr(34))}]
    mov rsi, rbx
    call rt_concat
    mov rdi, rax
    lea rsi, [rip+{s(chr(34) + ' is not a whole number')}]
    call rt_concat
    mov rdi, rax
    mov rsi, r14
    jmp rt_die
"""


def data_section(cp):
    out = ["    .section .data"]
    for text, lab in cp.strs.items():
        b = text.encode("utf-8")
        out.append(f"    .balign 8\n{lab}:\n    .quad {len(b)}")
        if b:
            out.append("    .byte " + ",".join(map(str, b)))
    out.append("    .section .bss\n    .balign 8")
    out.append("heap_ptr: .zero 8\nheap_end: .zero 8\nheap_base: .zero 8\nfreelists: .zero 256")
    for g in cp.bss:
        out.append(f"{g}: .zero 8")
    return "\n".join(out)


def compile_source(src):
    prog = Parser(tokenize(src)).program()
    cp = Compiler()
    main_asm = cp.program(prog)
    rt = runtime(cp)  # registers its own strings
    text = "\n".join(["    .intel_syntax noprefix", "    .section .text", main_asm] + cp.fun_asm + cp.glue_asm)
    return text + "\n" + rt.replace("    .intel_syntax noprefix\n    .section .text\n", "", 1) + "\n" + data_section(cp) + "\n"


def assemble(asm, out):
    with tempfile.TemporaryDirectory() as d:
        s, o = os.path.join(d, "p.s"), os.path.join(d, "p.o")
        open(s, "w").write(asm)
        for cmd in (["as", "-o", o, s], ["ld", "-o", out, o]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                print("internal error while assembling:\n" + r.stderr, file=sys.stderr)
                return False
    return True


def main(argv):
    if len(argv) < 3 or argv[1] not in ("build", "run", "check", "asm"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd, path = argv[1], argv[2]
    try:
        src = open(path, encoding="utf-8").read()
    except OSError as ex:
        print(f"error: can't read {path}: {ex.strerror}", file=sys.stderr)
        return 2
    try:
        asm = compile_source(src)
    except FishError as e:
        report(e, path, src)
        return 1
    if cmd == "check":
        print("ok: no problems found")
        return 0
    if cmd == "asm":
        print(asm)
        return 0
    if cmd == "build":
        out = argv[argv.index("-o") + 1] if "-o" in argv else os.path.splitext(path)[0]
        if not assemble(asm, out):
            return 3
        print(f"built {out}")
        return 0
    with tempfile.TemporaryDirectory() as d:
        exe = os.path.join(d, "prog")
        if not assemble(asm, exe):
            return 3
        return subprocess.run([exe]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
