#!/usr/bin/env python3
"""Strip a unit from a rendered table cell when the column header already carries it.

Why this exists: MultiQC bakes the unit into BOTH the column header and every cell,
so a table reads `N50 (Kbp) | 244.8 Kbp`, `% Dups | 12.9 %`. The header is the right
place for the unit; repeating it in every row is noise, and it is the kind of thing a
reader notices before they notice the numbers.

It cannot be fixed in configuration. MultiQC 1.21 builds module columns with a single
`suffix` used for the title *and* the cell (QUAST: `title = f"N50 ({suffix})"`,
`"suffix": suffix`), so setting the suffix to "" gives `N50 ()`. FastQC exposes no
suffix hook at all. The alternatives were worse: forking the modules, or replacing
their tables with our own and losing their plots and general-statistics columns.

So the report is post-processed, narrowly:
  * only inside <table>, only inside a cell's <span class="val"> (the visible text);
  * only when the SAME column's <th> already contains that unit;
  * the raw value lives in the cell's `val` attribute and in `data-sort`, which are
    never touched -- sorting, filtering, CSV export and the plots are unaffected;
  * idempotent: a second run finds nothing to do.

Usage: strip_units.py REPORT.html [REPORT2.html ...]
Exit 0 on success; 2 if a file is missing.
"""
import os
import re
import sys

TABLE_RE = re.compile(r"(<table\b[^>]*>)(.*?)(</table>)", re.S)
THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.S)
TH_RE = re.compile(r"<th\b[^>]*>(.*?)</th>", re.S)
TBODY_RE = re.compile(r"(<tbody\b[^>]*>)(.*?)(</tbody>)", re.S)
TR_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)
CELL_RE = re.compile(r"(<t[hd]\b[^>]*>)(.*?)(</t[hd]>)", re.S)

# The visible value: <span class="val">244.8<span class='mqc_small_space'></span>Kbp</span>
VAL_RE = re.compile(
    r'(<span class="val">)([^<]*)(?:<span class=[\'"]mqc_small_space[\'"]></span>)?([^<]*)(</span>)'
)
# A unit token is the trailing alphabetic/percent run of the unit text.
UNIT_RE = re.compile(r"\s*([%A-Za-z]+)\s*$")


def _text(fragment: str) -> str:
    """Visible text of an HTML fragment, whitespace-collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def _fix_cell(inner: str, header: str):
    """Return (new_inner, 1) if a redundant unit was removed, else (inner, 0)."""
    m = VAL_RE.search(inner)
    if not m:
        return inner, 0
    number, unit = m.group(2), m.group(3)   # (open, number, unit, close)
    um = UNIT_RE.match(unit)
    if not um:
        return inner, 0
    token = um.group(1)
    if token.lower() not in header.lower():
        # The header does not carry this unit, so the cell is the only place it
        # appears -- removing it would destroy information.
        return inner, 0
    replacement = '<span class="val">%s</span>' % number
    return inner[: m.start()] + replacement + inner[m.end():], 1


def _fix_table(table_html: str):
    """Return (new_table_html, cells_changed)."""
    thead = THEAD_RE.search(table_html)
    if not thead:
        return table_html, 0
    headers = [_text(c) for c in TH_RE.findall(thead.group(1))]
    if not headers:
        return table_html, 0

    changed = 0

    def fix_row(row_match):
        nonlocal changed
        row = row_match.group(0)
        pieces = []
        pos = 0
        # Count <th> and <td> together: several MultiQC tables use <th> for the
        # row label (the sample name), so counting only <td> shifts every column
        # by one and the unit check lands on the wrong header.
        for idx, cell in enumerate(CELL_RE.finditer(row)):
            pieces.append(row[pos: cell.start()])
            if cell.group(1).startswith("<td"):
                inner, n = _fix_cell(cell.group(2), headers[idx] if idx < len(headers) else "")
                changed += n
                pieces.append(cell.group(1) + inner + cell.group(3))
            else:
                pieces.append(cell.group(0))
            pos = cell.end()
        pieces.append(row[pos:])
        return "".join(pieces)

    tbody = TBODY_RE.search(table_html)
    if tbody:
        new_body = TR_RE.sub(fix_row, tbody.group(2))
        table_html = (table_html[: tbody.start(2)] + new_body + table_html[tbody.end(2):])
    return table_html, changed


def process(path: str) -> int:
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        doc = fh.read()

    total = 0

    def fix_table(m):
        nonlocal total
        new, n = _fix_table(m.group(2))
        total += n
        return m.group(1) + new + m.group(3)

    new_doc = TABLE_RE.sub(fix_table, doc)
    if total:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(new_doc)
        os.replace(tmp, path)
    print("strip_units: %s -- %d cell unit(s) removed" % (path, total))
    return 0


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    for path in argv[1:]:
        if not os.path.isfile(path):
            sys.stderr.write("strip_units: no such file: %s\n" % path)
            return 2
        process(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
