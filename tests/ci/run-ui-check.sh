#!/usr/bin/env bash
# ============================================================
# docs/ui.md §13 -- the UI contract, asserted.
#
# What is tested: the local Web UI (bin/pipeline-ui + ui/server.py) as a
# *process driver and reporter*, not the bioinformatics. The engine is faked at
# the process boundary (tests/ci/fixtures/ui/fake-nextflow): the UI cannot tell
# it from nextflow, and every interesting behaviour -- a run that fails, one
# that exits 0 without a report, one that hangs long enough to cancel, one that
# survives the UI dying -- can be produced on demand in a couple of seconds.
#
# Real integration (last section): when a real `nextflow` is available, the
# complete stub DAG is driven through the UI inside a THROWAWAY COPY of the
# project (main.nf + nextflow.config + scripts/envs/bin/media + tests/ci), so
# the repository's validated results/ tree is never written (F18). Without
# nextflow that section prints SKIP -- it must run in CI.
#
# No third-party dependency: bash + curl + python3 (stdlib only). Every
# assertion is a PASS/FAIL line (tests/ci/lib.sh) and `summary` exits non-zero
# if any failed.
#
# Debugging: UI_CHECK_KEEP=1 keeps the temp workspace and prints its path.
# Optional real failure injection: UI_CHECK_REAL_FAILURE=0 skips it.
#
# Local usage:  bash tests/ci/run-ui-check.sh
# ============================================================

# shellcheck source=tests/ci/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

UI_BIN="${PROJECT_DIR}/bin/pipeline-ui"
SERVER_PY="${PROJECT_DIR}/ui/server.py"
UI_INDEX="${PROJECT_DIR}/ui/index.html"
UI_APPJS="${PROJECT_DIR}/ui/app.js"
UI_STYLE="${PROJECT_DIR}/ui/style.css"
FAKE="${CI_DIR}/fixtures/ui/fake-nextflow"
REPO_RESULTS="${PROJECT_DIR}/results"
REPO_DATA="${PROJECT_DIR}/data"
REPO_SAMPLES="${PROJECT_DIR}/samples.tsv"

START_SECONDS=${SECONDS}
SKIPS=0
skip() { printf '  SKIP %s\n' "$1"; SKIPS=$((SKIPS + 1)); }
banner() { printf '\n== %s ==\n' "$1"; }

RESULTS_MTIME_NS=""
REPO_SAMPLES_SHA=""
F18_MARKER=""

WORK="$(mktemp -d "${TMPDIR:-/tmp}/wgs-ui-check.XXXXXX")"
WORK="$(cd "${WORK}" && pwd -P)"
LOG_DIR="${WORK}/logs"
mkdir -p "${LOG_DIR}"
CTRL="${WORK}/fake-ctrl.json"

# Launcher/server bookkeeping. start_ui/stop_ui own these; cleanup is the
# backstop so a failed assertion can never leave a server or a fake engine
# behind (that would poison the next scenario and the developer's machine).
UI_PIDS=""
OUR_SERVERS=""
CUR_LAUNCHER=""
CUR_SERVER=""
CUR_LOG=""
BASE=""
TOKEN=""
PORT=""
ORIGIN_OK=""

