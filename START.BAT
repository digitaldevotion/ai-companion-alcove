@echo off
cd /d "%~dp0"
if exist "%USERPROFILE%\alcove-env\Scripts\activate.bat" call "%USERPROFILE%\alcove-env\Scripts\activate.bat"
set PYTHONUTF8=1
powershell -NoProfile -Command "$OutputEncoding = [Console]::OutputEncoding = [Console]::InputEncoding = [System.Text.Encoding]::UTF8; $_logfile = Join-Path $env:TEMP ('alcove_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.log'); python -u modules\alcove.py 2>&1 | Tee-Object -FilePath $_logfile; Write-Host ''; Write-Host ('Log saved to: ' + $_logfile)"
pause
