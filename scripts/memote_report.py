#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MEMOTE 报告生成器：一次运行，产出 HTML + 全部机读结果 + 评分摘要 TSV。

/ MEMOTE report generator: one run, three outputs (HTML + full machine-readable
results + score summary TSV).

为什么不用 `memote report snapshot`？

  `memote report snapshot --filename index.html <model>` 只产出 HTML，拿不到机读结果。
  想拿机读结果只能再跑一次 `memote run --filename result.json.gz`，而 MEMOTE 的整套
  测试包含大量 FBA 求解，跑两遍的时间成本不可接受。

本脚本直接调用 memote 的 Python API，走的是与 `memote report snapshot` **完全相同**
的代码路径：

    validate_model()  ->  test_model(results=True)  ->  SnapshotReport().render_html()

因此 index.html 与原来由 CLI 生成的一模一样，同时顺带拿到带 score 的结果字典，
再写成 memote_result.json（完整结果）和 memote_summary.tsv（喂 MultiQC）。

/ Why not `memote report snapshot`? It only emits HTML. Getting machine-readable
results would require a second `memote run` -- but MEMOTE's suite runs a lot of FBA,
so running it twice is wasteful. This script calls memote's own Python API, which is
the exact code path used by `memote report snapshot`, so the HTML is unchanged while
we also get the scored result dict for free.

用法 / Usage:

    python3 scripts/memote_report.py MODEL.xml --outdir OUTDIR --sample SAMPLE \\
        [--solver glpk] [--solver-timeout 10] [--pytest-args "--tb no"]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

# memote_result.json 里 score.sections 的取值参考：
# 只有 cards.scored.sections 下的 section 才有分数（memote 默认是
# consistency / annotation_met / annotation_rxn / annotation_gene / annotation_sbo）。
# 这里不写死 section 名，直接遍历结果里出现的所有 section，
# 这样 memote 升级后新增/改名 section 也不用手改脚本。
# / Section ids are taken from the result itself (not hard-coded), so memote
# upgrades that add or rename scored sections need no change here.
TOTAL_SCORE_ID = "Total score"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the MEMOTE test suite once and write HTML + JSON + TSV.",
    )
    parser.add_argument("model", help="SBML 代谢模型路径 / path to the SBML model")
    parser.add_argument(
        "--outdir",
        required=True,
        help="输出目录（不存在会自动创建）/ output directory (created if missing)",
    )
    parser.add_argument(
        "--sample",
        required=True,
        help="样本名，写进摘要 TSV 的第一列 / sample name for the summary TSV",
    )
    parser.add_argument(
        "--solver",
        default="glpk",
        help="cobra 求解器名（memote 环境自带 glpk）/ cobra solver name (default: glpk)",
    )
    parser.add_argument(
        "--solver-timeout",
        type=int,
        default=10,
        help="求解器超时（秒）/ solver timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--pytest-args",
        default="--tb no",
        help='透传给 pytest 的参数 / extra pytest args (default: "--tb no")',
    )
    return parser


def percent(value) -> str:
    """把 0..1 的分数格式化成 0..100 的百分比字符串。

    / Format a 0..1 score as a 0..100 percentage string.
    """
    if value is None:
        return ""
    try:
        return "{:.1f}".format(float(value) * 100.0)
    except (TypeError, ValueError):
        return ""


def extract_scores(payload: dict) -> "tuple[str, list[tuple[str, str]]]":
    """从（已算分的）结果字典里取出总评分与各 section 评分。

    / Pull the total score and the per-section scores out of the scored result.
    """
    score = payload.get("score") or {}
    total = percent(score.get("total_score"))
    sections: "list[tuple[str, str]]" = []
    for entry in score.get("sections") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("section")
        if not name:
            continue
        sections.append((str(name), percent(entry.get("score"))))
    return total, sections


def write_summary_tsv(path: str, sample: str, payload: dict) -> None:
    """写 MultiQC 用的评分摘要：Sample / Total score / 各 section。

    / Write the score summary consumed by MultiQC's custom content module.
    """
    total, sections = extract_scores(payload)
    header = ["Sample", TOTAL_SCORE_ID] + [name for name, _ in sections]
    row = [sample, total] + [value for _, value in sections]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        writer.writerow(row)
    print(
        "[memote_report] summary: total={}% over {} section(s)".format(
            total or "n/a", len(sections)
        )
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # 延迟 import：让 --help 在没装 memote 的环境里也能用。
    # / Import lazily so --help works even outside the memote env.
    import cobra
    from memote.suite.api import test_model, validate_model
    from memote.suite.reporting import ReportConfiguration, SnapshotReport

    cobra.Configuration().solver = args.solver

    os.makedirs(args.outdir, exist_ok=True)

    model, sbml_version, notifications = validate_model(args.model)
    if model is None:
        print(
            "[memote_report] FATAL: 模型无法载入 / model could not be loaded: "
            "{}".format(args.model),
            file=sys.stderr,
        )
        for level in ("errors", "warnings"):
            for message in (notifications or {}).get(level, []):
                print("[memote_report]   {}: {}".format(level, message), file=sys.stderr)
        return 1

    # 与 `memote report snapshot` 一致：pytest 默认 --tb no，不因个别测试失败而死掉。
    # / Same as `memote report snapshot`: individual test failures must not abort.
    code, results = test_model(
        model,
        sbml_version=sbml_version,
        results=True,
        pytest_args=args.pytest_args.split(),
        solver_timeout=args.solver_timeout,
    )
    if code != 0:
        print(
            "[memote_report] note: test suite exit code = {} "
            "(个别测试未通过属正常，报告仍会生成) / some tests failed; "
            "the report is still generated.".format(code)
        )

    # SnapshotReport 在 __init__ 里就会 compute_score()，所以 render_html /
    # render_json 拿到的都是「已经带 score」的结果。
    # / SnapshotReport computes the score in __init__, so both renderers see it.
    report = SnapshotReport(result=results, configuration=ReportConfiguration.load())

    html_path = os.path.join(args.outdir, "index.html")
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(report.render_html())

    rendered = report.render_json()
    payload = json.loads(rendered) if isinstance(rendered, str) else rendered

    json_path = os.path.join(args.outdir, "memote_result.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")

    write_summary_tsv(
        os.path.join(args.outdir, "memote_summary.tsv"), args.sample, payload
    )

    print(
        "[memote_report] {}: wrote index.html / memote_result.json / "
        "memote_summary.tsv into {}".format(args.sample, args.outdir)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
