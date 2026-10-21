#!/usr/bin/env python3
"""Normalise the rendered report's column headers, then drop duplicated units.

Two problems in one pass, because they are the same problem: what a column is called,
and where its unit lives.

1. HEADERS. MultiQC mixes conventions it does not let a config change: `% Dups`,
   `# contigs`, `reactions` (lowercase), `M Seqs`, and the same quantity under two
   names (`Length (Mbp)` in the QUAST table vs `Assembly Length (Mbp)` in General
   Statistics; `L50 (K)` vs `L50`). The canonical form is:

       Sample | Quantity name, qualifier (unit)

   i.e. the first column is always `Sample`; units are a trailing `(unit)` and never a
   leading `%`; counts carry no unit; one quantity keeps one name everywhere. Casing is
   left alone except where it was genuinely inconsistent (`reactions` next to
   `Reactions`).

2. CELLS. MultiQC writes the unit into the header AND into every cell
   (`N50 (Kbp) | 244.8 Kbp`). After step 1 the header is authoritative, so a cell that
   repeats it is stripped -- but only then: a unit the header does not carry is kept,
   because deleting it would lose information.

Both steps touch display text only: the cells' sortable `val` / `data-sort` attributes,
the plots, and CSV export inputs are untouched. Idempotent: a second run changes nothing.

Usage: normalize_report.py REPORT.html [MORE.html ...]
"""
import os
import re
import sys

TABLE_RE = re.compile(r"(<table\b[^>]*>)(.*?)(</table>)", re.S)
TABLE_ID_RE = re.compile(r'id="([^"]+)"')
THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.S)
TH_RE = re.compile(r"(<th\b[^>]*>)(.*?)(</th>)", re.S)
TBODY_RE = re.compile(r"(<tbody\b[^>]*>)(.*?)(</tbody>)", re.S)
TR_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)
CELL_RE = re.compile(r"(<t[hd]\b[^>]*>)(.*?)(</t[hd]>)", re.S)

# The visible value: <span class="val">244.8<span class='mqc_small_space'></span>Kbp</span>
VAL_RE = re.compile(
    r'(<span class="val">)([^<]*)(?:<span class=[\'"]mqc_small_space[\'"]></span>)?([^<]*)(</span>)'
)
UNIT_RE = re.compile(r"\s*([%A-Za-z]+)\s*$")

# ---------------------------------------------------------------------------
# The canonical header map. Keys are the header text AS RENDERED; values are the
# canonical name. Anything not listed is left alone unless a generic rule applies.
# ---------------------------------------------------------------------------
HEADER_MAP = {
    "table-general_stats_table": {
        "Sample Name": "Sample",
        "% Dups": "Duplicates (%)",
        "% GC": "Reads GC (%)",
        "Average Read Length": "Read length, mean (bp)",
        "Median Read Length": "Read length, median (bp)",
        "% Failed": "Modules failed (%)",
        "M Seqs": "Reads (M)",
        "N50 (Kbp)": "N50 (kbp)",
        "Assembly Length (Mbp)": "Assembly length (Mbp)",
        "L50": "L50 (contigs)",
        "# contigs": "Contigs",
        "GC (%)": "Assembly GC (%)",
        "reactions": "Reactions",
        "metabolites": "Metabolites",
        "MEMOTE score": "MEMOTE score (%)",
    },
    "table-quast_table": {
        "Sample Name": "Sample",
        "N50 (Kbp)": "N50 (kbp)",
        "Length (Mbp)": "Assembly length (Mbp)",
        "L50 (K)": "L50 (contigs)",
        "L50": "L50 (contigs)",
        "Largest contig (Kbp)": "Largest contig (kbp)",
    },
    "table-memote_summary_table": {
        "Total score": "Total score (%)",
        "Consistency": "Consistency (%)",
        "Annotation · metabolites": "Annotation · metabolites (%)",
        "Annotation · reactions": "Annotation · reactions (%)",
        "Annotation · genes": "Annotation · genes (%)",
        "Annotation · SBO": "Annotation · SBO (%)",
    },
    "table-fastqc_top_overrepresented_sequences_table": {
        "% of all reads": "Reads (%)",
    },
}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def _canonical(table_id: str, header: str) -> str:
    mapped = HEADER_MAP.get(table_id, {}).get(header)
    if mapped:
        return mapped
    if header == "Sample Name":
        return "Sample"
    m = re.match(r"^% (.+)$", header)
    if m:
        return "%s (%%)" % m.group(1).strip()
    m = re.match(r"^# (.+)$", header)
    if m:
        return m.group(1).strip()[:1].upper() + m.group(1).strip()[1:]
    return header


