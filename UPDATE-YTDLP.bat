@echo off
title ClipForge bot - update downloader
cd /d "%~dp0"
echo Updating the video downloader (the bot also does this by itself about once a week) ...
".venv\Scripts\python.exe" -m pip install -U "yt-dlp[default]"
echo.
pause
