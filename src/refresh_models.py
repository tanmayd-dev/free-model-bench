"""Reconcile the on-disk `opencode models` list with the local registry.

Called at the start of every run. Cheap (one CLI call), and the only thing
that discovers new free models.
"""

from __future__ import annotations

import subprocess
from typing import Any

from evallib import execute, log_live, now_iso, q


def list_opencode_models(timeout: float = 60.0) -> list[str]:
    try:
        proc = subprocess.run(
            ["opencode", "models"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_live(f"model refresh failed: {exc}", kind="error")
        return []
    ids = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        # `opencode models` prints bare ids; tolerate a "provider/name" column.
        ids.append(line.split()[0])
    return sorted(dict.fromkeys(ids))


def refresh() -> dict[str, Any]:
    available = list_opencode_models()
    if not available:
        log_live("model refresh returned nothing; registry left unchanged",
                 kind="warn")
        return {"new": [], "available": [], "delisted": [], "ok": False}

    now = now_iso()
    new, delisted = [], []
    known = {r["id"]: r for r in q("SELECT * FROM models")}

    for mid in available:
        row = known.get(mid)
        if row is None:
            execute(
                "INSERT INTO models(id, status, enabled, first_seen, last_seen) "
                "VALUES(?, 'new', 1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "last_seen=excluded.last_seen",
                (mid, now, now),
            )
            new.append(mid)
        else:
            status = "known" if row["status"] in ("new", "delisted") else row["status"]
            execute("UPDATE models SET last_seen=?, status=? WHERE id=?", (now, status, mid))

    for mid, row in known.items():
        if mid not in available and row["status"] != "delisted":
            execute("UPDATE models SET status='delisted' WHERE id=?", (mid,))
            delisted.append(mid)

    log_live(f"models: {len(available)} available, "
             f"{len(new)} new, {len(delisted)} delisted")
    return {"new": new, "available": available, "delisted": delisted, "ok": True}


def enable(mid: str, enabled: bool) -> None:
    execute("UPDATE models SET enabled=? WHERE id=?", (1 if enabled else 0, mid))


def set_status(mid: str, status: str, notes: str | None = None) -> None:
    if notes is None:
        execute("UPDATE models SET status=? WHERE id=?", (status, mid))
    else:
        execute("UPDATE models SET status=?, notes=? WHERE id=?", (status, notes, mid))


def runnable_models(only: list[str] | None = None) -> list[str]:
    """Enabled, currently-available models, in a stable order."""
    rows = q("SELECT id FROM models WHERE enabled=1 AND status != 'delisted' ORDER BY id")
    ids = [r["id"] for r in rows]
    if only:
        wanted = set(only)
        ids = [m for m in ids if m in wanted]
    return ids
