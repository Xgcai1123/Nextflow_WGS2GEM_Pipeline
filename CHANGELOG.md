# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-10-24

### Added

- **An English interface and an overall progress bar (user feedback).** The console was Chinese-only and
  its progress was implicit: four counters plus a 12-process ladder, no single "where is the run".
  Everything user-visible is now English — page chrome, labels, hints, dialogs, and the API's
  validation/preflight/failure messages (the existing DOM ids and JSON keys are unchanged, so the
  contract and the test suite carried over). Above the ladder there is now an overall progress bar
  (`#overall-progress`, ARIA progressbar) with `completed / expected` percentage, a caption such as
  `17 / 26 tasks · 65% · 2 running`, the current stage, terminal-state colouring (done green, failed
  red with the failing stage, cancelled grey) and a `state updated Ns ago` marker; the ladder and the
  four counters stay as the per-step/per-sample detail.

### Changed

- **SSE `state` events now carry a progress digest.** The server used to emit state only when the run
  id or the state *name* changed, so during a 38-minute run the page's counters, ladder and progress
  sat on the first frame and only jumped when the run finished; a background tab could also come back
  showing a stale `Running`. The event is now re-sent whenever `submitted/completed/failed` moves (26
  tasks = 26 small events per run).
- **The run panel no longer depends on one long-lived connection.** While the tab is visible the page
  polls `GET /api/state` every 5 s (paused when hidden), refreshes immediately on `visibilitychange`
  and `focus`, re-fetches 600 ms after a terminal SSE state (so the report/exit code are complete), and
  a 45 s SSE watchdog closes and reconnects when nothing has arrived.

### Fixed

- **"The Run module does not update after the task ends."** Root cause was the event model above, not
  rendering: progress was pushed at state-machine granularity (F51). The fixes above make the bar move
  per task and make the page converge after sleep/reconnect; verified with the fake engine
  (8%→…→100%, `54%` while running, `100% · Finished`, `38% · failed at SPADES`) and with a kill -9 /
  restart of the UI server while a run was in flight (the un-refreshed page recovered to `Running`,
  `Log stream connected`, elapsed 00:33).
- **`bin/pipeline-ui` printed three Chinese errors** (missing server file / wrong directory / missing
  python3); it is English now, like the rest of the interface.

### Verification

`tests/ci/run-ui-check.sh`: **318 ok / 0 FAIL / 0 SKIP in 39 s** (was 282), including 36 new assertions
for English-only pages and Python messages, the progress-bar element/aria/clamp, and the four refresh
fallbacks; the real `-stub-run` DAG and the injected-FASTQC failure leg still pass.

## [0.2.1] - 2026-10-24

### Fixed

- **Starting a run right after opening the page could be refused with a false "external nextflow process"
  409 (F50).** The page auto-runs the preflight on load, and the preflight calls `nextflow -version`; the
  busy detector excluded this server's own pid, its recorded run pid and its `Popen` handle, but **not its
  own child processes**, so that short-lived JVM — whose command line naturally mentions "nextflow" — was
  reported as a foreign engine. Clicking Start within that window failed with
  `检测到外部 nextflow 进程 (pid …)` and then worked seconds later. The heuristic was also too loose in a
  second way: the checkout is called `…/WGSpipeline-nextflow`, so any command line that mentioned the
  directory and the word `run` matched. The classifier now only counts (a) an executable whose basename is
  exactly `nextflow` / `fake-nextflow` (including behind `sh -c`), (b) a JVM carrying
  `nextflow.cli.Launcher` / `nextflow.jar` / `-jar …/nextflow-<version>-<build>.jar`, **and both shapes must
  carry the `run` argument** — so `-version` and `config -flat` can never match. In addition, the busy scan
  now excludes every descendant of this server (parent/child column from `ps`) plus children registered by
  `_run_cmd` while alive (the `ps`-less fallback), the `409` message names the offending pid and command,
  and the test gate imports `ui/runner.py` instead of keeping a drifting copy of the heuristic. Tests:
  17 new assertions over a true/false classifier table and a synthetic process table; suite 282 ok /
  0 FAIL / 0 SKIP (42 s). Existing 409 semantics are unchanged: a real foreign engine still blocks a
  second run.

