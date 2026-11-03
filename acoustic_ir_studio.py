# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""
Acoustic IR Studio - piezo -> mic impulse responses for acoustic guitar.

Records piezo and mic at the same time (one interface, or interface + USB
measurement mic), runs a guided multi-take session, builds one accurate IR
from all takes, and exports it optimally fitted to each pedal's IR length.
"""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import numpy as np

import matplotlib
matplotlib.use("TkAgg")
matplotlib.rcParams["agg.path.chunksize"] = 10000    # long, dense lines can't overflow the renderer
matplotlib.rcParams["path.simplify"] = True
import matplotlib.ticker                                   # noqa: E402
from matplotlib.figure import Figure                       # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg   # noqa: E402

import ir_engine as E                                      # noqa: E402
import tone_match as TM                                    # noqa: E402
import win_volume as WV                                    # noqa: E402
from matplotlib.widgets import SpanSelector                # noqa: E402

try:
    import sounddevice as sd
except Exception as exc:          # pragma: no cover - shown in UI
    sd = None
    SD_ERROR = str(exc)
else:
    SD_ERROR = ""

APP_NAME = "Acoustic IR Studio"
APP_MAJOR = 1                 # the version shown is <major>.<build number>; every release build counts up
RELEASES_URL = "https://github.com/paulmcevoy75/acoustic-ir-studio/releases/latest"
SOURCE_URL = "https://github.com/paulmcevoy75/acoustic-ir-studio"
AUTHOR = "Paul McEvoy"
LINKS = {"website": ("paulmcevoyguitars.com", "https://paulmcevoyguitars.com"),
         "instagram": ("Instagram @paulmcevoyguitars", "https://www.instagram.com/paulmcevoyguitars/"),
         "youtube": ("YouTube @paulmcevoyguitars", "https://www.youtube.com/@paulmcevoyguitars"),
         "vic": ("Vic Wong", "https://www.paniquejazz.com/")}


def open_url(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        log.exception("couldn't open %s", url)


def build_info():
    """Version + build date, stamped into build_info.json by the automatic Windows build.
    Running from the source code there's no stamp, so it says so."""
    try:
        with open(os.path.join(HERE, "build_info.json"), encoding="utf-8-sig") as fh:
            bi = json.load(fh)
        date = bi.get("date", "?")
        try:                                   # stamped in UTC; show it in this PC's time zone
            import datetime as _dt
            t = _dt.datetime.strptime(date.replace(" UTC", ""), "%Y-%m-%d %H:%M").replace(
                tzinfo=_dt.timezone.utc).astimezone()
            date = f"{t.day} {t.strftime('%b %Y, %H:%M')}"
        except Exception:
            pass
        return {"version": str(bi.get("version") or f"{APP_MAJOR}.x"), "date": date,
                "commit": bi.get("commit", "?"), "release": True}
    except Exception:
        return {"version": f"{APP_MAJOR}.x (development copy)", "date": "not a release build",
                "commit": "-", "release": False}
HERE = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
SLOTS = ("A", "B")
SLOT_LABEL = {"A": "Reference Mic", "B": "Color Mic"}
# instrument, optional 2nd piezo element, reference mic, color mic, internal mic
ROLES = ("piezo", "piezo2", "A", "B", "I")
ROLE_BASE = {"piezo": "Instrument", "piezo2": "Instrument 2", "A": "Reference Mic", "B": "Color Mic",
             "I": "Internal Mic"}
SETTINGS_FILE = None   # set after DATA_DIR is known
STEPS_FILE = None      # set after DATA_DIR is known


def _data_dir():
    """Folder for logs, autosave and device memory: next to the app if writable,
    otherwise %LOCALAPPDATA%\\AcousticIRStudio."""
    try:
        test = os.path.join(HERE, ".write_test")
        with open(test, "w") as fh:
            fh.write("ok")
        os.remove(test)
        return HERE
    except Exception:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "AcousticIRStudio")
        os.makedirs(d, exist_ok=True)
        return d


DATA_DIR = _data_dir()
LOG_DIR = os.path.join(DATA_DIR, "logs")
AUTOSAVE_DIR = os.path.join(DATA_DIR, "autosave")
AUTOSAVE_PREV = os.path.join(DATA_DIR, "autosave_previous")
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
STEPS_FILE = os.path.join(DATA_DIR, "step_lengths.json")
CUSTOM_TARGETS_FILE = os.path.join(DATA_DIR, "custom_targets.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_settings(**kw):
    st = load_settings()
    st.update(kw)
    try:
        with open(SETTINGS_FILE, "w") as fh:
            json.dump(st, fh, indent=2)
    except Exception:
        log.exception("couldn't save settings")
log = logging.getLogger("acoustic_ir")


def setup_logging():
    """app.log gets every error with full details; crash.log catches hard crashes
    (the kind that close the window instantly)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    try:   # keep the log from growing forever
        p = os.path.join(LOG_DIR, "app.log")
        if os.path.exists(p) and os.path.getsize(p) > 5e6:
            os.replace(p, p + ".old")
    except Exception:
        pass
    logging.basicConfig(filename=os.path.join(LOG_DIR, "app.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    try:
        setup_logging.crash_fh = open(os.path.join(LOG_DIR, "crash.log"), "a")
        setup_logging.crash_fh.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        setup_logging.crash_fh.flush()
        faulthandler.enable(setup_logging.crash_fh, all_threads=True)
    except Exception:
        pass

    def hook(t, v, tb):
        log.error("Unhandled error:\n%s", "".join(traceback.format_exception(t, v, tb)))
    sys.excepthook = hook
    threading.excepthook = lambda a: log.error(
        "Unhandled error in thread %s:\n%s", a.thread.name if a.thread else "?",
        "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    log.info("%s starting (python %s, data folder %s)", APP_NAME, sys.version.split()[0], DATA_DIR)
MEAS_MIC_HINTS = ("measurement", "usb", "umik", "umm-6", "umm6", "emm-6", "ecm8000", "minidsp",
                  "dayton", "xref", "imm-6", "usb")
SMOOTH_OPTS = {"None": 0.0, "1/48 oct": 1 / 48, "1/24 oct": 1 / 24,
               "1/12 oct (recommended)": 1 / 12, "1/6 oct": 1 / 6, "1/3 oct": 1 / 3,
               "Detailed lows (1/24 -> 1/6)": (1 / 24, 1 / 6),
               "Psychoacoustic (1/3 -> 1/6)": (1 / 3, 1 / 6)}
ESTIMATORS = {"H1 - recommended": "H1",
              "Hv - more of the mic's air/room": "Hv",
              "H2 - for a noisy pickup": "H2"}


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
STATUS_COL = {"good": "#2e9d5b", "ok": "#d6a419", "weak": "#e0662f", "need data": "#9aa0a6"}


# ============================================================================
# Audio
# ============================================================================
class AuditionPlayer:
    """Plays one buffer with live volume, looping (whole demo or a region) and seamless source
    switching (the position is kept when the buffer changes). Uses its own output stream - or,
    if the headphone monitor is already running on the same device at the same rate, mixes
    into that stream, so the two never fight over the device."""

    def __init__(self, rig):
        self.rig = rig
        self.lock = threading.Lock()
        self.y = None
        self.fs = 48000
        self.pos = 0
        self.loop = None                 # (start, end) samples, or None = whole buffer
        self.looping = True
        self.gain = 1.0
        self.playing = False
        self.stream = None
        self.stream_key = None
        self.via_mon = False
        self.dev_name = ""

    def set_buffer(self, y, fs):
        with self.lock:
            if self.y is not None and fs != self.fs:
                self.pos = int(self.pos * fs / self.fs)
                if self.loop:
                    self.loop = tuple(int(v * fs / self.fs) for v in self.loop)
            self.y, self.fs = y, int(fs)
            n = len(y)
            if self.loop:
                self.loop = (min(self.loop[0], n), min(self.loop[1], n))
            self.pos = min(self.pos, max(0, n - 1))

    def next(self, frames):
        out = np.zeros(frames, np.float32)
        with self.lock:
            y = self.y
            if not self.playing or y is None or not len(y):
                return out
            n = len(y)
            a, b = self.loop if (self.looping and self.loop) else (0, n)
            i = 0
            guard = 0
            while i < frames and guard < 64:
                guard += 1
                if self.pos >= b or self.pos < a:
                    if self.looping:
                        self.pos = a
                    elif self.pos >= n:
                        self.playing = False
                        self.pos = 0
                        break
                    else:
                        a, b = 0, n
                        continue
                k = min(frames - i, b - self.pos)
                out[i:i + k] = y[self.pos:self.pos + k]
                self.pos += k
                i += k
            g = self.gain
        out *= g
        if g > 1.0:
            np.tanh(out, out=out)        # never hard-clip when the volume is pushed up
        return out

    def _cb(self, outdata, frames, t, status):
        outdata[:] = self.next(frames)[:, None]

    def start(self, dev):
        rig = self.rig
        if (rig is not None and rig.mon_stream is not None and rig.mon_dev == dev
                and rig.mon_fs == self.fs):
            self._close()
            self.via_mon = True
            self.dev_name = "headphone monitor output"
            self.playing = True
            return
        self.via_mon = False
        key = (dev, self.fs)
        if self.stream is None or self.stream_key != key:
            self._close()
            nch = 2
            try:
                nch = max(1, min(2, sd.query_devices(dev)["max_output_channels"]))
            except Exception:
                pass
            kw = dict(device=dev, channels=nch, samplerate=self.fs, dtype="float32",
                      blocksize=2048, latency="high", callback=self._cb)
            try:
                st = sd.OutputStream(**kw)
            except Exception as first:
                try:                          # let Windows convert the rate (WASAPI shared mode)
                    st = sd.OutputStream(extra_settings=sd.WasapiSettings(auto_convert=True), **kw)
                except Exception:
                    raise first
            st.start()
            self.stream, self.stream_key = st, key
            try:
                self.dev_name = sd.query_devices(dev)["name"] if dev is not None else "default output"
            except Exception:
                self.dev_name = str(dev)
            log.info("audition output opened: %s @ %d Hz", self.dev_name, self.fs)
        self.playing = True

    def stop(self, rewind=True):
        self.playing = False
        if rewind:
            self.pos = self.loop[0] if self.loop else 0
        self._close()

    def _close(self):
        st, self.stream, self.stream_key = self.stream, None, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass


class AudioRig:
    """Keeps input stream(s) open; meters always, records on demand.

    roles: {"piezo": (dev, ch), "A": (dev, ch), "B": (dev, ch)} - any mic may be on
    the piezo's interface (sample-locked) or on its own device such as a USB mic
    (the engine corrects clock drift and latency afterwards). One stream is
    opened per device, so e.g. a piezo + mic on an interface and a USB mic all
    record at the same time.
    """

    def __init__(self):
        self.streams = []
        self.lock = threading.Lock()
        self.recording = False
        self.roles = {}
        self.buf, self.peak, self.hold = {}, {}, {}
        self.xruns = 0
        self.warning = ""
        # headphone monitor
        self.mon_stream = None
        self.mon_gain = {r: 1.0 for r in ("piezo", "piezo2", "A", "B", "I")}
        self.mon_master = 0.5
        self.mon_mute = set()
        self.mon_solo = set()
        self.ring = {}
        self.sring = {}
        self.mon_target = 768          # samples of buffering per input (~16 ms @ 48k)
        self.mon_dev = None
        self.mon_fs = None
        self.aud = None                # AuditionPlayer, mixed in when it shares the monitor output

    def running(self):
        return bool(self.streams)

    def _cb(self, routes):
        def cb(indata, frames, t, status):
            if status:
                self.xruns += 1
            for role, ch in routes:
                col = indata[:, ch]
                pk = float(np.max(np.abs(col))) if frames else 0.0
                self.peak[role] = max(self.peak.get(role, 0.0), pk)
                self.hold[role] = max(self.hold.get(role, 0.0), pk)
                if self.recording:
                    self.buf[role].append(col.copy())
                sr = self.sring.get(role)
                if sr is not None:
                    b_, n_ = sr[0], len(col)
                    i_ = sr[1] % len(b_)
                    k_ = min(n_, len(b_) - i_)
                    b_[i_:i_ + k_] = col[:k_]
                    if n_ > k_:
                        b_[:n_ - k_] = col[k_:]
                    sr[1] += n_
                rg = self.ring.get(role)
                if rg is not None:               # feed the headphone monitor
                    buf = rg[0]
                    n = len(col)
                    i = rg[1] % len(buf)
                    if i + n <= len(buf):
                        buf[i:i + n] = col
                    else:
                        k = len(buf) - i
                        buf[i:] = col[:k]
                        buf[:n - k] = col[k:]
                    rg[1] += n
        return cb

    # ---- headphone monitor -------------------------------------------------
    def start_monitor(self, out_dev, fs):
        """Mix all inputs (even ones on other devices, like a USB mic) to an output.
        Each input has its own small ring buffer; clock differences between
        devices are absorbed by occasionally skipping/repeating a few samples,
        which is inaudible for monitoring."""
        self.stop_monitor()
        self.ring = {r: [np.zeros(int(fs), np.float32), 0, 0] for r in self.roles}
        nch = 2
        try:
            nch = max(1, min(2, sd.query_devices(out_dev)["max_output_channels"]))
        except Exception:
            pass
        kw = dict(device=out_dev, channels=nch, samplerate=fs, dtype="float32",
                  blocksize=256, callback=self._out_cb)
        try:
            st = sd.OutputStream(**kw)
        except Exception:
            kw["extra_settings"] = sd.WasapiSettings(auto_convert=True)
            st = sd.OutputStream(**kw)
        self.mon_stream = st
        self.mon_dev, self.mon_fs = out_dev, int(fs)
        st.start()

    def stop_monitor(self):
        if self.aud is not None and self.aud.via_mon:
            self.aud.playing = False
            self.aud.via_mon = False
        st, self.mon_stream = self.mon_stream, None
        self.mon_dev = self.mon_fs = None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        self.ring = {}

    def _out_cb(self, outdata, frames, t, status):
        mix = np.zeros(frames, np.float32)
        solo = self.mon_solo
        for role, rg in list(self.ring.items()):
            buf, w, r = rg
            avail = w - r
            if avail > self.mon_target + 6 * frames or r == 0 and w > 0:
                r = w - self.mon_target            # too far behind (or first call): catch up
                avail = self.mon_target
            if avail >= frames:
                i = r % len(buf)
                if i + frames <= len(buf):
                    seg = buf[i:i + frames]
                else:
                    seg = np.concatenate([buf[i:], buf[:frames - (len(buf) - i)]])
                r += frames
            else:
                seg = None                         # under-run: this block is silent
            rg[2] = r
            if seg is None:
                continue
            on = (role in solo) if solo else (role not in self.mon_mute)
            if on:
                mix += seg * self.mon_gain.get(role, 1.0)
        mix *= self.mon_master
        aud = self.aud
        if aud is not None and aud.via_mon:
            mix += aud.next(frames)
        np.clip(mix, -1.0, 1.0, out=mix)
        outdata[:] = mix[:, None]

    def _open(self, dev, channels, routes, fs):
        kw = dict(device=dev, channels=channels, samplerate=fs, dtype="float32",
                  blocksize=256, callback=self._cb(routes))
        try:
            s = sd.InputStream(**kw)
        except Exception as first:
            api = sd.query_hostapis(sd.query_devices(dev)["hostapi"])["name"]
            if "WASAPI" in api:
                kw["extra_settings"] = sd.WasapiSettings(auto_convert=True)
                s = sd.InputStream(**kw)
                self.warning += (f"'{sd.query_devices(dev)['name']}' isn't set to {fs} Hz in "
                                 "Windows, so Windows is resampling it. For best accuracy set it "
                                 f"to {fs} Hz (Sound settings > device > Advanced). ")
            else:
                raise first
        return s

    def start(self, roles, fs):
        self.stop()
        self.roles = dict(roles)
        self.warning = ""
        self.xruns = 0
        self.buf = {r: [] for r in roles}
        self.peak = {r: 0.0 for r in roles}
        self.hold = {r: 0.0 for r in roles}
        self.sring = {r: [np.zeros(16384, np.float32), 0] for r in roles}   # for the live spectrum
        by_dev = {}
        for role, (dev, ch) in roles.items():
            by_dev.setdefault(dev, []).append((role, ch))
        try:
            for dev, routes in by_dev.items():
                n = max(ch for _, ch in routes) + 1
                self.streams.append(self._open(dev, n, routes, fs))
            for st in self.streams:
                st.start()
        except Exception:
            self.stop()
            raise

    def stop(self):
        self.recording = False
        self.stop_monitor()
        for st in self.streams:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        self.streams = []

    def begin(self):
        with self.lock:
            self.buf = {r: [] for r in self.roles}
            self.hold = {r: 0.0 for r in self.roles}
            self.xruns = 0
            self.recording = True

    def end(self):
        with self.lock:
            self.recording = False
            out = {r: (np.concatenate(b) if b else np.zeros(0)).astype(np.float64)
                   for r, b in self.buf.items()}
            self.buf = {r: [] for r in self.roles}
        return out

    def recent(self, role, n=8192):
        """The last n samples of an input (for the live spectrum)."""
        sr = self.sring.get(role)
        if sr is None or sr[1] < n:
            return None
        b_, w = sr[0], sr[1] % len(sr[0])
        return np.concatenate([b_[w:], b_[:w]])[-n:].copy()

    def read_peaks(self):
        p = dict(self.peak)
        self.peak = {r: 0.0 for r in self.roles}
        return p


def list_devices(kind="input", api_filter=None):
    if sd is None:
        return []
    out = []
    apis = sd.query_hostapis()
    for i, d in enumerate(sd.query_devices()):
        n = d["max_input_channels"] if kind == "input" else d["max_output_channels"]
        if n <= 0:
            continue
        api = apis[d["hostapi"]]["name"]
        if api_filter and api_filter != "All" and api != api_filter:
            continue
        tag = f"  [{api}]" if not api_filter or api_filter == "All" else ""
        out.append((f"{i}: {d['name']}{tag}  ({n} ch)", i, n))
    return out


def list_mono_inputs(api_filter=None):
    """Every input channel of every device as its own mono choice.

    Returns [(label, dev_index, channel_index, device_name)].
    """
    out = []
    for _, i, n in list_devices("input", api_filter):
        d = sd.query_devices(i)
        api = sd.query_hostapis(d["hostapi"])["name"]
        tag = f"   [{api}]" if not api_filter or api_filter == "All" else ""
        for ch in range(n):
            out.append((f"{d['name']}  -  Input {ch + 1}{tag}", i, ch, d["name"]))
    seen = {}
    res = []
    for lab, i, ch, nm in out:                  # keep labels unique
        k = seen.get(lab, 0)
        seen[lab] = k + 1
        res.append((lab if k == 0 else f"{lab}  (#{k + 1})", i, ch, nm))
    return res


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
        pass
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


TEXT_TIPS = {
    "Instrument": "The pickup signal you'll put the IR on: the piezo (or a DI'd electric / bass). Choose the "
                  "Interface input it's plugged into.",
    "Label": "Optional name for this input (e.g. the mic model). It's shown on the meters, the mic mix and "
             "the audition buttons. Leave blank to show the device and input number.",
    "Calibration file...": "For a measurement mic: load its calibration file (.txt/.cal from the maker) so "
                           "its own coloration is removed. Not needed for normal mics.",
    "Windows input level...": "Opens Windows' recording settings for devices without a gain knob (e.g. a "
                              "USB Mic). Interface inputs use their hardware gain knobs.",
    "Windows input levels...": "Opens Windows' recording settings (Properties > Levels) to set a USB Mic's "
                               "level.",
    "Refresh devices": "Re-scan for audio devices - press after plugging something in.",
    "   Driver type": "WASAPI is the best choice on Windows. 'All' lists every driver type if a device is "
                      "missing.",
    "Sample rate": "All devices must run at this rate (set it in Windows > Sound > device > Advanced too). "
                   "48 kHz is a good choice.",
    "Audition output (headphones / monitors)": "Where the headphone monitor and Audition play. Usually your "
                                              "Interface's outputs.",
    "Start audio": "Opens all the inputs so the meters work. Set levels so your hardest playing peaks "
                   "around -12 to -6 dBFS.",
    "Stop audio": "Closes the audio devices.",
    "Reset peaks": "Clears the peak markers on the meters.",
    "Headphone monitor": "Hear every input through the Audition output. M = mute, S = solo (only that "
                         "input). This only changes what you hear, never what's recorded.",
    "M": "Mute this input in the headphones (not in the recording).",
    "S": "Solo: hear only this input in the headphones.",
    "Master": "Overall headphone level.",
    "Session": "Full = every step; Quick = similar steps merged, a few minutes shorter.",
    "Step lengths...": "Set how long each guided step records. 0 skips a step.",
    "Record this step": "Records the current guided step; it stops by itself when the time is up.",
    "+30 s": "Give the current take 30 more seconds.",
    "Stop": "Stop recording now - the take is kept and analysed.",
    "Free take": "Record anything extra, as long as you like - e.g. whatever the coverage chart says is "
                 "weak.",
    "Undo last take": "Removes the last take from the IR (its recording stays in the autosave folder).",
    "< Prev": "Go back one step.",
    "Skip >": "Skip this step.",
    "Record demo": "Record ~30 s of normal playing (a bit of everything). It isn't used to build the IR - "
                   "it's what the Audition panel on the Build tab plays back through every option, so you "
                   "can compare the real mics against the IRs.",
    "Coverage - how certain the IR is in each band": "Each bar is how well that frequency range has been "
                                                     "measured so far. Green = solid; red/orange = play more "
                                                     "notes that land there.",
}

PLOT_COL = {"A": "#1565c0", "B": "#ef6c00", "piezo": "#2e7d32", "ir": "#111111", "fit": "#d81b60"}

BUILD_TIPS = {
    "name": ("The IR's name - used for the exported file names."),
    "smooth": ("How much fine detail to keep in the frequency response. The IR is averaged over a band this wide (1/12 octave = about a semitone).\n\n- Finer (1/24, 1/48): more detail, body resonances stay sharp, but more risk of keeping things specific to this exact mic spot.\n- Coarser (1/6, 1/3): smoother, more forgiving, more 'generic'.\n- Detailed lows: fine in the bass (where body resonances matter) and smoother in the treble.\n- Psychoacoustic: roughly how the ear resolves pitch.\n1/12 is a good default."),
    "reg": ("Protects against bad data. Where the instrument signal was weak or noisy (e.g. very low or very high notes nobody played), the IR would boost wildly and whistle. This limits how far the IR may go in those places.\n\nLower (40-60 dB) = safer, smoother. Higher (80-100 dB) = trusts the data more, more detail, more risk of harsh peaks. 70 dB is a good default."),
    "lc": ("Removes everything below this frequency from the IR (0 = off). Cuts rumble, handling noise and boom that a stage PA doesn't want. 35 Hz for guitar, ~22 Hz for double bass. Raise to 70-100 Hz to tame feedback-prone boom."),
    "hc": ("Removes everything above this frequency (0 = off). Above ~16-18 kHz there's little musical content and mostly hiss; lower it (8-12 kHz) to soften pick and string noise."),
    "est": ("How the instrument and mic signals are compared:\n\n- H1 (recommended): ignores noise in the mic signal (room noise, hiss, other sounds). The most accurate in normal conditions.\n- Hv: a balance between H1 and H2 - keeps a bit more of the mic's air and room.\n- H2: use if the PICKUP is noisy (hum, buzz) and the mic is clean.\nIf unsure, keep H1."),
    "phase": ("- Minimum: all the energy as early as possible. Tight, punchy, no pre-echo, lowest latency - and it survives shortening for pedals best. Recommended.\n- Measured: keeps the timing exactly as recorded (including the delay from instrument to mic and any room reflections). Can sound more 'roomy' but smears and shortens badly; mostly for experiments."),
    "len": ("How long the full-length (master) IR is. Longer captures more of the body's ring and the lowest notes accurately, but it's mostly decay after ~200 ms. 1000 ms (the default) covers the Ultimate target; up to 2000 ms for double bass. Every pedal version is cut down from this, so keep it at least as long as your longest target."),
    "inv": ("Flips the IR upside down (polarity). Doesn't change the tone on its own, but if you blend the IR'd signal with another signal (a mic, an internal mic, a magnetic pickup) and the sum sounds thin, try flipping it."),
    "build": ("Builds the IR from all your takes using the settings above."),
    "targets": ("The pedals / devices to make IRs for. Each has a maximum IR length; the app fits the best possible IR into that length. 'Error' = how different the short version is from the full IR (under ~1 dB is very hard to hear). Click one to see it on the graph and to audition at that length; Ctrl/Shift-click to pick several for export."),
    "norm": ("- Match loudness: every exported length plays at the same perceived volume, so you can swap IRs or pedals without a jump in level.\n- Peak -1 dBFS: each file is made as loud as possible without clipping (lengths may differ in volume)."),
    "bits": ("File format of the exported WAVs. 24-bit suits almost every pedal. Use 16-bit only if your pedal rejects 24-bit; 32-bit float for software / DAW convolvers."),
    "bundle": ("Also saves the full-length master IR (32-bit float) and a notes file with all settings. Every export goes into the folder '<Project Name> IRs', next to where the project is saved."),
    "export": ("Saves an IR file for every selected target, into the folder '<Project Name> IRs' next to the saved project (you're asked to save the project first if it hasn't been)."),
    "add": ("Add your own pedal / device: its sample rate and maximum IR length (check its manual)."),
    "rem": ("Remove the selected targets from the list."),
}


class ScrollFrame(ttk.Frame):
    """A tab body that scrolls vertically when the window is too short for it (13" laptops),
    and simply fills the window when there's room (graphs then grow)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self._last = None
        self._vsb_on = False
        self.body.bind("<Configure>", self._sync, add="+")
        self.canvas.bind("<Configure>", self._sync, add="+")
        self.bind("<Enter>", lambda e: self.bind_all("<MouseWheel>", self._wheel), add="+")
        self.bind("<Leave>", lambda e: self.unbind_all("<MouseWheel>"), add="+")

    def _sync(self, _e=None):
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        rh = self.body.winfo_reqheight()
        if cw < 2 or ch < 2:
            return
        h = max(rh, ch)
        key = (cw, h)
        if key != self._last:
            self._last = key
            self.canvas.itemconfigure(self._win, width=cw, height=h)
            self.canvas.configure(scrollregion=(0, 0, cw, h))
        need = rh > ch + 2
        if need and not self._vsb_on:
            self.vsb.pack(side="right", fill="y", before=self.canvas)
            self._vsb_on = True
        elif not need and self._vsb_on:
            self.vsb.pack_forget()
            self._vsb_on = False
            self.canvas.yview_moveto(0)

    def _wheel(self, e):
        if self._vsb_on:
            w = e.widget
            if isinstance(w, (tk.Text, tk.Listbox)) or str(w).endswith("popdown.f.l"):
                return                                  # let lists / logs scroll themselves
            self.canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")


class Tooltip:
    """Hover help: shows a small yellow box after the mouse rests on a widget for ~0.5 s."""

    def __init__(self, widget, text, wrap=380):
        self.w, self.text, self.wrap = widget, text, wrap
        self.tip = self.job = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e=None):
        self._cancel()
        self.job = self.w.after(500, self._show)

    def _cancel(self):
        if self.job is not None:
            try:
                self.w.after_cancel(self.job)
            except Exception:
                pass
            self.job = None

    def _show(self):
        self.job = None
        if self.tip is not None or not self.text:
            return
        try:
            x = self.w.winfo_rootx() + 16
            y = self.w.winfo_rooty() + self.w.winfo_height() + 4
            self.tip = tk.Toplevel(self.w)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tip, text=self.text, justify="left", wraplength=self.wrap, background="#ffffe0",
                     relief="solid", borderwidth=1, font=("Segoe UI", 9), padx=6, pady=4).pack()
        except Exception:
            self.tip = None

    def _leave(self, _e=None):
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None

