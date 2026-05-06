# PyInstaller spec for the Windows build.
# Build with:
#   pyinstaller windows/audi-converter.spec --noconfirm --clean
# (run from the repo root)
#
# Place ffmpeg.exe and fdkaac.exe in windows/tools/ before building.
# They are bundled inside the dist folder. ffprobe is NOT needed —
# audi_converter.py probes video info from `ffmpeg -i` stderr.
import os

block_cipher = None

# Resolve paths relative to this spec file so the build works no matter what
# the current directory is at invocation time.
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
REPO_ROOT = os.path.dirname(SPEC_DIR)

binaries = []
for tool in ("ffmpeg", "fdkaac"):
    p = os.path.join(SPEC_DIR, "tools", f"{tool}.exe")
    if os.path.exists(p):
        binaries.append((p, "."))

a = Analysis(
    [os.path.join(REPO_ROOT, "audi_converter.py")],
    pathex=[REPO_ROOT],
    binaries=binaries,
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="audi-converter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX can corrupt some DLLs; size win isn't worth the risk
    console=False,  # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="audi-converter",
)
