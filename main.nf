// ============================================================
// WGSpipeline-nextflow — Nextflow DSL2 port (bacterial WGS pipeline)
// Milestone 1: sample-sheet validation + FASTQC_RAW + TRIMMOMATIC +
//              FASTQC_TRIMMED. Later milestones add SPADES/QUAST/BUSCO/
//              BAKTA/CARVEME/MEMOTE/MULTIQC (design doc §7/§8).
// Syntax target: Nextflow >= 25.10 (strict syntax, M0 findings F1/F2/F3).
// ============================================================

process FASTQC_RAW {
    tag "${sample}_${read}"
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
    # fastqc 0.12 on macOS sometimes exits non-zero even when outputs are
    # written; `[ -f <html> ]` is the same guard as the snakefile. A true
    # failure (no output) still propagates as a non-zero exit.
    fastqc ${fq} -o . --threads ${task.cpus} \\
        > ${params.logs_dir}/fastqc_raw/${sample}_${read}.log 2>&1 \\
        || [ -f ${sample}_${read}_fastqc.html ]
    bench_finish fastqc_raw ${sample}_${read}
    """

    stub:
    """
    touch ${sample}_${read}_fastqc.zip ${sample}_${read}_fastqc.html
    """
}

process FASTQC_TRIMMED {
    tag "${sample}_${read}"
    conda "${params.envs_dir}/fastqc.yaml"
    cpus 4
    publishDir "${params.trim_qc_dir}", mode: 'copy', overwrite: true

    input:  tuple val(sample), val(read), path(fq)
    output: tuple val(sample), val(read),
            path("${sample}_R${read}_paired_fastqc.zip"), path("${sample}_R${read}_paired_fastqc.html")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/fastqc_trimmed
    fastqc ${fq} -o . --threads ${task.cpus} \\
        > ${params.logs_dir}/fastqc_trimmed/${sample}_${read}.log 2>&1 \\
        || [ -f ${sample}_R${read}_paired_fastqc.html ]
    bench_finish fastqc_trimmed ${sample}_${read}
    """

    stub:
    """
    touch ${sample}_R${read}_paired_fastqc.zip ${sample}_R${read}_paired_fastqc.html
    """
}

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
    # shipped in the conda env.
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

    stub:
    """
    touch ${sample}_R1_paired.fastq.gz ${sample}_R2_paired.fastq.gz
    touch ${sample}_R1_unpaired.fastq.gz ${sample}_R2_unpaired.fastq.gz
    """
}

process SPADES {
    tag "${sample}"
    conda "${params.envs_dir}/spades.yaml"
    cpus params.threads_default
    // F15: 26.x pattern does NOT strip the matched prefix -> saveAs strips 'out/'
    publishDir(path: { "${params.assembly_dir}/${sample}" }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })

    input:  tuple val(sample), path(r1_paired), path(r2_paired)
    output: tuple val(sample), path("out/contigs.fasta")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/spades
    mkdir -p out
    # Fresh workdir replaces the snakefile's `rm -rf outdir` (SPAdes refuses
    # to run into an existing -o directory).
    spades.py -1 ${r1_paired} -2 ${r2_paired} -o out \\
        --threads ${task.cpus} ${params.spades_careful} \\
        > ${params.logs_dir}/spades/${sample}.log 2>&1
    bench_finish spades ${sample}
    """

    stub:
    """
    mkdir -p out
    touch out/contigs.fasta
    """
}

process QUAST {
    tag "${sample}"
    conda "${params.envs_dir}/quast.yaml"
    cpus 1
    // F15: saveAs strips 'out/' so reports land directly in quast/<sample>/
    publishDir(path: { "${params.quast_dir}/${sample}" }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })

    input:  tuple val(sample), path(contigs)
    output: tuple val(sample), path("out/report.html"), path("out/report.txt"), path("out/report.tsv")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/quast
    mkdir -p out
    # PITFALL (kept): --labels is mandatory -- input is always contigs.fasta,
    # without it every sample is called "contigs" in QUAST/MultiQC.
    # PITFALL (kept): pip quast 5.2.0 only ships the quast.py entry point
    # (bare `quast` exits 127).
    quast.py ${contigs} -o out \\
        --labels ${sample} \\
        --min-contig ${params.quast_min_contig} \\
        --threads 1 > ${params.logs_dir}/quast/${sample}.log 2>&1
    bench_finish quast ${sample}
    """

    stub:
    """
    mkdir -p out
    touch out/report.html out/report.txt out/report.tsv
    """
}

