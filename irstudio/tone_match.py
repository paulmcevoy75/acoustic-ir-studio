# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""
Tone matching: make an IR that turns your piezo into a reference sound, using
sustained single notes (the reference and the piezo don't have to be recorded
at the same time).

A sustained note only contains energy at its harmonics, so we measure the
level of each harmonic in the reference and in the piezo playing the same
note. The ratio at each harmonic is one point on the correction curve; several
notes give many points. A smooth curve through all points (weighted by how far
each harmonic sits above the noise) becomes the IR's magnitude response, built
as minimum phase.

"Refine" mode: run the piezo through your measured piezo->mic IR first and
only correct what is still different. That keeps the fine body-resonance
detail of the measurement and borrows the overall tone of the reference.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import signal

from . import engine as E

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(f):
    if not f or f <= 0:
        return "?"
    n = int(round(12 * math.log2(f / 440.0))) + 57
    cents = 1200 * math.log2(f / (440.0 * 2 ** ((n - 57) / 12)))
    return f"{NOTE_NAMES[n % 12]}{n // 12} ({f:.1f} Hz, {cents:+.0f} c)"


# ----------------------------------------------------------------------------
# Pitch
# ----------------------------------------------------------------------------
def detect_f0(x, fs, fmin=30.0, fmax=1200.0):
    """YIN pitch over several frames; returns median f0 (Hz) or None."""
    x = np.asarray(x, float)
    tau_max = int(fs / fmin)
    tau_min = max(2, int(fs / fmax))
    W = int(2 ** math.ceil(math.log2(2.5 * tau_max)))
    if len(x) < W + tau_max:
        W = max(256, len(x) - tau_max - 1)
        if W < 256:
            return None
    hop = W // 2
    f0s = []
    for s in range(0, len(x) - W - tau_max, hop):
        fr = x[s:s + W + tau_max]
        if np.sqrt(np.mean(fr ** 2)) < 1e-5:
            continue
        a = fr[:W]
        n = 1 << int(math.ceil(math.log2(len(fr) + W)))
        r = np.fft.irfft(np.fft.rfft(fr, n) * np.conj(np.fft.rfft(a, n)), n)[:tau_max + 1]
        e = np.concatenate([[0.0], np.cumsum(fr ** 2)])
        energy_a = e[W]
        energy_b = e[np.arange(tau_max + 1) + W] - e[np.arange(tau_max + 1)]
        d = energy_a + energy_b - 2 * r
        d[0] = 0
        cm = np.cumsum(d[1:])
        cmndf = np.ones_like(d)
        cmndf[1:] = d[1:] * np.arange(1, len(d)) / np.maximum(cm, 1e-20)
        cand = np.where(cmndf[tau_min:] < 0.15)[0]
        if len(cand):
            t = cand[0] + tau_min
            while t + 1 < len(cmndf) and cmndf[t + 1] < cmndf[t]:
                t += 1
        else:
            t = int(np.argmin(cmndf[tau_min:])) + tau_min
            if cmndf[t] > 0.35:
                continue
        if 1 <= t < len(cmndf) - 1:
            y0, y1, y2 = cmndf[t - 1], cmndf[t], cmndf[t + 1]
            den = y0 - 2 * y1 + y2
            t = t + (0.5 * (y0 - y2) / den if den else 0.0)
        f0s.append(fs / t)
    if not f0s:
        return None
    return float(np.median(f0s))


