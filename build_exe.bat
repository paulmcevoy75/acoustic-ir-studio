@echo off
rem Builds a standalone AcousticIRStudio.exe in dist\ the same way the automatic build does
rem (run "Run Acoustic IR Studio.bat" once first so the .venv exists).
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install -q -r requirements-lock.txt -r requirements-dev.txt
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onedir --windowed --name AcousticIRStudio ^
  --collect-all _sounddevice_data --collect-all _soundfile_data --collect-all imageio_ffmpeg ^
  --hidden-import pycaw --collect-submodules comtypes acoustic_ir_studio.py
echo. & echo Done: dist\AcousticIRStudio\AcousticIRStudio.exe & pause
