# matrix.fish — matrix multiplication with nested lists
# Empty lists need a type so the compiler knows what they will hold.

fun mat_mul(a: list of list of whole, b: list of list of whole) gives list of list of whole
  so var out: list of list of whole = []
  for i in 0 until a.length
    so var row: list of whole = []
    for j in 0 until b[0].length
      so var s = 0
      for k in 0 until b.length
        s += a[i][k] * b[k][j]
      stop
      row.push(s)
    stop
    out.push(row)
  stop
  give out
stop

so a = [[1, 2], [3, 4]]
so b = [[5, 6], [7, 8]]
say "A = {a}"
say "B = {b}"
say "A*B = {mat_mul(a, b)}"
