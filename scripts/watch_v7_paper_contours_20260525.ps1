param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = "D:\piton\python.exe",
    [int]$DashboardPort = 8768,
    [int]$LoopSec = 15,
    [int]$StaleSec = 90,
    [int]$StartupGraceSec = 180,
    [switch]$Once
)

$ErrorActionPreference = "Stop"

$script:ProjectRoot = (Resolve-Path $ProjectRoot).Path
$script:Python = $PythonExe
$script:RuntimeDir = Join-Path $script:ProjectRoot "reports\runtime"
$script:RunDir = Join-Path $script:ProjectRoot "reports\paper_runs\v7_live_20260525"
$script:LogPath = Join-Path $script:RuntimeDir "v7_paper_supervisor_20260525.log"
$script:SupervisorPidPath = Join-Path $script:RuntimeDir "v7_paper_supervisor_20260525.pid"
New-Item -ItemType Directory -Force -Path $script:RuntimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $script:RunDir | Out-Null

$script:Portfolios = @(
    @{
        Name = "classic_core"
        Secids = @("PTZ6", "PDU6", "SiM7", "BRU6", "SVH7", "BRQ6", "PTM6", "BTN6", "BTM6", "BTK6", "PTU6", "LKU6", "BRV6")
    },
    @{
        Name = "gl_watch"
        Secids = @("GLH7", "GLZ6", "GLM6")
    },
    @{
        Name = "neo"
        Secids = @("AMDperpA", "COINperpA", "TSLAperpA")
    },
    @{
        Name = "tail_research"
        Secids = @("BRN6", "PDM6", "MMH7", "SiH7", "MMZ6", "BMN6", "BMM6", "BMV6", "BMX6", "BMU6", "S1H7", "BRX6", "BMQ6", "S1Z6", "SVZ6")
    }
)

function Write-SupervisorLog {
    param([string]$Message)
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
    Write-Output $line
}

