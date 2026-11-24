// convolver.h - zero-latency, full-length partitioned FFT convolution.
//
// An IR of up to kMaxIrSamples (default 4 s @ 48 kHz) is split in two zones:
//
//   zone A  h[0 .. 4096)      64 partitions of 64 samples, FFT size 128.
//                             Runs inside the audio interrupt, once per
//                             64-sample audio block -> no added latency.
//   zone B  h[4096 .. end)    partitions of 2048 samples, FFT size 4096.
//                             Runs in the MAIN LOOP (ServiceBackground()).
//
// Why zone B can run in the background: it starts exactly 2 partitions
// (4096 samples) into the IR.  Input block q is complete at time (q+1)*P, and
// its contribution is needed from time (q+2)*P on, so the background code has a
// full partition period (2048 samples = 42.7 ms) to compute each block.  The
// audio interrupt only copies samples in and out, and checks that the result
// is there (if not it sets the overload flag and plays silence for the tail).
//
// The result is exactly the same as direct convolution with the whole IR.
//
// Switching IRs: the loader fills the *inactive* IrSet, then QueueSwap().  The
// audio thread fades the convolver out (8 blocks), swaps, stays muted for
// 66 blocks (so that tail blocks computed with the old IR are never heard) and
// fades back in (8 blocks).  The input history lives in frequency-domain
// delay lines that don't depend on the IR, so nothing needs to be cleared.
#pragma once

#include "../platform.h"
#include "fft.h"

namespace pureir
{
namespace conv
{
constexpr int   kBlock      = 64; // audio block size
constexpr int   kFftA       = 2 * kBlock;
constexpr int   kBinsA      = kFftA / 2 + 1;
constexpr int   kMaxPartsA  = 64; // power of two
constexpr int   kLenA       = kBlock * kMaxPartsA; // 4096
constexpr int   kPartB      = 2048;
constexpr int   kFftB       = 2 * kPartB;
constexpr int   kBinsB      = kFftB / 2 + 1;
constexpr float kMaxIrSeconds = 4.0f;
constexpr int   kMaxIrSamples = (int)(48000.0f * kMaxIrSeconds);
constexpr int   kMaxPartsB    = (kMaxIrSamples - kLenA + kPartB - 1) / kPartB;
constexpr int   kInRing  = 4;
constexpr int   kOutRing = 3;

constexpr int kFadeBlocks = 8;
constexpr int kHoldBlocks = 2 * (kPartB / kBlock) + 2; // 66

constexpr size_t kIrASize  = (size_t)kMaxPartsA * 2 * kBinsA;
constexpr size_t kIrBSize  = (size_t)kMaxPartsB * 2 * kBinsB;
constexpr size_t kFdlASize = kIrASize;
constexpr size_t kFdlBSize = kIrBSize;

/** An IR converted to the frequency domain, ready to be used. */
struct IrSet
{
    int    len;     // samples
    int    partsA;  // zone A partitions in use
    int    partsB;  // zone B partitions in use
    float* a;       // [partsA][2*kBinsA]
    float* b;       // [partsB][2*kBinsB]
};

/** Memory the convolver needs.  Put the big ones in SDRAM, the small in SRAM. */
struct Memory
{
    float* irA[2];  // kIrASize each
    float* irB[2];  // kIrBSize each   (SDRAM)
    float* fdlA;    // kFdlASize
    float* fdlB;    // kFdlBSize       (SDRAM)
    float* inRing;  // kInRing  * kPartB
    float* outRing; // kOutRing * kPartB
    float* workB;   // background scratch: 3*kFftB + 2*kBinsB floats
    float* buildX;  // IR build scratch: 2*kFftB floats
};
constexpr size_t kWorkBSize  = 3 * (size_t)kFftB + 2 * kBinsB;
constexpr size_t kBuildSize  = 2 * (size_t)kFftB;
} // namespace conv

class Convolver
{
  public:
    using IrSet = conv::IrSet;