process BUSCO {
    tag "${sample}"
    conda "${params.envs_dir}/busco.yaml"
    cpus 4
    // F15: saveAs strips 'out/' -> out/<sample>/... lands in busco/<sample>/...
    publishDir "${params.busco_dir}", pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename }

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
    # PITFALL (kept): bin/busco's shebang resolves via PATH; invoke with the
    # conda env's own python3 explicitly.
    "\${CONDA_PREFIX}/bin/python3" "\${CONDA_PREFIX}/bin/busco" \\
        -i ${contigs} -m genome \\
        -l ${params.busco_lineage} \\
        -o ${sample} --out_path out \\
        --cpu ${task.cpus} \\
        --download_path ${params.busco_db} \\
        > ${params.logs_dir}/busco/${sample}.log 2>&1
    bench_finish busco ${sample}
    """

    stub:
    """
    mkdir -p out/${sample}
    touch out/${sample}/short_summary.specific.${params.busco_lineage}.${sample}.json
    touch out/${sample}/short_summary.specific.${params.busco_lineage}.${sample}.txt
    """
}

process BAKTA {
    tag "${sample}"
    conda "${params.envs_dir}/bakta.yaml"
    cpus 4
    publishDir(path: { "${params.annot_dir}/${sample}" }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })

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

    # Prepend the AMR-guard wrapper dir so it shadows the conda bakta entry
    # point (upstream ncbi/amr#191: one protein with overlapping NCBIFAM hits
    # kills the whole run).
    export PATH="${params.scripts_dir}/bin:\$PATH"

    # D8 (kept): the snakefile's `2> >(tee ...)` process substitution is dropped;
    # Nextflow captures stdout+stderr natively in .command.log, and we merge them
    # into the same log (progress bar goes non-tty, cosmetic only).
    bakta --db ${params.bakta_db} \\
        --prefix ${sample} \\
        --output out \\
        --threads ${task.cpus} \\
        --force --skip-plot ${contigs} \\
        > ${params.logs_dir}/bakta/${sample}.log 2>&1
    bench_finish bakta ${sample}
    """

    stub:
    """
    mkdir -p out
    touch out/${sample}.gff3 out/${sample}.faa out/${sample}.tsv out/${sample}.ffn out/${sample}.gbff out/${sample}.txt
    """
}

