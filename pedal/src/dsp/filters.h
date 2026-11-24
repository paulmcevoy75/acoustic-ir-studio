// filters.h - biquads (RBJ cookbook), one-pole filters, smoothing helpers.
#pragma once

#include "../platform.h"

namespace pureir
{
/** Transposed direct form II biquad. */
struct Biquad
{
    float b0, b1, b2, a1, a2; // a0 normalised to 1
    float z1, z2;

    void Reset()
    {
        z1 = z2 = 0.f;
    }
    void Passthrough()
    {
        b0 = 1.f;
        b1 = b2 = a1 = a2 = 0.f;
    }
    void Init()
    {
        Passthrough();
        Reset();
    }

    PUREIR_INLINE float Process(float x)
    {
        const float y = b0 * x + z1;
        z1            = b1 * x - a1 * y + z2;
        z2            = b2 * x - a2 * y;
        return y;
    }
    void ProcessBlock(float* x, int n)
    {
        for(int i = 0; i < n; ++i)
            x[i] = Process(x[i]);
    }

    void Set(float B0, float B1, float B2, float A0, float A1, float A2)
    {
        const float inv = 1.0f / A0;
        b0              = B0 * inv;
        b1              = B1 * inv;
        b2              = B2 * inv;
        a1              = A1 * inv;
        a2              = A2 * inv;
    }

    static float Warp(float fs, float f)
    {
        return kTwoPi * Clampf(f, 5.f, 0.45f * fs) / fs;
    }

    void SetHighpass(float fs, float f, float q)
    {
        const float w = Warp(fs, f), c = std::cos(w), al = std::sin(w) / (2.f * q);
        Set((1 + c) * 0.5f, -(1 + c), (1 + c) * 0.5f, 1 + al, -2 * c, 1 - al);
    }
    void SetLowpass(float fs, float f, float q)
    {
        const float w = Warp(fs, f), c = std::cos(w), al = std::sin(w) / (2.f * q);
        Set((1 - c) * 0.5f, 1 - c, (1 - c) * 0.5f, 1 + al, -2 * c, 1 - al);
    }
    void SetPeaking(float fs, float f, float q, float gainDb)
    {
        const float A = std::pow(10.f, gainDb / 40.f);
        const float w = Warp(fs, f), c = std::cos(w), al = std::sin(w) / (2.f * q);
        Set(1 + al * A, -2 * c, 1 - al * A, 1 + al / A, -2 * c, 1 - al / A);
    }
    void SetLowShelf(float fs, float f, float q, float gainDb)
    {
        const float A  = std::pow(10.f, gainDb / 40.f);
        const float w  = Warp(fs, f), c = std::cos(w), s = std::sin(w);
        const float al = s / (2.f * q), t = 2.f * std::sqrt(A) * al;
        Set(A * ((A + 1) - (A - 1) * c + t),
            2 * A * ((A - 1) - (A + 1) * c),
            A * ((A + 1) - (A - 1) * c - t),
            (A + 1) + (A - 1) * c + t,
            -2 * ((A - 1) + (A + 1) * c),
            (A + 1) + (A - 1) * c - t);
    }
    void SetHighShelf(float fs, float f, float q, float gainDb)
    {
        const float A  = std::pow(10.f, gainDb / 40.f);
        const float w  = Warp(fs, f), c = std::cos(w), s = std::sin(w);
        const float al = s / (2.f * q), t = 2.f * std::sqrt(A) * al;
        Set(A * ((A + 1) + (A - 1) * c + t),
            -2 * A * ((A - 1) + (A + 1) * c),
            A * ((A + 1) + (A - 1) * c - t),
            (A + 1) - (A - 1) * c + t,
            2 * ((A - 1) - (A + 1) * c),
            (A + 1) - (A - 1) * c - t);
    }
    /** First-order (s+za)/(s+pa) with za,pa in Hz, bilinear transform. */
    void SetFirstOrder(float fs, float zeroHz, float poleHz)
    {
        const float c = 2.f * fs;
        const float a = kTwoPi * zeroHz, b = kTwoPi * poleHz;
        Set(c + a, a - c, 0.f, c + b, b - c, 0.f);
    }
    /** First-order high-pass at f (used as DC blocker). */
    void SetDcBlock(float fs, float f)
    {
        const float c = 2.f * fs, a = kTwoPi * f;
        Set(c, -c, 0.f, c + a, a - c, 0.f);
    }
};

/** One-pole low-pass. */
struct OnePole
{
    float a, y;
    void Init()
    {
        a = 1.f;
        y = 0.f;
    }
    void SetCutoff(float fs, float f)
    {
        a = 1.f - std::exp(-kTwoPi * Clampf(f, 5.f, 0.45f * fs) / fs);
    }
    PUREIR_INLINE float Process(float x)
    {
        y += a * (x - y);
        return y;
    }
};

/** Exponential smoother with per-sample update. */
struct Smoother
{
    float v, k;
    void Init(float value, float coef)
    {
        v = value;
        k = coef;
    }
    PUREIR_INLINE float Process(float target)
    {
        v += k * (target - v);
        return v;
    }
};

/** Smoothing coefficient for a one-pole with the given time constant. */
PUREIR_INLINE float TimeConstCoef(float fs, float seconds)
{
    return 1.f - std::exp(-1.f / (fs * seconds));
}

/** "Magic circle" sine LFO, no trig per sample. */
struct Lfo
{
    float s, c, e;
    void Init(float fs, float hz, float phase = 0.f)
    {
        e = 2.f * std::sin(kPi * hz / fs);
        s = std::sin(phase);
        c = std::cos(phase);
    }
    PUREIR_INLINE float Process()
    {
        s += e * c;
        c -= e * s;
        return s;
    }
};

/** Soft limiter: transparent below 0.7, smoothly bounded at 1.0. */
PUREIR_INLINE float SoftLimit(float x)
{
    const float ax = std::fabs(x);
    if(ax <= 0.7f)
        return x;
    const float y = 0.7f + 0.3f * FastTanh((ax - 0.7f) * (1.0f / 0.3f));
    return x < 0.f ? -y : y;
}
} // namespace pureir
