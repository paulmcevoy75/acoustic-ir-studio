# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Setup tab: choosing devices and inputs."""

from __future__ import annotations

import json
import os

import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk

from . import audio
from . import engine as E
from . import win_volume as WV
from .audio import list_devices, list_mono_inputs
from .config import APP_NAME, AUTHOR, DEVICES_FILE, LINKS, MEAS_MIC_HINTS, ROLES, ROLE_BASE, SLOTS, STEPS_FILE, write_json
from .util import dev_family, short_dev
import logging

log = logging.getLogger("acoustic_ir")


class SetupTab:
    """Part of App (mixed in) - setup tab: choosing devices and inputs."""

    def _build_setup(self):
        t = self._scroll_body(self.tab_setup)
        cr = ttk.Frame(t)
        cr.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(cr, text=f"Created by {AUTHOR}  \u00b7 ", font=("Segoe UI", 9, "bold")).pack(side="left")
        self._link(cr, *LINKS["website"], font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Label(cr, text="      Thank you to ", foreground="#555").pack(side="left")
        self._link(cr, *LINKS["vic"]).pack(side="left")
        ttk.Label(cr, text=" for the inspiration.", foreground="#555").pack(side="left")
        top = ttk.Frame(t)
        top.pack(fill="x", padx=10, pady=10)
        ttk.Label(top, text="Instrument").pack(side="left")
        ic = ttk.Combobox(top, values=list(E.INSTRUMENTS), width=15, state="readonly",
                          textvariable=self.instrument)
        ic.pack(side="left", padx=(6, 18))
        ic.bind("<<ComboboxSelected>>", lambda e: self._instrument_changed())
        self._last_instrument = self.instrument.get()
        ttk.Label(top, text="Sample rate").pack(side="left")
        cb = ttk.Combobox(top, values=[44100, 48000, 96000], width=8, state="readonly",
                          textvariable=self.fs)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._rate_changed())
        ttk.Label(top, text="   Driver type").pack(side="left")
        apis = ["All"] + ([a["name"] for a in audio.sd.query_hostapis()] if audio.sd else [])
        default_api = "Windows WASAPI" if "Windows WASAPI" in apis else "All"
        self.api = tk.StringVar(value=default_api)
        a = ttk.Combobox(top, values=apis, textvariable=self.api, state="readonly", width=22)
        a.pack(side="left", padx=6)
        a.bind("<<ComboboxSelected>>", lambda e: self._refresh_devices())
        ttk.Button(top, text="Refresh devices", command=lambda: self._refresh_devices(rescan=True)).pack(side="left", padx=6)
        self.api_cb = a

        self.inp = {}
        fr = ttk.LabelFrame(t, text="Inputs - all of these record at the same time on every take")
        fr.pack(fill="x", padx=15, pady=4)
        ttk.Label(fr, text="Each choice is one mono input (e.g. 'My Interface - Input 2'). One mic of any "
                  "kind works; the others are optional. Type a label for each (e.g. 'SM57', 'USB Mic') so "
                  "you can tell them apart everywhere in the app.",
                  foreground="#555", wraplength=820, justify="left").grid(row=10, column=1, columnspan=3, sticky="w", pady=(0, 6))
        rows = (("piezo", "Instrument (piezo / DI)", None),
                ("piezo2", "Instrument 2 (2nd piezo element, optional)", True),
                ("A", "Reference Mic", True),
                ("B", "Color Mic (optional)", True),
                ("I", "Internal Mic (optional)", True))
        self.inp_widgets = {}
        self.inp_label = {}
        for k, (role, label, togg) in enumerate(rows):
            r = 2 * k
            v = {"dev": tk.StringVar(), "on": tk.BooleanVar(value=(role in ("piezo", "A"))),
                 "info": tk.StringVar()}
            self.inp[role] = v
            self.inp_label[role] = tk.StringVar()
            if togg is None:
                w0 = ttk.Label(fr, text=label)
            else:
                w0 = ttk.Checkbutton(fr, text=label, variable=v["on"], command=self._inputs_changed)
            w0.grid(row=r, column=0, sticky="w", padx=8, pady=(6, 0))
            self.inp_widgets[role] = {"name": w0}
            lf_ = ttk.Frame(fr)
            lf_.grid(row=r, column=2, sticky="w", padx=(0, 8), pady=(6, 0))
            ttk.Label(lf_, text="Label").pack(side="left")
            le = ttk.Entry(lf_, textvariable=self.inp_label[role], width=18)
            self.inp_widgets[role]["label"] = le
            le.pack(side="left", padx=4)
            le.bind("<FocusOut>", lambda e: self._inputs_changed())
            le.bind("<Return>", lambda e: self._inputs_changed())
            v["cb"] = ttk.Combobox(fr, textvariable=v["dev"], width=70, state="readonly", height=20)
            v["cb"].grid(row=r, column=1, sticky="we", padx=(0, 8), pady=(6, 0))
            v["cb"].bind("<<ComboboxSelected>>", lambda e: self._inputs_changed())
            sub = ttk.Frame(fr)
            sub.grid(row=r + 1, column=1, columnspan=2, sticky="w", pady=(2, 4))
            self.inp_widgets[role]["winlvl"] = ttk.Button(sub, text="Windows input level...",
                                                          command=WV.open_recording_settings)
            self.inp_widgets[role]["winlvl"].pack(side="left", padx=(0, 6))
            if role in SLOTS:
                self.inp_widgets[role]["cal"] = ttk.Button(sub, text="Calibration file...",
                                                           command=lambda s=role: self.load_cal(s))
                self.inp_widgets[role]["cal"].pack(side="left")
                ttk.Button(sub, text="Clear", width=6, command=lambda s=role: self.clear_cal(s)).pack(side="left", padx=4)
                ttk.Label(sub, textvariable=self.cal_name[role]).pack(side="left", padx=(4, 16))
            ttk.Label(sub, textvariable=v["info"], foreground="#2b6cb0").pack(side="left")
        fr.columnconfigure(1, weight=1)
        self.dev_hint = ttk.Label(t, foreground="#a33", wraplength=1000, justify="left")
        self.dev_hint.pack(fill="x", padx=15)

        out = ttk.LabelFrame(t, text="Audition output (headphones / monitors)")
        out.pack(fill="x", padx=15, pady=5)
        self.out_dev = tk.StringVar()
        self.out_cb = ttk.Combobox(out, textvariable=self.out_dev, width=70, state="readonly")
        self.out_cb.pack(side="left", padx=8, pady=8)

        help_txt = (
            "How the inputs are used:\n"
            "  - Instrument: the Interface input your guitar / bass is plugged into (piezo, or a DI'd electric).\n"
            "  - Instrument 2 (optional): if your pickup has two elements wired to separate outputs, the second "
            "one (same Interface as the first). The app then also makes an IR PAIR - one IR per element - for "
            "2-input convolvers like a Daisy Seed, and shows how much that gains over the summed pickup.\n"
            "  - Reference Mic: the mic whose sound the IR recreates - ideally an accurate one, e.g. a USB "
            "measurement mic (load its calibration file, 0-degree / on-axis), but any mic works (SM57, condenser...).\n"
            "  - Color Mic (optional): a second mic with a character you like. It gets its own IR, and on the "
            "Build tab you can mix the two by frequency (e.g. lows from the Reference Mic, highs from the Color Mic).\n"
            "  - Internal Mic (optional): the guitar's own built-in mic, if it has one. It's recorded with "
            "everything else and can be blended in when you audition.\n"
            "  - Every take records all inputs at once. Put the Reference Mic and Color Mic close together, "
            "and only turn on phantom power for mics that need it.\n"
            "  - A USB Mic (on a different device from the Interface) is clock-synced automatically; its level "
            "can be set on the Capture tab.\n"
            "  - Set every device to the SAME sample rate in Windows (Sound settings > device > Advanced) and "
            "here. Close other audio apps while capturing.\n"
            "  - USB Mic not listed? Plug it in and press Refresh devices. Still missing: set Driver type to "
            "'All', and check Windows Settings > Privacy > Microphone lets desktop apps use the microphone.")
        ttk.Label(t, text=help_txt, wraplength=1000, justify="left").pack(fill="x", padx=15, pady=10)
        self._refresh_devices()

    def _refresh_devices(self, rescan=False):
        if rescan and audio.sd is not None:
            if self.rig.running():
                self.stop_audio()
            try:                                   # re-scan: picks up newly plugged-in USB mics
                audio.sd._terminate()
                audio.sd._initialize()
            except Exception:
                log.debug("_refresh_devices: ignored error", exc_info=True)
            apis = ["All"] + [a["name"] for a in audio.sd.query_hostapis()]
            self.api_cb["values"] = apis
            if self.api.get() not in apis:
                self.api.set("All")
        ins = list_mono_inputs(self.api.get())
        outs = list_devices("output", self.api.get())
        self.in_map = {lab: (i, ch, nm) for lab, i, ch, nm in ins}
        labels = [lab for lab, *_ in ins]
        saved = {}
        try:
            with open(DEVICES_FILE) as fh:
                saved = json.load(fh)
        except Exception:
            log.debug("_refresh_devices: ignored error", exc_info=True)

        # the Interface = the input device with the most channels; a USB Mic = a 1-2 input
        # device that isn't the Interface
        # Windows often lists one interface as several 2-input devices ("Analogue 1 + 2 (iD..)",
        # "Analogue 3 + 4 (iD..)"), so count inputs per device FAMILY (the name in brackets)
        nch, fam_ch = {}, {}
        for _, _dev, c, nm in ins:
            nch[nm] = max(nch.get(nm, 0), c + 1)
        for nm, c in nch.items():
            fam_ch[dev_family(nm)] = fam_ch.get(dev_family(nm), 0) + c
        fam = max(fam_ch, key=fam_ch.get) if fam_ch else ""
        self.iface_family = fam if fam_ch.get(fam, 0) >= 3 else ""
        fam_devs = [nm for nm in nch if dev_family(nm) == self.iface_family]
        self.iface_name = max(fam_devs, key=nch.get) if fam_devs else ""
        is_iface = lambda nm: bool(self.iface_family) and dev_family(nm) == self.iface_family
        is_meas = lambda nm: (not is_iface(nm) and nch.get(nm, 0) <= 2 and
                              any(h in nm.lower() for h in MEAS_MIC_HINTS))

        def find(pred, ch=None):
            return next((lab for lab, i, c, nm in ins if pred(nm) and (ch is None or c == ch)), "")

        default_ch = {"piezo": 0, "piezo2": 1, "A": 0, "B": 1, "I": 2}
        for role, v in self.inp.items():
            if v["dev"].get() in self.in_map:
                continue
            pick = ""
            sv = saved.get(role)
            if sv and sv.get("name"):
                ch = int(sv.get("ch", 1)) - 1
                pick = find(lambda nm: nm == sv["name"], ch) or \
                    find(lambda nm: sv["name"].lower() in nm.lower(), ch)
                v["on"].set(sv.get("on", role in ("piezo", "A")))
                if sv.get("label"):
                    self.inp_label[role].set(sv["label"])
            if not pick:
                if role == "A":
                    pick = find(is_meas, 0) or find(is_iface, 1)
                else:
                    pick = find(is_iface, default_ch[role]) or find(is_iface)
            v["dev"].set(pick or (labels[0] if labels else ""))
        if not labels:
            self.dev_hint.configure(text="No input devices found. Plug in your interface and mic, then "
                                    "press Refresh devices.")
        elif len(nch) == 1:
            self.dev_hint.configure(text="Only one input device found. If you use a USB Mic, plug it in "
                                    "and press Refresh devices; if it still doesn't appear, try Driver type "
                                    "'All', and check Windows Settings > Privacy & security > Microphone "
                                    "allows desktop apps.")
        elif not self.iface_name:
            self.dev_hint.configure(text="No multi-input Interface found - each device shows 1-2 inputs. Try "
                                    "Driver type 'All' or another driver type; some drivers list each input "
                                    "pair of an interface as a separate device.")
        else:
            self.dev_hint.configure(text="")
        for v in self.inp.values():
            v["cb"]["values"] = labels
        self.out_cb["values"] = [lab for lab, _, _ in outs]
        if hasattr(self, "aud_out_cb"):
            self.aud_out_cb["values"] = self.out_cb["values"]
        if self.out_dev.get() not in self.out_cb["values"] and outs:
            stem = (self.iface_name.split("(")[-1].rstrip(") ").lower() if self.iface_name else "")
            pick = next((lab for lab, _, _ in outs if stem and stem in lab.lower()), outs[0][0])
            self.out_dev.set(pick)
        self._inputs_changed()

    def mic_name(self, role):
        """What to call an input everywhere: your label, else the device + input number."""
        lab = self.inp_label[role].get().strip() if hasattr(self, "inp_label") else ""
        if lab:
            return lab
        m = self._sel(role) if hasattr(self, "inp") else None
        base = ROLE_BASE[role]
        return f"{base} ({short_dev(m[2], m[1])})" if m else base

    def mic_name_full(self, role):
        """Name + exactly which device input it is (for the blend / mix controls)."""
        m = self._sel(role) if hasattr(self, "inp") else None
        where = short_dev(m[2], m[1]) if m else "no input"
        lab = self.inp_label[role].get().strip() if hasattr(self, "inp_label") else ""
        return f"{lab or ROLE_BASE[role]} [{where}]"

    def _sel(self, role):
        """(device_index, channel_index, device_name) for a role, or None."""
        return getattr(self, "in_map", {}).get(self.inp[role]["dev"].get())

    def _inputs_changed(self):
        p = self._sel("piezo")
        for role in ("piezo2", "A", "B", "I"):
            v = self.inp[role]
            v["cb"].configure(state="readonly" if v["on"].get() else "disabled")
            m = self._sel(role)
            if not v["on"].get():
                v["info"].set("off")
            elif m is None or p is None:
                v["info"].set("")
            elif role == "piezo2":
                v["info"].set("same device as Instrument 1 - good" if m[0] == p[0] else
                              "must be on the SAME device as the Instrument (sample-locked)")
            elif m[0] == p[0]:
                v["info"].set("same device as the Instrument (sample-locked)")
            else:
                v["info"].set("separate device (e.g. USB Mic) - auto clock-sync")
        if hasattr(self, "meter"):
            for role in ROLES:
                self.meter_name[role].configure(text=self.mic_name(role))
            for s_ in SLOTS:
                self.cov_radio[s_].configure(text=self.mic_name(s_))
            for role in ("piezo2", "A", "B", "I"):
                c, lab = self.meter[role]
                on = bool(self.inp[role]["on"].get())
                if not on:
                    c.delete("all")
                    lab.configure(text="off")
                for w in self.meter_row.get(role, []):      # only show inputs that are switched on
                    if on:
                        w.grid()
                    else:
                        w.grid_remove()
            self._refresh_input_levels()
        if hasattr(self, "mix_title"):
            self.mix_title.configure(text=f"0 = all {self.mic_name_full('A')}    "
                                          f"100 = all {self.mic_name_full('B')}")
        if hasattr(self, "tm_src_cb"):
            self.tm_src_cb["values"] = [self.mic_name(r) for r in ROLES]
        if hasattr(self, "aud_src"):
            self._refresh_audition_names()

    def save_devices(self):
        d = {}
        for role, v in self.inp.items():
            m = self._sel(role)
            d[role] = {"name": m[2] if m else "", "ch": (m[1] + 1) if m else 1,
                       "on": bool(v["on"].get()), "label": self.inp_label[role].get()}
        try:
            write_json(DEVICES_FILE, d)
        except OSError:
            log.warning("couldn't save the device choices", exc_info=True)

    def input_config(self):
        nice = {r: self.mic_name(r) for r in ROLES}
        roles = {}
        for role, v in self.inp.items():
            if role != "piezo" and not v["on"].get():
                continue
            m = self._sel(role)
            if m is None:
                raise ValueError(f"Choose an input for {nice[role]} on the Setup tab first.")
            roles[role] = (m[0], m[1])
        if not any(r in roles for r in SLOTS):
            raise ValueError("Turn on at least one mic (Reference Mic or Color Mic) on the Setup tab.")
        if "piezo2" in roles and roles["piezo2"][0] != roles["piezo"][0]:
            raise ValueError("Instrument 2 must be on the same device (Interface) as the Instrument, so the "
                             "two piezo elements stay sample-locked.")
        used = {}
        for role, key in roles.items():
            if key in used:
                raise ValueError(f"{nice[used[key]]} and {nice[role]} are set to the same input. "
                                 "Each needs its own input.")
            used[key] = role
        return roles

    def profile(self):
        return E.INSTRUMENTS[self.instrument.get()]

    def _step_overrides(self):
        try:
            with open(STEPS_FILE) as fh:
                return json.load(fh)
        except Exception:
            log.debug("_step_overrides: ignored error", exc_info=True)
            return {}

    def _session_key(self):
        quick = getattr(self, "v_session", None) is not None and self.v_session.get().startswith("Quick")
        return f"{self.instrument.get()}|{'quick' if quick else 'full'}"

    def base_steps(self):
        quick = self._session_key().endswith("quick")
        return self.profile()["quick" if quick else "steps"]

    def steps(self):
        """Steps for the chosen session, with your edited lengths (0 = skip that step)."""
        ov = self._step_overrides().get(self._session_key(), {})
        out = []
        for name, dur, txt in self.base_steps():
            if dur is not None and name in ov:
                d = int(ov[name])
                if d <= 0:
                    continue
                dur = d
            out.append((name, dur, txt))
        return out

    def edit_step_lengths(self):
        win = tk.Toplevel(self)
        win.title("Step lengths")
        win.transient(self)
        ov_all = self._step_overrides()
        key = self._session_key()
        ov = ov_all.get(key, {})
        ttk.Label(win, text=f"{self.instrument.get()} - {self.v_session.get()}\n"
                  "Seconds for each step (0 = skip it). Free play has no limit and can't be skipped.",
                  justify="left").grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=8)
        vars_ = {}
        r = 1
        for name, dur, _ in self.base_steps():
            ttk.Label(win, text=name).grid(row=r, column=0, sticky="w", padx=10, pady=2)
            if dur is None:
                ttk.Label(win, text="no limit").grid(row=r, column=1, sticky="w")
            else:
                v = tk.IntVar(value=int(ov.get(name, dur)))
                ttk.Spinbox(win, from_=0, to=900, increment=5, width=6, textvariable=v).grid(row=r, column=1)
                ttk.Label(win, text=f"(default {dur})", foreground="#777").grid(row=r, column=2, sticky="w", padx=6)
                vars_[name] = (v, dur)
            r += 1
        tot = ttk.Label(win)
        tot.grid(row=r, column=0, columnspan=3, sticky="w", padx=10, pady=6)

        def total(*_):
            try:
                t = sum(int(v.get()) for v, _ in vars_.values())
                tot.configure(text=f"Total: {t // 60} min {t % 60} s + free play")
            except Exception:
                log.debug("total: ignored error", exc_info=True)
        for v, _ in vars_.values():
            v.trace_add("write", total)
        total()

        def save():
            try:
                ov_all[key] = {n: int(v.get()) for n, (v, d) in vars_.items() if int(v.get()) != d}
                write_json(STEPS_FILE, ov_all)
            except (OSError, ValueError, tk.TclError) as exc:
                messagebox.showerror(APP_NAME, f"Couldn't save: {exc}", parent=win)
                return
            win.destroy()
            self.goto_step(min(self.step_i, len(self.steps())))

        def reset():
            for v, d in vars_.values():
                v.set(d)
        bf = ttk.Frame(win)
        bf.grid(row=r + 1, column=0, columnspan=3, sticky="e", padx=10, pady=(0, 10))
        ttk.Button(bf, text="Defaults", command=reset).pack(side="left", padx=4)
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="left", padx=4)
        ttk.Button(bf, text="Save", command=save).pack(side="left", padx=4)

    def _has_takes(self):
        return any(self.acc[s] is not None and not self.acc[s].empty() for s in SLOTS)

    def _instrument_changed(self):
        if self._has_takes():
            if not messagebox.askyesno(APP_NAME, "Switching instrument starts a new capture "
                                       "(the current takes are cleared - save the project first "
                                       "if you want to keep them). Continue?"):
                self.instrument.set(self._last_instrument)
                return
            self.acc = {s: None for s in SLOTS}
            self.take_stack = []
            self.irm = None
        self._last_instrument = self.instrument.get()
        pr = self.profile()
        self.v_lc.set(pr["low_cut"])
        self.v_master_ms.set(str(pr["master_ms"]))
        if self.v_name.get() in ("MyGuitar", "MyBass"):
            self.v_name.set("MyBass" if self.instrument.get() == "double bass" else "MyGuitar")
        self.goto_step(0)
        self._update_coverage()

    def _rate_changed(self):
        if any(self.acc[s] is not None and not self.acc[s].empty() for s in SLOTS):
            if not messagebox.askyesno(APP_NAME, "Changing the sample rate clears the captured "
                                       "takes (they must all share one rate). Continue?"):
                self.fs.set(self.acc["A"].fs if self.acc["A"] else self.acc["B"].fs)
                return
            self.acc = {s: None for s in SLOTS}
            self.take_stack = []
            self._update_coverage()
        if self.rig.running():
            self.stop_audio()

    def load_cal(self, s):
        p = filedialog.askopenfilename(title="Mic calibration file",
                                       filetypes=[("Calibration", "*.cal *.txt *.frd *.csv"), ("All", "*.*")])
        if not p:
            return
        try:
            self.cal[s] = E.load_calibration(p)
            self.cal_name[s].set(os.path.basename(p) + f"  ({len(self.cal[s][0])} points)")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def clear_cal(self, s):
        self.cal[s] = None
        self.cal_name[s].set("(none)")