process CARVEME {
    tag "${sample}"
    conda "${params.envs_dir}/carveme.yaml"
    cpus 4
    publishDir(path: { "${params.carveme_dir}/${sample}" }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })

    input:  tuple val(sample), path(faa)
    output: tuple val(sample), path("out/${sample}.xml"), path("out/${sample}.faa"), path("out/${sample}.tsv")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/carveme
    mkdir -p out

    # CRITICAL (docs/known-issues.md #5): carve derives its diamond output path
    # from the input path (stem + '.tsv'). Feeding bakta's faa directly would
    # overwrite annotation/bakta/<sample>/<sample>.tsv. Always carve this staged
    # private copy so the intermediate lands in CarveMe's own directory.
    cp ${faa} out/${sample}.faa
    carve out/${sample}.faa -o out/${sample}.xml \\
        --solver ${params.carveme_solver} \\
        --verbose > ${params.logs_dir}/carveme/${sample}.log 2>&1
    bench_finish carveme ${sample}
    """

    stub:
    """
    mkdir -p out
    touch out/${sample}.xml out/${sample}.faa out/${sample}.tsv
    """
}

// step 10 -- MEMOTE (model quality suite). Runs AFTER GROWTH_CHECK (step 9); the
// ordering is enforced by the `growth_done` edge below, not by file order (F29).
process MEMOTE {
    tag "${sample}"
    conda "${params.envs_dir}/memote.yaml"
    cpus 4
    publishDir(path: { "${params.memote_dir}/${sample}" }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })

    input:
    // F29: `growth_done` is an ordering edge from GROWTH_CHECK. The value is
    // unused; the CHANNEL is the point -- without it this branch and GROWTH_CHECK
    // could run in either order, because both depend only on CARVEME. It is part
    // of the tuple (rather than a second input) on purpose: a single-valued input
    // channel next to a per-sample one is consumed once, which silently drops all
    // but the first task (caught by the stub-smoke task count).
    tuple val(sample), path(model), val(growth_done)
    output: tuple val(sample), path("out/index.html"),
            path("out/memote_result.json"), path("out/memote_summary.tsv")

    script:
    """
    source ${params.bin_dir}/task_env.sh
    export BENCH_DIR=${params.bench_dir}
    bench_start
    mkdir -p ${params.logs_dir}/memote
    mkdir -p out
    # F29: the cheap viability check (~6 s) runs before this suite (~250 s/sample);
    # record that the edge held so a log reader can see the ordering.
    echo "[ordering] growth check completed before MEMOTE (${growth_done})" \\
        >> ${params.logs_dir}/memote/${sample}.log
    # PITFALL (kept): invoke with the conda env's own python3 (cobra module).
    "\${CONDA_PREFIX}/bin/python3" ${params.scripts_dir}/memote_report.py ${model} \\
        --outdir out --sample ${sample} --solver ${params.memote_solver} \\
        >> ${params.logs_dir}/memote/${sample}.log 2>&1
    bench_finish memote ${sample}
    """

    stub:
    """
    mkdir -p out
    touch out/index.html out/memote_result.json out/memote_summary.tsv
    """
}

// step 9 -- growth check on a declared medium. Defined after MEMOTE in this file
// because it was added later, but it RUNS first: see the ordering edge in
// workflow.main (F29).
process GROWTH_CHECK {
    tag "cohort"
    conda "${params.envs_dir}/memote.yaml"     // cobra + a solver live in this env
    cpus 1
    publishDir(path: { params.growth_dir }, pattern: 'out/**', mode: 'copy', overwrite: true,
               saveAs: { filename -> filename.startsWith('out/') ? filename - 'out/' : filename })

    input:
    path models              // collect(): every CarveMe model, so ONE task (F26/D17)
    path medium              // media/m9_aerobic.yaml
    path checker             // scripts/check_growth.py

    output:
    path("out/growth_mqc.tsv"), emit: mqc
    path("out/growth.tsv"),     emit: tsv

    script:
    """
    mkdir -p ${params.logs_dir}/growth out

    # F26: assert physiological behaviour under a DECLARED medium. The CarveMe
    # models ship with every exchange reaction open (a complete medium), under
    # which FBA reports ~66 1/h -- an assembly-rate artefact, not a growth rate.
    #
    # --report-only is deliberate (D17): the plausibility window is E. coli
    # specific and this pipeline can be pointed at other organisms
    # (--busco_lineage), so a window violation is REPORTED -- it lands in the
    # MultiQC section below and in the log -- instead of failing the run. A
    # medium that does not match the model is still fatal (exit 2), because that
    # is a configuration defect rather than a biological finding.
    #
    # No bench_start/bench_finish here: benchmarks/ carries the Snakemake step
    # set that multiqc_extras.py maps to labelled runtime columns, and this step
    # has no label there -- a file would surface as a raw "growth_check" column.
    "\${CONDA_PREFIX}/bin/python3" ${checker} \\
        --media ${medium} \\
        --report-only \\
        --mqc-out out/growth_mqc.tsv \\
        --tsv-out out/growth.tsv \\
        ${models} > ${params.logs_dir}/growth/growth_check.log 2>&1
    """

    stub:
    """
    mkdir -p out
    touch out/growth_mqc.tsv out/growth.tsv
    """
}

process MULTIQC {
    tag "${report_name}"
    conda "${params.envs_dir}/fastqc.yaml"
    cpus 1
    publishDir "${params.multiqc_dir}", mode: 'copy', overwrite: true

    input:
    val  report_name             // "multiqc_report" + optional tag (F19: computed in
                                 // workflow.main; process directives can't see runtime
                                 // params mutation, so pass it as a channel input)
    val  samples                 // List<String>: cohort, from the sheet
    val  upstream                // collected trigger: all upstream steps done
    path multiqc_config
    path multiqc_config_extra    // presentation overlay: pipeline section order +
                                 // full MEMOTE / QUAST HTML embedding (F21/F24)
    path growth_section          // F26: growth-on-defined-medium table (*_mqc.tsv
                                 // is auto-discovered, so no `sp:` entry is needed)

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
    "\${CONDA_PREFIX}/bin/python3" ${params.scripts_dir}/multiqc_extras.py \\
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
        set -- ${params.busco_dir}"/\$s"/short_summary.specific.*.txt
        if [ -f "\$1" ]; then
            ln -sf "\$1" busco_stage/short_summary_"\$s".txt
        fi
    done

    # ---- 2b. MEMOTE 完整报告内嵌段（每样本一个 *_mqc.html；
    #          MEMOTE 是自带 CSS/JS 的完整页面，这里包成独立 iframe，避免污染本报告）----
    rm -rf memote_html
    mkdir -p memote_html
    "\${CONDA_PREFIX}/bin/python3" ${params.scripts_dir}/memote_html_section.py \\
        --memote-dir ${params.memote_dir} \\
        --samples "${samples.join(' ')}" \\
        --outdir memote_html \\
        --report-dir ${params.multiqc_dir} \\
        >> ${params.logs_dir}/multiqc.log 2>&1

    # ---- 2c. QUAST 自身输出文件内嵌段（report.html 完整交互报告 + report.txt 纯文本；
    #          report.html 是自带 Bootstrap CSS/22 段 JS 的完整页面，同样包成独立 iframe；
    #          全队列只出一个 *_mqc.html —— report_section_order 的锚点是精确匹配、
    #          不支持通配，逐样本 id 无法排到 QUAST 表旁，故用固定锚点 quast_full）----
    rm -rf quast_html
    mkdir -p quast_html
    "\${CONDA_PREFIX}/bin/python3" ${params.scripts_dir}/quast_html_section.py \\
        --quast-dir ${params.quast_dir} \\
        --samples "${samples.join(' ')}" \\
        --outdir quast_html \\
        --report-dir ${params.multiqc_dir} \\
        >> ${params.logs_dir}/multiqc.log 2>&1

    # ---- 3. 只喂本队列的显式文件（不是目录！）----
    qc_files=""
    for s in ${samples.join(' ')}; do
        qc_files="\$qc_files ${params.raw_qc_dir}/\${s}_1_fastqc.zip ${params.raw_qc_dir}/\${s}_2_fastqc.zip"
        qc_files="\$qc_files ${params.trim_qc_dir}/\${s}_R1_paired_fastqc.zip ${params.trim_qc_dir}/\${s}_R2_paired_fastqc.zip"
        qc_files="\$qc_files ${params.quast_dir}/\${s}/report.tsv"
    done

    # -c twice: the reused config first, the presentation overlay second.
    multiqc \$qc_files busco_stage ${params.cc_dir} memote_html quast_html ${growth_section} \\
        -c ${multiqc_config} \\
        -c ${multiqc_config_extra} \\
        -o . -f -n ${report_name} \\
        >> ${params.logs_dir}/multiqc.log 2>&1

    bench_finish multiqc ""
    """

    stub:
    """
    touch ${report_name}.html
    """
}

