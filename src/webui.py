#!/usr/bin/env python3
"""Local control panel for the model evaluation harness.

    python3 src/webui.py --port 8765     ->  http://127.0.0.1:8765

No auth, no build step, no dependencies: a stdlib HTTP server serving a
handful of JSON endpoints and three static files. It binds to loopback only.

The website and `scripts/run-eval.sh` are deliberately the same code path:
pressing "start" spawns the shell script, and progress is read back out of
the shared SQLite database, so the two can never disagree.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evallib import (  # noqa: E402
    LIVE_LOG,
    clear_cfg,
    ensure_layout,
    execute,
    get_cfg,
    now_iso,
    q,
    q1,
    set_cfg,
)
from rank import latest_runs, leaderboard, recompute_scores  # noqa: E402
from refresh_models import refresh  # noqa: E402
from tasks import (  # noqa: E402
    SCORE_COMPONENTS,
    Task,
    clear_override,
    load_tasks,
    save_override,
    sync_registry,
    task_summary,
)

WEB_DIR = ROOT / "web"
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-/]*$")

_cache: dict[str, Any] = {"t": 0.0, "value": None}
CACHE_TTL = 2.0


# --------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------


def _ok(payload: Any, status: int = 200) -> tuple[int, dict[str, Any]]:
    return status, payload


def _err(msg: str, status: int = 400) -> tuple[int, dict[str, Any]]:
    return status, {"error": msg}


def _status(result: Any) -> tuple[int, dict[str, Any]]:
    """Normalise a mutation helper's return value to ``(status, payload)``.

    The helpers are inconsistent by nature: the happy path returns a bare
    ``dict`` while the failure paths go through ``_err``/``_ok`` and return a
    tuple. Unwrapping once here means ``do_POST`` never has to guess -- and
    specifically never has to do ``out.pop("error", 400)``, which returns the
    error *message* and would be sent as the HTTP status code.
    """
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
        return int(result[0]), result[1]
    if isinstance(result, dict):
        return (400 if "error" in result else 200), result
    return 500, {"error": f"handler returned {type(result).__name__}: {result!r}"}


def current_job() -> dict[str, Any] | None:
    rows = q("SELECT * FROM jobs ORDER BY started DESC LIMIT 1")
    return dict(rows[0]) if rows else None


def job_running() -> bool:
    job = current_job()
    return bool(job and job["status"] in ("running", "queued"))


def counts() -> dict[str, Any]:
    runs = q("SELECT stage, status, COUNT(*) AS n FROM runs GROUP BY stage, status")
    by_stage: dict[str, dict[str, int]] = {}
    total = 0
    for r in runs:
        by_stage.setdefault(r["stage"], {})[r["status"]] = r["n"]
        total += r["n"]
    judges = q1("SELECT COUNT(*) AS n FROM judges")["n"]
    return {"runs": total, "by_stage": by_stage, "judges": judges}


def pending_count() -> int:
    from runner import Options, pending_units, resolve_units

    opts = Options(mode="probe", refresh_models=False)
    try:
        return len(pending_units(resolve_units(opts), opts))
    except Exception:  # noqa: BLE001
        return -1


def state_payload() -> dict[str, Any]:
    now = time.monotonic()
    if _cache["value"] is not None and now - _cache["t"] < CACHE_TTL:
        return _cache["value"]

    sync_registry()
    models = [dict(r) for r in q("SELECT * FROM models ORDER BY id")]
    job = current_job()
    if job and job["status"] == "running" and job["progress_total"] == 0:
        # a job that has not started scheduling yet still counts as running
        pass

    payload = {
        "generated": now_iso(),
        "models": models,
        "tasks": task_summary(),
        "job": job,
        "running": job_running(),
        "counts": counts(),
        "pending": pending_count(),
        "settings": settings_payload(),
        "leaderboard": _read_leaderboard(),
    }
    _cache["t"] = now
    _cache["value"] = payload
    return payload


def _read_leaderboard() -> dict[str, Any]:
    p = ROOT / "results" / "leaderboard.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"generated": None, "profile": None, "rows": []}


def settings_payload() -> dict[str, Any]:
    return {
        "concurrency": get_cfg("ui.concurrency", 3),
        "budget_minutes": get_cfg("ui.budget_minutes", None),
        "judge_models": get_cfg("global.judge_models", None),
        "mode": get_cfg("ui.mode", "auto"),
        "state_dir": os.environ.get("MODEL_EVAL_STATE", str(ROOT / "state")),
        "live_log": str(LIVE_LOG),
    }


def _log_tail(lines: int = 200) -> list[str]:
    try:
        with LIVE_LOG.open(errors="replace") as fh:
            return fh.readlines()[-lines:]
    except FileNotFoundError:
        return []


def run_detail(run_id: str) -> dict[str, Any]:
    row = q1("SELECT * FROM runs WHERE id=?", (run_id,))
    if row is None:
        return {"error": "not found"}
    out = dict(row)
    art = Path(row["artifacts_dir"] or "")
    for field, filename, limit in (
        ("plan", "plan.md", 200_000),
        ("prompt", "prompt.md", 40_000),
        ("diff", "patch.diff", 300_000),
        ("verify", "verify.json", 200_000),
        ("checks", "checks.json", 40_000),
        ("tree_index", "tree_index.json", 40_000),
    ):
        p = art / filename
        if p.exists() and p.is_file():
            out[field] = p.read_text(errors="replace")[:limit]
    events = art / "events.ndjson"
    if events.exists():
        out["events_tail"] = _tail_file(events, 400)[:80_000]
    judges = []
    for r in q("SELECT * FROM judges WHERE run_id=?", (run_id,)):
        j = dict(r)
        try:
            j["payload_parsed"] = json.loads(j["payload"])
        except json.JSONDecodeError:
            j["payload_parsed"] = None
        judges.append(j)
    out["judges"] = judges
    return out


def _tail_file(path: Path, n: int) -> str:
    try:
        with path.open(errors="replace") as fh:
            return "".join(fh.readlines()[-n:])
    except (OSError, ValueError):
        return ""


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------


def start_job(body: dict[str, Any]) -> tuple[int, dict[str, Any]] | dict[str, Any]:
    if job_running():
        return _err("a job is already running", 409)

    mode = str(body.get("mode") or "auto")
    if mode not in ("smoke", "auto", "daily", "monthly", "full", "all"):
        return _err(f"bad mode {mode!r}")

    args = [str(ROOT / "scripts" / "run-eval.sh"), "--mode", mode]
    conc = body.get("concurrency")
    if conc is not None:
        set_cfg("ui.concurrency", int(conc))
        args += ["--concurrency", str(int(conc))]
    else:
        args += ["--concurrency", str(int(get_cfg("ui.concurrency", 3)))]
    budget = body.get("budget_minutes")
    if budget:
        set_cfg("ui.budget_minutes", float(budget))
        args += ["--budget-minutes", str(float(budget))]
    if body.get("force"):
        args.append("--force")
    if body.get("no_judge"):
        args.append("--no-judge")
    if body.get("models"):
        args += ["--models", ",".join(str(m) for m in body["models"])]
    if body.get("tasks"):
        args += ["--tasks", ",".join(str(t) for t in body["tasks"])]
    if body.get("profile"):
        args += ["--profile", str(body["profile"])]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = ROOT / "state" / "jobs" / f"ui-{stamp}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n=== spawn {now_iso()} :: {' '.join(args)}\n")
        log.flush()
        subprocess.Popen(
            args, cwd=str(ROOT), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    _cache["t"] = 0.0
    time.sleep(0.4)
    return {"spawned": True, "log": str(log_path), "args": args}


def control_job(action: str) -> dict[str, Any]:
    if action not in ("pause", "stop", "running"):
        return _err(f"bad action {action!r}")
    job = current_job()
    if not job:
        return _err("no job", 404)
    execute("UPDATE jobs SET control=? WHERE id=?", (action, job["id"]))
    _cache["t"] = 0.0
    return {"job_id": job["id"], "control": action}


def _same_as_manifest(task: Task, key: str, value: Any) -> bool:
    """True when a patched value merely repeats what the manifest already says."""
    base = task.manifest_default(key)
    if isinstance(base, dict) and isinstance(value, dict):
        try:
            return ({str(k): float(v) for k, v in base.items()} ==
                    {str(k): float(v) for k, v in value.items()})
        except (TypeError, ValueError):
            return False
    if isinstance(base, list) and isinstance(value, list):
        return [str(v) for v in base] == [str(v) for v in value]
    if isinstance(base, bool) or isinstance(value, bool):
        return bool(base) == bool(value)
    if isinstance(base, (int, float)) and isinstance(value, (int, float)):
        return float(base) == float(value)
    return base == value


def patch_task(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    task = next((t for t in load_tasks(include_disabled=True) if t.id == task_id), None)
    if task is None:
        return _err(f"unknown task {task_id!r}", 404)
    allowed = {"enabled", "exclude_tests", "exclude_checks", "weight",
               "profiles", "judge_models", "scoring"}
    unknown = set(body) - allowed
    if unknown:
        return _err(f"unknown field(s): {', '.join(sorted(unknown))}")
    applied: dict[str, Any] = {}
    for key, value in body.items():
        if key in ("exclude_tests", "exclude_checks", "judge_models",
                   "profiles") and isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if key in ("exclude_tests", "exclude_checks"):
            valid = set(task.hidden_tests_all) if key == "exclude_tests" else set(task.check_names)
            bad = [v for v in value if v not in valid]
            if bad:
                return _err(f"not a valid {key}: {', '.join(bad)}")
            value = [str(v) for v in value]
        if key in ("profiles", "judge_models"):
            if not isinstance(value, list) or not value:
                return _err(f"{key} must name at least one entry"
                            + ("" if key != "profiles"
                               else ", or the task would never be scheduled"))
            value = [str(v) for v in value]
        if key == "scoring":
            if not isinstance(value, dict) or not value:
                return _err("scoring must be a non-empty object of weights")
            unknown = sorted(set(value) - set(SCORE_COMPONENTS))
            if unknown:
                return _err(f"unknown scoring component(s): {', '.join(unknown)} "
                            f"(known: {', '.join(sorted(SCORE_COMPONENTS))})")
            norm: dict[str, float] = {}
            for component, weight in value.items():
                try:
                    norm[str(component)] = float(weight)
                except (TypeError, ValueError):
                    return _err(f"scoring.{component} must be a number")
                if norm[str(component)] <= 0:
                    return _err(f"scoring.{component} must be > 0")
            value = norm
        if key == "enabled":
            value = bool(value)
        if key == "weight":
            try:
                value = float(value)
            except (TypeError, ValueError):
                return _err("weight must be a number")
            if value < 0:
                return _err("weight must not be negative")

        # Store only genuine deltas. A value identical to the manifest is not
        # an override, and persisting it would hide every later edit to the
        # file behind an invisible row in the config table.
        if _same_as_manifest(task, key, value):
            clear_override(task_id, key)
        else:
            save_override(task_id, key, value)
        applied[key] = value
    _cache["t"] = 0.0
    return {"task": task_id, "applied": applied}


def patch_model(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
    if not MODEL_ID_RE.match(model_id):
        return _err("bad model id")
    if not q1("SELECT id FROM models WHERE id=?", (model_id,)):
        return _err(f"unknown model {model_id!r}", 404)
    if "enabled" in body:
        execute("UPDATE models SET enabled=? WHERE id=?",
                (1 if body["enabled"] else 0, model_id))
    if "weight" in body:
        try:
            weight = float(body["weight"])
        except (TypeError, ValueError):
            return _err("weight must be a number")
        if weight < 0:
            return _err("weight must not be negative")
        execute("UPDATE models SET weight=? WHERE id=?", (weight, model_id))
    if "status" in body:
        execute("UPDATE models SET status=? WHERE id=?", (str(body["status"]), model_id))
    _cache["t"] = 0.0
    return {"model": model_id, "applied": body}


def patch_settings(body: dict[str, Any]) -> tuple[int, dict[str, Any]] | dict[str, Any]:
    if "concurrency" in body:
        try:
            conc = int(body["concurrency"])
        except (TypeError, ValueError):
            return _err("concurrency must be an integer")
        set_cfg("ui.concurrency", max(1, min(16, conc)))
    if "budget_minutes" in body:
        raw = body["budget_minutes"]
        try:
            set_cfg("ui.budget_minutes", float(raw) if raw else None)
        except (TypeError, ValueError):
            return _err("budget_minutes must be a number")
    if "judge_models" in body:
        val = body["judge_models"]
        if isinstance(val, str):
            val = [v.strip() for v in val.split(",") if v.strip()]
        if val is None or (isinstance(val, list) and not val):
            clear_cfg("global.judge_models")   # blank = per-task manifest
        elif isinstance(val, list):
            set_cfg("global.judge_models", [str(v) for v in val])
        else:
            return _err("judge_models must be a list or a comma string")
    if "mode" in body:
        set_cfg("ui.mode", str(body["mode"]))
    _cache["t"] = 0.0
    return {"settings": settings_payload()}


def score_now() -> dict[str, Any]:
    counts_ = recompute_scores()
    rows = leaderboard()
    from rank import write_leaderboard

    write_leaderboard(rows, profile=None)
    _cache["t"] = 0.0
    return {"recomputed": counts_, "rows": len(rows)}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "model-eval/1.0"
    protocol_version = "HTTP/1.1"

    # ---- plumbing -------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("WEBUI_QUIET"):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, default=str).encode(), "application/json")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1_000_000))
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    # ---- GET -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)
        try:
            if path.startswith("/api/"):
                self._api_get(path, qs)
            else:
                self._static(path)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _api_get(self, path: str, qs: dict[str, list[str]]) -> None:
        if path in ("/api/state", "/api/"):
            self._json(200, state_payload())
        elif path == "/api/leaderboard":
            self._json(200, _read_leaderboard())
        elif path == "/api/log":
            n = int((qs.get("lines") or ["200"])[0])
            self._json(200, {"lines": _log_tail(max(1, min(4000, n)))})
        elif path == "/api/runs":
            filters, where, params = [], [], []
            for col in ("task_id", "model", "stage", "status", "source"):
                if qs.get(col):
                    where.append(f"{col}=?")
                    params.append(qs[col][0])
            sql = "SELECT * FROM runs"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY started DESC LIMIT 500"
            self._json(200, {"runs": [dict(r) for r in q(sql, tuple(params))]})
        elif path.startswith("/api/run/"):
            self._json(200, run_detail(unquote(path[len("/api/run/"):])))
        elif path == "/api/tasks":
            self._json(200, {"tasks": task_summary()})
        elif path == "/api/models":
            self._json(200, {"models": [dict(r) for r in q("SELECT * FROM models ORDER BY id")]})
        elif path == "/api/settings":
            self._json(200, settings_payload())
        elif path == "/api/jobs":
            self._json(200, {"jobs": [dict(r) for r in
                                      q("SELECT * FROM jobs ORDER BY started DESC LIMIT 30")]})
        else:
            self._json(404, {"error": "unknown endpoint"})

    # ---- POST ------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        try:
            body = self._body()
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            if path == "/api/job/start":
                status, payload = _status(start_job(body))
            elif path == "/api/job/control":
                status, payload = _status(
                    control_job(str(body.get("action") or "")))
            elif path.startswith("/api/task/"):
                status, payload = _status(
                    patch_task(unquote(path[len("/api/task/"):]), body))
            elif path.startswith("/api/model/"):
                status, payload = _status(
                    patch_model(unquote(path[len("/api/model/"):]), body))
            elif path == "/api/models/refresh":
                _cache["t"] = 0.0
                status, payload = 200, refresh()
            elif path == "/api/settings":
                status, payload = _status(patch_settings(body))
            elif path == "/api/score":
                status, payload = 200, score_now()
            else:
                status, payload = 404, {"error": "unknown endpoint"}
            self._json(status, payload)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    # ---- static ----------------------------------------------------------
    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
            self._json(403, {"error": "forbidden"})
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            target = WEB_DIR / "index.html"
            if not target.is_file():
                self._json(404, {"error": "not found"})
                return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1",
                    help="loopback by default; no auth is implemented")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    ensure_layout()
    sync_registry()
    if args.quiet:
        os.environ["WEBUI_QUIET"] = "1"

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"model_eval control panel -> http://{args.host}:{args.port}")
    print(f"state: {os.environ.get('MODEL_EVAL_STATE', ROOT / 'state')}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
