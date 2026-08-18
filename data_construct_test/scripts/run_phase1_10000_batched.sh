#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="/home/roge/miniconda3/envs/my_new_env/bin/python"
OUT="$ROOT/data_construct_test/outputs/phase1_10000_complex_batches"
mkdir -p "$OUT"
cd "$ROOT"

for batch in $(seq 1 20); do
  seed=$((2026081500 + batch))
  echo "batch $batch/20 seed=$seed"
  "$PY" data_construct_test/scripts/run_phase1_cfg_convergence_benchmark.py \
    --generated-cases 500 --web-cases 0 --batch-size 100 \
    --skip-fragment-stratum --seed "$seed" > "$OUT/batch_${batch}.log"
  cp data_construct_test/outputs/phase1_cfg_convergence_report.json "$OUT/batch_${batch}.json"
done

echo "completed 10000/10000"
