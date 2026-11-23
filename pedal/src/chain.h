// chain.h - the complete signal path for one mono piezo input, stereo output.
//
//  in -> DC block / piezo compensation -> input gain -> low cut
//     -> [ dry <-> long IR ] blend -> 4-band EQ -> compressor -> boost
//     -> reverb (parallel, mono in / stereo out) -> output level -> soft limit
//     -> bypass crossfade
//
// ProcessBlock() works on exactly conv::kBlock samples.
#pragma once

#include "dsp/compressor.h"
#include "dsp/convolver.h"
#include "dsp/filters.h"
#include "dsp/reverbs.h"
#include "params.h"

namespace pureir
{
constexpr float kPiezoHpHz  = 130.f; // high-pass formed by the input stage + piezo
constexpr float kPiezoPoleHz = 25.f; // how far down the compensation extends

struct Meters
{
    float inPeak, outPeak; // linear, since last read
    float grDb;            // max gain reduction (dB, positive)
    bool  clip;
};

class Chain
{
  public:
    void Init(const conv::Memory& cm, float* reverbMem)
    {
        conv_.Init(cm);
        comp_.Init(kSampleRate);
        verb_.Init(kSampleRate, reverbMem);
        dc_.Init();
        dc_.SetDcBlock(kSampleRate, 12.f);
        piezo_.Init();
        piezo_.SetFirstOrder(kSampleRate, kPiezoHpHz, kPiezoPoleHz);
        lowCut_.Init();
        lowCut2_.Init();
        for(auto& b : eq_)
            b.Init();
        const float k = TimeConstCoef(kSampleRate, 0.01f);
        sInGain_.Init(1.f, k);
        sIrMix_.Init(1.f, k);
        sRevMix_.Init(0.f, k);
        sOut_.Init(1.f, k);
        sBoost_.Init(1.f, k);
        sComp_.Init(1.f, k);
        sBypass_.Init(1.f, TimeConstCoef(kSampleRate, 0.012f));
        sMute_.Init(1.f, TimeConstCoef(kSampleRate, 0.012f));
        tIn_ = tOut_ = 0.f;
        clip_        = false;
        bypass_ = mute_ = false;
        for(int i = 0; i < 6; ++i)
            cache_[i] = -1e9f;
        std::memset(dry_, 0, sizeof(dry_));
        float v[kNumParams];
        for(int i = 0; i < kNumParams; ++i)
            v[i] = kParamDefs[i].def;
        SetParams(v, Reverb::kRoom);
    }

    Convolver& Conv() { return conv_; }
    Reverb&    Verb() { return verb_; }

    void SetBypass(bool b) { bypass_ = b; }
    void SetMute(bool m) { mute_ = m; }
    bool Bypassed() const { return bypass_; }
    bool Muted() const { return mute_; }

    /** v[] holds the parameter values in kParamDefs units. */
    void SetParams(const float* v, Reverb::Engine engine)
    {
        irMix_    = v[0] * 0.01f;
        revMix_   = v[1] * 0.01f;
        inGain_   = DbToLin(v[4]);
        outGain_  = DbToLin(v[3]);
        boost_    = DbToLin(v[5]);
        compAmt_  = v[11] * 0.01f;
        const float decay = v[2];
        // shelves / peaks only recomputed when they change
        Eq(0, v[6], [&](float g) { eq_[0].SetLowShelf(kSampleRate, 120.f, 0.7f, g); });
        Eq(1, v[7], [&](float g) { eq_[1].SetPeaking(kSampleRate, 400.f, 1.0f, g); });
        Eq(2, v[8], [&](float g) { eq_[2].SetPeaking(kSampleRate, 2500.f, 0.8f, g); });
        Eq(3, v[9], [&](float g) { eq_[3].SetHighShelf(kSampleRate, 7000.f, 0.7f, g); });
        if(Changed(4, v[10]))
        {
            lowCut_.SetHighpass(kSampleRate, v[10], 0.7071f);
            lowCut2_.SetHighpass(kSampleRate, v[10], 0.7071f);
        }
        const bool compOn = v[13] > 1.05f;
        compOn_           = compOn;
        if(Changed(5, v[12] + 1000.f * v[13] + 1e6f * v[14] + 1e9f * v[15]))
            comp_.SetParams(v[12], v[13], 12.f, v[14], v[15]);
        verb_.SetEngine(engine);
        verb_.SetParams(decay, v[16] * 0.01f, v[17] * 0.01f, 12.f, 0.4f);
    }

