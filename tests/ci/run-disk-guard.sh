#!/usr/bin/env bash
# ============================================================
# Disk pre-flight guard (F20).
#
# Before this guard existed, README.md only *reminded* the operator about free
# space. The measured failure mode when the reminder was ignored: SPAdes' own
# `gzip -f -7` died mid read-correction with OS return 1 at ~2 GB free, and a
# near-full volume inflated individual steps up to 17x.
#
# Three branches, each deterministic regardless of how much space the runner
# happens to have:
#   low threshold  -> passes and reports the resolved filesystem
#   impossible     -> aborts BEFORE any task, names the shortfall, hints the fix
#   threshold 0    -> skipped entirely (documented escape hatch)
#
# Local usage:  bash tests/ci/run-disk-guard.sh
# ============================================================

# shellcheck source=tests/ci/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
require_nextflow

rm -rf "${CI_OUT}"

echo "== disk guard: threshold satisfied =="
log="$(mktemp)"
run_nf "${log}" run . -preview --min_free_disk_gb=1
expect_eq "run proceeds at 1 GiB floor" "0" "$?"
expect_grep "resolved a real filesystem" "[disk-check]" "${log}"
expect_grep "reported free space in GiB" "GiB free of" "${log}"

echo "== disk guard: threshold unmet =="
log2="$(mktemp)"
run_nf "${log2}" run . -stub-run --min_free_disk_gb=999999
status=$?
expect_eq "run aborts (exit 1)" "1" "${status}"
expect_grep   "explains the shortfall"      "GiB free < 999999" "${log2}"
expect_grep   "refuses to start"            "refusing to start" "${log2}"
expect_grep   "names the work cache"        "Nextflow task cache" "${log2}"
expect_grep   "names the conda cache"       "conda environments" "${log2}"
expect_grep   "documents the override"      "--min_free_disk_gb" "${log2}"
expect_nogrep "no task was submitted"       "Submitted process" "${log2}"
expect_absent "no report was written"       "${CI_OUT}/results/report/multiqc_report.html"

echo "== disk guard: disabled =="
log3="$(mktemp)"
run_nf "${log3}" run . -preview --min_free_disk_gb=0
expect_eq "run proceeds with the check disabled" "0" "$?"
expect_nogrep "check stayed silent" "[disk-check]" "${log3}"

rm -f "${log}" "${log2}" "${log3}"
summary "run-disk-guard"
