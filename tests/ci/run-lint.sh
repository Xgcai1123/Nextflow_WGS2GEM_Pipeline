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
summary "run-lint"
