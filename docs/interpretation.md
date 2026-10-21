# Reading the results

A field guide to `results/report/multiqc_report.html`: what every table, column and number means,
where it comes from, when it indicates a problem — and what it does **not** license you to
conclude.

Written from a reviewer's point of view. §2 answers "what is this column?"; §3 answers the
questions people actually ask when they look at the report and something looks off.

Reference points throughout are this project's two validation isolates:

| | SRR1770413 | SRR10194530 |
|---|---|---|
| organism / strain | *E. coli* K-12 | *E. coli* O157:H7 |
| reference used | GCA_000005845.2 (MG1655, 4.64 Mbp) | GCA_000008865.2 (Sakai, 5.59 Mbp) |
| reads | 2 × ~0.6 M, 301 bp | 2 × ~0.6 M, 241 bp |
| assembly | 370 contigs, 4.84 Mbp, N50 159 kbp | 390 contigs, 5.40 Mbp, N50 245 kbp |

---

> **Looking for "why this tool / why this parameter"?** This file is organised by
> *report table*: it tells you how to read each number. The *decisions* behind them —
> which parameters are inherited from the original pipeline, which are tool defaults,
> and which are conventions with no evidence — are in **`docs/justification.md`**.

## 1. A successful run, at a glance

Exit code 0, `results/report/multiqc_report.html` written, and per sample roughly:

| signal | healthy | this project |
|---|---|---|
| BUSCO complete | >= 95 % | 100 % (440/440) |
| genome fraction (with reference) | >= 95 % | 99.4 % / 97.4 % |
| growth on the declared medium | 0.4–1.5 h⁻¹ | 0.95 h⁻¹ |
| biomass yield | 0.3–0.7 g/g glucose | 0.53 g/g |
| MEMOTE total | 60–75 for a draft | 68.1 / 67.8 |

If one of these is off, §5 tells you where to look. If they are all in range, the run is
*plausible* — not *validated*. §4 explains the difference.

---

## 2. Field guide: every table in the report

**Header conventions.** Every column follows one shape — `Quantity, qualifier (unit)` — and the
first column is always `Sample`. Units appear once, in the header; cells carry the bare number.
Counts carry no unit, and `%` is written `(%)` rather than as a prefix.