cleanup() {
    local status=$? pid
    set +e
    for pid in ${UI_PIDS:-}; do kill -TERM "${pid}" 2>/dev/null; done
    sleep 1
    for pid in ${UI_PIDS:-}; do kill -KILL "${pid}" 2>/dev/null; done
    # Anything still referencing the temp workspace is ours: the python server,
    # a fake engine, or the launcher. The workspace path is unique per run, so
    # this cannot touch another checkout or a developer's own UI.
    if command -v pkill >/dev/null 2>&1; then
        pkill -TERM -f -- "${WORK}" 2>/dev/null
        sleep 0.3
        pkill -KILL -f -- "${WORK}" 2>/dev/null
    fi
    if [ "${FAILURES:-0}" -ne 0 ]; then
        echo
        echo "== diagnostics: UI launcher/server logs =="
        for pid in ${WORK}/logs/*.log; do
            [ -f "${pid}" ] || continue
            echo "---- ${pid} (tail) ----"
            tail -40 "${pid}" | sed 's/^/    /'
        done
    fi
    if [ "${UI_CHECK_KEEP:-0}" = "1" ]; then
        echo "  (kept temp workspace: ${WORK})"
    else
        rm -rf "${WORK}"
    fi
    exit "${status}"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

# json_get <file> <dotted.path> -- prints the value; empty string for missing or
# null, JSON text for objects/arrays. Never aborts on a malformed body, so a
# broken response shows up as a failed assertion instead of killing the script.
json_get() {
    python3 - "$1" "$2" <<'PY'
import json, sys
path, expr = sys.argv[1], sys.argv[2]
try:
    with open(path, "r", encoding="utf-8") as fh:
        node = json.load(fh)
except Exception:
    print("")
    sys.exit(0)
for key in expr.split("."):
    if key == "":
        continue
    if isinstance(node, dict):
        node = node.get(key)
    elif isinstance(node, list):
        try:
            node = node[int(key)]
        except Exception:
            node = None
    else:
        node = None
    if node is None:
        break
if node is None:
    print("")
elif isinstance(node, bool):
    print("true" if node else "false")
elif isinstance(node, (dict, list)):
    print(json.dumps(node, ensure_ascii=False))
else:
    print(node)
PY
}

sha256_of() {
    python3 - "$1" <<'PY'
import hashlib, sys
try:
    with open(sys.argv[1], "rb") as fh:
        print(hashlib.sha256(fh.read()).hexdigest())
except OSError:
    print("")
PY
}

# JSON of the `UI_READY {...}` line: prints base URL, token, port on 3 lines.
# Tolerant by design: the contract fixes the line prefix, not the JSON shape,
# so the URL is authoritative and JSON fields are only used if present.
ui_ready_parse() {
    python3 - "$1" <<'PY'
import json, re, sys
raw = sys.argv[1]
url = token = ""
try:
    d = json.loads(raw)
    if isinstance(d, dict):
        url = str(d.get("url") or "")
        token = str(d.get("token") or "")
except Exception:
    d = {}
if not url:
    m = re.search(r"https?://[^\s\"']+", raw)
    url = m.group(0) if m else ""
if not token:
    m = re.search(r"[?&]token=([^&\s\"']+)", raw)
    token = m.group(1) if m else ""
m = re.match(r"(https?://[^/?#]+)", url)
base = m.group(1) if m else ""
port = ""
m = re.search(r":([0-9]+)$", base)
if m:
    port = m.group(1)
print(base)
print(token)
print(port)
PY
}

# mk_body <outfile> <mode> <clean 0|1> <report_tag> <name fq1 fq2 species strain ref>...
mk_body() {
    local out="$1" mode="$2" clean="$3" tag="$4"
    shift 4
    python3 - "$out" "$mode" "$clean" "$tag" "$@" <<'PY'
import json, sys
out, mode, clean, tag = sys.argv[1:5]
rest = sys.argv[5:]
samples = []
for i in range(0, len(rest), 6):
    name, fq1, fq2, species, strain, ref = rest[i:i + 6]
    samples.append({"name": name, "fq1": fq1, "fq2": fq2,
                    "species": species, "strain": strain, "reference": ref})
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"samples": samples, "mode": mode, "clean": clean == "1",
               "profile": "standard", "report_tag": tag}, fh, ensure_ascii=False, indent=1)
    fh.write("\n")
PY
}

# The sheet is a byte-exact contract, so the expected file is generated from the
# same JSON request the UI received: any escaping/column/order mistake shows up
# as a cmp failure, not as a fuzzy grep.
expected_sheet() {
    python3 - "$1" "$2" <<'PY'
import json, sys
body = json.load(open(sys.argv[2], "r", encoding="utf-8"))
cols = ["sample", "fq1", "fq2", "species", "strain", "reference"]
rows = ["\t".join(cols)]
for s in body["samples"]:
    rows.append("\t".join([s["name"], s["fq1"], s["fq2"],
                           s.get("species") or "", s.get("strain") or "",
                           s.get("reference") or ""]))
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(rows) + "\n")
PY
}

# Layout rules from §6 that byte equality alone does not name: absolute paths,
# 6 tab-separated columns on every data row, LF endings, final newline.
sheet_layout_ok() {
    python3 - "$1" <<'PY'
import sys
path = sys.argv[1]
try:
    raw = open(path, "rb").read()
except OSError as exc:
    print("cannot read sheet: %s" % exc)
    sys.exit(1)
problems = []
if not raw.endswith(b"\n"):
    problems.append("no final newline")
if b"\r" in raw:
    problems.append("CR found (must be LF only)")
text = raw.decode("utf-8", "replace")
lines = text.split("\n")
if lines and lines[-1] == "":
    lines = lines[:-1]
if not lines:
    problems.append("empty sheet")
else:
    if lines[0] != "sample\tfq1\tfq2\tspecies\tstrain\treference":
        problems.append("header is %r" % lines[0])
    for i, line in enumerate(lines[1:], start=2):
        cols = line.split("\t")
        if len(cols) != 6:
            problems.append("row %d has %d columns, not 6" % (i, len(cols)))
            continue
        for field, value in zip(("fq1", "fq2", "reference"), (cols[1], cols[2], cols[5])):
            if value and not value.startswith("/"):
                problems.append("row %d %s is not absolute: %r" % (i, field, value))
        if cols[0].strip() == "":
            problems.append("row %d has an empty sample" % i)
if problems:
    print("; ".join(problems))
    sys.exit(1)
PY
}

# ctrl-file status id (preflight) -> prints status or empty.
preflight_status() {
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    print("")
    sys.exit(0)
for check in data.get("checks") or []:
    if isinstance(check, dict) and check.get("id") == sys.argv[2]:
        print(check.get("status") or "")
        break
else:
    print("")
PY
}

# SSE capture -> <prefix>.states (one state per line), <prefix>.logtext (the
# concatenated `log` event text), <prefix>.meta (event_count=, first_from=).
sse_summary() {
    python3 - "$1" "$2" <<'PY'
import json, sys
raw = open(sys.argv[1], "r", encoding="utf-8", errors="replace").read()
states, texts, first_from, events = [], [], None, 0
ev = None
for line in raw.splitlines():
    if line.startswith("event:"):
        ev = line.split(":", 1)[1].strip()
    elif line.startswith("data:"):
        payload = line.split(":", 1)[1].strip()
        try:
            data = json.loads(payload)
        except Exception:
            continue
        if ev == "state":
            run = (data or {}).get("run") or {}
            states.append(str(run.get("state")))
        elif ev == "log":
            events += 1
            if first_from is None:
                first_from = data.get("from")
            texts.append(data.get("text") or "")
    elif line.strip() == "":
        ev = None
with open(sys.argv[2] + ".states", "w", encoding="utf-8") as fh:
    fh.write("\n".join(states) + ("\n" if states else ""))
with open(sys.argv[2] + ".logtext", "w", encoding="utf-8") as fh:
    fh.write("".join(texts))
with open(sys.argv[2] + ".meta", "w", encoding="utf-8") as fh:
    fh.write("event_count=%d\nfirst_from=%s\n" % (events, "" if first_from is None else first_from))
PY
}

meta_value() { sed -n "s/^$2=//p" "$1" | head -1; }

# Compare a downloads tarball against the tree the fake engine actually wrote.
# The expected set comes from the control file the test itself wrote, so this is
# a real equality check, not a smoke test of "some files are in there".
tar_matches() {
    python3 - "$1" "$2" "$3" <<'PY'
import json, os, sys, tarfile
tar_path, ctrl_path, report_path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    ctrl = json.load(open(ctrl_path, "r", encoding="utf-8"))
except Exception:
    ctrl = {}
exp = set()
for rel in (ctrl.get("results_files") or {}):
    rel = rel.replace("\\", "/").lstrip("./")
    if rel.startswith("results/"):
        rel = rel[len("results/"):]
    exp.add(rel)
report = os.path.realpath(report_path)
root = os.path.dirname(os.path.dirname(report))
exp.add(os.path.relpath(report, root).replace(os.sep, "/"))
try:
    with tarfile.open(tar_path, "r:gz") as tf:
        members = [m.name for m in tf.getmembers() if m.isfile()]
except Exception as exc:
    print("cannot read tarball: %s" % exc)
    sys.exit(1)
actual = []
for name in members:
    name = name.replace("\\", "/").lstrip("./")
    actual.append(name)
if actual and all(n.startswith("results/") for n in actual):
    actual = [n[len("results/"):] for n in actual]
missing = sorted(exp - set(actual))
extra = sorted(set(actual) - exp)
if missing or extra:
    if missing:
        print("missing: %s" % ", ".join(missing))
    if extra:
        print("extra: %s" % ", ".join(extra))
    sys.exit(1)
PY
}

# Last-run marker for F18: every file must be older than this.
touch_f18_marker() {
    F18_MARKER="${WORK}/f18-marker"
    : > "${F18_MARKER}"
    sleep 1
}

# ---------------------------------------------------------------------------
# launcher / HTTP plumbing
# ---------------------------------------------------------------------------

# start_ui <project> <logfile> [launcher args...]
# Waits for the `UI_READY` line the contract requires under --print-url, then
# for /api/state to answer. Fills BASE/TOKEN/PORT and CUR_*.
start_ui() {
    local project="$1" log="$2"
    shift 2
    if [ -z "${project}" ] || [ ! -d "${project}" ]; then
        echo "  start_ui: refusing to launch with --project '${project}' (must be a real directory)"
        fail "start_ui got an empty/missing project path -- refusing to launch"
        return 1
    fi
    : > "${log}"
    "${UI_BIN}" --project "${project}" --port 0 --no-open --token "${TOKEN_ARG}" \
        --print-url "$@" >>"${log}" 2>&1 &
    local launcher=$!
    UI_PIDS="${UI_PIDS} ${launcher}"
    local i=0 line out
    while [ "${i}" -lt 600 ]; do
        if grep -q '^UI_READY ' "${log}" 2>/dev/null; then
            CUR_LAUNCHER="${launcher}"
            CUR_LOG="${log}"
            line="$(grep '^UI_READY ' "${log}" | tail -1)"
            line="${line#UI_READY }"
            out="$(ui_ready_parse "${line}")"
            BASE="$(printf '%s\n' "${out}" | sed -n '1p')"
            TOKEN="$(printf '%s\n' "${out}" | sed -n '2p')"
            PORT="$(printf '%s\n' "${out}" | sed -n '3p')"
            [ -n "${TOKEN}" ] || TOKEN="${TOKEN_ARG}"
            ORIGIN_OK="http://127.0.0.1:${PORT}"
            CUR_SERVER="$(json_get "${project}/.nextflow/ui/server.json" pid)"
            OUR_SERVERS="${OUR_SERVERS} ${launcher} ${CUR_SERVER}"
            if [ ! -f "${project}/.nextflow/ui/server.json" ]; then
                echo "  start_ui: launcher did not write ${project}/.nextflow/ui/server.json"
                fail "launcher wrote server.json outside the requested --project"
                return 1
            fi
            return 0
        fi
        if ! kill -0 "${launcher}" 2>/dev/null; then
            echo "  launcher exited before printing UI_READY:" | sed 's/^/  /'
            tail -20 "${log}" | sed 's/^/    /'
            return 1
        fi
        sleep 0.1
        i=$((i + 1))
    done
    echo "  timed out waiting for UI_READY (60 s)"; tail -20 "${log}" | sed 's/^/    /'
    return 1
}

stop_ui() {
    local launcher="${CUR_LAUNCHER:-}" server="${CUR_SERVER:-}" i=0
    [ -n "${launcher}" ] || return 0
    kill -TERM "${launcher}" 2>/dev/null
    while [ "${i}" -lt 150 ] && kill -0 "${launcher}" 2>/dev/null; do
        sleep 0.1
        i=$((i + 1))
    done
    kill -KILL "${launcher}" 2>/dev/null
    if [ -n "${server}" ] && [ "${server}" != "${launcher}" ]; then
        kill -TERM "${server}" 2>/dev/null
        sleep 0.2
        kill -KILL "${server}" 2>/dev/null
    fi
    CUR_LAUNCHER=""
    CUR_SERVER=""
    CUR_LOG=""
    BASE=""
    sleep 0.2
}

api_url() { printf '%s/api/%s' "${BASE}" "$1"; }

# get_api <path> <outfile> [extra curl args] -> http code
get_api() {
    local path="$1" out="$2"
    shift 2
    curl -sS --max-time 30 -H "X-UI-Token: ${TOKEN}" "$@" -o "${out}" -w '%{http_code}' "${BASE}${path}"
}

get_api_hdr() {
    local path="$1" out="$2" hdr="$3"
    shift 3
    curl -sS --max-time 30 -D "${hdr}" -H "X-UI-Token: ${TOKEN}" "$@" -o "${out}" -w '%{http_code}' "${BASE}${path}"
}

# post_api <path> <bodyfile> <outfile> -> http code (JSON content type + token)
post_api() {
    local path="$1" body="$2" out="$3"
    curl -sS --max-time 300 -H "Content-Type: application/json" -H "X-UI-Token: ${TOKEN}" \
        -d @"${body}" -o "${out}" -w '%{http_code}' "${BASE}${path}"
}

post_api_hdr() {
    local path="$1" body="$2" out="$3" hdr="$4"
    curl -sS --max-time 300 -D "${hdr}" -H "Content-Type: application/json" -H "X-UI-Token: ${TOKEN}" \
        -d @"${body}" -o "${out}" -w '%{http_code}' "${BASE}${path}"
}

# POST /api/run with the §7.2 busy case tolerated.
#
# "busy" is the *correct* answer when an unrelated nextflow-like process exists
# on the machine: another agent's fake engine looks exactly like the real thing
# at the process boundary. In CI there is one job and no such process; locally
# (parallel agents) the scenario must SKIP rather than fail, and must never wait
# for a state that will never become running.
post_run_retrying_busy() { # <bodyfile> <outfile> -> http code
    local body="$1" out="$2" code i=0
    code="$(post_api "/api/run" "${body}" "${out}")"
    while [ "${code}" = "409" ] && [ "$(json_get "${out}" error)" = "busy" ] && [ "${i}" -lt 40 ]; do
        sleep 0.5
        code="$(post_api "/api/run" "${body}" "${out}")"
        i=$((i + 1))
    done
    printf '%s' "${code}"
}

# run_accepted_or_skip <label> <code> <responsefile> -- pass on 202; SKIP on
# busy (the caller returns); FAIL on any other status.
run_accepted_or_skip() {
    local label="$1" code="$2" out="$3"
    if [ "${code}" = "202" ]; then
        pass "${label}: POST /api/run -> 202"
        return 0
    fi
    if [ "${code}" = "409" ] && [ "$(json_get "${out}" error)" = "busy" ]; then
        skip "${label}: POST /api/run -> 409 busy (§7.2: an unrelated nextflow-like process is running; CI runs this check alone)"
        return 1
    fi
    fail "${label}: POST /api/run -> HTTP ${code}, expected 202 ($(head -c 200 "${out}" 2>/dev/null))"
    return 1
}

get_state() { get_api "/api/state" "${WORK}/state.json" >/dev/null; }
state_field() { json_get "${WORK}/state.json" "$1"; }

# step_field <process> <field> -- one cell of the 12-stage ladder from the last
# /api/state snapshot (state.json is refreshed by get_state).
step_field() {
    python3 - "${WORK}/state.json" "$1" "$2" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    print("")
    sys.exit(0)
run = data.get("run") or {}
for step in run.get("steps") or []:
    if step.get("process") == sys.argv[2]:
        value = step.get(sys.argv[3])
        print("" if value is None else value)
        break
else:
    print("")
PY
}

# step_by_sample <process> <sample> -- the by_sample cell of the ladder.
step_by_sample() {
    python3 - "${WORK}/state.json" "$1" "$2" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    print("")
    sys.exit(0)
run = data.get("run") or {}
for step in run.get("steps") or []:
    if step.get("process") == sys.argv[2]:
        print((step.get("by_sample") or {}).get(sys.argv[3], ""))
        break
else:
    print("")
PY
}

wait_api_ready() {
    local path="$1" timeout="${2:-25}" i=0 code
    while [ "${i}" -lt $((timeout * 10)) ]; do
        code="$(get_api "${path}" "${WORK}/wait.json" 2>/dev/null)"
        [ "${code}" = "200" ] && return 0
        sleep 0.1
        i=$((i + 1))
    done
    return 1
}

# wait_run_state_t <seconds> <expected...> -- poll /api/state until run.state
# is one of them (the fake engine makes every transition a second-scale event).
wait_run_state_t() {
    local timeout="$1"
    shift
    local want=" $* " i=0 s
    while [ "${i}" -lt $((timeout * 10)) ]; do
        get_state
        s="$(state_field run.state)"
        case "${want}" in
            *" ${s} "*) return 0 ;;
        esac
        sleep 0.1
        i=$((i + 1))
    done
    return 1
}

expect_status() { # <label> <expected> <actual>
    expect_eq "$1" "$2" "$3"
}

expect_no_store() { # <label> <path>
    local label="$1" path="$2" hdr="${WORK}/hdr-cache.txt" code
    code="$(get_api_hdr "${path}" "${WORK}/cache-body.tmp" "${hdr}")"
    if [ "${code}" != "200" ]; then
        fail "${label}: expected 200 before checking Cache-Control, got ${code}"
        return
    fi
    if grep -qiE '^cache-control:[[:space:]]*no-store' "${hdr}"; then
        pass "${label}: Cache-Control: no-store"
    else
        fail "${label}: missing Cache-Control: no-store (got '$(grep -i '^cache-control' "${hdr}" | tr -d '\r' | head -1)')"
    fi
}

header_has() { grep -qiE "$2" "$1"; }

# Value of a response header, CR stripped (curl writes CRLF; a trailing CR is
# what makes an anchored grep fail on a perfectly correct header).
header_value() { grep -i "^$2:" "$1" 2>/dev/null | head -1 | tr -d '\r' | sed "s/^[^:]*:[[:space:]]*//"; }

# Direct children of the UI server: a spawned engine is one, so "no spawn" and
# "cancelled" can be proven without pattern-matching other checkouts' processes
# (a developer's own UI, running from /tmp, uses the same fake-nextflow path).
server_children() { pgrep -P "$1" 2>/dev/null || true; }

# ---------------------------------------------------------------------------
# temp projects
# ---------------------------------------------------------------------------

P_FAKE=""
P_NONF=""
P_REAL=""
P_REAL2=""

# Minimal project for the fake engine: the launcher only requires main.nf and
# nextflow.config to exist; the engine (not Nextflow) answers `config -flat`.
make_fake_project() {
    local dir="$1"
    mkdir -p "${dir}/data/raw" "${dir}/data/reference" "${dir}/bin"
    : > "${dir}/main.nf"
    : > "${dir}/nextflow.config"
    if [ -x "${PROJECT_DIR}/bin/pipeline-clean" ]; then
        cp "${PROJECT_DIR}/bin/pipeline-clean" "${dir}/bin/pipeline-clean"
    fi
    printf '@r1\nACGTACGT\n+\nIIIIIIII\n' | gzip -c > "${dir}/data/raw/a_1.fastq.gz"
    printf '@r1\nACGTACGT\n+\nIIIIIIII\n' | gzip -c > "${dir}/data/raw/a_2.fastq.gz"
    printf '@r1\nTTTTGGGG\n+\nIIIIIIII\n' | gzip -c > "${dir}/data/raw/b_1.fastq.gz"
    printf '@r1\nTTTTGGGG\n+\nIIIIIIII\n' | gzip -c > "${dir}/data/raw/b_2.fastq.gz"
    printf '>ref\nACGTACGTACGTACGT\n' > "${dir}/data/reference/ref.fasta"
}

# A throwaway copy of everything a `-stub-run` needs. The real checkout is only
# read; the CI overlay redirects every publishDir under <copy>/tests/ci/out, so
# the validated results/ tree cannot be touched (F18).
make_real_project() {
    local dir="$1"
    mkdir -p "${dir}/tests/ci"
    cp "${PROJECT_DIR}/main.nf" "${PROJECT_DIR}/nextflow.config" "${dir}/"
    cp -Rp "${PROJECT_DIR}/scripts" "${PROJECT_DIR}/envs" "${PROJECT_DIR}/bin" \
        "${PROJECT_DIR}/media" "${dir}/"
    cp "${PROJECT_DIR}"/multiqc_*.yaml "${dir}/" 2>/dev/null
    cp "${PROJECT_DIR}/tests/ci/ci.config" "${PROJECT_DIR}/tests/ci/inject-sample-failure.config" \
        "${dir}/tests/ci/"
    cp -Rp "${PROJECT_DIR}/tests/ci/fixtures" "${dir}/tests/ci/fixtures"
}

count_run_dirs() {
    local dir="$1/.nextflow/ui/runs"
    [ -d "${dir}" ] || { echo 0; return; }
    find "${dir}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' '
}

# Processes that look like an engine but are not this check's own child: with
# any of them present, §7.2 makes POST /api/run return 409 busy. CI has none;
# a parallel agent (or a developer's real run) does.
external_engine_procs() {
    # Mirror ui/runner.py's looks_like_nextflow() rather than a bare pgrep:
    # a shell whose command line merely mentions "nextflow" (e.g. `rm -rf
    # .nextflow/ui`) is not an engine. ui/server.py / bin/pipeline-ui are
    # excluded there too -- they merely *hold* a nextflow path in their argv.
    pgrep -fl 'nextflow' 2>/dev/null | grep -v -F "${WORK}" | PROJECT_DIR="${PROJECT_DIR}" python3 - <<'PYENGINE' || true
# Use the code under test instead of a copy of the heuristic: a drifted mirror is
# how a "nextflow -version" JVM became a false 409 (F50). The classifier is pure
# and importing ui/runner.py starts nothing.
import os, sys
sys.path.insert(0, os.path.join(os.environ["PROJECT_DIR"], "ui"))
try:
    import runner
except Exception:
    sys.exit(0)
for line in sys.stdin:
    line = line.rstrip()
    if not line:
        continue
    _pid, _, cmd = line.partition(" ")
    if runner.looks_like_nextflow(cmd):
        print(line)
PYENGINE
}

nextflow_bin() {
    if [ -n "${NEXTFLOW:-}" ] && [ -x "${NEXTFLOW}" ]; then
        printf '%s' "${NEXTFLOW}"
        return 0
    fi
    if command -v nextflow >/dev/null 2>&1; then
        command -v nextflow
        return 0
    fi
    if [ -x "${HOME}/miniforge3/envs/nextflow/bin/nextflow" ]; then
        printf '%s' "${HOME}/miniforge3/envs/nextflow/bin/nextflow"
        return 0
    fi
    printf ''
}

# ===========================================================================
# 0. static frontend contract (no server needed)
# ===========================================================================
static_checks() {
    banner "static: offline single page (§11, §13.3)"
    expect_file "ui/index.html exists" "${UI_INDEX}"
    expect_file "ui/app.js exists" "${UI_APPJS}"
    expect_file "ui/style.css exists" "${UI_STYLE}"

    local f name
    for f in "${UI_INDEX}" "${UI_APPJS}" "${UI_STYLE}"; do
        [ -f "${f}" ] || continue
        name="$(basename "${f}")"
        if external_links "${f}" >"${WORK}/external-${name}.txt" 2>&1; then
            pass "${name}: no external http(s) resource links"
        else
            fail "${name}: external link(s) found: $(tr '\n' ' ' < "${WORK}/external-${name}.txt")"
        fi
    done

    if command -v node >/dev/null 2>&1; then
        if node --check "${UI_APPJS}" >"${WORK}/node-check.log" 2>&1; then
            pass "node --check ui/app.js"
        else
            fail "node --check ui/app.js failed: $(tail -3 "${WORK}/node-check.log" | tr '\n' ' ')"
        fi
    else
        skip "node --check ui/app.js (node not installed -- CI must run this, see .github/workflows)"
    fi
}

# Prints offending URLs and exits 1. XML namespaces (http://www.w3.org/...) are
# identifiers, not fetched resources; loopback is the UI itself.
external_links() {
    python3 - "$1" <<'PY'
import re, sys
text = open(sys.argv[1], "r", encoding="utf-8", errors="replace").read()
allowed = ("http://127.0.0.1", "https://127.0.0.1", "http://localhost", "https://localhost",
           "http://www.w3.org/")
bad = []
for m in re.finditer(r"https?://[^\s\"'<>)]+", text):
    url = m.group(0)
    if url.startswith(allowed):
        continue
    bad.append(url)
if bad:
    print("\n".join(bad))
    sys.exit(1)
PY
}

# ===========================================================================
# 1. launcher, auth, static serving, preflight, shell of the API (§13.1-4,16,18)
# ===========================================================================
phase_busy_detection() {
    banner "busy detection: strict classifier + own descendants (F50)"
    cat > "${WORK}/busy_unit.py" <<'PYUNIT'
import os, sys
sys.path.insert(0, os.path.join(os.environ["PROJECT_DIR"], "ui"))
import runner

FAILS = 0

def check(label, ok):
    global FAILS
    print(("OK " if ok else "FAIL ") + label)
    if not ok:
        FAILS += 1

CASES = [
    ("python3 /x/ui/server.py --project /x/WGSpipeline-nextflow --nextflow /y/bin/nextflow",
     False, "our own server (project path + --nextflow)"),
    ("bash /x/bin/pipeline-ui --port 0", False, "our own launcher"),
    ("bash -c sleep 3 /x/WGSpipeline-nextflow run", False,
     "shell that merely mentions the checkout and 'run'"),
    ("rm -rf /x/WGSpipeline-nextflow/.nextflow/ui", False, "cleanup mentioning the checkout"),
    ("java -jar /x/nextflow-26.10.0-14055.jar -version", False,
     "preflight nextflow -version JVM"),
    ("nextflow -version", False, "bare nextflow -version"),
    ("nextflow config -flat", False, "nextflow config -flat"),
    ("/x/bin/nextflow run . --samples_tsv s.tsv", True, "the conda launcher for a run"),
    ("python3 /x/tests/ci/fixtures/ui/fake-nextflow run .", True, "the fake engine for a run"),
    ("/usr/bin/java -jar /x/nextflow-26.10.0-14055.jar run .", True, "the real engine JVM"),
    ("/usr/bin/java -classpath /x/nextflow.jar nextflow.cli.Launcher run .", True,
     "the classpath launcher"),
    ("/bin/bash -c 'cd /x && nextflow run .'", True, "sh -c wrapper around a run"),
]
for cmd, want, label in CASES:
    check("looks_like_nextflow(%s) == %s" % (label, want),
          runner.looks_like_nextflow(cmd) is want)

# synthetic process table: 100 = our server, 200/300/400 = our children and
# grandchildren (preflight/engine), 500 = a foreign engine, 600 = decoy,
# 700 = foreign `nextflow -version`.
ROWS = [
    (100, 1, "python3 /x/ui/server.py --project /x/WGSpipeline-nextflow"),
    (200, 100, "bash /x/bin/pipeline-ui --port 0"),
    (300, 100, "java -jar /x/nextflow-26.10.0-14055.jar run ."),
    (400, 300, "java -jar /x/nextflow-26.10.0-14055.jar run ."),
    (500, 1, "java -jar /x/nextflow-26.10.0-14055.jar run ."),
    (600, 1, "bash -c sleep 3 /x/WGSpipeline-nextflow run"),
    (700, 1, "java -jar /x/nextflow-26.10.0-14055.jar -version"),
]
found = runner.external_nextflow_pids_from_rows(ROWS, {100})
check("the foreign engine is reported", found == [500])
check("our own engine and its descendants are excluded", 300 not in found and 400 not in found)
check("the checkout-name decoy is not reported", 600 not in found)
check("a foreign preflight -version is not reported", 700 not in found)
sys.exit(1 if FAILS else 0)
PYUNIT
    local out rc
    out="$(PROJECT_DIR="${PROJECT_DIR}" python3 "${WORK}/busy_unit.py" 2>&1)"; rc=$?
    while IFS= read -r line; do
        case "${line}" in
            "OK "*) pass "busy: ${line#OK }" ;;
            "FAIL "*) fail "busy: ${line#FAIL }" ;;
        esac
    done <<< "${out}"
    expect_eq "busy detection unit script exits 0" "0" "${rc}"
    if [ "${rc}" -ne 0 ]; then printf '%s\n' "${out}" | head -25 | sed 's/^/    /'; fi
}

