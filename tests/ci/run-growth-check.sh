#!/usr/bin/env bash
# ============================================================
# Growth validation on a defined medium (F25).
#
# WHY THIS CHECK EXISTS
#   The CarveMe models this pipeline produces ship with all 342 exchange reactions
#   open (a complete medium), so FBA "grows" them at 66 h^-1 -- an assembly-rate
#   artefact, not a growth rate. `media/m9_aerobic.yaml` declares a real medium and
#   `scripts/check_growth.py` asserts the model behaves properly under it.
#
# WHAT THIS SCRIPT GUARDS
#   The models themselves are generated artefacts and are NOT in the repository, so
#   CI cannot validate a real model. What CI can and must prevent is the *checker*
#   silently breaking: a medium typo that starves every model, a threshold edited
#   into uselessness, or control logic that stops controlling. So this check runs
#   the real checker + the real medium against a SYNTHETIC toy model whose correct
#   behaviour is known by construction, then verifies that each guard actually fires
#   when it should (three negative controls).
#
#   The toy model is generated here rather than committed: it is plumbing, not data,
#   and a fake biological model in fixtures/ invites being mistaken for one.
#
#   It also pins the exit-code contract (D17), which the pipeline depends on:
#   0 = every check passed, 1 = a physiological check failed, 2 = the medium does
#   not match the model (a configuration defect). --report-only downgrades 1 to 0
#   -- so the pipeline reports an organism-specific window violation instead of
#   dying on it -- but must never downgrade 2.
#
# REQUIREMENTS: a python3 with `cobra` importable (brings its own GLPK solver via
#   swiglpk). Locally that is the pipeline's memote environment; in CI it is
#   `pip install cobra`. Everything else in tests/ci needs only Nextflow or stdlib.
#
#   PYTHON=/path/to/python bash tests/ci/run-growth-check.sh
# ============================================================

# shellcheck source=tests/ci/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

PYTHON="${PYTHON:-python3}"
if ! command -v "${PYTHON}" >/dev/null 2>&1; then
    echo "${PYTHON} is not on PATH -- this check runs the growth checker directly" >&2
    exit 2
fi
if ! "${PYTHON}" -c 'import cobra' >/dev/null 2>&1; then
    echo "python3 cannot import cobra -- install it (pip install cobra) or point PYTHON"
    echo "at an environment that has it, e.g.:"
    echo "  PYTHON=.nextflow/conda/<env>/bin/python3 bash tests/ci/run-growth-check.sh" >&2
    exit 2
fi

MEDIA="${PROJECT_DIR}/media/m9_aerobic.yaml"
CHECKER="${PROJECT_DIR}/scripts/check_growth.py"
WORK="${CI_OUT}/growth"
rm -rf "${WORK}"
mkdir -p "${WORK}"

# ---------------------------------------------------------------
# Synthetic model with behaviour known by construction: growth is limited by
# glucose (10 glc per unit biomass, glucose capped at 10 -> growth 1.0 /h),
# requires O2 (so anaerobic growth is strictly lower), and cannot grow without a
# carbon source. It carries all 20 M9 exchange reactions so the medium maps 1:1.
# ---------------------------------------------------------------
echo "== toy model (plumbing only, not biology) =="
if ! "${PYTHON}" - "${WORK}/toy_m9.xml" >"${WORK}/toy.log" 2>&1 <<'PY'
import sys
import cobra
from cobra import Model, Reaction, Metabolite

COMPOUNDS = ["glc__D", "o2", "nh4", "pi", "so4", "h2o", "h", "k", "na1", "mg2",
             "ca2", "cl", "fe2", "fe3", "mn2", "zn2", "cu2", "cobalt2", "mobd", "ni2"]

model = Model("toy_m9")
external = {}
for compound in COMPOUNDS:
    met = Metabolite("%s_e" % compound, compartment="e")
    external[compound] = met
    rxn = Reaction("EX_%s_e" % compound)
    rxn.add_metabolites({met: -1.0})
    rxn.bounds = (-1000.0, 1000.0)
    model.add_reactions([rxn])

glc_c = Metabolite("glc__D_c", compartment="c")
o2_c = Metabolite("o2_c", compartment="c")
bio_c = Metabolite("biomass_c", compartment="c")

glct = Reaction("GLCt")
glct.add_metabolites({external["glc__D"]: -1.0, glc_c: 1.0})
glct.bounds = (0.0, 1000.0)
o2t = Reaction("O2t")
o2t.add_metabolites({external["o2"]: -1.0, o2_c: 1.0})
o2t.bounds = (0.0, 1000.0)

growth = Reaction("Growth")
growth.add_metabolites({glc_c: -10.0, o2_c: -5.0, bio_c: 1.0})
growth.bounds = (0.0, 1000.0)

demand = Reaction("DM_biomass_c")
demand.add_metabolites({bio_c: -1.0})
demand.bounds = (0.0, 1000.0)

model.add_reactions([glct, o2t, growth, demand])
# The objective can only be assigned once the reaction belongs to a model.
model.objective = "Growth"
cobra.io.write_sbml_model(model, sys.argv[1])
print("wrote %s" % sys.argv[1])
PY
then
    fail "toy model generation crashed (see ${WORK}/toy.log)"
    summary "run-growth-check"
fi

