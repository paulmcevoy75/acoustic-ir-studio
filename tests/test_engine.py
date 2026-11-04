# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""IR engine accuracy against a known ('true') body + mic response."""

import numpy as np
import pytest
from scipy import signal

from irstudio import engine as E
from conftest import FS, SESSIONS, make_body, playing

H_TRUE = make_body()


def mic_of(x, rng, noise=1e-3):
    return signal.fftconvolve(x, H_TRUE)[:len(x)] + noise * rng.standard_normal(len(x))


def err_db(acc, P):
    f = acc.freqs
    Pt = E.smooth_frac(np.abs(np.fft.rfft(H_TRUE, acc.nfft)) ** 2, f, 1 / 12)
    return E._err_db(E.smooth_frac(P, f, 1 / 12), Pt, f, 80, 10000)[0]


@pytest.fixture(scope="module")
def single():
    """Four takes on one interface; take 1 has a bump on the mic only."""
    rng = np.random.default_rng(2)
    acc = E.Accumulator(FS)
    reports = []
    for i, notes in enumerate(SESSIONS):
        x = playing(notes, 12, rng)
        y = mic_of(x, rng)
        if i == 1:
            y[3 * FS:3 * FS + 3000] += 0.3 * rng.standard_normal(3000)
        reports.append(acc.process_take(x, y, f"take{i}"))
    return acc, reports


def test_each_take_matches_the_truth(single):
    acc, reports = single
    assert all(r["accepted"] > 0 for r in reports)
    assert err_db(acc, np.abs(acc.H()) ** 2) < 0.3
    assert abs(reports[0]["delay_ms"] - 1.2) < 0.1


def test_most_of_the_mic_is_explained(single):
    acc, _ = single
    assert acc.explained_db()[0] < -20


def test_master_ir_is_accurate(single):
    acc, _ = single
    m = E.build_master(acc, None, E.BuildParams())
    assert err_db(acc, m["P"]) < 1.5
    assert np.max(np.abs(m["h"])) <= 1.0


@pytest.mark.parametrize("fs_dst,N", [(44100, 256), (44100, 1024), (48000, 2048), (48000, 9600)])
def test_pedal_fit_beats_plain_truncation(single, fs_dst, N):
    acc, _ = single
    m = E.build_master(acc, None, E.BuildParams())
    r = E.fit_for_pedal(m, fs_dst, N)
    assert len(r["h"]) <= N and np.max(np.abs(r["h"])) <= 1.0
    hn = signal.resample_poly(m["h"], 147, 160)[:N] if fs_dst != FS else m["h"][:N]
    Rn = np.abs(np.fft.rfft(hn, r["freqs"].size * 2 - 2)) ** 2
    naive = E._err_db(E.smooth_frac(Rn, r["freqs"], 1 / 12), r["T"], r["freqs"], 60, min(12000, .45 * fs_dst))[0]
    assert r["err_vs_full_db"] <= naive + 0.05


def test_two_devices_drift_and_offset_are_corrected():
    """A USB mic on its own clock: starts 37 ms later and runs 45 ppm fast."""
    rng = np.random.default_rng(3)
    acc = E.Accumulator(FS)
    for i, notes in enumerate(SESSIONS[:3]):
        x = playing(notes, 15, rng)
        y = mic_of(x, rng)
        ppm = 45e-6
        tt = np.arange(int(len(y) * (1 + ppm)) - 2000) / (1 + ppm) + 0.0371 * FS
        tt = tt[tt < len(y) - 1]
        yd = signal.resample(y, len(y) * 4)
        yd = np.interp(tt * 4, np.arange(len(yd)), yd)
        r = acc.process_take(x, yd, f"t{i}", two_device=True)
        assert abs(r["drift_ppm"] - 45) < 3
        assert r["accepted"] > 0
    assert err_db(acc, np.abs(acc.H()) ** 2) < 0.5
    assert acc.explained_db()[0] < -20


def test_project_round_trip(single):
    acc, _ = single
    b = E.Accumulator.from_dict(acc.to_dict("A"), "A")
    assert b.n_seg == acc.n_seg
    assert np.allclose(b.H(), acc.H())


def test_suggestions_name_notes_for_weak_mids():
    cov = [{"center": 400.0, "status": "weak", "unc_db": 3.0},
           {"center": 12500.0, "status": "need data", "unc_db": float("inf")}]
    tips = E.suggestions(cov)
    assert any("fret" in t for t in tips)          # a specific place on the neck
    assert any("harmonics" in t for t in tips)     # the top end: harmonics / bright playing


def test_band_weights_follow_the_mix():
    f = np.array([50, 1000, 16000])
    w = E.band_weights(f, 0.0, 0.5, 1.0, 250, 3000)
    assert w[0] < 0.05 and abs(w[1] - 0.5) < 0.05 and w[2] > 0.95


def test_firmware_files(tmp_path):
    h = np.linspace(-0.5, 0.5, 100).astype(np.float32)
    E.write_bin(tmp_path / "ir.bin", h)
    assert np.array_equal(np.fromfile(tmp_path / "ir.bin", "<f4"), h)
    ident = E.write_c_header(str(tmp_path / "ir.h"), h, 48000, "My IR 100")
    txt = (tmp_path / "ir.h").read_text()
    assert f"static const float {ident}[100]" in txt and "_LENGTH 100" in txt
