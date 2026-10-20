#!/usr/bin/env bash
# Build the multi-arch image and push it to the registry.
#   scripts/release.sh            -> tags <version> and latest
#   IMAGE=registry/x/y scripts/release.sh
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-harbor.dockerbit.com/gopro-sync/gopro-sync}"
VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
PLATFORMS="${PLATFORMS:-linux/arm64,linux/amd64}"

if ! docker buildx inspect gopro-sync-builder >/dev/null 2>&1; then
  docker buildx create --name gopro-sync-builder --driver docker-container >/dev/null
fi

docker buildx build \
  --builder gopro-sync-builder \
  --platform "$PLATFORMS" \
  --label "org.opencontainers.image.version=$VERSION" \
  --label "org.opencontainers.image.revision=$(git rev-parse --short HEAD)" \
  -t "$IMAGE:$VERSION" \
  -t "$IMAGE:latest" \
  --push .

echo "pushed $IMAGE:$VERSION and $IMAGE:latest ($PLATFORMS)"
