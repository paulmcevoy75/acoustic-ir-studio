# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
import numpy as np
from scipy import signal
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ir_engine as E

rng = np.random.default_rng(1)
fs = 48000

# --- "true" body+mic response: resonances + reflections + HF tilt
L = int(0.3 * fs)
t = np.arange(L) / fs
h_true = np.zeros(L)
for f0, dec, amp in [(98, 40, 1.0), (195, 30, .7), (230, 35, .5), (410, 25, .4),
                     (620, 30, .3), (1100, 50, .2), (2400, 80, .15)]:
    h_true += amp * np.exp(-dec * t) * np.sin(2 * np.pi * f0 * t)
h_true += 0.3 * rng.standard_normal(L) * np.exp(-60 * t)
h_true[int(0.0012 * fs)] += 2.0                        # direct sound ~1.2 ms
b, a = signal.butter(1, 6000, "low", fs=fs)
h_true = signal.lfilter(b, a, h_true)


def pluck(f0, dur, amp):
    n = int(dur * fs)
    N = int(fs / f0)
    buf = rng.uniform(-1, 1, N)
    out = np.zeros(n)
    for i in range(n):
        out[i] = buf[i % N]
        buf[i % N] = 0.996 * 0.5 * (buf[i % N] + buf[(i + 1) % N])
    return amp * out


def take(notes, dur=12):
    x = np.zeros(int(dur * fs))
    pos = 0
    while pos < len(x) - fs:
        f0 = rng.choice(notes)
        p = pluck(f0, 1.5, rng.uniform(.05, .25))
        n = min(len(p), len(x) - pos)
        x[pos:pos + n] += p[:n]
        pos += int(rng.uniform(.3, 1.0) * fs)
    return x


def mic_of(x, noise=1e-3):
    y = signal.fftconvolve(x, h_true)[:len(x)] * 0.1
    return y + noise * rng.standard_normal(len(y))


def true_P(acc):
    H = np.fft.rfft(h_true * 0.1, acc.nfft)
    return np.abs(H) ** 2


def err(acc, P):
    f = acc.freqs
    Pt = E.smooth_frac(true_P(acc), f, 1 / 12)
    Pe = E.smooth_frac(P, f, 1 / 12)
    e, d = E._err_db(Pe, Pt, f, 80, 10000)
    return e


sessions = [
    [82, 110, 147, 196, 247, 330],          # chords-ish
    [82, 87, 98, 110, 123],                  # low
    [330, 392, 440, 523, 659, 784, 988],     # high
    [1319, 1568, 1976, 2637],                # harmonics
]

print("=== single interface ===")
acc = E.Accumulator(fs)
for i, notes in enumerate(sessions):
    x = take(notes)
    y = mic_of(x)
    if i == 1:   # inject a squeak/bump into the mic only
        y[3 * fs:3 * fs + 3000] += 0.3 * rng.standard_normal(3000)
    if i == 0:
        e1 = None
    r = acc.process_take(x, y, f"take{i}")
    _, Hr = E._power_response(acc, E.BuildParams(), None)
    print(f"take {i}: accepted {r['accepted']}/{r['segments_total']} "
          f"outliers {r.get('rejected_outlier')} delay {r['delay_ms']:.2f} ms "
          f"err {err(acc, np.abs(acc.H())**2):.2f} dB  warn={r['warnings']}")
cov = acc.coverage()
print("coverage:", [(int(c['center']), c['status']) for c in cov])
print("unexplained:", acc.explained_db())
print("tips:", E.suggestions(cov))

m = E.build_master(acc, None, E.BuildParams())
f = m["freqs"]
print("master err (min phase target vs true): %.2f dB" % err(acc, m["P"]))
Hm = np.abs(np.fft.rfft(m["h"], acc.nfft)) ** 2
for N, fsd in [(256, 44100), (512, 44100), (1024, 48000), (2048, 48000), (8192, 48000)]:
    r = E.fit_for_pedal(m, fsd, N)
    print(f"pedal {fsd}/{N}: fit err {r['fit_err_db']:.2f} dB, vs full {r['err_vs_full_db']:.2f} dB,"
          f" fade {r['fade']}, energy {r['energy_retained']*100:.1f}%, peak {np.max(np.abs(r['h'])):.3f}")
    # compare naive truncation
    from scipy.signal import resample_poly
    hn = m["h"]
    if fsd != fs:
        hn = resample_poly(hn, 147, 160)
    hn = hn[:N]
    Rn = np.abs(np.fft.rfft(hn, r["freqs"].size * 2 - 2)) ** 2
    en, _ = E._err_db(E.smooth_frac(Rn, r["freqs"], 1/12), r["T"], r["freqs"], 60, min(12000, .45*fsd))
    print(f"      naive hard truncation vs full: {en:.2f} dB")

print("=== two devices (drift + offset) ===")
acc2 = E.Accumulator(fs)
for i, notes in enumerate(sessions):
    x = take(notes, 15)
    y = mic_of(x)
    # simulate other device: 37 ms later start, +45 ppm clock
    ppm = 45e-6
    tt = np.arange(int(len(y) * (1 + ppm)) - 2000) / (1 + ppm)
    tt = tt + 0.0371 * fs
    tt = tt[tt < len(y) - 1]
    yd = signal.resample(y, len(y) * 4)  # band-limited interp via 4x upsample
    yd = np.interp(tt * 4, np.arange(len(yd)), yd)
    r = acc2.process_take(x, yd, f"t{i}", two_device=True)
    print(f"take {i}: offset {r['offset_ms']:.2f} ms drift {r['drift_ppm']:.1f} ppm "
          f"resid {r['lock_resid_samples']:.3f} prior {r['used_session_drift']} acc {r['accepted']} "
          f"fine {r.get('fine_align_samples', 0):.3f} err {err(acc2, np.abs(acc2.H())**2):.2f} dB "
          f"warn={r['warnings']}")
print("unexplained:", acc2.explained_db())
