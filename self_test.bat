@echo off
cd /d "%~dp0"
echo === Piezo/mic IR engine ===
".venv\Scripts\python.exe" tests\test_engine.py
echo.
echo === Tone match ===
".venv\Scripts\python.exe" tests\test_tone_match.py
pause
