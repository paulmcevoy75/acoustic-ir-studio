// USB IR upload: stores up to 8 IRs in the Daisy's QSPI flash, written over USB serial.
//
// Portable on purpose (no libDaisy includes) so it can be tested on a PC against a
// simulated flash chip (tests/test_upload.cpp).  main.cpp supplies the real flash.
//
// Flash layout (offsets inside the 8 MB QSPI chip; the Daisy bootloader keeps the
// firmware at 0x40000 and it is well under 1 MB, so 0x100000 and up is free):
//   slot i starts at kBase + i * kSlotSize (512 KB each, 8 slots = 4 MB)
//     +0x0000  header sector (4 KB): written LAST, so a half-finished upload is ignored
//     +0x1000  IR samples, 16-bit little-endian mono at `rate`
//
// Protocol (PC -> pedal text lines; every reply line starts with '@'):
//   PING                       -> @PUREIR 1 <firmware> slots <n>
//   LIST                       -> @SLOT <i> <samples> <rate> <name>   (one per used slot) then @END
//   PUT <slot> <samples> <crc32> <name...>
//                              -> @READY   (after erasing; can take several seconds)
//      then <samples*2> bytes in chunks of up to 4096, waiting for @A after each chunk
//                              -> @OK <slot>   or   @ERR <reason>
//   DEL <slot>                 -> @OK <slot>
//   ABORT                      -> @OK 0
//   BOOT                       -> @OK 0 then the pedal restarts into the bootloader
#pragma once
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>