# ----------------------------------------------------------------------------
# Harmonics
# ----------------------------------------------------------------------------
def harmonics(x, fs, f0=None, fmax=16000.0, min_snr_db=10.0):
    """Measure harmonic levels of a sustained note.

    x may be one array or a list of segments (e.g. several plucks of the same
    note); their spectra are averaged without stitching the audio together.
    Returns dict(f0, freqs, power, snr_db) for each harmonic found.
    Handles string inharmonicity by tracking each partial's actual position.
    """
    segs = [np.asarray(v, float) for v in (x if isinstance(x, (list, tuple)) else [x])]
    if f0 is None:
        f0 = detect_f0(max(segs, key=len), fs)
    if not f0:
        raise ValueError("Couldn't find a clear pitch - select a single sustained note.")
    nper = int(2 ** math.ceil(math.log2(fs * 12.0 / f0)))
    longest = max(len(v) for v in segs)
    nper = max(1024, min(nper, 2 ** int(math.log2(max(longest, 1024)))))
    P = None
    wsum = 0.0
    for v in segs:
        if len(v) < nper:
            v = np.pad(v, (0, nper - len(v)))
        fr, Pi = signal.welch(v, fs, window="hann", nperseg=nper, noverlap=nper // 2,
                              detrend=False, scaling="spectrum")
        w = len(v)
        P = Pi * w if P is None else P + Pi * w
        wsum += w
    P = P / wsum
    df = fr[1] - fr[0]
    fmax = min(fmax, 0.45 * fs)
    out_f, out_p, out_s = [], [], []
    ratio = 1.0
    misses = 0
    k = 1
    while True:
        pred = k * f0 * ratio
        if pred > fmax:
            break
        lo = pred - 0.3 * f0
        hi = pred + 0.3 * f0
        i0 = max(1, int(lo / df))
        i1 = min(len(P) - 2, int(math.ceil(hi / df)))
        if i1 - i0 < 3:
            break
        seg = P[i0:i1 + 1]
        j = int(np.argmax(seg)) + i0
        # parabolic peak frequency (on log power)
        a, b, c = np.log(P[j - 1] + 1e-30), np.log(P[j] + 1e-30), np.log(P[j + 1] + 1e-30)
        den = a - 2 * b + c
        off = 0.5 * (a - c) / den if den else 0.0
        fpk = (j + off) * df
        pk = float(np.sum(P[max(0, j - 2):j + 3]))          # main-lobe power
        mask = np.ones(i1 - i0 + 1, bool)
        mask[max(0, j - i0 - 3):j - i0 + 4] = False
        noise = float(np.median(seg[mask])) * 5 if mask.any() else 1e-30
        snr = 10 * math.log10(max(pk, 1e-30) / max(noise, 1e-30))
        if snr >= min_snr_db:
            out_f.append(fpk)
            out_p.append(pk)
            out_s.append(snr)
            ratio = fpk / (k * f0)
            misses = 0
        else:
            misses += 1
            if misses >= 8:
                break
        k += 1
    if len(out_f) < 3:
        raise ValueError("Too few harmonics found - pick a louder, steadier part of the note.")
    return {"f0": f0, "freqs": np.array(out_f), "power": np.array(out_p),
            "snr_db": np.array(out_s)}


# ----------------------------------------------------------------------------
# Selection helpers
# ----------------------------------------------------------------------------
def _env_db(x, fs, hop_s=0.01):
    hop = max(1, int(hop_s * fs))
    n = len(x) // hop
    env = np.sqrt(np.mean(x[:n * hop].reshape(n, hop) ** 2, axis=1)) + 1e-9
    return 20 * np.log10(env), hop


def _onsets(edb, thresh=6.0, x=None, fs=None, hop=None):
    """Pluck onsets (indices into the 10 ms envelope).

    Uses the high-frequency 'click' of each attack when audio is given: on a
    mic'd guitar a re-pluck of a still-ringing note barely raises the overall
    level, but its attack is clearly visible above ~2 kHz.
    """
    if x is not None:
        sos = signal.butter(2, min(2000.0, 0.4 * fs), "highpass", fs=fs, output="sos")
        hf = signal.sosfilt(sos, x)
        n = len(edb)
        e = np.sqrt(np.mean(hf[:n * hop].reshape(n, hop) ** 2, axis=1)) + 1e-9
        hdb = 20 * np.log10(e)
        top = hdb.max()
        on = []
        for k in range(n):
            base = hdb[max(0, k - 6):k].min() if k else -200.0
            if hdb[k] - base > max(thresh, 9.0) and hdb[k] > top - 35:
                if not on or k - on[-1] > 15:          # >150 ms apart
                    on.append(k)
        if len(edb) and hdb[:5].max() > top - 25 and (not on or on[0] > 5):
            on.insert(0, 0)                           # note already sounding at the start
        return np.array(on, int)
    rise = np.diff(edb, prepend=edb[0])
    on = np.where(rise > thresh)[0]
    keep = []
    for o in on:                      # merge onsets closer than 100 ms
        if not keep or o - keep[-1] > 10:
            keep.append(o)
    return np.array(keep, int)


def auto_select(x, fs, max_len_s=20.0):
    """Select the span containing the played notes (all plucks within 20 dB of
    the loudest), from the first attack to where the last one has died away."""
    x = np.asarray(x, float)
    edb, hop = _env_db(x, fs)
    n = len(edb)
    if n < 10:
        return 0, len(x)
    top = edb.max()
    on = _onsets(edb, x=x, fs=fs, hop=hop)
    on = [o for o in on if edb[o:o + 8].max() > top - 20] or [int(np.argmax(edb))]
    start = max(0, on[0] - 2)
    end = on[-1]
    while end < n - 1 and edb[end] > top - 35:
        end += 1
    end = min(end, start + int(max_len_s * fs / hop))
    return int(start * hop), int(min(len(x), end * hop))


