{ Ownership in Fish.
  Every value has one owner. `so b = a` moves a into b. Lend with `ref` (read)
  or `mut ref` (change). Memory is freed automatically when the owner ends. }

record Player
  name: text
  scores: list of whole
stop

extend Player
  fun total(self) gives whole
    so var t = 0
    for s in self.scores
      t += s
    stop
    give t
  stop
  fun record_score(mut self, s: whole)
    self.scores.push(s)
  stop
stop

fun shout(t: ref text) gives text
  give "{t}!"
stop

fun bump(n: mut ref whole)
  n += 1
stop

so var p = Player("Ann", [])
p.record_score(10)
p.record_score(32)
say p, p.total()

so greeting = "hello"
say shout(ref greeting), shout(ref greeting)   # lent twice: still ours

so moved = greeting                            # moves it
say moved
{ say greeting   <- error: `greeting` was moved on line 33 }

so var count = 0
bump(mut ref count)
bump(mut ref count)
say count

so backup = p.copy()                           # an independent copy
p.record_score(1)
say p.total(), backup.total()
