// Host tests: g++ -std=c++17 -O2 -I../src host_test.cpp
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <random>
#include <complex>
#include "chain.h"
#include "ir_loader.h"
#include "link.h"
using namespace pureir;

static int fails = 0;
#define CHECK(c, ...) do { if(!(c)) { ++fails; printf("FAIL %s:%d: ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); } } while(0)

static std::mt19937 rng(1234);
static float Rand() { return std::uniform_real_distribution<float>(-1.f, 1.f)(rng); }

// ---------------------------------------------------------------- FFT
static void TestFft()
{
    static RealFft<128> f; f.Init();
    float x[128], X[2 * 65], y[128], z[128];
    for(auto& v : x) v = Rand();
    f.Forward(x, X, z);
    double maxe = 0;
    for(int k = 0; k <= 64; ++k)
    {
        std::complex<double> s = 0;
        for(int n = 0; n < 128; ++n) s += (double)x[n] * std::polar(1.0, -2 * M_PI * k * n / 128);
        maxe = std::fmax(maxe, std::abs(s - std::complex<double>(X[2 * k], X[2 * k + 1])));
    }
    CHECK(maxe < 1e-4, "fft vs dft err %g", maxe);
    f.Inverse(X, y, z);
    double e = 0; for(int i = 0; i < 128; ++i) e = std::fmax(e, std::fabs(x[i] - y[i]));
    CHECK(e < 1e-5, "fft roundtrip %g", e);
    printf("fft ok (dft err %.2g, roundtrip %.2g)\n", maxe, e);
}

// ---------------------------------------------------------------- convolver
struct ConvMem
{
    std::vector<float> irA0, irA1, irB0, irB1, fdlA, fdlB, inR, outR, work, build;
    conv::Memory m;
    ConvMem()
    {
        using namespace conv;
        irA0.assign(kIrASize, 0); irA1.assign(kIrASize, 0);
        irB0.assign(kIrBSize, 0); irB1.assign(kIrBSize, 0);
        fdlA.assign(kFdlASize, 0); fdlB.assign(kFdlBSize, 0);
        inR.assign(kInRing * kPartB, 0); outR.assign(kOutRing * kPartB, 0);
        work.assign(kWorkBSize, 0); build.assign(kBuildSize, 0);
        m = {{irA0.data(), irA1.data()}, {irB0.data(), irB1.data()}, fdlA.data(), fdlB.data(),
             inR.data(), outR.data(), work.data(), build.data()};
    }
};

static void DirectConv(const std::vector<float>& x, const std::vector<float>& h, std::vector<float>& y)
{
    y.assign(x.size(), 0);
    for(size_t n = 0; n < x.size(); ++n)
    {
        double a = 0;
        size_t kmax = std::min(h.size(), n + 1);
        for(size_t k = 0; k < kmax; ++k) a += (double)h[k] * x[n - k];
        y[n] = (float)a;
    }
}