phase_startup_auth_preflight() {
    banner "launcher + auth + static serving (§13.1, §13.2, §13.4, §13.16)"
    local code body hdr

    # The UI runs the fake engine (normal success unless a scenario rewrites the
    # control file); FAKE_NEXTFLOW_CTRL is inherited by the server's children.
    export FAKE_NEXTFLOW_CTRL="${CTRL}"
    if ! start_ui "${P_FAKE}" "${LOG_DIR}/ui-01-startup.log" --nextflow "${FAKE}"; then
        fail "launcher failed to start: no UI_READY"
        return 1
    fi
    expect_grep "launcher prints UI_READY (--print-url)" "UI_READY" "${CUR_LOG}"
    expect_eq "UI_READY URL carries the --token value" "${TOKEN_ARG}" "${TOKEN}"
    if [ -n "${PORT}" ]; then pass "UI_READY URL has an explicit port (${PORT})"; else fail "UI_READY URL has no port"; fi
    if wait_api_ready "/api/state" 25; then pass "GET /api/state 200 after startup"; else fail "GET /api/state never returned 200"; fi

    # --- GET / without token: 403 HTML pointing at the printed URL
    code="$(curl -sS --max-time 20 -o "${WORK}/index-403.html" -w '%{http_code}' "${BASE}/")"
    expect_eq "GET / without token -> 403" "403" "${code}"
    if grep -qi '<html' "${WORK}/index-403.html"; then pass "403 response is HTML"; else fail "403 response is not HTML"; fi

    # --- GET / with token: 200 + Set-Cookie + Chinese title
    code="$(curl -sS --max-time 20 -c "${WORK}/cookies.txt" -D "${WORK}/hdr-index.txt" \
        -o "${WORK}/index.html" -w '%{http_code}' "${BASE}/?token=${TOKEN}")"
    expect_eq "GET /?token=... -> 200" "200" "${code}"
    if header_has "${WORK}/hdr-index.txt" '^set-cookie:.*ui_token='; then
        pass "GET /?token=... sets ui_token cookie"
    else
        fail "GET /?token=... did not set a ui_token cookie"
    fi
    if header_has "${WORK}/hdr-index.txt" '^set-cookie:.*httponly'; then pass "cookie is HttpOnly"; else fail "cookie is not HttpOnly"; fi
    if header_has "${WORK}/hdr-index.txt" '^set-cookie:.*samesite=strict'; then pass "cookie is SameSite=Strict"; else fail "cookie is not SameSite=Strict"; fi
    if html_title_ok "${WORK}/index.html"; then pass "GET / serves the Chinese single page (<title> + CJK)"; else fail "GET / body is not the Chinese single page"; fi

    # --- cookie alone is enough (browser reload after the first navigation)
    code="$(curl -sS --max-time 20 -b "${WORK}/cookies.txt" -o "${WORK}/index-cookie.html" -w '%{http_code}' "${BASE}/")"
    expect_eq "GET / with cookie but no token -> 200" "200" "${code}"
    code="$(curl -sS --max-time 20 -o "${WORK}/index-badtoken.html" -w '%{http_code}' "${BASE}/?token=wrong-token")"
    expect_eq "GET / with a wrong token -> 403" "403" "${code}"

    # --- static files: same origin, no auth (contract: no sensitive content)
    code="$(curl -sS --max-time 20 -o "${WORK}/app.js" -w '%{http_code}' "${BASE}/app.js")"
    expect_eq "GET /app.js (no token) -> 200" "200" "${code}"
    if cmp -s "${WORK}/app.js" "${UI_APPJS}"; then pass "GET /app.js bytes == ui/app.js"; else fail "GET /app.js differs from ui/app.js"; fi
    code="$(curl -sS --max-time 20 -o "${WORK}/style.css" -w '%{http_code}' "${BASE}/style.css")"
    expect_eq "GET /style.css (no token) -> 200" "200" "${code}"

    # --- /api/state shell
    get_state
    if json_get "${WORK}/state.json" server >/dev/null 2>&1 && [ -n "$(json_get "${WORK}/state.json" server)" ]; then
        pass "GET /api/state has a server object"
    else
        fail "GET /api/state has no server object"
    fi
    if [ "$(json_get "${WORK}/state.json" server.port)" = "${PORT}" ]; then
        pass "GET /api/state server.port matches the URL port"
    else
        fail "GET /api/state server.port '$(json_get "${WORK}/state.json" server.port)' != URL port '${PORT}'"
    fi
    expect_empty "GET /api/state run is null while idle" "$(state_field run.state)"

    # --- auth negatives (no token) -----------------------------------------
    code="$(curl -sS --max-time 20 -H 'Content-Type: application/json' -d '{}' \
        -o "${WORK}/post-noauth.json" -w '%{http_code}' "$(api_url run)")"
    expect_eq "POST /api/run without token -> 403 (auth before body)" "403" "${code}"
    code="$(curl -sS --max-time 20 -H 'Content-Type: application/json' -d '{}' \
        -o "${WORK}/cancel-noauth.json" -w '%{http_code}' "$(api_url cancel)")"
    expect_eq "POST /api/cancel without token -> 403" "403" "${code}"

    # --- Origin check (§10, §13.16): local CSRF guard ----------------------
    code="$(curl -sS --max-time 20 -H "Origin: http://evil.example" -H "X-UI-Token: ${TOKEN}" \
        -o "${WORK}/state-evil.json" -w '%{http_code}' "$(api_url state)")"
    expect_eq "GET /api/state with Origin: http://evil.example -> 403" "403" "${code}"
    mk_body "${WORK}/body-origin.json" resume 0 "" \
        "SR-A" "${P_FAKE}/data/raw/a_1.fastq.gz" "${P_FAKE}/data/raw/a_2.fastq.gz" "Escherichia coli" "K-12" ""
    code="$(curl -sS --max-time 20 -H 'Content-Type: application/json' -H "X-UI-Token: ${TOKEN}" \
        -H "Origin: http://evil.example" -d @"${WORK}/body-origin.json" \
        -o "${WORK}/run-evil.json" -w '%{http_code}' "$(api_url run)")"
    expect_eq "POST /api/run with Origin: http://evil.example -> 403" "403" "${code}"
    get_state
    expect_empty "evil-Origin POST did not spawn a run" "$(state_field run.state)"
    code="$(curl -sS --max-time 20 -H "Origin: ${ORIGIN_OK}" -H "X-UI-Token: ${TOKEN}" \
        -o "${WORK}/state-okorigin.json" -w '%{http_code}' "$(api_url state)")"
    expect_eq "GET /api/state with Origin: ${ORIGIN_OK} -> 200" "200" "${code}"

    # --- preflight (§7.1, §13.4) ------------------------------------------
    if wait_api_ready "/api/preflight" 30; then
        pass "GET /api/preflight 200"
    else
        fail "GET /api/preflight never returned 200"
    fi
    get_api "/api/preflight" "${WORK}/preflight.json" >/dev/null
    expect_eq "preflight nextflow check is ok (fake engine on PATH)" "ok" "$(preflight_status "${WORK}/preflight.json" nextflow)"
    local ids
    ids="$(python3 - "${WORK}/preflight.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], "r", encoding="utf-8"))