workflow {
    main:
    // ---- disk pre-flight (F20) --------------------------------------------
    // A full volume does not fail loudly, it fails expensively: SPAdes' own
    // `gzip -f -7` died mid read-correction with OS return 1 when the host had
    // ~2 GB left, and a near-full disk was measured to inflate individual
    // steps up to 17x. Refuse to start instead of losing hours of compute.
    // Runs before the sample sheet so even a bad sheet cannot mask the cause.
    def min_free_gb = (params.min_free_disk_gb ?: 0) as double
    if (min_free_gb > 0) {
        def disk_targets = [
            'work dir'           : params.project_dir,
            'raw read QC'        : params.raw_qc_dir,
            'trimmed reads'      : params.trim_dir,
            'assembly (SPAdes)'  : params.assembly_dir,
            'annotation (Bakta)' : params.annot_dir,
            'model (CarveMe)'    : params.carveme_dir,
            'model (MEMOTE)'     : params.memote_dir,
            'growth check (F26)' : params.growth_dir,
            'reports (MultiQC)'  : params.multiqc_dir,
        ]
        def volumes = [:]            // filesystem -> [anchor path, usable bytes, total bytes]
        // publishDir targets may not exist yet on the first run, and the
        // filesystem is decided by the nearest existing ancestor. Strict syntax
        // forbids loops, so the ancestor chain is built declaratively
        // (root -> ... -> full path) by injecting each name element.
        def ancestors_of = { dir ->
            def abs = java.nio.file.Paths.get(dir.toString()).toAbsolutePath().normalize()
            def root = abs.getRoot()
            if (root == null) { return [abs] }
            // NB: `acc + p` would concatenate p's name elements (Path is
            // Iterable); the extra [] forces List + List append semantics.
            return abs.iterator().collect { it.toString() }
                      .inject([root]) { acc, name -> acc + [acc.last().resolve(name)] }
        }
        def nearest_existing = { dir ->
            return ancestors_of.call(dir).reverse().find { java.nio.file.Files.exists(it) }
        }
        disk_targets.each { label, dir ->
            def anchor = nearest_existing.call(dir)
            if (anchor == null) {
                error "[disk-check] ${label}: no existing directory on the path of ${dir}"
            }
            def store = java.nio.file.Files.getFileStore(anchor)
            if (!volumes.containsKey(store.name())) {
                volumes[store.name()] = [anchor.toString(), store.getUsableSpace(), store.getTotalSpace()]
            }
        }
        def too_full = []
        volumes.each { fs, v ->
            def free_gb  = v[1] / 1073741824
            def total_gb = v[2] / 1073741824
            log.info "[disk-check] ${fs}: ${String.format('%.1f', free_gb)} GiB free of " +
                     "${String.format('%.1f', total_gb)} GiB (anchor ${v[0]})"
            if (free_gb < min_free_gb) {
                too_full << "${fs}: ${String.format('%.1f', free_gb)} GiB free < ${min_free_gb} GiB required (anchor ${v[0]})"
            }
        }
        if (too_full) {
            error """[disk-check] refusing to start: not enough free disk space.
  ${too_full.join('\n  ')}

  Free space first -- both directories below are regenerable, only time is lost:
    rm -rf ${params.project_dir}/work             # Nextflow task cache
    rm -rf ${params.project_dir}/.nextflow/conda  # conda environments
  Or tune the threshold: --min_free_disk_gb=<GB>, or --min_free_disk_gb=0 to skip the check."""
        }
    }

    // ---- sample sheet: parse + validate (M0 F1: must live here, before any
    //      process invocation; error() aborts before any task) ----
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
    def resolve     = { fq, row ->        // hoisted (F9: typed/inner closures unsupported)
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
    def report_name = params.report_name   // F19: local copy; process directives can't see runtime params mutation

    // ---- M1 dataflow: FASTQC_RAW ∥ TRIMMOMATIC -> FASTQC_TRIMMED ----
    // Channel.fromList: Channel.of(list) no longer splats a collection in 26.x
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

    // ---- step 9: growth check on a declared medium (F26/D17) ----------------
    // One aggregate task -- the report gets one section with a row per sample, so
    // collecting the models here mirrors that shape (cf. D15). `_`-prefixed lambda
    // parameters are unused by design: only the SBML path is needed, and the lint
    // ratchet must not grow.
    GROWTH_CHECK(
        CARVEME.out.map { _n, xml, _faa, _tsv -> xml }.collect(),
        channel.of(file(params.growth_medium)),
        channel.of(file("${params.scripts_dir}/check_growth.py"))
    )

    // ---- step 10: model quality suite (F29: AFTER the growth check) ----------
    // Declaration order alone does not order two branches that both depend only on
    // CARVEME, so the edge is made real: MEMOTE consumes a trigger emitted by
    // GROWTH_CHECK. `combine` broadcasts that single value across the per-sample
    // tuples (2 samples x 1 value), keeping one task per sample.
    // Rationale: the growth check costs ~6 s and answers "is this model viable at
    // all", while MEMOTE costs ~250 s per sample and scores something narrower.
    MEMOTE(
        CARVEME.out.map { n, xml, _faa, _tsv -> tuple(n, xml) }
            .combine(GROWTH_CHECK.out.tsv.map { _f -> true })
    )

    // dependency trigger: MULTIQC waits for all upstream steps (their outputs are
    // read from repo paths by multiqc_extras.py / the BUSCO staging loop).
    up_ok = channel.empty()
        .mix(FASTQC_RAW.out.map    { n, r, z, h -> z })
        .mix(FASTQC_TRIMMED.out.map{ n, r, z, h -> z })
        .mix(QUAST.out.map         { n, html, txt, tsv -> tsv })
        .mix(BUSCO.out.map         { n, j, t -> t })
        .mix(CARVEME.out.map       { n, xml, faa, tsv -> xml })
        .mix(MEMOTE.out.map        { n, html, json, tsv -> tsv })
        .collect()

    MULTIQC(
        report_name,                           // val: report filename (no runtime params mutation)
        channel.value(SAMPLE_NAMES),          // cohort == the sheet, never a dir scan
        up_ok,
        channel.of(file(params.multiqc_config)),
        channel.of(file(params.multiqc_config_extra)),
        GROWTH_CHECK.out.mqc                   // F26: growth table, one section
    )

    onComplete:
    log.info "[NextFlow-WGS2GEM v${workflow.manifest.version}] workflow dir = ${params.project_dir}"
    log.info "[NextFlow-WGS2GEM v${workflow.manifest.version}] report       = ${params.multiqc_dir}/${params.report_name}.html"
    log.info "[NextFlow-WGS2GEM v${workflow.manifest.version}] rerun a step = nextflow run . -resume (delete the output to force it)"
}
