@echo off
cd /d "%~dp0"
chcp 65001 > nul
:loop
python monst_helper.py %*
if errorlevel 3 if not errorlevel 4 goto loop
pause
