# PureIR — long acoustic-guitar IR + reverb + EQ + compressor + boost

Firmware for the **Daisy Seed3 Pedal Dev Kit**, aimed at a **piezo pickup**, plus an
optional **ESP32 display** (one wire from the Daisy). The same DSP is written so it
can later live on a bare Daisy Seed inside a guitar.

```
piezo → DC block → PIEZO COMP → input gain → low cut ─┐
                                                      ▼
        ┌── dry ──────────────────────────────► IR mix ◄── full-length IR (≤ 4 s)
        ▼
   4-band EQ → compressor → boost → reverb (mix) → output → soft limiter → L/R out
```

- **Full-length IRs, zero added latency.** The first 4096 samples of the IR are
  convolved inside the audio interrupt (64-sample partitions); the rest (up to
  192 000 samples = **4 s at 48 kHz**) in the main loop with 2048-sample partitions.
  The result equals straight convolution (host tests compare against brute force to
  ~1e-5). IR changes cross-fade silently; no clicks.
- **IRs from the SD card** (`/IR/*.wav`, else the card root) — 8/16/24/32-bit PCM or
  float, mono or multichannel (folded to mono), 8–192 kHz (resampled to 48 kHz),
  leading silence trimmed, noise floor trimmed, DC removed, normalised to unit energy so
  switching IRs doesn't jump in level. Two built-in IRs (**Neutral**, **Demo Body**)
  work with no card. *Demo Body is synthetic, not a real guitar.*
- **Piezo compensation:** undoes a 130 Hz one-pole high-pass (pickup capacitance into
  the input impedance) with a gentle 25 Hz floor. Amount is a knob (0–100 %).
- **EQ:** low shelf 120 Hz, low-mid 400 Hz, presence 2.5 kHz, high shelf 7 kHz (each
  ±12 dB), plus a 12 dB/oct low-cut (20–250 Hz).
- **Compressor:** feed-forward, soft knee, threshold / ratio / release / make-up
  (ratio 1:1 = off). **Boost:** 0…+12 dB after the EQ/compressor.
- **Reverb:** Room, Hall, Plate (feedback-delay networks) and Spring; mix, time
  0.3–8 s, size, damping. Switching engines fades out, clears and fades in.
- **Footswitches:** bypass (tap) / mute (hold) and IR next (tap) / previous (hold).

## Controls (Pedal DevKit)

SW1 (3-way) picks a **bank**; the six knobs act on that bank. A knob does nothing
until it passes the stored value ("pickup"), so changing bank never makes the sound
jump.

| Knob | SW1 up: **MAIN** | SW1 centre: **TONE** | SW1 down: **DYNAMICS / VERB** |
|---|---|---|---|
| 1 | IR mix (default 100 %) | Low shelf ±12 dB | Compressor threshold |
| 2 | Reverb mix | Low-mid ±12 dB | Ratio (1–10:1) |
| 3 | Reverb time | Presence ±12 dB | Release |
| 4 | Output level | High shelf ±12 dB | Make-up gain |
| 5 | Input gain −12…+30 dB | Low cut 20–250 Hz | Reverb size |
| 6 | Boost 0…+12 dB | Piezo comp 0–100 % | Reverb damping |

SW2 / SW3 select the reverb engine as a 2-bit code: both left = Room, SW3 right =
Hall, SW2 right = Plate, both right = Spring.
FSW1: tap = bypass, hold (0.6 s) = mute. FSW2: tap = next IR, hold = previous IR.
LED1 = effect on. LED2: green = ready, blue blink = loading IR, red = IR could not
be loaded, magenta = DSP late (see below), yellow blink = muted.

See [docs/MANUAL.md](docs/MANUAL.md) for the short user manual.

## IR files

Copy WAVs to an SD card (FAT32) in a folder named `IR`. They are sorted by name.
Press FSW2 to step through them (the SD card is read at power-up, or when a card is
inserted later). A bad file turns LED2 red and the previous IR keeps playing.

## Build & flash

Needs `arm-none-eabi-gcc` and a built libDaisy (tested against commit `933bd9e`).

