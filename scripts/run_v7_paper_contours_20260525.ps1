param(
    [ValidateSet("all", "main", "dashboard")]
    [string]$Mode = "all",
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = "",
    [int]$DashboardPort = 8767
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

function Write-RunLog {
    param([string]$Message)
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    $line | Tee-Object -FilePath $script:LogPath -Append
}

function Stop-MatchingPython {
    param([string]$Needle)
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*$Needle*" }
    foreach ($p in $procs) {
        Write-RunLog "Stopping pid=$($p.ProcessId) cmd=$($p.CommandLine)"
        Stop-Process -Id $p.ProcessId -Force
    }
}

function Start-Portfolio {
    param(
        [string]$Name,
        [string[]]$Secids
    )
    $stdout = Join-Path $script:RunDir "${Name}_multi_paper.log"
    $stderr = Join-Path $script:RunDir "${Name}_multi_paper.err.log"
    $args = @(
        "src\multi_futures_paper.py",
        "--secids"
    ) + $Secids + @(
        "--runtime-sec", "86400",
        "--report-sec", "600",
        "--seed-minutes", "240",
        "--orderbook-depth", "10",
        "--profiles", "reports\futures_scalp_profiles_v7_paper_20260525.csv",
        "--paper-capital", "800000",
        "--max-total-margin-pct", "0.80",
        "--max-position-margin-pct", "0.20",
        "--stop-limit-emergency-ticks", "2",
        "--actual-exit-model", "candle_like",
        "--stream-stale-sec", "15",
        "--fallback-poll-sec", "2",
        "--snapshot-sec", "10",
        "--log", "reports\paper_runs\v7_live_20260525\${Name}_multi_futures_paper_trades.csv",
        "--snapshot-log", "reports\paper_runs\v7_live_20260525\${Name}_live_orderbook_snapshots.csv",
        "--open-positions-log", "reports\paper_runs\v7_live_20260525\${Name}_paper_open_positions.json",
        "--instrument-specs-log", "reports\paper_runs\v7_live_20260525\${Name}_instrument_specs.csv",
        "--shadow-log", "reports\paper_runs\v7_live_20260525\${Name}_shadow_exit_models.csv",
        "--health-log", "reports\paper_runs\v7_live_20260525\${Name}_health.json"
    )
    Write-RunLog "${Name}Command=$script:Python $($args -join ' ')"
    $proc = Start-Process -FilePath $script:Python -ArgumentList $args -WorkingDirectory $script:ProjectRoot `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    $proc.Id | Set-Content -LiteralPath (Join-Path $script:RuntimeDir "v7_paper_${Name}.pid") -Encoding ASCII
    Write-RunLog "${Name}StartedPid=$($proc.Id)"
}

function Write-PortfolioConfig {
    $config = [ordered]@{
        run_name = "v7_live_20260525"
        capital_per_contour = 800000
        profiles_csv = "reports/futures_scalp_profiles_v7_paper_20260525.csv"
        portfolios = [ordered]@{
            classic_core = [ordered]@{
                capital = 800000
                tickers = @("PTZ6", "PDU6", "SiM7", "BRU6", "SVH7", "BRQ6", "PTM6", "BTN6", "BTM6", "BTK6", "PTU6", "LKU6", "BRV6")
            }
            gl_watch = [ordered]@{
                capital = 800000
                tickers = @("GLH7", "GLZ6", "GLM6")
            }
            neo = [ordered]@{
                capital = 800000
                tickers = @("AMDperpA", "COINperpA", "TSLAperpA")
            }
            tail_research = [ordered]@{
                capital = 800000
                tickers = @("BRN6", "PDM6", "MMH7", "SiH7", "MMZ6", "BMN6", "BMM6", "BMV6", "BMX6", "BMU6", "S1H7", "BRX6", "BMQ6", "S1Z6", "SVZ6")
            }
        }
    }
    $config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $script:RunDir "portfolio_config.json") -Encoding UTF8
}

$script:ProjectRoot = (Resolve-Path $ProjectRoot).Path
Set-Location $script:ProjectRoot
$script:Python = Resolve-Python -Requested $PythonExe -Root $script:ProjectRoot
$script:RuntimeDir = Join-Path $script:ProjectRoot "reports\runtime"
$script:RunDir = Join-Path $script:ProjectRoot "reports\paper_runs\v7_live_20260525"
New-Item -ItemType Directory -Force -Path $script:RuntimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $script:RunDir | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$script:LogPath = Join-Path $script:RuntimeDir "v7_paper_contours_20260525_${Mode}_${timestamp}.log"

Write-RunLog "Mode=$Mode"
Write-RunLog "ProjectRoot=$script:ProjectRoot"
Write-RunLog "Python=$script:Python"
Write-RunLog "RunDir=$script:RunDir"
Write-PortfolioConfig

if ($Mode -eq "dashboard" -or $Mode -eq "all") {
    Stop-MatchingPython "paper_dashboard.py*$DashboardPort"
    $dashArgs = @("src\paper_dashboard.py", "--port", "$DashboardPort", "--dir", "reports\paper_runs\v7_live_20260525")
    $dashOut = Join-Path $script:RuntimeDir "v7_paper_dashboard_20260525.stdout.log"
    $dashErr = Join-Path $script:RuntimeDir "v7_paper_dashboard_20260525.stderr.log"
    $dash = Start-Process -FilePath $script:Python -ArgumentList $dashArgs -WorkingDirectory $script:ProjectRoot `
        -RedirectStandardOutput $dashOut -RedirectStandardError $dashErr -WindowStyle Hidden -PassThru
    $dash.Id | Set-Content -LiteralPath (Join-Path $script:RuntimeDir "v7_paper_dashboard_20260525.pid") -Encoding ASCII
    Write-RunLog "DashboardPid=$($dash.Id)"
    Write-RunLog "DashboardUrl=http://127.0.0.1:$DashboardPort/"
}

if ($Mode -eq "main" -or $Mode -eq "all") {
    Stop-MatchingPython "multi_futures_paper.py*scalp_once_20260525"
    Stop-MatchingPython "multi_futures_paper.py*v7_live_20260525"

    foreach ($name in "classic_core", "gl_watch", "neo", "tail_research") {
        $openPath = Join-Path $script:RunDir "${name}_paper_open_positions.json"
        if (-not (Test-Path -LiteralPath $openPath)) {
            "[]" | Set-Content -LiteralPath $openPath -Encoding UTF8
        }
    }

    Start-Portfolio -Name "classic_core" -Secids @("PTZ6", "PDU6", "SiM7", "BRU6", "SVH7", "BRQ6", "PTM6", "BTN6", "BTM6", "BTK6", "PTU6", "LKU6", "BRV6")
    Start-Portfolio -Name "gl_watch" -Secids @("GLH7", "GLZ6", "GLM6")
    Start-Portfolio -Name "neo" -Secids @("AMDperpA", "COINperpA", "TSLAperpA")
    Start-Portfolio -Name "tail_research" -Secids @("BRN6", "PDM6", "MMH7", "SiH7", "MMZ6", "BMN6", "BMM6", "BMV6", "BMX6", "BMU6", "S1H7", "BRX6", "BMQ6", "S1Z6", "SVZ6")
}

