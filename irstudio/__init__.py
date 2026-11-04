# Acoustic IR Studio - Copyright (C) 2026 Paul McEvoy (paulmcevoyguitars.com)
# Free software under the GNU General Public License v3.0 or later - see LICENSE.
# This program comes with ABSOLUTELY NO WARRANTY.
"""Acoustic IR Studio: piezo -> mic impulse responses for acoustic guitar.

Layout:
  engine, tone_match   - the signal processing (no UI; fully tested)
  audio                - recording rig and audition player
  config, util, texts  - settings/paths, helpers, help texts
  widgets              - generic Tk widgets
  app                  - the main window, built from the ui_* tabs and project
"""

__version__ = "1.0"
