# Reading the results

What every number in this pipeline means, when it indicates a problem, and — more importantly —
what it does **not** license you to conclude. Written for the person who runs the pipeline and
then has to explain the output to someone else.

Reference points throughout are this project's two validation isolates:

| | SRR1770413 | SRR10194530 |
|---|---|---|
| organism / strain | *E. coli* K-12 | *E. coli* O157:H7 |
| reference used | GCA_000005845.2 (MG1655) | GCA_000008865.2 (Sakai) |

---

## 1. A successful run, at a glance

Exit code 0, `reports/multiqc/multiqc_report.html` written, and per sample roughly:

| signal | healthy | this project |
|---|---|---|
| BUSCO complete | >= 95 % | 100 % (440/440) |
| genome fraction (with reference) | >= 95 % | 99.4 % / 97.4 % |
| growth on the declared medium | 0.4–1.5 h⁻¹ | 0.95 h⁻¹ |
| biomass yield | 0.3–0.7 g/g glucose | 0.53 g/g |
| MEMOTE total | 60–75 for a draft | 68.1 / 67.8 |

If one of these is off, §4 tells you where to look. If they are all in range, the run is
*plausible* — not *validated*. §3 explains the difference.

---

## 2. Metric by metric

### Step 1 / 3 — FastQC (raw and trimmed)

| metric | read it as | investigate when |
|---|---|---|
| per-base quality | should stay above ~28 across the read | a tail dropping below ~20 → trimming is doing real work, check what survived |
| adapter content | high in raw is normal | still high in *trimmed* → Trimmomatic's adapter list missed this library |
| over-represented sequences | a few are normal | a single sequence at >1 % of reads → contamination or PCR duplicates |

The pair of reports exists so you can see the *effect* of step 2, not just its success.

### Step 2 — Trimmomatic

Surviving reads are the honest denominator for everything downstream: if 40 % of reads are
discarded, a low BUSCO score is a data problem, not an assembly problem. This project's
settings (`LEADING:3 TRAILING:3 SLIDINGWINDOW:4:20 MINLEN:36`) are the original pipeline's,
kept verbatim.

### Step 4 / 5 — SPAdes and QUAST

| metric | read it as | investigate when |
|---|---|---|
| `# contigs` (>= 1000 bp) | fragmentation of the draft | hundreds of contigs is normal for short reads; it is *not* a failure |
| N50 / L50 | contiguity | N50 in the tens of kb is expected here, not megabases — short reads do not close genomes |
| Total length | compare with the expected genome size | `> 1.1x` expected → duplicated regions or contamination; `< 0.9x` → under-assembly |
| GC (%) | compare with the reference's GC | a difference above ~2 percentage points suggests the wrong organism or a contaminated sample |

QUAST reports `GC (%)` and, when a reference is supplied to QUAST itself, `Reference GC (%)`.
Note this pipeline does **not** use QUAST's reference mode (see §3.3); the reference comparison
is its own step with its own section.

### Step 6 — BUSCO

| metric | read it as | investigate when |
|---|---|---|
| `C` complete | share of the 440 Enterobacterales markers found | `< 90 %` → incomplete assembly, or the wrong lineage (`--busco_lineage`) |
| `S` single-copy vs `D` duplicated | `D` above ~5 % suggests a duplicated genome, a mixed culture, or a very fragmented assembly | high `D` |
| `F` fragmented / `M` missing | which genes are absent | a cluster of missing genes → real deletion or a coverage gap |

BUSCO completeness is *gene presence*, not correctness: a chimeric assembly can still be 100 %
complete. That is what the reference comparison is for.

### Step 7 — Bakta

CDS/tRNA/rRNA counts are a sanity check on the annotation, not a quality score. A CDS count far
from `genome size / ~950 bp` means gene calling went wrong. The AMR guard reports resistance
hits: absence is not evidence of susceptibility (the database is incomplete), and presence is
not evidence of resistance (a silent or truncated gene still matches).

### Step 8 — CarveMe

Reaction/metabolite/gene counts describe the **draft**, and drafts are incomplete by
construction: CarveMe builds from the annotation, so a missing gene becomes a missing reaction,
which becomes a gap-filled or absent pathway. Compare with a published model of the same
species, not with an absolute threshold. This project: 2486/1551/1623 and 2478/1538/1609
(reactions/metabolites/genes), 3 compartments.

### Step 9 — growth on the declared medium

| quantity | read it as | investigate when |
|---|---|---|
| growth rate (h⁻¹) | maximum biomass flux under the declared medium | `0.000` → no carbon source reaching the cell, or the medium does not map onto the model (the task fails with exit 2 if a compound cannot be mapped) |
| `> 2.81 h⁻¹` | physically impossible (fastest known bacterium) | always — that is the bound the checker asserts |
| yield (gDW per g substrate) | how much biomass per gram of carbon | outside 0.3–0.7 for glucose → the biomass equation is probably wrong |
| the "complete medium" row | ~66 h⁻¹ for a CarveMe draft | **expected**, not a finding — see §3.1 |

The rate is **conditional on the medium's uptake bounds** (§3.2). The yield is the robust
number; prefer it when you need to make a claim.

### Step 10 — MEMOTE

| score | scope | read it as |
|---|---|---|
| total | 12 weighted tests: stoichiometric consistency + annotation | a *code-quality* proxy for the model file, not a statement about biology |
| consistency | mass/charge balance, blocked reactions, loops | `< 40 %` → the network has real problems |
| annotation | metabolite/reaction/gene annotation and SBO terms | depends on the databases, not on your data |

