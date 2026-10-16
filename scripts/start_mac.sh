#!/usr/bin/env bash
# Start FinAlly in Docker (macOS/Linux). Safe to run repeatedly.
# Usage: scripts/start_mac.sh [--build] [--no-open]
set -euo pipefail

CONTAINER_NAME="finally"
IMAGE_NAME="finally"
VOLUME_NAME="finally-data"
PORT=8000
URL="http://localhost:$PORT"

BUILD=false
OPEN_BROWSER=true
for arg in "$@"; do
    case "$arg" in
        --build) BUILD=true ;;
        --no-open) OPEN_BROWSER=false ;;
        -h|--help)
            echo "Usage: $0 [--build] [--no-open]"
            echo "  --build    Rebuild the image (and restart the container if running)"
            echo "  --no-open  Don't open the browser"
            exit 0
            ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 1 ;;
    esac
done

cd "$(dirname "$0")/.."

if ! docker info &>/dev/null; then
    echo "Error: Docker is not running. Start Docker and try again." >&2
    exit 1
fi

# Build if image doesn't exist or --build flag passed
if $BUILD || [[ -z "$(docker images -q "$IMAGE_NAME")" ]]; then
    echo "Building image..."
    docker build -t "$IMAGE_NAME" .
fi

# Already running and nothing to rebuild: just report it (idempotent)
if [[ -n "$(docker ps -q --filter "name=^${CONTAINER_NAME}\$")" ]] && ! $BUILD; then
    echo "FinAlly is already running at $URL"
    exit 0
fi

# Replace a stopped container, or a running one after a rebuild
docker rm -f "$CONTAINER_NAME" &>/dev/null || true

ENV_ARGS=()
if [[ -f .env ]]; then
    ENV_ARGS=(--env-file .env)
else
    echo "Warning: .env not found; starting without it (AI chat needs OPENROUTER_API_KEY)." >&2
    echo "         Create one with: cp .env.example .env" >&2
fi

docker run -d \
    --name "$CONTAINER_NAME" \
    -p "$PORT:8000" \
    -v "$VOLUME_NAME:/app/db" \
    ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} \
    "$IMAGE_NAME" >/dev/null

# Wait for the server to come up before pointing the browser at it
echo "Waiting for FinAlly to start..."
healthy=false
command -v curl &>/dev/null || echo "(curl not found; not waiting for health check)"
for _ in $(command -v curl &>/dev/null && seq 1 60); do
    if curl -sf "$URL/api/health" &>/dev/null; then
        healthy=true
        break
    fi
    if [[ -z "$(docker ps -q --filter "name=^${CONTAINER_NAME}\$")" ]]; then
        echo "Error: container exited. Logs:" >&2
        docker logs "$CONTAINER_NAME" >&2 || true
        exit 1
    fi
    sleep 2
done

if $healthy; then
    echo "FinAlly running at $URL"
else
    echo "FinAlly is still starting at $URL (check: docker logs $CONTAINER_NAME)"
fi

if $OPEN_BROWSER; then
    if command -v open &>/dev/null; then
        open "$URL"
    elif command -v xdg-open &>/dev/null; then
        xdg-open "$URL" &>/dev/null || true
    fi
fi