print(",".join(str(c.get("id")) for c in (d.get("checks") or [])))
PY
)"
    case ",${ids}," in
        *,nextflow,*) pass "preflight checks include 'nextflow'" ;;
        *) fail "preflight checks lack 'nextflow' (got ${ids})" ;;
    esac
    case ",${ids}," in
        *,disk,*) pass "preflight checks include 'disk'" ;;
        *) fail "preflight checks lack 'disk' (got ${ids})" ;;
    esac
    if [ "$(json_get "${WORK}/preflight.json" summary.failures)" != "" ]; then
        pass "preflight has summary.failures"
    else
        fail "preflight lacks summary.failures"
    fi
    if [ -n "$(json_get "${WORK}/preflight.json" config.source)" ]; then
        pass "preflight reports config.source ($(json_get "${WORK}/preflight.json" config.source))"
    else
        fail "preflight lacks config.source"
    fi

    # --- Cache-Control: no-store on the API (§7, §13.18) ------------------
    expect_no_store "GET /api/state" "/api/state"
    expect_no_store "GET /api/preflight" "/api/preflight"

    # --- /api/fs (§7.3): the frontend's file browser ----------------------
    code="$(get_api "/api/fs?path=${P_FAKE}/data/raw" "${WORK}/fs.json")"
    expect_eq "GET /api/fs?path=data/raw -> 200" "200" "${code}"
    if grep -q 'a_1.fastq.gz' "${WORK}/fs.json" && grep -q '"kind": *"fastq"' "${WORK}/fs.json"; then
        pass "/api/fs lists the FASTQ file with kind=fastq"
    else
        fail "/api/fs did not list a_1.fastq.gz as kind=fastq"
    fi
}

html_title_ok() {
    python3 - "$1" <<'PY'
import sys
data = open(sys.argv[1], "r", encoding="utf-8", errors="replace").read()
if "<title>" not in data.lower():
    sys.exit(1)
if not any(ord(ch) > 127 for ch in data):
    sys.exit(1)
sys.exit(0)
PY
}

# ===========================================================================
# 2. idle semantics + validation: 400, no spawn (§13.5, §13.16, §18)
# ===========================================================================
phase_validation() {
    banner "validation: 400 with row+field, and NO spawn (§6, §13.5)"
    local dirs_before dirs_after resp code label body
    dirs_before="$(count_run_dirs "${P_FAKE}")"

    # POST /api/cancel with no active run -> 409 no_active_run
    printf '{}\n' > "${WORK}/empty.json"
    code="$(post_api "/api/cancel" "${WORK}/empty.json" "${WORK}/cancel-idle.json")"
    expect_eq "POST /api/cancel with no active run -> 409" "409" "${code}"
    expect_eq "POST /api/cancel error is no_active_run" "no_active_run" "$(json_get "${WORK}/cancel-idle.json" error)"

    validation_case() { # <label> <bodyfile> <require row+field 0|1>
        local label="$1" body="$2" want_rowcol="$3" code
        code="$(post_api_hdr "/api/run" "${body}" "${WORK}/val-resp.json" "${WORK}/hdr-val.txt")"
        if [ "${code}" = "409" ] && [ "$(json_get "${WORK}/val-resp.json" error)" = "busy" ]; then
            skip "${label}: validation not exercised -- §7.2 busy (external engine); CI runs this check alone"
            return 0
        fi
        expect_eq "${label}: POST /api/run -> 400" "400" "${code}"
        expect_eq "${label}: error code validation_failed" "validation_failed" "$(json_get "${WORK}/val-resp.json" error)"
        if grep -qiE '^cache-control:[[:space:]]*no-store' "${WORK}/hdr-val.txt"; then
            pass "${label}: 400 response is Cache-Control: no-store"
        else
            fail "${label}: 400 response lacks Cache-Control: no-store"
        fi
        if [ "${want_rowcol}" = "1" ]; then
            if python3 - "${WORK}/val-resp.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], "r", encoding="utf-8"))
errs = d.get("errors") or []
sys.exit(0 if any(isinstance(e, dict) and "row" in e and "field" in e for e in errs) else 1)
PY
            then
                pass "${label}: errors[] carries row+field"
            else
                fail "${label}: errors[] lacks row+field ($(head -c 200 "${WORK}/val-resp.json"))"
            fi
        fi
        get_state
        expect_empty "${label}: state.run stays null (no spawn)" "$(state_field run.state)"
    }

    local A="${P_FAKE}/data/raw/a_1.fastq.gz" B="${P_FAKE}/data/raw/a_2.fastq.gz"
    local NB="${P_FAKE}/data/raw/does-not-exist.fastq.gz"
    local REF="${P_FAKE}/data/reference/ref.fasta"

    # missing fq2
    mk_body "${WORK}/v-missing-fq2.json" resume 0 "" "SR-X" "${A}" "" "" "" ""
    validation_case "missing fq2" "${WORK}/v-missing-fq2.json" 1

    # relative fq1
    mk_body "${WORK}/v-relative.json" resume 0 "" "SR-X" "data/raw/a_1.fastq.gz" "${B}" "" "" ""
    validation_case "relative fq1" "${WORK}/v-relative.json" 1

    # nonexistent fq1
    mk_body "${WORK}/v-notfound.json" resume 0 "" "SR-X" "${NB}" "${B}" "" "" ""
    validation_case "nonexistent fq1" "${WORK}/v-notfound.json" 1

    # duplicate sample name
    mk_body "${WORK}/v-duplicate.json" resume 0 "" \
        "DUP" "${A}" "${B}" "" "" "" \
        "DUP" "${P_FAKE}/data/raw/b_1.fastq.gz" "${P_FAKE}/data/raw/b_2.fastq.gz" "" "" ""
    validation_case "duplicate sample name" "${WORK}/v-duplicate.json" 1

    # illegal characters in the sample name
    mk_body "${WORK}/v-invalid.json" resume 0 "" "bad name!" "${A}" "${B}" "" "" ""
    validation_case "illegal sample name" "${WORK}/v-invalid.json" 1

    # same file as fq1 and fq2 (§6: code same_file)
    mk_body "${WORK}/v-samefile.json" resume 0 "" "SR-X" "${A}" "${A}" "" "" ""
    validation_case "fq2 == fq1" "${WORK}/v-samefile.json" 1

    # no samples at all
    printf '{"samples": [], "mode": "resume", "clean": false, "profile": "standard", "report_tag": ""}\n' \
        > "${WORK}/v-nosamples.json"
    validation_case "no samples" "${WORK}/v-nosamples.json" 0

    # non-JSON content type is refused (POST only accepts application/json)
    printf '{}\n' > "${WORK}/empty.json"
    code="$(curl -sS --max-time 20 -H 'Content-Type: text/plain' -H "X-UI-Token: ${TOKEN}" \
        -d 'not json' -o "${WORK}/textplain.json" -w '%{http_code}' "$(api_url run)")"
    case "${code}" in
        400|415|403) pass "POST /api/run with text/plain is refused (HTTP ${code})" ;;
        409) skip "text/plain refusal not exercised -- §7.2 busy (external engine)" ;;
        *) fail "POST /api/run with text/plain returned ${code}, expected 4xx" ;;
    esac

    dirs_after="$(count_run_dirs "${P_FAKE}")"
    expect_eq "no run directory was created by any rejected request" "${dirs_before}" "${dirs_after}"
    local kids
    kids="$(server_children "${CUR_SERVER}")"
    if [ -z "${kids}" ]; then
        pass "no engine child of the UI server was spawned by a rejected request"
    else
        fail "rejected request left a child process running: ${kids}"
    fi

    dirs_before="${dirs_after}"
}

# ===========================================================================
# 3. single instance (§13.13)
# ===========================================================================
phase_single_instance() {
    banner "single instance: second launcher exits 1 with UI_ALREADY_RUNNING (§3, §13.13)"
    local second_log="${LOG_DIR}/ui-02-second.log" pid i=0 code
    : > "${second_log}"
    "${UI_BIN}" --project "${P_FAKE}" --port 0 --no-open --print-url --token second-token \
        >"${second_log}" 2>&1 &
    pid=$!
    UI_PIDS="${UI_PIDS} ${pid}"
    while [ "${i}" -lt 150 ] && kill -0 "${pid}" 2>/dev/null; do
        sleep 0.1
        i=$((i + 1))
    done
    if kill -0 "${pid}" 2>/dev/null; then
        kill -KILL "${pid}" 2>/dev/null
        fail "second launcher kept running (single-instance lock did not fire)"
        return
    fi
    wait "${pid}" 2>/dev/null
    code=$?
    expect_eq "second launcher exit code is 1" "1" "${code}"
    if grep -q 'UI_ALREADY_RUNNING' "${second_log}"; then
        pass "second launcher prints UI_ALREADY_RUNNING"
    else
        fail "second launcher did not print UI_ALREADY_RUNNING: $(head -3 "${second_log}" | tr '\n' ' ')"
    fi
    # The first instance must still be alive and serving after the attempt.
    expect_eq "first instance still serves /api/state after the second attempt" "200" "$(get_api "/api/state" "${WORK}/state.json")"
}

