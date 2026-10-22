#!/usr/bin/env bash
# ============================================================
# Lint gate: `nextflow lint` must report no errors, and must not accumulate
# new warnings beyond the pinned baseline (a ratchet).
#
# Local usage:  bash tests/ci/run-lint.sh
# ============================================================

# shellcheck source=tests/ci/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
require_nextflow

echo "== lint =="

# Baseline recorded with the Nextflow version pinned in
# .github/workflows/ci.yml (see NEXTFLOW_VERSION). Warning counts are
# version-specific: after a deliberate Nextflow bump, re-run this script, read
# the reported count, and update the number below in the same commit.
LINT_WARNING_BASELINE=38

log="$(mktemp)"
run_nf "${log}" lint main.nf nextflow.config -o concise
lint_status=$?

nv="$(nf_version)"
echo "  nextflow: ${nv}"

errors=$(grep -c '^Error' "${log}" || true)
warnings=$(grep -c '^Warn' "${log}" || true)

echo "  lint: ${errors} error(s), ${warnings} warning(s) (baseline ${LINT_WARNING_BASELINE})"
if [ "${errors}" -gt 0 ]; then
    grep '^Error' "${log}" | sed 's/^/    /'
fi

expect_eq "lint exits 0 (errors abort)" "0" "${lint_status}"
expect_eq "no lint errors" "0" "${errors}"

if [ "${warnings}" -le "${LINT_WARNING_BASELINE}" ]; then
    pass "warning count within baseline (${warnings} <= ${LINT_WARNING_BASELINE})"
else
    fail "warning count grew: ${warnings} > ${LINT_WARNING_BASELINE} -- fix the new warnings, or (after a deliberate Nextflow bump) re-record the baseline for '${nv}' in tests/ci/run-lint.sh"
    grep '^Warn' "${log}" | sed 's/^/    /'
fi

rm -f "${log}"

# --- the pipeline figure is code, so it is checked like code ------------------
# docs/make_pipeline_diagram.py generates the flow chart in
# docs/pipeline-summary.html. It refuses to emit a figure whose edge leaves a box
# on the wrong side, runs through another box, or whose label is wider than the
# box that holds it -- the class of error a hand-drawn diagram cannot catch.
if command -v python3 >/dev/null 2>&1; then
    if out="$(cd "${PROJECT_DIR}" && python3 docs/make_pipeline_diagram.py --check 2>&1)"; then
        pass "pipeline diagram: ${out#OK diagram: }"
    else
        fail "pipeline diagram geometry: ${out}"
    fi
else
    fail "python3 not found -- cannot validate docs/make_pipeline_diagram.py"
fi

# --- shell escaping: an assignment target is never escaped (F43) -------------
# `export \$VAR=value` inside a `script:` block expands an unset variable in an
# assignment position. Every task wrapper runs `bash -ue`, so the task dies with
# "unbound variable" before its tool starts -- which is how BAKTA sat broken from
# 2026-10-21 until a clean re-run on 2026-10-22. No stub run can catch it: a stub
# block *replaces* the script. So it is caught statically, here.
# Comment lines are skipped on purpose: a shell comment inside a `script:` string
# is where this rule is *explained*, so matching the explanation would make the
# guard fail on its own documentation.
if bad="$(grep -nE '\\\$[A-Za-z_][A-Za-z0-9_]*=' "${PROJECT_DIR}/main.nf" | grep -vE '^[0-9]+:[[:space:]]*#')"; then
    fail "escaped variable in an assignment position -- the task will abort with 'unbound variable' (F43): ${bad}"
else
    pass "no escaped assignment targets in main.nf"
fi

summary "run-lint"
