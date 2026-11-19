@echo off
cd /d "%~dp0"
if not exist "output\ready" mkdir "output\ready"
start "" "output\ready"
