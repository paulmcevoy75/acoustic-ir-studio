# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""The whole app on a fake sound card: capture, build, audition, export, save, reopen."""

import os
import time

import numpy as np
import pytest
from scipy import signal

from conftest import FS, SESSIONS, make_body, playing

pytestmark = pytest.mark.gui

H_A = make_body(1)                                    # reference mic path
LP = signal.butter(2, 4000, "low", fs=FS)


def take_signals(rng, notes, dur=10, dual=False):
    """Interface: piezo In1, color mic In2, (piezo 2 In3), internal mic In4.
    USB mic: the reference mic, starting 20 ms late and running 40 ppm fast."""
    x1 = playing(notes, dur, rng)
    x2 = 0.5 * signal.lfilter(*signal.butter(2, 600, "high", fs=FS), playing(notes, dur, rng)) if dual else None
    x = x1 + (x2 if dual else 0)
    y = signal.fftconvolve(x, H_A)[:len(x)] + 1e-4 * rng.standard_normal(len(x))
    iface = np.zeros((len(x), 4), np.float32)
    iface[:, 0] = x1
    iface[:, 1] = signal.lfilter(*LP, y) * 1.3
    if dual:
        iface[:, 2] = x2
    iface[:, 3] = 0.3 * signal.lfilter(*signal.butter(2, 800, "high", fs=FS), x)
    ppm, off = 40e-6, int(0.02 * FS)
    tt = np.arange(int(len(y) * (1 + ppm)) - off - 500) / (1 + ppm) + off
    tt = tt[tt < len(y) - 1]
    yA = np.interp(tt, np.arange(len(y)), y)
    return iface, np.stack([yA, yA], axis=1)


def setup_inputs(app, dual=False):
    vals = list(app.inp["piezo"]["cb"]["values"])
    pick = lambda s: next(v for v in vals if s in v)
    app.inp["piezo"]["dev"].set(pick("Test Interface 4x4)  -  Input 1"))
    app.inp["A"]["dev"].set(pick("USB Measurement Mic)  -  Input 1"))
    app.inp["B"]["dev"].set(pick("Test Interface 4x4)  -  Input 2"))
    app.inp["I"]["dev"].set(pick("Test Interface 4x4)  -  Input 4"))
    for r in ("A", "B", "I"):
        app.inp[r]["on"].set(True)
    if dual:
        app.inp["piezo2"]["on"].set(True)
        app.inp["piezo2"]["dev"].set(pick("Test Interface 4x4)  -  Input 3"))
    app._inputs_changed()


def wait(app, cond, timeout=60):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        app.update()
        time.sleep(0.02)
    assert cond(), "timed out"


def record(app, fake_sd, iface, usb, start):
    fake_sd.feed(iface, usb)
    start()
    wait(app, lambda: fake_sd.sent >= 2)


def capture(app, fake_sd, n=3, dual=False):
    rng = np.random.default_rng(5)
    for i in range(n):
        iface, usb = take_signals(rng, SESSIONS[i % len(SESSIONS)], dual=dual)
        app.goto_step(i)
        record(app, fake_sd, iface, usb, app.record_step)
        app.stop_record()
        wait(app, lambda: not app.busy)
    app.update()


@pytest.fixture
def save_to(app, data_dir, monkeypatch):
    from tkinter import filedialog
    path = str(data_dir / "projects" / "My Song.irproject")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **k: path)
    return path


def test_capture_build_audition_export_reopen(app, fake_sd, save_to):
    setup_inputs(app)
    assert not app.inp["piezo2"]["on"].get()
    capture(app, fake_sd)
    assert app.acc["A"].n_seg > 0 and app.acc["B"].n_seg > 0
    assert "drift_ppm" in app.acc["A"].takes[-1].meta            # USB mic was clock-synced

    app.nb.select(app.tab_build)
    app.build()
    assert app.irm is not None
    assert app.v_ir_src.get() == "mix"                          # blend stays available (regression)
    assert "disabled" not in app.mix_scales[0].state()

    # demo + every audition option (the demo records the mics by default)
    rng = np.random.default_rng(9)
    iface, usb = take_signals(rng, SESSIONS[0], dur=6)
    record(app, fake_sd, iface, usb, app.record_demo)
    app.transport_stop()                                        # one press stops the recording
    assert not app.rig.recording and {"piezo", "A", "B", "I"} <= set(app.demo)
    for mode in ("dry", "A", "B", "I", "ir", "irA", "irB", "irmix"):
        app.aud_src.set(mode)
        app.audition_play()
        assert app.player.playing, mode
    app.audition_stop()
    errors = [m for m in app.test_messages if m[0] == "error"]
    assert not errors, errors

    # export every length into '<Project Name> IRs' next to the saved project
    app.export_all()
    folder = os.path.join(os.path.dirname(save_to), "My Song IRs")
    files = os.listdir(folder)
    for tag in ("Short-", "Medium-", "Long-", "Ultimate-"):
        assert any(tag in f and f.endswith(".wav") for f in files), tag
    assert any(f.endswith(".bin") for f in files) and any(f.endswith(".h") for f in files)
    assert not any("_P1" in f for f in files)                   # single piezo: no pair
    assert os.path.isfile(save_to)

    # reopen the project
    n_seg = app.acc["A"].n_seg
    app.acc = {"A": None, "B": None}
    assert app.load_session(save_to)
    assert app.acc["A"].n_seg == n_seg and app.demo is not None and app.irm is not None


