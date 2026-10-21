#!/usr/bin/env bash
# Build the multi-arch image and push it from this machine. Releases normally go through
# GitHub Actions (push a v<version> tag); this is for other registries or manual pushes.
#   scripts/release.sh            -> tags <version> and latest
#   IMAGE=registry/x/y scripts/release.sh
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-ghcr.io/iacob28/ferry}"
VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
PLATFORMS="${PLATFORMS:-linux/arm64,linux/amd64}"

if ! docker buildx inspect ferry-builder >/dev/null 2>&1; then
  docker buildx create --name ferry-builder --driver docker-container >/dev/null
fi

docker buildx build \
  --builder ferry-builder \
  --platform "$PLATFORMS" \
  --label "org.opencontainers.image.version=$VERSION" \
  --label "org.opencontainers.image.revision=$(git rev-parse --short HEAD)" \
  -t "$IMAGE:$VERSION" \
  -t "$IMAGE:latest" \
  --push .

echo "pushed $IMAGE:$VERSION and $IMAGE:latest ($PLATFORMS)"
