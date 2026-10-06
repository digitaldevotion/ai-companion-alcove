@echo off
cd /d "%~dp0"
if exist "%USERPROFILE%\alcove-env\Scripts\activate.bat" call "%USERPROFILE%\alcove-env\Scripts\activate.bat"
python -u utils\wizard.py
pause
