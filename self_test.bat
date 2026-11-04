@echo off
rem Runs the full test suite (engine accuracy + the app on a fake sound card).
rem Run "Run Acoustic IR Studio.bat" once first so the .venv exists.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run "Run Acoustic IR Studio.bat" once first. & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements-dev.txt
".venv\Scripts\python.exe" -m pytest -q
pause