## [0.2.0] - 2026-10-24

### Added

- **A local web UI for one run at a time — `bin/pipeline-ui` (roadmap C).** The pipeline was
  usable only from a shell, so "run it, watch it, read the report" meant keeping a terminal open
  and knowing which of a dozen paths to open afterwards. The new interface is deliberately
  **local and single-user**: it binds `127.0.0.1`, serves one page, and owns exactly one
  `nextflow run` at a time. It is not a hosted service — the three questions that decide such a
  project (who maintains the 4 GB reference databases, who pays for the 8 cores, who carries the
  pathogen-data compliance) have no answer here, and BV-BRC/KBase already occupy that space.
  - **An operator shell, not a second pipeline.** It does not validate biology, does not delete
    anything and does not render its own report. Sample-sheet authority stays in `main.nf` (the
    UI writes the same six columns and passes `--samples_tsv`), deletion authority stays in
    `bin/pipeline-clean` (ticking "clear outputs" shows its `check` plan first and runs
    `outputs --yes` only after confirmation), and presentation authority stays in MultiQC (the
    real self-contained 15 MB report in an iframe, plus the real `results/` tree as a streamed
    tar.gz). Reads are chosen with a **server-side file browser**, so a 400 MB FASTQ is never
    uploaded through the browser.
  - **State comes from the engine, never from a guess.** Progress is the engine's own
    `[hash] Submitted|Completed process > NAME (tag)` lines, rendered as a 12-process x N-sample
    ladder plus a live log (SSE, replayable from a byte offset); the report path comes from the
    `onComplete` line, with the resolved config and a glob as fallbacks. Expected tasks are
    `11*N + references + 2` — 26 for the validated two-sample cohort. A run that exits 0 without
    writing a report is **`failed(report_missing)`**, never a green check; a failed task is shown
    with its process, sample, exit code, work directory and stderr tail, and can be retried with
    `-resume`.
  - **Survivable operation.** Cancel sends SIGTERM to the process group (10 s, then SIGKILL) and
    keeps `work/`, so the run stays resumable; it is a distinct state from failure. The engine is
    spawned in its own session with its output on disk, so killing the UI mid-run is not fatal: a
    restarted UI **attaches** to the live run, keeps streaming its log, and can still cancel it.
    The security boundary is boring on purpose — one-shot token in the printed URL, `Origin`
    check, no generic static-file serving, no upload endpoint.
  - **Verification.** `tests/ci/run-ui-check.sh` (new `local-ui` CI job) is 265 assertions in
    35–47 s: a programmable fake engine covers fail-on-demand, hang-until-cancelled and
    exit-0-without-report; one leg runs the real `-stub-run` DAG (25 tasks, `done`); an injected
    FASTQC failure is parsed into `FASTQC_RAW / CI_B_1 / exit 1`; seven malformed payloads are
    each rejected with row+field and **no spawn**; SSE replay is byte-identical; the results
    tarball is compared file by file; cancellation from an attached run is asserted end to end
    after killing and restarting the UI; and the repository's `results/` and `data/` are proven
    untouched (F18). Three mutation negative controls make the suite fail (always-true token,
    noreport treated as done, cancel without killing the process group). Design contract and
    decisions: `docs/ui.md` (UI-D1..D12, D26). Measured end-to-end acceptance (2026-10-24): the UI drove the real two-sample cohort
    from a cleaned tree to the report. **26/26 tasks, exit 0, 77.5 minutes** (00:33:50 ->
    01:51:21 +0800) on an 8-core host with only **8 GiB of RAM**, swap 3.3-4.8 GiB in use
    and loadavg 3.4 rising to 24.1. Every number matched the 0.1.0 baseline: assembly
    4,835,534 bp / 370 contigs / N50 159,317 / GC 50.33 and 5,404,919 / 390 / 244,803 /
    50.34; BUSCO 440/440 (C:100.0% [S:99.5%, D:0.5%]); MEMOTE 68.1 / 67.8; growth
    0.953 h-1 / 0.529 g g-1 with 66.000 / 0.001 / 0.000 / 0.228; Bakta 4,891 / 5,496 rows;
    genome fraction 99.38 / identity 99.926 and 97.37 / 99.721. The 16,150,251-byte report
    was served byte-identically and rendered in the iframe, and the 350 MiB results tarball
    round-tripped **69/69 files**, with the QUAST table and the report byte-identical after
    extraction. The **<=45 min** figure was **not** met on this host (77.5 min); that is the
    F42 condition rather than the UI -- the run is the same 26-task DAG that takes ~38 min on
    an idle host -- so the timing criterion stays open, with the reproduction procedure in
    roadmap C and docs/ui.md section 14. In a first attempt on the same night the host was in
    a worse state (loadavg up to 24.1, swap 7.1 GiB) and the run was cancelled at 67.9 min /
    10 of 26 tasks; that attempt is what proved the real-run cancel and kill/restart attach
    paths.
