# Audi MMI MIB1 Converter

Small desktop app with a native GTK4 + WebKit window that re-encodes
videos to a profile the Audi MMI MIB1 (High Harman variant) head unit
reliably plays from USB/SD:

- Container: MP4, `+faststart`
- Video: **MPEG-4 ASP (Xvid)**, yuv420p, **720×480**, 25 fps,
  **2000 kbps** target, 2 B-frames
- Audio: AAC LC, **44.1 kHz**, stereo, **128 kbps strict CBR** (via
  [`fdkaac`](https://github.com/nu774/fdkaac))

The MIB1 High Harman decoder is strict: native ffmpeg AAC overshoots
128 kbps and crackles, so the tool pipes the audio through `fdkaac` for
true CBR. If `fdkaac` is missing it falls back to native AAC at 120 kbps
to stay safely under the limit (a hint in the UI tells you how to
install it).

Aspect ratio handling is automatic from the source DAR:

- DAR ≤ 2.0 (4:3, 16:9, mild crops): **crop-to-fill** to 720×480 — no
  black bars.
- DAR > 2.0 (cinematic 21:9 / 2.39:1): **scale-and-pad** keeping the
  full frame, with black bars top/bottom — preserves the picture.

Frontend: HTML + Tailwind (CDN), live progress via Server-Sent Events,
embedded in a GTK4 + WebKit native window. Backend: FastAPI + uvicorn
driving `ffmpeg` per file in an asyncio worker.

## Run from source

```bash
sudo dnf install python3-fastapi python3-uvicorn python3-multipart \
                 python3-gobject gtk4 webkitgtk6.0 ffmpeg fdkaac
make run
# or: python3 audi_converter.py
```

Pass `--no-window` to run headless and use a regular browser instead.

## Install locally

```bash
sudo make install
```

## Build an RPM (Fedora)

```bash
sudo dnf install rpm-build python3-devel make
make rpm
sudo dnf install ~/rpmbuild/RPMS/noarch/audi-converter-1.0.0-1.*.noarch.rpm
```

## Build for Windows

There's a separate, self-contained build for Windows that ships
`audi-converter.exe` plus bundled `ffmpeg.exe` / `ffprobe.exe` /
`fdkaac.exe` — no extra install needed on the target machine (other than
the Edge WebView2 Runtime, which is preinstalled on Windows 11 and a
free evergreen download on Windows 10).

On a Windows box with Python 3.11+:

1. Drop the three binaries into `windows\tools\`:
   - `ffmpeg.exe` and `ffprobe.exe` from a release-essentials build at
     <https://www.gyan.dev/ffmpeg/builds/>
   - `fdkaac.exe` from <https://github.com/nu774/fdkaac/releases>
2. Run the build script from the repo root:

   ```powershell
   .\windows\build.ps1
   ```

   The script installs PyInstaller + pywebview, then bundles everything
   under `dist\audi-converter\`. Zip that folder to ship.

The Windows build uses **pywebview** (Edge WebView2) for the native
window; the Linux RPM continues to use GTK4 + WebKit. Same Python
backend, same encoder profile, same UI.

## Usage

Launch *Audi MMI MIB1 Converter* from your app menu (or run
`audi-converter` in a terminal). A native window opens. Drop video
files on it, pick an output folder, and watch them convert one at a
time with live fps, speed, ETA and current size. Output files are
named `<original>.mp4`.
