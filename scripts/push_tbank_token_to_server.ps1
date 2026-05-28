param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [string]$ProjectRoot = "/opt/3pips"
)

$ErrorActionPreference = "Stop"

function Find-TBankToken {
    foreach ($name in "TBANK_TOKEN", "TBANK_TOKEN_READONLY", "TINKOFF_TOKEN") {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value -and $value.Trim()) {
            return $value.Trim()
        }
    }

    $desktop = [Environment]::GetFolderPath("Desktop")
    if (Test-Path -LiteralPath $desktop) {
        foreach ($path in Get-ChildItem -LiteralPath $desktop -Filter "*.txt" -File -ErrorAction SilentlyContinue) {
            $text = Get-Content -LiteralPath $path.FullName -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
            if (-not $text) {
                continue
            }
            $match = [regex]::Match($text, "(?i)(t\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{40,})")
            if ($match.Success) {
                return $match.Value.Trim()
            }
        }
    }
    throw "T-Bank token not found in env or Desktop txt files."
}

$token = Find-TBankToken
$remoteCommand = "mkdir -p '$ProjectRoot/secrets' && umask 077 && cat > '$ProjectRoot/secrets/tbank_token.txt' && cd '$ProjectRoot' && sudo docker compose restart paper"

$token | ssh $Server $remoteCommand
Write-Host "Token pushed to $Server:$ProjectRoot/secrets/tbank_token.txt and paper container restarted."
