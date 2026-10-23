# WGSpipeline → Nextflow 移植 · 详细设计文档

| 项 | 值 |
|---|---|
| 版本 | v1.2（里程碑 0–4 完成，M5 对拍结论已回写） |
| 日期 | 2026-10-14 |
| 源管线 | `../WGSpipeline`（Snakemake，10 步；相对本仓库根目录） |
| 目标项目 | 本仓库（Nextflow DSL2） |
| 状态 | **已实施并验证**：里程碑 0–4 完成，产物与源仓库逐字节/数值对拍通过（见 §10 耗时对比、§17 里程碑表、§18 审查结论） |

---

## 1. 背景与目标

WGSpipeline 是一条细菌全基因组测序（WGS）管线，由单一 `snakefile` 驱动：

```
FastQC → Trimmomatic → FastQC → SPAdes → QUAST → BUSCO → Bakta → CarveMe → MEMOTE → MultiQC
```

本设计将整条管线移植到 Nextflow（DSL2），目标：

1. **功能 1:1 对等**——10 个步骤、输出目录树、报告行为全部一致；
2. **零改动复用关键资产**——8 个 conda 环境、`multiqc_extras.py`、`memote_report.py`、bakta AMR-guard 包装器、`multiqc_config.yaml` 原样复用；
3. **行为语义对等**——cohort 严格等于样本表、`report_tag` 多报告并存、单样本失败不拖垮其余样本（对应 `--keep-going`）、断点续跑（对应 Snakemake 缓存）；
4. **历史坑位全部保留对策**——CarveMe 覆盖 Bakta TSV、QUAST `--labels`、FastQC 假失败兜底、AMRFinderPlus 崩溃 guard 等（详见 §8 各 process 的"坑位"小节）。

## 2. 范围与非目标

**范围内：**
- Nextflow 版完整 10 步管线（DSL2，单 `main.nf` + `nextflow.config`，镜像"单一 snakefile"约定）；
- conda 执行后端（8 个 env yaml 原样复用，macOS/arm64 的四个 workaround 自动保留）；
- 样本表解析期校验、`report_tag`、耗时 benchmark 表、日志目录布局；
- 与现有 Snakemake 产物的对拍验证方案。

**非目标（明确不做）：**
- 不重推导 macOS/arm64 环境 workaround（BUSCO 5.8.3 / QUAST pip / SPAdes 4.0.0 / pyhmmer 0.10.15 pin）——随 env yaml 原样迁移；
- 不引入 Docker/Singularity（后续可作为独立 profile 追加）；
- 不改线程数字（**决策 D2**：保持原声明——`carveme` 声明 4 实际单线程、`spades` 声明 8 等一律原样）；
- 不改 `multiqc_config.yaml`、`multiqc_extras.py`、`memote_report.py` 的功能（`multiqc_extras.py` 对 benchmark 目录的读取契约保持不变，见 §10）；
- 不迁移 `raw/`、`db/` 数据（默认以参数指回原仓库，见 §3、§5）。

## 3. 新项目布局

新目录 `WGSpipeline-nextflow/` 与 `WGSpipeline/` 平级，是**独立项目**（自有 `work/`、自有输出目录树）。复用资产以**字节一致副本**形式纳入新仓库，与数据类资产（大文件）分离：

```
WGSpipeline-nextflow/
├── main.nf                     # DSL2：解析期校验 + 11 个 process（10 步 + F26 生长验证）+ workflow + onComplete
├── nextflow.config             # params / process / conda / profiles / workDir
├── samples.tsv                 # 队列样本表（自有一份，初始内容与原仓库一致）
├── multiqc_config.yaml         # 复用副本（字节一致）
├── multiqc_nextflow.yaml       # 新增：MultiQC 排版覆盖层（章节/列顺序 + MEMOTE 内嵌，F21）
├── envs/                       # 复用副本：8 个 conda env yaml（字节一致）
│   ├── fastqc.yaml  trimmomatic.yaml  spades.yaml  quast.yaml
│   ├── busco.yaml   bakta.yaml  carveme.yaml  memote.yaml
├── scripts/                    # 复用副本（字节一致）+ 少量新增
│   ├── multiqc_extras.py
│   ├── memote_report.py
│   ├── memote_html_section.py  # 新增：把 MEMOTE HTML 包成 MultiQC 内嵌段（F21）
│   ├── quast_html_section.py   # 新增：把 QUAST 自身产物包成 MultiQC 内嵌段（F24）
│   ├── check_growth.py         # 新增：限定介质下的生长验证断言（F25 / D16）
│   ├── bin/bakta               # AMR-guard 包装器入口
│   └── amr_guard/amr_chunk_guard.py
├── media/
│   └── m9_aerobic.yaml         # 新增：版本化的生理介质（组分取 CarveMe 自带 M9，F25）
├── bin/
│   └── task_env.sh             # 新增：任务内计时 helper（§10）
├── docs/
│   ├── nextflow-port-design.md # 本文档
│   └── asset-sources.md        # 复用资产来源 commit + sha256（决策 D4）
├── m0probe/                     # M0 验证探针（main.nf + config + 假 fastq，26.x 语法回归复测用）
├── .gitignore                  # 见下
├── work/                       # Nextflow workDir（运行时生成，gitignore）
├── .nextflow/                  # conda cacheDir + 运行缓存（gitignore）
└── data/ assembly/ annotation/ models/ reports/ logs/ benchmarks/
                                # 输出目录树，与 Snakemake 完全一致（运行时生成）
```

**`.gitignore` 草案：**

```
work/
.nextflow/
.nf-nextflow/
data/
assembly/
annotation/
models/
reports/
logs/
benchmarks/
.DS_Store
```

**默认数据指向（`nextflow.config` 的 params）：**

| 参数 | 默认值 | 说明 |
|---|---|---|
| `raw_dir` | `../WGSpipeline/raw` | 原始 fastq（相对 samples.tsv 的 fq 路径解析基准） |
| `bakta_db` | `../WGSpipeline/db/bakta/db-light` | Bakta 6.0 light 库 |
| `busco_db` | `../WGSpipeline/db/busco` | BUSCO 谱系缓存 |

三者均可命令行覆盖。**复用副本的同步策略**：`envs/`、`scripts/`、`multiqc_config.yaml` 在移植时与原仓库字节一致，并在 `docs` 里记录来源 commit；后续上游改动需手动同步（或改为 git submodule，实施时再定）。

## 4. 路径锚定（对应 `wfpath()` 的教训）

Snakemake 版的核心教训：shell 命令在**调用目录**执行，仓库内相对路径必须锚定。Nextflow 同样存在两组目录：

| 概念 | 含义 | 对应 |
|---|---|---|
| `projectDir`（`$projectDir`） | `main.nf` 所在目录 | `BASE_DIR` / `wfpath()` |
| `launchDir`（`$launchDir`） | `nextflow run` 的调用目录 | 调用目录，**不可依赖** |

**规则：所有仓库内路径（samples.tsv、scripts、envs、multiqc_config、logs、benchmarks、输出目录）一律用 `$projectDir` 锚定；task 的写入一律落到 `$projectDir` 下的绝对路径。** 实现方式：`nextflow.config` 顶部统一用 `projectDir` 展开成 `params.*` 绝对路径（§12），process 脚本里只引用 `params.*`，不出现裸相对路径。

## 5. 参数设计（config key → params 映射）

Snakemake 的 `config:` 键与 Nextflow `params` 一一对应，**同名保留**，便于文档与肌肉记忆迁移：

| Snakemake config | Nextflow param | 默认值 | 说明 |
|---|---|---|---|
| `threads_default` | `params.threads_default` | `8` | CPU 密集步骤（trimmomatic/spades） |
| `carveme_solver` | `params.carveme_solver` | `scip` | gurobi/cplex 需授权 |
| `memote_solver` | `params.memote_solver` | `glpk` | cobra 求解器 |
| `busco_lineage` | `params.busco_lineage` | `enterobacterales_odb10` | 改它会改变 BUSCO 产物路径 |
| `bakta_db` | `params.bakta_db` | `../WGSpipeline/db/bakta/db-light` | 见 §3 |
| `quast_min_contig` | `params.quast_min_contig` | `0` | 0 = 统计全部 contig |
| `raw_dir` | `params.raw_dir` | `../WGSpipeline/raw` | fq 相对路径解析基准 |
| `samples_tsv` | `params.samples_tsv` | `$projectDir/samples.tsv` | Snakemake 版为 `config.get("samples_tsv", ...)` |
| `report_tag` | `params.report_tag` | `""` | `""` → `multiqc_report.html`；`cohortB` → `multiqc_report_cohortB.html` |
| `trimmomatic.*` | `params.trim.*` | leading=3, trailing=3, slidingwindow="4:20", minlen=36 | 嵌套 dict → 嵌套 params |
| `trimmomatic_adapters` | `params.trimmomatic_adapters` | `""`（空 = 用 `$CONDA_PREFIX` 内自带） | fallback 顺序见 §8.2 |
| （spades `--careful`） | `params.spades_careful` | `"--careful"` | 原 snakefile 为 params.careful，`""` 关闭 |
| `--set-threads <rule>=N` | `-profile` 或 config override（`process.withName`） | 见 §12 | 语义对等物 |
| `--keep-going` | `process.errorStrategy = 'finish'` | 见 §12 | 语义对等物 |
| `--latency-wait 60` | N/A | — | Snakemake/APFS 特有问题，Nextflow 无对应物 |
| `--forcerun <rule>` | `-resume` + 删除对应产物（或改输入触发 hash 变化） | 见 §13 | 语义不同，覆盖同一需求 |

优先级：CLI `--key=value` > `nextflow.config` 默认值 > main.nf 内 `?:` 兜底（与原"CLI > config > 代码默认"一致）。Snakemake 的"`--config` 值是字符串需显式 int 转换"陷阱在 Nextflow 不存在（params 保留类型，`--threads_default 8` 即为 int）。

## 6. 样本表解析与校验（main.nf 解析期）

原 snakefile 在解析期用 pandas 校验样本表，**坏输入立即失败而非跑到一半才炸**。Nextflow 在 `main.nf` 顶层用 Groovy 实现同等校验：

```groovy
// ---- sample sheet: parse + validate at parse time (mirrors the snakefile) ----
def sheet_file = file(params.samples_tsv)
if (!sheet_file.exists()) { error "samples.tsv not found: ${sheet_file}" }

def sheet_lines = sheet_file.readLines()
def header      = sheet_lines[0].split('\t', -1)*.trim()
def REQUIRED    = ['sample', 'fq1', 'fq2']
def missing     = REQUIRED.findAll { !header.contains(it) }
if (missing) { error "samples.tsv: missing required column(s): ${missing}; found: ${header}" }

def SAMPLES     = [:]                 // LinkedHashMap: name -> [fq1, fq2, meta...]
def META_COLS   = header - REQUIRED
def name_re     = ~/[A-Za-z0-9._-]+/  // sample name becomes a path component
def resolve     = { fq, row ->        // F11: hoisted + untyped + .call() (strict parser)
    def p = fq.startsWith('/') ? fq : "${params.raw_dir}/${fq}"
    if (!file(p).exists()) {
        error "samples.tsv: row ${row}: fastq not found: ${p}"
    }
    p
}

sheet_lines[1..-1].eachWithIndex { line, idx ->
    if (line.trim().isEmpty()) { return }            // skip blank lines
    def cols = line.split('\t', -1)*.trim()
    if (cols.size() != header.size()) {
        error "samples.tsv: row ${idx + 2}: expected ${header.size()} fields, got ${cols.size()}"
    }
    def rec  = [header, cols].transpose().collectEntries()
    def name = rec.sample
    if (!name || !name_re.matcher(name).matches()) {
        error "samples.tsv: row ${idx + 2}: invalid sample name ${name}"
    }
    if (SAMPLES.containsKey(name)) { error "samples.tsv: duplicate sample name ${name}" }
    SAMPLES[name] = [resolve.call(rec.fq1, idx + 2), resolve.call(rec.fq2, idx + 2)] + META_COLS.collect { rec[it] }
}
def SAMPLE_NAMES = SAMPLES.keySet() as List

// report_tag: same filename-safety check as the snakefile
def tag_re = ~/[A-Za-z0-9._-]+/
if (params.report_tag && !tag_re.matcher(params.report_tag).matches()) {
    error "report_tag ${params.report_tag} may only use letters, digits, dot, underscore and dash"
}
params.report_name = "multiqc_report" + (params.report_tag ? "_${params.report_tag}" : "")
```

**M0 实测修正（F1，26.x 严格语法）：** 顶层语句与 process/workflow 声明不可混排——上面整段校验代码必须移入 `workflow { main: ... }` 块的最前面（任何 process 调用之前）。实测 `error "..."` 在 workflow 内可用、坏输入立即失败；`-preview`/`-stub` 都会执行 workflow 语句，校验同样生效。校验时机从"脚本解析期"变为"workflow 启动期"，fail-fast 语义不变（仍早于任何任务执行）。

行为对齐点：列检查、样本名正则（字母数字点下划线连字符）、重复名、fq 相对路径按 `raw_dir` 解析、空路径、文件存在性、坏行号报错信息——与 snakefile 逐条一致。元数据列保留但不参与 DAG（与现状一致）。

## 7. 通道与数据流

```
samples.tsv ──解析期校验──▶ SAMPLES(map) / SAMPLE_NAMES(list)
                                │
                    Channel.of(SAMPLE_NAMES) → tuple(sample, fq1, fq2)
                                │
        ┌───────────────────────┼───────────────────────────────┐
        │ flatMap → (sample,read,fq)        │                   │
        ▼                                    ▼                   │
  FASTQC_RAW (read=1/2)              TRIMMOMATIC                │
        │                                    │ 4 个 paired/unpaired 输出
        │ flatMap(paired 1/2) ◄──────────────┤
        ▼                                    ▼
  FASTQC_TRIMMED                          SPADES
        │                                    │ contigs.fasta
        │                              ┌─────┼──────────┐
        │                              ▼     ▼          ▼
        │                            QUAST  BUSCO      BAKTA
        │                              │     │          │ faa
        │                              │     │          ▼
        │                              │     │        CARVEME
        │                              │     │          │ xml
        │                              │     │          ▼
        │                              │     │        MEMOTE
        │                              │     │          │ summary
        ▼                              ▼     ▼          ▼
   ──────────── 显式 per-sample 文件（zip / report.tsv / txt / xml / summary）────────────
        │ 各自 .collect() 为列表（report 的 cohort = 这张样本表，绝不扫目录）
        ▼
     MULTIQC（val SAMPLE_NAMES + 各文件列表）──▶ reports/multiqc/multiqc_report[_tag].html
```

workflow 主干（DSL2）：

```groovy
workflow {
    main:
    // M0 修正（F1）：§6 的样本表校验代码放在这里（main: 节最前、任何 process 调用之前）
    // M1 修正（F12）：Channel.fromList —— 26.x 的 Channel.of(collection) 不再拆列表元素
    sample_ch = Channel.fromList(SAMPLE_NAMES).map { n ->
        tuple(n, file(SAMPLES[n][0]), file(SAMPLES[n][1]))
    }

    FASTQC_RAW(sample_ch.flatMap { n, f1, f2 -> [tuple(n, '1', f1), tuple(n, '2', f2)] })

    TRIMMOMATIC(sample_ch)
    FASTQC_TRIMMED(TRIMMOMATIC.out.flatMap { n, r1p, r2p, r1u, r2u ->
        [tuple(n, '1', r1p), tuple(n, '2', r2p)]
    })

    SPADES(TRIMMOMATIC.out.map { n, r1p, r2p, r1u, r2u -> tuple(n, r1p, r2p) })
    QUAST(SPADES.out)
    BUSCO(SPADES.out)
    BAKTA(SPADES.out.map { n, contigs ->
        tuple(n, contigs, file("${params.bakta_db}/version.json"))   // db-upgrade sentinel
    })
    CARVEME(BAKTA.out.map { n, gff3, faa, tsv, ffn, gbff, txt -> tuple(n, faa) })
    MEMOTE(CARVEME.out.map { n, xml, faa, tsv -> tuple(n, xml) })

    MULTIQC(
        Channel.value(SAMPLE_NAMES),                          // staging loop 遍历的 cohort
        Channel.empty()
            .mix(FASTQC_RAW.out.map    { n, r, z, h -> z })
            .mix(FASTQC_TRIMMED.out.map{ n, r, z, h -> z })
            .mix(QUAST.out.map         { n, html, txt, tsv -> tsv })
            .collect(),                                       // 显式 QC 文件列表（不扫目录）
        BUSCO.out.map { n, j, t -> t }.collect(),             // short_summary txt
        CARVEME.out.map { n, xml, faa, tsv -> xml }.collect(),
        MEMOTE.out.map { n, html, json, tsv -> tsv }.collect(),
        Channel.of(file(params.multiqc_config)).collect()
    )

    // M0 修正（F2）：26.x 用块内 onComplete: 节（失败退出时同样执行）
    onComplete:
    log.info "[WGSpipeline-nf] workflow dir = ${params.project_dir}"
    log.info "[WGSpipeline-nf] report       = ${params.multiqc_dir}/${params.report_name}.html"
    log.info "[WGSpipeline-nf] rerun a step = nextflow run . -resume (delete the output to force it)"
}
```

要点：
- **M0 实测修正（F2）**：26.x 已移除 `workflow.onComplete { }` 处理器；上文代码块已改为块内 `onComplete:` 节（注意：一旦出现 `onComplete:`/`onError:` 节，`main:` 标签不可省略）；`onComplete:` 节在失败退出时同样执行；
- **QUAST/BUSCO/BAKTA 都只消费 `SPADES.out` 的 `(sample, contigs)`**——与 snakefile 中三者并行于 spades 之后一致；
- BAKTA 额外带 `db/version.json` 作为 staged input：库升级 → input hash 变化 → `-resume` 自动重跑注释（Snakemake 版用同一 sentinel 触发 provenance）；
- MultiQC 的队列范围 = 通道里实际存在的样本 = 样本表，与 Snakemake"显式 per-sample 文件而非目录扫描"纪律一致（§9）。

## 8. 各 process 详细设计

通用约定（每个 process 的 script 前四行与最后一行）：

```sh
source ${params.bin_dir}/task_env.sh
export BENCH_DIR=${params.bench_dir}
bench_start
mkdir -p ${params.logs_dir}/<step>      # 有日志的步骤
... 主命令 ...
bench_finish <step> <sample或"">        # 见 §10
```

所有工具在 task 的私有 workdir 内执行（输出写到 `out/` 子目录或当前目录），再由 `publishDir` 发布到仓库目录——**Snakemake 版的 `rm -rf outdir` 幂等技巧在 Nextflow 里天然由"每次任务全新 workdir + publish 覆盖"替代**，无需再 rm（BAKTA 例外保留 `--force`，见 §8.7）。

**M0 实测修正（F3，publishDir 写法）：** 26.x 下 `publishDir "${dir}/${sample}"` 字符串形式在解析期报 `No such variable`；引用 input 变量的各 process（§8.3–8.8）一律改闭包形式 `publishDir(path: { "${dir}/${sample}" }, ...)`，实测按样本正确发布到独立目录。不引用 input 变量的 publishDir（如 §8.1 FASTQC、§9 MULTIQC）字符串形式不受影响。**M2 实测修正（F15）：** `pattern` 不会剥离匹配前缀——`out/contigs.fasta` 会发布成 `<目标>/out/contigs.fasta`，多出一层 `out/`；所有"工具写 out/ 子目录"的 process（§8.3–8.8）必须加 `saveAs: { filename -> filename - 'out/' }` 去前缀（pattern 用 `out/**`）。

---

### 8.1 FASTQC_RAW / FASTQC_TRIMMED

| 项 | 值 |
|---|---|
| conda | `envs/fastqc.yaml`（fastqc 0.12.1 + multiqc 1.21） |
| cpus | 4（与 snakefile 一致） |
| publish | `data/qc/raw`（RAW）/ `data/qc/trimmed`（TRIMMED），mode: copy |
| 输入 | `tuple(sample, read, fq)`；TRIMMED 的 fq 为 `<sample>_R<read>_paired.fastq.gz` |
| 输出 | `tuple(sample, read, zip, html)` |

```groovy
process FASTQC_RAW {
    tag { "${sample}_${read}" }
    conda "${params.envs_dir}/fastqc.yaml"
    cpus 4
    publishDir "${params.raw_qc_dir}", mode: 'copy', overwrite: true

    input:  tuple val(sample), val(read), path(fq)
    output: tuple val(sample), val(read),
            path("${sample}_${read}_fastqc.zip"), path("${sample}_${read}_fastqc.html")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/fastqc_raw
    fastqc ${fq} -o . --threads ${task.cpus} \\
        > ${params.logs_dir}/fastqc_raw/${sample}_${read}.log 2>&1 \\
        || [ -f ${sample}_${read}_fastqc.html ]
    bench_finish fastqc_raw ${sample}_${read}
    """
}
```