static void TestConvolver(int irLen, bool randomBg)
{
    ConvMem mem; Convolver c; c.Init(mem.m);
    std::vector<float> h(irLen);
    for(int i = 0; i < irLen; ++i) h[i] = Rand() * std::exp(-3.0f * i / irLen);
    auto* set = c.Inactive();
    c.BuildSet(set, h.data(), irLen, nullptr, nullptr);
    c.QueueSwap(set);
    const int blocks = (irLen + 20000) / 64 + 400;
    std::vector<float> x((size_t)blocks * 64), y(x.size()), ref;
    for(auto& v : x) v = Rand() * 0.5f;
    // run until the swap is done silently (feed zeros), then restart from clean state
    std::vector<float> zeros(64, 0.f), tmp(64);
    for(int b = 0; b < 200; ++b) { c.Process(zeros.data(), tmp.data()); while(c.ServiceBackground()) {} }
    // skip the first samples of x so history is empty -> zeros until the end of swap are in history anyway
    std::mt19937 r2(5);
    for(int b = 0; b < blocks; ++b)
    {
        c.Process(&x[b * 64], &y[b * 64]);
        if(randomBg) { if(r2() % 3) c.ServiceBackground(); }  // background sometimes late
        else while(c.ServiceBackground()) {}
        // background must not fall more than 1 block behind (kept by service every call)
        if(randomBg && (b % 8 == 7)) while(c.ServiceBackground()) {}
    }
    DirectConv(x, h, ref);
    double maxe = 0, maxr = 0;
    for(size_t i = 0; i < x.size(); ++i) { maxe = std::fmax(maxe, std::fabs(y[i] - ref[i])); maxr = std::fmax(maxr, std::fabs(ref[i])); }
    bool ov = c.TakeOverload();
    printf("convolver len=%d bg=%s: max err %.3g (peak %.3g) overload=%d\n", irLen, randomBg ? "rand" : "prompt", maxe, maxr, ov);
    CHECK(maxe < 2e-3 * (maxr + 1e-3) + 1e-4, "convolver mismatch len=%d err=%g", irLen, maxe);
}

// ---------------------------------------------------------------- compressor
static void TestCompressor()
{
    Compressor c; c.Init(48000.f); c.SetParams(-20.f, 4.f, 5.f, 100.f, 0.f);
    float out = 0;
    for(int i = 0; i < 48000; ++i) out = c.Process(DbToLin(-4.f));  // DC-ish: level -4 dB
    float outDb = 20 * std::log10(std::fabs(out));
    float expect = -20 + (-4 + 20) / 4.f;  // -16 dB
    printf("comp static: out %.2f dB (expect %.2f)\n", outDb, expect);
    CHECK(std::fabs(outDb - expect) < 0.4f, "compressor static curve");
    float q = c.Process(DbToLin(-40.f)); (void)q;
    c.Init(48000.f); c.SetParams(-20.f, 4.f, 5.f, 100.f, 0.f);
    float o2 = 0; for(int i = 0; i < 48000; ++i) o2 = c.Process(DbToLin(-40.f));
    CHECK(std::fabs(20 * std::log10(o2) + 40.f) < 0.1f, "below threshold must be unity");
}

// ---------------------------------------------------------------- EQ / piezo comp
static float BiquadGainDb(const Biquad& b, float f)
{
    const double w = 2 * M_PI * f / 48000.0;
    std::complex<double> z = std::polar(1.0, -w), z2 = z * z;
    std::complex<double> h = ((double)b.b0 + (double)b.b1 * z + (double)b.b2 * z2) / (1.0 + (double)b.a1 * z + (double)b.a2 * z2);
    return (float)(20 * std::log10(std::abs(h)));
}
static void TestFilters()
{
    Biquad b; b.Init();
    b.SetPeaking(48000, 2500, 0.8f, 9.f);
    CHECK(std::fabs(BiquadGainDb(b, 2500) - 9.f) < 0.05f, "peak gain");
    b.SetLowShelf(48000, 120, 0.7f, -6.f);
    CHECK(std::fabs(BiquadGainDb(b, 20) + 6.f) < 0.3f, "low shelf gain %f", BiquadGainDb(b, 20));
    b.SetHighShelf(48000, 7000, 0.7f, 6.f);
    CHECK(std::fabs(BiquadGainDb(b, 18000) - 6.f) < 0.5f, "high shelf gain %f", BiquadGainDb(b, 18000));
    // piezo comp undoes a 130 Hz one-pole HP above ~60 Hz
    Biquad hp, comp; hp.Init(); comp.Init();
    const float c = 2 * 48000.f, a = kTwoPi * 130.f;
    hp.Set(c, -c, 0, c + a, a - c, 0);
    comp.SetFirstOrder(48000, 130, 25);
    double worst = 0;
    for(float f : {60.f, 100.f, 200.f, 500.f, 2000.f, 10000.f})
    {
        float g = BiquadGainDb(hp, f) + BiquadGainDb(comp, f);
        worst = std::fmax(worst, std::fabs(g));
    }
    printf("piezo comp residual (60Hz..10k) max %.2f dB\n", worst);
    CHECK(worst < 1.5, "piezo comp flatness");
    // DC blocker / lowcut
    b.SetHighpass(48000, 100, 0.7071f);
    CHECK(BiquadGainDb(b, 100) < -2.5f && BiquadGainDb(b, 100) > -3.5f, "highpass -3dB point");
}

