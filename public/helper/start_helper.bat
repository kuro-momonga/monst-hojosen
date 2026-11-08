@echo off
cd /d "%~dp0"
chcp 65001 > nul
set /p P=BlueStacks の ADB ポート番号（そのまま Enter で 5555）: 
if "%P%"=="" set P=5555
python monst_helper.py --port %P%
pause
