# Stop FinAlly (Windows). Keeps the finally-data volume. Safe to run repeatedly.
$ErrorActionPreference = "Stop"

$ContainerName = "finally"

# Under Windows PowerShell 5.1, redirected native stderr + ErrorActionPreference=Stop
# becomes a terminating error, so probe with Continue and avoid stderr elsewhere.
$ErrorActionPreference = "Continue"
docker version --format "{{.Server.Version}}" *> $null
$dockerUp = $LASTEXITCODE -eq 0
$ErrorActionPreference = "Stop"
if (-not $dockerUp) {
    Write-Host "Docker is not running; nothing to stop."
    exit 0
}

if (-not (docker ps -aq --filter "name=^$ContainerName`$")) {
    Write-Host "FinAlly is not running."
    exit 0
}

# Graceful stop first so SQLite can close cleanly, then remove the container
docker stop $ContainerName | Out-Null
docker rm -f $ContainerName | Out-Null

Write-Host "FinAlly stopped. Data kept in the finally-data volume."
