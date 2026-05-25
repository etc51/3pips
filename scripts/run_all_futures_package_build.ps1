param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    param([string]$Requested, [string]$Root)
    if ($Requested) { return $Requested }
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return $python.Source }
    throw "Python was not found."
}

$ProjectRoot = (Resolve-Path $ProjectRoot).Path
Set-Location $ProjectRoot
New-Item -ItemType Directory -Force -Path "reports\runtime" | Out-Null
$py = Resolve-Python -Requested $PythonExe -Root $ProjectRoot
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$stdout = Join-Path $ProjectRoot "reports\runtime\all_futures_package_build_$stamp.stdout.log"
$stderr = Join-Path $ProjectRoot "reports\runtime\all_futures_package_build_$stamp.stderr.log"
$pidPath = Join-Path $ProjectRoot "reports\runtime\all_futures_package_build.pid"
$args = @(
    "src\build_all_futures_cloud_package.py",
    "--download",
    "--days", "365",
    "--sleep-sec", "0.08",
    "--retries", "20",
    "--min-rows", "1"
)

$proc = Start-Process -FilePath $py -ArgumentList $args -WorkingDirectory $ProjectRoot -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
$proc.Id | Set-Content -LiteralPath $pidPath -Encoding ASCII
Write-Host "Started all futures package build"
Write-Host "PID: $($proc.Id)"
Write-Host "stdout: $stdout"
Write-Host "stderr: $stderr"
Write-Host "zip target: $(Join-Path $ProjectRoot 'reports\cloud_all_futures_grid_package.zip')"
