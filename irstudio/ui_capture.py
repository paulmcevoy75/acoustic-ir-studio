# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Capture tab: meters, live spectrum, guided session, takes and coverage."""

from __future__ import annotations

import threading
import time
import traceback

import matplotlib
import matplotlib.ticker
import numpy as np
import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from tkinter import messagebox
from tkinter import ttk

from . import audio
from . import engine as E
from . import win_volume as WV
from .audio import dev_index
from .config import APP_NAME, ROLES, SLOTS, SLOT_LABEL, STATUS_COL, log
from .util import dev_family


class CaptureTab:
    """Part of App (mixed in) - capture tab: meters, live spectrum, guided session, takes and coverage."""

    def _build_capture(self):
        t = self._scroll_body(self.tab_cap)
        top = ttk.Frame(t)
        top.pack(fill="x", padx=10, pady=8)
        ttk.Label(top, text="Records the instrument + every mic that's on, together.     Show coverage for:").pack(side="left")
        self.cov_radio = {}
        for s in SLOTS:
            self.cov_radio[s] = ttk.Radiobutton(top, text=SLOT_LABEL[s], value=s, variable=self.slot,
                                                command=self._slot_changed)
            self.cov_radio[s].pack(side="left", padx=8)
        self.audio_btn = ttk.Button(top, text="Start audio", command=self.toggle_audio)
        self.audio_btn.pack(side="right")

        mon = ttk.Frame(t)
        mon.pack(fill="x", padx=10, pady=(0, 2))
        self.v_mon = tk.BooleanVar(value=False)
        ttk.Checkbutton(mon, text="Headphone monitor", variable=self.v_mon,
                        command=self._monitor_changed).pack(side="left")
        self.mon_dev_lab = ttk.Label(mon, foreground="#555")
        self.mon_dev_lab.pack(side="left", padx=(4, 12))
        ttk.Label(mon, text="Master").pack(side="left")
        self.v_mon_master = tk.DoubleVar(value=-6.0)
        ttk.Scale(mon, from_=-40, to=6, variable=self.v_mon_master, length=150,
                  command=lambda e: self._monitor_levels()).pack(side="left", padx=4)
        self.mon_master_lab = ttk.Label(mon, text="-6 dB", width=7)
        self.mon_master_lab.pack(side="left")
        ttk.Button(mon, text="Reset peaks", command=self.reset_peaks).pack(side="right")
        ttk.Button(mon, text="Windows input levels...", command=WV.open_recording_settings).pack(side="right", padx=6)

        mrow = ttk.Frame(t)
        mrow.pack(fill="x", padx=10)
        spf = ttk.Frame(mrow)                    # packed first, so the meters can't squeeze it out
        spf.pack(side="right", fill="y", padx=(8, 0))
        meters = ttk.Frame(mrow)
        meters.pack(side="left", fill="x", expand=True)
        self.spec_fig = Figure(figsize=(3.4 if self.compact else 4.0, 1.6 if self.compact else 1.75), dpi=100)
        self.spec_ax = self.spec_fig.add_subplot(111)
        self.spec_canvas = FigureCanvasTkAgg(self.spec_fig, master=spf)
        self.spec_canvas.get_tk_widget().pack(fill="both", expand=True)
        sfo = ttk.Frame(spf)
        sfo.pack(fill="x")
        self.v_spec_match = tk.BooleanVar(value=True)
        smc = ttk.Checkbutton(sfo, text="Level-match (compare tone)", variable=self.v_spec_match)
        smc.pack(side="left")
        self._tip(smc, "On: every input is shifted to the same overall level so you see the difference "
                       "in TONE (e.g. the piezo's quack vs the mic). Off: real levels in dBFS.")
        self._tip(self.spec_canvas.get_tk_widget(), "Live spectrum of every input (last ~0.2 s, smoothed). "
                  "Play and watch how the piezo differs from the mics - that difference is what the IR "
                  "corrects.")
        self._spec_lines = {}
        self._spec_avg = {}
        self._spec_t = 0.0
        self._spec_init()
        self.meter = {}
        self.meter_name = {}
        self.meter_row = {}
        self.v_mute, self.v_solo, self.v_gain, self.gain_lab = {}, {}, {}, {}
        self.v_inlvl, self.inlvl_scale, self.inlvl_lab = {}, {}, {}
        for col, txt in ((1, "Mute"), (2, "Solo"), (3, "Headphone level"), (5, "Input level (USB)"),
                         (7, "Meter")):
            ttk.Label(meters, text=txt, foreground="#666", font=("Segoe UI", 8)).grid(row=0, column=col)
        style = ttk.Style(self)
        style.configure("MS.Toolbutton", padding=(6, 1))
        for i0, role in enumerate(ROLES):
            i = i0 + 1
            self.meter_name[role] = ttk.Label(meters, text=role, width=18 if self.compact else 24)
            self.meter_name[role].grid(row=i, column=0, sticky="w")
            self.v_mute[role] = tk.BooleanVar(value=False)
            self.v_solo[role] = tk.BooleanVar(value=False)
            ttk.Checkbutton(meters, text="M", style="MS.Toolbutton", variable=self.v_mute[role],
                            command=self._monitor_levels).grid(row=i, column=1, padx=1)
            ttk.Checkbutton(meters, text="S", style="MS.Toolbutton", variable=self.v_solo[role],
                            command=self._monitor_levels).grid(row=i, column=2, padx=(1, 6))
            self.v_gain[role] = tk.DoubleVar(value=0.0)
            ttk.Scale(meters, from_=-40, to=12, variable=self.v_gain[role], length=80 if self.compact else 120,
                      command=lambda e: self._monitor_levels()).grid(row=i, column=3)
            self.gain_lab[role] = ttk.Label(meters, text="0 dB", width=6)
            self.gain_lab[role].grid(row=i, column=4, padx=(2, 8))
            self.v_inlvl[role] = tk.DoubleVar(value=100.0)
            self.inlvl_scale[role] = ttk.Scale(meters, from_=0, to=100, variable=self.v_inlvl[role],
                                               length=70 if self.compact else 100,
                                               command=lambda e, r=role: self._set_input_level(r))
            self.inlvl_scale[role].grid(row=i, column=5)
            self.inlvl_lab[role] = ttk.Label(meters, text="", width=10)
            self.inlvl_lab[role].grid(row=i, column=6, padx=(2, 8))
            c = tk.Canvas(meters, height=22, width=120, bg="#1e1f22", highlightthickness=0)
            c.grid(row=i, column=7, sticky="we", pady=2)
            lab = tk.Label(meters, text="-inf dBFS", width=17 if self.compact else 22, anchor="w")
            lab.grid(row=i, column=8, padx=6)
            self.meter[role] = (c, lab)
            self.meter_row[role] = [w for w in meters.grid_slaves(row=i)]
        meters.columnconfigure(7, weight=1)
        ttk.Label(meters, foreground="#555", wraplength=760, justify="left", text=(
            "Recording level: interface inputs use the interface's gain knobs; a USB mic uses the 'Input level "
            "(USB)' slider (that's its Windows level). Aim for peaks of -12 to -6 dBFS on your hardest playing. "
            "The marker shows the highest peak - red above -3 dBFS, CLIP if it clipped. Mute / Solo / "
            "Headphone level only change what you hear, not the recording.")).grid(
            row=len(ROLES) + 1, column=0, columnspan=9, sticky="w", pady=(2, 0))

        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, padx=10, pady=6)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        g = ttk.LabelFrame(left, text="Guided session")
        g.pack(fill="x")
        sf_ = ttk.Frame(g)
        sf_.pack(fill="x", padx=10, pady=(6, 0))
        ttk.Label(sf_, text="Session").pack(side="left")
        self.v_session = tk.StringVar(value="Full (14 steps, ~5 min)")
        scb = ttk.Combobox(sf_, values=["Full (14 steps, ~5 min)", "Quick (merged steps, ~3.5 min)"],
                           textvariable=self.v_session, state="readonly", width=28)
        scb.pack(side="left", padx=6)
        scb.bind("<<ComboboxSelected>>", lambda e: self.goto_step(0))
        ttk.Button(sf_, text="Step lengths...", command=self.edit_step_lengths).pack(side="left")
        self.step_title = ttk.Label(g, style="Big.TLabel")
        self.step_title.pack(anchor="w", padx=10, pady=(8, 2))
        self.step_text = ttk.Label(g, style="Step.TLabel", wraplength=560, justify="left")
        self.step_text.pack(anchor="w", padx=10)
        self.rec_time = ttk.Label(g, text="", style="Big.TLabel", foreground="#c0392b")
        self.rec_time.pack(anchor="w", padx=10, pady=4)
        bf = ttk.Frame(g)
        bf.pack(fill="x", padx=10, pady=(0, 10))
        self.rec_btn = ttk.Button(bf, text="Record this step", style="Rec.TButton",
                                  command=self.record_step)
        self.rec_btn.pack(side="left")
        self.stop_btn = ttk.Button(bf, text="Stop", command=self.stop_record, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.ext_btn = ttk.Button(bf, text="+30 s", command=self.extend_record, state="disabled")
        self.ext_btn.pack(side="left")
        df = ttk.Frame(g)
        df.pack(fill="x", padx=10, pady=(0, 8))
        self.demo_btn = ttk.Button(df, text="Record demo", command=self.record_demo)
        self.demo_btn.pack(side="left")
        self.demo_lab = ttk.Label(df, text="No demo yet - play something typical (a song section, ~30 s); "
                                  "the Build tab's Audition plays it back every way.", foreground="#555",
                                  wraplength=470, justify="left")
        self.demo_lab.pack(side="left", padx=8)
        ttk.Button(bf, text="< Prev", command=lambda: self.goto_step(self.step_i - 1)).pack(side="left", padx=(16, 2))
        ttk.Button(bf, text="Skip >", command=lambda: self.goto_step(self.step_i + 1)).pack(side="left")
        ttk.Button(bf, text="Free take", command=self.record_free).pack(side="left", padx=(16, 2))
        ttk.Button(bf, text="Undo last take", command=self.undo_take).pack(side="left", padx=2)

        tipf = ttk.LabelFrame(left, text="What to play next")
        tipf.pack(fill="x", pady=8)
        self.tips = ttk.Label(tipf, wraplength=560, justify="left")
        self.tips.pack(anchor="w", padx=10, pady=6)

        lf = ttk.LabelFrame(left, text="Take log")
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, height=8, wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        self.cov_summary = ttk.Label(right, text="", justify="left", wraplength=440)
        self.cov_summary.pack(anchor="w", pady=(0, 6))
        cf = ttk.LabelFrame(right, text="Coverage - how certain the IR is in each band")
        cf.pack(fill="both", expand=True)
        self.cov_fig = Figure(figsize=(5.6, 2.2 if self.compact else 3.0), dpi=100)
        self.cov_ax = self.cov_fig.add_subplot(111)
        self.cov_canvas = FigureCanvasTkAgg(self.cov_fig, master=cf)
        self.cov_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.goto_step(0)
        self._update_coverage()
        self._inputs_changed()

    def _slot_changed(self):
        self._update_coverage()

    def toggle_audio(self):
        if self.rig.running():
            self.stop_audio()
        else:
            self.start_audio()

    def start_audio(self):
        if audio.sd is None:
            messagebox.showerror(APP_NAME, "Audio library not available.")
            return False
        try:
            cfg = self.input_config()
            self.rig.start(cfg, int(self.fs.get()))
        except Exception as exc:
            msg = str(exc)
            if "sample rate" in msg.lower() or "-9997" in msg:
                msg += ("\n\nThe device isn't running at the chosen rate. Set it in Windows "
                        "(Sound settings > device > Advanced) or in your interface's control panel, or "
                        "pick the same rate here.")
            messagebox.showerror(APP_NAME, "Couldn't open audio:\n" + msg)
            return False
        self.audio_btn.configure(text="Stop audio")
        self.save_devices()
        log.info("audio started: %s @ %s", cfg, self.fs.get())
        if self.v_mon.get():
            self._start_monitor()
        mics = " + ".join(self.mic_name(r) for r in SLOTS if r in cfg)
        ndev = len({d for d, _ in cfg.values()})
        self.status.set(f"Audio running - piezo + {mics} on {ndev} device(s) @ {self.fs.get()} Hz")
        if self.rig.warning:
            self.logln("WARNING: " + self.rig.warning)
        return True

    def stop_audio(self):
        if self.rig.recording:
            self.stop_record()
        self.rig.stop()
        self.audio_btn.configure(text="Start audio")
        self.status.set("Audio stopped.")

    def goto_step(self, i):
        n = len(self.steps())
        if i >= n:
            self.step_i = n
            self.step_title.configure(text="Guided session complete")
            self.step_text.configure(text="Everything is autosaved. Check the coverage chart - use "
                                     "'Free take' for anything 'What to play next' suggests - then go "
                                     "to Build & Export.")
            self.rec_btn.configure(state="disabled")
            return
        self.step_i = max(0, i)
        name, dur, txt = self.steps()[self.step_i]
        self.step_title.configure(text=f"Step {self.step_i + 1} of {n}:  {name}")
        if dur:
            self.step_text.configure(text=f"{txt}\n(about {dur} s - stops automatically; "
                                     "press +30 s if you need longer)")
        else:
            self.step_text.configure(text=f"{txt}\n(no time limit - press Stop when you're done)")
        if not self.rig.recording:
            self.rec_btn.configure(state="normal")

    def _ensure_audio(self):
        if not self.rig.running():
            return self.start_audio()
        return True

    def record_step(self):
        if self.busy:
            messagebox.showinfo(APP_NAME, "Still analysing the last take - one moment.")
            return
        if self.step_i >= len(self.steps()) or not self._ensure_audio():
            return
        name, dur, _ = self.steps()[self.step_i]
        self._start_rec(f"{self.step_i + 1}. {name}", dur, step=True)

    def record_free(self):
        if self.busy:
            messagebox.showinfo(APP_NAME, "Still analysing the last take - one moment.")
            return
        if not self._ensure_audio():
            return
        self._start_rec("free take", None)

    def _start_rec(self, label, limit, step=False):
        self.rig.begin()
        self.rec_started = time.time()
        self.rec_limit = limit
        self.rec_label = label
        self.rec_is_step = step
        self.rec_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.ext_btn.configure(state="normal" if limit else "disabled")
        log.info("recording '%s' (%s)", label, f"{limit} s" if limit else "no limit")

    def extend_record(self):
        if self.rig.recording and self.rec_limit:
            self.rec_limit += 30

    def _refresh_input_levels(self):
        """Enable the input-level slider for devices whose level Windows can set
        (USB mics); interface inputs are set with their hardware gain knobs."""
        for role in ROLES:
            m = self._sel(role)
            lvl = None
            p = self._sel("piezo")
            iface = m is not None and (
                (getattr(self, "iface_family", "") and dev_family(m[2]) == self.iface_family) or
                (p is not None and dev_family(m[2]) == dev_family(p[2])))
            if m is not None and not iface and WV.available():
                try:
                    lvl = WV.get_level(m[2])
                except Exception:
                    lvl = None
            if lvl is None:
                self.inlvl_scale[role].state(["disabled"])
                self.inlvl_scale[role].grid_remove()          # interface inputs: no slider at all
                self.inlvl_lab[role].configure(text="gain knob" if iface else "-")
            else:
                self.inlvl_scale[role].grid()
                self.inlvl_scale[role].state(["!disabled"])
                self.v_inlvl[role].set(lvl)
                self.inlvl_lab[role].configure(text=f"{lvl:.0f}%")

    def _set_input_level(self, role):
        m = self._sel(role)
        if m is None or "disabled" in self.inlvl_scale[role].state():
            return
        pct = float(self.v_inlvl[role].get())
        if WV.set_level(m[2], pct):
            self.inlvl_lab[role].configure(text=f"{pct:.0f}%")

    def reset_peaks(self):
        for r in list(self.rig.hold):
            self.rig.hold[r] = 0.0

    def _monitor_levels(self):
        for r in ROLES:
            g = float(self.v_gain[r].get())
            self.gain_lab[r].configure(text=f"{g:+.0f} dB")
            self.rig.mon_gain[r] = 10 ** (g / 20)
        m = float(self.v_mon_master.get())
        self.mon_master_lab.configure(text=f"{m:+.0f} dB")
        self.rig.mon_master = 10 ** (m / 20)
        self.rig.mon_mute = {r for r in ROLES if self.v_mute[r].get()}
        self.rig.mon_solo = {r for r in ROLES if self.v_solo[r].get()}

    def _monitor_changed(self):
        self._monitor_levels()
        if not self.v_mon.get():
            self.rig.stop_monitor()
            return
        if not self.rig.running() and not self.start_audio():
            self.v_mon.set(False)
            return
        self._start_monitor()

    def _start_monitor(self):
        out = dev_index(self.out_dev.get())
        if out is None:
            messagebox.showinfo(APP_NAME, "Choose the headphone output on the Setup tab "
                                "('Audition output').")
            self.v_mon.set(False)
            return
        try:
            self.rig.start_monitor(out, int(self.fs.get()))
            self.mon_dev_lab.configure(text="-> " + self.out_dev.get().split(":", 1)[-1].split("  (")[0])
        except Exception as exc:
            log.exception("monitor failed")
            self.v_mon.set(False)
            messagebox.showerror(APP_NAME, f"Couldn't open the headphone output: {exc}")

    def stop_record(self):
        if not self.rig.recording:
            return
        self.stop_btn.configure(state="disabled")
        self.update_idletasks()
        data = self.rig.end()
        self.rec_started = None
        self.rec_time.configure(text="")
        self.stop_btn.configure(state="disabled")
        self.ext_btn.configure(state="disabled")
        if self.step_i < len(self.steps()):
            self.rec_btn.configure(state="normal")
        if self.rig.xruns:
            self.logln(f"note: {self.rig.xruns} audio dropout(s) during the take - affected audio "
                       "will mostly be rejected; close other apps if this keeps happening.")
        x = data.get("piezo", np.zeros(0))
        fs = self.fs.get()
        two = {r: self.rig.roles[r][0] != self.rig.roles["piezo"][0] for r in data if r != "piezo"}
        if getattr(self, "rec_is_demo", False):
            self.rec_is_demo = False
            self._demo_done(data, fs, two)
            return
        mics = {}
        for s in SLOTS:
            if s in data and len(data[s]) >= 2 * fs:
                mics[s] = (data[s], two[s])
        extras = {r: data[r] for r in ("piezo2", "I") if r in data and len(data[r]) >= 2 * fs}
        if len(x) < 2 * fs or not mics:
            self.logln(f"[{self.rec_label}] too short - ignored.")
            return
        self.last_take = {"fs": fs, "two": two, **{r: v for r, v in data.items()}}
        self._process_take(x, mics, self.rec_label, advance=getattr(self, "rec_is_step", False),
                           extras=extras, x2=extras.get("piezo2"))

    def _get_acc(self, s):
        if self.acc[s] is None:
            fs = int(self.fs.get())
            n48 = self.profile()["nfft48"]
            self.acc[s] = E.Accumulator(fs, n48 * 2 if fs > 50000 else n48)
        return self.acc[s]

    def _process_take(self, x, mics, label, advance=False, save_raw=True, extras=None, x2=None):
        """x = piezo; mics = {slot: (y, two_device)} - all from the same take.
        x2 = second piezo element (dual piezo), same device as x."""
        accs = {s: self._get_acc(s) for s in mics}
        self.busy = True
        self.status.set(f"Saving + analysing '{label}'...")
        self.last_piezo = (x.copy(), int(self.fs.get()))
        idx = None
        if save_raw:
            self._begin_autosave_session()
            idx = self.take_counter
            self.take_counter += 1
        fs = int(self.fs.get())

        def work():
            if idx is not None:
                self._save_raw_take(idx, label, x, mics, fs, extras or {})
            done = []
            for s, (y, two) in mics.items():
                try:
                    rep = accs[s].process_take(x, y, label, two_device=two, x2=x2)
                    if rep.get("accepted"):
                        done.append(s)
                    self.q.put(("take", s, rep, False))
                except Exception as exc:
                    log.exception("processing take %s mic %s", label, s)
                    self.q.put(("error", f"[{self.mic_name(s)}] [{label}] {exc}", traceback.format_exc()))
            self.q.put(("take_done", done, advance))
        threading.Thread(target=work, daemon=True).start()

    def _process_async(self, x, y, label, two_dev, advance=False):
        """Single-mic take into the slot currently shown (used by WAV import)."""
        self._process_take(x, {self.slot.get(): (y, two_dev)}, label, advance)

    def undo_take(self):
        if not self.take_stack:
            return
        slots = self.take_stack.pop()
        for s in slots:
            if self.acc[s] is not None and self.acc[s].takes:
                name = self.acc[s].undo_last()
                self.logln(f"Removed take from {self.mic_name(s)}: {name}")
        self.irm = None
        self._update_coverage()
        self.autosave()

    def logln(self, txt):
        self.log.insert("end", txt + "\n")
        self.log.see("end")
        log.info("log: %s", txt)

    def _report_take(self, s, rep, advance):
        line = (f"[{self.mic_name(s)}] {rep['name']}: kept {rep.get('accepted', 0)}/{rep.get('segments_total', 0)} "
                f"segments")
        if "delay_ms" in rep and rep.get("accepted"):
            line += f", mic delay {rep['delay_ms']:.2f} ms"
        if "drift_ppm" in rep:
            line += (f", device sync {rep['offset_ms']:+.1f} ms / {rep['drift_ppm']:+.1f} ppm"
                     + (" (session clock)" if rep.get("used_session_drift") else ""))
        if rep.get("polarity") == "inverted":
            line += ", mic polarity inverted vs piezo (handled automatically)"
        self.logln(line)
        for w in rep["warnings"]:
            self.logln("   ! " + w)
        acc = self.acc.get(s)
        if acc is not None and acc.has_dual() and s == next(k for k in SLOTS if self._has(k)):
            self.logln("   " + self._dual_summary(acc))
        self.irm = None
        self._update_coverage()

    def _dual_summary(self, acc):
        """One line: what the second piezo element adds over the summed pickup."""
        g = [b for b in acc.dual_gain() if 80 <= b["center"] <= 8000]
        if not g:
            return "Dual piezo: not enough data yet."
        us = sum(b["single"] for b in g) / len(g)
        ud = sum(b["dual"] for b in g) / len(g)
        tot = 10 * np.log10(us / max(ud, 1e-9))
        big = [b for b in g if b["gain_db"] >= 3]
        where = ""
        if big:
            lo, hi = big[0]["center"], big[-1]["center"]
            fmt = lambda f: f"{f / 1000:g} kHz" if f >= 1000 else f"{f:.0f} Hz"
            where = f", most in {fmt(lo)}-{fmt(hi)}" if lo != hi else f", most around {fmt(lo)}"
        if tot < 1.0 and not big:
            verdict = "the two elements say almost the same thing - the summed pickup is as good."
        elif tot < 3.0:
            verdict = f"a small gain ({tot:.1f} dB less unexplained sound{where})."
        else:
            verdict = f"a real gain: {tot:.1f} dB less unexplained sound than the summed pickup{where}."
        return "Dual piezo: " + verdict

    def _update_coverage(self):
        if hasattr(self, "ir_src_rb"):
            self._ir_src_state()
            self._refresh_audition_names()
        ax = self.cov_ax
        ax.clear()
        acc = self.acc[self.slot.get()]
        if acc is None or acc.empty():
            ax.text(0.5, 0.5, "No takes yet", ha="center", va="center", transform=ax.transAxes,
                    color="#888")
            ax.set_xticks([])
            ax.set_yticks([])
            self.cov_canvas.draw_idle()
            self.cov_summary.configure(text="")
            self.tips.configure(text="Follow the guided steps. Suggestions appear here after the "
                                "first take.")
            return
        cov = acc.coverage(min_band=self.profile()["min_band"])
        xs = np.arange(len(cov))
        vals = [min(c["unc_db"], 3.0) for c in cov]
        cols = [STATUS_COL[c["status"]] for c in cov]
        ax.bar(xs, vals, color=cols, width=0.8)
        ax.axhline(E.GOOD_DB, color=STATUS_COL["good"], lw=0.8, ls="--")
        ax.axhline(E.OK_DB, color=STATUS_COL["ok"], lw=0.8, ls="--")
        ax.set_ylim(0, 3.05)
        ax.set_ylabel("uncertainty, +/- dB (lower is better)", fontsize=8)
        labs = [(f"{c['center'] / 1000:g}k" if c["center"] >= 1000 else f"{c['center']:g}") for c in cov]
        ax.set_xticks(xs)
        ax.set_xticklabels(labs, rotation=90, fontsize=7)
        ax.tick_params(axis="y", labelsize=7)
        ax.set_xlabel("Hz  (green = solid, yellow = ok, orange = weak, grey = no data yet)", fontsize=8)
        self.cov_fig.tight_layout()
        self.cov_canvas.draw_idle()
        un_db, frac = acc.explained_db()
        n_good = sum(c["status"] == "good" for c in cov)
        self.cov_summary.configure(
            text=f"Takes: {len(acc.takes)}   Segments used: {acc.n_seg}   "
                 f"Bands solid: {n_good}/{len(cov)}\n"
                 f"The instrument signal explains {frac * 100:.2f}% of the mic signal "
                 f"(residual {un_db:.1f} dB). Below -20 dB is very good; worse usually means room "
                 f"noise, a moving mic, or a mic too far away.")
        tips = E.suggestions(cov, instrument=self.instrument.get())
        self.tips.configure(text="\n".join("- " + t for t in tips) if tips else
                            "All bands are solid. More takes will only refine it slightly - "
                            "go to Build & Export.")

    SPEC_COL = {"piezo": "#2e7d32", "piezo2": "#7cb342", "A": "#1565c0", "B": "#ef6c00", "I": "#8e24aa"}

    def _spec_init(self):
        ax = self.spec_ax
        ax.clear()
        self._spec_f = np.geomspace(40, 16000, 160)
        ax.set_xscale("log")
        ax.set_xlim(40, 16000)
        ax.set_ylim(-60, 6)
        ticks = [50, 100, 200, 500, 1000, 2000, 5000, 10000]
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t_ / 1000:g}k" if t_ >= 1000 else str(t_) for t_ in ticks])
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.tick_params(labelsize=6, pad=1)
        ax.grid(True, which="both", alpha=0.2)
        ax.set_title("Live spectrum (Start audio, then play)", fontsize=7, pad=2)
        self._spec_lines = {}
        for r in ROLES:
            (ln,) = ax.semilogx(self._spec_f, np.full(len(self._spec_f), np.nan), color=self.SPEC_COL[r],
                                lw=1.2, label=self._short_name(r) if hasattr(self, "inp_label") else r)
            self._spec_lines[r] = ln
        self.spec_fig.subplots_adjust(left=0.09, right=0.99, top=0.88, bottom=0.14)
        self.spec_canvas.draw_idle()

    def _spec_update(self):
        try:
            n = 8192
            fs = int(self.fs.get())
            win = np.hanning(n)
            ff = np.fft.rfftfreq(n, 1 / fs)
            ref = (ff > 200) & (ff < 5000)
            lo = self._spec_f * 2 ** (-1 / 12)
            hi = self._spec_f * 2 ** (1 / 12)
            edges_lo = np.searchsorted(ff, lo)
            edges_hi = np.maximum(np.searchsorted(ff, hi), edges_lo + 1)
            ymax = -200.0
            shown = []
            for r, ln in self._spec_lines.items():
                x = self.rig.recent(r, n)
                if x is None or not np.any(x):
                    ln.set_ydata(np.full(len(self._spec_f), np.nan))
                    continue
                P = np.abs(np.fft.rfft(x * win)) ** 2 / (np.sum(win ** 2) * n / 2)
                c = np.concatenate([[0], np.cumsum(P)])
                band = (c[edges_hi] - c[edges_lo]) / (edges_hi - edges_lo)
                if self.v_spec_match.get():
                    band = band / max(np.mean(P[ref]), 1e-20)
                prev = self._spec_avg.get(r)
                band = band if prev is None or len(prev) != len(band) else 0.6 * prev + 0.4 * band
                self._spec_avg[r] = band
                y = 10 * np.log10(np.maximum(band, 1e-14))
                ln.set_ydata(y)
                ln.set_label(self._short_name(r))
                ymax = max(ymax, float(np.max(y)))
                shown.append(ln)
            ax = self.spec_ax
            if shown:
                top = ymax + 4 if self.v_spec_match.get() else 3
                ax.set_ylim(top - 60, top)
                ax.legend(handles=shown, fontsize=6, loc="lower left", ncol=3, frameon=False)
                ax.set_title("Live spectrum" + (" - level-matched" if self.v_spec_match.get() else " (dBFS)"),
                             fontsize=7, pad=2)
            self.spec_canvas.draw_idle()
        except Exception:
            log.debug("spectrum", exc_info=True)

    def _draw_meter(self, c, lab, v, hold_lin):
        d = 20 * np.log10(v + 1e-9)
        hold = 20 * np.log10(hold_lin + 1e-9)
        w = max(c.winfo_width(), 10)
        H = 22
        c.delete("all")
        frac = np.clip((d + 60) / 60, 0, 1)
        col = "#2e9d5b" if d < -12 else ("#d6a419" if d < -3 else "#e03131")
        c.create_rectangle(0, 0, w * frac, H, fill=col, width=0)
        for m in (-48, -36, -24, -12, -6, -3):
            xm = w * (m + 60) / 60
            c.create_line(xm, H - 5, xm, H, fill="#777")
        if hold > -110:
            hot = hold > -3.0
            clip = hold >= -0.3
            mc = "#ff3b3b" if hot else "#ffffff"
            hx = w * np.clip((hold + 60) / 60, 0, 1)
            c.create_line(hx, 0, hx, H, fill=mc, width=3 if hot else 2)
            txt = "CLIP" if clip else f"{hold:.1f}"
            if hx > 46:
                c.create_text(hx - 4, H / 2, text=txt, anchor="e", fill=mc,
                              font=("Segoe UI", 9, "bold"))
            else:
                c.create_text(hx + 4, H / 2, text=txt, anchor="w", fill=mc,
                              font=("Segoe UI", 9, "bold"))
        lab.configure(text=(f"{d:6.1f} dBFS" if d > -110 else "  -inf dBFS") +
                      (f"   peak {hold:5.1f}" if hold > -110 else ""),
                      fg="#c62828" if hold > -3 else "#222")