- **`bin/pipeline-clean` — one cleanup command with three explicit blast radii.** "Clean up" meant
  three different things and the repository only offered `rm -rf`, so the expensive thing and the
  big thing had to be paid for together: `results/` is 434 MiB and ~38 minutes of compute,
  `.nextflow/` is 5.87 GiB of conda environments and a network round trip. The levels are now
  named: **`check`** (the default — removes nothing, prints what every level would remove with
  sizes), **`outputs`** (every product *and* every intermediate — `results/ work/ logs/
  benchmarks/ .nextflow/history .nextflow/cache data/cache/` and the engine logs — keeping
  `.nextflow/conda`, so the next run needs no download) and **`all`** (the above plus
  `.nextflow/` and `.nxhome/`). The line is drawn at the only thing that a run cannot
  regenerate from local material: the conda environments cost ~5.7 GiB of downloads. What no level can remove is enforced three ways
  rather than promised: the delete tables are an **allowlist** (an input that appears in no table
  cannot be removed, even by a typo); every candidate path is re-validated immediately before
  removal (relative, no `..`, not a symlink, parent resolving inside the checkout, not a declared
  input); and the inputs are inventoried before and after, so a changed byte under `data/raw`,
  `data/db` or `data/reference` — or an edited tracked file — makes it exit 1 and name the
  difference instead of reporting success. Reclaimed space is measured with `df` before/after,
  never by summing `du`: measured the same day, the old checkout's `.snakemake/conda` reported
  **6.8 GB** by `du` and freed **2.80 GiB**, because conda environments are hardlinked to each
  other. Refusals: a running Nextflow launcher (`pgrep` on `nextflow.cli.Launcher`), a symlinked
  `results/` (which would make `work/` the only copy), no `--yes` on a non-terminal stdin (exit 2 —
  it refuses rather than guessing). Two resume behaviours were measured on 26.10.0 rather than
  assumed, and both are documented in the script: deleting `work/` and keeping `.nextflow/cache`
  makes `-resume` recompute (the stale cache is not silently reused), while deleting only
  `results/` and keeping `work/` re-publishes every cached task in seconds (F17). Guarded by
  `tests/ci/run-clean-check.sh` (0.4 s, no Nextflow, no conda, no Python): a fixture project of
  sentinel files is cleaned for real and asserted on both sides — what is gone and what survived —
  with two refusals proven to fire and a negative control that injects `data/raw` into a **copy**
  of the delete table and checks the input refusal still wins. Design doc F41.

