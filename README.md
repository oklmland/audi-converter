# Audi MMI MIB1 Converter

Native desktop app that re-encodes videos to a profile the Audi MMI
MIB1 (High Harman variant) head unit reliably plays from USB / SD:

- Container: MP4, `+faststart`
- Video: **MPEG-4 ASP (Xvid)**, yuv420p, **720×480**, 25 fps,
  **2000 kbps** target, 2 B-frames
- Audio: AAC LC, **44.1 kHz**, stereo, **128 kbps strict CBR** (via
  [`fdkaac`](https://github.com/nu774/fdkaac))

The MIB1 High Harman decoder is strict: native ffmpeg AAC overshoots
128 kbps and crackles, so the app pipes audio through `fdkaac` for true
CBR. If `fdkaac` is missing it falls back to native AAC at 120 kbps to
stay safely under the limit.

Aspect-ratio handling is automatic:

- The app runs `cropdetect` on each input. Letterbox / pillarbox bars
  baked into the master are stripped before re-encoding.
- After that, DAR ≤ 2.0 (4:3, 16:9) is **crop-to-filled** to 720×480
  with no black bars; DAR > 2.0 (cinematic 21:9 / 2.39:1) is
  **scale-and-padded** so the full frame is preserved.

## Stack

Plain Python + Tkinter. No web server, no embedded browser, no
PyQt / GTK / pywebview. The whole thing is one
[`audi_converter.py`](audi_converter.py) file.

## Download

Pre-built releases live on
**[the GitHub Releases page](https://github.com/oklmland/audi-converter/releases/latest)**
— pick:

- **Windows** : `audi-converter-windows.zip` — extract anywhere, run
  `audi-converter.exe`. ffmpeg + fdkaac are bundled.
- **Fedora** : `audi-converter-X.Y.Z-1.fc44.noarch.rpm` —
  `sudo dnf install ./audi-converter-*.rpm`.

## Run from source (Linux)

```bash
sudo dnf install python3 python3-tkinter ffmpeg fdkaac
make run
# or: python3 audi_converter.py
```

## Install locally (Linux)

```bash
sudo make install
```

## Build the RPM (Fedora)

```bash
sudo dnf install rpm-build python3-devel make
make rpm
sudo dnf install ~/rpmbuild/RPMS/noarch/audi-converter-*.noarch.rpm
```

## Build for Windows

The Windows `.exe` is built with PyInstaller. The repo also ships a
GitHub Actions workflow that does this on every tag push.

To do it manually on a Windows box with Python 3.11+ :

1. Drop `ffmpeg.exe` and `fdkaac.exe` into `windows\tools\` (sources
   listed in `windows\tools\.gitkeep`).
2. From the repo root, in PowerShell :

   ```powershell
   .\windows\build.ps1
   ```

   Output ends up in `dist\audi-converter\`. Zip that folder to ship.

## Usage

Launch *Audi MMI MIB1 Converter* (from the app menu, or `audi-converter`
in a terminal, or `audi-converter.exe` on Windows). A native window
opens. Click *Ajouter des fichiers…*, pick the output folder, and
watch them convert one at a time with live fps / speed / ETA. Output
files land in `<output-dir>/<original>.mp4`.
