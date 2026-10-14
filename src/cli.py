#!/usr/bin/env python3
"""Command line front-end for the model evaluation harness.

    python3 src/cli.py run --mode auto --concurrency 3
    python3 src/cli.py run --mode smoke          # 1 model x 1 task, sanity check
    python3 src/cli.py run --mode full --force   # monthly re-run of everything
    python3 src/cli.py status
    python3 src/cli.py score
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evallib import (  # noqa: E402
    LIVE_LOG,
    ensure_layout,
    log_live,
    q,
    tail,
)
from refresh_models import refresh, runnable_models  # noqa: E402
from tasks import load_tasks, sync_registry, task_summary  # noqa: E402

PROFILES = {
    "smoke": None,   # handled specially
    "daily": "minimal",
    "auto": "minimal",
    "monthly": "full",
    "full": "full",
    "all": None,
}


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def cmd_run(args: argparse.Namespace) -> int:
    from runner import Options, run

    opts = Options(
        mode=args.mode,
        profile=args.profile or PROFILES.get(args.mode),
        tasks=_csv(args.tasks),
        models=_csv(args.models),
        concurrency=args.concurrency,
        budget_minutes=args.budget_minutes,
        force=args.force or args.mode == "monthly",
        retry_failed=not args.no_retry_failed,
        refresh_models=not args.no_refresh,
        probe_models=not args.no_probe,
        judge=not args.no_judge,
        sources=_csv(args.source) or [],
    )

    if args.mode == "smoke":
        models = runnable_models(opts.models)[:1] or ["<none>"]
        tasks = load_tasks()
        if not tasks:
            print("no tasks configured under config/tasks/", file=sys.stderr)
            return 2
        opts.models = models
        opts.tasks = [tasks[0].id]
        opts.concurrency = 1
        opts.budget_minutes = opts.budget_minutes or 45
        print(f"smoke: model={models} task={tasks[0].id}", file=sys.stderr)

    summary = run(opts)
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("status") in ("done", "nothing-to-do") else 1


def cmd_refresh(_args: argparse.Namespace) -> int:
    ensure_layout()
    sync_registry()
    out = refresh()
    print(json.dumps(out, indent=2))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from probe import probe_all

    ensure_layout()
    models = [m.strip() for m in (args.models or "").split(",") if m.strip()] or None
    out = probe_all(models)
    print(json.dumps({k: v for k, v in out.items() if k != "results"}, indent=2))
    for r in out["results"]:
        mark = "ok  " if r["ok"] else ("dead" if r.get("permanent") else "warn")
        print(f"  {mark} {r['model']:<44} {r.get('duration_s', 0):>6.1f}s  "
              f"{r.get('error') or ''}")
    return 1 if out["dead"] or out["degraded"] else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Pre-flight config check. Static: no model calls, no cost."""
    from doctor import main as doctor_main

    argv: list[str] = []
    if args.all:
        argv.append("--all")
    if args.quiet:
        argv.append("--quiet")
    for t in (args.task or []):
        argv += ["--task", t]
    return doctor_main(argv)


