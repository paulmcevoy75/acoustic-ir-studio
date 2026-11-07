# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Hover-help texts."""

from __future__ import annotations


TEXT_TIPS = {
    "Instrument": "The pickup signal you'll put the IR on: the piezo (or a DI'd electric / bass). Choose the "
                  "Interface input it's plugged into.",
    "Label": "Optional name for this input (e.g. the mic model). It's shown on the meters, the mic mix and "
             "the audition buttons. Leave blank to show the device and input number.",
    "Calibration file...": "For a measurement mic: load its calibration file (.txt/.cal from the maker) so "
                           "its own coloration is removed. Not needed for normal mics.",
    "Mac sound settings...": "Opens the Mac's Sound settings (Input), to set the level of a USB Mic or "
                             "other device without a gain knob. Interface inputs use their gain knobs.",
    "System sound settings...": "Opens the system sound settings, to set the level of a USB Mic.",
    "Windows input level...": "Opens Windows' recording settings for devices without a gain knob (e.g. a "
                              "USB Mic). Interface inputs use their hardware gain knobs.",
    "Windows input levels...": "Opens Windows' recording settings (Properties > Levels) to set a USB Mic's "
                               "level.",
    "Refresh devices": "Re-scan for audio devices - press after plugging something in.",
    "   Driver type": "WASAPI is the best choice on Windows. 'All' lists every driver type if a device is "
                      "missing.",
    "Sample rate": "All devices must run at this rate (set it in Windows > Sound > device > Advanced too). "
                   "48 kHz is a good choice.",
    "Audition output (headphones / monitors)": "Where the headphone monitor and Audition play. Usually your "
                                              "Interface's outputs.",
    "Start audio": "Opens all the inputs so the meters work. Set levels so your hardest playing peaks "
                   "around -12 to -6 dBFS.",
    "Stop audio": "Closes the audio devices.",
    "Reset peaks": "Clears the peak markers on the meters.",
    "Headphone monitor": "Hear every input through the Audition output. M = mute, S = solo (only that "
                         "input). This only changes what you hear, never what's recorded.",
    "M": "Mute this input in the headphones (not in the recording).",
    "S": "Solo: hear only this input in the headphones.",
    "Master": "Overall headphone level.",
    "Session": "Full = every step; Quick = similar steps merged, a few minutes shorter.",
    "Step lengths...": "Set how long each guided step records. 0 skips a step.",
    "Record this step": "Records the current guided step; it stops by itself when the time is up.",
    "+30 s": "Give the current take 30 more seconds.",
    "Stop": "Stop recording now - the take is kept and analysed.",
    "Free take": "Record anything extra, as long as you like - e.g. whatever the coverage chart says is "
                 "weak.",
    "Undo last take": "Removes the last take from the IR (its recording stays in the autosave folder).",
    "< Prev": "Go back one step.",
    "Skip >": "Skip this step.",
    "Record demo": "Record ~30 s of normal playing (a bit of everything). It isn't used to build the IR - "
                   "it's what the Audition panel on the Build tab plays back through every option, so you "
                   "can compare the real mics against the IRs.",
    "Coverage - how certain the IR is in each band": "Each bar is how well that frequency range has been "
                                                     "measured so far. Green = solid; red/orange = play more "
                                                     "notes that land there.",
}


