"""LLM-as-judge, via agy's premium models.

Judging is blind: the model under test is never named, sandbox paths are
redacted, and judges are told that length is not scored. Results are cached
by (run, judge model, artifact content, rubric content), so re-judging after
a rubric tweak costs nothing for unchanged artifacts.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from evallib import (
    CONFIG_DIR,
    execute,
    log_live,
    now_iso,
    q,
    q1,
    redact,
    run_agy,
    sha_file,
    sha_text,
)
from tasks import Task

SCHEMA_DIR = CONFIG_DIR / "schemas"
MAX_BUNDLE_CHARS = 90_000
JUDGE_RETRIES = 2
# Backoff between judge attempts (retries index 0 and 1). Judges fail on rate
# limits and slow upstreams, so a short pause buys a lot.
JUDGE_BACKOFF = (5, 20)

# agy premium models have their own rate limits; judge calls are the only
# thing that touches them, so serialising them globally is both safe and
# cheap (judge time is small next to model-run time).
import threading  # noqa: E402

JUDGE_LOCK = threading.Lock()


class Judge:
    """A scoring bundle for one stage of one task."""

    def __init__(self, task: Task, stage: str):
        assert stage in ("plan", "implement")
        self.task = task
        self.stage = stage
        kind = "plan" if stage == "plan" else "code"
        self.rubric = task.rubric_plan if kind == "plan" else task.rubric_code
        self.schema_path = self._schema(kind)
        self.rubric_sha = sha_text(self.rubric + "\n" + self._schema_id())
        self.judge_models = task.judge_models

    def _schema(self, kind: str) -> Path:
        override = (self.task.manifest.get("judge_schema") or {}).get(kind)
        if override:
            p = self.task.path / str(override)
            if p.exists():
                return p
        return SCHEMA_DIR / f"{kind}.schema.json"

    def _schema_id(self) -> str:
        return sha_file(self.schema_path) if self.schema_path.exists() else "none"


# --------------------------------------------------------------------------
# Bundles
# --------------------------------------------------------------------------


def _listing(root: Path) -> str:
    lines = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel == "opencode.jsonc" or "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        lines.append(("/" if p.is_dir() else "  ") + rel)
    return "\n".join(lines)


def _clip(text: str, limit: int = MAX_BUNDLE_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit - limit // 2 - 40):]
    return head + f"\n\n... [{len(text) - limit} chars elided] ...\n\n" + tail


def build_plan_bundle(task: Task, run_row: Any, plan_text: str) -> str:
    files_touched = int(run_row["files_touched"] or 0) if run_row else 0
    evidence = [
        f"- files modified by the planning agent: {files_touched} "
        "(this stage is read-only; anything other than 0 is a violation)",
    ]
    # Plan-stage deterministic checks (e.g. "did the plan name the modules
    # that actually exist?") are facts the judge should weigh, exactly as the
    # code bundle does with verify-stage checks.
    if run_row is not None and run_row["checks_payload"]:
        try:
            checks = json.loads(run_row["checks_payload"])
        except json.JSONDecodeError:
            checks = {}
        for name, res in sorted(checks.items()):
            if name.startswith("_") or not isinstance(res, dict):
                continue
            mark = "PASS" if res.get("ok") else "FAIL"
            evidence.append(f"- deterministic check {name}: {mark} -- "
                            f"{res.get('detail', '')}")

    return redact(
        "\n\n".join(
            [
                "# FILE LISTING (repository as it exists today)\n\n```\n"
                + _listing(task.path / "fixture")
                + "\n```",
                "# PROJECT BRIEFING AND FEATURE REQUEST\n\n" + task.spec,
                "# THE PLAN UNDER REVIEW\n\n" + plan_text,
                "# HARNESS-GENERATED EVIDENCE\n" + "\n".join(evidence),
            ]
        )
    )


def build_code_bundle(task: Task, run_row: Any, verify_row: Any) -> str:
    artifact = Path(run_row["artifacts_dir"])
    diff = ""
    patch = artifact / "patch.diff"
    if patch.exists():
        diff = patch.read_text(errors="replace")

    evidence = ["- (no verification results: the implementation run did not complete)"]
    if verify_row is not None:
        payload = json.loads(verify_row["checks_payload"] or "{}")
        hidden = payload.get("hidden", {})
        visible = payload.get("visible", {})
        checks = payload.get("checks", {})
        total = int(hidden.get("collected", 0)) - int(hidden.get("skipped", 0))
        evidence = [
            f"- hidden tests: {hidden.get('passed', 0)}/{max(total, 0)} passed "
            f"({hidden.get('failed', 0)} failed, {hidden.get('errors', 0)} errors)",
            "- hidden tests that did NOT pass:",
        ]
        for t in hidden.get("tests", []):
            if t["status"] != "passed":
                evidence.append(f"    {t['status']:<7} {t['test']}")
        evidence.append(
            f"- visible (pre-existing) tests: {visible.get('passed', 0)} passed, "
            f"{visible.get('failed', 0)} failed, {visible.get('errors', 0)} errors"
        )
        for name, res in sorted(checks.items()):
            if name.startswith("_") or not isinstance(res, dict):
                continue
            mark = "PASS" if res.get("ok") else "FAIL"
            evidence.append(f"- deterministic check {name}: {mark} -- {res.get('detail', '')}")
        if int(verify_row["tests_total"] or 0) < len(payload.get("active_tests", [])):
            evidence.append("- WARNING: fewer hidden tests ran than are configured")

    return redact(
        "\n\n".join(
            [
                "# FILE LISTING (BEFORE the change)\n\n```\n"
                + _listing(task.path / "fixture")
                + "\n```",
                "# PROJECT BRIEFING AND FEATURE REQUEST\n\n" + task.spec,
                "# DIFF (AFTER the change)\n```diff\n" + diff + "\n```",
                "# EVIDENCE FROM THE HARNESS\n" + "\n".join(evidence),
            ]
        )
    )


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _dimensions(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    dims = payload.get("dimensions")
    if not isinstance(dims, dict):
        return {}
    out = {}
    for k, v in dims.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 10:
            out[str(k)] = n
    return out


def _score(payload: Any) -> float | None:
    dims = _dimensions(payload)
    if not dims:
        return None
    return round(sum(dims.values()) / len(dims) * 10.0, 2)  # 0..100


def _backoff(seconds: float, control_check=None) -> bool:
    """Sleep, but yield to pause/stop. Returns False if interrupted."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if control_check and control_check() in ("stop", "pause"):
            return False
        time.sleep(min(1.0, deadline - time.monotonic()))
    return True


