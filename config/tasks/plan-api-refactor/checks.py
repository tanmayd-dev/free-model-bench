"""Deterministic checks on the plan artifact for plan-api-refactor.

Same contract as every other task's checks.py -- each ``check_<name>`` takes
a ``ctx`` dict and returns ``{"ok": bool, "score": float, "detail": str}``
with ``score`` in 1.0 (compliant) .. 0.0 (violating).

The plan stage is READ-ONLY, so these checks are about whether the plan is
grounded, ordered and actually written -- things a judge should not have to
spend a call on.
"""

from __future__ import annotations

import re

#: A plan shorter than this has not engaged with a 5-module codebase.
MIN_CHARS = 1200

#: The package modules the plan is expected to account for.
PACKAGE = "shopfront"

_ORDERED_RE = re.compile(r"(?m)^\s{0,3}\d{1,2}[.)]\s+\S")
_PHASE_RE = re.compile(r"(?i)\b(?:phase|step|stage|milestone)\s+\d+\b")
_SECTION_RE = re.compile(r"(?m)^#{1,6}\s+\S")


def check_plan_read_only(ctx) -> dict:
    """The plan stage must not have touched a single file."""
    bad = sorted(ctx["changed"]) + sorted(ctx["created"])
    if not bad:
        return {"ok": True, "score": 1.0, "detail": "working tree untouched"}
    return {
        "ok": False,
        "score": 0.0,
        "detail": "read-only plan stage modified: " + ", ".join(bad[:12]),
    }


def check_plan_substantial(ctx) -> dict:
    """Long enough to actually specify a migration, not a paragraph."""
    n = len((ctx["plan"] or "").strip())
    if n >= MIN_CHARS:
        return {"ok": True, "score": 1.0, "detail": f"{n} characters"}
    if n == 0:
        return {"ok": False, "score": 0.0, "detail": "no plan produced"}
    return {
        "ok": False,
        "score": round(n / MIN_CHARS, 2),
        "detail": f"{n} characters, expected at least {MIN_CHARS}",
    }


def check_plan_names_real_files(ctx) -> dict:
    """Grounding: does it account for the modules that actually exist?

    A plan that proposes a target architecture without naming the source
    modules cannot be executed, and a judge should not have to re-read the
    repository to notice.
    """
    plan = (ctx["plan"] or "").lower()
    pkg = ctx["fixture"] / PACKAGE
    if not pkg.is_dir():
        return {"ok": True, "score": 1.0, "detail": "package not found; skipped"}

    names = sorted(p.name for p in pkg.glob("*.py"))
    if not names:
        return {"ok": True, "score": 1.0, "detail": "no modules; skipped"}

    named = [n for n in names if n.lower() in plan]
    need = max(2, (len(names) + 1) // 2)
    ok = len(named) >= need
    return {
        "ok": ok,
        "score": round(min(1.0, len(named) / need), 2),
        "detail": f"named {len(named)}/{need} required modules: "
                  + (", ".join(named) or "none"),
    }


def check_plan_is_ordered(ctx) -> dict:
    """The brief demands numbered migration steps; prose does not qualify."""
    plan = ctx["plan"] or ""
    numbered = len(_ORDERED_RE.findall(plan))
    phased = len(_PHASE_RE.findall(plan))
    headings = len(_SECTION_RE.findall(plan))
    ordered = max(numbered, phased)

    if ordered >= 3 and headings >= 5:
        return {"ok": True, "score": 1.0,
                "detail": f"{numbered} numbered steps, {headings} sections"}
    score = min(1.0, (ordered / 3) * 0.6 + (headings / 5) * 0.4)
    missing = []
    if ordered < 3:
        missing.append(f"only {ordered} ordered steps")
    if headings < 5:
        missing.append(f"only {headings} sections")
    return {
        "ok": False,
        "score": round(score, 2),
        "detail": "not broken into an ordered, sectioned migration: "
                  + "; ".join(missing),
    }