```
git clone --recurse-submodules https://github.com/electro-smith/libDaisy ../../deps/libDaisy
make -C ../../deps/libDaisy
make                       # or: make LIBDAISY_DIR=/path/to/libDaisy
```

The app is built as **BOOT_SRAM**: the Daisy bootloader copies it from QSPI flash to
SRAM and runs it, because the internal flash is only 128 KB. Install the Daisy
bootloader once (see the Daisy docs), then put the Seed in DFU mode (hold BOOT, tap
RESET) and run `make program-dfu`. `make` also leaves `build/PureIR.bin` for the
Daisy web programmer.

Memory (from the linker): SRAM 205 KB of 480 KB, DTCM 90 KB of 128 KB, D2 SRAM
218 KB of 256 KB, SDRAM 9.3 MB of 64 MB.

## Display (ESP32)

`display/PureIR_Display/` is an Arduino sketch for an ESP32 "Cheap Yellow
Display" (ESP32-2432S028R, 320×240). It needs the `LovyanGFX` library.

Wiring: **Daisy D12 (pin 13, UART4 TX) → ESP32 GPIO35**, and a **common ground**.
Only one wire carries data; the pedal never waits for the display, and works
identically without it. Power the ESP32 from its own USB supply while testing —
the display backlight is noisy and should not share a supply with the audio
circuit. If colours are inverted or swapped, change the `DISPLAY_*` defines at the top
of the sketch; if the board has a single USB-C port it is probably an ST7789 panel
(`PANEL_ST7789 1`).

The display shows the bank, reverb engine, IR name/number, SD/loading/error state,
input and output level, compressor gain reduction, CPU load and the six active
parameters. The protocol is a single ASCII line, documented in `src/link.h`.

## Piezo / input notes

- The Daisy Seed's audio input is a line-level input, **not** a high-impedance
  instrument input. A piezo needs a high-impedance buffer or preamp in front of it
  (a FET or op-amp stage, ≥1 MΩ in). Check what the Pedal DevKit's input stage
  actually provides before connecting a passive piezo; if you use a preamp, adjust
  `kPiezoHpHz` in `src/chain.h` to the high-pass you really get (or set Piezo Comp
  to 0 %).
- Set Input Gain so peaks stay below the clip warning, then use Boost for level.

## Tests

`tests/run.sh` builds and runs the PC tests: FFT vs DFT; convolver vs brute force
(300 samples … 4 s, late-background timing); IR swap; compressor curve; EQ and
piezo-compensation response; reverb stability, level calibration and RT60; WAV
parsing (8/16/24-bit, float, extensible, odd chunks); resampling; link frames and
checksum; parameter mapping and knob pickup; whole chain (no NaN, limiter ≤ 1.0);
and the display parser against the real frame builder.

## Not tested on hardware

I could build the firmware and run all DSP on a PC, but I had no Daisy or ESP32 to
run it on. Specifically unverified: real-time CPU load (the 4 s IR case reads about
3 MB of SDRAM per 42.7 ms block; if LED2 turns magenta, use a shorter IR), SD card
speed/compatibility, the switch/LED polarity and bank order on your unit, the UART
link, and the ESP32 sketch (checked for syntax only). Expect to tune small things.

## Layout

```
src/main.cpp            hardware glue: controls, SD, LEDs, display link
src/chain.h             the signal chain
src/params.h            parameters, knob mapping, pickup
src/dsp/convolver.h     two-zone partitioned FFT convolution
src/dsp/reverbs.h       Room / Hall / Plate / Spring
src/dsp/compressor.h    compressor
src/dsp/filters.h       biquads, smoothing, soft limiter
src/dsp/fft.h           real FFT
src/ir_loader.h         WAV parse, resample, clean-up
src/link.h              status frames for the display
tools/ir2header.py      regenerate src/ir_builtin.h
tools/gen_display_params.py   keep the display's parameter table in sync
display/PureIR_Display/   ESP32 sketch
housing/                3D-printable base tray (make_base.py -> PureIR-base.stl)
(this folder is the PureIR pedal inside the Acoustic IR Studio repo)
tests/                  PC tests
```

## Licence
GPL-3.0, the same as Acoustic IR Studio (see `../LICENSE`). Uses libDaisy (MIT).
