# -*- mode: python ; coding: utf-8 -*-
#
# Hand-tuned build recipe for the macOS .app bundle. Source, not a build
# artifact, so it is version-controlled (see the !exception in .gitignore).
#
#   python3 -m PyInstaller SciHubDaemon-macOS.spec --noconfirm
#
# Named -macOS on purpose: the Windows build calls PyInstaller without a spec,
# which generates a plain "SciHubDaemon.spec" and would clobber this file.


a = Analysis(
    ['scihubdaemon.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    [],
    exclude_binaries=True,
    name='SciHubDaemon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SciHubDaemon',
)
app = BUNDLE(
    coll,
    name='SciHubDaemon.app',
    icon='icon.icns',
    bundle_identifier=None,
)
