param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonExe = "",
    [string]$StartDate = "2026-05-24",
    [string]$DashboardTime = "09:40",
    [string]$MainTime = "09:50",
    [string]$TaskPrefix = "Scalp Paper Once 20260524"
)

$ErrorActionPreference = "Stop"

function New-OnceTask {
    param(
        [string]$Name,
        [datetime]$At,
        [string]$Mode,
        [string]$ScriptPath
    )

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }

    $argument = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -Mode $Mode -ProjectRoot `"$ProjectRoot`""
    if ($PythonExe) {
        $argument += " -PythonExe `"$PythonExe`""
    }

    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $ProjectRoot
    $trigger = New-ScheduledTaskTrigger -Once -At $At
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null

    Write-Host "Registered: $Name"
    Write-Host "Start:      $($At.ToString('yyyy-MM-dd HH:mm:ss'))"
    Write-Host "Command:    powershell.exe $argument"
    Write-Host ""
}

$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$scriptRun = Join-Path $ProjectRoot "scripts\run_scalp_paper_once_20260524.ps1"
if (!(Test-Path $scriptRun)) { throw "Missing script: $scriptRun" }

$dashboardAt = [datetime]::ParseExact("$StartDate $DashboardTime", "yyyy-MM-dd HH:mm", $null)
$mainAt = [datetime]::ParseExact("$StartDate $MainTime", "yyyy-MM-dd HH:mm", $null)

New-OnceTask -Name "$TaskPrefix Dashboard" -At $dashboardAt -Mode "dashboard" -ScriptPath $scriptRun
New-OnceTask -Name "$TaskPrefix Main" -At $mainAt -Mode "main" -ScriptPath $scriptRun

Write-Host "One-time scalp paper tasks registered."
