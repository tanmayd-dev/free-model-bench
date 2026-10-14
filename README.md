# model_eval

Overnight benchmark for the free `opencode/*` models.

Each model is given a plan-then-implement task in a small synthetic repo. Its
plan is scored by a panel of premium models (`agy`), its code is scored the
same way, and a hidden test suite scores it objectively. Results land in a
leaderboard. Everything is cached, so a daily run only does the work that is
actually new.

```bash
./scripts/run-eval.sh                  # daily: whatever is missing
./scripts/run-eval.sh --mode smoke     # 1 model x 1 task, ~10 min sanity check
./scripts/run-eval.sh --mode full --force   # monthly: re-run everything
python3 src/webui.py                   # control panel on 127.0.0.1:8765
```

No accounts, no build step, no services to configure. One directory, one
SQLite file.

---

## Quick start

```bash
# 1. make sure the two CLI tools the harness shells out to are on PATH
command -v opencode agy

# 2. static check of every manifest -- free, no model calls
python3 src/cli.py doctor

# 3. a cheap end-to-end run against one model
./scripts/run-eval.sh --mode smoke

# 4. the real thing, run overnight
./scripts/run-eval.sh --mode auto --budget-minutes 360

# 5. watch it / control it
python3 src/webui.py
open http://127.0.0.1:8765
```

`run-eval.sh` takes a `flock`, so a timer firing while you have a run going
exits immediately with code `75` instead of colliding.

---

## Modes

| Mode | Profile | Force | What it does |
|---|---|---|---|
| `smoke` | — | no | 1 model × 1 task, concurrency 1, 45 min budget. Sanity check. |
| `daily` / `auto` | `minimal` | no | Only units with no successful cached result. |
| `monthly` | `full` | yes | Every task, cache ignored — the full re-run. |
| `full` | `full` | no | Every task, but uses cache where it can. |
| `all` | — | no | Every task, no profile filter. |

`auto` is what the daily timer runs; it is `daily` plus a model-list refresh
and a health probe.

---

## Schedule

```bash
./scripts/install-systemd.sh              # install
./scripts/install-systemd.sh --uninstall  # remove
```

Installs systemd **user** units (not root, not system-wide):

| Unit | When | Command |
|---|---|---|
| `model-eval-daily.timer` | 03:00 daily | `run-eval.sh --mode auto` |
| `model-eval-monthly.timer` | 04:00 on the 1st | `run-eval.sh --mode full --force` |
| `model-eval-webui.service` | always | `python3 src/webui.py` |

```bash
systemctl --user list-timers 'model-eval-*'
systemctl --user start model-eval-daily.service   # run one by hand
tail -f state/live.log                            # follow it
```

User timers only fire while your session is alive. For runs after logout:
`loginctl enable-linger $USER`.

No systemd? Put `./scripts/run-eval.sh --mode auto` in cron.

---

## The control panel

`python3 src/webui.py` — a stdlib HTTP server bound to `127.0.0.1`, no auth,
no dependencies.

- **Leaderboard** — sortable; click a model for its per-task breakdown.
- **Runs** — every run with its prompt, plan, diff, verify output, judge
  scores and event log.
- **Tasks** — toggle a task, hidden test or deterministic check on/off, or
  change its weight. Writes an override; the manifest stays authoritative
  for everything you do not touch.
- **Models** — enable/disable a model, change its weight, refresh the list.
- **Jobs** — start/pause/stop a run and watch the log. These write the same
  flags `run-eval.sh` polls, so the UI and the CLI are one code path.
- **Settings** — default concurrency, budget and judge panel.

---

## The tasks

| Task | Stages | Profiles | Weight | What it measures |
|---|---|---|---|---|
| `e2e-feature` | plan → implement → verify → judge | minimal, full | 1.0 | End-to-end: plan a recurring-transaction + budget feature in an existing stdlib CLI, implement your own plan, pass 60 hidden tests. |
| `plan-api-refactor` | plan → judge | minimal, full | 0.6 | Planning alone: migrate a monolithic HTTP API to a layered architecture without one behaviour change. |

