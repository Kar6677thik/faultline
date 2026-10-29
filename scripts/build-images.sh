#!/usr/bin/env bash
# Build Faultline's local images and pull the pinned dependency images.
# Usage (from the repository root, inside Linux or WSL2): scripts/build-images.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# Pinned dependency digests come from the engine itself, so they cannot drift from it.
read -r postgres proxy < <(
  uv run --frozen python -c \
    'from faultline.environment import POSTGRES_IMAGE, PROXY_IMAGE; print(POSTGRES_IMAGE, PROXY_IMAGE)'
)

echo "Pulling pinned dependencies" >&2
docker pull --quiet "$postgres"
docker pull --quiet "$proxy"

echo "Building faultline-runtime:0.1.0 (order application, worker, observer)" >&2
docker build --quiet -t faultline-runtime:0.1.0 .

echo "Building faultline-transfers:0.1.0 (transfer application)" >&2
docker build --quiet -t faultline-transfers:0.1.0 examples/transfers

docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' \
  | grep -E '^(faultline-runtime|faultline-transfers|postgres|ghcr.io/shopify/toxiproxy)' >&2
