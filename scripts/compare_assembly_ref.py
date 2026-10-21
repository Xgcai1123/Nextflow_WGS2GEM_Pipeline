#!/usr/bin/env python3
"""Compare a draft assembly against a public reference genome (F30).

Why not QUAST's own reference mode? It has no hook for an external aligner: it
compiles its bundled minimap2 on first use, and on arm64 macOS that compile fails
(`unsupported option '-msse2'` / `'-mfpu='`), after which QUAST *silently* falls
back to reference-independent statistics. A reference comparison that can quietly
degrade to "no comparison" is exactly what this project refuses to ship, so the
comparison is done here with BLASTN instead -- BLAST+ already ships inside the
pinned Bakta environment, so no new environment is introduced.

Metrics mirror what QUAST/NUCMER-based reports give for this purpose:
  genome fraction     reference bases covered by accepted alignments / reference length
  identity            length-weighted mean identity over accepted alignments
  mismatches/indels   per 100 kbp of aligned assembly
  assembly aligned    assembly bases inside accepted alignments / assembly length
Thresholds are QUAST's defaults (>= 95 % identity, >= 65 bp alignment) so the
numbers are comparable with a standard QUAST/dnadiff run.

Exit codes: 0 success; 2 the reference matched nothing (wrong reference, or a
sample that is not the organism the reference belongs to) -- a defect in the
request, not a biological finding, so it must not pass silently.
"""
import argparse
import subprocess
import sys

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def fasta_lengths(path):
    """(total length, number of sequences) without loading the sequence data."""
    total = 0
    n = 0
    with open(path, "rb") as fh:
        for line in fh:
            if line.startswith(b">"):
                n += 1
            else:
                total += len(line.strip())
    return total, n


def merge_intervals(intervals):
    """Merge 1-based inclusive intervals; returns merged list + covered bases."""
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged, sum(e - s + 1 for s, e in merged)


