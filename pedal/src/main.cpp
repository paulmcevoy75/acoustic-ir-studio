// PureIR - long acoustic-guitar impulse responses + reverb + EQ +
// compression + boost for a piezo pickup, on the Daisy Seed Pedal DevKit.
//
// Signal processing lives in chain.h (identical code is tested on a PC, see
// tests/host_test.cpp).  This file is only the hardware glue:
//   * controls   : 6 knobs (3 banks), SW1 bank, SW2/SW3 reverb engine, 2 footswitches
//   * storage    : IR library on the SD card (+ built-in demo IRs)
//   * display    : one-way status frames to an ESP32 on UART4 TX (pin D12)
#include "pedal_devkit.h"
#include "util/CpuLoadMeter.h"

#include "chain.h"
#include "ir_builtin.h"
#include "ir_loader.h"
#include "link.h"
#include "upload.h"
#include "hooks.h"

#ifndef PUREIR_VERSION
#define PUREIR_VERSION "0.2.1"
#endif

using namespace daisy;
using namespace daisy::pedal_devkit;

// ---------------------------------------------------------------------------
// memory map (see PureIR_sram.lds)
//   DTCM  (128 KB): code objects, stack
//   D2    (256 KB): convolver zone A + rings   -> PUREIR_AXI_BSS
//   SDRAM (64 MB) : everything big               -> PUREIR_SDRAM_BSS
// ---------------------------------------------------------------------------
static PUREIR_AXI_BSS float g_irA0[pureir::conv::kIrASize];
static PUREIR_AXI_BSS float g_irA1[pureir::conv::kIrASize];
static PUREIR_AXI_BSS float g_fdlA[pureir::conv::kFdlASize];
static PUREIR_AXI_BSS float g_inRing[pureir::conv::kInRing * pureir::conv::kPartB];
static PUREIR_AXI_BSS float g_outRing[pureir::conv::kOutRing * pureir::conv::kPartB];

static PUREIR_SDRAM_BSS float g_irB0[pureir::conv::kIrBSize];
static PUREIR_SDRAM_BSS float g_irB1[pureir::conv::kIrBSize];
static PUREIR_SDRAM_BSS float g_fdlB[pureir::conv::kFdlBSize];
static PUREIR_AXI_BSS float g_work[pureir::conv::kWorkBSize];
static PUREIR_SDRAM_BSS float g_build[pureir::conv::kBuildSize];
static PUREIR_SDRAM_BSS float g_reverb[pureir::Reverb::kMemFloats];

constexpr size_t kSrcCap = 1000000; // samples of file data we are willing to hold
static PUREIR_SDRAM_BSS float g_src[kSrcCap];
static PUREIR_SDRAM_BSS float g_dst[pureir::conv::kMaxIrSamples + 64];
static PUREIR_SDRAM_BSS float g_kernel[pureir::irload::kTableSize];

// DMA-capable memory (non-cached D2 SRAM)
static DMA_BUFFER_MEM_SECTION uint8_t g_io[8192] __attribute__((aligned(32)));
static DMA_BUFFER_MEM_SECTION char    g_tx[256] __attribute__((aligned(32)));

// ---------------------------------------------------------------------------
static Hardware        hw;
static pureir::Chain      g_chain;
static pureir::ParamSet   g_params;
static CpuLoadMeter    g_cpu;
#ifdef PUREIR_DIAG
static int g_lastSt = 0; // IrStatus of the last load attempt (diagnostic build)
static volatile uint32_t g_dFsw1Edges = 0, g_dFsw2Edges = 0; // raw footswitch pin changes seen
static uint32_t          g_dLoops      = 0;                  // main-loop iterations
static uint32_t          g_dLoadStart  = 0;
#define DLOG(...) hw.seed.PrintLine(__VA_ARGS__)
#endif
static SdmmcHandler    g_sdmmc;
static FatFSInterface  g_fsi;
static UartHandler     g_uart;

// IR library ----------------------------------------------------------------
constexpr int kMaxFiles = 48;
constexpr int kNameLen  = 64;
static char   g_names[kMaxFiles][kNameLen];
static int    g_nSd      = 0;
static int    g_nUsb     = 0; // IRs stored in flash by the USB uploader (after the SD files)
static pureir::store::SlotInfo g_usb[pureir::store::kNumSlots];
static bool   g_sdOk     = false;
static int    g_irIndex  = 0;
static int    g_irCount  = 0;
static char   g_curName[24] = "Neutral";
static bool   g_curBuiltin  = true;

