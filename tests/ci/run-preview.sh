#!/usr/bin/env bash
# ============================================================
# Parse/validation smoke test (`-preview` + `-stub-run`):
#
#   1. `-preview` on the fixture sample sheet compiles main.nf and runs the
#      workflow body (sample-sheet validation, report-name check, disk check)
#      without executing a single task.
#   2. Each malformed sheet is injected in turn and must abort *before any task
#      is submitted*. These runs use `-stub-run`, not `-preview`, precisely so
#      that a task WOULD be submitted if validation had failed to fire.
#
# Local usage:  bash tests/ci/run-preview.sh
# ============================================================

# shellcheck source=tests/ci/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
require_nextflow

echo "== preview: valid sheet =="

log="$(mktemp)"
run_nf "${log}" run . -preview --min_free_disk_gb=1
expect_eq "-preview exits 0" "0" "$?"
expect_grep "disk pre-flight ran" "[disk-check]" "${log}"
expect_grep "workflow body executed (onComplete banner)" "NextFlow-WGS2GEM v" "${log}"
expect_nogrep "no task was submitted in preview mode" "Submitted process" "${log}"

echo "== preview: malformed sheets abort before any task =="

bad_dir="${CI_OUT}/tmp"
mkdir -p "${bad_dir}"

# name|expected message fragment|sheet contents
case_specs=(
    "missing_column|missing required column(s): [fq2]|sample\tfq1\nCI_A\tCI_A_1.fastq.gz\n"
    "invalid_name|invalid sample name bad/name|sample\tfq1\tfq2\nbad/name\tCI_A_1.fastq.gz\tCI_A_2.fastq.gz\n"
    "duplicate_name|duplicate sample name CI_A|sample\tfq1\tfq2\nCI_A\tCI_A_1.fastq.gz\tCI_A_2.fastq.gz\nCI_A\tCI_A_1.fastq.gz\tCI_A_2.fastq.gz\n"
    "field_count|expected 3 fields, got 2|sample\tfq1\tfq2\nCI_A\tCI_A_1.fastq.gz\n"
    "missing_fastq|fastq not found|sample\tfq1\tfq2\nCI_A\tnope_1.fastq.gz\tnope_2.fastq.gz\n"
    # F30: a reference column is optional, but a value that points nowhere must
    # abort. Silently dropping it would downgrade the assembly evaluation to
    # "no reference metrics" without saying so.
    "missing_reference|reference not found|sample\tfq1\tfq2\treference\nCI_A\tCI_A_1.fastq.gz\tCI_A_2.fastq.gz\tnope.fasta\n"
    "missing_sheet|samples.tsv not found|__NOFILE__"
)

for spec in "${case_specs[@]}"; do
    name="${spec%%|*}"; rest="${spec#*|}"
    want="${rest%%|*}"; body="${rest#*|}"
    sheet="${bad_dir}/${name}.tsv"
    if [ "${body}" = "__NOFILE__" ]; then
        rm -f "${sheet}"
    else
        printf '%b' "${body}" >"${sheet}"
    fi

    blog="$(mktemp)"
    run_nf "${blog}" run . -stub-run --min_free_disk_gb=1 --samples_tsv "${sheet}"
    status=$?

    [ "${status}" -ne 0 ] && pass "${name}: aborted (exit ${status})" \
                          || fail "${name}: expected a non-zero exit, got 0"
    expect_grep   "${name}: message" "${want}" "${blog}"
    expect_nogrep "${name}: no task submitted" "Submitted process" "${blog}"
    rm -f "${blog}"
done

rm -f "${log}"
summary "run-preview"