- **`docs/justification.md` — the per-decision answer sheet for reviewers.** The existing docs are
  organised by report artifact and column ("what does N50 mean"), which is not the axis a reviewer
  asks along ("why Trimmomatic and not fastp"), so a complete field guide can still feel
  unanswerable. The new sheet is organised by decision, and its backbone is a three-class taxonomy
  of every parameter: **inherited** from the original pipeline (strong evidence, because the
  byte-identical comparison checks it), **tool defaults** (evidence: the manual — we added no
  constraint), and **conventions** chosen by us with no experimental basis — the growth medium's
  uptake bounds, db-light instead of db-full, free solvers. The third class is declared as such
  rather than dressed up as evidence, which is exactly what makes the other two credible. Content:
  twelve steps x five questions (what does it decide / why this tool / what do the parameters mean
  in one sentence / what breaks downstream / what will a reviewer ask), ten rehearsed answers to
  the questions that actually come up, a plain-language glossary of the report's jargon (N50, BUSCO
  complete/duplicated/fragmented, MEMOTE total score, exchange vs biomass reaction, uptake bound,
  yield), and a readability ladder (30 seconds / 3 minutes / deep dive). Written in Chinese on
  purpose: the person preparing the defence reads Chinese, and a document that cannot be skimmed in
  one pass does not exist. It states plainly what the project's strongest honest sentence is — the
  parameters are not ours, they are the original pipeline's, kept so the outputs compare byte for
  byte, and the two steps we added carry their own evidence and finding numbers. `interpretation.md`
  and `README.md` now point at it, and it is cross-referenced from the README's "new to the output"
  block.