// ---------------------------------------------------------------- reverb
static void TestReverb()
{
    std::vector<float> mem(Reverb::kMemFloats, 123.f); // NOLOAD-like garbage
    for(int e = 0; e < 4; ++e)
    {
        Reverb r; r.Init(48000, mem.data());
        r.SetParams(2.f, 0.5f, 0.5f, 0.f, 0.3f);
        r.SetEngine((Reverb::Engine)e);
        for(int i = 0; i < 400; ++i) r.Housekeeping(), (void)0;  // not processed -> no change yet
        // run until engine switched
        float l, rr;
        for(int i = 0; i < 48000 * 2; ++i) { if(i % 64 == 0) r.Housekeeping(); r.Process(0.f, l, rr); }
        CHECK(r.CurrentEngine() == (Reverb::Engine)e, "engine switch %d", e);
        // noise in 1 s -> measure steady-state wet vs dry RMS
        double ed = 0, ew = 0; int n = 0;
        bool bad = false;
        for(int i = 0; i < 48000 * 6; ++i)
        {
            if(i % 64 == 0) r.Housekeeping();
            float x = Rand() * 0.3f;
            r.Process(x, l, rr);
            if(!std::isfinite(l) || !std::isfinite(rr) || std::fabs(l) > 20.f) bad = true;
            if(i > 48000 * 3) { ed += x * x; ew += 0.5 * (l * l + rr * rr); ++n; }
        }
        printf("reverb %d: steady-state wet/dry = %.2f dB%s\n", e, 10 * std::log10(ew / ed), bad ? " BAD" : "");
        CHECK(!bad, "reverb %d non-finite/huge", e);
        CHECK(std::fabs(10 * std::log10(ew / ed) + 3.0) < 1.5, "reverb %d level calibration", e);
        // impulse -> measure RT60 (Schroeder) for decay=2s
        r.SetParams(2.f, 0.5f, 0.2f, 0.f, 0.f);
        for(int i = 0; i < 48000 * 6; ++i) { if(i % 64 == 0) r.Housekeeping(); r.Process(0, l, rr); }
        std::vector<float> ir; r.Process(1.f, l, rr); ir.push_back(l);
        for(int i = 0; i < 48000 * 6; ++i) { r.Process(0, l, rr); ir.push_back(l); }
        std::vector<double> edc(ir.size()); double s = 0;
        for(int i = (int)ir.size() - 1; i >= 0; --i) { s += (double)ir[i] * ir[i]; edc[i] = s; }
        auto at = [&](double db) { for(size_t i = 0; i < edc.size(); ++i) if(10 * std::log10(edc[i] / edc[0] + 1e-30) < db) return (double)i / 48000.0; return -1.0; };
        double t5 = at(-5), t25 = at(-25);
        if(t5 > 0 && t25 > 0) printf("   RT60 (T20 est) = %.2f s (set 2.0)\n", (t25 - t5) * 3.0);
    }
}

