# NextFlow-WGS2GEM

Industrial pipeline to build genome-scale metabolic model from the WGS of the following work:

Melkonian C et al. "Microbial interactions shape cheese flavour formation." Nature Communications 14, 8348 (2023). https://doi.org/10.1038/s41467-023-41059-2

---

## Overview

A [Nextflow](https://www.nextflow.io/) DSL2 pipeline that takes paired-end Illumina reads of a
bacterial isolate all the way from raw reads to a draft genome-scale metabolic model (GEM):

```
FastQC (raw) → Trimmomatic → FastQC (trimmed) → SPAdes
             → { QUAST | BUSCO | Bakta } → CarveMe → Growth check (M9) → MEMOTE → MultiQC
```

| # | Step | Tool | Purpose |
|---|------|------|---------|
| 1 | Read QC (raw) | FastQC 0.12.1 | raw read quality |
| 2 | Adapter/quality trimming | Trimmomatic 0.39 | clean paired reads |
| 3 | Read QC (trimmed) | FastQC 0.12.1 | post-trim quality |
| 4 | De novo assembly | SPAdes 4.0.0 (`--careful`) | draft genome |
| 5 | Assembly QC | QUAST 5.2.0 | N50 / L50 / contigs / GC% |
| 5b | Reference comparison *(optional)* | `compare_assembly_ref.py` (BLAST+ 2.16) | genome fraction / identity vs a public reference (F30) |
| 6 | Completeness | BUSCO 5.8.3 | lineage completeness |
| 7 | Annotation | Bakta 1.11.4 | GFF3 / FAA / TSV (+ AMR guard) |
| 8 | Model reconstruction | CarveMe 1.6.6 (SCIP) | draft GEM (SBML) |
| 9 | Growth validation | `check_growth.py` (COBRApy) | growth on a **declared** medium — not in the original Snakemake pipeline |
| 10 | Model evaluation | MEMOTE 0.17.0 | model quality score |
| 11 | Aggregate report | MultiQC 1.21 | single QC/report page |

The pipeline is a 1:1 Nextflow port of the original Snakemake implementation; the reused
assets (conda environments, helper scripts, MultiQC config) are byte-identical copies. Step 9
is the one addition: it closes the gap that the ported workflow never gave the models a medium
(see *Model validation* below). It runs **before** MEMOTE on purpose — it costs seconds and
answers the more fundamental question, so an inviable model fails fast instead of after the
four-minute quality suite (F29).

## Requirements

- **Nextflow ≥ 25.10** (strict-syntax era; developed and validated on 26.10.0)
- **conda** or **mamba** on `PATH` (all environments are created automatically)
- **Java 17+** (bundled with the conda `nextflow` package if installed that way)
- **Python 3.8+** — needed only by the optional local web UI (`bin/pipeline-ui`); the
  pipeline itself never calls it (the two report embedders run inside the conda environments)
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

### Cleaning up — three levels, one command

`bin/pipeline-clean` removes what the pipeline **produced** and never what it was **given**. The
default level deletes nothing, so it is safe to run at any time:

```bash
bin/pipeline-clean                 # check: print what each level would remove, with sizes
bin/pipeline-clean outputs --yes   # everything a run produced; keeps the conda environments
bin/pipeline-clean all --yes       # the above plus the toolchain caches (8 conda envs)
```

`outputs` removes **every product and every intermediate** — the task directories, the engine's
run index and its task-hash cache, and the tool cache under `data/cache/` — and stops at the one
thing that needs a network round trip: the conda environments. `all` removes those too.

| Level | Removes | Next run costs |
|---|---|---|
| `check` (default) | nothing — prints the plan | nothing |
| `outputs` | `results/` `work/` `logs/` `benchmarks/` `.nextflow/history` `.nextflow/cache` `data/cache/` `.nextflow.log*` | ~38 min of compute; **no** download, the envs are reused |
| `all` | the above + `.nextflow/` + `data/cache/` + `.nxhome/` | ~38 min + 8 conda environments rebuilt from `envs/*.yaml` (~5.7 GiB) |

`data/raw` (the reads), `data/db` (4 GB of reference databases), `data/reference` and every tracked
file are refused at all three levels: they appear in no delete table, and each candidate path is
re-validated (not a symlink, resolves inside the checkout, not a declared input) immediately before
it is removed. The inputs are then inventoried again, and the script exits non-zero if a byte of
them changed. Reclaimed space is measured with `df` before/after, never by summing `du`, because
conda environments are hardlinked to each other and `du` over-counts what you actually get back.

To regenerate only the deliverables, don't clean at all: `rm -rf results && nextflow run . -resume`
re-publishes every cached task in seconds without recomputing it (measured on 26.10.0, F17).

### Verify without running the pipeline

The repository ships a check suite — no conda environment is built and no tool is executed (the
twelve processes run from their `stub:` blocks, and the embedding check calls the two helper
scripts directly). Only the last line needs a python with `cobra` and `PyYAML`; the rest need
nothing but Nextflow:

```bash
bash tests/ci/run-embed-check.sh           # MEMOTE / QUAST section scripts (no Nextflow needed)
bash tests/ci/run-clean-check.sh           # cleanup tool: three levels + refusals (nothing needed)
bash tests/ci/run-lint.sh                  # nextflow lint (errors + warning ratchet)
bash tests/ci/run-preview.sh               # compile + sample-sheet validation, good and bad
bash tests/ci/run-disk-guard.sh            # disk pre-flight, all three branches
bash tests/ci/run-stub-smoke.sh            # full DAG (25 tasks) + published output contract
bash tests/ci/run-failure-isolation.sh     # injected single-sample failure + -resume
bash tests/ci/run-ui-check.sh              # local UI contract (fake engine + stub DAG)
PYTHON=.../bin/python3 bash tests/ci/run-growth-check.sh   # defined-medium growth (needs cobra)
```

### Local web UI — one run, in a browser

`bin/pipeline-ui` starts a browser interface for **exactly one run at a time**, bound to
`127.0.0.1`. It is a local UI by design, not a web service: no account, no multi-user, no
upload, and no deletion surface of its own.

```bash
bin/pipeline-ui                                   # print the token URL and open the browser
bin/pipeline-ui --port 8787 --no-open             # scripted / remote-shell use
bin/pipeline-ui --nextflow ~/miniforge3/envs/nextflow/bin/nextflow   # engine not on PATH
```

It needs **python3 ≥ 3.8** (standard library only — no pip, no npm) on top of what a normal run
needs. Everything else is the pipeline you already have: the UI writes a sample sheet and runs
`nextflow run . --samples_tsv …`, the report it shows is the same self-contained MultiQC page
(≈15 MB) in an iframe, and the download is the real `results/` tree as a tar.gz.

The flow is this README's flow with the file picker replaced by the local filesystem: choose
R1/R2 in the built-in browser (**reads are never uploaded** — the page lists paths server-side),
pick *resume* or *fresh*, press start, watch the 12-process ladder and the engine log stream in,
then load the report. `bin/pipeline-clean` remains the only thing that deletes: ticking "clear
outputs" shows its `check` plan first and runs `outputs --yes` only after confirmation, so the
UI never becomes a second cleanup implementation.

Failure is reported as failure: the panel names the process, the sample, the exit code, the work
directory and the tail of stderr, and a run that exits 0 without writing a report is
**failed(report_missing)**, never a green check. Cancelling sends SIGTERM to the whole process
group (10 s, then SIGKILL) and keeps `work/`, so the run stays resumable. Killing the UI mid-run
is survivable: the engine runs in its own session, its log is on disk, and a restarted UI
attaches to the live run — and can still cancel it.

Security is deliberately dull: bind `127.0.0.1`, a one-shot token in the printed URL, an
`Origin` check on every API call, and no generic static-file serving. A non-loopback `--host`
prints a warning in the terminal and a red banner in the page.

Design record, HTTP contract and the measured acceptance run: [docs/ui.md](docs/ui.md).

The same scripts run on every push
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)); [`tests/ci/README.md`](tests/ci/README.md)
lists exactly what each one proves.

