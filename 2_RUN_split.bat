@echo off
title PDF Form Splitter
cd /d "%~dp0"
rem Change the profile below to the document type you are processing.
set PROFILE=profiles\release_form.json
py -V:3.13 --version >nul 2>&1
if %errorlevel%==0 (
  py -V:3.13 split_forms.py --profile %PROFILE%
) else (
  py split_forms.py --profile %PROFILE%
)
