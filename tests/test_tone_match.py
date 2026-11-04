# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""Tone-match accuracy test: reference = notes through a known 'target' response,
piezo = separately played notes of the same pitches. Prints error vs the truth."""
import numpy as np
from scipy import signal
import pytest

from irstudio import engine as E
from irstudio import tone_match as T

rng = np.random.default_rng(7)
fs = 48000
L = int(0.3 * fs); t = np.arange(L) / fs
h_true = np.zeros(L)
for f0, dec, amp in [(98, 40, 1.0), (195, 30, .7), (230, 35, .5), (410, 25, .4),
                     (620, 30, .3), (1100, 50, .2), (2400, 80, .15)]:
    h_true += amp * np.exp(-dec * t) * np.sin(2 * np.pi * f0 * t)
h_true += 0.3 * rng.standard_normal(L) * np.exp(-60 * t)
h_true[60] += 2.0
b, a = signal.butter(1, 6000, "low", fs=fs); h_true = signal.lfilter(b, a, h_true)

def pluck(f0, dur=2.5, amp=0.3, pos=None):
    """Karplus-Strong with fractional delay (accurate pitch) and a pluck-position comb."""
    n = int(dur * fs); N = fs / f0
    Ni = int(N); frac = N - Ni
    # realistic pluck: triangular string displacement peaking at the pick point,
    # softened (finger/pick width), with a little random variation
    pp = (pos or rng.uniform(0.15, 0.25)) * (Ni + 2)
    k = np.arange(Ni + 2)
    exc = np.where(k < pp, k / pp, (Ni + 2 - k) / (Ni + 2 - pp))
    exc = np.convolve(exc, np.hanning(max(3, int(0.04 * Ni))), "same")
    exc = exc - exc.mean() + 0.03 * rng.standard_normal(Ni + 2) * exc.std()
    buf = np.zeros(n + Ni + 2); buf[:Ni + 2] = exc
    g = 0.997
    for i in range(Ni + 2, n + Ni + 2):
        d = i - Ni
        buf[i] = g * (0.5 * ((1 - frac) * buf[d] + frac * buf[d - 1]) +
                      0.5 * ((1 - frac) * buf[d - 1] + frac * buf[d - 2]))
    y = buf[Ni + 2:]
    return amp * y / np.max(np.abs(y))

def truth_db(grid, sm=1 / 6):
    H = np.abs(np.fft.rfft(h_true, 1 << 17)) ** 2
    f = np.fft.rfftfreq(1 << 17, 1 / fs)
    Hs = E.smooth_frac(H, f, sm)
    d = 10 * np.log10(np.interp(grid, f, Hs))
    ref = (grid > 200) & (grid < 4000)
    return d - d[ref].mean()


def note_pairs(notes):
    pairs = []
    for f0 in notes:
        r = signal.fftconvolve(pluck(f0), h_true)[: int(2.5 * fs)] * 0.1
        r += 1e-4 * rng.standard_normal(len(r))
        p = pluck(f0) + 1e-4 * rng.standard_normal(int(2.5 * fs))
        rs, re_ = T.auto_select(r, fs)
        ps, pe = T.auto_select(p, fs)
        hr, hp = T.harmonics(r[rs:re_], fs), T.harmonics(p[ps:pe], fs)
        assert abs(hr["f0"] / f0 - 1) < 0.01 and abs(hp["f0"] / f0 - 1) < 0.01
        pairs.append((hr, hp))
    return pairs


def test_pitch_detection():
    assert abs(T.detect_f0(pluck(82.4), fs) / 82.4 - 1) < 0.01
    assert abs(T.detect_f0(pluck(329.6), fs) / 329.6 - 1) < 0.01


def test_notes_only_gets_the_broad_tone():
    """Six open strings, compared with the true response at 1-octave detail."""
    m = T.build_matched(note_pairs([82.4, 110, 146.8, 196, 246.9, 329.6]), fs, smooth_oct=1.0,
                        low_cut=0, high_cut=0)
    g, c = m["match"]["grid"], m["match"]["curve"]
    sel = (g >= 80) & (g <= 8000)
    assert np.sqrt(np.mean((c - truth_db(g, 1.0))[sel] ** 2)) < 4.0


def eq_db(f):
    f = np.maximum(f, 1e-3)   # target tone difference: +4 dB low shelf, -5 dB presence dip, bright top
    return (4 / (1 + (f / 200.0) ** 2) - 5 * np.exp(-0.5 * (np.log2(f / 3000) / 0.5) ** 2)
            + 3 / (1 + (8000 / np.maximum(f, 1)) ** 2))


@pytest.mark.parametrize("notes", [[82.4, 146.8, 246.9], [82.4, 110, 146.8, 196, 246.9, 329.6]])
def test_refine_mode_finds_the_tone_difference(notes):
    nfft = 1 << 17
    f = np.fft.rfftfreq(nfft, 1 / fs)
    Ht = np.fft.rfft(h_true, nfft)
    h_ref = np.fft.irfft(Ht * 10 ** (eq_db(f) / 20), nfft)[: int(0.5 * fs)]
    base = {"h": h_true / np.max(np.abs(h_true)), "fs": fs, "P": np.abs(Ht) ** 2, "freqs": f,
            "nfft": nfft, "sign": 1.0}
    pairs = []
    for f0 in notes:
        xr = np.concatenate([pluck(f0, 2.0) for _ in range(4)])
        xp = np.concatenate([pluck(f0, 2.0) for _ in range(4)])
        r = signal.fftconvolve(xr, h_ref)[: len(xr)] * rng.uniform(0.02, 0.3) + 1e-5 * rng.standard_normal(len(xr))
        p = T.through_ir(xp, fs, base) * rng.uniform(0.02, 0.3) + 1e-5 * rng.standard_normal(len(xp))
        pairs.append((T.analyse(r[slice(*T.auto_select(r, fs))], fs),
                      T.analyse(p[slice(*T.auto_select(p, fs))], fs)))
    m = T.build_matched(pairs, fs, smooth_oct=1 / 3, base_master=base)
    g, c = m["match"]["grid"], m["match"]["curve"]
    tr = eq_db(g)
    tr = tr - tr[(g > 200) & (g < 4000)].mean()
    sel = (g >= 80) & (g <= 8000)
    assert np.sqrt(np.mean((c - tr)[sel] ** 2)) < 2.5
