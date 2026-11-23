#include <cassert>
#include <cstdio>
#include <string>
#include <vector>
#include "upload_fake.h"
using namespace pureir::store;

static std::string g_out;
static void Tx(const char* l, void*) { g_out += l; }

static void Pump(Uploader& u, const std::string& in, uint32_t& t)
{
    u.Feed((const uint8_t*)in.data(), (uint32_t)in.size());
    for(int i = 0; i < 400; ++i) u.Poll(t += 1);
}
static bool Has(const char* s) { return g_out.find(s) != std::string::npos; }

// send a whole IR the way the PC tool does (chunk, wait for @A)
static std::string Put(Uploader& u, uint32_t& t, int slot, const std::vector<int16_t>& s, const char* name, uint32_t crcOverride = 0, bool useOverride = false)
{
    const uint8_t* raw = (const uint8_t*)s.data();
    const uint32_t bytes = (uint32_t)s.size() * 2;
    const uint32_t crc = useOverride ? crcOverride : Crc32(0, raw, bytes);
    g_out.clear();
    char cmd[128]; std::snprintf(cmd, sizeof cmd, "PUT %d %u %08x %s\n", slot, (unsigned)s.size(), crc, name);
    Pump(u, cmd, t);
    for(int i = 0; i < 5000 && !Has("@READY") && !Has("@ERR"); ++i) u.Poll(t += 1);
    if(!Has("@READY")) return g_out;
    for(uint32_t off = 0; off < bytes; off += kChunk)
    {
        const uint32_t n = std::min<uint32_t>(kChunk, bytes - off);
        g_out.clear();
        u.Feed(raw + off, n);
        for(int i = 0; i < 50; ++i) u.Poll(t += 1);
        if(Has("@ERR")) return g_out;
        if(off + n < bytes && !Has("@A")) { fprintf(stderr,"no @A slot=%d name=%s off=%u out=[%s]\n",slot,name,off,g_out.c_str()); assert(false);}
    }
    return g_out;
}

int main()
{
    FakeFlash ff; uint32_t t = 0;
    Uploader u(ff.ops(), Tx, nullptr, "test");
    SlotInfo si[kNumSlots];
    assert(ScanSlots(ff.ops(), si) == 0);

    g_out.clear(); Pump(u, "PING\n", t); assert(Has("@PUREIR 1 test slots 8"));
    g_out.clear(); Pump(u, "LIST\n", t);  assert(Has("@END") && !Has("@SLOT"));

    std::vector<int16_t> a(48000), b(24000), big(kMaxSamples);
    for(size_t i = 0; i < a.size(); ++i) a[i] = (int16_t)((i * 37) % 30000 - 15000);
    for(size_t i = 0; i < b.size(); ++i) b[i] = (int16_t)((i * 11) % 20000 - 10000);
    for(size_t i = 0; i < big.size(); ++i) big[i] = (int16_t)((i * 7) % 32000 - 16000);

    // normal upload, name with spaces
    std::string r = Put(u, t, 2, a, "My Guitar One"); assert(r.find("@OK 2") != std::string::npos);
    assert(u.TakeChanged() == 2 && u.TakeChanged() == -1);
    int n = ScanSlots(ff.ops(), si);
    assert(n == 1 && si[0].slot == 2 && si[0].length == 48000 && si[0].rate == 48000);
    assert(std::string(si[0].name) == "My Guitar One");
    assert(std::memcmp(si[0].data, a.data(), a.size() * 2) == 0);

    // second slot, then list
    r = Put(u, t, 0, b, "Short"); assert(r.find("@OK 0") != std::string::npos);
    g_out.clear(); Pump(u, "LIST\n", t);
    assert(Has("@SLOT 0 24000 48000 Short") && Has("@SLOT 2 48000 48000 My Guitar One") && Has("@END"));

    // overwrite slot 2 with the longest IR (4 s)
    r = Put(u, t, 2, big, "Four seconds"); assert(r.find("@OK 2") != std::string::npos);
    n = ScanSlots(ff.ops(), si); assert(n == 2 && si[1].length == kMaxSamples);
    assert(std::memcmp(si[1].data, big.data(), big.size() * 2) == 0);
    // slot 0 untouched
    assert(std::memcmp(si[0].data, b.data(), b.size() * 2) == 0);

    // wrong CRC: refused, slot stays empty (header never written)
    r = Put(u, t, 5, a, "Bad", 0xdeadbeef, true); assert(r.find("@ERR crc") != std::string::npos);
    n = ScanSlots(ff.ops(), si); assert(n == 2);
    assert(!u.Busy());

    // interrupted upload leaves the slot invalid, and the previous content is gone but nothing half-valid shows
    g_out.clear();
    char cmd[96]; std::snprintf(cmd, sizeof cmd, "PUT 0 48000 %08x Half\n", Crc32(0, (const uint8_t*)a.data(), a.size() * 2));
    Pump(u, cmd, t); for(int i = 0; i < 5000 && !Has("@READY"); ++i) u.Poll(t += 1);
    u.Feed((const uint8_t*)a.data(), 1000); for(int i = 0; i < 20; ++i) u.Poll(t += 1);
    assert(u.Busy());
    for(int i = 0; i < 20; ++i) u.Poll(t += 1000);   // host went away
    assert(Has("@ERR timeout") && !u.Busy());
    n = ScanSlots(ff.ops(), si); assert(n == 1 && si[0].slot == 2);

    // delete
    g_out.clear(); Pump(u, "DEL 2\n", t); assert(Has("@OK 2") && u.TakeChanged() == 2);
    assert(ScanSlots(ff.ops(), si) == 0);

    // bad input
    g_out.clear(); Pump(u, "PUT 9 100 0 x\n", t); assert(Has("@ERR bad slot"));
    g_out.clear(); Pump(u, "PUT 1 0 0 x\n", t);   assert(Has("@ERR bad length"));
    g_out.clear(); Pump(u, "PUT 1 999999 0 x\n", t); assert(Has("@ERR bad length"));
    g_out.clear(); Pump(u, "FOO\n", t); assert(Has("@ERR unknown"));
    g_out.clear(); Pump(u, "DEL x\n", t); assert(Has("@ERR bad slot"));

    // flash write failure is reported
    ff.failWrite = true; r = Put(u, t, 1, b, "X"); assert(r.find("@ERR flash") != std::string::npos);
    ff.failWrite = false;

    // BOOT
    g_out.clear(); Pump(u, "BOOT\r\n", t); assert(u.TakeBoot() && Has("@OK"));

    // name-less PUT works
    r = Put(u, t, 3, b, ""); assert(r.find("@OK 3") != std::string::npos);

    // nothing outside the slot area was ever erased/written (the firmware lives below kBase)
    for(uint32_t i = 0; i < kBase; ++i) assert(ff.mem[i] == 0xFF);
    printf("upload protocol: all tests passed (%d erases, %d writes)\n", ff.erases, ff.writes);
    return 0;
}
