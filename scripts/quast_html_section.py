#!/usr/bin/env python3
"""Merge QUAST's own output files into the MultiQC report as one section.

QUAST 5.2.0 writes three files per sample:

  * `report.tsv` -- already consumed by MultiQC's built-in `quast` module (the
    assembly QC table) and by `multiqc_extras.py` (`quast_generalstats`);
  * `report.txt` -- the plain-text summary a reader actually quotes;
  * `report.html` -- the full interactive report (~380 KB), a *complete*
    self-contained document: all CSS and 22 scripts are inline and it
    references no sibling asset (verified: no `<base>` tag, no local
    `src`/`href`). Only the upstream credit link points outside.

This helper adds the latter two to the report:

  * ONE file is written, `quast_full_mqc.html`, holding every sample -- one
    section, not one section per sample. Reason: MultiQC 1.21 matches
    `report_section_order` anchors EXACTLY (no globs; multiqc.py:895), and each
    `_mqc.html` with a unique `id:` becomes its own top-level module. A
    per-sample id could therefore never be ordered next to the QUAST tables. A
    single fixed anchor (`quast_full`) is ordered explicitly in
    multiqc_nextflow.yaml, which keeps every section in pipeline order.
  * `report.html` goes into `<iframe srcdoc="...">`: inlining a complete
    document would leak its CSS/scripts into the MultiQC page and corrupt both.
    An iframe is a separate document; `srcdoc` keeps the report fully
    self-contained (renders over `file://` and HTTP alike). The frame sizes
    itself to its content on load, with a fixed height as fallback.
  * `report.txt` goes into a collapsed `<details><pre>` block.

Trade-off: `srcdoc` inflates the payload (~x1.2 after escaping), so the MultiQC
report grows by roughly the total size of the embedded QUAST pages.

Usage:
  quast_html_section.py --quast-dir <abs>/data/qc/quast --samples "S1 S2" \
      --outdir quast_html --report-dir <abs>/reports/multiqc
"""
from __future__ import annotations

import argparse
import html
import os
import sys
from typing import List

SECTION_ID = "quast_full"
SECTION_NAME = "QUAST: full assembly reports"

IFRAME_STYLE = (
    "width:100%;height:1400px;border:1px solid #d0d7de;"
    "border-radius:6px;background:#ffffff;"
)
# A srcdoc frame is same-origin with its parent, so it can measure its own
# document and grow to fit -- no nested scrollbar inside a report page. Wrapped
# in try/catch: a browser that treats the frame as an opaque origin (observed
# for file:// in some builds) must fall back to IFRAME_STYLE, not break the page.
IFRAME_ONLOAD = (
    "try{this.style.height="
    "(this.contentDocument.documentElement.scrollHeight+40)+'px'}catch(e){}"
)

