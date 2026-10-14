# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The growth result is a pipeline step and a MultiQC section** (`GROWTH_CHECK`, `models/growth/`,
  F26/D17). Until now the defined-medium check only ran when invoked by hand, so the report said
  nothing about whether a model grows — the artefacts landed next to a MEMOTE score that does not
  cover growth at all. Now:
  - `scripts/check_growth.py` gained `--mqc-out` / `--tsv-out` / `--report-only`;
  - the process runs **once for the cohort** — one section with a row per sample, mirroring D15 —
    and publishes a self-describing `growth_mqc.tsv` that MultiQC auto-discovers;
  - its section configuration lives in the **file header**, which `_find_file_header()` parses
    from the `#`-prefixed YAML. That is deliberate: declaring it in the overlay would need an
    `sp:` entry, and a second `sp:` REPLACES the block in the reused `multiqc_config.yaml`
    (replace-not-merge, F21), silently dropping every existing custom-content section;
  - the section is ordered 550, i.e. directly after the MEMOTE table (600) and before the embedded
    full reports and the runtimes table: the summary tables stay in pipeline order, the large
    embedded documents follow;
  - `--report-only` means a plausibility-window violation is **reported** (it appears in the
    report and the log) rather than failing the run, because the window is *E. coli*-specific;
    a medium that does not match the model still exits 2, since that is a configuration defect;
  - no `bench_start/bench_finish`: `benchmarks/` carries the Snakemake step set that the reused
    `multiqc_extras.py` maps to labelled runtime columns, so a file there would surface as a raw
    `growth_check` column.
  Adding the step also **caught a real hole in the check suite**: the new `publishDir` was not
  redirected by `tests/ci/ci.config`, so the first `-stub-run` published empty placeholders into
  the real `models/growth/` — the F18 guard flagged it (that is what the guard is for). Fixed by
  adding the redirect, and the stray stub files were removed. The DAG is now 24 tasks.
- **Growth validation on a defined physiological medium.** A flux balance model has no single
  growth rate — the medium is a boundary condition. CarveMe ships these models with all 342
  exchange reactions open (a complete medium), under which they "grow" at **66 h⁻¹**: with
  everything importable the cell never builds amino acids, nucleotides, vitamins or metals, so
  the objective measures assembly speed rather than growth. The medium is now declared and
  asserted:
  - `media/m9_aerobic.yaml` — aerobic M9, glucose as sole carbon source. The 20 compounds are
    CarveMe's own M9 (`carveme/data/input/media_db.tsv`), so the recipe is not invented here;
    the uptake rates (glucose 10, O₂ 20, minerals 1000 mmol·gDW⁻¹·h⁻¹) are a documented
    convention, not measurements;
  - `scripts/check_growth.py` — asserts that every medium component maps to an exchange
    reaction in the model (a typo would otherwise silently starve it and look like a result),
    that growth lands in a plausible window, that it respects the organism-independent physical
    bound of 2.81 h⁻¹ (the fastest known bacterium, *V. natriegens*), that removing the carbon
    source gives zero growth, and that anaerobic growth is strictly lower;
  - `tests/ci/run-growth-check.sh` (+ a CI job) — guards the checker and the medium with three
    negative controls (unmappable component, impossible window, physical-bound violation), each
    of which must make it exit non-zero. It runs against a synthetic model generated in the
    test, because the real models are generated artefacts that are not in the repository.

  Measured on both v0.1.0 models: **0.953 h⁻¹** on M9 (glucose uptake saturated at 10.000 of
  10 mmol·gDW⁻¹·h⁻¹, so carbon is the single limiting nutrient; O₂ not limiting at 17.6 of 20),
  0.000 with no carbon source, 0.228 anaerobic, and 66.000 on the shipped complete medium. The
  implied biomass yield is 95 gDW/mol glucose (≈ 0.53 g/g) against ~0.5 g/g measured for
  *E. coli* on glucose minimal medium. This establishes physiological plausibility, **not**
  quantitative accuracy; both models predict the same rate because CarveMe's template biomass
  equation dominates (design doc F25, D16).
