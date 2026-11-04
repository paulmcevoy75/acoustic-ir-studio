# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""
Windows input-level control for capture devices (e.g. a USB measurement mic),
via the Windows Core Audio API (pycaw). Everything here fails quietly: if pycaw
isn't installed or a device doesn't expose a level control, the app just
offers the 'Windows sound settings' button instead.
"""

from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger("acoustic_ir")
_cache = {}


def available():
    if sys.platform != "win32":
        return False
    try:
        import pycaw.pycaw  # noqa: F401
        return True
    except Exception:
        log.debug("available: ignored error", exc_info=True)
        return False


def _norm(s):
    return " ".join(str(s).lower().replace("(", " ").replace(")", " ").split())


def _endpoint(device_name):
    """IAudioEndpointVolume for the capture endpoint whose name matches the
    PortAudio device name (WASAPI names are the Windows endpoint names)."""
    if device_name in _cache:
        return _cache[device_name]
    vol = None
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        want = _norm(device_name)
        best = None
        for d in AudioUtilities.GetAllDevices():
            nm = _norm(getattr(d, "FriendlyName", "") or "")
            if not nm:
                continue
            if nm == want or want.startswith(nm) or nm.startswith(want):
                best = d
                break
            if best is None and (want[:24] in nm or nm[:24] in want):
                best = d
        if best is not None:
            enum = AudioUtilities.GetDeviceEnumerator()
            dev = enum.GetDevice(best.id)
            iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol = cast(iface, POINTER(IAudioEndpointVolume))
            vol.GetMasterVolumeLevelScalar()          # test it works
    except Exception:
        log.info("no Windows level control for %r", device_name, exc_info=True)
        vol = None
    _cache[device_name] = vol
    return vol


def get_level(device_name):
    """0..100 or None."""
    v = _endpoint(device_name)
    if v is None:
        return None
    try:
        return float(v.GetMasterVolumeLevelScalar()) * 100.0
    except Exception:
        log.debug("get_level: ignored error", exc_info=True)
        return None


def set_level(device_name, pct):
    v = _endpoint(device_name)
    if v is None:
        return False
    try:
        v.SetMasterVolumeLevelScalar(max(0.0, min(1.0, pct / 100.0)), None)
        return True
    except Exception:
        log.info("setting level failed for %r", device_name, exc_info=True)
        return False


def open_recording_settings():
    """Opens Windows' classic Sound > Recording dialog (Properties > Levels)."""
    try:
        subprocess.Popen(["control", "mmsys.cpl,,1"])
        return True
    except Exception:
        log.debug("open_recording_settings: ignored error", exc_info=True)
        try:
            import os
            os.startfile("ms-settings:sound")
            return True
        except Exception:
            log.debug("open_recording_settings: ignored error", exc_info=True)
            return False
