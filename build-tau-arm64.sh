#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

IMAGE_TAG=${1:-${TAG:-tau:arm64-tui-latest}}
TARGET_PLATFORM=${TARGET_PLATFORM:-linux/arm64}
IMAGE_ARCHIVE=${IMAGE_ARCHIVE:-dist/tau-arm64-tui.tar}
BUILD_RETRIES=${BUILD_RETRIES:-3}
DRY_RUN=${DRY_RUN:-0}

run() {
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
    if [[ "$DRY_RUN" == "1" ]]; then
        return 0
    fi
    "$@"
}

for required_file in Dockerfile pyproject.toml uv.lock README.md LICENSE; do
    if [[ ! -f "$required_file" ]]; then
        echo "error: required build input is missing: $required_file" >&2
        exit 1
    fi
done

if ! [[ "$BUILD_RETRIES" =~ ^[1-9][0-9]*$ ]]; then
    echo "error: BUILD_RETRIES must be a positive integer" >&2
    exit 1
fi

archive_dir=$(dirname -- "$IMAGE_ARCHIVE")
checksum_path="${IMAGE_ARCHIVE}.sha256"

echo "==> Tau image build"
echo "platform: $TARGET_PLATFORM"
echo "tag:      $IMAGE_TAG"
echo "archive:  $IMAGE_ARCHIVE"

if [[ "$DRY_RUN" != "1" ]]; then
    buildx_output=$(docker buildx ls 2>&1) || {
        echo "$buildx_output" >&2
        echo "error: docker buildx is unavailable" >&2
        exit 1
    }
    if ! grep -q "$TARGET_PLATFORM" <<<"$buildx_output"; then
        echo "error: the active buildx builder does not advertise $TARGET_PLATFORM" >&2
        exit 1
    fi
fi

attempt=1
while true; do
    if run docker buildx build \
        --platform "$TARGET_PLATFORM" \
        --progress=plain \
        --load \
        -t "$IMAGE_TAG" \
        -f Dockerfile \
        .; then
        break
    fi
    if ((attempt >= BUILD_RETRIES)); then
        echo "error: image build failed after $attempt attempts" >&2
        exit 1
    fi
    echo "build failed; retrying ($attempt/$BUILD_RETRIES)" >&2
    sleep 5
    ((attempt += 1))
done

run docker image inspect "$IMAGE_TAG" --format '{{.Id}} {{.Os}}/{{.Architecture}} {{.Size}} bytes'

if [[ "$DRY_RUN" != "1" ]]; then
    actual_platform=$(docker image inspect "$IMAGE_TAG" --format '{{.Os}}/{{.Architecture}}')
    if [[ "$actual_platform" != "$TARGET_PLATFORM" ]]; then
        echo "error: built image platform is $actual_platform, expected $TARGET_PLATFORM" >&2
        exit 1
    fi
fi

echo "==> Smoke tests"
run docker run --rm --platform "$TARGET_PLATFORM" "$IMAGE_TAG" --help
run docker run --rm --platform "$TARGET_PLATFORM" "$IMAGE_TAG" --print --help

echo "==> Export image"
run mkdir -p "$archive_dir"
temporary_archive="${IMAGE_ARCHIVE}.tmp.$$"
if [[ "$DRY_RUN" == "1" ]]; then
    run docker save --output "$temporary_archive" "$IMAGE_TAG"
    run mv "$temporary_archive" "$IMAGE_ARCHIVE"
    printf '+ sha256sum %q > %q\n' "$IMAGE_ARCHIVE" "$checksum_path"
else
    trap 'rm -f -- "$temporary_archive"' EXIT
    run docker save --output "$temporary_archive" "$IMAGE_TAG"
    run mv "$temporary_archive" "$IMAGE_ARCHIVE"
    sha256sum "$IMAGE_ARCHIVE" >"$checksum_path"
    trap - EXIT
fi

echo "done: $IMAGE_ARCHIVE"
echo "done: $checksum_path"