// ---------------------------------------------------------------- loader
struct MemReader : IReader
{
    std::vector<uint8_t> d; size_t pos = 0;
    size_t Read(void* dst, size_t n) override { n = std::min(n, d.size() - pos); std::memcpy(dst, &d[pos], n); pos += n; return n; }
    bool Seek(uint32_t p) override { if(p > d.size()) return false; pos = p; return true; }
    uint32_t Size() override { return (uint32_t)d.size(); }
};
static void Put32(std::vector<uint8_t>& v, uint32_t x) { for(int i = 0; i < 4; ++i) v.push_back((x >> (8 * i)) & 255); }
static void Put16(std::vector<uint8_t>& v, uint16_t x) { v.push_back(x & 255); v.push_back(x >> 8); }
static std::vector<uint8_t> MakeWav(const std::vector<float>& s, int rate, int bits, int ch, bool flt, bool ext = false)
{
    std::vector<uint8_t> v;
    const int bps = bits / 8; const size_t dataBytes = s.size() * ch * bps;
    v.insert(v.end(), {'R','I','F','F'}); Put32(v, 0);
    v.insert(v.end(), {'W','A','V','E'});
    v.insert(v.end(), {'L','I','S','T'}); Put32(v, 5); v.insert(v.end(), {'a','b','c','d','e', 0}); // odd chunk + pad
    v.insert(v.end(), {'f','m','t',' '}); Put32(v, ext ? 40 : 16);
    Put16(v, ext ? 0xFFFE : (flt ? 3 : 1)); Put16(v, ch); Put32(v, rate); Put32(v, rate * ch * bps); Put16(v, ch * bps); Put16(v, bits);
    if(ext) { Put16(v, 22); Put16(v, bits); Put32(v, 0); Put16(v, flt ? 3 : 1); for(int i = 0; i < 14; ++i) v.push_back(0); }
    v.insert(v.end(), {'d','a','t','a'}); Put32(v, (uint32_t)dataBytes);
    for(float x : s) for(int c = 0; c < ch; ++c)
    {
        if(flt) { float f = x; uint8_t b[4]; std::memcpy(b, &f, 4); v.insert(v.end(), b, b + 4); }
        else if(bits == 16) Put16(v, (uint16_t)(int16_t)std::lrintf(x * 32767));
        else if(bits == 24) { int32_t q = std::lrintf(x * 8388607); v.push_back(q & 255); v.push_back((q >> 8) & 255); v.push_back((q >> 16) & 255); }
        else if(bits == 8) v.push_back((uint8_t)(128 + std::lrintf(x * 127)));
    }
    return v;
}
static void TestLoader()
{
    static std::vector<float> src(600000), dst(200000), kern(irload::kTableSize);
    static std::vector<uint8_t> io(4096);
    irload::BuildKernel(kern.data());
    IrBuffers b{src.data(), src.size(), dst.data(), dst.size(), kern.data(), io.data(), io.size()};
    // IR: decaying noise with some leading silence at 44.1k
    for(int rate : {44100, 48000, 96000})
    {
        const int n = rate * 12 / 10;
        std::vector<float> s(n, 0.f);
        for(int i = 300; i < n; ++i) s[i] = Rand() * std::exp(-6.f * (i - 300) / (float)rate);
        for(int fmt = 0; fmt < 5; ++fmt)
        {
            MemReader r;
            if(fmt == 0) r.d = MakeWav(s, rate, 16, 1, false);
            if(fmt == 1) r.d = MakeWav(s, rate, 24, 2, false);
            if(fmt == 2) r.d = MakeWav(s, rate, 32, 1, true);
            if(fmt == 3) r.d = MakeWav(s, rate, 8, 1, false);
            if(fmt == 4) r.d = MakeWav(s, rate, 32, 1, true, true);
            IrResult res{};
            auto st = LoadIrFromWav(r, b, 192000, res, nullptr, nullptr);
            CHECK(st == IrStatus::Ok, "wav load rate %d fmt %d status %d", rate, fmt, (int)st);
            if(st != IrStatus::Ok) continue;
            double e = 0; for(int i = 0; i < res.len; ++i) e += (double)dst[i] * dst[i];
            const float expLen = (n - 300 + 16) * 48000.f / rate;
            if(fmt == 0)
                printf("loader rate %d: len %d (≈ %.0f), energy %.4f, trimmed %.2f ms\n", rate, res.len, expLen, e, res.trimmedMs);
            CHECK(std::fabs(e - 1.0) < 1e-3, "unit energy %g", e);
            CHECK(res.len > 0.7f * expLen && res.len < 1.1f * expLen + 100, "length %d vs %.0f (rate %d fmt %d)", res.len, expLen, rate, fmt);
        }
    }
    // garbage
    MemReader g; g.d.assign(100, 7);
    IrResult res{};
    CHECK(LoadIrFromWav(g, b, 192000, res, nullptr, nullptr) == IrStatus::BadWav, "garbage rejected");
    // corrupt chunk size must not hang the loader
    {
        MemReader cr; cr.d = MakeWav(std::vector<float>(1000, 0.1f), 48000, 16, 1, false);
        const uint32_t bad = 0xFFFFFFF8u; std::memcpy(&cr.d[16], &bad, 4); // the LIST chunk's size
        CHECK(LoadIrFromWav(cr, b, 192000, res, nullptr, nullptr) == IrStatus::BadWav, "corrupt chunk size rejected");
    }
    // NaN samples in a float file become silence, not NaN output
    {
        std::vector<float> s(4800); for(size_t i = 0; i < s.size(); ++i) s[i] = Rand() * std::exp(-0.002f * i);
        s[100] = NAN;
        MemReader nr; nr.d = MakeWav(s, 48000, 32, 1, true);
        CHECK(LoadIrFromWav(nr, b, 192000, res, nullptr, nullptr) == IrStatus::Ok, "NaN file loads");
        bool fin = true; for(int i = 0; i < res.len; ++i) fin = fin && std::isfinite(dst[i]);
        CHECK(fin, "NaN removed");
    }
    // silent file
    std::vector<float> z(1000, 0.f); MemReader zr; zr.d = MakeWav(z, 48000, 16, 1, false);
    CHECK(LoadIrFromWav(zr, b, 192000, res, nullptr, nullptr) == IrStatus::Empty, "silent file rejected");
    // resampling accuracy: a 1 kHz sine at 96k comes out as 1 kHz at 48k
    std::vector<float> sine(96000); for(size_t i = 0; i < sine.size(); ++i) sine[i] = std::sin(2 * M_PI * 1000 * i / 96000.0);
    std::vector<float> o(48000);
    size_t m = irload::Resample(kern.data(), sine.data(), sine.size(), 96000, o.data(), o.size(), nullptr, nullptr);
    double err = 0; for(size_t i = 100; i < m - 100; ++i) err = std::fmax(err, std::fabs(o[i] - std::sin(2 * M_PI * 1000 * i / 48000.0)));
    printf("resample 96k->48k sine err %.2g (n=%zu)\n", err, m);
    CHECK(err < 2e-3, "resample err %g", err);
    std::vector<float> s44(44100); for(size_t i = 0; i < s44.size(); ++i) s44[i] = std::sin(2 * M_PI * 1000 * i / 44100.0);
    m = irload::Resample(kern.data(), s44.data(), s44.size(), 44100, o.data(), o.size(), nullptr, nullptr);
    err = 0; for(size_t i = 100; i < m - 100; ++i) err = std::fmax(err, std::fabs(o[i] - std::sin(2 * M_PI * 1000 * i / 48000.0)));
    CHECK(err < 2e-3, "resample 44.1 err %g", err);
}

