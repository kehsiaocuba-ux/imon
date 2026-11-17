# Builds the Windows executable and an installer for imon.
#   .\packaging\windows\build.ps1               PyInstaller + installer
#   .\packaging\windows\build.ps1 -NoInstaller  executable only
#   .\packaging\windows\build.ps1 -Portable     skip Inno Setup, make the portable ZIP
#
# The build uses its own virtual environment (.venv-build), so it does not depend on
# what is installed on the machine. The version is read from pyproject.toml.
#   * With Inno Setup 6 (free, https://jrsoftware.org/isdl.php or: winget install JRSoftware.InnoSetup)
#     -> dist\imon-<version>-setup.exe, a classic setup wizard (per user, no administrator rights).
#   * Without Inno Setup the script produces dist\imon-<version>-windows-portable.zip, which
#     contains the program plus install.ps1 / uninstall.ps1 (per-user install, no admin rights).
param([switch]$NoInstaller, [switch]$Portable)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

$m = Select-String -Path (Join-Path $root "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $m) { throw "version not found in pyproject.toml" }
$version = $m.Matches[0].Groups[1].Value
Write-Host "Building imon $version"

if (-not (Test-Path ".venv-build\Scripts\python.exe")) { python -m venv .venv-build }
$py = Join-Path $root ".venv-build\Scripts\python.exe"
& $py -m pip install --upgrade pip | Out-Null
& $py -m pip install -e . pyinstaller | Out-Null
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

& $py -m PyInstaller --noconfirm --clean packaging\windows\imon.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "Smoke test:"
$help = & dist\imon\imon.exe --help
if ($LASTEXITCODE -ne 0) { throw "imon.exe --help failed" }
$help | Select-Object -First 3
Copy-Item packaging\windows\install.ps1, packaging\windows\uninstall.ps1 dist\imon\

if ($NoInstaller) { Write-Host "Done. Executable in dist\imon\"; exit 0 }

function Find-Iscc {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"     # per-user installation
    )
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$iscc = $null
if (-not $Portable) {
    $iscc = Find-Iscc
    if (-not $iscc -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        $answer = Read-Host "Inno Setup 6 is not installed. Install it now with winget? [Y/n]"
        if ($answer -eq "" -or $answer -match "^[Yy]") {
            winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements
            $iscc = Find-Iscc
        }
    }
}

if ($iscc) {
    & $iscc "/DAppVersion=$version" packaging\windows\imon.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "Installer: dist\imon-$version-setup.exe"
} else {
    Write-Host "Inno Setup not available - building the portable ZIP instead."
    $zip = "dist\imon-$version-windows-portable.zip"
    Compress-Archive -Path dist\imon\* -DestinationPath $zip -Force
    Write-Host "Portable package: $zip"
    Write-Host "  unzip it anywhere and run install.ps1 (per-user install: Start Menu, PATH, uninstaller)"
    Write-Host "  or just run imon.exe from the folder."
}
Write-Host "Done. Executable in dist\imon\"
