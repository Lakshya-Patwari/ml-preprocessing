@echo off
cd /d "%~dp0backend"
py -3.14 main.py 2>nul
if errorlevel 1 py main.py
pause
