#!/usr/bin/env bash
# ============================================================
# Single-sample failure isolation -- injected, not asserted from configuration.
#
# The port originally claimed `process.errorStrategy = 'finish'` was the
# equivalent of Snakemake's `--keep-going`, and CHANGELOG.md carried "has not
# been exercised with an injected failing sample" as a known limitation. This
# check injects a real task failure and measures what actually happens. The
# measured semantics -- which the docs now state instead of the old claim --
# are:
#
#   1. a failing task fails the RUN loudly (exit 1); it is never swallowed;
#   2. the failed sample yields no downstream deliverables, so no partial or
#      half-written model can be mistaken for a result;
#   3. work already completed for the healthy sample is preserved, and
#      `-resume` converges without recomputing it;
#   4. independent branches are NOT continued in the same run: Nextflow's
#      `finish` waits for running tasks and then stops. `ignore` is the only
#      strategy that continues independent branches, and it exits 0 -- it is
#      rejected on purpose (see ignore-strategy.config), which this script
#      re-measures as a negative control.
#
# Local usage:  bash tests/ci/run-failure-isolation.sh
# ============================================================

# shellcheck source=tests/ci/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
require_nextflow

INJECT_CONFIG="${CI_DIR}/inject-sample-failure.config"
FAIL_SAMPLE="${CI_FAIL_SAMPLE:-CI_B}"
OK_SAMPLE="${CI_OK_SAMPLE:-CI_A}"

echo "== negative control: errorStrategy=ignore swallows the failure =="

rm -rf "${CI_OUT}"
log_ignore="$(mktemp)"
NF_CONFIGS="${INJECT_CONFIG} ${CI_DIR}/ignore-strategy.config" \
    run_nf "${log_ignore}" run . -stub-run --min_free_disk_gb=1
status_ignore=$?
expect_eq "ignore exits 0 despite the dead task" "0" "${status_ignore}"
expect_grep "the dead task is only a NOTE" "Error is ignored" "${log_ignore}"
expect_absent "and the hole stays in the outputs" "${CI_OUT}/results/qc/fastqc_raw/${FAIL_SAMPLE}_1_fastqc.zip"

echo "== run 1: inject a task failure for ${FAIL_SAMPLE} (finish) =="

rm -rf "${CI_OUT}"
log1="$(mktemp)"
NF_CONFIGS="${INJECT_CONFIG}" run_nf "${log1}" run . -stub-run --min_free_disk_gb=1
status1=$?

expect_eq "failing run exits 1" "1" "${status1}"
expect_grep "the injected failure is visible in the log" "[failure-injection] deliberately failing" "${log1}"
expect_grep "the failed task is named" "FASTQC_RAW (${FAIL_SAMPLE}" "${log1}"
expect_grep "engine reports the task error" "terminated with an error exit status" "${log1}"

echo "  -- no deliverables for the failed sample (no partial results) --"
for d in "results/assembly/${FAIL_SAMPLE}" \
         "results/qc/quast/${FAIL_SAMPLE}" \
         "results/qc/busco/${FAIL_SAMPLE}" \
         "results/annotation/${FAIL_SAMPLE}" \
         "results/models/carveme/${FAIL_SAMPLE}" \
         "results/models/memote/${FAIL_SAMPLE}"; do
    expect_absent "absent: ${d}" "${CI_OUT}/${d}"
done
expect_absent "no aggregate report claiming success" "${CI_OUT}/results/report/multiqc_report.html"

echo "  -- work already finished for the healthy sample survives --"
expect_file "preserved: ${OK_SAMPLE} raw QC" "${CI_OUT}/results/qc/fastqc_raw/${OK_SAMPLE}_1_fastqc.zip"

if [ "${status1}" -ne 0 ] && [ "${status1}" -ne 1 ]; then
    echo "  ---- nextflow output ----"
    tail -30 "${log1}" | sed 's/^/    /'
fi

echo "== run 2: -resume with the injection removed =="

log2="$(mktemp)"
run_nf "${log2}" run . -stub-run --min_free_disk_gb=1 -resume
status2=$?

expect_eq "-resume exits 0" "0" "${status2}"
expect_grep "-resume reuses ${OK_SAMPLE} work instead of recomputing it" \
    "Cached process > FASTQC_RAW (${OK_SAMPLE}_1)" "${log2}"
expect_nogrep "-resume does not resubmit that task" "Submitted process > FASTQC_RAW (${OK_SAMPLE}_1)" "${log2}"
for s in "${OK_SAMPLE}" "${FAIL_SAMPLE}"; do
    expect_file "recovered: ${s} assembly"       "${CI_OUT}/results/assembly/${s}/contigs.fasta"
    expect_file "recovered: ${s} model"          "${CI_OUT}/results/models/carveme/${s}/${s}.xml"
    expect_file "recovered: ${s} MEMOTE report"  "${CI_OUT}/results/models/memote/${s}/index.html"
done
expect_file "recovered: aggregate report" "${CI_OUT}/results/report/multiqc_report.html"

if [ "${status2}" -ne 0 ]; then
    echo "  ---- nextflow output ----"
    tail -30 "${log2}" | sed 's/^/    /'
fi

rm -f "${log1}" "${log2}" "${log_ignore}"
summary "run-failure-isolation"
