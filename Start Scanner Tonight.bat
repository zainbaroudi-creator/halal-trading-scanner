@echo off
title Halal Momentum Scanner - Watch Mode
cd /d "%~dp0"
set "BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED_PYTHON%" (
  "%BUNDLED_PYTHON%" scanner.py --watch --interval 60
) else (
  py scanner.py --watch --interval 60
)
pause
