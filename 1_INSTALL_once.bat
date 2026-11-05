@echo off
title PDF Form Splitter - one-time install
echo Installing what the splitter needs (only once, ~3 minutes)...
rem The OCR library needs Python 3.13 (newer versions may not have it yet).
py install 3.13 >nul 2>&1
py -V:3.13 -m pip install --upgrade -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo Python 3.13 not available - trying the default Python...
  py -m pip install --upgrade -r "%~dp0requirements.txt"
)
echo.
echo Done. From now on just double-click 2_RUN_split.bat
pause
