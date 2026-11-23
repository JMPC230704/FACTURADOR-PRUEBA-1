@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw boletas_gui.py
) else (
  start "" /min python boletas_gui.py
)
