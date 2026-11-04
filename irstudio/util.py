# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Small helpers: plotting decimation, device names, file names, audio-file loading."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

import numpy as np

from .config import log


def open_url(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        log.exception("couldn't open %s", url)


def decimate_line(x, y, max_pts=3000):
    """Min/max envelope so a 100k-sample IR plots as ~3k points (looks identical)."""
    x = np.asarray(x)
    y = np.asarray(y)
    n = len(y)
    if n <= max_pts:
        return x, y
    k = int(np.ceil(n / (max_pts / 2)))
    m = n // k
    yy = y[:m * k].reshape(m, k)
    xx = x[:m * k].reshape(m, k)
    lo = yy.argmin(axis=1)
    hi = yy.argmax(axis=1)
    idx = np.sort(np.stack([lo, hi], axis=1), axis=1)
    rows = np.arange(m)[:, None]
    return xx[rows, idx].ravel(), yy[rows, idx].ravel()


def finite(a, fill=0.0):
    a = np.asarray(a, float)
    return np.where(np.isfinite(a), a, fill)


def short_dev(name, ch=None):
    """'Microphone (2- My Interface)', ch 1 -> 'My Interface In 2'."""
    n = str(name)
    if "(" in n and n.rstrip().endswith(")"):
        inner = n[n.index("(") + 1:n.rstrip().rindex(")")]
        if inner.strip():
            n = inner
    n = " ".join(n.split())
    if len(n) > 3 and n[0].isdigit() and n[1:3] == "- ":
        n = n[3:]
    return f"{n} In {ch + 1}" if ch is not None else n


def dev_family(name):
    """'Analogue 3 + 4 (2- My Interface)' -> 'My Interface': inputs of one physical box."""
    return short_dev(name).lower()


def safe_name(s):
    return "".join(c for c in str(s) if c.isalnum() or c in " -_").strip()


def file_tag(target_name):
    """'Short - Smart Box & other short loaders' -> 'Short-Smart_Box_other_short_loaders'."""
    t = re.sub(r"\(.*?\)", " ", str(target_name))          # the length is added separately
    t = safe_name(t.replace("&", " ").replace(".", "_"))
    t = re.sub(r"\s*-\s*", "-", t)
    t = re.sub(r"\s+", "_", t)
    return re.sub(r"_+", "_", t).strip("_-")


MEDIA_EXT = ("wav", "flac", "aif", "aiff", "ogg", "opus", "mp3", "m4a", "aac", "wma", "mp4", "m4v", "mov",
             "mkv", "webm", "avi", "wmv", "flv", "3gp")


def _ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        log.debug("_ffmpeg_exe: ignored error", exc_info=True)
        return shutil.which("ffmpeg")


def load_any_audio(path, max_s=900):
    """Any audio or video file -> (samples [n, ch] float, fs). soundfile first (wav/flac/ogg/mp3...),
    then ffmpeg for everything else (m4a, video files...)."""
    try:
        import soundfile as sf
        a, fs = sf.read(path, always_2d=True, dtype="float32")
        if len(a):
            return a, fs
    except Exception:
        log.debug("load_any_audio: ignored error", exc_info=True)
    exe = _ffmpeg_exe()
    if not exe:
        raise RuntimeError("this file type needs the ffmpeg decoder, which isn't available")
    fs = 48000
    cmd = [exe, "-v", "error", "-nostdin", "-i", path, "-t", str(max_s), "-vn", "-ac", "2", "-ar", str(fs),
           "-f", "f32le", "-"]
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}     # CREATE_NO_WINDOW
    r = subprocess.run(cmd, capture_output=True, timeout=300, **kw)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError((r.stderr.decode(errors="replace").strip().splitlines() or ["no audio track"])[-1])
    a = np.frombuffer(r.stdout, np.float32).reshape(-1, 2)
    return a, fs
