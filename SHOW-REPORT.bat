@echo off
setlocal EnableExtensions
title ClipForge bot - what has it learned?
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py --report
echo.
echo The report was also saved as LEARNING-REPORT.md in this folder.
echo.
pause
