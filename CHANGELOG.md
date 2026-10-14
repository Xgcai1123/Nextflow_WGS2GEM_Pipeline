# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Changed

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