BUILD_TIPS = {
    "name": ("The IR's name - used for the exported file names."),
    "smooth": ("How much fine detail to keep in the frequency response. The IR is averaged over a band this wide (1/12 octave = about a semitone).\n\n- Finer (1/24, 1/48): more detail, body resonances stay sharp, but more risk of keeping things specific to this exact mic spot.\n- Coarser (1/6, 1/3): smoother, more forgiving, more 'generic'.\n- Detailed lows: fine in the bass (where body resonances matter) and smoother in the treble.\n- Psychoacoustic: roughly how the ear resolves pitch.\n1/12 is a good default."),
    "reg": ("Protects against bad data. Where the instrument signal was weak or noisy (e.g. very low or very high notes nobody played), the IR would boost wildly and whistle. This limits how far the IR may go in those places.\n\nLower (40-60 dB) = safer, smoother. Higher (80-100 dB) = trusts the data more, more detail, more risk of harsh peaks. 70 dB is a good default."),
    "lc": ("Removes everything below this frequency from the IR (0 = off). Cuts rumble, handling noise and boom that a stage PA doesn't want. 35 Hz for guitar, ~22 Hz for double bass. Raise to 70-100 Hz to tame feedback-prone boom."),
    "hc": ("Removes everything above this frequency (0 = off). Above ~16-18 kHz there's little musical content and mostly hiss; lower it (8-12 kHz) to soften pick and string noise."),
    "est": ("How the instrument and mic signals are compared:\n\n- H1 (recommended): ignores noise in the mic signal (room noise, hiss, other sounds). The most accurate in normal conditions.\n- Hv: a balance between H1 and H2 - keeps a bit more of the mic's air and room.\n- H2: use if the PICKUP is noisy (hum, buzz) and the mic is clean.\nIf unsure, keep H1."),
    "phase": ("- Minimum: all the energy as early as possible. Tight, punchy, no pre-echo, lowest latency - and it survives shortening for pedals best. Recommended.\n- Measured: keeps the timing exactly as recorded (including the delay from instrument to mic and any room reflections). Can sound more 'roomy' but smears and shortens badly; mostly for experiments."),
    "len": ("How long the full-length (master) IR is. Longer captures more of the body's ring and the lowest notes accurately, but it's mostly decay after ~200 ms. 1000 ms (the default) covers the Ultimate target; up to 2000 ms for double bass. Every pedal version is cut down from this, so keep it at least as long as your longest target."),
    "tame": ("Softens finger squeaks, slides and string noise, which piezos exaggerate. It's a broad, gentle dip "
             "built into the IR (2-6 kHz is where squeaks live; 3500 Hz is a good start). It also takes a little "
             "sparkle off the notes in that range, so use as little as you need: switch it on and off while "
             "auditioning your demo. Off by default.\n\nFor squeaks only (notes untouched), a pedal can add a "
             "'de-squeak' that reacts only when the noise happens."),
    "inv": ("Flips the IR upside down (polarity). Doesn't change the tone on its own, but if you blend the IR'd signal with another signal (a mic, an internal mic, a magnetic pickup) and the sum sounds thin, try flipping it."),
    "build": ("Builds the IR from all your takes using the settings above."),
    "targets": ("The pedals / devices to make IRs for. Each has a maximum IR length; the app fits the best possible IR into that length. 'Error' = how different the short version is from the full IR (under ~1 dB is very hard to hear). Click one to see it on the graph and to audition at that length; Ctrl/Shift-click to pick several for export."),
    "norm": ("- Same loudness as dry (recommended): each IR is set so your piezo through it is as loud as the "
             "piezo without it - switching the IR on doesn't drop the volume. If a file can't get that loud, the "
             "notes under the graph say how much to turn the pedal up.\n- Match loudness: every exported length plays at the same perceived volume, so you can swap IRs or pedals without a jump in level.\n- Peak -1 dBFS: each file is made as loud as possible without clipping (lengths may differ in volume)."),
    "bits": ("File format of the exported WAVs. 24-bit suits almost every pedal. Use 16-bit only if your pedal rejects 24-bit; 32-bit float for software / DAW convolvers."),
    "bundle": ("Also saves the full-length master IR (32-bit float) and a notes file with all settings. Every export goes into the folder '<Project Name> IRs', next to where the project is saved."),
    "export": ("Saves an IR file for every selected target, into the folder '<Project Name> IRs' next to the saved project (you're asked to save the project first if it hasn't been)."),
    "add": ("Add your own pedal / device: its sample rate and maximum IR length (check its manual)."),
    "rem": ("Remove the selected targets from the list."),
}
