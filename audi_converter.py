#!/usr/bin/env python3
"""Audi MMI MIB1 video converter — native Tkinter desktop app.

Re-encodes videos to the profile the Audi MMI MIB1 (High Harman) head unit
plays from USB / SD: MPEG-4 ASP (Xvid) at 720x480 25 fps 2000 kbps, AAC LC
stereo at 128 kbps strict CBR (via fdkaac when available, falling back to
the native ffmpeg AAC at 120 kbps to stay under the 128 kbps limit).

Stdlib only + Tkinter for the UI. Spawns ffmpeg / fdkaac as subprocesses;
ships them next to the executable on Windows (PyInstaller bundle), expects
them on PATH on Linux (RPM runtime dependencies).
"""
from __future__ import annotations

import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterator, Optional

# ----- Output profile ----------------------------------------------------

MAX_W, MAX_H = 720, 480       # NTSC frame, fits the 800x480 MIB1 screen
V_BITRATE = "2000k"           # max allowed by Audi spec
A_BITRATE_FDKAAC = 128        # strict CBR via fdkaac (kbps)
A_BITRATE_NATIVE = "120k"     # ffmpeg native aac overshoots ~3-5%, undertarget
A_RATE = "44100"              # 44.1 kHz — the Harman decoder distorts at 48
TARGET_FPS = 25
XVID_BFRAMES = "2"
CINEMATIC_DAR_THRESHOLD = 2.0  # > this = preserve aspect with bars (cinema)

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv",
              ".m4v", ".mpg", ".mpeg", ".ogv", ".3gp", ".ts", ".m2ts")

# ----- Subprocess helpers -----------------------------------------------

# Hide console windows that would otherwise pop up for every ffmpeg call
# in PyInstaller --windowed builds.
if sys.platform == "win32":
    _CFLAGS = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
else:
    _CFLAGS = 0


def _spawn(cmd: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(cmd, creationflags=_CFLAGS, **kw)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, creationflags=_CFLAGS, **kw)


# ----- Tool resolution --------------------------------------------------

def _tool(name: str) -> str:
    """Resolve a tool's path. Frozen builds prefer a binary shipped in the
    bundle; otherwise fall back to PATH. Returns the bare name when nothing
    is found, so spawn raises FileNotFoundError as before.

    PyInstaller 6.x one-folder layouts put bundled binaries under _internal/
    rather than next to the .exe — check both.
    """
    if getattr(sys, "frozen", False):
        ext = ".exe" if sys.platform == "win32" else ""
        for base in (getattr(sys, "_MEIPASS", None),
                     str(Path(sys.executable).parent)):
            if not base:
                continue
            cand = Path(base) / f"{name}{ext}"
            if cand.exists():
                return str(cand)
    return shutil.which(name) or name


def _have(name: str) -> bool:
    p = _tool(name)
    return os.path.isabs(p) or shutil.which(p) is not None


# ----- Video probing (ffmpeg, no ffprobe) -------------------------------