FASTQC_TRIMMED 同构，输出名 `<sample>_R<read>_paired_fastqc.{zip,html}`，log 目录 `fastqc_trimmed`，benchmark step `fastqc_trimmed`。

**坑位（保留）：** fastqc 0.12 在 macOS 下偶发"产物已写出但退出码非 0"——`|| [ -f <html> ]` 兜底原样保留；真正失败（无产物）时 shell 仍非 0，Nextflow 会捕获。
**对齐点：** input 文件名即 fastqc 输出前缀的来源（`<sample>_<read>.fastq.gz` → `<sample>_<read>_fastqc.*`），staging 不改名即可与 Snakemake 输出名一致。

### 8.2 TRIMMOMATIC

| 项 | 值 |
|---|---|
| conda | `envs/trimmomatic.yaml`（trimmomatic 0.39 + openjdk 17） |
| cpus | `params.threads_default`（8） |
| publish | `data/clean_trimmomatic` |
| 输入 | `tuple(sample, fq1, fq2)` |
| 输出 | `tuple(sample, r1_paired, r2_paired, r1_unpaired, r2_unpaired)` |

```groovy
process TRIMMOMATIC {
    tag "${sample}"
    conda "${params.envs_dir}/trimmomatic.yaml"
    cpus params.threads_default
    publishDir "${params.trim_dir}", mode: 'copy', overwrite: true

    input:  tuple val(sample), path(fq1), path(fq2)
    output: tuple val(sample),
            path("${sample}_R1_paired.fastq.gz"),    path("${sample}_R2_paired.fastq.gz"),
            path("${sample}_R1_unpaired.fastq.gz"),  path("${sample}_R2_unpaired.fastq.gz")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/trimmomatic

    # Adapter file: param wins (snakefile semantics), fall back to the copy
    # shipped in the conda env. NOTE (F10): shell vars in script strings use
    # single-backslash escaping — "\\$" would trigger Groovy interpolation.
    ADAPTERS="${params.trimmomatic_adapters}"
    [ -n "\$ADAPTERS" ] || ADAPTERS="\${CONDA_PREFIX}/share/trimmomatic/adapters/TruSeq3-PE.fa"

    trimmomatic PE -threads ${task.cpus} -phred33 ${fq1} ${fq2} \\
        ${sample}_R1_paired.fastq.gz ${sample}_R1_unpaired.fastq.gz \\
        ${sample}_R2_paired.fastq.gz ${sample}_R2_unpaired.fastq.gz \\
        ILLUMINACLIP:\$ADAPTERS:2:30:10 \\
        LEADING:${params.trim.leading} TRAILING:${params.trim.trailing} \\
        SLIDINGWINDOW:${params.trim.slidingwindow} MINLEN:${params.trim.minlen} \\
        > ${params.logs_dir}/trimmomatic/${sample}.log 2>&1
    bench_finish trimmomatic ${sample}
    """
}
```

**对齐点：** 参数值与原 snakefile 完全一致（phred33、ILLUMINACLIP:2:30:10、LEADING:3、TRAILING:3、SLIDINGWINDOW:4:20、MINLEN:36）。unpaired 输出保留（不参与组装，与原版一致）。**M1 修正（决策 D11）：** adapter 优先级按 snakefile 语义为"param 优先、空则 `$CONDA_PREFIX` 自带"（原草案的 env-first 顺序与源行为相反，已改）。

### 8.3 SPADES

| 项 | 值 |
|---|---|
| conda | `envs/spades.yaml`（spades 4.0.0，osx-arm64 workaround） |
| cpus | `params.threads_default`（8） |
| publish | `assembly/spades/<sample>/`（pattern `out/*`） |
| 输入 | `tuple(sample, r1_paired, r2_paired)` |
| 输出 | `tuple(sample, path("out/contigs.fasta"))` |

```groovy
process SPADES {
    tag "${sample}"
    conda "${params.envs_dir}/spades.yaml"
    cpus params.threads_default
    publishDir(path: { "${params.assembly_dir}/${sample}" }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })   // F15

    input:  tuple val(sample), path(r1_paired), path(r2_paired)
    output: tuple val(sample), path("out/contigs.fasta")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/spades
    mkdir -p out
    spades.py -1 ${r1_paired} -2 ${r2_paired} -o out \\
        --threads ${task.cpus} ${params.spades_careful} \\
        > ${params.logs_dir}/spades/${sample}.log 2>&1
    bench_finish spades ${sample}
    """
}
```

**对齐点：**
- `contigs.fasta` 为管线终点；scaffolds/assembly_graph 等随 `out/` 一并 publish（与"SPAdes 侧产物随 -o 目录产出"一致）；
- `--careful` 保留（约 +30% 耗时；`--spades_careful ''` 关闭）；
- Snakemake 的 `rm -rf` 由全新 workdir 替代（SPAdes 拒绝已存在目录的报错不会再触发）。

### 8.4 QUAST

| 项 | 值 |
|---|---|
| conda | `envs/quast.yaml`（python 3.10 + pip quast 5.2.0，osx-arm64 workaround） |
| cpus | 1（macOS 上多线程偶发不稳，与原版一致） |
| publish | `data/qc/quast/<sample>/`（pattern `out/*`） |
| 输入 | `tuple(sample, contigs)` |
| 输出 | `tuple(sample, report.html, report.txt, report.tsv)` |

```groovy
process QUAST {
    tag "${sample}"
    conda "${params.envs_dir}/quast.yaml"
    cpus 1
    publishDir(path: { "${params.quast_dir}/${sample}" }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })   // F15

    input:  tuple val(sample), path(contigs)
    output: tuple val(sample), path("out/report.html"), path("out/report.txt"), path("out/report.tsv")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/quast
    mkdir -p out
    quast.py ${contigs} -o out \\
        --labels ${sample} \\
        --min-contig ${params.quast_min_contig} \\
        --threads 1 > ${params.logs_dir}/quast/${sample}.log 2>&1
    bench_finish quast ${sample}
    """
}
```

**坑位（两条都保留）：**
1. **`--labels ${sample}` 必须保留**——输入恒为 `contigs.fasta`，否则所有样本在 QUAST/MultiQC 里都叫 "contigs" 互相覆盖（known-issues 相关注释）；
2. **入口是 `quast.py` 而非 `quast`**——pip 版只有 `quast.py` entry point（裸 `quast` 会 exit 127）。
**对齐点：** `--min-contig 0` 统计全部 contig；`report.tsv` 作为输出捕获并喂给 MultiQC（`multiqc_cohort_files()` 的一部分）。

### 8.5 BUSCO

| 项 | 值 |
|---|---|
| conda | `envs/busco.yaml`（busco 5.8.3，osx-arm64 workaround） |
| cpus | 4 |
| publish | `data/qc/busco/<sample>/`（pattern `out/<sample>/*`） |
| 输入 | `tuple(sample, contigs)` |
| 输出 | `tuple(sample, short_summary json, short_summary txt)` |

```groovy
process BUSCO {
    tag "${sample}"
    conda "${params.envs_dir}/busco.yaml"
    cpus 4
    publishDir "${params.busco_dir}", pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })   // F15: 静态路径 + 去前缀，out/<sample>/... 落进 busco/<sample>/...

    input:  tuple val(sample), path(contigs)
    output: tuple val(sample),
            path("out/${sample}/short_summary.specific.${params.busco_lineage}.${sample}.json"),
            path("out/${sample}/short_summary.specific.${params.busco_lineage}.${sample}.txt")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/busco
    mkdir -p out
    # Explicit interpreter: bin/busco's shebang resolves via PATH; under conda
    # activation that can pick a different python3 lacking the busco module.
    "\\${CONDA_PREFIX}/bin/python3" "\\${CONDA_PREFIX}/bin/busco" \\
        -i ${contigs} -m genome \\
        -l ${params.busco_lineage} \\
        -o ${sample} --out_path out \\
        --cpu ${task.cpus} \\
        --download_path ${params.busco_db} \\
        > ${params.logs_dir}/busco/${sample}.log 2>&1
    bench_finish busco ${sample}
    """
}
```

**对齐点：** 显式 `$CONDA_PREFIX/bin/python3` 调用保留（shebang 解析坑的防御）；`--download_path` 指向共享 db 缓存；首跑自动下载谱系（约数百 MB）。
**注意：** 改 `busco_lineage` 会改变 short_summary 文件名（输出模板与 publish 同步变化，与 snakefile 注释一致）。

### 8.6 BAKTA

| 项 | 值 |
|---|---|
| conda | `envs/bakta.yaml`（bakta 1.11.4 + pyhmmer 0.10.15 pin） |
| cpus | 4 |
| publish | `annotation/bakta/<sample>/`（pattern `out/*`） |
| 输入 | `tuple(sample, contigs, db_version)` |
| 输出 | `tuple(sample, gff3, faa, tsv, ffn, gbff, txt)` |

```groovy
process BAKTA {
    tag "${sample}"
    conda "${params.envs_dir}/bakta.yaml"
    cpus 4
    publishDir(path: { "${params.annot_dir}/${sample}" }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })   // F15

    input:  tuple val(sample), path(contigs), path(db_version)
    output: tuple val(sample),
            path("out/${sample}.gff3"), path("out/${sample}.faa"), path("out/${sample}.tsv"),
            path("out/${sample}.ffn"),  path("out/${sample}.gbff"), path("out/${sample}.txt")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/bakta
    mkdir -p out

    # Prepend the AMR-guard wrapper dir so it shadows the conda bakta entry point
    # (upstream ncbi/amr#191: one protein with overlapping NCBIFAM hits kills the run).
    export PATH="${params.scripts_dir}/bin:\\$PATH"

    bakta --db ${params.bakta_db} \\
        --prefix ${sample} \\
        --output out \\
        --threads ${task.cpus} \\
        --force --skip-plot ${contigs} \\
        > ${params.logs_dir}/bakta/${sample}.log 2>&1
    bench_finish bakta ${sample}
    """
}
```

**与 Snakemake 版的差异（有意为之）：**
1. **删除 `2> >(tee -a ...)` 进程替换**——Nextflow 原生把 stdout/stderr 都收进 `.command.log`，不再需要 tee 分流；代价是 bakta 的 alive-progress 进度条变非 tty 而关闭（纯外观损失）。stderr 合并进同一 log（`> ... 2>&1`），崩溃 traceback 仍可查；
2. **`--output out` 写进 workdir**，`--force` 保留以覆盖已 mkdir 的目录（Snakemake 的 rm -rf 不再需要）；
3. `db_version.json` 作为 staged input：库升级自动触发 `-resume` 重跑（sentinel 语义保留）。

**坑位（保留）：** AMR-guard 包装器（`scripts/bin/bakta` + `scripts/amr_guard/`）通过 PATH 前置生效；包装器 shebang 是 `#!/usr/bin/env python`，conda 激活后 `env python` 解析到环境内 python，可正常 import bakta.main。`--skip-plot` 保留；`--meta` 不开（分离株，非宏基因组）。

### 8.7 CARVEME

| 项 | 值 |
|---|---|
| conda | `envs/carveme.yaml`（carveme 1.6.6 + diamond 2.2.8 + pyscipopt 5.7.1 + scip 9.2.4） |
| cpus | 4（**决策 D2：保持原声明**；carve 本身单线程，4 只是沿袭） |
| publish | `models/carveme/<sample>/`（pattern `out/*`） |
| 输入 | `tuple(sample, faa)` |
| 输出 | `tuple(sample, xml, faa_copy, tsv)`（tsv 为 carve 的 diamond 中间表，随目录 publish） |

```groovy
process CARVEME {
    tag "${sample}"
    conda "${params.envs_dir}/carveme.yaml"
    cpus 4
    publishDir(path: { "${params.carveme_dir}/${sample}" }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })   // F15

    input:  tuple val(sample), path(faa)
    output: tuple val(sample), path("out/${sample}.xml"), path("out/${sample}.faa"), path("out/${sample}.tsv")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/carveme
    mkdir -p out

    # CRITICAL (docs/known-issues.md #5): carve derives its diamond output path from
    # the input path (stem + '.tsv'). Feeding bakta's faa directly would overwrite
    # annotation/bakta/<sample>/<sample>.tsv. Always carve this staged private copy.
    cp ${faa} out/${sample}.faa
    carve out/${sample}.faa -o out/${sample}.xml \\
        --solver ${params.carveme_solver} \\
        --verbose > ${params.logs_dir}/carveme/${sample}.log 2>&1
    bench_finish carveme ${sample}
    """
}
```

**坑位（保留）：** staged copy trick 原样移植——**绝不允许把 bakta 的 `.faa` 原路径喂给 carve**。`--verbose` 必加（progress print 全被 `if verbose:` 守卫；本步是全流程最慢单步）。`diamond` 中间表落在 `out/${sample}.tsv`（CarveMe 自己的目录），publish 后与 Snakemake 产物布局一致（`models/carveme/<sample>/{faa,tsv,xml}`）。

### 8.8 MEMOTE

| 项 | 值 |
|---|---|
| conda | `envs/memote.yaml`（memote 0.17.0，cobra/glpk） |
| cpus | 4（沿袭原声明；memote 自身主要单线程） |
| publish | `models/memote/<sample>/`（pattern `out/*`） |
| 输入 | `tuple(sample, model_xml)` |
| 输出 | `tuple(sample, index.html, memote_result.json, memote_summary.tsv)` |

```groovy
process MEMOTE {
    tag "${sample}"
    conda "${params.envs_dir}/memote.yaml"
    cpus 4
    publishDir(path: { "${params.memote_dir}/${sample}" }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })   // F15

    input:  tuple val(sample), path(model)
    output: tuple val(sample), path("out/index.html"),
            path("out/memote_result.json"), path("out/memote_summary.tsv")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/memote
    mkdir -p out
    "\\${CONDA_PREFIX}/bin/python3" ${params.scripts_dir}/memote_report.py ${model} \\
        --outdir out --sample ${sample} --solver ${params.memote_solver} \\
        > ${params.logs_dir}/memote/${sample}.log 2>&1
    bench_finish memote ${sample}
    """
}
```

**对齐点：** 一次运行三产物（HTML + 完整 JSON + 评分摘要 TSV），脚本零改动；`$CONDA_PREFIX/bin/python3` 显式解释器调用保留（cobra 模块解析坑的防御）。

## 9. MULTIQC 汇聚 process 设计

单 process 内五段（对应 snakefile `rule multiqc` 的 1→2→3 顺序，另加 2b 的 MEMOTE 内嵌与 2c 的 QUAST 自身产物内嵌），队列范围严格等于样本表：

| 项 | 值 |
|---|---|
| conda | `envs/fastqc.yaml`（含 multiqc 1.21） |
| cpus | 1 |
| 输入 | `val report_name`（F19：运行时算出的报告名走 channel，不用运行时 params 变更）+ `val samples`（SAMPLE_NAMES 列表）+ `val upstream`（收集触发，保证上游完成）+ `path multiqc_config`（原样复用）+ `path multiqc_config_extra`（F21 排版覆盖层）+ `path growth_section`（F26：生长验证表，它同时构成对 GROWTH_CHECK 的依赖） |
| 输出 | `path("${report_name}.html")` → publish 到 `reports/multiqc/` |

**报告排版（F21 + F24，用户要求）：**

1. **章节与 General Statistics 列一律按管线步骤排序**（FastQC → QUAST → BUSCO → CarveMe → MEMOTE → 耗时 → 版本），由 `multiqc_nextflow.yaml` 覆盖层实现——该覆盖层以第二个 `-c` 叠加，**复用配置 `multiqc_config.yaml` 保持字节一致**（决策 D4 不受影响）；
2. **MEMOTE 的完整 `index.html` 内嵌进总报告**：MEMOTE 是自带 CSS/JS 的完整 Angular 页面（约 4.3 MB/样本），直接内联会与 MultiQC 页面互相污染（其 `<base href="/">` 会改变整页基准 URL），故由 `scripts/memote_html_section.py` 包成 `<iframe srcdoc="…">`——iframe 是独立文档，CSS/JS 相互隔离，`srcdoc` 让报告保持自包含（`file://` 或 HTTP 打开都能渲染）。代价：报告由 4.9 MB 增至 15.2 MB。
3. **QUAST 自身产物内嵌进总报告（F24）**：`report.tsv` 早已被内置 `quast` 模块与 `multiqc_extras.py` 消费，此处补齐另外两个产物——完整交互报告 `report.html`（约 380 KB/样本，自带 Bootstrap CSS 与 22 段内联 JS，无 `<base>`、无外部资源引用）与纯文本摘要 `report.txt`。由 `scripts/quast_html_section.py` 生成**单个** `quast_full_mqc.html`（内含每样本一个 `<iframe srcdoc>` + 折叠的 `report.txt`），在覆盖层中以固定锚点 `quast_full: {order: 850}` 排在 QUAST 表之后、BUSCO 之前。

```groovy
process MULTIQC {
    tag "${report_name}"
    conda "${params.envs_dir}/fastqc.yaml"
    cpus 1
    publishDir "${params.multiqc_dir}", mode: 'copy', overwrite: true

    input:
    val  report_name          // F19: 报告名作为 channel 输入（process 指令看不到运行时 params 变更）
    val  samples              // List<String>: cohort, from the sheet
    val  upstream             // 收集触发：所有上游步骤完成后再跑
    path multiqc_config

    output:
    path("${report_name}.html")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}

    # ---- 1. custom content（先清空再生成：数据源缺失时脚本会跳过写文件，
    #          残留 TSV 会把上一个更大队列的样本"泄漏"进本次报告）----
    rm -rf ${params.cc_dir}
    "\\${CONDA_PREFIX}/bin/python3" ${params.scripts_dir}/multiqc_extras.py \\
        --samples-tsv ${params.samples_tsv} \\
        --quast-dir ${params.quast_dir} \\
        --carveme-dir ${params.carveme_dir} \\
        --memote-dir ${params.memote_dir} \\
        --bench-dir ${params.bench_dir} \\
        --outdir ${params.cc_dir} \\
        >> ${params.logs_dir}/multiqc.log 2>&1

    # ---- 2. BUSCO 摘要暂存（按样本表软链，不 glob 整个 busco 目录）----
    rm -rf busco_stage
    mkdir -p busco_stage
    for s in ${samples.join(' ')}; do
        set -- ${params.busco_dir}"/\\$s"/short_summary.specific.*.txt
        if [ -f "\\$1" ]; then
            ln -sf "\\$1" busco_stage/short_summary_"\\$s".txt
        fi
    done

    # ---- 2b. MEMOTE 完整报告内嵌段（F21；每样本一个 *_mqc.html，
    #          MEMOTE 自带 CSS/JS 的完整页面 -> 包成独立 iframe，避免互相污染）----
    rm -rf memote_html
    mkdir -p memote_html
    "\\${CONDA_PREFIX}/bin/python3" ${params.scripts_dir}/memote_html_section.py \\
        --memote-dir ${params.memote_dir} \\
        --samples "${samples.join(' ')}" \\
        --outdir memote_html \\
        --report-dir ${params.multiqc_dir} \\
        >> ${params.logs_dir}/multiqc.log 2>&1

    # ---- 2c. QUAST 自身输出文件内嵌段（F24；report.html 完整交互报告 +
    #          report.txt 纯文本。全队列只出一个 *_mqc.html：report_section_order
    #          的锚点是精确匹配、不支持通配，逐样本 id 排不到 QUAST 表旁）----
    rm -rf quast_html
    mkdir -p quast_html
    "\\${CONDA_PREFIX}/bin/python3" ${params.scripts_dir}/quast_html_section.py \\
        --quast-dir ${params.quast_dir} \\
        --samples "${samples.join(' ')}" \\
        --outdir quast_html \\
        --report-dir ${params.multiqc_dir} \\
        >> ${params.logs_dir}/multiqc.log 2>&1

    # ---- 3. 只喂本队列的显式文件（不是目录！）----
    # -c 两次：先原样复用的配置，再叠排版覆盖层（章节/列顺序 + MEMOTE / QUAST 内嵌）
    multiqc ${qc_files.join(' ')} busco_stage ${params.cc_dir} memote_html quast_html \\
        -c ${params.multiqc_config} \\
        -c ${params.multiqc_config_extra} \\
        -o . -f -n ${params.report_name} \\
        >> ${params.logs_dir}/multiqc.log 2>&1

    bench_finish multiqc ""
    """
}
```

