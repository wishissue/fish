# stars.fish — classic star patterns in Fish
# Every `row` is a `so var` so it can be appended to.

fun repeat(s: text, n: whole) gives text
  so var out = ""
  so var i = 0
  while i is below n
    out = out + s
    i += 1
  stop
  give out
stop

# 1. right triangle
say "1. right triangle"
so n = 5
for i in 1 to n
  so var row = repeat("*", i)
  say row
stop
say ""

# 2. inverted triangle
say "2. inverted triangle"
for i in 1 to n
  so var row = repeat("*", n - i + 1)
  say row
stop
say ""

# 3. pyramid
say "3. pyramid"
for i in 1 to n
  so var row = repeat(" ", n - i) + repeat("*", 2 * i - 1)
  say row
stop
say ""

# 4. hollow square
say "4. hollow square"
for i in 1 to n
  so var row = ""
  so var j = 1
  while j is at most n
    if i is 1 or i is n or j is 1 or j is n
      row = row + "*"
    or
      row = row + " "
    stop
    j += 1
  stop
  say row
stop
say ""

# 5. diamond
say "5. diamond"
for i in 1 to n
  so var row = repeat(" ", n - i) + repeat("*", 2 * i - 1)
  say row
stop
for i in 1 until n
  so var row = repeat(" ", i) + repeat("*", 2 * (n - i) - 1)
  say row
stop
say ""