def _replace_last_text(inner: str, old: str, new: str):
    """Replace the last text node of `inner` whose collapsed text is `old`.

    A <th> is `<span class="mqc_table_tooltip" title="...">N50 (Kbp)</span>`: the
    label is a text node inside a span, and the same words also appear in the
    tooltip attribute, so only text that is NOT inside a tag is considered.
    """
    spans = []
    depth = 0
    start = None
    i = 0
    while i < len(inner):
        if inner[i] == "<":
            if start is not None:
                spans.append((start, i))
                start = None
            close = inner.find(">", i)
            i = len(inner) if close < 0 else close + 1
        else:
            if start is None:
                start = i
            i += 1
    if start is not None:
        spans.append((start, len(inner)))
    for begin, end in reversed(spans):
        if _text(inner[begin:end]) == old:
            return inner[:begin] + new + inner[end:]
    return None


def _normalise_headers(table_id: str, thead_inner: str):
    """Return (new_thead_inner, [canonical headers], renamed count)."""
    headers, renamed = [], 0

    def fix(m):
        nonlocal renamed
        open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
        current = _text(inner)
        canonical = _canonical(table_id, current)
        headers.append(canonical)
        if canonical == current:
            return m.group(0)
        replaced = _replace_last_text(inner, current, canonical)
        if replaced is None:
            return m.group(0)
        renamed += 1
        return open_tag + replaced + close_tag

    return TH_RE.sub(fix, thead_inner), headers, renamed


def _fix_cell(inner: str, header: str):
    m = VAL_RE.search(inner)
    if not m:
        return inner, 0
    number, unit = m.group(2), m.group(3)
    um = UNIT_RE.match(unit)
    if not um or um.group(1).lower() not in header.lower():
        return inner, 0
    return inner[: m.start()] + '<span class="val">%s</span>' % number + inner[m.end():], 1


def _normalise_table(table_html: str, table_id: str = ""):
    thead = THEAD_RE.search(table_html)
    if not thead:
        return table_html, 0, 0

    new_thead_inner, headers, renamed = _normalise_headers(table_id, thead.group(1))
    table_html = (table_html[: thead.start(1)] + new_thead_inner + table_html[thead.end(1):])

    stripped = 0

    def fix_row(row_match):
        nonlocal stripped
        row = row_match.group(0)
        pieces, pos = [], 0
        for idx, cell in enumerate(CELL_RE.finditer(row)):
            pieces.append(row[pos: cell.start()])
            if cell.group(1).startswith("<td"):
                inner, n = _fix_cell(cell.group(2), headers[idx] if idx < len(headers) else "")
                stripped += n
                pieces.append(cell.group(1) + inner + cell.group(3))
            else:
                pieces.append(cell.group(0))
            pos = cell.end()
        pieces.append(row[pos:])
        return "".join(pieces)

    tbody = TBODY_RE.search(table_html)
    if tbody:
        new_body = TR_RE.sub(fix_row, tbody.group(2))
        table_html = table_html[: tbody.start(2)] + new_body + table_html[tbody.end(2):]
    return table_html, renamed, stripped


def process(path: str) -> int:
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        doc = fh.read()

    totals = {"renamed": 0, "stripped": 0}

    def fix_table(m):
        tid_m = TABLE_ID_RE.search(m.group(1))      # the id lives on <table ...>, i.e. group(1)
        new, renamed, stripped = _normalise_table(m.group(2), tid_m.group(1) if tid_m else "")
        totals["renamed"] += renamed
        totals["stripped"] += stripped
        return m.group(1) + new + m.group(3)

    new_doc = TABLE_RE.sub(fix_table, doc)
    if totals["renamed"] or totals["stripped"]:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(new_doc)
        os.replace(tmp, path)
    print("normalize_report: %s -- %d header(s) normalised, %d cell unit(s) removed"
          % (path, totals["renamed"], totals["stripped"]))
    return 0


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    for path in argv[1:]:
        if not os.path.isfile(path):
            sys.stderr.write("normalize_report: no such file: %s\n" % path)
            return 2
        process(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