    void Init(const conv::Memory& m)
    {
        using namespace conv;
        mem_ = m;
        fftA_.Init();
        fftB_.Init();
        std::memset(mem_.fdlA, 0, kFdlASize * sizeof(float));
        std::memset(mem_.fdlB, 0, kFdlBSize * sizeof(float));
        std::memset(mem_.inRing, 0, kInRing * kPartB * sizeof(float));
        std::memset(mem_.outRing, 0, kOutRing * kPartB * sizeof(float));
        std::memset(xA_, 0, sizeof(xA_));
        sets_[0].a = mem_.irA[0];
        sets_[0].b = mem_.irB[0];
        sets_[1].a = mem_.irA[1];
        sets_[1].b = mem_.irB[1];
        MakeNeutral(&sets_[0]);
        MakeNeutral(&sets_[1]);
        active_  = &sets_[0];
        pending_ = nullptr;
        slotA_ = 0;
        posB_  = 0;
        inCount_ = outCount_ = bgNext_ = 0;
        std::memset(bgPrev_, 0, sizeof(bgPrev_));
        tailValid_ = false;
        swapState_ = kIdle;
        swapCount_ = 0;
        overload_  = false;
        bgPrevValid_ = false;
    }

    // ------------------------------------------------------------------
    // main-thread API
    // ------------------------------------------------------------------

    /** The set the loader may write into.  Only valid while !SwapPending(). */
    IrSet* Inactive() { return active_ == &sets_[0] ? &sets_[1] : &sets_[0]; }
    bool   SwapPending() const { return pending_ != nullptr; }
    const IrSet* Active() const { return active_; }

    void QueueSwap(IrSet* s)
    {
        PUREIR_MEMBAR();
        pending_ = s;
    }

    /** Impulse (pass-through) response. */
    void MakeNeutral(IrSet* s)
    {
        using namespace conv;
        s->len    = 1;
        s->partsA = 1;
        s->partsB = 0;
        for(int k = 0; k < kBinsA; ++k)
        {
            s->a[2 * k]     = 1.f;
            s->a[2 * k + 1] = 0.f;
        }
    }

    /** Convert h[0..len) (48 kHz mono) to spectra in *set.  `yield` (optional)
     *  is called regularly so the caller can keep ServiceBackground() running. */
    void BuildSet(IrSet* set, const float* h, int len, void (*yield)(void*), void* ctx)
    {
        using namespace conv;
        if(len > kMaxIrSamples)
            len = kMaxIrSamples;
        if(len < 1)
        {
            MakeNeutral(set);
            return;
        }
        float* x = mem_.buildX;           // kFftB
        float* z = mem_.buildX + kFftB;   // kFftB
        const int nA = (len + kBlock - 1) / kBlock > kMaxPartsA
                           ? kMaxPartsA
                           : (len + kBlock - 1) / kBlock;
        for(int p = 0; p < nA; ++p)
        {
            for(int i = 0; i < kFftA; ++i)
            {
                const int idx = p * kBlock + i;
                x[i] = (i < kBlock && idx < len) ? h[idx] : 0.f;
            }
            fftA_.Forward(x, set->a + (size_t)p * 2 * kBinsA, z);
        }
        const int tail = len - kLenA;
        const int nB   = tail > 0 ? (tail + kPartB - 1) / kPartB : 0;
        for(int q = 0; q < nB; ++q)
        {
            for(int i = 0; i < kFftB; ++i)
            {
                const int idx = kLenA + q * kPartB + i;
                x[i]          = (i < kPartB && idx < len) ? h[idx] : 0.f;
            }
            fftB_.Forward(x, set->b + (size_t)q * 2 * kBinsB, z);
            if(yield)
                yield(ctx);
        }
        set->len    = len;
        set->partsA = nA;
        set->partsB = nB;
    }

