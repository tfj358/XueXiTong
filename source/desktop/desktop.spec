# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_submodules

source = Path(SPECPATH)
upstream = source.parent / "superstar-checker"
version_file = Path(os.environ["SUPERSTAR_VERSION_FILE"])
data = [
    (str(upstream / "src/superstar_checker/data/font_map_table.json"), "superstar_checker/data"),
    (str(upstream / "LICENSE"), "licenses/superstar-checker"),
    (str(upstream / "THIRD_PARTY_NOTICES.md"), "licenses/superstar-checker"),
    (str(source / "app.ico"), "."),
    (str(source / "version.txt"), "."),
]
a = Analysis(
    [str(source / "desktop.py")],
    pathex=[str(source), str(upstream / "src")],
    binaries=[], datas=data,
    hiddenimports=collect_submodules("Crypto.Cipher")+collect_submodules("Crypto.Util"),
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["mcp", "pytest", "numpy", "matplotlib", "IPython"],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="学习通助手", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    icon=str(source / "app.ico"), version=str(version_file),
)
