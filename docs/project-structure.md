# Project structure (the pipeline only)

Generated from `git ls-files` at commit `5edf247` by `docs/make_structure.py` -- **regenerate it, do not hand-edit**. Because only tracked files are read, generated artefacts and fetched databases cannot appear below: the "pipeline only" property is structural.

81 tracked files, in the order you would read them:

## The pipeline you run (4 files)

The only files Nextflow needs. Everything else exists to serve these four.

```
WGSpipeline-nextflow/
├── main.nf
├── nextflow.config
├── samples.tsv
└── samples.with_reference.tsv
```

## Execution environments (8 files)

One pinned conda environment per tool (D4: byte-identical to the original pipeline).

```
WGSpipeline-nextflow/
└── envs/
    ├── bakta.yaml
    ├── busco.yaml
    ├── carveme.yaml
    ├── fastqc.yaml
    ├── memote.yaml
    ├── quast.yaml
    ├── spades.yaml
    └── trimmomatic.yaml
```

## Data definitions (1 file)

Declared inputs that are data rather than configuration (F25/F31).

```
WGSpipeline-nextflow/
└── media/
    └── m9_aerobic.yaml
```

## Helper programs called by tasks (11 files)

Python/bash helpers the processes invoke; the reused ones are byte-identical (D4).

```
WGSpipeline-nextflow/
├── bin/
│   ├── pipeline-clean
│   └── task_env.sh
└── scripts/
    ├── amr_guard/
    │   └── amr_chunk_guard.py
    ├── bin/
    │   └── bakta
    ├── check_growth.py
    ├── compare_assembly_ref.py
    ├── memote_html_section.py
    ├── memote_report.py
    ├── multiqc_extras.py
    ├── normalize_report.py
    └── quast_html_section.py
```

## Local web UI (single user) (7 files)

One python3 process (stdlib only) and one plain-JS page; the browser is a client of the local server, so reads are never uploaded and the UI never deletes anything itself.

```
WGSpipeline-nextflow/
├── bin/
│   └── pipeline-ui
└── ui/
    ├── app.js
    ├── index.html
    ├── runner.py
    ├── server.py
    ├── sheet.py
    └── style.css
```

## Report configuration (2 files)

MultiQC config: the reused one plus the presentation overlay (section order, F21).

```
WGSpipeline-nextflow/
├── multiqc_config.yaml
└── multiqc_nextflow.yaml
```

## Tests and CI (29 files)

Eight local checks and the five-job workflow; nothing here runs tools.

```
WGSpipeline-nextflow/
├── .github/
│   └── workflows/
│       └── ci.yml
└── tests/
    └── ci/
        ├── fixtures/
        │   ├── bakta_db/
        │   │   └── version.json
        │   ├── busco_db/
        │   │   └── .gitkeep
        │   ├── memote/
        │   │   ├── CI_A/
        │   │   │   └── index.html
        │   │   └── CI_B/
        │   │       └── index.html
        │   ├── quast/
        │   │   ├── CI_A/
        │   │   │   ├── report.html
        │   │   │   └── report.txt
        │   │   └── CI_B/
        │   │       └── report.html
        │   ├── raw/
        │   │   ├── CI_A_1.fastq.gz
        │   │   ├── CI_A_2.fastq.gz
        │   │   ├── CI_B_1.fastq.gz
        │   │   └── CI_B_2.fastq.gz
        │   ├── reference/
        │   │   └── CI_A.fasta
        │   ├── ui/
        │   │   └── fake-nextflow
        │   └── samples.tsv
        ├── README.md
        ├── ci.config
        ├── ignore-strategy.config
        ├── inject-sample-failure.config
        ├── lib.sh
        ├── run-clean-check.sh
        ├── run-disk-guard.sh
        ├── run-embed-check.sh
        ├── run-failure-isolation.sh
        ├── run-growth-check.sh
        ├── run-lint.sh
        ├── run-preview.sh
        ├── run-stub-smoke.sh
        └── run-ui-check.sh
```

## Documentation (14 files)

Entry points (README), the change log, the design record, and this tree.

```
WGSpipeline-nextflow/
├── docs/
│   ├── asset-sources.md
│   ├── interpretation.md
│   ├── justification.md
│   ├── make_pipeline_diagram.py
│   ├── make_structure.py
│   ├── nextflow-port-design.md
│   ├── pipeline-summary.html
│   ├── project-structure.md
│   ├── project-structure.png
│   ├── roadmap.md
│   └── ui.md
├── CHANGELOG.md
├── LICENSE
└── README.md
```

## Repository metadata (1 file)

Ignore rules and line-ending policy.

```
WGSpipeline-nextflow/
└── .gitignore
```

## Not the pipeline: the engine probe (4 files)

A minimal Nextflow project that validates engine behaviour on a version upgrade (projectDir in nextflow.config, conda name:, CONDA_PREFIX in a task, publishDir with an input variable). Documented in README > Repository layout; not part of the workflow and not run by CI.

```
WGSpipeline-nextflow/
└── m0probe/
    ├── data/
    │   ├── alpha_R1.fastq.gz
    │   └── beta_R1.fastq.gz
    ├── main.nf
    └── nextflow.config
```

## What is deliberately NOT in the repository

Every path below is git-ignored. They are the pipeline's *outputs* and its *fetched* inputs, which is why the tree above stays readable:

| path | what it holds | can it be recreated? |
|---|---|---|
| `work/` | Nextflow task cache (per-task inputs, outputs, logs, .command.sh) | regenerable; delete freely (`nextflow clean`) |
| `.nextflow/` | run history, cache, and the conda environments built from envs/ | regenerable; deleting the conda dir costs a rebuild |
| `data/raw/` | raw paired-end FASTQ | supplied; never regenerable |
| `data/db/` | Bakta database (4 GB) and BUSCO lineage cache | fetched; see docs/asset-sources.md |
| `data/reference/` | the optional NCBI reference genomes (step 5b) | fetched |
| `data/cache/` | persistent caches (AMR guard's known-bad sequences) | regenerable, at ~5 min per sample of rediscovery |
| `.nextflow/ui/` | local UI runtime state: per-run samples.tsv, run.log, state.json | git-ignored; `pipeline-clean outputs` keeps it (it owns no output), `all` removes it |
| `results/qc/` | every quality report: FastQC, QUAST, BUSCO, reference comparison | regenerable by re-running |
| `results/reads/` | trimmed reads | regenerable by re-running |
| `results/assembly/` | SPAdes assemblies | regenerable by re-running |
| `results/annotation/` | Bakta annotations | regenerable by re-running |
| `results/models/` | CarveMe models, MEMOTE results, growth check output | regenerable by re-running |
| `results/report/` | the MultiQC report, its parsed data, Nextflow's own report/trace | regenerable by re-running |
| `logs/, benchmarks/` | per-task logs and wall times | regenerable by re-running |

## The three files that matter most

| file | why you would open it |
|---|---|
| `main.nf` | the whole workflow: 11 steps, 12 processes, one file |
| `nextflow.config` | every parameter, every output path, every resource directive |
| `docs/interpretation.md` | what the numbers in the report mean |
