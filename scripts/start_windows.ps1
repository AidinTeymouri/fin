# Start FinAlly in Docker (Windows). Safe to run repeatedly.
# Usage: scripts\start_windows.ps1 [-Build] [-NoOpen]   (--build / --no-open also accepted)
param(
    [switch]$Build,
    [switch]$NoOpen,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"

$ContainerName = "finally"
$ImageName = "finally"
$VolumeName = "finally-data"
$Port = 8000
$Url = "http://localhost:$Port"

foreach ($arg in $Rest) {
    switch ($arg) {
        "--build" { $Build = $true }
        "--no-open" { $NoOpen = $true }
        default { Write-Error "Unknown option: $arg (use -Build / -NoOpen)" }
    }
}

Set-Location "$PSScriptRoot\.."

# Under Windows PowerShell 5.1, redirected native stderr + ErrorActionPreference=Stop
# becomes a terminating error, so probe with Continue and avoid stderr elsewhere.
$ErrorActionPreference = "Continue"
docker version --format "{{.Server.Version}}" *> $null
$dockerUp = $LASTEXITCODE -eq 0
$ErrorActionPreference = "Stop"
if (-not $dockerUp) {
    Write-Error "Docker is not running. Start Docker Desktop and try again."
}

function Test-ContainerRunning {
    return [bool](docker ps -q --filter "name=^$ContainerName`$")
}

# Build if image doesn't exist or -Build flag passed
if ($Build -or -not (docker images -q $ImageName)) {
    Write-Host "Building image..."
    docker build -t $ImageName .
    if ($LASTEXITCODE -ne 0) { Write-Error "docker build failed" }
}

# Already running and nothing to rebuild: just report it (idempotent)
if ((Test-ContainerRunning) -and -not $Build) {
    Write-Host "FinAlly is already running at $Url"
    exit 0
}

# Replace a stopped container, or a running one after a rebuild
if (docker ps -aq --filter "name=^$ContainerName`$") {
    docker rm -f $ContainerName | Out-Null
}

$envArgs = @()
if (Test-Path ".env") {
    $envArgs = @("--env-file", ".env")
} else {
    Write-Warning ".env not found; starting without it (AI chat needs OPENROUTER_API_KEY)."
    Write-Warning "Create one with: Copy-Item .env.example .env"
}

docker run -d `
    --name $ContainerName `
    -p "${Port}:8000" `
    -v "${VolumeName}:/app/db" `
    @envArgs `
    $ImageName | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "docker run failed" }

# Wait for the server to come up before pointing the browser at it
Write-Host "Waiting for FinAlly to start..."
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
        $healthy = $true
        break
    } catch {
        if (-not (Test-ContainerRunning)) {
            Write-Host "Container exited. Logs:"
            docker logs $ContainerName
            Write-Error "FinAlly failed to start"
        }
        Start-Sleep -Seconds 2
    }
}

if ($healthy) {
    Write-Host "FinAlly running at $Url"
} else {
    Write-Host "FinAlly is still starting at $Url (check: docker logs $ContainerName)"
}

if (-not $NoOpen) {
    Start-Process $Url
}