static void RecountIrs() { g_irCount = g_nSd + g_nUsb + pureir::kBuiltinIrCount; }

// events from the audio callback to the main loop
static volatile int  g_reqIr   = -1;   // IR index requested (absolute)
static volatile bool g_busy    = false;
static volatile bool g_error   = false;
static volatile bool g_txBusy  = false;

// ---------------------------------------------------------------------------
// controls (audio callback, once per block)
// ---------------------------------------------------------------------------
struct FswState
{
    bool fired = false;
};
enum FswEvent
{
    kNone,
    kTap,
    kHold
};
static FswEvent PollFsw(const Switch& s, FswState& st)
{
    if(s.RisingEdge())
        st.fired = false;
    if(s.Pressed() && !st.fired && s.TimeHeldMs() >= 600.f)
    {
        st.fired = true;
        return kHold;
    }
    if(s.FallingEdge() && !st.fired)
        return kTap;
    return kNone;
}
static FswState g_fsw1, g_fsw2;
static float    g_lastV[pureir::kNumParams];
static int      g_lastEngine = -1;

static pureir::hooks::Context& HookCtx();

static void ReadControls()
{
    using namespace pureir;
    hw.UpdateAllControls();
#ifdef PUREIR_DIAG
    if(hw.fsw_1.RisingEdge() || hw.fsw_1.FallingEdge()) ++g_dFsw1Edges;
    if(hw.fsw_2.RisingEdge() || hw.fsw_2.FallingEdge()) ++g_dFsw2Edges;
#endif

    // bank (3-way switch)
    const int pos  = hw.sw1.Read();
    const int bank = pos == Switch3::POS_UP ? 0 : (pos == Switch3::POS_DOWN ? 2 : 1);

    hooks::Controls hc;
    hc.bank        = bank;
    hc.fsw1Pressed = hw.fsw_1.Pressed();
    hc.fsw1Rising  = hw.fsw_1.RisingEdge();
    hc.fsw1Falling = hw.fsw_1.FallingEdge();
    for(int k = 0; k < kKnobs; ++k)
        hc.knob[k] = hw.pot[k].Value();
    hooks::OnControls(HookCtx(), hc);

    g_params.SelectBank(bank);
    if(!hc.knobsTaken)
        g_params.UpdateKnobs(hc.knob);

    // reverb engine = 2 bits from SW2 / SW3
    const int engine = (hw.sw2.Pressed() ? 2 : 0) + (hw.sw3.Pressed() ? 1 : 0);

    float v[kNumParams];
    bool  changed = engine != g_lastEngine;
    for(int i = 0; i < kNumParams; ++i)
    {
        v[i] = g_params.Value(i);
        if(v[i] != g_lastV[i])
            changed = true;
    }
    if(changed)
    {
        std::memcpy(g_lastV, v, sizeof(v));
        g_lastEngine = engine;
        g_chain.SetParams(v, (Reverb::Engine)engine);
    }

    // footswitches
    FswEvent e1 = PollFsw(hw.fsw_1, g_fsw1);
    if(hc.suppressFsw1)
    {
        g_fsw1.fired = true; // nothing more from this press (cleared on the next press)
        e1           = kNone;
    }
    switch(e1)
    {
        case kTap: g_chain.SetBypass(!g_chain.Bypassed()); break;
        case kHold: g_chain.SetMute(!g_chain.Muted()); break;
        default: break;
    }
    switch(PollFsw(hw.fsw_2, g_fsw2))
    {
        case kTap:
            if(g_irCount > 0)
                g_reqIr = (g_irIndex + 1) % g_irCount;
            break;
        case kHold:
            if(g_irCount > 0)
                g_reqIr = (g_irIndex + g_irCount - 1) % g_irCount;
            break;
        default: break;
    }
}

static void AudioCallback(AudioHandle::InputBuffer  in,
                          AudioHandle::OutputBuffer out,
                          size_t                    size)
{
    g_cpu.OnBlockStart();
    ReadControls();
    if(size == pureir::conv::kBlock)
    {
        g_chain.ProcessBlock(in[0], out[0], out[1]);
    }
    else
    {
        for(size_t i = 0; i < size; ++i)
            out[0][i] = out[1][i] = 0.f;
    }
    g_cpu.OnBlockEnd();
}

