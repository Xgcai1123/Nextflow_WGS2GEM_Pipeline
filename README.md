# NextFlow-WGS2GEM

Industrial pipeline to build genome-scale metabolic model from the WGS of the following work:

Melkonian C et al. "Microbial interactions shape cheese flavour formation." Nature Communications 14, 8348 (2023). https://doi.org/10.1038/s41467-023-41059-2

---

## Overview

A [Nextflow](https://www.nextflow.io/) DSL2 pipeline that takes paired-end Illumina reads of a
bacterial isolate all the way from raw reads to a draft genome-scale metabolic model (GEM):

```
FastQC (raw) → Trimmomatic → FastQC (trimmed) → SPAdes
             → { QUAST | BUSCO | Bakta } → CarveMe → MEMOTE → MultiQC
```

| # | Step | Tool | Purpose |
|---|------|------|---------|
| 1 | Read QC (raw) | FastQC 0.12.1 | raw read quality |
| 2 | Adapter/quality trimming | Trimmomatic 0.39 | clean paired reads |
| 3 | Read QC (trimmed) | FastQC 0.12.1 | post-trim quality |
| 4 | De novo assembly | SPAdes 4.0.0 (`--careful`) | draft genome |
| 5 | Assembly QC | QUAST 5.2.0 | N50 / L50 / contigs / GC% |
| 6 | Completeness | BUSCO 5.8.3 | lineage completeness |
| 7 | Annotation | Bakta 1.11.4 | GFF3 / FAA / TSV (+ AMR guard) |
| 8 | Model reconstruction | CarveMe 1.6.6 (SCIP) | draft GEM (SBML) |
| 9 | Model evaluation | MEMOTE 0.17.0 | model quality score |
| 10 | Aggregate report | MultiQC 1.21 | single QC/report page |

The pipeline is a 1:1 Nextflow port of the original Snakemake implementation; the reused
assets (conda environments, helper scripts, MultiQC config) are byte-identical copies.

## Requirements

- **Nextflow ≥ 25.10** (strict-syntax era; developed and validated on 26.10.0)
- **conda** or **mamba** on `PATH` (all environments are created automatically)
- **Java 17+** (bundled with the conda `nextflow` package if installed that way)
- **Disk space**: allow **≥ 20 GB free** on the volume that holds `work/` and the output
  directories. This is **enforced, not just documented**: the workflow refuses to start when
  a volume is below the floor, names the shortfall, and prints what can safely be deleted. A
  full volume fails in expensive ways — see *Performance* and *Failure behaviour*.
- Reference databases (Bakta light DB, BUSCO lineage cache) are referenced via parameters and
  are **not** stored in this repository

## Quick start

```bash
# 1. Install Nextflow (once)
conda create -n nextflow -c bioconda nextflow -y
conda activate nextflow

# 2. Point the pipeline at your sample sheet and reference databases
#    (defaults assume ../WGSpipeline/{raw,db}; override on the command line)
nextflow run . \
    --raw_dir   /path/to/fastq \
    --bakta_db  /path/to/bakta/db-light \
    --busco_db  /path/to/busco/db

# Common operations
nextflow run . -resume                 # restart / crash recovery (task-hash cache)
nextflow run . -profile lowmem         # lower thread usage
nextflow run . --report_tag cohortB    # write a second, coexisting report
nextflow run . -preview                # parse + run validation only, no tasks
```

The sample sheet `samples.tsv` is tab-separated and must contain at least
`sample`, `fq1`, `fq2` (additional metadata columns are carried through but not used):

```
sample	fq1	fq2	species	strain
SRR1770413	SRR1770413_1.fastq.gz	SRR1770413_2.fastq.gz	Escherichia coli	K-12
```

It is validated **before any task starts**: missing columns, invalid sample names, duplicate
names, wrong field counts and missing FASTQ files all abort the run immediately with a
row-specific message.

### Verify without running the pipeline

The repository ships a tool-free check suite — no conda environment is built and no tool is
executed (the ten processes run from their `stub:` blocks, and the embedding check calls the
two helper scripts directly):

```bash
bash tests/ci/run-embed-check.sh           # MEMOTE / QUAST section scripts (no Nextflow needed)
bash tests/ci/run-lint.sh                  # nextflow lint (errors + warning ratchet)
bash tests/ci/run-preview.sh               # compile + sample-sheet validation, good and bad
bash tests/ci/run-disk-guard.sh            # disk pre-flight, all three branches
bash tests/ci/run-stub-smoke.sh            # full 10-step DAG + published output contract
bash tests/ci/run-failure-isolation.sh     # injected single-sample failure + -resume
```

The same scripts run on every push
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)); [`tests/ci/README.md`](tests/ci/README.md)
lists exactly what each one proves.

