@echo off
setlocal EnableExtensions
title ClipForge bot - run now
cd /d "%~dp0"
echo.
echo Checking your channels for new videos and making Shorts. Please wait...
echo.
".venv\Scripts\python.exe" main.py
echo.
echo Done. Finished Shorts are in the folder output\ready  (double-click OPEN-FINISHED-SHORTS.bat).
echo.
pause