H4_STYLE = "margin:18px 0 4px 0;font-size:15px;"
NOTE_STYLE = "margin:0 0 6px 0;font-size:12px;color:#57606a;"
DETAILS_STYLE = "margin:0 0 8px 0;font-size:12px;"
PRE_STYLE = (
    "margin:4px 0 0 0;padding:8px;background:#f6f8fa;border:1px solid #d0d7de;"
    "border-radius:6px;font-size:11px;line-height:1.45;white-space:pre-wrap;"
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _standalone_href(path: str, report_dir: str) -> str:
    """Path to a QUAST output as seen from the published report directory."""
    if report_dir:
        return os.path.relpath(os.path.abspath(path), os.path.abspath(report_dir))
    return os.path.abspath(path)


def build_sample_block(sample: str, quast_sample_dir: str, report_dir: str) -> str:
    """Return the report block for one sample (heading + links + text + frame)."""
    html_path = os.path.join(quast_sample_dir, "report.html")
    txt_path = os.path.join(quast_sample_dir, "report.txt")
    tsv_path = os.path.join(quast_sample_dir, "report.tsv")

    parts = ['<a id="%s_%s"></a>' % (SECTION_ID, sample)]
    parts.append('<h4 style="%s">%s</h4>' % (H4_STYLE, html.escape(sample)))

    links = []
    for label, path in (("report.html", html_path),
                        ("report.txt", txt_path),
                        ("report.tsv", tsv_path)):
        if os.path.isfile(path):
            links.append(
                '<a href="%s" target="_blank" rel="noopener">%s</a>'
                % (html.escape(_standalone_href(path, report_dir), quote=True), label)
            )
    if links:
        parts.append(
            '<p style="%s">Standalone QUAST output for %s: %s</p>'
            % (NOTE_STYLE, html.escape(sample), " &middot; ".join(links))
        )

    if os.path.isfile(txt_path):
        parts.append(
            '<details style="%s"><summary>QUAST summary (report.txt)</summary>'
            "<pre style=\"%s\">%s</pre></details>"
            % (DETAILS_STYLE, PRE_STYLE, html.escape(_read(txt_path)))
        )

    if os.path.isfile(html_path):
        page = _read(html_path)
        # & and " are mandatory (attribute delimiter); < and > are escaped too so
        # the attribute value can never terminate the tag early.
        srcdoc = html.escape(page, quote=True)
        parts.append(
            '<iframe srcdoc="%s" style="%s" onload="%s" '
            'title="QUAST full report %s"></iframe>'
            % (srcdoc, IFRAME_STYLE, IFRAME_ONLOAD, html.escape(sample, quote=True))
        )
    else:
        parts.append(
            '<p style="%s">No QUAST report.html found for this sample.</p>'
            % NOTE_STYLE
        )

    return "\n".join(parts) + "\n"


def build_document(blocks: List[str], samples: List[str], section_id: str,
                   section_name: str) -> str:
    """Wrap the per-sample blocks in the MultiQC custom-content HTML header."""
    comment = "\n".join([
        "<!--",
        'id: "%s"' % section_id,
        'section_name: "%s"' % section_name,
        'description: "Complete QUAST assembly reports for %s, embedded inline '
        '(rendered in an isolated frame). Assembly statistics are summarised in '
        'the QUAST table above."' % ", ".join(samples),
        'plot_type: "html"',
        "-->",
        "",
    ])
    return comment + "\n".join(blocks)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Emit a MultiQC-embeddable section around QUAST reports."
    )
    parser.add_argument("--quast-dir", required=True,
                        help="Directory holding <sample>/report.{html,txt,tsv}.")
    parser.add_argument("--samples", required=True,
                        help="Space-separated sample names (report cohort).")
    parser.add_argument("--outdir", required=True,
                        help="Directory to write quast_full_mqc.html into.")
    parser.add_argument("--report-dir", default=None,
                        help="MultiQC report directory, used to build relative "
                             "links back to the standalone QUAST outputs.")
    parser.add_argument("--section-id", default=SECTION_ID,
                        help="MultiQC section anchor (default: %s)." % SECTION_ID)
    parser.add_argument("--section-name", default=SECTION_NAME,
                        help="Section title shown in the report.")
    args = parser.parse_args(argv)

    samples = args.samples.split()
    quast_dir = os.path.abspath(args.quast_dir)
    os.makedirs(args.outdir, exist_ok=True)

    blocks, embedded = [], []
    for sample in samples:
        sample_dir = os.path.join(quast_dir, sample)
        if not os.path.isfile(os.path.join(sample_dir, "report.html")):
            print("[quast_html] skip %s: no QUAST report at %s/report.html"
                  % (sample, sample_dir), file=sys.stderr)
            continue
        blocks.append(build_sample_block(sample, sample_dir, args.report_dir))
        embedded.append(sample)
        print("[quast_html] %s: embedded report.html (%d B)"
              % (sample, os.path.getsize(os.path.join(sample_dir, "report.html"))))

    if not embedded:
        print("[quast_html] WARNING: no QUAST report embedded", file=sys.stderr)
        return 0

    out = os.path.join(args.outdir, "%s_mqc.html" % args.section_id)
    document = build_document(blocks, embedded, args.section_id, args.section_name)
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(document)
    print("[quast_html] wrote %s (%d B, %d sample(s))"
          % (out, len(document), len(embedded)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
