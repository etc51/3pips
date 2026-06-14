[CmdletBinding()]
param(
    [string]$Server = "3pips-vds",
    [string]$Email = "etc00051@yandex.ru",
    [string]$DailyTime = "23:59",
    [switch]$TestNow
)

$secure = Read-Host "Пароль SMTP Яндекса" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
}
finally {
    if ($ptr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

if ([string]::IsNullOrWhiteSpace($plain)) {
    throw "SMTP пароль пустой"
}

$testFlag = if ($TestNow) { "--test-now" } else { "" }
$remote = "cd /opt/3pips && sudo bash scripts/enable_server_mail.sh --password-stdin --email '$Email' --daily-time '$DailyTime' $testFlag"

$plain | ssh.exe $Server $remote

if ($LASTEXITCODE -ne 0) {
    throw "Не удалось включить письма на сервере $Server"
}
