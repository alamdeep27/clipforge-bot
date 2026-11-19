@echo off
setlocal EnableExtensions
title ClipForge bot - make a test Short
cd /d "%~dp0"
echo.
echo Make one test Short from one video. Use a video that is YOURS or from a channel that said YES.
echo The video must be longer than 2 minutes.
echo.
set "LINK="
set /p LINK=Paste the video link here, then press Enter: 
if "%LINK%"=="" (
  echo You did not paste a link.
  pause
  exit /b 1
)
echo.
echo Working... this takes a few minutes. Please wait.
echo.
".venv\Scripts\python.exe" main.py --dry-run --video "%LINK%"
echo.
echo ==================================================
echo   If it says "Dry run OK", your test Short is in the folder
echo   output\dry-run  (double-click the .mp4 to watch it).
echo ==================================================
echo.
pause
