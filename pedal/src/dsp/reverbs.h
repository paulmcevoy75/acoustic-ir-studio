// reverbs.h - four reverb engines sharing one block of memory.
//
//   Room   : 8-line feedback delay network, short lines
//   Hall   : 8-line FDN, long lines, slow modulation
//   Plate  : 8-line FDN, dense input diffusion, brighter, more modulation
//   Spring : two dispersive feedback loops ("boing")
//
// Mono in, stereo out.  All delay memory comes from the caller (SDRAM on the
// pedal), the class itself is small.  Switching engine fades the wet signal
// out, clears the memory a little at a time from Housekeeping() (called once per
// audio block), then fades back in - no clicks, no long ISR stalls.
#pragma once

#include "../platform.h"
#include "filters.h"

namespace pureir
{
class Reverb
{
  public:
    enum Engine
    {
        kRoom = 0,
        kHall,
        kPlate,
        kSpring,
        kNumEngines
    };

    static constexpr int kLines   = 8;
    static constexpr int kLineLen = 4096; // power of two
    static constexpr int kPreLen  = 8192; // power of two (170 ms)
    static constexpr int kApLen   = 512;
    static constexpr int kNumAp   = 4;
    static constexpr int kSpLen   = 2048;
    static constexpr int kSpStages = 10;
    static constexpr size_t kMemFloats
        = (size_t)kLines * kLineLen + kPreLen + (size_t)kNumAp * kApLen + 2 * kSpLen;

    void Init(float fs, float* mem)
    {
        fs_ = fs;
        for(int i = 0; i < kLines; ++i)
            line_[i] = mem + (size_t)i * kLineLen;
        pre_ = mem + (size_t)kLines * kLineLen;
        ap_  = pre_ + kPreLen;
        sp_[0] = ap_ + kNumAp * kApLen;
        sp_[1] = sp_[0] + kSpLen;
        mem_   = mem;
        const float r[4] = {0.31f, 0.43f, 0.53f, 0.67f};
        for(int i = 0; i < 4; ++i)
            lfo_[i].Init(fs, r[i], 1.3f * (float)i);
        engine_ = kRoom;
        pending_ = kRoom;
        decay_ = 2.f;
        size_  = 0.5f;
        damp_  = 0.5f;
        preMs_ = 0.f;
        mod_   = 0.3f;
        clearPos_ = 0;
        phase_    = kRun;
        wet_      = 1.f;
        ClearAll();
        // memory is NOLOAD on the target: clear it right now, synchronously
        std::memset(mem_, 0, kMemFloats * sizeof(float));
        Retune(true);
    }

    /** decay: RT60 seconds, size/damp/mod: 0..1, preMs: 0..150. */
    void SetParams(float decaySec, float size, float damp, float preMs, float mod)
    {
        decay_ = Clampf(decaySec, 0.1f, 20.f);
        size_  = Clampf(size, 0.f, 1.f);
        damp_  = Clampf(damp, 0.f, 1.f);
        preMs_ = Clampf(preMs, 0.f, 150.f);
        mod_   = Clampf(mod, 0.f, 1.f);
        Retune(false);
    }

    void SetEngine(Engine e)
    {
        if(e == pending_ || e >= kNumEngines)
            return;
        pending_ = e;
        if(e != engine_ && phase_ == kRun)
            phase_ = kFadeOut;
    }
    Engine CurrentEngine() const { return engine_; }

    /** Call once per audio block (not per sample). */
    void Housekeeping()
    {
        if(phase_ == kClear)
        {
            constexpr size_t step = 2048;
            size_t           n    = kMemFloats - clearPos_;
            if(n > step)
                n = step;
            std::memset(mem_ + clearPos_, 0, n * sizeof(float));
            clearPos_ += n;
            if(clearPos_ >= kMemFloats)
            {
                ClearAll();
                engine_ = pending_;
                Retune(true);
                phase_ = kFadeIn;
            }
        }
        else if(phase_ == kFadeOut && wet_ < 0.001f)
        {
            clearPos_ = 0;
            phase_    = kClear;
        }
    }

    void Process(float in, float& l, float& r)
    {
        // ---- engine switch gain ------------------------------------------
        const float target = (phase_ == kRun || phase_ == kFadeIn) ? 1.f : 0.f;
        wet_ += kSwitchK * (target - wet_);
        if(phase_ == kFadeIn && wet_ > 0.999f)
            phase_ = kRun;
        if(phase_ == kClear)
        {
            l = r = 0.f;
            return;
        }

        // ---- pre-delay ---------------------------------------------------
        preLen_ += kSmoothK * (preTarget_ - preLen_);
        pre_[preWr_] = in;
        const float x = ReadInterp(pre_, kPreLen - 1, preWr_, preLen_);
        preWr_        = (preWr_ + 1) & (kPreLen - 1);

        if(engine_ == kSpring)
        {
            ProcessSpring(x, l, r);
        }
        else
        {
            ProcessFdn(x, l, r);
        }
        l *= wet_;
        r *= wet_;
    }

