@echo off
cd /d "%~dp0"
chcp 65001 > nul
python monst_helper.py %*
pause
