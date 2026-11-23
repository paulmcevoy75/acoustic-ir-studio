// compressor.h - feed-forward log-domain compressor with soft knee.
//
// Level detector -> static gain curve (threshold / ratio / knee) -> attack /
// release smoothing applied to the gain reduction in dB -> make-up gain.
// (Giannoulis, Massberg & Reiss, "Digital Dynamic Range Compressor Design".)
// No look-ahead, so it adds no latency.
#pragma once

#include "../platform.h"

namespace pureir
{
class Compressor
{
  public:
    void Init(float fs)
    {
        fs_ = fs;
        SetParams(-20.f, 3.f, 10.f, 150.f, 0.f);
        yl_    = 0.f;
        maxGr_ = 0.f;
    }

    void SetParams(float threshDb, float ratio, float attackMs, float releaseMs, float makeupDb)
    {
        thr_    = threshDb;
        invR_   = 1.f / (ratio < 1.f ? 1.f : ratio);
        aA_     = std::exp(-1.f / (0.001f * attackMs * fs_));
        aR_     = std::exp(-1.f / (0.001f * releaseMs * fs_));
        makeup_ = makeupDb;
    }

    PUREIR_INLINE float Process(float x)
    {
        const float a    = std::fabs(x) + 1e-9f;
        const float xdb  = 6.0205999f * FastLog2(a);
        const float over = xdb - thr_;
        float       yg;
        if(2.f * over < -kKnee)
            yg = xdb;
        else if(2.f * std::fabs(over) <= kKnee)
        {
            const float t = over + 0.5f * kKnee;
            yg            = xdb + (invR_ - 1.f) * t * t / (2.f * kKnee);
        }
        else
            yg = thr_ + over * invR_;
        const float xl = xdb - yg; // gain reduction wanted (>= 0)
        yl_            = xl > yl_ ? aA_ * yl_ + (1.f - aA_) * xl
                                  : aR_ * yl_ + (1.f - aR_) * xl;
        if(yl_ > maxGr_)
            maxGr_ = yl_;
        return x * FastExp2((makeup_ - yl_) * (1.f / 6.0205999f));
    }

    /** Largest gain reduction (dB, positive) seen since the last call. */
    float TakeMaxGainReduction()
    {
        const float g = maxGr_;
        maxGr_        = yl_;
        return g;
    }

    float CurrentGainReductionDb() const { return yl_; }

  private:
    static constexpr float kKnee = 6.f;
    float fs_, thr_, invR_, aA_, aR_, makeup_, yl_, maxGr_;
};
} // namespace pureir
