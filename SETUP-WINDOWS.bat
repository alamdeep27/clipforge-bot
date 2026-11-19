@echo off
setlocal EnableExtensions
title ClipForge bot - setup
cd /d "%~dp0"

echo.
echo ==================================================
echo   ClipForge bot - one-time setup
echo ==================================================
echo.

where winget >nul 2>nul
if errorlevel 1 (
  echo Windows "winget" was not found on this computer.
  echo Open the Microsoft Store, search for "App Installer", install or update it,
  echo then double-click this file again.
  echo.
  pause
  exit /b 1
)

set "NEEDRESTART=0"

py -3 --version >nul 2>nul
if errorlevel 1 (
  echo Installing Python ...
  winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  set "NEEDRESTART=1"
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Installing ffmpeg ...
  winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
  set "NEEDRESTART=1"
)

where deno >nul 2>nul
if errorlevel 1 (
  echo Installing Deno ...
  winget install -e --id DenoLand.Deno --silent --accept-package-agreements --accept-source-agreements
  set "NEEDRESTART=1"
)

if "%NEEDRESTART%"=="1" (
  echo.
  echo --------------------------------------------------
  echo   Part 1 is done.
  echo   Now CLOSE this window, then double-click
  echo   SETUP-WINDOWS.bat one more time to finish.
  echo --------------------------------------------------
  echo.
  pause
  exit /b 0
)

echo [1/5] Creating the bot's private Python folder ...
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo.
    echo Could not create the Python folder. Take a photo of this window and show it to Claude.
    pause
    exit /b 1
  )
)

echo.
echo [2/5] Installing the bot's parts. This can take 5-10 minutes the first time. Please wait ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo The install failed. Check your internet connection and double-click this file again.
  pause
  exit /b 1
)

echo.
echo [3/5] Downloading the caption model (about 150 MB, only once) ...
".venv\Scripts\python.exe" -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8'); print('caption model ready')"
if errorlevel 1 (
  echo Could not download the caption model. The bot still works, but Shorts will have no captions until this works.
)

echo.
echo [4/5] Checking that everything works (it makes a short test video, about 3 minutes) ...
".venv\Scripts\python.exe" selftest.py

echo.
echo [5/5] Making the bot run by itself every 15 minutes while this computer is on ...
schtasks /Create /SC MINUTE /MO 15 /TN "ClipForgeBot" /TR "\"%~dp0.venv\Scripts\python.exe\" \"%~dp0main.py\"" /F
if errorlevel 1 (
  echo Could not create the schedule. Take a photo of this window and show it to Claude.
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$t = Get-ScheduledTask -TaskName 'ClipForgeBot'; $t.Settings.DisallowStartIfOnBatteries = $false; $t.Settings.StopIfGoingOnBatteries = $false; $t.Settings.StartWhenAvailable = $true; Set-ScheduledTask -InputObject $t | Out-Null"
)

echo.
echo ==================================================
echo   DONE.
echo   Next: open channels.txt in Notepad and paste the
echo   links of channels that said YES to clips.
echo   Then double-click TEST-ONE-VIDEO.bat.
echo ==================================================
echo.
pause
