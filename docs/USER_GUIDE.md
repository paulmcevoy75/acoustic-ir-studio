# Acoustic IR Studio - User Guide

This app makes acoustic-guitar IRs from a piezo and a mic recorded at the same time, all inside the app. It runs a guided multi-take session, combines every take into one IR, and exports versions fitted to the IR length each of your pedals can handle.

## Install and run

**Easiest: download the ready-made app (no Python needed)**
1. Go to the repository's **Releases** page (right-hand side of the GitHub page), open the newest **Version**, and download **AcousticIRStudio-windows.zip**. It's rebuilt automatically every time the code is updated; the app's title bar and **Help > About** show which version you have.
2. Unzip it anywhere and run **AcousticIRStudio.exe**.
3. Windows may show *"Windows protected your PC"*, because the app isn't code-signed. Click **More info → Run anyway**.
4. To update, download the newest zip and unzip it over the old folder. Your autosaves, logs and device settings in that folder are kept.

**Or run from the source code** (for trying changes before they're released)
1. Install **Python 3.10 or newer** from python.org. Tick **"Add python.exe to PATH"** during setup.
2. Download the code (green **Code** button → Download ZIP), unzip it, and double-click **`Run Acoustic IR Studio.bat`**. The first launch installs the libraries (needs internet).
3. If the app closes unexpectedly, use **`Run with error window (troubleshooting).bat`** and include the message it shows, plus `logs\app.log`, when you report the problem.

## Inputs

Every take records all the inputs at the same time. Each dropdown lists every input as its own **mono** channel, e.g. *Interface - Input 1*. Hover over any control in the app for an explanation.

| Input | What it is | Notes |
|---|---|---|
| **Instrument** | the piezo, or a DI'd electric / bass | the interface input it's plugged into |
| **Reference Mic** | the mic you trust most: ideally a measurement mic (USB or XLR), but any mic works (an SM57, a condenser...) | load a **calibration file** if it's a measurement mic |
| **Color Mic** (optional) | a second mic with a sound you like | gets its own IR; mix the two by frequency on the Build tab |
| **Instrument 2** (optional) | the second element of a dual piezo, wired to its own output | must be on the **same Interface** as the Instrument; see *Dual piezo* below |
| **Internal Mic** (optional) | the guitar's own built-in mic, if it has one | recorded with everything else; can be blended in when you audition |

- **Type a Label** for each input on the Setup tab (e.g. the mic model). That name shows on the Capture meters, the mic mix and the audition buttons. Without a label, the device and input number are shown, e.g. *Color Mic [Interface In 2]*, so it's always clear which Interface channel is which.
- A mic on a different device from the instrument (e.g. a USB mic) is clock-synced automatically.
- **USB mic level:** use the **Input level (USB)** slider on the Capture tab, which sets the mic's Windows level from inside the app. **Windows input level...** opens the Windows recording settings if you prefer. Interface inputs use their hardware gain knobs.
- Set every device to the **same sample rate** in Windows (Sound settings > device > Advanced) as the app, ideally 48 kHz.
- Close Reaper and other audio apps while you capture.

## Capture

1. Click **Start audio**. Set the **Interface gain knobs** so your hardest playing peaks around **-12 to -6 dBFS** on the meters. A USB Mic's level is set with the slider on the Capture tab.
   - Each meter's **peak marker** shows the highest peak as a number. It turns **red above -3 dBFS** (too hot) and shows **CLIP** if the signal clipped. Clipped parts are thrown away automatically. **Reset peaks** clears the markers.
2. **Headphone monitor:** tick it to hear every input through the output chosen on the Setup tab, e.g. the Interface's headphone jack.
   - Each input has **M** (mute), **S** (solo) and a level slider, plus there's a master level.
   - These only change what you hear, never the recording.
   - For zero-delay playing, the Interface's own direct-monitor knob is best. Use Solo to check each input on its own.
3. Choose a **Session**: **Full** (14 steps, about 9 minutes) or **Quick** (overlapping steps merged, about 6 minutes). **Step lengths...** lets you set every step's length, and 0 skips a step. The Full session covers:
   - strums, fingerpicking, low and high single notes
   - **the highest notes on the neck, slow, then medium and fast**
   - **picking close to the bridge, then far from it**
   - held low notes, harmonics, percussive playing and barre chords
   - **Free play** as the required last step: anything you think was missed, for as long as you like. Press Stop when you're done.

   Timed steps stop by themselves. Press **+30 s** if you need longer.
4. Watch the **coverage chart** and **What to play next**. Use **Free take** for anything extra.
5. **Record demo** (also on the Build tab): about 30 s of normal playing. It isn't used to build the IR; it's what **Audition** plays back. It's saved with the session and the project file.

## Saved as you go and crash recovery

- **Every take is autosaved** the moment you stop: both the analysis and the actual recordings (lossless FLAC).
- If the app closes unexpectedly, it **asks to reopen your last session** next time, and carries on from the step you were on.
- **File > Reopen last session**: the autosaved session.
- **File > Open the session before that...**: starting a new session keeps the previous one.
- **File > Rebuild last session from its recordings...**: re-analyses every saved recording from scratch.
- Errors no longer close the app. They're shown and written to a log. If anything goes wrong, use **Help > Open logs folder** and send `app.log`; hard crashes are recorded in `crash.log`.
- The autosave and logs are kept next to the app, or in `%LOCALAPPDATA%\AcousticIRStudio` if that folder is read-only.

## Build and export

**Algorithm** (Build tab):
- **H1** (recommended): ignores mic-side noise such as room noise and hiss.
- **Hv**: keeps a little more of the mic's air and room.
- **H2**: for a noisy pickup.

**Smoothing** also offers *Detailed lows* (1/24 octave in the bass, 1/6 in the treble) and *Psychoacoustic* (1/3 → 1/6 octave).


- **Build IR**. The defaults are best for most guitars: 1/12-octave smoothing, minimum phase, 35 Hz low cut.
- **Mic mix** (with a Color Mic): choose how much of each mic to use in the **Lows**, **Mids** and **Highs**, e.g. bass from the Reference Mic and sparkle from the Color Mic. 0 = all Reference Mic, 100 = all Color Mic. The two split points (default 250 Hz and 3 kHz) are adjustable, and **Link** turns it back into one simple blend.
- **Which IR to make:** *Blended mics* (using the Mic mix), *Reference Mic only* or *Color Mic only*. That's the IR that's shown and exported.
- **Export targets** are four presets, each tuned for one kind of device:
  - **Short** – Sonicake Smart Box and other short loaders (1024 samples at 44.1 kHz)
  - **Medium** – TC Electronic Impulse IR Loader (200 ms at 48 kHz)
  - **Long** – a basic Daisy Seed convolver (0.5 s)
  - **Ultimate** – the longest that's practical on a Daisy Seed (1 s)

  Pick your pedal first: the graph and Audition use that length. The **Error** column shows how close that length gets to the full IR; below about 1 dB is very hard to hear. Your own devices (**Add custom...**) are kept in your data folder, so updates don't remove them.
- **Match loudness** keeps all your exported lengths at the same volume.
- **Audition:** press **Record new demo** (right on the Build tab) and play up to 60 s; it records the piezo (plus the Internal Mic if it's on, and the mics only if you tick *also record the mics*). Then play it as: the piezo dry, the real mics (if recorded), or the piezo through the chosen / Reference Mic / Color Mic / Blended IR, all cut to the selected target's length.
  - Every option is level-matched, and switching while it plays carries on from the same spot.
  - **Volume** and **Output** are right there; drag across the waveform to **loop** a section, **Whole demo** to clear it.
  - **Blend in Internal Mic** mixes the guitar's internal mic in with any option.
- **Add custom...** lets you add any pedal by sample rate and maximum IR length. Pedals marked *(verify)* use typical values, so check your unit's manual.

**Other handy bits:** **Play IR** (above the graph) plays the impulse once. The **Show** checkboxes above the graph turn each line on or off. **Blend graph** opens a bigger window that follows the Lows / Mids / Highs sliders live. On the Capture tab, the **live spectrum** shows every input while you play, so you can see how the piezo differs from the mics. **Settings > Set backup folder** copies every export to a second folder too. On small screens (13" laptops) each tab scrolls. **Settings > Set default export folder** makes Export start in a folder you choose; pick one inside Google Drive / Dropbox / OneDrive and every export is backed up automatically. The small graph next to the Mic mix shows how much Color Mic is used at each frequency, and the IR rebuilds by itself a moment after you change the mix.

## Projects, re-editing and multi-pedal export

- **File > Save project** (`.irproject`) stores everything: all takes, calibration files, build settings and your selected targets. Re-open it later to tweak the settings, add takes, or export for a new pedal.
- **Exports go next to the project**, into a folder called **`<Project Name> IRs`** in the same folder as the `.irproject` file. If the project hasn't been saved yet, Export asks you to save it first. The project is re-saved on every export, so it always matches its IRs.
- **Export selected...** exports the targets you pick; **Export ALL lengths...** exports every target in the list. Each file name says the pedal and the length, e.g. `MyGuitar_Short-Smart_Box_other_short_loaders_23ms_44.1k_1024.wav`. With *"Also save full-length IR + notes"* ticked (the default) the folder also gets:
  - `<name>_FULL_..._32f.wav`: the full-length, 32-bit float master
  - `<name>_info.txt`: settings, plus each target's length and error
- **Dual piezo:** the IR pair (`_P1` / `_P2`) is exported automatically when the session was recorded with Instrument 2, and is off for a single piezo.
- **File > Open an IR WAV to re-fit for pedals...** loads any IR (your own full-length master, or someone else's) and exports it for any pedal. Starting from a long IR gives the best short versions.

## Tone Match (tab 4): make your instrument sound like a recording

Use this when you have a sound you love, such as another guitar, a mic'd recording, or a record. The reference doesn't have to be recorded at the same time as your piezo.

1. **Reference:** load **any audio or video file** (WAV, FLAC, MP3, M4A, MP4, MOV, MKV...). Find a clear, sustained note, e.g. an open A ringing out. Drag on the waveform to select it, or press **Auto-select**.
2. **You:** play that **same pitch** (the same note and octave, e.g. your own open A) 3–5 times, letting each ring about 2 s. Choose the input and press **Record 12 s from**, or load a file.
3. **Add this note pair.** One pair only tells the app about that note's overtones, so repeat steps 1–2 with a few different notes (e.g. low E, A, D, G, B, high E). More pairs give a more accurate match.
4. **Build matched IR.** There are two modes:
   - **Start from my Capture IR, then nudge it toward the reference (best):** keeps the detailed body resonances from your Capture session and only corrects the overall tone. Needs Capture takes. In tests it got within about 1.5 dB of the target, using 6 notes × 4 plucks.
   - **Notes only (no Capture session needed):** builds the IR from the notes alone, e.g. for an electric or bass. It gets the broad tonal balance right (within about 2–3 dB per octave with 6+ notes), but it can't know about narrow resonances between the notes you played.
5. **Listen** (*Reference* vs *Mine + matched IR*), then export it on **Build & Export** like any other IR. It's saved in the project file too.

How it works: a sustained note only has energy at its harmonics, so the app measures each harmonic of the reference and of your piezo playing the same note, then compares them harmonic by harmonic. Pick attacks are skipped, and repeated plucks are averaged, which cancels the overtone differences between individual plucks. Each note pair's overall level is solved automatically, so the recordings can be any loudness. A smooth curve is fitted through all the points (energy-averaged, so narrow dips don't drag it down) and becomes a minimum-phase IR.

Tips:
- Use the **same note** on both sides. Different notes still work, but less accurately.
- Pick the reference from a clean, solo, sustained passage, without reverb or other instruments.
- 24-bit audio is better than MP3 for the quiet high harmonics.

## Sonicake Smart Box

The Smart Box runs at 44.1 kHz and has 5 user IR slots, which you load in Sonicake Manager or the SonicLink app. Sonicake doesn't publish the maximum IR length, so there are two presets:
- **Smart Box (verify)**: 1024 samples
- **Smart Box - 2048 (try)**: 2048 samples

Export both. If Manager accepts the 2048 file without trimming it, use that one: it's noticeably closer to the full IR.

## Double bass and long IRs

Choose **Instrument: double bass** on the Setup tab **before you capture**. This switches to:
- the double-bass guided steps (pizz, arco, harmonics, slaps)
- bass-aware playing suggestions
- coverage down to 31.5 Hz
- a 22 Hz low cut
- a longer analysis window, so the **Full IR length** can go up to 2 s (guitar sessions go up to about 1 s)

## Dual piezo (two pickup elements on separate outputs)

If your pickup has two elements and you can wire each to its own Interface input, turn on **Instrument 2** on the Setup tab and capture as usual.

- After every take, the log says what the second element adds, e.g. *"Dual piezo: a real gain: 6 dB less unexplained sound than the summed pickup, most in 125 Hz-800 Hz"*, or that the two elements say almost the same thing (then the summed pickup is just as good and there's no point wiring them separately).
- **Build IR** still makes the normal IR (for both elements summed, as on a single-input pedal) and also an **IR pair**: one IR per element. Run element 1 through `_P1`, element 2 through `_P2`, and add the two outputs. That needs a 2-input convolver such as a Daisy Seed (stereo in); it costs about 1.5-2x the processing of one IR.
- **Audition > Piezo 1 + 2 through the IR pair** plays exactly that, so you can compare it with the summed pickup through one IR.
- **Export** writes the pair next to each normal file (tick *Dual piezo: also export the IR pair*); Daisy targets get `.wav`, `.bin` and `.h` for each.
- Where the two elements don't add information, the pair automatically falls back to the summed-pickup answer, so it is never worse than the normal IR.

## Daisy Seed / DIY firmware targets

The **Daisy Seed** targets (250 ms to 2 s at 48 kHz) export three files:
- a 32-bit float `.wav`
- a raw little-endian float32 `.bin`, the easiest to load from SD card or QSPI
- a C header `.h` with a `static const float` array, for compiling the IR straight into firmware

At 4 bytes per sample, a 1 s IR is 192 KB. That's more than the Seed's 128 KB internal flash, so either build with the Daisy bootloader (QSPI/SRAM app) or load the `.bin` at startup.

If an IR is shorter than a target's limit, the whole IR is exported with no padding.

## How it stays accurate

- **One estimate from all takes.** It doesn't average finished IRs. Each take adds its raw cross-spectrum and power spectrum, so every take counts most where it actually had energy: low notes define the bass, harmonics define the top.
- **Coherence-based quality.** The app measures, band by band, how much of the mic signal the piezo explains, and converts that into a ± dB uncertainty. That's what drives the coverage meter and the suggestions.
- **Noise protection.** Bins with poor data lean on wider smoothing instead of being boosted into whistles.
- **Mic calibration.** The measurement mic's own coloration is removed using its calibration file.
- **Pedal fitting.** For each target length, the app tries plain trimming with different tail fades, plus an iterative minimum-phase redesign that compensates for what a short window smears. It keeps whichever measures closest to the full IR, so the result is never worse than simple truncation.

If the app ever closes unexpectedly, start it with **Run with error window (troubleshooting).bat**. It keeps a console open that shows the error. Also send `logs\app.log` and `logs\crash.log`.

`self_test.bat` runs the built-in accuracy test on simulated guitar recordings with a known answer.
