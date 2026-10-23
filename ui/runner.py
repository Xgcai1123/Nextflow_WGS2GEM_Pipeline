"""Run state machine, engine-log contract parser and report discovery.

The contract is docs/ui.md sections 4, 5, 7.1 and 9.  This module owns
everything about *a run*; :mod:`server` owns HTTP and :mod:`sheet` owns the
sample table.

Design notes
------------
* The engine is an opaque subprocess.  The UI must never guess from exit code
  alone: **exit 0 without a located report is ``failed(report_missing)``**
  (UI-D6), while a non-zero exit becomes ``failed`` with the process/tag/work
  dir/stderr tail parsed out of the engine's own error block.
* All progress comes from the log contract (``Submitted|Completed process >
  NAME (tag)``), replayed from byte 0 when the UI attaches to a run it did not
  launch.  ``run.log`` is append-only, so byte offsets are stable across
  refreshes and SSE reconnects.
* A UI restart is not a pipeline restart: a live pid is *attached* (state stays
  ``running``), a dead pid is ``interrupted``.  There is deliberately no
  third path that pretends to know the exit code of a process it did not reap.
* Cancellation is SIGTERM to the whole process group, 10 s grace, then SIGKILL.
  ``cancelled`` is not ``failed`` and ``work/`` is never touched (UI-D7), so
  ``-resume`` can converge.
"""

import codecs
import copy
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time

import sheet

# ---------------------------------------------------------------------------
# The stage ladder.  Twelve process names, in dataflow order; ASSEMBLY_REF is
# per-sample-but-optional and the last two are cohort-wide singletons.
# ---------------------------------------------------------------------------
PROCESS_ORDER = [
    "FASTQC_RAW",
    "TRIMMOMATIC",
    "FASTQC_TRIMMED",
    "SPADES",
    "QUAST",
    "ASSEMBLY_REF",
    "BUSCO",
    "BAKTA",
    "CARVEME",
    "MEMOTE",
    "GROWTH_CHECK",
    "MULTIQC",
]

COHORT_PROCESSES = ("GROWTH_CHECK", "MULTIQC")
PER_SAMPLE_PROCESSES = tuple(p for p in PROCESS_ORDER if p not in COHORT_PROCESSES)

#: contract section 5: expected = 11*N + refs + 2
BASE_PER_SAMPLE = 11

SUBMITTED_RE = re.compile(r"Submitted process > ([A-Za-z0-9_]+) \((.+?)\)\s*$")
COMPLETED_RE = re.compile(r"Completed process > ([A-Za-z0-9_]+) \((.+?)\)\s*$")

# Real Nextflow (26.10.0) prints only `Submitted process >` to stdout; task
# COMPLETION is written to `.nextflow.log` by TaskPollingMonitor, at DEBUG:
#   [Task monitor] DEBUG ... Task completed > TaskHandler[id: 6; name: FASTQC_RAW
#   (SRR10194530_2); status: COMPLETED; exit: 0; error: -; workDir: ...]
# A parser that only reads stdout therefore shows "0 completed" for the whole real
# run -- found during the 2026-10-23 acceptance run (F48), while the fake engine
# (which printed Completed lines itself) kept the tests green. Keep both: the
# stdout form for the fake engine and older engines, the log form for the real one.
TASK_COMPLETED_RE = re.compile(
    r"Task completed > TaskHandler\[id: \d+; name: ([A-Za-z0-9_]+) \((.+?)\); "
    r"status: (COMPLETED|FAILED|ABORTED|CACHED)")
CACHED_RE = re.compile(r"Cached process > ([A-Za-z0-9_]+) \((.+?)\)\s*$")
ERROR_PROCESS_RE = re.compile(r"Error executing process > '?([A-Za-z0-9_]+) \(([^)]*)\)'?")
REPORT_RE = re.compile(r"\]\s*report\s*=\s*(/\S+?\.html)\s*$")


class RunError(Exception):
    """Base class for start/cancel refusals that the HTTP layer maps to 4xx."""

    status = 409
    code = "busy"

    def __init__(self, message, run=None):
        Exception.__init__(self, message)
        self.message = message
        self.run = run

    def payload(self):
        body = {"error": self.code, "message": self.message}
        if self.run is not None:
            body["run"] = self.run
        return body


class BusyError(RunError):
    code = "busy"


class NoActiveRunError(RunError):
    code = "no_active_run"


class NextflowMissingError(RunError):
    code = "nextflow_missing"


# ---------------------------------------------------------------------------
# Engine log parsing
# ---------------------------------------------------------------------------

_STDERR_SECTION_PREFIXES = (
    "Command executed:",
    "Command exit status:",
    "Command output:",
    "Command error:",
    "Work dir:",
    "Tip:",
    "Caused by:",
)