`plan-api-refactor` has no `implement` and no `verify` stage and no `hidden/`
directory — the plan *is* the artifact, graded by the rubric plus four
deterministic checks (read-only, substantive, names the real modules, broken
into ordered steps). It is in the suite partly to keep the harness honest
about stage subsets.

---

## Layout

```
config/tasks/<task_id>/     task definitions (manifest-driven)
  manifest.yaml             id, stages, timeouts, scoring weights, judges
  task.md                   the brief shown to the model
  prompt-plan.txt           plan-stage prompt ({{SPEC}})
  prompt-implement.txt      implement-stage prompt ({{SPEC}}, {{PLAN}})
  rubric-*.txt              what the judges are told to grade against
  fixture/                  the repo the model sees and edits
  hidden/                   pytest suite the model never sees
  checks.py                 deterministic checks (no model, no tests)
  reference/                reference implementation, used by calibrate.py
src/                        harness
web/                        control panel (vanilla HTML/JS/CSS)
state/                      <— all state lives here
  eval.db                   SQLite (WAL): models, tasks, runs, judges, jobs
  live.log                  progress log
  jobs/run-*.log            one log per run
  runs/<run_id>/            artifacts: prompt.md, plan.md, patch.diff, verify.json
results/
  leaderboard.{md,json}     generated output
  history/YYYY-MM.json      snapshot per month
```

`state/` is the single source of truth. Delete it and the harness rebuilds
its registries — you only lose scored results. Point it elsewhere with
`MODEL_EVAL_STATE=/path`.

---

## How a unit is scored

A *unit* is `(task, model)`. It runs as:

1. **plan** — read-only `plan` agent in a throwaway copy of the fixture → `plan.md`.
2. **implement** — `build` agent gets the plan and the fixture → a diff.
3. **verify** — in a *second* throwaway copy: hidden tests + deterministic
   checks. No model call. Hidden tests are copied in **after** the model
   finishes, so the model can never read them.
4. **judge** — three premium models grade `plan.md` and the diff against the
   rubric, blind to which model produced them.

Scores are 0–100:

| Component | Source | Default weight |
|---|---|---|
| `judge_plan` | plan rubric, mean of panel | 0.30 |
| `tests` | hidden test pass rate | 0.50 |
| `judge_code` | code rubric, mean of panel | 0.20 |

Weights live in `manifest.yaml` → `scoring`. **Blank means not measured, not
zero** — a model with no run yet does not get dragged to the bottom.

The judge panel is deliberately three models of different vendors, and the
spread between them is reported as `Judge Δ`. A high Δ means the judgement
itself is noisy for that model, so read the score with suspicion.

---

## Extensibility

### Add or disable a test

Hidden tests are discovered from `hidden/`, so this is a file operation, not
a code change:

```bash
# add
cp my_test.py config/tasks/e2e-feature/hidden/test_mine.py

# disable one (also editable from the Tasks tab in the UI)
# config/tasks/e2e-feature/manifest.yaml
exclude_tests: [test_forecast.py]
```

Disabling a hidden test does **not** invalidate cached model runs — the
fixture the model saw is unchanged. Only `verify` is re-scored.

Two separate hashes enforce this: `fixture_sha` (what the model sees) and
`verify_sha` (what grades it). Edit a hidden test and only the second moves.

### Add a task

```bash
mkdir -p config/tasks/my-task
```

Only the files the declared stages actually need are required:

```yaml
# config/tasks/my-task/manifest.yaml
id: my-task
stages: [plan, judge]        # any subset of plan | implement | verify | judge
profiles: [minimal, full]
```

