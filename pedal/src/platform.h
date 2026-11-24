// platform.h - tiny portability layer so the DSP code builds both for the
// Daisy Seed (ARM Cortex-M7) and on a PC for the host tests.
#pragma once

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>

#if defined(__arm__)
// ---- Daisy Seed -----------------------------------------------------------
//  .axi_bss  : 256 KB D2 SRAM (RAM_D2, see PureIR_sram.lds); the section
//              keeps its historical name "axi_bss"
//  .sdram_bss: 64 MB external SDRAM
//  Both are NOLOAD sections: the startup code does NOT clear them, so every
//  object placed there must be initialised explicitly (all our classes have an
//  Init() that does this).  SDRAM does not exist yet when C++ static
//  constructors run, so objects in SDRAM must be plain arrays.
#define PUREIR_AXI_BSS __attribute__((section(".axi_bss"), aligned(32)))
#define PUREIR_SDRAM_BSS __attribute__((section(".sdram_bss"), aligned(32)))
#define PUREIR_MEMBAR() __asm volatile("dmb 0xF" ::: "memory")
#define PUREIR_INLINE inline __attribute__((always_inline))
#else
// ---- PC (host tests) ------------------------------------------------------
#include <atomic>
#define PUREIR_AXI_BSS alignas(32)
#define PUREIR_SDRAM_BSS alignas(32)
#define PUREIR_MEMBAR() std::atomic_thread_fence(std::memory_order_seq_cst)
#define PUREIR_INLINE inline
#endif

namespace pureir
{
constexpr float kPi      = 3.14159265358979323846f;
constexpr float kTwoPi   = 6.28318530717958647692f;
constexpr float kSampleRate = 48000.0f;

PUREIR_INLINE float Clampf(float x, float lo, float hi)
{
    return x < lo ? lo : (x > hi ? hi : x);
}

PUREIR_INLINE float DbToLin(float db) { return std::pow(10.0f, db * 0.05f); }

// --- fast log2 / exp2 (polynomial fits, max error 2e-4 / 8e-6) --------------
PUREIR_INLINE float FastLog2(float x)
{
    uint32_t b;
    std::memcpy(&b, &x, 4);
    const float e = (float)((int)((b >> 23) & 255) - 127);
    b             = (b & 0x007FFFFFu) | 0x3F800000u;
    float m;
    std::memcpy(&m, &b, 4); // m in [1,2)
    const float p = ((((-0.0791503658f * m + 0.628815729f) * m - 2.0810602f) * m
                      + 4.02837277f)
                     * m
                     - 2.49677377f);
    return e + p;
}

PUREIR_INLINE float FastExp2(float x)
{
    x           = Clampf(x, -120.f, 120.f);
    const float fl = std::floor(x);
    const float f  = x - fl; // [0,1)
    const float q  = (((0.0136765311f * f + 0.0516668774f) * f + 0.241710262f) * f
                     + 0.692931289f)
                        * f
                    + 1.00000728f;
    int32_t     ib = ((int32_t)fl + 127) << 23;
    float       s;
    std::memcpy(&s, &ib, 4);
    return q * s;
}

// tanh-like soft clipper (Pade), exactly +-1 beyond |x|>=3
PUREIR_INLINE float FastTanh(float x)
{
    if(x > 3.f) return 1.f;
    if(x < -3.f) return -1.f;
    const float x2 = x * x;
    return x * (27.f + x2) / (27.f + 9.f * x2);
}
} // namespace pureir
