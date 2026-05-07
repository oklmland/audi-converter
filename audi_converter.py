#!/usr/bin/env python3
"""Audi MMI MIB1 video converter — FastAPI server with a web UI on localhost.

Encodes to MPEG-4 ASP (Xvid) + AAC LC inside MP4. The MIB1 High Harman
firmware decodes this profile cleanly with B-frames intact, which gives
better motion handling than the H.264 Constrained Baseline that the unit
also accepts. Output is 720×480 (NTSC frame) — a 1:1 pixel match for the
800×480 head-unit screen on the vertical axis.

Run `audi-converter`; a browser tab opens at http://127.0.0.1:<port>/.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from starlette.datastructures import UploadFile
import uvicorn

# ----- Output profile ----------------------------------------------------

MAX_W, MAX_H = 720, 480       # NTSC frame, fits the 800x480 MIB1 screen
V_BITRATE = "2000k"           # max allowed by Audi spec
A_BITRATE_FDKAAC = 128        # strict CBR via fdkaac (kbps)
A_BITRATE_NATIVE = "120k"     # ffmpeg native aac overshoots ~3-5%, undertarget
A_RATE = "44100"              # 44.1 kHz — the Harman decoder grésille at 48
TARGET_FPS = 25
XVID_BFRAMES = "2"
CINEMATIC_DAR_THRESHOLD = 2.0  # > this = preserve aspect with bars (cinema)

DURATION_RE = re.compile(rb"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
TIME_RE = re.compile(rb"time=(\d+):(\d+):(\d+\.\d+)")
FPS_RE = re.compile(rb"fps=\s*([\d.]+)")
SIZE_RE = re.compile(rb"size=\s*(\d+)\s*kB")
SPEED_RE = re.compile(rb"speed=\s*([\d.]+)\s*x")


def hms_to_seconds(h: bytes, m: bytes, s: bytes) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


# ----- Tool / path resolution -------------------------------------------

@functools.lru_cache(maxsize=None)
def _tool(name: str) -> str:
    """Resolve a tool's path. Frozen builds (PyInstaller, Windows) prefer a
    binary shipped inside the bundle; otherwise fall back to PATH.
    Returns the bare name when nothing is found, so spawn raises
    FileNotFoundError as before.

    PyInstaller 6.x one-folder layouts put bundled binaries under
    `_internal/` rather than next to the .exe, so we check both.
    """
    if getattr(sys, "frozen", False):
        ext = ".exe" if sys.platform == "win32" else ""
        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / f"{name}{ext}")
        candidates.append(Path(sys.executable).parent / f"{name}{ext}")
        for cand in candidates:
            if cand.exists():
                return str(cand)
    return shutil.which(name) or name


def _default_output_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return home / "Videos" / "AudiMMI"
    return home / "Vidéos" / "AudiMMI"


def _default_upload_dir() -> Path:
    return Path(tempfile.gettempdir()) / "audi-converter-uploads"


# ----- ffmpeg command builders ------------------------------------------

def _build_video_filter(
    effective_dar: float,
    active_crop: Optional[tuple[int, int, int, int]] = None,
) -> str:
    """Video filter chain, picked from the source's effective DAR.

    If `active_crop` is set (W, H, X, Y) the source has baked-in letterbox
    bars: strip them first, then always crop-to-fill the resulting picture
    (the user has already accepted some loss at master time, they don't want
    bars on the output).

    Without a detected crop:
    * DAR ≤ 2.0 (16:9, 4:3, vertical, etc.): crop content to match the 720×480
      output aspect — fills the frame, no bars.
    * DAR > 2.0 (cinematic 21:9, 2.39:1): scale-and-pad keeping aspect — the
      letterbox bars come back, but cropping cinemascope full-frame masters
      would lose too much picture.
    """
    crop_to_fill = (
        f"crop=w='if(gt(iw/ih\\,{MAX_W}/{MAX_H})\\,trunc(ih*{MAX_W}/{MAX_H}/2)*2\\,iw)'"
        f":h='if(gt(iw/ih\\,{MAX_W}/{MAX_H})\\,ih\\,trunc(iw*{MAX_H}/{MAX_W}/2)*2)',"
        f"scale={MAX_W}:{MAX_H},setsar=1"
    )
    if active_crop is not None:
        cw, ch, cx, cy = active_crop
        return f"crop={cw}:{ch}:{cx}:{cy},{crop_to_fill}"
    if effective_dar > CINEMATIC_DAR_THRESHOLD:
        return (
            f"scale={MAX_W}:{MAX_H}:force_original_aspect_ratio=decrease"
            f":force_divisible_by=2,"
            f"pad={MAX_W}:{MAX_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
    return crop_to_fill


def _xvid_video_args(vf: str) -> list[str]:
    return [
        "-c:v", "libxvid", "-b:v", V_BITRATE,
        "-bf", XVID_BFRAMES,
        "-pix_fmt", "yuv420p",
        "-vf", vf,
        "-r", str(TARGET_FPS),
    ]


def _native_audio_args() -> list[str]:
    return [
        "-c:a", "aac", "-b:a", A_BITRATE_NATIVE, "-ar", A_RATE, "-ac", "2",
        "-aac_coder", "twoloop",
    ]


def _metadata_args(title: str, artist: str) -> list[str]:
    """ffmpeg -metadata flags for MP4 iTunes-style title / artist atoms."""
    args: list[str] = []
    if title:
        args += ["-metadata", f"title={title}"]
    if artist:
        args += ["-metadata", f"artist={artist}"]
    return args


def ffmpeg_cmd_xvid(src: Path, dst: Path, vf: str, meta: list[str]) -> list[str]:
    """Single-pass: Xvid video + native AAC audio, in MP4."""
    return [
        _tool("ffmpeg"), "-y", "-nostdin", "-i", str(src),
        *_xvid_video_args(vf),
        *_native_audio_args(),
        *meta,
        "-movflags", "+faststart",
        str(dst),
    ]


def ffmpeg_video_only_xvid(src: Path, dst: Path, vf: str) -> list[str]:
    """Video-only Xvid for the fdkaac pipeline. Metadata is added at mux time."""
    return [
        _tool("ffmpeg"), "-y", "-nostdin", "-i", str(src),
        *_xvid_video_args(vf),
        "-an", "-f", "mp4", str(dst),
    ]


def have_fdkaac() -> bool:
    return _tool("fdkaac") != "fdkaac"


# ----- State -------------------------------------------------------------

def _parse_filename_metadata(name: str) -> tuple[str, str]:
    """Best-effort split of "Artist - Title.ext" into (artist, title).

    Falls back to ("", stem) when there's no " - " separator. Users can
    correct via the per-file inputs in the UI before encoding.
    """
    stem = Path(name).stem
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", stem


class Job:
    def __init__(self, src: Path, src_name: str, size: int, owns_src: bool):
        self.id = uuid.uuid4().hex[:12]
        self.src = src
        self.src_name = src_name
        self.size = size
        self.owns_src = owns_src
        self.status = "pending"   # pending|running|done|error|cancelled
        self.progress = 0.0
        self.stats: dict[str, Any] = {}
        self.info: dict[str, Any] = {}
        self.dst: Optional[Path] = None
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.cancelled = False
        self.message = ""
        self.artist, self.title = _parse_filename_metadata(src_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "src_name": self.src_name,
            "size": self.size,
            "status": self.status,
            "progress": self.progress,
            "stats": self.stats,
            "info": self.info,
            "dst": str(self.dst) if self.dst else None,
            "message": self.message,
            "title": self.title,
            "artist": self.artist,
        }


class State:
    def __init__(self) -> None:
        self.jobs: list[Job] = []
        self.output_dir = _default_output_dir()
        self.upload_dir = _default_upload_dir()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.subscribers: list[asyncio.Queue] = []
        self.work_event: Optional[asyncio.Event] = None
        self.worker_task: Optional[asyncio.Task] = None
        self.have_fdkaac: bool = False

    async def broadcast(self, event: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


state = State()


# ----- Probing -----------------------------------------------------------

# `ffmpeg -hide_banner -i <input>` exits with code 1 because we give it no
# output, but writes the same stream info ffprobe would print, to stderr.
# Parsing it lets us drop a 97 MB ffprobe.exe from the Windows bundle.

_FF_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")
_FF_VID_RE = re.compile(r"Stream.*?Video:\s*([\w][\w-]*)")
_FF_AUD_RE = re.compile(r"Stream.*?Audio:\s*([\w][\w-]*)")
_FF_WH_RE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_FF_DAR_RE = re.compile(r"DAR\s+(\d+):(\d+)")


async def probe(path: Path) -> dict:
    """Return {codec_v, codec_a, duration, width, height, dar_num, dar_den}."""
    from math import gcd
    try:
        proc = await asyncio.create_subprocess_exec(
            _tool("ffmpeg"), "-hide_banner", "-i", str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (asyncio.TimeoutError, FileNotFoundError):
        return {}
    s = err.decode("utf-8", "replace")
    info: dict[str, Any] = {}
    if (m := _FF_DUR_RE.search(s)):
        h_, mn, sec = m.groups()
        info["duration"] = int(h_) * 3600 + int(mn) * 60 + float(sec)
    if (m := _FF_VID_RE.search(s)):
        info["codec_v"] = m.group(1)
    if (m := _FF_AUD_RE.search(s)):
        info["codec_a"] = m.group(1)
    if (m := _FF_WH_RE.search(s)):
        info["width"] = int(m.group(1))
        info["height"] = int(m.group(2))
    if (m := _FF_DAR_RE.search(s)):
        info["dar_num"] = int(m.group(1))
        info["dar_den"] = int(m.group(2))
    elif info.get("width") and info.get("height"):
        g = gcd(info["width"], info["height"]) or 1
        info["dar_num"] = info["width"] // g
        info["dar_den"] = info["height"] // g
    return info


async def probe_and_emit(job: Job) -> None:
    job.info = await probe(job.src)
    await state.broadcast({"type": "info", "id": job.id, "info": job.info})


# ----- ffmpeg pipeline ---------------------------------------------------

async def read_chunks(stream: asyncio.StreamReader):
    """Yield decoded text fragments split on either \\r or \\n."""
    buf = b""
    while True:
        chunk = await stream.read(256)
        if not chunk:
            if buf:
                yield buf.decode("utf-8", "replace")
            return
        buf += chunk
        while True:
            idx = -1
            for sep in (b"\r", b"\n"):
                i = buf.find(sep)
                if i != -1 and (idx == -1 or i < idx):
                    idx = i
            if idx == -1:
                break
            line, buf = buf[:idx], buf[idx + 1:]
            if line:
                yield line.decode("utf-8", "replace")


async def _run_video_encoding(job: Job, cmd: list[str]) -> tuple[int, str]:
    """Run an ffmpeg subprocess that encodes video, parsing progress.

    Returns (rc, error_msg). rc == -1 means the binary is missing.
    On non-zero rc, error_msg holds the tail of ffmpeg's stderr — useful
    when the failure is a crash with no diagnostic of our own.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return -1, "ffmpeg introuvable"
    job.proc = proc
    duration = float(job.info.get("duration") or 0.0)
    last_emit = 0.0
    stderr_tail: list[str] = []
    assert proc.stderr is not None
    async for line in read_chunks(proc.stderr):
        if job.cancelled:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            break
        stderr_tail.append(line)
        if len(stderr_tail) > 80:
            del stderr_tail[:-80]
        b = line.encode("utf-8", "replace")
        if duration <= 0:
            m = DURATION_RE.search(b)
            if m:
                duration = hms_to_seconds(*m.groups())
        m = TIME_RE.search(b)
        if not m or duration <= 0:
            continue
        done = hms_to_seconds(*m.groups())
        frac = max(0.0, min(1.0, done / duration))
        fps_m = FPS_RE.search(b)
        sz_m = SIZE_RE.search(b)
        sp_m = SPEED_RE.search(b)
        speed = float(sp_m.group(1)) if sp_m else None
        stats = {
            "frac": frac,
            "fps": float(fps_m.group(1)) if fps_m else None,
            "speed": speed,
            "size": int(sz_m.group(1)) * 1024 if sz_m else None,
            "eta": (duration - done) / speed if speed and speed > 0 else None,
        }
        job.stats = stats
        job.progress = frac
        now = time.monotonic()
        if now - last_emit >= 0.15 or frac >= 1.0:
            last_emit = now
            await state.broadcast({"type": "progress", "id": job.id, **stats})
    rc = await proc.wait()
    if rc != 0 and stderr_tail:
        # Last few non-empty lines, joined onto one line for the UI.
        last = [ln.strip() for ln in stderr_tail[-12:] if ln.strip()]
        return rc, " | ".join(last[-6:])
    return rc, ""