// ---------------------------------------------------------------------------
// IR loading (main loop)
// ---------------------------------------------------------------------------
static void Yield(void*)
{
    // keep zone-B convolution of the *running* IR alive during long jobs
    g_chain.Conv().ServiceBackground();
}

class SdFile : public pureir::IReader
{
  public:
    bool Open(const char* path)
    {
        open_ = f_open(&f_, path, FA_READ) == FR_OK;
        return open_;
    }
    void Close()
    {
        if(open_)
            f_close(&f_);
        open_ = false;
    }
    size_t Read(void* dst, size_t n) override
    {
        UINT got = 0;
        if(f_read(&f_, dst, (UINT)n, &got) != FR_OK)
            return 0;
        return got;
    }
    bool     Seek(uint32_t pos) override { return f_lseek(&f_, pos) == FR_OK; }
    uint32_t Size() override { return (uint32_t)f_size(&f_); }

  private:
    FIL  f_;
    bool open_ = false;
};
static SdFile g_file;

static bool IsWav(const char* n)
{
    const size_t l = std::strlen(n);
    if(l < 5)
        return false;
    const char* e = n + l - 4;
    return e[0] == '.' && (e[1] | 32) == 'w' && (e[2] | 32) == 'a' && (e[3] | 32) == 'v';
}

static void ScanSd()
{
    g_nSd = 0;
    const char* dirs[] = {"/IR", "/ir", "/"};
    for(const char* d : dirs)
    {
        DIR dir;
        if(f_opendir(&dir, d) != FR_OK)
            continue;
        FILINFO fi;
        while(g_nSd < kMaxFiles && f_readdir(&dir, &fi) == FR_OK && fi.fname[0])
        {
            if(fi.fattrib & (AM_DIR | AM_HID | AM_SYS))
                continue;
            if(fi.fname[0] == '.' || !IsWav(fi.fname))
                continue;
            // store "dir/name" so the path can be rebuilt; "/" files get a bare name
            char* dst = g_names[g_nSd];
            std::snprintf(dst, kNameLen, "%s/%s", d[1] ? d : "", fi.fname);
            ++g_nSd;
        }
        f_closedir(&dir);
        if(g_nSd > 0)
            break; // first directory that has IRs wins
    }
    // alphabetical order (insertion sort)
    for(int i = 1; i < g_nSd; ++i)
    {
        char tmp[kNameLen];
        std::memcpy(tmp, g_names[i], kNameLen);
        int j = i - 1;
        while(j >= 0 && strcasecmp(g_names[j], tmp) > 0)
        {
            std::memcpy(g_names[j + 1], g_names[j], kNameLen);
            --j;
        }
        std::memcpy(g_names[j + 1], tmp, kNameLen);
    }
    RecountIrs();
}

static bool TryMount(SdmmcHandler::Speed sp, SdmmcHandler::BusWidth bw)
{
    SdmmcHandler::Config c;
    c.Defaults();
    c.speed = sp;
    c.width = bw;
    if(g_sdmmc.Init(c) != SdmmcHandler::Result::OK)
        return false;
    return f_mount(&g_fsi.GetSDFileSystem(), "/", 1) == FR_OK;
}

static void MountAndScan()
{
    g_sdOk = TryMount(SdmmcHandler::Speed::FAST, SdmmcHandler::BusWidth::BITS_4)
             || TryMount(SdmmcHandler::Speed::STANDARD, SdmmcHandler::BusWidth::BITS_1);
    if(g_sdOk)
        ScanSd();
    else
        RecountIrs();
}

static void ShortName(const char* path, char* out, size_t n)
{
    const char* s = std::strrchr(path, '/');
    s             = s ? s + 1 : path;
    size_t l      = std::strlen(s);
    if(l > 4 && IsWav(s))
        l -= 4;
    if(l >= n)
        l = n - 1;
    std::memcpy(out, s, l);
    out[l] = 0;
}

