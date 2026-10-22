#!/usr/bin/env bash
# ============================================================
# bin/pipeline-clean -- the three levels, and the refusals that make them safe.
#
# A cleanup script is the one tool in a project that can destroy the evidence,
# so its blast radius is asserted rather than described. The fixture below is a
# miniature project in which inputs, products and caches are all sentinel files;
# every level is then executed for real and checked on *both* sides -- what is
# gone, and what survived.
#
# Two of the checks are deliberately negative controls, because a guard that
# cannot fail is not a guard (see tests/ci/README.md):
#   - the "declared input" refusal is exercised by editing a *copy* of the
#     script so that data/raw appears in the delete table. The allowlist is not
#     the only thing standing between a typo and the reads.
#   - running without --yes over a non-terminal stdin must refuse, not guess.
#
# No Nextflow, no conda, no real data: this check runs in a couple of seconds.
#
# Local usage:  bash tests/ci/run-clean-check.sh
# ============================================================

# shellcheck source=tests/ci/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/pipeline-clean.XXXXXX")" || { echo "cannot create temp dir" >&2; exit 2; }
FX="${TMP}/project"
LOG="${TMP}/last.log"
trap 'rm -rf "${TMP}"' EXIT

# Sentinel files: names are stable so the assertions read as prose.
make_fixture() {
    rm -rf "${FX}"
    mkdir -p "${FX}/bin" "${FX}/envs" "${FX}/scripts" \
             "${FX}/data/raw" "${FX}/data/db/bakta" "${FX}/data/reference" "${FX}/data/cache/amr_guard" \
             "${FX}/results/qc/fastqc_raw" "${FX}/results/report" "${FX}/work/ab/1234" \
             "${FX}/logs/fastqc_raw" "${FX}/benchmarks/fastqc_raw" \
             "${FX}/.nextflow/conda/env-1/bin" "${FX}/.nextflow/cache"

    printf 'process X { script: "true" }\n' > "${FX}/main.nf"
    printf 'workDir = "${projectDir}/work"\n' > "${FX}/nextflow.config"
    printf 'sample\tfq1\tfq2\n' > "${FX}/samples.tsv"
    printf 'name: fastqc\n' > "${FX}/envs/fastqc.yaml"
    printf '# helper\n' > "${FX}/scripts/multiqc_extras.py"

    # inputs -- these must survive every level
    for s in SRR1770413 SRR10194530; do
        printf 'reads %s 1\n' "${s}" > "${FX}/data/raw/${s}_1.fastq.gz"
        printf 'reads %s 2\n' "${s}" > "${FX}/data/raw/${s}_2.fastq.gz"
    done
    printf '{"name": "db-light"}\n' > "${FX}/data/db/bakta/version.json"
    printf '>ref\nACGT\n' > "${FX}/data/reference/ref.fna"

    # products -- level `outputs` removes these
    printf 'fastqc\n' > "${FX}/results/qc/fastqc_raw/sampleA_fastqc.html"
    printf 'report\n' > "${FX}/results/report/multiqc_report.html"
    printf '>contig\nACGTACGT\n' > "${FX}/work/ab/1234/contigs.fasta"
    printf 'log\n' > "${FX}/logs/fastqc_raw/sampleA.log"
    printf 's\th:m:s\n12\t0:00:12\n' > "${FX}/benchmarks/fastqc_raw/sampleA.tsv"
    printf 'run index\n' > "${FX}/.nextflow/history"
    printf 'engine log\n' > "${FX}/.nextflow.log"
    printf 'engine log 1\n' > "${FX}/.nextflow.log.1"
    printf '<html>run report</html>\n' > "${FX}/report-2026.html"
    printf 'trace\n' > "${FX}/trace-2026.txt"
    printf '<html>dag</html>\n' > "${FX}/dag-2026.html"

    # caches -- only level `all` removes these
    printf '#!/bin/sh\necho fastqc\n' > "${FX}/.nextflow/conda/env-1/bin/fastqc"
    printf 'leveldb\n' > "${FX}/.nextflow/cache/session.db"
    printf 'known bad\n' > "${FX}/data/cache/amr_guard/known_bad_sequences.tsv"

    cp "${PROJECT_DIR}/bin/pipeline-clean" "${FX}/bin/pipeline-clean"
    chmod +x "${FX}/bin/pipeline-clean"
}

# run_clean [script] <args...> -- always from a foreign cwd, to prove the tool
# locates the project from its own path and not from $PWD.
run_clean() {
    local runner="${FX}/bin/pipeline-clean"
    if [ "${1:-}" = "--script" ]; then runner="$2"; shift 2; fi
    ( cd / && "${runner}" "$@" ) > "${LOG}" 2>&1 < /dev/null
    LAST_STATUS=$?
}
LAST_STATUS=0

survives() { [ -e "${FX}/$1" ]; }
gone()     { [ ! -e "${FX}/$1" ]; }

