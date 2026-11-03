@echo off
rem Builds a standalone AcousticIRStudio.exe in dist\ (run "Run Acoustic IR Studio.bat" once first)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onedir --windowed --name AcousticIRStudio --collect-all _sounddevice_data --collect-all _soundfile_data --hidden-import pycaw --hidden-import comtypes acoustic_ir_studio.py
echo. & echo Done: dist\AcousticIRStudio\AcousticIRStudio.exe & pause
