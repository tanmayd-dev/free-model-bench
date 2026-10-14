#!/usr/bin/env python3
"""Calibrate a task's hidden tests against two known states of the world.

  1. the reference implementation  -> every hidden test must PASS
  2. the pristine fixture          -> hidden tests must mostly FAIL

If (1) fails, the test suite or the reference is wrong and no model score
derived from it means anything. If (2) passes everything, the tests are too
weak to discriminate.

    python3 scripts/calibrate.py [task_id ...]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "config" / "tasks"


def run_pytest(cwd: Path, targets: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         "--junitxml", str(cwd / "junit.xml"), *targets],
        cwd=str(cwd), capture_output=True, text=True, timeout=600,
    )
    return proc.returncode, (proc.stdout + proc.stderr)[-6000:]


def copy(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))


def calibrate(task_dir: Path) -> bool:
    task_id = task_dir.name
    fixture = task_dir / "fixture"
    hidden = task_dir / "hidden"
    reference = task_dir / "reference"
    visible = sorted(p.name for p in (fixture / "tests").glob("test_*.py")) if (fixture / "tests").exists() else []
    hidden_tests = sorted(p.name for p in hidden.glob("test_*.py")) if hidden.exists() else []

    if not hidden_tests:
        print(f"  [{task_id}] no hidden tests -- skipping")
        return True
    if not reference.is_dir():
        print(f"  [{task_id}] no reference/ -- skipping")
        return True

    ok = True
    with tempfile.TemporaryDirectory(prefix=f"calib-{task_id}-") as tmp:
        tmp_path = Path(tmp)

        # ---- reference: everything must pass -----------------------------
        ref = tmp_path / "reference"
        copy(reference, ref)
        shutil.rmtree(ref / "hidden", ignore_errors=True)
        copy(hidden, ref / "hidden")
        rc, out = run_pytest(ref, ["tests", "hidden"])
        n_hidden = len(hidden_tests)
        s = _count(out)
        print(f"  [{task_id}] reference : {s['summary'] or '(no summary)'}  (rc={rc})")
        if rc != 0:
            ok = False
            print("    !! reference does NOT pass the full suite:")
            print(_failures(out))

        # ---- pristine fixture: hidden tests must fail --------------------
        clean = tmp_path / "fixture"
        copy(fixture, clean)
        shutil.rmtree(clean / "tests", ignore_errors=True)
        copy(fixture / "tests", clean / "tests")
        shutil.rmtree(clean / "hidden", ignore_errors=True)
        copy(hidden, clean / "hidden")
        rc2, out2 = run_pytest(clean, ["hidden"])
        s2 = _count(out2)
        discriminating = s2["failed"] + s2["errors"] > 0
        print(f"  [{task_id}] pristine  : {s2['summary'] or '(no summary)'} "
              f"out of {n_hidden} hidden test files")
        if not discriminating:
            ok = False
            print("    !! pristine fixture passes EVERY hidden test -- "
                  "the task is trivial, strengthen the tests")

        if ok:
            # visible suite must still be green in the pristine fixture
            rc3, out3 = run_pytest(clean, ["tests"])
            s3 = _count(out3)
            print(f"  [{task_id}] visible   : {s3['summary'] or '(no summary)'}")
            if rc3 != 0:
                ok = False
                print("    !! visible suite is red on the pristine fixture:")
                print(_failures(out3))
    return ok


def _count(out: str) -> dict[str, int]:
    import re

    res = {"passed": 0, "failed": 0, "errors": 0, "collected": 0}
    # pytest's last line carries the totals; parse that, not the first match.
    summary = ""
    for line in reversed(out.strip().splitlines()):
        if re.search(r"\b(passed|failed|errors?|collected|no tests ran)\b", line):
            summary = line
            break
    m = re.search(r"(\d+) passed", summary)
    if m:
        res["passed"] = int(m.group(1))
    m = re.search(r"(\d+) failed", summary)
    if m:
        res["failed"] = int(m.group(1))
    m = re.search(r"(\d+) error", summary)
    if m:
        res["errors"] = int(m.group(1))
    m = re.search(r"(\d+) collected", out)
    if m:
        res["collected"] = int(m.group(1))
    res["summary"] = summary  # type: ignore[assignment]
    return res


def _failures(out: str) -> str:
    lines = [l for l in out.splitlines() if l.startswith(("FAILED", "E  ", "ERROR"))]
    return "\n".join("      " + l for l in lines[:40])


def main() -> int:
    ids = sys.argv[1:] or None
    if not TASKS.exists():
        print("no config/tasks directory")
        return 2
    all_ok = True
    for task_dir in sorted(p for p in TASKS.iterdir() if p.is_dir()):
        if ids and task_dir.name not in ids:
            continue
        print(f"== {task_dir.name}")
        all_ok = calibrate(task_dir) and all_ok
    print("\nCALIBRATION " + ("OK" if all_ok else "FAILED"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
