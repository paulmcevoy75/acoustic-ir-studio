// ir_loader.h - turn a WAV file (or a builtin sample array) into a clean,
// level-matched, 48 kHz mono impulse response.
//
//   WAV parse (PCM 8/16/24/32, float 32/64, WAVE_FORMAT_EXTENSIBLE, 1..8 ch,
//   8..192 kHz)  ->  fold to mono  ->  remove DC  ->  trim leading silence
//   ->  resample to 48 kHz (windowed sinc, anti-aliased)  ->  trim the noise
//   floor at the end, fade out  ->  normalise to unit energy.
//
// Nothing here knows about the hardware: files are read through IReader so the
// same code is exercised by the PC tests.
#pragma once

#include "platform.h"

namespace pureir
{
class IReader
{
  public:
    virtual size_t   Read(void* dst, size_t n) = 0; // returns bytes read
    virtual bool     Seek(uint32_t pos)        = 0;
    virtual uint32_t Size()                    = 0;
};

enum class IrStatus
{
    Ok,
    NoFile,
    BadWav,
    Unsupported,
    Empty,
    ReadError
};

struct WavInfo
{
    uint16_t format; // 1 = PCM, 3 = IEEE float
    uint16_t channels;
    uint32_t rate;
    uint16_t bits;
    uint32_t dataPos;
    uint32_t dataBytes;
    uint32_t frames;
};

namespace irload
{
constexpr int      kZeroCross = 16;  // sinc zero crossings each side
constexpr int      kTablePerUnit = 256;
constexpr int      kTableSize = kZeroCross * kTablePerUnit + 2;
constexpr uint32_t kMaxRate   = 192000;
constexpr uint32_t kMinRate   = 8000;

inline uint32_t Rd32(const uint8_t* p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16)
           | ((uint32_t)p[3] << 24);
}
inline uint16_t Rd16(const uint8_t* p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}

inline IrStatus ParseWav(IReader& r, WavInfo& w)
{
    uint8_t h[12];
    const uint32_t fsize = r.Size();
    if(fsize < 44 || !r.Seek(0) || r.Read(h, 12) != 12)
        return IrStatus::BadWav;
    if(std::memcmp(h, "RIFF", 4) != 0 || std::memcmp(h + 8, "WAVE", 4) != 0)
        return IrStatus::BadWav;
    bool     haveFmt = false;
    uint32_t pos     = 12;
    w                = WavInfo{};
    while(pos + 8 <= fsize)
    {
        uint8_t ch[8];
        if(!r.Seek(pos) || r.Read(ch, 8) != 8)
            return IrStatus::ReadError;
        const uint32_t size = Rd32(ch + 4);
        const uint32_t body = pos + 8;
        if(std::memcmp(ch, "fmt ", 4) == 0)
        {
            uint8_t f[40] = {0};
            const size_t n = size < 40 ? size : 40;
            if(n < 16 || r.Read(f, n) != n)
                return IrStatus::BadWav;
            w.format   = Rd16(f);
            w.channels = Rd16(f + 2);
            w.rate     = Rd32(f + 4);
            w.bits     = Rd16(f + 14);
            if(w.format == 0xFFFE && n >= 26)
                w.format = Rd16(f + 24); // sub-format GUID starts with the real tag
            haveFmt = true;
        }
        else if(std::memcmp(ch, "data", 4) == 0)
        {
            if(!haveFmt)
                return IrStatus::BadWav;
            uint32_t bytes = size;
            if(bytes == 0xFFFFFFFFu || body + bytes > fsize)
                bytes = fsize - body;
            w.dataPos   = body;
            w.dataBytes = bytes;
            break;
        }
        // a broken size would wrap around and loop forever: stop at the end of the file
        if(size > fsize - body)
            break;
        pos = body + size + (size & 1u);
    }
    if(!haveFmt || w.dataPos == 0)
        return IrStatus::BadWav;
    if(w.channels < 1 || w.channels > 8 || w.rate < kMinRate || w.rate > kMaxRate)
        return IrStatus::Unsupported;
    const bool pcmOk = w.format == 1 && (w.bits == 8 || w.bits == 16 || w.bits == 24 || w.bits == 32);
    const bool fltOk = w.format == 3 && (w.bits == 32 || w.bits == 64);
    if(!pcmOk && !fltOk)
        return IrStatus::Unsupported;
    const uint32_t frameBytes = (uint32_t)w.channels * (w.bits / 8);
    w.frames                  = w.dataBytes / frameBytes;
    if(w.frames == 0)
        return IrStatus::Empty;
    return IrStatus::Ok;
}

/** Read up to maxFrames frames, folded to mono float.  Returns frames read. */
inline size_t ReadMono(IReader&       r,
                       const WavInfo& w,
                       float*         dst,
                       size_t         maxFrames,
                       uint8_t*       io,
                       size_t         ioSize,
                       void (*yield)(void*),
                       void* ctx)
{
    const size_t frameBytes = (size_t)w.channels * (w.bits / 8);
    const size_t perChunk   = ioSize / frameBytes;
    if(perChunk == 0 || !r.Seek(w.dataPos))
        return 0;
    size_t total = w.frames < maxFrames ? w.frames : maxFrames;
    size_t done  = 0;
    const float invCh = 1.0f / (float)w.channels;
    while(done < total)
    {
        size_t n = total - done;
        if(n > perChunk)
            n = perChunk;
        const size_t got = r.Read(io, n * frameBytes) / frameBytes;
        if(got == 0)
            break;
        const uint8_t* p = io;
        for(size_t i = 0; i < got; ++i)
        {
            float acc = 0.f;
            for(int c = 0; c < w.channels; ++c)
            {
                float v;
                if(w.format == 3)
                {
                    if(w.bits == 32)
                    {
                        float f;
                        std::memcpy(&f, p, 4);
                        v = f;
                        p += 4;
                    }
                    else
                    {
                        double d;
                        std::memcpy(&d, p, 8);
                        v = (float)d;
                        p += 8;
                    }
                }
                else if(w.bits == 16)
                {
                    v = (float)(int16_t)Rd16(p) * (1.0f / 32768.0f);
                    p += 2;
                }
                else if(w.bits == 24)
                {
                    int32_t s = (int32_t)(((uint32_t)p[0] << 8) | ((uint32_t)p[1] << 16)
                                          | ((uint32_t)p[2] << 24))
                                >> 8;
                    v = (float)s * (1.0f / 8388608.0f);
                    p += 3;
                }
                else if(w.bits == 32)
                {
                    v = (float)(int32_t)Rd32(p) * (1.0f / 2147483648.0f);
                    p += 4;
                }
                else
                {
                    v = ((float)p[0] - 128.0f) * (1.0f / 128.0f);
                    p += 1;
                }
                acc += v;
            }
            const float m = acc * invCh;
            dst[done + i] = std::isfinite(m) ? m : 0.f; // NaN / inf in a float file would silence the pedal
        }
        done += got;
        if(yield)
            yield(ctx);
        if(got < n)
            break;
    }
    return done;
}

/** Windowed-sinc kernel, half table: T(u) = sinc(u) * BlackmanHarris(u/Z). */
inline void BuildKernel(float* T)
{
    for(int i = 0; i < kTableSize; ++i)
    {
        const double u = (double)i / (double)kTablePerUnit;
        double       s = 1.0;
        if(u > 1e-9)
            s = std::sin(3.14159265358979323846 * u) / (3.14159265358979323846 * u);
        double r = u / (double)kZeroCross;
        double w = 0.0;
        if(r < 1.0)
            w = 0.35875 + 0.48829 * std::cos(3.14159265358979323846 * r)
                + 0.14128 * std::cos(2.0 * 3.14159265358979323846 * r)
                + 0.01168 * std::cos(3.0 * 3.14159265358979323846 * r);
        T[i] = (float)(s * w);
    }
}

/** Resample `n` mono samples from `rateIn` to 48 kHz.  Returns samples written. */
inline size_t Resample(const float* T,
                       const float* src,
                       size_t       n,
                       uint32_t     rateIn,
                       float*       dst,
                       size_t       maxOut,
                       void (*yield)(void*),
                       void* ctx)
{
    if(rateIn == 48000)
    {
        const size_t m = n < maxOut ? n : maxOut;
        std::memcpy(dst, src, m * sizeof(float));
        return m;
    }
    const double ratio = (double)rateIn / 48000.0; // input samples per output sample
    const double fc    = ratio > 1.0 ? 1.0 / ratio : 1.0;
    const double half  = (double)kZeroCross / fc;
    size_t       nOut  = (size_t)((double)n / ratio);
    if(nOut > maxOut)
        nOut = maxOut;
    const float scale = (float)(kTablePerUnit * fc);
    for(size_t o = 0; o < nOut; ++o)
    {
        const double t  = (double)o * ratio;
        long         i0 = (long)std::ceil(t - half);
        long         i1 = (long)std::floor(t + half);
        if(i0 < 0)
            i0 = 0;
        if(i1 >= (long)n)
            i1 = (long)n - 1;
        float acc = 0.f;
        for(long i = i0; i <= i1; ++i)
        {
            const float d   = std::fabs((float)(t - (double)i)) * scale;
            const int   idx = (int)d;
            const float fr  = d - (float)idx;
            const float k   = T[idx] + fr * (T[idx + 1] - T[idx]);
            acc += src[i] * k;
        }
        dst[o] = acc * (float)fc;
        if(yield && (o & 8191u) == 8191u)
            yield(ctx);
    }
    return nOut;
}
} // namespace irload