// ---- IRs uploaded over USB (see upload.h) ------------------------------------
static bool QErase(uint32_t off, void*) { return hw.seed.qspi.EraseSector(off) == QSPIHandle::Result::OK; }
static bool QWrite(uint32_t off, const uint8_t* d, uint32_t n, void*)
{
    return hw.seed.qspi.Write(off, n, const_cast<uint8_t*>(d)) == QSPIHandle::Result::OK;
}
static const uint8_t* QMap(uint32_t off, void*) { return (const uint8_t*)hw.seed.qspi.GetData(off); }
static void QInval(uint32_t off, uint32_t n, void*)
{
    // the CPU may have cached old flash contents: drop them so reads see what was written
    const uintptr_t a0 = (uintptr_t)hw.seed.qspi.GetData(off) & ~(uintptr_t)31;
    const uintptr_t a1 = ((uintptr_t)hw.seed.qspi.GetData(off) + n + 31) & ~(uintptr_t)31;
    SCB_InvalidateDCache_by_Addr((uint32_t*)a0, (int32_t)(a1 - a0));
}
static const pureir::store::FlashOps kFlash{QErase, QWrite, QMap, QInval, nullptr};

// The USB driver sends straight from the buffer we give it, so replies need buffers that stay
// valid until the transfer is done: two of them, used alternately.
static void UsbTx(const char* line, void*)
{
    static char buf[2][100];
    static int  k = 0;
    k ^= 1;
    const size_t n = std::min<size_t>(std::strlen(line), sizeof(buf[0]) - 1);
    std::memcpy(buf[k], line, n);
    for(int t = 0; t < 300; ++t)
    {
        if(hw.seed.usb_handle.TransmitInternal((uint8_t*)buf[k], n) == UsbHandle::Result::OK)
            return;
        System::Delay(1);
    }
}
static pureir::store::Uploader g_up(kFlash, UsbTx, nullptr, PUREIR_VERSION);

// ---- optional hooks (hooks.h): these do nothing unless a build supplies its own --------
static pureir::hooks::Context& HookCtx()
{
    static pureir::hooks::Context c{g_chain, g_params, g_up};
    return c;
}
namespace pureir
{
namespace hooks
{
__attribute__((weak)) void Setup(Context&) {}
__attribute__((weak)) void OnControls(Context&, Controls&) {}
__attribute__((weak)) void OnIrLoaded(Context&, int, const store::SlotInfo*) {}
__attribute__((weak)) void Poll(Context&, uint32_t) {}
__attribute__((weak)) void NormalLed(Context&, uint32_t, float&, float&, float&) {}
__attribute__((weak)) void OnStatus(Context&, LinkStatus&) {}
} // namespace hooks
} // namespace pureir

static void UsbRx(uint8_t* buf, uint32_t* len)
{
    // a lone 'b' (typed in a serial monitor) restarts into the bootloader, ready for flashing
    // (not during an upload: IR data could contain a lone 'b' byte)
    if(*len == 1 && buf[0] == 'b' && !g_up.Busy())
        System::ResetToBootloader(System::BootloaderMode::DAISY_INFINITE_TIMEOUT);
    else
        g_up.Feed(buf, *len);
}

static void ScanUsb()
{
    g_nUsb = pureir::store::ScanSlots(kFlash, g_usb);
    RecountIrs();
}

