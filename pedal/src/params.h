// params.h - the user-facing parameters, how the six knobs map onto them, and
// the knob "pickup" logic.
//
// Three banks (selected by the 3-way switch SW1), six knobs each:
//
//   Bank 0  MAIN      K1 IR mix    K2 Reverb mix   K3 Reverb time
//                     K4 Output    K5 Input gain   K6 Boost
//   Bank 1  TONE      K1 Low shelf K2 Low-mid      K3 Presence
//                     K4 High shelf K5 Low cut     K6 Piezo comp
//   Bank 2  DYNAMICS  K1 Threshold K2 Ratio        K3 Release
//                     K4 Make-up   K5 Verb size    K6 Verb damping
//
// Every parameter keeps its own stored value.  When a bank is selected the
// physical knob does nothing until it passes through the stored value
// ("pickup"), so switching banks never makes the sound jump.
#pragma once

#include "platform.h"

namespace pureir
{
constexpr int kNumBanks    = 3;
constexpr int kKnobs       = 6;
constexpr int kNumParams   = kNumBanks * kKnobs;

enum Curve : uint8_t
{
    kLin,
    kExp, // lo * (hi/lo)^x
};

struct ParamDef
{
    const char* name;
    const char* unit;
    float       lo, hi, def;
    Curve       curve;
};

// clang-format off
static const ParamDef kParamDefs[kNumParams] = {
    // ---- bank 0: MAIN
    {"IR Mix",     "%",   0.f,  100.f, 100.f, kLin},
    {"Reverb Mix", "%",   0.f,  100.f,  20.f, kLin},
    {"Reverb Time","s",   0.3f,   8.f,   1.8f, kExp},
    {"Output",     "dB", -24.f,  12.f,   0.f, kLin},
    {"Input Gain", "dB", -12.f,  30.f,   0.f, kLin},
    {"Boost",      "dB",   0.f,  12.f,   0.f, kLin},
    // ---- bank 1: TONE
    {"Low Shelf",  "dB", -12.f,  12.f,   0.f, kLin},
    {"Low-Mid",    "dB", -12.f,  12.f,   0.f, kLin},
    {"Presence",   "dB", -12.f,  12.f,   0.f, kLin},
    {"High Shelf", "dB", -12.f,  12.f,   0.f, kLin},
    {"Low Cut",    "Hz",  20.f, 250.f,  60.f, kExp},
    {"Piezo Comp", "%",    0.f, 100.f, 100.f, kLin},
    // ---- bank 2: DYNAMICS / REVERB
    {"Threshold",  "dB", -40.f,   0.f, -18.f, kLin},
    {"Ratio",      ":1",   1.f,  10.f,   2.5f, kExp},
    {"Release",    "ms",  50.f, 800.f, 200.f, kExp},
    {"Make-up",    "dB",   0.f,  12.f,   0.f, kLin},
    {"Verb Size",  "%",    0.f, 100.f,  50.f, kLin},
    {"Verb Damp",  "%",    0.f, 100.f,  50.f, kLin},
};
// clang-format on

static const char* const kBankNames[kNumBanks]   = {"MAIN", "TONE", "DYNAMICS"};
static const char* const kEngineNames[4]         = {"Room", "Hall", "Plate", "Spring"};

/** knob position 0..1 -> parameter value */
inline float NormToValue(int idx, float x)
{
    const ParamDef& d = kParamDefs[idx];
    x                 = Clampf(x, 0.f, 1.f);
    if(d.curve == kExp)
        return d.lo * std::pow(d.hi / d.lo, x);
    return d.lo + (d.hi - d.lo) * x;
}

/** parameter value -> knob position 0..1 */
inline float ValueToNorm(int idx, float v)
{
    const ParamDef& d = kParamDefs[idx];
    float           x;
    if(d.curve == kExp)
        x = std::log(v / d.lo) / std::log(d.hi / d.lo);
    else
        x = (v - d.lo) / (d.hi - d.lo);
    return Clampf(x, 0.f, 1.f);
}

/** Stored values (as knob positions) plus pickup state for the 6 physical knobs. */
class ParamSet
{
  public:
    void Init()
    {
        for(int i = 0; i < kNumParams; ++i)
            norm_[i] = ValueToNorm(i, kParamDefs[i].def);
        bank_ = 0;
        for(int k = 0; k < kKnobs; ++k)
        {
            picked_[k] = false;
            last_[k]   = -1.f;
        }
    }

    /** Switching banks re-arms pickup of every knob. */
    void SelectBank(int b)
    {
        if(b == bank_)
            return;
        bank_ = b;
        Rearm();
    }
    /** Re-arm pickup of every knob (each one waits until it reaches its stored value). */
    void Rearm()
    {
        for(int k = 0; k < kKnobs; ++k)
            picked_[k] = false;
    }
    int Bank() const { return bank_; }

    /** Feed the physical knob positions (0..1). */
    void UpdateKnobs(const float* knob)
    {
        for(int k = 0; k < kKnobs; ++k)
        {
            const int   idx = bank_ * kKnobs + k;
            const float x   = knob[k];
            if(!picked_[k])
            {
                const float stored = norm_[idx];
                const bool  near   = std::fabs(x - stored) < kPickupWindow;
                const bool  cross  = last_[k] >= 0.f
                                    && ((last_[k] - stored) * (x - stored) <= 0.f);
                if(near || cross)
                    picked_[k] = true;
            }
            if(picked_[k])
                norm_[idx] = x;
            last_[k] = x;
        }
    }

    bool  Picked(int knob) const { return picked_[knob]; }
    float Norm(int idx) const { return norm_[idx]; }
    float Value(int idx) const { return NormToValue(idx, norm_[idx]); }
    void  SetNorm(int idx, float x) { norm_[idx] = Clampf(x, 0.f, 1.f); }

  private:
    static constexpr float kPickupWindow = 0.02f;
    float norm_[kNumParams];
    float last_[kKnobs];
    bool  picked_[kKnobs];
    int   bank_;
};
} // namespace pureir