struct IrBuffers
{
    float*   src;     // srcCap floats
    size_t   srcCap;
    float*   dst;     // dstCap floats
    size_t   dstCap;
    float*   kernel;  // irload::kTableSize floats, filled by BuildKernel()
    uint8_t* io;
    size_t   ioSize;
};

struct IrResult
{
    int      len;       // samples in dst, 48 kHz mono, unit energy
    uint32_t srcRate;
    float    trimmedMs; // leading silence removed
};

/** Shared back half of the pipeline: src (mono, `rate`) -> dst (48 kHz, clean). */
inline IrStatus ProcessIr(const IrBuffers& b,
                          size_t           n,
                          uint32_t         rate,
                          size_t           maxOut,
                          IrResult&        res,
                          void (*yield)(void*),
                          void* ctx)
{
    if(n == 0)
        return IrStatus::Empty;
    float* x = b.src;
    // peak + DC
    float  peak = 0.f;
    double sum  = 0.0;
    for(size_t i = 0; i < n; ++i)
    {
        sum += x[i];
        const float a = std::fabs(x[i]);
        if(a > peak)
            peak = a;
    }
    if(peak < 1e-7f)
        return IrStatus::Empty;
    const float mean = (float)(sum / (double)n);
    peak = 0.f;
    for(size_t i = 0; i < n; ++i)
    {
        x[i] -= mean;
        const float a = std::fabs(x[i]);
        if(a > peak)
            peak = a;
    }
    if(peak < 1e-7f)
        return IrStatus::Empty;
    // leading silence (-50 dB re peak), keep a short run-in
    size_t       start = 0;
    const float  thr   = peak * 0.00316f;
    while(start < n && std::fabs(x[start]) < thr)
        ++start;
    start = start > 16 ? start - 16 : 0;
    res.trimmedMs = 1000.f * (float)start / (float)rate;

    size_t len = irload::Resample(b.kernel, x + start, n - start, rate, b.dst,
                                  maxOut < b.dstCap ? maxOut : b.dstCap, yield, ctx);
    if(len < 8)
        return IrStatus::Empty;
    float* h = b.dst;

    // trailing noise floor (-80 dB re peak)
    float pk = 0.f;
    for(size_t i = 0; i < len; ++i)
        pk = std::fabs(h[i]) > pk ? std::fabs(h[i]) : pk;
    if(pk < 1e-9f)
        return IrStatus::Empty;
    const float tthr = pk * 1e-4f;
    size_t      end  = len;
    while(end > 64 && std::fabs(h[end - 1]) < tthr)
        --end;
    len = end;
    // fade out the last bit so truncation never clicks
    size_t fade = len / 10;
    if(fade > 2400)
        fade = 2400;
    if(fade < 32)
        fade = len < 32 ? len : 32;
    for(size_t i = 0; i < fade; ++i)
    {
        const float t = (float)(i + 1) / (float)fade;           // 0..1
        const float g = 0.5f + 0.5f * std::cos(3.14159265f * t); // 1..0
        h[len - fade + i] *= g;
    }
    // unit energy
    double e = 0.0;
    for(size_t i = 0; i < len; ++i)
        e += (double)h[i] * h[i];
    if(e < 1e-18)
        return IrStatus::Empty;
    const float g = (float)(1.0 / std::sqrt(e));
    for(size_t i = 0; i < len; ++i)
        h[i] *= g;
    res.len     = (int)len;
    res.srcRate = rate;
    return IrStatus::Ok;
}