/** Load absolute index `idx` and queue it.  Returns true on success. */
static bool LoadIr(int idx)
{
    using namespace pureir;
    g_busy = true;
#ifdef PUREIR_DIAG
    g_dLoadStart = System::GetNow();
    DLOG("LOAD start idx=%d of %d (sd files %d)", idx, g_irCount, g_nSd);
#endif
    // the inactive set is only free once the previous swap has completed
    while(g_chain.Conv().SwapPending())
    {
        g_chain.Conv().ServiceBackground();
        System::DelayUs(200);
    }
    IrBuffers b{g_src, kSrcCap, g_dst, sizeof(g_dst) / sizeof(float), g_kernel, g_io, sizeof(g_io)};
    IrResult  res{};
    IrStatus  st;
    char      name[24];
    const bool isSd   = idx < g_nSd;
    const bool isUsb  = !isSd && idx < g_nSd + g_nUsb;
    const bool builtin = !isSd && !isUsb;
    if(isSd)
    {
        if(!g_file.Open(g_names[idx]))
            st = IrStatus::NoFile;
        else
        {
            st = LoadIrFromWav(g_file, b, conv::kMaxIrSamples, res, Yield, nullptr);
            g_file.Close();
        }
        ShortName(g_names[idx], name, sizeof(name));
    }
    else if(isUsb)
    {
        const store::SlotInfo& u = g_usb[idx - g_nSd];
        st = LoadIrFromPcm16((const int16_t*)u.data, u.length, u.rate, b, conv::kMaxIrSamples, res, Yield, nullptr);
        std::snprintf(name, sizeof(name), "%s", u.name);
    }
    else
    {
        const BuiltinIr& bi = kBuiltinIrs[idx - g_nSd - g_nUsb];
        st = LoadIrFromPcm16(bi.data, bi.length, bi.rate, b, conv::kMaxIrSamples, res, Yield, nullptr);
        std::snprintf(name, sizeof(name), "%s", bi.name);
    }
#ifdef PUREIR_DIAG
    g_lastSt = (int)st;
#endif
    if(st != IrStatus::Ok)
    {
        g_error = true;
        g_busy  = false;
#ifdef PUREIR_DIAG
        DLOG("LOAD FAILED idx=%d status=%d after %d ms  (1 nofile 2 badwav 3 unsupported 4 empty 5 read)", idx, g_lastSt, (int)(System::GetNow() - g_dLoadStart));
#endif
        return false;
    }
    auto* set = g_chain.Conv().Inactive();
    g_chain.Conv().BuildSet(set, g_dst, res.len, Yield, nullptr);
    g_chain.Conv().QueueSwap(set);
    std::snprintf(g_curName, sizeof(g_curName), "%s", name);
    g_curBuiltin = builtin;
    g_irIndex    = idx;
    g_error      = false;
    g_busy       = false;
    if(isUsb)
        hooks::OnIrLoaded(HookCtx(), g_usb[idx - g_nSd].slot, &g_usb[idx - g_nSd]);
    else
        hooks::OnIrLoaded(HookCtx(), -1, nullptr);
#ifdef PUREIR_DIAG
    DLOG("LOAD ok idx=%d name=%s len=%d samples after %d ms", idx, g_curName, (int)res.len, (int)(System::GetNow() - g_dLoadStart));
#endif
    return true;
}

// ---------------------------------------------------------------------------
// display link
// ---------------------------------------------------------------------------
static void TxDone(void*, UartHandler::Result) { g_txBusy = false; }

static int ToDb(float lin)
{
    if(lin < 1e-5f)
        return -99;
    return (int)std::lround(20.f * std::log10(lin));
}

static void SendStatus(bool overload)
{
    using namespace pureir;
    static uint32_t txStart = 0;
    if(g_txBusy)
    {
        if(System::GetNow() - txStart > 100)
            g_txBusy = false; // lost callback: recover
        return;
    }
    const Meters m = g_chain.TakeMeters();
    LinkStatus   s{};
    s.bank    = g_params.Bank();
    s.engine  = g_lastEngine < 0 ? 0 : g_lastEngine;
    s.irIndex = g_irIndex;
    s.irCount = g_irCount;
    s.inDb    = ToDb(m.inPeak);
    s.outDb   = ToDb(m.outPeak);
    s.grDb    = (int)std::lround(m.grDb);
    s.cpu     = (int)std::lround(g_cpu.GetAvgCpuLoad() * 100.f);
    uint16_t f = 0;
    if(g_chain.Bypassed()) f |= kFlagBypass;
    if(g_chain.Muted()) f |= kFlagMute;
    if(g_busy) f |= kFlagLoading;
    if(g_error) f |= kFlagError;
    if(g_sdOk) f |= kFlagSd;
    if(m.clip) f |= kFlagClip;
    if(g_curBuiltin) f |= kFlagBuiltin;
    if(overload) f |= kFlagOverload;
    s.flags = f;
    for(int i = 0; i < kNumParams; ++i)
        s.norm[i] = g_params.Norm(i);
    s.irName = g_curName;
    hooks::OnStatus(HookCtx(), s);
    const size_t n = BuildFrame(g_tx, sizeof(g_tx), s);
    if(n)
    {
        g_txBusy = true;
        txStart  = System::GetNow();
        if(g_uart.DmaTransmit((uint8_t*)g_tx, n, nullptr, TxDone, nullptr) != UartHandler::Result::OK)
            g_txBusy = false;
    }
}

