#!/bin/sh
set -eu

usage() {
    echo "Usage: $0 [all|build|check|export] [--out-dir DIR]" >&2
    exit 2
}

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
IMAGE=${ZONT_TEST_IMAGE:-zont-analyzer:test-local}
COMMAND=all
OUT_DIR=

while [ "$#" -gt 0 ]; do
    case "$1" in
        all|build|check|export)
            COMMAND=$1
            ;;
        --out-dir)
            [ "$#" -ge 2 ] || usage
            OUT_DIR=$2
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            usage
            ;;
    esac
    shift
done

build_image() {
    docker build --target test --tag "$IMAGE" "$ROOT"
}

run_in_image() {
    docker run --rm \
        --network none \
        --user "$(id -u):$(id -g)" \
        --workdir /workspace \
        --mount "type=bind,src=$ROOT,dst=/workspace,readonly" \
        --tmpfs /tmp:rw,exec,nosuid,nodev,size=1g \
        --entrypoint "" \
        "$IMAGE" "$@"
}

run_export() {
    docker run --rm \
        --network none \
        --user "$(id -u):$(id -g)" \
        --workdir /workspace \
        --mount "type=bind,src=$ROOT,dst=/workspace,readonly" \
        --mount "type=bind,src=$OUT_DIR,dst=/output" \
        --tmpfs /tmp:rw,exec,nosuid,nodev,size=1g \
        --entrypoint "" \
        "$IMAGE" python -m build --no-isolation --outdir /output
}

build_package() {
    if [ -n "$OUT_DIR" ]; then
        run_export
    else
        run_in_image sh -c 'mkdir -p /tmp/package && python -m build --no-isolation --outdir /tmp/package'
    fi
}

check() {
    run_in_image ruff check --no-cache /workspace/src /workspace/tests
    run_in_image mypy --cache-dir /tmp/mypy /workspace/src/zont_analyzer
    run_in_image pytest -ra -p no:cacheprovider
    build_package
}

validate_output() {
    case "$OUT_DIR" in
        /*) ;;
        *) echo "--out-dir must be an absolute path" >&2; exit 2 ;;
    esac
    OUT_DIR=$(realpath -m -- "$OUT_DIR")
    case "$OUT_DIR" in
        "$ROOT"|"$ROOT"/*)
            echo "--out-dir must be outside the repository" >&2
            exit 2
            ;;
    esac
    mkdir -p "$OUT_DIR"
}

if [ -n "$OUT_DIR" ]; then validate_output; fi
if [ "$COMMAND" = export ] && [ -z "$OUT_DIR" ]; then usage; fi

case "$COMMAND" in
    all)
        build_image
        check
        ;;
    build)
        build_image
        ;;
    check)
        build_image
        check
        ;;
    export)
        build_image
        run_export
        ;;
esac