- **`docs/interpretation.md` is now a field guide plus a reviewer Q&A.** Section 2 documents every
  column of every table in the report — unit, denominator, this project's value, and how to judge
  it — and section 3 answers the twenty questions that actually come up. Two of them are cases
  where the report itself is misleading: `L50` renders as `0.0 K` in the QUAST table while General
  Statistics shows the integer (MultiQC scales count columns, and a config overlay cannot change a
  module's scale), and the software-versions table still claims `Snakemake 9.25.2` because it comes
  from the reused Snakemake-era config (D4). The README keeps a single link rather than growing
  (F34).
- **`docs/interpretation.md` — a reader's guide to the numbers.** Metric by metric: what it
  means, when it indicates a problem, and what it does not license you to conclude. Includes the
  three traps that look right and are not (the 66 h⁻¹ complete-medium artefact, the growth rate's
  dependence on the chosen uptake bounds, and a low genome fraction caused by a different strain),
  a troubleshooting table, a parameter-sensitivity table, and a section-by-section navigation of
  the report.
- **Data-asset fingerprints.** `docs/asset-sources.md` now also records the assets that are
  fetched rather than copied: CarveMe's `media_db.tsv` (carveme 1.6.6, sha256), and both NCBI
  references (accession, md5, length, sequence count) — including the honest note that CarveMe
  does not document where its medium database came from, so that layer remains unverified.
- **Optional assembly-vs-reference comparison (step 5b, `ASSEMBLY_REF`).** A `reference` column
  in the sample sheet names a public genome (resolved against `ref_dir`); that sample's assembly
  is aligned to it with BLASTN and the result becomes a report section — genome fraction,
  identity, mismatch and indel rates, share of assembly and of contigs that align. Thresholds
  are QUAST's defaults (>= 95 % identity, >= 65 bp), so the numbers are comparable with a
  standard QUAST/dnadiff run, and BLAST+ comes from the pinned Bakta environment, so no new
  environment is introduced. A reference that matches nothing fails the task (exit 2) instead of
  publishing a table of zeroes. Measured on the two validation isolates: 99.38 % of MG1655
  recovered for K-12 at 99.926 % identity, 97.37 % of Sakai for O157:H7 at 99.721 % — the latter
  read as a different strain of the same serotype, not as a broken assembly. QUAST's own `-r`
  was rejected: on arm64 macOS its bundled minimap2 does not compile and QUAST then *silently*
  falls back to reference-independent statistics (F30/D20).
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
  - the section is ordered 650, i.e. directly after the CarveMe table (700) and before MEMOTE (600)
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

- **The local UI showed "0 completed" for the whole run because it read task completion from the wrong
  stream (F48).** Real Nextflow 26.10.0 prints only `Submitted process > …` to stdout; task completion is
  a DEBUG line in the project's `.nextflow.log`
  (`Task completed > TaskHandler[id: N; name: NAME (tag); status: COMPLETED; exit: 0; …]`). The first
  version parsed stdout only, so the 2026-10-23 acceptance run was 18.6 minutes and 10 completed tasks in
  while the UI still said `completed=0` — the fake engine, which printed `Completed process >` itself, had
  kept all 237 assertions green. The UI now tails `.nextflow.log` as the completion source (fresh runs start
  at the current end of the file; a rotation or truncation resets to 0; attach and terminal restore replay
  from the beginning), keeps accepting the stdout form for the fake/older engines, and both sources are
  covered by tests. Verified live: after `kill -9`ing the UI and restarting, the attached run reported
  `completed=10` with SPAdes still running, on the same run id and pid.

- **BAKTA had been broken since the F33 change, and a clean re-run finally said so.** F33 moved the
  AMR-guard cache from `db/amr_guard/` to `data/cache/amr_guard/` by adding
  `export \$WGS_AMR_GUARD_CACHE=${params.amr_cache}` to the process script. The escaped `\$` turns
  that into `export $VAR=/path`: an unset variable expanded in an *assignment* position, and every
  task wrapper runs `bash -ue`, so the task aborted with `unbound variable` before `bakta` started.
  Nothing caught it for a day: the line arrived after the last real BAKTA run, every CI check is a
  `-stub-run`, and a stub block *replaces* the script — the F18 class of defect, reachable only by
  executing the real tool. It surfaced the moment the cache cleanup made a fresh, uncached run
  necessary, and `errorStrategy = 'finish'` turned it into a loud exit 1 rather than a report with a
  missing annotation section. Fixed by dropping the escape, plus a static guard in
  `tests/ci/run-lint.sh` that flags `\$NAME=` in an assignment position (comment lines excluded, or
  the guard would match its own explanation); verified by mutation — reinstating the `\$` makes lint
  fail with the line number. The rule recorded for later: F10's "escape shell variables as `\$VAR`"
  applies to *uses*, never to assignment targets. Design doc F43.

- **A run that looked 4-5x slower than baseline was the host, not the code — and two cheap
  mechanisms now say so out loud.** During the clean re-run, 12 of 25 tasks took 55 minutes against
  a 38-minute whole-run baseline. Forensics: SPADES#1 reached K99 in 3m46s (baseline-comparable)
  and then spent 27 minutes in the single-threaded graph construction; the engine's own poll
  interval stretched from 5 s to 296 s; `jcmd Thread.print` showed `Task submitter` parked in
  `TaskPollingMonitor.awaitSlots` while four downstream `workDir`s stayed empty; `vm.loadavg` read
  34-37 on 8 cores **and stayed at 29 after every pipeline process was killed**. A process census
  put it at 9 of ours against ~480 others (Firefox 17, Chrome 17, VS Code 10, Office/ClassIn 12,
  Spotify 9, Spotlight 16), with `tmutil status` showing no backup. Single-threaded stages get
  ~1/30 of a core under that load, which is why the run was normal for four minutes and then
  collapsed — the asymmetry is what made it look like a defect. Fixes: the checkout now carries
  `.metadata_never_index` (git-ignored) so Spotlight stops tagging the ~150k files `work/` churns
  per run; `bin/pipeline-clean` warns when it removes more than 20,000 files that macOS will now
  reindex and that a slow follow-up run is the host (threshold overridable via
  `PIPELINE_CLEAN_SPOTLIGHT_FILES`, asserted both ways in CI); and every timing claim now has to
  carry `vm.loadavg` and the core count — the old 38-minute baseline is not comparable for exactly
  that reason. Design doc F42. The `awaitSlots` question is explicitly left open: SPADES declares
  `cpus = 8` on an 8-core host, which looks like slot accounting, but four FASTQC tasks (4 cpus
  each) plus two Trimmomatic tasks (8 cpus each) did run concurrently earlier, so the mechanism is
  not established and needs an idle-host experiment.

- **The disk pre-flight now points at `bin/pipeline-clean` instead of at two raw `rm -rf` lines.**
  The F20 guard's advice was `rm -rf work` and `rm -rf .nextflow/conda`, printed exactly when the
  volume is full and nobody is reading carefully — two paths that happen to be right today and that
  no test checked. It now names `bin/pipeline-clean outputs --yes` (task cache, `results/`, logs)
  and `bin/pipeline-clean all --yes` (plus the conda environments), and
  `tests/ci/run-disk-guard.sh` asserts the hint names the tool rather than a raw deletion, so the
  advice cannot drift away from the tool that owns it. The guard's own assertions are unchanged:
  it still aborts before the first task, still names the shortfall, and still documents
  `--min_free_disk_gb`.

- **The pipeline figure is generated and machine-checked instead of hand-drawn.** Section 3 of
  `docs/pipeline-summary.html` is now an inline SVG produced by `docs/make_pipeline_diagram.py`,
  whose edge list is transcribed from `workflow { main: … }` and whose geometry is computed —
  no coordinates are typed by hand. The script refuses to write anything when an edge leaves a
  node on the wrong side, when any sampled point of an edge falls inside *another* node's box,
  when two boxes overlap, or when a label is wider than the box holding it; the geometry check is
  part of `tests/ci/run-lint.sh`, so CI keeps guarding it. Three line styles carry the semantics —
  solid: a file is handed on; dotted: ordering only (step 9 before step 10, no file passes);
  dashed: read into the report by MULTIQC. The layout is a pipeline, not a tree: the data path
  (raw → Trimmomatic → SPAdes → Bakta → CarveMe) runs across the top, every step whose output the
  report reads sits below it on a dashed collection bus that feeds the MULTIQC bar, and CarveMe's
  model statistics join the same bus through a right-hand channel. Each box shows its step number,
  the tool and version that runs, and the artifact it publishes. The old nested-list figure and the
  `.dag` CSS that styled it are gone (design doc F39).
- **MULTIQC did not wait for the reference-comparison step.** The `up_ok` trigger mixed FastQC
  (both passes), QUAST, BUSCO, CarveMe and MEMOTE, but not `ASSEMBLY_REF` — while MULTIQC copies
  that section out of `refcomp_dir` by path with `if [ -f … ]`. A cohort with references could
  therefore lose the section silently if 5b were still running when the report was assembled: no
  error, no warning, a report that looks complete. It never triggered on the two-sample run
  (BLASTN finishes far earlier than MEMOTE) but it is a race, not a guarantee, and it contradicts
  the step's own comment. Fixed by mixing `ASSEMBLY_REF.out` into `up_ok`; an empty channel is a
  no-op, so cohorts without references are unaffected. Task count and published artifacts are
  unchanged; MULTIQC's task hash changes, so the next `-resume` rebuilds the report once
  (design doc F40).

- **Corrected the pipeline diagram and audited the summary page against source.** The diagram drew
  an edge from the raw FASTQ straight to SPAdes; `main.nf` wires
  `SPADES(TRIMMOMATIC.out.map { … })` and the process declares `path(r1_paired), path(r2_paired)`,
  so Spades consumes the **trimmed paired** reads and the raw files feed only FastQC and
  Trimmomatic. That was a drawing error, not a pipeline defect — and it is exactly why hand-drawn
  ASCII is no longer the primary picture.

  The architecture section is now a nested flow in which every line is `process → the artifact it
  produces`, so indentation is the edge and every output is named with its real path pattern; a
  MultiQC collector table lists, group by group, exactly which files it reads. The project
  structure gained a visual tree (CSS, no absolute positioning) while the copy-friendly `<pre>`
  stays.

  Re-deriving every claim from the source and from the published tree turned up three more
  over-claims, all fixed: Spades publishes **only** `contigs.fasta` (its ~200 side products stay in
  `work/`, because Nextflow publishes declared outputs only); the reference comparison TSV has
  **17** metrics, not 18; and `results/report/nextflow/{report.html,trace.tsv}` is the **engine's**
  report, not MultiQC's. Everything else was checked and stands (12 process contracts, 14 parameter
  defaults, the two Bakta line counts, the CarveMe diamond counts, the report's 13 sections, the
  seven checks and four CI jobs). Design document F38.
