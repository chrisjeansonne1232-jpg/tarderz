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
        # config.toml: replace it if it is an unmodified copy of an earlier release,
        # keep it (and save the new defaults next to it) if you've edited it.
        $shipped = @("2536e702e6aa5518fe4ee785793415a92f340ea19eba59f36ac53a2f110715ca", "423bddbe5dc5c398757ac3685e71b570387f52a1b59914445f767ea52b757d96", "28f8344f62acc3f04ab7c5e215df3427eb903fe880c5f93373f77fac3a659e74", "071340332ce3dd87563729c54784a1176743c9cc367fa17d75c36e55363fe788", "78ed7a464ce53ee4f5a82e73fb2d04780570d51f9c705ccf14fd395e781069a8", "70711e73c3c3c219f07423a5bd0a0d6221422d06cc9b43daacb9bb83c93156d6", "c75d98666829b6b35d7aa7ca70a84c00e0d010939f3c42c82b954e515a7fc078")
        $stampFile = Join-Path $dir ".config.shipped.sha256"
        if (Test-Path -LiteralPath $stampFile) { $shipped += (Get-Content -LiteralPath $stampFile -Raw).Trim().ToLower() }
        $newCfg = Join-Path $src.FullName "config.toml"
        $newHash = (Get-FileHash -LiteralPath $newCfg -Algorithm SHA256).Hash.ToLower()
        $cfg = Join-Path $dir "config.toml"
        if (Test-Path -LiteralPath $cfg) {
            $h = (Get-FileHash -LiteralPath $cfg -Algorithm SHA256).Hash.ToLower()
            if ($shipped -contains $h) {
                Write-Host "> updating config.toml to this version's defaults (you hadn't changed it)" -ForegroundColor Magenta
            } else {
                Write-Host "> keeping your edited config.toml (this version's defaults saved as config.default.toml)" -ForegroundColor Magenta
                Move-Item -LiteralPath $newCfg -Destination (Join-Path $src.FullName "config.default.toml") -Force
            }
        }
        Get-ChildItem -LiteralPath $src.FullName -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $dir -Recurse -Force
        }
        Set-Content -LiteralPath $stampFile -Value $newHash
    } catch {
        Write-Host "Install failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Download used: $url"
        $installed = Join-Path $dir "start.ps1"
        if ($env:POLYBOT_NO_START -or -not (Test-Path -LiteralPath $installed)) { return }
        Write-Host "> starting the copy already installed instead" -ForegroundColor Magenta
        $psExe = (Get-Process -Id $PID).Path
        & $psExe -NoProfile -ExecutionPolicy Bypass -File $installed
        return
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    if ($onWindows) {
        try {
            $desktop = [Environment]::GetFolderPath("Desktop")
            $shell = New-Object -ComObject WScript.Shell
            # The shortcuts run update.ps1: fetch the latest version, then start (offline: start as is).
            $psPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
            $upd = Join-Path $dir "update.ps1"
            foreach ($pair in @(@("Polybot.lnk", ""), @("Polybot (iPad).lnk", " -Ipad"))) {
                $lnk = $shell.CreateShortcut((Join-Path $desktop $pair[0]))
                $lnk.TargetPath = $psPath
                $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$upd`"" + $pair[1]
                $lnk.WorkingDirectory = $dir
                $lnk.Save()
            }
            # A shortcut to the daily archive (every trade, signal and skip as spreadsheet files).
            $archiveDir = Join-Path $dir "data\archive"
            New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null
            $lnk = $shell.CreateShortcut((Join-Path $desktop "Polybot archive.lnk"))
            $lnk.TargetPath = $archiveDir
            $lnk.Save()
            Write-Host "> added 'Polybot', 'Polybot (iPad)' and 'Polybot archive' shortcuts to your desktop" -ForegroundColor Magenta
        } catch {
            Write-Host "(could not create desktop shortcuts: $($_.Exception.Message))"
        }
    }

    $ver = ""
    $initPy = Join-Path $dir "polybot\__init__.py"
    if (Test-Path -LiteralPath $initPy) {
        $m = Select-String -LiteralPath $initPy -Pattern '__version__ = "([^"]+)"' | Select-Object -First 1
        if ($m) { $ver = " v" + $m.Matches[0].Groups[1].Value }
    }
    Write-Host "> installed polybot$ver. Next time: double-click 'Polybot' on your desktop" -ForegroundColor Magenta
    if ($env:POLYBOT_NO_START) { return }  # for testing the installer alone
    # start.ps1 runs in a child PowerShell so script execution policy doesn't block it.
    $psExe = (Get-Process -Id $PID).Path
    & $psExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dir "start.ps1")
}
