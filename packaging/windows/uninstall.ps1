# Removes a per-user installation made by install.ps1 (program files, PATH entry, shortcuts).
# Your imon data (monitoring history and settings) is kept.
$ErrorActionPreference = "Stop"
$target = Join-Path $env:LOCALAPPDATA "Programs\imon"
$menu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\imon"

$path = [Environment]::GetEnvironmentVariable("Path", "User")
$newPath = (($path -split ";") | Where-Object { $_ -and $_ -ne $target }) -join ";"
[Environment]::SetEnvironmentVariable("Path", $newPath, "User")
Remove-Item -Recurse -Force $menu -ErrorAction SilentlyContinue
Write-Host "Removing $target"
# delete the folder after this script exits (it is running from inside it)
Start-Process cmd.exe -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$target`"" -WindowStyle Hidden
Write-Host "imon was uninstalled. Your monitoring data was kept."