| Stage | Needs |
|---|---|
| `plan` | `task.md`, `prompt-plan.txt`, `rubric-plan.txt`, `fixture/` |
| `implement` | `task.md`, `prompt-implement.txt` (uses `{{SPEC}}` and `{{PLAN}}`), `fixture/` |
| `verify` | `fixture/tests/` (visible), `hidden/` (optional), `checks.py` (optional) |
| `judge` | the rubric for whichever of the above produced an artifact |

Leave out `prompt-implement.txt` and a `{{PLAN}}` reference and the implement
stage is simply never reached — `plan-api-refactor` is exactly that shape.
Omit `scoring` and the manifest defaults to `judge_plan: 1.0`.

The registry picks it up on the next run (`sync_registry`). Nothing in
`src/` needs editing. Add `profiles: [minimal]` if the daily run should cover
it.

### Disable a task or a model

Untick it in the **Tasks** / **Models** tab, or set `enabled: false` in the
manifest / database. It stops being scheduled but keeps its results.

### Add a judge

Append an `agy` model id to `judge_models` in `manifest.yaml` (or set a global
panel in **Settings**). Effective panel, most specific wins:

```
per-task Tasks-tab override  >  Settings (blank = manifest)  >  manifest  >  code default
```

Existing judge results are kept; only the new judge runs — the panel check is
per judge, not "has this artifact been judged at all", so a run that only got
two of three judges gets the third on the next pass.

**Editing a rubric re-judges and nothing else.** Rubrics are not model-visible,
so they are deliberately excluded from the fixture hash: a rubric tweak
invalidates the cached scores (and drops the superseded rows) but leaves the
model's run alone. The two hashes are:

| Hash | Covers | Changing it re-runs |
|---|---|---|
| `fixture_sha` | model-visible inputs — `fixture/`, `task.md`, `prompt-*.txt`, `gold-plan.md` | the model |
| `verify_sha` | grading inputs — `manifest.yaml`, `hidden/`, `checks.py`, the declared rubrics, a task-local judge schema | verification |

`reference/` is excluded from both: it is what calibrate grades against and is
never handed to a model. A bare name in an ignore list covers everything under
that directory — without which `hidden/` was quietly hashed into `fixture_sha`
and editing a hidden test forced a full re-run, exactly what the split exists
to prevent.

### Overrides are deltas only

Everything the control panel writes goes to the `config` table in
`state/eval.db`, never into a manifest. Saving a field whose value already
matches the manifest **clears** its override instead of storing it — a value
that merely repeats the file is invisible in that file and would shadow every
later edit to it. `doctor` reports any such row that predates this rule.

---

## Caching and retries

- A run whose cache key already holds an `ok` result is skipped. Keys include
  the stage, model, prompt hash, fixture hash and harness version — change
  any of those and that unit re-runs.
- `error` / `timeout` / `cancelled` results are **not** trusted; they are
  retried, up to 3 attempts. Clear them manually with
  `python3 src/cli.py reset-failed`.
- Transient provider failures (HTTP 4xx/5xx, upstream JSON errors) are
  retried inside the stage with 10s/30s/90s backoff. A retry of the implement
  stage rebuilds the sandbox first, so attempt 2 never inherits attempt 1's
  half-written tree.
- Permanent failures (model deprecated, unavailable, not found) are **not**
  retried.
- `timeout` is our own resource limit, so it is never retried — a second
  attempt would cost another full timeout for the same likely answer.

### Health probe

Before a run, each model that is about to be used gets one trivial prompt
(`python3 src/cli.py probe`). This costs seconds and saves a 15-minute
timeout on a model that was deprecated this afternoon.

- answers → `known`
- permanent error → `delisted`, and it is dropped from this run
- transient error → `degraded`, still enabled so tomorrow can heal it

---

## Pre-flight (`doctor`)

```bash
python3 src/cli.py doctor              # everything enabled
python3 src/cli.py doctor --all        # include disabled tasks
python3 src/cli.py doctor --task e2e-feature --quiet
```