# ---------------------------------------------------------------
# Positive case: the real checker, the real medium, known-good model
# ---------------------------------------------------------------
echo "== checker on the toy model =="
"${PYTHON}" "${CHECKER}" --media "${MEDIA}" "${WORK}/toy_m9.xml" \
    >"${WORK}/ok.log" 2>&1
expect_eq "checker passes a known-good model" "0" "$?"
expect_grep "all 20 medium components map" "20/20" "${WORK}/ok.log"
expect_grep "growth is the constructed 1.0 /h" "1.000 1/h" "${WORK}/ok.log"
expect_grep "no growth without a carbon source" "no growth without a carbon source" "${WORK}/ok.log"
expect_grep "anaerobic growth is lower" "anaerobic growth is lower" "${WORK}/ok.log"
expect_nogrep "no failing check" "FAIL" "${WORK}/ok.log"

# ---------------------------------------------------------------
# Negative control 1: a medium component with no exchange reaction must be
# caught, not silently treated as absent (which would look like zero growth).
# This is a CONFIGURATION defect, so it exits 2 -- distinct from a failed
# physiological check (1) and, crucially, it is not suppressed by --report-only.
# ---------------------------------------------------------------
echo "== negative control: medium typo =="
sed 's/^  glc__D: 10/  glc__D_typo: 10/' "${MEDIA}" >"${WORK}/typo.yaml"
"${PYTHON}" "${CHECKER}" --media "${WORK}/typo.yaml" "${WORK}/toy_m9.xml" \
    >"${WORK}/typo.log" 2>&1
expect_eq "checker rejects an unmappable component (structural, exit 2)" "2" "$?"
expect_grep "names the missing component" "missing: EX_glc__D_typo_e" "${WORK}/typo.log"

# ---------------------------------------------------------------
# --report-only semantics (D17): a window violation becomes a report entry
# rather than a failure, but a medium that does not match the model stays fatal.
# Both halves are asserted, because the whole point is that the pipeline reports
# physiology honestly without dying on organism-specific thresholds.
# ---------------------------------------------------------------
echo "== --report-only semantics =="
sed 's/^  min_growth: 0.4/  min_growth: 5.0/' "${MEDIA}" >"${WORK}/window.yaml"
"${PYTHON}" "${CHECKER}" --media "${WORK}/window.yaml" "${WORK}/toy_m9.xml" \
    >"${WORK}/window.log" 2>&1
expect_eq "a window violation fails the checker" "1" "$?"
expect_grep "reports the window violation" "FAIL growth on M9 aerobic (glucose) within" "${WORK}/window.log"

"${PYTHON}" "${CHECKER}" --media "${WORK}/window.yaml" --report-only "${WORK}/toy_m9.xml" \
    >"${WORK}/window_ro.log" 2>&1
expect_eq "the same violation is NOT fatal under --report-only" "0" "$?"
expect_grep "but it is still reported" "FAIL growth on M9 aerobic (glucose) within" "${WORK}/window_ro.log"

"${PYTHON}" "${CHECKER}" --media "${WORK}/typo.yaml" --report-only "${WORK}/toy_m9.xml" \
    >"${WORK}/typo_ro.log" 2>&1
expect_eq "a medium/model mismatch stays fatal even under --report-only" "2" "$?"

# ---------------------------------------------------------------
# Negative control 3: the physical bound must fire independently of the window
# (this is the bound MEMOTE's own test asserts with inverted polarity)
# ---------------------------------------------------------------
echo "== negative control: physical bound =="
sed 's/^  max_absolute_growth: 2.81/  max_absolute_growth: 0.1/' "${MEDIA}" \
    >"${WORK}/bound.yaml"
"${PYTHON}" "${CHECKER}" --media "${WORK}/bound.yaml" "${WORK}/toy_m9.xml" \
    >"${WORK}/bound.log" 2>&1
expect_eq "checker rejects growth above the physical bound" "1" "$?"
expect_grep "reports the physical violation" "FAIL growth below the physical bound" "${WORK}/bound.log"

# ---------------------------------------------------------------
# The real medium must stay loadable and self-consistent
# ---------------------------------------------------------------
echo "== the committed medium =="
"${PYTHON}" - "${MEDIA}" >"${WORK}/media.log" 2>&1 <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
comps = cfg["components"]
assert cfg["provenance"]["source_medium_row"] == "M9", "medium is not CarveMe's M9"
assert cfg["carbon_source"] in comps, "carbon source is not in the component list"
assert len(comps) == cfg["provenance"]["compound_count"] == 20, \
    "expected CarveMe's 20 M9 compounds, got %d" % len(comps)
for control in ("no_carbon_source", "anaerobic"):
    for c in cfg["controls"][control]["close"]:
        assert c in comps, "control closes %r which is not in the medium" % c
assert cfg["checks"]["min_growth"] < cfg["checks"]["max_growth"], "empty window"
assert cfg["checks"]["max_absolute_growth"] > cfg["checks"]["max_growth"], \
    "the physical bound must sit above the plausibility window"
print("medium ok: %d components, window [%s, %s], physical bound %s"
      % (len(comps), cfg["checks"]["min_growth"], cfg["checks"]["max_growth"],
         cfg["checks"]["max_absolute_growth"]))
PY
expect_eq "medium passes its own consistency assertions" "0" "$?"

summary "run-growth-check"