def score_artifact(
    task: Task,
    stage: str,
    run_id: str,
    bundle: str,
    *,
    control_check=None,
) -> list[dict[str, Any]]:
    """Run every configured judge over one artifact. Returns judge rows."""
    j = Judge(task, stage)
    artifact_sha = sha_text(bundle)
    results: list[dict[str, Any]] = []

    if not j.rubric.strip():
        log_live(f"judge skipped {task.id} {stage}: no rubric", kind="warn")
        return results

    # A rubric edit supersedes the judgement of the old rubric. Drop those    # rows, or judge_summary() would average two rubrics together and
    # panel_missing() would keep seeing a panel it should consider empty.
    superseded = execute("DELETE FROM judges WHERE run_id=? AND rubric_sha<>?",
                         (run_id, j.rubric_sha))
    if superseded:
        log_live(f"rubric changed for {task.id} {stage}: dropped {superseded} "
                 f"superseded judge row(s)")

    for judge_model in j.judge_models:
        existing = q1(
            "SELECT * FROM judges WHERE run_id=? AND judge_model=? "
            "AND artifact_sha=? AND rubric_sha=?",
            (run_id, judge_model, artifact_sha, j.rubric_sha),
        )
        if existing is not None:
            results.append(dict(existing))
            continue

        if control_check and control_check() in ("stop", "pause"):
            continue

        prompt = _prompt(task, j, bundle)
        payload, error = None, ""
        for attempt in range(JUDGE_RETRIES + 1):
            if control_check and control_check() in ("stop", "pause"):
                break
            with JUDGE_LOCK:
                res = run_agy(
                    model=judge_model,
                    prompt=prompt,
                    schema_path=j.schema_path,
                    timeout_s=task.timeouts["judge"],
                )
            if res.status == "ok":
                payload = res.payload
                error = ""
                break
            error = res.error
            if attempt < JUDGE_RETRIES:
                delay = JUDGE_BACKOFF[attempt]
                log_live(
                    f"judge retry {task.id} {stage} {judge_model} after "
                    f"{error[:110]} -- next attempt in {delay}s",
                    kind="warn",
                )
                if not _backoff(delay, control_check):
                    break

        score = _score(payload)
        if score is None:
            # Deliberately NOT written to the judges table. Whether an artifact
            # still needs judging is decided by counting judge rows, so
            # caching a failure would mean it is never judged again.
            reason = error or "judge returned no usable dimensions"
            log_live(f"judge FAIL {task.id} {stage} {judge_model}: {reason[:200]}",
                     kind="warn")
            results.append({"run_id": run_id, "judge_model": judge_model,
                            "score": None, "payload": payload})
            continue

        row = {
            "run_id": run_id,
            "judge_model": judge_model,
            "artifact_sha": artifact_sha,
            "rubric_sha": j.rubric_sha,
            "payload": json.dumps(payload, default=str),
            "score": score,
            "created": now_iso(),
        }
        execute(
            "INSERT INTO judges(run_id, judge_model, artifact_sha, rubric_sha, "
            "payload, score, created) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id, judge_model, artifact_sha, rubric_sha) "
            "DO UPDATE SET payload=excluded.payload, score=excluded.score, "
            "created=excluded.created",
            (row["run_id"], row["judge_model"], row["artifact_sha"],
             row["rubric_sha"], row["payload"], row["score"], row["created"]),
        )
        log_live(f"judged     {task.id} {stage} {judge_model} -> {score}")
        results.append(row)

    return results


