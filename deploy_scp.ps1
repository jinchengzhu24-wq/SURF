param(
    [string]$RemoteUser = "root",
    [string]$RemoteHost = "111.231.136.4",
    [string]$RemoteRoot = "/root/SURF"
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$remoteBase = "${RemoteUser}@${RemoteHost}:${RemoteRoot}"

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
    & scp $localPath $target

    if ($LASTEXITCODE -ne 0) {
        throw "scp failed for $($file.Local)"
    }
}

Write-Host ""
Write-Host "Upload complete."
Write-Host "Next, run this on the server:"
Write-Host "cd $RemoteRoot && ./deploy_scp"
