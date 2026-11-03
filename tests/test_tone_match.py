# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""Tone-match accuracy test: reference = notes through a known 'target' response,
piezo = separately played notes of the same pitches. Prints error vs the truth."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np
from scipy import signal
import ir_engine as E
import tone_match as T

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

notes_sets = {"1 note (A2)": [110.0],
              "3 notes": [82.4, 146.8, 246.9],
              "6 notes (open strings)": [82.4, 110, 146.8, 196, 246.9, 329.6],
              "10 notes": [82.4, 98, 110, 130.8, 146.8, 174.6, 196, 246.9, 329.6, 440]}
cache = {}
for label, notes in notes_sets.items():
    pairs = []
    for f0 in notes:
        r = signal.fftconvolve(pluck(f0), h_true)[: int(2.5 * fs)] * 0.1
        r += 1e-4 * rng.standard_normal(len(r))
        p = pluck(f0) + 1e-4 * rng.standard_normal(int(2.5 * fs))
        rs, re_ = T.auto_select(r, fs); ps, pe = T.auto_select(p, fs)
        hr = T.harmonics(r[rs:re_], fs); hp = T.harmonics(p[ps:pe], fs)
        assert abs(hr["f0"] / f0 - 1) < 0.01 and abs(hp["f0"] / f0 - 1) < 0.01, (f0, hr["f0"], hp["f0"])
        pairs.append((hr, hp))
    line = f"{label:<24}"
    for sm, nm in ((1 / 3, "1/3 oct"), (1.0, "1 oct")):
        m = T.build_matched(pairs, fs, smooth_oct=sm, low_cut=0, high_cut=0)
        g = m["match"]["grid"]; c = m["match"]["curve"]; tr = truth_db(g, sm)
        sel = (g >= 80) & (g <= 8000)
        lo = (g >= 80) & (g <= 300)
        line += (f" | {nm}: 80-8k {np.sqrt(np.mean((c - tr)[sel] ** 2)):4.1f} dB"
                 f" (80-300 {np.sqrt(np.mean((c - tr)[lo] ** 2)):4.1f})")
    print(line)
print("pitch check:", T.note_name(T.detect_f0(pluck(82.4), fs)), "|", T.note_name(T.detect_f0(pluck(329.6), fs)))

# ---- refine mode: measured IR of this guitar + a reference with a different overall tone
def eq_db(f):
    f = np.maximum(f, 1e-3)   # target tone difference: +4 dB low shelf, -5 dB presence dip, bright top
    return (4 / (1 + (f / 200.0) ** 2) - 5 * np.exp(-0.5 * (np.log2(f / 3000) / 0.5) ** 2)
            + 3 / (1 + (8000 / np.maximum(f, 1)) ** 2))
nfft = 1 << 17
f = np.fft.rfftfreq(nfft, 1 / fs)
Ht = np.fft.rfft(h_true, nfft)
Heq = Ht * 10 ** (eq_db(f) / 20)
h_ref = np.fft.irfft(Heq, nfft)[: int(0.5 * fs)]
base = {"h": h_true / np.max(np.abs(h_true)), "fs": fs, "P": np.abs(Ht) ** 2, "freqs": f,
        "nfft": nfft, "sign": 1.0}
for label, notes in (("refine, 3 notes x4", [82.4, 146.8, 246.9]),
                     ("refine, 6 notes x4", [82.4, 110, 146.8, 196, 246.9, 329.6])):
    pairs = []
    for f0 in notes:
        # recommended use: the same note plucked 4 times, at a different level each side
        xr = np.concatenate([pluck(f0, 2.0) for _ in range(4)])
        xp = np.concatenate([pluck(f0, 2.0) for _ in range(4)])
        r = signal.fftconvolve(xr, h_ref)[: len(xr)] * rng.uniform(0.02, 0.3) + 1e-5 * rng.standard_normal(len(xr))
        p = T.through_ir(xp, fs, base) * rng.uniform(0.02, 0.3) + 1e-5 * rng.standard_normal(len(xp))
        pairs.append((T.analyse(r[slice(*T.auto_select(r, fs))], fs),
                      T.analyse(p[slice(*T.auto_select(p, fs))], fs)))
    m = T.build_matched(pairs, fs, smooth_oct=1 / 3, base_master=base)
    g = m["match"]["grid"]; c = m["match"]["curve"]
    tr = eq_db(g); ref = (g > 200) & (g < 4000); tr = tr - tr[ref].mean()
    sel = (g >= 80) & (g <= 8000)
    print(f"{label:<24} | correction vs true EQ: 80-8k {np.sqrt(np.mean((c - tr)[sel] ** 2)):4.1f} dB rms, "
          f"max {np.max(np.abs(c - tr)[sel]):4.1f} dB")