// ---------------------------------------------------------------------------
int main()
{
    using namespace pureir;
    hw.Init();
    hw.seed.StartLog(false); // USB serial (does not wait for a PC): used by the IR uploader and the diagnostic log
    hw.seed.usb_handle.SetReceiveCallback(UsbRx, UsbHandle::FS_INTERNAL);
    hw.seed.SetAudioBlockSize(conv::kBlock);
    hw.seed.SetAudioSampleRate(SaiHandle::Config::SampleRate::SAI_48KHZ);
    g_cpu.Init(hw.seed.AudioSampleRate(), hw.seed.AudioBlockSize());

    // ---- DSP ----------------------------------------------------------------
    irload::BuildKernel(g_kernel);
    conv::Memory cm{{g_irA0, g_irA1},
                    {g_irB0, g_irB1},
                    g_fdlA,
                    g_fdlB,
                    g_inRing,
                    g_outRing,
                    g_work,
                    g_build};
    g_chain.Init(cm, g_reverb);
    g_params.Init();
    for(int i = 0; i < kNumParams; ++i)
        g_lastV[i] = -1e9f;
    hooks::Setup(HookCtx());

    // ---- display UART (TX only) ----------------------------------------------
    UartHandler::Config uc;
    uc.periph         = UartHandler::Config::Peripheral::UART_4;
    uc.mode           = UartHandler::Config::Mode::TX;
    uc.baudrate       = 115200;
    uc.pin_config.tx  = seed::D12;
    uc.pin_config.rx  = Pin();
    g_uart.Init(uc);

    // ---- storage -----------------------------------------------------------
    g_fsi.Init(FatFSInterface::Config::Media::MEDIA_SD);
    RecountIrs();
    MountAndScan(); // the card-detect switch is not trusted at boot
    ScanUsb();
#ifdef PUREIR_DIAG
    DLOG("PureIR DIAG boot: sdOk=%d sdFiles=%d totalIrs=%d", (int)g_sdOk, g_nSd, g_irCount);
#endif

    // audio first: the pedal is playable (neutral IR) while the IR loads
    hw.seed.StartAudio(AudioCallback);

    int      first    = 0; // first SD file, else first built-in
    bool     firstTry = true;
    uint32_t tSend = 0, tLed = 0, tOverload = 0, tSd = 0;
    bool     overload = false;
    g_reqIr           = first;

#ifdef PUREIR_DIAG
    uint32_t tDiag = 0, loopsPrev = 0;
#endif
    while(true)
    {
#ifdef PUREIR_DIAG
        ++g_dLoops;
        {
            const uint32_t nowD = System::GetNow();
            if(nowD - tDiag >= 1000)
            {
                const uint32_t dt = nowD - tDiag;
                const uint32_t lps = (g_dLoops - loopsPrev) * 1000u / (dt ? dt : 1u);
                tDiag = nowD; loopsPrev = g_dLoops;
                DLOG("t=%ds loops/s=%d cpu=%d%% fsw1 pin=%d edges=%d | fsw2 pin=%d edges=%d | ir=%d '%s' busy=%d err=%d byp=%d",
                     (int)(nowD / 1000), (int)lps, (int)(g_cpu.GetAvgCpuLoad() * 100.f),
                     (int)hw.fsw_1.Pressed(), (int)g_dFsw1Edges, (int)hw.fsw_2.Pressed(), (int)g_dFsw2Edges,
                     g_irIndex, g_curName, (int)g_busy, (int)g_error, (int)g_chain.Bypassed());
            }
        }
#endif
        // keep zone-B convolution fed (this is the most important job)
        while(g_chain.Conv().ServiceBackground()) {}
        if(g_chain.Conv().TakeOverload())
        {
            overload  = true;
            tOverload = System::GetNow();
        }
        const uint32_t now = System::GetNow();
        if(overload && now - tOverload > 2000)
            overload = false;

        // IRs sent over USB
        g_up.Poll(now);
        {
            static bool wasBusy = false, prevMute = false;
            const bool  busy    = g_up.Busy();
            if(busy && !wasBusy)
            {
                prevMute = g_chain.Muted();
                g_chain.SetMute(true); // flash work: keep the output quiet while uploading
            }
            else if(!busy && wasBusy)
                g_chain.SetMute(prevMute);
            wasBusy = busy;
        }
        const int changedSlot = g_up.TakeChanged();
        if(changedSlot >= 0)
        {
            ScanUsb();
            int pos = -1;
            for(int i = 0; i < g_nUsb; ++i)
                if(g_usb[i].slot == changedSlot)
                    pos = i;
            g_reqIr = pos >= 0 ? g_nSd + pos : 0; // play the new IR; after a delete go to the first
#ifdef PUREIR_DIAG
            DLOG("UPLOAD slot %d changed; %d IRs stored in flash", changedSlot, g_nUsb);
#endif
        }
        hooks::Poll(HookCtx(), now);
        if(g_up.TakeBoot())
            System::ResetToBootloader(System::BootloaderMode::DAISY_INFINITE_TIMEOUT);

        const int req = g_reqIr;
        if(req >= 0 && req < g_irCount)
        {
            g_reqIr = -1;
            if(!LoadIr(req) && firstTry && g_nSd > 0 && req < g_nSd)
                g_reqIr = g_nSd; // unreadable first file: fall back to built-in
            firstTry = false;
        }
        else if(req >= g_irCount)
            g_reqIr = -1;

        // late-inserted SD card
        if(!g_sdOk && hw.sd_detect.Pressed() && now - tSd > 2000)
        {
            tSd = now;
            MountAndScan();
            if(g_sdOk && g_nSd > 0)
                g_reqIr = 0;
        }

        if(now - tSend >= 100)
        {
            tSend = now;
            SendStatus(overload);
        }

        if(now != tLed)
        {
            tLed = now;
            hw.led1.Set(g_chain.Bypassed() ? 0.f : 1.f);
            // Only full-on / full-off colours: the LEDs are dimmed by switching them in this
            // loop (about 1 kHz), which is too slow for dim levels and made the idle green flicker.
            const bool blink = (now & 255) < 128;
            float r = 0.f, g = 1.f, b = 0.f;
            const bool normal = !(g_busy || g_up.Busy() || g_error || overload || g_chain.Muted());
            if(normal)
                hooks::NormalLed(HookCtx(), now, r, g, b);
            if(g_busy || g_up.Busy())
            {
                r = 0.f; g = 0.f; b = blink ? 1.f : 0.f;
            }
            else if(g_error)
            {
                r = 1.f; g = 0.f; b = 0.f;
            }
            else if(overload)
            {
                r = 1.f; g = 0.f; b = 1.f;
            }
            else if(g_chain.Muted())
            {
                r = blink ? 1.f : 0.f; g = blink ? 1.f : 0.f; b = 0.f; // yellow blink
            }
#ifdef PUREIR_DIAG
            // Diagnostic lights (bright, steady colours):
            //  LED2 blue   = loading            green  = card OK, IR loaded
            //  LED2 red    = IR load failed     yellow = card OK but no .wav files found
            //  LED2 cyan   = card not mounted (built-in IR in use)
            //  LED1 blinks N times every 2 s = last IR status (1 no file, 2 bad wav,
            //                                  3 unsupported, 4 empty, 5 read error)
            //  Seed LED: slow blink = loop running; fast blink = CPU load over 90%
            if(g_busy || g_up.Busy()) { r = 0.f; g = 0.f; b = 1.f; }
            else if(g_error)      { r = 1.f; g = 0.f; b = 0.f; }
            else if(!g_sdOk)      { r = 0.f; g = 1.f; b = 1.f; }
            else if(g_nSd == 0)   { r = 1.f; g = 1.f; b = 0.f; }
            else                  { r = 0.f; g = 1.f; b = 0.f; }
            float l1 = g_chain.Bypassed() ? 0.f : 1.f;
            if(g_lastSt > 0)
            {
                const uint32_t ph = now % 2000;      // 0..1999 ms
                const uint32_t k  = ph / 200;        // 0..9 slots of 200 ms
                l1 = (k < (uint32_t)g_lastSt * 2 && (k & 1) == 0) ? 1.f : 0.f;
            }
            hw.led1.Set(l1);
            hw.led2.Set(r, g, b);
            hw.led1.Update();
            hw.led2.Update();
            const bool hot = g_cpu.GetAvgCpuLoad() > 0.9f;
            hw.seed.SetLed(((now / (hot ? 100u : 500u)) & 1u) != 0);
#else
            hw.led2.Set(r, g, b);
            hw.led1.Update();
            hw.led2.Update();
            hw.seed.SetLed(g_cpu.GetAvgCpuLoad() > 0.9f);
#endif
        }
        System::DelayUs(250);
    }
}
