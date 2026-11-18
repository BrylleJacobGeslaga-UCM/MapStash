@echo off
REM Builds MapStash.exe from mapstash.pyw
REM Needs Python installed (python.org, tick "Add python.exe to PATH").
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --windowed --name "MapStash" mapstash.pyw
echo.
echo Done! MapStash.exe is in the "dist" folder.
pause
