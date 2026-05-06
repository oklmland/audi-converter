Name:           audi-converter
Version:        2.1.1
Release:        1%{?dist}
Summary:        Video converter for the Audi MMI MIB1 head unit

License:        MIT
URL:            https://github.com/oklmland/audi-converter
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  make

Requires:       python3
Requires:       python3-fastapi
Requires:       python3-uvicorn
Requires:       python3-multipart
Requires:       python3-tkinter
Requires:       ffmpeg
Recommends:     fdkaac
Recommends:     zenity

%description
A small desktop app that re-encodes videos to the MPEG-4 ASP (Xvid) /
AAC LC MP4 profile played by the Audi MMI MIB1 head unit. The web UI
(FastAPI + SSE) is served on localhost; the user's default browser is
opened automatically and a small Tkinter control window is shown so the
app can be quit cleanly. Multi-file queue, drag-and-drop, live progress
with fps/speed/ETA, per-file cancellation, automatic aspect-ratio
handling.

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
* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 2.1.1-1
- Fix silent file truncation on upload. Starlette 0.38+ defaults
  max_part_size to 1 MiB, so large videos were cut to 1 MB and ffmpeg
  rejected them ("moov atom not found"). Bypass FastAPI's File()
  declaration and call request.form(max_part_size=64 GiB) directly.

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 2.1.0-1
- Bring back the v1.0.5 web UI (FastAPI + Tailwind + drag-and-drop +
  SSE), but no longer try to embed it in a native window. The browser
  opens on launch; a tiny Tk control window with the URL and a Quitter
  button replaces the GTK4+WebKit / pywebview embedding.
- Drop ffprobe usage: probe video info from `ffmpeg -i` stderr
  (saves ~97 MB on the Windows bundle).

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 2.0.0-1
- Stripped-down Tkinter rewrite to escape the pywebview/pythonnet
  Windows packaging mess.

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 1.0.0-1
- Initial release: MPEG-4 ASP (Xvid) + 128k AAC via fdkaac (44.1 kHz)
  for reliable playback on MIB1 High Harman.