# ===========================================================================
# 4. success run: sheet bytes, 409 while running, SSE, done, report, downloads
#    (§13.6-9, §13.18)
# ===========================================================================
phase_success_run() {
    banner "successful run: samples.tsv, 409, SSE, done, report, downloads (§13.6-9)"
    local P="${P_FAKE}"
    local A1="${P}/data/raw/a_1.fastq.gz" A2="${P}/data/raw/a_2.fastq.gz"
    local B1="${P}/data/raw/b_1.fastq.gz" B2="${P}/data/raw/b_2.fastq.gz"
    local REF="${P}/data/reference/ref.fasta"
    local body="${WORK}/body-success.json"

    mk_body "${body}" resume 0 "" \
        "SR-A" "${A1}" "${A2}" "Escherichia coli" "K-12" "${REF}" \
        "SR-B" "${B1}" "${B2}" "" "" ""

    echo '{"mode": "success", "delay": 0.12,
           "report_html": "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>FAKE MULTIQC REPORT</title></head><body><h1>FAKE MULTIQC REPORT</h1><p>sentinel:FAKE_REPORT_BODY</p></body></html>",
           "results_files": {"results/assembly/SR-A/contigs.fasta": ">contig_1\nACGTACGTACGT\n",
                              "results/assembly/SR-B/contigs.fasta": ">contig_1\nTTTTGGGGCCCC\n",
                              "results/qc/quast/SR-A/report.txt": "QUAST fake report for SR-A\n",
                              "results/qc/fastqc_raw/SR-A_1_fastqc.html": "<html>FAKE FASTQC</html>\n"}}' \
        > "${CTRL}"

    local code run_id
    code="$(post_run_retrying_busy "${body}" "${WORK}/run-response.json")"
    if ! run_accepted_or_skip "successful run" "${code}" "${WORK}/run-response.json"; then
        return 0
    fi
    run_id="$(json_get "${WORK}/run-response.json" run_id)"
    if [ -n "${run_id}" ]; then pass "POST /api/run returns a run_id"; else fail "POST /api/run response has no run_id"; fi

    if wait_run_state_t 30 running; then pass "state becomes running"; else fail "state never became running"; fi
    expect_eq "GET /api/state run.id matches the POST response" "${run_id}" "$(state_field run.id)"
    expect_eq "mode is resume" "resume" "$(state_field run.mode)"
    expect_eq "started_at is set" "true" "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("true" if (d.get("run") or {}).get("started_at") else "false")' "${WORK}/state.json")"

    # --- samples.tsv: byte-exact (§13.6) ----------------------------------
    local sheet
    sheet="$(state_field run.sheet)"
    if [ -n "${sheet}" ] && [ -f "${sheet}" ]; then
        pass "run.sheet points at an existing file (${sheet})"
    else
        fail "run.sheet is missing or does not exist: '${sheet}'"
    fi
    case "${sheet}" in
        "${P}/.nextflow/ui/runs/${run_id}/samples.tsv") pass "sheet lives in .nextflow/ui/runs/<id>/ (§2)" ;;
        *) fail "sheet path '${sheet}' is not .nextflow/ui/runs/${run_id}/samples.tsv" ;;
    esac
    expected_sheet "${WORK}/samples-expected.tsv" "${body}"
    if cmp -s "${WORK}/samples-expected.tsv" "${sheet}"; then
        pass "samples.tsv is byte-identical to the 6-column contract"
    else
        fail "samples.tsv differs from the contract: $(diff <(od -c "${WORK}/samples-expected.tsv") <(od -c "${sheet}") 2>/dev/null | head -6 | tr '\n' ' ')"
    fi
    if out="$(sheet_layout_ok "${sheet}")"; then
        pass "sheet layout: header/tabs/absolute paths/LF/final newline"
    else
        fail "sheet layout: ${out}"
    fi
    expect_file "run.log exists" "$(state_field run.log)"
    if grep -qF -- "=== run ${run_id}:" "$(state_field run.log)"; then
        pass "run.log starts with the documented '=== run <id>: <argv> ===' header"
    else
        fail "run.log lacks the '=== run <id>:' header"
    fi
    if [ -n "$(state_field run.argv)" ] && printf '%s' "$(state_field run.argv)" | grep -q -- '-resume'; then
        pass "mode=resume put -resume in argv"
    else
        fail "argv has no -resume: $(state_field run.argv)"
    fi
    expect_eq "argv carries --samples_tsv" "true" "$(python3 -c 'import json,sys
d=json.load(open(sys.argv[1])); a=(d.get("run") or {}).get("argv") or []
print("true" if "--samples_tsv" in a else "false")' "${WORK}/state.json")"
    expect_eq "repository-root samples.tsv was not touched (UI-D1)" "${REPO_SAMPLES_SHA}" "$(sha256_of "${REPO_SAMPLES}")"

    # --- second POST while running -> 409 (§13.7) -------------------------
    code="$(post_api "/api/run" "${body}" "${WORK}/run-409.json")"
    expect_eq "second POST /api/run while running -> 409" "409" "${code}"
    expect_eq "409 error code is busy" "busy" "$(json_get "${WORK}/run-409.json" error)"

    # --- live SSE (§13.8) -------------------------------------------------
    local hdr="${WORK}/hdr-sse-live.txt" sse="${WORK}/sse-live.txt"
    code="$(curl -sS -N --max-time 2.5 -D "${hdr}" -o "${sse}" \
        "${BASE}/api/events?from=0&token=${TOKEN}" 2>/dev/null || true)"
    if header_has "${hdr}" '^content-type:.*text/event-stream'; then pass "SSE Content-Type is text/event-stream"; else fail "SSE Content-Type is not text/event-stream"; fi
    if header_has "${hdr}" '^cache-control:.*no-cache'; then pass "SSE Cache-Control is no-cache (§8)"; else fail "SSE Cache-Control is not no-cache"; fi
    sse_summary "${sse}" "${WORK}/sse-live"
    if grep -qx 'running' "${WORK}/sse-live.states"; then
        pass "SSE sends a state event with state=running"
    else
        fail "SSE state events: $(tr '\n' ' ' < "${WORK}/sse-live.states")"
    fi
    if grep -qF 'Submitted process' "${WORK}/sse-live.logtext"; then pass "SSE live stream carries Submitted lines"; else fail "SSE live stream has no Submitted line"; fi
    if grep -qF 'Completed process' "${WORK}/sse-live.logtext"; then pass "SSE live stream carries Completed lines"; else fail "SSE live stream has no Completed line"; fi

    # --- terminal state ---------------------------------------------------
    if wait_run_state_t 120 done failed; then
        pass "run reaches a terminal state"
    else
        fail "run never reached done/failed (state=$(state_field run.state))"
    fi
    expect_eq "run state is done (exit 0 + report found)" "done" "$(state_field run.state)"
    expect_eq "run.exit_code is 0" "0" "$(state_field run.exit_code)"
    expect_eq "run.finished_at is set" "true" "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("true" if (d.get("run") or {}).get("finished_at") else "false")' "${WORK}/state.json")"
    if [ -n "$(state_field run.elapsed_seconds)" ]; then pass "run.elapsed_seconds is set"; else fail "run.elapsed_seconds is missing"; fi
    expect_eq "run.report.url is /api/report" "/api/report" "$(state_field run.report.url)"
    expect_eq "default fake shape: tasks.completed == tasks.submitted (stdout Completed lines)" \
        "$(state_field run.tasks.submitted)" "$(state_field run.tasks.completed)"
    if grep -qF 'Completed process >' "$(state_field run.log)"; then
        pass "default fake shape: run.log carries 'Completed process >' lines"
    else
        fail "default fake shape: run.log lost its Completed process lines"
    fi

    local rpath
    rpath="$(state_field run.report.path)"
    if [ -n "${rpath}" ] && [ -f "${rpath}" ]; then pass "report path exists (${rpath})"; else fail "report path missing: '${rpath}'"; fi
    expect_eq "run.report.size == actual file size" "$(wc -c < "${rpath}" | tr -d ' ')" "$(state_field run.report.size)"
    if [ "$(wc -c < "${rpath}" | tr -d ' ')" -gt 0 ]; then pass "fake report is non-empty"; else fail "fake report is empty"; fi
    get_state
    if [ -f "${P}/.nextflow/ui/state.json" ]; then
        pass ".nextflow/ui/state.json exists"
        expect_eq "state.json run.id == /api/state run.id" "$(state_field run.id)" "$(json_get "${P}/.nextflow/ui/state.json" run.id)"
    else
        fail ".nextflow/ui/state.json is missing"
    fi

    # --- /api/report (§13.9) ---------------------------------------------
    code="$(get_api_hdr "/api/report" "${WORK}/dl-report.html" "${WORK}/hdr-report.txt")"
    expect_eq "GET /api/report -> 200" "200" "${code}"
    if header_has "${WORK}/hdr-report.txt" '^content-type:.*text/html'; then pass "/api/report Content-Type is text/html"; else fail "/api/report Content-Type is not text/html"; fi
    expect_eq "/api/report X-UI-Report-Path names the located report" \
        "${rpath}" "$(header_value "${WORK}/hdr-report.txt" "x-ui-report-path")"
    if cmp -s "${WORK}/dl-report.html" "${rpath}"; then pass "/api/report bytes == report file"; else fail "/api/report bytes differ from ${rpath}"; fi
    expect_no_store "GET /api/report" "/api/report"
    get_api_hdr "/api/state" "${WORK}/state.json" "${WORK}/hdr-state.txt" >/dev/null
    expect_no_store "GET /api/download?what=log" "/api/download?what=log"

    # --- downloads (§13.9) ------------------------------------------------
    code="$(get_api_hdr "/api/download?what=results" "${WORK}/dl-results.tar.gz" "${WORK}/hdr-results.txt")"
    expect_eq "GET /api/download?what=results -> 200" "200" "${code}"
    if header_has "${WORK}/hdr-results.txt" '^content-type:.*application/gzip'; then pass "results download is application/gzip"; else fail "results download Content-Type is not application/gzip"; fi
    if header_has "${WORK}/hdr-results.txt" '^content-disposition:.*attachment;.*filename="results-.*\.tar\.gz"'; then
        pass "results download has the documented Content-Disposition"
    else
        fail "results Content-Disposition: '$(grep -i '^content-disposition' "${WORK}/hdr-results.txt" | tr -d '\r')'"
    fi
    if out="$(tar_matches "${WORK}/dl-results.tar.gz" "${CTRL}" "${rpath}")"; then
        pass "results tarball file list == fake results tree (no .DS_Store)"
    else
        fail "results tarball mismatch: ${out}"
    fi
    if tar -tzf "${WORK}/dl-results.tar.gz" 2>/dev/null | grep -q '\.DS_Store'; then
        fail "results tarball contains .DS_Store"
    else
        pass "results tarball excludes .DS_Store"
    fi
    code="$(get_api "/api/download?what=report" "${WORK}/dl-report-dl.html")"
    expect_eq "GET /api/download?what=report -> 200" "200" "${code}"
    if cmp -s "${WORK}/dl-report-dl.html" "${rpath}"; then pass "report download bytes == report file"; else fail "report download differs from the report file"; fi
    code="$(get_api "/api/download?what=log" "${WORK}/dl-run.log")"
    expect_eq "GET /api/download?what=log -> 200" "200" "${code}"
    if cmp -s "${WORK}/dl-run.log" "$(state_field run.log)"; then pass "log download bytes == run.log"; else fail "log download differs from run.log"; fi
    if grep -qF 'sentinel:FAKE_REPORT_BODY' "${rpath}"; then pass "report carries the fake sentinel body"; else fail "report body lost the sentinel"; fi

    # --- SSE replay / from= offset (§8, §13.8) ---------------------------
    local logf offset sse_all
    logf="$(state_field run.log)"
    offset="$(python3 - "${logf}" <<'PY'
import sys
data = open(sys.argv[1], "rb").read()
i = data.index(b"Submitted process")
end = data.find(b"\n", i)
print(end + 1)
PY
)"
    sse_all="${WORK}/sse-replay0.txt"
    curl -sS -N --max-time 2 -o "${sse_all}" "${BASE}/api/events?from=0&token=${TOKEN}" 2>/dev/null || true
    sse_summary "${sse_all}" "${WORK}/sse-replay0"
    if grep -qx 'done' "${WORK}/sse-replay0.states"; then
        pass "SSE replay sends the terminal state (done)"
    else
        fail "SSE replay state events: $(tr '\n' ' ' < "${WORK}/sse-replay0.states")"
    fi
    if cmp -s "${WORK}/sse-replay0.logtext" "${logf}"; then
        pass "SSE from=0 replays run.log byte-identically"
    else
        fail "SSE from=0 replay differs from run.log ($(wc -c < "${WORK}/sse-replay0.logtext" 2>/dev/null) vs $(wc -c < "${logf}" 2>/dev/null) bytes)"
    fi

    local sse_from
    sse_from="${WORK}/sse-replayX.txt"
    curl -sS -N --max-time 2 -o "${sse_from}" "${BASE}/api/events?from=${offset}&token=${TOKEN}" 2>/dev/null || true
    sse_summary "${sse_from}" "${WORK}/sse-replayX"
    expect_eq "SSE from=<offset> starts at that byte offset" "${offset}" "$(meta_value "${WORK}/sse-replayX.meta" first_from)"
    if grep -qF 'Launching' "${WORK}/sse-replayX.logtext"; then
        fail "SSE from=<offset> re-sent bytes before the offset"
    else
        pass "SSE from=<offset> does not re-send earlier log bytes"
    fi
    if tail -c +$((offset + 1)) "${logf}" | cmp -s - "${WORK}/sse-replayX.logtext"; then
        pass "SSE from=<offset> text == run.log tail from that offset"
    else
        fail "SSE from=<offset> text differs from the run.log tail"
    fi
}