| rule | before (MultiQC's mix) | now |
|---|---|---|
| first column | `Sample Name` | `Sample` |
| percent | `% Dups`, `% Failed` | `Duplicates (%)`, `Modules failed (%)` |
| unit suffix | `N50 (Kbp)` + cell `244.8 Kbp` | `N50 (kbp)` + cell `244.8` |
| unit casing | `Kbp` | `kbp`, `Mbp` (SI) |
| one quantity, one name | `Length (Mbp)` vs `Assembly Length (Mbp)` | `Assembly length (Mbp)` |
| counts have no unit | `L50`, `L50 (K)`, `# contigs` | `L50 (contigs)`, `Contigs` |
| two GC columns | `% GC` vs `GC (%)` | `Reads GC (%)` vs `Assembly GC (%)` |
| casing | `reactions`, `metabolites` | `Reactions`, `Metabolites` |

`scripts/normalize_report.py` enforces this on the rendered report, so the reused MultiQC config
stays byte-identical (D4): it rewrites the visible header text, then removes a unit from a cell
only when that (corrected) header already carries it. The cells' sortable `val`/`data-sort`
attributes, the plots and the CSV export inputs are never touched, and the step is idempotent.

The report has **thirteen** sections in pipeline order: ten of our own plus three embedded
snapshots (QUAST's full report and one full MEMOTE page per sample). Two of our tables are
*aggregate* — they mix per-file and per-sample metrics — and that is the source of most
confusion, so it is explained first.

### 2.1 General Statistics (the table at the top)

Fifteen columns, and **five rows per isolate**. The rows exist because the metrics have different
granularity:

| row | comes from | populated columns |
|---|---|---|
| `SRR1770413` | QUAST + CarveMe + MEMOTE | N50, Assembly Length, L50, # contigs, GC (%), reactions, metabolites, MEMOTE score |
| `SRR1770413_1`, `_2` | FastQC on the **raw** read files | % Dups, % GC, read length, % Failed, M Seqs |
| `SRR1770413_R1_paired`, `_R2_paired` | FastQC on the **trimmed** read files | same five |

So a cell is blank when that metric does not exist at that granularity — not because it failed.
FastQC is per *file* (four files per sample); assembly and model are per *sample*.

| column | meaning | unit / basis | K-12 | O157:H7 |
|---|---|---|---|---|
| `Duplicates (%)` | share of reads that are PCR/optical duplicates | % of reads in that file | 15.0 / 14.0 raw → 14.6 / 14.1 trimmed | 12.9 / 11.6 → 12.2 / 11.8 |
| `Reads GC (%)` | GC content of the **reads** | % of bases | 50 / 49 | 50 / 50 |
| `Read length, mean` / `Read length, median` | read length before/after trimming | bp | 301 → 267 / 200 | 241 → 229 / 210 |
| `Modules failed (%)` | share of FastQC *modules* that failed (not reads) | % of ~12 modules | 20 / 10 raw → 0 / 0 trimmed | 10 → 0 |
| `Reads (M)` | number of reads in millions | 10⁶ reads | 0.6 → 0.5 | 0.6 → 0.5 |
| `N50 (kbp)` | contig length at the 50 % point of the assembly | kbp | 159.3 | 244.8 |
| `Assembly length (Mbp)` | total assembly size | Mbp | 4.8 | 5.4 |
| `L50 (contigs)` | number of contigs needed to reach 50 % of the assembly | **count** | 11 | 8 |
| `Contigs` | contigs in the assembly | count | 370 | 390 |
| `Assembly GC (%)` | GC content of the **assembly** (not reads) | % of bases | 50.33 | 50.34 |
| `Reactions` / `Metabolites` | size of the reconstructed model | count | 2486 / 1551 | 2478 / 1538 |
| `MEMOTE score (%)` | MEMOTE total score (see §2.8) | score, not a % | 68.1 | 67.8 |

Coverage follows from `M Seqs`: 0.6 M reads × 2 ends × ~250 bp ≈ 300 Mbp over a 5 Mb genome ≈
**50–60×**. That is why `# contigs` can be in the hundreds without it being a coverage problem.

### 2.2 FastQC

Per-file plots: per-base quality, per-sequence quality, GC distribution, N content, length
distribution, duplication, over-represented sequences, adapter content, and a status-check
heatmap. Read them as *before/after trimming* pairs:

| what | read it as | investigate when |
|---|---|---|
| per-base quality | should stay above ~28 across the read | a tail dropping below ~20 → the trimming step is doing real work |
| adapter content | high in raw is normal | still high in *trimmed* → Trimmomatic's adapter list missed this library |
| over-represented sequences | a few are normal | one sequence at >1 % of reads → contamination or PCR duplicates |
| status checks | `% Failed` above | a *cluster* of failures in trimmed reads |

### 2.3 QUAST

| column | meaning | unit | K-12 | O157:H7 |
|---|---|---|---|---|
| `N50 (kbp)` | contig length such that contigs of that length or longer hold half the assembly | kbp | 159.3 | 244.8 |
| `Assembly length (Mbp)` | total assembly size | Mbp | 4.8 | 5.4 |
| `L50 (contigs)` | how many contigs reach that half | count, **scaled to thousands** | 0.0 (= 11) | 0.0 (= 8) |
| `Largest contig (kbp)` | longest contig | kbp | 358.9 | 674.2 |

`L50` is a **count**, so the header says `(contigs)` and the value is rendered raw
(`8.0` / `11.0`). The QUAST module scales count columns to thousands by default, which printed
an L50 of 8 as `0.0 K` — meaningless at that precision. The overlay sets
`quast_config.total_number_contigs_multiplier: 1` to stop that (F36).

N50/L50 measure *contiguity*, not *correctness*. Short-read assemblies fragment; that is
expected. Whether the sequence is right is answered by §2.4.

### 2.4 Assembly vs reference genome (step 5b)

Present only when the sample sheet supplies a `reference`. Our own BLASTN comparison (QUAST's
own reference mode is unusable on arm64 — see §3, Q1). Thresholds are QUAST's defaults:
identity >= 95 %, alignment >= 65 bp.

| column | meaning | denominator | K-12 | O157:H7 |
|---|---|---|---|---|
| `Genome fraction (%)` | how much of the **reference** the assembly covers | reference length | 99.38 | 97.37 |
| `Identity (%)` | length-weighted base agreement over aligned regions | aligned bases | 99.926 | 99.721 |
| `Mismatches (per 100 kbp)` | substitutions per 100 kbp of aligned sequence | aligned bases | 67.59 | 260.56 |
| `Indels (per 100 kbp)` | gap-opening events per 100 kbp | aligned bases | 5.55 | 11.33 |
| `Assembly aligned (%)` | how much of the **assembly** falls inside alignments | assembly length | 94.45 | 97.84 |
| `Aligned contigs (%)` | share of contigs with at least one alignment | contig count | 30.54 | 96.92 |
| `Reference (Mbp)` | length of the yardstick used | — | 4.64 | 5.59 |

Two different denominators, and that is the point: `Genome fraction` answers "did I recover it?",
`Assembly aligned` answers "is what I built supported?". `Aligned contigs` is a *shape* metric —
see Q13 in §3.

### 2.5 BUSCO

A bar plot of the 440 Enterobacterales markers (`enterobacterales_odb10`):

| segment | meaning | ours |
|---|---|---|
| `C` complete | markers found at full length (S + D) | 440/440 = 100 % |
| `S` single-copy | found exactly once | 438 |
| `D` duplicated | found more than once | 2 |
| `F` fragmented | partially found | 0 |
| `M` missing | not found | 0 |

Completeness is *gene presence*, not sequence correctness: a chimeric assembly can still be
100 % complete. That is what §2.4 is for.

### 2.6 CarveMe

| column | meaning | ours (K-12 / O157:H7) |
|---|---|---|
| `Reactions` | reactions in the SBML draft | 2486 / 2478 |
| `Metabolites` | metabolites | 1551 / 1538 |
| `Genes` | genes with a GPR association | 1623 / 1609 |
| `Compartments` | compartments (cytosol, periplasm, extracellular) | 3 / 3 |

Drafts are incomplete by construction: CarveMe builds from the annotation, so a missing gene
becomes a missing reaction. Compare with a published model of the same species, not with an
absolute threshold. See Q10 for why the embedded MEMOTE report shows 10 more reactions.

### 2.7 Growth on defined medium (step 9)

| column | meaning | ours (both) |
|---|---|---|
| `Growth on M9 (1/h)` | maximum biomass flux on the declared medium | 0.953 |
| `Complete medium (1/h)` | the same model with **all 342 exchanges open** | 66.000 / 66.001 |
| `No carbon source (1/h)` | negative control, must be 0 | 0.000 |
| `Anaerobic (1/h)` | negative control, must be lower | 0.228 |
| `Yield (gDW/g)` | biomass per gram of the carbon source | 0.529 |
| `Failed checks` | how many of the checker's assertions failed | 0 |

The `Complete medium` column is not a growth rate — it is the artefact that motivated this step
(§3.1). The **rate** depends on the medium's uptake bounds; the **yield** does not (§3.2).

### 2.8 MEMOTE (step 10)

| column | scope | ours (K-12 / O157:H7) |
|---|---|---|
| `Total score (%)` | weighted score of 12 consistency + annotation tests | 68.1 / 67.8 |
| `Consistency (%)` | mass/charge balance, blocked reactions, loops | 44.5 / 43.8 |
| `Annotation · metabolites (%)` | share of metabolites with an annotation | 82.5 / 82.4 |
| `Annotation · reactions (%)` | share of reactions with an annotation | 77.6 / 77.6 |
| `Annotation · genes (%)` | share of genes with an annotation/SBO term | 33.3 / 33.3 |
| `Annotation · SBO (%)` | share with Systems Biology Ontology terms | 90.9 / 90.9 |

The score is a **file-quality proxy**, not a statement about biology. The biomass, growth and
medium tests carry weight **0** in MEMOTE 0.17.0, so they cannot move it — a high score says
nothing about whether the model grows. That is why step 9 exists.

### 2.9 Per-step runtimes

Wall time in seconds per step, per sample, plus an `all samples` row for the cohort-level
MultiQC step. Two steps are deliberately absent: the growth check and the reference comparison.
They write no benchmark record, because `benchmarks/` is the Snakemake step set and an unknown
label would appear in this table as a raw column name (F26/F30). Their cost is seconds; the log
timestamps are the record.

### 2.10 Software versions

Tool and version for everything that ran. One entry is a known wart: **`Snakemake 9.25.2`**.
This table comes from the reused Snakemake-era MultiQC config (D4: byte-identical), and that row
is a leftover — this is a Nextflow pipeline (see Q18).

---

## 3. Questions a reviewer will ask

These are the ones that actually come up. Short answers, with where to find the evidence.

### Method

**Q1. Why didn't you just run QUAST's reference mode (`-r`)?**
We tried. QUAST 5.2 compiles a bundled minimap2 on first use, and on arm64 macOS that compile
fails (`-msse2`, `-mfpu=` are x86/Linux flags). QUAST then only *warns* and silently falls back
to reference-independent statistics — a comparison that is not one. It also has no hook for an
external aligner. So the comparison is its own step with BLASTN from the pinned Bakta
environment, at QUAST's default thresholds. QUAST's own output therefore stays byte-identical to
the original pipeline (F30/D20).

**Q2. Are these genome-fraction numbers comparable to dnadiff/QUAST's?**
Conceptually yes, and the thresholds match (identity >= 95 %, alignment >= 65 bp). The aligner
differs (BLASTN vs minimap2/nucmer), so expect differences in the last percent — treat
`99.38 %` as "essentially complete", not as a value to compare with a published 99.4 %.

**Q3. Why these two references? What if the isolate is another strain?**
The reference is a **yardstick**, not a truth claim. A different strain of the same species
lowers `Genome fraction` and raises `Mismatches` — which is a finding, not a failure (Q12). The
pipeline fails only when a reference matches *nothing* (exit 2), so a wrong file cannot quietly
produce a table of zeroes.

**Q4. Why are the growth rates identical for two different strains?**
Both models share CarveMe's template biomass equation and the same declared medium, so the
optimum is set by the medium and the template, not by the isolate. The number is reproducible,
not strain-discriminating. Do not use it to compare isolates.

**Q5. Why is there a 66 h⁻¹ column at all?**
Because it is the number that would have been reported as "growth" if nobody declared a medium.
It is CarveMe's complete medium (all 342 exchanges open): the cell imports every amino acid,
nucleotide, vitamin and metal, so the objective measures assembly speed, not growth (§3.1).

### Numbers that look contradictory

**Q6. Why does one sample appear as five rows, with blank cells?**
FastQC runs per FASTQ file (four per sample), assembly/model steps run per sample. The aggregate
table has one row per *entity*, so the same isolate appears as `SRR1770413` plus `_1`, `_2`,
`_R1_paired`, `_R2_paired`. Blank cells are "not applicable at this granularity", not missing
data. This is MultiQC's design, not a pipeline bug.

**Q7. `L50` is a count — why does it print `8.0`?**
Because MultiQC formats every numeric column with one decimal. The header says `(contigs)`, so it
cannot be misread as a length. This column used to be worse: the QUAST module scaled counts to
thousands, so an L50 of 8 appeared as `0.0 K`. The overlay now renders the raw count (F36).

**Q8. There are two GC columns: `Reads GC (%)` 50 and `Assembly GC (%)` 50.34. Which is the genome's?**
The second. `Reads GC (%)` is measured on the reads (FastQC), `Assembly GC (%)` on the assembly
(QUAST). They agree here, which is the expected sanity check; a divergence would point at
contamination or a wrong species.

**Q9. MEMOTE total is 68.1 % — so the model is 68 % correct?**
No. It is a weighted score over 12 consistency/annotation tests, not a fraction of anything
physical. Two weights are structurally zero (biomass/growth/medium tests), and annotation scores
depend on the databases rather than on your data. Use it to compare drafts, not to claim
correctness.

**Q10. The table says 2486 reactions, the embedded MEMOTE report says 2496. Which is wrong?**
Neither. COBRApy creates 10 exchange reactions on load for metabolites that have none in the
file (here: `na1`, `ni2`, `cd2`, `cm`, `hg2`, `fusa`, `gbbtn`, `novbcn`, `doxrbcn`, `ttrcyc`).
The SBML on disk has 2486; MEMOTE sees 2496. Both counts are correct for their own object
(F27). If you quote one, say which.

**Q11. BUSCO says 100 % complete, but genome fraction is 97.4 %. Contradiction?**
No. BUSCO asks "are the 440 conserved genes present?", genome fraction asks "how much of the
reference sequence did I recover?". A complete gene set can sit on an assembly that is missing
accessory regions — which is exactly the O157:H7 case.

**Q12. 440/440 complete yet 2 duplicates — is that a contaminated sample?**
`D = 2` means two markers occur more than once; the complete count still includes them. Two is
within normal variation for short-read assemblies and does not by itself indicate a mixed
culture. High `D` (say > 5 %, i.e. > 20 markers) would.

**Q13. Only 30.5 % of the K-12 contigs align — is the assembly bad?**
No, and this is the most misread number in the report. Those 113 aligned contigs carry 94.5 % of
the assembly's bases; the remaining ~257 are tiny fragments (often below the 65 bp alignment
threshold). Read `Genome fraction` (99.38 %) for completeness and ignore the contig share.

**Q14. `% Dups` 12–15 % — is that high?**
It is normal for short-read libraries and does not affect the assembly conclusions; it inflates
apparent coverage slightly. Compare raw vs trimmed: our trimming does not change it, which is
expected (duplicates are mostly PCR artefacts, not adapters).

**Q15. `% Failed` 10–20 % — did 20 % of the reads fail?**
No. That number is the share of FastQC *modules* (per-base quality, adapter content, …) that
failed for that file. It drops to 0 % after trimming, which is the useful signal.

**Q16. Assembly length 5.40 Mbp vs reference 5.59 Mbp, yet genome fraction is 97.4 % — consistent?**
Yes: 97.4 % of 5.59 Mbp ≈ 5.45 Mbp of the reference is covered. The assembly is *shorter* than
the reference because it lacks ~190 kbp of accessory/plasmid sequence present in Sakai (and has
a little extra of its own). Consistent coverage of a shorter genome is exactly what a different
strain looks like.

**Q17. Why are the growth check and the reference comparison missing from the runtime table?**
They write no `benchmarks/` record: that directory is the Snakemake step set, and an unknown
label would surface as a raw column. Their measured cost is seconds; the log timestamps are the
record (F26/F30).

**Q18. The version table lists Snakemake 9.25.2 — isn't this a Nextflow pipeline?**
Yes, it is. That row is a leftover from the reused Snakemake-era MultiQC config (kept
byte-identical on purpose, D4). The `benchmarks/` format is Snakemake's, which is why the config
mentions it. Treat the row as documentation of the benchmark format, not of the engine; the
engine is Nextflow (see the run header, `Nextflow >= 25.10`). F33 records this as a known wart.

**Q19. Why do the numbers contain spaces, e.g. `2 486`?**
MultiQC's thousand separator. `2 486` = 2486.

**Q20. `Annotation · genes` is 33.3 % for both models — is that a red flag?**
It is a property of CarveMe drafts, not of your data: gene-associated annotations and SBO terms
are sparse in reconstructed models. It is identical across the two isolates because both came
through the same reconstruction path. It does not affect the growth or reference results.

---

## 4. Three things that look right and are not

### 4.1 The 66 h⁻¹ "growth rate"

CarveMe ships every draft with all exchange reactions open — a complete medium in which amino
acids, nucleotides, vitamins and metals are all importable. FBA on that medium measures how fast
the cell can *assemble* biomass from pre-made parts: 66 h⁻¹ for these drafts. It was already
visible in the ported pipeline's own MEMOTE output (`test_biomass_default_production`,
`data = 65.99993`) and is easy to miss, because MEMOTE's growth tests do not affect the score.

