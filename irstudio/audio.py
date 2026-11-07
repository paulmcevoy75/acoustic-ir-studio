# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Audio I/O: the recording rig (inputs, meters, headphone monitor) and the audition player."""

from __future__ import annotations

import threading

import numpy as np

from .config import log


try:
    import sounddevice as sd
except Exception as exc:          # pragma: no cover - shown in UI
    log.debug("module: ignored error", exc_info=True)
    sd = None
    SD_ERROR = str(exc)
else:
    SD_ERROR = ""


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
                log.debug("start: ignored error", exc_info=True)
            kw = dict(device=dev, channels=nch, samplerate=self.fs, dtype="float32",
                      blocksize=2048, latency="high", callback=self._cb)
            try:
                st = sd.OutputStream(**kw)
            except Exception as first:
                try:                          # let Windows convert the rate (WASAPI shared mode)
                    st = sd.OutputStream(extra_settings=sd.WasapiSettings(auto_convert=True), **kw)
                except Exception:
                    raise first from None
            st.start()
            self.stream, self.stream_key = st, key
            try:
                self.dev_name = sd.query_devices(dev)["name"] if dev is not None else "default output"
            except Exception:
                log.debug("start: ignored error", exc_info=True)
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
                log.debug("_close: ignored error", exc_info=True)


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
        self.mon_gain = {r: 1.0 for r in ("piezo", "piezo2", "A", "B", "I", "PO")}
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
            log.debug("start_monitor: ignored error", exc_info=True)
        kw = dict(device=out_dev, channels=nch, samplerate=fs, dtype="float32",
                  blocksize=256, callback=self._out_cb)
        try:
            st = sd.OutputStream(**kw)
        except Exception:
            log.debug("start_monitor: ignored error", exc_info=True)
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
                log.debug("stop_monitor: ignored error", exc_info=True)
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
                log.debug("stop: ignored error", exc_info=True)
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


def dev_index(label):
    try:
        return int(label.split(":", 1)[0])
    except (ValueError, AttributeError):
        return None