function Get-ProcessByPidFile {
    param(
        [string]$PidPath,
        [string]$Needle
    )
    if (-not (Test-Path -LiteralPath $PidPath)) {
        return $null
    }
    $pidText = (Get-Content -LiteralPath $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
    $procId = 0
    if (-not [int]::TryParse($pidText, [ref]$procId)) {
        return $null
    }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
    if ($null -eq $proc) {
        return $null
    }
    if ($proc.CommandLine -notlike "*$Needle*") {
        return $null
    }
    return $proc
}

function Get-ProcessAgeSec {
    param($Proc)
    try {
        if ($Proc.CreationDate -is [datetime]) {
            $created = $Proc.CreationDate
        } else {
            $created = [Management.ManagementDateTimeConverter]::ToDateTime($Proc.CreationDate)
        }
        return [int]((Get-Date) - $created).TotalSeconds
    } catch {
        return 999999
    }
}

function Stop-Existing {
    param(
        [string]$PidPath,
        [string]$Needle,
        [string]$Reason
    )
    $proc = Get-ProcessByPidFile -PidPath $PidPath -Needle $Needle
    if ($null -ne $proc) {
        Write-SupervisorLog "stop pid=$($proc.ProcessId) reason=$Reason needle=$Needle"
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-OpenPositionsFile {
    param([string]$Name)
    $path = Join-Path $script:RunDir "${Name}_paper_open_positions.json"
    if (-not (Test-Path -LiteralPath $path)) {
        "[]" | Set-Content -LiteralPath $path -Encoding UTF8
    }
}

function Backup-OpenPositionsFile {
    param([string]$Name)
    $path = Join-Path $script:RunDir "${Name}_paper_open_positions.json"
    if (Test-Path -LiteralPath $path) {
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        Copy-Item -LiteralPath $path -Destination (Join-Path $script:RunDir "${Name}_paper_open_positions_before_restart_${stamp}.json") -Force
    }
}

function New-BotArgs {
    param(
        [string]$Name,
        [string[]]$Secids
    )
    return @(
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
        "--max-full-stop-rub", "4000",
        "--stop-limit-emergency-ticks", "2",
        "--actual-exit-model", "candle_like",
        "--stream-stale-sec", "15",
        "--fallback-poll-sec", "2",
        "--no-new-expiry-days", "5",
        "--expiry-force-close-days", "3",
        "--roll-observe-days", "10",
        "--roll-state-log", "reports\paper_runs\v7_live_20260525\${Name}_roll_state.json",
        "--snapshot-sec", "10",
        "--log", "reports\paper_runs\v7_live_20260525\${Name}_multi_futures_paper_trades.csv",
        "--snapshot-log", "reports\paper_runs\v7_live_20260525\${Name}_live_orderbook_snapshots.csv",
        "--open-positions-log", "reports\paper_runs\v7_live_20260525\${Name}_paper_open_positions.json",
        "--instrument-specs-log", "reports\paper_runs\v7_live_20260525\${Name}_instrument_specs.csv",
        "--startup-status-log", "reports\paper_runs\v7_live_20260525\${Name}_startup_status.csv",
        "--shadow-log", "reports\paper_runs\v7_live_20260525\${Name}_shadow_exit_models.csv",
        "--health-log", "reports\paper_runs\v7_live_20260525\${Name}_health.json"
    )
}

function Restart-Bot {
    param(
        [string]$Name,
        [string[]]$Secids,
        [string]$Reason
    )
    $pidPath = Join-Path $script:RuntimeDir "v7_paper_${Name}.pid"
    Ensure-OpenPositionsFile -Name $Name
    Backup-OpenPositionsFile -Name $Name
    Stop-Existing -PidPath $pidPath -Needle "multi_futures_paper.py" -Reason $Reason
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $stdout = Join-Path $script:RunDir "${Name}_supervisor_${stamp}.stdout.log"
    $stderr = Join-Path $script:RunDir "${Name}_supervisor_${stamp}.stderr.log"
    $args = New-BotArgs -Name $Name -Secids $Secids
    Write-SupervisorLog "start bot=$Name reason=$Reason cmd=$script:Python $($args -join ' ')"
    $proc = Start-Process -FilePath $script:Python -ArgumentList $args -WorkingDirectory $script:ProjectRoot `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    $proc.Id | Set-Content -LiteralPath $pidPath -Encoding ASCII
    Write-SupervisorLog "started bot=$Name pid=$($proc.Id)"
}

function Check-Bot {
    param($Portfolio)
    $name = [string]$Portfolio.Name
    $pidPath = Join-Path $script:RuntimeDir "v7_paper_${name}.pid"
    $healthPath = Join-Path $script:RunDir "${name}_health.json"
    $snapshotPath = Join-Path $script:RunDir "${name}_live_orderbook_snapshots.csv"
    $proc = Get-ProcessByPidFile -PidPath $pidPath -Needle "multi_futures_paper.py"
    if ($null -eq $proc) {
        Restart-Bot -Name $name -Secids $Portfolio.Secids -Reason "missing_process"
        return
    }
    $age = Get-ProcessAgeSec -Proc $proc
    if (-not (Test-Path -LiteralPath $healthPath)) {
        if ($age -lt $StartupGraceSec) {
            Write-SupervisorLog "wait bot=$name reason=missing_health startup_age_sec=$age"
            return
        }
        Restart-Bot -Name $name -Secids $Portfolio.Secids -Reason "missing_health"
        return
    }
    $healthAge = [int]((Get-Date) - (Get-Item -LiteralPath $healthPath).LastWriteTime).TotalSeconds
    if ($healthAge -gt $StaleSec -and $age -ge $StartupGraceSec) {
        Restart-Bot -Name $name -Secids $Portfolio.Secids -Reason "stale_health_${healthAge}s"
        return
    }
    if (Test-Path -LiteralPath $snapshotPath) {
        $snapshotAge = [int]((Get-Date) - (Get-Item -LiteralPath $snapshotPath).LastWriteTime).TotalSeconds
        if ($snapshotAge -gt (2 * $StaleSec) -and $age -ge $StartupGraceSec) {
            Restart-Bot -Name $name -Secids $Portfolio.Secids -Reason "stale_snapshot_${snapshotAge}s"
            return
        }
    }
    Write-SupervisorLog "ok bot=$name pid=$($proc.ProcessId) age_sec=$age health_age_sec=$healthAge"
}

function Restart-Dashboard {
    param([string]$Reason)
    $pidPath = Join-Path $script:RuntimeDir "v7_paper_dashboard_20260525.pid"
    Stop-Existing -PidPath $pidPath -Needle "paper_dashboard.py" -Reason $Reason
    $stdout = Join-Path $script:RuntimeDir "v7_paper_dashboard_20260525.stdout.log"
    $stderr = Join-Path $script:RuntimeDir "v7_paper_dashboard_20260525.stderr.log"
    $args = @("src\paper_dashboard.py", "--port", "$DashboardPort", "--dir", "reports\paper_runs\v7_live_20260525")
    Write-SupervisorLog "start dashboard reason=$Reason"
    $proc = Start-Process -FilePath $script:Python -ArgumentList $args -WorkingDirectory $script:ProjectRoot `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    $proc.Id | Set-Content -LiteralPath $pidPath -Encoding ASCII
    Write-SupervisorLog "started dashboard pid=$($proc.Id)"
}

function Check-Dashboard {
    $pidPath = Join-Path $script:RuntimeDir "v7_paper_dashboard_20260525.pid"
    $proc = Get-ProcessByPidFile -PidPath $pidPath -Needle "paper_dashboard.py"
    if ($null -eq $proc) {
        Restart-Dashboard -Reason "missing_process"
        return
    }
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$DashboardPort/" -UseBasicParsing -TimeoutSec 3 | Out-Null
    } catch {
        Restart-Dashboard -Reason "http_check_failed"
        return
    }
    Write-SupervisorLog "ok dashboard pid=$($proc.ProcessId)"
}

if (-not $Once) {
    $PID | Set-Content -LiteralPath $script:SupervisorPidPath -Encoding ASCII
}
Write-SupervisorLog "supervisor_start once=$Once root=$script:ProjectRoot stale_sec=$StaleSec startup_grace_sec=$StartupGraceSec"

while ($true) {
    foreach ($portfolio in $script:Portfolios) {
        Check-Bot -Portfolio $portfolio
    }
    Check-Dashboard
    if ($Once) {
        break
    }
    Start-Sleep -Seconds $LoopSec
}
