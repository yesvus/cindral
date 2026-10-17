#!/usr/bin/env bash
# Build and publish a multi-architecture broker image outside GitHub Actions.
#
#   CINDRAL_REPO=owner/name GHCR_TOKEN=... scripts/publish-image.sh
#
# The token is read from the environment and never written to a file in the
# repository. The digest it prints can be used by a deployment.
set -euo pipefail

REPO="${CINDRAL_REPO:-}"
if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "set CINDRAL_REPO to the GitHub owner/name for this image" >&2
  exit 1
fi
IMAGE="ghcr.io/${REPO}"
WORKDIR="${CINDRAL_PUBLISH_DIR:-/var/lib/cindral/publish}"
BUILDX_CACHE="${CINDRAL_BUILDX_CACHE:-/var/lib/cindral/buildx-cache}"
BUILDER="${CINDRAL_BUILDER:-cindral-publish}"

if ! command -v docker >/dev/null; then
  echo "docker is not installed on this device" >&2
  exit 1
fi
if ! docker buildx version >/dev/null 2>&1; then
  echo "docker buildx is not installed; see docs/release-path.md" >&2
  exit 1
fi
if [ -z "${GHCR_TOKEN:-}" ]; then
  echo "GHCR_TOKEN is unset; a registry credential is required to push" >&2
  exit 1
fi

mkdir -p "$WORKDIR"
cd "$WORKDIR"
if [ ! -d "$REPO/.git" ]; then
  git clone --depth 1 "git@github.com:${REPO}.git" "$REPO"
fi
git -C "$REPO" fetch --depth 1 origin main
git -C "$REPO" checkout --force FETCH_HEAD
SHA="$(git -C "$REPO" rev-parse HEAD)"
VERSION="$(tr -d 'v\n' < "$REPO/VERSION")"
# One name, built once, used to push and to read the digest back. Deriving it twice
# is how the read-back came to inspect a tag the push no longer wrote, so the
# record it prints would have named the digest of a different image.
TAG="${VERSION}-${SHA:0:8}"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# Both published names carry the commit, matching publish-broker.yml, so no name
# this script or that workflow pushes can repoint. A bare version tag looks like a
# release and answers "what is 0.12.0" with "whatever ran last"; the cluster pins
# a digest, so nothing needs the moving name.

echo "building ${IMAGE}:${VERSION} from ${SHA}"

docker buildx create --name "$BUILDER" --driver docker-container --use >/dev/null 2>&1 \
  || docker buildx use "$BUILDER"
# Both architectures are included for deployments on amd64 and arm64.
# binfmt has to be registered for the *foreign* half, not the native one: a host
# that is already arm64 emulates nothing, and installing arm64 there registers
# the emulator this machine does not need while leaving amd64 unregistered, so
# the build fails on the last step. Derived from the native architecture rather
# than hardcoded, so the script is right on either host.
NATIVE="$(docker info --format '{{.Architecture}}')"
FOREIGN="amd64"
if [ "$NATIVE" = "amd64" ]; then
  FOREIGN="arm64"
fi
echo "building for linux/amd64,linux/arm64 natively on ${NATIVE}, emulating ${FOREIGN}"
# without this registration the foreign build fails at the last step, after the
# native half has already been built and the cache written
docker run --privileged --rm tonistiigi/binfmt --install "$FOREIGN" >/dev/null 2>&1 || true
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-$(id -un)}" --password-stdin >/dev/null

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --build-arg "CINDRAL_VERSION=${VERSION}" \
  --build-arg "CINDRAL_COMMIT=${SHA}" \
  --build-arg "CINDRAL_BUILT_AT=${BUILT_AT}" \
  --cache-from "type=local,src=${BUILDX_CACHE}" \
  --cache-to "type=local,dest=${BUILDX_CACHE},mode=max" \
  --tag "${IMAGE}:${TAG}" \
  --tag "${IMAGE}:${SHA}" \
  --push \
  "$REPO"

# The authoritative record of what was pushed. The ghcr packages API needs a
# package-scoped token and `gh auth token` is refused for manifest reads, so the
# build output can be used to pin a digest in a deployment.
DIGEST="$(docker buildx imagetools inspect "${IMAGE}:${TAG}" --format '{{ .Manifest.Digest }}' 2>/dev/null || true)"
if [ -z "$DIGEST" ]; then
  echo "WARNING: could not read the pushed digest back; take it from the build output above" >&2
  exit 1
fi
echo "PUSHED ${IMAGE}@${DIGEST} tag=${TAG} version=${VERSION} commit=${SHA} built_at=${BUILT_AT}"