def note_bodies(x, fs, skip_attack_ms=80, min_len_s=0.25):
    """The sustained part of every pluck in x (attacks removed), as a list of
    segments. Repeated plucks of the same note average out the pluck-to-pluck
    differences in overtone balance."""
    x = np.asarray(x, float)
    edb, hop = _env_db(x, fs)
    n = len(edb)
    on = list(_onsets(edb, x=x, fs=fs, hop=hop))
    if not on or on[0] > 5:
        on = [0] + on
    top = edb.max()
    parts = []
    for a, b in zip(on, on[1:] + [n]):
        s0 = a + int(skip_attack_ms / 10)
        e0 = b - 1
        while e0 > s0 and edb[e0] < top - 40:
            e0 -= 1
        if (e0 - s0) * hop < min_len_s * fs:
            continue
        parts.append(x[s0 * hop:e0 * hop])
    if not parts:
        return [x[int(skip_attack_ms * 1e-3 * fs):]], 1
    return parts, len(parts)


def analyse(x, fs):
    """Harmonic analysis of a selection that may contain several plucks."""
    bodies, n_plucks = note_bodies(x, fs)
    f0 = detect_f0(max(bodies, key=len), fs)
    h = harmonics(bodies, fs, f0)
    h["plucks"] = n_plucks
    return h


# ----------------------------------------------------------------------------
# Correction points + curve
# ----------------------------------------------------------------------------
def pair_points(ref, pz):
    """Matched harmonic powers from one reference/piezo note pair.

    Returns rows (f, ref_power, piezo_power, weight). Powers are normalised per
    pair (by the piezo's total harmonic power) so notes of different loudness
    combine fairly. Same note: harmonic-by-harmonic. Different notes: the
    reference envelope is interpolated to the piezo's harmonics (less accurate).
    """
    same = abs(ref["f0"] / pz["f0"] - 1) < 0.03
    rows = []
    if same:
        for fr_, pr, sr in zip(ref["freqs"], ref["power"], ref["snr_db"]):
            j = int(np.argmin(np.abs(pz["freqs"] - fr_)))
            if abs(pz["freqs"][j] / fr_ - 1) > 0.02:
                continue
            w = min(sr, pz["snr_db"][j]) - 6
            if w <= 0:
                continue
            rows.append((0.5 * (fr_ + pz["freqs"][j]), pr, pz["power"][j], min(w, 40) / 40))
    else:
        lo = max(ref["freqs"][0], pz["freqs"][0])
        hi = min(ref["freqs"][-1], pz["freqs"][-1])
        lr = np.log(ref["freqs"])
        for f, p, s_ in zip(pz["freqs"], pz["power"], pz["snr_db"]):
            if f < lo or f > hi:
                continue
            r = math.exp(np.interp(math.log(f), lr, np.log(ref["power"])))
            rows.append((f, r, p, 0.5 * min(s_ - 6, 40) / 40))
    rows = np.array(rows).reshape(-1, 4)
    if len(rows):
        g = rows[:, 2].sum()
        rows[:, 1] /= g
        rows[:, 2] /= g
    return rows, same


def _kernel_curve(lf, pr, pp, w, lg, sig0):
    out = np.zeros(len(lg))
    conf = np.zeros(len(lg))
    for i, g in enumerate(lg):
        sig = sig0
        for _ in range(8):
            k = w * np.exp(-0.5 * ((lf - g) / sig) ** 2)
            if k.sum() > 0.5:
                break
            sig *= 1.6
        out[i] = 10 * math.log10(max(np.sum(k * pr), 1e-300) / max(np.sum(k * pp), 1e-300))
        conf[i] = min(1.0, k.sum() / 0.5) * (sig0 / sig)
    return out, conf


