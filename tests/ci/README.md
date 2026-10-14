# Automated checks (`tests/ci/`)

Every check here is a shell script. The GitHub workflow
([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) installs Nextflow and
calls the same scripts, so **CI and a local run execute identical assertions** and cannot
drift apart.

```bash
export PATH="/path/to/nextflow/bin:$PATH"     # Nextflow >= 25.10
bash tests/ci/run-lint.sh
bash tests/ci/run-preview.sh
bash tests/ci/run-disk-guard.sh
bash tests/ci/run-stub-smoke.sh
bash tests/ci/run-failure-isolation.sh
```

Each script exits non-zero on the first failed assertion and prints a `PASS/FAIL` line per
assertion. They are independent and can be run in any order.

## What each check proves

| Script | Proves |
|---|---|
| `run-lint.sh` | `nextflow lint main.nf nextflow.config` reports **no errors**, and the warning count does not exceed the pinned baseline (a ratchet, so lint debt cannot grow unnoticed). |
| `run-preview.sh` | `-preview` compiles `main.nf` and runs the workflow body without executing a task; and six malformed sample sheets (missing column, bad name, duplicate name, wrong field count, missing FASTQ, missing file) each abort **before a single task is submitted**. The bad sheets are run with `-stub-run`, not `-preview`, precisely so a task *would* be submitted if validation failed to fire. |
| `run-disk-guard.sh` | The F20 disk pre-flight passes at a low floor, **aborts before any task** with a message naming the shortfall and the cleanup commands when the floor is unmet, and is fully skipped with `--min_free_disk_gb=0`. |
| `run-stub-smoke.sh` | The complete 10-step DAG (23 tasks) runs to success with stubs, producing the full documented artifact tree — and the validated output tree at the repository root is **not modified** (the F18 regression guard: a stub run once published empty placeholders over real deliverables). |
| `run-failure-isolation.sh` | With one sample's first task deliberately killed: the run **exits 1** (never swallowed), the failed sample produces **no downstream deliverables**, the healthy sample's finished work **survives**, and `-resume` converges while reusing that work. It also re-measures the negative control: `errorStrategy = 'ignore'` — the only strategy that would continue independent branches — exits **0** with a dead task, which is why it is rejected (design doc D12). |

## Layout

| Path | Role |
|---|---|
| `ci.config` | Nextflow config overlay for every check: redirects all output/log/benchmark dirs and `workDir` under `tests/ci/out/`, and disables conda. Nothing a check does can touch validated data. |
| `inject-sample-failure.config` | Fails `FASTQC_RAW` for one sample (`CI_FAIL_SAMPLE`, default `CI_B`) through `beforeScript`, so `main.nf` carries no test hook. |
| `ignore-strategy.config` | Negative control for the rejected `errorStrategy = 'ignore'`. |
| `fixtures/` | Two-sample sheet with tiny synthetic gzipped FASTQ reads and a `bakta_db/version.json` sentinel. |
| `lib.sh` | Shared assertion helpers and the `nextflow` wrapper (`NXF_HOME` stays inside the project, ANSI-free logs make the greps stable). |
| `out/` | Scratch space; git-ignored, safe to delete at any time. |

## Notes

- **No conda environment is built** and no tool is executed: all checks use `-stub-run`
  (every process has a `stub:` block). Measured on the development machine: `run-lint.sh` 2 s,
  `run-stub-smoke.sh` 8 s, `run-disk-guard.sh` 16 s, `run-failure-isolation.sh` 27 s,
  `run-preview.sh` 33 s.
- The failure injection is delivered through a config-level `beforeScript`, which is
  emitted verbatim into the task wrapper. A config directive is resolved long before a
  process input such as `sample` exists, so it cannot interpolate that input; the injector
  instead reads the task tag out of the wrapper's own `### name:` header. If a future
  Nextflow renames that header the injection stops firing — `run-failure-isolation.sh`
  asserts that it fired, so this degrades into a loud failure, never a false pass.
- `run-lint.sh` pins a warning baseline that is specific to the Nextflow version in
  `NEXTFLOW_VERSION`. Re-record it in the same commit as a deliberate version bump.
- These are fast, tool-free guards. The real acceptance test — ten steps, real
  tools, real reads — stays the manual procedure in
  [`docs/nextflow-port-design.md`](../../docs/nextflow-port-design.md) §14.
