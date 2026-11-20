#!/bin/sh
# Build the sandbox tool image from this package's wheel, push to a local registry, print image.json fragment.
set -eu
REPO=${1:?usage: build_image.sh <registry/repo> [base_image]}
BASE=${2:?usage: build_image.sh <registry/repository> <base-image@sha256:digest>}
HERE=$(cd "$(dirname "$0")/.." && pwd)
CTX=$(mktemp -d)
uv build --wheel --project "$HERE" -o "$CTX" >/dev/null
W=$(cd "$CTX" && ls tender_three_stage_harness-*.whl)
cp "$HERE/image/Dockerfile" "$CTX/Dockerfile"
TAG="$REPO:$(shasum -a 256 "$CTX/$W" | cut -c1-16)"
docker build --platform linux/amd64 --build-arg BASE_IMAGE="$BASE" --build-arg WHEEL="$W" -t "$TAG" "$CTX" >&2
docker push "$TAG" >&2
DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' "$TAG")
SITE=/usr/local/lib/python3.12/site-packages/tender_harness/docx_ops
E=$(docker run --rm --platform linux/amd64 --entrypoint sha256sum "$DIGEST" $SITE/engine.py | cut -c1-64)
B=$(docker run --rm --platform linux/amd64 --entrypoint sha256sum "$DIGEST" $SITE/base.py | cut -c1-64)
printf '{"image":"%s","wheel":"%s","wheel_sha256":"%s","engine_sha256":"%s","base_sha256":"%s","src_engine_sha256":"%s","src_base_sha256":"%s"}\n' "$DIGEST" "$W" "$(shasum -a 256 "$CTX/$W"|cut -c1-64)" "$E" "$B" "$(shasum -a 256 "$HERE/src/tender_harness/docx_ops/engine.py"|cut -c1-64)" "$(shasum -a 256 "$HERE/src/tender_harness/docx_ops/base.py"|cut -c1-64)"
