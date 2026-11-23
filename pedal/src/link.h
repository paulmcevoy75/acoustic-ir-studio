// link.h - one-way status frames for the ESP32 display (UART, 115200 8N1).
//
//   $PUREIR,1,<bank>,<engine>,<irIdx>,<irCnt>,<flags>,<inDb>,<outDb>,<grDb>,<cpu>,
//        <v0>..<v17>,<irName>*HH\n
//
//  flags : bit0 bypass, bit1 mute, bit2 loading, bit3 error, bit4 SD present,
//          bit5 clip, bit6 builtin-IR, bit7 overload
//  dB values are integers (x1), cpu is percent, v0..v17 are knob positions
//  0..1000 for every parameter, irName is sanitised ASCII (<= 20 chars).
//  HH = XOR of every byte between '$' and '*', two upper-case hex digits.
#pragma once

#include "params.h"

namespace pureir
{
enum LinkFlags : uint16_t
{
    kFlagBypass   = 1 << 0,
    kFlagMute     = 1 << 1,
    kFlagLoading  = 1 << 2,
    kFlagError    = 1 << 3,
    kFlagSd       = 1 << 4,
    kFlagClip     = 1 << 5,
    kFlagBuiltin  = 1 << 6,
    kFlagOverload = 1 << 7,
    kFlagUser     = 1 << 8, // bits 8..15: not used by PureIR, free for builds that extend it
};

struct LinkStatus
{
    int         bank, engine, irIndex, irCount;
    uint16_t    flags;
    int         inDb, outDb, grDb, cpu;
    float       norm[kNumParams];
    const char* irName;
};

namespace linkdetail
{
inline char* PutInt(char* p, char* end, int v)
{
    if(end - p < 12)
        return p;
    if(v < 0)
    {
        *p++ = '-';
        v    = -v;
    }
    char t[11];
    int  n = 0;
    do
    {
        t[n++] = (char)('0' + v % 10);
        v /= 10;
    } while(v > 0);
    while(n > 0)
        *p++ = t[--n];
    return p;
}
} // namespace linkdetail

/** Writes a frame including '\n'.  Returns its length (0 if cap too small). */
inline size_t BuildFrame(char* out, size_t cap, const LinkStatus& s)
{
    using namespace linkdetail;
    char*       p   = out;
    char* const end = out + cap - 6; // room for "*HH\n"
    if(cap < 200)
        return 0;
    *p++ = '$';
#ifndef PUREIR_LINK_TAG
#define PUREIR_LINK_TAG "PUREIR"
#endif
    const char* tag = PUREIR_LINK_TAG ",1,";
    while(*tag)
        *p++ = *tag++;
    const int head[] = {s.bank, s.engine, s.irIndex, s.irCount, (int)s.flags,
                        s.inDb, s.outDb, s.grDb, s.cpu};
    for(int v : head)
    {
        p    = PutInt(p, end, v);
        *p++ = ',';
    }
    for(int i = 0; i < kNumParams; ++i)
    {
        p    = PutInt(p, end, (int)(Clampf(s.norm[i], 0.f, 1.f) * 1000.f + 0.5f));
        *p++ = ',';
    }
    int n = 0;
    for(const char* c = s.irName ? s.irName : ""; *c && n < 20 && p < end; ++c, ++n)
    {
        char ch = *c;
        if(ch < 32 || ch > 126 || ch == ',' || ch == '*' || ch == '$')
            ch = '_';
        *p++ = ch;
    }
    uint8_t x = 0;
    for(const char* q = out + 1; q < p; ++q)
        x ^= (uint8_t)*q;
    static const char hex[] = "0123456789ABCDEF";
    *p++                    = '*';
    *p++                    = hex[x >> 4];
    *p++                    = hex[x & 15];
    *p++                    = '\n';
    return (size_t)(p - out);
}
} // namespace pureir
