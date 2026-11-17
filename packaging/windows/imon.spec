# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build of imon: one console executable in a folder (dist/imon/).
#
#   pip install pyinstaller
#   pyinstaller packaging/windows/imon.spec
#
# The version comes from pyproject.toml, so there is one place to change it.
import os
import re

here = os.path.dirname(os.path.abspath(SPEC))
root = os.path.abspath(os.path.join(here, "..", ".."))
src = os.path.join(root, "src")

with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as f:
    version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', f.read()).group(1)
nums = [int(x) for x in re.findall(r"\d+", version)[:4]]
nums += [0] * (4 - len(nums))
tup = ", ".join(str(n) for n in nums)

os.makedirs(os.path.join(root, "build"), exist_ok=True)
vfile = os.path.join(root, "build", "version_info.txt")
with open(vfile, "w", encoding="utf-8") as f:
    f.write(
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(filevers=(%s), prodvers=(%s), mask=0x3f, flags=0x0,\n"
        "                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
        "  kids=[\n"
        "    StringFileInfo([StringTable('040904B0', [\n"
        "      StringStruct('CompanyName', 'Alejandro Castaneda'),\n"
        "      StringStruct('FileDescription', 'imon - internet and website availability monitor'),\n"
        "      StringStruct('FileVersion', '%s'),\n"
        "      StringStruct('ProductName', 'imon'),\n"
        "      StringStruct('ProductVersion', '%s'),\n"
        "      StringStruct('LegalCopyright', 'Copyright (c) 2026 Alejandro Castaneda. MIT license.')])]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n" % (tup, tup, version, version)
    )

a = Analysis(
    [os.path.join(here, "entry_cli.py")],
    pathex=[src],
    binaries=[],
    datas=[(os.path.join(root, "LICENSE"), ".")],
    hiddenimports=["imon.cli", "imon.monitor", "imon.server", "imon.pdf", "imon.speedtest"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest"],
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="imon",
          console=True, icon=None, version=vfile)
COLLECT(exe, a.binaries, a.zipfiles, a.datas,
        strip=False, upx=False, name="imon")
