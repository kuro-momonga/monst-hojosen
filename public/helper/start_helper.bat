@echo off
cd /d "%~dp0"
chcp 65001 > nul
set /p P=BlueStacks の ADB ポート番号（複数の画面ならカンマ区切り 例 5575,5585,5595 / そのまま Enter で 5555）: 
if "%P%"=="" set P=5555
python monst_helper.py --port %P%
pause