/** WAV file -> processed IR in b.dst. */
inline IrStatus LoadIrFromWav(IReader&        r,
                              const IrBuffers& b,
                              size_t           maxOut,
                              IrResult&        res,
                              void (*yield)(void*),
                              void* ctx)
{
    WavInfo w;
    IrStatus st = irload::ParseWav(r, w);
    if(st != IrStatus::Ok)
        return st;
    const size_t n = irload::ReadMono(r, w, b.src, b.srcCap, b.io, b.ioSize, yield, ctx);
    if(n == 0)
        return IrStatus::ReadError;
    return ProcessIr(b, n, w.rate, maxOut, res, yield, ctx);
}

/** Builtin 16-bit mono sample array -> processed IR in b.dst. */
inline IrStatus LoadIrFromPcm16(const int16_t*  data,
                                size_t           n,
                                uint32_t         rate,
                                const IrBuffers& b,
                                size_t           maxOut,
                                IrResult&        res,
                                void (*yield)(void*),
                                void* ctx)
{
    if(n > b.srcCap)
        n = b.srcCap;
    for(size_t i = 0; i < n; ++i)
        b.src[i] = (float)data[i] * (1.0f / 32768.0f);
    return ProcessIr(b, n, rate, maxOut, res, yield, ctx);
}
} // namespace pureir
