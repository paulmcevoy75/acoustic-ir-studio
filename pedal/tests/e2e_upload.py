#!/usr/bin/env python3
"""End to end: the PC uploader (real Go code) <-> a virtual serial port <-> the pedal's upload code
(src/upload.h running on the PC with a fake flash chip).  Usage: e2e_upload.py <uploader-binary> <sim-binary> <wav> [<wav2>]"""
import os, pty, select, subprocess, sys, threading, tty, time

uploader, sim, wav1 = sys.argv[1:4]
wav2 = sys.argv[4] if len(sys.argv) > 4 else wav1
master, slave = pty.openpty()
tty.setraw(slave)
slave_name = os.ttyname(slave)
simp = subprocess.Popen([sim], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
stop = False

def pump_to_sim():          # PC -> pedal, 64 bytes at a time like USB full speed
    while not stop:
        r, _, _ = select.select([master], [], [], 0.05)
        if r:
            try: d = os.read(master, 4096)
            except OSError: return
            for i in range(0, len(d), 64):
                simp.stdin.write(d[i:i+64]); simp.stdin.flush()
def pump_from_sim():        # pedal -> PC
    while not stop:
        d = simp.stdout.read1(256) if hasattr(simp.stdout, 'read1') else os.read(simp.stdout.fileno(), 256)
        if not d: return
        os.write(master, d)
for f in (pump_to_sim, pump_from_sim): threading.Thread(target=f, daemon=True).start()

def run(*args, expect_rc=0):
    p = subprocess.run([uploader, "--port", slave_name, "--no-pause", *args], capture_output=True, text=True, timeout=120)
    print("$ pureir-upload", *args, "\n" + p.stdout.replace("\r", "\n").strip().splitlines()[-1] if p.stdout.strip() else "", flush=True)
    assert p.returncode == expect_rc, (p.returncode, p.stdout, p.stderr)
    return p.stdout

def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg, flush=True)
    if not cond: sys.exit(1)

out = run("list");                                  check("(no uploaded IRs yet)" in out, "empty pedal lists nothing")
out = run(wav1);                                    check("stored in slot 0" in out, "first file goes to slot 0")
out = run("--slot", "3", "--name", "Ultimate One", wav2); check("stored in slot 3" in out, "explicit slot and name")
out = run("list");                                  check("slot 0" in out and "slot 3: Ultimate One" in out, "list shows both")
out = run(wav1, wav2);                              check("slot 1" in out and "slot 2" in out, "two files fill the next free slots")
out = run("delete", "0");                           check("Slot 0 is now empty" in out, "delete")
out = run("list");                                  check("slot 0" not in out and "slot 1" in out, "slot 0 gone")
for i in range(5): run(wav1)                         # fill up: slots 0,4,5,6,7 -> full
out = run(wav1, expect_rc=1);                       check("all 8 slots are full" in out, "full pedal is reported, nothing overwritten")
bad = "/tmp/not_a_wav.wav"; open(bad, "wb").write(b"hello")
out = run(bad, "--slot", "0", expect_rc=1);         check("not a WAV" in out, "garbage file rejected")
simp_alive = simp.poll() is None
out = run("boot");                                  check("restarting into the bootloader" in out, "boot command")
time.sleep(0.3); check(simp.poll() is not None, "pedal simulator left for the bootloader")
stop = True
print("END TO END: all checks passed")
