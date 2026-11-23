// The pedal's upload code driven through stdin/stdout, for testing the PC tool end to end.
#include <cstdio>
#include <unistd.h>
#include <fcntl.h>
#include <poll.h>
#include <time.h>
#include "upload_fake.h"
using namespace pureir::store;
static void Tx(const char* l, void*) { fputs(l, stdout); fflush(stdout); }
int main(int argc, char** argv)
{
    FakeFlash ff;
    if(argc > 1) { if(FILE* f = fopen(argv[1], "rb")) { if(fread(ff.mem.data(), 1, ff.mem.size(), f)) {} fclose(f); } }
    Uploader u(ff.ops(), Tx, nullptr, "sim");
    uint8_t buf[64];
    for(;;)
    {
        struct pollfd p{0, POLLIN, 0};
        if(poll(&p, 1, 1) > 0 && (p.revents & POLLIN))
        {
            ssize_t n = read(0, buf, sizeof buf);       // USB full speed delivers 64 bytes at a time
            if(n <= 0) break;
            u.Feed(buf, (uint32_t)n);
        }
        struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
        u.Poll((uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000));
        if(u.TakeBoot()) break;
        if(argc > 1 && u.TakeChanged() >= 0) { if(FILE* f = fopen(argv[1], "wb")) { fwrite(ff.mem.data(), 1, ff.mem.size(), f); fclose(f); } }
    }
}
