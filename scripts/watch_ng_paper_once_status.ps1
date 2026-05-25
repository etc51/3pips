param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$TaskPrefix = "NG Paper Once 20260524"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
Write-Host "ProjectRoot: $ProjectRoot"
Write-Host ""
Write-Host "Scheduled tasks:"
$tasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "$TaskPrefix*" } | Sort-Object TaskName
if (!$tasks) {
    Write-Host "  none"
} else {
    foreach ($task in $tasks) {
        $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -ErrorAction SilentlyContinue
        Write-Host "  $($task.TaskName)"
        Write-Host "    State:       $($task.State)"
        Write-Host "    LastRunTime: $($info.LastRunTime)"
        Write-Host "    NextRunTime: $($info.NextRunTime)"
        Write-Host "    LastResult:  $($info.LastTaskResult)"
    }
}

$runtime = Join-Path $ProjectRoot "reports\runtime"
Write-Host ""
Write-Host "Latest runtime log:"
$latest = $null
if (Test-Path $runtime) {
    $latest = Get-ChildItem -LiteralPath $runtime -Filter "ng_paper_once_20260524_*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}
if ($latest) {
    Write-Host "  $($latest.FullName)"
    Get-Content -LiteralPath $latest.FullName -Tail 20
} else {
    Write-Host "  no runtime log yet"
}

$summaryPath = Join-Path $ProjectRoot "reports\paper_execution_summary.csv"
Write-Host ""
Write-Host "Paper execution summary:"
if (Test-Path $summaryPath) {
    $summary = Import-Csv -LiteralPath $summaryPath | Where-Object { $_.execution_policy -eq "__overall__" } | Select-Object -First 1
    if ($summary) {
        $fields = @(
            "raw_snapshots",
            "valid_live_signals",
            "stale_signals",
            "orderbook_missing",
            "executable_signals",
            "strategy_opened_trades",
            "strategy_closed_trades",
            "shadow_opened_trades",
            "shadow_closed_trades",
            "execution_test_meaningful"
        )
        foreach ($field in $fields) {
            Write-Host "  ${field}: $($summary.$field)"
        }
    } else {
        Write-Host "  overall row not found"
    }
} else {
    Write-Host "  summary not found"
}
