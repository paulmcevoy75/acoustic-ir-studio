// g++ -std=c++17 -I../src -I../display/PureIR_Display display_parser_test.cpp
#include <cstdio>
#include <cstring>
#include "link.h"
#include "../display/PureIR_Display/link_parser.h"
#include "../display/PureIR_Display/params_table.h"
using namespace pureir;
int main()
{
    int fails = 0;
    // table must match firmware
    for(int i = 0; i < kNumParams; ++i)
    {
        if(strcmp(kDParams[i].name, kParamDefs[i].name) || kDParams[i].lo != kParamDefs[i].lo
           || kDParams[i].hi != kParamDefs[i].hi || kDParams[i].exp != (kParamDefs[i].curve == kExp)
           || strcmp(kDParams[i].unit, kParamDefs[i].unit))
        { printf("FAIL table row %d\n", i); ++fails; }
    }
    LinkStatus s{}; s.bank = 2; s.engine = 3; s.irIndex = 4; s.irCount = 11;
    s.flags = kFlagSd | kFlagLoading | kFlagClip; s.inDb = -17; s.outDb = -3; s.grDb = 6; s.cpu = 37;
    for(int i = 0; i < kNumParams; ++i) s.norm[i] = (i * 53 % 101) / 100.f;
    s.irName = "Taylor 814 Body";
    char f[400]; size_t n = BuildFrame(f, sizeof f, s);
    FrameParser p; AirStatus a; bool got = false;
    // garbage before, one corrupted copy, then the good one
    const char* junk = "xx\xff$PUREIR,1,9\n";
    for(const char* c = junk; *c; ++c) got |= p.Feed(*c, a);
    char bad[400]; memcpy(bad, f, n); bad[10] ^= 1;
    for(size_t i = 0; i < n; ++i) got |= p.Feed(bad[i], a);
    if(got) { printf("FAIL corrupted frame accepted\n"); ++fails; }
    for(size_t i = 0; i < n; ++i) got |= p.Feed(f[i], a);
    if(!got) { printf("FAIL good frame rejected\n"); return 1; }
    if(a.bank != 2 || a.engine != 3 || a.irIdx != 4 || a.irCnt != 11 || a.flags != (F_SD | F_LOADING | F_CLIP)
       || a.inDb != -17 || a.outDb != -3 || a.grDb != 6 || a.cpu != 37 || strcmp(a.name, "Taylor 814 Body"))
    { printf("FAIL fields\n"); ++fails; }
    for(int i = 0; i < 18; ++i) if(a.v[i] != (int)((s.norm[i]) * 1000.f + 0.5f)) { printf("FAIL v[%d]=%d\n", i, a.v[i]); ++fails; }
    printf(fails ? "%d FAILURES\n" : "display parser: ALL PASSED\n", fails);
    return fails != 0;
}
