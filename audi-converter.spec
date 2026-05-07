Name:           audi-converter
Version:        2.3.2
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
* Thu May 07 2026 totorkmh <kemmeh.victor@gmail.com> - 2.3.2-1
- ★ Stop rewriting tagged files in place. v2.3.0 / v2.3.1 corrupted
  files on a user's FAT32 SD card because the in-place
  ffmpeg-then-os.replace dance turned out not to be safe enough
  against vfat / SD-card flakiness. The "Tagger des fichiers
  existants" panel now writes copies under <folder>/_tagged/ and
  never touches the originals.
- Add a size sanity-check (tagged copy must be ≥ 80 % of source) so
  truncated writes are detected and discarded instead of silently
  declared a success.

* Thu May 07 2026 totorkmh <kemmeh.victor@gmail.com> - 2.3.1-1
- Live progress bar in the "Tagger des fichiers existants" panel.
  Backend broadcasts tag_start / tag_progress / tag_done SSE events as
  it walks the folder; the UI shows the current file + a percentage so
  users aren't left wondering whether the operation is hung on a slow
  SD card.

* Thu May 07 2026 totorkmh <kemmeh.victor@gmail.com> - 2.3.0-1
- Add "Tagger des fichiers existants" panel: scan a folder (e.g. an
  SD card already populated), parse "Artist - Title.mp4" filenames, and
  write the tags into each MP4 in place via ffmpeg -c copy (no
  re-encoding — seconds per file). Files without the separator are
  skipped.

* Thu May 07 2026 totorkmh <kemmeh.victor@gmail.com> - 2.2.0-1
- Add per-job title / artist metadata. Auto-parsed from filenames
  matching "Artist - Title.ext"; editable in the UI before encoding.
  Written into the output MP4's iTunes-style atoms via ffmpeg
  -metadata so head units can display them.

* Wed May 06 2026 totorkmh <kemmeh.victor@gmail.com> - 2.1.2-1
- Fix v2.1.1 regression: `isinstance(value, fastapi.UploadFile)` is False
  for the starlette.datastructures.UploadFile values that
  request.form() returns, so all uploads were silently rejected with
  no error. Switch the isinstance check to starlette's UploadFile.

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