## Configuration

All parameters live in `nextflow.config` and can be overridden with `--key=value`.
The most relevant ones:

| Parameter | Default | Meaning |
|---|---|---|
| `raw_dir` | `data/raw` | base directory for the `fq1`/`fq2` paths |
| `bakta_db` | `data/db/bakta/db-light` | Bakta database (`version.json` acts as an upgrade sentinel) |
| `busco_db` | `data/db/busco` | BUSCO lineage download/cache directory |
| `ref_dir` | `data/reference` | base directory for the optional `reference` column (F30) |
| `threads_default` | `8` | threads for Trimmomatic / SPAdes |
| `busco_lineage` | `enterobacterales_odb10` | changes the BUSCO output filenames |
| `carveme_solver` | `scip` | `cplex`/`gurobi` need a licence |
| `memote_solver` | `glpk` | solver used for MEMOTE FBA tests |
| `quast_min_contig` | `0` | `0` = count every contig |
| `min_free_disk_gb` | `20` | pre-flight floor, in GiB, on every volume that holds `work/` or an output directory; the run aborts before any task if a volume is below it. `0` disables the check |
| `report_tag` | `""` | suffix for `multiqc_report[_tag].html` |

## Outputs

Everything a run produces lands under one root. The layout groups by what a reader
is looking for, not by which tool produced it:

