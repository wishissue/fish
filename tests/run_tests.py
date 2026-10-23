#!/usr/bin/env python3
"""Fish test suite. Run from the fish folder: python3 tests/run_tests.py

1. Differential tests: every program must print the same thing under the
   interpreter (fish.py) and the native compiler (fishc.py).
2. Failure tests: the program must fail with the expected message, both at
   compile time and at run time.
"""
import os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
passed = failed = 0


def run(cmd, stdin=""):
    r = subprocess.run(cmd, capture_output=True, text=True, input=stdin, cwd=ROOT)
    return r.returncode, r.stdout, r.stderr


def write(src):
    f = tempfile.NamedTemporaryFile("w", suffix=".fish", delete=False)
    f.write(src); f.close()
    return f.name


def ok(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {name}\n{detail}")


# ---- differential programs (name, source, stdin)
SAME = [
 ("arith", 'say 1 + 2 * 3, (1 + 2) * 3, 10 - 4 - 3, 100 / 7, -100 / 7, 100 mod 7, -100 mod 7, 3 ** 4', ""),
 ("big", 'say 9223372036854775807, -9223372036854775807 - 1, 3037000499 * 3037000499', ""),
 ("logic", 'so a = 5\nsay a is 5 and a is above 3, not true, false or a is below 2', ""),
 ("text", 'so n = "Fish"\nsay "Hi {n}! {1 + 1} {n is "Fish"}", n + "!" + n', ""),
 ("nested", 'fun f(n)\n  if n is 0\n    give "zero"\n  stop\n  give "n={n}"\nstop\nsay f(0), f(5)', ""),
 ("recursion", 'fun fib(n)\n  if n is below 2\n    give n\n  stop\n  give fib(n - 1) + fib(n - 2)\nstop\nsay fib(20)', ""),
 ("globals", 'so var hits = 0\nfun bump()\n  hits += 1\nstop\nbump()\nbump()\nsay hits', ""),
 ("generic", 'fun twice(x)\n  give x + x\nstop\nsay twice(4), twice("ab")', ""),
 ("implicit", 'fun sq(n)\n  n * n\nstop\nsay sq(9)', ""),
 ("loops", 'so var s = 0\nfor i in 0 until 5\n  if i is 3\n    continue\n  stop\n  s += i\nstop\nsay s\nloop\n  s += 1\n  if s is above 20\n    break\n  stop\nstop\nsay s', ""),
 ("listlist", 'so grid = [[1, 2], [3]]\nsay grid, grid[0][1], grid.length', ""),
 ("shadow", 'so x = 1\nso x = x + 1\nso x = "now text {x}"\nsay x', ""),
 ("ask", 'so name = ask("name? ")\nsay "hi {name}", whole(ask()) + 1', "Ann\n41\n"),
 ("matchtext", 'match "b"\n  when "a"\n    say 1\n  when "b" or "c"\n    say 2\n  else\n    say 3\nstop', ""),
 ("or-if", 'fun grade(n)\n  if n is at least 90\n    give "A"\n  or if n is at least 80\n    give "B"\n  or\n    give "C"\n  stop\nstop\nsay grade(95), grade(85), grade(10)', ""),
 ("mixed-else-or", 'so n = 5\nif n is 1\n  say "one"\nelse if n is 5\n  say "five"\nor\n  say "other"\nstop\nif n is 9\n  say "nine"\nor if n is 8\n  say "eight"\nelse\n  say "neither"\nstop', ""),
 ("match-or-default", 'fun f(n)\n  match n\n    when 1 or 2\n      give "low"\n    or\n      give "high"\n  stop\nstop\nsay f(1), f(7)', ""),
 ("dec-basic", 'so a = 2.5\nsay a + 4.0, a * 2.0, 1.0 / 3.0, a is above 2.0, decimal(3) / 2.0, whole(9.99), sqrt(2.0), -a', ""),
 ("dec-fn", 'fun half(x: decimal) gives decimal\n  give x / 2.0\nstop\nsay half(5.0), "v={half(3.0)}"', ""),
 ("rec-basic", 'record P\n  x: whole\n  name: text\nstop\nso var p = P(1, "a")\np.x += 5\nsay p, p.x, p.name', ""),
 ("rec-nested", 'record In\n  v: whole\nstop\nrecord Out\n  i: In\n  l: list of whole\nstop\nso o = Out(In(3), [1, 2])\nsay o, o.i.v, o.l[1]', ""),
 ("choice-basic", 'choice S\n  Circle(r: whole)\n  Rect(w: whole, h: whole)\n  Nil\nstop\nfun area(s: S) gives whole\n  match s\n    when Circle(r)\n      give 3 * r * r\n    when Rect(w, h)\n      give w * h\n    when Nil\n      give 0\n  stop\nstop\nsay area(Circle(2)), area(Rect(3, 4)), area(Nil), [Circle(1), Nil]', ""),
 ("choice-guard", 'choice S\n  A(n: whole)\n  B\nstop\nfun f(s)\n  match s\n    when A(n) if n is above 5\n      give "big"\n    when A(0)\n      give "zero"\n    when A(n)\n      give "small {n}"\n    else\n      give "other"\n  stop\nstop\nsay f(A(9)), f(A(0)), f(A(2)), f(B)', ""),
 ("method", 'record V\n  x: whole\nstop\nextend V\n  fun double(self) gives V\n    give V(self.x * 2)\n  stop\n  fun add(self, n)\n    self.x + n\n  stop\nstop\nso v = V(4)\nsay v.double().double(), v.add(10)', ""),

 ("own-ref", 'fun total(xs: ref list of whole) gives whole\n  so var t = 0\n  for x in xs\n    t += x\n  stop\n  give t\nstop\nso nums = [1, 2, 3]\nsay total(ref nums), total(ref nums), nums', ""),
 ("own-move", 'fun eat(s: text) gives whole\n  give s.length\nstop\nso a = "hello"\nso b = a\nsay eat(b)\nso c = "x{1}"\nsay eat(c), "done"', ""),
 ("own-mutref", 'fun bump(n: mut ref whole, by: whole)\n  n += by\nstop\nfun add_to(xs: mut ref list of whole, v: whole)\n  xs.push(v)\nstop\nso var k = 1\nbump(mut ref k, 4)\nbump(mut ref k, 5)\nso var xs = [1]\nadd_to(mut ref xs, 7)\nadd_to(mut ref xs, 8)\nsay k, xs', ""),
 ("own-mutself", 'record C\n  n: whole\n  tags: list of text\nstop\nextend C\n  fun bump(mut self)\n    self.n += 1\n    self.tags.push("t{self.n}")\n  stop\n  fun show(self) gives text\n    give "{self.n} {self.tags}"\n  stop\nstop\nso var c = C(0, [])\nc.bump()\nc.bump()\nsay c.show(), c', ""),
 ("own-field-ref", 'record P\n  x: whole\n  y: whole\nstop\nfun incr(v: mut ref whole)\n  v += 100\nstop\nso var p = P(1, 2)\nincr(mut ref p.y)\nso var xs = [10, 20]\nincr(mut ref xs[1])\nsay p, xs', ""),
 ("own-copy", 'so var a = [1, 2]\nso b = a.copy()\na.push(3)\nsay a, b\nrecord R\n  names: list of text\nstop\nso var r = R(["a"])\nso r2 = r.copy()\nr.names.push("b")\nsay r, r2', ""),
 ("own-reassign", 'so var s = "a"\ns = s + "b"\ns += "c"\nfun f()\n  so var t = [1]\n  t = [2, 3]\n  give t\nstop\nsay s, f()', ""),
 ("own-giveback", 'fun build_list(n: whole) gives list of text\n  so var out: list of text = []\n  out.push("n{n}")\n  give out\nstop\nsay build_list(1), build_list(2).length', ""),
 ("own-branch", 'fun pick(a: text, b: text, first: bool) gives text\n  if first\n    give a\n  stop\n  give b\nstop\nsay pick("x", "y", true), pick("x", "y", false)', ""),
 ("own-match-temp", 'choice T\n  Leaf(v: text)\n  Pair(l: text, r: text)\nstop\nfun mk(n: whole) gives T\n  if n is 0\n    give Leaf("solo")\n  stop\n  give Pair("a{n}", "b{n}")\nstop\nmatch mk(1)\n  when Pair(l, r)\n    say l + r\n  else\n    say "leaf"\nstop\nmatch mk(0)\n  when Leaf(v)\n    say v\n  else\n    say "pair"\nstop', ""),
 ("own-loop-owned", 'so var all: list of text = []\nfor i in 1 to 5\n  so s = "v{i}"\n  if i is 3\n    continue\n  stop\n  all.push(s)\nstop\nsay all', ""),
 ("emptytext", 'so e = ""\nsay e.length, e.is_empty(), "[" + e + "]"', ""),
]
for name, src, stdin in SAME:
    p = write(src)
    a = run([PY, "fish.py", "run", p], stdin)
    b = run([PY, "fishc.py", "run", p], stdin)
    ok(f"same:{name}", a[0] == 0 and b[0] == 0 and a[1] == b[1],
       f"interp({a[0]}): {a[1]!r} {a[2]}\nnative({b[0]}): {b[1]!r} {b[2]}")
    os.unlink(p)

# ---- programs that must fail natively with a given message
FAILS = [
 ("overflow-add", 'so var x = 9223372036854775807\nx = x + 1', "overflow"),
 ("overflow-mul", 'say 4611686018427387904 * 2', "overflow"),
 ("overflow-pow", 'say 2 ** 64', "overflow"),
 ("divzero", 'so z = 0\nsay 1 / z', "divide by zero"),
 ("modzero", 'so z = 0\nsay 1 mod z', "divide by zero"),
 ("index", 'so xs = [1, 2]\nsay xs[2]', "item 2 is outside this list, which has 2 items"),
 ("neg-index", 'so xs = [1, 2]\nsay xs[0 - 1]', "item -1"),
 ("badwhole", 'say whole("12x")', "is not a whole number"),
 ("panic", 'panic("boom")', "panic: boom"),
 ("check", 'check(1 is 2, "math broke")', "check failed: math broke"),
 ("step0", 'for i in 1 to 3 step 0\n  say i\nstop', "`step` must be above 0"),
 ("change-so", 'so a = 1\na = 2', "can't be changed"),
 ("type-mismatch", 'so a = 1\nsay a + "x"', "can't add whole and text"),
 ("annot", 'so text t = 5', "should be text but got whole"),
 ("unknown-name", 'so name = 1\nsay nmae', "did you mean `name`"),
 ("cond-type", 'so n = 5\nif n\n  say 1\nstop', "must be true or false"),
 ("arg-type", 'fun f(a: text)\n  give a\nstop\nsay f(3)', "should be text but got whole"),
 ("every-path", 'fun f(n) gives whole\n  if n is 1\n    give 1\n  stop\nstop\nsay f(1)', "every path"),
 ("empty-list", 'so xs = []', "empty list"),

 ("mix", 'say 1 + 2.5', "can't mix whole and decimal"),
 ("annot-decimal", 'so x: decimal = 5', "should be decimal but got whole"),
 ("dec-divzero", 'so z = 0.0\nsay 1.0 / z', "divide by zero"),
 ("sqrt-neg", 'say sqrt(0.0 - 4.0)', "negative number"),
 ("whole-big", 'say whole(decimal(9223372036854775807) * 4.0)', "overflow"),
 ("neg-power", 'say 2 ** (0 - 1)', "negative power"),
 ("rec-immutable", 'record P\n  x: whole\nstop\nso p = P(1)\np.x = 2', "can't be changed"),
 ("rec-field", 'record P\n  x: whole\nstop\nso p = P(1)\nsay p.y', "has no field `y`"),
 ("rec-arity", 'record P\n  x: whole\nstop\nsay P(1, 2)', "needs 1 value(s) but got 2"),
 ("rec-fieldtype", 'record P\n  x: whole\nstop\nsay P("a")', "should be whole but got text"),
 ("choice-missing", 'choice C\n  A\n  B\n  D(n: whole)\nstop\nso c = A\nmatch c\n  when A\n    say 1\n  when D(n)\n    say n\nstop', "doesn't cover: B"),
 ("choice-wrong", 'choice C\n  A\nstop\nchoice E\n  F\nstop\nmatch A\n  when F\n    say 1\n  else\n    say 2\nstop', "belongs to `E`"),
 ("method-missing", 'record P\n  x: whole\nstop\nso p = P(1)\nsay p.nope()', "no method `.nope`"),
 ("unknown-type", 'so x: Nope = 1', "I don't know the type"),
 ("old-word", 'make x = 5', "not a Fish word"),

 ("use-after-move", 'so a = "x"\nso b = a\nsay a', "`a` was moved on line 2"),
 ("move-into-call", 'fun f(s: text) gives whole\n  give s.length\nstop\nso a = "abc"\nsay f(a)\nsay a', "was moved on line 5"),
 ("move-in-loop", 'fun f(s: text) gives whole\n  give s.length\nstop\nso s = "abc"\nfor i in 1 to 3\n  say f(s)\nstop', "moved inside a loop"),
 ("move-in-loop-local", 'fun f(s: text) gives whole\n  give s.length\nstop\nfun g()\n  so s = "abc"\n  for i in 1 to 3\n    say f(s)\n  stop\nstop\ng()', "moved inside a loop"),
 ("move-global", 'keep a = [1]\nso b = a', "global value"),
 ("fn-cant-see-main", 'so xs = [1, 2]\nfun f()\n  give xs.length\nstop\nsay f()', "belongs to the main program"),
 ("move-from-field", 'record R\n  t: text\nstop\nso r = R("x")\nso t = r.t', "can't move a value out"),
 ("move-from-item", 'so xs = ["a"]\nso t = xs[0]', "can't move a value out"),
 ("move-borrowed", 'fun f(s: ref text) gives text\n  give s\nstop\nsay f(ref "x")', "only borrowed"),
 ("move-loopvar", 'so xs = ["a"]\nfun g()\n  so ys = ["a"]\n  for y in ys\n    so z = y\n  stop\nstop\ng()', "only borrowed"),
 ("ref-missing", 'fun f(s: ref text) gives whole\n  give s.length\nstop\nso a = "x"\nsay f(a)', "write `ref ...`"),
 ("ref-extra", 'fun f(s: text) gives whole\n  give s.length\nstop\nso a = "x"\nsay f(ref a)', "takes ownership"),
 ("mutref-needs-var", 'fun f(n: mut ref whole)\n  n += 1\nstop\nso k = 1\nf(mut ref k)', "can't be changed"),
 ("mutref-wrong-word", 'fun f(n: mut ref whole)\n  n += 1\nstop\nso var k = 1\nf(ref k)', "needs `mut ref`"),
 ("ref-no-change", 'fun f(xs: ref list of whole)\n  xs.push(1)\nstop\nso var a = [1]\nf(ref a)', "only borrowed"),
 ("ref-no-assign", 'fun f(n: ref whole)\n  n = 5\nstop\nso var k = 1\nf(ref k)', "only borrowed"),
 ("mutref-no-replace", 'fun f(xs: mut ref list of whole)\n  xs = [1]\nstop\nso var a = [2]\nf(mut ref a)', "can't be replaced as a whole"),
 ("exclusive", 'fun f(a: mut ref list of whole, b: ref list of whole)\n  a.push(1)\nstop\nso var x = [1]\nf(mut ref x, ref x)', "used twice in this call"),
 ("push-immutable", 'fun g()\n  so xs = [1]\n  xs.push(2)\nstop\ng()', "can't be changed"),
 ("item-immutable", 'fun g()\n  so xs = [1]\n  xs[0] = 2\nstop\ng()', "can't be changed"),
 ("ref-in-var", 'so a = 1\nso b = ref a', "only be used when passing"),
 ("ref-type-var", 'so x: ref whole = 1', "only be used for function parameters"),
 ("temp-mutref", 'fun f(xs: mut ref list of whole)\n  xs.push(1)\nstop\nf(mut ref [1])', "temporary can't be lent"),
 ("mut-self-immutable", 'record C\n  n: whole\nstop\nextend C\n  fun bump(mut self)\n    self.n += 1\n  stop\nstop\nso c = C(0)\nc.bump()', "can't be changed"),
 ("ref-self-change", 'record C\n  n: whole\nstop\nextend C\n  fun bump(self)\n    self.n += 1\n  stop\nstop\nso var c = C(0)\nc.bump()', "only borrowed"),
 ("stray-or", 'so n = 1\nor\n  say n', "does not belong"),
 ("unclosed", 'if true\n  say 1', "never closed"),
]
for name, src, msg in FAILS:
    p = write(src)
    b = run([PY, "fishc.py", "run", p])
    ok(f"fail:{name}", b[0] != 0 and msg in b[2], f"exit {b[0]} stderr={b[2]!r}")
    os.unlink(p)

# ---- the examples
for ex in ("hello", "stars", "matrix", "decimals", "or_else", "ownership"):
    p = f"examples/{ex}.fish"
    a, b = run([PY, "fish.py", "run", p]), run([PY, "fishc.py", "run", p])
    ok(f"example:{ex}", a[0] == b[0] == 0 and a[1] == b[1], b[2])

# ---- memory is really reused: these programs allocate far more than the 1 GiB heap in total
MEM = [
 ("mem-text-loop", 'so var total = 0\nfor i in 1 to 8000000\n  so s = "item number {i} with some padding text to make this one rather long, long, long, long"\n  total += s.length\nstop\nsay total', "710888896"),
 ("mem-list-loop", 'so var total = 0\nfor i in 1 to 2000000\n  so var xs = [i, i + 1, i + 2, i + 3, i + 4, i + 5, i + 6, i + 7, i + 8, i + 9, i + 10]\n  xs.push(i)\n  total += xs.length\nstop\nsay total', "24000000"),
 ("mem-record-loop", 'record Node\n  name: text\n  tags: list of text\nstop\nfun mk(i: whole) gives Node\n  give Node("node {i} padding padding padding padding padding padding", ["a{i}", "b{i}", "c{i}"])\nstop\nso var total = 0\nfor i in 1 to 2000000\n  so n = mk(i)\n  total += n.tags.length + n.name.length\nstop\nsay total', None),
 ("mem-fn-loop", 'fun build(i: whole) gives text\n  so a = "x{i}"\n  so b = a + "yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"\n  give b + a\nstop\nso var n = 0\nfor i in 1 to 6000000\n  n += build(i).length\nstop\nsay n', None),
]
for name, src, want in MEM:
    p = write(src)
    b = run([PY, "fishc.py", "run", p])
    good = b[0] == 0 and (want is None or b[1].strip() == want)
    ok(f"memory:{name}", good, f"exit {b[0]} out={b[1]!r} err={b[2][:200]!r}")
    os.unlink(p)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