**对齐点与差异：**
- 五段结构与 snakefile 逐段对应：custom content 清空+生成 → BUSCO 软链 staging（命名 `short_summary_<sample>.txt`，匹配 MultiQC 的 fn 模式且 trim 出干净样本名）→ **MEMOTE 完整 HTML 内嵌** → **QUAST 自身产物内嵌（F24）** → `multiqc` 显式文件列表；
- `qc_files` 列表内容 = `multiqc_cohort_files()` 的产物（4 个 fastqc zip + `report.tsv` × 样本数），**built-in 模块绝不扫目录**（否则子集报告里 custom content 是子集、FastQC/QUAST 却是全队列，自相矛盾）；
- 差异一：`multiqc` 的 `-o .` 写进 workdir，再由 publishDir 发布 `reports/multiqc/`（Snakemake 直写仓库目录）；custom content 仍写仓库 `reports/multiqc/custom_content/`（与原布局一致，每次先清空）；
- 差异二：busco staging 目录改在 workdir（Snakemake 在仓库目录）——纯临时物，不进仓库更干净；软链源仍是仓库内的 BUSCO 产物（绝对路径，不会拼出无效路径）；
- 差异三（F21 + F24）：报告章节/General Stats 列按管线顺序排列，且 MEMOTE 与 QUAST 的自身报告完整内嵌——Snakemake 版无此三项，属移植版新增的呈现能力（不改任何数值产物）；
- `-c` 显式指定 `multiqc_config.yaml` + `multiqc_nextflow.yaml`；两者都作为 input，改任一个都会改变 MULTIQC 的 input hash → `-resume` 自动重跑报告。

## 10. 计时与 benchmark 适配（`bin/task_env.sh`）

`multiqc_extras.py` 的耗时表读取契约**不可破坏**：`benchmarks/<step>/<sample>.tsv`（per-sample）与 `benchmarks/<step>.tsv`（流水线级，如 `multiqc.tsv`），TSV 需含 `s` 与 `h:m:s` 两列。Snakemake 的 `benchmark:` 由引擎写入；Nextflow 无对等物，改由**任务内自计时**产出同格式文件，使 `multiqc_extras.py` 一行不改：

```sh
# bin/task_env.sh — write Snakemake-format benchmark TSVs so that
# scripts/multiqc_extras.py keeps working unchanged.
# Requires $BENCH_DIR in the environment (set by the task before sourcing).

bench_start() { _bench_t0=$(date +%s); }

bench_finish() {
    step="$1"; sample="$2"
    t1=$(date +%s); secs=$((t1 - _bench_t0))
    hms=$(awk -v s="$secs" 'BEGIN { printf "%d:%02d:%02d", int(s/3600), int(s%3600/60), s%60 }')
    if [ -n "$sample" ]; then
        out="$BENCH_DIR/$step/$sample.tsv"
    else
        out="$BENCH_DIR/$step.tsv"          # pipeline-level row (e.g. multiqc)
    fi
    mkdir -p "$(dirname "$out")"
    printf 's\th:m:s\n%d\t%s\n' "$secs" "$hms" > "$out"
}
```

约定：
- 每个 process 的 script 以 `bench_start` 开头、`bench_finish <step> [<sample>]` 结尾（见 §8 各代码块）；fastqc 类 step 的 sample 为 `<sample>_<read>`，与原 benchmark 文件名一致；
- MULTIQC 调用 `bench_finish multiqc ""` → `benchmarks/multiqc.tsv`，对应"all samples"行；
- 已知偏差（可接受）：MULTIQC 任务运行时，`benchmarks/multiqc.tsv` 是**上一次**运行留下的（`collect_benchmarks` 对缺失文件本就优雅跳过），首跑时"all samples"行缺席——与 Snakemake 首跑行为一致；
- 资源列（max_rss 等）不写——本机 Snakemake benchmark 的资源列本来就是全 `NA`（known-issues #3），只有 wall time 可用；如需资源数据，用 Nextflow 自己的 `-with-report`（execution report HTML）补充。

**耗时可比性说明（2026-10-14 复盘，勿误读报告里的 runtime 表）：**

- `pipeline_runtime` 表是**各步骤 wall time 的逐列明细**；把一行横向相加得到的是**各步耗时之和**，不是整条管线的墙钟时长——并行步骤（QUAST/BUSCO/BAKTA 同批、两样本并行）会被重复计入，因此"求和值"天然大于真实墙钟。与 Snakemake 的"整条管线 40 分钟"直接对比属于**口径不同**。
- 首次移植验证的这批数字**受机器环境严重干扰，不代表管线固有成本**。当时实测证据：整机 load average **18–33**（8 核机器），且磁盘**近乎写满（460 GiB 用 431 GiB，仅剩 2.3 GiB / 100%）**——Spades/Bakta/CarveMe 都是写密集型，写满的卷会显著拖慢 I/O，其后还直接导致 SPAdes `gzip` 崩溃（F20）。
- 同机同参数的两个样本差异可达 **30×**（BUSCO：SRR1770413 用 979 s vs SRR10194530 用 32 s），这是环境干扰占主导的直接证据，而非管线逻辑差异。
- 已排除的因素：线程数正确（Spades 日志 `--threads 8`）；Nextflow 26.x 本地执行器**确实按 `cpus` 预算调度**（受控实验：4 个 `cpus=4` 任务在 8 核上严格两两串行；真实运行中第二个 BUSCO 被排队到有空槽才提交）；`bench_start/bench_finish` 精确夹住工具命令，测量本身无偏差。
- 结论：**在空闲机器 + 充足磁盘上重跑，数值才与 Snakemake 基线可比。** 报告里那张表应连同本说明一起阅读。

**单步对照复测（2026-10-14 09:44 起，空闲机器 load ≈ 3，磁盘 25 GiB 可用；同一机器/同一输入/同一命令/同一 conda 环境）：**

| 步骤（样本） | Snakemake 基线¹ | 受干扰运行 | 空闲机器复测 | 复测/基线 |
|---|---|---|---|---|
| Bakta (SRR1770413) | 291 s (4:51) | 3780 s (63:00) | **227 s (3:46)** | **0.78×（更快）** |
| SPAdes (SRR1770413) | 558 s (9:18) | 7173 s (119:33) | **446 s (7:26)** | **0.80×（更快）** |

¹ 基线取自源仓库 `docs/known-issues.md` §4 的表（由 `.snakemake/metadata/` 的 starttime/endtime 提取）。

- **结论（已证实）**：管线本身**没有性能缺陷**——空闲环境下两个重型步骤都比 Snakemake 基线**更快**；此前 13–17× 的膨胀完全来自外部环境（满盘 + 3–4 倍超订负载）。按复测节奏推算，空闲机器上两样本整轮墙钟应与 Snakemake 的 ~51 分钟相当。
- 顺带确认可复现性：复测产出的 `contigs.fasta` 与**已发布产物及源仓库 Snakemake 产物三者逐字节一致**；Bakta tsv 行数一致（4891）。
- 复测暴露的规律：**轻量步骤（fastqc/trimmomatic/quast）当时几乎不受影响（≈1.0×），只有重型、写密集步骤爆炸（3–30×）**——这是"满盘 I/O 压力"而非"CPU 被抢"的典型特征。

**全量重跑（2026-10-14 12:47–13:25，空闲机器 load ≈ 2.6、磁盘 23–25 GiB 可用、无环境重建）：**

| 步骤 | 空闲重跑 K-12 | 空闲重跑 O157 | 受干扰运行 K-12 | 受干扰运行 O157 | Snakemake 基线 K-12 | Snakemake 基线 O157 |
|---|---|---|---|---|---|---|
| fastqc_raw | 23 s | 21 s | 26 s | 18 s | 22 s | 16 s |
| trimmomatic | 40 s | 33 s | 34 s | 29 s | 33 s | 24 s |
| fastqc_trimmed | 18 s | 14 s | 24 s | 13 s | 17 s | 22 s |
| **spades** | **437 s** | **303 s** | 7173 s | 2549 s | 558 s | 428 s |
| quast | 21 s | 21 s | 1 s | 1 s | 1 s | 1 s |
| busco | 36 s | 75 s | 979 s | 32 s | 33 s | 36 s |
| **bakta** | **385 s** | **366 s** | 3780 s | 6670 s | 291 s | 398 s |
| **carveme** | **619 s** | **660 s** | 1870 s | 1004 s | 616 s | 644 s |
| memote | 258 s | 241 s | 1134 s | 318 s | 174 s | 145 s |
| **各步之和（两样本）** | | **3495 s** | | 25574 s | | **3382 s** |

- **整轮墙钟 ≈ 38 分钟**（12:47:20 → 13:25:10），比 Snakemake 记录的 ~51 分钟墙钟更短（并行度更高）。
- **各步骤之和 3495 s vs 基线 3382 s = 1.03×**——移植版在空闲环境下与 Snakemake **基本等速**；SPAdes 反而快 22–29%，CarveMe 几乎完全一致（1.00×/1.02×）。
- 受干扰运行 25574 s → 空闲重跑 3495 s：**同样的管线相差 7.3×**，环境是唯一变量。
- 本轮 MULTIQC 由**管线自身的 process** 生成（非手工），顺带端到端验证了 F19（report_name 走 channel 输入）与 F21（章节排序 + MEMOTE 内嵌）在真实调度路径下生效；报告内 `iframe srcdoc` ×2、章节顺序符合管线序。
- 产物再次与源仓库**逐字节一致**（Spades `contigs.fasta` ×2、QUAST `report.tsv` ×2、Trimmomatic 全部），Bakta tsv 行数一致（4891）。
- 遗留小差异（不影响结论）：QUAST 本轮 21 s（上次 1 s、基线 1 s），绝对值差 20 s；MEMOTE 比基线慢 1.5–1.7×（绝对值 <100 s，且 MEMOTE 含 FBA 求解的机器相关波动）。

## 11. 日志与 publishDir 策略

| 产物 | 位置 | 机制 |
|---|---|---|
| 各 step 日志 | `logs/<step>/<sample>.log`（仓库目录，与 Snakemake 布局一致） | task 内 `> ${params.logs_dir}/...` 直写绝对路径 |
| multiqc 日志 | `logs/multiqc.log` | 同上（`>>` 追加，三段共用一个 log） |
| task 完整 stdout/stderr | `work/**/.command.log` | Nextflow 原生（bakta 的 traceback 由此兜底） |
| 数据产物 | `data/ assembly/ annotation/ models/ reports/` | task 写 workdir → `publishDir` copy 到仓库绝对路径，`overwrite: true` |
| 执行报告/资源 | `nextflow run -with-report -with-trace -with-timeline` | 按需开启，替换 Snakemake benchmark 的资源列 |

**注意：** 数据产物必须"写 workdir → publish"，因为 Nextflow 只捕获 workdir 内的声明输出（直接写仓库路径会报 missing output）；logs/benchmarks 是非声明输出，允许像 Snakemake 一样直写仓库路径（本管线运行于本地单机，无共享文件系统并发顾虑；若未来上集群，改为 publishDir 重命名发布）。

## 12. `nextflow.config` 草案

```groovy
// ============================================================
// WGSpipeline-nextflow configuration
// ============================================================

params {
    // ---- project anchors (wfpath() analogue) ----
    project_dir    = projectDir                       // dir of main.nf
    samples_tsv    = "${projectDir}/samples.tsv"
    raw_dir        = "${projectDir}/../WGSpipeline/raw"
    bakta_db       = "${projectDir}/../WGSpipeline/db/bakta/db-light"
    busco_db       = "${projectDir}/../WGSpipeline/db/busco"
    scripts_dir    = "${projectDir}/scripts"
    envs_dir       = "${projectDir}/envs"
    bin_dir        = "${projectDir}/bin"
    multiqc_config = "${projectDir}/multiqc_config.yaml"

    // ---- output dirs (identical layout to the Snakemake version) ----
    raw_qc_dir   = "${projectDir}/data/qc/raw"
    trim_dir     = "${projectDir}/data/clean_trimmomatic"
    trim_qc_dir  = "${projectDir}/data/qc/trimmed"
    assembly_dir = "${projectDir}/assembly/spades"
    quast_dir    = "${projectDir}/data/qc/quast"
    busco_dir    = "${projectDir}/data/qc/busco"
    annot_dir    = "${projectDir}/annotation/bakta"
    carveme_dir  = "${projectDir}/models/carveme"
    memote_dir   = "${projectDir}/models/memote"
    multiqc_dir  = "${projectDir}/reports/multiqc"
    cc_dir       = "${multiqc_dir}/custom_content"
    logs_dir     = "${projectDir}/logs"
    bench_dir    = "${projectDir}/benchmarks"

    // ---- workflow params (same names as the Snakemake config keys) ----
    threads_default    = 8
    carveme_solver     = 'scip'
    memote_solver      = 'glpk'
    busco_lineage      = 'enterobacterales_odb10'
    quast_min_contig   = 0
    report_tag         = ''
    spades_careful     = '--careful'
    trimmomatic_adapters = ''                       // fallback if $CONDA_PREFIX copy missing

    trim {
        leading       = 3
        trailing      = 3
        slidingwindow = '4:20'
        minlen        = 36
    }
}

process {
    errorStrategy = 'finish'                        // Snakemake --keep-going equivalent
    withName: FASTQC_RAW     { cpus = 4 }
    withName: FASTQC_TRIMMED { cpus = 4 }
    withName: TRIMMOMATIC    { cpus = params.threads_default }
    withName: SPADES         { cpus = params.threads_default }
    withName: QUAST          { cpus = 1 }
    withName: BUSCO          { cpus = 4 }
    withName: BAKTA          { cpus = 4 }
    withName: CARVEME        { cpus = 4 }
    withName: MEMOTE         { cpus = 4 }
    withName: MULTIQC        { cpus = 1 }
}

conda {
    enabled  = true
    useMamba = true                                    // M0 实测（F5）：环境创建走 mamba
    cacheDir = "${params.project_dir}/.nextflow/conda"   // mirrors .snakemake/conda
}

workDir = "${params.project_dir}/work"

profiles {
    standard { }                                     // 8-core local machine default
    // --set-threads analogue, e.g.: nextflow run . -profile lowmem
    // F13: 26.x 拒绝 process.withName: 简写，须用嵌套 process { withName: 'X' } 形式
    lowmem {
        process {
            withName: 'SPADES'      { cpus = 4 }
            withName: 'TRIMMOMATIC' { cpus = 4 }
        }
    }
}
```

**待验证清单——里程碑 0 已实测（Nextflow 26.10.0，conda env `nextflow`，Java 25 / Groovy 4.0.33）：**

| # | 问题 | 结论 |
|---|---|---|
| 1 | `projectDir` 在 `nextflow.config` 中可用？ | ✅ **可用**。实测 `params.probe_root = projectDir` 展开为 `main.nf` 所在目录的绝对路径，与 `workflow.projectDir` 一致。§4 路径锚定方案成立。 |
| 2 | conda yaml `name:` 字段兼容性 | ✅ **无问题**。用真实 `fastqc.yaml`（`name: fastqc`）实测：Nextflow 接受、无告警无报错；环境缓存目录按 hash 命名（`env-<md5>`），与 `name:` 字段无关。8 个 yaml 无需剥离 `name:` 行。 |
| 3 | `$CONDA_PREFIX` 在 conda 任务内导出？ | ✅ **已导出**。实测任务内 `$CONDA_PREFIX` 指向 Nextflow 创建的环境（`<cacheDir>/env-<hash>`），`python3/fastqc/multiqc` 均解析到环境内。注意：若该 yaml 未 pin java（如 fastqc.yaml），`java` 解析到系统 java（`/usr/bin/java`）——正常；trimmomatic.yaml 自带 openjdk=17，其任务内 `java` 会解析到环境内。§8.2 的 adapter 路径、§8.5/8.8 的 `$CONDA_PREFIX/bin/python3` 写法全部成立，fallback 不需要。 |
| 4 | `publishDir` 引用 input 变量（`${sample}`） | ⚠️ **字符串形式不行，闭包形式可行**（与"预期无风险"相反）。26.x 严格语法下 `publishDir "${dir}/${sample}"` 在解析期报 `No such variable: sample`；改为闭包 `publishDir(path: { "${dir}/${sample}" }, mode: 'copy', overwrite: true)` 实测按样本正确发布。§8.3–8.8 全部改闭包形式。 |

**里程碑 0 额外发现（影响代码写法，F1–F8）：**

- **F1 严格语法（26.x 默认）**：顶层语句与声明不可混排（`Statements cannot be mixed with script declarations`）。§6 的解析期校验代码必须移入 `workflow { main: ... }`（放最前、任何 process 调用之前）。实测 `error "msg"` 在 workflow 内可用，坏输入同样立即失败（fail-fast 语义保留）；且 `-preview`/`-stub` 都会执行 workflow 语句，校验在两者下同样生效。
- **F2 `workflow.onComplete` 处理器已移除**：25.10 起改为入口 workflow 块内的 `onComplete:` 节（`onError:` 同理）。实测 `onComplete:` 节在失败退出时也会执行。§7 代码片段照此改写。
- **F3 publishDir 闭包**：见清单 #4。
- **F4 `nextflow.enable.dsl = 2` 不再需要**：DSL2 早已是默认；写法保留亦可（feature flag 赋值），正式代码省略。
- **F5 `conda.useMamba = true` 可用**：环境创建走 mamba，显著加速；已加入 §12 config 草案。
- **F6 Nextflow 版本**：安装到的是 26.10.0（远高于文档假定的 ≥23.04），严格语法默认开启；本机 Java 25 兼容实测通过（Groovy 4.0.33 on OpenJDK 25）。
- **F7 `-preview` 行为变化**：26.x 下 `-preview` 只执行 workflow 语句（校验生效）但**不再输出各 process 的渲染命令**；§14 阶段 A 的"读渲染命令"改用 `-stub` 后读 `work/**/.command.sh`（M1 实测可行，F14）。`nextflow inspect` 在最小探针与真实项目上均返回空 `processes` 列表，**不作为验收手段**。
- **F8 环境构建成本**：首个 conda 任务触发 mamba 建环境（fastqc.yaml ≈ 1 分钟内）；8 个环境将在首次真实运行时逐个构建，与风险 #1 预期一致。

以上结论的复现探针保存在 `m0probe/`（`main.nf` + `nextflow.config` + `data/` 假 fastq），可作为后续 Nextflow 升级的回归检查。

**里程碑 1 补充发现（编写正式 main.nf/config 时实测，F9–F14）：**

- **F9 GString `!r` 修饰符不支持**：`${name!r}` 解析期报 `Unexpected input: '!'`——错误信息里直接用普通插值 `${name}`。
- **F10 script 字符串转义**：shell 变量必须**单反斜杠** `\$VAR`；文档草案里 `\\$VAR` 会被 Groovy 当成"反斜杠 + 插值"→ 解析期 `VAR is not defined`。
- **F11 闭包变量**：严格检查器不接受 `f(args)` 闭包调用与带类型闭包参数（`{ String x -> }`），报 `f is not defined`——闭包定义提到外层、参数去类型、调用用 `f.call(args)`。
- **F12 `Channel.of(collection)` 不再拆列表**：26.x 下整列表被当作单个元素（`SAMPLES[n]` 变 null）——用 `Channel.fromList(list)`。
- **F13 config 的 process selector 简写**：`process.withName: X` 在 26.x 解析失败（`Unexpected input: ':'`）——用嵌套 `process { withName: 'X' { … } }` 且名字加引号。
- **F14 `-stub` 语义（M2 更正）**：26.10 下**未声明 `stub:` 块的 process 在 `-stub` 时照常执行真实脚本**（M1 的"stub 干跑"实为真跑，M2 复测确认）。为所有 process 补上 `stub:` 块后，`-stub` 才是真干跑（生成 touch 出的占位产物）。⚠️ 干跑会以空文件覆盖已 publish 的真实产物（F18）——只在产物目录为空/可丢弃时使用。
- **F15 publishDir pattern 不剥前缀**：`out/contigs.fasta` 发布成 `<目标>/out/contigs.fasta`——加 `saveAs: { filename -> filename - 'out/' }` 去前缀（pattern 用 `out/**`），实测（含 closure path 组合）布局正确。§8.3–8.8 已按此改写。
- **F16 严格解析器拒绝单行 section**：`input: val x` / 逗号串接的 `output: path(...), path(...)` 会解析失败（`Unknown method invocation 'path'`）——input/output 各占独立行，多输出逐行写。
- **F17 `-resume` 对缓存任务按最新 publishDir 指令重新发布**：改 publish 指令后 `-resume` 无需重算即可修正已发布布局（M2 实测）；但同时会把旧布局残留（如 `out/` 嵌套目录）留在仓库里，需手动清理。
- **F18 `-stub` 干跑会 publish 空产物**：`touch` 出的占位文件经 publishDir 覆盖真实产物（M2 踩坑，已用真实重跑恢复）——干跑仅限产物目录为空时使用，或先备份产物。
- **F19 process 指令看不到运行时 params 变更**：在 `workflow.main` 里 `params.report_name = ...` 运行时赋值，`onComplete` 能读到、但 process 的 `tag`/`output`/`publishDir` 指令求值在独立上下文，读到的是**未变更的 config params**（`report_name` 为 null）→ 报 `unable to find ... null.html`。解法：把运行时计算的值（report 名）作为 channel `val` 输入传给 process，指令里用该输入变量（`${report_name}`），不再依赖运行时 params 变更。
- **F20 磁盘满导致的 SPAdes gzip 失败**：整机磁盘 100%（剩 ~2GB）时，SPAdes 在 read-correction 阶段 `gzip -f -7` 写 corrected 文件失败（OS return 1）并 abort，且 `-resume` 缓存也无法持久化（每次重跑全量）。**非管线缺陷**，属机器资源问题；清出 ~10GB（删 `work/`、`.conda-pkgs`）后恢复。教训：跑重步骤（SPAdes/BUSCO/Bakta）前确认磁盘余量 ≥ ~20GB。