async def _encode_audio_fdkaac(src: Path, dst_m4a: Path) -> bool:
    """Decode audio with ffmpeg, encode strict CBR 128k with fdkaac via OS pipe."""
    r_fd, w_fd = os.pipe()
    ffmpeg_p = fdkaac_p = None
    try:
        try:
            ffmpeg_p = await asyncio.create_subprocess_exec(
                _tool("ffmpeg"), "-y", "-nostdin", "-i", str(src),
                "-vn", "-f", "wav", "-ar", A_RATE, "-ac", "2", "-",
                stdout=w_fd,
                stderr=asyncio.subprocess.DEVNULL,
            )
            os.close(w_fd)
            w_fd = -1
            fdkaac_p = await asyncio.create_subprocess_exec(
                _tool("fdkaac"),
                "-b", str(A_BITRATE_FDKAAC), "-p", "2",
                "-", "-o", str(dst_m4a),
                stdin=r_fd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            os.close(r_fd)
            r_fd = -1
        except FileNotFoundError:
            return False
        rc1 = await ffmpeg_p.wait()
        rc2 = await fdkaac_p.wait()
        return rc1 == 0 and rc2 == 0
    finally:
        for fd in (r_fd, w_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        for p in (ffmpeg_p, fdkaac_p):
            if p is not None and p.returncode is None:
                try:
                    p.terminate()
                except ProcessLookupError:
                    pass


async def _mux_av(vid: Path, aud: Path, dst: Path,
                  meta: list[str]) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            _tool("ffmpeg"), "-y", "-nostdin",
            "-i", str(vid), "-i", str(aud),
            "-c", "copy",
            *meta,
            "-movflags", "+faststart",
            str(dst),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    return (await proc.wait()) == 0


def _job_dar(job: Job) -> float:
    n = job.info.get("dar_num") or 16
    d = job.info.get("dar_den") or 9
    return n / d if d else 16 / 9


_CROPDETECT_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


async def detect_active_crop(
    src: Path, src_w: int, src_h: int, duration: float
) -> Optional[tuple[int, int, int, int]]:
    """Run a fast cropdetect on a 2-second sample to strip baked-in bars.

    Returns (w, h, x, y) if real bars are detected (≥4px on any side), else
    None — meaning the picture already fills its container.
    """
    if src_w <= 0 or src_h <= 0:
        return None
    seek = max(0.0, min(duration / 2 if duration else 5.0, 600.0))
    cmd = [
        _tool("ffmpeg"), "-hide_banner", "-nostdin",
        "-ss", f"{seek:.2f}", "-i", str(src),
        "-t", "2", "-vf", "cropdetect=24:2:0", "-an", "-f", "null", "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
    except (OSError, asyncio.CancelledError):
        return None
    last = None
    for m in _CROPDETECT_RE.finditer(err.decode("utf-8", errors="ignore")):
        last = m
    if not last:
        return None
    w, h, x, y = (int(g) for g in last.groups())
    if w <= 0 or h <= 0:
        return None
    # Even dimensions for libxvid + yuv420p.
    w &= ~1; h &= ~1; x &= ~1; y &= ~1
    if w + x > src_w or h + y > src_h:
        return None
    # Skip if the detected crop is essentially the whole frame (no real bars).
    if src_w - w < 4 and src_h - h < 4:
        return None
    return w, h, x, y


def _job_active_crop(job: Job) -> Optional[tuple[int, int, int, int]]:
    c = job.info.get("active_crop")
    if not c:
        return None
    return tuple(c)  # type: ignore[return-value]


def _job_effective_dar(job: Job) -> float:
    """DAR of the active picture, after stripping baked-in bars if any."""
    c = _job_active_crop(job)
    if c is not None:
        cw, ch, _, _ = c
        sar_n = job.info.get("dar_num") or 16
        sar_d = job.info.get("dar_den") or 9
        src_w = int(job.info.get("width") or 0)
        src_h = int(job.info.get("height") or 0)
        if src_w > 0 and src_h > 0 and ch > 0:
            # Pixel-aspect from source (DAR × height / width).
            pixel_aspect = (sar_n / sar_d) * (src_h / src_w)
            return (cw / ch) * pixel_aspect
    return _job_dar(job)


async def run_job(job: Job) -> None:
    out_dir = state.output_dir
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        job.status = "error"
        job.message = f"Sortie : {exc}"
        await state.broadcast({"type": "done", "id": job.id, "ok": False,
                               "msg": job.message})
        return

    base = Path(job.src_name).stem
    dst = out_dir / f"{base}.mp4"
    try:
        if dst.resolve() == job.src.resolve():
            dst = out_dir / f"{base} (converti).mp4"
    except OSError:
        pass
    job.dst = dst
    job.status = "running"
    await state.broadcast({"type": "started", "id": job.id, "dst": str(dst)})

    if not job.info:
        await probe_and_emit(job)

    if "active_crop" not in job.info:
        crop = await detect_active_crop(
            job.src,
            int(job.info.get("width") or 0),
            int(job.info.get("height") or 0),
            float(job.info.get("duration") or 0.0),
        )
        job.info["active_crop"] = list(crop) if crop else None
        await state.broadcast({"type": "info", "id": job.id, "info": job.info})

    if state.have_fdkaac:
        await _run_with_fdkaac(job, dst)
    else:
        await _run_singlepass(job, dst)

    await state.broadcast({"type": "done", "id": job.id,
                           "ok": job.status == "done",
                           "msg": job.message,
                           "dst": str(dst) if job.status == "done" else None})

    if job.owns_src:
        try:
            job.src.unlink(missing_ok=True)
        except OSError:
            pass


async def _run_singlepass(job: Job, dst: Path) -> None:
    """Xvid + native AAC in one ffmpeg invocation. Used when fdkaac is missing."""
    vf = _build_video_filter(_job_effective_dar(job), _job_active_crop(job))
    meta = _metadata_args(job.title, job.artist)
    rc, err = await _run_video_encoding(
        job, ffmpeg_cmd_xvid(job.src, dst, vf, meta)
    )
    if rc == -1:
        job.status = "error"
        job.message = err
        return
    if job.cancelled:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        job.status = "cancelled"
        job.message = "Annulé"
        return
    if rc == 0:
        job.status = "done"
        job.message = str(dst)
    else:
        job.status = "error"
        detail = f": {err}" if err else ""
        job.message = f"ffmpeg a échoué (code {rc}){detail}"


async def _run_with_fdkaac(job: Job, dst: Path) -> None:
    """Three-step pipeline: video-only Xvid, parallel fdkaac audio, then mux."""
    vid_tmp = state.upload_dir / f"{job.id}.video.mp4"
    aud_tmp = state.upload_dir / f"{job.id}.audio.m4a"
    audio_task: Optional[asyncio.Task] = None
    try:
        audio_task = asyncio.create_task(_encode_audio_fdkaac(job.src, aud_tmp))
        vf = _build_video_filter(_job_effective_dar(job), _job_active_crop(job))
        rc, err = await _run_video_encoding(
            job, ffmpeg_video_only_xvid(job.src, vid_tmp, vf)
        )

        if rc == -1:
            audio_task.cancel()
            job.status = "error"
            job.message = err
            return
        if job.cancelled:
            audio_task.cancel()
            job.status = "cancelled"
            job.message = "Annulé"
            return
        if rc != 0:
            audio_task.cancel()
            job.status = "error"
            detail = f": {err}" if err else ""
            job.message = f"ffmpeg a échoué (code {rc}){detail}"
            return

        audio_ok = await audio_task
        if job.cancelled:
            job.status = "cancelled"
            job.message = "Annulé"
            return
        if not audio_ok:
            job.status = "error"
            job.message = "fdkaac a échoué"
            return
        meta = _metadata_args(job.title, job.artist)
        if not await _mux_av(vid_tmp, aud_tmp, dst, meta):
            job.status = "error"
            job.message = "Le mux final a échoué"
            return
        job.status = "done"
        job.message = str(dst)
    finally:
        for p in (vid_tmp, aud_tmp):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


async def worker() -> None:
    assert state.work_event is not None
    while True:
        nxt = next((j for j in state.jobs if j.status == "pending"), None)
        if nxt is None:
            await state.work_event.wait()
            state.work_event.clear()
            continue
        try:
            await run_job(nxt)
        except Exception as exc:  # noqa: BLE001
            nxt.status = "error"
            nxt.message = f"{exc.__class__.__name__}: {exc}"
            await state.broadcast({"type": "done", "id": nxt.id, "ok": False,
                                   "msg": nxt.message})


# ----- FastAPI -----------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.work_event = asyncio.Event()
    state.have_fdkaac = await asyncio.to_thread(have_fdkaac)
    state.worker_task = asyncio.create_task(worker())
    try:
        yield
    finally:
        if state.worker_task:
            state.worker_task.cancel()


app = FastAPI(lifespan=lifespan, title="Audi MMI MIB1 Converter")


class OutputDirRequest(BaseModel):
    path: str


class MetadataRequest(BaseModel):
    title: Optional[str] = None
    artist: Optional[str] = None


class TagFolderRequest(BaseModel):
    path: str


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


def _snapshot() -> dict:
    return {
        "output_dir": str(state.output_dir),
        "jobs": [j.to_dict() for j in state.jobs],
        "have_fdkaac": state.have_fdkaac,
        "platform": sys.platform,
    }


@app.get("/api/state")
async def api_state() -> dict:
    return _snapshot()


@app.get("/api/events")
async def api_events(request: Request) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)
    state.subscribers.append(queue)

    async def gen():
        snap = {"type": "snapshot", **_snapshot()}
        yield f"data: {json.dumps(snap)}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                state.subscribers.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/upload")
async def api_upload(request: Request) -> dict:
    """Accept video uploads of any size.

    Don't use FastAPI's `File()` declaration: it routes through
    `Request.form()` with Starlette's default `max_part_size=1 MiB`,
    which silently truncates anything bigger. We call `Request.form()`
    ourselves with a 64 GiB ceiling so the moov atom of large videos
    doesn't get sliced off.
    """
    form = await request.form(max_part_size=64 * 1024 ** 3)
    new_ids: list[str] = []
    try:
        for _key, value in form.multi_items():
            if not isinstance(value, UploadFile) or not value.filename:
                continue
            target = state.upload_dir / f"{uuid.uuid4().hex}_{Path(value.filename).name}"
            with target.open("wb") as fh:
                while True:
                    chunk = await value.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            size = target.stat().st_size
            job = Job(src=target, src_name=value.filename, size=size,
                      owns_src=True)
            state.jobs.append(job)
            new_ids.append(job.id)
            await state.broadcast({"type": "added", "job": job.to_dict()})
            asyncio.create_task(probe_and_emit(job))
    finally:
        await form.close()
    if state.work_event:
        state.work_event.set()
    return {"job_ids": new_ids}


@app.post("/api/metadata/{job_id}")
async def api_metadata(job_id: str, req: MetadataRequest) -> dict:
    for j in state.jobs:
        if j.id == job_id:
            if j.status != "pending":
                raise HTTPException(400, "Le job est déjà lancé ou terminé")
            if req.title is not None:
                j.title = req.title.strip()
            if req.artist is not None:
                j.artist = req.artist.strip()
            await state.broadcast({"type": "metadata", "id": j.id,
                                   "title": j.title, "artist": j.artist})
            return {"ok": True}
    raise HTTPException(404)


@app.post("/api/cancel/{job_id}")
async def api_cancel(job_id: str) -> dict:
    for j in state.jobs:
        if j.id == job_id:
            j.cancelled = True
            if j.status == "pending":
                j.status = "cancelled"
                j.message = "Annulé"
                await state.broadcast({"type": "done", "id": j.id,
                                       "ok": False, "msg": "Annulé"})
            elif j.proc and j.proc.returncode is None:
                try:
                    j.proc.terminate()
                except ProcessLookupError:
                    pass
            return {"ok": True}
    raise HTTPException(404)


@app.post("/api/cancel-all")
async def api_cancel_all() -> dict:
    for j in state.jobs:
        if j.status in ("pending", "running"):
            j.cancelled = True
            if j.status == "pending":
                j.status = "cancelled"
                j.message = "Annulé"
                await state.broadcast({"type": "done", "id": j.id,
                                       "ok": False, "msg": "Annulé"})
            elif j.proc and j.proc.returncode is None:
                try:
                    j.proc.terminate()
                except ProcessLookupError:
                    pass
    return {"ok": True}


@app.post("/api/clear")
async def api_clear() -> dict:
    keep = []
    removed = []
    for j in state.jobs:
        if j.status == "running":
            keep.append(j)
        else:
            removed.append(j.id)
            if j.owns_src:
                try:
                    j.src.unlink(missing_ok=True)
                except OSError:
                    pass
    state.jobs = keep
    for jid in removed:
        await state.broadcast({"type": "removed", "id": jid})
    return {"ok": True}


@app.post("/api/output")
async def api_output(req: OutputDirRequest) -> dict:
    p = Path(req.path).expanduser()
    state.output_dir = p
    await state.broadcast({"type": "output_dir", "path": str(p)})
    return {"ok": True, "path": str(p)}


def _pick_dir_linux() -> Optional[str]:
    """Native folder picker via zenity / kdialog."""
    for cmd in (["zenity", "--file-selection", "--directory",
                 "--title=Dossier de sortie"],
                ["kdialog", "--getexistingdirectory", str(Path.home())]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        return None
    return None


def _pick_dir_windows() -> Optional[str]:
    """Native folder picker via tkinter (stdlib)."""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(title="Dossier de sortie")
    finally:
        root.destroy()
    return path or None


@app.post("/api/pick-output")
async def api_pick_output() -> dict:
    picker = _pick_dir_windows if sys.platform == "win32" else _pick_dir_linux
    path = await asyncio.to_thread(picker)
    if path:
        state.output_dir = Path(path)
        await state.broadcast({"type": "output_dir", "path": path})
    return {"path": path}


@app.post("/api/open-output")
async def api_open_output() -> dict:
    path = state.output_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return {"ok": True}
    except (OSError, FileNotFoundError) as exc:
        raise HTTPException(500, str(exc))


async def _retag_copy(src: Path, dst: Path,
                      title: str, artist: str) -> bool:
    """Write a tag-only copy of `src` to `dst` without re-encoding.

    Originals are never touched — if the destination disk is full, the FS
    glitches, or ffmpeg trips on a weird input, the source survives. This
    is the safe replacement for the previous in-place rewrite which
    occasionally corrupted files on FAT32 SD cards.
    """
    meta: list[str] = []
    if title:
        meta += ["-metadata", f"title={title}"]
    if artist:
        meta += ["-metadata", f"artist={artist}"]
    if not meta:
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            _tool("ffmpeg"), "-y", "-nostdin",
            "-i", str(src),
            "-c", "copy",
            *meta,
            "-movflags", "+faststart",
            str(dst),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    rc = await proc.wait()
    if rc != 0:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    # Sanity-check: written file must exist and be at least 80 % of the
    # source's size (tag rewrite reuses every byte of the streams, so we
    # should be very close to the original).
    try:
        if not dst.exists() or dst.stat().st_size < src.stat().st_size * 0.8:
            dst.unlink(missing_ok=True)
            return False
    except OSError:
        return False
    return True


@app.post("/api/tag-folder")
async def api_tag_folder(req: TagFolderRequest) -> dict:
    """Walk a folder for .mp4 files, parse Artist/Title from filenames, and
    write tagged copies under `<folder>/_tagged/` (originals untouched).

    Progress is broadcast on the SSE channel as `tag_start` / `tag_progress`
    / `tag_done` events so the UI can show a live progress bar.
    """
    folder = Path(req.path).expanduser()
    if not folder.is_dir():
        raise HTTPException(400, f"Dossier introuvable : {folder}")
    out_dir = folder / "_tagged"
    files = sorted(
        p for p in folder.rglob("*.mp4")
        if p.is_file() and out_dir not in p.parents
    )
    total = len(files)
    await state.broadcast({"type": "tag_start", "total": total,
                           "out_dir": str(out_dir)})
    tagged = skipped = errors = 0
    error_names: list[str] = []
    for i, f in enumerate(files, start=1):
        await state.broadcast({"type": "tag_progress",
                               "i": i, "total": total, "name": f.name})
        if " - " not in f.stem:
            skipped += 1
            continue
        artist, title = _parse_filename_metadata(f.name)
        rel = f.relative_to(folder)
        dst = out_dir / rel
        ok = await _retag_copy(f, dst, title, artist)
        if ok:
            tagged += 1
        else:
            errors += 1
            error_names.append(f.name)
    result = {
        "total": total,
        "tagged": tagged,
        "skipped": skipped,
        "errors": errors,
        "error_names": error_names[:10],
        "out_dir": str(out_dir),
    }
    await state.broadcast({"type": "tag_done", **result})
    return result


@app.post("/api/pick-dir")
async def api_pick_dir() -> dict:
    """Generic folder picker — same dialog backends as /api/pick-output but
    just returns the chosen path without mutating server state."""
    picker = _pick_dir_windows if sys.platform == "win32" else _pick_dir_linux
    path = await asyncio.to_thread(picker)
    return {"path": path}


# ----- HTML --------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audi MMI MIB1 — Convertisseur</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  body { font-family: 'Inter', system-ui, sans-serif; }
  .mono { font-family: 'JetBrains Mono', ui-monospace, monospace; }
  .glass {
    background: rgba(15, 23, 42, 0.55);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border: 1px solid rgba(148, 163, 184, 0.10);
  }
  @keyframes auroraA { 0%,100% { transform: translate(0,0) scale(1); } 50% { transform: translate(40px,-30px) scale(1.1); } }
  @keyframes auroraB { 0%,100% { transform: translate(0,0) scale(1); } 50% { transform: translate(-50px,40px) scale(1.15); } }
  @keyframes auroraC { 0%,100% { transform: translate(0,0) scale(1); } 50% { transform: translate(30px,30px) scale(0.95); } }
  .aurora { position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: -1; }
  .aurora .blob { position: absolute; border-radius: 50%; filter: blur(90px); opacity: 0.55; }
  .aurora .a { background: #6366f1; width: 520px; height: 520px; top: -120px; left: -100px; animation: auroraA 14s ease-in-out infinite; }
  .aurora .b { background: #a855f7; width: 480px; height: 480px; bottom: -160px; right: -80px; animation: auroraB 18s ease-in-out infinite; }
  .aurora .c { background: #ec4899; width: 380px; height: 380px; top: 40%; left: 50%; animation: auroraC 22s ease-in-out infinite; opacity: 0.35; }
  @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
  .shimmer-bar {
    background: linear-gradient(90deg, #6366f1 0%, #a855f7 50%, #ec4899 100%);
    background-size: 200% 100%;
    animation: shimmer 2.4s linear infinite;
  }
  @keyframes fadeUp { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
  .fade-up { animation: fadeUp 200ms ease-out; }
  .ring-grad { position: relative; }
  .ring-grad::before {
    content: ""; position: absolute; inset: -1px; border-radius: inherit;
    background: linear-gradient(135deg, rgba(99,102,241,.5), rgba(168,85,247,.3), rgba(236,72,153,.4));
    z-index: -1; filter: blur(10px); opacity: 0; transition: opacity .25s;
  }
  .drop-active { border-color: #818cf8 !important; background: rgba(99,102,241,0.10) !important; }
  .drop-active::before { opacity: 1; }
  .scrollbox::-webkit-scrollbar { width: 8px; }
  .scrollbox::-webkit-scrollbar-thumb { background: rgba(148,163,184,.25); border-radius: 4px; }
  .scrollbox::-webkit-scrollbar-thumb:hover { background: rgba(148,163,184,.45); }
</style>
</head>
<body class="min-h-screen text-slate-100 bg-slate-950">
  <div class="aurora"><div class="blob a"></div><div class="blob b"></div><div class="blob c"></div></div>

  <div class="max-w-5xl mx-auto px-5 py-7">
    <header class="flex items-center justify-between mb-7">
      <div class="flex items-center gap-3">
        <div class="w-11 h-11 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-lg shadow-indigo-500/30">
          <svg class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>
        </div>
        <div>
          <h1 class="text-2xl font-extrabold tracking-tight bg-gradient-to-r from-indigo-200 via-fuchsia-200 to-pink-200 bg-clip-text text-transparent">Audi MMI MIB1</h1>
          <p class="text-xs text-slate-400 -mt-0.5">Convertisseur vidéo</p>
        </div>
      </div>
      <div class="text-right text-[11px] mono text-slate-400 leading-tight">
        <div>720×480 · MPEG-4 ASP (Xvid) · MP4</div>
        <div>2000k v · AAC LC 128k CBR · 25 fps · 44.1 kHz</div>
      </div>
    </header>

    <div id="dropzone" class="ring-grad rounded-2xl border-2 border-dashed border-slate-700/70 hover:border-slate-500 transition-all p-9 text-center cursor-pointer mb-5 glass">
      <div class="flex flex-col items-center gap-2">
        <div class="w-14 h-14 rounded-2xl bg-gradient-to-br from-indigo-500/20 to-purple-500/20 border border-indigo-500/20 flex items-center justify-center">
          <svg class="w-7 h-7 text-indigo-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        </div>
        <p class="text-base font-semibold text-slate-100">Glissez-déposez vos vidéos ici</p>
        <p class="text-xs text-slate-400">ou cliquez pour choisir des fichiers</p>
      </div>
      <input type="file" id="filepicker" multiple accept="video/*" class="hidden">
    </div>

    <div class="glass rounded-2xl p-4 mb-5">
      <div class="flex items-center justify-between mb-3">
        <div class="flex items-center gap-2">
          <h2 class="font-semibold text-slate-200">File d'attente</h2>
          <span id="counter" class="text-[11px] mono px-2 py-0.5 rounded-full bg-slate-800 text-slate-400"></span>
        </div>
        <div class="flex gap-2">
          <button id="clearbtn" class="px-3 py-1.5 text-xs font-medium rounded-lg bg-slate-800/70 hover:bg-slate-700 transition border border-slate-700/50">Vider</button>
          <button id="cancelbtn" class="px-3 py-1.5 text-xs font-medium rounded-lg bg-rose-600/80 hover:bg-rose-500 transition shadow-rose-900/40 hidden">Tout annuler</button>
        </div>
      </div>
      <div id="empty" class="text-center py-10 text-slate-500 text-sm">
        Aucun fichier en file d'attente.
      </div>
      <div id="jobs" class="space-y-2.5 max-h-[460px] overflow-y-auto scrollbox pr-1"></div>
    </div>

    <div class="glass rounded-2xl p-4">
      <div class="flex items-center justify-between mb-2">
        <label class="text-xs uppercase tracking-wider text-slate-400 font-semibold">Dossier de sortie</label>
        <button id="openout" class="text-xs text-indigo-300 hover:text-indigo-200">Ouvrir le dossier ↗</button>
      </div>
      <div class="flex gap-2">
        <input id="outdir" class="flex-1 mono text-sm bg-slate-900/70 border border-slate-700/60 rounded-lg px-3 py-2 focus:outline-none focus:border-indigo-500/60 focus:ring-1 focus:ring-indigo-500/40">
        <button id="pickbtn" class="px-3 py-2 text-sm rounded-lg bg-slate-800/70 hover:bg-slate-700 transition border border-slate-700/50">Parcourir…</button>
        <button id="savedir" class="px-4 py-2 text-sm rounded-lg bg-gradient-to-br from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 transition shadow-lg shadow-indigo-900/40 font-medium">Enregistrer</button>
      </div>
      <p id="outhint" class="text-[11px] mono text-slate-500 mt-2"></p>
      <div id="fdkaac-hint" class="hidden mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-100/90 leading-relaxed">
        <div class="font-semibold mb-1 text-amber-200">Audio AAC plus précis :</div>
        Sans <span class="mono text-amber-200">fdkaac</span> l'encodeur natif vise 120k pour rester sous la limite Harman 128k. Pour du vrai 128k strict :
        <code class="block mt-1.5 mono px-2 py-1 rounded bg-slate-900/70 border border-slate-700 select-all">sudo dnf install fdkaac</code>
        Redémarre l'app après install.
      </div>
    </div>

    <div class="glass rounded-2xl p-4 mt-5">
      <div class="flex items-center justify-between mb-2">
        <label class="text-xs uppercase tracking-wider text-slate-400 font-semibold">Tagger des fichiers existants</label>
      </div>
      <p class="text-[11px] text-slate-400 mb-3 leading-relaxed">
        Scan un dossier (ta carte SD par exemple), parse <span class="mono">Artist - Title.mp4</span> et écrit des <span class="font-semibold">copies taggées</span> dans <span class="mono">&lt;dossier&gt;/_tagged/</span>. Pas de ré-encodage (quelques secondes par fichier), originaux jamais modifiés.
      </p>
      <div class="flex gap-2">
        <input id="tagdir" placeholder="/run/media/..." class="flex-1 mono text-sm bg-slate-900/70 border border-slate-700/60 rounded-lg px-3 py-2 focus:outline-none focus:border-indigo-500/60 focus:ring-1 focus:ring-indigo-500/40">
        <button id="tagpick" class="px-3 py-2 text-sm rounded-lg bg-slate-800/70 hover:bg-slate-700 transition border border-slate-700/50">Parcourir…</button>
        <button id="tagrun" class="px-4 py-2 text-sm rounded-lg bg-gradient-to-br from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 transition shadow-lg shadow-indigo-900/40 font-medium">Tagger</button>
      </div>
      <div id="tagbar-wrap" class="hidden mt-3 h-1.5 bg-slate-800 rounded-full overflow-hidden">
        <div id="tagbar" class="h-full rounded-full transition-[width] duration-200 ease-out shimmer-bar" style="width:0%"></div>
      </div>
      <p id="taghint" class="text-[11px] mono text-slate-500 mt-2"></p>
    </div>
  </div>

<script>
const $ = (s) => document.querySelector(s);
const jobs = new Map();

const STATUS = {
  pending:   { label: "En attente", color: "text-slate-400", icon: "clock" },
  running:   { label: "En cours",   color: "text-indigo-300", icon: "play" },
  done:      { label: "Terminé",    color: "text-emerald-300", icon: "check" },
  error:     { label: "Erreur",     color: "text-rose-300", icon: "x" },
  cancelled: { label: "Annulé",     color: "text-amber-300", icon: "ban" },
};

const ICONS = {
  clock: '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  play:  '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>',
  check: '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  x:     '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  ban:   '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>',
  trash: '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>',
};

function fmtDur(s) {
  if (s == null || !isFinite(s) || s < 0) return "—";
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}
function fmtSize(b) {
  if (!b || b <= 0) return "—";
  const u = ["B","KB","MB","GB","TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function infoBits(j) {
  const i = j.info || {};
  const out = [];
  if (i.width && i.height) out.push(`${i.width}×${i.height}`);
  const c = [i.codec_v, i.codec_a].filter(Boolean).join("/");
  if (c) out.push(c);
  if (i.duration) out.push(fmtDur(i.duration));
  out.push(fmtSize(j.size));
  return out.join("  ·  ");
}

function newCard(j) {
  const card = document.createElement("div");
  card.className = "fade-up rounded-xl bg-slate-900/40 border border-slate-700/40 p-3 hover:border-slate-600/60 transition-colors";
  card.dataset.id = j.id;
  card.innerHTML = `
    <div class="flex items-start gap-3">
      <div class="status-pill mt-0.5 w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0"></div>
      <div class="flex-1 min-w-0">
        <div class="flex items-center justify-between gap-2">
          <div class="font-medium truncate text-sm name"></div>
          <div class="status-text text-[11px] mono whitespace-nowrap"></div>
        </div>
        <div class="text-[11px] mono text-slate-400 mt-0.5 info"></div>
        <div class="metadata hidden mt-2 grid grid-cols-2 gap-1.5">
          <input class="title-input text-xs px-2 py-1 rounded-md bg-slate-900/60 border border-slate-700/50 focus:outline-none focus:border-indigo-500/60 focus:ring-1 focus:ring-indigo-500/30" placeholder="Titre" autocomplete="off" spellcheck="false">
          <input class="artist-input text-xs px-2 py-1 rounded-md bg-slate-900/60 border border-slate-700/50 focus:outline-none focus:border-indigo-500/60 focus:ring-1 focus:ring-indigo-500/30" placeholder="Interprète" autocomplete="off" spellcheck="false">
        </div>
        <div class="metadata-static hidden text-[11px] mono text-slate-400 mt-1"></div>
        <div class="bar-wrap hidden mt-2.5 h-1.5 bg-slate-800 rounded-full overflow-hidden">
          <div class="bar h-full rounded-full transition-[width] duration-200 ease-out" style="width:0%"></div>
        </div>
        <div class="stats hidden text-[11px] mono text-slate-400 mt-1.5"></div>
      </div>
      <button class="cancel-btn hidden w-7 h-7 rounded-lg flex items-center justify-center text-slate-500 hover:text-rose-300 hover:bg-rose-500/10 transition" title="Annuler"></button>
    </div>
  `;
  card.querySelector(".cancel-btn").innerHTML = ICONS.trash;
  card.querySelector(".cancel-btn").onclick = () => {
    fetch(`/api/cancel/${j.id}`, { method: "POST" });
  };

  // Debounced metadata sync — fires 400 ms after the last keystroke or on
  // blur, whichever comes first.
  const titleEl = card.querySelector(".title-input");
  const artistEl = card.querySelector(".artist-input");
  let metaTimer = null;
  const sync = () => {
    fetch(`/api/metadata/${j.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: titleEl.value,
        artist: artistEl.value,
      }),
    });
  };
  const schedule = () => {
    clearTimeout(metaTimer);
    metaTimer = setTimeout(sync, 400);
  };
  for (const el of [titleEl, artistEl]) {
    el.addEventListener("input", schedule);
    el.addEventListener("blur", () => { clearTimeout(metaTimer); sync(); });
  }
  return card;
}

function renderJob(j) {
  let entry = jobs.get(j.id);
  if (!entry) {
    const dom = newCard(j);
    document.getElementById("jobs").appendChild(dom);
    entry = { data: j, dom };
    jobs.set(j.id, entry);
  } else {
    Object.assign(entry.data, j);
    j = entry.data;
  }
  const dom = entry.dom;
  const st = STATUS[j.status] || STATUS.pending;
  const pill = dom.querySelector(".status-pill");
  pill.className = `status-pill mt-0.5 w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 ${st.color} bg-slate-800/60`;
  pill.innerHTML = ICONS[st.icon];
  dom.querySelector(".name").textContent = j.src_name;
  dom.querySelector(".info").textContent = infoBits(j);

  const titleEl = dom.querySelector(".title-input");
  const artistEl = dom.querySelector(".artist-input");
  const meta = dom.querySelector(".metadata");
  const metaStatic = dom.querySelector(".metadata-static");
  if (j.status === "pending") {
    if (document.activeElement !== titleEl) titleEl.value = j.title || "";
    if (document.activeElement !== artistEl) artistEl.value = j.artist || "";
    meta.classList.remove("hidden");
    metaStatic.classList.add("hidden");
  } else {
    meta.classList.add("hidden");
    const bits = [];
    if (j.title) bits.push(`♪ ${j.title}`);
    if (j.artist) bits.push(`— ${j.artist}`);
    metaStatic.textContent = bits.join("  ");
    metaStatic.classList.toggle("hidden", bits.length === 0);
  }

  const bar = dom.querySelector(".bar-wrap");
  const barInner = dom.querySelector(".bar");
  const stats = dom.querySelector(".stats");
  const stTxt = dom.querySelector(".status-text");
  const cancelBtn = dom.querySelector(".cancel-btn");

  if (j.status === "running") {
    bar.classList.remove("hidden");
    stats.classList.remove("hidden");
    cancelBtn.classList.remove("hidden");
    barInner.className = "bar h-full rounded-full transition-[width] duration-200 ease-out shimmer-bar";
    barInner.style.width = `${(j.progress * 100).toFixed(1)}%`;
    const s = j.stats || {};
    const sb = [];
    if (s.fps) sb.push(`${s.fps.toFixed(1)} fps`);
    if (s.speed) sb.push(`${s.speed.toFixed(2)}×`);
    if (s.eta != null) sb.push(`ETA ${fmtDur(s.eta)}`);
    if (s.size) sb.push(fmtSize(s.size));
    stats.textContent = sb.join("  ·  ");
    stTxt.textContent = `${Math.floor((j.progress || 0) * 100)} %`;
    stTxt.className = "status-text text-[11px] mono whitespace-nowrap text-indigo-300";
  } else {
    bar.classList.add("hidden");
    stats.classList.add("hidden");
    cancelBtn.classList.toggle("hidden", j.status !== "pending");
    stTxt.textContent = (j.status === "error" && j.message) ? j.message : st.label;
    stTxt.className = `status-text text-[11px] mono whitespace-nowrap ${st.color}`;
  }
  updateCounter();
}

function removeJob(id) {
  const e = jobs.get(id);
  if (!e) return;
  e.dom.style.transition = "opacity 150ms";
  e.dom.style.opacity = "0";
  setTimeout(() => e.dom.remove(), 150);
  jobs.delete(id);
  updateCounter();
}

function updateCounter() {
  const total = jobs.size;
  const counts = { pending: 0, running: 0, done: 0, error: 0, cancelled: 0 };
  for (const e of jobs.values()) counts[e.data.status] = (counts[e.data.status] || 0) + 1;
  $("#counter").textContent = total === 0 ? "" : `${total} fichier${total>1?'s':''}`;
  $("#empty").classList.toggle("hidden", total > 0);
  const hasActive = counts.running + counts.pending > 0;
  $("#cancelbtn").classList.toggle("hidden", !hasActive);
}

// SSE
function connect() {
  const ev = new EventSource("/api/events");
  ev.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "snapshot") {
      $("#outdir").value = m.output_dir;
      const hideHint = !!m.have_fdkaac || m.platform === "win32";
      $("#fdkaac-hint").classList.toggle("hidden", hideHint);
      jobs.forEach((_v, id) => removeJob(id));
      m.jobs.forEach(renderJob);
    } else if (m.type === "added") {
      renderJob(m.job);
    } else if (m.type === "info") {
      const d = jobs.get(m.id)?.data;
      if (d) { d.info = m.info; renderJob(d); }
    } else if (m.type === "metadata") {
      const d = jobs.get(m.id)?.data;
      if (d) { d.title = m.title; d.artist = m.artist; renderJob(d); }
    } else if (m.type === "tag_start") {
      $("#tagbar-wrap").classList.remove("hidden");
      $("#tagbar").style.width = "0%";
      $("#taghint").textContent = m.total === 0
        ? "Aucun .mp4 trouvé dans ce dossier."
        : `0 / ${m.total} fichier${m.total > 1 ? "s" : ""}…`;
    } else if (m.type === "tag_progress") {
      const pct = m.total > 0 ? (m.i / m.total) * 100 : 0;
      $("#tagbar").style.width = pct.toFixed(1) + "%";
      $("#taghint").textContent = `${m.i} / ${m.total}  ·  ${m.name}`;
    } else if (m.type === "tag_done") {
      $("#tagbar").style.width = "100%";
      setTimeout(() => $("#tagbar-wrap").classList.add("hidden"), 800);
      const parts = [
        `${m.tagged} copié${m.tagged > 1 ? "s" : ""} avec tags`,
        `${m.skipped} ignoré${m.skipped > 1 ? "s" : ""} (pas de motif "Artist - Title")`,
      ];
      if (m.errors > 0) parts.push(`${m.errors} erreur${m.errors > 1 ? "s" : ""}`);
      parts.push(`→ ${m.out_dir}`);
      $("#taghint").textContent = parts.join("  ·  ");
    } else if (m.type === "started") {
      const d = jobs.get(m.id)?.data;
      if (d) { d.status = "running"; d.progress = 0; renderJob(d); }
    } else if (m.type === "progress") {
      const d = jobs.get(m.id)?.data;
      if (d) {
        d.status = "running";
        d.progress = m.frac;
        d.stats = { fps: m.fps, speed: m.speed, eta: m.eta, size: m.size };
        renderJob(d);
      }
    } else if (m.type === "done") {
      const d = jobs.get(m.id)?.data;
      if (d) {
        d.status = m.ok ? "done" : (m.msg === "Annulé" ? "cancelled" : "error");
        d.message = m.msg;
        d.progress = m.ok ? 1 : d.progress;
        renderJob(d);
      }
    } else if (m.type === "removed") {
      removeJob(m.id);
    } else if (m.type === "output_dir") {
      $("#outdir").value = m.path;
      $("#outhint").textContent = "";
    }
  };
  ev.onerror = () => {
    ev.close();
    setTimeout(connect, 1500);
  };
}
connect();

// Drag & drop
const dz = $("#dropzone");
let dragDepth = 0;
dz.addEventListener("dragenter", (e) => {
  e.preventDefault(); dragDepth++; dz.classList.add("drop-active");
});
dz.addEventListener("dragover", (e) => { e.preventDefault(); });
dz.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dz.classList.remove("drop-active");
});
dz.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  dz.classList.remove("drop-active");
  upload(e.dataTransfer.files);
});
dz.addEventListener("click", () => $("#filepicker").click());
$("#filepicker").addEventListener("change", (e) => upload(e.target.files));

// Window-wide drop also works
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  if (!e.target.closest("#dropzone")) upload(e.dataTransfer.files);
});

async function upload(fileList) {
  if (!fileList || !fileList.length) return;
  const fd = new FormData();
  for (const f of fileList) fd.append("files", f);
  await fetch("/api/upload", { method: "POST", body: fd });
}

// Output dir controls
$("#savedir").onclick = async () => {
  const r = await fetch("/api/output", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: $("#outdir").value }),
  });
  if (r.ok) {
    $("#outhint").textContent = "Enregistré ✓";
    setTimeout(() => $("#outhint").textContent = "", 1500);
  }
};
$("#pickbtn").onclick = async () => {
  const r = await fetch("/api/pick-output", { method: "POST" });
  const j = await r.json();
  if (!j.path) $("#outhint").textContent = "Aucun dossier sélectionné (zenity/kdialog manquant ?).";
};
$("#openout").onclick = (e) => {
  e.preventDefault();
  fetch("/api/open-output", { method: "POST" });
};
$("#clearbtn").onclick = () => fetch("/api/clear", { method: "POST" });
$("#cancelbtn").onclick = () => fetch("/api/cancel-all", { method: "POST" });

$("#tagpick").onclick = async () => {
  const r = await fetch("/api/pick-dir", { method: "POST" });
  const j = await r.json();
  if (j.path) $("#tagdir").value = j.path;
};
$("#tagrun").onclick = async () => {
  const path = $("#tagdir").value.trim();
  if (!path) {
    $("#taghint").textContent = "Indique un dossier d'abord.";
    return;
  }
  $("#tagrun").disabled = true;
  // The actual progress + final summary land via SSE (tag_start /
  // tag_progress / tag_done). We just kick off the request here.
  try {
    const r = await fetch("/api/tag-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      $("#taghint").textContent = "Erreur : " + (err.detail || r.status);
      $("#tagbar-wrap").classList.add("hidden");
    }
  } finally {
    $("#tagrun").disabled = false;
  }
};
</script>
</body>
</html>
"""


# ----- Launcher ----------------------------------------------------------

def _free_port(preferred: int = 7878) -> int:
    for port in (preferred, 7879, 7880, 7881, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    return 0


def _run_server_thread(port: int) -> threading.Thread:
    """Start uvicorn in a daemon thread so the GTK loop owns the main thread."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    def serve():
        asyncio.run(server.serve())

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def _wait_until_ready(url: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    import urllib.request
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.3):
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.05)


def _run_quit_window(url: str) -> int:
    """Tiny Tkinter control window: shows the URL and a Quitter button.

    The real UI lives in the user's browser; this just gives a clear way
    to stop the app on any platform without going through Task Manager.
    """
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Audi MMI MIB1 — serveur actif")
    try:
        root.minsize(420, 0)
    except tk.TclError:
        pass

    style = ttk.Style()
    for theme in ("vista", "winnative", "aqua", "clam"):
        if theme in style.theme_names():
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue

    frame = ttk.Frame(root, padding=(16, 14))
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text="Audi MMI MIB1 — Convertisseur",
              font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
    ttk.Label(frame,
              text="L'interface est ouverte dans ton navigateur.",
              foreground="#555").pack(anchor="w", pady=(2, 8))

    url_var = tk.StringVar(value=url)
    url_entry = ttk.Entry(frame, textvariable=url_var, state="readonly",
                          width=42)
    url_entry.pack(fill="x", pady=(0, 10))

    btn_row = ttk.Frame(frame)
    btn_row.pack(fill="x")

    def open_browser() -> None:
        import webbrowser
        webbrowser.open(url)

    def quit_app() -> None:
        try:
            root.destroy()
        finally:
            os._exit(0)  # daemon thread serving uvicorn dies with the process

    ttk.Button(btn_row, text="Ouvrir dans le navigateur",
               command=open_browser).pack(side="left")
    ttk.Button(btn_row, text="Quitter",
               command=quit_app).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", quit_app)
    root.mainloop()
    return 0


def main() -> int:
    if sys.platform == "win32":
        # PyInstaller / frozen builds need this before any subprocess use.
        import multiprocessing
        multiprocessing.freeze_support()
        # `--windowed` PyInstaller builds set sys.stdout / sys.stderr to
        # None. uvicorn's logging calls sys.stderr.isatty() and crashes
        # before it ever serves a request. Patch in dummy sinks.
        for attr in ("stdout", "stderr"):
            if getattr(sys, attr) is None:
                setattr(sys, attr, open(os.devnull, "w"))

    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    headless = "--no-window" in sys.argv or os.environ.get("AUDI_NO_WINDOW") == "1"
    print(f"Audi MMI MIB1 Converter — {url}", flush=True)

    if headless:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning",
                    access_log=False)
        return 0

    _run_server_thread(port)
    _wait_until_ready(url)

    import webbrowser
    webbrowser.open(url)

    return _run_quit_window(url)


if __name__ == "__main__":
    sys.exit(main())