    /** Run pending zone-B work.  Returns true if a block was processed. */
    bool ServiceBackground()
    {
        using namespace conv;
        const uint32_t avail = inCount_;
        if(bgNext_ >= avail)
            return false;
        if(avail - bgNext_ >= (uint32_t)kInRing)
        {
            // we fell too far behind: input blocks were overwritten.  Skip ahead.
            overload_    = true;
            bgNext_      = avail - 1;
            bgPrevValid_ = false;
        }
        const uint32_t q = bgNext_;
        float* x   = mem_.workB;                 // kFftB
        float* acc = x + kFftB;                  // 2*kBinsB
        float* y   = acc + 2 * kBinsB;           // kFftB
        float* z   = y + kFftB;                  // kFftB
        const float* blk = mem_.inRing + (size_t)(q % kInRing) * kPartB;
        if(bgPrevValid_)
            std::memcpy(x, bgPrev_, kPartB * sizeof(float));
        else
            std::memset(x, 0, kPartB * sizeof(float));
        std::memcpy(x + kPartB, blk, kPartB * sizeof(float));
        std::memcpy(bgPrev_, blk, kPartB * sizeof(float));
        bgPrevValid_ = true;

        const int slot = (int)(q % kMaxPartsB);
        fftB_.Forward(x, mem_.fdlB + (size_t)slot * 2 * kBinsB, z);

        const IrSet* s = active_;
        float* out     = mem_.outRing + (size_t)(q % kOutRing) * kPartB;
        const int nB   = s->partsB;
        if(nB > 0)
        {
            std::memset(acc, 0, 2 * kBinsB * sizeof(float));
            for(int k = 0; k < nB; ++k)
            {
                int idx = slot - k;
                if(idx < 0)
                    idx += kMaxPartsB;
                const float* H = s->b + (size_t)k * 2 * kBinsB;
                const float* X = mem_.fdlB + (size_t)idx * 2 * kBinsB;
                for(int b = 0; b < kBinsB; ++b)
                {
                    const float hr = H[2 * b], hi = H[2 * b + 1];
                    const float xr = X[2 * b], xi = X[2 * b + 1];
                    acc[2 * b] += hr * xr - hi * xi;
                    acc[2 * b + 1] += hr * xi + hi * xr;
                }
            }
            fftB_.Inverse(acc, y, z);
            std::memcpy(out, y + kPartB, kPartB * sizeof(float));
        }
        else
            std::memset(out, 0, kPartB * sizeof(float));
        PUREIR_MEMBAR();
        outCount_ = q + 1;
        bgNext_   = q + 1;
        return true;
    }

    /** True if the background thread was late since the last call. */
    bool TakeOverload()
    {
        const bool o = overload_;
        overload_    = false;
        return o;
    }

