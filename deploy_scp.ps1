param(
    [string]$RemoteUser = "root",
    [string]$RemoteHost = "111.231.136.4",
    [string]$RemoteRoot = "/root/SURF"
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$remoteBase = "${RemoteUser}@${RemoteHost}:${RemoteRoot}"
$scpCommand = Get-Command scp -ErrorAction SilentlyContinue

if ($scpCommand) {
    $scpPath = $scpCommand.Source
} else {
    $scpCandidates = @(
        (Join-Path $env:WINDIR "System32\OpenSSH\scp.exe"),
        (Join-Path $env:WINDIR "Sysnative\OpenSSH\scp.exe")
    )
    $scpPath = $scpCandidates | Where-Object {
        Test-Path -LiteralPath $_
    } | Select-Object -First 1
}

if (-not $scpPath -or -not (Test-Path -LiteralPath $scpPath)) {
    throw "scp was not found. Install Windows OpenSSH Client, or add scp.exe to PATH."
}

$files = @(
    @{ Local = "Backend\app.py"; Remote = "Backend/app.py" },
    @{ Local = "Frontend\index.html"; Remote = "Frontend/index.html" },
    @{ Local = "Frontend\app.js"; Remote = "Frontend/app.js" },
    @{ Local = "Frontend\styles.css"; Remote = "Frontend/styles.css" }
)

foreach ($file in $files) {
    $localPath = Join-Path $projectRoot $file.Local

    if (-not (Test-Path -LiteralPath $localPath)) {
        throw "Missing local file: $localPath"
    }

    $target = "$remoteBase/$($file.Remote)"
    Write-Host "Uploading $($file.Local) -> $target"
    & $scpPath $localPath $target

    if ($LASTEXITCODE -ne 0) {
        throw "scp failed for $($file.Local)"
    }
}

Write-Host ""
Write-Host "Upload complete."
Write-Host "Next, run this on the server:"
Write-Host "cd $RemoteRoot && ./deploy_scp"
