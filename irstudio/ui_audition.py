# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Audition panel: demo recording and playback through every IR."""

from __future__ import annotations

import json
import os
import threading
import time

import numpy as np
import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.widgets import SpanSelector
from tkinter import messagebox
from tkinter import ttk

from . import audio
from . import engine as E
from . import tone_match as TM
from .audio import AuditionPlayer, dev_index
from .config import APP_NAME, AUTOSAVE_DIR, ROLES, ROLE_BASE, log
from .util import decimate_line


class AuditionPanel:
    """Part of App (mixed in) - audition panel: demo recording and playback through every ir."""

    def record_demo(self):
        if self.rig.recording:
            if getattr(self, "rec_is_demo", False):
                self.stop_record()
            else:
                messagebox.showinfo(APP_NAME, "Wait for the current take to finish first.")
            return
        if self.busy:
            messagebox.showinfo(APP_NAME, "Still analysing the last take - one moment.")
            return
        if not self._ensure_audio():
            return
        self.audition_stop()
        self.rec_is_demo = True
        self.demo_btn.configure(state="disabled")
        self._start_rec("demo", 60)
        self.rec_is_step = False
        self.logln("Recording a demo (up to 60 s): play something typical - a bit of strumming, some "
                   "single notes. Press Stop when done.")
        self._demo_rec_tick()

    def _demo_rec_tick(self):
        if not hasattr(self, "aud_rec_btn"):
            return
        if self.rig.recording and getattr(self, "rec_is_demo", False):
            el = time.time() - self.rec_started
            self.aud_rec_btn.configure(text=f"\u25cf  REC {el:.0f} s")
            self.aud_status.configure(text="RECORDING the piezo - play something typical...",
                                      foreground="#c0392b")
            try:                                   # live waveform (copy the block list first:
                blocks = list(self.rig.buf.get("piezo", []))   # the audio thread appends to it)
                if blocks:
                    fs = int(self.fs.get())
                    step = max(1, len(blocks) // 1500)
                    pk = np.array([np.max(np.abs(b_)) for b_ in blocks[::step]])
                    t = np.arange(len(pk)) * step * len(blocks[0]) / fs
                    ax = self.aud_ax
                    ax.clear()
                    ax.fill_between(t, -pk, pk, color="#c62828", lw=0)
                    ax.set_xlim(0, max(10.0, t[-1] if len(t) else 0))
                    ax.set_ylim(-1, 1)
                    ax.set_yticks([])
                    ax.tick_params(labelsize=7)
                    ax.set_title(f"Recording... {el:.0f} s", fontsize=8)
                    self.aud_canvas.draw_idle()
            except Exception:
                log.debug("live demo waveform", exc_info=True)
            self.after(200, self._demo_rec_tick)
        else:
            self.aud_rec_btn.configure(text="\u25cf  Record")

    def _demo_done(self, data, fs, two):
        """Called by stop_record for a demo take. Keeps the piezo (and Internal Mic); the
        mics only if asked, for comparison."""
        self.demo_btn.configure(state="normal")
        x = data.get("piezo", np.zeros(0))
        if len(x) < 3 * fs:
            self.logln("Demo too short - record at least a few seconds.")
            if hasattr(self, "aud_status"):
                self._refresh_audition_names()
            return
        keep = {"piezo", "piezo2", "I"} | ({"A", "B"} if self.v_demo_mics.get() else set())
        self._set_demo({r: v for r, v in data.items() if r in keep}, fs, two)

    def _set_demo(self, data, fs, two, save=True):
        self.demo = {"fs": int(fs), "two": dict(two), **{r: np.asarray(v, np.float32) for r, v in data.items()
                                                       if r in ROLES}}
        self._aud_cache = {}
        dur = len(self.demo["piezo"]) / fs
        have = ", ".join(self._short_name(r) for r in ROLES if r in self.demo)
        self.demo_lab.configure(text=f"Demo: {dur:.0f} s ({have}). Audition it on the Build tab.",
                                foreground="#2e7d32")
        self.logln(f"Demo recorded: {dur:.0f} s ({have}).")
        if save:
            self._begin_autosave_session()
            demo = dict(self.demo)
            roles = [r for r in ROLES if r in demo]

            def work():
                try:
                    import soundfile as sf
                    n = max(len(demo[r]) for r in roles)
                    a = np.zeros((n, len(roles)), np.float32)
                    for k, r in enumerate(roles):
                        a[:len(demo[r]), k] = demo[r]
                    sf.write(os.path.join(AUTOSAVE_DIR, "demo.flac"), np.clip(a, -1, 1), fs, subtype="PCM_24")
                    with open(os.path.join(AUTOSAVE_DIR, "demo.json"), "w") as fh:
                        json.dump({"fs": fs, "roles": roles, "lengths": [len(demo[r]) for r in roles],
                                   "two": {r: bool(v) for r, v in two.items()}}, fh)
                except Exception:
                    log.exception("saving demo failed")
            threading.Thread(target=work, daemon=True).start()
        if hasattr(self, "aud_status"):
            self.player.loop = None
            self.player.pos = 0
            self._refresh_audition_names()
            self._aud_update(play_if=False)

    def _load_demo(self, folder):
        """Reload the demo saved next to an autosaved session (demo.flac + demo.json)."""
        try:
            import soundfile as sf
            with open(os.path.join(folder, "demo.json")) as fh:
                meta = json.load(fh)
            a, fs = sf.read(os.path.join(folder, "demo.flac"), dtype="float32", always_2d=True)
            data = {r: a[:meta["lengths"][k], k] for k, r in enumerate(meta["roles"])}
            self._set_demo(data, fs, meta.get("two", {}), save=False)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("loading demo failed")

    AUD_RAW = (("dry", "Piezo (dry)"), ("A", "Reference Mic"), ("B", "Color Mic"), ("I", "Internal Mic"))

    AUD_IR = (("ir", "Piezo + chosen IR"), ("irA", "Piezo + Reference Mic IR"),
              ("irB", "Piezo + Color Mic IR"), ("irmix", "Piezo + Blended IR"),
              ("dual", "Piezo 1 + 2 through the IR pair"))

    def _build_audition(self, parent):
        au = tk.LabelFrame(parent, text=" Audition ", font=("Segoe UI", 14, "bold"), fg="#1f3b66",
                           bd=2, relief="groove", padx=4, pady=2)
        au.pack(side="bottom", fill="x", pady=(4, 0))
        self.player = AuditionPlayer(self.rig)
        self.rig.aud = self.player
        left = ttk.Frame(au)
        right = ttk.Frame(au)
        if self.compact:                       # small screens: waveform full width, under the controls
            left.pack(side="top", fill="x", padx=(6, 4), pady=4)
            right.pack(side="top", fill="both", expand=True, padx=(4, 6), pady=(0, 4))
        else:
            left.pack(side="left", fill="y", padx=(6, 4), pady=4)
            right.pack(side="left", fill="both", expand=True, padx=(4, 6), pady=4)

        self.aud_target_lab = tk.Label(left, anchor="w", justify="left", wraplength=520,
                                       font=("Segoe UI", 9, "bold"), fg="#1f4e9c")
        self.aud_target_lab.pack(fill="x")
        self._tip(self.aud_target_lab, "Every 'Piezo + IR' option plays the IR cut to the length of the "
                  "target selected in Export targets - so you hear what that pedal will really do. "
                  "Choose your pedal there first.")
        self.aud_src = tk.StringVar(value="dry")
        self.aud_rb = {}
        tips = {"dry": "The piezo exactly as recorded - no IR. The 'before'.",
                "A": "The real Reference Mic, as recorded with the demo - the sound the IR aims for. "
                     "(Greyed out? The demo was recorded without the mics: tick 'also record the "
                     "mics' and record a new demo.)",
                "B": "The real Color Mic, as recorded with the demo. (Greyed out? Tick 'also record "
                     "the mics' and record a new demo.)",
                "I": "The guitar's internal mic, as recorded.",
                "ir": "The piezo through the IR you chose under 'Which IR to make' - exactly what "
                      "you'll export.",
                "irA": "The piezo through an IR made from the Reference Mic only.",
                "irB": "The piezo through an IR made from the Color Mic only.",
                "irmix": "The piezo through the IR blended from both mics (the Lows / Mids / Highs mix).",
                "dual": "Dual piezo: each element through its own IR (the _P1 / _P2 pair), then added - "
                        "what a 2-input convolver would play. Compare with 'Piezo + chosen IR', which is "
                        "the summed pickup through one IR."}
        grid = ttk.Frame(left)
        grid.pack(fill="x", pady=(2, 0))
        ttk.Label(grid, text="As recorded", foreground="#555").grid(row=0, column=0, sticky="w", padx=4)
        ttk.Label(grid, text="Through the IR (at the target's length)", foreground="#555").grid(
            row=0, column=1, sticky="w", padx=(18, 4))
        for col, rows in ((0, self.AUD_RAW), (1, self.AUD_IR)):
            for k, (key, text) in enumerate(rows):
                rb = ttk.Radiobutton(grid, text=text, value=key, variable=self.aud_src,
                                     command=lambda: self._aud_update(play_if=True))
                rb.grid(row=k + 1, column=col, sticky="w", padx=(4 if col == 0 else 18, 4), pady=0)
                self.aud_rb[key] = rb
                self._tip(rb, tips[key])

        tf = ttk.Frame(left)
        tf.pack(fill="x", pady=(6, 2))
        self.aud_rec_btn = ttk.Button(tf, text="\u25cf  Record", style="Transport.TButton", width=11,
                                      command=self.record_demo)
        self.aud_rec_btn.pack(side="left")
        self._tip(self.aud_rec_btn, "Records a new demo from the piezo (up to 60 s) - play anything you "
                                    "want to hear through the IRs. Stop (or Record again) ends it. The "
                                    "Internal Mic / Instrument 2 are recorded too if they're switched on.")
        self.aud_play_btn = ttk.Button(tf, text="\u25b6  Play", style="Transport.TButton", width=11,
                                       command=self.audition_play)
        self.aud_play_btn.pack(side="left", padx=6)
        self._tip(self.aud_play_btn, "Plays the demo in a loop - the whole demo, or just the part you "
                                     "dragged across on the waveform.")
        st_ = ttk.Button(tf, text="\u25a0  Stop", style="Transport.TButton", width=11,
                         command=self.transport_stop)
        st_.pack(side="left")
        self._tip(st_, "Stops playback - or the demo recording, if one is running.")
        self.v_aud_loop = tk.BooleanVar(value=True)      # Play always loops
        cf = ttk.Frame(left)
        cf.pack(fill="x", pady=(4, 0))
        ttk.Label(cf, text="Volume").pack(side="left")
        self.v_aud_vol = tk.DoubleVar(value=0)
        vs = ttk.Scale(cf, from_=-24, to=12, variable=self.v_aud_vol, length=110, command=self._aud_vol)
        vs.pack(side="left", padx=4)
        self._aud_vol_lab = ttk.Label(cf, text="+0 dB", width=6)
        self._aud_vol_lab.pack(side="left")
        self._tip(vs, "Audition volume (doesn't affect the IR). Every option is already level-matched, so "
                      "you compare tone, not loudness.")

        bf = ttk.Frame(left)
        bf.pack(fill="x", pady=(4, 0))
        ttk.Label(bf, text="Blend in Internal Mic").pack(side="left")
        self.v_aud_int = tk.DoubleVar(value=0)
        self.aud_int_sc = ttk.Scale(bf, from_=0, to=100, variable=self.v_aud_int, length=110,
                                    command=self._aud_int_moved)
        self.aud_int_sc.pack(side="left", padx=4)
        self._aud_int_lab = ttk.Label(bf, text="0%", width=5)
        self._aud_int_lab.pack(side="left")
        self._tip(self.aud_int_sc, "Mix some of the guitar's internal mic in with whatever is playing - "
                                   "like a blender preamp. Needs an Internal Mic in the demo.")

        of = bf
        ttk.Label(of, text="   Output").pack(side="left")
        self.aud_out_cb = ttk.Combobox(of, textvariable=self.out_dev, state="readonly", width=26,
                                       values=list(self.out_cb["values"]))
        self.aud_out_cb.pack(side="left", padx=4)
        self.aud_out_cb.bind("<<ComboboxSelected>>", lambda e: self._aud_out_changed())
        self._tip(self.aud_out_cb, "Where Audition plays: your interface's headphones/outputs, or "
                                   "speakers / Bluetooth. Same setting as on the Setup tab.")

        rf = ttk.Frame(left)
        rf.pack(fill="x", pady=(4, 0))
        self.aud_rec_row = rf
        self.v_demo_mics = tk.BooleanVar(value=True)
        dm = ttk.Checkbutton(rf, text="Record: also record the mics, to compare", variable=self.v_demo_mics)
        dm.pack(side="left", padx=6)
        self._tip(dm, "Tick to also record the Reference / Color Mic with the demo, so you can A/B the "
                      "real mics against 'Piezo + IR'. Leave off to record the piezo only.")

        self.aud_status = ttk.Label(left, foreground="#555", wraplength=470, justify="left")
        self.aud_status.pack(fill="x", pady=(4, 0))

        # waveform + looper
        self.aud_fig = Figure(figsize=(3.4, 1.5 if self.compact else 1.7), dpi=100)
        self.aud_ax = self.aud_fig.add_subplot(111)
        self.aud_canvas = FigureCanvasTkAgg(self.aud_fig, master=right)
        self.aud_canvas.get_tk_widget().pack(fill="both", expand=True)
        lf = ttk.Frame(right)
        lf.pack(fill="x")
        ttk.Label(lf, text="Drag to loop a part", foreground="#555").pack(side="left")
        ttk.Button(lf, text="Whole demo", command=self._aud_clear_loop).pack(side="right")
        self.aud_span = SpanSelector(self.aud_ax, self._aud_on_span, "horizontal", useblit=True,
                                     interactive=True, drag_from_anywhere=True,
                                     props=dict(alpha=0.2, facecolor="#2e7d32"))
        self.aud_canvas.mpl_connect("button_press_event", self._aud_click)
        self._aud_bg = None
        self.aud_canvas.mpl_connect("draw_event", self._aud_on_draw)
        self._aud_cache = {}
        self._aud_y = None
        self._aud_head = None
        self._aud_refresh_job = None
        self._aud_tick_job = None
        self._refresh_audition_names()
        self._aud_draw()

    def _short_name(self, role):
        """Label if given, else the generic name ('Piezo' for the instrument input)."""
        lab = self.inp_label[role].get().strip() if hasattr(self, "inp_label") else ""
        return lab or {"piezo": "Piezo", "piezo2": "Piezo 2"}.get(role, ROLE_BASE.get(role, role))

    def _target_iid(self):
        if not hasattr(self, "ped_tree"):
            return None
        sel = self.ped_tree.selection()
        if not sel:
            return None
        f = self.ped_tree.focus()
        return f if f in sel else sel[0]

    def _refresh_audition_names(self):
        if not hasattr(self, "aud_rb"):
            return
        src = self.demo
        self.aud_rb["dry"].configure(text=f"{self._short_name('piezo')} (dry)")
        for key in ("A", "B", "I"):
            self.aud_rb[key].configure(text=self._short_name(key))
            ok = src is not None and key in src
            self.aud_rb[key].state(["!disabled"] if ok else ["disabled"])
        self.aud_rb["irA"].configure(text=f"Piezo + {self._short_name('A')} IR")
        self.aud_rb["irB"].configure(text=f"Piezo + {self._short_name('B')} IR")
        built = self.master_src in (None, "built")
        has_a, has_b = self._has("A"), self._has("B")
        names = {"mix": "Blended", "A": self._short_name("A"), "B": self._short_name("B")}
        self.aud_rb["ir"].configure(text=(f"Piezo + chosen IR ({names[self.v_ir_src.get()]})" if built
                                          else "Piezo + loaded IR"))
        self.aud_rb["irA"].state(["!disabled"] if built and has_a else ["disabled"])
        self.aud_rb["irB"].state(["!disabled"] if built and has_b else ["disabled"])
        self.aud_rb["irmix"].state(["!disabled"] if built and has_a and has_b else ["disabled"])
        self.aud_int_sc.state(["!disabled"] if src is not None and "I" in src else ["disabled"])
        dual_ok = (src is not None and "piezo2" in src and getattr(self, "dual_pair", None) is not None
                   and self.master_src in (None, "built"))
        self.aud_rb["dual"].state(["!disabled"] if dual_ok else ["disabled"])
        iid = self._target_iid()
        if iid is None:
            self.aud_target_lab.configure(text="Step 1: select your pedal in 'Export targets' (left) - "
                                               "every IR here is played at that pedal's length.", fg="#c0392b")
        else:
            pd = self.pedals[int(iid)]
            self.aud_target_lab.configure(
                text=f"IRs play at:  {pd['name']}  ({pd['samples'] / pd['fs'] * 1000:.0f} ms)"
                     "  - change it in Export targets", fg="#1f4e9c")
        if src is None:
            self.aud_status.configure(text="No demo yet - press 'Record new demo' and play ~30 s of "
                                           "something typical.", foreground="#555")
        else:
            self.aud_status.configure(text=f"Demo: {len(src['piezo']) / src['fs']:.0f} s "
                                           f"({', '.join(self._short_name(r) for r in ROLES if r in src)}).",
                                      foreground="#555")

    def _params_for(self, src):
        p = self.params()
        if src == "mix":
            p.mix_bands = self._mix_bands()
        else:
            p.mix_bands = None
            p.blend_b = 0.0 if src == "A" else 1.0
        return p

    def _master_for(self, src):
        """The full-length IR for a source ('mix' / 'A' / 'B'), built on demand and cached."""
        if self.master_src not in (None, "built") and self.irm is not None:
            return self.irm                        # a loaded or tone-matched IR
        if src == self.v_ir_src.get() and self.irm is not None:
            return self.irm
        p = self._params_for(src)
        key = ("master", src, repr(p))
        if key in self._aud_cache:
            return self._aud_cache[key]
        a = self.acc["A"] if src in ("A", "mix") and self._has("A") else None
        b = self.acc["B"] if src in ("B", "mix") and self._has("B") else None
        if a is None and b is None:
            raise ValueError("No takes for that mic yet.")
        if a is None or b is None:
            p.mix_bands = None
            p.blend_b = 0.0 if a is not None else 1.0
        acc0 = a if a is not None else b
        p.master_ms = min(p.master_ms, int(0.75 * acc0.nfft / acc0.fs * 1000))
        m = E.build_master(a, b, p, self.cal["A"], self.cal["B"])
        self._aud_cache[key] = m
        return m

    def _fit_master(self, m, iid):
        pd = self.pedals[int(iid)]
        key = ("fit", id(m), iid, self.v_norm.get())
        if key not in self._aud_cache:
            self._aud_cache[key] = E.fit_for_pedal(m, pd["fs"], pd["samples"], normalize="peak",
                                                   fmin=self.profile()["fmin"])
        return self._aud_cache[key]

    def _render_audition(self, mode):
        src = self.demo
        if src is None:
            raise ValueError("Record a demo first: press 'Record new demo' (in Audition) and play "
                             "~30 s of something typical.")
        fs = src["fs"]
        x = np.asarray(src["piezo"], float)
        if "piezo2" in src:                               # dual piezo: the summed pickup
            x2 = np.asarray(src["piezo2"], float)
            m_ = min(len(x), len(x2))
            x1d, x2d = x[:m_], x2[:m_]
            x = x1d + x2d
        b = float(self.v_aud_int.get()) / 100 if "I" in src else 0.0
        iid = self._target_iid()
        key = (mode, round(b, 2), id(self.irm), iid, self.v_ir_src.get(), repr(self.params()))
        if key in self._aud_cache:
            return self._aud_cache[key], fs
        if mode in ("A", "B", "I"):
            if mode not in src:
                raise ValueError(f"The demo has no {self._short_name(mode)} recording - tick 'also record "
                                 "the mics' and record a new demo.")
            y = np.asarray(src[mode], float)
            if src.get("two", {}).get(mode):           # USB Mic: line it up with the piezo
                try:
                    _, y, _ = E.align_two_device(x, y, fs)
                except Exception:
                    log.info("demo alignment failed for %s", mode, exc_info=True)
            y = np.pad(y, (0, max(0, len(x) - len(y))))[:len(x)]
            b = 0.0 if mode == "I" else b
        elif mode == "dry":
            y = x
        elif mode == "dual":
            if "piezo2" not in src or self.dual_pair is None:
                raise ValueError("Needs dual-piezo takes (Instrument 2 on) and a demo recorded with "
                                 "Instrument 2 on.")
            if iid is None:
                raise ValueError("Select your pedal in 'Export targets' first.")
            pd_ = self.pedals[int(iid)]
            fp = E.fit_pair(self.dual_pair, pd_["fs"], pd_["samples"])
            y = (TM.through_ir(x1d, fs, {"h": fp["h1"], "fs": fp["fs"]}) +
                 TM.through_ir(x2d, fs, {"h": fp["h2"], "fs": fp["fs"]}))
        else:
            if iid is None:
                raise ValueError("Select your pedal in 'Export targets' first - the IR is played at "
                                 "that pedal's length.")
            if self.irm is None:
                self.build()
            if self.irm is None:
                raise ValueError("Build the IR first.")
            srcm = {"ir": self.v_ir_src.get(), "irA": "A", "irB": "B", "irmix": "mix"}[mode]
            r = self._fit_master(self._master_for(srcm), iid)
            y = TM.through_ir(x, fs, {"h": r["h"], "fs": r["fs"]})
        if b > 0:
            zi = np.asarray(src["I"], float)
            if src.get("two", {}).get("I"):
                try:
                    _, zi, _ = E.align_two_device(x, zi, fs)
                except Exception:
                    pass
            zi = np.pad(zi, (0, max(0, len(y) - len(zi))))[:len(y)]
            rms = lambda v: np.sqrt(np.mean(v ** 2)) + 1e-12
            y = (1 - b) * y / rms(y) + b * zi / rms(zi)
        for _ in range(3):              # same loudness for every mode, with peaks soft-limited
            y = y / (np.sqrt(np.mean(y ** 2)) + 1e-12) * 10 ** (-15 / 20)
            a = np.abs(y)
            y = np.where(a > 0.7, np.sign(y) * (0.7 + 0.29 * np.tanh((a - 0.7) / 0.29)), y)
        y = y.astype(np.float32)
        self._aud_cache[key] = y
        return y, fs

    def _aud_update(self, play_if=True):
        """Render the selected option and hand it to the player (keeps the play position, so
        switching while playing carries on from the same spot)."""
        mode = self.aud_src.get()
        if self.demo is None:
            self._aud_y = None
            self._aud_draw()
            return False
        try:
            if mode not in ("dry", "A", "B", "I"):
                self.status.set("Preparing the IR for audition...")
                self.update_idletasks()
            y, fs = self._render_audition(mode)
        except Exception as exc:
            self.status.set("Ready.")
            if play_if:
                messagebox.showinfo(APP_NAME, str(exc))
            log.info("audition render: %s", exc)
            return False
        self.player.set_buffer(y, fs)
        self._aud_y = (y, fs)
        self._aud_draw()
        self.status.set("Ready." if not self.player.playing else self._aud_playing_text())
        return True

    def _aud_playing_text(self):
        return f"Playing: {self.aud_rb[self.aud_src.get()].cget('text')}  ->  {self.player.dev_name}"

    def audition_play(self):
        if audio.sd is None:
            return
        if self.rig.recording:
            messagebox.showinfo(APP_NAME, "Stop the recording first.")
            return
        if not self._aud_update(play_if=True):
            return
        dev = dev_index(self.out_dev.get())
        try:
            self.player.start(dev)
        except Exception as exc:
            log.exception("audition output failed")
            messagebox.showerror(APP_NAME, f"Couldn't play on '{self.out_dev.get()}':\n{exc}\n\n"
                                 "Pick another Output, or set that device to the same sample rate as the "
                                 "app in Windows (Sound settings > device > Advanced). If you just "
                                 "connected it, press Refresh devices on the Setup tab.")
            return
        self.player.looping = bool(self.v_aud_loop.get())
        self.status.set(self._aud_playing_text())
        if self._aud_tick_job is None:
            self._aud_tick()

    def _play_ir(self):
        if self.irm is None:
            self.build()
            if self.irm is None:
                return
        iid = self._target_iid()
        try:
            r = self._fit(iid) if iid is not None else {"h": self.irm["h"], "fs": self.irm["fs"]}
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't prepare the IR: {exc}")
            return
        h = np.asarray(r["h"], float)
        fs = int(r["fs"])
        y = np.concatenate([np.zeros(int(0.05 * fs)), h, np.zeros(int(0.6 * fs))])
        y = (y / (np.max(np.abs(y)) + 1e-12) * 0.5).astype(np.float32)
        self.player.loop = None
        self.player.set_buffer(y, fs)
        self.player.pos = 0
        self.player.looping = False
        try:
            self.player.start(dev_index(self.out_dev.get()))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't play on '{self.out_dev.get()}': {exc}")
            return
        self._aud_y = None                           # the audition waveform is re-rendered on next Play
        self.status.set("Playing the IR...")
        if self._aud_tick_job is None:
            self._aud_tick()

    def transport_stop(self):
        if self.rig.recording and getattr(self, "rec_is_demo", False):
            self.aud_rec_btn.configure(text="Saving...")
            self.aud_status.configure(text="Stopping...", foreground="#555")
            self.update_idletasks()
            self.stop_record()
            return
        self.audition_stop()

    def audition_stop(self):
        if hasattr(self, "player"):
            self.player.stop()
            self._aud_draw_head()
        self.status.set("Ready.")

    def _aud_out_changed(self):
        if self.player.playing:
            self.player.stop(rewind=False)
            self.audition_play()

    def _aud_vol(self, _e=None):
        v = float(self.v_aud_vol.get())
        self.player.gain = 10 ** (v / 20)
        self._aud_vol_lab.configure(text=f"{v:+.0f} dB")

    def _aud_int_moved(self, _e=None):
        self._aud_int_lab.configure(text=f"{self.v_aud_int.get():.0f}%")
        if self._aud_refresh_job is not None:
            self.after_cancel(self._aud_refresh_job)
        self._aud_refresh_job = self.after(300, self._aud_int_apply)

    def _aud_int_apply(self):
        self._aud_refresh_job = None
        self._aud_update(play_if=False)

    def _aud_target_changed(self):
        if hasattr(self, "aud_rb"):
            self._refresh_audition_names()
            if self.aud_src.get() not in ("dry", "A", "B", "I") and self.demo is not None and self.irm is not None:
                self._aud_update(play_if=False)

    def _aud_draw(self):
        ax = self.aud_ax
        ax.clear()
        self._aud_head = None
        if self._aud_y is None:
            ax.text(0.5, 0.5, "No demo yet" if self.demo is None else "Press Play", ha="center",
                    va="center", color="#888", transform=ax.transAxes)
            ax.set_xticks([])
            ax.set_yticks([])
        else:
            y, fs = self._aud_y
            t = np.arange(len(y)) / fs
            tt, yy = decimate_line(t, y, 1600)
            ax.plot(tt, yy, color="#1565c0" if self.aud_src.get() in ("dry", "A", "B", "I") else "#c62828",
                    lw=0.5)
            ax.set_xlim(0, len(y) / fs)
            ax.set_ylim(-1, 1)
            ax.set_yticks([])
            ax.tick_params(labelsize=7)
            ax.set_xlabel("s", fontsize=7, labelpad=0)
            if self.player.loop:
                a, b = self.player.loop
                ax.axvspan(a / fs, b / fs, color="#2e7d32", alpha=0.15)
            self._aud_head = ax.axvline(self.player.pos / fs, color="#111", lw=1, animated=True)
            ax.set_title(self.aud_rb[self.aud_src.get()].cget("text"), fontsize=8)
        self.aud_fig.subplots_adjust(left=0.03, right=0.98, top=0.86, bottom=0.2)
        self.aud_canvas.draw_idle()

    def _aud_on_draw(self, _e=None):
        try:
            self._aud_bg = self.aud_canvas.copy_from_bbox(self.aud_fig.bbox)
            if self._aud_head is not None:
                self.aud_ax.draw_artist(self._aud_head)
        except Exception:
            self._aud_bg = None

    def _aud_draw_head(self):
        """Move the playhead by redrawing only that line over a cached background (cheap -
        a full redraw 10x a second steals time from the audio and causes clicks)."""
        if self._aud_head is None or self._aud_y is None:
            return
        fs = self._aud_y[1]
        self._aud_head.set_xdata([self.player.pos / fs] * 2)
        if self._aud_bg is None:
            self.aud_canvas.draw_idle()
            return
        try:
            self.aud_canvas.restore_region(self._aud_bg)
            self.aud_ax.draw_artist(self._aud_head)
            self.aud_canvas.blit(self.aud_fig.bbox)
        except Exception:
            self.aud_canvas.draw_idle()

    def _aud_tick(self):
        self._aud_tick_job = None
        self._aud_draw_head()
        if self.player.playing:
            self._aud_tick_job = self.after(120, self._aud_tick)
        else:
            self.status.set("Ready.")

    def _aud_on_span(self, a, b):
        if self._aud_y is None:
            return
        fs = self._aud_y[1]
        n = len(self._aud_y[0])
        i0, i1 = sorted((int(a * fs), int(b * fs)))
        i0, i1 = max(0, i0), min(n, i1)
        if i1 - i0 < int(0.25 * fs):
            return
        self.player.loop = (i0, i1)
        self.player.pos = i0
        self.v_aud_loop.set(True)
        self.player.looping = True
        self._aud_draw()

    def _aud_click(self, ev):
        """A plain click (no drag) moves the play position."""
        if ev.inaxes is self.aud_ax and ev.dblclick and self._aud_y is not None:
            self.player.pos = int(max(0, ev.xdata) * self._aud_y[1])
            self._aud_draw_head()

    def _aud_clear_loop(self):
        self.player.loop = None
        try:
            self.aud_span.clear()
        except Exception:
            pass
        self._aud_draw()
