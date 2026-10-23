"""Sample-sheet validation and serialization for the local Web UI.

Contract: docs/ui.md section 6.  The server is the authority -- the browser does
the same checks only to give immediate feedback.

The sheet the UI writes is *not* the tracked ``samples.tsv`` at the project
root (UI-D1): it lives in ``.nextflow/ui/runs/<run_id>/samples.tsv`` and its
FASTQ paths are absolute, so nothing is copied and no input file is touched.

Column order is fixed and must not drift: the pipeline parses the header and
``reference`` is looked up by name, but a stable order keeps the file diffable
and lets tests compare it byte for byte.
"""

import os
import re

#: Fixed column order, exactly as docs/ui.md section 6 requires.
HEADER = ("sample", "fq1", "fq2", "species", "strain", "reference")

#: A sample name becomes a path component inside work/ and results/.
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Suffixes the browser directory picker treats as reads (informational only).
FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")

#: README of the pipeline: non-gzip inputs are only a warning -- the pipeline
#: itself decides whether it can process them (Trimmomatic/SPAdes accept plain
#: FASTQ, FastQC does too).  Never block on this.
GZIP_SUFFIXES = (".gz",)


def _add_error(errors, row, field, code, message):
    errors.append({"row": row, "field": field, "code": code, "message": message})


def _bad_chars(value):
    """A tab or newline would corrupt the TSV instead of failing loudly later."""
    return ("\t" in value) or ("\n" in value) or ("\r" in value)


def _check_meta(value, field, row, errors):
    if value is None:
        return ""
    if not isinstance(value, str):
        _add_error(errors, row, field, "invalid", "Row %d %s must be a string" % (row, field))
        return ""
    if _bad_chars(value):
        _add_error(errors, row, field, "invalid_chars",
                   "Row %d %s must not contain tabs or newlines" % (row, field))
        return value
    return value


