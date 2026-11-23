// link_parser.h - parses the status frames sent by the Daisy (see src/link.h).
// Plain C++, no Arduino dependencies, so it is unit-tested on a PC.
#pragma once
#include <stdint.h>
#include <string.h>

struct AirStatus
{
    int  bank = 0, engine = 0, irIdx = 0, irCnt = 0, flags = 0;
    int  inDb = -99, outDb = -99, grDb = 0, cpu = 0;
    int  v[18] = {0};          // knob positions, 0..1000
    char name[24] = "";
    char tag[16]  = "";        // who sent it: "PUREIR", or a longer name from an extended build
};

enum AirFlags
{
    F_BYPASS = 1, F_MUTE = 2, F_LOADING = 4, F_ERROR = 8,
    F_SD = 16, F_CLIP = 32, F_BUILTIN = 64, F_OVERLOAD = 128
};

class FrameParser
{
  public:
    /** Feed one received byte; returns true when a valid frame was stored in `out`. */
    bool Feed(char c, AirStatus& out)
    {
        if(c == '$') { n_ = 0; active_ = true; return false; }
        if(!active_) return false;
        if(c == '\n' || c == '\r')
        {
            active_ = false;
            return n_ > 4 && Parse(out);
        }
        if(n_ >= (int)sizeof(buf_) - 1) { active_ = false; return false; }
        buf_[n_++] = c;
        return false;
    }

  private:
    static int Hex(char c)
    {
        if(c >= '0' && c <= '9') return c - '0';
        if(c >= 'A' && c <= 'F') return c - 'A' + 10;
        if(c >= 'a' && c <= 'f') return c - 'a' + 10;
        return -1;
    }
    bool Parse(AirStatus& out)
    {
        buf_[n_] = 0;
        // checksum: "<payload>*HH"
        int star = -1;
        for(int i = n_ - 1; i >= 0; --i) if(buf_[i] == '*') { star = i; break; }
        if(star < 0 || n_ - star != 3) return false;
        const int h1 = Hex(buf_[star + 1]), h2 = Hex(buf_[star + 2]);
        if(h1 < 0 || h2 < 0) return false;
        uint8_t x = 0;
        for(int i = 0; i < star; ++i) x ^= (uint8_t)buf_[i];
        if(x != (uint8_t)(h1 * 16 + h2)) return false;
        buf_[star] = 0;

        // tag: 1..15 of A-Z 0-9 - _ up to the first comma (PureIR sends "PUREIR")
        char* p = buf_;
        AirStatus s;
        int   t = 0;
        while(p[t] && p[t] != ',')
        {
            const char c = p[t];
            if(t >= (int)sizeof(s.tag) - 1
               || !((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_'))
                return false;
            s.tag[t] = c;
            ++t;
        }
        if(t == 0 || p[t] != ',') return false;
        s.tag[t] = 0;
        p += t + 1;
        int ver;
        if(!Int(p, ver) || ver != 1) return false;
        int* head[] = {&s.bank, &s.engine, &s.irIdx, &s.irCnt, &s.flags,
                       &s.inDb, &s.outDb, &s.grDb, &s.cpu};
        for(int* d : head) if(!Int(p, *d)) return false;
        for(int i = 0; i < 18; ++i) if(!Int(p, s.v[i])) return false;
        int k = 0;
        while(*p && k < (int)sizeof(s.name) - 1) s.name[k++] = *p++;
        s.name[k] = 0;
        if(s.bank < 0 || s.bank > 2 || s.engine < 0 || s.engine > 3) return false;
        out = s;
        return true;
    }
    // reads "<int>," and advances p
    static bool Int(char*& p, int& v)
    {
        bool neg = false, any = false;
        long r = 0;
        if(*p == '-') { neg = true; ++p; }
        while(*p >= '0' && *p <= '9') { r = r * 10 + (*p - '0'); any = true; ++p; }
        if(!any || *p != ',') return false;
        ++p;
        v = (int)(neg ? -r : r);
        return true;
    }
    char buf_[256];
    int  n_ = 0;
    bool active_ = false;
};
