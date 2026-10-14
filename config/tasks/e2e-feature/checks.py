"""Deterministic post-run checks for the e2e-feature task.

Each check is a module-level function named ``check_<name>`` taking a single
``ctx`` dict and returning::

    {"ok": bool, "score": float, "detail": str}

``score`` is 1.0 (fully compliant) .. 0.0 (fully violating).

ctx keys
--------
sandbox      Path -- the model's final working tree
fixture      Path -- the pristine fixture the sandbox was copied from
spec         str  -- contents of task.md
plan         str  -- the plan this run was based on ('' for plan-only runs)
diff         str  -- unified diff fixture -> sandbox
changed      [str] -- paths (relative to the project root) the run altered
created      [str] -- paths present in the sandbox but not the fixture

Disable a check by adding its name to ``exclude_checks`` in manifest.yaml.
"""

from __future__ import annotations

import re
import sys

_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))",
    re.M,
)


def _py_files(paths, sandbox):
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        p = sandbox / rel
        if p.is_file():
            yield rel, p.read_text(errors="replace")


def check_no_test_edits(ctx) -> dict:
    """The brief forbids touching anything under tests/."""
    bad = sorted(p for p in ctx["changed"] if p == "tests" or p.startswith("tests/"))
    if not bad:
        return {"ok": True, "score": 1.0, "detail": "tests/ untouched"}
    return {
        "ok": False,
        "score": 0.0,
        "detail": "modified files under tests/: " + ", ".join(bad),
    }


def check_no_new_dependencies(ctx) -> dict:
    """Production code must stay stdlib-only (plus the ledger package itself)."""
    allowed = set(sys.stdlib_module_names) | {"ledger", "tests", "__future__"}
    bad: set[str] = set()
    for rel, src in _py_files(ctx["changed"] + ctx["created"], ctx["sandbox"]):
        for m in _IMPORT_RE.finditer(src):
            mod = (m.group(1) or m.group(2) or "").split(".")[0]
            if mod and mod not in allowed:
                bad.add(f"{mod} (in {rel})")
    if not bad:
        return {"ok": True, "score": 1.0, "detail": "no third-party imports added"}
    return {
        "ok": False,
        "score": 0.0,
        "detail": "forbidden imports: " + ", ".join(sorted(bad)),
    }


def check_no_out_of_scope_files(ctx) -> dict:
    """New files must live inside the project tree, not beside/above it."""
    roots = {"ledger", "tests", "docs", "examples", "README.md"}
    stray = sorted(
        p
        for p in ctx["created"]
        if p.split("/")[0] not in roots and not p.startswith("opencode.")
    )
    if not stray:
        return {"ok": True, "score": 1.0, "detail": "no files created outside the project"}
    return {
        "ok": False,
        "score": 0.5,
        "detail": "unexpected new paths: " + ", ".join(stray),
    }