- **CI coverage for the report-embedding scripts** (`tests/ci/run-embed-check.sh`). The rest of
  the suite drives the pipeline with `-stub-run`, and in stub mode MULTIQC's `stub:` block
  replaces the real script body — so `memote_html_section.py` and `quast_html_section.py` were
  **never executed** by any check. The new script calls both directly against tiny fixtures
  (needs no conda, no Nextflow, no network; 0.3 s, 47 assertions) and pins: the section header
  each emits (MultiQC *silently ignores* a malformed one), one aggregate file for QUAST vs one
  file per sample for MEMOTE (D15), a byte-identical `srcdoc` round-trip for every embedded page
  (fixtures deliberately full of `&`, `<`, `>`, `"`, `'`), no `<base href="/">`/`<style>`/
  `<script>` leaking from an embedded page into the outer document, a missing cohort member
  being a warning rather than a crash, no file at all when nothing can be embedded, and
  `quast_full` still sorting between `quast` and `busco`. Mutation-tested rather than assumed:
  unescaping the `srcdoc`, renaming the anchor, dropping the header comment and inserting an
  extra anchor between `quast` and `busco` each make it exit non-zero with 4 / 13 / 2 / 1
  failing assertions.
- **QUAST's own output files are merged into the MultiQC report.** `report.tsv` was already
  consumed (built-in `quast` table + `quast_generalstats`), so the new
  `scripts/quast_html_section.py` adds the other two: the complete interactive `report.html`
  of every sample (~380 KB each, a fully self-contained document — no `<base>`, no external
  assets) embedded in an isolated `srcdoc` iframe, and the plain-text `report.txt` in a
  collapsible block. It emits **one** `quast_full_mqc.html` for the whole cohort, ordered by a
  fixed `quast_full` anchor (850) directly after the QUAST statistics table and before BUSCO.
  One section, not one per sample, because MultiQC 1.21 matches `report_section_order` anchors
  exactly (no globs) while each `_mqc.html` with a unique `id:` becomes its own top-level
  module — per-sample anchors could not be placed next to the QUAST table (design doc F24/D15).
  Verified: 4 `srcdoc` frames (2 MEMOTE + 2 QUAST) each round-trip byte-identical to its source
  file, section order fastqc → quast → quast_full → busco → … → versions, report
  15.24 MB → 16.12 MB, and no `<base href="/">` leaked into the MultiQC DOM.
- **Disk-space pre-flight check** (`main.nf`, `min_free_disk_gb`, default 20 GiB). The
  workflow now resolves the filesystem of `work/` and of every output directory (via the
  nearest existing ancestor, so a first run before any directory exists is still checked) and
  **aborts before the first task** when a volume is below the floor. The error names the
  shortfall and prints the directories that are safe to delete; `--min_free_disk_gb=0`
  disables the check. This replaces the README-only reminder with an enforced guard for the
  F20 incident (a full volume made SPAdes' internal `gzip -f -7` die mid read-correction with
  OS return 1, broke `-resume` persistence, and inflated individual steps up to 17×). It
  tripped for real on the development host during this work: the volume was at 98 % with
  11.8 GiB free, and `nextflow run . -preview` was refused with the shortfall and the
  safe-to-delete list.
- **Continuous integration** (`.github/workflows/ci.yml`) — three jobs on every push and pull
  request: `nextflow lint` (errors fail the build, warning count is ratcheted), a
  compile/validation smoke test (`-preview` plus six malformed sample sheets that must each
  abort before any task), a full 10-step DAG run with stubs that verifies the published output
  contract (23 tasks) and that the validated output tree is never touched, and the injected
  single-sample failure test below. No conda environment is built and no tool is executed:
  the scripts take about 2 s (lint) to 35 s each, so the three jobs finish in about a minute.
- **Tool-free check suite** (`tests/ci/`) — the CI jobs only call these scripts, so a local
  `bash tests/ci/run-*.sh` performs exactly the same assertions as CI; see
  `tests/ci/README.md`.

### Fixed