# --- 1. default level is `check`: it must delete nothing --------------------
echo "== level: check (the default) =="
make_fixture
run_clean
expect_eq "check exits 0" "0" "${LAST_STATUS}"
expect_grep    "names the three levels"        "check      removes nothing"     "${LOG}"
expect_grep    "documents level outputs"       "keeps   .nextflow/conda"        "${LOG}"
expect_grep    "documents level all"           "rebuilds the 8 environments"    "${LOG}"
expect_grep    "outputs keeps the envs"        "conda environments are NOT rebuilt"          "${LOG}"
expect_grep    "lists the reads as untouchable" "data/raw"                      "${LOG}"
expect_grep    "says nothing was deleted"      "Nothing was deleted"            "${LOG}"
[ -e "${FX}/results/qc/fastqc_raw/sampleA_fastqc.html" ] \
    && pass "deliverables still present" || fail "deliverables still present"
[ -e "${FX}/.nextflow/conda/env-1/bin/fastqc" ] \
    && pass "conda env still present" || fail "conda env still present"

# --- 2. no --yes over a non-terminal stdin: refuse, delete nothing ----------
echo "== refusal: no --yes, no terminal =="
run_clean outputs
expect_eq      "refuses with exit 2"        "2" "${LAST_STATUS}"
expect_grep    "says how to proceed"        "--yes" "${LOG}"
[ -e "${FX}/results/qc/fastqc_raw/sampleA_fastqc.html" ] \
    && pass "nothing removed without confirmation" || fail "nothing removed without confirmation"

# --- 3. level `outputs`: products go, inputs and envs stay ------------------
echo "== level: outputs --yes =="
run_clean outputs --yes
expect_eq "outputs exits 0" "0" "${LAST_STATUS}"
# `outputs` is "everything the pipeline made", intermediates included: the task
# directories, the engine's run index *and* its task-hash cache, and the tool
# cache the AMR guard writes under data/. Only the conda envs survive here.
for p in results work logs benchmarks .nextflow/history .nextflow/cache data/cache \
         .nextflow.log .nextflow.log.1 report-2026.html trace-2026.txt dag-2026.html; do
    gone "${p}" && pass "removed ${p}" || fail "removed ${p}"
done
for p in data/raw/SRR1770413_1.fastq.gz data/raw/SRR10194530_2.fastq.gz \
         data/db/bakta/version.json data/reference/ref.fna \
         .nextflow/conda/env-1/bin/fastqc \
         main.nf nextflow.config samples.tsv envs/fastqc.yaml scripts/multiqc_extras.py; do
    survives "${p}" && pass "kept ${p}" || fail "kept ${p}"
done
expect_grep "verifies the inputs afterwards" "inputs verified unchanged" "${LOG}"
expect_grep "reports reclaimed space"        "reclaimed"                 "${LOG}"
expect_grep "says the envs are reused"       "conda envs are reused"     "${LOG}"

# --- 4. level `all`: caches go too, inputs still stay ----------------------
echo "== level: all --yes =="
make_fixture
run_clean all --yes
expect_eq "all exits 0" "0" "${LAST_STATUS}"
for p in .nextflow data/cache results work logs benchmarks; do
    gone "${p}" && pass "removed ${p}" || fail "removed ${p}"
done
for p in data/raw/SRR1770413_1.fastq.gz data/db/bakta/version.json data/reference/ref.fna \
         main.nf nextflow.config samples.tsv; do
    survives "${p}" && pass "kept ${p}" || fail "kept ${p}"
done
expect_grep "warns that the envs must be rebuilt" "rebuilt from envs/*.yaml" "${LOG}"

# --- 5. a symlinked deliverable tree is refused, not followed ---------------
echo "== refusal: results/ is a symlink =="
make_fixture
mkdir -p "${TMP}/outside"
printf 'precious\n' > "${TMP}/outside/precious.txt"
rm -rf "${FX}/results"
ln -s "${TMP}/outside" "${FX}/results"
run_clean all --yes
expect_eq   "refuses a symlinked results/ (exit 1)" "1" "${LAST_STATUS}"
expect_grep "says why: symlink"                     "symlink" "${LOG}"
[ -e "${TMP}/outside/precious.txt" ] \
    && pass "symlink target untouched" || fail "symlink target untouched"

# --- 6. usage errors -------------------------------------------------------
echo "== refusal: unknown level =="
make_fixture
run_clean nonsense
expect_eq   "unknown level exits 2" "2" "${LAST_STATUS}"
expect_grep "prints usage"          "usage: bin/pipeline-clean" "${LOG}"

# --- 7. negative control: the input allowlist is not the only guard ---------
echo "== negative control: data/raw injected into the delete table =="
make_fixture
# The copy must live inside the fixture: the tool derives the project root from
# its own location, which is what makes it safe to call from any directory.
tampered="${FX}/bin/tampered-pipeline-clean"
sed 's|^OUTPUTS_DIRS=($|OUTPUTS_DIRS=( "data/raw"|' "${FX}/bin/pipeline-clean" > "${tampered}"
chmod +x "${tampered}"
grep -q 'OUTPUTS_DIRS=( "data/raw"' "${tampered}" \
    && pass "injection fired (table really contains data/raw)" \
    || fail "injection did not fire -- this control proves nothing"
run_clean --script "${tampered}" outputs --yes
expect_eq   "declared input beats the table (exit 1)" "1" "${LAST_STATUS}"
expect_grep "names the input refusal"                 "declared input" "${LOG}"
[ -e "${FX}/data/raw/SRR1770413_1.fastq.gz" ] \
    && pass "reads survive a tampered table" || fail "reads survive a tampered table"

summary "run-clean-check"
