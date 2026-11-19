@echo off
title ClipForge bot - stop
schtasks /Delete /TN "ClipForgeBot" /F
echo.
echo The bot will no longer run by itself.
echo (Double-click SETUP-WINDOWS.bat again any time to turn it back on.)
echo.
pause
