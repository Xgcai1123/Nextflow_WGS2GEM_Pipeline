# Automated checks (`tests/ci/`)

Every check here is a shell script. The GitHub workflow
([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) installs Nextflow and
calls the same scripts, so **CI and a local run execute identical assertions** and cannot
drift apart.

```bash
export PATH="/path/to/nextflow/bin:$PATH"     # Nextflow >= 25.10
bash tests/ci/run-embed-check.sh              # needs python3 only, no Nextflow
bash tests/ci/run-clean-check.sh              # needs neither python3 nor Nextflow
bash tests/ci/run-lint.sh
bash tests/ci/run-preview.sh
bash tests/ci/run-disk-guard.sh
bash tests/ci/run-stub-smoke.sh
bash tests/ci/run-failure-isolation.sh
bash tests/ci/run-ui-check.sh                 # local UI contract (fake engine + stub DAG)

# Needs a python with `cobra` (no Nextflow, no conda):
PYTHON=.nextflow/conda/<memote env>/bin/python3 bash tests/ci/run-growth-check.sh
```

Each script exits non-zero on the first failed assertion and prints a `PASS/FAIL` line per
assertion. They are independent and can be run in any order.

## What each check proves

| Script | Proves |
|---|---|
| `run-embed-check.sh` | The two report-embedder scripts **actually run** — they never do under `-stub-run`, because MULTIQC's `stub:` block replaces the real script body. Asserts the section header each emits (`id` / `section_name` / `plot_type`; MultiQC silently ignores a malformed one), that QUAST yields **one aggregate** file for the cohort while MEMOTE yields **one file per sample** (D15), that every `<iframe srcdoc>` parses back **byte-identical** to its source page (fixtures deliberately full of `&`, `<`, `>`, `"`, `'`), that an embedded page's `<base href="/">`, `<style>` and `<script>` never reach the outer document, that a cohort member with no report is a warning rather than a crash — and that the file with no inputs is not written at all. Also pins the ordering contract: `quast_full` sorts between `quast` and `busco` in the overlay, with nothing else squeezed in. Needs no conda, no Nextflow and no network. |
| `run-clean-check.sh` | [`bin/pipeline-clean`](../../bin/pipeline-clean) removes what the pipeline **produced** and nothing it was **given**, at each of its three levels. A miniature project of sentinel files is cleaned for real and checked on both sides: `check` (the default) deletes nothing; `outputs` removes every product and every intermediate (`results/ work/ logs/ benchmarks/ .nextflow/history .nextflow/cache data/cache/` and the engine logs) while `data/raw`, `data/db`, `data/reference`, the code and `.nextflow/conda` all survive; `all` also removes the caches. Two refusals are proven to fire — no `--yes` over a non-terminal stdin (exit 2), and a symlinked `results/` (exit 1, the symlink target untouched). The last case is a negative control: a **copy** of the script with `data/raw` injected into its delete table still refuses, so the input allowlist is not the only thing standing between a typo and the reads. Runs in under a second and needs no Nextflow, no conda and no Python. |
| `run-lint.sh` | `nextflow lint main.nf nextflow.config` reports **no errors**, and the warning count does not exceed the pinned baseline (a ratchet, so lint debt cannot grow unnoticed). It also runs `docs/make_pipeline_diagram.py --check`, so the flow chart in `docs/pipeline-summary.html` cannot grow an edge that leaves a box on the wrong side, crosses another box, or a label wider than its box — the failure mode a hand-drawn figure has (design doc F39). Needs no Nextflow for that part. |
| `run-preview.sh` | `-preview` compiles `main.nf` and runs the workflow body without executing a task; and seven malformed sample sheets (missing column, bad name, duplicate name, wrong field count, missing FASTQ, missing reference, missing file) each abort **before a single task is submitted**. The bad sheets are run with `-stub-run`, not `-preview`, precisely so a task *would* be submitted if validation failed to fire. |
| `run-disk-guard.sh` | The F20 disk pre-flight passes at a low floor, **aborts before any task** with a message naming the shortfall and the cleanup commands when the floor is unmet, and is fully skipped with `--min_free_disk_gb=0`. |
| `run-stub-smoke.sh` | The complete DAG (25 tasks: 2 samples x 11 steps, plus the one aggregate growth-check task, plus MultiQC, plus one reference comparison for the single fixture sample that names a reference) runs to success with stubs, producing the full documented artifact tree — and the validated output tree at the repository root is **not modified** (the F18 regression guard: a stub run once published empty placeholders over real deliverables — and it fired again when the F26 growth check was added without redirecting its new `publishDir`). |
| `run-failure-isolation.sh` | With one sample's **entry points** (FastQC *and* Trimmomatic — they are parallel branches, and SPAdes depends only on Trimmomatic, so poisoning one was not enough: F32) deliberately killed: the run **exits 1** (never swallowed), the failed sample produces **no downstream deliverables**, the healthy sample's finished work **survives**, and `-resume` converges while reusing that work. It also re-measures the negative control: `errorStrategy = 'ignore'` — the only strategy that would continue independent branches — exits **0** with a dead task, which is why it is rejected (design doc D12). |
| `run-ui-check.sh` | The local web UI ([docs/ui.md](../../docs/ui.md), roadmap C) is an **operator shell, not a second pipeline**: a programmable fake engine (`fixtures/ui/fake-nextflow`) covers success / fail on demand / slow (cancel) / exit 0 without a report, and one leg runs the real `-stub-run` DAG in a temporary project copy with `ci.config`. Asserts the security boundary (no token / wrong token / evil `Origin` → 403; `HttpOnly` + `SameSite=Strict` cookie; `no-store` on every API response), seven malformed payloads each → 400 with row+field and **no spawn** (state stays idle, no run directory, no engine child), the six-column sheet byte-for-byte, the single-job 409, SSE replay byte-identical from `from=`, `done` → report bytes == file and results tarball file list == tree, `failed` → process/tag/exit_code/stderr_tail, **exit 0 without a report is failed(report_missing) rather than a green check**, cancel → process group gone + state cancelled + log kept (also for an attached run after the UI is killed and restarted, exit_code null, second cancel 409), single instance, stale pid → interrupted, and F18 (repository `results/` and `data/` untouched). Needs bash + curl + python3; only the stub leg needs Nextflow. Measured 35–47 s. Three mutation negative controls each make it fail (token check always true, noreport as done, cancel without killpg). |
| `run-growth-check.sh` | `media/m9_aerobic.yaml` + `scripts/check_growth.py` still work: the checker passes a synthetic model whose behaviour is known by construction (growth 1.0 h⁻¹, strictly lower anaerobically, zero without a carbon source), and **three negative controls each make it exit non-zero** — an unmappable medium component (exit **2**, a configuration defect), an impossible plausibility window (exit 1), and a physical-bound violation (exit 1). It also pins the `--report-only` contract the pipeline relies on: a window violation is downgraded to 0 but still reported, while a medium/model mismatch stays fatal. Plus the committed medium's own consistency (20 compounds, provenance row `M9`, every control compound present, physical bound above the window). The real models are generated artefacts and are not in the repository, so this guards the *checker*; the models themselves are validated on every real run, where the same script is step 9 (design doc F25/F26). |

## Layout

| Path | Role |
|---|---|
| `ci.config` | Nextflow config overlay for every check: redirects all output/log/benchmark dirs and `workDir` under `tests/ci/out/`, disables conda, and clamps CPUs (`process.resourceLimits = [cpus: 1]`). Nothing a check does can touch validated data, and the clamp is what lets the stub DAG run on a 2-CPU CI runner at all (F28). |
| `inject-sample-failure.config` | Fails `FASTQC_RAW` for one sample (`CI_FAIL_SAMPLE`, default `CI_B`) through `beforeScript`, so `main.nf` carries no test hook. |
| `ignore-strategy.config` | Negative control for the rejected `errorStrategy = 'ignore'`. |
| `fixtures/` | Two-sample sheet with tiny synthetic gzipped FASTQ reads, a `reference` column that is populated for `CI_A` only, and a `bakta_db/version.json` sentinel; `reference/CI_A.fasta` is a 240 bp stub (so the mixed cohort is exercised without a download); plus `quast/` and `memote/` — minimal stand-ins for a QUAST `report.html`/`report.txt` and a MEMOTE `index.html` (including its `<base href="/">`), each seeded with the characters that break naive escaping. |
| `lib.sh` | Shared assertion helpers and the `nextflow` wrapper (`NXF_HOME` stays inside the project, ANSI-free logs make the greps stable). |
| `out/` | Scratch space; git-ignored, safe to delete at any time. |

## Notes

- **No conda environment is built** and no tool is executed: all Nextflow checks use `-stub-run`
  (every process has a `stub:` block). Measured on the development machine: `run-embed-check.sh`
  0.3 s, `run-clean-check.sh` 0.4 s, `run-lint.sh` 2 s, `run-stub-smoke.sh` 8 s,
  `run-growth-check.sh` 12 s, `run-disk-guard.sh` 16 s, `run-failure-isolation.sh` 27 s,
  `run-preview.sh` 33 s.
- `run-ui-check.sh` is the newest member: 265 assertions, 37 s. It deliberately owns **no output tree**: every
  scenario builds a `mktemp -d` project, and the real stub leg copies the pipeline into one, so a UI regression
  cannot publish over validated data (F18). The `SKIP` it prints when `nextflow` is absent is a skip, not a pass —
  the new `local-ui` CI job installs Nextflow so the stub leg always runs there.
- `run-growth-check.sh` is the **one check that is not tool-free**: it needs `cobra` to solve
  flux balance problems and `PyYAML` to read the medium definition, so its CI job runs
  `pip install cobra pyyaml` (cobra brings the GLPK solver `swiglpk`; it does **not** bring
  PyYAML, which is what broke this job on its first real CI run — F28). Locally, point `PYTHON`
  at the pipeline's memote environment, which already has both. It generates its own synthetic
  model instead of committing one: the toy is plumbing, not data, and a fake biological model
  under `fixtures/` invites being mistaken for one.
- The negative controls here matter more than the positive case. A checker that only ever
  reports "ok" is indistinguishable from a checker that does nothing, so each guard is proven
  to *fire*: a medium component with no exchange reaction, a window no model could satisfy, and
  a physical-bound violation must each make the script exit non-zero.
- `run-embed-check.sh` needs no Nextflow at all: it calls `scripts/memote_html_section.py` and
  `scripts/quast_html_section.py` directly with `python3` (standard library only), which is why
  it is the first step of the smoke job. It exists because the stub path means those two scripts
  are **never executed** by any other check: a regression in them would not fail CI, it would
  just be missing from a 16 MB report.
- That check was mutation-tested rather than assumed to work: unescaping the `srcdoc`, renaming
  the section anchor, dropping the section header comment, and inserting an extra anchor between
  `quast` and `busco` each make it exit non-zero (with 4, 13, 2 and 1 failing assertions
  respectively), and the unmutated tree passes all 47.
- The failure injection is delivered through a config-level `beforeScript`, which is
  emitted verbatim into the task wrapper. A config directive is resolved long before a
  process input such as `sample` exists, so it cannot interpolate that input; the injector
  instead reads the task tag out of the wrapper's own `### name:` header. If a future
  Nextflow renames that header the injection stops firing — `run-failure-isolation.sh`
  asserts that it fired, so this degrades into a loud failure, never a false pass.
- `run-lint.sh` pins a warning baseline that is specific to the Nextflow version in
  `NEXTFLOW_VERSION`. Re-record it in the same commit as a deliberate version bump.
- Apart from `run-growth-check.sh` (cobra), these are fast, tool-free guards. The real
  acceptance test — ten steps, real tools, real reads — stays the manual procedure in
  [`docs/nextflow-port-design.md`](../../docs/nextflow-port-design.md) §14.