def _prompt(task: Task, j: Judge, bundle: str) -> str:
    return (
        "You are a strict senior engineer scoring work produced by another "
        "engineer. You do not know, and must not guess, which model produced "
        "it. Score it against the rubric using ONLY the evidence provided.\n\n"
        "The evidence below is a redacted engineering dossier: sandbox paths "
        "and model identifiers have been replaced with placeholders.\n\n"
        "Apply the evidence hierarchy: measured test results outrank your "
        "impressions of the diff; the diff outranks prose.\n\n"
        "=== RUBRIC ===\n"
        + j.rubric
        + "\n\n=== EVIDENCE ===\n"
        + _clip(bundle)
        + "\n\nReturn the scoring object in the required schema."
    )


# --------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------


def panel_missing(task: Task, stage: str, run_id: str) -> bool:
    """True when the configured panel has not scored this artifact under the
    rubric in force *now*.

    Two things make a run need judging again:

    * a judge with no scored row. Failures are deliberately never written
      (see :func:`score_artifact`), otherwise one failed attempt would mean
      that judge never gets tried again -- but it also means "has it been
      judged?" has to ask per judge, not "is the count > 0", which left
      partial panels in place for ever.
    * a rubric edit, which changes ``rubric_sha`` and so invalidates every
      cached judgement of the old rubric.
    """
    want = set(task.judge_models)
    if not want:
        return False
    j = Judge(task, stage)
    if not j.rubric.strip():
        return False  # score_artifact skips these; nothing to judge
    have = {r["judge_model"] for r in q(
        "SELECT judge_model FROM judges WHERE run_id=? AND rubric_sha=? "
        "AND score IS NOT NULL",
        (run_id, j.rubric_sha),
    )}
    return not want.issubset(have)


def judge_summary(run_id: str) -> dict[str, Any]:
    rows = q("SELECT judge_model, payload, score FROM judges WHERE run_id=?", (run_id,))
    scores = [float(r["score"]) for r in rows if r["score"] is not None]
    dims: dict[str, list[int]] = {}
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        for k, v in _dimensions(payload).items():
            dims.setdefault(k, []).append(v)
    return {
        "n": len(scores),
        "mean": round(sum(scores) / len(scores), 2) if scores else None,
        "min": min(scores) if scores else None,
        "max": max(scores) if scores else None,
        "spread": round(max(scores) - min(scores), 2) if scores else None,
        "models": [r["judge_model"] for r in rows],
        "dimensions": dims,
        "raw": [dict(r) for r in rows],
    }