- **Report cells no longer repeat a unit the header already carries.** MultiQC renders module
  columns as `N50 (Kbp) | 244.8 Kbp` and `% Dups | 12.9 %`; the duplication is baked into the
  module, where one `suffix` feeds both the title and every cell (setting it empty yields
  `N50 ()`). `scripts/strip_units.py` therefore normalises the rendered report: it removes a unit
  from a cell only when that column's header already contains it, touches only the visible text
  (never `val`/`data-sort`, so sorting, filtering and CSV export still see the raw numbers), and
  is idempotent. 48 cells in the current report; `run-embed-check.sh` pins six assertions
  (removed / kept / attribute intact / idempotent). Documented as the report's display convention
  in `docs/interpretation.md`.
- **Fixed: `--data-dir multiqc_data` in the MULTIQC task.** In MultiQC 1.21 `--data-dir` is a
  boolean switch, not a path, so it swallowed the following token as an analysis directory and a
  real run would have failed. Neither the stub smoke nor CI could catch it — the stub `stub:`
  block replaces MULTIQC's script body, so the multiqc command never executes there. Replaying the
  task's commands did catch it. The parsed-data directory now uses MultiQC's default
  `<report>_data` name, published by pattern (F35).
- **The failure-isolation test injected at the wrong entry point, so its central assertion was
  weaker than it claimed.** `FASTQC_RAW` and `TRIMMOMATIC` are parallel branches off the same
  channel, and `SPADES` depends only on `TRIMMOMATIC`, so killing a sample's `FASTQC_RAW` never
  prevented that sample's assembly from being produced — the run simply raced the scheduler.
  It passed locally by timing and failed on CI, which is the correct outcome for a test whose
  assertion was passing through an empty path. The injection now poisons both entry points, which
  is also the realistic model of a corrupt input (F32).
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