- **F21 MultiQC 排版（用户要求的两项呈现改动）**，实测于 MultiQC 1.21：
  - `report_section_order` 在此版本是**单一扁平层级**——custom content 的每个章节各自作为顶层条目，anchor 就是**章节 ID**（`carveme_summary`/`memote_summary`/`pipeline_runtime`），写成 `custom_content:` 会报 `anchor 'custom_content' not found` 而被静默忽略；语义为 **`order` 数值越大越靠前**（默认值按 10,20,30… 递增分配）。
  - General Statistics 表的**列顺序**另由 `table_columns_placement`（`namespace` → 列 key → 数值，默认 1000，**越小越靠左**）控制；内置 QUAST 的列 key 带空格（`"Total length"`，见 `quast.py`），写 `Total_length` 不生效。
  - **多层 `-c` 配置是替换而非深合并**：第二个配置里的 `sp:` 会整体覆盖第一个的 `sp:`，令复用配置的 search pattern 全部失效（实测：MEMOTE 段被静默丢弃）。故 MEMOTE 内嵌段改用 MultiQC 内置的 `*_mqc.html` **自动发现**约定（无需 `sp:`），每文件的 `id`/`section_name`/`description` 写在文件头部 HTML 注释里（MultiQC `_find_html_file_header()` 读取）。
  - 完整 HTML 页面（MEMOTE 的 Angular 报告，含 `<base href="/">` 与自带 `<style>/<script>`）**不能直接内联**：会与 MultiQC 页面互相污染。改包 `<iframe srcdoc="…">`——iframe 是独立文档（CSS/JS 隔离），`srcdoc` 让报告保持自包含。代价：报告 4.9 MB → 15.2 MB（两样本各约 5.2 MB 转义后）。
  - 验收：章节顺序 `fastqc → quast → busco → carveme_summary → memote_summary → memote_full_* → pipeline_runtime → multiqc_software_versions`；General Stats 列 `FastQC×6 → QUAST×5 → CarveMe×2 → MEMOTE×1`；报告内 `iframe srcdoc` ×2、`MemoteReportApp` ×2，且 MultiQC 页面 DOM 中不含 MEMOTE 的 `<base href="/">`（未污染）；`multiqc_data.json` 仍为 0.79 MB（HTML 内容不入数据文件）。

- **F22 磁盘预检（把 F20 的"提醒"变成硬闸门，2026-10-14）**：`workflow.main` 开头检查 `work/` 与各输出目录所在卷的可用空间（`params.min_free_disk_gb`，默认 20 GiB，`0` 关闭），不足即 `error()` 中止。实测与语法坑：
  - **严格语法无 `while`**（`while loops are no longer supported`），也**不能自递归闭包**（自引用报 `` `nearest_existing` is not defined ``）。"最近的存在祖先目录"改为声明式链路：`abs.iterator().collect{ it.toString() }.inject([root]){ acc, name -> acc + [acc.last().resolve(name)] }`，再 `reverse().find{ Files.exists(it) }`。
  - **Groovy 陷阱**：`List + Path` **不是**追加元素——`Path` 实现 `Iterable`，会被当成 iterable 拼接，累加器退化成相对路径片段（实测 anchor 被解析成 `data`、`/` 这类错误值，且 `data` 恰好在 cwd 下存在→误判）。必须写 `acc + [path]` 强制 List+List 语义。
  - `Files.getFileStore(path)` **要求路径存在**（不存在抛 `NoSuchFileException`，已用独立 Java 探针确认），所以必须先解析祖先；按 `FileStore.name()` 去重，同一文件系统只报一次。
  - CI 配置 `tests/ci/ci.config` 通过 `params.ci_out` 重定向**所有** publishDir / logs / benchmarks / workDir，并在无 conda 的前提下用 `-stub-run` 跑全 DAG——这是 F18（stub 覆盖真实产物）在自动化里的防线，`run-stub-smoke.sh` 还用 `find -newer` 断言仓库根产物目录**一个文件都没被改写**。
  - 三分支实测（`tests/ci/run-disk-guard.sh`）：阈值满足→打印 `[disk-check] <fs>: X.X GiB free of Y.Y GiB (anchor …)` 后继续；阈值不满足→**任何任务提交之前**中止（exit 1，无产物、无报告），消息给出缺口数值 + `rm -rf work` / `rm -rf .nextflow/conda` + `--min_free_disk_gb` 覆盖方式；`=0`→完全静默跳过。
  - **真实触发（2026-10-14，本机收尾测试时）**：`/dev/disk3s5` 已用 98%、仅剩 **11.8 GiB**，默认 20 GiB 下限当场拒绝了 `nextflow run . -preview` 并列出可删项。F20 的设备条件仍在复现，这道闸门不是纸面功能。

- **F23 `errorStrategy='finish'` ≠ Snakemake `--keep-going`（注入实测，2026-10-14）**：用配置层 `beforeScript` 让 `FASTQC_RAW (CI_B)` 真实 exit 1（`main.nf` 不携带任何测试钩子），实测：
  - **`finish`**：run **exit 1**，失败不会被吞；失败样本下游**零产物**（无半成品模型/注释表）；健康样本已完成任务**保留**，`-resume` 收敛且不重算（日志 `Cached process > FASTQC_RAW (CI_A_1)`）。但**不会继续独立分支**——只等已在运行的任务结束即停（本次 4 个 FASTQC 提交后即 `Execution cancelled -- Finishing pending tasks before exit`）。故旧表述"其余样本照常完成"**不成立**，已改写为实测语义。
  - **`ignore`**（唯一会继续独立分支的策略）：23 个任务全部提交、**exit 0**，死任务只留 `NOTE: Process ... Error is ignored`，原始产物留下空洞 → **静默成功**，不可作默认；作为负对照固化进 CI（`tests/ci/ignore-strategy.config`）。
  - **`beforeScript` 不可插值进程输入**：它在**配置解析期**求值，此时 `sample`/`read` 尚不存在，严格解析器直接拒绝（`` `sample` is not defined ``）；写成普通字符串则**原样**进 `.command.run`（实测 `bad substitution`，且该行在 `nxf_stage` 之前执行）。注入器因此改从任务自身 wrapper 的 `### name:` 头读取 tag（`$0` = `.command.run`）。该头是 Nextflow 内部约定，所以测试**断言注入确实触发**：将来若头格式变化，注入静默失效→`run-failure-isolation.sh` 立刻转红，而不是假通过。

- **F24 把 QUAST 自身产物并入 MultiQC（用户要求，2026-10-14）**：QUAST 5.2.0 每样本产出三个文件——`report.tsv`（早已被内置 `quast` 模块与 `multiqc_extras.py` 消费）、`report.txt`（纯文本摘要）、`report.html`（约 380 KB 完整交互报告）。实测该 HTML 是**自包含**的：无 `<base>` 标签、无本地 `src`/`href`（仅一个指向 cab.spbu.ru 的致谢链接），CSS 与 22 段脚本全部内联。因此沿用 F21 的 iframe 方案嵌入，另把 `report.txt` 放进折叠的 `<details><pre>`。读 MultiQC 1.21 源码后确认两条硬约束，决定了实现形态：
  - **`report_section_order` 的锚点是精确匹配、不支持通配**（`multiqc/multiqc.py:895` 为 `if anchor not in section_id_order.keys(): continue`，未命中的锚点只写 debug 日志**静默忽略**）；
  - **每个 `_mqc.html` 的 `id:` 会各自成为一个顶层模块**（`modules/custom_content/custom_content.py:299` `anchor=mod["config"].get("section_anchor", c_id)`），而**同 `id` 的 HTML 内容会互相覆盖**（同文件 184–185 行 `cust_mods[c_id]["data"] = parsed_data`）。
  故"逐样本一个 QUAST 内嵌段"（照抄 MEMOTE 的写法）**无法**排到 QUAST 表旁：锚点是 `<样本名>` 无法预知、也无法写进静态覆盖层。改为**全队列只出一个** `quast_full_mqc.html`（固定锚点 `quast_full`，覆盖层给 `order: 850`），内部按样本分块（标题 + 独立产物相对链接 + `report.txt` 折叠块 + `report.html` 的 iframe）。
  - **iframe 自适应高度**：`srcdoc` 帧与父页同源，故 `onload` 里用 `contentDocument.documentElement.scrollHeight` 把帧高调成内容高度（避免嵌套滚动条），并用 `try{…}catch(e){}` 包住 + 固定 1400 px 兜底——若某浏览器把 `file://` 下的 srcdoc 帧当作不透明源，只会退化成固定高度，不会让报告报错。
  - **验收（2026-10-14）**：报告 `iframe srcdoc` 由 2 增至 **4**（2 MEMOTE + 2 QUAST），且用 `html.parser` 解析后逐个比对——**4 个 srcdoc 全部与源文件逐字节相同**（属性值由解析器反转义，故直接等值比较即证明转义无损）；章节顺序 `fastqc(1000) < quast(900) < quast_full(850) < busco(800) < carveme_summary(700) < memote_summary(600) < memote_full_*(默认) < pipeline_runtime(-100) < multiqc_software_versions(-200)`；报告 15.24 MB → **16.12 MB**（+873,573 B，与嵌入载荷 872,684 B 相符）；MultiQC DOM 中 `<base href="/">` 计数仍为 0（未污染）；两样本 `report.txt` 内容均在报告中可按原文检索。
  - **该内嵌能力现有 CI 守卫（2026-10-14）**：`tests/ci/run-embed-check.sh` 用 fixtures **直跑**两个内嵌脚本（既不建 conda 也不经 Nextflow，0.3 秒 / 47 条断言），断言章节头（`id`/`section_name`/`plot_type`——MultiQC 对畸形头是**静默忽略**，写成错的只会少一个章节、不会报错）、每样本 `srcdoc` 与源文件**逐字节往返**（fixtures 刻意塞满 `&` `<` `>` `"` `'`）、内嵌页的 `<base href="/">`/`<style>`/`<script>` 绝不外泄到外层文档、QUAST 单文件 vs MEMOTE 逐样本（D15）、缺失样本只告警不崩、以及覆盖层里 `quast_full` 落在 `quast` 与 `busco` 之间。它之所以必要：其余检查全走 `-stub-run`，而 stub 模式下 MULTIQC 的 `stub:` 块**替换**真实脚本体，这两个内嵌脚本在任何其他检查里都**从未被执行**过。该检查本身也做了变异验证（去转义 / 改锚点 / 删注释头 / 插锚点分别触发 4 / 13 / 2 / 1 条断言失败并非零退出），不是"看着像能过"。
  - 报告由**与 MULTIQC process 完全一致的命令**在仓库外重放生成后发布（同 §12.1 F19/F20 的既有做法）。原因：上一轮 `-resume` 会话缓存只含部分任务（磁盘 99%、仅 8.3 GiB 可用，低于 F22 的 20 GiB 闸门，需显式 `--min_free_disk_gb`；且被杀死的探查运行把任务写进了另一个会话的缓存，`.nextflow/history` 里最后一条指向的会话没有完整 23 任务），不存在只重跑 MULTIQC 的 `-resume` 路径。改跑 `tests/ci/run-lint.sh`（0 error / 38 warning = 告警棘轮未涨）与 `tests/ci/run-stub-smoke.sh`（23 任务 DAG + 产物契约 + 根产物零改写）验证 `main.nf` 改动。

- **F25 交付的 CarveMe 模型在出厂默认介质下"生长"66 h⁻¹——这是装配速度假象，且管线自己的 MEMOTE 结果里一直记着它（2026-10-15）**：CarveMe 产出的 SBML **342/342 个 exchange 全部开放**（=完全培养基、所有底物无限吸收）。在此边界下 FBA 给出 **66.000 h⁻¹**（两样本 66.000 / 66.001），而 *E. coli* 实测最大比生长速率约 1 h⁻¹。机制：氨基酸/核苷酸/维生素/金属全都能直接**吸收**，细胞不需要合成，生物质方程退化成装配问题，约束只剩"生物质中摩尔需求最大的前体"。两个对照排除"模型漏了"：完全不给营养 → **0.000**（生物质不能被凭空满足）；只给葡萄糖+O₂ → **0.000**（正确地缺 N/P/S/矿质）。**故模型本身自洽，缺的是介质定义这一步**——FBA 里介质是**边界条件**而非模型属性，"这个模型能长多快"在未声明介质时无意义。
  - **这个问题一直在管线自己的产物里**：`models/memote/<s>/memote_result.json` 的 `test_biomass_default_production` / `test_biomass_open_production` 记录的 `data` 就是 **65.99993**，`test_fast_growth_default` 记录 `{'Growth': True}`（"比已知最快细菌还快"）。没人发现有三个叠加原因：
    1. **该测试断言极性反了**（MEMOTE 0.17.0 `suite/tests/test_biomass.py:411/433`）：文档字符串写 "*Expect the predicted growth rate ... to be below 2.81*"，代码却是 `outcome = get_biomass_flux(...) > 2.81` 然后 `assert outcome`。复跑实测：MEMOTE 自己的 helper 对同一模型返回 **65.9999**、`> 2.81` 为 True，而 pytest 报 **PASSED**。它自己的提示文字也印证极性装反（True 分支描述"是问题"，False 分支描述"属正常"）；同一函数里的 `metric = 1.0 - outcome` 随之失效（由 `test_biomass_default_production` 可反推 metric 语义是"偏差、0 最好"）。
    2. **它根本不进分数**：`memote_result.json` 的 `weights`（真正参与打分的 12 条测试）全部属于 consistency / annotation，`score.sections` 只有 `consistency` + `annotation_met/rxn/gene/sbo`——**biomass / basic 这组测试权重为 0**。所以"MEMOTE 68.1"不能作为"模型被生理学检验过"的证据。
    3. `test_find_medium_metabolites` 是**崩的**：`TypeError: unsupported operand type(s) for /: 'int' and 'list'`（同为上游 bug，非模型缺陷）。
  - 另两条**真实**的模型问题（非 bug，来自同一次定向跑）：`test_biomass_consistency[Growth]` 失败——生物质反应各组分摩尔质量合计 **1.0327**，超出 1 mmol·gCDW⁻¹·h⁻¹ 的 1e-3 容差（CarveMe 模板未归一化）；`test_essential_precursors_not_in_biomass[Growth]` 失败——生物质缺必需前体 **`fmn_c`**。
  - **对策（D16）**：介质版本化为 `media/m9_aerobic.yaml`（**组分直接取 CarveMe 自带 `carveme/data/input/media_db.tsv` 的 `M9` 行，20 个化合物，不自行发明**；摄取上限葡萄糖 10 / O₂ 20 / 其余 1000 mmol·gDW⁻¹·h⁻¹，明确标注为**项目约定而非实测**），断言由 `scripts/check_growth.py` 自己做——**不依赖上游那条极性反了的测试**。
  - **实测（两个 v0.1.0 模型）**：M9 好氧 → **0.953 h⁻¹**（两样本 0.9528 / 0.9526）；葡萄糖摄取**恰好饱和在 10.000/10**（=碳是唯一限制性营养）、O₂ 17.6/20（不限制）；无碳源 → 0.000；厌氧 → 0.228（低于好氧）；出厂全开介质 → 66.000。**隐含得率 95 gDW/mol 葡萄糖 ≈ 0.53 g/g，对照实测约 0.5 g/g**——这是 66 h⁻¹ 永远过不了的独立合理性检验。
  - **边界（诚实声明）**：该检查证明的是**生理合理性，不是定量准确性**（后者需要实测生长数据）；且两个模型给出**相同**生长速率，因为 CarveMe 的模板生物质方程占主导——**该检查无法区分菌株**。
  - **CI 守卫**：`tests/ci/run-growth-check.sh` + 独立 job（唯一需要 `cobra` + `PyYAML` 的 job；`pip install cobra` 自带 GLPK/swiglpk 但**不带 PyYAML**，F28）。真实模型是生成物、不在仓库里，故 CI 用**测试内生成的合成模型**（行为由构造保证）验证 checker 本身，并带三条负对照（介质组分无法映射 / 阈值不可能满足 / 越过物理上限）——每条都必须让脚本非零退出。

- **F26 生长验证进管线、进报告（用户指出"multiqc 里好像没有这个信息"，2026-10-15）**：F25 的结论此前只在**手动**调用时产生，报告里没有它；而报告里唯一与模型质量相关的数字（MEMOTE 总分）又**不覆盖**生长测试（F25 发现其权重为 0）。于是新增 `GROWTH_CHECK` process（coda = `envs/memote.yaml`，含 cobra + 求解器）。实现与两个关键机制：
  - **全队列一个任务**（不是每样本一个），与报告"一个章节、每样本一行"同形（cf. D15）。产出 `models/growth/growth_mqc.tsv`（自描述）+ `growth.tsv` + `logs/growth/growth_check.log`；MULTIQC 把它作为 **`path` 文件输入**（既传入命令行、又天然构成对 GROWTH_CHECK 的依赖，无需再塞进 `up_ok`）。
  - **章节配置写在文件头**，由 MultiQC 的 `_find_file_header()` 解析：它把文件里**所有 `#` 开头的行**去掉 `#` 后拼接成 YAML。这是**被迫也是刻意**的选择——在覆盖层里声明需要 `sp:`，而第二个 `sp:` 会**替换**（非深合并）复用配置 `multiqc_config.yaml` 的 `sp:` 块，静默丢掉全部既有自定义内容章节（F21）。已用 **MultiQC 自己的解析器**验证该文件头：返回 dict，`id=growth_on_medium`、`plot_type=table`、`headers` 五列齐全、`pconfig.id=growth_on_medium_table`。
  - **排序 650**（F29 起；原为 550）：紧接 CarveMe 表（700）之后、**在 MEMOTE 表（600）之前**。理由：生长检查在管线里本来就先跑（见 F29），报告里的位置要与之相符；两者都属"摘要表"，且都排在内嵌完整文档（合计约 9 MB 的 iframe）之前，免得读者翻过巨型 iframe 才看得到。（`memote_full_*` 走默认 order、无法静态排序——见 D15。）
  - **`--report-only` 的语义（D17）**：窗口越界**只报告、不失败**（窗口是 *E. coli* 特定的，而 `--busco_lineage` 可换、样本可为非肠杆菌）；**介质与模型不匹配仍然 exit 2**（那是配置缺陷，不是生物学发现）。所以 66 h⁻¹ 这类泄漏会明确出现在报告表格里，而不会让整条 run 死掉。
  - **不写 benchmark**：`benchmarks/` 承载 Snakemake 的步骤集合，`multiqc_extras.py` 用 `STEP_LABELS` 把它映射成带标签的列；新步骤没有标签，文件会以生名 `growth_check` 出现在耗时表里。
  - **顺带抓到一个真实漏洞（检查套件立功）**：新 `publishDir` 未在 `tests/ci/ci.config` 中重定向，第一次 `-stub-run` 就把**空占位文件发布进了真实 `models/growth/`**——被 **F18 守卫当场抓出**（`run-stub-smoke.sh` 的 `find -newer` 断言报 "no repository output file modified" 失败）。已补 ci.config 重定向并删除两个 0 字节污染文件。这正是那道防线的价值。
  - **验收（2026-10-15）**：DAG 24 任务（`run-stub-smoke` 全绿，产物契约含 `models/growth/{growth_mqc.tsv,growth.tsv}`）；`nextflow lint` **0 error / 34 warning**（低于 38 棘轮——新增代码零告警，并顺手把 4 处 `Channel.` 换成新 API `channel.`）；报告按 process 命令重放后新增 *Growth on defined medium* 章节，顺序为 `fastqc → quast → quast_full → busco → carveme_summary → memote_summary → growth_on_medium → memote_full_* → pipeline_runtime → versions`，表内含 5 列与两样本（0.953 / 66.000 / 0.000 / 0.228 / 0 失败）；4 个内嵌 iframe 往返仍**逐字节一致**、`<base href="/">` 计数仍为 0；报告 16.12 → **16.14 MB**。

