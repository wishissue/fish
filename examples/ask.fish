so name = "Sunday"
so var apples = 13
say "hello {name}"
say "today i have {apples} apples"

so age = ask("what is your age? : ")


say "you are {age} years old"

so city = ask("enter your city : ")
say "You live in {city}"


so n = 5
for i in 1 to n
  so var row = ""
  so var k = 1
  while k is at most i
    row = row + "*"
    k += 1
  stop
  say row
stop