# ===========================================================================
# 4b. F48: real engines print only Submitted on stdout; completion is a
#     `Task completed > TaskHandler[...]` DEBUG line in <project>/.nextflow.log.
#     The fake's default mode prints Completed itself, which is why this bug
#     survived; this scenario uses the real shape (engine_log=true) and asserts
#     the ladder/task counts come from the log.
# ===========================================================================
phase_engine_log_run() {
    banner "engine log: completion parsed from .nextflow.log, not stdout (F48)"
    local P="${P_FAKE}"
    local body="${WORK}/body-engine-log.json" code logf rpath mismatch spec
    mk_body "${body}" fresh 0 "" \
        "SR-A" "${P}/data/raw/a_1.fastq.gz" "${P}/data/raw/a_2.fastq.gz" "Escherichia coli" "K-12" "${P}/data/reference/ref.fasta" \
        "SR-B" "${P}/data/raw/b_1.fastq.gz" "${P}/data/raw/b_2.fastq.gz" "" "" ""

    # A fresh run must not inherit completions from an earlier run: the fake
    # rotates .nextflow.log (new inode), which is what real nextflow does too.
    rm -rf "${P}/results" "${P}/.nextflow.log" "${P}/.nextflow.log.1"
    echo '{"mode": "success", "delay": 0.02, "engine_log": true,
           "report_html": "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>ENGINE LOG REPORT</title></head><body><p>sentinel:FAKE_ENGINE_LOG_REPORT</p></body></html>",
           "results_files": {"results/assembly/SR-A/contigs.fasta": ">contig_1\nACGTACGT\n"}}' \
        > "${CTRL}"

    code="$(post_run_retrying_busy "${body}" "${WORK}/run-engine-log-response.json")"
    if ! run_accepted_or_skip "engine_log run" "${code}" "${WORK}/run-engine-log-response.json"; then
        return 0
    fi
    if wait_run_state_t 120 done failed; then pass "engine_log run reaches a terminal state"; else fail "engine_log run never reached a terminal state"; fi
    expect_eq "engine_log run state is done" "done" "$(state_field run.state)"
    expect_eq "engine_log run exit_code is 0" "0" "$(state_field run.exit_code)"
    logf="$(state_field run.log)"

    # The fixture shape: 2 samples x 11 + 1 reference + growth + MultiQC = 25.
    expect_eq "engine_log: tasks.submitted == 25 (from stdout Submitted lines)" "25" "$(state_field run.tasks.submitted)"
    expect_eq "engine_log: tasks.completed == 25 (from .nextflow.log TaskHandler lines)" "25" "$(state_field run.tasks.completed)"
    expect_eq "engine_log: tasks.expected == 25" "25" "$(state_field run.tasks.expected)"
    expect_eq "engine_log: tasks.failed == 0" "0" "$(state_field run.tasks.failed)"
    expect_eq "engine_log: run.log has NO 'Completed process >' line (real-engine shape)" \
        "0" "$(grep -c 'Completed process >' "${logf}" 2>/dev/null || true)"
    expect_file "engine_log: engine wrote <project>/.nextflow.log" "${P}/.nextflow.log"
    expect_eq "engine_log: .nextflow.log has one TaskHandler line per task" \
        "25" "$(grep -c 'Task completed > TaskHandler' "${P}/.nextflow.log" 2>/dev/null || true)"

    # Per-process ladder: every step fully complete, and the counts are the
    # contract's expected numbers, not just "submitted == expected".
    mismatch="$(python3 - "${WORK}/state.json" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    data = {}
run = data.get("run") or {}
bad = []
for step in run.get("steps") or []:
    if step.get("completed") != step.get("expected"):
        bad.append("%s:%s/%s" % (step.get("process"), step.get("completed"), step.get("expected")))
print(",".join(bad))
PY
)"
    expect_empty "engine_log: every ladder step completed == expected" "${mismatch}"
    for spec in "FASTQC_RAW 4" "TRIMMOMATIC 2" "SPADES 2" "MULTIQC 1" "ASSEMBLY_REF 1" "GROWTH_CHECK 1"; do
        set -- ${spec}
        expect_eq "engine_log: ${1} completed == ${2}" "${2}" "$(step_field "${1}" completed)"
    done
    expect_eq "engine_log: FASTQC_RAW by_sample SR-A == 2" "2" "$(step_by_sample FASTQC_RAW SR-A)"
    expect_eq "engine_log: FASTQC_RAW by_sample SR-B == 2" "2" "$(step_by_sample FASTQC_RAW SR-B)"
    expect_eq "engine_log: SPADES by_sample SR-B == 1" "1" "$(step_by_sample SPADES SR-B)"

    # `done` must still require a real report, not just "all Submitted".
    expect_eq "engine_log: run.report.url is /api/report" "/api/report" "$(state_field run.report.url)"
    rpath="$(state_field run.report.path)"
    if [ -n "${rpath}" ] && [ -f "${rpath}" ] && grep -qF 'sentinel:FAKE_ENGINE_LOG_REPORT' "${rpath}"; then
        pass "engine_log: done report is the new report written by this run"
    else
        fail "engine_log: done report is missing or stale: '${rpath}'"
    fi
    expect_eq "engine_log: run.failed is null" "" "$(state_field run.failed)"
}

# ===========================================================================
# 5. failed run: failure panel fields from /api/state (§13.10)
# ===========================================================================
phase_failed_run() {
    banner "failed run: process/tag/exit_code/stderr_tail (§4, §13.10)"
    local P="${P_FAKE}"
    local body="${WORK}/body-fail.json" code
    mk_body "${body}" fresh 0 "" \
        "SR-A" "${P}/data/raw/a_1.fastq.gz" "${P}/data/raw/a_2.fastq.gz" "Escherichia coli" "K-12" "" \
        "SR-B" "${P}/data/raw/b_1.fastq.gz" "${P}/data/raw/b_2.fastq.gz" "" "" ""
    # Isolate the results tree for this scenario. docs/ui.md §9 lets report
    # location fall back to <multiqc_dir>/<report>.html, so a report left by the
    # previous scenario would be exposed by a failed or report-less run. A fresh
    # project has an empty results tree; each failure-mode scenario does too.
    rm -rf "${P}/results"
    echo '{"mode": "fail", "delay": 0.02, "fail_process": "SPADES", "fail_sample": "SR-B"}' > "${CTRL}"

    code="$(post_run_retrying_busy "${body}" "${WORK}/run-fail-response.json")"
    if ! run_accepted_or_skip "failed run" "${code}" "${WORK}/run-fail-response.json"; then
        return 0
    fi
    if wait_run_state_t 120 failed done; then pass "fail run reaches a terminal state"; else fail "fail run never reached a terminal state"; fi
    expect_eq "fail run state is failed" "failed" "$(state_field run.state)"
    expect_eq "failed.process is SPADES" "SPADES" "$(state_field run.failed.process)"
    expect_eq "failed.tag is SR-B" "SR-B" "$(state_field run.failed.tag)"
    expect_eq "failed.exit_code is 1" "1" "$(state_field run.failed.exit_code)"
    if printf '%s' "$(state_field run.failed.stderr_tail)" | grep -qF 'sentinel:FAKE_STDERR_TAIL'; then
        pass "failed.stderr_tail carries the engine's error output"
    else
        fail "failed.stderr_tail lacks the fake error sentinel: '$(state_field run.failed.stderr_tail | head -c 160)'"
    fi
    if [ -n "$(state_field run.failed.work_dir)" ]; then pass "failed.work_dir is set"; else fail "failed.work_dir is missing"; fi
    if [ -n "$(state_field run.failed.message)" ]; then pass "failed.message is set"; else fail "failed.message is missing"; fi
    if [ -n "$(state_field run.failed.reason)" ]; then pass "failed.reason is set ($(state_field run.failed.reason))"; else fail "failed.reason is missing"; fi
    expect_eq "failed run has no report" "" "$(state_field run.report.path)"
    expect_eq "failed run exit_code is 1" "1" "$(state_field run.exit_code)"
    if grep -qF "Error executing process > 'SPADES (SR-B)'" "$(state_field run.log)"; then
        pass "run.log carries the engine error block (failure panel source)"
    else
        fail "run.log lacks the SPADES (SR-B) error block"
    fi
}

# ===========================================================================
# 6. exit 0 without a report is a FAILURE, never done (§13.11)
# ===========================================================================
phase_noreport_run() {
    banner "no report + exit 0 => failed(report_missing), never done (§4, §13.11)"
    local P="${P_FAKE}"
    local body="${WORK}/body-noreport.json" code
    mk_body "${body}" fresh 0 "" \
        "SR-A" "${P}/data/raw/a_1.fastq.gz" "${P}/data/raw/a_2.fastq.gz" "" "" ""
    # Isolate the results tree for this scenario. docs/ui.md §9 lets report
    # location fall back to <multiqc_dir>/<report>.html, so a report left by the
    # previous scenario would be exposed by a failed or report-less run. A fresh
    # project has an empty results tree; each failure-mode scenario does too.
    rm -rf "${P}/results"
    echo '{"mode": "noreport", "delay": 0.01}' > "${CTRL}"

    code="$(post_run_retrying_busy "${body}" "${WORK}/run-noreport-response.json")"
    if ! run_accepted_or_skip "noreport run" "${code}" "${WORK}/run-noreport-response.json"; then
        return 0
    fi
    if wait_run_state_t 120 failed done; then pass "noreport run reaches a terminal state"; else fail "noreport run never reached a terminal state"; fi
    expect_eq "noreport run exit code was 0 but the state is failed, not done" "failed" "$(state_field run.state)"
    expect_eq "noreport reason is report_missing" "report_missing" "$(state_field run.failed.reason)"
    expect_eq "noreport run exit_code is 0" "0" "$(state_field run.exit_code)"
    expect_eq "noreport run has no report" "" "$(state_field run.report.path)"
    code="$(get_api "/api/report" "${WORK}/report-404.json")"
    expect_eq "GET /api/report with no located report -> 404" "404" "${code}"
    expect_eq "404 error code is no_report" "no_report" "$(json_get "${WORK}/report-404.json" error)"
}

# ===========================================================================
# 7. cancel: process group gone, state cancelled, log kept (§13.12)
# ===========================================================================
phase_cancel_run() {
    banner "cancel: SIGTERM process group, state=cancelled, log kept (§4, §7, §13.12)"
    local P="${P_FAKE}"
    local body="${WORK}/body-slow.json" code run_pid logf i kids
    mk_body "${body}" fresh 0 "" \
        "SR-A" "${P}/data/raw/a_1.fastq.gz" "${P}/data/raw/a_2.fastq.gz" "" "" "" \
        "SR-B" "${P}/data/raw/b_1.fastq.gz" "${P}/data/raw/b_2.fastq.gz" "" "" ""
    # Isolate the results tree for this scenario. docs/ui.md §9 lets report
    # location fall back to <multiqc_dir>/<report>.html, so a report left by the
    # previous scenario would be exposed by a failed or report-less run. A fresh
    # project has an empty results tree; each failure-mode scenario does too.
    rm -rf "${P}/results"
    echo '{"mode": "slow", "delay": 0.05}' > "${CTRL}"

    code="$(post_run_retrying_busy "${body}" "${WORK}/run-slow-response.json")"
    if ! run_accepted_or_skip "cancel run" "${code}" "${WORK}/run-slow-response.json"; then
        return 0
    fi
    if wait_run_state_t 60 running; then pass "slow run becomes running"; else fail "slow run never became running"; fi
    sleep 0.7
    get_state
    run_pid="$(state_field run.pid)"
    logf="$(state_field run.log)"
    if [ -n "${run_pid}" ]; then pass "run.pid is reported while running"; else fail "run.pid is missing while running"; fi
    if [ -n "${run_pid}" ] && kill -0 "${run_pid}" 2>/dev/null; then pass "engine process is alive before cancel"; else fail "engine process is not alive before cancel"; fi
    if [ -n "${run_pid}" ] && server_children "${CUR_SERVER}" | grep -qx "${run_pid}"; then
        pass "engine is a child of the UI server"
    else
        fail "engine pid '${run_pid}' is not a child of the UI server"
    fi
    if [ -n "${run_pid}" ]; then
        expect_eq "engine runs in its own process group (start_new_session)" \
            "${run_pid}" "$(python3 -c 'import os,sys; print(os.getpgid(int(sys.argv[1])))' "${run_pid}" 2>/dev/null)"
    fi
    if [ -n "${logf}" ] && grep -qF 'Submitted process' "${logf}"; then pass "run.log is being written before cancel"; else fail "run.log has no Submitted line before cancel"; fi

    code="$(post_api "/api/cancel" "${WORK}/empty.json" "${WORK}/cancel-response.json")"
    expect_eq "POST /api/cancel -> 200" "200" "${code}"
    expect_eq "cancel response says cancel_requested=true" "true" "$(json_get "${WORK}/cancel-response.json" cancel_requested)"
    if wait_run_state_t 30 cancelled failed; then pass "cancel run reaches a terminal state"; else fail "cancel run never reached a terminal state"; fi
    expect_eq "state is cancelled, not failed" "cancelled" "$(state_field run.state)"

    i=0
    while [ "${i}" -lt 120 ] && [ -n "${run_pid}" ] && kill -0 "${run_pid}" 2>/dev/null; do
        sleep 0.1
        i=$((i + 1))
    done
    if [ -n "${run_pid}" ] && kill -0 "${run_pid}" 2>/dev/null; then
        fail "cancel did not terminate the engine process (pid ${run_pid} still alive after 12 s)"
    else
        pass "engine process (and its group) is gone after cancel"
    fi
    if [ -n "${run_pid}" ] && [ -n "$(pgrep -P "${run_pid}" 2>/dev/null)" ]; then
        fail "children of the cancelled engine survived: $(pgrep -P "${run_pid}" | tr '\n' ' ')"
    else
        pass "no child of the cancelled engine survived"
    fi
    kids="$(server_children "${CUR_SERVER}")"
    if [ -z "${kids}" ]; then
        pass "UI server has no engine child left after cancel"
    else
        fail "UI server still has child process(es) after cancel: ${kids}"
    fi
    if [ -n "${logf}" ] && [ -s "${logf}" ]; then pass "run.log is preserved after cancel"; else fail "run.log was lost after cancel"; fi
    if grep -qF '=== run ' "${logf}"; then pass "preserved run.log keeps its run header"; else fail "preserved run.log lost its run header"; fi
    expect_eq "cancelled run has no report" "" "$(state_field run.report.path)"
}