- **F27 报告里两个"反应数"对不上（用户发现，2026-10-15）**：用户报告 MultiQC 里 MEMOTE 板块的 **Total Reactions / Total Metabolites** 与原始 MEMOTE 数据对不上。实测**两份产物都对，只是量的是两件事**：
  - MultiQC 的 **CarveMe 表**（`multiqc_extras.py` 产出）直接数 **SBML 文件元素**：reactions **2486 / 2478**，species **1551 / 1538**，genes **1623 / 1609**；
  - **MEMOTE** 用 **cobra 加载后**计数（`test_reactions_presence` / `test_metabolites_presence` / `test_genes_presence` 的 `data` 长度）：reactions **2496 / 2488**，metabolites **1551 / 1538**，genes **1623 / 1609**。
  - 结论：**代谢物与基因完全一致，只有反应数差 10**（两样本各差 10）。
  - 差的 10 个**全部是 cobra 加载时自动创建**的 exchange 反应——文件里这些胞外边界代谢物没有对应 exchange：`EX_na1_e, EX_ni2_e, EX_cd2_e, EX_cm_e, EX_hg2_e, EX_fusa_e, EX_gbbtn_e, EX_novbcn_e, EX_doxrbcn_e, EX_ttrcyc_e`。**踩坑记录**：SBML 里 reaction 的 `id` 形如 `R_<biggid>`（`R_12DGR140tipp`），而 cobra 暴露的是 `<biggid>`；直接对两个 id 集合做差会得到"2495 个全不匹配"的假象（我也先被这个坑了一次），必须按 `R_` 前缀对齐后再比。
  - **对策**：**不改复用资产**（D4：`multiqc_extras.py` 与 `multiqc_config.yaml` 保持字节一致），改用覆盖层的 **`section_comments`**（按**章节 anchor** 键控，`multiqc/modules/base_module.py:60` 读取）在 `carveme_summary` 章节内就地解释。实测注释渲染在 CarveMe 章节描述之后、表格之前。
  - **顺带发现的互操作性隐患**：M9 配方里的 `na1` 与 `ni2` **恰好落在这 10 个"文件里没有、cobra 补上"的 exchange 里**。也就是说 `check_growth.py` 能把它们映射成功，**依赖 cobra 的自动补齐**；换一个不自动补 exchange 的下游工具加载同一个 SBML，介质应用会报"缺组分"。已在 F25/D16 的语境下记明：**模型的 exchange 集合是"文件 ∪ cobra 补齐"，不是文件本身自足的。**

- **F28 第一次真实 CI 运行失败——3/4 job 红，两个根因是环境、还有一个是本项目的错误声称（用户报告"ci 跑不过"，2026-10-15）**：push `54ab47f` 触发的工作流 run `38606410168` 结果是 **failure**（lint job 通过，smoke / failure-isolation / growth-validation 三个失败）。API 顺带查明:**该仓库在此之前没有任何 workflow run**——见下方的更正。
  - **更正（重要）**：§17 行 6 原先写"**CI 3 个 job 全绿**"。这是**从未在 GitHub 上验证过**的说法:run 列表显示 2026-10-15 之前该仓库一次都没跑过 workflow，那个结论来自**本地跑脚本**。**本地脚本通过 ≠ CI 通过**——runner 的 CPU 数、已装依赖都与本机不同，本例中两者恰好各自踩了一个坑。这正是本项目反复强调的"不要声称未验证的事"，记在这里以免再犯。
  - **根因一：runner 只有 2 个 CPU（环境）**。`nextflow.config` 给 FASTQC_RAW / FASTQC_TRIMMED / BUSCO / BAKTA / CARVEME / MEMOTE 声明 `cpus = 4`、`SPADES`/`TRIMMOMATIC` 用 `params.threads_default`（8）。Nextflow 直接拒绝调度:
    ```
    ERROR ~ Error executing process > 'FASTQC_RAW (CI_A_1)'
    Caused by: Task requirement exceeds available CPUs -- req: 4; avail: 2
    ```
    后果:smoke job 一个产物都没发布（随后 65 条产物断言连环失败）;failure-isolation job 的**两个**内部 run 同样当场死掉，它的断言把结果读成"注入失效""`-resume` 失效"（其实连第一个任务都没跑起来）。
    **对策（D18）**：在 `tests/ci/ci.config` 里 `process { resourceLimits = [cpus: 1] }`。stub 任务不执行任何工具，CPU 声明在 CI 里没有意义;用**单一上限**而不是逐个 `withName` 列表，是因为后者会随新增 process 静默失效。
    **本地实测**（用一个探针配置把 `FASTQC_RAW` 顶到 `cpus = 999` 来复现这一类失败）：`nextflow -c probe -c ci.config run . -stub-run` → **24 任务照常提交**;只给 probe 不给 ci.config 时同一份配置报 `req: 999; avail: 8`（本机 8 核）。即"探针先、clamp 后"的顺序与真实层叠一致。
  - **根因二：`pip install cobra` 不装 PyYAML（依赖）**。新 growth job 只装了 cobra（cobra 自带 GLPK/swiglpk，因此安装步骤本身成功），而 `scripts/check_growth.py` 用 PyYAML 读介质定义 → 死在 `import yaml`。**更糟的是失败的样子**：脚本把它报成 **13 条互相矛盾的断言失败**（"检查不通过"、负对照该 2 却是 1、窗口与物理上限都没触发、介质自身一致性也不过……），**崩溃伪装成了检查结果**。
    **对策**：CI 改成 `pip install cobra pyyaml`;`run-growth-check.sh` 开头**同时预检 cobra 与 yaml**，缺任一个就以 **exit 2** 退出并打印确切的安装命令与 `PYTHON=` 用法。本地用一个"装了 cobra 但没有 yaml"的干净 venv 验证了新预检（exit 2 + 明确提示），用完整环境验证 16 条断言仍全绿。
  - **根因三（可诊断性，非缺陷）**：`run-stub-smoke.sh` 在一次早退后仍然跑完 65 条产物断言，把"1 条根因"淹没成"65 条失败"。它确实把 nextflow 输出 tail 打了出来（根因就在那段里），行为保留;教训记下:**排查 CI 失败先看 `---- nextflow output ----` 段**，而不是数 FAIL 行数。
  - **验证方式也改进了**：这次不再"本地绿就宣布 CI 绿"，而是 push 后**回读 GitHub 上的 run 结果**再下结论（本条的结论即为回读所得）。

- **F29 生长检查必须在 MEMOTE 之前——而且"写在前面"并不等于"先跑"（用户与导师反馈，2026-10-15，本轮最重要的一条）**：要求是 growth medium checking 排在 MEMOTE 前面。动机很实在:生长检查约 6 秒、回答的是"这个模型到底能不能活";MEMOTE 约 250 秒/样本、评分口径更窄,而且它的生长测试权重为 0、断言极性还反了(F25)。所以便宜且更根本的检查应当先跑,让不可行的模型**快速失败**。
  - **实现上的关键坑**：在 Nextflow 里把 `GROWTH_CHECK(...)` 写在 `MEMOTE(...)` 前面是**装饰性的**——两者都只依赖 CARVEME,是两条独立分支,引擎可以任意并行。要真正定序,必须造一条**数据流边**。
  - **第一次修法错了,而且被测试抓到**：最初把 trigger 声明成 MEMOTE 的**第二个 input**(`val growth_done`),与"每样本"的 tuple 通道并存时,**单值通道只被消费一次** → 两个样本只跑了 1 个 MEMOTE,**总任务数从 24 掉到 23**。是 `run-stub-smoke.sh` 的任务数断言当场报红。**修法**:把 flag 并入 tuple(`CARVEME.out...combine(GROWTH_CHECK.out.tsv.map { _f -> true })`),`combine` 把单值广播到每个样本——语义上也更准确:每个样本任务都"携带"这个已完成标记。
  - **顺带把断言升级**：任务数只能说明"少了",不能说明"少了哪个"。已在 `run-stub-smoke.sh` 增加**顺序断言**:`Submitted process > GROWTH_CHECK` 的行号必须早于第一个 `Submitted process > MEMOTE`。这样"顺序边掉了"会直接报红,而不是等到某天有人数任务。
  - **报告顺序同步**:`growth_on_medium` 650 > `memote_summary` 600(即位于 CarveMe 700 之后、MEMOTE 之前),报告与执行顺序一致。
  - **步骤重编号**:8 CarveMe → **9 Growth** → **10 MEMOTE** → 11 MultiQC;README(步骤表、DAG 图、Report/Model validation 段)、本文档、CHANGELOG、`tests/ci/README.md` 全部同步。
  - **实测**:DAG 24 任务;日志顺序 `GROWTH_CHECK (cohort)` → `MEMOTE (CI_B)` → `MEMOTE (CI_A)` → `MULTIQC`;`nextflow lint` 0 error / 34 warning(≤38 棘轮);`run-stub-smoke` 全绿(含新增顺序断言)。

- **F30 用 NCBI 上的良好装配做参照比对,评估管线自己的装配结果（用户与导师反馈 #2，2026-10-15）**：装配"看着合理"(N50/GC/BUSCO)不等于"是它声称的那个生物";采样污染、菌株不符、乃至装配本身出错,都能通过基础统计。给一个公共参照基因组,才能把"看起来对"变成**测出来对**。
  - **先试最自然的做法,并否决它**:QUAST 本身支持 `-r`。实测 QUAST 5.2.0 在本机(arm64 macOS)**自带 minimap2 第一次运行时编译失败**——`clang: error: unsupported option '-msse2' for target 'arm64-apple-darwin'`(换 `make arm_neon=1` 又撞 `-mfpu=`);QUAST 随后只打一条 WARNING 就**静默退化**成"只算基础统计",`report.tsv` 里只剩 Reference length / Reference GC / NGx,**没有 genome fraction、没有 mismatches**。这正是本项目一贯拒绝的"静默降级",而且 QUAST 没有外部比对器入口(`--help` 里没有 `--minimap2`/`--nucmer` 之类的路径参数)。
  - **决定**:**放弃 `-r`**,QUAST 保持与原始管线**逐字节一致**(原因也写进了 `main.nf` 的注释);参照比对**独立成步 5b `ASSEMBLY_REF`**,引擎用 **BLASTN**——BLAST+ 就在 pinned 的 Bakta 环境里,**不新增任何环境**(D4 完好)。
  - **指标与阈值**:阈值取 QUAST 默认(identity ≥ 95 %、比对长度 ≥ 65 bp),使结果与标准 QUAST/dnadiff 可比:
    - **genome fraction**(参考被覆盖比例,区间**按参考序列分别合并**——染色体与质粒不能混算)
    - **identity**(按比对长度加权)
    - **mismatches / indels per 100 kbp**
    - assembly aligned %、aligned contigs %
  - **实测(两个真实样本)**:
    | 样本 | 参照 | genome fraction | identity | mismatches/100 kbp | indels/100 kbp | assembly aligned | aligned contigs |
    |---|---|---|---|---|---|---|---|
    | SRR1770413 (K-12) | GCA_000005845.2 MG1655, 4,641,652 bp (`U00096.3`) | **99.38 %** | **99.926 %** | 67.6 | 5.6 | 94.45 % | 30.54 % |
    | SRR10194530 (O157:H7) | GCA_000008865.2 Sakai, 5,594,605 bp(染色体+pOSAK1+pO157) | **97.37 %** | **99.721 %** | 260.6 | 11.3 | 97.84 % | 96.92 % |
  - **怎么读这两个数**:K-12 样本恢复 MG1655 的 99.4 %、一致度 99.93 %——这就是"它确实是 K-12 衍生株"的数值形态。O157:H7 样本只恢复 Sakai 的 97.4 %、错配率高约 4 倍,且装配比 Sakai 短约 190 kb——这与"同血清型、**不同菌株**"一致(Sakai 只是该血清型的一个分离株),**不是**装配坏了。另外 aligned contigs 的不对称也很有解释力:K-12 只有 30.5 % 的 contig 承载了 94.5 % 的碱基(三百多个微小 contig),O157:H7 则有 96.9 % 的 contig 能比对。
  - **工程细节(三条都踩过)**:
    1. **混合队列**:参照是**逐样本可选**的,只有带参照的行才生成任务。CI fixture 故意 `CI_A` 有、`CI_B` 无,于是任务数 **24 → 25**,并且 smoke 里同时断言"**CI_B 不产生目录**"——没有参照就不该长出一个空章节。
    2. **多文件同 id 合并**:每样本各写一个 `*_mqc.tsv`(同 `id: assembly_ref`),先**实测** MultiQC 会把它们合并成一张表(2 行都在,`Showing 2 / 2 rows`),因此**不需要**额外聚合脚本;章节里"每样本一行"的形态得以保持。
    3. **参照不匹配必须响亮失败**:脚本在"没有任何比对过阈值"时 **exit 2**,而不是输出一张全零的表——后者会被当成结果。
  - **影响面**:新参数 `ref_dir`(默认 `db/reference`,已被 `/db/` 忽略)与 `refcomp_dir`;新可选步骤 5b(不在 11 步主链上,无参照时 DAG 与产物**完全不变**);报告新增 `assembly_ref` 章节(order **870**,紧跟 QUAST 表、在内嵌 QUAST 报告之前);磁盘预检把 `refcomp_dir` 也纳入目标。参照 FASTA 不入库,下载命令写进 README。

- **F31 可解释性:把"这个数怎么读"和"这个数有多依赖我的约定"写进仓库（用户与导师反馈 #3，2026-10-15）**：基础功能达标之后,真正的缺口是**读者能不能正确解释这些数字**。做了五件事:
  1. **`docs/interpretation.md`**：逐指标"怎么读 / 什么情况该去查",含三个"看起来对其实不对"的陷阱(66 h⁻¹ 完整介质假象、生长率依赖吸收上限、genome fraction 低≠装配坏)、排错表、参数敏感度表、报告章节导航。
  2. **得率断言(机制改动)**：`check_growth.py` 现在除 μ 之外还计算并断言 **gDW/g 得率** = (1000·μ / v_carbon) / M_carbon,窗口取 `[0.30, 0.70] g/g`,摩尔质量声明在介质文件(`carbon_source_mw: 180.156`)而不是写死在代码里。实测 **0.529 g/g**(对照 *E. coli* 葡萄糖无机盐实测约 0.5 g/g)。MQC 表新增 `yield_g_per_g` 列。
  3. **敏感度入档**:把实测表写进 `media/m9_aerobic.yaml` 与解读文档——葡萄糖上限 5/10/20/不限量 → **0.476 / 0.953 / 1.283 / 28.772 h⁻¹**。结论:μ 是"介质+约定"的函数,得率是模型化学计量的函数,所以对外讲合理性要讲**得率**。
  4. **数据资产指纹**:`docs/asset-sources.md` 增补"外部数据资产(不复制、按需获取)"一节——carveme `media_db.tsv` 的 sha256 与版本、两个 NCBI 参照的登录号/md5/长度;并**诚实记录**"CarveMe 包内没有说明 media_db 的上游出处,这一层本项目未核实"。
  5. **排错表**:症状 → 最可能原因 → 用什么确认 → 怎么修(覆盖 0 生长、谱系选错、参照不符、磁盘、MEMOTE 崩溃、参照路径)。
  - **为什么得率更值得断言**:它是生物质方程的化学计量,几乎不随吸收上限变化;而 μ 随碳源上限线性变化。断言一个随约定漂移的量,等于把约定伪装成结论。

- **F32 CI 抓到"测试断言比它声称的弱":失败注入点选错了（2026-10-15）**：push `7b3bdd6` 的 CI 里 `injected single-sample failure` job 红在 **`FAIL absent: assembly/spades/CI_B`**。不是环境问题,是**测试自身的语义缺陷**。
  - **根因**:`FASTQC_RAW` 与 `TRIMMOMATIC` 都是 `sample_ch` 的**两条并行分支**(彼此没有依赖边),而 `SPADES` 只依赖 `TRIMMOMATIC`。所以"注入该样本 `FASTQC_RAW` 失败"**根本不能**保证该样本没有下游产物:`errorStrategy='finish'` 只承诺"不再提交新任务",已经提交的 `TRIMMOMATIC(CI_B)` → `SPADES(CI_B)` 会照常跑完并发布——于是 `assembly/spades/CI_B` 出现了。
  - **为什么以前"通过"**:纯**时序侥幸**。本地核数与调度顺序让失败先被吸收;CI 上调度顺序一变就暴露。换句话说,这条断言一直比它声称的弱,只是没有反例。
  - **修法**:注入点同时覆盖**两个入口**(`FASTQC_RAW` + `TRIMMOMATIC`)。这既消除了对调度时序的依赖,语义上也更真实——一个坏掉的 FASTQ 本来就会同时让 FastQC 和 Trimmomatic 失败。
  - **验证**:本地连跑 3 次全绿;25 条断言(含 `absent: assembly/spades/CI_B`)全部通过。
  - **教训**:这次是 CI 替本地抓到了问题,而不是反过来——说明"本地绿"确实不等于"CI 绿"(F28),也说明**注入式测试的注入点必须落在"能截断该样本全部下游"的位置**,否则断言的通路其实是空的。

- **F33 目录结构整改:项目必须自立,产物必须归一处（用户反馈，2026-10-21）**：用户指出"这是独立项目,raw data 却在兄弟仓库"是**结构性失误**,并要求整治"QC/报告东一个西一个、让人困惑的空目录、`make_ppt.py` 这类中间产物"。整改如下。
  - **输入搬进项目**(不再依赖 `../WGSpipeline`):`../WGSpipeline/raw` → `data/raw`(424 MB)、`../WGSpipeline/db/{bakta,busco}` → `data/db/`(4.0 G + 62 M)、`db/reference` → `data/reference`(9.9 M)。老仓库原地留**符号链接**(`raw`/`db` 指向新位置),所以它仍可运行,而磁盘上没有第二份 4.4 G。
  - **单一产物根 `results/`**:原来的 `data/qc/*`、`data/clean_trimmomatic`、`assembly/spades`、`annotation/bakta`、`models/*`、`reports/multiqc` 六处并列,现在收成
    `results/qc/{fastqc_raw,fastqc_trimmed,quast,busco,refcompare}/`、`results/reads/`、`results/assembly/`、`results/annotation/`、`results/models/{carveme,memote,growth}/`、`results/report/`。
  - **报告目录只放报告**:MULTIQC 原来 `publishDir` 整个任务目录,于是 `custom_content/`、`busco_stage/`、`memote_html/`、`quast_html/`、`ref_section/`、`multiqc_report_data/` 全都出现在报告旁边——这正是"像个构建目录"的来源。现在只发布 `multiqc_report*.html` 与 `multiqc_data/**`;custom content 改为任务内目录(`--outdir custom_content`),不再发布。
  - **Nextflow 自己的 report/trace 归位**:Nextflow 默认把 `report-<ts>.html`、`trace-<ts>.txt`、timeline、dag 写到**启动目录**,这就是项目根上那 10 个散文件的来源。现固定到 `results/report/nextflow/{report.html,trace.tsv}`,并关掉 timeline/dag。
    **踩到的坑**:这两个块在**配置解析期**求值,看不到 `-c` 覆盖(与 F19 同类)。实测:改完路径后跑 CI,报告仍写进了**真实的** `results/report/nextflow/`(1.6 MB)。修法是在 `tests/ci/ci.config` 里直接 `report { enabled = false }` / `trace { enabled = false }`,而不是"重定向"——CI 不该产生执行报告。
  - **AMR 缓存不再长在根上**:`scripts/amr_guard/amr_chunk_guard.py` 的缓存默认是 `<repo>/db/amr_guard/known_bad_sequences.tsv`(脚本用 `Path(__file__).parents[2]` 定位),所以根目录会冒出一个 `db/`。该脚本是 D4 字节一致资产**不能改**,于是用脚本自己支持的 `WGS_AMR_GUARD_CACHE` 环境变量把缓存指到 `data/cache/amr_guard/`,并在 `BAKTA` 里 export。缓存是持久收益:它按 sha256 记住会让 AMRFinderPlus 崩溃的序列,省掉每样本约 5.5 分钟的重试。
  - **清掉可重建垃圾**:`.DS_Store`、10 个 `.nextflow.log.*`、根目录 `report-*.html`×2 与 `trace-*.txt`×2、`.nxhome/`、`.pytest_cache/`、`m0probe/conda-cache/`(942 MB)、`tests/ci/out/`,以及全部空目录。`m0probe/` 的**源码**保留(它是文档化的引擎探针)。
  - **取舍(显式)**:不再镜像 Snakemake 的输出树。内容仍逐字节可比,**只有路径变了**;README 里"intentionally mirrors the original Snakemake layout"这句话已改写。这是 D23。
  - **验证**:7 个本地检查全绿;CI stub 25 任务;`tests/ci/out` 顶层只剩 `results/` 与 `work/`(此前是散的五类目录);CI 运行后真实 `results/` 与 `data/` **未被触碰**。

