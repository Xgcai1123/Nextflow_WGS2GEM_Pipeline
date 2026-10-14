#!/usr/bin/env bash
# ============================================================
# Shared helpers for the tests/ci checks. Sourced, never executed.
#
# The GitHub workflow (.github/workflows/ci.yml) only installs Nextflow and
# calls these scripts, so `bash tests/ci/run-*.sh` locally and CI execute the
# exact same assertions -- they cannot drift apart.
#
# Contract: each run-*.sh sources this file, accumulates assertions, and ends
# with `summary <name>`.
# ============================================================

set -uo pipefail

CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${CI_DIR}/../.." && pwd)"
CI_OUT="${PROJECT_DIR}/tests/ci/out"
CI_CONFIG="${CI_DIR}/ci.config"

# Colour only on a terminal, so CI logs stay plain and greppable.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_OFF=$'\033[0m'
else
    C_OK=''; C_BAD=''; C_OFF=''
fi

FAILURES=0
pass() { printf '  %sok%s   %s\n' "${C_OK}" "${C_OFF}" "$1"; }
fail() { printf '  %sFAIL%s %s\n' "${C_BAD}" "${C_OFF}" "$1"; FAILURES=$((FAILURES + 1)); }

expect_eq()     { [ "$2" = "$3" ] && pass "$1: '$3'" || fail "$1: expected '$2', got '$3'"; }
expect_file()   { [ -f "$2" ] && pass "$1" || fail "$1 -- missing file ${2}"; }
expect_absent() { [ ! -e "$2" ] && pass "$1" || fail "$1 -- unexpected ${2}"; }
expect_grep()   { grep -qF -- "$2" "$3" && pass "$1" || fail "$1 -- '$2' not found in ${3}"; }
expect_nogrep() { grep -qF -- "$2" "$3" && fail "$1 -- '$2' unexpectedly found in ${3}" || pass "$1"; }
expect_empty()  { [ -z "$2" ] && pass "$1" || fail "$1 -- unexpected output: $(printf '%s' "$2" | head -3)"; }

require_nextflow() {
    command -v nextflow >/dev/null 2>&1 && return 0
    echo "nextflow is not on PATH -- install Nextflow >= 25.10 (see README.md)" >&2
    exit 2
}

# nextflow + the CI config overlay(s). NF_CONFIGS holds extra config files
# (space separated). NXF_HOME stays inside the project so a check never writes
# to $HOME, and plain ANSI-free logs keep the greps stable everywhere.
nf() {
    local args=(-c "${CI_CONFIG}") c
    for c in ${NF_CONFIGS:-}; do args+=(-c "$c"); done
    NXF_HOME="${NXF_HOME:-${PROJECT_DIR}/.nxhome}" \
    NXF_ANSI_LOG="${NXF_ANSI_LOG:-false}" \
        nextflow "${args[@]}" "$@"
}

# run_nf <logfile> <nextflow args...> -- never aborts the script; use $?.
run_nf() {
    local log="$1"; shift
    nf "$@" >"${log}" 2>&1
}

nf_version() {
    NXF_HOME="${NXF_HOME:-${PROJECT_DIR}/.nxhome}" NXF_ANSI_LOG=false \
        nextflow -v 2>&1 | tail -1
}

# files_newer_than <marker-file> <dirs...> -- files touched after the marker.
# Used to prove a stub run did not rewrite the validated output tree (F18).
files_newer_than() {
    local marker="$1"; shift
    find "$@" -type f -newer "${marker}" 2>/dev/null
}

summary() {
    local name="$1"
    printf '\n'
    if [ "${FAILURES}" -eq 0 ]; then
        printf '%s: all checks passed\n' "${name}"
        exit 0
    fi
    printf '%s: %d check(s) FAILED\n' "${name}" "${FAILURES}"
    exit 1
}
