@echo off
REM Builds MapStash.exe from mapstash.pyw (with the ui folder inside it)
REM Needs Python installed (python.org, tick "Add python.exe to PATH").
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller
python -m pip install -r requirements.txt
python -m PyInstaller --onefile --windowed --name "MapStash" --icon assets/icon.ico --add-data "ui;ui" mapstash.pyw
echo.
echo Done! MapStash.exe is in the "dist" folder.
pause