def cmd_score(args: argparse.Namespace) -> int:
    from rank import leaderboard, recompute_scores, write_leaderboard

    ensure_layout()
    sync_registry()
    counts = recompute_scores()
    rows = leaderboard(profile=args.profile)
    payload = write_leaderboard(rows, profile=args.profile)
    print(f"re-scored: {counts}")
    print(f"rows: {len(rows)}")
    print(f"wrote {payload['generated']}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    ensure_layout()
    models = q("SELECT * FROM models ORDER BY id")
    jobs = q("SELECT * FROM jobs ORDER BY started DESC LIMIT 5")
    runs = q("SELECT stage, status, COUNT(*) AS n FROM runs GROUP BY stage, status")
    judges = q("SELECT COUNT(*) AS n FROM judges")
    print(f"models : {len(models)} "
          f"(enabled {sum(1 for m in models if m['enabled'])})")
    print(f"tasks  : {len(load_tasks(include_disabled=True))} "
          f"(enabled {len(load_tasks())})")
    for r in runs:
        print(f"runs   : {r['stage']:<10} {r['status']:<10} {r['n']}")
    print(f"judges : {judges[0]['n']}")
    if jobs:
        j = jobs[0]
        print(f"last job: {j['id']} {j['status']} "
              f"{j['progress_done']}/{j['progress_total']} {j['message'] or ''}")
    return 0


def cmd_tasks(_args: argparse.Namespace) -> int:
    ensure_layout()
    sync_registry()
    print(json.dumps(task_summary(), indent=2))
    return 0


def cmd_models(_args: argparse.Namespace) -> int:
    ensure_layout()
    print(json.dumps([dict(r) for r in q("SELECT * FROM models ORDER BY id")], indent=2))
    return 0


def cmd_reset_failed(args: argparse.Namespace) -> int:
    from evallib import clear_failed_runs

    n = clear_failed_runs(only_today=args.today)
    print(f"cleared {n} failed run(s)")
    return 0


def cmd_log(_args: argparse.Namespace) -> int:
    for line in tail(LIVE_LOG, 200):
        sys.stdout.write(line)
    return 0


def build() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="model-eval", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the evaluation queue")
    run.add_argument("--mode", default="auto",
                     choices=list(PROFILES), help="auto (daily) / full (monthly) / smoke")
    run.add_argument("--profile", default=None,
                     help="override the task profile (minimal|full|<any task profile>)")
    run.add_argument("--models", default=None, help="comma-separated model ids")
    run.add_argument("--tasks", default=None, help="comma-separated task ids")
    run.add_argument("--concurrency", type=int, default=3)
    run.add_argument("--budget-minutes", type=float, default=None)
    run.add_argument("--force", action="store_true", help="ignore the cache")
    run.add_argument("--no-retry-failed", action="store_true")
    run.add_argument("--no-refresh", action="store_true", help="skip the model list refresh")
    run.add_argument("--no-probe", action="store_true",
                     help="skip the per-model health probe (saves ~2s per model)")
    run.add_argument("--no-judge", action="store_true", help="skip LLM judging (free)")
    run.add_argument("--source", default=None, help="own, gold, or both")
    run.set_defaults(func=cmd_run)

    for name, fn, help_ in (
        ("refresh", cmd_refresh, "reconcile opencode models with the registry"),
        ("score", cmd_score, "recompute scores and rewrite the leaderboard"),
        ("status", cmd_status, "one-screen summary"),
        ("tasks", cmd_tasks, "dump the task registry as JSON"),
        ("models", cmd_models, "dump the model registry as JSON"),
        ("log", cmd_log, "tail state/live.log"),
    ):
        sp = sub.add_parser(name, help=help_)
        if name == "score":
            sp.add_argument("--profile", default=None)
        if name == "reset-failed":
            sp.add_argument("--today", action="store_true")
        sp.set_defaults(func=fn)

    rf = sub.add_parser("reset-failed", help="clear failed runs so they are retried")
    rf.add_argument("--today", action="store_true")
    rf.set_defaults(func=cmd_reset_failed)

    pr = sub.add_parser("probe", help="cheap health check of every model in the registry")
    pr.add_argument("--models", default="", help="comma-separated ids (default: all)")
    pr.set_defaults(func=cmd_probe)

    doc = sub.add_parser("doctor",
                         help="pre-flight config check (no model calls, no cost)")
    doc.add_argument("--all", action="store_true", help="include disabled tasks")
    doc.add_argument("--task", action="append", default=None,
                     help="check only this task (repeatable)")
    doc.add_argument("--quiet", action="store_true",
                     help="only show warnings and failures")
    doc.set_defaults(func=cmd_doctor)

    return p


def main() -> int:
    args = build().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log_live("interrupted", kind="warn")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