- **F34 报告可解释性审计:把"审稿人会问什么"变成文档（用户要求，2026-10-21）**：用户把"这些参数是什么意思 / 为什么没让 QUAST 跑参考模式"当作**学术工作者会问的典型问题**,要求带着这种眼光把整个 MultiQC 报告过一遍,找出所有会引起疑问的地方、给出答案并落盘(README 不扩容)。产出:把 `docs/interpretation.md` 从"指标说明"扩成 **字段手册 + 问答**——§2 覆盖报告里每张表的每一列(单位 / 分母 / 我们的值 / 怎么判断),§3 是 **20 条 Q&A**,分"方法""看起来矛盾的数""溯源"。
  - **审计确认的两处"报告本身会说错话"**:
    1. **L50 在同一份报告里有两种显示**:General Statistics 是整数(8 / 11),QUAST 模块是 `0.0 K`。原因是 MultiQC 对"计数类"列做了千位缩放(8 → 0.008 K → 显示 0.0);`table_columns_placement` 只能排序,改不了模块的 `suffix`/`modify`,所以**只能靠文档指明"信 General Statistics 那一列"**。
    2. **软件版本表声称 `Snakemake 9.25.2`**,而这是 Nextflow 管线。该行来自**复用的 Snakemake 时代 `multiqc_config.yaml`(D4 字节一致)**,是遗留项;修它必须动这份复用资产(破坏字节一致、需更新 `docs/asset-sources.md` 的 sha256),因此列为**待用户决定**、未擅自修改。
  - 其余问答覆盖:QUAST `-r` 为何不可用、genome fraction 与 dnadiff 的可比性、一个样本为何在总表里出现 5 行、两个 GC 列各是什么、MEMOTE 68.1 % 不是"正确率"、表里 2486 vs 内嵌 MEMOTE 2496(cobra 读入补 10 个 exchange)、BUSCO 100 % 与 genome fraction 97.4 % 为何不矛盾、aligned contigs 30.5 % 为何不代表装配差、%Dups / %Failed 的正确读法、运行时表为何没有 5b 与生长检查、千分位空格等。

- **F35 报告显示规范化:单位只在表头出现一次（用户反馈，2026-10-21）**：用户指出"表头已经写了单位(如 `N50 (Kbp)`),单元格里又写一遍 `244.8 Kbp`,不规范"。查证确认这是 **MultiQC 模块的固有行为**:模块列的 `suffix` 同时喂给标题和单元格(QUAST:`title = f"N50 ({suffix})"` + `"suffix": suffix`),把它设空只会得到 `N50 ()`;FastQC 连 suffix 配置入口都没有。替换模块表会丢掉图表与 general-stats 列,fork 模块不可维护,所以选了**窄范围后处理** `scripts/strip_units.py`(F36 起升级并更名为 `scripts/normalize_report.py`,逻辑保留):
  - 只改 `<table>` 内单元格 `<span class="val">` 的**显示文本**;
  - **仅当同一列的表头已含该单位**时才删(表头没有的不动,否则等于销毁信息);
  - `val=` / `data-sort` 属性、图表、CSV 导出**一概不碰**(排序仍按原始数值);
  - **幂等**(第二次运行 0 处)。
  - 实测:整份报告删除 **48 处**冗余(`Kbp/Mbp/K/M/%`);`Average Read Length` 的 `241 bp` 与 MEMOTE 的 `%` **保留**(表头无单位);`2 486` 这类千分位单元格不受影响。
  - CI:`run-embed-check.sh` 新增 6 条断言(删对/保留/排序属性/幂等)。
  - **留下的不一致(已记录、未擅自改)**:MEMOTE 各列的 `%` 只在单元格出现、表头没有单位;要统一必须改复用资产的列定义(D4),与 F34 的 `Snakemake 9.25.2` 同属"是否偏离 D4"的待决事项。
  - **顺带修掉一个自己引入的真 bug**:F33 里写了 `--data-dir multiqc_data`,而 MultiQC 1.21 的 `--data-dir` 是**布尔开关**(不是路径),真实运行会把 `multiqc_data` 当成 analysis directory 并失败。**stub 模式不执行 multiqc,CI 抓不到**,是重放任务命令时暴露的。已改为不传该参数(数据目录名由 `-n` 派生),publishDir 模式改为 `multiqc_report*_data/**`。
  - **教训**:`-stub-run` 覆盖的是 `script:` 块,所以**凡是只在真实运行里执行的命令都缺 CI 覆盖**;这次靠"重放"补上。以后新增这类命令,应同时加一条直接调用它的检查(embed-check 那种模式)。

- **F36 表头格式统一（用户反馈，2026-10-21）**：F35 去掉"单元格重复单位"之后,用户进一步要求"**所有表头统一格式**"。把报告里 10 张可见表的表头全抽出来比对,发现 MultiQC 混用了多种约定:`Sample Name` vs `Sample`;`% Dups`(前缀百分号)vs `GC (%)`(括号后缀);`Kbp` vs `kbp`;`reactions`/`metabolites` 小写 vs `Reactions`/`Metabolites`;同一个量两个名字(`Length (Mbp)` vs `Assembly Length (Mbp)`);同一个 L50 三副面孔(`L50` / `L50 (K)` / `# contigs`);MEMOTE 的 `%` 只在单元格、表头没有单位。
  - **定下的规范**:**`Quantity, qualifier (unit)`**;首列恒为 `Sample`;单位只出现在表头、用括号后缀;计数类不带单位;一个量只用一个名字;`%` 写成 `(%)` 而不是前缀;SI 大小写(`kbp`/`Mbp`)。
  - **实现**:`scripts/strip_units.py` → **`scripts/normalize_report.py`**。两步:先**改表头的可见文本**(按 `table id + 原表头` 的显式映射;通用兜底:`% X`→`X (%)`、`# X`→`X`、`Sample Name`→`Sample`),再用**改名后的表头**判断单元格里是否有重复单位要删。仍然只动显示文本:`val`/`data-sort`、图表、CSV 导出输入一概不碰;**幂等**。
  - **顺带修掉一个"数据展示"缺陷**:QUAST 模块默认把计数类列按千缩放,`L50 = 8` 显示成 `0.0 K`(F34 当时只能写进文档让读者"信另一列")。现在 overlay 设 `quast_config.total_number_contigs_multiplier: 1`,直接显示 `8.0`,表头写 `L50 (contigs)` —— 同一个数在两处显示一致。
  - **结果**:整份报告 **27 个表头规范化 + 76 处单元格去单位**;两张表里 `N50 (kbp)`、`Assembly length (Mbp)`、`L50 (contigs)` 完全同名;`Reads GC (%)` 与 `Assembly GC (%)` 明确区分;`Mismatches / 100 kbp` 统一为 `Mismatches (per 100 kbp)`(改在 `compare_assembly_ref.py` 源头)。
  - **CI**:`run-embed-check.sh` 直接调用脚本并断言(改名 / 百分号 / 单位后缀 / 未映射表头不动 / 排序属性完好 / 幂等)。
  - **重要**:这一步**没有改任何 D4 复用资产** —— 展示层在渲染产物上归一化,所以 `multiqc_config.yaml`、`multiqc_extras.py` 仍与上游字节一致。这也回答了 F34/F35 留下的"D4 要不要偏离"的问题:**格式统一不再需要偏离 D4**。仍剩一项未决:`Snakemake 9.25.2` 那行属于**内容**(版本表数据)而不是**格式**,后处理不应编造版本号,是否编辑复用配置仍需用户决定。

- **F37 版本表纠错:删掉假的 `Snakemake`、补上漏掉的三个工具、引擎版本改为运行时注入（用户要求，2026-10-21）**：用户要求"把这个 Snakemake 的 version 去掉",并顺带核对"所有用到的资源是否正确"。把版本表当成资源清单逐项核对后,发现四类问题:
  1. **错误陈述**:表里有一行 `Snakemake 9.25.2`。这是**复用的 Snakemake 时代配置**遗留(F34 已记录),而本管线是 Nextflow —— 一个假的版本号比格式问题严重。
  2. **漏项**:真实使用但没有列的 **BLAST+ 2.17.0**(步骤 5b)和 **COBRApy 0.32.1 + GLPK 5.0**(步骤 9);MEMOTE 用 GLPK 也没写。
  3. **引擎缺失**:管线跑在 Nextflow 26.10.0 上,版本表里没有任何引擎信息。
  4. 其余条目逐项核对:**全部正确**(FastQC 0.12.1、Trimmomatic 0.39 + openjdk 17、SPAdes 4.0.0、QUAST 5.2.0、BUSCO 5.8.3、Bakta 1.11.4 + pyhmmer 0.10.15、CarveMe 1.6.6 + diamond 2.2.8 + pyscipopt 5.7.1 + SCIP 9.2.4、MEMOTE 0.17.0、MultiQC 1.21)。注意 `openjdk 17` 看着可疑(别的 env 装的是 25.0.2),但 `envs/trimmomatic.yaml` 确实 pin 了 17、实际装的是 17.0.18,所以它是**对的**。
  - **做法**:(a) 从 `multiqc_config.yaml` 删掉 Snakemake 组,补 `BLAST: blast 2.17.0`、`Growth check: cobrapy 0.32.1 / glpk 5.0`;(b) **引擎版本不硬编码** —— `MULTIQC` 任务在运行时把 `workflow.nextflow.version` 与 `workflow.manifest.version` 写成 `custom_content/nextflow_mqc_versions.yaml`,MultiQC 通过 `*_mqc_versions.yaml` 搜索规则读入,于是永远跟着实际引擎走(写进 `custom_content/` 而不是任务根目录,因为 multiqc 命令行只扫那几个目录);(c) 顺手在启动横幅里加上引擎版本。
  - **代价**:`multiqc_config.yaml` 不再与上游字节一致 —— 这是**第一处有意偏离 D4**(记作 D24),已在 `docs/asset-sources.md` 就地注明并更新 sha256,同时写明"上游同步时要保留这一处改动"。
  - **验证**:重放生成的报告里版本表为 BLAST / BUSCO / Bakta / CarveMe / FastQC / Growth check / MEMOTE / MultiQC / NextFlow-WGS2GEM 0.1.0 / Nextflow 26.10.0 / QUAST / SPAdes / Trimmomatic,**`Snakemake` 已不存在**。

- **F38 管线图纠错 + 全篇复核（用户发现，2026-10-22）**：用户指出汇总页的 DAG 上有一条**从 raw data 直连 SPAdes 的边**,并问"是画错了还是我们的 PE 结构有问题"。**结论:画错了,管线没有问题。** 证据是源码:`main.nf` 的接线为 `SPADES(TRIMMOMATIC.out.map { n, r1p, r2p, r1u, r2u -> tuple(n, r1p, r2p) })`,而 `SPADES` 进程声明 `input: tuple val(sample), path(r1_paired), path(r2_paired)` —— 它吃的是**修剪后的 paired reads**;原始 reads 只进 `FASTQC_RAW` 与 `TRIMMOMATIC` 两个进程。手绘 ASCII 时我把"raw 是两条分支的共同起点"错画成了一条直连边。
  - **用户同时指出图"简陋、产物标注不全、方向不明确"**。于是整页重做:
    - §2 增加**树状图**(纯 HTML/CSS 嵌套列表 + 连接线,无绝对定位,文字不会溢出),`<pre>` 复制版保留;
    - §3 换成**嵌套流程图**:每行是「进程(tool+版本)→ 产物路径」,缩进即"这个产物喂给谁";新增 **MULTIQC 汇总表**(逐组列出它到底读了哪些文件)、图例,并在图下写明这次的更正;
    - 去掉手绘 ASCII 作为主图(手绘坐标靠猜,是这次错误的直接来源)。
  - **按同一标准复核全篇,又抓到三处过度声称**:
    1. **"SPAdes 的副产物也一并发布" —— 错。** 实测该任务 `out/` 有 206 个文件,但 `results/assembly/<sample>/` 只有 `contigs.fasta`:**Nextflow 只发布声明的输出**。已在流程图、步骤卡、产物表三处改正。
    2. **"18 个指标的 TSV" —— 错,是 17 个**(文件 18 行含表头)。两处已改。
    3. **`results/report/nextflow/{report.html,trace.tsv}` 被写成 MULTIQC 的产物 —— 错**,那是**引擎**在运行结束时写的(由 `nextflow.config` 钉住路径)。已拆成独立条目并注明归属。
  - **复核通过、无需改动的项**(逐条对源码/磁盘):12 个进程的 `input`/`output`/`publishDir`;14 个参数默认值;两样本 Bakta `.tsv` 行数(4891/5496)与 CarveMe diamond 行数(8961/9306);refcomp 的 17 个指标;报告 13 个章节;7 个本地检查 + 4 个 CI job;以及 fastqc/trimmomatic/spades/quast/busco/bakta/carve/memote 的**实际命令行参数**。
  - **根因与教训**:这些错误全部来自**凭记忆写文档**,而不是**从源码重推**。规则收紧:凡是要进报告的**结构性陈述**(谁吃谁、产物叫什么、哪一步产出什么),必须做**双向核对** —— 一端是 `main.nf` 的 process 契约(`input:`/`output:`/`publishDir`)与 workflow 接线,另一端是**磁盘上的真实产物树**;手绘 ASCII 图尤其危险,因为它不参与任何校验。

- **F39 流程图改为「生成 + 机器校验」，手绘图彻底退场（用户要求，2026-10-22）**：用户看过嵌套列表版的 DAG 后说"这个 DAG 有点像树状图,很难让人看清楚,能不能像管道图一样,用心思画漂亮一点"。这不是排版偏好,而是 F38 的直接后果:嵌套列表用**缩进**表达边,读者必须逐行重建拓扑,而缩进本身不校验任何东西 —— 这正是我上次画错 raw→Spades 那类错误能存活的原因。
  - 改为 `docs/make_pipeline_diagram.py` 生成内联 SVG:**边是数据**(`EDGES` 表,逐条对应 `workflow { main: }` 里的实际调用),坐标由脚本**算**出来,没有人手写坐标。图上两行:上排是数据通路(raw → 2 → 4 → 7 → 8),下排是"报告会读到的每一步"(1/3/5/5b/6/9/10),用一条虚线汇流排接到 11 MULTIQC;CarveMe 的模型统计走右侧通道并进同一条汇流排。每格三行:步骤号徽标、进程名、工具+版本、以及**它发布的产物**(见 §4 查完整路径)。
  - 三种线型区分语义:**实线**=有文件传递;**点线**=仅排序(9 → 10,无文件);**虚线**=被 MULTIQC 按路径读取。图例移到 HTML,不占画布。
  - **校验先于写出**:脚本在任何写入之前检查 (a) 每条边必须从左/下侧离开、从上/左侧进入,(b) 沿边采样 240 点,任何一点都不许落进**别的**方框 —— 这就是上次那个错误边的自动版,(c) 任意两个方框不许重叠,(d) 每个标签的估算字宽不许超过它所在的方框。任何一条不满足就非零退出、**一个字节都不写**。实测它在开发中真的拦下过一次(徽标重复导致的 `3 · FASTQC_TRIMMED` 超宽)。
  - 该检查并入 `tests/ci/run-lint.sh`(无需 Nextflow、0 秒),所以 CI 会一直守着这张图。
- **F40 MULTIQC 少等 5b:一个真实存在的静默缺口(本次复核发现,2026-10-22)**：做 F39 时要把"11 谁等谁"画准,于是逐条核对 `up_ok` 的 `mix` 链,发现 **`ASSEMBLY_REF` 不在其中**。而 MULTIQC 的 `ref_section` 是**按路径**从 `${params.refcomp_dir}` 拷出来的,并且拷贝语句是 `if [ -f "$f" ]; then cp ...; fi`。也就是说:若 5b 尚未结束而 MULTIQC 抢先启动,参照比对这一章会**静默消失** —— 不报错、不警告,报告看上去完全正常。这与 F30 的立项目标(宁可失败也不静默降级)直接冲突,也和该段代码自己的注释("MULTIQC waits for all upstream steps")矛盾。实测两样本时 BLASTN 远快于 MEMOTE,所以从未触发,但这是**竞态**,不是保证。修法:把 `ASSEMBLY_REF.out` 加进 `up_ok`(空通道 `mix` 是 no-op,无参考的队列不受影响);任务数不变,发布物不变。附带记录:该边加入后 MULTIQC 的任务哈希变化,下一次 `-resume` 会重跑 MULTIQC —— 代价是一次报告重建,换来的是"要么完整、要么报错"。

- **F41 清理的分级、边界与两条 `-resume` 实测(2026-10-22)**：需求是"一键清空,但原数据不能动,而且要先能空跑确认"。它同时照出两块此前没人验证过的空白:**删掉 `work/` 之后 `-resume` 到底做什么**,以及**只删 `results/` 能不能从缓存重生报告**。用一个两步 mini probe(`A → B`,`publishDir mode: 'copy'`)在 26.10.0 上实测:
  - 删 `work/`、保留 `.nextflow/cache` 后 `-resume`:不报错、不静默跳过,而是**重新提交**两个任务(A 哈希不变;B 因输入路径变化换哈希)。所以"哈希缓存指向已删目录会退化成静默空操作"这个担心在本版本上不成立,清理工具可以纯按目录边界切分,不必为规避它去额外删 `.nextflow/cache`。
  - 只删 `results/`、保留 `work/` 后 `-resume`:两个任务都报 `Cached process`,`results/` 被完整重建(与 F17 一致)。这把一个最高频需求——"我只想要一份新报告"——从 38 分钟降到数秒,因此写进工具的 `--help` 作为建议路径,而不是做成第四个层级(层级一多就没人记得住)。
  - `du` 与 `df` 的差再次被量化:同日删旧 checkout 的 `.snakemake/conda`,`du` 报 **6.8 GB**、`df` 只回 **2.80 GiB**。故清理工具的唯一口径是 `df` 前后差,`du` 的数字必须标注为"上界",否则工具会长期系统性高报自己的功劳。
  - **一次被当场纠正的界线错误**:初版把 `.nextflow/cache` 与 `data/cache/` 划在 `all` 档,理由是"引擎记账、无害"。用户复核后指出:"不只是 output,中间产物都要删"——`cache` 是引擎自己写出来的中间产物,`data/cache/` 是 AMR guard 写的中间产物,二者都该归 `outputs`,不该让用户为了删中间产物去动必须联网重建的环境。已上移。现在 `outputs` 与 `all` 的唯一差别是 **conda 环境**,理由收敛为一条可检验的事实:它是整棵树上唯一需要联网(~5.7 GiB 下载)才能回来的东西。
  - 设计:三级(`check` 默认不删 / `outputs` 删全部产物与中间产物但保留 conda 环境 / `all` 连环境),删除表是**允许清单**,每条路径删除前再复核一次(相对路径、无 `..`、非软链接、父目录 realpath 在仓库内、不是申报输入),输入在清理前后各盘点一次并比对,不符即 exit 1 并打印差异。两个负对照写进 CI:非终端且无 `--yes` 必须拒绝;把 `data/raw` 注入**副本**的删除表后仍须被"申报输入"那条拒掉——即允许清单不是最后一道防线。

