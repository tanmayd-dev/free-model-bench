"""Scoring and the leaderboard.

Component scores (each 0..100):
    judge_plan   mean of the premium judges' rubric score for the plan
    judge_code   mean of the premium judges' rubric score for the diff
    tests        hidden-test pass rate from the verify stage

Per-task weights come from the task's manifest `scoring` block. Components
that could not be measured (judge down, run failed) are dropped and the
remaining weights renormalised, so a single missing judge never zeroes a model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evallib import RESULTS_DIR, execute, now_iso, q, write_json
from judge import judge_summary
from tasks import Task, load_tasks


# --------------------------------------------------------------------------
# Latest run per (task, model, stage, source)
# --------------------------------------------------------------------------


def latest_runs() -> dict[tuple[str, str, str, str], Any]:
    """Newest row per key. Old fixture revisions are ignored automatically."""
    out: dict[tuple[str, str, str, str], Any] = {}
    for row in q("SELECT * FROM runs ORDER BY started ASC, id ASC"):
        if row["status"] == "running":
            continue
        key = (row["task_id"], row["model"], row["stage"], row["source"] or "own")
        out[key] = row
    return out


# --------------------------------------------------------------------------
# Component scores
# --------------------------------------------------------------------------


def recompute_scores() -> dict[str, int]:
    """Fold judge results and test results back onto runs.score."""
    runs = latest_runs()
    n_plan = n_impl = n_verify = 0
    for row in runs.values():
        stage = row["stage"]
        if stage == "verify":
            total = int(row["tests_total"] or 0)
            score = (100.0 * int(row["tests_passed"] or 0) / total) if total > 0 else None
            if score is not None:
                execute("UPDATE runs SET score=? WHERE id=?", (round(score, 2), row["id"]))
                n_verify += 1
            continue
        if stage not in ("plan", "implement"):
            continue
        summary = judge_summary(row["id"])
        if summary["mean"] is not None:
            execute("UPDATE runs SET score=? WHERE id=?",
                    (summary["mean"], row["id"]))
            if stage == "plan":
                n_plan += 1
            else:
                n_impl += 1
    return {"plan": n_plan, "implement": n_impl, "verify": n_verify}


def verify_for_impl(impl_row: Any) -> Any | None:
    """Find the verify run whose parent is this implement run."""
    if impl_row is None:
        return None
    rows = q("SELECT * FROM runs WHERE parent_id=? ORDER BY started DESC",
             (impl_row["id"],))
    if rows:
        return rows[0]
    # Legacy rows (written before parent_id existed): newest verify for this
    # task/model/source with a completed status.
    rows = q(
        "SELECT * FROM runs WHERE task_id=? AND model=? AND stage='verify' "
        "AND source=? ORDER BY started DESC",
        (impl_row["task_id"], impl_row["model"], impl_row["source"] or "own"),
    )
    return rows[0] if rows else None


def task_components(task: Task, model: str, source: str = "own",
                    runs: dict | None = None) -> dict[str, Any]:
    runs = runs if runs is not None else latest_runs()
    plan_row = runs.get((task.id, model, "plan", "own"))
    impl_row = runs.get((task.id, model, "implement", source))
    verify_row = verify_for_impl(impl_row)

    comps: dict[str, Any] = {}
    if plan_row is not None and plan_row["score"] is not None:
        comps["judge_plan"] = float(plan_row["score"])
    if impl_row is not None and impl_row["score"] is not None:
        comps["judge_code"] = float(impl_row["score"])
    if verify_row is not None:
        total = int(verify_row["tests_total"] or 0)
        if total > 0:
            comps["tests"] = 100.0 * int(verify_row["tests_passed"] or 0) / total
    return {
        "components": comps,
        "plan_run": plan_row,
        "impl_run": impl_row,
        "verify_run": verify_row,
    }


def task_score(task: Task, model: str, source: str = "own",
               runs: dict | None = None) -> float | None:
    info = task_components(task, model, source, runs=runs)
    weights = task.scoring
    got = info["components"]
    avail = {k: v for k, v in got.items() if k in weights and v is not None}
    if not avail:
        return None
    total_w = sum(weights[k] for k in avail)
    if total_w <= 0:
        return None
    return round(sum(weights[k] * avail[k] for k in avail) / total_w, 2)


# --------------------------------------------------------------------------
# Leaderboard
# --------------------------------------------------------------------------


def _weighted_mean(pairs: list[tuple[float, float]]) -> float | None:
    usable = [(w, v) for w, v in pairs if v is not None and w > 0]
    if not usable:
        return None
    tw = sum(w for w, _ in usable)
    return round(sum(w * v for w, v in usable) / tw, 2)


def leaderboard(
    *,
    profile: str | None = None,
    source: str = "own",
    models: list[str] | None = None,
) -> list[dict[str, Any]]:
    tasks = [t for t in load_tasks() if profile is None or profile in t.profiles]
    all_models = [r["id"] for r in q("SELECT id FROM models ORDER BY id")]
    wanted = models or all_models
    runs = latest_runs()

    rows: list[dict[str, Any]] = []
    for mid in wanted:
        mrows = q("SELECT * FROM models WHERE id=?", (mid,))
        if not mrows:
            continue
        mrow = mrows[0]
        per_task = []
        plan_c, code_c, test_c = [], [], []
        tokens_in = tokens_out = tools = 0
        wall = 0.0
        failures = 0
        spreads = []
        ns: list[int] = []
        for t in tasks:
            info = task_components(t, mid, source, runs=runs)
            sc = task_score(t, mid, source, runs=runs)
            per_task.append({"task": t.id, "score": sc,
                             "components": info["components"]})
            w = t.weight * (float(mrow["weight"] or 1.0))
            if "judge_plan" in info["components"]:
                plan_c.append((w, info["components"]["judge_plan"]))
            if "judge_code" in info["components"]:
                code_c.append((w, info["components"]["judge_code"]))
            if "tests" in info["components"]:
                test_c.append((w, info["components"]["tests"]))
            for r in (info["plan_run"], info["impl_run"], info["verify_run"]):
                if r is None:
                    continue
                if r["status"] in ("error", "timeout", "cancelled"):
                    failures += 1
                tokens_in += int(r["tokens_in"] or 0)
                tokens_out += int(r["tokens_out"] or 0)
                tools += int(r["tool_calls"] or 0)
                wall += float(r["duration_s"] or 0.0)
                if r["stage"] != "verify":
                    s = judge_summary(r["id"])
                    if s["spread"] is not None:
                        spreads.append(s["spread"])
                    if r["score"] is not None:
                        # How many judges actually contributed to this score.
                        # A panel of 1 is not the same evidence as a panel of 3.
                        ns.append(int(s["n"]))

        composite = _weighted_mean(
            [(t.weight * float(mrow["weight"] or 1.0), p["score"])
             for t, p in zip(tasks, per_task)]
        )
        rows.append(
            {
                "model": mid,
                "status": mrow["status"],
                "enabled": bool(mrow["enabled"]),
                "composite": composite,
                "plan_rubric": _weighted_mean(plan_c),
                "code_rubric": _weighted_mean(code_c),
                "tests": _weighted_mean(test_c),
                "tasks": per_task,
                "tasks_scored": sum(1 for p in per_task if p["score"] is not None),
                "tasks_total": len(tasks),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "tool_calls": tools,
                "wall_clock_s": round(wall, 1),
                "failures": failures,
                "judge_spread": round(max(spreads), 2) if spreads else None,
                "judge_n": min(ns) if ns else None,
                "last_run": _last_run(mid),
            }
        )

    rows.sort(key=lambda r: (r["composite"] is None, -(r["composite"] or 0.0)))
    return rows


def _last_run(mid: str) -> str | None:
    row = q("SELECT MAX(started) AS m FROM runs WHERE model=?", (mid,))[0]
    return row["m"]


def history_path(month: str | None = None) -> Path:
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    return RESULTS_DIR / "history" / f"{month}.json"


def write_leaderboard(rows: list[dict[str, Any]], *, profile: str | None = None) -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": now_iso(),
        "profile": profile,
        "rows": rows,
    }
    write_json(RESULTS_DIR / "leaderboard.json", payload)
    (RESULTS_DIR / "leaderboard.md").write_text(render_markdown(rows, profile=profile))
    hist = history_path()
    hist.parent.mkdir(parents=True, exist_ok=True)
    write_json(hist, payload)
    return payload


def _fmt(v: Any, nd: int = 1) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_markdown(rows: list[dict[str, Any]], *, profile: str | None = None) -> str:
    lines = [
        "# Model evaluation leaderboard",
        "",
        f"Generated {now_iso()}"
        + (f" · profile `{profile}`" if profile else ""),
        "",
        "Scores are 0–100. `Composite` weights each task's components by the "
        "task manifest. **Blank means not measured** — it is not a zero.",
        "",
        "| # | Model | Composite | Plan (rubric) | Code (rubric) | Hidden tests "
        "| Tasks | Wall clock | Tokens in/out | Tools | Failures | Judges "
        "| Judge spread |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(rows, start=1):
        mark = "" if r["enabled"] else " ⚠️disabled"
        lines.append(
            "| {i} | `{m}`{mark} | {c} | {p} | {cd} | {t} | {ts}/{tt} | {w} | "
            "{ti}/{to} | {tc} | {f} | {j} | {sp} |".format(
                i=i,
                m=r["model"],
                mark=mark,
                c=_fmt(r["composite"]),
                p=_fmt(r["plan_rubric"]),
                cd=_fmt(r["code_rubric"]),
                t=_fmt(r["tests"]),
                ts=r["tasks_scored"],
                tt=r["tasks_total"],
                w=_fmt(r["wall_clock_s"], 0) + "s",
                ti=r["tokens_in"],
                to=r["tokens_out"],
                tc=r["tool_calls"],
                f=r["failures"],
                j=r["judge_n"] if r["judge_n"] is not None else "—",
                sp=_fmt(r["judge_spread"]),
            )
        )
    lines += ["", "## Per-task breakdown", ""]
    for r in rows:
        lines.append(f"### `{r['model']}` — composite {_fmt(r['composite'])}")
        lines.append("")
        lines.append("| Task | Score | judge_plan | judge_code | tests |")
        lines.append("|---|---:|---:|---:|---:|")
        for t in r["tasks"]:
            c = t["components"]
            lines.append(
                f"| {t['task']} | {_fmt(t['score'])} | {_fmt(c.get('judge_plan'))} "
                f"| {_fmt(c.get('judge_code'))} | {_fmt(c.get('tests'))} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"