A growth rate is meaningless without a medium. That is the entire reason step 9 exists: it
declares a medium (`media/m9_aerobic.yaml`) and reports against it.

### 4.2 The growth rate depends on the uptake bound you choose

The medium file sets uptake **bounds** (mmol·gDW⁻¹·h⁻¹), which are not the same thing as a
laboratory recipe in mM. Measured sensitivity for SRR1770413 on M9:

| glucose bound | O₂ bound | predicted growth |
|---|---|---|
| 5 | 20 | 0.476 h⁻¹ |
| **10** | **20** | **0.953 h⁻¹** ← the project's convention |
| 10 | 1000 | 0.953 h⁻¹ (oxygen is not limiting) |
| 20 | 20 | 1.283 h⁻¹ |
| 1000 | 1000 | 28.772 h⁻¹ |

So "0.953 h⁻¹" is a **carbon-limited rate under a stated convention**, not a measurement. The
robust quantity is the **yield** — 95 gDW/mol glucose ≈ 0.53 g/g — because it is the model's
biomass stoichiometry and does not move when the uptake bound changes. Anchoring plausibility on
the yield (against ~0.5 g/g measured for *E. coli* on glucose minimal medium) is therefore the
stronger claim; the checker asserts both.

### 4.3 A low genome fraction does not mean a broken assembly