    /** Optional block hooks for builds that add their own processing (null = none).
     *  input:  after DC block / piezo comp / input gain, before low cut + limiter.
     *  postIr: on the dry/IR blend, before the EQ.  Both run in the audio callback. */
    using BlockHook = void (*)(float* x, int n, void* ctx);
    void SetHooks(BlockHook input, BlockHook postIr, void* ctx)
    {
        hookInput_  = input;
        hookPostIr_ = postIr;
        hookCtx_    = ctx;
    }

    /** exactly conv::kBlock samples */
    void ProcessBlock(const float* in, float* outL, float* outR)
    {
        using namespace conv;
        float x[kBlock], y[kBlock];
        verb_.Housekeeping();

        float pkIn = 0.f;
        for(int i = 0; i < kBlock; ++i)
        {
            const float s = in[i];
            pkIn          = std::fabs(s) > pkIn ? std::fabs(s) : pkIn;
            float v       = dc_.Process(s);
            const float c = sComp_.Process(compAmt_);
            v             = v + c * (piezo_.Process(v) - v);
            x[i] = v * sInGain_.Process(inGain_);
        }
        if(hookInput_)
            hookInput_(x, kBlock, hookCtx_);
        for(int i = 0; i < kBlock; ++i)
            x[i] = SoftLimit(lowCut2_.Process(lowCut_.Process(x[i])));
        // IR: always run, so the history is right when the mix comes up
        conv_.Process(x, y);

        float pkOut = 0.f;
        for(int i = 0; i < kBlock; ++i)
        {
            const float m = sIrMix_.Process(irMix_);
            y[i]          = x[i] + m * (y[i] - x[i]);
        }
        if(hookPostIr_)
            hookPostIr_(y, kBlock, hookCtx_);
        for(int i = 0; i < kBlock; ++i)
        {
            float v = y[i];
            for(auto& b : eq_)
                v = b.Process(v);
            if(compOn_)
                v = comp_.Process(v);
            v *= sBoost_.Process(boost_);

            float wl, wr;
            verb_.Process(v, wl, wr);
            const float rm = sRevMix_.Process(revMix_);
            const float dl = v * (1.f - 0.5f * rm); // keep the dry path strong
            float       l  = dl + rm * wl;
            float       r  = dl + rm * wr;
            const float og = sOut_.Process(outGain_);
            l *= og;
            r *= og;

            const float a = std::fabs(l) > std::fabs(r) ? std::fabs(l) : std::fabs(r);
            if(a > 0.98f)
                clip_ = true;
            l = SoftLimit(l);
            r = SoftLimit(r);

            // bypass: raw input to both outputs (unity), crossfaded
            const float bp = sBypass_.Process(bypass_ ? 0.f : 1.f);
            const float mt = sMute_.Process(mute_ ? 0.f : 1.f);
            const float d  = in[i];
            l              = (d + bp * (l - d)) * mt;
            r              = (d + bp * (r - d)) * mt;
            outL[i]        = l;
            outR[i]        = r;
            pkOut          = std::fabs(l) > pkOut ? std::fabs(l) : pkOut;
        }
        if(pkIn > tIn_)
            tIn_ = pkIn;
        if(pkOut > tOut_)
            tOut_ = pkOut;
    }

    /** Read-and-clear the meters (call from the main loop). */
    Meters TakeMeters()
    {
        Meters m;
        m.inPeak  = tIn_;
        m.outPeak = tOut_;
        m.grDb    = comp_.TakeMaxGainReduction();
        m.clip    = clip_;
        tIn_ = tOut_ = 0.f;
        clip_        = false;
        return m;
    }

  private:
    bool Changed(int slot, float v)
    {
        if(v == cache_[slot])
            return false;
        cache_[slot] = v;
        return true;
    }
    template <typename F>
    void Eq(int band, float gainDb, F f)
    {
        if(eqCache_[band] == gainDb)
            return;
        eqCache_[band] = gainDb;
        f(gainDb);
    }

    Convolver  conv_;
    Compressor comp_;
    Reverb     verb_;
    Biquad     dc_, piezo_, lowCut_, lowCut2_, eq_[4];
    Smoother   sInGain_, sIrMix_, sRevMix_, sOut_, sBoost_, sComp_, sBypass_, sMute_;
    float      irMix_ = 1.f, revMix_ = 0.f, inGain_ = 1.f, outGain_ = 1.f, boost_ = 1.f,
          compAmt_ = 1.f;
    bool  compOn_ = false;
    bool  bypass_, mute_, clip_;
    float tIn_, tOut_;
    float cache_[6];
    float eqCache_[4] = {1e9f, 1e9f, 1e9f, 1e9f};
    float dry_[conv::kBlock];
    BlockHook hookInput_ = nullptr, hookPostIr_ = nullptr;
    void*     hookCtx_   = nullptr;
};
} // namespace pureir