- **CI: the first real workflow run failed in three of four jobs.** Two causes were
  environmental, one was a claim this project should not have made:
  - **The runner has 2 CPUs**, while `nextflow.config` declares 4–8 for the real tools, so
    Nextflow refused to schedule anything (`Task requirement exceeds available CPUs -- req: 4;
    avail: 2`). The smoke job published no artifacts at all (65 artifact assertions then failed
    in a cascade) and both inner runs of the failure-isolation job died the same way, which its
    assertions reported as a broken injection and a broken `-resume`. Fixed in
    `tests/ci/ci.config` with `process { resourceLimits = [cpus: 1] }` — a single ceiling that
    also covers processes added later, rather than an explicit list that would rot.
  - **`pip install cobra` does not install PyYAML**, which `check_growth.py` needs for the medium
    definition, so the new growth job died on `import yaml` — and reported it as **13 mutually
    contradictory check failures**, a crash dressed up as check results. The job now installs
    `cobra pyyaml`, and `run-growth-check.sh` pre-checks *both* imports and exits 2 with the exact
    install command instead of producing misleading assertions.
  - **Correction: the design document claimed "CI 3 jobs green" on 2026-10-14.** That was never
    verified on GitHub — the repository had **no workflow run at all** before this one; the claim
    came from running the scripts locally, and local green is not CI green. The claim is corrected
    in place with a pointer to F28, and verification now means reading the run result back from
    GitHub. CI is four jobs.
  Verified locally by forcing the failure: a probe overlay setting `FASTQC_RAW` to `cpus = 999`
  reproduces the refusal (`req: 999; avail: 8`), and layering `ci.config` after it submits all 24
  tasks; the growth check passes with both dependencies and exits 2 with a clear message when
  PyYAML is absent (checked in a clean environment that has neither).

- **The report's two disagreeing reaction counts are now explained where they appear.** The
  CarveMe table reads its counts from the SBML file (2486 / 2478), while MEMOTE reports them
  after cobra loads the model (2496 / 2488) — because cobra **creates an exchange reaction**
  for 10 boundary metabolites the file leaves without one (`EX_na1_e`, `EX_ni2_e`, `EX_cd2_e`,
  `EX_cm_e`, `EX_hg2_e`, `EX_fusa_e`, `EX_gbbtn_e`, `EX_novbcn_e`, `EX_doxrbcn_e`,
  `EX_ttrcyc_e`). Both numbers are correct but measure different things, and the report
  previously showed them side by side with no explanation. A `section_comments` entry in the
  overlay now states it under the CarveMe section, so the reused `multiqc_extras.py` and
  `multiqc_config.yaml` stay byte-identical (D4). Metabolite and gene counts agree exactly in
  both. Side finding, recorded in F27: because `na1` and `ni2` are among those 10 reactions,
  the growth check's medium maps only thanks to cobra's auto-creation — the model's exchange
  set is "the file ∪ what cobra adds", not what the file alone contains.

### Changed

- **MEMOTE figures corrected, and what that score does *not* cover.** `README.md` and the design
  document quoted total scores **68.0 / 67.7**; those are *truncations*, while the pipeline's own
  tables (`memote_score.tsv`) round the raw `0.680641 / 0.677781` to **68.1 / 67.8**, which the
  documentation now matches. The more important correction is the claim's scope: the scorecard's
  12 weighted tests (from `memote_result.json` → `weights`) are all consistency and annotation,
  so the **biomass/growth/medium tests carry weight 0** and "MEMOTE 68.1" is not evidence that a
  model was checked physiologically. Those tests have in fact always recorded the complete-medium
  artefact (`test_biomass_default_production` → `data = 65.99993`), and MEMOTE 0.17.0's
  `test_fast_growth_default` asserts it with **inverted polarity** — `outcome = flux > 2.81`
  followed by `assert outcome`, where the docstring says growth is expected to be *below* 2.81.
  Re-running it confirms the inversion: pytest reports `PASSED` while MEMOTE's own helper returns
  65.9999 for the same model. (`test_find_medium_metabolites` fails with an internal
  `TypeError`, also upstream.) The physical bound is therefore asserted by this project instead
  of being delegated to that test — see the growth validation entry above.
- **Single-sample failure isolation is now measured, not assumed.** The port claimed
  `process.errorStrategy = 'finish'` was the equivalent of Snakemake's `--keep-going`; an
  injected failure (`tests/ci/run-failure-isolation.sh`) shows it is only partly equivalent.
  Verified behaviour: a dead task always fails the run (exit 1, never swallowed); the failed
  sample produces no downstream deliverables; work already finished for the healthy sample is
  preserved and `-resume` converges without recomputing it. Not equivalent: `finish` waits for
  already-running tasks and then stops — it does **not** start the remaining independent
  branches. `ignore`, the only strategy that would, was measured to exit **0** with a dead
  task and is therefore rejected (design doc D12). `README.md` and the design document now
  state the measured semantics instead of the old claim.
- The `-stub-run` smoke test is wired into `.gitignore`-protected scratch space
  (`tests/ci/out/`): every output, log and benchmark directory is redirected there by
  `tests/ci/ci.config`, so a check can never republish empty placeholders over validated
  deliverables (F18).

### Removed

