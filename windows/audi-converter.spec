# PyInstaller spec for the Windows build.
# Build with:
#   pyinstaller windows/audi-converter.spec --noconfirm --clean
# (run from the repo root)
#
# Place ffmpeg.exe, ffprobe.exe and fdkaac.exe in windows/tools/ before
# building. They are bundled next to audi-converter.exe in dist/.
import os
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Resolve everything relative to this spec file so the build works no matter
# what the working directory is at invocation time.
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
REPO_ROOT = os.path.dirname(SPEC_DIR)

binaries = []
for tool in ("ffmpeg", "ffprobe", "fdkaac"):
    p = os.path.join(SPEC_DIR, "tools", f"{tool}.exe")
    if os.path.exists(p):
        binaries.append((p, "."))

# pywebview, pythonnet and clr_loader ship lots of side-DLLs and data files
# (Python.Runtime.dll, edgechromium glue, etc.) that PyInstaller's automatic
# analysis misses — collect_all() pulls everything they declare.
extra_datas = []
extra_binaries = []
extra_hiddenimports = []
for pkg in ("webview", "pythonnet", "clr_loader"):
    d, b, h = collect_all(pkg)
    extra_datas += d
    extra_binaries += b
    extra_hiddenimports += h

a = Analysis(
    [os.path.join(REPO_ROOT, "audi_converter.py")],
    pathex=[REPO_ROOT],
    binaries=binaries + extra_binaries,
    datas=extra_datas,
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ] + extra_hiddenimports,
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
    upx=False,  # UPX compresses .NET assemblies and breaks Python.Runtime.dll
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
    upx=False,  # UPX compresses .NET assemblies and breaks Python.Runtime.dll
    upx_exclude=[],
    name="audi-converter",
)