Crucially: the biomass/growth/medium tests carry weight **0** in MEMOTE 0.17.0, so they cannot
move the score. A high MEMOTE score says nothing about whether the model grows. That is why
step 9 exists.

### Step 5b — assembly vs reference genome

| metric | read it as | investigate when |
|---|---|---|
| genome fraction | share of the reference recovered by the assembly | `< 90 %` → wrong strain, contamination, or missing regions |
| identity | length-weighted identity over accepted alignments | `< 99 %` → wrong strain or a low-quality assembly |
| mismatches / 100 kbp | substitutions per 100 kbp aligned | `> 500` → likely a different strain rather than sequencing error |
| indels / 100 kbp | gap-opening events | `> 50` → assembly indel errors (short-read homopolymer trouble) |
| aligned contigs | share of contigs with any alignment | low with high genome fraction is *fine*: a few large contigs carry the genome and the rest are tiny |

---

## 3. Three things that look right and are not

### 3.1 The 66 h⁻¹ "growth rate"

CarveMe ships every draft with all exchange reactions open — a complete medium in which amino
acids, nucleotides, vitamins and metals are all importable. FBA on that medium measures how fast
the cell can *assemble* biomass from pre-made parts: 66 h⁻¹ for these drafts. It was already
visible in the ported pipeline's own MEMOTE output (`test_biomass_default_production`,
`data = 65.99993`) and is easy to miss, because MEMOTE's growth tests do not affect the score.

A growth rate is meaningless without a medium. That is the entire reason step 9 exists: it
declares a medium (`media/m9_aerobic.yaml`) and reports against it.

### 3.2 The growth rate depends on the uptake bound you choose

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
stronger claim; the checker now asserts both.

### 3.3 A low genome fraction does not mean a broken assembly

Sakai is one O157:H7 isolate. This project's O157:H7 sample recovers 97.4 % of it at 99.72 %
identity with a four-fold higher mismatch rate than the K-12 sample — the signature of a
**different strain of the same serotype** (accessory genome differs), not of a bad assembly. The
pipeline therefore fails only when a reference matches *nothing* (exit 2); a partial match is
reported, and interpreting it is your job. If you need a tighter comparison, use a reference of
the same strain.

Related: QUAST's own `-r` is deliberately not used. On arm64 macOS its bundled minimap2 does
not compile (`-msse2`, `-mfpu=`), after which QUAST *warns* and silently reports
reference-independent statistics — a reference comparison that is not one. Step 5b replaces it.

---

## 4. Troubleshooting

| symptom | most likely cause | confirm with | fix |
|---|---|---|---|
| growth `0.000` | medium compound not mapped to the model, or the carbon source is absent | `logs/growth/growth_check.log` — it names unmappable compounds | fix the medium YAML or the exchange id |
| growth task exits 2 | a medium component has no exchange reaction in the model | the log's message (structural, not biological) | correct the medium, or use a medium for this organism |
| growth above the physical bound | bounds too generous / medium accidental | the medium's `components` | tighten the carbon bound (§3.2) |
| BUSCO `< 90 %` | wrong lineage or fragmented assembly | `short_summary.*.txt`, QUAST N50 | set `--busco_lineage`, or fix the reads |
| BUSCO `D` high | duplicated genome / mixed culture | QUAST total length vs expected | re-check the sample; consider a different isolate |
| genome fraction low | reference of a different strain | §3.3, Bakta species markers | use a same-strain reference |
| refcompare task exits 2 | reference does not match the assembly at all | `logs/refcompare/<sample>.log` | wrong file in the sheet, or the sample is not the organism claimed |
| run aborts before any task, mentions GiB | disk pre-flight (F20) | the message names the volume and the cleanup commands | free space or `--min_free_disk_gb=0` |
| MEMOTE crashes | MEMOTE 0.17.0 bugs (e.g. `test_find_medium_metabolites`) | `logs/memote/<sample>.log` | known; the port keeps the original invocation |
| `reference not found` | sheet path or `--ref_dir` | the error names the resolved path | download the reference (README) or empty the cell |

---

## 5. What changing a parameter does

| parameter | effect |
|---|---|
| `threads_default` | wall time only; no output change |
| `min_free_disk_gb` | pre-flight only; `0` disables it |
| `quast_min_contig` | changes how many contigs QUAST counts, not the assembly |
| `busco_lineage` | changes which markers are scored **and the output filenames the report looks for** |
| `carveme_solver`, `memote_solver` | solver choice; results should agree, speeds differ |
| medium `components` bounds | growth rate (strongly — see §3.2), yield (not) |
| `report_tag` | filename of the report only; lets two runs coexist |
| `reference` column | adds a comparison; absent → no task, no section |

---

## 6. Navigating the report

Sections appear in pipeline order, so the report reads like the workflow:

1. **FastQC** — raw and trimmed side by side.
2. **QUAST: assembly QC** — contiguity and size.
3. **Assembly vs reference** — only when a reference was supplied (§2, step 5b).
4. **QUAST: full reports** — QUAST's own interactive page, embedded.
5. **BUSCO** — completeness and duplication.
6. **CarveMe: draft model size** — what was reconstructed.
7. **Growth on defined medium** — the viability check, with the complete-medium artefact beside it.
8. **MEMOTE: model quality** — the scorecard, with its scope caveat.
9. **MEMOTE: full reports** — the complete per-sample MEMOTE pages, embedded.
10. **Per-step runtimes** and **software versions** — provenance for the run.

The embedded MEMOTE and QUAST pages are isolated in `srcdoc` iframes: they bring their own CSS
and JavaScript, so they cannot affect the report around them. A missing embedded page is a
warning, not a crashed report.