// ---------------------------------------------------------------- link
static void TestLink()
{
    LinkStatus s{}; s.bank = 1; s.engine = 2; s.irIndex = 3; s.irCount = 9; s.flags = kFlagSd | kFlagBypass;
    s.inDb = -12; s.outDb = -6; s.grDb = 3; s.cpu = 41; s.irName = "My,Guitar*IR$ok";
    for(int i = 0; i < kNumParams; ++i) s.norm[i] = i / 17.f;
    char buf[400]; size_t n = BuildFrame(buf, sizeof buf, s);
    buf[n] = 0;
    printf("link frame (%zu B): %s", n, buf);
    CHECK(n > 20 && buf[n - 1] == '\n' && buf[0] == '$', "frame shape");
    uint8_t x = 0; const char* star = std::strchr(buf, '*');
    for(const char* q = buf + 1; q < star; ++q) x ^= (uint8_t)*q;
    char hx[3]; std::snprintf(hx, 3, "%02X", x);
    CHECK(std::strncmp(hx, star + 1, 2) == 0, "checksum");
    CHECK(!std::strchr(buf + 1, '$'), "no stray $");
}

// ---------------------------------------------------------------- params
static void TestParams()
{
    for(int i = 0; i < kNumParams; ++i)
    {
        float prev = -1e30f;
        for(int k = 0; k <= 20; ++k)
        {
            float v = NormToValue(i, k / 20.f);
            CHECK(v >= prev, "monotonic %d", i); prev = v;
        }
        CHECK(std::fabs(NormToValue(i, ValueToNorm(i, kParamDefs[i].def)) - kParamDefs[i].def) < 1e-3f * (1 + std::fabs(kParamDefs[i].def)), "default roundtrip %d", i);
    }
    ParamSet p; p.Init();
    float knob[6] = {0, 0, 0, 0, 0, 0};
    p.UpdateKnobs(knob);   // bank 0 knobs at 0 vs stored: IR mix stored 1.0 -> not picked
    float before = p.Norm(0);
    p.UpdateKnobs(knob);
    CHECK(p.Norm(0) == before, "knob must not take over before pickup");
    knob[0] = 0.5f; p.UpdateKnobs(knob);
    CHECK(p.Norm(0) == before, "still not picked at 0.5");
    knob[0] = 1.0f; p.UpdateKnobs(knob);
    CHECK(p.Norm(0) == 1.0f, "picked at crossing");
    knob[0] = 0.3f; p.UpdateKnobs(knob);
    CHECK(std::fabs(p.Norm(0) - 0.3f) < 1e-6f, "follows after pickup");
    p.SelectBank(1);
    knob[0] = 0.9f; p.UpdateKnobs(knob);
    CHECK(p.Picked(0) == false || std::fabs(p.Norm(6) - 0.9f) < 1e-6f, "bank switch re-arms");
}