# ===========================================================================
# 8. stale state.json -> interrupted (§13.14)
# ===========================================================================
phase_stale_state() {
    banner "stale state.json (dead pid) -> interrupted on startup (§4, §13.14)"
    stop_ui
    local ui_dir="${P_FAKE}/.nextflow/ui"
    local dead_pid
    ( sleep 0 ) &
    dead_pid=$!
    wait "${dead_pid}" 2>/dev/null
    if kill -0 "${dead_pid}" 2>/dev/null; then
        fail "could not obtain a dead pid for the stale-state fixture"
        return 1
    fi
    mkdir -p "${ui_dir}/runs/stale-run"
    : > "${ui_dir}/runs/stale-run/samples.tsv"
    : > "${ui_dir}/runs/stale-run/run.log"
    python3 - "${ui_dir}/state.json" "${dead_pid}" "${ui_dir}/runs/stale-run" <<'PY'
import json, sys, time
path, pid, run_dir = sys.argv[1], int(sys.argv[2]), sys.argv[3]
run = {
    "id": "stale-run", "state": "running", "mode": "resume", "clean_requested": False,
    "pid": pid, "since_pid_alive": True, "started_at": time.time() - 3600,
    "finished_at": None, "elapsed_seconds": 3600.0, "exit_code": None,
    "cancel_requested": False, "sheet": run_dir + "/samples.tsv",
    "log": run_dir + "/run.log", "log_bytes": 0, "samples": ["STALE"],
    "argv": ["nextflow", "run", ".", "--samples_tsv", run_dir + "/samples.tsv"],
    "tasks": {"submitted": 1, "completed": 0, "failed": 0, "expected": 13},
    "steps": [], "failed": None, "report": None,
}
json.dump({"run": run}, open(path, "w", encoding="utf-8"), indent=1)
PY
    if ! start_ui "${P_FAKE}" "${LOG_DIR}/ui-03-stale.log" --nextflow "${FAKE}"; then
        fail "launcher failed to restart over a stale state.json"
        return 1
    fi
    if wait_run_state_t 30 interrupted running; then pass "startup resolves the stale run"; else fail "stale state was not resolved at startup"; fi
    expect_eq "stale run with a dead pid becomes interrupted" "interrupted" "$(state_field run.state)"
    expect_eq "stale run keeps its run id" "stale-run" "$(state_field run.id)"
    if [ -n "$(state_field run.failed.reason)" ] || [ -n "$(state_field run.message)" ]; then
        pass "interrupted run carries a reason/message"
    else
        fail "interrupted run has no reason/message explaining the interruption"
    fi
}

# ===========================================================================
# 9. attach after the UI dies while a run is alive (§13.15)
# ===========================================================================
phase_attach() {
    banner "attach: kill the UI mid-run, restart -> same running run (§4, §13.15)"
    local P="${P_FAKE}"
    local body="${WORK}/body-attach.json" code run_id run_pid logf l0 l1 server_pid launcher i
    mk_body "${body}" fresh 0 "" \
        "SR-A" "${P}/data/raw/a_1.fastq.gz" "${P}/data/raw/a_2.fastq.gz" "" "" ""
    echo '{"mode": "slow", "delay": 0.05}' > "${CTRL}"

    code="$(post_run_retrying_busy "${body}" "${WORK}/run-attach-response.json")"
    if ! run_accepted_or_skip "attach run" "${code}" "${WORK}/run-attach-response.json"; then
        return 0
    fi
    if wait_run_state_t 60 running; then pass "attach run becomes running"; else fail "attach run never became running"; fi
    sleep 0.8
    get_state
    run_id="$(state_field run.id)"
    run_pid="$(state_field run.pid)"
    logf="$(state_field run.log)"
    l0="$(state_field run.log_bytes)"

    server_pid="${CUR_SERVER:-}"
    launcher="${CUR_LAUNCHER}"
    if [ -n "${server_pid}" ] && kill -0 "${server_pid}" 2>/dev/null; then
        pass "server pid is alive before the simulated UI crash"
    else
        fail "server pid is missing/dead before the crash simulation"
    fi
    # SIGKILL, not SIGTERM: the contract's graceful path cancels the active run,
    # and attach is about the ungraceful path.
    kill -KILL "${launcher}" 2>/dev/null
    [ -n "${server_pid}" ] && kill -KILL "${server_pid}" 2>/dev/null
    sleep 0.6
    if kill -0 "${launcher}" 2>/dev/null; then fail "launcher survived SIGKILL"; else pass "launcher is dead after SIGKILL (simulated crash)"; fi
    if [ -n "${server_pid}" ] && kill -0 "${server_pid}" 2>/dev/null; then
        fail "server survived SIGKILL"
    else
        pass "server is dead after SIGKILL (simulated crash)"
    fi
    CUR_LAUNCHER=""
    CUR_SERVER=""
    if [ -n "${run_pid}" ] && kill -0 "${run_pid}" 2>/dev/null; then
        pass "engine keeps running after the UI dies (start_new_session)"
    else
        fail "engine died together with the UI (should be in its own session)"
    fi
    l1="${l0}"

    if ! start_ui "${P}" "${LOG_DIR}/ui-04-attach.log" --nextflow "${FAKE}"; then
        fail "launcher failed to restart for attach"
        return 1
    fi
    local ok=0 i2=0
    while [ "${i2}" -lt 300 ]; do
        get_state
        if [ "$(state_field run.state)" = "running" ] && [ "$(state_field run.id)" = "${run_id}" ]; then
            ok=1
            break
        fi
        sleep 0.1
        i2=$((i2 + 1))
    done
    if [ "${ok}" = "1" ]; then pass "restart attaches to the live run (same run id, state running)"; else fail "restart did not attach: state=$(state_field run.state) id=$(state_field run.id)"; fi
    sleep 1.2
    get_state
    l1="$(state_field run.log_bytes)"
    if [ -n "${l0}" ] && [ -n "${l1}" ] && [ "${l1}" -gt "${l0}" ]; then
        pass "attached server keeps reading the run log (log_bytes ${l0} -> ${l1})"
    else
        fail "attached run log did not grow (log_bytes ${l0} -> ${l1})"
    fi
    expect_eq "attached run keeps the same engine pid" "${run_pid}" "$(state_field run.pid)"
    if wait_run_state_t 10 running; then
        # Cancel of an attached run (Lead-approved extension of §7/§13.15): the
        # restarted server has no Popen handle, so it signals the surviving
        # process group directly. The terminal state is cancelled (never failed/
        # interrupted), exit_code stays null, and run.log remains downloadable.
        code="$(post_api "/api/cancel" "${WORK}/empty.json" "${WORK}/cancel-attach.json")"
        expect_eq "POST /api/cancel on the attached run -> 200" "200" "${code}"
        expect_eq "attached-run cancel response says cancel_requested=true" \
            "true" "$(json_get "${WORK}/cancel-attach.json" cancel_requested)"
        if wait_run_state_t 30 cancelled failed interrupted; then
            pass "attached run reaches a terminal state after cancel"
        else
            fail "attached run did not terminate after cancel (state=$(state_field run.state))"
        fi
        expect_eq "attached run state is cancelled, not failed" "cancelled" "$(state_field run.state)"
        expect_eq "attached run has no exit code (no Popen handle)" "" "$(state_field run.exit_code)"
        expect_eq "attached cancelled run has no failure payload" "" "$(state_field run.failed)"
        i=0
        while [ "${i}" -lt 120 ] && kill -0 "${run_pid}" 2>/dev/null; do
            sleep 0.1
            i=$((i + 1))
        done
        if kill -0 "${run_pid}" 2>/dev/null; then
            fail "attached engine process group survived cancel (pid ${run_pid})"
        else
            pass "attached engine process group is gone after cancel"
        fi
        if [ -n "${logf}" ] && [ -s "${logf}" ] && grep -qF 'Submitted process' "${logf}"; then
            pass "attached run.log is preserved after cancel"
        else
            fail "attached run.log was lost or emptied by cancel"
        fi
        code="$(get_api "/api/download?what=log" "${WORK}/attach-log-dl.log")"
        expect_eq "GET /api/download?what=log after attach-cancel -> 200" "200" "${code}"
        if cmp -s "${WORK}/attach-log-dl.log" "${logf}"; then
            pass "attached run.log downloads byte-identically after cancel"
        else
            fail "attached run.log download differs from the preserved file"
        fi
        code="$(post_api "/api/cancel" "${WORK}/empty.json" "${WORK}/cancel-attach2.json")"
        expect_eq "second POST /api/cancel on the attached run -> 409" "409" "${code}"
        expect_eq "second cancel error is no_active_run" "no_active_run" "$(json_get "${WORK}/cancel-attach2.json" error)"
    else
        fail "attached run disappeared before it could be cancelled"
    fi
    stop_ui
}

# ===========================================================================
# 10. missing nextflow: preflight reports fail, server does not crash (§13.4)
# ===========================================================================
phase_missing_nextflow() {
    banner "missing nextflow: preflight fail, server stays up (§3, §7.1, §13.4)"
    local code ids
    if ! start_ui "${P_NONF}" "${LOG_DIR}/ui-05-nonf.log" --nextflow /nonexistent/nextflow-does-not-exist; then
        fail "launcher refused to start with --nextflow /nonexistent (contract says it must not)"
        return 0
    fi
    pass "launcher starts even when the engine path does not exist"
    if wait_api_ready "/api/preflight" 40; then pass "preflight still answers with a missing nextflow"; else fail "preflight did not answer with a missing nextflow"; fi
    get_api "/api/preflight" "${WORK}/preflight-missing.json" >/dev/null
    expect_eq "preflight nextflow check is fail" "fail" "$(preflight_status "${WORK}/preflight-missing.json" nextflow)"
    code="$(get_api "/api/state" "${WORK}/state-nonf.json")"
    expect_eq "GET /api/state still 200 (server did not crash)" "200" "${code}"
    code="$(curl -sS --max-time 20 -o "${WORK}/index-nonf.html" -w '%{http_code}' "${BASE}/?token=${TOKEN}")"
    expect_eq "GET / with a missing nextflow -> 200" "200" "${code}"
    if kill -0 "${CUR_LAUNCHER}" 2>/dev/null; then pass "server is still running after the failed preflight"; else fail "server exited after the failed preflight"; fi
    stop_ui
}

