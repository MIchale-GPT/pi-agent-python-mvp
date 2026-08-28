#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
IMAGE_TAG=${IMAGE_TAG:-tau:arm64-tui-latest}
CONTAINER_NAME=${CONTAINER_NAME:-tau}
SAG_NETWORK=${SAG_NETWORK:-sag_default}
TAU_ENV_FILE=${TAU_ENV_FILE:-$SCRIPT_DIR/tau.env.production}
TAU_HOME_DIR=${TAU_HOME_DIR:-$HOME/.tau}
TAU_STATE_DIR=${TAU_STATE_DIR:-$SCRIPT_DIR/tau-runtime/.tau}
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

if [[ ! -f "$TAU_ENV_FILE" ]]; then
    echo "error: environment file not found: $TAU_ENV_FILE" >&2
    exit 1
fi
if [[ ! -d "$TAU_HOME_DIR" ]]; then
    echo "error: Tau config directory not found: $TAU_HOME_DIR" >&2
    exit 1
fi
if ! grep -Eq '^[[:space:]]*TAU_SAG_PLANNING_MODE=agent[[:space:]]*$' "$TAU_ENV_FILE"; then
    echo "error: $TAU_ENV_FILE must set TAU_SAG_PLANNING_MODE=agent" >&2
    exit 1
fi

if [[ "$DRY_RUN" != "1" ]]; then
    docker image inspect "$IMAGE_TAG" >/dev/null
    docker network inspect "$SAG_NETWORK" >/dev/null
fi

run mkdir -p "$TAU_STATE_DIR"

docker_args=(
    docker run -d
    --name "$CONTAINER_NAME"
    --network "$SAG_NETWORK"
    --add-host host.docker.internal:host-gateway
    --user root
    --env-file "$TAU_ENV_FILE"
    --mount "type=bind,src=$TAU_STATE_DIR,dst=/root/.tau"
    --restart unless-stopped
    --entrypoint /bin/sleep
)

for config_name in credentials.json providers.json dataquery.json catalog.toml; do
    config_path="$TAU_HOME_DIR/$config_name"
    if [[ -f "$config_path" ]]; then
        docker_args+=(
            --mount "type=bind,src=$config_path,dst=/root/.tau/$config_name,readonly"
        )
    fi
done

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    run docker rm -f "$CONTAINER_NAME"
fi

docker_args+=("$IMAGE_TAG" infinity)
run "${docker_args[@]}"
run docker exec "$CONTAINER_NAME" tau --help

echo "Tau is running without published ports."
echo "TUI: docker exec -it $CONTAINER_NAME tau"
echo "Print: docker exec $CONTAINER_NAME tau --print --mode json \"your question\""
