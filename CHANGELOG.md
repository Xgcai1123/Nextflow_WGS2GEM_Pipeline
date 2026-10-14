# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

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
  not been exercised with an injected failing sample.
- **Reference databases are not bundled** — `--raw_dir`, `--bakta_db` and `--busco_db`
  must point at local copies (defaults assume a sibling `../WGSpipeline` checkout).
- **Requires Nextflow ≥ 25.10** (strict syntax and the `onComplete:` section syntax used
  by this pipeline).

[Unreleased]: https://github.com/MeMoModelling/NextFlow-WGS2GEM/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/MeMoModelling/NextFlow-WGS2GEM/releases/tag/v0.1.0
