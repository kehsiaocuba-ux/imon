# Per-user installation of the portable imon package (no administrator rights needed).
# Run from the unzipped folder:   powershell -ExecutionPolicy Bypass -File .\install.ps1
# Installs to %LOCALAPPDATA%\Programs\imon, adds it to the user's PATH and creates
# Start Menu shortcuts. Remove everything again with uninstall.ps1 (also copied there).
$ErrorActionPreference = "Stop"
$source = $PSScriptRoot
$target = Join-Path $env:LOCALAPPDATA "Programs\imon"
if (-not (Test-Path (Join-Path $source "imon.exe"))) { throw "imon.exe not found next to this script" }

Write-Host "Installing imon to $target"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Path (Join-Path $source "*") -Destination $target -Recurse -Force

# user PATH
$path = [Environment]::GetEnvironmentVariable("Path", "User")
if (($path -split ";") -notcontains $target) {
    [Environment]::SetEnvironmentVariable("Path", ($path.TrimEnd(";") + ";" + $target), "User")
    Write-Host "Added to your PATH (open a new terminal to use 'imon')"
}

# Start Menu shortcuts
$menu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\imon"
New-Item -ItemType Directory -Force -Path $menu | Out-Null
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $menu "imon help.lnk"))
$lnk.TargetPath = "$env:ComSpec"; $lnk.Arguments = "/k `"$target\imon.exe`" --help"; $lnk.Save()
$lnk = $shell.CreateShortcut((Join-Path $menu "Uninstall imon.lnk"))
$lnk.TargetPath = "powershell.exe"; $lnk.Arguments = "-ExecutionPolicy Bypass -File `"$target\uninstall.ps1`""; $lnk.Save()

Write-Host "Done. Run 'imon' in a new terminal."
