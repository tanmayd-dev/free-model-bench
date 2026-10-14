"""The three per-task stages: plan, implement, verify.

Each stage:
  1. computes its cache key,
  2. returns early if we already have a good answer,
  3. otherwise does the (possibly expensive) work and records it.

`verify` never spends a model call -- it re-grades an existing implement run,
which is what makes "disable a test" and "add a test" cheap.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from evallib import (
    HARNESS_VERSION,
    RUNS_DIR,
    claim_run,
    find_cached_run,
    finish_run,
    drop_sandbox,
    log_live,
    make_diff,
    make_sandbox,
    q1,
    redact,
    run_opencode_retry,
    sha_text,
    snapshot_tree,
    tree_diff,
    write_json,
)
from tasks import Task, render

# --------------------------------------------------------------------------
# Deterministic checks (config/tasks/<id>/checks.py)
# --------------------------------------------------------------------------


def run_checks(
    task: Task,
    *,
    sandbox: Path,
    plan: str = "",
    diff: str = "",
    changed: list[str] | None = None,
    created: list[str] | None = None,
) -> dict[str, Any]:
    """Execute every `check_*` function in the task's checks.py."""
    changed, created = changed or [], created or []
    checks_path = task.path / "checks.py"
    if not checks_path.exists():
        return {}
    spec = importlib.util.spec_from_file_location(f"{task.id}_checks", checks_path)
    if spec is None or spec.loader is None:
        return {"_error": f"cannot load {checks_path}"}
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"{type(exc).__name__}: {exc}"}

    ctx = {
        "sandbox": sandbox,
        "fixture": task.path / "fixture",
        "spec": task.spec,
        "plan": plan,
        "diff": diff,
        "changed": changed,
        "created": created,
    }
    out: dict[str, Any] = {}
    for name in task.active_checks:
        fn = getattr(mod, name, None)
        if fn is None:
            out[name] = {"ok": False, "score": 0.0, "detail": "check not found"}
            continue
        try:
            out[name] = fn(ctx)
        except Exception as exc:  # noqa: BLE001
            out[name] = {"ok": False, "score": 0.0,
                         "detail": f"{type(exc).__name__}: {exc}"}
    out["_score"] = _mean(
        [float(v.get("score", 0.0)) for v in out.values() if isinstance(v, dict)]
    )
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# --------------------------------------------------------------------------
# Stage 1: plan
# --------------------------------------------------------------------------


def plan_key(task: Task) -> str:
    return task.cache_key(
        "plan",
        prompt=sha_text(task.prompt_plan),
        spec=sha_text(task.spec),
    )


def run_plan_stage(
    task: Task,
    model: str,
    *,
    control_check=None,
    retry_failed: bool = False,
) -> Any:
    key = plan_key(task)
    cached = find_cached_run(task.id, "plan", model, key, task.fixture_sha,
                             retry_failed=retry_failed)
    if cached is not None:
        log_live(f"cache hit  plan    {task.id} {model}")
        return cached

    run_id = claim_run(task.id, "plan", model, key, task.fixture_sha)
    artifacts = RUNS_DIR / run_id
    artifacts.mkdir(parents=True, exist_ok=True)
    sandbox = make_sandbox(task.path, run_id)
    prompt = render(task.prompt_plan, SPEC=task.spec)
    (artifacts / "prompt.md").write_text(prompt)

    log_live(f"running    plan    {task.id} {model}")
    try:
        res = run_opencode_retry(
            label=f"plan {task.id} {model}",
            cancel_on=control_check,
            model=model,
            agent="plan",
            prompt=prompt,
            cwd=sandbox,
            timeout_s=task.timeouts["plan"],
            artifacts_dir=artifacts,
            control_check=control_check,
        )
        (artifacts / "plan.md").write_text(res.text or "")

        changed, created, _deleted = tree_diff(task.path / "fixture", sandbox)
        diff = "" if not changed and not created else make_diff(task.path / "fixture", sandbox)
        checks = run_checks(task, sandbox=sandbox, plan=res.text, diff=diff,
                            changed=changed, created=created)
        write_json(artifacts / "checks.json", checks)
    except Exception as exc:  # noqa: BLE001 -- a stage failure must not wedge the row
        drop_sandbox(sandbox)
        log_live(f"plan stage failed {task.id} {model}: {exc}", kind="error")
        finish_run(run_id, status="error", artifacts_dir=str(artifacts),
                   error=f"{type(exc).__name__}: {exc}")
        return q1("SELECT * FROM runs WHERE id=?", (run_id,))
    finally:
        if sandbox.exists():
            drop_sandbox(sandbox)

    finish_run(
        run_id,
        status=res.status,
        duration_s=round(res.duration_s, 3),
        tokens_in=res.tokens_in,
        tokens_out=res.tokens.get("output", 0),
        tokens_reasoning=res.tokens.get("reasoning", 0),
        tokens_cache_read=res.tokens.get("cache_read", 0),
        tool_calls=res.tool_calls,
        shell_calls=res.shell_calls,
        edit_calls=res.edit_calls,
        files_touched=len(changed) + len(created),
        active_checks=json.dumps(task.active_checks),
        checks_payload=json.dumps(checks),
        artifacts_dir=str(artifacts),
        session_id=res.session_id,
        error=res.error or None,
    )
    return q1("SELECT * FROM runs WHERE id=?", (run_id,))


