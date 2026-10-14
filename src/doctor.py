"""Pre-flight configuration check. Static, free, and fast.

    python3 src/cli.py doctor             # every enabled task + global state
    python3 src/cli.py doctor --all       # include disabled tasks
    python3 src/cli.py doctor --quiet     # only warnings and failures

No model calls and no network: this is the check you want to run *before*
an overnight job, or have the runner run for you. It catches the class of
mistake that otherwise surfaces hours in -- a missing rubric, a typo'd
``{{placeholder}}``, a judge dimension the JSON schema does not accept --
either as a crash mid-stage or, worse, as a score that silently measures
the wrong thing.

``failures()`` is the subset the runner acts on: global failures abort the
job, task failures skip that one task so a single bad manifest cannot cost
you the night.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallib import (
    STATE_DIR,
    TASKS_DIR,
    ensure_layout,
    q,
)
from judge import Judge
from tasks import (  # noqa: E401
    MANIFEST_DEFAULTS,
    SCORE_COMPONENTS,
    Task,
    load_task,
    load_tasks,
    placeholders,
    sync_registry,
)

KNOWN_STAGES = ("plan", "implement", "verify", "judge")

# Rubric dimension headings: a bare lowercase word at column 0, which is how
# every rubric in this repo declares them under its DIMENSIONS section.
DIMENSION_RE = re.compile(r"(?m)^([a-z][a-z0-9_]{2,})\s*$")

PLAN_VARS = {"SPEC"}
IMPLEMENT_VARS = {"SPEC", "PLAN"}


@dataclass
class Finding:
    level: str      # ok | WARN | FAIL
    where: str      # "global" or a task id
    message: str

    def __str__(self) -> str:
        return f"{self.level:<4} {self.where:<20} {self.message}"


class _Report:
    """Collects findings for one scope (the run, or one task)."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.items: list[Finding] = []

    def ok(self, msg: str) -> None:
        self.items.append(Finding("ok", self.where, msg))

    def warn(self, msg: str) -> None:
        self.items.append(Finding("WARN", self.where, msg))

    def fail(self, msg: str) -> None:
        self.items.append(Finding("FAIL", self.where, msg))

    @property
    def failed(self) -> bool:
        return any(i.level == "FAIL" for i in self.items)


