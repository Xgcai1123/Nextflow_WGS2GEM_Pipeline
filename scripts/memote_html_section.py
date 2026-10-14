#!/usr/bin/env python3
"""Wrap each sample's MEMOTE snapshot HTML so MultiQC can embed it in the report.

MEMOTE writes a self-contained Angular snapshot report (`index.html`, ~4 MB) per
sample. MultiQC's `custom_content` module can inject an HTML snippet into the
report (`plot_type: html`), but the MEMOTE page is a *complete* HTML document
with its own `<style>`/`<script>` and a `<base href="/">` tag -- inlining it
directly would leak its CSS/JS into the MultiQC page and corrupt both.

This helper therefore emits a small `memote_full_<sample>.html` file per sample:

  * a leading HTML comment carrying the MultiQC section config (id / section_name
    / description) -- MultiQC reads it via `_find_html_file_header()`;
  * an `<iframe srcdoc="...">` whose `srcdoc` holds the HTML-escaped MEMOTE page.

An iframe is a separate document, so the MEMOTE app keeps its own CSS/JS/base and
cannot disturb the MultiQC report. `srcdoc` (rather than `src`) keeps the report
fully self-contained: no sibling files have to travel with it, and it renders
regardless of whether the report is opened over `file://` or HTTP.

Trade-off: `srcdoc` inflates the payload (~x1.2 after escaping), so the MultiQC
report grows by roughly the size of the embedded MEMOTE pages.

Usage:
  memote_html_section.py --memote-dir <abs>/models/memote --samples "S1 S2" \
      --outdir memote_html --report-dir <abs>/reports/multiqc
"""
from __future__ import annotations

import argparse
import html
import os
import sys
from typing import List

IFRAME_STYLE = (
    "width:100%;height:1200px;border:1px solid #d0d7de;"
    "border-radius:6px;background:#ffffff;"
)


def build_section(sample: str, page: str, standalone_href: str) -> str:
    """Return the MultiQC custom-content HTML for one sample."""
    # Escape the whole document so it can live inside the srcdoc attribute.
    # & and " are mandatory (attribute delimiter); < and > are escaped too so the
    # attribute value can never terminate the tag early.
    srcdoc = html.escape(page, quote=True)

    comment = "\n".join([
        "<!--",
        'id: "memote_full_%s"' % sample,
        'section_name: "MEMOTE: full report \\u2014 %s"' % sample,
        'description: "Complete MEMOTE snapshot report for %s, embedded inline '
        '(rendered in an isolated frame). Scores are summarised in the MEMOTE '
        'table above."' % sample,
        'plot_type: "html"',
        "-->",
        "",
    ])

    link = (
        '<p style="margin:0 0 6px 0;font-size:12px;">'
        '<a href="%s" target="_blank" rel="noopener">'
        "Open the standalone MEMOTE report for %s</a></p>\n"
    ) % (standalone_href, sample)

    frame = (
        '<iframe srcdoc="%s" style="%s" title="MEMOTE full report %s"></iframe>\n'
    ) % (srcdoc, IFRAME_STYLE, sample)

    return comment + link + frame


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Emit MultiQC-embeddable wrappers around MEMOTE snapshot HTML."
    )
    parser.add_argument("--memote-dir", required=True,
                        help="Directory holding <sample>/index.html MEMOTE reports.")
    parser.add_argument("--samples", required=True,
                        help="Space-separated sample names (report cohort).")
    parser.add_argument("--outdir", required=True,
                        help="Directory to write memote_full_<sample>.html into.")
    parser.add_argument("--report-dir", default=None,
                        help="MultiQC report directory, used to build a relative "
                             "link back to the standalone MEMOTE report.")
    args = parser.parse_args(argv)

    samples = args.samples.split()
    memote_dir = os.path.abspath(args.memote_dir)
    os.makedirs(args.outdir, exist_ok=True)

    written = 0
    for sample in samples:
        path = os.path.join(memote_dir, sample, "index.html")
        if not os.path.isfile(path):
            print("[memote_html] skip %s: no MEMOTE report at %s" % (sample, path),
                  file=sys.stderr)
            continue

        if args.report_dir:
            standalone_href = os.path.relpath(
                path, os.path.abspath(args.report_dir)
            )
        else:
            standalone_href = path

        with open(path, encoding="utf-8", errors="replace") as handle:
            page = handle.read()

        content = build_section(sample, page, standalone_href)
        out = os.path.join(args.outdir, "memote_full_%s_mqc.html" % sample)
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(content)
        written += 1
        print("[memote_html] %s: embedded %d B -> %s (%d B)"
              % (sample, os.path.getsize(path), out, len(content)))

    if written == 0:
        print("[memote_html] WARNING: no MEMOTE report embedded", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
