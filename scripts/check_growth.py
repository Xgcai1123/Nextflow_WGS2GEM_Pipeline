#!/usr/bin/env python3
"""Validate that a CarveMe draft model grows on a DEFINED physiological medium.

The problem this solves
-----------------------
A flux balance model has no single "growth rate": the medium is a *boundary
condition*, so the model is only testable once the medium is declared. The
models this pipeline produces are shipped with every exchange reaction open --
342 of 342 -- i.e. a complete medium with unlimited uptake of everything. Under
those bounds FBA reports 66 h^-1 for an *E. coli* draft model, because the cell
can import every amino acid, nucleotide, vitamin and metal instead of building
them. That number measures assembly speed, not growth, and it is already recorded
in the pipeline's own MEMOTE results (`test_biomass_default_production`, data =
65.99993) where it is easy to miss.

What this checker asserts
-------------------------
Applied to `media/m9_aerobic.yaml` (aerobic M9, glucose as sole carbon source):

  1. every compound in the medium maps to an exchange reaction in the model
     (a typo would otherwise silently starve the model and look like a result);
  2. growth on M9 is inside a plausible window [min_growth, max_growth];
  3. growth obeys the organism-independent physical bound 2.81 h^-1 (the fastest
     known bacterium, Vibrio natriegens). MEMOTE 0.17.0 has this same test but
     asserts it with inverted polarity, so it reports PASSED on a 66 h^-1 model;
  4. closing the carbon source gives ZERO growth;
  5. closing O2 gives LOWER growth (E. coli can ferment, so zero would be wrong).

It also reports the complete-medium growth rate for contrast, so the artefact is
visible next to the real number.

Usage
-----
  check_growth.py models/carveme                     # every *.xml below a dir
  check_growth.py models/carveme/SRR1770413/*.xml
  check_growth.py --media media/m9_aerobic.yaml --json /tmp/growth.json models/carveme
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Dict, List, Tuple

import yaml

# The SBML reader logs one line per duplicate reaction on these models; keep the
# tool's own output readable.
logging.getLogger("cobra").setLevel(logging.ERROR)

import cobra  # noqa: E402  (after the logging setup on purpose)

EX_TEMPLATE = "EX_{}_e"
COMPLETE_UPTAKE = 1000.0

# Columns of the emitted tables, in report order. `growth_complete` sits next to
# `growth_M9` on purpose: the contrast between them IS the finding.
MQC_COLUMNS = ["growth_M9", "growth_complete", "growth_no_carbon",
               "growth_anaerobic", "failed_checks"]

# MultiQC custom-content section, self-described in the file header.
#
# MultiQC's `_find_file_header()` collects every line starting with '#' from the
# top of the file, strips the '#', and parses the result as YAML -- so a
# `*_mqc.tsv` file needs no `sp:` entry in any config file. That matters here: a
# second `sp:` in multiqc_nextflow.yaml would REPLACE the block in the reused
# multiqc_config.yaml (replace-not-merge, F21), silently dropping every existing
# custom-content section. The anchor MultiQC sorts a section on is this `id`.
MQC_HEADER = '''\
# id: "growth_on_medium"
# section_name: "Growth on defined medium"
# description: "Growth under a declared physiological medium -- aerobic M9 with glucose as the sole carbon source (media/m9_aerobic.yaml; the compound list is taken from CarveMe's own M9). A flux balance model has no single growth rate, because the medium is a boundary condition. The complete-medium column is the medium CarveMe ships, with every exchange reaction open, under which these drafts report about 66 per hour -- an assembly-rate artefact rather than a prediction."
# plot_type: "table"
# file_format: "tsv"
# pconfig:
#   id: "growth_on_medium_table"
#   col1_header: "Sample"
# headers:
#   growth_M9:
#     title: "Growth on M9 (1/h)"
#     description: "Maximum biomass flux on aerobic M9 with glucose as the sole carbon source. Carbon is the limiting nutrient; the physical bound for any organism is 2.81 1/h."
#     min: 0
#     format: "{:,.3f}"
#   growth_complete:
#     title: "Complete medium (1/h)"
#     description: "The same model with every exchange reaction open, i.e. the medium CarveMe ships. A value orders of magnitude above the M9 column means the model was never given a real medium."
#     min: 0
#     format: "{:,.3f}"
#   growth_no_carbon:
#     title: "No carbon source (1/h)"
#     description: "Must be zero -- without a carbon source the model cannot satisfy the biomass equation."
#     min: 0
#     format: "{:,.3f}"
#   growth_anaerobic:
#     title: "Anaerobic (1/h)"
#     description: "Must be below the aerobic value. E. coli ferments glucose, so zero is not expected."
#     min: 0
#     format: "{:,.3f}"
#   failed_checks:
#     title: "Failed checks"
#     description: "Number of growth checks this sample failed; 0 means every check passed."
#     min: 0
#     format: "{:,.0f}"
'''


def exchange_id(compound: str) -> str:
    """BiGG exchange id for a CarveMe medium-db compound id (glc__D -> EX_glc__D_e)."""
    return EX_TEMPLATE.format(compound)


def load_medium(path: str) -> Tuple[dict, Dict[str, float]]:
    with open(path, encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)
    uptake = {exchange_id(c): float(v) for c, v in cfg["components"].items()}
    return cfg, uptake


def find_models(paths: List[str]) -> List[str]:
    found: List[str] = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                found += [os.path.join(root, f) for f in sorted(files)
                          if f.endswith(".xml")]
        else:
            found.append(path)
    return sorted(set(found))


def biomass_id(model: "cobra.Model") -> str:
    for rxn in model.reactions:
        if rxn.objective_coefficient != 0:
            return rxn.id
    return "?"


def growth_on(model: "cobra.Model", medium: Dict[str, float]) -> float:
    """Maximum biomass flux under the given medium (uptake rates, mmol/gDW/h)."""
    model.medium = dict(medium)
    flux = model.slim_optimize(error_value=0.0)
    if flux is None or flux != flux:  # None or NaN
        return 0.0
    flux = float(flux)
    # Solver noise around zero would print as "-0.000" in the report (and in the
    # console table); treat it as the zero it is.
    return 0.0 if abs(flux) < max(model.tolerance, 1e-6) else flux


def evaluate(model_path: str, cfg: dict, uptake: Dict[str, float]) -> dict:
    model = cobra.io.read_sbml_model(model_path)
    objective = biomass_id(model)

    available = {rxn.id for rxn in model.exchanges}
    missing = sorted(ex for ex in uptake if ex not in available)

    result = {
        "model": model_path,
        "sample": os.path.splitext(os.path.basename(model_path))[0],
        "objective": objective,
        "medium": cfg.get("name", "?"),
        "components": len(uptake),
        "missing_components": missing,
        "reactions": len(model.reactions),
        "genes": len(model.genes),
    }
    if missing:
        # Without the full component set the growth numbers below are meaningless,
        # so report and stop for this model rather than producing a false zero.
        result["checks"] = [("every medium component maps to an exchange reaction",
                             False, "missing: %s" % ", ".join(missing))]
        return result

    result["growth_m9"] = growth_on(model, uptake)
    result["growth_complete"] = growth_on(
        model, {ex.id: COMPLETE_UPTAKE for ex in model.exchanges})

    no_carbon = dict(uptake)
    for compound in cfg["controls"]["no_carbon_source"]["close"]:
        no_carbon.pop(exchange_id(compound), None)
    result["growth_no_carbon"] = growth_on(model, no_carbon)

    anaerobic = dict(uptake)
    for compound in cfg["controls"]["anaerobic"]["close"]:
        anaerobic.pop(exchange_id(compound), None)
    result["growth_anaerobic"] = growth_on(model, anaerobic)

    tol = max(model.tolerance * 10, 1e-6)
    bounds = cfg["checks"]
    g, gc, ga = (result["growth_m9"], result["growth_no_carbon"],
                 result["growth_anaerobic"])
    result["checks"] = [
        ("every medium component maps to an exchange reaction", True,
         "%d/%d" % (len(uptake), len(uptake))),
        ("growth on %s within [%s, %s]" % (result["medium"], bounds["min_growth"],
                                           bounds["max_growth"]),
         bounds["min_growth"] <= g <= bounds["max_growth"],
         "%.3f 1/h" % g),
        ("growth below the physical bound %.2f 1/h" % bounds["max_absolute_growth"],
         g <= bounds["max_absolute_growth"], "%.3f 1/h" % g),
        ("no growth without a carbon source", gc <= tol, "%.3f 1/h" % gc),
        ("anaerobic growth is lower than aerobic", ga < g,
         "%.3f < %.3f 1/h" % (ga, g)),
    ]
    return result


def report(result: dict) -> None:
    print("%s  (%s)" % (result["sample"], result["model"]))
    print("   objective=%s  reactions=%s  genes=%s"
          % (result["objective"], result.get("reactions"), result.get("genes")))
    if result.get("missing_components"):
        print("   medium '%s': %d components, MISSING %s"
              % (result["medium"], result["components"],
                 ", ".join(result["missing_components"])))
    else:
        print("   medium '%s': %d components, all present as exchange reactions"
              % (result["medium"], result["components"]))
        print("      growth on M9 (aerobic)   : %8.3f 1/h" % result["growth_m9"])
        print("      no carbon source         : %8.3f 1/h" % result["growth_no_carbon"])
        print("      anaerobic                : %8.3f 1/h" % result["growth_anaerobic"])
        print("      complete medium (shipped): %8.3f 1/h   <- the artefact F25"
              % result["growth_complete"])
    for label, ok, detail in result["checks"]:
        print("      %s %s -- %s" % ("ok  " if ok else "FAIL", label, detail))
    if result.get("growth_m9") == 0.0 and not result.get("missing_components"):
        print("      note: zero growth on M9 can also mean a missing medium component")
        print("            (e.g. CO2 is not part of CarveMe's M9 recipe) -- check the")
        print("            medium before concluding the model is broken.")
    print()


def table_rows(results: List[dict]) -> List[List[str]]:
    """Rows for the emitted tables, in MQC_COLUMNS order (blank when unavailable)."""
    rows = []
    for r in results:
        rows.append([
            r["sample"],
            "%.3f" % r["growth_m9"] if "growth_m9" in r else "",
            "%.3f" % r["growth_complete"] if "growth_complete" in r else "",
            "%.3f" % r["growth_no_carbon"] if "growth_no_carbon" in r else "",
            "%.3f" % r["growth_anaerobic"] if "growth_anaerobic" in r else "",
            "%d" % sum(1 for _l, ok, _d in r["checks"] if not ok),
        ])
    return rows


def write_tsv(path: str, rows: List[List[str]], header_comment: str = "") -> None:
    with open(path, "w", encoding="utf-8") as handle:
        if header_comment:
            handle.write(header_comment)
        handle.write("\t".join(["Sample"] + MQC_COLUMNS) + "\n")
        for row in rows:
            handle.write("\t".join(row) + "\n")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Assert that a draft GEM grows on a defined physiological medium.")
    parser.add_argument("models", nargs="+",
                        help="SBML model files, or directories searched for *.xml.")
    parser.add_argument("--media", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "media", "m9_aerobic.yaml"), help="Medium definition (YAML).")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="Also write the results as JSON.")
    parser.add_argument("--mqc-out", dest="mqc_out", default=None,
                        help="Write a self-describing MultiQC custom-content TSV "
                             "(*_mqc.tsv: MultiQC auto-discovers it, no sp: entry).")
    parser.add_argument("--tsv-out", dest="tsv_out", default=None,
                        help="Write the same table without the MultiQC header.")
    parser.add_argument("--report-only", action="store_true",
                        help="Do not fail on physiological checks; a mismatch between "
                             "the medium and the model (unmappable component) still "
                             "fails, because that is a configuration defect.")
    args = parser.parse_args(argv)

    cfg, uptake = load_medium(args.media)
    models = find_models(args.models)
    if not models:
        print("no models found in: %s" % ", ".join(args.models), file=sys.stderr)
        return 2

    print("media: %s (%s)" % (args.media, cfg.get("name", "?")))
    print("units: %s   carbon source: %s\n" % (cfg.get("units", "?"),
                                               cfg.get("carbon_source", "?")))
    results, failures, structural = [], 0, 0
    for path in models:
        result = evaluate(path, cfg, uptake)
        report(result)
        results.append(result)
        if result.get("missing_components"):
            structural += 1
        failures += sum(1 for _l, ok, _d in result["checks"] if not ok)

    rows = table_rows(results)
    if args.mqc_out:
        write_tsv(args.mqc_out, rows, MQC_HEADER)
        print("wrote %s" % args.mqc_out)
    if args.tsv_out:
        write_tsv(args.tsv_out, rows)
        print("wrote %s" % args.tsv_out)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"media": args.media, "results": results}, handle, indent=2)
        print("wrote %s" % args.json_out)

    print("%d model(s), %d failed check(s)" % (len(results), failures))
    if structural:
        print("structural failure: the medium does not match the model(s)", file=sys.stderr)
        return 2
    if failures and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
