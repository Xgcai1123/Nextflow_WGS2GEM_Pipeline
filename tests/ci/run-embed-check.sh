#!/usr/bin/env bash
# ============================================================
# Embedding scripts: MEMOTE (F21) and QUAST (F24) report sections.
#
# Why this check exists: every other check drives the pipeline with `-stub-run`,
# and in stub mode MULTIQC's `stub:` block REPLACES the real script body -- so
# `scripts/memote_html_section.py` and `scripts/quast_html_section.py` were
# never executed anywhere in CI. A regression in either (a malformed section
# header the YAML parser rejects, a dropped `html.escape`, a per-sample file
# where one aggregate file is required) would ship silently and only surface as
# a broken section inside a 16 MB report.
#
# So this check calls both scripts directly against tiny fixtures. It needs NO
# conda environment, NO Nextflow and NO network, and finishes in well under a
# second.
#
# What it pins:
#   * the section header each script emits (id / section_name / plot_type) --
#     MultiQC's `_find_html_file_header()` silently ignores a bad one, so a
#     typo here costs a missing section, not an error;
#   * ONE aggregate file for QUAST vs ONE file per sample for MEMOTE (D15);
#   * a lossless escaping round-trip: every `<iframe srcdoc>` must parse back
#     byte-identical to its source page. The fixtures deliberately contain
#     `&`, `<`, `>`, `"` and `'`;
#   * no leakage: a `<base href="/">` or a `<style>`/`<script>` from an embedded
#     page must never appear unescaped in the outer document;
#   * the missing-input branch is a warning, not a crash;
#   * `quast_full` still sorts between `quast` and `busco` in the overlay.
#
# Local usage:  bash tests/ci/run-embed-check.sh
# ============================================================

# shellcheck source=tests/ci/lib.sh
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

PYTHON="${PYTHON:-python3}"
if ! command -v "${PYTHON}" >/dev/null 2>&1; then
    echo "${PYTHON} is not on PATH -- this check runs the embedder scripts directly" >&2
    exit 2
fi

FIXTURES="${PROJECT_DIR}/tests/ci/fixtures"
WORK="${CI_OUT}/embed"
rm -rf "${WORK}"
mkdir -p "${WORK}"

# ---------------------------------------------------------------
# QUAST (F24): one aggregate file for the whole cohort
# ---------------------------------------------------------------
echo "== quast_html_section.py =="
"${PYTHON}" "${PROJECT_DIR}/scripts/quast_html_section.py" \
    --quast-dir "${FIXTURES}/quast" \
    --samples "CI_A CI_B CI_MISSING" \
    --outdir "${WORK}/quast_html" \
    --report-dir "${WORK}/report" >"${WORK}/quast.log" 2>&1
expect_eq "exits 0 with a sample that has no report" "0" "$?"
expect_file "wrote the aggregate section" "${WORK}/quast_html/quast_full_mqc.html"
expect_eq "exactly one file for the cohort, not one per sample (D15)" \
    "1" "$(ls "${WORK}/quast_html" | wc -l | tr -d ' ')"
expect_grep "both present samples embedded" "CI_B: embedded report.html" "${WORK}/quast.log"
expect_grep "sample without report.txt is still embedded" "CI_A: embedded report.html" "${WORK}/quast.log"
expect_grep "missing sample is a warning, not a failure" "skip CI_MISSING" "${WORK}/quast.log"
expect_grep "reports the aggregate size" "2 sample(s)" "${WORK}/quast.log"

# ---------------------------------------------------------------
# MEMOTE (F21): one file per sample
# ---------------------------------------------------------------
echo "== memote_html_section.py =="
"${PYTHON}" "${PROJECT_DIR}/scripts/memote_html_section.py" \
    --memote-dir "${FIXTURES}/memote" \
    --samples "CI_A CI_B CI_MISSING" \
    --outdir "${WORK}/memote_html" \
    --report-dir "${WORK}/report" >"${WORK}/memote.log" 2>&1
expect_eq "exits 0 with a sample that has no report" "0" "$?"
expect_file "wrote a section for CI_A" "${WORK}/memote_html/memote_full_CI_A_mqc.html"
expect_file "wrote a section for CI_B" "${WORK}/memote_html/memote_full_CI_B_mqc.html"
expect_eq "exactly one file per sample" "2" "$(ls "${WORK}/memote_html" | wc -l | tr -d ' ')"
expect_grep "missing sample is a warning, not a failure" "skip CI_MISSING" "${WORK}/memote.log"

# ---------------------------------------------------------------
# Edge case: nothing to embed at all -> no file, no crash
# ---------------------------------------------------------------
"${PYTHON}" "${PROJECT_DIR}/scripts/quast_html_section.py" \
    --quast-dir "${FIXTURES}/quast" \
    --samples "CI_MISSING" \
    --outdir "${WORK}/empty_html" >"${WORK}/empty.log" 2>&1
expect_eq "exits 0 when the cohort has no QUAST report" "0" "$?"
expect_absent "no empty section file is written" "${WORK}/empty_html/quast_full_mqc.html"
expect_grep "says so out loud" "WARNING: no QUAST report embedded" "${WORK}/empty.log"

