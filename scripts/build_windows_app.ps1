param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = "D:\piton\python.exe",
    [switch]$InstallPyInstaller
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path $ProjectRoot).Path
Set-Location $ProjectRoot

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python not found: $PythonExe"
}

& $PythonExe -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    if (-not $InstallPyInstaller) {
        throw "PyInstaller is not installed. Re-run with -InstallPyInstaller."
    }
    & $PythonExe -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install PyInstaller."
    }
}

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --noconsole `
    --name "3pips" `
    "src\windows_app_launcher.py"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

Write-Output "Built: $(Join-Path $ProjectRoot 'dist\3pips.exe')"
