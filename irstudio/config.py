# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.

"""Names, paths, settings files, logging and the built-in pedal targets."""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import sys
import threading
import time
import traceback


import matplotlib

log = logging.getLogger("acoustic_ir")
matplotlib.rcParams["agg.path.chunksize"] = 10000    # long, dense lines can't overflow the renderer
matplotlib.rcParams["path.simplify"] = True

APP_NAME = "Acoustic IR Studio"


APP_MAJOR = 1                 # the version shown is <major>.<build number>; every release build counts up


RELEASES_URL = "https://github.com/paulmcevoy75/acoustic-ir-studio/releases/latest"


SOURCE_URL = "https://github.com/paulmcevoy75/acoustic-ir-studio"


AUTHOR = "Paul McEvoy"


LINKS = {"website": ("paulmcevoyguitars.com", "https://paulmcevoyguitars.com"),
         "instagram": ("Instagram @paulmcevoyguitars", "https://www.instagram.com/paulmcevoyguitars/"),
         "youtube": ("YouTube @paulmcevoyguitars", "https://www.youtube.com/@paulmcevoyguitars"),
         "vic": ("Vic Wong", "https://www.paniquejazz.com/")}


def build_info():
    """Version + build date, stamped into build_info.json by the automatic Windows build.
    Running from the source code there's no stamp, so it says so."""
    try:
        with open(os.path.join(HERE, "build_info.json"), encoding="utf-8-sig") as fh:
            bi = json.load(fh)
        date = bi.get("date", "?")
        try:                                   # stamped in UTC; show it in this PC's time zone
            import datetime as _dt
            t = _dt.datetime.strptime(date.replace(" UTC", ""), "%Y-%m-%d %H:%M").replace(
                tzinfo=_dt.timezone.utc).astimezone()
            date = f"{t.day} {t.strftime('%b %Y, %H:%M')}"
        except ValueError:
            log.debug("build_info: ignored error", exc_info=True)
        return {"version": str(bi.get("version") or f"{APP_MAJOR}.x"), "date": date,
                "commit": bi.get("commit", "?"), "release": True}
    except (OSError, ValueError):
        log.debug("build_info: ignored error", exc_info=True)
        return {"version": f"{APP_MAJOR}.x (development copy)", "date": "not a release build",
                "commit": "-", "release": False}


# the app folder: next to the .exe when packaged, else the repository folder (above this package)
HERE = (os.path.dirname(os.path.abspath(sys.argv[0])) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


SLOTS = ("A", "B")


SLOT_LABEL = {"A": "Reference Mic", "B": "Color Mic"}


# instrument, optional 2nd piezo element, reference mic, color mic, internal mic
ROLES = ("piezo", "piezo2", "A", "B", "I")


ROLE_BASE = {"piezo": "Instrument", "piezo2": "Instrument 2", "A": "Reference Mic", "B": "Color Mic",
             "I": "Internal Mic"}


def _data_dir():
    """Folder for logs, autosave and device memory: next to the app if writable,
    otherwise %LOCALAPPDATA%\\AcousticIRStudio. On a Mac, always
    ~/Library/Application Support/AcousticIRStudio (an app bundle must not be written to)."""
    if sys.platform == "darwin":
        d = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "AcousticIRStudio")
        os.makedirs(d, exist_ok=True)
        return d
    try:
        test = os.path.join(HERE, ".write_test")
        with open(test, "w") as fh:
            fh.write("ok")
        os.remove(test)
        return HERE
    except OSError:
        log.debug("_data_dir: ignored error", exc_info=True)
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "AcousticIRStudio")
        os.makedirs(d, exist_ok=True)
        return d


DATA_DIR = _data_dir()


LOG_DIR = os.path.join(DATA_DIR, "logs")


AUTOSAVE_DIR = os.path.join(DATA_DIR, "autosave")


AUTOSAVE_PREV = os.path.join(DATA_DIR, "autosave_previous")


STEPS_FILE = os.path.join(DATA_DIR, "step_lengths.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")


CUSTOM_TARGETS_FILE = os.path.join(DATA_DIR, "custom_targets.json")


IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
# the button that opens the computer's own input-level settings
LEVELS_BUTTON = "Windows input level..." if IS_WINDOWS else ("Mac sound settings..." if IS_MAC else
                                                            "System sound settings...")


