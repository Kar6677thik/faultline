#!/usr/bin/env bash
# Reproduce the M4 release evidence in serial chunks (each well under two hours here).
# Usage: scripts/release-evidence.sh STAGE OUTPUT_DIR
#   STAGE: defects | correct-a | correct-b | measure | integration
set -euo pipefail
cd "$(dirname "$0")/.."
stage=$1
out=$2
mkdir -p "$out"
calibrate() { uv run --frozen python -m faultline.calibration "$@"; }
case "$stage" in
  defects)
    calibrate examples/orders/corpus.json --only defects --trials 20 --output "$out/orders-defects" || true
    calibrate examples/transfers/corpus.json --trials 20 --output "$out/transfers" || true
    ;;
  correct-a)
    calibrate examples/orders/corpus.json --only correct --variant correct --trials 20 \
      --output "$out/orders-correct-a" || true
    ;;
  correct-b)
    calibrate examples/orders/corpus.json --only correct --variant correct_b --trials 20 \
      --output "$out/orders-correct-b" || true
    ;;
  measure)
    uv run --frozen python -m faultline.measure examples/orders/lost-response.yaml \
      --variant correct --runs 20 --output "$out/measure" || true
    ;;
  integration)
    FAULTLINE_INTEGRATION=1 uv run --frozen pytest -q -m integration -p no:cacheprovider \
      --basetemp="$out/integration" --durations=5 || true
    ;;
  *)
    echo "unknown stage: $stage" >&2
    exit 2
    ;;
esac
# Calibration/measurement exit codes are recorded in their JSON; never hide them here.
echo "stage $stage finished at $(date -u +%FT%TZ)"
