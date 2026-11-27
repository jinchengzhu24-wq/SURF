<#
.SYNOPSIS
    Package and deploy the unified Sokoban AI Study Platform over SSH.

.DESCRIPTION
    Builds the Vite/Preact frontend (`web/`), packages `server/`, `web/dist/`,
    `deploy/`, and `pyproject.toml`, uploads the archive using the configured
    SSH alias (`sokoban`) and dedicated key (`%USERPROFILE%\.ssh\sokoban` with
    `-o IdentitiesOnly=yes`), restarts `sokoban.service`, and verifies
    `/health` and `/ready`.

    Supports `-WhatIf` for a full dry-run without building, uploading, or
    restarting remote services.

.EXAMPLE
    .\deploy\deploy.ps1 -WhatIf

.EXAMPLE
    .\deploy\deploy.ps1 -SkipWebBuild
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$SshAlias = "sokoban",

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$IdentityFile = (Join-Path $env:USERPROFILE ".ssh\sokoban"),

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RemoteDir = "/opt/sokoban",

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$ServiceName = "sokoban.service",

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$HealthBaseUrl = "http://127.0.0.1:8000",

    [Parameter()]
    [switch]$SkipWebBuild,

    [Parameter()]
    [switch]$SkipHealthCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    Write-Host "==> $Description" -ForegroundColor Cyan
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($ArgumentList -join ' ')"
    }
}

function Copy-FilteredDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourceDir,

        [Parameter(Mandatory = $true)]
        [string]$DestinationDir,

        [string[]]$ExcludeDirNames = @("__pycache__", ".pytest_cache", ".ruff_cache", "data"),

        [string[]]$ExcludeFileExtensions = @(".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm")
    )

    New-Item -ItemType Directory -Path $DestinationDir -Force | Out-Null
    $items = Get-ChildItem -Path $SourceDir -Force
    foreach ($item in $items) {
        if ($item.PSIsContainer) {
            if ($ExcludeDirNames -contains $item.Name) {
                continue
            }
            $childDest = Join-Path $DestinationDir $item.Name
            Copy-FilteredDirectory -SourceDir $item.FullName -DestinationDir $childDest `
                -ExcludeDirNames $ExcludeDirNames -ExcludeFileExtensions $ExcludeFileExtensions
        }
        else {
            if ($ExcludeFileExtensions -contains $item.Extension) {
                continue
            }
            if ($item.Name -like ".env*") {
                continue
            }
            Copy-Item -Path $item.FullName -Destination (Join-Path $DestinationDir $item.Name) -Force
        }
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$webDir = Join-Path $repoRoot "web"
$serverDir = Join-Path $repoRoot "server"
$deployDir = Join-Path $repoRoot "deploy"
$pyprojectPath = Join-Path $repoRoot "pyproject.toml"

foreach ($tool in @("ssh", "scp", "tar")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Required CLI tool '$tool' was not found on PATH."
    }
}
if (-not $SkipWebBuild -and -not (Get-Command "npm" -ErrorAction SilentlyContinue)) {
    throw "Required CLI tool 'npm' was not found on PATH (pass -SkipWebBuild to skip)."
}

foreach ($requiredPath in @($serverDir, $deployDir, $pyprojectPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required release input is missing: $requiredPath"
    }
}

if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
    if ($WhatIfPreference) {
        Write-Warning "SSH identity file not found locally (continuing because -WhatIf is active): $IdentityFile"
    }
    else {
        throw "SSH identity file not found: $IdentityFile"
    }
}

$sshCommonArgs = @(
    "-i", $IdentityFile,
    "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new"
)

if (-not $SkipWebBuild) {
    if ($PSCmdlet.ShouldProcess($webDir, "Build all web workspaces (@sokoban/dashboard, @sokoban/cocreation, @sokoban/survey, @sokoban/webgl-bridge)")) {
        Invoke-CheckedCommand -FilePath "npm" -ArgumentList @("--prefix", $webDir, "run", "build") `
            -Description "Building web workspaces (@sokoban/dashboard, @sokoban/cocreation, @sokoban/survey, @sokoban/webgl-bridge) in $webDir"
    }
}