```
data/                        INPUTS (git-ignored, fetched or supplied)
  raw/                       raw paired-end FASTQ
  db/                        Bakta database, BUSCO lineage cache
  reference/                 NCBI reference genomes (optional, step 5b)
  cache/                     persistent caches (AMR guard's known-bad sequences)

results/                     OUTPUTS (git-ignored, recreated by a run)
  qc/                        every quality report in one place
    fastqc_raw/              FastQC on raw reads
    fastqc_trimmed/          FastQC on trimmed reads
    quast/<sample>/          QUAST report.html/.txt/.tsv
    busco/<sample>/          BUSCO short summaries
    refcompare/<sample>/     assembly vs reference (F30)
  reads/                     trimmed reads (paired + unpaired)
  assembly/<sample>/         SPAdes assembly (contigs.fasta + side products)
  annotation/<sample>/       Bakta .gff3/.faa/.tsv/.ffn/.gbff/.txt
  models/
    carveme/<sample>/        draft model (.xml) + diamond table
    memote/<sample>/         MEMOTE index.html + result JSON + score TSV
    growth/                  defined-medium growth check (growth_mqc.tsv + growth.tsv)
  report/                    the reports you show to somebody
    multiqc_report.html      the single aggregate deliverable
    multiqc_data/            MultiQC's parsed data (machine-readable)
    nextflow/                Nextflow's execution report + trace

logs/                        per-step logs
benchmarks/                  per-step wall times (MultiQC "step runtimes" table)
```

Two roots and nothing else: **`data/` is what goes in, `results/` is what comes out.**
A run creates no files anywhere else (`logs/`, `benchmarks/`, `work/` and `.nextflow/`
are run bookkeeping, not results). This deliberately does **not** mirror the original
Snakemake tree — content is still byte-comparable, only the paths moved (F33/D23).

## Report

`results/report/multiqc_report.html` is a single self-contained page that:

> Want the whole thing on one page? **[docs/pipeline-summary.html](docs/pipeline-summary.html)** — architecture, per-step input/output contract, the report field guide, the evidence and the open items.
>
> New to the output? **[docs/interpretation.md](docs/interpretation.md)** explains every
> number in it: what it means, when it indicates a problem, and what it does not prove.
>
> Defending a choice? **[docs/justification.md](docs/justification.md)** answers *why* per
> decision, not per column: which parameters are inherited from the original pipeline, which
> are tool defaults, and which are **conventions with no evidence behind them** — plus ten
> rehearsed answers and a plain-language glossary.

- orders **report sections and General Statistics columns by pipeline step**
  (FastQC → QUAST → assembly vs reference → BUSCO → CarveMe → growth → MEMOTE → runtimes
  → software versions), and
- **embeds the full MEMOTE snapshot report** of every sample inline (each MEMOTE page is a
  self-contained Angular document, so it is wrapped in an isolated `srcdoc` iframe to keep
  its CSS/JS from clashing with the MultiQC page), and
- **merges QUAST's own output files** into the report: the complete interactive `report.html`
  of every sample (same isolated-`srcdoc` treatment; QUAST's page is likewise fully
  self-contained) plus its plain-text `report.txt` summary, in one *QUAST: full assembly
  reports* section placed directly after the QUAST statistics table, and
- **reports the defined-medium growth result** as a table directly after the CarveMe model-size
  table (step 9): growth on M9 next to growth on the complete medium CarveMe ships, the
  negative controls, and a count of failed checks. Its section configuration lives in the
  TSV's own file header, so no `sp:` entry was added to the reused config (F26), and
- **explains its two reaction counts instead of letting them contradict each other.** The
  CarveMe table reads its counts from the SBML file, while MEMOTE reports them after cobra
  auto-creates an exchange reaction for 10 boundary metabolites the file leaves without one
  (`EX_na1_e`, `EX_ni2_e`, …) — hence 2486 against MEMOTE's 2496 on the same model. A
  `section_comments` note sits next to the number; metabolite and gene counts agree exactly
  (design doc F27).