## Configuration

All parameters live in `nextflow.config` and can be overridden with `--key=value`.
The most relevant ones:

| Parameter | Default | Meaning |
|---|---|---|
| `raw_dir` | `../WGSpipeline/raw` | base directory for the `fq1`/`fq2` paths |
| `bakta_db` | `../WGSpipeline/db/bakta/db-light` | Bakta database (`version.json` acts as an upgrade sentinel) |
| `busco_db` | `../WGSpipeline/db/busco` | BUSCO lineage download/cache directory |
| `threads_default` | `8` | threads for Trimmomatic / SPAdes |
| `busco_lineage` | `enterobacterales_odb10` | changes the BUSCO output filenames |
| `carveme_solver` | `scip` | `cplex`/`gurobi` need a licence |
| `memote_solver` | `glpk` | solver used for MEMOTE FBA tests |
| `quast_min_contig` | `0` | `0` = count every contig |
| `min_free_disk_gb` | `20` | pre-flight floor, in GiB, on every volume that holds `work/` or an output directory; the run aborts before any task if a volume is below it. `0` disables the check |
| `report_tag` | `""` | suffix for `multiqc_report[_tag].html` |

## Outputs

The output directory tree intentionally mirrors the original Snakemake layout:

```
data/qc/raw/                 FastQC on raw reads
data/clean_trimmomatic/      trimmed reads (paired + unpaired)
data/qc/trimmed/             FastQC on trimmed reads
assembly/spades/<sample>/    SPAdes assembly (contigs.fasta + side products)
data/qc/quast/<sample>/      QUAST report.html/.txt/.tsv
data/qc/busco/<sample>/      BUSCO short summaries
annotation/bakta/<sample>/   Bakta .gff3/.faa/.tsv/.ffn/.gbff/.txt
models/carveme/<sample>/     CarveMe draft model (.xml + diamond .tsv)
models/memote/<sample>/      MEMOTE index.html + result JSON + score TSV
reports/multiqc/             aggregate report (+ custom_content/, multiqc_report_data/)
logs/                        per-step logs
benchmarks/                  per-step wall times (MultiQC "step runtimes" table)
```

## Report

`reports/multiqc/multiqc_report.html` is a single self-contained page that:

- orders **report sections and General Statistics columns by pipeline step**
  (FastQC → QUAST → BUSCO → CarveMe → MEMOTE → runtimes → software versions), and
- **embeds the full MEMOTE snapshot report** of every sample inline (each MEMOTE page is a
  self-contained Angular document, so it is wrapped in an isolated `srcdoc` iframe to keep
  its CSS/JS from clashing with the MultiQC page), and
- **merges QUAST's own output files** into the report: the complete interactive `report.html`
  of every sample (same isolated-`srcdoc` treatment; QUAST's page is likewise fully
  self-contained) plus its plain-text `report.txt` summary, in one *QUAST: full assembly
  reports* section placed directly after the QUAST statistics table.

The cohort shown in the report is exactly the sample sheet — built-in modules are fed an
explicit file list, never a directory scan, so subset reports stay consistent.

## Performance

The per-step wall times in `benchmarks/` are real measurements of the tool commands. They are
**highly sensitive to the host machine**: on an idle 8-core machine the whole 10-step pipeline
for two samples takes **~38 minutes**, and the sum of all step times is within **3 %** of the
original Snakemake baseline. On a machine that is simultaneously running other heavy software,
or with the disk nearly full, individual steps can take an order of magnitude longer
(observed up to 17×) even though the pipeline itself is unchanged.