# ===========================================================================
# 11. real integration: complete stub DAG driven through the UI, in a COPY
#     (§13 real-integration paragraph; F18)
# ===========================================================================
phase_real_stub() {
    banner "real integration: stub DAG through the UI in a temp project copy (§13)"
    if [ "${UI_CHECK_REAL_STUB:-1}" = "0" ]; then
        skip "real stub integration (disabled with UI_CHECK_REAL_STUB=0)"
        return 0
    fi
    local busy
    busy="$(external_engine_procs)"
    if [ -n "${busy}" ]; then
        skip "real stub integration -- external engine-like process present (would make POST /api/run 409 busy, §7.2): $(printf '%s' "${busy}" | head -2 | tr '\n' ';')"
        return 0
    fi
    local nf
    nf="$(nextflow_bin)"
    if [ -z "${nf}" ]; then
        skip "real stub integration -- nextflow not found (CI MUST run this: install Nextflow >= 25.10)"
        skip "real failure-injection parse -- nextflow not found"
        return 0
    fi
    banner "real integration: engine = ${nf}"
    P_REAL="$(cd "${WORK}/project-real" && pwd -P)"
    local P="${P_REAL}"
    local body="${WORK}/body-real.json"
    mk_body "${body}" fresh 0 "" \
        "CI_A" "${P}/tests/ci/fixtures/raw/CI_A_1.fastq.gz" "${P}/tests/ci/fixtures/raw/CI_A_2.fastq.gz" "" "" "${P}/tests/ci/fixtures/reference/CI_A.fasta" \
        "CI_B" "${P}/tests/ci/fixtures/raw/CI_B_1.fastq.gz" "${P}/tests/ci/fixtures/raw/CI_B_2.fastq.gz" "" "" ""

    if ! start_ui "${P}" "${LOG_DIR}/ui-06-real.log" --nextflow "${nf}" \
        --nextflow-arg=-c --nextflow-arg=tests/ci/ci.config --nextflow-arg=-stub-run; then
        fail "launcher failed to start for the real stub integration"
        return 0
    fi
    local code
    code="$(post_run_retrying_busy "${body}" "${WORK}/run-real-response.json")"
    if ! run_accepted_or_skip "real stub DAG" "${code}" "${WORK}/run-real-response.json"; then
        stop_ui
        return 0
    fi
    if wait_run_state_t 300 done failed interrupted; then pass "real stub run reaches a terminal state"; else fail "real stub run never terminated (state=$(state_field run.state))"; fi
    expect_eq "real stub run state is done" "done" "$(state_field run.state)"
    expect_eq "real stub run exit_code is 0" "0" "$(state_field run.exit_code)"
    expect_eq "real stub task count is 25 (2 samples x 11 + growth + MultiQC + 1 ref)" "25" "$(state_field run.tasks.submitted)"
    expect_eq "real stub expected task count is 25 (11*N + refs + 2)" "25" "$(state_field run.tasks.expected)"
    expect_eq "real stub has no failed task" "0" "$(state_field run.tasks.failed)"
    expect_eq "real stub completed task count is 25 (parsed from .nextflow.log, F48)" \
        "25" "$(state_field run.tasks.completed)"
    expect_eq "real stub tasks.completed == tasks.submitted" \
        "$(state_field run.tasks.submitted)" "$(state_field run.tasks.completed)"

    local steps
    steps="$(python3 - "${WORK}/state.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], "r", encoding="utf-8"))
run = d.get("run") or {}
print(",".join(str(s.get("process")) for s in (run.get("steps") or [])))
PY
)"
    case ",${steps}," in
        *,FASTQC_RAW,*) pass "task ladder includes FASTQC_RAW" ;;
        *) fail "task ladder lacks FASTQC_RAW (got ${steps})" ;;
    esac
    case ",${steps}," in
        *,SPADES,*) pass "task ladder includes SPADES" ;;
        *) fail "task ladder lacks SPADES (got ${steps})" ;;
    esac
    case ",${steps}," in
        *,MULTIQC,*) pass "task ladder includes MULTIQC" ;;
        *) fail "task ladder lacks MULTIQC (got ${steps})" ;;
    esac

    local rpath
    rpath="$(state_field run.report.path)"
    if [ -n "${rpath}" ]; then pass "real stub run located a report"; else fail "real stub run did not locate a report (report_missing)"; fi
    if [ -n "${rpath}" ] && [ -f "${rpath}" ]; then pass "located report exists on disk"; else fail "located report does not exist: '${rpath}'"; fi
    if [ -f "${rpath}" ]; then
        expect_eq "real stub report size field == file size" "$(wc -c < "${rpath}" | tr -d ' ')" "$(state_field run.report.size)"
        if [ "$(wc -c < "${rpath}" | tr -d ' ')" -gt 0 ]; then
            pass "real stub report is non-empty"
        else
            pass "real stub report recorded (0 bytes is expected: MULTIQC's stub: block touches an empty file, §13 asks for the report to be *located*)"
        fi
        code="$(get_api "/api/report" "${WORK}/real-report.html")"
        expect_eq "GET /api/report for the real stub report -> 200" "200" "${code}"
        if cmp -s "${WORK}/real-report.html" "${rpath}"; then pass "real stub /api/report bytes == located report"; else fail "real stub /api/report differs from the report file"; fi
    fi
    if grep -qF "${rpath}" "$(state_field run.log)" 2>/dev/null; then
        pass "report path came from the engine's onComplete line (D3)"
    else
        fail "run.log does not contain the onComplete report line for '${rpath}'"
    fi
    expect_file "real stub published the MultiQC report in the CI out tree" \
        "${P}/tests/ci/out/results/report/multiqc_report.html"
    stop_ui
}

phase_real_failure() {
    local nf
    nf="$(nextflow_bin)"
    if [ -z "${nf}" ]; then
        skip "real failure-injection parse -- nextflow not found (CI MUST run this)"
        return 0
    fi
    if [ "${UI_CHECK_REAL_FAILURE:-1}" = "0" ]; then
        skip "real failure-injection parse (disabled with UI_CHECK_REAL_FAILURE=0)"
        return 0
    fi
    local busy
    busy="$(external_engine_procs)"
    if [ -n "${busy}" ]; then
        skip "real failure-injection parse -- external engine-like process present (would make POST /api/run 409 busy, §7.2): $(printf '%s' "${busy}" | head -2 | tr '\n' ';')"
        return 0
    fi
    banner "real integration: injected FASTQC failure parsed into the failure panel (§13 optional)"
    P_REAL2="$(cd "${WORK}/project-real-fail" && pwd -P)"
    local P="${P_REAL2}"
    local body="${WORK}/body-real-fail.json" code
    mk_body "${body}" fresh 0 "" \
        "CI_A" "${P}/tests/ci/fixtures/raw/CI_A_1.fastq.gz" "${P}/tests/ci/fixtures/raw/CI_A_2.fastq.gz" "" "" "" \
        "CI_B" "${P}/tests/ci/fixtures/raw/CI_B_1.fastq.gz" "${P}/tests/ci/fixtures/raw/CI_B_2.fastq.gz" "" "" ""
    if ! start_ui "${P}" "${LOG_DIR}/ui-07-real-fail.log" --nextflow "${nf}" \
        --nextflow-arg=-c --nextflow-arg=tests/ci/ci.config \
        --nextflow-arg=-c --nextflow-arg=tests/ci/inject-sample-failure.config \
        --nextflow-arg=-stub-run; then
        fail "launcher failed to start for the real failure injection"
        return 0
    fi
    code="$(post_run_retrying_busy "${body}" "${WORK}/run-real-fail-response.json")"
    if ! run_accepted_or_skip "injected failure" "${code}" "${WORK}/run-real-fail-response.json"; then
        stop_ui
        return 0
    fi
    if wait_run_state_t 300 failed done interrupted cancelled; then pass "injected-failure run reaches a terminal state"; else fail "injected-failure run never terminated"; fi
    expect_eq "injected-failure run state is failed" "failed" "$(state_field run.state)"
    local proc tag
    proc="$(state_field run.failed.process)"
    tag="$(state_field run.failed.tag)"
    case "${proc}" in
        FASTQC_RAW|TRIMMOMATIC) pass "failure panel names the injected process (${proc})" ;;
        *) fail "failure panel process is '${proc}', expected FASTQC_RAW or TRIMMOMATIC" ;;
    esac
    case "${tag}" in
        CI_B*) pass "failure panel names the injected sample (${tag})" ;;
        *) fail "failure panel tag is '${tag}', expected CI_B_* or CI_B" ;;
    esac
    expect_eq "injected-failure exit_code is 1" "1" "$(state_field run.failed.exit_code)"
    if [ -n "$(state_field run.failed.stderr_tail)" ]; then
        pass "injected failure has a parsed stderr_tail"
    else
        fail "injected failure has no stderr_tail"
    fi
    if grep -qF 'Error executing process' "$(state_field run.log)"; then
        pass "real engine error block is in run.log"
    else
        fail "real engine error block missing from run.log"
    fi
    stop_ui
}

# ===========================================================================
# 12. F18: the repository's validated tree never changed
# ===========================================================================
phase_f18() {
    banner "F18: repository results/ + data/ untouched by every run above"
    if [ ! -d "${REPO_RESULTS}" ]; then
        skip "F18 results/ mtime -- ${REPO_RESULTS} does not exist"
        return 0
    fi
    local mtime now touched
    mtime="$(python3 -c 'import os,sys; print(os.stat(sys.argv[1]).st_mtime_ns)' "${REPO_RESULTS}")"
    expect_eq "repository results/ directory mtime unchanged" "${RESULTS_MTIME_NS}" "${mtime}"
    touched="$(files_newer_than "${F18_MARKER}" "${REPO_RESULTS}" "${REPO_DATA}")"
    expect_empty "no repository results/ or data/ file newer than the run marker" "${touched}"
    expect_eq "repository samples.tsv is byte-identical (UI never rewrites it)" "${REPO_SAMPLES_SHA}" "$(sha256_of "${REPO_SAMPLES}")"
    # A UI launched with an empty --project would fall back to its cwd (the
    # repository) and write .nextflow/ui/server.json here. That is exactly the
    # bug this assertion caught once, so it stays -- but it must be attributable:
    # another agent or a developer may have a UI running against this checkout.
    local repo_server="${PROJECT_DIR}/.nextflow/ui/server.json"
    if [ -f "${repo_server}" ]; then
        touched="$(files_newer_than "${F18_MARKER}" "${repo_server}")"
        if [ -z "${touched}" ]; then
            pass "repository .nextflow/ui/server.json was not written by the checks"
        else
            local rp
            rp="$(json_get "${repo_server}" pid)"
            case " ${OUR_SERVERS} " in
                *" ${rp} "*)
                    fail "one of this check's UI launches wrote the repository's .nextflow/ui (server pid ${rp})"
                    ;;
                *)
                    skip "repository .nextflow/ui/server.json was written by another UI process (pid ${rp:-?}), not by this check"
                    ;;
            esac
        fi
    else
        pass "repository .nextflow/ui/server.json was not created by the checks"
    fi
}

# ===========================================================================
# main
# ===========================================================================

# The fixed token the launcher is asked to use; if it ignores --token the test
# reports the mismatch and keeps using whatever it printed.
TOKEN_ARG="uitest-$(date +%s)-$$"
export NXF_HOME="${WORK}/nxhome"
export NXF_ANSI_LOG=false

printf '== ui check: docs/ui.md §13 ==\n'
printf '  workspace: %s\n' "${WORK}"

RESULTS_MTIME_NS="$(python3 -c 'import os,sys; print(os.stat(sys.argv[1]).st_mtime_ns)' "${REPO_RESULTS}" 2>/dev/null || echo '')"
REPO_SAMPLES_SHA="$(sha256_of "${REPO_SAMPLES}")"
touch_f18_marker

static_checks

if [ ! -x "${UI_BIN}" ]; then
    fail "bin/pipeline-ui is missing or not executable -- the UI implementation is required for §13"
fi
if [ ! -f "${SERVER_PY}" ]; then
    fail "ui/server.py is missing -- the UI implementation is required for §13"
fi
if [ "${FAILURES}" -ne 0 ]; then
    printf '\n  %d skipped assertion(s) (UI not runnable)\n  elapsed: %ss\n' "${SKIPS}" "$((SECONDS - START_SECONDS))"
    summary "run-ui-check"
fi

make_fake_project "${WORK}/project-fake"
make_fake_project "${WORK}/project-nonf"
mkdir -p "${WORK}/project-real" "${WORK}/project-real-fail"
make_real_project "${WORK}/project-real"
make_real_project "${WORK}/project-real-fail"
# Explicit, canonical paths: a start_ui with an empty --project would make the
# launcher fall back to its cwd (the repository) and write state there.
P_FAKE="$(cd "${WORK}/project-fake" && pwd -P)"
P_NONF="$(cd "${WORK}/project-nonf" && pwd -P)"

phase_busy_detection || true
phase_startup_auth_preflight || true
phase_validation || true
phase_single_instance || true
phase_success_run || true
phase_engine_log_run || true
phase_failed_run || true
phase_noreport_run || true
phase_cancel_run || true
phase_stale_state || true
phase_attach || true
phase_missing_nextflow || true
phase_real_stub || true
phase_real_failure || true
phase_f18 || true

stop_ui

printf '\n  %d assertion(s) skipped\n  elapsed: %ss\n' "${SKIPS}" "$((SECONDS - START_SECONDS))"
summary "run-ui-check"
