#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 MultiQC 生成 custom content 数据文件（无需给 MultiQC 写插件）。

/ Generate MultiQC custom content data files (no MultiQC plugin required).

背景 / Background
-----------------

MultiQC 覆盖 180+ 工具，但有四样我们想要的东西它管不了：

1. **MEMOTE 没有 MultiQC 模块** —— 代谢模型评分完全进不了汇总报告。
2. **CarveMe 也没有 MultiQC 模块** —— 草稿模型规模（反应/代谢物/基因数）进不了报告。
3. **QUAST 模块的 General Stats 表把列写死了** —— 只有 N50 和 Total length；
   L50 / contig 数 / GC% 只出现在 QUAST 自己的段落表里，进不了顶部的 General Stats。
4. **各步骤耗时** —— 谁跑得慢，报告里看不到（readme 备注里的 “time stand”）。

MultiQC 的 `custom_content` 模块可以把任意 TSV 渲染成表格 / General Stats 列，
本脚本就是把上述四类数据整理成它认识的 TSV：

- `quast_generalstats.tsv`  ← `data/qc/quast/<sample>/report.tsv`
                              （L50 / contigs / GC%，补进 General Stats）
- `carveme_summary.tsv`     ← `models/carveme/<sample>/<sample>.xml`（SBML，表格用）
- `carveme_modelstats.tsv`  ← 同上，只含 reactions/metabolites（General Stats 用）
- `memote_summary.tsv`      ← `models/memote/<sample>/memote_summary.tsv`（合并各样本）
- `memote_score.tsv`        ← 同上，只含 Total score（General Stats 用）
- `pipeline_runtime.tsv`    ← `benchmarks/<step>/<sample>.tsv`（Snakemake benchmark）