# --------------------------------------------------------------------------
# Stage 2: implement
# --------------------------------------------------------------------------


def implement_key(task: Task, source: str, plan_text: str) -> str:
    return task.cache_key(
        "implement",
        source=source,
        prompt=sha_text(task.prompt_implement),
        plan=sha_text(plan_text),
        spec=sha_text(task.spec),
    )


def run_implement_stage(
    task: Task,
    model: str,
    plan_text: str,
    *,
    source: str = "own",
    control_check=None,
    retry_failed: bool = False,
) -> Any:
    key = implement_key(task, source, plan_text)
    cached = find_cached_run(task.id, "implement", model, key, task.fixture_sha,
                             retry_failed=retry_failed)
    if cached is not None:
        log_live(f"cache hit  impl    {task.id} {model} [{source}]")
        return cached

    run_id = claim_run(task.id, "implement", model, key, task.fixture_sha, source=source)
    artifacts = RUNS_DIR / run_id
    artifacts.mkdir(parents=True, exist_ok=True)
    sandbox = make_sandbox(task.path, run_id)
    prompt = render(task.prompt_implement, SPEC=task.spec, PLAN=plan_text)
    (artifacts / "prompt.md").write_text(prompt)

    log_live(f"running    impl    {task.id} {model} [{source}]")

    def rebuild() -> dict:
        # A retry must not inherit the previous attempt's half-written tree.
        nonlocal sandbox
        drop_sandbox(sandbox)
        sandbox = make_sandbox(task.path, run_id)
        return dict(
            model=model,
            agent="build",
            prompt=prompt,
            cwd=sandbox,
            timeout_s=task.timeouts["implement"],
            auto=True,
            artifacts_dir=artifacts,
            control_check=control_check,
        )

    try:
        res = run_opencode_retry(
            label=f"implement {task.id} {model} [{source}]",
            cancel_on=control_check,
            rebuild=rebuild,
            model=model,
            agent="build",
            prompt=prompt,
            cwd=sandbox,
            timeout_s=task.timeouts["implement"],
            auto=True,
            artifacts_dir=artifacts,
            control_check=control_check,
        )

        fixture = task.path / "fixture"
        changed, created, deleted = tree_diff(fixture, sandbox)
        diff = make_diff(fixture, sandbox)
        (artifacts / "patch.diff").write_text(diff)
        checks = run_checks(task, sandbox=sandbox, plan=plan_text, diff=diff,
                            changed=changed, created=created)
        write_json(artifacts / "checks.json", checks)
        write_json(artifacts / "tree_index.json",
                   {"changed": changed, "created": created, "deleted": deleted})

        # Snapshot the post-model tree BEFORE hidden tests are copied in.
        snapshot_tree(sandbox, artifacts / "tree")
    except Exception as exc:  # noqa: BLE001 -- a stage failure must not wedge the row
        log_live(f"implement stage failed {task.id} {model}: {exc}", kind="error")
        finish_run(run_id, status="error", artifacts_dir=str(artifacts),
                   error=f"{type(exc).__name__}: {exc}")
        return q1("SELECT * FROM runs WHERE id=?", (run_id,))
    finally:
        if sandbox.exists():
            drop_sandbox(sandbox)

    finish_run(
        run_id,
        status=res.status,
        duration_s=round(res.duration_s, 3),
        tokens_in=res.tokens_in,
        tokens_out=res.tokens.get("output", 0),
        tokens_reasoning=res.tokens.get("reasoning", 0),
        tokens_cache_read=res.tokens.get("cache_read", 0),
        tool_calls=res.tool_calls,
        shell_calls=res.shell_calls,
        edit_calls=res.edit_calls,
        files_touched=len(changed) + len(created),
        active_checks=json.dumps(task.active_checks),
        checks_payload=json.dumps(checks),
        artifacts_dir=str(artifacts),
        session_id=res.session_id,
        error=res.error or None,
    )
    return q1("SELECT * FROM runs WHERE id=?", (run_id,))


# --------------------------------------------------------------------------
# Stage 3: verify (no model call)
# --------------------------------------------------------------------------


def verify_key(task: Task, impl_run_id: str) -> str:
    return task.cache_key(
        "verify",
        impl=impl_run_id,
        active_tests=task.active_tests_sha,
        verify=task.verify_sha,
        harness=HARNESS_VERSION,
    )


