# hello.fish — the very basics

so name = "World"
so var count = 0

say "Hello, {name}!"
say "Two plus three is {2 + 3}"

fun greet(who: text) gives text
  give "Nice to meet you, {who}!"
stop

say greet("Fish")

for i in 1 to 3
  count += 1
  say "step {i}: count is {count}"
stop

say "Final count: {count}"