class LogParser:
    """Incremental parser for Nextflow's stdout/stderr log contract.

    The log is read in chunks and decoded with an incremental UTF-8 decoder so
    a multi-byte character split across two reads cannot corrupt the stream.
    Lines are consumed once, so feeding the file incrementally and replaying it
    from byte 0 produce identical state.
    """

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._partial = ""
        self.bytes_consumed = 0
        # `.nextflow.log` is a second, independent stream (engine-side completion
        # lines); it needs its own incremental decoder so a chunk boundary in one
        # stream cannot corrupt the other.
        self._engine_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._engine_partial = ""
        self.engine_bytes_consumed = 0
        self.submitted = set()      # (process, tag)
        self.completed = set()      # (process, tag)
        self.failed = set()         # (process, tag)
        self.last_report = None     # last absolute report path from onComplete
        self.has_error_block = False
        self.fail = {
            "process": None,
            "tag": None,
            "exit_code": None,
            "work_dir": None,
            "stderr_tail": None,
        }
        self._stderr_lines = []
        self._stderr_mode = False
        self._pending = None        # "exit" | "workdir"
        self._cleaned = False

    # -- feeding ----------------------------------------------------------
    def feed(self, data):
        if not data:
            return
        self.bytes_consumed += len(data)
        text = self._decoder.decode(data)
        if not text:
            return
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._consume_line(line.rstrip("\r"))

    def flush(self):
        """Process a final line that has no trailing newline yet."""
        if self._partial:
            line, self._partial = self._partial, ""
            self._consume_line(line.rstrip("\r"))

    def feed_engine_log(self, data):
        """Incremental reader for ``.nextflow.log`` (completion status only)."""
        if not data:
            return
        self.engine_bytes_consumed += len(data)
        text = self._engine_decoder.decode(data)
        if not text:
            return
        self._engine_partial += text
        while "\n" in self._engine_partial:
            line, self._engine_partial = self._engine_partial.split("\n", 1)
            self._consume_engine_line(line.rstrip("\r"))

    def flush_engine_log(self):
        if self._engine_partial:
            line, self._engine_partial = self._engine_partial, ""
            self._consume_engine_line(line.rstrip("\r"))

    def _consume_engine_line(self, line):
        m = TASK_COMPLETED_RE.search(line)
        if not m:
            return
        key = (m.group(1), m.group(2).strip())
        self.submitted.add(key)
        status = m.group(3)
        if status in ("COMPLETED", "CACHED"):
            self.completed.add(key)
        elif status in ("FAILED", "ABORTED"):
            self.failed.add(key)

    # -- line handling ----------------------------------------------------
    def _consume_line(self, line):
        stripped = line.strip()

        m = SUBMITTED_RE.search(line)
        if m:
            self.submitted.add((m.group(1), m.group(2).strip()))
        m = COMPLETED_RE.search(line)
        if m:
            self.completed.add((m.group(1), m.group(2).strip()))
        m = CACHED_RE.search(line)
        if m:
            # -resume re-publishes cached tasks without a Submitted line; the UI
            # must show them as done, never as missing progress.
            key = (m.group(1), m.group(2).strip())
            self.submitted.add(key)
            self.completed.add(key)

        m = REPORT_RE.search(line)
        if m:
            self.last_report = m.group(1)

        if ("ERROR " in line and "Error executing process" in line) or \
                ERROR_PROCESS_RE.search(line):
            self.has_error_block = True
        m = ERROR_PROCESS_RE.search(line)
        if m:
            self.fail["process"] = m.group(1)
            self.fail["tag"] = m.group(2).strip()
            self.failed.add((m.group(1), m.group(2).strip()))
        if re.search(r"(^|\]\s)ERROR\b", line) or stripped.startswith("ERROR"):
            self.has_error_block = True

        # -- "Command exit status:" + next non-empty line ------------------
        if self._pending == "exit":
            m = re.match(r"^(\d+)\s*$", stripped)
            if m:
                self.fail["exit_code"] = int(m.group(1))
                self._pending = None
            elif stripped == "":
                pass
            else:
                # Some engine versions print "Command exit status: 1" on one line.
                m = re.search(r"exit status:?\s+(-?\d+)", stripped)
                if m:
                    self.fail["exit_code"] = int(m.group(1))
                self._pending = None
        if self._pending == "workdir":
            if stripped:
                self.fail["work_dir"] = stripped
                self._pending = None

        # -- "Command error:" block, kept as the last 40 lines -------------
        if self._stderr_mode:
            if stripped == "" or stripped.startswith(_STDERR_SECTION_PREFIXES):
                self._stderr_mode = False
            else:
                self._stderr_lines.append(line)
        if stripped.startswith("Command error:"):
            self._stderr_mode = True
            self.has_error_block = True
            rest = stripped[len("Command error:"):].strip()
            if rest:
                self._stderr_lines.append(rest)
        elif stripped.startswith("Command exit status:"):
            m = re.search(r"exit status:?\s+(-?\d+)", stripped)
            if m:
                self.fail["exit_code"] = int(m.group(1))
            else:
                self._pending = "exit"
        elif stripped.startswith("Work dir:"):
            rest = stripped[len("Work dir:"):].strip()
            if rest:
                self.fail["work_dir"] = rest
            else:
                self._pending = "workdir"

        if self._stderr_lines:
            self.fail["stderr_tail"] = "\n".join(self._stderr_lines[-40:])

    # -- derived views ----------------------------------------------------
    def task_counts(self):
        return {
            "submitted": len(self.submitted),
            "completed": len(self.completed),
            "failed": len(self.failed),
        }

    def steps(self, samples, refs):
        """The 12-stage ladder with per-process and per-sample completion."""
        counts_sub = {}
        counts_done = {}
        counts_fail = {}
        for process, _tag in self.submitted:
            counts_sub[process] = counts_sub.get(process, 0) + 1
        for process, _tag in self.completed:
            counts_done[process] = counts_done.get(process, 0) + 1
        for process, _tag in self.failed:
            counts_fail[process] = counts_fail.get(process, 0) + 1

        steps = []
        for process in PROCESS_ORDER:
            submitted = counts_sub.get(process, 0)
            completed = counts_done.get(process, 0)
            failed = counts_fail.get(process, 0)
            running = max(0, submitted - completed - failed)
            by_sample = {}
            if process in PER_SAMPLE_PROCESSES:
                by_sample = {name: 0 for name in samples}
                for seen_process, tag in self.completed:
                    if seen_process != process:
                        continue
                    name = sample_for_tag(tag, samples)
                    if name in by_sample:
                        by_sample[name] += 1
            steps.append({
                "process": process,
                "expected": expected_for(process, len(samples), refs),
                "completed": completed,
                "running": running,
                "failed": failed,
                "by_sample": by_sample,
            })
        return steps


def sample_for_tag(tag, samples):
    """Longest sample name that is a prefix of the task tag (or the tag itself)."""
    best = None
    for name in samples:
        if tag == name or tag.startswith(name + "_"):
            if best is None or len(name) > len(best):
                best = name
    return best


def expected_for(process, n_samples, refs):
    if process == "FASTQC_RAW" or process == "FASTQC_TRIMMED":
        return 2 * n_samples
    if process == "ASSEMBLY_REF":
        return refs
    if process in COHORT_PROCESSES:
        return 1
    if process in PER_SAMPLE_PROCESSES:
        return n_samples
    return 0


# ---------------------------------------------------------------------------
# Small filesystem / process helpers
# ---------------------------------------------------------------------------

def atomic_write_json(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def dir_bytes(path):
    """Sum of regular-file sizes under *path* (best effort, follows symlinks)."""
    total = 0
    try:
        walker = os.walk(path, onerror=lambda exc: None)
        for root, _dirs, files in walker:
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def count_conda_envs(conda_dir):
    """Number of built environments under ``.nextflow/conda``.

    Nextflow stores one directory per environment; the marker that it is a real
    environment (not a half-finished download) is ``conda-meta/`` or a working
    ``bin/python``.
    """
    try:
        entries = sorted(os.listdir(conda_dir))
    except OSError:
        return 0
    count = 0
    for name in entries:
        path = os.path.join(conda_dir, name)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        if os.path.isdir(os.path.join(path, "conda-meta")) or \
                os.path.isfile(os.path.join(path, "bin", "python")):
            count += 1
    return count


def _parse_ps_lines(text):
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), parts[1]))
    return rows


# Child processes this server started and has not reaped yet. `subprocess.run`
# hides the pid, but the busy detector must never mistake our own `nextflow
# -version` (preflight) for a foreign engine -- that produced a false 409 when
# the user pressed Start just after the page auto-preflighted (F50).
_ACTIVE_CHILDREN = set()
_ACTIVE_CHILDREN_LOCK = threading.Lock()


def _active_child_pids():
    with _ACTIVE_CHILDREN_LOCK:
        return set(_ACTIVE_CHILDREN)


def _register_child(pid):
    with _ACTIVE_CHILDREN_LOCK:
        _ACTIVE_CHILDREN.add(int(pid))


def _unregister_child(pid):
    with _ACTIVE_CHILDREN_LOCK:
        _ACTIVE_CHILDREN.discard(int(pid))


def _parse_ps_lines(text, with_ppid=False):
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2 if with_ppid else 1)
        if with_ppid:
            if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        else:
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            rows.append((int(parts[0]), None, parts[1]))
    return rows


def _ps_table():
    """[(pid, ppid, command)]; ppid is None when only pgrep is available.

    Some restricted environments deny ``ps`` but allow ``pgrep``; both are
    optional (the caller simply sees fewer rows), which matters because this is
    only *detection*, never the mechanism that launches or cancels a run.
    """
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return _parse_ps_lines(proc.stdout, with_ppid=True)
    except (OSError, subprocess.SubprocessError):
        pass
    rows = _pgrep_table()
    return rows if rows is not None else []


