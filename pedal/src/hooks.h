// hooks.h - optional extension points for builds that add their own code to PureIR.
//
// PureIR itself does nothing in any of these: main.cpp has empty default versions
// (weak symbols).  A build that wants one adds its own .cpp with a normal definition
// (make EXTRA_SOURCES=src/mine.cpp) and the linker uses that one instead.
//
// Also overridable at build time (EXTRA_DEFS=-D...): PUREIR_VERSION, PUREIR_USB_TAG
// (first word of the USB PING reply, default "PUREIR") and PUREIR_LINK_TAG (display
// frame tag, default "PUREIR"), and TARGET=Name in make for the .bin name.
#pragma once
#include <cstdint>

#include "chain.h"
#include "params.h"
#include "upload.h"

namespace pureir
{
namespace hooks
{
/** What ReadControls saw this block (audio callback, once per block). */
struct Controls
{
    int   bank;              // 0..2 from the bank switch
    float knob[kKnobs];      // 0..1, may be changed by the hook
    bool  fsw1Pressed, fsw1Rising;
    // set by the hook:
    bool knobsTaken   = false; // true: the knobs do not drive the normal parameters this block
    bool suppressFsw1 = false; // true: no left-footswitch tap/hold action this block
};

/** Everything a hook may need to reach. */
struct Context
{
    Chain&           chain;
    ParamSet&        params;
    store::Uploader& uploader;
};

/** Once, after the signal chain is set up and before audio starts. */
void Setup(Context& c);
/** Audio callback, every block, after the controls are read and before they are used. */
void OnControls(Context& c, Controls& k);
/** Main loop, after an IR has been loaded. usbSlot is the flash slot (-1 for SD or built-in),
 *  info is that slot's header data (nullptr if not a USB slot). */
void OnIrLoaded(Context& c, int usbSlot, const store::SlotInfo* info);
/** Main loop, every pass. */
void Poll(Context& c, uint32_t nowMs);
/** Left LED, normal (green) state only: may change the colour. */
void NormalLed(Context& c, uint32_t nowMs, float& r, float& g, float& b);
} // namespace hooks
} // namespace pureir
