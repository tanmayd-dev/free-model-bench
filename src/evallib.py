"""Core runtime for the model evaluation harness.

Responsibilities:
  * paths + configuration (single place to change where state lives)
  * SQLite access (WAL, the single source of truth)
  * running `opencode` / `agy` as subprocesses and parsing their output
  * artifact bookkeeping

Nothing in here knows about specific tasks -- that lives in tasks.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from fnmatch import fnmatch
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent

STATE_DIR = Path(os.environ.get("MODEL_EVAL_STATE", ROOT / "state"))
CONFIG_DIR = Path(os.environ.get("MODEL_EVAL_CONFIG", ROOT / "config"))
TASKS_DIR = CONFIG_DIR / "tasks"
RESULTS_DIR = ROOT / "results"
RUNS_DIR = STATE_DIR / "runs"
JOBS_DIR = STATE_DIR / "jobs"
LOCKS_DIR = STATE_DIR / "locks"
DB_PATH = STATE_DIR / "eval.db"
LIVE_LOG = STATE_DIR / "live.log"

# Bump to invalidate every cached run (prompt/template/algorithm changes).
HARNESS_VERSION = "1"

# Sandbox root. Deliberately under /tmp/opencode (pre-approved scratch space).
SANDBOX_ROOT = Path(os.environ.get("MODEL_EVAL_SANDBOX", "/tmp/opencode/model-eval"))


def ensure_layout() -> None:
    for d in (STATE_DIR, RESULTS_DIR, RUNS_DIR, JOBS_DIR, LOCKS_DIR, SANDBOX_ROOT):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_text(text: str) -> str:
    return sha_bytes(text.encode("utf-8"))


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def _ignored(rel: str, patterns: set[str]) -> bool:
    """Is this relative path covered by an ignore pattern?

    As well as a plain fnmatch, a bare name like ``hidden`` or ``reference``
    covers everything *underneath* it. Without that, ``hidden/`` and
    ``reference/`` -- both of which are grading inputs, never model inputs --
    were hashed into the fixture and editing one forced a full model re-run.
    """
    for pat in patterns:
        if fnmatch(rel, pat):
            return True
        if "/" not in pat and rel.startswith(pat + "/"):
            return True
    return False


def sha_tree(path: Path, *, ignore: set[str] | None = None) -> str:
    """Content hash of a directory: relative paths + contents.

    Two runs of the same fixture always produce the same value, and any edit
    to any file changes it, which is what drives cache invalidation.

    `ignore` entries are shell-style glob patterns matched against the path
    relative to `path` (so `*.pyc` and `__pycache__` both work); a pattern
    without a slash also excludes that directory as a whole.
    """
    ignore = ignore or set()
    h = hashlib.sha256()
    if not path.exists():
        return h.hexdigest()
    for p in sorted(path.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(path).as_posix()
        if _ignored(rel, ignore):
            continue
        h.update(rel.encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# --------------------------------------------------------------------------
# Transient vs permanent model failures
# --------------------------------------------------------------------------

# Errors that will never succeed on a retry: the model is gone or misconfigured.
PERMANENT_PATTERNS = (
    "has been deprecated",
    "model is unavailable",
    "model not found",
    "unknown model",
    "agent not found",
    "does not exist",
    "no such model",
    "unsupported model",
    "invalid model",
)

# Failures worth retrying inside a single job: 429s, 5xx, bad gateway, timeouts.
TRANSIENT_HINTS = (
    "429", "500", "502", "503", "504", "rate limit", "overloaded",
    "upstream", "temporarily", "connection", "timeout", "eof", "econnreset",
    "not valid json", "http 400", "http 408", "try again", "capacity",
)


def is_permanent_error(message: str) -> bool:
    m = (message or "").lower()
    if any(p in m for p in PERMANENT_PATTERNS):
        return True
    return False


def is_transient_error(message: str) -> bool:
    m = (message or "").lower()
    return any(h in m for h in TRANSIENT_HINTS)


def tail(path: Path, lines: int = 400) -> list[str]:
    try:
        with path.open(errors="replace") as fh:
            return fh.readlines()[-lines:]
    except FileNotFoundError:
        return []


def log_live(msg: str, *, kind: str = "info") -> None:
    """Append a line to state/live.log (and echo to stderr for interactive runs)."""
    line = f"{now_iso()} [{kind}] {msg}\n"
    with LIVE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line)
    if sys.stderr.isatty():
        sys.stderr.write(line)
        sys.stderr.flush()


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
  id            TEXT PRIMARY KEY,
  status        TEXT NOT NULL DEFAULT 'known',   -- new|known|degraded|delisted
  enabled       INTEGER NOT NULL DEFAULT 1,
  weight        REAL NOT NULL DEFAULT 1.0,
  first_seen    TEXT,
  last_full_run TEXT,
  last_seen     TEXT,
  notes         TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL DEFAULT '',
  enabled      INTEGER NOT NULL DEFAULT 1,
  profiles     TEXT NOT NULL DEFAULT '[]',   -- json array
  weight       REAL NOT NULL DEFAULT 1.0,
  manifest_sha TEXT,
  updated      TEXT
);

-- Per-model / per-task overrides applied on top of manifest.yaml.
-- key examples: 'task:e2e-feature:exclude_tests', 'model:opencode/x:enabled'
CREATE TABLE IF NOT EXISTS config (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id                TEXT PRIMARY KEY,
  task_id           TEXT NOT NULL,
  stage             TEXT NOT NULL,     -- plan|implement|verify
  model             TEXT NOT NULL,
  source            TEXT NOT NULL DEFAULT 'own',  -- own|gold (implement only)
  key               TEXT NOT NULL,     -- cache key (stage-specific)
  fixture_sha       TEXT NOT NULL,
  harness_version   TEXT NOT NULL,
  status            TEXT NOT NULL,     -- ok|error|timeout|cancelled|skipped
  started           TEXT,
  finished          TEXT,
  duration_s        REAL DEFAULT 0,
  tokens_in         INTEGER DEFAULT 0,
  tokens_out        INTEGER DEFAULT 0,
  tokens_reasoning  INTEGER DEFAULT 0,
  tokens_cache_read INTEGER DEFAULT 0,
  tool_calls        INTEGER DEFAULT 0,
  shell_calls       INTEGER DEFAULT 0,
  edit_calls        INTEGER DEFAULT 0,
  files_touched     INTEGER DEFAULT 0,
  tests_passed      INTEGER,
  tests_total       INTEGER,
  active_tests      TEXT,
  active_checks     TEXT,
  checks_payload    TEXT,
  score             REAL,
  artifacts_dir     TEXT,
  session_id        TEXT,
  error             TEXT,
  parent_id         TEXT,   -- implement run this verify run grades
  attempts          INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_cache
  ON runs(task_id, stage, model, key, fixture_sha, harness_version);
CREATE INDEX IF NOT EXISTS idx_runs_task_model ON runs(task_id, model);

CREATE TABLE IF NOT EXISTS judges (
  run_id     TEXT NOT NULL,
  judge_model TEXT NOT NULL,
  artifact_sha TEXT NOT NULL,   -- what was judged (input identity)
  rubric_sha TEXT NOT NULL,
  payload    TEXT NOT NULL,
  score      REAL,
  created    TEXT,
  PRIMARY KEY (run_id, judge_model, artifact_sha, rubric_sha)
);

CREATE TABLE IF NOT EXISTS jobs (
  id             TEXT PRIMARY KEY,
  mode           TEXT,
  args           TEXT,
  status         TEXT NOT NULL,   -- queued|running|paused|stopped|done|failed
  control        TEXT NOT NULL DEFAULT 'running',  -- running|pause|stop
  progress_done  INTEGER DEFAULT 0,
  progress_total INTEGER DEFAULT 0,
  current        TEXT,
  message        TEXT,
  pid            INTEGER,
  started        TEXT,
  finished       TEXT,
  log_path       TEXT
);
"""

