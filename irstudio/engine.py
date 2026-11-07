# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""
IR engine for acoustic-guitar piezo -> mic impulse responses.

Method
------
* Every take (piezo x, mic y, recorded simultaneously) is cut into long Hann
  segments. Each usable segment adds to running cross/auto spectra:
      Sxx += |X|^2,  Syy += |Y|^2,  Sxy += conj(X) * Y
  The transfer function is the H1 estimator  H = Sxy / Sxx  computed once,
  over *all* takes. Each take therefore contributes most at the frequencies
  where it actually had energy (low notes -> bass, harmonics -> treble).
* Segments are rejected for clipping, silence, or poor agreement with the
  piezo (fret squeaks, room noise, bumps).
* Coherence gives a per-band statistical uncertainty (+/- dB) which drives
  the coverage meter and the "what to play next" suggestions.
* When mic and piezo come from two different devices (e.g. USB measurement
  mic + interface), clock drift and latency are estimated per take and the
  mic signal is time-warped onto the piezo clock before analysis.
* Build: regularised, coherence-aware smoothing, mic calibration, cut filters,
  minimum or measured phase, A/B mic blend.
* Pedal export: resample, then an iterative minimum-phase FIR fit that
  pre-compensates the target so the truncated IR matches the full response
  as closely as its length allows; the best tail fade is chosen automatically.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field

import numpy as np
from scipy import signal

log = logging.getLogger("acoustic_ir")

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
BAND_CENTERS = np.array([31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
                         630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000,
                         5000, 6300, 8000, 10000, 12500], dtype=float)
BAND_LO = BAND_CENTERS * 2 ** (-1 / 6)
BAND_HI = BAND_CENTERS * 2 ** (1 / 6)

GOOD_DB = 0.5     # band uncertainty considered solid
OK_DB = 1.5       # acceptable
MIN_HITS = 4      # segments with energy in a band before we trust coherence

OPEN_STRINGS = [("low E", 82.41), ("A", 110.0), ("D", 146.83),
                ("G", 196.0), ("B", 246.94), ("high E", 329.63)]
BASS_STRINGS = [("E", 41.2), ("A", 55.0), ("D", 73.42), ("G", 98.0)]
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(f):
    n = int(round(12 * math.log2(f / 440.0))) + 57
    return f"{NOTE_NAMES[n % 12]}{n // 12}"


# ----------------------------------------------------------------------------
# Small DSP helpers
# ----------------------------------------------------------------------------
def db(x, floor=1e-20):
    return 10 * np.log10(np.maximum(x, floor))


def variable_frac(freqs, low_frac, high_frac, f_lo=100.0, f_hi=1000.0):
    """Per-frequency smoothing width: low_frac below f_lo, high_frac above f_hi,
    log-interpolated in between (e.g. psychoacoustic 1/3 -> 1/6)."""
    f = np.maximum(freqs, 1e-3)
    t = np.clip((np.log(f) - np.log(f_lo)) / (np.log(f_hi) - np.log(f_lo)), 0, 1)
    return np.exp((1 - t) * np.log(low_frac) + t * np.log(high_frac))


def smooth_frac(P, freqs, frac, min_hz=0.0):
    """Fractional-octave smoothing (mean of power) with optional minimum width.
    frac may be a number or a per-frequency array (variable smoothing)."""
    if isinstance(frac, (tuple, list)):
        frac = variable_frac(freqs, *frac)
    if np.ndim(frac) == 0 and (frac is None or frac <= 0) and min_hz <= 0:
        return P.copy()
    if np.ndim(frac) == 0:
        frac = frac or 0.0
    f = np.maximum(freqs, 1e-9)
    lo = f * 2 ** (-frac / 2)
    hi = f * 2 ** (frac / 2)
    if min_hz > 0:
        lo = np.minimum(lo, f - min_hz / 2)
        hi = np.maximum(hi, f + min_hz / 2)
    i0 = np.clip(np.searchsorted(freqs, lo, "left"), 0, len(P) - 1)
    i1 = np.clip(np.searchsorted(freqs, hi, "right"), 1, len(P))
    i1 = np.maximum(i1, i0 + 1)
    c = np.concatenate([[0.0], np.cumsum(P)])
    out = (c[i1] - c[i0]) / (i1 - i0)
    out[0] = P[0]
    return out


