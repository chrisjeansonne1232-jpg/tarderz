# Start polybot on Windows (paper trading only; nothing here can place an order).
#   Double-click start.bat            (or start-ipad.bat to also serve your iPad)
#   powershell -ExecutionPolicy Bypass -File start.ps1 [-Ipad] [-Config other.toml]
# First run: finds or installs Python 3.11+, creates .venv, installs
# dependencies, checks the live markets. Then starts the bot with the
# dashboard and opens it in your browser. Ctrl+C stops it.
param(
    [switch]$Ipad,
    [string]$Config = "config.toml"
)

if ($env:POLYBOT_CONFIG -and $Config -eq "config.toml") { $Config = $env:POLYBOT_CONFIG }  # for testing
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"
$onWindows = ($env:OS -eq "Windows_NT")

function Say([string]$msg) { Write-Host "> $msg" -ForegroundColor Magenta }

function Stop-WithMessage([string]$msg) {
    Write-Host $msg -ForegroundColor Red
    if ($onWindows) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

function Test-Python([string]$exe, [string[]]$pre) {
    # Returns the interpreter path if it is Python 3.11+, else $null.
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $null }
    try {
        $out = & $exe @pre -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -Last 1)).Trim() }
    } catch { }
    return $null
}

function Find-Python {
    $candidates = @(
        @("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("py", "-3"),
        @("python", $null), @("python3", $null)
    )
    foreach ($c in $candidates) {
        $pre = @()
        if ($c[1]) { $pre = @($c[1]) }
        $found = Test-Python $c[0] $pre
        if ($found) { return $found }
    }
    foreach ($v in @("313", "312", "311")) {
        $p = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"
        if ($env:LOCALAPPDATA -and (Test-Path -LiteralPath $p)) { return $p }
    }
    return $null
}

# --- 1. Python 3.11+ ---------------------------------------------------------
$py = Find-Python
if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $answer = Read-Host "Python 3.11 or newer is needed. Install Python 3.12 now with winget? [Y/n]"
        if ($answer -match '^[nN]') { Stop-WithMessage "Python is required. Get it from https://www.python.org/downloads/" }
        winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        $py = Find-Python
    }
    if (-not $py) {
        Stop-WithMessage ("Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/ " +
            "(tick 'Add python.exe to PATH'), then run this again.")
    }
}

# --- 2. Private environment + dependencies (reinstalled only when they change) --
if ($onWindows) { $vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe" }
else { $vpy = Join-Path $PSScriptRoot ".venv/bin/python" }
if (-not (Test-Path -LiteralPath $vpy)) {
    $ver = & $py --version
    Say "setting up a private Python environment in .venv ($ver)"
    & $py -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $vpy)) { Stop-WithMessage "Could not create the Python environment." }
}
$stamp = Join-Path $PSScriptRoot ".venv\.requirements.sha256"
$sum = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot "requirements.txt") -Algorithm SHA256).Hash
$old = ""
if (Test-Path -LiteralPath $stamp) { $old = (Get-Content -LiteralPath $stamp -Raw).Trim() }
if ($old -ne $sum) {
    Say "installing dependencies (first run only, ~30 s)"
    & $vpy -m pip install --quiet --disable-pip-version-check --upgrade pip
    & $vpy -m pip install --quiet --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Installing dependencies failed (see above)." }
    Set-Content -LiteralPath $stamp -Value $sum
}

# --- 3. One-shot check of the live markets and fee parameters -----------------
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "data") | Out-Null
$discover = Join-Path $PSScriptRoot "data\discover.txt"
Say "checking the live Polymarket markets (full output: data\discover.txt)"
$lines = & $vpy -m polybot --config $Config discover
$lines | Out-File -LiteralPath $discover -Encoding utf8
$lines | Where-Object { $_ -cmatch '^=== |rules verified|fee model used|NOT FOUND|failed|NOTE' } | ForEach-Object { "  $_" }

# --- 4. Start the bot + dashboard (it opens your browser itself) ---------------
$botArgs = @("-m", "polybot", "--config", $Config, "run", "--dashboard", "--open")
if ($Ipad) {
    $botArgs += @("--host", "0.0.0.0")
    Say "iPad mode: if Windows Firewall asks about Python, allow it on Private networks"
}
Say "starting paper trading - press Ctrl+C to stop (keep this window open)"
& $vpy @botArgs