Sakai is one O157:H7 isolate. This project's O157:H7 sample recovers 97.4 % of it at 99.72 %
identity with a four-fold higher mismatch rate than the K-12 sample — the signature of a
**different strain of the same serotype** (accessory genome differs), not of a bad assembly. The
pipeline therefore fails only when a reference matches *nothing* (exit 2); a partial match is
reported, and interpreting it is your job. If you need a tighter comparison, use a reference of
the same strain.

Related: QUAST's own `-r` is deliberately not used (Q1). Step 5b replaces it.

---

## 5. Troubleshooting

| symptom | most likely cause | confirm with | fix |
|---|---|---|---|
| growth `0.000` | medium compound not mapped to the model, or the carbon source is absent | `logs/growth/growth_check.log` — it names unmappable compounds | fix the medium YAML or the exchange id |
| growth task exits 2 | a medium component has no exchange reaction in the model | the log's message (structural, not biological) | correct the medium, or use a medium for this organism |
| growth above the physical bound | bounds too generous / medium accidental | the medium's `components` | tighten the carbon bound (§4.2) |
| BUSCO `< 90 %` | wrong lineage or fragmented assembly | `short_summary.*.txt`, QUAST N50 | set `--busco_lineage`, or fix the reads |
| BUSCO `D` high | duplicated genome / mixed culture | QUAST total length vs expected | re-check the sample; consider a different isolate |
| genome fraction low | reference of a different strain | Q3/Q12, Bakta species markers | use a same-strain reference |
| refcompare task exits 2 | reference does not match the assembly at all | `logs/refcompare/<sample>.log` | wrong file in the sheet, or the sample is not the organism claimed |
| run aborts before any task, mentions GiB | disk pre-flight (F20) | the message names the volume and the cleanup commands | free space or `--min_free_disk_gb=0` |
| MEMOTE crashes | MEMOTE 0.17.0 bugs (e.g. `test_find_medium_metabolites`) | `logs/memote/<sample>.log` | known; the port keeps the original invocation |
| `reference not found` | sheet path or `--ref_dir` | the error names the resolved path | download the reference (README) or empty the cell |

---

## 6. What changing a parameter does

| parameter | effect |
|---|---|
| `threads_default` | wall time only; no output change |
| `min_free_disk_gb` | pre-flight only; `0` disables it |
| `quast_min_contig` | changes how many contigs QUAST counts, not the assembly |
| `busco_lineage` | changes which markers are scored **and the output filenames the report looks for** |
| `carveme_solver`, `memote_solver` | solver choice; results should agree, speeds differ |
| medium `components` bounds | growth rate (strongly — see §4.2), yield (not) |
| `report_tag` | filename of the report only; lets two runs coexist |
| `reference` column | adds a comparison; absent → no task, no section |

---

## 7. Reading the embedded reports

Two sections embed other people's HTML: QUAST's interactive assembly report (`.report.html`) and
each sample's full MEMOTE snapshot. Both pages carry their own CSS and JavaScript, so they are
wrapped in isolated `srcdoc` iframes — they cannot affect the report around them, and a missing
embedded page is a warning rather than a crashed report.

The embedded pages are the *original* outputs. When a number differs from the summary table, the
summary is our extraction of a specific field and the embedded page is the full document — Q10 is
the one case where both are right, and §2.10 is the one known wart.
