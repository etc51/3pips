param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = "",
    [string]$StartDate = "2026-05-24",
    [string]$SmokeTime = "09:45",
    [string]$MainTime = "09:50",
    [string]$CollectTime = "19:05",
    [string]$TaskPrefix = "NG Paper Once 20260524"
)

$ErrorActionPreference = "Stop"

function New-OnceTask {
    param(
        [string]$Name,
        [datetime]$At,
        [string]$ScriptPath,
        [string]$ExtraArgs
    )

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Warning "Scheduled task '$Name' already exists. Replacing it."
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }

    $logPath = Join-Path $ProjectRoot "reports\runtime"
    $argument = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" $ExtraArgs -ProjectRoot `"$ProjectRoot`""
    if ($PythonExe) {
        $argument += " -PythonExe `"$PythonExe`""
    }

    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $ProjectRoot
    $trigger = New-ScheduledTaskTrigger -Once -At $At
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null

    Write-Host ""
    Write-Host "Registered: $Name"
    Write-Host "Start:      $($At.ToString('yyyy-MM-dd HH:mm:ss')) local time"
    Write-Host "Command:    powershell.exe $argument"
    Write-Host "Project:    $ProjectRoot"
    Write-Host "Log dir:    $logPath"
}

$project = Resolve-Path $ProjectRoot
$ProjectRoot = $project.Path
$scriptRun = Join-Path $ProjectRoot "scripts\run_ng_paper_once_20260524.ps1"
$scriptCollect = Join-Path $ProjectRoot "scripts\collect_ng_paper_once_20260524.ps1"

if (!(Test-Path $scriptRun)) { throw "Missing script: $scriptRun" }
if (!(Test-Path $scriptCollect)) { throw "Missing script: $scriptCollect" }

$smokeAt = [datetime]::ParseExact("$StartDate $SmokeTime", "yyyy-MM-dd HH:mm", $null)
$mainAt = [datetime]::ParseExact("$StartDate $MainTime", "yyyy-MM-dd HH:mm", $null)
$collectAt = [datetime]::ParseExact("$StartDate $CollectTime", "yyyy-MM-dd HH:mm", $null)

New-OnceTask -Name "$TaskPrefix Smoke" -At $smokeAt -ScriptPath $scriptRun -ExtraArgs "-Mode smoke"
New-OnceTask -Name "$TaskPrefix Main" -At $mainAt -ScriptPath $scriptRun -ExtraArgs "-Mode main"
New-OnceTask -Name "$TaskPrefix Collect" -At $collectAt -ScriptPath $scriptCollect -ExtraArgs ""

Write-Host ""
Write-Host "One-time tasks registered. No daily or repeating trigger was created."
