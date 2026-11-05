# De-squeak for the pedal (spec)

A dynamic filter that turns down finger squeaks, slides and string scrapes **only while they happen**, so notes keep their full brightness. It works like a vocal de-esser. It runs on the pedal, not in the IR, because an IR is a fixed EQ and can't tell a squeak from a note.

## Where it goes

```
piezo in ──► de-squeak ──► IR convolution ──► (notch filters) ──► out
```

Process the raw piezo signal before the convolution. Squeaks are cleanest to detect there, and because everything after it is linear, the IR needs no changes.

## How it works

1. **Split off the squeak band** with a state-variable band-pass: centre `fc` (default **3.5 kHz**, adjustable 2.5–5 kHz), Q ≈ **0.7**, so it's broad.
   `band = BP(x)`, `rest = x − band`. Recombined as `rest + band` it's exactly the input, so there's no colouration when nothing is happening.
2. **Three envelope followers** (peak or RMS, attack ≈ 0.5 ms):
   - `eb`: the squeak band, release ≈ 30 ms
   - `el`: a low band (low-pass ~800 Hz), release ≈ 30 ms; this is where the notes' fundamentals live
   - `base`: a slow average of `eb / el`, ≈ 300 ms; what's "normal" for the current playing
3. **Decide it's a squeak** when the squeak band jumps without the low band jumping with it:
   `excess_dB = 20·log10((eb / el) / base) − threshold`.
   A pick or finger attack raises both bands together, so it isn't treated as a squeak. A slide or scrape raises only the squeak band.
4. **Gain on the band:** `gr_dB = clamp(excess_dB · (1 − 1/ratio), 0, range)`, smoothed with **attack ≈ 1 ms** and **release ≈ 60 ms**. Then `out = rest + band · 10^(−gr_dB/20)`.

## Defaults and the knob

| | Default |
|---|---|
| Centre | 3.5 kHz (could come from the builder later) |
| Threshold | 6 dB above the running baseline |
| Ratio | 4 : 1 |
| Range (max cut) | 0–12 dB, **set by the knob** |
| Attack / release | 1 ms / 60 ms |

**Knob:** fully left is off (range 0). Turning it up raises the range to 12 dB and lowers the threshold from 9 dB to 3 dB, so it catches more and cuts deeper.

## Cost

One band-pass, one low-pass, three envelope followers and a gain per sample. That's well under 1% of the Daisy's processor at 48 kHz, with no added latency.

## How to check it

- Slides on the wound strings and scrapes between chords: these should drop by 6–12 dB at higher knob settings.
- Pick attacks, strums and bright high notes: these should be unchanged, with no gain reduction or only a little.
- Show gain reduction on an LED or in a debug print while tuning the threshold.

## Relation to the builder

The builder's **Finger-noise tamer** is the fixed, always-on version (a gentle dip built into the IR, off by default). Use either or both. With de-squeak on the pedal, the tamer can usually stay off, or at −2 to −3 dB.
