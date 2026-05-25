param(
    [string]$TaskPrefix = "NG Paper Once 20260524"
)

$ErrorActionPreference = "Stop"
$names = @("$TaskPrefix Smoke", "$TaskPrefix Main", "$TaskPrefix Collect")
foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "removed: $name"
    } else {
        Write-Host "not found: $name"
    }
}