def dev_family(name):
    """'Analogue 3 + 4 (2- My Interface)' -> 'My Interface': inputs of one physical box."""
    return short_dev(name).lower()


def dev_index(label):
    try:
        return int(label.split(":", 1)[0])
    except (ValueError, AttributeError):
        return None


# ============================================================================
# Pedal list
# ============================================================================
PRESET_TARGETS = [
    {"name": "Short - Smart Box & other short loaders", "fs": 44100, "samples": 1024, "preset": True,
     "tip": "1024 samples at 44.1 kHz (23 ms) - Sonicake Smart Box and similar small IR pedals "
            "(check your unit's manual for its exact limit)."},
    {"name": "Medium - TC Electronic Impulse", "fs": 48000, "samples": 9600, "preset": True,
     "tip": "200 ms at 48 kHz (9600 samples) - the TC Electronic Impulse IR Loader's maximum."},
    {"name": "Long - Daisy Seed (0.5 s)", "fs": 48000, "samples": 24000, "format": "daisy", "preset": True,
     "tip": "500 ms at 48 kHz - a basic Daisy Seed convolver; captures nearly all of a guitar body's ring. "
            "Exports .wav + .bin + C header."},
    {"name": "Ultimate - Daisy Seed (1 s)", "fs": 48000, "samples": 48000, "format": "daisy", "preset": True,
     "tip": "1 s at 48 kHz - the longest that's practical on a Daisy Seed (needs an efficient partitioned "
            "convolver). Exports .wav + .bin + C header."},
]


def load_pedals():
    """The four presets, then the user's own targets (kept in the data folder, so they survive
    updating the app)."""
    custom = []
    try:
        with open(CUSTOM_TARGETS_FILE, "r") as fh:
            custom = [p for p in json.load(fh)["targets"] if not p.get("preset")]
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("couldn't read custom targets")
    return [dict(p) for p in PRESET_TARGETS] + custom


def save_pedals(pedals):
    try:
        with open(CUSTOM_TARGETS_FILE, "w") as fh:
            json.dump({"targets": [p for p in pedals if not p.get("preset")]}, fh, indent=2)
    except Exception:
        log.exception("couldn't save custom targets")