// ---------------------------------------------------------------- full chain
static void TestChain()
{
    ConvMem cm; std::vector<float> rv(Reverb::kMemFloats, 5.f);
    static Chain ch; ch.Init(cm.m, rv.data());
    // a 1.5 s IR
    const int L = 72000; std::vector<float> h(L);
    for(int i = 0; i < L; ++i) h[i] = Rand() * std::exp(-5.f * i / L);
    double e = 0; for(float v : h) e += (double)v * v; for(auto& v : h) v /= (float)std::sqrt(e);
    auto* s = ch.Conv().Inactive(); ch.Conv().BuildSet(s, h.data(), L, nullptr, nullptr); ch.Conv().QueueSwap(s);
    float v[kNumParams]; for(int i = 0; i < kNumParams; ++i) v[i] = kParamDefs[i].def;
    v[4] = 20.f; v[1] = 50.f;                    // +20 dB gain, 50% reverb
    ch.SetParams(v, Reverb::kHall);
    float in[64], l[64], r[64]; bool bad = false; float pk = 0;
    for(int b = 0; b < 48000 * 8 / 64; ++b)
    {
        for(int i = 0; i < 64; ++i) in[i] = (b % 700 < 300 ? 0.5f * Rand() : 0.f);   // loud bursts of noise
        if(b == 1000) { ch.SetBypass(true); } if(b == 1100) ch.SetBypass(false);
        if(b == 2000) ch.SetParams(v, Reverb::kSpring);
        ch.ProcessBlock(in, l, r);
        while(ch.Conv().ServiceBackground()) {}
        for(int i = 0; i < 64; ++i) { if(!std::isfinite(l[i]) || !std::isfinite(r[i])) bad = true; pk = std::fmax(pk, std::fmax(std::fabs(l[i]), std::fabs(r[i]))); }
    }
    Meters m = ch.TakeMeters();
    printf("chain: peak out %.3f, meters in %.2f out %.2f gr %.1f clip %d\n", pk, m.inPeak, m.outPeak, m.grDb, m.clip);
    CHECK(!bad, "chain produced NaN/inf");
    CHECK(pk <= 1.0001f, "soft limiter exceeded 1.0: %g", pk);
}