def _run_pytest(cwd: Path, target: str, junit: Path, timeout: int) -> dict[str, Any]:
    if not (cwd / target).exists():
        return {"collected": 0, "passed": 0, "failed": 0, "errors": 1, "skipped": 0,
                "tests": [], "error": f"{target} not present"}
    cmd = [
        sys.executable, "-m", "pytest", "-q", "--no-header",
        "-p", "no:cacheprovider", "--continue-on-collection-errors",
        "--junitxml", str(junit), target,
    ]
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"collected": 0, "passed": 0, "failed": 0, "errors": 1, "skipped": 0,
                "tests": [], "error": f"pytest timed out after {timeout}s"}
    except OSError as exc:
        return {"collected": 0, "passed": 0, "failed": 0, "errors": 1, "skipped": 0,
                "tests": [], "error": str(exc)}

    summary = _parse_junit(junit)
    summary["rc"] = proc.returncode
    # pytest exit code 5 = no tests collected at all
    if proc.returncode == 5 and summary["collected"] == 0:
        summary["error"] = "no tests collected"
    return summary


def _parse_junit(junit: Path) -> dict[str, Any]:
    empty = {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "tests": []}
    if not junit.exists():
        return empty
    try:
        root = ET.fromstring(junit.read_text(errors="replace"))
    except ET.ParseError:
        return empty
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    out = dict(empty)
    for suite in suites:
        cases = list(suite.findall("testcase"))
        out["collected"] += len(cases)
        for case in cases:
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            if case.find("failure") is not None:
                out["failed"] += 1
                out["tests"].append({"test": name, "status": "failed"})
            elif case.find("error") is not None:
                out["errors"] += 1
                out["tests"].append({"test": name, "status": "error"})
            elif case.find("skipped") is not None:
                out["skipped"] += 1
                out["tests"].append({"test": name, "status": "skipped"})
            else:
                out["passed"] += 1
                out["tests"].append({"test": name, "status": "passed"})
    return out


def run_verify_stage(
    task: Task,
    model: str,
    impl_row: Any,
    *,
    retry_failed: bool = False,
) -> Any:
    """Grade an implement run with hidden tests + deterministic checks."""
    if impl_row is None or impl_row["status"] not in ("ok", "timeout"):
        # 'error' means the model produced nothing usable; there is nothing to grade.
        return None
    key = verify_key(task, impl_row["id"])
    cached = find_cached_run(task.id, "verify", model, key, task.fixture_sha,
                             retry_failed=retry_failed)
    if cached is not None:
        log_live(f"cache hit  verify  {task.id} {model}")
        return cached

    artifacts = Path(impl_row["artifacts_dir"])
    tree = artifacts / "tree"
    if not tree.exists():
        finish_run(claim_run(task.id, "verify", model, key, task.fixture_sha),
                   status="error", error="missing post-run tree; re-run implement")
        return None

    run_id = claim_run(task.id, "verify", model, key, task.fixture_sha,
                       source=str(impl_row["source"] or "own"))
    out_dir = RUNS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Work on a throwaway copy so the artifact tree stays pristine.
    work = out_dir / "work"
    shutil.copytree(tree, work, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    active = task.hidden_tests
    if active:
        dest = work / "hidden_tests"
        dest.mkdir(parents=True, exist_ok=True)
        for name in active:
            shutil.copy2(task.hidden_dir / name, dest / name)

    timeout = task.timeouts["verify"]
    hidden = _run_pytest(work, "hidden_tests", out_dir / "junit-hidden.xml", timeout) \
        if active else {"collected": 0, "passed": 0, "failed": 0, "errors": 0,
                        "skipped": 0, "tests": [], "error": "all hidden tests excluded"}
    visible = _run_pytest(work, "tests", out_dir / "junit-visible.xml", timeout)

    # Re-run the deterministic checks against the final tree.
    fixture = task.path / "fixture"
    changed, created, _deleted = tree_diff(fixture, work)
    diff = make_diff(fixture, work)
    checks = run_checks(task, sandbox=work, plan="", diff=diff,
                        changed=changed, created=created)
    shutil.rmtree(work, ignore_errors=True)

    payload = {
        "hidden": hidden,
        "visible": visible,
        "checks": checks,
        "active_tests": active,
        "files_touched": len(changed) + len(created),
    }
    write_json(out_dir / "verify.json", payload)

    total = max(int(hidden["collected"]) - int(hidden["skipped"]), 0)
    passed = int(hidden["passed"])
    finish_run(
        run_id,
        status="ok",
        duration_s=0.0,
        parent_id=impl_row["id"],
        tests_passed=passed,
        tests_total=total,
        active_tests=json.dumps(active),
        active_checks=json.dumps(task.active_checks),
        checks_payload=json.dumps(payload),
        artifacts_dir=str(out_dir),
        error=None,
    )
    log_live(f"verified   {task.id} {model} hidden {passed}/{total}")
    return q1("SELECT * FROM runs WHERE id=?", (run_id,))


# --------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------


def plan_text_for(task: Task, run_row: Any) -> str:
    if run_row is None:
        return ""
    p = Path(run_row["artifacts_dir"]) / "plan.md"
    return p.read_text() if p.exists() else ""


def redacted_plan(task: Task, run_row: Any) -> str:
    return redact(plan_text_for(task, run_row))