namespace pureir
{
namespace store
{
constexpr uint32_t kBase       = 0x100000;
constexpr uint32_t kSlotSize   = 0x80000;
constexpr int      kNumSlots   = 8;
constexpr uint32_t kHdrSize    = 0x1000;
constexpr uint32_t kChunk      = 4096;
constexpr uint32_t kMaxSamples = 192000; // 4 s at 48 kHz (must match conv::kMaxIrSamples)
constexpr uint32_t kMagic      = 0x53524950; // "PIRS"
constexpr uint32_t kNameLen    = 24;

struct Header
{
    uint32_t magic;
    uint32_t version; // 1
    uint32_t length;  // samples
    uint32_t rate;    // Hz
    uint32_t crc;     // CRC-32 of the sample bytes
    uint32_t dataOff; // kHdrSize
    char     name[kNameLen];
};
static_assert(sizeof(Header) == 48, "header layout");

struct FlashOps
{
    bool (*erase)(uint32_t off, void* ctx);                       // erase ONE 4 KB sector
    bool (*write)(uint32_t off, const uint8_t* d, uint32_t n, void* ctx);
    const uint8_t* (*map)(uint32_t off, void* ctx);               // read pointer
    void (*invalidate)(uint32_t off, uint32_t n, void* ctx);      // drop CPU cache for a range
    void* ctx;
};

struct SlotInfo
{
    int            slot;
    char           name[kNameLen];
    uint32_t       length;
    uint32_t       rate;
    const int16_t* data;
};

inline uint32_t Crc32(uint32_t crc, const uint8_t* p, uint32_t n)
{
    crc = ~crc;
    for(uint32_t i = 0; i < n; ++i)
    {
        crc ^= p[i];
        for(int b = 0; b < 8; ++b)
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

inline uint32_t SlotOffset(int slot) { return kBase + (uint32_t)slot * kSlotSize; }

/** Reads the headers of all slots. Returns how many are valid (ordered by slot number). */
inline int ScanSlots(const FlashOps& f, SlotInfo* out)
{
    int n = 0;
    for(int s = 0; s < kNumSlots; ++s)
    {
        f.invalidate(SlotOffset(s), sizeof(Header), f.ctx);
        Header h;
        std::memcpy(&h, f.map(SlotOffset(s), f.ctx), sizeof(h));
        if(h.magic != kMagic || h.version != 1 || h.dataOff != kHdrSize || h.length == 0
           || h.length > kMaxSamples || h.rate < 8000 || h.rate > 192000)
            continue;
        SlotInfo& o = out[n++];
        o.slot      = s;
        std::memcpy(o.name, h.name, kNameLen);
        o.name[kNameLen - 1] = 0;
        o.length             = h.length;
        o.rate               = h.rate;
        o.data               = (const int16_t*)f.map(SlotOffset(s) + kHdrSize, f.ctx);
    }
    return n;
}

class Uploader
{
  public:
    using TxFn = void (*)(const char* line, void* ctx);

    Uploader(const FlashOps& f, TxFn tx, void* txctx, const char* firmware)
    : f_(f), tx_(tx), txctx_(txctx), fw_(firmware)
    {
    }

    /** Called from the USB receive interrupt: just queue the bytes. */
    void Feed(const uint8_t* d, uint32_t n)
    {
        for(uint32_t i = 0; i < n; ++i)
        {
            const uint32_t next = (head_ + 1) % kRing;
            if(next == tail_)
            {
                overflow_ = true;
                return;
            }
            ring_[head_] = d[i];
            head_        = next;
        }
    }

    /** Called often from the main loop.  One flash sector is erased per call. */
    void Poll(uint32_t nowMs)
    {
        if(overflow_)
        {
            overflow_ = false;
            if(state_ != kLine)
                Fail("overflow");
        }
        if(state_ == kErase)
        {
            StepErase();
            return;
        }
        while(tail_ != head_)
        {
            const uint8_t c = ring_[tail_];
            tail_           = (tail_ + 1) % kRing;
            lastByte_       = nowMs;
            if(state_ == kLine)
                OnLineByte(c);
            else
                OnDataByte(c);
            if(state_ == kErase)
                return;
        }
        if(state_ == kData && nowMs - lastByte_ > 8000)
            Fail("timeout");
    }

    bool Busy() const { return state_ != kLine; }
    /** Slot that was just stored / deleted, or -1.  Cleared by reading. */
    int TakeChanged()
    {
        const int s = changed_;
        changed_    = -1;
        return s;
    }
    bool TakeBoot()
    {
        const bool b = boot_;
        boot_        = false;
        return b;
    }

  private:
    enum State
    {
        kLine,
        kErase,
        kData
    };
    static constexpr uint32_t kRing = 16384;

    void Say(const char* fmt, int a = 0, int b = 0, int c = 0, const char* s = "")
    {
        char buf[96];
        std::snprintf(buf, sizeof(buf), fmt, a, b, c, s);
        tx_(buf, txctx_);
    }
    void Fail(const char* why)
    {
        state_ = kLine;
        len_   = 0;
        char buf[64];
        std::snprintf(buf, sizeof(buf), "@ERR %s\n", why);
        tx_(buf, txctx_);
    }

    void OnLineByte(uint8_t c)
    {
        if(c == '\r')
            return;
        if(c == '\n')
        {
            line_[len_] = 0;
            len_        = 0;
            HandleLine();
            return;
        }
        if(len_ < sizeof(line_) - 1)
            line_[len_++] = (char)c;
        else
            len_ = 0; // garbage: start over
    }

    static bool Starts(const char* s, const char* w)
    {
        const size_t n = std::strlen(w);
        return std::strncmp(s, w, n) == 0 && (s[n] == 0 || s[n] == ' ');
    }

    void HandleLine()
    {
        if(Starts(line_, "PING"))
        {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "@PUREIR 1 %s slots %d\n", fw_, kNumSlots);
            tx_(buf, txctx_);
        }
        else if(Starts(line_, "LIST"))
        {
            SlotInfo si[kNumSlots];
            const int n = ScanSlots(f_, si);
            for(int i = 0; i < n; ++i)
            {
                char buf[96];
                std::snprintf(buf, sizeof(buf), "@SLOT %d %u %u %s\n", si[i].slot, (unsigned)si[i].length,
                              (unsigned)si[i].rate, si[i].name);
                tx_(buf, txctx_);
            }
            tx_("@END\n", txctx_);
        }
        else if(Starts(line_, "DEL"))
        {
            int slot = -1;
            if(std::sscanf(line_, "DEL %d", &slot) != 1 || slot < 0 || slot >= kNumSlots)
                return Fail("bad slot");
            if(!f_.erase(SlotOffset(slot), f_.ctx))
                return Fail("flash");
            f_.invalidate(SlotOffset(slot), kHdrSize, f_.ctx);
            changed_ = slot;
            Say("@OK %d\n", slot);
        }
        else if(Starts(line_, "PUT"))
        {
            int      slot = -1;
            unsigned n = 0, crc = 0;
            int      used = 0;
            if(std::sscanf(line_, "PUT %d %u %x %n", &slot, &n, &crc, &used) < 3 || used <= 0)
            {
                // name may be missing: sscanf stops before %n then
                if(std::sscanf(line_, "PUT %d %u %x", &slot, &n, &crc) != 3)
                    return Fail("bad command");
                used = (int)std::strlen(line_);
            }
            if(slot < 0 || slot >= kNumSlots)
                return Fail("bad slot");
            if(n == 0 || n > kMaxSamples)
                return Fail("bad length");
            std::memset(&hdr_, 0, sizeof(hdr_));
            hdr_.magic   = kMagic;
            hdr_.version = 1;
            hdr_.length  = n;
            hdr_.rate    = 48000;
            hdr_.crc     = crc;
            hdr_.dataOff = kHdrSize;
            const char* nm = line_ + used;
            size_t      nl = std::strlen(nm);
            if(nl >= kNameLen)
                nl = kNameLen - 1;
            std::memcpy(hdr_.name, nm, nl);
            slot_      = slot;
            total_     = n * 2;
            written_   = 0;
            crcRun_    = 0;
            eraseNext_ = 0;
            eraseLeft_ = 1 + (total_ + 4095) / 4096; // header sector + data sectors
            state_     = kErase;
        }
        else if(Starts(line_, "BOOT"))
        {
            Say("@OK %d\n", 0);
            boot_ = true;
        }
        else if(Starts(line_, "ABORT"))
            Say("@OK %d\n", 0);
        else if(line_[0] != 0)
            Fail("unknown command");
    }

    void StepErase()
    {
        if(!f_.erase(SlotOffset(slot_) + eraseNext_, f_.ctx))
            return Fail("flash erase");
        eraseNext_ += 4096;
        if(--eraseLeft_ > 0)
            return;
        f_.invalidate(SlotOffset(slot_), kSlotSize, f_.ctx);
        state_    = kData;
        chunkLen_ = 0;
        needNow_  = total_ < kChunk ? total_ : kChunk;
        Say("@READY\n");
    }

    void OnDataByte(uint8_t c)
    {
        chunk_[chunkLen_++] = c;
        if(chunkLen_ < needNow_)
            return;
        if(!f_.write(SlotOffset(slot_) + kHdrSize + written_, chunk_, chunkLen_, f_.ctx))
            return Fail("flash write");
        crcRun_ = Crc32(crcRun_, chunk_, chunkLen_);
        written_ += chunkLen_;
        chunkLen_ = 0;
        if(written_ < total_)
        {
            const uint32_t left = total_ - written_;
            needNow_            = left < kChunk ? left : kChunk;
            Say("@A\n");
            return;
        }
        Finish();
    }

    void Finish()
    {
        f_.invalidate(SlotOffset(slot_) + kHdrSize, total_, f_.ctx);
        // read back what is really in flash and check it
        const uint8_t* p   = f_.map(SlotOffset(slot_) + kHdrSize, f_.ctx);
        const uint32_t crc = Crc32(0, p, total_);
        if(crc != crcRun_ || crc != hdr_.crc)
            return Fail("crc mismatch");
        if(!f_.write(SlotOffset(slot_), (const uint8_t*)&hdr_, sizeof(hdr_), f_.ctx))
            return Fail("flash header");
        f_.invalidate(SlotOffset(slot_), kHdrSize, f_.ctx);
        state_   = kLine;
        changed_ = slot_;
        Say("@OK %d\n", slot_);
    }

    FlashOps    f_;
    TxFn        tx_;
    void*       txctx_;
    const char* fw_;

    uint8_t           ring_[kRing];
    volatile uint32_t head_ = 0, tail_ = 0;
    volatile bool     overflow_ = false;

    State    state_ = kLine;
    char     line_[96];
    uint32_t len_ = 0;

    Header   hdr_{};
    int      slot_ = 0;
    uint32_t total_ = 0, written_ = 0, crcRun_ = 0, eraseNext_ = 0, eraseLeft_ = 0;
    uint32_t chunkLen_ = 0, needNow_ = 0, lastByte_ = 0;
    uint8_t  chunk_[kChunk];

    int  changed_ = -1;
    bool boot_    = false;
};

} // namespace store
} // namespace pureir
