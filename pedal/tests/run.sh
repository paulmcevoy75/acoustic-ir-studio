#!/bin/sh
# Build and run all PC tests.
set -e
cd "$(dirname "$0")"
g++ -D_USE_MATH_DEFINES -std=c++17 -O2 -Wall -Wextra -I../src host_test.cpp -o /tmp/air_host_test
g++ -D_USE_MATH_DEFINES -std=c++17 -O2 -Wall -I../src display_parser_test.cpp -o /tmp/air_display_test
g++ -D_USE_MATH_DEFINES -std=c++17 -O2 -Wall -Wextra -I../src test_upload.cpp -o /tmp/air_upload_test
/tmp/air_upload_test
/tmp/air_display_test
/tmp/air_host_test

# end to end: the PC uploader (Go) against the pedal's upload code, through a virtual serial port
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) WIN=1;; *) WIN=0;; esac
if [ "$WIN" = 1 ] && command -v go >/dev/null 2>&1; then
    # Windows: no virtual serial port (pty), so only the Go uploader's own tests run here
    (cd ../tools/pureir-upload && go test ./...)
    echo "end-to-end upload test skipped (needs Linux)"
elif command -v go >/dev/null 2>&1; then
    g++ -D_USE_MATH_DEFINES -std=c++17 -O2 -Wall -I../src -I. upload_sim.cpp -o /tmp/air_upload_sim
    (cd ../tools/pureir-upload && go test ./... && go build -o /tmp/pureir-upload-linux .)
    python3 - <<'PY'
import numpy as np, wave, struct
r = np.random.default_rng(1); x = (r.standard_normal(24000) * np.exp(-np.arange(24000)/3000)).astype('float32')
open('/tmp/e2e_ir.wav','wb').write(b'')
import scipy.io.wavfile as w; w.write('/tmp/e2e_ir.wav', 48000, x)
PY
    python3 e2e_upload.py /tmp/pureir-upload-linux /tmp/air_upload_sim /tmp/e2e_ir.wav
fi
