# or_else.fish — `or if` / `or` are the short forms of `else if` / `else`

fun grade(n: whole) gives text
  if n is at least 90
    give "A"
  or if n is at least 80
    give "B"
  or
    give "C"
  stop
stop

say grade(95), grade(85), grade(10)

so n = 5
if n is 1
  say "one"
else if n is 5
  say "five"
or
  say "other"
stop