- **The versions table no longer claims `Snakemake`, and now lists what actually ran.** It carried
  a `Snakemake 9.25.2` row — a stale claim inherited from the reused Snakemake-era config — while
  omitting three tools the pipeline really uses (BLAST+ 2.17.0 for the reference comparison,
  COBRApy 0.32.1 and GLPK 5.0 for the growth check). All other entries were checked against the
  installed environments and are correct. The engine version is no longer hard-coded: the MULTIQC
  task writes `custom_content/nextflow_mqc_versions.yaml` from `workflow.nextflow.version`, so the
  table always names the Nextflow that actually ran (F37).
- **`docs/pipeline-summary.html` — the whole pipeline on one self-contained page.** Architecture
  and DAG, the annotated project tree, a per-step input/output/consumer contract, the configuration
  reference, the report's section tour and field guide, the twenty questions the numbers raise, the
  validation evidence, the quality gates, the limits, and how to run it. No external assets; prints
  cleanly; hand-written rather than generated.
- **Every table header now follows one convention.** `Sample` as the first column,
  `Quantity, qualifier (unit)` as the shape, units in the header only, counts unitless, and one
  quantity under one name: 27 headers normalised and 76 cell units dropped in the current report.
  `scripts/normalize_report.py` (successor of `strip_units.py`) rewrites the visible header text,
  then strips a cell unit only when the corrected header carries it; `val`/`data-sort`, the plots
  and the CSV export inputs are untouched, and the step is idempotent.
  `quast_config.total_number_contigs_multiplier: 1` also stops L50 printing `0.0 K` for a count of
  eight. No reused asset was edited: the display layer is normalised at render time, so the reused
  MultiQC config stays byte-identical (F36). One item is still open — the `Snakemake 9.25.2` row in
  the versions table is *content*, not formatting, so post-processing cannot fix it without
  inventing a version number.