def _check_path(value, field, row, errors, warnings, required):
    """Validate one FASTQ/reference path.  Returns the value to write (or "")."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        _add_error(errors, row, field, "invalid", "Row %d %s must be a string" % (row, field))
        return ""
    if _bad_chars(value):
        _add_error(errors, row, field, "invalid_chars",
                   "Row %d %s must not contain tabs or newlines" % (row, field))
        return ""
    if value == "":
        if required:
            _add_error(errors, row, field, "required",
                       "Row %d %s is required" % (row, field))
        return ""
    if not value.startswith("/"):
        _add_error(errors, row, field, "not_absolute",
                   "Row %d %s must be an absolute path: %s" % (row, field, value))
        return value
    path = os.path.realpath(os.path.expanduser(value))
    if not os.path.exists(path):
        _add_error(errors, row, field, "not_found",
                   "Row %d %s does not exist: %s" % (row, field, path))
        return value
    if not os.path.isfile(path):
        _add_error(errors, row, field, "not_file",
                   "Row %d %s is not a regular file: %s" % (row, field, path))
        return value
    if not os.access(path, os.R_OK):
        _add_error(errors, row, field, "unreadable",
                   "Row %d %s is not readable: %s" % (row, field, path))
        return value
    try:
        if os.path.getsize(path) == 0:
            _add_error(errors, row, field, "empty_file",
                       "Row %d %s is an empty file: %s" % (row, field, path))
            return value
    except OSError as exc:
        _add_error(errors, row, field, "unreadable",
                   "Row %d %s cannot be read: %s (%s)" % (row, field, path, exc))
        return value
    if field in ("fq1", "fq2") and not path.lower().endswith(GZIP_SUFFIXES):
        warnings.append({
            "row": row, "field": field, "code": "not_gzip",
            "message": "Row %d %s is not a .gz file; the pipeline may not be able to process it: %s"
                       % (row, field, path),
        })
    return value


def validate_samples(samples):
    """Validate the ``samples`` array of ``POST /api/run``.

    Returns ``{"errors": [...], "warnings": [...]}``.  ``errors`` is what the
    contract puts in the 400 body (``row`` + ``field`` + ``code``); a non-empty
    list means the process must not be spawned.
    """
    errors = []
    warnings = []

    if not isinstance(samples, list):
        _add_error(errors, 0, "samples", "no_samples", "samples must be an array")
        return {"errors": errors, "warnings": warnings}
    if len(samples) == 0:
        _add_error(errors, 0, "samples", "no_samples", "At least one sample is required")
        return {"errors": errors, "warnings": warnings}

    seen = {}
    for idx, item in enumerate(samples):
        row = idx + 2  # row 1 is the header
        if not isinstance(item, dict):
            _add_error(errors, row, "samples", "invalid",
                       "Sample %d must be an object" % row)
            continue

        name = item.get("name")
        if name is None:
            name = item.get("sample")
        if name is None or not isinstance(name, str) or name == "":
            _add_error(errors, row, "sample", "empty", "Row %d sample must not be empty" % row)
            name = ""
        elif _bad_chars(name):
            _add_error(errors, row, "sample", "invalid_chars",
                       "Row %d sample must not contain tabs or newlines" % row)
        elif not NAME_RE.match(name):
            _add_error(errors, row, "sample", "invalid",
                       "Row %d sample may only contain letters, digits, dots, underscores and hyphens: %s"
                       % (row, name))
        elif name in (".", ".."):
            _add_error(errors, row, "sample", "invalid",
                       "Row %d sample must not be %s" % (row, name))
        if name and name not in ("", ".", ".."):
            if name in seen:
                _add_error(errors, row, "sample", "duplicate",
                           "Row %d sample is duplicated: %s (already used on row %d)"
                           % (row, name, seen[name]))
            else:
                seen[name] = row

        fq1 = _check_path(item.get("fq1"), "fq1", row, errors, warnings, required=True)
        fq2 = _check_path(item.get("fq2"), "fq2", row, errors, warnings, required=True)
        if fq1 and fq2:
            # Symlink-safe comparison: point at the same file means the pipeline
            # would run blind on a duplicated read set.
            if os.path.realpath(os.path.expanduser(fq1)) == \
                    os.path.realpath(os.path.expanduser(fq2)):
                _add_error(errors, row, "fq2", "same_file",
                           "Row %d fq2 is the same file as fq1: %s" % (row, fq2))

        _check_path(item.get("reference"), "reference", row, errors, warnings,
                    required=False)
        _check_meta(item.get("species"), "species", row, errors)
        _check_meta(item.get("strain"), "strain", row, errors)

    return {"errors": errors, "warnings": warnings}


def write_sheet(path, samples):
    """Write the run's ``samples.tsv``: fixed header, LF, trailing newline.

    Only called after :func:`validate_samples` returned an empty ``errors``
    list, so every path here is absolute and every field is tab-free.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    lines = ["\t".join(HEADER)]
    for item in samples:
        name = item.get("name")
        if name is None:
            name = item.get("sample")
        cols = [
            _field(name),
            _field(item.get("fq1")),
            _field(item.get("fq2")),
            _field(item.get("species")),
            _field(item.get("strain")),
            _field(item.get("reference")),
        ]
        lines.append("\t".join(cols))
    data = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
    return data


def _field(value):
    """None/omitted -> empty column; strings verbatim (validated to be TSV-safe)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def read_refs(path):
    """Count samples with a non-empty ``reference`` column in a written sheet.

    Needed for the progress denominator (``11*N + refs + 2``) when the UI
    attaches to a run it did not launch: the run record deliberately carries no
    extra bookkeeping field, so the sheet is the source of truth.
    """
    refs = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0
    if not lines:
        return 0
    header = lines[0].split("\t")
    try:
        ri = header.index("reference")
    except ValueError:
        return 0
    for line in lines[1:]:
        if not line.strip():
            continue
        cols = line.split("\t")
        if ri < len(cols) and cols[ri].strip():
            refs += 1
    return refs


def samples_from_sheet(path):
    """Best-effort sample names from a written sheet (for attach replay)."""
    names = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return names
    if not lines:
        return names
    header = lines[0].split("\t")
    try:
        si = header.index("sample")
    except ValueError:
        return names
    for line in lines[1:]:
        if not line.strip():
            continue
        cols = line.split("\t")
        if si < len(cols):
            names.append(cols[si].strip())
    return names
