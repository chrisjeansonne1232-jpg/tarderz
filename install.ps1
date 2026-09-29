# One-line install + start for Windows (paper trading only; nothing here can place an order).
# Paste into PowerShell:
#
#   irm https://raw.githubusercontent.com/chrisjeansonne1232-jpg/tarderz/HEAD/install.ps1 | iex
#
# Installs into %USERPROFILE%\polybot, adds "Polybot" shortcuts to your desktop,
# then starts the bot and opens the dashboard. Paste the same line again to
# update: code is replaced, your data\, .venv and config.toml are kept.
& {
    $ErrorActionPreference = "Stop"          # scoped to this block, not your session
    $ProgressPreference = "SilentlyContinue"  # Windows PowerShell downloads are slow with the progress bar
    $repo = "chrisjeansonne1232-jpg/tarderz"
    $branch = "claude/polymarket-btc-paper-trading-nxqncp"
    $url = "https://github.com/$repo/archive/refs/heads/$branch.zip"
    if ($env:POLYBOT_ZIP_URL) { $url = $env:POLYBOT_ZIP_URL }
    $dir = Join-Path $HOME "polybot"
    if ($env:POLYBOT_DIR) { $dir = $env:POLYBOT_DIR }
    $onWindows = ($env:OS -eq "Windows_NT")

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }

    Write-Host "> downloading polybot into $dir" -ForegroundColor Magenta
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("polybot-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp "src.zip"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        $unzipped = Join-Path $tmp "x"
        Expand-Archive -LiteralPath $zip -DestinationPath $unzipped -Force
        $src = Get-ChildItem -LiteralPath $unzipped -Directory | Select-Object -First 1
        if (-not $src -or -not (Test-Path -LiteralPath (Join-Path $src.FullName "start.ps1"))) {
            throw "the download doesn't look like polybot (no start.ps1)"
        }
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        if (Test-Path -LiteralPath (Join-Path $dir "config.toml")) {
            Write-Host "> keeping your config.toml (this version's defaults saved as config.default.toml)" -ForegroundColor Magenta
            Move-Item -LiteralPath (Join-Path $src.FullName "config.toml") -Destination (Join-Path $src.FullName "config.default.toml") -Force
        }
        Get-ChildItem -LiteralPath $src.FullName -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $dir -Recurse -Force
        }
    } catch {
        Write-Host "Install failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Download used: $url"
        return
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    if ($onWindows) {
        try {
            $desktop = [Environment]::GetFolderPath("Desktop")
            $shell = New-Object -ComObject WScript.Shell
            foreach ($pair in @(@("Polybot.lnk", "start.bat"), @("Polybot (iPad).lnk", "start-ipad.bat"))) {
                $lnk = $shell.CreateShortcut((Join-Path $desktop $pair[0]))
                $lnk.TargetPath = Join-Path $dir $pair[1]
                $lnk.WorkingDirectory = $dir
                $lnk.Save()
            }
            Write-Host "> added 'Polybot' and 'Polybot (iPad)' shortcuts to your desktop" -ForegroundColor Magenta
        } catch {
            Write-Host "(could not create desktop shortcuts: $($_.Exception.Message))"
        }
    }

    Write-Host "> installed. Next time: double-click 'Polybot' on your desktop" -ForegroundColor Magenta
    # start.ps1 runs in a child PowerShell so script execution policy doesn't block it.
    $psExe = (Get-Process -Id $PID).Path
    & $psExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dir "start.ps1")
}
