@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call "%~dp0start.bat"
".venv\Scripts\python.exe" "%~dp0setup_icon.py" %*
echo.
pause
