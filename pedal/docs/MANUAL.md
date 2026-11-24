# PureIR pedal manual (dev board) - firmware v0.2.1

Names used in this manual: **left LED** = the colour LED by the SD slot (LED2); **right LED** = the effect-on LED near the footswitch sockets (LED1); **Daisy light** = the small LED on the Daisy module; **left footswitch** = FSW1 (effect on/off); **right footswitch** = FSW2 (next IR).

## 0. Put the firmware on the pedal (flashing)
The firmware is the file **PureIR.bin** (in the `firmware` folder of the zip).

You need: a USB-C cable to your computer, and Chrome or Edge (not Safari or Firefox).

First time only: install the Daisy bootloader.
1. Plug the USB-C cable into the USB-C socket on the Daisy Seed module itself (the small module with the BOOT and RESET buttons), not the separate USB-C socket on the main board. Also plug in the 9 V power.
2. On the small Daisy Seed module on the board, find the BOOT and RESET buttons. Hold BOOT, tap RESET, then let go of BOOT. (This is "DFU mode".)
3. In Chrome or Edge open https://flash.daisy.audio and click the **Bootloader** tab.
4. Click the button to flash/install the bootloader and choose the Daisy device if asked. Wait until it says done.

Every time you load or update PureIR:
1. Tap RESET only (do not hold BOOT). The Seed pulses its light for about 2.5 seconds; start the next steps within that time.
2. On flash.daisy.audio click the **File Upload** tab, choose **PureIR.bin**, and click Flash.
3. Wait until it says done. Tap RESET again. The left LED turns green when it is ready.

If you see "incompatible binary", the bootloader is missing: do the first-time steps. If you get a USB error, unplug, redo the RESET step, and try again.

## 1. Set up
1. Plug both footswitches into their headers (FSW_1 = left, FSW_2 = right) on the front edge of the board.
2. Guitar into the LEFT input (IN L). The right input does nothing.
3. Amp or headphones into OUT L, OUT R, or both. One jack = one side only.
4. Plug in the 9 V DC supply.
5. Wait about 5 seconds. Right LED on = effect on. Left LED green = ready.

## 2. IRs: built in (no SD card needed)
The firmware has three IRs built in. With no SD card the pedal starts on the first one:
1. **MyGuitar 0.5s** (starts here)
2. **MyGuitar 1s**
3. **Neutral** (no body sound, just the pedal's tone controls and reverb)

Right footswitch: tap = next IR, hold = previous IR.
To change the built-in IRs, send new WAV files to Claude and ask for a new build.

## 2a. Add your own IRs over USB (no SD card, no re-flashing)
PureIR (v0.2.0 and newer) can store up to 8 IRs of your own in the pedal's flash memory. They come first in the list, so the first one you upload is the IR the pedal starts on.
1. Plug the USB-C cable into the USB-C socket on the Daisy module (the same one used for flashing). Power the pedal.
2. Close any serial monitor first (only one program can use the port).
3. Drag a `.wav` file onto **pureir-upload.exe** (in the `uploader` folder). Or drag several; they fill the next free slots.
4. Wait for "done". The new IR starts playing straight away. The output is muted while it uploads (a few seconds).
5. Right footswitch: tap = next IR, hold = previous IR.

Other things pureir-upload.exe can do (open a Command Prompt in its folder):
- `pureir-upload list`: show what is stored.
- `pureir-upload delete 2`: empty slot 2.
- `pureir-upload --slot 2 my.wav`: replace what is in slot 2.

Any WAV works (any sample rate, 16/24/32-bit or float, mono or stereo). It is converted to 48 kHz mono and anything longer than 4 seconds is cut off.
Windows may say "unknown publisher" the first time: click More info, then Run anyway.

## 2b. Load more IRs from an SD card (optional)
1. microSD card, 32 GB or smaller, formatted FAT32.
2. On your computer make a folder named `IR` on the card.
3. Copy your `.wav` IR files into it (max 4 s each; longer is cut off). Name them `01 ...`, `02 ...` to set the order.
4. Power the pedal OFF. Put the card in the slot on the left edge (near USB-C). Power ON.
5. The left LED blinks blue while loading, then green.
6. Right footswitch: tap = next IR, hold = previous IR. IRs on the card come first in the list, then the built-in ones.
Left LED red = that file could not be read (the previous IR keeps playing). No card = only the built-in IRs.

## 3. Controls
SW1 (left / middle / right) chooses what the six knobs do. A knob does nothing until you turn it past its stored position. Normal playing: SW1 left.

| Knob | SW1 left: MAIN | SW1 middle: TONE | SW1 right: DYNAMICS |
|---|---|---|---|
| K1 | IR amount | Low shelf | Comp threshold |
| K2 | Reverb amount | Low-mid | Comp ratio |
| K3 | Reverb length | Presence | Release |
| K4 | Output volume | High shelf | Make-up gain |
| K5 | Input level | Low cut | Reverb size |
| K6 | Boost | Piezo comp | Reverb damping |

SW2 / SW3 pick the reverb: both left = Room, SW3 right = Hall, SW2 right = Plate, both right = Spring.
Left footswitch: tap = effect on/off, hold = mute. Right footswitch: tap = next IR, hold = previous IR.

## 4. Lights
Right LED: effect on. Left LED: green ready, blue blink loading, red IR error, magenta processor behind (use a shorter IR), yellow blink muted. Daisy light: on when the processor is more than 90% busy.

## 5. Problems
- No sound: guitar in the LEFT input? Powered? Tap footswitch 1; hold it to un-mute.
- One side only: use both outputs or either one with a mono cable.
- Thin bass: raise Piezo comp. Boomy: lower it.
- Feedback with the effect on: turn Piezo comp down first (SW1 middle, K6), then raise Low cut (K5), lower Reverb amount (K2 on the left) and Output (K4 on the left).
- Hum: shorter cable, move away from computers and power supplies.
- Right footswitch does not change the IR: unplug the power, swap the two footswitches between the FSW_1 and FSW_2 headers, power up and try again. Tell Claude whether the problem stayed with the header or moved with the switch.
- Re-flash: flash.daisy.audio, bootloader first, then PureIR.bin.
