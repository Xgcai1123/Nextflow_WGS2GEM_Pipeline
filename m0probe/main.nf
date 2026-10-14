// ============================================================
// M0 probe workflow — design-doc §12 verification
//   1. projectDir usable in nextflow.config (params.probe_root)
//   2. conda yaml `name:` field (real fastqc.yaml, name: fastqc)
//   3. $CONDA_PREFIX exported inside a conda task
//   4. publishDir referencing an input variable (${sample})
// Findings so far (Nextflow 26.10.0, strict syntax):
//   F1: top-level statements forbidden when declarations exist -> use workflow block
//   F2: legacy workflow.onComplete {} handler removed -> use onComplete: section
//       inside the entry workflow (Nextflow >= 25.10 syntax)
//   F3: publishDir "..." string cannot resolve input vals at parse time
//       -> closure form publishDir(path: {...}) under test
// ============================================================

process CHECK_CONDA_PREFIX {
    tag "${sample}"
    conda "${params.envs_dir}/fastqc.yaml"
    cpus 1

    input:
    tuple val(sample), path(fq)

    output:
    path("${sample}.env.txt")

    script:
    """
    {
        echo "CONDA_PREFIX=[\${CONDA_PREFIX:-UNSET}]"
        echo "python3=[\$(command -v python3)]"
        echo "fastqc=[\$(command -v fastqc)]"
        echo "multiqc=[\$(command -v multiqc)]"
        echo "java=[\$(command -v java)]"
    } > ${sample}.env.txt
    """
}

process CHECK_PUBLISH_INPUT {
    tag "${sample}"
    cpus 1
    publishDir(path: { "${params.out_dir}/publishinput/${sample}" }, mode: 'copy', overwrite: true)

    input:
    tuple val(sample), path(fq)

    output:
    path("${sample}.marker.txt")

    script:
    """
    echo "marker for ${sample}" > ${sample}.marker.txt
    """
}

workflow {
    main:
    log.info "[M0] projectDir from config = ${params.probe_root}"
    ch = Channel.of(
        tuple('alpha', file("${params.probe_root}/data/alpha_R1.fastq.gz")),
        tuple('beta',  file("${params.probe_root}/data/beta_R1.fastq.gz"))
    )
    CHECK_CONDA_PREFIX(ch)
    CHECK_PUBLISH_INPUT(ch)

    onComplete:
    log.info "[M0] workflow.projectDir = ${workflow.projectDir}"
    log.info "[M0] DONE"
}