`doctor` is static: no model calls, no network, no cost, well under a second.
Every run does it first, so the class of mistake that would otherwise surface
hours into an overnight job is reported in the first second:

* a manifest that does not load (unparseable YAML, `id` ≠ directory name)
* a missing or empty `task.md`, prompt, rubric or schema
* a `{{placeholder}}` that is not supplied — `render()` raises `KeyError` and
  kills the stage mid-run — or a prompt that never references `{{SPEC}}`,
  which silently drops the brief
* **a rubric whose dimensions the judge's JSON schema does not accept.**
  `agy --json-schema` enforces the schema, so judges would answer with the
  generic dimension set and the rubric would be decorative.
* `exclude_tests` / `exclude_checks` naming files that do not exist, or a
  component in `scoring` that `task_score()` does not know (dropped, which
  quietly renormalises the rest)
* a list field written without brackets — `stages: plan, implement` parses as
  one string, and every reader then iterates *characters*
* a `checks.py` that does not import, has the wrong signature, raises, or
  returns something `run_checks()` cannot read (it runs every check once
  against a throwaway copy of the fixture)
* a stored override that merely repeats its manifest value — invisible in the
  file, and it shadows every later edit to it

Exit code is `1` on any `FAIL`. The runner uses `doctor.failures()` directly:
a `global` failure aborts the job, and a task failure skips **only that task**,
so one bad manifest cannot cost you the night.

---

## CLI

```bash
python3 src/cli.py run    --mode auto --concurrency 3 [--force] [--no-judge]
                                          [--no-probe] [--no-refresh]
                                          [--models a,b] [--tasks x] [--profile p]
python3 src/cli.py doctor         # static config check -- no model calls, no cost
python3 src/cli.py refresh          # reconcile `opencode models` with the registry
python3 src/cli.py probe            # health-check every model
python3 src/cli.py score            # re-score and rewrite the leaderboard
python3 src/cli.py status           # one-screen summary
python3 src/cli.py tasks | models   # dump a registry as JSON
python3 src/cli.py log              # tail state/live.log
python3 src/cli.py reset-failed     # forget failures so they are retried
```

Equivalently: `./scripts/run-eval.sh --mode auto` (adds the lock + log).

---

## Costs and resources

Free models report `cost: 0`, so **token counts are the accounting basis** —
they are what the leaderboard reports per model. Judge spend is the only real
money: three premium calls per judged artifact, and judges are cached by
`(run, judge, artifact_sha, rubric_sha)`.

Concurrency defaults to 3, but **units are serialized per model**, so a free
model is never hit with parallel requests. The judge lock is global: `agy`
calls happen one at a time.

Expected shape of a nightly `auto` run: fast for models already scored
(cache miss only), and one full plan→implement→verify→judge cycle for any
model that is new or previously failed.

---

## Troubleshooting

| Symptom | Look at |
|---|---|
| A run exits `75` | Another `run-eval.sh` holds the lock — that is intentional. |
| A task is skipped at startup | `python3 src/cli.py doctor` — it reports the `FAIL` and why. |
| A model scores blank | No successful run yet; check **Runs** for `error`/`timeout`. |
| `Judge Δ` is high | The judges disagree for that model — treat the score as noisy. |
| Judge was skipped | `--no-judge`, or agy failed; `python3 src/cli.py score` re-scores. |
| A model should be gone | `python3 src/cli.py probe` — permanent errors mark it `delisted`. |
| Want a clean slate | Move `state/` aside and re-run `refresh`. |

```bash
python3 src/cli.py status
tail -100 state/live.log
```

---

## Calibrating a task

`scripts/calibrate.py` runs the **reference implementation** through the same
verification path. If the reference does not score ~100, the task is broken,
not the model:

```bash
python3 scripts/calibrate.py
```

It also reports how many hidden tests the *pristine* fixture fails, which is
the floor a model has to clear to prove it did anything.
