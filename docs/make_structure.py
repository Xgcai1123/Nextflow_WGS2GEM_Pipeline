#!/usr/bin/env python3
"""Generate docs/project-structure.md (and a PNG tree) from the TRACKED file list.

Why generate instead of hand-writing: the request was a tree of the *pipeline*, with
no artefacts in it. Reading `git ls-files` makes that structural -- `work/`,
`.nextflow/`, `data/`, `models/`, `db/reference/` and every other generated or
fetched path are untracked, so they cannot appear here even by accident. The script
also refuses to run if a tracked file is not classified, so the document cannot
silently drift out of date.

Usage:
    python3 docs/make_structure.py            # write .md and .png
    python3 docs/make_structure.py --md-only  # skip the PNG (no Pillow needed)
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (key, heading, description, predicate). Order is the reading order of the doc.
GROUPS = [
    ("entry", "The pipeline you run",
     "The only files Nextflow needs. Everything else exists to serve these four.",
     lambda p: p in {"main.nf", "nextflow.config", "samples.tsv",
                     "samples.with_reference.tsv"}),
    ("envs", "Execution environments",
     "One pinned conda environment per tool (D4: byte-identical to the original pipeline).",
     lambda p: p.startswith("envs/")),
    ("media", "Data definitions",
     "Declared inputs that are data rather than configuration (F25/F31).",
     lambda p: p.startswith("media/")),
    ("helpers", "Helper programs called by tasks",
     "Python/bash helpers the processes invoke; the reused ones are byte-identical (D4).",
     lambda p: p.startswith("scripts/") or p.startswith("bin/")),
    ("report", "Report configuration",
     "MultiQC config: the reused one plus the presentation overlay (section order, F21).",
     lambda p: p.startswith("multiqc_")),
    ("tests", "Tests and CI",
     "Eight local checks and the five-job workflow; nothing here runs tools.",
     lambda p: p.startswith("tests/") or p.startswith(".github/")),
    ("docs", "Documentation",
     "Entry points (README), the change log, the design record, and this tree.",
     lambda p: p in {"README.md", "CHANGELOG.md", "LICENSE"} or p.startswith("docs/")),
    ("meta", "Repository metadata",
     "Ignore rules and line-ending policy.",
     lambda p: p in {".gitignore", ".gitattributes"}),
    ("probe", "Not the pipeline: the engine probe",
     "A minimal Nextflow project that validates engine behaviour on a version upgrade"
     " (projectDir in nextflow.config, conda name:, CONDA_PREFIX in a task, publishDir with an"
     " input variable). Documented in README > Repository layout; not part of the workflow and"
     " not run by CI.",
     lambda p: p.startswith("m0probe/")),
]

# Directories that exist but are never tracked. Explicitly listed so the "no
# artefacts" promise is verifiable rather than implied.
UNTRACKED = [
    ("work/", "Nextflow task cache (per-task inputs, outputs, logs, .command.sh)",
     "regenerable; delete freely (`nextflow clean`)"),
    (".nextflow/", "run history, cache, and the conda environments built from envs/",
     "regenerable; deleting the conda dir costs a rebuild"),
    ("data/raw/", "raw paired-end FASTQ", "supplied; never regenerable"),
    ("data/db/", "Bakta database (4 GB) and BUSCO lineage cache", "fetched; see docs/asset-sources.md"),
    ("data/reference/", "the optional NCBI reference genomes (step 5b)", "fetched"),
    ("data/cache/", "persistent caches (AMR guard's known-bad sequences)",
     "regenerable, at ~5 min per sample of rediscovery"),
    ("results/qc/", "every quality report: FastQC, QUAST, BUSCO, reference comparison",
     "regenerable by re-running"),
    ("results/reads/", "trimmed reads", "regenerable by re-running"),
    ("results/assembly/", "SPAdes assemblies", "regenerable by re-running"),
    ("results/annotation/", "Bakta annotations", "regenerable by re-running"),
    ("results/models/", "CarveMe models, MEMOTE results, growth check output",
     "regenerable by re-running"),
    ("results/report/", "the MultiQC report, its parsed data, Nextflow's own report/trace",
     "regenerable by re-running"),
    ("logs/, benchmarks/", "per-task logs and wall times", "regenerable by re-running"),
]


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("not a git repository: %s" % out.stderr.strip())
    return [p for p in out.stdout.splitlines() if p.strip()]


def classify(files):
    """Group every tracked file exactly once; refuse to guess."""
    assigned = {}
    for key, _label, _desc, pred in GROUPS:
        assigned[key] = sorted(f for f in files if pred(f))
    claimed = [f for group in assigned.values() for f in group]
    unclassified = sorted(set(files) - set(claimed))
    duplicated = sorted({f for f in claimed if claimed.count(f) > 1})
    if unclassified or duplicated:
        sys.exit("classification is not a partition -- unclassified: %s | duplicated: %s"
                 % (unclassified, duplicated))
    return assigned


def tree_lines(paths, collapse_over=0):
    """Render a flat path list as tree lines with box-drawing characters.

    collapse_over > 0 folds directories with more children than that into a single
    annotated line. Used for the figure only: the .md lists every file, but a slide
    does not benefit from fourteen fixture filenames.
    """
    tree = {}
    for path in paths:
        node = tree
        for part in path.split("/"):
            node = node.setdefault(part, {})
    lines = []

    def walk(node, prefix):
        items = sorted(node.items(), key=lambda kv: (not kv[1], kv[0]))
        for i, (name, child) in enumerate(items):
            last = i == len(items) - 1
            if child and collapse_over and len(child) > collapse_over:
                lines.append("%s%s%s/  (%d files)" % (
                    prefix, "\u2514\u2500\u2500 " if last else "\u251c\u2500\u2500 ", name, len(child)))
                continue
            lines.append("%s%s%s%s" % (prefix, "\u2514\u2500\u2500 " if last else "\u251c\u2500\u2500 ",
                                       name, "/" if child else ""))
            if child:
                walk(child, prefix + ("    " if last else "\u2502   "))

    walk(tree, "")
    return lines


def build_md(files, assigned, commit):
    lines = [
        "# Project structure (the pipeline only)",
        "",
        "Generated from `git ls-files` at commit `%s` by `docs/make_structure.py` -- **regenerate it, do"
        " not hand-edit**. Because only tracked files are read, generated artefacts and fetched"
        " databases cannot appear below: the \"pipeline only\" property is structural." % commit,
        "",
        "%d tracked files, in the order you would read them:" % len(files),
        "",
    ]
    for key, label, desc, _pred in GROUPS:
        group = assigned[key]
        if not group:
            continue
        lines.append("## %s (%d file%s)" % (label, len(group), "" if len(group) == 1 else "s"))
        lines.append("")
        lines.append(desc)
        lines.append("")
        lines.append("```")
        lines.append("%s/" % ROOT.name)
        lines.extend(tree_lines(group))
        lines.append("```")
        lines.append("")
    lines.append("## What is deliberately NOT in the repository")
    lines.append("")
    lines.append("Every path below is git-ignored. They are the pipeline's *outputs* and its"
                 " *fetched* inputs, which is why the tree above stays readable:")
    lines.append("")
    lines.append("| path | what it holds | can it be recreated? |")
    lines.append("|---|---|---|")
    for path, what, recreate in UNTRACKED:
        lines.append("| `%s` | %s | %s |" % (path, what, recreate))
    lines.append("")
    lines.append("## The three files that matter most")
    lines.append("")
    lines.append("| file | why you would open it |")
    lines.append("|---|---|")
    lines.append("| `main.nf` | the whole workflow: 11 steps, 12 processes, one file |")
    lines.append("| `nextflow.config` | every parameter, every output path, every resource"
                 " directive |")
    lines.append("| `docs/interpretation.md` | what the numbers in the report mean |")
    lines.append("")
    return "\n".join(lines)


def build_png(assigned, out_path):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("Pillow not available -- skipping the PNG (use --md-only to silence this)")
        return False

    font_path = "/System/Library/Fonts/Menlo.ttc"
    if not Path(font_path).exists():
        font_path = "/System/Library/Fonts/Monaco.ttf"
    size, line_h = 19, 27
    try:
        font = ImageFont.truetype(font_path, size)
        bold = ImageFont.truetype(font_path, size + 2)
        title_f = ImageFont.truetype(font_path, 34)
    except OSError:
        print("no monospace font found -- skipping the PNG")
        return False

    columns = [GROUPS[:4], GROUPS[4:]]
    blocks = []
    for col in columns:
        block = []
        for key, label, _desc, _pred in col:
            group = assigned[key]
            if not group:
                continue
            block.append(("head", "%s" % label))
            block.extend(("line", l) for l in tree_lines(group, collapse_over=4))
            block.append(("gap", ""))
        blocks.append(block)

    width = 3000
    pad = 90
    col_w = (width - 3 * pad) // 2
    height = pad + 150 + max(len(b) for b in blocks) * line_h + 150

    img = Image.new("RGB", (width, height), "#f7f9fb")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, 118], fill="#0f223d")
    d.text((pad, 34), "NextFlow-WGS2GEM - project structure (pipeline only)", font=title_f,
           fill="#ffffff")
    d.text((pad, 130), "generated from git ls-files; nothing generated or fetched appears here",
           font=font, fill="#53606d")

    for c, block in enumerate(blocks):
        x = pad + c * (col_w + pad)
        y = 190
        for kind, text in block:
            if kind == "head":
                d.text((x, y), text, font=bold, fill="#178fa0")
                y += line_h + 6
            elif kind == "line":
                d.text((x, y), text, font=font, fill="#0f223d")
                y += line_h
            else:
                y += line_h // 2

    d.text((pad, height - 62),
           "outputs (data/, assembly/, models/, reports/, work/, .nextflow/, db/) are git-ignored"
           " and intentionally absent",
           font=font, fill="#53606d")
    img.save(out_path)
    return True


def main():
    md_only = "--md-only" in sys.argv
    files = tracked_files()
    assigned = classify(files)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip() or "(no commit)"
    md = ROOT / "docs" / "project-structure.md"
    md.write_text(build_md(files, assigned, commit), encoding="utf-8")
    print("wrote %s (%d tracked files)" % (md.relative_to(ROOT), len(files)))
    if not md_only:
        png = ROOT / "docs" / "project-structure.png"
        if build_png(assigned, png):
            print("wrote %s" % png.relative_to(ROOT))


if __name__ == "__main__":
    main()
