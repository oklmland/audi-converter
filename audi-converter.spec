Name:           audi-converter
Version:        1.0.5
Release:        1%{?dist}
Summary:        Web-based video converter for Audi MMI MIB1 head units

License:        MIT
URL:            https://example.invalid/audi-converter
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  make

Requires:       python3
Requires:       python3-fastapi
Requires:       python3-uvicorn
Requires:       python3-multipart
Requires:       python3-gobject
Requires:       gtk4
Requires:       webkitgtk6.0
Requires:       ffmpeg
Recommends:     fdkaac
Recommends:     zenity

%description
A small desktop app with a native GTK4 + WebKit window that re-encodes
videos to the MPEG-4 ASP (Xvid) / AAC LC MP4 profile played by the Audi
MMI MIB1 head unit. Supports drag-and-drop, a queue, live progress with
fps/speed/ETA, and per-file cancellation.

%prep
%setup -q

%build
# Pure-Python, nothing to build.

%install
%make_install PREFIX=%{_prefix}

%files
%{_bindir}/audi-converter
%{_datadir}/applications/audi-converter.desktop
%{python3_sitelib}/audi_converter.py
%{python3_sitelib}/__pycache__/audi_converter.*.pyc

%changelog
* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 1.0.5-1
- Surface the tail of ffmpeg's stderr in the UI when it fails so users
  see why instead of just an opaque exit code.

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 1.0.4-1
- Look for bundled ffmpeg/ffprobe/fdkaac inside _MEIPASS as well —
  PyInstaller 6.x one-folder layouts put bundled binaries under
  _internal/, not next to the .exe.

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 1.0.3-1
- Disable UPX in the Windows PyInstaller spec; UPX corrupts the bundled
  .NET assembly Python.Runtime.dll, breaking pywebview on Windows.
- Fall back to the system browser when pywebview can't initialise on
  Windows, so the app stays usable regardless.

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 1.0.2-1
- Force pywebview's Edge WebView2 backend on Windows (the default
  WinForms backend's pythonnet wiring fails on stock Windows machines).
- PyInstaller spec now collects pywebview/pythonnet/clr_loader data
  files so the bundled .exe ships every required DLL.

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 1.0.1-1
- Fix Windows .exe crash on startup (None stdout/stderr in --windowed
  PyInstaller builds was breaking uvicorn's logging config).

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 1.0.0-1
- Switch from GTK4 to FastAPI + web UI on localhost.
- Switch encoder to MPEG-4 ASP (Xvid) + strict 128k AAC via fdkaac
  (44.1 kHz) for reliable playback on MIB1 High Harman.
- Embed the web UI in a native GTK4 + WebKit window — no longer opens
  the system browser.
