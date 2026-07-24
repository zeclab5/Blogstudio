@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 블로그 스튜디오를 시작합니다...
start "" pythonw studio_gui.py
exit