_db_lock = threading.Lock()
_db_conn: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    """Process-wide SQLite connection (WAL allows concurrent readers/writers)."""
    global _db_conn
    if _db_conn is None:
        ensure_layout()
        conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        _db_conn = conn
    return _db_conn


def q(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _db_lock:
        return db().execute(sql, tuple(params)).fetchall()


def q1(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _db_lock:
        return db().execute(sql, tuple(params)).rowcount


def get_cfg(key: str, default: Any = None) -> Any:
    row = q1("SELECT value FROM config WHERE key=?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_cfg(key: str, value: Any) -> None:
    execute(
        "INSERT INTO config(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, default=str)),
    )


def clear_cfg(key: str) -> None:
    """Remove a setting so the default behind it speaks again.

    Storing an explicit null is not the same thing: it leaves a row that
    shadows nothing today but will shadow a later file edit tomorrow.
    """
    execute("DELETE FROM config WHERE key=?", (key,))


# --------------------------------------------------------------------------
# Running opencode
# --------------------------------------------------------------------------


@dataclass
class RunResult:
    status: str  # ok | error | timeout | cancelled
    text: str = ""           # concatenated assistant text
    events: list[dict] = field(default_factory=list)
    session_id: str = ""
    duration_s: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    shell_calls: int = 0
    edit_calls: int = 0
    error: str = ""
    stdout_path: Path | None = None
    stderr_path: Path | None = None

    @property
    def tokens_in(self) -> int:
        return int(self.tokens.get("input", 0))

    @property
    def tokens_out(self) -> int:
        return int(self.tokens.get("output", 0))


def _parse_stream(stdout_path: Path) -> tuple[list[dict], dict[str, Any]]:
    events: list[dict] = []
    totals = {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "tool_calls": 0,
        "shell_calls": 0,
        "edit_calls": 0,
        "session_id": "",
        "texts": [],
        "errors": [],
    }
    if not stdout_path.exists():
        return events, totals
    with stdout_path.open(errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(ev)
            session_id = ev.get("sessionID") or ""
            if session_id:
                totals["session_id"] = session_id
            typ = ev.get("type")
            part = ev.get("part") or {}

            if typ == "text":
                txt = part.get("text") or ""
                if txt:
                    totals["texts"].append(txt)
            elif typ == "tool_use" or part.get("type") == "tool":
                state = part.get("state") or {}
                tool = (part.get("tool") or "").lower()
                if state.get("status") == "completed":
                    totals["tool_calls"] += 1
                    if tool in ("shell", "bash", "exec", "terminal"):
                        totals["shell_calls"] += 1
                    elif tool in ("edit", "write", "multiedit", "patch", "create"):
                        totals["edit_calls"] += 1
            elif typ == "step_finish":
                tk = part.get("tokens") or {}
                totals["input"] += int(tk.get("input") or 0)
                totals["output"] += int(tk.get("output") or 0)
                totals["reasoning"] += int(tk.get("reasoning") or 0)
                cache = tk.get("cache") or {}
                totals["cache_read"] += int(cache.get("read") or 0)
            elif typ == "error":
                msg = ((ev.get("error") or {}).get("message")
                       or json.dumps(ev.get("error")))
                totals["errors"].append(msg)
    return events, totals


def run_opencode(
    *,
    model: str,
    agent: str,
    prompt: str,
    cwd: Path,
    timeout_s: float,
    auto: bool = True,
    artifacts_dir: Path | None = None,
    cancel_event: threading.Event | None = None,
    control_check=None,
) -> RunResult:
    """Run one `opencode run` and parse the JSON event stream.

    stdout/stderr are streamed to files so a timeout still leaves usable output.
    """
    artifacts_dir = artifacts_dir or (RUNS_DIR / "scratch")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifacts_dir / "events.ndjson"
    stderr_path = artifacts_dir / "stderr.log"
    cmd = ["opencode", "run", "--model", model, "--agent", agent, "--format", "json"]
    if auto:
        cmd.append("--auto")
    cmd.append(prompt)

    (artifacts_dir / "cmd.json").write_text(
        json.dumps({"cmd": cmd, "cwd": str(cwd), "model": model, "agent": agent},
                   indent=2, default=str)
    )

    start = time.monotonic()
    with stdout_path.open("w", encoding="utf-8") as out, \
            stderr_path.open("w", encoding="utf-8") as err:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=out, stderr=err, start_new_session=True
        )
        status = "ok"
        error = ""
        deadline = start + timeout_s
        while True:
            if cancel_event is not None and cancel_event.is_set():
                status, error = "cancelled", "cancelled by operator"
                proc.kill()
                break
            if control_check is not None:
                decision = control_check()
                if decision in ("stop", "pause"):
                    status, error = "cancelled", f"job {decision}"
                    proc.kill()
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status, error = "timeout", f"exceeded {int(timeout_s)}s"
                proc.kill()
                break
            try:
                proc.wait(timeout=min(0.5, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    duration = time.monotonic() - start
    events, totals = _parse_stream(stdout_path)
    text = "\n\n".join(t for t in totals["texts"]).strip()
    stream_errors = totals["errors"]

    if status == "timeout" and stream_errors:
        # Our deadline fired, but the stream shows the provider dropped the
        # connection (ECONNRESET, truncated body, ...). That is a failed call,
        # not a model that simply needed longer, so hand it to the stage-level
        # retry -- which can tell the difference -- and keep the deadline in
        # the message so the two stay distinguishable in the log.
        status = "error"
        error = (f"our {int(timeout_s)}s deadline reached after a provider fault: "
                 + "; ".join(stream_errors[:2])[:600])
    elif status == "ok":
        if stream_errors and not text:
            status = "error"
            error = "; ".join(stream_errors)[:2000]
        elif not text:
            status = "error"
            err_text = (stderr_path.read_text(errors="replace")[-2000:]
                        if stderr_path.exists() else "")
            error = "no assistant output produced. " + err_text.strip()[:1000]
        elif stream_errors:
            # partial success: keep going, but surface the error text
            error = "; ".join(stream_errors)[:2000]

    return RunResult(
        status=status,
        text=text,
        events=events,
        session_id=totals["session_id"],
        duration_s=duration,
        tokens={
            "input": totals["input"],
            "output": totals["output"],
            "reasoning": totals["reasoning"],
            "cache_read": totals["cache_read"],
        },
        tool_calls=totals["tool_calls"],
        shell_calls=totals["shell_calls"],
        edit_calls=totals["edit_calls"],
        error=error,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


# --------------------------------------------------------------------------
# Retry policy for model calls
# --------------------------------------------------------------------------

# Backoff in seconds before attempt 2, 3, 4... (we stop after len()+1 tries).
STAGE_RETRY_BACKOFF = (10, 30, 90)
# Total attempts = 1 initial + len(STAGE_RETRY_BACKOFF).
MAX_STAGE_ATTEMPTS = 1 + len(STAGE_RETRY_BACKOFF)


def run_opencode_retry(*, label: str, cancel_on=None, rebuild=None,
                       **kwargs) -> RunResult:
    """Run ``run_opencode``, retrying transient provider failures.

    Only ``status == "error"`` is retried. Timeouts are our own resource limit
    (retrying one doubles the wall clock for the same likely answer) and
    cancellations are operator intent, so neither is retried.

    ``cancel_on`` is an optional ``() -> str`` control probe, called between
    attempts and during backoff so a stop/pause does not sit through a sleep.

    ``rebuild`` is an optional ``Callable[[], dict]`` that returns a fresh
    kwargs dict for every attempt after the first -- used by the implement
    stage to start from a clean sandbox rather than its own half-written mess.
    """
    res: RunResult | None = None
    for attempt in range(1, MAX_STAGE_ATTEMPTS + 1):
        if attempt > 1 and rebuild is not None:
            kwargs = rebuild()
        res = run_opencode(**kwargs)

        if res.status in ("timeout", "cancelled"):
            return res
        if res.status == "ok":
            if attempt > 1:
                log_live(f"{label}: succeeded on attempt {attempt}", kind="warn")
            return res

        # status == "error" from here on
        if is_permanent_error(res.error):
            log_live(f"{label}: permanent failure, not retrying: "
                     f"{res.error[:160]}", kind="warn")
            return res
        if not is_transient_error(res.error):
            log_live(f"{label}: error is not a known transient class, "
                     f"not retrying: {res.error[:160]}", kind="warn")
            return res
        if attempt >= MAX_STAGE_ATTEMPTS:
            log_live(f"{label}: giving up after {attempt} attempts: "
                     f"{res.error[:166]}", kind="warn")
            return res

        delay = STAGE_RETRY_BACKOFF[attempt - 1]
        log_live(f"{label}: transient failure (attempt {attempt}/"
                 f"{MAX_STAGE_ATTEMPTS}), retrying in {delay}s: "
                 f"{res.error[:160]}", kind="warn")
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if cancel_on is not None and cancel_on() in ("stop", "pause"):
                return res
            time.sleep(min(1.0, deadline - time.monotonic()))
    return res  # pragma: no cover -- loop always returns


# --------------------------------------------------------------------------
# Running agy (judge)
# --------------------------------------------------------------------------


@dataclass
class JudgeResult:
    status: str
    payload: Any = None
    raw: str = ""
    duration_s: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def run_agy(
    *,
    model: str,
    prompt: str,
    schema_path: Path | None = None,
    timeout_s: float = 180.0,
    effort: str | None = None,
    mode: str | None = None,
) -> JudgeResult:
    """Run a judge through agy's print mode.

    NOTE: agy parses `--print` greedily -- the prompt MUST be attached to the
    flag (`--print='...'`) and `--model` must come before it.
    """
    cmd = ["agy", "--output-format", "json", "--disable-slash-commands"]
    if effort:
        cmd += ["--effort", effort]
    if mode:
        cmd += ["--mode", mode]
    cmd += ["--model", model]
    if schema_path is not None:
        cmd += ["--json-schema", str(schema_path)]
    cmd.append(f"--print={prompt}")

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        return JudgeResult(status="timeout", error=f"judge exceeded {int(timeout_s)}s")
    duration = time.monotonic() - start
    out = (proc.stdout or "").strip()
    if not out:
        return JudgeResult(
            status="error",
            raw=(proc.stderr or "")[-2000:],
            duration_s=duration,
            error=f"empty judge output (rc={proc.returncode}): {(proc.stderr or '')[-800:]}",
        )
    # agy may emit a leading informational line before the JSON object
    start_idx = out.find("{")
    if start_idx < 0:
        return JudgeResult(status="error", raw=out, duration_s=duration,
                           error="judge did not return JSON")
    try:
        data = json.loads(out[start_idx:])
    except json.JSONDecodeError as exc:
        return JudgeResult(status="error", raw=out, duration_s=duration,
                           error=f"unparseable judge output: {exc}")
    if (data.get("status") or "").upper() not in ("SUCCESS", "", None):
        return JudgeResult(status="error", raw=out, duration_s=duration,
                           error=f"judge status {data.get('status')}: {str(data.get('response'))[:800]}")
    payload = data.get("structured_output")
    if payload is None:
        payload = data.get("response")
    return JudgeResult(
        status="ok",
        payload=payload,
        raw=out,
        duration_s=duration,
        usage=data.get("usage") or {},
    )


# --------------------------------------------------------------------------
# Run caching
# --------------------------------------------------------------------------

# Statuses that mean "we have a usable answer; do not spend a model call".
TERMINAL_OK = ("ok",)
# Statuses that are cached too, but retryable (see attempts / --retry-failed).
TERMINAL_BAD = ("error", "timeout", "cancelled", "skipped")
MAX_ATTEMPTS = 3


def find_cached_run(
    task_id: str,
    stage: str,
    model: str,
    key: str,
    fixture_sha: str,
    *,
    retry_failed: bool = False,
) -> sqlite3.Row | None:
    """Return a usable cached run, or None when the work needs doing."""
    row = q1(
        "SELECT * FROM runs WHERE task_id=? AND stage=? AND model=? AND key=? "
        "AND fixture_sha=? AND harness_version=?",
        (task_id, stage, model, key, fixture_sha, HARNESS_VERSION),
    )
    if row is None:
        return None
    if row["status"] in TERMINAL_OK:
        return row
    if row["status"] not in TERMINAL_BAD:
        return None
    if retry_failed or int(row["attempts"] or 0) < MAX_ATTEMPTS:
        return None  # still retryable
    return row  # exhausted: treat the failure as cached


def clear_failed_runs(only_today: bool = False) -> int:
    """Drop known failures so the next run retries them (daily mode does this)."""
    sql = "DELETE FROM runs WHERE status IN ({})".format(
        ",".join("?" for _ in TERMINAL_BAD)
    )
    params: tuple = tuple(TERMINAL_BAD)
    if only_today:
        sql += " AND started >= date('now')"
    return execute(sql, params)


def finish_run(run_id: str, **fields: Any) -> None:
    fields.setdefault("finished", now_iso())
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
    vals.append(run_id)
    execute(f"UPDATE runs SET {', '.join(cols)} WHERE id=?", vals)


def claim_run(task_id: str, stage: str, model: str, key: str, fixture_sha: str,
              source: str = "own") -> str:
    """Insert-or-reuse the run row for this cache key and return its id.

    Also bumps `attempts`, which is what stops a permanently-broken model from
    burning a whole night on retries.
    """
    row = q1(
        "SELECT id FROM runs WHERE task_id=? AND stage=? AND model=? AND key=? "
        "AND fixture_sha=? AND harness_version=?",
        (task_id, stage, model, key, fixture_sha, HARNESS_VERSION),
    )
    if row:
        run_id = row["id"]
        execute("UPDATE runs SET attempts=COALESCE(attempts,0)+1, status='running', "
                "started=? WHERE id=?", (now_iso(), run_id))
        return run_id
    run_id = "run_" + uuid.uuid4().hex[:20]
    execute(
        "INSERT INTO runs(id, task_id, stage, model, source, key, fixture_sha, "
        "harness_version, status, started, attempts) "
        "VALUES(?,?,?,?,?,?,?,?, 'running', ?, 1)",
        (run_id, task_id, stage, model, source, key, fixture_sha, HARNESS_VERSION,
         now_iso()),
    )
    return run_id


# --------------------------------------------------------------------------
# Job control (shared with the web UI)
# --------------------------------------------------------------------------

CONTROL_POLL_INTERVAL = 0.5


def create_job(mode: str, args: dict[str, Any], log_path: Path) -> str:
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    execute(
        "INSERT INTO jobs(id,mode,args,status,control,started,log_path) "
        "VALUES(?,?,?,'running','running',?,?)",
        (job_id, mode, json.dumps(args, default=str), now_iso(), str(log_path)),
    )
    return job_id


def update_job(job_id: str, **fields: Any) -> None:
    if not job_id:
        return
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
    if not cols:
        return
    vals.append(job_id)
    execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", vals)


def job_control(job_id: str) -> str:
    if not job_id:
        return "running"
    row = q1("SELECT control FROM jobs WHERE id=?", (job_id,))
    return row["control"] if row else "running"


def make_control_check(job_id: str):
    """Called between units by the runner; returns 'running'|'pause'|'stop'."""
    cache: dict[str, Any] = {"t": 0.0, "v": "running"}

    def check() -> str:
        now = time.monotonic()
        if now - cache["t"] > CONTROL_POLL_INTERVAL:
            cache["t"] = now
            cache["v"] = job_control(job_id)
        return cache["v"]

    return check


# --------------------------------------------------------------------------
# Sandbox management
# --------------------------------------------------------------------------


def make_sandbox(task_dir: Path, run_id: str) -> Path:
    """Fresh copy of a task fixture. Models never share state."""
    sandbox = SANDBOX_ROOT / run_id
    if sandbox.exists():
        shutil.rmtree(sandbox, ignore_errors=True)
    shutil.copytree(task_dir / "fixture", sandbox)
    (sandbox / "opencode.jsonc").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "plugin": False,
                "mcp": {},
                "permission": {
                    "edit": "allow",
                    "bash": "allow",
                    "webfetch": "deny",
                },
            },
            indent=2,
        )
    )
    return sandbox


def drop_sandbox(sandbox: Path) -> None:
    shutil.rmtree(sandbox, ignore_errors=True)


def snapshot_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))


# --------------------------------------------------------------------------
# Diff / patch
# --------------------------------------------------------------------------


def make_diff(fixture_dir: Path, work_dir: Path) -> str:
    try:
        proc = subprocess.run(
            ["diff", "-ruN", "--exclude=.git", "--exclude=opencode.jsonc",
             str(fixture_dir), str(work_dir)],
            capture_output=True, text=True, timeout=60,
        )
        out = proc.stdout or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"# diff failed: {exc}"
    if len(out) > 1_000_000:
        out = out[:1_000_000] + "\n# ... truncated ...\n"
    return out


def _file_map(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == "opencode.jsonc" or "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        out[rel] = p.read_bytes()
    return out


def tree_diff(fixture_dir: Path, work_dir: Path) -> tuple[list[str], list[str], list[str]]:
    """(modified, created, deleted) relative to the pristine fixture."""
    a, b = _file_map(fixture_dir), _file_map(work_dir)
    changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    created = sorted(set(b) - set(a))
    deleted = sorted(set(a) - set(b))
    return changed, created, deleted


# --------------------------------------------------------------------------
# Redaction (keep the judge blind to model identity / host paths)
# --------------------------------------------------------------------------

REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"/tmp/opencode/model-eval/[^\"'\s)]*"), "<SANDBOX>"),
    (re.compile(r"/home/tanmay/[^\"'\s)]*"), "<HOME>"),
    (re.compile(r"\bopencode/[a-z0-9][a-z0-9.\-]*-free\b"), "<MODEL>"),
    (re.compile(r"\bses_[A-Za-z0-9]+\b"), "<SESSION>"),
    (re.compile(r"\bmsg_[A-Za-z0-9]+\b"), "<MSG>"),
    (re.compile(r"\bprt_[A-Za-z0-9]+\b"), "<PART>"),
]


def redact(text: str) -> str:
    if not text:
        return ""
    for pat, repl in REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return text