def fit_curve(points, grid, smooth_oct=1 / 3, max_db=24.0):
    """Smooth correction curve (dB) through matched harmonic powers.

    points: rows (f, ref_power, piezo_power, weight, pair_id).
    Within a Gaussian window on log-frequency it takes sum(ref power) /
    sum(piezo power) - an energy average, like the H1 estimator - so a harmonic
    that sits in a narrow dip doesn't drag the curve down. The window widens
    where points are sparse (low notes).
    Every note pair has its own unknown level (each recording is a different
    loudness), so a level offset per pair is solved jointly with the curve.
    """
    f, pr, pp, w = points[:, 0], points[:, 1].copy(), points[:, 2], points[:, 3]
    pid = points[:, 4].astype(int) if points.shape[1] > 4 else np.zeros(len(f), int)
    lf = np.log2(f)
    lg = np.log2(grid)
    sig0 = max(smooth_oct, 1 / 48) / 2
    raw_db = 10 * np.log10(np.maximum(pr, 1e-300) / np.maximum(pp, 1e-300))
    off = np.zeros(pid.max() + 1)
    for p_ in range(len(off)):                 # start: each pair's median ratio
        m = pid == p_
        if m.any():
            off[p_] = np.median(raw_db[m])
    for _ in range(6):
        adj = pr * 10 ** (-off[pid] / 10)
        curve_pts, _ = _kernel_curve(lf, adj, pp, w, lf, sig0)
        res = raw_db - off[pid] - curve_pts
        for p_ in range(len(off)):
            m = pid == p_
            if m.any():
                o = np.argsort(res[m])
                cw = np.cumsum(w[m][o])
                off[p_] += res[m][o][np.searchsorted(cw, cw[-1] / 2)]   # weighted median
        off -= off.mean()
    adj = pr * 10 ** (-off[pid] / 10)
    out, conf = _kernel_curve(lf, adj, pp, w, lg, sig0)
    ref = (grid > 200) & (grid < 4000)
    norm = np.average(out[ref], weights=np.maximum(conf[ref], 1e-3)) if ref.any() else 0.0
    out -= norm
    pts_db = raw_db - off[pid] - norm          # points on the same scale, for display
    return np.clip(out, -max_db, max_db), conf, pts_db


def build_matched(pairs, fs_out=48000, smooth_oct=1 / 3, max_db=24.0, master_ms=500.0,
                  low_cut=35.0, high_cut=18000.0, base_master=None):
    """pairs: list of (ref_harmonics, piezo_harmonics). Returns a master-style dict."""
    pts = []
    for k, (r, p) in enumerate(pairs):
        rows = pair_points(r, p)[0]
        if len(rows):
            pts.append(np.column_stack([rows, np.full(len(rows), k)]))
    pts = np.vstack(pts) if pts else np.zeros((0, 5))
    if len(pts) < 4:  # noqa
        raise ValueError("Not enough matching harmonics. Use the same note for the reference "
                         "and the piezo, and pick the steady part after the attack.")
    grid = np.geomspace(20, min(20000, 0.49 * fs_out), 700)
    curve, conf, pts_db = fit_curve(pts, grid, smooth_oct, max_db)
    n = 65536 if fs_out <= 50000 else 131072
    f = np.fft.rfftfreq(n, 1 / fs_out)
    corr_db = np.interp(np.log(np.maximum(f, 1e-3)), np.log(grid), curve)
    P = 10 ** (corr_db / 10)
    sign = 1.0
    if base_master is not None:
        Pb = np.exp(E.interp_logf(base_master["freqs"],
                                  np.log(np.maximum(base_master["P"], 1e-30)), f))
        if base_master["fs"] != fs_out:
            Pb[f > 0.47 * base_master["fs"]] = Pb[np.searchsorted(f, 0.47 * base_master["fs"]) - 1]
        P = P * Pb
        sign = base_master.get("sign", 1.0)
    else:
        if low_cut > 0:
            P *= E.butter_mag2(f, low_cut, 2, "hp")
        hc = min(high_cut if high_cut > 0 else 0.47 * fs_out, 0.47 * fs_out)
        P *= E.butter_mag2(f, hc, 4, "lp")
    H = E.minphase_from_power(P, n)
    h = np.fft.irfft(H, n) * sign
    L = min(n, int(master_ms * 1e-3 * fs_out))
    h = h[:L] * E.half_hann_tail(L, 0.2)
    h = h / (np.max(np.abs(h)) + 1e-20) * 10 ** (-1 / 20)
    return {"h": h, "fs": int(fs_out), "P": P, "freqs": f, "nfft": n, "sign": sign,
            "phase": "minimum", "removed_predelay_ms": 0.0,
            "match": {"points": pts, "points_db": pts_db, "grid": grid, "curve": curve, "conf": conf,
                      "refine": base_master is not None}}


def through_ir(x, fs, master):
    """Piezo audio run through an IR (resampling the IR if needed)."""
    h = master["h"]
    if master["fs"] != fs:
        h = E.resample_to(h, master["fs"], fs)
    return signal.fftconvolve(x, h)[:len(x)]
