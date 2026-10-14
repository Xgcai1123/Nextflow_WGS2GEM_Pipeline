"""
Chunked-retry guard for AMRFinderPlus inside bakta's AMR expert system.

WHY THIS EXISTS
---------------
Upstream bug: https://github.com/ncbi/amr/issues/191

amr_report (AMRFinderPlus 4.2.5 and 4.2.7 — the newest bioconda build as of
2026-09) aborts with

    "amr_report.cpp", line 2246, in HmmAlignment::Domain::Domain(...): hmmStart

as soon as several overlapping NCBIFAM HMM models hit the same protein. A single
such protein is enough to crash the run, and it is enough to have it in the
protein set: the raw hmmsearch output is well-formed and the sequence is a
perfectly normal gene. Observed trigger: the intimin/invasin-family adhesin of
E. coli O157:H7 (LEE pathogenicity island), which gets overlapping
`intimin_all` / `intimin_alpha` / `inverse_SinH` hits. E. coli K-12 lacks intimin,
which is why it annotates fine while O157:H7 does not.

bakta calls AMRFinderPlus as an expert system and turns any failure into a fatal
exception (bakta has no `--skip-amr` switch, and `--skip-cds` would drop all gene
annotation), so one gene kills the entire annotation job.

WHAT THIS DOES
--------------
Wraps `bakta.expert.amrfinder.search`. If the search raises, the CDS protein
FASTA is split in half and each half is searched again, recursively, until the
offending sequence is isolated. Hits from the surviving chunks are merged into
bakta's own CDS objects (bakta's parser does the attaching), so everything except
the pathological sequence is retained, and that sequence is reported as a warning
instead of taking the pipeline down.

The full protein set is tried first, so unaffected genomes (and unaffected
AMRFinderPlus versions, once upstream fixes it) pay no extra cost.

Sequences isolated that way are cached by content hash under
`db/amr_guard/known_bad_sequences.tsv` (override with `$WGS_AMR_GUARD_CACHE`) and
masked *before* the first search on later runs. Rediscovering an offender costs
~12 extra amrfinder calls, measured at ~5.5 min of retries on the O157:H7 sample,
and re-running is routine -- changing a rule or a resolved parameter re-annotates
every sample -- so the cache is what keeps repeat runs at a single amrfinder call.
Delete the cache after upgrading AMRFinderPlus.

REPORTING
---------
The guard prints at most four lines to stderr: the initial crash (or the masking
notice on a cache hit), the CDSs that had to be dropped, and a one-line summary.
The round-by-round trace (a 5000-CDS split needs ~12 rounds) goes to DEBUG, i.e.
it ends up in bakta's own log when you run bakta with `--verbose`. Printing a line
per round turned the terminal into noise, and an unaffected genome still prints
nothing at all.

Set `WGS_AMR_GUARD=0` to restore the upstream strict behaviour.
"""
import hashlib
import logging
import os
import shutil
import sys
from pathlib import Path

LOG = logging.getLogger("WGSPIPELINE-AMR-GUARD")
_INSTALLED = False

# Number of failing sequences tolerated before we give up on warning only.
_MAX_DROPPED_REPORTED = 5


def _warn(msg: str) -> None:
    """Log through bakta's logging setup AND straight to stderr."""
    LOG.warning(msg)
    print(f"[amr-guard] WARNING: {msg}", file=sys.stderr, flush=True)


def _info(msg: str) -> None:
    print(f"[amr-guard] {msg}", file=sys.stderr, flush=True)


def _read_fasta(path: Path):
    """Parse a FASTA file into a list of (header, sequence) records."""
    records = []
    header = None
    chunks = []
    with open(path) as fh:
        for line in fh:
            if line.startswith(">"):
                if header is not None:
                    records.append((header, "".join(chunks)))
                header = line.rstrip("\n")
                chunks = []
            elif line.strip():
                chunks.append(line.strip())
    if header is not None:
        records.append((header, "".join(chunks)))
    return records


def _write_fasta(records, path: Path) -> None:
    with open(path, "w") as fh:
        for header, seq in records:
            fh.write(header + "\n")
            for offset in range(0, len(seq), 60):
                fh.write(seq[offset:offset + 60] + "\n")