# ---------------------------------------------------------------
# Structural assertions (parsing, not grepping: an attribute value can only be
# checked by a real parser, which is what a browser does too)
# ---------------------------------------------------------------
echo "== generated documents =="
ASSERT_LOG="${WORK}/assertions.tsv"
if ! "${PYTHON}" - "${WORK}" "${FIXTURES}" "${PROJECT_DIR}" \
        >"${ASSERT_LOG}" 2>"${WORK}/assertions.err" <<'PY'
import html.parser
import os
import re
import sys

work, fixtures, project = sys.argv[1], sys.argv[2], sys.argv[3]
out = []


def emit(ok, label):
    out.append(("ok" if ok else "FAIL") + "\t" + label)


class Frames(html.parser.HTMLParser):
    """Collect iframes. html.parser UNESCAPES attribute values, so `srcdoc`
    arrives exactly as the source page -- comparing it to the fixture file is
    therefore a lossless-escaping test."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.frames = []

    def handle_starttag(self, tag, attrs):
        if tag == "iframe":
            d = dict(attrs)
            self.frames.append((d.get("title"), d.get("srcdoc"), d.get("onload")))


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def header_id(doc):
    m = re.match(r"\s*<!--(.*?)-->", doc, re.S)
    if not m:
        return None, ""
    head = m.group(1)
    i = re.search(r'^id:\s*"([^"]+)"', head, re.M)
    p = re.search(r'^plot_type:\s*"([^"]+)"', head, re.M)
    return (i.group(1) if i else None), (p.group(1) if p else None)


def emit_file_match(actual, path, label):
    """Compare a parsed `srcdoc` with its source page.

    A mismatch is what a dropped `html.escape` looks like: the page's own `"`
    closes the attribute early, so the parser hands back a truncated value (and
    the iframe's `title` may be mangled with it -- hence the missing-file branch,
    which must FAIL rather than raise).
    """
    if not os.path.isfile(path):
        emit(False, "%s -- no source page to compare against (%s)" % (label, path))
        return
    emit(actual == read(path), label)


# ---------- QUAST aggregate ----------
# An absent file must degrade into failing assertions, not a traceback.
quast_path = os.path.join(work, "quast_html", "quast_full_mqc.html")
quast_doc = read(quast_path) if os.path.isfile(quast_path) else ""
qid, qplot = header_id(quast_doc)
emit(qid == "quast_full", "section header id is quast_full (got %r)" % qid)
emit(qplot == "html", "section header plot_type is html (got %r)" % qplot)
emit("section_name" in quast_doc, "section header carries section_name")

qf = Frames()
qf.feed(quast_doc)
emit(len(qf.frames) == 2, "2 QUAST iframes, one per embedded sample (got %d)" % len(qf.frames))
by_sample = {}
for title, srcdoc, onload in qf.frames:
    for sample in ("CI_A", "CI_B"):
        if (title or "").endswith(sample):
            by_sample[sample] = (srcdoc, onload)
for sample in ("CI_A", "CI_B"):
    if sample not in by_sample:
        emit(False, "no QUAST iframe carries this sample in its title (%s)" % sample)
        continue
    srcdoc, onload = by_sample[sample]
    emit_file_match(srcdoc, os.path.join(fixtures, "quast", sample, "report.html"),
                    "srcdoc round-trips byte-identical (%s)" % sample)
    emit(bool(onload) and "scrollHeight" in (onload or ""),
         "iframe self-sizes to its content (%s)" % sample)
    emit("try{" in (onload or "") and "catch" in (onload or ""),
         "self-sizing has a fallback guard (%s)" % sample)

# escaped, not live: the embedded page's CSS/JS and the text fixture
emit("&lt;style" in quast_doc and "<style" not in quast_doc,
     "embedded <style> is escaped, not live in the outer document")
emit("&lt;script" in quast_doc and "<script" not in quast_doc,
     "embedded <script> is escaped, not live in the outer document")
txt = read(os.path.join(fixtures, "quast", "CI_A", "report.txt"))
emit(html.escape(txt) in quast_doc, "report.txt is carried into the section")
emit("N50" in quast_doc and "50.75" in quast_doc, "report.txt content survives escaping")
emit('target="_blank"' in quast_doc, "standalone outputs are linked")
emit("../../" in quast_doc, "standalone links are relative to the report dir")

# ---------- MEMOTE per sample ----------
for sample in ("CI_A", "CI_B"):
    name = "memote_full_%s_mqc.html" % sample
    path = os.path.join(work, "memote_html", name)
    doc = read(path) if os.path.isfile(path) else ""
    mid, mplot = header_id(doc)
    emit(mid == "memote_full_%s" % sample, "%s header id is per-sample" % sample)
    emit(mplot == "html", "%s header plot_type is html" % sample)
    mf = Frames()
    mf.feed(doc)
    emit(len(mf.frames) == 1, "%s has exactly 1 iframe (got %d)" % (sample, len(mf.frames)))
    if mf.frames:
        emit_file_match(mf.frames[0][1], os.path.join(fixtures, "memote", sample, "index.html"),
                        "%s srcdoc round-trips byte-identical" % sample)
    emit('<base href="/">' not in doc,
         "%s: the page's <base href=\"/\"> never reaches the outer document" % sample)
    emit("&lt;base href=&quot;/&quot;&gt;" in doc,
         "%s: <base> is present but escaped" % sample)

# ---------- section ordering contract (F24 / D15) ----------
overlay = read(os.path.join(project, "multiqc_nextflow.yaml"))
orders = {}
for key, value in re.findall(r"^\s*([a-z_]+):\s*\{\s*order:\s*(-?\d+)\s*\}", overlay, re.M):
    orders[key] = int(value)
emit("quast_full" in orders, "overlay orders the quast_full anchor")
if "quast_full" in orders:
    emit(orders.get("quast", 0) > orders["quast_full"] > orders.get("busco", 0),
         "quast_full sorts between quast (%s) and busco (%s), i.e. in pipeline order"
         % (orders.get("quast"), orders.get("busco")))
    between = sorted([k for k, v in orders.items()
                      if orders.get("busco", 0) < v < orders.get("quast", 0)],
                     key=lambda k: -orders[k])
    # F30 added the reference-comparison table. It belongs with the summaries,
    # i.e. after the QUAST table and before the embedded QUAST page, so the
    # expected gap content is exactly these two anchors in this order -- anything
    # else there is a wiring mistake, not a preference.
    emit(between == ["assembly_ref", "quast_full"],
         "the gap holds exactly the reference table then the embedded QUAST page, in that order (%s)"
         % between)
    if "assembly_ref" in orders:
        emit(orders["quast"] > orders["assembly_ref"] > orders["quast_full"],
             "reference comparison sorts after the QUAST table and before the embedded page")

for line in out:
    print(line)
PY
then
    fail "the assertion program crashed (see ${WORK}/assertions.err)"
    sed -n '1,8p' "${WORK}/assertions.err" | sed 's/^/       /'
fi

ASSERTIONS=0
while IFS=$'\t' read -r verdict label; do
    [ -n "${verdict}" ] || continue
    ASSERTIONS=$((ASSERTIONS + 1))
    case "${verdict}" in
        ok) pass "${label}" ;;
        *)  fail "${label}" ;;
    esac
done <"${ASSERT_LOG}"

# A crashed or silently truncated assertion program must not look like success.
if [ "${ASSERTIONS}" -lt 20 ]; then
    fail "expected at least 20 structural assertions, got ${ASSERTIONS}"
else
    pass "ran the full structural assertion set (${ASSERTIONS} assertions)"
fi

# ---------------------------------------------------------------------------
# scripts/strip_units.py -- the report's cosmetic post-process (F35).
#
# MultiQC writes the unit into the header AND into every cell; the pipeline strips
# the duplicate from the visible text after multiqc runs. That step is tool-free,
# so it is checked here directly rather than through a stub run (which never
# executes MULTIQC's script body).
# ---------------------------------------------------------------------------
STRIP_WORK="$(mktemp -d)"
cat > "${STRIP_WORK}/r.html" <<'HTML'
<table><thead><tr><th>Sample Name</th><th>N50 (Kbp)</th><th>Read length</th></tr></thead>
<tbody><tr>
<td val="S1">S1</td>
<td val="244.803" data-sort="244.803"><span class="val">244.8<span class='mqc_small_space'></span>Kbp</span></td>
<td val="241"><span class="val">241<span class='mqc_small_space'></span>bp</span></td>
</tr></tbody></table>
HTML
"${PYTHON}" "${PROJECT_DIR}/scripts/strip_units.py" "${STRIP_WORK}/r.html" >"${STRIP_WORK}/log1" 2>&1
expect_eq "strip_units exits 0" "0" "$?"
expect_grep "strip_units removed the header-duplicated unit" "1 cell unit" "${STRIP_WORK}/log1"
grep -q '244\.8<span class=.mqc_small_space.></span>Kbp' "${STRIP_WORK}/r.html" \
    && fail "the redundant 'Kbp' is still in the N50 cell" || pass "the redundant 'Kbp' left the cell"
grep -q '241<span class=.mqc_small_space.></span>bp' "${STRIP_WORK}/r.html" \
    && pass "a unit the header does NOT carry is kept (Read length | 241 bp)" \
    || fail "strip_units removed a unit the header does not carry"
grep -q 'val="244\.803"' "${STRIP_WORK}/r.html" \
    && pass "the sortable val= attribute is untouched" || fail "the val= attribute was rewritten"
"${PYTHON}" "${PROJECT_DIR}/scripts/strip_units.py" "${STRIP_WORK}/r.html" >"${STRIP_WORK}/log2" 2>&1
expect_grep "strip_units is idempotent (second run is a no-op)" "0 cell unit" "${STRIP_WORK}/log2"
rm -rf "${STRIP_WORK}"

summary "run-embed-check"
