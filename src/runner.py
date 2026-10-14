"""The overnight orchestrator: resolve what needs doing, then do it.

Design notes
------------
* A *unit* is one (task, model) pair; the whole chain
  plan -> implement -> verify -> judge runs inside it, in that order.
* Units for the SAME model are serialised (per-model lock) so we never hit
  one free model with parallel requests; different models run concurrently
  up to ``--concurrency``.
* Every stage commits to SQLite the moment it finishes, so a Ctrl-C, a time
  budget, or a crash loses nothing. "Resuming" is just running again: the
  cache skips everything already done.
* ``pause`` and ``stop`` both exit cleanly between stages. Pausing is
  resumed by running again.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from evallib import (
    JOBS_DIR,
    STATE_DIR,
    create_job,
    ensure_layout,
    execute,
    find_cached_run,
    log_live,
    make_control_check,
    now_iso,
    q,
    update_job,
)
from judge import build_code_bundle, build_plan_bundle, panel_missing, score_artifact
from probe import probe_all
from rank import leaderboard, recompute_scores, verify_for_impl, write_leaderboard
from refresh_models import refresh, runnable_models
from stages import (
    plan_key,
    plan_text_for,
    run_implement_stage,
    run_plan_stage,
    run_verify_stage,
)
from tasks import Task, load_tasks, sync_registry

_NEEDS_PLAN = re.compile(r"\{\{\s*PLAN\s*\}\}")


@dataclass
class Options:
    mode: str = "auto"
    profile: str | None = None
    tasks: list[str] | None = None
    models: list[str] | None = None
    concurrency: int = 3
    budget_minutes: float | None = None
    force: bool = False
    retry_failed: bool = True
    refresh_models: bool = True
    probe_models: bool = True
    judge: bool = True
    #: empty = use whatever the task manifest declares (own, gold, or both)
    sources: list[str] = field(default_factory=list)


def source_for(task: Task, opts: Options) -> list[str]:
    declared = list(task.implement_from)
    if not opts.sources:
        return declared
    wanted = [s for s in opts.sources if s in declared]
    return wanted or declared


# --------------------------------------------------------------------------
# Pending-work detection
# --------------------------------------------------------------------------


def _unit_pending(task: Task, model: str, opts: Options) -> bool:
    if opts.force:
        return True
    stages = set(task.stages)
    need_judge = opts.judge and "judge" in stages

    if "plan" in stages:
        row = find_cached_run(task.id, "plan", model, plan_key(task), task.fixture_sha,
                              retry_failed=opts.retry_failed)
        if row is None:
            return True
        if need_judge and panel_missing(task, "plan", row["id"]):
            return True

    if "implement" in stages:
        for source in source_for(task, opts):
            row = _latest(task.id, model, "implement", source)
            if row is None or row["status"] not in ("ok", "timeout"):
                return True
            if need_judge and panel_missing(task, "implement", row["id"]):
                return True

    if "verify" in stages:
        for source in source_for(task, opts):
            impl = _latest(task.id, model, "implement", source)
            if impl is None:
                return True
            vrow = _latest(task.id, model, "verify", source)
            if vrow is None or vrow["parent_id"] != impl["id"]:
                return True
    return False


def _latest(task_id: str, model: str, stage: str, source: str | None = None):
    rows = q(
        "SELECT * FROM runs WHERE task_id=? AND model=? AND stage=? "
        "ORDER BY started DESC, id DESC",
        (task_id, model, stage),
    )
    for r in rows:
        if source is None or (r["source"] or "own") == source:
            return r
    return None


def resolve_units(opts: Options) -> list[tuple[Task, str]]:
    tasks = load_tasks(profile=opts.profile)
    if opts.tasks:
        wanted = set(opts.tasks)
        tasks = [t for t in tasks if t.id in wanted]
    models = runnable_models(opts.models)
    return [(t, m) for m in models for t in tasks]


def pending_units(units: list[tuple[Task, str]], opts: Options) -> list[tuple[Task, str]]:
    out: list[tuple[Task, str]] = []
    for task, model in units:
        try:
            if _unit_pending(task, model, opts):
                out.append((task, model))
        except Exception as exc:  # noqa: BLE001
            log_live(f"pending check failed for {task.id}/{model}: {exc}", kind="warn")
            out.append((task, model))
    return out


# --------------------------------------------------------------------------
# Forced invalidation (monthly re-run)
# --------------------------------------------------------------------------


def force_clear(tasks: list[str], models: list[str]) -> int:
    """Delete cached runs (and their judge rows) so they are redone from scratch."""
    if not tasks or not models:
        return 0
    t_ph = ",".join("?" for _ in tasks)
    m_ph = ",".join("?" for _ in models)
    run_ids = [r["id"] for r in q(
        f"SELECT id FROM runs WHERE task_id IN ({t_ph}) AND model IN ({m_ph})",
        tuple(tasks) + tuple(models))]
    if not run_ids:
        return 0
    r_ph = ",".join("?" for _ in run_ids)
    execute(f"DELETE FROM judges WHERE run_id IN ({r_ph})", tuple(run_ids))
    for r in q(f"SELECT artifacts_dir FROM runs WHERE id IN ({r_ph})", tuple(run_ids)):
        d = r["artifacts_dir"]
        if d and (d == str(STATE_DIR) or str(STATE_DIR) in d):
            shutil.rmtree(d, ignore_errors=True)
    return execute(f"DELETE FROM runs WHERE id IN ({r_ph})", tuple(run_ids))


# --------------------------------------------------------------------------
# One unit: plan -> implement -> verify -> judge
# --------------------------------------------------------------------------


def run_unit(
    task: Task,
    model: str,
    opts: Options,
    *,
    model_lock: threading.Lock,
    control_check,
) -> dict[str, Any]:
    result: dict[str, Any] = {"task": task.id, "model": model, "stages": {}}
    stages = set(task.stages)
    needs_plan = bool(_NEEDS_PLAN.search(task.prompt_implement or ""))
    stop = lambda: control_check() in ("stop", "pause")  # noqa: E731

    plan_row = None
    if "plan" in stages:
        with model_lock:
            plan_row = run_plan_stage(task, model, control_check=control_check,
                                      retry_failed=opts.retry_failed)
        result["stages"]["plan"] = plan_row["status"] if plan_row else "missing"
        if stop():
            return result

        if opts.judge and "judge" in stages and plan_row is not None:
            ptext = plan_text_for(task, plan_row)
            if ptext.strip() and plan_row["status"] in ("ok", "timeout"):
                bundle = build_plan_bundle(task, plan_row, ptext)
                score_artifact(task, "plan", plan_row["id"], bundle,
                               control_check=control_check)
            if stop():
                return result

    plan_text = plan_text_for(task, plan_row) if plan_row is not None else ""

    if "implement" in stages:
        for source in source_for(task, opts):
            text = (task.gold_plan or "") if source == "gold" else plan_text
            if needs_plan and not text.strip():
                result["stages"][f"implement/{source}"] = "skipped: no plan"
                continue

            with model_lock:
                impl_row = run_implement_stage(
                    task, model, text, source=source,
                    control_check=control_check, retry_failed=opts.retry_failed,
                )
            result["stages"][f"implement/{source}"] = \
                impl_row["status"] if impl_row else "missing"
            if impl_row is None:
                continue

            vrow = None
            if "verify" in stages:
                vrow = run_verify_stage(task, model, impl_row,
                                        retry_failed=opts.retry_failed)
                result["stages"][f"verify/{source}"] = (
                    f"{vrow['tests_passed']}/{vrow['tests_total']}" if vrow else "missing"
                )

            if opts.judge and "judge" in stages and impl_row["status"] in ("ok", "timeout"):
                bundle = build_code_bundle(task, impl_row, vrow)
                score_artifact(task, "implement", impl_row["id"], bundle,
                               control_check=control_check)
            if stop():
                return result
    return result


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _pre_flight(units: list[tuple[Task, str]]) -> list[tuple[Task, str]]:
    """Run the static config check before anything expensive.

    A broken manifest should cost *that task* the night, not the whole job --
    otherwise one missing rubric at 03:00 kills an otherwise healthy run. A
    broken environment (no `opencode`, no `agy`, no models) has no smaller
    scope than "everything", so that one raises and the job fails fast with
    the reason instead of spending an hour discovering it.
    """
    from doctor import failures

    problems = failures()
    if "global" in problems:
        raise RuntimeError("pre-flight failed: " + "; ".join(problems["global"]))

    task_problems = {k: v for k, v in problems.items() if k != "global"}
    if not task_problems:
        return units

    for tid, msgs in task_problems.items():
        for msg in msgs:
            log_live(f"pre-flight [{tid}]: {msg}", kind="error")
        log_live(f"pre-flight: skipping task {tid} for this run", kind="error")
    kept = [(t, m) for t, m in units if t.id not in task_problems]
    if units and not kept:
        raise RuntimeError(
            f"pre-flight failed for every selected task: "
            f"{', '.join(sorted(task_problems))}"
        )
    return kept


def execute_job(opts: Options, job_id: str = "") -> dict[str, Any]:
    ensure_layout()
    # Any row still marked 'running' belongs to a process that died: mark it
    # failed so it is not mistaken for a completed (or infinite) run.
    stale = q("SELECT id FROM runs WHERE status='running'")
    if stale:
        from evallib import execute as _execute

        _execute("UPDATE runs SET status='error', error='interrupted before completion' "
                 "WHERE status='running'")
        log_live(f"marked {len(stale)} stale running run(s) as failed", kind="warn")

    started = time.monotonic()
    summary: dict[str, Any] = {"mode": opts.mode, "started": now_iso()}

    if opts.refresh_models:
        refresh()
    sync_registry()

    units = resolve_units(opts)
    if opts.sources:
        # source_for() falls back to whatever the task declares when none of
        # the requested sources match, so say so rather than silently doing
        # something other than what was asked for.
        unsupported = sorted({t.id for t, _ in units
                              if not set(opts.sources) & set(t.implement_from)})
        if unsupported:
            log_live(f"--source {','.join(opts.sources)} is not declared by "
                     f"{', '.join(unsupported)}; those tasks fall back to their "
                     f"own declared sources", kind="warn")

    units = _pre_flight(units)

    if opts.probe_models and units:
        probed = probe_all(sorted({m for _, m in units}))
        dead = set(probed["dead"])
        if dead:
            log_live(f"skipping {len(dead)} model(s) the health probe marked "
                     f"dead: {', '.join(sorted(dead))}", kind="warn")
            units = [(t, m) for t, m in units if m not in dead]

    todo = pending_units(units, opts)
    log_live(f"job {job_id or '-'}: {len(units)} units considered, "
             f"{len(todo)} pending (mode={opts.mode})")

    if opts.force and todo:
        n = force_clear([t.id for t, _ in todo], sorted({m for _, m in todo}))
        log_live(f"--force cleared {n} cached run(s)")
        todo = units

    if job_id:
        update_job(job_id, progress_total=len(todo), progress_done=0,
                   message=f"{len(todo)} unit(s) pending")

    if not todo:
        summary.update({"units": len(units), "pending": 0, "completed": 0,
                        "status": "nothing-to-do"})
        log_live("nothing pending -- cache is warm")
        return summary

    model_locks: dict[str, threading.Lock] = {}
    control = make_control_check(job_id) if job_id else (lambda: "running")
    deadline = (started + opts.budget_minutes * 60) if opts.budget_minutes else None
    done = 0
    errors = 0
    stopped_reason: str | None = None
    state_lock = threading.Lock()

    def worker(task: Task, model: str) -> dict[str, Any]:
        ml = model_locks.setdefault(model, threading.Lock())
        return run_unit(task, model, opts, model_lock=ml, control_check=control)

    with ThreadPoolExecutor(max_workers=max(1, opts.concurrency)) as pool:
        futures = {pool.submit(worker, t, m): (t, m) for t, m in todo}
        for fut in as_completed(futures):
            task, model = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                res = {"task": task.id, "model": model,
                       "error": f"{type(exc).__name__}: {exc}"}
                errors += 1
                log_live(f"unit ERROR {task.id}/{model}: {exc}", kind="error")
            with state_lock:
                done += 1
                if job_id:
                    update_job(
                        job_id, progress_done=done,
                        current=f"{model} · {task.id}",
                        message=res.get("error")
                        or (", ".join(f"{k}={v}" for k, v in res.get("stages", {}).items())
                            or "ok"),
                    )
                log_live(f"[{done}/{len(todo)}] {task.id} · {model} :: "
                         f"{res.get('stages') or res.get('error')}")

            if control() in ("stop", "pause"):
                stopped_reason = f"job {control()}"
            elif deadline and time.monotonic() > deadline:
                stopped_reason = f"time budget of {opts.budget_minutes} min reached"
            if stopped_reason:
                pool.shutdown(wait=True, cancel_futures=True)
                break

    recompute_scores()
    rows = leaderboard(profile=opts.profile,
                       source=(opts.sources[0] if opts.sources else "own"))
    write_leaderboard(rows, profile=opts.profile)

    elapsed = time.monotonic() - started
    summary.update(
        {
            "units": len(units),
            "pending": len(todo),
            "completed": done,
            "errors": errors,
            "stopped_reason": stopped_reason,
            "elapsed_s": round(elapsed, 1),
            "status": "paused" if stopped_reason else "done",
            "finished": now_iso(),
        }
    )
    log_live(f"job finished: {summary}")
    return summary


def run(opts: Options) -> dict[str, Any]:
    """Entry point shared by the CLI and the web UI."""
    ensure_layout()
    log_path = JOBS_DIR / f"{now_iso().split('T')[0]}-{uuid.uuid4().hex[:8]}.log"
    job_id = create_job(opts.mode, dict(vars(opts)), log_path)
    try:
        summary = execute_job(opts, job_id)
        update_job(
            job_id,
            status=summary.get("status", "done"),
            finished=now_iso(),
            message=summary.get("stopped_reason") or summary.get("status", ""),
            progress_total=summary.get("pending", 0),
        )
        return {"job_id": job_id, **summary}
    except Exception as exc:  # noqa: BLE001
        update_job(job_id, status="failed", finished=now_iso(),
                   message=f"{type(exc).__name__}: {exc}")
        log_live(f"job {job_id} failed: {exc}", kind="error")
        raise