def test_autosave_and_crash_recovery(app, fake_sd, data_dir):
    from irstudio import project
    setup_inputs(app)
    capture(app, fake_sd, n=2)
    app.autosave(wait=True)
    p = os.path.join(project.AUTOSAVE_DIR, "session.irproject")
    assert os.path.isfile(p) and not os.path.exists(p + ".tmp")
    takes = sorted(f for f in os.listdir(os.path.join(project.AUTOSAVE_DIR, "takes")) if f.endswith(".flac"))
    assert len(takes) == 2
    n_seg = app.acc["A"].n_seg
    app.acc = {"A": None, "B": None}
    app._reopen_autosave()
    assert app.acc["A"].n_seg == n_seg
    app.recover_from_recordings()                                # rebuild from the saved audio
    wait(app, lambda: not app.busy, 120)
    assert app.acc["A"].n_seg > 0


def test_dual_piezo_pair(app, fake_sd, save_to):
    setup_inputs(app, dual=True)
    assert "same device" in app.inp["piezo2"]["info"].get()
    capture(app, fake_sd, n=3, dual=True)
    assert app.acc["A"].has_dual()
    app.nb.select(app.tab_build)
    app.build()
    assert app.dual_pair is not None and app.v_dual_export.get()
    app.ped_tree.selection_set(["2"])
    app.export()
    folder = os.path.join(os.path.dirname(save_to), "My Song IRs")
    assert any("_P1" in f for f in os.listdir(folder)) and any("_P2" in f for f in os.listdir(folder))


def test_only_inputs_in_use_get_meter_rows(app):
    app.inp["B"]["on"].set(False)
    app.inp["I"]["on"].set(False)
    app._inputs_changed()
    app.update()
    shown = {r: bool(app.meter_row[r][0].winfo_manager()) for r in ("piezo", "piezo2", "A", "B", "I")}
    assert shown == {"piezo": True, "piezo2": False, "A": True, "B": False, "I": False}


def test_bad_output_device_is_reported(app, fake_sd):
    app.demo = None
    app._set_demo({"piezo": np.zeros(3 * FS, np.float32)}, FS, {}, save=False)
    app.out_dev.set("99: Broken")
    app.audition_play()
    assert any(m[0] == "error" and "Broken" in str(m[1]) for m in app.test_messages)


def test_finger_noise_tamer_is_off_by_default_and_saved(app, fake_sd, save_to):
    setup_inputs(app)
    capture(app, fake_sd, n=2)
    assert not app.v_tame.get() and app.params().tame_db == 0
    app.nb.select(app.tab_build)
    app.build()
    p_off = app.irm["P"].copy()
    app.v_tame.set(True)
    app.v_tame_db.set(6)
    app.v_tame_hz.set("4000")
    app.build()
    f = app.irm["freqs"]
    drop = 10 * np.log10(np.interp(4000, f, app.irm["P"]) / np.interp(4000, f, p_off))
    assert -6.5 < drop < -5.5
    app.save_session()
    app.v_tame.set(False)
    app.load_session(save_to)
    assert app.v_tame.get() and app.params().tame_hz == 4000


def test_pedal_check_and_same_loudness_export(app, fake_sd, save_to):
    setup_inputs(app)
    capture(app, fake_sd, n=3)
    app.nb.select(app.tab_build)
    app.build()
    assert app.v_norm.get() == "Same loudness as dry"
    r = app._fit("0")
    assert r["short_db"] is not None                          # the level note is computed

    # the pedal's output on Input 3: the piezo through an IR that lost its low end
    vals = list(app.inp["piezo"]["cb"]["values"])
    app.inp["PO"]["on"].set(True)
    app.inp["PO"]["dev"].set(next(v for v in vals if "Test Interface 4x4)  -  Input 3" in v))
    app._inputs_changed()
    rng = np.random.default_rng(12)
    iface, usb = take_signals(rng, SESSIONS[0], dur=15)
    h_ped = signal.lfilter(*signal.butter(2, 200, "high", fs=FS), H_A)
    iface[:, 2] = signal.fftconvolve(iface[:, 0], h_ped)[:len(iface)]
    app.open_pedal_check()
    app.update()
    record(app, fake_sd, iface, usb, app.pedal_check_record)
    app.pedal_check_stop()
    wait(app, lambda: app._pc_result is not None or "Couldn't" in app.pc_status.cget("text"), 120)
    assert app._pc_result is not None, app.pc_status.cget("text")
    assert app._pc_result["correction_db"][np.argmin(abs(app._pc_result["freqs"] - 100))] > 2
    p_before = app.irm["P"].copy()
    app.pedal_check_apply()
    f = app.irm["freqs"]
    assert np.interp(100, f, app.irm["P"]) > 1.5 * np.interp(100, f, p_before)   # lows added back
    app.save_session()
    app.pedal_corr = None
    app.load_session(save_to)
    assert app.pedal_corr is not None and app.params().corr is not None
    app.pc_win.destroy()
