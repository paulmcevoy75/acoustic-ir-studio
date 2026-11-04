# Contributing

Bug reports, ideas and pull requests are welcome. For a bug, please attach `app.log` (in the app: **Help > Open logs folder**) and say what you did.

## Setup

```
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python acoustic_ir_studio.py      # run the app
pytest                            # run the tests (Linux: xvfb-run -a pytest)
ruff check irstudio tests         # lint
```

## How the code is laid out

| Module | What's in it |
|---|---|
| `irstudio/engine.py` | the IR maths: cross-spectra, clock sync between devices, building and fitting IRs, dual-piezo pairs. No UI. |
| `irstudio/tone_match.py` | Tone Match: pitch detection, harmonic matching. No UI. |
| `irstudio/audio.py` | recording rig (inputs, meters, headphone monitor) and the audition player |
| `irstudio/config.py` | names, data folders, settings files, logging, pedal presets |
| `irstudio/util.py`, `texts.py`, `widgets.py` | helpers, hover-help texts, tooltip / scrolling widgets |
| `irstudio/ui_setup.py`, `ui_capture.py`, `ui_build.py`, `ui_audition.py`, `ui_tone.py` | one class per tab, mixed into the main window |
| `irstudio/project.py` | project files, autosave, crash recovery |
| `irstudio/app.py` | the main window (`App`), menus, timer loop, shutdown, `main()` |

The engine modules have no Tk dependency, so they can be reused (e.g. for batch processing).

## Rules of thumb

- Keep signal-processing changes in `engine.py` and back them with a test against a known answer (see `tests/test_engine.py`).
- Never let an error pass silently. Log it (`log.debug(..., exc_info=True)` at least), and tell the user when it affects them.
- Write files through `config.write_atomic` / `write_json` (or `engine._atomic`), so a crash can't leave half a file behind.
- Run long work with `self._background(fn)`. Closing the app waits for it.
- Tests use the fake sound card in `tests/conftest.py`. New UI behaviour should get a test in `tests/test_app.py`.

## Releases

Every push to `main` that passes the tests on Linux and Windows builds the app and publishes it as a numbered release (`v1.<build number>`). Earlier releases stay available. Exact library versions for the release build are in `requirements-lock.txt`.

By contributing you agree that your contribution is licensed under the GPL-3.0 (see `LICENSE`).