def _pgrep_table():
    """Process table via `pgrep -fl nextflow`; None when pgrep is unavailable."""
    try:
        proc = subprocess.run(
            ["pgrep", "-fl", "nextflow"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):
        return None
    return _parse_ps_lines(proc.stdout, with_ppid=False)


_NEXTFLOW_EXE_NAMES = frozenset(("nextflow", "fake-nextflow", "nextflow.bat"))


def looks_like_nextflow(cmd):
    """Is this command line a *running* engine? Detection only, never launching.

    The project directory is called ``.../WGSpipeline-nextflow``, so "the string
    nextflow appears somewhere" is not evidence -- a python server, a shell
    wrapper, or `rm -rf .nextflow/ui` all mention it. Only two shapes count, and
    either must carry the ``run`` argument, so a one-off ``nextflow -version`` /
    ``nextflow config -flat`` (the preflight) is never mistaken for a run (F50):

      * an executable whose basename is exactly nextflow / fake-nextflow, incl.
        ``python .../fake-nextflow run .`` and ``sh -c '... nextflow run .'``;
      * a JVM launcher: ``nextflow.cli.Launcher``, ``nextflow.jar``, or
        ``java -jar .../nextflow-<version>-<build>.jar run ...``.
    """
    if not cmd or "nextflow" not in cmd.lower():
        return False
    low = cmd.lower()
    if "ui/server.py" in cmd or "bin/pipeline-ui" in cmd:
        return False
    try:
        argv = shlex.split(cmd)
    except ValueError:
        argv = cmd.split()
    if not argv:
        return False
    exe = os.path.basename(argv[0]).lower()
    if exe in ("sh", "bash", "zsh", "dash") and "-c" in argv:
        idx = argv.index("-c")
        if idx + 1 < len(argv):
            return looks_like_nextflow(argv[idx + 1])
    launcher = ("nextflow.cli.launcher" in low or "nextflow.jar" in low)
    if not launcher:
        # Any argument whose basename is exactly the launcher (covers
        # `python .../fake-nextflow`, `sh -c 'cd x && nextflow run .'`, and the
        # conda script itself); `--nextflow /path/nextflow` is harmless because
        # the "run" requirement below still has to hold.
        launcher = any(os.path.basename(tok).lower() in _NEXTFLOW_EXE_NAMES
                       for tok in argv)
    if not launcher:
        for i, tok in enumerate(argv):
            if tok == "-jar" and i + 1 < len(argv):
                jar = os.path.basename(argv[i + 1]).lower()
                launcher = jar.endswith(".jar") and "nextflow" in jar
                break
    if not launcher:
        return False
    return "run" in argv


def external_nextflow_pids_from_rows(rows, own_pids):
    """Pure form of the busy scan so it can be tested without a process table.

    ``own_pids`` are this server's pid, its recorded run pid and every child it
    spawned; all of their **descendants** are excluded too. Otherwise the
    ``nextflow -version`` JVM that the page's own preflight starts would look like
    a foreign run and block the Start button with a 409 (F50).
    """
    parent = {pid: ppid for pid, ppid, _cmd in rows if ppid}
    children = {}
    for pid, ppid in parent.items():
        children.setdefault(ppid, []).append(pid)
    excluded = set(own_pids)
    stack = list(own_pids)
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in excluded:
                excluded.add(child)
                stack.append(child)
    found = []
    for pid, _ppid, command in rows:
        if pid in excluded:
            continue
        if looks_like_nextflow(command):
            found.append(pid)
    return found



# ---------------------------------------------------------------------------
# nextflow config -flat (startup only; the UI never parses nextflow.config)
# ---------------------------------------------------------------------------

def default_params(project):
    return {
        "source": "fallback",
        "multiqc_dir": os.path.join(project, "results", "report"),
        "report_name": "multiqc_report",
        "min_free_disk_gb": 20,
        "raw_dir": os.path.join(project, "data", "raw"),
        "ref_dir": os.path.join(project, "data", "reference"),
        "bakta_db": os.path.join(project, "data", "db", "bakta", "db-light"),
        "busco_db": os.path.join(project, "data", "db", "busco"),
        "busco_lineage": "enterobacterales_odb10",
        "project_dir": project,
    }


def _unquote_config_value(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        if re.match(r"^-?\d+$", raw):
            return int(raw)
        if re.match(r"^-?\d+\.\d+$", raw):
            return float(raw)
    except ValueError:
        pass
    return raw


def parse_flat_config(text):
    params = {}
    for line in text.splitlines():
        m = re.match(r"\s*params\.([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if not m:
            continue
        params[m.group(1)] = _unquote_config_value(m.group(2))
    return params


def _config_overlay_args(extra_args):
    """Only -c/--config belongs to `nextflow config`; -stub-run etc. do not."""
    out = []
    i = 0
    while i < len(extra_args):
        arg = extra_args[i]
        if arg in ("-c", "--config") and i + 1 < len(extra_args):
            out.extend([arg, extra_args[i + 1]])
            i += 2
            continue
        if arg.startswith("-c ") or arg.startswith("-c=") or arg.startswith("--config="):
            out.append(arg)
        i += 1
    return out


def read_flat_config(project, nextflow, extra_args):
    """Run ``nextflow config -flat`` once at startup (15 s timeout).

    Failure, timeout or a missing engine falls back to built-in defaults and
    reports ``source: "fallback"`` so preflight can warn.  The UI never parses
    ``nextflow.config`` text (contract section 7.1) -- CI overlays must be
    honoured, and only the engine knows how.

    Overlay configs (``--nextflow-arg=-c``) are global launcher options, so the
    working form is ``nextflow -c <overlay> config -flat``; ``nextflow config
    -flat -c <overlay>`` is rejected with "Unknown option: -c".  The overlay
    attempt is tried first and a plain ``config -flat`` is the fallback, which
    also keeps the fake engine (which only understands the plain form) working.
    """
    params = default_params(project)
    if not nextflow:
        return params, "nextflow not found, cannot run config -flat"
    overlay = _config_overlay_args(extra_args)
    attempts = []
    if overlay:
        attempts.append([nextflow] + overlay + ["config", "-flat"])
    attempts.append([nextflow, "config", "-flat"])
    last_error = ""
    for argv in attempts:
        try:
            proc = subprocess.run(
                argv, cwd=project, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            last_error = "nextflow config -flat timed out (15s)"
            continue
        except OSError as exc:
            last_error = "nextflow config -flat could not be executed: %s" % exc
            continue
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            last_error = "nextflow config -flat exited with code %d: %s" % (
                proc.returncode, " / ".join(tail))
            continue
        parsed = parse_flat_config(proc.stdout or "")
        if not parsed:
            last_error = "nextflow config -flat output could not be parsed"
            continue
        params.update(parsed)
        # `params.report_name` is assigned at *runtime* in main.nf's workflow
        # body, so it never appears in `config -flat`; the filename base is a
        # fixed contract value, not a guess.
        if not params.get("report_name"):
            params["report_name"] = "multiqc_report"
        params["source"] = "nextflow config -flat"
        return params, ""
    return params, last_error


# ---------------------------------------------------------------------------
# Run manager
# ---------------------------------------------------------------------------

class RunManager(object):
    """Owns at most one run; the HTTP layer calls only these methods."""

    def __init__(self, project, nextflow, extra_args, params, ui_dir):
        self.project = os.path.abspath(project)
        self.nextflow = nextflow
        self.extra_args = list(extra_args or [])
        self.params = dict(params or {})
        self.runs_dir = os.path.join(ui_dir, "runs")
        self.state_file = os.path.join(ui_dir, "state.json")
        os.makedirs(self.runs_dir, mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.run = None
        self.proc = None
        self.parser = None
        self._log_pos = 0
        self._engine_pos = 0
        self._engine_ino = None
        self._refs = 0
        self._stop = threading.Event()
        self._watch_thread = None
        self._attached = False
        self._last_persist = 0.0

    # -- lifecycle --------------------------------------------------------
    def load(self):
        """Restore the last run: attach to a live pid, else mark interrupted."""
        payload = None
        try:
            with open(self.state_file, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            payload = None
        run = None
        if isinstance(payload, dict):
            candidate = payload.get("run")
            if isinstance(candidate, dict):
                run = candidate
        if run is None:
            return
        with self.lock:
            self.run = self._normalise_run(run)
            if self.run.get("state") == "running":
                if self._pid_alive_and_nextflow(self.run.get("pid")):
                    self._attach_locked()
                else:
                    self._mark_interrupted_locked()
            else:
                # Terminal run from a previous server: restore progress views so
                # GET /api/state can render the last result.  Recompute from the
                # log when it is still there; otherwise keep what state.json
                # recorded (`.nextflow/ui/` survives `pipeline-clean outputs`).
                self._attach_parser_locked(replay=False)
                self._replay_log_locked()
                if self.parser.bytes_consumed:
                    self.parser.flush()
                    self._apply_progress_locked()

    def _normalise_run(self, run):
        base = {
            "id": run.get("id") or "unknown",
            "state": run.get("state") or "idle",
            "mode": run.get("mode") or "fresh",
            "clean_requested": bool(run.get("clean_requested")),
            "pid": run.get("pid"),
            "since_pid_alive": bool(run.get("since_pid_alive")),
            "started_at": run.get("started_at") or time.time(),
            "finished_at": run.get("finished_at"),
            "elapsed_seconds": run.get("elapsed_seconds") or 0.0,
            "exit_code": run.get("exit_code"),
            "cancel_requested": bool(run.get("cancel_requested")),
            "sheet": run.get("sheet"),
            "log": run.get("log"),
            "log_bytes": run.get("log_bytes") or 0,
            "samples": list(run.get("samples") or []),
            "argv": list(run.get("argv") or []),
            "tasks": run.get("tasks") or {
                "submitted": 0, "completed": 0, "failed": 0, "expected": 0},
            "steps": run.get("steps") or [],
            "failed": run.get("failed"),
            "report": run.get("report"),
        }
        return base

    def _attach_parser_locked(self, replay=False):
        run = self.run
        self.parser = LogParser()
        self._log_pos = 0
        self._engine_pos = 0
        self._engine_ino = None
        self._refs = sheet.read_refs(run.get("sheet") or "")
        if not run.get("samples"):
            run["samples"] = sheet.samples_from_sheet(run.get("sheet") or "")
        if replay:
            self._replay_log_locked()
        self._apply_progress_locked()

    def _replay_log_locked(self):
        log = self.run.get("log")
        if not log or not os.path.isfile(log):
            return
        try:
            with open(log, "rb") as fh:
                while True:
                    chunk = fh.read(262144)
                    if not chunk:
                        break
                    self.parser.feed(chunk)
                    self._log_pos += len(chunk)
            self.parser.flush()
        except OSError:
            pass
        self._replay_engine_log_locked()

    def _replay_engine_log_locked(self):
        """Read `.nextflow.log` from the beginning (attach / terminal restore)."""
        path = os.path.join(self.project, ".nextflow.log")
        try:
            size = os.path.getsize(path)
            ino = os.stat(path).st_ino
        except OSError:
            return
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(262144)
                    if not chunk:
                        break
                    self.parser.feed_engine_log(chunk)
                    self._engine_pos += len(chunk)
            self.parser.flush_engine_log()
            self._engine_ino = ino
        except OSError:
            pass

    def _refresh_engine_log_locked(self):
        """Tail `.nextflow.log`; reset on rotation (nextflow moves it to .1)."""
        path = os.path.join(self.project, ".nextflow.log")
        try:
            st = os.stat(path)
        except OSError:
            return
        if self._engine_ino != st.st_ino or st.st_size < self._engine_pos:
            self._engine_pos = 0
            self._engine_ino = st.st_ino
        if st.st_size > self._engine_pos:
            try:
                with open(path, "rb") as fh:
                    fh.seek(self._engine_pos)
                    data = fh.read(st.st_size - self._engine_pos)
            except OSError:
                return
            if data:
                self.parser.feed_engine_log(data)
                self._engine_pos += len(data)

    def _attach_locked(self):
        run = self.run
        self._attached = True
        self.proc = None
        run["since_pid_alive"] = True
        self._attach_parser_locked(replay=True)
        self._persist_locked()
        self._start_watcher()

    def _mark_interrupted_locked(self):
        run = self.run
        run["state"] = "interrupted"
        run["since_pid_alive"] = False
        run["exit_code"] = run.get("exit_code")
        if not run.get("finished_at"):
            run["finished_at"] = time.time()
        run["elapsed_seconds"] = round(
            max(0.0, run["finished_at"] - run["started_at"]), 1)
        run["failed"] = {
            "process": None,
            "tag": None,
            "exit_code": run.get("exit_code"),
            "reason": "interrupted",
            "work_dir": None,
            "stderr_tail": None,
            "message": "The previous run ended or was killed after the UI exited; use -resume to continue",
        }
        self._attach_parser_locked(replay=True)
        self._persist_locked()

    # -- state views ------------------------------------------------------
    def snapshot(self):
        with self.lock:
            self._refresh_locked()
            run = copy.deepcopy(self.run)
        if run is None:
            return None
        now = time.time()
        end = run.get("finished_at") or now
        run["elapsed_seconds"] = round(max(0.0, end - (run.get("started_at") or end)), 1)
        log = run.get("log")
        if log:
            try:
                run["log_bytes"] = os.path.getsize(log)
            except OSError:
                pass
        return run

    def is_running(self):
        with self.lock:
            return bool(self.run and self.run.get("state") == "running")

    def log_path(self):
        with self.lock:
            return self.run.get("log") if self.run else None

    def sheet_path(self):
        with self.lock:
            return self.run.get("sheet") if self.run else None

    def report_path(self):
        with self.lock:
            return self.run.get("report") if self.run else None

    def results_root(self):
        """``<results_root>/report/<name>.html`` -> ``<results_root>`` (section 9)."""
        with self.lock:
            run = self.run
            report = (run or {}).get("report")
        path = (report or {}).get("path")
        if not path:
            path = self._locate_report_static()
        if path:
            report_dir = os.path.dirname(path)
            if os.path.basename(report_dir) == "report":
                root = os.path.dirname(report_dir)
                if os.path.isdir(root):
                    return root
        fallback = os.path.join(self.project, "results")
        return fallback

    def _locate_report_static(self):
        """Location steps 2 and 3 of section 9, without the log."""
        mdir = self.params.get("multiqc_dir")
        rname = self.params.get("report_name") or "multiqc_report"
        if mdir and isinstance(rname, str):
            candidate = os.path.join(str(mdir), rname + ".html")
            if os.path.isfile(candidate):
                return candidate
        pattern = os.path.join(self.project, "results", "report",
                               "multiqc_report*.html")
        candidates = [p for p in glob.glob(pattern) if os.path.isfile(p)]
        if candidates:
            return max(candidates, key=lambda p: os.path.getmtime(p))
        return None

    def _locate_report_locked(self):
        if self.parser is not None and self.parser.last_report:
            candidate = self.parser.last_report
            if os.path.isfile(candidate):
                return candidate
        return self._locate_report_static()

    # -- progress ---------------------------------------------------------
    def _refresh_locked(self):
        run = self.run
        if run is None or self.parser is None:
            return
        log = run.get("log")
        if not log:
            return
        try:
            size = os.path.getsize(log)
        except OSError:
            size = 0
        if size < self._log_pos:      # should not happen: run.log is append-only
            self._log_pos = 0
            self.parser = LogParser()
            # A fresh run must not inherit the previous run's completion lines.
            # Start at the current end of .nextflow.log; when nextflow rotates it
            # (inode changes, size drops) the tailer resets to 0 and reads the new
            # file from its beginning.
            engine_log = os.path.join(self.project, ".nextflow.log")
            try:
                self._engine_pos = os.path.getsize(engine_log)
                self._engine_ino = os.stat(engine_log).st_ino
            except OSError:
                self._engine_pos = 0
                self._engine_ino = None
        if size > self._log_pos:
            try:
                with open(log, "rb") as fh:
                    fh.seek(self._log_pos)
                    data = fh.read(size - self._log_pos)
            except OSError:
                data = b""
            if data:
                self.parser.feed(data)
                self._log_pos += len(data)
        self._refresh_engine_log_locked()
        run["log_bytes"] = max(size, self._log_pos)
        self._apply_progress_locked()

    def _apply_progress_locked(self):
        run = self.run
        if run is None or self.parser is None:
            return
        samples = run.get("samples") or []
        counts = self.parser.task_counts()
        counts["expected"] = BASE_PER_SAMPLE * len(samples) + self._refs + 2
        run["tasks"] = counts
        run["steps"] = self.parser.steps(samples, self._refs)

    def _persist_locked(self, force=True):
        if not force and time.time() - self._last_persist < 2.0:
            return
        self._last_persist = time.time()
        if self.run is None:
            return
        try:
            atomic_write_json(self.state_file, {"run": self.run})
        except OSError:
            return
        run_dir = os.path.join(self.runs_dir, str(self.run.get("id")))
        meta = os.path.join(run_dir, "meta.json")
        try:
            if os.path.isdir(run_dir):
                atomic_write_json(meta, self.run)
        except OSError:
            pass

    # -- start ------------------------------------------------------------
    def start(self, samples, mode, clean, profile, report_tag):
        """Create the run, run the optional clean, spawn the engine.

        Returns the new run record.  Raises :class:`BusyError`,
        :class:`NextflowMissingError`.
        """
        with self.lock:
            if self.run and self.run.get("state") == "running":
                raise BusyError("A run is already in progress", copy.deepcopy(self.run))
            details = self.external_nextflow_details()
            if details:
                shown = "; ".join(
                    "pid %d (%s)" % (pid, (cmd or "")[:80])
                    for pid, cmd in details[:3])
                raise BusyError(
                    "Detected an external nextflow process: %s; running concurrently would contend for work/ and results/"
                    % shown,
                    copy.deepcopy(self.run))
            if not (self.nextflow and os.path.isfile(self.nextflow)
                    and os.access(self.nextflow, os.X_OK)):
                raise NextflowMissingError(
                    "nextflow executable not found: %s; specify one with --nextflow"
                    % (self.nextflow or "nextflow"))

            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
            run_dir = os.path.join(self.runs_dir, run_id)
            os.makedirs(run_dir, mode=0o700, exist_ok=True)
            sheet_path = os.path.join(run_dir, "samples.tsv")
            sheet.write_sheet(sheet_path, samples)
            log_path = os.path.join(run_dir, "run.log")
            names = []
            refs = 0
            for item in samples:
                name = item.get("name")
                if name is None:
                    name = item.get("sample")
                names.append(name or "")
                if (item.get("reference") or "").strip():
                    refs += 1
            argv = self._build_argv(sheet_path, mode, profile, report_tag)
            with open(log_path, "ab") as fh:
                fh.write(("=== run %s: %s ===\n" % (run_id, " ".join(argv)))
                         .encode("utf-8"))
            run = {
                "id": run_id,
                "state": "running",
                "mode": mode,
                "clean_requested": bool(clean),
                "pid": None,
                "since_pid_alive": True,
                "started_at": time.time(),
                "finished_at": None,
                "elapsed_seconds": 0.0,
                "exit_code": None,
                "cancel_requested": False,
                "sheet": sheet_path,
                "log": log_path,
                "log_bytes": 0,
                "samples": names,
                "argv": argv,
                "tasks": {"submitted": 0, "completed": 0, "failed": 0,
                          "expected": BASE_PER_SAMPLE * len(names) + refs + 2},
                "steps": [],
                "failed": None,
                "report": None,
            }
            self.run = run
            self.parser = LogParser()
            self._log_pos = 0
            self._refs = refs
            self._attached = False
            self.proc = None
            self._apply_progress_locked()
            self._persist_locked()

        try:
            if clean and not self._run_clean():
                return self.run
            proc = self._spawn(argv)
        except OSError as exc:
            self._finalize_failed("spawn_failed",
                                  "Could not start nextflow: %s" % exc)
            return self.run
        with self.lock:
            run = self.run
            run["pid"] = proc.pid
            run["since_pid_alive"] = True
            self.proc = proc
            self._persist_locked()
        self._start_watcher()
        return self.run

    def _build_argv(self, sheet_path, mode, profile, report_tag):
        argv = [self.nextflow, "run", ".", "--samples_tsv", sheet_path]
        if report_tag:
            argv.extend(["--report_tag", report_tag])
        if profile == "lowmem":
            argv.extend(["-profile", "lowmem"])
        if mode == "resume":
            argv.append("-resume")
        argv.extend(self.extra_args)
        return argv

    def _append_log(self, text):
        log = self.run.get("log") if self.run else None
        if not log:
            return
        try:
            with open(log, "ab") as fh:
                fh.write(text.encode("utf-8", "replace"))
        except OSError:
            pass

    def _run_clean(self):
        """``bin/pipeline-clean outputs --yes`` before spawn (section 5)."""
        clean_bin = os.path.join(self.project, "bin", "pipeline-clean")
        self._append_log("=== pipeline-clean outputs --yes ===\n")
        if not (os.path.isfile(clean_bin) and os.access(clean_bin, os.X_OK)):
            self._finalize_failed(
                "clean_failed",
                "Pre-run cleanup failed: no executable bin/pipeline-clean found")
            return False
        log = self.run.get("log")
        try:
            with open(log, "ab") as fh:
                proc = subprocess.run(
                    [clean_bin, "outputs", "--yes"],
                    cwd=self.project, stdin=subprocess.DEVNULL,
                    stdout=fh, stderr=subprocess.STDOUT, timeout=1800,
                )
        except subprocess.TimeoutExpired:
            self._finalize_failed("clean_failed",
                                  "Pre-run cleanup timed out (30 minutes); nextflow was not started")
            return False
        except OSError as exc:
            self._finalize_failed("clean_failed",
                                  "Pre-run cleanup could not be executed: %s" % exc)
            return False
        if proc.returncode != 0:
            self._finalize_failed(
                "clean_failed",
                "Pre-run cleanup failed: bin/pipeline-clean outputs --yes "
                "exited with code %d; nextflow was not started" % proc.returncode,
                exit_code=proc.returncode)
            return False
        return True

    def _spawn(self, argv):
        log = self.run.get("log")
        env = dict(os.environ)
        nf_dir = os.path.dirname(self.nextflow) if self.nextflow else ""
        if nf_dir:
            env["PATH"] = nf_dir + os.pathsep + env.get("PATH", "")
        log_fh = open(log, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=self.project,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
                close_fds=True,
            )
        finally:
            log_fh.close()
        return proc

    # -- cancel -----------------------------------------------------------
    def request_cancel(self):
        """Cancel the active run (owned by this server or attached to it).

        An attached run was started by a previous UI process with
        ``start_new_session=True``, so ``pgid == pid`` and the restarted server
        can signal the whole group exactly like the owner could.  The only thing
        it does not have is the ``Popen`` handle, hence the pid-based polling in
        :meth:`_cancel_process`.
        """
        with self.lock:
            run = self.run
            if run is None or run.get("state") != "running":
                raise NoActiveRunError("No active run")
            proc = self.proc
            pid = run.get("pid")
            if proc is None:
                # attached run: the pid is the only handle we have
                if not self._pid_alive_and_nextflow(pid):
                    raise NoActiveRunError(
                        "The attached run process is gone; cannot cancel")
            elif not pid:
                raise NoActiveRunError("The run has no usable pid; cannot cancel")
            run["cancel_requested"] = True
            self._persist_locked()
        thread = threading.Thread(
            target=self._cancel_process, args=(proc, pid),
            name="run-cancel-%s" % (self.run or {}).get("id"), daemon=True)
        thread.start()
        return True

    def _cancel_process(self, proc, pid):
        target = proc.pid if proc is not None else pid
        if target:
            try:
                os.killpg(target, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(target, signal.SIGTERM)
                except OSError:
                    pass

        def _alive():
            if proc is not None:
                return proc.poll() is None
            return self._pid_alive_and_nextflow(pid)

        deadline = time.time() + 10.0
        while time.time() < deadline:
            if not _alive():
                break
            time.sleep(0.1)
        else:
            if target:
                try:
                    os.killpg(target, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        os.kill(target, signal.SIGKILL)
                    except OSError:
                        pass
            # give the kernel a moment to tear the group down
            deadline = time.time() + 5.0
            while time.time() < deadline and _alive():
                time.sleep(0.1)
        if proc is not None:
            try:
                proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                pass
        self._finalize(proc.returncode if proc is not None else None)

    # -- watcher / finalisation ------------------------------------------
    def _start_watcher(self):
        self._stop.clear()
        thread = threading.Thread(
            target=self._watch, name="run-watcher-%d" % id(self), daemon=True)
        self._watch_thread = thread
        thread.start()

    def _watch(self):
        while not self._stop.is_set():
            with self.lock:
                run = self.run
                if run is None or run.get("state") != "running":
                    return
                self._refresh_locked()
                proc = self.proc
                if proc is not None:
                    returncode = proc.poll()
                    alive = returncode is None
                else:
                    returncode = None
                    alive = self._pid_alive_and_nextflow(run.get("pid"))
                self._persist_locked(force=False)
            if proc is not None and not alive:
                self._finalize(returncode)
                return
            if proc is None and not alive:
                self._finalize(None)
                return
            self._stop.wait(0.25)

    def _finalize(self, exit_code):
        """Move ``running`` to a terminal state and persist it."""
        with self.lock:
            run = self.run
            if run is None or run.get("state") != "running":
                return
            self._refresh_locked()
            self.parser.flush()
            report_file = self._locate_report_locked()
            report = self._make_report_dict(report_file) if report_file else None
            run["report"] = report
            run["finished_at"] = time.time()
            run["exit_code"] = exit_code
            run["since_pid_alive"] = False
            run["elapsed_seconds"] = round(
                max(0.0, run["finished_at"] - run["started_at"]), 1)
            if run.get("cancel_requested"):
                run["state"] = "cancelled"
                run["failed"] = None
            elif exit_code is None:
                # Attached run: it outlived the server that spawned it, so no
                # exit code exists.  Contract rule for this case: report exists
                # and no ERROR block -> done, otherwise failed(unknown_exit).
                if report is not None and not self.parser.has_error_block:
                    run["state"] = "done"
                else:
                    run["state"] = "failed"
                    run["failed"] = self._failed_payload(
                        "unknown_exit",
                        "The previous run ended or was killed after the UI exited; no exit code was captured, "
                        "and the report is missing or the log contains an ERROR block; use -resume to continue")
            elif exit_code == 0 and report is not None:
                run["state"] = "done"
            elif exit_code == 0:
                run["state"] = "failed"
                run["failed"] = self._failed_payload(
                    "report_missing",
                    "The pipeline exited with code 0 but no MultiQC report was found")
            else:
                run["state"] = "failed"
                run["failed"] = self._task_failed_payload(exit_code)
            self._persist_locked()
            self.proc = None

    def _finalize_failed(self, reason, message, exit_code=None):
        with self.lock:
            run = self.run
            if run is None or run.get("state") != "running":
                return
            self._refresh_locked()
            run["state"] = "failed"
            run["finished_at"] = time.time()
            run["exit_code"] = exit_code
            run["since_pid_alive"] = False
            run["elapsed_seconds"] = round(
                max(0.0, run["finished_at"] - run["started_at"]), 1)
            payload = self._failed_payload(reason, message)
            payload["exit_code"] = exit_code
            run["failed"] = payload
            run["report"] = None
            self._persist_locked()
            self.proc = None

    def _failed_payload(self, reason, message):
        fail = self.parser.fail if self.parser else {}
        tail = None
        if fail.get("stderr_tail"):
            tail = fail["stderr_tail"]
        elif self.parser is not None and self.parser.has_error_block:
            tail = self._log_tail(40)
        return {
            "process": fail.get("process"),
            "tag": fail.get("tag"),
            "exit_code": fail.get("exit_code"),
            "reason": reason,
            "work_dir": fail.get("work_dir"),
            "stderr_tail": tail,
            "message": message,
        }

    def _task_failed_payload(self, exit_code):
        fail = self.parser.fail if self.parser else {}
        task_exit = fail.get("exit_code")
        if task_exit is None:
            task_exit = exit_code
        process = fail.get("process")
        tag = fail.get("tag")
        if process:
            message = "%s failed on sample %s with exit code %s" % (
                process, tag or "(unknown)", task_exit)
        else:
            message = "The pipeline failed with exit code %s (no failing task could be located in the log)" % exit_code
        payload = self._failed_payload("task_failed", message)
        payload["process"] = process
        payload["tag"] = tag
        payload["exit_code"] = task_exit
        payload["work_dir"] = fail.get("work_dir")
        payload["stderr_tail"] = fail.get("stderr_tail") or self._log_tail(40)
        return payload

    def _log_tail(self, limit):
        log = self.run.get("log") if self.run else None
        if not log:
            return None
        try:
            with open(log, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return None
        lines = [ln for ln in lines if ln.strip()]
        if not lines:
            return None
        return "\n".join(lines[-limit:])

    def _make_report_dict(self, path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return {
            "path": os.path.abspath(path),
            "size": stat.st_size,
            "url": "/api/report",
            "found_at": time.time(),
        }

    # -- pid helpers ------------------------------------------------------
    def _pid_alive_and_nextflow(self, pid):
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        cmd = self._pid_command(int(pid))
        if cmd is None:
            return True
        return looks_like_nextflow(cmd)

    def _pid_command(self, pid):
        try:
            proc = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                return proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        rows = _pgrep_table()
        if rows is None:
            return None       # no way to inspect: caller falls back to kill(0)
        for row_pid, _ppid, command in rows:
            if row_pid == int(pid):
                return command
        return ""             # pid alive but not a nextflow process (pid reuse)

    def external_nextflow_pids(self):
        with self.lock:
            own = {os.getpid()}
            run_pid = (self.run or {}).get("pid")
            if run_pid:
                own.add(int(run_pid))
            if self.proc is not None:
                own.add(self.proc.pid)
        own |= _active_child_pids()
        return external_nextflow_pids_from_rows(_ps_table(), own)

    def external_nextflow_details(self):
        """[(pid, command)] for the first few foreign engines (diagnostics only)."""
        with self.lock:
            own = {os.getpid()}
            run_pid = (self.run or {}).get("pid")
            if run_pid:
                own.add(int(run_pid))
            if self.proc is not None:
                own.add(self.proc.pid)
        own |= _active_child_pids()
        rows = _ps_table()
        found = external_nextflow_pids_from_rows(rows, own)
        by_pid = {pid: cmd for pid, _ppid, cmd in rows}
        return [(pid, by_pid.get(pid, "")) for pid in found]


    # -- shutdown ---------------------------------------------------------
    def shutdown(self, cancel=True):
        """Cancel an active run (owned or attached), then stop the watcher."""
        with self.lock:
            run = self.run
            running = bool(run and run.get("state") == "running")
        if cancel and running:
            try:
                self.request_cancel()
            except NoActiveRunError:
                pass
            deadline = time.time() + 12.0
            while time.time() < deadline:
                with self.lock:
                    if self.run is None or self.run.get("state") != "running":
                        break
                time.sleep(0.1)
            with self.lock:
                still = bool(self.run and self.run.get("state") == "running")
                proc_now = self.proc
                pid_now = (self.run or {}).get("pid")
            if still and pid_now:
                # Last resort: a SIGTERM-ignoring group is killed outright.
                try:
                    os.killpg(int(pid_now), signal.SIGKILL)
                except OSError:
                    try:
                        os.kill(int(pid_now), signal.SIGKILL)
                    except OSError:
                        pass
                self._finalize(proc_now.returncode)
        self._stop.set()

    # -- preflight --------------------------------------------------------
    def preflight(self):
        """Cheap advisory checks (contract section 7.1).  Never authoritative."""
        checks = []
        if self.params.get("source") == "fallback":
            checks.append({
                "id": "config",
                "label": "Nextflow config",
                "status": "warn",
                "detail": "nextflow config -flat unavailable; using built-in default paths",
                "hint": "The report path follows the onComplete line in the log; "
                        "specify an engine with --nextflow and restart",
            })

        # -- Nextflow ------------------------------------------------------
        nf = self.nextflow
        version = None
        if nf and os.path.isfile(nf) and os.access(nf, os.X_OK):
            rc, out = _run_cmd([nf, "-version"], timeout=10)
            version = _parse_version(out)
            if rc == 0 and version:
                checks.append({
                    "id": "nextflow", "label": "Nextflow", "status": "ok",
                    "detail": "%s (%s)" % (version, nf), "hint": "",
                })
            else:
                checks.append({
                    "id": "nextflow", "label": "Nextflow", "status": "fail",
                    "detail": "%s -version failed (exit code %s)" % (nf, rc),
                    "hint": "Reinstall Nextflow >=25.10, or specify the executable with --nextflow",
                })
        else:
            checks.append({
                "id": "nextflow", "label": "Nextflow", "status": "fail",
                "detail": "nextflow executable not found",
                "hint": "Install Nextflow >=25.10, or use --nextflow PATH",
            })

        # -- Java ----------------------------------------------------------
        java = _find_java()
        if java:
            rc, out = _run_cmd([java, "-version"], timeout=10)
            major = _parse_java_major(out)
            if rc == 0 and major is not None:
                if major >= 17:
                    checks.append({
                        "id": "java", "label": "Java 17+", "status": "ok",
                        "detail": "Java %d (%s)" % (major, java), "hint": "",
                    })
                else:
                    checks.append({
                        "id": "java", "label": "Java 17+", "status": "warn",
                        "detail": "Java %d (%s)" % (major, java),
                        "hint": "Nextflow 26 recommends Java 17+",
                    })
            else:
                checks.append({
                    "id": "java", "label": "Java 17+", "status": "fail",
                    "detail": "java -version could not be executed", "hint": "Install JDK 17+",
                })
        else:
            checks.append({
                "id": "java", "label": "Java 17+", "status": "fail",
                "detail": "java not found", "hint": "Install JDK 17+ (or conda install openjdk)",
            })

        # -- conda / mamba ------------------------------------------------
        conda_name, conda_path = _find_conda()
        if conda_path:
            checks.append({
                "id": "conda", "label": "conda / mamba", "status": "ok",
                "detail": "%s (%s)" % (conda_path, conda_name), "hint": "",
            })
        else:
            checks.append({
                "id": "conda", "label": "conda / mamba", "status": "fail",
                "detail": "mamba/conda not found",
                "hint": "Install Miniforge, or put mamba/conda on PATH",
            })

        # -- Bakta db-light sentinel --------------------------------------
        bakta_db = self.params.get("bakta_db") or os.path.join(
            self.project, "data", "db", "bakta", "db-light")
        marker = os.path.join(str(bakta_db), "version.json")
        if os.path.isfile(marker):
            checks.append({
                "id": "bakta_db", "label": "Bakta db-light", "status": "ok",
                "detail": "version.json present", "hint": "",
            })
        else:
            checks.append({
                "id": "bakta_db", "label": "Bakta db-light", "status": "warn",
                "detail": "version.json missing: %s" % marker,
                "hint": "The first run downloads db-light (about 4 GiB, network required)",
            })

        # -- BUSCO lineage ------------------------------------------------
        # The pipeline passes `--download_path <busco_db>`, and BUSCO 5 stores a
        # downloaded lineage under `<download_path>/lineages/<lineage>` (that is
        # where this checkout has it: data/db/busco/lineages/enterobacterales_odb10).
        # A preflight that only checks `<busco_db>/<lineage>` stays permanently
        # yellow on a correctly provisioned machine -- and a warning that is always
        # on is a warning nobody reads. Accept both layouts, but require the marker
        # files so an empty directory is not mistaken for a lineage.
        busco_db = self.params.get("busco_db") or os.path.join(
            self.project, "data", "db", "busco")
        lineage = str(self.params.get("busco_lineage") or
                      "enterobacterales_odb10")
        lineage_candidates = [
            os.path.join(str(busco_db), "lineages", lineage),
            os.path.join(str(busco_db), lineage),
        ]
        lineage_dir = None
        for candidate in lineage_candidates:
            if not os.path.isdir(candidate):
                continue
            if any(os.path.exists(os.path.join(candidate, marker))
                   for marker in ("hmms", "info", "dataset.cfg")):
                lineage_dir = candidate
                break
        if lineage_dir:
            checks.append({
                "id": "busco_db", "label": "BUSCO lineage", "status": "ok",
                "detail": "%s (%s)" % (lineage, lineage_dir), "hint": "",
            })
        else:
            checks.append({
                "id": "busco_db", "label": "BUSCO lineage", "status": "warn",
                "detail": "%s not downloaded (%s)" % (
                    lineage, " or ".join(lineage_candidates)),
                "hint": "The first run downloads the lineage (network required)",
            })

        # -- built conda environments ------------------------------------
        conda_dir = os.path.join(self.project, ".nextflow", "conda")
        envs = count_conda_envs(conda_dir)
        if envs >= 8:
            checks.append({
                "id": "conda_envs", "label": "conda environments", "status": "ok",
                "detail": "%d/8 built" % envs, "hint": "",
            })
        else:
            checks.append({
                "id": "conda_envs", "label": "conda environments", "status": "warn",
                "detail": "%d/8 built" % envs,
                "hint": "The first run builds 8 environments (about 5.7 GiB, network required) and may take far longer than 45 minutes",
            })

        # -- disk ---------------------------------------------------------
        required = _as_float(self.params.get("min_free_disk_gb"), 20.0)
        free_gb, usage_path = _free_disk_gb(self.project)
        if required <= 0 or free_gb >= required:
            checks.append({
                "id": "disk", "label": "Disk", "status": "ok",
                "detail": "%.1f GiB free (requires >=%g)" % (free_gb, required),
                "hint": "",
            })
        else:
            checks.append({
                "id": "disk", "label": "Disk", "status": "fail",
                "detail": "%.1f GiB free (< %g GiB required)" % (free_gb, required),
                "hint": "Free space first: bin/pipeline-clean outputs --yes, "
                        "or bin/pipeline-clean all --yes; "
                        "you can also lower min_free_disk_gb",
            })
        disk = {"free_gb": round(free_gb, 1), "required_gb": required,
                "path": usage_path}

        # -- external nextflow -------------------------------------------
        external = self.external_nextflow_pids()
        if external:
            checks.append({
                "id": "external_nextflow", "label": "External nextflow processes",
                "status": "warn",
                "detail": "%d found: pid %s" % (
                    len(external), ", ".join(str(p) for p in external)),
                "hint": "Running concurrently would contend for work/ and results/; starting a run will be refused",
            })
        else:
            checks.append({
                "id": "external_nextflow", "label": "External nextflow processes",
                "status": "ok", "detail": "None", "hint": "",
            })

        failures = sum(1 for c in checks if c["status"] == "fail")
        warnings = sum(1 for c in checks if c["status"] == "warn")
        results_root = self.results_root()
        with self.lock:
            run_report = ((self.run or {}).get("report") or {}).get("path")
        report_path = None
        if run_report and os.path.isfile(run_report):
            report_path = run_report
        else:
            report_path = self._locate_report_static()
        run = self.snapshot()
        return {
            "ok": failures == 0,
            "checks": checks,
            "summary": {"failures": failures, "warnings": warnings},
            "disk": disk,
            "config": {
                "source": self.params.get("source", "fallback"),
                "multiqc_dir": self.params.get("multiqc_dir"),
                "report_name": self.params.get("report_name"),
                "min_free_disk_gb": self.params.get("min_free_disk_gb"),
                "raw_dir": self.params.get("raw_dir"),
                "ref_dir": self.params.get("ref_dir"),
            },
            "existing": {
                "results_bytes": dir_bytes(results_root),
                "report": (
                    self._make_report_dict(report_path)
                    if report_path and os.path.isfile(report_path) else None),
                "conda_envs": envs,
                "work_bytes": dir_bytes(os.path.join(self.project, "work")),
                "clean_plan": self._clean_plan(),
            },
            "active_run": run if run and run.get("state") == "running" else None,
        }

    def _clean_plan(self):
        """`pipeline-clean check` (deletes nothing) for the confirmation dialog."""
        clean_bin = os.path.join(self.project, "bin", "pipeline-clean")
        if not (os.path.isfile(clean_bin) and os.access(clean_bin, os.X_OK)):
            return {"level": "outputs", "text": "bin/pipeline-clean not found"}
        rc, out = _run_cmd([clean_bin, "check"], timeout=15, cwd=self.project)
        text = out.strip()
        if len(text) > 12000:
            text = text[:12000] + "\n...(truncated)"
        if rc != 0:
            text = ("pipeline-clean check exited with code %d\n%s" % (rc, text)).strip()
        return {"level": "outputs", "text": text}


# ---------------------------------------------------------------------------
# Small helpers shared by preflight
# ---------------------------------------------------------------------------

def _run_cmd(argv, timeout=10, cwd=None):
    """Run a short helper command and keep it in the child registry while alive.

    ``subprocess.run`` would hide the pid; the registry lets the busy detector
    exclude our own ``nextflow -version`` JVM even where ``ps`` cannot report
    parent/child relationships (F50).
    """
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )
    except OSError as exc:
        return 127, str(exc)
    _register_child(proc.pid)
    try:
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return 124, ""
        return proc.returncode, out or ""
    finally:
        _unregister_child(proc.pid)


def _parse_version(text):
    m = re.search(r"version\s+(\S+)", text or "")
    return m.group(1) if m else None


def _parse_java_major(text):
    m = re.search(r'version\s+"?(\d+)', text or "")
    return int(m.group(1)) if m else None


def _find_java():
    java = shutil.which("java")
    if java:
        return java
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = os.path.join(java_home, "bin", "java")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _find_conda():
    for name in ("mamba", "conda"):
        path = shutil.which(name)
        if path:
            return name, path
    for base in (
        os.path.expanduser("~/miniforge3/bin"),
        os.path.expanduser("~/mambaforge/bin"),
        os.path.expanduser("~/miniconda3/bin"),
        os.path.expanduser("~/anaconda3/bin"),
    ):
        for name in ("mamba", "conda"):
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return name, candidate
    return None, None


def _free_disk_gb(path):
    try:
        usage = shutil.disk_usage(path)
        return usage.free / 1073741824.0, path
    except OSError:
        return 0.0, path


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def resolve_nextflow(explicit):
    """Resolve the engine: explicit -> PATH -> the known conda location.

    A missing engine does not block startup (preflight reports it), so an
    explicit path is returned even when it does not exist -- run will refuse
    with ``nextflow_missing`` instead of spawning something random.
    """
    if explicit:
        expanded = os.path.expanduser(explicit)
        if os.sep in expanded:
            return os.path.abspath(expanded)
        found = shutil.which(expanded)
        if found:
            return os.path.abspath(found)
        return os.path.abspath(expanded)
    found = shutil.which("nextflow")
    if found:
        return os.path.abspath(found)
    for candidate in (
        os.path.expanduser("~/miniforge3/envs/nextflow/bin/nextflow"),
        os.path.expanduser("~/mambaforge/envs/nextflow/bin/nextflow"),
        os.path.expanduser("~/miniconda3/envs/nextflow/bin/nextflow"),
        os.path.expanduser("~/miniforge3/bin/nextflow"),
        "/usr/local/bin/nextflow",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.abspath(candidate)
    return ""