  private:
    enum Phase
    {
        kRun,
        kFadeOut,
        kClear,
        kFadeIn
    };
    static constexpr float kSwitchK = 0.0014f; // ~15 ms time constant
    static constexpr float kSmoothK = 0.0004f; // delay length smoothing

    static PUREIR_INLINE float ReadInterp(const float* b, int mask, int wr, float d)
    {
        const float p  = (float)wr - d;
        const float pf = std::floor(p);
        const float fr = p - pf;
        const int   i0 = ((int)pf) & mask;
        const int   i1 = (i0 + 1) & mask;
        return b[i0] + fr * (b[i1] - b[i0]);
    }

    void ClearAll()
    {
        for(int i = 0; i < kLines; ++i)
        {
            lp_[i]  = 0.f;
            wr_[i]  = 0;
        }
        preWr_ = 0;
        for(int i = 0; i < kNumAp; ++i)
            apWr_[i] = 0;
        for(int s = 0; s < 2; ++s)
        {
            spWr_[s] = 0;
            spLp_[s] = 0.f;
            for(int k = 0; k < kSpStages; ++k)
                spZ_[s][k] = 0.f;
        }
    }

    void Retune(bool snap)
    {
        // base delay sets (samples @48k)
        static const float room[kLines]
            = {397, 509, 631, 761, 883, 1013, 1151, 1277};
        static const float hall[kLines]
            = {1087, 1409, 1657, 1931, 2269, 2591, 2909, 3313};
        static const float plate[kLines]
            = {641, 797, 953, 1181, 1367, 1553, 1789, 2003};
        static const float spring[2] = {1789, 1223};
        const float        ratio     = fs_ / 48000.f;
        const float*       base      = room;
        float              scaleLo = 0.6f, scaleHi = 1.2f;
        modScale_ = 2.f;
        dampLo_ = 2500.f;
        dampHi_ = 12000.f;
        apCoef_ = 0.6f;
        switch(engine_)
        {
            case kHall:
                base = hall;
                modScale_ = 6.f;
                dampLo_   = 1800.f;
                apCoef_   = 0.6f;
                break;
            case kPlate:
                base = plate;
                modScale_ = 9.f;
                dampLo_   = 3500.f;
                dampHi_   = 16000.f;
                apCoef_   = 0.72f;
                break;
            default: break;
        }
        const float scale = scaleLo + (scaleHi - scaleLo) * size_;
        for(int i = 0; i < kLines; ++i)
        {
            const float len = base[i] * ratio * scale;
            lenTarget_[i]   = len;
            gain_[i]        = std::pow(10.f, -3.f * len / (fs_ * decay_));
            if(snap)
                len_[i] = len;
        }
        // damping: 0 = bright (dampHi), 1 = dark (dampLo)
        const float fc = dampHi_ * std::pow(dampLo_ / dampHi_, damp_);
        dampA_         = 1.f - std::exp(-kTwoPi * fc / fs_);
        modDepth_      = mod_ * modScale_ * ratio;
        preTarget_     = preMs_ * 0.001f * fs_;
        if(snap)
            preLen_ = preTarget_;
        for(int s = 0; s < 2; ++s)
        {
            const float len = spring[s] * ratio * (0.7f + 0.6f * size_);
            spTarget_[s]    = len;
            float g         = std::pow(10.f, -3.f * len / (fs_ * decay_));
            spGain_[s]      = g > 0.93f ? 0.93f : g;
            if(snap)
                spLen_[s] = len;
        }
        const float sfc = 1500.f + 4500.f * (1.f - damp_);
        spA_            = 1.f - std::exp(-kTwoPi * sfc / fs_);
        if(snap)
        {
            // clear any smoothing residue is unnecessary; lengths are snapped
        }
    }

    PUREIR_INLINE float AllpassStage(int k, float x)
    {
        // Schroeder allpass, delay line in ap_ (fixed lengths per stage)
        static const int lens[kNumAp] = {142, 107, 379, 277};
        float*           b            = ap_ + (size_t)k * kApLen;
        const int        n            = lens[k];
        int&             w            = apWr_[k];
        const int        rp           = w - n < 0 ? w - n + kApLen : w - n;
        const float      d            = b[rp];
        const float      v            = x - apCoef_ * d;
        b[w]                          = v;
        w                             = w + 1 == kApLen ? 0 : w + 1;
        return d + apCoef_ * v;
    }