The cohort shown in the report is exactly the sample sheet — built-in modules are fed an
explicit file list, never a directory scan, so subset reports stay consistent.

## Assembly validation (reference genome)

An assembly with a respectable N50 can still be the wrong organism, a contaminated sample, or a
different strain than the sheet claims. Naming a public reference genome turns "the assembly
looks plausible" into a measured statement.

The `reference` column is optional. When a row names a FASTA (resolved against `--ref_dir`),
step 5b aligns that sample's assembly to it and adds a report section:

| column | meaning |
|---|---|
| Genome fraction (%) | reference bases covered by accepted alignments / reference length |
| Identity (%) | length-weighted mean identity over accepted alignments |
| Mismatches / 100 kbp, Indels / 100 kbp | rates over the aligned assembly |
| Assembly aligned (%) | assembly bases inside accepted alignments |
| Aligned contigs (%) | contigs with at least one accepted alignment |

Thresholds are QUAST's defaults (>= 95 % identity, >= 65 bp alignment), so the numbers are
comparable with a standard QUAST/dnadiff run. BLAST+ already ships in the pinned Bakta
environment, so this adds no environment. A reference that matches nothing makes the task
**fail** (exit 2): a wrong reference is worse than none, and a table of zeroes looks like a
result.

Fetching the two references used in this project's validation (~1.4 MB and ~1.7 MB):

```bash
mkdir -p data/reference && cd data/reference
for acc in GCA_000005845.2 GCA_000008865.2; do
  curl -sSL "https://api.ncbi.nlm.nih.gov/datasets/v2/genome/accession/$acc/download?include_annotation_type=GENOME_FASTA" -o "$acc.zip"
  unzip -o -q "$acc.zip" -d "$acc.d"
  cp "$(find "$acc.d" -name '*.fna' | head -1)" "$acc.fasta"
done
```

Both are complete genomes from RefSeq/GenBank: *E. coli* K-12 substr. MG1655 (4,641,652 bp,
`U00096.3`) and *E. coli* O157:H7 str. Sakai (5,594,605 bp: chromosome + pOSAK1 + pO157).
`data/` is git-ignored, so they are fetched, never committed.

Measured on this project's two isolates:

| sample | reference | genome fraction | identity | mismatches / 100 kbp | aligned contigs |
|---|---|---|---|---|---|
| SRR1770413 (K-12) | GCA_000005845.2 (MG1655) | 99.38 % | 99.926 % | 67.6 | 30.5 % |
| SRR10194530 (O157:H7) | GCA_000008865.2 (Sakai) | 97.37 % | 99.721 % | 260.6 | 96.9 % |

The K-12 assembly recovers 99.4 % of MG1655 at 99.93 % identity: that is what "this isolate is
a K-12 derivative" looks like numerically. The O157:H7 assembly recovers 97.4 % of Sakai at
99.72 % identity with a four-fold higher mismatch rate — consistent with the same serotype but
a **different strain** (Sakai is one isolate; the accessory genome differs), not with a broken
assembly. Note also the aligned-contig asymmetry: only 30.5 % of the K-12 contigs carry 94.5 %
of its bases (300-odd tiny contigs), while 96.9 % of the O157:H7 contigs align.

**Why not QUAST's own `-r`?** QUAST has no hook for an external aligner: it compiles a bundled
minimap2 on first use, and on arm64 macOS that compile fails (`-msse2` and `-mfpu=` are
x86/Linux flags), after which QUAST warns and silently reports reference-independent
statistics — a reference comparison that is not one. QUAST is therefore left byte-identical to
the original pipeline and the comparison lives in its own step (F30).

## Model validation (defined medium)

A flux balance model has no single growth rate: **the medium is a boundary condition**, so a
model is only testable once its medium is declared. CarveMe ships these models with *every*
exchange reaction open — a complete medium — under which the drafts "grow" at **66 h⁻¹**. That
is an assembly-rate artefact, not a growth prediction: with everything available the cell
imports amino acids, nucleotides, vitamins and trace metals instead of building them. The
pipeline's own MEMOTE results have always contained that number
(`test_biomass_default_production`, `data = 65.99993`) without the score reflecting it — the
biomass/basic tests carry **weight 0** in the scorecard, whose 12 weighted tests are all
consistency and annotation.

So the medium is declared in the repository and asserted:

