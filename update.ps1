# Desktop shortcut target: fetch the latest polybot, then start it.
# Paper trading only; nothing here can place an order.
#   powershell -ExecutionPolicy Bypass -File update.ps1 [-Ipad]
# If GitHub can't be reached, it starts the copy already installed.
param([switch]$Ipad)

if ($Ipad) { $env:POLYBOT_IPAD = "1" }
$env:POLYBOT_DIR = $PSScriptRoot
$url = "https://raw.githubusercontent.com/chrisjeansonne1232-jpg/tarderz/refs/heads/claude/polymarket-btc-paper-trading-nxqncp/install.ps1"
if ($env:POLYBOT_INSTALLER_URL) { $url = $env:POLYBOT_INSTALLER_URL }  # for testing

$installer = $null
try {
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }
    Write-Host "> checking for updates" -ForegroundColor Magenta
    $installer = Invoke-RestMethod -Uri $url -UseBasicParsing -TimeoutSec 20
} catch {
    Write-Host "(could not check for updates: $($_.Exception.Message))"
}

if ($installer -is [string] -and $installer.Contains("polybot")) {
    Invoke-Expression $installer  # downloads the latest code, keeps data\ and your config, then starts the bot
} else {
    Write-Host "> starting the installed copy" -ForegroundColor Magenta
    & (Join-Path $PSScriptRoot "start.ps1")
}
