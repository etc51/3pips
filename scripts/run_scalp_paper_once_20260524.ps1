param(
    [ValidateSet("dashboard", "main")]
    [string]$Mode,
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

function Write-RunLog {
    param([string]$Message)
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    $line | Tee-Object -FilePath $script:LogPath -Append
}

function Stop-Existing {
    param([string]$Needle)
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*$Needle*" -and ($_.CommandLine -like "*8766*" -or $_.CommandLine -like "*scalp_once_20260524*") }
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force
    }
}

$ProjectRoot = (Resolve-Path $ProjectRoot).Path
Set-Location $ProjectRoot
New-Item -ItemType Directory -Force -Path "reports\runtime" | Out-Null
New-Item -ItemType Directory -Force -Path "reports\paper_runs" | Out-Null
$RunDir = Join-Path $ProjectRoot "reports\paper_runs\scalp_once_20260524"
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$script:LogPath = Join-Path $ProjectRoot "reports\runtime\scalp_paper_once_20260524_${Mode}_${timestamp}.log"
$py = Resolve-Python -Requested $PythonExe -Root $ProjectRoot

try {
    Write-RunLog "Mode=$Mode"
    Write-RunLog "ProjectRoot=$ProjectRoot"
    Write-RunLog "Python=$py"

    if ($Mode -eq "dashboard") {
        Stop-Existing "paper_dashboard.py"
        $args = @("src\paper_dashboard.py", "--port", "8766", "--dir", "reports\paper_runs\scalp_once_20260524")
        $out = Join-Path $ProjectRoot "reports\runtime\paper_dashboard_20260524.stdout.log"
        $err = Join-Path $ProjectRoot "reports\runtime\paper_dashboard_20260524.stderr.log"
        $proc = Start-Process -FilePath $py -ArgumentList $args -WorkingDirectory $ProjectRoot -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
        Write-RunLog "DashboardPid=$($proc.Id)"
        Write-RunLog "DashboardUrl=http://127.0.0.1:8766/"
        exit 0
    }

    Remove-Item -LiteralPath (Join-Path $RunDir "multi_futures_paper_trades.csv") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "live_orderbook_snapshots.csv") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "multi_paper_live_v2.log") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "multi_paper_live_v2.err.log") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "weak_multi_futures_paper_trades.csv") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "weak_live_orderbook_snapshots.csv") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "weak_multi_paper_live_v2.log") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "weak_multi_paper_live_v2.err.log") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "rejected_multi_futures_paper_trades.csv") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "rejected_live_orderbook_snapshots.csv") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "rejected_multi_paper_live_v2.log") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "rejected_multi_paper_live_v2.err.log") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "neo_multi_futures_paper_trades.csv") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "neo_live_orderbook_snapshots.csv") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "neo_multi_paper_live_v2.log") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $RunDir "neo_multi_paper_live_v2.err.log") -Force -ErrorAction SilentlyContinue
    "[]" | Set-Content -LiteralPath (Join-Path $RunDir "paper_open_positions.json") -Encoding UTF8
    "[]" | Set-Content -LiteralPath (Join-Path $RunDir "weak_paper_open_positions.json") -Encoding UTF8
    "[]" | Set-Content -LiteralPath (Join-Path $RunDir "rejected_paper_open_positions.json") -Encoding UTF8
    "[]" | Set-Content -LiteralPath (Join-Path $RunDir "neo_paper_open_positions.json") -Encoding UTF8
    Write-RunLog "RunDir=$RunDir"

    $stdout = Join-Path $ProjectRoot "reports\multi_paper_live_v2.log"
    $stderr = Join-Path $ProjectRoot "reports\multi_paper_live_v2.err.log"
    $stdout = Join-Path $RunDir "multi_paper_live_v2.log"
    $stderr = Join-Path $RunDir "multi_paper_live_v2.err.log"
    $strongArgs = @(
        "src\multi_futures_paper.py",
        "--secids", "LKM6", "PTU6",
        "--runtime-sec", "32100",
        "--report-sec", "600",
        "--seed-minutes", "240",
        "--orderbook-depth", "10",
        "--profiles", "reports\futures_scalp_profiles_live_20260524.csv",
        "--paper-capital", "200000",
        "--max-total-margin-pct", "0.80",
        "--max-position-margin-pct", "0.20",
        "--no-trade-before", "10:10",
        "--no-new-after", "18:45",
        "--snapshot-sec", "10",
        "--log", "reports\paper_runs\scalp_once_20260524\multi_futures_paper_trades.csv",
        "--snapshot-log", "reports\paper_runs\scalp_once_20260524\live_orderbook_snapshots.csv",
        "--open-positions-log", "reports\paper_runs\scalp_once_20260524\paper_open_positions.json"
    )

    $weakStdout = Join-Path $RunDir "weak_multi_paper_live_v2.log"
    $weakStderr = Join-Path $RunDir "weak_multi_paper_live_v2.err.log"
    $weakArgs = @(
        "src\multi_futures_paper.py",
        "--secids", "S1M6", "GDM6",
        "--runtime-sec", "32100",
        "--report-sec", "600",
        "--seed-minutes", "240",
        "--orderbook-depth", "10",
        "--profiles", "reports\futures_scalp_profiles_live_20260524.csv",
        "--paper-capital", "200000",
        "--max-total-margin-pct", "0.80",
        "--max-position-margin-pct", "0.20",
        "--no-trade-before", "10:10",
        "--no-new-after", "18:45",
        "--snapshot-sec", "10",
        "--log", "reports\paper_runs\scalp_once_20260524\weak_multi_futures_paper_trades.csv",
        "--snapshot-log", "reports\paper_runs\scalp_once_20260524\weak_live_orderbook_snapshots.csv",
        "--open-positions-log", "reports\paper_runs\scalp_once_20260524\weak_paper_open_positions.json"
    )

    $rejectedStdout = Join-Path $RunDir "rejected_multi_paper_live_v2.log"
    $rejectedStderr = Join-Path $RunDir "rejected_multi_paper_live_v2.err.log"
    $rejectedArgs = @(
        "src\multi_futures_paper.py",
        "--secids", "CEM6", "FSM6", "S1U6",
        "--runtime-sec", "32100",
        "--report-sec", "600",
        "--seed-minutes", "240",
        "--orderbook-depth", "10",
        "--profiles", "reports\futures_scalp_profiles_live_20260524.csv",
        "--paper-capital", "200000",
        "--max-total-margin-pct", "0.80",
        "--max-position-margin-pct", "0.20",
        "--no-trade-before", "10:10",
        "--no-new-after", "18:45",
        "--snapshot-sec", "10",
        "--log", "reports\paper_runs\scalp_once_20260524\rejected_multi_futures_paper_trades.csv",
        "--snapshot-log", "reports\paper_runs\scalp_once_20260524\rejected_live_orderbook_snapshots.csv",
        "--open-positions-log", "reports\paper_runs\scalp_once_20260524\rejected_paper_open_positions.json"
    )

    $neoStdout = Join-Path $RunDir "neo_multi_paper_live_v2.log"
    $neoStderr = Join-Path $RunDir "neo_multi_paper_live_v2.err.log"
    $neoArgs = @(
        "src\multi_futures_paper.py",
        "--secids", "AMZNperpA",
        "--runtime-sec", "32100",
        "--report-sec", "600",
        "--seed-minutes", "240",
        "--orderbook-depth", "10",
        "--profiles", "reports\futures_scalp_profiles_live_20260524.csv",
        "--paper-capital", "200000",
        "--max-total-margin-pct", "0.80",
        "--max-position-margin-pct", "0.20",
        "--no-trade-before", "10:10",
        "--no-new-after", "18:45",
        "--snapshot-sec", "10",
        "--log", "reports\paper_runs\scalp_once_20260524\neo_multi_futures_paper_trades.csv",
        "--snapshot-log", "reports\paper_runs\scalp_once_20260524\neo_live_orderbook_snapshots.csv",
        "--open-positions-log", "reports\paper_runs\scalp_once_20260524\neo_paper_open_positions.json"
    )

    Write-RunLog "StrongCommand=$py $($strongArgs -join ' ')"
    $strongProc = Start-Process -FilePath $py -ArgumentList $strongArgs -WorkingDirectory $ProjectRoot -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    $strongProc.Id | Set-Content -LiteralPath (Join-Path $ProjectRoot "reports\runtime\scalp_paper_once_20260524_strong.pid") -Encoding ASCII
    Write-RunLog "StrongStartedPid=$($strongProc.Id)"

    Write-RunLog "WeakCommand=$py $($weakArgs -join ' ')"
    $weakProc = Start-Process -FilePath $py -ArgumentList $weakArgs -WorkingDirectory $ProjectRoot -RedirectStandardOutput $weakStdout -RedirectStandardError $weakStderr -WindowStyle Hidden -PassThru
    $weakProc.Id | Set-Content -LiteralPath (Join-Path $ProjectRoot "reports\runtime\scalp_paper_once_20260524_weak.pid") -Encoding ASCII
    Write-RunLog "WeakStartedPid=$($weakProc.Id)"

    Write-RunLog "RejectedCommand=$py $($rejectedArgs -join ' ')"
    $rejectedProc = Start-Process -FilePath $py -ArgumentList $rejectedArgs -WorkingDirectory $ProjectRoot -RedirectStandardOutput $rejectedStdout -RedirectStandardError $rejectedStderr -WindowStyle Hidden -PassThru
    $rejectedProc.Id | Set-Content -LiteralPath (Join-Path $ProjectRoot "reports\runtime\scalp_paper_once_20260524_rejected.pid") -Encoding ASCII
    Write-RunLog "RejectedStartedPid=$($rejectedProc.Id)"

    Write-RunLog "NeoCommand=$py $($neoArgs -join ' ')"
    $neoProc = Start-Process -FilePath $py -ArgumentList $neoArgs -WorkingDirectory $ProjectRoot -RedirectStandardOutput $neoStdout -RedirectStandardError $neoStderr -WindowStyle Hidden -PassThru
    $neoProc.Id | Set-Content -LiteralPath (Join-Path $ProjectRoot "reports\runtime\scalp_paper_once_20260524_neo.pid") -Encoding ASCII
    Write-RunLog "NeoStartedPid=$($neoProc.Id)"
    exit 0
} catch {
    Write-RunLog "ERROR: $($_.Exception.Message)"
    exit 1
}