- [`media/m9_aerobic.yaml`](media/m9_aerobic.yaml) — aerobic M9, glucose as sole carbon source.
  The 20 compounds are CarveMe's own M9 (`carveme/data/input/media_db.tsv`), so the recipe is
  not invented here; the uptake rates (glucose 10, O₂ 20, minerals 1000 mmol·gDW⁻¹·h⁻¹) are a
  **documented project convention, not measurements**.
- [`scripts/check_growth.py`](scripts/check_growth.py) — asserts that every medium component
  maps to an exchange reaction, that growth lands in a plausible window, that it respects the
  organism-independent physical bound (2.81 h⁻¹, the fastest known bacterium), that removing
  the carbon source gives zero growth, and that anaerobic growth is lower. Run it directly, or
  via `bash tests/ci/run-growth-check.sh`.

Measured on both models of the v0.1.0 cohort — and reported in every MultiQC run, as the
*Growth on defined medium* table right below the CarveMe model-size table and above MEMOTE:

| | K-12 (`SRR1770413`) | O157:H7 (`SRR10194530`) |
|---|---|---|
| M9 aerobic, glucose | **0.953 h⁻¹** | **0.953 h⁻¹** |
| no carbon source | 0.000 | 0.000 |
| anaerobic | 0.228 | 0.228 |
| complete medium (shipped default) | 66.000 | 66.001 |

Glucose uptake is saturated (10.000 of 10 mmol·gDW⁻¹·h⁻¹), so carbon is the single limiting
nutrient, and O₂ is not limiting (17.6 of 20). The implied biomass yield is **95 gDW/mol
glucose ≈ 0.53 g/g**, against roughly 0.5 g/g measured for *E. coli* on glucose minimal medium —
an independent plausibility check that 66 h⁻¹ could never have passed.

Two honest limits: this establishes **physiological plausibility, not quantitative accuracy**
(the latter needs measured growth data), and both models predict the *same* rate because
CarveMe's template biomass equation dominates — the check cannot discriminate between strains.

## Performance

The per-step wall times in `benchmarks/` are real measurements of the tool commands. They are
**highly sensitive to the host machine**: on an idle 8-core machine the whole pipeline
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
(440/440), assembly sizes and GC% consistent with K-12 and O157:H7, and MEMOTE total scores
**68.1 and 67.8** (raw 0.680641 / 0.677781; earlier revisions of this file quoted the truncated
68.0 / 67.7). Note what that score does and does not cover: its 12 weighted tests are all
consistency and annotation, so it is **not** evidence that the models were checked
physiologically — see *Model validation (defined medium)* above for that, and for the
biomass/growth tests that carry zero weight.

## Repository layout

```
main.nf                     DSL2 workflow: validation + 12 processes
nextflow.config             parameters, process settings, conda, profiles,
                            Nextflow's own report/trace location
samples.tsv                 the cohort (no reference genomes)
samples.with_reference.tsv  the same cohort with the optional `reference` column
multiqc_config.yaml         MultiQC config (byte-identical reused asset)
multiqc_nextflow.yaml       MultiQC presentation overlay (step order, MEMOTE + QUAST embedding)
envs/                       conda environment definitions (8, byte-identical)
media/                      versioned growth-medium definition (M9, aerobic, glucose)
scripts/                    helper scripts (byte-identical) + memote_html_section.py,
                            quast_html_section.py, check_growth.py, compare_assembly_ref.py
bin/task_env.sh             in-task wall-time helper feeding benchmarks/
bin/pipeline-clean          three-level cleanup: check (default) / outputs / all
docs/                       design document (F/D records), interpretation guide,
                            asset provenance, generated project tree
tests/ci/                   tool-free check suite (lint, validation, DAG, disk guard,
                            failure isolation, report-embedding scripts, growth check,
                            cleanup blast radius)
.github/workflows/ci.yml    runs tests/ci/ on every push and pull request
m0probe/                    minimal Nextflow probe used to validate runtime behaviour
                            (not part of the workflow, not run by CI)
data/                       inputs: raw reads, databases, references, caches  (ignored)
results/                    outputs: qc, reads, assembly, annotation, models, report (ignored)
logs/ benchmarks/           per-run bookkeeping                                  (ignored)
work/ .nextflow/            engine cache, environments, run history             (ignored)
```

Generated artefacts are never committed; `docs/project-structure.md` is the same tree
with annotations, produced from `git ls-files` so it cannot drift.

## Versioning

This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The current
version is declared once, in the `manifest` block of `nextflow.config`:

```groovy
manifest {
    name            = 'NextFlow-WGS2GEM'
    version         = '0.2.1'
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
