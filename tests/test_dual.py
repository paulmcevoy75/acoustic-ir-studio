# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""Dual piezo: two pickup elements that hear two independent sources differently.
A single IR on the summed pickup can't reproduce the mic; the IR pair should."""
import numpy as np
from scipy import signal
import pytest

from irstudio import engine as E

rng = np.random.default_rng(7)
fs = 48000


def body(seed, f0s, delay_ms):
    r = np.random.default_rng(seed)
    L = int(0.25 * fs)
    t = np.arange(L) / fs
    h = np.zeros(L)
    for f0 in f0s:
        h += r.uniform(.3, 1) * np.exp(-r.uniform(25, 60) * t) * np.sin(2 * np.pi * f0 * t + r.uniform(0, 6))
    h[int(delay_ms * 1e-3 * fs)] += 2.0
    return signal.lfilter(*signal.butter(1, 7000, "low", fs=fs), h) * 0.01


Ma = body(1, [98, 205, 410, 900, 2300], 1.1)    # how source a (bass side) reaches the mic
Mb = body(2, [120, 260, 520, 1300, 3100], 1.6)  # how source b (treble side) reaches the mic
lp = signal.butter(2, 2500, "low", fs=fs)
hp = signal.butter(2, 400, "high", fs=fs)


def sources(dur):
    n = int(dur * fs)
    sa = np.zeros(n)
    sb = np.zeros(n)
    pos = 0
    while pos < n - fs:
        for s, lo in ((sa, 80), (sb, 300)):
            f0 = rng.uniform(lo, lo * 4)
            k = int(1.2 * fs)
            tt = np.arange(k) / fs
            note = sum(np.sin(2 * np.pi * f0 * m * tt + rng.uniform(0, 6)) / m for m in range(1, 12)
                       if f0 * m < 15000) * np.exp(-3 * tt)
            note += 0.3 * rng.standard_normal(k) * np.exp(-60 * tt)       # pick noise: broadband
            kk = min(k, n - pos)
            s[pos:pos + kk] += rng.uniform(.05, .2) * note[:kk]
        pos += int(rng.uniform(.3, .8) * fs)
    return sa, sb


def mix(sa, sb, correlated=False):
    x1 = sa + 0.35 * signal.lfilter(*lp, sb)
    x2 = 0.3 * signal.lfilter(*hp, sa) + sb
    if correlated:                                     # one source only: no extra information
        x2 = 0.6 * signal.lfilter(*lp, x1)
        sb = np.zeros_like(sb)
    y = signal.fftconvolve(sa, Ma)[:len(sa)] + signal.fftconvolve(sb, Mb)[:len(sb)]
    return x1, x2, y + 1e-4 * rng.standard_normal(len(y))


def residual(pred, y):
    m = slice(int(0.5 * fs), len(y) - int(0.5 * fs))
    best = None
    for lag in range(-200, 201):
        p = np.roll(pred, lag)[m]
        g = np.dot(p, y[m]) / max(np.dot(p, p), 1e-30)
        r = 10 * np.log10(np.sum((y[m] - g * p) ** 2) / np.sum(y[m] ** 2))
        best = r if best is None else min(best, r)
    return best


def run(correlated):
    acc = E.Accumulator(fs, 65536)
    for k in range(6):
        sa, sb = sources(12)
        x1, x2, y = mix(sa, sb, correlated)
        acc.process_take(x1, y, f"t{k}", x2=x2)
    p = E.BuildParams(master_ms=250)
    single = E.build_master(acc, None, p)
    pair = E.build_dual(acc, None, p, single)
    sa, sb = sources(10)
    x1, x2, y = mix(sa, sb, correlated)
    pd = signal.fftconvolve(x1, pair["h1"])[:len(y)] + signal.fftconvolve(x2, pair["h2"])[:len(y)]
    hs = np.fft.irfft(acc.H(), acc.nfft)[: int(0.25 * fs)]          # measured-phase single IR
    ps = signal.fftconvolve(x1 + x2, hs)[:len(y)]
    gain = float(np.median([b["gain_db"] for b in acc.dual_gain() if 100 <= b["center"] <= 5000]))
    return acc, pair, residual(pd, y), residual(ps, y), gain


@pytest.fixture(scope="module")
def independent():
    return run(False)


@pytest.fixture(scope="module")
def correlated():
    return run(True)


def test_pair_beats_one_ir_when_elements_differ(independent):
    _, _, rd, rs, gain = independent
    assert rd < rs - 6          # the IR pair reproduces the mic far better
    assert gain > 3             # ...and the app reports a real gain


def test_pair_is_safe_when_elements_say_the_same(correlated):
    _, _, rd, rs, gain = correlated
    assert abs(rd - rs) < 3     # no worse than one IR on the summed pickup
    assert gain < 1.0           # ...and the app says there's nothing to gain


def test_dual_data_survives_saving(independent):
    acc = independent[0]
    acc2 = E.Accumulator.from_dict(acc.to_dict("A"), "A")
    assert acc2.has_dual() and np.allclose(acc2.dual["S1y"], acc.dual["S1y"])


def test_pair_fits_a_pedal(independent):
    fp = E.fit_pair(independent[1], 44100, 1024)
    assert len(fp["h1"]) == len(fp["h2"]) == 1024
    assert max(np.max(np.abs(fp["h1"])), np.max(np.abs(fp["h2"]))) <= 1