注意：给 General Stats 的段（`*_modelstats.tsv` / `memote_score.tsv`）刻意用**单独的
文件**，而不是复用表格那个 TSV —— 同一个文件被两个搜索模式命中时，MultiQC 的
generalstats 段会静默失效。详见 `docs/known-issues.md` 第 3 条。
/ NOTE: the generalstats feeds use separate files on purpose; two search patterns on
one file silently kills the generalstats section (docs/known-issues.md #3).

TSV 约定（MultiQC custom content）／TSV contract:
  第 1 行是表头，第 1 列是样本名；其余列是数值。

本脚本只用 Python 标准库，跑在 multiqc 所在的 conda 环境里即可。
/ Stdlib-only; runs inside the MultiQC conda env.

用法 / Usage:

    python3 scripts/multiqc_extras.py --outdir reports/multiqc/custom_content
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import xml.etree.ElementTree as ET

# 整条流水线级别的步骤（没有 per-sample 产物）在表里的“样本名”。
# / Row label used for whole-pipeline steps that have no per-sample output.
PIPELINE_ROW = "all samples"

# 步骤显示名。键的**顺序**就是流水线顺序，决定表里列的顺序；
# 未知的 step 会按字母序追加到后面。
# / Step labels. Key order = pipeline order and therefore column order in the table.
# "(s)" 表示该列单位是秒 / "(s)" = the column is measured in seconds.
STEP_LABELS = {
    "fastqc_raw": "FastQC raw (s)",
    "trimmomatic": "Trimmomatic (s)",
    "fastqc_trimmed": "FastQC trimmed (s)",
    "spades": "SPAdes (s)",
    "quast": "QUAST (s)",
    "busco": "BUSCO (s)",
    "bakta": "Bakta (s)",
    "carveme": "CarveMe (s)",
    "memote": "MEMOTE (s)",
    "multiqc": "MultiQC (s)",
}

# 要补进 MultiQC General Stats 的 QUAST 指标：输出列名 -> report.tsv 里的指标名。
# / QUAST metrics to surface in the MultiQC General Stats table: column -> metric name.
QUAST_COLUMNS = [
    ("L50", "L50"),
    ("contigs", "# contigs"),
    ("gc_percent", "GC (%)"),
]

# CarveMe 草稿模型的规模指标（列表顺序 = 输出列顺序）。
# 表格段显示全部四项；General Stats 只取前两项（别让顶部的表继续变宽）。
# / CarveMe draft model size metrics (list order = column order). The table section
# shows all four; General Stats only the first two.
CARVEME_COLUMNS = ["reactions", "metabolites", "genes", "compartments"]
CARVEME_GENERALSTATS_COLUMNS = ["reactions", "metabolites"]

# memote rule 产出的评分摘要文件名 / summary filename produced by the memote rule
MEMOTE_SUMMARY_NAME = "memote_summary.tsv"

QUAST_OUTPUT = "quast_generalstats.tsv"
CARVEME_OUTPUT = "carveme_summary.tsv"
CARVEME_STATS_OUTPUT = "carveme_modelstats.tsv"
MEMOTE_OUTPUT = MEMOTE_SUMMARY_NAME
MEMOTE_SCORE_OUTPUT = "memote_score.tsv"
RUNTIME_OUTPUT = "pipeline_runtime.tsv"

# memote 摘要里的总分列名（由 scripts/memote_report.py 写入）。
# / Column holding the total score in the memote summary.
TOTAL_SCORE_COLUMN = "Total score"


# -----------------------------------------------------------------------------
# 通用小工具 / Generic helpers
# -----------------------------------------------------------------------------
def warn(message: str) -> None:
    """非致命问题只打警告，绝不返回非零（否则会连累 multiqc rule 失败）。"""
    print("[multiqc_extras] warning: {}".format(message), file=sys.stderr)


def read_sample_names(path: str) -> "list[str]":
    """读 samples.tsv 的 sample 列（保序去重）。

    / Read the `sample` column of samples.tsv (order preserved, de-duplicated).
    """
    if not path or not os.path.isfile(path):
        warn("sample sheet not found: {}".format(path))
        return []
    names: "list[str]" = []
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if not reader.fieldnames or "sample" not in reader.fieldnames:
                warn("{} has no 'sample' column".format(path))
                return []
            for row in reader:
                name = (row.get("sample") or "").strip()
                if name and name not in names:
                    names.append(name)
    except OSError as exc:
        warn("cannot read {}: {}".format(path, exc))
        return []
    return names


def ordered_keys(found, samples: "list[str]") -> "list[str]":
    """按 samples.tsv 的顺序排列，额外的 key 按字母序追加。

    / Sort keys by samples.tsv order; extra keys are appended alphabetically.
    """
    ordered = [sample for sample in samples if sample in found]
    ordered += sorted(key for key in found if key not in ordered)
    return ordered


def write_tsv(path: str, header: "list[str]", rows: "list[list]"):
    """写 TSV；空表直接跳过（空文件会让 MultiQC 打 "No data found" 警告）。

    返回是否真的写了文件。
    / Write a TSV, skipping empty ones (an empty file makes MultiQC log
    "No data found"). Returns whether the file was written.
    """
    if not rows:
        warn("nothing to write for {}, skipping".format(os.path.basename(path)))
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    print(
        "[multiqc_extras] wrote {} ({} row(s), {} column(s))".format(
            path, len(rows), len(header)
        )
    )
    return True


def parse_number(text: str):
    """能转数字就转，否则原样返回（QUAST 偶尔给出 '-' 之类的非数值）。"""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return text
    return int(value) if value.is_integer() else value


# -----------------------------------------------------------------------------
# 1. QUAST → General Stats（L50 / contigs / GC%）
# -----------------------------------------------------------------------------
def parse_quast_metrics(path: str) -> "dict[str, object]":
    """解析 QUAST 的 report.tsv。

    格式：第 1 行是 `Assembly<TAB><label>`，之后每行是 `指标<TAB>值`。
    这里只取第 1 个 assembly 那一列（本流水线每样本单独跑 QUAST，只有一列）。
    / Parse QUAST's report.tsv: first row is `Assembly<TAB><label>`, every later row
    is `metric<TAB>value`. Only the first assembly column is used.
    """
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            rows = [line.rstrip("\n").split("\t") for line in handle if line.strip()]
    except OSError as exc:
        warn("cannot read {}: {}".format(path, exc))
        return {}
    if len(rows) < 2 or len(rows[0]) < 2:
        return {}
    metrics = {}
    for row in rows[1:]:
        if len(row) >= 2:
            metrics[row[0].strip()] = parse_number(row[1].strip())
    return metrics


def write_quast_generalstats(quast_dir: str, samples: "list[str]", outdir: str) -> bool:
    found = {}
    if os.path.isdir(quast_dir):
        for name in sorted(os.listdir(quast_dir)):
            if os.path.isfile(os.path.join(quast_dir, name, "report.tsv")):
                found[name] = True
    else:
        warn("QUAST dir not found: {}".format(quast_dir))

    rows = []
    for sample in ordered_keys(found, samples):
        metrics = parse_quast_metrics(os.path.join(quast_dir, sample, "report.tsv"))
        if not metrics:
            warn("{}/report.tsv has no parsable metrics".format(sample))
            continue
        rows.append([sample] + [metrics.get(metric, "") for _, metric in QUAST_COLUMNS])

    return write_tsv(
        os.path.join(outdir, QUAST_OUTPUT),
        ["Sample"] + [column for column, _ in QUAST_COLUMNS],
        rows,
    )


# -----------------------------------------------------------------------------
# 2. CarveMe 草稿模型规模（流式解析 SBML，不需要 cobra）
# -----------------------------------------------------------------------------
def local_name(tag: str) -> str:
    """去掉 XML 命名空间的 {ns} 前缀：'{ns}species' -> 'species'。

    / Strip the XML namespace prefix: '{ns}species' -> 'species'.
    """
    return tag.rsplit("}", 1)[-1]


def sbml_model_stats(path: str) -> "dict[str, int]":
    """流式解析 SBML，返回模型规模。

    按**元素本地名**统计，因此不依赖调用方用的是 core 还是 fbc 命名空间前缀。

    基因数按**去重的 geneProduct 引用**统计，而不是数 <fbc:listOfGeneProducts> 的子元素：
    CarveMe 的模型通常不写这个声明，只在反应的 <fbc:geneProductAssociation> 里用
    <fbc:geneProductRef fbc:geneProduct="G_..."/> 引用基因。

    / Count by local element name (namespace-agnostic). Genes are counted as unique
    geneProductRef targets, because CarveMe usually omits <fbc:listOfGeneProducts>
    and only references genes from reactions.
    """
    stats = {"reactions": 0, "metabolites": 0, "genes": 0, "compartments": 0}
    genes = set()
    try:
        # events=("end",) + elem.clear()：整份 SBML 不会常驻内存。
        # / Streaming parse so the whole SBML never sits in memory.
        for _event, elem in ET.iterparse(path, events=("end",)):
            tag = local_name(elem.tag)
            if tag == "reaction":
                stats["reactions"] += 1
            elif tag == "species":
                stats["metabolites"] += 1
            elif tag == "compartment":
                stats["compartments"] += 1
            elif tag == "geneProductRef":
                for key, value in elem.attrib.items():
                    if local_name(key) == "geneProduct":
                        genes.add(value)
            elem.clear()
    except ET.ParseError as exc:
        warn("cannot parse SBML {}: {}".format(path, exc))
        return {}
    stats["genes"] = len(genes)
    return stats


def find_model_file(directory: str):
    """返回目录下第一个 .xml（CarveMe 每样本只产出一个模型文件）。

    / Return the first .xml in a directory (CarveMe emits one model per sample).
    """
    if not os.path.isdir(directory):
        return None
    for name in sorted(os.listdir(directory)):
        if name.endswith(".xml"):
            return os.path.join(directory, name)
    return None


def write_carveme_summary(carveme_dir: str, samples: "list[str]", outdir: str) -> bool:
    """CarveMe 草稿模型规模：写表格用的完整表 + General Stats 用的精简表。

    与 MEMOTE 同样刻意分成**两个文件**：同一个文件被两个搜索模式命中时，
    MultiQC 的 generalstats 段会静默失效（见 docs/known-issues.md 第 3 条）。

    / Two files on purpose, same reason as MEMOTE: two search patterns on one file
    silently kills the MultiQC generalstats section.
    """
    found = {}
    if os.path.isdir(carveme_dir):
        for name in sorted(os.listdir(carveme_dir)):
            if find_model_file(os.path.join(carveme_dir, name)):
                found[name] = True
    else:
        warn("CarveMe dir not found: {}".format(carveme_dir))

    records: "list[tuple[str, dict]]" = []
    for sample in ordered_keys(found, samples):
        model_path = find_model_file(os.path.join(carveme_dir, sample))
        if model_path is None:
            continue
        stats = sbml_model_stats(model_path)
        if not stats:
            warn("{}: no parsable SBML model in {}".format(sample, model_path))
            continue
        records.append((sample, stats))

    if not records:
        warn("no CarveMe models under {}, skipping".format(carveme_dir))
        return False

    written = write_tsv(
        os.path.join(outdir, CARVEME_OUTPUT),
        ["Sample"] + CARVEME_COLUMNS,
        [
            [sample] + [stats.get(column, "") for column in CARVEME_COLUMNS]
            for sample, stats in records
        ],
    )
    write_tsv(
        os.path.join(outdir, CARVEME_STATS_OUTPUT),
        ["Sample"] + CARVEME_GENERALSTATS_COLUMNS,
        [
            [sample] + [stats.get(column, "") for column in CARVEME_GENERALSTATS_COLUMNS]
            for sample, stats in records
        ],
    )
    return written


# -----------------------------------------------------------------------------
# 3. MEMOTE 评分摘要合并（每样本一个 TSV -> 一个 TSV）
# -----------------------------------------------------------------------------
def read_tsv(path: str) -> "list[list[str]]":
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return [
                row for row in csv.reader(handle, delimiter="\t") if any(row)
            ]
    except OSError as exc:
        warn("cannot read {}: {}".format(path, exc))
        return []


def write_memote_summary(memote_dir: str, samples: "list[str]", outdir: str) -> bool:
    """把各样本的 memote_summary.tsv 合并成一个。

    每个样本的列集合理论上一样（同一个 memote 版本），但这里仍然取并集并保序，
    这样即使某个样本缺某个 section 也不会错位。
    / Merge the per-sample summaries. Columns are unioned (order preserved) so a
    sample missing a section can never shift the other values.
    """
    found = {}
    if os.path.isdir(memote_dir):
        for name in sorted(os.listdir(memote_dir)):
            if os.path.isfile(os.path.join(memote_dir, name, MEMOTE_SUMMARY_NAME)):
                found[name] = True
    else:
        warn("MEMOTE dir not found: {}".format(memote_dir))

    columns: "list[str]" = []
    records: "list[tuple[str, dict]]" = []
    for sample in ordered_keys(found, samples):
        table = read_tsv(os.path.join(memote_dir, sample, MEMOTE_SUMMARY_NAME))
        if len(table) < 2:
            warn("{}/{} has no data row".format(sample, MEMOTE_SUMMARY_NAME))
            continue
        header, values = table[0], table[1]
        record = dict(zip(header, values))
        for column in header[1:]:
            if column not in columns:
                columns.append(column)
        records.append((sample, record))

    rows = [
        [sample] + [record.get(column, "") for column in columns]
        for sample, record in records
    ]
    written = write_tsv(
        os.path.join(outdir, MEMOTE_OUTPUT), ["Sample"] + columns, rows
    )

    # 额外写一个**只含总分**的文件给 General Stats 用。
    # 必须另起一个文件：MultiQC 对「同一个文件被两个搜索模式命中」的处理不可靠——
    # 实测把 memote_summary.tsv 同时挂在 table 段和 generalstats 段时，
    # generalstats 段完全不生效（log 里连一行都没有），也不报错。
    # / Write a separate score-only file for General Stats. Two search patterns
    # matching the SAME file is unreliable in MultiQC: pointing both the table and the
    # generalstats section at memote_summary.tsv silently produced no generalstats
    # column at all (not even a log line).
    score_column = TOTAL_SCORE_COLUMN if TOTAL_SCORE_COLUMN in columns else (
        columns[0] if columns else None
    )
    if score_column:
        write_tsv(
            os.path.join(outdir, MEMOTE_SCORE_OUTPUT),
            ["Sample", score_column],
            [[sample, record.get(score_column, "")] for sample, record in records],
        )
    return written


# -----------------------------------------------------------------------------
# 4. Snakemake benchmark -> 各步骤耗时表
# -----------------------------------------------------------------------------
def to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def hms_to_seconds(text) -> "float | None":
    """'0:01:23.5' -> 83.5 秒。"""
    parts = [to_float(part) for part in str(text).split(":")]
    if not parts or any(part is None for part in parts):
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60.0 + part
    return seconds


def benchmark_seconds(path: str):
    """读 Snakemake 的 benchmark TSV，返回 wall time（秒）。

    benchmark 文件形如：

        s       h:m:s   max_rss max_vms ...
        83.5    0:01:23 1234.5  2345.6  ...

    列名随 Snakemake 版本变过（`s` / `wall_time`），这里都兼容。
    / Read Snakemake's benchmark TSV and return the wall time in seconds.
    Column names changed across Snakemake versions (`s` / `wall_time`); both work.
    """
    lines = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("#") or not line.strip():
                    continue
                lines.append(line.rstrip("\n"))
    except OSError as exc:
        warn("cannot read {}: {}".format(path, exc))
        return None
    if len(lines) < 2:
        return None
    header = [cell.strip() for cell in lines[0].split("\t")]
    row = dict(zip(header, lines[1].split("\t")))
    for key in ("s", "wall_time"):
        if key in row:
            return to_float(row[key])
    if "h:m:s" in row:
        return hms_to_seconds(row["h:m:s"])
    return None


def resolve_sample(stem: str, samples: "list[str]") -> str:
    """从 benchmark 文件名（如 SRR1770413_1）反推样本名。

    fastq 相关的 rule 每个 read 一个 benchmark 文件，文件名是 `<sample>_<read>`；
    用 samples.tsv 里的样本名做最长前缀匹配，避免样本名本身含下划线时切错。
    / Recover the sample name from a benchmark filename (`<sample>_<read>` for the
    FastQC rules) using longest-prefix matching against samples.tsv, so sample names
    containing underscores are still split correctly.
    """
    best = None
    for sample in samples:
        if stem == sample or stem.startswith(sample + "_"):
            if best is None or len(sample) > len(best):
                best = sample
    if best is not None:
        return best
    # samples.tsv 不可用时的兜底：去掉 fastqc 的 _1 / _2 后缀。
    # / Fallback when the sample sheet is unavailable.
    return re.sub(r"_[12]$", "", stem)


def collect_benchmarks(bench_dir: str, samples: "list[str]"):
    """返回 (per_sample, pipeline)：per_sample[step][sample] = 秒；pipeline[step] = 秒。

    `benchmarks/<step>/<sample>.tsv` 是 per-sample 的；
    `benchmarks/<step>.tsv` 是整条流水线级别的（例如 multiqc）。
    同一 (step, sample) 有多个文件（fastqc 的 read 1/2）时按求和合并。
    / Per-sample steps live in `benchmarks/<step>/<sample>.tsv`; pipeline-level steps
    (e.g. multiqc) in `benchmarks/<step>.tsv`. Multiple files per (step, sample) --
    FastQC read 1/2 -- are summed.
    """
    per_sample: "dict[str, dict[str, float]]" = {}
    pipeline: "dict[str, float]" = {}
    if not os.path.isdir(bench_dir):
        warn("benchmark dir not found: {}".format(bench_dir))
        return per_sample, pipeline

    for root, _dirs, files in os.walk(bench_dir):
        for filename in files:
            if not filename.endswith(".tsv"):
                continue
            path = os.path.join(root, filename)
            relative = os.path.relpath(path, bench_dir)
            parts = relative.split(os.sep)
            stem = os.path.splitext(parts[-1])[0]
            seconds = benchmark_seconds(path)
            if seconds is None:
                warn("no wall time in {}".format(relative))
                continue
            if len(parts) > 1:
                step = parts[0]
                sample = resolve_sample(stem, samples)
                per_sample.setdefault(step, {})
                per_sample[step][sample] = per_sample[step].get(sample, 0.0) + seconds
            else:
                pipeline[stem] = pipeline.get(stem, 0.0) + seconds

    return per_sample, pipeline


def write_pipeline_runtime(bench_dir: str, samples: "list[str]", outdir: str) -> bool:
    per_sample, pipeline = collect_benchmarks(bench_dir, samples)

    steps = [step for step in STEP_LABELS if step in per_sample or step in pipeline]
    steps += sorted(
        step
        for step in set(per_sample) | set(pipeline)
        if step not in steps
    )
    if not steps:
        warn("no benchmark files under {}, skipping runtime table".format(bench_dir))
        return False

    rows = []
    # 行 = 出现在任意 step 里的样本名，按 samples.tsv 排序；多余的样本按字母序追加。
    # / Rows = every sample seen in any step, ordered by samples.tsv.
    all_samples = set()
    for entry in per_sample.values():
        all_samples.update(entry)
    for sample in ordered_keys(all_samples, samples):
        rows.append(
            [sample]
            + [
                "{:.1f}".format(entry[sample]) if sample in entry else ""
                for entry in (per_sample.get(step, {}) for step in steps)
            ]
        )

    if pipeline:
        rows.append(
            [PIPELINE_ROW]
            + [
                "{:.1f}".format(pipeline[step]) if step in pipeline else ""
                for step in steps
            ]
        )

    return write_tsv(
        os.path.join(outdir, RUNTIME_OUTPUT),
        ["Sample"] + [STEP_LABELS.get(step, step) for step in steps],
        rows,
    )


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate MultiQC custom content TSVs for WGSpipeline.",
    )
    parser.add_argument("--outdir", required=True, help="custom content 输出目录")
    parser.add_argument(
        "--samples-tsv", default="samples.tsv", help="样本表（默认 samples.tsv）"
    )
    parser.add_argument(
        "--quast-dir", default="data/qc/quast", help="QUAST 输出目录"
    )
    parser.add_argument(
        "--memote-dir", default="models/memote", help="MEMOTE 输出目录"
    )
    parser.add_argument(
        "--carveme-dir", default="models/carveme", help="CarveMe 输出目录"
    )
    parser.add_argument(
        "--bench-dir", default="benchmarks", help="Snakemake benchmark 目录"
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)
    samples = read_sample_names(args.samples_tsv)
    if samples:
        print("[multiqc_extras] samples: {}".format(", ".join(samples)))
    else:
        warn("no samples found in {}, falling back to directory names".format(
            args.samples_tsv
        ))

    written = [
        write_quast_generalstats(args.quast_dir, samples, args.outdir),
        write_carveme_summary(args.carveme_dir, samples, args.outdir),
        write_memote_summary(args.memote_dir, samples, args.outdir),
        write_pipeline_runtime(args.bench_dir, samples, args.outdir),
    ]
    print(
        "[multiqc_extras] done: {}/{} custom content file(s) written to {}".format(
            sum(1 for flag in written if flag), len(written), args.outdir
        )
    )
    # 即使某个数据源缺失也返回 0：缺数据的段落不显示，不该让整条流水线失败。
    # / Always exit 0: a missing data source only means a missing section.
    return 0


if __name__ == "__main__":
    sys.exit(main())