- The presentation deck and its generator script
  (`WGSpipeline_Nextflow_Migration.pptx`, `WGSpipeline_Nextflow_Migration.pdf`,
  `build_ppt.py`) were dropped from the repository: they are outreach material rather
  than pipeline code. They are still retrievable from the `v0.1.0` tag.

## [0.1.0] - 2026-10-14

First base version: a complete, validated Nextflow DSL2 port of the original Snakemake
WGS-to-GEM pipeline.

### Added

- **Pipeline** (`main.nf`, `nextflow.config`) — ten-step DSL2 workflow:

  ```
  FastQC (raw) → Trimmomatic → FastQC (trimmed) → SPAdes
               → { QUAST | BUSCO | Bakta } → CarveMe → MEMOTE → MultiQC
  ```

  executed through conda environments, with the original per-sample output directory
  layout preserved.
- **Parse-time sample-sheet validation** — required columns, per-row field counts,
  sample-name syntax, duplicate names and FASTQ existence are all checked before any
  task starts, with row-specific error messages.
- **Snakemake-semantics equivalents** — `--keep-going` → `process.errorStrategy = 'finish'`;
  Snakemake cache → `-resume` (task-hash cache); `--config report_tag=…` → coexisting
  report filenames; a `db/version.json` sentinel that forces re-annotation when the
  Bakta database is upgraded.
- **Pitfall countermeasures preserved** — QUAST `--labels` sample attribution; the
  AMR-guard wrapper around Bakta (ncbi/amr#191 AMRFinderPlus crash); the CarveMe staged
  copy that stops it from overwriting Bakta's annotation table; the FastQC
  false-failure guard; explicit `$CONDA_PREFIX` interpreters for BUSCO and MEMOTE.
- **MultiQC presentation** (`multiqc_nextflow.yaml`, `scripts/memote_html_section.py`) —
  report sections and General Statistics columns ordered by pipeline step, and the full
  MEMOTE snapshot report of every sample embedded inline in an isolated `srcdoc` iframe.
- **Profiles and overrides** — `standard` / `lowmem` profiles; every parameter overridable
  with `--key=value`.
- **Documentation** (`docs/`, `README.md`) — migration design document with the
  per-process mapping, parameter table, pitfall inventory and acceptance records;
  reused-asset provenance with source commit and SHA-256 checksums; usage, configuration
  and output-layout documentation.
- **Validation assets** — `m0probe/`, a minimal Nextflow project used to validate runtime
  behaviour and kept as a regression probe for Nextflow upgrades; an English presentation
  deck (`WGSpipeline_Nextflow_Migration.pptx` / `.pdf`) with its generator script.

### Validation

- Ten-step pipeline executed end to end on two *E. coli* isolates (K-12 `SRR1770413`,
  O157:H7 `SRR10194530`): BUSCO 100 % complete (440/440), assembly size and GC %
  consistent with both strains, MEMOTE total scores 68.0 / 67.7.
- Deterministic stages reproduce the original Snakemake results **byte for byte**:
  Trimmomatic trimmed reads, SPAdes `contigs.fasta` and QUAST `report.tsv` are
  `cmp`-identical; Bakta tables match line for line; BUSCO scores are identical.
- Whole-pipeline wall time on an idle 8-core machine: **~38 min**, with the sum of all
  step times within **3 %** of the Snakemake baseline. Timing is host-sensitive: a nearly
  full disk plus concurrent heavy load was observed to inflate individual steps up to 17×,
  which is why `README.md` documents the ≥ 20 GB free-disk requirement.

### Known limitations

- **conda only** — Docker/Singularity profiles are not included.
- **Single-sample failure isolation** (`errorStrategy = 'finish'`) is configured but has
  not been exercised with an injected failing sample. → **Resolved in
  [Unreleased](#unreleased):** an injected failure now exercises it
  (`tests/ci/run-failure-isolation.sh`, part of CI), and it verified the semantics are
  narrower than Snakemake's `--keep-going` — see the *Changed* entry there and design doc
  D12. The line is kept here because it was accurate for the `v0.1.0` release.
- **Reference databases are not bundled** — `--raw_dir`, `--bakta_db` and `--busco_db`
  must point at local copies (defaults assume a sibling `../WGSpipeline` checkout).
- **Requires Nextflow ≥ 25.10** (strict syntax and the `onComplete:` section syntax used
  by this pipeline).

[Unreleased]: https://github.com/MeMoModelling/NextFlow-WGS2GEM/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/MeMoModelling/NextFlow-WGS2GEM/releases/tag/v0.1.0
