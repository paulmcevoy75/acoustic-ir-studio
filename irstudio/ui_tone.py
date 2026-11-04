# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Tone Match tab: IRs from single notes against any recording."""

from __future__ import annotations

import os
import time

import numpy as np
import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.widgets import SpanSelector
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk

from . import audio
from . import engine as E
from . import tone_match as TM
from .audio import dev_index
from .config import APP_NAME, ROLES
from .util import MEDIA_EXT, load_any_audio


class ToneMatchTab:
    """Part of App (mixed in) - tone match tab: irs from single notes against any recording."""

    def _build_tone(self):
        t = self._scroll_body(self.tab_tone)
        self.tm = {"ref": None, "pz": None, "pairs": [], "pair_audio": [],
                   "sel": {"ref": None, "pz": None}}
        left = ttk.Frame(t, width=430)
        left.pack(side="left", fill="y", padx=10, pady=8)
        right = ttk.Frame(t)
        right.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=8)

        ttk.Label(left, wraplength=420, justify="left", foreground="#444", text=(
            "Make an IR that makes your instrument sound like a recording you love (another guitar, a "
            "record, a video...). They don't need to be recorded together.\n\n"
            "How: find a clear, sustained note in the reference (e.g. an open A ringing out). Then "
            "record yourself playing that SAME pitch (the same note name and octave, e.g. your own "
            "open A), 3-5 times, letting each ring ~2 s. That's one 'note pair'.\n"
            "Each pair only tells the app about that note's overtones, so do a few pairs with "
            "different notes (e.g. low E, A, D, G, B, high E) - more pairs = a more accurate match.")
        ).pack(fill="x")

        self.tm_info = {}
        for key, title in (("ref", "1. Reference: a note from the recording you want to sound like"),
                           ("pz", "2. You: the same note played on your instrument")):
            fr = ttk.LabelFrame(left, text=title)
            fr.pack(fill="x", pady=(8, 0))
            bf = ttk.Frame(fr)
            bf.pack(fill="x", padx=6, pady=4)
            if key == "pz":
                self.tm_rec_btn = ttk.Button(bf, text="Record 12 s from", command=self.tm_record)
                self.tm_rec_btn.pack(side="left")
                self.tm_src = tk.StringVar()
                self.tm_src_cb = ttk.Combobox(bf, textvariable=self.tm_src, state="readonly", width=20)
                self.tm_src_cb.pack(side="left", padx=4)
            if key == "pz":
                self._tip(self.tm_rec_btn, "Records 12 s from the chosen input. Play the same note as the "
                                           "reference 3-5 times, letting each ring ~2 s.")
                self._tip(self.tm_src_cb, "Which input to record: usually the Instrument (piezo / DI).")
            self._tip(ttk.Button(bf, text="Load file...", command=lambda k=key: self.tm_load(k)),
                      "Load any audio or video file: WAV, FLAC, MP3, M4A/AAC, OGG, MP4, MOV, MKV, "
                      "WEBM... (only the sound is used).").pack(side="left", padx=4)
            self._tip(ttk.Button(bf, text="Auto-select", command=lambda k=key: self.tm_auto(k)),
                      "Finds the clearest sustained note automatically. Or drag on the waveform (right) "
                      "to choose the part yourself.").pack(side="left", padx=4)
            self._tip(ttk.Button(bf, text="Play selection", command=lambda k=key: self.tm_play(k)),
                      "Listen to the selected part to check it's the right note.").pack(side="left", padx=4)
            self.tm_info[key] = ttk.Label(fr, text="(nothing loaded)", foreground="#2b6cb0",
                                          wraplength=410, justify="left")
            self.tm_info[key].pack(anchor="w", padx=6, pady=(0, 4))

        pf = ttk.LabelFrame(left, text="3. Save this pair, then repeat 1-2 with another note")
        pf.pack(fill="x", pady=(8, 0))
        pb = ttk.Frame(pf)
        pb.pack(fill="x", padx=6, pady=4)
        self._tip(ttk.Button(pb, text="Add this note pair", style="Rec.TButton", command=self.tm_add_pair),
                  "Saves the selected reference note + your note as a pair. Then pick another note in the "
                  "reference, record yourself playing it, and add that pair too. 4-6 pairs across the "
                  "range of the instrument works well.").pack(side="left")
        ttk.Button(pb, text="Remove", command=self.tm_remove_pair).pack(side="left", padx=4)
        ttk.Button(pb, text="Clear all", command=self.tm_clear_pairs).pack(side="left")
        self.tm_list = tk.Listbox(pf, height=5, font=("Consolas", 9))
        self.tm_list.pack(fill="x", padx=6, pady=(0, 6))

        bfr = ttk.LabelFrame(left, text="4. Build")
        bfr.pack(fill="x", pady=(8, 0))
        self.tm_mode = tk.StringVar(value="scratch")
        self.tm_refine_rb = ttk.Radiobutton(
            bfr, text="Start from my Capture IR, then nudge it toward the reference (best)", value="refine",
            variable=self.tm_mode)
        self.tm_refine_rb.pack(anchor="w", padx=6, pady=(4, 0))
        self._tip(self.tm_refine_rb, "Uses the IR you measured on the Capture tab (instrument + mics) as "
                  "the starting point - it already has your guitar's body detail - and only corrects the "
                  "overall tone toward the reference. Needs Capture takes, and your notes must be played "
                  "through the same pickup.")
        rb2 = ttk.Radiobutton(bfr, text="Notes only - no Capture session needed", value="scratch",
                              variable=self.tm_mode)
        rb2.pack(anchor="w", padx=6)
        self._tip(rb2, "Builds the IR from the note pairs alone. Gets the broad tone right but can't "
                  "know about narrow resonances between the notes you played. Use this for an electric "
                  "or bass, or when you haven't done a Capture session.")
        of = ttk.Frame(bfr)
        of.pack(fill="x", padx=6, pady=4)
        ttk.Label(of, text="Smoothing").pack(side="left")
        self.tm_smooth = tk.StringVar(value="1/3 oct")
        ttk.Combobox(of, values=["1/6 oct", "1/3 oct", "1/2 oct", "1 oct"], width=8,
                     textvariable=self.tm_smooth, state="readonly").pack(side="left", padx=4)
        ttk.Label(of, text="  Max change +/- dB").pack(side="left")
        self.tm_max = tk.IntVar(value=24)
        self._tip(ttk.Spinbox(of, from_=3, to=36, width=4, textvariable=self.tm_max),
                  "The most the matched IR may boost or cut at any frequency. Lower = gentler, safer.").pack(
            side="left", padx=4)
        ttk.Button(bfr, text="Build matched IR", style="Rec.TButton",
                   command=self.tm_build).pack(fill="x", padx=6, pady=(2, 6))

        af = ttk.LabelFrame(left, text="Listen")
        af.pack(fill="x", pady=(8, 0))
        ttk.Button(af, text="Reference", command=lambda: self.tm_play("ref")).pack(side="left", padx=4, pady=6)
        ttk.Button(af, text="Mine dry", command=lambda: self.tm_play("pz")).pack(side="left", padx=4)
        ttk.Button(af, text="Mine + matched IR", command=lambda: self.tm_play("pz", True)).pack(side="left", padx=4)
        ttk.Button(af, text="Stop", command=lambda: audio.sd and audio.sd.stop()).pack(side="left", padx=4)
        self.tm_msg = ttk.Label(left, wraplength=420, justify="left")
        self.tm_msg.pack(fill="x", pady=6)

        self.tm_fig = Figure(figsize=(7, 4.6 if self.compact else 6), dpi=100)
        self.tm_ax = {"ref": self.tm_fig.add_subplot(311), "pz": self.tm_fig.add_subplot(312)}
        self.tm_axc = self.tm_fig.add_subplot(313)
        self.tm_canvas = FigureCanvasTkAgg(self.tm_fig, master=right)
        self.tm_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.tm_span = {}
        for key in ("ref", "pz"):
            self.tm_span[key] = SpanSelector(
                self.tm_ax[key], lambda a, b, k=key: self._tm_on_select(k, a, b), "horizontal",
                useblit=True, interactive=True, drag_from_anywhere=True,
                props=dict(alpha=0.25, facecolor="#2b6cb0"))
        self._tm_draw()
        self._inputs_changed()
        self.tm_src.set(self.mic_name("piezo"))

    def _tm_set(self, key, x, fs, name):
        x = np.asarray(x, float)
        if x.ndim > 1:
            x = x.mean(axis=1)
        self.tm[key] = {"x": x, "fs": int(fs), "name": name}
        self.tm_auto(key)

    def tm_load(self, key):
        import soundfile as sf
        p = filedialog.askopenfilename(
            title="Reference recording" if key == "ref" else "Your instrument's recording",
            filetypes=[("Audio / video", " ".join("*." + e for e in MEDIA_EXT)), ("All files", "*.*")])
        if not p:
            return
        try:
            self.status.set("Loading...")
            self.update_idletasks()
            a, fs = load_any_audio(p)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't read that file: {exc}")
            return
        finally:
            self.status.set("Ready.")
        self._tm_set(key, a, fs, os.path.basename(p))

    def tm_record(self):
        if audio.sd is None:
            return
        if self.rig.recording:
            messagebox.showinfo(APP_NAME, "Already recording.")
            return
        started_here = False
        names = {self.mic_name(r): r for r in ROLES}
        role = names.get(self.tm_src.get(), "piezo")
        if self.rig.running() and role not in self.rig.roles:
            self.stop_audio()
        if not self.rig.running():
            sel = self._sel(role)
            if sel is None:
                messagebox.showinfo(APP_NAME, "Choose that input on the Setup tab first.")
                return
            try:
                self.rig.start({role: (sel[0], sel[1])}, int(self.fs.get()))
                started_here = True
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Couldn't open the piezo input: {exc}")
                return
        self.rig.begin()
        self.tm_rec_btn.configure(state="disabled")
        t_end = time.time() + 12

        def tick():
            left = t_end - time.time()
            if left > 0:
                self.tm_info["pz"].configure(text=f"RECORDING - play the same note 3-5 times...  "
                                                  f"{left:4.0f} s")
                self.after(200, tick)
                return
            data = self.rig.end()
            if started_here:
                self.rig.stop()
            self.tm_rec_btn.configure(state="normal")
            x = data.get(role, np.zeros(0))
            if len(x) < self.fs.get():
                self.tm_info["pz"].configure(text="Recording failed - check that input on the Setup tab.")
                return
            self._tm_set("pz", x, self.fs.get(), f"{self.mic_name(role)} recording")
        tick()

    def _tm_on_select(self, key, a, b):
        d = self.tm[key]
        if d is None:
            return
        i0, i1 = sorted((int(a * d["fs"]), int(b * d["fs"])))
        i0, i1 = max(0, i0), min(len(d["x"]), i1)
        if i1 - i0 < int(0.3 * d["fs"]):
            return
        self.tm["sel"][key] = (i0, i1)
        self._tm_analyse(key)

    def tm_auto(self, key):
        d = self.tm[key]
        if d is None:
            return
        self.tm["sel"][key] = TM.auto_select(d["x"], d["fs"])
        self._tm_draw()
        i0, i1 = self.tm["sel"][key]
        try:
            self.tm_span[key].extents = (i0 / d["fs"], i1 / d["fs"])
        except Exception:
            pass
        self._tm_analyse(key)

    def _tm_analyse(self, key):
        d = self.tm[key]
        i0, i1 = self.tm["sel"][key]
        try:
            h = TM.analyse(d["x"][i0:i1], d["fs"])
            d["h"] = h
            txt = (f"{d['name']}:  {TM.note_name(h['f0'])},  {len(h['freqs'])} harmonics up to "
                   f"{h['freqs'][-1] / 1000:.1f} kHz,  {h['plucks']} pluck(s) in selection "
                   f"({(i1 - i0) / d['fs']:.1f} s)")
            if h["plucks"] < 3:
                txt += "  - more plucks of the note = more accurate"
        except Exception as exc:
            d["h"] = None
            txt = f"{d['name']}: {exc}"
        self.tm_info[key].configure(text=txt)
        o = self.tm["pz" if key == "ref" else "ref"]
        if d.get("h") and o and o.get("h") and abs(d["h"]["f0"] / o["h"]["f0"] - 1) > 0.03:
            self.tm_msg.configure(text="Note: the reference and piezo notes are different pitches. "
                                  "Same-note pairs are much more accurate.", foreground="#a33")
        else:
            self.tm_msg.configure(text="", foreground="")

    def tm_add_pair(self):
        r, p = self.tm["ref"], self.tm["pz"]
        if not (r and r.get("h") and p and p.get("h")):
            messagebox.showinfo(APP_NAME, "Load/record both a reference and a piezo note first "
                                "(each needs a detected pitch).")
            return
        rows, same = TM.pair_points(r["h"], p["h"])
        if len(rows) < 3:
            messagebox.showinfo(APP_NAME, "Too few matching harmonics in this pair.")
            return
        self.tm["pairs"].append((r["h"], p["h"]))
        i0, i1 = self.tm["sel"]["pz"]
        self.tm.setdefault("pair_audio", []).append((p["x"][i0:i1].copy(), p["fs"]))
        nm = TM.note_name(r["h"]["f0"]).split(" ")[0]
        self.tm_list.insert("end", f"{nm:<4} {len(rows):3d} pts  ref {r['h']['plucks']}x / piezo "
                                   f"{p['h']['plucks']}x" + ("" if same else "  (different notes!)"))
        self.tm_msg.configure(text=f"{len(self.tm['pairs'])} note pair(s). Add more notes, then "
                              "Build matched IR.", foreground="")

    def tm_remove_pair(self):
        for i in sorted(self.tm_list.curselection(), reverse=True):
            self.tm_list.delete(i)
            self.tm["pairs"].pop(i)
            self.tm["pair_audio"].pop(i)

    def tm_clear_pairs(self):
        self.tm_list.delete(0, "end")
        self.tm["pairs"] = []
        self.tm["pair_audio"] = []

    def _measured_master(self):
        a, b = self.acc["A"], self.acc["B"]
        has_a = a is not None and not a.empty()
        has_b = b is not None and not b.empty()
        if not (has_a or has_b):
            return None
        p = self.params()
        p.blend_b = 0.0 if not has_b else (1.0 if not has_a else p.blend_b)
        acc0 = a if has_a else b
        p.master_ms = min(p.master_ms, int(0.75 * acc0.nfft / acc0.fs * 1000))
        return E.build_master(a, b, p, self.cal["A"], self.cal["B"])

    def tm_build(self):
        if not self.tm["pairs"]:
            messagebox.showinfo(APP_NAME, "Add at least one note pair first (3-6 is better).")
            return
        base = None
        if self.tm_mode.get() == "refine":
            try:
                base = self._measured_master()
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
            if base is None:
                messagebox.showinfo(APP_NAME, "Refine needs a measured IR: capture some takes with "
                                    "the piezo and mic first (Capture tab), or use 'From scratch'.")
                return
            # piezo side is compared *through* the measured IR (re-analysed per pair)
            pairs = self._tm_pairs_through(base)
            if pairs is None:
                return
        else:
            pairs = self.tm["pairs"]
        sm = {"1/6 oct": 1 / 6, "1/3 oct": 1 / 3, "1/2 oct": 1 / 2, "1 oct": 1.0}[self.tm_smooth.get()]
        p = self.params()
        try:
            m = TM.build_matched(pairs, fs_out=int(base["fs"] if base else self.fs.get()),
                                 smooth_oct=sm, max_db=float(self.tm_max.get()),
                                 master_ms=float(self.v_master_ms.get() or 500),
                                 low_cut=p.low_cut, high_cut=p.high_cut, base_master=base)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.irm = m
        n = len(self.tm["pairs"])
        self.master_src = f"tone match ({n} note{'s' if n > 1 else ''}, " \
                          f"{'refined measured IR' if base else 'from scratch'})"
        self.fits = {}
        for iid in self.ped_tree.get_children():
            self.ped_tree.set(iid, "err", "")
        self._tm_draw()
        self._plot_master()
        self.fit_info.configure(text=f"Using the {self.master_src}. Select targets and export.")
        self.tm_msg.configure(foreground="", text=(
            f"Built from {len(m['match']['points'])} harmonic points. Listen, then export on "
            "Build & Export (pressing Build IR there goes back to the measured IR)."))

    def _tm_pairs_through(self, base):
        """For refine mode the piezo notes are compared after passing through the
        measured IR, so only the remaining difference gets corrected."""
        if not self.tm.get("pair_audio") or len(self.tm["pair_audio"]) != len(self.tm["pairs"]):
            messagebox.showinfo(APP_NAME, "Refine needs the piezo audio of each pair - please "
                                "clear the pairs and add them again.")
            return None
        out = []
        for (rh, _), (x, fs) in zip(self.tm["pairs"], self.tm["pair_audio"]):
            out.append((rh, TM.analyse(TM.through_ir(x, fs, base), fs)))
        return out

    def tm_play(self, key, through=False):
        if audio.sd is None:
            return
        d = self.tm[key]
        if d is None:
            return
        i0, i1 = self.tm["sel"][key] or (0, len(d["x"]))
        y, fs = d["x"][i0:i1], d["fs"]
        if through:
            if self.irm is None:
                messagebox.showinfo(APP_NAME, "Build the matched IR first.")
                return
            y = TM.through_ir(y, fs, self.irm)
        rms = np.sqrt(np.mean(y ** 2)) + 1e-12
        y = y * (0.08 / rms)
        y = np.clip(y, -0.95, 0.95)
        try:
            audio.sd.play(y.astype(np.float32), fs, device=dev_index(self.out_dev.get()))
        except Exception:
            try:
                audio.sd.play(y.astype(np.float32), fs)
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Playback failed: {exc}")

    def _tm_draw(self):
        for key, title in (("ref", "Reference - drag to select the note(s)"),
                           ("pz", "Piezo - drag to select the same note")):
            ax = self.tm_ax[key]
            ax.clear()
            d = self.tm[key]
            ax.set_title(title, fontsize=9, loc="left")
            ax.set_yticks([])
            if d is None:
                ax.text(0.5, 0.5, "load or record a note", ha="center", va="center",
                        transform=ax.transAxes, color="#888")
                ax.set_xticks([])
                continue
            x, fs = d["x"], d["fs"]
            nb = 1500
            step = max(1, len(x) // nb)
            m = len(x) // step
            env = np.abs(x[:m * step]).reshape(m, step).max(axis=1)
            tt = np.arange(m) * step / fs
            ax.fill_between(tt, -env, env, color="#555", lw=0)
            ax.set_xlim(0, len(x) / fs)
            ax.tick_params(labelsize=7)
            if self.tm["sel"][key]:
                i0, i1 = self.tm["sel"][key]
                ax.axvspan(i0 / fs, i1 / fs, color="#2b6cb0", alpha=0.18)
        ax = self.tm_axc
        ax.clear()
        m = self.irm.get("match") if self.irm else None
        if m:
            pts = m["points"]
            ax.scatter(pts[:, 0], np.clip(m["points_db"], -30, 30),
                       s=6, color="#9aa0a6", label="harmonic points")
            ax.semilogx(m["grid"], m["curve"], color="#c0392b", lw=1.8, label="correction")
            ax.set_xlim(40, 16000)
            ax.set_ylim(-30, 30)
            ax.grid(True, which="both", alpha=0.25)
            ax.legend(fontsize=7, loc="lower left")
            ax.set_title("Correction " + ("on top of your measured IR" if m["refine"] else
                                          "applied to the piezo") + " (dB)", fontsize=9, loc="left")
            ax.tick_params(labelsize=7)
        else:
            ax.text(0.5, 0.5, "Correction curve appears here after Build", ha="center", va="center",
                    transform=ax.transAxes, color="#888")
            ax.set_xticks([])
            ax.set_yticks([])
        self.tm_fig.tight_layout()
        self.tm_canvas.draw_idle()
