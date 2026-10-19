# find_apps.ps1
#
# Looks up the real install path for a list of common apps and writes
# the result as a ready-to-paste Python dict (matching the APPS format
# in skills/pc_control.py) to found_app_paths.txt, then opens it in
# Notepad.
#
# Usage:
#   cd D:\claude\cipher\backend
#   powershell -ExecutionPolicy Bypass -File find_apps.ps1

# Map: the name you'll type in chat -> the actual .exe to look for.
# Add/remove entries here as you like before running.
$apps = [ordered]@{
    "notepad"      = "notepad.exe"
    "calculator"   = "calc.exe"
    "explorer"     = "explorer.exe"
    "chrome"       = "chrome.exe"
    "firefox"      = "firefox.exe"
    "edge"         = "msedge.exe"
    "spotify"      = "Spotify.exe"
    "word"         = "WINWORD.EXE"
    "excel"        = "EXCEL.EXE"
    "powerpoint"   = "POWERPNT.EXE"
    "outlook"      = "OUTLOOK.EXE"
    "task manager" = "taskmgr.exe"
    "vscode"       = "Code.exe"
    "discord"      = "Discord.exe"
    "slack"        = "slack.exe"
    "zoom"         = "Zoom.exe"
    "vlc"          = "vlc.exe"

    # From your installed-programs list
    "notion"                = "Notion.exe"
    "perplexity"            = "Perplexity.exe"
    "winrar"                = "WinRAR.exe"
    "vmware"                = "vmware.exe"
    "vmware workstation"    = "vmware.exe"
    "7-zip"                 = "7zFM.exe"
    "acrobat"               = "Acrobat.exe"
    "adobe acrobat"         = "Acrobat.exe"
    "bluestacks"            = "HD-Player.exe"
    "capcut"                = "CapCut.exe"
    "cheat engine"          = "cheatengine-x86_64.exe"
    "epic games"            = "EpicGamesLauncher.exe"
    "epic games launcher"   = "EpicGamesLauncher.exe"
    "gameloop"              = "GameLoop.exe"
    "google drive"          = "GoogleDriveFS.exe"
    "idm"                   = "IDMan.exe"
    "internet download manager" = "IDMan.exe"
    "itop screen recorder"  = "iTop Screen Recorder.exe"
    "lively wallpaper"      = "Lively.exe"
    "onedrive"              = "OneDrive.exe"
    "remote desktop"        = "mstsc.exe"
    "remote desktop connection" = "mstsc.exe"
    "autohotkey"            = "AutoHotkeyUX.exe"
    "turbovpn"              = "TurboVPN.exe"
    "hotspot shield"        = "HssLauncher.exe"
}

# Folders to fall back to searching if the exe isn't already on PATH.
$searchRoots = @(
    "$env:ProgramFiles",
    "${env:ProgramFiles(x86)}",
    "$env:LOCALAPPDATA\Programs",
    "$env:APPDATA"
) | Where-Object { $_ -and (Test-Path $_) }

$outFile = Join-Path $PSScriptRoot "found_app_paths.txt"
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# Paste the entries below into the APPS dict in skills/pc_control.py")
$lines.Add("# Anything marked NOT FOUND needs a manual path (right-click the app's")
$lines.Add("# shortcut -> Properties -> Target), or just delete that line.")
$lines.Add("")
$lines.Add("APPS = {")

foreach ($name in $apps.Keys) {
    $exe = $apps[$name]
    Write-Host "Looking for $name ($exe)..."

    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $path = $cmd.Source
        $lines.Add("    `"$name`": [r`"$path`"],")
        continue
    }

    # Not on PATH - search common install locations (can take a little
    # while the first time; it's only scanning Program Files/AppData).
    $found = $null
    foreach ($root in $searchRoots) {
        $found = Get-ChildItem -Path $root -Filter $exe -Recurse -File -ErrorAction SilentlyContinue |
                 Select-Object -First 1
        if ($found) { break }
    }

    if ($found) {
        $lines.Add("    `"$name`": [r`"$($found.FullName)`"],")
    } else {
        $lines.Add("    # NOT FOUND: `"$name`" ($exe) -- fill in manually if you have it installed")
    }
}

$lines.Add("}")

$lines | Out-File -FilePath $outFile -Encoding utf8
Write-Host ""
Write-Host "Done. Results written to $outFile"
notepad.exe $outFile
