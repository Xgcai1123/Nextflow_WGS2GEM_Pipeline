# bin/task_env.sh — write Snakemake-format benchmark TSVs so that
# scripts/multiqc_extras.py keeps working unchanged.
# Requires $BENCH_DIR in the environment (set by the task before sourcing).

bench_start() { _bench_t0=$(date +%s); }

bench_finish() {
    step="$1"; sample="$2"
    t1=$(date +%s); secs=$((t1 - _bench_t0))
    hms=$(awk -v s="$secs" 'BEGIN { printf "%d:%02d:%02d", int(s/3600), int(s%3600/60), s%60 }')
    if [ -n "$sample" ]; then
        out="$BENCH_DIR/$step/$sample.tsv"
    else
        out="$BENCH_DIR/$step.tsv"          # pipeline-level row (e.g. multiqc)
    fi
    mkdir -p "$(dirname "$out")"
    printf 's\th:m:s\n%d\t%s\n' "$secs" "$hms" > "$out"
}
