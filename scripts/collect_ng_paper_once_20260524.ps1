param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$runDir = Join-Path $ProjectRoot "reports\paper_runs\20260524_once"
$zipPath = Join-Path $ProjectRoot "reports\paper_runs\ng_paper_once_20260524.zip"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$items = @(
    "reports\live_orderbook_snapshots.csv",
    "reports\paper_execution_trades.csv",
    "reports\paper_execution_summary.csv",
    "reports\paper_execution_by_day.csv",
    "reports\paper_execution_daily_summary.md",
    "reports\paper_monitor_heartbeat.csv",
    "reports\paper_contract_selection.csv",
    "reports\paper_open_positions.json"
)
$copied = New-Object System.Collections.Generic.List[string]
$missing = New-Object System.Collections.Generic.List[string]

foreach ($rel in $items) {
    $src = Join-Path $ProjectRoot $rel
    if (Test-Path $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $runDir (Split-Path $rel -Leaf)) -Force
        $copied.Add($rel)
    } else {
        $missing.Add($rel)
    }
}

$runtime = Join-Path $ProjectRoot "reports\runtime"
if (Test-Path $runtime) {
    Get-ChildItem -LiteralPath $runtime -Filter "ng_paper_once_20260524_*.log" -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $runDir $_.Name) -Force
        $copied.Add("reports\runtime\$($_.Name)")
    }
    Get-ChildItem -LiteralPath $runtime -Filter "ng_paper_once_20260524_*.pid" -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $runDir $_.Name) -Force
        $copied.Add("reports\runtime\$($_.Name)")
    }
}

$mainLog = Get-ChildItem -LiteralPath $runtime -Filter "ng_paper_once_20260524_main_*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$dailySummary = Join-Path $ProjectRoot "reports\paper_execution_daily_summary.md"
$readme = Join-Path $runDir "README_RUN_RESULT.md"

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# NG paper once 20260524 run result")
$lines.Add("")
$lines.Add("generated_at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$lines.Add("")
$lines.Add("## Files copied")
if ($copied.Count) { $copied | ForEach-Object { $lines.Add("- $_") } } else { $lines.Add("- none") }
$lines.Add("")
$lines.Add("## Missing files")
if ($missing.Count) { $missing | ForEach-Object { $lines.Add("- $_") } } else { $lines.Add("- none") }
$lines.Add("")
$lines.Add("## Last 30 lines from main log")
if ($mainLog) {
    $lines.Add("source: $($mainLog.FullName)")
    $lines.Add('```')
    Get-Content -LiteralPath $mainLog.FullName -Tail 30 | ForEach-Object { $lines.Add($_) }
    $lines.Add('```')
} else {
    $lines.Add("main log not found")
}
$lines.Add("")
$lines.Add("## paper_execution_daily_summary.md")
if (Test-Path $dailySummary) {
    Get-Content -LiteralPath $dailySummary | ForEach-Object { $lines.Add($_) }
    $content = Get-Content -Raw -LiteralPath $dailySummary
    if ($content -match "execution test meaningful:\s*False") {
        $lines.Add("")
        $lines.Add("WARNING: execution_test_meaningful=False")
    }
} else {
    $lines.Add("paper_execution_daily_summary.md not found")
}
$lines | Set-Content -LiteralPath $readme -Encoding UTF8

if (Test-Path $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
Compress-Archive -Path (Join-Path $runDir "*") -DestinationPath $zipPath -Force

Write-Host "Collected run artifacts into: $runDir"
Write-Host "Zip created: $zipPath"