$webDistDir = Join-Path $webDir "dist"
if (-not $WhatIfPreference -and -not (Test-Path -LiteralPath $webDistDir -PathType Container)) {
    throw "Built frontend output directory not found: $webDistDir"
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stageRoot = Join-Path $env:TEMP "sokoban-deploy-$timestamp"
$archiveName = "sokoban-release-$timestamp.tar.gz"
$archivePath = Join-Path $env:TEMP $archiveName
$remoteArchive = "/tmp/$archiveName"

try {
    if ($PSCmdlet.ShouldProcess($archivePath, "Stage and package release bundle (server, web/dist, deploy, pyproject.toml)")) {
        Write-Host "==> Staging release payload in $stageRoot" -ForegroundColor Cyan
        New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null

        Copy-FilteredDirectory -SourceDir $serverDir -DestinationDir (Join-Path $stageRoot "server")
        Copy-FilteredDirectory -SourceDir $webDistDir -DestinationDir (Join-Path $stageRoot "web\dist") -ExcludeDirNames @()
        Copy-FilteredDirectory -SourceDir $deployDir -DestinationDir (Join-Path $stageRoot "deploy") -ExcludeDirNames @()
        Copy-Item -LiteralPath $pyprojectPath -Destination (Join-Path $stageRoot "pyproject.toml") -Force

        Invoke-CheckedCommand -FilePath "tar" -ArgumentList @("-czf", $archivePath, "-C", $stageRoot, ".") `
            -Description "Creating release tarball $archivePath"
    }

    if ($PSCmdlet.ShouldProcess("${SshAlias}:${remoteArchive}", "Upload release tarball via SCP")) {
        $scpArgs = @($sshCommonArgs) + @($archivePath, "${SshAlias}:${remoteArchive}")
        Invoke-CheckedCommand -FilePath "scp" -ArgumentList $scpArgs `
            -Description "Uploading $archiveName to $SshAlias"
    }

    $skipHealthValue = if ($SkipHealthCheck) { "1" } else { "0" }
    $remoteScript = @'
set -euo pipefail
REMOTE_DIR="__REMOTE_DIR__"
REMOTE_ARCHIVE="__REMOTE_ARCHIVE__"
SERVICE_NAME="__SERVICE_NAME__"
HEALTH_BASE_URL="__HEALTH_BASE_URL__"
SKIP_HEALTH="__SKIP_HEALTH__"

cleanup() {
    rm -f "$REMOTE_ARCHIVE"
}
trap cleanup EXIT

mkdir -p "$REMOTE_DIR" "$REMOTE_DIR/server/data"
tar -xzf "$REMOTE_ARCHIVE" -C "$REMOTE_DIR"

if [ "$(id -u)" -eq 0 ]; then
    chown -R sokoban:sokoban "$REMOTE_DIR/server" "$REMOTE_DIR/server/data" "$REMOTE_DIR/web/dist" "$REMOTE_DIR/deploy"
elif command -v sudo >/dev/null 2>&1; then
    sudo chown -R sokoban:sokoban "$REMOTE_DIR/server" "$REMOTE_DIR/server/data" "$REMOTE_DIR/web/dist" "$REMOTE_DIR/deploy"
fi

if [ -x "$REMOTE_DIR/.venv/bin/python" ]; then
    if "$REMOTE_DIR/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
        "$REMOTE_DIR/.venv/bin/python" -c "import subprocess, sys, tomllib; deps = tomllib.load(open('$REMOTE_DIR/pyproject.toml', 'rb'))['project']['dependencies']; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', *deps])"
    elif command -v uv >/dev/null 2>&1; then
        "$REMOTE_DIR/.venv/bin/python" -c "import subprocess, sys, tomllib; deps = tomllib.load(open('$REMOTE_DIR/pyproject.toml', 'rb'))['project']['dependencies']; subprocess.check_call(['uv', 'pip', 'install', '--python', sys.executable, '--quiet', *deps])"
    elif [ -x "$HOME/.local/bin/uv" ]; then
        "$REMOTE_DIR/.venv/bin/python" -c "import os, subprocess, sys, tomllib; deps = tomllib.load(open('$REMOTE_DIR/pyproject.toml', 'rb'))['project']['dependencies']; subprocess.check_call([os.path.expanduser('~/.local/bin/uv'), 'pip', 'install', '--python', sys.executable, '--quiet', *deps])"
    else
        echo "WARNING: Neither pip nor uv found for $REMOTE_DIR/.venv/bin/python; skipping dependency sync." >&2
    fi
else
    echo "WARNING: $REMOTE_DIR/.venv/bin/python not found; skipping dependency sync." >&2
fi

if command -v sudo >/dev/null 2>&1 && [ "$(id -u)" -ne 0 ]; then
    sudo systemctl restart "$SERVICE_NAME"
    sudo systemctl is-active --quiet "$SERVICE_NAME"
else
    systemctl restart "$SERVICE_NAME"
    systemctl is-active --quiet "$SERVICE_NAME"
fi

if [ "$SKIP_HEALTH" != "1" ]; then
    check_endpoint() {
        local path="$1"
        local attempt
        for attempt in $(seq 1 15); do
            if curl -fsS --max-time 5 "${HEALTH_BASE_URL}${path}" >/dev/null; then
                echo "OK ${HEALTH_BASE_URL}${path}"
                return 0
            fi
            sleep 1
        done
        echo "ERROR: ${HEALTH_BASE_URL}${path} failed health check after 15 attempts." >&2
        return 1
    }

    check_endpoint "/health"
    check_endpoint "/ready"
fi
'@

    $remoteScript = $remoteScript.Replace("__REMOTE_DIR__", $RemoteDir)
    $remoteScript = $remoteScript.Replace("__REMOTE_ARCHIVE__", $remoteArchive)
    $remoteScript = $remoteScript.Replace("__SERVICE_NAME__", $ServiceName)
    $remoteScript = $remoteScript.Replace("__HEALTH_BASE_URL__", $HealthBaseUrl.TrimEnd("/"))
    $remoteScript = $remoteScript.Replace("__SKIP_HEALTH__", $skipHealthValue)
    $remoteScript = $remoteScript -replace "`r`n", "`n"

    if ($PSCmdlet.ShouldProcess("${SshAlias}:${RemoteDir}", "Extract bundle, restart $ServiceName, and run /health + /ready checks")) {
        $sshArgs = @($sshCommonArgs) + @($SshAlias, "tr -d '\r' | bash -s")
        Write-Host "==> Deploying on $SshAlias ($RemoteDir) and restarting $ServiceName" -ForegroundColor Cyan
        $remoteScript | & ssh @sshArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Remote deployment script failed with exit code $LASTEXITCODE."
        }
        Write-Host "==> Deployment completed and verified." -ForegroundColor Green
    }
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
    }
}