// ---------------------------------------------------------------- chain hooks
// Runs 2 s of noise bursts through a chain; returns the output.  mode 0 = no hooks,
// 1 = hooks that do nothing (must give the same bits), 2 = input hook that silences.
static std::vector<float> RunHooked(int mode, int* calls)
{
    ConvMem cm; std::vector<float> rv(Reverb::kMemFloats, 5.f);
    static Chain ch; ch.Init(cm.m, rv.data());
    const int L = 24000; std::vector<float> h(L);
    std::mt19937 r2(99); std::uniform_real_distribution<float> U(-1.f, 1.f);
    for(int i = 0; i < L; ++i) h[i] = U(r2) * std::exp(-6.f * i / L) * 0.03f;
    auto* s = ch.Conv().Inactive(); ch.Conv().BuildSet(s, h.data(), L, nullptr, nullptr); ch.Conv().QueueSwap(s);
    float v[kNumParams]; for(int i = 0; i < kNumParams; ++i) v[i] = kParamDefs[i].def;
    v[1] = 40.f;
    ch.SetParams(v, Reverb::kPlate);
    Chain::BlockHook count = [](float*, int n, void* c) { *(int*)c += n; };
    Chain::BlockHook zero  = [](float* x, int n, void* c) { *(int*)c += n; for(int i = 0; i < n; ++i) x[i] = 0.f; };
    if(mode == 0) ch.SetHooks(nullptr, nullptr, nullptr);
    if(mode == 1) ch.SetHooks(count, count, calls);
    if(mode == 2) ch.SetHooks(zero, nullptr, calls);
    std::vector<float> out; float in[64], l[64], r[64];
    for(int b = 0; b < 48000 * 2 / 64; ++b)
    {
        for(int i = 0; i < 64; ++i) in[i] = (b % 300 < 120 ? 0.5f * U(r2) : 0.f);
        ch.ProcessBlock(in, l, r);
        while(ch.Conv().ServiceBackground()) {}
        out.insert(out.end(), l, l + 64); out.insert(out.end(), r, r + 64);
    }
    ch.SetHooks(nullptr, nullptr, nullptr);
    return out;
}
static void TestChainHooks()
{
    int c1 = 0, c2 = 0;
    const auto a = RunHooked(0, nullptr), b = RunHooked(1, &c1), z = RunHooked(2, &c2);
    CHECK(std::memcmp(a.data(), b.data(), a.size() * sizeof(float)) == 0, "do-nothing hooks changed the output");
    CHECK(c1 == 2 * 48000 * 2 / 64 * 64, "hooks called for %d samples", c1);
    float pk = 0; for(size_t i = z.size() / 2; i < z.size(); ++i) pk = std::fmax(pk, std::fabs(z[i]));
    CHECK(c2 > 0 && pk < 1e-6f, "silencing input hook: tail peak %g", pk);
    printf("chain hooks: no-op hooks bit-identical, input hook works (tail peak %.1g)\n", pk);
}

int main()
{
    TestFft();
    TestConvolver(300, false);
    TestConvolver(4096, false);
    TestConvolver(4097 + 2048 * 3 + 17, false);
    TestConvolver(48000 * 2, false);
    TestConvolver(48000 * 4, false);
    TestConvolver(48000 * 2 + 5, true);
    TestCompressor();
    TestFilters();
    TestReverb();
    TestLoader();
    TestLink();
    TestParams();
    TestChain();
    TestChainHooks();
    printf(fails ? "\n%d FAILURES\n" : "\nALL TESTS PASSED\n", fails);
    return fails != 0;
}