# ============================================================================
# App
# ============================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.build_meta = build_info()
        self.title(f"{APP_NAME}  {self.build_meta['version']}" +
                   (f"   (built {self.build_meta['date']})" if self.build_meta["release"] else ""))
        log.info("version %s, built %s, commit %s", self.build_meta["version"], self.build_meta["date"],
                 self.build_meta["commit"])
        try:                                   # use more of the screen when there is room
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except Exception:
            sw, sh = 1280, 820
        self.compact = sh < 900                 # 13-14" laptops (or big Windows scaling)
        if sw < 1400 or sh < 900:
            self.geometry(f"{max(900, sw - 20)}x{max(560, sh - 80)}+0+0")
            try:
                self.state("zoomed")            # Windows: use the whole screen
            except tk.TclError:
                pass
        else:
            self.geometry(f"{min(1560, sw - 60)}x{min(1000, sh - 90)}+20+10")
        self.minsize(900, 560)
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Big.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Step.TLabel", font=("Segoe UI", 11))
        style.configure("Rec.TButton", font=("Segoe UI", 11, "bold"))
        style.configure("Transport.TButton", font=("Segoe UI", 12, "bold"), padding=(6, 6))

        self.rig = AudioRig()
        self.fs = tk.IntVar(value=48000)
        self.acc = {s: None for s in SLOTS}
        self.cal = {s: None for s in SLOTS}
        self.cal_name = {s: tk.StringVar(value="(none)") for s in SLOTS}
        self.slot = tk.StringVar(value="A")
        self.last_piezo = None
        self.demo = None
        self.dual_pair = None
        self.project_path = None        # the .irproject file; its IRs export to '<name> IRs' beside it
        self.default_export_dir = load_settings().get("export_dir", "")
        self.backup_dir = load_settings().get("backup_dir", "")
        self.take_stack = []
        self.autosave_live = False      # this session is being written to the autosave folder
        self.take_counter = 0
        self._err_shown = 0.0
        self.irm = None
        self.fits = {}
        self.q = queue.Queue()
        self.busy = False
        self.step_i = 0
        self.rec_started = None
        self.rec_limit = None
        self.rec_label = ""
        self.pedals = load_pedals()
        self.instrument = tk.StringVar(value="acoustic guitar")
        self.master_src = None          # None | "built" | path of loaded WAV

        self._build_menu()
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.tab_setup = ttk.Frame(nb)
        self.tab_cap = ttk.Frame(nb)
        self.tab_build = ttk.Frame(nb)
        nb.add(self.tab_setup, text="  1  Setup  ")
        nb.add(self.tab_cap, text="  2  Capture  ")
        nb.add(self.tab_build, text="  3  Build & Export  ")
        self.tab_tone = ttk.Frame(nb)
        nb.add(self.tab_tone, text="  4  Tone Match  ")
        self.nb = nb
        self._build_setup()
        self._build_capture()
        self._build_build()
        self._build_tone()
        self._auto_tips(self)
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8, pady=(0, 4))
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self._after_id = self.after(60, self._tick)
        if sd is None:
            messagebox.showerror(APP_NAME, "Audio library failed to load:\n" + SD_ERROR)

    # ------------------------------------------------------------------ menu
    def _scroll_body(self, tab):
        sf = ScrollFrame(tab)
        sf.pack(fill="both", expand=True)
        return sf.body

    def _link(self, parent, text, url, **kw):
        """A clickable web link."""
        font = kw.pop("font", ("Segoe UI", 9))
        lab = tk.Label(parent, text=text, fg="#1a5fb4", cursor="hand2", font=font + ("underline",), **kw)
        lab.bind("<Button-1>", lambda e: open_url(url))
        self._tip(lab, url)
        return lab

    def _tip(self, widget, text):
        """Attach hover help to a widget (kept so it isn't garbage-collected)."""
        self._tips = getattr(self, "_tips", [])
        self._tips.append(Tooltip(widget, text))
        return widget

    def _auto_tips(self, w):
        """Hover help for Setup / Capture controls, matched by their text."""
        try:
            txt = str(w.cget("text"))
        except Exception:
            txt = ""
        tip = TEXT_TIPS.get(txt.strip())
        if tip and not getattr(w, "_has_tip", False):
            self._tip(w, tip)
        for ch in w.winfo_children():
            self._auto_tips(ch)

    def _build_menu(self):
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Save project...", command=self.save_session)
        f.add_command(label="Open project...", command=self.load_session)
        f.add_separator()
        f.add_command(label="Reopen last session (autosaved)", command=self.reopen_last)
        f.add_command(label="Open the session before that...", command=self.reopen_previous)
        f.add_command(label="Rebuild last session from its recordings...",
                      command=self.recover_from_recordings)
        f.add_separator()
        f.add_command(label="Open an IR WAV to re-fit for pedals...", command=self.open_ir_wav)
        f.add_command(label="Import recorded WAVs as a take...", command=self.import_wavs)
        f.add_separator()
        f.add_command(label="Exit", command=self._quit)
        m.add_cascade(label="File", menu=f)
        h = tk.Menu(m, tearoff=0)
        h.add_command(label="How to get the best IR", command=self.show_tips)
        h.add_command(label="Open logs folder (send app.log if something goes wrong)",
                      command=lambda: self._open_folder(LOG_DIR))
        h.add_separator()
        h.add_command(label=f"About {APP_NAME}...", command=self.show_about)
        m.add_cascade(label="Help", menu=h)
        st = tk.Menu(m, tearoff=0)
        st.add_command(label="Set projects folder (projects + their IRs)...", command=self.set_export_folder)
        st.add_command(label="Open projects folder", command=self._open_export_folder)
        st.add_separator()
        st.add_command(label="Set backup folder (gets a copy of every export)...", command=self.set_backup_folder)
        st.add_command(label="Stop copying to a backup folder", command=lambda: (
            setattr(self, "backup_dir", ""), save_settings(backup_dir=""),
            self.status.set("Exports are no longer copied to a backup folder.")))
        m.add_cascade(label="Settings", menu=st)
        self.config(menu=m)

    def show_about(self):
        b = self.build_meta
        w = tk.Toplevel(self)
        w.title(f"About {APP_NAME}")
        w.transient(self)
        w.resizable(False, False)
        ttk.Label(w, text=APP_NAME, font=("Segoe UI", 15, "bold")).pack(padx=24, pady=(16, 2))
        ttk.Label(w, text=f"Version {b['version']}", font=("Segoe UI", 12)).pack()
        rows = [("Built", b["date"]), ("Code version (commit)", b["commit"]),
                ("Python", sys.version.split()[0]), ("Your data folder", DATA_DIR)]
        g = ttk.Frame(w)
        g.pack(padx=24, pady=10)
        for i, (k, v) in enumerate(rows):
            ttk.Label(g, text=k + ":", foreground="#555").grid(row=i, column=0, sticky="e", padx=(0, 8))
            ttk.Label(g, text=v).grid(row=i, column=1, sticky="w")
        ttk.Label(w, text=f"Created by {AUTHOR}", font=("Segoe UI", 10, "bold")).pack(pady=(4, 2))
        lf = ttk.Frame(w)
        lf.pack()
        for key in ("website", "instagram", "youtube"):
            self._link(lf, *LINKS[key]).pack(anchor="center")
        vf = ttk.Frame(w)
        vf.pack(pady=(8, 0))
        ttk.Label(vf, text="Thank you to ").pack(side="left")
        self._link(vf, *LINKS["vic"]).pack(side="left")
        ttk.Label(vf, text=" for the inspiration.").pack(side="left")
        ttk.Label(w, foreground="#555", wraplength=420, justify="center", text=(
            "Free, open-source software under the GNU GPL v3.0 - you may use, study, change and share "
            "it under its terms. It comes with ABSOLUTELY NO WARRANTY.")).pack(padx=24, pady=(10, 2))
        self._link(w, "Source code and new versions on GitHub", SOURCE_URL).pack()
        ttk.Label(w, foreground="#555", wraplength=420, justify="center", text=(
            "Each new release counts the version up by one. To update, download the newest "
            "zip and unzip it over this folder - your sessions and settings are kept.")).pack(padx=24, pady=(8, 0))
        bf = ttk.Frame(w)
        bf.pack(pady=12)
        ttk.Button(bf, text="Download page", command=lambda: open_url(RELEASES_URL)).pack(side="left", padx=4)
        ttk.Button(bf, text="Close", command=w.destroy).pack(side="left", padx=4)

    def set_export_folder(self):
        d = filedialog.askdirectory(title="Projects folder - new projects (and their IRs) are saved here; "
                                          "e.g. a folder inside Google Drive", initialdir=self.projects_dir())
        if d:
            self.default_export_dir = d
            save_settings(export_dir=d)
            self.status.set(f"New projects will be saved in: {d}")

    def projects_dir(self):
        """Where projects are saved by default: your choice (Settings), else
        Documents/Acoustic IR Studio Projects."""
        if self.default_export_dir and os.path.isdir(self.default_export_dir):
            return self.default_export_dir
        d = os.path.join(os.path.expanduser("~"), "Documents", "Acoustic IR Studio Projects")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            return os.path.expanduser("~")

    def set_backup_folder(self):
        d = filedialog.askdirectory(title="Backup folder - every export is also copied here (e.g. Google "
                                          "Drive / Dropbox / OneDrive / a USB stick)",
                                    initialdir=self.backup_dir or None)
        if d:
            self.backup_dir = d
            save_settings(backup_dir=d)
            self.status.set(f"Every export will also be copied to: {d}")

    def _open_export_folder(self):
        self._open_folder(self.projects_dir())

    # ------------------------------------------------------------------ setup tab
    def _build_setup(self):
        t = self._scroll_body(self.tab_setup)
        cr = ttk.Frame(t)
        cr.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(cr, text=f"Created by {AUTHOR}  \u00b7 ", font=("Segoe UI", 9, "bold")).pack(side="left")
        self._link(cr, *LINKS["website"], font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Label(cr, text="      Thank you to ", foreground="#555").pack(side="left")
        self._link(cr, *LINKS["vic"]).pack(side="left")
        ttk.Label(cr, text=" for the inspiration.", foreground="#555").pack(side="left")
        top = ttk.Frame(t)
        top.pack(fill="x", padx=10, pady=10)
        ttk.Label(top, text="Instrument").pack(side="left")
        ic = ttk.Combobox(top, values=list(E.INSTRUMENTS), width=15, state="readonly",
                          textvariable=self.instrument)
        ic.pack(side="left", padx=(6, 18))
        ic.bind("<<ComboboxSelected>>", lambda e: self._instrument_changed())
        self._last_instrument = self.instrument.get()
        ttk.Label(top, text="Sample rate").pack(side="left")
        cb = ttk.Combobox(top, values=[44100, 48000, 96000], width=8, state="readonly",
                          textvariable=self.fs)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._rate_changed())
        ttk.Label(top, text="   Driver type").pack(side="left")
        apis = ["All"] + ([a["name"] for a in sd.query_hostapis()] if sd else [])
        default_api = "Windows WASAPI" if "Windows WASAPI" in apis else "All"
        self.api = tk.StringVar(value=default_api)
        a = ttk.Combobox(top, values=apis, textvariable=self.api, state="readonly", width=22)
        a.pack(side="left", padx=6)
        a.bind("<<ComboboxSelected>>", lambda e: self._refresh_devices())
        ttk.Button(top, text="Refresh devices", command=lambda: self._refresh_devices(rescan=True)).pack(side="left", padx=6)
        self.api_cb = a

        self.inp = {}
        fr = ttk.LabelFrame(t, text="Inputs - all of these record at the same time on every take")
        fr.pack(fill="x", padx=15, pady=4)
        ttk.Label(fr, text="Each choice is one mono input (e.g. 'My Interface - Input 2'). One mic of any "
                  "kind works; the others are optional. Type a label for each (e.g. 'SM57', 'USB Mic') so "
                  "you can tell them apart everywhere in the app.",
                  foreground="#555", wraplength=820, justify="left").grid(row=10, column=1, columnspan=3, sticky="w", pady=(0, 6))
        rows = (("piezo", "Instrument (piezo / DI)", None),
                ("piezo2", "Instrument 2 (2nd piezo element, optional)", True),
                ("A", "Reference Mic", True),
                ("B", "Color Mic (optional)", True),
                ("I", "Internal Mic (optional)", True))
        self.inp_widgets = {}
        self.inp_label = {}
        for k, (role, label, togg) in enumerate(rows):
            r = 2 * k
            v = {"dev": tk.StringVar(), "on": tk.BooleanVar(value=(role in ("piezo", "A"))),
                 "info": tk.StringVar()}
            self.inp[role] = v
            self.inp_label[role] = tk.StringVar()
            if togg is None:
                w0 = ttk.Label(fr, text=label)
            else:
                w0 = ttk.Checkbutton(fr, text=label, variable=v["on"], command=self._inputs_changed)
            w0.grid(row=r, column=0, sticky="w", padx=8, pady=(6, 0))
            self.inp_widgets[role] = {"name": w0}
            lf_ = ttk.Frame(fr)
            lf_.grid(row=r, column=2, sticky="w", padx=(0, 8), pady=(6, 0))
            ttk.Label(lf_, text="Label").pack(side="left")
            le = ttk.Entry(lf_, textvariable=self.inp_label[role], width=18)
            self.inp_widgets[role]["label"] = le
            le.pack(side="left", padx=4)
            le.bind("<FocusOut>", lambda e: self._inputs_changed())
            le.bind("<Return>", lambda e: self._inputs_changed())
            v["cb"] = ttk.Combobox(fr, textvariable=v["dev"], width=70, state="readonly", height=20)
            v["cb"].grid(row=r, column=1, sticky="we", padx=(0, 8), pady=(6, 0))
            v["cb"].bind("<<ComboboxSelected>>", lambda e: self._inputs_changed())
            sub = ttk.Frame(fr)
            sub.grid(row=r + 1, column=1, columnspan=2, sticky="w", pady=(2, 4))
            self.inp_widgets[role]["winlvl"] = ttk.Button(sub, text="Windows input level...",
                                                          command=WV.open_recording_settings)
            self.inp_widgets[role]["winlvl"].pack(side="left", padx=(0, 6))
            if role in SLOTS:
                self.inp_widgets[role]["cal"] = ttk.Button(sub, text="Calibration file...",
                                                           command=lambda s=role: self.load_cal(s))
                self.inp_widgets[role]["cal"].pack(side="left")
                ttk.Button(sub, text="Clear", width=6, command=lambda s=role: self.clear_cal(s)).pack(side="left", padx=4)
                ttk.Label(sub, textvariable=self.cal_name[role]).pack(side="left", padx=(4, 16))
            ttk.Label(sub, textvariable=v["info"], foreground="#2b6cb0").pack(side="left")
        fr.columnconfigure(1, weight=1)
        self.dev_hint = ttk.Label(t, foreground="#a33", wraplength=1000, justify="left")
        self.dev_hint.pack(fill="x", padx=15)

        out = ttk.LabelFrame(t, text="Audition output (headphones / monitors)")
        out.pack(fill="x", padx=15, pady=5)
        self.out_dev = tk.StringVar()
        self.out_cb = ttk.Combobox(out, textvariable=self.out_dev, width=70, state="readonly")
        self.out_cb.pack(side="left", padx=8, pady=8)

        help_txt = (
            "How the inputs are used:\n"
            "  - Instrument: the Interface input your guitar / bass is plugged into (piezo, or a DI'd electric).\n"
            "  - Instrument 2 (optional): if your pickup has two elements wired to separate outputs, the second "
            "one (same Interface as the first). The app then also makes an IR PAIR - one IR per element - for "
            "2-input convolvers like a Daisy Seed, and shows how much that gains over the summed pickup.\n"
            "  - Reference Mic: the mic whose sound the IR recreates - ideally an accurate one, e.g. a USB "
            "measurement mic (load its calibration file, 0-degree / on-axis), but any mic works (SM57, condenser...).\n"
            "  - Color Mic (optional): a second mic with a character you like. It gets its own IR, and on the "
            "Build tab you can mix the two by frequency (e.g. lows from the Reference Mic, highs from the Color Mic).\n"
            "  - Internal Mic (optional): the guitar's own built-in mic, if it has one. It's recorded with "
            "everything else and can be blended in when you audition.\n"
            "  - Every take records all inputs at once. Put the Reference Mic and Color Mic close together, "
            "and only turn on phantom power for mics that need it.\n"
            "  - A USB Mic (on a different device from the Interface) is clock-synced automatically; its level "
            "can be set on the Capture tab.\n"
            "  - Set every device to the SAME sample rate in Windows (Sound settings > device > Advanced) and "
            "here. Close other audio apps while capturing.\n"
            "  - USB Mic not listed? Plug it in and press Refresh devices. Still missing: set Driver type to "
            "'All', and check Windows Settings > Privacy > Microphone lets desktop apps use the microphone.")
        ttk.Label(t, text=help_txt, wraplength=1000, justify="left").pack(fill="x", padx=15, pady=10)
        self._refresh_devices()

    def _refresh_devices(self, rescan=False):
        if rescan and sd is not None:
            if self.rig.running():
                self.stop_audio()
            try:                                   # re-scan: picks up newly plugged-in USB mics
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
            apis = ["All"] + [a["name"] for a in sd.query_hostapis()]
            self.api_cb["values"] = apis
            if self.api.get() not in apis:
                self.api.set("All")
        ins = list_mono_inputs(self.api.get())
        outs = list_devices("output", self.api.get())
        self.in_map = {lab: (i, ch, nm) for lab, i, ch, nm in ins}
        labels = [lab for lab, *_ in ins]
        saved = {}
        try:
            with open(DEVICES_FILE) as fh:
                saved = json.load(fh)
        except Exception:
            pass

        # the Interface = the input device with the most channels; a USB Mic = a 1-2 input
        # device that isn't the Interface
        # Windows often lists one interface as several 2-input devices ("Analogue 1 + 2 (iD..)",
        # "Analogue 3 + 4 (iD..)"), so count inputs per device FAMILY (the name in brackets)
        nch, fam_ch = {}, {}
        for _, i, c, nm in ins:
            nch[nm] = max(nch.get(nm, 0), c + 1)
        for nm, c in nch.items():
            fam_ch[dev_family(nm)] = fam_ch.get(dev_family(nm), 0) + c
        fam = max(fam_ch, key=fam_ch.get) if fam_ch else ""
        self.iface_family = fam if fam_ch.get(fam, 0) >= 3 else ""
        fam_devs = [nm for nm in nch if dev_family(nm) == self.iface_family]
        self.iface_name = max(fam_devs, key=nch.get) if fam_devs else ""
        is_iface = lambda nm: bool(self.iface_family) and dev_family(nm) == self.iface_family
        is_meas = lambda nm: (not is_iface(nm) and nch.get(nm, 0) <= 2 and
                              any(h in nm.lower() for h in MEAS_MIC_HINTS))

        def find(pred, ch=None):
            return next((lab for lab, i, c, nm in ins if pred(nm) and (ch is None or c == ch)), "")

        default_ch = {"piezo": 0, "piezo2": 1, "A": 0, "B": 1, "I": 2}
        for role, v in self.inp.items():
            if v["dev"].get() in self.in_map:
                continue
            pick = ""
            sv = saved.get(role)
            if sv and sv.get("name"):
                ch = int(sv.get("ch", 1)) - 1
                pick = find(lambda nm: nm == sv["name"], ch) or \
                    find(lambda nm: sv["name"].lower() in nm.lower(), ch)
                v["on"].set(sv.get("on", role in ("piezo", "A")))
                if sv.get("label"):
                    self.inp_label[role].set(sv["label"])
            if not pick:
                if role == "A":
                    pick = find(is_meas, 0) or find(is_iface, 1)
                else:
                    pick = find(is_iface, default_ch[role]) or find(is_iface)
            v["dev"].set(pick or (labels[0] if labels else ""))
        if not labels:
            self.dev_hint.configure(text="No input devices found. Plug in your interface and mic, then "
                                    "press Refresh devices.")
        elif len(nch) == 1:
            self.dev_hint.configure(text="Only one input device found. If you use a USB Mic, plug it in "
                                    "and press Refresh devices; if it still doesn't appear, try Driver type "
                                    "'All', and check Windows Settings > Privacy & security > Microphone "
                                    "allows desktop apps.")
        elif not self.iface_name:
            self.dev_hint.configure(text="No multi-input Interface found - each device shows 1-2 inputs. Try "
                                    "Driver type 'All' or another driver type; some drivers list each input "
                                    "pair of an interface as a separate device.")
        else:
            self.dev_hint.configure(text="")
        for v in self.inp.values():
            v["cb"]["values"] = labels
        self.out_cb["values"] = [l for l, _, _ in outs]
        if hasattr(self, "aud_out_cb"):
            self.aud_out_cb["values"] = self.out_cb["values"]
        if self.out_dev.get() not in self.out_cb["values"] and outs:
            stem = (self.iface_name.split("(")[-1].rstrip(") ").lower() if self.iface_name else "")
            pick = next((l for l, _, _ in outs if stem and stem in l.lower()), outs[0][0])
            self.out_dev.set(pick)
        self._inputs_changed()

    def mic_name(self, role):
        """What to call an input everywhere: your label, else the device + input number."""
        lab = self.inp_label[role].get().strip() if hasattr(self, "inp_label") else ""
        if lab:
            return lab
        m = self._sel(role) if hasattr(self, "inp") else None
        base = ROLE_BASE[role]
        return f"{base} ({short_dev(m[2], m[1])})" if m else base

    def mic_name_full(self, role):
        """Name + exactly which device input it is (for the blend / mix controls)."""
        m = self._sel(role) if hasattr(self, "inp") else None
        where = short_dev(m[2], m[1]) if m else "no input"
        lab = self.inp_label[role].get().strip() if hasattr(self, "inp_label") else ""
        return f"{lab or ROLE_BASE[role]} [{where}]"

    def _sel(self, role):
        """(device_index, channel_index, device_name) for a role, or None."""
        return getattr(self, "in_map", {}).get(self.inp[role]["dev"].get())

    def _inputs_changed(self):
        p = self._sel("piezo")
        for role in ("piezo2", "A", "B", "I"):
            v = self.inp[role]
            v["cb"].configure(state="readonly" if v["on"].get() else "disabled")
            m = self._sel(role)
            if not v["on"].get():
                v["info"].set("off")
            elif m is None or p is None:
                v["info"].set("")
            elif role == "piezo2":
                v["info"].set("same device as Instrument 1 - good" if m[0] == p[0] else
                              "must be on the SAME device as the Instrument (sample-locked)")
            elif m[0] == p[0]:
                v["info"].set("same device as the Instrument (sample-locked)")
            else:
                v["info"].set("separate device (e.g. USB Mic) - auto clock-sync")
        if hasattr(self, "meter"):
            for role in ROLES:
                self.meter_name[role].configure(text=self.mic_name(role))
            for s_ in SLOTS:
                self.cov_radio[s_].configure(text=self.mic_name(s_))
            for role in ("piezo2", "A", "B", "I"):
                c, lab = self.meter[role]
                on = bool(self.inp[role]["on"].get())
                if not on:
                    c.delete("all")
                    lab.configure(text="off")
                for w in self.meter_row.get(role, []):      # only show inputs that are switched on
                    if on:
                        w.grid()
                    else:
                        w.grid_remove()
            self._refresh_input_levels()
        if hasattr(self, "mix_title"):
            self.mix_title.configure(text=f"0 = all {self.mic_name_full('A')}    "
                                          f"100 = all {self.mic_name_full('B')}")
        if hasattr(self, "tm_src_cb"):
            self.tm_src_cb["values"] = [self.mic_name(r) for r in ROLES]
        if hasattr(self, "aud_src"):
            self._refresh_audition_names()

    def save_devices(self):
        d = {}
        for role, v in self.inp.items():
            m = self._sel(role)
            d[role] = {"name": m[2] if m else "", "ch": (m[1] + 1) if m else 1,
                       "on": bool(v["on"].get()), "label": self.inp_label[role].get()}
        try:
            with open(DEVICES_FILE, "w") as fh:
                json.dump(d, fh, indent=2)
        except Exception:
            pass

    def input_config(self):
        nice = {r: self.mic_name(r) for r in ROLES}
        roles = {}
        for role, v in self.inp.items():
            if role != "piezo" and not v["on"].get():
                continue
            m = self._sel(role)
            if m is None:
                raise ValueError(f"Choose an input for {nice[role]} on the Setup tab first.")
            roles[role] = (m[0], m[1])
        if not any(r in roles for r in SLOTS):
            raise ValueError("Turn on at least one mic (Reference Mic or Color Mic) on the Setup tab.")
        if "piezo2" in roles and roles["piezo2"][0] != roles["piezo"][0]:
            raise ValueError("Instrument 2 must be on the same device (Interface) as the Instrument, so the "
                             "two piezo elements stay sample-locked.")
        used = {}
        for role, key in roles.items():
            if key in used:
                raise ValueError(f"{nice[used[key]]} and {nice[role]} are set to the same input. "
                                 "Each needs its own input.")
            used[key] = role
        return roles

    def profile(self):
        return E.INSTRUMENTS[self.instrument.get()]

    def _step_overrides(self):
        try:
            with open(STEPS_FILE) as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _session_key(self):
        quick = getattr(self, "v_session", None) is not None and self.v_session.get().startswith("Quick")
        return f"{self.instrument.get()}|{'quick' if quick else 'full'}"

    def base_steps(self):
        quick = self._session_key().endswith("quick")
        return self.profile()["quick" if quick else "steps"]

    def steps(self):
        """Steps for the chosen session, with your edited lengths (0 = skip that step)."""
        ov = self._step_overrides().get(self._session_key(), {})
        out = []
        for name, dur, txt in self.base_steps():
            if dur is not None and name in ov:
                d = int(ov[name])
                if d <= 0:
                    continue
                dur = d
            out.append((name, dur, txt))
        return out

    def edit_step_lengths(self):
        win = tk.Toplevel(self)
        win.title("Step lengths")
        win.transient(self)
        ov_all = self._step_overrides()
        key = self._session_key()
        ov = ov_all.get(key, {})
        ttk.Label(win, text=f"{self.instrument.get()} - {self.v_session.get()}\n"
                  "Seconds for each step (0 = skip it). Free play has no limit and can't be skipped.",
                  justify="left").grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=8)
        vars_ = {}
        r = 1
        for name, dur, _ in self.base_steps():
            ttk.Label(win, text=name).grid(row=r, column=0, sticky="w", padx=10, pady=2)
            if dur is None:
                ttk.Label(win, text="no limit").grid(row=r, column=1, sticky="w")
            else:
                v = tk.IntVar(value=int(ov.get(name, dur)))
                ttk.Spinbox(win, from_=0, to=900, increment=5, width=6, textvariable=v).grid(row=r, column=1)
                ttk.Label(win, text=f"(default {dur})", foreground="#777").grid(row=r, column=2, sticky="w", padx=6)
                vars_[name] = (v, dur)
            r += 1
        tot = ttk.Label(win)
        tot.grid(row=r, column=0, columnspan=3, sticky="w", padx=10, pady=6)

        def total(*_):
            try:
                t = sum(int(v.get()) for v, _ in vars_.values())
                tot.configure(text=f"Total: {t // 60} min {t % 60} s + free play")
            except Exception:
                pass
        for v, _ in vars_.values():
            v.trace_add("write", total)
        total()

        def save():
            try:
                ov_all[key] = {n: int(v.get()) for n, (v, d) in vars_.items() if int(v.get()) != d}
                with open(STEPS_FILE, "w") as fh:
                    json.dump(ov_all, fh, indent=2)
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Couldn't save: {exc}", parent=win)
                return
            win.destroy()
            self.goto_step(min(self.step_i, len(self.steps())))

        def reset():
            for v, d in vars_.values():
                v.set(d)
        bf = ttk.Frame(win)
        bf.grid(row=r + 1, column=0, columnspan=3, sticky="e", padx=10, pady=(0, 10))
        ttk.Button(bf, text="Defaults", command=reset).pack(side="left", padx=4)
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="left", padx=4)
        ttk.Button(bf, text="Save", command=save).pack(side="left", padx=4)

    def _has_takes(self):
        return any(self.acc[s] is not None and not self.acc[s].empty() for s in SLOTS)

    def _instrument_changed(self):
        if self._has_takes():
            if not messagebox.askyesno(APP_NAME, "Switching instrument starts a new capture "
                                       "(the current takes are cleared - save the project first "
                                       "if you want to keep them). Continue?"):
                self.instrument.set(self._last_instrument)
                return
            self.acc = {s: None for s in SLOTS}
            self.take_stack = []
            self.irm = None
        self._last_instrument = self.instrument.get()
        pr = self.profile()
        self.v_lc.set(pr["low_cut"])
        self.v_master_ms.set(str(pr["master_ms"]))
        if self.v_name.get() in ("MyGuitar", "MyBass"):
            self.v_name.set("MyBass" if self.instrument.get() == "double bass" else "MyGuitar")
        self.goto_step(0)
        self._update_coverage()

    def _rate_changed(self):
        if any(self.acc[s] is not None and not self.acc[s].empty() for s in SLOTS):
            if not messagebox.askyesno(APP_NAME, "Changing the sample rate clears the captured "
                                       "takes (they must all share one rate). Continue?"):
                self.fs.set(self.acc["A"].fs if self.acc["A"] else self.acc["B"].fs)
                return
            self.acc = {s: None for s in SLOTS}
            self.take_stack = []
            self._update_coverage()
        if self.rig.running():
            self.stop_audio()

    def load_cal(self, s):
        p = filedialog.askopenfilename(title="Mic calibration file",
                                       filetypes=[("Calibration", "*.cal *.txt *.frd *.csv"), ("All", "*.*")])
        if not p:
            return
        try:
            self.cal[s] = E.load_calibration(p)
            self.cal_name[s].set(os.path.basename(p) + f"  ({len(self.cal[s][0])} points)")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def clear_cal(self, s):
        self.cal[s] = None
        self.cal_name[s].set("(none)")

    # ------------------------------------------------------------------ capture tab
    def _build_capture(self):
        t = self._scroll_body(self.tab_cap)
        top = ttk.Frame(t)
        top.pack(fill="x", padx=10, pady=8)
        ttk.Label(top, text="Records the instrument + every mic that's on, together.     Show coverage for:").pack(side="left")
        self.cov_radio = {}
        for s in SLOTS:
            self.cov_radio[s] = ttk.Radiobutton(top, text=SLOT_LABEL[s], value=s, variable=self.slot,
                                                command=self._slot_changed)
            self.cov_radio[s].pack(side="left", padx=8)
        self.audio_btn = ttk.Button(top, text="Start audio", command=self.toggle_audio)
        self.audio_btn.pack(side="right")

        mon = ttk.Frame(t)
        mon.pack(fill="x", padx=10, pady=(0, 2))
        self.v_mon = tk.BooleanVar(value=False)
        ttk.Checkbutton(mon, text="Headphone monitor", variable=self.v_mon,
                        command=self._monitor_changed).pack(side="left")
        self.mon_dev_lab = ttk.Label(mon, foreground="#555")
        self.mon_dev_lab.pack(side="left", padx=(4, 12))
        ttk.Label(mon, text="Master").pack(side="left")
        self.v_mon_master = tk.DoubleVar(value=-6.0)
        ttk.Scale(mon, from_=-40, to=6, variable=self.v_mon_master, length=150,
                  command=lambda e: self._monitor_levels()).pack(side="left", padx=4)
        self.mon_master_lab = ttk.Label(mon, text="-6 dB", width=7)
        self.mon_master_lab.pack(side="left")
        ttk.Button(mon, text="Reset peaks", command=self.reset_peaks).pack(side="right")
        ttk.Button(mon, text="Windows input levels...", command=WV.open_recording_settings).pack(side="right", padx=6)

        mrow = ttk.Frame(t)
        mrow.pack(fill="x", padx=10)
        spf = ttk.Frame(mrow)                    # packed first, so the meters can't squeeze it out
        spf.pack(side="right", fill="y", padx=(8, 0))
        meters = ttk.Frame(mrow)
        meters.pack(side="left", fill="x", expand=True)
        self.spec_fig = Figure(figsize=(3.4 if self.compact else 4.0, 1.6 if self.compact else 1.75), dpi=100)
        self.spec_ax = self.spec_fig.add_subplot(111)
        self.spec_canvas = FigureCanvasTkAgg(self.spec_fig, master=spf)
        self.spec_canvas.get_tk_widget().pack(fill="both", expand=True)
        sfo = ttk.Frame(spf)
        sfo.pack(fill="x")
        self.v_spec_match = tk.BooleanVar(value=True)
        smc = ttk.Checkbutton(sfo, text="Level-match (compare tone)", variable=self.v_spec_match)
        smc.pack(side="left")
        self._tip(smc, "On: every input is shifted to the same overall level so you see the difference "
                       "in TONE (e.g. the piezo's quack vs the mic). Off: real levels in dBFS.")
        self._tip(self.spec_canvas.get_tk_widget(), "Live spectrum of every input (last ~0.2 s, smoothed). "
                  "Play and watch how the piezo differs from the mics - that difference is what the IR "
                  "corrects.")
        self._spec_lines = {}
        self._spec_avg = {}
        self._spec_t = 0.0
        self._spec_init()
        self.meter = {}
        self.meter_name = {}
        self.meter_row = {}
        self.v_mute, self.v_solo, self.v_gain, self.gain_lab = {}, {}, {}, {}
        self.v_inlvl, self.inlvl_scale, self.inlvl_lab = {}, {}, {}
        for col, txt in ((1, "Mute"), (2, "Solo"), (3, "Headphone level"), (5, "Input level (USB)"),
                         (7, "Meter")):
            ttk.Label(meters, text=txt, foreground="#666", font=("Segoe UI", 8)).grid(row=0, column=col)
        style = ttk.Style(self)
        style.configure("MS.Toolbutton", padding=(6, 1))
        for i0, role in enumerate(ROLES):
            i = i0 + 1
            self.meter_name[role] = ttk.Label(meters, text=role, width=18 if self.compact else 24)
            self.meter_name[role].grid(row=i, column=0, sticky="w")
            self.v_mute[role] = tk.BooleanVar(value=False)
            self.v_solo[role] = tk.BooleanVar(value=False)
            ttk.Checkbutton(meters, text="M", style="MS.Toolbutton", variable=self.v_mute[role],
                            command=self._monitor_levels).grid(row=i, column=1, padx=1)
            ttk.Checkbutton(meters, text="S", style="MS.Toolbutton", variable=self.v_solo[role],
                            command=self._monitor_levels).grid(row=i, column=2, padx=(1, 6))
            self.v_gain[role] = tk.DoubleVar(value=0.0)
            ttk.Scale(meters, from_=-40, to=12, variable=self.v_gain[role], length=80 if self.compact else 120,
                      command=lambda e: self._monitor_levels()).grid(row=i, column=3)
            self.gain_lab[role] = ttk.Label(meters, text="0 dB", width=6)
            self.gain_lab[role].grid(row=i, column=4, padx=(2, 8))
            self.v_inlvl[role] = tk.DoubleVar(value=100.0)
            self.inlvl_scale[role] = ttk.Scale(meters, from_=0, to=100, variable=self.v_inlvl[role],
                                               length=70 if self.compact else 100,
                                               command=lambda e, r=role: self._set_input_level(r))
            self.inlvl_scale[role].grid(row=i, column=5)
            self.inlvl_lab[role] = ttk.Label(meters, text="", width=10)
            self.inlvl_lab[role].grid(row=i, column=6, padx=(2, 8))
            c = tk.Canvas(meters, height=22, width=120, bg="#1e1f22", highlightthickness=0)
            c.grid(row=i, column=7, sticky="we", pady=2)
            lab = tk.Label(meters, text="-inf dBFS", width=17 if self.compact else 22, anchor="w")
            lab.grid(row=i, column=8, padx=6)
            self.meter[role] = (c, lab)
            self.meter_row[role] = [w for w in meters.grid_slaves(row=i)]
        meters.columnconfigure(7, weight=1)
        ttk.Label(meters, foreground="#555", wraplength=760, justify="left", text=(
            "Recording level: interface inputs use the interface's gain knobs; a USB mic uses the 'Input level "
            "(USB)' slider (that's its Windows level). Aim for peaks of -12 to -6 dBFS on your hardest playing. "
            "The marker shows the highest peak - red above -3 dBFS, CLIP if it clipped. Mute / Solo / "
            "Headphone level only change what you hear, not the recording.")).grid(
            row=len(ROLES) + 1, column=0, columnspan=9, sticky="w", pady=(2, 0))

        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, padx=10, pady=6)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        g = ttk.LabelFrame(left, text="Guided session")
        g.pack(fill="x")
        sf_ = ttk.Frame(g)
        sf_.pack(fill="x", padx=10, pady=(6, 0))
        ttk.Label(sf_, text="Session").pack(side="left")
        self.v_session = tk.StringVar(value="Full (14 steps, ~5 min)")
        scb = ttk.Combobox(sf_, values=["Full (14 steps, ~5 min)", "Quick (merged steps, ~3.5 min)"],
                           textvariable=self.v_session, state="readonly", width=28)
        scb.pack(side="left", padx=6)
        scb.bind("<<ComboboxSelected>>", lambda e: self.goto_step(0))
        ttk.Button(sf_, text="Step lengths...", command=self.edit_step_lengths).pack(side="left")
        self.step_title = ttk.Label(g, style="Big.TLabel")
        self.step_title.pack(anchor="w", padx=10, pady=(8, 2))
        self.step_text = ttk.Label(g, style="Step.TLabel", wraplength=560, justify="left")
        self.step_text.pack(anchor="w", padx=10)
        self.rec_time = ttk.Label(g, text="", style="Big.TLabel", foreground="#c0392b")
        self.rec_time.pack(anchor="w", padx=10, pady=4)
        bf = ttk.Frame(g)
        bf.pack(fill="x", padx=10, pady=(0, 10))
        self.rec_btn = ttk.Button(bf, text="Record this step", style="Rec.TButton",
                                  command=self.record_step)
        self.rec_btn.pack(side="left")
        self.stop_btn = ttk.Button(bf, text="Stop", command=self.stop_record, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.ext_btn = ttk.Button(bf, text="+30 s", command=self.extend_record, state="disabled")
        self.ext_btn.pack(side="left")
        df = ttk.Frame(g)
        df.pack(fill="x", padx=10, pady=(0, 8))
        self.demo_btn = ttk.Button(df, text="Record demo", command=self.record_demo)
        self.demo_btn.pack(side="left")
        self.demo_lab = ttk.Label(df, text="No demo yet - play something typical (a song section, ~30 s); "
                                  "the Build tab's Audition plays it back every way.", foreground="#555",
                                  wraplength=470, justify="left")
        self.demo_lab.pack(side="left", padx=8)
        ttk.Button(bf, text="< Prev", command=lambda: self.goto_step(self.step_i - 1)).pack(side="left", padx=(16, 2))
        ttk.Button(bf, text="Skip >", command=lambda: self.goto_step(self.step_i + 1)).pack(side="left")
        ttk.Button(bf, text="Free take", command=self.record_free).pack(side="left", padx=(16, 2))
        ttk.Button(bf, text="Undo last take", command=self.undo_take).pack(side="left", padx=2)

        tipf = ttk.LabelFrame(left, text="What to play next")
        tipf.pack(fill="x", pady=8)
        self.tips = ttk.Label(tipf, wraplength=560, justify="left")
        self.tips.pack(anchor="w", padx=10, pady=6)

        lf = ttk.LabelFrame(left, text="Take log")
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, height=8, wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        self.cov_summary = ttk.Label(right, text="", justify="left", wraplength=440)
        self.cov_summary.pack(anchor="w", pady=(0, 6))
        cf = ttk.LabelFrame(right, text="Coverage - how certain the IR is in each band")
        cf.pack(fill="both", expand=True)
        self.cov_fig = Figure(figsize=(5.6, 2.2 if self.compact else 3.0), dpi=100)
        self.cov_ax = self.cov_fig.add_subplot(111)
        self.cov_canvas = FigureCanvasTkAgg(self.cov_fig, master=cf)
        self.cov_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.goto_step(0)
        self._update_coverage()
        self._inputs_changed()

    def _slot_changed(self):
        self._update_coverage()

    def toggle_audio(self):
        if self.rig.running():
            self.stop_audio()
        else:
            self.start_audio()

    def start_audio(self):
        if sd is None:
            messagebox.showerror(APP_NAME, "Audio library not available.")
            return False
        try:
            cfg = self.input_config()
            self.rig.start(cfg, int(self.fs.get()))
        except Exception as exc:
            msg = str(exc)
            if "sample rate" in msg.lower() or "-9997" in msg:
                msg += ("\n\nThe device isn't running at the chosen rate. Set it in Windows "
                        "(Sound settings > device > Advanced) or in your interface's control panel, or "
                        "pick the same rate here.")
            messagebox.showerror(APP_NAME, "Couldn't open audio:\n" + msg)
            return False
        self.audio_btn.configure(text="Stop audio")
        self.save_devices()
        log.info("audio started: %s @ %s", cfg, self.fs.get())
        if self.v_mon.get():
            self._start_monitor()
        mics = " + ".join(self.mic_name(r) for r in SLOTS if r in cfg)
        ndev = len({d for d, _ in cfg.values()})
        self.status.set(f"Audio running - piezo + {mics} on {ndev} device(s) @ {self.fs.get()} Hz")
        if self.rig.warning:
            self.logln("WARNING: " + self.rig.warning)
        return True

    def stop_audio(self):
        if self.rig.recording:
            self.stop_record()
        self.rig.stop()
        self.audio_btn.configure(text="Start audio")
        self.status.set("Audio stopped.")

    def goto_step(self, i):
        n = len(self.steps())
        if i >= n:
            self.step_i = n
            self.step_title.configure(text="Guided session complete")
            self.step_text.configure(text="Everything is autosaved. Check the coverage chart - use "
                                     "'Free take' for anything 'What to play next' suggests - then go "
                                     "to Build & Export.")
            self.rec_btn.configure(state="disabled")
            return
        self.step_i = max(0, i)
        name, dur, txt = self.steps()[self.step_i]
        self.step_title.configure(text=f"Step {self.step_i + 1} of {n}:  {name}")
        if dur:
            self.step_text.configure(text=f"{txt}\n(about {dur} s - stops automatically; "
                                     "press +30 s if you need longer)")
        else:
            self.step_text.configure(text=f"{txt}\n(no time limit - press Stop when you're done)")
        if not self.rig.recording:
            self.rec_btn.configure(state="normal")

    def _ensure_audio(self):
        if not self.rig.running():
            return self.start_audio()
        return True

    def record_step(self):
        if self.busy:
            messagebox.showinfo(APP_NAME, "Still analysing the last take - one moment.")
            return
        if self.step_i >= len(self.steps()) or not self._ensure_audio():
            return
        name, dur, _ = self.steps()[self.step_i]
        self._start_rec(f"{self.step_i + 1}. {name}", dur, step=True)

    def record_free(self):
        if self.busy:
            messagebox.showinfo(APP_NAME, "Still analysing the last take - one moment.")
            return
        if not self._ensure_audio():
            return
        self._start_rec("free take", None)

    def _start_rec(self, label, limit, step=False):
        self.rig.begin()
        self.rec_started = time.time()
        self.rec_limit = limit
        self.rec_label = label
        self.rec_is_step = step
        self.rec_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.ext_btn.configure(state="normal" if limit else "disabled")
        log.info("recording '%s' (%s)", label, f"{limit} s" if limit else "no limit")

    def extend_record(self):
        if self.rig.recording and self.rec_limit:
            self.rec_limit += 30

    def _refresh_input_levels(self):
        """Enable the input-level slider for devices whose level Windows can set
        (USB mics); interface inputs are set with their hardware gain knobs."""
        for role in ROLES:
            m = self._sel(role)
            lvl = None
            p = self._sel("piezo")
            iface = m is not None and (
                (getattr(self, "iface_family", "") and dev_family(m[2]) == self.iface_family) or
                (p is not None and dev_family(m[2]) == dev_family(p[2])))
            if m is not None and not iface and WV.available():
                try:
                    lvl = WV.get_level(m[2])
                except Exception:
                    lvl = None
            if lvl is None:
                self.inlvl_scale[role].state(["disabled"])
                self.inlvl_scale[role].grid_remove()          # interface inputs: no slider at all
                self.inlvl_lab[role].configure(text="gain knob" if iface else "-")
            else:
                self.inlvl_scale[role].grid()
                self.inlvl_scale[role].state(["!disabled"])
                self.v_inlvl[role].set(lvl)
                self.inlvl_lab[role].configure(text=f"{lvl:.0f}%")

    def _set_input_level(self, role):
        m = self._sel(role)
        if m is None or "disabled" in self.inlvl_scale[role].state():
            return
        pct = float(self.v_inlvl[role].get())
        if WV.set_level(m[2], pct):
            self.inlvl_lab[role].configure(text=f"{pct:.0f}%")

    def reset_peaks(self):
        for r in list(self.rig.hold):
            self.rig.hold[r] = 0.0

    def _monitor_levels(self):
        for r in ROLES:
            g = float(self.v_gain[r].get())
            self.gain_lab[r].configure(text=f"{g:+.0f} dB")
            self.rig.mon_gain[r] = 10 ** (g / 20)
        m = float(self.v_mon_master.get())
        self.mon_master_lab.configure(text=f"{m:+.0f} dB")
        self.rig.mon_master = 10 ** (m / 20)
        self.rig.mon_mute = {r for r in ROLES if self.v_mute[r].get()}
        self.rig.mon_solo = {r for r in ROLES if self.v_solo[r].get()}

    def _monitor_changed(self):
        self._monitor_levels()
        if not self.v_mon.get():
            self.rig.stop_monitor()
            return
        if not self.rig.running() and not self.start_audio():
            self.v_mon.set(False)
            return
        self._start_monitor()

    def _start_monitor(self):
        out = dev_index(self.out_dev.get())
        if out is None:
            messagebox.showinfo(APP_NAME, "Choose the headphone output on the Setup tab "
                                "('Audition output').")
            self.v_mon.set(False)
            return
        try:
            self.rig.start_monitor(out, int(self.fs.get()))
            self.mon_dev_lab.configure(text="-> " + self.out_dev.get().split(":", 1)[-1].split("  (")[0])
        except Exception as exc:
            log.exception("monitor failed")
            self.v_mon.set(False)
            messagebox.showerror(APP_NAME, f"Couldn't open the headphone output: {exc}")

    def stop_record(self):
        if not self.rig.recording:
            return
        self.stop_btn.configure(state="disabled")
        self.update_idletasks()
        data = self.rig.end()
        self.rec_started = None
        self.rec_time.configure(text="")
        self.stop_btn.configure(state="disabled")
        self.ext_btn.configure(state="disabled")
        if self.step_i < len(self.steps()):
            self.rec_btn.configure(state="normal")
        if self.rig.xruns:
            self.logln(f"note: {self.rig.xruns} audio dropout(s) during the take - affected audio "
                       "will mostly be rejected; close other apps if this keeps happening.")
        x = data.get("piezo", np.zeros(0))
        fs = self.fs.get()
        two = {r: self.rig.roles[r][0] != self.rig.roles["piezo"][0] for r in data if r != "piezo"}
        if getattr(self, "rec_is_demo", False):
            self.rec_is_demo = False
            self._demo_done(data, fs, two)
            return
        mics = {}
        for s in SLOTS:
            if s in data and len(data[s]) >= 2 * fs:
                mics[s] = (data[s], two[s])
        extras = {r: data[r] for r in ("piezo2", "I") if r in data and len(data[r]) >= 2 * fs}
        if len(x) < 2 * fs or not mics:
            self.logln(f"[{self.rec_label}] too short - ignored.")
            return
        self.last_take = {"fs": fs, "two": two, **{r: v for r, v in data.items()}}
        self._process_take(x, mics, self.rec_label, advance=getattr(self, "rec_is_step", False),
                           extras=extras, x2=extras.get("piezo2"))

    def _get_acc(self, s):
        if self.acc[s] is None:
            fs = int(self.fs.get())
            n48 = self.profile()["nfft48"]
            self.acc[s] = E.Accumulator(fs, n48 * 2 if fs > 50000 else n48)
        return self.acc[s]

    def _process_take(self, x, mics, label, advance=False, save_raw=True, extras=None, x2=None):
        """x = piezo; mics = {slot: (y, two_device)} - all from the same take.
        x2 = second piezo element (dual piezo), same device as x."""
        accs = {s: self._get_acc(s) for s in mics}
        self.busy = True
        self.status.set(f"Saving + analysing '{label}'...")
        self.last_piezo = (x.copy(), int(self.fs.get()))
        idx = None
        if save_raw:
            self._begin_autosave_session()
            idx = self.take_counter
            self.take_counter += 1
        fs = int(self.fs.get())

        def work():
            if idx is not None:
                self._save_raw_take(idx, label, x, mics, fs, extras or {})
            done = []
            for s, (y, two) in mics.items():
                try:
                    rep = accs[s].process_take(x, y, label, two_device=two, x2=x2)
                    if rep.get("accepted"):
                        done.append(s)
                    self.q.put(("take", s, rep, False))
                except Exception as exc:
                    log.exception("processing take %s mic %s", label, s)
                    self.q.put(("error", f"[{self.mic_name(s)}] [{label}] {exc}", traceback.format_exc()))
            self.q.put(("take_done", done, advance))
        threading.Thread(target=work, daemon=True).start()

    def _process_async(self, x, y, label, two_dev, advance=False):
        """Single-mic take into the slot currently shown (used by WAV import)."""
        self._process_take(x, {self.slot.get(): (y, two_dev)}, label, advance)

    def undo_take(self):
        if not self.take_stack:
            return
        slots = self.take_stack.pop()
        for s in slots:
            if self.acc[s] is not None and self.acc[s].takes:
                name = self.acc[s].undo_last()
                self.logln(f"Removed take from {self.mic_name(s)}: {name}")
        self.irm = None
        self._update_coverage()
        self.autosave()

    def logln(self, txt):
        self.log.insert("end", txt + "\n")
        self.log.see("end")
        log.info("log: %s", txt)

    def _report_take(self, s, rep, advance):
        line = (f"[{self.mic_name(s)}] {rep['name']}: kept {rep.get('accepted', 0)}/{rep.get('segments_total', 0)} "
                f"segments")
        if "delay_ms" in rep and rep.get("accepted"):
            line += f", mic delay {rep['delay_ms']:.2f} ms"
        if "drift_ppm" in rep:
            line += (f", device sync {rep['offset_ms']:+.1f} ms / {rep['drift_ppm']:+.1f} ppm"
                     + (" (session clock)" if rep.get("used_session_drift") else ""))
        if rep.get("polarity") == "inverted":
            line += ", mic polarity inverted vs piezo (handled automatically)"
        self.logln(line)
        for w in rep["warnings"]:
            self.logln("   ! " + w)
        acc = self.acc.get(s)
        if acc is not None and acc.has_dual() and s == next(k for k in SLOTS if self._has(k)):
            self.logln("   " + self._dual_summary(acc))
        self.irm = None
        self._update_coverage()

    def _dual_summary(self, acc):
        """One line: what the second piezo element adds over the summed pickup."""
        g = [b for b in acc.dual_gain() if 80 <= b["center"] <= 8000]
        if not g:
            return "Dual piezo: not enough data yet."
        us = sum(b["single"] for b in g) / len(g)
        ud = sum(b["dual"] for b in g) / len(g)
        tot = 10 * np.log10(us / max(ud, 1e-9))
        big = [b for b in g if b["gain_db"] >= 3]
        where = ""
        if big:
            lo, hi = big[0]["center"], big[-1]["center"]
            fmt = lambda f: f"{f / 1000:g} kHz" if f >= 1000 else f"{f:.0f} Hz"
            where = f", most in {fmt(lo)}-{fmt(hi)}" if lo != hi else f", most around {fmt(lo)}"
        if tot < 1.0 and not big:
            verdict = "the two elements say almost the same thing - the summed pickup is as good."
        elif tot < 3.0:
            verdict = f"a small gain ({tot:.1f} dB less unexplained sound{where})."
        else:
            verdict = f"a real gain: {tot:.1f} dB less unexplained sound than the summed pickup{where}."
        return "Dual piezo: " + verdict

    def _update_coverage(self):
        if hasattr(self, "ir_src_rb"):
            self._ir_src_state()
            self._refresh_audition_names()
        ax = self.cov_ax
        ax.clear()
        acc = self.acc[self.slot.get()]
        if acc is None or acc.empty():
            ax.text(0.5, 0.5, "No takes yet", ha="center", va="center", transform=ax.transAxes,
                    color="#888")
            ax.set_xticks([])
            ax.set_yticks([])
            self.cov_canvas.draw_idle()
            self.cov_summary.configure(text="")
            self.tips.configure(text="Follow the guided steps. Suggestions appear here after the "
                                "first take.")
            return
        cov = acc.coverage(min_band=self.profile()["min_band"])
        xs = np.arange(len(cov))
        vals = [min(c["unc_db"], 3.0) for c in cov]
        cols = [STATUS_COL[c["status"]] for c in cov]
        ax.bar(xs, vals, color=cols, width=0.8)
        ax.axhline(E.GOOD_DB, color=STATUS_COL["good"], lw=0.8, ls="--")
        ax.axhline(E.OK_DB, color=STATUS_COL["ok"], lw=0.8, ls="--")
        ax.set_ylim(0, 3.05)
        ax.set_ylabel("uncertainty, +/- dB (lower is better)", fontsize=8)
        labs = [(f"{c['center'] / 1000:g}k" if c["center"] >= 1000 else f"{c['center']:g}") for c in cov]
        ax.set_xticks(xs)
        ax.set_xticklabels(labs, rotation=90, fontsize=7)
        ax.tick_params(axis="y", labelsize=7)
        ax.set_xlabel("Hz  (green = solid, yellow = ok, orange = weak, grey = no data yet)", fontsize=8)
        self.cov_fig.tight_layout()
        self.cov_canvas.draw_idle()
        un_db, frac = acc.explained_db()
        n_good = sum(c["status"] == "good" for c in cov)
        self.cov_summary.configure(
            text=f"Takes: {len(acc.takes)}   Segments used: {acc.n_seg}   "
                 f"Bands solid: {n_good}/{len(cov)}\n"
                 f"The instrument signal explains {frac * 100:.2f}% of the mic signal "
                 f"(residual {un_db:.1f} dB). Below -20 dB is very good; worse usually means room "
                 f"noise, a moving mic, or a mic too far away.")
        tips = E.suggestions(cov, instrument=self.instrument.get())
        self.tips.configure(text="\n".join("- " + t for t in tips) if tips else
                            "All bands are solid. More takes will only refine it slightly - "
                            "go to Build & Export.")

    # ------------------------------------------------------------------ build tab
    def _build_build(self):
        t = self._scroll_body(self.tab_build)
        left = ttk.Frame(t)
        left.pack(side="left", fill="y", padx=10, pady=10)
        right = ttk.Frame(t)
        right.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)

        p = ttk.LabelFrame(left, text="IR settings")
        p.pack(fill="x")
        self.v_smooth = tk.StringVar(value="1/12 oct (recommended)")
        self.v_reg = tk.DoubleVar(value=70)
        self.v_lc = tk.DoubleVar(value=35)
        self.v_hc = tk.DoubleVar(value=18000)
        self.v_phase = tk.StringVar(value="minimum")
        self.v_blend = tk.DoubleVar(value=0)
        self.v_inv = tk.BooleanVar(value=False)
        self.v_name = tk.StringVar(value="MyGuitar")
        self.v_master_ms = tk.StringVar(value="1000")
        r = 0

        def row(label, w, tip=None):
            nonlocal r
            lab = ttk.Label(p, text=label + ("  (?)" if tip and label else ""))
            lab.grid(row=r, column=0, sticky="w", padx=6, pady=3)
            w.grid(row=r, column=1, sticky="we", padx=6, pady=3)
            if tip:
                self._tip(lab, tip)
                self._tip(w, tip)
                for ch in w.winfo_children():
                    self._tip(ch, tip)
            r += 1

        TIPS = BUILD_TIPS
        row("Name", ttk.Entry(p, textvariable=self.v_name, width=22), TIPS["name"])
        row("Smoothing", ttk.Combobox(p, values=list(SMOOTH_OPTS), textvariable=self.v_smooth,
                                      state="readonly", width=22), TIPS["smooth"])
        regf = ttk.Frame(p)
        ttk.Scale(regf, from_=40, to=100, variable=self.v_reg, length=140).pack(side="left")
        self.reg_lab = ttk.Label(regf, width=6)
        self.reg_lab.pack(side="left")
        self.v_reg.trace_add("write", lambda *a: self.reg_lab.configure(text=f"{self.v_reg.get():.0f} dB"))
        self.reg_lab.configure(text="70 dB")
        row("Noise protection", regf, TIPS["reg"])
        row("Low cut (Hz, 0=off)", ttk.Entry(p, textvariable=self.v_lc, width=10), TIPS["lc"])
        row("High cut (Hz, 0=off)", ttk.Entry(p, textvariable=self.v_hc, width=10), TIPS["hc"])
        self.v_est = tk.StringVar(value="H1 - recommended")
        row("Algorithm", ttk.Combobox(p, values=list(ESTIMATORS), textvariable=self.v_est,
                                      state="readonly", width=28), TIPS["est"])
        row("Phase", ttk.Combobox(p, values=["minimum", "measured"], textvariable=self.v_phase,
                                  state="readonly", width=22), TIPS["phase"])
        row("Full IR length (ms)", ttk.Combobox(p, values=["250", "500", "750", "1000", "1500", "2000"],
                                                textvariable=self.v_master_ms, width=10), TIPS["len"])
        row("", ttk.Checkbutton(p, text="Invert polarity  (?)", variable=self.v_inv), TIPS["inv"])
        self._tip(ttk.Button(p, text="Build IR", style="Rec.TButton", command=self.build), TIPS["build"]).grid(
            row=r, column=0, columnspan=2, sticky="we", padx=6, pady=8)
        ttk.Label(left, wraplength=400, justify="left", foreground="#555", text=(
            "Hover over any setting (or its (?) label) for an explanation.")
        ).pack(fill="x", pady=4)

        ped = ttk.LabelFrame(left, text="Export targets - choose your pedal first (it sets what Audition plays)")
        ped.pack(fill="x", pady=(4, 0))
        # bottom-pinned controls first, so they stay visible on small screens
        self.v_bundle = tk.BooleanVar(value=True)
        self.v_dual_export = tk.BooleanVar(value=False)
        self.v_daisy_files = tk.BooleanVar(value=True)
        exf = ttk.Frame(ped)
        exf.pack(side="bottom", fill="x", padx=4, pady=(2, 6))
        self._tip(ttk.Button(exf, text="Export selected...", style="Rec.TButton",
                             command=self.export), TIPS["export"]).pack(side="left", fill="x", expand=True)
        self._tip(ttk.Button(exf, text="Export ALL lengths...", style="Rec.TButton",
                             command=self.export_all),
                  "Exports every target in the list in one go - one file per pedal / length, each named "
                  "with the pedal and its length, e.g. MyGuitar_Short-Smart_Box_23ms_44.1k_1024.wav."
                  ).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._tip(ttk.Checkbutton(ped, text="Also save full-length IR + notes",
                                  variable=self.v_bundle), TIPS["bundle"]).pack(side="bottom", anchor="w", padx=4)
        self.dual_ck = ttk.Checkbutton(ped, text="Dual piezo: also export the IR pair (_P1 / _P2)",
                                       variable=self.v_dual_export, state="disabled")
        self.dual_ck.pack(side="bottom", anchor="w", padx=4)
        dz = ttk.Checkbutton(ped, text="Daisy targets: also .bin + .h files (for the firmware)",
                             variable=self.v_daisy_files)
        dz.pack(side="bottom", anchor="w", padx=4)
        self._tip(dz, "Long / Ultimate (Daisy) targets get three files of the SAME IR:\n"
                      "- .wav: 32-bit float - to listen to, or load in any IR loader / DAW\n"
                      "- .bin: raw 32-bit floats - for the Daisy to load from SD card / flash\n"
                      "- .h: a C header - to build the IR straight into the firmware\n"
                      "Untick to get only the .wav.")
        self._tip(self.dual_ck, "Only when Instrument 2 was recorded. Adds one IR per piezo element, for a "
                                "2-input convolver (e.g. a Daisy Seed with both elements wired to its two "
                                "inputs): element 1 through _P1, element 2 through _P2, outputs added. The "
                                "normal file is still made for single-input pedals.")
        of = ttk.Frame(ped)
        of.pack(side="bottom", fill="x", padx=4, pady=2)
        self._tip(ttk.Button(of, text="Add custom...", command=self.add_pedal), TIPS["add"]).pack(side="left")
        self._tip(ttk.Button(of, text="Remove", command=self.remove_pedal), TIPS["rem"]).pack(side="left", padx=4)
        self.v_norm = tk.StringVar(value="Match loudness")
        self._tip(ttk.Combobox(of, values=["Match loudness", "Peak -1 dBFS"], textvariable=self.v_norm,
                               state="readonly", width=14), TIPS["norm"]).pack(side="left", padx=(8, 0))
        self.v_bits = tk.StringVar(value="24-bit")
        self._tip(ttk.Combobox(of, values=["16-bit", "24-bit", "32-bit float"], textvariable=self.v_bits,
                               state="readonly", width=10), TIPS["bits"]).pack(side="left", padx=4)
        cols = ("ms", "err")
        tf = ttk.Frame(ped)
        tf.pack(fill="x", padx=4, pady=4)
        self.ped_tree = ttk.Treeview(tf, columns=cols, show="tree headings", height=5,
                                     selectmode="extended")
        self.ped_tree.heading("#0", text="Target")
        self.ped_tree.column("#0", width=270)
        for c, h, w in (("ms", "Length", 70), ("err", "Error", 70)):
            self.ped_tree.heading(c, text=h)
            self.ped_tree.column(c, width=w, anchor="e")
        tsb = ttk.Scrollbar(tf, command=self.ped_tree.yview)
        self.ped_tree.configure(yscrollcommand=tsb.set)
        self.ped_tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="left", fill="y")
        self.ped_tree.bind("<<TreeviewSelect>>", lambda e: (self.preview_fit(), self._aud_target_changed()))
        self._tip(self.ped_tree, TIPS["targets"] + "\n\n" + "\n".join(f"- {p['name']}: {p['tip']}"
                                                                        for p in PRESET_TARGETS))
        self._fill_pedals()

        self._build_mix(right)
        self._build_audition(right)
        self.fit_info = ttk.Label(right, text="", justify="left", wraplength=700)
        self.fit_info.bind("<Configure>", lambda e: self.fit_info.configure(wraplength=max(300, e.width - 8)))
        self.fit_info.pack(side="bottom", fill="x", pady=4)
        pf = ttk.LabelFrame(right, text="Frequency response - what the IR adds to the piezo  |  Impulse response")
        pf.pack(fill="both", expand=True)
        hdr = ttk.Frame(pf)
        hdr.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Label(hdr, text="Show:").pack(side="left")
        self.v_show = {}
        for key, text in (("A", "Reference Mic"), ("B", "Color Mic"), ("dry", "Piezo dry"),
                          ("ir", "IR (full length)"), ("fit", "IR at target length")):
            self.v_show[key] = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(hdr, text=text, variable=self.v_show[key], fg=PLOT_COL.get(
                {"dry": "piezo"}.get(key, key), "#111"), selectcolor="white", font=("Segoe UI", 9, "bold"),
                command=lambda: self._plot_master(getattr(self, "_last_fit", None)) if self.irm is not None else None)
            cb.pack(side="left", padx=(4, 2))
            self._tip(cb, "Tick / untick to show or hide this line on the graph.")
        self._tip(ttk.Button(hdr, text="\u25b6 Play IR", command=self._play_ir),
                  "Plays the impulse response once (the selected target's version) through the Audition "
                  "output - a quick 'tock' you can compare between settings.").pack(side="right")
        self.fig = Figure(figsize=(7, 2.2 if self.compact else 2.8), dpi=100)
        gs = self.fig.add_gridspec(1, 2, width_ratios=[2.5, 1])
        self.ax = self.fig.add_subplot(gs[0, 0])
        self.ax2 = self.fig.add_subplot(gs[0, 1])
        self.ax2.set_box_aspect(1)
        self.canvas = FigureCanvasTkAgg(self.fig, master=pf)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self._plot_empty()

    def _fill_pedals(self):
        self.ped_tree.delete(*self.ped_tree.get_children())
        for i, pd in enumerate(self.pedals):
            ms = pd["samples"] / pd["fs"] * 1000
            self.ped_tree.insert("", "end", iid=str(i), text=pd["name"],
                                 values=(f"{ms:.0f} ms" if ms >= 10 else f"{ms:.1f} ms", ""))
        if not self.ped_tree.selection() and self.pedals:
            self.ped_tree.selection_set("0")
            self.ped_tree.focus("0")

    def add_pedal(self):
        win = tk.Toplevel(self)
        win.title("Add custom target")
        win.transient(self)
        win.resizable(False, False)
        ttk.Label(win, text="Target name:").grid(row=0, column=0, sticky="e", padx=8, pady=(10, 4))
        name_v = tk.StringVar()
        ne = ttk.Entry(win, textvariable=name_v, width=30)
        ne.grid(row=0, column=1, sticky="w", padx=8, pady=(10, 4))
        ttk.Label(win, text="Sample rate (Hz):").grid(row=1, column=0, sticky="e", padx=8, pady=4)
        fs_v = tk.StringVar(value="48000")
        ttk.Combobox(win, textvariable=fs_v, values=["44100", "48000", "96000"], width=10).grid(
            row=1, column=1, sticky="w", padx=8)
        ttk.Label(win, text="Max IR length:").grid(row=2, column=0, sticky="e", padx=8, pady=4)
        lf = ttk.Frame(win)
        lf.grid(row=2, column=1, sticky="w", padx=8)
        n_v = tk.StringVar(value="1024")
        ttk.Entry(lf, textvariable=n_v, width=10).pack(side="left")
        unit_v = tk.StringVar(value="samples")
        ttk.Combobox(lf, textvariable=unit_v, values=["samples", "ms"], width=8, state="readonly").pack(
            side="left", padx=4)
        fmt_v = tk.StringVar(value="wav")
        ttk.Radiobutton(win, text="Normal pedal (WAV)", variable=fmt_v, value="wav").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=20, pady=(6, 0))
        ttk.Radiobutton(win, text="DIY firmware, e.g. Daisy Seed (also .bin + C header)", variable=fmt_v,
                        value="daisy").grid(row=4, column=0, columnspan=2, sticky="w", padx=20)
        err = ttk.Label(win, foreground="#c0392b")
        err.grid(row=5, column=0, columnspan=2, padx=8)

        def save():
            nm = name_v.get().strip()
            try:
                fs = int(float(fs_v.get()))
                n = float(n_v.get())
                if unit_v.get() == "ms":
                    n = n * fs / 1000
                n = int(round(n))
            except ValueError:
                err.configure(text="Sample rate and length must be numbers.")
                return
            if not nm:
                err.configure(text="Give the target a name.")
                return
            if not 8000 <= fs <= 192000 or not 32 <= n <= 1_000_000:
                err.configure(text="Sample rate 8000-192000 Hz, length 32 samples or more.")
                return
            entry = {"name": nm, "fs": fs, "samples": n}
            if fmt_v.get() == "daisy":
                entry["format"] = "daisy"
            self.pedals.append(entry)
            save_pedals(self.pedals)
            self._fill_pedals()
            win.destroy()
        bf = ttk.Frame(win)
        bf.grid(row=6, column=0, columnspan=2, pady=10)
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="left", padx=4)
        ttk.Button(bf, text="Save", command=save).pack(side="left", padx=4)
        ne.focus_set()
        win.grab_set()

    def remove_pedal(self):
        sel = sorted((int(i) for i in self.ped_tree.selection()
                      if not self.pedals[int(i)].get("preset")), reverse=True)
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a target you added yourself - the four presets "
                                "can't be removed.")
            return
        for i in sel:
            self.pedals.pop(i)
        save_pedals(self.pedals)
        self._fill_pedals()

    def params(self):
        return E.BuildParams(smoothing=SMOOTH_OPTS[self.v_smooth.get()],
                             reg_db=float(self.v_reg.get()),
                             low_cut=float(self.v_lc.get() or 0),
                             high_cut=float(self.v_hc.get() or 0),
                             phase=self.v_phase.get(),
                             blend_b={"A": 0.0, "B": 1.0}.get(self.v_ir_src.get(),
                                                              float(self.v_mix["mid"].get()) / 100),
                             mix_bands=self._mix_bands() if self.v_ir_src.get() == "mix" else None,
                             invert=bool(self.v_inv.get()),
                             estimator=ESTIMATORS.get(self.v_est.get(), "H1"),
                             master_ms=float(self.v_master_ms.get() or 500))

    def build(self):
        if self.busy:
            messagebox.showinfo(APP_NAME, "Still analysing the last take - give it a moment, "
                                "then press Build IR again.")
            return
        a, b = self.acc["A"], self.acc["B"]
        p = self.params()
        has_a = a is not None and not a.empty()
        has_b = b is not None and not b.empty()
        if not has_a and not has_b:
            if self.irm is not None and self.master_src not in (None, "built"):
                self.fits = {}
                self._plot_master()
                return
            messagebox.showinfo(APP_NAME, "Record some takes on the Capture tab first "
                                "(or File > Open an IR WAV).")
            return
        acc0 = a if has_a else b
        max_ms = int(0.75 * acc0.nfft / acc0.fs * 1000)
        clamp_note = ""
        if p.master_ms > max_ms:
            clamp_note = (f" Full length limited to {max_ms} ms by this session's analysis size"
                          " (choose 'double bass' before capturing for up to 2 s).")
            p.master_ms = max_ms
        if not has_b:
            p.blend_b, p.mix_bands = 0.0, None
        if not has_a:
            p.blend_b, p.mix_bands = 1.0, None
        try:
            log.info("build: A=%s B=%s params=%s", a.n_seg if has_a else 0, b.n_seg if has_b else 0, p)
            self.irm = E.build_master(a, b, p, self.cal["A"], self.cal["B"])
        except Exception as exc:
            log.exception("build failed")
            messagebox.showerror(APP_NAME, f"Couldn't build the IR: {exc}\n\nYour takes are safe "
                                 "(autosaved). Details are in the log (Help > Open logs folder).")
            return
        for w in self.irm.get("warnings", []):
            self.logln("BUILD: " + w)
        self.master_src = "built"
        self.fits = {}
        self._aud_cache = {}
        self.dual_pair = None
        if any(self.acc[k] is not None and self.acc[k].has_dual() for k in SLOTS):
            try:
                self.dual_pair = E.build_dual(a if has_a else None, b if has_b else None, p, self.irm,
                                              self.cal["A"], self.cal["B"])
            except Exception as exc:
                log.exception("dual build failed")
                self.logln(f"BUILD: dual-piezo IR pair couldn't be made: {exc}")
        # recorded with two piezo elements -> export the pair too; single piezo -> off
        self.v_dual_export.set(self.dual_pair is not None)
        self.dual_ck.state(["!disabled"] if self.dual_pair is not None else ["disabled"])
        self._ir_src_state()
        self._aud_target_changed()
        for iid in self.ped_tree.get_children():
            self.ped_tree.set(iid, "err", "")
        self._plot_master()
        msg = (f"Built: {len(self.irm['h'])} samples @ {self.irm['fs']} Hz, "
               f"{self.irm['phase']} phase")
        if p.phase == "measured" and self.irm["phase"] == "minimum":
            msg += " (blending two mics always uses minimum phase, to avoid comb filtering)"
        if self.irm["removed_predelay_ms"] > 0:
            msg += f", removed {self.irm['removed_predelay_ms']:.2f} ms mic-distance delay"
        if self.irm.get("warnings"):
            msg += ". Note: " + " ".join(self.irm["warnings"])
        if self.dual_pair is not None:
            acc_d = self.acc[self.dual_pair["mic"]]
            msg += (f". Also built a dual-piezo IR PAIR (one IR per element, from the "
                    f"{self._short_name(self.dual_pair['mic'])}) - {self._dual_summary(acc_d)[12:].rstrip('.')}")
        self.fit_info.configure(text=msg + "." + clamp_note + " Select a target to see how it fits.")
        if self.ped_tree.selection():
            self.preview_fit()

    def _plot_empty(self):
        for ax in (self.ax, self.ax2):
            ax.clear()
        self.ax.text(0.5, 0.5, "Build the IR to see its response", ha="center", va="center",
                     transform=self.ax.transAxes, color="#888")
        self.ax.set_axis_off()
        self.ax2.set_visible(False)
        self.canvas.draw_idle()

    def _plot_master(self, fit=None):
        """Draw the response graphs. A drawing problem must never take the app down,
        so any failure is logged and replaced by a simple message on the graph."""
        self._last_fit = fit
        try:
            self._plot_master_inner(fit)
            self.canvas.draw()                    # draw now, so errors are caught here
        except Exception:
            log.exception("plot failed (fit=%s)", None if fit is None else fit.get("N"))
            try:
                for ax in (self.ax, self.ax2):
                    ax.clear()
                self.ax.text(0.5, 0.5, "Graph couldn't be drawn (details in the log) - the IR "
                             "itself is fine and can be exported.", ha="center", va="center",
                             transform=self.ax.transAxes, color="#a33", wrap=True)
                self.ax2.set_visible(False)
                self.canvas.draw()
            except Exception:
                log.exception("fallback plot failed")

    def _plot_master_inner(self, fit=None):
        m = self.irm
        ax, ax2 = self.ax, self.ax2
        ax.clear()
        ax2.clear()
        f = m["freqs"]
        sel = (f >= 20) & (f <= min(20000, m["fs"] / 2))
        p = self.params()
        ref = (f > 200) & (f < 5000)
        src = self.v_ir_src.get() if hasattr(self, "v_ir_src") else "mix"
        show = lambda k: not hasattr(self, "v_show") or bool(self.v_show[k].get())
        ax.set_xscale("log")
        for s_, col in (("A", PLOT_COL["A"]), ("B", PLOT_COL["B"])):
            acc = self.acc[s_]
            if acc is None or acc.empty() or not show(s_):
                continue
            Ps, _ = E._power_response(acc, p, self.cal[s_])
            Ps = E.smooth_frac(Ps, f, 1 / 12)
            ax.semilogx(f[sel], finite(E.db(Ps[sel] / max(np.mean(Ps[ref]), 1e-30)), -200), color=col,
                        lw=1.1, alpha=0.9, label=f"{self._short_name(s_)} correction")
        acc0 = next((self.acc[s_] for s_ in SLOTS if self.acc[s_] is not None and not self.acc[s_].empty()), None)
        if acc0 is not None and self.master_src in (None, "built"):
            Px = np.interp(f, acc0.freqs, acc0.Sxx)
            Px = E.smooth_frac(Px, f, 1 / 12)
            dry = finite(E.db(Px[sel] / max(np.mean(Px[ref]), 1e-30)), -200)
            if show("dry"):
                ax.semilogx(f[sel], dry,
                        color=PLOT_COL["piezo"], lw=1.1, ls=(0, (4, 2)), label="Piezo dry (its own tone)")
        Pm = E.smooth_frac(m["P"], f, 1 / 12)
        curve = finite(E.db(Pm[sel] / np.mean(Pm[ref])), -200)
        name = ({"mix": "Blended", "A": self._short_name("A"), "B": self._short_name("B")}[src]
                if self.master_src in (None, "built") else "Loaded")
        if show("ir"):
            ax.semilogx(f[sel], curve, color=PLOT_COL["ir"], lw=2.0, label=f"IR: {name} (full length)")
        if fit is not None and show("fit"):
            ff = fit["freqs"]
            sl = (ff >= 20) & (ff <= min(20000, fit["fs"] / 2))
            R = E.smooth_frac(fit["R"], ff, 1 / 12)
            T = fit["T"]
            refp = (ff > 200) & (ff < 5000)
            off = np.mean(E.db(T[refp])) - np.mean(E.db(R[refp]))
            ms = fit["N"] / fit["fs"] * 1000
            ax.semilogx(ff[sl], finite(E.db(R[sl] / max(np.mean(T[refp]), 1e-30)) + off, -200),
                        color=PLOT_COL["fit"], lw=1.4, ls="--", label=f"IR cut to {ms:.0f} ms (selected target)")
        ax.set_axis_on()
        ax.set_xlim(20, 20000)
        ticks = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t_ / 1000:g}k" if t_ >= 1000 else str(t_) for t_ in ticks])
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        band = (f[sel] > 40) & (f[sel] < 16000)
        top = float(np.max(curve[band]))
        if acc0 is not None and self.master_src in (None, "built"):
            top = max(top, min(top + 12, float(np.max(dry[band]))))
        if not np.isfinite(top):
            top = 0.0
        ax.set_ylim(top - 48, top + 6)
        ax.grid(True, which="both", alpha=0.25)
        ax.set_ylabel("dB")
        ax.set_xlabel("Hz", labelpad=0)
        # (no legend: the coloured 'Show' checkboxes above the graph name each line)
        ax2.set_visible(True)
        h = fit["h"] if fit is not None else m["h"]
        fs = fit["fs"] if fit is not None else m["fs"]
        n = min(len(h), int(0.1 * fs)) if fit is None else len(h)
        tms = np.arange(n) / fs * 1000
        dp = getattr(self, "dual_pair", None)
        if dp is not None and self.master_src in (None, "built"):
            nd = min(len(dp["h1"]), int(0.1 * dp["fs"]))
            td = np.arange(nd) / dp["fs"] * 1000
            ax2.plot(*decimate_line(td, dp["h1"][:nd]), color=PLOT_COL["A"], lw=0.6, alpha=0.8, label="pair: P1")
            ax2.plot(*decimate_line(td, dp["h2"][:nd]), color=PLOT_COL["piezo"], lw=0.6, alpha=0.8, label="P2")
        ax2.plot(*decimate_line(tms, finite(h[:n])), color=PLOT_COL["fit"] if fit is not None else PLOT_COL["ir"],
                 lw=0.7, label="IR")
        if dp is not None and self.master_src in (None, "built"):
            ax2.legend(fontsize=6, loc="upper right", frameon=False)
        ax2.set_xlabel("ms", labelpad=0)
        ax2.set_yticks([])
        ax2.tick_params(labelsize=7)
        ax2.set_title("IR" + (" (first 100 ms)" if fit is None else " at target length"), fontsize=9)
        ax2.grid(True, alpha=0.25)
        # fixed margins (not tight_layout): the graphs stay the same size whatever is plotted
        self.fig.subplots_adjust(left=0.065, right=0.99, top=0.92, bottom=0.15, wspace=0.14)
        # (drawn by _plot_master)

    def _fit(self, iid):
        if iid in self.fits:
            return self.fits[iid]
        pd = self.pedals[int(iid)]
        ref = E.loudness_ref_at(self.irm, pd["fs"]) if self.v_norm.get() == "Match loudness" else None
        r = E.fit_for_pedal(self.irm, pd["fs"], pd["samples"],
                            normalize="loudness" if ref else "peak", ref_level=ref,
                            fmin=self.profile()["fmin"])
        self.fits[iid] = r
        self.ped_tree.set(iid, "err", f"{r['err_vs_full_db']:.2f} dB")
        return r

    def preview_fit(self):
        if self.irm is None or getattr(self, "_suppress_preview", False) or self.busy:
            return
        sel = self.ped_tree.selection()
        if not sel:
            return
        try:
            log.info("preview fit: %s", self.pedals[int(sel[0])])
            r = self._fit(sel[0])
        except Exception as exc:
            log.exception("fit failed")
            messagebox.showerror(APP_NAME, f"Couldn't fit that target: {exc}")
            return
        pd = self.pedals[int(sel[0])]
        self._plot_master(r)
        log.info("preview fit done")
        self.fit_info.configure(text=(
            f"{pd['name']}: {r['N'] / r['fs'] * 1000:.1f} ms ({r['N']} samples at {r['fs'] / 1000:g} kHz, "
            f"the rate this device runs at)"
            f"{' (whole IR - fits within the ' + str(r['N_max']) + '-sample limit)' if r['N'] < r['N_max'] else ''}, "
            f"frequency resolution ~{r['freq_res_hz']:.{0 if r['freq_res_hz'] >= 10 else 2}f} Hz.  "
            f"Difference from the full IR: {r['err_vs_full_db']:.2f} dB RMS "
            f"({self.profile()['fmin']:.0f} Hz-12 kHz).  Method: {'optimised redesign' if r['method'] == 'fit' else 'trimmed'}"
            f", tail fade {int(r['fade'] * 100)}%.  "
            f"{'Under 1 dB is hard to hear; ' if r['err_vs_full_db'] < 1 else ''}"
            f"{'short IRs mainly lose low-end resonance detail.' if r['N'] / r['fs'] < 0.03 else ''}"
            f"{'  Dual-piezo IR pair ready: try Audition > Piezo 1 + 2.' if self.dual_pair is not None else ''}"))

    def export_all(self):
        self.export(all_targets=True)

    def export(self, all_targets=False):
        if self.irm is None:
            self.build()
            if self.irm is None:
                return
        sel = self.ped_tree.get_children() if all_targets else self.ped_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select one or more export targets in the list "
                                "(Ctrl/Shift-click to pick several).")
            return
        if not getattr(self, "project_path", None):
            if not messagebox.askokcancel(APP_NAME, "Save the project first - its IRs are exported next to "
                                          "it, into a folder called '<Project Name> IRs'."):
                return
            if not self.save_session(title="Save project (IRs export next to it)"):
                return
        else:
            try:
                self._write_project(self.project_path)          # keep the project in step with the IRs
            except Exception:
                log.exception("re-saving project before export failed")
        folder = self._project_ir_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't create '{folder}': {exc}")
            return
        bits = {"16-bit": 16, "24-bit": 24, "32-bit float": 32}[self.v_bits.get()]
        name = safe_name(self.v_name.get()) or safe_name(self._project_name()) or "IR"
        self.fits = {}
        done, notes = [], []
        t_start = time.time() - 1
        try:
            for iid in sel:
                pd = self.pedals[int(iid)]
                r = self._fit(iid)
                tag = file_tag(pd["name"])
                k = f"{pd['fs'] / 1000:g}k"
                ms = r["N"] / pd["fs"] * 1000
                base = f"{name}_{tag}_{ms:.0f}ms_{k}_{r['N']}"
                if pd.get("format") == "daisy" and self.v_daisy_files.get():
                    E.write_wav(os.path.join(folder, base + ".wav"), r["h"], pd["fs"], 32)
                    E.write_bin(os.path.join(folder, base + ".bin"), r["h"])
                    ident = E.write_c_header(os.path.join(folder, base + ".h"), r["h"], pd["fs"],
                                             f"{name}_{r['N']}")
                    files = f"{base}.wav / .bin / .h (array '{ident}')"
                else:
                    E.write_wav(os.path.join(folder, base + ".wav"), r["h"], pd["fs"], bits)
                    files = base + ".wav"
                if self.dual_pair is not None and self.v_dual_export.get():
                    fp = E.fit_pair(self.dual_pair, pd["fs"], pd["samples"])
                    for k, tag2 in (("h1", "P1"), ("h2", "P2")):
                        b2 = f"{base}_{tag2}"
                        if pd.get("format") == "daisy" and self.v_daisy_files.get():
                            E.write_wav(os.path.join(folder, b2 + ".wav"), fp[k], pd["fs"], 32)
                            E.write_bin(os.path.join(folder, b2 + ".bin"), fp[k])
                            E.write_c_header(os.path.join(folder, b2 + ".h"), fp[k], pd["fs"],
                                             f"{name}_{tag2}_{fp['N']}")
                        else:
                            E.write_wav(os.path.join(folder, b2 + ".wav"), fp[k], pd["fs"], bits)
                    files += f"  + dual pair {base}_P1 / _P2"
                done.append(f"{files}   ({r['err_vs_full_db']:.2f} dB from full)")
                notes.append(f"{pd['name']:<40} {pd['fs']:>6} Hz  {r['N']:>6} samples "
                             f"({r['N'] / pd['fs'] * 1000:7.1f} ms)  "
                             f"{r['err_vs_full_db']:5.2f} dB from full  -> {files}")
            if self.v_bundle.get():
                m = self.irm
                mfn = f"{name}_FULL_{m['fs'] / 1000:g}k_{len(m['h'])}_32f.wav"
                E.write_wav(os.path.join(folder, mfn), m["h"], m["fs"], 32)
                if self.dual_pair is not None and self.v_dual_export.get():
                    dp = self.dual_pair
                    for k, tag2 in (("h1", "P1"), ("h2", "P2")):
                        E.write_wav(os.path.join(folder, f"{name}_FULL_{tag2}_{dp['fs'] / 1000:g}k_"
                                                         f"{len(dp[k])}_32f.wav"), dp[k], dp["fs"], 32)
                # (the project itself is saved right next to this folder)
                p = self.params()
                with open(os.path.join(folder, name + "_info.txt"), "w") as fh:
                    fh.write(f"{name} - exported {time.strftime('%Y-%m-%d %H:%M')} by {APP_NAME}\n\n")
                    fh.write(f"Instrument: {self.instrument.get()}\n")
                    fh.write(f"Full IR: {mfn}  ({len(m['h']) / m['fs'] * 1000:.0f} ms, {m['phase']} phase)\n")
                    fh.write(f"Settings: smoothing {self.v_smooth.get()}, noise protection {p.reg_db:.0f} dB, "
                             f"low cut {p.low_cut:g} Hz, high cut {p.high_cut:g} Hz, "
                             f"blend A/B {100 - p.blend_b * 100:.0f}/{p.blend_b * 100:.0f}, "
                             f"invert {p.invert}, normalise {self.v_norm.get()}\n")
                    if self.master_src not in (None, "built"):
                        fh.write(f"Source: re-fitted from {self.master_src}\n")
                    if self.dual_pair is not None and self.v_dual_export.get():
                        fh.write("Dual piezo: files ending _P1 / _P2 are an IR PAIR - run element 1 through _P1 "
                                 "and element 2 through _P2, then add the two outputs. The plain files are "
                                 "for a single input (both elements summed).\n")
                    fh.write("\nTargets:\n" + "\n".join(notes) + "\n\n")
                    fh.write(f"Open {os.path.basename(self.project_path)} (next to this folder) in {APP_NAME} "
                             "to change settings, add takes, "
                             "or export for more pedals.\n")
                done += ["", f"Full-length master: {mfn}", f"Notes: {name}_info.txt",
                         f"Project (re-open to edit): {self.project_path}"]
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Export failed: {exc}")
            return
        if self.backup_dir:
            try:
                if not os.path.isdir(self.backup_dir):
                    raise OSError(f"the backup folder '{self.backup_dir}' isn't available")
                dst = os.path.join(self.backup_dir, os.path.basename(folder))
                if self.project_path and os.path.isfile(self.project_path):
                    shutil.copy2(self.project_path, os.path.join(self.backup_dir,
                                                                 os.path.basename(self.project_path)))
                os.makedirs(dst, exist_ok=True)
                n = 0
                for fn in os.listdir(folder):
                    src = os.path.join(folder, fn)
                    if os.path.isfile(src) and os.path.getmtime(src) >= t_start:
                        shutil.copy2(src, os.path.join(dst, fn))
                        n += 1
                done += ["", f"Also copied {n} file(s) to the backup folder: {dst}"]
            except Exception as exc:
                log.exception("backup copy failed")
                done += ["", f"Couldn't copy to the backup folder: {exc}"]
        messagebox.showinfo(APP_NAME, f"Exported to {folder}:\n\n" + "\n".join(done))

    # ------------------------------------------------------------------ mic mix + IR choice
    def _build_mix(self, parent):
        mf = ttk.LabelFrame(parent, text="Which IR to make")
        mf.pack(side="top", fill="x", pady=(0, 6))
        self.v_ir_src = tk.StringVar(value="mix")
        sf_ = ttk.Frame(mf)
        sf_.grid(row=0, column=0, columnspan=10, sticky="we", padx=6, pady=(2, 2))
        self.ir_src_rb = {}
        for key, text, tip in (
                ("mix", "Blended mics (set below)", "The IR comes from both mics, mixed by frequency "
                 "with the Lows / Mids / Highs sliders below."),
                ("A", "Reference Mic only", "The IR comes only from the Reference Mic - the most "
                 "accurate, natural sound."),
                ("B", "Color Mic only", "The IR comes only from the Color Mic - its character, "
                 "nothing else.")):
            rb = ttk.Radiobutton(sf_, text=text, value=key, variable=self.v_ir_src,
                                 command=self._ir_src_changed)
            rb.pack(side="left", padx=(0, 14))
            self._tip(rb, tip + " This is the IR that's shown, exported and auditioned as 'Piezo + "
                      "chosen IR'.")
            self.ir_src_rb[key] = rb
        self.v_mix = {"low": tk.DoubleVar(value=0), "mid": tk.DoubleVar(value=0), "high": tk.DoubleVar(value=0),
                      "x1": tk.DoubleVar(value=250), "x2": tk.DoubleVar(value=3000)}
        self.v_mix_link = tk.BooleanVar(value=True)
        self.mix_title = ttk.Label(mf, foreground="#555")
        self.mix_title.grid(row=1, column=0, columnspan=9, sticky="w", padx=6)
        self.mix_hint = ttk.Label(mf, foreground="#b45309")
        self.mix_hint.grid(row=4, column=0, columnspan=9, sticky="w", padx=6)
        self.mix_lab, self.mix_scales = {}, []
        for c, (k, name) in enumerate((("low", "Lows"), ("mid", "Mids"), ("high", "Highs"))):
            ttk.Label(mf, text=name).grid(row=2, column=c * 3, sticky="e", padx=(8, 2))
            sc = ttk.Scale(mf, from_=0, to=100, variable=self.v_mix[k], length=95,
                           command=lambda e, k=k: self._mix_changed(k))
            sc.grid(row=2, column=c * 3 + 1)
            self.mix_scales.append(sc)
            self.mix_lab[k] = ttk.Label(mf, width=4, text="0")
            self.mix_lab[k].grid(row=2, column=c * 3 + 2, sticky="w")
            self._tip(sc, f"{name}: 0 = use only the Reference Mic's IR in this range, 100 = only the Color "
                          "Mic's, in between = a mix. E.g. Lows 0 + Highs 70 = accurate bass from the "
                          "Reference Mic with the Color Mic's sparkle on top.")
        xf = ttk.Frame(mf)
        xf.grid(row=3, column=0, columnspan=9, sticky="w", padx=6, pady=(2, 4))
        ck = ttk.Checkbutton(xf, text="Link (one blend for everything)", variable=self.v_mix_link,
                             command=lambda: self._mix_changed("mid"))
        ck.pack(side="left")
        self.mix_scales.append(ck)
        self._tip(ck, "On: the three sliders move together - a simple overall blend. Off: set Lows, Mids "
                      "and Highs separately.")
        ttk.Label(xf, text="   Lows/Mids split at").pack(side="left")
        e1 = ttk.Entry(xf, textvariable=self.v_mix["x1"], width=6)
        e1.pack(side="left", padx=2)
        ttk.Label(xf, text="Hz    Mids/Highs split at").pack(side="left")
        e2 = ttk.Entry(xf, textvariable=self.v_mix["x2"], width=6)
        e2.pack(side="left", padx=2)
        ttk.Label(xf, text="Hz").pack(side="left")
        self.mix_scales += [e1, e2]
        self._tip(e1, "Where 'Lows' end and 'Mids' begin. Body boom lives around 80-250 Hz. "
                      "Press Build IR to apply.")
        self._tip(e2, "Where 'Mids' end and 'Highs' begin. Pick attack and 'air' live above ~3 kHz. "
                      "The IR rebuilds by itself a moment after you change the mix.")
        self.mix_fig = Figure(figsize=(3.0, 0.9), dpi=100)
        self.mix_ax = self.mix_fig.add_subplot(111)
        self.mix_canvas = FigureCanvasTkAgg(self.mix_fig, master=mf)
        if not self.compact:                  # small screens: the Blend graph window shows it instead
            self.mix_canvas.get_tk_widget().grid(row=1, column=9, rowspan=3, sticky="nsew", padx=(4, 6), pady=2)
        self._tip(self.mix_canvas.get_tk_widget(), "How much of the Color Mic is used at each frequency "
                  "(0 = all Reference Mic, 100 = all Color Mic).")
        mf.columnconfigure(9, weight=1)
        bw = ttk.Button(sf_, text="Blend graph \u2197", command=self.open_blend_window)
        bw.pack(side="right", padx=(6, 0))
        self._tip(bw, "Opens a bigger window showing the Reference Mic, Color Mic and blended responses - "
                      "it follows the sliders live while you blend.")
        self.blend_win = None
        self._blend_parts = None
        self._mix_job = None
        for k in ("x1", "x2"):
            self.v_mix[k].trace_add("write", lambda *a: self._mix_changed(None))
        self._ir_src_state()
        self._mix_changed(None)

    def _has(self, s):
        return self.acc.get(s) is not None and not self.acc[s].empty()

    def _ir_src_state(self):
        """Enable only the IR choices the session has data for."""
        if not hasattr(self, "ir_src_rb"):
            return
        has_a, has_b = self._has("A"), self._has("B")
        any_data = has_a or has_b
        self.ir_src_rb["A"].state(["!disabled"] if has_a or not any_data else ["disabled"])
        self.ir_src_rb["B"].state(["!disabled"] if has_b else ["disabled"])
        self.ir_src_rb["mix"].state(["!disabled"] if (has_a and has_b) or not any_data else ["disabled"])
        # the user's choice is kept (self._ir_src_pref) and only stood in for while the data
        # for it isn't there - e.g. for a moment while a take's second mic is still analysing
        want = getattr(self, "_ir_src_pref", "mix")
        ok = {"mix": has_a and has_b, "A": has_a, "B": has_b}
        if not any_data or ok[want]:
            pick = want
        else:
            pick = "mix" if ok["mix"] else ("A" if has_a else "B")
        if self.v_ir_src.get() != pick:
            self.v_ir_src.set(pick)
        on = pick == "mix"
        for w in self.mix_scales:
            w.state(["!disabled"] if on else ["disabled"])
        if hasattr(self, "mix_hint"):
            self.mix_hint.configure(text="" if on else (
                "These sliders are for 'Blended mics' - choose it above to use them." if ok["mix"] else
                "Blending needs takes from both the Reference Mic and the Color Mic."))

    def _ir_src_changed(self):
        self._ir_src_pref = self.v_ir_src.get()           # a deliberate choice: remember it
        self._ir_src_state()
        if self.irm is not None and self.master_src == "built":
            self.build()

    def _mix_changed(self, k):
        if k is not None and self.v_mix_link.get():
            v = self.v_mix[k].get()
            for kk in ("low", "mid", "high"):
                if kk != k:
                    self.v_mix[kk].set(v)
        for kk in ("low", "mid", "high"):
            self.mix_lab[kk].configure(text=f"{self.v_mix[kk].get():.0f}")
        self._draw_mix_curve()
        self._update_blend_window()
        # rebuild once the slider has settled (not on every pixel of a drag)
        if (self.irm is not None and self.master_src == "built" and self.v_ir_src.get() == "mix"
                and self._has("A") and self._has("B") and not getattr(self, "_suppress_preview", False)):
            if self._mix_job is not None:
                self.after_cancel(self._mix_job)
            self._mix_job = self.after(600, self._mix_rebuild)

    # ---- live blend window
    def open_blend_window(self):
        if self.blend_win is not None and self.blend_win.winfo_exists():
            self.blend_win.lift()
            return
        if not (self._has("A") and self._has("B")):
            messagebox.showinfo(APP_NAME, "Blending needs takes from both the Reference Mic and the Color "
                                "Mic.")
            return
        w = tk.Toplevel(self)
        w.title("Blend - Reference Mic vs Color Mic")
        w.geometry("820x460")
        self.blend_win = w
        self.blend_fig = Figure(figsize=(8, 4.2), dpi=100)
        self.blend_ax = self.blend_fig.add_subplot(111)
        self.blend_ax2 = self.blend_ax.twinx()
        self.blend_canvas = FigureCanvasTkAgg(self.blend_fig, master=w)
        self.blend_canvas.get_tk_widget().pack(fill="both", expand=True)
        ttk.Label(w, foreground="#555", text="Moves live with the Lows / Mids / Highs sliders. Shaded area = "
                  "how much Color Mic is used at each frequency.").pack(pady=(0, 6))
        w.protocol("WM_DELETE_WINDOW", lambda: (w.destroy(), setattr(self, "blend_win", None)))
        self._update_blend_window()

    def _blend_parts_now(self):
        """Level-matched responses of each mic (cached; depends on the IR settings, not the mix)."""
        p = self.params()
        key = repr((p.smoothing, p.reg_db, p.estimator, p.low_cut, p.high_cut,
                    self.acc["A"].n_seg, self.acc["B"].n_seg))
        if self._blend_parts is None or self._blend_parts[0] != key:
            f = self.acc["A"].freqs
            ref = (f > 200) & (f < 5000)
            parts = []
            for s_ in SLOTS:
                P, _ = E._power_response(self.acc[s_], p, self.cal[s_])
                P = E.smooth_frac(P, f, 1 / 12)
                if p.low_cut > 0:
                    P = P * E.butter_mag2(f, p.low_cut, 2, "hp")
                parts.append(P / max(np.mean(P[ref]), 1e-30))
            self._blend_parts = (key, f, parts[0], parts[1])
        return self._blend_parts[1:]

    def _update_blend_window(self):
        if getattr(self, "blend_win", None) is None or not self.blend_win.winfo_exists():
            return
        try:
            f, PA, PB = self._blend_parts_now()
            low, mid, high, x1, x2 = self._mix_bands()
            wB = E.band_weights(f, low, mid, high, x1, x2)
            P = (1 - wB) * PA + wB * PB
            sel = (f >= 20) & (f <= 20000)
            ax, ax2 = self.blend_ax, self.blend_ax2
            ax.clear()
            ax2.clear()
            ax2.fill_between(f[sel], 0, wB[sel] * 100, color=PLOT_COL["B"], alpha=0.12, lw=0)
            ax2.set_ylim(0, 100)
            ax2.yaxis.tick_right()
            ax2.yaxis.set_label_position("right")
            ax2.set_ylabel("Color Mic share %", fontsize=8, color=PLOT_COL["B"])
            ax2.tick_params(labelsize=7)
            ax.semilogx(f[sel], E.db(PA[sel]), color=PLOT_COL["A"], lw=1.1, label=self._short_name("A"))
            ax.semilogx(f[sel], E.db(PB[sel]), color=PLOT_COL["B"], lw=1.1, label=self._short_name("B"))
            ax.semilogx(f[sel], E.db(P[sel]), color=PLOT_COL["ir"], lw=2.2, label="Blended")
            ax.set_zorder(ax2.get_zorder() + 1)
            ax.patch.set_visible(False)
            ticks = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
            ax.set_xticks(ticks)
            ax.set_xticklabels([f"{t_ / 1000:g}k" if t_ >= 1000 else str(t_) for t_ in ticks])
            ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
            ax.set_xlim(20, 20000)
            top = float(np.max(E.db(P[(f > 40) & (f < 16000)])))
            ax.set_ylim(top - 45, top + 5)
            ax.grid(True, which="both", alpha=0.25)
            ax.set_ylabel("dB")
            ax.legend(fontsize=8, loc="lower left")
            ax.set_title(f"Lows {low * 100:.0f}  |  Mids {mid * 100:.0f}  |  Highs {high * 100:.0f}   "
                         f"(splits {x1:.0f} Hz / {x2:.0f} Hz)", fontsize=9)
            self.blend_fig.subplots_adjust(left=0.08, right=0.92, top=0.92, bottom=0.1)
            self.blend_canvas.draw_idle()
        except Exception:
            log.debug("blend window", exc_info=True)

    def _mix_rebuild(self):
        self._mix_job = None
        if self.busy or self.rig.recording:
            return
        self.build()

    def _draw_mix_curve(self):
        if not hasattr(self, "mix_ax"):
            return
        try:
            f = np.geomspace(20, 20000, 400)
            low, mid, high, x1, x2 = self._mix_bands()
            w = E.band_weights(f, low, mid, high, x1, x2)
            ax = self.mix_ax
            ax.clear()
            ax.fill_between(f, 0, w * 100, color=PLOT_COL["B"], alpha=0.25, lw=0)
            ax.semilogx(f, w * 100, color=PLOT_COL["B"], lw=1.5)
            ax.set_xlim(20, 20000)
            ax.set_ylim(-5, 105)
            ax.set_xticks([100, 1000, 10000])
            ax.set_xticklabels(["100", "1k", "10k"])
            ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
            ax.set_yticks([0, 50, 100])
            ax.tick_params(labelsize=6, pad=1)
            ax.set_title("Color Mic share %", fontsize=7, pad=2)
            ax.grid(True, alpha=0.2)
            self.mix_fig.subplots_adjust(left=0.13, right=0.98, top=0.8, bottom=0.22)
            self.mix_canvas.draw_idle()
        except Exception:
            log.debug("mix curve", exc_info=True)

    def _mix_bands(self):
        try:
            x1 = max(20.0, float(self.v_mix["x1"].get()))
            x2 = max(x1 * 1.2, float(self.v_mix["x2"].get()))
        except (tk.TclError, ValueError):
            x1, x2 = 250.0, 3000.0
        return (self.v_mix["low"].get() / 100, self.v_mix["mid"].get() / 100,
                self.v_mix["high"].get() / 100, x1, x2)

    # ------------------------------------------------------------------ demo recording
    def record_demo(self):
        if self.rig.recording:
            if getattr(self, "rec_is_demo", False):
                self.stop_record()
            else:
                messagebox.showinfo(APP_NAME, "Wait for the current take to finish first.")
            return
        if self.busy:
            messagebox.showinfo(APP_NAME, "Still analysing the last take - one moment.")
            return
        if not self._ensure_audio():
            return
        self.audition_stop()
        self.rec_is_demo = True
        self.demo_btn.configure(state="disabled")
        self._start_rec("demo", 60)
        self.rec_is_step = False
        self.logln("Recording a demo (up to 60 s): play something typical - a bit of strumming, some "
                   "single notes. Press Stop when done.")
        self._demo_rec_tick()

    def _demo_rec_tick(self):
        if not hasattr(self, "aud_rec_btn"):
            return
        if self.rig.recording and getattr(self, "rec_is_demo", False):
            el = time.time() - self.rec_started
            self.aud_rec_btn.configure(text=f"\u25cf  REC {el:.0f} s")
            self.aud_status.configure(text="RECORDING the piezo - play something typical...",
                                      foreground="#c0392b")
            try:                                   # live waveform (copy the block list first:
                blocks = list(self.rig.buf.get("piezo", []))   # the audio thread appends to it)
                if blocks:
                    fs = int(self.fs.get())
                    step = max(1, len(blocks) // 1500)
                    pk = np.array([np.max(np.abs(b_)) for b_ in blocks[::step]])
                    t = np.arange(len(pk)) * step * len(blocks[0]) / fs
                    ax = self.aud_ax
                    ax.clear()
                    ax.fill_between(t, -pk, pk, color="#c62828", lw=0)
                    ax.set_xlim(0, max(10.0, t[-1] if len(t) else 0))
                    ax.set_ylim(-1, 1)
                    ax.set_yticks([])
                    ax.tick_params(labelsize=7)
                    ax.set_title(f"Recording... {el:.0f} s", fontsize=8)
                    self.aud_canvas.draw_idle()
            except Exception:
                log.debug("live demo waveform", exc_info=True)
            self.after(200, self._demo_rec_tick)
        else:
            self.aud_rec_btn.configure(text="\u25cf  Record")

    def _demo_done(self, data, fs, two):
        """Called by stop_record for a demo take. Keeps the piezo (and Internal Mic); the
        mics only if asked, for comparison."""
        self.demo_btn.configure(state="normal")
        x = data.get("piezo", np.zeros(0))
        if len(x) < 3 * fs:
            self.logln("Demo too short - record at least a few seconds.")
            if hasattr(self, "aud_status"):
                self._refresh_audition_names()
            return
        keep = {"piezo", "piezo2", "I"} | ({"A", "B"} if self.v_demo_mics.get() else set())
        self._set_demo({r: v for r, v in data.items() if r in keep}, fs, two)

    def _set_demo(self, data, fs, two, save=True):
        self.demo = {"fs": int(fs), "two": dict(two), **{r: np.asarray(v, np.float32) for r, v in data.items()
                                                       if r in ROLES}}
        self._aud_cache = {}
        dur = len(self.demo["piezo"]) / fs
        have = ", ".join(self._short_name(r) for r in ROLES if r in self.demo)
        self.demo_lab.configure(text=f"Demo: {dur:.0f} s ({have}). Audition it on the Build tab.",
                                foreground="#2e7d32")
        self.logln(f"Demo recorded: {dur:.0f} s ({have}).")
        if save:
            self._begin_autosave_session()
            demo = dict(self.demo)
            roles = [r for r in ROLES if r in demo]

            def work():
                try:
                    import soundfile as sf
                    n = max(len(demo[r]) for r in roles)
                    a = np.zeros((n, len(roles)), np.float32)
                    for k, r in enumerate(roles):
                        a[:len(demo[r]), k] = demo[r]
                    sf.write(os.path.join(AUTOSAVE_DIR, "demo.flac"), np.clip(a, -1, 1), fs, subtype="PCM_24")
                    with open(os.path.join(AUTOSAVE_DIR, "demo.json"), "w") as fh:
                        json.dump({"fs": fs, "roles": roles, "lengths": [len(demo[r]) for r in roles],
                                   "two": {r: bool(v) for r, v in two.items()}}, fh)
                except Exception:
                    log.exception("saving demo failed")
            threading.Thread(target=work, daemon=True).start()
        if hasattr(self, "aud_status"):
            self.player.loop = None
            self.player.pos = 0
            self._refresh_audition_names()
            self._aud_update(play_if=False)

    def _load_demo(self, folder):
        """Reload the demo saved next to an autosaved session (demo.flac + demo.json)."""
        try:
            import soundfile as sf
            with open(os.path.join(folder, "demo.json")) as fh:
                meta = json.load(fh)
            a, fs = sf.read(os.path.join(folder, "demo.flac"), dtype="float32", always_2d=True)
            data = {r: a[:meta["lengths"][k], k] for k, r in enumerate(meta["roles"])}
            self._set_demo(data, fs, meta.get("two", {}), save=False)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("loading demo failed")

    # ------------------------------------------------------------------ audition
    AUD_RAW = (("dry", "Piezo (dry)"), ("A", "Reference Mic"), ("B", "Color Mic"), ("I", "Internal Mic"))
    AUD_IR = (("ir", "Piezo + chosen IR"), ("irA", "Piezo + Reference Mic IR"),
              ("irB", "Piezo + Color Mic IR"), ("irmix", "Piezo + Blended IR"),
              ("dual", "Piezo 1 + 2 through the IR pair"))

    def _build_audition(self, parent):
        au = tk.LabelFrame(parent, text=" Audition ", font=("Segoe UI", 14, "bold"), fg="#1f3b66",
                           bd=2, relief="groove", padx=4, pady=2)
        au.pack(side="bottom", fill="x", pady=(4, 0))
        self.player = AuditionPlayer(self.rig)
        self.rig.aud = self.player
        left = ttk.Frame(au)
        right = ttk.Frame(au)
        if self.compact:                       # small screens: waveform full width, under the controls
            left.pack(side="top", fill="x", padx=(6, 4), pady=4)
            right.pack(side="top", fill="both", expand=True, padx=(4, 6), pady=(0, 4))
        else:
            left.pack(side="left", fill="y", padx=(6, 4), pady=4)
            right.pack(side="left", fill="both", expand=True, padx=(4, 6), pady=4)

        self.aud_target_lab = tk.Label(left, anchor="w", justify="left", wraplength=520,
                                       font=("Segoe UI", 9, "bold"), fg="#1f4e9c")
        self.aud_target_lab.pack(fill="x")
        self._tip(self.aud_target_lab, "Every 'Piezo + IR' option plays the IR cut to the length of the "
                  "target selected in Export targets - so you hear what that pedal will really do. "
                  "Choose your pedal there first.")
        self.aud_src = tk.StringVar(value="dry")
        self.aud_rb = {}
        tips = {"dry": "The piezo exactly as recorded - no IR. The 'before'.",
                "A": "The real Reference Mic, as recorded with the demo - the sound the IR aims for. "
                     "(Greyed out? The demo was recorded without the mics: tick 'also record the "
                     "mics' and record a new demo.)",
                "B": "The real Color Mic, as recorded with the demo. (Greyed out? Tick 'also record "
                     "the mics' and record a new demo.)",
                "I": "The guitar's internal mic, as recorded.",
                "ir": "The piezo through the IR you chose under 'Which IR to make' - exactly what "
                      "you'll export.",
                "irA": "The piezo through an IR made from the Reference Mic only.",
                "irB": "The piezo through an IR made from the Color Mic only.",
                "irmix": "The piezo through the IR blended from both mics (the Lows / Mids / Highs mix).",
                "dual": "Dual piezo: each element through its own IR (the _P1 / _P2 pair), then added - "
                        "what a 2-input convolver would play. Compare with 'Piezo + chosen IR', which is "
                        "the summed pickup through one IR."}
        grid = ttk.Frame(left)
        grid.pack(fill="x", pady=(2, 0))
        ttk.Label(grid, text="As recorded", foreground="#555").grid(row=0, column=0, sticky="w", padx=4)
        ttk.Label(grid, text="Through the IR (at the target's length)", foreground="#555").grid(
            row=0, column=1, sticky="w", padx=(18, 4))
        for col, rows in ((0, self.AUD_RAW), (1, self.AUD_IR)):
            for k, (key, text) in enumerate(rows):
                rb = ttk.Radiobutton(grid, text=text, value=key, variable=self.aud_src,
                                     command=lambda: self._aud_update(play_if=True))
                rb.grid(row=k + 1, column=col, sticky="w", padx=(4 if col == 0 else 18, 4), pady=0)
                self.aud_rb[key] = rb
                self._tip(rb, tips[key])

        tf = ttk.Frame(left)
        tf.pack(fill="x", pady=(6, 2))
        self.aud_rec_btn = ttk.Button(tf, text="\u25cf  Record", style="Transport.TButton", width=11,
                                      command=self.record_demo)
        self.aud_rec_btn.pack(side="left")
        self._tip(self.aud_rec_btn, "Records a new demo from the piezo (up to 60 s) - play anything you "
                                    "want to hear through the IRs. Stop (or Record again) ends it. The "
                                    "Internal Mic / Instrument 2 are recorded too if they're switched on.")
        self.aud_play_btn = ttk.Button(tf, text="\u25b6  Play", style="Transport.TButton", width=11,
                                       command=self.audition_play)
        self.aud_play_btn.pack(side="left", padx=6)
        self._tip(self.aud_play_btn, "Plays the demo in a loop - the whole demo, or just the part you "
                                     "dragged across on the waveform.")
        st_ = ttk.Button(tf, text="\u25a0  Stop", style="Transport.TButton", width=11,
                         command=self.transport_stop)
        st_.pack(side="left")
        self._tip(st_, "Stops playback - or the demo recording, if one is running.")
        self.v_aud_loop = tk.BooleanVar(value=True)      # Play always loops
        cf = ttk.Frame(left)
        cf.pack(fill="x", pady=(4, 0))
        ttk.Label(cf, text="Volume").pack(side="left")
        self.v_aud_vol = tk.DoubleVar(value=0)
        vs = ttk.Scale(cf, from_=-24, to=12, variable=self.v_aud_vol, length=110, command=self._aud_vol)
        vs.pack(side="left", padx=4)
        self._aud_vol_lab = ttk.Label(cf, text="+0 dB", width=6)
        self._aud_vol_lab.pack(side="left")
        self._tip(vs, "Audition volume (doesn't affect the IR). Every option is already level-matched, so "
                      "you compare tone, not loudness.")

        bf = ttk.Frame(left)
        bf.pack(fill="x", pady=(4, 0))
        ttk.Label(bf, text="Blend in Internal Mic").pack(side="left")
        self.v_aud_int = tk.DoubleVar(value=0)
        self.aud_int_sc = ttk.Scale(bf, from_=0, to=100, variable=self.v_aud_int, length=110,
                                    command=self._aud_int_moved)
        self.aud_int_sc.pack(side="left", padx=4)
        self._aud_int_lab = ttk.Label(bf, text="0%", width=5)
        self._aud_int_lab.pack(side="left")
        self._tip(self.aud_int_sc, "Mix some of the guitar's internal mic in with whatever is playing - "
                                   "like a blender preamp. Needs an Internal Mic in the demo.")

        of = bf
        ttk.Label(of, text="   Output").pack(side="left")
        self.aud_out_cb = ttk.Combobox(of, textvariable=self.out_dev, state="readonly", width=26,
                                       values=list(self.out_cb["values"]))
        self.aud_out_cb.pack(side="left", padx=4)
        self.aud_out_cb.bind("<<ComboboxSelected>>", lambda e: self._aud_out_changed())
        self._tip(self.aud_out_cb, "Where Audition plays: your interface's headphones/outputs, or "
                                   "speakers / Bluetooth. Same setting as on the Setup tab.")

        rf = ttk.Frame(left)
        rf.pack(fill="x", pady=(4, 0))
        self.aud_rec_row = rf
        self.v_demo_mics = tk.BooleanVar(value=True)
        dm = ttk.Checkbutton(rf, text="Record: also record the mics, to compare", variable=self.v_demo_mics)
        dm.pack(side="left", padx=6)
        self._tip(dm, "Tick to also record the Reference / Color Mic with the demo, so you can A/B the "
                      "real mics against 'Piezo + IR'. Leave off to record the piezo only.")

        self.aud_status = ttk.Label(left, foreground="#555", wraplength=470, justify="left")
        self.aud_status.pack(fill="x", pady=(4, 0))

        # waveform + looper
        self.aud_fig = Figure(figsize=(3.4, 1.5 if self.compact else 1.7), dpi=100)
        self.aud_ax = self.aud_fig.add_subplot(111)
        self.aud_canvas = FigureCanvasTkAgg(self.aud_fig, master=right)
        self.aud_canvas.get_tk_widget().pack(fill="both", expand=True)
        lf = ttk.Frame(right)
        lf.pack(fill="x")
        ttk.Label(lf, text="Drag to loop a part", foreground="#555").pack(side="left")
        ttk.Button(lf, text="Whole demo", command=self._aud_clear_loop).pack(side="right")
        self.aud_span = SpanSelector(self.aud_ax, self._aud_on_span, "horizontal", useblit=True,
                                     interactive=True, drag_from_anywhere=True,
                                     props=dict(alpha=0.2, facecolor="#2e7d32"))
        self.aud_canvas.mpl_connect("button_press_event", self._aud_click)
        self._aud_bg = None
        self.aud_canvas.mpl_connect("draw_event", self._aud_on_draw)
        self._aud_cache = {}
        self._aud_y = None
        self._aud_head = None
        self._aud_refresh_job = None
        self._aud_tick_job = None
        self._refresh_audition_names()
        self._aud_draw()

    # -- labels / state
    def _short_name(self, role):
        """Label if given, else the generic name ('Piezo' for the instrument input)."""
        lab = self.inp_label[role].get().strip() if hasattr(self, "inp_label") else ""
        return lab or {"piezo": "Piezo", "piezo2": "Piezo 2"}.get(role, ROLE_BASE.get(role, role))

    def _target_iid(self):
        if not hasattr(self, "ped_tree"):
            return None
        sel = self.ped_tree.selection()
        if not sel:
            return None
        f = self.ped_tree.focus()
        return f if f in sel else sel[0]

    def _refresh_audition_names(self):
        if not hasattr(self, "aud_rb"):
            return
        src = self.demo
        self.aud_rb["dry"].configure(text=f"{self._short_name('piezo')} (dry)")
        for key in ("A", "B", "I"):
            self.aud_rb[key].configure(text=self._short_name(key))
            ok = src is not None and key in src
            self.aud_rb[key].state(["!disabled"] if ok else ["disabled"])
        self.aud_rb["irA"].configure(text=f"Piezo + {self._short_name('A')} IR")
        self.aud_rb["irB"].configure(text=f"Piezo + {self._short_name('B')} IR")
        built = self.master_src in (None, "built")
        has_a, has_b = self._has("A"), self._has("B")
        names = {"mix": "Blended", "A": self._short_name("A"), "B": self._short_name("B")}
        self.aud_rb["ir"].configure(text=(f"Piezo + chosen IR ({names[self.v_ir_src.get()]})" if built
                                          else "Piezo + loaded IR"))
        self.aud_rb["irA"].state(["!disabled"] if built and has_a else ["disabled"])
        self.aud_rb["irB"].state(["!disabled"] if built and has_b else ["disabled"])
        self.aud_rb["irmix"].state(["!disabled"] if built and has_a and has_b else ["disabled"])
        self.aud_int_sc.state(["!disabled"] if src is not None and "I" in src else ["disabled"])
        dual_ok = (src is not None and "piezo2" in src and getattr(self, "dual_pair", None) is not None
                   and self.master_src in (None, "built"))
        self.aud_rb["dual"].state(["!disabled"] if dual_ok else ["disabled"])
        iid = self._target_iid()
        if iid is None:
            self.aud_target_lab.configure(text="Step 1: select your pedal in 'Export targets' (left) - "
                                               "every IR here is played at that pedal's length.", fg="#c0392b")
        else:
            pd = self.pedals[int(iid)]
            self.aud_target_lab.configure(
                text=f"IRs play at:  {pd['name']}  ({pd['samples'] / pd['fs'] * 1000:.0f} ms)"
                     "  - change it in Export targets", fg="#1f4e9c")
        if src is None:
            self.aud_status.configure(text="No demo yet - press 'Record new demo' and play ~30 s of "
                                           "something typical.", foreground="#555")
        else:
            self.aud_status.configure(text=f"Demo: {len(src['piezo']) / src['fs']:.0f} s "
                                           f"({', '.join(self._short_name(r) for r in ROLES if r in src)}).",
                                      foreground="#555")

    # -- rendering
    def _params_for(self, src):
        p = self.params()
        if src == "mix":
            p.mix_bands = self._mix_bands()
        else:
            p.mix_bands = None
            p.blend_b = 0.0 if src == "A" else 1.0
        return p

    def _master_for(self, src):
        """The full-length IR for a source ('mix' / 'A' / 'B'), built on demand and cached."""
        if self.master_src not in (None, "built") and self.irm is not None:
            return self.irm                        # a loaded or tone-matched IR
        if src == self.v_ir_src.get() and self.irm is not None:
            return self.irm
        p = self._params_for(src)
        key = ("master", src, repr(p))
        if key in self._aud_cache:
            return self._aud_cache[key]
        a = self.acc["A"] if src in ("A", "mix") and self._has("A") else None
        b = self.acc["B"] if src in ("B", "mix") and self._has("B") else None
        if a is None and b is None:
            raise ValueError("No takes for that mic yet.")
        if a is None or b is None:
            p.mix_bands = None
            p.blend_b = 0.0 if a is not None else 1.0
        acc0 = a if a is not None else b
        p.master_ms = min(p.master_ms, int(0.75 * acc0.nfft / acc0.fs * 1000))
        m = E.build_master(a, b, p, self.cal["A"], self.cal["B"])
        self._aud_cache[key] = m
        return m

    def _fit_master(self, m, iid):
        pd = self.pedals[int(iid)]
        key = ("fit", id(m), iid, self.v_norm.get())
        if key not in self._aud_cache:
            self._aud_cache[key] = E.fit_for_pedal(m, pd["fs"], pd["samples"], normalize="peak",
                                                   fmin=self.profile()["fmin"])
        return self._aud_cache[key]

    def _render_audition(self, mode):
        src = self.demo
        if src is None:
            raise ValueError("Record a demo first: press 'Record new demo' (in Audition) and play "
                             "~30 s of something typical.")
        fs = src["fs"]
        x = np.asarray(src["piezo"], float)
        if "piezo2" in src:                               # dual piezo: the summed pickup
            x2 = np.asarray(src["piezo2"], float)
            m_ = min(len(x), len(x2))
            x1d, x2d = x[:m_], x2[:m_]
            x = x1d + x2d
        b = float(self.v_aud_int.get()) / 100 if "I" in src else 0.0
        iid = self._target_iid()
        key = (mode, round(b, 2), id(self.irm), iid, self.v_ir_src.get(), repr(self.params()))
        if key in self._aud_cache:
            return self._aud_cache[key], fs
        if mode in ("A", "B", "I"):
            if mode not in src:
                raise ValueError(f"The demo has no {self._short_name(mode)} recording - tick 'also record "
                                 "the mics' and record a new demo.")
            y = np.asarray(src[mode], float)
            if src.get("two", {}).get(mode):           # USB Mic: line it up with the piezo
                try:
                    _, y, _ = E.align_two_device(x, y, fs)
                except Exception:
                    log.info("demo alignment failed for %s", mode, exc_info=True)
            y = np.pad(y, (0, max(0, len(x) - len(y))))[:len(x)]
            b = 0.0 if mode == "I" else b
        elif mode == "dry":
            y = x
        elif mode == "dual":
            if "piezo2" not in src or self.dual_pair is None:
                raise ValueError("Needs dual-piezo takes (Instrument 2 on) and a demo recorded with "
                                 "Instrument 2 on.")
            if iid is None:
                raise ValueError("Select your pedal in 'Export targets' first.")
            pd_ = self.pedals[int(iid)]
            fp = E.fit_pair(self.dual_pair, pd_["fs"], pd_["samples"])
            y = (TM.through_ir(x1d, fs, {"h": fp["h1"], "fs": fp["fs"]}) +
                 TM.through_ir(x2d, fs, {"h": fp["h2"], "fs": fp["fs"]}))
        else:
            if iid is None:
                raise ValueError("Select your pedal in 'Export targets' first - the IR is played at "
                                 "that pedal's length.")
            if self.irm is None:
                self.build()
            if self.irm is None:
                raise ValueError("Build the IR first.")
            srcm = {"ir": self.v_ir_src.get(), "irA": "A", "irB": "B", "irmix": "mix"}[mode]
            r = self._fit_master(self._master_for(srcm), iid)
            y = TM.through_ir(x, fs, {"h": r["h"], "fs": r["fs"]})
        if b > 0:
            zi = np.asarray(src["I"], float)
            if src.get("two", {}).get("I"):
                try:
                    _, zi, _ = E.align_two_device(x, zi, fs)
                except Exception:
                    pass
            zi = np.pad(zi, (0, max(0, len(y) - len(zi))))[:len(y)]
            rms = lambda v: np.sqrt(np.mean(v ** 2)) + 1e-12
            y = (1 - b) * y / rms(y) + b * zi / rms(zi)
        for _ in range(3):              # same loudness for every mode, with peaks soft-limited
            y = y / (np.sqrt(np.mean(y ** 2)) + 1e-12) * 10 ** (-15 / 20)
            a = np.abs(y)
            y = np.where(a > 0.7, np.sign(y) * (0.7 + 0.29 * np.tanh((a - 0.7) / 0.29)), y)
        y = y.astype(np.float32)
        self._aud_cache[key] = y
        return y, fs

    def _aud_update(self, play_if=True):
        """Render the selected option and hand it to the player (keeps the play position, so
        switching while playing carries on from the same spot)."""
        mode = self.aud_src.get()
        if self.demo is None:
            self._aud_y = None
            self._aud_draw()
            return False
        try:
            if mode not in ("dry", "A", "B", "I"):
                self.status.set("Preparing the IR for audition...")
                self.update_idletasks()
            y, fs = self._render_audition(mode)
        except Exception as exc:
            self.status.set("Ready.")
            if play_if:
                messagebox.showinfo(APP_NAME, str(exc))
            log.info("audition render: %s", exc)
            return False
        self.player.set_buffer(y, fs)
        self._aud_y = (y, fs)
        self._aud_draw()
        self.status.set("Ready." if not self.player.playing else self._aud_playing_text())
        return True

    def _aud_playing_text(self):
        return f"Playing: {self.aud_rb[self.aud_src.get()].cget('text')}  ->  {self.player.dev_name}"

    # -- transport
    def audition_play(self):
        if sd is None:
            return
        if self.rig.recording:
            messagebox.showinfo(APP_NAME, "Stop the recording first.")
            return
        if not self._aud_update(play_if=True):
            return
        dev = dev_index(self.out_dev.get())
        try:
            self.player.start(dev)
        except Exception as exc:
            log.exception("audition output failed")
            messagebox.showerror(APP_NAME, f"Couldn't play on '{self.out_dev.get()}':\n{exc}\n\n"
                                 "Pick another Output, or set that device to the same sample rate as the "
                                 "app in Windows (Sound settings > device > Advanced). If you just "
                                 "connected it, press Refresh devices on the Setup tab.")
            return
        self.player.looping = bool(self.v_aud_loop.get())
        self.status.set(self._aud_playing_text())
        if self._aud_tick_job is None:
            self._aud_tick()

    def _play_ir(self):
        if self.irm is None:
            self.build()
            if self.irm is None:
                return
        iid = self._target_iid()
        try:
            r = self._fit(iid) if iid is not None else {"h": self.irm["h"], "fs": self.irm["fs"]}
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't prepare the IR: {exc}")
            return
        h = np.asarray(r["h"], float)
        fs = int(r["fs"])
        y = np.concatenate([np.zeros(int(0.05 * fs)), h, np.zeros(int(0.6 * fs))])
        y = (y / (np.max(np.abs(y)) + 1e-12) * 0.5).astype(np.float32)
        self.player.loop = None
        self.player.set_buffer(y, fs)
        self.player.pos = 0
        self.player.looping = False
        try:
            self.player.start(dev_index(self.out_dev.get()))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't play on '{self.out_dev.get()}': {exc}")
            return
        self._aud_y = None                           # the audition waveform is re-rendered on next Play
        self.status.set("Playing the IR...")
        if self._aud_tick_job is None:
            self._aud_tick()

    def transport_stop(self):
        if self.rig.recording and getattr(self, "rec_is_demo", False):
            self.aud_rec_btn.configure(text="Saving...")
            self.aud_status.configure(text="Stopping...", foreground="#555")
            self.update_idletasks()
            self.stop_record()
            return
        self.audition_stop()

    def audition_stop(self):
        if hasattr(self, "player"):
            self.player.stop()
            self._aud_draw_head()
        self.status.set("Ready.")

    def _aud_out_changed(self):
        if self.player.playing:
            self.player.stop(rewind=False)
            self.audition_play()

    def _aud_vol(self, _e=None):
        v = float(self.v_aud_vol.get())
        self.player.gain = 10 ** (v / 20)
        self._aud_vol_lab.configure(text=f"{v:+.0f} dB")

    def _aud_int_moved(self, _e=None):
        self._aud_int_lab.configure(text=f"{self.v_aud_int.get():.0f}%")
        if self._aud_refresh_job is not None:
            self.after_cancel(self._aud_refresh_job)
        self._aud_refresh_job = self.after(300, self._aud_int_apply)

    def _aud_int_apply(self):
        self._aud_refresh_job = None
        self._aud_update(play_if=False)

    def _aud_target_changed(self):
        if hasattr(self, "aud_rb"):
            self._refresh_audition_names()
            if self.aud_src.get() not in ("dry", "A", "B", "I") and self.demo is not None and self.irm is not None:
                self._aud_update(play_if=False)

    # -- waveform + looper
    def _aud_draw(self):
        ax = self.aud_ax
        ax.clear()
        self._aud_head = None
        if self._aud_y is None:
            ax.text(0.5, 0.5, "No demo yet" if self.demo is None else "Press Play", ha="center",
                    va="center", color="#888", transform=ax.transAxes)
            ax.set_xticks([])
            ax.set_yticks([])
        else:
            y, fs = self._aud_y
            t = np.arange(len(y)) / fs
            tt, yy = decimate_line(t, y, 1600)
            ax.plot(tt, yy, color="#1565c0" if self.aud_src.get() in ("dry", "A", "B", "I") else "#c62828",
                    lw=0.5)
            ax.set_xlim(0, len(y) / fs)
            ax.set_ylim(-1, 1)
            ax.set_yticks([])
            ax.tick_params(labelsize=7)
            ax.set_xlabel("s", fontsize=7, labelpad=0)
            if self.player.loop:
                a, b = self.player.loop
                ax.axvspan(a / fs, b / fs, color="#2e7d32", alpha=0.15)
            self._aud_head = ax.axvline(self.player.pos / fs, color="#111", lw=1, animated=True)
            ax.set_title(self.aud_rb[self.aud_src.get()].cget("text"), fontsize=8)
        self.aud_fig.subplots_adjust(left=0.03, right=0.98, top=0.86, bottom=0.2)
        self.aud_canvas.draw_idle()

    def _aud_on_draw(self, _e=None):
        try:
            self._aud_bg = self.aud_canvas.copy_from_bbox(self.aud_fig.bbox)
            if self._aud_head is not None:
                self.aud_ax.draw_artist(self._aud_head)
        except Exception:
            self._aud_bg = None

    def _aud_draw_head(self):
        """Move the playhead by redrawing only that line over a cached background (cheap -
        a full redraw 10x a second steals time from the audio and causes clicks)."""
        if self._aud_head is None or self._aud_y is None:
            return
        fs = self._aud_y[1]
        self._aud_head.set_xdata([self.player.pos / fs] * 2)
        if self._aud_bg is None:
            self.aud_canvas.draw_idle()
            return
        try:
            self.aud_canvas.restore_region(self._aud_bg)
            self.aud_ax.draw_artist(self._aud_head)
            self.aud_canvas.blit(self.aud_fig.bbox)
        except Exception:
            self.aud_canvas.draw_idle()

    def _aud_tick(self):
        self._aud_tick_job = None
        self._aud_draw_head()
        if self.player.playing:
            self._aud_tick_job = self.after(120, self._aud_tick)
        else:
            self.status.set("Ready.")

    def _aud_on_span(self, a, b):
        if self._aud_y is None:
            return
        fs = self._aud_y[1]
        n = len(self._aud_y[0])
        i0, i1 = sorted((int(a * fs), int(b * fs)))
        i0, i1 = max(0, i0), min(n, i1)
        if i1 - i0 < int(0.25 * fs):
            return
        self.player.loop = (i0, i1)
        self.player.pos = i0
        self.v_aud_loop.set(True)
        self.player.looping = True
        self._aud_draw()

    def _aud_click(self, ev):
        """A plain click (no drag) moves the play position."""
        if ev.inaxes is self.aud_ax and ev.dblclick and self._aud_y is not None:
            self.player.pos = int(max(0, ev.xdata) * self._aud_y[1])
            self._aud_draw_head()

    def _aud_clear_loop(self):
        self.player.loop = None
        try:
            self.aud_span.clear()
        except Exception:
            pass
        self._aud_draw()

    # ------------------------------------------------------------------ session
    def save_session(self, title="Save project"):
        """Returns True if saved. The project's folder is also where its IRs get exported."""
        if not self._has_takes() and self.irm is None:
            messagebox.showinfo(APP_NAME, "Nothing captured yet.")
            return False
        cur = getattr(self, "project_path", None)
        start = os.path.dirname(cur) if cur else self.projects_dir()
        p = filedialog.asksaveasfilename(
            title=title, defaultextension=".irproject", initialdir=start,
            initialfile=os.path.basename(cur) if cur else (safe_name(self.v_name.get()) or "IR") + ".irproject",
            filetypes=[("IR project", "*.irproject")])
        if not p:
            return False
        self._write_project(p)
        self.project_path = p
        self.status.set(f"Project saved: {p}  (its IRs export to '{self._project_ir_dir()}')")
        return True

    def _project_name(self):
        p = getattr(self, "project_path", None)
        return os.path.splitext(os.path.basename(p))[0] if p else (safe_name(self.v_name.get()) or "IR")

    def _project_ir_dir(self):
        """'<folder the project is saved in>/<Project Name> IRs'."""
        p = getattr(self, "project_path", None)
        return os.path.join(os.path.dirname(p), f"{self._project_name()} IRs") if p else None

    def _settings(self):
        return {"version": 2, "name": self.v_name.get(), "instrument": self.instrument.get(),
                "smoothing": self.v_smooth.get(), "reg_db": float(self.v_reg.get()),
                "low_cut": float(self.v_lc.get() or 0), "high_cut": float(self.v_hc.get() or 0),
                "phase": self.v_phase.get(), "blend": float(self.v_blend.get()),
                "invert": bool(self.v_inv.get()), "master_ms": self.v_master_ms.get(),
                "estimator": self.v_est.get(), "session": self.v_session.get(),
                "mix": {k: float(v.get()) for k, v in self.v_mix.items()}, "mix_link": bool(self.v_mix_link.get()), "ir_src": self.v_ir_src.get(),
                "normalize": self.v_norm.get(), "bits": self.v_bits.get(),
                "selected": [self.pedals[int(i)]["name"] for i in self.ped_tree.selection()],
                "step": int(getattr(self, "step_i", 0)),
                "master_src": self.master_src if self.master_src not in (None, "built") else None}

    def _write_project(self, path, include_demo=True):
        self._write_npz(path, self._project_dict(include_demo))

    @staticmethod
    def _write_npz(path, d):
        with open(path, "wb") as fh:
            np.savez_compressed(fh, **d)

    def _project_dict(self, include_demo=True):
        d = {"settings": np.array(json.dumps(self._settings()))}
        for s in SLOTS:
            if self.acc[s] is not None and not self.acc[s].empty():
                d.update(self.acc[s].to_dict(s))
            if self.cal[s] is not None:
                d[f"{s}_calf"], d[f"{s}_calg"] = self.cal[s]
                d[f"{s}_caln"] = np.array(self.cal_name[s].get())
        if self.irm is not None:
            d["master_h"] = self.irm["h"]
            d["master_fs"] = np.array(self.irm["fs"])
        demo = getattr(self, "demo", None) if include_demo else None
        if demo is not None:
            roles = [r for r in ROLES if r in demo]
            d["demo_meta"] = np.array(json.dumps({"fs": demo["fs"], "roles": roles,
                                                  "two": {k: bool(v) for k, v in demo["two"].items()}}))
            for r in roles:
                d[f"demo_{r}"] = (np.clip(demo[r], -1, 1) * 32767).astype(np.int16)
        return d

    def load_session(self, p=None):
        if p is None:
            p = filedialog.askopenfilename(filetypes=[("IR project", "*.irproject *.irsession"),
                                                      ("All", "*.*")])
        if not p:
            return False
        try:
            d = np.load(p, allow_pickle=False)
            st = json.loads(str(d["settings"])) if "settings" in d else {}
            ap = os.path.abspath(p)
            in_auto = any(ap.startswith(os.path.abspath(x) + os.sep) for x in (AUTOSAVE_DIR, AUTOSAVE_PREV))
            self.project_path = None if in_auto else ap
            if st.get("instrument") in E.INSTRUMENTS:
                self.instrument.set(st["instrument"])
                self._last_instrument = st["instrument"]
            for s in SLOTS:
                self.acc[s] = E.Accumulator.from_dict(d, s) if f"{s}_meta" in d else None
                self.cal[s], _ = None, self.cal_name[s].set("(none)")
                if f"{s}_calf" in d:
                    self.cal[s] = (d[f"{s}_calf"], d[f"{s}_calg"])
                    self.cal_name[s].set(str(d[f"{s}_caln"]))
            for key, var in (("name", self.v_name), ("smoothing", self.v_smooth),
                             ("reg_db", self.v_reg), ("low_cut", self.v_lc),
                             ("high_cut", self.v_hc), ("phase", self.v_phase),
                             ("blend", self.v_blend), ("invert", self.v_inv),
                             ("master_ms", self.v_master_ms), ("normalize", self.v_norm),
                             ("estimator", self.v_est), ("session", self.v_session),
                             ("bits", self.v_bits)):
                if key in st:
                    var.set(st[key])
            for k, v in (st.get("mix") or {}).items():
                if k in self.v_mix:
                    self.v_mix[k].set(v)
            if "mix_link" in st:
                self.v_mix_link.set(bool(st["mix_link"]))
            if st.get("ir_src") in ("mix", "A", "B"):
                self.v_ir_src.set(st["ir_src"])
                self._ir_src_pref = st["ir_src"]
            self._mix_changed("low" if not self.v_mix_link.get() else "mid")
            accs = [self.acc[s] for s in SLOTS if self.acc[s] is not None]
            self.demo = None
            if "demo_meta" in d:
                dm = json.loads(str(d["demo_meta"]))
                self._set_demo({r: d[f"demo_{r}"].astype(np.float32) / 32767 for r in dm["roles"]},
                               dm["fs"], dm.get("two", {}), save=False)
            elif hasattr(self, "aud_status"):
                self._refresh_audition_names()
            self.irm, self.master_src = None, None
            tone = str(st.get("master_src") or "").startswith("tone match")
            if accs:
                self.fs.set(accs[0].fs)
            if "master_h" in d and (tone or not accs):   # loaded IR WAV / tone-matched IR
                h, fs = d["master_h"], int(d["master_fs"])
                n = int(2 ** int(np.ceil(np.log2(max(65536, 2 * len(h))))))
                self.irm = {"h": h, "fs": fs, "P": np.abs(np.fft.rfft(h, n)) ** 2,
                               "freqs": np.fft.rfftfreq(n, 1 / fs), "nfft": n, "sign": 1.0,
                               "phase": "from file", "removed_predelay_ms": 0.0}
                self.master_src = st.get("master_src") or os.path.basename(p)
        except Exception as exc:
            log.exception("open project failed")
            messagebox.showerror(APP_NAME, f"Couldn't open project: {exc}")
            return False
        self.take_stack = []
        self.goto_step(int(st.get("step", 0)))
        self._update_coverage()
        names = set(st.get("selected", []))
        self._suppress_preview = True            # don't auto-run fits/graphs while opening
        try:
            self.ped_tree.selection_set([str(i) for i, pd in enumerate(self.pedals) if pd["name"] in names])
            if not self.ped_tree.selection():
                self.ped_tree.selection_set("0")
                self.ped_tree.focus("0")
            if accs and not tone:
                try:
                    self.build()
                except Exception:
                    log.exception("build during open failed")
            elif self.irm is not None:
                self._plot_master()
        finally:
            self.after(300, lambda: setattr(self, "_suppress_preview", False))
        self.status.set(f"Opened {os.path.basename(p)} - change settings, add takes, or export "
                        "for more pedals.")
        return True

    def open_ir_wav(self):
        p = filedialog.askopenfilename(title="Open an IR WAV (ideally a long, full-length one)",
                                       filetypes=[("WAV", "*.wav"), ("All", "*.*")])
        if not p:
            return
        if self._has_takes() and not messagebox.askyesno(
                APP_NAME, "Use this WAV instead of the IR built from your takes? "
                "(Your takes are kept - press Build IR to go back to them.)"):
            return
        try:
            self.irm = E.master_from_wav(p)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't read IR: {exc}")
            return
        self.master_src = os.path.basename(p)
        self.fits = {}
        for iid in self.ped_tree.get_children():
            self.ped_tree.set(iid, "err", "")
        name = os.path.splitext(os.path.basename(p))[0]
        self.v_name.set(name.split("_FULL")[0])
        self.nb.select(self.tab_build)
        self._plot_master()
        self.fit_info.configure(text=(
            f"Loaded {self.master_src}: {len(self.irm['h'])} samples @ {self.irm['fs']} Hz "
            f"({len(self.irm['h']) / self.irm['fs'] * 1000:.0f} ms). Select targets and "
            "export. Start from a full-length IR for the best results on every pedal."))

    def import_wavs(self):
        """Add a take from WAV files recorded elsewhere (e.g. Reaper)."""
        import soundfile as sf
        p = filedialog.askopenfilename(title="Piezo WAV (or a stereo WAV: piezo L, mic R)",
                                       filetypes=[("WAV", "*.wav"), ("All", "*.*")])
        if not p:
            return
        try:
            a, fs = sf.read(p, always_2d=True)
            if a.shape[1] >= 2:
                x, y = a[:, 0], a[:, 1]
            else:
                q = filedialog.askopenfilename(title="Mic WAV (same take)",
                                               filetypes=[("WAV", "*.wav"), ("All", "*.*")])
                if not q:
                    return
                b, fs2 = sf.read(q, always_2d=True)
                if fs2 != fs:
                    raise ValueError("Piezo and mic files have different sample rates.")
                x, y = a[:, 0], b[:, 0]
            if fs != self.fs.get():
                if any(self.acc[s] is not None and not self.acc[s].empty() for s in SLOTS):
                    raise ValueError(f"File is {fs} Hz but the session is {self.fs.get()} Hz.")
                self.fs.set(fs)
            two = messagebox.askyesno(APP_NAME, "Were the piezo and mic recorded on two different "
                                      "devices (e.g. a USB mic)?")
            self._process_async(np.asarray(x, float), np.asarray(y, float),
                                "import " + os.path.basename(p), two)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    # ------------------------------------------------------------------ tone match tab
    def _build_tone(self):
        t = self._scroll_body(self.tab_tone)
        self.tm = {"ref": None, "pz": None, "pairs": [], "pair_audio": [],
                   "sel": {"ref": None, "pz": None}}
        left = ttk.Frame(t, width=430)
        left.pack(side="left", fill="y", padx=10, pady=8)
        right = ttk.Frame(t)
        right.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=8)

        ttk.Label(left, wraplength=420, justify="left", foreground="#444", text=(
            "Make an IR that makes your instrument sound like a recording you love (another guitar, a "
            "record, a video...). They don't need to be recorded together.\n\n"
            "How: find a clear, sustained note in the reference (e.g. an open A ringing out). Then "
            "record yourself playing that SAME pitch (the same note name and octave, e.g. your own "
            "open A), 3-5 times, letting each ring ~2 s. That's one 'note pair'.\n"
            "Each pair only tells the app about that note's overtones, so do a few pairs with "
            "different notes (e.g. low E, A, D, G, B, high E) - more pairs = a more accurate match.")
        ).pack(fill="x")

        self.tm_info = {}
        for key, title in (("ref", "1. Reference: a note from the recording you want to sound like"),
                           ("pz", "2. You: the same note played on your instrument")):
            fr = ttk.LabelFrame(left, text=title)
            fr.pack(fill="x", pady=(8, 0))
            bf = ttk.Frame(fr)
            bf.pack(fill="x", padx=6, pady=4)
            if key == "pz":
                self.tm_rec_btn = ttk.Button(bf, text="Record 12 s from", command=self.tm_record)
                self.tm_rec_btn.pack(side="left")
                self.tm_src = tk.StringVar()
                self.tm_src_cb = ttk.Combobox(bf, textvariable=self.tm_src, state="readonly", width=20)
                self.tm_src_cb.pack(side="left", padx=4)
            if key == "pz":
                self._tip(self.tm_rec_btn, "Records 12 s from the chosen input. Play the same note as the "
                                           "reference 3-5 times, letting each ring ~2 s.")
                self._tip(self.tm_src_cb, "Which input to record: usually the Instrument (piezo / DI).")
            self._tip(ttk.Button(bf, text="Load file...", command=lambda k=key: self.tm_load(k)),
                      "Load any audio or video file: WAV, FLAC, MP3, M4A/AAC, OGG, MP4, MOV, MKV, "
                      "WEBM... (only the sound is used).").pack(side="left", padx=4)
            self._tip(ttk.Button(bf, text="Auto-select", command=lambda k=key: self.tm_auto(k)),
                      "Finds the clearest sustained note automatically. Or drag on the waveform (right) "
                      "to choose the part yourself.").pack(side="left", padx=4)
            self._tip(ttk.Button(bf, text="Play selection", command=lambda k=key: self.tm_play(k)),
                      "Listen to the selected part to check it's the right note.").pack(side="left", padx=4)
            self.tm_info[key] = ttk.Label(fr, text="(nothing loaded)", foreground="#2b6cb0",
                                          wraplength=410, justify="left")
            self.tm_info[key].pack(anchor="w", padx=6, pady=(0, 4))

        pf = ttk.LabelFrame(left, text="3. Save this pair, then repeat 1-2 with another note")
        pf.pack(fill="x", pady=(8, 0))
        pb = ttk.Frame(pf)
        pb.pack(fill="x", padx=6, pady=4)
        self._tip(ttk.Button(pb, text="Add this note pair", style="Rec.TButton", command=self.tm_add_pair),
                  "Saves the selected reference note + your note as a pair. Then pick another note in the "
                  "reference, record yourself playing it, and add that pair too. 4-6 pairs across the "
                  "range of the instrument works well.").pack(side="left")
        ttk.Button(pb, text="Remove", command=self.tm_remove_pair).pack(side="left", padx=4)
        ttk.Button(pb, text="Clear all", command=self.tm_clear_pairs).pack(side="left")
        self.tm_list = tk.Listbox(pf, height=5, font=("Consolas", 9))
        self.tm_list.pack(fill="x", padx=6, pady=(0, 6))

        bfr = ttk.LabelFrame(left, text="4. Build")
        bfr.pack(fill="x", pady=(8, 0))
        self.tm_mode = tk.StringVar(value="scratch")
        self.tm_refine_rb = ttk.Radiobutton(
            bfr, text="Start from my Capture IR, then nudge it toward the reference (best)", value="refine",
            variable=self.tm_mode)
        self.tm_refine_rb.pack(anchor="w", padx=6, pady=(4, 0))
        self._tip(self.tm_refine_rb, "Uses the IR you measured on the Capture tab (instrument + mics) as "
                  "the starting point - it already has your guitar's body detail - and only corrects the "
                  "overall tone toward the reference. Needs Capture takes, and your notes must be played "
                  "through the same pickup.")
        rb2 = ttk.Radiobutton(bfr, text="Notes only - no Capture session needed", value="scratch",
                              variable=self.tm_mode)
        rb2.pack(anchor="w", padx=6)
        self._tip(rb2, "Builds the IR from the note pairs alone. Gets the broad tone right but can't "
                  "know about narrow resonances between the notes you played. Use this for an electric "
                  "or bass, or when you haven't done a Capture session.")
        of = ttk.Frame(bfr)
        of.pack(fill="x", padx=6, pady=4)
        ttk.Label(of, text="Smoothing").pack(side="left")
        self.tm_smooth = tk.StringVar(value="1/3 oct")
        ttk.Combobox(of, values=["1/6 oct", "1/3 oct", "1/2 oct", "1 oct"], width=8,
                     textvariable=self.tm_smooth, state="readonly").pack(side="left", padx=4)
        ttk.Label(of, text="  Max change +/- dB").pack(side="left")
        self.tm_max = tk.IntVar(value=24)
        self._tip(ttk.Spinbox(of, from_=3, to=36, width=4, textvariable=self.tm_max),
                  "The most the matched IR may boost or cut at any frequency. Lower = gentler, safer.").pack(
            side="left", padx=4)
        ttk.Button(bfr, text="Build matched IR", style="Rec.TButton",
                   command=self.tm_build).pack(fill="x", padx=6, pady=(2, 6))

        af = ttk.LabelFrame(left, text="Listen")
        af.pack(fill="x", pady=(8, 0))
        ttk.Button(af, text="Reference", command=lambda: self.tm_play("ref")).pack(side="left", padx=4, pady=6)
        ttk.Button(af, text="Mine dry", command=lambda: self.tm_play("pz")).pack(side="left", padx=4)
        ttk.Button(af, text="Mine + matched IR", command=lambda: self.tm_play("pz", True)).pack(side="left", padx=4)
        ttk.Button(af, text="Stop", command=lambda: sd and sd.stop()).pack(side="left", padx=4)
        self.tm_msg = ttk.Label(left, wraplength=420, justify="left")
        self.tm_msg.pack(fill="x", pady=6)

        self.tm_fig = Figure(figsize=(7, 4.6 if self.compact else 6), dpi=100)
        self.tm_ax = {"ref": self.tm_fig.add_subplot(311), "pz": self.tm_fig.add_subplot(312)}
        self.tm_axc = self.tm_fig.add_subplot(313)
        self.tm_canvas = FigureCanvasTkAgg(self.tm_fig, master=right)
        self.tm_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.tm_span = {}
        for key in ("ref", "pz"):
            self.tm_span[key] = SpanSelector(
                self.tm_ax[key], lambda a, b, k=key: self._tm_on_select(k, a, b), "horizontal",
                useblit=True, interactive=True, drag_from_anywhere=True,
                props=dict(alpha=0.25, facecolor="#2b6cb0"))
        self._tm_draw()
        self._inputs_changed()
        self.tm_src.set(self.mic_name("piezo"))

    # -- audio in
    def _tm_set(self, key, x, fs, name):
        x = np.asarray(x, float)
        if x.ndim > 1:
            x = x.mean(axis=1)
        self.tm[key] = {"x": x, "fs": int(fs), "name": name}
        self.tm_auto(key)

    def tm_load(self, key):
        import soundfile as sf
        p = filedialog.askopenfilename(
            title="Reference recording" if key == "ref" else "Your instrument's recording",
            filetypes=[("Audio / video", " ".join("*." + e for e in MEDIA_EXT)), ("All files", "*.*")])
        if not p:
            return
        try:
            self.status.set("Loading...")
            self.update_idletasks()
            a, fs = load_any_audio(p)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't read that file: {exc}")
            return
        finally:
            self.status.set("Ready.")
        self._tm_set(key, a, fs, os.path.basename(p))

    def tm_record(self):
        if sd is None:
            return
        if self.rig.recording:
            messagebox.showinfo(APP_NAME, "Already recording.")
            return
        started_here = False
        names = {self.mic_name(r): r for r in ROLES}
        role = names.get(self.tm_src.get(), "piezo")
        if self.rig.running() and role not in self.rig.roles:
            self.stop_audio()
        if not self.rig.running():
            sel = self._sel(role)
            if sel is None:
                messagebox.showinfo(APP_NAME, "Choose that input on the Setup tab first.")
                return
            try:
                self.rig.start({role: (sel[0], sel[1])}, int(self.fs.get()))
                started_here = True
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Couldn't open the piezo input: {exc}")
                return
        self.rig.begin()
        self.tm_rec_btn.configure(state="disabled")
        t_end = time.time() + 12

        def tick():
            left = t_end - time.time()
            if left > 0:
                self.tm_info["pz"].configure(text=f"RECORDING - play the same note 3-5 times...  "
                                                  f"{left:4.0f} s")
                self.after(200, tick)
                return
            data = self.rig.end()
            if started_here:
                self.rig.stop()
            self.tm_rec_btn.configure(state="normal")
            x = data.get(role, np.zeros(0))
            if len(x) < self.fs.get():
                self.tm_info["pz"].configure(text="Recording failed - check that input on the Setup tab.")
                return
            self._tm_set("pz", x, self.fs.get(), f"{self.mic_name(role)} recording")
        tick()

    # -- selection
    def _tm_on_select(self, key, a, b):
        d = self.tm[key]
        if d is None:
            return
        i0, i1 = sorted((int(a * d["fs"]), int(b * d["fs"])))
        i0, i1 = max(0, i0), min(len(d["x"]), i1)
        if i1 - i0 < int(0.3 * d["fs"]):
            return
        self.tm["sel"][key] = (i0, i1)
        self._tm_analyse(key)

    def tm_auto(self, key):
        d = self.tm[key]
        if d is None:
            return
        self.tm["sel"][key] = TM.auto_select(d["x"], d["fs"])
        self._tm_draw()
        i0, i1 = self.tm["sel"][key]
        try:
            self.tm_span[key].extents = (i0 / d["fs"], i1 / d["fs"])
        except Exception:
            pass
        self._tm_analyse(key)

    def _tm_analyse(self, key):
        d = self.tm[key]
        i0, i1 = self.tm["sel"][key]
        try:
            h = TM.analyse(d["x"][i0:i1], d["fs"])
            d["h"] = h
            txt = (f"{d['name']}:  {TM.note_name(h['f0'])},  {len(h['freqs'])} harmonics up to "
                   f"{h['freqs'][-1] / 1000:.1f} kHz,  {h['plucks']} pluck(s) in selection "
                   f"({(i1 - i0) / d['fs']:.1f} s)")
            if h["plucks"] < 3:
                txt += "  - more plucks of the note = more accurate"
        except Exception as exc:
            d["h"] = None
            txt = f"{d['name']}: {exc}"
        self.tm_info[key].configure(text=txt)
        o = self.tm["pz" if key == "ref" else "ref"]
        if d.get("h") and o and o.get("h") and abs(d["h"]["f0"] / o["h"]["f0"] - 1) > 0.03:
            self.tm_msg.configure(text="Note: the reference and piezo notes are different pitches. "
                                  "Same-note pairs are much more accurate.", foreground="#a33")
        else:
            self.tm_msg.configure(text="", foreground="")

    # -- pairs
    def tm_add_pair(self):
        r, p = self.tm["ref"], self.tm["pz"]
        if not (r and r.get("h") and p and p.get("h")):
            messagebox.showinfo(APP_NAME, "Load/record both a reference and a piezo note first "
                                "(each needs a detected pitch).")
            return
        rows, same = TM.pair_points(r["h"], p["h"])
        if len(rows) < 3:
            messagebox.showinfo(APP_NAME, "Too few matching harmonics in this pair.")
            return
        self.tm["pairs"].append((r["h"], p["h"]))
        i0, i1 = self.tm["sel"]["pz"]
        self.tm.setdefault("pair_audio", []).append((p["x"][i0:i1].copy(), p["fs"]))
        nm = TM.note_name(r["h"]["f0"]).split(" ")[0]
        self.tm_list.insert("end", f"{nm:<4} {len(rows):3d} pts  ref {r['h']['plucks']}x / piezo "
                                   f"{p['h']['plucks']}x" + ("" if same else "  (different notes!)"))
        self.tm_msg.configure(text=f"{len(self.tm['pairs'])} note pair(s). Add more notes, then "
                              "Build matched IR.", foreground="")

    def tm_remove_pair(self):
        for i in sorted(self.tm_list.curselection(), reverse=True):
            self.tm_list.delete(i)
            self.tm["pairs"].pop(i)
            self.tm["pair_audio"].pop(i)

    def tm_clear_pairs(self):
        self.tm_list.delete(0, "end")
        self.tm["pairs"] = []
        self.tm["pair_audio"] = []

    # -- build
    def _measured_master(self):
        a, b = self.acc["A"], self.acc["B"]
        has_a = a is not None and not a.empty()
        has_b = b is not None and not b.empty()
        if not (has_a or has_b):
            return None
        p = self.params()
        p.blend_b = 0.0 if not has_b else (1.0 if not has_a else p.blend_b)
        acc0 = a if has_a else b
        p.master_ms = min(p.master_ms, int(0.75 * acc0.nfft / acc0.fs * 1000))
        return E.build_master(a, b, p, self.cal["A"], self.cal["B"])

    def tm_build(self):
        if not self.tm["pairs"]:
            messagebox.showinfo(APP_NAME, "Add at least one note pair first (3-6 is better).")
            return
        base = None
        if self.tm_mode.get() == "refine":
            try:
                base = self._measured_master()
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
            if base is None:
                messagebox.showinfo(APP_NAME, "Refine needs a measured IR: capture some takes with "
                                    "the piezo and mic first (Capture tab), or use 'From scratch'.")
                return
            # piezo side is compared *through* the measured IR (re-analysed per pair)
            pairs = self._tm_pairs_through(base)
            if pairs is None:
                return
        else:
            pairs = self.tm["pairs"]
        sm = {"1/6 oct": 1 / 6, "1/3 oct": 1 / 3, "1/2 oct": 1 / 2, "1 oct": 1.0}[self.tm_smooth.get()]
        p = self.params()
        try:
            m = TM.build_matched(pairs, fs_out=int(base["fs"] if base else self.fs.get()),
                                 smooth_oct=sm, max_db=float(self.tm_max.get()),
                                 master_ms=float(self.v_master_ms.get() or 500),
                                 low_cut=p.low_cut, high_cut=p.high_cut, base_master=base)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.irm = m
        n = len(self.tm["pairs"])
        self.master_src = f"tone match ({n} note{'s' if n > 1 else ''}, " \
                          f"{'refined measured IR' if base else 'from scratch'})"
        self.fits = {}
        for iid in self.ped_tree.get_children():
            self.ped_tree.set(iid, "err", "")
        self._tm_draw()
        self._plot_master()
        self.fit_info.configure(text=f"Using the {self.master_src}. Select targets and export.")
        self.tm_msg.configure(foreground="", text=(
            f"Built from {len(m['match']['points'])} harmonic points. Listen, then export on "
            "Build & Export (pressing Build IR there goes back to the measured IR)."))

    def _tm_pairs_through(self, base):
        """For refine mode the piezo notes are compared after passing through the
        measured IR, so only the remaining difference gets corrected."""
        if not self.tm.get("pair_audio") or len(self.tm["pair_audio"]) != len(self.tm["pairs"]):
            messagebox.showinfo(APP_NAME, "Refine needs the piezo audio of each pair - please "
                                "clear the pairs and add them again.")
            return None
        out = []
        for (rh, _), (x, fs) in zip(self.tm["pairs"], self.tm["pair_audio"]):
            out.append((rh, TM.analyse(TM.through_ir(x, fs, base), fs)))
        return out

    # -- listen
    def tm_play(self, key, through=False):
        if sd is None:
            return
        d = self.tm[key]
        if d is None:
            return
        i0, i1 = self.tm["sel"][key] or (0, len(d["x"]))
        y, fs = d["x"][i0:i1], d["fs"]
        if through:
            if self.irm is None:
                messagebox.showinfo(APP_NAME, "Build the matched IR first.")
                return
            y = TM.through_ir(y, fs, self.irm)
        rms = np.sqrt(np.mean(y ** 2)) + 1e-12
        y = y * (0.08 / rms)
        y = np.clip(y, -0.95, 0.95)
        try:
            sd.play(y.astype(np.float32), fs, device=dev_index(self.out_dev.get()))
        except Exception:
            try:
                sd.play(y.astype(np.float32), fs)
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Playback failed: {exc}")

    # -- plot
    def _tm_draw(self):
        for key, title in (("ref", "Reference - drag to select the note(s)"),
                           ("pz", "Piezo - drag to select the same note")):
            ax = self.tm_ax[key]
            ax.clear()
            d = self.tm[key]
            ax.set_title(title, fontsize=9, loc="left")
            ax.set_yticks([])
            if d is None:
                ax.text(0.5, 0.5, "load or record a note", ha="center", va="center",
                        transform=ax.transAxes, color="#888")
                ax.set_xticks([])
                continue
            x, fs = d["x"], d["fs"]
            nb = 1500
            step = max(1, len(x) // nb)
            m = len(x) // step
            env = np.abs(x[:m * step]).reshape(m, step).max(axis=1)
            tt = np.arange(m) * step / fs
            ax.fill_between(tt, -env, env, color="#555", lw=0)
            ax.set_xlim(0, len(x) / fs)
            ax.tick_params(labelsize=7)
            if self.tm["sel"][key]:
                i0, i1 = self.tm["sel"][key]
                ax.axvspan(i0 / fs, i1 / fs, color="#2b6cb0", alpha=0.18)
        ax = self.tm_axc
        ax.clear()
        m = self.irm.get("match") if self.irm else None
        if m:
            pts = m["points"]
            ax.scatter(pts[:, 0], np.clip(m["points_db"], -30, 30),
                       s=6, color="#9aa0a6", label="harmonic points")
            ax.semilogx(m["grid"], m["curve"], color="#c0392b", lw=1.8, label="correction")
            ax.set_xlim(40, 16000)
            ax.set_ylim(-30, 30)
            ax.grid(True, which="both", alpha=0.25)
            ax.legend(fontsize=7, loc="lower left")
            ax.set_title("Correction " + ("on top of your measured IR" if m["refine"] else
                                          "applied to the piezo") + " (dB)", fontsize=9, loc="left")
            ax.tick_params(labelsize=7)
        else:
            ax.text(0.5, 0.5, "Correction curve appears here after Build", ha="center", va="center",
                    transform=ax.transAxes, color="#888")
            ax.set_xticks([])
            ax.set_yticks([])
        self.tm_fig.tight_layout()
        self.tm_canvas.draw_idle()

    # ------------------------------------------------------------------ autosave / recovery
    def _state_path(self, folder=AUTOSAVE_DIR):
        return os.path.join(folder, "state.json")

    def _read_state(self, folder=AUTOSAVE_DIR):
        try:
            with open(self._state_path(folder)) as fh:
                return json.load(fh)
        except Exception:
            return None

    def _write_state(self, **kw):
        st = self._read_state() or {}
        st.update(kw)
        try:
            tmp = self._state_path() + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(st, fh, indent=2)
            os.replace(tmp, self._state_path())
        except Exception:
            log.exception("state write failed")

    def _begin_autosave_session(self):
        """First take of a new session: keep the old autosave as 'previous'."""
        if self.autosave_live:
            return
        try:
            if os.path.isdir(AUTOSAVE_DIR) and os.listdir(AUTOSAVE_DIR):
                if os.path.isdir(AUTOSAVE_PREV):
                    shutil.rmtree(AUTOSAVE_PREV, ignore_errors=True)
                os.replace(AUTOSAVE_DIR, AUTOSAVE_PREV)
            os.makedirs(os.path.join(AUTOSAVE_DIR, "takes"), exist_ok=True)
        except Exception:
            log.exception("autosave folder setup failed")
            os.makedirs(os.path.join(AUTOSAVE_DIR, "takes"), exist_ok=True)
        self.autosave_live = True
        self.take_counter = 0
        self._write_state(started=time.strftime("%Y-%m-%d %H:%M"), clean=False, takes=0,
                          instrument=self.instrument.get(), fs=int(self.fs.get()))
        log.info("autosave session started in %s", AUTOSAVE_DIR)

    def autosave(self, wait=False):
        """Save the session. The data is gathered here; compressing + writing happens on a
        background thread so the window never freezes (the demo is kept in demo.flac, not
        in the autosave project, for the same reason)."""
        if not self.autosave_live:
            return
        try:
            d = self._project_dict(include_demo=False)
            n = max((len(self.acc[s].takes) for s in SLOTS if self.acc[s] is not None), default=0)
        except Exception:
            log.exception("autosave failed")
            return
        if not hasattr(self, "_save_lock"):
            self._save_lock = threading.Lock()

        def work():
            with self._save_lock:
                try:
                    p = os.path.join(AUTOSAVE_DIR, "session.irproject")
                    tmp = p + ".tmp"
                    self._write_npz(tmp, d)
                    os.replace(tmp, p)
                except Exception:
                    log.exception("autosave failed")
        t = threading.Thread(target=work, daemon=True)
        t.start()
        if wait:
            t.join(timeout=60)
        self._write_state(updated=time.strftime("%Y-%m-%d %H:%M:%S"), takes=n, clean=False)

    def _save_raw_take(self, idx, label, x, mics, fs, extras=None):
        """Keep every recording (FLAC, lossless) so a session can be rebuilt from audio.
        Channel order: instrument, the mics in 'mics', then 'extras' (e.g. internal mic)."""
        extras = extras or {}
        try:
            import soundfile as sf
            chans = ([np.asarray(x, np.float32)] + [np.asarray(y, np.float32) for y, _ in mics.values()] +
                     [np.asarray(v, np.float32) for v in extras.values()])
            n = max(len(c) for c in chans)
            a = np.zeros((n, len(chans)), np.float32)
            for k, c in enumerate(chans):
                a[:len(c), k] = c
            base = os.path.join(AUTOSAVE_DIR, "takes", f"take_{idx:03d}")
            sf.write(base + ".flac", np.clip(a, -1, 1), fs, subtype="PCM_24")
            meta = {"label": label, "fs": fs, "mics": list(mics.keys()), "extras": list(extras.keys()),
                    "two_device": {s: bool(t) for s, (_, t) in mics.items()},
                    "lengths": [len(c) for c in chans], "time": time.strftime("%H:%M:%S")}
            with open(base + ".json", "w") as fh:
                json.dump(meta, fh)
        except Exception:
            log.exception("saving raw take failed")

    def _offer_restore(self):
        st = self._read_state()
        proj = os.path.join(AUTOSAVE_DIR, "session.irproject")
        if not st or not st.get("takes") or not os.path.exists(proj):
            return
        crashed = not st.get("clean", True)
        msg = ((f"{APP_NAME} didn't close properly last time.\n\n" if crashed else "") +
               f"Reopen your last session?\n\n{st.get('takes')} take(s), {st.get('instrument', '')}, "
               f"last saved {st.get('updated', st.get('started', '?'))}.\n\n"
               "Yes = carry on where you left off.\nNo = start a new session (the old one stays "
               "available under File > Open the session before that).")
        if messagebox.askyesno(APP_NAME, msg):
            self._reopen_autosave()

    def _reopen_autosave(self):
        if self.load_session(os.path.join(AUTOSAVE_DIR, "session.irproject")):
            if self.demo is None:
                self._load_demo(AUTOSAVE_DIR)
            self.autosave_live = True
            self.take_counter = len([f for f in os.listdir(os.path.join(AUTOSAVE_DIR, "takes"))
                                     if f.endswith(".json")]) if os.path.isdir(
                os.path.join(AUTOSAVE_DIR, "takes")) else 0
            self._write_state(clean=False)
            n = max((len(self.acc[s].takes) for s in SLOTS if self.acc[s] is not None), default=0)
            self.logln(f"Reopened the last session - carry on from step {min(self.step_i + 1, len(self.steps()))}. "
                       "New takes are added to it.")
            self.nb.select(self.tab_cap)

    def reopen_last(self):
        if not os.path.exists(os.path.join(AUTOSAVE_DIR, "session.irproject")):
            messagebox.showinfo(APP_NAME, "There's no autosaved session yet.")
            return
        if self.autosave_live:
            messagebox.showinfo(APP_NAME, "You're already in the last session - it's being "
                                "autosaved as you go.")
            return
        self._reopen_autosave()

    def reopen_previous(self):
        p = os.path.join(AUTOSAVE_PREV, "session.irproject")
        if not os.path.exists(p):
            messagebox.showinfo(APP_NAME, "There's no earlier session saved.")
            return
        if self.load_session(p):
            messagebox.showinfo(APP_NAME, "Opened the earlier session. Use File > Save project "
                                "to keep it - new takes start a fresh autosave.")

    def recover_from_recordings(self):
        folder = os.path.join(AUTOSAVE_DIR, "takes")
        metas = sorted(f for f in os.listdir(folder) if f.endswith(".json")) if os.path.isdir(folder) else []
        if not metas:
            messagebox.showinfo(APP_NAME, "No saved recordings found for the last session.")
            return
        if self.busy:
            messagebox.showinfo(APP_NAME, "Busy - try again in a moment.")
            return
        if not messagebox.askyesno(APP_NAME, f"Re-analyse all {len(metas)} saved recording(s) of the "
                                   "last session from scratch? This replaces the takes currently "
                                   "loaded."):
            return
        import soundfile as sf
        self.acc = {s: None for s in SLOTS}
        self.take_stack = []
        self.autosave_live = True
        self.busy = True
        self.status.set("Rebuilding from recordings...")
        items = []
        for m in metas:
            with open(os.path.join(folder, m)) as fh:
                meta = json.load(fh)
            if meta.get("fs") != int(self.fs.get()):
                self.fs.set(meta["fs"])
            items.append((m[:-5], meta))
        for _, meta in items:
            for s in meta["mics"]:
                self._get_acc(s)
        accs = dict(self.acc)

        def work():
            n = 0
            for base, meta in items:
                try:
                    a, fs = sf.read(os.path.join(folder, base + ".flac"), dtype="float32", always_2d=True)
                    L = meta["lengths"]
                    x = a[:L[0], 0].astype(np.float64)
                    ex = meta.get("extras", [])
                    x2 = None
                    if "piezo2" in ex:
                        c = 1 + len(meta["mics"]) + ex.index("piezo2")
                        x2 = a[:L[c], c].astype(np.float64)
                    done = []
                    for k, s in enumerate(meta["mics"]):
                        y = a[:L[k + 1], k + 1].astype(np.float64)
                        rep = accs[s].process_take(x, y, meta["label"], two_device=meta["two_device"][s],
                                                   x2=x2)
                        if rep.get("accepted"):
                            done.append(s)
                        self.q.put(("take", s, rep, False))
                    self.q.put(("take_done", done, False, "recover"))
                    n += 1
                except Exception as exc:
                    log.exception("recover %s", base)
                    self.q.put(("error", f"[{base}] {exc}", ""))
            self.q.put(("recovered", n))
        threading.Thread(target=work, daemon=True).start()

    def _open_folder(self, path):
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)            # Windows
        except Exception:
            messagebox.showinfo(APP_NAME, path)

    def report_callback_exception(self, exc, val, tb):
        """Any error in a button/handler: log it, tell the user, keep running."""
        log.error("Error in UI callback:\n%s", "".join(traceback.format_exception(exc, val, tb)))
        try:
            self.autosave()
        except Exception:
            pass
        if time.time() - self._err_shown > 2:
            self._err_shown = time.time()
            messagebox.showerror(APP_NAME, f"Something went wrong: {val}\n\nYour session is "
                                 "autosaved and the app is still running. Details were written to "
                                 "the log (Help > Open logs folder) - send me app.log.")

    def show_tips(self):
        messagebox.showinfo(APP_NAME, (
            "Getting the most accurate IR\n\n"
            "- Quiet room, fans/AC off. Fresh-ish strings.\n"
            "- Mic 20-40 cm from the guitar, aimed between the 12th fret and the soundhole "
            "(the classic spot), or wherever it sounds best to your ears.\n"
            "- Nothing moves during a session: mic stand, guitar, you (sit, and keep the "
            "guitar in the same place on your leg).\n"
            "- Levels: hardest strum peaking around -6 dBFS on both channels.\n"
            "- Do the whole guided session, then free takes until the bands you care about are green.\n"
            "- Reference Mic: if it's a measurement mic, load its calibration file. A Color Mic "
            "(any mic you like the sound of) goes right next to it; mix the two by frequency on the "
            "Build tab.\n"
            "- Record a short demo (Capture tab) so you can audition every option on the Build tab.\n"
            "- Save the session (File menu) - you can come back and add takes later."))

    # ------------------------------------------------------------------ timer loop
    def _tick(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "take":
                    self._report_take(msg[1], msg[2], msg[3])
                elif msg[0] == "take_done" and len(msg) > 3:     # during a rebuild
                    if msg[1]:
                        self.take_stack.append(msg[1])
                elif msg[0] == "take_done":
                    self.busy = False
                    self.status.set("Ready - session autosaved.")
                    if msg[1]:
                        self.take_stack.append(msg[1])
                        if msg[2]:
                            self.goto_step(self.step_i + 1)
                    self.autosave()
                elif msg[0] == "recovered":
                    self.busy = False
                    self.irm = None
                    self._update_coverage()
                    self.autosave()
                    self.status.set(f"Rebuilt the session from {msg[1]} recording(s).")
                    messagebox.showinfo(APP_NAME, f"Rebuilt the session from {msg[1]} saved "
                                        "recording(s). Go to Build & Export when ready.")
                elif msg[0] == "error":
                    self.logln("ERROR " + msg[1])
                    self.status.set("Take failed - see log.")
        except queue.Empty:
            pass
        if self.rig.running():
            pk = self.rig.read_peaks()
            on_cap = self.nb.select() == str(self.tab_cap)
            if on_cap and time.time() - self._spec_t > 0.2:
                self._spec_t = time.time()
                self._spec_update()
            for role in (ROLES if on_cap else ()):
                c, lab = self.meter[role]
                if role not in pk:
                    continue
                self._draw_meter(c, lab, pk[role], self.rig.hold.get(role, 0.0))
        if self.rec_started is not None:
            el = time.time() - self.rec_started
            if self.rec_limit:
                left = self.rec_limit - el
                self.rec_time.configure(text=f"REC  {self.rec_label}   {max(left, 0):4.0f} s left")
                if left <= 0:
                    self.stop_record()
            else:
                self.rec_time.configure(text=f"REC  {self.rec_label}   {int(el // 60)}:{int(el % 60):02d}"
                                             "   - press Stop when done")
                if el > 30 * 60:          # safety stop after 30 minutes
                    self.stop_record()
        self._after_id = self.after(60, self._tick)

    SPEC_COL = {"piezo": "#2e7d32", "piezo2": "#7cb342", "A": "#1565c0", "B": "#ef6c00", "I": "#8e24aa"}

    def _spec_init(self):
        ax = self.spec_ax
        ax.clear()
        self._spec_f = np.geomspace(40, 16000, 160)
        ax.set_xscale("log")
        ax.set_xlim(40, 16000)
        ax.set_ylim(-60, 6)
        ticks = [50, 100, 200, 500, 1000, 2000, 5000, 10000]
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t_ / 1000:g}k" if t_ >= 1000 else str(t_) for t_ in ticks])
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.tick_params(labelsize=6, pad=1)
        ax.grid(True, which="both", alpha=0.2)
        ax.set_title("Live spectrum (Start audio, then play)", fontsize=7, pad=2)
        self._spec_lines = {}
        for r in ROLES:
            (ln,) = ax.semilogx(self._spec_f, np.full(len(self._spec_f), np.nan), color=self.SPEC_COL[r],
                                lw=1.2, label=self._short_name(r) if hasattr(self, "inp_label") else r)
            self._spec_lines[r] = ln
        self.spec_fig.subplots_adjust(left=0.09, right=0.99, top=0.88, bottom=0.14)
        self.spec_canvas.draw_idle()

    def _spec_update(self):
        try:
            n = 8192
            fs = int(self.fs.get())
            win = np.hanning(n)
            ff = np.fft.rfftfreq(n, 1 / fs)
            ref = (ff > 200) & (ff < 5000)
            lo = self._spec_f * 2 ** (-1 / 12)
            hi = self._spec_f * 2 ** (1 / 12)
            edges_lo = np.searchsorted(ff, lo)
            edges_hi = np.maximum(np.searchsorted(ff, hi), edges_lo + 1)
            ymax = -200.0
            shown = []
            for r, ln in self._spec_lines.items():
                x = self.rig.recent(r, n)
                if x is None or not np.any(x):
                    ln.set_ydata(np.full(len(self._spec_f), np.nan))
                    continue
                P = np.abs(np.fft.rfft(x * win)) ** 2 / (np.sum(win ** 2) * n / 2)
                c = np.concatenate([[0], np.cumsum(P)])
                band = (c[edges_hi] - c[edges_lo]) / (edges_hi - edges_lo)
                if self.v_spec_match.get():
                    band = band / max(np.mean(P[ref]), 1e-20)
                prev = self._spec_avg.get(r)
                band = band if prev is None or len(prev) != len(band) else 0.6 * prev + 0.4 * band
                self._spec_avg[r] = band
                y = 10 * np.log10(np.maximum(band, 1e-14))
                ln.set_ydata(y)
                ln.set_label(self._short_name(r))
                ymax = max(ymax, float(np.max(y)))
                shown.append(ln)
            ax = self.spec_ax
            if shown:
                top = ymax + 4 if self.v_spec_match.get() else 3
                ax.set_ylim(top - 60, top)
                ax.legend(handles=shown, fontsize=6, loc="lower left", ncol=3, frameon=False)
                ax.set_title("Live spectrum" + (" - level-matched" if self.v_spec_match.get() else " (dBFS)"),
                             fontsize=7, pad=2)
            self.spec_canvas.draw_idle()
        except Exception:
            log.debug("spectrum", exc_info=True)

    def _draw_meter(self, c, lab, v, hold_lin):
        d = 20 * np.log10(v + 1e-9)
        hold = 20 * np.log10(hold_lin + 1e-9)
        w = max(c.winfo_width(), 10)
        H = 22
        c.delete("all")
        frac = np.clip((d + 60) / 60, 0, 1)
        col = "#2e9d5b" if d < -12 else ("#d6a419" if d < -3 else "#e03131")
        c.create_rectangle(0, 0, w * frac, H, fill=col, width=0)
        for m in (-48, -36, -24, -12, -6, -3):
            xm = w * (m + 60) / 60
            c.create_line(xm, H - 5, xm, H, fill="#777")
        if hold > -110:
            hot = hold > -3.0
            clip = hold >= -0.3
            mc = "#ff3b3b" if hot else "#ffffff"
            hx = w * np.clip((hold + 60) / 60, 0, 1)
            c.create_line(hx, 0, hx, H, fill=mc, width=3 if hot else 2)
            txt = "CLIP" if clip else f"{hold:.1f}"
            if hx > 46:
                c.create_text(hx - 4, H / 2, text=txt, anchor="e", fill=mc,
                              font=("Segoe UI", 9, "bold"))
            else:
                c.create_text(hx + 4, H / 2, text=txt, anchor="w", fill=mc,
                              font=("Segoe UI", 9, "bold"))
        lab.configure(text=(f"{d:6.1f} dBFS" if d > -110 else "  -inf dBFS") +
                      (f"   peak {hold:5.1f}" if hold > -110 else ""),
                      fg="#c62828" if hold > -3 else "#222")

    def _quit(self):
        if self.rig.recording:
            if not messagebox.askyesno(APP_NAME, "You're recording - stop and keep this take before "
                                       "closing?"):
                pass
            else:
                self.stop_record()
                t0 = time.time()
                while self.busy and time.time() - t0 < 60:
                    self.update()
                    time.sleep(0.05)
        try:
            self.autosave(wait=True)
            if self.autosave_live:
                self._write_state(clean=True)
            log.info("clean exit")
        except Exception:
            pass
        try:
            self.after_cancel(self._after_id)
        except Exception:
            pass
        try:
            if self._aud_tick_job is not None:
                self.after_cancel(self._aud_tick_job)
            self.player.stop()
        except Exception:
            pass
        try:
            self.rig.stop()
            if sd:
                sd.stop()
        finally:
            self.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    setup_logging()
    try:
        app = App()
    except Exception:
        log.exception("startup failed")
        raise
    if "--selftest" in sys.argv:
        # used by the automatic build: open every tab, run the engine once, close
        try:
            for tab in (app.tab_setup, app.tab_cap, app.tab_build, app.tab_tone):
                app.nb.select(tab)
                app.update()
            fs = 48000
            rng = np.random.default_rng(0)
            x = rng.standard_normal(10 * fs) * 0.05
            y = np.convolve(x, np.r_[np.zeros(60), 1.0, -0.4, 0.2])[:len(x)]
            acc = E.Accumulator(fs)
            acc.process_take(x, y, "selftest")
            m = E.build_master(acc, None, E.BuildParams())
            E.fit_for_pedal(m, 44100, 1024)
            assert app.ped_tree._root() is app
            log.info("selftest OK")
            app.destroy()
            sys.exit(0)
        except SystemExit:
            raise
        except Exception:
            log.exception("selftest failed")
            sys.exit(1)
    app.after(500, app._offer_restore)
    app.mainloop()


if __name__ == "__main__":
    main()