- **F42 一次"看起来像管线坏了"的慢:主机负载,不是代码(2026-10-22)**：清空重跑时,25 个任务跑到第 12 个(SPADES#2)就用了 55 分钟,而基线是 38 分钟跑完全部。逐层取证后归因于主机,不是管线:
  - **形态**:SPADES#1 内部日志到 K99 只用 **3:46**(与基线同量级),之后 **27 分钟**全耗在 K99 的单线程图构建上;SPADES#2 同样只走到 K127 就耗尽 55 分钟。
  - **引擎自身的读数**:`TaskPollingMonitor` 的轮询周期从设计的 **5 s** 变成 **296 s**(60×),这是 JVM 抢不到 CPU 的直接证据。线程栈(`jcmd Thread.print`)显示 `Task submitter` 停在 `TaskPollingMonitor.awaitSlots`,4 个下游任务的 `workDir` 是空的。
  - **负载归属**:`vm.loadavg` 34–37(8 核);把管线进程**全部杀掉后 30 秒均值仍是 29** —— 负载不是我们造的。进程普查:我们 9 个进程,其余约 480 个,主要是浏览器(`plugin-container` 17、`Google` 17)、编辑器(`Visual` 10)、办公/通讯(`Microsoft` 7、ClassIn 5)、媒体(Spotify 9),外加 Spotlight(`mdworker_shared` 16)。`tmutil status` 的 `Running = 0`,可排除 Time Machine。
  - **机制**:单线程阶段(图构建、报告生成)在 30+ 可运行线程里只能分到约 1/30 的 CPU,所以"前 4 分钟正常、后面突然慢";8 线程阶段受影响小得多 —— 这个非对称正是误判的成因。
  - **触发/放大**:当天为验收清理了约 20 万个文件(6.8 GB `.snakemake/conda`、2.4 GB `work/`、183 MB `.nextflow/cache`),fsevents 队列让 Spotlight 的积压雪上加霜。
  - **修复**:①仓库根放 `.metadata_never_index`(已 gitignore),让 mds 不再索引这个 checkout —— `work/` 每跑一次就churn 约 15 万文件,这个代价本来每跑一次都要付;②`bin/pipeline-clean` 在删除超过 20 000 个文件时打印"接下来会重索引、慢的是主机不是管线",阈值可用 `PIPELINE_CLEAN_SPOTLIGHT_FILES` 覆盖并在 `run-clean-check.sh` 里**双向断言**(超过要报警、低于要沉默);③验收计时必须附 `vm.loadavg` 与核数,否则"耗时"这个数不可比 —— 旧基线 38 分钟恰恰缺这个条件。
  - **已结(F44)**:`awaitSlots` 的机制用任务完成时刻钉死了 —— local executor 的**槽位 = 核数(nproc),每个任务按自己声明的 `cpus` 占槽**。证据:4 个 FASTQC_RAW(各 4 cpus)是**两个两个**完成的(13:27:12/13:27:14,13:27:22/13:27:24);2 个 TRIMMOMATIC(各 8 cpus)**串行**完成(13:27:50 / 13:28:24);SPADES(8 cpus)独占期间,4 个下游任务在 `awaitSlots` 干等 65 分钟;修复后在读数 3.9–14 的负载下,2 个 BAKTA(各 4)与 2 个 CARVEME(各 4)都是**并发**执行的。所以"早期一批任务同时 submit"不等于同时运行 —— submit 不看槽位,运行才看。
  - **这条不是缺陷,是引擎正确行为**(从不超订);但它的后果值得写进性能一节:在 8 核机器上 `threads_default = 8` 让 SPADES/TRIMMOMATIC 独占全部槽位,重型步骤实际是串行的;`-profile lowmem`(两者降到 4)反而可能让 SPADES 与 BAKTA/CARVEME 重叠,总时长更短 —— 这是**下一个可测量的假设**,不是结论(未测)。

- **F43 `export \$VAR=` 让 BAKTA 从 10-21 起一直是坏的,而所有自动化检查都没抓到(2026-10-22 清空重跑时暴露)**:F33 把 AMR-guard 缓存从 `db/amr_guard/` 挪到 `data/cache/amr_guard/`,做法是在 BAKTA 的 `script:` 里加一行 `export \$WGS_AMR_GUARD_CACHE=${params.amr_cache}`。`\$` 把 `$` 转义了,shell 看到的是 `export $VAR=/path` —— **在赋值位置展开一个未定义的变量**;而每个任务 wrapper 都是 `bash -ue`,于是任务在 `bakta` 启动之前就以 `unbound variable` 退出。
  - **为什么一直没被发现**:①该行由 F33(10-21)引入,之后只跑过 `-stub-run`;②stub 块会**替换整个 script**,干跑永远走不到这一行;③它只在真实执行 BAKTA(第 7 步)时触发;④CI 里所有 Nextflow 检查都是 stub。这与 F18 同类:**只有真跑才会现形的缺陷**。
  - **怎么抓到的**:本次清空后 `-resume` 全量重跑,BAKTA 第一次在新代码下真正执行,`.command.sh` 第 11 行报错,`errorStrategy = 'finish'` 让整跑以 exit 1 结束 —— 也就是"宁可失败也不静默降级"这条设计起了作用。
  - **修法**:去掉 `\$`(赋值目标永远不该被转义);并在 `run-lint.sh` 加静态守卫(跳过注释行,否则守卫会匹配自己的说明文字),负对照实测:把 `\$` 放回去,lint 立即 FAIL 并打印行号。
  - **规则修正**:F10 的"script 里的 shell 变量要写成 `\$VAR`"只适用于**使用**,不适用于**赋值目标**。已写入 SKILL 的 pitfall 表。

- **F45 预检把 BUSCO 谱系查在错误的路径上,于是"永远黄"(2026-10-23,真实项目预检时暴露)**:管线传的是 `--download_path <busco_db>`,BUSCO 5 把谱系放在 `<busco_db>/lineages/<lineage>`;预检却查 `<busco_db>/<lineage>`。本 checkout 正是 `data/db/busco/lineages/enterobacterales_odb10`,所以**环境完全正确的机器上预检也会报"未下载"**。危害不是功能性的,而是信噪比:一条永远亮着的警告会训练用户忽略所有警告 —— 这与 F20 "宁可失败也不静默"同源,只是方向相反(常态化的假警报)。修法:两种布局都接受,但要求 `hmms`/`info`/`dataset.cfg` 任一标记存在(空目录不算),并把命中的真实路径写进 detail。实测修后真实项目预检 **8/8 ok、0 warning**。规则:**预检的判据必须来自被测工具自己的布局/契约(这里是 `--download_path` 的语义),不能来自"目录大概长什么样"的猜测**。
- **F46 一条"跳过的断言"暴露了真实缺陷:UI 重启后不能取消正在跑的任务(2026-10-23)**:第一版 `run-ui-check.sh` 在 attach 场景打印的是 `SKIP cancel of an attached run (409)`,理由是契约只允许取消"当前服务器自己启动"的运行。但这个运行本来就是 UI 启动的:`state.json` 记着 pid,`start_new_session` 保证 pgid==pid,重启后的服务器完全可以 killpg。接受"跳过"等于接受"38 分钟长跑中途重启 UI 之后只能等它跑完"——这不是测试覆盖问题,是设计缺口。修法:owned 与 attached 走同一条取消路径(attached 用 `os.kill(pid,0)` 轮询,10 s 后 SIGKILL;终态 `cancel_requested` 优先于 `exit_code is None`,记 `cancelled` 且 exit_code=null)。SKIP 改为 7 条断言,假引擎与真实 Nextflow 26.10.0 两条路径均实测。规则:**测试里的 SKIP 要当成一个待回答的问题**——除非环境确实不具备(比如缺 node),否则它要么是缺陷,要么是契约写错了;两者都该修,而不是让它留在报告里。
- **F47 假引擎漏了 MULTIQC:测试替身必须提交与被测系统相同的任务集(2026-10-23)**:假引擎按"每样本 11 步 + growth"造了 24 个任务,漏掉聚合的 MULTIQC;真实 2 样本带参考是 **26**(11×2 + 2×ASSEMBLY_REF + GROWTH_CHECK + MULTIQC)。这个错误没有立刻让测试变红(当时的断言只查若干 process 名),但它会让"任务计数"这条最便宜的契约断言失去意义。发现方式是 Lead 在写提交信息**之前先数了一遍实际输出**(26 vs 24),而不是相信记忆。修法:补上 MULTIQC;断言改成 `expected == actual == 11*N + refs + 2`,并在 CI fixture 形状(1 个 reference)上对齐 25。规则:**替身的任务集与日志行格式是契约的一部分,计数是它的最小断言**;任何"应该有几个任务"的数字,要么来自公式,要么来自实测,不能来自印象。

- **F48 真实引擎的 stdout 不报告任务完成,而假引擎报告了 —— 验收时界面显示"0 完成"(2026-10-23)**:真实 2 样本验收跑到 18.6 分钟时,UI 的阶梯仍显示 `completed=0`、而 `submitted=11`(管线本身完全正常,`work/` 里有 4 个 FASTQC_RAW/2 个 TRIMMOMATIC/4 个 FASTQC_TRIMMED 的产物)。原因分两层:**Nextflow 26.10.0 的 stdout 只打印 `[hash] Submitted process > NAME (tag)`,任务完成只写进项目根的 `.nextflow.log`**(`[Task monitor] DEBUG … Task completed > TaskHandler[id: N; name: NAME (tag); status: COMPLETED; exit: 0; error: -; workDir: …]`);而第一版 fake engine 是自己把 `Completed process >` 打到 stdout 的 —— **替身比被替身更话痨**,于是 237 条断言里没有一条能发现这件事。这正是"测试替身必须是契约的一部分"(F47)的下半句:替身的行为不能比真实系统更丰富,否则它测的是替身。
  - **修法**:`LogParser` 增加 `.nextflow.log` 增量解析(`feed_engine_log`/`_consume_engine_line`,COMPLETED/CACHED 计入完成,FAILED/ABORTED 计入失败);`RunManager` 增加引擎日志游标:新一轮运行从**当前文件末尾**起步(不继承上次完成行),检测 inode 变化或文件截断(Nextflow 启动时会轮转 `.nextflow.log` → `.nextflow.log.1`)则从 0 重读,attach/终态恢复从 0 重放。stdout 的 `Completed process >` 仍被接受(假引擎/旧版引擎)。
  - **实测**:修复后 kill -9 UI 再启动 attach 到同一次真实运行 —— `state=running`、`completed=10`、`SPADES running=1`、run id 不变、日志继续流入;这正是 §13.15 attach 场景的真实版。测试补了"引擎只写日志、stdout 无 Completed"的模式与真实 stub 段 `completed==25` 的断言。
  - **规则**:**进度/完成这类状态,必须以真实引擎的实际输出为准去验证一次**;测试替身可以少说,不能多说。任何"看起来对"的界面数字,都要有一次真实运行的截图或 state 快照作证据。
- **F49 真实 2 样本验收:首轮被主机条件中止,次轮跑完但 ≤45 min 未达成(2026-10-23/24)**:按 roadmap C 的验收口径,从"清空产物"开始、经 UI 跑真实 2 样本。22:36:55 起跑,清空(13 条路径/1.74 GiB,输入盘点一致)与 10 个真实任务(4×FASTQC_RAW + 2×TRIMMOMATIC + 4×FASTQC_TRIMMED)都正常;随后卡在 SPADES:23:44:49、**67.9 分钟**时仍只有 10/26 个任务完成,SPADES 的单个样本尚未跑完,于是人工从 UI 取消。取证:机器只有 **8 GiB RAM**,swap 已用 3.8–7.1 GiB,loadavg 从起跑 2.22 升到 **24.1**(15-min 均值 11.5);SPADES 内部日志停在单线程 K-mer 图构建阶段的同一种"前几分钟正常、之后爬行"形态 —— 与 F42 完全相同的签名。**这不是 UI/管线缺陷**:同一轮里 UI 侧的全部动作都按契约工作(取消 <1 s、进程组消失、`work/`+`results/` 保留;kill -9 UI 后 attach 到同一 run/pid、完成数从日志恢复;`results/` 324 MiB 下载逐文件一致、`trace.tsv` 往返逐字节相同)。因此本版**不宣称** ≤45 分钟,也不对拍基线;复测步骤(关重负载应用、确认 loadavg<4 与 swap≈0、记录时间+loadavg+swap)写进 `docs/ui.md` §14 与 roadmap C。**第二轮(同日 00:33–01:51)在同一台机器上跑完了**:26/26、exit 0、**77.5 分钟**,报告 16,150,251 B 被 `/api/report` 逐字节转发并在 iframe 内渲染,350 MiB `results` tar.gz 的 69/69 个文件与磁盘一致,全部数字与 0.1.0 基线逐项相同(组装/BUSCO/MEMOTE/生长/Bakta/参照比对)。**45 分钟仍未达成**,原因仍是主机:8 GiB RAM、swap 3.3–4.8 GiB、loadavg 峰值 24.1 —— 同一 DAG 的空载基线是 38 分钟。结论:`≤45 min` 作为**待复测的验收项**保留,不在受污染条件下硬凑;功能与正确性验收已完成。规则:**验收失败时先分清"被测系统坏了"与"测量条件不成立"**;后者要写清条件与复测方法,绝不用一次受污染的数据去换一个绿色结论。
- **F50 页面自己的预检 JVM 被当成"外部 nextflow",点"开始运行"偶发 409(2026-10-24,用户反馈"UI 不能重新开始")**:用户重启 UI 后打开页面、点开始,收到 `检测到外部 nextflow 进程 (pid 15231)`;几秒后同样的请求恢复正常。根因两层。①**判据过宽**:`looks_like_nextflow` 只要求命令行里出现 "nextflow";项目目录本身叫 `WGSpipeline-nextflow`,而预检的 `nextflow -version` / `config -flat` 会短暂起一个 JVM —— 两者都"提到 nextflow"。②**没有排除自家子进程**:`external_nextflow_pids` 只排除了本进程 pid、记录的 run pid 与 `self.proc`,没有排除**本进程 spawn 的预检子进程**;于是"刷新页面(自动预检)后立刻点开始"的严格时序必然撞上 409。另一个结构性问题是**测试复制了启发式而不是调用被测代码**,所以即使生产判据变了,门禁也不会失败(与 F47"替身不能比现实更话痨"同源,方向相反:门禁替生产背锅)。
  - **修法**:①分类器收紧为"basename 恰为 `nextflow`/`fake-nextflow`(含 `sh -c` 包装),或 JVM 带 `nextflow.cli.Launcher`/`nextflow.jar`/`-jar …/nextflow-<ver>-<build>.jar`,**且 argv 含 `run`**";`-version`、`config`、`rm -rf .nextflow/...`、`ui/server.py --nextflow …` 都不再匹配。②`ps -axo pid=,ppid=,command=` 取父子关系,忙扫描排除本进程的**全部后代**;`_run_cmd` 改用 Popen 并把存活 pid 登记到 `_ACTIVE_CHILDREN`,在 `ps` 不可用(只有 pgrep、没有 ppid)的受限环境同样能排除自家子进程。③`409` 文案带上被判定的 pid + 命令行前 80 字符,下一次可直接定位。④测试门禁改为 `import ui/runner.py` 调真正的 `looks_like_nextflow`,并新增 17 条断言:12 条真/假分类表 + 合成进程表(自家引擎/后代/预检 `-version` 不算,外来引擎算)。
  - **验收**:新 gate **282 ok / 0 FAIL / 0 SKIP(42 s)**;上一版 265 条不含这组断言。用户侧需要重启 UI 才能加载修复(重启后 token 改变,旧 URL 失效)。
  - **规则**:**"忙"检测必须排除自身后代,判据只能来自被测进程的真实形状**;测试门禁不能复制生产启发式,要 import 被测代码 —— 复制出来的门禁只会漂移,不会失败。
**里程碑 1 验收记录（2026-10-14）：** 首次 `-stub` 运行（当时未加 stub: 块，按 F14 语义实为真实执行）10 任务 DAG 正确；渲染命令与 snakefile 逐 token 一致（fastqc `-o` 为 workdir 属预期差异）；坏样本表 4 例注入（缺列/坏名/重复名/fq 不存在）全部在任务启动前失败且报错信息逐条对齐；真实运行 10/10 成功，产物目录树、logs/、benchmarks/（Snakemake 格式 `s\th:m:s`）与源仓库一致，**8 个 Trimmomatic 产物与 Snakemake 版逐字节一致**。

## 13. 运行方式

```bash
cd /path/to/WGSpipeline-nextflow

nextflow run .                          # 全流程（profile standard 自动生效）
nextflow run . -profile lowmem          # 覆盖线程（--set-threads 语义）
nextflow run . --report_tag cohort_b    # 子集/单样本报告（需先换 samples.tsv）
nextflow run . -resume                  # 断点续跑（对应 Snakemake 缓存）
nextflow run . -preview                 # 不执行任务；26.x 不再渲染命令（见下注 F7）
nextflow run . -stub                    # 真干跑（各 process 均有 stub: 块）；⚠️ 会以空文件覆盖已 publish 的真实产物（F18），仅限产物目录为空时使用
nextflow run . -with-report -with-trace # 执行报告 + 资源/耗时 trace

# 零成本自动检查（不建 conda 环境、不跑任何真实工具；实测单脚本 0.3–35 秒；CI 调用的就是这些脚本）
bash tests/ci/run-embed-check.sh        # F21/F24 内嵌脚本：章节头 + 转义往返 + 排序锚点（无需 Nextflow）
bash tests/ci/run-lint.sh               # nextflow lint：0 error + 告警数棘轮
bash tests/ci/run-preview.sh            # -preview 编译 + 6 个坏样本表必须在任何任务前失败
bash tests/ci/run-disk-guard.sh         # F22 磁盘预检三分支
bash tests/ci/run-stub-smoke.sh         # 全 DAG -stub-run：24 任务 + 产物契约 + 不动根产物目录
bash tests/ci/run-failure-isolation.sh  # F23 注入失败 + -resume 收敛 + ignore 负对照
```

**M0 实测（F7）：** 26.x 的 `-preview` 会执行 workflow 语句（样本表校验生效）但**不再输出各 process 的渲染命令**；阶段 A 的"读渲染命令"改用 `-stub` 跑完后读 `work/**/.command.sh`（stub 模式下各任务目录仍生成命令脚本），或 `nextflow inspect`（M0 对最小探针返回空 `processes` 列表，M1 在真实项目上再验证其可用性）。

与 Snakemake 差异说明：`-resume` 按任务 hash 缓存；**强制重跑某步骤** = 删除该步骤在仓库里的 publish 产物（Nextflow 检测不到输出即重算）——比 `--forcerun` 粗糙，但覆盖同一需求。

## 14. 验证与对拍方案

遵循既有纪律：**改完规则先读渲染命令，不真跑**；对拍用现有 Snakemake 产物做基准。

### 阶段 A：静态验证（零成本）
1. `nextflow run . -stub` 真干跑（F7/F14：26.x 的 `-preview` 不渲染命令；所有 process 已带 stub: 块；**注意 F18——stub 会 publish 空文件，仅在产物目录为空时执行**）：确认 process 数量、输入/输出签名；读 `work/**/.command.sh` 核对渲染命令（stub 模式下 `.command.sh` 是 stub 体，渲染命令核对改在首次真实任务目录进行）；
2. 逐一核对 §8 渲染出的命令行与原 snakefile `-n` 输出的 shell 行（wildcards 替换后逐 token 对齐：参数值、路径、`--labels`、`--min-contig`、`--solver` 等）；
3. 坏样本表注入测试：缺列 / 坏样本名 / 重复名 / fq 不存在 → 全部在 workflow 启动期（任何任务执行前）即失败。

### 阶段 B：2 样本全量对拍（约 38 分钟基线）
```bash
nextflow run . -resume
```
与 `../WGSpipeline/` 现有产物逐项对比：

| 产物 | 对比方式 | 通过标准 |
|---|---|---|
| `data/clean_trimmomatic/*`（4 个 fastq.gz） | `cmp` 字节比较 | 字节一致（Trimmomatic 确定性） |
| `assembly/spades/<s>/contigs.fasta` | 序列比对（`cmp`/`diff`，不一致则查 N50 与总长） | 一致；SPAdes 同版本同线程应确定 |
| `data/qc/quast/<s>/report.tsv` | `diff` 关键指标（N50/L50/GC%） | 数值一致 |
| `data/qc/busco/<s>/short_summary.*.txt` | `diff` | 一致 |
| `annotation/bakta/<s>/<s>.tsv` | `head -1` 检查 | 首行 `# Annotated with Bakta`；与 Snakemake 版 diff 一致 |
| `models/carveme/<s>/<s>.xml` + `<s>.tsv` | 存在性 + 行数 | **Bakta 的 tsv 未被覆盖**（known-issues #5 spot check）；diamond 表落在 CarveMe 目录 |
| `models/memote/<s>/`（3 文件） | `diff memote_summary.tsv` | 评分一致（FBA 数值允许微小平台差） |
| `reports/multiqc/multiqc_report.html` | grep 样本名 | **恰好 2 个样本**，custom content（MEMOTE 评分、耗时表、QUAST L50）齐全 |
| `logs/`、`benchmarks/` | 布局检查 | 与 Snakemake 目录树一致；`benchmarks/multiqc.tsv` 存在 |

### 阶段 C：行为对拍
1. `--report_tag cohort_b`：子集报告与全队列报告并存，互不覆盖，报告内容=样本表；
2. **单样本失败 —— ✅ 已用注入失败实测（2026-10-14，F23，`tests/ci/run-failure-isolation.sh`）**：`finish` 下 run exit 1、失败样本下游零产物、健康样本已完成任务保留且 `-resume` 收敛不重算；**但 `finish` 不继续独立分支**，旧表述"其余样本照常完成"不成立，已按实测改写（见 §18）；`ignore` 实测 exit 0（静默成功）→ 拒绝，负对照入 CI；
3. 改 `multiqc_config.yaml` / `db/version.json` → `-resume` 自动重跑 MULTIQC / BAKTA；
4. O157:H7 样本（SRR10194530）跑通 BAKTA——AMR-guard 路径生效的既有触发样本。

### 阶段 D：每次 push 的自动化守卫（2026-10-14 起，`.github/workflows/ci.yml`）
CI 只安装 Nextflow，然后调用 `tests/ci/run-*.sh`——**本地跑与 CI 跑是同一批断言**，不会漂移。
lint（error 门禁 + 告警棘轮）、`-preview` 编译 + 6 个坏样本表（用 `-stub-run` 跑，确保"若校验没生效就一定会有任务被提交"）、F22 磁盘预检三分支、全 DAG stub 干跑（24 任务 + 产物契约 + 根产物目录零改写）、F23 注入失败 + `-resume` 收敛 + `ignore` 负对照、F21/F24 内嵌脚本直跑（章节头 + 转义往返 + 排序锚点；不需要 Nextflow，0.3 秒）、F25 生长检查直跑（合成模型 + 3 条负对照；只需 cobra，12 秒）。
**不建 conda 环境、不跑真实工具**：实测 `run-lint` 2s / `run-stub-smoke` 8s / `run-disk-guard` 16s / `run-failure-isolation` 27s / `run-preview` 33s / `run-embed-check` 0.3s，各 job 约 1 分钟内；`growth-validation` 另需 `pip install cobra pyyaml`（F28）。真实数据的 10 步验收（阶段 B）仍是人工流程。

## 15. 风险登记表

| # | 风险 | 影响 | 应对 |
|---|---|---|---|
| 1 | Nextflow 未安装（本机当前没有） | ~~阻塞实施~~ | ✅ **已解决（M0）**：conda 环境 `nextflow` 安装 26.10.0（满足 ≥23.04，DSL2 默认，Java 25 实测兼容）；8 个 conda 环境将在首次真实运行时逐个联网构建 |
| 2 | conda yaml `name:` 字段与 Nextflow 兼容性 | ~~低~~ | ✅ **已验证（M0）**：接受且无告警，无需剥离 `name:` 行（见 §12 清单 #2） |
| 3 | `$CONDA_PREFIX` 在任务内不可用 | ~~中~~ | ✅ **已验证（M0）**：任务内已导出（见 §12 清单 #3）；fallback 方案废弃 |
| 4 | macOS/arm64 四个 env workaround 是 osx 专用 | 中 | 原样随 yaml 迁移（本机仍是 Mac）；换 Linux 集群时另行重推导（既有结论，非本次移植引入） |
| 5 | `multiqc_extras.py` 依赖仓库绝对路径（quast/carveme/memote/bench 目录） | 中 | 已按设计传 `params.*` 绝对路径；对拍阶段 B 的 custom content 检查兜底 |
| 6 | 线程声明与实际不符（carveme 4/实际 1、spades 8 独占） | 低 | 决策 D2 保持原数字；`-profile lowmem` 提供覆盖手段；对拍通过后另行优化 |
| 7 | MULTIQC 首跑缺 "all samples" 耗时行 | 极低 | 已知偏差，Snakemake 首跑同行为 |
| 8 | `-resume` 与"改样本表/改参数"交互（参数变化不触发重算，与 Snakemake 相同） | 低 | 文档写明：换参数用 `-profile`/CLI 显式覆盖并在必要时清空对应产物 |
| 9 | publishDir `overwrite: true` 覆盖语义与旧产物并存 | 低 | 每样本目录隔离，同样本重跑即覆盖，与 Snakemake 幂等语义一致 |
| 10 | 设计文档代码片段基于 23.x 语法（顶层语句、`workflow.onComplete`、publishDir 字符串），与 26.x 严格语法不兼容 | 高（M1 直接编译失败） | ✅ **已消除（M0–M4）**：F1–F21 实测结论已回写 §6/§7/§8/§9/§12/§13/§14 并落地正式 `main.nf`/`nextflow.config`；`m0probe/` 探针可回归复测 |

## 16. 决策记录

| ID | 决策 | 状态 |
|---|---|---|
| D1 | 新项目目录 `WGSpipeline-nextflow/`，与 `WGSpipeline/` 平级（用户指定） | 已定 |
| D2 | 线程数保持原声明数字（carveme=4 等），不做"顺手修正" | 已定 |
| D3 | 本轮只产出设计文档；代码实施待批准后按里程碑执行 | 已定 |
| D4 | 复用资产（envs/scripts/multiqc_config.yaml）以字节一致副本纳入新仓库；raw/、db/ 默认以 params 指回原仓库 | 已定 |
| D5 | 输出目录树布局与 Snakemake 完全一致，但落在新仓库内（原仓库产物不动） | 已定 |
| D6 | benchmark 改由任务内自计时（`bin/task_env.sh`）产出同格式 TSV，`multiqc_extras.py` 零改动 | 已定 |
| D7 | `--keep-going` → `process.errorStrategy = 'finish'` | 已定 |
| D8 | bakta 的 `2> >(tee)` 进程替换删除（Nextflow 原生捕获 stderr）；AMR-guard 保留 | 已定 |
| D9 | 数据产物"workdir 写入 + publishDir 发布"；logs/benchmarks 直写仓库绝对路径（本地单机假设） | 已定 |
| D10 | 代码写法按 Nextflow 26.x 严格语法修订：§6 校验代码进 `workflow.main`、§7 `onComplete` 改块内节语法、§8.3–8.8 publishDir 改闭包形式（依据 M0 实测 F1–F8） | 已定 |
| D11 | trimmomatic adapter 优先级按 snakefile 语义：param 优先、空则 `$CONDA_PREFIX` 自带（修正 §8.2 草案 env-first 顺序） | 已定 |
| D12 | 失败语义**保持** `errorStrategy = 'finish'`：失败必须响亮（exit 1）且不留半成品；实测它**不等价**于 `--keep-going`（不继续独立分支），唯一会继续的 `ignore` 会 exit 0 静默成功，故拒绝；补偿手段是"已完成任务保留 + `-resume` 收敛"，文档按实测措辞（F23） | 已定（2026-10-14 注入实测） |
| D13 | F20 磁盘余量由 README 提醒改为 `workflow.main` 开头硬预检（`min_free_disk_gb`，默认 20 GiB，`0` 关闭）；逐个输出目录解析"最近存在祖先"的**文件系统**并去重，任务启动前中止 | 已定（2026-10-14 实测三分支） |
| D14 | 回归守卫用 `tests/ci/*.sh` 承载、GitHub Actions 只做"装 Nextflow + 调脚本"：本地与 CI 同一批断言；全部用 `-stub-run` + 禁用 conda，真实数据验收仍人工 | 已定（2026-10-14） |
| D15 | 内嵌第三方工具自身报告（MEMOTE / QUAST）时，**每个工具一个固定锚点的章节**，不按样本拆章：`report_section_order` 锚点精确匹配、无法预知样本名，逐样本 id 会让章节落到管线顺序之外；代价是章节内按样本分块而非每样本一个折叠标题 | 已定（2026-10-14，F24） |
| D16 | 生长验证**自建**而非委托上游：介质版本化为 `media/m9_aerobic.yaml`（组分取 CarveMe 自带 M9 行，不自行发明），断言由 `scripts/check_growth.py` 承担（组分可映射 / 落窗 [0.4,1.5] / 物理上限 2.81 / 无碳源为 0 / 厌氧更低）。理由：MEMOTE 0.17.0 的 `test_fast_growth_default` 断言极性反了、且该组测试进不了分数（F25），把物理上限委托给它等于没有守卫。**不**把该断言塞进 `main.nf` 做硬失败：窗口是 *E. coli* 特定的，而 BUSCO 谱系可换、样本可为非肠杆菌；管线的普适断言只有"不长于已知最快细菌"与"无碳源不长" | 已定（2026-10-15，F25） |
| D17 | 生长验证**入管线**的具体形态：全队列**一个** `GROWTH_CHECK` 任务（与报告"一章节、每样本一行"同形，cf. D15）；章节配置写在 `*_mqc.tsv` 的**文件头**而非覆盖层（否则需要第二个 `sp:`，会替换掉复用配置的 sp 块，F21）；`--report-only` = 窗口越界只报告不失败，介质与模型不匹配仍 exit 2；**不写 benchmark**（会以生名 `growth_check` 出现在耗时表） | 已定（2026-10-15，F26） |
| D18 | CI 的 CPU 上限写在 `tests/ci/ci.config`（`process.resourceLimits = [cpus: 1]`），**不**削弱 `nextflow.config`：runner 只有 2 核而真实工具声明 4–8，Nextflow 会直接拒绝调度（F28）。用单一上限而非逐 process 的 `withName` 列表，因为后者会随新增 process 静默失效；stub 任务不执行工具，CPU 声明在 CI 里无意义 | 已定（2026-10-15，F28） |
| D19 | 生长检查排在 MEMOTE **之前**,且用**数据流边强制**(MEMOTE 消费由 GROWTH_CHECK 广播出的 trigger,`combine` 到每样本),不靠声明顺序;报告章节顺序同步为 growth(650) > memote(600)。理由:6 s vs 250 s/样本,生长是更根本的可存活性问题;MEMOTE 的生长测试权重为 0 且极性反了(F25) | 已定(2026-10-15,用户与导师反馈,F29) |
| D20 | 参照比对**独立成步**(`ASSEMBLY_REF`,BLASTN,Bakta 环境),**不用** QUAST 的 `-r`:后者在 arm64 macOS 上自带 minimap2 编译失败并**静默降级**成无参照统计(且无外部比对器入口)。阈值取 QUAST 默认(≥95 % identity、≥65 bp)以便与标准 dnadiff 可比;参照不匹配时 **exit 2**;QUAST 输出保持逐字节一致 | 已定(2026-10-15,用户与导师反馈 #2,F30) |
| D21 | 生长检查**同时断言得率**(gDW/g,窗口 0.30–0.70),不只断言 μ：μ 强依赖介质的吸收上限这一约定,得率不依赖(它是生物质化学计量);摩尔质量由介质文件声明(`carbon_source_mw`)而非代码硬编码。敏感度表写入介质文件与 `docs/interpretation.md` | 已定(2026-10-15,用户与导师反馈 #3,F31) |
| D23 | 目录结构:**`data/` = 输入**(raw/db/reference/cache)、**`results/` = 产物**(qc/reads/assembly/annotation/models/report)、`logs/`+`benchmarks/` = 运行账本、`work/`+`.nextflow/` = 引擎;**不再镜像 Snakemake 输出树**(内容仍逐字节可比,只换路径)。MULTIQC 只发布报告本体与其数据目录;Nextflow 自身 report/trace 固定 `results/report/nextflow/` 且在 CI 关闭;AMR 缓存经 `WGS_AMR_GUARD_CACHE` 落在 `data/cache/` | 已定(2026-10-21,用户反馈,F33) |
| D24 | **允许对复用资产做一处有意偏离**:`multiqc_config.yaml` 的 `software_versions` 段删掉假的 `Snakemake 9.25.2`、补上 BLAST+/COBRApy/GLPK;引擎版本改为运行时注入(`*_mqc_versions.yaml`)。理由:假版本号属于**错误陈述**而非风格;D4 的字节一致是为省同步成本,不能以保留错误为代价。资产 sha256 与背离原因已记入 `docs/asset-sources.md` | 已定(2026-10-21,用户要求,F37) |
| D25 | **破坏性操作的默认层级必须是"什么都不删"**:`bin/pipeline-clean` 三档 `check`(默认,只印计划)/`outputs`(产物,保留 conda 环境)/`all`(连缓存);输入(`data/raw`、`data/db`、`data/reference`)与全部被跟踪文件在任何档位都不进删除表;真实删除需 `--yes` 或终端确认,非终端无 `--yes` 一律拒绝。理由:用户要的"一键"由 `outputs --yes`/`all --yes` 提供,不必靠降低默认档来换;而默认档一旦会删东西,就没有第二次机会(2026-10-22,用户要求,F41) | 已定(2026-10-22) |
| D26 | 本地 UI 的形态:**单进程 python3 标准库 HTTP 服务 + 原生 JS 单页**(无框架/无构建/无 CDN),只绑 `127.0.0.1`、一次一个任务、不提供上传、不自己删任何东西、不复刻报告;进度与报告路径只从引擎输出读取(`Submitted|Completed process` 行、`onComplete` 的 `report =` 行),**exit 0 但没有报告判 failed**;取消打整个进程组(SIGTERM→10 s→SIGKILL)并保留 `work/`,重启后 attach 的运行同样可取消;鉴权=一次性 token + Origin 校验。理由:用户已定"本地单用户 Web UI"且要求零依赖、与现有 CI 工具无关;Electron 多一套打包链,FastAPI+React 引入 pip/npm,均与"同事拿到就能跑、CI 只认 bash+python3"冲突。代价:手写 CSS/JS、无组件库(2026-10-23,roadmap C) | 已定(2026-10-23) |

## 17. 里程碑与后续开放问题

| 里程碑 | 内容 | 验收 |
|---|---|---|
| 0 | 安装 Nextflow；验证 §12 待验证清单 1–4 | ✅ **完成（2026-10-14）**：Nextflow 26.10.0（conda env `nextflow`，mamba 安装）；四项结论 + F1–F8 见 §12 清单表；复现探针 `m0probe/` |
| 1 | 骨架：config + 解析期校验 + FASTQC×2 + TRIMMOMATIC | ✅ **完成（2026-10-14）**：资产副本入库（`docs/asset-sources.md`，来源 commit `75f1d21`）；`-stub` 干跑 10 任务 DAG 正确；渲染命令逐 token 对齐；坏样本表 4 例注入全部启动期失败；真实运行产物与源仓库一致且 Trimmomatic 逐字节一致；F9–F14 见 §12 清单表 |
| 2 | 组装与 QC：SPADES / QUAST / BUSCO | ✅ **完成（2026-10-14）**：渲染命令逐 token 对齐；QUAST `--labels` 坑位验证（报告内 assembly=样本名）；真实运行 **SPAdes contigs.fasta 与 QUAST report.tsv 均与源仓库逐字节一致**；BUSCO 评分与源仓库一致（差异仅输入路径与 python 补丁号等元数据）；publish 布局修正（F15 saveAs）；F14–F18 见 §12 清单表 |
| 3 | 注释与建模：BAKTA(+guard) / CARVEME / MEMOTE | ✅ **完成（2026-10-14）**：渲染命令逐 token 对齐；真实运行通过——**AMR-guard 在 O157:H7（SRR10194530）触发并成功兜底**（日志见 `[amr-guard] WARNING ... retrying chunk-wise`，bakta.tsv 完整产出）；**CarveMe 不覆盖 Bakta TSV**（坑位 #5：bakta tsv 首行 `# Annotated with Bakta` 完好，diamond 表落在 carveme 目录）；MEMOTE 评分 SRR1770413=68.0 / SRR10194530=67.7（**数值更正 2026-10-15**：原始分为 0.680641 / 0.677781，管线自己的表格四舍五入为 **68.1 / 67.8**，此处 68.0 / 67.7 系截断；且该总分只覆盖 consistency + annotation 两类，biomass/basic 组**权重为 0**——详见 F25） |
| 4 | 报告：MULTIQC + task_env 计时 | ✅ **完成（2026-10-14）**：custom content 6 个 TSV（carveme_modelstats/summary、memote_score/summary、pipeline_runtime、quast_generalstats）齐全；`benchmarks/` 含 multiqc.tsv + 22 个 per-sample 计时 TSV；报告内恰 2 个样本（cohort=样本表，不扫目录）。**排版（F21，用户要求）**：章节与 General Stats 列均按管线顺序（FastQC→QUAST→BUSCO→CarveMe→MEMOTE→耗时→版本），并把 MEMOTE 完整 `index.html` 以独立 iframe 内嵌进报告（报告 4.9 MB → 15.2 MB）。注：因 F19/F20，报告最终以与 process 完全一致的命令手动生成（见 §12.1 F20/F21） |
| 5 | 全量对拍（阶段 B/C）+ 回写文档 + readme 补充 Nextflow 运行说明 | 对拍表全部通过 |
| 6 | 防御性加固：磁盘硬预检（F22/D13）+ 每次 push 的 CI 回归守卫（阶段 D/D14）+ 单样本失败注入实测（F23/D12） | ✅ **完成（2026-10-14）**：预检三分支实测（满足/不满足/关闭）且不满足时在任何任务前中止；CI 3 个 job 全绿（lint 0 error + 告警棘轮、`-preview` + 6 坏样本表、F22 三分支、23 任务 stub 干跑 + 产物契约 + 根产物零改写、F23 注入失败 + `-resume` 收敛 + `ignore` 负对照）；单样本失败从"未验证限制"变为"实测语义"，并更正了 `finish` ≠ `--keep-going` 的旧表述。**（更正 2026-10-15：本行原称"CI 3 个 job 全绿"，但该仓库当时从未跑过 workflow——那是本地脚本的结论，而本地绿 ≠ CI 绿；首次真实 CI 运行 3/4 job 红，根因与对策见 F28/D18。CI 现为 4 个 job。）** |

| 7 | 本地一键 UI(roadmap C):契约 + 假引擎 → 后端/前端/测试 → 真实 2 样本验收 | ✅ **完成(2026-10-24)**:282 条断言 0 FAIL/0 SKIP(42 s,CI job `local-ui`);真实 stub 全 DAG 25 任务;注入失败定位 `FASTQC_RAW/CI_B_1/exit 1`;真实 2 样本经 UI 从清空跑到报告:**26/26、exit 0、77.5 min**、报告逐字节转发并在 iframe 渲染、350 MiB 下载 69/69 一致、全部数字与 0.1.0 基线相同;真实运行中的取消与 kill -9 后 attach 均实测。⏳ **≤45 min 未达成**(主机 8 GiB RAM/loadavg 峰值 24.1,非 UI/管线开销),作为待复测项保留(F49;复测方法见 `docs/ui.md` §14) |

**开放问题：**
1. 复用资产副本的同步机制：手动同步（记录来源 commit）还是 git submodule？（实施时定）
2. `raw/`、`db/` 后续是否复制/迁移进新仓库，还是长期以 `../WGSpipeline` 引用？（用户定）
3. 是否在里程碑 5 后追加 Docker/Singularity profile？（可选）
4. `samples.tsv` 的新仓库副本与原仓库如何保持同步（两份表并存期间的分歧风险）？

## 18. 收尾审查结论（逻辑 / 生物学准确性 / 易用性，2026-10-14）

**管线逻辑 —— 通过。** 10 步 DAG 正确（FastQC→Trimmomatic→FastQC→SPAdes→{QUAST,BUSCO,BAKTA}→CarveMe→MEMOTE→MultiQC）；cohort 严格等于样本表（报告恰好 2 样本，不扫目录）；params 与源 config 键 1:1；`-resume`（hash 缓存）、`errorStrategy='finish'`（--keep-going）、`report_tag` 多报告并存、`db/version.json` sentinel 均已实现并验证。

**生物学准确性 —— 通过（有据）。**
- BUSCO 100% 完整（440/440，0 缺失）——高质量组装；
- QUAST：K-12 4.84 Mb / 50.33% GC / N50 159 kb；O157:H7 5.40 Mb / 50.34% GC / N50 244 kb——与已知大肠杆菌基因组大小一致（O157:H7 因前噬菌体/O-岛更大）；
- 组装大小与样本表物种（K-12 vs O157:H7）吻合；BUSCO 谱系 enterobacterales_odb10 正确；MEMOTE 评分 68.0 / 67.7（草稿 GEM 典型值；**更正 2026-10-15 → 68.1 / 67.8，且该分数不含生长/生物质测试，见 F25**）；
- Trimmomatic/SPAdes/QUAST 与源仓库逐字节一致 → 无静默漂移；AMR-guard 正确处理 O157:H7 的 AMRFinderPlus 崩溃边缘用例。

**易用性 —— 通过（缺口已补）。** 单入口 `nextflow run .`，conda 环境自动构建，`-profile standard/lowmem`，CLI 覆盖 params，samples.tsv 沿用源格式。**原缺口已闭环（2026-10-14）**：已补 Nextflow 版 `README.md`（运行说明、参数表、产物布局、失败行为），磁盘余量 ≥20 GB 从"提醒"升级为**启动前硬预检**（F22/D13），并新增 `tests/ci/` 零成本检查套件与 GitHub Actions（阶段 D/D14）。

**未运行时验证项（更新 2026-10-14）：** 单样本失败**已完成注入实测**（F23 + `tests/ci/run-failure-isolation.sh`，并已进 CI）；F20 磁盘预检已完成三分支实测（F22）。**仍有两点是诚实缺口**：(1) 完整 `-resume` 干净复现因磁盘曾满未闭环（代码已按 F19/F20 修复并编译通过）——CI 的 stub `-resume` 只验证了缓存的**调度与发布**语义，不是真实工具产物的复用；(2) 真实数据的 10 步运行在 CI 中**不跑**（依赖 conda 环境与参考库），仍是人工验收；自动化守卫覆盖的是逻辑回归，不是工具行为回归。
**措辞更正（F23）：** 旧文档称 `errorStrategy='finish'` 即 `--keep-going`、失败样本之外"照常完成"——实测**不成立**（`finish` 只等已运行任务结束即停）。现在对外表述为：失败必定响亮、失败样本无半成品、已完成工作保留且 `-resume` 收敛；不承诺"同一次运行内继续独立分支"。
