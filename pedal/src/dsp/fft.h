// fft.h - real FFT built on a half-length radix-2 complex FFT.
//
//   RealFft<N>::Forward : N real samples  -> N/2+1 complex bins  (unnormalised)
//   RealFft<N>::Inverse : N/2+1 bins      -> N real samples      (includes 1/N)
//
// Spectra are stored interleaved (re,im), bins 0..N/2, so a spectrum is
// 2*(N/2+1) floats.  Bins 0 and N/2 have im == 0.
//
// The object only holds read-only tables after Init(), so the SAME instance may
// be used from the audio interrupt and from the main loop at the same time, as
// long as each caller brings its own scratch buffer.
#pragma once

#include "../platform.h"

namespace pureir
{
template <int N>
class RealFft
{
  public:
    static_assert(N >= 16 && (N & (N - 1)) == 0, "N must be a power of two");
    static constexpr int kM    = N / 2;
    static constexpr int kBins = N / 2 + 1;

    void Init()
    {
        for(int k = 0; k <= kM; ++k)
        {
            const double a = 6.283185307179586476925 * (double)k / (double)N;
            tw_[2 * k]     = (float)std::cos(a);
            tw_[2 * k + 1] = (float)-std::sin(a);
        }
        int bits = 0;
        while((1 << bits) < kM)
            ++bits;
        for(int i = 0; i < kM; ++i)
        {
            int r = 0;
            for(int b = 0; b < bits; ++b)
                if(i & (1 << b))
                    r |= 1 << (bits - 1 - b);
            rev_[i] = (uint16_t)r;
        }
    }

    /** x: N reals (not modified).  X: 2*kBins floats.  z: scratch, 2*kM floats. */
    void Forward(const float* x, float* X, float* z) const
    {
        for(int k = 0; k < kM; ++k)
        {
            const int r = rev_[k];
            z[2 * r]     = x[2 * k];
            z[2 * r + 1] = x[2 * k + 1];
        }
        Fft(z);
        X[0]              = z[0] + z[1];
        X[1]              = 0.f;
        X[2 * kM]         = z[0] - z[1];
        X[2 * kM + 1]     = 0.f;
        for(int k = 1; k < kM; ++k)
        {
            const float ar = z[2 * k], ai = z[2 * k + 1];
            const float br = z[2 * (kM - k)], bi = -z[2 * (kM - k) + 1];
            const float fer = 0.5f * (ar + br), fei = 0.5f * (ai + bi);
            const float dr = 0.5f * (ar - br), di = 0.5f * (ai - bi);
            const float for_ = di, foi = -dr; // (a-b)/(2i)
            const float wr = tw_[2 * k], wi = tw_[2 * k + 1];
            X[2 * k]     = fer + wr * for_ - wi * foi;
            X[2 * k + 1] = fei + wr * foi + wi * for_;
        }
    }

    /** X: 2*kBins floats (not modified).  x: N reals out.  z: scratch 2*kM. */
    void Inverse(const float* X, float* x, float* z) const
    {
        for(int k = 0; k < kM; ++k)
        {
            const float ar = X[2 * k], ai = X[2 * k + 1];
            const float br = X[2 * (kM - k)], bi = -X[2 * (kM - k) + 1];
            const float er = 0.5f * (ar + br), ei = 0.5f * (ai + bi);
            const float dr = 0.5f * (ar - br), di = 0.5f * (ai - bi);
            const float wr = tw_[2 * k], wi = tw_[2 * k + 1];
            const float orr = dr * wr + di * wi; // D * conj(W)
            const float oi  = di * wr - dr * wi;
            const float zr = er - oi, zi = ei + orr; // E + i*O
            const int   r  = rev_[k];
            z[2 * r]       = zr;
            z[2 * r + 1]   = -zi; // conjugate trick for the inverse
        }
        Fft(z);
        const float s = 1.0f / (float)kM;
        for(int k = 0; k < kM; ++k)
        {
            x[2 * k]     = z[2 * k] * s;
            x[2 * k + 1] = -z[2 * k + 1] * s;
        }
    }

  private:
    // in-place radix-2 DIT; input already in bit-reversed order
    void Fft(float* z) const
    {
        for(int len = 2; len <= kM; len <<= 1)
        {
            const int half = len >> 1;
            const int step = N / len;
            for(int i = 0; i < kM; i += len)
            {
                float* a = z + 2 * i;
                float* b = a + 2 * half;
                for(int j = 0; j < half; ++j)
                {
                    const float wr = tw_[2 * j * step];
                    const float wi = tw_[2 * j * step + 1];
                    const float tr = b[2 * j] * wr - b[2 * j + 1] * wi;
                    const float ti = b[2 * j] * wi + b[2 * j + 1] * wr;
                    b[2 * j]       = a[2 * j] - tr;
                    b[2 * j + 1]   = a[2 * j + 1] - ti;
                    a[2 * j] += tr;
                    a[2 * j + 1] += ti;
                }
            }
        }
    }

    float    tw_[2 * (kM + 1)];
    uint16_t rev_[kM];
};
} // namespace pureir
