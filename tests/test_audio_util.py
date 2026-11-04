# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""Audio rig, audition player, settings and small helpers (no window needed)."""

import json
import os
import time

import numpy as np

from irstudio import config, util
from irstudio.audio import AudioRig, AuditionPlayer


# ---------------------------------------------------------------------------- audition player
def test_player_loops_the_whole_buffer():
    p = AuditionPlayer(None)
    y = np.arange(1000, dtype=np.float32) / 1000
    p.set_buffer(y, 48000)
    p.playing = True
    out = np.concatenate([p.next(300) for _ in range(5)])   # 1500 samples from a 1000 buffer
    assert np.allclose(out[:1000], y) and np.allclose(out[1000:], y[:500])


def test_player_loops_a_region_and_keeps_position_on_switch():
    p = AuditionPlayer(None)
    p.set_buffer(np.zeros(48000, np.float32), 48000)
    p.loop = (1000, 2000)
    p.pos = 1900
    p.playing = True
    p.next(300)
    assert 1000 <= p.pos < 2000                               # wrapped inside the loop
    pos = p.pos
    p.set_buffer(np.ones(48000, np.float32), 48000)           # switching option mid-play
    assert p.pos == pos


def test_player_plays_once_when_not_looping():
    p = AuditionPlayer(None)
    p.set_buffer(np.ones(500, np.float32), 48000)
    p.looping = False
    p.playing = True
    out = p.next(800)
    assert out[:500].sum() == 500 and out[500:].sum() == 0 and not p.playing


def test_player_volume_never_clips():
    p = AuditionPlayer(None)
    p.set_buffer(np.full(1000, 0.9, np.float32), 48000)
    p.gain = 4.0
    p.playing = True
    assert np.max(np.abs(p.next(500))) <= 1.0


# ---------------------------------------------------------------------------- recording rig
def test_rig_records_every_input_from_two_devices(fake_sd):
    rig = AudioRig()
    fake_sd.is_recording = lambda: rig.recording
    n = 48000
    iface = np.zeros((n, 4), np.float32)
    iface[:, 0], iface[:, 1] = 0.1, 0.2
    usb = np.full((n, 2), 0.3, np.float32)
    fake_sd.feed(iface, usb)
    rig.start({"piezo": (0, 0), "B": (0, 1), "A": (2, 0)}, 48000)
    try:
        rig.begin()
        t0 = time.time()
        while fake_sd.sent < 2 and time.time() - t0 < 10:
            time.sleep(0.02)
        data = rig.end()
    finally:
        rig.stop()
    assert set(data) == {"piezo", "A", "B"}
    assert np.allclose(data["piezo"][:1000], 0.1) and np.allclose(data["B"][:1000], 0.2)
    assert np.allclose(data["A"][:1000], 0.3)
    x = rig.recent("piezo", 4096)
    assert x is None or len(x) == 4096                        # live-spectrum buffer


# ---------------------------------------------------------------------------- helpers
def test_device_names():
    assert util.short_dev("Microphone (2- My Interface)", 1) == "My Interface In 2"
    assert util.dev_family("Analogue 3 + 4 (2- My Interface)") == util.dev_family("Analogue 1 + 2 (My Interface)")


def test_file_names_say_pedal_and_length():
    assert util.file_tag("Short - Smart Box & other short loaders") == "Short-Smart_Box_other_short_loaders"
    assert util.file_tag("Ultimate - Daisy Seed (1 s)") == "Ultimate-Daisy_Seed"


def test_decimate_keeps_the_peaks():
    x = np.arange(100000)
    y = np.sin(x / 50.0)
    y[54321] = 5.0
    xd, yd = util.decimate_line(x, y, 2000)
    assert len(yd) <= 2100 and yd.max() == 5.0


def test_settings_round_trip(data_dir):
    config.save_settings(export_dir="X", backup_dir="Y")
    config.save_settings(export_dir="Z")
    assert config.load_settings() == {"export_dir": "Z", "backup_dir": "Y"}


def test_custom_targets_are_kept_and_presets_are_not_saved(data_dir):
    pedals = config.load_pedals()
    assert [p["name"].split(" -")[0] for p in pedals] == ["Short", "Medium", "Long", "Ultimate"]
    config.save_pedals(pedals + [{"name": "Mine", "fs": 48000, "samples": 4096}])
    saved = json.load(open(config.CUSTOM_TARGETS_FILE))["targets"]
    assert saved == [{"name": "Mine", "fs": 48000, "samples": 4096}]
    assert config.load_pedals()[-1]["name"] == "Mine"


def test_version_info_without_a_release_stamp():
    b = config.build_info()
    assert set(b) == {"version", "date", "commit", "release"}


def test_data_folder_falls_back_when_the_app_folder_is_read_only(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HERE", str(tmp_path / "does-not-exist" / "x"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(config.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    d = config._data_dir()
    assert os.path.isdir(d) and str(tmp_path) in d
