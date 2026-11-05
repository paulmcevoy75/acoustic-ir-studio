# IR file format for pedals and DIY firmware

This file describes exactly what Acoustic IR Studio exports, so pedal firmware (e.g. a Daisy Seed convolver) can rely on it.

## Single IR

| | |
|---|---|
| Sample rate | as the target says. The Daisy targets are 48 kHz |
| Channels | mono |
| Length | at most the target's limit. A shorter IR is exported as is, never padded |
| Start | the first arrival, with no extra delay at the start |
| Level | peak ≤ −1 dBFS. With *Match loudness*, all lengths share one loudness, peak ≤ −0.3 dBFS |

Files for targets marked as DIY firmware (the *Long* and *Ultimate* presets, or custom targets added as "DIY firmware"):

- **`<name>_<target>_<ms>ms_<rate>k_<samples>.wav`**: 32-bit float WAV.
- **`….bin`**: the same samples as raw **little-endian float32**, with no header. The sample count is the file size ÷ 4.
- **`….h`**: a C header:
  ```c
  #define <NAME>_LENGTH 48000
  #define <NAME>_SAMPLERATE 48000
  static const float <name>[48000] = { ... };
  ```

Normal pedal targets get only the WAV, at the bit depth chosen in the app (16-bit, 24-bit or 32-bit float).

## Dual piezo IR pair (optional)

When a session was recorded with **Instrument 2**, each export also gets `…_P1` and `…_P2` files in the same formats:

- Run piezo element 1 through `_P1` and element 2 through `_P2`, then **add the two outputs**.
- Both IRs share one time base, so both paths must have **identical latency**.
- Both are scaled by the same factor, so their peak is ≤ −1 dBFS.
- The plain (non-P) file is the IR for both elements summed, for single-input pedals.

## Daisy Seed notes

- **Size:** a 1 s IR is 192 KB of float32, which is larger than the Seed's 128 KB internal flash. Either:
  - build with the Daisy bootloader (`APP_TYPE = BOOT_QSPI`) so the app and the `.h` array live in the 8 MB QSPI flash, or
  - write the `.bin` into QSPI flash or load it from the SD card.
- **Speed:** at startup, copy the IR into RAM. Put the early part in AXI SRAM and the tail in SDRAM (`DSY_SDRAM_BSS`).
- **Method:** use FFT partitioned convolution (CMSIS-DSP `arm_rfft_fast_f32`). Non-uniform partitions (small blocks first) keep latency low and make 1 s practical. Working memory is roughly 4–6 times the IR size.

## De-squeak

A dynamic filter for finger squeaks and string noise, processed before the convolution. See [desqueak-spec.md](desqueak-spec.md).

## Reserved for later

- **Notches:** a list of feedback notches (frequency Hz, width Q, maximum depth dB), meant to run as filters after the convolution. A "smart notch" pot would scale the depth. It will be a small JSON file next to the IR.
- **Metadata JSON:** name, version, sample rate, length, single or pair.
