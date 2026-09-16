# -*- mode: python ; coding: utf-8 -*-
import os
import re

# Версия агента берётся из файла VERSION рядом со spec-ом и на этапе сборки
# превращается в модуль _version.py, который PyInstaller вшивает в exe
# (client_agent.py импортирует его только в frozen-режиме). Файл _version.py
# генерируется при каждой сборке и в git не хранится.
with open(os.path.join(SPECPATH, "VERSION"), "r", encoding="utf-8") as _f:
    CLIENT_VERSION = _f.read().strip()
if not re.fullmatch(r"\d+(\.\d+)+", CLIENT_VERSION):
    raise SystemExit(f"client/VERSION must look like 1.4 or 1.4.2, got {CLIENT_VERSION!r}")
with open(os.path.join(SPECPATH, "_version.py"), "w", encoding="utf-8") as _f:
    _f.write(f'CLIENT_VERSION = "{CLIENT_VERSION}"\n')


a = Analysis(
    ['client_agent.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['_version'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='client_agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
