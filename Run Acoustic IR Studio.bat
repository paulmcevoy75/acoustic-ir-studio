@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up ^(one-time, needs internet^)...
  %PY% -m venv .venv || (echo. & echo Python 3.10+ is needed: https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^) & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
)
rem install anything new since the last version (quick when nothing is missing)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || (echo Install failed - check your internet connection. & pause & exit /b 1)
start "" ".venv\Scripts\pythonw.exe" acoustic_ir_studio.py
