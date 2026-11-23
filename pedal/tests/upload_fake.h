// Fake 8 MB NOR flash for testing upload.h on a PC (erase sets 0xFF; write can only clear bits).
#pragma once
#include <cstdint>
#include <cstring>
#include <vector>
#include "upload.h"
struct FakeFlash
{
    std::vector<uint8_t> mem = std::vector<uint8_t>(8u << 20, 0xFF);
    int                  erases = 0, writes = 0;
    bool                 failWrite = false;
    pureir::store::FlashOps ops()
    {
        pureir::store::FlashOps o{};
        o.ctx = this;
        o.erase = [](uint32_t off, void* c) {
            auto* f = (FakeFlash*)c;
            if(off % 4096 || off + 4096 > f->mem.size()) return false;
            std::memset(&f->mem[off], 0xFF, 4096); ++f->erases; return true; };
        o.write = [](uint32_t off, const uint8_t* d, uint32_t n, void* c) {
            auto* f = (FakeFlash*)c;
            if(f->failWrite || off + n > f->mem.size()) return false;
            for(uint32_t i = 0; i < n; ++i) f->mem[off + i] &= d[i]; // NOR: can only clear bits
            ++f->writes; return true; };
        o.map = [](uint32_t off, void* c) { return (const uint8_t*)&((FakeFlash*)c)->mem[off]; };
        o.invalidate = [](uint32_t, uint32_t, void*) {};
        return o;
    }
};
