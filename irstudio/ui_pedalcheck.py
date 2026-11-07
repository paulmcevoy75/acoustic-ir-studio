# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""Pedal check: record what the IR pedal really puts out next to the Reference Mic, show what's
still different, and fold that into the IR as a correction."""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from . import engine as E
from .config import APP_NAME, PLOT_COL, log


class PedalCheck:
    """Part of App (mixed in) - the Pedal check window."""

    def open_pedal_check(self):
        if getattr(self, "pc_win", None) is not None and self.pc_win.winfo_exists():
            self.pc_win.lift()
            return
        w = tk.Toplevel(self)
        w.title("Pedal check - how close is the pedal to the mic?")
        h = max(480, min(660, self.winfo_screenheight() - 90))
        w.geometry(f"860x{h}+40+20")
        self.pc_win = w
        self._pc_result = None
        self._pc_pending = None
        intro = ("Records your IR pedal's real output and the Reference Mic at the same time, then shows what's "
                 "still different - including anything the pedal does itself (its sample rate, how it cuts the "
                 "IR, its level).\n\n"
                 "1. Export the IR and load it into your pedal (mix fully wet).\n"
                 "2. Guitar -> pedal -> an Interface input. On the Setup tab, turn on 'Pedal output' and choose "
                 "that input. (Optional: also keep the dry piezo on the Instrument input via a splitter / DI "
                 "thru, to compare levels.)\n"
                 "3. Put the Reference Mic where it was for the capture, press Record, and play ~30 s of "
                 "everything - strums, single notes, low and high.\n"
                 "4. Apply the correction, export again, load it into the pedal - and check again.")
        ttk.Label(w, text=intro, wraplength=820, justify="left").pack(fill="x", padx=12, pady=(10, 4))
        self.pc_target = ttk.Label(w, foreground="#1f4e9c")
        self.pc_target.pack(fill="x", padx=12)
        tf = ttk.Frame(w)
        tf.pack(fill="x", padx=12, pady=6)
        self.pc_rec_btn = ttk.Button(tf, text="● Record", style="Transport.TButton", width=11,
                                     command=self.pedal_check_record)
        self.pc_rec_btn.pack(side="left")
        ttk.Button(tf, text="■ Stop", style="Transport.TButton", width=9,
                   command=self.pedal_check_stop).pack(side="left", padx=6)
        self.pc_status = ttk.Label(tf, foreground="#555", wraplength=560, justify="left")
        self.pc_status.pack(side="left", padx=8)
        bf = ttk.Frame(w)
        bf.pack(side="bottom", fill="x", padx=12, pady=(0, 10))
        self.pc_summary = ttk.Label(w, wraplength=820, justify="left")
        self.pc_summary.pack(side="bottom", fill="x", padx=12, pady=4)
        self.pc_fig = Figure(figsize=(8, 2.4), dpi=100)
        self.pc_ax = self.pc_fig.add_subplot(111)
        self.pc_canvas = FigureCanvasTkAgg(self.pc_fig, master=w)
        self.pc_canvas.get_tk_widget().pack(fill="both", expand=True, padx=8)
        self.pc_apply = ttk.Button(bf, text="Apply this correction to the IR", style="Rec.TButton",
                                   command=self.pedal_check_apply, state="disabled")
        self.pc_apply.pack(side="left")
        self._tip(self.pc_apply, "Adds the measured difference to the IR (in the bands the take covered "
                                 "well, up to +-12 dB), rebuilds it, and keeps it in the project. Then export "
                                 "and load it into the pedal again, and re-check: each round gets closer.")
        self.pc_clear = ttk.Button(bf, text="Remove all corrections", command=self.pedal_check_clear)
        self.pc_clear.pack(side="left", padx=8)
        ttk.Button(bf, text="Close", command=w.destroy).pack(side="right")
        self._pc_refresh()
        self._pc_draw()

    # -- state
    def _pc_refresh(self):
        iid = self._target_iid()
        tgt = self.pedals[int(iid)]["name"] if iid is not None else "(no target selected)"
        corr = getattr(self, "pedal_corr", None)
        self.pc_target.configure(text=f"Pedal: {tgt}.   Corrections applied so far: "
                                      f"{getattr(self, 'pedal_corr_rounds', 0) if corr else 0}")
        problems = []
        if not self.inp["PO"]["on"].get():
            problems.append("turn on 'Pedal output' on the Setup tab")
        if not self.inp["A"]["on"].get():
            problems.append("turn on the Reference Mic")
        self.pc_status.configure(text=("To start: " + "; ".join(problems) + ".") if problems else
                                 "Ready - press Record and play.")
        self.pc_clear.state(["!disabled"] if corr else ["disabled"])

    def pedal_check_record(self):
        if self.rig.recording:
            messagebox.showinfo(APP_NAME, "Something is already recording - stop it first.", parent=self.pc_win)
            return
        if not (self.inp["PO"]["on"].get() and self.inp["A"]["on"].get()):
            self._pc_refresh()
            return
        if not self._ensure_audio():
            return
        self.audition_stop()
        self.rec_is_pcheck = True
        self._start_rec("pedal check", 90)
        self.rec_is_step = False
        self._pc_t0 = time.time()
        self._pc_tick()

    def _pc_tick(self):
        if getattr(self, "pc_win", None) is None or not self.pc_win.winfo_exists():
            return
        if self.rig.recording and getattr(self, "rec_is_pcheck", False):
            self.pc_rec_btn.configure(text=f"● REC {time.time() - self._pc_t0:.0f} s")
            self.pc_status.configure(text="Recording the pedal and the mic - play a bit of everything, "
                                          "then press Stop.", foreground="#c0392b")
            self.after(250, self._pc_tick)
        else:
            self.pc_rec_btn.configure(text="● Record")

    def pedal_check_stop(self):
        if self.rig.recording and getattr(self, "rec_is_pcheck", False):
            self.stop_record()

    def _pedal_check_done(self, data, fs, two):
        """Called by stop_record for a pedal-check take: analyse it in the background."""
        x, y = data.get("PO"), data.get("A")
        if x is None or y is None or len(x) < 8 * fs:
            self.pc_status.configure(text="Too short - record at least 10-20 s.", foreground="#c0392b")
            return
        two_dev = self.rig.roles["A"][0] != self.rig.roles["PO"][0]
        dry = data.get("piezo")
        cal = self.cal.get("A")
        self.pc_status.configure(text="Analysing...", foreground="#555")
        self._pc_pending = None

        def work():
            try:
                self._pc_pending = ("ok", E.pedal_check(x, y, fs, two_device=two_dev, cal=cal, x_dry=dry))
            except Exception as exc:
                log.info("pedal check failed", exc_info=True)
                self._pc_pending = ("error", str(exc))
        self._background(work)
        self.after(200, self._pc_poll)

    def _pc_poll(self):
        if self._pc_pending is None:
            self.after(200, self._pc_poll)
            return
        kind, val = self._pc_pending
        self._pc_pending = None
        if getattr(self, "pc_win", None) is None or not self.pc_win.winfo_exists():
            return
        if kind == "error":
            self.pc_status.configure(text=val, foreground="#c0392b")
            return
        self._pc_result = val
        self.pc_status.configure(text="Done.", foreground="#2e7d32")
        self.pc_apply.state(["!disabled"])
        self._pc_draw()
        self._pc_refresh()

    # -- results
    def _pc_draw(self):
        ax = self.pc_ax
        ax.clear()
        r = self._pc_result
        if r is None:
            ax.text(0.5, 0.5, "No check recorded yet", ha="center", va="center", color="#888",
                    transform=ax.transAxes)
            ax.set_xticks([])
            ax.set_yticks([])
            self.pc_summary.configure(text="")
        else:
            f, d, t = r["freqs"], r["diff_db"], r["trust"]
            ax.axhspan(-1.5, 1.5, color="#2e7d32", alpha=0.08)
            ax.axhline(0, color="#999", lw=0.8)
            ax.semilogx(f, np.where(t > 0.5, d, np.nan), color=PLOT_COL["A"], lw=2,
                        label="mic minus pedal (well measured)")
            ax.semilogx(f, np.where(t <= 0.5, d, np.nan), color=PLOT_COL["A"], lw=1, ls=":",
                        label="(little playing there)")
            ax.semilogx(f, r["correction_db"], color=PLOT_COL["fit"], lw=1.2, ls="--",
                        label="correction that would be applied")
            ax.set_xlim(40, f[-1])
            lim = max(6.0, float(np.nanmax(np.abs(d))) + 1)
            ax.set_ylim(-min(lim, 18), min(lim, 18))
            ticks = [50, 100, 200, 500, 1000, 2000, 5000, 10000]
            ax.set_xticks(ticks)
            ax.set_xticklabels([f"{t_ / 1000:g}k" if t_ >= 1000 else str(t_) for t_ in ticks])
            ax.set_ylabel("dB")
            ax.set_xlabel("Hz")
            ax.grid(True, which="both", alpha=0.25)
            ax.legend(fontsize=8, loc="lower left")
            above = "above 0 = the mic has more than the pedal there"
            lvl = r["level_db"]
            lvl_txt = ("" if lvl is None else
                       f"  Level: the pedal output is {abs(lvl):.1f} dB {'louder' if lvl > 0 else 'quieter'} "
                       "than the dry piezo." + (" Use 'Same loudness as dry' when exporting, or turn the pedal "
                                                "up." if lvl < -1.5 else ""))
            verdict = ("Very close - under 1.5 dB is hard to hear." if r["rms_db"] < 1.5 else
                       "Close - applying the correction should tighten it." if r["rms_db"] < 3 else
                       "Clearly different - apply the correction, re-export and check again.")
            self.pc_summary.configure(text=f"Remaining difference: {r['rms_db']:.1f} dB on average "
                                           f"(80 Hz-10 kHz, {above}). {verdict}{lvl_txt}")
        self.pc_fig.subplots_adjust(left=0.08, right=0.98, top=0.95, bottom=0.15)
        self.pc_canvas.draw_idle()

    def pedal_check_apply(self):
        r = self._pc_result
        if r is None:
            return
        old = getattr(self, "pedal_corr", None)
        add = np.asarray(r["correction_db"], float)
        f = np.asarray(r["freqs"], float)
        total = add if old is None else add + np.interp(np.log2(f), np.log2(old[0]), old[1])
        total = np.clip(total, -12, 12)
        self.pedal_corr = (tuple(float(v) for v in f), tuple(float(v) for v in total))
        self.pedal_corr_rounds = getattr(self, "pedal_corr_rounds", 0) + 1
        self.pc_apply.state(["disabled"])
        self.build()
        self.autosave()
        self._pc_refresh()
        self.pc_status.configure(text="Applied. Export the IR again, load it into the pedal, then check again.",
                                 foreground="#2e7d32")

    def pedal_check_clear(self):
        self.pedal_corr = None
        self.pedal_corr_rounds = 0
        self.build()
        self._pc_refresh()
