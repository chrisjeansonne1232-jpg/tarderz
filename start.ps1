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
if ($env:POLYBOT_IPAD) { $Ipad = $true }  # set by update.ps1 -Ipad (the "Polybot (iPad)" shortcut)
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"
$onWindows = ($env:OS -eq "Windows_NT")

function Say([string]$msg) { Write-Host "> $msg" -ForegroundColor Magenta }

function Stop-WithMessage([string]$msg) {
    Write-Host $msg -ForegroundColor Red
    if ($onWindows) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

function Test-Exe([string]$exe, [string[]]$pre) {
    # Run an interpreter and return its path if it is Python 3.11+, else $null.
    # stdin is closed so nothing can sit waiting on a prompt we don't show.
    try {
        $out = $null | & $exe @pre -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -Last 1)).Trim() }
    } catch { }
    return $null
}

function Find-Python {
    # The Python install manager's "py"/"python" commands silently download a
    # runtime when none is installed; keep them from doing that while we look.
    $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = "false"
    $paths = @()
    # 1. Standard install folders: python.org (per-user, all-users) and the install manager.
    $roots = @()
    if ($env:LOCALAPPDATA) { $roots += @((Join-Path $env:LOCALAPPDATA "Programs\Python"), (Join-Path $env:LOCALAPPDATA "Python")) }
    if ($env:ProgramFiles) { $roots += $env:ProgramFiles }
    foreach ($root in $roots) {
        if (Test-Path -LiteralPath $root) {
            Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '^(Python3\d+|pythoncore-3\.\d+)' } |
                Sort-Object Name -Descending |
                ForEach-Object { $paths += (Join-Path $_.FullName "python.exe") }
        }
    }
    # 2. Whatever python / python3 resolve to on PATH.
    foreach ($name in @("python", "python3")) {
        foreach ($cmd in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
            $paths += $cmd.Source
        }
    }
    foreach ($p in $paths) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        $found = Test-Exe $p @()
        if ($found) { return $found }
    }
    # 3. The py launcher, last.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $found = Test-Exe "py" @("-3")
        if ($found) { return $found }
    }
    return $null
}

# --- 1. Python 3.11+ ---------------------------------------------------------
Say "looking for Python 3.11 or newer"
$py = Find-Python
if (-not $py) {
    $answer = Read-Host "Python 3.11 or newer is needed. Install Python 3.12 now? [Y/n]"
    if ($answer -match '^[nN]') { Stop-WithMessage "Python is required. Get it from https://www.python.org/downloads/" }
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "installing Python 3.12 with winget (a minute or two)"
        winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        Say "installing Python 3.12 with the Python install manager"
        py install 3.12
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $py = Find-Python
    if (-not $py) {
        Stop-WithMessage ("Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/ " +
            "(tick 'Add python.exe to PATH'), then run this again.")
    }
}
Say "using $py"

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

# --- 3b. Only one Polybot can run at a time ----------------------------------
# An older copy still running (often a minimized or forgotten window) holds the
# dashboard port and the database, so stop it before starting this one.
if ($onWindows) {
    try {
        $old = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -and $_.CommandLine -match '-m\s+polybot\b' -and $_.CommandLine -match '\s(run|watch)\b' })
        if ($old.Count -gt 0) {
            $ids = ($old | ForEach-Object { $_.ProcessId }) -join ", "
            Say "stopping an older Polybot that was still running (process $ids)"
            foreach ($p in $old) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 3
        }
    } catch {
        Write-Host "(could not check for an older Polybot: $($_.Exception.Message))"
    }
}

# --- 4. Start the bot + dashboard (it opens your browser itself) ---------------
$botArgs = @("-m", "polybot", "--config", $Config, "run", "--dashboard", "--open")
if ($Ipad) {
    $botArgs += @("--host", "0.0.0.0")
    Say "iPad mode: if Windows Firewall asks about Python, allow it on Private networks"
}
Say "starting paper trading - press Ctrl+C to stop (keep this window open)"
& $vpy @botArgs
if ($LASTEXITCODE -ne 0 -and $onWindows) { Read-Host "polybot stopped (see the message above). Press Enter to close this window" | Out-Null }
