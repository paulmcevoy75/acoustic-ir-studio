# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Build & Export tab: IR settings, mic mix, graphs, pedal targets and export."""

from __future__ import annotations

import os
import shutil
import time

import matplotlib
import matplotlib.ticker
import numpy as np
import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from tkinter import messagebox
from tkinter import ttk

from . import engine as E
from .config import APP_NAME, ESTIMATORS, PLOT_COL, PRESET_TARGETS, SLOTS, SMOOTH_OPTS, log, save_pedals
from .texts import BUILD_TIPS
from .util import decimate_line, file_tag, finite, safe_name


class BuildTab:
    """Part of App (mixed in) - build & export tab: ir settings, mic mix, graphs, pedal targets and export."""

    def _build_build(self):
        t = self._scroll_body(self.tab_build)
        left = ttk.Frame(t)
        left.pack(side="left", fill="y", padx=10, pady=10)
        right = ttk.Frame(t)
        right.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)

        p = ttk.LabelFrame(left, text="IR settings")
        p.pack(fill="x")
        self.v_smooth = tk.StringVar(value="1/12 oct (recommended)")
        self.v_reg = tk.DoubleVar(value=70)
        self.v_lc = tk.DoubleVar(value=35)
        self.v_hc = tk.DoubleVar(value=18000)
        self.v_phase = tk.StringVar(value="minimum")
        self.v_blend = tk.DoubleVar(value=0)
        self.v_inv = tk.BooleanVar(value=False)
        self.v_name = tk.StringVar(value="MyGuitar")
        self.v_master_ms = tk.StringVar(value="1000")
        r = 0

        def row(label, w, tip=None):
            nonlocal r
            lab = ttk.Label(p, text=label + ("  (?)" if tip and label else ""))
            lab.grid(row=r, column=0, sticky="w", padx=6, pady=3)
            w.grid(row=r, column=1, sticky="we", padx=6, pady=3)
            if tip:
                self._tip(lab, tip)
                self._tip(w, tip)
                for ch in w.winfo_children():
                    self._tip(ch, tip)
            r += 1

        TIPS = BUILD_TIPS
        row("Name", ttk.Entry(p, textvariable=self.v_name, width=22), TIPS["name"])
        row("Smoothing", ttk.Combobox(p, values=list(SMOOTH_OPTS), textvariable=self.v_smooth,
                                      state="readonly", width=22), TIPS["smooth"])
        regf = ttk.Frame(p)
        ttk.Scale(regf, from_=40, to=100, variable=self.v_reg, length=140).pack(side="left")
        self.reg_lab = ttk.Label(regf, width=6)
        self.reg_lab.pack(side="left")
        self.v_reg.trace_add("write", lambda *a: self.reg_lab.configure(text=f"{self.v_reg.get():.0f} dB"))
        self.reg_lab.configure(text="70 dB")
        row("Noise protection", regf, TIPS["reg"])
        row("Low cut (Hz, 0=off)", ttk.Entry(p, textvariable=self.v_lc, width=10), TIPS["lc"])
        row("High cut (Hz, 0=off)", ttk.Entry(p, textvariable=self.v_hc, width=10), TIPS["hc"])
        self.v_est = tk.StringVar(value="H1 - recommended")
        row("Algorithm", ttk.Combobox(p, values=list(ESTIMATORS), textvariable=self.v_est,
                                      state="readonly", width=28), TIPS["est"])
        row("Phase", ttk.Combobox(p, values=["minimum", "measured"], textvariable=self.v_phase,
                                  state="readonly", width=22), TIPS["phase"])
        row("Full IR length (ms)", ttk.Combobox(p, values=["250", "500", "750", "1000", "1500", "2000"],
                                                textvariable=self.v_master_ms, width=10), TIPS["len"])
        tf = ttk.Frame(p)
        self.v_tame = tk.BooleanVar(value=False)              # off by default
        self.v_tame_db = tk.DoubleVar(value=4.0)
        self.v_tame_hz = tk.StringVar(value="3500")
        ttk.Checkbutton(tf, text="On", variable=self.v_tame, command=self._tame_changed).pack(side="left")
        ttk.Scale(tf, from_=1, to=9, variable=self.v_tame_db, length=70,
                  command=lambda e: self._tame_changed(label_only=True)).pack(side="left", padx=(6, 2))
        self.tame_lab = ttk.Label(tf, text="-4 dB", width=6)
        self.tame_lab.pack(side="left")
        ttk.Label(tf, text="at").pack(side="left")
        tc = ttk.Combobox(tf, values=["2500", "3000", "3500", "4000", "5000", "6000"], width=5,
                          textvariable=self.v_tame_hz)
        tc.pack(side="left", padx=2)
        tc.bind("<<ComboboxSelected>>", lambda e: self._tame_changed())
        tc.bind("<Return>", lambda e: self._tame_changed())
        ttk.Label(tf, text="Hz").pack(side="left")
        self._tame_job = None
        row("Finger-noise tamer", tf, TIPS["tame"])
        row("", ttk.Checkbutton(p, text="Invert polarity  (?)", variable=self.v_inv), TIPS["inv"])
        self._tip(ttk.Button(p, text="Build IR", style="Rec.TButton", command=self.build), TIPS["build"]).grid(
            row=r, column=0, columnspan=2, sticky="we", padx=6, pady=8)
        ttk.Label(left, wraplength=400, justify="left", foreground="#555", text=(
            "Hover over any setting (or its (?) label) for an explanation.")
        ).pack(fill="x", pady=4)

        ped = ttk.LabelFrame(left, text="Export targets - choose your pedal first (it sets what Audition plays)")
        ped.pack(fill="x", pady=(4, 0))
        # bottom-pinned controls first, so they stay visible on small screens
        self.v_bundle = tk.BooleanVar(value=True)
        self.v_dual_export = tk.BooleanVar(value=False)
        self.v_daisy_files = tk.BooleanVar(value=True)
        exf = ttk.Frame(ped)
        exf.pack(side="bottom", fill="x", padx=4, pady=(2, 6))
        self._tip(ttk.Button(exf, text="Export selected...", style="Rec.TButton",
                             command=self.export), TIPS["export"]).pack(side="left", fill="x", expand=True)
        self._tip(ttk.Button(exf, text="Export ALL lengths...", style="Rec.TButton",
                             command=self.export_all),
                  "Exports every target in the list in one go - one file per pedal / length, each named "
                  "with the pedal and its length, e.g. MyGuitar_Short-Smart_Box_23ms_44.1k_1024.wav."
                  ).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._tip(ttk.Checkbutton(ped, text="Also save full-length IR + notes",
                                  variable=self.v_bundle), TIPS["bundle"]).pack(side="bottom", anchor="w", padx=4)
        self.dual_ck = ttk.Checkbutton(ped, text="Dual piezo: also export the IR pair (_P1 / _P2)",
                                       variable=self.v_dual_export, state="disabled")
        self.dual_ck.pack(side="bottom", anchor="w", padx=4)
        dz = ttk.Checkbutton(ped, text="Daisy targets: also .bin + .h files (for the firmware)",
                             variable=self.v_daisy_files)
        dz.pack(side="bottom", anchor="w", padx=4)
        self._tip(dz, "Long / Ultimate (Daisy) targets get three files of the SAME IR:\n"
                      "- .wav: 32-bit float - to listen to, or load in any IR loader / DAW\n"
                      "- .bin: raw 32-bit floats - for the Daisy to load from SD card / flash\n"
                      "- .h: a C header - to build the IR straight into the firmware\n"
                      "Untick to get only the .wav.")
        self._tip(self.dual_ck, "Only when Instrument 2 was recorded. Adds one IR per piezo element, for a "
                                "2-input convolver (e.g. a Daisy Seed with both elements wired to its two "
                                "inputs): element 1 through _P1, element 2 through _P2, outputs added. The "
                                "normal file is still made for single-input pedals.")
        of = ttk.Frame(ped)
        of.pack(side="bottom", fill="x", padx=4, pady=2)
        self._tip(ttk.Button(of, text="Add custom...", command=self.add_pedal), TIPS["add"]).pack(side="left")
        self._tip(ttk.Button(of, text="Remove", command=self.remove_pedal), TIPS["rem"]).pack(side="left", padx=4)
        self.v_norm = tk.StringVar(value="Match loudness")
        self._tip(ttk.Combobox(of, values=["Match loudness", "Peak -1 dBFS"], textvariable=self.v_norm,
                               state="readonly", width=14), TIPS["norm"]).pack(side="left", padx=(8, 0))
        self.v_bits = tk.StringVar(value="24-bit")
        self._tip(ttk.Combobox(of, values=["16-bit", "24-bit", "32-bit float"], textvariable=self.v_bits,
                               state="readonly", width=10), TIPS["bits"]).pack(side="left", padx=4)
        cols = ("ms", "err")
        tf = ttk.Frame(ped)
        tf.pack(fill="x", padx=4, pady=4)
        self.ped_tree = ttk.Treeview(tf, columns=cols, show="tree headings", height=5,
                                     selectmode="extended")
        self.ped_tree.heading("#0", text="Target")
        self.ped_tree.column("#0", width=270)
        for c, h, w in (("ms", "Length", 70), ("err", "Error", 70)):
            self.ped_tree.heading(c, text=h)
            self.ped_tree.column(c, width=w, anchor="e")
        tsb = ttk.Scrollbar(tf, command=self.ped_tree.yview)
        self.ped_tree.configure(yscrollcommand=tsb.set)
        self.ped_tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="left", fill="y")
        self.ped_tree.bind("<<TreeviewSelect>>", lambda e: (self.preview_fit(), self._aud_target_changed()))
        self._tip(self.ped_tree, TIPS["targets"] + "\n\n" + "\n".join(f"- {p['name']}: {p['tip']}"
                                                                        for p in PRESET_TARGETS))
        self._fill_pedals()

        self._build_mix(right)
        self._build_audition(right)
        self.fit_info = ttk.Label(right, text="", justify="left", wraplength=700)
        self.fit_info.bind("<Configure>", lambda e: self.fit_info.configure(wraplength=max(300, e.width - 8)))
        self.fit_info.pack(side="bottom", fill="x", pady=4)
        pf = ttk.LabelFrame(right, text="Frequency response - what the IR adds to the piezo  |  Impulse response")
        pf.pack(fill="both", expand=True)
        hdr = ttk.Frame(pf)
        hdr.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Label(hdr, text="Show:").pack(side="left")
        self.v_show = {}
        for key, text in (("A", "Reference Mic"), ("B", "Color Mic"), ("dry", "Piezo dry"),
                          ("ir", "IR (full length)"), ("fit", "IR at target length")):
            self.v_show[key] = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(hdr, text=text, variable=self.v_show[key], fg=PLOT_COL.get(
                {"dry": "piezo"}.get(key, key), "#111"), selectcolor="white", font=("Segoe UI", 9, "bold"),
                command=lambda: self._plot_master(getattr(self, "_last_fit", None)) if self.irm is not None else None)
            cb.pack(side="left", padx=(4, 2))
            self._tip(cb, "Tick / untick to show or hide this line on the graph.")
        self._tip(ttk.Button(hdr, text="\u25b6 Play IR", command=self._play_ir),
                  "Plays the impulse response once (the selected target's version) through the Audition "
                  "output - a quick 'tock' you can compare between settings.").pack(side="right")
        self.fig = Figure(figsize=(7, 2.2 if self.compact else 2.8), dpi=100)
        gs = self.fig.add_gridspec(1, 2, width_ratios=[2.5, 1])
        self.ax = self.fig.add_subplot(gs[0, 0])
        self.ax2 = self.fig.add_subplot(gs[0, 1])
        self.ax2.set_box_aspect(1)
        self.canvas = FigureCanvasTkAgg(self.fig, master=pf)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self._plot_empty()

    def _fill_pedals(self):
        self.ped_tree.delete(*self.ped_tree.get_children())
        for i, pd in enumerate(self.pedals):
            ms = pd["samples"] / pd["fs"] * 1000
            self.ped_tree.insert("", "end", iid=str(i), text=pd["name"],
                                 values=(f"{ms:.0f} ms" if ms >= 10 else f"{ms:.1f} ms", ""))
        if not self.ped_tree.selection() and self.pedals:
            self.ped_tree.selection_set("0")
            self.ped_tree.focus("0")

    def add_pedal(self):
        win = tk.Toplevel(self)
        win.title("Add custom target")
        win.transient(self)
        win.resizable(False, False)
        ttk.Label(win, text="Target name:").grid(row=0, column=0, sticky="e", padx=8, pady=(10, 4))
        name_v = tk.StringVar()
        ne = ttk.Entry(win, textvariable=name_v, width=30)
        ne.grid(row=0, column=1, sticky="w", padx=8, pady=(10, 4))
        ttk.Label(win, text="Sample rate (Hz):").grid(row=1, column=0, sticky="e", padx=8, pady=4)
        fs_v = tk.StringVar(value="48000")
        ttk.Combobox(win, textvariable=fs_v, values=["44100", "48000", "96000"], width=10).grid(
            row=1, column=1, sticky="w", padx=8)
        ttk.Label(win, text="Max IR length:").grid(row=2, column=0, sticky="e", padx=8, pady=4)
        lf = ttk.Frame(win)
        lf.grid(row=2, column=1, sticky="w", padx=8)
        n_v = tk.StringVar(value="1024")
        ttk.Entry(lf, textvariable=n_v, width=10).pack(side="left")
        unit_v = tk.StringVar(value="samples")
        ttk.Combobox(lf, textvariable=unit_v, values=["samples", "ms"], width=8, state="readonly").pack(
            side="left", padx=4)
        fmt_v = tk.StringVar(value="wav")
        ttk.Radiobutton(win, text="Normal pedal (WAV)", variable=fmt_v, value="wav").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=20, pady=(6, 0))
        ttk.Radiobutton(win, text="DIY firmware, e.g. Daisy Seed (also .bin + C header)", variable=fmt_v,
                        value="daisy").grid(row=4, column=0, columnspan=2, sticky="w", padx=20)
        err = ttk.Label(win, foreground="#c0392b")
        err.grid(row=5, column=0, columnspan=2, padx=8)

        def save():
            nm = name_v.get().strip()
            try:
                fs = int(float(fs_v.get()))
                n = float(n_v.get())
                if unit_v.get() == "ms":
                    n = n * fs / 1000
                n = int(round(n))
            except ValueError:
                err.configure(text="Sample rate and length must be numbers.")
                return
            if not nm:
                err.configure(text="Give the target a name.")
                return
            if not 8000 <= fs <= 192000 or not 32 <= n <= 1_000_000:
                err.configure(text="Sample rate 8000-192000 Hz, length 32 samples or more.")
                return
            entry = {"name": nm, "fs": fs, "samples": n}
            if fmt_v.get() == "daisy":
                entry["format"] = "daisy"
            self.pedals.append(entry)
            save_pedals(self.pedals)
            self._fill_pedals()
            win.destroy()
        bf = ttk.Frame(win)
        bf.grid(row=6, column=0, columnspan=2, pady=10)
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="left", padx=4)
        ttk.Button(bf, text="Save", command=save).pack(side="left", padx=4)
        ne.focus_set()
        win.grab_set()

    def remove_pedal(self):
        sel = sorted((int(i) for i in self.ped_tree.selection()
                      if not self.pedals[int(i)].get("preset")), reverse=True)
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a target you added yourself - the four presets "
                                "can't be removed.")
            return
        for i in sel:
            self.pedals.pop(i)
        save_pedals(self.pedals)
        self._fill_pedals()

    def _tame_hz(self):
        try:
            return min(12000.0, max(500.0, float(self.v_tame_hz.get())))
        except (ValueError, tk.TclError):
            return 3500.0

    def _tame_changed(self, label_only=False):
        """Finger-noise tamer changed: update the label, and rebuild once the slider settles."""
        self.tame_lab.configure(text=f"-{self.v_tame_db.get():.0f} dB")
        if label_only and not self.v_tame.get():
            return
        if self.irm is not None and self.master_src == "built" and not self.busy:
            if self._tame_job is not None:
                self.after_cancel(self._tame_job)
            self._tame_job = self.after(500, self._tame_rebuild)

    def _tame_rebuild(self):
        self._tame_job = None
        if not self.busy and not self.rig.recording:
            self.build()

    def params(self):
        return E.BuildParams(smoothing=SMOOTH_OPTS[self.v_smooth.get()],
                             reg_db=float(self.v_reg.get()),
                             low_cut=float(self.v_lc.get() or 0),
                             high_cut=float(self.v_hc.get() or 0),
                             phase=self.v_phase.get(),
                             blend_b={"A": 0.0, "B": 1.0}.get(self.v_ir_src.get(),
                                                              float(self.v_mix["mid"].get()) / 100),
                             mix_bands=self._mix_bands() if self.v_ir_src.get() == "mix" else None,
                             invert=bool(self.v_inv.get()),
                             tame_db=float(self.v_tame_db.get()) if self.v_tame.get() else 0.0,
                             tame_hz=self._tame_hz(),
                             estimator=ESTIMATORS.get(self.v_est.get(), "H1"),
                             master_ms=float(self.v_master_ms.get() or 500))

    def build(self):
        if self.busy:
            messagebox.showinfo(APP_NAME, "Still analysing the last take - give it a moment, "
                                "then press Build IR again.")
            return
        a, b = self.acc["A"], self.acc["B"]
        p = self.params()
        has_a = a is not None and not a.empty()
        has_b = b is not None and not b.empty()
        if not has_a and not has_b:
            if self.irm is not None and self.master_src not in (None, "built"):
                self.fits = {}
                self._plot_master()
                return
            messagebox.showinfo(APP_NAME, "Record some takes on the Capture tab first "
                                "(or File > Open an IR WAV).")
            return
        acc0 = a if has_a else b
        max_ms = int(0.75 * acc0.nfft / acc0.fs * 1000)
        clamp_note = ""
        if p.master_ms > max_ms:
            clamp_note = (f" Full length limited to {max_ms} ms by this session's analysis size"
                          " (choose 'double bass' before capturing for up to 2 s).")
            p.master_ms = max_ms
        if not has_b:
            p.blend_b, p.mix_bands = 0.0, None
        if not has_a:
            p.blend_b, p.mix_bands = 1.0, None
        try:
            log.info("build: A=%s B=%s params=%s", a.n_seg if has_a else 0, b.n_seg if has_b else 0, p)
            self.irm = E.build_master(a, b, p, self.cal["A"], self.cal["B"])
        except Exception as exc:
            log.exception("build failed")
            messagebox.showerror(APP_NAME, f"Couldn't build the IR: {exc}\n\nYour takes are safe "
                                 "(autosaved). Details are in the log (Help > Open logs folder).")
            return
        for w in self.irm.get("warnings", []):
            self.logln("BUILD: " + w)
        self.master_src = "built"
        self.fits = {}
        self._aud_cache = {}
        self.dual_pair = None
        if any(self.acc[k] is not None and self.acc[k].has_dual() for k in SLOTS):
            try:
                self.dual_pair = E.build_dual(a if has_a else None, b if has_b else None, p, self.irm,
                                              self.cal["A"], self.cal["B"])
            except Exception as exc:
                log.exception("dual build failed")
                self.logln(f"BUILD: dual-piezo IR pair couldn't be made: {exc}")
        # recorded with two piezo elements -> export the pair too; single piezo -> off
        self.v_dual_export.set(self.dual_pair is not None)
        self.dual_ck.state(["!disabled"] if self.dual_pair is not None else ["disabled"])
        self._ir_src_state()
        self._aud_target_changed()
        for iid in self.ped_tree.get_children():
            self.ped_tree.set(iid, "err", "")
        self._plot_master()
        msg = (f"Built: {len(self.irm['h'])} samples @ {self.irm['fs']} Hz, "
               f"{self.irm['phase']} phase")
        if p.phase == "measured" and self.irm["phase"] == "minimum":
            msg += " (blending two mics always uses minimum phase, to avoid comb filtering)"
        if self.irm["removed_predelay_ms"] > 0:
            msg += f", removed {self.irm['removed_predelay_ms']:.2f} ms mic-distance delay"
        if self.irm.get("warnings"):
            msg += ". Note: " + " ".join(self.irm["warnings"])
        if self.dual_pair is not None:
            acc_d = self.acc[self.dual_pair["mic"]]
            msg += (f". Also built a dual-piezo IR PAIR (one IR per element, from the "
                    f"{self._short_name(self.dual_pair['mic'])}) - {self._dual_summary(acc_d)[12:].rstrip('.')}")
        self.fit_info.configure(text=msg + "." + clamp_note + " Select a target to see how it fits.")
        if self.ped_tree.selection():
            self.preview_fit()

    def _plot_empty(self):
        for ax in (self.ax, self.ax2):
            ax.clear()
        self.ax.text(0.5, 0.5, "Build the IR to see its response", ha="center", va="center",
                     transform=self.ax.transAxes, color="#888")
        self.ax.set_axis_off()
        self.ax2.set_visible(False)
        self.canvas.draw_idle()

    def _plot_master(self, fit=None):
        """Draw the response graphs. A drawing problem must never take the app down,
        so any failure is logged and replaced by a simple message on the graph."""
        self._last_fit = fit
        try:
            self._plot_master_inner(fit)
            self.canvas.draw()                    # draw now, so errors are caught here
        except Exception:
            log.exception("plot failed (fit=%s)", None if fit is None else fit.get("N"))
            try:
                for ax in (self.ax, self.ax2):
                    ax.clear()
                self.ax.text(0.5, 0.5, "Graph couldn't be drawn (details in the log) - the IR "
                             "itself is fine and can be exported.", ha="center", va="center",
                             transform=self.ax.transAxes, color="#a33", wrap=True)
                self.ax2.set_visible(False)
                self.canvas.draw()
            except Exception:
                log.exception("fallback plot failed")

    def _plot_master_inner(self, fit=None):
        m = self.irm
        ax, ax2 = self.ax, self.ax2
        ax.clear()
        ax2.clear()
        f = m["freqs"]
        sel = (f >= 20) & (f <= min(20000, m["fs"] / 2))
        p = self.params()
        ref = (f > 200) & (f < 5000)
        src = self.v_ir_src.get() if hasattr(self, "v_ir_src") else "mix"
        show = lambda k: not hasattr(self, "v_show") or bool(self.v_show[k].get())
        ax.set_xscale("log")
        for s_, col in (("A", PLOT_COL["A"]), ("B", PLOT_COL["B"])):
            acc = self.acc[s_]
            if acc is None or acc.empty() or not show(s_):
                continue
            Ps, _ = E._power_response(acc, p, self.cal[s_])
            Ps = E.smooth_frac(Ps, f, 1 / 12)
            ax.semilogx(f[sel], finite(E.db(Ps[sel] / max(np.mean(Ps[ref]), 1e-30)), -200), color=col,
                        lw=1.1, alpha=0.9, label=f"{self._short_name(s_)} correction")
        acc0 = next((self.acc[s_] for s_ in SLOTS if self.acc[s_] is not None and not self.acc[s_].empty()), None)
        if acc0 is not None and self.master_src in (None, "built"):
            Px = np.interp(f, acc0.freqs, acc0.Sxx)
            Px = E.smooth_frac(Px, f, 1 / 12)
            dry = finite(E.db(Px[sel] / max(np.mean(Px[ref]), 1e-30)), -200)
            if show("dry"):
                ax.semilogx(f[sel], dry,
                        color=PLOT_COL["piezo"], lw=1.1, ls=(0, (4, 2)), label="Piezo dry (its own tone)")
        Pm = E.smooth_frac(m["P"], f, 1 / 12)
        curve = finite(E.db(Pm[sel] / np.mean(Pm[ref])), -200)
        name = ({"mix": "Blended", "A": self._short_name("A"), "B": self._short_name("B")}[src]
                if self.master_src in (None, "built") else "Loaded")
        if show("ir"):
            ax.semilogx(f[sel], curve, color=PLOT_COL["ir"], lw=2.0, label=f"IR: {name} (full length)")
        if fit is not None and show("fit"):
            ff = fit["freqs"]
            sl = (ff >= 20) & (ff <= min(20000, fit["fs"] / 2))
            R = E.smooth_frac(fit["R"], ff, 1 / 12)
            T = fit["T"]
            refp = (ff > 200) & (ff < 5000)
            off = np.mean(E.db(T[refp])) - np.mean(E.db(R[refp]))
            ms = fit["N"] / fit["fs"] * 1000
            ax.semilogx(ff[sl], finite(E.db(R[sl] / max(np.mean(T[refp]), 1e-30)) + off, -200),
                        color=PLOT_COL["fit"], lw=1.4, ls="--", label=f"IR cut to {ms:.0f} ms (selected target)")
        ax.set_axis_on()
        ax.set_xlim(20, 20000)
        ticks = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t_ / 1000:g}k" if t_ >= 1000 else str(t_) for t_ in ticks])
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        band = (f[sel] > 40) & (f[sel] < 16000)
        top = float(np.max(curve[band]))
        if acc0 is not None and self.master_src in (None, "built"):
            top = max(top, min(top + 12, float(np.max(dry[band]))))
        if not np.isfinite(top):
            top = 0.0
        ax.set_ylim(top - 48, top + 6)
        ax.grid(True, which="both", alpha=0.25)
        ax.set_ylabel("dB")
        ax.set_xlabel("Hz", labelpad=0)
        # (no legend: the coloured 'Show' checkboxes above the graph name each line)
        ax2.set_visible(True)
        h = fit["h"] if fit is not None else m["h"]
        fs = fit["fs"] if fit is not None else m["fs"]
        n = min(len(h), int(0.1 * fs)) if fit is None else len(h)
        tms = np.arange(n) / fs * 1000
        dp = getattr(self, "dual_pair", None)
        if dp is not None and self.master_src in (None, "built"):
            nd = min(len(dp["h1"]), int(0.1 * dp["fs"]))
            td = np.arange(nd) / dp["fs"] * 1000
            ax2.plot(*decimate_line(td, dp["h1"][:nd]), color=PLOT_COL["A"], lw=0.6, alpha=0.8, label="pair: P1")
            ax2.plot(*decimate_line(td, dp["h2"][:nd]), color=PLOT_COL["piezo"], lw=0.6, alpha=0.8, label="P2")
        ax2.plot(*decimate_line(tms, finite(h[:n])), color=PLOT_COL["fit"] if fit is not None else PLOT_COL["ir"],
                 lw=0.7, label="IR")
        if dp is not None and self.master_src in (None, "built"):
            ax2.legend(fontsize=6, loc="upper right", frameon=False)
        ax2.set_xlabel("ms", labelpad=0)
        ax2.set_yticks([])
        ax2.tick_params(labelsize=7)
        ax2.set_title("IR" + (" (first 100 ms)" if fit is None else " at target length"), fontsize=9)
        ax2.grid(True, alpha=0.25)
        # fixed margins (not tight_layout): the graphs stay the same size whatever is plotted
        self.fig.subplots_adjust(left=0.065, right=0.99, top=0.92, bottom=0.15, wspace=0.14)

    def _fit(self, iid):
        if iid in self.fits:
            return self.fits[iid]
        pd = self.pedals[int(iid)]
        ref = E.loudness_ref_at(self.irm, pd["fs"]) if self.v_norm.get() == "Match loudness" else None
        r = E.fit_for_pedal(self.irm, pd["fs"], pd["samples"],
                            normalize="loudness" if ref else "peak", ref_level=ref,
                            fmin=self.profile()["fmin"])
        self.fits[iid] = r
        self.ped_tree.set(iid, "err", f"{r['err_vs_full_db']:.2f} dB")
        return r

    def preview_fit(self):
        if self.irm is None or getattr(self, "_suppress_preview", False) or self.busy:
            return
        sel = self.ped_tree.selection()
        if not sel:
            return
        try:
            log.info("preview fit: %s", self.pedals[int(sel[0])])
            r = self._fit(sel[0])
        except Exception as exc:
            log.exception("fit failed")
            messagebox.showerror(APP_NAME, f"Couldn't fit that target: {exc}")
            return
        pd = self.pedals[int(sel[0])]
        self._plot_master(r)
        log.info("preview fit done")
        self.fit_info.configure(text=(
            f"{pd['name']}: {r['N'] / r['fs'] * 1000:.1f} ms ({r['N']} samples at {r['fs'] / 1000:g} kHz, "
            f"the rate this device runs at)"
            f"{' (whole IR - fits within the ' + str(r['N_max']) + '-sample limit)' if r['N'] < r['N_max'] else ''}, "
            f"frequency resolution ~{r['freq_res_hz']:.{0 if r['freq_res_hz'] >= 10 else 2}f} Hz.  "
            f"Difference from the full IR: {r['err_vs_full_db']:.2f} dB RMS "
            f"({self.profile()['fmin']:.0f} Hz-12 kHz).  Method: {'optimised redesign' if r['method'] == 'fit' else 'trimmed'}"
            f", tail fade {int(r['fade'] * 100)}%.  "
            f"{'Under 1 dB is hard to hear; ' if r['err_vs_full_db'] < 1 else ''}"
            f"{'short IRs mainly lose low-end resonance detail.' if r['N'] / r['fs'] < 0.03 else ''}"
            f"{'  Dual-piezo IR pair ready: try Audition > Piezo 1 + 2.' if self.dual_pair is not None else ''}"))

    def export_all(self):
        self.export(all_targets=True)

    def export(self, all_targets=False):
        if self.irm is None:
            self.build()
            if self.irm is None:
                return
        sel = self.ped_tree.get_children() if all_targets else self.ped_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select one or more export targets in the list "
                                "(Ctrl/Shift-click to pick several).")
            return
        if not getattr(self, "project_path", None):
            if not messagebox.askokcancel(APP_NAME, "Save the project first - its IRs are exported next to "
                                          "it, into a folder called '<Project Name> IRs'."):
                return
            if not self.save_session(title="Save project (IRs export next to it)"):
                return
        else:
            try:
                self._write_project(self.project_path)          # keep the project in step with the IRs
            except Exception:
                log.exception("re-saving project before export failed")
        folder = self._project_ir_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't create '{folder}': {exc}")
            return
        bits = {"16-bit": 16, "24-bit": 24, "32-bit float": 32}[self.v_bits.get()]
        name = safe_name(self.v_name.get()) or safe_name(self._project_name()) or "IR"
        self.fits = {}
        done, notes = [], []
        t_start = time.time() - 1
        try:
            for iid in sel:
                pd = self.pedals[int(iid)]
                r = self._fit(iid)
                tag = file_tag(pd["name"])
                k = f"{pd['fs'] / 1000:g}k"
                ms = r["N"] / pd["fs"] * 1000
                base = f"{name}_{tag}_{ms:.0f}ms_{k}_{r['N']}"
                if pd.get("format") == "daisy" and self.v_daisy_files.get():
                    E.write_wav(os.path.join(folder, base + ".wav"), r["h"], pd["fs"], 32)
                    E.write_bin(os.path.join(folder, base + ".bin"), r["h"])
                    ident = E.write_c_header(os.path.join(folder, base + ".h"), r["h"], pd["fs"],
                                             f"{name}_{r['N']}")
                    files = f"{base}.wav / .bin / .h (array '{ident}')"
                else:
                    E.write_wav(os.path.join(folder, base + ".wav"), r["h"], pd["fs"], bits)
                    files = base + ".wav"
                if self.dual_pair is not None and self.v_dual_export.get():
                    fp = E.fit_pair(self.dual_pair, pd["fs"], pd["samples"])
                    for k, tag2 in (("h1", "P1"), ("h2", "P2")):
                        b2 = f"{base}_{tag2}"
                        if pd.get("format") == "daisy" and self.v_daisy_files.get():
                            E.write_wav(os.path.join(folder, b2 + ".wav"), fp[k], pd["fs"], 32)
                            E.write_bin(os.path.join(folder, b2 + ".bin"), fp[k])
                            E.write_c_header(os.path.join(folder, b2 + ".h"), fp[k], pd["fs"],
                                             f"{name}_{tag2}_{fp['N']}")
                        else:
                            E.write_wav(os.path.join(folder, b2 + ".wav"), fp[k], pd["fs"], bits)
                    files += f"  + dual pair {base}_P1 / _P2"
                done.append(f"{files}   ({r['err_vs_full_db']:.2f} dB from full)")
                notes.append(f"{pd['name']:<40} {pd['fs']:>6} Hz  {r['N']:>6} samples "
                             f"({r['N'] / pd['fs'] * 1000:7.1f} ms)  "
                             f"{r['err_vs_full_db']:5.2f} dB from full  -> {files}")
            if self.v_bundle.get():
                m = self.irm
                mfn = f"{name}_FULL_{m['fs'] / 1000:g}k_{len(m['h'])}_32f.wav"
                E.write_wav(os.path.join(folder, mfn), m["h"], m["fs"], 32)
                if self.dual_pair is not None and self.v_dual_export.get():
                    dp = self.dual_pair
                    for k, tag2 in (("h1", "P1"), ("h2", "P2")):
                        E.write_wav(os.path.join(folder, f"{name}_FULL_{tag2}_{dp['fs'] / 1000:g}k_"
                                                         f"{len(dp[k])}_32f.wav"), dp[k], dp["fs"], 32)
                # (the project itself is saved right next to this folder)
                p = self.params()
                with open(os.path.join(folder, name + "_info.txt"), "w") as fh:
                    fh.write(f"{name} - exported {time.strftime('%Y-%m-%d %H:%M')} by {APP_NAME}\n\n")
                    fh.write(f"Instrument: {self.instrument.get()}\n")
                    fh.write(f"Full IR: {mfn}  ({len(m['h']) / m['fs'] * 1000:.0f} ms, {m['phase']} phase)\n")
                    fh.write(f"Settings: smoothing {self.v_smooth.get()}, noise protection {p.reg_db:.0f} dB, "
                             f"low cut {p.low_cut:g} Hz, high cut {p.high_cut:g} Hz, "
                             f"blend A/B {100 - p.blend_b * 100:.0f}/{p.blend_b * 100:.0f}, "
                             f"invert {p.invert}, normalise {self.v_norm.get()}, finger-noise tamer "
                             f"{f'-{p.tame_db:.0f} dB at {p.tame_hz:.0f} Hz' if p.tame_db > 0 else 'off'}\n")
                    if self.master_src not in (None, "built"):
                        fh.write(f"Source: re-fitted from {self.master_src}\n")
                    if self.dual_pair is not None and self.v_dual_export.get():
                        fh.write("Dual piezo: files ending _P1 / _P2 are an IR PAIR - run element 1 through _P1 "
                                 "and element 2 through _P2, then add the two outputs. The plain files are "
                                 "for a single input (both elements summed).\n")
                    fh.write("\nTargets:\n" + "\n".join(notes) + "\n\n")
                    fh.write(f"Open {os.path.basename(self.project_path)} (next to this folder) in {APP_NAME} "
                             "to change settings, add takes, "
                             "or export for more pedals.\n")
                done += ["", f"Full-length master: {mfn}", f"Notes: {name}_info.txt",
                         f"Project (re-open to edit): {self.project_path}"]
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Export failed: {exc}")
            return
        if self.backup_dir:
            try:
                if not os.path.isdir(self.backup_dir):
                    raise OSError(f"the backup folder '{self.backup_dir}' isn't available")
                dst = os.path.join(self.backup_dir, os.path.basename(folder))
                if self.project_path and os.path.isfile(self.project_path):
                    shutil.copy2(self.project_path, os.path.join(self.backup_dir,
                                                                 os.path.basename(self.project_path)))
                os.makedirs(dst, exist_ok=True)
                n = 0
                for fn in os.listdir(folder):
                    src = os.path.join(folder, fn)
                    if os.path.isfile(src) and os.path.getmtime(src) >= t_start:
                        shutil.copy2(src, os.path.join(dst, fn))
                        n += 1
                done += ["", f"Also copied {n} file(s) to the backup folder: {dst}"]
            except Exception as exc:
                log.exception("backup copy failed")
                done += ["", f"Couldn't copy to the backup folder: {exc}"]
        messagebox.showinfo(APP_NAME, f"Exported to {folder}:\n\n" + "\n".join(done))

    def _build_mix(self, parent):
        mf = ttk.LabelFrame(parent, text="Which IR to make")
        mf.pack(side="top", fill="x", pady=(0, 6))
        self.v_ir_src = tk.StringVar(value="mix")
        sf_ = ttk.Frame(mf)
        sf_.grid(row=0, column=0, columnspan=10, sticky="we", padx=6, pady=(2, 2))
        self.ir_src_rb = {}
        for key, text, tip in (
                ("mix", "Blended mics (set below)", "The IR comes from both mics, mixed by frequency "
                 "with the Lows / Mids / Highs sliders below."),
                ("A", "Reference Mic only", "The IR comes only from the Reference Mic - the most "
                 "accurate, natural sound."),
                ("B", "Color Mic only", "The IR comes only from the Color Mic - its character, "
                 "nothing else.")):
            rb = ttk.Radiobutton(sf_, text=text, value=key, variable=self.v_ir_src,
                                 command=self._ir_src_changed)
            rb.pack(side="left", padx=(0, 14))
            self._tip(rb, tip + " This is the IR that's shown, exported and auditioned as 'Piezo + "
                      "chosen IR'.")
            self.ir_src_rb[key] = rb
        self.v_mix = {"low": tk.DoubleVar(value=0), "mid": tk.DoubleVar(value=0), "high": tk.DoubleVar(value=0),
                      "x1": tk.DoubleVar(value=250), "x2": tk.DoubleVar(value=3000)}
        self.v_mix_link = tk.BooleanVar(value=True)
        self.mix_title = ttk.Label(mf, foreground="#555")
        self.mix_title.grid(row=1, column=0, columnspan=9, sticky="w", padx=6)
        self.mix_hint = ttk.Label(mf, foreground="#b45309")
        self.mix_hint.grid(row=4, column=0, columnspan=9, sticky="w", padx=6)
        self.mix_lab, self.mix_scales = {}, []
        for c, (k, name) in enumerate((("low", "Lows"), ("mid", "Mids"), ("high", "Highs"))):
            ttk.Label(mf, text=name).grid(row=2, column=c * 3, sticky="e", padx=(8, 2))
            sc = ttk.Scale(mf, from_=0, to=100, variable=self.v_mix[k], length=95,
                           command=lambda e, k=k: self._mix_changed(k))
            sc.grid(row=2, column=c * 3 + 1)
            self.mix_scales.append(sc)
            self.mix_lab[k] = ttk.Label(mf, width=4, text="0")
            self.mix_lab[k].grid(row=2, column=c * 3 + 2, sticky="w")
            self._tip(sc, f"{name}: 0 = use only the Reference Mic's IR in this range, 100 = only the Color "
                          "Mic's, in between = a mix. E.g. Lows 0 + Highs 70 = accurate bass from the "
                          "Reference Mic with the Color Mic's sparkle on top.")
        xf = ttk.Frame(mf)
        xf.grid(row=3, column=0, columnspan=9, sticky="w", padx=6, pady=(2, 4))
        ck = ttk.Checkbutton(xf, text="Link (one blend for everything)", variable=self.v_mix_link,
                             command=lambda: self._mix_changed("mid"))
        ck.pack(side="left")
        self.mix_scales.append(ck)
        self._tip(ck, "On: the three sliders move together - a simple overall blend. Off: set Lows, Mids "
                      "and Highs separately.")
        ttk.Label(xf, text="   Lows/Mids split at").pack(side="left")
        e1 = ttk.Entry(xf, textvariable=self.v_mix["x1"], width=6)
        e1.pack(side="left", padx=2)
        ttk.Label(xf, text="Hz    Mids/Highs split at").pack(side="left")
        e2 = ttk.Entry(xf, textvariable=self.v_mix["x2"], width=6)
        e2.pack(side="left", padx=2)
        ttk.Label(xf, text="Hz").pack(side="left")
        self.mix_scales += [e1, e2]
        self._tip(e1, "Where 'Lows' end and 'Mids' begin. Body boom lives around 80-250 Hz. "
                      "Press Build IR to apply.")
        self._tip(e2, "Where 'Mids' end and 'Highs' begin. Pick attack and 'air' live above ~3 kHz. "
                      "The IR rebuilds by itself a moment after you change the mix.")
        self.mix_fig = Figure(figsize=(3.0, 0.9), dpi=100)
        self.mix_ax = self.mix_fig.add_subplot(111)
        self.mix_canvas = FigureCanvasTkAgg(self.mix_fig, master=mf)
        if not self.compact:                  # small screens: the Blend graph window shows it instead
            self.mix_canvas.get_tk_widget().grid(row=1, column=9, rowspan=3, sticky="nsew", padx=(4, 6), pady=2)
        self._tip(self.mix_canvas.get_tk_widget(), "How much of the Color Mic is used at each frequency "
                  "(0 = all Reference Mic, 100 = all Color Mic).")
        mf.columnconfigure(9, weight=1)
        bw = ttk.Button(sf_, text="Blend graph \u2197", command=self.open_blend_window)
        bw.pack(side="right", padx=(6, 0))
        self._tip(bw, "Opens a bigger window showing the Reference Mic, Color Mic and blended responses - "
                      "it follows the sliders live while you blend.")
        self.blend_win = None
        self._blend_parts = None
        self._mix_job = None
        for k in ("x1", "x2"):
            self.v_mix[k].trace_add("write", lambda *a: self._mix_changed(None))
        self._ir_src_state()
        self._mix_changed(None)

    def _has(self, s):
        return self.acc.get(s) is not None and not self.acc[s].empty()

    def _ir_src_state(self):
        """Enable only the IR choices the session has data for."""
        if not hasattr(self, "ir_src_rb"):
            return
        has_a, has_b = self._has("A"), self._has("B")
        any_data = has_a or has_b
        self.ir_src_rb["A"].state(["!disabled"] if has_a or not any_data else ["disabled"])
        self.ir_src_rb["B"].state(["!disabled"] if has_b else ["disabled"])
        self.ir_src_rb["mix"].state(["!disabled"] if (has_a and has_b) or not any_data else ["disabled"])
        # the user's choice is kept (self._ir_src_pref) and only stood in for while the data
        # for it isn't there - e.g. for a moment while a take's second mic is still analysing
        want = getattr(self, "_ir_src_pref", "mix")
        ok = {"mix": has_a and has_b, "A": has_a, "B": has_b}
        if not any_data or ok[want]:
            pick = want
        else:
            pick = "mix" if ok["mix"] else ("A" if has_a else "B")
        if self.v_ir_src.get() != pick:
            self.v_ir_src.set(pick)
        on = pick == "mix"
        for w in self.mix_scales:
            w.state(["!disabled"] if on else ["disabled"])
        if hasattr(self, "mix_hint"):
            self.mix_hint.configure(text="" if on else (
                "These sliders are for 'Blended mics' - choose it above to use them." if ok["mix"] else
                "Blending needs takes from both the Reference Mic and the Color Mic."))

    def _ir_src_changed(self):
        self._ir_src_pref = self.v_ir_src.get()           # a deliberate choice: remember it
        self._ir_src_state()
        if self.irm is not None and self.master_src == "built":
            self.build()

    def _mix_changed(self, k):
        if k is not None and self.v_mix_link.get():
            v = self.v_mix[k].get()
            for kk in ("low", "mid", "high"):
                if kk != k:
                    self.v_mix[kk].set(v)
        for kk in ("low", "mid", "high"):
            self.mix_lab[kk].configure(text=f"{self.v_mix[kk].get():.0f}")
        self._draw_mix_curve()
        self._update_blend_window()
        # rebuild once the slider has settled (not on every pixel of a drag)
        if (self.irm is not None and self.master_src == "built" and self.v_ir_src.get() == "mix"
                and self._has("A") and self._has("B") and not getattr(self, "_suppress_preview", False)):
            if self._mix_job is not None:
                self.after_cancel(self._mix_job)
            self._mix_job = self.after(600, self._mix_rebuild)

    def open_blend_window(self):
        if self.blend_win is not None and self.blend_win.winfo_exists():
            self.blend_win.lift()
            return
        if not (self._has("A") and self._has("B")):
            messagebox.showinfo(APP_NAME, "Blending needs takes from both the Reference Mic and the Color "
                                "Mic.")
            return
        w = tk.Toplevel(self)
        w.title("Blend - Reference Mic vs Color Mic")
        w.geometry("820x460")
        self.blend_win = w
        self.blend_fig = Figure(figsize=(8, 4.2), dpi=100)
        self.blend_ax = self.blend_fig.add_subplot(111)
        self.blend_ax2 = self.blend_ax.twinx()
        self.blend_canvas = FigureCanvasTkAgg(self.blend_fig, master=w)
        self.blend_canvas.get_tk_widget().pack(fill="both", expand=True)
        ttk.Label(w, foreground="#555", text="Moves live with the Lows / Mids / Highs sliders. Shaded area = "
                  "how much Color Mic is used at each frequency.").pack(pady=(0, 6))
        w.protocol("WM_DELETE_WINDOW", lambda: (w.destroy(), setattr(self, "blend_win", None)))
        self._update_blend_window()

    def _blend_parts_now(self):
        """Level-matched responses of each mic (cached; depends on the IR settings, not the mix)."""
        p = self.params()
        key = repr((p.smoothing, p.reg_db, p.estimator, p.low_cut, p.high_cut,
                    self.acc["A"].n_seg, self.acc["B"].n_seg))
        if self._blend_parts is None or self._blend_parts[0] != key:
            f = self.acc["A"].freqs
            ref = (f > 200) & (f < 5000)
            parts = []
            for s_ in SLOTS:
                P, _ = E._power_response(self.acc[s_], p, self.cal[s_])
                P = E.smooth_frac(P, f, 1 / 12)
                if p.low_cut > 0:
                    P = P * E.butter_mag2(f, p.low_cut, 2, "hp")
                parts.append(P / max(np.mean(P[ref]), 1e-30))
            self._blend_parts = (key, f, parts[0], parts[1])
        return self._blend_parts[1:]

    def _update_blend_window(self):
        if getattr(self, "blend_win", None) is None or not self.blend_win.winfo_exists():
            return
        try:
            f, PA, PB = self._blend_parts_now()
            low, mid, high, x1, x2 = self._mix_bands()
            wB = E.band_weights(f, low, mid, high, x1, x2)
            P = (1 - wB) * PA + wB * PB
            sel = (f >= 20) & (f <= 20000)
            ax, ax2 = self.blend_ax, self.blend_ax2
            ax.clear()
            ax2.clear()
            ax2.fill_between(f[sel], 0, wB[sel] * 100, color=PLOT_COL["B"], alpha=0.12, lw=0)
            ax2.set_ylim(0, 100)
            ax2.yaxis.tick_right()
            ax2.yaxis.set_label_position("right")
            ax2.set_ylabel("Color Mic share %", fontsize=8, color=PLOT_COL["B"])
            ax2.tick_params(labelsize=7)
            ax.semilogx(f[sel], E.db(PA[sel]), color=PLOT_COL["A"], lw=1.1, label=self._short_name("A"))
            ax.semilogx(f[sel], E.db(PB[sel]), color=PLOT_COL["B"], lw=1.1, label=self._short_name("B"))
            ax.semilogx(f[sel], E.db(P[sel]), color=PLOT_COL["ir"], lw=2.2, label="Blended")
            ax.set_zorder(ax2.get_zorder() + 1)
            ax.patch.set_visible(False)
            ticks = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
            ax.set_xticks(ticks)
            ax.set_xticklabels([f"{t_ / 1000:g}k" if t_ >= 1000 else str(t_) for t_ in ticks])
            ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
            ax.set_xlim(20, 20000)
            top = float(np.max(E.db(P[(f > 40) & (f < 16000)])))
            ax.set_ylim(top - 45, top + 5)
            ax.grid(True, which="both", alpha=0.25)
            ax.set_ylabel("dB")
            ax.legend(fontsize=8, loc="lower left")
            ax.set_title(f"Lows {low * 100:.0f}  |  Mids {mid * 100:.0f}  |  Highs {high * 100:.0f}   "
                         f"(splits {x1:.0f} Hz / {x2:.0f} Hz)", fontsize=9)
            self.blend_fig.subplots_adjust(left=0.08, right=0.92, top=0.92, bottom=0.1)
            self.blend_canvas.draw_idle()
        except Exception:
            log.debug("blend window", exc_info=True)

    def _mix_rebuild(self):
        self._mix_job = None
        if self.busy or self.rig.recording:
            return
        self.build()

    def _draw_mix_curve(self):
        if not hasattr(self, "mix_ax"):
            return
        try:
            f = np.geomspace(20, 20000, 400)
            low, mid, high, x1, x2 = self._mix_bands()
            w = E.band_weights(f, low, mid, high, x1, x2)
            ax = self.mix_ax
            ax.clear()
            ax.fill_between(f, 0, w * 100, color=PLOT_COL["B"], alpha=0.25, lw=0)
            ax.semilogx(f, w * 100, color=PLOT_COL["B"], lw=1.5)
            ax.set_xlim(20, 20000)
            ax.set_ylim(-5, 105)
            ax.set_xticks([100, 1000, 10000])
            ax.set_xticklabels(["100", "1k", "10k"])
            ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
            ax.set_yticks([0, 50, 100])
            ax.tick_params(labelsize=6, pad=1)
            ax.set_title("Color Mic share %", fontsize=7, pad=2)
            ax.grid(True, alpha=0.2)
            self.mix_fig.subplots_adjust(left=0.13, right=0.98, top=0.8, bottom=0.22)
            self.mix_canvas.draw_idle()
        except Exception:
            log.debug("mix curve", exc_info=True)

    def _mix_bands(self):
        try:
            x1 = max(20.0, float(self.v_mix["x1"].get()))
            x2 = max(x1 * 1.2, float(self.v_mix["x2"].get()))
        except (tk.TclError, ValueError):
            x1, x2 = 250.0, 3000.0
        return (self.v_mix["low"].get() / 100, self.v_mix["mid"].get() / 100,
                self.v_mix["high"].get() / 100, x1, x2)
