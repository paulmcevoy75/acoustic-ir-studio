# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Projects, autosave, crash recovery and importing recordings."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time

import numpy as np
from tkinter import filedialog
from tkinter import messagebox

from . import engine as E
from .config import APP_NAME, AUTOSAVE_DIR, AUTOSAVE_PREV, ROLES, SLOTS, log, write_atomic, write_json
from .util import safe_name


class ProjectFiles:
    """Part of App (mixed in) - projects, autosave, crash recovery and importing recordings."""

    def save_session(self, title="Save project"):
        """Returns True if saved. The project's folder is also where its IRs get exported."""
        if not self._has_takes() and self.irm is None:
            messagebox.showinfo(APP_NAME, "Nothing captured yet.")
            return False
        cur = getattr(self, "project_path", None)
        start = os.path.dirname(cur) if cur else self.projects_dir()
        p = filedialog.asksaveasfilename(
            title=title, defaultextension=".irproject", initialdir=start,
            initialfile=os.path.basename(cur) if cur else (safe_name(self.v_name.get()) or "IR") + ".irproject",
            filetypes=[("IR project", "*.irproject")])
        if not p:
            return False
        self._write_project(p)
        self.project_path = p
        self.status.set(f"Project saved: {p}  (its IRs export to '{self._project_ir_dir()}')")
        return True

    def _project_name(self):
        p = getattr(self, "project_path", None)
        return os.path.splitext(os.path.basename(p))[0] if p else (safe_name(self.v_name.get()) or "IR")

    def _project_ir_dir(self):
        """'<folder the project is saved in>/<Project Name> IRs'."""
        p = getattr(self, "project_path", None)
        return os.path.join(os.path.dirname(p), f"{self._project_name()} IRs") if p else None

    def _settings(self):
        return {"version": 2, "name": self.v_name.get(), "instrument": self.instrument.get(),
                "smoothing": self.v_smooth.get(), "reg_db": float(self.v_reg.get()),
                "low_cut": float(self.v_lc.get() or 0), "high_cut": float(self.v_hc.get() or 0),
                "phase": self.v_phase.get(), "blend": float(self.v_blend.get()),
                "invert": bool(self.v_inv.get()), "master_ms": self.v_master_ms.get(),
                "tame": bool(self.v_tame.get()), "tame_db": float(self.v_tame_db.get()),
                "tame_hz": self.v_tame_hz.get(),
                "estimator": self.v_est.get(), "session": self.v_session.get(),
                "mix": {k: float(v.get()) for k, v in self.v_mix.items()}, "mix_link": bool(self.v_mix_link.get()), "ir_src": self.v_ir_src.get(),
                "normalize": self.v_norm.get(), "norm_v": 2, "bits": self.v_bits.get(),
                "pedal_corr": [list(c) for c in self.pedal_corr] if getattr(self, "pedal_corr", None) else None,
                "pedal_corr_rounds": getattr(self, "pedal_corr_rounds", 0),
                "selected": [self.pedals[int(i)]["name"] for i in self.ped_tree.selection()],
                "step": int(getattr(self, "step_i", 0)),
                "master_src": self.master_src if self.master_src not in (None, "built") else None}

    def _write_project(self, path, include_demo=True):
        self._write_npz(path, self._project_dict(include_demo))

    @staticmethod
    def _write_npz(path, d):
        def w(tmp):
            with open(tmp, "wb") as fh:
                np.savez_compressed(fh, **d)
        write_atomic(path, w)

    def _project_dict(self, include_demo=True):
        d = {"settings": np.array(json.dumps(self._settings()))}
        for s in SLOTS:
            if self.acc[s] is not None and not self.acc[s].empty():
                d.update(self.acc[s].to_dict(s))
            if self.cal[s] is not None:
                d[f"{s}_calf"], d[f"{s}_calg"] = self.cal[s]
                d[f"{s}_caln"] = np.array(self.cal_name[s].get())
        if self.irm is not None:
            d["master_h"] = self.irm["h"]
            d["master_fs"] = np.array(self.irm["fs"])
        demo = getattr(self, "demo", None) if include_demo else None
        if demo is not None:
            roles = [r for r in ROLES if r in demo]
            d["demo_meta"] = np.array(json.dumps({"fs": demo["fs"], "roles": roles,
                                                  "two": {k: bool(v) for k, v in demo["two"].items()}}))
            for r in roles:
                d[f"demo_{r}"] = (np.clip(demo[r], -1, 1) * 32767).astype(np.int16)
        return d

    def load_session(self, p=None):
        if p is None:
            p = filedialog.askopenfilename(filetypes=[("IR project", "*.irproject *.irsession"),
                                                      ("All", "*.*")])
        if not p:
            return False
        try:
            d = np.load(p, allow_pickle=False)
            st = json.loads(str(d["settings"])) if "settings" in d else {}
            ap = os.path.abspath(p)
            in_auto = any(ap.startswith(os.path.abspath(x) + os.sep) for x in (AUTOSAVE_DIR, AUTOSAVE_PREV))
            self.project_path = None if in_auto else ap
            if st.get("instrument") in E.INSTRUMENTS:
                self.instrument.set(st["instrument"])
                self._last_instrument = st["instrument"]
            for s in SLOTS:
                self.acc[s] = E.Accumulator.from_dict(d, s) if f"{s}_meta" in d else None
                self.cal[s], _ = None, self.cal_name[s].set("(none)")
                if f"{s}_calf" in d:
                    self.cal[s] = (d[f"{s}_calf"], d[f"{s}_calg"])
                    self.cal_name[s].set(str(d[f"{s}_caln"]))
            for key, var in (("name", self.v_name), ("smoothing", self.v_smooth),
                             ("reg_db", self.v_reg), ("low_cut", self.v_lc),
                             ("high_cut", self.v_hc), ("phase", self.v_phase),
                             ("blend", self.v_blend), ("invert", self.v_inv),
                             ("tame", self.v_tame), ("tame_db", self.v_tame_db), ("tame_hz", self.v_tame_hz),
                             ("master_ms", self.v_master_ms), ("normalize", self.v_norm),
                             ("estimator", self.v_est), ("session", self.v_session),
                             ("bits", self.v_bits)):
                if key in st:
                    var.set(st[key])
            for k, v in (st.get("mix") or {}).items():
                if k in self.v_mix:
                    self.v_mix[k].set(v)
            if "mix_link" in st:
                self.v_mix_link.set(bool(st["mix_link"]))
            if st.get("normalize") == "Match loudness" and not st.get("norm_v"):
                self.v_norm.set("Same loudness as dry")     # older projects: that was just the default
            pc = st.get("pedal_corr")
            self.pedal_corr = (tuple(pc[0]), tuple(pc[1])) if pc else None
            self.pedal_corr_rounds = int(st.get("pedal_corr_rounds", 0) or 0)
            if st.get("ir_src") in ("mix", "A", "B"):
                self.v_ir_src.set(st["ir_src"])
                self._ir_src_pref = st["ir_src"]
            self._mix_changed("low" if not self.v_mix_link.get() else "mid")
            accs = [self.acc[s] for s in SLOTS if self.acc[s] is not None]
            self.demo = None
            if "demo_meta" in d:
                dm = json.loads(str(d["demo_meta"]))
                self._set_demo({r: d[f"demo_{r}"].astype(np.float32) / 32767 for r in dm["roles"]},
                               dm["fs"], dm.get("two", {}), save=False)
            elif hasattr(self, "aud_status"):
                self._refresh_audition_names()
            self.irm, self.master_src = None, None
            tone = str(st.get("master_src") or "").startswith("tone match")
            if accs:
                self.fs.set(accs[0].fs)
            if "master_h" in d and (tone or not accs):   # loaded IR WAV / tone-matched IR
                h, fs = d["master_h"], int(d["master_fs"])
                n = int(2 ** int(np.ceil(np.log2(max(65536, 2 * len(h))))))
                self.irm = {"h": h, "fs": fs, "P": np.abs(np.fft.rfft(h, n)) ** 2,
                               "freqs": np.fft.rfftfreq(n, 1 / fs), "nfft": n, "sign": 1.0,
                               "phase": "from file", "removed_predelay_ms": 0.0}
                self.master_src = st.get("master_src") or os.path.basename(p)
        except Exception as exc:
            log.exception("open project failed")
            messagebox.showerror(APP_NAME, f"Couldn't open project: {exc}")
            return False
        self.take_stack = []
        self.goto_step(int(st.get("step", 0)))
        self._update_coverage()
        names = set(st.get("selected", []))
        self._suppress_preview = True            # don't auto-run fits/graphs while opening
        try:
            self.ped_tree.selection_set([str(i) for i, pd in enumerate(self.pedals) if pd["name"] in names])
            if not self.ped_tree.selection():
                self.ped_tree.selection_set("0")
                self.ped_tree.focus("0")
            if accs and not tone:
                try:
                    self.build()
                except Exception:
                    log.exception("build during open failed")
            elif self.irm is not None:
                self._plot_master()
        finally:
            self.after(300, lambda: setattr(self, "_suppress_preview", False))
        self.status.set(f"Opened {os.path.basename(p)} - change settings, add takes, or export "
                        "for more pedals.")
        return True

    def open_ir_wav(self):
        p = filedialog.askopenfilename(title="Open an IR WAV (ideally a long, full-length one)",
                                       filetypes=[("WAV", "*.wav"), ("All", "*.*")])
        if not p:
            return
        if self._has_takes() and not messagebox.askyesno(
                APP_NAME, "Use this WAV instead of the IR built from your takes? "
                "(Your takes are kept - press Build IR to go back to them.)"):
            return
        try:
            self.irm = E.master_from_wav(p)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't read IR: {exc}")
            return
        self.master_src = os.path.basename(p)
        self.fits = {}
        for iid in self.ped_tree.get_children():
            self.ped_tree.set(iid, "err", "")
        name = os.path.splitext(os.path.basename(p))[0]
        self.v_name.set(name.split("_FULL")[0])
        self.nb.select(self.tab_build)
        self._plot_master()
        self.fit_info.configure(text=(
            f"Loaded {self.master_src}: {len(self.irm['h'])} samples @ {self.irm['fs']} Hz "
            f"({len(self.irm['h']) / self.irm['fs'] * 1000:.0f} ms). Select targets and "
            "export. Start from a full-length IR for the best results on every pedal."))

    def import_wavs(self):
        """Add a take from WAV files recorded elsewhere (e.g. Reaper)."""
        import soundfile as sf
        p = filedialog.askopenfilename(title="Piezo WAV (or a stereo WAV: piezo L, mic R)",
                                       filetypes=[("WAV", "*.wav"), ("All", "*.*")])
        if not p:
            return
        try:
            a, fs = sf.read(p, always_2d=True)
            if a.shape[1] >= 2:
                x, y = a[:, 0], a[:, 1]
            else:
                q = filedialog.askopenfilename(title="Mic WAV (same take)",
                                               filetypes=[("WAV", "*.wav"), ("All", "*.*")])
                if not q:
                    return
                b, fs2 = sf.read(q, always_2d=True)
                if fs2 != fs:
                    raise ValueError("Piezo and mic files have different sample rates.")
                x, y = a[:, 0], b[:, 0]
            if fs != self.fs.get():
                if any(self.acc[s] is not None and not self.acc[s].empty() for s in SLOTS):
                    raise ValueError(f"File is {fs} Hz but the session is {self.fs.get()} Hz.")
                self.fs.set(fs)
            two = messagebox.askyesno(APP_NAME, "Were the piezo and mic recorded on two different "
                                      "devices (e.g. a USB mic)?")
            self._process_async(np.asarray(x, float), np.asarray(y, float),
                                "import " + os.path.basename(p), two)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _state_path(self, folder=None):
        return os.path.join(folder or AUTOSAVE_DIR, "state.json")

    def _read_state(self, folder=None):
        try:
            with open(self._state_path(folder)) as fh:
                return json.load(fh)
        except Exception:
            log.debug("_read_state: ignored error", exc_info=True)
            return None

    def _write_state(self, **kw):
        st = self._read_state() or {}
        st.update(kw)
        try:
            write_json(self._state_path(), st)
        except OSError:
            log.exception("state write failed")

    def _begin_autosave_session(self):
        """First take of a new session: keep the old autosave as 'previous'."""
        if self.autosave_live:
            return
        try:
            if os.path.isdir(AUTOSAVE_DIR) and os.listdir(AUTOSAVE_DIR):
                if os.path.isdir(AUTOSAVE_PREV):
                    shutil.rmtree(AUTOSAVE_PREV, ignore_errors=True)
                os.replace(AUTOSAVE_DIR, AUTOSAVE_PREV)
            os.makedirs(os.path.join(AUTOSAVE_DIR, "takes"), exist_ok=True)
        except Exception:
            log.exception("autosave folder setup failed")
            os.makedirs(os.path.join(AUTOSAVE_DIR, "takes"), exist_ok=True)
        self.autosave_live = True
        self.take_counter = 0
        self._write_state(started=time.strftime("%Y-%m-%d %H:%M"), clean=False, takes=0,
                          instrument=self.instrument.get(), fs=int(self.fs.get()))
        log.info("autosave session started in %s", AUTOSAVE_DIR)

    def autosave(self, wait=False):
        """Save the session. The data is gathered here; compressing + writing happens on a
        background thread so the window never freezes (the demo is kept in demo.flac, not
        in the autosave project, for the same reason)."""
        if not self.autosave_live:
            return
        try:
            d = self._project_dict(include_demo=False)
            n = max((len(self.acc[s].takes) for s in SLOTS if self.acc[s] is not None), default=0)
        except Exception:
            log.exception("autosave failed")
            return
        if not hasattr(self, "_save_lock"):
            self._save_lock = threading.Lock()

        def work():
            with self._save_lock:
                try:
                    self._write_npz(os.path.join(AUTOSAVE_DIR, "session.irproject"), d)
                except Exception:
                    log.exception("autosave failed")
        t = self._background(work)
        if wait:
            t.join(timeout=60)
        self._write_state(updated=time.strftime("%Y-%m-%d %H:%M:%S"), takes=n, clean=False)

    def _save_raw_take(self, idx, label, x, mics, fs, extras=None):
        """Keep every recording (FLAC, lossless) so a session can be rebuilt from audio.
        Channel order: instrument, the mics in 'mics', then 'extras' (e.g. internal mic)."""
        extras = extras or {}
        try:
            import soundfile as sf
            chans = ([np.asarray(x, np.float32)] + [np.asarray(y, np.float32) for y, _ in mics.values()] +
                     [np.asarray(v, np.float32) for v in extras.values()])
            n = max(len(c) for c in chans)
            a = np.zeros((n, len(chans)), np.float32)
            for k, c in enumerate(chans):
                a[:len(c), k] = c
            base = os.path.join(AUTOSAVE_DIR, "takes", f"take_{idx:03d}")
            write_atomic(base + ".flac", lambda tmp: sf.write(tmp, np.clip(a, -1, 1), fs, subtype="PCM_24",
                                                               format="FLAC"))
            meta = {"label": label, "fs": fs, "mics": list(mics.keys()), "extras": list(extras.keys()),
                    "two_device": {s: bool(t) for s, (_, t) in mics.items()},
                    "lengths": [len(c) for c in chans], "time": time.strftime("%H:%M:%S")}
            write_json(base + ".json", meta)
        except Exception:
            log.exception("saving raw take failed")

    def _offer_restore(self):
        st = self._read_state()
        proj = os.path.join(AUTOSAVE_DIR, "session.irproject")
        if not st or not st.get("takes") or not os.path.exists(proj):
            return
        crashed = not st.get("clean", True)
        msg = ((f"{APP_NAME} didn't close properly last time.\n\n" if crashed else "") +
               f"Reopen your last session?\n\n{st.get('takes')} take(s), {st.get('instrument', '')}, "
               f"last saved {st.get('updated', st.get('started', '?'))}.\n\n"
               "Yes = carry on where you left off.\nNo = start a new session (the old one stays "
               "available under File > Open the session before that).")
        if messagebox.askyesno(APP_NAME, msg):
            self._reopen_autosave()

    def _reopen_autosave(self):
        if self.load_session(os.path.join(AUTOSAVE_DIR, "session.irproject")):
            if self.demo is None:
                self._load_demo(AUTOSAVE_DIR)
            self.autosave_live = True
            self.take_counter = len([f for f in os.listdir(os.path.join(AUTOSAVE_DIR, "takes"))
                                     if f.endswith(".json")]) if os.path.isdir(
                os.path.join(AUTOSAVE_DIR, "takes")) else 0
            self._write_state(clean=False)
            self.logln(f"Reopened the last session - carry on from step {min(self.step_i + 1, len(self.steps()))}. "
                       "New takes are added to it.")
            self.nb.select(self.tab_cap)

    def reopen_last(self):
        if not os.path.exists(os.path.join(AUTOSAVE_DIR, "session.irproject")):
            messagebox.showinfo(APP_NAME, "There's no autosaved session yet.")
            return
        if self.autosave_live:
            messagebox.showinfo(APP_NAME, "You're already in the last session - it's being "
                                "autosaved as you go.")
            return
        self._reopen_autosave()

    def reopen_previous(self):
        p = os.path.join(AUTOSAVE_PREV, "session.irproject")
        if not os.path.exists(p):
            messagebox.showinfo(APP_NAME, "There's no earlier session saved.")
            return
        if self.load_session(p):
            messagebox.showinfo(APP_NAME, "Opened the earlier session. Use File > Save project "
                                "to keep it - new takes start a fresh autosave.")

    def recover_from_recordings(self):
        folder = os.path.join(AUTOSAVE_DIR, "takes")
        metas = sorted(f for f in os.listdir(folder) if f.endswith(".json")) if os.path.isdir(folder) else []
        if not metas:
            messagebox.showinfo(APP_NAME, "No saved recordings found for the last session.")
            return
        if self.busy:
            messagebox.showinfo(APP_NAME, "Busy - try again in a moment.")
            return
        if not messagebox.askyesno(APP_NAME, f"Re-analyse all {len(metas)} saved recording(s) of the "
                                   "last session from scratch? This replaces the takes currently "
                                   "loaded."):
            return
        import soundfile as sf
        self.acc = {s: None for s in SLOTS}
        self.take_stack = []
        self.autosave_live = True
        self.busy = True
        self.status.set("Rebuilding from recordings...")
        items = []
        for m in metas:
            with open(os.path.join(folder, m)) as fh:
                meta = json.load(fh)
            if meta.get("fs") != int(self.fs.get()):
                self.fs.set(meta["fs"])
            items.append((m[:-5], meta))
        for _, meta in items:
            for s in meta["mics"]:
                self._get_acc(s)
        accs = dict(self.acc)

        def work():
            n = 0
            for base, meta in items:
                try:
                    a, fs = sf.read(os.path.join(folder, base + ".flac"), dtype="float32", always_2d=True)
                    L = meta["lengths"]
                    x = a[:L[0], 0].astype(np.float64)
                    ex = meta.get("extras", [])
                    x2 = None
                    if "piezo2" in ex:
                        c = 1 + len(meta["mics"]) + ex.index("piezo2")
                        x2 = a[:L[c], c].astype(np.float64)
                    done = []
                    for k, s in enumerate(meta["mics"]):
                        y = a[:L[k + 1], k + 1].astype(np.float64)
                        rep = accs[s].process_take(x, y, meta["label"], two_device=meta["two_device"][s],
                                                   x2=x2)
                        if rep.get("accepted"):
                            done.append(s)
                        self.q.put(("take", s, rep, False))
                    self.q.put(("take_done", done, False, "recover"))
                    n += 1
                except Exception as exc:
                    log.exception("recover %s", base)
                    self.q.put(("error", f"[{base}] {exc}", ""))
            self.q.put(("recovered", n))
        self._background(work)
