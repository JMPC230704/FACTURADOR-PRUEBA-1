@echo off
cd /d "%~dp0"
echo Instalando herramientas (solo la primera vez)...
python -m pip install pyinstaller playwright
echo.
echo Creando el programa, esto tarda unos minutos...
python -m PyInstaller --noconfirm --windowed --name "Boletas Kedro" --collect-all playwright boletas_gui.py
echo.
echo ============================================================
echo Listo. Abre la carpeta  dist\Boletas Kedro  y usa  "Boletas Kedro.exe"
echo ============================================================
pause
