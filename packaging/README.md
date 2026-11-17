# Packaging imon

imon is pure Python (3.9+) with one dependency (`requests`) and runs on
Windows, Linux and macOS.

## From source (any platform)

    pip install .            # installs the 'imon' command
    pipx install .           # isolated install for one user

## Windows executable and installer

    .\packaging\windows\build.ps1                # PyInstaller -> dist\imon\imon.exe + installer
    .\packaging\windows\build.ps1 -NoInstaller   # executable only
    .\packaging\windows\build.ps1 -Portable      # skip Inno Setup, make the portable ZIP

With Inno Setup 6 installed (free: https://jrsoftware.org/isdl.php, or
`winget install JRSoftware.InnoSetup` - the script offers to do this) you get
`dist\imon-<version>-setup.exe`, a classic wizard with "add to PATH" and Start
Menu entries; it installs per user and does not ask for administrator rights.
Without Inno Setup (or with `-Portable`) the script produces
`dist\imon-<version>-windows-portable.zip`: unzip anywhere and run `install.ps1`
for a per-user install (Start Menu, PATH, `uninstall.ps1`), or simply run
`imon.exe` from the folder.

The build creates its own virtual environment (`.venv-build`), so it does not
depend on what is installed on the machine.

## Linux

    ./packaging/linux/build.sh

Run it on Linux (PyInstaller cannot cross-compile), on the oldest system you
want the result to run on. It needs python3 with the venv module and
binutils (`sudo apt install python3-venv binutils`). It produces
`dist/imon-<version>-linux-<arch>.tar.gz`.
