#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
IMAGE_TAG=${IMAGE_TAG:-tau:arm64-tui-latest}
CONTAINER_NAME=${CONTAINER_NAME:-tau}
SAG_NETWORK=${SAG_NETWORK:-sag_default}
SAG_API_CONTAINER=${SAG_API_CONTAINER:-}
SAG_API_IP=${SAG_API_IP:-}
TAU_ENV_FILE=${TAU_ENV_FILE:-$SCRIPT_DIR/tau.env.production}
TAU_HOME_DIR=${TAU_HOME_DIR:-$HOME/.tau}
TAU_STATE_DIR=${TAU_STATE_DIR:-$SCRIPT_DIR/tau-runtime/.tau}
DRY_RUN=${DRY_RUN:-0}

run_unless_dry() {
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
    if [[ "$DRY_RUN" == "1" ]]; then
        return 0
    fi
    "$@"
}

resolve_sag_api_ip() {
    if [[ -n "$SAG_API_IP" ]]; then
        printf '%s\n' "$SAG_API_IP"
        return
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "192.0.2.1"
        return
    fi

    local api_container=$SAG_API_CONTAINER
    if [[ -z "$api_container" ]]; then
        local -a candidates=()
        mapfile -t candidates < <(
            docker ps \
                --filter label=com.docker.compose.project=sag \
                --filter label=com.docker.compose.service=api \
                --format '{{.ID}}'
        )
        if ((${#candidates[@]} != 1)); then
            echo "error: expected one running SAG api container; set SAG_API_CONTAINER" >&2
            return 1
        fi
        api_container=${candidates[0]}
    fi

    local network_template
    network_template="{{with index .NetworkSettings.Networks \"$SAG_NETWORK\"}}{{.IPAddress}}{{end}}"
    local api_ip
    api_ip=$(docker container inspect "$api_container" --format "$network_template")
    if [[ -z "$api_ip" ]]; then
        echo "error: SAG api container is not attached to $SAG_NETWORK" >&2
        return 1
    fi
    printf '%s\n' "$api_ip"
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

sag_api_ip=$(resolve_sag_api_ip)
if ! [[ "$sag_api_ip" =~ ^[0-9A-Fa-f:.]+$ ]]; then
    echo "error: invalid SAG API IP: $sag_api_ip" >&2
    exit 1
fi

run_unless_dry mkdir -p "$TAU_STATE_DIR"

docker_args=(
    docker run -d
    --name "$CONTAINER_NAME"
    --network host
    --add-host "api:$sag_api_ip"
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
    run_unless_dry docker rm -f "$CONTAINER_NAME"
fi

docker_args+=("$IMAGE_TAG" infinity)
run_unless_dry "${docker_args[@]}"
run_unless_dry docker exec "$CONTAINER_NAME" tau --help

echo "Tau is running without published ports."
echo "TUI: docker exec -it $CONTAINER_NAME tau"
echo "Print: docker exec $CONTAINER_NAME tau --print --mode json \"your question\""