def run_blastn(blastn, query, subject, threads, min_identity, min_length):
    outfmt = "6 qseqid qlen qstart qend sseqid slen sstart send pident length mismatch gapopen"
    cmd = [
        blastn, "-task", "megablast",
        "-query", query, "-subject", subject,
        "-outfmt", outfmt,
        "-evalue", "1e-10", "-max_target_seqs", "5",
        "-num_threads", str(threads),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write("blastn failed:\n%s\n" % proc.stderr)
        sys.exit(2)
    hits = []
    for line in proc.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 12:
            continue
        pident, alen = float(f[8]), int(f[9])
        if pident < min_identity or alen < min_length:
            continue
        sstart, send = int(f[6]), int(f[7])
        qstart, qend = int(f[2]), int(f[3])
        hits.append({
            "query": f[0], "qlen": int(f[1]),
            "q": (min(qstart, qend), max(qstart, qend)),
            "subject": f[4], "slen": int(f[5]),
            "s": (min(sstart, send), max(sstart, send)),
            "pident": pident, "alen": alen,
            "mismatch": int(f[10]), "gapopen": int(f[11]),
        })
    return hits


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--contigs", required=True, help="draft assembly FASTA")
    ap.add_argument("--reference", required=True, help="reference genome FASTA")
    ap.add_argument("--sample", required=True)
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--blastn", default="blastn", help="blastn executable")
    ap.add_argument("--min-identity", type=float, default=95.0)
    ap.add_argument("--min-length", type=int, default=65)
    ap.add_argument("--mqc-out", default=None)
    ap.add_argument("--tsv-out", default=None)
    args = ap.parse_args()

    hits = run_blastn(args.blastn, args.contigs, args.reference, args.threads,
                      args.min_identity, args.min_length)
    if not hits:
        sys.stderr.write(
            "compare_assembly_ref: no alignment passed the thresholds "
            "(identity >= %s %%, length >= %s bp) between %s and %s.\n"
            "  The reference does not match this assembly -- check the sample sheet:\n"
            "  a reference is optional, but a WRONG one is worse than none.\n"
            % (args.min_identity, args.min_length, args.contigs, args.reference))
        sys.exit(2)

    ref_len, ref_seq = fasta_lengths(args.reference)
    asm_len, asm_seq = fasta_lengths(args.contigs)

    # reference coverage, per reference sequence (a genome can be chromosome +
    # plasmids, and intervals from different sequences must not be merged)
    by_subject = {}
    for h in hits:
        by_subject.setdefault(h["subject"], []).append(h["s"])
    ref_covered = 0
    for ivs in by_subject.values():
        ref_covered += merge_intervals(ivs)[1]

    by_query = {}
    for h in hits:
        by_query.setdefault(h["query"], []).append(h["q"])
    asm_aligned = 0
    for ivs in by_query.values():
        asm_aligned += merge_intervals(ivs)[1]

    aligned_bases = sum(h["alen"] for h in hits)
    identity = sum(h["pident"] * h["alen"] for h in hits) / aligned_bases
    mismatches = sum(h["mismatch"] for h in hits)
    indels = sum(h["gapopen"] for h in hits)
    per_100k = 100000.0 / aligned_bases

    metrics = [
        ("genome_fraction", round(100.0 * ref_covered / ref_len, 2)),
        ("identity", round(identity, 3)),
        ("mismatches_per_100kbp", round(mismatches * per_100k, 2)),
        ("indels_per_100kbp", round(indels * per_100k, 2)),
        ("assembly_aligned", round(100.0 * asm_aligned / asm_len, 2)),
        ("aligned_contigs", round(100.0 * len(by_query) / asm_seq, 2)),
        ("reference_mb", round(ref_len / 1e6, 2)),
    ]

    # ---- plain, human-readable record -------------------------------------
    tsv_path = args.tsv_out or "%s/%s_refcomp.tsv" % (args.outdir, args.sample)
    with open(tsv_path, "w", encoding="utf-8") as fh:
        fh.write("metric\tvalue\n")
        fh.write("sample\t%s\n" % args.sample)
        fh.write("reference\t%s\n" % args.reference)
        fh.write("reference_length_bp\t%d\n" % ref_len)
        fh.write("reference_sequences\t%d\n" % ref_seq)
        fh.write("assembly_length_bp\t%d\n" % asm_len)
        fh.write("assembly_contigs\t%d\n" % asm_seq)
        fh.write("contigs_with_alignment\t%d\n" % len(by_query))
        fh.write("accepted_alignments\t%d\n" % len(hits))
        fh.write("min_identity_pct\t%s\n" % args.min_identity)
        fh.write("min_alignment_bp\t%s\n" % args.min_length)
        for key, val in metrics:
            fh.write("%s\t%s\n" % (key, val))

    # ---- MultiQC table (one row per sample; same section id => MultiQC merges
    #      the per-sample files into one table, which was verified explicitly) --
    mqc_path = args.mqc_out or "%s/%s_assembly_ref_mqc.tsv" % (args.outdir, args.sample)
    header = """# id: "assembly_ref"
# section_name: "Assembly vs reference genome"
# description: "The draft assembly aligned (BLASTN, megablast) against a public reference genome supplied in the sample sheet. Genome fraction is the share of the reference covered; identity is length-weighted over accepted alignments; mismatch and indel rates are per 100 kbp of aligned assembly. Thresholds are QUAST's defaults (>= 95 % identity, >= 65 bp). Unlike MEMOTE's score, these numbers say whether the assembly is the organism it claims to be."
# plot_type: "table"
# file_format: "tsv"
# pconfig:
#   id: "assembly_ref_table"
#   col1_header: "Sample"
# headers:
#   genome_fraction:
#     title: "Genome fraction (%)"
#     description: "Reference bases covered by accepted alignments, as a share of the reference length."
#     min: 0
#     max: 100
#     format: "{:,.2f}"
#   identity:
#     title: "Identity (%)"
#     description: "Length-weighted mean identity over accepted alignments."
#     min: 0
#     max: 100
#     format: "{:,.3f}"
#   mismatches_per_100kbp:
#     title: "Mismatches / 100 kbp"
#     description: "Substitutions per 100 kbp of aligned assembly."
#     min: 0
#     format: "{:,.2f}"
#   indels_per_100kbp:
#     title: "Indels / 100 kbp"
#     description: "Gap-opening events per 100 kbp of aligned assembly."
#     min: 0
#     format: "{:,.2f}"
#   assembly_aligned:
#     title: "Assembly aligned (%)"
#     description: "Share of the assembly's bases that fall inside accepted alignments."
#     min: 0
#     max: 100
#     format: "{:,.2f}"
#   aligned_contigs:
#     title: "Aligned contigs (%)"
#     description: "Share of contigs with at least one accepted alignment."
#     min: 0
#     max: 100
#     format: "{:,.2f}"
#   reference_mb:
#     title: "Reference (Mbp)"
#     description: "Length of the reference genome used."
#     min: 0
#     format: "{:,.2f}"
"""
    cols = [k for k, _ in metrics]
    with open(mqc_path, "w", encoding="utf-8") as fh:
        fh.write(header)
        fh.write("Sample\t" + "\t".join(cols) + "\n")
        fh.write(args.sample + "\t" + "\t".join(str(v) for _, v in metrics) + "\n")

    print("compare_assembly_ref: %s  genome fraction %.2f %%  identity %.3f %%  "
          "reference %.2f Mbp" % (args.sample, metrics[0][1], metrics[1][1], metrics[6][1]))


if __name__ == "__main__":
    main()