    void ProcessFdn(float x, float& l, float& r)
    {
        // input diffusion
        float u = x;
        for(int k = 0; k < kNumAp; ++k)
            u = AllpassStage(k, u);

        const float m0 = lfo_[0].Process(), m1 = lfo_[1].Process(),
                    m2 = lfo_[2].Process(), m3 = lfo_[3].Process();
        const float mods[4] = {m0, m1, m2, m3};
        float       y[kLines];
        for(int i = 0; i < kLines; ++i)
        {
            len_[i] += kSmoothK * (lenTarget_[i] - len_[i]);
            const float d = len_[i] + modDepth_ * mods[i & 3];
            float       v = ReadInterp(line_[i], kLineLen - 1, wr_[i], d);
            lp_[i] += dampA_ * (v - lp_[i]);
            y[i] = lp_[i] * gain_[i];
        }
        // outputs (tapped before mixing)
        const float cL = 0.5f, cR = 0.5f;
        l = cL * (y[0] - y[2] + y[4] - y[6]) * kOutScale[engine_];
        r = cR * (y[1] - y[3] + y[5] - y[7]) * kOutScale[engine_];

        // 8x8 Hadamard (orthogonal)
        float a0 = y[0] + y[1], a1 = y[0] - y[1], a2 = y[2] + y[3], a3 = y[2] - y[3];
        float a4 = y[4] + y[5], a5 = y[4] - y[5], a6 = y[6] + y[7], a7 = y[6] - y[7];
        float b0 = a0 + a2, b1 = a1 + a3, b2 = a0 - a2, b3 = a1 - a3;
        float b4 = a4 + a6, b5 = a5 + a7, b6 = a4 - a6, b7 = a5 - a7;
        const float s = 0.35355339f;
        y[0] = (b0 + b4) * s; y[1] = (b1 + b5) * s;
        y[2] = (b2 + b6) * s; y[3] = (b3 + b7) * s;
        y[4] = (b0 - b4) * s; y[5] = (b1 - b5) * s;
        y[6] = (b2 - b6) * s; y[7] = (b3 - b7) * s;

        static const float sign[kLines] = {1, -1, 1, 1, -1, 1, -1, -1};
        const float        inj          = 0.35f * u;
        for(int i = 0; i < kLines; ++i)
        {
            line_[i][wr_[i]] = y[i] + sign[i] * inj;
            wr_[i]           = (wr_[i] + 1) & (kLineLen - 1);
        }
    }

    void ProcessSpring(float x, float& l, float& r)
    {
        float out[2];
        for(int s = 0; s < 2; ++s)
        {
            spLen_[s] += kSmoothK * (spTarget_[s] - spLen_[s]);
            const float d = spLen_[s];
            float       v = ReadInterp(sp_[s], kSpLen - 1, spWr_[s], d);
            out[s]        = v;
            // dispersion: chain of first-order allpasses (frequency dependent delay)
            float z = v;
            for(int k = 0; k < kSpStages; ++k)
            {
                const float a = 0.62f;
                const float yv = a * z + spZ_[s][k];
                spZ_[s][k]     = z - a * yv;
                z              = yv;
            }
            spLp_[s] += spA_ * (z - spLp_[s]);
            sp_[s][spWr_[s]] = 0.5f * x + spGain_[s] * spLp_[s];
            spWr_[s]         = (spWr_[s] + 1) & (kSpLen - 1);
        }
        l = out[0] * kOutScale[kSpring];
        r = out[1] * kOutScale[kSpring];
    }

  public:
    // output calibration (from the host test): steady-state wet RMS is about
    // -3 dB re the dry RMS for white noise at the default 2 s decay.
    static constexpr float kOutScale[kNumEngines] = {2.24f, 3.3f, 2.2f, 1.17f};

  private:
    float  fs_ = 48000.f;
    float* mem_ = nullptr;
    float* line_[kLines];
    float* pre_ = nullptr;
    float* ap_  = nullptr;
    float* sp_[2];
    int    wr_[kLines];
    int    apWr_[kNumAp];
    int    preWr_ = 0;
    int    spWr_[2];
    float  len_[kLines], lenTarget_[kLines], gain_[kLines], lp_[kLines];
    float  spLen_[2], spTarget_[2], spGain_[2], spLp_[2];
    float  spZ_[2][kSpStages];
    float  spA_ = 0.1f;
    float  dampA_ = 0.3f, dampLo_ = 2500.f, dampHi_ = 12000.f;
    float  modDepth_ = 0.f, modScale_ = 2.f, apCoef_ = 0.6f;
    float  preLen_ = 0.f, preTarget_ = 0.f;
    float  decay_, size_, damp_, preMs_, mod_;
    float  wet_ = 1.f;
    Lfo    lfo_[4];
    Engine engine_, pending_;
    Phase  phase_;
    size_t clearPos_ = 0;
};
} // namespace pureir
