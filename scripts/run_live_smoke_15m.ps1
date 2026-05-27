param(
    [string]$Ticker = "BRN6",
    [ValidateSet("long", "short")]
    [string]$Direction = "long",
    [int]$Qty = 1,
    [int]$DurationSec = 900,
    [int]$StopTicks = 20,
    [int]$TrailTicks = 3,
    [double]$MaxSpreadTicks = 5,
    [switch]$RealOrders,
    [string]$AccountId = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$Python = "D:\piton\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$runtime = Join-Path $Root "reports\runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$jsonLog = Join-Path $runtime "live_smoke_${Ticker}_${stamp}.jsonl"
$stdout = Join-Path $runtime "live_smoke_${Ticker}_${stamp}.stdout.log"
$stderr = Join-Path $runtime "live_smoke_${Ticker}_${stamp}.stderr.log"

function Quote-Arg {
    param([string]$Value)
    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

$argsList = @(
    "src\tbank_live_smoke.py",
    "--ticker", $Ticker,
    "--direction", $Direction,
    "--qty", "$Qty",
    "--duration-sec", "$DurationSec",
    "--stop-ticks", "$StopTicks",
    "--trail-ticks", "$TrailTicks",
    "--max-spread-ticks", "$MaxSpreadTicks",
    "--log", $jsonLog
)
if ($AccountId) {
    $argsList += @("--account-id", $AccountId)
}
if ($RealOrders) {
    if ($env:LIVE_SMOKE_ENABLE -ne "1") {
        throw "RealOrders blocked. Set LIVE_SMOKE_ENABLE=1 explicitly for this shell."
    }
    $argsList += @("--real-orders", "--confirm-real-orders", "YES", "--confirm-margin-trade")
}

$argString = ($argsList | ForEach-Object { Quote-Arg $_ }) -join " "
$proc = Start-Process -FilePath $Python -ArgumentList $argString -WorkingDirectory $Root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden
$pidPath = Join-Path $runtime "live_smoke_${Ticker}.pid"
Set-Content -LiteralPath $pidPath -Value $proc.Id -Encoding ascii

Write-Host "Started live smoke PID=$($proc.Id)"
Write-Host "JSONL: $jsonLog"
Write-Host "stdout: $stdout"
Write-Host "stderr: $stderr"
Write-Host "RealOrders=$($RealOrders.IsPresent)"
