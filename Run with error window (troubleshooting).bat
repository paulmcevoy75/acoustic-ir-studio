@echo off
rem Same as "Run Acoustic IR Studio.bat" but keeps a console window open, so any error is visible.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run "Run Acoustic IR Studio.bat" once first. & pause & exit /b 1)
".venv\Scripts\python.exe" -X faulthandler acoustic_ir_studio.py
echo.
echo The app has closed. If it crashed, the message above shows why - copy it to Claude.
pause