# `ffmpeg -hide_banner -i <input>` exits with code 1 because we give no
# output, but writes the same stream info that ffprobe would print to
# stderr. Parsing it lets us drop a 97 MB ffprobe.exe from the Windows
# bundle.

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")
_VID_RE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_DAR_RE = re.compile(r"DAR\s+(\d+):(\d+)")
_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def probe(path: Path) -> dict:
    """Return width / height / duration / dar_num / dar_den, best effort."""
    try:
        r = _run([_tool("ffmpeg"), "-hide_banner", "-i", str(path)],
                 capture_output=True, timeout=20, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    s = r.stderr.decode("utf-8", "replace")
    info: dict = {}
    if (m := _DUR_RE.search(s)):
        h, mn, sec = m.groups()
        info["duration"] = int(h) * 3600 + int(mn) * 60 + float(sec)
    if (m := _VID_RE.search(s)):
        info["width"] = int(m.group(1))
        info["height"] = int(m.group(2))
    if (m := _DAR_RE.search(s)):
        info["dar_num"] = int(m.group(1))
        info["dar_den"] = int(m.group(2))
    elif "width" in info:
        g = math.gcd(info["width"], info["height"]) or 1
        info["dar_num"] = info["width"] // g
        info["dar_den"] = info["height"] // g
    return info


def detect_active_crop(path: Path, w: int, h: int, duration: float
                       ) -> Optional[tuple[int, int, int, int]]:
    """Run a fast cropdetect on a 2-second sample to strip baked-in bars."""
    if w <= 0 or h <= 0:
        return None
    seek = max(0.0, min(duration / 2 if duration else 5.0, 600.0))
    try:
        r = _run(
            [_tool("ffmpeg"), "-hide_banner", "-nostdin",
             "-ss", f"{seek:.2f}", "-i", str(path), "-t", "2",
             "-vf", "cropdetect=24:2:0", "-an", "-f", "null", "-"],
            capture_output=True, timeout=30, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    matches = list(_CROP_RE.finditer(r.stderr.decode("utf-8", "replace")))
    if not matches:
        return None
    cw, ch, cx, cy = (int(g) for g in matches[-1].groups())
    cw &= ~1
    ch &= ~1
    cx &= ~1
    cy &= ~1
    if cw <= 0 or ch <= 0 or cw + cx > w or ch + cy > h:
        return None
    if w - cw < 4 and h - ch < 4:
        return None
    return cw, ch, cx, cy


# ----- Filter chain -----------------------------------------------------

def build_video_filter(dar: float,
                       crop: Optional[tuple[int, int, int, int]] = None) -> str:
    """Pick the right scale/crop/pad chain for the source's effective DAR."""
    crop_to_fill = (
        f"crop=w='if(gt(iw/ih\\,{MAX_W}/{MAX_H})\\,trunc(ih*{MAX_W}/{MAX_H}/2)*2\\,iw)'"
        f":h='if(gt(iw/ih\\,{MAX_W}/{MAX_H})\\,ih\\,trunc(iw*{MAX_H}/{MAX_W}/2)*2)',"
        f"scale={MAX_W}:{MAX_H},setsar=1"
    )
    if crop is not None:
        cw, ch, cx, cy = crop
        return f"crop={cw}:{ch}:{cx}:{cy},{crop_to_fill}"
    if dar > CINEMATIC_DAR_THRESHOLD:
        return (f"scale={MAX_W}:{MAX_H}:force_original_aspect_ratio=decrease"
                f":force_divisible_by=2,"
                f"pad={MAX_W}:{MAX_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    return crop_to_fill


def effective_dar(info: dict,
                  crop: Optional[tuple[int, int, int, int]] = None) -> float:
    """DAR of the active picture, after stripping baked-in bars if any."""
    n = info.get("dar_num") or 16
    d = info.get("dar_den") or 9
    base = (n / d) if d else 16 / 9
    if crop is not None:
        cw, ch, _, _ = crop
        w = int(info.get("width") or 0)
        h = int(info.get("height") or 0)
        if w > 0 and h > 0 and ch > 0:
            pixel_aspect = base * (h / w)
            return (cw / ch) * pixel_aspect
    return base


# ----- ffmpeg arg builders ----------------------------------------------

def _xvid_args(vf: str) -> list[str]:
    return ["-c:v", "libxvid", "-b:v", V_BITRATE,
            "-bf", XVID_BFRAMES, "-pix_fmt", "yuv420p",
            "-vf", vf, "-r", str(TARGET_FPS)]


def _native_aac_args() -> list[str]:
    return ["-c:a", "aac", "-b:a", A_BITRATE_NATIVE, "-ar", A_RATE,
            "-ac", "2", "-aac_coder", "twoloop"]


# ----- Job ---------------------------------------------------------------

class Job:
    def __init__(self, src: Path) -> None:
        self.src = src
        self.size = src.stat().st_size if src.exists() else 0
        self.status = "pending"  # pending | running | done | error | cancelled
        self.progress = 0.0
        self.fps: Optional[float] = None
        self.speed: Optional[float] = None
        self.eta: Optional[float] = None
        self.message = ""
        self.cancelled = False
        self.proc: Optional[subprocess.Popen] = None


# ----- ffmpeg progress reading ------------------------------------------

_TIME_RE = re.compile(rb"time=(\d+):(\d+):(\d+\.\d+)")
_FPS_RE = re.compile(rb"fps=\s*([\d.]+)")
_SPEED_RE = re.compile(rb"speed=\s*([\d.]+)\s*x")


def _read_lines(stream) -> Iterator[bytes]:
    """Yield bytes-fragments split on either \\r or \\n (ffmpeg uses \\r)."""
    buf = b""
    while True:
        chunk = stream.read(256)
        if not chunk:
            if buf:
                yield buf
            return
        buf += chunk
        while True:
            i = -1
            for sep in (b"\r", b"\n"):
                j = buf.find(sep)
                if j != -1 and (i == -1 or j < i):
                    i = j
            if i == -1:
                break
            line, buf = buf[:i], buf[i + 1:]
            if line:
                yield line


def _run_ffmpeg(job: Job, cmd: list[str], duration: float,
                on_progress: Callable[[Job], None]) -> tuple[int, str]:
    """Spawn ffmpeg, parse progress, return (rc, stderr_tail).

    rc == -1 means the binary couldn't be spawned at all.
    """
    try:
        proc = _spawn(cmd, stdout=subprocess.DEVNULL,
                      stderr=subprocess.PIPE)
    except FileNotFoundError:
        return -1, "ffmpeg introuvable"
    job.proc = proc

    tail: list[str] = []
    last_emit = 0.0
    assert proc.stderr is not None

    for line_b in _read_lines(proc.stderr):
        if job.cancelled:
            try:
                proc.terminate()
            except OSError:
                pass
            break
        line = line_b.decode("utf-8", "replace")
        tail.append(line)
        if len(tail) > 80:
            del tail[:-80]

        if duration > 0 and (m := _TIME_RE.search(line_b)):
            done = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                    + float(m.group(3)))
            job.progress = max(0.0, min(1.0, done / duration))
            if (mf := _FPS_RE.search(line_b)):
                job.fps = float(mf.group(1))
            if (ms := _SPEED_RE.search(line_b)):
                job.speed = float(ms.group(1))
                if job.speed > 0:
                    job.eta = (duration - done) / job.speed
            now = time.monotonic()
            if now - last_emit >= 0.15 or job.progress >= 1.0:
                last_emit = now
                on_progress(job)

    rc = proc.wait()
    if rc != 0 and tail:
        last = [ln.strip() for ln in tail[-12:] if ln.strip()]
        return rc, " | ".join(last[-5:])
    return rc, ""


# ----- Conversion pipeline ----------------------------------------------

def convert(job: Job, dst: Path,
            on_progress: Callable[[Job], None]) -> None:
    """Run probe → cropdetect → encode → optional mux. Updates job in place."""
    info = probe(job.src)
    if not info or "width" not in info:
        job.status = "error"
        job.message = "Impossible de lire la vidéo (probe échoué)"
        return

    crop = detect_active_crop(
        job.src, info["width"], info["height"], info.get("duration", 0.0)
    )
    dar = effective_dar(info, crop)
    vf = build_video_filter(dar, crop)
    duration = float(info.get("duration") or 0.0)

    if _have("fdkaac"):
        _convert_with_fdkaac(job, dst, vf, duration, on_progress)
    else:
        _convert_singlepass(job, dst, vf, duration, on_progress)


def _convert_singlepass(job: Job, dst: Path, vf: str, duration: float,
                        on_progress: Callable[[Job], None]) -> None:
    cmd = [_tool("ffmpeg"), "-y", "-nostdin", "-i", str(job.src),
           *_xvid_args(vf), *_native_aac_args(),
           "-movflags", "+faststart", str(dst)]
    rc, err = _run_ffmpeg(job, cmd, duration, on_progress)
    _finish(job, dst, rc, err)


def _convert_with_fdkaac(job: Job, dst: Path, vf: str, duration: float,
                         on_progress: Callable[[Job], None]) -> None:
    """Video-only Xvid in foreground; audio via ffmpeg→fdkaac in parallel; mux."""
    tmp = Path(tempfile.gettempdir()) / "audi-converter-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    vid = tmp / f"{uuid.uuid4().hex}.video.mp4"
    aud = tmp / f"{uuid.uuid4().hex}.audio.m4a"

    audio_done = threading.Event()
    audio_ok = [False]

    def encode_audio() -> None:
        ff = fdk = None
        try:
            ff = _spawn(
                [_tool("ffmpeg"), "-y", "-nostdin", "-i", str(job.src),
                 "-vn", "-f", "wav", "-ar", A_RATE, "-ac", "2", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            fdk = _spawn(
                [_tool("fdkaac"), "-b", str(A_BITRATE_FDKAAC), "-p", "2",
                 "-", "-o", str(aud)],
                stdin=ff.stdout, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ff.stdout is not None:
                ff.stdout.close()  # let fdkaac see EOF when ffmpeg exits
            audio_ok[0] = (ff.wait() == 0 and fdk.wait() == 0)
        except (FileNotFoundError, OSError):
            audio_ok[0] = False
        finally:
            for p in (ff, fdk):
                if p is not None and p.poll() is None:
                    try:
                        p.terminate()
                    except OSError:
                        pass
            audio_done.set()

    audio_t = threading.Thread(target=encode_audio, daemon=True)
    audio_t.start()

    try:
        cmd = [_tool("ffmpeg"), "-y", "-nostdin", "-i", str(job.src),
               *_xvid_args(vf), "-an", "-f", "mp4", str(vid)]
        rc, err = _run_ffmpeg(job, cmd, duration, on_progress)

        if rc == -1:
            job.status, job.message = "error", err
            return
        if job.cancelled:
            job.status, job.message = "cancelled", "Annulé"
            return
        if rc != 0:
            detail = f": {err}" if err else ""
            job.status = "error"
            job.message = f"ffmpeg a échoué (code {rc}){detail}"
            return

        audio_done.wait()
        if not audio_ok[0]:
            job.status, job.message = "error", "fdkaac a échoué"
            return

        mux = _run(
            [_tool("ffmpeg"), "-y", "-nostdin",
             "-i", str(vid), "-i", str(aud),
             "-c", "copy", "-movflags", "+faststart", str(dst)],
            capture_output=True, check=False,
        )
        if mux.returncode != 0:
            err_lines = mux.stderr.decode("utf-8", "replace").strip().splitlines()
            tail = " | ".join(err_lines[-3:]) or "—"
            job.status = "error"
            job.message = f"Mux a échoué : {tail}"
            return

        job.status, job.message = "done", str(dst)
    finally:
        for p in (vid, aud):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _finish(job: Job, dst: Path, rc: int, err: str) -> None:
    if rc == -1:
        job.status, job.message = "error", err
        return
    if job.cancelled:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        job.status, job.message = "cancelled", "Annulé"
        return
    if rc == 0:
        job.status, job.message = "done", str(dst)
    else:
        detail = f": {err}" if err else ""
        job.status = "error"
        job.message = f"ffmpeg a échoué (code {rc}){detail}"


# ----- Helpers ----------------------------------------------------------

def _default_output_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return home / "Videos" / "AudiMMI"
    return home / "Vidéos" / "AudiMMI"


def _fmt_duration(s: Optional[float]) -> str:
    if s is None or not math.isfinite(s) or s < 0:
        return "—"
    s = int(s)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fmt_size(b: int) -> str:
    if not b or b <= 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(b)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}" if i else f"{int(f)} {units[i]}"


# ----- UI ---------------------------------------------------------------

class App:
    PROFILE = ("720×480 · MPEG-4 ASP (Xvid) · MP4   |   "
               "2000k v · AAC LC 128k CBR · 25 fps · 44.1 kHz")

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Audi MMI MIB1 Converter")
        self.root.geometry("820x560")
        try:
            self.root.minsize(720, 480)
        except tk.TclError:
            pass

        # Use the modern Windows theme when available, otherwise stay native.
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue

        self.jobs: list[Job] = []
        self.events: queue.Queue = queue.Queue()
        self.output_dir = _default_output_dir()
        self.worker: Optional[threading.Thread] = None
        self.current: Optional[Job] = None

        self._build_ui()
        self.root.after(0, self._preflight)
        self.root.after(80, self._poll)

    def _build_ui(self) -> None:
        hdr = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Audi MMI MIB1 — Convertisseur",
                  font=("TkDefaultFont", 13, "bold")).pack(side="left")
        ttk.Label(hdr, text=self.PROFILE,
                  font=("TkFixedFont", 8),
                  foreground="#666").pack(side="right")

        bar = ttk.Frame(self.root, padding=(12, 4, 12, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="Ajouter des fichiers…",
                   command=self.add_files).pack(side="left")
        ttk.Button(bar, text="Vider la liste",
                   command=self.clear).pack(side="left", padx=(6, 0))
        self.cancel_btn = ttk.Button(bar, text="Annuler le job en cours",
                                     command=self.cancel_current,
                                     state="disabled")
        self.cancel_btn.pack(side="right")

        tv = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        tv.pack(fill="both", expand=True)
        cols = ("status", "progress", "info")
        self.tree = ttk.Treeview(tv, columns=cols, show="tree headings",
                                 height=12, selectmode="browse")
        self.tree.heading("#0", text="Fichier")
        self.tree.heading("status", text="État")
        self.tree.heading("progress", text="Avancement")
        self.tree.heading("info", text="Détails")
        self.tree.column("#0", width=320, anchor="w")
        self.tree.column("status", width=110, anchor="center")
        self.tree.column("progress", width=110, anchor="center")
        self.tree.column("info", width=240, anchor="w")
        sb = ttk.Scrollbar(tv, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._show_error_for_selection)

        out = ttk.Frame(self.root, padding=(12, 4, 12, 6))
        out.pack(fill="x")
        ttk.Label(out, text="Sortie :").pack(side="left")
        self.out_var = tk.StringVar(value=str(self.output_dir))
        ttk.Entry(out, textvariable=self.out_var,
                  state="readonly").pack(side="left", fill="x",
                                         expand=True, padx=8)
        ttk.Button(out, text="Parcourir…",
                   command=self.pick_output).pack(side="left")
        ttk.Button(out, text="Ouvrir le dossier",
                   command=self.open_output).pack(side="left", padx=(6, 0))

        self.status_var = tk.StringVar(value="Prêt")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w",
                  relief="sunken",
                  padding=(8, 4)).pack(fill="x", side="bottom")

    def _preflight(self) -> None:
        if not _have("ffmpeg"):
            messagebox.showerror(
                "ffmpeg introuvable",
                "ffmpeg n'a pas été trouvé.\n\n"
                "• Linux : sudo dnf install ffmpeg (ou équivalent).\n"
                "• Windows : utilise la version bundle de la release."
            )
            self.root.destroy()
            sys.exit(1)
        if not _have("fdkaac"):
            self.status_var.set(
                "Prêt   ·   ⚠ fdkaac introuvable — l'audio sera encodé "
                "à 120k natif (au lieu de 128k strict)"
            )

    # ----- actions -----

    def add_files(self) -> None:
        types = [("Vidéos", " ".join("*" + e for e in VIDEO_EXTS)),
                 ("Tous les fichiers", "*.*")]
        files = filedialog.askopenfilenames(title="Choisir des vidéos",
                                            filetypes=types)
        for f in files:
            self._add_one(Path(f))
        self._wake_worker()

    def _add_one(self, path: Path) -> None:
        if not path.is_file():
            return
        job = Job(path)
        self.jobs.append(job)
        self.tree.insert("", "end", iid=str(id(job)), text=path.name,
                         values=("En attente", "—", _fmt_size(job.size)))

    def _wake_worker(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._run_jobs, daemon=True)
        self.worker.start()

    def _run_jobs(self) -> None:
        for job in list(self.jobs):
            if job.status != "pending":
                continue
            self.current = job
            self.events.put(("started", job))
            try:
                self.output_dir.mkdir(parents=True, exist_ok=True)
                dst = self.output_dir / f"{job.src.stem}.mp4"
                try:
                    if dst.resolve() == job.src.resolve():
                        dst = (self.output_dir
                               / f"{job.src.stem} (converti).mp4")
                except OSError:
                    pass
                convert(job, dst, self._on_progress)
            except Exception as exc:  # noqa: BLE001
                job.status = "error"
                job.message = f"{exc.__class__.__name__}: {exc}"
            self.events.put(("done", job))
        self.current = None
        self.events.put(("idle",))

    def _on_progress(self, job: Job) -> None:
        self.events.put(("progress", job))

    def cancel_current(self) -> None:
        j = self.current
        if not j:
            return
        j.cancelled = True
        if j.proc and j.proc.poll() is None:
            try:
                j.proc.terminate()
            except OSError:
                pass

    def clear(self) -> None:
        keep, removed = [], []
        for j in self.jobs:
            if j.status == "running":
                keep.append(j)
            else:
                removed.append(j)
        self.jobs = keep
        for j in removed:
            try:
                self.tree.delete(str(id(j)))
            except tk.TclError:
                pass

    def pick_output(self) -> None:
        d = filedialog.askdirectory(title="Dossier de sortie",
                                    initialdir=str(self.output_dir))
        if d:
            self.output_dir = Path(d)
            self.out_var.set(d)

    def open_output(self) -> None:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(self.output_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.output_dir)])
            else:
                subprocess.Popen(["xdg-open", str(self.output_dir)])
        except (OSError, FileNotFoundError) as exc:
            messagebox.showerror("Erreur",
                                 f"Impossible d'ouvrir le dossier : {exc}")

    def _show_error_for_selection(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        target = next((j for j in self.jobs if str(id(j)) == sel[0]), None)
        if target and target.status == "error" and target.message:
            messagebox.showerror(target.src.name, target.message)

    # ----- event polling -----

    def _poll(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _handle_event(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "started":
            job = ev[1]
            self.tree.item(str(id(job)),
                           values=("Conversion…", "0 %", "—"))
            self.status_var.set(f"En cours : {job.src.name}")
            self.cancel_btn.config(state="normal")
        elif kind == "progress":
            job = ev[1]
            pct = f"{int(job.progress * 100)} %"
            bits = []
            if job.fps:
                bits.append(f"{job.fps:.1f} fps")
            if job.speed:
                bits.append(f"{job.speed:.2f}×")
            if job.eta is not None:
                bits.append(f"ETA {_fmt_duration(job.eta)}")
            self.tree.item(str(id(job)),
                           values=("Conversion…", pct, " · ".join(bits)))
            self.status_var.set(f"{job.src.name}   ·   {pct}")
        elif kind == "done":
            job = ev[1]
            label = {"done": "Terminé ✓",
                     "cancelled": "Annulé",
                     "error": "Erreur"}.get(job.status, job.status)
            if job.status == "error":
                detail = job.message
                pct = "—"
            elif job.status == "done":
                detail = _fmt_size(job.size) + "  →  output"
                pct = "100 %"
            else:
                detail = job.message
                pct = "—"
            self.tree.item(str(id(job)), values=(label, pct, detail))
        elif kind == "idle":
            self.cancel_btn.config(state="disabled")
            self.status_var.set("Prêt")

    def run(self) -> int:
        self.root.mainloop()
        return 0


# ----- main --------------------------------------------------------------

def main() -> int:
    if sys.platform == "win32":
        # PyInstaller / frozen builds need this before any subprocess use.
        import multiprocessing
        multiprocessing.freeze_support()
        # Windowed PyInstaller builds set sys.stdout / sys.stderr to None.
        for attr in ("stdout", "stderr"):
            if getattr(sys, attr) is None:
                setattr(sys, attr, open(os.devnull, "w"))
    return App().run()


if __name__ == "__main__":
    sys.exit(main())