def load_settings():
    try:
        with open(SETTINGS_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        log.debug("load_settings: ignored error", exc_info=True)
        return {}


def write_atomic(path, write):
    """Write a file so it's never left half-written: write(tmp_path) fills a temporary file
    next to it, which then replaces the real one in a single step."""
    root, ext = os.path.splitext(path)
    tmp = f"{root}.tmp{os.getpid()}{ext}"
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                log.debug("couldn't remove %s", tmp, exc_info=True)


def write_json(path, obj):
    def w(tmp):
        with open(tmp, "w") as fh:
            json.dump(obj, fh, indent=2)
    write_atomic(path, w)


def save_settings(**kw):
    st = load_settings()
    st.update(kw)
    try:
        write_json(SETTINGS_FILE, st)
    except OSError:
        log.exception("couldn't save settings")




def setup_logging():
    """app.log gets every error with full details; crash.log catches hard crashes
    (the kind that close the window instantly)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    try:   # keep the log from growing forever
        p = os.path.join(LOG_DIR, "app.log")
        if os.path.exists(p) and os.path.getsize(p) > 5e6:
            os.replace(p, p + ".old")
    except Exception:
        log.debug("setup_logging: ignored error", exc_info=True)
    logging.basicConfig(filename=os.path.join(LOG_DIR, "app.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    try:
        setup_logging.crash_fh = open(os.path.join(LOG_DIR, "crash.log"), "a")
        setup_logging.crash_fh.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        setup_logging.crash_fh.flush()
        faulthandler.enable(setup_logging.crash_fh, all_threads=True)
    except Exception:
        log.debug("setup_logging: ignored error", exc_info=True)

    def hook(t, v, tb):
        log.error("Unhandled error:\n%s", "".join(traceback.format_exception(t, v, tb)))
    sys.excepthook = hook
    threading.excepthook = lambda a: log.error(
        "Unhandled error in thread %s:\n%s", a.thread.name if a.thread else "?",
        "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    log.info("%s starting (python %s, data folder %s)", APP_NAME, sys.version.split()[0], DATA_DIR)


MEAS_MIC_HINTS = ("measurement", "usb", "umik", "umm-6", "umm6", "emm-6", "ecm8000", "minidsp",
                  "dayton", "xref", "imm-6", "usb")


SMOOTH_OPTS = {"None": 0.0, "1/48 oct": 1 / 48, "1/24 oct": 1 / 24,
               "1/12 oct (recommended)": 1 / 12, "1/6 oct": 1 / 6, "1/3 oct": 1 / 3,
               "Detailed lows (1/24 -> 1/6)": (1 / 24, 1 / 6),
               "Psychoacoustic (1/3 -> 1/6)": (1 / 3, 1 / 6)}


ESTIMATORS = {"H1 - recommended": "H1",
              "Hv - more of the mic's air/room": "Hv",
              "H2 - for a noisy pickup": "H2"}


STATUS_COL = {"good": "#2e9d5b", "ok": "#d6a419", "weak": "#e0662f", "need data": "#9aa0a6"}


PLOT_COL = {"A": "#1565c0", "B": "#ef6c00", "piezo": "#2e7d32", "ir": "#111111", "fit": "#d81b60"}


PRESET_TARGETS = [
    {"name": "Short - Smart Box & other short loaders", "fs": 44100, "samples": 1024, "preset": True,
     "tip": "1024 samples at 44.1 kHz (23 ms) - Sonicake Smart Box and similar small IR pedals "
            "(check your unit's manual for its exact limit)."},
    {"name": "Medium - TC Electronic Impulse", "fs": 48000, "samples": 9600, "preset": True,
     "tip": "200 ms at 48 kHz (9600 samples) - the TC Electronic Impulse IR Loader's maximum."},
    {"name": "Long - Daisy Seed (0.5 s)", "fs": 48000, "samples": 24000, "format": "daisy", "preset": True,
     "tip": "500 ms at 48 kHz - a basic Daisy Seed convolver; captures nearly all of a guitar body's ring. "
            "Exports .wav + .bin + C header."},
    {"name": "Ultimate - Daisy Seed (1 s)", "fs": 48000, "samples": 48000, "format": "daisy", "preset": True,
     "tip": "1 s at 48 kHz - the longest that's practical on a Daisy Seed (needs an efficient partitioned "
            "convolver). Exports .wav + .bin + C header."},
]


def load_pedals():
    """The four presets, then the user's own targets (kept in the data folder, so they survive
    updating the app)."""
    custom = []
    try:
        with open(CUSTOM_TARGETS_FILE, "r") as fh:
            custom = [p for p in json.load(fh)["targets"] if not p.get("preset")]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        log.exception("couldn't read custom targets")
    return [dict(p) for p in PRESET_TARGETS] + custom


def save_pedals(pedals):
    try:
        write_json(CUSTOM_TARGETS_FILE, {"targets": [p for p in pedals if not p.get("preset")]})
    except OSError:
        log.exception("couldn't save custom targets")
