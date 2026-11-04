# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""The main window: menus, timer loop, About, shutdown, and main()."""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback

import numpy as np
import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk

from . import audio
from . import engine as E
from .audio import AudioRig, SD_ERROR
from .config import APP_NAME, AUTHOR, DATA_DIR, LINKS, LOG_DIR, RELEASES_URL, ROLES, SLOTS, SOURCE_URL, build_info, load_pedals, load_settings, log, save_settings, setup_logging
from .project import ProjectFiles
from .texts import TEXT_TIPS
from .ui_audition import AuditionPanel
from .ui_build import BuildTab
from .ui_capture import CaptureTab
from .ui_setup import SetupTab
from .ui_tone import ToneMatchTab
from .util import open_url
from .widgets import ScrollFrame, Tooltip


class App(SetupTab, CaptureTab, BuildTab, AuditionPanel, ProjectFiles, ToneMatchTab, tk.Tk):
    def __init__(self):
        super().__init__()
        self.build_meta = build_info()
        self.title(f"{APP_NAME}  {self.build_meta['version']}" +
                   (f"   (built {self.build_meta['date']})" if self.build_meta["release"] else ""))
        log.info("version %s, built %s, commit %s", self.build_meta["version"], self.build_meta["date"],
                 self.build_meta["commit"])
        try:                                   # use more of the screen when there is room
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except Exception:
            log.debug("__init__: ignored error", exc_info=True)
            sw, sh = 1280, 820
        self.compact = sh < 900                 # 13-14" laptops (or big Windows scaling)
        if sw < 1400 or sh < 900:
            self.geometry(f"{max(900, sw - 20)}x{max(560, sh - 80)}+0+0")
            try:
                self.state("zoomed")            # Windows: use the whole screen
            except tk.TclError:
                pass
        else:
            self.geometry(f"{min(1560, sw - 60)}x{min(1000, sh - 90)}+20+10")
        self.minsize(900, 560)
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Big.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Step.TLabel", font=("Segoe UI", 11))
        style.configure("Rec.TButton", font=("Segoe UI", 11, "bold"))
        style.configure("Transport.TButton", font=("Segoe UI", 12, "bold"), padding=(6, 6))

        self.rig = AudioRig()
        self.fs = tk.IntVar(value=48000)
        self.acc = {s: None for s in SLOTS}
        self.cal = {s: None for s in SLOTS}
        self.cal_name = {s: tk.StringVar(value="(none)") for s in SLOTS}
        self.slot = tk.StringVar(value="A")
        self.last_piezo = None
        self.demo = None
        self.dual_pair = None
        self.project_path = None        # the .irproject file; its IRs export to '<name> IRs' beside it
        self.default_export_dir = load_settings().get("export_dir", "")
        self.backup_dir = load_settings().get("backup_dir", "")
        self.take_stack = []
        self.autosave_live = False      # this session is being written to the autosave folder
        self.take_counter = 0
        self._err_shown = 0.0
        self.irm = None
        self.fits = {}
        self.q = queue.Queue()
        self.busy = False
        self.step_i = 0
        self.rec_started = None
        self.rec_limit = None
        self.rec_label = ""
        self.pedals = load_pedals()
        self.instrument = tk.StringVar(value="acoustic guitar")
        self.master_src = None          # None | "built" | path of loaded WAV

        self._build_menu()
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.tab_setup = ttk.Frame(nb)
        self.tab_cap = ttk.Frame(nb)
        self.tab_build = ttk.Frame(nb)
        nb.add(self.tab_setup, text="  1  Setup  ")
        nb.add(self.tab_cap, text="  2  Capture  ")
        nb.add(self.tab_build, text="  3  Build & Export  ")
        self.tab_tone = ttk.Frame(nb)
        nb.add(self.tab_tone, text="  4  Tone Match  ")
        self.nb = nb
        self._build_setup()
        self._build_capture()
        self._build_build()
        self._build_tone()
        self._auto_tips(self)
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8, pady=(0, 4))
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self._after_id = self.after(60, self._tick)
        if audio.sd is None:
            messagebox.showerror(APP_NAME, "Audio library failed to load:\n" + SD_ERROR)

    def _scroll_body(self, tab):
        sf = ScrollFrame(tab)
        sf.pack(fill="both", expand=True)
        return sf.body

    def _link(self, parent, text, url, **kw):
        """A clickable web link."""
        font = kw.pop("font", ("Segoe UI", 9))
        lab = tk.Label(parent, text=text, fg="#1a5fb4", cursor="hand2", font=font + ("underline",), **kw)
        lab.bind("<Button-1>", lambda e: open_url(url))
        self._tip(lab, url)
        return lab

    def _tip(self, widget, text):
        """Attach hover help to a widget (kept so it isn't garbage-collected)."""
        self._tips = getattr(self, "_tips", [])
        self._tips.append(Tooltip(widget, text))
        return widget

    def _auto_tips(self, w):
        """Hover help for Setup / Capture controls, matched by their text."""
        try:
            txt = str(w.cget("text"))
        except Exception:
            log.debug("_auto_tips: ignored error", exc_info=True)
            txt = ""
        tip = TEXT_TIPS.get(txt.strip())
        if tip and not getattr(w, "_has_tip", False):
            self._tip(w, tip)
        for ch in w.winfo_children():
            self._auto_tips(ch)

    def _build_menu(self):
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Save project...", command=self.save_session)
        f.add_command(label="Open project...", command=self.load_session)
        f.add_separator()
        f.add_command(label="Reopen last session (autosaved)", command=self.reopen_last)
        f.add_command(label="Open the session before that...", command=self.reopen_previous)
        f.add_command(label="Rebuild last session from its recordings...",
                      command=self.recover_from_recordings)
        f.add_separator()
        f.add_command(label="Open an IR WAV to re-fit for pedals...", command=self.open_ir_wav)
        f.add_command(label="Import recorded WAVs as a take...", command=self.import_wavs)
        f.add_separator()
        f.add_command(label="Exit", command=self._quit)
        m.add_cascade(label="File", menu=f)
        h = tk.Menu(m, tearoff=0)
        h.add_command(label="How to get the best IR", command=self.show_tips)
        h.add_command(label="Open logs folder (send app.log if something goes wrong)",
                      command=lambda: self._open_folder(LOG_DIR))
        h.add_separator()
        h.add_command(label=f"About {APP_NAME}...", command=self.show_about)
        m.add_cascade(label="Help", menu=h)
        st = tk.Menu(m, tearoff=0)
        st.add_command(label="Set projects folder (projects + their IRs)...", command=self.set_export_folder)
        st.add_command(label="Open projects folder", command=self._open_export_folder)
        st.add_separator()
        st.add_command(label="Set backup folder (gets a copy of every export)...", command=self.set_backup_folder)
        st.add_command(label="Stop copying to a backup folder", command=lambda: (
            setattr(self, "backup_dir", ""), save_settings(backup_dir=""),
            self.status.set("Exports are no longer copied to a backup folder.")))
        m.add_cascade(label="Settings", menu=st)
        self.config(menu=m)

    def show_about(self):
        b = self.build_meta
        w = tk.Toplevel(self)
        w.title(f"About {APP_NAME}")
        w.transient(self)
        w.resizable(False, False)
        ttk.Label(w, text=APP_NAME, font=("Segoe UI", 15, "bold")).pack(padx=24, pady=(16, 2))
        ttk.Label(w, text=f"Version {b['version']}", font=("Segoe UI", 12)).pack()
        rows = [("Built", b["date"]), ("Code version (commit)", b["commit"]),
                ("Python", sys.version.split()[0]), ("Your data folder", DATA_DIR)]
        g = ttk.Frame(w)
        g.pack(padx=24, pady=10)
        for i, (k, v) in enumerate(rows):
            ttk.Label(g, text=k + ":", foreground="#555").grid(row=i, column=0, sticky="e", padx=(0, 8))
            ttk.Label(g, text=v).grid(row=i, column=1, sticky="w")
        ttk.Label(w, text=f"Created by {AUTHOR}", font=("Segoe UI", 10, "bold")).pack(pady=(4, 2))
        lf = ttk.Frame(w)
        lf.pack()
        for key in ("website", "instagram", "youtube"):
            self._link(lf, *LINKS[key]).pack(anchor="center")
        vf = ttk.Frame(w)
        vf.pack(pady=(8, 0))
        ttk.Label(vf, text="Thank you to ").pack(side="left")
        self._link(vf, *LINKS["vic"]).pack(side="left")
        ttk.Label(vf, text=" for the inspiration.").pack(side="left")
        ttk.Label(w, foreground="#555", wraplength=420, justify="center", text=(
            "Free, open-source software under the GNU GPL v3.0 - you may use, study, change and share "
            "it under its terms. It comes with ABSOLUTELY NO WARRANTY.")).pack(padx=24, pady=(10, 2))
        self._link(w, "Source code and new versions on GitHub", SOURCE_URL).pack()
        ttk.Label(w, foreground="#555", wraplength=420, justify="center", text=(
            "Each new release counts the version up by one. To update, download the newest "
            "zip and unzip it over this folder - your sessions and settings are kept.")).pack(padx=24, pady=(8, 0))
        bf = ttk.Frame(w)
        bf.pack(pady=12)
        ttk.Button(bf, text="Download page", command=lambda: open_url(RELEASES_URL)).pack(side="left", padx=4)
        ttk.Button(bf, text="Close", command=w.destroy).pack(side="left", padx=4)

    def set_export_folder(self):
        d = filedialog.askdirectory(title="Projects folder - new projects (and their IRs) are saved here; "
                                          "e.g. a folder inside Google Drive", initialdir=self.projects_dir())
        if d:
            self.default_export_dir = d
            save_settings(export_dir=d)
            self.status.set(f"New projects will be saved in: {d}")

    def projects_dir(self):
        """Where projects are saved by default: your choice (Settings), else
        Documents/Acoustic IR Studio Projects."""
        if self.default_export_dir and os.path.isdir(self.default_export_dir):
            return self.default_export_dir
        d = os.path.join(os.path.expanduser("~"), "Documents", "Acoustic IR Studio Projects")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            return os.path.expanduser("~")

    def set_backup_folder(self):
        d = filedialog.askdirectory(title="Backup folder - every export is also copied here (e.g. Google "
                                          "Drive / Dropbox / OneDrive / a USB stick)",
                                    initialdir=self.backup_dir or None)
        if d:
            self.backup_dir = d
            save_settings(backup_dir=d)
            self.status.set(f"Every export will also be copied to: {d}")

    def _open_export_folder(self):
        self._open_folder(self.projects_dir())

    def _open_folder(self, path):
        os.makedirs(path, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                import subprocess
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except Exception:
            log.debug("couldn't open %s", path, exc_info=True)
            messagebox.showinfo(APP_NAME, path)

    def report_callback_exception(self, exc, val, tb):
        """Any error in a button/handler: log it, tell the user, keep running."""
        log.error("Error in UI callback:\n%s", "".join(traceback.format_exception(exc, val, tb)))
        try:
            self.autosave()
        except Exception:
            log.debug("report_callback_exception: ignored error", exc_info=True)
        if time.time() - self._err_shown > 2:
            self._err_shown = time.time()
            messagebox.showerror(APP_NAME, f"Something went wrong: {val}\n\nYour session is "
                                 "autosaved and the app is still running. Details were written to "
                                 "the log (Help > Open logs folder) - send me app.log.")

    def show_tips(self):
        messagebox.showinfo(APP_NAME, (
            "Getting the most accurate IR\n\n"
            "- Quiet room, fans/AC off. Fresh-ish strings.\n"
            "- Mic 20-40 cm from the guitar, aimed between the 12th fret and the soundhole "
            "(the classic spot), or wherever it sounds best to your ears.\n"
            "- Nothing moves during a session: mic stand, guitar, you (sit, and keep the "
            "guitar in the same place on your leg).\n"
            "- Levels: hardest strum peaking around -6 dBFS on both channels.\n"
            "- Do the whole guided session, then free takes until the bands you care about are green.\n"
            "- Reference Mic: if it's a measurement mic, load its calibration file. A Color Mic "
            "(any mic you like the sound of) goes right next to it; mix the two by frequency on the "
            "Build tab.\n"
            "- Record a short demo (Capture tab) so you can audition every option on the Build tab.\n"
            "- Save the session (File menu) - you can come back and add takes later."))

    def _tick(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "take":
                    self._report_take(msg[1], msg[2], msg[3])
                elif msg[0] == "take_done" and len(msg) > 3:     # during a rebuild
                    if msg[1]:
                        self.take_stack.append(msg[1])
                elif msg[0] == "take_done":
                    self.busy = False
                    self.status.set("Ready - session autosaved.")
                    if msg[1]:
                        self.take_stack.append(msg[1])
                        if msg[2]:
                            self.goto_step(self.step_i + 1)
                    self.autosave()
                elif msg[0] == "recovered":
                    self.busy = False
                    self.irm = None
                    self._update_coverage()
                    self.autosave()
                    self.status.set(f"Rebuilt the session from {msg[1]} recording(s).")
                    messagebox.showinfo(APP_NAME, f"Rebuilt the session from {msg[1]} saved "
                                        "recording(s). Go to Build & Export when ready.")
                elif msg[0] == "error":
                    self.logln("ERROR " + msg[1])
                    self.status.set("Take failed - see log.")
        except queue.Empty:
            pass
        if self.rig.running():
            pk = self.rig.read_peaks()
            on_cap = self.nb.select() == str(self.tab_cap)
            if on_cap and time.time() - self._spec_t > 0.2:
                self._spec_t = time.time()
                self._spec_update()
            for role in (ROLES if on_cap else ()):
                c, lab = self.meter[role]
                if role not in pk:
                    continue
                self._draw_meter(c, lab, pk[role], self.rig.hold.get(role, 0.0))
        if self.rec_started is not None:
            el = time.time() - self.rec_started
            if self.rec_limit:
                left = self.rec_limit - el
                self.rec_time.configure(text=f"REC  {self.rec_label}   {max(left, 0):4.0f} s left")
                if left <= 0:
                    self.stop_record()
            else:
                self.rec_time.configure(text=f"REC  {self.rec_label}   {int(el // 60)}:{int(el % 60):02d}"
                                             "   - press Stop when done")
                if el > 30 * 60:          # safety stop after 30 minutes
                    self.stop_record()
        self._after_id = self.after(60, self._tick)

    def _background(self, fn):
        """Run fn on a worker thread that shutdown waits for (so files are never cut off)."""
        t = threading.Thread(target=fn, daemon=True)
        self._bg_threads = [x for x in getattr(self, "_bg_threads", []) if x.is_alive()] + [t]
        t.start()
        return t

    def _finish_background(self, timeout=60):
        """Before closing: let a take finish analysing/saving, and any background save finish."""
        t0 = time.time()
        while self.busy and time.time() - t0 < timeout:
            self.update()
            time.sleep(0.05)
        for t in getattr(self, "_bg_threads", []):
            t.join(timeout=max(0.1, timeout - (time.time() - t0)))

    def _quit(self):
        if self.rig.recording:
            if not messagebox.askyesno(APP_NAME, "You're recording - stop and keep this take before "
                                       "closing?"):
                pass
            else:
                self.stop_record()
                t0 = time.time()
                while self.busy and time.time() - t0 < 60:
                    self.update()
                    time.sleep(0.05)
        self._finish_background()
        try:
            self.autosave(wait=True)
            if self.autosave_live:
                self._write_state(clean=True)
            log.info("clean exit")
        except Exception:
            log.debug("_quit: ignored error", exc_info=True)
        try:
            self.after_cancel(self._after_id)
        except Exception:
            log.debug("_quit: ignored error", exc_info=True)
        try:
            if self._aud_tick_job is not None:
                self.after_cancel(self._aud_tick_job)
            self.player.stop()
        except Exception:
            log.debug("_quit: ignored error", exc_info=True)
        try:
            self.rig.stop()
            if audio.sd:
                audio.sd.stop()
        finally:
            self.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            log.debug("main: couldn't set DPI awareness", exc_info=True)
    setup_logging()
    try:
        app = App()
    except Exception:
        log.exception("startup failed")
        raise
    if "--selftest" in sys.argv:
        # used by the automatic build: open every tab, run the engine once, close
        try:
            for tab in (app.tab_setup, app.tab_cap, app.tab_build, app.tab_tone):
                app.nb.select(tab)
                app.update()
            fs = 48000
            rng = np.random.default_rng(0)
            x = rng.standard_normal(10 * fs) * 0.05
            y = np.convolve(x, np.r_[np.zeros(60), 1.0, -0.4, 0.2])[:len(x)]
            acc = E.Accumulator(fs)
            acc.process_take(x, y, "selftest")
            m = E.build_master(acc, None, E.BuildParams())
            E.fit_for_pedal(m, 44100, 1024)
            assert app.ped_tree._root() is app
            log.info("selftest OK")
            app.destroy()
            sys.exit(0)
        except SystemExit:
            raise
        except Exception:
            log.exception("selftest failed")
            sys.exit(1)
    app.after(500, app._offer_restore)
    app.mainloop()
