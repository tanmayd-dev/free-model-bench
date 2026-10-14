# shopfront — layered architecture migration plan

## Context

`shopfront` is a small product catalogue and order API written in 2022. It
works, it has a visible test suite that has been green since the day it was
written, and it has been extended by three different people in the four
years since.

Everything HTTP-shaped lives in `shopfront/api.py`: the route table,
authentication, request validation, SQL, business rules and response
rendering. `shopfront/db.py` opens a brand new SQLite connection for every
single query. `shopfront/auth.py` keeps sessions in a module-level dict.

Product has asked for an API v2 with a different JSON shape, a second
worker process, and webhook callbacks. Those cannot be built on top of the
current layout.

## What is being asked

Write the **migration plan** that takes `shopfront` from where it is to a
layered architecture, without a single behaviour change along the way.

The target architecture is:

```
shopfront/
  http/        routing, request parsing, response rendering, status codes
  services/    business rules and validation -- no SQL, no HTTP
  repositories/ SQL and transactions, one module per aggregate
  auth/        password hashing, sessions, authorisation rules
```

## Hard constraints

1. **No behaviour change.** Every one of the visible tests in `tests/` must
   still pass after every single step. The response bodies, status codes and
   error strings must be byte-for-byte identical unless this plan says
   otherwise explicitly.
2. **No big bang.** The system must be deployable and green after each step.
   A step that leaves the tree broken is not an acceptable step.
3. **Stdlib only.** No new dependencies.
4. **The move is incremental.** `shopfront.api.handle` is the only public
   entry point today (`__main__.py` and `tests/test_api.py` both call it).
   It must keep working throughout, and it is acceptable for it to become a
   thin shim by the end.
5. **Known problems must be addressed**, at minimum:
   - `db.py` opens and closes a connection per query, so a single order
     creation performs a full connect/`executescript` round trip several
     times, and the stock deduction plus line insert is not atomic.
   - `_list_products` interpolates the `q` parameter directly into SQL.
   - sessions in `auth.SESSIONS` are process-local and cannot be revoked
     across workers.
   - the route table is an order-sensitive `if`/`elif` chain.
6. **Do not fix everything.** This is a plan for restructuring, not a rewrite.
   Anything that changes observable behaviour must be called out as a
   separate, explicitly-flagged follow-up rather than slipped into a
   refactor step.

## What the plan must contain

1. **Target layout** — which existing function moves to which new module,
   and which functions get split.
2. **Step-by-step migration** — an ordered sequence of steps, each of which
   leaves the tree green, with the verification for that step named.
3. **Seam inventory** — the places where the current code makes the move
   hard (module-level state, connection-per-query, the `handle` shim), and
   how each is dealt with.
4. **Risk register** — what can go wrong, what the blast radius is, and how
   you would detect it.
5. **Behaviour-preservation strategy** — concretely, how you prove the
   refactor did not change behaviour beyond "the tests pass".
6. **Explicit non-goals** — what you are deliberately leaving alone.
7. **v2 follow-ups** — a short list of the behaviour changes (JSON shape,
   atomic stock deduction, revocable sessions) that must NOT be smuggled
   into the refactor, so they can be scheduled separately.
