@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1
python build.py
if errorlevel 1 exit /b 1
echo Built dist\PlaylistBatchAssistant.exe
pause