The disk half of that observation is now a pre-flight check rather than advice: at ~2 GB free a
full volume made SPAdes' own `gzip -f -7` die mid read-correction (OS return 1) and also broke
`-resume` persistence, so the workflow refuses to start below `min_free_disk_gb` (default
20 GiB) — before the first task, with the shortfall and the safe-to-delete directories printed.

Note that the "step runtimes" table lists steps individually: steps that run in parallel
(QUAST/BUSCO/Bakta, and the two samples) are counted separately, so the sum of a row is larger
than the pipeline wall-clock time.

## Failure behaviour

Verified by injecting a real task failure (`tests/ci/run-failure-isolation.sh`), not inferred
from configuration:

- **A failing task always fails the run.** The pipeline exits non-zero; a dead task is never
  swallowed, and no report is written that would suggest success.
- **The failed sample leaves no partial deliverables.** Nothing downstream of it runs, so a
  half-built model or annotation table cannot be mistaken for a result.
- **Finished work survives.** After fixing the cause, `nextflow run . -resume` converges and
  reuses the tasks that already completed for the healthy samples instead of recomputing them.
- **In-run isolation is weaker than Snakemake's `--keep-going`.** Nextflow's
  `errorStrategy = 'finish'` waits for already-running tasks and then stops; it does not start
  the remaining independent branches. The only strategy that would continue them, `ignore`,
  exits `0` with a dead task, so it is deliberately rejected. See design doc D12.

## Reproducibility

Outputs of the deterministic stages reproduce the original Snakemake results **byte for byte**:

| Output | Check |
|---|---|
| Trimmomatic trimmed reads | `cmp` identical |
| SPAdes `contigs.fasta` | `cmp` identical |
| QUAST `report.tsv` | `cmp` identical |
| Bakta `.tsv` | identical line counts |
| BUSCO summaries | identical scores (only paths / patch versions differ) |

Biological sanity of the reference run (two *E. coli* isolates): BUSCO 100 % complete
(440/440), assembly sizes and GC% consistent with K-12 and O157:H7, MEMOTE total scores 68.0
and 67.7.

## Repository layout

```
main.nf                     DSL2 workflow: validation + 10 processes
nextflow.config             parameters, process settings, conda, profiles
multiqc_config.yaml         MultiQC config (byte-identical reused asset)
multiqc_nextflow.yaml       MultiQC presentation overlay (step order, MEMOTE + QUAST embedding)
envs/                       conda environment definitions (8, byte-identical)
scripts/                    helper scripts (byte-identical) + memote_html_section.py,
                            quast_html_section.py
bin/task_env.sh             in-task wall-time helper feeding benchmarks/
docs/                       design document, asset provenance, audit notes
tests/ci/                   tool-free check suite (lint, validation, DAG, disk guard,
                            failure isolation, report-embedding scripts)
.github/workflows/ci.yml    runs tests/ci/ on every push and pull request
m0probe/                    minimal Nextflow probe used to validate runtime behaviour
```

## Versioning

This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The current
version is declared once, in the `manifest` block of `nextflow.config`:

```groovy
manifest {
    name            = 'NextFlow-WGS2GEM'
    version         = '0.1.0'
    nextflowVersion = '>=25.10.0'
}
```

Conventions:

- every user-visible change is recorded in [CHANGELOG.md](CHANGELOG.md)
  ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format);
- releases are tagged `v<major>.<minor>.<patch>` (e.g. `v0.1.0`);
- the pipeline prints its own version at the end of every run, and exposes it to the
  workflow as `workflow.manifest.version`;
- `nextflowVersion` is enforced, so an unsupported Nextflow fails fast instead of
  producing subtly different output.

While the major version is `0`, the interface (parameters, output layout) may still change
in a minor release; the per-sample output directory layout is nonetheless kept identical to
the original Snakemake implementation.

## License

MIT — see [LICENSE](LICENSE).