    // ------------------------------------------------------------------
    // audio-thread API: exactly kBlock samples
    // ------------------------------------------------------------------
    void Process(const float* in, float* out)
    {
        using namespace conv;
        // ---- swap state machine -> gain ramp for this block ------------
        float g0 = 1.f, g1 = 1.f;
        switch(swapState_)
        {
            case kIdle:
                if(pending_)
                {
                    swapState_ = kFadeOut;
                    swapCount_ = 0;
                }
                break;
            default: break;
        }
        if(swapState_ == kFadeOut)
        {
            g0 = 1.f - (float)swapCount_ / kFadeBlocks;
            g1 = 1.f - (float)(swapCount_ + 1) / kFadeBlocks;
            if(++swapCount_ >= kFadeBlocks)
            {
                swapState_ = kSwap;
            }
        }
        else if(swapState_ == kSwap || swapState_ == kHold)
        {
            if(swapState_ == kSwap)
            {
                PUREIR_MEMBAR();
                active_  = pending_;
                pending_ = nullptr;
                swapState_ = kHold;
                swapCount_ = 0;
            }
            g0 = g1 = 0.f;
            if(++swapCount_ >= kHoldBlocks)
            {
                swapState_ = kFadeIn;
                swapCount_ = 0;
            }
        }
        else if(swapState_ == kFadeIn)
        {
            g0 = (float)swapCount_ / kFadeBlocks;
            g1 = (float)(swapCount_ + 1) / kFadeBlocks;
            if(++swapCount_ >= kFadeBlocks)
                swapState_ = kIdle;
        }

        const IrSet* s = active_;

        // ---- zone A -------------------------------------------------------
        float* z   = zA_;
        float* acc = accA_;
        std::memcpy(xA_ + kBlock, in, kBlock * sizeof(float));
        slotA_ = (slotA_ + 1) & (kMaxPartsA - 1);
        fftA_.Forward(xA_, mem_.fdlA + (size_t)slotA_ * 2 * kBinsA, z);
        std::memcpy(xA_, in, kBlock * sizeof(float)); // becomes "previous"
        for(int b = 0; b < 2 * kBinsA; ++b)
            acc[b] = 0.f;
        const int nA = s->partsA;
        for(int k = 0; k < nA; ++k)
        {
            const float* H = s->a + (size_t)k * 2 * kBinsA;
            const float* X
                = mem_.fdlA + (size_t)((slotA_ - k) & (kMaxPartsA - 1)) * 2 * kBinsA;
            for(int b = 0; b < kBinsA; ++b)
            {
                const float hr = H[2 * b], hi = H[2 * b + 1];
                const float xr = X[2 * b], xi = X[2 * b + 1];
                acc[2 * b] += hr * xr - hi * xi;
                acc[2 * b + 1] += hr * xi + hi * xr;
            }
        }
        fftA_.Inverse(acc, yA_, z);
        for(int i = 0; i < kBlock; ++i)
            out[i] = yA_[kBlock + i];

        // ---- zone B (audio side: copy in, add finished tail) --------------
        if(posB_ == 0)
        {
            const uint32_t m = inCount_;
            tailValid_       = false;
            if(m >= 2)
            {
                if(outCount_ >= m - 1)
                    tailValid_ = true;
                else
                    overload_ = true;
            }
        }
        std::memcpy(mem_.inRing + (size_t)(inCount_ % kInRing) * kPartB + posB_,
                    in,
                    kBlock * sizeof(float));
        if(tailValid_)
        {
            const float* t
                = mem_.outRing + (size_t)((inCount_ - 2) % kOutRing) * kPartB + posB_;
            for(int i = 0; i < kBlock; ++i)
                out[i] += t[i];
        }
        posB_ += kBlock;
        if(posB_ >= kPartB)
        {
            posB_ = 0;
            PUREIR_MEMBAR();
            inCount_ = inCount_ + 1;
        }

        // ---- fade ---------------------------------------------------------
        if(g0 != 1.f || g1 != 1.f)
        {
            const float d = (g1 - g0) * (1.0f / kBlock);
            float       g = g0;
            for(int i = 0; i < kBlock; ++i)
            {
                out[i] *= g;
                g += d;
            }
        }
    }

  private:
    enum SwapState
    {
        kIdle,
        kFadeOut,
        kSwap,
        kHold,
        kFadeIn
    };

    conv::Memory mem_;
    RealFft<conv::kFftA> fftA_;
    RealFft<conv::kFftB> fftB_;
    IrSet  sets_[2];
    IrSet* volatile active_;
    IrSet* volatile pending_;

    // zone A
    float xA_[conv::kFftA];
    float yA_[conv::kFftA];
    float zA_[conv::kFftA];
    float accA_[2 * conv::kBinsA];
    int   slotA_;

    // zone B
    int               posB_;
    bool              tailValid_;
    volatile uint32_t inCount_;
    volatile uint32_t outCount_;
    uint32_t          bgNext_;
    float             bgPrev_[conv::kPartB];
    bool              bgPrevValid_;
    volatile bool     overload_;

    // swap
    SwapState swapState_;
    int       swapCount_;
};
} // namespace pureir