# -----------------------------------------------------------------------------
# Persistent cache of crash-triggering sequences
# -----------------------------------------------------------------------------
# Isolating the offender costs a binary split: ~12 extra amrfinder calls, measured
# at ~5.5 min of retries on the O157:H7 sample. The offender is a property of the
# *genome*, not of the run, yet every re-run pays for rediscovering it -- and
# re-running is routine, because changing a rule or a resolved parameter
# re-annotates every sample.
#
# So remember offending sequences by a hash of their sequence and mask them before
# the first search. That turns every later run back into a single amrfinder call,
# while a genome that never triggered the bug still pays nothing.
#
# Delete the cache after upgrading AMRFinderPlus: once upstream fixes ncbi/amr#191
# these sequences no longer need to be masked, and the cache would otherwise keep
# suppressing one gene's AMR annotation for no reason.
_DEFAULT_CACHE = Path("db") / "amr_guard" / "known_bad_sequences.tsv"


def _cache_path() -> Path:
    """Cache location: $WGS_AMR_GUARD_CACHE, else db/amr_guard/ under the workflow."""
    override = os.environ.get("WGS_AMR_GUARD_CACHE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / _DEFAULT_CACHE


def _digest(seq: str) -> str:
    return hashlib.sha256(seq.encode()).hexdigest()


def _load_cache() -> dict:
    """Return {sha256(sequence): header} for previously isolated sequences."""
    path = _cache_path()
    if not path.is_file():
        return {}
    cache = {}
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                fields = line.rstrip("\n").split("\t")
                if len(fields) >= 2:
                    cache[fields[0]] = fields[1]
    except OSError as exc:
        _info(f"could not read sequence cache {path} ({exc}); ignoring it")
        return {}
    return cache


def _save_cache(entries) -> None:
    """Append `(header, sequence)` pairs that AMRFinderPlus crashed on."""
    entries = list(entries)
    if not entries:
        return
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        with open(path, "a") as fh:
            if fresh:
                fh.write(
                    "# Sequences that crash AMRFinderPlus (ncbi/amr#191). They are masked\n"
                    "# before the first search so later runs skip rediscovering them by\n"
                    "# binary split. Delete this file after upgrading AMRFinderPlus.\n"
                    "# sha256\theader\n"
                )
            for header, seq in entries:
                fh.write(f"{_digest(seq)}\t{header}\n")
    except OSError as exc:
        _info(f"could not update sequence cache {path} ({exc})")


def _partition_cached(records, cache: dict):
    """Split FASTA records into (already cached, still to be searched)."""
    if not cache:
        return [], list(records)
    cached, remaining = [], []
    for header, seq in records:
        (cached if _digest(seq) in cache else remaining).append((header, seq))
    return cached, remaining


def _report(dropped, found_count: int, rounds) -> None:
    """Emit the summary lines the pipeline relies on; `rounds=None` means no chunking."""
    if dropped:
        shown = ", ".join(dropped[:_MAX_DROPPED_REPORTED])
        more = " ..." if len(dropped) > _MAX_DROPPED_REPORTED else ""
        _warn(
            f"AMR annotation dropped for {len(dropped)} CDS(s): {shown}{more} "
            "(upstream ncbi/amr#191; run bakta with --verbose for the round-by-round trace)"
        )
    if rounds is None:
        _info(
            "AMR search succeeded with cached crasher sequences masked: "
            f"{found_count} CDS(s) with AMR hits, {len(dropped)} dropped"
        )
    else:
        _info(
            f"chunk-wise AMR search finished: {rounds} split round(s), "
            f"{found_count} CDS(s) with AMR hits, {len(dropped)} dropped"
        )


def _run_original(orig, cdss, fasta: Path, tmp_path: Path):
    """
    Call bakta's original search on `fasta`.

    bakta's implementation does `tmp_path/'amrfinderplus'` with `mkdir()` (no
    exist_ok), so the directory has to go before every call.
    """
    shutil.rmtree(tmp_path / "amrfinderplus", ignore_errors=True)
    return orig(cdss, fasta)


def _search_with_split(orig, cdss, work_fasta: Path, records, tmp_path: Path, stats: dict):
    """
    Search `records`; on failure split in half and recurse.

    Returns (set_of_cds_ids_with_hits, list_of_dropped_headers).

    `stats['rounds']` counts the subsets that had to be re-tried. The caller reports
    that total in one line at the end, instead of one stderr line per round.
    / `stats['rounds']` 只累加轮数，最后由调用方汇总成一行；逐轮细节降到 DEBUG。
    """
    _write_fasta(records, work_fasta)
    try:
        found = _run_original(orig, cdss, work_fasta, tmp_path)
        return set(found or ()), []
    except Exception as exc:  # noqa: BLE001 - swallowing the upstream crash is the point
        if len(records) <= 1:
            cds_id = records[0][0].split()[0]
            LOG.debug("AMRFinderPlus keeps crashing on a single CDS (%s): %s", cds_id, exc)
            return set(), [cds_id]

        stats["rounds"] += 1
        LOG.debug(
            "AMR search failed on %i CDSs (%s); splitting in half and retrying",
            len(records),
            type(exc).__name__,
        )
        middle = len(records) // 2
        found, dropped = set(), []
        for part in (records[:middle], records[middle:]):
            sub_found, sub_dropped = _search_with_split(
                orig, cdss, work_fasta, part, tmp_path, stats
            )
            found |= sub_found
            dropped += sub_dropped
        return found, dropped


def _install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    if os.environ.get("WGS_AMR_GUARD", "1") == "0":
        _info("guard disabled via WGS_AMR_GUARD=0 (upstream strict behaviour)")
        return False

    try:
        import bakta.config as cfg
        import bakta.expert.amrfinder as amr
    except Exception as exc:  # pragma: no cover - diagnostics only
        _warn(f"could not install guard ({exc}); continuing without it")
        return False

    original = amr.search
    if getattr(original, "_wgs_chunk_guard", False):
        _INSTALLED = True
        return True

    def guarded_search(cdss, cds_fasta_path):
        fasta = Path(cds_fasta_path)
        tmp_path = Path(cfg.tmp_path)
        work_fasta = fasta.parent / "cds.expert.chunk.faa"

        records = _read_fasta(fasta)
        cached, remaining = _partition_cached(records, _load_cache())
        cached_ids = [header.split()[0] for header, _ in cached]

        if not cached:
            try:
                # Happy path: no extra cost for genomes that do not trigger the bug.
                return _run_original(original, cdss, fasta, tmp_path)
            except Exception as exc:  # noqa: BLE001
                _warn(
                    "AMRFinderPlus crashed on the full CDS set "
                    f"({type(exc).__name__}: {exc}); retrying chunk-wise (ncbi/amr#191)"
                )
        else:
            # Sequences isolated by an earlier run: mask them before the first
            # search instead of paying again for the binary split that found them.
            _info(
                f"masking {len(cached)} sequence(s) cached as AMRFinderPlus crashers "
                "(ncbi/amr#191) before the first search"
            )
            _write_fasta(remaining, work_fasta)
            if not remaining:
                _report(cached_ids, 0, None)
                return set()
            try:
                found = set(_run_original(original, cdss, work_fasta, tmp_path) or ())
                _report(cached_ids, len(found), None)
                return found
            except Exception as exc:  # noqa: BLE001
                _warn(
                    "AMRFinderPlus crashed even with the cached sequences masked "
                    f"({type(exc).__name__}: {exc}); retrying chunk-wise (ncbi/amr#191)"
                )

        by_id = {header.split()[0]: (header, seq) for header, seq in records}
        stats = {"rounds": 0}
        found, newly_dropped = _search_with_split(
            original, cdss, work_fasta, remaining, tmp_path, stats
        )
        # Remember the newly isolated sequences so the next run can mask them.
        _save_cache([by_id[i] for i in newly_dropped if i in by_id])
        _report(cached_ids + newly_dropped, len(found), stats["rounds"])
        return found

    guarded_search._wgs_chunk_guard = True
    amr.search = guarded_search
    _INSTALLED = True
    # 装载信息只走 DEBUG（`bakta --verbose` 才看得到）。
    #
    # 这里曾经写的是 `if multiprocessing.parent_process() is None: 打到 stderr`，
    # 想只让主进程说话；但 fork / forkserver 起的子进程里 parent_process() 同样返回
    # None，于是每个 worker 都往 stderr 刷了一行 —— 实测 SRR10194530 的 stderr 里多出 4 行
    # 重复的 "chunk guard installed"。既然子进程本来就不会执行 main()（不会重复注释），
    # 干脆彻底静音，别再做不可靠的父子进程判断。
    # / The "installed" line used to be printed to stderr from the process where
    # `multiprocessing.parent_process() is None`. That check is unreliable: fork and
    # forkserver children also report None, which leaked 4 duplicate lines into the log.
    # Workers never run main() anyway, so stay quiet and log at DEBUG only.
    LOG.debug("AMRFinderPlus chunk guard installed")
    return True


_install()
