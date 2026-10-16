#!/usr/bin/env bash
# Stop FinAlly (macOS/Linux). Keeps the finally-data volume. Safe to run repeatedly.
set -euo pipefail

CONTAINER_NAME="finally"

if ! docker info &>/dev/null; then
    echo "Docker is not running; nothing to stop."
    exit 0
fi

if [[ -z "$(docker ps -aq --filter "name=^${CONTAINER_NAME}\$")" ]]; then
    echo "FinAlly is not running."
    exit 0
fi

# Graceful stop first so SQLite can close cleanly, then remove the container
docker stop "$CONTAINER_NAME" &>/dev/null || true
docker rm -f "$CONTAINER_NAME" &>/dev/null || true

echo "FinAlly stopped. Data kept in the finally-data volume."
