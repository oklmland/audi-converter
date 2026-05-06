# Build the Windows distribution of audi-converter.
#
# Usage (from the repo root, in PowerShell):
#   .\windows\build.ps1
#
# Prerequisites:
#   - Python 3.11+ (https://python.org)
#   - Edge WebView2 Runtime (preinstalled on Win11; evergreen download for Win10)
#   - The three bundled binaries placed in windows\tools\:
#       - ffmpeg.exe   from https://www.gyan.dev/ffmpeg/builds/  (essentials build)
#       - ffprobe.exe  (same archive as ffmpeg)
#       - fdkaac.exe   from https://github.com/nu774/fdkaac/releases
#
# Output:
#   dist\audi-converter\audi-converter.exe  (+ all DLLs and bundled tools)

$ErrorActionPreference = "Stop"

# Move to repo root regardless of where the script was invoked from.
Set-Location (Resolve-Path "$PSScriptRoot\..")

# Verify bundled tools are present.
$missing = @()
foreach ($tool in @("ffmpeg.exe", "ffprobe.exe", "fdkaac.exe")) {
    if (-not (Test-Path "windows\tools\$tool")) { $missing += $tool }
}
if ($missing.Count -gt 0) {
    Write-Host "Missing in windows\tools\:" -ForegroundColor Red
    foreach ($m in $missing) { Write-Host "  - $m" }
    Write-Host ""
    Write-Host "Download:"
    Write-Host "  ffmpeg + ffprobe : https://www.gyan.dev/ffmpeg/builds/  (release essentials)"
    Write-Host "  fdkaac           : https://github.com/nu774/fdkaac/releases"
    exit 1
}

# Install / refresh build deps.
Write-Host "==> Installing build dependencies" -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install --upgrade `
    fastapi `
    "uvicorn[standard]" `
    python-multipart `
    pydantic `
    pywebview `
    pyinstaller

# Clean previous build outputs.
if (Test-Path "build")             { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist\audi-converter") { Remove-Item -Recurse -Force "dist\audi-converter" }

# Build.
Write-Host ""
Write-Host "==> Running PyInstaller" -ForegroundColor Cyan
python -m PyInstaller windows\audi-converter.spec --noconfirm --clean

if (-not (Test-Path "dist\audi-converter\audi-converter.exe")) {
    Write-Error "Build failed — audi-converter.exe not found in dist\audi-converter\"
    exit 1
}

Write-Host ""
Write-Host "==> Built: dist\audi-converter\audi-converter.exe" -ForegroundColor Green
Write-Host "    Zip the dist\audi-converter\ folder for distribution."
