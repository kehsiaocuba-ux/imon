#!/usr/bin/env bash
# Builds the Linux executable (PyInstaller) and a tar.gz from this checkout.
#
#   ./packaging/linux/build.sh
#
# Needs: python3 (3.9+) with the venv module and objdump (binutils).
# On Debian/Ubuntu: sudo apt install python3-venv binutils
#
# Produces dist/imon-<version>-linux-<arch>.tar.gz. PyInstaller cannot
# cross-compile, so this must run on Linux, on the oldest system you want
# the result to run on (the glibc version of the build machine is the minimum).
# The version is read from pyproject.toml.
set -euo pipefail

if [ "$(uname -s)" != "Linux" ]; then
    echo "This script builds the Linux package and must run on Linux." >&2
    exit 1
fi

cd "$(dirname "$0")/../.."

version=$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' pyproject.toml | head -1)
[ -n "$version" ] || { echo "version not found in pyproject.toml" >&2; exit 1; }

python3 -m venv .venv-build
. .venv-build/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e . pyinstaller

arch=$(uname -m)
name="imon-${version}-linux-${arch}"

rm -rf build dist
python -m PyInstaller --noconfirm --clean packaging/windows/imon.spec

echo "Smoke test:"
help_text=$(./dist/imon/imon --help)
echo "$help_text" | head -3

glibc=$( (for f in dist/imon/imon $(find dist/imon/_internal -type f \( -name '*.so' -o -name '*.so.*' \)); do
              objdump -T "$f" 2>/dev/null | grep -o 'GLIBC_[0-9.]*' || true
          done) | sed 's/GLIBC_//' | sort -V -u | tail -1 || true)
[ -n "$glibc" ] || glibc="unknown"

stage="dist/${name}"
mkdir -p "$stage"
cp -a dist/imon/. "$stage"/
cp LICENSE "$stage"/

cat > "$stage/README.txt" <<'EOT'
imon @VERSION@ for Linux (@ARCH@)

Run it from this folder, nothing to install:

    ./imon --help       # all commands and options
    ./imon watch        # monitor continuously
    ./imon serve        # web dashboard
    ./imon report       # report in the terminal

Keep imon and the _internal folder together.

Optional per-user install:

    mkdir -p ~/.local/opt ~/.local/bin
    cp -r . ~/.local/opt/imon
    ln -sf ~/.local/opt/imon/imon ~/.local/bin/imon

To remove it, delete that link and the folder ~/.local/opt/imon.

Compatibility: the newest system library this build needs is glibc @GLIBC@,
so it runs on Linux systems with glibc @GLIBC@ or newer. On older systems run
imon from source instead: clone the repository and run pip install .
(Python 3.9+).

License: MIT, see LICENSE. Source: https://github.com/kehsiaocuba-ux/imon
Verify the download against checksums.txt: sha256sum -c checksums.txt
EOT
sed -i -e "s/@VERSION@/${version}/g" -e "s/@ARCH@/${arch}/g" -e "s/@GLIBC@/${glibc}/g" "$stage/README.txt"

tar -C dist --owner=0 --group=0 -czf "dist/${name}.tar.gz" "$name"

echo "Package: dist/${name}.tar.gz (needs glibc ${glibc} or newer)"
sha256sum "dist/${name}.tar.gz"