- **The project is now self-contained: `data/` in, `results/` out, nothing else.** This project
  claimed to be standalone while its raw reads, Bakta database and BUSCO lineage still lived in the
  sibling Snakemake checkout — a structural mistake. They moved into `data/` (raw reads, `db/`,
  `reference/`, `cache/`), and every output moved under a single `results/` root (`qc/`, `reads/`,
  `assembly/`, `annotation/`, `models/`, `report/`). The old checkout keeps symlinks to the new
  locations, so it still runs without a second 4.4 GB copy. The output tree no longer mirrors the
  original Snakemake tree: content is still byte-comparable, only the paths moved (F33/D23).

  The clutter that motivated this is gone with it: `results/report/` now holds only the report, its
  parsed data and a `nextflow/` subdirectory — not the staging directories and custom-content TSVs
  that `publishDir` used to spray there. Nextflow's own `report-<ts>.html` / `trace-<ts>.txt` (ten
  of which had accumulated in the project root) are pinned to `results/report/nextflow/` and
  disabled in CI, because those directives are evaluated at config-parse time and a `-c` overlay
  cannot redirect them — CI had already written a 1.6 MB report into the real tree before this was
  caught. The AMR guard's cache, which defaulted to a stray `db/` at the project root, is now
  `data/cache/amr_guard/` via the script's own `WGS_AMR_GUARD_CACHE` override (the script is a
  byte-identical reused asset and was not edited). `.DS_Store`, ten rotated engine logs, `.nxhome/`,
  `.pytest_cache/`, a 942 MB `m0probe/conda-cache/` and every empty directory were deleted.
- **The growth check also asserts the biomass yield, not just the growth rate.** The rate moves
  with the medium's uptake bounds — measured: 0.476 / 0.953 / 1.283 / 28.772 h⁻¹ for a glucose
  bound of 5 / 10 / 20 / unlimited — so a rate alone cannot support a claim about the model. The
  yield is the model's biomass stoichiometry and barely moves; it is now computed
  ((1000·μ / v_carbon) / M_carbon, with the molar mass declared in the medium file) and asserted
  within 0.30–0.70 g/g. Measured: 0.529 g/g, against ~0.5 g/g for *E. coli* on glucose minimal
  medium. It also appears as a column in the report (F31/D21).
- **The growth check now runs before MEMOTE, and the order is enforced rather than implied.**
  The growth check costs ~6 s and answers whether the model is viable at all; MEMOTE costs
  ~250 s per sample and scores something narrower, so the cheap fundamental check runs first
  and an inviable model fails fast. Declaration order in `main.nf` would have been cosmetic —
  both branches depend only on CarveMe, so Nextflow is free to run them in either order — so
  MEMOTE now consumes a trigger broadcast from `GROWTH_CHECK`. The report order follows (growth
  at 650, before MEMOTE at 600) and the steps are renumbered: 8 CarveMe, **9 growth**,
  **10 MEMOTE**, 11 MultiQC. The first attempt at the edge was wrong in an instructive way: a
  single-valued `val` input next to the per-sample tuple is consumed once, so only one of the
  two MEMOTE tasks ran (24 tasks became 23). The stub-smoke task count caught it; the flag is
  now part of the tuple, and that check additionally asserts the submission order (F29/D19).
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

[Unreleased]: https://github.com/MeMoModelling/NextFlow-WGS2GEM/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/MeMoModelling/NextFlow-WGS2GEM/releases/tag/v0.2.0
[0.1.0]: https://github.com/MeMoModelling/NextFlow-WGS2GEM/releases/tag/v0.1.0
