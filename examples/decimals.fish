# decimals.fish — decimals never mix with whole numbers

so a = 2.5
say a + 4.0, a * 2.0, 1.0 / 3.0
say 0.1 + 0.2                      # shows 0.3, trailing zeros are trimmed

# convert on purpose, never by accident
say decimal(3) / 2.0, whole(9.99), sqrt(2.0)

fun half(x: decimal) gives decimal
  give x / 2.0
stop

say half(5.0), "half of 3.0 is {half(3.0)}"