def _decode(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _equal(a: Any, b: Any) -> bool:
    """Loose structural equality for override-vs-manifest comparison."""
    if isinstance(a, dict) and isinstance(b, dict):
        try:
            return ({str(k): float(v) for k, v in a.items()} ==
                    {str(k): float(v) for k, v in b.items()})
        except (TypeError, ValueError):
            return dict(a) == dict(b)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return [str(v) for v in a] == [str(v) for v in b]
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except (FileNotFoundError, IsADirectoryError, OSError):
        return ""


# --------------------------------------------------------------------------
# global
# --------------------------------------------------------------------------


def check_global() -> list[Finding]:
    r = _Report("global")

    for tool, why in (("opencode", "runs the model under test"),
                      ("agy", "premium judges score the artifacts")):
        path = shutil.which(tool)
        if path:
            r.ok(f"{tool} on PATH ({path})")
        else:
            r.fail(f"{tool} not on PATH -- {why}")

    try:
        ensure_layout()
        probe = STATE_DIR / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        r.ok(f"state dir writable ({STATE_DIR})")
    except OSError as exc:
        r.fail(f"state dir not writable ({STATE_DIR}): {exc}")

    try:
        sync_registry()
    except Exception as exc:  # noqa: BLE001
        r.fail(f"registry sync failed: {type(exc).__name__}: {exc}")
        return r.items

    models = q("SELECT id, enabled FROM models")
    enabled = [m for m in models if m["enabled"]]
    if not models:
        r.fail("no models in the registry -- run `cli.py refresh`")
    elif not enabled:
        r.fail(f"{len(models)} models registered, none enabled -- nothing would run")
    else:
        r.ok(f"{len(enabled)}/{len(models)} models enabled")

    tasks = load_tasks(include_disabled=True)
    live = [t for t in tasks if t.enabled]
    if not tasks:
        r.fail(f"no tasks under {TASKS_DIR}")
    elif not live:
        r.fail(f"{len(tasks)} tasks registered, none enabled -- nothing would run")
    else:
        r.ok(f"{len(live)}/{len(tasks)} tasks enabled")

    # A task directory whose manifest does not load is invisible to every
    # other tool, because load_task() returns None and moves on.
    for d in sorted(p for p in TASKS_DIR.iterdir() if p.is_dir()) if TASKS_DIR.is_dir() else []:
        if d.name.startswith((".", "_")):
            continue
        if load_task(d.name) is None:
            r.fail(f"{d.name}/ does not load -- manifest.yaml missing, unparseable, "
                   f"or its id does not match the directory name")

    return r.items


# --------------------------------------------------------------------------
# per task
# --------------------------------------------------------------------------


def check_task(t: Task) -> list[Finding]:
    r = _Report(t.id)
    stages = list(t.stages)
    st = set(stages)

    # ---- structure --------------------------------------------------------
    unknown = [s for s in stages if s not in KNOWN_STAGES]
    if unknown:
        r.fail(f"unknown stage(s): {', '.join(unknown)} (known: {', '.join(KNOWN_STAGES)})")
    if not stages:
        r.fail("no stages declared -- the task would never do anything")
    if "verify" in st and "implement" not in st:
        r.fail("declares `verify` without `implement` -- there is nothing to verify")
    if not t.enabled:
        r.warn("task is disabled (results kept, not scheduled)")
    if not t.profiles:
        r.warn("no profiles -- the daily and monthly runs both filter on profiles, "
               "so this task would never be scheduled")
    else:
        r.ok(f"stages={','.join(stages) or '-'} profiles={','.join(t.profiles)} "
             f"weight={t.weight}")
    if t.weight <= 0:
        r.warn(f"weight={t.weight} -- this task cannot move the composite score")

    # ---- the brief --------------------------------------------------------
    spec = _read(t.path / "task.md")
    if not spec.strip():
        r.fail("task.md missing or empty -- it is the brief handed to the model "
               "at every stage")
    else:
        r.ok(f"task.md ({len(spec)} chars)")

    # ---- shape of the manifest -------------------------------------------
    # A field written as `stages: plan, implement` parses as one string, and
    # every reader does `for s in manifest.get("stages")` -- which then
    # iterates *characters*. The result is a task with two thousand nonsense
    # stages rather than an error, so catch the shape before anything reads it.
    for key in ("stages", "implement_from", "profiles", "exclude_tests",
                "exclude_checks", "judge_models"):
        raw = t.manifest.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            r.fail(f"manifest {key}: {raw!r} is a string, not a list -- a YAML "
                   f"list needs `[a, b]` or `- item` lines. Read as-is it "
                   f"iterates its characters.")
        elif not isinstance(raw, list):
            r.fail(f"manifest {key}: expected a list, got "
                   f"{type(raw).__name__} ({raw!r})")
        elif not raw and key in ("stages", "implement_from", "profiles"):
            r.fail(f"manifest {key}: empty list")
        elif any(not isinstance(v, (str, int, float, bool)) for v in raw):
            r.fail(f"manifest {key}: contains a non-scalar entry: {raw!r}")

    hidden = t.manifest.get("hidden_tests")
    if hidden is not None and hidden != "auto" and not isinstance(hidden, list):
        r.fail(f"manifest hidden_tests: {hidden!r} must be `auto` or a list of "
               f"file names")
    for key in ("timeouts", "rubrics", "judge_schema"):
        raw = t.manifest.get(key)
        if raw is not None and not isinstance(raw, dict):
            r.fail(f"manifest {key}: expected a mapping, got "
                   f"{type(raw).__name__} ({raw!r})")

    # ---- plan -------------------------------------------------------------
    if "plan" in st:
        tpl = _read(t.path / "prompt-plan.txt")
        if not tpl.strip():
            r.fail("plan stage: prompt-plan.txt missing or empty")
        else:
            _check_prompt(r, "prompt-plan.txt", tpl, PLAN_VARS)

    # ---- implement --------------------------------------------------------
    if "implement" in st:
        tpl = _read(t.path / "prompt-implement.txt")
        if not tpl.strip():
            r.fail("implement stage: prompt-implement.txt missing or empty")
        else:
            ph = _check_prompt(r, "prompt-implement.txt", tpl, IMPLEMENT_VARS)
            if {"own"} & set(t.implement_from) and "PLAN" not in ph:
                r.fail("implement_from includes `own` but prompt-implement.txt has no "
                       "{{PLAN}} -- the model's own plan would be silently dropped, so "
                       "every implement run would see an empty plan")
            elif "PLAN" in ph:
                r.ok(f"implement_from={','.join(t.implement_from)}")

        if "gold" in set(t.implement_from):
            if not _read(t.path / "gold-plan.md").strip():
                r.fail("implement_from includes `gold` but gold-plan.md is missing "
                       "or empty")
            else:
                r.ok("gold-plan.md present")

        if not (t.path / "fixture").is_dir():
            r.fail("fixture/ missing -- there is nothing to implement into")

    # ---- verify -----------------------------------------------------------
    if "verify" in st:
        fixture = t.path / "fixture"
        if not fixture.is_dir():
            r.fail("fixture/ missing -- verify has nothing to run against")
        else:
            if not (fixture / "tests").is_dir():
                r.warn("fixture/tests/ missing -- no visible regression signal, only "
                       "the hidden tests will be scored")

        declared = t.manifest.get("hidden_tests", "auto")
        if isinstance(declared, list):
            missing = [n for n in declared if not (t.hidden_dir / str(n)).exists()]
            if missing:
                r.fail(f"manifest hidden_tests names files that do not exist in "
                       f"hidden/: {', '.join(missing)}")
            elif declared:
                r.ok(f"{len(declared)} hidden tests declared explicitly")
        elif not t.hidden_tests_all:
            r.fail("hidden_tests is `auto` but hidden/ contains no test_*.py -- the "
                   "implementation would never be graded")
        else:
            r.ok(f"{len(t.hidden_tests_all)} hidden test files discovered")

        if t.hidden_tests_all and not t.hidden_tests:
            r.fail(f"every hidden test is excluded ({', '.join(t.hidden_tests_all)}) "
                   f"-- the tests component can never be earned")
        elif t.excluded_tests:
            r.ok(f"{len(t.hidden_tests)} of {len(t.hidden_tests_all)} hidden tests active "
                 f"after exclusions")

        stray = [n for n in t.excluded_tests if n not in t.hidden_tests_all]
        if stray:
            r.fail(f"exclude_tests names files that are not in hidden/: "
                   f"{', '.join(stray)}")

    # ---- judging ----------------------------------------------------------
    if "judge" in st:
        if not t.judge_models:
            r.fail("judge_models is empty -- every artifact would be unscored")
        else:
            r.ok(f"{len(t.judge_models)} judges: {', '.join(t.judge_models)}")

        for stage, kind in (("plan", "plan"), ("implement", "code")):
            if stage not in st:
                continue
            try:
                j = Judge(t, stage)
            except AssertionError:
                continue
            if not j.rubric.strip():
                r.fail(f"judge stage `{stage}`: no rubric -- score_artifact() skips it "
                       f"and the component stays blank for ever")
                continue
            r.ok(f"rubric for `{stage}` ({len(j.rubric)} chars)")

            if not j.schema_path.exists():
                r.fail(f"judge stage `{stage}`: schema not found at {j.schema_path}")
                continue
            try:
                doc = json.loads(_read(j.schema_path))
            except json.JSONDecodeError as exc:
                r.fail(f"judge stage `{stage}`: {j.schema_path.name} is not valid "
                       f"JSON: {exc}")
                continue

            schema_dims = set(
                (doc.get("properties", {}).get("dimensions", {}) or {})
                .get("properties", {}) or {}
            )
            rubric_dims = set(DIMENSION_RE.findall(j.rubric))
            if not schema_dims:
                r.fail(f"judge stage `{stage}`: {j.schema_path.name} declares no "
                       f"dimensions -- _score() would always return None and the run "
                       f"would never be scored")
            elif not rubric_dims:
                r.warn(f"judge stage `{stage}`: no dimension headings found in the "
                       f"rubric (expected a bare lowercase line per dimension); "
                       f"cannot verify it against the schema")
            else:
                off = sorted(rubric_dims - schema_dims)
                unused = sorted(schema_dims - rubric_dims)
                if off:
                    r.fail(f"judge stage `{stage}`: the rubric scores {off} but "
                           f"{j.schema_path.name} does not accept them (schema has "
                           f"{sorted(schema_dims)}). agy enforces --json-schema, so "
                           f"judges would answer with the generic dimensions and the "
                           f"rubric would be decorative. Add a task-local schema via "
                           f"`judge_schema: {{ {kind}: ... }}`.")
                elif unused:
                    r.fail(f"judge stage `{stage}`: {j.schema_path.name} requires {unused} "
                           f"which the rubric never mentions -- judges would be scoring "
                           f"an unpublished criterion")
                else:
                    r.ok(f"`{stage}` rubric and schema agree on "
                         f"{len(schema_dims)} dimensions")

    # ---- overrides --------------------------------------------------------
    # The website writes these; only genuine deltas belong there. A stored
    # value that repeats the manifest is invisible in the file and quietly
    # shadows every later edit to it.
    for row in q("SELECT key, value FROM config WHERE key LIKE ?",
                 (f"task:{t.id}:%",)):
        key = row["key"].split(":", 2)[-1]
        stored = _decode(row["value"])
        if key in MANIFEST_DEFAULTS and _equal(stored, t.manifest_default(key)):
            r.warn(f"override {row['key']} repeats the manifest value -- it would "
                   f"mask a later edit to manifest.yaml. Re-saving the field in "
                   f"the UI clears it.")
        else:
            r.ok(f"override {row['key']} = {stored!r}")

    # ---- scoring ----------------------------------------------------------
    scoring = t.scoring
    unknown_sc = sorted(set(scoring) - SCORE_COMPONENTS)
    if unknown_sc:
        r.fail(f"scoring names component(s) {unknown_sc} that task_score() does not "
               f"know; they would be dropped and the remaining weights renormalised "
               f"over a different set")
    for k, v in scoring.items():
        try:
            w = float(v)
        except (TypeError, ValueError):
            r.fail(f"scoring.{k} = {v!r} is not a number")
            continue
        if w <= 0:
            r.fail(f"scoring.{k} = {w} -- weights must be > 0")
    for comp, needed in (("judge_plan", "plan"), ("judge_code", "implement"),
                         ("tests", "verify")):
        if comp in scoring and needed not in st:
            r.warn(f"scoring includes `{comp}` but the task has no `{needed}` stage -- "
                   f"that weight is never earned")

    # ---- checks -----------------------------------------------------------
    checks_file = t.path / "checks.py"
    if checks_file.exists():
        stray = [c for c in t.excluded_checks if c not in t.check_names]
        if stray:
            r.fail(f"exclude_checks names unknown check(s): {', '.join(stray)} "
                   f"(checks.py defines {', '.join(t.check_names) or 'none'})")
        problem = _dry_run_checks(t)
        if problem:
            r.fail(f"checks.py: {problem}")
        else:
            r.ok(f"{len(t.active_checks)} deterministic checks load and run")
    elif t.excluded_checks:
        r.fail(f"exclude_checks is set but {t.id} has no checks.py")

    # ---- timeouts ---------------------------------------------------------
    for stage, value in t.timeouts.items():
        try:
            secs = float(value)
        except (TypeError, ValueError):
            r.fail(f"timeouts.{stage} = {value!r} is not a number")
            continue
        if secs <= 0:
            r.fail(f"timeouts.{stage} = {secs} -- must be > 0")
        elif stage == "judge" and secs < 60:
            r.warn(f"timeouts.judge = {int(secs)}s -- judges routinely take over a "
                   f"minute to reason over a bundle")
        elif stage in ("plan", "implement") and secs < 120:
            r.warn(f"timeouts.{stage} = {int(secs)}s is very short for a model that "
                   f"has to read the repository first")

    return r.items


def _check_prompt(rep: _Report, name: str, template: str,
                  allowed: set[str]) -> set[str]:
    """Validate one prompt template; returns the placeholders it uses.

    Two distinct ways to be wrong, both only discovered at run time otherwise:
    a placeholder that is not supplied makes ``render()`` raise KeyError and
    kill the stage, and a template that never references ``{{SPEC}}`` drops
    the entire brief on the floor while the stage still "succeeds".
    """
    ph = placeholders(template)
    stray = sorted(ph - allowed)
    if stray:
        rep.fail(
            f"{name} references {['{{' + s + '}}' for s in stray]}, but only "
            f"{['{{' + s + '}}' for s in sorted(allowed)]} are supplied -- render() "
            f"raises KeyError and the stage dies mid-run"
        )
    if "SPEC" not in ph:
        rep.fail(f"{name} never references {{{{SPEC}}}} -- task.md would be silently "
                 f"dropped and the model would see no brief at all")
    if not stray and "SPEC" in ph:
        rep.ok(f"{name} ({len(template)} chars, {len(ph)} placeholder(s))")
    return ph


def _dry_run_checks(t: Task) -> str | None:
    """Load checks.py and invoke every check_* against a throwaway copy.

    Catches the failure that only shows up once a unit is already running: a
    wrong signature, a typo'd ctx key, a check that raises, or a return value
    run_checks() cannot read.
    """
    path = t.path / "checks.py"
    spec = importlib.util.spec_from_file_location(f"{t.id}_doctor_checks", path)
    if spec is None or spec.loader is None:
        return f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001
        return f"import raised {type(exc).__name__}: {exc}"

    fixture = t.path / "fixture"
    if not fixture.is_dir():
        return None  # already reported as a fixture failure

    try:
        with tempfile.TemporaryDirectory(prefix=f"doctor-{t.id}-") as tmp:
            sandbox = Path(tmp) / "fixture"
            shutil.copytree(fixture, sandbox,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                          ".pytest_cache"))
            ctx: dict[str, Any] = {
                "sandbox": sandbox,
                "fixture": fixture,
                "spec": _read(t.path / "task.md"),
                "plan": "",
                "diff": "",
                "changed": [],
                "created": [],
            }
            for name in t.check_names:
                fn = getattr(mod, name, None)
                if not callable(fn):
                    return f"{name} is defined but not callable"
                try:
                    res = fn(ctx)
                except TypeError as exc:
                    return f"{name}(ctx) -> TypeError: {exc}"
                except Exception as exc:  # noqa: BLE001
                    return f"{name} raised {type(exc).__name__}: {exc}"
                if not isinstance(res, dict) or "ok" not in res or "score" not in res:
                    return (f"{name} returned {type(res).__name__} without both an "
                            f"`ok` and a `score` key")
                score = res.get("score")
                if isinstance(score, bool) or not isinstance(score, (int, float)):
                    return f"{name} returned score={score!r}; expected a number"
                if not 0.0 <= float(score) <= 1.0:
                    return f"{name} returned score={score!r}; expected 0.0..1.0"
    except OSError as exc:
        return f"could not stage a sandbox to run the checks in: {exc}"
    return None


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def doctor(*, include_disabled: bool = False,
           task_ids: list[str] | None = None) -> list[Finding]:
    """Every finding for this configuration.

    Never raises. The runner treats a ``FAIL`` as "skip that scope", so a bug
    in the checker itself must not take the whole job down with it -- that
    would be strictly worse than the config problem we were looking for.
    """
    try:
        out = check_global()
    except Exception as exc:  # noqa: BLE001
        out = [Finding("FAIL", "global",
                       f"checker raised {type(exc).__name__}: {exc}")]
    for t in load_tasks(include_disabled=True):
        if not include_disabled and not t.enabled:
            continue
        if task_ids and t.id not in task_ids:
            continue
        try:
            findings = check_task(t)
        except Exception as exc:  # noqa: BLE001
            findings = [Finding("FAIL", t.id,
                                f"checker raised {type(exc).__name__}: {exc}")]
        out.extend(findings or [Finding("ok", t.id, "no findings")])
    return out


