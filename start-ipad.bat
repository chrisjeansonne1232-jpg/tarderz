@echo off
rem Double-click to start polybot and also serve the dashboard to your iPad (same wifi).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Ipad %*
if errorlevel 1 pause
