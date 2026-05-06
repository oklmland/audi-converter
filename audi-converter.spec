Name:           audi-converter
Version:        2.0.0
Release:        1%{?dist}
Summary:        Video converter for the Audi MMI MIB1 head unit

License:        MIT
URL:            https://github.com/oklmland/audi-converter
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  make

Requires:       python3
Requires:       python3-tkinter
Requires:       ffmpeg
Recommends:     fdkaac

%description
A small native desktop app (Tkinter) that re-encodes videos to the
MPEG-4 ASP (Xvid) / AAC LC MP4 profile played by the Audi MMI MIB1
head unit. Multi-file queue, live progress with fps / speed / ETA,
per-file cancellation, automatic aspect-ratio handling.

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
* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 2.0.0-1
- Rewrite as a native Tkinter desktop app. No more FastAPI / uvicorn /
  pywebview / GTK + WebKit — just stdlib + Tkinter, runs identically on
  Linux and Windows.
- Drop ffprobe dependency: probe video info from "ffmpeg -i" stderr.
  Cuts the Windows bundle size by ~100 MB.
- Surface ffmpeg's stderr tail in the UI when it fails.

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 1.0.4-1
- Look for bundled ffmpeg/ffprobe/fdkaac inside _MEIPASS as well —
  PyInstaller 6.x one-folder layouts put bundled binaries under
  _internal/, not next to the .exe.

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 1.0.0-1
- Initial release: MPEG-4 ASP (Xvid) + 128k AAC via fdkaac (44.1 kHz)
  for reliable playback on MIB1 High Harman.