def failures(findings: list[Finding] | None = None) -> dict[str, list[str]]:
    """``{scope: [messages]}`` for every scope with at least one FAIL.

    The runner uses this: a ``global`` entry aborts the job, any other key
    means "run everything except this task".
    """
    findings = doctor() if findings is None else findings
    out: dict[str, list[str]] = {}
    for f in findings:
        if f.level == "FAIL":
            out.setdefault(f.where, []).append(f.message)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="include disabled tasks")
    ap.add_argument("--task", action="append", default=None,
                    help="check only this task (repeatable)")
    ap.add_argument("--quiet", action="store_true",
                    help="only show warnings and failures")
    args = ap.parse_args(argv)

    findings = doctor(include_disabled=args.all, task_ids=args.task)
    for f in findings:
        if args.quiet and f.level == "ok":
            continue
        print(f"{f.level:<5} {f.where:<20} {f.message}")

    n_fail = sum(1 for f in findings if f.level == "FAIL")
    n_warn = sum(1 for f in findings if f.level == "WARN")
    n_ok = sum(1 for f in findings if f.level == "ok")
    print()
    print(f"{n_fail} failure(s), {n_warn} warning(s), {n_ok} check(s) passed")
    if n_fail:
        print("FAILED -- fix before running; the runner will skip these scopes.")
        return 1
    print("doctor: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
