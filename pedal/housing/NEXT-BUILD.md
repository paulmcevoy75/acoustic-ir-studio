# Housing: changes saved for the next build (not built yet)

Requested 2026-11-01. Current files (tray v4, lid v2) are unchanged.

1. **Remove the bars between the jack slots** on the back wall (the five 1/4" jacks and the DC jack), so the back wall has one long opening.
2. ~~Remove the bar between the microSD slot and the Daisy USB-C slot.~~ No longer applies: after the first print the two holes sit at different heights (SD low, Daisy USB-C high), so they stay separate.
3. **Join the LED1 hole in the lid to the pot window above it** (a neck from the round hole up to the window) for looks.

Things to decide when we build it:
- Without bars, the roof over a long slot has nothing to hold it up while printing. A 160 mm roof over the back jacks needs supports, or the wall can be cut down to an open notch the height of the jacks (then the lid rests on less wall). Pick one of these before building.
- If the back wall gets an open notch, the lid skirt along that edge needs a matching cut so it does not hang in the air.

Where it is in code: `BACK`, `DC` and the left-wall slots in `housing/make_base.py`, and `LID_WINDOWS` / `LID_LED_HOLES` for the lid.