def minphase_from_power(P, n):
    """Minimum-phase spectrum (rfft grid, length n FFT) from a power spectrum."""
    logmag = 0.5 * np.log(np.maximum(P, 1e-24))
    c = np.fft.irfft(logmag, n)
    fold = np.zeros(n)
    fold[0] = c[0]
    fold[1:n // 2] = 2 * c[1:n // 2]
    fold[n // 2] = c[n // 2]
    return np.exp(np.fft.rfft(fold))


def interp_logf(f_src, v_src, f_dst):
    """Interpolate a positive quantity on log-frequency, holding edges."""
    fs = np.maximum(f_src, 1e-3)
    fd = np.maximum(f_dst, 1e-3)
    return np.interp(np.log(fd), np.log(fs), v_src)


def butter_mag2(freqs, fc, order, kind):
    r = np.maximum(freqs, 1e-9) / fc
    if kind == "hp":
        return 1.0 / (1.0 + r ** (-2 * order))
    return 1.0 / (1.0 + r ** (2 * order))


def half_hann_tail(n, frac):
    w = np.ones(n)
    m = max(2, int(round(n * frac)))
    m = min(m, n)
    w[n - m:] = 0.5 * (1 + np.cos(np.pi * np.arange(1, m + 1) / m))
    return w


def resample_to(x, fs_in, fs_out):
    if fs_in == fs_out:
        return x
    g = math.gcd(int(fs_in), int(fs_out))
    return signal.resample_poly(x, int(fs_out) // g, int(fs_in) // g)


# ----------------------------------------------------------------------------
# Clock-drift / latency correction for two-device capture
# ----------------------------------------------------------------------------
def _gcc_phat(a, b, fs, band=(200.0, 5000.0), max_lag=None):
    """Return (lag_samples, strength) where b ~= a delayed by lag."""
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    A = np.fft.rfft(a, n)
    B = np.fft.rfft(b, n)
    R = np.conj(A) * B
    f = np.fft.rfftfreq(n, 1 / fs)
    R = R / np.maximum(np.abs(R), 1e-20)
    R[(f < band[0]) | (f > band[1])] = 0
    r = np.fft.irfft(R, n)
    r = np.concatenate([r[-(len(a) - 1):], r[:len(b)]]) if len(a) > 1 else r
    lags = np.arange(-(len(a) - 1), len(b))
    if max_lag is not None:
        m = np.abs(lags) <= max_lag
        r, lags = r[m], lags[m]
    k = int(np.argmax(r))
    # parabolic sub-sample refinement
    if 0 < k < len(r) - 1:
        y0, y1, y2 = r[k - 1], r[k], r[k + 1]
        den = y0 - 2 * y1 + y2
        off = 0.5 * (y0 - y2) / den if den != 0 else 0.0
    else:
        off = 0.0
    strength = r[k] / (np.median(np.abs(r)) + 1e-12)
    return lags[k] + off, strength


def _gcc_ml(x, y, fs, block=16384, max_lag=None, band=(150.0, 6000.0)):
    """Delay of y relative to x with maximum-likelihood (Hannan-Thomson) GCC.

    The cross spectrum is averaged over overlapping blocks, and every frequency
    is weighted by its coherence (gamma^2 / (1 - gamma^2)), so frequencies where
    the two signals really agree (the notes being played) decide the delay,
    and noise or room reverb between the notes is ignored. Much more reliable
    than plain PHAT on tonal guitar signals in a real room.
    Returns (lag_samples, strength).
    """
    n = min(len(x), len(y))
    if n < block:
        block = 1 << int(np.floor(np.log2(max(n, 1024))))
    hop = block // 2
    win = np.hanning(block)
    Sxy = np.zeros(block // 2 + 1, complex)
    Sxx = np.zeros(block // 2 + 1)
    Syy = np.zeros(block // 2 + 1)
    cnt = 0
    for s0 in range(0, n - block + 1, hop):
        X = np.fft.rfft(win * x[s0:s0 + block])
        Y = np.fft.rfft(win * y[s0:s0 + block])
        Sxy += np.conj(X) * Y
        Sxx += np.abs(X) ** 2
        Syy += np.abs(Y) ** 2
        cnt += 1
    if cnt < 2:
        return 0.0, 0.0
    f = np.fft.rfftfreq(block, 1 / fs)
    g2 = np.abs(Sxy) ** 2 / np.maximum(Sxx * Syy, 1e-30)
    g2 = np.clip(g2 - 1.0 / cnt, 0, 0.995)          # remove the small-sample bias
    W = g2 / (1 - g2) / np.maximum(np.abs(Sxy), 1e-30)
    W[(f < band[0]) | (f > band[1])] = 0
    r = np.fft.irfft(Sxy * W, block)
    L = block // 2 - 1 if max_lag is None else min(int(max_lag), block // 2 - 1)
    r = np.concatenate([r[-L:], r[:L + 1]])
    lags = np.arange(-L, L + 1)
    k = int(np.argmax(r))
    off = 0.0
    if 0 < k < len(r) - 1:
        y0, y1, y2 = r[k - 1], r[k], r[k + 1]
        den = y0 - 2 * y1 + y2
        off = 0.5 * (y0 - y2) / den if den != 0 else 0.0
    strength = r[k] / (np.median(np.abs(r)) + 1e-12)
    return float(lags[k] + off), float(strength)


def _theil_sen(t, d, w=None):
    """Robust line fit (median of pairwise slopes): one bad window can't skew it."""
    n = len(t)
    sl, ww = [], []
    for a in range(n):
        for b in range(a + 1, n):
            if t[b] != t[a]:
                sl.append((d[b] - d[a]) / (t[b] - t[a]))
                ww.append(1.0 if w is None else w[a] * w[b])
    sl, ww = np.asarray(sl), np.asarray(ww)
    o = np.argsort(sl)
    cw = np.cumsum(ww[o])
    slope = sl[o][np.searchsorted(cw, cw[-1] / 2)]
    icpt = np.median(d - slope * t)
    return float(icpt), float(slope)


def _xspec(x, y, block):
    """Welch-averaged cross/auto spectra of two equal-length signals."""
    hop = block // 2
    win = np.hanning(block)
    Sxy = np.zeros(block // 2 + 1, complex)
    Sxx = np.zeros(block // 2 + 1)
    Syy = np.zeros(block // 2 + 1)
    cnt = 0
    for s0 in range(0, min(len(x), len(y)) - block + 1, hop):
        X = np.fft.rfft(win * x[s0:s0 + block])
        Y = np.fft.rfft(win * y[s0:s0 + block])
        Sxy += np.conj(X) * Y
        Sxx += np.abs(X) ** 2
        Syy += np.abs(Y) ** 2
        cnt += 1
    return Sxy, Sxx, Syy, cnt


def _ir_peak_offset(x, y, fs, c0, block=65536, max_s=0.4):
    """Absolute offset from the peak of the measured impulse response (the direct
    sound) - robust where plain correlation is confused by body/room ringing."""
    n = min(len(x), len(y) - max(c0, 0))
    a0 = max(0, -c0)
    L = min(n - a0, int(40 * fs))
    if L < block:
        return c0
    xs = x[a0:a0 + L]
    ys = y[a0 + c0:a0 + c0 + L]
    Sxy, Sxx, _, cnt = _xspec(xs, ys, block)
    if cnt < 2:
        return c0
    H = Sxy / np.maximum(Sxx + 1e-9 * Sxx.max(), 1e-30)
    f = np.fft.rfftfreq(block, 1 / fs)
    H[(f < 100) | (f > 8000)] = 0
    h = np.fft.irfft(H, block)
    m = int(max_s * fs)
    cand = np.concatenate([h[-m:], h[:m + 1]])
    lags = np.arange(-m, m + 1)
    k = int(np.argmax(np.abs(cand)))
    return int(c0 + lags[k])


def estimate_drift(x, y, fs, max_offset_s=0.5):
    """Estimate delay(t) = a + b*t (samples) of y relative to x.

    Two devices (interface + USB mic) run on separate clocks, so the delay
    grows slowly and linearly through a take.
    1. Offset: rough correlation, then the peak of the measured impulse
       response (the direct sound), which body and room ringing can't fool.
    2. Drift: the take is cut into overlapping 4 s windows; for neighbouring
       windows the transfer functions are compared. The guitar body and room
       cancel out of that ratio, leaving only the tiny delay change between
       the windows, measured from the phase slope over the coherent
       frequencies. Summed up, these give the delay curve; a robust Theil-Sen
       line through it gives the drift.
    Returns (a, b_ppm, n_points, residual_samples) or None.
    """
    n = min(len(x), len(y))
    seg = min(n, int(40 * fs))
    c0, _ = _gcc_ml(x[:seg], y[:seg], fs, block=65536, max_lag=int(max_offset_s * fs))
    c = _ir_peak_offset(x, y, fs, int(round(c0)))
    dur = n / fs
    win = int((4.0 if dur >= 25 else 2.5) * fs)        # short takes: more, shorter windows
    hop = int((2.0 if dur >= 25 else 1.0) * fs)
    block = 16384
    f = np.fft.rfftfreq(block, 1 / fs)
    band = (f > 150) & (f < 7000)
    om = 2 * np.pi * f[band] / fs
    Hs, Ws, ts = [], [], []
    for s0 in range(max(0, -c), n - win - max(c, 0), hop):
        xs = x[s0:s0 + win]
        if np.sqrt(np.mean(xs ** 2)) < 1e-4:
            Hs.append(None); Ws.append(None); ts.append(s0 + win / 2)
            continue
        Sxy, Sxx, Syy, cnt = _xspec(xs, y[s0 + c:s0 + c + win], block)
        g2 = np.abs(Sxy) ** 2 / np.maximum(Sxx * Syy, 1e-30)
        g2 = np.clip(g2 - 1.0 / max(cnt, 1), 0, 0.99)
        Hs.append(Sxy / np.maximum(Sxx, 1e-30))
        Ws.append(np.minimum(g2 / (1 - g2), 50.0))
        ts.append(s0 + win / 2)
    if len(ts) < 3:
        return None
    d = [0.0]
    ok = [Hs[0] is not None]
    for k in range(1, len(ts)):
        if Hs[k] is None or Hs[k - 1] is None:
            d.append(d[-1]); ok.append(False)
            continue
        R = Hs[k][band] * np.conj(Hs[k - 1][band])
        w = np.minimum(Ws[k][band], Ws[k - 1][band])
        sw = np.sum(w * om * om)
        if sw <= 0:
            d.append(d[-1]); ok.append(False)
            continue
        # low band first (no phase wrapping possible), then refine on the full band
        lowb = f[band] < 1500
        swl = np.sum((w * om * om)[lowb])
        tau = -np.sum((w * om * np.angle(R))[lowb]) / swl if swl > 0 else 0.0
        for _ in range(3):
            ph = np.angle(R * np.exp(1j * om * tau))     # residual after removing tau
            tau += -np.sum(w * om * ph) / sw
        d.append(d[-1] + tau)
        ok.append(np.sum(w > 1) > 20)
    ts = np.asarray(ts, float)
    d = np.asarray(d, float) + c
    ok = np.asarray(ok)
    if ok.sum() < 3:
        return None
    a, b = _theil_sen(ts[ok], d[ok])
    res = d[ok] - (a + b * ts[ok])
    resid = float(np.sqrt(np.mean(res ** 2)))
    return float(a), float(b * 1e6), int(ok.sum()), resid


def warp_delay(y, a, b, n_out, lead):
    """Return z with z[t] = y[t + a + b*t - lead] (fractional, smoothly varying).

    Uses overlapping Hann chunks; each chunk gets a constant fractional delay
    applied in the frequency domain, which is accurate because drift within a
    chunk is far below a sample.
    """
    chunk = 4096
    hop = chunk // 2
    pad = 2048
    n_fft = chunk + 2 * pad
    w = np.hanning(chunk + 1)[:-1]  # periodic Hann: COLA at 50% overlap
    out = np.zeros(n_out + chunk)
    freqs = np.fft.rfftfreq(n_fft)
    for s in range(-hop, n_out, hop):
        center = s + chunk / 2
        d = a + b * center - lead          # source position offset
        di = int(np.floor(d))
        frac = d - di
        src0 = s + di - pad
        idx = np.arange(src0, src0 + n_fft)
        valid = (idx >= 0) & (idx < len(y))
        buf = np.zeros(n_fft)
        buf[valid] = y[idx[valid]]
        B = np.fft.rfft(buf)
        B *= np.exp(2j * np.pi * freqs * frac)   # advance by frac samples
        sh = np.fft.irfft(B, n_fft)[pad:pad + chunk]
        o0 = s
        lo = max(0, o0)
        hi = min(n_out + chunk, o0 + chunk)
        if hi > lo:
            out[lo:hi] += (sh * w)[lo - o0:hi - o0]
    return out[:n_out]


def align_two_device(x, y, fs, lead_ms=5.0, prior_ppm=None):
    """Align mic y onto piezo x clock. Returns (x, y_aligned, info dict).

    If this take's own drift estimate is unreliable (e.g. only high harmonics,
    whose periodicity confuses correlation) and a session drift is known, the
    session drift is used; the offset is then refined later from the IR.
    """
    est = estimate_drift(x, y, fs)
    used_prior = False
    if est is not None:
        a, ppm, npts, resid = est
    if prior_ppm is not None and (est is None or resid > 4.0
                                  or abs(ppm - prior_ppm) > 25):
        seg = min(len(x), len(y), int(20 * fs))
        coarse, _ = _gcc_phat(x[:seg], y[:seg], fs, max_lag=int(0.5 * fs))
        a, ppm = float(coarse), float(prior_ppm)
        npts, resid = (est[2] if est else 0), float("nan")
        used_prior = True
    elif est is None:
        raise ValueError("Could not lock the two devices together - "
                         "play louder or longer (needs ~6 s of playing).")
    lead = lead_ms * 1e-3 * fs
    n_out = len(x)
    z = warp_delay(y, a, ppm * 1e-6, n_out, lead)
    # second pass: measure the drift that's left after correcting (now tiny, so the
    # estimate is nearly unbiased) and fold it in
    if not used_prior:
        try:
            for _ in range(2):                    # repeat once more if a lot was left
                est2 = estimate_drift(x[:len(z)], z, fs)
                if est2 is None or est2[3] > 2.0 or abs(est2[1]) > 300:
                    break
                ppm += est2[1]
                z = warp_delay(y, a, ppm * 1e-6, n_out, lead)
                if abs(est2[1]) < 2:
                    break
        except Exception:
            log.debug("align_two_device: ignored error", exc_info=True)
    # absolute offset: with the drift removed, find the direct sound in the IR
    # (high band, phase-only + coherence weighted, so body resonances can't pull it)
    try:
        L = min(n_out, int(60 * fs))
        Sxy, Sxx, Syy, cnt = _xspec(x[:L], z[:L], 65536)
        if cnt >= 2:
            ff = np.fft.rfftfreq(65536, 1 / fs)
            g2 = np.clip(np.abs(Sxy) ** 2 / np.maximum(Sxx * Syy, 1e-30) - 1.0 / cnt, 0, 0.99)
            W = np.minimum(g2 / (1 - g2), 50.0) / np.maximum(np.abs(Sxy), 1e-30)
            W[(ff < 800) | (ff > 9000)] = 0
            r = np.fft.irfft(Sxy * W, 65536)
            m = int(0.06 * fs)
            seg_r = np.concatenate([r[-m:], r[:m + 1]])
            k = int(np.argmax(seg_r))
            off = 0.0
            if 0 < k < len(seg_r) - 1:
                y0, y1, y2 = seg_r[k - 1], seg_r[k], seg_r[k + 1]
                den = y0 - 2 * y1 + y2
                off = 0.5 * (y0 - y2) / den if den != 0 else 0.0
            shift = (k - m) + off - lead      # direct sound should sit at 'lead'
            if abs(shift) > 0.5:
                a += shift
                z = warp_delay(y, a, ppm * 1e-6, n_out, lead)
    except Exception:
        log.debug("align_two_device: ignored error", exc_info=True)
    t = np.arange(n_out)
    src = t + a + ppm * 1e-6 * t - lead
    ok = (src >= 0) & (src < len(y) - 1)
    i0 = int(np.argmax(ok))
    i1 = int(len(ok) - np.argmax(ok[::-1]))
    info = {"offset_ms": a / fs * 1e3, "drift_ppm": ppm, "lock_points": npts,
            "lock_resid_samples": resid, "used_session_drift": used_prior, "trim": (i0, i1)}
    return x[i0:i1], z[i0:i1], info


def ir_offset(Ht, Hg, freqs, fs, weight=None, max_ms=25.0, band=(80.0, 10000.0)):
    """Delay (samples, fractional) of transfer function Ht relative to Hg.

    Phase-only correlation weighted by where this take actually has data.
    """
    m = (freqs > band[0]) & (freqs < band[1])
    C = np.zeros_like(Ht)
    C[m] = Ht[m] * np.conj(Hg[m])
    C[m] /= np.maximum(np.abs(C[m]), 1e-30)
    if weight is not None:
        C[m] *= weight[m] / max(weight[m].max(), 1e-30)
    n = 2 * (len(freqs) - 1)
    r = np.fft.irfft(C, n)
    L = int(max_ms * 1e-3 * fs)
    r = np.concatenate([r[-L:], r[:L + 1]])
    lags = np.arange(-L, L + 1)
    k = int(np.argmax(r))
    off = 0.0
    if 0 < k < len(r) - 1:
        y0, y1, y2 = r[k - 1], r[k], r[k + 1]
        den = y0 - 2 * y1 + y2
        off = 0.5 * (y0 - y2) / den if den != 0 else 0.0
    return float(lags[k] + off)


# ----------------------------------------------------------------------------
# Accumulator
# ----------------------------------------------------------------------------
@dataclass
class TakeContribution:
    name: str
    Sxx: np.ndarray
    Syy: np.ndarray
    Sxy: np.ndarray
    hits: np.ndarray
    n_seg: int
    meta: dict = field(default_factory=dict)
    dual: dict | None = None          # dual-piezo spectra (see Accumulator.dual)


class Accumulator:
    """Holds accumulated spectra for one mic (slot)."""

    def __init__(self, fs=48000, nfft=65536):
        self.fs = int(fs)
        self.nfft = int(nfft)
        self.hop = self.nfft // 2
        self.win = signal.get_window("hann", self.nfft)
        self.freqs = np.fft.rfftfreq(self.nfft, 1 / self.fs)
        nb = len(self.freqs)
        self.Sxx = np.zeros(nb)
        self.Syy = np.zeros(nb)
        self.Sxy = np.zeros(nb, complex)
        self.hits = np.zeros(len(BAND_CENTERS))
        self.n_seg = 0
        self.takes: list[TakeContribution] = []
        self.ref_delay = None          # samples, from first take
        self.session_ppm: list[float] = []   # good drift locks (two-device mode)
        self._band_idx = [np.where((self.freqs >= lo) & (self.freqs < hi))[0]
                          for lo, hi in zip(BAND_LO, BAND_HI)]
        # Dual piezo (two pickup elements recorded separately): the summed pickup x1+x2 is
        # the 'x' above (so everything single-input keeps working), plus the 2-input cross
        # spectra here. S12 = E[conj(X1) X2], S1y = E[conj(X1) Y] etc.
        self.dual = None

    def _new_dual(self):
        nb = len(self.freqs)
        return {"S11": np.zeros(nb), "S22": np.zeros(nb), "S12": np.zeros(nb, complex),
                "S1y": np.zeros(nb, complex), "S2y": np.zeros(nb, complex)}

    def has_dual(self):
        return self.dual is not None and float(np.sum(self.dual["S22"])) > 0

    # ---- state ----
    def empty(self):
        return self.n_seg == 0

    def H(self):
        return self.Sxy / np.maximum(self.Sxx, 1e-30)

    def coherence(self):
        return np.abs(self.Sxy) ** 2 / np.maximum(self.Sxx * self.Syy, 1e-30)

    def _add(self, c: TakeContribution, sign=1):
        self.Sxx += sign * c.Sxx
        self.Syy += sign * c.Syy
        self.Sxy += sign * c.Sxy
        self.hits += sign * c.hits
        self.n_seg += sign * c.n_seg
        if c.dual is not None:
            if self.dual is None:
                self.dual = self._new_dual()
            for k in self.dual:
                self.dual[k] = self.dual[k] + sign * c.dual[k]

    def undo_last(self):
        if not self.takes:
            return None
        c = self.takes.pop()
        self._add(c, -1)
        if not self.takes:
            self.ref_delay = None
        return c.name

    # ---- analysis of one take ----
    def process_take(self, x, y, name="take", two_device=False, x2=None):
        """x = piezo, y = mic, both float at self.fs. Returns report dict.

        x2 = a second piezo element recorded on its own input (same device as x). The
        summed pickup x + x2 is then used for everything single-input, and the two
        elements' cross spectra are kept for the dual-piezo IR pair."""
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        x1 = None
        if x2 is not None and len(x2):
            x2 = np.asarray(x2, float)
            m = min(len(x), len(x2))
            x1, x2 = x[:m], x2[:m]
            x = x1 + x2
        else:
            x2 = None
        report = {"name": name, "warnings": []}
        clip_x = np.abs(x) >= 0.995
        if x1 is not None:
            clip_x = (np.abs(x1) >= 0.995) | (np.abs(x2) >= 0.995)
        clip_y = np.abs(y) >= 0.995
        if two_device:
            prior = float(np.median(self.session_ppm)) if self.session_ppm else None
            x, y, info = align_two_device(x, y, self.fs, prior_ppm=prior)
            if x1 is not None:
                i0, i1 = info["trim"]
                x1, x2, clip_x = x1[i0:i1], x2[i0:i1], clip_x[i0:i1]
            info = {k: v for k, v in info.items() if k != "trim"}
            report.update(info)
            if not info["used_session_drift"] and info["lock_resid_samples"] < 2.0:
                self.session_ppm.append(info["drift_ppm"])
            if abs(info["drift_ppm"]) > 300:
                report["warnings"].append(
                    f"Clock drift {info['drift_ppm']:.0f} ppm is unusually high - "
                    "check both devices are set to the same sample rate.")
            if x1 is None:
                clip_x = np.abs(x) >= 0.995
            clip_y = np.abs(y) >= 0.995
        n = min(len(x), len(y))
        x, y = x[:n], y[:n]
        if x1 is not None:
            x1, x2 = x1[:n], x2[:n]
        if n < self.nfft:
            padn = self.nfft - n
            x = np.pad(x, (0, padn))
            y = np.pad(y, (0, padn))
            if x1 is not None:
                x1, x2 = np.pad(x1, (0, padn)), np.pad(x2, (0, padn))
            clip_x = np.pad(clip_x[:n], (0, padn))
            clip_y = np.pad(clip_y[:n], (0, padn))
            n = self.nfft
        starts = list(range(0, n - self.nfft + 1, self.hop))
        rms = []
        for s in starts:
            xs = x[s:s + self.nfft]
            rms.append(np.sqrt(np.mean(xs ** 2)))
        rms = np.array(rms)
        rms_db = 20 * np.log10(rms + 1e-12)
        top = rms_db.max() if len(rms_db) else -120
        n_clip = n_quiet = 0
        use = []                                   # segment start positions to analyse
        for s, r in zip(starts, rms_db):
            if clip_x[s:s + self.nfft].any() or clip_y[s:s + self.nfft].any():
                n_clip += 1
                continue
            if r < top - 35 or r < -75:
                n_quiet += 1
                continue
            use.append(s)
        report.update(segments_total=len(starts), rejected_clip=n_clip,
                      rejected_quiet=n_quiet)
        if n_clip:
            report["warnings"].append(
                f"{n_clip} segment(s) clipped and were discarded - lower the gain "
                "or play a little softer.")
        if np.max(np.abs(y)) < 10 ** (-70 / 20):
            report["accepted"] = 0
            report["warnings"].append("The mic signal is silent - check that this mic is on the "
                                      "right input and its gain is turned up.")
            return report
        if not use:
            report["accepted"] = 0
            report["warnings"].append("No usable audio in this take.")
            return report

        # Streaming passes (constant memory, any take length):
        # 1) provisional H including this take, 2) per-segment mismatch, 3) accumulate kept.
        def spec(s):
            return (np.fft.rfft(self.win * x[s:s + self.nfft]),
                    np.fft.rfft(self.win * y[s:s + self.nfft]))
        tSxx = np.zeros_like(self.Sxx)
        tSxy = np.zeros_like(self.Sxy)
        for s in use:
            X, Y = spec(s)
            tSxx += np.abs(X) ** 2
            tSxy += np.conj(X) * Y
        Href = (self.Sxy + tSxy) / np.maximum(self.Sxx + tSxx, 1e-30)
        band = (self.freqs > 80) & (self.freqs < min(10000, 0.45 * self.fs))
        errs = []
        for s in use:
            X, Y = spec(s)
            E = Y[band] - Href[band] * X[band]
            errs.append(db(np.sum(np.abs(E) ** 2) / max(np.sum(np.abs(Y[band]) ** 2), 1e-30)))
        errs = np.array(errs)
        lim = min(np.median(errs) + 9.0, 0.0)   # 0 dB: an omni mic in a room is still usable
        keep = errs <= lim
        if keep.sum() == 0:
            keep[:] = True
        n_out = int((~keep).sum())
        report["rejected_outlier"] = n_out
        if n_out:
            report["warnings"].append(
                f"{n_out} segment(s) didn't match the piezo (squeaks, bumps, room "
                "noise?) and were discarded.")

        Sxx = np.zeros_like(self.Sxx)
        Syy = np.zeros_like(self.Syy)
        Sxy = np.zeros_like(self.Sxy)
        hits = np.zeros(len(BAND_CENTERS))
        dual = self._new_dual() if x1 is not None else None
        for s, k in zip(use, keep):
            if not k:
                continue
            X, Y = spec(s)
            if dual is not None:
                X1 = np.fft.rfft(self.win * x1[s:s + self.nfft])
                X2 = X - X1                                  # linear: X = X1 + X2
                dual["S11"] += np.abs(X1) ** 2
                dual["S22"] += np.abs(X2) ** 2
                dual["S12"] += np.conj(X1) * X2
                dual["S1y"] += np.conj(X1) * Y
                dual["S2y"] += np.conj(X2) * Y
            px = np.abs(X) ** 2
            Sxx += px
            Syy += np.abs(Y) ** 2
            Sxy += np.conj(X) * Y
            be = np.array([px[i].mean() if len(i) else 0 for i in self._band_idx])
            hits += (db(be) > db(be.max()) - 45).astype(float)

        # residual fractional delay vs. existing estimate (two-device mode)
        if two_device and not self.empty():
            Hg = self.H()
            ph = lambda tau: np.exp(2j * np.pi * self.freqs / self.fs * tau)
            g_t = np.abs(Sxy) ** 2 / np.maximum(Sxx * Syy, 1e-30)
            wt = np.clip(g_t / (1 - np.clip(g_t, 0, 0.999)), 0, 50) * \
                np.clip(self.coherence() / (1 - np.clip(self.coherence(), 0, .999)), 0, 50)
            tau1 = ir_offset(Sxy / np.maximum(Sxx, 1e-30), Hg, self.freqs, self.fs, wt)
            Sxy = Sxy * ph(tau1)
            tau2 = self._phase_slope_delay(Sxy / np.maximum(Sxx, 1e-30), Hg, Sxx)
            Sxy = Sxy * ph(tau2)
            if dual is not None:
                dual["S1y"] = dual["S1y"] * ph(tau1 + tau2)
                dual["S2y"] = dual["S2y"] * ph(tau1 + tau2)
            report["fine_align_samples"] = tau1 + tau2

        Ht = Sxy / np.maximum(Sxx, 1e-30)
        ht = np.fft.irfft(Ht, self.nfft)
        pk = int(np.argmax(np.abs(ht[: int(0.05 * self.fs)])))
        report["delay_ms"] = pk / self.fs * 1e3
        report["polarity"] = "inverted" if ht[pk] < 0 else "normal"
        if not two_device:
            if self.ref_delay is None:
                self.ref_delay = pk
            elif abs(pk - self.ref_delay) > 0.25e-3 * self.fs:
                report["warnings"].append(
                    f"Mic-to-piezo delay changed by "
                    f"{(pk - self.ref_delay) / self.fs * 1e3:+.2f} ms "
                    "- the mic or guitar moved. Keep positions fixed for the session.")

        # level consistency (compression / something moved): compare only bands this
        # take excited strongly and where both estimates are reliable; warn when most
        # of them disagree, not when one sparse band does
        if not self.empty():
            Hg = self.H()
            g_take = np.abs(Sxy) ** 2 / np.maximum(Sxx * Syy, 1e-30)
            g_glob = self.coherence()
            band_e = np.array([Sxx[idx].mean() if len(idx) else 0 for idx in self._band_idx])
            devs = []
            for b, idx in enumerate(self._band_idx):
                if not len(idx) or BAND_CENTERS[b] < 100 or BAND_CENTERS[b] > 5000:
                    continue
                if band_e[b] < band_e.max() * 10 ** (-15 / 10) or self.hits[b] < 3:
                    continue
                if np.median(g_take[idx]) < 0.9 or np.median(g_glob[idx]) < 0.9:
                    continue
                gt = db(np.mean(np.abs(Ht[idx]) ** 2))
                gg = db(np.mean(np.abs(Hg[idx]) ** 2))
                devs.append(gt - gg)
            if len(devs) >= 4:
                devs = np.abs(np.array(devs))
                report["max_band_dev_db"] = float(np.max(devs))
                typical = float(np.percentile(devs, 75))
                report["typical_band_dev_db"] = typical
                if typical > 3.0:
                    report["warnings"].append(
                        f"This take's tone differs from the others by about {typical:.1f} dB in most "
                        "bands - did the mic or guitar move, or is something compressing at this volume?")

        c = TakeContribution(name, Sxx, Syy, Sxy, hits, int(keep.sum()),
                             {k: v for k, v in report.items() if k != "warnings"}, dual)
        self.takes.append(c)
        self._add(c)
        report["accepted"] = int(keep.sum())
        report["piezo_peak_db"] = float(20 * np.log10(np.max(np.abs(x)) + 1e-12))
        report["mic_peak_db"] = float(20 * np.log10(np.max(np.abs(y)) + 1e-12))
        return report

    def _phase_slope_delay(self, Ht, Hg, w):
        f = self.freqs
        m = (f > 300) & (f < min(6000, 0.4 * self.fs))
        ph = np.angle(Ht[m] * np.conj(Hg[m]))
        ww = w[m] * self.coherence()[m]
        # phase = -2*pi*f*tau/fs  -> weighted LS through origin
        om = 2 * np.pi * f[m] / self.fs
        tau = -np.sum(ww * om * ph) / max(np.sum(ww * om * om), 1e-30)
        return float(np.clip(tau, -4, 4))

    # ---- coverage ----
    def coverage(self, min_band=50.0):
        """Per band: dict(center, unc_db, hits, status)."""
        g = self.coherence()
        out = []
        for b, idx in enumerate(self._band_idx):
            hits = self.hits[b]
            if not len(idx) or BAND_CENTERS[b] > 0.45 * self.fs or BAND_CENTERS[b] < min_band - 1:
                continue
            w = self.Sxx[idx]
            gam = float(np.sum(g[idx] * w) / max(np.sum(w), 1e-30)) if w.sum() > 0 else 0
            gam = min(max(gam, 1e-4), 0.99999)
            if hits < MIN_HITS:
                unc, status = float("inf"), "need data"
            else:
                unc = 20 * math.log10(1 + math.sqrt((1 - gam) / (2 * hits * gam)))
                status = "good" if unc <= GOOD_DB else ("ok" if unc <= OK_DB else "weak")
            out.append({"center": float(BAND_CENTERS[b]), "unc_db": unc,
                        "hits": int(hits), "coh": gam, "status": status})
        return out

    def explained_db(self):
        """How much mic energy is NOT explained by the piezo (dB, lower=better)."""
        f = self.freqs
        m = (f > 80) & (f < min(10000, 0.45 * self.fs))
        g = self.coherence()[m]
        e = np.sum(self.Syy[m] * g) / max(np.sum(self.Syy[m]), 1e-30)
        return float(db(1 - e)), float(e)

    # ---- dual piezo ----
    def _bulk_delay(self):
        """Mic delay (samples) of the summed-pickup response, so it can be taken out before
        smoothing complex spectra (otherwise the phase rotation cancels them)."""
        h = np.fft.irfft(self.H(), self.nfft)
        return int(np.argmax(np.abs(h[: int(0.05 * self.fs)])))

    def _dual_smoothed(self, frac=1 / 24):
        d = self.dual
        rot = np.exp(2j * np.pi * self.freqs / self.fs * self._bulk_delay())
        f = self.freqs
        sm = lambda v: smooth_frac(v, f, frac)
        cs = lambda v: sm(v.real) + 1j * sm(v.imag)
        S1y, S2y = d["S1y"] * rot, d["S2y"] * rot
        Syy_ = sm(self.Syy)
        return sm(d["S11"]), sm(d["S22"]), cs(d["S12"]), cs(S1y), cs(S2y), Syy_

    def dual_H(self, rel_ridge=1e-3, frac=1 / 24, shrink=False):
        """Two-input least squares per frequency: the pair (H1, H2) that best turns
        element 1 + element 2 into the mic. A small ridge keeps it stable where the two
        elements say the same thing (then the result is shared evenly). Mic delay removed."""
        S11, S22, S12, S1y, S2y, Syy = self._dual_smoothed(frac)
        lam = rel_ridge * 0.5 * (S11 + S22) + 1e-30
        a, b, c, e = S11 + lam, S12, np.conj(S12), S22 + lam
        det = a * e - b * c
        det = np.where(np.abs(det) < 1e-300, 1e-300, det)
        H1 = (e * S1y - b * S2y) / det
        H2 = (a * S2y - c * S1y) / det
        if shrink:
            # where the data is thin (low multiple coherence), lean on the summed-pickup
            # answer H1 = H2 = Hs, which is what a normal single IR would do
            # and where the second element doesn't explain more of the mic than the summed
            # pickup does (elements saying the same thing), use the summed answer outright
            Sss = S11 + S22 + 2 * S12.real
            Hs = (S1y + S2y) / np.maximum(Sss, 1e-30)
            Yp = np.maximum(Syy, 1e-30)
            gd = np.clip(np.real(np.conj(S1y) * H1 + np.conj(S2y) * H2) / Yp, 0, 0.9999)
            gs = np.clip(np.abs(S1y + S2y) ** 2 / np.maximum(Sss, 1e-30) / Yp, 0, 0.9999)
            r = 1 - (1 - gd) / np.maximum(1 - gs, 1e-6)       # share of the leftover it explains
            r = smooth_frac(np.clip(r, 0, 1) * Syy, self.freqs, 1 / 3) / np.maximum(
                smooth_frac(Syy, self.freqs, 1 / 3), 1e-30)
            w = np.clip((r - 0.15) / 0.35, 0, 1)
            w *= np.clip(gd / (1 - gd) / 2.0, 0, 1) ** 2        # and only where the data is good
            H1 = w * H1 + (1 - w) * Hs
            H2 = w * H2 + (1 - w) * Hs
        return H1, H2

    def dual_gain(self):
        """Per band: how much more of the mic the two elements explain together than the
        summed pickup does. Returns list of dict(center, single, dual, gain_db) where
        single/dual are the unexplained fractions (lower = better). Computed per frequency
        bin from the raw averages and unbiased for the number of segments (an extra input
        always 'explains' a little noise: E[unexplained] = true * (n - inputs) / n)."""
        if not self.has_dual():
            return []
        d = self.dual
        S11, S22, S12, S1y, S2y = d["S11"], d["S22"], d["S12"], d["S1y"], d["S2y"]
        lam = 1e-9 * (S11 + S22) + 1e-300
        a, b, c, e = S11 + lam, S12, np.conj(S12), S22 + lam
        det = a * e - b * c
        det = np.where(np.abs(det) < 1e-300, 1e-300, det)
        H1 = (e * S1y - b * S2y) / det
        H2 = (a * S2y - c * S1y) / det
        Y = np.maximum(self.Syy, 1e-300)
        ud = np.clip(1 - np.real(np.conj(S1y) * H1 + np.conj(S2y) * H2) / Y, 0, 1)
        Sss = S11 + S22 + 2 * S12.real
        us = np.clip(1 - np.abs(S1y + S2y) ** 2 / np.maximum(Sss, 1e-300) / Y, 0, 1)
        n = max(self.n_seg, 3)
        us = us * n / (n - 1)
        ud = np.minimum(ud * n / (n - 2), us)
        out = []
        for bnd, idx in enumerate(self._band_idx):
            if not len(idx) or BAND_CENTERS[bnd] > 0.45 * self.fs or self.hits[bnd] < MIN_HITS:
                continue
            w = self.Syy[idx]
            if w.sum() <= 0:
                continue
            u1 = max(float(np.sum(us[idx] * w) / w.sum()), 1e-6)
            u2 = max(float(np.sum(ud[idx] * w) / w.sum()), 1e-6)
            out.append({"center": float(BAND_CENTERS[bnd]), "single": u1, "dual": u2,
                        "gain_db": float(10 * np.log10(u1 / u2))})
        return out

    # ---- persistence ----
    def to_dict(self, prefix):
        d = {f"{prefix}_Sxx": self.Sxx, f"{prefix}_Syy": self.Syy,
             f"{prefix}_Sxy": self.Sxy, f"{prefix}_hits": self.hits,
             f"{prefix}_meta": np.array(json.dumps({
                 "fs": self.fs, "nfft": self.nfft, "n_seg": self.n_seg,
                 "ref_delay": self.ref_delay,
                 "takes": [t.name for t in self.takes]}))}
        if self.dual is not None:
            for k, v in self.dual.items():
                d[f"{prefix}_dual_{k}"] = v
        return d

    @classmethod
    def from_dict(cls, d, prefix):
        meta = json.loads(str(d[f"{prefix}_meta"]))
        a = cls(meta["fs"], meta["nfft"])
        a.Sxx = d[f"{prefix}_Sxx"].copy()
        a.Syy = d[f"{prefix}_Syy"].copy()
        a.Sxy = d[f"{prefix}_Sxy"].copy()
        a.hits = d[f"{prefix}_hits"].copy()
        a.n_seg = meta["n_seg"]
        a.ref_delay = meta["ref_delay"]
        # previous takes are merged into one block (cannot be undone individually)
        if f"{prefix}_dual_S11" in d:
            a.dual = {k: d[f"{prefix}_dual_{k}"].copy() for k in ("S11", "S22", "S12", "S1y", "S2y")}
        if a.n_seg:
            a.takes = [TakeContribution("saved session", a.Sxx.copy(), a.Syy.copy(),
                                        a.Sxy.copy(), a.hits.copy(), a.n_seg,
                                        dual={k: v.copy() for k, v in a.dual.items()} if a.dual else None)]
        return a


# ----------------------------------------------------------------------------
# Suggestions
# ----------------------------------------------------------------------------
def suggestions(cov, max_items=4, instrument="guitar"):
    weak = [c for c in cov if c["status"] in ("weak", "need data")]
    weak.sort(key=lambda c: -c["unc_db"] if math.isfinite(c["unc_db"]) else -1e9)
    bass = instrument == "double bass"
    strings = BASS_STRINGS if bass else OPEN_STRINGS
    low_open = strings[0][1]
    # notes can be aimed at a band as long as some string can play that pitch: up to ~fret 19
    # on a guitar (high E fret 19 = 1245 Hz); above that, overtones fill bands, not single notes
    top_fund = 400 if bass else 1250
    max_fret = 12 if bass else 19
    tips = []
    seen = set()
    for c in weak:
        f = c["center"]
        if f < low_open * 0.97:
            key = "sub"
            if bass:
                tip = (f"Deep bass ~{f:.0f} Hz: let the open E ring long (and a low C/B "
                       "extension if you have one), and slap the body gently with an open hand.")
            else:
                tip = (f"Bass below the low E (~{f:.0f} Hz): let low E open and drop-D notes "
                       "ring, strum full open chords, and tap the lower bout with your palm.")
        elif f < top_fund:
            key = f"fund{int(f)}"
            opts = []
            for sname, fo in strings:
                fret = round(12 * math.log2(f / fo))
                if 0 <= fret <= max_fret:
                    where = ("open string" if fret == 0 else
                             (f"{fret} semitones up" if bass else f"fret {fret}"))
                    opts.append((abs(fret - 5), f"{sname} string ({where})"))
            opts = [o for _, o in sorted(opts)]               # easiest positions first
            tip = (f"~{f:.0f} Hz ({note_name(f)}): play {note_name(f)} single notes - "
                   f"{' or '.join(opts[:2]) or 'that pitch'} - a few times, letting them ring, "
                   f"plus the notes a semitone either side.")
        elif f < 1100:
            key = "mid"
            tip = (f"Mids ~{f:.0f} Hz: " + (
                "notes high on the G and D strings in thumb position, and strong pizz "
                "attacks." if bass else
                "single notes on the G, B and high-E strings between frets 5 and 15, "
                "plus chords up the neck."))
        elif f < 4500:
            key = "upper"
            tip = (f"Upper mids ~{f / 1000:.1f} kHz: " + (
                "firm pizz near the end of the fingerboard, string slaps and finger "
                "noise." if bass else
                "bright strums with a pick and single notes high on the neck."))
        else:
            key = "air"
            tip = (f"Top end ~{f / 1000:.1f} kHz: " + (
                "harmonics (octave and fifth) on every string and percussive slaps." if bass
                else "harmonics at frets 12/7/5, pick near the bridge, and muted "
                "percussive strums."))
        if key in seen:
            continue
        seen.add(key)
        tips.append(tip)
        if len(tips) >= max_items:
            break
    return tips


# ----------------------------------------------------------------------------
# Mic calibration file
# ----------------------------------------------------------------------------
def load_calibration(path):
    """Parse REW/miniDSP-style cal files: lines 'freq dB [phase]'."""
    fr, gd = [], []
    with open(path, "r", errors="ignore") as fh:
        for line in fh:
            line = line.strip().replace(",", " ").replace(";", " ")
            if not line or line[0] in "*\"#" or line[0].isalpha():
                continue
            parts = line.split()
            try:
                f = float(parts[0])
                g = float(parts[1])
            except (ValueError, IndexError):
                continue
            if f > 0:
                fr.append(f)
                gd.append(g)
    if len(fr) < 5:
        raise ValueError("Calibration file has no usable frequency/dB data.")
    o = np.argsort(fr)
    return np.array(fr)[o], np.array(gd)[o]


# ----------------------------------------------------------------------------
# Build master response
# ----------------------------------------------------------------------------
@dataclass
class BuildParams:
    smoothing: float = 1 / 12        # octave fraction, 0 = none
    reg_db: float = 70.0             # regularisation depth (higher = less)
    low_cut: float = 35.0            # Hz, 0 = off
    high_cut: float = 18000.0        # Hz, 0 = off
    phase: str = "minimum"           # "minimum" | "measured"
    estimator: str = "H1"            # "H1" | "H2" | "Hv"   (see _power_response)
    master_ms: float = 500.0
    blend_b: float = 0.0             # 0 = only mic A, 1 = only mic B
    # Frequency-split mixing: (low, mid, high, crossover1_hz, crossover2_hz) where each of
    # low/mid/high is how much of mic B to use in that band (0 = all A, 1 = all B).
    # None = use blend_b everywhere.
    mix_bands: tuple | None = None
    invert: bool = False
    # Finger-noise tamer: a broad, gentle dip where finger squeaks / string noise live.
    # tame_db = depth (0 = off), centred on tame_hz, tame_oct octaves wide.
    tame_db: float = 0.0
    tame_hz: float = 3500.0
    tame_oct: float = 1.5
    # Correction measured by a Pedal check: ((Hz, ...), (dB, ...)) added to the response.
    corr: tuple | None = None


def _power_response(acc: Accumulator, p: BuildParams, cal=None):
    f = acc.freqs
    S3 = smooth_frac(acc.Sxx, f, 1 / 3)
    lam = S3.max() * 10 ** (-p.reg_db / 10)
    Hraw = acc.Sxy / (acc.Sxx + lam)
    est = getattr(p, "estimator", "H1")
    if est == "H2":
        # H2 = Syy / Syx: unbiased by noise on the PIEZO side, biased up by mic noise
        # = H1 / coherence; coherence floored at 0.5 so bins with little signal can't explode
        g2 = np.clip(acc.coherence(), 0.5, 1.0)
        P = np.abs(Hraw) ** 2 / g2
    elif est == "Hv":
        # Hv = sqrt(H1 * H2): balanced when both signals carry noise (total least squares)
        P = acc.Syy / (acc.Sxx + lam)
    else:
        # H1 = Sxy / Sxx: unbiased by noise on the MIC side (room, mic hiss) - default
        P = np.abs(Hraw) ** 2
    # Each bin is weighted by how much trustworthy data it holds (piezo energy
    # x coherence SNR). Bins between harmonics, with almost no excitation,
    # barely count; well-excited bins define the curve.
    gam = np.clip(acc.coherence(), 0, 0.9999)
    snr = gam / (1 - gam)                  # per-bin signal-to-noise (linear)
    w = np.clip(snr / 2.0, 0, 1) ** 2      # only noise-dominated bins lose weight
    frac = p.smoothing if (isinstance(p.smoothing, (tuple, list)) or p.smoothing) else 1 / 48
    num = smooth_frac(P * w, f, frac)
    den = smooth_frac(w, f, frac)
    num3 = smooth_frac(P * w, f, 1 / 3)
    den3 = smooth_frac(w, f, 1 / 3)
    P_fine = num / np.maximum(den, 1e-300)
    P_wide = num3 / np.maximum(den3, 1e-300)
    # if a narrow band has almost no data, fall back to the 1/3-oct estimate
    trust = den / np.maximum(den + 1e-6 * den3.max(), 1e-300)
    P = trust * P_fine + (1 - trust) * P_wide
    if cal is not None:
        cf, cg = cal
        P = P / 10 ** (interp_logf(cf, cg, f) / 10)
    return P, Hraw


def correction_gain2(freqs, corr):
    """Power gain of a Pedal-check correction curve (log-frequency interpolation, 0 dB outside)."""
    if not corr:
        return np.ones(len(freqs))
    cf, cdb = np.asarray(corr[0], float), np.asarray(corr[1], float)
    lf = np.log2(np.maximum(freqs, 1.0))
    return 10 ** (np.interp(lf, np.log2(cf), cdb, left=0.0, right=0.0) / 10)


def pedal_check(x_pedal, y_mic, fs, two_device=False, cal=None, x_dry=None):
    """Compare what the pedal really puts out with the Reference Mic, recorded together.

    Returns the remaining difference per 1/6 octave (dB, mic minus pedal, level-matched at
    200 Hz-5 kHz), how much it can be trusted per point, a suggested correction, and the pedal
    output's level relative to the dry piezo (if x_dry, recorded at the same time, is given)."""
    acc = Accumulator(fs)
    rep = acc.process_take(np.asarray(x_pedal, float), np.asarray(y_mic, float), "pedal check",
                           two_device=two_device)
    if not rep.get("accepted"):
        raise ValueError("Couldn't match the pedal output to the mic - play a little longer and louder, "
                         "and check the Pedal output input is the pedal (not the dry piezo).")
    P, _ = _power_response(acc, BuildParams(smoothing=1 / 6), cal)
    grid = np.geomspace(40, min(16000, 0.45 * fs), 61)
    db = 10 * np.log10(np.maximum(np.interp(grid, acc.freqs, P), 1e-30))
    ref = (grid >= 200) & (grid <= 5000)
    db -= np.mean(db[ref])
    # trust: from the take's coverage (bands with little playing get little say)
    cov = acc.coverage()
    cc = np.array([c["center"] for c in cov])
    unc = np.array([c["unc_db"] if np.isfinite(c["unc_db"]) else 20.0 for c in cov])
    trust = np.clip((6.0 - np.interp(np.log2(grid), np.log2(cc), unc)) / 4.0, 0, 1)
    taper = np.clip((np.log2(grid / 50.0)) / 0.5, 0, 1) * np.clip(np.log2(12000.0 / grid) / 0.5, 0, 1)
    sugg = np.clip(db, -12, 12) * trust * taper
    sel = (grid >= 80) & (grid <= 10000) & (trust > 0.5)
    rms = float(np.sqrt(np.mean(db[sel] ** 2))) if np.any(sel) else float("nan")
    level = None
    if x_dry is not None and len(x_dry):
        r = lambda v: np.sqrt(np.mean(np.asarray(v, float) ** 2)) + 1e-12
        level = float(20 * np.log10(r(x_pedal) / r(x_dry)))
    return {"freqs": grid, "diff_db": db, "trust": trust, "correction_db": sugg, "rms_db": rms,
            "level_db": level, "report": rep}


def scale_to_dry(h, fs, dry_freqs, dry_power, fmin=40.0, fmax=12000.0, ceiling_db=-0.1):
    """Scale an IR so the piezo played through it is as loud as the dry piezo (power-weighted
    by the piezo's own spectrum). The file can't exceed its ceiling, so returns
    (h, shortfall_db): how much quieter than dry it still is (0 when it made it)."""
    h = np.asarray(h, float)
    M = int(2 ** int(np.ceil(np.log2(max(16384, 4 * len(h))))))
    fd = np.fft.rfftfreq(M, 1 / fs)
    R = np.abs(np.fft.rfft(h, M)) ** 2
    Sx = np.interp(fd, dry_freqs, dry_power, left=0.0, right=0.0)
    m = (fd >= fmin) & (fd <= min(fmax, 0.45 * fs))
    g = np.sqrt(np.sum(Sx[m]) / max(np.sum(Sx[m] * R[m]), 1e-30))
    h2 = h * g
    pk = np.max(np.abs(h2))
    ceil = 10 ** (ceiling_db / 20)
    short = 0.0
    if pk > ceil:
        short = float(20 * np.log10(pk / ceil))
        h2 *= ceil / pk
    return h2, short


def tamer_gain2(freqs, p):
    """Power gain of the finger-noise tamer: a smooth bell-shaped dip of tame_db at tame_hz,
    tame_oct octaves wide (between the points where it's half as deep). 1.0 everywhere when off."""
    depth = float(getattr(p, "tame_db", 0.0) or 0.0)
    if depth <= 0:
        return np.ones(len(freqs))
    fc = max(float(p.tame_hz), 100.0)
    sigma = max(float(p.tame_oct), 0.2) / 2.355            # half-depth width -> gaussian sigma
    d = np.log2(np.maximum(freqs, 1.0) / fc)
    return 10 ** (-depth * np.exp(-0.5 * (d / sigma) ** 2) / 10)


def band_weights(freqs, low, mid, high, x1, x2, width_oct=1 / 3):
    """Share of mic B at each frequency for a 3-band split with smooth crossovers."""
    lf = np.log2(np.maximum(freqs, 1e-3))
    s1 = 1 / (1 + np.exp(-(lf - np.log2(max(x1, 1))) / (width_oct / 2)))
    s2 = 1 / (1 + np.exp(-(lf - np.log2(max(x2, 1))) / (width_oct / 2)))
    return np.clip(low + (mid - low) * s1 + (high - mid) * s2, 0, 1)


def build_master(acc_a: Accumulator | None, acc_b: Accumulator | None,
                 p: BuildParams, cal_a=None, cal_b=None):
    """Return dict with master IR and target power spectrum."""
    have_a = acc_a is not None and not acc_a.empty()
    have_b = acc_b is not None and not acc_b.empty()
    if not (have_a or have_b):
        raise ValueError("No captured data to build from.")
    a0 = acc_a if have_a else acc_b
    fs, n, f = a0.fs, a0.nfft, a0.freqs
    if have_a and have_b and (acc_b.fs != fs or acc_b.nfft != n):
        raise ValueError("Both mics must be captured at the same sample rate.")
    # share of mic B at every frequency
    if p.mix_bands is not None:
        wB = band_weights(f, *p.mix_bands)
    else:
        wB = np.full(len(f), float(p.blend_b))
    if not have_b:
        wB[:] = 0.0
    if not have_a:
        wB[:] = 1.0
    ref = (f > 200) & (f < 5000)
    warnings = []
    parts = {}
    for key, acc, cal in (("A", acc_a, cal_a), ("B", acc_b, cal_b)):
        if acc is None or acc.empty():
            continue
        Pi, Hraw = _power_response(acc, p, cal)
        lvl = float(np.mean(Pi[ref]))
        if not np.all(np.isfinite(Pi)) or not np.isfinite(lvl) or lvl <= 1e-20:
            warnings.append("One mic has no usable signal (silent or wrong input?) and was left out.")
            continue
        parts[key] = (Pi / lvl, Hraw)                 # level-matched
    if not parts:
        raise ValueError("No usable mic signal in the captured takes - check that the mic "
                         "inputs are right and their gain is up, then record again.")
    if "A" not in parts:
        wB[:] = 1.0
    if "B" not in parts:
        wB[:] = 0.0
    P = np.zeros(len(f))
    if "A" in parts:
        P += (1 - wB) * parts["A"][0]
    if "B" in parts:
        P += wB * parts["B"][0]
    main = "B" if np.mean(wB[ref]) > 0.5 and "B" in parts else ("A" if "A" in parts else "B")
    Hphase = parts[main][1]
    hr = np.fft.irfft(Hphase, n)
    pk = int(np.argmax(np.abs(hr[: int(0.05 * fs)])))
    sign = 1.0 if hr[pk] >= 0 else -1.0
    accs = [k for k in parts if (k == "A" and np.max(1 - wB) > 0.01) or (k == "B" and np.max(wB) > 0.01)]
    if p.low_cut > 0:
        P *= butter_mag2(f, p.low_cut, 2, "hp")
    hc = p.high_cut if p.high_cut > 0 else 0.48 * fs
    hc = min(hc, 0.47 * fs)
    P *= butter_mag2(f, hc, 4, "lp")
    P *= tamer_gain2(f, p)
    P *= correction_gain2(f, p.corr)

    phase_mode = p.phase
    if len(accs) > 1:
        phase_mode = "minimum"     # blending two mics: use min phase (no comb filtering)
    if phase_mode == "minimum":
        H = minphase_from_power(P, n)
        h = np.fft.irfft(H, n) * sign
        start = 0
    else:
        H = np.sqrt(P) * np.exp(1j * np.angle(Hphase))
        h = np.fft.irfft(H, n)
        pk = int(np.argmax(np.abs(h[: int(0.05 * fs)])))
        thr = np.abs(h[pk]) * 10 ** (-40 / 20)
        first = int(np.argmax(np.abs(h[:pk + 1]) > thr))
        start = max(0, first - int(0.0005 * fs))
        h = np.roll(h, -start)
    if p.invert:
        h = -h
        sign = -sign
    L = min(n, int(p.master_ms * 1e-3 * fs))
    h = h[:L] * half_hann_tail(L, 0.2)
    if not np.all(np.isfinite(h)) or np.max(np.abs(h)) == 0:
        raise ValueError("The IR came out empty - the captured data looks invalid. Try "
                         "'Undo last take' on any take with warnings, or record again.")
    h = h / (np.max(np.abs(h)) + 1e-20) * 10 ** (-1 / 20)
    return {"h": h, "fs": fs, "P": P, "freqs": f, "nfft": n,
            "sign": -sign if p.invert else sign, "phase": phase_mode,
            "removed_predelay_ms": start / fs * 1e3, "warnings": warnings}


def build_dual(acc_a, acc_b, p: BuildParams, single_master, cal_a=None, cal_b=None, lead_ms=0.5):
    """IR pair for a dual piezo: h1 for element 1, h2 for element 2, so that
    h1*x1 + h2*x2 sounds like the mic(s). Built from the mic with the most weight in the
    chosen mix; the tone of the chosen mix (single_master['P'] vs that mic's own response)
    is applied to both as a smooth zero-phase EQ, so 'Blended' / cuts / smoothing choices
    carry over. Phase between the two paths is kept (it's what makes the pair work)."""
    have = {k: a for k, a in (("A", acc_a), ("B", acc_b)) if a is not None and a.has_dual()}
    if not have:
        raise ValueError("No dual-piezo takes yet - turn on Instrument 2 and record some takes.")
    if p.mix_bands is not None:
        wB = float(np.mean(band_weights(np.array([300.0, 1000.0, 3000.0]), *p.mix_bands)))
    else:
        wB = float(p.blend_b)
    main = "B" if (wB > 0.5 and "B" in have) or "A" not in have else "A"
    acc = have[main]
    cal = cal_b if main == "B" else cal_a
    f, fs, n = acc.freqs, acc.fs, acc.nfft
    H1, H2 = acc.dual_H(rel_ridge=1e-3, shrink=True)
    # tone: target power (chosen mix) vs this mic's own single-input response
    q = BuildParams(**{**p.__dict__, "mix_bands": None, "blend_b": 1.0 if main == "B" else 0.0,
                       "low_cut": 0.0, "high_cut": 0.0})
    P_main, _ = _power_response(acc, q, cal)
    P_tgt = np.interp(f, single_master["freqs"], single_master["P"]) if len(single_master["freqs"]) != len(f) \
        else single_master["P"]
    ref = (f > 200) & (f < 5000)
    G2 = (P_tgt / max(np.mean(P_tgt[ref]), 1e-30)) / np.maximum(P_main / max(np.mean(P_main[ref]), 1e-30), 1e-30)
    G = np.sqrt(np.clip(smooth_frac(G2, f, 1 / 6), 1e-4, 1e4))
    if p.low_cut <= 0:                     # cuts were applied in single_master already; when off,
        G *= np.sqrt(butter_mag2(f, 20.0, 2, "hp"))   # still keep junk below 20 Hz out
    H1, H2 = H1 * G, H2 * G
    # put the mic delay back, then trim it the way 'measured' phase does: start just
    # before the first arrival (common to both paths, so their timing is kept)
    back = np.exp(-2j * np.pi * f / fs * acc._bulk_delay())
    h1 = np.fft.irfft(H1 * back, n)
    h2 = np.fft.irfft(H2 * back, n)
    env = np.abs(h1) + np.abs(h2)
    pk = int(np.argmax(env[: int(0.05 * fs)]))
    first = int(np.argmax(env[:pk + 1] > env[pk] * 10 ** (-40 / 20)))
    start = max(0, first - int(lead_ms * 1e-3 * fs))
    h1, h2 = np.roll(h1, -start), np.roll(h2, -start)
    if p.invert:
        h1, h2 = -h1, -h2
    L = min(n, int(p.master_ms * 1e-3 * fs))
    w = half_hann_tail(L, 0.2)
    h1, h2 = h1[:L] * w, h2[:L] * w
    if not (np.all(np.isfinite(h1)) and np.all(np.isfinite(h2))):
        raise ValueError("The dual-piezo IR pair came out invalid - record a few more takes.")
    k = 10 ** (-1 / 20) / max(np.max(np.abs(h1)), np.max(np.abs(h2)), 1e-20)
    return {"h1": h1 * k, "h2": h2 * k, "fs": fs, "mic": main, "gain": acc.dual_gain()}


def fit_pair(pair, fs_dst, N, fade=0.1):
    """Cut an IR pair to a target length (plain trim + tail fade: a minimum-phase redesign
    would break the timing between the two paths). Both get the same scaling."""
    out = {}
    for k in ("h1", "h2"):
        h = resample_to(pair[k], pair["fs"], fs_dst)
        NN = min(int(N), len(h))
        out[k] = h[:NN] * half_hann_tail(NN, fade)
    s = 10 ** (-1 / 20) / max(np.max(np.abs(out["h1"])), np.max(np.abs(out["h2"])), 1e-20)
    return {"h1": out["h1"] * s, "h2": out["h2"] * s, "fs": fs_dst, "N": len(out["h1"])}


# ----------------------------------------------------------------------------
# Optimal fit for a pedal's IR length / sample rate
# ----------------------------------------------------------------------------
def _err_db(R, T, f, fmin, fmax):
    pts = np.geomspace(fmin, fmax, 240)
    r = np.interp(pts, f, db(R))
    t = np.interp(pts, f, db(T))
    d = r - t
    d -= np.mean(d)                 # level is normalised separately
    return float(np.sqrt(np.mean(d ** 2))), d


def fit_for_pedal(master, fs_dst, N, normalize="peak", ref_level=None, fmin=60.0):
    """Design the best N-sample IR at fs_dst for a pedal.

    Candidates (all compared against the resolution-limited target):
      * the master IR itself, resampled and cut with several tail fades
      * an iterative minimum-phase redesign that pre-compensates the target
        for the smearing a short window causes (a big win for 128-1024 taps)
    The candidate with the lowest error wins, so the result is never worse
    than a plain truncation.
    """
    fs = master["fs"]
    h_src = resample_to(master["h"], fs, fs_dst)
    N_max = int(N)
    N = min(int(N), len(h_src))        # never pad: a shorter IR is always accepted
    M = int(2 ** int(np.ceil(np.log2(max(16384, 4 * N, len(h_src))))))
    fd = np.fft.rfftfreq(M, 1 / fs_dst)
    P0 = np.abs(np.fft.rfft(h_src, M)) ** 2
    res = fs_dst / N
    T_full = smooth_frac(P0, fd, 1 / 12)                    # what the master sounds like
    T = smooth_frac(P0, fd, 1 / 12, min_hz=1.5 * res)       # best a length-N IR can do
    fmax = min(12000.0, 0.45 * fs_dst)
    band = (fd > 25) & (fd < 0.46 * fs_dst)

    def score(h):
        """Error vs the real (full-length) master response; also returns the
        resolution-limited smoothed response that drives the redesign."""
        R = np.abs(np.fft.rfft(h, M)) ** 2
        Rs = smooth_frac(R, fd, 1 / 12, min_hz=1.5 * res)
        e = _err_db(smooth_frac(R, fd, 1 / 12), T_full, fd, fmin, fmax)[0]
        return e, Rs

    cands = []
    for fade in (0.0, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75):
        w = half_hann_tail(N, fade) if fade > 0 else np.ones(N)
        hc = np.zeros(N)
        hc[:min(N, len(h_src))] = h_src[:N]
        hc = hc * w
        e, _ = score(hc)
        cands.append((e, hc, fade, "trim"))
        if fade == 0.0 or N > 16384:   # long IRs: trimming is already near-perfect
            continue
        # iterative min-phase redesign
        Q = P0.copy()
        best_local = None
        for _ in range(12):
            hq = np.fft.irfft(minphase_from_power(Q, M), M)[:N] * w * master["sign"]
            e, Rs = score(hq)
            if best_local is None or e < best_local[0]:
                best_local = (e, hq.copy())
            ratio = np.ones_like(Q)
            ratio[band] = T[band] / np.maximum(Rs[band], 1e-30)
            ratio /= np.exp(np.mean(np.log(ratio[band])))
            Q = Q * np.clip(ratio, 0.5, 2.0) ** 0.8
            Q = np.clip(Q, P0 * 10 ** (-2.4), P0 * 10 ** 2.4)
        cands.append((best_local[0], best_local[1], fade, "fit"))
    err, h, fade, method = min(cands, key=lambda c: c[0])
    R_ = np.abs(np.fft.rfft(h, M)) ** 2
    err_ach = _err_db(smooth_frac(R_, fd, 1 / 12, min_hz=1.5 * res), T, fd, fmin, fmax)[0]

    e_tot = np.sum(h_src ** 2)
    e_ret = float(np.sum(h_src[:N] ** 2) / max(e_tot, 1e-30))
    R = np.abs(np.fft.rfft(h, M)) ** 2
    err_full, _ = _err_db(smooth_frac(R, fd, 1 / 12), T_full, fd, fmin, fmax)
    if normalize == "loudness" and ref_level is not None:
        m = (fd > 200) & (fd < 4000)
        h = h * np.sqrt(ref_level / max(np.mean(R[m]), 1e-30))
        pk = np.max(np.abs(h))
        if pk > 10 ** (-0.3 / 20):
            h *= 10 ** (-0.3 / 20) / pk
    else:
        h = h / (np.max(np.abs(h)) + 1e-20) * 10 ** (-1 / 20)
    R_final = np.abs(np.fft.rfft(h, M)) ** 2
    return {"h": h, "fs": fs_dst, "N": N, "N_max": N_max, "fade": fade, "method": method,
            "fit_err_db": err_ach, "err_vs_full_db": err_full,
            "energy_retained": e_ret, "freq_res_hz": res,
            "freqs": fd, "R": R_final, "T": T_full}


def master_from_wav(path, low_ms=None):
    """Load an existing IR WAV as a 'master' so it can be re-fitted/exported."""
    import soundfile as sf
    a, fs = sf.read(path, always_2d=True)
    h = np.asarray(a[:, 0], float)
    nz = np.nonzero(np.abs(h) > 1e-7 * np.max(np.abs(h)))[0]
    h = h[: nz[-1] + 1] if len(nz) else h
    n = int(2 ** int(np.ceil(np.log2(max(65536, 2 * len(h))))))
    P = np.abs(np.fft.rfft(h, n)) ** 2
    pk = int(np.argmax(np.abs(h[: max(1, int(0.05 * fs))])))
    return {"h": h / (np.max(np.abs(h)) + 1e-20) * 10 ** (-1 / 20), "fs": int(fs), "P": P,
            "freqs": np.fft.rfftfreq(n, 1 / fs), "nfft": n,
            "sign": 1.0 if h[pk] >= 0 else -1.0, "phase": "from file",
            "removed_predelay_ms": 0.0}


def write_c_header(path, h, fs, name):
    """C header for embedding an IR in firmware (e.g. Daisy Seed)."""
    ident = "".join(c if c.isalnum() else "_" for c in name).strip("_").lower() or "ir"
    if ident[0].isdigit():
        ident = "ir_" + ident
    h = np.asarray(h, np.float32)
    lines = [
        "// Generated by Acoustic IR Studio",
        f"// {len(h)} samples @ {fs} Hz ({len(h) / fs * 1000:.1f} ms), mono, float32, peak -1 dBFS",
        "// Long IRs are big (4 bytes/sample): on Daisy Seed build with the bootloader",
        "// (APP_TYPE = BOOT_QSPI or BOOT_SRAM) or load the .bin/.wav from SD card instead.",
        "#pragma once",
        "#include <stddef.h>",
        f"#define {ident.upper()}_LENGTH {len(h)}",
        f"#define {ident.upper()}_SAMPLERATE {int(fs)}",
        f"static const float {ident}[{len(h)}] = {{",
    ]
    for i in range(0, len(h), 8):
        lines.append("    " + ", ".join(f"{v:.9e}f" for v in h[i:i + 8]) + ",")
    lines.append("};")
    def w(tmp):
        with open(tmp, "w") as fh:
            fh.write("\n".join(lines) + "\n")
    _atomic(path, w)
    return ident


def write_bin(path, h):
    """Raw little-endian float32 samples (easy to load from SD/QSPI on a microcontroller)."""
    _atomic(path, lambda tmp: np.asarray(h, "<f4").tofile(tmp))


def loudness_ref_at(master, fs_dst):
    """Mean 200-4k power of the master IR resampled to fs_dst (for matched loudness)."""
    h = resample_to(master["h"], master["fs"], fs_dst)
    M = int(2 ** int(np.ceil(np.log2(max(16384, len(h))))))
    fd = np.fft.rfftfreq(M, 1 / fs_dst)
    R = np.abs(np.fft.rfft(h, M)) ** 2
    m = (fd > 200) & (fd < 4000)
    return float(np.mean(R[m]))


def loudness_ref(master, fs_dst=48000):
    f = master["freqs"]
    m = (f > 200) & (f < 4000)
    h = master["h"]
    R = np.abs(np.fft.rfft(h, master["nfft"])) ** 2
    return float(np.mean(R[m]))


def _atomic(path, write):
    """Write via a temporary file + rename, so a crash never leaves a half-written file."""
    root, ext = os.path.splitext(str(path))
    tmp = f"{root}.tmp{os.getpid()}{ext}"
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_wav(path, h, fs, bits=24):
    import soundfile as sf
    sub = {16: "PCM_16", 24: "PCM_24", 32: "FLOAT"}[bits]
    _atomic(path, lambda tmp: sf.write(tmp, np.asarray(h, np.float64), int(fs), subtype=sub, format="WAV"))


# ----------------------------------------------------------------------------
# Guided session definition
# ----------------------------------------------------------------------------
# (name, seconds or None = no limit, instructions).  None = you press Stop.
GUITAR_STEPS = [
    ("Medium strums", 20,
     "Strum open chords (G, C, D, Em, A) at your normal volume. Let the last chord ring."),
    ("Soft fingerpicking", 20,
     "Fingerpick chords softly - arpeggios across all six strings."),
    ("Hard strums", 20,
     "Strum hard, as loud as you'd ever play live. Watch the meters: no red."),
    ("Low strings, single notes", 30,
     "Single notes on the low E, A and D strings, frets 0 to 7. Let each ring ~2 s."),
    ("High strings, single notes", 30,
     "Single notes on the G, B and high-E strings, frets 0 to 12. Let each ring."),
    ("Highest notes - slow", 30,
     "The highest notes on the guitar: B and high-E strings, frets 12 up to the top of the "
     "neck. One note at a time, slowly, letting each ring."),
    ("Highest notes - medium & fast", 30,
     "Same top-of-the-neck area: first a steady medium tempo, then fast runs, trills and "
     "repeated notes."),
    ("Pick close to the bridge", 20,
     "Pick (or pluck) right next to the bridge - bright and snappy. Single notes and chords "
     "all over the neck."),
    ("Pick far from the bridge", 20,
     "Now pick well away from the bridge - over the end of the fingerboard / neck joint - "
     "warm and round. Single notes and chords."),
    ("Low end, held", 20,
     "Low E open, then (if you use it) drop-D low D, then A open. Let each ring about 6 s."),
    ("Harmonics", 20,
     "Natural harmonics at frets 12, 7 and 5 on every string."),
    ("Percussive", 20,
     "Palm-muted strums, muted string slaps and light taps on the top/lower bout."),
    ("Barre chords up the neck", 20,
     "Barre chords at frets 3, 5, 7 and 10, medium strumming."),
    ("Free play (required)", None,
     "Play anything you think was missed or that you play a lot - songs, riffs, your style. "
     "As long as you like. Check 'What to play next' for gaps, then press Stop when done."),
]

BASS_STEPS = [
    ("Medium pizz, walking line", 45,
     "Pizzicato walking line across all four strings at normal volume. Let notes sustain."),
    ("Soft pizz", 40,
     "Soft, gentle pizzicato over the whole range - like a ballad."),
    ("Hard pizz", 30,
     "Dig in as hard as you play live. Watch the meters: no red."),
    ("Low strings, single notes", 60,
     "Single notes on the E and A strings, open to an octave up. Let each ring ~3 s."),
    ("High strings, single notes", 45,
     "Single notes on the D and G strings, open up into thumb position. Let each ring."),
    ("Highest notes - slow", 45,
     "Thumb position, the highest notes you play on the G and D strings. Slowly, letting "
     "each ring."),
    ("Highest notes - medium & fast", 40,
     "Same high register: a steady medium tempo, then faster lines."),
    ("Pluck close to the bridge", 40,
     "Pluck near the end of the fingerboard / towards the bridge - tight and punchy."),
    ("Pluck far from the bridge", 40,
     "Pluck well up the fingerboard - big and round."),
    ("Low end, held", 40,
     "Open E (and low C/B extension if fitted), then open A. Let each ring ~8 s."),
    ("Harmonics", 30,
     "Octave and fifth harmonics on every string."),
    ("Percussive", 30,
     "Slaps, muted pizz, and gentle open-hand taps on the top."),
    ("Arco (optional)", 40,
     "If you bow: long bowed notes on each string. Skip if you only play pizz."),
    ("Free play (required)", None,
     "Play anything you think was missed - lines, grooves, your style. As long as you like. "
     "Check 'What to play next' for gaps, then press Stop when done."),
]

# Quick sessions: the same coverage with the overlapping steps merged (~6 min + free play).
GUITAR_QUICK = [
    ("Strums, soft to hard", 30,
     "Open and barre chords. Start soft, get gradually louder, finish as hard as you play live."),
    ("Low strings + held low notes", 30,
     "Single notes on the low E, A and D strings (frets 0-7), then let low E / drop-D / A ring ~6 s."),
    ("High strings, single notes", 25,
     "Single notes on the G, B and high-E strings, frets 0 to 12. Let each ring."),
    ("Highest notes, slow to fast", 30,
     "Top of the neck on the B and high-E strings: slow ringing notes, then medium, then fast runs."),
    ("Pick close to the bridge", 20,
     "Pick right next to the bridge - bright and snappy. Notes and chords."),
    ("Pick far from the bridge", 20,
     "Pick well away from the bridge, over the end of the fingerboard - warm and round."),
    ("Harmonics + percussive", 25,
     "Harmonics at frets 12/7/5 on every string, then muted strums, slaps and light body taps."),
    ("Fingerpicking", 20,
     "Fingerpicked arpeggios across all six strings, soft to medium."),
    ("Free play (required)", None,
     "Play anything you think was missed or that you play a lot. As long as you like. "
     "Check 'What to play next' for gaps, then press Stop when done."),
]
BASS_QUICK = [
    ("Pizz, soft to hard", 60,
     "Walking line over all strings. Start soft, get louder, finish as hard as you play live."),
    ("Low strings + held low notes", 60,
     "Single notes on the E and A strings, then let open E (and extension) and A ring ~8 s."),
    ("High strings, single notes", 45,
     "D and G strings, open up into thumb position. Let each ring."),
    ("Highest notes, slow to fast", 45,
     "Thumb position: slow ringing notes, then faster lines."),
    ("Pluck close to the bridge", 35, "Pluck towards the bridge - tight and punchy."),
    ("Pluck far from the bridge", 35, "Pluck well up the fingerboard - big and round."),
    ("Harmonics + percussive", 40, "Octave and fifth harmonics, then slaps and gentle body taps."),
    ("Free play (required)", None,
     "Play anything you think was missed. As long as you like. Press Stop when done."),
]

INSTRUMENTS = {
    "acoustic guitar": {"steps": GUITAR_STEPS, "quick": GUITAR_QUICK, "low_cut": 35.0, "min_band": 50.0,
                        "fmin": 60.0, "nfft48": 65536, "master_ms": 1000},
    "double bass": {"steps": BASS_STEPS, "quick": BASS_QUICK, "low_cut": 22.0, "min_band": 31.5,
                    "fmin": 35.0, "nfft48": 131072, "master_ms": 1000},
}
GUIDED_STEPS = GUITAR_STEPS
