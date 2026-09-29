@echo off
rem Double-click to start polybot (paper trading + dashboard). Ctrl+C stops it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
