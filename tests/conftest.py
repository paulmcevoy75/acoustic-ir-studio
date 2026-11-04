# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""Shared test helpers: synthetic guitar/mic signals, a fake sound card, and an app fixture."""

from __future__ import annotations

import os
import threading
import time

import numpy as np
import pytest
from scipy import signal

FS = 48000


# ---------------------------------------------------------------------------- signals
def make_body(seed=1, delay_ms=1.2, scale=0.1):
    """A 'true' body + mic response: resonances, a direct sound and a gentle top roll-off."""
    rng = np.random.default_rng(seed)
    n = int(0.3 * FS)
    t = np.arange(n) / FS
    h = np.zeros(n)
    for f0, dec, amp in [(98, 40, 1.0), (195, 30, .7), (230, 35, .5), (410, 25, .4),
                         (620, 30, .3), (1100, 50, .2), (2400, 80, .15)]:
        h += amp * np.exp(-dec * t) * np.sin(2 * np.pi * f0 * t)
    h += 0.3 * rng.standard_normal(n) * np.exp(-60 * t)
    h[int(delay_ms * 1e-3 * FS)] += 2.0
    return signal.lfilter(*signal.butter(1, 6000, "low", fs=FS), h) * scale


def pluck(f0, dur, amp, rng):
    """Karplus-Strong string pluck."""
    n = int(dur * FS)
    period = int(FS / f0)
    buf = rng.uniform(-1, 1, period)
    out = np.zeros(n)
    for i in range(n):
        out[i] = buf[i % period]
        buf[i % period] = 0.996 * 0.5 * (buf[i % period] + buf[(i + 1) % period])
    return amp * out


def playing(notes, dur, rng):
    """A take: random notes from the list, overlapping, at random levels."""
    x = np.zeros(int(dur * FS))
    pos = 0
    while pos < len(x) - FS:
        p = pluck(rng.choice(notes), 1.5, rng.uniform(.05, .25), rng)
        k = min(len(p), len(x) - pos)
        x[pos:pos + k] += p[:k]
        pos += int(rng.uniform(.3, 1.0) * FS)
    return x


SESSIONS = [
    [82, 110, 147, 196, 247, 330],          # chord tones
    [82, 87, 98, 110, 123],                  # low notes
    [330, 392, 440, 523, 659, 784, 988],     # high notes
    [1319, 1568, 1976, 2637],                # harmonics
]


@pytest.fixture(scope="session")
def rng():
    return np.random.default_rng(1)


# ---------------------------------------------------------------------------- fake sound card
class FakeSoundDevice:
    """Stands in for the sounddevice module: two input devices (a 4-input interface and a
    2-input USB mic), one output. Data queued with feed() is delivered while recording."""

    def __init__(self):
        self.devices = [
            dict(name="Microphone (Test Interface 4x4)", hostapi=0, max_input_channels=4, max_output_channels=0),
            dict(name="Speakers (Test Interface 4x4)", hostapi=0, max_input_channels=0, max_output_channels=2),
            dict(name="Microphone (USB Measurement Mic)", hostapi=0, max_input_channels=2, max_output_channels=0),
        ]
        self.pending = {}            # device index -> array (frames, channels) to deliver
        self.sent = 0
        self.is_recording = lambda: False
        self.opened_outputs = []
        fake = self

        class InputStream:
            def __init__(self, device, channels, samplerate, dtype, blocksize, callback, **kw):
                self.dev, self.ch, self.cb, self.bs = device, channels, callback, blocksize
                self.alive = False

            def start(self):
                self.alive = True
                threading.Thread(target=self._run, daemon=True).start()

            def _run(self):
                delivered = False
                while self.alive:
                    data = fake.pending.get(self.dev)
                    if fake.is_recording() and data is not None and not delivered:
                        for i in range(0, len(data) - self.bs, self.bs):
                            self.cb(data[i:i + self.bs], self.bs, None, None)
                        delivered = True
                        fake.sent += 1
                    else:
                        self.cb(np.zeros((self.bs, self.ch), np.float32), self.bs, None, None)
                        time.sleep(0.02)
                    if not fake.is_recording():
                        delivered = False

            def stop(self):
                self.alive = False

            def close(self):
                pass

        class OutputStream:
            def __init__(self, device=None, channels=2, samplerate=48000, callback=None, **kw):
                if device == 99:
                    raise RuntimeError("device unavailable")
                self.cb = callback
                fake.opened_outputs.append((device, samplerate))

            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                pass

        self.InputStream = InputStream
        self.OutputStream = OutputStream

    @staticmethod
    def WasapiSettings(**kw):
        return None

    def query_devices(self, i=None):
        return self.devices if i is None else self.devices[i]

    def query_hostapis(self, i=None):
        a = [dict(name="Windows WASAPI")]
        return a if i is None else a[i]

    def play(self, *a, **k):
        pass

    def stop(self):
        pass

    def _terminate(self):
        pass

    def _initialize(self):
        pass

    def feed(self, iface, usb=None):
        """Queue one take: iface (frames, 4) for the interface, usb (frames, 2) for the USB mic."""
        self.pending = {0: np.asarray(iface, np.float32)}
        if usb is not None:
            self.pending[2] = np.asarray(usb, np.float32)
        self.sent = 0


@pytest.fixture
def fake_sd(monkeypatch):
    from irstudio import audio
    fake = FakeSoundDevice()
    monkeypatch.setattr(audio, "sd", fake)
    return fake


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point every file the app writes (autosave, logs, settings) at a temporary folder."""
    from irstudio import config, project, ui_setup, app as app_mod, ui_capture, ui_build, ui_audition
    paths = {
        "DATA_DIR": str(tmp_path), "LOG_DIR": str(tmp_path / "logs"),
        "AUTOSAVE_DIR": str(tmp_path / "autosave"), "AUTOSAVE_PREV": str(tmp_path / "autosave_previous"),
        "DEVICES_FILE": str(tmp_path / "devices.json"), "STEPS_FILE": str(tmp_path / "step_lengths.json"),
        "CUSTOM_TARGETS_FILE": str(tmp_path / "custom_targets.json"),
        "SETTINGS_FILE": str(tmp_path / "settings.json"),
    }
    for mod in (config, project, ui_setup, app_mod, ui_capture, ui_build, ui_audition):
        for k, v in paths.items():
            if hasattr(mod, k):
                monkeypatch.setattr(mod, k, v)
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path / "home")))
    return tmp_path


@pytest.fixture
def app(fake_sd, data_dir, monkeypatch):
    """The real main window, on a fake sound card, with dialogs answered automatically."""
    import sys
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no display available for the window tests (use xvfb-run)")
    from tkinter import messagebox
    msgs = []
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: msgs.append(("info", a[1] if len(a) > 1 else a)))
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: msgs.append(("error", a[1] if len(a) > 1 else a)))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(messagebox, "askokcancel", lambda *a, **k: True)
    from irstudio.app import App
    a = App()
    a.update()
    a.test_messages = msgs
    fake_sd.is_recording = lambda: a.rig.recording
    yield a
    try:
        a._quit()
    except Exception:
        pass
