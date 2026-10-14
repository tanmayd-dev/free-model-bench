"""Cheap health check for every model in the registry.

One trivial prompt per model (a few seconds each). This is what stops an
overnight run burning a 15-minute timeout on a model that was deprecated
this afternoon. Called at the start of a run unless ``--no-probe``.

Classification
    ok           -> status 'known'
    permanent    -> status 'delisted'  (deprecated / unavailable / not found)
    transient    -> status 'degraded'  (left enabled; the daily retry may heal it)
"""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from evallib import execute, is_permanent_error, log_live, now_iso, q1

PROBE_PROMPT = "Reply with exactly: OK"
PROBE_TIMEOUT = 90.0


def probe_model(model: str) -> dict[str, Any]:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["opencode", "run", "--model", model, "--agent", "plan",
             "--format", "json", PROBE_PROMPT],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"model": model, "ok": False, "permanent": False,
                "error": f"probe timed out after {PROBE_TIMEOUT:.0f}s"}
    except OSError as exc:
        return {"model": model, "ok": False, "permanent": False, "error": str(exc)}

    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    duration = round(time.monotonic() - start, 1)

    message = ""
    has_error = False
    has_text = False
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = ev.get("type")
        if typ == "error":
            has_error = True
            err = ev.get("error") or {}
            message = err.get("message") or str(err)
        elif typ == "text" and (ev.get("part") or {}).get("text"):
            has_text = True

    if has_error:
        return {"model": model, "ok": False, "permanent": is_permanent_error(message),
                "error": message[:400], "duration_s": duration}
    if not has_text:
        return {"model": model, "ok": False, "permanent": False,
                "error": "no assistant output", "duration_s": duration}
    return {"model": model, "ok": True, "duration_s": duration}


def probe_all(models: list[str] | None = None, workers: int = 6) -> dict[str, Any]:
    ids = models or [r["id"] for r in q_all()]
    # One probe per *distinct* model, so they never contend with the
    # per-model serialization rule -- running them together just saves wall
    # clock (a probe can be slow: models think for 10s before saying "OK").
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(ids) or 1))) as pool:
        results = list(pool.map(probe_model, ids))

    for res in results:
        mid = res["model"]
        row = q1("SELECT * FROM models WHERE id=?", (mid,))
        if row is None:
            continue
        note = (res.get("error") or "")[:500] or None
        if res["ok"]:
            # A model that just answered is not delisted or deprecated, even
            # if a previous probe or refresh thought otherwise.
            status = row["status"] if row["status"] in ("known", "new") else "known"
            execute("UPDATE models SET status=?, notes=NULL, last_seen=? WHERE id=?",
                    (status, now_iso(), mid))
            log_live(f"probe OK       {mid} ({res.get('duration_s', 0)}s)")
        elif res.get("permanent"):
            execute("UPDATE models SET status='delisted', notes=?, last_seen=? WHERE id=?",
                    (note, now_iso(), mid))
            log_live(f"probe DEAD     {mid}: {note}", kind="warn")
        else:
            # Transient: leave enabled so the daily job can retry it.
            execute("UPDATE models SET status='degraded', notes=?, last_seen=? WHERE id=?",
                    (note, now_iso(), mid))
            log_live(f"probe DEGRADED {mid}: {note}", kind="warn")

    dead = [r["model"] for r in results if r.get("permanent")]
    degraded = [r["model"] for r in results if not r["ok"] and not r.get("permanent")]
    log_live(f"probe: {len(results) - len(dead) - len(degraded)} healthy, "
             f"{len(degraded)} degraded, {len(dead)} dead")
    return {"healthy": [r["model"] for r in results if r["ok"]],
            "degraded": degraded, "dead": dead, "results": results}


def q_all():
    from evallib import q

    return q("SELECT id FROM models ORDER BY id")
