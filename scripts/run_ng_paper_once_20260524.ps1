param(
    [ValidateSet("smoke", "main")]
    [string]$Mode,
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    param([string]$Requested, [string]$Root)
    if ($Requested) {
        if (!(Get-Command $Requested -ErrorAction SilentlyContinue) -and !(Test-Path $Requested)) {
            throw "PythonExe was provided but not found: $Requested"
        }
        return @{ File = $Requested; PrefixArgs = @() }
    }
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return @{ File = $venv; PrefixArgs = @() } }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return @{ File = $python.Source; PrefixArgs = @() } }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return @{ File = $py.Source; PrefixArgs = @("-3") } }
    throw "Python was not found. Tried .venv\Scripts\python.exe, python, py -3."
}

function Write-Log {
    param([string]$Message)
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    $line | Tee-Object -FilePath $script:LogPath -Append
}

function Backup-PaperFiles {
    param([string]$Root)
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupDir = Join-Path $Root "reports\paper_runs\pre_once_backup_$stamp"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $files = @(
        "reports\live_orderbook_snapshots.csv",
        "reports\paper_execution_trades.csv",
        "reports\paper_execution_summary.csv",
        "reports\paper_execution_by_day.csv",
        "reports\paper_execution_daily_summary.md",
        "reports\paper_monitor_heartbeat.csv",
        "reports\paper_contract_selection.csv",
        "reports\paper_open_positions.json"
    )
    foreach ($rel in $files) {
        $src = Join-Path $Root $rel
        if (Test-Path $src) {
            Copy-Item -LiteralPath $src -Destination (Join-Path $backupDir (Split-Path $rel -Leaf)) -Force
        }
    }
    return $backupDir
}

function Has-OpenPositions {
    param([string]$Root)
    $path = Join-Path $Root "reports\paper_open_positions.json"
    if (!(Test-Path $path)) { return $false }
    try {
        $json = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
        if ($null -eq $json) { return $false }
        return @($json).Count -gt 0
    } catch {
        return $true
    }
}

$ProjectRoot = (Resolve-Path $ProjectRoot).Path
Set-Location $ProjectRoot
New-Item -ItemType Directory -Force -Path "reports\runtime" | Out-Null
New-Item -ItemType Directory -Force -Path "reports\paper_runs" | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$script:LogPath = Join-Path $ProjectRoot "reports\runtime\ng_paper_once_20260524_${Mode}_${timestamp}.log"
$pidPath = Join-Path $ProjectRoot "reports\runtime\ng_paper_once_20260524_main.pid"

try {
    Write-Log "Mode=$Mode"
    Write-Log "ProjectRoot=$ProjectRoot"
    Write-Log "PaperOnly=True"

    $monitor = Join-Path $ProjectRoot "src\leadlag_ng_paper_orderbook_monitor.py"
    if (!(Test-Path $monitor)) { throw "Missing monitor script: $monitor" }

    $py = Resolve-Python -Requested $PythonExe -Root $ProjectRoot
    Write-Log "Python=$($py.File) $($py.PrefixArgs -join ' ')"

    $tokenPresent = [bool]($env:TBANK_TOKEN_READONLY) -or [bool]($env:TINKOFF_TOKEN)
    Write-Log "token_present=$tokenPresent"

    $checkArgs = @($py.PrefixArgs) + @("-c", "import pandas, numpy, requests, statsmodels; print('imports_ok')")
    $checkOut = & $py.File @checkArgs 2>&1
    $checkCode = $LASTEXITCODE
    $checkOut | Tee-Object -FilePath $script:LogPath -Append
    if ($checkCode -ne 0) {
        throw "Python import preflight failed. Check requirements.txt and the active Python environment."
    }

    if ($Mode -eq "smoke") {
        $args = @($py.PrefixArgs) + @(
            "src\leadlag_ng_paper_orderbook_monitor.py",
            "--once",
            "--weekend-session",
            "--shadow-execution",
            "--orderbook-source", "auto",
            "--paper-only"
        )
        Write-Log "Command=$($py.File) $($args -join ' ')"
        & $py.File @args 2>&1 | Tee-Object -FilePath $script:LogPath -Append
        $code = $LASTEXITCODE
        Write-Log "ExitCode=$code"
        exit $code
    }

    $backupDir = Backup-PaperFiles -Root $ProjectRoot
    Write-Log "PreRunBackup=$backupDir"

    $monitorArgs = @($py.PrefixArgs) + @(
        "src\leadlag_ng_paper_orderbook_monitor.py",
        "--loop",
        "--weekend-session",
        "--shadow-execution",
        "--orderbook-source", "tbank-stream",
        "--heartbeat-seconds", "5",
        "--max-target-spread-ticks", "4",
        "--max-plus1-spread-ticks", "6",
        "--min-touch-size", "1",
        "--paper-only"
    )
    if (!(Has-OpenPositions -Root $ProjectRoot)) {
        $monitorArgs += "--reset-paper-day"
        Write-Log "ResetPaperDay=True; no open paper positions found."
    } else {
        Write-Log "ResetPaperDay=False; existing paper_open_positions.json is non-empty or unreadable."
    }

    $stdoutPath = Join-Path $ProjectRoot "reports\runtime\ng_paper_once_20260524_main_${timestamp}.stdout.log"
    $stderrPath = Join-Path $ProjectRoot "reports\runtime\ng_paper_once_20260524_main_${timestamp}.stderr.log"
    Write-Log "Command=$($py.File) $($monitorArgs -join ' ')"
    $proc = Start-Process -FilePath $py.File -ArgumentList $monitorArgs -WorkingDirectory $ProjectRoot -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru
    $proc.Id | Set-Content -LiteralPath $pidPath -Encoding ASCII
    Write-Log "StartedPid=$($proc.Id)"
    Write-Log "PidFile=$pidPath"

    $stopAt = [datetime]::ParseExact("2026-05-24 18:58", "yyyy-MM-dd HH:mm", $null)
    $stoppedBySchedule = $false
    while (!$proc.HasExited) {
        if ((Get-Date) -ge $stopAt) {
            $stoppedBySchedule = $true
            Write-Log "StopAt reached. Stopping PID $($proc.Id)."
            try { $proc.CloseMainWindow() | Out-Null } catch {}
            Start-Sleep -Seconds 30
            if (!$proc.HasExited) {
                Stop-Process -Id $proc.Id -Force
                Write-Log "Forced stop for PID $($proc.Id)."
            }
            break
        }
        Start-Sleep -Seconds 5
        try { $proc.Refresh() } catch {}
    }
    $proc.Refresh()
    Write-Log "StoppedBySchedule=$stoppedBySchedule"
    Write-Log "StopTimestamp=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    Write-Log "ExitCode=$($proc.ExitCode)"
    if (Test-Path $stdoutPath) {
        Write-Log "--- monitor stdout ---"
        Get-Content -LiteralPath $stdoutPath | Tee-Object -FilePath $script:LogPath -Append
    }
    if (Test-Path $stderrPath) {
        Write-Log "--- monitor stderr ---"
        Get-Content -LiteralPath $stderrPath | Tee-Object -FilePath $script:LogPath -Append
    }
    exit 0
} catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 1
}
