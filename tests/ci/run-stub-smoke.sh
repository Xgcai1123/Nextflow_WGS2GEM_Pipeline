#!/usr/bin/env bash
# ============================================================
# Full-DAG smoke test (`-stub-run` with conda disabled).
#
# Every process has a `stub:` block, so this executes the complete 10-step DAG
# -- channel wiring, per-process inputs/outputs, publishDir layout, report
# naming, the Bakta db sentinel -- in seconds and without installing a single
# tool. It is the cheapest guard against the wiring regressions this port has
# hit before (F14 stub-less processes, F15 publishDir prefix, F18/F19).
#
# It also proves the CI overlay does what it claims: the validated output tree
# at the repository root is NOT touched (F18: a stub run once published empty
# placeholders over real deliverables).
#
# Local usage:  bash tests/ci/run-stub-smoke.sh
# ============================================================

# shellcheck source=tests/ci/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
require_nextflow

echo "== stub smoke: full DAG =="

rm -rf "${CI_OUT}"

# Marker taken *before* the run: nothing under the real output dirs may be
# newer than it afterwards. mtime resolution is at worst 1 s, hence the sleep.
marker="$(mktemp)"
touch "${marker}"
sleep 1

log="$(mktemp)"
run_nf "${log}" run . -stub-run --min_free_disk_gb=1
status=$?
expect_eq "stub run exits 0" "0" "${status}"

submitted=$(grep -c 'Submitted process' "${log}" || true)
# 2 samples x (FastQC raw 1/2, Trimmomatic, FastQC trimmed 1/2, SPAdes, QUAST,
# BUSCO, Bakta, CarveMe, MEMOTE) = 22, + GROWTH_CHECK (one aggregate task, F26)
# + MULTIQC = 24, + ASSEMBLY_REF for the ONE sample whose fixture row names a
# reference (F30; CI_B has none on purpose, so the mixed cohort is exercised)
# = 25.
expect_eq "task count (2 samples x 11 steps + growth check + MultiQC + 1 ref compare)" "25" "${submitted}"

# F29: the growth check must be ORDERED before MEMOTE. Declaration order in
# main.nf does not achieve that (both branches depend only on CARVEME), so the
# dataflow edge has to be asserted. This also catches the failure mode that broke
# it once: a single-valued input channel next to a per-sample one is consumed
# once, which silently drops every task but the first (visible in the count above
# too, but the count alone does not say WHICH task went missing).
growth_line=$(grep -n 'Submitted process > GROWTH_CHECK' "${log}" | head -1 | cut -d: -f1)
memote_line=$(grep -n 'Submitted process > MEMOTE' "${log}" | head -1 | cut -d: -f1)
if [ -n "${growth_line}" ] && [ -n "${memote_line}" ]; then
    if [ "${growth_line}" -lt "${memote_line}" ]; then
        pass "growth check is ordered before MEMOTE (F29)"
    else
        fail "MEMOTE was submitted before the growth check -- the F29 ordering edge was lost"
    fi
else
    fail "could not find GROWTH_CHECK / MEMOTE submission lines in the run log"
fi

if [ "${status}" -ne 0 ]; then
    echo "  ---- nextflow output ----"
    tail -30 "${log}" | sed 's/^/    /'
fi

echo "== published artifact tree =="

for s in CI_A CI_B; do
    for r in 1 2; do
        expect_file "raw fastqc zip ${s}_${r}"  "${CI_OUT}/results/qc/fastqc_raw/${s}_${r}_fastqc.zip"
        expect_file "raw fastqc html ${s}_${r}" "${CI_OUT}/results/qc/fastqc_raw/${s}_${r}_fastqc.html"
        expect_file "trimmed fastqc zip ${s}_${r}"  "${CI_OUT}/results/qc/fastqc_trimmed/${s}_R${r}_paired_fastqc.zip"
        expect_file "trimmed fastqc html ${s}_${r}" "${CI_OUT}/results/qc/fastqc_trimmed/${s}_R${r}_paired_fastqc.html"
    done
    for kind in R1_paired R1_unpaired R2_paired R2_unpaired; do
        expect_file "trimmed reads ${s}_${kind}" "${CI_OUT}/results/reads/${s}_${kind}.fastq.gz"
    done
    expect_file "assembly ${s}"       "${CI_OUT}/results/assembly/${s}/contigs.fasta"
    for f in report.html report.txt report.tsv; do
        expect_file "quast ${f} ${s}"  "${CI_OUT}/results/qc/quast/${s}/${f}"
    done
    for ext in json txt; do
        expect_file "busco ${ext} ${s}" "${CI_OUT}/results/qc/busco/${s}/short_summary.specific.enterobacterales_odb10.${s}.${ext}"
    done
    for ext in gff3 faa tsv ffn gbff txt; do
        expect_file "bakta ${ext} ${s}" "${CI_OUT}/results/annotation/${s}/${s}.${ext}"
    done
    for f in "${s}.xml" "${s}.faa" "${s}.tsv"; do
        expect_file "carveme ${f}"     "${CI_OUT}/results/models/carveme/${s}/${f}"
    done
    for f in index.html memote_result.json memote_summary.tsv; do
        expect_file "memote ${f} ${s}" "${CI_OUT}/results/models/memote/${s}/${f}"
    done
done
# F26: the growth check is ONE aggregate task (one section, a row per sample),
# so its published artefacts are cohort-level, not per sample.
for f in growth_mqc.tsv growth.tsv; do
    expect_file "growth ${f}" "${CI_OUT}/results/models/growth/${f}"
done
# F30: only CI_A names a reference, so only CI_A publishes a comparison. The
# absence for CI_B is an assertion too -- a cohort without references must not
# grow an empty section or a task of zeros.
for f in CI_A_refcomp.tsv CI_A_assembly_ref_mqc.tsv; do
    expect_file "refcompare ${f}" "${CI_OUT}/results/qc/refcompare/CI_A/${f}"
done
if [ -e "${CI_OUT}/results/qc/refcompare/CI_B" ]; then
    fail "CI_B has no reference in the fixture but a refcompare directory was published"
else
    pass "refcompare: no task and no directory for the sample without a reference"
fi
expect_file "aggregate report" "${CI_OUT}/results/report/multiqc_report.html"

echo "== validated output tree untouched (F18 guard) =="

touched="$(files_newer_than "${marker}" \
    "${PROJECT_DIR}/data" "${PROJECT_DIR}/assembly" "${PROJECT_DIR}/annotation" \
    "${PROJECT_DIR}/models" "${PROJECT_DIR}/reports" "${PROJECT_DIR}/logs" \
    "${PROJECT_DIR}/benchmarks")"
expect_empty "no repository output file modified by the stub run" "${touched}"

rm -f "${log}" "${marker}"
summary "run-stub-smoke"
