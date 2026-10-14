"""Task manifests: the only contract for adding/disabling evaluation tests.

A task is a directory under config/tasks/<id>/ containing manifest.yaml.
Dropping in a new directory adds a test; flipping `enabled` off removes one.
No code changes anywhere else.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evallib import (
    TASKS_DIR,
    clear_cfg,
    execute,
    get_cfg,
    now_iso,
    q,
    set_cfg,
    sha_file,
    sha_text,
    sha_tree,
)

DEFAULT_TIMEOUTS = {"plan": 900, "implement": 1800, "verify": 300, "judge": 300}
DEFAULT_SCORING = {"judge_plan": 1.0}
DEFAULT_JUDGES = ["claude-opus-4-6-thinking", "claude-sonnet-4-6",
                  "gemini-3.8-flash-high"]

# Fields the website is allowed to patch, with the value the manifest
# (falling back to the code default) already provides for each. An override
# equal to one of these is not an override at all and must not be stored.
MANIFEST_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "exclude_tests": [],
    "exclude_checks": [],
    "weight": 1.0,
    "profiles": ["full"],
    "judge_models": DEFAULT_JUDGES,
    "scoring": DEFAULT_SCORING,
}

# The components task_score() knows how to weight. A weight for anything else
# is dropped silently when the weights are applied, which quietly renormalises
# the rest over a different set -- so both the manifest and the UI reject it.
SCORE_COMPONENTS = frozenset({"judge_plan", "judge_code", "tests"})


# --------------------------------------------------------------------------
# Overrides stored in the DB (what the website writes)
# --------------------------------------------------------------------------


def _override(task_id: str, key: str, default=None):
    return get_cfg(f"task:{task_id}:{key}", default)


def save_override(task_id: str, key: str, value: Any) -> None:
    set_cfg(f"task:{task_id}:{key}", value)


def clear_override(task_id: str, key: str) -> None:
    """Drop a DB override so the manifest speaks again.

    Overrides only ever store a *delta*. A stored value equal to the manifest
    is worse than no value at all: it is invisible in the file, and it shadows
    every later edit to that file.
    """
    clear_cfg(f"task:{task_id}:{key}")


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    path: Path
    manifest: dict[str, Any]
    fixture_sha: str = ""
    verify_sha: str = ""
    manifest_sha: str = ""

    # ---- identity / lifecycle -------------------------------------------
    @property
    def name(self) -> str:
        return str(self.manifest.get("name") or self.id)

    @property
    def description(self) -> str:
        return str(self.manifest.get("description") or "")

    @property
    def profiles(self) -> list[str]:
        ov = _override(self.id, "profiles")
        if isinstance(ov, (list, tuple)) and ov:
            return [str(p) for p in ov]
        if isinstance(ov, str) and ov.strip():
            return [p.strip() for p in ov.split(",") if p.strip()]
        return [str(p) for p in self.manifest.get("profiles", ["full"])]

    @property
    def weight(self) -> float:
        ov = _override(self.id, "weight")
        if ov is not None:
            try:
                return float(ov)
            except (TypeError, ValueError):
                pass
        return float(self.manifest.get("weight", 1.0))

    @property
    def enabled(self) -> bool:
        """DB override wins over manifest, so the website is instant."""
        ov = _override(self.id, "enabled")
        if ov is None:
            return bool(self.manifest.get("enabled", True))
        return bool(ov)

    @property
    def stages(self) -> list[str]:
        return [str(s) for s in self.manifest.get("stages", ["plan", "implement"])]

    @property
    def implement_from(self) -> list[str]:
        return [str(s) for s in self.manifest.get("implement_from", ["own"])]

    @property
    def timeouts(self) -> dict[str, int]:
        out = dict(DEFAULT_TIMEOUTS)
        out.update({k: int(v) for k, v in (self.manifest.get("timeouts") or {}).items()})
        return out

    @property
    def scoring(self) -> dict[str, float]:
        ov = _override(self.id, "scoring")
        raw = ov if isinstance(ov, dict) else (self.manifest.get("scoring") or DEFAULT_SCORING)
        total = sum(float(v) for v in raw.values()) or 1.0
        return {k: float(v) / total for k, v in raw.items()}

    @property
    def judge_models(self) -> list[str]:
        """Effective panel, most specific wins.

        per-task DB override > task manifest > global Settings > default.
        The global setting used to be written by the Settings tab and read by
        nothing at all, so changing the panel there did nothing.
        """
        ov = _override(self.id, "judge_models")
        if ov:
            return [str(m) for m in ov]
        # The Settings tab is a global override and its label says "blank =
        # per-task manifest", so it has to outrank the manifest -- otherwise
        # changing the panel there does nothing at all for any task that
        # declares one, which is most of them.
        global_panel = get_cfg("global.judge_models", None)
        if isinstance(global_panel, (list, tuple)) and global_panel:
            return [str(m) for m in global_panel]
        if isinstance(global_panel, str) and global_panel.strip():
            return [m.strip() for m in global_panel.split(",") if m.strip()]
        manifest = self.manifest.get("judge_models")
        if manifest:
            return [str(m) for m in manifest]
        return DEFAULT_JUDGES

    # ---- overrides --------------------------------------------------------
    def manifest_default(self, key: str) -> Any:
        """What the manifest says for a patchable field, ignoring the override.

        The website needs this to tell a genuine override from one that merely
        repeats the manifest. Storing the latter looks like a no-op but is not:
        it sits in the config table where no file shows it and silently masks
        every later edit to the manifest.
        """
        if key not in MANIFEST_DEFAULTS:
            return None
        raw = self.manifest.get(key)
        return MANIFEST_DEFAULTS[key] if raw is None else raw

    @property
    def rubric_plan(self) -> str:
        return self._rubric("plan", "rubric-plan.txt")

    @property
    def rubric_code(self) -> str:
        return self._rubric("code", "rubric-implement.txt")

    def _rubric(self, kind: str, default_name: str) -> str:
        rubrics = self.manifest.get("rubrics") or {}
        path = self.path / str(rubrics.get(kind, default_name))
        return path.read_text() if path.exists() else ""

    # ---- content ----------------------------------------------------------
    @property
    def spec(self) -> str:
        return (self.path / "task.md").read_text()

    @property
    def prompt_plan(self) -> str:
        # Optional: a task with no plan stage has no plan prompt.
        p = self.path / "prompt-plan.txt"
        return p.read_text() if p.exists() else ""

    @property
    def prompt_implement(self) -> str:
        # Optional: a plan-only task has no implement prompt. run_unit reads
        # this unconditionally to decide whether {{PLAN}} is needed, so it
        # must not raise.
        p = self.path / "prompt-implement.txt"
        return p.read_text() if p.exists() else ""

    @property
    def gold_plan(self) -> str | None:
        p = self.path / "gold-plan.md"
        return p.read_text() if p.exists() else None

    # ---- tests ------------------------------------------------------------
    @property
    def hidden_dir(self) -> Path:
        return self.path / "hidden"

    @property
    def visible_dir(self) -> Path:
        return self.path / "fixture" / "tests"

    @property
    def hidden_tests_all(self) -> list[str]:
        if not self.hidden_dir.exists():
            return []
        return sorted(p.name for p in self.hidden_dir.glob("test_*.py"))

    @property
    def excluded_tests(self) -> list[str]:
        ov = _override(self.id, "exclude_tests")
        if ov is not None:
            return [str(t) for t in ov]
        return [str(t) for t in self.manifest.get("exclude_tests", [])]

    @property
    def hidden_tests(self) -> list[str]:
        """Tests that will actually run, after manifest + DB exclusions."""
        spec = self.manifest.get("hidden_tests", "auto")
        if isinstance(spec, list):
            base = [str(t) for t in spec]
        else:
            base = self.hidden_tests_all
        excluded = set(self.excluded_tests)
        return [t for t in base if t not in excluded]

    @property
    def active_tests_sha(self) -> str:
        return sha_text("\n".join(sorted(self.hidden_tests)))

    # ---- checks -----------------------------------------------------------
    @property
    def check_names(self) -> list[str]:
        f = self.path / "checks.py"
        if not f.exists():
            return []
        return sorted(
            m.group(1)
            for m in re.finditer(r"^def\s+(check_[A-Za-z0-9_]+)\s*\(", f.read_text(), re.M)
        )

    @property
    def excluded_checks(self) -> list[str]:
        ov = _override(self.id, "exclude_checks")
        if ov is not None:
            return [str(c) for c in ov]
        return [str(c) for c in self.manifest.get("exclude_checks", [])]

    @property
    def active_checks(self) -> list[str]:
        return [c for c in self.check_names if c not in self.excluded_checks]

    # ---- cache keys --------------------------------------------------------
    def cache_key(self, stage: str, **parts: str) -> str:
        return sha_text(stage + "\n" + "\n".join(f"{k}={v}" for k, v in sorted(parts.items())))


def _load_manifest(task_dir: Path) -> dict[str, Any] | None:
    mf = task_dir / "manifest.yaml"
    if not mf.exists():
        return None
    data = yaml.safe_load(mf.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{mf}: manifest must be a YAML mapping")
    data.setdefault("id", task_dir.name)
    if data["id"] != task_dir.name:
        raise ValueError(f"{mf}: id {data['id']!r} must match directory name {task_dir.name!r}")
    return data


def load_task(task_id: str) -> Task | None:
    task_dir = TASKS_DIR / task_id
    if not task_dir.is_dir():
        return None
    manifest = _load_manifest(task_dir)
    if manifest is None:
        return None
    t = Task(id=task_id, path=task_dir, manifest=manifest)
    # Everything that can change the MODEL's input or behaviour. Deliberately
    # excludes anything that only affects grading -- hidden tests, checks,
    # rubrics and judge schemas -- so editing a grader does not force an
    # expensive re-run of the model. Rubrics and schemas are still keyed
    # separately for judging (rubric_sha covers both), so re-judging on a
    # rubric tweak costs nothing for an unchanged artifact.
    #
    # gold-plan.md is NOT ignored: when implement_from includes `gold` it is
    # handed straight to the model, so editing it has to invalidate the run.
    t.fixture_sha = sha_tree(
        task_dir,
        ignore={
            "reference", "hidden", "checks.py", "manifest.yaml",
            "rubric-*.txt", "*.schema.json",
            "__pycache__", ".pytest_cache", "*.pyc",
        },
    )
    # Everything that can change how an existing run is GRADED.
    rubrics = manifest.get("rubrics") or {}
    parts = [
        sha_file(task_dir / "manifest.yaml"),
        sha_tree(task_dir / "hidden") if (task_dir / "hidden").exists() else "",
        sha_file(task_dir / "checks.py") if (task_dir / "checks.py").exists() else "",
    ]
    for kind, default in (("plan", "rubric-plan.txt"),
                          ("code", "rubric-implement.txt")):
        p = task_dir / str(rubrics.get(kind, default))
        parts.append(sha_file(p) if p.exists() else "")
    # A task may swap in its own judge schema; that is a grading input too.
    for override in (manifest.get("judge_schema") or {}).values():
        p = task_dir / str(override)
        parts.append(sha_file(p) if p.exists() else "")
    t.verify_sha = sha_text("\n".join(parts))
    t.manifest_sha = sha_file(task_dir / "manifest.yaml")
    return t


def load_tasks(
    *,
    only: list[str] | None = None,
    profile: str | None = None,
    include_disabled: bool = False,
) -> list[Task]:
    if not TASKS_DIR.exists():
        return []
    out: list[Task] = []
    for d in sorted(p for p in TASKS_DIR.iterdir() if p.is_dir()):
        if d.name.startswith(("_", ".")):
            continue
        t = load_task(d.name)
        if t is None:
            continue
        if only and t.id not in only:
            continue
        if profile and profile not in t.profiles:
            continue
        if not include_disabled and not t.enabled:
            continue
        out.append(t)
    return out


# --------------------------------------------------------------------------
# Registry sync (what the website reads/writes)
# --------------------------------------------------------------------------


def sync_registry() -> dict[str, Any]:
    """Mirror on-disk tasks into the `tasks` table so the UI has one place to look."""
    seen = set()
    for t in load_tasks(include_disabled=True):
        seen.add(t.id)
        _upsert_task(t)
    # Hide tasks removed from disk.
    for row in _all_task_ids():
        if row not in seen:
            _delete_task(row)
    return {"tasks": sorted(seen)}


def _upsert_task(t: Task) -> None:
    execute(
        "INSERT INTO tasks(id,name,enabled,profiles,weight,manifest_sha,updated) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "name=excluded.name, profiles=excluded.profiles, weight=excluded.weight, "
        "manifest_sha=excluded.manifest_sha, updated=excluded.updated",
        (
            t.id,
            t.name,
            1 if t.enabled else 0,
            json.dumps(t.profiles),
            t.weight,
            t.manifest_sha,
            now_iso(),
        ),
    )


def _all_task_ids() -> list[str]:
    return [r["id"] for r in q("SELECT id FROM tasks")]


def _delete_task(task_id: str) -> None:
    execute("DELETE FROM tasks WHERE id=?", (task_id,))


# --------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def placeholders(template: str) -> set[str]:
    """Every ``{{NAME}}`` this template will substitute.

    ``render`` raises KeyError on anything not in its kwargs, so a typo'd
    placeholder only surfaces when the stage actually runs -- potentially
    hours into a job. Checking statically catches it first.
    """
    return set(_PLACEHOLDER.findall(template))


def render(template: str, **vars: str) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in vars:
            raise KeyError(f"unknown prompt placeholder {{{{{key}}}}} "
                           f"(have: {', '.join(sorted(vars))})")
        return str(vars[key])

    return _PLACEHOLDER.sub(sub, template)


def task_summary() -> list[dict[str, Any]]:
    """Everything the UI needs about tasks in one pass."""
    rows = []
    for t in load_tasks(include_disabled=True):
        rows.append(
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "enabled": t.enabled,
                "profiles": t.profiles,
                "weight": t.weight,
                "stages": t.stages,
                "implement_from": t.implement_from,
                "scoring": t.scoring,
                "judge_models": t.judge_models,
                "timeouts": t.timeouts,
                "hidden_tests_total": len(t.hidden_tests_all),
                "hidden_tests_active": len(t.hidden_tests),
                "hidden_tests_all": t.hidden_tests_all,
                "excluded_tests": t.excluded_tests,
                "checks": t.check_names,
                "active_checks": t.active_checks,
                "excluded_checks": t.excluded_checks,
                "fixture_sha": t.fixture_sha,
                "manifest_sha": t.manifest_sha,
            }
        )
    return rows
